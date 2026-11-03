import hashlib
import hmac
import os
import re
from base64 import b64encode, b64decode
from typing import Optional


_API_KEY_KEYS = ("llm_api_key", "image_caption_api_key", "cloud_tts_api_key",
                "asr_api_key", "napcat_token", "web_search_api_keys",
                "plugin_publish_token")

_WEBUI_PASSWORD_KEYS = ("webui_password", "webui_second_password")

_SECOND_PASSWORD_PATHS = frozenset({
    "/api/history", "/api/delete", "/api/history/delete_messages",
    "/api/config/save", "/api/config/import", "/api/config/export",
    "/api/config/profiles/save", "/api/config/profiles/delete",
    "/api/connections/save", "/api/connections/delete",
    "/api/model-library/save",
    "/api/jobs/save", "/api/jobs/batch", "/api/jobs/run",
    "/api/events/save", "/api/events/batch",
    "/api/todos/add", "/api/todos/update", "/api/todos/delete", "/api/todos/batch",
    "/api/tools/save",
    "/api/profiles/save", "/api/profiles/delete",
    "/api/lexicon/save", "/api/lexicon/delete",
    "/api/lexicon/confirm", "/api/lexicon/reject",
    "/api/stickers/upload", "/api/stickers/delete", "/api/stickers/auto_import",
    "/api/emotions/upload", "/api/emotions/create", "/api/emotions/delete",
    "/api/rag/upload", "/api/rag/delete",
    "/api/plugins/upload", "/api/plugins/install_remote", "/api/plugins/delete",
    "/api/plugins/settings",
    "/api/plugins/publish", "/api/plugins/unpublish",
    "/api/plugins/publish_token", "/api/plugins/publish_token_clear",
    "/api/tts/voice_design/create", "/api/tts/voice_design/delete",
    "/api/tts/voice_enrollment/create", "/api/tts/voice_enrollment/delete",
})

_SECOND_PASSWORD_PREFIXES = ("/api/memory/",)

_CONNECTION_SECRET_KEYS = ("token", "app_secret", "bot_token")


def _needs_second_password(path: str) -> bool:
    return path in _SECOND_PASSWORD_PATHS or path.startswith(_SECOND_PASSWORD_PREFIXES)


def _password_matches(given, expected) -> bool:
    return hmac.compare_digest(str(given or "").encode("utf-8"),
                               str(expected or "").encode("utf-8"))


_ENC_PREFIX = "enc2:"
_ENC_KEY_CACHE = None


def _enc_key() -> bytes:
    global _ENC_KEY_CACHE
    if _ENC_KEY_CACHE is None:
        material = b"Lovomo-KEYSTORE-v2"
        try:
            if os.name == "nt":
                import winreg
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                    r"SOFTWARE\Microsoft\Cryptography") as reg_key:
                    material += str(winreg.QueryValueEx(reg_key, "MachineGuid")[0]).encode("utf-8")
        except Exception:
            pass
        material += os.environ.get("COMPUTERNAME", "").encode("utf-8")
        material += os.environ.get("USERNAME", "").encode("utf-8")
        _ENC_KEY_CACHE = hashlib.pbkdf2_hmac("sha256", material,
                                             b"Lovomo-KEYSTORE-SALT-v2", 100_000)
    return _ENC_KEY_CACHE


def _keystream(nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hmac.new(_enc_key(), nonce + counter.to_bytes(8, "big"),
                            hashlib.sha256).digest())
        counter += 1
    return bytes(out[:length])


def _encrypt_value(value: str) -> str:
    v = str(value or "")
    if not v or v.startswith(_ENC_PREFIX) or v.startswith("enc:"):
        return v
    if _is_masked_value(v):
        return v
    raw = v.encode("utf-8")
    nonce = os.urandom(16)
    checksum = hashlib.sha256(raw).digest()[:8]
    ks = _keystream(nonce, len(raw))
    ct = bytes(a ^ b for a, b in zip(raw, ks))
    return _ENC_PREFIX + b64encode(nonce + checksum + ct).decode("ascii")


def _decrypt_value(value: str) -> Optional[str]:
    v = str(value or "")
    if v.startswith("enc:"):
        try:
            return b64decode(v[4:].encode("ascii")).decode("utf-8")
        except Exception as e:
            print(f"[加密] API 密钥解密失败，保留原密文：{e}")
            return None
    if not v.startswith(_ENC_PREFIX):
        return v
    try:
        blob = b64decode(v[len(_ENC_PREFIX):].encode("ascii"))
        nonce, checksum, ct = blob[:16], blob[16:24], blob[24:]
        raw = bytes(a ^ b for a, b in zip(ct, _keystream(nonce, len(ct))))
        if hashlib.sha256(raw).digest()[:8] != checksum:
            raise ValueError("校验不匹配（密钥绑定本机，可能来自其他电脑）")
        return raw.decode("utf-8")
    except Exception as e:
        print(f"[加密] API 密钥解密失败，保留原密文（不再置空，请重新在 WebUI 填写）：{e}")
        return None


