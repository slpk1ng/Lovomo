import asyncio
import contextlib
import contextvars
import json
import random
import re
import time
from pathlib import Path
from typing import List, Optional

from .llm_helpers import (MENTION_PLACEHOLDER, RoleContext, segment_for_tts, lang_text_broken,
                          translate_to_lang, available_mimics, apply_literal_mention,
                          display_text_of, DELIVERY_CHARS, DELIVERY_MODES)
from .tts import synthesize_sentence, merge_wavs, get_audio_duration
from .tts_service import check_tts_service
from .stickers import resize_for_output
from .adapters import client_supports, client_reply_limit


_CURRENT_CLIENT = contextvars.ContextVar("lovomo_send_client", default=None)

_VOICE_LOCKS: dict = {}

TEXT_ONLY_SEND_MIN_GAP = 1.5
TEXT_ONLY_SEND_GAP_JITTER = 1.0

RECALL_MIN_DELAY_SECONDS = 1.0
RECALL_MAX_DELAY_SECONDS = 60.0
DEFAULT_RECALL_DELAY_SECONDS = 3.0
RECALL_DENY = "deny"
GROUP_ADMIN_ROLES = ("owner", "admin")

DEFAULT_MUTE_SECONDS = 600
MUTE_MIN_SECONDS = 60
MUTE_MAX_SECONDS = 2592000

RECENT_SENT_MAX = 200
RECENT_SENT_TTL_SECONDS = 600.0

DELIVERY_MAX_MESSAGES = 20
DELIVERY_CHAR_GAP_SECONDS = 0.2


def _action_receipt(action: str, ok: bool, **extra) -> dict:
    receipt = {"action": action, "ok": bool(ok), "at": time.time()}
    receipt.update({key: value for key, value in extra.items() if value is not None})
    return receipt


def sent_action_receipts(quote_used: bool, at_ids=None) -> List[dict]:
    receipts = []
    if quote_used:
        receipts.append(_action_receipt("引用", True))
    ids = [str(q).strip() for q in (at_ids or []) if str(q).strip()]
    if ids:
        receipts.append(_action_receipt("@人", True,
                                        target=ids[0] if len(ids) == 1 else None))
    return receipts


def _char_pieces(text) -> List[str]:
    rest = str(text or "")
    pieces: List[str] = []
    while rest:
        idx = rest.find(MENTION_PLACEHOLDER)
        if idx < 0:
            pieces.extend(ch for ch in rest if ch.strip())
            break
        pieces.extend(ch for ch in rest[:idx] if ch.strip())
        pieces.append(MENTION_PLACEHOLDER)
        rest = rest[idx + len(MENTION_PLACEHOLDER):]
    return pieces

VOICE_MODE_ALWAYS = "always"
VOICE_MODE_CHANCE = "chance"
VOICE_MODE_PRIVATE = "private"
VOICE_MODES = (VOICE_MODE_ALWAYS, VOICE_MODE_CHANCE, VOICE_MODE_PRIVATE)
DEFAULT_VOICE_CHANCE = 0.3


def _voice_mode(config) -> str:
    mode = str((config or {}).get("reply_voice_mode") or VOICE_MODE_ALWAYS).strip().lower()
    return mode if mode in VOICE_MODES else VOICE_MODE_ALWAYS


def voice_mode_allows(config, session_type: str) -> bool:
    """只按模式判断，不抽签；chance 模式视为可能发语音。"""
    return _voice_mode(config) != VOICE_MODE_PRIVATE or str(session_type or "") == "private"


def voice_wanted(config, session_type: str, client) -> bool:
    """确定这一轮不会发语音时返回 False，用于省掉只为配音做的额外请求。"""
    if not (config or {}).get("tts_reply_enabled", True):
        return False
    if not client_supports(client, "voice"):
        return False
    return voice_mode_allows(config, session_type)


def voice_enabled_for(config, session_type: str) -> bool:
    if not voice_mode_allows(config, session_type):
        return False
    if _voice_mode(config) != VOICE_MODE_CHANCE:
        return True
    try:
        chance = float((config or {}).get("reply_voice_chance", DEFAULT_VOICE_CHANCE))
    except (TypeError, ValueError):
        chance = DEFAULT_VOICE_CHANCE
    return random.random() < max(0.0, min(1.0, chance))


def voice_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _VOICE_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _VOICE_LOCKS[loop] = lock
    return lock


def _normalize_target(session_type: str, target_id) -> tuple:
    s = str(target_id)
    if s.startswith("private_"):
        return "private", s.split("_", 1)[1]
    if s.startswith("group_"):
        return "group", s.split("_", 1)[1].split("_")[0]
    return session_type, target_id


def _as_target(target_id):
    s = str(target_id)
    return int(s) if s.lstrip("-").isdigit() else s


def _extract_message_id(result) -> str:
    if isinstance(result, dict):
        for key in ("message_id", "msg_id", "id"):
            value = result.get(key)
            if value not in (None, "", 0):
                return str(value)
    return ""


def recall_delay_of(sentence) -> float:
    raw = sentence.get("recall_delay") if isinstance(sentence, dict) else None
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        delay = DEFAULT_RECALL_DELAY_SECONDS
    return max(RECALL_MIN_DELAY_SECONDS, min(RECALL_MAX_DELAY_SECONDS, delay))


def mute_seconds_of(sentence) -> int:
    raw = sentence.get("mute_duration") if isinstance(sentence, dict) else None
    try:
        seconds = int(float(raw))
    except (TypeError, ValueError):
        seconds = DEFAULT_MUTE_SECONDS
    return max(MUTE_MIN_SECONDS, min(MUTE_MAX_SECONDS, seconds))


