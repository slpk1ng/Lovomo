"""接入方式适配器：把各平台的消息通道包装成 OneBot 形态，复用同一条消息处理管线。

- NapCat 本身就是 OneBot（直接用 napcat 客户端，不走这里）；
- 微信 ClawBot 走 iLink HTTP 协议：扫码登录 + 长轮询收消息 + sendmessage 回复；
- QQ 官方机器人走 WebSocket 网关：AppID/AppSecret 换 Access Token，
  Identify 鉴权、心跳、收 C2C/群 @ 消息，回复走 REST。

两个适配器都实现同一组方法（send_private_msg / send_group_msg / 异步迭代事件），
事件字段也照 OneBot 摆（user_id / group_id / sender / message / message_id），
这样 handle_message_event 与 MessageSender 一行都不用改。
"""
import asyncio
import base64
import dataclasses
import json
import os
import random
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp

from napcat import (At, GroupMessageEvent, Image, MessageSender, PrivateMessageEvent,
                    Text)

ILINK_BASE = "https://ilinkai.weixin.qq.com"
# 收到的图片等媒体放在这个 CDN 上（官方客户端的默认值）
ILINK_CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
# item_list 里的消息类型：1 文本 / 2 图片 / 3 语音 / 4 文件 / 5 视频
ILINK_ITEM_TEXT = 1
ILINK_ITEM_IMAGE = 2
ILINK_ITEM_VOICE = 3
ILINK_ITEM_FILE = 4
ILINK_ITEM_VIDEO = 5
# 收到的图片落盘的位置：识图那条管线要的是本地文件路径
WECHAT_MEDIA_DIR = Path(tempfile.gettempdir()) / "lovomo_wechat_media"

# 个人微信 ClawBot 的 bot_type（官方客户端固定传 3）
ILINK_BOT_TYPE = "3"
ILINK_CHANNEL_VERSION = "lovomo"
# 轮询扫码状态时带的客户端版本号（官方客户端只在那个请求上带）
ILINK_CLIENT_VERSION = "1"
# 「正在输入」票据的有效期与刷新间隔（官方给的是 60 秒）
ILINK_TYPING_TICKET_TTL = 60
ILINK_TYPING_START = 1
ILINK_TYPING_CANCEL = 2
# 发送成功时服务端回的投递凭证（字段名各版本略有出入，按候选取）
ILINK_ACK_ID_KEYS = ("message_id", "msg_id", "server_id", "id")
# iLink 的登录态失效错误码：重试没用，得重新扫码
ILINK_SESSION_TIMEOUT = -14
# context_token 失效（和限流共用这个码）：去掉 token 重发一次还有机会
ILINK_STALE_CONTEXT = -2
QQ_TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
QQ_API_BASE = "https://api.sgroup.qq.com"
QQ_SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"
# 群与单聊消息（C2C_MESSAGE_CREATE / GROUP_AT_MESSAGE_CREATE）
QQ_INTENTS_GROUP_C2C = 1 << 25


# ---------------------------------------------------------------------------
# 事件与消息段：让适配器产出与 NapCat 一样的形状
# ---------------------------------------------------------------------------

def client_supports(client, feature: str) -> bool:
    """这条接入方式支不支持某个能力（没声明的默认支持）。

    必须查**类**属性：NapCat 客户端的 __getattr__ 会给任意属性返回函数，
    hasattr/getattr 探不出真假。微信 ClawBot、QQ 官方都只实现了文本。
    """
    caps = getattr(type(client), "capabilities", None)
    if not isinstance(caps, dict):
        return True
    return bool(caps.get(feature, True))


def recent_ilink_target(connection: dict) -> str:
    """这条微信接入方式最近跟谁说过话（context_tokens 末尾那个）。

    投递自检拿它当收件人：只在真的收发过消息之后才有目标，不会凭空给谁发东西。
    """
    tokens = (connection or {}).get("context_tokens")
    if isinstance(tokens, dict) and tokens:
        return str(list(tokens)[-1])
    return ""


