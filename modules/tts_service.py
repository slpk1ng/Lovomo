import ctypes
import json
import os
import re
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import yaml_lite
from .tls import verified_context
from .tts_cloud import is_cloud_tts


def no_window_kwargs() -> dict:
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}


TTS_READY_TIMEOUT_SECONDS = 300
TTS_READY_POLL_SECONDS = 5

_tts_child_log = None


# ============================================================ 子进程归属
#
_JOB_LOCK = threading.Lock()
_JOB_STATE = {"handle": None, "tried": False}

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


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
        if _JOB_STATE["handle"] is not None or _JOB_STATE["tried"]:
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


def _child_record_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "Lovomo" / "tts_child.json"


def _child_log_path() -> Path:
    return _child_record_path().with_name("tts_start.log")


def _open_child_log():
    global _tts_child_log
    if _tts_child_log not in (None, subprocess.DEVNULL):
        try:
            _tts_child_log.close()
        except Exception:
            pass
    try:
        path = _child_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _tts_child_log = open(path, "wb")
    except Exception:
        _tts_child_log = subprocess.DEVNULL
    return _tts_child_log


def _write_child_record(pid: int, port: int, exe: str) -> None:
    path = _child_record_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        tmp.write_text(json.dumps({
            "pid": int(pid),
            "started": process_start_time(int(pid)),
            "port": int(port),
            "exe": str(exe),
            "spawned_at": time.time(),
        }, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:
        print(f"[TTS] 记录子进程信息失败（下次启动可能认不到遗留进程）: "
              f"{type(e).__name__}: {e}")


def _read_child_record() -> dict:
    try:
        data = json.loads(_child_record_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _clear_child_record() -> None:
    try:
        _child_record_path().unlink(missing_ok=True)
    except Exception:
        pass


def _port_owner_pid(port: int):
    try:
        r = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                           capture_output=True, text=True, errors="replace",
                           timeout=8, **no_window_kwargs())
    except Exception:
        return None
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        if parts[1].endswith(f":{port}") and parts[-1].isdigit():
            pid = int(parts[-1])
            if pid:
                return pid
    return None


def _process_image(pid: int) -> str:
    if os.name != "nt":
        return ""
    try:
        k = _kernel32()
        k.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                 wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
        k.QueryFullProcessImageNameW.restype = wintypes.BOOL
        h = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            if not k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return ""
            return buf.value
        finally:
            k.CloseHandle(h)
    except Exception:
        return ""


class _AdoptedProcess:

    def __init__(self, pid: int):
        self.pid = int(pid)

    def wait(self, timeout=None):
        return None


# ------------------------------------------------------------ 生命周期开关
_LIFETIME_LOCK = threading.Lock()
_LIFETIME = {"ending": False}


def mark_exiting() -> None:
    with _LIFETIME_LOCK:
        _LIFETIME["ending"] = True


def is_exiting() -> bool:
    with _LIFETIME_LOCK:
        return _LIFETIME["ending"]


class ProcessManager:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance.processes = []
        return cls._instance

    def register(self, proc, name=""):
        if proc is not None:
            self.processes.append({"proc": proc, "name": name})
            print(f"已注册子进程: {name or 'unnamed'} (PID: {proc.pid})")

    def shutdown_all(self, budget: float = 6.0):
        deadline = time.time() + max(0.5, budget)
        for entry in self.processes:
            proc = entry["proc"]
            name = entry["name"]
            remaining = deadline - time.time()
            if remaining <= 0:
                print(f"关闭子进程预算用尽，跳过等待（进程会随主进程退出）：{name}")
                break
            try:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)],
                                   capture_output=True, timeout=min(3, remaining),
                                   **no_window_kwargs())
                else:
                    proc.terminate()
            except Exception:
                try:
                    proc.kill()
                except Exception as e:
                    print(f"关闭子进程 {name} 失败: {e}")
                    continue
            try:
                proc.wait(timeout=max(0.2, min(1.5, deadline - time.time())))
            except Exception:
                pass
        self.processes.clear()
        _clear_child_record()


