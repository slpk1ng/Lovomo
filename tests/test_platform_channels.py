# -*- coding: utf-8 -*-
"""接入方式能力差异测试：只能发文字的通道（微信 ClawBot / QQ 官方）。

NapCat 支持语音/表情包/图片，微信 ClawBot 与 QQ 官方机器人只收发文本，
用户 ID 又是长字符串 openid。这里用桩客户端覆盖主动消息、定时任务、
待办提醒、节日/生日问候、心情日记、纪念日/承诺、表情包、语音、识图
这些链路，验证它们发消息时用的是收到消息那条连接，并且不会真的发出
语音/表情包/图片、不会把字符串 ID 当数字处理。

运行: python tests/test_platform_channels.py     （全通过退出码 0）
"""
import asyncio
import base64
import contextlib
import io
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

import main as M
import modules.jobs as jobs_mod
import modules.llm_helpers as LH
import modules.sender as sender_mod
import modules.reply_pipeline
from modules.adapters import (ILinkClient, QQBotClient, client_supports,
                              segment_to_dict, segments_to_text)
from modules.events import EventManager
from modules.sender import MessageSender
from modules.todo_manager import TodoManager

# 微信 ClawBot 与 QQ 官方给出的都是长字符串 ID，不能当数字用
WECHAT_ID = "o9cq7f2XyAb3Kd9Lm0NpQrStUv@im.wechat"
QQ_OPENID = "7B1F2A4C9D0E4F5A8B3C6D7E9F0A1B2C"
QQ_GROUP_OPENID = "A1B2C3D4E5F60718293A4B5C6D7E8F90"

# 1x1 的 PNG，给表情包链路当一张真实存在的图
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")

PASS, FAIL = [], []
TMP_ROOT = ROOT / ".tmp_test" / "platform_channels"


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


def fresh_dir(name: str) -> Path:
    path = TMP_ROOT / name
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# 桩：连接、表情包管理器、数据库
# ---------------------------------------------------------------------------
class _RecordingClient:
    """把收到的消息段原样记下来，供断言"发端到底构造了什么"。"""

    def __init__(self, self_id=""):
        self.self_id = self_id
        self.calls = []

    def _record(self, kind, target, message):
        self.calls.append({
            "kind": kind, "target": target, "text": segments_to_text(message),
            "segment_types": [str(segment_to_dict(s).get("type")) for s in (message or [])],
        })

    async def send_private_msg(self, user_id, message):
        self._record("private", user_id, message)

    async def send_group_msg(self, group_id, message):
        self._record("group", group_id, message)


class StubTextOnlyClient(_RecordingClient):
    """对齐微信 ClawBot / QQ 官方：只收发文本，群聊发送未实现。"""

    capabilities = {"voice": False, "sticker": False, "image": False,
                    "poke": False, "recall": False, "mention": False, "quote": False}
    platform = "wechat_clawbot"

    async def send_group_msg(self, group_id, message):
        raise NotImplementedError("这条接入方式不支持群聊发送")


class StubNapCatClient(_RecordingClient):
    """真实 NapCat 连接没有 capabilities 类属性，任何能力都按支持处理。"""

    def __init__(self, self_id="10000"):
        super().__init__(self_id)


class StubStickerManager:
    """挑表情包：只记次数，返回一张真实存在的图。"""

    def __init__(self, path: Path):
        self.path = path
        self.picks = 0

    async def pick_async(self, ctx=None, emotion: str = "", text: str = ""):
        self.picks += 1
        return self.path


class StubDb:
    def __init__(self):
        self.rows = []

    def execute(self, sql, params=()):
        self.rows.append((sql, params))


def segment_types(client) -> list:
    return [t for call in client.calls for t in call["segment_types"]]


def call_texts(client) -> list:
    return [call["text"] for call in client.calls]


def _prompt_with(caps) -> str:
    cfg = M.ConfigLoader.default_config()
    return LH.build_system_prompt(LH.RoleContext(cfg, {}, caps), {})


def _text_only_prompt_ok() -> bool:
    prompt = _prompt_with(StubTextOnlyClient.capabilities)
    return (all(s not in prompt for s in
                ("【引用消息】", "【@某人】", "【戳一戳】", "【撤回】"))
            and "【这条通道只能发文字】" in prompt
            and "【翻聊天记录】" in prompt)