def friendly_user_label(platform: str, user_id: str, nickname: str = "") -> str:
    """给人看的用户称呼。

    微信/QQ 官方的用户 ID 是一长串 openid（o9cq...@im.wechat），
    直接拿去当会话名会变成「丛雨和o9cq803cY8qdjYVpVczwZ0gfBNwU@im.wechat的聊天」。
    """
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
    """按 NapCat 的事件类造一个事件。

    消息管线是按 isinstance 分派的：不是 PrivateMessageEvent / GroupMessageEvent
    的实例会在第一步就被丢掉（而且一声不响，日志里什么都没有）；
    消息段也必须是真正的 Text/Image 对象，自己拼字典过不了 isinstance 解析。
    """
    platform = "wechat_clawbot" if str(user_id).endswith("@im.wechat") else ""
    sender = MessageSender(user_id=user_id,
                           nickname=nickname or friendly_user_label(platform, str(user_id)))
    # 群聊要带上 @ 段：管线靠它判断"有没有 @ 机器人"，
    # 而 QQ 官方群的推送本来就只发生在被 @ 时
    segments = ([At(qq=str(self_id))] if at_bot else []) + [Text(text=text)]
    # 图片这类媒体段跟在文本后面：识图那条管线按 isinstance(seg, Image) 找图
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
    """把要发出去的消息段拼成一段纯文本（只取文本，其余类型暂时丢弃）。"""
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
    """去掉 AES 的 PKCS7 填充；填充不合法时原样返回（别把图片截坏）。"""
    if not data:
        return data
    pad_len = data[-1]
    if pad_len <= 0 or pad_len > block_size:
        return data
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        return data
    return data[:-pad_len]


def _media_key(key_hex: str = "", key_b64: str = "") -> bytes:
    """iLink 图片的 AES 密钥：16 字节十六进制，或 base64（内含十六进制文本）。"""
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
    """解密 iLink 媒体（AES-ECB + PKCS7）；没给密钥就按明文处理。"""
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
    """按文件头猜后缀：只影响临时文件名，猜不出就按 jpg。"""
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


def _rand_uint32_b64() -> str:
    """iLink 要求每个请求带一个新的 X-WECHAT-UIN（防重放）。

    官方客户端发的是 base64(十进制字符串)——照它来。自己发明成 base64(原始 4 字节)
    时服务端一样收下，返回值也看不出差别，所以别拿它做实验。
    """
    return base64.b64encode(str(random.getrandbits(32)).encode("utf-8")).decode()


# ---------------------------------------------------------------------------
# 微信 ClawBot（iLink）
# ---------------------------------------------------------------------------

def ilink_headers(token: str = "", client_version: str = "") -> dict:
    head = {"Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": _rand_uint32_b64()}
    if client_version:
        # 官方客户端只在轮询扫码状态时带它，其余请求不带
        head["iLink-App-ClientVersion"] = str(client_version)
    if token:
        head["Authorization"] = f"Bearer {token}"
    return head


async def ilink_login_qr(base: str = ILINK_BASE, bot_type: str = ILINK_BOT_TYPE,
                         timeout: float = 20) -> dict:
    """取一张 ClawBot 登录二维码。

    响应里 qrcode 是轮询用的票据，qrcode_img_content 才是要编进二维码的内容
    （扫码打开的是微信侧的登录确认页，不是官网）。
    """
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
    """登录态失效（iLink errcode -14）：只能重新扫码，重试没有意义。"""


class ILinkStaleContext(RuntimeError):
    """context_token 已失效（iLink ret -2）：去掉 token 再发一次还有机会成功。

    这个错误码和限流共用，光看码分不出来；但去掉 token 重试对两种情况都无害。
    """


def _as_json(text: str) -> dict:
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {"raw": text}
    return data if isinstance(data, dict) else {"data": data}


def _first(data: dict, *keys):
    """从响应里按候选键名取第一个非空值（接口字段名各版本略有出入）。"""
    for key in keys:
        value = (data or {}).get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _mask(value, keep: int = 6) -> str:
    """日志里只留头尾，别把完整 openid / 令牌写进去。"""
    text = str(value or "")
    if not text:
        return "(空)"
    if len(text) <= keep * 2:
        return "*" * len(text)
    return f"{text[:keep]}…{text[-keep:]}"


