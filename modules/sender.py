"""统一消息发送器：文本/语音/表情包发送，供主消息管线与定时、主动消息共用。"""
import asyncio
import contextlib
import contextvars
import time
from pathlib import Path
from typing import List, Optional

from .llm_helpers import (RoleContext, segment_for_tts, lang_text_broken,
                          translate_to_lang, available_mimics)
from .tts import synthesize_sentence, merge_wavs, get_audio_duration
from .tts_service import check_tts_service
from .stickers import resize_for_output


# 本次发送使用的 NapCat 连接（多账号时按角色临时切换；未设置则用默认连接）
_CURRENT_CLIENT = contextvars.ContextVar("lovomo_send_client", default=None)


def _normalize_target(session_type: str, target_id) -> tuple:
    """兼容传入完整会话ID的情形：private_10001 / group_456 / group_456_789
    （待办提醒等模块保存的是 session_id，而发送需要纯数字号码）。"""
    s = str(target_id)
    if s.startswith("private_"):
        return "private", s.split("_", 1)[1]
    if s.startswith("group_"):
        return "group", s.split("_", 1)[1].split("_")[0]
    return session_type, target_id


_VOICE_GAP_LEAD = 0.5     # 语音播完后额外留的间隔
_VOICE_GAP_FIXED = 0.2    # 关闭动态等待时每条语音之间的固定间隔


class VoicePacer:
    """语音发送节奏：只在「下一条语音要发之前」等上一条播完。

    历史实现是"发完一条就 sleep(音频时长)"，连最后一条也照睡。这段等待发生在
    会话锁里，于是排队中的下一条用户消息要等上一条语音播完才送进 LLM，
    表现为"回复延迟恰好等于上一条语音的长度"。改成发下一条前才等之后：
    句间节奏完全不变，收尾那次等待被去掉，发送时长一律取自刚发出去的那条语音。
    """

    def __init__(self, dynamic: bool = True, gap: float = _VOICE_GAP_LEAD,
                 fixed_gap: float = _VOICE_GAP_FIXED):
        self.dynamic = bool(dynamic)
        self.gap = gap
        self.fixed_gap = fixed_gap
        self._until = 0.0

    async def wait(self):
        """发下一条之前调用：上一条还在播就在这里等到播完。"""
        delay = self._until - time.monotonic()
        self._until = 0.0
        if delay > 0:
            await asyncio.sleep(delay)

    async def hold_voice(self, path) -> float:
        """登记刚发出去的这条语音占用的播放时间（时长取自这条语音本身）。"""
        if self.dynamic and path is not None:
            seconds = await asyncio.to_thread(get_audio_duration, str(path))
            span = float(seconds or 0.0) + self.gap
        else:
            span = self.fixed_gap
        self._until = max(self._until, time.monotonic() + span)
        return span


