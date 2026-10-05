# -*- coding: utf-8 -*-
"""@ 写成普通文字、逐字发送丢标点、生成失败的提醒三件事的测试。

覆盖：
1. 模型把 @ 写成普通文字（「@」「@QQ号」「@昵称」）或只写占位符不填 mention_ids 时，
   发送层要还原成真正的 @（被@的人才会收到提醒）；
2. 逐字发送（delivery=chars）不能把标点（尤其是 @）当成"纯标点"整字丢掉，
   @ 占位符也不能被拆成「a」「t」两条消息；
3. 生成失败发回会话的是中文提醒，不含上游原始报错，且这条消息不合成语音。

运行: python tests/test_mention_chars_error.py     （全通过退出码 0）
"""
import asyncio
import io
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
import modules.sender as S
from modules.adapters import segment_to_dict
from modules.sender import MessageSender as Sender

PASS, FAIL = [], []
TMP_ROOT = ROOT / ".tmp_test" / "mention_chars_error"
BOT_ID = "100"
USER_ID = "200"
OTHER_ID = "300"
GROUP_ID = "999"
NICK = "困了就去睡觉"

EMOTIONS = {"pingjing": {}, "gaoxing": {}}


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


def fresh_dir(name: str) -> Path:
    path = TMP_ROOT / name
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def ctx_of(**overrides):
    cfg = {"text_lang": "ja", "display_lang": "zh", "default_voice": "pingjing",
           "llm_judge": True, "poke_enabled": True}
    cfg.update(overrides)
    return LH.RoleContext(cfg, {})


class StubClient:
    """记录每次发送的消息段，便于断言里面有没有真正的 at 段。"""

    platform = "napcat"

    def __init__(self):
        self.self_id = BOT_ID
        self.messages = []
        self._n = 0

    def _next_id(self):
        self._n += 1
        return f"m{self._n}"

    async def send_group_msg(self, group_id, message):
        self.messages.append(list(message or []))
        return {"message_id": self._next_id()}

    async def send_private_msg(self, user_id, message):
        self.messages.append(list(message or []))
        return {"message_id": self._next_id()}

    async def delete_msg(self, message_id):
        return {}


def build_sender(name: str, **overrides):
    work = fresh_dir(name)
    config = M.ConfigLoader(str(work / "config.json"))
    config.config.update({"memory_data_path": str(work), "ref_audio_root": "",
                          "active_character": "murasame",
                          "roles": [{"character_key": "murasame", "character_name": "丛雨",
                                     "personality_prompt": "你是丛雨"}],
                          "send_retry": 0, "sticker_output_max_side": 0})
    config.config.update(overrides)
    config.roles = config._parse_roles()
    sender = Sender(config, M.MemoryManager(config))
    client = StubClient()
    sender.client = client
    return sender, client


def sentence(zh, **extra):
    data = {"zh": zh, "lang": zh, "display": zh, "emotion": "pingjing"}
    data.update(extra)
    return data


def at_segments(message):
    return [segment_to_dict(seg) for seg in message
            if segment_to_dict(seg).get("type") == "at"]


def text_segments(message):
    return [str((segment_to_dict(seg).get("data") or {}).get("text") or "")
            for seg in message if segment_to_dict(seg).get("type") == "text"]


def all_at_segments(messages):
    return [seg for m in messages for seg in at_segments(m)]


def all_text(messages):
    return "".join(t for m in messages for t in text_segments(m))


