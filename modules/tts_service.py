import asyncio
import ctypes
import ipaddress
import json
import os
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
from .process_lifecycle import (
    _job_handle, _kernel32, bind_to_job, process_start_time,
    _PROCESS_QUERY_LIMITED_INFORMATION,
)
from . import process_lifecycle


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
        r = process_lifecycle.run(["netstat", "-ano", "-p", "TCP"],
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
        self._identity = process_lifecycle._identity(self.pid)

    def wait(self, timeout=None):
        return self._identity.wait(timeout) if self._identity is not None else None

    def poll(self):
        return None if self._identity is not None and process_lifecycle._live(self._identity) else 0


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
                    cls._instance.processes_lock = threading.RLock()
        return cls._instance

    def register(self, proc, name=""):
        if proc is None:
            return
        with self.processes_lock:
            ending = is_exiting() or process_lifecycle.is_exiting()
            if not ending:
                self.processes.append({"proc": proc, "name": name})
        if ending:
            process_lifecycle.stop_tree(proc, timeout=0)
            return
        print(f"已注册子进程: {name or 'unnamed'} (PID: {proc.pid})")

    def shutdown_all(self, budget: float = 6.0):
        invalidate_local_weights()
        deadline = time.monotonic() + max(0.0, budget)
        with self.processes_lock:
            pending = list(self.processes)
            self.processes.clear()
        failed = []
        # Request termination for every entry even when the waiting budget is
        # zero. Do not hold the registry lock while processes are stopping.
        for entry in pending:
            proc = entry["proc"]
            try:
                stopped = process_lifecycle.stop_tree(proc, timeout=0)
                if not stopped and time.monotonic() < deadline:
                    stopped = process_lifecycle.stop_tree(
                        proc, timeout=max(0.0, deadline - time.monotonic()))
                if not stopped:
                    failed.append(entry)
            except Exception as e:
                failed.append(entry)
                print(f"关闭子进程 {entry['name']} 失败: {e}")
        with self.processes_lock:
            self.processes.extend(failed)
            if not self.processes:
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
_ENDPOINT_LOCKS = {}


def _endpoint_lock(base_url):
    with _WEIGHT_LOCK:
        return _ENDPOINT_LOCKS.setdefault(str(base_url).rstrip("/"), threading.Lock())


def invalidate_local_weights(base_url=None):
    with _WEIGHT_LOCK:
        if base_url is None:
            _APPLIED_WEIGHTS.clear()
        else:
            _APPLIED_WEIGHTS.pop(str(base_url).rstrip("/"), None)


def _weight_target(model_dir: str, config=None):
    folder = Path(model_dir)
    if not folder.is_dir():
        return None
    selected = []
    for suffix, key in ((".ckpt", "tts_gpt_weights"), (".pth", "tts_sovits_weights")):
        explicit = str(config.get(key, "") or "").strip() if config else ""
        if explicit:
            path = Path(explicit)
            path = path if path.is_absolute() else folder / path
            if not path.is_file() or path.suffix.lower() != suffix:
                raise ValueError(f"{key} 指定的模型文件不存在或格式不正确")
        else:
            candidates = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == suffix)
            if not candidates:
                return None
            if len(candidates) != 1:
                raise ValueError(f"模型目录有多个 {suffix}，请设置 {key} 明确选择")
            path = candidates[0]
        selected.append(path.resolve())
    return str(selected[0]), str(selected[1]), selected[0].stem


def _apply_local_weights_unlocked(config) -> bool:
    if is_cloud_tts(config):
        return True
    model_dir = str(config.get("model_dir", "") or "").strip()
    base_url = str(config.get("client_base_url", "") or "").strip().rstrip("/")
    if not model_dir:
        # A manually managed service can keep its own weights.
        return not config.get("auto_start_tts", False)
    if not base_url:
        return False
    try:
        target = _weight_target(model_dir, config)
        if target is None:
            raise ValueError("模型目录中缺少 .ckpt / .pth")
        gpt_path, sovits_path, name = target
        signature = tuple((p, Path(p).stat().st_mtime_ns, Path(p).stat().st_size)
                          for p in (gpt_path, sovits_path))
        with _WEIGHT_LOCK:
            if _APPLIED_WEIGHTS.get(base_url) == signature:
                return True
            # A partial switch invalidates the last successful pair too.
            _APPLIED_WEIGHTS.pop(base_url, None)
        from .runtime_policy import limit
        timeout = limit(config, "tts_weight_timeout_seconds")
        for endpoint, path in (("set_gpt_weights", gpt_path), ("set_sovits_weights", sovits_path)):
            resp = httpx.get(f"{base_url}/{endpoint}", params={"weights_path": path.replace("\\", "/")},
                             timeout=timeout, trust_env=False, verify=verified_context())
            if resp.status_code != 200:
                raise RuntimeError(f"{endpoint} 返回 HTTP {resp.status_code}")
        with _WEIGHT_LOCK:
            _APPLIED_WEIGHTS[base_url] = signature
        print(f"[ {name} ] 已按接入方式切换 TTS 权重")
        return True
    except Exception as e:
        invalidate_local_weights(base_url)
        print(f"TTS 权重切换失败：{type(e).__name__}: {e}")
        return False


