# -*- coding: utf-8 -*-
# 回复生成管线（自 main.py 搬迁）：SentenceSink 流式逐句发送、防复读系列、
# 工具调用与搜索链接补发、表情判定、generate_reply/stream、心情判定入口。
import asyncio
import json
import re
import time
from pathlib import Path
from typing import Optional, List

from modules import app_context
from modules.adapters import client_supports
from modules.sender import (VoicePacer, voice_enabled_for, recall_delay_of,
                            DEFAULT_RECALL_DELAY_SECONDS, RECALL_DENY)
from modules.tts import synthesize_sentence
from modules.tts_service import ensure_tts_service
from modules.llm_helpers import (
    DELIVERY_PLAIN,
    SENTENCE_ACTION_KEYS,
    RoleContext,
    SentenceStreamParser,
    segment_for_tts,
    apply_literal_mention,
    asks_self_context,
    available_mimics,
    build_chat_messages,
    chat_once,
    chat_with_tools,
    error_reply_text,
    extract_json,
    get_image_reply,
    image_identity_note,
    image_self_claim,
    is_search_dissatisfied,
    is_search_request,
    lang_text_broken,
    normalize_sentences,
    normalize_single,
    record_sent_links,
    sent_links,
    sentence_obj_has_action,
    sentence_obj_has_text,
    split_multi_clause_sentences,
    stream_chat,
    strip_mention_placeholder,
    strip_quote_note,
    text_needs_tools,
    translate_to_lang)


# ============================================================================
# 回复生成（含流式）与句子发送
# ============================================================================

async def _poke_with_receipt(sender, session_type, target_id, target) -> dict:
    """戳一戳并拿回执；发送端没提供回执接口时退回布尔结果。

    只认布尔结果的话，"被挡下的那次戳一戳"在链路上完全无声：
    模型以为戳了、判定以为没戳。回执让上层拿到真实结果。
    """
    helper = getattr(sender, "_poke_and_receipt", None)
    if helper is not None:
        return await helper(session_type, target_id, target)
    ok = await sender.send_poke(session_type, target_id, target)
    return {"action": "戳一戳", "ok": bool(ok), "at": time.time()}


