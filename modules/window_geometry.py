# -*- coding: utf-8 -*-
import json
import os
import threading
import time
from pathlib import Path

from modules.app_paths import _probe_writable, runtime_path, user_data_dir

_DPI_AWARE_DONE = False


# ============================================================================
# ============================================================================
#
#     像素除以 _scale（见 winforms.py:1066/1075 的 get_position/get_size）。
#
#
#
#       传给 create_window / move / resize 之前必须过 _phys_to_logical()。

_WINDOW_GEOMETRY_FILE = "window_geometry.json"
_GEOMETRY_VERSION = 2
_WINDOW_GEOMETRY = {
    "geometry_v": _GEOMETRY_VERSION,
    "maximized": True,
    "normal": {"x": None, "y": None, "width": 1280, "height": 720},
}
_geometry_save_timer = None
_geometry_lock = threading.Lock()


def _geometry_path() -> Path:
    return runtime_path(_WINDOW_GEOMETRY_FILE, Path(user_data_dir()))


def _webview_profile_dir() -> str:
    path = user_data_dir() / "webview"
    return str(path) if _probe_writable(path) else ""


# ---------------------------------------------------------------------------
# 物理像素 <-> pywebview 逻辑像素
# ---------------------------------------------------------------------------

def _window_scale(window=None) -> float:
    if os.name != "nt":
        return 1.0
    try:
        import ctypes
        if window is not None:
            hwnd = _window_hwnd(window)
            if hwnd:
                dpi = int(ctypes.windll.user32.GetDpiForWindow(hwnd))
                if dpi > 0:
                    return dpi / 96.0
        dpi = int(ctypes.windll.user32.GetDpiForSystem())
        if dpi > 0:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


def _window_hwnd(window) -> int:
    if window is None:
        return 0
    try:
        native = getattr(window, "native", None)
        if native is not None:
            handle = getattr(native, "Handle", None)
            if handle is not None:
                hwnd = int(handle.ToInt32())
                if hwnd:
                    return hwnd
    except Exception:
        pass
    return 0


def _phys_to_logical(value, scale: float) -> int:
    try:
        return int(round(float(value) / max(scale, 1e-6)))
    except (TypeError, ValueError):
        return int(value)


def _logical_to_phys(value, scale: float) -> int:
    try:
        return int(round(float(value) * max(scale, 1e-6)))
    except (TypeError, ValueError):
        return int(value)


# ---------------------------------------------------------------------------
# 原生窗口状态 / 几何读取
# ---------------------------------------------------------------------------
# 为什么不直接用 pywebview 的 window.state / window.width：
#   · window.state 返回的是 State(dict)（pywebview 的"状态字典"），

def _show_state(hwnd: int) -> int:
    """SW_SHOWNORMAL=1 / SW_SHOWMINIMIZED=2 / SW_SHOWMAXIMIZED=3，失败返回 0。"""
    if not hwnd or os.name != "nt":
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        class WINDOWPLACEMENT(ctypes.Structure):
            _fields_ = [("length", wintypes.UINT),
                        ("flags", wintypes.UINT),
                        ("showCmd", wintypes.UINT),
                        ("ptMinPosition", POINT),
                        ("ptMaxPosition", POINT),
                        ("rcNormalPosition", RECT)]

        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        if ctypes.windll.user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
            return int(wp.showCmd)
    except Exception:
        pass
    return 0


def _window_state_name(window) -> str:
    """"maximized" / "minimized" / "normal"（拿不到就返回 ""）。"""
    cmd = _show_state(_window_hwnd(window))
    if cmd == 3:
        return "maximized"
    if cmd == 2:
        return "minimized"
    if cmd == 1:
        return "normal"
    return ""


def _window_visible(window) -> bool:
    hwnd = _window_hwnd(window)
    if not hwnd or os.name != "nt":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.user32.IsWindowVisible(hwnd))
    except Exception:
        return True


def _window_hide(window) -> bool:
    from modules.webview_runtime import _webview2_form, _webview2_on_ui
    form = _webview2_form(window)
    if form is None:
        return False
    box = _webview2_on_ui(form, lambda: (form.Hide(), True)[1])
    if "err" in box:
        return False
    return not _window_visible(window)


def _normal_rect(window) -> dict:
    hwnd = _window_hwnd(window)
    if not hwnd or os.name != "nt":
        return {}
    try:
        import ctypes
        from ctypes import wintypes

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        class WINDOWPLACEMENT(ctypes.Structure):
            _fields_ = [("length", wintypes.UINT),
                        ("flags", wintypes.UINT),
                        ("showCmd", wintypes.UINT),
                        ("ptMinPosition", POINT),
                        ("ptMaxPosition", POINT),
                        ("rcNormalPosition", RECT)]

        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        if not ctypes.windll.user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
            return {}
        r = wp.rcNormalPosition
        w = int(r.right - r.left)
        h = int(r.bottom - r.top)
        if w >= 200 and h >= 150:
            return {"x": int(r.left), "y": int(r.top), "width": w, "height": h}
    except Exception:
        pass
    return {}


