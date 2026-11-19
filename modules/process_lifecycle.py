"""Ownership and bounded cleanup for processes launched by this application.

Existing services are never selected by name or port. Restart/install helpers
are explicit exceptions so they can wait for the old application to exit.
"""
import atexit
import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes

import psutil


_JOB_LOCK = threading.Lock()
_JOB_STATE = {"handle": None, "tried": False, "closed": False}

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_APPLICATION_JOB = None
_COMMAND_JOBS = {}


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _kernel32():
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    dll.CreateJobObjectW.restype = wintypes.HANDLE
    dll.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                            ctypes.c_void_p, wintypes.DWORD]
    dll.SetInformationJobObject.restype = wintypes.BOOL
    dll.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    dll.AssignProcessToJobObject.restype = wintypes.BOOL
    dll.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    dll.OpenProcess.restype = wintypes.HANDLE
    dll.CloseHandle.argtypes = [wintypes.HANDLE]
    dll.CloseHandle.restype = wintypes.BOOL
    dll.GetProcessTimes.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                    ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_void_p]
    dll.GetProcessTimes.restype = wintypes.BOOL
    return dll


def _job_handle():
    if os.name != "nt":
        return None
    with _JOB_LOCK:
        if _JOB_STATE["closed"] or _JOB_STATE["handle"] is not None or _JOB_STATE["tried"]:
            return _JOB_STATE["handle"]
        _JOB_STATE["tried"] = True
        try:
            k = _kernel32()
            handle = k.CreateJobObjectW(None, None)
            if not handle:
                return None
            info = _ExtendedLimitInformation()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k.SetInformationJobObject(handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                             ctypes.byref(info), ctypes.sizeof(info)):
                k.CloseHandle(handle)
                return None
            _JOB_STATE["handle"] = handle
            return handle
        except Exception:
            return None


def bind_to_job(pid: int) -> bool:
    handle = _job_handle()
    if handle is None or not pid:
        return False
    try:
        k = _kernel32()
        h = k.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid))
        if not h:
            return False
        try:
            return bool(k.AssignProcessToJobObject(handle, h))
        finally:
            k.CloseHandle(h)
    except Exception:
        return False


def _close_command_job(token):
    with _LOCK:
        handle = token.get("handle")
        token["handle"] = None
        _COMMAND_JOBS.pop(id(token), None)
        if handle is not None:
            _kernel32().CloseHandle(handle)


def _bind_command_job(proc, restrictions=None):
    """Keep each command's descendants reachable after its launcher exits."""
    kernel = _kernel32()
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        return False
    token = {"handle": handle}
    try:
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if restrictions:
            max_processes, memory_bytes = restrictions
            info.BasicLimitInformation.ActiveProcessLimit = max_processes
            info.JobMemoryLimit = memory_bytes
            # JOB_OBJECT_LIMIT_ACTIVE_PROCESS | JOB_OBJECT_LIMIT_JOB_MEMORY
            info.BasicLimitInformation.LimitFlags |= 0x00000008 | 0x00000200
        if not kernel.SetInformationJobObject(
                handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info)):
            return False
        if not kernel.AssignProcessToJobObject(handle, wintypes.HANDLE(int(proc._handle))):
            return False
        proc._lovomo_job = token
        _COMMAND_JOBS[id(token)] = token
        return True
    finally:
        if id(token) not in _COMMAND_JOBS:
            kernel.CloseHandle(handle)


def command_job_contains(proc, pid):
    """Check the real worker, since Windows launchers may spawn another process."""
    if os.name != "nt" or type(pid) is not int or pid <= 0:
        return False
    token = getattr(proc, "_lovomo_job", None)
    with _LOCK:
        job = token.get("handle") if token else None
        if not job:
            return False
        kernel = _kernel32()
        kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE,
                                         ctypes.POINTER(wintypes.BOOL)]
        kernel.IsProcessInJob.restype = wintypes.BOOL
        handle = kernel.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            member = wintypes.BOOL()
            return bool(kernel.IsProcessInJob(handle, job, ctypes.byref(member)) and member.value)
        finally:
            kernel.CloseHandle(handle)


def _reap_command_jobs():
    # BASIC_ACCOUNTING_INFORMATION ends with four DWORD counters. Querying an
    # empty job lets short-lived FFmpeg/ASR commands release their handles.
    class Accounting(ctypes.Structure):
        _fields_ = [(name, ctypes.c_longlong) for name in (
            "user", "kernel", "period_user", "period_kernel")] + [
            (name, wintypes.DWORD) for name in (
                "faults", "total", "active", "terminated")]
    kernel = _kernel32()
    kernel.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
        wintypes.DWORD, ctypes.c_void_p]
    kernel.QueryInformationJobObject.restype = wintypes.BOOL
    with _LOCK:
        for token in list(_COMMAND_JOBS.values()):
            info = Accounting()
            if kernel.QueryInformationJobObject(
                    token["handle"], 1, ctypes.byref(info), ctypes.sizeof(info), None
            ) and info.active == 0:
                _close_command_job(token)


