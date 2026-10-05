# -*- coding: utf-8 -*-
"""WebView2 控件运行时辅助（自 main.py 原样搬迁）。"""

import shutil
import threading
import time
from pathlib import Path

from modules.app_paths import get_resource_path
from modules.window_geometry import _webview_profile_dir
from modules.log_console import log_window_event
from modules.app_context import _APP_HOOKS

# ---------------------------------------------------------------------------
# 隐藏时释放 WebView2
# ---------------------------------------------------------------------------
# 窗口收进托盘之后把 WebView2 控件整个拆掉，再打开时重建：browser / gpu /
# renderer / utility 这组进程会一起退出，三百来 MB 交还系统（实测 6 个进程
# 323MB -> 0）。也试过只让页面休眠（CoreWebView2.TrySuspendAsync 能返回成功），
# 但任务管理器里的占用几乎不动，所以不采用。
# 窗体本身留着：托盘、几何记忆、任务栏唤醒、单实例唤醒都挂在窗体上，重建的是
# 控件，不是窗口。


def _webview2_form(window):
    """取 pywebview 窗口背后的 WinForms 窗体（非 Windows / 拿不到返回 None）。"""
    try:
        return getattr(window, "native", None)
    except Exception:
        return None


def _webview2_on_ui(form, fn) -> dict:
    """在 UI 线程上跑 fn —— WebView2 的成员只能在 UI 线程访问。

    pywebview 自己的 hide/show 也是这么派发的（Invoke + Func[Type]）。
    返回 {"value": 结果} 或 {"err": 异常}，调用方自己判断。
    """
    try:
        from System import Func, Type
    except Exception as e:
        return {"err": e}
    box = {}

    def _wrap():
        try:
            box["value"] = fn()
        except Exception as e:
            box["err"] = e

    try:
        form.Invoke(Func[Type](_wrap))
    except Exception as e:
        box.setdefault("err", e)
    return box


def _webview2_release(window) -> bool:
    """拆掉 WebView2 控件，它的所有子进程随之退出。窗体保留。"""
    form = _webview2_form(window)
    if form is None:
        return False
    ctrl = getattr(form, "webview", None)
    if ctrl is None:
        return True  # 已经拆过了
    box = _webview2_on_ui(form, ctrl.Dispose)
    if "err" in box:
        log_window_event(f"释放界面进程失败：{box['err']}")
        return False
    return True