class SentenceSink:
    """流式回复的逐句发送器：句子入队，后台工作线程按顺序合成+发送。"""

    def __init__(self, session_type, target_id, emotions, ctx, last_reply="", user_text="",
                 reply_id=None, allowed_at_ids=None, poke_target=None,
                 recall_request="", at_names=None, speaker_id="", repeat_requested=False):
        self.session_type = session_type
        self.reply_id = reply_id
        self.allowed_at_ids = {str(q) for q in (allowed_at_ids or [])}
        # 昵称映射只在这一层用来把台词里写成文字的 @ 认出来，不进提示词
        self.at_names = {str(q): str(n) for q, n in (at_names or {}).items()}
        self.speaker_id = str(speaker_id or "")
        self.actions_pending = True
        self.poke_target = poke_target
        self.poke_pending = True
        # 模型要求撤回时：记下这条回复发出去的消息 id，收尾时按延迟挂撤回任务。
        # 主人明确让撤回（recall_request="next"）时同样生效，不看模型填没填。
        self.recall_pending = False
        self.recall_delay = DEFAULT_RECALL_DELAY_SECONDS
        self.recall_request = str(recall_request or "")
        # 要撤的是"声明撤回的那一句"发出的消息（含它的语音），不是整轮拆出来的全部
        self.recall_ids: List[str] = []
        # 动作执行回执（戳一戳等）：判定与下一轮上下文据此知道动作到底做了没有
        self.action_receipts: List[dict] = []
        self.sent_ids: List[str] = []
        # 模型指定了发送形态（逐字/纯文字）：逐句流式发会破坏它，
        # 整条让给批量发送路径处理
        self.batch_only = False
        # 已交给后台发送的句子数。判断"是不是第一句"只能看它：sent 由后台工作器
        # 异步累加，第一句还在合成时第二句就到了，那时 sent 仍是 0
        self.queued = 0
        # 后面的句子又带发送形态时只提示一次
        self.delivery_ignored = False
        self.target_id = target_id
        self.emotions = emotions
        self.ctx = ctx
        refs = last_reply if isinstance(last_reply, (list, tuple)) else [last_reply]
        self.last_replies = [str(r) for r in refs if str(r or "").strip()]
        self.last_reply = self.last_replies[0] if self.last_replies else ""
        self.user_text = user_text or ""
        # 用户明确要求复述时，回复与用户的话/上一轮的话重合是正常的，不做防复读拦截
        self.repeat_requested = bool(repeat_requested)
        self.queue: asyncio.Queue = asyncio.Queue()
        self.worker: Optional[asyncio.Task] = None
        self.sent = 0
        self.sent_sentences: List[dict] = []  # 已成功发送的句子（流式中断时用于入库）
        self.sent_texts: List[str] = []       # 逐条发出去的文本（聊天记录按它拆条）
        self.unsent: List[str] = []           # 发送失败的句子，收尾时整段重发
        self._tts_ok: Optional[bool] = None
        self.tts_ms = 0.0
        self.tts_calls = 0
        self.blocked = False
        self._first_checked = False
        self.pending_sticker_emotion = None
        self.pending_sticker_text = ""
        self.sticker_sent = False
        # 构造时就把防复读开关定下来：流式发送过程中配置不会变，
        # 逐句重读既要保证一致，也省得每次都走一遍配置读取
        self.guard = repeat_guard_flags(ctx)
        self.thresholds = repeat_thresholds(ctx)
        self.pacer = VoicePacer(bool(app_context.global_config.get("dynamic_sleep", True)))

    def _looks_repeat(self, sentence: dict, flags: dict = None) -> bool:
        zh = str(sentence.get("zh") or "").strip()
        if not zh or getattr(self, "repeat_requested", False):
            return False
        flags = flags or self.guard
        self_threshold, user_threshold = getattr(self, "thresholds", None) \
            or repeat_thresholds()
        if flags.get("compare_self", True):
            if any(_repeat_ratio(ref, zh) >= self_threshold
                   for ref in self.last_replies):
                return True
        if not flags.get("compare_user", True):
            return False
        u = str(self.user_text or "")
        if u and "[图片]" not in u and len(_norm_text(u)) >= _USER_ECHO_MIN_CHARS \
                and _repeat_ratio(u, zh) >= user_threshold:
            return True
        return False

    async def on_sentence(self, sentence: dict):
        if self.batch_only:
            return
        if sentence.get("delivery"):
            if self.queued == 0:
                # 让给批量发送路径：本 sink 一条都不发，主流程看到 sent == 0
                # 就会用 send_reply 走纯文字/逐字那条分支
                self.batch_only = True
                return
            # 发送形态出现在后面的句子上：这条回复已经在流式发了，让位也来不及
            # （已发出去的那句会重复）。照常把剩下的句子发完，别把它们丢掉
            if not self.delivery_ignored:
                self.delivery_ignored = True
                print("发送形态写在后面的句子上，本轮仍按逐句流式发完。")
        if not self._first_checked:
            self._first_checked = True
            flags = self.guard
            if not flags.get("streaming", True):
                print("防复读：流式首句检查已关闭（repeat_guard_streaming_check=false），"
                      "首句不再拦截。")
            elif not repeat_guard_active(flags):
                print("防复读：比对对象全部关闭，流式首句检查无内容可比，跳过。")
            elif self._looks_repeat(sentence, flags):
                self.blocked = True
                print("流式首句疑似复读，已暂停发送，改为整段重新生成。")
                return
        if self.blocked:
            return
        if self.worker is None:
            self.worker = asyncio.create_task(self._run())
        self.queued += 1
        await self.queue.put(sentence)

    async def _run(self):
        while True:
            item = await self.queue.get()
            if item is None:
                return
            try:
                if app_context.global_config and app_context.global_config.get("separate_send", False) \
                        and (app_context.global_config.get("send_voice_separately", False)
                             or app_context.global_config.get("text_separate", False)) \
                        and app_context.global_config.get("separate_force_segment", True):
                    for piece in segment_for_tts([item]):
                        await self._send_one(piece)
                else:
                    await self._send_one(item)
            except Exception as e:
                print(f"流式发送单句异常: {type(e).__name__}: {e}")
            finally:
                self.queue.task_done()

    async def _tts_available(self) -> bool:
        if self._tts_ok is None:
            if not client_supports(app_context.sender._active_client(), "voice"):
                # 微信 ClawBot / QQ 官方只能发文字：语音根本发不出去，
                # 合成一遍只是白白把这条回复拖慢几十秒
                self._tts_ok = False
                print("这条接入方式不支持语音，流式回复只发文字。")
            elif not voice_enabled_for(app_context.global_config, self.session_type):
                # 选择性发送语音：整条回复只掷一次，逐句合成时不会再变
                self._tts_ok = False
            else:
                self._tts_ok = await ensure_tts_service(app_context.global_config)
                if not self._tts_ok:
                    print("警告：TTS 服务不可用，流式回复降级为纯文本。")
        return self._tts_ok and app_context.global_config.get("tts_reply_enabled", True)

    async def _speech_text(self, sentence: dict) -> str:
        """流式合成前的台词语言兜底：模型把展示语言填进台词字段时先重译。"""
        text = str(sentence.get("lang") or "")
        target = str((self.ctx.get("text_lang", "") if self.ctx else "") or "").strip().lower()
        if not target or target == "auto" or not lang_text_broken(text, target):
            return text
        source = strip_mention_placeholder(str(sentence.get("zh") or "").strip() or text)
        try:
            fixed = await asyncio.wait_for(translate_to_lang(self.ctx, source, target),
                                           timeout=30)
        except Exception as e:
            print(f"流式台词语言修复失败（忽略）: {type(e).__name__}: {e}")
            fixed = ""
        fixed = strip_mention_placeholder(fixed)
        if fixed:
            print(f"流式台词语言修复：该句不是{target}，已重译为 {fixed[:40]!r}")
            return fixed
        return text

    async def _send_one(self, sentence: dict):
        if app_context.sticker_mgr and self.sent == 0 and self.pending_sticker_emotion is None:
            self.pending_sticker_emotion = sentence.get("emotion", "")
            self.pending_sticker_text = str(sentence.get("display")
                                            or sentence.get("zh") or "")
        wav = None
        if await self._tts_available():
            start = time.time()
            # 参数顺序：text=要念的台词(sentence["lang"])，emotion=情绪名。
            # 早期版本此处传反，情绪被当成台词、台词被当成情绪，
            # 导致情绪永远回退 default_voice（语音情绪与文本标注不一致）。
            wav = await synthesize_sentence(self.ctx, await self._speech_text(sentence),
                                            sentence.get("emotion", ""),
                                            self.emotions, app_context.memory_manager.data_path,
                                            stats=app_context.stats_mgr,
                                            mimic=sentence.get("mimic", ""),
                                            mimics=available_mimics(self.ctx))
            self.tts_ms += (time.time() - start) * 1000
            if wav:
                self.tts_calls += 1
        shown = str(sentence.get("display") or sentence.get("zh") or "")
        # 台词里写成普通文字的 @ 在这一句里就换成真正的@：字面写法可能出现在后面
        # 某一句上（动作字段只认第一句，字面 @ 不能跟着一起只认第一句）
        if apply_literal_mention(sentence, self.allowed_at_ids, self.at_names, self.speaker_id):
            print("发送：台词里连着写的两个@，已换成真正的@。")
            shown = str(sentence.get("display") or sentence.get("zh") or "")
        mentions = [str(q) for q in (sentence.get("mention_ids") or [])
                    if str(q) in self.allowed_at_ids]
        reply_to = sentence.get("reply_to") is True and self.reply_id is not None
        # 这一句开始发之前的位置：发完用它切出"这一句自己那组消息"，撤回只撤这些
        mark = len(self.sent_ids)
        want_recall = (self.recall_request == "next"
                       or (sentence.get("recall") is True
                           and self.recall_request != RECALL_DENY)) and not self.recall_pending
        if self.poke_pending and sentence.get("poke") is True:
            self.poke_pending = False
            # 戳一戳可能被通道能力挡下：把回执收下来，别让它无声消失
            self.action_receipts.append(await _poke_with_receipt(
                app_context.sender, self.session_type, self.target_id, self.poke_target))
        if wav:
            try:
                # 节奏器只在"还有下一条语音要发"时才真正等：最后一句发完立刻返回，
                # 否则这一段播放等待会一直占着会话锁，拖住排队的下一条消息
                await self.pacer.wait()
                # 这一句的文字与语音算同一条消息的两半：撤回要一起撤
                msg_group = app_context.sender.new_message_group()
                ok = await app_context.sender.send_text(
                    self.session_type, self.target_id, shown,
                    reply_id=self.reply_id if self.actions_pending and reply_to else None,
                    at_ids=mentions,
                    group=msg_group)
                self.actions_pending = False
                self._note_send_result(ok, shown)
                self._track_sent_id()
                await app_context.sender.send_voice(self.session_type, self.target_id, wav,
                                        group=msg_group)
                self._track_sent_id()
                await self.pacer.hold_voice(wav)
            finally:
                Path(wav).unlink(missing_ok=True)
        else:
            ok = await app_context.sender.send_text(
                self.session_type, self.target_id, shown,
                reply_id=self.reply_id if self.actions_pending and reply_to else None,
                at_ids=mentions)
            self.actions_pending = False
            self._note_send_result(ok, shown)
            self._track_sent_id()
        self.sent += 1
        self.sent_sentences.append(sentence)
        # 逐条发出去的文本对应哪句台词：聊天记录据此跟着拆成多条
        self.sent_texts.append(str(sentence.get("zh") or shown))
        if want_recall:
            self.recall_pending = True
            self.recall_delay = recall_delay_of(sentence)
            self.recall_ids = self.sent_ids[mark:]

    def _note_send_result(self, ok: bool, shown: str) -> None:
        """发送失败的句子记下来。

        以前这里不看返回值：发失败的那句照样计入已发送，用户那边就是
        "回复断了一截"甚至"完全没有回复"，而且整段兜底也不会触发。
        """
        if ok:
            return
        self.unsent.append(str(shown or ""))
        print(f"流式发送：这句没发出去，已记为待重发：{str(shown or '')[:30]!r}")

    def _track_sent_id(self) -> None:
        """记下刚发出去那条消息的 id（模型要求撤回时按它撤）。"""
        mid = str(getattr(app_context.sender, "last_message_id", "") or "").strip()
        if mid:
            self.sent_ids.append(mid)

    async def _resend_unsent(self):
        """本轮有句子没发出去时，把它们的文本合成一条再发一次。"""
        pending = [t for t in self.unsent if t.strip()]
        self.unsent = []
        if not pending:
            return
        print(f"流式发送有 {len(pending)} 句没送达，改为整段重发一次。")
        if not await app_context.sender.send_text(self.session_type, self.target_id, "\n".join(pending)):
            print("流式补发仍未成功，请检查这条接入方式是否还能发送。")

    async def _send_pending_sticker(self):
        if self.sticker_sent or not self.pending_sticker_emotion or app_context.sticker_mgr is None:
            return
        # 只发文字的通道发不出图片段：别挑图（挑一次可能多调一次 LLM）
        if not client_supports(app_context.sender._active_client(), "sticker"):
            self.sticker_sent = True
            return
        self.sticker_sent = True
        sticker = await app_context.sticker_mgr.pick_async(self.ctx, self.pending_sticker_emotion,
                                               getattr(self, "pending_sticker_text", ""))
        if sticker:
            await app_context.sender.send_text(self.session_type, self.target_id, "", sticker=sticker)

    async def flush(self):
        if self.worker is not None:
            await self.queue.put(None)
            try:
                await self.worker
            except Exception as e:
                print(f"流式发送工作器异常: {e}")
            self.worker = None
        try:
            await self._resend_unsent()
        except Exception as e:
            print(f"流式补发异常: {type(e).__name__}: {e}")
        try:
            await self._send_pending_sticker()
        except Exception as e:
            print(f"流式表情包发送异常: {type(e).__name__}: {e}")
        if self.recall_pending:
            recall_ids = self.recall_ids or self.sent_ids
            app_context.sender.schedule_recall(self.session_type, self.target_id,
                                   recall_ids, self.recall_delay)
            self.action_receipts.append({
                "action": "撤回", "ok": bool(recall_ids),
                "count": len(recall_ids), "at": time.time()})

    async def abort(self):
        """放弃本轮流式发送：停掉工作器并丢弃未发送的句子。

        生成超时/异常时若只是 return，工作器会永远阻塞在 queue.get() 上，
        引用链（sender/ctx/emotions）不释放，每失败一次泄漏一个后台任务。
        """
        task, self.worker = self.worker, None
        self.recall_pending = False
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except Exception:
                break


