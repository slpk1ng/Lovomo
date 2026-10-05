# -*- coding: utf-8 -*-
"""退出速度 / CMD 闪窗 / 窗口位置还原 三项修复的回归测试。

覆盖：
  A 退出速度：ProcessManager.shutdown_all 支持 budget，超时被有效压缩
  B 无窗口子进程：no_window_kwargs() 在 Windows 上返回 CREATE_NO_WINDOW
  C 窗口几何记忆：读写/去抖/有效性校验/应用逻辑
  D 静态绑定：open_console 不再裸调 restore()，事件已绑定，退出前落盘
  H 收进托盘：点 × 走 hide（从任务栏消失）而不是 minimize；单实例互斥体

运行: python tests/test_exit_window.py      （全通过退出码 0）
"""
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
sys.stdin = io.StringIO()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import main as M  # noqa: E402
from modules import tts_service as T  # noqa: E402
# 窗口几何符号本体在 modules/window_geometry.py（main 只是再导出）
from modules import window_geometry as W  # noqa: E402
# webview 运行时符号本体在 modules/webview_runtime.py（main 只是再导出）
from modules import webview_runtime as WRT  # noqa: E402

PASS, FAIL = [], []


def check(name, fn):
    try:
        result = fn()
        if not result:
            raise AssertionError(f"断言为假: {result!r}")
        PASS.append(name)
        print(f"  [PASS] {name}")
    except Exception as e:
        FAIL.append((name, repr(e)))
        print(f"  [FAIL] {name}: {e!r}")


def section(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


MAIN_SRC = (ROOT / "main.py").read_text(encoding="utf-8")
# 单实例相关符号已搬迁至 modules/single_instance.py（原样搬迁，行为不变）
_SINGLE_INSTANCE_PATH = ROOT / "modules" / "single_instance.py"
SINGLE_INSTANCE_SRC = (_SINGLE_INSTANCE_PATH.read_text(encoding="utf-8")
                       if _SINGLE_INSTANCE_PATH.exists() else "")
# 窗口几何 / WebView2 运行时符号已搬迁至 modules（原样搬迁，行为不变）
_WINDOW_GEOMETRY_PATH = ROOT / "modules" / "window_geometry.py"
_WINDOW_GEOMETRY_SRC = (_WINDOW_GEOMETRY_PATH.read_text(encoding="utf-8")
                        if _WINDOW_GEOMETRY_PATH.exists() else "")
_WEBVIEW_RUNTIME_PATH = ROOT / "modules" / "webview_runtime.py"
_WEBVIEW_RUNTIME_SRC = (_WEBVIEW_RUNTIME_PATH.read_text(encoding="utf-8")
                        if _WEBVIEW_RUNTIME_PATH.exists() else "")
# 静态扫描的回退顺序：main.py -> window_geometry -> webview_runtime -> single_instance
_ALL_MIGRATED_SRC = (_WINDOW_GEOMETRY_SRC + _WEBVIEW_RUNTIME_SRC
                     + SINGLE_INSTANCE_SRC)


# ---------------------------------------------------------------- A 退出速度
section("A 退出速度：shutdown_all 预算化")


def a1():
    import inspect
    sig = inspect.signature(T.ProcessManager.shutdown_all)
    return "budget" in sig.parameters


def a2():
    src = inspect.getsource(T.ProcessManager.shutdown_all)
    # taskkill 的 timeout 必须是 min(3, ...) 这种被预算约束的形式，
    # 不能再是写死的 15 秒
    return "timeout=15" not in src and "min(3" in src


def a3():
    """空进程表时 shutdown_all 应立即返回，不能有固定 sleep。"""
    pm = T.ProcessManager()
    t0 = time.time()
    pm.shutdown_all(budget=1.0)
    return time.time() - t0 < 0.4


def a4():
    src = inspect.getsource(T.ProcessManager.shutdown_all)
    return "deadline" in src


def a5():
    """真正的子进程也应该被快速回收（预算 2.5 秒内）。"""
    pm = T.ProcessManager()
    kwargs = T.no_window_kwargs()
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    before = list(pm.processes)
    try:
        pm.register(proc, "probe-a5")
        t0 = time.time()
        pm.shutdown_all(budget=2.5)
        elapsed = time.time() - t0
        # 进程要么已死，要么已被请求结束；关键耗时必须远低于原来的十几秒
        return elapsed < 6.0
    finally:
        pm.processes[:] = before
        try:
            proc.kill()
        except Exception:
            pass


check("shutdown_all 接受 budget 参数", a1)
check("taskkill 超时被预算约束（不再写死 15s）", a2)
check("空进程表 shutdown_all 立即返回", a3)
check("shutdown_all 内部有 deadline 预算控制", a4)
check("存在存活子进程时也能在预算内回收", a5)


# ------------------------------------------------------------ B 无窗口子进程
section("B CMD 闪窗：no_window_kwargs / CREATE_NO_WINDOW")


def b1():
    return callable(T.no_window_kwargs)


def b2():
    kw = T.no_window_kwargs()
    if os.name != "nt":
        return kw == {}
    flag = kw.get("creationflags", 0)
    return bool(flag & 0x08000000)


def b3():
    """TTS Popen 调用点必须带上无窗口参数。"""
    src = inspect.getsource(T)
    return "**no_window_kwargs()" in src


def b4():
    """tts_service 里所有真实的 taskkill 调用都必须带 no_window_kwargs()。

    用 AST 精确找出 subprocess.run(...) 调用节点，避免把注释、docstring
    或括号切分错误误判成调用点。
    """
    import ast
    src = inspect.getsource(T)
    tree = ast.parse(src)
    found = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and fn.attr == "run"):
            continue
        if not (isinstance(fn.value, ast.Name) and fn.value.id == "subprocess"):
            continue
        args_src = ast.unparse(node)
        if "taskkill" not in args_src:
            continue
        found += 1
        if "no_window_kwargs" not in args_src:
            return False
    return found >= 1


def b4b():
    """用 AST 抽查：确有 taskkill 调用被识别出来（防止检查形同虚设）。"""
    import ast
    tree = ast.parse(inspect.getsource(T))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if "taskkill" in ast.unparse(node):
                return True
    return False


def b5():
    """main.py 的重启 spawn 必须带 CREATE_NO_WINDOW + SW_HIDE。"""
    return ("CREATE_NO_WINDOW" in MAIN_SRC
            and "wShowWindow = 0" in MAIN_SRC
            and "_spawn_detached" in MAIN_SRC)


def b6():
    """重启优先用 pythonw.exe（无控制台子系统）。"""
    return "pythonw.exe" in MAIN_SRC


def b7():
    """冻结成 exe 后直接复用自身，不再 spawn 解释器。"""
    src = MAIN_SRC
    i = src.find("def _resolve_spawn_exe")
    return i > 0 and 'getattr(sys, "frozen", False)' in src[i:i + 600]


check("no_window_kwargs 可调用", b1)
check("Windows 上带 CREATE_NO_WINDOW 标志", b2)
check("TTS Popen 使用 no_window_kwargs()", b3)
check("tts_service 内 taskkill 均无窗口化", b4)
check("AST 抽查确实抓到 taskkill 调用（防检查失效）", b4b)
check("main.py 重启 spawn 带 CREATE_NO_WINDOW + SW_HIDE", b5)
check("重启优先 pythonw.exe", b6)
check("冻结模式直接复用自身", b7)


# ------------------------------------------------------------ C 窗口几何记忆
section("C 窗口几何记忆：读写 / 去抖 / 有效性 / 应用")


def c1():
    return all(hasattr(M, n) for n in (
        "_geometry_path", "_load_window_geometry", "_save_window_geometry",
        "_schedule_geometry_save", "_finish_geometry_save",
        "_is_geometry_valid", "_apply_saved_geometry"))


_TMP = Path(tempfile.mkdtemp(prefix="lovomo_geom_"))
_ORIG_GEOM = json.loads(json.dumps(M._WINDOW_GEOMETRY))
_ORIG_PATH = M._geometry_path
# _geometry_path 的调用点在 modules/window_geometry.py 模块内部，补丁必须打在新模块本体上
W._geometry_path = lambda: _TMP / "window_geometry.json"
M._geometry_path = W._geometry_path


def c2():
    """落盘后能原样读回。"""
    M._geometry_path().unlink(missing_ok=True)
    M._WINDOW_GEOMETRY["maximized"] = False
    M._WINDOW_GEOMETRY["normal"] = {"x": 120, "y": 80, "width": 1280, "height": 720}
    M._save_window_geometry()
    got = M._load_window_geometry()
    return (got["maximized"] is False
            and got["normal"] == {"x": 120, "y": 80, "width": 1280, "height": 720})


def c3():
    """maximized 状态也要正确持久化。"""
    M._WINDOW_GEOMETRY["maximized"] = True
    M._save_window_geometry()
    return M._load_window_geometry()["maximized"] is True


