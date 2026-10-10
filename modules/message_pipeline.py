# -*- coding: utf-8 -*-
# 消息入口管线（自 main.py 搬迁）：事件解析、连发合并排队、未处理消息记录、
# 总入口 handle_message_event 与主处理 _process_message_event。
# 管理器槽位、ROLE_CONNECTIONS/_SESSION_LOCKS 与主动消息状态统一经
# modules.app_context 读写；napcat_client 的两处运行时写入（handle_message_event /
# _process_message_event 收到连接时挂载客户端）也落在 app_context.napcat_client。
# _SESSION_PENDING 与 _COALESCE_WINDOW 是本模块自有的连发合并排队状态，随模块走。
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
                                 speaker_labeled_lines,
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
from modules.sender import RECALL_DENY, can_recall_others
from modules.reply_pipeline import (SentenceSink, generate_reply, _spawn,
                                    _norm_text, _repeat_ratio, _strip_image_claims,
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
                                     config_for_client, _fetch_member_name)
from modules.tts_service import ensure_tts_service
from modules.webui_common import (_dispatch_plugin_command,
                                  _dispatch_plugin_message, _plugin_runtime)
from napcat import (PrivateMessageEvent, GroupMessageEvent, Text, Record,
                    Image, At, Reply, Forward, FriendPokeEvent, GroupPokeEvent)


_SESSION_PENDING: Dict[str, dict] = {}
# 每个会话最近收到的那条消息 id（含没被@、不回复的群消息）：回复生成完时发现
# 已经不是要回的那条了，说明那条被别人的消息刷下去了，得引用它再回
_SESSION_LAST_INCOMING: Dict[str, str] = {}
# 连发消息的等待窗口：收到一条后先等这么久，期间又来消息就重新计时，
# 等对方把话说完再回。窗口太短会变成「说一句回一句」，对方还没打完就抢答。
_COALESCE_WINDOW = 1.2
# 任务推进：角色只答应不动手时，一条主人消息最多再自动接几轮让她把事做出来
_TASK_CONTINUE_ROUNDS = 2


def _touch_pending(session_id: str) -> None:
    """记下该会话最后一条消息到达的时刻，用来把合并窗口往后推。"""
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
        # 引用消息必须放行到 process_message：只有在那里回查被引用的消息，
        # 才能知道引用的是不是机器人（是则视同 @）。引用他人消息的做法是在
        # process_message 里、回查之后再按同一套配置拦下。
        if reply_seg is None:
            if app_context.active_config().get("only_private", False):
                return None
            # 没被@的群消息不回复，但仍要记进历史：模型下一条被@时才有上下文，
            # 聊天记录页也要能看到这些消息。
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
    """群聊里没被@的消息：不回复，但落进历史。

    开启「回复需要@」后这类消息以前会被整条丢掉，模型下一条被@时看不到群里
    刚聊了什么，聊天记录页也完全不显示。这里只补记历史，不碰主动消息的闲置
    计时、也不消耗防刷屏额度。
    语音不在这条链路上转文字（反正不回复），只留一个 [语音] 占位。
    """
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
    try:
        app_context.memory_manager.migrate_legacy_memory(session_id)
        data = app_context.memory_manager.load_session_data(session_id)
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
        app_context.memory_manager.save_session_data(session_id, data)
    except Exception as e:
        print(f"记录未@的群消息失败: {e}")


def _queue_unaddressed_message(session_id: str, text: str, has_image: bool,
                               sender_id: str, sender_name: str,
                               has_voice: bool = False,
                               has_forward: bool = False) -> None:
    """等会话锁释放后再补记没被@的群消息（该会话正在生成回复时走这条）。"""
    async def run():
        lock = app_context._SESSION_LOCKS.setdefault(session_id, asyncio.Lock())
        async with lock:
            _remember_unaddressed_message(session_id, text, has_image,
                                          sender_id, sender_name, has_voice, has_forward)
    try:
        asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        pass