# ---------------------------------------------------------------------------
# T1 台词里写成普通文字的 @
# ---------------------------------------------------------------------------
def t1_apply_literal_mention():
    section("T1 台词里写成两个 @ 的 @ 还原成真 @")

    def bare_at_single_target():
        s = {"zh": "@@还要本座搞什么名堂呀", "display": "@@还要本座搞什么名堂呀"}
        ok = LH.apply_literal_mention(s, [USER_ID], {}, "")
        return (ok and s["mention_ids"] == [USER_ID]
                and s["zh"] == f"{LH.MENTION_PLACEHOLDER}还要本座搞什么名堂呀"
                and s["display"] == s["zh"])

    def bare_at_prefers_speaker():
        s = {"zh": "@@ 你说什么", "display": "@@ 你说什么"}
        ok = LH.apply_literal_mention(s, [OTHER_ID, USER_ID], {}, USER_ID)
        return ok and s["mention_ids"] == [USER_ID]

    def bare_at_ambiguous_untouched():
        s = {"zh": "@@ 你说什么", "display": "@@ 你说什么"}
        return not LH.apply_literal_mention(s, [OTHER_ID, USER_ID], {}, "") \
            and "mention_ids" not in s

    def qq_written_out():
        s = {"zh": f"@@{USER_ID} 你听好了", "display": f"@@{USER_ID} 你听好了"}
        ok = LH.apply_literal_mention(s, [USER_ID], {}, "")
        return ok and s["mention_ids"] == [USER_ID] \
            and s["zh"] == f"{LH.MENTION_PLACEHOLDER} 你听好了"

    def unknown_qq_untouched():
        s = {"zh": "@@999999 你听好了", "display": "@@999999 你听好了"}
        return not LH.apply_literal_mention(s, [USER_ID], {}, USER_ID) \
            and "mention_ids" not in s

    def nickname_written_out():
        s = {"zh": f"@@{NICK} 快看", "display": f"@@{NICK} 快看"}
        ok = LH.apply_literal_mention(s, [USER_ID], {USER_ID: NICK}, "")
        return ok and s["mention_ids"] == [USER_ID] \
            and s["zh"] == f"{LH.MENTION_PLACEHOLDER} 快看"

    def placeholder_without_ids():
        text = f"{LH.MENTION_PLACEHOLDER}主人真坏"
        s = {"zh": text, "display": text}
        ok = LH.apply_literal_mention(s, [USER_ID], {}, USER_ID)
        return ok and s["mention_ids"] == [USER_ID] and s["zh"] == text

    def placeholder_with_ids_untouched():
        text = f"{LH.MENTION_PLACEHOLDER}主人真坏"
        s = {"zh": text, "display": text, "mention_ids": [USER_ID]}
        return not LH.apply_literal_mention(s, [USER_ID, OTHER_ID], {}, OTHER_ID)

    def no_allowed_ids_untouched():
        s = {"zh": "@@你听好了", "display": "@@你听好了"}
        return not LH.apply_literal_mention(s, [], {}, USER_ID) \
            and "mention_ids" not in s

    def idempotent():
        s = {"zh": "@@你听好了", "display": "@@你听好了"}
        LH.apply_literal_mention(s, [USER_ID], {}, USER_ID)
        return not LH.apply_literal_mention(s, [USER_ID], {}, USER_ID)

    def no_at_untouched():
        s = {"zh": "主人真坏", "display": "主人真坏"}
        return not LH.apply_literal_mention(s, [USER_ID], {}, USER_ID) \
            and "mention_ids" not in s

    def single_at_left_alone():
        # 单个 @ 是普通文字：角色只是在谈论「@所有人」这件事，不是要真@人
        text = "@所有人なんて権限、吾輩にはないんだよ。"
        s = {"zh": text, "display": text}
        return not LH.apply_literal_mention(s, [USER_ID, LH.MENTION_ALL_ID], {}, USER_ID) \
            and "mention_ids" not in s and s["zh"] == text

    def all_members_written_out():
        s = {"zh": "@@所有人 快来看", "display": "@@所有人 快来看"}
        ok = LH.apply_literal_mention(s, [USER_ID], {}, USER_ID)
        return (ok and s["mention_ids"] == [LH.MENTION_ALL_ID]
                and s["zh"] == f"{LH.MENTION_PLACEHOLDER} 快来看")

    def single_at_beside_double_at():
        # 一句里既有普通 @ 又有真要@的：只动两个 @ 的那个
        s = {"zh": "@所有人 还有 @@你", "display": "@所有人 还有 @@你"}
        ok = LH.apply_literal_mention(s, [USER_ID], {}, USER_ID)
        return (ok and s["mention_ids"] == [USER_ID]
                and s["zh"] == f"@所有人 还有 {LH.MENTION_PLACEHOLDER}你")

    check("单个可@对象时两个 @ 被还原", bare_at_single_target)
    check("多个可@对象时优先当前发言者", bare_at_prefers_speaker)
    check("多个可@对象且没写清对象时不乱@", bare_at_ambiguous_untouched)
    check("写成 @@QQ号 时按号还原", qq_written_out)
    check("模型编造的 QQ 号不拿兜底对象顶上", unknown_qq_untouched)
    check("写成 @@昵称 时按昵称还原", nickname_written_out)
    check("只写占位符没填 mention_ids 时补上目标", placeholder_without_ids)
    check("占位符与 mention_ids 都写好时不动它", placeholder_with_ids_untouched)
    check("没有可@对象时不改台词", no_allowed_ids_untouched)
    check("重复调用不会重复改写", idempotent)
    check("台词里没有 @ 时不动它", no_at_untouched)
    check("单个 @ 一律当普通文字", single_at_left_alone)
    check("@@所有人 换成真@全体成员", all_members_written_out)
    check("同一句里单个 @ 不动、两个 @ 照常还原", single_at_beside_double_at)