def _mask_preview(value: str) -> str:
    v = str(value or "")
    if not v:
        return ""
    if len(v) < 12:
        return f"****（{len(v)}位）"
    return f"{v[:4]}****{v[-4:]}（{len(v)}位）"


def _is_masked_value(value) -> bool:
    v = str(value or "")
    return v == "********" or ("****" in v and v.endswith("位）"))


def _is_kept_secret(value) -> bool:
    return _is_masked_value(value) or not str(value or "").strip()


def _encrypt_api_keys(config: dict) -> dict:
    for key in _API_KEY_KEYS:
        val = config.get(key)
        if isinstance(val, dict):
            config[key] = {k: _encrypt_value(v) if isinstance(v, str) else v
                           for k, v in val.items()}
        elif isinstance(val, str):
            config[key] = _encrypt_value(val)
    for role in (config.get("roles") or []):
        if isinstance(role, dict) and isinstance(role.get("napcat_token"), str):
            role["napcat_token"] = _encrypt_value(role["napcat_token"])
    return config


def _encrypt_webui_password(config: dict) -> dict:
    for key in _WEBUI_PASSWORD_KEYS:
        val = config.get(key)
        if isinstance(val, str):
            config[key] = _encrypt_value(val)
    return config


_SECRET_FIELD_KEYS = _API_KEY_KEYS + _WEBUI_PASSWORD_KEYS

_PLAINTEXT_SECRET_PREFIXES = ("sk-", "sk_", "ak-", "ak_", "ghp_", "gho_", "xoxb-",
                              "AIza", "Bearer ", "eyJhbGciOi")

_EMBEDDED_SECRET_RE = re.compile(
    r"(?:sk-|sk_|ak-|ak_|ghp_|gho_|xoxb-|AIza)[A-Za-z0-9_\-]{8,}"
    r"|Bearer\s+[A-Za-z0-9_\-\.]{12,}"
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")

_SECRET_REDACTION = "********"


def _looks_like_plaintext_secret(value) -> bool:
    v = str(value or "").strip()
    if not v:
        return False
    if v.startswith(_ENC_PREFIX) or v.startswith("enc:"):
        return False          # 已经是密文，原样保留
    if _is_masked_value(v):
        return False          # 打码预览（****），不含真实密钥
    if any(v.startswith(p) for p in _PLAINTEXT_SECRET_PREFIXES):
        return True
    return bool(_EMBEDDED_SECRET_RE.search(v))


def _scrub_value(value: str) -> Optional[str]:
    v = str(value or "")
    if not v:
        return None
    if v.startswith(_ENC_PREFIX) or v.startswith("enc:"):
        return None           # 整串已是密文
    if _is_masked_value(v):
        return None           # 打码预览，无需处理

    replaced = False

    def _sub(m):
        nonlocal replaced
        token = m.group(0)
        if token.startswith(_ENC_PREFIX) or token.startswith("enc:"):
            return token
        if _is_masked_value(token):
            return token
        encrypted = _encrypt_value(token)
        replaced = True
        if encrypted.startswith(_ENC_PREFIX):
            return encrypted
        return _SECRET_REDACTION

    out = _EMBEDDED_SECRET_RE.sub(_sub, v)
    return out if replaced else None


def _scrub_plaintext_secrets(config: dict) -> dict:
    def scrub(node, in_secret_field: bool):
        if isinstance(node, dict):
            return {k: scrub(v, in_secret_field or k in _SECRET_FIELD_KEYS)
                    for k, v in node.items()}
        if isinstance(node, list):
            return [scrub(v, in_secret_field) for v in node]
        if not isinstance(node, str):
            return node
        if not node:
            return node           # 空串：没填密钥，保持原样
        if node.startswith(_ENC_PREFIX) or node.startswith("enc:"):
            return node
        if _is_masked_value(node):
            return node
        if in_secret_field:
            encrypted = _encrypt_value(node)
            if encrypted.startswith(_ENC_PREFIX):
                return encrypted
            return _SECRET_REDACTION
        scrubbed = _scrub_value(node)
        return node if scrubbed is None else scrubbed

    if not isinstance(config, dict):
        return config
    return scrub(config, False)


def _decrypt_api_keys(config: dict) -> dict:
    for key in _API_KEY_KEYS:
        val = config.get(key)
        if isinstance(val, dict):
            decoded = {}
            for k, v in val.items():
                plain = _decrypt_value(v) if isinstance(v, str) else v
                decoded[k] = v if plain is None else plain
            config[key] = decoded
        elif isinstance(val, str):
            plain = _decrypt_value(val)
            if plain is not None:
                config[key] = plain
    for role in (config.get("roles") or []):
        if isinstance(role, dict) and isinstance(role.get("napcat_token"), str):
            plain = _decrypt_value(role["napcat_token"])
            if plain is not None:
                role["napcat_token"] = plain
    return config


def _decrypt_webui_password(config: dict) -> dict:
    for key in _WEBUI_PASSWORD_KEYS:
        val = config.get(key)
        if isinstance(val, str) and (val.startswith(_ENC_PREFIX) or val.startswith("enc:")):
            plain = _decrypt_value(val)
            if plain is not None:
                config[key] = plain
    return config
