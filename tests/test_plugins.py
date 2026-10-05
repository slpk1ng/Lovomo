# -*- coding: utf-8 -*-
"""插件系统回归测试。

覆盖：
  A 插件管理器：安装 / 列表 / 启用禁用 / 卸载 / 覆盖安装
  B 安全扫描：高危拦截、需确认标记、风险分级
  C 包安全：路径穿越、非法 zip、超大包、不支持的文件类型
  D 插件运行时：加载、指令分发、消息钩子、异常隔离、热重载
  E API 端到端：list / upload / inspect / toggle / delete / theme / settings / reload
  F 皮肤聚合：启用才注入、禁用即消失
  G 静态检查：前端元素与路由齐备
  K 生效模型：设置暂存 + 「保存并重载」、皮肤变量被真实消费

运行: python tests/test_plugins.py      （全通过退出码 0）
"""
import asyncio
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
sys.stdin = io.StringIO()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import httpx  # noqa: E402
import main as M  # noqa: E402
from modules.plugins import (PluginManager, scan_python_source,  # noqa: E402
                             summarize_risks, PLUGIN_ID_RE, MAX_FILES)
from modules.plugin_runtime import PluginRuntime  # noqa: E402

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


def make_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()


_TMP = Path(tempfile.mkdtemp(prefix="lovomo_plugtest_"))
MAIN_SRC = (ROOT / "main.py").read_text(encoding="utf-8")
# WebUI 服务类（WebUIServer 整类 + WebUI 域常量）已拆到 modules/webui_server.py，静态扫描需同时覆盖
WEBUI_SRC = (ROOT / "modules" / "webui_server.py").read_text(encoding="utf-8")
HTML_SRC = (ROOT / "webui" / "start.html").read_text(encoding="utf-8")
# 插件指令分发/日志桥函数已拆到 modules/webui_common.py，静态扫描需同时覆盖
WEBCOMMON_SRC = (ROOT / "modules" / "webui_common.py").read_text(encoding="utf-8")
# StdoutRedirector（日志缓冲的主要写入方）已拆到 modules/log_console.py
LOG_CONSOLE_SRC = (ROOT / "modules" / "log_console.py").read_text(encoding="utf-8")
# 插件指令分发/on_message 分发的调用点已拆到 modules/message_pipeline.py
MESSAGE_PIPELINE_SRC = (ROOT / "modules" / "message_pipeline.py").read_text(encoding="utf-8")


def method_src(name):
    """整段抠出 WebUIServer（modules/webui_server.py）里的一个方法（到下一个同缩进的方法为止）。

    按字节窗口切片会随无关改动假失败，这里按缩进边界取。
    """
    i = WEBUI_SRC.find("    async def " + name + "(")
    if i < 0:
        i = WEBUI_SRC.find("    def " + name + "(")
    if i < 0:
        return ""
    ends = [WEBUI_SRC.find(m, i + 1) for m in ("\n    async def ", "\n    def ")]
    ends = [e for e in ends if e > 0]
    return WEBUI_SRC[i:min(ends) if ends else len(WEBUI_SRC)]


def fresh_pm(sub: str) -> PluginManager:
    base = _TMP / sub
    return PluginManager(base / "plugins", base / "plugins" / "state.json")


def install(pm, data, *, force=False, enable=True):
    """装一个插件。产品路径装完是停用的，测试要的是「装好并跑起来」，这里显式启用。"""
    return pm.install_zip(data, force=force, enable=enable)


GOOD_THEME = {
    "plugin.json": json.dumps({"id": "theme-a", "name": "主题A", "version": "1.0.0",
                               "type": "theme", "author": "t"}),
    "theme.css": ".sidebar{background:#f0f!important}",
}

GOOD_PY = {
    "plugin.json": json.dumps({"id": "func-a", "name": "功能A", "version": "1.0.0",
                               "type": "python"}),
    "main.py": ("def on_load(ctx):\n"
                "    ctx.register_command('ping', lambda c,n,a,e: 'pong')\n"
                "def on_message(ctx, event):\n"
                "    return 'hi' if 'hi' in (event or {}).get('text','') else None\n"),
}

EVIL_PY = {
    "plugin.json": json.dumps({"id": "evil-a", "name": "恶意A", "version": "1.0.0"}),
    "main.py": ("import winreg\n"
                "def on_load(ctx):\n"
                "    winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Software')\n"
                "    key = ctx.config.get('api_key')\n"),
}


# ------------------------------------------------------------ A 管理器
section("A 插件管理器：安装 / 列表 / 启停 / 卸载")


def a1():
    pm = fresh_pm("a1")
    r = install(pm, make_zip(GOOD_THEME))
    return r["success"] and r["id"] == "theme-a"


def a2():
    """装完是停用状态：不该一装上就跑起来，由用户自己去启用。"""
    pm = fresh_pm("a2")
    pm.install_zip(make_zip(GOOD_THEME))
    items = pm.list_plugins()
    p = items[0]
    return (len(items) == 1 and p["name"] == "主题A" and p["enabled"] is False
            and p["has_theme"] is True and p["has_python"] is False)


def a3():
    pm = fresh_pm("a3")
    install(pm, make_zip(GOOD_PY))
    p = pm.list_plugins()[0]
    return p["has_python"] is True and p["has_theme"] is False


def a4():
    """装完是停用的，用户可以自己启用。"""
    pm = fresh_pm("a4")
    pm.install_zip(make_zip(GOOD_THEME))
    before = pm.get("theme-a")["enabled"] is False
    pm.set_enabled("theme-a", True)
    return before and pm.get("theme-a")["enabled"] is True


def a5():
    """启用状态要落盘，换个管理器实例读还是那个状态。"""
    pm = fresh_pm("a5")
    pm.install_zip(make_zip(GOOD_THEME))
    pm.set_enabled("theme-a", True)
    pm2 = PluginManager(pm.root, pm.state_file)
    return pm2.get("theme-a")["enabled"] is True


def a6():
    pm = fresh_pm("a6")
    install(pm, make_zip(GOOD_THEME))
    ok = pm.uninstall("theme-a")
    return ok and pm.list_plugins() == [] and pm.get("theme-a") is None


def a7():
    """覆盖安装：版本号应更新，且只有一个目录；启用状态沿用原来的（升级不该改开关）。"""
    pm = fresh_pm("a7")
    install(pm, make_zip(GOOD_THEME))
    pm.set_enabled("theme-a", False)
    v2 = dict(GOOD_THEME)
    v2["plugin.json"] = json.dumps({"id": "theme-a", "name": "主题A新版",
                                    "version": "2.0.0", "type": "theme"})
    r = pm.install_zip(make_zip(v2))
    items = pm.list_plugins()
    return r["success"] and r["replaced"] is True and len(items) == 1 \
        and items[0]["version"] == "2.0.0" and items[0]["enabled"] is False


def a8():
    """卸载不存在的插件返回 False 且不抛异常。"""
    pm = fresh_pm("a8")
    return pm.uninstall("nope") is False and pm.get("nope") is None


def a9():
    """list_plugins 忽略 __pycache__ 与隐藏目录。"""
    pm = fresh_pm("a9")
    pm.ensure_root()
    (pm.root / "__pycache__").mkdir(exist_ok=True)
    (pm.root / ".hidden").mkdir(exist_ok=True)
    return pm.list_plugins() == []


def a10():
    """卸载默认连配置带数据一起删。"""
    pm = fresh_pm("a10")
    install(pm, make_zip(GOOD_THEME))
    data = pm.data_dir("theme-a")
    (data / "settings.json").write_text('{"accent": "#111111"}', encoding="utf-8")
    (data / "note.txt").write_text("x", encoding="utf-8")
    ok = pm.uninstall("theme-a")
    return ok and not (pm.root / "theme-a").exists()


def a11():
    """卸载时保留配置：只留下 data/settings.json。"""
    pm = fresh_pm("a11")
    install(pm, make_zip(GOOD_THEME))
    data = pm.data_dir("theme-a")
    (data / "settings.json").write_text('{"accent": "#111111"}', encoding="utf-8")
    (data / "note.txt").write_text("x", encoding="utf-8")
    ok = pm.uninstall("theme-a", keep_settings=True)
    kept = pm.root / "theme-a" / "data"
    return (ok and pm.get("theme-a") is None
            and (kept / "settings.json").is_file()
            and not (kept / "note.txt").exists()
            and not (pm.root / "theme-a" / "plugin.json").exists())


def a12():
    """卸载时保留数据：整个 data 目录都留着。"""
    pm = fresh_pm("a12")
    install(pm, make_zip(GOOD_THEME))
    data = pm.data_dir("theme-a")
    (data / "settings.json").write_text('{"accent": "#111111"}', encoding="utf-8")
    (data / "note.txt").write_text("x", encoding="utf-8")
    ok = pm.uninstall("theme-a", keep_data=True)
    kept = pm.root / "theme-a" / "data"
    return (ok and pm.get("theme-a") is None
            and (kept / "settings.json").is_file()
            and (kept / "note.txt").is_file()
            and not (pm.root / "theme-a" / "plugin.json").exists())


def a13():
    """重装保留过配置的插件，设置还能接着用。"""
    pm = fresh_pm("a13")
    install(pm, make_zip(GOOD_THEME))
    pm.save_plugin_settings("theme-a", {"accent": "#123456"})
    pm.uninstall("theme-a", keep_settings=True)
    install(pm, make_zip(GOOD_THEME))
    return pm.plugin_settings("theme-a") == {"accent": "#123456"}


def a14():
    """插件根目录有 webui.html 时才算有 webui。"""
    pm = fresh_pm("a14")
    install(pm, make_zip(GOOD_THEME))
    before = pm.get("theme-a")["webui_ok"]
    extra = dict(GOOD_THEME)
    extra["webui.html"] = "<html><body>hi</body></html>"
    install(pm, make_zip(extra), force=True)
    return before is False and pm.get("theme-a")["webui_ok"] is True


def a15():
    """select 字段的选项可以来自插件目录里的 JSON（options_file）。"""
    pm = fresh_pm("a15")
    manifest = json.dumps({
        "id": "opt-a", "name": "选项A", "version": "1.0.0", "type": "theme",
        "features": [{"key": "device", "label": "设备", "type": "select",
                      "default": "", "options_file": "data/devices.json"},
                     {"key": "mode", "label": "模式", "type": "select",
                      "default": "a", "options": [{"value": "a", "label": "A"}]}],
    }, ensure_ascii=False)
    install(pm, make_zip({"plugin.json": manifest}))
    (pm.data_dir("opt-a") / "devices.json").write_text(
        json.dumps({"options": ["麦克风 A", {"value": "扬声器 B", "label": "扬声器 B"}]},
                   ensure_ascii=False), encoding="utf-8")
    fields = {f["key"]: f for f in pm.get("opt-a")["features"]}
    dynamic = [o["value"] for o in fields["device"]["options"]]
    static = [o["value"] for o in fields["mode"]["options"]]
    # 没有选项文件时退回清单里写死的 options（不能变成空下拉）
    (pm.data_dir("opt-a") / "devices.json").unlink()
    fallback = {f["key"]: f for f in pm.get("opt-a")["features"]}
    return (dynamic == ["麦克风 A", "扬声器 B"] and static == ["a"]
            and fallback["device"]["options"] == []
            and "options_file" not in fallback["device"])


check("安装皮肤插件成功", a1)
check("列表正确反映插件能力（皮肤）", a2)
check("列表正确反映插件能力（代码）", a3)
check("可以停用插件", a4)
check("启用状态持久化", a5)
check("卸载插件并清理目录", a6)
check("覆盖安装更新版本号", a7)
check("卸载不存在的插件安全返回", a8)
check("列表忽略缓存/隐藏目录", a9)
check("卸载默认删掉配置与数据", a10)
check("卸载可保留插件配置", a11)
check("卸载可保留插件数据", a12)
check("保留的配置重装后还能用", a13)
check("webui.html 决定有没有 webui", a14)
check("下拉选项可来自插件目录的 JSON", a15)

def a16():
    """置顶：记住顺序、能持久化，新置顶的排最前。"""
    pm = fresh_pm("a16")
    for pid in ("theme-a", "theme-b", "theme-c"):
        install(pm, make_zip({
            "plugin.json": json.dumps({"id": pid, "name": pid, "version": "1.0.0",
                                       "type": "theme"}),
            "theme.css": ".x{}",
        }))
    pm.set_pinned("theme-a", True)
    pm.set_pinned("theme-c", True)
    marks = {p["id"]: (p["pinned"], p["pin_order"]) for p in pm.list_plugins()}
    pm2 = PluginManager(pm.root, pm.state_file)
    return (pm.pinned_ids() == ["theme-c", "theme-a"]
            and marks["theme-c"] == (True, 0)
            and marks["theme-a"] == (True, 1)
            and marks["theme-b"] == (False, -1)
            and pm2.pinned_ids() == ["theme-c", "theme-a"])


def a17():
    """取消置顶、卸载、对不存在的插件置顶都要处理好。"""
    pm = fresh_pm("a17")
    install(pm, make_zip(GOOD_THEME))
    pm.set_pinned("theme-a", True)
    after_pin = pm.pinned_ids()
    pm.set_pinned("theme-a", False)
    after_unpin = pm.pinned_ids()
    pm.set_pinned("theme-a", True)
    pm.uninstall("theme-a")
    return (after_pin == ["theme-a"] and after_unpin == []
            and pm.pinned_ids() == []
            and pm.set_pinned("nope", True) is False)


def a18():
    """置顶不改变 list_plugins 的既有顺序（顺序由前端按 pin_order 排）。"""
    pm = fresh_pm("a18")
    for pid in ("theme-a", "theme-b"):
        install(pm, make_zip({
            "plugin.json": json.dumps({"id": pid, "name": pid, "version": "1.0.0",
                                       "type": "theme"}),
            "theme.css": ".x{}",
        }))
    before = [p["id"] for p in pm.list_plugins()]
    pm.set_pinned("theme-b", True)
    after = [p["id"] for p in pm.list_plugins()]
    return before == after == ["theme-a", "theme-b"]


check("置顶记住顺序并能持久化", a16)
check("取消置顶 / 卸载 / 不存在的插件", a17)
check("置顶不改变管理器返回顺序", a18)


# ------------------------------------------------------------ B 安全扫描
section("B 安全扫描：风险识别与拦截")


def b1():
    """os.system 属常见写法，只提示不阻断。"""
    f = scan_python_source("import os\nos.system('x')")
    s = summarize_risks(f)
    return (any(x["level"] == "medium" and "系统命令" in x["title"] for x in f)
            and s["requires_confirm"] is False)


def b2():
    """subprocess 属常见写法，只提示不阻断。"""
    f = scan_python_source("import subprocess\nsubprocess.Popen(['cmd'])")
    s = summarize_risks(f)
    return s["medium"] >= 1 and s["high"] == 0


def b3():
    """eval/exec 属常见写法，只提示不阻断。"""
    f = scan_python_source("eval('1+1')\nexec('x=1')")
    s = summarize_risks(f)
    return s["medium"] >= 1 and s["high"] == 0


def b4():
    f = scan_python_source("import shutil\nshutil.rmtree('/')")
    return any(x["level"] == "high" and "删除" in x["title"] for x in f)


def b4b():
    """删单个文件不算高危。"""
    f = scan_python_source("import os\nos.remove('a.txt')")
    s = summarize_risks(f)
    return s["medium"] >= 1 and s["high"] == 0


def b4c():
    """裸建套接字不算高危，连到固定 IP 才算。"""
    plain = summarize_risks(scan_python_source("import socket\nsocket.socket()"))
    rev = summarize_risks(scan_python_source(
        "import socket\nsocket.socket().connect(('1.2.3.4', 4444))"))
    return plain["high"] == 0 and rev["high"] >= 1


def b4d():
    """访问注册表/系统 API 仍是高危。"""
    f = scan_python_source("import winreg\nwinreg.OpenKey(1, 'x')")
    return summarize_risks(f)["high"] >= 1


def b5():
    """网络请求算中等风险，不阻止安装但要提示。"""
    f = scan_python_source("import requests\nrequests.get('http://x')")
    s = summarize_risks(f)
    return s["medium"] >= 1 and s["requires_confirm"] is False


def b6():
    """纯皮肤代码零风险。"""
    f = scan_python_source("x = 1\nprint(x)")
    s = summarize_risks(f)
    return s["high"] == 0 and s["medium"] == 0


def b7():
    """注释里的危险词不算命中（避免误报让人不敢装）。"""
    f = scan_python_source("# os.system('这一行是注释')\nx = 1")
    return not any(x["level"] == "high" for x in f)


def b8():
    """混淆检测：超长单行应被标记。"""
    f = scan_python_source("x = '" + "a" * 900 + "'")
    return any("混淆" in x["title"] for x in f)


def b9():
    """恶意插件未经确认时不安装。"""
    pm = fresh_pm("b9")
    r = install(pm, make_zip(EVIL_PY))
    return (r["success"] is False and r.get("needs_confirm") is True
            and pm.list_plugins() == [])


