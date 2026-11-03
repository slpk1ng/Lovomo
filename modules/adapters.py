import asyncio
import base64
import dataclasses
import json
import os
import random
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp

from napcat import (At, GroupMessageEvent, Image, MessageSender, PrivateMessageEvent,
                    Record, Text)

ILINK_BASE = "https://ilinkai.weixin.qq.com"
ILINK_CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_ITEM_TEXT = 1
ILINK_ITEM_IMAGE = 2
ILINK_ITEM_VOICE = 3
ILINK_ITEM_FILE = 4
ILINK_ITEM_VIDEO = 5
WECHAT_MEDIA_DIR = Path(tempfile.gettempdir()) / "lovomo_wechat_media"

ILINK_BOT_TYPE = "3"
ILINK_CHANNEL_VERSION = "lovomo"
ILINK_CLIENT_VERSION = "1"
ILINK_TYPING_TICKET_TTL = 60
ILINK_TYPING_START = 1
ILINK_TYPING_CANCEL = 2
ILINK_ACK_ID_KEYS = ("message_id", "msg_id", "server_id", "id")
ILINK_SESSION_TIMEOUT = -14
ILINK_STALE_CONTEXT = -2
ILINK_PENDING_LIMIT = 3
QQ_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
QQ_API_BASE = "https://api.sgroup.qq.com"
QQ_SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"
# 群与单聊消息（C2C_MESSAGE_CREATE / GROUP_AT_MESSAGE_CREATE）
QQ_INTENTS_GROUP_C2C = 1 << 25
QQ_HEARTBEAT_FALLBACK = 30.0
QQ_HEARTBEAT_MIN = 5.0
QQ_HELLO_TIMEOUT = 15.0


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

def client_supports(client, feature: str, default: bool = True) -> bool:
    caps = getattr(type(client), "capabilities", None)
    if not isinstance(caps, dict):
        return default
    return bool(caps.get(feature, default))


def recent_ilink_target(connection: dict) -> str:
    tokens = (connection or {}).get("context_tokens")
    if isinstance(tokens, dict) and tokens:
        return str(list(tokens)[-1])
    return ""


def friendly_user_label(platform: str, user_id: str, nickname: str = "") -> str:
    uid = str(user_id or "")
    name = str(nickname or "").strip()
    if name and name != uid:
        return name
    if not uid:
        return ""
    if uid.endswith("@im.wechat"):
        head = uid.split("@", 1)[0]
        return f"微信用户 {head[:6]}…{head[-4:]}" if len(head) > 12 else f"微信用户 {head}"
    if uid.endswith("@im.bot"):
        return "机器人"
    return uid


def make_event(*, private: bool, user_id, text: str, group_id=None,
               message_id=0, nickname: str = "", self_id=0, raw=None,
               at_bot: bool = False, extra_segments=None):
    platform = "wechat_clawbot" if str(user_id).endswith("@im.wechat") else ""
    sender = MessageSender(user_id=user_id,
                           nickname=nickname or friendly_user_label(platform, str(user_id)))
    segments = ([At(qq=str(self_id))] if at_bot else []) + [Text(text=text)]
    segments.extend(seg for seg in (extra_segments or []) if seg is not None)
    common = {"time": int(time.time()), "self_id": self_id, "post_type": "message",
              "message_id": message_id, "user_id": user_id,
              "message_seq": message_id, "real_id": message_id, "sender": sender,
              "raw_message": text, "message": tuple(segments)}
    if private:
        return PrivateMessageEvent(**common)
    return GroupMessageEvent(**common, group_id=group_id)


def segment_to_dict(seg) -> dict:
    """napcat 的消息段（dataclass）转成 OneBot 的 {"type":..., "data":{...}}。"""
    if isinstance(seg, dict):
        return seg
    name = type(seg).__name__.lower()
    data = {}
    try:
        for field in dataclasses.fields(seg):
            value = getattr(seg, field.name, None)
            if value is not None:
                data[field.name] = value
    except TypeError:
        return {"type": "text", "data": {"text": str(seg)}}
    return {"type": name, "data": data}


def segments_to_text(segments) -> str:
    parts = []
    for seg in segments or []:
        item = segment_to_dict(seg)
        if item.get("type") == "text":
            parts.append(str((item.get("data") or {}).get("text") or ""))
    return "".join(parts)


def _text_event(user_id, text, group_id=None, message_id="", nickname="",
                self_id="", raw=None, at_bot=False, extra_segments=None):
    return make_event(private=group_id is None, user_id=user_id, text=text,
                      group_id=group_id, message_id=message_id or 0,
                      nickname=nickname, self_id=self_id, raw=raw, at_bot=at_bot,
                      extra_segments=extra_segments)


def _pkcs7_unpad(data: bytes, block_size: int = 16) -> bytes:
    if not data:
        return data
    pad_len = data[-1]
    if pad_len <= 0 or pad_len > block_size:
        return data
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        return data
    return data[:-pad_len]