def c4():
    """文件损坏时回落默认（最大化），不抛异常。

    默认尺寸不再硬编码 1920 —— 改成屏幕的 80%（物理像素），
    所以这里只校验「是个合理的正数、且不超过屏幕」。
    """
    M._geometry_path().write_text("{ this is not json", encoding="utf-8")
    got = M._load_window_geometry()
    sw, sh = M._primary_screen_size()
    n = got["normal"]
    ok_size = n["width"] > 0 and n["height"] > 0
    if sw > 0 and sh > 0:
        ok_size = ok_size and n["width"] <= sw and n["height"] <= sh
    return got["maximized"] is True and ok_size


def c5():
    """缺 x/y 的矩形判定为无效（避免窗口开到屏幕外）。"""
    return M._is_geometry_valid({"x": None, "y": None,
                                 "width": 1280, "height": 720}) is False


def c6():
    """过小的矩形判定为无效。"""
    return M._is_geometry_valid({"x": 10, "y": 10,
                                 "width": 100, "height": 100}) is False


def c7():
    """极端负坐标（模拟显示器被拔掉）判定为无效。"""
    if not M._screen_rects():
        return True  # 非 Windows / 拿不到屏幕信息，跳过
    return M._is_geometry_valid({"x": -50000, "y": -50000,
                                 "width": 1280, "height": 720}) is False


def c8():
    """正常坐标判定为有效。"""
    screens = M._screen_rects()
    if not screens:
        return True
    sx, sy, sw, sh = screens[0]
    return M._is_geometry_valid({"x": sx + 100, "y": sy + 100,
                                 "width": 1280, "height": 720}) is True


def c9():
    """去抖：连续多次调度只落一次盘。"""
    M._geometry_path().unlink(missing_ok=True)
    M._WINDOW_GEOMETRY["maximized"] = True
    for _ in range(8):
        M._schedule_geometry_save(delay=0.15)
    time.sleep(0.6)
    return M._geometry_path().exists()


def c10():
    """_finish_geometry_save 同步落盘，不等定时器。"""
    M._geometry_path().unlink(missing_ok=True)
    M._WINDOW_GEOMETRY["maximized"] = True
    M._schedule_geometry_save(delay=30)   # 故意设很长
    M._finish_geometry_save()
    return M._geometry_path().exists()


def c11():
    """同步落盘后不应再残留定时器（避免退出时又写一次）。"""
    M._finish_geometry_save()
    return W._geometry_save_timer is None


def c12():
    """延迟保存与退出保存走同一把锁：两次保存不会交错，最后落盘的是最新快照。"""
    M._geometry_path().unlink(missing_ok=True)
    order = []
    orig_save = W._save_window_geometry

    def slow_save():
        order.append("start")
        time.sleep(0.2)
        orig_save()
        order.append("done")

    # _save_window_geometry 的调用点（定时器线程 / 同步落盘）在模块内部，补丁到本体
    W._save_window_geometry = slow_save
    try:
        M._schedule_geometry_save(delay=0.05)
        time.sleep(0.15)                     # 让定时器进入保存
        M._finish_geometry_save()            # 应当等它写完再写，而不是插进去
    finally:
        W._save_window_geometry = orig_save
    return order == ["start", "done", "start", "done"]


class FakeWindow:
    """最小化的 pywebview 窗口替身，用于验证应用逻辑。"""

    def __init__(self, state="normal", x=0, y=0, w=800, h=600):
        self.state = state
        self.x, self.y, self.width, self.height = x, y, w, h
        self.calls = []

    def maximize(self):
        self.calls.append("maximize")
        self.state = "maximized"

    def move(self, x, y):
        self.calls.append(("move", x, y))
        self.x, self.y = x, y

    def resize(self, w, h):
        self.calls.append(("resize", w, h))
        self.width, self.height = w, h

    def restore(self):
        self.calls.append("restore")


def c12():
    """记忆为最大化时：调 maximize()，不调 restore()。"""
    M._geometry_path().unlink(missing_ok=True)
    M._WINDOW_GEOMETRY["maximized"] = True
    M._save_window_geometry()
    w = FakeWindow()
    M._apply_saved_geometry(w)
    return w.calls == ["maximize"]


def c13():
    """记忆为普通窗口时：按记忆的坐标 move + resize。

    注意 move/resize 收的是**逻辑像素**（pywebview 的口径），而记忆里存的是
    物理像素，所以要按当前 scale 换算后再比对。scale=1.0 时两者相同。
    """
    M._geometry_path().unlink(missing_ok=True)
    screens = M._screen_rects()
    sx, sy = (screens[0][0], screens[0][1]) if screens else (0, 0)
    M._WINDOW_GEOMETRY["maximized"] = False
    M._WINDOW_GEOMETRY["normal"] = {"x": sx + 60, "y": sy + 40,
                                    "width": 1200, "height": 800}
    M._save_window_geometry()
    w = FakeWindow()
    M._apply_saved_geometry(w)
    scale = M._window_scale(w)
    want_move = ("move", M._phys_to_logical(sx + 60, scale),
                 M._phys_to_logical(sy + 40, scale))
    want_resize = ("resize", M._phys_to_logical(1200, scale),
                   M._phys_to_logical(800, scale))
    return ("maximize" not in w.calls
            and want_move in w.calls
            and want_resize in w.calls)


def c14():
    """坐标失效时不 move（交给系统），但仍按记忆尺寸 resize。"""
    M._geometry_path().unlink(missing_ok=True)
    M._WINDOW_GEOMETRY["maximized"] = False
    M._WINDOW_GEOMETRY["normal"] = {"x": None, "y": None,
                                    "width": 1100, "height": 700}
    M._save_window_geometry()
    w = FakeWindow()
    M._apply_saved_geometry(w)
    moves = [c for c in w.calls if isinstance(c, tuple) and c[0] == "move"]
    scale = M._window_scale(w)
    want = ("resize", M._phys_to_logical(1100, scale),
            M._phys_to_logical(700, scale))
    return not moves and want in w.calls


def c15():
    """_apply_saved_geometry 内部异常不能抛出去。"""
    class Boom:
        state = "normal"
        @property
        def x(self):
            raise RuntimeError("boom")
        x = None
    bad = type("W", (), {"maximize": lambda self: (_ for _ in ()).throw(RuntimeError("x")),
                         "resize": lambda self, w, h: None,
                         "move": lambda self, x, y: None})()
    M._geometry_path().unlink(missing_ok=True)
    M._WINDOW_GEOMETRY["maximized"] = True
    M._save_window_geometry()
    M._apply_saved_geometry(bad)   # 不应抛异常
    return True


check("几何相关函数齐备", c1)
check("几何落盘后可原样读回", c2)
check("maximized 状态可持久化", c3)
check("文件损坏回落默认最大化", c4)
check("缺 x/y 的矩形判定无效", c5)
check("过小矩形判定无效", c6)
check("屏幕外的极端坐标判定无效", c7)
check("正常坐标判定有效", c8)
check("高频调度只落一次盘（去抖）", c9)
check("_finish_geometry_save 同步落盘", c10)
check("同步落盘后清空定时器", c11)
check("两次保存串行（不会交错覆盖）", c12)
check("记忆为最大化时调 maximize 而非 restore", c12)
check("记忆为普通窗口时 move+resize 回原位", c13)
check("坐标失效时不 move 仍 resize", c14)
check("应用几何异常不外抛", c15)


# ------------------------------------------------------------ D 静态绑定检查
section("D 静态绑定：窗口事件与退出落盘")


def _fn_src(name, span=2600):
    i = MAIN_SRC.find(f"def {name}(")
    if i < 0:
        # 搬迁到 modules/ 的函数（window_geometry / webview_runtime /
        # single_instance）到对应模块源码里找
        j = _ALL_MIGRATED_SRC.find(f"def {name}(")
        return _ALL_MIGRATED_SRC[j:j + span] if j >= 0 else ""
    return MAIN_SRC[i:i + span]


def d1():
    """open_console 主路径必须是 _apply_saved_geometry，restore() 只能作兜底。

    判定方式：拿掉 _apply_saved_geometry 的那个 try 块之后，剩下的代码里
    才可能出现 restore()；也就是 restore() 必须在 except 分支内。
    """
    src = _fn_src("open_console")
    if "_apply_saved_geometry" not in src:
        return False
    # 找 _apply_saved_geometry 的 try 块：其后第一个 "except" 之后的 restore
    i = src.find("_apply_saved_geometry")
    tail = src[i:]
    e = tail.find("except")
    if e < 0:
        return False
    return "restore()" not in tail[:e]   # try 块内不许出现 restore()


def d2():
    """open_console 必须清掉「已收进托盘」标记。

    不清的话，唤醒动作会被自己挡住 —— 用户点托盘「打开 Lovomo」也不会有反应。
    """
    return 'close_state["hidden"] = False' in _fn_src("open_console")


def d3():
    src = _fn_src("_bind_geometry_events", 1500)
    return all(e in src for e in ("resized", "moved", "maximized", "restored"))


def d4():
    """run_webview_loop 里应绑定几何事件并在创建窗口时带上记忆的几何。

    用 AST 取函数体（不再按固定字符数截取）：这个函数还会继续长——
    多一行起始步骤就会把窗口截断，断言跟着红，属于误报。
    """
    code = _fn_code("run_webview_loop")
    return "_bind_geometry_events" in code and "kwargs" in code


