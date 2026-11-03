# -*- coding: utf-8 -*-
# 总入口 handle_message_event 与主处理 _process_message_event。
import asyncio
import time
from pathlib import Path
from typing import Optional, Dict

from modules import app_context
from modules.adapters import make_event, client_supports
from modules.affection import SCORE_MAX, commitment_warning, looks_like_acceptance
from modules.companion_tasks import (background_ready, mood_diary_note,
                                     _take_recall_block, _summary_keep_count,
                                     post_reply_context_tasks,
                                     _extract_and_store_promise,
                                     _detect_rival_mention, _to_float,
                                     wants_task_action, check_task_progress,
                                     refuses_task,
                                     ACTION_NOW_HINT, _mood_bounds_now)
from modules.config_loader import typing_client
from modules.llm_helpers import (RoleContext, build_speaker_labels, identity_note,
                                 chat_once, extract_json,
                                 recent_user_ids, wants_mention_request,
                                 wants_quote_request, wants_repeat_request,
                                 sentence_actions_note, action_receipts_note,
                                 _ACTION_FAIL_REASONS,
                                 speaker_labeled_lines, resolve_emotion_key,
                                 MENTION_ALL_ID,
                                 MENTION_PLACEHOLDER, strip_mention_placeholder,
                                 POKE_MESSAGE_TEXT, recall_request_kind,
                                 wants_mute_request, wants_self_mute,
                                 mute_request_seconds,
                                 image_self_claim, IMAGE_CLAIM_WARNING,
                                 record_sent_links, urls_in_text, error_reply_text,
                                 repair_sentence_lang)
from modules.media_cache import (pick_media_source, refresh_image_urls,
                                 transcribe_voice_message, fetch_forward_text,
                                 fetch_quoted_context, _remember_pending_image,
                                 _take_pending_image, _clear_pending_image,
                                 _backfill_image_description, _attach_history_image,
                                 _cache_image_bytes, _spawn_sticker_capture,
                                 _diary_recognition_note)
from modules.mood import (clamp, commit_mood, current_mood, judge_and_decide,
                          judge_enabled, mood_enabled, mood_style,
                          affection_enabled, stored_mood,
                          fixed_reply_probability)
from modules.promises import PROMISE_HINT_RE
from modules.proactive_state import save_proactive_state
from modules.sender import RECALL_DENY, can_recall_others, voice_wanted
from modules.reply_pipeline import (SentenceSink, generate_reply, _spawn,
                                    _norm_text, _repeat_ratio, _repeat_ratio_user,
                                    _strip_image_claims,
                                    _recent_assistant_replies, _max_repeat_ratio,
                                    repeat_thresholds, repeat_guard_flags,
                                    repeat_guard_summary, repeat_guard_active,
                                    _log_mood_commit, _judge_mood_text,
                                    _judge_gate_cause, _image_reply_override,
                                    _tool_notes_from_trace,
                                    _USER_ECHO_MIN_CHARS, _RETRY_TEMPERATURE,
                                    _RETRY_TEMPERATURE_MAX)
from modules.session_context import (_session_whitelisted, _allow_message,
                                     get_active_ctx, reply_ctx_of,
                                     get_role_emotions, resolve_target_roles,
                                     config_for_client, _fetch_member_name,
                                     memory_for_session, role_memory)
from modules.tts_service import ensure_tts_service
from modules.webui_common import (_dispatch_plugin_command,
                                  _dispatch_plugin_message, _plugin_runtime)
from napcat import (PrivateMessageEvent, GroupMessageEvent, Text, Record,
                    Image, At, Reply, Forward, FriendPokeEvent, GroupPokeEvent)


_SESSION_PENDING: Dict[str, dict] = {}
_SESSION_LAST_INCOMING: Dict[str, str] = {}
_COALESCE_WINDOW = 1.2
_TASK_CONTINUE_ROUNDS = 2


def _touch_pending(session_id: str) -> None:
    pending = _SESSION_PENDING.get(session_id)
    if isinstance(pending, dict):
        pending["last_at"] = time.monotonic()


def _extract_event_info(event, client) -> Optional[dict]:
    if not isinstance(event, (PrivateMessageEvent, GroupMessageEvent)):
        return None
    is_private = isinstance(event, PrivateMessageEvent)
    user_text = ""
    has_image = False
    image_urls = []
    image_file_ids = {}
    has_voice = False
    voice_sources = []
    voice_file_ids = {}
    at_ids = []
    at_bot = False
    reply_seg = None
    forward_ids = []
    for seg in event.message:
        if isinstance(seg, Text):
            user_text += seg.text
        elif isinstance(seg, Image):
            has_image = True
            chosen = pick_media_source(seg)
            if chosen and chosen not in image_urls:
                image_urls.append(chosen)
                fid = getattr(seg, "file", None)
                if fid:
                    image_file_ids[chosen] = str(fid)
        elif isinstance(seg, Record):
            has_voice = True
            chosen = pick_media_source(seg, "语音")
            if chosen and chosen not in voice_sources:
                voice_sources.append(chosen)
                fid = getattr(seg, "file", None)
                if fid:
                    voice_file_ids[chosen] = str(fid)
        elif isinstance(seg, At):
            qq = str(seg.qq)
            at_ids.append(qq)
            if qq == str(client.self_id):
                at_bot = True
                user_text += "[@本机器人]"
            else:
                user_text += f"[@{qq}]"
        elif isinstance(seg, Reply):
            reply_seg = seg
        elif isinstance(seg, Forward):
            fid = str(getattr(seg, "id", "") or "")
            if fid and fid not in forward_ids:
                forward_ids.append(fid)
    if is_private:
        session_id = f"private_{event.user_id}"
    else:
        group_id = event.group_id
        sender_id = getattr(event.sender, "user_id", None) or "0"
        session_id = f"group_{group_id}"
        if app_context.active_config().get("isolated_session", False):
            session_id = f"group_{group_id}_{sender_id}"
    silent = False
    if not is_private and not at_bot:
        if reply_seg is None:
            if app_context.active_config().get("only_private", False):
                return None
            silent = bool(app_context.active_config().get("group_need_at", True))
    if not user_text and not has_image and not has_voice and not forward_ids:
        return None
    return {"session_id": session_id, "event": event, "client": client,
            "text": user_text, "has_image": has_image, "image_urls": image_urls,
            "image_file_ids": image_file_ids, "has_voice": has_voice,
            "voice_sources": voice_sources, "voice_file_ids": voice_file_ids,
            "at_ids": at_ids,
            "at_bot": at_bot, "reply_seg": reply_seg, "silent": silent,
            "forward_ids": forward_ids,
            "sender_id": str(getattr(event, "user_id", "") or "")}


def _merge_event_info(pending: dict, info: dict) -> dict:
    texts = [t for t in (pending.get("text", ""), info.get("text", "")) if t]
    urls = list(pending.get("image_urls", []))
    for u in info.get("image_urls", []):
        if u not in urls:
            urls.append(u)
    fids = dict(pending.get("image_file_ids", {}))
    fids.update(info.get("image_file_ids", {}))
    voices = list(pending.get("voice_sources", []))
    for v in info.get("voice_sources", []):
        if v not in voices:
            voices.append(v)
    vfids = dict(pending.get("voice_file_ids", {}))
    vfids.update(info.get("voice_file_ids", {}))
    at_ids = list(pending.get("at_ids", []))
    for q in info.get("at_ids", []):
        if q not in at_ids:
            at_ids.append(q)
    forward_ids = list(pending.get("forward_ids", []))
    for fid in info.get("forward_ids", []):
        if fid not in forward_ids:
            forward_ids.append(fid)
    return {"session_id": info["session_id"], "event": info["event"], "client": info["client"],
            "text": "\n".join(texts),
            "has_image": bool(pending.get("has_image") or info.get("has_image") or urls),
            "image_urls": urls, "image_file_ids": fids,
            "has_voice": bool(pending.get("has_voice") or info.get("has_voice") or voices),
            "voice_sources": voices, "voice_file_ids": vfids, "at_ids": at_ids,
            "at_bot": bool(pending.get("at_bot") or info.get("at_bot")),
            "reply_seg": info.get("reply_seg") or pending.get("reply_seg"),
            "forward_ids": forward_ids,
            "sender_id": info.get("sender_id") or pending.get("sender_id")}


def _merged_payload(pending: dict) -> dict:
    return {"text": pending["text"], "has_image": pending["has_image"],
            "image_urls": pending["image_urls"], "image_file_ids": pending["image_file_ids"],
            "has_voice": pending.get("has_voice", False),
            "voice_sources": pending.get("voice_sources", []),
            "voice_file_ids": pending.get("voice_file_ids", {}),
            "at_ids": pending["at_ids"], "at_bot": pending["at_bot"],
            "reply_seg": pending["reply_seg"], "forward_ids": pending.get("forward_ids", []),
            "sender_id": pending.get("sender_id", "")}


def _spawn_drainer(session_id: str, lock: asyncio.Lock):
    async def drain():
        async with lock:
            while True:
                pending = _SESSION_PENDING.pop(session_id, None)
                if pending is None:
                    break
                try:
                    await _process_message_event(pending["event"], pending["client"],
                                                 _merged_payload(pending))
                except Exception as e:
                    print(f"会话 {session_id} 排队消息处理异常: {type(e).__name__}: {e}")
    try:
        asyncio.get_running_loop().create_task(drain())
    except RuntimeError:
        pass


