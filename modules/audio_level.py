"""音频电平工具：测量峰值/响度/静音，做响度归一与合成前的参考音频预处理。

参考音频的响度差异会直接传到合成结果上（太轻的参考音频合成出来也轻、
削波的参考音频合成出来发哑），所以参考音频要在送进 TTS 之前先统一。
"""
import hashlib
import math
import os
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Optional

import numpy as np

from .tts_service import no_window_kwargs

CLIP_LEVEL = 0.999
SILENCE_FLOOR_DB = -50.0
SPEECH_MIN_RATIO = 0.2
# TTS 服务端只接受 3~10 秒的参考音频，裁静音不能把长度裁到下限以下
REF_MIN_SECONDS = 3.0
REF_MAX_SECONDS = 10.0
DECODE_RATE = 32000
# 音高差到这个倍数（约 ±35%）就提醒"不像同一个人"
PITCH_WARN_RATIO = 1.35
DEFAULT_TARGET_DB = -20.0
DEFAULT_PEAK_DB = -1.0
DEFAULT_MAX_GAIN_DB = 12.0
CACHE_MAX_FILES = 200
FFMPEG_TIMEOUT_SECONDS = 120.0
_FFMPEG_MISSING = "未安装 ffmpeg，只能处理 16bit PCM WAV"

_failed_paths: set = set()


def _db(value) -> float:
    return 20 * math.log10(max(float(value), 1e-9))


def _ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def _warn_once(key: str, message: str) -> None:
    if key in _failed_paths:
        return
    _failed_paths.add(key)
    print(message)


def read_pcm16(path) -> Optional[tuple]:
    """读 16bit PCM WAV；其它格式返回 None。"""
    try:
        with wave.open(str(path), "rb") as wf:
            if wf.getsampwidth() != 2:
                return None
            channels = wf.getnchannels()
            rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
    except (wave.Error, OSError, EOFError):
        return None
    if not rate or not channels or not frames:
        return None
    audio = np.frombuffer(frames, dtype=np.int16).reshape(-1, channels)
    return audio, rate, channels


def write_pcm16(path, audio, rate: int) -> bool:
    audio = np.clip(np.asarray(audio), -32768, 32767).astype(np.int16)
    if audio.ndim == 1:
        audio = audio.reshape(-1, 1)
    try:
        with wave.open(str(path), "wb") as out:
            out.setnchannels(audio.shape[1])
            out.setsampwidth(2)
            out.setframerate(int(rate))
            out.writeframes(audio.astype("<i2").tobytes())
    except (wave.Error, OSError):
        return False
    return True