def _webview2_rebuild(window, url: str, profile_dir: str) -> bool:
    """在已存在的窗体里重建 WebView2 控件，并导航回界面。

    只重建控件：窗体、托盘、几何记忆、任务栏钩子都还在原位。用户数据目录沿用
    原来的，登录态与本地存储不会丢；页面本身是重新加载的。
    """
    form = _webview2_form(window)
    if form is None or not url:
        return False
    try:
        import System.Windows.Forms as WinForms
        from Microsoft.Web.WebView2.WinForms import (
            CoreWebView2CreationProperties, WebView2)
    except Exception as e:
        log_window_event(f"重建界面失败（WebView2 组件不可用）：{e}")
        return False

    def _create():
        props = CoreWebView2CreationProperties()
        if profile_dir:
            props.UserDataFolder = profile_dir
        # 与 pywebview 建窗口时的取值保持一致
        props.AdditionalBrowserArguments = "--disable-features=ElasticOverscroll"
        ctrl = WebView2()
        ctrl.CreationProperties = props
        # pywebview 手里还拿着旧控件：先换掉，否则下面接回它的初始化时，
        # 它内部 load_url 会落到已经释放的控件上，整段初始化就断在半路
        browser = getattr(form, "browser", None)
        if browser is not None:
            browser.webview = ctrl
        form.webview = ctrl
        # 接回 pywebview 自己的初始化：设置项（右键菜单 / F12 / 快捷键）、
        # NewWindowRequested（没它的话外链会被 WebView2 弹成一个自带标签页的
        # 浏览器窗口）、下载与证书处理全在里面
        ready = getattr(browser, "on_webview_ready", None)
        if ready is not None:
            def _on_ready(sender, args):
                try:
                    ready(sender, args)
                except Exception as e:
                    log_window_event(
                        f"接回界面初始化失败（界面仍可重建）：{type(e).__name__}: {e}")
            ctrl.CoreWebView2InitializationCompleted += _on_ready
        # 先清掉窗体上残留的 WebView2 控件：以前没清，反复重建会在同一个窗体上
        # 叠出多个控件，界面就可能停在某个已经没人用的旧控件上
        try:
            for old in [c for c in form.Controls if isinstance(c, WebView2)]:
                form.Controls.Remove(old)
        except Exception:
            pass
        form.Controls.Add(ctrl)
        ctrl.Dock = WinForms.DockStyle.Fill
        ctrl.EnsureCoreWebView2Async(None)
        return ctrl

    box = _webview2_on_ui(form, _create)
    ctrl = box.get("value")
    if ctrl is None:
        log_window_event(f"重建界面失败：{box.get('err')}")
        return False
    # 等 CoreWebView2 真正初始化出来再继续。初始化失败（内核起不来、
    # 用户数据目录被占用等）时 CoreWebView2 会一直是 None —— 那时绝不能
    # 返回 True 假装成功，否则窗口会被当成"已重建"拉出来，结果一片空白。
    deadline = time.time() + 10
    ready = False
    while time.time() < deadline:
        if _webview2_on_ui(form, lambda: ctrl.CoreWebView2 is not None).get("value"):
            ready = True
            break
        time.sleep(0.05)
    if not ready:
        log_window_event("重建界面超时：内核未初始化完成")
        return False
    # 导航交给 on_webview_ready（上面 _create 已接回）：它会用窗口原来的
    # real_url 重新 load_url，走 pywebview 完整路径，不再重复 Navigate。
    _WEBVIEW_PROCESS_DEAD["flag"] = False
    attach_process_failed_watch(window)
    return True


# ---------------------------------------------------------------------------
# WebView2 缓存
# ---------------------------------------------------------------------------
# WebView2 用的是完全独立的用户数据目录（<数据目录>/webview/EBWebView），
# 系统浏览器里清缓存对它无效 —— 程序更新后界面可能一直停在老版本上。
# 所以这里在「版本号或界面文件变了」的时候把那几个缓存目录删掉重建。
# 只删缓存，不碰 Cookies 与 Local Storage：那是登录态与界面偏好，删了用户
# 每次开窗口都要重输密码、面板还会回到默认页。

_WEBVIEW_CACHE_DIRS = (
    "Cache",
    "Code Cache",
    "GPUCache",
    "DawnCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "GraphiteDawnCache",
    "ShaderCache",
    "GrShaderCache",
    "Service Worker/CacheStorage",
    "Service Worker/ScriptCache",
)
_WEBVIEW_CACHE_KEY_FILE = "lovomo_cache_key.txt"


def webview_cache_key() -> str:
    """缓存的新鲜度指纹：版本号 + 界面文件的修改时间与大小。

    只看版本号不够 —— 同一个版本号反复重打包（开发期常态）时界面照样会变。
    """
    stamp = "unknown"
    try:
        info = (get_resource_path("webui") / "start.html").stat()
        stamp = f"{int(info.st_mtime)}-{info.st_size}"
    except OSError:
        pass
    try:
        from modules.updater import APP_VERSION
    except Exception:
        APP_VERSION = ""
    return f"{APP_VERSION}|{stamp}"


def _remove_cache_dir(path: Path) -> int:
    """删掉一个缓存目录，返回它占用的字节数；被占用时重试几次。"""
    size = 0
    try:
        for item in path.rglob("*"):
            if item.is_file():
                size += item.stat().st_size
    except OSError:
        pass
    for _ in range(3):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            break
        time.sleep(0.3)
    return size


