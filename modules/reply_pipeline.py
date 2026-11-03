# -*- coding: utf-8 -*-
import asyncio
import json
import re
import time
from pathlib import Path
from typing import Optional, List, Tuple
from urllib.parse import urlparse

from modules import app_context
from modules.adapters import client_supports
from modules.sender import (VoicePacer, voice_enabled_for, recall_delay_of,
                            mute_seconds_of, mute_target_of, sent_action_receipts,
                            DEFAULT_RECALL_DELAY_SECONDS, RECALL_DENY)
from modules.tts import synthesize_sentence, strip_non_dialogue_text
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
    display_text_of,
    error_reply_text,
    extract_json,
    get_image_reply,
    has_structured_sentences,
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


_FORMAT_RETRY_HINT = (
    "【系统规则】你这一轮没有按系统提示里的 JSON 格式输出。下面这一条回复请严格"
    "按规定格式重新输出：只输出 JSON，不要解释、不要旁白、不要表情包占位；"
    "每句台词必须带上 zh、台词语言字段与 emotion（从【情绪可选列表】原样照抄一个）。"
)


# ============================================================================
# 回复生成（含流式）与句子发送
# ============================================================================

async def _poke_with_receipt(sender, session_type, target_id, target) -> dict:
    helper = getattr(sender, "_poke_and_receipt", None)
    if helper is not None:
        return await helper(session_type, target_id, target)
    ok = await sender.send_poke(session_type, target_id, target)
    return {"action": "戳一戳", "ok": bool(ok), "target": str(target or ""),
            "at": time.time()}