def _tool_notes_from_trace(tool_trace) -> str:
    """把本轮工具调用结果浓缩成历史备注（随助手消息持久化）。

    事故背景：工具结果只存在于当轮请求里，不进会话历史；用户追问"你唱一段我听听"时
    模型上下文里已经没有歌词内容，只能再搜一遍。存一份精简备注后，
    build_merged_history 会在下一轮把它回放进上下文，追问可直接引用，不再重复搜索。
    条数与单条字数可调：tool_notes_max_entries / tool_notes_max_chars。
    """
    try:
        max_notes = max(1, int(app_context.global_config.get("tool_notes_max_entries", 4) or 4))
        note_chars = max(100, int(app_context.global_config.get("tool_notes_max_chars", 500) or 500))
    except (AttributeError, TypeError, ValueError):
        max_notes, note_chars = 4, 500
    notes = []
    for t in (tool_trace or []):
        if not isinstance(t, dict):
            continue
        try:
            args = json.dumps(t.get("arguments", {}), ensure_ascii=False)[:120]
        except Exception:
            args = ""
        output = str(t.get("output", "")).strip()[:note_chars]
        if not output:
            continue
        # 失败的调用也要进备注：模型下一轮才知道"这个动作被拒绝了"，
        # 否则它会以为自己已经做过，还会再试一次同样的调用
        notes.append(f"{t.get('name', '?')}({args}) → "
                     + ("" if t.get("ok") else "失败：") + output)
        if len(notes) >= max_notes:
            break
    return "\n---\n".join(notes)