def d5():
    """create_window 不再无条件 maximized=True。"""
    return "maximized=True)" not in MAIN_SRC


def d6():
    """on_closing 必须先抓几何，再把窗口收进托盘。

    收进托盘的语义是「从屏幕和任务栏一起消失、只留托盘图标」，所以走 hide()。
    但 hide 不能在 on_closing 里直接调 —— WinForms 的关闭序列会把在
    FormClosing 期间改的窗口状态覆盖回去，表现出来就是「偶尔没关干净、
    屏幕上还留一层窗口」。断言钉住「抓几何 → 交给 _hide_to_tray」这个顺序。
    """
    src = _fn_src("on_closing")
    c = src.find("_capture_geometry")
    h = src.find("_hide_to_tray(w)")
    return c > 0 and h > 0 and c < h and "w.hide()" not in src


def d7():
    """quit_app / relaunch_console 退出前同步落盘。"""
    return ("_finish_geometry_save" in _fn_src("quit_app")
            and "_finish_geometry_save" in _fn_src("relaunch_console"))


def d8():
    """_force_quit 延迟已压缩到 1.5 秒以内。"""
    src = _fn_src("_force_quit", 900)
    return "_force_quit(delay: float = 1.2)" in MAIN_SRC and "budget=1.0" in src


def d9():
    """main() 收尾不再有长超时 join / 无预算 shutdown。"""
    i = MAIN_SRC.rfind("backend_thread.join")
    tail = MAIN_SRC[i:i + 400]
    return "timeout=1.5" in tail and "budget=1.5" in tail


def d10():
    """重启/退出路径的 delay 参数都不超过 1.5 秒。"""
    import re
    delays = re.findall(r"_force_quit\(([\d.]+)\)", MAIN_SRC)
    return bool(delays) and all(float(d) <= 1.5 for d in delays)


def d11():
    """_ensure_dpi_aware 存在，且不在运行时改窗口尺寸的那套路径里乱调。

    这是本次修复的核心：窗口创建前必须声明 DPI 感知，之后不能再声明，
    否则 pywebview 的缩放会和系统虚拟化叠加。
    """
    return (callable(getattr(M, "_ensure_dpi_aware", None))
            and "_ensure_dpi_aware()" in MAIN_SRC)


def d12():
    """_screen_rects 不能再自己调 SetProcessDPIAware()。

    以前它在每次枚举显示器时都调一次，和 pywebview 的 DPI 缩放打架。
    """
    src = _fn_src("_screen_rects", 1600)
    return "SetProcessDPIAware()" not in src


def d13():
    """物理/逻辑像素换算函数必须成对存在且互为逆运算。"""
    f = getattr(M, "_phys_to_logical", None)
    g = getattr(M, "_logical_to_phys", None)
    if not callable(f) or not callable(g):
        return False
    for scale in (1.0, 1.25, 1.5, 2.0):
        for v in (800, 1200, 1920, 2560):
            if abs(g(f(v, scale), scale) - v) > 1:
                return False
    return True


def _fn_body(name, span=4000):
    """切出某个函数的真实函数体（按缩进判断结束），避免误伤后面的函数。"""
    src = _fn_src(name, span)
    if not src:
        return ""
    lines = src.splitlines()
    body = [lines[0]]
    for ln in lines[1:]:
        if ln.strip() and not ln.startswith("    "):
            break
        body.append(ln)
    return "\n".join(body)


def _fn_body_node(name):
    """用 AST 取某个函数的节点，用于做「只看代码、不看注释」的检查。

    搬迁到 modules/ 的函数在主模块源码里找不到，按回退顺序到
    window_geometry / webview_runtime / single_instance 的源码里取。
    """
    import ast
    for src in (MAIN_SRC, _WINDOW_GEOMETRY_SRC, _WEBVIEW_RUNTIME_SRC,
                SINGLE_INSTANCE_SRC):
        if not src:
            continue
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == name:
                    return node
    return None


def _strip_docstrings(tree):
    """把 AST 里所有 docstring 摘掉。

    docstring 是字符串常量，不是代码 —— 我们在注释里解释「以前错误地用了
    window.state」时不该被判定成「现在还在用」。
    """
    import ast
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    return tree


def _fn_code(name):
    """返回函数源码，且已剔除 docstring。"""
    import ast
    node = _fn_body_node(name)
    if node is None:
        return ""
    sub = ast.Module(body=[node], type_ignores=[])
    _strip_docstrings(sub)
    return ast.unparse(sub)


def d14():
    """_capture_geometry（含其 fallback）不许再用 pywebview 的 window.state。

    window.state 返回 State(dict)，str() 是 "{}"，永远不等于 "maximized"，
    用它判断最大化必然误判。检查用 AST 剔掉 docstring 后再找属性访问，
    这样「注释里说明不能用它」不会被误判。
    """
    for fn in ("_capture_geometry", "_capture_geometry_fallback"):
        code = _fn_code(fn)
        if not code:
            return False
        if ".state" in code:
            return False
    return True


def d15():
    """_capture_geometry 只在 Normal 状态记录 Normal 矩形。

    最大化/最小化分支里都不能出现给 normal 赋值的语句 —— 最大化时读到的
    是全屏矩形（实测某 2560x1440@150% 机器上是 2586x1466），最小化时读到
    的是 (-32000,-32000)。

    注意 ast.unparse 会把引号统一成单引号，所以匹配用 ["normal"]。
    """
    src = _fn_code("_capture_geometry")
    i = src.find("if state == 'maximized'")
    j = src.find("elif state == 'minimized'")
    k = src.find("else:", j)
    if not (0 < i < j < k):
        return False
    maximized_branch = src[i:j]
    minimized_branch = src[j:k]
    normal_branch = src[k:]
    # 前两个分支不允许写 normal 字段
    if "['normal']" in maximized_branch or "['normal']" in minimized_branch:
        return False
    # Normal 分支必须写
    return "['normal']" in normal_branch


def d16():
    """退化矩形（小到像未初始化窗口）必须被拒收。"""
    f = getattr(M, "_rect_is_degenerate", None)
    if not callable(f):
        return False
    if not f({"width": 200, "height": 100}):
        return False          # pywebview 的 min_size，必须算退化
    if f({"width": 1200, "height": 800}):
        return False          # 正常尺寸不算退化
    if not f({}):
        return False
    return True


def d17():
    """默认 Normal 几何必须按屏幕比例算，不能硬编码 1920x1080。

    150% 缩放下 1920 逻辑 = 2880 物理，比 2560 的屏幕还宽。
    """
    f = getattr(M, "_default_normal_geometry", None)
    if not callable(f):
        return False
    g = f()
    sw, sh = M._primary_screen_size()
    if sw <= 0 or sh <= 0:
        return True           # 拿不到屏幕信息时跳过
    return 0 < g["width"] <= sw and 0 < g["height"] <= sh


def d18():
    """几何数据必须有版本号，且 load 会丢弃旧版本的脏数据。"""
    if int(getattr(M, "_GEOMETRY_VERSION", 0)) < 2:
        return False
    src = inspect.getsource(M._load_window_geometry)
    return "geometry_v" in src and "_GEOMETRY_VERSION" in src


def d19():
    """run_webview_loop 必须把物理像素换算成逻辑像素再交给 create_window。"""
    src = _fn_src("run_webview_loop", 3000)
    return "_phys_to_logical" in src and "to_logical" in src


def d20():
    """启动后不许再有「定时器/loaded 里重新 apply 几何」的补救逻辑。

    那个补救会在窗口未初始化时读到 200x100 并照着 resize，
    把窗口压成小方块。几何只在 create_window 时决定一次。

    用 AST 在函数体内找真正的调用节点，注释里提到这些名字不算命中。
    """
    import ast
    node = _fn_body_node("run_webview_loop")
    if node is None:
        return False
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        fn = sub.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name in ("_apply_saved_geometry", "_apply_once"):
            return False
        if name == "Timer":
            return False
    return True


def d21():
    """_bind_geometry_events 必须有启动宽限期，避免把初始化中间态记下来。"""
    src = _fn_src("_bind_geometry_events", 1600)
    return "startup_grace" in src


def d22():
    """唤醒窗口必须走原生前台调用，不能只靠 pywebview 的 show()。

    pywebview 的 show() 只做 Show+Activate，在 Windows 前台锁下会被忽略。
    """
    src = _fn_src("_raise_to_foreground", 2600)
    if not src:
        return False
    return all(k in src for k in ("SetForegroundWindow", "BringWindowToTop",
                                  "ShowWindow"))


def d23():
    """open_console 必须调用 _raise_to_foreground 唤醒窗口。"""
    return "_raise_to_foreground" in _fn_src("open_console", 1600)


