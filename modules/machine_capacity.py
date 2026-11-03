import ctypes
import subprocess
from typing import Optional

from .tts_service import no_window_kwargs

TTS_FIRST_FREE_PHYSICAL_MB = 4096
TTS_FIRST_FREE_VRAM_MB = 3072
_VRAM_PROBE_TIMEOUT = 8


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


def free_vram_mb() -> Optional[int]:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=_VRAM_PROBE_TIMEOUT,
            **no_window_kwargs())
        line = (out.stdout or "").strip().splitlines()[0]
        return int(float(line.strip()))
    except Exception:
        return None


def summary() -> str:
    ram = free_physical_mb()
    vram = free_vram_mb()
    return "，".join([
        f"可用内存 {ram} MB" if ram is not None else "可用内存未知",
        f"空闲显存 {vram} MB" if vram is not None else "空闲显存未知",
    ])


def tts_first_recommended() -> bool:
    ram = free_physical_mb()
    if ram is not None and ram < TTS_FIRST_FREE_PHYSICAL_MB:
        return True
    vram = free_vram_mb()
    if vram is not None and vram < TTS_FIRST_FREE_VRAM_MB:
        return True
    return False
