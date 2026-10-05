# -*- coding: utf-8 -*-
"""静态守卫：源码里不能出现「用了但没定义」的全局名。

抓的是这一类事故：函数里用了某个模块级名字，但那个名字是在**别的函数**
里 import 的 —— 运行到这一行才抛 NameError，平时测不到。
（插件市场就曾整页挂在这上面：`_market_manifest()` 用了 `RAW_TMPL`，
而 `RAW_TMPL` 只在 `_market_branch_entries()` 里导入。）

原理：编译源码，遍历每个代码对象里 LOAD_GLOBAL / LOAD_NAME 的名字，
不在「模块级绑定 ∪ 内置 ∪ 隐式 dunder」里的就是漏了定义。

运行: python tests/test_static_names.py      （全通过退出码 0）
"""
import builtins
import dis
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
sys.stdin = io.StringIO()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PASS, FAIL = [], []

# 解释器自己塞进模块命名空间的隐式名字，不算「没定义」
IMPLICIT = {"__file__", "__name__", "__doc__", "__package__", "__spec__",
            "__loader__", "__builtins__", "__debug__", "__path__",
            "__annotations__"}


def check(name, fn):
    try:
        ok = bool(fn())
    except Exception as e:
        ok = False
        print(f"  [ERR ] {name}: {type(e).__name__}: {e}")
    (PASS if ok else FAIL).append(name)
    print(("  [PASS] " if ok else "  [FAIL] ") + name)


def module_bindings(code):
    """模块级被绑定的名字：import / def / class / 赋值 / global 声明。"""
    names = set()
    for ins in dis.get_instructions(code):
        if ins.opname in ("STORE_NAME", "IMPORT_NAME", "STORE_GLOBAL"):
            names.add(ins.argval)
    return names


def walk_codes(code):
    yield code
    for const in code.co_consts:
        if hasattr(const, "co_name"):
            yield from walk_codes(const)


def undefined_names(path: Path):
    """返回 [(所在函数, 名字)]，全部是运行到就会 NameError 的引用。"""
    code = compile(path.read_bytes(), str(path), "exec")
    known = module_bindings(code) | IMPLICIT | set(dir(builtins))
    bad = []
    for sub in walk_codes(code):
        for ins in dis.get_instructions(sub):
            if ins.opname in ("LOAD_GLOBAL", "LOAD_NAME") and ins.argval not in known:
                bad.append((sub.co_name, ins.argval))
    return bad


def source_files():
    return [ROOT / "main.py"] + sorted(
        p for p in (ROOT / "modules").glob("*.py") if p.name != "__init__.py")


def s1():
    """主程序与全部业务模块都没有「用了没定义」的全局名。"""
    bad = []
    for path in source_files():
        for fn, name in undefined_names(path):
            bad.append(f"{path.name}::{fn} -> {name}")
    if bad:
        print("       " + "\n       ".join(bad))
    return not bad


def s2():
    """守卫本身有效：给一段故意漏定义的代码，必须能报出来。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="static_guard_")) / "sample.py"
    tmp.write_text("CONST = 1\n\n\ndef f():\n    return MISSING_NAME\n", encoding="utf-8")
    bad = undefined_names(tmp)
    return len(bad) == 1 and bad[0] == ("f", "MISSING_NAME")


def s3():
    """守卫不误报：同一段代码把名字 import 进来就该干净。"""
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="static_guard_")) / "sample.py"
    tmp.write_text("def f():\n    from os import sep\n    return sep\n", encoding="utf-8")
    return not undefined_names(tmp)


def s4():
    """守卫覆盖到模块文件，而不是只扫 main.py。"""
    return len(source_files()) > 10


def _none_global_gaps(source: str):
    """返回 (未赋值, 赋值了但漏写 global) 两组模块级 None 全局名。"""
    import ast
    tree = ast.parse(source)

    def _is_none(value):
        return isinstance(value, ast.Constant) and value.value is None

    # 只认带类型标注的声明（`x: Optional[T] = None`）：不带标注的模块级 None
    # 由各自的函数自己 `global` 后赋值，不属于「必须由 main() 实例化」这一类
    declared, module_assigned = set(), set()
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            (declared if _is_none(node.value) else module_assigned).add(node.target.id)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not _is_none(node.value):
                    module_assigned.add(target.id)
    if not declared:
        return set(), set()
    assigned, global_declared = set(), set()
    for node in tree.body:
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "main"):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Global):
                global_declared.update(sub.names)
            targets = sub.targets if isinstance(sub, ast.Assign) else (
                [sub.target] if isinstance(sub, ast.AnnAssign) else [])
            assigned.update(t.id for t in targets if isinstance(t, ast.Name))
    return declared - assigned, assigned & declared - global_declared - module_assigned


def s5():
    """模块级声明为 `X = None` 的全局，必须在 main() 里被赋值，且该赋值真的写到全局上。

    抓的是这一类事故：管理器在模块级声明成 None、各处调用都写了 `if xxx is not None`
    的兜底，唯独 main() 里忘了实例化 —— 整条链路静默失效，接口一律返回空数据，
    日志里连一条报错都没有。

    只查「main() 里出现过这个赋值语句」是不够的：**漏写 `global X` 时，
    `X = XxxManager(...)` 只是建了个局部变量**，模块级那个仍然是 None，症状与
    「忘了实例化」一模一样（会话回忆/承诺追踪/奇遇三个功能就是这么静默失效的）。
    所以还要要求该名字出现在某处 `global` 声明里，或本来就是模块级赋值。
    """
    missing, local_only = _none_global_gaps((ROOT / "main.py").read_text(encoding="utf-8"))
    if missing:
        print("       未在 main() 里赋值: " + "\n       ".join(sorted(missing)))
    if local_only:
        print("       main() 里赋值但漏写 global（仍是 None）: "
              + "\n       ".join(sorted(local_only)))
    return not missing and not local_only


def s6():
    """守卫本身要能报出漏写 global —— 否则它对这类事故就是形同虚设。"""
    bad = (
        "import typing\n"
        "mgr: typing.Optional[object] = None\n"
        "def main():\n"
        "    mgr = object()\n"
    )
    missing, local_only = _none_global_gaps(bad)
    if sorted(local_only) != ["mgr"]:
        print(f"       漏写 global 的用例没被报出: {local_only!r}")
        return False
    good = (
        "import typing\n"
        "mgr: typing.Optional[object] = None\n"
        "def main():\n"
        "    global mgr\n"
        "    mgr = object()\n"
    )
    return _none_global_gaps(good) == (set(), set())


def s7():
    """忘了在 main() 里实例化（连赋值语句都没有）同样要报出来。"""
    bad = (
        "import typing\n"
        "mgr: typing.Optional[object] = None\n"
        "def main():\n"
        "    pass\n"
    )
    missing, _local = _none_global_gaps(bad)
    if sorted(missing) != ["mgr"]:
        print(f"       未实例化的用例没被报出: {missing!r}")
        return False
    return True


def main():
    print("=" * 70)
    print("静态守卫：未定义的全局名")
    print("=" * 70)
    for n, f in [("源码无未定义全局名", s1), ("守卫能报出漏定义", s2),
                 ("守卫不误报函数内 import", s3), ("扫描覆盖全部模块", s4),
                 ("模块级 None 全局都在 main() 里赋值", s5),
                 ("守卫能报出漏写 global", s6),
                 ("守卫能报出漏实例化", s7)]:
        check(n, f)
    print("\n" + "=" * 70)
    print(f"结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
    for n in FAIL:
        print("  - " + n)
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