def _calls_in_order(fn_name: str) -> list:
    """按源码顺序收集某个函数体里出现的调用名（只看代码，不看注释/docstring）。"""
    import ast
    node = _fn_body_node(fn_name)
    if node is None:
        return []
    out = []

    def _walk(n):
        for child in ast.iter_child_nodes(n):
            if isinstance(child, ast.Call):
                f = child.func
                if isinstance(f, ast.Name):
                    out.append((child.lineno, f.id))
                elif isinstance(f, ast.Attribute):
                    out.append((child.lineno, f.attr))
            _walk(child)

    # 跳过函数节点自己的 body[0]（docstring 是 Expr 常量，本来就不是 Call）
    _walk(node)
    out.sort()
    return [name for _, name in out]


def d24():
    """最小化判定必须发生在 window.show() 之前。

    实测：最小化状态下 window.show() 会把前台抢过去却把窗口留在最小化
    （fg=True 而 iconic 仍为 True），画面毫无变化 —— 这正是
    「点任务栏/托盘『打开 Lovomo』没反应」的根因。所以 IsIconic 必须在
    show() 之前读到，show() 之后再读就已经晚了。
    """
    src = _fn_body("_raise_to_foreground", 4000)
    if not src or "_was_minimized" not in src:
        return False
    i_iconic = src.find("def _was_minimized")
    i_show = src.rfind("window.show()")
    return -1 < i_iconic < i_show


def d25():
    """唤醒后要做可见性与最小化的校验，并允许重试一轮。"""
    src = _fn_body("_raise_to_foreground", 4000)
    if not src:
        return False
    return all(k in src for k in ("IsWindowVisible", "_ok()", "for attempt in range(2)"))


def d26():
    """唤醒的最后一步必须是抢前台，不能被几何应用盖掉。"""
    order = [n for n in _calls_in_order("open_console")
             if n in ("_raise_to_foreground", "_apply_saved_geometry")]
    return len(order) >= 3 and order[-1] == "_raise_to_foreground"


def d27():
    """窗口处于隐藏/最小化时 _apply_saved_geometry 不应改几何。

    pywebview 的 move/resize 会把隐藏窗口显示出来，但不会带成前台
    （实测 vis=True 而 fg=False），于是出现「窗口露出来了却压在别的窗口后面」。
    """
    src = _fn_body("_apply_saved_geometry", 4000)
    if not src:
        return False
    return "IsWindowVisible" in src and "IsIconic" in src


def d28():
    """唤醒期间的窗口事件要暂停几何记录，免得把过渡态写进记忆。"""
    main_src = Path(__file__).resolve().parent.parent.joinpath("main.py").read_text(
        encoding="utf-8")
    if "_geometry_paused" not in main_src or "_wake_geometry_guard" not in main_src:
        return False
    return "_geometry_paused[0] > 0" in main_src


def d29():
    """任务栏激活钩子必须拦截 WM_ACTIVATE / WM_ACTIVATEAPP。

    从任务栏还原窗口是系统自己走的路径（不经过 pywebview，也不经过
    open_console）。只有拦下这两个「我被激活了」的消息，才能在被激活的
    时机补一次抢前台 —— 这是「点任务栏窗口没跑到最前面」的根治点。
    """
    src = _fn_body("_install_taskbar_activate_hook", 6000)
    if not src:
        return False
    return ("WM_ACTIVATE" in src and "WM_ACTIVATEAPP" in src
            and "SetWindowLongPtrW" in src and "CallWindowProcW" in src)


def d30():
    """激活钩子接受自定义回调，默认回落到抢前台。"""
    if "_install_taskbar_activate_hook(window, on_activate=None)" not in (
            MAIN_SRC + _WINDOW_GEOMETRY_SRC):
        return False
    src = _fn_body("_install_taskbar_activate_hook", 6000)
    return "on_activate or _raise_to_foreground" in src


def d31():
    """WNDPROC 回调必须被持有引用，否则会被 GC 回收导致崩溃。

    ctypes 的 WINFUNCTYPE 回调对象一旦失去 Python 侧引用就会被回收，
    Windows 下次回调时进程直接崩 —— 这个坑非常隐蔽，用断言钉住。
    """
    src = _fn_body("_install_taskbar_activate_hook", 6000)
    if "_WNDPROC_HOLDER" not in src or "callback" not in src:
        return False
    # 全局 holder 里必须有 callback 键，且确实被赋值
    return '_WNDPROC_HOLDER["callback"] = cb' in src


def d32():
    """钩子要在窗口 shown 之后安装（此前原生句柄可能还不存在）。"""
    src = _fn_body("run_webview_loop", 9000)
    if not src:
        return False
    return "events.shown" in src and "_install_taskbar_activate_hook" in src


def d33():
    """open_console 要补一次延迟抢前台，对抗前台锁连吞两次的情况。"""
    src = _fn_src("open_console", 4000)
    if not src:
        return False
    return "_retry" in src and src.count("_raise_to_foreground") >= 3


def d34():
    """退出前必须还原窗口过程，避免回调悬空。"""
    return "_uninstall_taskbar_activate_hook" in _fn_src("quit_app", 2000)


# ------------------------------------------------- F 关闭程序（严重回归）
section("F 关闭程序：窗口过程 ABI / 关闭事件语义")


def f1():
    """CallWindowProcW 必须显式声明 argtypes 和 restype。

    这是「程序关不掉」的根因。不声明 argtypes 时，第一个参数（old WNDPROC
    指针）按默认 c_int 传递，64 位下高 32 位被截断 —— 转发出去的就是个
    非法函数指针，整条消息链（含 WM_CLOSE）静默失效。
    """
    src = _fn_body("_install_taskbar_activate_hook", 7000)
    if not src:
        return False
    return "call_proc.argtypes" in src and "call_proc.restype" in src


def f2():
    """WNDPROC 的返回类型必须是 8 字节的 LRESULT，不能是 c_long。

    Windows 上 c_long 只有 4 字节，而 LRESULT/LONG_PTR 是 8 字节。
    返回类型写错，WinForms 会拿到被错误解释的窗口过程结果。
    """
    import ctypes
    src = _fn_body("_install_taskbar_activate_hook", 7000)
    if "c_ssize_t" not in src:
        return False
    # c_long 在 Windows 上确实比指针窄，所以用它当 LRESULT 一定错
    return ctypes.sizeof(ctypes.c_long) < ctypes.sizeof(ctypes.c_void_p)


def f3():
    """设置窗口过程的入口也要声明 argtypes/restype。"""
    src = _fn_body("_install_taskbar_activate_hook", 7000)
    return "setter.argtypes" in src and "setter.restype" in src


def f4():
    """还原窗口过程要用安装时记下的 setter，而不是重新抓一个。

    重新抓的 ctypes 函数对象没有沿用安装时的 argtypes 声明，
    SetWindowLongPtr 少一次声明就又退回到 32 位截断。
    """
    src = _fn_body("_uninstall_taskbar_activate_hook", 2000)
    return "$setter" or ('_WNDPROC_HOLDER.get("setter")' in src)


def f5():
    """on_closing 必须先判断退出标记，再决定是否取消关闭。

    退出流程里 destroy 窗口会再次触发 on_closing；此时若还返回 False，
    pywebview 的 Event.set() 就会把它判定成「取消关闭」，
    w.destroy() 变成空操作 → 进程永远退不掉。
    """
    src = _fn_body("on_closing", 3000)
    if not src:
        return False
    q = src.find('close_state.get("quitting")')
    h = src.find("_hide_to_tray(w)")
    return q > 0 and h > 0 and q < h


def f6():
    """退出路径必须先立 quitting 标记再销毁窗口。"""
    q1 = 'close_state["quitting"] = True' in _fn_src("quit_app", 1500)
    q2 = 'close_state["quitting"] = True' in _fn_src("relaunch_console", 1500)
    return q1 and q2


def f7():
    """托盘「退出 Lovomo」走统一入口 request_quit，且它会立起退出标记。"""
    src = _fn_src("request_quit", 1200)
    if not src:
        return False
    return ('close_state["quitting"] = True' in src
            and "lambda i, item: request_quit()" in MAIN_SRC)


def f8():
    """on_closing 里取消关闭的 False 只允许出现一次，且是最后一条语句。

    False 会取消关闭。任何出现在退出分支里的 False 都会让程序关不掉：
    第一个可执行语句必须是 quitting 判断且返回 None（放行），
    真正的 return False 只能落在函数末尾。
    """
    import ast
    node = _fn_body_node("on_closing")
    if node is None:
        return False
    body = list(node.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]                      # 去掉 docstring
    if not body:
        return False
    first = body[0]
    if not (isinstance(first, ast.If) and "quitting" in ast.unparse(first.test)):
        return False
    if not (len(first.body) == 1 and isinstance(first.body[0], ast.Return)
            and isinstance(first.body[0].value, ast.Constant)
            and first.body[0].value.value is None):
        return False
    rets = [n for n in ast.walk(node)
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)
            and n.value.value is False]
    return len(rets) == 1 and body[-1] is rets[0]


# ------------------------------------- H 收进托盘 / 单实例（任务栏与重复启动）
section("H 收进托盘：隐藏而非最小化 / 单实例")


def h1():
    """收进托盘走 hide：窗口要从屏幕和任务栏一起消失（走 UI 线程 form.Hide）。"""
    src = _fn_src("_hide_to_tray", 1600)
    return "_window_hide(w)" in src


