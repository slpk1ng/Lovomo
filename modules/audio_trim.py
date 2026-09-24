# -*- coding: utf-8 -*-
"""参考音频时长规整：短于下限前后补静音，长于上限压缩内部停顿。

优先用 ffmpeg（任意格式），找不到 ffmpeg 时退回内置 WAV 处理；
两条路都走不通时只回报原因，不改动原文件。
"""
import array
import math
import os
import shutil
import subprocess
import sys
import wave
from pathlib import Path
from typing import Optional

from .tts_service import no_window_kwargs

MIN_SECONDS = 3.0
MAX_SECONDS = 10.0
# 补静音时多补一点，避免编码器取整后落到下限以下
PAD_MARGIN_SECONDS = 0.05
# 压缩时多压一点，避免取整后仍卡在上限上
TRIM_MARGIN_SECONDS = 0.05
# 压缩停顿后保留的最短静音，避免把一句话切成碎片
KEEP_SILENCE_SECONDS = 0.06
# 只有长于该时长的停顿才值得压缩
MIN_SILENCE_TO_TRIM = 0.3
# 判定静音的峰值阈值（占满量程比例）
SILENCE_THRESHOLD = 0.02
# 依次加强的压缩参数（停顿判定时长, 保留静音时长），够用就停
TRIM_STEPS = ((MIN_SILENCE_TO_TRIM, KEEP_SILENCE_SECONDS),
              (0.15, 0.05), (0.08, 0.04))
FFMPEG_TIMEOUT_SECONDS = 300.0
FFPROBE_TIMEOUT_SECONDS = 60.0
WAV_WINDOW_SECONDS = 0.01
# 16 位有符号采样满量程
PCM16_FULL_SCALE = 32768


def _emit(log, message: str):
    if log:
        log(message)
    else:
        print(message)


def _ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def _ffprobe() -> Optional[str]:
    return shutil.which("ffprobe")


def probe_duration(path) -> Optional[float]:
    """音频时长（秒）：优先 ffprobe，其次内置 wave（仅 WAV）。"""
    path = Path(path)
    ffprobe = _ffprobe()
    if ffprobe:
        try:
            proc = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                timeout=FFPROBE_TIMEOUT_SECONDS, **no_window_kwargs())
            value = float((proc.stdout or "").strip())
            if value > 0:
                return value
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return _wav_duration(path)


def _wav_duration(path) -> Optional[float]:
    try:
        with wave.open(str(path), "rb") as wf:
            rate = wf.getframerate()
            return wf.getnframes() / float(rate) if rate else None
    except (wave.Error, OSError, EOFError):
        return None


def _run_ffmpeg(exe: str, args) -> None:
    proc = subprocess.run([exe, "-y", "-hide_banner", "-loglevel", "error"] + args,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          encoding="utf-8", errors="replace",
                          timeout=FFMPEG_TIMEOUT_SECONDS, **no_window_kwargs())
    if proc.returncode != 0:
        detail = (proc.stdout or "").strip()[-200:]
        raise RuntimeError(detail or f"ffmpeg 退出码 {proc.returncode}")


def _temp_path(path: Path) -> Path:
    # 临时名必须唯一（扩展名保留在末尾给 ffmpeg 推断格式）：同一音频被并发规整时
    # 共用固定的 xxx__fix.wav 会互相覆盖，失败清理还会删掉对方正在写的文件
    return path.parent / f"{path.stem}.{os.getpid()}.{os.urandom(4).hex()}{path.suffix}"


def normalize_audio(path, min_seconds: float = MIN_SECONDS,
                    max_seconds: float = MAX_SECONDS, log=None) -> dict:
    """把音频时长规整到 [min_seconds, max_seconds]，原地替换成功时返回新时长。"""
    path = Path(path)
    result = {"name": path.name, "path": str(path), "before": None, "after": None,
              "action": "skipped", "message": ""}
    if not path.is_file():
        result["action"] = "failed"
        result["message"] = "文件不存在"
        return result
    duration = probe_duration(path)
    if duration is None:
        result["action"] = "failed"
        result["message"] = "读不出音频时长"
        _emit(log, f"[音频规整] {path.name}：读不出音频时长，跳过")
        return result
    result["before"] = round(duration, 2)
    result["after"] = round(duration, 2)
    if min_seconds <= duration <= max_seconds:
        result["action"] = "ok"
        result["message"] = f"时长 {duration:.2f} 秒，已在范围内"
        return result
    if duration < min_seconds:
        return _pad_short(path, duration, min_seconds, result, log)
    return _trim_long(path, duration, min_seconds, max_seconds, result, log)


def _pad_short(path: Path, duration: float, min_seconds: float, result: dict, log) -> dict:
    target = min_seconds + PAD_MARGIN_SECONDS
    before = (target - duration) / 2.0
    exe = _ffmpeg()
    tmp = _temp_path(path)
    try:
        if exe:
            before_ms = int(round(before * 1000))
            filt = f"adelay={before_ms}|{before_ms},apad" if before_ms else "apad"
            _run_ffmpeg(exe, ["-i", str(path), "-af", filt,
                              "-t", f"{target:.3f}", str(tmp)])
        else:
            _pad_wav(path, tmp, before, target - duration - before)
        os.replace(tmp, path)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        result["action"] = "failed"
        result["message"] = f"补静音失败：{e}"
        _emit(log, f"[音频规整] {path.name}：补静音失败（{e}）")
        return result
    after = probe_duration(path)
    result["action"] = "padded"
    result["after"] = round(after, 2) if after else None
    result["message"] = f"时长 {duration:.2f} → {result['after']} 秒（前后补静音）"
    _emit(log, f"[音频规整] {path.name}：{result['message']}")
    return result