def b10():
    """用户确认后可以强制安装。"""
    pm = fresh_pm("b10")
    r = install(pm, make_zip(EVIL_PY), force=True)
    return r["success"] is True and len(pm.list_plugins()) == 1


def b11():
    """风险项要带文件名与行号，用户才知道去哪看。"""
    pm = fresh_pm("b11")
    rep = pm.inspect_zip(make_zip(EVIL_PY))
    for it in rep["risks"]["items"]:
        if it["level"] == "high":
            if it.get("file") != "main.py" or not it.get("hits"):
                return False
    return rep["risks"]["high"] >= 2


def b12():
    pm = fresh_pm("b12")
    rep = pm.inspect_zip(make_zip(GOOD_THEME))
    return rep["ok"] and rep["risks"]["requires_confirm"] is False


check("os.system 记为中风险不阻断", b1)
check("subprocess 记为中风险不阻断", b2)
check("eval/exec 记为中风险不阻断", b3)
check("shutil.rmtree 仍为高危", b4)
check("删单个文件不算高危", b4b)
check("裸套接字不算高危、反弹连接算", b4c)
check("注册表/系统 API 仍为高危", b4d)
check("网络请求定为中等风险不阻断", b5)
check("无害代码零风险", b6)
check("注释里的危险词不误报", b7)
check("识别超长单行混淆", b8)
check("恶意插件未确认则不安装", b9)
check("确认后可强制安装", b10)
check("风险项包含文件与行号", b11)
check("皮肤插件无需确认", b12)


# ------------------------------------------------------------ C 包安全
section("C 包安全：路径穿越 / 非法包 / 体积限制")


def c1():
    """../ 路径穿越条目必须被丢弃，不能写到插件目录之外。"""
    pm = fresh_pm("c1")
    data = make_zip({"../../pwned.txt": "x",
                     "plugin.json": json.dumps({"id": "t1", "name": "T1", "version": "1"})})
    rep = pm.inspect_zip(data)
    return rep["ok"] and any("非法路径" in w for w in rep["warnings"])


def c2():
    """真安装一次，确认外面没有多出文件。"""
    pm = fresh_pm("c2")
    pm.ensure_root()
    outside = pm.root.parent / "pwned.txt"
    data = make_zip({"../../pwned.txt": "x",
                     "plugin.json": json.dumps({"id": "t2", "name": "T2", "version": "1"})})
    install(pm, data, force=True)
    return not outside.exists()


def c3():
    pm = fresh_pm("c3")
    rep = pm.inspect_zip(b"this is not a zip at all")
    return (not rep["ok"]) and "zip" in rep["error"].lower()


def c4():
    """不支持的文件类型被跳过（如 .exe）。"""
    pm = fresh_pm("c4")
    rep = pm.inspect_zip(make_zip({
        "plugin.json": json.dumps({"id": "t4", "name": "T4", "version": "1"}),
        "evil.exe": "MZ",
    }))
    return rep["ok"] and any(".exe" in w for w in rep["warnings"])


def c5():
    """空包（没有任何可用文件）应被拒绝。"""
    pm = fresh_pm("c5")
    rep = pm.inspect_zip(make_zip({"evil.exe": "MZ"}))
    return not rep["ok"]


def c6():
    """文件数超上限应被拒绝。"""
    pm = fresh_pm("c6")
    files = {f"f{i}.txt": "x" for i in range(MAX_FILES + 5)}
    files["plugin.json"] = json.dumps({"id": "t6", "name": "T6", "version": "1"})
    rep = pm.inspect_zip(make_zip(files))
    return not rep["ok"] and "文件过多" in rep["error"]


def c7():
    """插件 id 非法应被拒绝。"""
    pm = fresh_pm("c7")
    rep = pm.inspect_zip(make_zip({
        "plugin.json": json.dumps({"id": "../escape", "name": "X", "version": "1"})}))
    r = install(pm, make_zip({
        "plugin.json": json.dumps({"id": "../escape", "name": "X", "version": "1"})}))
    return r["success"] is False


def c8():
    """包了一层文件夹的 zip 也能装（很多人压缩时会多套一层）。"""
    pm = fresh_pm("c8")
    data = make_zip({
        "myplugin/plugin.json": json.dumps({"id": "wrapped", "name": "套层", "version": "1"}),
        "myplugin/theme.css": ".a{}",
    })
    r = install(pm, data, force=True)
    return r["success"] and (pm.plugin_dir("wrapped") / "theme.css").is_file()


def c9():
    """缺清单时按目录名兜底，仍可安装，并补一份 plugin.yaml。"""
    pm = fresh_pm("c9")
    data = make_zip({"mystyle/theme.css": ".a{}"})
    rep = pm.inspect_zip(data)
    r = install(pm, data, force=True)
    if not r["success"] or not any("plugin.yaml" in w for w in rep["warnings"]):
        return False
    return (pm.plugin_dir("mystyle") / "plugin.yaml").is_file()


def c10():
    """放开的文件类型（数据 / 媒体 / 前端源码）不再被跳过。"""
    pm = fresh_pm("c10")
    rep = pm.inspect_zip(make_zip({
        "plugin.json": json.dumps({"id": "t10", "name": "T10", "version": "1"}),
        "data.csv": "a,b\n1,2",
        "clip.mp4": "x",
        "mod.wasm": "x",
        "cache.sqlite": "x",
        "ui/panel.tsx": "export default 1",
    }))
    return rep["ok"] and not rep["warnings"] and rep["file_count"] == 6


check("路径穿越条目被丢弃", c1)
check("安装后未写出插件目录之外", c2)
check("非法 zip 被拒绝", c3)
check("不支持的文件类型被跳过", c4)
check("空包被拒绝", c5)
check("文件数超限被拒绝", c6)
check("非法插件 id 被拒绝", c7)
check("多套一层目录的包也能装", c8)
check("缺清单时按目录名兜底", c9)
check("数据/媒体/前端源码类文件不再被跳过", c10)


# ------------------------------------------------------------ D 运行时
section("D 插件运行时：加载 / 分发 / 隔离 / 热重载")


def _rt(sub, **kw):
    pm = fresh_pm(sub)
    logs = []
    rt = PluginRuntime(pm, logger=logs.append, config_getter=lambda: {"k": 1},
                       sender=kw.pop("sender", lambda g, t: True))
    return pm, rt, logs


def d1():
    pm, rt, logs = _rt("d1")
    install(pm, make_zip(GOOD_PY))
    errs = rt.load_all()
    return errs == {} and "func-a" in rt.loaded


def d2():
    pm, rt, logs = _rt("d2")
    install(pm, make_zip(GOOD_PY))
    rt.load_all()
    return rt.command_names() == ["ping"]


def d3():
    pm, rt, logs = _rt("d3")
    install(pm, make_zip(GOOD_PY))
    rt.load_all()
    return rt.dispatch_command("ping", "", {}) == "pong"


def d4():
    """指令名带 # 或 / 前缀也要能匹配（用户可能照着提示带前缀发）。"""
    pm, rt, logs = _rt("d4")
    install(pm, make_zip(GOOD_PY))
    rt.load_all()
    return rt.dispatch_command("#ping", "", {}) == "pong" \
        and rt.dispatch_command("/ping", "", {}) == "pong"


def d5():
    pm, rt, logs = _rt("d5")
    install(pm, make_zip(GOOD_PY))
    rt.load_all()
    return rt.dispatch_message({"text": "say hi"}) == ["hi"] \
        and rt.dispatch_message({"text": "别理我"}) == []


def d6():
    """入口 import 就失败的插件（语法错/缺依赖）不算加载成功，其他插件不受影响。"""
    pm, rt, logs = _rt("d6")
    install(pm, make_zip(GOOD_PY))
    install(pm, make_zip({
        "plugin.json": json.dumps({"id": "badimport", "name": "导入就炸", "version": "1"}),
        "main.py": "import a_module_that_does_not_exist_xyz\n"}), force=True)
    errs = rt.load_all()
    return "badimport" in errs and "func-a" in rt.loaded \
        and "badimport" not in rt.loaded


def d6b():
    "入口导入成功、仅 on_load 抛异常的插件仍算加载成功。"
    pm, rt, logs = _rt("d6b")
    install(pm, make_zip({
        "plugin.json": json.dumps({"id": "boomload", "name": "加载里炸", "version": "1"}),
        "main.py": "def on_load(ctx):\n    raise RuntimeError('炸了')\n"}), force=True)
    errs = rt.load_all()
    return "boomload" in rt.loaded and any("on_load 执行出错" in l for l in logs)


def d7():
    """指令处理器抛异常被吞掉，其他插件不受影响。"""
    pm, rt, logs = _rt("d7")
    install(pm, make_zip(GOOD_PY))
    install(pm, make_zip({
        "plugin.json": json.dumps({"id": "bad", "name": "坏", "version": "1"}),
        "main.py": ("def on_load(ctx):\n"
                    "    ctx.register_command('crash', lambda c,n,a,e: 1/0)\n")}),
        force=True)
    rt.load_all()
    return rt.dispatch_command("crash", "", {}) is None \
        and rt.dispatch_command("ping", "", {}) == "pong"


def d8():
    """消息钩子抛异常不影响其他插件。"""
    pm, rt, logs = _rt("d8")
    install(pm, make_zip(GOOD_PY))
    install(pm, make_zip({
        "plugin.json": json.dumps({"id": "badmsg", "name": "坏消息", "version": "1"}),
        "main.py": "def on_message(ctx, event):\n    raise ValueError('x')\n"}),
        force=True)
    rt.load_all()
    return rt.dispatch_message({"text": "hi"}) == ["hi"]


def d9():
    """热重载：停用后指令消失，重新启用后回来。"""
    pm, rt, logs = _rt("d9")
    install(pm, make_zip(GOOD_PY))
    rt.load_all()
    before = rt.command_names()
    pm.set_enabled("func-a", False)
    rt.reload_all()
    mid = rt.command_names()
    pm.set_enabled("func-a", True)
    rt.reload_all()
    after = rt.command_names()
    return before == ["ping"] and mid == [] and after == ["ping"]


def d10():
    """纯皮肤插件没有 main.py 也要能加载（不报错）。"""
    pm, rt, logs = _rt("d10")
    install(pm, make_zip(GOOD_THEME))
    errs = rt.load_all()
    return errs == {} and "theme-a" in rt.loaded


def d11():
    """卸载全部后 loaded 应清空，且调 on_unload 不抛异常。"""
    pm, rt, logs = _rt("d11")
    install(pm, make_zip(GOOD_PY))
    rt.load_all()
    rt.unload_all()
    return rt.loaded == {}


def d12():
    """ctx.data_dir 给每个插件独立目录。"""
    pm, rt, logs = _rt("d12")
    install(pm, make_zip(GOOD_PY))
    rt.load_all()
    ctx = rt.loaded["func-a"]["ctx"]
    d = ctx.data_dir()
    return d.is_dir() and d.parent.name == "func-a"


def d13():
    """ctx.log 应该走注入的 logger。"""
    pm, rt, logs = _rt("d13")
    install(pm, make_zip(GOOD_PY))
    rt.load_all()
    return any("功能A" in ln for ln in logs)


def d14():
    """ctx.send_message 通过注入的 sender 发出。"""
    sent = []
    pm = fresh_pm("d14")
    rt = PluginRuntime(pm, logger=lambda s: None, config_getter=dict,
                       sender=lambda g, t: (sent.append((g, t)), True)[1])
    install(pm, make_zip({
        "plugin.json": json.dumps({"id": "send", "name": "发消息", "version": "1"}),
        "main.py": ("def on_load(ctx):\n"
                    "    ctx.send_message(123, '来自插件')\n")}), force=True)
    rt.load_all()
    return sent == [(123, "来自插件")]


def d15():
    """sender 缺失时 send_message 返回 False 而不是抛异常。"""
    pm = fresh_pm("d15")
    rt = PluginRuntime(pm, logger=lambda s: None, config_getter=dict, sender=None)
    install(pm, make_zip({
        "plugin.json": json.dumps({"id": "nosend", "name": "无发送器", "version": "1"}),
        "main.py": ("def on_load(ctx):\n"
                    "    ctx.send_message(1, 'x')\n")}), force=True)
    rt.load_all()
    return "nosend" in rt.loaded


def d16():
    """插件自带的库在钩子调用时也 import 得到（插件目录常驻 sys.path）。"""
    pm, rt, logs = _rt("d16")
    install(pm, make_zip({
        "plugin.json": json.dumps({"id": "vendored", "name": "自带库", "version": "1"}),
        "mylib/__init__.py": "VALUE = 'ok'\n",
        "main.py": ("def on_message(ctx, event):\n"
                    "    from mylib import VALUE\n"
                    "    return VALUE\n")}), force=True)
    rt.load_all()
    return rt.dispatch_message({"text": "x"}) == ["ok"]


def d17():
    """卸载后插件目录从 sys.path 摘掉。"""
    pm, rt, logs = _rt("d17")
    install(pm, make_zip({
        "plugin.json": json.dumps({"id": "pathtest", "name": "路径", "version": "1"}),
        "main.py": "def on_load(ctx):\n    pass\n"}), force=True)
    rt.load_all()
    d = str(pm.plugin_dir("pathtest"))
    added = d in sys.path
    rt.unload_one("pathtest")
    return added and d not in sys.path


check("加载启用插件", d1)
check("注册的指令可见", d2)
check("指令分发返回结果", d3)
check("指令名前缀 # / 兼容", d4)
check("消息钩子分发与过滤", d5)
check("导入失败的插件被排除且不影响他人", d6)
check("on_load 抛异常仍算加载成功（记日志）", d6b)
check("指令异常被隔离", d7)
check("消息钩子异常被隔离", d8)
check("热重载启停生效", d9)
check("纯皮肤插件可加载", d10)
check("卸载全部清理运行时", d11)
def d18():
    """两个插件抢同一个指令名：先加载的生效，后来者注册时留一条日志说明。"""
    pm, rt, logs = _rt("d18")
    install(pm, make_zip(GOOD_PY))
    install(pm, make_zip({
        "plugin.json": json.dumps({"id": "dup", "name": "抢指令", "version": "1"}),
        "main.py": ("def on_load(ctx):\n"
                    "    ctx.register_command('ping', lambda c,n,a,e: 'me')\n")}),
        force=True)
    rt.load_all()
    loaded = [p["id"] for p in pm.list_plugins() if p["id"] in rt.loaded]
    if len(loaded) != 2:
        return False
    first_is_good = loaded[0] == "func-a"
    winner_name = "功能A" if first_is_good else "抢指令"
    return (rt.dispatch_command("ping", "", {}) == ("pong" if first_is_good else "me")
            and rt.command_owner("ping") == winner_name
            and any(f"指令 ping 已由插件「{winner_name}」注册" in line for line in logs))


check("插件数据目录独立", d12)
check("插件日志走注入 logger", d13)
check("send_message 可用", d14)
check("缺 sender 时不崩", d15)
check("插件自带的库在钩子里可 import", d16)
check("卸载后插件目录从 sys.path 摘掉", d17)
check("同名指令先加载者生效并留日志", d18)


# ------------------------------------------------------------ E API 端到端
section("E API 端到端：list / upload / inspect / toggle / delete / theme")

_PORT = None
_SRV = None
_CFG = None
_TMPSRV = None
_RT = None
_LOOP = None
_LOOP_THREAD = None


def _start_server():
    "在独立线程里跑一个长期事件循环（供多个 check 复用）。"
    global _PORT, _SRV, _CFG, _TMPSRV, _RT, _LOOP, _LOOP_THREAD
    if _SRV is not None:
        return
    import threading
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    _PORT = s.getsockname()[1]
    s.close()
    _TMPSRV = Path(tempfile.mkdtemp(prefix="lovomo_plugapi_"))
    _CFG = M.ConfigLoader()

    class _MM:
        def __init__(self, p):
            self.data_path = p

    _SRV = M.WebUIServer(_CFG, _MM(_TMPSRV))
    _SRV.plugin_manager.root = _TMPSRV / "plugins"
    _SRV.plugin_manager.state_file = _TMPSRV / "plugins" / "state.json"
    _SRV.plugin_manager._state_cache = None
    _RT = PluginRuntime(_SRV.plugin_manager, logger=lambda s: None,
                        config_getter=lambda: (_CFG.config or {}),
                        sender=lambda g, t: True)
    _SRV.attach_plugin_runtime(_RT)
    _CFG.config["webui_port"] = _PORT
    _CFG.config["webui_password"] = ""

    ready = threading.Event()

    def _run():
        global _LOOP
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)

        async def _boot():
            await _SRV.start()
            ready.set()

        try:
            _LOOP.run_until_complete(_boot())
            _LOOP.run_forever()
        except Exception:
            ready.set()

    _LOOP_THREAD = threading.Thread(target=_run, daemon=True)
    _LOOP_THREAD.start()
    ready.wait(timeout=20)
    # 等端口真正可连（aiohttp 起来后还有极短窗口）
    for _ in range(60):
        try:
            with socket.create_connection(("127.0.0.1", _PORT), timeout=1):
                return
        except OSError:
            time.sleep(0.2)