def _full_prompt_ok() -> bool:
    prompt = _prompt_with(None)
    return (all(s in prompt for s in
                ("【引用消息】", "【@某人】", "【戳一戳】", "【撤回】"))
            and "【这条通道只能发文字】" not in prompt)


# ---------------------------------------------------------------------------
# 运行环境：默认 NapCat 桩 + 已登记会话的微信桩
# ---------------------------------------------------------------------------
class Env:
    def __init__(self, name: str, **config_overrides):
        self.work = fresh_dir(name)
        self.config = M.ConfigLoader(str(self.work / "config.json"))
        self.config.config.update({
            "memory_data_path": str(self.work),
            "ref_audio_root": "",
            "active_character": "murasame",
            "roles": [{"character_key": "murasame", "character_name": "丛雨",
                       "personality_prompt": "你是丛雨"}],
            "proactive_quiet_start": "00:00",
            "proactive_quiet_end": "00:00",
            "send_retry": 0,
            "sticker_output_max_side": 0,
        })
        self.config.config.update(config_overrides)
        self.config.roles = self.config._parse_roles()
        self.session_id = f"private_{WECHAT_ID}"
        self.napcat = StubNapCatClient()
        self.wechat = StubTextOnlyClient("wechat_bot")
        self.memory = M.MemoryManager(self.config)
        self.sender = MessageSender(self.config, self.memory)
        self.sender.client = self.napcat
        self.sender.remember_session(self.session_id, self.wechat)

    def ctx(self):
        return M.RoleContext(self.config.config)

    def sticker_file(self) -> Path:
        path = self.work / "sticker.png"
        if not path.exists():
            path.write_bytes(PNG_BYTES)
        return path


_MAIN_GLOBAL_NAMES = ("global_config", "sender", "memory_manager", "mood_mgr",
                      "affection_mgr", "promise_mgr", "encounter_mgr", "sticker_mgr",
                      "profile_mgr")
_PROACTIVE_NAMES = ("last_user_activity", "last_proactive_sent", "proactive_pending",
                    "proactive_awaiting", "proactive_counts", "last_interaction")


@contextlib.contextmanager
def main_env(env: Env):
    """把主程序的全局换成这套桩，退出时原样还回去。"""
    saved = {n: getattr(M, n, None) for n in _MAIN_GLOBAL_NAMES}
    saved_state = {n: getattr(M, n) for n in _PROACTIVE_NAMES}
    M.global_config = env.config
    M.app_context.global_config = M.global_config
    M.sender = env.sender
    M.app_context.sender = M.sender
    M.memory_manager = env.memory
    M.app_context.memory_manager = M.memory_manager
    M.mood_mgr = None
    M.affection_mgr = None
    M.promise_mgr = None
    M.encounter_mgr = None
    M.sticker_mgr = None
    M.profile_mgr = None
    M.app_context.mood_mgr = M.mood_mgr
    M.app_context.affection_mgr = M.affection_mgr
    M.app_context.promise_mgr = M.promise_mgr
    M.app_context.encounter_mgr = M.encounter_mgr
    M.app_context.sticker_mgr = M.sticker_mgr
    M.app_context.profile_mgr = M.profile_mgr
    for name in _PROACTIVE_NAMES:
        getattr(M, name).clear()
    try:
        yield env
    finally:
        for name, value in saved.items():
            setattr(M, name, value)
        for name, value in saved_state.items():
            current = getattr(M, name)
            current.clear()
            current.update(value)


@contextlib.contextmanager
def no_text_only_gap():
    """文本通道的发送间隔在测试里没必要真的等。"""
    old_gap = sender_mod.TEXT_ONLY_SEND_MIN_GAP
    old_jitter = sender_mod.TEXT_ONLY_SEND_GAP_JITTER
    sender_mod.TEXT_ONLY_SEND_MIN_GAP = 0.0
    sender_mod.TEXT_ONLY_SEND_GAP_JITTER = 0.0
    try:
        yield
    finally:
        sender_mod.TEXT_ONLY_SEND_MIN_GAP = old_gap
        sender_mod.TEXT_ONLY_SEND_GAP_JITTER = old_jitter


@contextlib.contextmanager
def fake_llm(text="（角色台词）"):
    """替掉 LLM 调用，避免测试联网。"""
    old = jobs_mod.generate_text_reply
    calls = []

    async def fake(ctx, system, user_prompt, max_tokens=200, **kwargs):
        calls.append(user_prompt)
        return text

    jobs_mod.generate_text_reply = fake
    try:
        yield calls
    finally:
        jobs_mod.generate_text_reply = old