def _remember_unaddressed_message(session_id: str, text: str, has_image: bool,
                                  sender_id: str, sender_name: str,
                                  has_voice: bool = False,
                                  has_forward: bool = False) -> None:
    content = str(text or "").strip()
    if has_image:
        content = f"{content} [图片]".strip()
    if has_voice:
        content = f"{content} [语音]".strip()
    if has_forward:
        content = f"{content} [聊天记录]".strip()
    if not session_id or not content or app_context.memory_manager is None:
        return
    if not _session_whitelisted(session_id):
        return
    mem = memory_for_session(session_id) or app_context.memory_manager
    try:
        mem.migrate_legacy_memory(session_id)
        data = mem.load_session_data(session_id)
        history = data.get("history")
        if not isinstance(history, list):
            history = []
        history.append({
            "role": "user",
            "content": content,
            "sender_id": str(sender_id or ""),
            "sender_name": sender_name or str(sender_id or ""),
            "timestamp": time.time(),
        })
        data["history"] = history
        mem.save_session_data(session_id, data)
    except Exception as e:
        print(f"记录未@的群消息失败: {e}")


def _queue_unaddressed_message(session_id: str, text: str, has_image: bool,
                               sender_id: str, sender_name: str,
                               has_voice: bool = False,
                               has_forward: bool = False) -> None:
    async def run():
        lock = app_context._SESSION_LOCKS.setdefault(session_id, asyncio.Lock())
        async with lock:
            _remember_unaddressed_message(session_id, text, has_image,
                                          sender_id, sender_name, has_voice, has_forward)
    try:
        asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        pass


DEFAULT_SMART_REPLY_DELAY_SECONDS = 10.0
_SMART_REPLY_POLL_SECONDS = 0.5
_SMART_REPLY_JUDGE_TIMEOUT = 20.0
# 配置里没填提示词时用的判定指令
DEFAULT_SMART_REPLY_PROMPT = (
    "判断对话中最后一条用户消息是不是已经说完了。"
    "话说到一半（末尾是逗号、顿号、省略号或连接词，明显还有下半句）、"
    "正在分多条补充、列举或描述尚未结束，都算没说完；"
    "否则算说完了。只输出一个 JSON 对象：{\"finished\": true 或 false}，"
    "禁止输出任何其它文字、解释或 Markdown。"
)


def _config_flag(key: str, default: bool = False) -> bool:
    config = app_context.active_config()
    raw = config.get(key, default) if config else default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("true", "1", "yes", "y", "on", "是", "开启")


def smart_reply_enabled() -> bool:
    return _config_flag("smart_reply_enabled", False)


def _smart_reply_delay() -> float:
    config = app_context.active_config()
    raw = config.get("smart_reply_delay_seconds", DEFAULT_SMART_REPLY_DELAY_SECONDS) \
        if config else DEFAULT_SMART_REPLY_DELAY_SECONDS
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        return DEFAULT_SMART_REPLY_DELAY_SECONDS


def _parse_finished(raw) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw or "").strip().lower()
    if text in ("false", "0", "no", "n", "off", "没说完", "否"):
        return False
    return True


async def _user_finished_speaking(pending: dict) -> bool:
    text = str(pending.get("text") or "").strip()
    if not text:
        return True
    ctx = get_active_ctx()
    prompt = str(ctx.get("smart_reply_prompt", "") or "").strip() or DEFAULT_SMART_REPLY_PROMPT
    messages = [{"role": "system", "content": prompt},
                {"role": "user", "content": f"对话中最后一条用户消息是：{text}"}]
    try:
        result = await asyncio.wait_for(chat_once(ctx, messages, label="智能回复判定"),
                                        timeout=_SMART_REPLY_JUDGE_TIMEOUT)
    except Exception as e:
        print(f"智能回复判定失败，按「说完了」处理: {type(e).__name__}: {e}")
        return True
    obj = extract_json((result or {}).get("content") or "")
    if not isinstance(obj, dict):
        return True
    return _parse_finished(obj.get("finished", True))


async def _smart_reply_hold(pending: dict) -> bool:
    if not smart_reply_enabled():
        return False
    last_at = float(pending.get("last_at") or 0)
    if float(pending.get("smart_judged_at") or 0) != last_at:
        pending["smart_judged_at"] = last_at
        pending["smart_until"] = 0.0
        if not await _user_finished_speaking(pending):
            pending["smart_until"] = time.monotonic() + _smart_reply_delay()
            print("智能回复：判断对方还没说完，先等一会儿；期间没有新消息就直接回复。")
    until = float(pending.get("smart_until") or 0)
    return bool(until) and time.monotonic() < until


async def _fetch_user_nickname(client, user_id: str, group_id=None) -> str:
    if group_id:
        return await _fetch_member_name(client, group_id, user_id)
    getter = getattr(client, "get_stranger_info", None)
    if getter is None:
        return ""
    try:
        info = await getter(user_id=int(user_id))
    except Exception:
        return ""
    if isinstance(info, dict):
        return str(info.get("nickname") or "")
    return str(getattr(info, "nickname", "") or "")


async def _poke_to_message_event(event, client):
    if not isinstance(event, (FriendPokeEvent, GroupPokeEvent)):
        return None
    user_id = str(getattr(event, "user_id", "") or "")
    if not user_id or user_id == str(client.self_id):
        return None
    target = str(getattr(event, "target_id", "") or "")
    if target and target != str(client.self_id):
        return None
    group_id = getattr(event, "group_id", None)
    return make_event(private=not group_id, user_id=user_id, text=POKE_MESSAGE_TEXT,
                      group_id=group_id, self_id=client.self_id,
                      nickname=await _fetch_user_nickname(client, user_id, group_id),
                      at_bot=bool(group_id))


async def handle_message_event(event, client):
    app_context.napcat_client = client
    token = app_context.set_active_config(config_for_client(client))
    try:
        await _dispatch_message_event(event, client)
    finally:
        app_context.reset_active_config(token)


async def _dispatch_message_event(event, client):
    if isinstance(event, (FriendPokeEvent, GroupPokeEvent)):
        event = await _poke_to_message_event(event, client)
        if event is None:
            return
    info = _extract_event_info(event, client)
    if info is None:
        return
    incoming_mid = str(getattr(event, "message_id", "") or "")
    if incoming_mid:
        _SESSION_LAST_INCOMING[info["session_id"]] = incoming_mid
    if app_context.sender is not None:
        app_context.sender.remember_session(info.get("session_id"), client)
    if info.get("silent"):
        silent_session = info["session_id"]
        silent_sender = getattr(event.sender, "user_id", None) or "0"
        silent_name = getattr(event.sender, "nickname", None) or ""
        silent_lock = app_context._SESSION_LOCKS.get(silent_session)
        if silent_lock is not None and silent_lock.locked():
            _queue_unaddressed_message(silent_session, info.get("text", ""),
                                       bool(info.get("has_image")),
                                       silent_sender, silent_name,
                                       bool(info.get("has_voice")),
                                       bool(info.get("forward_ids")))
        else:
            _remember_unaddressed_message(silent_session, info.get("text", ""),
                                          bool(info.get("has_image")),
                                          silent_sender, silent_name,
                                          bool(info.get("has_voice")),
                                          bool(info.get("forward_ids")))
        return
    session_id = info["session_id"]
    lock = app_context._SESSION_LOCKS.setdefault(session_id, asyncio.Lock())
    if lock.locked():
        pending = _SESSION_PENDING.get(session_id)
        if pending is None:
            _SESSION_PENDING[session_id] = info
            _touch_pending(session_id)
            print(f"会话 {session_id} 正在处理上一条消息，本条已排队，处理完后立即跟进。")
        elif pending.get("sender_id") == info.get("sender_id"):
            _SESSION_PENDING[session_id] = _merge_event_info(pending, info)
            _touch_pending(session_id)
            print(f"会话 {session_id} 连发消息，已合并待处理内容。")
        else:
            async def process_after_current():
                async with lock:
                    await _process_message_event(info["event"], info["client"],
                                                 _merged_payload(info))
            asyncio.create_task(process_after_current())
            return
        _spawn_drainer(session_id, lock)
        return
    async with lock:
        try:
            _SESSION_PENDING.setdefault(session_id, info)
            _touch_pending(session_id)
            while True:
                pending = _SESSION_PENDING.get(session_id)
                if pending is None:
                    break
                idle = time.monotonic() - float(pending.get("last_at") or 0)
                if idle < _COALESCE_WINDOW:
                    await asyncio.sleep(_COALESCE_WINDOW - idle)
                    continue
                if await _smart_reply_hold(pending):
                    await asyncio.sleep(_SMART_REPLY_POLL_SECONDS)
                    continue
                pending = _SESSION_PENDING.pop(session_id, None)
                if pending is None:
                    break
                await _process_message_event(pending["event"], pending["client"],
                                             _merged_payload(pending))
        except Exception as e:
            print(f"会话 {session_id} 消息处理异常: {type(e).__name__}: {e}")
        finally:
            _SESSION_PENDING.pop(session_id, None)


def _mute_candidates(at_ids, quoted_user_id, speaker_id, bot_id,
                     recent_other_id="", self_request=False) -> list:
    candidates = [str(q) for q in (at_ids or [])
                  if str(q).isdigit() and str(q) != str(bot_id)]
    for extra in (quoted_user_id, recent_other_id):
        value = str(extra or "")
        if value.isdigit() and value != str(bot_id) and value not in candidates:
            candidates.append(value)
    requester = str(speaker_id or "")
    if self_request and requester.isdigit() and requester != str(bot_id) \
            and requester not in candidates:
        candidates.append(requester)
    return candidates


def _sulk_active(meta: dict, character_key: str) -> float:
    """该角色在本会话是否正冷处理（晾人）；返回到期时间戳，不在冷处理返回 0。"""
    if not bool(app_context.active_config().get("sulk_enabled", True)):
        return 0.0
    sulks = meta.get("sulk_until")
    if not isinstance(sulks, dict):
        return 0.0
    key = str(character_key or "")
    if not key:
        return 0.0
    try:
        until = float(sulks.get(key) or 0)
    except (TypeError, ValueError):
        until = 0.0
    if until > time.time():
        return until
    if key in sulks:
        sulks.pop(key, None)
        if not sulks:
            meta.pop("sulk_until", None)
    return 0.0


