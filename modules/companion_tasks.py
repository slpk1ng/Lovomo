# -*- coding: utf-8 -*-
import asyncio
import random
import re
import time

from modules import app_context
from modules.jobs import generate_proactive_text
from modules.llm_helpers import (RoleContext, POKE_MESSAGE_TEXT, chat_once,
                                 strip_thinking, extract_json,
                                 speaker_labeled_lines)
from modules.memory_store import _is_memory_filename, _memory_session_id
from modules.mood import MoodManager, affection_enabled
from modules.promises import extract_promise
from modules.proactive_state import (save_proactive_state,
                                     session_memory_exists,
                                     forget_proactive_session)
from modules.reply_pipeline import _norm_text, _repeat_ratio
from modules.session_context import (get_active_ctx, get_active_role, reply_ctx_of,
                                     role_memory, get_active_emotions,
                                     get_role_emotions, parse_session_target,
                                     _in_quiet_hours, _parse_jitter_minutes,
                                     _session_whitelisted, memory_for_session)
from modules.stickers import StickerManager


_SELF_REFERENCE_RE = r"本座|本尊|吾輩|吾辈|本小姐|本大爷"
_ATTRIBUTED_SELF_RE = re.compile(
    rf"(?:自称|被称(?:为|作)|自居为?|管自己叫)\s*[「『“‘\"']?(?:{_SELF_REFERENCE_RE})[」』”’\"']?")


def in_character_background(text) -> bool:
    cleaned = _ATTRIBUTED_SELF_RE.sub("", str(text or ""))
    return bool(re.search(_SELF_REFERENCE_RE, cleaned))


def background_ready(text) -> str:
    raw = str(text or "").strip()
    if not raw or not in_character_background(raw):
        return raw
    kept = "".join(s for s in re.split(r"(?<=[。！？!?；;\n])", raw)
                   if s.strip() and not in_character_background(s)).strip()
    return "" if in_character_background(kept) else kept


async def generate_neutral_text(ctx: RoleContext, prompt: str, max_chars: int,
                                label: str = "") -> str:
    try:
        result = await chat_once(ctx, [{"role": "user", "content": prompt}], label=label)
    except Exception as e:
        print(f"中立整理失败: {type(e).__name__}: {e}")
        return ""
    text = strip_thinking(str((result or {}).get("content") or "")).strip()
    return text[:max_chars]


CONTEXT_RECHECK_MIN_INTERVAL = 600
RELATION_RECHECK_PROMPT = (
    "你是关系确认判定器。读下面的对话记录，判断双方是否已经把关系明确定成恋人："
    "必须是一方明确表白（或明确要求确认关系）、另一方明确答应或承认，才算 true；"
    "只是暧昧、调情、亲密称呼、单方面期待，都算 false。"
    '只输出 JSON：{"confirmed": true 或 false}，不要输出其它文字。'
)


def _json_true(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "y", "是", "确认", "已确认")


def session_user_id(session_id: str, history: list) -> str:
    sid = str(session_id or "")
    if sid.startswith("private_"):
        return sid[len("private_"):]
    for msg in reversed(history or []):
        if isinstance(msg, dict) and msg.get("role") == "user":
            uid = str(msg.get("sender_id") or "").strip()
            if uid:
                return uid
    return ""


TASK_URGE_RE = re.compile(r"开始|继续|接着|动手|去做|别说了|不要说这些|赶紧|快点|催"
                          r"|搞定|说好的|答应|还没|又忘|怎么还不")
TASK_ACTION_RE = re.compile(r"戳|@|艾特|引用|撤回|禁言|一个字|一字|逐字|帮我|给我|帮忙|替我")
TASK_SYSTEM_TAG_RE = re.compile(r"\[@[^\[\]\n]{0,60}\]|" + re.escape(POKE_MESSAGE_TEXT))
# 硬推着她做既失真又白耗一轮
TASK_REFUSE_RE = re.compile(
    r"才不|偏不|偏要|不干|拒绝|懒得|休想|没门|免谈|想得美|凭什么|不奉陪|恕难"
    r"|做不到|办不到|撤不掉|删不掉|没权限|没有权限|权限不足|无权|无能为力")
ACTION_NOW_HINT = (
    "【不要空转】主人已经明确要求的事，本轮要直接给出结果本身（内容、答案、东西），"
    "不要只说「这就开始」「马上做」这类答应的话；上一条回复只是在答应、并没有真做时，"
    "这一轮必须直接动手，不要再答应一次。"
    "动作（戳一戳 / 引用 / @人 / 撤回 / 禁言）想做什么自己决定就行，不用等主人先开口；"
    "只在台词里写「（戳回去）」「（撤回这条）」这种描述不算做了，必须填对应的动作字段；"
    "也不要在台词里宣布结果（「已成功禁言…」「已经戳了你一下」），那同样不算做。"
    "戳一戳 / @人 这些是你对主人做的动作：对话里出现「被戳」时那是你戳的主人，"
    "不要说成主人戳了你。"
)
TASK_PROGRESS_PROMPT = (
    "你负责判断角色有没有真的把主人要的事做出来。输入是主人最近说的话、这个会话最近几轮对话、"
    "角色刚刚发出的回复、这条回复声明的动作字段、系统给出的动作执行回执，以及实际发出的消息条数。"
    "以下情况都算没做：只是答应、表示「这就开始 / 马上做」、反问主人要不要开始；"
    "只顾撒娇打岔而没有任何实际内容；主人要的是某个动作"
    "（戳一戳 / @人 / 引用 / 撤回 / 撤回别人的消息 / 禁言 / 一个字一条），"
    "却只在台词里嘴上说要这么做、动作字段是空的；或者声明了动作字段但实际没有做出来"
    "（要@人却一条带@的消息都没发出、要逐字发却只发了一条、要禁言却谁都没被禁言）。"
    "回执是动作的真实执行结果：显示成功就是真的做了；显示失败（这条接入方式不支持 / 发送失败）"
    "就是没做成——这时不要把「重做同一个动作」当成办法，action 里写「如实说明或换个方式」。"
    "主人只要求了某个动作时，回执显示成功就算做成了：台词只是撒娇、打岔或随口一句，"
    "也不必再让她重做一遍。"
    "已经把内容本身说出来（要讲的事、要写的段子、要给的答案、要办的事的进展）"
    "并且要求的动作确实做了，才算做了。"
    "主人只是在抱怨、感慨、描述一件事、自言自语，或者话里带过某个词时，都不算要求——"
    "只有他明确要角色去做某事才算；角色并没有答应要做什么、只是照常回应时也算做了。"
    "角色明确拒绝做这件事、或者说自己做不了这件事（「才不」「不做」「不想」「偏不」「拒绝」"
    "「做不到」「没权限」这类话）时也算做了——那是她自己的决定或能力所限，不要逼她重做。"
    "客观判断，不要写成角色的台词。"
    "判定为没做时，必须写清「她该怎么做」——照下面这几条写，不要写「赶紧做」「别再答应了」这种空话："
    "① 要动手做的，直接点名要填哪个动作字段（戳一戳 → poke；撤回她自己刚发的那条 → recall；"
    "撤回别人的消息 → recall_other；禁言 → mute，再配 mute_duration 写清禁言多少秒；"
    "引用 → reply_to；@人 → mention_ids；只发文字或一个字一条 → delivery），"
    "并写清参数（戳谁、撤哪一条、禁言谁、@哪几个人）；"
    "禁言别人与撤回别人的消息都要求提出要求的人和机器人自己都是管理员或群主，"
    "群主不能被禁言，"
    "禁言他自己则只要机器人自己是管理员就够，"
    "写步骤时按这个前提写，权限不够就让她如实说明做不到；"
    "② 要输出内容的，写清这一轮该直接说出什么（讲什么、答什么、写什么），可以只给要点；"
    "③ steps 里最多 3 条，按先后顺序写，让她照着做就行。"
    '只输出一个 JSON 对象：{"done": true 或 false,'
    '"task": "主人要角色做的事，没有就填空串",'
    '"steps": ["照做就能完成的步骤，1~3 条；写清动作名与该说的内容"],'
    '"action": "第一步，一句话"}，不要输出其它文字。'
)