def _media_key(key_hex: str = "", key_b64: str = "") -> bytes:
    text = str(key_hex or "").strip()
    if text:
        try:
            return bytes.fromhex(text)
        except ValueError:
            pass
    b64_text = str(key_b64 or "").strip()
    if not b64_text:
        return b""
    try:
        decoded = base64.b64decode(b64_text + "=" * (-len(b64_text) % 4))
    except Exception:
        return b""
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        try:
            return bytes.fromhex(decoded.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            return b""
    return b""


def _decrypt_media(raw: bytes, key_hex: str = "", key_b64: str = "") -> bytes:
    key = _media_key(key_hex, key_b64)
    if not key:
        return raw
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        print("微信 ClawBot：缺少 cryptography，收到的图片没法解密（装上依赖后重启即可）")
        return raw
    try:
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        return _pkcs7_unpad(decryptor.update(raw) + decryptor.finalize())
    except Exception as e:
        print(f"微信 ClawBot：图片解密失败，按原样处理: {type(e).__name__}: {e}")
        return raw


def _image_suffix(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:2] == b"BM":
        return ".bmp"
    return ".jpg"


def _audio_suffix(data: bytes) -> str:
    if data[:9] == b"#!SILK_V3" or data[1:10] == b"#!SILK_V3":
        return ".silk"
    if data[:5] == b"#!AMR":
        return ".amr"
    if data[:4] == b"RIFF":
        return ".wav"
    if data[:3] == b"\xff\xd8\xff" or data[:3] == b"ID3" or data[:2] == b"\xff\xfb":
        return ".mp3"
    if data[:4] == b"OggS":
        return ".ogg"
    if data[:4] == b"fLaC":
        return ".flac"
    if data[4:8] == b"ftyp":
        return ".m4a"
    return ".silk"


def _rand_uint32_b64() -> str:
    return base64.b64encode(str(random.getrandbits(32)).encode("utf-8")).decode()


# ---------------------------------------------------------------------------
# 微信 ClawBot（iLink）
# ---------------------------------------------------------------------------

def ilink_headers(token: str = "", client_version: str = "") -> dict:
    head = {"Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": _rand_uint32_b64()}
    if client_version:
        head["iLink-App-ClientVersion"] = str(client_version)
    if token:
        head["Authorization"] = f"Bearer {token}"
    return head


async def ilink_login_qr(base: str = ILINK_BASE, bot_type: str = ILINK_BOT_TYPE,
                         timeout: float = 20) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base}/ilink/bot/get_bot_qrcode",
                               params={"bot_type": str(bot_type)},
                               headers=ilink_headers(),
                               timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            text = await resp.text()
    return _as_json(text)


async def ilink_login_status(qrcode: str, base: str = ILINK_BASE,
                             timeout: float = 40) -> dict:
    """轮询扫码状态：status 为 confirmed 时带 bot_token / ilink_bot_id / baseurl。"""
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base}/ilink/bot/get_qrcode_status",
                               params={"qrcode": str(qrcode)},
                               headers=ilink_headers(client_version=ILINK_CLIENT_VERSION),
                               timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            text = await resp.text()
    return _as_json(text)


class ILinkSessionExpired(RuntimeError):
    pass


class ILinkStaleContext(RuntimeError):
    pass


def _as_json(text: str) -> dict:
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {"raw": text}
    return data if isinstance(data, dict) else {"data": data}


def _first(data: dict, *keys):
    for key in keys:
        value = (data or {}).get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _mask(value, keep: int = 6) -> str:
    text = str(value or "")
    if not text:
        return "(空)"
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}…{text[-keep:]}"


def _preview(text, limit: int = 40) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _describe_fields(data: dict) -> str:
    if not isinstance(data, dict):
        return str(data)
    out = []
    for key in sorted(data):
        value = data[key]
        if key in ("from_user_id", "to_user_id", "from_id", "to_id", "user_id"):
            out.append(f"{key}={_mask(value)}")
        elif key in ("context_token", "ctx_token", "bot_token", "client_id"):
            out.append(f"{key}=<{len(str(value or ''))} 字符>")
        elif key in ("item_list", "msgs", "messages", "updates", "msg_list"):
            out.append(f"{key}=<{len(value) if hasattr(value, '__len__') else '?'} 项>")
        elif isinstance(value, (int, float, bool)) or value is None:
            out.append(f"{key}={value}")
        elif isinstance(value, str) and len(value) <= 24:
            out.append(f"{key}={value!r}")
        else:
            out.append(f"{key}=<{type(value).__name__}>")
    return " ".join(out) or "(空对象)"


