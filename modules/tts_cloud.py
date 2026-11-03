import asyncio
import base64
import re
import time
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlsplit

import httpx

from .tls import verified_context

LOCAL_BACKEND = "local"
CLOUD_BACKEND = "cloud"

PROTOCOL_OPENAI = "openai"
PROTOCOL_DASHSCOPE = "dashscope"

_DASHSCOPE_PATH = "/services/aigc/multimodal-generation/generation"

DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_BACKOFF_SECONDS = 0.6
DOWNLOAD_CONNECT_TIMEOUT = 10.0
DOWNLOAD_READ_TIMEOUT = 60.0

_DASHSCOPE_LANGS = {"zh": "Chinese", "ja": "Japanese", "en": "English"}

_VOICE_MAP_SPLIT_RE = re.compile(r"[=:：]")


def is_cloud_tts(config) -> bool:
    return str(config.get("tts_backend", LOCAL_BACKEND) or LOCAL_BACKEND) \
        .strip().lower() == CLOUD_BACKEND


def parse_voice_map(raw) -> dict:
    mapping = {}
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = _VOICE_MAP_SPLIT_RE.split(line, 1)
        if len(parts) != 2:
            continue
        name, voice = parts[0].strip(), parts[1].strip()
        if name and voice:
            mapping[name] = voice
    return mapping


def resolve_voice(config, emotion: str) -> str:
    mapping = parse_voice_map(config.get("cloud_tts_voice_map", ""))
    return mapping.get(str(emotion or "").strip()) \
        or str(config.get("cloud_tts_voice", "") or "").strip()


def cloud_emotion_names(config) -> list:
    names = []
    for name in [str(config.get("default_voice", "") or "").strip()] \
            + list(parse_voice_map(config.get("cloud_tts_voice_map", ""))):
        if name and name not in names:
            names.append(name)
    return names


async def _download_audio(url: str) -> Tuple[Optional[bytes], str]:
    last = ""
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(
                    timeout=httpx.Timeout(DOWNLOAD_READ_TIMEOUT,
                                          connect=DOWNLOAD_CONNECT_TIMEOUT),
                    trust_env=False, verify=verified_context()) as client:
                got = await client.get(url)
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        else:
            if got.status_code < 400:
                return got.content, ""
            last = f"HTTP {got.status_code}"
            if got.status_code < 500:
                break
        if attempt < DOWNLOAD_ATTEMPTS:
            await asyncio.sleep(DOWNLOAD_BACKOFF_SECONDS * attempt)
    return None, f"下载音频失败 {last}（{urlsplit(url).hostname or url}，试了 {DOWNLOAD_ATTEMPTS} 次）"


async def _dashscope_audio(resp) -> Tuple[Optional[bytes], str]:
    try:
        data = resp.json()
    except Exception:
        return None, f"响应不是 JSON {resp.text[:120]}"
    audio = (data.get("output") or {}).get("audio") or {}
    encoded = str(audio.get("data") or "")
    if encoded:
        try:
            return base64.b64decode(encoded), ""
        except Exception as e:
            return None, f"Base64 解码失败 {type(e).__name__}"
    url = str(audio.get("url") or "")
    if not url:
        return None, str(data.get("message") or data.get("code") or "响应里没有 audio 字段")
    return await _download_audio(url)


