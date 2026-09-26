"""云端声音设计：调百炼 /services/audio/tts/customization 管理自定义音色。

用一段文字描述设计音色，拿到的 voice 值填进云 TTS 的音色字段即可（声音设计类模型
只认这样创建出来的音色，配系统音色会被上游拒绝）。创建时的 target_model 必须与
合成时用的模型一致，所以这里默认取「云 TTS 模型」。
"""
import base64
import re
from typing import List

import httpx

from .tls import verified_context

CUSTOMIZATION_PATH = "/services/audio/tts/customization"
DESIGN_MODEL = "qwen-voice-design"

# 声音描述与预览文本支持的语言，顺序即界面下拉的顺序
LANGUAGES = ("zh", "en", "de", "it", "pt", "es", "ja", "ko", "fr", "ru")
DEFAULT_LANGUAGE = "zh"

_NAME_RE = re.compile(r"[0-9A-Za-z_]{1,16}\Z")


class VoiceDesignError(Exception):
    """接口返回的失败原因，文案可直接展示给用户。"""


async def _call(config, body: dict) -> dict:
    base_url = str(config.get("cloud_tts_base_url", "") or "").strip().rstrip("/")
    api_key = str(config.get("cloud_tts_api_key", "") or "").strip()
    if not base_url:
        raise VoiceDesignError("未配置云 TTS 服务地址")
    if not api_key:
        raise VoiceDesignError("未配置云 TTS API Key")
    async with httpx.AsyncClient(timeout=config.get("timeout_seconds", 120),
                                 trust_env=False, verify=verified_context()) as client:
        resp = await client.post(f"{base_url}{CUSTOMIZATION_PATH}", json=body,
                                 headers={"Authorization": f"Bearer {api_key}"})
    try:
        data = resp.json()
    except Exception:
        raise VoiceDesignError(f"响应不是 JSON（HTTP {resp.status_code}）")
    if resp.status_code >= 400:
        raise VoiceDesignError(str(data.get("message") or data.get("code")
                                   or f"HTTP {resp.status_code}"))
    return data.get("output") or {}


def _preview_bytes(out: dict) -> bytes:
    data = str((out.get("preview_audio") or {}).get("data") or "")
    if not data:
        return b""
    try:
        return base64.b64decode(data)
    except Exception:
        return b""


async def create_voice(config, *, voice_prompt: str, preview_text: str, name: str,
                       language: str = "", target_model: str = "") -> dict:
    """按描述设计一个音色，返回 voice / target_model / preview_audio(wav 字节)。"""
    model = str(target_model or config.get("cloud_tts_model", "") or "").strip()
    if not model:
        raise VoiceDesignError("先在「云 TTS 模型」里填要驱动的模型")
    if not _NAME_RE.match(str(name or "")):
        raise VoiceDesignError("音色名称只能包含字母、数字与下划线，且不超过 16 个字符")
    prompt = str(voice_prompt or "").strip()
    preview = str(preview_text or "").strip()
    if not prompt:
        raise VoiceDesignError("请填写声音描述")
    if not preview:
        raise VoiceDesignError("请填写预览文本")
    out = await _call(config, {
        "model": DESIGN_MODEL,
        "input": {"action": "create", "target_model": model, "preferred_name": name,
                  "voice_prompt": prompt, "preview_text": preview,
                  "language": str(language or "").strip() or DEFAULT_LANGUAGE},
    })
    return {"voice": str(out.get("voice") or name),
            "target_model": str(out.get("target_model") or model),
            "preview_audio": _preview_bytes(out)}


async def list_voices(config, *, page_index: int = 0, page_size: int = 50) -> List[dict]:
    """列出已创建的音色，返回 voice_list 原样条目。"""
    out = await _call(config, {"model": DESIGN_MODEL,
                               "input": {"action": "list", "page_index": page_index,
                                         "page_size": page_size}})
    return list(out.get("voice_list") or [])


async def delete_voice(config, name: str) -> str:
    out = await _call(config, {"model": DESIGN_MODEL,
                               "input": {"action": "delete", "voice": str(name or "")}})
    return str(out.get("voice") or name)