class ILinkClient:

    platform = "wechat_clawbot"
    capabilities = {"voice": False, "sticker": False, "image": False, "poke": False,
                    "recall": False, "mention": False, "quote": False,
                    "recall_other": False, "mute": False}

    def __init__(self, connection: dict, base: str = ILINK_BASE):
        self.connection = connection or {}
        self.base = str(self.connection.get("base_url") or base).rstrip("/")
        self.token = str(self.connection.get("bot_token") or "")
        self.bot_id = str(self.connection.get("account_id")
                          or self.connection.get("bot_id") or "")
        self.self_id = self.bot_id or "wechat_bot"
        self.cursor = str(self.connection.get("cursor") or "")
        self._session = None
        self._typing_tickets = {}
        self._typing_warned = False
        self._typing_tasks = set()
        self._media_warned = False
        self.cdn_base = str(self.connection.get("cdn_base_url")
                            or ILINK_CDN_BASE).rstrip("/")
        saved = self.connection.get("context_tokens")
        self._contexts = dict(saved) if isinstance(saved, dict) else {}
        self._pending = {}

    # ---- 生命周期 ----
    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc):
        if self._session is not None:
            await self._session.close()
        self._session = None

    async def close(self):
        await self.__aexit__()

    async def _post(self, path: str, payload: dict, timeout: float = 45,
                    log_result: bool = False) -> dict:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        async with self._session.post(f"{self.base}/{path.lstrip('/')}", json=payload,
                                      headers=ilink_headers(self.token),
                                      timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            text = await resp.text()
        data = _as_json(text)
        ret = int(data.get("ret") or 0)
        errcode = int(data.get("errcode") or 0)
        if log_result:
            print(f"[微信 ClawBot] {path} 响应：ret={ret} errcode={errcode}")
        if errcode == ILINK_SESSION_TIMEOUT:
            raise ILinkSessionExpired("微信 ClawBot 登录态已失效，请重新扫码登录")
        if ret == ILINK_STALE_CONTEXT:
            raise ILinkStaleContext(f"iLink {path} 拒绝（ret=-2）："
                                    f"{data.get('err_msg') or 'context_token 可能已过期'}")
        if resp.status >= 400 or ret or errcode:
            raise RuntimeError(f"iLink {path} 失败: {data.get('err_msg') or data}")
        return data

    def snapshot(self) -> dict:
        return {"cursor": self.cursor, "context_tokens": dict(self._contexts)}

    # ---- 收消息 ----
    async def __aiter__(self):
        while True:
            try:
                data = await self._post(
                    "ilink/bot/getupdates",
                    {"base_info": {"channel_version": ILINK_CHANNEL_VERSION},
                     "get_updates_buf": self.cursor},
                    timeout=90)
            except ILinkSessionExpired:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError):
                await asyncio.sleep(1)
                continue
            new_cursor = _first(data, "get_updates_buf", "cursor", "next_buf")
            if new_cursor:
                self.cursor = str(new_cursor)
            for msg in (_first(data, "msgs", "messages", "updates", "msg_list") or []):
                event = self.to_event(msg, await self._inbound_media(msg))
                if event is not None:
                    await self._flush_pending(event.user_id)
                    yield event

    async def _inbound_media(self, msg: dict) -> list:
        segments = []
        for item in (msg.get("item_list") or []):
            if not isinstance(item, dict):
                continue
            item_type = int(item.get("type") or 0)
            if item_type not in (ILINK_ITEM_IMAGE, ILINK_ITEM_VOICE):
                if item_type in (ILINK_ITEM_FILE, ILINK_ITEM_VIDEO) \
                        and not self._media_warned:
                    self._media_warned = True
                    print(f"微信 ClawBot：收到类型 {item_type} 的消息"
                          "（微信这条通道目前只认得文字、图片与语音），已忽略该段。")
                continue
            try:
                segment = await self._image_segment(item) \
                    if item_type == ILINK_ITEM_IMAGE else await self._voice_segment(item)
            except Exception as e:
                if not self._media_warned:
                    self._media_warned = True
                    print(f"微信 ClawBot：媒体下载/解密失败，这段只当纯文本处理: "
                          f"{type(e).__name__}: {e}")
                segment = None
            if segment is not None:
                segments.append(segment)
        return segments

    async def _voice_segment(self, item: dict):
        voice_item = item.get("voice_item") or {}
        media = voice_item.get("media") or {}
        param = str(media.get("encrypt_query_param") or "").strip()
        if not param:
            return None
        raw = await self._download_media(param)
        data = _decrypt_media(raw, str(voice_item.get("aeskey") or ""),
                              str(media.get("aes_key") or ""))
        WECHAT_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        path = WECHAT_MEDIA_DIR / (f"wechat_voice_{int(time.time() * 1000)}_"
                                   f"{os.urandom(3).hex()}{_audio_suffix(data)}")
        path.write_bytes(data)
        return Record(file=str(path))

    async def _image_segment(self, item: dict):
        image_item = item.get("image_item") or {}
        media = image_item.get("media") or {}
        param = str(media.get("encrypt_query_param") or "").strip()
        if not param:
            return None
        raw = await self._download_media(param)
        data = _decrypt_media(raw, str(image_item.get("aeskey") or ""),
                              str(media.get("aes_key") or ""))
        WECHAT_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        path = WECHAT_MEDIA_DIR / (f"wechat_{int(time.time() * 1000)}_"
                                    f"{os.urandom(3).hex()}{_image_suffix(data)}")
        path.write_bytes(data)
        return Image(file=str(path))

    async def _download_media(self, encrypted_query_param: str) -> bytes:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        url = (f"{self.cdn_base}/download?encrypted_query_param="
               f"{quote(encrypted_query_param)}")
        async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"iLink 媒体下载失败: {resp.status}")
            return await resp.read()

    def to_event(self, msg: dict, media_segments=None):
        if not isinstance(msg, dict):
            return None
        from_id = str(_first(msg, "from_user_id", "from_id", "user_id") or "")
        if not from_id:
            return None
        context = str(_first(msg, "context_token", "ctx_token") or "")
        if context:
            self._contexts.pop(from_id, None)
            self._contexts[from_id] = context
        text = ""
        for item in (msg.get("item_list") or []):
            if not isinstance(item, dict):
                continue
            if int(item.get("type") or 0) == ILINK_ITEM_TEXT:
                text += str((item.get("text_item") or {}).get("text") or "")
        return _text_event(from_id, text,
                           message_id=str(_first(msg, "msg_id", "message_id", "id") or ""),
                           nickname=str(_first(msg, "from_user_nickname", "nickname") or ""),
                           self_id=self.bot_id, raw=msg,
                           extra_segments=media_segments or [])

    # ---- 发消息 ----
    async def _typing_ticket(self, user_id: str, context_token: str) -> str:
        cached = self._typing_tickets.get(str(user_id))
        now = time.time()
        if cached and now - cached[1] < ILINK_TYPING_TICKET_TTL:
            return cached[0]
        data = await self._post("ilink/bot/getconfig", {
            "ilink_user_id": str(user_id),
            "context_token": context_token,
            "base_info": {"channel_version": ILINK_CHANNEL_VERSION}}, timeout=20)
        ticket = str(data.get("typing_ticket") or "")
        if ticket:
            self._typing_tickets[str(user_id)] = (ticket, now)
        return ticket

    async def _typing(self, user_id: str, context_token: str, status: int) -> None:
        if not context_token:
            return
        try:
            ticket = await self._typing_ticket(user_id, context_token)
            if not ticket:
                return
            await self._post("ilink/bot/sendtyping", {
                "ilink_user_id": str(user_id), "typing_ticket": ticket,
                "status": int(status),
                "base_info": {"channel_version": ILINK_CHANNEL_VERSION}}, timeout=20)
        except Exception as e:
            if not self._typing_warned:
                self._typing_warned = True
                print(f"微信 ClawBot：「正在输入」状态没发出去（不影响发消息）: "
                      f"{type(e).__name__}: {e}")

    def begin_typing(self, user_id) -> None:
        self._typing_task(user_id, ILINK_TYPING_START)

    def end_typing(self, user_id) -> None:
        """把「正在输入」置为结束。"""
        self._typing_task(user_id, ILINK_TYPING_CANCEL)

    def _typing_task(self, user_id, status: int) -> None:
        target = str(user_id)
        context = self._contexts.get(target, "")
        if not context:
            return
        try:
            task = asyncio.get_running_loop().create_task(
                self._typing(target, context, status))
        except RuntimeError:
            return
        self._typing_tasks.add(task)
        task.add_done_callback(self._typing_tasks.discard)

    def _hold_pending(self, user_id: str, text: str) -> None:
        target = str(user_id)
        box = self._pending.setdefault(target, [])
        box.append(str(text))
        dropped = 0
        while len(box) > ILINK_PENDING_LIMIT:
            box.pop(0)
            dropped += 1
        print(f"微信 ClawBot：{_preview(text)} 没投递到微信，先留着等对方下一条消息（刷新 context_token）后补发，待补发 {len(box)} 条"
              + (f"（更早的 {dropped} 条已丢弃）" if dropped else ""))

    async def _flush_pending(self, user_id: str) -> None:
        target = str(user_id)
        box = self._pending.get(target)
        context = self._contexts.get(target, "")
        if not box or not context:
            return
        while box:
            try:
                data = await self._send_text(target, box[0], context)
            except ILinkStaleContext:
                return              # token 还是不行：留着下回再试
            except Exception as e:
                print(f"微信 ClawBot：补发失败（{type(e).__name__}: {e}），留到下次再试。")
                return
            if not _first(data, *ILINK_ACK_ID_KEYS):
                return
            box.pop(0)
        self._pending.pop(target, None)

    async def send_private_msg(self, user_id, message):
        text = segments_to_text(message).strip()
        if not text:
            return
        target = str(user_id)
        await self._flush_pending(target)
        context = self._contexts.get(target, "")
        try:
            data = await self._send_text(target, text, context)
        except ILinkStaleContext as e:
            print(f"微信 ClawBot：{target} 的 context_token 已被拒绝，改用不带 token 的方式重试一次（{e}）")
            data = await self._send_text(target, text, "")
        if not _first(data, *ILINK_ACK_ID_KEYS):
            self._hold_pending(target, text)
        return data

    async def _send_text(self, user_id, text: str, context: str):
        msg = {"from_user_id": "", "to_user_id": str(user_id),
               "client_id": os.urandom(16).hex(),
               "message_type": 2, "message_state": 2,
               "item_list": [{"type": 1, "text_item": {"text": text}}]}
        if context:
            msg["context_token"] = context
        data = await self._post("ilink/bot/sendmessage",
                                {"base_info": {"channel_version": ILINK_CHANNEL_VERSION},
                                 "msg": msg})
        message_id = _first(data, *ILINK_ACK_ID_KEYS)
        if message_id:
            print(f"[微信 ClawBot] 已发送（message_id={message_id}）：{_preview(text)}")
        else:
            print("微信 ClawBot：服务端收下了却没返回 message_id，这条多半没投递到微信；去接入方式里重新扫码登录（重建绑定）。")
            print(f"微信 ClawBot：发送载荷字段：{_describe_fields(msg)}")
            print(f"微信 ClawBot：发送响应字段：{_describe_fields(data)}")
        return data

    async def send_group_msg(self, group_id, message):
        raise NotImplementedError("微信 ClawBot 暂不支持群聊发送")