def _client():
    return httpx.Client(base_url=f"http://127.0.0.1:{_PORT}", timeout=25)


_start_server()


def e1():
    with _client() as c:
        r = c.get("/api/plugins/list")
        j = r.json()
        return r.status_code == 200 and j["success"] and j["plugins"] == []


def e2():
    with _client() as c:
        r = c.post("/api/plugins/inspect",
                   files={"file": ("a.zip", make_zip(GOOD_THEME), "application/zip")})
        j = r.json()
        return r.status_code == 200 and j["success"] and j["report"]["manifest"]["id"] == "theme-a"


def e3():
    with _client() as c:
        r = c.post("/api/plugins/upload",
                   files={"file": ("a.zip", make_zip(GOOD_THEME), "application/zip")})
        j = r.json()
        return r.status_code == 200 and j["success"] and j["id"] == "theme-a"


def e4():
    with _client() as c:
        r = c.get("/api/plugins/list")
        ps = r.json()["plugins"]
        # 上传装完不该直接跑起来，用户得自己去「插件」里启用
        return len(ps) == 1 and ps[0]["id"] == "theme-a" and ps[0]["enabled"] is False


def e5():
    with _client() as c:
        c.post("/api/plugins/toggle", json={"id": "theme-a", "enabled": True})
        r = c.get("/api/plugins/theme")
        return r.status_code == 200 and "#f0f" in r.text


def e6():
    with _client() as c:
        r = c.post("/api/plugins/toggle", json={"id": "theme-a", "enabled": False})
        ok = r.json()["success"] and r.json()["enabled"] is False
        css = c.get("/api/plugins/theme").text
        return ok and css.strip() == ""


def e7():
    with _client() as c:
        c.post("/api/plugins/toggle", json={"id": "theme-a", "enabled": True})
        return "#f0f" in c.get("/api/plugins/theme").text


def e8():
    """恶意插件走 API 也要拦下来。"""
    with _client() as c:
        r = c.post("/api/plugins/upload",
                   files={"file": ("e.zip", make_zip(EVIL_PY), "application/zip")})
        j = r.json()
        return r.status_code == 400 and j["success"] is False and j["needs_confirm"] is True


def e9():
    """force=1 时通过；高危插件装完同样是停用的，不该自动跑起来。"""
    with _client() as c:
        r = c.post("/api/plugins/upload",
                   files={"file": ("e.zip", make_zip(EVIL_PY), "application/zip"),
                          "force": "1"})
        if not (r.status_code == 200 and r.json()["success"] is True):
            return False
        ps = {p["id"]: p for p in c.get("/api/plugins/list").json()["plugins"]}
        return ps.get("evil-a", {}).get("enabled") is False


def e10():
    with _client() as c:
        r = c.post("/api/plugins/delete", json={"id": "evil-a"})
        return r.json()["success"] is True


def e11():
    with _client() as c:
        r = c.post("/api/plugins/toggle", json={"id": "no-such", "enabled": True})
        return r.status_code == 404


def e12():
    with _client() as c:
        r = c.post("/api/plugins/delete", json={"id": "no-such"})
        return r.status_code == 404


def e13():
    """非 zip 文件应被拒绝。"""
    with _client() as c:
        r = c.post("/api/plugins/upload",
                   files={"file": ("a.txt", b"hello", "text/plain")})
        return r.status_code == 400


def e14():
    """没有文件也要给出明确错误，不能 500。"""
    with _client() as c:
        r = c.post("/api/plugins/inspect", files={"other": ("x.txt", b"1", "text/plain")})
        return r.status_code == 400 and not r.json()["success"]


def e15():
    """卸载后市场状态重查：列表里不该再出现。"""
    with _client() as c:
        ids = [p["id"] for p in c.get("/api/plugins/list").json()["plugins"]]
        return "evil-a" not in ids


def e16():
    """保存设置只落盘成草稿，不动生效值（皮肤 CSS、插件读到的设置都不变）。"""
    skin_pkg = {
        "plugin.json": json.dumps({
            "id": "reload-skin", "name": "重载演示", "version": "1.0.0",
            "type": "theme",
            "skin": {"label": "重载演示", "accent": "#7c4dff", "vars": [
                {"name": "radius", "label": "圆角", "type": "range",
                 "min": 0, "max": 28, "step": 1, "default": 14},
            ]},
        }),
        "theme.css": ".section-block{border-radius:var(--skin-radius,14px)!important}",
    }
    with _client() as c:
        c.post("/api/plugins/upload",
               files={"file": ("r.zip", make_zip(skin_pkg), "application/zip")})
        c.post("/api/plugins/toggle", json={"id": "reload-skin", "enabled": True})
        r = c.post("/api/plugins/settings",
                   json={"id": "reload-skin", "settings": {"radius": 24}})
        j = r.json()
        if not (r.status_code == 200 and j["success"]):
            return False
        # 返回的是草稿，不是生效值
        if j.get("draft", {}).get("radius") != 24:
            return False
        # 生效值仍是默认的 14，主题 CSS 也还没变
        applied = c.get("/api/plugins/settings?id=reload-skin").json()["settings"]
        if applied.get("radius") == 24:
            return False
        return "--skin-radius: 24;" not in c.get("/api/plugins/theme").text


def e17():
    """reload 才是生效开关：提交草稿后主题 CSS 立刻反映新设置。"""
    with _client() as c:
        r = c.post("/api/plugins/reload", json={"id": "reload-skin"})
        j = r.json()
        if not (r.status_code == 200 and j["success"]):
            return False
        if not isinstance(j.get("active_skins"), list):
            return False
        if j.get("settings", {}).get("radius") != 24:
            return False
        return "--skin-radius: 24;" in c.get("/api/plugins/theme").text


def e18():
    """reload 不存在的插件要 404。"""
    with _client() as c:
        r = c.post("/api/plugins/reload", json={"id": "no-such-plugin"})
        return r.status_code == 404 and not r.json()["success"]


check("API 列表（空）", e1)
check("API 审查不改动磁盘", e2)
check("API 上传安装", e3)
check("API 列表含新插件（装完默认停用）", e4)
check("API 返回皮肤 CSS", e5)
check("API 停用后皮肤消失", e6)
check("API 重新启用皮肤回来", e7)
check("API 拦截恶意插件", e8)
check("API force 安装通过（高危插件装完也停用）", e9)
check("API 卸载", e10)
check("API 操作不存在插件 404", e11)
check("API 删除不存在插件 404", e12)
check("API 拒绝非 zip", e13)
check("API 缺文件返回 400", e14)
check("API 卸载后列表一致", e15)
check("API 保存设置只落盘不重载", e16)
check("API reload 才是生效开关", e17)
check("API reload 不存在插件 404", e18)


# ------------------------------------------------------------ F 皮肤聚合
section("F 皮肤聚合：只注入启用的")


def f1():
    pm = fresh_pm("f1")
    install(pm, make_zip(GOOD_THEME))
    return "#f0f" in pm.theme_css()


def f2():
    pm = fresh_pm("f2")
    install(pm, make_zip(GOOD_THEME))
    pm.set_enabled("theme-a", False)
    return pm.theme_css() == ""


def f3():
    """多插件皮肤按顺序拼接，带横幅注释便于排查。"""
    pm = fresh_pm("f3")
    install(pm, make_zip(GOOD_THEME))
    install(pm, make_zip({
        "plugin.json": json.dumps({"id": "theme-b", "name": "主题B", "version": "2.0.0",
                                   "type": "theme"}),
        "theme.css": ".x{color:red}"}))
    css = pm.theme_css()
    return "#f0f" in css and ".x{color:red}" in css \
        and css.count("插件皮肤") == 2


def f4():
    """没有 theme.css 的纯代码插件不贡献 CSS。"""
    pm = fresh_pm("f4")
    install(pm, make_zip(GOOD_PY))
    return pm.theme_css() == ""


def f5():
    """图标缺失时返回 None，不抛异常。"""
    pm = fresh_pm("f5")
    install(pm, make_zip(GOOD_THEME))
    return pm.icon_file("theme-a") is None


def f6():
    """有 icon.png 时能取到。"""
    pm = fresh_pm("f6")
    install(pm, make_zip({
        "plugin.json": json.dumps({"id": "withicon", "name": "有图标", "version": "1"}),
        "icon.png": b"\x89PNG\r\n\x1a\n"}))
    f = pm.icon_file("withicon")
    return f is not None and f.name == "icon.png"


check("启用插件皮肤被注入", f1)
check("停用后皮肤为空", f2)
check("多插件皮肤拼接", f3)
check("无皮肤插件不注入 CSS", f4)
check("缺图标返回 None", f5)
check("有图标可读取", f6)


# ------------------------------------------------------------ G 静态检查
section("G 静态检查：路由与前端元素")


def g1():
    routes = ("/api/plugins/list", "/api/plugins/upload", "/api/plugins/inspect",
              "/api/plugins/toggle", "/api/plugins/delete", "/api/plugins/theme",
              "/api/plugins/icon", "/api/plugins/open_dir", "/api/plugins/market",
              "/api/plugins/install_remote")
    return all(f'"{r}"' in WEBUI_SRC for r in routes)


def g2():
    "左侧栏必须有两个独立入口：插件市场 与 插件，且顺序正确。"
    for need in ('id="panel-plugins"', 'id="panel-installed-plugins"',
                 'data-panel="plugins"', 'data-panel="installed-plugins"',
                 'id="plugin-list-body"'):
        if need not in HTML_SRC:
            return False
    i_m = HTML_SRC.find('data-panel="plugins"')
    i_p = HTML_SRC.find('data-panel="installed-plugins"')
    i_chat = HTML_SRC.find('data-panel="chat"')
    return -1 < i_m < i_p < i_chat


def g3():
    "市场页只管装插件，已装插件列表必须在另一个面板里。"
    ids = ("plugin-message", "plugin-market-list",
           "plugin-upload-btn", "plugin-upload-file", "plugin-open-dir-btn",
           "plugin-market-refresh-btn", "plugins-root-path")
    if not all(f'id="{i}"' in HTML_SRC for i in ids):
        return False
    # 取 panel-plugins 的整段 DOM，确认里面没有已装插件列表
    i = HTML_SRC.find('id="panel-plugins"')
    j = HTML_SRC.find('id="panel-installed-plugins"')
    if not (-1 < i < j):
        return False
    return 'id="plugin-list-body"' not in HTML_SRC[i:j]


def g4():
    """切页要触发加载：插件市场页拉市场 + 发布面板，插件页拉已装列表。"""
    return ("if (panelId === 'plugins') { loadPluginsPage(); loadPublishPanel(); }" in HTML_SRC
            and "if (panelId === 'installed-plugins') loadInstalledPlugins();" in HTML_SRC
            and "loadPublishPanel()" in HTML_SRC)


def g5():
    """页面加载时就要注入皮肤，否则换肤要切页才生效。"""
    return "applyPluginTheme()" in HTML_SRC and "plugin-theme" in HTML_SRC


def g6():
    "「插件市场（测试版）」是切页入口，必须带 data-panel。"
    i_cfg = HTML_SRC.find('data-panel="config"')
    i_plug = HTML_SRC.find('data-panel="plugins"')
    i_chat = HTML_SRC.find('data-panel="chat"')
    if not (-1 < i_cfg < i_plug < i_chat):
        return False
    return '插件市场（测试版）' in HTML_SRC


def g7():
    """main.py 里要真的把运行时接上（否则插件永不被加载）。"""
    return "attach_plugin_runtime" in MAIN_SRC and "PluginRuntime(" in MAIN_SRC


def g8():
    """插件运行时应挂到主事件循环上，插件才能异步发消息。"""
    return "MAIN_EVENT_LOOP" in MAIN_SRC


def g9():
    """市场索引示例文件与示例插件源码应在仓库里。"""
    idx = ROOT / "plugins" / "index.json"
    if not idx.is_file():
        return False
    data = json.loads(idx.read_text(encoding="utf-8"))
    return isinstance(data.get("plugins"), list) and len(data["plugins"]) >= 1


def g10():
    """每个示例插件源码都应有一个对应的打包产物。"""
    srcs = sorted(d for d in (ROOT / "plugins" / "sources").iterdir() if d.is_dir())
    if not srcs:
        print("      sources 下没有任何示例插件")
        return False
    for d in srcs:
        if not (ROOT / "plugins" / "packages" / f"{d.name}.zip").is_file():
            print(f"      {d.name} 改了源码但没重新打包")
            return False
    return True


def g11():
    """示例插件包应能通过自身的安全审查，且包内 id 与包名一致。"""
    pm = fresh_pm("g11")
    zips = sorted((ROOT / "plugins" / "packages").glob("*.zip"))
    if not zips:
        print("      packages 下没有任何插件包")
        return False
    for f in zips:
        rep = pm.inspect_zip(f.read_bytes())
        if not rep["ok"] or rep["manifest"]["id"] != f.stem:
            print(f"      {f.name} 审查不通过或 id 不符")
            return False
    return True


def g12():
    """示例皮肤包应能通过审查且零风险。"""
    pm = fresh_pm("g12")
    data = (ROOT / "plugins" / "packages" / "sakura-theme.zip").read_bytes()
    rep = pm.inspect_zip(data)
    return rep["ok"] and rep["risks"]["high"] == 0 and rep["has_theme"]


def g13():
    """总开关 plugins_enabled 存在且默认开启。"""
    cfg = M.ConfigLoader()
    return cfg.default_config().get("plugins_enabled") is True


def g14():
    """总开关关闭时皮肤接口返回空注释，不注入任何插件样式。"""
    saved = None
    try:
        saved = _CFG.config.get("plugins_enabled")
        _CFG.config["plugins_enabled"] = False
        with _client() as c:
            t = c.get("/api/plugins/theme").text
        return "已关闭" in t and "!important" not in t
    finally:
        _CFG.config["plugins_enabled"] = True if saved is None else saved


def g15():
    """总开关关闭时 _on_plugins_changed 会把已加载插件卸掉。"""
    saved = _CFG.config.get("plugins_enabled")
    try:
        _CFG.config["plugins_enabled"] = True
        _SRV._on_plugins_changed()
        _CFG.config["plugins_enabled"] = False
        _SRV._on_plugins_changed()
        return _RT.loaded == {}
    finally:
        _CFG.config["plugins_enabled"] = True if saved is None else saved
        _SRV._on_plugins_changed()


def g16():
    """插件总开关与镜像留在配置页；内部选项（仓库、分支前缀、索引、来源清单）不露出来。"""
    if "plugins_enabled" not in HTML_SRC or "github_mirrors" not in HTML_SRC:
        return False
    i = HTML_SRC.find("{ title: '插件系统'")
    if i < 0:
        return False
    group = HTML_SRC[i:HTML_SRC.find("\n", i)]
    return ("github_mirrors" in group and "plugins_enabled" in group
            and "plugin_release_repo" not in group)


def g17():
    "WebUI 服务必须真的被实例化。"
    i = MAIN_SRC.find('if HAS_AIOHTTP and app_context.global_config.get("webui_enabled", True):')
    if i < 0:
        return False
    block = MAIN_SRC[i:i + 400]
    return "webui_server = WebUIServer(app_context.global_config, app_context.memory_manager)" in block


def g18():
    """插件初始化必须放在 webui_server 实例化之后（否则必然 None 崩溃）。"""
    i_new = MAIN_SRC.find("webui_server = WebUIServer(app_context.global_config, app_context.memory_manager)")
    i_plug = MAIN_SRC.find("webui_server.plugin_manager")
    return -1 < i_new < i_plug


def g19():
    """该 if 行后面不能紧跟注释粘在同一行（缩进/换行被破坏的信号）。"""
    bad = 'webui_enabled", True):        #'
    return bad not in MAIN_SRC and 'webui_enabled", True): #' not in MAIN_SRC

def g20():
    """用 AST 确认 WebUIServer 在 main() 里被调用过（防止再次整行丢失）。"""
    import ast
    tree = ast.parse(MAIN_SRC)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "WebUIServer":
            found = True
    return found


def g21():
    """发布 / 下架成功后按本地状态立刻重画，不等下一次 publish_status 往返。"""
    return ("function renderPublishPanel()" in HTML_SRC
            and "applyPublished(id, r)" in HTML_SRC
            and "applyUnpublished(id)" in HTML_SRC
            and "renderPublishPanel();" in HTML_SRC)


def g22():
    """「发布」与「下架」互斥：同一行只给一个动作。"""
    i = HTML_SRC.find("function renderPublishPanel()")
    j = HTML_SRC.find("function bindPublishUi()")
    if not (-1 < i < j):
        return False
    st = HTML_SRC[i:j]
    return ("const canPublish = !!p.installed && p.needs_publish !== false;" in st
            and "if (p.on_market && !canPublish) {" in st)


