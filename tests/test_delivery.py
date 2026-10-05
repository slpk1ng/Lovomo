# -*- coding: utf-8 -*-
"""发送形态（delivery）测试。

模型可以用句子级的 delivery 字段决定这条回复怎么发出去：
`chars` = 一个字一条消息（主人要求「一个字一个字说话」时用），
`plain` = 只发文字、不发语音；不填就照配置发。

这里验证这条链路是**真的落到发送动作上**，而不是只写进了提示词：
解析层保住字段、流式路径让位给批量路径、发送层按字符拆条且不合成语音。

运行: python tests/test_delivery.py     （全通过退出码 0）
"""
import asyncio
import io
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
sys.stdin = io.StringIO()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import main as M
import modules.llm_helpers as LH
from modules.adapters import segments_to_text
from modules.sender import (DELIVERY_CHAR_GAP_SECONDS, DELIVERY_MAX_MESSAGES,
                            MessageSender, voice_enabled_for)

PASS, FAIL = [], []
TMP_ROOT = ROOT / ".tmp_test" / "delivery"
USER_ID = "200"


def check(name, fn):
    try:
        result = fn()
        if not result:
            raise AssertionError(f"断言为假: {result!r}")
        PASS.append(name)
        print(f"  [PASS] {name}")
    except Exception as e:
        FAIL.append((name, f"{type(e).__name__}: {e}"))
        print(f"  [FAIL] {name}: {type(e).__name__}: {e}")


def section(title):
    print(f"\n{title}")
    print("-" * 70)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class StubNapCat:
    """记录真正发出去的文字；没有 capabilities → 语音/戳一戳都算支持。"""

    platform = "napcat"
    self_id = "100"

    def __init__(self):
        self.texts = []

    async def send_private_msg(self, user_id=0, message=None):
        self.texts.append(segments_to_text(message))

    async def send_group_msg(self, group_id=0, message=None):
        self.texts.append(segments_to_text(message))

    async def friend_poke(self, **kwargs):
        return None

    async def group_poke(self, **kwargs):
        return None


class StubSender:
    """流式 sink 用的桩：一旦被调用就记下来。"""

    def __init__(self):
        self.texts = []

    def _active_client(self):
        return StubTextOnlyClient()

    async def send_text(self, session_type, target_id, text, **kwargs):
        self.texts.append(str(text))
        return True

    async def send_voice(self, session_type, target_id, wav_path):
        return True

    async def send_poke(self, session_type, target_id, user_id):
        return True


class StubTextOnlyClient:
    """语音能力位关闭 → _tts_available() 直接返回 False，不会去起 TTS 服务。"""

    capabilities = {"voice": False, "sticker": False}


class StubVoiceClient:
    """没有 capabilities 声明 → 语音算支持，能走到「语音发送方式」那一步。"""


class StubVoiceSender:
    def _active_client(self):
        return StubVoiceClient()


def build_sender(name: str, **overrides):
    work = TMP_ROOT / name
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    config = M.ConfigLoader(str(work / "config.json"))
    config.config.update({"memory_data_path": str(work), "ref_audio_root": "",
                          "active_character": "murasame",
                          "roles": [{"character_key": "murasame", "character_name": "丛雨",
                                     "personality_prompt": "你是丛雨"}],
                          "send_retry": 0, "sticker_output_max_side": 0})
    config.config.update(overrides)
    config.roles = config._parse_roles()
    sender = MessageSender(config, M.MemoryManager(config))
    client = StubNapCat()
    sender.client = client
    return config, sender, client


def sentence(zh: str, **extra) -> dict:
    return dict({"zh": zh, "lang": zh, "display": zh, "emotion": "pingjing"}, **extra)