def _sulk_seconds_of(sentence: dict) -> int:
    config = app_context.active_config()
    try:
        default_s = max(1, int(config.get("sulk_default_seconds", 180) or 180))
    except (TypeError, ValueError):
        default_s = 180
    try:
        lo = max(1, int(config.get("sulk_min_seconds", 60) or 60))
    except (TypeError, ValueError):
        lo = 60
    try:
        hi = max(lo, int(config.get("sulk_max_seconds", 900) or 900))
    except (TypeError, ValueError):
        hi = 900
    try:
        seconds = int(float(str(sentence.get("sulk_seconds") or "").strip()))
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        seconds = default_s
    return min(hi, max(lo, seconds))


async def _send_sulk_sticker_later(role: dict, ctx, session_type: str, target_id,
                                   session_id: str, seconds: int):
    # 冷处理到点后补发一个表情：装死逃跑的收尾（过几分钟再发个生气的表情）。
    await asyncio.sleep(max(0, seconds))
    config = app_context.active_config()
    if app_context.sticker_mgr is None or not config.get("stickers_enabled", False) \
            or not config.get("sulk_sticker_enabled", True):
        return
    client = app_context.sender._active_client()
    if client is not None and not client_supports(client, "sticker"):
        return
    emotions = get_role_emotions(role)
    want = str(config.get("sulk_sticker_emotion", "生气") or "生气")
    emotion = resolve_emotion_key(want, list(emotions.keys())) or want
    sticker = await app_context.sticker_mgr.pick_async(ctx, emotion, "")
    if not sticker:
        print("冷处理：到点了，但没挑到可发的表情包，收尾跳过。")
        return
    speaker = str(role.get("character_name")
                  or (ctx.character_key if ctx is not None else "") or "角色")
    if not await app_context.sender.send_text(session_type, target_id, "", sticker=sticker):
        print("冷处理：收尾表情发送失败。")
        return
    try:
        spath = Path(str(sticker))
        if spath.is_file():
            mem = role_memory(role) or app_context.memory_manager
            data = mem.load_session_data(session_id)
            sname = _cache_image_bytes(spath.read_bytes(), spath.suffix or ".gif")
            if sname:
                data.setdefault("history", []).append({
                    "role": "assistant", "content": f"[表情包: {spath.stem[:40]}]",
                    "image": sname, "timestamp": time.time(),
                    "speaker": speaker, "emotion": ""})
                mem.save_session_data(session_id, data)
    except Exception as e:
        print(f"冷处理收尾表情记入历史失败（忽略）: {type(e).__name__}: {e}")
    print(f"冷处理：到点，{speaker} 给 [{target_id}] 补发了一个表情（{emotion}）。")


# 挂起的「等不到回话就先开口」任务：{session_id|character_key: asyncio.Task}
_SESSION_FOLLOWUP: Dict[str, asyncio.Task] = {}


def _followup_seconds_of(sentences) -> int:
    """从本轮句子里取 followup_seconds（0 表示没挂）；按配置钳到上下限。"""
    config = app_context.active_config()
    try:
        lo = max(1, int(config.get("followup_min_seconds", 30) or 30))
    except (TypeError, ValueError):
        lo = 30
    try:
        hi = max(lo, int(config.get("followup_max_seconds", 900) or 900))
    except (TypeError, ValueError):
        hi = 900
    raw = 0
    for sentence in sentences or []:
        if not isinstance(sentence, dict):
            continue
        try:
            raw = int(float(str(sentence.get("followup_seconds") or "").strip()))
        except (TypeError, ValueError):
            raw = 0
        if raw > 0:
            break
    if raw <= 0:
        return 0
    return min(hi, max(lo, raw))


def _cancel_session_followups(session_id: str, why: str = "用户已回话") -> None:
    prefix = f"{session_id}|"
    stale = [k for k in _SESSION_FOLLOWUP if k.startswith(prefix)]
    for key in stale:
        task = _SESSION_FOLLOWUP.pop(key)
        if not task.done():
            task.cancel()
    if stale:
        print(f"先开口：会话 {session_id} {why}，挂起的主动开口已取消（{len(stale)} 个）。")


def _schedule_speak_first(role: dict, session_type: str, target_id: str,
                          session_id: str, seconds: int) -> None:
    key = f"{session_id}|{role.get('character_key', '')}"
    prev = _SESSION_FOLLOWUP.pop(key, None)
    if prev is not None and not prev.done():
        prev.cancel()
    _SESSION_FOLLOWUP[key] = _spawn(_speak_first_after_silence(
        role, session_type, target_id, session_id, seconds, time.time()))
    print(f"先开口：{role.get('character_name') or role.get('character_key')} 说完这轮，"
          f"等 [{target_id}] {seconds} 秒内回话，没回就由她先开口。")


async def _speak_first_after_silence(role: dict, session_type: str, target_id: str,
                                     session_id: str, seconds: int, sent_at: float):
    # 等不到回话就先开口：本轮挂了 followup_seconds，时限内对方一句话都没回，
    # 就由角色主动打破沉默（嘴硬完绷不住、自己找台阶）。
    await asyncio.sleep(max(1, seconds))
    key = f"{session_id}|{role.get('character_key', '')}"
    if _SESSION_FOLLOWUP.get(key) is not asyncio.current_task():
        return  # 已被更新一轮的约定替换
    _SESSION_FOLLOWUP.pop(key, None)
    config = app_context.active_config()
    if not config.get("followup_enabled", True):
        return
    if app_context.last_user_activity.get(session_id, 0) >= sent_at:
        return  # 等待期内对方回过话（正常应已取消，这里兜底）
    client = app_context.sender._active_client()
    if client is None:
        print(f"先开口：[{target_id}] 当前没有可用接入方式，本轮放弃。")
        return
    mem = role_memory(role) or app_context.memory_manager
    try:
        data = mem.load_session_data(session_id)
    except Exception as e:
        print(f"先开口：读取会话 {session_id} 失败（放弃）: {type(e).__name__}: {e}")
        return
    history = data.get("history", [])
    meta = data.get("meta", {})
    ctx = reply_ctx_of(role)
    character_key = str(ctx.character_key or "")
    # 她主动开口就说明先绷不住了，还挂着的冷处理随之作废
    sulks = meta.get("sulk_until")
    if isinstance(sulks, dict) and character_key in sulks:
        sulks.pop(character_key, None)
        if not sulks:
            meta.pop("sulk_until", None)
        print(f"先开口：{ctx.character_name or character_key} 绷不住先找台阶，冷处理提前结束。")
    emotions = get_role_emotions(role)
    last_user_id = ""
    for msg in reversed(history):
        if isinstance(msg, dict) and msg.get("role") == "user":
            last_user_id = str(msg.get("sender_id") or "")
            break
    trigger = (
        f"（系统提示：你上一轮说完之后，对方已经 {seconds} 秒没有任何回话。"
        "现在由你先开口打破沉默：按你此刻的心情自然地说——"
        "可以是绷不住嘴硬找台阶、装作没事换个话题、或者小声服软，"
        "不要提「系统」「提示」这类字眼，也不要重复上一轮说过的话。）"
    )
    try:
        budget = max(90.0, float(config.get("llm_timeout", 120) or 120) + 60.0)
    except (TypeError, ValueError):
        budget = 180.0
    try:
        reply = await asyncio.wait_for(
            generate_reply(ctx, emotions, trigger, history, None, [],
                           last_user_id, session_id=session_id,
                           speaker_labels=build_speaker_labels(history),
                           wants_voice=voice_wanted(config, session_type, client)),
            timeout=budget)
    except Exception as e:
        print(f"先开口：生成失败（放弃）: {type(e).__name__}: {e}")
        return
    if not reply or not reply.get("sentences"):
        print("先开口：这轮没生成出可用台词，放弃。")
        return
    use_voice = bool(config.get("tts_reply_enabled", True)) \
        and client_supports(client, "voice")
    if use_voice:
        await ensure_tts_service(config)
    send_result = await app_context.sender.send_reply(
        session_type, target_id, reply["sentences"], emotions, ctx, use_voice=use_voice)
    speaker = str(role.get("character_name") or character_key or "角色")
    zh_text = strip_mention_placeholder(
        "".join(str(s.get("zh") or s.get("display") or "")
                for s in reply["sentences"]))
    parts = [strip_mention_placeholder(t) for t in (send_result.get("sent_texts") or [])]
    parts = [t for t in parts if t.strip()]
    if len(parts) <= 1:
        parts = [zh_text]
    for index, part in enumerate(parts):
        entry = {"role": "assistant", "content": part, "timestamp": time.time(),
                 "speaker": speaker,
                 "emotion": reply["sentences"][0].get("emotion", "")}
        if index == len(parts) - 1:
            entry["action_notes"] = "先开口打破沉默"
        history.append(entry)
    data["history"] = history
    data["meta"] = meta
    mem.save_session_data(session_id, data)
    print(f"先开口：{speaker} 等了 {seconds} 秒没等到回话，主动打破沉默 → {zh_text[:60]}")