# ---------------------------------------------------------------------------
# T2 逐字拆句保留标点
# ---------------------------------------------------------------------------
def t2_char_pieces():
    section("T2 逐字发送的拆句")

    def punctuation_kept():
        return S._char_pieces("@你好。") == ["@", "你", "好", "。"]

    def spaces_dropped():
        return S._char_pieces("你 好\n啊") == ["你", "好", "啊"]

    def placeholder_atomic():
        text = f"{LH.MENTION_PLACEHOLDER}你好"
        return S._char_pieces(text) == [LH.MENTION_PLACEHOLDER, "你", "好"]

    def placeholder_in_middle():
        text = f"你{LH.MENTION_PLACEHOLDER}好"
        return S._char_pieces(text) == ["你", LH.MENTION_PLACEHOLDER, "好"]

    check("单个标点（含 @）不再被丢掉", punctuation_kept)
    check("空白被丢掉", spaces_dropped)
    check("占位符整块保留、不拆成 a/t", placeholder_atomic)
    check("占位符在句中也整块保留", placeholder_in_middle)


# ---------------------------------------------------------------------------
# T3 发送路径落到真正的 at 段
# ---------------------------------------------------------------------------
def t3_real_at_segment():
    section("T3 发送路径产出真正的 at 段")

    def chars_path_bare_at():
        sender, client = build_sender("chars_bare_at")

        async def flow():
            return await sender.send_reply(
                "group", GROUP_ID,
                [sentence("@@还要本座搞什么名堂呀", delivery="chars")],
                EMOTIONS, ctx_of(), use_voice=False,
                allowed_at_ids=[USER_ID], speaker_id=USER_ID)

        run(flow())
        joined = all_text(client.messages)
        ats = all_at_segments(client.messages)
        return (ats and ats[0]["data"].get("qq") == USER_ID
                and "@" not in joined and "还" in joined)

    def chars_path_keeps_punctuation():
        sender, client = build_sender("chars_punct")

        async def flow():
            return await sender.send_reply(
                "group", GROUP_ID, [sentence("你好。", delivery="chars")],
                EMOTIONS, ctx_of(), use_voice=False)

        run(flow())
        texts = [t for m in client.messages for t in text_segments(m)]
        return texts == ["你", "好", "。"]

    def batch_path_bare_at():
        sender, client = build_sender("batch_bare_at")

        async def flow():
            return await sender.send_reply(
                "group", GROUP_ID, [sentence("@@主人真坏")],
                EMOTIONS, ctx_of(), use_voice=False,
                allowed_at_ids=[USER_ID], speaker_id=USER_ID)

        run(flow())
        ats = all_at_segments(client.messages)
        joined = all_text(client.messages)
        return ats and ats[0]["data"].get("qq") == USER_ID and "@" not in joined

    def private_no_at():
        sender, client = build_sender("private_no_at")

        async def flow():
            return await sender.send_reply(
                "private", USER_ID, [sentence("@@主人真坏")],
                EMOTIONS, ctx_of(), use_voice=False, allowed_at_ids=[])

        run(flow())
        return not all_at_segments(client.messages)

    def batch_path_at_has_space():
        sender, client = build_sender("batch_at_space")

        async def flow():
            return await sender.send_reply(
                "group", GROUP_ID, [sentence("@@主人真坏")],
                EMOTIONS, ctx_of(), use_voice=False,
                allowed_at_ids=[USER_ID], speaker_id=USER_ID)

        run(flow())
        texts = [t for m in client.messages for t in text_segments(m)]
        return texts == [" 主人真坏"]

    def mid_placeholder_at_has_space():
        sender, client = build_sender("mid_at_space")

        async def flow():
            return await sender.send_reply(
                "group", GROUP_ID, [sentence(f"喂{LH.MENTION_PLACEHOLDER}主人真坏")],
                EMOTIONS, ctx_of(), use_voice=False,
                allowed_at_ids=[USER_ID], speaker_id=USER_ID)

        run(flow())
        texts = [t for m in client.messages for t in text_segments(m)]
        return texts == ["喂", " 主人真坏"]

    check("逐字发送时裸 @ 变成真 at 段", chars_path_bare_at)
    check("逐字发送保留句末标点", chars_path_keeps_punctuation)
    check("整条发送时裸 @ 变成真 at 段", batch_path_bare_at)
    check("没有可@对象时不发 at 段", private_no_at)
    check("@ 挂在句首时与正文之间留空格", batch_path_at_has_space)
    check("@ 在句中时与后面正文之间留空格", mid_placeholder_at_has_space)