def _tool_requested(user_text: str, ctx) -> bool:
    mode = str(ctx.get("tools_trigger_mode", "keyword") or "keyword").strip().lower()
    if mode in ("llm", "llm_auto", "auto"):
        # LLM 自主判断模式：模型手边有工具就什么都想调，所以先做一次意图判定。
        # 只有消息确实带客观信息需求（时间/计算/天气/搜索/链接/实体提问…）才进工具流程；
        # 纯寒暄、纯情绪、纯角色扮演直接走普通回复，回复节奏不被工具拖慢。
        if not bool(ctx.get("tools_guard_enabled", True)):
            return True
        return text_needs_tools(user_text) or is_search_dissatisfied(user_text)
    if mode == "always":
        return True
    if not ctx.get("tools_guard_enabled", True):
        return True
    low = strip_quote_note(str(user_text or "")).lower()
    if "://" in low:
        return True
    if is_search_request(user_text) or is_search_dissatisfied(user_text):
        return True
    # 问自己的提示词/设定或之前聊过什么：答案只在角色自己的上下文里，网上搜不到
    # （明确要求搜索的在上面已经放行）
    if asks_self_context(user_text):
        return False
    keywords = str(ctx.get("tools_guard_keywords", "") or "").split("\n")
    kws = [k.strip().lower() for k in keywords if k.strip()]
    if not kws:
        return True
    if any(kw in low for kw in kws):
        return True
    # 实体类提问（"X是谁""知道X吗"）即使没命中触发词也要进工具流程：
    # 这类问题问的多半是模型不认识的具体人/事/物，不搜索就只能装认识或装没听说过
    entity_q = ("是谁", "是什么", "什么是", "什么叫", "听说过", "了解吗", "认识吗",
                "知道吗", "怎么回事", "哪里人")
    if any(kw in low for kw in entity_q):
        return True
    return False


_URL_RE = re.compile(r"https?://[^\s\"'<>，,。）)】]+")
_LINK_REQUEST_RE = re.compile(r"链接|网址|[Uu][Rr][Ll]|下载地址|官网|地址")
_SEARCH_ENTRY_URL_RE = re.compile(r"网址[:：]\s*(\S+)")
_SEARCH_ENTRY_ABS_RE = re.compile(r"摘要[:：][^\n]*")
_SEARCH_ENTRY_TITLE_RE = re.compile(r"^\s*\d+[.、]\s*(.+)$", re.M)


def _search_entries(output) -> list:
    """从搜索结果文本中按顺序取出 (链接, 该条目的标题+摘要上下文)。

    条目结构是「标题 / 网址行 / 摘要行」，区间以上一条摘要行结尾为起点、
    本条摘要行结尾为终点，避免上一条的摘要混进下一条的上下文。
    """
    text = str(output or "")
    urls = list(_SEARCH_ENTRY_URL_RE.finditer(text))
    abstracts = list(_SEARCH_ENTRY_ABS_RE.finditer(text))
    entries = []
    for i, m in enumerate(urls):
        seg_start = abstracts[i - 1].end() if 0 < i < len(abstracts) + 1 and i - 1 < len(abstracts) else 0
        if i < len(abstracts):
            seg_end = abstracts[i].end()
        else:
            seg_end = urls[i + 1].start() if i + 1 < len(urls) else len(text)
        entries.append((m.group(1).rstrip(".,;、"), text[seg_start:seg_end]))
    return entries