def wants_task_action(text) -> bool:
    body = TASK_SYSTEM_TAG_RE.sub(" ", str(text or ""))
    return bool(TASK_URGE_RE.search(body) or TASK_ACTION_RE.search(body))


def refuses_task(text) -> bool:
    return bool(TASK_REFUSE_RE.search(str(text or "")))


async def check_task_progress(ctx: RoleContext, user_text: str, reply_text: str,
                              actions: str = "", sent: str = "",
                              context_lines: list = None, receipts: str = "") -> dict:
    prompt = (f"主人最近说的话：{str(user_text or '').strip()}\n"
              f"角色刚刚发出的回复：{str(reply_text or '').strip()[:400]}\n"
              f"这条回复声明的动作字段：{str(actions or '').strip() or '无'}\n"
              f"动作执行回执：{str(receipts or '').strip() or '无'}\n"
              f"实际发送情况：{str(sent or '').strip() or '未知'}\n"
              + (("最近的对话：\n" + "\n".join(context_lines)) if context_lines else ""))
    try:
        result = await chat_once(ctx, [{"role": "system", "content": TASK_PROGRESS_PROMPT},
                                       {"role": "user", "content": prompt}], label="任务推进判定")
        obj = extract_json(strip_thinking(str((result or {}).get("content") or "")))
    except Exception as e:
        print(f"任务推进判定失败: {type(e).__name__}: {e}")
        return {}
    if not isinstance(obj, dict):
        return {}
    raw_steps = obj.get("steps")
    steps = [str(x).strip() for x in raw_steps if str(x or "").strip()] \
        if isinstance(raw_steps, list) else []
    return {"done": _json_true(obj.get("done")),
            "task": str(obj.get("task") or "").strip(),
            "steps": steps[:3],
            "action": str(obj.get("action") or "").strip()}


async def recheck_relationship(ctx: RoleContext, history: list, meta: dict, user_id: str,
                               session_id: str = ""):
    if app_context.affection_mgr is None or not user_id or not affection_enabled(ctx):
        return
    rec = app_context.affection_mgr.get(ctx.character_key, user_id, session_id)
    if rec["partner"] or not rec["romance"]:
        return
    lines = speaker_labeled_lines(history, limit=20)
    if not lines:
        return
    summary = str(meta.get("summary", "") or "").strip()
    prompt = (RELATION_RECHECK_PROMPT
              + (f"\n背景摘要（只作资料）：{summary}\n" if summary else "")
              + "\n对话：\n" + "\n".join(lines))
    try:
        result = await chat_once(ctx, [{"role": "user", "content": prompt}], label="关系复核")
    except Exception as e:
        print(f"关系复核失败: {type(e).__name__}: {e}")
        return
    obj = extract_json(strip_thinking(str((result or {}).get("content") or "")))
    if not isinstance(obj, dict) or not _json_true(obj.get("confirmed")):
        return
    app_context.affection_mgr.confirm_partner(ctx.character_key, user_id, session_id)


async def recheck_profile(ctx: RoleContext, history: list, user_id: str):
    if app_context.profile_mgr is None or not user_id \
            or not app_context.global_config.get("profiles_enabled", False) \
            or not app_context.global_config.get("profiles_auto_extract", False):
        return
    recent = [m for m in (history or []) if isinstance(m, dict)][-40:]
    mine = [str(m.get("content", "") or "") for m in recent
            if m.get("role") == "user" and str(m.get("sender_id") or "") == user_id]
    theirs = [str(m.get("content", "") or "") for m in recent if m.get("role") == "assistant"]
    if not [line for line in mine if line.strip()]:
        return
    await app_context.profile_mgr.extract_from_dialog(ctx, "\n".join(mine), "\n".join(theirs),
                                          user_id, only_missing=True)


async def recheck_lexicon(ctx: RoleContext, history: list, session_id: str):
    if app_context.lexicon_mgr is None:
        return
    await app_context.lexicon_mgr.learn_from_history(ctx, history, session_id)


async def context_recheck(session_id: str, ctx: RoleContext, history: list, meta: dict):
    try:
        if not recheck_enabled(ctx):
            return
        user_id = session_user_id(session_id, history)
        print(f"回检：会话 {session_id} 的摘要已更新，复核关系确认、用户画像与词条。")
        await recheck_relationship(ctx, history, meta, user_id, session_id)
        await recheck_profile(ctx, history, user_id)
        await recheck_lexicon(ctx, history, session_id)
    except Exception as e:
        print(f"回检失败（忽略）: {type(e).__name__}: {e}")


def recheck_enabled(ctx) -> bool:
    if app_context.affection_mgr is not None and affection_enabled(ctx):
        return True
    if app_context.lexicon_mgr is not None:
        return True
    return bool(app_context.profile_mgr is not None
                and app_context.global_config.get("profiles_enabled", False)
                and app_context.global_config.get("profiles_auto_extract", False))


def _recheck_due(meta: dict) -> bool:
    try:
        last = float((meta or {}).get("recheck_at") or 0)
    except (TypeError, ValueError):
        last = 0.0
    return time.time() - last >= CONTEXT_RECHECK_MIN_INTERVAL


async def _take_recall_block(task) -> str:
    if task is None:
        return ""
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=3)
    except Exception as e:
        print(f"回忆检索超时或失败（本轮不注入往事）: {type(e).__name__}: {e}")
        return ""


