# 凭据加密/解密、明文密钥清扫打码、二级密码把守判定。
# 自 main.py 原样搬迁：函数体、注释、docstring 一字未改。
import hashlib
import hmac
import os
import re
from base64 import b64encode, b64decode
from typing import Optional


_API_KEY_KEYS = ("llm_api_key", "image_caption_api_key", "cloud_tts_api_key", "napcat_token", "web_search_api_keys", "plugin_publish_token")

# WebUI 的两个密码：访问密码（登录）与二级密码（敏感操作前再验一次）
_WEBUI_PASSWORD_KEYS = ("webui_password", "webui_second_password")

# 二级密码守的接口：读聊天记录、保存、导入导出、装插件、删除、发布与下架
_SECOND_PASSWORD_PATHS = frozenset({
    "/api/history", "/api/delete", "/api/history/delete_messages",
    "/api/config/save", "/api/config/import", "/api/config/export",
    "/api/config/profiles/save", "/api/config/profiles/delete",
    "/api/connections/save", "/api/connections/delete",
    "/api/roles/save", "/api/jobs/save", "/api/jobs/batch", "/api/jobs/run",
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

# 前缀命中即算敏感（整个记忆库的导入导出）
_SECOND_PASSWORD_PREFIXES = ("/api/memory/",)

# 接入方式里属于凭据的字段：读给前端时只回掩码，保存时掩码值表示"不改"
_CONNECTION_SECRET_KEYS = ("token", "app_secret", "bot_token")


def _needs_second_password(path: str) -> bool:
    return path in _SECOND_PASSWORD_PATHS or path.startswith(_SECOND_PASSWORD_PREFIXES)


def _password_matches(given, expected) -> bool:
    """密码比较走定时安全比较：用 == 逐字符比较会按相同前缀的长度产生时间差。"""
    return hmac.compare_digest(str(given or "").encode("utf-8"),
                               str(expected or "").encode("utf-8"))


_ENC_PREFIX = "enc2:"
_ENC_KEY_CACHE = None


def _enc_key() -> bytes:
    """加密密钥绑定本机（Windows MachineGuid 等），config.json 拷到别的机器解不开。"""
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
    # 打码预览（****）不是真实密钥：加密它等于把一行掩码存成密钥，
    # 下次读出来还会当成真的密钥去请求上游。
    if _is_masked_value(v):
        return v
    raw = v.encode("utf-8")
    nonce = os.urandom(16)
    checksum = hashlib.sha256(raw).digest()[:8]
    ks = _keystream(nonce, len(raw))
    ct = bytes(a ^ b for a, b in zip(raw, ks))
    return _ENC_PREFIX + b64encode(nonce + checksum + ct).decode("ascii")


def _decrypt_value(value: str) -> Optional[str]:
    """解密失败返回 None：调用方必须保留原密文，绝不能把密钥写成空串。

    密文绑定了本机信息，换电脑后解不开是正常的；此时若置空，下一次保存
    就会把空值加密回磁盘，原密钥不可恢复。
    """
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
    """密钥字段的「没改动」判定：空值或打码预览都表示沿用磁盘上已有的值。

    配置文件页不显示密钥（密钥只在设置页管理），那一页保存回来的密钥字段必然
    是空的；没有这条规则，随便改个开关就会把密钥覆盖成空串。
    """
    return _is_masked_value(value) or not str(value or "").strip()


def _encrypt_api_keys(config: dict) -> dict:
    for key in _API_KEY_KEYS:
        val = config.get(key)
        if isinstance(val, dict):
            config[key] = {k: _encrypt_value(v) if isinstance(v, str) else v
                           for k, v in val.items()}
        elif isinstance(val, str):
            config[key] = _encrypt_value(val)
    # 角色可以各自配一个 NapCat 令牌，它和顶层 napcat_token 一样是登录凭据，
    # 只处理顶层键会让它明文留在 config.json 里
    for role in (config.get("roles") or []):
        if isinstance(role, dict) and isinstance(role.get("napcat_token"), str):
            role["napcat_token"] = _encrypt_value(role["napcat_token"])
    return config


def _encrypt_webui_password(config: dict) -> dict:
    """WebUI 访问密码与二级密码落盘前加密。

    它们不是 API 密钥，所以不在 _API_KEY_KEYS 里、_encrypt_api_keys 不会碰它们；
    但同样是登录凭据，必须与 API 密钥一样做到「磁盘密文 / 内存明文」。
    """
    for key in _WEBUI_PASSWORD_KEYS:
        val = config.get(key)
        if isinstance(val, str):
            config[key] = _encrypt_value(val)
    return config


# 导出/写盘前必须确认「不可能是明文」的字段：除 _API_KEY_KEYS 外，WebUI 访问
# 密码与二级密码同样属于登录凭据，绝不能以明文形式随导出文件外流。
_SECRET_FIELD_KEYS = _API_KEY_KEYS + _WEBUI_PASSWORD_KEYS

# 已知的明文密钥前缀：命中即视为真实密钥，必须加密后才允许出现在导出内容里
_PLAINTEXT_SECRET_PREFIXES = ("sk-", "sk_", "ak-", "ak_", "ghp_", "gho_", "xoxb-",
                              "AIza", "Bearer ", "eyJhbGciOi")

# 嵌在长文本 / JSON 串里的密钥片段：如 llm_extra_body={"headers":{"k":"sk-xxx"}}
_EMBEDDED_SECRET_RE = re.compile(
    r"(?:sk-|sk_|ak-|ak_|ghp_|gho_|xoxb-|AIza)[A-Za-z0-9_\-]{8,}"
    r"|Bearer\s+[A-Za-z0-9_\-\.]{12,}"
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")

_SECRET_REDACTION = "********"


def _looks_like_plaintext_secret(value) -> bool:
    """判断字符串是否是「不该出现在导出结果里」的明文密钥。"""
    v = str(value or "").strip()
    if not v:
        return False
    if v.startswith(_ENC_PREFIX) or v.startswith("enc:"):
        return False          # 已经是密文，原样保留
    if _is_masked_value(v):
        return False          # 打码预览（****），不含真实密钥
    if any(v.startswith(p) for p in _PLAINTEXT_SECRET_PREFIXES):
        return True
    # 密钥埋在长文本/JSON 串里：用正则捞出疑似密钥片段
    return bool(_EMBEDDED_SECRET_RE.search(v))


def _scrub_value(value: str) -> Optional[str]:
    """把单个字符串里的明文密钥整段替换为对应密文。

    返回 None 表示该串不含任何明文密钥，调用方可原样保留。
    """
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
        return _SECRET_REDACTION      # 实在加密不了就整段打码，绝不留下明文

    out = _EMBEDDED_SECRET_RE.sub(_sub, v)
    return out if replaced else None


def _scrub_plaintext_secrets(config: dict) -> dict:
    """导出兜底：敏感字段里的明文密钥一律加密，残留明文密钥一律打码。

    _encrypt_api_keys 只覆盖 _API_KEY_KEYS 列表里的字段；配置里别的键（嵌套
    结构、新增的密钥项、webui_password）如果带着明文，就会直接进导出文件。
    这里做一次与字段名无关的全量扫描：

    - 敏感字段（_SECRET_FIELD_KEYS）：明文一律 _encrypt_value 成 enc2:...，
      解不开的外来密文原样保留；
    - 其他字段：按 sk- 等已知前缀识别，命中的密钥片段加密成 enc2:...；连成串
      嵌在文本/JSON 里的密钥也会被逐段替换，绝不留下明文。
    """
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
        # 打码预览（****）本就不含真实密钥，任何字段里都原样保留
        if _is_masked_value(node):
            return node
        if in_secret_field:
            # _encrypt_value 对空串/已是密文的值会原样返回，非空明文必然带前缀
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
    """WebUI 访问密码与二级密码读盘后解密，与 _encrypt_webui_password 对称。

    解不开（换过电脑）时保留原密文、不置空——置空会让「导入配置」里
    密码变成空串，等于把密码静默清掉。
    """
    for key in _WEBUI_PASSWORD_KEYS:
        val = config.get(key)
        if isinstance(val, str) and (val.startswith(_ENC_PREFIX) or val.startswith("enc:")):
            plain = _decrypt_value(val)
            if plain is not None:
                config[key] = plain
    return config