def attach_application_job() -> bool:
    """Include raw/plugin/WebView children, retaining helpers via breakaway.

    This handle stays open until the OS closes the application. Explicitly
    closing it during cleanup would also terminate the current process.
    """
    global _APPLICATION_JOB
    if os.name != "nt":
        return False
    with _LOCK:
        if _APPLICATION_JOB is not None:
            return True
        kernel = _kernel32()
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            return False
        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | 0x00000800)  # BREAKAWAY_OK
        if not kernel.SetInformationJobObject(
                handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info)):
            kernel.CloseHandle(handle)
            return False
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        if not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
            kernel.CloseHandle(handle)
            return False
        _APPLICATION_JOB = handle
        return True


def process_start_time(pid: int):
    if os.name != "nt" or not pid:
        return None
    try:
        k = _kernel32()
        h = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return None
        try:
            created = wintypes.FILETIME()
            exited = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            ok = k.GetProcessTimes(h, ctypes.byref(created), ctypes.byref(exited),
                                   ctypes.byref(kernel), ctypes.byref(user))
            if not ok:
                return None
            return (created.dwHighDateTime << 32) | created.dwLowDateTime
        finally:
            k.CloseHandle(h)
    except Exception:
        return None


def _close_job() -> None:
    with _JOB_LOCK:
        handle = _JOB_STATE["handle"]
        _JOB_STATE["handle"] = None
        _JOB_STATE["closed"] = True
    if handle is not None:
        _kernel32().CloseHandle(handle)


_LOCK = threading.RLock()
_ENDING = threading.Event()
_OWNED = {}
_HELPERS = {}

def begin_exit() -> None:
    """Prevent new launches; this must never run for a TTS-only restart."""
    _ENDING.set()


def is_exiting() -> bool:
    return _ENDING.is_set()


def python_child_executable(env):
    """Use the real interpreter so the child remains in the assigned Windows job."""
    if os.name != "nt" or getattr(sys, "frozen", False):
        return sys.executable
    executable = psutil.Process().exe()
    if os.path.normcase(executable) != os.path.normcase(sys.executable):
        # Preserve the venv even when we bypass its executable redirector.
        env["__PYVENV_LAUNCHER__"] = sys.executable
    return executable


def _identity(pid):
    try:
        process = psutil.Process(pid)
        process.create_time()  # Pin identity before PID reuse becomes possible.
        return process
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def _live(process):
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return process.is_running()


def _children(process):
    try:
        return process.children(recursive=True) if _live(process) else []
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return []


def _remember(proc):
    process = _identity(proc.pid)
    if process is not None:
        # Completed CLI parents can leave children in the Windows job. Its
        # handle remains open until the application exits.
        for pid, old in list(_OWNED.items()):
            if not _live(old):
                _OWNED.pop(pid, None)
        _OWNED[process.pid] = process


def popen(*args, job_restrictions=None, **kwargs):
    """Launch an owned command and bind its future descendants to the job."""
    with _LOCK:
        if is_exiting():
            raise RuntimeError("程序正在退出，不能再启动子进程")
        if os.name == "nt":
            # Assign before the first instruction runs: fast CLI launchers can
            # otherwise spawn a detached model daemon and exit before binding.
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x08000000 | 0x00000004
            _reap_command_jobs()
        proc = subprocess.Popen(*args, **kwargs)
        try:
            _remember(proc)
            if os.name == "nt" and not _bind_command_job(proc, job_restrictions):
                if job_restrictions:
                    raise RuntimeError("无法限制插件进程资源，已停止启动")
                if proc.poll() is None:
                    print(f"[进程] PID {proc.pid} 未能绑定作业对象，退出时将扫描进程树回收")
            if os.name == "nt":
                _resume_process(proc.pid)
        except Exception:
            proc.kill()
            proc.wait()
            raise
        return proc


def _resume_process(pid):
    kernel = _kernel32()
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    threads = psutil.Process(pid).threads()
    if not threads:
        raise OSError("挂起的子进程没有可恢复的主线程")
    handle = kernel.OpenThread(0x0002, False, threads[0].id)  # THREAD_SUSPEND_RESUME
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if kernel.ResumeThread(handle) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(handle)