def _source_rate(path) -> int:
    """源文件采样率：按原采样率解码才不会被重采样出来的过冲误判成削波。"""
    exe = _ffmpeg()
    if not exe:
        return DECODE_RATE
    probe = exe.replace("ffmpeg.exe", "ffprobe.exe").replace("ffmpeg", "ffprobe")
    try:
        proc = subprocess.run([probe, "-v", "error", "-select_streams", "a:0",
                               "-show_entries", "stream=sample_rate",
                               "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=FFMPEG_TIMEOUT_SECONDS, text=True,
                              **no_window_kwargs())
        value = int((proc.stdout or "").strip())
        return value if value > 0 else DECODE_RATE
    except (OSError, ValueError, subprocess.SubprocessError):
        return DECODE_RATE


def _decode_any(path, rate: int = 0) -> Optional[np.ndarray]:
    """非 16bit WAV 时用 ffmpeg 解码成单声道 float32；rate=0 表示保持原采样率。"""
    exe = _ffmpeg()
    if not exe:
        return None
    args = [exe, "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "1"]
    if rate:
        args += ["-ar", str(rate)]
    try:
        proc = subprocess.run(args + ["-"], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL,
                              timeout=FFMPEG_TIMEOUT_SECONDS, **no_window_kwargs())
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout:
        return None
    return np.frombuffer(proc.stdout, dtype="<f4")


def _to_mono_float(audio: np.ndarray) -> np.ndarray:
    if audio.ndim > 1 and audio.shape[1] > 1:
        audio = audio.mean(axis=1)
    else:
        audio = audio.reshape(-1)
    return audio.astype(np.float32) / 32768.0


def _median_f0(mono: np.ndarray, rate: int) -> float:
    """估个基频中位数（Hz）：用自相关找每帧的周期，判断是不是同一个人的音高。

    只用来做"模仿音频和角色本体差太多"的提醒，不需要很精确：
    帧长 25ms、最多看 24 帧，相关度太低的帧（气声/噪声）直接丢掉。
    """
    win = int(rate * 0.025)
    if win <= 8 or mono.size < win * 4:
        return 0.0
    lo, hi = int(rate / 400), int(rate / 70)
    if hi <= lo:
        return 0.0
    frames = mono[: mono.size // win * win].reshape(-1, win)
    step = max(1, len(frames) // 24)
    pitches = []
    for frame in frames[::step]:
        if float(np.sqrt(np.mean(np.square(frame, dtype=np.float64)))) < 0.02:
            continue
        frame = frame - frame.mean()
        ac = np.correlate(frame, frame, mode="full")[len(frame) - 1:]
        if ac[0] <= 0:
            continue
        seg = ac[lo:hi]
        if seg.size == 0:
            continue
        lag = int(np.argmax(seg)) + lo
        if ac[lag] / ac[0] < 0.3:
            continue
        pitches.append(rate / lag)
    return float(np.median(pitches)) if len(pitches) >= 4 else 0.0


def measure(path, rate: int = 0) -> dict:
    """测一段音频：时长、峰值、RMS、削波与静音占比。"""
    pcm = read_pcm16(path)
    if pcm is not None:
        audio, sr, _ch = pcm
        rate = sr
        mono = _to_mono_float(np.asarray(audio, dtype=np.int16))
    else:
        rate = rate or _source_rate(path)
        mono = _decode_any(path, rate)
        if mono is None:
            return {"ok": False, "reason": _FFMPEG_MISSING if not _ffmpeg()
                    else "无法解析该音频文件"}
    if mono.size == 0:
        return {"ok": False, "reason": "音频内容为空"}
    peak = float(np.max(np.abs(mono)))
    rms = float(np.sqrt(np.mean(np.square(mono, dtype=np.float64))))
    win = max(1, int(rate * 0.05))
    frames = mono[: mono.size // win * win].reshape(-1, win)
    headroom = np.max(np.abs(frames), axis=1) if frames.size else np.array([peak])
    speech = float(np.mean(headroom > 10 ** (SILENCE_FLOOR_DB / 20)))
    return {"ok": True, "duration": float(mono.size) / float(rate or 1),
            "sample_rate": rate, "peak_db": _db(peak), "rms_db": _db(rms),
            "clip_ratio": float(np.mean(np.abs(mono) >= CLIP_LEVEL)),
            "speech_ratio": speech, "f0_hz": _median_f0(mono, rate or 32000)}


def pitch_note(stats: dict, ref_f0: float) -> str:
    """模仿音频和角色本体音高差得多时给一句提醒（音色会被平均、听起来不像同一个人）。"""
    if not stats or not stats.get("ok") or ref_f0 <= 0:
        return ""
    f0 = float(stats.get("f0_hz") or 0)
    if f0 <= 0:
        return ""
    ratio = f0 / ref_f0 if f0 > ref_f0 else ref_f0 / f0
    if ratio < PITCH_WARN_RATIO:
        return ""
    return (f"音高 {f0:.0f}Hz 与角色本体 {ref_f0:.0f}Hz 差得较多，"
            "模仿后音色可能不像同一个人")


def quality_notes(stats: dict) -> list:
    """把测量结果翻译成"这段参考音频有什么问题"（空列表表示没问题）。"""
    if not stats or not stats.get("ok"):
        return []
    notes = []
    duration = stats.get("duration", 0)
    if duration < REF_MIN_SECONDS or duration > REF_MAX_SECONDS:
        notes.append(f"时长 {duration:.1f}s 不在 {REF_MIN_SECONDS:.0f}~{REF_MAX_SECONDS:.0f} 秒，"
                     "合成会被服务端拒绝")
    if stats["peak_db"] > -0.1 or stats["clip_ratio"] > 0.001:
        notes.append(f"峰值 {stats['peak_db']:+.1f}dB 已削波，合成容易发哑")
    if stats["rms_db"] < -30:
        notes.append(f"整体偏轻（RMS {stats['rms_db']:.1f}dB），合成声音也会偏小")
    if stats.get("speech_ratio", 1.0) < SPEECH_MIN_RATIO:
        notes.append(f"{100 - stats['speech_ratio'] * 100:.0f}% 是静音，参考信息太少")
    return notes


def normalize_file(path, target_db: float = DEFAULT_TARGET_DB,
                   peak_db: float = DEFAULT_PEAK_DB,
                   max_gain_db: float = DEFAULT_MAX_GAIN_DB,
                   trim_silence: bool = False, warn: str = "") -> dict:
    """按目标响度做增益归一（原地写回 16bit PCM WAV）。"""
    pcm = read_pcm16(path)
    if pcm is None:
        if warn:
            _warn_once(warn, "响度统一已跳过：音频不是 16bit PCM WAV"
                             "（如 mp3/ogg 输出，或缺少 ffmpeg）。")
        return {"ok": False, "reason": _FFMPEG_MISSING}
    audio, rate, channels = pcm
    work = np.asarray(audio, dtype=np.int16)
    if trim_silence:
        work = _trim_edges(work, channels, rate)
    mono = _to_mono_float(work)
    if mono.size == 0:
        return {"ok": False, "reason": "音频内容为空"}
    peak = float(np.max(np.abs(mono)))
    rms = float(np.sqrt(np.mean(np.square(mono, dtype=np.float64))))
    if peak <= 0 or rms <= 0:
        return {"ok": False, "reason": "整段是静音"}
    gain = min(10 ** (target_db / 20) / rms, 10 ** (peak_db / 20) / peak)
    limit = 10 ** (abs(max_gain_db) / 20)
    gain = max(1.0 / limit, min(limit, gain))
    out = np.clip(work.astype(np.float32) * gain, -32768, 32767).astype(np.int16)
    if not write_pcm16(path, out, rate):
        return {"ok": False, "reason": "写回音频失败"}
    mono_out = _to_mono_float(out)
    return {"ok": True, "gain_db": _db(gain),
            "rms_before": rms, "rms_after": float(np.sqrt(np.mean(np.square(mono_out,
                                                                          dtype=np.float64))))}


def _trim_keep_length(audio: np.ndarray, rate: int) -> np.ndarray:
    """裁首尾静音；裁完短于服务端下限（3 秒）就保持原样，别把参考音频裁到被拒。"""
    trimmed = _trim_edges(audio, 1, rate)
    if len(trimmed) < int(REF_MIN_SECONDS * rate):
        return audio
    return trimmed


def _trim_edges(audio: np.ndarray, channels: int, rate: int, keep_ms: int = 60,
                level: int = 240) -> np.ndarray:
    """裁掉首尾的纯数字静音（保留一点呼吸感）。"""
    if audio is None or len(audio) == 0:
        return audio
    mono = np.abs(np.asarray(audio, dtype=np.int64))
    mono = mono.max(axis=1) if channels > 1 else mono.reshape(-1)
    loud = np.ravel(mono > level)
    if not loud.any():
        return audio
    keep = max(0, int(rate * keep_ms / 1000))
    start = max(0, int(np.argmax(loud)) - keep)
    last = len(loud) - 1 - int(np.argmax(loud[::-1]))
    end = min(len(audio), last + 1 + keep)
    return audio[start:end] if end > start else audio


def prepare_reference(path, cache_dir: Path, target_db: float = DEFAULT_TARGET_DB,
                      peak_db: float = DEFAULT_PEAK_DB,
                      max_gain_db: float = DEFAULT_MAX_GAIN_DB) -> str:
    """合成前的参考音频：单声道 + 裁首尾静音 + 电平归一，结果缓存复用。

    处理不了（非 16bit WAV 又没有 ffmpeg）时原样返回原路径，合成照常进行。
    """
    src = Path(str(path or ""))
    if not src.exists():
        return str(path or "")
    exe = _ffmpeg()
    try:
        stat = src.stat()
    except OSError:
        return str(path or "")
    key = hashlib.sha1("|".join([
        str(src.resolve()), str(int(stat.st_mtime)), str(stat.st_size),
        f"{target_db}", f"{peak_db}", f"{max_gain_db}", exe or "",
    ]).encode("utf-8")).hexdigest()[:16]
    cached = Path(cache_dir) / f"ref_{key}.wav"
    if cached.exists():
        return str(cached)
    cache_dir.mkdir(parents=True, exist_ok=True)

    pcm = read_pcm16(src)
    if pcm is not None:
        audio, rate, channels = pcm
        mono = np.asarray(audio, dtype=np.int16)
        if channels > 1:
            mono = mono.reshape(-1, channels).mean(axis=1).astype(np.int16)
        mono = _trim_keep_length(mono, rate)
    else:
        if not exe:
            _warn_once(str(src.resolve()),
                       f"参考音频 {src.name} 不是 16bit WAV，且{_FFMPEG_MISSING}："
                       "该段不做电平预处理。")
            return str(path or "")
        raw = _decode_any(src, DECODE_RATE)
        if raw is None or raw.size == 0:
            _warn_once(str(src.resolve()), f"参考音频 {src.name} 解码失败，按原文件使用。")
            return str(path or "")
        rate = DECODE_RATE
        clipped = np.clip(raw, -1.0, 1.0)
        mono = _trim_keep_length((clipped * 32767).astype(np.int16), rate)

    if mono.size == 0:
        return str(path or "")
    # 先写唯一临时文件、归一完成后再提交：直接写 cached 的话，文件一被创建
    # （内容还没写完）并发的第二个调用就会在 cached.exists() 处拿到半截音频
    tmp = cached.with_name(f"{cached.stem}.{os.getpid()}.{os.urandom(4).hex()}.tmp.wav")
    if not write_pcm16(tmp, mono, rate):
        tmp.unlink(missing_ok=True)
        return str(path or "")
    result = normalize_file(tmp, target_db=target_db, peak_db=peak_db,
                            max_gain_db=max_gain_db)
    if not result.get("ok"):
        tmp.unlink(missing_ok=True)
        return str(path or "")
    try:
        os.replace(str(tmp), str(cached))
    except OSError as e:
        print(f"参考音频缓存提交失败（按原文件使用）: {e}")
        tmp.unlink(missing_ok=True)
        return str(path or "")
    _prune_cache(cache_dir)
    return str(cached)


def _prune_cache(cache_dir: Path) -> None:
    try:
        files = sorted((p for p in Path(cache_dir).glob("ref_*.wav") if p.is_file()),
                       key=lambda p: p.stat().st_mtime)
    except OSError:
        return
    for old in files[:-CACHE_MAX_FILES]:
        try:
            old.unlink()
        except OSError:
            pass