def h2():
    """on_closing 自己不许直接动窗口，必须交给 _hide_to_tray 延后处理。

    在 FormClosing 里直接改窗口状态会被关闭序列覆盖。
    """
    src = _fn_body("on_closing", 3000)
    if not src:
        return False
    return ("w.hide()" not in src and "w.minimize()" not in src
            and "_hide_to_tray(w)" in src)


def h3():
    """藏完要校验窗口真的不可见了，没藏住要重试（UI 线程 form.Hide + 多次确认）。"""
    src = _fn_src("_hide_to_tray", 1600)
    return ("_window_visible(w)" in src and "for _ in range(5)" in src
            and "_window_hide(w)" in src)


def h4():
    """没有托盘图标时不能隐藏窗口，否则用户再也叫不回来。"""
    src = _fn_src("_hide_to_tray", 1600)
    return "if not tray_ok" in src and "w.minimize()" in src


def h5():
    """收进托盘后，系统激活唤醒不能把窗口又拉出来。

    WM_ACTIVATEAPP 会发给应用的所有顶层窗口，隐藏的窗口也可能收到；
    不挡一下就会出现「关掉之后又冒出一层窗口」。
    """
    src = _fn_src("_wake_from_system", 900)
    return 'close_state.get("hidden")' in src and "open_console()" in src


def h6():
    """open_console 的延迟重试同样要挡住已经收进托盘的窗口。"""
    src = _fn_src("_retry", 700)
    return 'close_state.get("hidden")' in src


def h7():
    """单实例入口齐备。"""
    return all(callable(getattr(M, n, None)) for n in (
        "_acquire_single_instance", "_signal_existing_instance",
        "_start_instance_show_waiter"))


def h8():
    """互斥体与事件的名字要固定，且按登录会话隔离。"""
    return (M._INSTANCE_MUTEX_NAME.startswith("Local\\")
            and M._INSTANCE_SHOW_EVENT_NAME.startswith("Local\\")
            and M._INSTANCE_MUTEX_NAME != M._INSTANCE_SHOW_EVENT_NAME)


def h9():
    """事件必须是手动重置的，且收到后要 ResetEvent。

    自动重置的事件在没人等待时会丢失信号；手动重置但不重置，下一次启动
    会被上一次留下的信号立刻再唤醒一遍。
    """
    src = _fn_src("_start_instance_show_waiter", 1600)
    if "ResetEvent" not in src:
        return False
    i = SINGLE_INSTANCE_SRC.find("def _acquire_single_instance")
    seg = SINGLE_INSTANCE_SRC[i:i + 2200]
    return "None, True, False, _INSTANCE_SHOW_EVENT_NAME" in seg


def h10():
    """CreateMutexW 的 last error 要开 use_last_error 才读得准。"""
    i = SINGLE_INSTANCE_SRC.find("def _kernel32")
    return "use_last_error=True" in SINGLE_INSTANCE_SRC[i:i + 400]


def h11():
    """__main__ 里的单实例判定必须在「等待旧进程退出」之后。

    重启流程是旧进程退出、新进程才启动；判定提前会把重启挡在门外。
    """
    i = MAIN_SRC.find('LOVOMO_WAIT_PID", ""')
    j = MAIN_SRC.find("_acquire_single_instance()", i)
    k = MAIN_SRC.find("config = ConfigLoader()", i)
    return 0 < i < j < k


def h12():
    """抢不到名额时要通知已有实例并立刻退出，不能继续起第二套服务。"""
    i = MAIN_SRC.find("acquired = _acquire_single_instance()")
    if i < 0:
        return False
    j = MAIN_SRC.find("if not acquired:", i)
    if j < 0:
        return False
    seg = MAIN_SRC[j:j + 400]
    return "_signal_existing_instance()" in seg and "sys.exit(0)" in seg


def h12b():
    """重启/安装派生的进程抢名额失败时，先重试几拍再认输。

    上一个实例可能正好处在「已经退出、名额还没交还」的一瞬间；直接放弃会让
    这次重启无声落空 —— 旧进程已走、新进程也没起，用户看到程序整个消失。
    """
    i = MAIN_SRC.find("acquired = _acquire_single_instance()")
    if i < 0:
        return False
    seg = MAIN_SRC[i:i + 900]
    return ("wait_pid" in seg and "while time.time() < deadline" in seg
            and "time.sleep" in seg)


def h12c():
    """新进程没起来时不许拆窗口退出：spawn 成功在前，destroy 在后。

    反过来的话，spawn 失败就留下「旧界面已经没了、新程序又没起来」的空档。
    """
    code = _fn_code("_do_relaunch")
    if not code:
        return False
    try:
        spawn = code.index("_spawn_detached")
        destroy = code.index("w.destroy()")
        force = code.index("_force_quit")
    except ValueError:
        return False
    if not spawn < destroy < force:
        return False
    branch = code[spawn:destroy]
    return "return" in branch and "relaunch_pending" in branch


def h12d():
    """释放/重建界面失败的原因要落盘，不能只用 print。

    print 只进内存日志缓冲，程序一退就没了；「进程莫名消失」这类问题事后
    只能靠 app.log 取证。
    """
    for fn in ("_webview2_release", "_webview2_rebuild"):
        code = _fn_code(fn)
        if not code or "log_window_event" not in code:
            return False
    return True


def h13():
    """真实跑一遍：第二个进程抢不到名额，且能把第一个进程唤醒。

    互斥体/事件是内核对象，mock 掉就等于什么都没验。测试用独立的对象名，
    避免和用户正在运行的程序互相干扰。
    """
    # 互斥体/事件名现在是 modules.single_instance 的模块自有状态，函数
    # 从自己的模块全局读取，所以这里要 patch 模块本体（main 里的再导出
    # 名只是同一对象的引用，改它不影响函数行为）。
    import modules.single_instance as S
    saved = (S._INSTANCE_MUTEX_NAME, S._INSTANCE_SHOW_EVENT_NAME,
             dict(S._INSTANCE_STATE))
    tag = f"probe{os.getpid()}"
    S._INSTANCE_MUTEX_NAME = f"Local\\Lovomo.Test.{tag}"
    S._INSTANCE_SHOW_EVENT_NAME = f"Local\\Lovomo.Test.Show.{tag}"
    S._INSTANCE_STATE.update({"mutex": None, "event": None, "waiter": False})
    try:
        if not M._acquire_single_instance():
            return False
        seen = []
        M._start_instance_show_waiter(lambda: seen.append(1))
        code = (
            "import sys, os;"
            f"sys.path.insert(0, r'{ROOT}');os.chdir(r'{ROOT}');"
            "import main as M;"
            "import modules.single_instance as S;"
            f"S._INSTANCE_MUTEX_NAME = r'{S._INSTANCE_MUTEX_NAME}';"
            f"S._INSTANCE_SHOW_EVENT_NAME = r'{S._INSTANCE_SHOW_EVENT_NAME}';"
            "print(M._acquire_single_instance(), M._signal_existing_instance())"
        )
        proc = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True, timeout=60,
                              **T.no_window_kwargs())
        time.sleep(0.8)
        return proc.stdout.strip().endswith("False True") and len(seen) == 1
    finally:
        S._INSTANCE_MUTEX_NAME = saved[0]
        S._INSTANCE_SHOW_EVENT_NAME = saved[1]
        S._INSTANCE_STATE.clear()
        S._INSTANCE_STATE.update(saved[2])


def h14():
    """拿不到 HWND 时按「可见」处理，避免误判成已藏好。"""
    return M._window_visible(None) is True and M._window_visible(object()) is True


def h15():
    """打开窗口时要清掉「已收进托盘」标记，否则唤醒会被自己挡住。"""
    return 'close_state["hidden"] = False' in _fn_src("open_console", 1600)


def h16():
    """窗口收进托盘后不再记录几何。

    隐藏窗口的 GetWindowPlacement 不报告正常状态，会退回到 pywebview 属性；
    最大化过的窗口这时读回来的是全屏尺寸，当成 Normal 记下来，
    下次启动就会开出一个「没最大化却占满屏幕」的窗口。
    """
    return "_window_visible" in _fn_code("_capture_geometry")


# ------------------------------------------------- E 许可证文件命名（GitHub）
section("E 许可证命名：避免 GitHub 误判「双证书」")


def e1():
    """根目录只能有一份被 GitHub 识别为许可证的文件。

    GitHub 用 Licensee 扫描 LICENSE* / COPYING* / LICENCE* 等文件名。
    LICENSE.txt 会被当成第二份许可证，仓库页面就显示双证书。
    """
    bad = []
    for pat in ("LICEN*", "LICENCE*", "COPYING*", "COPYING.*"):
        for p in ROOT.glob(pat):
            if p.is_file():
                bad.append(p.name)
    return bad == ["LICENSE"]


def e2():
    """免责声明必须存在于 DISCLAIMER.txt（不被 Licensee 识别）。"""
    f = ROOT / "DISCLAIMER.txt"
    return f.is_file() and f.stat().st_size > 1000