# ---------------------------------------------------------------------------
# T4 生成失败的提醒
# ---------------------------------------------------------------------------
def t4_error_reply_text():
    section("T4 生成失败的中文提醒")

    raw = ('RuntimeError: HTTP 403 https://example.com/v1/chat/completions：'
           '{"error":{"message":"Free quota exhausted","code":"AllocationQuota"}}')

    def is_chinese():
        text = LH.error_reply_text(RuntimeError(raw))
        return all(ch not in text for ch in "{}\"") and "HTTP" not in text \
            and "https" not in text and "quota" not in text

    def has_hint():
        return "模型额度不足" in LH.error_reply_text(RuntimeError(raw))

    def timeout_hint():
        return "超时" in LH.error_reply_text(RuntimeError("Read timed out"))

    def unknown_error_still_chinese():
        text = LH.error_reply_text(ValueError("boom"))
        return text == LH.ERROR_REPLY_TEXT and "boom" not in text

    def empty_error():
        return LH.error_reply_text(None) == LH.ERROR_REPLY_TEXT

    check("报错文案不含上游原文与 JSON", is_chinese)
    check("额度不足给中文原因", has_hint)
    check("超时给中文原因", timeout_hint)
    check("未知异常只给通用提醒", unknown_error_still_chinese)
    check("没有异常对象也给通用提醒", empty_error)


# ---------------------------------------------------------------------------
# T5 报错消息不合成语音
# ---------------------------------------------------------------------------
def t5_error_no_tts():
    section("T5 报错消息不合成语音")

    def normalized_as_plain():
        s = LH.normalize_single(
            {"zh": LH.error_reply_text(RuntimeError("HTTP 403 quota")),
             "delivery": LH.DELIVERY_PLAIN}, ctx_of(), EMOTIONS, "在吗")
        return s.get("delivery") == LH.DELIVERY_PLAIN and s["zh"].startswith("呜")

    def send_reply_skips_tts():
        sender, client = build_sender("error_no_tts")
        calls = []
        original = S.synthesize_sentence

        async def spy(*args, **kwargs):
            calls.append(args)
            return ""

        S.synthesize_sentence = spy
        try:
            s = LH.normalize_single(
                {"zh": LH.error_reply_text(RuntimeError("HTTP 403 quota")),
                 "delivery": LH.DELIVERY_PLAIN}, ctx_of(), EMOTIONS, "在吗")

            async def flow():
                return await sender.send_reply("group", GROUP_ID, [s],
                                               EMOTIONS, ctx_of(), use_voice=True)

            result = run(flow())
        finally:
            S.synthesize_sentence = original
        joined = all_text(client.messages)
        return not calls and result.get("tts_calls") == 0 and "呜" in joined

    check("报错句子被标成只发文字", normalized_as_plain)
    check("报错句子不调用语音合成", send_reply_skips_tts)