def _window_rect(window) -> dict:
    hwnd = _window_hwnd(window)
    if not hwnd or os.name != "nt":
        return {}
    try:
        import ctypes
        from ctypes import wintypes

        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        r = RECT()
        if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return {}
        w = int(r.right - r.left)
        h = int(r.bottom - r.top)
        if w >= 200 and h >= 150:
            return {"x": int(r.left), "y": int(r.top), "width": w, "height": h}
    except Exception:
        pass
    return {}


def _default_normal_geometry() -> dict:
    sw, sh = _primary_screen_size()
    if sw <= 0 or sh <= 0:
        return {"x": None, "y": None, "width": 1280, "height": 720}
    w = int(sw * 0.8)
    h = int(sh * 0.8)
    return {"x": (sw - w) // 2, "y": (sh - h) // 2, "width": w, "height": h}


def _load_window_geometry() -> dict:
    default_normal = _default_normal_geometry()
    try:
        raw = json.loads(_geometry_path().read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("geometry 不是 dict")
        if int(raw.get("geometry_v") or 1) != _GEOMETRY_VERSION:
            print("检测到旧版窗口位置记录（DPI 口径不兼容），本次按默认最大化打开。")
            raise ValueError("geometry_v mismatch")
        normal = raw.get("normal") if isinstance(raw.get("normal"), dict) else {}
        return {
            "geometry_v": _GEOMETRY_VERSION,
            "maximized": bool(raw.get("maximized", True)),
            "normal": {
                "x": normal.get("x"),
                "y": normal.get("y"),
                "width": int(normal.get("width") or default_normal["width"]),
                "height": int(normal.get("height") or default_normal["height"]),
            },
        }
    except Exception:
        return {"geometry_v": _GEOMETRY_VERSION,
                "maximized": True,
                "normal": dict(default_normal)}


def _save_window_geometry() -> None:
    try:
        _WINDOW_GEOMETRY["geometry_v"] = _GEOMETRY_VERSION
        data = json.loads(json.dumps(_WINDOW_GEOMETRY))
        from modules.jsonio import save_json
        save_json(_geometry_path(), data)
    except Exception as e:
        print(f"保存窗口位置失败（下次启动按默认最大化打开）：{e}")


def _schedule_geometry_save(delay: float = 1.2) -> None:
    global _geometry_save_timer

    def _run():
        global _geometry_save_timer
        with _geometry_lock:
            _geometry_save_timer = None
            _save_window_geometry()

    with _geometry_lock:
        if _geometry_save_timer is not None:
            try:
                _geometry_save_timer.cancel()
            except Exception:
                pass
        _geometry_save_timer = threading.Timer(delay, _run)
        _geometry_save_timer.daemon = True
        _geometry_save_timer.start()


def _finish_geometry_save() -> None:
    global _geometry_save_timer
    with _geometry_lock:
        if _geometry_save_timer is not None:
            try:
                _geometry_save_timer.cancel()
            except Exception:
                pass
            _geometry_save_timer = None
        _save_window_geometry()


def _is_geometry_valid(geom: dict) -> bool:
    try:
        x, y = geom.get("x"), geom.get("y")
        w = int(geom.get("width") or 0)
        h = int(geom.get("height") or 0)
        if w < 400 or h < 300:
            return False
        if x is None or y is None:
            return False
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        return False
    try:
        screens = _screen_rects()
    except Exception:
        screens = []
    if not screens:
        return x > -10000 and y > -10000
    for (sx, sy, sw, sh) in screens:
        if x + w > sx + 40 and x < sx + sw - 40 and y + 40 < sy + sh and y + h > sy:
            return True
    return False


def _rect_is_degenerate(rect: dict) -> bool:
    try:
        w = int(rect.get("width") or 0)
        h = int(rect.get("height") or 0)
    except (TypeError, ValueError):
        return True
    return w < 640 or h < 480


def _geom_looks_like_fullscreen(geom: dict) -> bool:
    try:
        w = int(geom.get("width") or 0)
        h = int(geom.get("height") or 0)
    except (TypeError, ValueError):
        return False
    if w < 400 or h < 300:
        return False
    sw, sh = _primary_screen_size()
    if sw <= 0 or sh <= 0:
        return False
    return w >= sw * 0.97 and h >= sh * 0.97


def _sanitize_normal_geometry(geom: dict) -> dict:
    fallback = _default_normal_geometry()
    if not isinstance(geom, dict) or not geom:
        return dict(fallback)
    try:
        w = int(geom.get("width") or fallback["width"])
        h = int(geom.get("height") or fallback["height"])
    except (TypeError, ValueError):
        return dict(fallback)
    cleaned = {"x": geom.get("x"), "y": geom.get("y"),
               "width": w, "height": h}
    if (_rect_is_degenerate(cleaned) or _geom_looks_like_fullscreen(cleaned)
            or not _is_geometry_valid(cleaned)):
        return {"x": None, "y": None, "width": w, "height": h}
    return cleaned


def _ensure_dpi_aware() -> None:
    global _DPI_AWARE_DONE
    if os.name != "nt" or _DPI_AWARE_DONE:
        return
    _DPI_AWARE_DONE = True
    try:
        import ctypes
        user32 = ctypes.windll.user32
        try:
            # -4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                return
        except Exception:
            pass
        try:
            # 1 = PROCESS_SYSTEM_DPI_AWARE（Win8.1+）
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            return
        except Exception:
            pass
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    except Exception:
        pass


def _screen_rects() -> list:
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        _ensure_dpi_aware()
        user32 = ctypes.windll.user32

        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        monitors = []

        MonitorEnumProc = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.POINTER(RECT), ctypes.c_double)

        def _cb(hmon, hdc, lprc, data):
            r = lprc.contents
            monitors.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
            return 1

        user32.EnumDisplayMonitors(0, 0, MonitorEnumProc(_cb), 0)
        return monitors
    except Exception:
        return []


def _primary_screen_size() -> tuple:
    try:
        import ctypes
        _ensure_dpi_aware()
        w = int(ctypes.windll.user32.GetSystemMetrics(0))
        h = int(ctypes.windll.user32.GetSystemMetrics(1))
        return (w, h) if w > 0 and h > 0 else (0, 0)
    except Exception:
        return (0, 0)


def _apply_saved_geometry(window) -> None:
    if window is None:
        return
    hwnd = _window_hwnd(window)
    if hwnd:
        try:
            import ctypes
            user32 = ctypes.windll.user32
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return
        except Exception:
            pass
    geom = _load_window_geometry()
    try:
        if geom.get("maximized"):
            window.maximize()
            return
        normal = geom.get("normal") or {}
        w = int(normal.get("width") or 0)
        h = int(normal.get("height") or 0)
        if w >= 400 and h >= 300:
            scale = _window_scale(window)
            x, y = normal.get("x"), normal.get("y")
            lw = _phys_to_logical(w, scale)
            lh = _phys_to_logical(h, scale)
            if _is_geometry_valid(normal) and x is not None and y is not None:
                window.move(_phys_to_logical(x, scale),
                            _phys_to_logical(y, scale))
            window.resize(lw, lh)
    except Exception as e:
        print(f"恢复窗口位置失败（按默认最大化打开）：{e}")
        try:
            window.maximize()
        except Exception:
            pass


def _raise_to_foreground(window) -> None:
    if window is None:
        return
    try:
        import ctypes
        hwnd = _window_hwnd(window)
        if not hwnd:
            return
        user32 = ctypes.windll.user32
        SW_RESTORE = 9
        SW_SHOW = 5
        SW_SHOWNA = 8

        def _was_minimized() -> bool:
            try:
                return bool(user32.IsIconic(hwnd))
            except Exception:
                return False

        def _grab() -> None:
            try:
                user32.BringWindowToTop(hwnd)
            except Exception:
                pass
            try:
                fg = int(user32.GetForegroundWindow() or 0)
                cur = int(ctypes.windll.kernel32.GetCurrentThreadId())
                tgt = int(user32.GetWindowThreadProcessId(hwnd, None) or 0)
                attached = False
                if fg and tgt and cur and cur != tgt:
                    attached = bool(user32.AttachThreadInput(cur, tgt, True))
                try:
                    user32.SetForegroundWindow(hwnd)
                finally:
                    if attached:
                        user32.AttachThreadInput(cur, tgt, False)
            except Exception:
                pass
            try:
                user32.SetActiveWindow(hwnd)
            except Exception:
                pass

        def _ok() -> bool:
            try:
                return (bool(user32.IsWindowVisible(hwnd))
                        and not bool(user32.IsIconic(hwnd)))
            except Exception:
                return True

        for attempt in range(2):
            minimized = _was_minimized()
            # 1) 先让 pywebview 层把窗口弄出来（Show + Activate）
            try:
                window.show()
            except Exception:
                pass
            # 2) 原生层还原 + 抢前台
            try:
                user32.ShowWindow(hwnd, SW_RESTORE if minimized else SW_SHOW)
            except Exception:
                pass
            _grab()
            if _ok():
                return
            if attempt == 0:
                try:
                    user32.ShowWindow(hwnd, SW_RESTORE)
                except Exception:
                    pass
                try:
                    user32.ShowWindow(hwnd, SW_SHOWNA)
                except Exception:
                    pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 任务栏唤醒收敛
# ---------------------------------------------------------------------------
# 为什么需要这一段：
#   ShowWindow(SW_RESTORE) 并把 WM_ACTIVATEAPP / WM_ACTIVATE 发给窗口，
#
#   WinForms 的 Activate() 只做 SetForegroundWindow，而 Windows 的前台锁
#

_WNDPROC_HOLDER = {"old": None, "hwnd": 0, "callback": None, "active": False}


def _install_taskbar_activate_hook(window, on_activate=None) -> bool:
    if os.name != "nt" or window is None:
        return False
    if _WNDPROC_HOLDER.get("active") and _WNDPROC_HOLDER.get("hwnd"):
        return True
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = _window_hwnd(window)
        if not hwnd:
            return False
        user32 = ctypes.windll.user32

        WM_ACTIVATE = 0x0006
        WM_ACTIVATEAPP = 0x001C
        GWLP_WNDPROC = -4

        LRESULT = ctypes.c_ssize_t

        WNDPROC = ctypes.WINFUNCTYPE(
            LRESULT, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM)

        if ctypes.sizeof(ctypes.c_void_p) == 8:
            setter = user32.SetWindowLongPtrW
            setter.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            setter.restype = ctypes.c_void_p
        else:
            setter = user32.SetWindowLongW
            setter.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
            setter.restype = wintypes.LONG

        call_proc = user32.CallWindowProcW
        call_proc.argtypes = [ctypes.c_void_p, wintypes.HWND, ctypes.c_uint,
                              wintypes.WPARAM, wintypes.LPARAM]
        call_proc.restype = LRESULT

        def _proc(h, msg, wparam, lparam):
            try:
                if msg in (WM_ACTIVATE, WM_ACTIVATEAPP):
                    # WM_ACTIVATE 的 wParam 低字 == WA_ACTIVE(1) / WA_CLICKACTIVE(2)
                    # WM_ACTIVATEAPP 的 wParam != 0 表示应用被激活。
                    activated = (msg == WM_ACTIVATEAPP and wparam) or \
                                (msg == WM_ACTIVATE and (wparam & 0xFFFF) != 0)
                    if activated:
                        cb = on_activate or _raise_to_foreground
                        def _kick(win=window, fn=cb):
                            try:
                                time.sleep(0.02)
                                fn(win)
                            except Exception:
                                pass
                        threading.Thread(target=_kick, daemon=True).start()
            except Exception:
                pass
            return call_proc(_WNDPROC_HOLDER.get("old") or 0,
                             h, msg, wparam, lparam)

        cb = WNDPROC(_proc)
        old = setter(hwnd, GWLP_WNDPROC, ctypes.cast(cb, ctypes.c_void_p))
        if not old:
            return False
        if old == ctypes.cast(cb, ctypes.c_void_p).value:
            return False
        _WNDPROC_HOLDER["old"] = old
        _WNDPROC_HOLDER["hwnd"] = hwnd
        _WNDPROC_HOLDER["setter"] = setter
        _WNDPROC_HOLDER["callback"] = cb      # 必须持有引用，否则会被 GC 掉
        _WNDPROC_HOLDER["active"] = True
        return True
    except Exception as e:
        print(f"安装任务栏唤醒钩子失败（不影响其他功能）: {e}")
        return False


def _uninstall_taskbar_activate_hook() -> None:
    if not _WNDPROC_HOLDER.get("active"):
        return
    try:
        import ctypes
        hwnd = _WNDPROC_HOLDER.get("hwnd") or 0
        old = _WNDPROC_HOLDER.get("old") or 0
        setter = _WNDPROC_HOLDER.get("setter")
        if hwnd and old and setter is not None:
            setter(hwnd, -4, old)
        elif hwnd and old:
            user32 = ctypes.windll.user32
            if ctypes.sizeof(ctypes.c_void_p) == 8:
                user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                     ctypes.c_void_p]
                user32.SetWindowLongPtrW.restype = ctypes.c_void_p
                user32.SetWindowLongPtrW(hwnd, -4, old)
            else:
                user32.SetWindowLongW(hwnd, -4, old)
    except Exception:
        pass
    finally:
        _WNDPROC_HOLDER.update({"old": None, "hwnd": 0, "setter": None,
                                "callback": None, "active": False})