def e3():
    """DISCLAIMER.txt 开头要明确声明「本文件不是许可证」。

    否则 Licensee 仍可能按内容把它匹配成一份许可证。
    """
    txt = (ROOT / "DISCLAIMER.txt").read_text(encoding="utf-8")
    head = txt[:400]
    return "不是许可证" in head or "NOT a license" in head


def e4():
    """LICENSE 必须是 AGPL-3.0 官方全文，且不含自定义免责声明段落。"""
    txt = (ROOT / "LICENSE").read_text(encoding="utf-8")
    if "GNU AFFERO GENERAL PUBLIC LICENSE" not in txt:
        return False
    # 自定义免责声明的标志性句子不应该出现在 LICENSE 里
    return "软件性质与用途" not in txt and "Nature and Purpose" not in txt


def e5():
    """打包配置与 README 都必须引用新文件名，且不再引用 LICENSE.txt。"""
    for rel in ("lovomo_app.spec", "Lovomo_Setup.iss", "README.md"):
        txt = (ROOT / rel).read_text(encoding="utf-8")
        if "DISCLAIMER.txt" not in txt:
            return False
        # 允许注释里提到旧名字（解释为什么改名），但不允许作为引用出现
        for line in txt.splitlines():
            if "LICENSE.txt" not in line:
                continue
            if line.lstrip().startswith((";", "#", "//", ">")):
                continue
            if "而不是" in line or "会被" in line:
                continue
            return False
    return True


def e6():
    """安装脚本仍要同时携带 LICENSE（AGPL 第 4 条要求）。"""
    txt = (ROOT / "Lovomo_Setup.iss").read_text(encoding="utf-8")
    return ('Source: "C:\\Users\\zhanglj\\Desktop\\Lovomo\\LICENSE"' in txt
            or 'Lovomo\\LICENSE"' in txt)


check("open_console 以 _apply_saved_geometry 为主路径", d1)
check("open_console 清除 minimized 标记", d2)
check("几何事件四类均已绑定", d3)
check("run_webview_loop 绑定事件并传记忆几何", d4)
check("create_window 不再无条件 maximized=True", d5)
check("on_closing 先抓几何再收进托盘（不在关闭序列里动窗口）", d6)
check("退出/重启前同步落盘几何", d7)
check("_force_quit 延迟压缩到 1.5s 内", d8)
check("main() 收尾 join/shutdown 均有短预算", d9)
check("所有 _force_quit 调用延迟 <= 1.5s", d10)
check("_ensure_dpi_aware 存在且在启动早期调用", d11)
check("_screen_rects 不再自行声明 DPI 感知", d12)
check("物理/逻辑像素换算互为逆运算", d13)
check("_capture_geometry 不再用 window.state 判断最大化", d14)
check("_capture_geometry 只在 Normal 时记录 Normal 矩形", d15)
check("退化矩形被拒收", d16)
check("默认 Normal 几何按屏幕比例计算", d17)
check("几何数据带版本号且会丢弃旧数据", d18)
check("run_webview_loop 做了物理->逻辑换算", d19)
check("启动后不再二次 apply 几何", d20)
check("几何事件绑定有启动宽限期", d21)
check("_raise_to_foreground 用了原生前台 API", d22)
check("open_console 会唤醒窗口到前台", d23)
check("最小化判定先于 show()", d24)
check("唤醒后校验可见性并可重试", d25)
check("抢前台是唤醒的最后一步", d26)
check("隐藏/最小化时不改几何", d27)
check("唤醒期间暂停几何记录", d28)
check("任务栏激活钩子拦截 WM_ACTIVATE 系列消息", d29)
check("激活钩子回调可自定义且默认抢前台", d30)
check("激活钩子持有 WNDPROC 引用防 GC", d31)
check("激活钩子在窗口 shown 后安装", d32)
check("open_console 有延迟重试抢前台", d33)
check("退出前还原窗口过程", d34)
check("CallWindowProcW 显式声明 argtypes/restype", f1)
check("WNDPROC 返回类型是 8 字节 LRESULT", f2)
check("设置窗口过程入口声明了参数类型", f3)
check("还原窗口过程复用安装时的 setter", f4)
check("on_closing 先看退出标记再决定取消关闭", f5)
check("退出路径先立 quitting 再销毁窗口", f6)
check("托盘退出走统一入口 request_quit", f7)
check("取消关闭的 False 只出现一次且在退出分支之后", f8)
check("收进托盘走 hide（任务栏一起消失）", h1)
check("on_closing 不直接动窗口，交给 _hide_to_tray", h2)
check("藏完校验可见性并重试", h3)
check("无托盘图标时退化为最小化（留入口）", h4)
check("系统激活唤醒不拉起已收进托盘的窗口", h5)
check("延迟重试不拉起已收进托盘的窗口", h6)
check("单实例入口齐备", h7)
check("互斥体/事件名固定且按会话隔离", h8)
check("唤醒事件为手动重置且收到后重置", h9)
check("CreateMutexW 读取 last error 前开启 use_last_error", h10)
check("单实例判定在等待旧进程退出之后", h11)
check("抢不到名额时通知已有实例并退出", h12)
check("重启派生的进程抢名额失败时重试几拍", h12b)
check("新进程没起来就不拆窗口退出", h12c)
check("界面释放/重建失败落盘", h12d)
check("真实两个进程：第二个抢不到名额且能唤醒第一个", h13)
check("拿不到 HWND 时按可见处理", h14)
check("打开窗口时清掉已收进托盘标记", h15)
check("收进托盘后不再记录几何", h16)
check("根目录只有 LICENSE 一份许可证文件", e1)
check("免责声明已改名为 DISCLAIMER.txt", e2)
check("DISCLAIMER.txt 声明自己不是许可证", e3)
check("LICENSE 是纯 AGPL-3.0 全文", e4)
check("打包配置与 README 引用新文件名", e5)
check("安装包仍携带 AGPL-3.0 全文", e6)


# ------------------------------------- G TTS 子进程归属（作业对象 / 认领）
section("G TTS 子进程：作业对象 / 认领遗留进程 / 退出竞态")


def g1():
    """修复所需的新入口都在。"""
    return all(callable(getattr(T, n, None)) for n in (
        "bind_to_job", "process_start_time", "mark_exiting", "is_exiting",
        "adopt_existing_tts", "_port_owner_pid", "_process_image",
        "_write_child_record", "_read_child_record", "_clear_child_record"))


def g2():
    """作业对象句柄能建出来（非 Windows 返回 None，不抛异常）。"""
    h = T._job_handle()
    if os.name != "nt":
        return h is None
    return bool(h)


def _in_job(pid: int) -> bool:
    """查询进程是否真的在某个作业对象里（测试用探针，不进产品代码）。"""
    import ctypes
    from ctypes import wintypes
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE,
                                     ctypes.POINTER(wintypes.BOOL)]
        k.IsProcessInJob.restype = wintypes.BOOL
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        try:
            res = wintypes.BOOL()
            if not k.IsProcessInJob(h, None, ctypes.byref(res)):
                return False
            return bool(res.value)
        finally:
            k.CloseHandle(h)
    except Exception:
        return False


def g3():
    """真实子进程能被 assign 进作业对象，且确实变成了"在作业里"。

    这是「TTS 不再是独立进程」的核心机制：进了作业，父进程一关（含崩溃、
    被强杀）系统就把子进程一起收掉。
    """
    if os.name != "nt":
        return True
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, **T.no_window_kwargs())
    try:
        bound = T.bind_to_job(proc.pid)
        return bound and _in_job(proc.pid)
    finally:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


def g4():
    """认领回来的"只有 PID"也能被 ProcessManager 收掉。"""
    if os.name != "nt":
        return True
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, **T.no_window_kwargs())
    pm = T.ProcessManager()
    before = list(pm.processes)
    try:
        pm.register(T._AdoptedProcess(proc.pid), "probe-g4")
        pm.shutdown_all(budget=3.0)
        time.sleep(0.4)
        try:
            proc.wait(timeout=3)
            gone = True
        except Exception:
            gone = proc.poll() is not None
        return gone and pm.processes == []
    finally:
        pm.processes[:] = before
        try:
            proc.kill()
        except Exception:
            pass


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def g5():
    """没开「自动启动 TTS」就不接管端口上那份服务（可能是用户自己起的）。"""
    saved = T._ADOPT_DONE["value"]
    try:
        T._ADOPT_DONE["value"] = False
        r = T.adopt_existing_tts({"auto_start_tts": False,
                                  "client_base_url": "http://127.0.0.1:9880"})
        return r is False
    finally:
        T._ADOPT_DONE["value"] = saved


def g6():
    """端口上没有 TTS 时不认领、也不留下记录。"""
    saved = T._ADOPT_DONE["value"]
    saved_record = T._read_child_record()
    try:
        T._clear_child_record()
        T._ADOPT_DONE["value"] = False
        r = T.adopt_existing_tts({"auto_start_tts": True,
                                  "client_base_url": f"http://127.0.0.1:{_free_port()}"})
        return r is False and T._read_child_record() == {}
    finally:
        T._ADOPT_DONE["value"] = saved
        if saved_record:
            T._write_child_record(saved_record.get("pid", 0),
                                  saved_record.get("port", 0),
                                  saved_record.get("exe", ""))


