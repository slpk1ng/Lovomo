# -*- coding: utf-8 -*-
"""戳一戳与合并转发消息测试。

覆盖三件事：
1. 收到「有人戳了机器人」的通知时折成一条普通消息走同一条回复管线，
   机器人自己戳别人、或别人被戳时不能响应（否则会自己戳自己、来回没完）；
2. 角色主动戳人（句子级 poke 字段）在两条发送路径上都执行，并受开关约束，
   微信 ClawBot / QQ 官方这类通道直接不发；
3. 合并转发过来的聊天记录能回查并渲染成文本，正文只有一个 id 时不会
   把整条消息丢掉。

运行: python tests/test_poke_forward.py     （全通过退出码 0）
"""
import asyncio
import io
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
sys.stdin = io.StringIO()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from napcat import At, Forward, GroupMessageEvent, MessageSender

import main as M
import modules.llm_helpers as LH
from modules.adapters import ILinkClient, QQBotClient, client_supports
from modules.sender import (MessageSender as Sender)

PASS, FAIL = [], []
TMP_ROOT = ROOT / ".tmp_test" / "poke_forward"
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


# ---------------------------------------------------------------------------
# 桩：一个能戳一戳的 NapCat 客户端
# ---------------------------------------------------------------------------
class StubPokeClient:
    """NapCat 不声明 capabilities，client_supports 默认放行。"""

    platform = "napcat"

    def __init__(self, self_id=BOT_ID):
        self.self_id = self_id
        self.calls = []

    async def friend_poke(self, **kwargs):
        self.calls.append(("friend_poke", kwargs))

    async def group_poke(self, **kwargs):
        self.calls.append(("group_poke", kwargs))


class StubForwardClient:
    self_id = BOT_ID

    def __init__(self):
        self.asked = []

    async def get_forward_msg(self, message_id=""):
        self.asked.append(str(message_id))
        return {"messages": [
            {"sender": {"nickname": "甲"},
             "message": [{"type": "text", "data": {"text": "看到我回复111"}}]},
            {"sender": {"card": "乙"},
             "message": [{"type": "image",
                          "data": {"url": "https://example.com/a.png", "file": "f1"}},
                         {"type": "face", "data": {"id": "1"}}]},
        ]}


class StubSender:
    """流式发送路径的桩：只发文字（voice 能力位关闭 → 不碰 TTS）。"""

    def __init__(self):
        self.poked = []
        self.texts = []

    def _active_client(self):
        return StubTextOnlyClient()

    async def send_text(self, session_type, target_id, text, **kwargs):
        self.texts.append(str(text))
        return True

    async def send_voice(self, session_type, target_id, wav_path):
        return True

    async def send_poke(self, session_type, target_id, user_id):
        self.poked.append((session_type, str(target_id), str(user_id)))
        return True


class StubTextOnlyClient:
    capabilities = {"voice": False, "sticker": False, "poke": False}


def make_poke(private: bool, user_id=USER_ID, target_id=BOT_ID):
    from napcat import FriendPokeEvent, GroupPokeEvent
    common = {"time": int(time.time()), "self_id": int(BOT_ID),
              "user_id": int(user_id), "target_id": int(target_id)}
    if private:
        return FriendPokeEvent(**common, sender_id=int(user_id), raw_info={})
    return GroupPokeEvent(**common, group_id=int(GROUP_ID), raw_info={})


def make_group_message(segments):
    return GroupMessageEvent(time=int(time.time()), self_id=int(BOT_ID),
                             post_type="message", message_id=1, user_id=int(USER_ID),
                             message_seq=1, real_id=1,
                             sender=MessageSender(user_id=int(USER_ID), nickname="甲"),
                             raw_message="", message=tuple(segments),
                             group_id=int(GROUP_ID))