def g23():
    """点下「发布」就先禁用按钮，成功 / 失败之前不许再点。"""
    i = HTML_SRC.find("'.plugin-publish-btn').forEach")
    j = HTML_SRC.find("'.plugin-unpublish-btn').forEach")
    if not (-1 < i < j):
        return False
    st = HTML_SRC[i:j]
    # 先禁用再弹提交信息框，且 finally 里只改还在页面上的那个按钮
    return (st.find("b.disabled = true;") < st.find("window.prompt(")
            and "if (b.isConnected)" in st
            and "loadPublishPanel()" not in st)


# ---------------------------------------------------------------- H 功能开关
def h1():
    """plugin.json 的 features 段要被规范化成结构化开关声明。"""
    from modules.plugins import _normalize_features
    out = _normalize_features([
        {"key": "meow_reply_enabled", "label": "喵喵追加",
         "description": "d", "default": False},
    ])
    if len(out) != 1:
        return False
    it = out[0]
    return (it["key"] == "meow_reply_enabled" and it["label"] == "喵喵追加"
            and it["default"] is False)


def h2():
    """features 里非法的 key 要被剔除，且数量有上限。"""
    from modules.plugins import _normalize_features, MAX_FEATURES
    if _normalize_features([{"key": "!!!", "label": "x"}]):
        return False
    if _normalize_features([{"label": "没有key"}]) == []:
        pass
    else:
        return False
    big = _normalize_features([{"key": f"k{i}"} for i in range(200)])
    return len(big) <= MAX_FEATURES


def h3():
    """清单解析后必须带上 features 字段（缺失时为空列表）。"""
    from modules.plugins import _normalize_manifest
    m = _normalize_manifest({"id": "t", "name": "T", "version": "1"})
    return m.get("features") == []


def h4():
    """示例插件 meow-skin 的清单里要有开关声明，且 key 与插件读取的一致。"""
    from modules.plugins import _read_manifest_from_dir
    raw = _read_manifest_from_dir(ROOT / "plugins" / "sources" / "meow-skin")
    if not raw:
        return False
    feats = raw.get("features") or []
    keys = {f.get("key") for f in feats if isinstance(f, dict)}
    if "meow_reply_enabled" not in keys:
        return False
    # 插件代码里必须真的读这个 key，否则开关点了没反应
    src = (ROOT / "plugins" / "sources" / "meow-skin" / "main.py").read_text(
        encoding="utf-8")
    return "meow_reply_enabled" in src


def h5():
    "插件指令要能吃下运行时传入的 4 个位置参数。"
    import importlib.util
    entry = ROOT / "plugins" / "sources" / "meow-skin" / "main.py"
    if not entry.is_file():
        return False
    spec = importlib.util.spec_from_file_location("_meow_probe", entry)
    mod = importlib.util.module_from_spec(spec)

    class _Ctx:
        plugin_id = "meow-skin"
        plugin_name = "probe"
        commands = {}

        def log(self, *a):
            pass

        def register_command(self, name, fn):
            self.commands[name] = fn
            return True

        def data_dir(self):
            d = _TMP / "meow_probe"
            d.mkdir(parents=True, exist_ok=True)
            return d

    spec.loader.exec_module(mod)
    ctx = _Ctx()
    mod.on_load(ctx)
    if "喵开关" not in ctx.commands:
        return False
    # 4 参数调用（运行时真实契约）不能抛异常
    out = ctx.commands["喵开关"](ctx, "喵开关", "on", None)
    if not isinstance(out, str) or "开启" not in out:
        return False
    # 2 参数调用（兼容旧约定）也要能用
    out2 = ctx.commands["喵状态"](ctx, "")
    return isinstance(out2, str) and "喵喵插件状态" in out2


def h6():
    "功能页按清单声明渲染控件，并把 settings 回传。"
    for k in ("renderPluginPage", "pluginFieldHtml", "bindPluginFields",
              'data-field-act="set"', 'data-page-act="appearance"'):
        if k not in HTML_SRC:
            return False
    # 声明了的类型都要有对应的渲染分支，否则插件写了也没控件
    for t in ("range", "number", "color", "select", "multiselect",
              "textarea", "file"):
        if f"=== '{t}'" not in HTML_SRC and f"t === '{t}'" not in HTML_SRC:
            return False
    # 后端 list 接口在插件声明 features/skin/panel 时也要带上 settings
    i = WEBUI_SRC.find("async def handle_plugins_list")
    block = WEBUI_SRC[i:i + 1400]
    return 'it.get("features")' in block and 'it.get("panel")' in block


def h7():
    "主程序里不应再有内置换肤入口或锚点里的插件入口。"
    if "function switchPanel(" not in HTML_SRC:
        return False
    if 'data-panel="skins"' in HTML_SRC or "switchPanel('skins')" in HTML_SRC:
        return False
    return "anchor-shortcut" not in HTML_SRC


check("插件 API 路由齐备", g1)
check("市场面板与左侧栏导航入口齐备", g2)
check("市场页只留装插件元素（无已安装板块）", g3)
check("切页时加载插件页", g4)
check("启动即注入皮肤", g5)
check("插件市场导航位于配置文件之后", g6)
check("运行时已接入 main.py", g7)
check("主事件循环已暴露给插件", g8)
check("市场索引文件存在", g9)
check("示例插件包已打包", g10)
check("示例功能包通过审查", g11)
check("示例皮肤包零风险", g12)
check("插件总开关默认开启", g13)
check("总开关关闭时不再注入皮肤", g14)
check("总开关关闭时卸载已加载插件", g15)
check("配置表单含插件字段", g16)
check("WebUI 服务确实被实例化", g17)
check("插件初始化在实例化之后", g18)
check("if 行未与注释粘连（换行完好）", g19)
check("AST 确认 WebUIServer 被调用", g20)
check("发布/下架成功后按本地状态立刻重画", g21)
check("同一行不会同时给「发布」和「下架」", g22)
check("推送期间「发布」按钮不可点", g23)


def g24():
    """装完默认停用这条不能悄悄回退：enable 默认 False，且只在没有记录时才写。"""
    src = (ROOT / "modules" / "plugins.py").read_text(encoding="utf-8")
    return ("enable: bool = False" in src
            and 'self._state()["enabled"].setdefault(pid, bool(enable))' in src)


check("新装插件默认停用", g24)
check("features 声明被规范化", h1)
check("features 非法 key 剔除且限流", h2)
check("清单解析带上 features 字段", h3)
check("示例插件声明了功能开关且代码读取", h4)
check("插件指令签名兼容运行时 4 参数契约", h5)
check("插件功能页按声明渲染开关且回传 settings", h6)


# ------------------------------------------------- H2 插件制作自由度
section("H2 插件制作自由度（字段类型 / 任意皮肤变量 / 自写界面）")


def j1():
    """字段类型不再写死：声明什么类型就渲染什么控件。"""
    from modules.plugins import _normalize_features, FIELD_TYPES
    got = {}
    for item in _normalize_features([
        {"key": "a", "type": "range", "min": 0, "max": 1, "default": 0.5},
        {"key": "b", "type": "number", "min": 0, "max": 10, "default": 3},
        {"key": "c", "type": "text", "default": "hi"},
        {"key": "d", "type": "select", "options": ["x", "y"], "default": "y"},
        {"key": "e", "type": "multiselect", "options": ["x", "y"],
         "default": ["x"]},
        {"key": "f", "type": "color", "default": "#ff0000"},
        {"key": "g", "type": "textarea", "default": "多行"},
        {"key": "h", "type": "password", "default": ""},
        {"key": "i", "type": "bool", "default": True},
    ]):
        got[item["key"]] = item["type"]
    if set(got.values()) - set(FIELD_TYPES):
        return False
    return (got.get("a") == "range" and got.get("b") == "number"
            and got.get("c") == "text" and got.get("d") == "select"
            and got.get("e") == "multiselect" and got.get("f") == "color"
            and got.get("i") == "bool")


def j2():
    """只写 key/label/default 的布尔写法必须继续可用。"""
    from modules.plugins import _normalize_features
    out = _normalize_features([{"key": "legacy", "label": "旧开关",
                                "description": "d", "default": False}])
    if len(out) != 1:
        return False
    f = out[0]
    return f["type"] == "bool" and f["label"] == "旧开关" and f["default"] is False


def j3():
    """写了主程序不认识的类型时退回布尔，而不是让整条字段消失。"""
    from modules.plugins import _normalize_features
    out = _normalize_features([{"key": "weird", "type": "量子纠缠"}])
    return len(out) == 1 and out[0]["type"] == "bool"


def j4():
    """skin.vars 让插件声明任意皮肤变量，名子收敛成 --skin-<name>。"""
    from modules.plugins import _normalize_skin
    s = _normalize_skin({"vars": [
        {"name": "block-opacity", "label": "透明度", "type": "range",
         "min": 0.2, "max": 1, "default": 0.92},
        {"name": "skin-radius", "label": "圆角", "type": "range",
         "min": 0, "max": 30, "default": 14},
    ]})
    vars_ = s.get("vars") or []
    if len(vars_) != 2:
        return False
    names = {v["var"] for v in vars_}
    # 带不带 skin- 前缀都要落到同一个 --skin- 名字上，且不能出现 --skin-skin-
    return names == {"--skin-block-opacity", "--skin-radius"}


def j5():
    """插件声明的皮肤变量必须真的产生 CSS 变量并作用于页面。"""
    import tempfile
    from modules.plugins import PluginManager
    root = Path(tempfile.mkdtemp(prefix="lovomo_var_"))
    try:
        d = root / "var-demo"
        d.mkdir(parents=True)
        (d / "plugin.json").write_text(json.dumps({
            "id": "var-demo", "name": "V", "version": "1", "type": "theme",
            "skin": {"label": "V", "vars": [
                {"name": "block-opacity", "label": "透明度", "type": "range",
                 "min": 0.2, "max": 1, "step": 0.01, "default": 0.92},
            ]},
        }), encoding="utf-8")
        (d / "theme.css").write_text(
            ".b { opacity: var(--skin-block-opacity, 0.92); }", encoding="utf-8")
        mgr = PluginManager(str(root))
        mgr.set_enabled("var-demo", True)
        mgr.save_plugin_settings("var-demo", {"block-opacity": 0.4,
                                             "appearance_enabled": True})
        # 存完还要提交才生效（主程序里这一步由「保存并重载」触发）
        mgr.commit_settings("var-demo")
        css = mgr.theme_css()
        if "--skin-block-opacity: 0.4;" not in css:
            return False
        # 值超范围要被夹回声明上限
        mgr.save_plugin_settings("var-demo", {"block-opacity": 99})
        mgr.commit_settings("var-demo")
        return "--skin-block-opacity: 1;" in mgr.theme_css()
    finally:
        import shutil
        for base, dirs, files in os.walk(root, topdown=False):
            for f in files:
                Path(base, f).unlink()
            for dd in dirs:
                Path(base, dd).rmdir()
        root.rmdir()


def j6():
    """插件值写进 CSS 前必须收敛，堵死借变量值注入样式规则的路径。"""
    from modules.plugins import _css_value_for
    evil = _css_value_for({"type": "text"}, '1; } body { display: none } .x {')
    if "\n" in evil or "}" in evil or "{" in evil:
        return False
    # 颜色只认 hex；别的写法一律不给变量，让 theme.css 的兜底值生效
    if _css_value_for({"type": "color"}, "red") is not None:
        return False
    if _css_value_for({"type": "color"}, "url(javascript:alert(1))") is not None:
        return False
    return _css_value_for({"type": "color"}, "#ff0000") == "#ff0000"


def j7():
    """未声明的键不能靠设置接口写进磁盘（settings.json 不是任意 JSON 暂存区）。"""
    import tempfile
    from modules.plugins import PluginManager
    root = Path(tempfile.mkdtemp(prefix="lovomo_filter_"))
    try:
        d = root / "filter-demo"
        d.mkdir(parents=True)
        (d / "plugin.json").write_text(json.dumps({
            "id": "filter-demo", "name": "F", "version": "1",
            "features": [{"key": "volume", "type": "range", "min": 0, "max": 100,
                          "default": 50}],
        }), encoding="utf-8")
        mgr = PluginManager(str(root))
        clean = mgr.filter_settings("filter-demo", {
            "volume": 999, "偷偷加的键": {"任意": "结构"},
            "evil": "x", "appearance_enabled": True,
        })
        if "偷偷加的键" in clean or "evil" in clean:
            return False
        if "appearance_enabled" not in clean:
            return False
        return clean.get("volume") == 100
    finally:
        for base, dirs, files in os.walk(root, topdown=False):
            for f in files:
                Path(base, f).unlink()
            for dd in dirs:
                Path(base, dd).rmdir()
        root.rmdir()


def j8():
    """插件可以自带界面（panel.html），主程序据此换成分帧加载。"""
    for k in ("function mountPluginPanel", "function mountPluginFrame",
              "plugin-panel-frame", "panel.html_ok", "iframe",
              "window.lovomoHost = {", "contentWindow.lovomo"):
        if k not in HTML_SRC:
            return False
    j = WEBUI_SRC.find("async def handle_plugins_panel")
    if j < 0:
        return False
    if 'add_get("/api/plugins/panel"' not in WEBUI_SRC:
        return False
    # 桥接由主程序注入到页面里（插件脚本一解析就拿到 window.lovomo），
    # 而不是父窗口在 iframe 加载后事后塞进去 —— 那样插件初始化时读不到。
    return "PLUGIN_BRIDGE_TEMPLATE" in WEBUI_SRC and "_inject_plugin_bridge" in WEBUI_SRC


def j8b():
    """插件自带的 webui.html：卡片上有入口，页面是 iframe 二级页，路由也在。"""
    for k in ('id="panel-plugin-webui"', 'id="plugin-webui-host"',
              "function openPluginWebui", "function closePluginWebui",
              "function loadPluginWebuiPage", "function mountPluginWebui",
              "panelId === 'plugin-webui'"):
        if k not in HTML_SRC:
            return False
    # 入口在卡片上，紧跟在「打开功能页」右边，且只在插件真有 webui.html 时出现
    if 'data-pact="webui"' not in HTML_SRC or "p.webui_ok" not in HTML_SRC:
        return False
    if "act === 'webui'" not in HTML_SRC:
        return False
    if "async def handle_plugins_webui" not in WEBUI_SRC:
        return False
    if 'add_get("/api/plugins/webui"' not in WEBUI_SRC:
        return False
    # webui 页也要能退回插件列表
    i = HTML_SRC.find("getElementById('plugin-webui-back')")
    if i < 0 or "closePluginWebui" not in HTML_SRC[i:i + 300]:
        return False
    # 插件包里的 webui.html 由主程序按固定名字找
    from modules.plugins import WEBUI_NAME
    return WEBUI_NAME == "webui.html"


def j8c():
    """卸载要同时问「清除配置 / 清除数据」，默认都保留。"""
    for k in ('id="plugin-uninstall-modal"', 'data-uninstall-flag="settings"',
              'data-uninstall-flag="data"', "function openUninstallDialog",
              "function bindUninstallDialog", "keep_settings", "keep_data"):
        if k not in HTML_SRC:
            return False
    # 默认两个都是「否（保留）」，且卸载按钮走的是弹窗而不是 confirm
    i = HTML_SRC.find("uninstallClear = {settings: false, data: false};")
    if i < 0:
        return False
    j = HTML_SRC.find("async function doUninstallPlugin")
    return j > 0 and "confirm(" not in HTML_SRC[j:j + 400]


def j8d():
    """功能页要有「查看README」入口，卡片上不再写「有 README / 有 update」。"""
    if 'data-page-act="readme"' not in HTML_SRC:
        return False
    if "act === 'readme'" not in HTML_SRC:
        return False
    # 入口紧跟在「查看历史更新」右边
    i = HTML_SRC.find('data-page-act="history"')
    if i < 0 or 'data-page-act="readme"' not in HTML_SRC[i:i + 200]:
        return False
    # 卡片上不再提示"有 README.md / 有 update.md"
    j = HTML_SRC.find("async function loadInstalledPlugins")
    return j > 0 and "p.readme ?" not in HTML_SRC[j:j + 3000] \
        and "p.update_note ?" not in HTML_SRC[j:j + 3000]


def j8e():
    """插件页面注入桥接脚本：插在插件自己的脚本之前，且带上插件信息。"""
    class FakeMgr:
        def plugin_settings(self, pid):
            return {"accent": "#123456"}

    class FakeServer:
        plugin_manager = FakeMgr()

    html = ("<html><head><title>t</title></head><body>"
            "<script>var a = window.lovomo.id;</script></body></html>")
    out = M.WebUIServer._inject_plugin_bridge(
        FakeServer(), html.encode("utf-8"), "p1",
        {"name": "插件一", "version": "2.0", "enabled": True}).decode("utf-8")
    bridge_at = out.find("window.lovomo = api;")
    plugin_at = out.find("var a = window.lovomo.id;")
    return (0 < bridge_at < plugin_at
            and '"id": "p1"' in out
            and '"accent": "#123456"' in out
            and "/api/plugins/asset?id=" in out
            and "lovomoHost" in out)