# ---------------------------------------------------------------------------
# T1 解析层保住 delivery
# ---------------------------------------------------------------------------
def t1_parse():
    section("T1 解析层")

    ctx = LH.RoleContext({"text_lang": "ja", "display_lang": "zh",
                          "default_voice": "pingjing", "llm_judge": True}, {})
    emotions = {"pingjing": {}}

    def normalize_keeps_chars():
        s = LH.normalize_single({"zh": "你不能这样说话。", "delivery": "chars"},
                                ctx, emotions, "一个字一个字说话")
        return s.get("delivery") == LH.DELIVERY_CHARS

    def normalize_rejects_unknown():
        for bad in ("char", "word", "CHARS ", 1, True, None):
            s = LH.normalize_single({"zh": "你好呀。", "delivery": bad},
                                    ctx, emotions, "在吗")
            if "delivery" in s and s["delivery"] != LH.DELIVERY_CHARS:
                return False
        return True

    def sentences_attach_to_first():
        raw = json.dumps({"sentences": [
            {"zh": "你不能这样说话。", "ja": "そんな話し方しないで。",
             "emotion": "pingjing", "delivery": "chars"},
            {"zh": "人家会害羞的呀。", "ja": "恥ずかしくなっちゃう。",
             "emotion": "pingjing"}]}, ensure_ascii=False)
        out = LH.normalize_sentences(raw, ctx, emotions, "一个字一个字说话")
        return (len(out) == 2 and out[0].get("delivery") == LH.DELIVERY_CHARS
                and "delivery" not in out[1])

    def split_keeps_action_keys():
        # 分句会重建句子对象：动作字段得能挪到第一条上（流式路径靠这个）
        src = sentence("你不能这样说话。人家会害羞的呀。", delivery="chars", poke=True)
        pieces = LH.split_multi_clause_sentences([src])
        for key in LH.SENTENCE_ACTION_KEYS:
            if key in src and pieces:
                pieces[0][key] = src[key]
        return (len(pieces) == 2 and pieces[0].get("delivery") == "chars"
                and pieces[0].get("poke") is True and "delivery" not in pieces[1])

    check("normalize_single 保留 chars", normalize_keeps_chars)
    check("normalize_single 丢弃白名单外的取值", normalize_rejects_unknown)
    check("normalize_sentences 把 delivery 挂到第一句", sentences_attach_to_first)
    check("分句后动作字段能挪到第一条", split_keeps_action_keys)


# ---------------------------------------------------------------------------
# T2 发送层
# ---------------------------------------------------------------------------
def t2_send():
    section("T2 发送层")

    def chars_one_by_one():
        # 用 ASCII 让断言与中文字序无关；标点是角色有意写的，要跟着发出去
        config, sender, client = build_sender("delivery_chars")
        result = run(sender.send_reply(
            "private", USER_ID, [sentence("abcd.", delivery="chars")],
            {}, M.RoleContext(config.config), use_voice=True))
        return (client.texts == ["a", "b", "c", "d", "."]
                and result["tts_calls"] == 0 and result["voice_ok"] is False)

    def chars_across_sentences():
        config, sender, client = build_sender("delivery_chars_multi")
        run(sender.send_reply(
            "private", USER_ID,
            [sentence("ab.", delivery="chars"), sentence("cd.")],
            {}, M.RoleContext(config.config), use_voice=True))
        return client.texts == ["a", "b", ".", "c", "d", "."]

    def chars_capped():
        config, sender, client = build_sender("delivery_cap")
        long_text = "a" * (DELIVERY_MAX_MESSAGES + 5) + "."
        run(sender.send_reply("private", USER_ID, [sentence(long_text, delivery="chars")],
                              {}, M.RoleContext(config.config), use_voice=True))
        # 超上限退回按句发：只有一条，内容是整句
        return client.texts == [long_text]

    def plain_sends_text_only():
        config, sender, client = build_sender("delivery_plain")
        result = run(sender.send_reply(
            "private", USER_ID,
            [sentence("你先说。", delivery="plain"), sentence("人家听着呢。")],
            {}, M.RoleContext(config.config), use_voice=True))
        return (client.texts == ["你先说。", "人家听着呢。"]
                and result["tts_calls"] == 0)

    def without_delivery_unchanged():
        # 没有 delivery 时不能走这条分支（走原逻辑，语音照常合成）
        config, sender, client = build_sender("delivery_absent")
        result = run(sender.send_reply("private", USER_ID, [sentence("你好呀。")],
                                       {}, M.RoleContext(config.config), use_voice=False))
        return client.texts == ["你好呀。"] and result["tts_calls"] == 0

    def pacing_is_used():
        # 逐字之间要留打字间隔，否则十几条消息瞬间糊上去
        return DELIVERY_CHAR_GAP_SECONDS > 0

    check("chars 一个字一条消息、不合成语音", chars_one_by_one)
    check("chars 跨多个句子继续拆字", chars_across_sentences)
    check(f"chars 超过 {DELIVERY_MAX_MESSAGES} 条退回按句发", chars_capped)
    check("plain 按句发文字、不合成语音", plain_sends_text_only)
    check("没有 delivery 的回复走原逻辑", without_delivery_unchanged)
    check("逐字之间有打字间隔", pacing_is_used)


