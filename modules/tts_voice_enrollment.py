"""云端声音复刻：用上传的音频克隆音色（百炼 /services/audio/tts/customization）。

模型固定 qwen-voice-enrollment，驱动模型（target_model）必须与合成时用的模型一致，
复刻出来的 voice 填进云 TTS 的音色字段即可。接口一次只收一段音频，上传多个时先合并：
有 ffmpeg 就统一转成单声道 24kHz WAV，没有则只支持单个音频原样提交。
"""
import base64
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Sequence, Tuple

from .audio_trim import probe_duration
from .tts_service import no_window_kwargs
from .tts_voice_design import VoiceDesignError
from .tts_voice_design import _call as customization_call

ENROLLMENT_MODEL = "qwen-voice-enrollment"
# Qwen-TTS 复刻要求采样率不低于 24kHz 且只收单声道
TARGET_SAMPLE_RATE = 24000
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_AUDIO_SECONDS = 60.0
FFMPEG_TIMEOUT_SECONDS = 300.0

# 音频文本支持的语言，顺序即界面下拉的顺序
LANGUAGES = ("zh", "en", "de", "it", "pt", "es", "ja", "ko", "fr", "ru")
DEFAULT_LANGUAGE = "zh"

_NAME_RE = re.compile(r"[0-9A-Za-z_]{1,16}\Z")
_MIME_BY_SUFFIX = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4"}


class VoiceEnrollmentError(Exception):
    """复刻失败的原因，文案可直接展示给用户。"""


async def _call(config, body: dict) -> dict:
    try:
        return await customization_call(config, body)
    except VoiceDesignError as e:
        raise VoiceEnrollmentError(str(e))


def _ffmpeg():
    return shutil.which("ffmpeg")


def _suffix(name: str) -> str:
    return Path(str(name or "")).suffix.lower()


def _run_ffmpeg(exe: str, inputs: List[Path], out: Path) -> None:
    args = [exe, "-y", "-hide_banner", "-loglevel", "error"]
    for path in inputs:
        args += ["-i", str(path)]
    chain = "".join(f"[{i}:a]" for i in range(len(inputs)))
    args += ["-filter_complex", f"{chain}concat=n={len(inputs)}:v=0:a=1[out]",
             "-map", "[out]", "-ar", str(TARGET_SAMPLE_RATE), "-ac", "1",
             "-c:a", "pcm_s16le", str(out)]
    proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=FFMPEG_TIMEOUT_SECONDS, **no_window_kwargs())
    if proc.returncode != 0 or not out.exists():
        detail = (proc.stdout or "").strip()[-200:]
        raise VoiceEnrollmentError(f"合并音频失败：{detail or 'ffmpeg 退出码 ' + str(proc.returncode)}")


def _prepare_audio(audios: Sequence[Tuple[str, bytes]]) -> Tuple[bytes, str]:
    """把上传的音频整成一段可提交的音频，返回 (音频字节, MIME)。"""
    if not audios:
        raise VoiceEnrollmentError("请先选择要上传的音频")
    for name, data in audios:
        if _suffix(name) not in _MIME_BY_SUFFIX:
            raise VoiceEnrollmentError(f"{name}：只支持 WAV、MP3、M4A")
        if not data:
            raise VoiceEnrollmentError(f"{name}：文件是空的")
    exe = _ffmpeg()
    if not exe:
        if len(audios) > 1:
            raise VoiceEnrollmentError("合并多个音频需要 ffmpeg，请只上传一个音频或先安装 ffmpeg")
        audio, mime = audios[0][1], _MIME_BY_SUFFIX[_suffix(audios[0][0])]
    else:
        with tempfile.TemporaryDirectory(prefix="lovomo_enroll_") as tmp:
            work = Path(tmp)
            inputs = []
            for i, (name, data) in enumerate(audios):
                path = work / f"in{i}{_suffix(name)}"
                path.write_bytes(data)
                inputs.append(path)
            out = work / "merged.wav"
            _run_ffmpeg(exe, inputs, out)
            seconds = probe_duration(out)
            if seconds and seconds > MAX_AUDIO_SECONDS:
                raise VoiceEnrollmentError(
                    f"音频总时长 {seconds:.0f} 秒，超过 {MAX_AUDIO_SECONDS:.0f} 秒上限")
            audio, mime = out.read_bytes(), "audio/wav"
    if len(audio) > MAX_AUDIO_BYTES:
        raise VoiceEnrollmentError("音频超过 10MB，请换短一些的样本")
    return audio, mime


async def create_voice(config, *, audios: Sequence[Tuple[str, bytes]], name: str,
                       text: str = "", language: str = "",
                       target_model: str = "") -> dict:
    """用上传的音频复刻一个音色，返回 voice / target_model / 降级信息。"""
    model = str(target_model or config.get("cloud_tts_model", "") or "").strip()
    if not model:
        raise VoiceEnrollmentError("先在「云 TTS 模型」里填要驱动的模型")
    if not _NAME_RE.match(str(name or "")):
        raise VoiceEnrollmentError("音色名称只能包含字母、数字与下划线，且不超过 16 个字符")
    audio, mime = _prepare_audio(audios)
    payload = {"action": "create", "target_model": model, "preferred_name": name,
               "audio": {"data": f"data:{mime};base64,{base64.b64encode(audio).decode('ascii')}"},
               "language": str(language or "").strip() or DEFAULT_LANGUAGE}
    transcript = str(text or "").strip()
    if transcript:
        payload["text"] = transcript
    out = await _call(config, {"model": ENROLLMENT_MODEL, "input": payload})
    return {"voice": str(out.get("voice") or name),
            "target_model": str(out.get("target_model") or model),
            "fallback_mode": bool(out.get("fallback_mode")),
            "fallback_reason": str(out.get("fallback_reason") or "")}


async def list_voices(config, *, page_index: int = 0, page_size: int = 50) -> List[dict]:
    """列出已复刻的音色，返回 voice_list 原样条目。"""
    out = await _call(config, {"model": ENROLLMENT_MODEL,
                               "input": {"action": "list", "page_index": page_index,
                                         "page_size": page_size}})
    return list(out.get("voice_list") or [])


async def delete_voice(config, name: str) -> str:
    out = await _call(config, {"model": ENROLLMENT_MODEL,
                               "input": {"action": "delete", "voice": str(name or "")}})
    return str(out.get("voice") or name)