def j8f():
    """功能页的开关按钮点一下要立刻变样，不能等「保存并重载」才变。"""
    i = HTML_SRC.find("function bindPluginFields")
    if i < 0:
        return False
    block = HTML_SRC[i:i + 1200]
    for need in ("btn.dataset.value = value ? '0' : '1'",
                 "btn.textContent = value ? '关闭' : '开启'",
                 "btn.classList.toggle('btn-gray', value)",
                 "btn.classList.toggle('btn-green', !value)"):
        if need not in block:
            return False
    return "saveField(btn.dataset.field, value, '设置已更新')" in block


def j8g():
    """市场卡片的「主页」指向插件在市场仓库里的页面，而不是清单里的 homepage。

    示例插件清单里写的主页是程序仓库，直接用 homepage 会点回主程序仓库去。
    """
    i = HTML_SRC.find("function pluginCard")
    if i < 0:
        return False
    block = HTML_SRC[i:i + 4200]
    if "const homeUrl = safeUrl(p.page_url || p.homepage || '');" not in block:
        return False
    return 'href="${esc(homeUrl)}"' in block


def j8h():
    """「插件」页卡片右上角有置顶按钮，置顶的排最前。"""
    i = HTML_SRC.find("async function loadInstalledPlugins")
    if i < 0:
        return False
    block = HTML_SRC[i:i + 4000]
    for need in ('data-pact="pin"', "data-pinned=",
                 "const ordered = plugins.slice().sort",
                 "a.pinned ? a.pin_order"):
        if need not in block:
            return False
    if "else if (act === 'pin') doPinPlugin" not in HTML_SRC:
        return False
    if "async function doPinPlugin" not in HTML_SRC:
        return False
    if "async def handle_plugins_pin" not in WEBUI_SRC:
        return False
    return 'add_post("/api/plugins/pin"' in WEBUI_SRC


def j9():
    """自带界面的 HTML 解析要防路径穿越，且文件不存在时退回自动渲染。"""
    import tempfile
    from modules.plugins import PluginManager
    root = Path(tempfile.mkdtemp(prefix="lovomo_panel_"))
    try:
        d = root / "p"
        (d / "ui").mkdir(parents=True)
        (d / "ui" / "panel.html").write_text("<h1>x</h1>", encoding="utf-8")
        mgr = PluginManager(str(root))
        if mgr.panel_html_path(d, {"html": "ui/panel.html"}) is None:
            return False
        if mgr.panel_html_path(d, {"html": "../../../etc/passwd"}) is not None:
            return False
        if mgr.panel_html_path(d, {"html": "ui/missing.html"}) is not None:
            return False
        if mgr.panel_html_path(d, {"html": "/abs/panel.html"}) is not None:
            return False
        # 非 html 后缀不接受
        return mgr.panel_html_path(d, {"html": "ui/panel.css"}) is None
    finally:
        for base, dirs, files in os.walk(root, topdown=False):
            for f in files:
                Path(base, f).unlink()
            for dd in dirs:
                Path(base, dd).rmdir()
        root.rmdir()


check("字段类型由插件声明（9 种控件）", j1)
check("旧的布尔写法向后兼容", j2)
check("未知类型退回布尔而非消失", j3)
check("skin.vars 声明任意皮肤变量", j4)
check("任意皮肤变量真的注入 CSS 且越界收敛", j5)
check("值写进 CSS 前收敛，堵死样式注入", j6)
check("未声明的键写不进 settings.json", j7)
check("插件可自带界面（panel.html + 桥接）", j8)
check("插件可自带 webui.html（卡片入口 + 二级页 + 路由）", j8b)
check("卸载可分别保留配置与数据", j8c)
check("功能页有查看README、卡片不再写有无 README", j8d)
check("插件页面注入桥接脚本（在插件脚本之前）", j8e)
check("功能页开关点了立刻变样", j8f)
check("市场「主页」指向插件自己的分支", j8g)
check("插件卡片可置顶、置顶排最前", j8h)
check("自带界面路径穿越防护与回退", j9)
check("配置页无插件快捷入口且无内置换肤", h7)


# ------------------------------------------------- I 指令分发 / 插件日志 / 目录整洁
section("I 指令分发、插件日志与目录整洁")


def i1():
    "main.py 必须真的把指令分发给插件运行时。"
    return ("rt.dispatch_command(" in WEBCOMMON_SRC
            and "_dispatch_plugin_command" in MAIN_SRC
            and "_dispatch_plugin_command(user_text" in MESSAGE_PIPELINE_SRC)


def i2():
    """指令分发要接在消息处理链路里，并且命中后要能打断后续 LLM 流程。"""
    i = MESSAGE_PIPELINE_SRC.find("_dispatch_plugin_command(user_text")
    if i < 0:
        return False
    tail = MESSAGE_PIPELINE_SRC[i:i + 700]
    return "if handled:" in tail and "return" in tail


def i3():
    """指令解析只认行首的 # / 前缀，避免正文里的井号被当成指令。"""
    import main as MM
    cases = {
        "#喵开关": ("喵开关", ""),
        "#喵开关 开": ("喵开关", "开"),
        "/hello 世界": ("hello", "世界"),
        "  #x yz  ": ("x", "yz"),
    }
    for text, want in cases.items():
        if MM._parse_plugin_command(text) != want:
            return False
    for bad in ("", "你好#喵开关", "普通聊天", "#", "#" + "x" * 40):
        if MM._parse_plugin_command(bad) is not None:
            return False
    return True


def i4():
    """插件 on_message 的返回值要真的被发出去。"""
    return ("_dispatch_plugin_message" in MAIN_SRC
            and "for extra in _dispatch_plugin_message(" in MESSAGE_PIPELINE_SRC)


def i5():
    """插件日志要并进主日志流，不再单独开一套缓冲和接口。"""
    if "plugin_log_buffer" in MAIN_SRC:
        return False
    if "/api/plugins/logs" in MAIN_SRC:
        return False
    i = WEBCOMMON_SRC.find("def _plugin_log(")
    if i < 0:
        return False
    body = WEBCOMMON_SRC[i:i + 500]
    # 只 print 进主日志（StdoutRedirector 会把它收进 global_log_buffer）
    return "print(" in body and "[插件]" in body


def i6():
    """插件运行时的 logger 必须接到 _plugin_log（否则插件日志进不了缓冲）。"""
    i = MAIN_SRC.find("_plugin_rt = PluginRuntime(")
    if i < 0:
        return False
    return "logger=_plugin_log" in MAIN_SRC[i:i + 300]


def i7():
    "日志缓冲写入必须去掉行尾的回车换行。"
    # StdoutRedirector 的写入路径已拆到 modules/log_console.py，守卫随之指向新位置
    i = LOG_CONSOLE_SRC.find("global_log_buffer.append(")
    if i < 0:
        return False
    line = LOG_CONSOLE_SRC[i:LOG_CONSOLE_SRC.find("\n", i)]
    return "rstrip('\\r\\n')" in line or 'rstrip("\\r\\n")' in line


def i8():
    """插件管理器启动时要清掉残留的安装临时目录。"""
    from modules.plugins import PluginManager
    import inspect as _ins
    return ("purge_temp_dirs" in MAIN_SRC or True) and \
        "purge_temp_dirs" in _ins.getsource(PluginManager.__init__) and \
        "purge_temp_dirs" in _ins.getsource(PluginManager)


def i9():
    """临时目录清理要能认出三种前缀，并且真的删掉它们。"""
    import tempfile, shutil as _sh
    from modules.plugins import PluginManager
    root = Path(tempfile.mkdtemp(prefix="plug-purge-"))
    try:
        for name in (".backup-demo-1", ".staging-demo-2", ".keep-demo-3"):
            d = root / name
            d.mkdir()
            (d / "x.txt").write_text("x", encoding="utf-8")
        (root / "real-plugin").mkdir()
        pm = PluginManager(root, on_change=lambda: None)
        names = sorted(p.name for p in root.iterdir())
        return names == ["real-plugin"] and pm is not None
    finally:
        _sh.rmtree(root, ignore_errors=True)


def i10():
    """删除目录要有重试：Windows 上文件被短暂占住会直接拒绝删除。"""
    from modules.plugins import _rmtree_retry
    return callable(_rmtree_retry)


def i11():
    """插件清单的 panel 段要被规范化（插件自带功能页的声明）。"""
    from modules.plugins import _normalize_panel
    if _normalize_panel({"label": "喵喵设置", "title": "标题"}) != \
            {"label": "喵喵设置", "title": "标题"}:
        return False
    return _normalize_panel({"title": "只有标题"}) == {} and \
        _normalize_panel("字符串") == {} and _normalize_panel(None) == {}


def i12():
    """已装插件在插件市场面板里直接成页，不再用弹窗。"""
    for k in ("plugin-list-body", "loadInstalledPlugins", "plugin-page-body",
              "renderPluginPage", "refreshPluginViews"):
        if k not in HTML_SRC:
            return False
    if "plugin-list-modal" in HTML_SRC:
        return False
    # 插件日志已并进「日志输出」页，功能页里不该再有独立的日志区
    return "plugin-log-content" not in HTML_SRC


def i12d():
    "「插件市场」与「插件」是两个互不混杂的面板。"
    i_mkt = HTML_SRC.find('id="panel-plugins"')
    i_inst = HTML_SRC.find('id="panel-installed-plugins"')
    i_page = HTML_SRC.find('id="panel-plugin-page"')
    if not (-1 < i_mkt < i_inst < i_page):
        return False
    market = HTML_SRC[i_mkt:i_inst]
    installed = HTML_SRC[i_inst:i_page]

    # 市场页：有装插件入口，没有已装列表
    for need in ("plugin-upload-btn", "plugin-open-dir-btn",
                 "plugin-market-refresh-btn", "plugin-market-list"):
        if need not in market:
            return False
    if "plugin-list-body" in market:
        return False

    # 插件页：有已装列表，没有装插件入口
    if "plugin-list-body" not in installed:
        return False
    for bad in ("plugin-upload-btn", "plugin-market-refresh-btn", "plugin-upload-file"):
        if bad in installed:
            return False

    # 功能页返回时要回到「插件」页，不是市场页
    k = HTML_SRC.find("function closePluginPage()")
    if k < 0:
        return False
    return "switchPanel('installed-plugins')" in HTML_SRC[k:k + 900]


def i12c():
    "用户能点到的面板必须在左侧栏有导航项（二级页显式豁免）。"
    import re
    # 二级页面：由插件卡片点进去（功能页 / 历史更新页）、由左上角
    # 「Lovomo + 版本号」点进去（程序历史版本），都不在侧边栏。
    # 豁免它们的前提是**入口真的存在** —— i12e / i12f 专门检查这一点。
    secondary = {"plugin-page", "plugin-history", "plugin-webui", "app-releases"}
    panels = set(re.findall(r'id="panel-([a-z-]+)"', HTML_SRC)) - secondary
    navs = set(re.findall(r'class="menu-item[^"]*"\s+data-panel="([a-z-]+)"', HTML_SRC))
    missing = sorted(p for p in panels if p not in navs)
    if missing:
        print("      侧边栏没有入口的面板:", missing)
        return False
    return True


def i12e():
    "二级页要有真实入口：功能页顶部的「查看历史更新」按钮。"
    if 'id="panel-plugin-history"' not in HTML_SRC:
        return False
    for need in ("function openPluginHistory",
                 "function loadPluginHistoryPage",
                 "switchPanel('plugin-history')"):
        if need not in HTML_SRC:
            return False
    # 入口在插件功能页的顶部操作条里，紧跟在「返回插件列表」之后
    i = HTML_SRC.find('data-page-act="back"')
    if i < 0 or 'data-page-act="history"' not in HTML_SRC[i:i + 300]:
        return False
    if "act === 'history'" not in HTML_SRC:
        return False
    # 「插件」列表的卡片上不再放这个入口
    j = HTML_SRC.find("async function loadInstalledPlugins")
    if j < 0 or 'data-pact="history"' in HTML_SRC[j:j + 3000]:
        return False
    # 切页时要记得加载这一页的数据（漏了就是"打开一片空白"）
    i = HTML_SRC.find("panelId === 'installed-plugins'")
    if i < 0 or "panelId === 'plugin-history'" not in HTML_SRC[i:i + 400]:
        return False
    # 返回按钮要能回到「插件」页
    j = HTML_SRC.find("getElementById('plugin-history-back')")
    return j > 0 and "installed-plugins" in HTML_SRC[j:j + 300]


def i12f():
    "程序历史版本页要有真实入口：左上角「Lovomo + 版本号」，且不在插件历史页里。"
    for need in ('id="panel-app-releases"', 'id="logo-btn"',
                 "switchPanel('app-releases')"):
        if need not in HTML_SRC:
            return False
    # 点 logo 必须真的绑上切页，而不是只有个 id
    i = HTML_SRC.find("getElementById('logo-btn')")
    if i < 0 or "switchPanel('app-releases')" not in HTML_SRC[i:i + 300]:
        return False
    # 切页时要记得拉 Releases（漏了就是"打开一片空白"）
    if "if (panelId === 'app-releases') loadReleases(false);" not in HTML_SRC:
        return False
    # 插件历史页里不能再有项目历史版本那一节
    i = HTML_SRC.find('id="panel-plugin-history"')
    j = HTML_SRC.find('id="panel-app-releases"')
    if i < 0 or j < i:
        return False
    return "程序历史版本" not in HTML_SRC[i:j]


def i12b():
    "市场页的上传/目录/刷新三个按钮都要真的绑上事件。"
    for k in ("plugin-upload-btn", "plugin-open-dir-btn",
              "plugin-market-refresh-btn"):
        i = HTML_SRC.find(f"getElementById('{k}')")
        if i < 0:
            return False
        seg = HTML_SRC[i:i + 700]
        if "addEventListener" not in seg:
            return False
    if "function doUploadPlugin" not in HTML_SRC:
        return False
    # 上传必须两步走：先 inspect 拿风险报告，再 upload
    i = HTML_SRC.find("function doUploadPlugin")
    body = HTML_SRC[i:i + 1600]
    return "api/plugins/inspect" in body and "api/plugins/upload" in body


def i13():
    """点插件卡片空白处要打开功能页（用户明确要求）。"""
    i = HTML_SRC.find('.plugin-card[data-pid]')
    if i < 0:
        return False
    tail = HTML_SRC[i:i + 600]
    return "openPluginPage(" in tail


def i14():
    """外观默认不生效：只有开了 appearance_enabled 才加 skin-on。"""
    from modules.plugins import PluginManager
    import inspect as _ins
    return ("active_skin_ids" in _ins.getsource(PluginManager)
            and "appearance_enabled" in _ins.getsource(PluginManager))


def i15():
    "theme.css 改底色的规则必须挂在 html.skin-on 下。"
    css = (ROOT / "plugins" / "sources" / "meow-skin" / "theme.css").read_text(
        encoding="utf-8")
    if "html.skin-on" not in css:
        return False
    # 不允许出现裸的 body/html 底色覆盖
    for m in re.finditer(r"^(html|body)\s*\{([^}]*)\}", css, re.M):
        if "background" in m.group(2):
            return False
    return True


def i16():
    """主日志页的尾部空白要被去掉（防止隐藏内容"露出来"）。"""
    i = HTML_SRC.find("const fetchLogs = async ()")
    if i < 0:
        return False
    tail = HTML_SRC[i:i + 1400]
    return "replace(/\\s+$/" in tail


def i17():
    """前端要按 active_skins 切换 skin-on 类。"""
    return ("refreshSkinOnState" in HTML_SRC
            and "active_skins" in HTML_SRC
            and "classList.toggle('skin-on'" in HTML_SRC)


def i18():
    "功能页不能引用不存在的前端函数。"
    i = HTML_SRC.find("async function renderPluginPage")
    if i < 0:
        return False
    tail = HTML_SRC[i:i + 4000]
    if "skinMeta(" in tail:
        return False
    # 皮肤能力直接读接口下发的 p.skin
    return "p.skin || {}" in tail


def i19():
    """功能页里用到的自定义前端函数都必须在页面里定义过。"""
    names = set(re.findall(r"\b([a-zA-Z_$][\w$]*)\s*\(", HTML_SRC))
    defined = set(re.findall(r"function\s+([a-zA-Z_$][\w$]*)", HTML_SRC))
    # 只检查插件相关的辅助函数，避免把浏览器内置 API 也算进来
    used = {n for n in names if n.startswith("plugin") or n == "fetchPlugins"}
    return used <= defined


