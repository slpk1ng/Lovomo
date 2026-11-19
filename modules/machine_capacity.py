import ctypes
import time
import threading
from . import process_lifecycle as child_processes

import subprocess
from typing import Optional

from .tts_service import no_window_kwargs

TTS_FIRST_FREE_PHYSICAL_MB = 4096
TTS_FIRST_FREE_VRAM_MB = 3072
_VRAM_PROBE_TIMEOUT = 8
_PROBE_LOCK = threading.Lock()
_PROBE_CACHE = None


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def free_physical_mb() -> Optional[int]:
    try:
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return None
        return int(status.ullAvailPhys // (1024 * 1024))
    except Exception:
        return None


def free_vram_mb(device="auto") -> Optional[int]:
    try:
        out = child_processes.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=_VRAM_PROBE_TIMEOUT,
            **no_window_kwargs())
        values = [int(float(line.strip())) for line in (out.stdout or "").strip().splitlines()]
        if not values:
            return None
        if str(device).lower() == "auto":
            return min(values)  # 未指定设备时保守判断，不能假定第一张卡承载模型。
        index = int(device)
        return values[index] if 0 <= index < len(values) else None
    except Exception:
        return None


def capacity(config=None):
    global _PROBE_CACHE
    config = config or {}
    device = str(config.get("startup_gpu_device", "auto") or "auto")
    with _PROBE_LOCK:
        now = time.monotonic()
        if _PROBE_CACHE and _PROBE_CACHE[0] == device and now - _PROBE_CACHE[1] < 5:
            return _PROBE_CACHE[2]
        value = (free_physical_mb(), free_vram_mb(device))
        _PROBE_CACHE = (device, time.monotonic(), value)
        return value


def summary(config=None) -> str:
    ram, vram = capacity(config)
    return "，".join([
        f"可用内存 {ram} MB" if ram is not None else "可用内存未知",
        f"空闲显存 {vram} MB" if vram is not None else "空闲显存未知",
    ])


def tts_first_recommended(config=None) -> bool:
    from .runtime_policy import limit
    config = config or {}
    ram, vram = capacity(config)
    if ram is not None and ram < limit(config, "startup_free_ram_mb"):
        return True
    if vram is not None and vram < limit(config, "startup_free_vram_mb"):
        return True
    return False