process_manager = ProcessManager()

_tts_started_lock = threading.Lock()
_tts_started_flag = False


async def check_tts_service(config) -> bool:
    if is_cloud_tts(config):
        # 未就绪，进而挡掉自动启动与语音发送
        return True
    base_url = config.get("client_base_url", "http://127.0.0.1:9880")
    try:
        async with httpx.AsyncClient(timeout=2, trust_env=False,
                                     verify=verified_context()) as client:
            resp = await client.get(f"{base_url}/docs")
            return 200 <= resp.status_code < 400
    except Exception:
        return False


_ensure_fail_until = 0.0

_WEIGHT_LOCK = threading.Lock()
_APPLIED_WEIGHTS: dict = {}


def _weight_target(model_dir: str):
    folder = Path(model_dir)
    if not folder.is_dir():
        return None
    gpt_file = next((f for f in folder.iterdir() if f.suffix == ".ckpt"), None)
    sovits_file = next((f for f in folder.iterdir() if f.suffix == ".pth"), None)
    if gpt_file is None or sovits_file is None:
        return None
    return (str(gpt_file.resolve()), str(sovits_file.resolve()), gpt_file.stem)


def apply_local_weights(config) -> bool:
    """服务已在线时把权重切到这条配置的模型目录，让每个接入方式用自己的角色模型。"""
    if is_cloud_tts(config):
        return True
    model_dir = str(config.get("model_dir", "") or "").strip()
    base_url = str(config.get("client_base_url", "") or "").strip().rstrip("/")
    if not model_dir or not base_url:
        return False
    target = _weight_target(model_dir)
    if target is None:
        print(f"模型目录 {model_dir} 里没有 .ckpt / .pth，跳过权重切换。")
        return False
    gpt_path, sovits_path, name = target
    with _WEIGHT_LOCK:
        if _APPLIED_WEIGHTS.get(base_url) == (gpt_path, sovits_path):
            return True
    ok = True
    for endpoint, path, label in (("set_gpt_weights", gpt_path, "GPT"),
                                  ("set_sovits_weights", sovits_path, "SoVITS")):
        try:
            resp = httpx.get(f"{base_url}/{endpoint}",
                             params={"weights_path": path.replace("\\", "/")},
                             timeout=120, trust_env=False,
                             verify=verified_context())
        except Exception as e:
            print(f"{label} 权重切换失败：{type(e).__name__}: {e}")
            ok = False
            continue
        if resp.status_code != 200:
            print(f"{label} 权重切换失败: {resp.text[:200]}")
            ok = False
    if ok:
        with _WEIGHT_LOCK:
            _APPLIED_WEIGHTS[base_url] = (gpt_path, sovits_path)
        print(f"[ {name} ] 已按接入方式切换 TTS 权重")
    return ok


_ADOPT_LOCK = threading.Lock()
_ADOPT_DONE = {"value": False}

# （python.exe / pythonw.exe / Windows Store 版 re-exec 出来的 python3.13.exe）
_PYTHON_IMAGE_RE = re.compile(r"pythonw?[0-9.]*\.exe$", re.I)


def _managed_pid(pid: int) -> bool:
    return any(getattr(entry.get("proc"), "pid", None) == int(pid)
               for entry in process_manager.processes)


def adopt_existing_tts(config) -> bool:
    if not config.get("auto_start_tts", False):
        return False
    with _ADOPT_LOCK:
        if _ADOPT_DONE["value"]:
            return False
        _ADOPT_DONE["value"] = True

    base_url = config.get("client_base_url", "http://127.0.0.1:9880")
    port = urlparse(base_url).port or 9880

    record = _read_child_record()
    pid = record.get("pid")
    live_start = None
    if pid:
        live_start = process_start_time(int(pid))
        if live_start is not None and record.get("started") == live_start:
            pass                      # 记录与实物完全吻合，就是它
        else:
            _clear_child_record()
            pid = None
    if not pid:
        candidate = _port_owner_pid(port)
        if not candidate or candidate == os.getpid():
            return False
        image = _process_image(candidate)
        if not _PYTHON_IMAGE_RE.match(Path(image).name):
            return False
        pid = int(candidate)
        live_start = process_start_time(pid)
        if live_start is None:
            return False

    if int(pid) == os.getpid():
        return False

    if _managed_pid(int(pid)):
        return False

    bound = bind_to_job(int(pid))
    process_manager.register(_AdoptedProcess(int(pid)), name="GPT-SoVITS TTS(认领)")
    _write_child_record(int(pid), port, _process_image(int(pid)))
    print(f"检测到上一次运行遗留的 TTS 服务（PID {pid} · 端口 {port}），已收编为"
          f"本程序的子进程{'并挂进作业对象' if bound else ''}，退出时会一起结束。")
    return True