def mute_target_of(sentence, allowed_ids, speaker_id) -> str:
    allowed = [str(q).strip() for q in (allowed_ids or []) if str(q).strip()]
    raw = sentence.get("mute") if isinstance(sentence, dict) else None
    picked = str(raw).strip() if isinstance(raw, (str, int)) and not isinstance(raw, bool) else ""
    if picked:
        # 宁可这次禁言失败，也不能拿别人顶上
        return picked if picked in allowed else ""
    if allowed:
        return allowed[0]
    return ""


async def group_member_role(client, group_id, user_id) -> str:
    getter = getattr(client, "get_group_member_info", None)
    if getter is None or not group_id or not user_id:
        return ""
    try:
        resp = await getter(group_id=_as_target(group_id), user_id=_as_target(user_id))
    except Exception as e:
        print(f"查群成员角色失败 ({group_id}/{user_id}): {type(e).__name__}: {e}")
        return ""
    if isinstance(resp, dict) and isinstance(resp.get("data"), dict):
        resp = resp["data"]
    if not isinstance(resp, dict):
        return ""
    return str(resp.get("role") or "")


async def bot_can_manage(client, group_id) -> bool:
    if not group_id:
        return False
    role = await group_member_role(client, group_id, getattr(client, "self_id", ""))
    return role.lower() in GROUP_ADMIN_ROLES


async def can_recall_others(client, group_id, requester_id, requester_role="") -> bool:
    if not group_id or not requester_id:
        return False
    role = str(requester_role or "").lower()
    if role not in GROUP_ADMIN_ROLES:
        role = (await group_member_role(client, group_id, requester_id)).lower()
    if role not in GROUP_ADMIN_ROLES:
        return False
    return await bot_can_manage(client, group_id)


async def can_manage_group(client, group_id, requester_id, requester_role="") -> bool:
    return await can_recall_others(client, group_id, requester_id, requester_role)


async def mute_denied_reason(client, group_id, requester_id, requester_role,
                             target_id) -> str:
    if not await bot_can_manage(client, group_id):
        return "denied"
    target = str(target_id or "").strip()
    if (await group_member_role(client, group_id, target)).lower() == "owner":
        return "target_owner"
    requester = str(requester_id or "").strip()
    if target and target == requester:
        return ""
    if await can_manage_group(client, group_id, requester_id, requester_role):
        return ""
    return "denied"


async def can_mute(client, group_id, requester_id, requester_role, target_id) -> bool:
    return not await mute_denied_reason(client, group_id, requester_id,
                                        requester_role, target_id)


_VOICE_GAP_LEAD = 0.5     # 语音播完后额外留的间隔
_VOICE_GAP_FIXED = 0.2


class VoicePacer:

    def __init__(self, dynamic: bool = True, gap: float = _VOICE_GAP_LEAD,
                 fixed_gap: float = _VOICE_GAP_FIXED):
        self.dynamic = bool(dynamic)
        self.gap = gap
        self.fixed_gap = fixed_gap
        self._until = 0.0

    async def wait(self):
        delay = self._until - time.monotonic()
        self._until = 0.0
        if delay > 0:
            await asyncio.sleep(delay)

    async def hold_voice(self, path) -> float:
        if self.dynamic and path is not None:
            seconds = await asyncio.to_thread(get_audio_duration, str(path))
            span = float(seconds or 0.0) + self.gap
        else:
            span = self.fixed_gap
        self._until = max(self._until, time.monotonic() + span)
        return span