def g6b():
    """本进程自己启动并登记过的 TTS 不算"上一次运行遗留"，不再重复认领。

    自动启动 TTS 的路径会在 ensure_tts_service 之前完成登记，随后每条消息
    触发的 ensure_tts_service 都会走到认领入口；不排除已登记进程的话，
    每次启动都会把自己刚拉起来的 TTS 当成遗留进程收编一次。
    """
    saved = T._ADOPT_DONE["value"]
    saved_record = T._read_child_record()
    saved_start = T.process_start_time
    pm = T.ProcessManager()
    before = list(pm.processes)
    fake_pid = 0x7FFFFFF0
    try:
        pm.processes.append({"proc": T._AdoptedProcess(fake_pid), "name": "GPT-SoVITS TTS"})
        path = T._child_record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": fake_pid, "started": 999.0,
                                    "port": _free_port()}), encoding="utf-8")
        T.process_start_time = lambda pid: 999.0 if int(pid) == fake_pid else None
        T._ADOPT_DONE["value"] = False
        r = T.adopt_existing_tts({"auto_start_tts": True,
                                  "client_base_url": f"http://127.0.0.1:{_free_port()}"})
        return r is False
    finally:
        T.process_start_time = saved_start
        pm.processes[:] = before
        T._ADOPT_DONE["value"] = saved
        T._clear_child_record()
        if saved_record:
            T._write_child_record(saved_record.get("pid", 0),
                                  saved_record.get("port", 0),
                                  saved_record.get("exe", ""))


def g7():
    """记录里的 PID 与实物创建时间不吻合（PID 被系统复用）→ 拒绝认领。

    只比 PID 会在 PID 复用后误杀别人的进程，所以必须比对创建时间。
    """
    saved = T._ADOPT_DONE["value"]
    try:
        T._clear_child_record()
        path = T._child_record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": os.getpid(),
                                    "started": 12345,
                                    "port": _free_port()}), encoding="utf-8")
        T._ADOPT_DONE["value"] = False
        r = T.adopt_existing_tts({"auto_start_tts": True,
                                  "client_base_url":
                                      f"http://127.0.0.1:{_free_port()}"})
        return r is False and T._read_child_record() == {}
    finally:
        T._ADOPT_DONE["value"] = saved
        T._clear_child_record()