async def post_reply_context_tasks(session_id: str, ctx: RoleContext):
    try:
        mem = memory_for_session(session_id) or app_context.memory_manager
        data = mem.load_session_data(session_id)
        history = data.get("history", [])
        meta = data.get("meta", {})
        valid_messages = [m for m in history if isinstance(m, dict)
                          and m.get("role") in ("user", "assistant")
                          and str(m.get("content", "")).strip()]
        if len(valid_messages) < 2:
            return
        history = valid_messages
        changed = False
        summary_updated = False
        # 上下文自动摘要
        if app_context.global_config.get("summary_enabled", False):
            try:
                threshold = int(app_context.global_config.get("summary_threshold", 20))
            except (TypeError, ValueError):
                threshold = 20
            keep = _summary_keep_count()
            if len(history) >= threshold + keep:
                old_msgs = history[:len(history) - keep]
                existing = meta.get("summary", "")
                lines = speaker_labeled_lines(old_msgs, limit=40)
                prompt = (f"{app_context.global_config.get('summary_prompt', '')}\n\n"
                          "你是对话记录整理器：只用第三人称客观陈述，不做主观评价，"
                          "不使用任何角色口吻、不写第一人称台词。"
                          "按每行开头的用户序号区分群成员，摘要中的事实、关系、称呼、情绪和观点必须保留对应说话人；"
                          "身份、称呼与关系一律写成「某人自称/被称作…」，不要当成既定事实；"
                          "不要把不同用户合并为同一个人，也不要把角色台词当作用户事实。\n"
                          f"{'已有摘要（请合并并保留说话人归属）：' + existing if existing else ''}\n\n对话：\n"
                          + "\n".join(lines))
                summary = await generate_neutral_text(ctx, prompt, 800, label="会话摘要")
                if summary:
                    meta["summary"] = summary
                    changed = True
                    summary_updated = True
                    print(f"已更新会话 {session_id} 的上下文摘要。")
        # 话题检测
        if app_context.global_config.get("dynamic_context_enabled", False):
            try:
                every = max(2, int(app_context.global_config.get("topic_summary_every_n", 10)))
            except (TypeError, ValueError):
                every = 10
            try:
                user_msg_count = int(meta.get("user_msg_count", 0))
            except (TypeError, ValueError):
                user_msg_count = 0
            if user_msg_count % every == 0:
                recent = history[-10:]
                lines = speaker_labeled_lines(recent, max_chars=120)
                prompt = (f"{app_context.global_config.get('topic_summary_prompt', '')}\n\n"
                          "你是对话记录整理器：只用第三人称客观陈述目前讨论的事情本身，"
                          "不做主观评价，不使用任何角色口吻、不写第一人称台词。"
                          "按行首用户序号区分群成员；若话题或立场只属于某位成员，保留其用户序号，不要推广为所有人的共同观点。\n"
                          + "\n".join(lines))
                topic = await generate_neutral_text(ctx, prompt, 200, label="当前话题")
                if topic:
                    meta["topic"] = topic
                    changed = True
                    print(f"已更新会话 {session_id} 的当前话题：{topic[:50]}")
        if app_context.lexicon_mgr is not None:
            try:
                user_msg_count = int(meta.get("user_msg_count", 0))
            except (TypeError, ValueError):
                user_msg_count = 0
            if app_context.lexicon_mgr.should_learn(user_msg_count):
                await app_context.lexicon_mgr.learn_from_history(ctx, history, session_id)
        if app_context.recall_mgr is not None and app_context.recall_mgr.enabled():
            try:
                await app_context.recall_mgr.refresh(session_id, history)
                await app_context.recall_mgr.update_open_topics(ctx, session_id, history)
            except Exception as e:
                print(f"会话回忆维护失败（忽略）: {type(e).__name__}: {e}")
        if summary_updated and _recheck_due(meta):
            meta["recheck_at"] = time.time()
            changed = True
            await context_recheck(session_id, ctx, history, meta)
        if changed:
            fresh = mem.load_session_data(session_id)
            fresh.setdefault("meta", {})
            for key in ("summary", "topic", "recheck_at"):
                if key in meta:
                    fresh["meta"][key] = meta[key]
            mem.save_session_data(session_id, fresh)
    except Exception as e:
        print(f"上下文维护任务异常: {e}")


# ============================================================================
# 主动消息与调度注册
# ============================================================================