def apply_local_weights(config) -> bool:
    with _endpoint_lock(config.get("client_base_url", "")):
        return _apply_local_weights_unlocked(config)


async def local_tts_request(config, params, timeout):
    """Keep the backend's two weight switches and synthesis in one transaction."""
    base_url = str(config.get("client_base_url", "") or "").rstrip("/")
    lock = _endpoint_lock(base_url)
    while not lock.acquire(blocking=False):
        await asyncio.sleep(0.05)
    try:
        switch = asyncio.create_task(asyncio.to_thread(_apply_local_weights_unlocked, config))
        try:
            ready = await asyncio.shield(switch)
        except asyncio.CancelledError:
            # Do not release the backend while its switch thread is still running.
            while not switch.done():
                try:
                    await asyncio.shield(switch)
                except asyncio.CancelledError:
                    continue
            raise
        if not ready:
            raise RuntimeError("TTS 角色权重未能加载，已停止本句合成")
        async with httpx.AsyncClient(timeout=timeout, trust_env=False, verify=verified_context()) as client:
            response = await client.get(f"{base_url}/tts", params=params)
        if response.status_code >= 500:
            invalidate_local_weights(base_url)
        return response
    except BaseException:
        invalidate_local_weights(base_url)
        raise
    finally:
        lock.release()


_ADOPT_LOCK = threading.Lock()
_ADOPT_DONE = {"value": False}


def _managed_pid(pid: int) -> bool:
    return any(getattr(entry.get("proc"), "pid", None) == int(pid)
               for entry in process_manager.processes)


def adopt_existing_tts(config) -> bool:
    if not config.get("auto_start_tts", False) or is_exiting():
        return False
    try:
        parsed = urlparse(config.get("client_base_url", "http://127.0.0.1:9880"))
        host = (parsed.hostname or "").lower()
        if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
            return False
        port = parsed.port or 9880
    except (TypeError, ValueError):
        return False
    with _ADOPT_LOCK:
        if _ADOPT_DONE["value"]:
            return False
        _ADOPT_DONE["value"] = True

    record = _read_child_record()
    try:
        pid = int(record.get("pid") or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    live_start = process_start_time(pid)
    if live_start is None or record.get("started") != live_start:
        _clear_child_record()
        return False
    if record.get("port") != port:
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
        return await asyncio.to_thread(apply_local_weights, config)
    invalidate_local_weights(config.get("client_base_url", ""))
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
            return await asyncio.to_thread(apply_local_weights, config)
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
            resp = httpx.get(f"{base_url}/docs", timeout=2, trust_env=False, verify=verified_context())
            if 200 <= resp.status_code < 400:
                print("TTS 服务已在线，跳过自动启动。")
                adopt_existing_tts(config)
                apply_local_weights(config)
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
            tts_proc = process_lifecycle.popen(
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

        from .runtime_policy import limit
        ready_timeout = limit(config, "tts_ready_timeout_seconds")
        poll = limit(config, "tts_ready_poll_seconds")
        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            if is_exiting() or process_lifecycle.is_exiting():
                return
            for _ in range(max(1, int(poll / 0.1))):
                if is_exiting() or process_lifecycle.is_exiting():
                    return
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            try:
                resp = httpx.get(f"{base_url}/docs", timeout=2, trust_env=False, verify=verified_context())
                if 200 <= resp.status_code < 400:
                    print("TTS 服务已就绪，加载模型中...")
                    break
            except Exception:
                continue
        else:
            print(f"TTS 服务在 {ready_timeout:g} 秒内未就绪，"
                  f"启动输出见 {_child_log_path()}")
            return

        invalidate_local_weights(base_url)
        if apply_local_weights(config):
            print("TTS 模型已加载，可以开始使用。")
    finally:
        with _tts_started_lock:
            _tts_started_flag = False
