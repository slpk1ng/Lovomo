# -*- coding: utf-8 -*-
"""禁言与「撤回别人的消息」的测试。

角色新增两个句子级动作字段：
- mute（+ mute_duration）：禁言群里某个人，true = 这一轮针对的那个人
  （被@的 / 被引用的 / 当前发言者），也可直接填 QQ 号；
- recall_other：撤回**别人发的**消息，撤哪条由系统定（引用了谁就撤谁，没引用撤本轮消息）。

覆盖四件事：
1. 解析层保住字段（含模型把字段名写成正文时的兜底）；
2. 发送层真的落到 set_group_ban / delete_msg 上，并把真实回执带回来；
3. 权限与开关挡住时不发 API，回执写明原因（没有权限 / 这条接入方式不支持 / 已关闭）；
4. 底层提示词与「她没做时该怎么引导」的子代理提示词都写进了这两个动作。

运行: python tests/test_mute_recall_other.py     （全通过退出码 0）
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
from modules.adapters import ILinkClient
from modules.companion_tasks import TASK_PROGRESS_PROMPT
from modules.message_pipeline import _mute_candidates
from modules.sender import (MessageSender as Sender, DEFAULT_MUTE_SECONDS,
                            MUTE_MAX_SECONDS, MUTE_MIN_SECONDS, RECALL_DENY,
                            can_manage_group, can_mute, mute_denied_reason,
                            mute_seconds_of, mute_target_of)

PASS, FAIL = [], []
TMP_ROOT = ROOT / ".tmp_test" / "mute_recall_other"
BOT_ID = "100"
USER_ID = "200"
OTHER_ID = "300"
GROUP_ID = "999"

EMOTIONS = {"pingjing": {}, "shengqi": {}}


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


def ctx_of(capabilities=None, **overrides):
    cfg = {"text_lang": "ja", "display_lang": "zh", "default_voice": "pingjing",
           "llm_judge": True, "mute_enabled": True, "recall_other_enabled": True}
    cfg.update(overrides)
    return LH.RoleContext(cfg, {}, capabilities)


def prompt_of(capabilities=None, **overrides):
    return LH.build_system_prompt(ctx_of(capabilities, **overrides), EMOTIONS)


def sentence(zh, **extra):
    data = {"zh": zh, "lang": zh, "display": zh, "emotion": "pingjing"}
    data.update(extra)
    return data


# ---------------------------------------------------------------------------
# 桩：一个既能禁言、也能撤回的 NapCat 客户端
# ---------------------------------------------------------------------------
class AdminClient:
    """语音能力位关掉，避免测试真的去起 TTS 服务；mute/recall_other 未声明 → 按支持处理。"""

    platform = "napcat"
    capabilities = {"voice": False, "sticker": False}

    def __init__(self, bot_role="admin", member_role="admin", wrapped=False):
        self.self_id = BOT_ID
        self.sent = []
        self.deleted = []
        self.banned = []
        self._bot_role = bot_role
        self._member_role = member_role
        # NapCat 的 call_action 已经把响应里的 data 取出来了，返回的就是成员信息本身；
        # wrapped=True 用来模拟仍带 {"data": {...}} 外层的老形态
        self._wrapped = wrapped
        self._n = 0

    def _next_id(self):
        self._n += 1
        return f"m{self._n}"

    async def send_group_msg(self, group_id, message):
        mid = self._next_id()
        self.sent.append(mid)
        return {"message_id": mid}

    async def send_private_msg(self, user_id, message):
        mid = self._next_id()
        self.sent.append(mid)
        return {"message_id": mid}

    async def get_group_member_info(self, group_id, user_id):
        role = self._bot_role if str(user_id) == BOT_ID else self._member_role
        info = {"group_id": int(group_id), "user_id": int(user_id), "role": role}
        return {"status": "ok", "data": info} if self._wrapped else info

    async def set_group_ban(self, group_id, user_id, duration):
        self.banned.append((str(group_id), str(user_id), int(duration)))
        return {}

    async def delete_msg(self, message_id):
        self.deleted.append(str(message_id))
        return {}


class TextOnlyClient:
    """微信 ClawBot 那类只能发文字的通道。"""

    platform = "wechat_clawbot"
    capabilities = {"voice": False, "sticker": False, "poke": False, "recall": False,
                    "mention": False, "quote": False,
                    "recall_other": False, "mute": False}

    self_id = "wx_bot"

    async def get_group_member_info(self, group_id, user_id):
        return {"status": "ok", "data": {"role": "admin"}}

    async def set_group_ban(self, group_id, user_id, duration):
        raise AssertionError("只能发文字的通道不该被调用禁言接口")

    async def delete_msg(self, message_id):
        raise AssertionError("只能发文字的通道不该被调用撤回接口")


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
    sender.client = client if client is not None else AdminClient()
    return config, sender


def receipts_of(result) -> dict:
    """把回执列表按动作名索引，方便断言。"""
    return {r.get("action"): r for r in (result.get("action_receipts") or [])}


# ---------------------------------------------------------------------------
# T1 解析层
# ---------------------------------------------------------------------------
def t1_parse():
    section("T1 解析层")

    def keeps_mute_true():
        s = LH.normalize_single({"zh": "别闹了。"}, ctx_of(), EMOTIONS, "别闹了")
        s = LH.normalize_single({"zh": "别闹了。", "mute": True}, ctx_of(), EMOTIONS, "别闹了")
        return s.get("mute") is True

    def keeps_mute_qq():
        s = LH.normalize_single({"zh": "别闹了。", "mute": "200"}, ctx_of(), EMOTIONS, "别闹了")
        return s.get("mute") == "200"

    def rejects_mute_false():
        s = LH.normalize_single({"zh": "别闹了。", "mute": False}, ctx_of(), EMOTIONS, "别闹了")
        return "mute" not in s

    def keeps_duration():
        s = LH.normalize_single({"zh": "别闹了。", "mute": True, "mute_duration": "900"},
                                ctx_of(), EMOTIONS, "别闹了")
        return s.get("mute_duration") == 900

    def rejects_bad_duration():
        s = LH.normalize_single({"zh": "别闹了。", "mute": True, "mute_duration": "很久"},
                                ctx_of(), EMOTIONS, "别闹了")
        return "mute_duration" not in s

    def keeps_recall_other():
        s = LH.normalize_single({"zh": "别闹了。", "recall_other": True},
                                ctx_of(), EMOTIONS, "别闹了")
        return s.get("recall_other") is True

    def strips_leaked_field_lines():
        s = LH.normalize_single({"zh": "mute: true\nmute_duration: 300\n别闹了。"},
                                ctx_of(), EMOTIONS, "别闹了")
        return (s.get("mute") is True and s.get("mute_duration") == 300
                and s["zh"] == "别闹了。")

    def attaches_to_first_sentence():
        raw = json.dumps({"sentences": [
            {"zh": "你别闹了。", "ja": "やめて。", "emotion": "pingjing"},
            {"zh": "再闹就把你禁言了。", "ja": "また騒ぐならミュートする。",
             "emotion": "shengqi", "mute": True, "mute_duration": 600,
             "recall_other": True}]}, ensure_ascii=False)
        out = LH.normalize_sentences(raw, ctx_of(), EMOTIONS, "把她禁言了")
        return (len(out) == 2 and out[0].get("mute") is True
                and out[0].get("mute_duration") == 600
                and out[0].get("recall_other") is True
                and "mute" not in out[1])

    def actions_note_names_them():
        note = LH.sentence_actions_note([{"mute": True, "recall_other": True}])
        return "禁言" in note and "撤回别人的消息" in note and "mute_duration" not in note

    def fail_reason_denied():
        note = LH.action_receipts_note([{"action": "禁言", "ok": False, "reason": "denied"}])
        return note == "禁言：失败（没有权限）"

    def receipt_names_target():
        labeled = LH.action_receipts_note(
            [{"action": "禁言", "ok": True, "target": "10002"}],
            {"10002": "用户2"})
        plain = LH.action_receipts_note(
            [{"action": "禁言", "ok": True, "target": "10002"}])
        return labeled == "禁言（对象：用户2）：成功" \
            and plain == "禁言（对象：QQ:10002）：成功"

    check("mute=true 被保留", keeps_mute_true)
    check("mute 可以直接填 QQ 号", keeps_mute_qq)
    check("mute=false 不当成动作", rejects_mute_false)
    check("mute_duration 转成秒数", keeps_duration)
    check("非法 mute_duration 当没填", rejects_bad_duration)
    check("recall_other 被保留", keeps_recall_other)
    check("字段名写成正文时剥掉并照常生效", strips_leaked_field_lines)
    check("整组动作字段挂到第一句", attaches_to_first_sentence)
    check("动作说明写出禁言与撤回别人的消息", actions_note_names_them)
    check("回执说明把 denied 翻成「没有权限」", fail_reason_denied)
    check("回执写明动作对象（编号标签或 QQ 号）", receipt_names_target)


# ---------------------------------------------------------------------------
# T2 取值规则
# ---------------------------------------------------------------------------
def t2_targets():
    section("T2 禁言对象与时长取值")

    def picks_named_qq():
        return mute_target_of({"mute": OTHER_ID}, [USER_ID, OTHER_ID], USER_ID) == OTHER_ID

    def falls_back_to_first_allowed():
        # 主人@了某个人再让她禁言：名单第一个就是被@的人
        return mute_target_of({"mute": True}, [OTHER_ID, USER_ID], USER_ID) == OTHER_ID

    def no_target_stays_empty():
        # 名单是空的说明这一轮根本没指名：宁可这次禁不了，也不能拿当前发言者顶上
        return mute_target_of({"mute": True}, [], USER_ID) == ""

    def refuses_invented_qq():
        # 模型编出来的号码不在名单里：宁可这次禁言失败，也不能拿别人顶上
        return mute_target_of({"mute": "88888"}, [USER_ID], USER_ID) == ""

    def duration_default_and_clamped():
        return (mute_seconds_of({"mute": True}) == DEFAULT_MUTE_SECONDS
                and mute_seconds_of({"mute_duration": 1}) == MUTE_MIN_SECONDS
                and mute_seconds_of({"mute_duration": 10 ** 9}) == MUTE_MAX_SECONDS
                and mute_seconds_of({"mute_duration": 900}) == 900)

    def candidates_put_at_ids_first():
        return _mute_candidates([OTHER_ID], None, USER_ID, BOT_ID) == [OTHER_ID]

    def candidates_put_quoted_sender_before_recent():
        # 回复了某人的消息再让她禁言：被回复的人排在刚刚说话的人前面
        return _mute_candidates([], OTHER_ID, USER_ID, BOT_ID, "400") == [OTHER_ID, "400"]

    def candidates_use_recent_speaker():
        # 没@人也没引用时，「他」指的是刚刚在说话的那个人
        return _mute_candidates([], "", USER_ID, BOT_ID, OTHER_ID) == [OTHER_ID]

    def candidates_keep_requester_out():
        # 提要求的人自己不在名单里：他说的是「他」，不是「我」
        return _mute_candidates([], "", USER_ID, BOT_ID, "") == []

    def candidates_include_requester_on_self_request():
        return _mute_candidates([], "", USER_ID, BOT_ID, OTHER_ID,
                                self_request=True) == [OTHER_ID, USER_ID]

    def candidates_drop_bot_and_junk():
        return _mute_candidates([BOT_ID, "abc"], "", BOT_ID, BOT_ID) == []

    check("写明 QQ 号就禁言那个人", picks_named_qq)
    check("名单第一个（被@的人）优先于当前发言者", falls_back_to_first_allowed)
    check("名单为空时不给目标，不拿当前发言者顶上", no_target_stays_empty)
    check("编出来的号码一律不认", refuses_invented_qq)
    check("禁言时长有默认值且夹在上下限内", duration_default_and_clamped)
    check("@到的人排在候选名单最前", candidates_put_at_ids_first)
    check("被引用的人排在刚刚说话的人前面", candidates_put_quoted_sender_before_recent)
    check("没指名时用刚刚说话的那个人", candidates_use_recent_speaker)
    check("提要求的主人自己不进名单", candidates_keep_requester_out)
    check("主人明说要禁言自己时才把他算进来", candidates_include_requester_on_self_request)
    check("候选名单剔掉机器人与非号码", candidates_drop_bot_and_junk)


# ---------------------------------------------------------------------------
# T3 发送层：禁言
# ---------------------------------------------------------------------------
def t3_mute_send():
    section("T3 发送层：禁言")

    def group_admin_bans():
        client = AdminClient("admin", "admin")
        _config, sender = build_sender("mute_ok", client)
        result = run(sender.send_reply(
            "group", GROUP_ID, [sentence("再闹就禁言你。", mute=True, mute_duration=900)],
            EMOTIONS, M.RoleContext(_config.config), use_voice=True,
            speaker_id=USER_ID, speaker_role="admin", mute_ids=[USER_ID]))
        return (client.banned == [(GROUP_ID, USER_ID, 900)]
                and receipts_of(result)["禁言"]["ok"] is True)

    def plain_member_cannot_mute_others():
        client = AdminClient("admin", "member")
        _config, sender = build_sender("mute_others_denied", client)
        result = run(sender.send_reply(
            "group", GROUP_ID, [sentence("再闹就禁言他。", mute=True)],
            EMOTIONS, M.RoleContext(_config.config), use_voice=True,
            speaker_id=USER_ID, speaker_role="member", mute_ids=[OTHER_ID, USER_ID]))
        receipt = receipts_of(result)["禁言"]
        return (client.banned == [] and receipt["ok"] is False
                and receipt["reason"] == "denied")

    def plain_member_can_mute_self():
        # 群成员自己要求把自己禁言：谈不上借她的号去禁言别人，只要她是管理员就够
        client = AdminClient("admin", "member")
        _config, sender = build_sender("mute_self_ok", client)
        result = run(sender.send_reply(
            "group", GROUP_ID,
            [sentence("好，那就禁言你一分钟。", mute=True, mute_duration=60)],
            EMOTIONS, M.RoleContext(_config.config), use_voice=True,
            speaker_id=USER_ID, speaker_role="member", mute_ids=[USER_ID]))
        return (client.banned == [(GROUP_ID, USER_ID, 60)]
                and receipts_of(result)["禁言"]["ok"] is True)

    def bot_not_admin_denied():
        client = AdminClient("member", "admin")
        _config, sender = build_sender("mute_bot_member", client)
        result = run(sender.send_reply(
            "group", GROUP_ID, [sentence("再闹就禁言你。", mute=True)],
            EMOTIONS, M.RoleContext(_config.config), use_voice=True,
            speaker_id=USER_ID, speaker_role="admin", mute_ids=[USER_ID]))
        return (client.banned == []
                and receipts_of(result)["禁言"]["reason"] == "denied")

    def private_is_unsupported():
        client = AdminClient()
        _config, sender = build_sender("mute_private", client)
        result = run(sender.send_reply(
            "private", USER_ID, [sentence("再闹就禁言你。", mute=True)],
            EMOTIONS, M.RoleContext(_config.config), use_voice=True,
            speaker_id=USER_ID, mute_ids=[USER_ID]))
        return (client.banned == []
                and receipts_of(result)["禁言"]["reason"] == "unsupported")

    def switch_off_skips():
        client = AdminClient()
        _config, sender = build_sender("mute_off", client, mute_enabled=False)
        result = run(sender.send_reply(
            "group", GROUP_ID, [sentence("再闹就禁言你。", mute=True)],
            EMOTIONS, M.RoleContext(_config.config), use_voice=True,
            speaker_id=USER_ID, speaker_role="admin", mute_ids=[USER_ID]))
        return (client.banned == []
                and receipts_of(result)["禁言"]["reason"] == "disabled")

    def text_only_channel_unsupported():
        _config, sender = build_sender("mute_text_only", TextOnlyClient())
        result = run(sender.send_reply(
            "group", GROUP_ID, [sentence("再闹就禁言你。", mute=True)],
            EMOTIONS, M.RoleContext(_config.config), use_voice=True,
            speaker_id=USER_ID, speaker_role="admin", mute_ids=[USER_ID]))
        return receipts_of(result)["禁言"]["reason"] == "unsupported"

    def no_mute_no_receipt():
        client = AdminClient()
        _config, sender = build_sender("mute_absent", client)
        result = run(sender.send_reply(
            "group", GROUP_ID, [sentence("今天天气不错。")],
            EMOTIONS, M.RoleContext(_config.config), use_voice=True,
            speaker_id=USER_ID, speaker_role="admin", mute_ids=[USER_ID]))
        return client.banned == [] and result["action_receipts"] == []

    check("管理员群里真的调了禁言接口", group_admin_bans)
    check("一般群成员不能借她禁言别人", plain_member_cannot_mute_others)
    check("一般群成员要求禁言他自己可以做到", plain_member_can_mute_self)
    check("机器人自己不是管理员时禁不了", bot_not_admin_denied)
    check("私聊里禁言按「这条接入方式不支持」处理", private_is_unsupported)
    check("mute_enabled 关闭后不发接口", switch_off_skips)
    check("只能发文字的通道不发禁言接口", text_only_channel_unsupported)
    check("没声明动作时不产生回执", no_mute_no_receipt)


# ---------------------------------------------------------------------------
# T4 发送层：撤回别人的消息
# ---------------------------------------------------------------------------
def t4_recall_other_send():
    section("T4 发送层：撤回别人的消息")

    def group_admin_recalls():
        client = AdminClient("admin", "admin")
        _config, sender = build_sender("recall_other_ok", client)
        result = run(sender.send_reply(
            "group", GROUP_ID, [sentence("这条我撤掉了。", recall_other=True)],
            EMOTIONS, M.RoleContext(_config.config), use_voice=True,
            speaker_id=USER_ID, speaker_role="admin", mute_ids=[USER_ID],
            recall_other_id="777"))
        return (client.deleted == ["777"]
                and receipts_of(result)["撤回别人的消息"]["ok"] is True)

    def plain_member_denied():
        client = AdminClient("admin", "member")
        _config, sender = build_sender("recall_other_denied", client)
        result = run(sender.send_reply(
            "group", GROUP_ID, [sentence("这条我撤掉了。", recall_other=True)],
            EMOTIONS, M.RoleContext(_config.config), use_voice=True,
            speaker_id=USER_ID, speaker_role="member", mute_ids=[USER_ID],
            recall_other_id="777"))
        return (client.deleted == []
                and receipts_of(result)["撤回别人的消息"]["reason"] == "denied")

    def own_message_needs_no_admin():
        # 被引用的那条本来就是她自己发的：不用管理员权限
        client = AdminClient("member", "member")
        _config, sender = build_sender("recall_other_own", client)
        sender._remember_sent("group", GROUP_ID, "888", 1)
        result = run(sender.send_reply(
            "group", GROUP_ID, [sentence("这条我撤掉了。", recall_other=True)],
            EMOTIONS, M.RoleContext(_config.config), use_voice=True,
            speaker_id=USER_ID, speaker_role="member", mute_ids=[USER_ID],
            recall_other_id="888"))
        return (client.deleted == ["888"]
                and receipts_of(result)["撤回别人的消息"]["ok"] is True)

    def no_target_fails():
        client = AdminClient()
        _config, sender = build_sender("recall_other_no_target", client)
        result = run(sender.send_reply(
            "group", GROUP_ID, [sentence("这条我撤掉了。", recall_other=True)],
            EMOTIONS, M.RoleContext(_config.config), use_voice=True,
            speaker_id=USER_ID, speaker_role="admin", mute_ids=[USER_ID],
            recall_other_id=""))
        return (client.deleted == []
                and receipts_of(result)["撤回别人的消息"]["reason"] == "error")

    def switch_off_skips():
        client = AdminClient()
        _config, sender = build_sender("recall_other_off", client,
                                       recall_other_enabled=False)
        result = run(sender.send_reply(
            "group", GROUP_ID, [sentence("这条我撤掉了。", recall_other=True)],
            EMOTIONS, M.RoleContext(_config.config), use_voice=True,
            speaker_id=USER_ID, speaker_role="admin", mute_ids=[USER_ID],
            recall_other_id="777"))
        return (client.deleted == []
                and receipts_of(result)["撤回别人的消息"]["reason"] == "disabled")

    def no_double_recall_when_system_handled():
        # 主人要求撤的那条，系统在生成回复之前就撤掉了：这一轮不能再撤一遍
        # （消息已经不在了，第二遍必然失败，回执还会骗到判定那边）
        client = AdminClient("admin", "admin")
        _config, sender = build_sender("recall_other_dedupe", client)
        result = run(sender.send_reply(
            "group", GROUP_ID, [sentence("这条我撤掉了。", recall_other=True)],
            EMOTIONS, M.RoleContext(_config.config), use_voice=True,
            speaker_id=USER_ID, speaker_role="admin", mute_ids=[USER_ID],
            recall_other_id="777", recall_request=RECALL_DENY))
        return client.deleted == [] and result["action_receipts"] == []

    check("管理员群里真的撤掉了被引用的那条", group_admin_recalls)
    check("一般群成员不能借她撤别人的消息", plain_member_denied)
    check("撤她自己发过的消息不需要管理员权限", own_message_needs_no_admin)
    check("没有目标消息时如实报失败", no_target_fails)
    check("recall_other_enabled 关闭后不发接口", switch_off_skips)
    check("系统已撤过时不再重复撤一遍", no_double_recall_when_system_handled)


# ---------------------------------------------------------------------------
# T5 纯文字分支也带得走回执
# ---------------------------------------------------------------------------
def t5_plain_branch():
    section("T5 纯文字（delivery）分支")

    def plain_branch_keeps_receipts():
        client = AdminClient()
        _config, sender = build_sender("plain_receipts", client)
        result = run(sender.send_reply(
            "group", GROUP_ID,
            [sentence("再闹就禁言你。", delivery="plain", mute=True, mute_duration=120)],
            EMOTIONS, M.RoleContext(_config.config), use_voice=True,
            speaker_id=USER_ID, speaker_role="admin", mute_ids=[USER_ID]))
        return (client.banned == [(GROUP_ID, USER_ID, 120)]
                and receipts_of(result).get("禁言", {}).get("ok") is True)

    check("逐字/纯文字分支照样带回禁言回执", plain_branch_keeps_receipts)


# ---------------------------------------------------------------------------
# T6 流式路径
# ---------------------------------------------------------------------------
def t6_stream():
    section("T6 流式逐句发送")

    def stream_executes_actions():
        client = AdminClient("admin", "admin")
        config, sender = build_sender("stream_actions", client)
        sink = M.SentenceSink("group", GROUP_ID, EMOTIONS, M.RoleContext(config.config),
                              "", "把她禁言了", speaker_id=USER_ID, speaker_role="admin",
                              mute_ids=[USER_ID], recall_other_id="777")
        saved = M.sender
        M.sender = sender
        M.app_context.sender = sender

        async def flow():
            # 三件事必须在同一个事件循环里：worker 任务绑在起它的那个循环上，
            # 分几次 run() 会让工作器留在旧循环里不执行
            await sink.on_sentence(sentence("再闹就禁言你。", mute=True, mute_duration=600))
            await sink.on_sentence(sentence("那条我也撤了。", recall_other=True))
            await sink.flush()

        try:
            run(flow())
        finally:
            M.sender = saved
            M.app_context.sender = saved
        receipts = receipts_of({"action_receipts": sink.action_receipts})
        return (client.banned == [(GROUP_ID, USER_ID, 600)]
                and client.deleted == ["777"]
                and receipts["禁言"]["ok"] is True
                and receipts["撤回别人的消息"]["ok"] is True)

    check("流式路径执行禁言与撤回别人并留下回执", stream_executes_actions)


# ---------------------------------------------------------------------------
# T7 提示词
# ---------------------------------------------------------------------------
def t7_prompts():
    section("T7 提示词")

    def base_prompt_guides():
        text = prompt_of()
        return ("【禁言】" in text and "【撤回别人的消息】" in text
                and "recall_other" in text and "mute_duration" in text)

    def text_only_channel_omits_them():
        text = prompt_of({"mute": False, "recall_other": False, "quote": False,
                          "mention": False, "poke": False, "recall": False})
        return ("【禁言】mute 写在" not in text
                and "【撤回别人的消息】recall_other 写在" not in text
                and "也不能禁言别人或撤回别人的消息" in text)

    def switch_off_omits_them():
        text = prompt_of(mute_enabled=False, recall_other_enabled=False)
        return ("【禁言】mute 写在" not in text
                and "【撤回别人的消息】recall_other 写在" not in text)

    def field_list_mentions_them():
        text = prompt_of()
        return ("reply_to / mention_ids / poke / recall / recall_other / mute"
                in text)

    def sub_agent_prompt_guides():
        return ("recall_other" in TASK_PROGRESS_PROMPT
                and "禁言 → mute" in TASK_PROGRESS_PROMPT
                and "mute_duration" in TASK_PROGRESS_PROMPT
                and "管理员或群主" in TASK_PROGRESS_PROMPT)

    def urge_regex_covers_mute():
        from modules.companion_tasks import wants_task_action
        return wants_task_action("把她禁言了") and wants_task_action("撤回他这条")

    check("底层提示词引导禁言与撤回别人的消息", base_prompt_guides)
    check("只能发文字的通道不出现这两节", text_only_channel_omits_them)
    check("开关关掉后提示词不提这两件事", switch_off_omits_them)
    check("字段清单里补上了新字段", field_list_mentions_them)
    check("子代理提示词带上禁言/撤回别人的引导", sub_agent_prompt_guides)
    check("主人说「禁言」也会进入任务推进判定", urge_regex_covers_mute)


# ---------------------------------------------------------------------------
# T8 权限判据
# ---------------------------------------------------------------------------
def t8_permission():
    section("T8 群管理动作的权限判据")

    def both_admin_allowed():
        return run(can_manage_group(AdminClient("admin", "admin"), GROUP_ID,
                                    USER_ID, "admin")) is True

    def member_denied():
        return run(can_manage_group(AdminClient("admin", "member"), GROUP_ID,
                                    USER_ID, "member")) is False

    def text_only_denied():
        return run(can_manage_group(ILinkClient({"bot_token": "t"}), GROUP_ID,
                                    USER_ID, "admin")) is False

    check("请求者与机器人都是管理员时放行", both_admin_allowed)
    check("普通群成员被拒", member_denied)
    check("没有群成员接口的接入方式一律拒绝", text_only_denied)


# ---------------------------------------------------------------------------
# T9 群成员角色响应结构 / 只有动作没有台词时不再念兜底台词
# ---------------------------------------------------------------------------
def t9_role_and_action_only():
    section("T9 角色查询与「只有动作」的回复")

    def unwrapped_role_allowed():
        # NapCat 的 call_action 已经把 data 取出来，直接就是成员信息
        return run(can_manage_group(AdminClient("admin", "admin"), GROUP_ID,
                                    USER_ID, "")) is True

    def wrapped_role_allowed():
        return run(can_manage_group(AdminClient("admin", "admin", wrapped=True),
                                    GROUP_ID, USER_ID, "")) is True

    def missing_role_denied():
        return run(can_manage_group(AdminClient("admin", ""), GROUP_ID,
                                    USER_ID, "")) is False

    def action_only_object_keeps_action():
        text = '{"sentences":[{"mute":true,"mute_duration":180,"emotion":"pingjing"}]}'
        out = LH.normalize_sentences(text, ctx_of(), EMOTIONS, "那把他禁言 3 分钟")
        return (len(out) == 1 and out[0].get("mute") is True
                and out[0].get("mute_duration") == 180
                and not out[0].get("zh"))

    def action_only_top_level_object():
        out = LH.normalize_sentences('{"mute":true,"mute_duration":180}',
                                     ctx_of(), EMOTIONS, "那把他禁言 3 分钟")
        return (len(out) == 1 and out[0].get("mute") is True
                and out[0].get("mute_duration") == 180
                and not out[0].get("zh"))

    def no_action_still_uses_fallback():
        out = LH.normalize_sentences('{"sentences": []}', ctx_of(), EMOTIONS, "在吗")
        return out[0]["zh"] == LH.FALLBACK_REPLY

    def normalize_single_keeps_empty_for_action():
        s = LH.normalize_single({"mute": True}, ctx_of(), EMOTIONS, "把他禁言")
        return s["zh"] == "" and s["display"] == "" and s["lang"] == ""

    def normalize_single_still_falls_back():
        s = LH.normalize_single({}, ctx_of(), EMOTIONS, "在吗")
        return s["zh"] == LH.FALLBACK_REPLY

    def stream_skips_empty_sentence():
        client = AdminClient("admin", "admin")
        config, sender = build_sender("stream_action_only", client)
        sink = M.SentenceSink("group", GROUP_ID, EMOTIONS, M.RoleContext(config.config),
                              "", "那把他禁言 3 分钟", speaker_id=USER_ID,
                              speaker_role="admin", mute_ids=[USER_ID])
        saved = M.sender
        M.sender = sender
        M.app_context.sender = sender

        async def flow():
            await sink.on_sentence(sentence("", mute=True, mute_duration=180))
            await sink.flush()

        try:
            run(flow())
        finally:
            M.sender = saved
            M.app_context.sender = saved
        return (client.banned == [(GROUP_ID, USER_ID, 180)]
                and client.sent == []
                and sink.sent == 1)

    check("NapCat 形态的成员信息能查到角色", unwrapped_role_allowed)
    check("带 data 外层的老形态也能查到角色", wrapped_role_allowed)
    check("查不到角色时仍然拒绝", missing_role_denied)
    check("只有动作的对象不再变成兜底台词", action_only_object_keeps_action)
    check("顶层只有动作的对象不再变成兜底台词", action_only_top_level_object)
    check("真正没有内容时仍然用兜底台词", no_action_still_uses_fallback)
    check("normalize_single 对动作对象留空台词", normalize_single_keeps_empty_for_action)
    check("normalize_single 对空对象仍给兜底台词", normalize_single_still_falls_back)
    check("流式发送不给「只有动作」的那句发空消息", stream_skips_empty_sentence)


# ---------------------------------------------------------------------------
# T10 禁言的权限判据
# ---------------------------------------------------------------------------
def t10_mute_permission():
    section("T10 禁言的权限判据")

    def self_mute_needs_only_bot_admin():
        # 机器人是管理员、提出要求的是普通群成员，禁言他自己
        return run(can_mute(AdminClient("admin", "member"), GROUP_ID,
                            USER_ID, "member", USER_ID)) is True

    def self_mute_still_needs_bot_admin():
        return run(can_mute(AdminClient("member", "member"), GROUP_ID,
                            USER_ID, "member", USER_ID)) is False

    def others_need_admin_requester():
        return run(can_mute(AdminClient("admin", "member"), GROUP_ID,
                            USER_ID, "member", OTHER_ID)) is False

    def others_allowed_for_admin_requester():
        return run(can_mute(AdminClient("admin", "admin"), GROUP_ID,
                            USER_ID, "admin", OTHER_ID)) is True

    def owner_target_refused():
        return run(can_mute(AdminClient("admin", "owner"), GROUP_ID,
                            USER_ID, "admin", OTHER_ID)) is False

    def owner_target_reports_reason():
        return run(mute_denied_reason(AdminClient("admin", "owner"), GROUP_ID,
                                      USER_ID, "admin", OTHER_ID)) == "target_owner"

    check("禁言他自己只要机器人是管理员", self_mute_needs_only_bot_admin)
    check("机器人不是管理员时连他自己也禁不了", self_mute_still_needs_bot_admin)
    check("禁言别人仍要求提要求的人是管理员", others_need_admin_requester)
    check("管理员要求禁言别人放行", others_allowed_for_admin_requester)
    check("群主不能被禁言", owner_target_refused)
    check("禁言群主时回执写明原因", owner_target_reports_reason)


def t11_task_trigger():
    section("T11 任务推进的触发判据")

    from modules.companion_tasks import (wants_task_action, TASK_PROGRESS_PROMPT)
    from modules.llm_helpers import POKE_MESSAGE_TEXT

    def poke_does_not_trigger():
        return wants_task_action(POKE_MESSAGE_TEXT) is False

    def plain_at_bot_does_not_trigger():
        return wants_task_action("[@本机器人] 在吗") is False

    def at_plus_request_still_triggers():
        return wants_task_action("[@本机器人] 戳他一下") is True

    def at_other_plus_request_still_triggers():
        return wants_task_action("[@张三(QQ:10001)] 帮我撤回") is True

    def mention_only_does_not_trigger():
        return wants_task_action("[@张三(QQ:10001)] 晚上好") is False

    def complaint_is_not_a_request():
        return ("不算要求" in TASK_PROGRESS_PROMPT
                and "并没有答应要做什么" in TASK_PROGRESS_PROMPT)

    def successful_action_receipt_counts_as_done():
        # 引用/@人只要求动作时，回执成功就是做成了：不然判定会反复催她重做同一条
        return "回执显示成功就算做成了" in TASK_PROGRESS_PROMPT

    check("戳一戳事件本身不算主人要求动作", poke_does_not_trigger)
    check("只是@机器人说话不算要求动作", plain_at_bot_does_not_trigger)
    check("@机器人并明确要求时照常判定", at_plus_request_still_triggers)
    check("@别人并明确要求时照常判定", at_other_plus_request_still_triggers)
    check("只是@别人打招呼不算要求动作", mention_only_does_not_trigger)
    check("子代理提示词写明抱怨与提到词都不算要求", complaint_is_not_a_request)
    check("只要求动作时回执成功即算做成", successful_action_receipt_counts_as_done)


def t12_mute_order_and_claims():
    section("T12 主人吩咐禁言 / 台词里宣称已做完的动作")

    from modules.llm_helpers import (wants_mute_request, wants_self_mute,
                                     mute_request_seconds, detect_claimed_actions)

    def orders_recognised():
        return (wants_mute_request("把他禁言1个小时 我的token都要没了") is True
                and wants_mute_request("禁言[@张三(QQ:10001)] 1个小时") is True
                and wants_mute_request("把[@张三(QQ:10001)] 禁言 3 分钟") is True
                and wants_mute_request("把[@张三(QQ:10001)]和[@李四(QQ:10002)]禁言") is True
                and wants_mute_request("禁言啊") is True
                and wants_mute_request("禁言他10分钟") is True
                and wants_mute_request("禁言他啊") is True
                and wants_mute_request("快禁言他") is True)

    def questions_and_denials_ignored():
        return (wants_mute_request("你要禁言我") is False
                and wants_mute_request("该不该禁言他") is False
                and wants_mute_request("别禁言他") is False
                and wants_mute_request("别把[@张三(QQ:10001)]禁言") is False
                and wants_mute_request("禁言功能怎么用") is False
                and wants_mute_request("还有一个没禁言呢") is False
                and wants_mute_request("不是禁言我 是刚刚骂你的那个人") is False)

    def self_mute_recognised():
        return (wants_self_mute("禁言我") is True
                and wants_self_mute("把他禁言1个小时") is False
                and wants_self_mute("不是禁言我") is False)

    def duration_parsed():
        return (mute_request_seconds("把他禁言1个小时") == 3600
                and mute_request_seconds("禁言他10分钟") == 600
                and mute_request_seconds("禁言600秒") == 600
                and mute_request_seconds("把[@张三(QQ:10001)] 禁言 3 分钟") == 180
                and mute_request_seconds("禁言啊") == 0)

    def claimed_mute_and_poke():
        return (detect_claimed_actions("已成功禁言群主300秒，主人请放心～") == {"mute": True}
                and detect_claimed_actions("本座都戳回去啦，再这样……就真的生气咯～")
                == {"poke": True})

    def hypothetical_claims_skipped():
        return (detect_claimed_actions("再敢说一遍就禁言你") == {}
                and detect_claimed_actions("要是能戳你就好了") == {}
                and detect_claimed_actions("本座可是会戳回去的！") == {})

    check("主人吩咐禁言的几种写法都认出来", orders_recognised)
    check("提问与否定不算吩咐", questions_and_denials_ignored)
    check("「禁言我」才把主人自己算成目标", self_mute_recognised)
    check("禁言时长按主人说的解析", duration_parsed)
    check("台词里宣称已做完的动作补成真动作", claimed_mute_and_poke)
    check("假设语气里的动作不算", hypothetical_claims_skipped)


def main():
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    saved_global, M.global_config = M.global_config, M.ConfigLoader.default_config()
    M.app_context.global_config = M.global_config
    try:
        for fn in (t1_parse, t2_targets, t3_mute_send, t4_recall_other_send,
                   t5_plain_branch, t6_stream, t7_prompts, t8_permission,
                   t9_role_and_action_only, t10_mute_permission, t11_task_trigger,
                   t12_mute_order_and_claims):
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
