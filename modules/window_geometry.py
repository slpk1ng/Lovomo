# -*- coding: utf-8 -*-
"""窗口几何记忆/校验/恢复、DPI 感知与任务栏唤醒钩子（自 main.py 原样搬迁）。"""
import json
import os
import threading
import time
from pathlib import Path

from modules.app_paths import _probe_writable, runtime_path, user_data_dir

# 进程级 DPI 感知只声明一次（原 main.py 中为隐式全局，迁入后改为模块内普通变量）
_DPI_AWARE_DONE = False


# ============================================================================
# 窗口几何记忆（位置/大小/是否最大化）
# ============================================================================
# 点任务栏图标或托盘「打开 Lovomo」还原窗口时，本来最大化的窗口
# 会被还原成一个往右下角偏移的小窗口；另一次是「打开只有一个很小的窗口」。
# 两个现象同源：坐标被 DPI 二次缩放。
#
# 这条链路要绝对小心，pywebview 的 winforms 后端在 DPI 处理上是自相矛盾的：
#   · create_window(width, height, x, y) 被当成「逻辑像素」，后端会乘 _scale
#     转成物理像素再交给 WinForms（见 winforms.py:209/217）。
#   · window.width / height / x / y 读回来的已经是「逻辑像素」，后端把物理
#     像素除以 _scale（见 winforms.py:1066/1075 的 get_position/get_size）。
#
# 所以只要把读到的值原样写回去，就会被再乘一次 _scale —— 125% 缩放下
# 1721×926 会变成 2151×1157，2560×1440 的屏幕装不下，看起来就是"小窗口
# 跑到奇怪的位置"，而最大化状态也在往返中丢掉了。
#
# 解决方式：本项目在 _screen_rects() 里调了 SetProcessDPIAware()（见下方），
# 进程已是 DPI 感知的，系统不会替我们虚拟化任何坐标 —— 于是干脆全程统一用
# **物理像素**：自己读系统 API 拿矩形，自己除/乘缩放系数，写回时再折算成
# pywebview 需要的逻辑像素。这样无论 DPI 是多少都只缩放一次。
#
# 所有落在 _WINDOW_GEOMETRY["normal"] 里的值都是物理像素，
#       传给 create_window / move / resize 之前必须过 _phys_to_logical()。

_WINDOW_GEOMETRY_FILE = "window_geometry.json"
# 几何数据的口径版本。老版本把 pywebview 的「逻辑像素」当物理像素存了，
# 在 125%/150% 缩放下这份数据是坏的（会被再缩放一次，窗口超出屏幕）。
# 升到 v2 后旧文件一律丢弃，避免用户升级后仍被脏数据坑一次。
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
    """WebView2 的站点数据目录；固定下来，否则每次启动都是临时目录，localStorage 留不住。

    目录不可写时返回空串，让 pywebview 用自己的默认位置（仍然是持久化的）。
    """
    path = user_data_dir() / "webview"
    return str(path) if _probe_writable(path) else ""


# ---------------------------------------------------------------------------
# 物理像素 <-> pywebview 逻辑像素
# ---------------------------------------------------------------------------

def _window_scale(window=None) -> float:
    """当前进程的 DPI 缩放系数（1.0 = 100%，1.25 = 125%）。

    已经 SetProcessDPIAware 的进程里 GetDpiForSystem() 拿到的就是真实值。
    取不到时返回 1.0，此时物理==逻辑，全部换算退化成恒等，不会出错。
    """
    if os.name != "nt":
        return 1.0
    try:
        import ctypes
        # 优先问窗口自己（多显示器下每个屏可能不同）
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
    """从 pywebview Window 上挖出原生 HWND（挖不到返回 0）。

    正常路径是 window.native.Handle；有些后端/时序下 window.native 还没挂上，
    那就退回用标题 EnumWindows 找一遍。拿不到不是错误，调用方都有兜底。
    """
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
    """物理像素 -> pywebview 逻辑像素（写回给 pywebview 时用）。"""
    try:
        return int(round(float(value) / max(scale, 1e-6)))
    except (TypeError, ValueError):
        return int(value)


def _logical_to_phys(value, scale: float) -> int:
    """pywebview 逻辑像素 -> 物理像素（读回 pywebview 属性时用）。"""
    try:
        return int(round(float(value) * max(scale, 1e-6)))
    except (TypeError, ValueError):
        return int(value)