def _search_entry_titles(output) -> list:
    """取出每条搜索结果的标题（与网址一一对应，用于判断模型是否真的提过这条）。"""
    titles = []
    for line in str(output or "").splitlines():
        m = re.match(r"^\s*\d+[.、]\s*(\S.*)$", line.strip())
        if m:
            title = re.sub(r"\s*[-_|｜]\s*[^-_|｜]{1,20}$", "", m.group(1).strip())
            titles.append(title.strip() or m.group(1).strip())
    return titles


def _entry_title_terms(title: str) -> list:
    return [t for t in re.split(r"[\s\-_|｜:：,，。.、()（）\[\]【】]+", str(title or ""))
            if len(t) >= 3]


def _title_mentioned(reply_text: str, title: str, url: str) -> bool:
    """回复里是否真的提到过这条结果（标题核心词或域名出现即算）。"""
    text = str(reply_text or "")
    if not text:
        return False
    for term in _entry_title_terms(title):
        if term in text:
            return True
    host = ""
    m = re.match(r"https?://([^/]+)", str(url or ""))
    if m:
        host = m.group(1)
        for piece in host.split("."):
            if len(piece) >= 4 and piece.lower() not in ("www", "com", "html") \
                    and piece in text:
                return True
    return False


def _reply_terms(sentences) -> set:
    """从回复台词里提取可比对的词：拉丁词/数字 + 中文二元组。"""
    text = "".join(str(s.get("zh", "") or "") for s in sentences)
    terms = set(re.findall(r"[A-Za-z0-9]{2,}", text))
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        for i in range(len(chunk) - 1):
            terms.add(chunk[i:i + 2])
    return terms


def _append_missing_links(sentences, user_text, tool_trace, session_id: str = ""):
    """把搜索结果里的链接补给回复；只补"模型确实提到过"的那几条。

    search_links_auto_append 开启时：回复里没写链接，就从本轮搜索结果的标题/域名
    里找模型提到过的那几条补上；一条都提不到就不补——宁可不给链接，也不给
    与回复内容无关的链接。用户明确索要链接时放宽一档，但仍按相关度排序取前几条。
    """
    if not sentences:
        return sentences
    if not app_context.global_config.get("search_links_auto_append", True):
        return sentences
    if any(_URL_RE.search(str(s.get("display", "") or "")) for s in sentences):
        return sentences
    wants_link = bool(_LINK_REQUEST_RE.search(str(user_text or "")))
    reply_text = "".join(str(s.get("display", "") or "") + str(s.get("zh", "") or "")
                         for s in sentences)
    seen = {u.rstrip("/") for u in sent_links(session_id)}
    entries, urls_seen = [], set()
    for t in tool_trace or []:
        if t.get("name") != "web_search" or not t.get("ok"):
            continue
        output = str(t.get("output", ""))
        titles = _search_entry_titles(output)
        for index, (url, context) in enumerate(_search_entries(output)):
            if not url or url in urls_seen or url.rstrip("/") in seen:
                continue
            urls_seen.add(url)
            title = titles[index] if index < len(titles) else ""
            entries.append((url, title, context))
    if not entries:
        if wants_link:
            print("搜索链接补发：本轮搜索结果里的链接此前都已发过，本次不重复发送。")
        return sentences
    mentioned = [(url, context) for url, title, context in entries
                 if _title_mentioned(reply_text, title, url)]
    picked = []
    if mentioned:
        picked = [url for url, _ctx in mentioned]
    elif wants_link:
        terms = _reply_terms(sentences)
        scored = []
        for url, title, context in entries:
            score = sum(1.0 for term in terms if term in context)
            if title and _title_mentioned(reply_text, title, url):
                score += 5.0
            if score > 0:
                scored.append((score, url))
        scored.sort(key=lambda x: -x[0])
        if scored:
            best = scored[0][0]
            picked = [url for score, url in scored if score >= best * 0.5]
    try:
        limit = max(1, int(app_context.global_config.get("search_links_max", 3) or 3))
    except (TypeError, ValueError):
        limit = 3
    picked = picked[:limit]
    if not picked:
        return sentences
    last = sentences[-1]
    last["display"] = (str(last.get("display", "") or "").rstrip()
                       + "\n" + " ".join(picked)).strip()
    record_sent_links(session_id, picked)
    print(f"搜索链接补发：{len(picked)} 条 → {' '.join(picked)}")
    return sentences


