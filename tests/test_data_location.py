"""用户数据落点与一次性迁移。

守两件事：
1. 打包运行时 data / config.json 固定落 %LOCALAPPDATA%\\Lovomo —— 换目录重装、覆盖
   升级都能接上；老版本写在程序目录里的那份会被防御式搬过去（复制→校验→原子改名→删旧，
   任何一步不对就原地不动）。
2. 源码运行时（开发/测试）沿用程序目录，绝对路径原样透传，不能把开发环境搬来搬去。
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import main as M  # noqa: E402
# webview 运行时符号已搬迁至 modules/webview_runtime（main 只再导出）；
# 猴子补丁要打在符号本体所在模块上才能被搬过去的函数看到
from modules import webview_runtime as WRT  # noqa: E402
# WebUI 服务整类已搬迁至 modules/webui_server；源码文本扫描指向代码本体所在模块
WEBUI_SRC = (ROOT / "modules" / "webui_server.py").read_text(encoding="utf-8")

PASS = []
FAIL = []


def check(name, fn):
    try:
        ok = bool(fn())
    except Exception as e:
        ok = False
        print(f"  !! {name} 抛异常: {type(e).__name__}: {e}")
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


# ---------------------------------------------------------------------------
# 伪造「打包运行」环境：程序目录与用户目录都是临时目录，不碰真实数据
# ---------------------------------------------------------------------------
_REAL_LOCALAPPDATA = os.environ.get("LOCALAPPDATA")
_REAL_FROZEN = getattr(M.sys, "frozen", None)
_REAL_EXECUTABLE = M.sys.executable


def fake_env(frozen=True):
    tmp = Path(tempfile.mkdtemp(prefix="lovomo_loc_"))
    appdir = tmp / "app"
    local = tmp / "local"
    appdir.mkdir(parents=True)
    local.mkdir(parents=True)
    os.environ["LOCALAPPDATA"] = str(local)
    if frozen:
        M.sys.frozen = True
        M.sys.executable = str(appdir / "Lovomo.exe")
    else:
        if hasattr(M.sys, "frozen"):
            del M.sys.frozen
    return appdir, M.user_data_dir()


def restore_env():
    if _REAL_LOCALAPPDATA is None:
        os.environ.pop("LOCALAPPDATA", None)
    else:
        os.environ["LOCALAPPDATA"] = _REAL_LOCALAPPDATA
    if _REAL_FROZEN is None:
        if hasattr(M.sys, "frozen"):
            del M.sys.frozen
    else:
        M.sys.frozen = _REAL_FROZEN
    M.sys.executable = _REAL_EXECUTABLE


def plant(appdir):
    """按真实布局埋一份「老版本写在程序目录里」的数据。"""
    (appdir / "data").mkdir(parents=True, exist_ok=True)
    (appdir / "data" / "lovomo.db").write_bytes(b"x" * 1234)
    (appdir / "data" / "tools.json").write_text("{}", encoding="utf-8")
    (appdir / "data" / "learned_terms.json").write_text('{"a":1}', encoding="utf-8")
    (appdir / "data" / "stickers").mkdir()
    (appdir / "data" / "stickers" / "a.png").write_bytes(b"pngdata")
    (appdir / "config.json").write_text('{"model":"m"}', encoding="utf-8")
    return M._data_signature(appdir / "data")


# ---------------------------------------------------------------------------
# a 打包运行：data 与 config.json 都搬到用户目录，内容一字不差
# ---------------------------------------------------------------------------
def a1():
    """data 落到用户目录。"""
    appdir, root = fake_env()
    try:
        plant(appdir)
        d = M.adopt_user_data("data")
        return d == root / "data" and d.is_dir()
    finally:
        shutil.rmtree(appdir.parent, ignore_errors=True)


def a2():
    """搬家后内容一字不差（文件数与字节数都对得上）。"""
    appdir, root = fake_env()
    try:
        before = plant(appdir)
        d = M.adopt_user_data("data")
        return M._data_signature(d) == before == (4, 1250)
    finally:
        shutil.rmtree(appdir.parent, ignore_errors=True)


def a3():
    """config.json 一起搬过去，内容不变。"""
    appdir, root = fake_env()
    try:
        plant(appdir)
        c = M.adopt_user_data("config.json")
        return c == root / "config.json" and c.read_text(encoding="utf-8") == '{"model":"m"}'
    finally:
        shutil.rmtree(appdir.parent, ignore_errors=True)


def a4():
    """搬完旧位置要清掉，且不留 .migrating 残骸。"""
    appdir, root = fake_env()
    try:
        plant(appdir)
        M.adopt_user_data("data")
        M.adopt_user_data("config.json")
        return (not (appdir / "data").exists() and not (appdir / "config.json").exists()
                and not (root / "data.migrating").exists()
                and not (root / "config.json.migrating").exists())
    finally:
        shutil.rmtree(appdir.parent, ignore_errors=True)


def a5():
    """重复调用幂等：不会反复搬、也不会把已搬好的再动一次。"""
    appdir, root = fake_env()
    try:
        before = plant(appdir)
        first = M.adopt_user_data("data")
        second = M.adopt_user_data("data")
        return first == second == root / "data" and M._data_signature(second) == before
    finally:
        shutil.rmtree(appdir.parent, ignore_errors=True)


def a6():
    """用户目录已经有数据时不覆盖、旧位置保留、落点仍是用户目录。"""
    appdir, root = fake_env()
    try:
        plant(appdir)
        (root / "data").mkdir(parents=True)
        (root / "data" / "existing.json").write_text("new", encoding="utf-8")
        d = M.adopt_user_data("data")
        return (d == root / "data"
                and (root / "data" / "existing.json").read_text(encoding="utf-8") == "new"
                and (appdir / "data" / "lovomo.db").exists())
    finally:
        shutil.rmtree(appdir.parent, ignore_errors=True)


def a7():
    """复制校验不过 → 中止搬家、旧数据原地不动、落点回退到旧位置。"""
    import modules.app_paths
    appdir, root = fake_env()
    real = M._data_signature
    calls = {"n": 0}

    def lying(p):
        calls["n"] += 1
        return (0, 0) if calls["n"] == 2 else real(p)

    try:
        plant(appdir)
        M._data_signature = lying
        # _migrate_user_data 已迁至 modules.app_paths，内部从该模块全局解析 _data_signature，
        # 补丁须同时打到新接缝才能拦截校验
        modules.app_paths._data_signature = lying
        d = M.adopt_user_data("data")
    finally:
        M._data_signature = real
        modules.app_paths._data_signature = real
    try:
        return (d == appdir / "data"
                and (appdir / "data" / "lovomo.db").exists()
                and not (root / "data").exists())
    finally:
        shutil.rmtree(appdir.parent, ignore_errors=True)


def a8():
    """_resolve_data_dir 在打包运行时用用户目录。"""
    appdir, root = fake_env()
    try:
        return M._resolve_data_dir({}) == root / "data"
    finally:
        shutil.rmtree(appdir.parent, ignore_errors=True)


def a9():
    """memory_data_path 优先级最高，配了就用配的那份（不搬家）。"""
    appdir, root = fake_env()
    try:
        plant(appdir)
        custom = appdir.parent / "custom_data"
        custom.mkdir()
        got = M._resolve_data_dir({"memory_data_path": str(custom)})
        return got == custom.resolve() and (appdir / "data" / "lovomo.db").exists()
    finally:
        shutil.rmtree(appdir.parent, ignore_errors=True)


def a10():
    """空目录也要搬过去（_data_signature 只数文件+字节，丢了空目录它看不出来）。"""
    appdir, root = fake_env()
    try:
        plant(appdir)
        (appdir / "data" / "rag" / "docs").mkdir(parents=True)
        (appdir / "data" / "empty_dir").mkdir()
        M.adopt_user_data("data")
        dst = root / "data"
        return (dst / "rag" / "docs").is_dir() and (dst / "empty_dir").is_dir()
    finally:
        shutil.rmtree(appdir.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# d 可写探测：清理失败不能把「可写」误判成「不可写」
# ---------------------------------------------------------------------------
def _hold_probe(appdir):
    """占住探针文件（模拟杀软/索引器/同步盘正读着这个刚建的文件）。"""
    probe = appdir / f".lovomo_write_{os.getpid()}"
    probe.write_text("1", encoding="utf-8")
    return probe, open(probe, "r", encoding="utf-8")


def d1():
    """探针删不掉时，_probe_writable 仍应报「可写」（写进去了就是可写）。"""
    appdir, _ = fake_env()
    fh = None
    try:
        probe, fh = _hold_probe(appdir)
        try:
            return M._probe_writable(appdir) is True
        finally:
            fh.close()
            fh = None
            probe.unlink(missing_ok=True)
    finally:
        if fh is not None:
            fh.close()
        shutil.rmtree(appdir.parent, ignore_errors=True)


def d2():
    """探针删不掉时，日志仍落在程序目录，不能被静默改写到用户目录。"""
    appdir, root = fake_env()
    fh = None
    try:
        probe, fh = _hold_probe(appdir)
        try:
            got = M.runtime_path("app.log", appdir)
        finally:
            fh.close()
            fh = None
            probe.unlink(missing_ok=True)
        return got.parent == appdir and got.parent != root
    finally:
        if fh is not None:
            fh.close()
        shutil.rmtree(appdir.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# b 源码运行：沿用程序目录，不打扰开发与测试
# ---------------------------------------------------------------------------
def b1():
    """源码运行时不搬数据（程序目录里那份原样留着）。"""
    appdir, root = fake_env(frozen=False)
    try:
        plant(appdir)
        M.adopt_user_data("data")
        return (appdir / "data" / "lovomo.db").exists() and not (root / "data").exists()
    finally:
        shutil.rmtree(appdir.parent, ignore_errors=True)


def b2():
    """绝对路径原样透传（测试与开发都靠这条）。"""
    appdir, root = fake_env()
    try:
        target = appdir.parent / "somewhere" / "my_config.json"
        return M.ConfigLoader._resolve_config_path(str(target)) == target
    finally:
        shutil.rmtree(appdir.parent, ignore_errors=True)


def b3():
    """源码运行 + 程序目录可写 → 配置文件就用程序目录里的那份。"""
    appdir, root = fake_env(frozen=False)
    try:
        cwd = Path.cwd()
        os.chdir(appdir)
        try:
            # 相对路径要在 chdir 期间解析，否则会按项目根去解
            got = M.ConfigLoader._resolve_config_path("config.json").resolve()
        finally:
            os.chdir(cwd)
        return got == (appdir / "config.json").resolve()
    finally:
        shutil.rmtree(appdir.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# c 收尾：确实没碰真实用户目录
# ---------------------------------------------------------------------------
def c1():
    """整个过程没有在真实 %LOCALAPPDATA%\\Lovomo 里留下 .migrating 残骸。"""
    if not _REAL_LOCALAPPDATA:
        return True
    real_root = Path(_REAL_LOCALAPPDATA) / "Lovomo"
    if not real_root.is_dir():
        return True
    return not any(real_root.glob("*.migrating"))


# ---------------------------------------------------------------------------
# e WebView2 缓存：界面更新后自动清理
# ---------------------------------------------------------------------------
def _plant_webview_profile(root):
    """造一份 WebView2 用户数据目录：几个缓存目录 + 几份「用户数据」。"""
    profile = root / "webview"
    default = profile / "EBWebView" / "Default"
    for rel in ("Cache", "Code Cache", "GPUCache", "Service Worker/CacheStorage"):
        d = default / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "blob.bin").write_bytes(b"x" * 100)
    (default / "Cookies").write_bytes(b"cookie-data")
    (default / "Local Storage").mkdir(parents=True, exist_ok=True)
    (default / "Local Storage" / "leveldb.ldb").write_bytes(b"ls")
    return profile, default


def e1():
    """界面更新后清掉缓存目录，但 Cookie 与 Local Storage 必须留着。"""
    appdir, root = fake_env()
    try:
        _plant_webview_profile(root)
        freed = M.purge_webview_cache()
        d = root / "webview" / "EBWebView" / "Default"
        return (freed == 400
                and not (d / "Cache").exists()
                and not (d / "Code Cache").exists()
                and not (d / "Service Worker" / "CacheStorage").exists()
                and (d / "Cookies").read_bytes() == b"cookie-data"
                and (d / "Local Storage" / "leveldb.ldb").exists())
    finally:
        shutil.rmtree(appdir.parent, ignore_errors=True)


def e2():
    """指纹没变就不再清第二次：把用户攒下的静态资源缓存白删掉会更慢。"""
    appdir, root = fake_env()
    try:
        _plant_webview_profile(root)
        M.purge_webview_cache()
        _plant_webview_profile(root)          # WebView2 又把缓存写回来了
        return M.purge_webview_cache() == 0
    finally:
        shutil.rmtree(appdir.parent, ignore_errors=True)


def e3():
    """指纹变了（版本/界面文件更新）→ 再清一次。"""
    appdir, root = fake_env()
    try:
        _plant_webview_profile(root)
        M.purge_webview_cache()
        _plant_webview_profile(root)
        marker = root / "webview" / M._WEBVIEW_CACHE_KEY_FILE
        marker.write_text("1.0.0.0|1-1", encoding="utf-8")
        return M.purge_webview_cache() == 400
    finally:
        shutil.rmtree(appdir.parent, ignore_errors=True)


def e4():
    """指纹里既有版本号，也有界面文件的修改时间与大小。"""
    from modules.updater import APP_VERSION
    key = M.webview_cache_key()
    info = (M.get_resource_path("webui") / "start.html").stat()
    return (key.startswith(APP_VERSION + "|")
            and f"{int(info.st_mtime)}-{info.st_size}" in key)


def e5():
    """拿不到（或不可写）用户目录时安全返回 0，不抛异常、不写标记。"""
    saved = WRT._webview_profile_dir
    WRT._webview_profile_dir = lambda: ""
    try:
        return M.purge_webview_cache() == 0
    finally:
        WRT._webview_profile_dir = saved


def e6():
    """调用点在窗口创建之前：WebView2 一启动就锁住缓存目录，晚一步就删不掉。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    i = src.find("def run_webview_loop")
    seg = src[i:i + 1500]
    if "purge_webview_cache()" not in seg or "create_window" not in seg:
        return False
    return seg.index("purge_webview_cache()") < seg.index("create_window")