async def proactive_idle_check():
    if not app_context.global_config.get("proactive_enabled", False):
        return
    if app_context.sender is None or app_context.sender.client is None:
        return
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    if today != app_context._proactive_state_date:
        app_context.proactive_counts.clear()
        app_context.proactive_pending.clear()
        app_context._proactive_state_date = today
        print(f"主动消息：已跨天（{today}），当日计数清零。")
    idle_minutes = float(app_context.global_config.get("proactive_idle_minutes", 30) or 30)
    idle_seconds = idle_minutes * 60
    jitter_lo, jitter_hi = _parse_jitter_minutes(app_context.global_config.get("proactive_idle_jitter", ""))
    max_per_day = max(1, int(app_context.global_config.get("proactive_max_per_day", 2) or 2))
    quiet = _in_quiet_hours()

    def _idle_ref(session_id: str) -> float:
        return max(app_context.last_user_activity.get(session_id, 0.0),
                   app_context.last_proactive_sent.get(session_id, 0.0))

    def _idle_threshold(session_id: str) -> float:
        factor = 1.0
        if app_context.affection_mgr is not None:
            session_type, target_id = parse_session_target(session_id)
            if session_type == "private":
                try:
                    factor = app_context.affection_mgr.stage_idle_factor(
                        get_active_ctx().character_key, str(target_id),
                        f"private_{target_id}")
                except Exception:
                    factor = 1.0
        return idle_seconds * max(0.1, float(factor))

    newly_scheduled = set()
    wait_reply = bool(app_context.global_config.get("proactive_wait_reply", True))
    for session_id in set(app_context.last_user_activity) | set(app_context.last_proactive_sent):
        if session_id in app_context.proactive_pending:
            continue
        if not session_memory_exists(session_id):
            forget_proactive_session(session_id)
            continue
        if not _session_whitelisted(session_id):
            continue
        if wait_reply and session_id in app_context.proactive_awaiting:
            continue
        last_ts = _idle_ref(session_id)
        if now - last_ts < _idle_threshold(session_id):
            continue
        if app_context.proactive_counts.get(f"{today}|{session_id}", 0) >= max_per_day:
            continue
        if quiet:
            continue
        jitter_seconds = random.uniform(jitter_lo, jitter_hi) * 60 if jitter_hi > 0 else 0.0
        target = now + jitter_seconds
        app_context.proactive_pending[session_id] = target
        newly_scheduled.add(session_id)
        print(f"主动消息：会话 {session_id} 已闲置 {int((now - last_ts) / 60)} 分钟，"
              f"计划在 {time.strftime('%H:%M:%S', time.localtime(target))} 主动开口。")
    if app_context.proactive_pending:
        save_proactive_state()

    for session_id, target in list(app_context.proactive_pending.items()):
        if session_id in newly_scheduled:
            continue
        if now < target:
            continue
        if not session_memory_exists(session_id):
            forget_proactive_session(session_id)
            continue
        if quiet:
            continue
        last_ts = _idle_ref(session_id)
        if now - last_ts < _idle_threshold(session_id):
            app_context.proactive_pending.pop(session_id, None)
            print(f"主动消息：会话 {session_id} 期间有互动，已取消本次主动开口。")
            continue
        used = app_context.proactive_counts.get(f"{today}|{session_id}", 0)
        if used >= max_per_day:
            app_context.proactive_pending.pop(session_id, None)
            print(f"主动消息：会话 {session_id} 已达当日上限（{max_per_day} 条），跳过。")
            continue
        session_type, target_id = parse_session_target(session_id)
        ctx = reply_ctx_of(get_active_role(), session_id=session_id)
        instruction = str(app_context.global_config.get("proactive_prompt", "主动找个话题和主人聊聊。"))
        try:
            mem = memory_for_session(session_id) or app_context.memory_manager
            hist_block = dialog_history_block(
                mem.load_session_data(session_id).get("history", []),
                ctx, session_id=session_id)
        except Exception as e:
            hist_block = ""
            print(f"主动消息：读取会话历史失败（忽略）: {type(e).__name__}: {e}")
        if hist_block:
            print(f"主动消息：已带上会话 {session_id} 的聊天历史（{len(hist_block)} 字），"
                  "开场白会承接上次话题。")
        try:
            text = await generate_proactive_text(ctx, instruction, history_block=hist_block)
        except Exception as e:
            print(f"主动消息生成失败: {e}")
            text = ""
        if not text:
            app_context.proactive_pending.pop(session_id, None)
            print(f"主动消息：会话 {session_id} 本轮生成空文本，已重新排队。")
            continue
        if now - _idle_ref(session_id) < idle_seconds:
            app_context.proactive_pending.pop(session_id, None)
            print(f"主动消息：会话 {session_id} 在生成期间有互动，取消本次发送。")
            continue
        if not session_memory_exists(session_id):
            forget_proactive_session(session_id)
            continue
        try:
            use_voice = bool(app_context.global_config.get("proactive_voice", False))
            use_sticker = bool(app_context.global_config.get("proactive_sticker", False))
            if app_context.affection_mgr is not None \
                    and bool(app_context.global_config.get("affection_stage_unlocks_enabled", True)) \
                    and session_type == "private":
                stage = app_context.affection_mgr.stage(get_active_ctx().character_key, str(target_id),
                                            f"private_{target_id}")
                if stage == "恋人":
                    use_voice = True
                elif stage == "暧昧" and app_context.sticker_mgr is not None:
                    use_sticker = True
            with app_context.sender.for_session(session_id):
                ok = await app_context.sender.speak_and_send(
                    session_type, target_id, text, get_active_emotions(), ctx,
                    use_voice=use_voice,
                    sticker=use_sticker,
                session_id=session_id)
        except Exception as e:
            app_context.proactive_pending.pop(session_id, None)
            print(f"主动消息发送失败（{session_id}）: {type(e).__name__}: {e}")
            continue
        if not ok:
            app_context.proactive_pending.pop(session_id, None)
            print(f"主动消息：会话 {session_id} 发送未成功（客户端可能未连接），本轮放弃。")
            continue
        app_context.proactive_pending.pop(session_id, None)
        app_context.last_proactive_sent[session_id] = now
        app_context.last_interaction[session_id] = now
        app_context.proactive_counts[f"{today}|{session_id}"] = used + 1
        app_context.proactive_awaiting.add(session_id)
        save_proactive_state()
        print(f"已向 {session_id} 发送主动消息（今日第 {used + 1}/{max_per_day} 条）：{text[:40]}"
              + ("（等待用户回复，回复前不再主动）" if wait_reply else ""))


async def _companion_send(session_id: str, role: dict, instruction: str) -> bool:
    if app_context.sender is None or app_context.sender.client is None:
        return False
    mem = role_memory(role)
    if mem is None or not mem.get_memory_file(session_id).exists():
        return False
    ctx = reply_ctx_of(role, session_id=session_id)
    try:
        hist_block = dialog_history_block(
            mem.load_history(session_id), ctx, session_id=session_id)
    except Exception as e:
        hist_block = ""
        print(f"陪伴消息：读取会话历史失败（忽略）: {type(e).__name__}: {e}")
    try:
        text = await generate_proactive_text(ctx, instruction, history_block=hist_block)
    except Exception as e:
        print(f"陪伴消息生成失败: {type(e).__name__}: {e}")
        return False
    if not str(text or "").strip():
        return False
    session_type, target_id = parse_session_target(session_id)
    try:
        with app_context.sender.for_session(session_id):
            return bool(await app_context.sender.speak_and_send(
                session_type, target_id, text, get_role_emotions(role), ctx,
                use_voice=False, sticker=False, session_id=session_id))
    except Exception as e:
        print(f"陪伴消息发送失败（{session_id}）: {type(e).__name__}: {e}")
        return False


def _detect_rival_mention(text: str, character_key: str) -> str:
    roles = getattr(app_context.global_config, "roles", None)
    if roles is None and isinstance(app_context.global_config, dict):
        roles = app_context.global_config.get("roles") or {}
    names = []
    for key, role in (roles or {}).items():
        if key == character_key:
            continue
        name = str((role or {}).get("character_name") or "").strip()
        if name:
            names.append(name)
    for name in sorted(names, key=len, reverse=True):
        if name and name in str(text or ""):
            return name
    return ""


def _to_float(value, default: float) -> float:
    try:
        if isinstance(value, bool):
            return default
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _mood_bounds_now() -> tuple:
    from modules.mood import mood_bounds
    return mood_bounds(RoleContext(app_context.global_config.config if app_context.global_config else {}, {}))


MOOD_DIARY_CATCHUP_DAYS = 7
MOOD_DIARY_MAX_SESSIONS_PER_DAY = 3
MOOD_DIARY_SILENT_MILESTONES = (1, 2, 3, 7, 14, 30, 60, 90, 180, 365)
MOOD_DIARY_MAX_DELIVER_PER_RUN = 3
DIARY_MAX_OVERLAP = 0.7

# 超上限时重写用的补充要求
DIARY_NO_COPY_WARNING = (
    "\n\n上一次你把自己当天说过的话原样（或换个说法）抄了一遍，那不是日记。"
    "重写：不要出现上面任何一句话的原话或近似说法，也不要再问候、提问对方；"
    "只用回忆的口吻写自己那天的心情与想法。"
)

MOOD_DIARY_MAX_AGE_DAYS = 1
_diary_writing = False


_REAL_SESSION_RE = re.compile(r"^(?:private|group)_.+$")


def _is_real_session(session_id: str) -> bool:
    return bool(_REAL_SESSION_RE.match(str(session_id or "")))


def _diary_material(session_id: str, role: dict, day: str, limit: int = 40) -> list:
    mem = role_memory(role)
    if mem is None:
        return []
    try:
        history = mem.load_history(session_id)
    except Exception as e:
        print(f"心情日记：读取会话历史失败（忽略）: {type(e).__name__}: {e}")
        return []
    start, end = MoodManager._day_bounds(day)
    if start <= 0:
        return []
    day_msgs = []
    for msg in history:
        if not isinstance(msg, dict) or msg.get("role") not in ("user", "assistant"):
            continue
        ts = _to_float(msg.get("timestamp", 0), 0.0)
        if start <= ts < end and str(msg.get("content", "")).strip():
            day_msgs.append(msg)
    return speaker_labeled_lines(day_msgs, limit=limit)