def k1():
    """插件设置改动不能实时生效：功能页里不许直接提交设置。"""
    i = HTML_SRC.find("function bindPluginPageActions")
    j = HTML_SRC.find("function bindPluginFields")
    if i < 0 or j < i:
        return False
    block = HTML_SRC[i:j]
    # 旧实现有个 saveAndReload()，会在每次控件 change 时立刻 POST settings
    if "saveAndReload" in block:
        return False
    if "api/plugins/settings" in block:
        return False
    # 改动必须走暂存
    return "stagePluginSetting(" in block


def k2():
    """右下角要有固定的「保存并重载」入口，且切到功能页才显示。"""
    if 'id="plugin-apply-btn"' not in HTML_SRC:
        return False
    if "保存并重载" not in HTML_SRC:
        return False
    if ".plugin-apply-bar" not in HTML_SRC:
        return False
    i = HTML_SRC.find("function updatePluginApplyState")
    if i < 0:
        return False
    return "activePanelId === 'plugin-page'" in HTML_SRC[i:i + 800]


def k3():
    "点「保存并重载」要落盘设置并重载插件运行时。"
    i = HTML_SRC.find("async function applyPluginSettings")
    if i < 0:
        return False
    block = HTML_SRC[i:i + 1600]
    for k in ("api/plugins/settings", "api/plugins/reload", "applyPluginTheme()"):
        if k not in block:
            return False
    return ("pluginDraft = null" in block and "pluginApplied = Object.assign" in block)


def k4():
    """后端必须有 reload 路由，且设置接口本身不再顺带重载。"""
    if 'add_post("/api/plugins/reload"' not in WEBUI_SRC:
        return False
    if '"/api/plugins/reload"' not in WEBUI_SRC:
        return False
    if "async def handle_plugins_reload" not in WEBUI_SRC:
        return False
    i = WEBUI_SRC.find("async def handle_plugins_settings_set")
    j = WEBUI_SRC.find("async def handle_plugins_reload")
    if i < 0 or j < i:
        return False
    # 设置接口只落盘，不该自己触发 _on_plugins_changed
    return "_on_plugins_changed" not in WEBUI_SRC[i:j]


def k7():
    "皮肤数值变量必须带清单声明的长度单位。"
    from modules.plugins import _css_value_for, CSS_LENGTH_UNITS
    radius = {"type": "range", "min": 0, "max": 28, "step": 1,
              "default": 14, "unit": "px"}
    if _css_value_for(radius, 28) != "28px":
        return False
    if _css_value_for(radius, 0) != "0px":
        return False
    # 非长度单位不写进 CSS（否则 3次 这种值会让声明整条失效）
    times = {"type": "range", "min": 1, "max": 5, "step": 1, "unit": "次"}
    if _css_value_for(times, 3) != "3":
        return False
    # 没声明 unit 的（如透明度）保持裸数字
    alpha = {"type": "range", "min": 0.2, "max": 1, "step": 0.01}
    if _css_value_for(alpha, 0.3) != "0.3":
        return False
    # unit 不能用来往样式表里塞东西
    evil = {"type": "range", "min": 0, "max": 10, "step": 1,
            "unit": "px; } body { display: none } /*"}
    if _css_value_for(evil, 2) != "2":
        return False
    # 清单里 radius 声明了 px：生成的变量块必须带单位
    pm = fresh_pm("k7")
    pkg = ROOT / "plugins" / "packages" / "meow-skin.zip"
    if not pkg.exists():
        return False
    if not install(pm, pkg.read_bytes(), force=True).get("success"):
        return False
    pm.commit_settings("meow-skin")
    css = pm.theme_css()
    m = re.search(r"--skin-radius:\s*([^;]+);", css)
    if not m or not m.group(1).strip().endswith("px"):
        return False
    for u in CSS_LENGTH_UNITS:
        if not re.match(r"^[a-z%]+$", u):
            return False
    return True


def k8():
    "主色必须能存住并生效。"
    pm = fresh_pm("k8")
    pkg = ROOT / "plugins" / "packages" / "meow-skin.zip"
    if not install(pm, pkg.read_bytes(), force=True).get("success"):
        return False
    # accent 必须能过白名单
    clean = pm.filter_settings("meow-skin", {"accent": "#00c853"})
    if clean.get("accent") != "#00c853":
        return False
    # 非法颜色不能被写进去
    bad = pm.filter_settings("meow-skin", {"accent": "red; } body { display:none"})
    if "accent" in bad:
        return False
    # 存盘 + 提交后，注入的 CSS 必须用新色而不是清单默认色
    pm.save_plugin_settings("meow-skin", {"accent": "#00c853"})
    pm.commit_settings("meow-skin")
    css = pm.theme_css()
    if "--skin-accent: #00c853;" not in css:
        return False
    if "--skin-accent: #7c4dff;" in css:
        return False
    return True


def k9():
    "取色器不能被输入框规则撑成一条横线。"
    page = (ROOT / "webui" / "start.html").read_text(encoding="utf-8")
    if not re.search(r"input\[type=\"color\"\]\s*\{[^}]*width:\s*\d+px\s*!important",
                      page):
        return False
    if not re.search(r"input\[type=\"color\"\]\s*\{[^}]*padding:\s*\d+px\s*!important",
                      page):
        return False
    if "::-webkit-color-swatch" not in page:
        return False
    # 皮肤里也必须单独给取色器定尺寸，否则 !important 的通用规则会盖回去
    css = (ROOT / "plugins" / "sources" / "meow-skin" / "theme.css").read_text(
        encoding="utf-8")
    if not re.search(r"input\[type=\"color\"\]\s*\{[^}]*width:\s*\d+px\s*!important",
                      css):
        return False
    if not re.search(r"input\[type=\"color\"\]\s*\{[^}]*padding:\s*2px\s*!important",
                      css):
        return False
    return True


def k10():
    "区块透明度要覆盖日志区、输入框、按钮，且嵌套层比外层浅。"
    css = (ROOT / "plugins" / "sources" / "meow-skin" / "theme.css").read_text(
        encoding="utf-8")
    # 不能再有写死的白底（复选框除外：原生勾选框留白才看得清勾）
    for _m in re.finditer(r"([^{}]+)\{[^}]*background:\s*(#fff|white|#ffffff)\s*!important", css):
        if "checkbox" not in _m.group(1):
            return False
    # 三种层级都要有
    for var in ("--meow-surface:", "--meow-surface-soft:", "--meow-surface-tint:"):
        if var not in css:
            return False
    # 日志区必须吃透明度变量（原来写死 #1e1e1e）
    if not re.search(r"\.log-container\s*\{[^}]*color-mix\([^}]*--meow-alpha", css):
        return False
    # 嵌套容器（内联表单）要归到 soft 档：先找到 soft 档那条规则，
    # 再看它的选择器列表里有没有 .inline-form。
    m = re.search(r"^([^@{}]*)\{[^}]*var\(--meow-surface-soft\)", css, re.M)
    if not m or ".inline-form" not in m.group(1):
        return False
    # 嵌套层 alpha 必须小于外层：78% < 100%
    if "--meow-surface-soft: color-mix(in srgb, var(--meow-base) calc(var(--meow-alpha) * 78%), transparent)" not in css:
        return False
    return True


def k5():
    "theme.css 必须真的消费清单声明的透明度与圆角变量。"
    css = (ROOT / "plugins" / "sources" / "meow-skin" / "theme.css").read_text(
        encoding="utf-8")
    if "--skin-radius" not in css:
        return False
    if "--skin-block-opacity" not in css:
        return False
    # 圆角变量要真的用在 border-radius 上，不能只是声明
    if not re.search(r"border-radius:[^;]*var\(--meow-radius", css):
        return False
    # 成块的卡片要成批消费透明度：一条组合选择器统一挂底色，
    # 而不是给每个组件各写一遍写死的白色
    if not re.search(r"\.section-block[\s\S]{0,400}?var\(--meow-surface\)", css):
        return False
    if css.count("background: #fff !important") > 1:
        return False
    # 清单声明的变量名必须与 CSS 引用的一致（改名不同步是这类 bug 的根源）
    from modules.plugins import _read_manifest_from_dir
    manifest = _read_manifest_from_dir(ROOT / "plugins" / "sources" / "meow-skin")
    declared = {v["name"] for v in (manifest.get("skin") or {}).get("vars") or []}
    for name in declared:
        if f"--skin-{name}" not in css:
            return False
    return True


def k6():
    "透明度要作用于底色，而不是元素整体 opacity。"
    css = (ROOT / "plugins" / "sources" / "meow-skin" / "theme.css").read_text(
        encoding="utf-8")
    i = css.find("--meow-surface:")
    if i < 0:
        return False
    decl = css[i:css.find(";", i)]
    if "color-mix(" not in decl and "rgba(" not in decl:
        return False
    # 不许出现把整块元素 opacity 调成变量值的规则（那会连字一起变淡）
    for m in re.finditer(r"^\s*opacity:\s*var\(--skin-block-opacity", css, re.M):
        return False
    return True


def k11():
    "透明度要覆盖聊天记录/表格/弹窗/统计等全部面板。"
    css = (ROOT / "plugins" / "sources" / "meow-skin" / "theme.css").read_text(
        encoding="utf-8")

    def consumes(selector: str, var: str) -> bool:
        """该选择器所在的那条规则里是否用了指定变量。"""
        for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
            sel = m.group(1)
            if selector not in sel:
                continue
            if var in m.group(2):
                return True
        return False

    # ---- 最外层（surface 档）必须是这一整串面 ----
    for sel in (".section-block", ".config-group", ".stat-card", ".chart-box",
                ".mood-row", ".emotion-card", ".file-item", ".tutorial",
                ".modal-content", ".plugin-card", ".plugin-panel-frame",
                ".risk-box", ".sticker-thumb", ".role-item", ".perf-item",
                ".anchor-nav"):
        if not consumes(sel, "var(--meow-surface)"):
            return False
    # 通用数据表（定时任务 / 事件 / 待办 / 工具 / 画像 / RAG 全用它）
    if not consumes(".data-table", "var(--meow-surface)"):
        return False
    if not consumes(".data-table th", "var(--meow-surface-tint)"):
        return False
    if not consumes(".data-table tr:hover td", "var(--meow-surface-tint)"):
        return False
    # ---- 嵌在里面的面（soft 档）----
    for sel in (".inline-form", ".editor-form", ".modal-tools",
                ".chat-list-header button", ".chat-list-header select",
                ".tutorial code"):
        if not consumes(sel, "var(--meow-surface-soft)"):
            return False
    # 输入框：程序里写的 input/textarea/select 都要过一遍
    for sel in ('input[type="text"]', 'input[type="password"]', "textarea",
                "select"):
        if not consumes(sel, "var(--meow-surface-soft)"):
            return False
    # 日志区
    if not consumes(".log-container", "var(--meow-alpha)"):
        return False
    # ---- 带色相的面：必须用 color-mix 保留原色，且跟着同一个设置值 ----
    for sel, hue in ((".badge.green", "#e8f5e9"),
                     (".msg-row.user .msg-bubble", "#95ec69"),
                     (".file-item.selected", "#e3f2fd"),
                     (".enable-card.checked", "#f1f9f2"),
                     (".message.success", "#d4edda"),
                     (".risk-high", "#ffebee")):
        if not re.search(re.escape(sel) + r"\s*\{[^}]*background:\s*color-mix\([^}]*"
                         + re.escape(hue), css):
            return False
        if not consumes(sel, "var(--meow-a)"):
            return False
    # ---- 白字压重色的那几个要留下限，否则调太透时字看不清 ----
    if "--meow-a-deep: calc(max(var(--meow-alpha), 0.55) * 100%)" not in css:
        return False
    for sel in (".btn-red", ".btn-orange", ".modal-header"):
        if not consumes(sel, "var(--meow-a-deep)"):
            return False
    return True


# ============================================================ L 资源名 / 图标
def l1():
    "非 ASCII 文件名必须原样保留。"
    from modules.plugins import safe_asset_name
    cases = {
        "猫咪背景.png": "猫咪背景.png",
        "背景 图 (1).jpg": "背景 图 (1).jpg",
        "café_äü.png": "café_äü.png",
        "a/b\\c.png": "c.png",            # 只取文件名
        "x<y>z.png": "x_y_z.png",         # 非法字符才替换
        "con.png": "_con.png",            # Windows 设备名要避开
        "尾部点... .png": "尾部点.png",
        "": None,                         # 空名走兜底，见下
    }
    for raw, want in cases.items():
        got = safe_asset_name(raw, ".png")
        if want is None:
            if not got.startswith("asset_") or not got.endswith(".png"):
                print("      兜底失败:", raw, got)
                return False
            continue
        if got != want:
            print(f"      {raw!r} -> {got!r}（期望 {want!r}）")
            return False
    return True


def l2():
    """重名自动编号，不覆盖已有文件。"""
    import tempfile
    from modules.plugins import unique_asset_name
    with tempfile.TemporaryDirectory() as tmp:
        first = unique_asset_name(tmp, "背景.png")
        (Path(tmp) / first).write_bytes(b"x")
        second = unique_asset_name(tmp, "背景.png")
        (Path(tmp) / second).write_bytes(b"x")
        third = unique_asset_name(tmp, "背景.png")
        return (first, second, third) == ("背景.png", "背景-2.png", "背景-3.png")


def l3():
    """背景图 URL 要接受中文名（并做百分号编码），但仍然挡住注入。"""
    from modules.plugins import _sanitize_background_url
    ok = _sanitize_background_url("猫咪 背景.png")
    if not ok.startswith("/api/plugins/asset?name=") or "%" not in ok:
        print("      中文名没被放行:", ok)
        return False
    if " " in ok:
        print("      空格没有编码:", ok)
        return False
    bad = ["javascript:alert(1)", "../../etc/passwd", "a.php", "",
           "x\"; url(evil)", "sub/dir.png", "..\\x.png", ".hidden.png"]
    for value in bad:
        if _sanitize_background_url(value):
            print("      不该放行:", value, "->", _sanitize_background_url(value))
            return False
    # 站内相对路径照旧放行
    return _sanitize_background_url("/api/plugins/asset?name=x.png").endswith("x.png")


def l4():
    """handle_pick_file 不能再整串替换成下划线。"""
    body = method_src("handle_pick_file")
    if not body:
        return False
    if 're.sub(r"[^A-Za-z0-9_.\\-]", "_"' in body:
        return False
    return "safe_asset_name(" in body and "unique_asset_name(" in body


def l5():
    """图标只认 logo.<主流格式>，并兼容老的 icon.*；都没有就是首字图标。"""
    import tempfile
    from modules.plugins import PluginManager
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cases = [("a", "logo.png"), ("b", "logo.webp"), ("c", "logo.svg"),
                 ("d", "icon.png"), ("e", "logo.gif")]
        for pid, icon in cases:
            d = root / pid
            d.mkdir(parents=True)
            (d / "plugin.json").write_text(
                json.dumps({"id": pid, "name": pid, "version": "1"}), encoding="utf-8")
            (d / icon).write_bytes(b"\x89PNG")
        # 没有图标的插件
        (root / "noicon").mkdir()
        (root / "noicon" / "plugin.json").write_text(
            json.dumps({"id": "noicon", "name": "noicon", "version": "1"}),
            encoding="utf-8")
        # 不认识的格式不算图标
        (root / "weird").mkdir()
        (root / "weird" / "plugin.json").write_text(
            json.dumps({"id": "weird", "name": "weird", "version": "1"}),
            encoding="utf-8")
        (root / "weird" / "logo.txt").write_bytes(b"nope")
        pm = PluginManager(root)
        infos = {p["id"]: p for p in pm.list_plugins()}
        for pid, icon in cases:
            if not infos[pid]["has_icon"] or infos[pid]["icon"] != icon:
                print("      图标识别失败:", pid, infos[pid].get("icon"))
                return False
        if infos["noicon"]["has_icon"] or infos["noicon"]["icon"]:
            return False
        if infos["weird"]["has_icon"]:
            return False
        return pm.icon_file("a").name == "logo.png" and pm.icon_file("noicon") is None


def l6():
    """说明文档：readme 与 update 都要能被读到（大小写不敏感、可截断）。"""
    import tempfile
    from modules.plugins import PluginManager
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        d = root / "doc"
        d.mkdir(parents=True)
        (d / "plugin.json").write_text(
            json.dumps({"id": "doc", "name": "doc", "version": "1"}), encoding="utf-8")
        (d / "README.md").write_text("# 说明\n中文内容", encoding="utf-8")
        (d / "update.TXT").write_text("v1.1 更新内容", encoding="utf-8")
        (root / "nodoc").mkdir()
        (root / "nodoc" / "plugin.json").write_text(
            json.dumps({"id": "nodoc", "name": "nodoc", "version": "1"}), encoding="utf-8")
        pm = PluginManager(root)
        readme = pm.read_doc_text("doc", "readme")
        update = pm.read_doc_text("doc", "update")
        if not (readme["found"] and readme["name"] == "README.md"
                and "中文内容" in readme["text"]):
            print("      readme 读取失败:", readme)
            return False
        if not (update["found"] and "更新内容" in update["text"]):
            print("      update 读取失败:", update)
            return False
        if pm.read_doc_text("nodoc", "readme")["found"]:
            return False
        if pm.read_doc_text("doc", "readme", limit=3)["truncated"] is not True:
            return False
        # 列表面板要知道有没有这两份文档（有才提示）
        info = pm.get("doc")
        return info.get("readme") == "README.md" and info.get("update_note") == "update.TXT"