async def _sticker_judgement_from_llm(ctx: RoleContext, image_result: dict) -> Optional[dict]:
    """中立地判定这张图的收藏分类与用途名，返回 {"category": str, "name": str}。

    只依据画面的客观描述判断：识图调用带着角色人设、对话历史与当前心情，
    模型会站在角色立场上给图归类与命名。判定失败返回 None（调用方沿用原分类），
    判定不适合当表情包时 category 为空串。
    """
    description = str(image_result.get("description", "") or "").strip()
    if not description:
        return None
    from modules.stickers import (category_candidates, category_candidates_text,
                                  CATEGORY_DISAMBIGUATION)
    # 候选分类取自表情库目录下实际存在的子文件夹，不预设固定的分类名
    cats = category_candidates(ctx)
    classify_prompt = (
        f"图片内容：{description}\n"
        "你是中立的图库管理员：只根据这张图自身的画面与文字判断它的用途，"
        "不要代入任何角色，也不要考虑对话里任何人的情绪。\n"
        "请判断这张图【将来被当作表情包发出去时，发图一方的情绪/使用场景】"
        "（使用者发图时的语气，不是画面中角色此刻的情绪）。"
        "画面人物的动作、表情与文字往往指向互动用途（挑逗、撩、调戏、嘲讽、炫耀、"
        "撒娇等），要据此归类。"
        "如果这张图其实是纯风景/空镜/静物/无文字无表情的随手拍，"
        "或者画面阴森、恐怖、诡异、病态、压抑，且没有明确的互动用途，"
        "就回答 none 表示不适合当表情包，不要硬选一个分类。"
        f"{CATEGORY_DISAMBIGUATION}"
        "可以收藏时，先逐个比较下面这些分类文件夹的适用范围，再选出最贴合的一个。"
        "分类清单取自表情库目录下实际存在的文件夹：\n"
        f"{category_candidates_text(ctx)}\n"
        "只输出一个 JSON 对象，不要其他任何内容："
        '{"category": "分类名（原样照抄上面的清单）；不适合当表情包则填 none", '
        '"name": "用途名"}。'
        "name 是这张表情以后反复使用时的名字：只写它适合表达的情绪与互动用途，"
        "简短（6~12 个字），不写成句子，不写画面里是谁、也不写是给谁用的；"
        "严禁出现角色名、人名、作品名，也不能是「好看」「有趣」「可爱」这类空话；"
        "category 为 none 时 name 留空。"
    )
    try:
        result = await chat_once(ctx, [{"role": "user", "content": classify_prompt}])
    except Exception as e:
        print(f"表情分类失败: {type(e).__name__}: {e}")
        return None
    raw = str(result.get("content") or "").strip()
    obj = extract_json(raw) or {}
    name = str(obj.get("name") or "").strip()
    category = str(obj.get("category") or "").strip().lower()
    if category in cats:
        return {"category": category, "name": name}
    # 结构化字段缺失或非法时按整段输出兜底。分类名之间互不为子串，
    # 所以命中多个说明模型只是在解释（"不是 A，是 B"），此时不能挑一个当答案。
    text = category or raw.lower()
    if re.search(r"\bnone\b|不适合|不收藏|无法归类|没有互动用途", text) \
            and not any(c in text for c in cats):
        print(f"【表情收藏-分类】模型判定不适合当表情包：{raw[:60]!r}")
        return {"category": "", "name": ""}
    hits = [c for c in cats if re.search(rf"\b{c}\b", text)]
    if len(hits) == 1:
        return {"category": hits[0], "name": name}
    if len(hits) > 1:
        print(f"【表情收藏-分类】输出里出现多个分类（{'、'.join(hits)}），无法确定，按不收藏处理。")
        return {"category": "", "name": ""}
    print("【表情收藏-分类】未能得到有效分类，按不收藏处理（不再默认 pingjing）。")
    return {"category": "", "name": ""}


async def generate_reply(ctx: RoleContext, emotions: dict, user_text: str, history: list,
                         images: Optional[list], extra_parts: List[str],
                         user_id: str = "", on_sentence=None,
                         session_id: str = "", describe_only: bool = False,
                         speaker_labels: Optional[dict] = None) -> Optional[dict]:
    """生成回复：识图 / 工具调用 / 流式 / 普通四种路径统一入口。

    describe_only=True 只用于「本轮不发消息、但要把图看进历史」的场景：
    识图只出画面描述与收藏判定，不产出台词，也不会降级成文本回复。
    speaker_labels 是**完整历史**的说话人编号表：history 在开了摘要时只是尾部窗口，
    编号必须沿用完整历史，否则 [用户N] 会和系统提示词里的当前发言者对不上。
    """
    if images is not None and not str(user_text or "").strip():
        user_text = "[图片]"
    if images is not None:
        result = await get_image_reply(ctx, user_text, history, emotions, images,
                                       extra_parts=extra_parts, stats=app_context.stats_mgr,
                                       describe_only=describe_only,
                                       speaker_labels=speaker_labels)
        if result is not None:
            out = {"sentences": result.get("sentences", []), "llm_ms": result.get("ms", 0),
                   "tool_trace": []}
            if result.get("description"):
                out["description"] = result["description"]
            if result.get("capture"):
                out["capture"] = result["capture"]
            if result.get("capture_image"):
                out["capture_image"] = result["capture_image"]
            return out
        if describe_only:
            return None
        # 识图失败（模型未配置/服务异常），降级为普通文本回复，避免用户消息石沉大海
        print("识图失败，降级为普通文本回复。")

    messages = build_chat_messages(ctx, user_text, history, emotions, extra_parts,
                                   trailing_notes=([image_identity_note(ctx)]
                                                   if images is not None
                                                   and app_context.global_config.get("image_identity_guard_enabled", True)
                                                   else None),
                                   speaker_labels=speaker_labels)

    # 工具调用路径（非流式，保证 tool_calls 正确处理）：无明确工具需求时走普通回复，避免误调
    if app_context.tool_registry and app_context.global_config.get("tools_enabled", False) \
            and app_context.tool_registry.has_enabled_tools() and _tool_requested(user_text, ctx):
        app_context.tool_registry.begin_reply()
        result = await chat_with_tools(ctx, messages, app_context.tool_registry, stats=app_context.stats_mgr,
                                       user_id=user_id, session_key=session_id)
        try:
            tool_log_cap = int(app_context.global_config.get("tool_log_output_chars", 0) or 0)
        except (AttributeError, TypeError, ValueError):
            tool_log_cap = 0
        for t in result.get("tool_trace", []):
            output_text = str(t["output"])
            if tool_log_cap > 0 and len(output_text) > tool_log_cap:
                # 0 = 完整输出（默认）。之前写死只打 80 字，搜索的 8 条结果在日志里
                # 只能看到第 1 条，用户会误以为"只搜到一个结果"。
                output_text = output_text[:tool_log_cap] \
                    + f"…（剩余 {len(str(t['output'])) - tool_log_cap} 字省略，tool_log_output_chars 可调）"
            print(f"[工具调用] {t['name']} ok={t['ok']} → {output_text}")
        sentences = _append_missing_links(
            normalize_sentences(result["content"], ctx, emotions, user_text),
            user_text, result.get("tool_trace"), session_id)
        return {"sentences": sentences, "llm_ms": result["ms"], "tool_trace": result["tool_trace"],
                "llm_calls": int(result.get("llm_calls", 1)),
                "tool_calls": len([t for t in result.get("tool_trace", []) if t.get("ok")])}

    # 流式路径
    if app_context.global_config.get("streaming_enabled", False):
        return await generate_reply_stream(ctx, emotions, user_text, messages, on_sentence)

    # 普通路径
    result = await chat_once(ctx, messages)
    if app_context.stats_mgr:
        app_context.stats_mgr.record_llm(result["ms"])
    sentences = normalize_sentences(result["content"], ctx, emotions, user_text)
    return {"sentences": sentences, "llm_ms": result["ms"], "tool_trace": [],
            "llm_calls": 1, "tool_calls": 0}