# ---------------------------------------------------------------------------
# T1 戳一戳接收
# ---------------------------------------------------------------------------
def t1_poke_to_message():
    section("T1 戳一戳接收")

    def private_poke():
        event = run(M._poke_to_message_event(make_poke(True), StubPokeClient()))
        info = M._extract_event_info(event, StubPokeClient())
        return (isinstance(event, M.PrivateMessageEvent)
                and str(event.user_id) == USER_ID
                and info["session_id"] == f"private_{USER_ID}"
                and info["text"] == LH.POKE_MESSAGE_TEXT)

    def group_poke():
        event = run(M._poke_to_message_event(make_poke(False), StubPokeClient()))
        info = M._extract_event_info(event, StubPokeClient())
        return (isinstance(event, M.GroupMessageEvent)
                and info["session_id"] == f"group_{GROUP_ID}"
                and info["at_bot"] is True)

    def ignore_self_poke():
        # 机器人自己戳别人：user_id 是机器人，必须丢掉，否则会自己戳自己
        event = make_poke(False, user_id=BOT_ID, target_id=USER_ID)
        return run(M._poke_to_message_event(event, StubPokeClient())) is None

    def ignore_poke_other():
        # 别人被戳（不是机器人被戳）：与机器人无关，不回应
        event = make_poke(False, user_id="300", target_id="400")
        return run(M._poke_to_message_event(event, StubPokeClient())) is None

    def ignore_plain_message():
        return run(M._poke_to_message_event(
            make_group_message([At(qq=BOT_ID)]), StubPokeClient())) is None

    check("私聊被戳折成一条普通私聊消息", private_poke)
    check("群聊被戳折成一条已 @ 机器人的群消息", group_poke)
    check("机器人自己戳别人时忽略", ignore_self_poke)
    check("戳的不是机器人时忽略", ignore_poke_other)
    check("普通消息不被当成戳一戳", ignore_plain_message)


# ---------------------------------------------------------------------------
# T2 只有 NapCat 支持戳一戳
# ---------------------------------------------------------------------------
def t2_poke_capabilities():
    section("T2 戳一戳能力声明")
    check("微信 ClawBot 不支持戳一戳", lambda: not client_supports(ILinkClient({}), "poke"))
    check("QQ 官方机器人不支持戳一戳", lambda: not client_supports(QQBotClient({}), "poke"))
    check("NapCat 支持戳一戳", lambda: client_supports(StubPokeClient(), "poke"))


# ---------------------------------------------------------------------------
# T3 主动戳人
# ---------------------------------------------------------------------------
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
    memory = M.MemoryManager(config)
    sender = Sender(config, memory)
    client = StubPokeClient()
    sender.client = client
    return config, sender, client


def t3_send_poke():
    section("T3 主动戳人")

    def private_target():
        _, sender, client = build_sender("poke_private")
        ok = run(sender.send_poke("private", USER_ID, USER_ID))
        return ok and client.calls == [("friend_poke", {"user_id": int(USER_ID)})]

    def group_target():
        _, sender, client = build_sender("poke_group")
        ok = run(sender.send_poke("group", GROUP_ID, USER_ID))
        return ok and client.calls == [("group_poke", {"group_id": int(GROUP_ID),
                                                      "user_id": int(USER_ID)})]

    def no_cooldown_between_pokes():
        _, sender, _client = build_sender("poke_no_cooldown")
        first = run(sender.send_poke("private", USER_ID, USER_ID))
        second = run(sender.send_poke("private", USER_ID, USER_ID))
        return first and second

    def no_daily_cap():
        _, sender, _client = build_sender("poke_no_cap")
        return all(run(sender.send_poke("private", USER_ID, USER_ID)) for _ in range(40))

    def disabled_switch():
        _, sender, client = build_sender("poke_off", poke_enabled=False)
        return not run(sender.send_poke("private", USER_ID, USER_ID)) and not client.calls

    def text_only_channel():
        _, sender, _client = build_sender("poke_wechat")
        sender.client = ILinkClient({"bot_token": "t"})
        return not run(sender.send_poke("private", USER_ID, USER_ID))

    check("私聊戳对方走 friend_poke", private_target)
    check("群聊戳成员走 group_poke（带群号）", group_target)
    check("同一会话连续戳两次都不被挡", no_cooldown_between_pokes)
    check("戳一戳没有每日上限，连着戳都发得出去", no_daily_cap)
    check("poke_enabled 关闭后不戳", disabled_switch)
    check("微信 ClawBot 通道不戳", text_only_channel)