# ============================================================ M 在线市场（分支）
def m1():
    """只认前缀匹配的分支，且前缀后面必须有东西。"""
    from modules.market import matches_branch_prefix as hit
    cases = [("lovomo_plugin_meow", "lovomo_plugin", True),
             ("Lovomo_Plugin_Cat", "lovomo_plugin", True),
             ("lovomo_plugin", "lovomo_plugin", False),
             ("main", "lovomo_plugin", False),
             ("lovomo_other", "lovomo_plugin", False),
             ("lovomo_plugin_x", "", True)]
    for name, prefix, want in cases:
        if hit(name, prefix) is not want:
            print("      判断错误:", name, prefix, hit(name, prefix))
            return False
    return True


def m2():
    """分支名 → 插件 id。"""
    from modules.market import plugin_id_from_branch as f
    return (f("lovomo_plugin_meow", "lovomo_plugin") == "meow"
            and f("lovomo_plugin-meow", "lovomo_plugin") == "meow"
            and f("lovomo_plugin", "lovomo_plugin") == "lovomo_plugin")


def _rel(tag, commitish, downloads, assets_zip=None, published="2026-09-17T12:00:00Z",
         reactions=0, draft=False):
    assets = []
    if assets_zip:
        assets.append({"name": "p.zip", "browser_download_url": assets_zip,
                       "download_count": downloads})
    else:
        assets.append({"name": "p.tar", "download_count": downloads})
    return {"tag_name": tag, "target_commitish": commitish, "draft": draft,
            "published_at": published, "assets": assets, "_reactions": reactions}


def m3():
    """Release 归属：target_commitish、tag 带分支名、tag 以插件 id 开头都算。"""
    from modules.market import release_belongs_to as f
    return (f(_rel("v1", "lovomo_plugin_meow", 1), "lovomo_plugin_meow", "meow") is True
            and f(_rel("lovomo_plugin_meow-v1", "main", 1), "lovomo_plugin_meow", "meow") is True
            and f(_rel("meow-1.0.0", "main", 1), "lovomo_plugin_meow", "meow") is True
            and f(_rel("v9", "main", 1), "lovomo_plugin_meow", "meow") is False)


def m4():
    """下载量 = 该分支所有 Release 资源下载数之和；收藏量 = 点赞数之和。"""
    from modules.market import summarize_releases
    rels = [_rel("lovomo_plugin_meow-v1", "lovomo_plugin_meow", 7, reactions=3),
            _rel("meow-1.0", "main", 5, published="2026-08-01T00:00:00Z",
                 assets_zip="https://github.com/x/y/releases/download/v1/p.zip"),
            _rel("v9", "main", 100),                     # 与插件无关，不能算进来
            _rel("draft1", "lovomo_plugin_meow", 999, draft=True)]
    stats = summarize_releases(rels, {"lovomo_plugin_meow": {"id": "meow"}})
    got = stats["lovomo_plugin_meow"]
    ok = (got["downloads"] == 12 and got["favorites"] == 3
          and got["releases"] == 2 and got["release_tag"] == "lovomo_plugin_meow-v1"
          and got["asset_url"] == "https://github.com/x/y/releases/download/v1/p.zip")
    if not ok:
        print("      统计结果:", got)
    return ok


def m5():
    """市场条目：优先用 Release 里的 zip 资源，其次才是分支归档。"""
    from modules.market import build_entry, summarize_releases
    stats = summarize_releases(
        [_rel("t1", "lovomo_plugin_a", 3,
              assets_zip="https://github.com/o/r/releases/download/t1/a.zip")],
        {"lovomo_plugin_a": {"id": "a"}})["lovomo_plugin_a"]
    entry = build_entry("lovomo_plugin_a", "lovomo_plugin",
                        {"id": "a", "name": "甲", "version": "2"}, "", "o/r", stats)
    if entry["download_kind"] != "asset" or not entry["download"].endswith("a.zip"):
        print("      没优先用 Release 资源:", entry)
        return False
    no_release = build_entry("lovomo_plugin_a", "lovomo_plugin", {"id": "a"}, "",
                             "o/r", {})
    if "codeload.github.com" not in no_release["download"]:
        return False
    return (no_release["logo_url"].endswith("/logo.png")
            and no_release["branch"] == "lovomo_plugin_a"
            and entry["downloads"] == 3 and entry["favorites"] == 0)


def m6():
    """三种排序都要真的按对应字段排。"""
    from modules.market import sort_entries
    data = [{"id": "a", "name": "a", "downloads": 1, "favorites": 9, "updated_at": 30},
            {"id": "b", "name": "b", "downloads": 9, "favorites": 1, "updated_at": 10},
            {"id": "c", "name": "c", "downloads": 5, "favorites": 5, "updated_at": 20}]
    latest = [x["id"] for x in sort_entries(data, "latest")]
    dl = [x["id"] for x in sort_entries(data, "downloads")]
    fav = [x["id"] for x in sort_entries(data, "favorites")]
    if latest != ["a", "c", "b"] or dl != ["b", "c", "a"] or fav != ["a", "c", "b"]:
        print("      排序结果:", latest, dl, fav)
        return False
    # 未知排序键退回"最新"，不能把列表打乱
    return [x["id"] for x in sort_entries(data, "??")] == latest


def m7():
    """reactions 接口返回列表，收藏量按条数算。"""
    from modules.market import count_reactions
    return (count_reactions([{"content": "+1"}, {"content": "heart"}]) == 2
            and count_reactions({}) == 0 and count_reactions(None) == 0)


def m8():
    """后端确实接上了分支扫描与新路由。"""
    src = WEBUI_SRC
    for need in ("_market_branch_entries", "_market_index_entries",
                 "_market_raw_text", "_market_manifest", "plugin_market_branch_prefix",
                 "MAX_REACTION_LOOKUPS", "reactions?per_page=100",
                 '/api/plugins/readme', '/api/releases',
                 '/api/releases/download'):
        if need not in src:
            print("      缺少:", need)
            return False
    return "handle_plugins_readme" in src and "handle_releases" in src


def m9():
    """市场缓存的是未排序条目：换排序不该再打一次网络。"""
    src = WEBUI_SRC
    i = src.find("async def _plugins_market_payload")
    j = src.find("\n    async def ", i + 1)
    body = src[i:j if j > 0 else len(src)]
    return ('cache.get("entries")' in body and "sort_entries(plugins, sort_key)" in body
            and "sort_key not in SORT_KEYS" in body)


def m10():
    """市场面板：排序下拉三个选项 + 卡片展示下载量/收藏量。"""
    for need in ('id="plugin-market-sort"', 'value="latest"', 'value="downloads"',
                 'value="favorites"', "plugin-market-sort'", "sort=${encodeURIComponent(sort)}"):
        if need not in HTML_SRC:
            print("      前端缺少:", need)
            return False
    i = HTML_SRC.find("const metrics = o.market ?")
    seg = HTML_SRC[i:i + 600]
    return "下载" in seg and "收藏" in seg and "p.downloads" in seg and "p.favorites" in seg


def m11():
    """安装完必须弹 readme 弹窗（两个安装入口都要接）。"""
    if "function showPluginReadme" not in HTML_SRC:
        return False
    if 'id="plugin-doc-modal"' not in HTML_SRC or "function showPluginDoc" not in HTML_SRC:
        return False
    if "api/plugins/readme?id=" not in HTML_SRC:
        return False
    # 上传安装与市场安装两条路径各要调一次
    return HTML_SRC.count("showPluginReadme(") >= 3


def m12():
    """插件更新说明页：update 文档；程序 Releases 列表与下载按钮在「程序历史版本」页。"""
    for need in ("function openPluginHistory", "function loadReleases",
                 "function downloadRelease", "api/releases", "/api/releases/download",
                 "plugin-history-update", "plugin-releases"):
        if need not in HTML_SRC:
            print("      前端缺少:", need)
            return False
    # 桌面窗口里不能靠前端自己下载，必须走服务端保存
    return "Content-Disposition" in HTML_SRC and "data-release-url" in HTML_SRC


def m13():
    """配置项三处同步：default_config + configGroups + configMeta；内部选项不露给用户。"""
    from main import ConfigLoader
    defaults = ConfigLoader.default_config()
    for key in ("plugin_market_repo", "plugin_release_repo", "log_max_size_mb",
                "github_mirrors", "plugin_market_branch_prefix",
                "plugin_market_sources_path", "plugin_market_path"):
        if key not in defaults:
            print("      不在 default_config:", key)
            return False
    # 声明了却不在任何分组里 = 用户根本看不到这一项
    i = HTML_SRC.find("const configGroups")
    j = HTML_SRC.find("const configMeta")
    if i < 0 or j < i:
        return False
    groups = HTML_SRC[i:j]
    # 插件市场走哪个仓库、索引与来源清单属于内部实现，配置页不再暴露；
    # 它们仍然留在 default_config 与 configMeta 里，靠 config.json 手工改
    for key in ("plugin_market_repo", "plugin_market_branch_prefix",
                "plugin_market_thirdparty", "plugin_market_sources_path",
                "plugin_market_path", "plugin_release_repo"):
        if f"'{key}'" in groups:
            print("      不该让用户改:", key)
            return False
    for key in ("log_max_size_mb", "github_mirrors"):
        if key not in groups or f"'{key}'" not in HTML_SRC:
            print("      前端没同步:", key)
            return False
    return (defaults["plugin_market_branch_prefix"] == "lovomo_plugin"
            and "gh-proxy.com" in defaults["github_mirrors"])


def m15():
    """镜像模板解析：raw 与 api 都走镜像（api 直连会吃光匿名配额）。"""
    from modules.ghmirror import DEFAULT_MIRRORS, candidates, parse_mirrors
    parsed = parse_mirrors("\n".join(DEFAULT_MIRRORS) + "\n\n# 注释\n不是地址\n"
                           "https://a.example/{url}\nhttps://a.example/{url}\n")
    if parsed != tuple(DEFAULT_MIRRORS) + ("https://a.example/{url}",):
        print("      解析结果不对:", parsed)
        return False
    raw = "https://raw.githubusercontent.com/me/repo/main/a/b.txt"
    got = candidates(raw, ("https://gh-proxy.com/{url}",
                           "https://cdn.jsdelivr.net/gh/{repo}@{ref}/{path}"))
    api = candidates("https://api.github.com/repos/me/repo/branches",
                     ("https://gh-proxy.com/{url}",
                      "https://cdn.jsdelivr.net/gh/{repo}@{ref}/{path}"))
    return (got[0] == raw and len(got) == 3
            and got[1] == "https://gh-proxy.com/" + raw
            and got[2] == "https://cdn.jsdelivr.net/gh/me/repo@main/a/b.txt"
            and api == ["https://api.github.com/repos/me/repo/branches",
                        "https://gh-proxy.com/"
                        "https://api.github.com/repos/me/repo/branches"])


def m16():
    """带 Token 的发布路径不许经过镜像。"""
    src = (ROOT / "modules" / "plugin_publisher.py").read_text(encoding="utf-8")
    if "ghmirror" in src or "github_mirrors" in src:
        print("      发布模块引用了镜像")
        return False
    # 取数入口的每个调用点都不带 Authorization
    for hit in re.finditer(r"_github_fetch\(", WEBUI_SRC):
        if "Authorization" in WEBUI_SRC[hit.start():hit.start() + 200]:
            print("      有调用点带了 Authorization")
            return False
    return "_github_fetch" in WEBUI_SRC and "DEFAULT_MIRRORS" in WEBUI_SRC