def e7():
    """界面页不再可缓存：老版本只写 no-cache 时，WebView2 缓存里那份旧页面照样能用。"""
    i = WEBUI_SRC.find("async def handle_index")
    return '"Cache-Control": "no-store"' in WEBUI_SRC[i:i + 900]


def e8():
    """指纹文件放在 EBWebView 外面 —— 那个目录是 WebView2 自己管的。"""
    appdir, root = fake_env()
    try:
        _plant_webview_profile(root)
        M.purge_webview_cache()
        outside = root / "webview" / M._WEBVIEW_CACHE_KEY_FILE
        inside = root / "webview" / "EBWebView" / M._WEBVIEW_CACHE_KEY_FILE
        return outside.is_file() and not inside.exists()
    finally:
        shutil.rmtree(appdir.parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# f 「清理界面缓存」按钮
# ---------------------------------------------------------------------------
def f1():
    """后端入口齐备，接口也注册了。"""
    return (all(callable(getattr(M, n, None)) for n in (
        "webview2_control_alive", "clear_webview_cache_and_reload"))
            and 'add_post("/api/clear-ui-cache", self.handle_clear_ui_cache)' in WEBUI_SRC
            and "async def handle_clear_ui_cache" in WEBUI_SRC)


def f2():
    """顺序必须是「先拆界面进程 → 再删缓存 → 最后重建」：反了缓存删不干净。"""
    calls = []
    saved = (WRT.webview2_control_alive, WRT._webview2_release,
             WRT.purge_webview_cache, WRT._webview2_rebuild)
    WRT.webview2_control_alive = lambda w: True
    WRT._webview2_release = lambda w: (calls.append("release"), True)[1]
    WRT.purge_webview_cache = lambda force=False: (calls.append(f"purge:{force}"), 123)[1]
    WRT._webview2_rebuild = lambda w, u, p: (calls.append("rebuild"), True)[1]
    try:
        out = M.clear_webview_cache_and_reload(object(), "http://127.0.0.1:11500")
        return (calls == ["release", "purge:True", "rebuild"]
                and out == {"released": True, "rebuilt": True, "freed": 123})
    finally:
        (WRT.webview2_control_alive, WRT._webview2_release,
         WRT.purge_webview_cache, WRT._webview2_rebuild) = saved


def f3():
    """界面进程不在（浏览器访问 / 已收进托盘）时只清缓存，不重建进程。"""
    calls = []
    saved = (WRT.webview2_control_alive, WRT._webview2_release,
             WRT.purge_webview_cache, WRT._webview2_rebuild)
    WRT.webview2_control_alive = lambda w: False
    WRT._webview2_release = lambda w: (calls.append("release"), True)[1]
    WRT.purge_webview_cache = lambda force=False: (calls.append("purge"), 0)[1]
    WRT._webview2_rebuild = lambda w, u, p: (calls.append("rebuild"), True)[1]
    try:
        out = M.clear_webview_cache_and_reload(object(), "http://127.0.0.1:11500")
        return (calls == ["purge"] and out["released"] is False
                and out["rebuilt"] is False)
    finally:
        (WRT.webview2_control_alive, WRT._webview2_release,
         WRT.purge_webview_cache, WRT._webview2_rebuild) = saved


def f4():
    """拆界面进程会把当前页面带走，所以必须先回响应、再动手。"""
    i = WEBUI_SRC.find("async def handle_clear_ui_cache")
    seg = WEBUI_SRC[i:i + 1600]
    if "json_response" not in seg or "Thread(" not in seg:
        return False
    return seg.index("Thread(") < seg.index("json_response")


def f5():
    """配置页有按钮、有 hover 提示，中英文案都在。"""
    html = (ROOT / "webui" / "start.html").read_text(encoding="utf-8")
    return ('id="clear-ui-cache-btn"' in html
            and 'data-icon="clean"' in html
            and 'title="更新了但是感觉没有变化？点击这个按钮清理界面缓存。"' in html
            and '"清理界面缓存": "Clear interface cache"' in html
            and '"更新了但是感觉没有变化？点击这个按钮清理界面缓存。":' in html)


def f5b():
    """请求路径必须带 api/ 前缀：API_BASE 是空串，漏了就是 404。"""
    html = (ROOT / "webui" / "start.html").read_text(encoding="utf-8")
    return ("apiPost('api/clear-ui-cache'" in html
            and "apiPost('clear-ui-cache'" not in html)


def f6():
    """按钮文案走 setBtnText（直接写 textContent 会把图标抹掉）。"""
    html = (ROOT / "webui" / "start.html").read_text(encoding="utf-8")
    i = html.find("const clearUiCacheBtn = document.getElementById")
    seg = html[i:i + 1200]
    return "setBtnText(clearUiCacheBtn" in seg and "btnLabel(clearUiCacheBtn)" in seg


# ---------------------------------------------------------------------------
# g 安装/卸载前后先关闭正在运行的程序
# ---------------------------------------------------------------------------
def _iss() -> str:
    return (ROOT / "Lovomo_Setup.iss").read_text(encoding="utf-8")


def g1():
    """互斥体名必须与程序里的一致，否则判断不出程序在不在跑。"""
    iss = _iss()
    return f"AppMutexName = '{M._INSTANCE_MUTEX_NAME}';" in iss


def g2():
    """安装与卸载都要先关掉正在运行的程序（关不掉就说明白，别装一半）。"""
    iss = _iss()
    return (iss.count("CloseRunningApp") >= 3
            and "function PrepareToInstall(var NeedsRestart: Boolean): String;" in iss
            and "无法关闭正在运行的 Lovomo" in iss)


def g3():
    """先请求正常退出（能落盘），几秒后再强制结束。"""
    iss = _iss()
    i = iss.index("function CloseRunningApp")
    seg = iss[i:iss.index("function PrepareToInstall")]
    return ("taskkill.exe" in seg
            and seg.index("'/IM '") < seg.index("'/F /IM '")
            and "Sleep(" in seg and "CheckForMutexes" in seg)


def g4():
    """卸载时先关程序、再问是否删除用户数据（问完才动手清）。"""
    iss = _iss()
    seg = iss[iss.index("procedure CurUninstallStepChanged"):]
    return seg.index("CloseRunningApp") < seg.index("AskRemoveUserData")


def main():
    print("用户数据落点与一次性迁移")
    check("打包运行时 data 落到用户目录", a1)
    check("搬家后内容一字不差", a2)
    check("config.json 一起搬且内容不变", a3)
    check("搬完旧位置清掉、不留残骸", a4)
    check("重复调用幂等", a5)
    check("用户目录已有数据时不覆盖", a6)
    check("校验不过时原地不动、落点回退", a7)
    check("_resolve_data_dir 打包时用用户目录", a8)
    check("memory_data_path 优先级最高", a9)
    check("空目录也搬过去", a10)
    check("探针删不掉时仍报可写", d1)
    check("探针删不掉时日志不搬家", d2)
    check("源码运行不搬数据", b1)
    check("绝对路径原样透传", b2)
    check("源码运行沿用程序目录的 config.json", b3)
    check("没在真实用户目录留下残骸", c1)
    check("界面更新后清缓存、保留 Cookie", e1)
    check("指纹没变不重复清", e2)
    check("指纹变了再清一次", e3)
    check("指纹含版本号与界面文件签名", e4)
    check("拿不到用户目录时安全返回", e5)
    check("清缓存发生在开窗口之前", e6)
    check("界面页改为 no-store", e7)
    check("指纹文件放在 EBWebView 外面", e8)
    check("清理按钮的后端入口齐备", f1)
    check("先拆界面进程再删再重建", f2)
    check("没有界面进程时只清缓存", f3)
    check("先回响应再拆界面", f4)
    check("按钮与 hover 提示（中英）都在", f5)
    check("请求路径带 api/ 前缀", f5b)
    check("按钮文案走 setBtnText", f6)
    check("安装包互斥体名与程序一致", g1)
    check("装/卸前先关程序，关不掉就提示", g2)
    check("先正常退出再强制结束", g3)
    check("卸载先关程序再问用户数据", g4)
    restore_env()
    print(f"结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