# ---------------------------------------------------------------------------
# T3 流式路径让位
# ---------------------------------------------------------------------------
def t3_stream_yield():
    section("T3 流式路径让位")

    def sink_defers_to_batch():
        config, _sender, _client = build_sender("delivery_sink")
        stub = StubSender()
        sink = M.SentenceSink("private", USER_ID, {}, M.RoleContext(config.config),
                              poke_target=USER_ID)
        saved, M.sender = M.sender, stub
        M.app_context.sender = M.sender
        try:
            run(sink.on_sentence(sentence("你不能这样说话。", delivery="chars")))
            run(sink.on_sentence(sentence("人家会害羞的呀。")))
            run(sink.flush())
        finally:
            M.sender = saved
            M.app_context.sender = M.sender
        return sink.batch_only and sink.sent == 0 and stub.texts == []

    def sink_normal_still_streams():
        config, _sender, _client = build_sender("delivery_sink_normal")
        stub = StubSender()
        sink = M.SentenceSink("private", USER_ID, {}, M.RoleContext(config.config),
                              poke_target=USER_ID)
        saved, M.sender = M.sender, stub
        M.app_context.sender = M.sender
        try:
            run(sink.on_sentence(sentence("人家在听着呢。")))
            run(sink.flush())
        finally:
            M.sender = saved
            M.app_context.sender = M.sender
        return (not sink.batch_only and sink.sent == 1
                and stub.texts == ["人家在听着呢。"])

    def sink_keeps_sentence_with_late_delivery():
        # 发送形态出现在第二句：这条回复已经在流式发了，让位也来不及，
        # 剩下的句子必须照常发出去（曾经会被整批丢掉，QQ 上就少了一截）
        config, _sender, _client = build_sender("delivery_sink_late")
        stub = StubSender()
        sink = M.SentenceSink("private", USER_ID, {}, M.RoleContext(config.config),
                              poke_target=USER_ID)
        saved, M.sender = M.sender, stub
        M.app_context.sender = M.sender
        try:
            async def flow():
                await sink.on_sentence(sentence("你不能这样说话。"))
                await sink.on_sentence(sentence("人家会害羞的呀。", delivery="chars"))
                await sink.flush()

            run(flow())
        finally:
            M.sender = saved
            M.app_context.sender = M.sender
        return (not sink.batch_only and sink.sent == 2 and not sink.unsent
                and stub.texts == ["你不能这样说话。", "人家会害羞的呀。"])

    def sink_first_sentence_still_defers():
        # 首句就带发送形态时仍要让位（这时候还没发过任何东西，让位不会重复）
        config, _sender, _client = build_sender("delivery_sink_first")
        stub = StubSender()
        sink = M.SentenceSink("private", USER_ID, {}, M.RoleContext(config.config),
                              poke_target=USER_ID)
        saved, M.sender = M.sender, stub
        M.app_context.sender = M.sender
        try:
            async def flow():
                await sink.on_sentence(sentence("你不能这样说话。", delivery="chars"))
                await sink.on_sentence(sentence("人家会害羞的呀。"))
                await sink.flush()

            run(flow())
        finally:
            M.sender = saved
            M.app_context.sender = M.sender
        return sink.batch_only and sink.sent == 0 and stub.texts == []

    check("首句带 delivery 时 sink 一条都不发", sink_defers_to_batch)
    check("普通回复仍然逐句流式发", sink_normal_still_streams)
    check("第二句才带 delivery 时后续句子照发不误", sink_keeps_sentence_with_late_delivery)
    check("首句带 delivery 时仍整条让位", sink_first_sentence_still_defers)


