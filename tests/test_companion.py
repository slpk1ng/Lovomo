# -*- coding: utf-8 -*-
"""Lovomo 陪伴功能专项回归测试。

覆盖：
  A 承诺追踪：记录、去重、到期、兑现标记、台词提取（LLM 用假实现）
  B 随机奇遇：角色现场想一件、当天不重掷、讲过即消耗、关闭
  C 关系进度扩展：伴侣确立时间、在一起天数、纪念日到期与登记、冷落流失、
    阶段解锁（回复概率下限 / 闲置阈值乘数）、好感曲线
  D 心情扩展：时间环境偏置（深夜/周末）、心情历史与曲线、按天取点、
    冷清衰减、心情日记
  E 消息里的情敌提及识别与默认配置键
  F 每日陪伴检查：纪念日 / 承诺兑现 / 冷落流失 / 心情日记 / 奇遇

运行: python tests/test_companion.py     （全通过退出码 0）
"""
import asyncio
import contextlib
import io
import os
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

PASS, FAIL = [], []


def check(name, fn):
    try:
        result = fn()
        if not result:
            raise AssertionError(f"断言为假: {result!r}")
        PASS.append(name)
        print(f"  [PASS] {name}")
    except Exception as e:
        FAIL.append((name, repr(e)))
        print(f"  [FAIL] {name}: {e!r}")