def spawn_detached(*args, **kwargs):
    """Launch only a restart/install helper and exclude its verified identity."""
    with _LOCK:
        if is_exiting():
            raise RuntimeError("程序正在退出，不能再启动助手")
        if os.name == "nt":
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x08000000
        if _APPLICATION_JOB is not None:
            kwargs["creationflags"] = kwargs.get("creationflags", 0) | 0x01000000
        proc = subprocess.Popen(*args, **kwargs)
        process = _identity(proc.pid)
        if process is not None:
            _HELPERS[process.pid] = process
        return proc


def _kill(processes):
    """Send every termination request before spending time waiting."""
    waiting = []
    for process in processes:
        try:
            if _live(process):
                process.kill()  # psutil verifies the creation time before kill.
                waiting.append(process)
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            print(f"[进程] 无权限关闭 PID {process.pid}，保留回收记录")
    return waiting


def stop_tree(proc, timeout=2.0):
    """Stop one owned service/task without ending the application's lifetime."""
    with _LOCK:
        process = _OWNED.get(proc.pid)
    if process is None:
        process = getattr(proc, "_identity", None)
    if process is None and proc.poll() is None:
        process = _identity(proc.pid)
    # Keep an identity snapshot before the job can terminate the launcher.
    targets = list(reversed(_children(process))) + [process] if process is not None else []
    token = getattr(proc, "_lovomo_job", None)
    if token is not None:
        _close_command_job(token)
    if process is None:
        return True
    waiting = _kill(targets)
    _, alive = psutil.wait_procs(waiting, timeout=max(0.0, timeout))
    try:
        proc.poll()
    except AttributeError:
        pass
    if not alive:
        with _LOCK:
            if _OWNED.get(process.pid) is process:
                _OWNED.pop(process.pid, None)
    return not alive and not any(_live(p) for p in targets)


def run(*popenargs, input=None, capture_output=False, timeout=None, check=False, **kwargs):
    """subprocess.run semantics, with tree cleanup on timeout and exceptions."""
    if input is not None:
        if kwargs.get("stdin") is not None:
            raise ValueError("stdin and input arguments may not both be used")
        kwargs["stdin"] = subprocess.PIPE
    if capture_output:
        if kwargs.get("stdout") is not None or kwargs.get("stderr") is not None:
            raise ValueError("stdout and stderr arguments may not be used with capture_output")
        kwargs["stdout"] = kwargs["stderr"] = subprocess.PIPE
    proc = popen(*popenargs, **kwargs)
    deferred = False
    try:
        try:
            stdout, stderr = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            stop_tree(proc, timeout=0)
            if os.name == "nt":
                try:
                    exc.stdout, exc.stderr = proc.communicate(timeout=0.5)
                except subprocess.TimeoutExpired:
                    # An inherited pipe can outlive the job. Closing a pipe with
                    # an active reader can also block, so finish off the caller.
                    deferred = True
                    threading.Thread(target=proc.__exit__, args=(None, None, None),
                                     name="lovomo-command-close", daemon=True).start()
            else:
                proc.wait()
            raise
        except BaseException:
            stop_tree(proc, timeout=0)
            raise
        code = proc.poll()
        if check and code:
            raise subprocess.CalledProcessError(code, proc.args, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(proc.args, code, stdout, stderr)
    finally:
        if not deferred:
            proc.__exit__(*sys.exc_info())


def shutdown_all_children(budget=2.0):
    """Final exit: close the job and reclaim all remaining app descendants.

    The waiting budget never prevents a kill request for a later process.
    Repeated or concurrent calls can retry failures without a long-held lock.
    """
    begin_exit()
    deadline = time.monotonic() + max(0.0, budget)
    with _LOCK:
        owned = list(_OWNED.values())
        helpers = list(_HELPERS.values())
    excluded = {p.pid for helper in helpers for p in [helper, *_children(helper)]
                if _live(p)}
    current = psutil.Process(os.getpid())
    targets = {p.pid: p for p in _children(current) if p.pid not in excluded}
    for process in owned:
        for child in [process, *_children(process)]:
            if child.pid not in excluded and child.pid != os.getpid():
                targets[child.pid] = child
    # Closing a kill-on-close job handles grandchildren even after their
    # original parent (for example lms load) has already exited.
    _close_job()
    with _LOCK:
        for token in list(_COMMAND_JOBS.values()):
            _close_command_job(token)
    waiting = _kill(reversed(list(targets.values())))
    _, alive = psutil.wait_procs(waiting, timeout=max(0.0, deadline - time.monotonic()))
    survivors = [p for p in targets.values() if _live(p)]
    with _LOCK:
        _OWNED.update({p.pid: p for p in survivors})
        for pid, process in list(_OWNED.items()):
            if not _live(process):
                _OWNED.pop(pid, None)
    if survivors:
        print("[进程] 退出回收仍有存活进程：" + ", ".join(str(p.pid) for p in survivors))
    return [p.pid for p in survivors]


atexit.register(shutdown_all_children)


