"""GPT-SoVITS 服务生命周期管理（自 main.py 迁出）。"""
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

from .tls import verified_context


def no_window_kwargs() -> dict:
    """让子进程完全不弹控制台窗口。

    Windows 上从 GUI 进程（pythonw / 打包 exe）spawn 控制台程序（cmd、taskkill、
    net 等）会短暂弹出一个黑框。仅设 stdin/stdout/stderr=DEVNULL 并不能阻止它——
    那只是重定向了流，控制台窗口本身仍会被创建。必须传 CREATE_NO_WINDOW。
    """
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}


# ============================================================ 子进程归属
# Popen 出来的进程在 Windows 上**只是"父子"关系，不是"生死绑定"**：父进程
# 退出（尤其是崩溃、被任务管理器结束、或退出路径没跑到 shutdown_all）之后，
# 子进程照旧活着。TTS 一旦这样脱管，就成了任务管理器里一个父进程不存在的
# "独立进程"；下次启动又因为 9880 已经在线而被跳过，于是永远脱管，用户在
# 任务管理器里怎么关都关不干净。
#
# 唯一可靠的解法是**作业对象**（Job Object）：把子进程 assign 进一个带
# JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE 的作业，句柄随本进程关闭（正常退出、
# 崩溃、被强杀都算）→ 系统替我们收尾，子进程不可能比父进程活得久。
# 句柄必须长期持有：句柄一被关掉就等于立刻杀子进程。
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
    """取 kernel32 并声明好本模块用到的每个入口的类型。

    所有 ctypes 入口都必须显式声明 argtypes/restype：默认按 c_int 传递会在
    64 位下把句柄/指针的高 32 位截断，而且错误会被静默吞掉。
    """
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
    """本进程的作业对象句柄（懒创建，建好就一直持有到进程结束）。"""
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
    """把进程挂进"父进程一退出就一起结束"的作业对象。"""
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
    """进程创建时间（FILETIME 原始值）；进程已退出/拿不到时返回 None。

    用来给"上次遗留的 TTS"做身份校验：PID 会被系统复用，只比 PID 有可能
    认错人，把用户的别的进程当成自己的 TTS 杀掉。
    """
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
    """上一次启动的 TTS 子进程记录。

    固定放用户数据目录而不是程序目录：程序目录可能只读（装到 Program Files），
    而且重装/换目录后这份记录还要能被读到 —— 认领上一轮遗留的 TTS 全靠它。
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "Lovomo" / "tts_child.json"


def _write_child_record(pid: int, port: int, exe: str) -> None:
    path = _child_record_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 临时名必须唯一：两个实例共用同一个 .tmp 时，os.replace 可能把
        # 对方正在写的半截记录提交上去，下次启动就会按错的 PID 去认领进程
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
    """谁在监听这个端口。

    用 netstat 而不是多绕一层 IP Helper API：这里的列标题/状态词是随系统
    语言变的，所以只认「本地地址列以 :port 结尾、最后一列是数字」这两条，
    不匹配任何英文单词（否则中文/其他语言的 Windows 上会全盘失效）。
    """
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
    """进程可执行文件的完整路径（拿不到返回空串）。"""
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
    """认领回来的 TTS：只有 PID、没有 Popen 句柄。

    只需要满足 ProcessManager 用到的两个接口（pid / wait），关闭时照样
    按 PID 收整棵进程树。
    """

    def __init__(self, pid: int):
        self.pid = int(pid)

    def wait(self, timeout=None):
        return None


# ------------------------------------------------------------ 生命周期开关
# 退出流程一旦开始，就不允许再 spawn 出新进程：那种"退出跑到一半、TTS 刚被
# Popen 出来"的竞态留下的正是脱管进程。这里只做标记，不做清理。
_LIFETIME_LOCK = threading.Lock()
_LIFETIME = {"ending": False}


def mark_exiting() -> None:
    """任何退出路径都要先调它（在 shutdown_all 之前）。"""
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
        """关闭全部子进程。

        退出耗时的关键瓶颈就在这：TTS 是 Python 子进程，还会再拉起一堆
        子/孙进程，`taskkill /T` 逐个收树 + 每一步 `proc.wait(timeout=5)`
        串起来动辄十几秒。这里改成「每条最多 3 秒、整体不超过 budget」，
        到点就返回，剩下的留给随后必然发生的进程退出兜底。
        """
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
            # 已经在 taskkill 里拿到结果，这里只做一次很短的确认等待
            try:
                proc.wait(timeout=max(0.2, min(1.5, deadline - time.time())))
            except Exception:
                pass
        self.processes.clear()
        # 子进程都没了，遗留记录也就没意义了（留着只会让下次启动白认领一次）
        _clear_child_record()


process_manager = ProcessManager()

_tts_started_lock = threading.Lock()
_tts_started_flag = False


async def check_tts_service(config) -> bool:
    base_url = config.get("client_base_url", "http://127.0.0.1:9880")
    try:
        async with httpx.AsyncClient(timeout=2, trust_env=False,
                                     verify=verified_context()) as client:
            resp = await client.get(f"{base_url}/docs")
            # 只要 2xx/3xx：端口被别的服务占用时会回 401/404，
            # 旧判定（<500）会误认成"TTS 已就绪"而跳过自动启动
            return 200 <= resp.status_code < 400
    except Exception:
        return False


_ensure_fail_until = 0.0  # 启动失败后的冷却截止时间，避免每条消息都阻塞重试 60 秒

# 认领只做一次：进程内第一次发现"端口上已经有 TTS"时判定归属
_ADOPT_LOCK = threading.Lock()
_ADOPT_DONE = {"value": False}

# 端口属主必须是 Python 解释器；解释器文件名随发行方式变化
# （python.exe / pythonw.exe / Windows Store 版 re-exec 出来的 python3.13.exe）
_PYTHON_IMAGE_RE = re.compile(r"pythonw?[0-9.]*\.exe$", re.I)


def _managed_pid(pid: int) -> bool:
    """该 PID 是否已经是本进程登记在册的子进程。"""
    return any(getattr(entry.get("proc"), "pid", None) == int(pid)
               for entry in process_manager.processes)


def adopt_existing_tts(config) -> bool:
    """把"上一轮 Lovomo 遗留的 TTS"重新收编成本进程的子进程。

    TTS 已经在 9880 上跑着时，启动流程会直接跳过 —— 但那个服务可能是上一次
    运行留下的（父进程早没了）。不认领的话它就永远是任务管理器里一个独立
    进程，本程序退出也带不走它。
    认领方式：挂进作业对象 + 登记到 process_manager，之后它就重新受本进程
    管辖了（退出时一起结束）。

    只认两种证据，其余一律不动：
      1. 上次启动留下的记录（PID + 创建时间完全吻合）—— 最可靠；
      2. 没有记录（老版本留下的遗留进程）：端口属主必须是 Python 解释器。
    判据必须严：PID 会被系统复用，只比 PID 有可能认错人、把用户的别的进程
    当成自己的 TTS 杀掉。
    另外只有用户开了「自动启动 TTS 服务」才接管 —— 那表示他把 TTS 的生死
    交给本程序管；没开的话端口上那份可能是他自己手工起的，不该被我们关掉。
    """
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
            # 进程没了，或同一个 PID 换了别的进程（系统复用）→ 记录作废
            _clear_child_record()
            pid = None
    if not pid:
        candidate = _port_owner_pid(port)
        if not candidate or candidate == os.getpid():
            return False
        image = _process_image(candidate)
        if not _PYTHON_IMAGE_RE.match(Path(image).name):
            # 端口被别的程序占着，那不是我们的 TTS
            return False
        pid = int(candidate)
        live_start = process_start_time(pid)
        if live_start is None:
            return False

    if int(pid) == os.getpid():
        return False

    if _managed_pid(int(pid)):
        return False                      # 本进程自己启动的 TTS，不是"上一次运行遗留的"

    bound = bind_to_job(int(pid))
    process_manager.register(_AdoptedProcess(int(pid)), name="GPT-SoVITS TTS(认领)")
    # 记下来：下次再启动，光看记录就能确认身份，不用再去猜端口属主
    _write_child_record(int(pid), port, _process_image(int(pid)))
    print(f"检测到上一次运行遗留的 TTS 服务（PID {pid} · 端口 {port}），已收编为"
          f"本程序的子进程{'并挂进作业对象' if bound else ''}，退出时会一起结束。")
    return True


async def ensure_tts_service(config) -> bool:
    global _ensure_fail_until
    if await check_tts_service(config):
        _ensure_fail_until = 0.0
        adopt_existing_tts(config)
        return True
    if time.time() < _ensure_fail_until:
        return False  # 冷却期内直接失败，不阻塞消息管线
    if not config.get("auto_start_tts", False):
        return False
    # 先写冷却标记再启动：启动快速失败（脚本缺失等）或并发消息到达时，
    # 其他消息不必各等 60 秒；启动成功后下一次 check 会把冷却清零
    _ensure_fail_until = time.time() + 60
    threading.Thread(target=auto_start_and_switch_tts, args=(config,), daemon=True).start()
    import asyncio
    for _ in range(12):
        await asyncio.sleep(5)
        if await check_tts_service(config):
            _ensure_fail_until = 0.0
            return True
    return False


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
            # 判据与 check_tts_service 保持一致：端口被别的服务占用时会回 401/404，
            # 用 <500 会把别人的服务当成 TTS 已在线，于是既不启动也看不出问题
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
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **no_window_kwargs(),
            )
        except Exception as e:
            print(f"启动 TTS 失败: {e}")
            return
        # 挂进作业对象：这一步之后，无论本进程怎么死（正常退出、崩溃、被任务
        # 管理器结束），系统都会连带把它收掉，不会再留下"独立进程"。
        bound = bind_to_job(tts_proc.pid)
        _write_child_record(tts_proc.pid, port, python_exe)
        process_manager.register(tts_proc, name="GPT-SoVITS TTS")
        print("已启动 TTS 服务，请等待就绪..."
              + ("" if bound else "（未能挂进作业对象，退出时将按进程树回收）"))
        if is_exiting():
            # 竞态兜底：spawn 与"开始退出"同时发生 —— 这时 shutdown_all 可能
            # 已经跑完并清过进程表了，这个进程会变成孤儿，就地收掉。
            print("程序正在退出，回收刚启动的 TTS 进程。")
            process_manager.shutdown_all(budget=2.0)
            return

        for _ in range(12):
            time.sleep(5)
            try:
                resp = httpx.get(f"{base_url}/docs", timeout=2)
                if 200 <= resp.status_code < 400:
                    print("TTS 服务已就绪，加载模型中...")
                    break
            except Exception:
                continue
        else:
            print("TTS 服务在 60 秒内未就绪，请检查日志。")
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
        try:
            resp = httpx.get(f"{base_url}/set_gpt_weights", params={"weights_path": model_gpt}, timeout=120)
            if resp.status_code == 200:
                print(f"[ {model_name} ] GPT 权重切换成功！")
            else:
                print(f"GPT 权重切换失败: {resp.text}")
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