async def generate_reply_stream(ctx: RoleContext, emotions: dict, user_text: str,
                                messages: list, on_sentence=None) -> dict:
    parser = SentenceStreamParser()
    sentences = []
    first_ms = None
    start = time.time()
    error = None
    try:
        async for chunk in stream_chat(ctx, messages):
            if chunk.get("first_token_ms") and first_ms is None:
                first_ms = chunk["first_token_ms"]
            delta = chunk.get("delta") or ""
            if not delta:
                continue
            for obj in parser.feed(delta):
                if not sentence_obj_has_text(obj) and not sentence_obj_has_action(obj):
                    continue  # 只有 emotion 等元数据的空句子对象：跳过，避免念出兜底台词
                sentence = normalize_single(obj, ctx, emotions, user_text)
                # 模型可能把多句塞进同一元素；按句号拆分后逐句流式发送
                pieces = split_multi_clause_sentences([sentence])
                # 分句会重建句子对象：句子级动作字段要挪到第一条上，
                # 否则引用/@/戳一戳/发送形态会在这一步丢掉
                for key in SENTENCE_ACTION_KEYS:
                    if key in sentence and pieces:
                        pieces[0][key] = sentence[key]
                for piece in pieces:
                    sentences.append(piece)
                    if on_sentence:
                        await on_sentence(piece)
    except Exception as e:
        error = e
        print(f"流式请求异常: {type(e).__name__}: {e}（已收到的内容将继续处理）")
    if error is not None and not sentences:
        # 一句都没收到：只发中文提醒，让主人知道这轮没成。
        # 报错文本不走整段规整：错误详情里常带 JSON，走规整会被当台词丢掉或换成兜底台词。
        # 标成只发文字：报错是系统消息，合成语音只会白等几十秒，还会把报错念出来
        sentence = normalize_single({"zh": error_reply_text(error),
                                     "delivery": DELIVERY_PLAIN},
                                    ctx, emotions, user_text)
        sentences.append(sentence)
        if on_sentence:
            await on_sentence(sentence)
    else:
        # 流结束兜底：一句未产出时整段规整（宽容修复/JSON防线）；
        # 已产出时抢救末尾被截断的半句
        for piece in parser.finish(ctx, user_text, emotions):
            sentences.append(piece)
            if on_sentence:
                await on_sentence(piece)
    total_ms = (time.time() - start) * 1000
    if app_context.stats_mgr:
        app_context.stats_mgr.record_llm(first_ms or total_ms)
    return {"sentences": sentences, "llm_ms": total_ms, "tool_trace": [],
            "llm_calls": 1, "tool_calls": 0}


# ============================================================================
# 消息处理主管线
# ============================================================================

def _spawn(coro):
    """安全的后台任务封装，异常仅记录不中断主流程。"""
    async def _wrapper():
        try:
            await coro
        except Exception as e:
            import traceback
            print(f"后台任务异常: {type(e).__name__}: {e}")
            traceback.print_exc()
    return asyncio.create_task(_wrapper())


def _norm_text(s: str) -> str:
    t = re.sub(r"[\s\u3000]+", "", str(s or ""))
    return re.sub(r"[，。！？、；：,.!?;:'\"“”‘’（）()\[\]【】…~～\-—_*#@/\\]+", "", t)