# ---------------------------------------------------------------------------
# T4 poke 字段在两条发送路径上都执行
# ---------------------------------------------------------------------------
def t4_poke_in_send_paths():
    section("T4 poke 字段落到发送")

    def send_reply_path():
        config, sender, client = build_sender("poke_send_reply")
        sentences = [{"zh": "戳你一下。", "lang": "戳你一下。", "display": "戳你一下。",
                      "emotion": "pingjing", "poke": True},
                     {"zh": "快回我。", "lang": "快回我。", "display": "快回我。",
                      "emotion": "pingjing"}]
        run(sender.send_reply("private", USER_ID, sentences, {}, M.RoleContext(config.config),
                              use_voice=False, poke_target=USER_ID))
        return client.calls == [("friend_poke", {"user_id": int(USER_ID)})]

    def streaming_path():
        config, _sender, client = build_sender("poke_stream")
        stub = StubSender()
        sink = M.SentenceSink("private", USER_ID, {}, M.RoleContext(config.config),
                              poke_target=USER_ID)
        saved, M.sender = M.sender, stub
        M.app_context.sender = M.sender
        try:
            run(sink.on_sentence({"zh": "戳你一下。", "lang": "戳你一下。",
                                  "display": "戳你一下。", "emotion": "pingjing",
                                  "poke": True}))
            run(sink.on_sentence({"zh": "快回我。", "lang": "快回我。",
                                  "display": "快回我。", "emotion": "pingjing"}))
            run(sink.flush())
        finally:
            M.sender = saved
            M.app_context.sender = M.sender
        return stub.poked == [("private", USER_ID, USER_ID)]

    check("send_reply 路径执行 poke", send_reply_path)
    check("流式路径执行 poke（且只戳一次）", streaming_path)


# ---------------------------------------------------------------------------
# T5 合并转发消息
# ---------------------------------------------------------------------------
def t5_forward():
    section("T5 合并转发消息")

    def render_segments():
        rendered = M._render_ob11_segments([
            {"type": "text", "data": {"text": "你好"}},
            {"type": "image", "data": {"url": "https://example.com/b.png", "file": "f2"}},
            {"type": "record", "data": {}},
            {"type": "face", "data": {}},
            {"type": "at", "data": {"qq": "300"}},
            {"type": "forward", "data": {}},
        ])
        return (rendered["text"] == "你好 [图片] [语音] [表情] [@300] [聊天记录]"
                and rendered["image_urls"] == ["https://example.com/b.png"]
                and rendered["image_file_ids"] == {"https://example.com/b.png": "f2"})

    def fetch_text():
        client = StubForwardClient()
        out = run(M.fetch_forward_text(client, ["fwd-1"]))
        return (client.asked == ["fwd-1"]
                and out["text"] == "甲: 看到我回复111\n乙: [图片] [表情]"
                and out["image_urls"] == ["https://example.com/a.png"])

    def fetch_missing():
        class Dead:
            self_id = BOT_ID

            async def get_forward_msg(self, message_id=""):
                raise RuntimeError("服务端拒绝")

        return run(M.fetch_forward_text(Dead(), ["x"])) is None

    def extract_forward_only():
        # 正文只有一个转发 id 的消息以前会被整条丢掉（user_text 为空即 return None）
        event = make_group_message([At(qq=BOT_ID), Forward(id="fwd-9")])
        info = M._extract_event_info(event, StubPokeClient())
        return info is not None and info["forward_ids"] == ["fwd-9"]

    def merge_carries_forward():
        event = make_group_message([At(qq=BOT_ID)])
        base = M._extract_event_info(event, StubPokeClient())
        nxt = dict(base, forward_ids=["fwd-9"], text="")
        merged = M._merge_event_info(base, nxt)
        return (merged["forward_ids"] == ["fwd-9"]
                and M._merged_payload(merged)["forward_ids"] == ["fwd-9"])

    check("消息段渲染（文本/图片/语音/表情/@/转发）", render_segments)
    check("回查转发内容并渲染成文本", fetch_text)
    check("回查失败时不抛异常、按无正文处理", fetch_missing)
    check("只有转发段的消息不会被丢掉", extract_forward_only)
    check("连发消息合并时转发 id 不丢", merge_carries_forward)