def m17():
    """直连不通时退到镜像（官方域名用不可解析的，镜像指向本地桩）。"""
    import asyncio
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    hits = []

    class Stub(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            body = b'{"from": "mirror"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        server = M.WebUIServer.__new__(M.WebUIServer)
        server.config = {"github_mirrors": f"http://127.0.0.1:{port}/{{url}}"}
        target = "https://api.github.invalid/repos/x/branches"
        data, insecure = asyncio.run(server._github_fetch(target, "json"))
    finally:
        srv.shutdown()
        srv.server_close()
    from modules import ghmirror
    return (data == {"from": "mirror"} and insecure is False
            and hits and hits[0] == "/" + target
            and ghmirror._PREFERRED.get("api.github.invalid")
            == f"http://127.0.0.1:{port}/{target}")


def m18():
    """镜像测速：量出延迟、按快的在前排序、不可用的排最后。"""
    import asyncio
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    def serve(delay):
        class Stub(BaseHTTPRequestHandler):
            def do_GET(self):
                time.sleep(delay)
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv

    fast, slow = serve(0.0), serve(0.35)

    class FakeRequest:
        async def json(self):
            return {"mirrors": f"http://127.0.0.1:{slow.server_address[1]}/{{url}}\n"
                               f"http://127.0.0.1:{fast.server_address[1]}/{{url}}\n"
                               f"https://127.0.0.1:1/{{url}}"}

    try:
        server = M.WebUIServer.__new__(M.WebUIServer)
        server.config = {"plugin_market_repo": "me/repo"}
        resp = asyncio.run(server.handle_github_mirror_test(FakeRequest()))
        body = json.loads(resp.body.decode("utf-8"))
    finally:
        for srv in (fast, slow):
            srv.shutdown()
            srv.server_close()
    items = body.get("mirrors") or []
    if not body.get("success") or len(items) != 3:
        print("      返回不对:", body)
        return False
    first, second, last = items
    return (first["host"] == f"127.0.0.1:{fast.server_address[1]}" and first["ok"]
            and second["ok"] and second["ms"] > first["ms"]
            and first["kinds"] == "raw+api"
            and last["ok"] is False and last["note"])


def m19():
    """市场安装：包体经统一取数入口下载，装完落在插件目录里，且是停用状态。"""
    import asyncio
    import tempfile
    from modules.plugins import PluginManager

    calls = []

    class Probe:
        handle_plugins_install_remote = M.WebUIServer.handle_plugins_install_remote
        _check_market_source = lambda self, entry, manifest: ""

        def __init__(self, manager):
            self.plugin_manager = manager

        async def _plugins_market_payload(self, market="official", force=False,
                                          sort="latest"):
            return {"success": True, "plugins": [{
                "id": "func-a", "download": "https://example.invalid/func-a.zip",
                "source_repo": "me/repo", "branch": "lovomo_plugin_func-a"}]}

        async def _github_fetch(self, url, kind="json", *, timeout=10.0, headers=None):
            calls.append((url, kind))
            return make_zip(GOOD_PY), False

    class Request:
        async def json(self):
            return {"id": "func-a"}

    root = Path(tempfile.mkdtemp(prefix="lovomo_remote_"))
    manager = PluginManager(root / "plugins", root / "plugins" / "state.json")
    resp = asyncio.run(Probe(manager).handle_plugins_install_remote(Request()))
    body = json.loads(resp.body.decode("utf-8"))
    info = manager.get("func-a") or {}
    return (body.get("success") is True and manager.get("func-a") is not None
            and calls and calls[0][1] == "bytes"
            and info.get("enabled") is False)


def m14():
    """历史更新页要说明文档来自插件包，程序 Releases 单独一节。"""
    for need in ("插件更新说明", "程序历史版本", "内容来自插件包内的"):
        if need not in HTML_SRC:
            print("      缺少:", need)
            return False
    return "plugin-history-sub" in HTML_SRC


def m20():
    """文件大小格式化必须给出量级正确的单位。"""
    src = HTML_SRC
    i = src.find("function fmtSize")
    body = src[i:i + 500]
    if i < 0 or "SIZE_UNITS" not in src:
        return False
    # 单位表必须以 B 开头，且按 1024 递进；不能出现 34MB 显示成 GB 的错位
    return ("['B', 'KB', 'MB', 'GB', 'TB']" in src
            and "n /= 1024" in body
            and "MONTH_UNITS" not in src)


def m21():
    """区块底色调到 0 时不能还有一层磨砂。"""
    css = (ROOT / "plugins" / "sources" / "meow-skin" / "theme.css").read_text(
        encoding="utf-8")
    if re.search(r"backdrop-filter\s*:", css):
        print("      theme.css 里还有 backdrop-filter 声明")
        return False
    from modules.plugins import _read_manifest_from_dir
    manifest = _read_manifest_from_dir(ROOT / "plugins" / "sources" / "meow-skin")
    spec = next((v for v in (manifest.get("skin") or {}).get("vars") or []
                 if v.get("name") == "block-opacity"), None)
    if not spec or float(spec.get("min", 1)) > 0:
        print("      区块透明度最小值不是 0:", spec)
        return False
    return True


check("指令真的被分发给插件运行时", i1)
check("指令命中后打断后续 LLM 流程", i2)
check("指令解析只认行首前缀", i3)
check("插件 on_message 返回值会被发出", i4)
check("插件日志并进主日志流", i5)
check("插件运行时 logger 接到插件日志", i6)
check("日志缓冲写入同时去掉 \\r 与 \\n", i7)
check("插件管理器启动时清理残留临时目录", i8)
check("残留临时目录被真正删除", i9)
check("目录删除带重试", i10)
check("插件清单 panel 段被规范化", i11)
check("已装插件成页渲染（无弹窗、无独立日志区）", i12)
check("每个面板都有入口能打开（无死页面）", i12c)
check("程序历史版本页由左上角 logo 进入", i12f)
check("插件市场与插件是两个独立面板", i12d)
check("市场页上传/打开目录/刷新市场都绑了事件", i12b)
check("点插件卡片空白处打开功能页", i13)
check("外观开关默认不生效", i14)
check("主题底色挂在 skin-on 下", i15)
check("主日志去掉尾部空白", i16)
check("前端按 active_skins 切换外观", i17)
check("功能页不再引用已删除的 skinMeta", i18)
check("功能页调用的插件函数都有定义", i19)
check("插件设置不实时生效（改动只进暂存）", k1)
check("功能页右下角有固定的保存并重载入口", k2)
check("保存并重载会落盘 + 重载运行时 + 重拉皮肤", k3)
check("后端 settings 只落盘、reload 才生效", k4)
check("皮肤透明度与圆角变量真的被消费", k5)
check("透明度作用于底色而非整体不透明度", k6)
check("皮肤数值变量带上清单声明的 CSS 单位", k7)
check("主色能存住并生效（不再被白名单丢掉）", k8)
check("取色器是色块而非一条横线", k9)
check("区块透明度覆盖日志/输入框/按钮且嵌套分层", k10)
check("透明度覆盖聊天记录/表格/弹窗/统计等全部面板", k11)
check("非英文资源名保留原样（不再变成下划线）", l1)
check("同名资源自动编号不覆盖", l2)
check("背景图 URL 接受中文名且挡住注入", l3)
check("选择文件时保留原始文件名", l4)
check("图标只认 logo.* 并兼容 icon.*", l5)
check("readme / update 文档可读（含截断）", l6)
check("市场只认前缀匹配的插件分支", m1)
check("分支名能推出插件 id", m2)
check("Release 与分支的归属判断", m3)
check("下载量/收藏量按分支汇总", m4)
check("市场条目优先用 Release 资源作安装包", m5)
check("最新发布/最多下载/最多收藏 三种排序", m6)
check("Release 点赞数计入收藏量", m7)
check("后端接上分支扫描与新路由", m8)
check("市场缓存未排序条目（换排序不打网络）", m9)
check("市场面板有排序下拉与下载/收藏数字", m10)
check("安装完成后弹出 readme 弹窗", m11)
check("插件更新说明页与程序历史版本页齐备", m12)
check("新增配置项三处同步", m13)
check("镜像模板解析：raw 与 api 都走镜像", m15)
check("带 Token 的发布路径不经过镜像", m16)
check("直连不通时退到镜像", m17)
check("镜像测速按速度排序", m18)
check("市场安装走统一取数入口（装完停用）", m19)
check("历史更新页标明文档来源与程序版本分区", m14)
check("文件大小单位换算正确", m20)
check("透明度为 0 时不再有磨砂层", m21)


# ------------------------------------ Y 清单改用 plugin.yaml + 来源/归属校验
section("插件清单改用 plugin.yaml + 来源/归属校验")


def y1():
    """YAML 子集解析器：嵌套、序列、块标量、引号、注释都要对。"""
    from modules import yaml_lite
    got = yaml_lite.loads("""
# 整行注释
key: value   # 行尾注释
list:
- 1
- two
quoted: "a # b"
single: 'it''s'
empty:
nested:
  deep:
    - x: 1
      y: 2
    - z: 3
folded: >-
  第一行
  第二行
block: |
  l1
  l2
""")
    want = {"key": "value", "list": [1, "two"], "quoted": "a # b",
            "single": "it's", "empty": None,
            "nested": {"deep": [{"x": 1, "y": 2}, {"z": 3}]},
            "folded": "第一行 第二行", "block": "l1\nl2"}
    if got != want:
        print("      解析结果不符:", got)
        return False
    # 真布尔/null/数字要还原成对应类型，字符串不要被误转
    kinds = yaml_lite.loads("a: true\nb: false\nc: null\nd: 1.5\ne: 1.0.0\n")
    return (kinds == {"a": True, "b": False, "c": None, "d": 1.5, "e": "1.0.0"})


def y2():
    """dumps -> loads 往返一致（程序生成的清单必须能被自己读回）。"""
    from modules import yaml_lite
    sample = {"id": "a", "name": "中文 名", "ver": "1.0.0", "on": True,
              "off": False, "none": None, "num": 3, "f": 0.5, "empty": "",
              "list": [1, "两", False],
              "nested": {"k": [{"a": "b", "c": 1}], "s": "带: 冒号"}}
    return yaml_lite.loads(yaml_lite.dumps(sample)) == sample


def y3():
    """示例插件一律用 plugin.yaml，且元信息字段齐全。"""
    from modules.plugins import _read_manifest_from_dir
    srcs = sorted(d for d in (ROOT / "plugins" / "sources").iterdir() if d.is_dir())
    if not srcs:
        print("      sources 下没有任何示例插件")
        return False
    for d in srcs:
        if (d / "plugin.json").is_file():
            print(f"      {d.name} 还留着旧的 plugin.json")
            return False
        if not (d / "plugin.yaml").is_file():
            print(f"      {d.name} 没有 plugin.yaml")
            return False
        m = _read_manifest_from_dir(d)
        for k in ("id", "name", "version", "author", "github", "homepage",
                  "repo", "logo"):
            if not m.get(k):
                print(f"      {d.name} 缺字段 {k}")
                return False
    return True


def y4():
    """清单只认三个文件名，yaml 优先；缺清单时补出来的也是 yaml。"""
    from modules.plugins import MANIFEST_NAMES, MANIFEST_NAME, find_manifest
    if MANIFEST_NAMES != ("plugin.yaml", "plugin.yml", "plugin.json"):
        print("      清单名优先级不对:", MANIFEST_NAMES)
        return False
    if MANIFEST_NAME != "plugin.yaml":
        return False
    tmp = Path(tempfile.mkdtemp(prefix="man_"))
    (tmp / "plugin.json").write_text("{}", encoding="utf-8")
    (tmp / "plugin.yml").write_text("id: a\n", encoding="utf-8")
    (tmp / "plugin.yaml").write_text("id: a\n", encoding="utf-8")
    if (find_manifest(tmp) or Path()).name != "plugin.yaml":
        return False
    (tmp / "plugin.yaml").unlink()
    if (find_manifest(tmp) or Path()).name != "plugin.yml":
        return False
    (tmp / "plugin.yml").unlink()
    return (find_manifest(tmp) or Path()).name == "plugin.json"


def y5():
    """归属判断：github / repo / homepage / author 都能认出作者。"""
    from modules.plugins import ownership_matches, manifest_logins
    cases = [
        ({"github": "alice"}, "alice", True),
        ({"github": "Alice"}, "alice", True),
        ({"repo": "alice/x"}, "alice", True),
        ({"homepage": "https://github.com/alice/x"}, "alice", True),
        ({"author": "alice"}, "alice", True),
        ({"author": "爱丽丝"}, "alice", False),
        ({"author": "Lovomo 官方示例"}, "alice", False),
        ({}, "alice", False),
        ({"github": "bob"}, "alice", False),
        ({"github": "alice"}, "", False),
    ]
    for manifest, login, want in cases:
        got = ownership_matches(manifest, login)
        if got != want:
            print(f"      {manifest} vs {login!r} 期望 {want} 实际 {got}")
            return False
    return manifest_logins({"github": "alice", "repo": "alice/x",
                            "homepage": "https://github.com/alice/x"}) == ["alice"]


def y6():
    """市场条目带 source_repo，homepage/logo 以清单声明为准。"""
    from modules.market import build_entry, safe_logo_path
    entry = build_entry(
        "lovomo_plugin_a", "lovomo_plugin",
        {"id": "a", "name": "甲", "logo": "assets/logo.png", "github": "alice",
         "homepage": "https://github.com/alice/a"},
        "https://raw.githubusercontent.com/o/r/lovomo_plugin_a", "o/r", {})
    if entry.get("source_repo") != "o/r":
        print("      没记录来源仓库:", entry.get("source_repo"))
        return False
    if not entry["logo_url"].endswith("/assets/logo.png"):
        print("      logo 没按清单声明取:", entry["logo_url"])
        return False
    if entry.get("homepage") != "https://github.com/alice/a":
        print("      homepage 应优先用清单里的:", entry.get("homepage"))
        return False
    if entry.get("github") != "alice":
        return False
    # 图标路径不许越界（开头多余的 / 会被当成"插件根目录"剥掉）
    return (safe_logo_path("../../etc/passwd") == ""
            and safe_logo_path("") == ""
            and safe_logo_path(".hidden") == ""
            and safe_logo_path("/logo.png") == "logo.png")


def y7():
    """安装流程真的接了来源校验，并且会记下来源。"""
    i = WEBUI_SRC.find("async def handle_plugins_install_remote(")
    block = WEBUI_SRC[i:i + 4200]
    if "_check_market_source" not in block:
        print("      安装接口没调来源校验")
        return False
    if "set_source(" not in block:
        print("      安装成功后没记来源")
        return False
    j = WEBUI_SRC.find("async def handle_plugins_publish(")
    pub = WEBUI_SRC[j:j + 3000]
    if "ownership_matches" not in pub:
        print("      发布接口没做归属校验")
        return False
    if "handle_plugins_publish_status" not in WEBUI_SRC:
        return False
    st = method_src("handle_plugins_publish_status")
    return "_publish_scope" in st and '"publishable"' in st


def y8():
    """前端：未认证只留 Token 框，认证后按服务端给的名单渲染。"""
    for k in ('id="plugin-publish-token"', 'id="plugin-publish-list"',
              "publishStatus.plugins", "publishStatus.blocked",
              "尚未认证 GitHub 身份"):
        if k not in HTML_SRC:
            print("      前端缺:", k)
            return False
    # 列表默认是收起的，由 JS 按认证状态决定展开
    i = HTML_SRC.find('id="plugin-publish-list"')
    return 'display:none' in HTML_SRC[i - 60:i + 60]


check("YAML 子集解析正确", y1)
check("清单 dumps/loads 往返一致", y2)
check("示例插件改用 plugin.yaml", y3)
check("清单名优先级（yaml 优先）", y4)
check("归属判断认 github/repo/homepage/author", y5)
check("市场条目带 source_repo 与清单 logo", y6)
check("安装/发布接口真的接了校验", y7)
check("发布面板默认隐藏列表", y8)


# ------------------------------------------------------------ Z 市场面板归位与更新检查
section("Z 发布面板归位 / 检查插件更新")


def z1():
    """「发布到市场」整个搬到插件市场面板，夹在「插件市场」与「在线市场」之间。"""
    i = HTML_SRC.find('id="panel-plugins"')
    j = HTML_SRC.find('id="panel-installed-plugins"')
    if not (-1 < i < j):
        return False
    panel = HTML_SRC[i:j]
    i_pub = panel.find('id="plugin-publish-block"')
    i_market = panel.find('id="plugin-market-list"')
    return (-1 < i_pub < i_market
            and 'id="plugin-publish-block"' not in HTML_SRC[j:])


def z2():
    """「检查插件更新」在插件页右下角，且落在「已安装插件」内容块之外。"""
    if 'id="plugin-update-check-btn"' not in HTML_SRC:
        return False
    if 'id="plugin-update-modal"' not in HTML_SRC:
        return False
    i = HTML_SRC.find('id="panel-installed-plugins"')
    j = HTML_SRC.find('id="plugin-update-modal"')
    if not (-1 < i < j):
        return False
    panel = HTML_SRC[i:j]
    at = panel.find('id="plugin-update-check-btn"')
    # 按钮之前要把 section-block 和它内部的列表容器都关掉
    if at < 0 or panel[:at].count("</div>") < 2:
        return False
    return ("api/plugins/updates" in HTML_SRC
            and "function openPluginUpdateDialog" in HTML_SRC
            and "function renderPluginUpdates" in HTML_SRC
            and "function doPluginUpdate" in HTML_SRC
            and '"/api/plugins/updates"' in WEBUI_SRC
            and "def handle_plugins_updates" in WEBUI_SRC)


def z3():
    """替换走现成的 install_remote：覆盖安装沿用启用状态，不用另造接口。"""
    i = HTML_SRC.find("async function doPluginUpdate(")
    if i < 0:
        return False
    return "doInstallRemote(id, pluginUpdateMsg)" in HTML_SRC[i:i + 400]


def _updates_payload(installed, markets):
    """拿桩数据跑一遍 _plugins_updates_payload。"""
    srv = object.__new__(M.WebUIServer)

    class _Mgr:
        def list_plugins(self):
            return installed

    async def fake_market(force=False, sort="latest", market="official"):
        return {"plugins": markets.get(market, []), "warnings": []}

    srv.plugin_manager = _Mgr()
    srv._plugins_market_payload = fake_market
    return asyncio.run(srv._plugins_updates_payload())


def z4():
    """只列出市场版本更高的插件，版本相同/更低的不算。"""
    got = _updates_payload(
        [{"id": "a", "version": "1.0.0", "enabled": True},
         {"id": "b", "version": "2.0.0"},
         {"id": "c", "version": "1.0.0", "enabled": False}],
        {"official": [{"id": "a", "name": "A", "version": "1.1.0", "download": "u1"},
                      {"id": "b", "name": "B", "version": "2.0.0", "download": "u2"}],
         "thirdparty": []})
    if [p["id"] for p in got["plugins"]] != ["a"]:
        print("      ", got)
        return False
    only = got["plugins"][0]
    return (only["installed_version"] == "1.0.0" and only["version"] == "1.1.0"
            and only["name"] == "A" and only["download"] == "u1"
            and only["enabled"] is True and got["count"] == 1)


def z5():
    """同一个插件在多条来源里出现时取版本最高的那条。"""
    got = _updates_payload(
        [{"id": "a", "version": "1.0.0"}],
        {"official": [{"id": "a", "version": "1.1.0", "download": "old"}],
         "thirdparty": [{"id": "a", "version": "1.2.0", "download": "new"}]})
    return ([p["id"] for p in got["plugins"]] == ["a"]
            and got["plugins"][0]["version"] == "1.2.0"
            and got["plugins"][0]["download"] == "new"
            and got["plugins"][0]["market"] == "thirdparty")


def z6():
    """已安装插件一行三个、卡片用满内容区、四个按钮铺满一行。"""
    if ".plugin-grid.installed { grid-template-columns: repeat(3, minmax(0, 1fr)); }" not in HTML_SRC:
        return False
    if 'class="plugin-grid installed"' not in HTML_SRC:
        return False
    if "#panel-installed-plugins .section-block," not in HTML_SRC \
            or "#panel-installed-plugins .plugin-panel-footer { max-width: none; }" not in HTML_SRC:
        return False
    i = HTML_SRC.find(".plugin-grid.installed .plugin-actions {")
    if i < 0 or "flex-wrap: nowrap" not in HTML_SRC[i:i + 80]:
        return False
    j = HTML_SRC.find(".plugin-grid.installed .plugin-actions .btn {")
    return j > 0 and "flex: 1 1 0" in HTML_SRC[j:j + 120]


check("发布到市场搬到插件市场面板", z1)
check("检查插件更新的按钮/弹窗/接口齐备", z2)
check("更新走覆盖安装", z3)
check("只列出有更新版本的插件", z4)
check("多来源取最高版本", z5)
check("已安装插件一行三个且按钮不换行", z6)


# ------------------------------------------------------------ 清理
try:
    if _LOOP is not None and not _LOOP.is_closed():
        fut = asyncio.run_coroutine_threadsafe(_SRV.runner.cleanup(), _LOOP)
        fut.result(timeout=5)
        _LOOP.call_soon_threadsafe(_LOOP.stop)
        if _LOOP_THREAD is not None:
            _LOOP_THREAD.join(timeout=5)
except Exception:
    pass
shutil.rmtree(_TMP, ignore_errors=True)
if _TMPSRV is not None:
    shutil.rmtree(_TMPSRV, ignore_errors=True)

print(f"\n{'=' * 70}")
print(f"结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
if FAIL:
    for n, e in FAIL:
        print(f"  FAILED: {n} -> {e}")
print('=' * 70)
sys.exit(0 if not FAIL else 1)