async def ensure_tts_service(config) -> bool:
    global _ensure_fail_until
    import asyncio
    if await check_tts_service(config):
        _ensure_fail_until = 0.0
        adopt_existing_tts(config)
        await asyncio.to_thread(apply_local_weights, config)
        return True
    if time.time() < _ensure_fail_until:
        return False  # 冷却期内直接失败，不阻塞消息管线
    if not config.get("auto_start_tts", False):
        return False
    _ensure_fail_until = time.time() + 60
    threading.Thread(target=auto_start_and_switch_tts, args=(config,), daemon=True).start()
    for _ in range(12):
        await asyncio.sleep(5)
        if await check_tts_service(config):
            _ensure_fail_until = 0.0
            await asyncio.to_thread(apply_local_weights, config)
            return True
    return False


def _loaded_weight_paths(config_path: str, root_dir: str) -> dict:
    try:
        data = yaml_lite.load_file(config_path)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    section = data.get("custom")
    if not isinstance(section, dict):
        section = data
    out = {}
    for key, name in (("t2s_weights_path", "gpt"), ("vits_weights_path", "sovits")):
        raw = str(section.get(key) or "").strip()
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = Path(root_dir) / path
        out[name] = str(path.resolve())
    return out


def _same_weight_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def auto_start_and_switch_tts(config):
    global _tts_started_flag
    with _tts_started_lock:
        if _tts_started_flag:
            return
        _tts_started_flag = True
    try:
        if not config.get("auto_start_tts", False):
            return
        if is_exiting():
            print("程序正在退出，跳过 TTS 自动启动。")
            return
        base_url = config.get("client_base_url", "http://127.0.0.1:9880")
        try:
            resp = httpx.get(f"{base_url}/docs", timeout=2)
            if 200 <= resp.status_code < 400:
                print("TTS 服务已在线，跳过自动启动。")
                adopt_existing_tts(config)
                return
        except Exception:
            pass

        script_path = config.get("tts_start_script", "")
        model_dir = config.get("model_dir", "")
        if not script_path or not model_dir:
            print("未配置 tts_start_script 或 model_dir，无法自动启动 TTS。")
            return

        script_path = Path(script_path)
        if script_path.is_dir():
            script_path = script_path / "api_v2.py"
        elif not script_path.exists() and script_path.suffix == "":
            candidate = script_path.parent / "api_v2.py"
            if candidate.exists():
                script_path = candidate
        if not script_path.exists():
            print(f"启动脚本不存在：{script_path}")
            return

        root_dir = str(script_path.parent).replace("\\", "/")
        python_candidates = [
            Path(root_dir) / "runtime" / "python.exe",
            Path(root_dir) / "runtime" / "python",
            Path(root_dir) / "venv" / "Scripts" / "python.exe",
            Path(root_dir) / "venv" / "bin" / "python",
            Path("python.exe"),
            Path("python"),
        ]
        python_exe = None
        for candidate in python_candidates:
            if candidate.exists():
                python_exe = str(candidate)
                break
        if not python_exe:
            try:
                import shutil
                python_exe = shutil.which("python") or shutil.which("python3")
            except Exception:
                python_exe = None
        if not python_exe:
            print("找不到 Python 可执行文件，无法启动 TTS。")
            return

        parsed = urlparse(base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 9880

        config_candidates = [
            Path(root_dir) / "GPT_SoVITS" / "configs" / "tts_infer.yaml",
            Path(root_dir) / "configs" / "tts_infer.yaml",
            Path(root_dir) / "tts_infer.yaml",
        ]
        config_path = None
        for candidate in config_candidates:
            if candidate.exists():
                config_path = str(candidate)
                break
        if not config_path:
            print("未找到 tts_infer.yaml 配置文件，无法启动。")
            return

        cmd = [
            python_exe,
            str(script_path),
            "-a", host,
            "-p", str(port),
            "-c", config_path,
        ]
        try:
            tts_proc = subprocess.Popen(
                cmd,
                cwd=root_dir,
                stdin=subprocess.DEVNULL,
                stdout=_open_child_log(),
                stderr=subprocess.STDOUT,
                **no_window_kwargs(),
            )
        except Exception as e:
            print(f"启动 TTS 失败: {e}")
            return
        bound = bind_to_job(tts_proc.pid)
        _write_child_record(tts_proc.pid, port, python_exe)
        process_manager.register(tts_proc, name="GPT-SoVITS TTS")
        print("已启动 TTS 服务，请等待就绪..."
              + ("" if bound else "（未能挂进作业对象，退出时将按进程树回收）"))
        if is_exiting():
            print("程序正在退出，回收刚启动的 TTS 进程。")
            process_manager.shutdown_all(budget=2.0)
            return

        deadline = time.time() + TTS_READY_TIMEOUT_SECONDS
        while time.time() < deadline:
            time.sleep(TTS_READY_POLL_SECONDS)
            try:
                resp = httpx.get(f"{base_url}/docs", timeout=2)
                if 200 <= resp.status_code < 400:
                    print("TTS 服务已就绪，加载模型中...")
                    break
            except Exception:
                continue
        else:
            print(f"TTS 服务在 {TTS_READY_TIMEOUT_SECONDS} 秒内未就绪，"
                  f"启动输出见 {_child_log_path()}")
            return

        model_dir_path = Path(model_dir)
        if not model_dir_path.exists():
            print(f"模型目录不存在：{model_dir}")
            return
        gpt_file = sovits_file = None
        for f in model_dir_path.iterdir():
            if f.suffix == ".ckpt" and gpt_file is None:
                gpt_file = f.name
            if f.suffix == ".pth" and sovits_file is None:
                sovits_file = f.name
        if not gpt_file or not sovits_file:
            print(f"模型目录 {model_dir} 中未找到 .ckpt 或 .pth 文件！")
            return

        model_gpt = f"{model_dir}/{gpt_file}".replace("\\", "/")
        model_sovits = f"{model_dir}/{sovits_file}".replace("\\", "/")
        model_name = Path(gpt_file).stem
        loaded = _loaded_weight_paths(config_path, root_dir)
        try:
            if loaded.get("gpt") and _same_weight_path(loaded["gpt"], model_gpt):
                print(f"[ {model_name} ] GPT 权重就是启动时加载的那份，跳过切换")
            else:
                resp = httpx.get(f"{base_url}/set_gpt_weights", params={"weights_path": model_gpt}, timeout=120)
                if resp.status_code == 200:
                    print(f"[ {model_name} ] GPT 权重切换成功！")
                else:
                    print(f"GPT 权重切换失败: {resp.text}")
            if loaded.get("sovits") and _same_weight_path(loaded["sovits"], model_sovits):
                print(f"[ {model_name} ] SoVITS 权重就是启动时加载的那份，跳过切换")
            else:
                resp = httpx.get(f"{base_url}/set_sovits_weights", params={"weights_path": model_sovits}, timeout=120)
                if resp.status_code == 200:
                    print(f"[ {model_name} ] SoVITS 权重切换成功！")
                else:
                    print(f"SoVITS 权重切换失败: {resp.text}")
            print(f"[ {model_name} ] 模型加载完毕，可以开始使用了！")
        except Exception as e:
            print(f"调用 API 切换模型权重失败: {e}")
    finally:
        with _tts_started_lock:
            _tts_started_flag = False
