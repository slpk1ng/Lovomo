# -*- coding: utf-8 -*-

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
# 控件，不是窗口。


def _webview2_form(window):
    try:
        return getattr(window, "native", None)
    except Exception:
        return None


def _webview2_on_ui(form, fn) -> dict:
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
        props.AdditionalBrowserArguments = "--disable-features=ElasticOverscroll"
        ctrl = WebView2()
        ctrl.CreationProperties = props
        browser = getattr(form, "browser", None)
        if browser is not None:
            browser.webview = ctrl
        form.webview = ctrl
        ready = getattr(browser, "on_webview_ready", None)
        if ready is not None:
            def _on_ready(sender, args):
                try:
                    ready(sender, args)
                except Exception as e:
                    log_window_event(
                        f"接回界面初始化失败（界面仍可重建）：{type(e).__name__}: {e}")
            ctrl.CoreWebView2InitializationCompleted += _on_ready
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
    _WEBVIEW_PROCESS_DEAD["flag"] = False
    attach_process_failed_watch(window)
    return True


# ---------------------------------------------------------------------------
# WebView2 缓存
# ---------------------------------------------------------------------------

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
    form = _webview2_form(window)
    if form is None:
        return False
    ctrl = getattr(form, "webview", None)
    if ctrl is None:
        return False
    return bool(_webview2_on_ui(form, lambda: not bool(ctrl.IsDisposed)).get("value"))


_WEBVIEW_PROCESS_DEAD = {"flag": False}
_FAILED_WATCHED = {"ctrl": None}


def attach_process_failed_watch(window) -> bool:
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


_WEBVIEW_LOCK = threading.Lock()


def clear_webview_cache_and_reload(window, url: str) -> dict:
    with _WEBVIEW_LOCK:
        alive = webview2_control_alive(window) if window is not None else False
        if alive:
            _webview2_release(window)
            time.sleep(0.4)
        freed = purge_webview_cache(force=True)
        rebuilt = bool(alive and url
                       and _webview2_rebuild(window, url, _webview_profile_dir()))
    if alive and not rebuilt:
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