# ---------------------------------------------------------------------------
# QQ 官方机器人（WebSocket 网关）
# ---------------------------------------------------------------------------

QQ_MSG_TYPE_TEXT = 0
QQ_MSG_TYPE_MEDIA = 7
QQ_FILE_TYPE_IMAGE = 1
QQ_FILE_TYPE_VOICE = 3
QQ_REPLY_MESSAGE_LIMIT = 5
QQ_PASSIVE_WINDOW_GROUP = 300.0
QQ_PASSIVE_WINDOW_C2C = 3600.0
QQ_MEDIA_CONTENT = " "
QQ_VOICE_SAMPLE_RATE = 16000
QQ_VOICE_SILK_BITRATE = 24000
QQ_MEDIA_DIR = Path(tempfile.gettempdir()) / "lovomo_qq_media"


def client_reply_limit(client) -> int:
    limit = getattr(type(client), "reply_message_limit", 0)
    try:
        return max(0, int(limit or 0))
    except (TypeError, ValueError):
        return 0


def _qq_msg_idx(data: dict) -> str:
    scene = data.get("message_scene") or {}
    ext = scene.get("ext") or []
    if isinstance(ext, str):
        ext = [ext]
    for item in ext:
        text = str(item or "")
        if text.startswith("msg_idx="):
            return text.split("=", 1)[1].strip()
    return ""