class MessageSender:

    def __init__(self, config, memory_manager, sticker_manager=None, stats=None):
        self.config = config
        self.memory_manager = memory_manager
        self.sticker_manager = sticker_manager
        self.last_sent_sticker = None
        self.stats = stats
        self.client = None  # NapCatClient，主循环连接后注入
        self.role_clients = {}
        self.session_clients = {}
        self.channel_clients = {}
        self._session_channels = {}
        self._session_channels_loaded = False
        self._text_only_last_sent = {}
        self.last_poke_receipt: Optional[dict] = None
        self.last_message_id = ""
        self._recall_tasks = set()
        self._recent_sent = {}
        self._sent_group = 0
        self.last_send_uncertain = False
        self._send_once = False

    def _session_channels_path(self) -> Optional[Path]:
        data_path = getattr(self.memory_manager, "data_path", None)
        return Path(data_path) / "session_channels.json" if data_path else None

    def _load_session_channels(self) -> None:
        if self._session_channels_loaded:
            return
        self._session_channels_loaded = True
        path = self._session_channels_path()
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"会话接入方式映射读取失败（按空处理）: {type(e).__name__}: {e}")
            return
        if isinstance(data, dict):
            self._session_channels = {str(k): str(v) for k, v in data.items() if v}

    def _save_session_channels(self) -> None:
        path = self._session_channels_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._session_channels, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        except OSError as e:
            print(f"会话接入方式映射保存失败（忽略）: {type(e).__name__}: {e}")

    def drop_channel_sessions(self, channel_key: str) -> List[str]:
        key = str(channel_key or "").strip()
        if not key:
            return []
        self._load_session_channels()
        dropped = [sid for sid, value in self._session_channels.items() if value == key]
        if not dropped:
            return []
        for sid in dropped:
            self._session_channels.pop(sid, None)
        self._save_session_channels()
        return dropped

    def set_channel_client(self, channel_key: str, client) -> None:
        key = str(channel_key or "").strip()
        if not key:
            return
        if client is None:
            self.channel_clients.pop(key, None)
        else:
            self.channel_clients[key] = client

    def channel_key_of(self, client) -> str:
        if client is None:
            return ""
        conn = getattr(client, "connection", None)
        if isinstance(conn, dict) and str(conn.get("id") or "").strip():
            return str(conn["id"]).strip()
        for key, value in self.channel_clients.items():
            if value is client:
                return key
        return ""

    def remember_session(self, session_id: str, client) -> None:
        sid = str(session_id or "")
        if not sid or client is None:
            return
        self.session_clients[sid] = client
        key = self.channel_key_of(client)
        if key and self._session_channels.get(sid) != key:
            self._load_session_channels()
            self._session_channels[sid] = key
            self._save_session_channels()

    _WECHAT_TARGET_MARKERS = ("@im.wechat", "@chatroom", "@im.bot",
                              "_im_wechat", "_chatroom", "_im_bot")

    @classmethod
    def platform_of_session(cls, session_id: str) -> str:
        raw = str(session_id or "")
        target = raw.split("_", 1)[1] if raw.startswith(("private_", "group_")) else raw
        if any(marker in target for marker in cls._WECHAT_TARGET_MARKERS):
            return "wechat_clawbot"
        return ""

    def _client_for_platform(self, platform: str):
        if not platform:
            return None
        matches = sorted((key, client) for key, client in self.channel_clients.items()
                         if str(getattr(type(client), "platform", "") or "") == platform)
        return matches[0][1] if matches else None

    def session_channel_key(self, session_id: str) -> str:
        sid = str(session_id or "")
        if not sid:
            return ""
        client = self.session_clients.get(sid)
        if client is not None:
            key = self.channel_key_of(client)
            if key:
                return key
        self._load_session_channels()
        return str(self._session_channels.get(sid, "") or "")

    def session_client(self, session_id: str):
        sid = str(session_id or "")
        client = self.session_clients.get(sid)
        if client is None:
            self._load_session_channels()
            key = self._session_channels.get(sid, "")
            client = self.channel_clients.get(key) if key else None
        if client is None:
            platform = self.platform_of_session(sid)
            client = self._client_for_platform(platform)
            if client is not None:
                print(f"会话 {sid} 没有接入方式记录，按 ID 判断走「{platform}」这条连接。")
                self.remember_session(sid, client)
        return client

    @contextlib.contextmanager
    def for_session(self, session_id: str):
        with self.using_client(self.session_client(session_id)):
            yield

    # ---------------- 连接选择 ----------------
    def set_role_client(self, role_key: str, client) -> None:
        key = str(role_key or "").strip()
        if not key:
            return
        if client is None:
            self.role_clients.pop(key, None)
        else:
            self.role_clients[key] = client

    def client_for(self, role) -> object:
        role = role or {}
        key = str(role.get("character_key", "") or "").strip()
        if key:
            client = self.role_clients.get(key)
            if client is not None:
                return client
        return self.client

    @contextlib.contextmanager
    def using_client(self, client):
        token = _CURRENT_CLIENT.set(client)
        try:
            yield
        finally:
            _CURRENT_CLIENT.reset(token)

    def _active_client(self):
        return _CURRENT_CLIENT.get() or self.client

    @contextlib.contextmanager
    def send_once(self):
        prev = self._send_once
        self._send_once = True
        try:
            yield
        finally:
            self._send_once = prev

    # ---------------- 基础发送 ----------------
    async def send_segments(self, session_type: str, target_id, segments: list,
                            group=None) -> bool:
        self.last_send_uncertain = False
        client = self._active_client()
        if client is None:
            print("发送失败：NapCat 客户端尚未连接")
            return False
        session_type, target_id = _normalize_target(session_type, target_id)
        self.last_message_id = ""
        try:
            result = await self._send_with_retry(session_type, target_id, segments)
            self.last_message_id = _extract_message_id(result)
            self._remember_sent(session_type, target_id, self.last_message_id, group)
            return True
        except Exception as e:
            self.last_send_uncertain = isinstance(e, (asyncio.TimeoutError, TimeoutError))
            print(f"发送消息失败 ({session_type} {target_id}): {type(e).__name__}: {e}")
            return False

    def new_message_group(self) -> int:
        self._sent_group += 1
        return self._sent_group

    def _remember_sent(self, session_type: str, target_id, message_id: str,
                       group=None) -> None:
        mid = str(message_id or "").strip()
        if not mid:
            return
        key = f"{session_type}|{target_id}"
        now = time.monotonic()
        items = [(t, m, g) for t, m, g in self._recent_sent.get(key, [])
                 if now - t < RECENT_SENT_TTL_SECONDS]
        items.append((now, mid, self.new_message_group() if group is None else group))
        self._recent_sent[key] = items[-RECENT_SENT_MAX:]

    async def _send_with_retry(self, session_type: str, target_id, segments: list):
        client = self._active_client()
        retries = 0 if self._send_once else max(0, int(self.config.get("send_retry", 1) or 0))
        for attempt in range(retries + 1):
            try:
                if session_type == "private":
                    return await client.send_private_msg(user_id=_as_target(target_id),
                                                         message=segments)
                return await client.send_group_msg(group_id=_as_target(target_id),
                                                   message=segments)
            except (asyncio.TimeoutError, TimeoutError):
                if attempt >= retries:
                    raise
                delay = 2.0 * (attempt + 1)
                print(f"发送超时 ({session_type} {target_id})，{delay:.0f}s 后重试 "
                      f"({attempt + 1}/{retries})")
                await asyncio.sleep(delay)

    async def send_text(self, session_type: str, target_id, text: str,
                        sticker=None, reply_id=None, at_ids=None, group=None) -> bool:
        from napcat import Text, Image, At, Reply
        normalized_type, _ = _normalize_target(session_type, target_id)
        text = "" if text is None else str(text)
        # 免得撤回拿到上一条消息的 id
        self.last_message_id = ""
        mentions = []
        if normalized_type == "group":
            seen_at = set()
            for qq in at_ids or []:
                value = str(qq or "").strip()
                if value and value not in seen_at:
                    mentions.append(At(qq=value))
                    seen_at.add(value)
        body = []
        if mentions and MENTION_PLACEHOLDER in text:
            head, _, tail = text.partition(MENTION_PLACEHOLDER)
            tail = tail.replace(MENTION_PLACEHOLDER, "")
            if head.strip():
                body.append(Text(text=head))
            body.extend(mentions)
            tail = tail.lstrip(" \t")
            if tail:
                body.append(Text(text=" " + tail))
        else:
            if MENTION_PLACEHOLDER in text:
                text = re.sub(r"[ \t]{2,}", " ",
                              text.replace(MENTION_PLACEHOLDER, "")).strip(" \t")
            body.extend(mentions)
            if mentions and text.strip():
                text = " " + text
            if text.strip():
                body.append(Text(text=text))
        segments = []
        if reply_id is not None and (normalized_type == "group"
                                     or client_supports(self._active_client(),
                                                        "quote_private", default=False)):
            segments.append(Reply(id=str(reply_id)))
        segments.extend(body)
        temp_sticker = None
        if sticker is not None and not client_supports(self._active_client(), "sticker"):
            sticker = None
        if sticker is not None:
            try:
                send_sticker, temp_sticker = resize_for_output(sticker, self.config)
                segments.append(Image(file=str(Path(send_sticker).resolve())))
            except Exception as e:
                if temp_sticker is not None:
                    Path(temp_sticker).unlink(missing_ok=True)
                    temp_sticker = None
                print(f"表情包发送失败: {e}")
        if not segments:
            return True
        await self._pace_text_only(session_type, target_id)
        try:
            ok = await self.send_segments(session_type, target_id, segments, group=group)
            if ok and sticker is not None:
                self.last_sent_sticker = {"key": f"{normalized_type}|{target_id}",
                                          "path": str(sticker)}
            return ok
        finally:
            if temp_sticker is not None:
                Path(temp_sticker).unlink(missing_ok=True)

    async def pace_text_only(self, session_type: str, target_id) -> None:
        await self._pace_text_only(session_type, target_id, force=True)

    async def _pace_text_only(self, session_type: str, target_id,
                              force: bool = False) -> None:
        if not force and client_supports(self._active_client(), "voice"):
            return
        key = f"{session_type}|{target_id}"
        now = time.monotonic()
        gap = TEXT_ONLY_SEND_MIN_GAP + random.uniform(0, TEXT_ONLY_SEND_GAP_JITTER)
        last = self._text_only_last_sent.get(key)
        wait = 0.0 if last is None else max(0.0, last + gap - now)
        if wait:
            await asyncio.sleep(wait)
        self._text_only_last_sent[key] = time.monotonic()

    async def send_voice(self, session_type: str, target_id, wav_path, group=None) -> bool:
        from napcat import Record
        return await self.send_segments(session_type, target_id,
                                        [Record(file=str(Path(wav_path).resolve()))],
                                        group=group)

    async def poke_receipt(self, session_type: str, target_id, user_id) -> dict:
        client = self._active_client()
        if client is None:
            return _action_receipt("戳一戳", False, reason="no_client")
        if not self.config.get("poke_enabled", True):
            return _action_receipt("戳一戳", False, reason="disabled")
        if not client_supports(client, "poke"):
            return _action_receipt("戳一戳", False, reason="unsupported")
        session_type, target_id = _normalize_target(session_type, target_id)
        poke_id = str(user_id or "").strip() or str(target_id)
        try:
            if session_type == "group":
                await client.group_poke(group_id=_as_target(target_id),
                                        user_id=_as_target(poke_id))
            else:
                await client.friend_poke(user_id=_as_target(poke_id))
        except Exception as e:
            print(f"戳一戳发送失败 ({session_type} {target_id}): {type(e).__name__}: {e}")
            return _action_receipt("戳一戳", False, reason="error")
        return _action_receipt("戳一戳", True, target=poke_id)

    async def send_poke(self, session_type: str, target_id, user_id) -> bool:
        receipt = await self.poke_receipt(session_type, target_id, user_id)
        self.last_poke_receipt = receipt
        return bool(receipt.get("ok"))

    async def _poke_and_receipt(self, session_type: str, target_id, target) -> dict:
        ok = await self.send_poke(session_type, target_id, target)
        receipt = getattr(self, "last_poke_receipt", None)
        if isinstance(receipt, dict) and receipt:
            return receipt
        return _action_receipt("戳一戳", bool(ok), target=str(target or ""))

    def _track_sent_id(self, bucket: List[str]) -> None:
        mid = str(self.last_message_id or "").strip()
        if mid:
            bucket.append(mid)

    async def delete_message(self, session_type: str, target_id, message_id) -> bool:
        client = self._active_client()
        mid = str(message_id or "").strip()
        if client is None or not mid:
            return False
        if not client_supports(client, "recall"):
            return False
        try:
            await client.delete_msg(message_id=_as_target(mid))
        except Exception as e:
            print(f"撤回消息失败 ({session_type} {target_id}): {type(e).__name__}: {e}")
            return False
        return True

    def schedule_recall(self, session_type: str, target_id, message_ids,
                        delay: float) -> None:
        ids = [str(m) for m in (message_ids or []) if str(m or "").strip()]
        if not ids:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        client = self._active_client()

        async def _worker():
            await asyncio.sleep(delay)
            with self.using_client(client):
                done = 0
                for mid in ids:
                    if await self.delete_message(session_type, target_id, mid):
                        done += 1
                if done < len(ids):
                    print(f"撤回：{done}/{len(ids)} 条撤回成功（发不出去的通常是这条接入方式不支持撤回）。")

        task = loop.create_task(_worker())
        self._recall_tasks.add(task)
        task.add_done_callback(self._recall_tasks.discard)

    async def recall_recent(self, session_type: str, target_id, count: int = 1) -> int:
        session_type, target_id = _normalize_target(session_type, target_id)
        key = f"{session_type}|{target_id}"
        now = time.monotonic()
        items = [(t, m, g) for t, m, g in self._recent_sent.get(key, [])
                 if now - t < RECENT_SENT_TTL_SECONDS]
        self._recent_sent[key] = items
        done = 0
        for _ in range(max(1, int(count or 1))):
            if not items:
                break
            _, mid, _ = items.pop()
            if await self.delete_message(session_type, target_id, mid):
                done += 1
        self._recent_sent[key] = items
        return done

    async def recall_message(self, session_type: str, target_id, message_id) -> int:
        session_type, target_id = _normalize_target(session_type, target_id)
        wanted = str(message_id or "").strip()
        if not wanted:
            return 0
        key = f"{session_type}|{target_id}"
        now = time.monotonic()
        items = [(t, m, g) for t, m, g in self._recent_sent.get(key, [])
                 if now - t < RECENT_SENT_TTL_SECONDS]
        group = next((g for _, m, g in items if m == wanted), None)
        ids = [m for _, m, g in items if g == group] if group is not None else [wanted]
        done = 0
        for mid in ids:
            if await self.delete_message(session_type, target_id, mid):
                done += 1
        if done:
            gone = set(ids)
            self._recent_sent[key] = [(t, m, g) for t, m, g in items if m not in gone]
        else:
            self._recent_sent[key] = items
        return done

    async def recall_other_receipt(self, session_type: str, target_id, message_id,
                                   requester_id: str = "",
                                   requester_role: str = "") -> dict:
        client = self._active_client()
        if client is None:
            return _action_receipt("撤回别人的消息", False, reason="no_client")
        if not self.config.get("recall_other_enabled", True):
            return _action_receipt("撤回别人的消息", False, reason="disabled")
        if str(session_type) != "group" or not client_supports(client, "recall_other"):
            return _action_receipt("撤回别人的消息", False, reason="unsupported")
        mid = str(message_id or "").strip()
        if not mid:
            return _action_receipt("撤回别人的消息", False, reason="error")
        session_type, target_id = _normalize_target(session_type, target_id)
        own = any(m == mid for _, m, _ in self._recent_sent.get(
            f"{session_type}|{target_id}", []))
        if not own and not await can_manage_group(client, target_id, requester_id,
                                                 requester_role):
            return _action_receipt("撤回别人的消息", False, reason="denied")
        done = await self.recall_message(session_type, target_id, mid)
        if not done:
            return _action_receipt("撤回别人的消息", False, reason="error")
        return _action_receipt("撤回别人的消息", True, count=done)

    async def mute_receipt(self, session_type: str, target_id, user_id, seconds: int = 0,
                           requester_id: str = "", requester_role: str = "") -> dict:
        client = self._active_client()
        if client is None:
            return _action_receipt("禁言", False, reason="no_client")
        if not self.config.get("mute_enabled", True):
            return _action_receipt("禁言", False, reason="disabled")
        if str(session_type) != "group" or not client_supports(client, "mute"):
            return _action_receipt("禁言", False, reason="unsupported")
        session_type, target_id = _normalize_target(session_type, target_id)
        target = str(user_id or "").strip()
        if not target or target == str(getattr(client, "self_id", "") or ""):
            return _action_receipt("禁言", False, reason="no_target")
        reason = await mute_denied_reason(client, target_id, requester_id,
                                          requester_role, target)
        if reason:
            return _action_receipt("禁言", False, reason=reason, target=target)
        try:
            await client.set_group_ban(group_id=_as_target(target_id),
                                       user_id=_as_target(target),
                                       duration=int(seconds or DEFAULT_MUTE_SECONDS))
        except Exception as e:
            print(f"禁言失败 ({target_id}/{target}): {type(e).__name__}: {e}")
            return _action_receipt("禁言", False, reason="error", target=target)
        return _action_receipt("禁言", True, target=target)

    async def _send_plain_reply(self, session_type: str, target_id, sentences: List[dict],
                                delivery: str, reply_id=None, at_ids=None,
                                ctx: Optional[RoleContext] = None) -> dict:
        result = {"tts_ms": 0.0, "voice_ok": False, "tts_calls": 0, "sent_texts": [],
                  "message_ids": [], "action_receipts": []}
        texts: List[str] = []
        if delivery == DELIVERY_CHARS:
            for s in sentences:
                texts.extend(_char_pieces(display_text_of(s, ctx)))
        if not texts or len(texts) > DELIVERY_MAX_MESSAGES:
            texts = [display_text_of(s, ctx).strip() for s in sentences]
            texts = [t for t in texts if t]
        mention_in_body = any(MENTION_PLACEHOLDER in t for t in texts)
        for idx, text in enumerate(texts):
            if idx:
                await asyncio.sleep(DELIVERY_CHAR_GAP_SECONDS)
            # 真正被@的是它所在的那条消息
            at_used = at_ids if (at_ids and (
                MENTION_PLACEHOLDER in text
                or (idx == 0 and not mention_in_body))) else None
            ok = await self.send_text(
                session_type, target_id, text,
                reply_id=reply_id if idx == 0 else None,
                at_ids=at_used)
            if ok:
                result["action_receipts"].extend(sent_action_receipts(
                    reply_id is not None and idx == 0, at_used))
                result["sent_texts"].append(text)
                self._track_sent_id(result["message_ids"])
        return result

    async def send_reply(self, session_type: str, target_id: str, sentences: List[dict],
                        emotions: dict, ctx: RoleContext, use_voice: bool = True,
                        reply_id=None, allowed_at_ids=None, poke_target=None,
                        recall_request: str = "", at_names=None,
                        speaker_id: str = "", speaker_role: str = "",
                        mute_ids=None, recall_other_id: str = "",
                        mute_done_ids=None) -> dict:
        result = {"tts_ms": 0.0, "voice_ok": False, "tts_calls": 0, "sent_texts": [],
                  "message_ids": [], "action_receipts": []}
        if use_voice and not client_supports(self._active_client(), "voice"):
            use_voice = False
        if use_voice and not voice_enabled_for(self.config, session_type):
            use_voice = False
        if not sentences:
            return result
        session_type, target_id = _normalize_target(session_type, target_id)
        mute_sentence = next((s for s in sentences if s.get("mute")), None)
        if mute_sentence is not None:
            mute_target = mute_target_of(mute_sentence, mute_ids, speaker_id)
            done_ids = {str(q) for q in (mute_done_ids or [])}
            if mute_target and str(mute_target) in done_ids:
                print(f"禁言：本轮系统已对 {mute_target} 执行过，台词里的 mute 不再重复执行。")
            else:
                result["action_receipts"].append(await self.mute_receipt(
                    session_type, target_id, mute_target,
                    mute_seconds_of(mute_sentence), speaker_id, speaker_role))
        recall_other_sentence = next((s for s in sentences if s.get("recall_other")), None)
        if recall_other_sentence is not None and recall_request != RECALL_DENY:
            result["action_receipts"].append(await self.recall_other_receipt(
                session_type, target_id, recall_other_id, speaker_id, speaker_role))
        literal_mention = False
        for sentence in sentences:
            if apply_literal_mention(sentence, allowed_at_ids, at_names, speaker_id):
                literal_mention = True
        if literal_mention:
            print("发送：台词里写成文字的@，已换成真正的@。")
        action_sentence = next((s for s in sentences
                                if s.get("reply_to") or s.get("mention_ids") or s.get("poke")
                                or s.get("delivery") or s.get("recall")), {})
        selected_mentions = []
        for sentence in sentences:
            for qq in sentence.get("mention_ids") or []:
                value = str(qq)
                if value in {str(x) for x in (allowed_at_ids or [])} \
                        and value not in selected_mentions:
                    selected_mentions.append(value)
        action_reply_id = reply_id if action_sentence.get("reply_to") else None
        actions_pending = bool(action_reply_id is not None or selected_mentions)
        mention_in_body = any(MENTION_PLACEHOLDER in str(s.get("display") or "")
                              for s in sentences)
        poke_pending = bool(action_sentence.get("poke")) and bool(poke_target)
        recall_pending = (recall_request == "next"
                          or (bool(action_sentence.get("recall"))
                              and recall_request != RECALL_DENY)) \
            and bool(self.config.get("recall_enabled", True))
        recall_delay = recall_delay_of(action_sentence)
        # 语音合成也会白跑一遍
        delivery = str(action_sentence.get("delivery") or "").strip().lower()
        if delivery not in DELIVERY_MODES:
            delivery = ""
        reply_limit = client_reply_limit(self._active_client())
        if reply_limit and delivery:
            delivery = ""
            print(f"发送：这条通道一次回复最多 {reply_limit} 条消息，逐字发送已改回合并发送。")
        if delivery:
            if poke_pending:
                result["action_receipts"].append(
                    await self._poke_and_receipt(session_type, target_id, poke_target))
            plain = await self._send_plain_reply(
                session_type, target_id, sentences, delivery,
                reply_id=action_reply_id, at_ids=selected_mentions, ctx=ctx)
            if recall_pending:
                self.schedule_recall(session_type, target_id,
                                     plain.get("message_ids"), recall_delay)
            plain["action_receipts"] = list(result["action_receipts"]) + list(
                plain.get("action_receipts") or [])
            return plain

        sent_texts: List[str] = []
        sent_ids: List[str] = []

        action_receipts: List[dict] = []

        async def _send_text(text, sticker=None, zh=None, group=None, pace=False):
            nonlocal actions_pending, poke_pending
            if poke_pending:
                poke_pending = False
                action_receipts.append(
                    await self._poke_and_receipt(session_type, target_id, poke_target))
            # 不再发一条空消息
            if not str(text or "").strip() and sticker is None:
                actions_pending = False
                return True
            if pace:
                await self.pace_text_only(session_type, target_id)
            quote_used = bool(actions_pending and action_reply_id is not None)
            at_used = selected_mentions if (selected_mentions and (
                MENTION_PLACEHOLDER in str(text)
                or (actions_pending and not mention_in_body))) else None
            ok = await self.send_text(
                session_type, target_id, text, sticker=sticker,
                reply_id=action_reply_id if quote_used else None,
                at_ids=at_used,
                group=group)
            if ok:
                action_receipts.extend(sent_action_receipts(quote_used, at_used))
                actions_pending = False
                sent_texts.append(str(text if zh is None else zh))
                self._track_sent_id(sent_ids)
            return ok

        data_path = self.memory_manager.data_path
        separate_send = self.config.get("separate_send", False)
        send_voice_separately = self.config.get("send_voice_separately", False)
        text_separate = self.config.get("text_separate", False)
        dynamic_sleep = self.config.get("dynamic_sleep", True)
        use_tts = use_voice and self.config.get("tts_reply_enabled", True)
        if separate_send and (send_voice_separately or text_separate) \
                and self.config.get("separate_force_segment", True):
            sentences = segment_for_tts(sentences, ctx)

        wavs: List[Optional[Path]] = [None] * len(sentences)
        if use_tts and self._active_client() is not None:
            synth_started = time.time()
            synth_sem = asyncio.Semaphore(2)
            async def _synthesize(s):
                async with synth_sem:
                    return await synthesize_sentence(ctx, s["lang"], s.get("emotion", ""), emotions,
                                                     data_path, stats=self.stats,
                                                     mimic=s.get("mimic", ""),
                                                     mimics=available_mimics(ctx))
            async with voice_lock():
                results = await asyncio.gather(*[_synthesize(s) for s in sentences],
                                               return_exceptions=True)
            result["tts_ms"] = (time.time() - synth_started) * 1000
            for idx, item in enumerate(results):
                if isinstance(item, BaseException):
                    print(f"第 {idx + 1} 句语音合成异常，该句降级为纯文本: "
                          f"{type(item).__name__}: {item}")
                    wavs[idx] = None
                else:
                    wavs[idx] = item
        valid_wavs = [w for w in wavs if w]
        result["voice_ok"] = bool(valid_wavs)
        result["tts_calls"] = len(valid_wavs)

        # 累计张数受 sticker_max_per_reply 限制
        sticker_sent = 0

        async def _pick_sticker_for(idx: int):
            nonlocal sticker_sent
            if self.sticker_manager is None or self._active_client() is None:
                return None
            if not client_supports(self._active_client(), "sticker"):
                return None
            try:
                max_stickers = max(1, int(self.config.get("sticker_max_per_reply", 1)))
            except (TypeError, ValueError):
                max_stickers = 1
            if sticker_sent >= max_stickers:
                return None
            if not bool(self.config.get("sticker_every_sentence", False)) and idx != 0:
                return None
            sentence = sentences[idx]
            st = await self.sticker_manager.pick_async(
                ctx, sentence.get("emotion", ""),
                str(sentence.get("display") or sentence.get("zh") or ""))
            if st:
                sticker_sent += 1
            return st

        if separate_send and send_voice_separately:
            missing = []
            done_stickers = []
            pacer = VoicePacer(dynamic_sleep)
            for idx, wav in enumerate(wavs):
                sentence_text = sentences[idx]["display"]
                if not wav or not wav.exists():
                    missing.append(idx)
                    continue
                await pacer.wait()
                msg_group = self.new_message_group()
                if sentence_text:
                    await _send_text(sentence_text, zh=sentences[idx].get("zh"),
                                     group=msg_group)
                await self.send_voice(session_type, target_id, wav, group=msg_group)
                self._track_sent_id(sent_ids)
                await pacer.hold_voice(wav)
                wav.unlink(missing_ok=True)
                sticker_i = await _pick_sticker_for(idx)
                if sticker_i:
                    done_stickers.append(sticker_i)

            if not valid_wavs:
                for s in sentences:
                    text = s["display"]
                    if text:
                        await _send_text(text, zh=s.get("zh"), pace=True)
            else:
                for idx in missing:
                    text = sentences[idx]["display"]
                    if text:
                        await _send_text(text, zh=sentences[idx].get("zh"), pace=True)

            # ⑤ 最后统一发送表情包
            for sticker_path in done_stickers:
                await self.send_text(session_type, target_id, "", sticker=sticker_path)
                self._track_sent_id(sent_ids)

        else:
            # 放进线程池避免阻塞事件循环
            combined_audio = await asyncio.to_thread(
                merge_wavs, valid_wavs, self.config, data_path) if valid_wavs else None
            if valid_wavs and not combined_audio:
                from .tts import simple_concat_wavs
                combined_audio = await asyncio.to_thread(
                    simple_concat_wavs, valid_wavs, data_path)
                if combined_audio:
                    print("合并音频已降级为无损直接拼接（内容不丢）。")
            combined_text = "".join(s["display"] for s in sentences)

            if combined_audio:
                if separate_send and text_separate:
                    await self.send_voice(session_type, target_id, combined_audio)
                    self._track_sent_id(sent_ids)
                    for i, s in enumerate(sentences):
                        await _send_text(s["display"], zh=s.get("zh"))
                        await asyncio.sleep(0.2)
                else:
                    msg_group = self.new_message_group()
                    await _send_text(combined_text, group=msg_group)
                    await self.send_voice(session_type, target_id, combined_audio,
                                          group=msg_group)
                    self._track_sent_id(sent_ids)

                # 清理临时文件
                for w in valid_wavs:
                    w.unlink(missing_ok=True)
                combined_audio.unlink(missing_ok=True)
            else:
                # 语音合成失败，降级纯文本
                print("TTS 合成失败或未启用，降级为纯文本。")
                await _send_text(combined_text)
                for w in valid_wavs:
                    w.unlink(missing_ok=True)

            # 最后发送表情包（合并路径最多一张）
            sticker = await _pick_sticker_for(0)
            if sticker:
                await self.send_text(session_type, target_id, "", sticker=sticker)
                self._track_sent_id(sent_ids)

        await asyncio.to_thread(self.memory_manager.cleanup_voice_cache,
                                self.config.get("max_voice_cache", 20))
        if recall_pending:
            self.schedule_recall(session_type, target_id, sent_ids, recall_delay)
            action_receipts.append({"action": "撤回", "ok": bool(sent_ids),
                                    "count": len(sent_ids), "at": time.time()})
        result["sent_texts"] = sent_texts
        result["message_ids"] = sent_ids
        result["action_receipts"] = list(result["action_receipts"]) + action_receipts
        return result

    async def speak_and_send(self, session_type: str, target_id, text: str,
                             emotions: dict, ctx: Optional[RoleContext] = None,
                             use_voice: bool = None, sticker: bool = False,
                             emotion: str = "", session_id: str = "",
                             record_history: bool = True) -> bool:
        if not text:
            return False
        self.last_send_uncertain = False
        if self._active_client() is None:
            print("主动消息发送失败：NapCat 客户端未连接")
            return False
        if ctx is None:
            ctx = RoleContext(self.config)
        if use_voice is None:
            use_voice = bool(self.config.get("proactive_voice", False))
        active = self._active_client()
        if use_voice and not client_supports(active, "voice"):
            use_voice = False
        if sticker and not client_supports(active, "sticker"):
            sticker = False
        sticker_path = (await self.sticker_manager.pick_async(ctx, emotion, text)
                        if (sticker and self.sticker_manager) else None)
        if use_voice and await check_tts_service(self.config):
            voice = str(emotion or ctx.get("default_voice", "pingjing") or "pingjing")
            speech_text = await self._speech_text_for(ctx, text)
            wav = await self._synthesize_proactive(ctx, speech_text, voice, emotions)
            if wav:
                try:
                    # 否则调用方会把同一句话重发一遍
                    text_ok = await self.send_text(session_type, target_id, text)
                    await self.send_voice(session_type, target_id, wav)
                    if sticker_path:
                        await self.send_text(session_type, target_id, "", sticker=sticker_path)
                    if text_ok and record_history:
                        self.record_outgoing_history(session_id, text, ctx)
                    return bool(text_ok)
                finally:
                    wav.unlink(missing_ok=True)
        ok = await self.send_text(session_type, target_id, text, sticker=sticker_path)
        if ok and record_history:
            self.record_outgoing_history(session_id, text, ctx)
        return ok

    def record_outgoing_history(self, session_id: str, text: str, ctx=None) -> None:
        if not session_id or self.memory_manager is None or not str(text or "").strip():
            return
        try:
            data = self.memory_manager.load_session_data(session_id)
            data.setdefault("history", []).append({
                "role": "assistant", "content": text, "timestamp": time.time(),
                "speaker": ctx.character_name if ctx is not None else "",
                "proactive": True})
            self.memory_manager.save_session_data(session_id, data)
        except Exception as e:
            print(f"主动消息写入历史失败: {type(e).__name__}: {e}")

    async def _speech_text_for(self, ctx, text: str) -> str:
        target = str(ctx.get("text_lang", "") or "").strip().lower()
        if not target or target == "auto":
            return text
        if not lang_text_broken(text, target):
            return text
        translated = await translate_to_lang(ctx, text, target, label="台词翻译")
        if translated and translated != text:
            print(f"主动消息语音语言修复：展示文本不是{target}，已按{target}重译后合成"
                  f"（{translated[:40]!r}）")
            return translated
        print(f"主动消息语音语言修复失败，改按台词文字判定语言合成：{text[:40]!r}")
        return text

    async def _synthesize_proactive(self, ctx, text: str, emotion: str, emotions: dict):
        from .tts import split_tts_chunks
        data_path = self.memory_manager.data_path
        chunks = split_tts_chunks(text, config=self.config)
        if len(chunks) > 1:
            wavs = []
            for chunk in chunks:
                w = await synthesize_sentence(ctx, chunk, emotion, emotions,
                                              data_path, stats=self.stats)
                if not w:
                    print(f"主动消息分段合成失败，改用整段合成: {chunk[:40]!r}")
                    for done in wavs:
                        done.unlink(missing_ok=True)
                    wavs = []
                    break
                wavs.append(w)
            if wavs:
                merged = await self._merge_voice_chunks(wavs, data_path, keep_on_fail=True)
                if merged:
                    return merged
        return await synthesize_sentence(ctx, text, emotion, emotions,
                                         data_path, stats=self.stats)

    async def _merge_voice_chunks(self, wavs: list, data_path, keep_on_fail: bool = False):
        import wave as _wave
        from .tts import merge_wavs, simple_concat_wavs

        def _usable(path) -> bool:
            if not path:
                return False
            try:
                p = Path(path)
                if not p.exists() or p.stat().st_size <= 44:
                    return False
                with _wave.open(str(p), 'rb') as wf:
                    return wf.getnframes() > 0
            except Exception:
                return False

        if len(wavs) == 1:
            return wavs[0] if _usable(wavs[0]) else None
        attempts = [
            ("语气渐变合并", lambda: merge_wavs(wavs, self.config, data_path)),
            ("无渐变无损拼接", lambda: merge_wavs(wavs, self.config, data_path,
                                                crossfade_ms=0)),
            ("直接拼接兜底", lambda: simple_concat_wavs(wavs, data_path)),
        ]
        merged = None
        for label, fn in attempts:
            try:
                candidate = await asyncio.to_thread(fn)
            except Exception as e:
                print(f"{label} 异常: {type(e).__name__}: {e}")
                continue
            if _usable(candidate):
                merged = candidate
                if label != attempts[0][0]:
                    print(f"分段音频合并已降级为「{label}」。")
                break
            if candidate:
                print(f"{label} 产出不可用（空文件或读取失败），尝试下一种合并方式。")
        if merged:
            for w in wavs:
                w.unlink(missing_ok=True)
            return merged
        if keep_on_fail and _usable(wavs[0]):
            for w in wavs[1:]:
                w.unlink(missing_ok=True)
            print("分段音频合并失败，退回第一段（文本内容仍完整）。")
            return wavs[0]
        for w in wavs:
            w.unlink(missing_ok=True)
        return None