class SentenceSink:

    def __init__(self, session_type, target_id, emotions, ctx, last_reply="", user_text="",
                 reply_id=None, allowed_at_ids=None, poke_target=None,
                 recall_request="", at_names=None, speaker_id="", repeat_requested=False,
                 speaker_role="", mute_ids=None, recall_other_id="",
                 mute_done_ids=None, force_quote=False):
        self.session_type = session_type
        self.reply_id = reply_id
        # 不指望模型自己填 reply_to
        self.force_quote = bool(force_quote)
        self.allowed_at_ids = {str(q) for q in (allowed_at_ids or [])}
        self.at_names = {str(q): str(n) for q, n in (at_names or {}).items()}
        self.speaker_id = str(speaker_id or "")
        self.actions_pending = True
        self.poke_target = poke_target
        self.poke_pending = True
        self.speaker_role = str(speaker_role or "")
        self.mute_ids = [str(q) for q in (mute_ids or []) if str(q).strip()]
        self.recall_other_id = str(recall_other_id or "")
        self.mute_done_ids = {str(q) for q in (mute_done_ids or [])}
        self.mute_pending = True
        self.recall_other_pending = True
        self.recall_pending = False
        self.recall_delay = DEFAULT_RECALL_DELAY_SECONDS
        self.recall_request = str(recall_request or "")
        self.recall_ids: List[str] = []
        self.action_receipts: List[dict] = []
        self.sent_ids: List[str] = []
        # 整条让给批量发送路径处理
        self.batch_only = False
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
        self.repeat_requested = bool(repeat_requested)
        self.queue: asyncio.Queue = asyncio.Queue()
        self.worker: Optional[asyncio.Task] = None
        self.sent = 0
        self.sent_sentences: List[dict] = []
        self.sent_texts: List[str] = []
        self.unsent: List[str] = []           # 发送失败的句子，收尾时整段重发
        self._tts_ok: Optional[bool] = None
        self.tts_ms = 0.0
        self.tts_calls = 0
        self.blocked = False
        self._first_checked = False
        self.pending_sticker_emotion = None
        self.pending_sticker_text = ""
        self.sticker_sent = False
        self.guard = repeat_guard_flags(ctx)
        self.thresholds = repeat_thresholds(ctx)
        self.pacer = VoicePacer(bool(app_context.active_config().get("dynamic_sleep", True)))

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
                and _repeat_ratio_user(u, zh) >= user_threshold:
            return True
        return False

    async def on_sentence(self, sentence: dict):
        if self.batch_only:
            return
        if sentence.get("delivery"):
            if self.queued == 0:
                self.batch_only = True
                return
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
                config = app_context.active_config()
                if config and config.get("separate_send", False) \
                        and (config.get("send_voice_separately", False)
                             or config.get("text_separate", False)) \
                        and config.get("separate_force_segment", True):
                    for piece in segment_for_tts([item], self.ctx):
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
                self._tts_ok = False
                print("这条接入方式不支持语音，流式回复只发文字。")
            elif not voice_enabled_for(app_context.active_config(), self.session_type):
                self._tts_ok = False
            else:
                self._tts_ok = await ensure_tts_service(app_context.active_config())
                if not self._tts_ok:
                    print("警告：TTS 服务不可用，流式回复降级为纯文本。")
        return self._tts_ok and app_context.active_config().get("tts_reply_enabled", True)

    async def _speech_text(self, sentence: dict) -> str:
        text = str(sentence.get("lang") or "")
        target = str((self.ctx.get("text_lang", "") if self.ctx else "") or "").strip().lower()
        if not target or target == "auto" or not lang_text_broken(text, target):
            return text
        source = strip_mention_placeholder(strip_non_dialogue_text(
            str(sentence.get("zh") or "").strip() or text, log=False))
        try:
            fixed = await asyncio.wait_for(translate_to_lang(self.ctx, source, target,
                                                             label="台词翻译"),
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
        shown = display_text_of(sentence, self.ctx)
        spoken = str(sentence.get("lang") or "")
        if app_context.sticker_mgr and self.sent == 0 and self.pending_sticker_emotion is None:
            self.pending_sticker_emotion = sentence.get("emotion", "")
            self.pending_sticker_text = shown
        wav = None
        if spoken.strip() and await self._tts_available():
            start = time.time()
            wav = await synthesize_sentence(self.ctx, await self._speech_text(sentence),
                                            sentence.get("emotion", ""),
                                            self.emotions, app_context.memory_manager.data_path,
                                            stats=app_context.stats_mgr,
                                            mimic=sentence.get("mimic", ""),
                                            mimics=available_mimics(self.ctx))
            self.tts_ms += (time.time() - start) * 1000
            if wav:
                self.tts_calls += 1
        if apply_literal_mention(sentence, self.allowed_at_ids, self.at_names, self.speaker_id):
            print("发送：台词里写成文字的@，已换成真正的@。")
            shown = display_text_of(sentence, self.ctx)
        mentions = [str(q) for q in (sentence.get("mention_ids") or [])
                    if str(q) in self.allowed_at_ids]
        reply_to = (sentence.get("reply_to") is True or self.force_quote) \
            and self.reply_id is not None
        mark = len(self.sent_ids)
        want_recall = (self.recall_request == "next"
                       or (sentence.get("recall") is True
                           and self.recall_request != RECALL_DENY)) and not self.recall_pending
        if self.poke_pending and sentence.get("poke") is True:
            self.poke_pending = False
            self.action_receipts.append(await _poke_with_receipt(
                app_context.sender, self.session_type, self.target_id, self.poke_target))
        if self.mute_pending and sentence.get("mute"):
            self.mute_pending = False
            mute_target = mute_target_of(sentence, self.mute_ids, self.speaker_id)
            if mute_target and str(mute_target) in self.mute_done_ids:
                print(f"禁言：本轮系统已对 {mute_target} 执行过，台词里的 mute 不再重复执行。")
            else:
                self.action_receipts.append(await app_context.sender.mute_receipt(
                    self.session_type, self.target_id, mute_target,
                    mute_seconds_of(sentence), self.speaker_id, self.speaker_role))
        if self.recall_other_pending and sentence.get("recall_other") is True:
            self.recall_other_pending = False
            if self.recall_request != RECALL_DENY:
                self.action_receipts.append(await app_context.sender.recall_other_receipt(
                    self.session_type, self.target_id, self.recall_other_id,
                    self.speaker_id, self.speaker_role))
        if not shown.strip() and not spoken.strip():
            self.sent += 1
            self.sent_sentences.append(sentence)
            return
        quote_used = bool(self.actions_pending and reply_to)
        if wav:
            try:
                await self.pacer.wait()
                msg_group = app_context.sender.new_message_group()
                ok = await app_context.sender.send_text(
                    self.session_type, self.target_id, shown,
                    reply_id=self.reply_id if quote_used else None,
                    at_ids=mentions,
                    group=msg_group)
                self.actions_pending = False
                self._note_send_result(ok, shown)
                self._note_action_receipts(ok, quote_used, mentions)
                self._track_sent_id()
                await app_context.sender.send_voice(self.session_type, self.target_id, wav,
                                        group=msg_group)
                self._track_sent_id()
                await self.pacer.hold_voice(wav)
            finally:
                Path(wav).unlink(missing_ok=True)
        else:
            await app_context.sender.pace_text_only(self.session_type, self.target_id)
            ok = await app_context.sender.send_text(
                self.session_type, self.target_id, shown,
                reply_id=self.reply_id if quote_used else None,
                at_ids=mentions)
            self.actions_pending = False
            self._note_send_result(ok, shown)
            self._note_action_receipts(ok, quote_used, mentions)
            self._track_sent_id()
        self.sent += 1
        self.sent_sentences.append(sentence)
        self.sent_texts.append(shown)
        if want_recall:
            self.recall_pending = True
            self.recall_delay = recall_delay_of(sentence)
            self.recall_ids = self.sent_ids[mark:]

    def _note_send_result(self, ok: bool, shown: str) -> None:
        if ok:
            return
        self.unsent.append(str(shown or ""))
        print(f"流式发送：这句没发出去，已记为待重发：{str(shown or '')[:30]!r}")

    def _note_action_receipts(self, ok: bool, quote_used: bool, at_ids) -> None:
        if ok:
            self.action_receipts.extend(sent_action_receipts(quote_used, at_ids))

    def _track_sent_id(self) -> None:
        mid = str(getattr(app_context.sender, "last_message_id", "") or "").strip()
        if mid:
            self.sent_ids.append(mid)

    async def _resend_unsent(self):
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
    try:
        max_notes = max(1, int(app_context.active_config().get("tool_notes_max_entries", 4) or 4))
        note_chars = max(100, int(app_context.active_config().get("tool_notes_max_chars", 500) or 500))
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
        notes.append(f"{t.get('name', '?')}({args}) → "
                     + ("" if t.get("ok") else "失败：") + output)
        if len(notes) >= max_notes:
            break
    return "\n---\n".join(notes)


_FOLLOWUP_CHALLENGE_RE = re.compile(
    r"^(?:为什么|为啥|怎么|咋|凭什么|谁让你)[^。！？!?]{0,16}(?:的|呢|啊|呀)?[？?]?$")


def _followup_challenge_note(user_text: str) -> str:
    text = strip_quote_note(str(user_text or "")).strip()
    if not _FOLLOWUP_CHALLENGE_RE.match(text):
        return ""
    return ("【可能是在追问上一条回复】如果这句话是在质疑你刚刚说过的内容，"
            "就针对上一条回复解释清楚、说错了就直接更正（需要核实就重新调用搜索工具），"
            "不要当成让你自我介绍；如果它本身是一个独立的问题，就正常回答。")


_CODE_CLAIM_RE = re.compile(
    r"(?:兑换码|激活码|礼包码|第[一二三四五1-5]个)[^。！？\n]{0,8}?(?:是|为|[:：])\s*[「『\"'（(]?"
    r"([^\s」』\"'）)，,。！？]{2,24})")

_CODE_FIX_HINT = (
    "【更正】你上一条给出的兑换码经不起核对。"
    "兑换码只会是字母数字串；上面工具结果里至少两个不同网站都写到的码才算可靠，"
    "只在一个站出现的很可能是引流页编的假码，一律不能给——"
    "改成给出有多个来源的码，或者如实说明没查到可靠的兑换码，"
    "把查到的版本、时间等确实写在结果里的信息讲清楚，不要再编。"
    "回复格式仍按原来的 JSON 要求输出。")

_URL_EVIDENCE_RE = re.compile(r"网址:\s*(\S+)")
_SOURCE_LABEL_RE = re.compile(r"（来源：\s*([^）]*?)\s*）")
_CODEISH_RUN_RE = re.compile(r"[A-Za-z0-9]{5,}")


def _host_chunks(evidence: str) -> List[Tuple[str, str]]:
    text = str(evidence or "")
    marks = []
    for m in _URL_EVIDENCE_RE.finditer(text):
        marks.append((m.start(), (urlparse(m.group(1)).hostname or "").lower()))
    for m in _SOURCE_LABEL_RE.finditer(text):
        marks.append((m.start(), m.group(1).lower()))
    if not marks:
        return []
    marks.sort()
    chunks = []
    for idx, (pos, host) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(text)
        chunks.append((host, text[pos:end]))
    return chunks


def _chunk_lists_codes(chunk: str) -> bool:
    squashed = re.sub(r"[^A-Za-z0-9]", "", re.sub(r"https?://\S+", " ", chunk)).upper()
    for m in _CODEISH_RUN_RE.finditer(squashed):
        run = m.group(0)
        if any(c.isdigit() for c in run) and any(c.isalpha() for c in run):
            return True
    return False


def _code_claim_unverified(content: str, user_text: str, tool_trace) -> bool:
    if not re.search(r"兑换码|激活码|礼包码", str(user_text or "")):
        return False
    tool_evidence = " ".join(str(t.get("output", "")) for t in (tool_trace or []))
    evidence = tool_evidence + " " + str(user_text or "")
    squashed = re.sub(r"[^A-Za-z0-9]", "", evidence).upper()
    chunks = _host_chunks(tool_evidence)
    strict = sum(1 for _, chunk in chunks if _chunk_lists_codes(chunk)) >= 2
    for m in _CODE_CLAIM_RE.finditer(str(content or "")):
        value = m.group(1).strip()
        if not value:
            continue
        token = re.sub(r"[^A-Za-z0-9]", "", value).upper()
        if len(token) < 4:
            return True          # 中文短语、版本名之类，不像兑换码
        if token not in squashed:
            return True          # 结果里根本没有这个码
        if strict:
            hosts_with = sum(1 for _, chunk in chunks
                             if token in re.sub(r"[^A-Za-z0-9]", "", chunk).upper())
            if hosts_with < 2:
                return True      # 只有单一来源：多半是引流页编的假码
    return False


def _tool_requested(user_text: str, ctx) -> bool:
    mode = str(ctx.get("tools_trigger_mode", "keyword") or "keyword").strip().lower()
    if mode in ("llm", "llm_auto", "auto"):
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
    # （明确要求搜索的在上面已经放行）
    if asks_self_context(user_text):
        return False
    keywords = str(ctx.get("tools_guard_keywords", "") or "").split("\n")
    kws = [k.strip().lower() for k in keywords if k.strip()]
    if not kws:
        return True
    if any(kw in low for kw in kws):
        return True
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
    text = "".join(str(s.get("zh", "") or "") for s in sentences)
    terms = set(re.findall(r"[A-Za-z0-9]{2,}", text))
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        for i in range(len(chunk) - 1):
            terms.add(chunk[i:i + 2])
    return terms


def _append_missing_links(sentences, user_text, tool_trace, session_id: str = ""):
    if not sentences:
        return sentences
    if not app_context.active_config().get("search_links_auto_append", True):
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
        limit = max(1, int(app_context.active_config().get("search_links_max", 3) or 3))
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
    description = str(image_result.get("description", "") or "").strip()
    if not description:
        return None
    from modules.stickers import (category_candidates, category_candidates_text,
                                  CATEGORY_DISAMBIGUATION)
    cats = category_candidates(ctx)
    classify_prompt = (
        f"图片内容：{description}\n"
        "你是中立的图库管理员：只根据这张图自身的画面与文字判断它的用途，"
        "不要代入任何角色，也不要考虑对话里任何人的情绪。\n"
        "请判断这张图【将来被当作表情包发出去时，发图一方的情绪/使用场景】"
        "（使用者发图时的语气，不是画面中角色此刻的情绪）。"
        "画面人物的动作、表情与文字往往指向互动用途（挑逗、撩、调戏、嘲讽、炫耀、"
        "撒娇等），要据此归类。"
        "如果这张图其实是屏幕截图或长图（画面里有界面元素：状态栏、播放控件、聊天气泡、"
        "评论区、按钮等，或只是某张图被嵌在别的界面里），"
        "或者它是纯风景/空镜/静物/无文字无表情的随手拍，"
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
        result = await chat_once(ctx, [{"role": "user", "content": classify_prompt}],
                                 label="表情分类")
    except Exception as e:
        print(f"表情分类失败: {type(e).__name__}: {e}")
        return None
    raw = str(result.get("content") or "").strip()
    obj = extract_json(raw) or {}
    name = str(obj.get("name") or "").strip()
    category = str(obj.get("category") or "").strip().lower()
    if category in cats:
        return {"category": category, "name": name}
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
                         speaker_labels: Optional[dict] = None,
                         wants_voice: Optional[bool] = None) -> Optional[dict]:
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
        print("识图失败，降级为普通文本回复。")

    trailing = []
    if images is not None and app_context.active_config().get("image_identity_guard_enabled", True):
        trailing.append(image_identity_note(ctx))
    follow_note = _followup_challenge_note(user_text)
    if follow_note:
        trailing.append(follow_note)
    messages = build_chat_messages(ctx, user_text, history, emotions, extra_parts,
                                   trailing_notes=(trailing or None),
                                   speaker_labels=speaker_labels)

    if app_context.tool_registry and app_context.active_config().get("tools_enabled", False) \
            and app_context.tool_registry.has_enabled_tools() and _tool_requested(user_text, ctx):
        app_context.tool_registry.begin_reply()
        result = await chat_with_tools(ctx, messages, app_context.tool_registry, stats=app_context.stats_mgr,
                                       user_id=user_id, session_key=session_id, label="回复生成")
        try:
            tool_log_cap = int(app_context.active_config().get("tool_log_output_chars", 0) or 0)
        except (AttributeError, TypeError, ValueError):
            tool_log_cap = 0
        for t in result.get("tool_trace", []):
            output_text = str(t["output"])
            if tool_log_cap > 0 and len(output_text) > tool_log_cap:
                output_text = output_text[:tool_log_cap] \
                    + f"…（剩余 {len(str(t['output'])) - tool_log_cap} 字省略，tool_log_output_chars 可调）"
            print(f"[工具调用] {t['name']} ok={t['ok']} → {output_text}")
        content = str(result.get("content") or "")
        if _code_claim_unverified(content, user_text, result.get("tool_trace")):
            print("[回复] 回复里的兑换码和检索结果对不上，追加一轮更正。")
            fix = await chat_once(ctx, messages + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": _CODE_FIX_HINT}], label="兑换码更正")
            fixed = str(fix.get("content") or "").strip()
            if fixed:
                content = fixed
                result["llm_calls"] = int(result.get("llm_calls", 1)) + 1
        sentences = _append_missing_links(
            normalize_sentences(content, ctx, emotions, user_text),
            user_text, result.get("tool_trace"), session_id)
        return {"sentences": sentences, "llm_ms": result["ms"], "tool_trace": result["tool_trace"],
                "llm_calls": int(result.get("llm_calls", 1)),
                "tool_calls": len([t for t in result.get("tool_trace", []) if t.get("ok")])}

    # 流式路径
    if app_context.active_config().get("streaming_enabled", False):
        return await generate_reply_stream(ctx, emotions, user_text, messages, on_sentence,
                                           wants_voice=wants_voice)

    # 普通路径
    result = await chat_once(ctx, messages, label="回复生成")
    if app_context.stats_mgr:
        app_context.stats_mgr.record_llm(result["ms"])
    sentences = normalize_sentences(result["content"], ctx, emotions, user_text)
    if wants_voice is not False and str(result["content"] or "").strip() \
            and not has_structured_sentences(result["content"]):
        print("模型输出不是规定格式（整段纯文本），追加一次格式要求重试。")
        retry = await chat_once(
            ctx, messages + [{"role": "user", "content": _FORMAT_RETRY_HINT}],
            label="回复生成-格式重试")
        if has_structured_sentences(retry["content"]):
            sentences = normalize_sentences(retry["content"], ctx, emotions, user_text)
        else:
            print("格式重试也没有结构化输出，仍按纯文本兜底。")
    return {"sentences": sentences, "llm_ms": result["ms"], "tool_trace": [],
            "llm_calls": 1, "tool_calls": 0}


async def generate_reply_stream(ctx: RoleContext, emotions: dict, user_text: str,
                                messages: list, on_sentence=None,
                                wants_voice: Optional[bool] = None) -> dict:
    async def _consume(parser, msgs, out):
        first_token = None
        try:
            async for chunk in stream_chat(ctx, msgs, label="回复生成"):
                if chunk.get("first_token_ms") and first_token is None:
                    first_token = chunk["first_token_ms"]
                delta = chunk.get("delta") or ""
                if not delta:
                    continue
                for obj in parser.feed(delta):
                    if not sentence_obj_has_text(obj) and not sentence_obj_has_action(obj):
                        continue
                    sentence = normalize_single(obj, ctx, emotions, user_text)
                    pieces = split_multi_clause_sentences([sentence], ctx)
                    for key in SENTENCE_ACTION_KEYS:
                        if key in sentence and pieces:
                            pieces[0][key] = sentence[key]
                    for piece in pieces:
                        out.append(piece)
                        if on_sentence:
                            await on_sentence(piece)
            return first_token, None
        except Exception as e:
            return first_token, e

    parser = SentenceStreamParser()
    sentences = []
    start = time.time()
    first_ms, error = await _consume(parser, messages, sentences)
    if error is not None:
        print(f"流式请求异常: {type(error).__name__}: {error}（已收到的内容将继续处理）")

    active = parser
    if not sentences and parser.yielded == 0 and parser.buffer.strip() and not error \
            and wants_voice is not False:
        print("模型输出不是规定格式（整段纯文本），追加一次格式要求重试。")
        retry = SentenceStreamParser()
        retry_ms, retry_err = await _consume(
            retry, messages + [{"role": "user", "content": _FORMAT_RETRY_HINT}], sentences)
        first_ms = first_ms or retry_ms
        if sentences:
            active = retry
        else:
            why = f"（{type(retry_err).__name__}）" if retry_err else ""
            print(f"格式重试也没有结构化输出{why}，仍按纯文本兜底。")

    if error is not None and not sentences:
        sentence = normalize_single({"zh": error_reply_text(error),
                                     "delivery": DELIVERY_PLAIN},
                                    ctx, emotions, user_text)
        sentences.append(sentence)
        if on_sentence:
            await on_sentence(sentence)
    else:
        # 已产出时抢救末尾被截断的半句
        for piece in active.finish(ctx, user_text, emotions):
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


def _repeat_ratio_user(user_text: str, reply_text: str) -> float:
    from difflib import SequenceMatcher
    an, bn = _norm_text(user_text), _norm_text(reply_text)
    if not an or not bn:
        return 0.0
    blocks = SequenceMatcher(None, an, bn).get_matching_blocks()
    matched = sum(blk.size for blk in blocks)
    cover_user = matched / min(len(an), len(bn))
    cover_reply = matched / max(len(an), len(bn))
    if cover_reply >= 0.6:
        return cover_user
    return cover_user * (cover_reply / 0.6)


def _strip_image_claims(sentences: list) -> list:
    return [s for s in (sentences or [])
            if not image_self_claim(str(s.get("zh", "") or ""))]


def _recent_assistant_replies(history: list, rounds: int) -> list:
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

#   streaming / regen  —— 检查时机
#   compare_self / compare_user —— 比对对象
_REPEAT_GUARD_KEYS = {
    "streaming": "repeat_guard_streaming_check",
    "regen": "repeat_guard_regen_check",
    "compare_self": "repeat_guard_compare_self",
    "compare_user": "repeat_guard_compare_user",
}


def repeat_thresholds(ctx=None) -> tuple:
    src = ctx if ctx is not None else app_context.active_config()
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
    src = ctx if ctx is not None else app_context.active_config()
    flags = {}
    for name, key in _REPEAT_GUARD_KEYS.items():
        try:
            flags[name] = bool(src.get(key, True))
        except Exception:
            flags[name] = True
    return flags


def repeat_guard_summary(flags: dict) -> str:
    names = {"streaming": "流式首句检查", "regen": "整段生成后校验",
             "compare_self": "比对角色历史回复", "compare_user": "比对用户本条消息"}
    off = [label for name, label in names.items() if not flags.get(name, True)]
    if not off:
        return ""
    return "（已关闭：" + "、".join(off) + "）"


def repeat_guard_active(flags: dict) -> bool:
    return bool(flags.get("compare_self", True) or flags.get("compare_user", True))


def _log_mood_commit(new_mood, verdict):
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
    base = float(verdict.get("mood_base", verdict.get("mood", 0)) or 0)
    judged = float(verdict.get("mood", base) or 0)
    if abs(judged - base) >= 0.5:
        return f"心情值 {base:.0f}（判定用值 {judged:.0f}，偏置 {judged - base:+.0f}）"
    return f"心情值 {base:.0f}"


def _judge_gate_cause(verdict) -> str:
    if not verdict.get("llm_reply", True):
        return "LLM判定无需回复"
    probability = float(verdict.get("probability", 0) or 0)
    roll = verdict.get("roll")
    if roll is None:
        return f"概率门控未通过（回复概率 {probability:.3f}）"
    return f"概率门控未通过（掷出 {float(roll):.3f} ≥ 回复概率 {probability:.3f}）"


def _image_reply_override(verdict, has_image: bool) -> bool:
    return bool(has_image and verdict is not None and not verdict["should_reply"]
                and not verdict.get("llm_reply", True))