def _diary_overlap(text: str, lines: list) -> float:
    best = 0.0
    for line in lines or []:
        body = re.sub(r"^[^:：]{1,14}[:：]\s*", "", str(line))
        if len(_norm_text(body)) < 6:
            continue
        best = max(best, _repeat_ratio(body, text))
    return best


def _private_sessions_of(role: dict) -> list:
    mem = role_memory(role)
    if mem is None:
        return []
    prefix = f"{mem.character_key}_private_"
    out = []
    for f in sorted(mem.data_path.glob("*.json")):
        if not f.name.startswith(prefix) or not _is_memory_filename(f.name):
            continue
        session_id = _memory_session_id(f.name)
        if session_id and session_id not in out:
            out.append(session_id)
    return out


def _session_last_user_day(role: dict, session_id: str) -> str:
    mem = role_memory(role)
    if mem is None:
        return ""
    try:
        history = mem.load_history(session_id)
    except Exception:
        return ""
    for msg in reversed(history or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        ts = _to_float(msg.get("timestamp", 0), 0.0)
        if ts > 0:
            return time.strftime("%Y-%m-%d", time.localtime(ts))
    return ""


async def write_silent_mood_diaries(roles: dict) -> int:
    if app_context.mood_mgr is None:
        return 0
    today = time.strftime("%Y-%m-%d")
    oldest = time.strftime(
        "%Y-%m-%d", time.localtime(time.time() - MOOD_DIARY_CATCHUP_DAYS * 86400))
    written = 0
    for key, role in roles.items():
        for session_id in _private_sessions_of(role):
            if not _is_real_session(session_id):
                continue
            last_day = _session_last_user_day(role, session_id)
            if not last_day:
                continue
            try:
                base = time.mktime(time.strptime(last_day, "%Y-%m-%d"))
            except ValueError:
                continue
            for step in MOOD_DIARY_SILENT_MILESTONES:
                day = time.strftime("%Y-%m-%d", time.localtime(base + step * 86400))
                if day >= today or day < oldest:
                    continue
                if app_context.mood_mgr.has_diary(key, day, session_id):
                    continue
                try:
                    ctx = reply_ctx_of(role, session_id=session_id)
                    instruction = (
                        f"你在写 {day} 这一天只给自己看的日记，别人不会看到它。"
                        f"对方从 {last_day} 之后就没再跟你说过话，{day} 是断联的第 {step} 天，"
                        "这天也没有任何对话。"
                        "只写你这一天的状态和想法：想不想对方、自己做了什么、有没有在意的事，"
                        "也可以只是发呆；不要编造你们之间发生过的对话，"
                        "也不要提到任何约定、承诺或对方说过的话——那天你们根本没有说话。"
                        "用你自己的口吻写几句话，只输出日记正文。")
                    text = await generate_proactive_text(ctx, instruction)
                    if not str(text or "").strip():
                        continue
                    app_context.mood_mgr.add_diary(key, day, text, {"count": 0}, session_id=session_id)
                    written += 1
                    print(f"心情日记：已为 {role.get('character_name') or key} "
                          f"写好 {day}（断联第 {step} 天，会话 {session_id}）的日记。")
                except Exception as e:
                    print(f"陪伴检查：沉默期日记异常（{key}/{session_id}）: "
                          f"{type(e).__name__}: {e}")
    return written


async def write_mood_diaries(days: int = MOOD_DIARY_CATCHUP_DAYS) -> int:
    global _diary_writing
    if app_context.mood_mgr is None or not bool(app_context.global_config.get("mood_diary_enabled", True)):
        return 0
    if _diary_writing:
        return 0
    _diary_writing = True
    written = 0
    try:
        roles = getattr(app_context.global_config, "roles", None) or {}
        now = time.time()
        for offset in range(1, max(1, int(days)) + 1):
            day = time.strftime("%Y-%m-%d", time.localtime(now - offset * 86400))
            for key, role in roles.items():
                try:
                    sessions = app_context.mood_mgr.sessions_for_day(key, day)
                except Exception as e:
                    print(f"心情日记：会话反查失败（{key}）: {type(e).__name__}: {e}")
                    continue
                for session_id in sessions[:MOOD_DIARY_MAX_SESSIONS_PER_DAY]:
                    try:
                        if not _is_real_session(session_id):
                            continue
                        if app_context.mood_mgr.has_diary(key, day, session_id):
                            continue
                        lines = _diary_material(session_id, role, day)
                        if len(lines) < 2:
                            continue
                        points = [p for p in app_context.mood_mgr.mood_points_for_day(key, day, session_id)]
                        values = [v for _ts, v in points] or [0]
                        lo, hi = _mood_bounds_now()
                        summary = {"count": len(values),
                                   "min": round(min(values)), "max": round(max(values)),
                                   "first": round(points[0][1]) if points else 0,
                                   "last": round(points[-1][1]) if points else 0}
                        ctx = reply_ctx_of(role, session_id=session_id)
                        instruction = (
                            f"你在写 {day} 这一天只给自己看的日记，别人不会看到它。"
                            "下面是你那天和对方的对话记录：只写其中真实发生过的事，"
                            "不要编造没发生的经历，也不要写你当时无从知道的事；"
                            "可以写自己的心情、想法和在意的地方。"
                            f"（那天心情值记录 {summary['count']} 次，"
                            f"最低 {summary['min']}、最高 {summary['max']}，"
                            f"{lo}~{hi} 是正常范围。）"
                            "用你自己的口吻写几句话，只输出日记正文："
                            "像写给自己看的那样用回忆的口吻，不要问候对方、不要提问，"
                            "也不要把当天说过的话再抄一遍。\n\n"
                            + "\n".join(lines))
                        text = ""
                        overlap = 1.0
                        prompt_text = instruction
                        for _attempt in range(2):
                            text = str(await generate_proactive_text(ctx, prompt_text) or "").strip()
                            if not text:
                                break
                            overlap = _diary_overlap(text, lines)
                            if overlap < DIARY_MAX_OVERLAP:
                                break
                            print(f"心情日记：{session_id} {day} 的日记与当天说过的话重合 "
                                  f"{overlap:.2f}，判为照抄原有台词，重写一次。")
                            prompt_text = instruction + DIARY_NO_COPY_WARNING
                        if not text:
                            continue
                        if overlap >= DIARY_MAX_OVERLAP:
                            print(f"心情日记：{session_id} {day} 重写后仍在照抄当天的话，"
                                  "这天先不写（免得发出不像日记的内容）。")
                            continue
                        app_context.mood_mgr.add_diary(key, day, text, summary, session_id=session_id)
                        written += 1
                        print(f"心情日记：已为 {role.get('character_name') or key} "
                              f"写好 {day}（会话 {session_id}）的日记。")
                    except Exception as e:
                        print(f"陪伴检查：心情日记异常（{key}/{session_id}）: "
                              f"{type(e).__name__}: {e}")
        written += await write_silent_mood_diaries(roles)
        await deliver_mood_diaries()
    finally:
        _diary_writing = False
    return written


async def deliver_mood_diaries() -> int:
    if app_context.mood_mgr is None or app_context.sender is None or app_context.sender.client is None:
        return 0
    if _in_quiet_hours():
        return 0
    roles = getattr(app_context.global_config, "roles", None) or {}
    today = time.strftime("%Y-%m-%d")
    fresh_after = time.strftime(
        "%Y-%m-%d", time.localtime(time.time() - MOOD_DIARY_MAX_AGE_DAYS * 86400))
    sent = 0
    used_sessions = set()
    pool = app_context.mood_mgr.unsent_diaries(MOOD_DIARY_MAX_DELIVER_PER_RUN * 4)
    for character_key, entry in pool:
        if sent >= MOOD_DIARY_MAX_DELIVER_PER_RUN:
            break
        role = roles.get(character_key)
        session_id = str(entry.get("session_id") or "")
        if role is None or not session_id or session_id in used_sessions:
            continue
        date = str(entry.get("date") or "")
        if date and date < fresh_after and date != today:
            continue
        session_type, target_id = parse_session_target(session_id)
        ctx = reply_ctx_of(role, session_id=session_id)
        try:
            diary_text = str(entry.get("text") or "").strip()
            if not diary_text:
                continue
            # 重发一次主人就会收到两篇同样的日记
            with app_context.sender.for_session(session_id), app_context.sender.send_once():
                ok = await app_context.sender.speak_and_send(
                    session_type, target_id, f"【日记】{diary_text}",
                    get_role_emotions(role), ctx, use_voice=False, sticker=False,
                    session_id=session_id, record_history=False)
            uncertain = bool(app_context.sender.last_send_uncertain)
        except Exception as e:
            print(f"心情日记发送失败（{session_id}）: {type(e).__name__}: {e}")
            continue
        if not ok and not uncertain:
            print(f"心情日记：会话 {session_id} 发送未成功，下次再发。")
            continue
        used_sessions.add(session_id)
        app_context.mood_mgr.mark_diary_sent(character_key, date, session_id)
        sent += 1
        name = role.get("character_name") or character_key
        if uncertain:
            print(f"心情日记：{name} {date} 的日记发送超时、是否送达未知，"
                  f"按已发送处理不再补发（{session_id}）。")
        else:
            print(f"心情日记：{name} {date} 的日记已发给 {session_id}。")
    return sent


def mood_diary_note(ctx: RoleContext, session_id: str = "", limit: int = 1) -> str:
    if app_context.mood_mgr is None or not bool(app_context.global_config.get("mood_diary_enabled", True)):
        return ""
    entries = (app_context.mood_mgr.get_diary(limit=limit, session_id=session_id)
               or {}).get(ctx.character_key) or []
    rows = [f"{e.get('date')}：{e.get('text')}" for e in entries if e.get("text")]
    if not rows:
        return ""
    return ("【你自己写的日记】这是你自己写给自己看的日记，别人看不到，也没有人会跟你提起它：\n"
            + "\n".join(rows))


async def mood_diary_catchup_task():
    if not app_context.global_config.get("scheduler_enabled", True):
        return
    try:
        written = await _guarded(write_mood_diaries)
        if written:
            print(f"心情日记：启动补写完成，共补上 {written} 篇。")
    except Exception as e:
        print(f"心情日记启动补写异常（忽略）: {type(e).__name__}: {e}")


async def companion_daily_check() -> int:
    if app_context.global_config is None:
        return 0
    today = time.strftime("%Y-%m-%d")
    roles = getattr(app_context.global_config, "roles", None) or {}
    sent = 0
    if _in_quiet_hours():
        print("陪伴检查：当前处于静默时段，跳过需要发言的部分（衰减/日记/奇遇照常）。")

    if app_context.affection_mgr is not None \
            and bool(app_context.global_config.get("affection_decay_enabled", True)):
        try:
            dropped = app_context.affection_mgr.decay_idle(
                int(app_context.global_config.get("affection_decay_idle_days", 3)),
                int(app_context.global_config.get("affection_decay_daily_drop", 1)))
            if dropped:
                print(f"陪伴检查：{dropped} 位久未互动的用户好感已流失。")
        except Exception as e:
            print(f"陪伴检查：好感流失执行异常: {type(e).__name__}: {e}")

    if app_context.mood_mgr is not None and bool(app_context.global_config.get("mood_env_enabled", True)):
        try:
            lo, _hi = _mood_bounds_now()
            dropped = app_context.mood_mgr.decay_lonely(
                int(app_context.global_config.get("mood_env_lonely_days", 3)),
                _to_float(app_context.global_config.get("mood_env_lonely_drop", 2), 2.0), lo)
            if dropped:
                print(f"陪伴检查：{dropped} 个冷清会话的心情已走低。")
        except Exception as e:
            print(f"陪伴检查：心情孤独衰减执行异常: {type(e).__name__}: {e}")

    if app_context.encounter_mgr is not None:
        default_client = app_context.sender.client if app_context.sender is not None else None
        for key, role in roles.items():
            try:
                if not app_context.encounter_mgr.should_roll(key, today):
                    continue
                ctx = reply_ctx_of(role, client=default_client)
                text = await generate_proactive_text(
                    ctx, "给自己想一件今天发生的小遭遇：日常、具体、一两句话，"
                         "符合你的人设与你现在的生活。只输出这件事本身，"
                         "不要提到这段说明，也不要写台词。")
                app_context.encounter_mgr.set_event(key, today, text)
                if str(text or "").strip():
                    print(f"陪伴检查：已为 {role.get('character_name') or key} "
                          f"想好今天的奇遇。")
            except Exception as e:
                print(f"陪伴检查：奇遇安排异常（{key}）: {type(e).__name__}: {e}")

    await write_mood_diaries()

    # 以下需要发言：静默时段直接跳过
    if _in_quiet_hours() or app_context.sender is None or app_context.sender.client is None:
        return sent

    if app_context.affection_mgr is not None and app_context.sender is not None:
        milestone_days = app_context.affection_mgr.anniversary_days()
        for key, by_user in list(app_context.affection_mgr.records.items()):
            role = roles.get(key)
            if role is None or not milestone_days:
                continue
            for scope in list((by_user or {}).keys()):
                try:
                    session_id, user_id = app_context.affection_mgr.split_scope(scope)
                    days = app_context.affection_mgr.anniversary_due(key, user_id, session_id)
                    if not days:
                        continue
                    target_session = session_id or f"private_{user_id}"
                    ok = await _companion_send(
                        target_session, role,
                        f"今天是你和对方在一起的第 {days} 天纪念日。"
                        "主动提起这个日子，自然地表达你的心意。")
                    if ok:
                        app_context.affection_mgr.mark_anniversary_sent(key, user_id, days, session_id)
                        sent += 1
                        print(f"陪伴检查：已向 {user_id} 送出 {days} 天纪念日祝贺"
                              f"（{role.get('character_name') or key}）。")
                except Exception as e:
                    print(f"陪伴检查：纪念日检查异常（{key}/{user_id}）: "
                          f"{type(e).__name__}: {e}")

    if app_context.promise_mgr is not None and bool(app_context.global_config.get("promise_enabled", True)):
        try:
            limit = max(0, int(app_context.global_config.get("promise_max_per_day", 3)))
        except (TypeError, ValueError):
            limit = 3
        min_age = _to_float(app_context.global_config.get("promise_fulfill_days", 1), 1.0)
        for promise in app_context.promise_mgr.due(min_age, limit):
            role = roles.get(str(promise.get("character_key") or ""))
            session_id = str(promise.get("session_id") or "")
            if role is None or not session_id:
                continue
            ok = await _companion_send(
                session_id, role,
                f"你之前答应过对方一件事：「{promise.get('content')}」"
                "（这件事是你答应要去做的，不是你要求对方做的）。"
                "现在主动兑现这个承诺：自然地提起它，并把答应的事当场做到。")
            if ok:
                app_context.promise_mgr.mark_fulfilled(promise)
                sent += 1
                print(f"陪伴检查：已兑现承诺「{promise.get('content')}」"
                      f"（{promise.get('character_key')} → {session_id}）。")
    return sent


async def companion_catchup_task():
    if not app_context.global_config.get("scheduler_enabled", True):
        return
    try:
        hh, mm = str(app_context.global_config.get("companion_check_time", "08:05") or "08:05").split(":")[:2]
        check_minutes = int(hh) * 60 + int(mm)
    except (TypeError, ValueError):
        check_minutes = 8 * 60 + 5
    lt = time.localtime()
    if lt.tm_hour * 60 + lt.tm_min < check_minutes:
        return
    waited = 0
    while app_context.sender is None or app_context.sender.client is None:
        if waited >= 300:
            print("陪伴检查补跑：等待 NapCat 连接已超过 5 分钟，本次跳过发言部分。")
            break
        await asyncio.sleep(10)
        waited += 10
    sent = await _guarded(companion_daily_check)
    if sent:
        print(f"陪伴检查补跑完成：发出主动消息 {sent} 条。")


async def _extract_and_store_promise(ctx: RoleContext, session_id: str,
                                     user_id: str, reply_text: str):
    try:
        promise = await extract_promise(ctx, reply_text)
        if promise and app_context.promise_mgr is not None:
            added = app_context.promise_mgr.add(ctx.character_key, session_id, user_id, promise)
            if added:
                print(f"承诺追踪：{ctx.character_key} 新承诺「{promise}」已记录。")
    except Exception as e:
        print(f"承诺提取失败（忽略）: {type(e).__name__}: {e}")


def _summary_keep_count() -> int:
    try:
        keep = int(app_context.global_config.get("summary_max_history", 5))
    except (TypeError, ValueError):
        keep = 5
    try:
        hl = int(app_context.global_config.get("history_length", 8))
    except (TypeError, ValueError):
        hl = 8
    if hl > 0:
        keep = max(keep, hl)
    return max(1, keep)


def dialog_history_block(history: list, ctx=None, session_id: str = "",
                         recent: int = None, summary_chars: int = None,
                         max_chars: int = None) -> str:
    msgs = [m for m in (history or []) if isinstance(m, dict)
            and m.get("role") in ("user", "assistant")
            and str(m.get("content", "")).strip()]
    if not msgs:
        return ""
    try:
        recent = max(0, int(recent if recent is not None
                            else (ctx.get("history_context_recent", 6) if ctx else 6) or 6))
    except (TypeError, ValueError):
        recent = 6
    try:
        summary_chars = max(0, int(summary_chars if summary_chars is not None
                                   else (ctx.get("history_context_summary_chars", 400)
                                         if ctx else 400) or 400))
    except (TypeError, ValueError):
        summary_chars = 400
    try:
        max_chars = max(200, int(max_chars if max_chars is not None
                                 else (ctx.get("history_context_max_chars", 1600)
                                       if ctx else 1600) or 1600))
    except (TypeError, ValueError):
        max_chars = 1600

    summary = ""
    if isinstance(ctx, RoleContext):
        summary = str(ctx.get("dialog_summary", "") or "").strip()
    if not summary and session_id and app_context.memory_manager is not None:
        try:
            mem = memory_for_session(session_id) or app_context.memory_manager
            meta = (mem.load_session_data(session_id) or {}).get("meta", {}) or {}
            summary = str(meta.get("summary", "") or "").strip()
        except Exception:
            summary = ""

    recent_msgs = msgs[-recent:] if recent > 0 else []
    older_msgs = msgs[:len(msgs) - len(recent_msgs)] if recent_msgs else msgs
    if summary:
        older_msgs = []          # 摘要已覆盖早期内容，不再重复描述
    elif older_msgs and len(older_msgs) > 6:
        older_msgs = older_msgs[:3] + older_msgs[-3:]

    header = ("【对话历史】以下是主人和你之前的真实聊天记录，"
              "本次发言必须承接这些内容（延续上次的话题、语气和称呼），"
              "绝对不要凭空换一个不相干的话题。")
    parts = [header]
    if summary:
        parts.append("较早的对话摘要：" + summary[:summary_chars])
    if older_msgs:
        brief = []
        for msg in older_msgs:
            who = "主人" if msg.get("role") == "user" else "你"
            brief.append(f"{who}：{str(msg.get('content', '')).strip()[:60]}")
        parts.append("更早的对话（简略）：" + "；".join(brief))
    if recent_msgs:
        lines = []
        for msg in recent_msgs:
            who = "主人" if msg.get("role") == "user" else "你"
            lines.append(f"{who}：{str(msg.get('content', '')).strip()[:200]}")
        parts.append("最近的对话（按时间顺序，全文）：\n" + "\n".join(lines))
    else:
        parts.append("（还没有更早的对话记录，正常开场即可。）")
    return "\n".join(parts)[:max_chars]


def session_history_block(session_key: str, ctx=None) -> str:
    if app_context.memory_manager is None:
        return ""
    try:
        mem = memory_for_session(session_key) or app_context.memory_manager
        history = mem.load_history(session_key)
    except Exception as e:
        print(f"问候历史读取失败（忽略）: {type(e).__name__}: {e}")
        return ""
    return dialog_history_block(history, ctx, session_id=session_key)


def _known_sessions() -> list:
    out = []
    seen = set()
    try:
        for f in app_context.memory_manager.data_path.glob("*.json"):
            m = re.match(r"^[A-Za-z0-9_\-]+_(private|group)_([A-Za-z0-9_\-]+)\.json$", f.name)
            if not m or (m.group(1), m.group(2)) in seen:
                continue
            seen.add((m.group(1), m.group(2)))
            out.append((m.group(1), m.group(2)))
    except Exception as e:
        print(f"解析已知会话列表失败: {e}")
    return out


async def greeting_daily_check() -> int:
    if _in_quiet_hours():
        print("问候检查：当前处于静默时段，跳过（避免深夜打扰）。")
        return 0
    if app_context.event_mgr and app_context.sender:
        return await app_context.event_mgr.check_and_greet(app_context.sender, get_active_ctx, get_active_emotions,
                                               sessions_provider=_known_sessions,
                                               history_provider=session_history_block) or 0
    print(f"问候检查：事件管理器或发送器未就绪，跳过（event_mgr={app_context.event_mgr is not None}, "
          f"sender={app_context.sender is not None}）。")
    return 0


async def greeting_catchup_task():
    if not app_context.global_config.get("greeting_catchup_enabled", True):
        print("问候补发：未开启（greeting_catchup_enabled=false），本次不补发。")
        return
    if not app_context.global_config.get("scheduler_enabled", True):
        print("问候补发：调度总开关已关闭（scheduler_enabled=false），本次不补发。")
        return
    try:
        deadline_minutes = max(1.0, float(app_context.global_config.get("greeting_catchup_deadline_minutes", 30) or 30))
    except (TypeError, ValueError):
        deadline_minutes = 30.0
    wait_until = time.time() + deadline_minutes * 60
    waited = 0.0
    while app_context.sender is None or app_context.sender.client is None:
        if time.time() >= wait_until:
            print(f"问候补发：等待 NapCat 连接已超过 {deadline_minutes:g} 分钟仍未连接，"
                  "本次跳过（下次启动或连接成功后仍会补发）。")
            return
        await asyncio.sleep(10)
        waited += 10
        if int(waited) % 60 == 0:
            print(f"问候补发：等待 NapCat 连接中…（已等 {int(waited)} 秒）")
    if _in_quiet_hours():
        print("问候补发：当前处于静默时段，跳过今天的补发。")
        return
    only_jobs = not app_context.global_config.get("greeting_events_enabled", False) \
        and not app_context.global_config.get("birthday_greeting_enabled", False)
    if only_jobs:
        print("问候补发：节日问候与生日祝福都未开启，本次只补跑每日定时任务。")
    print(f"问候补发：程序启动时已过问候相关时刻（已等待 NapCat {int(waited)} 秒），"
          "开始检查今天漏掉的问候。")
    sent_events = 0
    try:
        hh, mm = str(app_context.global_config.get("greeting_check_time", "08:00")
                     or "08:00").split(":")[:2]
        check_minutes = int(hh) * 60 + int(mm)
    except Exception:
        check_minutes = 8 * 60
    lt = time.localtime()
    now_minutes = lt.tm_hour * 60 + lt.tm_min
    if now_minutes >= check_minutes:
        if not only_jobs:
            try:
                sent_events = await _guarded(greeting_daily_check)
            except Exception as e:
                print(f"问候补发执行异常: {type(e).__name__}: {e}")
    else:
        print(f"问候补发：当前 {time.strftime('%H:%M')} 还没到问候时刻"
              f"（{app_context.global_config.get('greeting_check_time', '08:00')}），"
              "节日/生日问候交给每日定时任务。")
    sent_jobs = 0
    if app_context.job_mgr is not None:
        try:
            sent_jobs = await _guarded(app_context.job_mgr.catch_up_missed_daily)
        except Exception as e:
            print(f"问候补发：补跑每日定时任务异常: {type(e).__name__}: {e}")
    if sent_events or sent_jobs:
        print(f"问候补发完成：节日/生日问候 {sent_events} 条，每日定时任务 {sent_jobs} 条。")
    else:
        print("问候补发完成：今天没有需要补发的问候"
              "（没有节日/生日命中，也没有漏掉的每日问候任务）。")


_SEND_TASK_LOCKS: dict = {}


def send_task_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _SEND_TASK_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _SEND_TASK_LOCKS[loop] = lock
    return lock


async def _guarded(coro_factory):
    async with send_task_lock():
        return await coro_factory()


def register_feature_jobs():
    if not app_context.global_config.get("scheduler_enabled", True):
        app_context.scheduler.remove_job("proactive_idle")
        app_context.scheduler.remove_job("greeting_check")
        app_context.scheduler.remove_job("companion_check")
        print("调度总开关已关闭：主动消息与问候检查不运行（待办提醒不受影响）。")
        return
    if app_context.global_config.get("proactive_enabled", False):
        try:
            seconds = max(30, int(app_context.global_config.get("proactive_check_seconds", 300)))
        except (TypeError, ValueError):
            seconds = 300
        app_context.scheduler.add_job("proactive_idle", "主动消息检查",
                          {"type": "interval", "seconds": seconds},
                          lambda: _guarded(proactive_idle_check))
        print(f"主动消息检查已开启（每 {seconds} 秒，闲置阈值 {app_context.global_config.get('proactive_idle_minutes', 30)} 分钟）。")
    else:
        app_context.scheduler.remove_job("proactive_idle")
    if app_context.global_config.get("greeting_events_enabled", False) \
            or app_context.global_config.get("birthday_greeting_enabled", False):
        app_context.scheduler.add_job("greeting_check", "节日生日问候检查",
                          {"type": "daily", "time": app_context.global_config.get("greeting_check_time", "08:00")},
                          lambda: _guarded(greeting_daily_check))
        print(f"节日/生日问候检查已开启（每日 {app_context.global_config.get('greeting_check_time', '08:00')}）。")
    else:
        app_context.scheduler.remove_job("greeting_check")
    try:
        check_time = str(app_context.global_config.get("companion_check_time", "08:05") or "08:05")
        app_context.scheduler.add_job("companion_check", "每日陪伴检查",
                          {"type": "daily", "time": check_time},
                          lambda: _guarded(companion_daily_check))
        print(f"每日陪伴检查已开启（每天 {check_time}：冷落流失 / 纪念日 / 心情日记 / 奇遇 / 承诺兑现）。")
    except Exception as e:
        print(f"每日陪伴检查注册失败: {type(e).__name__}: {e}")


def hot_reload_managers():
    if app_context.sticker_mgr is not None:
        app_context.sticker_mgr = StickerManager(app_context.global_config)
        if app_context.sender is not None:
            app_context.sender.sticker_manager = app_context.sticker_mgr
    app_context._role_emotions_cache.clear()
    app_context._role_mimics_cache.clear()
    if app_context.lexicon_mgr is not None:
        app_context.lexicon_mgr.config = app_context.global_config
    if app_context.recall_mgr is not None:
        app_context.recall_mgr.config = app_context.global_config
    if app_context.encounter_mgr is not None:
        app_context.encounter_mgr.config = app_context.global_config
    register_feature_jobs()
    if app_context.job_mgr is not None:
        app_context.job_mgr.reload()
    if app_context.sender is not None:
        app_context.sender.config = app_context.global_config