# ---------------------------------------------------------------------------
# T4 提示词接口说明
# ---------------------------------------------------------------------------
def t4_prompt():
    section("T4 提示词接口说明")

    def rule_present():
        cfg = M.ConfigLoader.default_config()
        prompt = LH.build_system_prompt(LH.RoleContext(cfg, {}), {})
        return ("【发送形态】" in prompt and LH.DELIVERY_CHARS in prompt
                and LH.DELIVERY_PLAIN in prompt)

    check("系统提示词说明 delivery 的两个取值", rule_present)


# ---------------------------------------------------------------------------
# T5 选择性发送语音
# ---------------------------------------------------------------------------
def t5_voice_mode():
    section("T5 选择性发送语音")

    def modes():
        return (voice_enabled_for({"reply_voice_mode": "always"}, "group") is True
                and voice_enabled_for({"reply_voice_mode": "private"}, "private") is True
                and voice_enabled_for({"reply_voice_mode": "private"}, "group") is False
                and voice_enabled_for({"reply_voice_mode": "chance",
                                       "reply_voice_chance": 0}, "private") is False
                and voice_enabled_for({"reply_voice_mode": "chance",
                                       "reply_voice_chance": 1}, "private") is True
                and voice_enabled_for({"reply_voice_mode": "不认识"}, "group") is True)

    def group_text_only():
        config, sender, client = build_sender("voice_private", reply_voice_mode="private")
        result = run(sender.send_reply("group", "999", [sentence("群里就打字吧。")],
                                       {}, M.RoleContext(config.config), use_voice=True))
        return result["tts_calls"] == 0 and client.texts == ["群里就打字吧。"]

    def private_keeps_voice():
        config, _sender, _client = build_sender("voice_private_chat",
                                                reply_voice_mode="private")
        return voice_enabled_for(config.config, "private") is True

    def sink_skips_tts():
        config, _sender, _client = build_sender("voice_sink", reply_voice_mode="private")
        saved_global, saved_sender = M.global_config, M.sender
        M.global_config, M.sender = config, StubVoiceSender()
        M.app_context.global_config = M.global_config
        M.app_context.sender = M.sender
        try:
            sink = M.SentenceSink("group", "999", {}, M.RoleContext(config.config))
            return run(sink._tts_available()) is False
        finally:
            M.global_config, M.sender = saved_global, saved_sender
            M.app_context.global_config = M.global_config
            M.app_context.sender = M.sender

    check("语音发送方式三种取值", modes)
    check("群聊在「只在私聊发语音」下不发语音", group_text_only)
    check("私聊在「只在私聊发语音」下仍发语音", private_keeps_voice)
    check("流式路径同样按语音发送方式跳过 TTS", sink_skips_tts)


def main():
    print("=" * 70)
    print("发送形态（delivery）测试")
    print("=" * 70)
    if TMP_ROOT.exists():
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    saved_global, M.global_config = M.global_config, M.ConfigLoader.default_config()
    M.app_context.global_config = M.global_config
    try:
        for fn in (t1_parse, t2_send, t3_stream_yield, t4_prompt, t5_voice_mode):
            try:
                fn()
            except Exception as e:
                FAIL.append((fn.__name__, repr(e)))
                print(f"  [FAIL] {fn.__name__} 执行异常: {type(e).__name__}: {e}")
    finally:
        M.global_config = saved_global
        M.app_context.global_config = M.global_config
    print(f"\n结果：通过 {len(PASS)} | 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：")
        for name, reason in FAIL:
            print(f"  - {name}: {reason}")
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