def _trim_long(path: Path, duration: float, min_seconds: float, max_seconds: float,
               result: dict, log) -> dict:
    exe = _ffmpeg()
    tmp = _temp_path(path)
    target = max_seconds - TRIM_MARGIN_SECONDS
    best = None
    try:
        for min_silence, keep_silence in TRIM_STEPS:
            if exe:
                _trim_ffmpeg(exe, path, tmp, min_silence, keep_silence)
            else:
                _trim_wav(path, tmp, target, min_silence, keep_silence)
            current = probe_duration(tmp)
            if current is None:
                raise RuntimeError("压缩后读不出时长")
            if current < min_seconds:
                raise RuntimeError(f"压缩后只剩 {current:.2f} 秒，过短")
            best = current
            if current <= target:
                break
    except Exception as e:
        tmp.unlink(missing_ok=True)
        result["action"] = "failed"
        result["message"] = f"压缩停顿失败：{e}"
        _emit(log, f"[音频规整] {path.name}：压缩停顿失败（{e}）")
        return result
    if best is None or abs(best - duration) < 0.01:
        tmp.unlink(missing_ok=True)
        result["action"] = "failed"
        result["message"] = f"没找到可压缩的停顿（{duration:.2f} 秒），建议更换该音频"
        _emit(log, f"[音频规整] {path.name}：{result['message']}")
        return result
    os.replace(tmp, path)
    result["action"] = "trimmed"
    result["after"] = round(best, 2)
    result["message"] = f"时长 {duration:.2f} → {best:.2f} 秒（压缩内部停顿）"
    if best > max_seconds:
        result["message"] += f"，已压到最短仍超过 {max_seconds:.0f} 秒，建议更换该音频"
    _emit(log, f"[音频规整] {path.name}：{result['message']}")
    return result


def _trim_ffmpeg(exe: str, src: Path, dst: Path, min_silence: float,
                 keep_silence: float) -> None:
    threshold_db = 20 * math.log10(SILENCE_THRESHOLD)
    filt = (f"silenceremove=stop_periods=-1:stop_duration={min_silence:.2f}:"
            f"stop_threshold={threshold_db:.0f}dB:stop_silence={keep_silence:.2f}")
    _run_ffmpeg(exe, ["-i", str(src), "-af", filt, str(dst)])


def _wav_params(path: Path):
    try:
        with wave.open(str(path), "rb") as wf:
            if wf.getcomptype() != "NONE":
                return None
            return (wf.getnchannels(), wf.getsampwidth(), wf.getframerate(),
                    wf.readframes(wf.getnframes()))
    except (wave.Error, OSError, EOFError):
        return None


def _pad_wav(src: Path, dst: Path, before_seconds: float, after_seconds: float) -> None:
    info = _wav_params(src)
    if info is None:
        raise RuntimeError("内置处理只支持未压缩的 WAV")
    channels, width, rate, raw = info
    frame_bytes = width * channels

    def silence(seconds):
        return b"\x00" * (max(0, int(round(seconds * rate))) * frame_bytes)

    with wave.open(str(dst), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        wf.writeframes(silence(before_seconds) + raw + silence(after_seconds))


def _trim_wav(src: Path, dst: Path, target_seconds: float, min_silence: float,
              keep_silence: float) -> None:
    info = _wav_params(src)
    if info is None:
        raise RuntimeError("内置处理只支持未压缩的 WAV")
    channels, width, rate, raw = info
    if width != 2:
        raise RuntimeError("内置处理只支持 16 位 WAV，请安装 ffmpeg")
    samples = array.array("h")
    samples.frombytes(raw[:len(raw) - len(raw) % (width * channels)])
    if sys.byteorder == "big":
        samples.byteswap()

    total_frames = len(samples) // channels
    window = max(1, int(rate * WAV_WINDOW_SECONDS))
    limit = SILENCE_THRESHOLD * PCM16_FULL_SCALE
    runs = []
    run_start = -1
    for start in range(0, total_frames, window):
        end = min(start + window, total_frames)
        chunk = samples[start * channels:end * channels]
        peak = max(max(chunk), -min(chunk))
        if peak < limit:
            if run_start < 0:
                run_start = start
        elif run_start >= 0:
            runs.append([run_start, start - run_start])
            run_start = -1
    if run_start >= 0:
        runs.append([run_start, total_frames - run_start])

    min_run = max(1, int(min_silence * rate))
    keep_frames = max(0, int(keep_silence * rate))
    # [起始帧, 原始长度, 当前保留长度]，从最长的停顿开始砍，砍够目标时长就停
    runs = [[start, length, length] for start, length in runs if length >= min_run]
    need = int(math.ceil((total_frames / float(rate) - target_seconds) * rate))
    while need > 0:
        target = None
        for run in runs:
            if run[2] - keep_frames > 0 and (target is None or run[2] > target[2]):
                target = run
        if target is None:
            break
        drop = min(target[2] - keep_frames, need)
        target[2] -= drop
        need -= drop

    out = array.array("h")
    cursor = 0
    for start, length, keep_len in sorted(runs, key=lambda r: r[0]):
        out.extend(samples[cursor * channels:(start + keep_len) * channels])
        cursor = start + length
    out.extend(samples[cursor * channels:])
    if sys.byteorder == "big":
        out.byteswap()
    with wave.open(str(dst), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        wf.writeframes(out.tobytes())