async def _process_message_event(event, client, merged: Optional[dict] = None):
    app_context.napcat_client = client

    is_private = isinstance(event, PrivateMessageEvent)

    if is_private:
        session_type = "private"
        target_id = event.user_id
        sender_id = event.user_id
        session_id = f"private_{event.user_id}"
    else:
        group_id = event.group_id
        sender_id = getattr(event.sender, "user_id", None) or "0"
        session_type = "group"
        target_id = group_id
        session_id = f"group_{group_id}"
        if app_context.active_config().get("isolated_session", False):
            session_id = f"group_{group_id}_{sender_id}"

    sender_name = getattr(event.sender, "nickname", None) or str(sender_id)
    if app_context.profile_mgr is not None and app_context.active_config().get("profiles_enabled", False):
        app_context.profile_mgr.remember_nickname(sender_id, getattr(event.sender, "nickname", ""))
    incoming_message_id = getattr(event, "message_id", None)
    sender_role = str(getattr(getattr(event, "sender", None), "role", "") or "")
    user_text = ""
    has_image = False
    image_urls = []
    image_file_ids = {}
    has_voice = False
    voice_sources = []
    voice_file_ids = {}
    at_bot = False
    at_ids = []
    at_names = {}
    reply_seg = None
    forward_ids = []
    quoted = None
    if merged is not None:
        user_text = merged.get("text", "")
        has_image = merged.get("has_image", False)
        image_urls = list(merged.get("image_urls", []))
        image_file_ids = dict(merged.get("image_file_ids", {}))
        has_voice = bool(merged.get("has_voice", False))
        voice_sources = list(merged.get("voice_sources", []))
        voice_file_ids = dict(merged.get("voice_file_ids", {}))
        at_ids = list(merged.get("at_ids", []))
        at_bot = merged.get("at_bot", False)
        reply_seg = merged.get("reply_seg")
        forward_ids = list(merged.get("forward_ids", []))
    else:
        for seg in event.message:
            if isinstance(seg, Text):
                user_text += seg.text
            elif isinstance(seg, Image):
                has_image = True
                chosen = pick_media_source(seg)
                if chosen and chosen not in image_urls:
                    # 直接将图片 URL 加入，不下载
                    image_urls.append(chosen)
                    fid = getattr(seg, "file", None)
                    if fid:
                        image_file_ids[chosen] = str(fid)
            elif isinstance(seg, Record):
                has_voice = True
                chosen = pick_media_source(seg, "语音")
                if chosen and chosen not in voice_sources:
                    voice_sources.append(chosen)
                    fid = getattr(seg, "file", None)
                    if fid:
                        voice_file_ids[chosen] = str(fid)
            elif isinstance(seg, At):
                qq = str(seg.qq)
                at_ids.append(qq)
                if qq == str(client.self_id):
                    at_bot = True
                    user_text += "[@本机器人]"
                else:
                    user_text += f"[@{qq}]"
            elif isinstance(seg, Reply):
                reply_seg = seg
            elif isinstance(seg, Forward):
                fid = str(getattr(seg, "id", "") or "")
                if fid and fid not in forward_ids:
                    forward_ids.append(fid)

    own_text = user_text
    if forward_ids:
        forwarded = await fetch_forward_text(client, forward_ids)
        if forwarded:
            for furl in forwarded["image_urls"]:
                if furl not in image_urls:
                    image_urls.append(furl)
                    has_image = True
                    ffid = forwarded["image_file_ids"].get(furl)
                    if ffid:
                        image_file_ids[furl] = ffid
            fwd_note = "（转发的聊天记录）"
            if forwarded["text"]:
                fwd_note = f"{fwd_note}\n{forwarded['text']}"
            user_text = f"{fwd_note}\n{user_text}" if user_text else fwd_note

    if reply_seg is not None:
        quoted = await fetch_quoted_context(client, reply_seg)
        if quoted:
            if quoted["user_id"] and quoted["user_id"] == str(client.self_id):
                at_bot = True
            for qurl in quoted.get("image_urls", []):
                if qurl not in image_urls:
                    image_urls.append(qurl)
                    has_image = True
                    qfid = quoted.get("image_file_ids", {}).get(qurl)
                    if qfid:
                        image_file_ids[qurl] = qfid
            quote_note = f"（回复 {quoted['who'] or '某人'} 的消息：{quoted['text']}）"
            user_text = f"{quote_note}\n{user_text}" if user_text else quote_note

    if not is_private and at_ids:
        for qq in at_ids:
            if qq == str(client.self_id):
                continue
            name = await _fetch_member_name(client, group_id, qq)
            if name:
                at_names[qq] = name
                user_text = user_text.replace(f"[@{qq}]", f"[@{name}(QQ:{qq})]")
            else:
                at_names[qq] = ""

    if not is_private and not at_bot:
        if app_context.active_config().get("only_private", False):
            return
        if app_context.active_config().get("group_need_at", True):
            _remember_unaddressed_message(session_id, user_text, has_image,
                                          sender_id, sender_name, has_voice)
            return
    if not user_text and not has_image and not has_voice:
        return
    if not _session_whitelisted(target_id):
        print(f"白名单：会话 {session_id}（{target_id}）不在白名单内，已忽略。")
        return
    if not _allow_message(session_id):
        print(f"防刷屏：会话 {session_id} 短时间内消息过多，本条已忽略（可在配置中调整 anti_spam_*）。")
        return

    if has_voice:
        voice_text = await transcribe_voice_message(client, voice_sources, voice_file_ids)
        if voice_text:
            user_text = f"{user_text} {voice_text}".strip()
            own_text = f"{own_text} {voice_text}".strip()
        else:
            print(f"会话 {session_id}：这条语音没转出文字（可在「配置文件 → 更多 → 语音识别」换识别通道）。")
            if not user_text and not has_image:
                return

    print(f"收到{'私聊' if is_private else '群聊'} [{target_id}] 来自 [{sender_id}]: {user_text}")
    app_context.last_interaction[session_id] = time.time()
    app_context.last_user_activity[session_id] = app_context.last_interaction[session_id]
    _cancel_session_followups(session_id)
    app_context.proactive_pending.pop(session_id, None)
    if session_id in app_context.proactive_awaiting:
        app_context.proactive_awaiting.discard(session_id)
        print(f"主动消息：用户已在 {session_id} 回复，恢复主动开口资格。")
    save_proactive_state()

    mem = memory_for_session(session_id, client) or app_context.memory_manager
    mem.migrate_legacy_memory(session_id)
    data = mem.load_session_data(session_id)
    history = data.get("history", [])
    meta = data.get("meta", {})

    handled, plugin_reply = _dispatch_plugin_command(user_text, session_type, target_id,
                                                     session_id, sender_id,
                                                     sender_name, event)
    if handled:
        if plugin_reply:
            await app_context.sender.send_text(session_type, target_id, str(plugin_reply))
        return
    # ------------------------------------------------------
    meta["user_msg_count"] = int(meta.get("user_msg_count", 0)) + 1
    meta["last_user_text"] = user_text[:200]
    data["meta"] = meta
    
    history_text = (f"{user_text} [图片]".strip() if has_image
                    else user_text) or "[图片]"

    images_from_pending = False
    if has_image:
        _remember_pending_image(session_id, image_urls, image_file_ids, user_text)
    else:
        pending_img = _take_pending_image(session_id, user_text)
        if pending_img:
            image_urls = list(pending_img.get("urls", []))
            image_file_ids = dict(pending_img.get("file_ids", {}))
            has_image = images_from_pending = bool(image_urls)
            if has_image:
                for prev_msg in reversed(history):
                    if prev_msg.get("role") == "user":
                        prev = str(prev_msg.get("content", ""))
                        if "[图片" not in prev:
                            prev_msg["content"] = (f"{prev} [上一个附件是图片]").strip()
                        break
                print(f"会话 {session_id}：本条消息指向刚才那张未被真正看过的图片，"
                      "已把识图模型叫回来重新识图（修复回复节奏错乱）。")
    # ---------------------------------------------------------------------

    history.append({
        "role": "user",
        "content": history_text,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "timestamp": time.time()
    })

    # 待办提取（后台异步，不阻塞回复）
    if app_context.active_config().get("todo_enabled", False) and app_context.todo_mgr:
        mode = app_context.active_config().get("todo_extract_mode", "regex")
        if mode == "regex":
            found = app_context.todo_mgr.extract_sync(user_text)
            for content, remind_ts in found:
                app_context.todo_mgr.add_todo(content, remind_ts, session_type, session_id, sender_id, source="regex")
        elif mode == "llm":
            recent_lines = []
            for msg in history[:-1][-6:]:
                who = "主人" if msg.get("role") == "user" else "你"
                content = str(msg.get("content", "")).strip()
                if content:
                    recent_lines.append(f"{who}：{content[:100]}")
            recent_lines.append(f"主人：{user_text}")
            _spawn(app_context.todo_mgr.extract_and_add(get_active_ctx(), user_text, session_type,
                                            session_id, sender_id,
                                            recent_context=recent_lines))

    source_role_key = app_context.ROLE_CONNECTIONS.get(id(client), "")
    target_roles = resolve_target_roles(user_text, is_private, source_role_key)
    max_total = max(1, int(app_context.active_config().get("multi_role_max_total", 6)))
    total_replies = 0
    first_reply_done = False
    replied: Dict = {}
    mute_state: Dict = {"done_ids": []}

    def _dispatch_reply_done(info: dict) -> None:
        rt = _plugin_runtime()
        if rt is None:
            return
        try:
            rt.dispatch_reply_done(info)
        except Exception as e:
            print(f"[插件] 回复完成钩子派发失败: {type(e).__name__}: {e}")


    async def process_role_reply(role: dict, trigger_text: str, gate_reply: bool = False,
                                 own_text: str = "", overrides: Optional[dict] = None) -> bool:
        source_client = client if client is not None and client is not app_context.sender.client else None
        with app_context.sender.using_client(source_client or app_context.sender.client_for(role)):
            nonlocal total_replies, first_reply_done
            if total_replies >= max_total:
                return False
            ctx = reply_ctx_of(role, overrides)
            sulk_until = _sulk_active(meta, ctx.character_key)
            if sulk_until:
                print(f"冷处理：{ctx.character_name or ctx.character_key} 正在把 "
                      f"[{target_id}] 晾着（还剩 {int(sulk_until - time.time())} 秒），"
                      "本条已读不回。")
                return False
            emotions = get_role_emotions(role)
            extra_parts = []
            jealousy_rival = ""
            speaker_labels = build_speaker_labels(history)
            # 会以为她只是在嘴上答应
            system_receipts: List[dict] = []
            mute_done_ids: List[str] = [str(q) for q in ((overrides or {}).get("mute_done_ids") or [])]
            recall_mode = recall_request_kind(own_text) if gate_reply else ""
            # 是它刚发出的新话，看着像撤错了对象
            send_recall_request = RECALL_DENY if recall_mode == "prev" else recall_mode
            quoted_mid = str((quoted or {}).get("message_id") or "")
            recall_other_id = quoted_mid or str(incoming_message_id or "")
            self_mute = wants_self_mute(own_text) if gate_reply else False
            recent_other_id = next(
                (q for q in recent_user_ids(history[-10:], limit=4,
                                            exclude=str(client.self_id))
                 if q != str(sender_id)), "")
            mute_ids = _mute_candidates(at_ids, (quoted or {}).get("user_id"),
                                        sender_id, client.self_id,
                                        recent_other_id, self_mute)
            if recall_mode == "prev":
                quoted_own = str((quoted or {}).get("user_id") or "") == str(client.self_id)
                allowed = False
                if quoted_mid:
                    allowed = quoted_own or (not is_private and await can_recall_others(
                        client, target_id, sender_id, sender_role))
                    done = await app_context.sender.recall_message(
                        session_type, target_id, quoted_mid) if allowed else 0
                else:
                    done = await app_context.sender.recall_recent(session_type, target_id)
                print(f"撤回：主人要求撤回上一条，已撤回 {done} 条消息。")
                system_receipts.append({
                    "action": "撤回别人的消息" if (quoted_mid and not quoted_own) else "撤回",
                    "ok": bool(done), "count": done or None,
                    "reason": "" if done else (
                        "denied" if (quoted_mid and not allowed) else "error"),
                    "at": time.time()})
                if done:
                    extra_parts.append(
                        "【本轮动作】主人这条消息是在要求撤回消息，系统已经撤掉了；"
                        "你只要自然地回一句就好，不要在台词里写「（撤回）」这类动作描述。")
                else:
                    why = ("：主人没有撤回别人消息的管理权限"
                           if quoted_mid and not allowed else "")
                    extra_parts.append(
                        f"【本轮动作】主人这条消息是在要求撤回消息，但系统这次没撤成{why}；"
                        "你只要如实回一句就好，不要说已经撤掉了，也不要自己猜原因"
                        "（别说消息太旧、超时这类话）。")
            elif recall_mode:
                extra_parts.append(
                    "【本轮动作】主人这条消息是在要求撤回消息，系统已经处理；"
                    "你只要自然地回一句就好，不要在台词里写「（撤回）」这类动作描述。")
            at_targets = [str(q) for q in at_ids
                          if str(q) != str(client.self_id)
                          and (str(q) != str(sender_id) or self_mute)]
            if gate_reply and not is_private \
                    and app_context.active_config().get("mute_enabled", True) \
                    and wants_mute_request(own_text):
                if self_mute:
                    targets = [str(sender_id)]
                elif at_targets:
                    targets = at_targets
                else:
                    targets = [recent_other_id] if recent_other_id else []
                if targets:
                    mute_receipts = [await app_context.sender.mute_receipt(
                        session_type, target_id, target,
                        mute_request_seconds(own_text), sender_id, sender_role)
                        for target in targets]
                    system_receipts.extend(mute_receipts)
                    mute_done_ids = [str(t) for t in targets]
                    mute_state["done_ids"] = list(mute_done_ids)
                    outcome = "；".join(
                        f"{speaker_labels.get(str(t)) or f'QQ:{t}'} "
                        + ("已禁言" if r.get("ok")
                           else f"没禁成（{_ACTION_FAIL_REASONS.get(str(r.get('reason') or ''), '失败')}）")
                        for t, r in zip(targets, mute_receipts))
                    print(f"禁言：主人这条消息在要求禁言，结果 {outcome}。")
                    if all(r.get("ok") for r in mute_receipts):
                        extra_parts.append(
                            f"【本轮动作】主人这条消息是在要求禁言，系统已经把 "
                            f"{outcome}；你只要自然地回一句就好，"
                            "不要在台词里宣布结果"
                            "（「已成功禁言…」这类话一律不要写）。")
                    else:
                        extra_parts.append(
                            f"【本轮动作】主人这条消息是在要求禁言，结果：{outcome}；"
                            "你只要如实回一句就好，禁成的可以说已处理，"
                            "没禁成的那个人要如实说明没禁成，"
                            "不要把没禁成的也说成已经禁言了，也不要自己猜原因。")
                else:
                    extra_parts.append(
                        "【本轮动作】主人这条消息是在要求禁言，但没说清是谁；"
                        "你只要反问一句要禁言谁就好，不要自己挑一个人禁言。")
            if gate_reply:
                if app_context.encounter_mgr is not None:
                    adventure = app_context.encounter_mgr.take(ctx.character_key,
                                                   time.strftime("%Y-%m-%d"))
                    if adventure:
                        extra_parts.append(
                            f"【今日奇遇】你今天遇到了这样一件事：{adventure} "
                            "自然地找机会把它讲给对方听，可以顺势展开成一段小剧情。")
                        print(f"奇遇：{ctx.character_key} 今天的奇遇已带入本轮回复。")
                if _to_float(app_context.active_config().get("mood_jealousy_penalty", 0), 0) != 0 \
                        and bool(app_context.active_config().get("mood_enabled", True)):
                    jealousy_rival = _detect_rival_mention(trigger_text, ctx.character_key)
                    if jealousy_rival:
                        extra_parts.append(
                            f"【吃醋】对方这句话里提到了别的角色「{jealousy_rival}」。"
                            "你在意这件事，可以吃醋、试探或闹小脾气，怎么表现由你的性格决定。")
                        print(f"吃醋：{ctx.character_key} 注意到 {sender_id} 提到了「{jealousy_rival}」。")
            allowed_at_ids = set() if is_private else {MENTION_ALL_ID}
            if not is_private:
                allowed_at_ids.update(q for q in at_ids if q != str(client.self_id))
            if not is_private:
                current_label = speaker_labels.get(str(sender_id), "")
                extra_parts.append(
                    f"【当前发言者】{current_label or '本轮用户'}（QQ:{sender_id}）；"
                    f"本轮消息正文是此人的话。昵称仅作展示，不用于推断身份；"
                    f"历史中其他用户标签（除 {current_label or '本轮用户'} 外）代表不同群成员，"
                    "不得把他们说过的话、身份、关系、称呼或观点归给当前发言者。"
                    "群聊摘要与话题只作背景参考，不能据此判断当前发言者的身份或关系。"
                    "问题里的预设指代若无法由带标签的原始消息确认，先向当前发言者澄清，不要顺着说。"
                    "引用内容只属于引用消息的发送者；被@成员也不等于当前发言者。")
                mention_parts = []
                for q in at_ids:
                    nm = at_names.get(q)
                    mention_parts.append(f"{nm}(QQ:{q})" if nm else f"QQ:{q}")
                who = "、".join(mention_parts) if mention_parts else "无（按提及的名字触发）"
                if at_bot:
                    extra_parts.append(
                        f"【@对象】本条消息@了：{who}（其中包含你：本机器人/当前角色）。"
                        "你是被@的机器人，消息里提到的其他人/QQ号都是别的群成员，不是你本人，"
                        "也不是给你发消息的用户；请区分“发消息的用户”和“被@的其他成员”，只按自己的身份回应。")
                else:
                    extra_parts.append(
                        f"【@对象】本条消息@了：{who}（都不是你）。"
                        "被@的是其他群成员，不是你本人，也不是给你发消息的用户；"
                        "只有消息明确提到你的名字时才由你回应，不要替其他被@的人作答。")
                ident = identity_note(history, sender_id)
                if ident:
                    extra_parts.append(ident)
                    print("身份记录：已重申本会话确立的关系。")
                if wants_mention_request(user_text):
                    targets = recent_user_ids(history, exclude=str(client.self_id))
                    listed = "、".join(f"{speaker_labels.get(q) or '群成员'}(QQ:{q})"
                                       for q in targets)
                    extra_parts.append(
                        f"【可@成员】{listed or '无'}。用户本条消息明确要求你@人："
                        "请在第一个句子对象里填 mention_ids 数组、原样写要叫的那个人的 QQ 号，"
                        f"并在正文里想@他的那个位置写一个 {MENTION_PLACEHOLDER} 占位符，"
                        "由系统在该位置真正@他，不要一律写在正文最前面；"
                        "要@全体成员时在正文里写「@@所有人」，系统会把它换成真正的@全体成员；"
                        "不要用「那个家伙」「刚才那位」之类的描述代替。")
                    allowed_at_ids.update(targets)
                if wants_quote_request(user_text):
                    extra_parts.append(
                        "【引用要求】用户本条消息明确要求引用消息：请在第一个句子对象里加 "
                        "reply_to: true，由系统引用本条消息，不要只在台词里口头答应。")
            repeat_requested = wants_repeat_request(user_text)
            if repeat_requested:
                extra_parts.append(
                    "【本轮要求】主人本条消息明确要求你复述/重复他指定的内容："
                    "按他的要求原样说出来即可，【禁止复读】这一轮不适用。")
            use_history = history
            if app_context.active_config().get("summary_enabled", False) and meta.get("summary"):
                summary_text = background_ready(meta["summary"])
                if not summary_text:
                    print("会话摘要疑似角色口吻（旧版本生成），本轮不注入，等下次重新整理。")
                else:
                    keep = _summary_keep_count()
                    use_history = history[-keep:]
                    if summary_text != str(meta["summary"]).strip():
                        print("会话摘要含角色台词，已只注入其余客观内容（更早的对话不会丢）。")
                    extra_parts.append(
                        f"【早期对话摘要（客观背景，不是台词）】{summary_text}"
                        "（身份、称呼与关系只以带说话人标签的原始消息为准）")
            if app_context.active_config().get("dynamic_context_enabled", False) and meta.get("topic"):
                topic_text = background_ready(meta["topic"])
                if not topic_text:
                    print("会话话题疑似角色口吻（旧版本生成），本轮不注入，等下次重新整理。")
                else:
                    extra_parts.append(
                        f"【当前话题（客观背景，不是台词）】{topic_text}")
            diary_note = _diary_recognition_note(ctx, user_text)
            if diary_note:
                extra_parts.append(diary_note)
            if app_context.profile_mgr and app_context.active_config().get("profiles_enabled", False):
                p = app_context.profile_mgr.build_injection(sender_id)
                if p:
                    extra_parts.append(p)
            if app_context.lexicon_mgr is not None:
                lex = app_context.lexicon_mgr.build_injection()
                if lex:
                    extra_parts.append(lex)
            if app_context.rag_mgr and app_context.active_config().get("rag_enabled", False):
                rc = await app_context.rag_mgr.build_context(user_text)
                if rc:
                    extra_parts.append(rc)
            try:
                repeat_rounds = max(1, int(app_context.active_config().get("repeat_guard_rounds", 3) or 3))
            except (TypeError, ValueError):
                repeat_rounds = 3
            repeat_flags = repeat_guard_flags(ctx)
            recent_replies = _recent_assistant_replies(history, repeat_rounds)
            last_reply = recent_replies[0] if recent_replies else ""
            if recent_replies and repeat_flags["compare_self"]:
                block = "\n".join(f"{i + 1}. {r[:200]}" for i, r in enumerate(recent_replies))
                extra_parts.append(
                    f"【禁止重复】你最近 {len(recent_replies)} 轮已经说过下面这些话：\n{block}\n"
                    "本次回复必须与上面每一句都明显不同：句子结构、用词、切入角度、"
                    "举例与收尾方式都要换新的；严禁把其中任何一句原样或换个说法再说一遍，"
                    "也严禁只是把前面轮次的话重新拼一遍。")

            image_sources = image_urls
            if has_image:
                image_sources = await refresh_image_urls(client, image_urls, image_file_ids)

            gen_budget = max(90.0, float(ctx.get("llm_timeout", 120) or 120) + 60.0)

            mood_user = "" if is_private else str(sender_id)
            verdict = None
            if gate_reply and app_context.mood_mgr is not None \
                    and (judge_enabled(ctx) or mood_enabled(ctx)
                         or fixed_reply_probability(ctx) is not None):
                reply_floor = app_context.affection_mgr.stage_reply_floor(
                    ctx.character_key, sender_id, session_id) \
                    if app_context.affection_mgr is not None else 0.0
                try:
                    verdict = await asyncio.wait_for(
                        judge_and_decide(ctx, app_context.mood_mgr, session_id, trigger_text,
                                         history, user_id=mood_user,
                                         reply_floor=reply_floor), timeout=30)
                except asyncio.TimeoutError:
                    print("回复审判超时（30s），本轮跳过审判直接回复。")
                    verdict = None
                if _image_reply_override(verdict, has_image):
                    print(f"回复审判：本条带图，{ctx.character_name or ctx.character_key} "
                          f"的「无需回复」判定不生效，仍由角色回复（{_judge_mood_text(verdict)}）")
                    verdict["should_reply"] = True
                if verdict is not None and not verdict["should_reply"]:
                    print(f"回复审判：{ctx.character_name or ctx.character_key} 决定不回复"
                          f"（{_judge_mood_text(verdict)}，"
                          f"回复概率 {verdict['probability']:.2f}，"
                          f"{_judge_gate_cause(verdict)}）")
                    if has_image and image_sources:
                        try:
                            pending_reply = await asyncio.wait_for(
                                generate_reply(ctx, emotions, trigger_text, use_history,
                                               image_sources, list(extra_parts), sender_id,
                                               session_id=session_id, describe_only=True,
                                               speaker_labels=speaker_labels),
                                timeout=gen_budget)
                        except Exception as e:
                            pending_reply = None
                            print(f"审判未回复时补记画面描述失败（忽略）: {type(e).__name__}: {e}")
                        desc = str((pending_reply or {}).get("description", "") or "").strip()
                        if desc:
                            _backfill_image_description(history, desc)
                            print(f"审判未回复：画面描述已写入历史，供主人下一条追问使用 → {desc[:60]}")
                        _spawn_sticker_capture(ctx, pending_reply, image_sources)
                    _log_mood_commit(commit_mood(ctx, app_context.mood_mgr, session_id, verdict, mood_user),
                                     verdict)
                    return False

            if app_context.mood_mgr is not None:
                mood_now = verdict["mood"] if verdict is not None \
                    else current_mood(ctx, app_context.mood_mgr, session_id, mood_user)
                tier, mood_note = mood_style(ctx, mood_now)
                if mood_note:
                    extra_parts.append(mood_note)
                    print(f"心情影响语气：心情值 {mood_now:.0f} → {tier}档，"
                          "本轮回复按该档位的语气与篇幅约束生成。")

            if app_context.affection_mgr is not None and affection_enabled(ctx):
                aff_label = "" if is_private else speaker_labels.get(str(sender_id), "")
                aff_note = app_context.affection_mgr.build_note(ctx, sender_id, label=aff_label,
                                                    session_id=session_id)
                if aff_note:
                    extra_parts.append(aff_note)
                    aff_now = app_context.affection_mgr.state(ctx.character_key, sender_id, session_id)
                    print(f"关系进度：与 {aff_label or sender_id} 当前为「{aff_now['stage']}」"
                          f"（好感 {aff_now['score']}/{SCORE_MAX}；亲近程度：{aff_now['band']}；"
                          f"关系性质：{aff_now['nature']}）。")

            diary_note = mood_diary_note(ctx, session_id)
            if diary_note:
                extra_parts.append(diary_note)

            if app_context.recall_mgr is not None and app_context.recall_mgr.enabled():
                topics_block = app_context.recall_mgr.open_topics_block(session_id)
                if topics_block:
                    extra_parts.append(topics_block)
                recall_block = await _take_recall_block(recall_task)
                if recall_block:
                    extra_parts.append(recall_block)

            if app_context.active_config().get("unfinished_action_enabled", True):
                extra_parts.append(ACTION_NOW_HINT)

            pushed_down = bool(incoming_message_id) and \
                _SESSION_LAST_INCOMING.get(session_id, "") not in ("", str(incoming_message_id))
            quote_reply_id = incoming_message_id if (
                incoming_message_id and (not is_private
                                         or client_supports(client, "quote_private",
                                                            default=False))) else None
            if pushed_down and quote_reply_id:
                print(f"引用：要回的那条消息已被后来的消息刷下去，"
                      f"本轮回复改为引用它（{quote_reply_id}）。")
            allowed_at_names = {q: n for q, n in at_names.items()
                                if n and q in allowed_at_ids}
            sink = None
            if app_context.active_config().get("streaming_enabled", False) and not first_reply_done:
                sink = SentenceSink(
                    session_type, target_id, emotions, ctx, recent_replies,
                    "" if has_image else user_text,
                    reply_id=quote_reply_id,
                    allowed_at_ids=list(allowed_at_ids) if not is_private else [],
                    poke_target=sender_id, recall_request=send_recall_request,
                    at_names=allowed_at_names, speaker_id=sender_id,
                    repeat_requested=repeat_requested,
                    speaker_role=sender_role, mute_ids=mute_ids,
                    recall_other_id=recall_other_id,
                    mute_done_ids=mute_done_ids,
                    force_quote=pushed_down)

            async def _generate(ctx_used, history_used, parts, on_sentence=None):
                return await asyncio.wait_for(
                    generate_reply(ctx_used, emotions, trigger_text, history_used,
                                   image_sources if has_image else None,
                                   parts, sender_id, on_sentence=on_sentence,
                                   session_id=session_id,
                                   speaker_labels=speaker_labels,
                                   wants_voice=voice_wanted(app_context.active_config(),
                                                            session_type, client)),
                    timeout=gen_budget)

            reply_started = time.time()
            timed_out = False
            try:
                reply = await _generate(ctx, use_history, extra_parts,
                                        sink.on_sentence if sink else None)
            except asyncio.TimeoutError:
                timed_out = True
                print(f"回复生成超出预算（{gen_budget:.0f}s），本轮放弃以释放会话锁。")
            except BaseException as e:
                if sink is not None:
                    await sink.abort()
                if isinstance(e, Exception) and total_replies <= 0:
                    try:
                        await app_context.sender.send_text(session_type, target_id,
                                               error_reply_text(e))
                        print(f"回复生成失败，已把中文提醒发回会话: "
                              f"{type(e).__name__}: {e}")
                    except Exception as send_error:
                        print(f"报错内容发送失败: {type(send_error).__name__}: {send_error}")
                raise
            _log_mood_commit(commit_mood(ctx, app_context.mood_mgr, session_id, verdict, mood_user), verdict)
            if timed_out:
                if sink is not None:
                    await sink.abort()
                return False

            if has_image and reply:
                early_desc = str(reply.get("description", "") or "").strip()
                if early_desc:
                    _backfill_image_description(history, early_desc)
                if image_sources:
                    await _attach_history_image(history, image_sources[0])

            tts_ms = 0.0
            if sink is not None:
                await sink.flush()
                if (not reply or not reply.get("sentences")) and sink.sent_sentences:
                    reply = {"sentences": sink.sent_sentences, "llm_ms": 0, "tool_trace": []}
                tts_ms = sink.tts_ms
            if not reply or not reply.get("sentences"):
                return False

            zh_now = "".join(s.get("zh", "") for s in reply["sentences"]).strip()
            can_retry = sink is None or sink.sent == 0
            self_ratio, self_target = (0.0, "")
            if repeat_flags["compare_self"] and not repeat_requested:
                self_ratio, self_target = _max_repeat_ratio(zh_now, recent_replies)
            user_ratio = 0.0
            if repeat_flags["compare_user"] and not repeat_requested and zh_now and user_text \
                    and not has_image \
                    and "[图片]" not in user_text \
                    and len(_norm_text(user_text)) >= _USER_ECHO_MIN_CHARS:
                user_ratio = _repeat_ratio_user(user_text, zh_now)
            print(f"相似度检查：与最近 {len(recent_replies)} 条回复最高 {self_ratio:.2f}，"
                  f"与用户本条 {user_ratio:.2f}"
                  + ("" if can_retry else "（流式内容已发送，无法打回）")
                  + ("（主人明确要求复述，本轮不判重复）" if repeat_requested else "")
                  + repeat_guard_summary(repeat_flags))
            if not repeat_flags["regen"]:
                print("防复读：整段生成后校验已关闭（repeat_guard_regen_check=false），"
                      "本次不做相似度重生成。")
            elif not repeat_guard_active(repeat_flags):
                print("防复读：比对对象全部关闭，整段校验无内容可比，跳过。")
            self_threshold, user_threshold = repeat_thresholds(ctx)
            if repeat_flags["regen"] and not repeat_requested \
                    and repeat_guard_active(repeat_flags) and can_retry \
                    and (self_ratio >= self_threshold
                         or user_ratio >= user_threshold):
                dup_is_user = user_ratio >= user_threshold and self_ratio < self_threshold
                dup_target = user_text if dup_is_user else (self_target or last_reply)
                base_ratio = max(self_ratio, user_ratio)
                dup_label = "复述了用户本条消息的原话" if dup_is_user else "与之前的回复几乎重复"
                print(f"检测到回复{dup_label}（重合率 {base_ratio:.2f}），重新生成。")
                retry_hist = use_history
                if not dup_is_user:
                    retry_hist = list(use_history)
                    while retry_hist and retry_hist[-1].get("role") == "assistant":
                        retry_hist.pop()
                retry_ctx = reply_ctx_of(role, {"temperature": _RETRY_TEMPERATURE,
                                                "temperature_max": _RETRY_TEMPERATURE_MAX})
                for attempt in range(1, 3):
                    retry_parts = list(extra_parts) + [
                        f"警告：你刚才的回复{dup_label}——“{dup_target[:120]}”。"
                        "重新生成时句子、用词、角度必须和这句话明显不同，"
                        "只回应对方话里的意图，不要照搬其中的词。"
                        + ("再换一个完全不同的切入角度。" if attempt > 1 else "")]
                    if time.time() - reply_started > 60:
                        print("回复生成耗时过长，跳过重生成以免长时间占用会话。")
                        break
                    try:
                        retried = await _generate(retry_ctx, retry_hist, retry_parts)
                    except asyncio.TimeoutError:
                        print("重生成超预算，保留原回复。")
                        break
                    if not retried or not retried.get("sentences"):
                        break
                    new_zh = "".join(s.get("zh", "") for s in retried["sentences"]).strip()
                    accept_threshold = user_threshold if dup_is_user else self_threshold
                    new_self = _max_repeat_ratio(new_zh, recent_replies)[0] \
                        if repeat_flags["compare_self"] else 0.0
                    new_user = _repeat_ratio_user(user_text, new_zh) \
                        if (repeat_flags["compare_user"] and user_text and new_zh) else 0.0
                    if dup_is_user:
                        new_ratio = max(new_self, new_user if new_zh else 1.0)
                    elif not new_zh:
                        new_ratio = 1.0
                    else:
                        new_ratio = new_self
                    print(f"重生成第 {attempt} 次，重合率 {new_ratio:.2f}")
                    if new_ratio < accept_threshold or (attempt == 2 and new_ratio < base_ratio):
                        reply = retried
                        break

            if has_image and app_context.active_config().get("image_identity_guard_enabled", True) \
                    and image_self_claim("".join(s.get("zh", "") for s in reply["sentences"])):
                print("图片身份规则：回复把用户发来的图当成了角色自己，重新生成。")
                fixed = None
                if can_retry and time.time() - reply_started <= 60:
                    claim_parts = list(extra_parts) + [IMAGE_CLAIM_WARNING]
                    try:
                        retried = await _generate(ctx, use_history, claim_parts)
                    except asyncio.TimeoutError:
                        print("图片身份重生成超预算，保留原回复。")
                        retried = None
                    if retried and retried.get("sentences") \
                            and not image_self_claim("".join(s.get("zh", "")
                                                             for s in retried["sentences"])):
                        fixed = retried
                if fixed is not None:
                    reply = fixed
                else:
                    kept = _strip_image_claims(reply["sentences"])
                    if kept and len(kept) != len(reply["sentences"]):
                        print("图片身份重生成未消除认领表述，已剔除相关句子。")
                        reply = {**reply, "sentences": kept}

            if app_context.affection_mgr is not None and affection_enabled(ctx) and verdict is not None \
                    and (verdict.get("confession") or verdict.get("acceptance")):
                said_yes = "".join(str(s.get("zh", "") or "")
                                   for s in reply.get("sentences", []))
                if not app_context.affection_mgr.can_commit(ctx.character_key, sender_id,
                                                bool(verdict.get("romance")
                                                     or verdict.get("confession")
                                                     or verdict.get("acceptance")),
                                                session_id=session_id) \
                        and looks_like_acceptance(said_yes):
                    aff_now = app_context.affection_mgr.state(ctx.character_key, sender_id, session_id)
                    print(f"关系进度：好感 {aff_now['score']}/{SCORE_MAX}"
                          f"（{aff_now['nature']}）还没到能定关系的时候却答应了交往，重新生成。")
                    fixed = None
                    if can_retry and time.time() - reply_started <= 60:
                        try:
                            retried = await _generate(
                                ctx, use_history,
                                list(extra_parts) + [commitment_warning(
                                    aff_now["stage"],
                                    app_context.affection_mgr.accept_min_stage())])
                        except asyncio.TimeoutError:
                            print("关系进度重生成超预算，保留原回复。")
                            retried = None
                        if retried and retried.get("sentences") \
                                and not looks_like_acceptance(
                                    "".join(str(s.get("zh", "") or "")
                                            for s in retried["sentences"])):
                            fixed = retried
                    if fixed is not None:
                        reply = fixed

            if sink is None or sink.sent == 0:
                if voice_wanted(app_context.active_config(), session_type, client):
                    await repair_sentence_lang(reply.get("sentences", []), ctx)
                if pushed_down and quote_reply_id and reply.get("sentences") \
                        and not reply["sentences"][0].get("reply_to"):
                    reply["sentences"][0]["reply_to"] = True

            if has_image:
                img_desc = str(reply.get("description", "") or "").strip()
                if img_desc:
                    _backfill_image_description(history, img_desc)
            # ========================================================

            if app_context.affection_mgr is not None and verdict is not None \
                    and verdict.get("affection_enabled") \
                    and verdict.get("affection_delta") is not None:
                aff_rec = app_context.affection_mgr.apply_delta(
                    ctx.character_key, sender_id, verdict.get("affection_delta"),
                    confession=bool(verdict.get("confession")),
                    romance=bool(verdict.get("romance")),
                    acceptance=bool(verdict.get("acceptance")),
                    breakup=bool(verdict.get("breakup")),
                    session_id=session_id,
                    reply_text="".join(str(s.get("zh", "") or "")
                                       for s in reply.get("sentences", [])))
                aff_now = app_context.affection_mgr.state(ctx.character_key, sender_id, session_id)
                print(f"关系进度：好感 {aff_now['score']}/{SCORE_MAX}"
                      f"（亲近程度：{aff_now['band']}；关系性质：{aff_now['nature']}）"
                      + ("，已记为伴侣" if aff_rec["partner"] else ""))

            if jealousy_rival and app_context.mood_mgr is not None:
                try:
                    penalty = abs(_to_float(app_context.active_config().get("mood_jealousy_penalty", 5), 5.0))
                    lo, hi = _mood_bounds_now()
                    cur = stored_mood(RoleContext(app_context.active_config().config, role),
                                      app_context.mood_mgr, session_id, user_id=mood_user)
                    app_context.mood_mgr.set_mood(session_id, ctx.character_key,
                                      clamp(cur - penalty, lo, hi),
                                      user_id=mood_user)
                    print(f"吃醋：心情额外 -{penalty:.0f}（提到了「{jealousy_rival}」）。")
                except Exception as e:
                    print(f"吃醋心情落盘失败（忽略）: {type(e).__name__}: {e}")

            sulk_hit = next((s for s in reply["sentences"] if s.get("sulk") is True), None)
            if sulk_hit is not None:
                system_receipts.append({"action": "冷处理", "ok": True,
                                        "target": str(sender_id), "at": time.time()})

            tts_calls_result = sink.tts_calls if sink is not None else 0
            send_result = {}
            send_options = {
                "reply_id": quote_reply_id,
                "allowed_at_ids": list(allowed_at_ids) if not is_private else [],
                "poke_target": sender_id,
                "recall_request": send_recall_request,
                "at_names": allowed_at_names,
                "speaker_id": sender_id,
                "speaker_role": sender_role,
                "mute_ids": mute_ids,
                "recall_other_id": recall_other_id,
                "mute_done_ids": mute_done_ids,
            }
            allow_voice = bool(app_context.active_config().get("tts_reply_enabled", True)) \
                and client_supports(client, "voice")
            if sink is not None:
                if sink.sent == 0:
                    send_result = await app_context.sender.send_reply(
                        session_type, target_id, reply["sentences"], emotions, ctx,
                        use_voice=allow_voice, **send_options)
                    tts_calls_result += send_result.get("tts_calls", 0)
            else:
                send_result = await app_context.sender.send_reply(
                    session_type, target_id, reply["sentences"], emotions, ctx,
                    use_voice=allow_voice, **send_options)
                tts_ms = send_result.get("tts_ms", 0.0)
                tts_calls_result = send_result.get("tts_calls", 0)
            sent_texts = (sink.sent_texts if sink is not None and sink.sent
                          else send_result.get("sent_texts") or [])
            receipts: List[dict] = []
            if sink is not None:
                receipts.extend(sink.action_receipts)
            receipts.extend(send_result.get("action_receipts") or [])
            receipts.extend(system_receipts)
            receipts_note = action_receipts_note(receipts, speaker_labels)

            sent_now = urls_in_text("".join(str(s.get("display", "") or "")
                                            for s in reply["sentences"]))
            if sent_now:
                record_sent_links(session_id, sent_now)

            for extra in _dispatch_plugin_message({
                    "session_type": session_type, "target_id": target_id,
                    "session_id": session_id, "sender_id": sender_id,
                    "sender_name": sender_name, "text": user_text,
                    "role": role, "emotions": emotions}):
                try:
                    await app_context.sender.send_text(session_type, target_id, extra)
                except Exception as e:
                    print(f"[插件] 追加回复发送失败: {type(e).__name__}: {e}")

            _dispatch_reply_done({
                "session_type": session_type, "target_id": target_id,
                "session_id": session_id, "sentences": reply["sentences"],
                "emotions": emotions, "role": role, "reply": reply,
                "sender_id": sender_id, "user_text": user_text,
            })

            zh_text = strip_mention_placeholder(
                "".join(str(s.get("zh") or s.get("display") or "")
                        for s in reply["sentences"]))

            if app_context.promise_mgr is not None and bool(app_context.active_config().get("promise_enabled", True)) \
                    and PROMISE_HINT_RE.search(zh_text):
                _spawn(_extract_and_store_promise(ctx, session_id, sender_id, zh_text))
            speaker = role.get("character_name", ctx.character_key)
            parts = [strip_mention_placeholder(t) for t in sent_texts]
            parts = [t for t in parts if t.strip()]
            if len(parts) <= 1:
                parts = [zh_text]
            tool_notes = _tool_notes_from_trace(reply.get("tool_trace"))
            for index, part in enumerate(parts):
                entry = {"role": "assistant", "content": part, "timestamp": time.time(),
                         "speaker": speaker,
                         "emotion": reply["sentences"][0].get("emotion", "")}
                if tool_notes and index == len(parts) - 1:
                    entry["tool_notes"] = tool_notes
                if receipts_note and index == len(parts) - 1:
                    entry["action_notes"] = receipts_note
                history.append(entry)
            try:
                sent_sticker = getattr(app_context.sender, "last_sent_sticker", None)
                if sent_sticker and sent_sticker.get("key") == f"{session_type}|{target_id}":
                    app_context.sender.last_sent_sticker = None
                    spath = Path(str(sent_sticker.get("path") or ""))
                    if spath.is_file():
                        sname = _cache_image_bytes(spath.read_bytes(),
                                                   spath.suffix or ".gif")
                        if sname:
                            stem = spath.stem[:40]
                            history.append({"role": "assistant", "content": f"[表情包: {stem}]",
                                            "image": sname, "timestamp": time.time(),
                                            "speaker": speaker, "emotion": ""})
            except Exception as e:
                print(f"表情包缓存失败: {type(e).__name__}: {e}")
            if sulk_hit is not None:
                if not zh_text.strip():
                    # 纯冷处理（一句台词都没说）：在历史里留一条记录，让角色之后知道自己晾过人
                    for prev in reversed(history):
                        if prev.get("role") == "assistant":
                            if not str(prev.get("content") or "").strip() \
                                    and prev.get("speaker") == speaker:
                                prev["content"] = "[冷处理：这段时间已读不回]"
                            break
                if bool(app_context.active_config().get("sulk_enabled", True)):
                    sulk_seconds = _sulk_seconds_of(sulk_hit)
                    meta.setdefault("sulk_until", {})[str(ctx.character_key)] = \
                        time.time() + sulk_seconds
                    print(f"冷处理：{ctx.character_name or ctx.character_key} 发完这句开始晾 "
                          f"[{target_id}] {sulk_seconds} 秒，期间已读不回，到点补一个表情。")
                    _spawn(_send_sulk_sticker_later(role, ctx, session_type, target_id,
                                                    session_id, sulk_seconds))
            followup_seconds = _followup_seconds_of(reply["sentences"])
            if followup_seconds > 0 \
                    and bool(app_context.active_config().get("followup_enabled", True)):
                _schedule_speak_first(role, session_type, target_id, session_id,
                                      followup_seconds)
            data["history"] = history
            data["meta"] = meta
            mem.save_session_data(session_id, data)
            if has_image:
                _clear_pending_image(session_id)

            if app_context.stats_mgr and app_context.active_config().get("stats_enabled", True) and app_context.db is not None:
                app_context.db.record_interaction(
                    session_type, session_id, sender_id, sender_name,
                    role.get("character_key", ""), reply["sentences"][0].get("emotion", ""),
                    reply.get("llm_ms", 0), tts_ms, len(reply["sentences"]), ok=True,
                    llm_calls=reply.get("llm_calls", 1),
                    tts_calls=tts_calls_result,
                    tool_calls=reply.get("tool_calls", 0))
            if app_context.stats_mgr:
                app_context.stats_mgr.record_message(session_id)

            if not first_reply_done and has_image:
                _spawn_sticker_capture(ctx, reply, image_sources)

            if app_context.profile_mgr and app_context.active_config().get("profiles_enabled", False) and \
                    app_context.active_config().get("profiles_auto_extract", False) and not first_reply_done:
                _spawn(app_context.profile_mgr.extract_from_dialog(ctx, trigger_text, zh_text, sender_id))
            sent_count = len(sent_texts) or len(send_result.get("message_ids") or [])
            replied.update({
                "role": role, "text": zh_text,
                "actions": sentence_actions_note(reply["sentences"]),
                "receipts": receipts_note,
                "sent": f"{sent_count} 条文本" + ("、含语音" if tts_calls_result else "、无语音"),
            })
            first_reply_done = True
            total_replies += 1
            return True

    async def _continue_unfinished_action():
        if not app_context.active_config().get("unfinished_action_enabled", True):
            return
        role = replied.get("role")
        if role is None or not str(replied.get("text") or "").strip():
            return
        if not wants_task_action(own_text):
            return
        character_key = str(role.get("character_key") or "")
        for _ in range(_TASK_CONTINUE_ROUNDS):
            if total_replies >= max_total:
                return
            if refuses_task(str(replied.get("text") or "")):
                print(f"任务推进：{character_key} 明确拒绝了这件事，不再催。")
                return
            context_lines = speaker_labeled_lines(history, limit=6)
            progress = await check_task_progress(reply_ctx_of(role), own_text,
                                                 str(replied.get("text") or ""),
                                                 str(replied.get("actions") or ""),
                                                 str(replied.get("sent") or ""),
                                                 context_lines,
                                                 str(replied.get("receipts") or ""))
            if not progress or progress.get("done"):
                return
            task = str(progress.get("task") or "").strip()
            action = str(progress.get("action") or "").strip()
            steps = [str(x).strip() for x in (progress.get("steps") or []) if str(x).strip()]
            if not (task or action or steps):
                return
            receipts = str(replied.get("receipts") or "")
            how = "；".join(steps) if steps else action
            receipt_note = f"系统回执：{receipts}。" if receipts else ""
            if receipts and "失败" in receipts:
                receipt_note += "被挡下的动作这轮做不了。"
            trigger = ("（系统提示：你上一条回复只是在答应，并没有真的把这件事做出来。"
                       + (f"主人要的是：{task}。" if task else "")
                       + (f"接下来照这么做：{how}。" if how else "")
                       + receipt_note
                       + "现在就直接做出来，不要再答应一次、不要再问一句、也不要只说准备。"
                         "这段是系统说明，不要把它抄进回复里。）")
            print(f"任务推进：{character_key} 上一条只答应没动手，接着让她把内容做出来。")
            if not await process_role_reply(role, trigger, overrides={
                    "repeat_guard_compare_self": False,
                    "mute_done_ids": list(mute_state["done_ids"])}):
                return

    recall_task = None
    if app_context.recall_mgr is not None and app_context.recall_mgr.enabled():
        try:
            win = max(0, int(app_context.active_config().get("history_length", 8) or 0))
        except (TypeError, ValueError):
            win = 8
        recent = [m for m in (history[-win:] if win else []) if isinstance(m, dict)]
        exclude_ts = float(recent[0].get("timestamp") or 0) if recent else 0.0
        recall_task = asyncio.create_task(
            app_context.recall_mgr.recall_context(session_id, user_text, exclude_after_ts=exclude_ts))

    typing = typing_client(client)
    if typing is not None:
        typing.begin_typing(target_id)
    try:
        voice_wanted = bool(app_context.active_config().get("tts_reply_enabled", True)) \
            and client_supports(client, "voice")
        if voice_wanted and not await ensure_tts_service(app_context.active_config()):
            print("警告：TTS 服务不可用，将降级为纯文本。")

        for role in target_roles:
            if total_replies >= max_total:
                break
            await process_role_reply(role, user_text, gate_reply=True, own_text=own_text)

        rounds = int(app_context.active_config().get("multi_role_auto_rounds", 0) or 0)
        if app_context.active_config().get("multi_role_enabled", False) and rounds > 0 and len(target_roles) > 1:
            for _ in range(rounds):
                for role in target_roles:
                    if total_replies >= max_total:
                        break
                    last_assistant = next((m for m in reversed(history)
                                           if m.get("role") == "assistant"), None)
                    if not last_assistant:
                        break
                    if last_assistant.get("speaker") == role.get("character_name", ""):
                        continue
                    trigger = (f"（群里的另一位角色「{last_assistant.get('speaker', '')}」刚刚说："
                               f"{last_assistant.get('content', '')}）请自然地接话回应。")
                    await process_role_reply(role, trigger)
        await _continue_unfinished_action()
    except Exception as e:
        print(f"回复生成失败: {type(e).__name__}: {e}")
    finally:
        if typing is not None:
            typing.end_typing(target_id)

    await asyncio.to_thread(app_context.memory_manager.cleanup_voice_cache,
                            app_context.active_config().get("max_voice_cache", 20))
    if total_replies > 0:
        _spawn(post_reply_context_tasks(session_id, get_active_ctx()))
    else:
        data["history"] = history
        data["meta"] = meta
        mem.save_session_data(session_id, data)