def _preview(text, limit: int = 40) -> str:
    """日志里回执用的文本预览：压成一行、超长截断。"""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _describe_fields(data: dict) -> str:
    """只列字段名与非敏感的小值，用于对照收发两端的差异。

    排「服务端收下却不投递」时，唯一还没看过的就是两端消息对象的字段差异：
    直接打整包会把 openid 与聊天内容写进日志，这里逐字段挑安全的写法。
    """
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
    """微信 ClawBot 连接：长轮询收消息，回复时带上消息自带的 context_token。"""

    platform = "wechat_clawbot"
    # 微信 ClawBot 只能收发文本：语音/贴纸/戳一戳/撤回/@/引用一律不发
    capabilities = {"voice": False, "sticker": False, "image": False, "poke": False,
                    "recall": False, "mention": False, "quote": False}

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
        # 用户 ID -> context_token（回复必须原样带回，否则关联不到会话）
        saved = self.connection.get("context_tokens")
        self._contexts = dict(saved) if isinstance(saved, dict) else {}

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
        # iLink 用 ret / errcode 报错，HTTP 状态码不一定能反映
        ret = int(data.get("ret") or 0)
        errcode = int(data.get("errcode") or 0)
        if log_result:
            # 服务端「收下」和「投递到微信」是两回事：发送出问题时，
            # 这一行是唯一能看出服务端到底怎么答的线索
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
        """要落盘的登录态与进度：游标 + 各用户的 context_token。"""
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
                # 长轮询被服务端断开或等超时是常态（不是故障）：
                # 稍等一下接着轮询，否则每分钟刷一条"连接失败"（错误信息还是空的）
                await asyncio.sleep(1)
                continue
            new_cursor = _first(data, "get_updates_buf", "cursor", "next_buf")
            if new_cursor:
                self.cursor = str(new_cursor)
            for msg in (_first(data, "msgs", "messages", "updates", "msg_list") or []):
                event = self.to_event(msg, await self._inbound_media(msg))
                if event is not None:
                    yield event

    async def _inbound_media(self, msg: dict) -> list:
        """把收到的图片落成本地文件（iLink 的媒体是加密的，得先下载再解密）。

        解密或下载失败只丢这一段媒体：文本照常进管线，别把整条消息吞掉。
        """
        segments = []
        for item in (msg.get("item_list") or []):
            if not isinstance(item, dict):
                continue
            item_type = int(item.get("type") or 0)
            if item_type != ILINK_ITEM_IMAGE:
                if item_type in (ILINK_ITEM_VOICE, ILINK_ITEM_FILE, ILINK_ITEM_VIDEO) \
                        and not self._media_warned:
                    self._media_warned = True
                    print(f"微信 ClawBot：收到类型 {item_type} 的消息"
                          "（微信这条通道目前只认得文字与图片），已忽略该段。")
                continue
            try:
                segment = await self._image_segment(item)
            except Exception as e:
                if not self._media_warned:
                    self._media_warned = True
                    print(f"微信 ClawBot：图片下载/解密失败，这条只当纯文本处理: "
                          f"{type(e).__name__}: {e}")
                segment = None
            if segment is not None:
                segments.append(segment)
        return segments

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
        """iLink 消息 → OneBot 事件；认不出的消息返回 None。"""
        if not isinstance(msg, dict):
            return None
        from_id = str(_first(msg, "from_user_id", "from_id", "user_id") or "")
        if not from_id:
            return None
        context = str(_first(msg, "context_token", "ctx_token") or "")
        if context:
            # 先删再写：让字典末尾始终是"最近说过话的人"（投递自检按它选目标）
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
        """取「正在输入」票据；拿不到就当这条路不支持（不影响发消息）。"""
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
        """把「正在输入」置为开始/结束。

        官方客户端（AstrBot 的 weixin_oc 适配器）每轮回复都走这条路：
        getconfig 取票据 → 发消息期间 sendtyping=1、发完 sendtyping=2。
        这里失败只提示一次，绝不因此影响消息本身。
        """
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
        """把「正在输入」置为开始；只提示，不阻塞发送。

        按整轮回复开关一次，而不是每发一条消息开关一遍：逐句发送时反复开关
        既让状态闪烁，又把两次握手（各 0.2s 上下）压在发消息的关键路径上。
        """
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

    async def send_private_msg(self, user_id, message):
        text = segments_to_text(message)
        if not text.strip():
            return
        target = str(user_id)
        context = self._contexts.get(target, "")
        if context:
            try:
                return await self._send_text(target, text, context)
            except ILinkStaleContext as e:
                # 服务端不认这个 context_token 了：文档给的降级办法是去掉 token
                # 再发一次；缓存也一并丢掉，等对方下一条消息刷新
                print(f"微信 ClawBot：{target} 的 context_token 已被拒绝，"
                      f"改用不带 token 的方式重试一次（{e}）")
                self._contexts.pop(target, None)
        return await self._send_text(target, text, "")

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
        # 服务端投递成功时会回 message_id；只在"收下但没这个 id"时才刷排查信息，
        # 平时每句台词就一行回执，别把日志淹了
        message_id = _first(data, *ILINK_ACK_ID_KEYS)
        if message_id:
            print(f"[微信 ClawBot] 已发送（message_id={message_id}）：{_preview(text)}")
        else:
            print("微信 ClawBot：服务端收下了却没返回 message_id，这条多半没投递到微信；"
                  "去接入方式里重新扫码登录（重建绑定）。")
            print(f"微信 ClawBot：发送载荷字段：{_describe_fields(msg)}")
            print(f"微信 ClawBot：发送响应字段：{_describe_fields(data)}")
        return data

    async def send_group_msg(self, group_id, message):
        # iLink 的群聊要单独的会话标识，暂不支持
        raise NotImplementedError("微信 ClawBot 暂不支持群聊发送")