# ---------------------------------------------------------------------------
# 1 能力声明
# ---------------------------------------------------------------------------
def t1_capabilities():
    section("1 能力声明：只能发文字的通道不声明语音/表情包/图片")
    env = Env("capabilities")
    check("微信 ClawBot 桩：语音/表情包/图片都不支持",
          lambda: not any(client_supports(env.wechat, f)
                          for f in ("voice", "sticker", "image")))
    check("NapCat 桩（无 capabilities 属性）：三项都按支持处理",
          lambda: all(client_supports(env.napcat, f)
                      for f in ("voice", "sticker", "image")))
    check("真实的 ILinkClient / QQBotClient 也不声明这三项能力",
          lambda: not any(client_supports(c, f)
                          for c in (ILinkClient({}), QQBotClient({}))
                          for f in ("voice", "sticker", "image")))
    check("真实的 ILinkClient / QQBotClient 也不声明@/引用/戳一戳/撤回",
          lambda: not any(client_supports(c, f)
                          for c in (ILinkClient({}), QQBotClient({}))
                          for f in ("mention", "quote", "poke", "recall")))
    check("只能发文字的通道：提示词不讲@/引用/戳一戳/撤回，并说明只能发文字",
          lambda: _text_only_prompt_ok())
    check("NapCat 通道：提示词照旧讲这些动作，不出现「只能发文字」",
          lambda: _full_prompt_ok())


# ---------------------------------------------------------------------------
# 2 会话 → 连接：字符串 ID 原样传递
# ---------------------------------------------------------------------------
def t2_for_session_routing():
    section("2 按会话选连接 + 长字符串 ID")
    env = Env("routing")
    with main_env(env), no_text_only_gap():
        ok = asyncio.run(_route_once(env))
    check("for_session 选中的是收到消息那条连接（默认 NapCat 收不到）",
          lambda: ok and len(env.wechat.calls) == 1 and not env.napcat.calls)
    check("目标 ID 原样传下去，没有被 int() 转换",
          lambda: env.wechat.calls[0]["target"] == WECHAT_ID
          and env.wechat.calls[0]["text"] == "你好")
    check("数字 ID 仍按数字发送（NapCat 行为不变）",
          lambda: sender_mod._as_target("10001") == 10001
          and sender_mod._as_target(WECHAT_ID) == WECHAT_ID)
    check("会话 ID 能被正确拆成 (private, openid)",
          lambda: M.parse_session_target(env.session_id) == ("private", WECHAT_ID))
    check("没登记过的会话会回落到默认连接（定时/问候类任务踩的就是这个）",
          lambda: asyncio.run(_route_unregistered(env)) and env.napcat.calls)


async def _route_once(env: Env) -> bool:
    with env.sender.for_session(env.session_id):
        return await env.sender.send_text("private", WECHAT_ID, "你好")


async def _route_unregistered(env: Env) -> bool:
    env.napcat.calls.clear()
    with env.sender.for_session(f"private_{QQ_OPENID}"):
        return await env.sender.send_text("private", QQ_OPENID, "没登记过")


# ---------------------------------------------------------------------------
# 3 语音降级
# ---------------------------------------------------------------------------
def t3_voice_downgrade():
    section("3 语音：只能发文字的通道降级为纯文本，不做语音合成")
    env = Env("voice")
    with main_env(env), no_text_only_gap(), fake_llm():
        synth_calls = _patch_tts_to_fail()
        try:
            reply_result = asyncio.run(_send_reply(env, use_voice=True))
            speak_result = asyncio.run(_speak_and_send(env, use_voice=True))
            sink_ok = asyncio.run(_sink_voice_probe(env))
        finally:
            _restore_tts()
    check("send_reply(use_voice=True)：一次语音合成都没发生，只发出文本",
          lambda: not synth_calls["synthesize"] and reply_result["tts_calls"] == 0
          and reply_result["voice_ok"] is False
          and "record" not in segment_types(env.wechat))
    check("speak_and_send(use_voice=True)：不发语音",
          lambda: speak_result and "record" not in segment_types(env.wechat))
    check("流式 SentenceSink：判定该通道无语音，不调用 TTS 服务",
          lambda: sink_ok is False and not synth_calls["service"])