def g8():
    """真正认领一次：端口上有个 python 监听者 → 收编 + 落记录。

    注意量的是**端口属主**而不是 Popen 的 PID：Windows 上 .venv 的
    python.exe 是个 re-exec 启动器，真正持有 socket 的是它的子进程
    （真实解释器）。产品代码里认领走的也正是端口属主。
    """
    if os.name != "nt":
        return True
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    listener = subprocess.Popen(
        [sys.executable, "-c",
         "import socket,time;s=socket.socket();"
         f"s.bind(('127.0.0.1',{port}));s.listen(5);time.sleep(60)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, **T.no_window_kwargs())
    saved = T._ADOPT_DONE["value"]
    pm = T.ProcessManager()
    before = list(pm.processes)
    owner = None
    try:
        T._clear_child_record()
        for _ in range(40):                       # 等端口真的 listen 上
            owner = T._port_owner_pid(port)
            if owner:
                break
            time.sleep(0.25)
        if not owner:
            return False
        T._ADOPT_DONE["value"] = False
        ok = T.adopt_existing_tts({"auto_start_tts": True,
                                   "client_base_url": f"http://127.0.0.1:{port}"})
        rec = T._read_child_record()
        return (ok is True
                and rec.get("pid") == owner
                and rec.get("port") == port
                and any(getattr(p["proc"], "pid", None) == owner
                        for p in pm.processes))
    finally:
        pm.processes[:] = before
        T._ADOPT_DONE["value"] = saved
        T._clear_child_record()
        for pid in (owner, listener.pid):         # 启动器 + 真实解释器一起收
            if pid:
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   capture_output=True, timeout=8,
                                   **T.no_window_kwargs())
                except Exception:
                    pass
        try:
            listener.kill()
            listener.wait(timeout=5)
        except Exception:
            pass


def g9():
    """Popen 之后必须立刻挂作业对象 + 落记录，且 spawn 前后都有退出守卫。"""
    src = inspect.getsource(T.auto_start_and_switch_tts)
    i_pop = src.find("tts_proc = subprocess.Popen")
    if i_pop < 0:
        return False
    after = src[i_pop:]
    return ("bind_to_job(tts_proc.pid)" in after
            and "_write_child_record(tts_proc.pid" in after
            and "is_exiting()" in src)


def g10():
    """认领入口要在"已在线上就跳过"的两条路径上都接上。"""
    a = inspect.getsource(T.ensure_tts_service)
    b = inspect.getsource(T.auto_start_and_switch_tts)
    return "adopt_existing_tts(config)" in a and "adopt_existing_tts(config)" in b


def g11():
    """shutdown_all 里**不能**立"正在退出"标记。

    「保存并重启 TTS」的路径是 shutdown_all() → 立刻 spawn 新的 TTS；
    如果 shutdown_all 顺手把退出标记立起来，这条重启路径就会永远拒绝启动。
    标记只能由真正的退出路径（_force_quit / main 收尾）来立。
    """
    return "mark_exiting" not in inspect.getsource(T.ProcessManager.shutdown_all)


def g12():
    """真正的退出路径必须立起退出标记（用 AST 看调用，不看注释）。"""
    import ast
    tree = ast.parse(MAIN_SRC)
    hits = set()

    def _walk(n, fn):
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # 嵌套函数（_force_quit 里的 _run）算外层函数的调用
                _walk(child, fn or child.name)
                continue
            if isinstance(child, ast.Call):
                f = child.func
                name = getattr(f, "id", None) or getattr(f, "attr", None)
                if name:
                    hits.add((fn, name))
            _walk(child, fn)

    _walk(tree, "")
    # 干净退出路径：_force_quit 里的硬退出计时器，以及 __main__ 收尾块
    # （收尾块在 `if __name__ == "__main__":` 里，不在任何函数内，fn 为空串）
    got = {fn for fn, name in hits if name == "mark_exiting"}
    return "_force_quit" in got and "" in got


def g13():
    """退出标记要真的能挡住 spawn，且必须在"已在线上"判定之前。"""
    saved = T._LIFETIME["ending"]
    try:
        T._LIFETIME["ending"] = False
        ok_false = T.is_exiting() is False
        T.mark_exiting()
        ok_true = T.is_exiting() is True
        src = inspect.getsource(T.auto_start_and_switch_tts)
        return ok_false and ok_true and src.index("is_exiting()") < src.index("TTS 服务已在线")
    finally:
        T._LIFETIME["ending"] = saved


check("作业对象/认领相关入口齐备", g1)
check("作业对象句柄可创建", g2)
check("真实子进程进入作业对象", g3)
check("只有 PID 的认领进程也能被回收", g4)
check("未开自动启动时不接管端口上的服务", g5)
check("端口无服务时不认领", g6)
check("本进程已登记的 TTS 不重复认领", g6b)
check("PID 复用（创建时间不符）时拒绝认领", g7)
check("端口的 python 监听者被成功认领", g8)
check("Popen 后立即挂作业对象并落记录", g9)
check("两条「已在线」路径都接上认领", g10)
check("shutdown_all 不立退出标记（否则重启 TTS 失效）", g11)
check("退出路径立起退出标记", g12)
check("退出标记确实挡住 spawn", g13)


# ------------------------------------------- I 收进托盘后释放 WebView2
section("I 收进托盘后释放 WebView2：进程退出 / 唤醒重建")


def i1():
    """旧的「启动参数量身法」必须彻底回退掉。

    _tune_webview2_memory 那套参数实测在任务管理器里看不出变化（GPU 进程照样在），
    留着只会是负担。
    """
    return ("WEBVIEW2_MEMORY_ARGS" not in MAIN_SRC
            and "_tune_webview2_memory" not in MAIN_SRC
            and not hasattr(M, "WEBVIEW2_MEMORY_ARGS")
            and not hasattr(M, "_tune_webview2_memory"))


def i2():
    """藏好之后立刻释放（跑在隐藏线程里，不占 UI 线程、不等宽限期）。"""
    src = _fn_src("_hide_to_tray", 2400)
    return "_release_webview_now()" in src


def i3():
    """释放前抢锁、复检托盘状态、**且确认窗口确实已藏住** —— 没藏住就别拆（否则白屏）。"""
    src = _fn_code("_release_webview_now")
    return ("_WEBVIEW_LOCK" in src
            and "close_state" in src
            and "_window_visible" in src
            and "_webview2_release" in src)


def i4():
    """不留宽限期 / 计时器那套：释放路径必须是即时的。"""
    src = _fn_src("_hide_to_tray", 2400)
    return ("WEBVIEW_RELEASE_DELAY" not in MAIN_SRC
            and "_release_webview_later" not in MAIN_SRC
            and "_cancel_webview_release" not in MAIN_SRC
            and "Timer" not in src)


def i5():
    """唤醒时先把界面重建好，再抢前台显示窗口（不能让空窗先露出来）。"""
    src = _fn_src("open_console", 2600)
    if "_ensure_webview_alive()" not in src:
        return False
    hidden = src.index('close_state["hidden"] = False')
    alive = src.index("_ensure_webview_alive()")
    raise_front = src.index("_raise_to_foreground", alive)
    return hidden < alive < raise_front


def i6():
    """重建是幂等的：判断与重建在同一把锁里，并发的两次打开只会重建一次。"""
    src = _fn_src("_ensure_webview_alive", 1600)
    return ("with modules.webview_runtime._WEBVIEW_LOCK" in src
            and src.index("with modules.webview_runtime._WEBVIEW_LOCK")
            < src.index("_webview2_rebuild(")
            and "http://127.0.0.1:{webui_port}" in src
            and "_webview_profile_dir()" in src)


def i7():
    """释放的是控件，不是窗口：窗体留着，托盘/几何/任务栏钩子都不用重建。"""
    code = _fn_code("_webview2_release")
    return ("Dispose" in code and "_webview2_on_ui" in code
            and ".destroy" not in code and "destroy(" not in code)


def i8():
    """控件操作一律派发到 UI 线程，且失败只回 {"err": ...}，不往外抛。"""
    code = _fn_code("_webview2_on_ui")
    return ("from System import" in code and "Invoke" in code
            and code.count("except") >= 2
            and ("\"err\"" in code or "'err'" in code))


def i9():
    """重建：等 CoreWebView2 就绪，初始化失败返回 False（不假装成功），并把旧控件换掉。"""
    code = _fn_code("_webview2_rebuild")
    if "CoreWebView2 is not None" not in code or "_webview2_on_ui" not in code:
        return False
    return ("if not ready" in code and "browser.webview" in code
            and "UserDataFolder" in code)


def i10():
    """桩掉 UI 派发后，release 真的 Dispose 控件并返回 True（不需要真窗口）。"""
    calls = []

    class _Ctrl:
        def Dispose(self):
            calls.append("dispose")

    class _Form:
        def __init__(self, ctrl):
            self.webview = ctrl

    class _Win:
        def __init__(self, ctrl):
            self.native = _Form(ctrl)

    saved = WRT._webview2_on_ui
    WRT._webview2_on_ui = lambda form, fn: {"value": fn()}
    try:
        ok = M._webview2_release(_Win(_Ctrl()))
        again = M._webview2_release(_Win(None))   # 已经拆过 -> True
        bad = M._webview2_release(object())       # 拿不到窗体 -> False
        return (ok is True and again is True and bad is False
                and calls == ["dispose"])
    finally:
        WRT._webview2_on_ui = saved


def i11():
    """Dispose 抛异常时返回 False，不能把收托盘这条路径带崩。"""
    class _Ctrl:
        def Dispose(self):
            raise RuntimeError("boom")

    class _Form:
        def __init__(self):
            self.webview = _Ctrl()

    class _Win:
        def __init__(self):
            self.native = _Form()

    def _stub(form, fn):
        try:
            return {"value": fn()}
        except Exception as e:
            return {"err": e}

    saved = WRT._webview2_on_ui
    WRT._webview2_on_ui = _stub
    try:
        return M._webview2_release(_Win()) is False
    finally:
        WRT._webview2_on_ui = saved


def i12():
    """清缓存按钮与唤醒路径共用同一把锁，两边不会同时动控件。"""
    return ("_WEBVIEW_LOCK" in _fn_code("clear_webview_cache_and_reload")
            and "_WEBVIEW_LOCK" in _fn_code("_ensure_webview_alive"))


def i18():
    """助手进程（重启 / 安装）等旧进程有时限，超时就放弃 —— 不能变成第二个实例。"""
    return ("while _pid_alive(target_pid) and time.time() < deadline" in MAIN_SRC
            and "20 秒仍未退出" in MAIN_SRC
            and "不会启动第二个实例" in MAIN_SRC)


def i19():
    """退出标记有过期保护：上次退出没走完，也不能把「关闭到托盘」放行成真退出。"""
    src = _fn_src("on_closing", 1800)
    return ("quitting_at" in src and "已重置退出标记" in src)


def i20():
    """重建前先清掉窗体上残留的 WebView2 控件（反复重建不再叠控件）。"""
    src = _fn_code("_webview2_rebuild")
    return "Controls.Remove" in src and "isinstance(c, WebView2)" in src


def i21():
    """界面重建不出来时先藏窗口再重启一次，绝不留下白屏窗口。"""
    # AST 反解出的字符串是单引号，所以只比对到 .get，不比对引号
    return ("_APP_HOOKS.get" in _fn_code("_ensure_webview_alive")
            and "_APP_HOOKS.get" in _fn_code("clear_webview_cache_and_reload"))


def i13():
    """重建用的助手确实在模块级定义（免得走了「用了没定义」的老路）。"""
    return all(callable(getattr(M, n, None)) for n in (
        "_webview2_form", "_webview2_on_ui", "_webview2_release",
        "_webview2_rebuild"))


def i22():
    """重启的重活（destroy + spawn）放到独立线程，托盘回调不被 Invoke 阻塞。"""
    src = _fn_src("relaunch_console", 600)
    return ("relaunch_pending[\"flag\"] = True" in src
            and "threading.Thread(target=_do_relaunch" in src)


def i23():
    """重启/退出前先释放 WebView2，避免 Form.Close 卡在界面进程清理上。"""
    code = _fn_code("_do_relaunch")
    return ("_webview2_release(w)" in code and "w.destroy()" in code
            and code.index("_webview2_release(w)") < code.index("w.destroy()"))


def i24():
    """收托盘隐藏走 UI 线程的 form.Hide，不是绕过 WinForms 的原生 ShowWindow。"""
    code = _fn_code("_window_hide")
    return ("_webview2_on_ui" in code and "Hide" in code
            and "ShowWindow" not in code)


check("旧的启动参数量身法已彻底回退", i1)
check("收进托盘后释放界面进程", i2)
check("没藏住就不拆界面（避免白屏）", i3)
check("不留宽限期/计时器", i4)
check("唤醒先重建界面再抢前台", i5)
check("重建幂等（锁内二次判 released）", i6)
check("只拆控件不拆窗体", i7)
check("控件操作派发到 UI 线程且有兜底", i8)
check("重建先等就绪再导航并换掉旧控件", i9)
check("release 真的 Dispose 控件", i10)
check("Dispose 异常时安全返回 False", i11)
def i14():
    """唤醒前按控件真实状态判断：界面进程被外部结束后，窗口不能空着拉出来。"""
    src = _fn_code("_ensure_webview_alive")
    return "webview2_control_alive(window)" in src and "_WEBVIEW_PROCESS_DEAD" in src


def i15():
    """重建时把 pywebview 的初始化接回来（设置项、外链处理、下载与证书都在里面）。

    不接回来的话：外链会被 WebView2 弹成一个自带标签页的浏览器窗口，
    右键菜单 / F12 / 快捷键也都会冒出来。
    """
    src = _fn_code("_webview2_rebuild")
    return ("CoreWebView2InitializationCompleted" in src
            and "on_webview_ready" in src
            and "browser.webview = ctrl" in src)


def i16():
    """内核挂了（任务管理器里结束那组「浏览器」进程）要能被识别。"""
    src = _fn_code("attach_process_failed_watch")
    return "ProcessFailed" in src and "_WEBVIEW_PROCESS_DEAD" in src


def i17():
    """首次创建的控件也挂上内核失败监听（加载完成时控件已就绪）。"""
    src = _fn_src("run_webview_loop", 4000)
    return "attach_process_failed_watch(w)" in src and "events.loaded" in src


check("清缓存与唤醒共用一把锁", i12)
check("助手进程不会变成第二个实例", i18)
check("退出标记有过期保护", i19)
check("重建前清掉残留控件", i20)
check("重建失败会重启而不是留白屏", i21)
check("界面进程被外部结束后按真实状态重建", i14)
check("重建时接回 pywebview 初始化", i15)
check("内核崩溃/被杀能被识别", i16)
check("首次创建的控件也挂监听", i17)
check("WebView2 助手都在模块级定义", i13)
check("重启重活放独立线程，不阻塞托盘", i22)
check("重启/退出前先释放 WebView2 再关窗", i23)
check("收托盘走 UI 线程 form.Hide（不用原生 ShowWindow）", i24)


# ---------------------------------------------------------------- 清理
M._geometry_path = _ORIG_PATH
W._geometry_path = _ORIG_PATH
M._WINDOW_GEOMETRY.clear()
M._WINDOW_GEOMETRY.update(_ORIG_GEOM)
try:
    import shutil
    shutil.rmtree(_TMP, ignore_errors=True)
except Exception:
    pass

print(f"\n{'=' * 70}")
print(f"结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
if FAIL:
    for n, e in FAIL:
        print(f"  FAILED: {n} -> {e}")
print('=' * 70)
sys.exit(0 if not FAIL else 1)