# ---------------------------------------------------------------------------
# 原生窗口状态 / 几何读取
# ---------------------------------------------------------------------------
# 为什么不直接用 pywebview 的 window.state / window.width：
#   · window.state 返回的是 State(dict)（pywebview 的"状态字典"），
#     str() 出来是 "{}"，永远不会等于 "maximized" —— 拿它判断最大化必错。
#   · window.width/height/x/y 是逻辑像素且会 wait(15) 阻塞，不如直接问系统。
# 所以这里用 GetWindowPlacement + GetWindowRect 直接读，单位统一物理像素。

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
    """窗口当前是否可见。

    拿不到 HWND（非 Windows / 句柄还没建好）时按"可见"处理：调用方都在
    "确认已藏起来"的语义上用它，误判成已隐藏比误判成没隐藏更危险。
    """
    hwnd = _window_hwnd(window)
    if not hwnd or os.name != "nt":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.user32.IsWindowVisible(hwnd))
    except Exception:
        return True


def _window_hide(window) -> bool:
    """在 UI 线程把 WinForms 窗体隐藏（form.Hide），并同步原生可见性。

    直接 ShowWindow(SW_HIDE) 只改了原生窗口，WinForms 的 Form.Visible 仍是
    True，下一个重绘/焦点消息就会把窗口又拉回来 —— 这正是「点一次没藏住、
    还得再点一次」的根因。必须让 WinForms 自己把 Visible 置 False，原生层
    才会一致。
    """
    from modules.webview_runtime import _webview2_form, _webview2_on_ui
    form = _webview2_form(window)
    if form is None:
        return False
    box = _webview2_on_ui(form, lambda: (form.Hide(), True)[1])
    if "err" in box:
        return False
    return not _window_visible(window)