def purge_webview_cache(force: bool = False) -> int:
    """清一次 WebView2 缓存，返回释放的字节数（0 = 不需要清）。

    界面文件和版本号都没变时不重复动手：那会把用户攒下来的静态资源缓存
    白白删掉，下次开窗口反而更慢。
    """
    profile = _webview_profile_dir()
    if not profile:
        return 0
    profile_dir = Path(profile)
    marker = profile_dir / _WEBVIEW_CACHE_KEY_FILE
    key = webview_cache_key()
    if not force:
        try:
            if marker.read_text(encoding="utf-8").strip() == key:
                return 0
        except OSError:
            pass
    root = profile_dir / "EBWebView"
    freed = 0
    # 老版本把缓存放在 EBWebView 根下，新版本放在 EBWebView/Default 下，都扫一遍
    for base in (root, root / "Default"):
        for rel in _WEBVIEW_CACHE_DIRS:
            target = base / rel
            if target.exists():
                freed += _remove_cache_dir(target)
    try:
        marker.write_text(key, encoding="utf-8")
    except OSError:
        pass
    return freed


def webview2_control_alive(window) -> bool:
    """界面进程还在不在（窗口收进托盘后会被拆掉，那时它是已释放状态）。"""
    form = _webview2_form(window)
    if form is None:
        return False
    ctrl = getattr(form, "webview", None)
    if ctrl is None:
        return False
    return bool(_webview2_on_ui(form, lambda: not bool(ctrl.IsDisposed)).get("value"))


# WebView2 的内核进程被外部结束（任务管理器里那组显示成「浏览器」的
# msedgewebview2）或崩溃时置位。控件本身不会被 Dispose，光看 IsDisposed 看不出来，
# 所以单独记一笔，唤醒路径据此重建界面。
_WEBVIEW_PROCESS_DEAD = {"flag": False}
_FAILED_WATCHED = {"ctrl": None}


def attach_process_failed_watch(window) -> bool:
    """给界面进程挂上「内核挂了」的监听（同一个控件只挂一次）。"""
    form = _webview2_form(window)
    ctrl = getattr(form, "webview", None) if form is not None else None
    if ctrl is None:
        return False

    def _hook():
        core = getattr(ctrl, "CoreWebView2", None)
        if core is None:
            return False
        if _FAILED_WATCHED["ctrl"] is ctrl:
            return True

        def on_failed(sender, args):
            try:
                kind = str(getattr(args, "ProcessFailedKind", "") or "")
            except Exception:
                kind = ""
            _WEBVIEW_PROCESS_DEAD["flag"] = True
            log_window_event(f"界面进程异常退出（{kind}）；下次打开窗口会自动重建")

        core.ProcessFailed += on_failed
        _FAILED_WATCHED["ctrl"] = ctrl
        return True

    return bool(_webview2_on_ui(form, _hook).get("value"))


# 重建界面的锁：托盘/任务栏唤醒与「清理界面缓存」按钮可能同时动手
_WEBVIEW_LOCK = threading.Lock()


def clear_webview_cache_and_reload(window, url: str) -> dict:
    """「清理界面缓存」按钮的实体：拆界面进程 → 删缓存 → 重建界面。

    程序跑着的时候缓存目录被 WebView2 占着，直接删只会删掉一半，所以先把控件
    拆掉（进程全退、句柄放开）再删，最后重建 —— 用户看到的就是界面重新加载了
    一次。没有界面进程（浏览器访问）时只清缓存，由页面自己刷新。
    """
    with _WEBVIEW_LOCK:
        alive = webview2_control_alive(window) if window is not None else False
        if alive:
            _webview2_release(window)
            time.sleep(0.4)      # 等 WebView2 的进程把文件句柄放开
        freed = purge_webview_cache(force=True)
        rebuilt = bool(alive and url
                       and _webview2_rebuild(window, url, _webview_profile_dir()))
    if alive and not rebuilt:
        # 拆了却没装回来：先把窗口藏掉（别让用户盯着白屏），再重启一次程序
        log_window_event("清理缓存后界面没重建起来：先隐藏窗口，再重启一次程序")
        try:
            window.hide()
        except Exception:
            pass
        hook = _APP_HOOKS.get("relaunch")
        if hook is not None:
            hook()
        else:
            log_window_event("没有可用的重启入口，请手动退出后重开")
    return {"released": alive, "rebuilt": rebuilt, "freed": freed}