# ---------------------------------------------------------------------------
# T6 提示词
# ---------------------------------------------------------------------------
def t6_prompts():
    section("T6 提示词")

    def poke_rule_gated():
        cfg = M.ConfigLoader.default_config()
        cfg["poke_enabled"] = True
        on = LH.build_system_prompt(LH.RoleContext(cfg, {}), {})
        cfg["poke_enabled"] = False
        off = LH.build_system_prompt(LH.RoleContext(cfg, {}), {})
        return "【戳一戳】" in on and "【戳一戳】" not in off

    def default_role_has_chat_style():
        roles = {r["character_key"]: r for r in M.ConfigLoader.default_config()["roles"]}
        return "【说话方式】" in roles["murasame"]["personality_prompt"]

    def migration_appends_once():
        cfg = {"personality_prompt": "【角色设定】你是丛雨，一位少女。",
               "roles": [{"character_key": "murasame", "personality_prompt": "你是丛雨。"},
                         {"character_key": "other", "personality_prompt": "你是猫娘。"}]}
        M._migrate_chat_style_prompt(cfg)
        first = ("【说话方式】" in cfg["personality_prompt"]
                 and "【说话方式】" in cfg["roles"][0]["personality_prompt"]
                 and "【说话方式】" not in cfg["roles"][1]["personality_prompt"])
        before = json.dumps(cfg, ensure_ascii=False)
        M._migrate_chat_style_prompt(cfg)
        return first and json.dumps(cfg, ensure_ascii=False) == before

    def migration_keeps_custom_supplement():
        cfg = {"roles": [{"character_key": "murasame",
                          "personality_prompt": "你是丛雨。",
                          "supplement_prompt": "我自己写的补充要求"}]}
        M._migrate_chat_style_prompt(cfg)
        return cfg["roles"][0]["supplement_prompt"] == "我自己写的补充要求"

    check("戳一戳规则随 poke_enabled 开关", poke_rule_gated)
    check("默认丛雨人设含「说话方式」", default_role_has_chat_style)
    check("老配置补「说话方式」且只补一次", migration_appends_once)
    check("迁移不动用户自己写的补充要求", migration_keeps_custom_supplement)


# ---------------------------------------------------------------------------
# T7 句子级 poke 字段
# ---------------------------------------------------------------------------
def t7_sentence_field():
    section("T7 句子级 poke 字段")

    ctx = LH.RoleContext({"text_lang": "ja", "display_lang": "zh",
                          "default_voice": "pingjing", "llm_judge": True}, {})
    emotions = {"pingjing": {}, "gaoxing": {}}

    def normalize_keeps_poke():
        s = LH.normalize_single({"zh": "戳你一下。", "poke": True}, ctx, emotions, "在吗")
        return s.get("poke") is True

    def normalize_ignores_other_values():
        s = LH.normalize_single({"zh": "戳你一下。", "poke": "yes"}, ctx, emotions, "在吗")
        return "poke" not in s

    def sentences_attach_to_first():
        raw = json.dumps({"sentences": [
            {"zh": "戳你一下。", "ja": "つんつん。", "emotion": "gaoxing", "poke": True},
            {"zh": "快回我消息呀。", "ja": "早く返事してよ。", "emotion": "pingjing"}]},
            ensure_ascii=False)
        out = LH.normalize_sentences(raw, ctx, emotions, "在吗")
        return (len(out) == 2 and out[0].get("poke") is True
                and "poke" not in out[1])

    check("normalize_single 保留 poke", normalize_keeps_poke)
    check("poke 只认 true", normalize_ignores_other_values)
    check("parse_sentences 把 poke 挂到第一句", sentences_attach_to_first)


def main():
    print("=" * 70)
    print("戳一戳与合并转发消息测试")
    print("=" * 70)
    if TMP_ROOT.exists():
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    saved_global, M.global_config = M.global_config, M.ConfigLoader.default_config()
    M.app_context.global_config = M.global_config
    try:
        for fn in (t1_poke_to_message, t2_poke_capabilities, t3_send_poke,
                   t4_poke_in_send_paths, t5_forward, t6_prompts, t7_sentence_field):
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