# ---------------------------------------------------------------------------
# T6 提示词
# ---------------------------------------------------------------------------
def t6_prompt():
    section("T6 提示词")

    def prompt_forbids_text_at():
        prompt = LH.build_system_prompt(ctx_of(), EMOTIONS)
        return "绝对不要用单个 @ 写" in prompt and "两个 @ 连着写" in prompt

    def prompt_mentions_all_members():
        prompt = LH.build_system_prompt(ctx_of(), EMOTIONS)
        return "「@@所有人」" in prompt

    check("提示词讲清单个 @ 与两个 @ 的区别", prompt_forbids_text_at)
    check("提示词给了@全体成员的写法", prompt_mentions_all_members)


# ---------------------------------------------------------------------------
# T7 流式路径
# ---------------------------------------------------------------------------
class TextOnlyCapClient:
    """流式桩用：语音能力位关闭，避免真的去起 TTS 服务。"""

    capabilities = {"voice": False, "sticker": False, "poke": False, "recall": False}


def t7_stream_sink():
    section("T7 流式路径的 @")

    def stream_converts_literal_at():
        _sender, _client = build_sender("stream_mention")
        seen = {}

        class StubSinkSender:
            last_message_id = "m1"

            def _active_client(self):
                return TextOnlyCapClient()

            def new_message_group(self):
                return 1

            async def send_text(self, session_type, target_id, text, **kwargs):
                seen["text"] = text
                seen["at_ids"] = kwargs.get("at_ids")
                return True

            async def send_voice(self, *args, **kwargs):
                return True

            async def send_poke(self, *args, **kwargs):
                return True

        stub = StubSinkSender()
        saved_cfg, M.global_config = M.global_config, _sender.config
        M.app_context.global_config = M.global_config
        sink = M.SentenceSink("group", GROUP_ID, {}, M.RoleContext(_sender.config.config),
                              allowed_at_ids=[USER_ID], at_names={}, speaker_id=USER_ID)
        saved, M.sender = M.sender, stub
        M.app_context.sender = M.sender
        try:
            run(sink._send_one(sentence("@@主人真坏")))
        finally:
            M.sender = saved
            M.app_context.sender = M.sender
            M.global_config = saved_cfg
            M.app_context.global_config = M.global_config
        return (seen.get("at_ids") == [USER_ID]
                and LH.MENTION_PLACEHOLDER in seen.get("text", ""))

    def stream_keeps_plain_text():
        _sender, _client = build_sender("stream_plain")
        seen = {}

        class StubSinkSender:
            last_message_id = "m2"

            def _active_client(self):
                return TextOnlyCapClient()

            async def send_text(self, session_type, target_id, text, **kwargs):
                seen["text"] = text
                seen["at_ids"] = kwargs.get("at_ids")
                return True

            async def send_voice(self, *args, **kwargs):
                return True

            async def send_poke(self, *args, **kwargs):
                return True

        stub = StubSinkSender()
        saved_cfg, M.global_config = M.global_config, _sender.config
        M.app_context.global_config = M.global_config
        sink = M.SentenceSink("group", GROUP_ID, {}, M.RoleContext(_sender.config.config),
                              allowed_at_ids=[USER_ID], speaker_id=USER_ID)
        saved, M.sender = M.sender, stub
        M.app_context.sender = M.sender
        try:
            run(sink._send_one(sentence("主人真坏")))
        finally:
            M.sender = saved
            M.app_context.sender = M.sender
            M.global_config = saved_cfg
            M.app_context.global_config = M.global_config
        return seen.get("text") == "主人真坏" and seen.get("at_ids") == []

    check("流式首句把裸 @ 换成真 at", stream_converts_literal_at)
    check("流式路径没有 @ 时不发 at 段", stream_keeps_plain_text)


def main_entry():
    print("=" * 70)
    print("台词里的 @ / 逐字发送 / 生成失败提醒")
    print("=" * 70)
    t1_apply_literal_mention()
    t2_char_pieces()
    t3_real_at_segment()
    t4_error_reply_text()
    t5_error_no_tts()
    t6_prompt()
    t7_stream_sink()
    print("\n" + "=" * 70)
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
    for name, err in FAIL:
        print(f"  [FAIL] {name}: {err}")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main_entry())