def _normal_rect(window) -> dict:
    """「还原后」该占的矩形（物理像素），即 GetWindowPlacement 的
    rcNormalPosition —— 最大化/最小化时它仍保留着 Normal 尺寸，正是我们要的。

    这条路径不受最大化动画影响，所以即使还原发生在一瞬间也能拿到正确的值。
    """
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
    """窗口当前实际矩形（物理像素），GetWindowRect 直接用不缩放。"""
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
    """没有可用记忆时的默认 Normal 矩形（物理像素）：屏幕的 80%，居中。

    不要硬编码 1920×1080 —— 在 150% 缩放下那是 2880 物理像素，比 2560 的
    屏幕还宽，窗口一开就超出屏幕被系统重新摆放，看起来就是"小窗口跑偏"。
    """
    sw, sh = _primary_screen_size()
    if sw <= 0 or sh <= 0:
        return {"x": None, "y": None, "width": 1280, "height": 720}
    w = int(sw * 0.8)
    h = int(sh * 0.8)
    return {"x": (sw - w) // 2, "y": (sh - h) // 2, "width": w, "height": h}


def _load_window_geometry() -> dict:
    """读取上次的窗口几何。任何异常都回落到默认（最大化）。

    口径版本不匹配（老数据是逻辑像素当物理像素存的）时直接丢弃，
    返回默认最大化 —— 宁可回到「默认最大化」也不要被脏数据带到错误位置。
    """
    default_normal = _default_normal_geometry()
    try:
        raw = json.loads(_geometry_path().read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("geometry 不是 dict")
        if int(raw.get("geometry_v") or 1) != _GEOMETRY_VERSION:
            # v1 数据：逻辑/物理像素口径混乱，不可信
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
    """落盘当前几何。写失败只是下次还原不准，不该影响运行。"""
    try:
        _WINDOW_GEOMETRY["geometry_v"] = _GEOMETRY_VERSION
        data = json.loads(json.dumps(_WINDOW_GEOMETRY))
        # 延迟定时器线程与退出流程会先后写同一份文件，必须走统一原子写
        # （临时名唯一），否则可能互相覆盖出半截 JSON
        from modules.jsonio import save_json
        save_json(_geometry_path(), data)
    except Exception as e:
        print(f"保存窗口位置失败（下次启动按默认最大化打开）：{e}")


def _schedule_geometry_save(delay: float = 1.2) -> None:
    """拖动/缩放窗口会高频触发事件，合并成一次延迟落盘，避免频繁写盘。"""
    global _geometry_save_timer

    def _run():
        global _geometry_save_timer
        with _geometry_lock:
            _geometry_save_timer = None
            # 落盘也在锁里：退出时的同步保存可能与这里并发，两份快照会互相覆盖
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
    """退出/重启前同步落盘：取消待执行的定时器，立刻写一次。

    退出流程随时可能被 _force_quit 打断，异步定时器不保证跑得到，
    所以这里必须同步写完再往下走。
    """
    global _geometry_save_timer
    with _geometry_lock:
        if _geometry_save_timer is not None:
            try:
                _geometry_save_timer.cancel()
            except Exception:
                pass
            _geometry_save_timer = None
        # 与延迟定时器的保存串行：否则最后写盘的可能是一份更早的快照
        _save_window_geometry()


def _is_geometry_valid(geom: dict) -> bool:
    """校验记忆下来的矩形是否还在当前屏幕范围内。**入参是物理像素。**

    换显示器、改分辨率、拔掉外接屏之后，旧坐标可能落在屏幕外；
    这种矩形要丢弃，否则窗口会「打开后看不见」。
    """
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
        # 窗口标题栏必须至少有 40px 落在某个屏幕内，否则用户抓不到窗口
        if x + w > sx + 40 and x < sx + sw - 40 and y + 40 < sy + sh and y + h > sy:
            return True
    return False


def _rect_is_degenerate(rect: dict) -> bool:
    """这个矩形小得不正常？小到一定程度就说明读到的是未初始化的窗口。

    pywebview 的默认 min_size 是 (200,100)。我们要防止把这种「最小尺寸」
    当成用户真实的窗口尺寸存下来 —— 一旦存进去，下次启动窗口就只有
    200x100，用户看到的就是"打开只有一个很小的窗口"。
    """
    try:
        w = int(rect.get("width") or 0)
        h = int(rect.get("height") or 0)
    except (TypeError, ValueError):
        return True
    return w < 640 or h < 480


def _geom_looks_like_fullscreen(geom: dict) -> bool:
    """这份 Normal 矩形是不是「铺满整块屏幕」——通常意味着它是最大化时记的。

    遇到这种就说明历史数据被 DPI 缩放污染过（最大化记成了 Normal），
    留着只会让窗口开成全屏尺寸但状态是 Normal，一眼"没最大化"。丢弃它、
    回落成默认最大化，比照着它还原更接近用户预期。
    """
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
    # 宽高都到屏幕的 97% 以上就认定是全屏矩形
    return w >= sw * 0.97 and h >= sh * 0.97


def _sanitize_normal_geometry(geom: dict) -> dict:
    """把明显不合理的 Normal 矩形清成「无坐标、用屏幕相对的默认尺寸」。

    清空坐标后调用方会走「系统居中」，不会出现小窗或幽灵位置。
    """
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
    """让本进程成为 DPI 感知，且必须「在 pywebview 创建窗口之前」就生效。

    注意：这里绝不能再调 SetProcessDPIAware()。
    pywebview 的 winforms 后端靠 GetDpiForWindow 自己算 _scale，再把
    create_window 的「逻辑像素」参数乘成物理像素。如果本进程没有 DPI 感知，
    Windows 会把窗口坐标「虚拟化」一遍，和 pywebview 的乘法叠加，最终尺寸
    被缩放两次。所以这里只声明感知、把缩放职责完整交给 pywebview。

    优先用 Per-Monitor-V2（GetDpiForWindow 才有意义），进程启动早期调用；
    太晚调用（已创建过任何窗口）会失败，失败就退回 System 级感知。
    """
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
    """枚举各显示器的 (x, y, width, height)，单位：物理像素。

    单位是物理像素这件事很重要 —— 落盘的 _WINDOW_GEOMETRY 全用物理像素，
    换算成 pywebview 的逻辑像素只发生在喂给 create_window/move/resize 之前。
    """
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
    """主屏的 (width, height)，单位物理像素。取不到返回 (0, 0)。"""
    try:
        import ctypes
        _ensure_dpi_aware()
        w = int(ctypes.windll.user32.GetSystemMetrics(0))
        h = int(ctypes.windll.user32.GetSystemMetrics(1))
        return (w, h) if w > 0 and h > 0 else (0, 0)
    except Exception:
        return (0, 0)


def _apply_saved_geometry(window) -> None:
    """把记忆的几何应用到窗口上，替代 pywebview 自身的隐式还原。

    记忆里存的是**物理像素**，pywebview 的 move/resize 收的是**逻辑像素**，
    所以这里必须过一遍 _phys_to_logical()，否则 125%/150% 缩放下窗口会被
    放大到超出屏幕（这就是"打开只有一个很小的窗口/跑到右下角"的根源）。

    只在窗口已经「露过面」时才应用：pywebview 的 move/resize 会把隐藏的窗口
    显示出来，但**不会把它带成前台**（实测 vis=True / fg=False），
    这种"露出来但压在别的窗口后面"的状态正是"点了托盘没反应"的观感来源。
    窗口还藏着/最小化时直接交给调用方的 _raise_to_foreground 处理，
    这里不做几何调整，免得先露出一个没抢到前台的窗口。
    """
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
    """把窗口提到最前台。

    pywebview 的 show() 只做到 Show+Activate（winforms.py:494），
    Windows 在前台锁（foreground lock）下会直接忽略它 —— 表现就是
    "最小化/放到后台后，点任务栏图标或托盘『打开 Lovomo』没反应"。
    这里补上原生调用：先把最小化状态还原，再抢前台。

    实测（150% 缩放、winforms 后端）必须遵守的顺序：
      · **先判断 IsIconic，再动窗口。** 最小化时 window.show() 会把前台
        抢过去却把窗口留在最小化状态（fg=True 但 iconic 仍为 True），
        画面不会有任何变化，用户看到的就是"点了没反应"。
        所以最小化判定必须发生在 show() 之前。
      · 最小化一律走 ShowWindow(SW_RESTORE)，其余情况才用 SW_SHOW。
      · 结束时校验一次 IsWindowVisible + IsIconic，没达标就重试一轮，
        避免前台锁/动画竞态导致这一次调用被静默吞掉。
    """
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
            # SetForegroundWindow 在前台锁下会失败，先用 AttachThreadInput 把
            # 当前前台线程的输入队列挂到自己身上，成功率明显更高。
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
                # 第一轮没达标：窗口可能卡在最小化或前台锁里，
                # 直接用原生 SW_RESTORE 再拉一次（不走 pywebview）。
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
#   Windows 从任务栏还原窗口走的是**系统自己的**路径（用户点任务栏缩略图、
#   右键任务栏条目选「还原」，或点任务栏图标）。系统会直接调
#   ShowWindow(SW_RESTORE) 并把 WM_ACTIVATEAPP / WM_ACTIVATE 发给窗口，
#   整个过程不经过 pywebview，也不经过我们的 open_console()。
#
#   问题就在这里：窗口被还原了、也"激活"了，但**没有抢到前台 Z 序**。
#   WinForms 的 Activate() 只做 SetForegroundWindow，而 Windows 的前台锁
#   （foreground lock）规定：只有当调用方线程就是当前前台线程、或收到用户
#   输入时，SetForegroundWindow 才真的生效。从任务栏还原时前台线程是
#   explorer.exe，我们的调用会被静默忽略 —— 表现就是"点了任务栏，窗口
#   出来了但还压在别的窗口后面，没显示在最前面"。
#
#   解法：子类化窗口过程（SetWindowLongPtr GWLP_WNDPROC），拦下
#   WM_ACTIVATEAPP / WM_ACTIVATE 这两个"我被激活了"的消息，在消息处理链
#   里立刻用 _raise_to_foreground 再抢一次前台。因为此时系统已经把我们
#   标记成正在激活的窗口，SetForegroundWindow 的通过率显著高于事后调用。

_WNDPROC_HOLDER = {"old": None, "hwnd": 0, "callback": None, "active": False}


def _install_taskbar_activate_hook(window, on_activate=None) -> bool:
    """给窗口装一个原生窗口过程钩子，拦截任务栏还原时的激活消息。

    必须在 **窗口所在线程** 调用（WinForms 的窗口线程）。装上后，任何一次
    WM_ACTIVATEAPP/WM_ACTIVATE 都会触发 on_activate（默认 _raise_to_foreground），
    从而把窗口真正提到最前面。

    重复调用是幂等的；拿不到 HWND 或非 Windows 时返回 False，调用方忽略即可
    （退化为没有钩子，行为与改动前一致）。

    ⚠ 这里每一个 ctypes 入口都必须显式声明 argtypes/restype。窗口过程是
    64 位指针进出的原生回调，任何一处按默认 c_int 传参/返回都会把高 32 位
    截断 —— 后果不是"钩子失效"这么轻，而是消息链（含 WM_CLOSE）转发失败，
    表现为任务栏图标单击/右键全无反应、任务栏「关闭窗口」关不掉程序。
    """
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

        # LRESULT / LONG_PTR 在 64 位下都是 8 字节；用 c_ssize_t 表达才与
        # 原生 ABI 一致（写 c_long 在 Windows 上只有 4 字节）。
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
                    # 表示窗口拿到了激活；WA_INACTIVE(0) 是失去激活，不管。
                    # WM_ACTIVATEAPP 的 wParam != 0 表示应用被激活。
                    activated = (msg == WM_ACTIVATEAPP and wparam) or \
                                (msg == WM_ACTIVATE and (wparam & 0xFFFF) != 0)
                    if activated:
                        cb = on_activate or _raise_to_foreground
                        # 不能在窗口过程里同步做重活（会重入/卡消息循环），
                        # 丢一个短延迟线程出去，等消息返回后再抢前台。
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
            # 理论上不会发生（原过程不可能就是我们的回调），但真出现了
            # 说明取到的是回调自身，再往下会自递归死循环，直接放弃。
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
    """还原原始窗口过程。退出前调用，避免进程退出时回调悬空。"""
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