# 智能回复：先判断对方说完了没，判定为「还没说完」时最多再等这么久；
# 这段时间内没有新消息就直接回复。轮询间隔决定来了新消息后多久重新判定。
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
    """问一次模型「对方说完了没」；问不出来时按说完了处理，不拖着回复不发。"""
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
    """智能回复的等待判定：返回 True 表示还要继续等，False 表示可以回复了。

    判定结果按「这批消息的最后一条」缓存：期间又来了消息就重新判定一次。
    """
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
    """戳一戳通知里没有昵称：单独问一次，别把 QQ 号当成昵称写进用户画像。"""
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
    """把「有人戳了机器人」的通知折成一条普通消息事件，走同一条回复管线。

    只认戳机器人的通知：机器人自己戳别人时上报里 user_id 是机器人，
    照单全收会变成自己戳自己、来回没完。
    """
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
    # 这条接入方式绑了配置文件时，整条处理链路（会话判定、提示词、发送）都用那份配置
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
    # 记住这条会话是从哪条接入方式来的：主动消息、提醒才知道该往哪条发
    if app_context.sender is not None:
        app_context.sender.remember_session(info.get("session_id"), client)
    if info.get("silent"):
        silent_session = info["session_id"]
        silent_sender = getattr(event.sender, "user_id", None) or "0"
        silent_name = getattr(event.sender, "nickname", None) or ""
        silent_lock = app_context._SESSION_LOCKS.get(silent_session)
        if silent_lock is not None and silent_lock.locked():
            # 该会话正在生成回复：直接写盘会被对方手里那份旧历史在结束时整份覆盖回来，
            # 这条没被@的消息就永久消失了，所以等锁释放后再补记
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
                # 距最后一条消息还没到窗口就接着等：对方还在打字，别抢答
                idle = time.monotonic() - float(pending.get("last_at") or 0)
                if idle < _COALESCE_WINDOW:
                    await asyncio.sleep(_COALESCE_WINDOW - idle)
                    continue
                # 智能回复：判断对方说完没有，没说完就再等一段
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
    """这一轮允许禁言的号码，按「这一轮针对谁」排序：
    @到的人 → 被引用的人 → 刚刚在说话的那个人 → 主人自己（仅当他明说要禁言自己）。

    名单之外的号码一律不认，免得模型编一个号就把无辜群成员禁言了。
    主人自己开口要求禁言时他不在名单里：他说的是「他」，把提要求的人自己禁掉是
    明显的错人（表现就是主人当场反问「不是禁言我」）。
    """
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
    # 用户画像的昵称默认就是对方的 QQ 昵称：只在还没有昵称时补上，之后不会被自动改写
    if app_context.profile_mgr is not None and app_context.active_config().get("profiles_enabled", False):
        app_context.profile_mgr.remember_nickname(sender_id, getattr(event.sender, "nickname", ""))
    incoming_message_id = getattr(event, "message_id", None)
    # 本轮发言者在群里的角色（owner/admin/member）：禁言与撤回别人的消息要靠它判权限，
    # 带上就不用为了判权限再多查两次群成员资料
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

    # 主人自己打的字（不含下面拼进来的引用 / 转发内容）：撤回判据只认它，
    # 否则引用一条提到「撤回」的旧消息会被当成新的撤回要求
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
            # 引用他人消息、又没@机器人：不回复，但消息要记进历史
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
        # 语音识别只在「这条消息真的要处理」之后才跑：转写要几秒甚至更久
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
    app_context.proactive_pending.pop(session_id, None)
    if session_id in app_context.proactive_awaiting:
        # 用户在会话里发言即视为已回应：恢复该会话的主动开口资格
        app_context.proactive_awaiting.discard(session_id)
        print(f"主动消息：用户已在 {session_id} 回复，恢复主动开口资格。")
    save_proactive_state()

    app_context.memory_manager.migrate_legacy_memory(session_id)
    data = app_context.memory_manager.load_session_data(session_id)
    history = data.get("history", [])
    meta = data.get("meta", {})

    # ---- 插件指令：命中就由插件直接回复，不进 LLM 管线 ----
    # 插件通过 ctx.register_command 注册具名指令。这里刻意放在"消息已通过
    # 白名单/防刷屏/唤醒校验、历史已加载"之后、"写入用户消息"之前：
    # 指令是控制面操作，不该污染对话历史、也不该触发识图与待办提取。
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
    
    # 初始历史（之后会在识图成功后更新描述）
    history_text = (f"{user_text} [图片]".strip() if has_image
                    else user_text) or "[图片]"

    # ---- 上一条图片消息没被真正看过时：本条相关追问要把识图模型叫回来 ----
    # 事故背景：用户发图 → 回复审判判定"无需回复" → 识图链路整条没跑、历史里只有
    # "[图片]"；用户接着问"这个怎么样"时系统只当纯文本处理，识图模型根本没被调用，
    # 于是回复节奏和上下文全对不上。
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
                # 历史里点明"上一条附件是图片"，让模型知道这条追问指向哪张图
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
            # 带上最近几条对话：主人可能先说"我要睡觉了"，再说"5分钟后再提醒我"，
            # 没有上下文就只能把"再提醒我"当成事项（历史 bug）。
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
    # 最近一条真正发出去的回复（角色 + 台词）：回复之后要据此判断她有没有真动手
    replied: Dict = {}
    # 本轮已由系统禁言过的号码：任务推进的接话轮同样不能对同一目标重复禁言
    mute_state: Dict = {"done_ids": []}

    def _dispatch_reply_done(info: dict) -> None:
        """通知插件"这一轮 LLM 回复已经发完"。插件系统没启用就什么都不做。"""
        rt = _plugin_runtime()
        if rt is None:
            return
        try:
            rt.dispatch_reply_done(info)
        except Exception as e:
            print(f"[插件] 回复完成钩子派发失败: {type(e).__name__}: {e}")


    async def process_role_reply(role: dict, trigger_text: str, gate_reply: bool = False,
                                 own_text: str = "", overrides: Optional[dict] = None) -> bool:
        # 回复要走「这条消息从哪条接入方式来的」那条连接：微信/QQ 来的消息
        # 若按 client_for(role) 选，会落到默认的 NapCat 上，根本发不出去。
        # 消息来自默认连接时仍按老规矩选角色自己的连接（多账号 NapCat 靠它）。
        source_client = client if client is not None and client is not app_context.sender.client else None
        with app_context.sender.using_client(source_client or app_context.sender.client_for(role)):
            nonlocal total_replies, first_reply_done
            if total_replies >= max_total:
                return False
            ctx = reply_ctx_of(role, overrides)
            emotions = get_role_emotions(role)
            # 先备好上下文与图片：审判判定"不回复"时也要能补记画面描述（见下）
            extra_parts = []
            jealousy_rival = ""
            # 编号表按完整历史算一次，供提示词、历史窗口、系统动作说明与@候选共用：
            # 历史窗口开了摘要后只是尾部几条，若在窗口里重新编号，同一标签会指向不同的人
            speaker_labels = build_speaker_labels(history)
            # 系统路径替她做掉的动作（主人要求撤回被引用/最近那条）也要记成回执：
            # 只写进【本轮动作】提示词的话，判定那边只看到她台词里说「撤了」，
            # 会以为她只是在嘴上答应
            system_receipts: List[dict] = []
            # 系统路径本轮已禁言过的号码：发送层据此不重复执行模型再填的同一动作
            # （任务推进的接话轮通过 overrides 接着沿用这份名单）
            mute_done_ids: List[str] = [str(q) for q in ((overrides or {}).get("mute_done_ids") or [])]
            # 主人明确要求撤回时不能指望模型填字段（它常常只在嘴上答应）：
            # 要撤的是她刚发过的那条就在这里立刻撤掉，要撤的是即将发出的这条
            # 则交给发送层，等回复发出去之后再撤。
            recall_mode = recall_request_kind(own_text) if gate_reply else ""
            # "prev" 要撤的那条（被引用的 / 最近发过的）在上面就处理完了，交给发送层的
            # 只能是"别撤新回复"：否则模型顺着「撤回这条」把 recall 填成 true，撤掉的
            # 是它刚发出的新话，看着像撤错了对象
            send_recall_request = RECALL_DENY if recall_mode == "prev" else recall_mode
            # 撤回别人的消息要撤的那一条：引用了谁的消息就撤谁，没引用就撤本轮触发消息
            quoted_mid = str((quoted or {}).get("message_id") or "")
            recall_other_id = quoted_mid or str(incoming_message_id or "")
            # 「他」指的是刚刚在说话的那个人，不是提要求的主人自己：把主人塞进候选
            # 会让「禁言他」变成「禁言我」（主人当场就会反问「不是禁言我」）
            self_mute = wants_self_mute(own_text) if gate_reply else False
            recent_other_id = next(
                (q for q in recent_user_ids(history[-10:], limit=4,
                                            exclude=str(client.self_id))
                 if q != str(sender_id)), "")
            mute_ids = _mute_candidates(at_ids, (quoted or {}).get("user_id"),
                                        sender_id, client.self_id,
                                        recent_other_id, self_mute)
            if recall_mode == "prev":
                # 主人引用了机器人发过的某条消息说「撤回这条」：要撤的就是被引用的
                # 那条，而不是最近发出去的那条（否则撤掉的是别的话，看着像撤错了）
                quoted_own = str((quoted or {}).get("user_id") or "") == str(client.self_id)
                allowed = False
                if quoted_mid:
                    # 撤别人的消息要管理员权限：请求的人和机器人自己都得是管理员或群主，
                    # 否则一般群成员能借她的号删掉别人的消息
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
                    # 撤不成只有两种原因：权限不够，或撤回接口本身失败。写明真实原因，
                    # 免得模型自己往「消息太旧」「超时」上猜
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
            # 主人明确吩咐禁言时同样不能指望模型填字段（它常常只在嘴上答应，甚至谎报
            # 「已成功禁言…」）：@ 了谁就禁谁（@ 了几个就禁几个），没 @ 就是刚刚说话的
            # 那个人，明说要禁言自己时才是他本人 —— 直接执行，别让她答应一句再拖着
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
                    # 对象要写成她认识的编号标签：不写是谁被禁了，她下一句就会
                    # 认错人（把「禁言了谁」安到当前发言者或主人头上）；
                    # 多个目标时逐人写结果，免得一部分成功被她说成"都处理了"
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
            # 奇遇 / 吃醋只认用户真的发来的消息；多角色自动接话轮不参与
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
            # 全体成员不是「某个群成员」，不受可@名单限制：群里随时能@（有没有权限由 QQ 判）
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
            # 主人明确要求复述时，【禁止复读】这条提示词不适用，否则模型会拒绝照做
            repeat_requested = wants_repeat_request(user_text)
            if repeat_requested:
                extra_parts.append(
                    "【本轮要求】主人本条消息明确要求你复述/重复他指定的内容："
                    "按他的要求原样说出来即可，【禁止复读】这一轮不适用。")
            use_history = history
            if app_context.active_config().get("summary_enabled", False) and meta.get("summary"):
                summary_text = background_ready(meta["summary"])
                if not summary_text:
                    # 摘要不能用时绝不能只留最近几条：那等于把更早的上下文整个丢掉
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
            # 开关判定必须走 mood 模块的兼容层：旧配置缺这两个键时以"是否配置了提示词"为准，
            # 直接读配置会漏判，且字符串 "false" 会被当成真值
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
                # 审判看不到画面：本条带图时，"无需回复"这个判定没有依据（消息里可能
                # 除了图片一个字都没有），不生效，交给角色自己回。否则识图模型已经
                # 生成的台词会被丢掉，历史里还会留下一张从没被回应过的图，下一轮模型
                # 看到它就会接着往下演。
                if _image_reply_override(verdict, has_image):
                    print(f"回复审判：本条带图，{ctx.character_name or ctx.character_key} "
                          f"的「无需回复」判定不生效，仍由角色回复（{_judge_mood_text(verdict)}）")
                    verdict["should_reply"] = True
                # 只开心情、没开审判时 verdict["should_reply"] 恒为 True，不会被拦下；
                # 开着审判才会出现真正"决定不回复"的分支。
                if verdict is not None and not verdict["should_reply"]:
                    print(f"回复审判：{ctx.character_name or ctx.character_key} 决定不回复"
                          f"（{_judge_mood_text(verdict)}，"
                          f"回复概率 {verdict['probability']:.2f}，"
                          f"{_judge_gate_cause(verdict)}）")
                    if has_image and image_sources:
                        # 审判在识图之前就判定"不用回"，但图还是要看：把画面描述写进历史，
                        # 供用户下一条相关追问使用（统一由收尾逻辑决定是否落盘）。
                        # 只取描述与收藏判定，不产出台词 —— 这一轮不发消息，台词留着
                        # 只会变成下一轮"接着演"的由头。
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
                        # 收藏判定也在同一份识图结果里：触发点原先只挂在回复路径上，
                        # 审判判不回时这条路径整段不执行，图就永远不会被收藏。
                        _spawn_sticker_capture(ctx, pending_reply, image_sources)
                    _log_mood_commit(commit_mood(ctx, app_context.mood_mgr, session_id, verdict, mood_user),
                                     verdict)
                    return False

            # 心情值不只是记录：按档位把语气与篇幅约束附加到本轮提示词
            if app_context.mood_mgr is not None:
                mood_now = verdict["mood"] if verdict is not None \
                    else current_mood(ctx, app_context.mood_mgr, session_id, mood_user)
                tier, mood_note = mood_style(ctx, mood_now)
                if mood_note:
                    extra_parts.append(mood_note)
                    print(f"心情影响语气：心情值 {mood_now:.0f} → {tier}档，"
                          "本轮回复按该档位的语气与篇幅约束生成。")

            # 关系进度（Galgame 式攻略）：只给当前阶段的边界，答不答应由角色自己决定
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

            # 心情日记：她自己写的东西，她记得写过，但不知道别人也能看到
            diary_note = mood_diary_note(ctx, session_id)
            if diary_note:
                extra_parts.append(diary_note)

            # 会话回忆：未了话题常驻置顶 + 本轮检索到的相关往事
            if app_context.recall_mgr is not None and app_context.recall_mgr.enabled():
                topics_block = app_context.recall_mgr.open_topics_block(session_id)
                if topics_block:
                    extra_parts.append(topics_block)
                recall_block = await _take_recall_block(recall_task)
                if recall_block:
                    extra_parts.append(recall_block)

            # 角色常常只答应不动手（一直"这就开始"）：先把"不许空转"摆到她面前
            if app_context.active_config().get("unfinished_action_enabled", True):
                extra_parts.append(ACTION_NOW_HINT)

            # 要回的那条被后来的消息刷下去了：这一轮强制引用它，免得回复看着像在答别人
            pushed_down = bool(incoming_message_id) and \
                _SESSION_LAST_INCOMING.get(session_id, "") not in ("", str(incoming_message_id))
            # 私聊引用只有声明了 quote_private 的通道发得出来（NapCat 私聊不带 Reply 段）
            quote_reply_id = incoming_message_id if (
                incoming_message_id and (not is_private
                                         or client_supports(client, "quote_private",
                                                            default=False))) else None
            if pushed_down and quote_reply_id:
                print(f"引用：要回的那条消息已被后来的消息刷下去，"
                      f"本轮回复改为引用它（{quote_reply_id}）。")
            # 允许@的成员的昵称：只在发送层用来认出台词里写成文字的「@某人」
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
                                   speaker_labels=speaker_labels),
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
                # 生成失败要回一条中文提醒（原因已写进日志），用户才知道这轮没成；
                # 本轮还没有完整回复发出去时才补发，避免和已发出的内容重复
                if isinstance(e, Exception) and total_replies <= 0:
                    try:
                        await app_context.sender.send_text(session_type, target_id,
                                               error_reply_text(e))
                        print(f"回复生成失败，已把中文提醒发回会话: "
                              f"{type(e).__name__}: {e}")
                    except Exception as send_error:
                        print(f"报错内容发送失败: {type(send_error).__name__}: {send_error}")
                raise
            # 本轮回复已生成，审判判定的心情变化量此时才落盘
            _log_mood_commit(commit_mood(ctx, app_context.mood_mgr, session_id, verdict, mood_user), verdict)
            if timed_out:
                if sink is not None:
                    await sink.abort()
                return False

            # 识图成功就立刻把画面描述回填进历史（哪怕这一轮最终不发消息）：
            # 这样"要不要回复"的审判、以及下一轮追问，都能看到画面真实内容。
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
                user_ratio = _repeat_ratio(user_text, zh_now)
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
                    # 验收同样只看还有效的那些维度：关掉的维度不该继续左右"重生成是否被接受"
                    new_self = _max_repeat_ratio(new_zh, recent_replies)[0] \
                        if repeat_flags["compare_self"] else 0.0
                    new_user = _repeat_ratio(user_text, new_zh) \
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

            # 关系进度：对方刚表白或明确答应交往，而好感或相处还没到能定关系的时候
            # 却直接确认了关系 → 重新生成（只说明节奏，怎么演仍由角色自己决定）
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
                await repair_sentence_lang(reply.get("sentences", []), ctx)
                # 强制引用的那条消息：批量发送只认句子字段，补在第一句上
                if pushed_down and quote_reply_id and reply.get("sentences") \
                        and not reply["sentences"][0].get("reply_to"):
                    reply["sentences"][0]["reply_to"] = True

            # ====== 上下文互通核心逻辑：回填识图模型的画面描述 ======
            if has_image:
                img_desc = str(reply.get("description", "") or "").strip()
                if img_desc:
                    _backfill_image_description(history, img_desc)
            # ========================================================

            # 关系进度：回复定稿后才落盘（好感变化 + 角色确实答应时才记成伴侣）
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

            # 吃醋：本轮消息提到了别的角色，心情按配置额外扣一点
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
            # 语音要看这条接入方式支不支持：微信 ClawBot / QQ 官方只能发文字
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
            # 流式路径逐句发、批量路径按分开发送逐条发：两种都从实际发出的文本取
            sent_texts = (sink.sent_texts if sink is not None and sink.sent
                          else send_result.get("sent_texts") or [])
            # 动作回执在发送完成后立刻收拢：入库与"她有没有真做"的判定都用这一份
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

            # 插件 on_message：主回复发完后，把插件想追加的文本挨条发出去。
            # 顺序放在主回复之后，是为了让插件的补充说明不打断角色本身的语气。
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

            # 流式路径补出来的句子可能只有 display 没有 zh，取不到时退回展示文本
            # @ 占位符只是发送时的定位标记，不进历史（否则下一轮会当成正文）
            zh_text = strip_mention_placeholder(
                "".join(str(s.get("zh") or s.get("display") or "")
                        for s in reply["sentences"]))

            # 承诺追踪：角色台词里出现承诺字样时，后台跑一次 LLM 提取（不阻塞发送）
            if app_context.promise_mgr is not None and bool(app_context.active_config().get("promise_enabled", True)) \
                    and PROMISE_HINT_RE.search(zh_text):
                _spawn(_extract_and_store_promise(ctx, session_id, sender_id, zh_text))
            speaker = role.get("character_name", ctx.character_key)
            # 分开发送时 QQ 里是好几条消息，聊天记录也要一条一条记：
            # 只写成一整段的话，记录页看到的样子和实际收到的对不上
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
                # 动作回执跟着最后一条入库：下一轮模型才知道上一轮对谁做了什么、
                # 谁做成了谁没做成，不会把失败的说成成功、也不会认错对象
                if receipts_note and index == len(parts) - 1:
                    entry["action_notes"] = receipts_note
                history.append(entry)
            # 表情包随消息发出后也进缓存：记录页用真实图片展示，清除缓存后退回文字
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
            data["history"] = history
            data["meta"] = meta
            app_context.memory_manager.save_session_data(session_id, data)
            if has_image:
                # 图片已经被真正看过并回复过了，不必再为后续追问重跑识图
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
            # 流式 sink 与批量发送是"二选一"的：谁真发了消息，回执就在谁那儿，
            # 两边都收一遍才不会漏（含系统路径替她做掉的那些动作）
            replied.update({
                "role": role, "text": zh_text,
                # 声明的动作字段、动作执行回执与实际发出的条数：
                # 判定"她是不是只嘴上答应"要用真回执，不能只看声明
                "actions": sentence_actions_note(reply["sentences"]),
                "receipts": receipts_note,
                "sent": f"{sent_count} 条文本" + ("、含语音" if tts_calls_result else "、无语音"),
            })
            first_reply_done = True
            total_replies += 1
            return True

    async def _continue_unfinished_action():
        """回复发完之后判一次：她只答应没动手就接着让她做，最多补 _TASK_CONTINUE_ROUNDS 轮。

        判定的时机刻意放在回复之后：主人已经发过话了，她口头答应却没真做时由系统接着催她，
        不需要主人再说一句「快开始」。只有主人这条消息在催结果、或要求了一个具体动作时才判，
        普通闲聊不会凭空多出一条回复。判定说不出"还差什么"时也不接。
        """
        if not app_context.active_config().get("unfinished_action_enabled", True):
            return
        role = replied.get("role")
        if role is None or not str(replied.get("text") or "").strip():
            return
        # 判据只看主人自己打的字：user_text 已经拼上引用/转发内容，
        # 拿它判会把别人说的话当成主人的要求，凭空多催一轮
        if not wants_task_action(own_text):
            return
        character_key = str(role.get("character_key") or "")
        for _ in range(_TASK_CONTINUE_ROUNDS):
            if total_replies >= max_total:
                return
            # 她已经明说不做这件事：这是她的决定，不再硬推着她做
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
            # 判定子调用给的是"该怎么做、用哪个动作"，这里原样交给她照着做
            how = "；".join(steps) if steps else action
            receipt_note = f"系统回执：{receipts}。" if receipts else ""
            if receipts and "失败" in receipts:
                receipt_note += "被挡下的动作这轮做不了。"
            trigger = ("（系统提示：你上一条回复只是在答应，并没有真的把这件事做出来。"
                       + (f"主人要的是：{task}。" if task else "")
                       + (f"接下来照这么做：{how}。" if how else "")
                       # 动作被上限/接入方式挡下时，真实结果是"没做成"：
                       # 让她如实说明或换个方式，而不是装作做过、也不是重复同一个动作
                       + receipt_note
                       + "现在就直接做出来，不要再答应一次、不要再问一句、也不要只说准备。"
                         "这段是系统说明，不要把它抄进回复里。）")
            print(f"任务推进：{character_key} 上一条只答应没动手，接着让她把内容做出来。")
            # 这一轮就是要她把刚说过的那件事真正做出来，台词本来就该跟上一条接近：
            # 关掉"和自己历史台词比对"的防复读，否则会被判重复、反复重生成；
            # 已禁言名单一起带下去，接话轮不会对同一目标再禁一遍
            if not await process_role_reply(role, trigger, overrides={
                    "repeat_guard_compare_self": False,
                    "mute_done_ids": list(mute_state["done_ids"])}):
                return

    # 会话回忆：检索与本轮消息相关的往事。与审判并发跑，取回时通常已经完成，
    # 不占首字延迟；检索失败一律当没有，绝不因此挡住回复。
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

    # 「正在输入」按整轮回复开关一次：逐句发送时每句都开关一遍，
    # 既让状态反复闪烁，又把握手压在发消息的关键路径上
    typing = typing_client(client)
    if typing is not None:
        typing.begin_typing(target_id)
    try:
        if not await ensure_tts_service(app_context.active_config()):
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
        # 角色只答应不动手：回复发出去之后判一次，没真做就接着让她把内容做出来，
        # 不需要主人再发一条。只在主人催着要结果、或这个会话确实还欠着事时判，避免每轮多花一次调用。
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
        # 没产生回复（审判判定不用回 / 生成失败 / 发送失败）也照常落盘：
        # 用户确实说过的话要留着，否则下一条追问时模型看不到原内容。
        data["history"] = history
        data["meta"] = meta
        app_context.memory_manager.save_session_data(session_id, data)