_saved_tts = {}


def _patch_tts_to_fail():
    calls = {"synthesize": 0, "service": 0}

    async def fake_synthesize(*args, **kwargs):
        calls["synthesize"] += 1
        raise AssertionError("这条接入方式不该去做语音合成")

    async def fake_service(*args, **kwargs):
        calls["service"] += 1
        raise AssertionError("这条接入方式不该去探测 TTS 服务")

    _saved_tts["sender_synthesize"] = sender_mod.synthesize_sentence
    _saved_tts["main_service"] = M.ensure_tts_service
    _saved_tts["rp_service"] = modules.reply_pipeline.ensure_tts_service
    sender_mod.synthesize_sentence = fake_synthesize
    M.ensure_tts_service = fake_service
    modules.reply_pipeline.ensure_tts_service = fake_service
    return calls


def _restore_tts():
    sender_mod.synthesize_sentence = _saved_tts["sender_synthesize"]
    M.ensure_tts_service = _saved_tts["main_service"]
    modules.reply_pipeline.ensure_tts_service = _saved_tts["rp_service"]


async def _send_reply(env: Env, use_voice: bool):
    with env.sender.for_session(env.session_id):
        return await env.sender.send_reply(
            "private", WECHAT_ID,
            [{"zh": "第一句", "display": "第一句", "lang": "第一句", "emotion": "pingjing"}],
            {}, env.ctx(), use_voice=use_voice)


async def _speak_and_send(env: Env, use_voice: bool, sticker: bool = False):
    with env.sender.for_session(env.session_id):
        return await env.sender.speak_and_send(
            "private", WECHAT_ID, "早安呀", {}, env.ctx(),
            use_voice=use_voice, sticker=sticker, session_id=env.session_id)


async def _sink_voice_probe(env: Env):
    sink = M.SentenceSink("private", WECHAT_ID, {}, env.ctx())
    with env.sender.using_client(env.wechat):
        return await sink._tts_available()


# ---------------------------------------------------------------------------
# 4 表情包
# ---------------------------------------------------------------------------
def t4_sticker():
    section("4 表情包：只能发文字的通道应当跳过")
    env = Env("sticker")
    env.sender.sticker_manager = StubStickerManager(env.sticker_file())
    with main_env(env), no_text_only_gap(), fake_llm():
        speak_ok = asyncio.run(_speak_and_send(env, use_voice=False, sticker=True))
        speak_picks = env.sender.sticker_manager.picks
        speak_segments = segment_types(env.wechat)
        env.wechat.calls.clear()

        asyncio.run(_send_text_with_sticker(env))
        send_text_segments = segment_types(env.wechat)
        env.wechat.calls.clear()

        asyncio.run(_send_reply(env, use_voice=False))
        reply_segments = segment_types(env.wechat)
        env.wechat.calls.clear()

        sink_segments = asyncio.run(_sink_sticker_probe(env))
    check("speak_and_send(sticker=True)：直接跳过挑图与发送",
          lambda: speak_ok and speak_picks == 0 and "image" not in speak_segments)
    check("send_text(sticker=...)：不该往只能发文字的通道塞图片段",
          lambda: "image" not in send_text_segments)
    check("send_reply：不该给只能发文字的通道挑表情包",
          lambda: "image" not in reply_segments)
    check("流式 SentenceSink：不该给只能发文字的通道发表情包",
          lambda: "image" not in sink_segments)


async def _send_text_with_sticker(env: Env):
    with env.sender.for_session(env.session_id):
        return await env.sender.send_text("private", WECHAT_ID, "配一张图",
                                          sticker=env.sticker_file())


async def _sink_sticker_probe(env: Env) -> list:
    M.sticker_mgr = env.sender.sticker_manager
    try:
        sink = M.SentenceSink("private", WECHAT_ID, {}, env.ctx())
        with env.sender.using_client(env.wechat):
            await sink.on_sentence({"zh": "一句", "display": "一句",
                                    "lang": "一句", "emotion": "pingjing"})
            await sink.flush()
    finally:
        M.sticker_mgr = None
    return segment_types(env.wechat)


