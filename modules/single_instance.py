# -*- coding: utf-8 -*-
"""Windows 命名互斥体单实例锁与已运行实例唤起（自 main.py 原样搬迁）。"""
import os
import sys
import threading
from pathlib import Path

from modules.app_paths import get_resource_path
from modules.log_console import log_window_event


def _pid_alive(pid) -> bool:
    """跨平台判断 pid 对应的进程是否仍在运行。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                # 只有"权限不足"才说明进程还在；其余错误码（进程不存在）就是已退出
                ERROR_ACCESS_DENIED = 5
                return kernel32.GetLastError() == ERROR_ACCESS_DENIED
            try:
                code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                return bool(ok) and code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(h)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


# ---------------------------------------------------------------------------
# 单实例
# ---------------------------------------------------------------------------
# 程序已经在跑时用户又双击一次 exe，不应该再起一套进程：那会多出一个任务栏
# 条目、多连一份 NapCat、多占一次 WebUI 端口。这里用命名互斥体判定「已经有
# 实例在跑」，再用一个命名事件通知那个实例把窗口亮出来。
#
# 用互斥体而不是锁文件：进程被强杀时系统会自动释放，不会留下需要人工清理的
# 残留。名字带 Local\ 前缀，按登录会话隔离，多用户各自算一个实例。

_INSTANCE_MUTEX_NAME = "Local\\Lovomo.SingleInstance"
_INSTANCE_SHOW_EVENT_NAME = "Local\\Lovomo.ShowWindow"
_INSTANCE_STATE = {"mutex": None, "event": None, "waiter": False}


def _kernel32():
    """kernel32 句柄；必须开 use_last_error，否则读不到可靠的 GetLastError。"""
    import ctypes
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _acquire_single_instance() -> bool:
    """抢占单实例名额，并建好唤醒事件。

    返回 False 表示已有实例在跑，调用方应该通知它然后退出。非 Windows 或
    接口异常时一律返回 True —— 宁可多开一次，也不能让程序打不开。
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        k32 = _kernel32()
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                     wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                     wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateEventW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL

        mutex = k32.CreateMutexW(None, False, _INSTANCE_MUTEX_NAME)
        if not mutex:
            return True
        if ctypes.get_last_error() == 183:          # ERROR_ALREADY_EXISTS
            k32.CloseHandle(mutex)
            return False
        _INSTANCE_STATE["mutex"] = mutex
        # 手动重置事件：谁收到谁负责 ResetEvent，否则下一次启动会被上一次
        # 留下的信号立刻再唤醒一遍。
        _INSTANCE_STATE["event"] = k32.CreateEventW(
            None, True, False, _INSTANCE_SHOW_EVENT_NAME)
        return True
    except Exception:
        return True


def _signal_existing_instance() -> bool:
    """通知已经在跑的那个实例把窗口亮出来。"""
    if os.name != "nt":
        return False
    try:
        from ctypes import wintypes
        k32 = _kernel32()
        k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                   wintypes.LPCWSTR]
        k32.OpenEventW.restype = wintypes.HANDLE
        k32.SetEvent.argtypes = [wintypes.HANDLE]
        k32.SetEvent.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        EVENT_MODIFY_STATE = 0x0002
        handle = k32.OpenEventW(EVENT_MODIFY_STATE, False,
                                _INSTANCE_SHOW_EVENT_NAME)
        if not handle:
            return False
        try:
            return bool(k32.SetEvent(handle))
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return False


def _start_instance_show_waiter(on_show) -> None:
    """等「又有人双击了 exe」，收到就把窗口亮出来。"""
    if os.name != "nt" or _INSTANCE_STATE.get("waiter"):
        return
    handle = _INSTANCE_STATE.get("event")
    if not handle:
        return
    try:
        from ctypes import wintypes
        k32 = _kernel32()
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.ResetEvent.argtypes = [wintypes.HANDLE]
        k32.ResetEvent.restype = wintypes.BOOL
        _INSTANCE_STATE["waiter"] = True

        def _loop():
            # 半秒一轮而不是无限等待：进程退出时线程能自己收掉。
            while True:
                if k32.WaitForSingleObject(handle, 500) == 0:   # WAIT_OBJECT_0
                    k32.ResetEvent(handle)
                    try:
                        on_show()
                    except Exception as e:
                        log_window_event(f"唤醒窗口失败：{type(e).__name__}: {e}")

        threading.Thread(target=_loop, daemon=True).start()
    except Exception:
        pass


def _candidate_icon_paths() -> list:
    """按打包/开发环境查找托盘与窗口图标，不写死任何路径。"""
    cands = []
    try:
        cands.append(get_resource_path("icon.ico"))
    except Exception:
        pass
    try:
        cands.append(Path(sys.executable).parent / "icon.ico")
    except Exception:
        pass
    try:
        cands.append(Path.cwd() / "icon.ico")
    except Exception:
        pass
    out = []
    for p in cands:
        try:
            if p and Path(p).exists():
                out.append(str(Path(p).resolve()))
        except Exception:
            continue
    return out