class MessageSender:
    """封装 NapCat 客户端发送行为。client 由主程序注入（连接后设置）。

    多账号时（每个角色一条 NapCat 连接）由 using_client() 指定本次发送用哪条
    连接：它往 contextvars 里写，所以并发的消息任务互不干扰。
    """

    def __init__(self, config, memory_manager, sticker_manager=None, stats=None):
        self.config = config
        self.memory_manager = memory_manager
        self.sticker_manager = sticker_manager
        self.stats = stats
        self.client = None  # NapCatClient，主循环连接后注入
        self.role_clients = {}  # 角色标识符 -> 该角色独立的 NapCatClient

    # ---------------- 连接选择 ----------------
    def set_role_client(self, role_key: str, client) -> None:
        """登记/注销某个角色独占的 NapCat 连接。"""
        key = str(role_key or "").strip()
        if not key:
            return
        if client is None:
            self.role_clients.pop(key, None)
        else:
            self.role_clients[key] = client

    def client_for(self, role) -> object:
        """取某个角色发送时该用的连接：配了独占连接就用它，否则用默认连接。"""
        role = role or {}
        key = str(role.get("character_key", "") or "").strip()
        if key and str(role.get("napcat_ws_url") or "").strip():
            client = self.role_clients.get(key)
            if client is not None:
                return client
        return self.client

    @contextlib.contextmanager
    def using_client(self, client):
        """临时把后续发送切到指定连接（为 None 时回到默认连接）。"""
        token = _CURRENT_CLIENT.set(client)
        try:
            yield
        finally:
            _CURRENT_CLIENT.reset(token)

    def _active_client(self):
        return _CURRENT_CLIENT.get() or self.client

    # ---------------- 基础发送 ----------------
    async def send_segments(self, session_type: str, target_id, segments: list) -> bool:
        client = self._active_client()
        if client is None:
            print("发送失败：NapCat 客户端尚未连接")
            return False
        session_type, target_id = _normalize_target(session_type, target_id)
        try:
            await self._send_with_retry(session_type, target_id, segments)
            return True
        except Exception as e:
            print(f"发送消息失败 ({session_type} {target_id}): {type(e).__name__}: {e}")
            return False

    async def _send_with_retry(self, session_type: str, target_id, segments: list):
        """NTQQ 的 sendMsg 偶发等不到消息列表更新确认（NapCat 报 NTEvent Timeout），
        此时消息通常并未发出。稍候重试，避免整句丢失；次数由 send_retry 配置，默认 1。"""
        client = self._active_client()
        retries = max(0, int(self.config.get("send_retry", 1) or 0))
        for attempt in range(retries + 1):
            try:
                if session_type == "private":
                    await client.send_private_msg(user_id=int(target_id), message=segments)
                else:
                    await client.send_group_msg(group_id=int(target_id), message=segments)
                return
            except (asyncio.TimeoutError, TimeoutError):
                if attempt >= retries:
                    raise
                delay = 2.0 * (attempt + 1)
                print(f"发送超时 ({session_type} {target_id})，{delay:.0f}s 后重试 "
                      f"({attempt + 1}/{retries})")
                await asyncio.sleep(delay)

    async def send_text(self, session_type: str, target_id, text: str,
                        sticker=None) -> bool:
        from napcat import Text, Image
        segments = []
        # 只在有文本时才加 Text 段：带表情包但不带文字时若塞入空 Text，
        # 消息段列表会出现一个空文本段（部分客户端会显示空白气泡）。
        if text and str(text).strip():
            segments.append(Text(text=text))
        temp_sticker = None
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
        try:
            return await self.send_segments(session_type, target_id, segments)
        finally:
            if temp_sticker is not None:
                Path(temp_sticker).unlink(missing_ok=True)

    async def send_voice(self, session_type: str, target_id, wav_path) -> bool:
        from napcat import Record
        return await self.send_segments(session_type, target_id,
                                        [Record(file=str(Path(wav_path).resolve()))])

    # ---------------- 回复合送（保持原有分合逻辑 + 表情包） ----------------
    async def send_reply(self, session_type: str, target_id: str, sentences: List[dict],
                        emotions: dict, ctx: RoleContext, use_voice: bool = True) -> dict:
        """按配置发送整组句子（文本+语音），返回 {tts_ms, voice_ok}。

        sentences: [{zh, lang, display, emotion}]
        """
        result = {"tts_ms": 0.0, "voice_ok": False, "tts_calls": 0}
        if not sentences:
            return result
        data_path = self.memory_manager.data_path
        separate_send = self.config.get("separate_send", False)
        send_voice_separately = self.config.get("send_voice_separately", False)
        text_separate = self.config.get("text_separate", False)
        dynamic_sleep = self.config.get("dynamic_sleep", True)
        use_tts = use_voice and self.config.get("tts_reply_enabled", True)
        if separate_send and (send_voice_separately or text_separate) \
                and self.config.get("separate_force_segment", True):
            sentences = segment_for_tts(sentences)

        # 1. 合成语音（全部句子一次性合成，或逐个合成，取决于是否需要分开）
        wavs: List[Optional[Path]] = [None] * len(sentences)
        if use_tts and self._active_client() is not None:
            synth_started = time.time()
            synth_sem = asyncio.Semaphore(2)
            async def _synthesize(s):
                async with synth_sem:
                    # 参数顺序：text=要念的台词(lang)，emotion=情绪名。
                    # 早期版本这里传反了，导致情绪被当成文本、文本被当成情绪，
                    # 情绪永远回退默认音色。synthesize_sentence 内另有兜底纠正。
                    return await synthesize_sentence(ctx, s["lang"], s.get("emotion", ""), emotions,
                                                     data_path, stats=self.stats,
                                                     mimic=s.get("mimic", ""),
                                                     mimics=available_mimics(ctx))
            results = await asyncio.gather(*[_synthesize(s) for s in sentences],
                                           return_exceptions=True)
            # 这条路径的耗时以前从不赋值，interactions.tts_ms 恒为 0，
            # 统计页的「平均 TTS 耗时」被系统性低估
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

        # 表情包选择：默认只挑第一句情绪；sticker_every_sentence 开启时逐句挑，
        # 累计张数受 sticker_max_per_reply 限制
        sticker_sent = 0

        async def _pick_sticker_for(idx: int):
            nonlocal sticker_sent
            if self.sticker_manager is None or self._active_client() is None:
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

        # ========== 分开发送 + 语音分开发送 ==========
        if separate_send and send_voice_separately:
            missing = []
            done_stickers = []
            pacer = VoicePacer(dynamic_sleep)
            for idx, wav in enumerate(wavs):
                sentence_text = sentences[idx]["display"]
                # ① 语音失败的句子先记下，稍后统一补发文本，避免文本重复发送
                if not wav or not wav.exists():
                    missing.append(idx)
                    continue
                # ② 逐句发送文本 + 对应语音（等上一条播完，用的是上一条自己的时长）
                await pacer.wait()
                if sentence_text:
                    await self.send_text(session_type, target_id, sentence_text)
                await self.send_voice(session_type, target_id, wav)
                await pacer.hold_voice(wav)
                wav.unlink(missing_ok=True)
                sticker_i = await _pick_sticker_for(idx)
                if sticker_i:
                    done_stickers.append(sticker_i)

            # ③ 如果所有语音都失败，降级发送合并文本
            if not valid_wavs:
                await self.send_text(session_type, target_id,
                                    "".join(s["display"] for s in sentences))
            else:
                # ④ 处理语音失败的句子（补发文本，只发一次）
                for idx in missing:
                    text = sentences[idx]["display"]
                    if text:
                        await self.send_text(session_type, target_id, text)

            # ⑤ 最后统一发送表情包
            for sticker_path in done_stickers:
                await self.send_text(session_type, target_id, "", sticker=sticker_path)

        # ========== 合并发送（默认或文字分开但语音合并） ==========
        else:
            # 合并是 CPU 密集的同步操作（numpy 拼接可能耗时数百毫秒），
            # 放进线程池避免阻塞事件循环
            combined_audio = await asyncio.to_thread(
                merge_wavs, valid_wavs, self.config, data_path) if valid_wavs else None
            if valid_wavs and not combined_audio:
                # 合并失败（历史上 merge_wavs 遇到异常会静默返回 None）时，
                # 退回无损直接拼接，绝不因为合并失败就把整条语音丢掉。
                from .tts import simple_concat_wavs
                combined_audio = await asyncio.to_thread(
                    simple_concat_wavs, valid_wavs, data_path)
                if combined_audio:
                    print("合并音频已降级为无损直接拼接（内容不丢）。")
            combined_text = "".join(s["display"] for s in sentences)

            if combined_audio:
                # 如果开启了文字分开发送（但语音合并），则先发语音，再逐句发文字
                if separate_send and text_separate:
                    await self.send_voice(session_type, target_id, combined_audio)
                    for i, s in enumerate(sentences):
                        await self.send_text(session_type, target_id, s["display"])
                        # 文字之间采用固定间隔（如果希望基于语音时长，可改为语音时长）
                        await asyncio.sleep(0.2)
                else:
                    # 正常合并发送：文字+语音一起发。
                    # 这条语音是本轮的最后一条，后面没有要发的语音，
                    # 再等它播完只会把整条会话锁住、拖慢排队的下一条消息
                    await self.send_text(session_type, target_id, combined_text)
                    await self.send_voice(session_type, target_id, combined_audio)

                # 清理临时文件
                for w in valid_wavs:
                    w.unlink(missing_ok=True)
                combined_audio.unlink(missing_ok=True)
            else:
                # 语音合成失败，降级纯文本
                print("TTS 合成失败或未启用，降级为纯文本。")
                await self.send_text(session_type, target_id, combined_text)
                for w in valid_wavs:
                    w.unlink(missing_ok=True)

            # 最后发送表情包（合并路径最多一张）
            sticker = await _pick_sticker_for(0)
            if sticker:
                await self.send_text(session_type, target_id, "", sticker=sticker)

        await asyncio.to_thread(self.memory_manager.cleanup_voice_cache,
                                self.config.get("max_voice_cache", 20))
        return result

    # ---------------- 主动消息（定时/提醒/问候） ----------------
    async def speak_and_send(self, session_type: str, target_id, text: str,
                             emotions: dict, ctx: Optional[RoleContext] = None,
                             use_voice: bool = None, sticker: bool = False,
                             emotion: str = "", session_id: str = "") -> bool:
        if not text:
            return False
        if self._active_client() is None:
            print("主动消息发送失败：NapCat 客户端未连接")
            return False
        if ctx is None:
            ctx = RoleContext(self.config)
        if use_voice is None:
            use_voice = bool(self.config.get("proactive_voice", False))
        sticker_path = (await self.sticker_manager.pick_async(ctx, emotion, text)
                        if (sticker and self.sticker_manager) else None)
        if use_voice and await check_tts_service(self.config):
            voice = str(emotion or ctx.get("default_voice", "pingjing") or "pingjing")
            speech_text = await self._speech_text_for(ctx, text)
            wav = await self._synthesize_proactive(ctx, speech_text, voice, emotions)
            if wav:
                try:
                    # 文本是正文，语音只是载体：正文发出即视为成功，
                    # 否则调用方会把同一句话重发一遍
                    text_ok = await self.send_text(session_type, target_id, text)
                    await self.send_voice(session_type, target_id, wav)
                    if sticker_path:
                        await self.send_text(session_type, target_id, "", sticker=sticker_path)
                    if text_ok:
                        self.record_outgoing_history(session_id, text, ctx)
                    return bool(text_ok)
                finally:
                    wav.unlink(missing_ok=True)
        ok = await self.send_text(session_type, target_id, text, sticker=sticker_path)
        if ok:
            self.record_outgoing_history(session_id, text, ctx)
        return ok

    def record_outgoing_history(self, session_id: str, text: str, ctx=None) -> None:
        """把主动发出的消息记进会话历史（问候/提醒/主动开口走这条）。

        这些消息不经过回复管线，此前只发不记：用户接着回复时模型看不到自己
        刚说过什么，WebUI 的聊天记录里也找不到这条主动问候。
        """
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
        """纯文本消息的合成台词：语音要念角色语言，展示文本是别种语言时先译过去。

        主动消息/提醒/问候的话术是展示语言（如中文）写出来的，而角色的语音
        参考音频与台词语言是另一种语言（如日语）：直接拿展示文本去合成，念出来
        就是另一种语言的语音。这里按配置的文本语言重译一份专供合成，
        译不出来就退回原文（由合成侧按文字判定语言，保证能听懂）。
        """
        target = str(ctx.get("text_lang", "") or "").strip().lower()
        if not target or target == "auto":
            return text
        if not lang_text_broken(text, target):
            return text
        translated = await translate_to_lang(ctx, text, target)
        if translated and translated != text:
            print(f"主动消息语音语言修复：展示文本不是{target}，已按{target}重译后合成"
                  f"（{translated[:40]!r}）")
            return translated
        print(f"主动消息语音语言修复失败，改按台词文字判定语言合成：{text[:40]!r}")
        return text

    async def _synthesize_proactive(self, ctx, text: str, emotion: str, emotions: dict):
        """主动消息语音：按句切分逐句合成，任一句失败则整段兜底重试。"""
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
        """合并分段音频：优先带语气渐变，失败则逐级降级，绝不返回"半截语音"。

        同时校验输出文件真实存在且非空——旧版本的 merge_wavs 在缓冲区极短时
        会抛 numpy 广播异常并返回 None，调用方却直接把它当成功结果使用。

        keep_on_fail：合并彻底失败时保留第一段（供调用方退回单段语音），
        而不是把它一起删掉、返回一个已不存在的路径。
        """
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