async def synthesize_cloud(config, text: str, emotion: str, data_path: Path,
                           stats=None) -> Optional[Path]:
    from .tts import (_audio_too_long, _audio_too_short, _configured_char_map,
                      _log_tts_payload, _looks_like_audio, _normalize_tts_output,
                      _safe_print, _sanitize_tts_text, _unique_stamp, _wav_duration,
                      detect_text_lang, strip_non_dialogue_enabled,
                      strip_non_dialogue_text, strip_urls_for_tts)

    text = strip_urls_for_tts(str(text or ""))
    strip_nondialogue = strip_non_dialogue_enabled(config)
    if strip_nondialogue:
        text = strip_non_dialogue_text(text)
    if not re.sub(r'[\s。，！？、,.!?…～~；;：:]+', '', text):
        _safe_print("TTS skipped: punctuation-only sentence "
                    f"({text.encode('unicode_escape').decode('ascii')})")
        return None
    clean_text = _sanitize_tts_text(text, emotion=emotion,
                                    extra_map=_configured_char_map(config))
    if not clean_text:
        _safe_print("TTS skipped: empty after sanitize "
                    f"({text[:30].encode('unicode_escape').decode('ascii')})")
        return None
    protocol = str(config.get("cloud_tts_protocol", PROTOCOL_OPENAI)
                   or PROTOCOL_OPENAI).strip().lower()
    base_url = str(config.get("cloud_tts_base_url", "") or "").strip().rstrip("/")
    model = str(config.get("cloud_tts_model", "") or "").strip()
    voice = resolve_voice(config, emotion)
    api_key = str(config.get("cloud_tts_api_key", "") or "").strip()
    if not base_url or not model:
        _safe_print("云 TTS 未配置服务地址或模型，本句改为只发文本。")
        return None
    if not voice:
        _safe_print(f"云 TTS 未配置音色（情绪={emotion or '空'}），本句改为只发文本。")
        return None
    cfg_lang = str(config.get("text_lang", "ja") or "ja")
    lang = detect_text_lang(clean_text, cfg_lang) if bool(config.get("tts_auto_lang", True)) \
        else cfg_lang
    _log_tts_payload(config, text, clean_text, emotion, voice, cloud=True,
                     spoken_source=text if strip_nondialogue else None)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    if protocol == PROTOCOL_DASHSCOPE:
        endpoint = f"{base_url}{_DASHSCOPE_PATH}"
        payload = {"model": model, "input": {"text": clean_text, "voice": voice}}
        if lang in _DASHSCOPE_LANGS:
            payload["input"]["language_type"] = _DASHSCOPE_LANGS[lang]
    else:
        endpoint = f"{base_url}/audio/speech"
        payload = {"model": model, "input": clean_text, "voice": voice,
                   "response_format": "wav"}
    timeout = config.get("timeout_seconds", 120)
    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False,
                                     verify=verified_context()) as client:
            print(f"正在合成(云): 音色={voice} | 模型={model} | "
                  f"语言={lang} | 文本={clean_text}")
            resp = await client.post(endpoint, json=payload, headers=headers)
            if resp.status_code >= 400:
                print(f"云 TTS 合成失败: {resp.status_code} - {str(resp.text)[:200]} | "
                      f"模型={model} 音色={voice} 语言={lang} 文本={clean_text[:60]}")
                return None
            if protocol == PROTOCOL_DASHSCOPE:
                audio, reason = await _dashscope_audio(resp)
                if audio is None:
                    print(f"云 TTS 响应里没有音频（{reason}）| 文本={clean_text[:60]}")
                    return None
            else:
                audio = resp.content
    except Exception as e:
        print(f"云 TTS 请求异常 ({type(e).__name__}: {e})，本次放弃该句语音。")
        return None
    temp_path = data_path / f"temp_{emotion or 'cloud'}_{_unique_stamp()}.wav"
    temp_path.write_bytes(audio)
    _normalize_tts_output(temp_path, config)
    if _wav_duration(temp_path) <= 0:
        if _looks_like_audio(temp_path):
            _safe_print("云 TTS 返回的不是 wav 容器，无法按时长校验，原样交给发送层。")
            return temp_path
        temp_path.unlink(missing_ok=True)
        _safe_print("云 TTS 返回的内容不是音频，已丢弃，本句改为只发文本。")
        return None
    if _audio_too_long(temp_path, clean_text, config):
        temp_path.unlink(missing_ok=True)
        _safe_print("云 TTS 合成结果明显偏长，已丢弃，本句改为只发文本。")
        return None
    if _audio_too_short(temp_path, clean_text, config):
        _safe_print("云 TTS 合成结果偏短，仍按原样使用（避免整句语音缺失）。")
    if stats:
        stats.record_tts((time.time() - start) * 1000)
    print(f"合成完成(云): {emotion or '默认音色'} | {clean_text[:30]}...")
    return temp_path