# ---------------------------------------------------------------------------
# 5 待办提醒
# ---------------------------------------------------------------------------
def t5_todo_reminder():
    section("5 待办提醒：按会话选连接")
    env = Env("todo", todo_remind_mode="preset", todo_voice=True)
    with main_env(env), no_text_only_gap():
        db = StubDb()
        mgr = TodoManager(env.config, db, None, sender=env.sender,
                          emotions_provider=lambda: {})
        mgr.ctx_provider = env.ctx
        asyncio.run(mgr.fire_reminder(1, "吃药", "private", env.session_id))
    check("提醒发到微信这条连接（默认 NapCat 收不到）",
          lambda: bool(env.wechat.calls) and not env.napcat.calls)
    check("提醒文本带上了事项本身",
          lambda: any("吃药" in t for t in call_texts(env.wechat)))
    check("配置了语音提醒也不发语音（降级成文字）",
          lambda: "record" not in segment_types(env.wechat))
    check("发送成功后待办标记为完成",
          lambda: any("status='done'" in sql for sql, _p in db.rows))


# ---------------------------------------------------------------------------
# 6 定时任务
# ---------------------------------------------------------------------------
def t6_scheduled_jobs():
    section("6 定时任务：目标会话在微信上时该走微信连接")
    env = Env("jobs", scheduler_enabled=True)
    job = {"id": "morning", "name": "每日早安", "enabled": True,
           "trigger": {"type": "daily", "time": "08:00"},
           "target": {"session_type": "private", "session_id": WECHAT_ID},
           "action": {"mode": "template", "template": "主人早上好呀", "use_voice": False}}
    with main_env(env), no_text_only_gap():
        mgr = jobs_mod.ScheduledJobManager(env.config, env.work, None, sender=env.sender,
                                           ctx_provider=env.ctx, emotions_provider=lambda: {})
        sent = asyncio.run(mgr._run_job(job))
    check("定时任务的问候发到了微信这条连接",
          lambda: sent and any("早上好" in t for t in call_texts(env.wechat)))
    check("定时任务的问候没有落到默认 NapCat 连接上", lambda: not env.napcat.calls)


# ---------------------------------------------------------------------------
# 7 节日 / 生日问候
# ---------------------------------------------------------------------------
def t7_greeting_events():
    section("7 节日问候 / 生日祝福")
    env = Env("events", greeting_events_enabled=True)
    with main_env(env), no_text_only_gap():
        mgr = EventManager(env.config, env.work)
        mgr.events = [{"id": "probe", "name": "节日", "type": "date",
                       "date": time.strftime("%m-%d"), "enabled": True,
                       "mode": "template", "template": "节日快乐",
                       "use_voice": False,
                       "targets": [{"session_type": "private", "session_id": WECHAT_ID}]}]
        mgr.save_events()
        sent = asyncio.run(mgr.check_and_greet(env.sender, env.ctx, lambda: {}))
    check("节日问候发到了微信这条连接",
          lambda: sent and any("节日快乐" in t for t in call_texts(env.wechat)))
    check("节日问候没有落到默认 NapCat 连接上", lambda: not env.napcat.calls)


def t8_birthday_greeting():
    section("8 画像生日祝福")
    env = Env("birthday", birthday_greeting_enabled=True, birthday_greet_mode="template")
    with main_env(env), no_text_only_gap():
        mgr = EventManager(env.config, env.work)
        mgr.profiles = M.UserProfileManager(env.config, env.work)
        mgr.profiles.profiles[WECHAT_ID] = {"birthday": time.strftime("%m-%d"),
                                            "nickname": "小明"}
        sent = asyncio.run(mgr.check_and_greet(env.sender, env.ctx, lambda: {}))
    check("生日祝福发到了微信这条连接",
          lambda: sent and any("小明" in t for t in call_texts(env.wechat)))
    check("生日祝福没有落到默认 NapCat 连接上", lambda: not env.napcat.calls)


# ---------------------------------------------------------------------------
# 9 心情日记
# ---------------------------------------------------------------------------
def t9_mood_diary():
    section("9 心情日记投递")
    env = Env("diary", mood_diary_enabled=True)
    with main_env(env), no_text_only_gap():
        M.mood_mgr = M.MoodManager(env.work)
        M.app_context.mood_mgr = M.mood_mgr
        env.memory.save_session_data(env.session_id, {"history": [
            {"role": "user", "content": "今天好累", "timestamp": time.time()}]})
        M.mood_mgr.add_diary("murasame", time.strftime("%Y-%m-%d"), "今天有点想你",
                             session_id=env.session_id)
        sent = asyncio.run(M.deliver_mood_diaries())
    check("日记发到了微信这条连接",
          lambda: sent == 1 and any("今天有点想你" in t for t in call_texts(env.wechat)))
    check("日记没有落到默认 NapCat 连接上", lambda: not env.napcat.calls)
    check("日记发送不带语音/表情包",
          lambda: not {"record", "image"} & set(segment_types(env.wechat)))