def section(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


class Cfg(dict):
    def get(self, k, d=None):
        return super().get(k, d)


TMP = ROOT / ".tmp_test" / "companion"


def fresh_dir(name: str) -> Path:
    import shutil
    path = TMP / name
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# B 承诺追踪
# ---------------------------------------------------------------------------
def b_promises():
    from modules.promises import PromiseManager, extract_promise, PROMISE_HINT_RE
    from modules.llm_helpers import RoleContext

    section("B 承诺追踪")
    mgr = PromiseManager(fresh_dir("promises"))
    p1 = mgr.add("murasame", "private_1", "u1", "下次给你讲故事")
    p2 = mgr.add("murasame", "private_1", "u1", "下次给你讲故事")
    check("承诺入库且内容重复的不再记第二条",
          lambda: p1 is not None and p2 is None
          and len(mgr.items) == 1)
    check("触发词粗筛能认出常见承诺说法",
          lambda: bool(PROMISE_HINT_RE.search("改天我讲给你听"))
          and not PROMISE_HINT_RE.search("今天天气不错"))
    check("没到等待天数的不算到期",
          lambda: mgr.due(min_age_days=1, limit=10) == [])
    mgr.items[0]["created"] = time.time() - 2 * 86400
    due = mgr.due(min_age_days=1, limit=10)
    check("超过等待天数的承诺到期",
          lambda: len(due) == 1 and due[0]["content"] == "下次给你讲故事")
    check("兑现后不再出现到期名单里",
          lambda: (mgr.mark_fulfilled(due[0]) or True)
          and mgr.due(min_age_days=0, limit=10) == []
          and mgr.items[0]["status"] == "fulfilled")

    from modules import promises as promise_mod

    async def run_extract(content):
        async def fake_chat(ctx, messages, tools=None):
            return {"content": content, "tool_calls": [], "ms": 1.0, "backend": "fake"}
        old = promise_mod.chat_once
        promise_mod.chat_once = fake_chat
        try:
            return await extract_promise(RoleContext({"character_key": "murasame"}, {}),
                                         "那说好了，明天我做蛋糕给你吃。")
        finally:
            promise_mod.chat_once = old

    check("LLM 返回的 JSON 提取出承诺内容",
          lambda: asyncio.run(run_extract('{"promise": "明天做蛋糕"}')) == "明天做蛋糕")
    check("LLM 判定没有承诺时返回 None",
          lambda: asyncio.run(run_extract('{"promise": null}')) is None)
    check("空台词不发起提取",
          lambda: asyncio.run(extract_promise(None, "")) is None)


# ---------------------------------------------------------------------------
# C 随机奇遇
# ---------------------------------------------------------------------------
def c_encounters():
    import random as random_mod
    from modules.encounters import EncounterManager

    section("C 随机奇遇")
    base = {"adventure_enabled": True, "adventure_daily_chance": 1.0}
    today = time.strftime("%Y-%m-%d")

    def manager(**over):
        cfg = dict(base)
        cfg.update(over)
        return EncounterManager(Cfg(cfg), fresh_dir("enc"))

    m = manager()
    check("概率命中时该由角色现场想一件", lambda: m.should_roll("murasame", today))
    m.set_event("murasame", today, "在路上捡到一只湿漉漉的小猫")
    text = m.take("murasame", today)
    check("角色想好的奇遇带入一轮对话后消耗掉",
          lambda: text == "在路上捡到一只湿漉漉的小猫"
          and m.take("murasame", today) == "")
    check("同一天不再重复安排", lambda: not m.should_roll("murasame", today))
    m.set_event("murasame", "2000-01-01", "在下雨天想起了些往事")
    check("换一天重新按概率安排",
          lambda: m.take("murasame", "2000-01-01") == "在下雨天想起了些往事")

    class _Never:
        @staticmethod
        def random():
            return 0.99

    m2 = manager(adventure_daily_chance=0.5)
    old_random = random_mod.random
    random_mod.random = _Never.random
    try:
        rolled = m2.should_roll("murasame", today)
        m2.mark_none("murasame", today)
    finally:
        random_mod.random = old_random
    check("概率未命中当天就没有奇遇",
          lambda: rolled is False and m2.take("murasame", today) == "")
    check("开关关闭时不安排",
          lambda: not manager(adventure_enabled=False).should_roll("murasame", today)
          and manager(adventure_enabled=False).take("murasame", today) == "")
    check("想不出内容时当天按没有奇遇处理",
          lambda: (manager().set_event("murasame", today, "  ") or True)
          and manager().take("murasame", today) == "")


# ---------------------------------------------------------------------------
# D 关系进度扩展
# ---------------------------------------------------------------------------
def d_affection_extra():
    import random as random_mod
    import modules.affection as affection_mod
    from modules.affection import AffectionManager

    section("D 关系进度：里程碑 / 解锁 / 流失 / 曲线")

    class _EvenRandom:
        @staticmethod
        def uniform(a, b):
            return 1.0

        @staticmethod
        def random():
            return 1.0

    work = fresh_dir("aff")
    now = time.time()
    base = {"affection_enabled": True, "affection_partner_threshold": 90,
            "affection_accept_min_stage": "暧昧", "affection_turn_delta_max": 3,
            "affection_daily_gain_cap": 100, "affection_anniversary_days": "7,30",
            "affection_stage_unlocks_enabled": True,
            "affection_decay_enabled": True}

    def manager(**over):
        cfg = dict(base)
        cfg.update(over)
        return AffectionManager(Cfg(cfg), work)

    old_random = affection_mod.random
    affection_mod.random = _EvenRandom
    try:
        m = manager()
        for _ in range(31):
            m.apply_delta("murasame", "u1", 3)
        m.apply_delta("murasame", "u1", 0, confession=True, reply_text="我愿意。")
        rec = m.get("murasame", "u1")
        check("确立关系时记录伴侣确立时间",
              lambda: rec["partner"] and 0 < rec["partner_since"] <= now + 5)
        check("在一起天数从确立当天算第 1 天",
              lambda: m.partner_days("murasame", "u1") == 1)

        # 关系性质已经是「暧昧」时，回复里出现"答应"字样不等于确认交往：
        # 她答应的可能是别的事（主人让她一个字一个字说话，她回一句"本座就答应你好了"）
        wrong = manager()
        for _ in range(31):
            wrong.apply_delta("murasame", "u9", 3)
        wrong.apply_delta("murasame", "u9", 0, romance=True,
                          reply_text="那……本座就勉为其难地答应你好了！")
        check("答应别的事不会被记成确认交往",
              lambda: not wrong.get("murasame", "u9")["partner"])
        wrong.apply_delta("murasame", "u9", 0, romance=True,
                          reply_text="那我认你做我的恋人了。")
        check("回复里明确说出关系词才记为伴侣",
              lambda: wrong.get("murasame", "u9")["partner"])

        # 关系性质要先到位：本轮刚心动不能顺手定成伴侣（否则一句心动 + 一句「答应你」
        # 就能把刚认识的人变成恋人）
        fresh = manager()
        fresh.apply_delta("murasame", "u11", 3, romance=True,
                          reply_text="本座…好像有点心动了呢。")
        check("本轮刚心动不会立刻变成伴侣",
              lambda: not fresh.get("murasame", "u11")["partner"])
        fresh.apply_delta("murasame", "u11", 1,
                          reply_text="那就认你做我的恋人了。")
        check("关系性质到位后下一轮确认才记伴侣",
              lambda: fresh.get("murasame", "u11")["partner"])

        check("好感历史随变化累积且供曲线读取",
              lambda: len(m.curve_points("murasame")) >= 31
              and m.curve_points("murasame")[0][1] == 3)

        per_session = manager()
        per_session.records["murasame"] = {
            "private_1|u6": {"score": 10, "history": [[now, 10]]},
            "group_9|u7": {"score": 20, "history": [[now, 20]]},
        }
        check("曲线按会话取点，不把别人的好感混成一条",
              lambda: per_session.curve_points("murasame", "private_1") == [[now, 10.0]]
              and per_session.curve_points("murasame", "group_9") == [[now, 20.0]]
              and len(per_session.curve_points("murasame")) == 2)

        # 会话记录被删掉时，这个会话自己的关系进度要跟着走（否则界面上还挂着
        # 「群聊 xxx：恋人」，再发一条消息又接着往下算）；群聊里每个成员各一条，
        # 按会话整体清，成员在私聊里的关系不受影响
        drop = manager()
        drop.records["murasame"] = {
            "group_9|u12": {"score": 30, "history": []},
            "group_9|u13": {"score": 20, "history": []},
            "private_1|u12": {"score": 80, "history": []},
        }
        removed = drop.drop_session("murasame", "group_9")
        check("删群聊会话时只清这个群里的关系记录",
              lambda: removed == 2
              and "group_9|u12" not in drop.records["murasame"]
              and "private_1|u12" in drop.records["murasame"])
        check("删不存在的会话不动任何记录",
              lambda: drop.drop_session("murasame", "group_nope") == 0)

        # 关系不能只有"记"没有"解"：对方明确说分手/绝交时要退回去
        check("分手/绝交的说法能认出来（否定句不算）",
              lambda: affection_mod.looks_like_breakup("我们分手吧。") is True
              and affection_mod.looks_like_breakup("那就绝交好了。") is True
              and affection_mod.looks_like_breakup("我才不要分手呢。") is False
              and affection_mod.looks_like_breakup("今天心情不错。") is False)
        broke = manager()
        broke.records["murasame"] = {"private_9|u14": {
            "score": 88, "partner": True, "partner_since": now, "romance": True,
            "history": [], "day": "", "day_gain": 0, "anniversaries": []}}
        broke.apply_delta("murasame", "u14", -3, breakup=True,
                          reply_text="好，那就分手吧。", session_id="private_9")
        rec_after = broke.get("murasame", "u14", "private_9")
        check("说分手时解除关系，好感保留",
              lambda: rec_after["partner"] is False and rec_after["romance"] is False
              and rec_after["partner_since"] == 0 and rec_after["score"] >= 80)
        by_her = manager()
        by_her.records["murasame"] = {"private_9|u15": {
            "score": 70, "partner": True, "partner_since": now, "romance": True,
            "history": [], "day": "", "day_gain": 0, "anniversaries": []}}
        by_her.apply_delta("murasame", "u15", 0,
                           reply_text="既然这样，那我们就分手吧。", session_id="private_9")
        check("角色自己把关系说断也会解除",
              lambda: not by_her.get("murasame", "u15", "private_9")["partner"])
        check("已经解除过的不再重复解除",
              lambda: by_her.break_up("murasame", "u15", "private_9") is False)

        # 关系没到「暧昧」之前不能调情：角色设定里"爱调情"那类描述要等关系到了才适用，
        # 否则刚认识就把气氛搞成暧昧
        stage = manager()
        from modules.llm_helpers import RoleContext
        role = {"character_key": "murasame", "character_name": "丛雨"}
        ctx_role = RoleContext({"character_name": "丛雨"}, role)
        note_new = stage.build_note(ctx_role, "u16", "用户1", "private_1")
        check("陌生阶段的说明明确压住人设里的调情描述",
              lambda: "都还不适用" in note_new and "你也可以主动" not in note_new)
        stage.records["murasame"] = {"private_1|u16": {
            "score": 20, "romance": True, "partner": False, "history": [],
            "day": "", "day_gain": 0, "anniversaries": []}}
        note_romance = stage.build_note(ctx_role, "u16", "用户1", "private_1")
        check("到了暧昧阶段才允许她主动",
              lambda: "你也可以主动" in note_romance and "都还不适用" not in note_romance)

        fake = manager()
        fake_rec = {"score": 90, "partner": True, "partner_since": now - 6 * 86400,
                    "updated": now, "day": "", "day_gain": 0, "anniversaries": []}
        fake.records["murasame"] = {"u2": fake_rec}
        check("第 7 天命中纪念日且未发过祝贺",
              lambda: fake.anniversary_due("murasame", "u2") == 7)
        fake.mark_anniversary_sent("murasame", "u2", 7)
        check("发过的纪念日不再重复祝贺",
              lambda: fake.anniversary_due("murasame", "u2") == 0
              and fake.get("murasame", "u2")["anniversaries"] == [7])
        not_partner = dict(fake_rec, partner=False)
        fake.records["murasame"]["u3"] = not_partner
        check("不是伴侣没有纪念日",
              lambda: fake.anniversary_due("murasame", "u3") == 0)

        idle = manager(affection_decay_idle_days=3, affection_decay_daily_drop=1)
        idle.records["murasame"] = {
            "stale": {"score": 10, "partner": False, "updated": now - 4 * 86400},
            "fresh": {"score": 10, "partner": False, "updated": now},
        }
        dropped = idle.decay_idle(3, 1)
        check("久未互动的好感流失、活跃的不动",
              lambda: dropped == 1
              and idle.records["murasame"]["stale"]["score"] == 9
              and idle.records["murasame"]["fresh"]["score"] == 10)
        idle.records["murasame"]["stale"]["score"] = 0
        check("好感为 0 后不再流失",
              lambda: idle.decay_idle(3, 1) == 0)

        m2 = manager()
        m2.records["murasame"] = {
            "u4": {"score": 80, "romance": True, "partner": False, "updated": now},
            "u4b": {"score": 80, "partner": False, "updated": now},
            "u5": {"score": 95, "partner": True, "partner_since": now,
                   "updated": now},
        }
        check("暧昧阶段解锁回复概率下限",
              lambda: m2.stage_reply_floor("murasame", "u4") == 0.5
              and m2.stage_reply_floor("murasame", "u5") == 0.8)
        check("只是好感高、没有恋爱意味的不会按暧昧解锁",
              lambda: m2.stage_reply_floor("murasame", "u4b") == 0.0
              and m2.stage("murasame", "u4b") == "挚友")
        check("恋人阶段私聊闲置阈值减半",
              lambda: m2.stage_idle_factor("murasame", "u5") == 0.5
              and m2.stage_idle_factor("murasame", "u4") == 1.0)
        check("关闭解锁后两项都回到默认",
              lambda: (manager(affection_stage_unlocks_enabled=False)
                       .stage_reply_floor("murasame", "u5") == 0.0
                       and manager(affection_stage_unlocks_enabled=False)
                       .stage_idle_factor("murasame", "u5") == 1.0))
    finally:
        affection_mod.random = random_mod


# ---------------------------------------------------------------------------
# E 心情扩展
# ---------------------------------------------------------------------------
def e_mood_extra():
    import modules.mood as mood_mod
    from modules.llm_helpers import RoleContext
    from modules.mood import MoodManager, env_bias, stored_mood

    section("E 心情：环境偏置 / 历史 / 冷清衰减 / 日记")

    def ctx_with(**over):
        cfg = {"mood_enabled": True, "mood_env_enabled": True,
               "reply_judge_mood_min": 0, "reply_judge_mood_max": 100,
               "reply_judge_mood_initial": 60, "character_key": "murasame",
               "mood_env_night_start": "00:00", "mood_env_night_end": "06:00",
               "mood_env_night_drop": 5, "mood_env_weekend_boost": 5}
        cfg.update(over)
        return RoleContext(Cfg(cfg))

    def fake_localtime(hour, minute, wday):
        return time.struct_time((2026, 9, 26, hour, minute, 0, wday, 269, 0))

    old_localtime = mood_mod.time.localtime
    try:
        mood_mod.time.localtime = lambda: fake_localtime(2, 0, 3)
        check("深夜窗口内心情偏置走低", lambda: env_bias(ctx_with()) == -5.0)
        check("深夜窗口可跨过午夜",
              lambda: env_bias(ctx_with(mood_env_night_start="23:00")) == -5.0)
        mood_mod.time.localtime = lambda: fake_localtime(12, 0, 5)
        check("周末偏置回暖",
              lambda: env_bias(ctx_with()) == 5.0
              and env_bias(ctx_with(mood_env_weekend_boost=0)) == 0.0)
        mood_mod.time.localtime = lambda: fake_localtime(12, 0, 1)
        check("工作日白天没有偏置", lambda: env_bias(ctx_with()) == 0.0)
        check("关闭环境偏置后恒为 0",
              lambda: env_bias(ctx_with(mood_env_enabled=False)) == 0.0)

        mm = MoodManager(fresh_dir("mood"))
        mm.set_mood("s1", "murasame", 55)
        ctx = ctx_with()
        mood_mod.time.localtime = lambda: fake_localtime(12, 0, 6)
        check("当前心情值 = 落盘值 + 环境偏置并夹回值域",
              lambda: mood_mod.current_mood(ctx, mm, "s1") == 60.0
              and stored_mood(ctx, mm, "s1") == 55)
        mood_mod.time.localtime = lambda: fake_localtime(2, 0, 3)
        check("深夜工作日的判定值走低",
              lambda: mood_mod.current_mood(ctx, mm, "s1") == 50.0)
        check("心情历史随落盘累积",
              lambda: len(mm.records[[k for k in mm.records][0]]["history"]) == 1)
        check("曲线按角色聚合历史点",
              lambda: mm.set_mood("s2", "murasame", 66) is None
              and len(mm.curve_points("murasame")) == 2)
        check("曲线可按会话单独取点，不把别的会话混进来",
              lambda: len(mm.curve_points("murasame", "s1")) == 1
              and mm.curve_points("murasame", "s1")[0][1] == 55.0
              and mm.curve_points("murasame", "s2")[0][1] == 66.0)
        day = time.strftime("%Y-%m-%d")
        check("按天取点落在当天区间内",
              lambda: len(mm.mood_points_for_day("murasame", day)) == 2
              and mm.mood_points_for_day("murasame", "2000-01-01") == [])

        lonely = MoodManager(fresh_dir("mood_lonely"))
        lonely.records = {
            "s1::murasame": {"mood": 50, "updated": time.time() - 4 * 86400},
            "s2::murasame": {"mood": 50, "updated": time.time()},
            "s3::murasame": {"mood": 0, "updated": time.time() - 4 * 86400},
        }
        dropped = lonely.decay_lonely(3, 2, 0)
        check("冷清会话心情走低、活跃与见底的不动",
              lambda: dropped == 1
              and lonely.records["s1::murasame"]["mood"] == 48
              and lonely.records["s2::murasame"]["mood"] == 50
              and lonely.records["s3::murasame"]["mood"] == 0)

        diary = MoodManager(fresh_dir("mood_diary"))
        diary.add_diary("murasame", "2026-09-28", "今天有点累。",
                        {"min": 30, "max": 60})
        check("日记按日期落档且能取回",
              lambda: diary.has_diary("murasame", "2026-09-28")
              and diary.get_diary()["murasame"][0]["text"] == "今天有点累。")
        diary.add_diary("murasame", "2026-09-28", "重写后的日记。")
        check("同一天重写日记不产生重复条目",
              lambda: len(diary.diary["murasame"]) == 1
              and diary.get_diary()["murasame"][0]["text"] == "重写后的日记。")
    finally:
        mood_mod.time.localtime = old_localtime


# ---------------------------------------------------------------------------
# F 情敌提及与默认配置键
# ---------------------------------------------------------------------------
def f_defaults_and_rival():
    section("F 情敌提及识别与默认配置键")

    def rival():
        import main as M
        M.global_config = Cfg({"roles": {
            "murasame": {"character_key": "murasame", "character_name": "丛雨"},
            "yukina": {"character_key": "yukina", "character_name": "小雪"},
        }})
        M.app_context.global_config = M.global_config
        return M

    check("消息提到别的角色名字能被识别，提到自己不算",
          lambda: rival()._detect_rival_mention("小雪今天怎么样", "murasame") == "小雪"
          and rival()._detect_rival_mention("丛雨最可爱", "murasame") == ""
          and rival()._detect_rival_mention("随便聊聊", "murasame") == "")

    import main as M
    defaults = M.ConfigLoader.default_config()
    needed = ("affection_stage_unlocks_enabled", "affection_anniversary_days",
              "affection_decay_enabled", "affection_decay_idle_days",
              "affection_decay_daily_drop", "mood_jealousy_penalty",
              "mood_env_enabled", "mood_env_night_start", "mood_env_night_end",
              "mood_env_night_drop", "mood_env_weekend_boost",
              "mood_env_lonely_days", "mood_env_lonely_drop", "mood_diary_enabled",
              "companion_check_time", "adventure_enabled", "adventure_daily_chance",
              "promise_enabled", "promise_fulfill_days", "promise_max_per_day")
    check("陪伴功能默认配置键齐全",
          lambda: all(k in defaults for k in needed))
    check("陪伴功能配置项在 WebUI 三处同步（default_config + configGroups + configMeta）",
          lambda: (lambda html: all(
              f"'{k}'" in html for k in needed
          ))((ROOT / "webui" / "start.html").read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# G 每日陪伴检查（端到端）
# ---------------------------------------------------------------------------
def g_companion_daily_check():
    import main as M

    section("G 每日陪伴检查：纪念日 / 承诺兑现 / 冷落流失 / 心情日记")

    class _FakeSender:
        def __init__(self):
            self.client = object()
            self.sent = []
            self.not_recorded = []

        def session_client(self, session_id):
            # 没有真实连接：按「能力全支持」处理（reply_ctx_of 会据此取能力位）
            return None

        def _active_client(self):
            return None

        @contextlib.contextmanager
        def for_session(self, session_id):
            self.last_session = session_id
            yield

        async def speak_and_send(self, session_type, target_id, text, emotions,
                                 ctx, use_voice=False, sticker=False,
                                 session_id="", record_history=True):
            self.sent.append((session_type, target_id, text))
            if not record_history:
                self.not_recorded.append((session_id, text))
            return True

    work = fresh_dir("daily")
    now = time.time()
    yesterday = time.strftime("%Y-%m-%d", time.localtime(now - 86400))
    cfg = M.ConfigLoader(str(work / "config.json"))
    cfg.config.update({
        "memory_data_path": str(work), "ref_audio_root": "",
        "scheduler_enabled": True, "proactive_quiet_start": "00:00",
        "proactive_quiet_end": "00:00", "affection_enabled": True,
        "affection_anniversary_days": "7,30", "affection_decay_enabled": True,
        "affection_decay_idle_days": 3, "affection_decay_daily_drop": 1,
        "mood_env_enabled": True, "mood_env_lonely_days": 3,
        "mood_env_lonely_drop": 2, "mood_diary_enabled": True,
        "adventure_enabled": True, "adventure_daily_chance": 1.0,
        "promise_enabled": True, "promise_fulfill_days": 1,
        "promise_max_per_day": 3,
        "active_character": "murasame",
        "roles": [
            {"character_key": "murasame", "character_name": "丛雨",
             "personality_prompt": "你是丛雨", "ref_audio_root": ""},
        ],
    })
    cfg.roles = cfg._parse_roles()
    M.global_config = cfg
    M.app_context.global_config = M.global_config
    M.memory_manager = M.MemoryManager(cfg)
    M.app_context.memory_manager = M.memory_manager
    M.mood_mgr = M.MoodManager(work)
    M.app_context.mood_mgr = M.mood_mgr
    M.affection_mgr = M.AffectionManager(cfg, work)
    M.app_context.affection_mgr = M.affection_mgr
    M.promise_mgr = M.PromiseManager(work)
    M.app_context.promise_mgr = M.promise_mgr
    M.encounter_mgr = M.EncounterManager(cfg, work)
    M.app_context.encounter_mgr = M.encounter_mgr
    fake_sender = _FakeSender()
    M.sender = fake_sender
    M.app_context.sender = M.sender

    # 假 LLM：generate_proactive_text → jobs.generate_in_character_text
    #        → jobs.generate_text_reply
    import modules.jobs as jobs_mod

    async def fake_text_reply(ctx, system, user_prompt, max_tokens=200, **kwargs):
        return "（生成文本）"

    old_text_reply = jobs_mod.generate_text_reply
    jobs_mod.generate_text_reply = fake_text_reply

    # 私聊会话存在；伴侣确立于 6 天前（今天是在一起第 7 天）
    M.memory_manager.save_session_data("private_10001",
                                       {"history": [{"role": "user",
                                                     "content": "嗨",
                                                     "timestamp": now}]})
    M.affection_mgr.records["murasame"] = {
        "10001": {"score": 95, "partner": True, "partner_since": now - 6 * 86400,
                  "updated": now, "day": "", "day_gain": 0, "anniversaries": []},
        "20002": {"score": 10, "partner": False, "updated": now - 5 * 86400},
    }
    M.promise_mgr.items = [{"character_key": "murasame", "session_id": "private_10001",
                            "user_id": "10001", "content": "下次给你讲故事",
                            "created": now - 2 * 86400, "status": "pending"}]
    # 昨天真的聊过（对话与心情点都落在昨天）→ 应生成并发出昨天的心情日记
    y_start = time.mktime(time.strptime(yesterday, "%Y-%m-%d"))
    M.memory_manager.save_session_data("private_10001", {"history": [
        {"role": "user", "content": "昨天一起去吃了草莓蛋糕", "sender_id": "10001",
         "timestamp": y_start + 3600},
        {"role": "assistant", "content": "嗯，很开心。", "speaker": "丛雨",
         "timestamp": y_start + 3700}]})
    M.mood_mgr.records["private_10001::10001::murasame"] = {
        "mood": 55, "updated": now,
        "history": [[y_start + 3600, 40], [y_start + 7200, 62]]}

    sent = asyncio.run(M.companion_daily_check())

    check("纪念日与承诺兑现各发出一条主动消息", lambda: sent == 2)
    check("纪念日祝贺发到对方私聊且已登记不再重发",
          lambda: ("private", "10001") in [(a, b) for a, b, _c in fake_sender.sent]
          and M.affection_mgr.get("murasame", "10001")["anniversaries"] == [7])
    check("兑现过的承诺标记完成",
          lambda: M.promise_mgr.items[0]["status"] == "fulfilled")
    check("久未互动的用户好感已流失",
          lambda: M.affection_mgr.records["murasame"]["20002"]["score"] == 9)
    check("昨天的日记已生成",
          lambda: M.mood_mgr.has_diary("murasame", yesterday, "private_10001")
          and bool(M.mood_mgr.get_diary()["murasame"][0]["text"]))
    check("日记写完发给了对应的会话且不写进会话历史",
          lambda: any(text == "（生成文本）" for _st, _sid, text in fake_sender.sent)
          and any(sid == "private_10001" for sid, _t in fake_sender.not_recorded)
          and all(m.get("content") != "（生成文本）"
                  for m in M.memory_manager.load_session_data("private_10001")["history"]))
    check("没聊过的日子不凭空写日记",
          lambda: not M.mood_mgr.has_diary("murasame",
                                           time.strftime("%Y-%m-%d",
                                                         time.localtime(now - 3 * 86400))))
    check("奇遇当天已安排",
          lambda: M.encounter_mgr.take("murasame", time.strftime("%Y-%m-%d")) == "（生成文本）")

    # 再跑一次：纪念日已登记、承诺已完成，不再重发
    sent_again = asyncio.run(M.companion_daily_check())
    check("同一天内重复检查不会重发消息", lambda: sent_again == 0)

    # 关机几天没开：更早那天的日记也要在启动补写时补上
    older = time.strftime("%Y-%m-%d", time.localtime(time.time() - 2 * 86400))
    older_start = time.mktime(time.strptime(older, "%Y-%m-%d"))
    M.memory_manager.save_session_data("private_10001", {"history": [
        {"role": "user", "content": "前天也陪本座聊了很久", "sender_id": "10001",
         "timestamp": older_start + 3600},
        {"role": "assistant", "content": "是啊。", "speaker": "丛雨",
         "timestamp": older_start + 3700}]})
    M.mood_mgr.records["private_10001::10001::murasame"]["history"] = [
        [older_start + 3600, 30], [older_start + 7200, 72]]
    written = asyncio.run(M.write_mood_diaries())
    check("错过每日检查时启动补写更早那天的日记",
          lambda: written >= 1 and M.mood_mgr.has_diary("murasame", older, "private_10001")
          and bool(M.mood_mgr.get_diary()["murasame"]))
    check("已经写过的那天不重复补写",
          lambda: asyncio.run(M.write_mood_diaries()) == 0)

    # 日记必须是新写的：模型把当天说过的话抄一遍（"偷懒"）时不落档
    copied_day = time.strftime("%Y-%m-%d", time.localtime(time.time() - 3 * 86400))
    copy_start = time.mktime(time.strptime(copied_day, "%Y-%m-%d"))
    M.memory_manager.save_session_data("private_10001", {"history": [
        {"role": "user", "content": "下午好", "sender_id": "10001",
         "timestamp": copy_start + 60},
        {"role": "assistant",
         "content": "主人，怎么一直不说话呀？是不是被本座准备的甜点馋坏了，"
                    "还是……在偷偷想什么坏主意呢？",
         "speaker": "丛雨", "timestamp": copy_start + 120}]})
    M.mood_mgr.records["private_10001::10001::murasame"]["history"] = [[copy_start + 60, 60]]
    import modules.companion_tasks as ct_mod
    orig_proactive = ct_mod.generate_proactive_text

    async def copying_proactive(ctx_, prompt):
        return ("主人下午好呀，本座刚梦见您呢，怎么一直不说话？"
                "是不是被甜点馋坏了，还是在想什么坏主意呀？")

    ct_mod.generate_proactive_text = copying_proactive
    try:
        asyncio.run(M.write_mood_diaries())
    finally:
        ct_mod.generate_proactive_text = orig_proactive
    check("照抄当天说过的话不算日记，不落档",
          lambda: not M.mood_mgr.has_diary("murasame", copied_day, "private_10001"))
    check("照抄当天台词能被判出来，正常日记不会误判",
          lambda: M._diary_overlap("主人下午好呀，本座刚梦见您呢，怎么一直不说话？"
                                   "是不是被甜点馋坏了，还是在想什么坏主意呀？",
                                   ["丛雨: 主人，怎么一直不说话呀？"
                                    "是不是被本座准备的甜点馋坏了，还是……在偷偷想什么坏主意呢？"]
                                   ) >= M.DIARY_MAX_OVERLAP
          and M._diary_overlap("今天主人一直忙着写代码，想到他答应回来陪我吃草莓蛋糕，"
                               "心情就变好啦。",
                               ["用户1: 我先去写代码了", "丛雨: 本座会乖乖等主人忙完的！"]
                               ) < M.DIARY_MAX_OVERLAP)

    # 补发规则：同一个会话一次只发一篇（跨天补写时会同时有几篇待发），
    # 太旧的日记不再补发（仍留在「心情日记」列表里）
    def diary_sends(session_id):
        return [t for sid, t in fake_sender.not_recorded
                if sid == session_id and t.startswith("【日记】")]

    yday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    today = time.strftime("%Y-%m-%d")
    M.mood_mgr.add_diary("murasame", yday, "昨天补写的日记", session_id="private_10001")
    M.mood_mgr.add_diary("murasame", today, "今天的日记", session_id="private_10001")
    fake_sender.sent.clear()
    fake_sender.not_recorded.clear()
    asyncio.run(M.deliver_mood_diaries())
    check("同一个会话一次只发一篇日记", lambda: len(diary_sends("private_10001")) == 1)
    fake_sender.sent.clear()
    fake_sender.not_recorded.clear()
    asyncio.run(M.deliver_mood_diaries())
    check("剩下的那篇留到下一轮再发", lambda: len(diary_sends("private_10001")) == 1)
    old_day = time.strftime("%Y-%m-%d", time.localtime(time.time() - 5 * 86400))
    M.mood_mgr.add_diary("murasame", old_day, "很久以前的日记", session_id="private_10001")
    fake_sender.sent.clear()
    fake_sender.not_recorded.clear()
    asyncio.run(M.deliver_mood_diaries())
    check("过旧的日记不再补发，但仍留在日记列表里",
          lambda: not diary_sends("private_10001")
          and M.mood_mgr.has_diary("murasame", old_day, "private_10001"))

    # 静默时段内跳过发言
    cfg.config["proactive_quiet_start"] = "00:00"
    cfg.config["proactive_quiet_end"] = "23:59"
    M.affection_mgr.records["murasame"]["10001"]["anniversaries"] = []
    sent_quiet = asyncio.run(M.companion_daily_check())
    check("静默时段不发言也不登记纪念日",
          lambda: sent_quiet == 0
          and M.affection_mgr.get("murasame", "10001")["anniversaries"] == [])
    cfg.config["proactive_quiet_end"] = "00:00"
    jobs_mod.generate_text_reply = old_text_reply
    M.sender = None
    M.app_context.sender = M.sender


def h_proactive_send_guards():
    """主动消息成批发：会话列表去重、成批发送排队、日记注入。"""
    import main as M
    from modules.sender import voice_lock
    from modules.events import EventManager

    section("H 主动消息：目标去重 / 发送排队 / 日记注入")

    work = fresh_dir("probe")
    cfg = M.ConfigLoader(str(work / "probe_cfg.json"))
    cfg.config.update({
        "memory_data_path": str(work / "data"),
        "mood_diary_enabled": True,
        "active_character": "murasame",
        "roles": [{"character_key": "murasame", "character_name": "丛雨",
                   "personality_prompt": "你是丛雨"}],
    })
    cfg.roles = cfg._parse_roles()
    saved = (M.global_config, M.memory_manager, M.mood_mgr)
    M.global_config = cfg
    M.app_context.global_config = M.global_config
    M.memory_manager = M.MemoryManager(cfg)
    M.app_context.memory_manager = M.memory_manager
    M.mood_mgr = M.MoodManager(M.memory_manager.data_path)
    M.app_context.mood_mgr = M.mood_mgr
    try:
        # 同一个群有多个角色的记忆文件时只算一个会话，否则问候会重复发
        M.memory_manager.save_session_data("group_777", {"history": []})
        M.memory_manager.save_session_data("maoniang_group_777", {"history": []})
        M.memory_manager.save_session_data("private_555", {"history": []})
        sessions = M._known_sessions()
        check("已知会话列表按会话去重",
              lambda: sessions.count(("group", "777")) == 1
              and ("private", "555") in sessions)

        sent = []

        class _Sender:
            client = object()

            @contextlib.contextmanager
            def for_session(self, session_id):
                self.last_session = session_id
                yield

            async def speak_and_send(self, stype, sid, text, emotions, ctx, **kw):
                sent.append((stype, sid))
                return True

        mgr = EventManager(cfg, M.memory_manager.data_path, None)
        targets = [{"session_type": "group", "session_id": "777"},
                   {"session_type": "group", "session_id": "777"}]
        asyncio.run(mgr._send_to_targets(_Sender(), targets, "节日快乐", {},
                                         M.get_active_ctx(), {}))
        check("同一批目标里的重复项只发一遍", lambda: sent == [("group", "777")])

        async def probe_batches():
            order = []

            async def batch(name):
                async def body():
                    order.append("start-" + name)
                    await asyncio.sleep(0.05)
                    order.append("end-" + name)
                await M._guarded(body)

            await asyncio.gather(batch("a"), batch("b"))
            return order

        order = asyncio.run(probe_batches())
        check("两批主动消息排队执行，不会同时占用语音",
              lambda: order in (["start-a", "end-a", "start-b", "end-b"],
                                ["start-b", "end-b", "start-a", "end-a"]))

        async def same_lock():
            return voice_lock() is voice_lock()

        check("语音合成排队锁在同一循环内是同一把", lambda: asyncio.run(same_lock()) is True)

        M.mood_mgr.add_diary("murasame", "2026-09-30", "今天和主人一起吃了蛋糕。",
                             session_id="private_555", sent=True)
        note = M.mood_diary_note(M.get_active_ctx())
        check("角色知道自己写过日记，且知道别人看不到",
              lambda: "你自己写的日记" in note and "别人看不到" in note
              and "吃了蛋糕" in note)
        cfg.config["mood_diary_enabled"] = False
        check("关掉心情日记后不注入", lambda: M.mood_diary_note(M.get_active_ctx()) == "")
    finally:
        M.global_config, M.memory_manager, M.mood_mgr = saved
        M.app_context.global_config = M.global_config
        M.app_context.memory_manager = M.memory_manager
        M.app_context.mood_mgr = M.mood_mgr


def i_mood_commit_accuracy():
    """心情落盘：日志打的是存档里的真实变化，且不再一路顶到上限。"""
    import contextlib
    import io as _io
    import main as M
    from modules.mood import MoodManager, commit_mood
    from modules.llm_helpers import RoleContext

    section("I 心情落盘：真实变化 + 回稳")
    work = fresh_dir("moodcommit")
    base = {"reply_judge_mood_min": 0, "reply_judge_mood_max": 100,
            "reply_judge_mood_initial": 60, "reply_judge_mood_delta_max": 15,
            "character_key": "murasame", "mood_enabled": True}

    def ctx_of(**kw):
        cfg = dict(base)
        cfg.update(kw)
        return RoleContext(Cfg(cfg))

    mm = MoodManager(work)
    mm.set_mood("private_1", "murasame", 100)
    verdict = {"mood": 95.0, "mood_base": 100.0, "mood_delta": 0.0,
               "mood_enabled": True, "mood_committed": False}
    buf = _io.StringIO()
    with contextlib.redirect_stdout(buf):
        new_mood = commit_mood(ctx_of(), mm, "private_1", verdict)
        M._log_mood_commit(new_mood, verdict)
    line = buf.getvalue().strip()
    check("变化前打的是存档值，深夜偏置与回稳分开写明",
          lambda: new_mood == 96.0 and line.startswith("心情更新：100 → 96")
          and "偏置" in line and "回稳" in line and "95" in line)

    mm.set_mood("private_2", "murasame", 100)
    for _ in range(3):
        commit_mood(ctx_of(), mm, "private_2",
                    {"mood": 100.0, "mood_base": 100.0, "mood_delta": 10.0,
                     "mood_enabled": True, "mood_committed": False})
    check("一直被夸也不会永远停在满值",
          lambda: mm.get_mood("private_2", "murasame", 60) < 100)

    mm.set_mood("private_3", "murasame", 100)
    commit_mood(ctx_of(mood_regress_rate=0), mm, "private_3",
                {"mood": 100.0, "mood_base": 100.0, "mood_delta": 0.0,
                 "mood_enabled": True, "mood_committed": False})
    check("回稳比例可以关掉（旧行为）",
          lambda: mm.get_mood("private_3", "murasame", 60) == 100)
    check("回稳比例可配且默认开启",
          lambda: M.ConfigLoader.default_config().get("mood_regress_rate") == 0.1
          and "mood_regress_rate" in (ROOT / "webui" / "start.html").read_text(encoding="utf-8"))


def main():
    print("Lovomo 陪伴功能回归测试")
    b_promises()
    c_encounters()
    d_affection_extra()
    e_mood_extra()
    f_defaults_and_rival()
    g_companion_daily_check()
    h_proactive_send_guards()
    i_mood_commit_accuracy()
    print(f"\n{'=' * 70}")
    print(f"结果：通过 {len(PASS)} | 失败 {len(FAIL)}")
    for name, err in FAIL:
        print(f"  - {name}: {err}")
    print(f"{'=' * 70}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
