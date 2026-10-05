# -*- coding: utf-8 -*-
"""撤回消息与「戳一戳写成文字」的测试。

覆盖三件事：
1. 模型把戳一戳写成台词（「（戳了戳你）」）时，摘掉这截文字并改走真的戳一戳，
   两条发送路径都要落到真动作上；
2. 句子级 recall 字段：解析、挂到第一句、延迟撤回已发出的消息，
   延迟夹在上下限之间，只能发文字的接入方式不撤；
3. 提示词：撤回规则随 recall_enabled 开关，戳一戳规则明确禁止写成文字。

运行: python tests/test_recall_poke_text.py     （全通过退出码 0）
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
from modules.adapters import ILinkClient, QQBotClient, client_supports
from modules.companion_tasks import TASK_PROGRESS_PROMPT, refuses_task
from modules.sender import (MessageSender as Sender, RECALL_MAX_DELAY_SECONDS,
                            RECALL_MIN_DELAY_SECONDS, DEFAULT_RECALL_DELAY_SECONDS,
                            can_recall_others, recall_delay_of)

PASS, FAIL = [], []
TMP_ROOT = ROOT / ".tmp_test" / "recall_poke_text"
BOT_ID = "100"
USER_ID = "200"
GROUP_ID = "999"


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
    return asyncio.new_event_loop().run_until_complete(coro)


def ctx_of(**overrides):
    cfg = {"text_lang": "ja", "display_lang": "zh", "default_voice": "pingjing",
           "llm_judge": True, "poke_enabled": True}
    cfg.update(overrides)
    return LH.RoleContext(cfg, {})


EMOTIONS = {"pingjing": {}, "gaoxing": {}}


# ---------------------------------------------------------------------------
# 桩：能发消息、能撤回的 NapCat 客户端
# ---------------------------------------------------------------------------
class StubClient:
    platform = "napcat"

    def __init__(self):
        self.self_id = BOT_ID
        self.sent = []
        self.deleted = []
        self._n = 0

    def _next_id(self):
        self._n += 1
        return f"m{self._n}"

    async def send_private_msg(self, user_id, message):
        mid = self._next_id()
        self.sent.append(mid)
        return {"message_id": mid}

    async def send_group_msg(self, group_id, message):
        mid = self._next_id()
        self.sent.append(mid)
        return {"message_id": mid}

    async def delete_msg(self, message_id):
        self.deleted.append(str(message_id))
        return {}


class StubTextOnlyClient:
    """流式路径的桩用：语音能力位关闭，避免真的去起 TTS 服务。"""

    capabilities = {"voice": False, "sticker": False, "poke": False, "recall": False}


def build_sender(name: str, client=None, **overrides):
    work = fresh_dir(name)
    config = M.ConfigLoader(str(work / "config.json"))
    config.config.update({"memory_data_path": str(work), "ref_audio_root": "",
                          "active_character": "murasame",
                          "roles": [{"character_key": "murasame", "character_name": "丛雨",
                                     "personality_prompt": "你是丛雨"}],
                          "send_retry": 0, "sticker_output_max_side": 0})
    config.config.update(overrides)
    config.roles = config._parse_roles()
    memory = M.MemoryManager(config)
    sender = Sender(config, memory)
    sender.client = client if client is not None else StubClient()
    return config, sender


def sentence(zh, **extra):
    data = {"zh": zh, "lang": zh, "display": zh, "emotion": "pingjing"}
    data.update(extra)
    return data


# ---------------------------------------------------------------------------
# T1 戳一戳被写成台词
# ---------------------------------------------------------------------------
def t1_poke_written_as_text():
    section("T1 「戳了戳你」写成文字")

    def bracket_only():
        s = LH.normalize_single({"zh": "（戳了戳你）"}, ctx_of(), EMOTIONS, "在吗")
        return s.get("poke") is True and not s["zh"].strip() and not s["lang"].strip()

    def bracket_inside_text():
        s = LH.normalize_single({"zh": "（戳了戳你）主人快理我。"}, ctx_of(), EMOTIONS, "在吗")
        return s.get("poke") is True and s["zh"] == "主人快理我。"

    def bare_action():
        s = LH.normalize_single({"zh": "戳了你一下"}, ctx_of(), EMOTIONS, "在吗")
        return s.get("poke") is True and not s["zh"].strip()

    def sentence_about_others_kept():
        s = LH.normalize_single({"zh": "他戳了戳你，你别理他。"}, ctx_of(), EMOTIONS, "在吗")
        return "poke" not in s and s["zh"] == "他戳了戳你，你别理他。"

    def switch_off_keeps_text():
        s = LH.normalize_single({"zh": "（戳了戳你）"}, ctx_of(poke_enabled=False),
                                EMOTIONS, "在吗")
        return "poke" not in s and s["zh"] == "（戳了戳你）"

    def action_attaches_to_next():
        raw = json.dumps({"sentences": [
            {"zh": "（戳了戳你）", "ja": "つんつん。", "emotion": "pingjing"},
            {"zh": "快回我消息呀。", "ja": "早く返事してよ。", "emotion": "pingjing"}]},
            ensure_ascii=False)
        out = LH.normalize_sentences(raw, ctx_of(), EMOTIONS, "在吗")
        return (len(out) == 1 and out[0].get("poke") is True
                and out[0]["zh"] == "快回我消息呀。")

    def action_only_reply_kept():
        raw = json.dumps({"sentences": [
            {"zh": "（戳了戳你）", "ja": "つんつん。", "emotion": "pingjing"}]},
            ensure_ascii=False)
        out = LH.normalize_sentences(raw, ctx_of(), EMOTIONS, "在吗")
        return len(out) == 1 and out[0].get("poke") is True

    check("整句只有括号动作时摘空并置 poke", bracket_only)
    check("动作混在正文里时只摘掉动作那一截", bracket_inside_text)
    check("整句是裸动作描述时也算戳一戳", bare_action)
    check("讲别人戳你的正文不会被误判", sentence_about_others_kept)
    check("poke_enabled 关闭时不动台词", switch_off_keeps_text)
    check("动作句被摘空后 poke 挂到下一句", action_attaches_to_next)
    check("整条回复只有动作时仍保留动作句", action_only_reply_kept)


# ---------------------------------------------------------------------------
# T2 两条发送路径都发真戳一戳
# ---------------------------------------------------------------------------
def t2_poke_reaches_send():
    section("T2 文字戳一戳落到真动作")

    def send_reply_path():
        config, sender = build_sender("poke_text_send_reply")
        poked = []

        async def fake_poke(session_type, target_id, user_id):
            poked.append((session_type, str(target_id), str(user_id)))
            return True

        sender.send_poke = fake_poke
        sentences = LH.normalize_sentences(
            json.dumps({"sentences": [
                {"zh": "（戳了戳你）", "ja": "つんつん。", "emotion": "pingjing"},
                {"zh": "快回我消息呀。", "ja": "早く返事してよ。", "emotion": "pingjing"}]},
                ensure_ascii=False),
            ctx_of(), EMOTIONS, "在吗")
        run(sender.send_reply("private", USER_ID, sentences, {}, M.RoleContext(config.config),
                              use_voice=False, poke_target=USER_ID))
        return poked == [("private", USER_ID, USER_ID)]

    def streaming_path():
        config, _sender = build_sender("poke_text_stream")
        seen = {}

        class StubSinkSender:
            last_message_id = "m1"

            def _active_client(self):
                return StubTextOnlyClient()

            async def send_text(self, session_type, target_id, text, **kwargs):
                seen.setdefault("texts", []).append(text)
                return True

            async def send_voice(self, session_type, target_id, wav):
                return True

            async def send_poke(self, session_type, target_id, user_id):
                seen["poked"] = (session_type, str(target_id), str(user_id))
                return True

            def schedule_recall(self, *args, **kwargs):
                return None

        stub = StubSinkSender()
        sink = M.SentenceSink("private", USER_ID, {}, M.RoleContext(config.config),
                              poke_target=USER_ID)
        saved, M.sender = M.sender, stub
        M.app_context.sender = M.sender
        try:
            piece = LH.normalize_single({"zh": "（戳了戳你）"}, ctx_of(), EMOTIONS, "在吗")
            run(sink.on_sentence(piece))
            run(sink.flush())
        finally:
            M.sender = saved
            M.app_context.sender = M.sender
        return seen.get("poked") == ("private", USER_ID, USER_ID)


    check("批量路径把文字戳一戳发成真戳", send_reply_path)
    check("流式路径把文字戳一戳发成真戳", streaming_path)


# ---------------------------------------------------------------------------
# T3 recall 字段解析
# ---------------------------------------------------------------------------
def t3_recall_field():
    section("T3 撤回字段解析")

    def normalize_keeps_recall():
        s = LH.normalize_single({"zh": "说错话了。", "recall": True}, ctx_of(), EMOTIONS, "在吗")
        return s.get("recall") is True

    def normalize_ignores_other_values():
        s = LH.normalize_single({"zh": "说错话了。", "recall": "yes"}, ctx_of(), EMOTIONS, "在吗")
        return "recall" not in s

    def attaches_to_first():
        raw = json.dumps({"sentences": [
            {"zh": "说错话了。", "ja": "言い間違えた。", "emotion": "pingjing", "recall": True},
            {"zh": "当我没说。", "ja": "今のはなし。", "emotion": "pingjing"}]},
            ensure_ascii=False)
        out = LH.normalize_sentences(raw, ctx_of(), EMOTIONS, "在吗")
        return (len(out) == 2 and out[0].get("recall") is True
                and "recall" not in out[1])

    def delay_clamped():
        return (recall_delay_of({"recall_delay": 0}) == RECALL_MIN_DELAY_SECONDS
                and recall_delay_of({"recall_delay": 999}) == RECALL_MAX_DELAY_SECONDS
                and recall_delay_of({"recall_delay": 5}) == 5.0
                and recall_delay_of({}) == DEFAULT_RECALL_DELAY_SECONDS)

    check("normalize_single 保留 recall", normalize_keeps_recall)
    check("recall 只认 true", normalize_ignores_other_values)
    check("recall 挂到第一句", attaches_to_first)
    check("撤回延迟夹在上下限之间", delay_clamped)


# ---------------------------------------------------------------------------
# T4 撤回真的发出去
# ---------------------------------------------------------------------------
def t4_recall_send():
    section("T4 撤回消息")

    def batch_recalls_sent_messages():
        config, sender = build_sender("recall_batch")
        client = sender.client
        sentences = [sentence("说错话了。", recall=True, recall_delay=1),
                     sentence("当我没说。")]

        async def flow():
            await sender.send_reply("private", USER_ID, sentences, {},
                                    M.RoleContext(config.config), use_voice=False)
            await asyncio.gather(*list(sender._recall_tasks))

        run(flow())
        return client.sent and client.deleted == list(client.sent)

    def text_only_channel_does_not_recall():
        config, sender = build_sender("recall_wechat")
        sender.client = ILinkClient({"bot_token": "t"})
        ok = run(sender.delete_message("private", USER_ID, "m1"))
        return ok is False and not client_supports(sender.client, "recall")

    def switch_off_skips():
        config, sender = build_sender("recall_off", recall_enabled=False)
        client = sender.client
        run(sender.send_reply("private", USER_ID, [sentence("说错话了。", recall=True)],
                              {}, M.RoleContext(config.config), use_voice=False))
        return not sender._recall_tasks and not client.deleted

    def delivery_branch_recalls():
        config, sender = build_sender("recall_delivery")
        client = sender.client

        async def flow():
            await sender.send_reply(
                "private", USER_ID,
                [sentence("abcd.", delivery="chars", recall=True, recall_delay=1)],
                {}, M.RoleContext(config.config), use_voice=False)
            await asyncio.gather(*list(sender._recall_tasks))

        run(flow())
        # 逐字发送连句末标点也照发（标点不再被当"纯标点"丢掉）
        return client.deleted == list(client.sent) and len(client.sent) == 5

    check("批量路径按延迟撤回本轮所有消息", batch_recalls_sent_messages)
    check("只能发文字的通道不撤回", text_only_channel_does_not_recall)
    check("recall_enabled 关闭后不撤回", switch_off_skips)
    check("纯文字分支同样撤回", delivery_branch_recalls)


# ---------------------------------------------------------------------------
# T5 流式路径撤回
# ---------------------------------------------------------------------------
def t5_recall_stream():
    section("T5 流式路径撤回")

    def flush_schedules_recall():
        config, _sender = build_sender("recall_stream")
        calls = {}

        class StubSinkSender:
            last_message_id = "m7"

            def _active_client(self):
                return StubTextOnlyClient()

            async def send_text(self, session_type, target_id, text, **kwargs):
                return True

            async def send_voice(self, session_type, target_id, wav):
                return True

            async def send_poke(self, session_type, target_id, user_id):
                return True

            def schedule_recall(self, session_type, target_id, message_ids, delay):
                calls["args"] = (session_type, str(target_id), list(message_ids), delay)

        stub = StubSinkSender()
        sink = M.SentenceSink("private", USER_ID, {}, M.RoleContext(config.config))
        saved, M.sender = M.sender, stub
        M.app_context.sender = M.sender
        try:
            run(sink.on_sentence(sentence("说错话了。", recall=True, recall_delay=4)))
            run(sink.flush())
        finally:
            M.sender = saved
            M.app_context.sender = M.sender
        return calls.get("args") == ("private", USER_ID, ["m7"], 4.0)

    def abort_drops_recall():
        config, _sender = build_sender("recall_stream_abort")
        stub = type("S", (), {"last_message_id": "m8", "_active_client": lambda self: None,
                              "send_text": None, "send_voice": None,
                              "send_poke": None,
                              "schedule_recall": lambda self, *a, **k: None})()
        sink = M.SentenceSink("private", USER_ID, {}, M.RoleContext(config.config))
        sink.recall_pending = True
        run(sink.abort())
        return sink.recall_pending is False

    check("流式收尾按第一句的延迟挂撤回", flush_schedules_recall)
    check("放弃本轮时不再撤回", abort_drops_recall)


# ---------------------------------------------------------------------------
# T6 主人明确要求撤回
# ---------------------------------------------------------------------------
def t6_recall_request():
    section("T6 主人要求撤回")

    kind = LH.recall_request_kind

    def detects_next():
        return (kind("撤回你下一条消息") == "next"
                and kind("我在测试呢 撤回你下一条消息") == "next"
                and kind("撤回下一句") == "next")

    def detects_prev():
        return (kind("撤回刚才那条") == "prev"
                and kind("把那条撤回") == "prev"
                and kind("撤回这条") == "prev")

    def detects_late_push():
        # 「还没撤回」是在催这一步没做，不是「别撤回」
        return (kind("你还没撤回呀") == "prev"
                and kind("你怎么还不撤回") == "prev"
                and kind("还没有撤回") == "prev")

    def ignores_questions_and_denials():
        return (kind("你会撤回消息吗") == ""
                and kind("能不能撤回") == ""
                and kind("不用撤回") == ""
                and kind("别撤回这条") == ""
                and kind("我刚刚撤回了一条消息") == "")

    def ignores_plain_chat():
        return kind("今天天气不错") == "" and kind("") == ""

    def forced_next_recalls_reply():
        config, sender = build_sender("recall_forced_next")
        client = sender.client

        async def flow():
            await sender.send_reply("private", USER_ID, [sentence("嗯。"), sentence("知道了。")],
                                    {}, M.RoleContext(config.config), use_voice=False,
                                    recall_request="next")
            await asyncio.gather(*list(sender._recall_tasks))

        run(flow())
        return client.deleted == list(client.sent) and len(client.sent) == 1

    def recall_recent_removes_last():
        config, sender = build_sender("recall_prev")
        client = sender.client
        run(sender.send_text("private", USER_ID, "第一条"))
        run(sender.send_text("private", USER_ID, "第二条"))
        done = run(sender.recall_recent("private", USER_ID))
        return done == 1 and client.deleted == [client.sent[-1]]

    def recall_recent_empty_is_noop():
        _, sender = build_sender("recall_prev_empty")
        return run(sender.recall_recent("private", USER_ID)) == 0

    def recall_message_removes_whole_group():
        # 一句话的文字与语音是同一条消息的两半：撤其中一条要连另一条一起撤
        _, sender = build_sender("recall_group")
        client = sender.client

        async def flow():
            group = sender.new_message_group()
            await sender.send_text("private", USER_ID, "文字", group=group)
            await sender.send_voice("private", USER_ID, "x.wav", group=group)
            await sender.send_text("private", USER_ID, "别的话")
            return await sender.recall_message("private", USER_ID, client.sent[0])

        done = run(flow())
        return done == 2 and client.deleted == client.sent[:2]

    def recall_message_without_record_still_deletes():
        # 那条消息太旧、记录里已经找不到：至少把它自己撤掉
        _, sender = build_sender("recall_group_old")
        client = sender.client
        done = run(sender.recall_message("private", USER_ID, "m999"))
        return done == 1 and client.deleted == ["m999"]

    check("认出「撤回下一条」", detects_next)
    check("认出「撤回刚才那条」", detects_prev)
    check("认出「还没撤回」这类催促", detects_late_push)
    check("提问、否定与主人自己撤回不算", ignores_questions_and_denials)
    check("普通聊天不触发", ignores_plain_chat)
    check("强制撤回这条回复（不看模型填没填）", forced_next_recalls_reply)
    check("撤回最近发出去的那条", recall_recent_removes_last)
    check("没有可撤的消息时不做动作", recall_recent_empty_is_noop)
    check("撤回指定消息时连同同一句话的语音", recall_message_removes_whole_group)
    check("指定消息已无记录时仍撤掉它自己", recall_message_without_record_still_deletes)


# ---------------------------------------------------------------------------
# T7 提示词
# ---------------------------------------------------------------------------
def t7_prompts():
    section("T7 提示词")

    def recall_rule_gated():
        cfg = M.ConfigLoader.default_config()
        cfg["recall_enabled"] = True
        on = LH.build_system_prompt(LH.RoleContext(cfg, {}), {})
        cfg["recall_enabled"] = False
        off = LH.build_system_prompt(LH.RoleContext(cfg, {}), {})
        return "【撤回】" in on and "【撤回】" not in off

    def recall_rule_has_example():
        cfg = M.ConfigLoader.default_config()
        text = LH.build_system_prompt(LH.RoleContext(cfg, {}), {})
        return "\"recall\": true" in text and "主人明确让你撤回时" in text

    def poke_rule_forbids_text():
        cfg = M.ConfigLoader.default_config()
        cfg["poke_enabled"] = True
        text = LH.build_system_prompt(LH.RoleContext(cfg, {}), {})
        return "绝对不要把它写进台词" in text

    def chat_style_has_no_assistant_rules():
        roles = {r["character_key"]: r for r in M.ConfigLoader.default_config()["roles"]}
        prompt = roles["murasame"]["personality_prompt"]
        return "不要当助手" in prompt and "情绪有起伏" in prompt

    def migration_upgrades_old_style():
        cfg = {"roles": [{"character_key": "murasame",
                          "personality_prompt": "你是丛雨。\n【说话方式】像真人一样聊天：1. 旧版。"}]}
        M._migrate_chat_style_prompt(cfg)
        prompt = cfg["roles"][0]["personality_prompt"]
        return "不要当助手" in prompt and "1. 旧版" not in prompt

    check("撤回规则随 recall_enabled 开关", recall_rule_gated)
    check("撤回规则给了具体写法示例", recall_rule_has_example)
    check("戳一戳规则明确禁止写成文字", poke_rule_forbids_text)
    check("默认人设含去助手味的要求", chat_style_has_no_assistant_rules)
    check("老配置的说话方式会升到最新版", migration_upgrades_old_style)


# ---------------------------------------------------------------------------
# T8 审判日志
# ---------------------------------------------------------------------------
def t8_judge_log():
    section("T8 审判日志")

    def mood_text_shows_base_and_bias():
        text = M._judge_mood_text({"mood": 55.0, "mood_base": 60.0})
        return "心情值 60" in text and "判定用值 55" in text and "偏置 -5" in text

    def mood_text_without_bias():
        return M._judge_mood_text({"mood": 62.0, "mood_base": 62.0}) == "心情值 62"

    def gate_cause_shows_roll():
        cause = M._judge_gate_cause({"llm_reply": True, "probability": 0.917, "roll": 0.983})
        return "0.983" in cause and "0.917" in cause

    def gate_cause_llm_refusal():
        cause = M._judge_gate_cause({"llm_reply": False, "probability": 1.0, "roll": None})
        return cause == "LLM判定无需回复"

    def gate_cause_without_roll():
        cause = M._judge_gate_cause({"llm_reply": True, "probability": 0.5, "roll": None})
        return "0.500" in cause

    check("审判日志写存档心情与偏置", mood_text_shows_base_and_bias)
    check("没有偏置时只写心情值", mood_text_without_bias)
    check("门控未通过时写出掷出的值", gate_cause_shows_roll)
    check("LLM 自己判定不用回时原因不同", gate_cause_llm_refusal)
    check("拿不到掷出值时退回只写概率", gate_cause_without_roll)


# ---------------------------------------------------------------------------
# T9 动作字段被当成台词写出来
# ---------------------------------------------------------------------------
def t9_action_field_leak():
    section("T9 动作字段被写成正文")

    def strips_field_line():
        return (LH.strip_action_field_lines("reply_to: true\n主人终于肯现身啦～")
                == ("主人终于肯现身啦～", {"reply_to": "true"}))

    def strips_multiple_and_keeps_rest():
        text, fields = LH.strip_action_field_lines("poke: true\nrecall: 1\n哼")
        return text == "哼" and fields == {"poke": "true", "recall": "1"}

    def plain_text_untouched():
        text, fields = LH.strip_action_field_lines("主人终于肯现身啦～")
        return text == "主人终于肯现身啦～" and fields == {}

    def normalize_removes_from_all_fields():
        s = LH.normalize_single({"zh": "reply_to: true\n主人终于肯现身啦～"},
                                ctx_of(), EMOTIONS, "在吗")
        return (s["zh"] == "主人终于肯现身啦～"
                and s["display"] == "主人终于肯现身啦～"
                and s["lang"] == "主人终于肯现身啦～"
                and s.get("reply_to") is True)

    def normalize_keeps_action_effect():
        s = LH.normalize_single({"zh": "recall: 1\n说错话了。"}, ctx_of(), EMOTIONS, "在吗")
        return s["display"] == "说错话了。" and s.get("recall") is True

    def field_only_sentence_falls_back():
        s = LH.normalize_single({"zh": "reply_to: true"}, ctx_of(), EMOTIONS, "在吗")
        return s.get("reply_to") is True and "reply_to" not in s["display"]

    check("剥掉开头被写成正文的字段行", strips_field_line)
    check("多行字段一起剥掉、保留其余台词", strips_multiple_and_keeps_rest)
    check("普通台词不受影响", plain_text_untouched)
    check("三个文本字段都不带字段行、动作照样生效", normalize_removes_from_all_fields)
    check("剥掉字段行后 recall 仍然生效", normalize_keeps_action_effect)
    check("整句只有字段行时不把字段名发出去", field_only_sentence_falls_back)


# ---------------------------------------------------------------------------
# T10 @ 只跟着写了占位符的那句
# ---------------------------------------------------------------------------
def t10_mention_placement():
    section("T10 @ 的位置")

    def mention_attaches_to_placeholder_sentence():
        raw = json.dumps({"sentences": [
            {"zh": "主人要本座叫他吗？", "ja": "a", "emotion": "pingjing"},
            {"zh": f"{LH.MENTION_PLACEHOLDER}快出来！", "ja": "b", "emotion": "pingjing"}],
            "mention_ids": ["932440356"]}, ensure_ascii=False)
        out = LH.normalize_sentences(raw, ctx_of(), EMOTIONS, "在吗")
        return (len(out) == 2 and "mention_ids" not in out[0]
                and out[1].get("mention_ids") == ["932440356"])

    def at_only_on_placeholder_message():
        config, sender = build_sender("mention_place", separate_send=True,
                                      send_voice_separately=True)
        client = sender.client
        seen = []

        async def fake_send_group(group_id, message):
            seen.append([getattr(seg, "qq", None) for seg in message
                         if type(seg).__name__ == "At"])
            return {"message_id": "m1"}

        client.send_group_msg = fake_send_group
        sentences = LH.normalize_sentences(
            json.dumps({"sentences": [
                {"zh": "主人要本座叫他吗？", "ja": "a", "emotion": "pingjing"},
                {"zh": f"{LH.MENTION_PLACEHOLDER}快出来！", "ja": "b",
                 "emotion": "pingjing"}],
                "mention_ids": ["932440356"]}, ensure_ascii=False),
            ctx_of(), EMOTIONS, "在吗")
        run(sender.send_reply("group", GROUP_ID, sentences, {}, M.RoleContext(config.config),
                              use_voice=False, allowed_at_ids=["932440356"]))
        return seen == [[], ["932440356"]]

    check("@ 目标挂到带占位符的那句", mention_attaches_to_placeholder_sentence)
    check("只有带占位符的消息真的带 @", at_only_on_placeholder_message)


# ---------------------------------------------------------------------------
# T11 撤别人消息的权限
# ---------------------------------------------------------------------------
class RoleClient:
    """能查群成员角色的客户端桩：机器人角色与各成员角色都可配。"""

    platform = "napcat"

    def __init__(self, bot_role, member_roles):
        self.self_id = BOT_ID
        self.deleted = []
        self._bot_role = bot_role
        self._member_roles = member_roles

    async def get_group_member_info(self, group_id, user_id):
        uid = str(user_id)
        role = self._bot_role if uid == BOT_ID else self._member_roles.get(uid, "member")
        return {"status": "ok", "data": {"role": role}}

    async def delete_msg(self, message_id):
        self.deleted.append(str(message_id))
        return {}


def t11_recall_others_permission():
    section("T11 撤别人消息的权限")

    def both_admin_allowed():
        client = RoleClient("admin", {USER_ID: "admin"})
        return run(can_recall_others(client, GROUP_ID, USER_ID, "admin")) is True

    def role_fetched_when_event_lacks_it():
        client = RoleClient("owner", {USER_ID: "owner"})
        return run(can_recall_others(client, GROUP_ID, USER_ID)) is True

    def plain_member_denied():
        client = RoleClient("admin", {USER_ID: "member"})
        return run(can_recall_others(client, GROUP_ID, USER_ID)) is False

    def bot_not_admin_denied():
        client = RoleClient("member", {USER_ID: "owner"})
        return run(can_recall_others(client, GROUP_ID, USER_ID)) is False

    def text_only_channel_denied():
        return run(can_recall_others(ILinkClient({"bot_token": "t"}),
                                     GROUP_ID, USER_ID, "admin")) is False

    check("请求者与机器人都是管理员时允许", both_admin_allowed)
    check("事件没带角色时回查群成员信息", role_fetched_when_event_lacks_it)
    check("一般群成员不能借她撤别人的消息", plain_member_denied)
    check("机器人自己不是管理员时撤不了", bot_not_admin_denied)
    check("没有撤回接口的接入方式一律拒绝", text_only_channel_denied)


# ---------------------------------------------------------------------------
# T12 角色说不做时不再催
# ---------------------------------------------------------------------------
def t12_task_refusal():
    section("T12 角色说不做时不再催")

    def plain_refusal():
        return refuses_task("本座偏要让它留着～") is True

    def cannot_do():
        return (refuses_task("这条消息不是本座发的，撤不掉呢……") is True
                and refuses_task("系统说这条消息不是本座发的，没有权限") is True)

    def agreeing_reply_is_not_refusal():
        return refuses_task("好嘞，本座这就帮你撤掉它～") is False

    def prompt_has_refusal_rule():
        return "不要逼她重做" in TASK_PROGRESS_PROMPT

    check("「偏要」这类明说不算拒绝继续催", plain_refusal)
    check("「撤不掉 / 没权限」也不再催", cannot_do)
    check("答应照做的回复不会被误判成拒绝", agreeing_reply_is_not_refusal)
    check("判定提示词写明拒绝算做了", prompt_has_refusal_rule)


# ---------------------------------------------------------------------------
# T13 戳一戳不受任何节流
# ---------------------------------------------------------------------------
def t13_poke_no_throttle():
    section("T13 戳一戳不受节流")

    def repeated_pokes_all_go_out():
        _, sender = build_sender("poke_no_throttle")
        calls = []

        async def fake_group_poke(group_id, user_id):
            calls.append(str(user_id))
            return {}

        sender.client.group_poke = fake_group_poke
        receipts = [run(sender.poke_receipt("group", GROUP_ID, USER_ID))
                    for _ in range(40)]
        return all(r.get("ok") is True for r in receipts) and len(calls) == 40

    def unsupported_channel_still_receipts():
        _, sender = build_sender("poke_unsupported")
        sender.client = ILinkClient({"bot_token": "t"})
        receipt = run(sender.poke_receipt("group", GROUP_ID, USER_ID))
        return receipt.get("ok") is False and receipt.get("reason") == "unsupported"

    check("连着戳 40 次都真的发出去", repeated_pokes_all_go_out)
    check("通道不支持时仍然给失败回执", unsupported_channel_still_receipts)


def main():
    print("=" * 70)
    print("撤回消息与「戳一戳写成文字」测试")
    print("=" * 70)
    if TMP_ROOT.exists():
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    saved_global, M.global_config = M.global_config, M.ConfigLoader.default_config()
    M.app_context.global_config = M.global_config
    try:
        for fn in (t1_poke_written_as_text, t2_poke_reaches_send, t3_recall_field,
                   t4_recall_send, t5_recall_stream, t6_recall_request,
                   t7_prompts, t8_judge_log, t9_action_field_leak,
                   t10_mention_placement, t11_recall_others_permission,
                   t12_task_refusal, t13_poke_no_throttle):
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