def _repeat_ratio(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    an, bn = _norm_text(a), _norm_text(b)
    if not an or not bn:
        return 0.0
    blocks = SequenceMatcher(None, an, bn).get_matching_blocks()
    matched = sum(blk.size for blk in blocks)
    return matched / min(len(an), len(bn))


def _strip_image_claims(sentences: list) -> list:
    """剔除把用户发来的图认领成角色自己的那些句子。"""
    return [s for s in (sentences or [])
            if not image_self_claim(str(s.get("zh", "") or ""))]


def _recent_assistant_replies(history: list, rounds: int) -> list:
    """按时间倒序取最近 rounds 条角色回复（跳过空内容）。"""
    out = []
    for msg in reversed(history or []):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = str(msg.get("content", "") or "").strip()
        if content:
            out.append(content)
        if len(out) >= max(1, rounds):
            break
    return out


def _max_repeat_ratio(reply_text: str, references: list) -> tuple:
    """返回 (最高重合率, 与之最像的那句参照文本)。"""
    best, target = 0.0, ""
    for ref in references or []:
        if not ref:
            continue
        ratio = _repeat_ratio(ref, reply_text)
        if ratio > best:
            best, target = ratio, ref
    return best, target


_USER_ECHO_MIN_CHARS = 5
_RETRY_TEMPERATURE = 1.3
_RETRY_TEMPERATURE_MAX = 1.4

# 防复读的四个独立开关（默认全开 = 原来的行为）。
# 「什么时候查」与「跟谁比」是两个维度，各自可单独关闭：
#   streaming / regen  —— 检查时机
#   compare_self / compare_user —— 比对对象
_REPEAT_GUARD_KEYS = {
    "streaming": "repeat_guard_streaming_check",
    "regen": "repeat_guard_regen_check",
    "compare_self": "repeat_guard_compare_self",
    "compare_user": "repeat_guard_compare_user",
}


def repeat_thresholds(ctx=None) -> tuple:
    """(自重复系数, 复述用户系数)：重合度达到该值即判定为重复，越大越宽容。"""
    src = ctx if ctx is not None else app_context.global_config
    values = []
    for key, fallback in (("repeat_guard_self_threshold", 0.85),
                          ("repeat_guard_user_threshold", 0.8)):
        try:
            value = float(src.get(key, fallback))
        except (TypeError, ValueError):
            value = fallback
        values.append(min(1.0, max(0.0, value)))
    return values[0], values[1]


def repeat_guard_flags(ctx=None) -> dict:
    """读取四个防复读开关；取不到时按开启处理（保持原行为）。"""
    src = ctx if ctx is not None else app_context.global_config
    flags = {}
    for name, key in _REPEAT_GUARD_KEYS.items():
        try:
            flags[name] = bool(src.get(key, True))
        except Exception:
            flags[name] = True
    return flags


def repeat_guard_summary(flags: dict) -> str:
    """把关闭的开关写成一行提示；全开时返回空串。"""
    names = {"streaming": "流式首句检查", "regen": "整段生成后校验",
             "compare_self": "比对角色历史回复", "compare_user": "比对用户本条消息"}
    off = [label for name, label in names.items() if not flags.get(name, True)]
    if not off:
        return ""
    return "（已关闭：" + "、".join(off) + "）"


def repeat_guard_active(flags: dict) -> bool:
    """是否还有任何一维在生效：比对对象全关掉时，整套防复读等于没开。"""
    return bool(flags.get("compare_self", True) or flags.get("compare_user", True))


def _log_mood_commit(new_mood, verdict):
    """心情变化量落盘后打一行日志；本轮无需更新时（new_mood 为 None）什么都不做。

    打印的必须是**存档里的真实变化**：以前拿判定用的心情值当"变化前"，
    而那个值含深夜/周末偏置，于是存档明明一直是 100 却打成「95 → 100」。
    """
    if new_mood is None:
        return
    base = float(verdict.get("mood_base", verdict.get("mood", new_mood)) or 0)
    judged = float(verdict.get("mood", base) or 0)
    parts = []
    if verdict.get("mood_delta") is not None:
        parts.append(f"判定 {float(verdict['mood_delta']):+.0f}")
    if abs(judged - base) >= 0.5:
        parts.append(f"判定用值 {judged:.0f}")
        parts.append(f"偏置 {judged - base:+.0f}")
    regress = float(verdict.get("mood_regress") or 0)
    if abs(regress) >= 0.5:
        parts.append(f"回稳 {regress:+.0f}")
    suffix = f"（{'，'.join(parts)}）" if parts else ""
    print(f"心情更新：{base:.0f} → {float(new_mood):.0f}{suffix}")


def _judge_mood_text(verdict) -> str:
    """审判日志里的心情描述：以存档值为准，另标出判定用的偏置值。

    审判用的是「存档值 + 深夜/周末偏置」那个临时值，直接打它会让界面上的
    心情值与日志对不上（存档 62、日志写 55），看起来像心情系统算错了。
    """
    base = float(verdict.get("mood_base", verdict.get("mood", 0)) or 0)
    judged = float(verdict.get("mood", base) or 0)
    if abs(judged - base) >= 0.5:
        return f"心情值 {base:.0f}（判定用值 {judged:.0f}，偏置 {judged - base:+.0f}）"
    return f"心情值 {base:.0f}"


def _judge_gate_cause(verdict) -> str:
    """审判没通过的原因：LLM 自己说不用回，还是概率门控掷输了。"""
    if not verdict.get("llm_reply", True):
        return "LLM判定无需回复"
    probability = float(verdict.get("probability", 0) or 0)
    roll = verdict.get("roll")
    if roll is None:
        return f"概率门控未通过（回复概率 {probability:.3f}）"
    return f"概率门控未通过（掷出 {float(roll):.3f} ≥ 回复概率 {probability:.3f}）"


def _image_reply_override(verdict, has_image: bool) -> bool:
    """带图消息是否要忽略审判的「无需回复」判定。

    审判看不到画面：本条带图时它的"不用回"没有依据（消息里可能除了图片一个字都
    没有）。心情决定的回复概率不属于此列，照常生效。
    """
    return bool(has_image and verdict is not None and not verdict["should_reply"]
                and not verdict.get("llm_reply", True))