# ---------------------------------------------------------------------------
# QQ 官方机器人（WebSocket 网关）
# ---------------------------------------------------------------------------

class QQBotClient:
    """QQ 官方机器人连接：Access Token → 网关 → Identify/心跳 → 收消息。"""

    platform = "qq_official"
    # 目前只实现了文本收发（发送时只取文字段）：语音/贴纸/戳一戳/撤回/@/引用别发
    capabilities = {"voice": False, "sticker": False, "image": False, "poke": False,
                    "recall": False, "mention": False, "quote": False}

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
        self._pending = asyncio.Queue()

    # ---- 生命周期 ----
    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        await self._ensure_token()
        gateway = await self._get_gateway()
        self._ws = await self._session.ws_connect(gateway, heartbeat=None, timeout=30)
        await self._identify()
        self._heartbeat = asyncio.create_task(self._heartbeat_loop())
        return self

    async def __aexit__(self, *exc):
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            self._heartbeat = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self._session is not None:
            await self._session.close()
        self._session = None

    async def close(self):
        await self.__aexit__()

    async def _ensure_token(self):
        if self._access and time.time() < self._access_expire - 60:
            return
        async with self._session.post(QQ_TOKEN_URL, json={
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
        async with self._session.get(f"{self.api_base}/gateway",
                                     headers=self._auth_headers(),
                                     timeout=aiohttp.ClientTimeout(total=20)) as resp:
            data = _as_json(await resp.text())
        url = _first(data, "url", "gateway")
        if not url:
            raise RuntimeError(f"取 QQ 网关地址失败：{data}")
        return str(url)

    async def _send_op(self, op: int, d):
        if self._ws is None:
            return
        await self._ws.send_json({"op": op, "d": d})

    async def _identify(self):
        await self._send_op(2, {
            "token": f"QQBot {self._access}",
            "intents": QQ_INTENTS_GROUP_C2C,
            "shard": [0, 1],
            "properties": {"$os": "windows", "$browser": "lovomo", "$device": "lovomo"}})

    async def _heartbeat_loop(self):
        try:
            while True:
                await asyncio.sleep(30)
                await self._ensure_token()
                await self._send_op(1, self._seq)
        except asyncio.CancelledError:
            return
        except Exception:
            return

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
                if payload.get("op") != 0:
                    continue
                event = self.to_event(payload.get("t"), payload.get("d") or {})
                if event is not None:
                    yield event
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                return

    def to_event(self, event_type: str, data: dict):
        """QQ 事件 → OneBot 事件；不关心的类型返回 None。"""
        kind = str(event_type or "")
        if kind == "C2C_MESSAGE_CREATE":
            user = str(_first(data, "user_openid", "openid") or
                       ((data.get("author") or {}).get("user_openid") or ""))
            if not user:
                return None
            return _text_event(user, str(data.get("content") or ""),
                               message_id=str(data.get("id") or ""),
                               self_id=self.app_id, raw=data)
        if kind in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
            group = str(_first(data, "group_openid") or "")
            author = data.get("author") or {}
            user = str(author.get("member_openid") or author.get("user_openid") or "")
            if not group or not user:
                return None
            return _text_event(user, str(data.get("content") or ""), group_id=group,
                               message_id=str(data.get("id") or ""),
                               self_id=self.app_id, raw=data, at_bot=True)
        return None

    # ---- 发消息 ----
    async def _post_message(self, path: str, text: str, message_id: str = ""):
        await self._ensure_token()
        body = {"content": text, "msg_type": 0}
        if message_id:
            body["msg_id"] = message_id
        async with self._session.post(f"{self.api_base}{path}", json=body,
                                      headers=self._auth_headers(),
                                      timeout=aiohttp.ClientTimeout(total=20)) as resp:
            text_body = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"QQ 发送失败 {resp.status}: {text_body[:200]}")

    async def send_private_msg(self, user_id, message):
        text = segments_to_text(message)
        if not text.strip():
            return
        await self._post_message(f"/v2/users/{user_id}/messages", text)

    async def send_group_msg(self, group_id, message):
        text = segments_to_text(message)
        if not text.strip():
            return
        await self._post_message(f"/v2/groups/{group_id}/messages", text)


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

def build_client(platform: str, connection: dict):
    """按平台建连接对象（与 NapCatClient 用法一致：async with + async for）。"""
    if platform == "wechat_clawbot":
        return ILinkClient(connection)
    if platform == "qq_official":
        return QQBotClient(connection)
    return None
