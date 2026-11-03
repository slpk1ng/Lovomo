# -*- coding: utf-8 -*-
import os
import sys
import threading
from pathlib import Path

from modules.app_paths import get_resource_path
from modules.log_console import log_window_event


def _pid_alive(pid) -> bool:
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
#

_INSTANCE_MUTEX_NAME = "Local\\Lovomo.SingleInstance"
_INSTANCE_SHOW_EVENT_NAME = "Local\\Lovomo.ShowWindow"
_INSTANCE_STATE = {"mutex": None, "event": None, "waiter": False}


def _kernel32():
    import ctypes
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _acquire_single_instance() -> bool:
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
        _INSTANCE_STATE["event"] = k32.CreateEventW(
            None, True, False, _INSTANCE_SHOW_EVENT_NAME)
        return True
    except Exception:
        return True


def _signal_existing_instance() -> bool:
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