# ---------------------------------------------------------------------------
# 10 纪念日 / 承诺兑现
# ---------------------------------------------------------------------------
def t10_companion_check():
    section("10 每日陪伴检查：纪念日 / 承诺兑现")
    env = Env("companion", affection_enabled=True, affection_anniversary_days="7",
              affection_decay_enabled=False, promise_enabled=False,
              mood_diary_enabled=False)
    now = time.time()
    with main_env(env), no_text_only_gap(), fake_llm("今天是值得纪念的日子"):
        M.affection_mgr = M.AffectionManager(env.config, env.work)
        M.app_context.affection_mgr = M.affection_mgr
        M.affection_mgr.records["murasame"] = {
            M.AffectionManager.scope_key(WECHAT_ID, env.session_id): {
                "score": 95, "partner": True, "partner_since": now - 6 * 86400,
                "updated": now, "day": "", "day_gain": 0, "anniversaries": []}}
        env.memory.save_session_data(env.session_id, {"history": [
            {"role": "user", "content": "嗨", "timestamp": now}]})
        sent = asyncio.run(M.companion_daily_check())
    check("纪念日祝贺发到了微信这条连接",
          lambda: sent == 1 and any("纪念日" in t or "纪念" in t
                                    for t in call_texts(env.wechat)))
    check("纪念日祝贺没有落到默认 NapCat 连接上", lambda: not env.napcat.calls)


def t11_promise_send():
    section("11 承诺兑现发送")
    env = Env("promise")
    with main_env(env), no_text_only_gap(), fake_llm("我这就讲给你听"):
        env.memory.save_session_data(env.session_id, {"history": [
            {"role": "user", "content": "嗨", "timestamp": time.time()}]})
        role = env.config.roles["murasame"]
        ok = asyncio.run(M._companion_send(env.session_id, role, "兑现你答应过的事"))
    check("承诺兑现发到了微信这条连接",
          lambda: ok and any("讲给你听" in t for t in call_texts(env.wechat)))
    check("承诺兑现没有落到默认 NapCat 连接上", lambda: not env.napcat.calls)


# ---------------------------------------------------------------------------
# 12 主动消息
# ---------------------------------------------------------------------------
def t12_proactive():
    section("12 主动消息（闲置搭话）")
    env = Env("proactive", proactive_enabled=True, proactive_idle_minutes=1,
              proactive_max_per_day=2, proactive_idle_jitter="",
              proactive_wait_reply=True, proactive_voice=False, proactive_sticker=False)
    with main_env(env), no_text_only_gap(), fake_llm("在忙吗"):
        env.memory.save_session_data(env.session_id, {"history": [
            {"role": "user", "content": "在", "timestamp": time.time() - 600}]})
        M.last_user_activity[env.session_id] = time.time() - 600
        asyncio.run(M.proactive_idle_check())    # 第一次：排期
        asyncio.run(M.proactive_idle_check())    # 第二次：到点发送
    check("主动消息发到了微信这条连接",
          lambda: any("在忙吗" in t for t in call_texts(env.wechat)))
    check("主动消息没有落到默认 NapCat 连接上", lambda: not env.napcat.calls)
    check("主动消息不带语音/表情包",
          lambda: not {"record", "image"} & set(segment_types(env.wechat)))


# ---------------------------------------------------------------------------
# 13 识图：文本通道上图片不会进入视觉链路
# ---------------------------------------------------------------------------
def t13_incoming_image():
    section("13 用户发图片（文本通道）")

    def probe():
        ilink = ILinkClient({"bot_id": "bot"})
        event = ilink.to_event({
            "from_user_id": WECHAT_ID, "msg_id": "m1",
            "item_list": [{"type": 1, "text_item": {"text": "看看这张"}},
                          {"type": 2, "image_item": {"url": "https://example.invalid/a.png"}}]})
        with main_env(Env("image")):
            info = M._extract_event_info(event, ilink)
        return info

    info = probe()
    check("微信消息里的图片不会变成图片段（不会触发识图，也不报错）",
          lambda: info is not None and info["has_image"] is False
          and "看看这张" in info["text"])
    check("微信会话键是 private_ + 长字符串 ID",
          lambda: info["session_id"] == f"private_{WECHAT_ID}")