def _to_16k_mono_wav(path: str) -> str:
    import wave

    try:
        with wave.open(str(path), "rb") as wf:
            if wf.getnchannels() == 1 and wf.getsampwidth() == 2 \
                    and wf.getframerate() == QQ_VOICE_SAMPLE_RATE:
                return str(path)
    except Exception:
        pass
    from modules.media_cache import _find_ffmpeg
    from modules.tts_service import no_window_kwargs
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("找不到 ffmpeg，语音没法转成 silk")
    target = str(Path(path).with_name(f"qq_voice_{time.time_ns()}.wav"))
    proc = subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(path),
         "-ar", str(QQ_VOICE_SAMPLE_RATE), "-ac", "1", target],
        capture_output=True, text=True, timeout=120, **no_window_kwargs())
    if proc.returncode != 0 or not os.path.isfile(target):
        raise RuntimeError(f"语音转码失败: {str(proc.stderr or '')[:200]}")
    return target


def _voice_silk_bytes(path: str) -> bytes:
    import io
    import wave

    import pysilk

    source = _to_16k_mono_wav(path)
    with wave.open(source, "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2 \
                or wf.getframerate() != QQ_VOICE_SAMPLE_RATE:
            raise RuntimeError("语音不是 16k 单声道 16 位 PCM，转不了 silk")
        frames = wf.readframes(wf.getnframes())
    if source != str(path):
        try:
            Path(source).unlink(missing_ok=True)
        except OSError:
            pass
    out = io.BytesIO()
    pysilk.encode(io.BytesIO(frames), out, QQ_VOICE_SAMPLE_RATE, QQ_VOICE_SILK_BITRATE)
    return out.getvalue()


class QQBotClient:

    platform = "qq_official"
    capabilities = {"voice": True, "sticker": True, "image": True, "poke": False,
                    "recall": True, "mention": False, "quote": True,
                    "quote_private": False, "recall_other": False, "mute": False}
    reply_message_limit = QQ_REPLY_MESSAGE_LIMIT

    def __init__(self, connection: dict, api_base: str = ""):
        self.connection = connection or {}
        self.app_id = str(self.connection.get("app_id") or "")
        self.app_secret = str(self.connection.get("app_secret") or "")
        self.sandbox = bool(self.connection.get("sandbox"))
        self.api_base = api_base or (QQ_SANDBOX_API_BASE if self.sandbox else QQ_API_BASE)
        self.self_id = self.app_id
        self._session = None
        self._ws = None
        self._access = ""
        self._access_expire = 0.0
        self._seq = None
        self._heartbeat = None
        self._heartbeat_interval = QQ_HEARTBEAT_FALLBACK
        self._hello = asyncio.Event()
        self._last_ack = 0.0
        self._pending = asyncio.Queue()
        self._passive = {}
        self._msg_refs = {}
        self._sent_route = {}

    # ---- 生命周期 ----
    def _http(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def __aenter__(self):
        await self._ensure_token()
        gateway = await self._get_gateway()
        self._ws = await self._http().ws_connect(
            gateway, heartbeat=None,
            timeout=aiohttp.ClientWSTimeout(ws_receive=None, ws_close=15))
        self._hello.clear()
        await self._identify()
        self._heartbeat = asyncio.create_task(self._heartbeat_loop())
        return self

    async def _stop_heartbeat(self):
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            self._heartbeat = None

    async def _abort_ws(self):
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def __aexit__(self, *exc):
        await self._stop_heartbeat()
        await self._abort_ws()
        if self._session is not None:
            await self._session.close()
        self._session = None

    async def close(self):
        await self.__aexit__()

    async def _ensure_token(self):
        if self._access and time.time() < self._access_expire - 60:
            return
        async with self._http().post(QQ_TOKEN_URL, json={
                "appId": self.app_id, "clientSecret": self.app_secret},
                timeout=aiohttp.ClientTimeout(total=20)) as resp:
            data = _as_json(await resp.text())
        token = _first(data, "access_token", "accessToken")
        if not token:
            raise RuntimeError(f"取 QQ Access Token 失败：{data}")
        self._access = str(token)
        self._access_expire = time.time() + float(_first(data, "expires_in") or 7200)

    def _auth_headers(self) -> dict:
        return {"Authorization": f"QQBot {self._access}",
                "Content-Type": "application/json"}

    async def _get_gateway(self) -> str:
        async with self._http().get(f"{self.api_base}/gateway",
                                    headers=self._auth_headers(),
                                    timeout=aiohttp.ClientTimeout(total=20)) as resp:
            data = _as_json(await resp.text())
        url = _first(data, "url", "gateway")
        if not url:
            raise RuntimeError(f"取 QQ 网关地址失败：{data}")
        return str(url)

    async def _send_op(self, op: int, d):
        # 悄悄空转下去（见 _heartbeat_loop）。
        if self._ws is None:
            raise RuntimeError("QQ 网关未连接")
        await self._ws.send_json({"op": op, "d": d})

    async def _identify(self):
        await self._send_op(2, {
            "token": f"QQBot {self._access}",
            "intents": QQ_INTENTS_GROUP_C2C,
            "shard": [0, 1],
            "properties": {"$os": "windows", "$browser": "lovomo", "$device": "lovomo"}})

    async def _heartbeat_loop(self):
        try:
            try:
                await asyncio.wait_for(self._hello.wait(), QQ_HELLO_TIMEOUT)
            except asyncio.TimeoutError:
                pass
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                if self._ws is None:
                    return
                await self._ensure_token_quietly()
                try:
                    await self._send_op(1, self._seq)
                except Exception as e:
                    print(f"QQ 官方：心跳发送失败，按断开处理。{type(e).__name__}: {e}")
                    await self._abort_ws()
                    return
        except asyncio.CancelledError:
            raise

    async def _ensure_token_quietly(self):
        try:
            await self._ensure_token()
        except Exception as e:
            print(f"QQ 官方：刷新 Access Token 失败（沿用旧 token）：{type(e).__name__}: {e}")

    # ---- 收消息 ----
    async def __aiter__(self):
        if self._ws is None:
            return
        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except ValueError:
                    continue
                if isinstance(payload.get("s"), int):
                    self._seq = payload["s"]
                op = payload.get("op")
                if op == 10:
                    hello = payload.get("d") or {}
                    try:
                        interval = float(hello.get("heartbeat_interval") or 0) / 1000.0
                    except (TypeError, ValueError):
                        interval = 0.0
                    if interval > 0:
                        self._heartbeat_interval = max(QQ_HEARTBEAT_MIN,
                                                       interval * 0.8)
                    self._hello.set()
                    continue
                if op == 11:
                    # 心跳 ACK
                    self._last_ack = time.time()
                    continue
                if op in (7, 9):
                    print(f"QQ 官方：网关要求重连（op={op}），按断开处理。")
                    if op == 9:
                        self._seq = None
                    return
                if op != 0:
                    continue
                kind = str(payload.get("t") or "")
                data = payload.get("d") or {}
                media = await self._inbound_media(data) \
                    if kind in ("C2C_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE",
                                "GROUP_MESSAGE_CREATE") else None
                event = self.to_event(kind, data, media)
                if event is not None:
                    yield event
            elif msg.type == aiohttp.WSMsgType.CLOSE:
                print(f"QQ 官方：网关关闭连接，code={msg.data}")
                return
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                return

    def to_event(self, event_type: str, data: dict, media_segments=None):
        kind = str(event_type or "")
        if kind == "C2C_MESSAGE_CREATE":
            user = str(_first(data, "user_openid", "openid") or
                       ((data.get("author") or {}).get("user_openid") or ""))
            if not user:
                return None
            message_id = str(data.get("id") or "")
            self._remember_inbound("users", user, message_id, data)
            return _text_event(user, str(data.get("content") or ""),
                               message_id=message_id,
                               self_id=self.app_id, raw=data,
                               extra_segments=media_segments)
        if kind in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
            group = str(_first(data, "group_openid") or "")
            author = data.get("author") or {}
            user = str(author.get("member_openid") or author.get("user_openid") or "")
            if not group or not user:
                return None
            message_id = str(data.get("id") or "")
            self._remember_inbound("groups", group, message_id, data)
            return _text_event(user, str(data.get("content") or ""), group_id=group,
                               message_id=message_id,
                               self_id=self.app_id, raw=data, at_bot=True,
                               extra_segments=media_segments)
        return None

    def _remember_inbound(self, scope: str, target: str, message_id: str,
                          data: dict) -> None:
        if not message_id:
            return
        window = QQ_PASSIVE_WINDOW_C2C if scope == "users" else QQ_PASSIVE_WINDOW_GROUP
        self._passive[(scope, target)] = {"msg_id": message_id, "seq": 0,
                                          "until": time.time() + window}
        idx = _qq_msg_idx(data)
        if idx:
            self._msg_refs[message_id] = idx

    # ---- 收媒体 ----
    async def _inbound_media(self, data: dict) -> list:
        segments = []
        for item in (data.get("attachments") or []):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            try:
                raw = await self._download(url)
            except Exception as e:
                print(f"QQ 官方：媒体下载失败，这段只当纯文本处理: "
                      f"{type(e).__name__}: {e}")
                continue
            if not raw:
                continue
            content_type = str(item.get("content_type") or "").lower()
            QQ_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
            stamp = f"{int(time.time() * 1000)}_{os.urandom(3).hex()}"
            if content_type.startswith("voice") or content_type.startswith("audio"):
                path = QQ_MEDIA_DIR / f"qq_voice_{stamp}{_audio_suffix(raw)}"
                path.write_bytes(raw)
                segments.append(Record(file=str(path)))
            elif content_type.startswith("image") or not content_type:
                path = QQ_MEDIA_DIR / f"qq_{stamp}{_image_suffix(raw)}"
                path.write_bytes(raw)
                segments.append(Image(file=str(path)))
        return segments

    async def _download(self, url: str) -> bytes:
        async with self._http().get(
                url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}")
            return await resp.read()

    # ---- 发消息 ----
    async def _api(self, method: str, path: str, body: dict = None) -> dict:
        await self._ensure_token()
        kwargs = {"headers": self._auth_headers(),
                  "timeout": aiohttp.ClientTimeout(total=60)}
        if body is not None:
            kwargs["json"] = body
        async with self._http().request(method, f"{self.api_base}{path}",
                                        **kwargs) as resp:
            status = resp.status
            text = await resp.text()
        data = _as_json(text)
        if status >= 400:
            raise RuntimeError(f"QQ 接口 {path} 失败 {status}: {text[:200]}")
        return data

    async def _upload_media(self, scope: str, target: str, path: str,
                            file_type: int) -> str:
        raw = Path(path).read_bytes()
        if not raw:
            raise RuntimeError("富媒体文件是空的")
        data = await self._api("POST", f"/v2/{scope}/{target}/files",
                               {"file_type": int(file_type),
                                "file_data": base64.b64encode(raw).decode("ascii"),
                                "srv_send_msg": False})
        file_info = str(_first(data, "file_info", "fileInfo") or "")
        if not file_info:
            raise RuntimeError(f"QQ 富媒体上传没返回 file_info: {str(data)[:200]}")
        return file_info

    def _passive_msg_id(self, scope: str, target: str, reply_id: str) -> str:
        key = (scope, target)
        if reply_id:
            current = self._passive.get(key)
            if not current or str(current.get("msg_id") or "") != reply_id:
                window = QQ_PASSIVE_WINDOW_C2C if scope == "users" \
                    else QQ_PASSIVE_WINDOW_GROUP
                self._passive[key] = {"msg_id": reply_id, "seq": 0,
                                      "until": time.time() + window}
            return reply_id
        current = self._passive.get(key)
        if not current:
            return ""
        if time.time() > float(current.get("until") or 0):
            self._passive.pop(key, None)
            return ""
        return str(current.get("msg_id") or "")

    async def _send_one(self, scope: str, target: str, payload: dict,
                        reply_id: str = "") -> dict:
        msg_id = self._passive_msg_id(scope, target, reply_id)
        if msg_id:
            slot = self._passive[(scope, target)]
            used = int(slot.get("seq") or 0)
            if used < QQ_REPLY_MESSAGE_LIMIT:
                slot["seq"] = used + 1
                payload["msg_id"] = msg_id
                payload["msg_seq"] = used + 1
                ref = str(self._msg_refs.get(msg_id) or "")
                if reply_id and ref:
                    payload["message_reference"] = {"message_id": ref}
            else:
                if not slot.get("overflow"):
                    slot["overflow"] = True
                    print(f"QQ 官方：一条被动回复最多 {QQ_REPLY_MESSAGE_LIMIT} 条，"
                          "名额已用完，剩下的改按主动消息发出。")
        data = await self._api("POST", f"/v2/{scope}/{target}/messages", payload)
        message_id = str(_first(data, "id", "message_id") or "")
        if message_id:
            self._sent_route[message_id] = (scope, target)
            ref_idx = str((data.get("ext_info") or {}).get("ref_idx") or "")
            if ref_idx:
                self._msg_refs[message_id] = ref_idx
        return data

    async def _send_segments(self, scope: str, target: str, message) -> dict:
        text_parts = []
        reply_id = ""
        images = []
        voices = []
        for seg in message or []:
            item = segment_to_dict(seg)
            kind = item.get("type")
            data = item.get("data") or {}
            if kind == "text":
                text_parts.append(str(data.get("text") or ""))
            elif kind == "reply":
                reply_id = str(data.get("id") or "")
            elif kind == "image":
                value = str(data.get("file") or "")
                if value:
                    images.append(value)
            elif kind == "record":
                value = str(data.get("file") or "")
                if value:
                    voices.append(value)
        text = "".join(text_parts).strip()
        if not text and not images and not voices:
            return {}
        result = {}
        if text:
            result = await self._send_one(
                scope, target,
                {"msg_type": QQ_MSG_TYPE_TEXT, "content": text}, reply_id)
            reply_id = ""
        for path in images:
            try:
                file_info = await self._upload_media(scope, target, path,
                                                     QQ_FILE_TYPE_IMAGE)
            except Exception as e:
                print(f"QQ 官方：图片发送失败（上传没成），已跳过: "
                      f"{type(e).__name__}: {e}")
                continue
            result = await self._send_one(scope, target, {
                "msg_type": QQ_MSG_TYPE_MEDIA, "content": QQ_MEDIA_CONTENT,
                "media": {"file_info": file_info}}, reply_id)
            reply_id = ""
        for path in voices:
            try:
                silk = await asyncio.to_thread(_voice_silk_bytes, path)
            except Exception as e:
                print(f"QQ 官方：语音转 silk 失败，这条语音跳过: "
                      f"{type(e).__name__}: {e}")
                continue
            temp = Path(tempfile.gettempdir()) / f"lovomo_qq_voice_{time.time_ns()}.silk"
            try:
                temp.write_bytes(silk)
                file_info = await self._upload_media(scope, target, str(temp),
                                                     QQ_FILE_TYPE_VOICE)
            except Exception as e:
                print(f"QQ 官方：语音发送失败（上传没成），已跳过: "
                      f"{type(e).__name__}: {e}")
                continue
            finally:
                temp.unlink(missing_ok=True)
            result = await self._send_one(scope, target, {
                "msg_type": QQ_MSG_TYPE_MEDIA,
                "content": QQ_MEDIA_CONTENT,
                "media": {"file_info": file_info}}, reply_id)
            reply_id = ""
        return result

    async def send_private_msg(self, user_id, message):
        return await self._send_segments("users", str(user_id), message)

    async def send_group_msg(self, group_id, message):
        return await self._send_segments("groups", str(group_id), message)

    async def delete_msg(self, message_id, **kwargs):
        mid = str(message_id or "").strip()
        route = self._sent_route.get(mid)
        if not route:
            raise RuntimeError("这条消息不是本次连接发出去的，撤不了")
        scope, target = route
        await self._api("DELETE", f"/v2/{scope}/{target}/messages/{mid}")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

QQ_BIND_BASE = "https://q.qq.com"
QQ_BIND_CREATE_PATH = "/lite/create_bind_task"
QQ_BIND_POLL_PATH = "/lite/poll_bind_result"
QQ_BIND_QR_URL = QQ_BIND_BASE + "/qqbot/openclaw/connect.html?task_id={task_id}&_wv=2"
QQ_BIND_STATUS_COMPLETED = 2
QQ_BIND_STATUS_EXPIRED = 3


def new_qq_bind_key() -> str:
    return base64.b64encode(os.urandom(32)).decode("ascii")


def decrypt_qq_bind_secret(encrypted: str, bind_key: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        key = base64.b64decode(bind_key)
        raw = base64.b64decode(encrypted)
    except Exception as e:
        raise ValueError("QQ 机器人凭证解码失败") from e
    if len(key) != 32 or len(raw) <= 28:
        raise ValueError("QQ 机器人凭证密文格式异常")
    try:
        return AESGCM(key).decrypt(raw[:12], raw[12:], None).decode("utf-8")
    except Exception as e:
        raise ValueError("QQ 机器人凭证解密失败") from e


async def qq_bind_create(timeout: float = 20) -> dict:
    bind_key = new_qq_bind_key()
    data = await _qq_bind_post(QQ_BIND_CREATE_PATH, {"key": bind_key}, timeout)
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    task_id = str(payload.get("task_id") or "").strip()
    if not task_id:
        raise RuntimeError(f"QQ 机器人绑定任务响应异常：{data}")
    return {"task_id": task_id, "bind_key": bind_key,
            "qr_content": QQ_BIND_QR_URL.format(task_id=quote(task_id, safe=""))}


async def qq_bind_poll(task_id: str, bind_key: str, timeout: float = 20) -> dict:
    data = await _qq_bind_post(QQ_BIND_POLL_PATH, {"task_id": str(task_id)}, timeout)
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    try:
        status = int(payload.get("status"))
    except (TypeError, ValueError):
        status = 0
    if status == QQ_BIND_STATUS_EXPIRED:
        return {"status": "expired"}
    if status != QQ_BIND_STATUS_COMPLETED:
        return {"status": "pending"}
    app_id = str(payload.get("bot_appid") or "").strip()
    encrypted = str(payload.get("bot_encrypt_secret") or "").strip()
    if not app_id or not encrypted:
        raise RuntimeError("扫码已通过，但没拿到完整的机器人凭证")
    return {"status": "completed", "app_id": app_id,
            "app_secret": decrypt_qq_bind_secret(encrypted, bind_key)}


async def _qq_bind_post(path: str, payload: dict, timeout: float) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{QQ_BIND_BASE}{path}", json=payload,
                                headers={"Accept": "application/json"},
                                timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            text = await resp.text()
    return _qq_bind_result(text)


def _qq_bind_result(text: str) -> dict:
    data = _as_json(text)
    retcode = data.get("retcode")
    if retcode not in (None, 0, "0"):
        raise RuntimeError(str(data.get("msg") or data.get("message")
                               or f"QQ 机器人绑定接口返回失败（retcode={retcode}）"))
    return data


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

def build_client(platform: str, connection: dict):
    if platform == "wechat_clawbot":
        return ILinkClient(connection)
    if platform == "qq_official":
        return QQBotClient(connection)
    return None