def t14_qq_official_events():
    section("14 QQ 官方机器人事件")
    client = QQBotClient({"app_id": "102000001"})
    c2c = client.to_event("C2C_MESSAGE_CREATE",
                          {"user_openid": QQ_OPENID, "content": "你好", "id": "m2"})
    group = client.to_event("GROUP_AT_MESSAGE_CREATE",
                            {"group_openid": QQ_GROUP_OPENID,
                             "author": {"member_openid": QQ_OPENID},
                             "content": "[@机器人] 在吗", "id": "m3"})
    with main_env(Env("qq_official")):
        c2c_info = M._extract_event_info(c2c, client)
        group_info = M._extract_event_info(group, client)
    check("单聊事件按 openid 建会话，不产生图片段",
          lambda: c2c_info["session_id"] == f"private_{QQ_OPENID}"
          and c2c_info["has_image"] is False)
    check("群 @ 事件按 group_openid 建会话并识别出被 @",
          lambda: group_info["session_id"] == f"group_{QQ_GROUP_OPENID}"
          and group_info["at_bot"] is True)


# ---------------------------------------------------------------------------
# 15 学习 / 画像 / 心情好感：与接入方式无关
# ---------------------------------------------------------------------------
def t15_platform_agnostic():
    section("15 自主学习 / 画像 / 心情好感")
    from modules.affection import AffectionManager
    from modules.lexicon import LexiconManager

    env = Env("agnostic")
    lexicon = LexiconManager({"learning_enabled": True},
                             fresh_dir("agnostic_lexicon"))
    affection = AffectionManager(env.config, env.work)
    mood = M.MoodManager(env.work)
    mood.set_mood(env.session_id, "murasame", 66, user_id=WECHAT_ID)
    check("黑话词典只管配置与数据目录，不需要任何连接",
          lambda: lexicon is not None and lexicon.terms == {})
    check("好感按长字符串用户 ID 正常读写",
          lambda: affection.state("murasame", WECHAT_ID, env.session_id)["score"] == 0
          and affection.get("murasame", WECHAT_ID, env.session_id)["score"] == 0)
    check("心情按长字符串用户 ID 正常读写",
          lambda: mood.get_mood(env.session_id, "murasame", 0, user_id=WECHAT_ID) == 66)


# ---------------------------------------------------------------------------
# 16 复述：真实适配器只发文本
# ---------------------------------------------------------------------------
def t16_adapter_text_only():
    section("16 适配器发送端只认文本")
    check("ILinkClient 把消息段拼成纯文本，图片/语音段被丢弃",
          lambda: segments_to_text(_mixed_segments()) == "正文")
    check("QQBotClient 的发送方法只取文本，不构造语音/图片段",
          lambda: all("segments_to_text" in src and "Record" not in src
                      and "Image" not in src
                      for src in (_method_source(QQBotClient.send_private_msg),
                                  _method_source(QQBotClient.send_group_msg))))


def _mixed_segments():
    from napcat import Image, Record, Text
    return [Text(text="正文"), Image(file="a.png"), Record(file="a.wav")]


def _method_source(fn) -> str:
    import inspect
    return inspect.getsource(fn)


def main():
    print("=" * 70)
    print("接入方式能力差异：只能发文字的通道（微信 ClawBot / QQ 官方）")
    print("=" * 70)
    if TMP_ROOT.exists():
        shutil.rmtree(TMP_ROOT, ignore_errors=True)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    for fn in (t1_capabilities, t2_for_session_routing, t3_voice_downgrade,
               t4_sticker, t5_todo_reminder, t6_scheduled_jobs, t7_greeting_events,
               t8_birthday_greeting, t9_mood_diary, t10_companion_check,
               t11_promise_send, t12_proactive, t13_incoming_image,
               t14_qq_official_events, t15_platform_agnostic, t16_adapter_text_only):
        try:
            fn()
        except Exception as e:
            FAIL.append((fn.__name__, repr(e)))
            print(f"  [FAIL] {fn.__name__} 执行异常: {type(e).__name__}: {e}")
    print(f"\n结果：通过 {len(PASS)} | 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：")
        for name, reason in FAIL:
            print(f"  - {name}: {reason}")
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
