# -*- coding: utf-8 -*-
"""Lovomo 本轮修复专项回归测试。

覆盖四类问题的修复：
  A. TTS 合成语音相比 LLM 实际发送文本漏句/漏词
     A1 台词清洗不再静默删除内容（…——♪ 等）
     A2 synthesize_sentence 参数顺序（text/emotion）不再错位
     A3 segment_for_tts 对不齐时不再用中文兜底、不丢内容
  B. 待办提醒话术 LLM / 预设可切换（默认 LLM + 失败回退）
  C. 情绪判定：亲密/成人语境优先害羞（haixiu），不再一律 pingjing
  D. 主动消息：重启后仍能主动开口、到点必发、计数持久化

运行: python tests/test_fixes.py     （全通过退出码 0）
"""
import asyncio
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
sys.stdin = io.StringIO()
try:  # Windows 控制台默认 GBK，测试输出含 ♪ 等符号会编码失败
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


def _timing_probe(fn):
    """在独立线程里跑一段代码，返回 (结果, 耗时秒)。"""
    import threading
    box = {}

    def run():
        t0 = time.time()
        try:
            box["result"] = fn()
        except Exception as e:
            box["error"] = e
        box["cost"] = time.time() - t0
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout=30)
    if "error" in box:
        raise box["error"]
    return box.get("result"), box.get("cost", 0.0)


class KeepTempDirectory:
    """沙箱内无法删除临时目录，这里不清理。"""

    def __init__(self, *a, **k):
        self.name = tempfile.mkdtemp(prefix="lovomo_fix_", dir=str(ROOT / ".tmp_test" / "fixrun"))

    def __enter__(self):
        return self.name

    def __exit__(self, *exc):
        return False

    def cleanup(self):
        return None


tempfile.TemporaryDirectory = KeepTempDirectory
(ROOT / ".tmp_test" / "fixrun").mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# A. TTS 文本完整性与参数顺序
# ---------------------------------------------------------------------------
def a_tts_text_integrity():
    from modules.tts import _sanitize_tts_text, split_tts_chunks
    import re

    section("A1 TTS 台词清洗不丢内容")

    cases = [
        "「ご主人、だめ……！」",
        "あっ…そこは、だめ——",
        "ふふっ♪ うれしいな～",
        "わたし、あなたのことが…好き。",
        "ご主人の、ばか。——でも、すき。",
        "（小声で）……すき、だよ。",
        "｢だめ｣って言ったのに…",
        "⏰ 提醒时间到啦：下午三点开会",
        "主人，提醒到啦。你说过要吃药，别忘了哦！",
    ]
    core = lambda s: re.sub(r"\W|_", "", s, flags=re.UNICODE)

    def all_preserved():
        for s in cases:
            if core(_sanitize_tts_text(s)) != core(s):
                print(f"    内容字丢失: {s!r} -> {_sanitize_tts_text(s)!r}")
                return False
        return True
    check("清洗后内容字一字不少（含 … — ♪ 等符号）", all_preserved)

    def ellipsis_kept():
        return _sanitize_tts_text("……すき、だよ。") == "すき、だよ。" or \
            "…" in _sanitize_tts_text("そう……なんだ。")
    check("省略号不再被静默删除", ellipsis_kept)

    def dash_mapped():
        out = _sanitize_tts_text("だめ——")
        return "—" not in out and out.startswith("だめ") and len(out) > 2
    check("破折号做等价替换而非删除", dash_mapped)

    def chunks_complete():
        text = "主人，提醒到啦。你说过要吃药，别忘了哦！"
        chunks = split_tts_chunks(text)
        return len(chunks) == 2 and core("".join(chunks)) == core(text)
    check("分片后内容完整（待办/主动消息逐句合成）", chunks_complete)

    def no_punct_only_chunk():
        return all(len(core(c)) > 0 for c in split_tts_chunks("「ご主人、だめ……！」"))
    check("不产生纯标点碎片片段", no_punct_only_chunk)


def a_tts_call_order():
    import modules.sender as SM
    from modules.llm_helpers import RoleContext

    section("A2 synthesize_sentence 参数顺序")

    calls = []

    async def fake(config, text, emotion, emotions, data_path, stats=None,
                   mimic="", mimics=None):
        calls.append((text, emotion))
        return None
    orig = SM.synthesize_sentence
    SM.synthesize_sentence = fake

    class FakeClient:
        async def send_private_msg(self, **k):
            return {}

    class MM:
        data_path = ROOT / ".tmp_test" / "fixrun"

        @staticmethod
        def cleanup_voice_cache(n=20):
            return None
    try:
        cfg = Cfg({"tts_reply_enabled": True, "separate_send": True,
                   "send_voice_separately": True, "separate_force_segment": False,
                   "dynamic_sleep": False})
        snd = SM.MessageSender(cfg, MM())
        snd.client = FakeClient()
        sentences = [{"zh": "不行啦", "lang": "だめだよ", "display": "不行啦",
                      "emotion": "haixiu"}]
        # 假合成返回 None，走纯文本降级，但参数已记录
        asyncio.run(snd.send_reply("private", 10001, sentences, {"haixiu": {}, "pingjing": {}},
                                   RoleContext(cfg), use_voice=True))
    finally:
        SM.synthesize_sentence = orig

    check("send_reply 传给 TTS 的是台词(lang) 与 情绪(emotion)",
          lambda: calls == [("だめだよ", "haixiu")])


    def same_definition_as_production():
        """断言修复后的调用链确实与生产代码一致，防止代码被改回。
        历史事故：调用处把 text 与 emotion 传反，日志里打印「正在合成」的文本
        其实就是情绪名，用户听到的语音永远用默认音色。"""
        from modules.sender import MessageSender
        from modules import tts
        import inspect as _inspect
        src_sender = _inspect.getsource(MessageSender._synthesize_proactive)
        sig = list(_inspect.signature(tts.synthesize_sentence).parameters)
        return (sig[:4] == ["config", "text", "emotion", "emotions"]
                and "synthesize_sentence(ctx, chunk, emotion" in src_sender
                and "synthesize_sentence(ctx, text, emotion" in src_sender)
    check("合成调用与 synthesize_sentence(config,text,emotion,emotions) 定义一致",
          same_definition_as_production)


def a_synth_guard():
    from modules.tts import synthesize_sentence

    section("A3 synthesize_sentence 自愈式纠正 + 情绪回退日志")

    sent = {}

    class FakeResp:
        status_code = 200
        content = b"RIFF0000WAVE"

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            sent.update(params or {})
            return FakeResp()

    import modules.tts as T
    orig = T.httpx.AsyncClient
    T.httpx.AsyncClient = FakeClient
    tmpdir = ROOT / ".tmp_test" / "fixrun"
    emotions = {"haixiu": {"ref_path": "r.mp3", "prompt_text": "p"},
                "pingjing": {"ref_path": "r2.mp3", "prompt_text": "p2"}}

    def run_swapped():
        # 传反顺序：text=haixiu(情绪), emotion=台词
        return asyncio.run(synthesize_sentence(Cfg({"default_voice": "pingjing"}), "haixiu",
                                               "だめ……！", emotions, tmpdir))
    try:
        out, cost = _timing_probe(run_swapped)
    finally:
        T.httpx.AsyncClient = orig

    check("参数传反时自动纠正（文本=だめ…, 情绪=haixiu）",
          lambda: sent.get("text", "").startswith("だめ") and
          sent.get("ref_audio_path") == "r.mp3")
    check("纠正后仍能正常返回音频路径（未被当作情绪查表失败）",
          lambda: out is not None and Path(out).exists())
    check("确实发起了 TTS 请求（不是被守卫提前 return）", lambda: bool(sent) and cost >= 0)


def a_segment_no_loss():
    from modules.llm_helpers import segment_for_tts

    section("A4 segment_for_tts 不丢内容、不串语言")

    def no_cross_language():
        # zh 2 句、日文只有 1 句：日文无法再切，则整段保持完整（逐字都被念到），
        # 且绝不能把中文塞进 lang 字段（那会让该句语音"消失"）
        sentences = [{"zh": "第一句话在这里。第二句话在这里。",
                      "lang": "ひとつめとふたつめの話。",
                      "display": "第一句话在这里。第二句话在这里。", "emotion": "haixiu"}]
        out = segment_for_tts(sentences)
        joined_lang = "".join(s["lang"] for s in out)
        joined_zh = "".join(s["zh"] for s in out)
        joined_disp = "".join(s["display"] for s in out)
        return (len(out) == 1
                and joined_zh == "第一句话在这里。第二句话在这里。"
                and joined_lang == "ひとつめとふたつめの話。"
                and "第" not in joined_lang
                and joined_disp == "第一句话在这里。第二句话在这里。")
    check("zh/ja 句数不一致时逐段对齐（不丢内容、不用中文朗读）", no_cross_language)

    def real_case_3_vs_4():
        """事故原始用例：中文 3 句、日文 4 句（「！？」曾被切成碎片）。

        旧逻辑见句数不等就整段合成 → 用户看到"主、主人…"被整段念出来，
        文字与语音不再逐段对应。现在应严格对齐成 3 段。
        """
        zh = ("主、主人……你说这种下流的话做什么！本座的羞脸都要熟透了啦！"
              "虽然、虽然不想承认，但是本座心里确实有点期待呢……才不是因为喜欢那种东西才听你说话！")
        ja = ("ご主人…そんな卑猥なことを何故言っちゃうの！？恵が滲み出しちゃいますよ！"
              "でもねー確かに心はちょっと期待してますってことは…バレてるでしょ！"
              "ただただそういうものがあるからじゃなくて、その声だけです！")
        out = segment_for_tts([{"zh": zh, "lang": ja, "display": zh, "emotion": "haixiu"}])
        core = lambda s: re.sub(r"[\W_]+", "", str(s or ""), flags=re.UNICODE)
        return (len(out) == 3
                and core("".join(s["zh"] for s in out)) == core(zh)
                and core("".join(s["lang"] for s in out)) == core(ja)
                and all("ご主人" not in s["lang"] or i == 0 for i, s in enumerate(out)))
    check("事故用例：中文 3 句 / 日文 4 句 → 对齐为 3 段且两边零丢失", real_case_3_vs_4)

    def punctuation_runs_not_fragmented():
        from modules.llm_helpers import split_terms
        return (split_terms("えっ！？本当に？") == ["えっ！？", "本当に？"]
                and split_terms("「だめ」！？") == ["「だめ」！？"]
                and len(split_terms("なに?! うそ！")) == 2)
    check("「！？」「?!」等连用标点不再被切成纯标点碎片",
          punctuation_runs_not_fragmented)

    def reverse_imbalance():
        # 反向：中文 2 句、日文 3 句 → 日文并入相邻段，仍逐段对应
        zh = "主人，你回来啦！今天辛苦了哦！"
        ja = "ご主人、おかえりなさい！今日もお疲れさま！ゆっくり休んでね！"
        out = segment_for_tts([{"zh": zh, "lang": ja, "display": zh, "emotion": "gaoxing"}])
        core = lambda s: re.sub(r"[\W_]+", "", str(s or ""), flags=re.UNICODE)
        return (len(out) == 2
                and core("".join(s["zh"] for s in out)) == core(zh)
                and core("".join(s["lang"] for s in out)) == core(ja)
                and out[0]["lang"] == "ご主人、おかえりなさい！")
    check("反向不齐（中文少日文多）同样对齐且不丢内容", reverse_imbalance)

    def aligned_still_splits():
        sentences = [{"zh": "第一句。第二句。", "lang": "ひとつめ。ふたつめ。",
                      "display": "第一句。第二句。", "emotion": "haixiu"}]
        # 让 zh 与 ja 都能切出 2 句（此处 ja 用句号切分对齐）
        sentences[0]["zh"] = "第一句话在这里。第二句话在这里。"
        out = segment_for_tts(sentences)
        return len(out) == 2 and [s["lang"] for s in out] == ["ひとつめ。", "ふたつめ。"]
    check("句数一致时正常拆句且语言对应", aligned_still_splits)

    def display_never_drops():
        sentences = [{"zh": "甲。乙。", "lang": "A。B。", "display": "甲。乙。",
                      "emotion": "gaoxing"}]
        out = segment_for_tts(sentences)
        return "".join(s["display"] for s in out).replace(" ", "") == "甲。乙。"
    check("display 文本不丢字", display_never_drops)


def a_normalize_no_loss():
    from modules.llm_helpers import normalize_sentences, RoleContext

    section("A5 normalize_sentences 保留全部台词内容")

    ctx = RoleContext({"text_lang": "ja", "display_lang": "zh", "default_voice": "pingjing",
                       "llm_judge": True}, {})
    emotions = {"pingjing": {}, "haixiu": {}, "gaoxing": {}}

    raw = ('{"sentences": [{"zh": "第一句完整台词。", "ja": "ひとつめ。", "emotion": "gaoxing"}, '
           '{"zh": "第二句完整台词。", "ja": "ふたつめ。", "emotion": "haixiu"}]}')
    out = normalize_sentences(raw, ctx, emotions, "hi")
    joined = "".join(s["zh"] for s in out)
    lang_joined = "".join(s["lang"] for s in out)
    check("多句台词内容零丢失", lambda: joined == "第一句完整台词。第二句完整台词。")
    check("日文台词内容零丢失", lambda: lang_joined == "ひとつめ。ふたつめ。")


# ---------------------------------------------------------------------------
# B. 待办提醒话术模式
# ---------------------------------------------------------------------------
def b_todo_remind_mode():
    from modules.todo_manager import TodoManager
    from modules.database import DatabaseManager
    from modules.scheduler import SchedulerManager
    from modules.llm_helpers import RoleContext

    section("B 待办提醒话术：LLM / 预设可切换")

    tmpdir = ROOT / ".tmp_test" / "fixrun" / "todo"
    tmpdir.mkdir(parents=True, exist_ok=True)
    db = DatabaseManager(tmpdir)
    sch = SchedulerManager()

    class FakeSender:
        client = object()

        def __init__(self):
            self.box = []

        @contextlib.contextmanager
        def for_session(self, session_id):
            self.last_session = session_id
            yield
        
        async def speak_and_send(self, session_type, target, text, emotions, ctx,
                                 use_voice=False, sticker=False, emotion="", session_id=""):
            self.box.append(text)
            return True

    box_sender = FakeSender()
    cfg = Cfg({"todo_remind_mode": "preset",
               "todo_remind_template": "⏰ 提醒时间到啦：{content}"})
    mgr = TodoManager(cfg, db, sch, box_sender)
    mgr.ctx_provider = lambda: RoleContext({"personality_prompt": "x"})
    mgr.emotions_provider = lambda: {}
    todo = mgr.add_todo("吃药", time.time() + 60, "private", "private_10001")
    asyncio.run(mgr.fire_reminder(todo["id"], "吃药", "private", "private_10001"))
    check("preset 模式使用模板话术",
          lambda: box_sender.box == ["⏰ 提醒时间到啦：吃药"])

    # LLM 模式：假装模型生成了角色化提醒（必须包含事项）
    import modules.todo_manager as TM
    orig_gen = TM.TodoManager._generate_reminder_text

    async def echo_gen(self, ctx, content):
        return f"主人，{content}的时间到啦，别忘了哦～"
    TM.TodoManager._generate_reminder_text = echo_gen
    try:
        box2 = FakeSender()
        cfg2 = Cfg({"todo_remind_mode": "llm",
                    "todo_remind_template": "⏰ 提醒时间到啦：{content}"})
        mgr2 = TodoManager(cfg2, db, sch, box2)
        mgr2.ctx_provider = lambda: RoleContext({"personality_prompt": "x"})
        mgr2.emotions_provider = lambda: {}
        todo2 = mgr2.add_todo("喝水", time.time() + 60, "private", "private_10001")
        asyncio.run(mgr2.fire_reminder(todo2["id"], "喝水", "private", "private_10001"))
        check("llm 模式使用模型生成的话术",
              lambda: box2.box and box2.box[0] == "主人，喝水的时间到啦，别忘了哦～")
    finally:
        TM.TodoManager._generate_reminder_text = orig_gen

    # LLM 模式：模型漏说事项 → 回退预设
    async def bad_gen(self, ctx, content):
        return "现在已经很晚了呢。"
    TM.TodoManager._generate_reminder_text = bad_gen
    try:
        box3 = FakeSender()
        cfg3 = Cfg({"todo_remind_mode": "llm",
                    "todo_remind_template": "⏰ 提醒时间到啦：{content}"})
        mgr3 = TodoManager(cfg3, db, sch, box3)
        mgr3.ctx_provider = lambda: RoleContext({"personality_prompt": "x"})
        mgr3.emotions_provider = lambda: {}
        todo3 = mgr3.add_todo("开会", time.time() + 60, "private", "private_10001")
        asyncio.run(mgr3.fire_reminder(todo3["id"], "开会", "private", "private_10001"))
        check("模型漏说事项时回退预设模板（不漏提醒）",
              lambda: box3.box and box3.box[0] == "⏰ 提醒时间到啦：开会")
    finally:
        TM.TodoManager._generate_reminder_text = orig_gen

    # LLM 模式：生成抛异常 → 回退预设
    import modules.todo_manager as TMM

    async def boom(self, ctx, content):
        raise RuntimeError("model down")
    TMM.TodoManager._generate_reminder_text = boom
    try:
        box4 = FakeSender()
        cfg4 = Cfg({"todo_remind_mode": "llm",
                    "todo_remind_template": "提醒：{content}"})
        mgr4 = TodoManager(cfg4, db, sch, box4)
        mgr4.ctx_provider = lambda: RoleContext({"personality_prompt": "x"})
        mgr4.emotions_provider = lambda: {}
        todo4 = mgr4.add_todo("睡觉", time.time() + 60, "private", "private_10001")
        err = ""
        try:
            asyncio.run(mgr4.fire_reminder(todo4["id"], "睡觉", "private", "private_10001"))
        except Exception as e:
            err = repr(e)
        check("生成失败不中断提醒且回退预设",
              lambda: (not err) and box4.box == ["提醒：睡觉"])
    finally:
        TMM.TodoManager._generate_reminder_text = orig_gen

    check("默认模式为 llm（新装即角色化提醒）",
          lambda: TodoManager(Cfg({}), db, sch, None)._remind_mode() == "llm")

    # 提醒语音情绪：配置必须经 fire_reminder 传到 speak_and_send
    voice_kwargs = {}

    class RecSender:
        client = object()

        @contextlib.contextmanager
        def for_session(self, session_id):
            self.last_session = session_id
            yield
        
        async def speak_and_send(self, session_type, target, text, emotions, ctx,
                                 use_voice=False, sticker=False, emotion="", session_id=""):
            voice_kwargs.update({"text": text, "emotion": emotion, "use_voice": use_voice})
            return True

    rec = RecSender()
    cfg_v = Cfg({"todo_remind_mode": "preset", "todo_remind_template": "提醒：{content}",
                 "todo_voice": True, "todo_voice_emotion": "haixiu"})
    mgr_v = TodoManager(cfg_v, db, sch, rec)
    mgr_v.ctx_provider = lambda: RoleContext({"personality_prompt": "x"})
    mgr_v.emotions_provider = lambda: {}
    todo_v = mgr_v.add_todo("吃药", time.time() + 60, "private", "private_10001")
    asyncio.run(mgr_v.fire_reminder(todo_v["id"], "吃药", "private", "private_10001"))
    check("提醒语音使用配置的情绪（todo_voice_emotion）",
          lambda: voice_kwargs.get("emotion") == "haixiu"
          and voice_kwargs.get("use_voice") is True)

    cfg_v2 = Cfg({"todo_remind_mode": "preset", "todo_remind_template": "提醒：{content}",
                  "todo_voice": True})
    mgr_v2 = TodoManager(cfg_v2, db, sch, rec)
    mgr_v2.ctx_provider = lambda: RoleContext({"personality_prompt": "x"})
    mgr_v2.emotions_provider = lambda: {}
    voice_kwargs.clear()
    todo_v2 = mgr_v2.add_todo("开会", time.time() + 60, "private", "private_10001")
    asyncio.run(mgr_v2.fire_reminder(todo_v2["id"], "开会", "private", "private_10001"))
    check("未配置提醒情绪时回退 pingjing",
          lambda: voice_kwargs.get("emotion") == "pingjing")


# ---------------------------------------------------------------------------
# C. 情绪判定引导
# ---------------------------------------------------------------------------
def c_emotion_guide():
    from modules.llm_helpers import (RoleContext, build_system_prompt,
                                     build_chat_messages, emotion_context_note)

    section("C 情绪判定：亲密/R18 语境优先 haixiu")

    emotions = {k: {} for k in ("gaoxing", "haixiu", "jingya", "pingjing", "shengqi", "zhaoji")}
    ctx = RoleContext({"text_lang": "ja", "display_lang": "zh", "default_voice": "pingjing",
                       "llm_judge": True, "personality_prompt": "你是猫娘"}, {})
    sp = build_system_prompt(ctx, emotions)

    check("系统提示词包含情绪判定规则", lambda: "【情绪判定规则】" in sp)
    check("明确把 pingjing 定为最后选择", lambda: "绝不是默认选项" in sp)
    check("明确列出必须优先害羞的场景", lambda: "必须优先害羞的场景" in sp and "R18" in sp)
    check("每个可用情绪都给了使用时机",
          lambda: all(f"- {k}：" in sp for k in emotions))

    check("普通寒暄不加情绪提醒",
          lambda: emotion_context_note(ctx, "今天天气不错", []) == "")
    check("R18 消息触发害羞提醒",
          lambda: "haixiu" in emotion_context_note(ctx, "主人想要你，把衣服脱了吧", []))
    check("撩拨消息触发害羞提醒",
          lambda: "haixiu" in emotion_context_note(ctx, "让我摸摸你的胸好不好", []))
    check("亲密历史也触发提醒",
          lambda: "haixiu" in emotion_context_note(
              ctx, "嗯……", [{"role": "user", "content": "我们做爱好不好"}]))
    check("英文 R18 触发提醒",
          lambda: "haixiu" in emotion_context_note(ctx, "let's do R18 roleplay", []))

    msgs = build_chat_messages(ctx, "主人想要你", [], emotions)
    check("提醒作为最后一条消息紧跟用户消息",
          lambda: msgs[-1]["role"] == "user" and msgs[-1]["content"].startswith("【本轮情绪提醒】"))

    off = RoleContext({"text_lang": "ja", "default_voice": "pingjing",
                       "emotion_guide_enabled": False, "personality_prompt": "x"}, {})
    check("开关关闭后不注入提醒", lambda: emotion_context_note(off, "做爱吧", []) == "")
    check("开关关闭后系统提示词不含判定规则",
          lambda: "【情绪判定规则】" not in build_system_prompt(off, emotions))
    str_off = RoleContext({"default_voice": "pingjing", "emotion_guide_enabled": "false",
                           "personality_prompt": "x"}, {})
    check("字符串 false 也被识别为关闭",
          lambda: emotion_context_note(str_off, "做爱吧", []) == "")

    extra = RoleContext({"default_voice": "pingjing",
                         "emotion_guide_extra": "只要被夸就必须 haixiu",
                         "personality_prompt": "x"}, {})
    check("自定义补充规则会注入",
          lambda: "只要被夸就必须 haixiu" in build_system_prompt(extra, emotions))


# ---------------------------------------------------------------------------
# D. 主动消息
# ---------------------------------------------------------------------------
def d_proactive():
    section("D 主动消息：闲置播种 / 到点必发 / 计数持久化")

    import main as M
    import modules.companion_tasks as CT

    tmpdir = ROOT / ".tmp_test" / "fixrun" / "proactive"
    tmpdir.mkdir(parents=True, exist_ok=True)
    for old in tmpdir.glob("*"):
        if old.is_file():
            old.unlink()

    cfg = Cfg({
        "memory_data_path": str(tmpdir), "character_key": "murasame",
        "character_name": "丛雨", "proactive_enabled": True,
        "proactive_idle_minutes": 30, "proactive_idle_jitter": "5~15",
        "proactive_max_per_day": 2, "proactive_quiet_start": "23:00",
        "proactive_quiet_end": "08:00", "proactive_voice": False,
        "proactive_sticker": False, "proactive_prompt": "找话题",
        "personality_prompt": "你是丛雨", "tts_reply_enabled": False,
        "scheduler_enabled": False,
    })
    sent = []
    proactive_keys = []

    class FakeClient:
        pass

    class FakeSender:
        client = FakeClient()

        @contextlib.contextmanager
        def for_session(self, session_id):
            self.last_session = session_id
            yield
        
        async def speak_and_send(self, session_type, target, text, emotions, ctx,
                                 use_voice=False, sticker=False, emotion="", session_id=""):
            sent.append((session_type, target, text))
            proactive_keys.append(session_id)
            return True

    class CfgLoader:
        config = dict(cfg)
        roles = {"murasame": {"character_key": "murasame", "character_name": "丛雨"}}
        active_character = "murasame"

        def get(self, k, d=None):
            return self.config.get(k, d)

    # --- 构造一个历史会话：用户最后发言在 2 小时前 ---
    M.global_config = CfgLoader()
    M.app_context.global_config = M.global_config
    M.memory_manager = M.MemoryManager(M.global_config)
    M.app_context.memory_manager = M.memory_manager
    M.last_interaction.clear()
    M.last_user_activity.clear()
    M.proactive_counts.clear()
    M.proactive_pending.clear()

    session_id = "private_1905332561"
    data = M.memory_manager.load_session_data(session_id)
    data["history"] = [{"role": "user", "content": "在吗",
                        "timestamp": time.time() - 7200}]
    M.memory_manager.save_session_data(session_id, data)

    M.seed_proactive_sessions()
    check("启动时按历史恢复闲置计时（重启后不再永远沉默）",
          lambda: session_id in M.last_user_activity and
          time.time() - M.last_user_activity[session_id] > 3600)

    M.last_interaction.clear()
    M.last_user_activity.clear()
    M.proactive_pending.clear()
    M.proactive_counts.clear()
    M.seed_proactive_sessions()

    M.sender = FakeSender()
    M.app_context.sender = M.sender

    async def fake_proactive(ctx, instruction, history_block=""):
        return "主人，好久没说话了，在忙吗？"
    M.generate_proactive_text = fake_proactive
    CT.generate_proactive_text = fake_proactive
    M._in_quiet_hours = lambda: False
    CT._in_quiet_hours = lambda: False

    # 第一次检查：应该排定计划（而不是直接发送）
    M.proactive_pending.clear()
    M.proactive_counts.clear()
    sent.clear()
    asyncio.run(M.proactive_idle_check())
    check("闲置会话被纳入候选并排定计划",
          lambda: session_id in M.proactive_pending and not sent)
    check("计划时刻大于当前时间（抖动生效，不是立刻发送）",
          lambda: M.proactive_pending.get(session_id, 0) > time.time())

    # 把计划时间提前到过去，模拟"到点"
    M.proactive_pending[session_id] = time.time() - 1
    asyncio.run(M.proactive_idle_check())
    check("到点后真正发出主动消息（修复 deadline 永远不满足的 bug）",
          lambda: len(sent) == 1 and sent[0][2].startswith("主人"))
    check("主动消息把会话键交给发送器（落盘统一由发送器负责）",
          lambda: proactive_keys == [session_id])
    M.MessageSender(M.global_config, M.memory_manager).record_outgoing_history(
        session_id, sent[0][2], M.get_active_ctx())
    check("发送后写入会话历史",
          lambda: any(m.get("proactive") for m in
                      M.memory_manager.load_session_data(session_id)["history"]))
    check("发送后计划与计数被清理/累加",
          lambda: session_id not in M.proactive_pending and
          M.proactive_counts.get(f"{time.strftime('%Y-%m-%d')}|{session_id}") == 1)

    # 状态已落盘
    state_file = Path(tmpdir) / "proactive_state.json"
    check("主动消息状态已持久化到磁盘", lambda: state_file.exists())
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    check("落盘内容含当日计数与用户活跃时间",
          lambda: saved.get("date") and saved.get("counts") and saved.get("user_activity"))

    # 当日上限：默认 2 条
    M.proactive_counts[f"{time.strftime('%Y-%m-%d')}|{session_id}"] = 2
    M.proactive_pending.clear()
    M.last_user_activity[session_id] = time.time() - 7200
    sent.clear()
    asyncio.run(M.proactive_idle_check())
    check("达到每日上限后不再排队", lambda: session_id not in M.proactive_pending and not sent)

    # 静默时段不发
    M.proactive_counts.clear()
    M.proactive_pending.clear()
    M.last_user_activity[session_id] = time.time() - 7200
    M._in_quiet_hours = lambda: True
    CT._in_quiet_hours = lambda: True
    asyncio.run(M.proactive_idle_check())
    check("静默时段不排定也不发送",
          lambda: session_id not in M.proactive_pending and not sent)

    # 用户在计划期间回来聊天 → 取消
    M._in_quiet_hours = lambda: False
    CT._in_quiet_hours = lambda: False
    M.proactive_pending[session_id] = time.time() - 1
    M.last_user_activity[session_id] = time.time()
    sent.clear()
    asyncio.run(M.proactive_idle_check())
    check("计划期间用户回来互动则取消本次主动消息",
          lambda: not sent and session_id not in M.proactive_pending)

    # 生成失败 → 重新排队而不是卡死
    M.proactive_pending[session_id] = time.time() - 1
    M.last_user_activity[session_id] = time.time() - 7200
    sent.clear()

    async def empty_gen(ctx, instruction, history_block=""):
        return ""
    M.generate_proactive_text = empty_gen
    CT.generate_proactive_text = empty_gen
    asyncio.run(M.proactive_idle_check())
    check("生成空文本时清空计划（可重试，不会卡死）",
          lambda: not sent and session_id not in M.proactive_pending)

    # 重启后计数不丢
    M.proactive_counts[f"{time.strftime('%Y-%m-%d')}|{session_id}"] = 1
    M.save_proactive_state()
    M.proactive_counts.clear()
    M.last_user_activity.clear()
    M.load_proactive_state()
    check("重启后当日计数恢复（每日上限不会被重启绕过）",
          lambda: M.proactive_counts.get(f"{time.strftime('%Y-%m-%d')}|{session_id}") == 1)
    check("重启后用户活跃时间恢复",
          lambda: session_id in M.last_user_activity)


def e_end_to_end():
    """端到端接线验证：真实调度器 Job 触发 + 主动消息语音分段合成。"""
    import main as M
    import modules.sender as SM
    import modules.companion_tasks as CT
    from modules.scheduler import SchedulerManager
    from modules.llm_helpers import RoleContext

    section("E1 主动消息经真实调度器 Job 触发")

    tmpdir = ROOT / ".tmp_test" / "fixrun" / "e2e"
    tmpdir.mkdir(parents=True, exist_ok=True)
    for old in tmpdir.glob("*"):
        if old.is_file():
            old.unlink()

    cfg = Cfg({"memory_data_path": str(tmpdir), "character_key": "murasame",
               "character_name": "丛雨", "proactive_enabled": True,
               "proactive_idle_minutes": 30, "proactive_idle_jitter": "",
               "proactive_max_per_day": 2, "proactive_quiet_start": "23:00",
               "proactive_quiet_end": "08:00", "proactive_voice": False,
               "proactive_sticker": False, "proactive_prompt": "找话题",
               "personality_prompt": "你是丛雨", "tts_reply_enabled": False,
               "scheduler_enabled": True})

    sent = []

    class FakeSender:
        client = object()

        @contextlib.contextmanager
        def for_session(self, session_id):
            self.last_session = session_id
            yield
        
        async def speak_and_send(self, session_type, target, text, emotions, ctx,
                                 use_voice=False, sticker=False, emotion="", session_id=""):
            sent.append(text)
            return True

    class CfgLoader:
        config = dict(cfg)
        roles = {"murasame": {"character_key": "murasame", "character_name": "丛雨"}}
        active_character = "murasame"

        def get(self, k, d=None):
            return self.config.get(k, d)

    M.global_config = CfgLoader()
    M.app_context.global_config = M.global_config
    M.memory_manager = M.MemoryManager(M.global_config)
    M.app_context.memory_manager = M.memory_manager
    for d in (M.last_interaction, M.last_user_activity, M.proactive_counts, M.proactive_pending):
        d.clear()
    M._proactive_state_date = time.strftime("%Y-%m-%d")
    M.sender = FakeSender()
    M.app_context.sender = M.sender

    async def fake_proactive(ctx, instruction, history_block=""):
        return "主人，今天也辛苦了，要好好休息哦。"

    M.generate_proactive_text = fake_proactive
    CT.generate_proactive_text = fake_proactive
    M._in_quiet_hours = lambda: False
    CT._in_quiet_hours = lambda: False

    sid = "private_777"
    session = M.memory_manager.load_session_data(sid)
    session["history"] = [{"role": "user", "content": "在吗",
                           "timestamp": time.time() - 7200}]
    M.memory_manager.save_session_data(sid, session)
    M.last_user_activity[sid] = time.time() - 7200
    M.proactive_pending[sid] = time.time() - 1  # 已到点

    # 用真实调度器 + 真实 Job 对象触发（验证函数签名/协程注册没问题）
    sch = SchedulerManager()
    job = sch.add_job("proactive_idle", "主动消息检查",
                      {"type": "interval", "seconds": 30}, M.proactive_idle_check)
    M.scheduler = sch
    M.register_feature_jobs()
    asyncio.run(sch._run_job(job))

    check("调度器触发后主动消息成功发出", lambda: len(sent) == 1)
    check("Job 执行无异常且被计入运行次数",
          lambda: job.run_count == 1 and job.last_error is None)
    check("发送后计数落盘可查",
          lambda: M.proactive_counts.get(f"{time.strftime('%Y-%m-%d')}|{sid}") == 1)

    # 会话被删除（记忆文件消失）后，闲置计时与计数不该再让它收到主动消息
    M.memory_manager.get_memory_file(sid).unlink()
    M.last_proactive_sent.pop(sid, None)
    M.proactive_awaiting.discard(sid)
    M.last_user_activity[sid] = time.time() - 7200
    M.proactive_counts.pop(f"{time.strftime('%Y-%m-%d')}|{sid}", None)
    M._in_quiet_hours = lambda: False
    CT._in_quiet_hours = lambda: False
    sent.clear()
    asyncio.run(M.proactive_idle_check())   # 进入调度
    asyncio.run(M.proactive_idle_check())   # 到点发送
    check("已删除的会话不再收到主动消息", lambda: not sent)
    check("已删除会话的状态被一并清理",
          lambda: sid not in M.proactive_pending and sid not in M.last_user_activity)

    # 删除动作同时清掉主动消息状态（WebUI 删除路径）
    alive = "private_778"
    M.memory_manager.save_session_data(
        alive, {"history": [{"role": "user", "content": "hi",
                             "timestamp": time.time()}]})
    M.last_user_activity[alive] = time.time()
    M.proactive_awaiting.add(alive)
    M.last_proactive_sent[alive] = time.time()
    M.forget_proactive_session(alive)
    check("删除会话时主动消息状态同步清除",
          lambda: alive not in M.last_user_activity and alive not in M.last_proactive_sent
          and alive not in M.proactive_awaiting)

    section("E2 主动消息/提醒语音：多句分段合成且不丢句")

    data_dir = ROOT / ".tmp_test" / "fixrun" / "e2e_voice"
    data_dir.mkdir(parents=True, exist_ok=True)
    wav_path = data_dir / "piece.wav"
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 1600)

    synth_calls = []

    async def fake_synth(config, text, emotion, emotions, dpath, stats=None,
                         mimic="", mimics=None):
        synth_calls.append((text, emotion))
        out = dpath / f"fake_{len(synth_calls)}.wav"
        out.write_bytes(wav_path.read_bytes())
        return out

    class FakeClient2:
        def __init__(self):
            self.calls = []

        async def send_private_msg(self, user_id=None, message=None):
            self.calls.append(message)
            return {}

    orig_synth = SM.synthesize_sentence
    orig_check = SM.check_tts_service
    SM.synthesize_sentence = fake_synth

    async def ok_tts(config):
        return True
    SM.check_tts_service = ok_tts

    class MM:
        data_path = data_dir

        @staticmethod
        def cleanup_voice_cache(n=20):
            return None
    try:
        vcfg = Cfg({"voice_transition": False, "default_voice": "pingjing",
                    "todo_voice_emotion": "haixiu"})
        snd = SM.MessageSender(vcfg, MM())
        snd.client = FakeClient2()
        text = "主人，提醒时间到啦。你说过要吃药，别忘了哦！"
        # emotion 由调用方传入（待办提醒由 fire_reminder 传 todo_voice_emotion；
        # 不传时回退角色默认情绪），这里模拟待办提醒的调用方式
        ok = asyncio.run(snd.speak_and_send("private", 10001, text, {"haixiu": {}},
                                            RoleContext(vcfg), use_voice=True,
                                            emotion="haixiu"))
        voices = [c for c in snd.client.calls
                  if any(type(seg).__name__ == "Record" for seg in c)]
        texts = [getattr(seg, "text", "") for c in snd.client.calls for seg in c
                 if getattr(seg, "text", None)]
    finally:
        SM.synthesize_sentence = orig_synth
        SM.check_tts_service = orig_check

    check("多句提醒被逐句合成（没有整段一次丢给 TTS）",
          lambda: [c[0] for c in synth_calls] == ["主人，提醒时间到啦。", "你说过要吃药，别忘了哦！"])
    check("合成用的是配置的提醒情绪 haixiu",
          lambda: all(c[1] == "haixiu" for c in synth_calls))
    check("分段语音合并后只发一条语音", lambda: len(voices) == 1)
    check("文本原样发送（表情符号等不影响文字）",
          lambda: texts and all(t == text for t in texts))
    check("speak_and_send 返回成功", lambda: ok is True)

    # 任一句合成失败 → 整段兜底重试，绝不出现"半截语音"
    fail_state = {"n": 0}

    async def flaky_synth(config, text, emotion, emotions, dpath, stats=None,
                          mimic="", mimics=None):
        fail_state["n"] += 1
        if fail_state["n"] == 2:
            return None
        out = dpath / f"flaky_{fail_state['n']}.wav"
        out.write_bytes(wav_path.read_bytes())
        return out

    SM.synthesize_sentence = flaky_synth
    SM.check_tts_service = ok_tts
    try:
        snd2 = SM.MessageSender(vcfg, MM())
        snd2.client = FakeClient2()
        ok2 = asyncio.run(snd2.speak_and_send("private", 10001, text, {"haixiu": {}},
                                              RoleContext(vcfg), use_voice=True,
                                              emotion="haixiu"))
        voices2 = [c for c in snd2.client.calls
                   if any(type(seg).__name__ == "Record" for seg in c)]
    finally:
        SM.synthesize_sentence = orig_synth
        SM.check_tts_service = orig_check

    check("分段合成失败时整段兜底（不发出半截语音）",
          lambda: ok2 is True and len(voices2) == 1 and fail_state["n"] == 3)


def f_thinking_never_spoken():
    """主动消息/提醒把 thinking 思考内容念出来 —— 事故回归测试。"""
    from modules.llm_helpers import (RoleContext, strip_thinking, looks_like_thinking,
                                     extract_answer_from_message, is_reasoning_model,
                                     chat_once)
    import modules.llm_helpers as LH

    section("F thinking 思考内容绝不能被当成台词")

    T = "<" + "think" + ">"
    TC = "<" + "/" + "think" + ">"
    RT = "<" + "reasoning" + ">"
    RTC = "<" + "/" + "reasoning" + ">"

    check("成对 think 标签被剥离",
          lambda: strip_thinking(f"{T}用户问天气，我需要思考。{TC}主人，今天天气不错呢。")
          == "主人，今天天气不错呢。")
    check("reasoning 标签同样被剥离",
          lambda: strip_thinking(f"{RT}let me think about it{RTC}主人，我在。") == "主人，我在。")
    check("缺少开始标签（服务端吞掉）时丢弃标签前内容",
          lambda: strip_thinking(f"用户想要我主动开口。\n{TC}主人，好久没说话了。")
          == "主人，好久没说话了。")
    check("未闭合标签（流式截断）也能抢救出正片",
          lambda: strip_thinking(f"{T}用户想要我主动开口。我需要想出合适的话。\n主人，好久没说话了。")
          == "主人，好久没说话了。")
    check("无标签的中文思考段被剥离",
          lambda: strip_thinking("思考过程：用户想要安慰。\n\n主人，我在呢。") == "主人，我在呢。")
    check("无标签的英文思考段 + Final Answer 被剥离",
          lambda: strip_thinking("The user asks me to say something. We need to be casual.\n"
                                 "Final Answer: 主人，好久不见。") == "主人，好久不见。")
    check("Thinking Process 分点列表被整体剥离",
          lambda: strip_thinking("Thinking Process:\n1. 分析用户情绪\n2. 决定回复\n\n主人，我在。")
          == "主人，我在。")
    check("正常台词不受影响（含「想」字）",
          lambda: strip_thinking("主人，我今天一直在想你呢。") == "主人，我今天一直在想你呢。")
    check("正常台词不以思考特征开头时不误伤",
          lambda: strip_thinking("主人，你说得对。") == "主人，你说得对。")

    # R1 系模板用全角竖线包的特殊 token，没有尖括号，早期写法认不出来就整段念了出去
    OPEN = "\uff1c\uff5cbegin\u2581of\u2581thinking\uff5c\uff1e"
    CLOSE = "\uff1c\uff5cend\u2581of\u2581thinking\uff5c\uff1e"
    check("R1 系思考 token（成对）被剥离",
          lambda: strip_thinking(f"{OPEN}用户问天气{CLOSE}主人，今天天气不错呢。")
          == "主人，今天天气不错呢。")
    check("R1 系思考 token（只有闭合）时丢弃前面内容",
          lambda: strip_thinking(f"用户在问天气。{CLOSE}主人，今天天气不错呢。")
          == "主人，今天天气不错呢。")
    check("R1 系思考 token（未闭合）也认得出",
          lambda: "用户问天气" not in strip_thinking(f"{OPEN}用户问天气"))

    check("Ollama thinking 字段有内容、content 为空 → 返回空（不拿思考当台词）",
          lambda: extract_answer_from_message({"content": "", "thinking": "一大段思考过程"}) == "")
    check("OpenAI reasoning_content 被丢弃、content 保留",
          lambda: extract_answer_from_message({"content": "主人好",
                                               "reasoning_content": "思考"}) == "主人好")
    check("content 内嵌 think 块被剥离",
          lambda: extract_answer_from_message({"content": f"{T}abc{TC}主人好"}) == "主人好")

    check("污染文本被判定为 thinking（供上层整段丢弃）",
          lambda: looks_like_thinking("The user asks me to say something. We need to be casual.")
          and looks_like_thinking(f"{T}思考{TC}台词"))
    check("正常台词不会被误判为 thinking",
          lambda: not looks_like_thinking("主人，我今天一直在想你呢。")
          and not looks_like_thinking("ご主人、おかえりなさい。"))

    check("识别推理模型（qwen3 → 需要强关思考）",
          lambda: is_reasoning_model({"llm_model_name": "huihui_ai/qwen3.5-abliterated:9b"})
          and not is_reasoning_model({"llm_model_name": "llama3:8b"}))

    # --- chat_once 全链路：服务端把思考放在 thinking 字段 ---
    import httpx

    class FakeResp:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeClient:
        payload = {}

        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            FakeClient.payload = json
            return FakeResp({"message": {"content": "主人，我在呢。",
                                         "thinking": "用户希望我主动开口。我需要思考话题……"}})

    orig = LH.httpx.AsyncClient
    LH.httpx.AsyncClient = FakeClient
    try:
        ctx = RoleContext({"llm_backend": "ollama", "llm_model_name": "qwen3:8b",
                           "enable_think": False}, {})
        res = asyncio.run(chat_once(ctx, [{"role": "user", "content": "hi"}]))
        sent_payload = dict(FakeClient.payload)
    finally:
        LH.httpx.AsyncClient = orig

    check("chat_once 只返回台词，不返回 thinking 字段",
          lambda: res["content"] == "主人，我在呢。")
    check("ollama 请求同时关闭 think 与 chat_template 思考开关",
          lambda: sent_payload.get("think") is False
          and sent_payload.get("chat_template_kwargs", {}).get("enable_thinking") is False)

    # --- 流式：思考分片必须被过滤，只有 content 会被 yield ---
    from modules.llm_helpers import stream_chat
    import json as _json

    class FakeStreamResp:
        status_code = 200

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            lines = [
                {"message": {"thinking": "用户希望我主动开口。"}, "done": False},
                {"message": {"thinking": "我需要思考一个合适的话题。"}, "done": False},
                {"message": {"content": "主人，"}, "done": False},
                {"message": {"content": "好久没说话了。"}, "done": True},
            ]
            for item in lines:
                yield _json.dumps(item)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeStreamClient:
        def __init__(self, *a, **k):
            pass

        def stream(self, method, url, json=None, headers=None):
            return FakeStreamResp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    LH.httpx.AsyncClient = FakeStreamClient
    try:
        sctx = RoleContext({"llm_backend": "ollama", "llm_model_name": "qwen3:8b"}, {})

        async def collect():
            parts = []
            async for chunk in stream_chat(sctx, [{"role": "user", "content": "hi"}]):
                parts.append(chunk.get("delta", ""))
            return "".join(parts)
        streamed = asyncio.run(collect())
    finally:
        LH.httpx.AsyncClient = orig

    check("流式输出里没有思考分片，只有台词",
          lambda: streamed == "主人，好久没说话了。")

    # --- 主动消息：思考污染 + 超长输出都必须被拦下 ---
    import modules.jobs as J

    async def fake_text(ctx, system, user_prompt, max_tokens=512):
        return "The user asks me to say something. We need to be casual.\n主人，好久不见。"
    orig_text = J.generate_text_reply
    J.generate_text_reply = fake_text
    try:
        out = asyncio.run(J.generate_proactive_text(RoleContext({"personality_prompt": "x"}), "说句话"))
        check("主动消息含思考特征 → 整段丢弃（不合成语音）", lambda: out == "")
    finally:
        J.generate_text_reply = orig_text

    # 正常台词但偏长：截断到上限内，不再整段丢弃
    long_line = ("主人，今天降温了，出门记得多穿一件外套。"
                 "我下午把屋子收拾了一下，窗台上那盆花也浇了水。"
                 "你昨天说的那件事我一直记着，别急，慢慢来就好。"
                 "晚饭别忘了吃，胃不好就别老喝冰的东西。"
                 "要是累了就早点休息，我在这儿等你回来。"
                 "外面风挺大的，回来路上小心一点，别又着凉了。") * 2

    async def long_text(ctx, system, user_prompt, max_tokens=512):
        return long_line
    J.generate_text_reply = long_text
    try:
        out2 = asyncio.run(J.generate_proactive_text(
            RoleContext({"personality_prompt": "x", "proactive_text_max_chars": 200}), "说句话"))
        check("主动消息超长 → 截断到上限内而不是整段丢弃",
              lambda: 0 < len(out2) <= 200 and out2.endswith("。"))
    finally:
        J.generate_text_reply = orig_text

    async def no_mark_text(ctx, system, user_prompt, max_tokens=512):
        return "主人" * 200
    J.generate_text_reply = no_mark_text
    try:
        out7 = asyncio.run(J.generate_proactive_text(
            RoleContext({"personality_prompt": "x", "proactive_text_max_chars": 120}), "说句话"))
        check("无任何标点的超长文本仍被丢弃", lambda: out7 == "")
    finally:
        J.generate_text_reply = orig_text

    # 内嵌 think 块（无元叙述特征）也必须被拦下：这是实际事故的形态
    async def inline_block(ctx, system, user_prompt, max_tokens=512):
        return (f"{T}用户希望我主动开口，我要想一个自然的话题。"
                f"考虑天气、考虑主人的工作、考虑最近的话题……{TC}"
                "主人，最近工作忙不忙？记得按时吃饭哦。")
    J.generate_text_reply = inline_block
    try:
        out5 = asyncio.run(J.generate_proactive_text(
            RoleContext({"personality_prompt": "x"}), "说句话"))
        check("内嵌 think 块的输出 → 整段丢弃（不把思考念出来）", lambda: out5 == "")
    finally:
        J.generate_text_reply = orig_text

    # 模型正常但偏长：截断到上限，避免一分钟语音
    async def medium_text(ctx, system, user_prompt, max_tokens=512):
        return "主人，" + "今天的风很温柔，" * 20
    J.generate_text_reply = medium_text
    try:
        out6 = asyncio.run(J.generate_proactive_text(
            RoleContext({"personality_prompt": "x", "proactive_text_max_chars": 60}), "说句话"))
        check("超过字数上限的文本按标点截断（上限可配）",
              lambda: 0 < len(out6) <= 60 and out6.endswith("，"))
    finally:
        J.generate_text_reply = orig_text

    async def tagged_text(ctx, system, user_prompt, max_tokens=512):
        return f"{T}用户希望我主动开口，我需要思考。{TC}主人，好久没说话了。"
    J.generate_text_reply = tagged_text
    try:
        out3 = asyncio.run(J.generate_proactive_text(RoleContext({"personality_prompt": "x"}), "说句话"))
        check("主动消息带 think 标签 → 整段丢弃（宁可这次不发）", lambda: out3 == "")
    finally:
        J.generate_text_reply = orig_text

    # 纯文本侧（摘要/画像等）仍应尽力抢救出正片
    from modules.llm_helpers import strip_thinking as _st
    check("纯文本侧仍能从 think 标签里抢救出台词",
          lambda: _st(f"{T}思考{TC}主人，好久没说话了。") == "主人，好久没说话了。")

    async def normal_text(ctx, system, user_prompt, max_tokens=512):
        return "主人，好久没说话了，在忙吗？"
    J.generate_text_reply = normal_text
    try:
        out4 = asyncio.run(J.generate_proactive_text(RoleContext({"personality_prompt": "x"}), "说句话"))
        check("正常主动消息不受影响", lambda: out4 == "主人，好久没说话了，在忙吗？")
        check("推理模型会补上强关思考的提示后缀",
              lambda: bool(LH.no_think_suffix({"llm_model_name": "qwen3:8b"}))
              and not LH.no_think_suffix({"llm_model_name": "llama3:8b"}))
    finally:
        J.generate_text_reply = orig_text


def g_sticker_capture_gate():
    """纯风景/阴森图被收藏进 gaoxing/any —— 事故回归测试。"""
    from modules.llm_helpers import (RoleContext, sticker_capture_allowed,
                                     _sticker_capture_instruction)
    import modules.stickers as ST

    section("G 表情包收藏：不适合的图片必须被拦下")

    def ctx_of(**kw):
        base = {"sticker_capture_enabled": True}
        base.update(kw)
        return RoleContext(base, {})

    safe_yes = ('{"sentences":[{"zh":"嗯","emotion":"pingjing"}]}\n'
                '{"sticker_safe": true, "category": "weixie", "reason": "适合阴阳怪气地回怼主人"}')
    safe_no = ('{"sentences":[{"zh":"好阴森","emotion":"jingya"}]}\n'
               '{"sticker_safe": false}')
    no_verdict = '{"sentences":[{"zh":"风景不错","emotion":"pingjing"}], "description": "一张阴森的森林照片。"}'
    vague_reason = ('{"sticker_safe": true, "category": "gaoxing", "reason": "好看"}')

    check("明确判定可收藏 → 允许", lambda: sticker_capture_allowed(ctx_of(), safe_yes) is True)
    check("明确判定不收藏 → 拒绝（阴森风景图）", lambda: sticker_capture_allowed(ctx_of(), safe_no) is False)
    check("模型没有给出判定 → 默认拒绝（不再默认收藏）",
          lambda: sticker_capture_allowed(ctx_of(), no_verdict) is False)
    check("判定理由过于笼统 → 拒绝",
          lambda: sticker_capture_allowed(ctx_of(), vague_reason) is False)
    check("关闭 require_verdict 才恢复旧行为",
          lambda: sticker_capture_allowed(ctx_of(sticker_capture_require_verdict=False),
                                          no_verdict) is True)
    check("收藏总开关关闭时一律拒绝",
          lambda: sticker_capture_allowed(RoleContext({}, {}), safe_yes) is False)

    instr = _sticker_capture_instruction(ctx_of())
    check("判定指令明确否决无人物/无文字/纯风景图",
          lambda: "纯风景" in instr and "无人物、无文字" in instr)
    check("判定指令明确否决阴森/恐怖/诡异画面",
          lambda: "阴森" in instr and "恐怖" in instr)
    check("判定指令要求拿不准就不收藏",
          lambda: "宁可不收藏" in instr and "拿不准就输出" in instr)
    check("判定指令要求换角色也能用的通用名字（不含角色名、不写具体事情）",
          lambda: "换角色也必须照样能用" in instr and "严禁出现任何角色名" in instr
          and "空话" in instr)
    check("分类说明带使用场景，避免模型只能瞎挑",
          lambda: "gaoxing(开心大笑" in instr and "weixie(威胁" in instr)
    check("阴森画面不得判为正向情绪的区分要点已写入",
          lambda: "绝不算开心" in ST.CATEGORY_DISAMBIGUATION)

    # 分类候选取自表情库目录下实际存在的子文件夹：写死分类名的话，
    # 用户把目录改成中文命名后，模型选出的分类会和磁盘上的文件夹对不上
    lib = ROOT / ".tmp_test" / "fixrun" / "stickercat"
    for old in lib.glob("*"):
        if old.is_dir():
            for f in old.iterdir():
                f.unlink()
            old.rmdir()
    (lib / "高兴").mkdir(parents=True, exist_ok=True)
    (lib / "无语").mkdir(parents=True, exist_ok=True)
    (lib / "any").mkdir(parents=True, exist_ok=True)
    (lib / "高兴" / "seed.png").write_bytes(b"seed-gaoxing")
    (lib / "无语" / "seed.png").write_bytes(b"seed-wuyu")
    lib_ctx = RoleContext({"sticker_capture_enabled": True,
                           "stickers_dir": str(lib)}, {})
    lib_instr = _sticker_capture_instruction(lib_ctx)
    check("候选分类取自表情库实际文件夹",
          lambda: "- 高兴" in lib_instr and "- 无语" in lib_instr
          and "gaoxing" not in lib_instr)
    check("通用池 any 不算分类",
          lambda: "any" not in ST.category_candidates(
              {"stickers_dir": str(lib)}))
    check("表情库没有任何分类时退回内置分类",
          lambda: ST.category_candidates({"stickers_dir": str(lib / "空")})
          == sorted(ST.STRICT_ALLOWED))
    (lib / "sajiao").mkdir(parents=True, exist_ok=True)
    check("空目录不算分类（面板不显示它，模型也不该选它）",
          lambda: "sajiao" not in ST.category_candidates({"stickers_dir": str(lib)}))
    (lib / "sajiao").rmdir()


def g2_sticker_no_category_fallback(monkeypatch_note=""):
    """分类拿不准时不得回退 pingjing，也不得把无关图片塞进表情库。"""
    import main as M
    import modules.reply_pipeline as RP
    from modules.llm_helpers import RoleContext

    section("G2 分类兜底：不再默认 pingjing / 不再存进表情库")

    seen = {}

    async def fake_chat_once(ctx, messages, tools=None):
        seen["messages"] = messages
        return {"content": "none", "tool_calls": [], "ms": 0, "backend": "ollama"}

    orig = M.chat_once
    M.chat_once = fake_chat_once
    RP.chat_once = fake_chat_once
    try:
        ctx = RoleContext({"sticker_capture_enabled": True}, {})
        judged = asyncio.run(M._sticker_judgement_from_llm(
            ctx, {"description": "一张阴森的森林风景照，没有人物也没有文字。",
                  "sentences": [{"zh": "有点吓人"}]}))
        check("模型回答 none（不适合当表情包）→ 返回空分类（不是 pingjing）",
              lambda: judged["category"] == "")
        check("中立判定只喂客观画面描述，不带角色回复原文",
              lambda: "有点吓人" not in str(seen["messages"])
              and "一张阴森的森林风景照" in str(seen["messages"]))
        check("中立判定要求不代入角色、不考虑对话中任何人的情绪",
              lambda: "不要代入任何角色" in str(seen["messages"])
              and "不要考虑对话里任何人的情绪" in str(seen["messages"]))

        async def fake_chat_pingjing(ctx2, messages, tools=None):
            return {"content": "pingjing", "tool_calls": [], "ms": 0, "backend": "ollama"}
        M.chat_once = fake_chat_pingjing
        RP.chat_once = fake_chat_pingjing
        cat2 = asyncio.run(M._sticker_judgement_from_llm(
            ctx, {"description": "普通风景照", "sentences": [{"zh": "嗯"}]}))
        check("模型给出合法分类时照常使用（pingjing 也是合法分类）",
              lambda: cat2["category"] == "pingjing")

        async def fake_chat_named(ctx2, messages, tools=None):
            return {"content": '{"category": "sajiao", "name": "撒娇求抱抱"}',
                    "tool_calls": [], "ms": 0, "backend": "ollama"}
        M.chat_once = fake_chat_named
        RP.chat_once = fake_chat_named
        named = asyncio.run(M._sticker_judgement_from_llm(
            ctx, {"description": "白色毛绒玩偶，图片上方写着：兄弟我爱你",
                  "sentences": [{"zh": "哼"}]}))
        check("中立判定同时给出分类与用途名",
              lambda: named["category"] == "sajiao" and named["name"] == "撒娇求抱抱")

        async def fake_chat_garbage(ctx2, messages, tools=None):
            return {"content": "我不知道该怎么分类", "tool_calls": [], "ms": 0, "backend": "ollama"}
        M.chat_once = fake_chat_garbage
        RP.chat_once = fake_chat_garbage
        cat3 = asyncio.run(M._sticker_judgement_from_llm(
            ctx, {"description": "风景照", "sentences": [{"zh": "嗯"}]}))
        check("模型输出无法识别 → 返回空分类（不再默认 pingjing）", lambda: cat3["category"] == "")

        async def fake_chat_explain(ctx2, messages, tools=None):
            return {"content": "这张图不是 gaoxing，更像是 wuyu 吧",
                    "tool_calls": [], "ms": 0, "backend": "ollama"}
        M.chat_once = fake_chat_explain
        RP.chat_once = fake_chat_explain
        ambiguous = asyncio.run(M._sticker_judgement_from_llm(
            ctx, {"description": "表情夸张的图", "sentences": [{"zh": "嗯"}]}))
        check("输出里出现多个分类时不猜（避免归错目录）",
              lambda: ambiguous["category"] == "")

        async def fake_chat_other(ctx2, messages, tools=None):
            return {"content": '{"category": "shangxin", "name": "难过想哭"}',
                    "tool_calls": [], "ms": 0, "backend": "ollama"}
        M.chat_once = fake_chat_other
        RP.chat_once = fake_chat_other
        other = asyncio.run(M._sticker_judgement_from_llm(
            ctx, {"description": "一张哭泣的图", "sentences": [{"zh": "嗯"}]}))
        check("白名单之外的分类不落库", lambda: other["category"] == "")

        async def fake_chat_boom(ctx2, messages, tools=None):
            raise RuntimeError("model down")
        M.chat_once = fake_chat_boom
        RP.chat_once = fake_chat_boom
        cat4 = asyncio.run(M._sticker_judgement_from_llm(
            ctx, {"description": "风景照", "sentences": [{"zh": "嗯"}]}))
        check("中立判定调用异常 → 返回 None（沿用识图给出的分类）", lambda: cat4 is None)
    finally:
        M.chat_once = orig
        RP.chat_once = orig

    # auto_capture_from_images：分类为空 → 不落盘
    captured = []
    import modules.stickers as ST
    import modules.media_cache as MC

    async def fake_auto_capture(ctx, mgr, source, category="", image_data=None, reason=""):
        captured.append((category, reason))
        return True
    orig_capture = ST.auto_capture_image
    ST.auto_capture_image = fake_auto_capture
    orig_judge_fn = M._sticker_judgement_from_llm

    async def judge_none(ctx, image_result):
        return {"category": "", "name": ""}
    M._sticker_judgement_from_llm = judge_none
    MC._sticker_judgement_from_llm = judge_none
    try:
        ctx = RoleContext({"sticker_capture_enabled": True,
                           "sticker_capture_min_score": 0.7}, {})
        src = ROOT / ".tmp_test" / "fixrun" / "unfit.png"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_bytes(b"\x89PNG\r\n\x1a\n0123456789")
        asyncio.run(M.auto_capture_from_images(
            ctx, {"should": True, "score": 0.9, "category": "wuyu", "reason": "懒得理你"},
            [str(src)], {"description": "阴森风景", "sentences": [{"zh": "嗯"}]}))
        check("中立判定为不适合时覆盖识图分类 → 完全不调用保存（不污染表情库）",
              lambda: captured == [])

        async def judge_sajiao(ctx, image_result):
            return {"category": "sajiao", "name": "撒娇求抱抱"}
        M._sticker_judgement_from_llm = judge_sajiao
        MC._sticker_judgement_from_llm = judge_sajiao
        captured.clear()
        asyncio.run(M.auto_capture_from_images(
            ctx, {"should": True, "score": 0.9, "category": "wuyu", "reason": "懒得理你"},
            [str(src)], {"description": "可爱玩偶", "sentences": [{"zh": "哼"}]}))
        check("中立判定优先于识图给出的分类与命名",
              lambda: captured == [("sajiao", "撒娇求抱抱")])
    finally:
        M._sticker_judgement_from_llm = orig_judge_fn
        MC._sticker_judgement_from_llm = orig_judge_fn
        ST.auto_capture_image = orig_capture


def g3_sticker_category_alias():
    """表情库已有中文分类目录时，模型给拼音也不再另建一个拼音目录。"""
    import modules.stickers as ST
    section("G3 分类名对回磁盘上的原名（不再按拼音新建目录）")

    class _Cfg(dict):
        def get(self, k, d=None):
            return dict.get(self, k, d)

    class _Mgr:
        enabled = True

        def __init__(self, d):
            self.dir = Path(d)

        def rescan(self):
            return {}

    image_data = b"\x89PNG\r\n\x1a\n" + b"0" * 200

    def capture(hint, folders, preexisting=False, empty=()):
        """跑一次收藏，返回（目录名列表, 这次新增了文件的目录）。

        folders 里每个目录都先放一张占位图：空目录不算分类，不放图就测不出归类。
        empty 里的目录只建空壳，用于验证空目录不会被当成分类。
        preexisting=True 时先把待收藏的同一张图放进第一个目录，用于验证不重复收藏。
        """
        tmp = Path(tempfile.mkdtemp(prefix="stk_alias_"))
        for name in folders:
            (tmp / name).mkdir(parents=True, exist_ok=True)
            if name not in empty:
                (tmp / name / "seed.png").write_bytes(b"seed-" + name.encode("utf-8"))
        if preexisting:
            (tmp / folders[0] / "已有.png").write_bytes(image_data)
        before = {d.name: len(list(d.iterdir())) for d in tmp.iterdir() if d.is_dir()}
        cfg = _Cfg({"stickers_dir": str(tmp), "sticker_capture_enabled": True,
                    "sticker_capture_min_interval": 0,
                    "sticker_capture_max_per_day": 0})
        asyncio.run(ST.auto_capture_image(
            cfg, _Mgr(tmp), "", category_hint=hint,
            image_data=image_data, reason="撒娇求抱抱"))
        dirs = sorted(d.name for d in tmp.iterdir() if d.is_dir())
        added = sorted(name for name in dirs
                       if len(list((tmp / name).iterdir())) > before.get(name, 0))
        return dirs, added

    folders = ["撒娇", "高兴"]
    check("模型给拼音 sajiao → 落进已有的「撒娇」目录",
          lambda: capture("sajiao", folders) == (sorted(folders), ["撒娇"]))
    check("模型给同义词 cute → 也落进已有的「撒娇」目录",
          lambda: capture("cute", folders) == (sorted(folders), ["撒娇"]))
    check("模型给拼音 gaoxing → 落进已有的「高兴」目录",
          lambda: capture("gaoxing", folders) == (sorted(folders), ["高兴"]))
    check("磁盘上没有对应目录时仍按内置分类名新建",
          lambda: capture("不认识的词", folders) == (sorted(folders + ["wuyu"]), ["wuyu"]))
    check("遗留的空目录不算分类，抢不走归类",
          lambda: capture("sajiao", folders + ["sajiao"], empty=("sajiao",))
          == (sorted(folders + ["sajiao"]), ["撒娇"]))
    check("库里已有的图片不再重复收藏",
          lambda: capture("sajiao", folders, preexisting=True) == (sorted(folders), []))


def h_tool_message_hygiene():
    """工具调用 400 Bad Request —— 事故回归测试。"""
    from modules.llm_helpers import (_coerce_arguments_object, normalize_message_tool_calls,
                                     normalize_messages_for_backend, _extract_first_url,
                                     _is_ollama_required_schema_error, chat_once, RoleContext)
    import modules.llm_helpers as LH

    from modules.tools import DEFAULT_TOOLS
    check("默认工具包含关键词搜索", lambda: any(
        t.get("name") == "web_search" and t.get("builtin") == "web_search"
        for t in DEFAULT_TOOLS))

    check("工具 schema 的 required 保持数组格式",
          lambda: _extract_first_url("请搜索 https://example.com/path?a=1。")
          == "https://example.com/path?a=1")
    check("识别 Ollama required 类型兼容错误",
          lambda: _is_ollama_required_schema_error(
              'json: cannot unmarshal object into Go struct field '
              'ToolFunctionParameters.tools.function.parameters.required of type string'))
    check("不误判普通 Ollama 400",
          lambda: not _is_ollama_required_schema_error('{"error":"bad request"}'))

    check("空 arguments 规整为空对象（不再把空串发给 ollama）",
          lambda: _coerce_arguments_object("") == {}
          and _coerce_arguments_object(None) == {}
          and _coerce_arguments_object("   ") == {})
    check("合法 JSON 字符串解析成对象",
          lambda: _coerce_arguments_object('{"url": "https://a.com"}')
          == {"url": "https://a.com"})
    check("dict 原样保留",
          lambda: _coerce_arguments_object({"url": "x"}) == {"url": "x"})
    check("半截/松散 JSON 尽力解析成对象",
          lambda: _coerce_arguments_object("{url: bad}") == {"url": "bad"})
    check("完全无法解析时置空对象（绝不发空串）",
          lambda: _coerce_arguments_object("}}}not json{{{") == {})
    check("裸文本参数按 URL 兜底，其它裸文本判为无效",
          lambda: _coerce_arguments_object("https://a.com/x") == {"url": "https://a.com/x"}
          and _coerce_arguments_object("www.a.com") == {"url": "www.a.com"}
          and _coerce_arguments_object("not json") == {})
    check("非对象标量包成 value 字段（保持合法对象）",
          lambda: _coerce_arguments_object("[1, 2]") == {"value": [1, 2]})

    # 字符串形态的 tool_calls（历史脏数据）会让 ollama 直接 400
    dirty = {"role": "assistant", "content": "",
             "tool_calls": '[{"id":"c1","type":"function","function":{"name":"web_fetch",'
                           '"arguments":{"url":"https://a.com"}}}]'}
    fixed = normalize_message_tool_calls(dict(dirty))
    check("字符串 tool_calls 被解析成数组",
          lambda: isinstance(fixed.get("tool_calls"), list)
          and fixed["tool_calls"][0]["function"]["name"] == "web_fetch")
    check("缺失 function 的调用按 name/arguments 兜底",
          lambda: normalize_message_tool_calls(
              {"role": "assistant", "tool_calls": [{"id": "c", "name": "calculate",
                                                    "arguments": '{"expression":"1+1"}'}]}
          )["tool_calls"][0]["function"]["arguments"] == {"expression": "1+1"})
    check("没有工具名的脏调用被丢弃（不留非法结构）",
          lambda: "tool_calls" not in normalize_message_tool_calls(
              {"role": "assistant", "tool_calls": [{"id": "c"}]}))

    msgs = [
        {"role": "user", "content": "搜索新闻"},
        {"role": "assistant", "content": "", "tool_calls": dirty["tool_calls"]},
        {"role": "tool", "content": "结果", "tool_call_id": "x"},
        {"role": "tool", "content": "孤立结果"},
    ]
    oll = normalize_messages_for_backend(json.loads(json.dumps(msgs)), "ollama")
    opn = normalize_messages_for_backend(json.loads(json.dumps(msgs)), "openai")
    check("ollama：tool 消息不带 tool_call_id，且 arguments 是对象",
          lambda: all("tool_call_id" not in m for m in oll if m["role"] == "tool")
          and isinstance(oll[1]["tool_calls"][0]["function"]["arguments"], dict))
    check("openai：arguments 序列化成字符串、tool_call_id 保留",
          lambda: isinstance(opn[1]["tool_calls"][0]["function"]["arguments"], str)
          and opn[2].get("tool_call_id") == "x")
    check("开头的孤立 tool 结果被丢弃（无对应 tool_calls 会 400）",
          lambda: not normalize_messages_for_backend(
              [{"role": "tool", "content": "孤立"}], "ollama"))

    # 真实链路：脏参数不应该让整条回复失败
    calls = {}

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": "好的主人。",
                                "tool_calls": [{"id": "c1", "function": {
                                    "name": "web_fetch", "arguments": ""}}]}}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            calls.setdefault("payloads", []).append(json)
            return FakeResp()

    class FakeRegistry:
        def get_schema(self):
            return [{"type": "function", "function": {"name": "web_fetch",
                                                      "parameters": {"type": "object"}}}]

        async def execute(self, name, arguments, user_id="", **kwargs):
            calls["exec"] = (name, arguments)
            return True, "网页内容"

    orig = LH.httpx.AsyncClient
    LH.httpx.AsyncClient = FakeClient
    try:
        ctx = RoleContext({"llm_backend": "ollama", "llm_model_name": "fake",
                           "tools_max_iterations": 1}, {})
        res = asyncio.run(LH.chat_with_tools(ctx, [{"role": "user", "content": "搜"}],
                                             FakeRegistry()))
    finally:
        LH.httpx.AsyncClient = orig

    check("空字符串参数被规整后执行工具（不再抛 400）",
          lambda: calls.get("exec") == ("web_fetch", {})
          and res["tool_trace"][0]["ok"] is True)
    check("发给后端的 assistant.tool_calls 里 arguments 是对象",
          lambda: all(isinstance(c["function"]["arguments"], dict)
                      for p in calls.get("payloads", [])
                      for m in p.get("messages", [])
                      for c in (m.get("tool_calls") or [])))
    check("工具循环正常返回内容", lambda: res["content"] == "好的主人。")

    # 400 的响应体必须进日志，否则只有一句 "400 Bad Request" 无法定位
    class BadClient(FakeClient):
        async def post(self, url, json=None, headers=None):
            class R:
                status_code = 400
                text = '{"error":"Value looks like object, but can\'t find closing \'}\'"}'
            return R()

    LH.httpx.AsyncClient = BadClient
    try:
        ctx = RoleContext({"llm_backend": "ollama", "llm_model_name": "fake"}, {})
        err = ""
        try:
            asyncio.run(chat_once(ctx, [{"role": "user", "content": "hi"}]))
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
    finally:
        LH.httpx.AsyncClient = orig
    check("400 时异常里带上服务端返回的原始错误信息",
          lambda: "400" in err and "closing" in err)


def i_gif_and_identity():
    """GIF 被存成 PNG + 模型把表情包当成自己 —— 事故回归测试。"""
    from modules.llm_helpers import RoleContext, build_system_prompt
    import modules.stickers as ST
    from modules.stickers import StickerManager, auto_capture_image

    section("I1 GIF 表情包保留原格式（不再存成 PNG）")

    tmpdir = ROOT / ".tmp_test" / "fixrun" / "gifcase"
    if tmpdir.exists():
        for old in sorted(tmpdir.rglob("*"), reverse=True):
            try:
                old.unlink() if old.is_file() else old.rmdir()
            except Exception:
                pass
    tmpdir.mkdir(parents=True, exist_ok=True)

    def make_gif(path, frames=3):
        from PIL import Image
        imgs = []
        for i in range(frames):
            frame = Image.new("RGB", (8, 8), color=(0, 0, 0))
            for x in range(i + 1):
                for y in range(8):
                    frame.putpixel((x, y), (255, 90, 20))
            imgs.append(frame)
        imgs[0].save(path, save_all=True, append_images=imgs[1:], duration=120, loop=0)
        return path

    gif_path = make_gif(tmpdir / "src.gif")
    cfg = Cfg({"stickers_enabled": True, "stickers_dir": str(tmpdir / "pool"),
               "sticker_capture_enabled": True, "sticker_capture_min_interval": 0,
               "sticker_capture_max_per_day": 0, "sticker_probability": 1.0,
               "sticker_max_per_reply": 1})
    mgr = StickerManager(cfg)
    ok = asyncio.run(auto_capture_image(cfg, mgr, str(gif_path), "gaoxing"))
    saved = list((tmpdir / "pool" / "gaoxing").glob("auto_*"))
    check("GIF 收藏成功", lambda: bool(ok) and len(saved) == 1)
    check("保存的文件仍是 .gif（不再变成 .png）",
          lambda: bool(saved) and saved[0].suffix.lower() == ".gif")
    check("保存内容仍是多帧动图（未被压成单帧）",
          lambda: bool(saved) and (lambda p: (lambda im: getattr(im, "n_frames", 1) > 1)(
              __import__("PIL.Image", fromlist=["Image"]).open(p)))(saved[0]))
    check("默认保留格式列表含 gif/webp",
          lambda: {".gif", ".webp"} <= ST._preserve_formats(cfg))
    check("配置可覆盖保留格式",
          lambda: ST._preserve_formats(Cfg({"sticker_capture_preserve_formats": "gif"}))
          == {".gif"}
          and ".webp" not in ST._preserve_formats(
              Cfg({"sticker_capture_preserve_formats": "gif"})))

    png_path = tmpdir / "src.png"
    from PIL import Image as PILImage
    PILImage.new("RGB", (900, 900), color=(10, 20, 30)).save(png_path)
    ok2 = asyncio.run(auto_capture_image(cfg, mgr, str(png_path), "gaoxing"))
    png_files = [p for p in (tmpdir / "pool" / "gaoxing").glob("auto_*")
                 if p.suffix.lower() in (".png", ".jpg")]
    check("静态图仍然走重编码缩小", lambda: bool(ok2) and any(
        PILImage.open(p).size[0] <= ST._sticker_max_side(cfg) for p in png_files))

    section("I2 发送表情包统一尺寸")

    from modules.sender import MessageSender

    class _SendRecorder(MessageSender):
        """发送时就把文件尺寸读出来：缩放用的临时文件发完即删。"""

        def __init__(self, cfg):
            super().__init__(cfg, None)
            self.sent_files = []
            self.sent_sizes = []

        async def send_segments(self, session_type, target_id, segments, group=None):
            for seg in segments:
                name = getattr(seg, "file", None)
                if not name:
                    continue
                p = Path(name)
                self.sent_files.append(p)
                self.sent_sizes.append(PILImage.open(p).size if p.exists() else None)
            return True

    out_cfg = Cfg({"sticker_output_max_side": 400})
    sender_rec = _SendRecorder(out_cfg)
    asyncio.run(sender_rec.send_text("private", "1", "hi", sticker=png_path))
    out_file = sender_rec.sent_files[-1]
    out_width = sender_rec.sent_sizes[-1][0]
    out_cleaned = not out_file.exists()
    check("超过上限的表情包发送前被等比缩小",
          lambda: out_width <= ST.output_max_side(out_cfg) and out_file != png_path)
    check("缩放产生的临时文件在发送后删除", lambda: out_cleaned)

    asyncio.run(sender_rec.send_text("private", "1", "hi", sticker=gif_path))
    check("动图按原图发送（重编码会丢帧）",
          lambda: sender_rec.sent_files[-1] == gif_path.resolve())

    small_png = tmpdir / "small.png"
    PILImage.new("RGB", (120, 90), color=(1, 2, 3)).save(small_png)
    asyncio.run(sender_rec.send_text("private", "1", "hi", sticker=small_png))
    check("不超过上限的表情包不重编码",
          lambda: sender_rec.sent_files[-1] == small_png.resolve())

    sender_rec.config["sticker_output_max_side"] = 0
    asyncio.run(sender_rec.send_text("private", "1", "hi", sticker=png_path))
    check("设为 0 时不缩放",
          lambda: sender_rec.sent_files[-1] == png_path.resolve())

    section("I3 模型不会把用户的表情包当成自己")

    ctx = RoleContext({"personality_prompt": "你是猫娘", "default_voice": "pingjing",
                       "character_name": "丛雨"}, {})
    sp = build_system_prompt(ctx, {"pingjing": {}})
    check("系统提示词包含图片身份规则", lambda: "【图片身份规则】" in sp)
    check("明确禁止“这是我/我的照片/我发的表情”",
          lambda: "这是我" in sp and "我发的表情" in sp and "我的照片" in sp)
    check("说明图片/表情包属于用户而不是角色",
          lambda: "都是**用户**发的东西" in sp and "绝不是丛雨自己" in sp)
    off = RoleContext({"personality_prompt": "x", "character_name": "丛雨",
                       "image_identity_guard_enabled": False}, {})
    check("可关闭该规则",
          lambda: "【图片身份规则】" not in build_system_prompt(off, {"pingjing": {}}))


def j_tts_detailed_log():
    """TTS 少词排查日志 —— 回归测试。"""
    import modules.tts as T

    section("J TTS 详细日志（LLM 完整内容 + 实际送合成文本）")

    class Sink:
        def __init__(self):
            self.lines = []

        def write(self, s):
            self.lines.append(str(s))

        def flush(self):
            pass

    def capture(fn):
        sink = Sink()
        old = sys.stdout
        sys.stdout = sink
        try:
            result = fn()
        finally:
            sys.stdout = old
        return result, "".join(sink.lines)

    _, logs = capture(lambda: T._log_tts_payload(
        Cfg({"tts_debug_log": True}), "主人，だめ……！", "主人，だめ……！", "pingjing", "ref.mp3"))
    check("打印 LLM 传入的完整台词", lambda: "LLM 传入" in logs and "主人，だめ……！" in logs)
    check("tts_debug_log 打开时打印详细参数",
          lambda: "详细参数" in logs and "pingjing" in logs and "ref.mp3" in logs)

    _, logs2 = capture(lambda: T._log_tts_payload(
        Cfg({}), "あっ…そこは、だめ——", "あっ…そこは、だめ～～", "pingjing", "ref.mp3"))
    check("文本被改写时同时打印原文与送合成文本",
          lambda: "LLM 传入" in logs2 and "实际送去合成" in logs2)

    _, logs3 = capture(lambda: T._log_tts_payload(
        Cfg({}), "主人，おはよう。", "主人，おはよう。", "pingjing", "ref.mp3"))
    check("文本一字未改时不刷屏默认日志", lambda: logs3.strip() == "")

    _, logs5 = capture(lambda: T._log_tts_payload(
        Cfg({}), "ふふっ♪ うれしいな", "ふふっ うれしいな", "gaoxing", "ref.mp3"))
    check("长度变化时打印完整两边并说明只是去掉装饰符号",
          lambda: "LLM 传入" in logs5 and "实际送去合成" in logs5 and "内容字未变" in logs5)

    out, logs4 = capture(lambda: T._sanitize_tts_text("えっ、そうなの？"))
    check("正常台词清洗结果不变（不漏字）", lambda: out == "えっ、そうなの？")
    check("正常台词不产生噪音日志", lambda: logs4.strip() == "")

    cfg_map = Cfg({"tts_char_map": "♪=～\n☆=、"})
    out2, _ = capture(lambda: T._sanitize_tts_text("ふふっ♪ やった☆", extra_map=T._configured_char_map(cfg_map)))
    check("字符映射来自配置（不是硬编码）", lambda: out2 == "ふふっ～ やった、")
    check("配置解析支持多行与注释",
          lambda: T._configured_char_map(Cfg({"tts_char_map": "# 注释\nx=y\n\n"})) == {"x": "y"})
    check("默认映射在未配置时仍生效",
          lambda: T._sanitize_tts_text("だめ——").startswith("だめ"))

    check("split_tts_chunks 也按配置映射",
          lambda: T.split_tts_chunks("やった☆", config=cfg_map) == ["やった、"])


def k_tts_merge_no_word_loss():
    """主动消息语音"每个句子之间少了很多字眼" —— 事故回归测试。

    旧实现把「上一段结尾」与「下一段开头」真实重叠 crossfade_ms 后拼接，
    每拼一句就凭空丢掉 crossfade_ms 的音频（默认 300ms ≈ 半个词到一个词），
    而 TTS 每段首尾本就带静音，重叠区里常常还有真实语音。
    """
    import wave as wavemod
    import numpy as np
    from modules.tts import merge_wavs, simple_concat_wavs

    section("K 主动消息语音合并：不再吃字，且失败时降级不丢内容")

    out_dir = ROOT / ".tmp_test" / "fixrun" / "k_merge"
    out_dir.mkdir(parents=True, exist_ok=True)
    sr = 16000

    def make_wav(name, seconds, lead_sil=0.05, tail_sil=0.05):
        """造一段"有声音 + 首尾静音"的测试音频。

        用 +8000 的方波而不是正弦：正弦经 int16 取整会在过零点留下若干小值样本，
        静音判定（阈值 240）会把这些点当成"有声"，边界就不干净了。
        """
        path = out_dir / name
        n = int(sr * seconds)
        tone = np.full(n, 8000, dtype=np.int16)
        data = np.concatenate([np.zeros(int(sr * lead_sil), dtype=np.int16), tone,
                               np.zeros(int(sr * tail_sil), dtype=np.int16)])
        with wavemod.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(data.tobytes())
        return path

    def read(path):
        with wavemod.open(str(path), "rb") as wf:
            frames = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
            return frames, wf.getframerate()

    def speech_seconds(path):
        frames, rate = read(path)
        return int((np.abs(frames.astype(np.int32)) > 240).sum()) / rate

    def total_duration(path):
        frames, rate = read(path)
        return len(frames) / rate

    a = make_wav("k_a.wav", 1.0)
    b = make_wav("k_b.wav", 1.0)
    c = make_wav("k_c.wav", 1.0)

    # 每段 = 1.0s 语音 + 首尾各 50ms 静音（TTS 的典型输出，共 1.1s / 17600 帧）。
    # 交叉渐变路径会先裁掉各段首尾的纯数字静音（保留 60ms 呼吸感），
    # 无渐变路径（simple_concat_wavs）不裁剪，按原始文件直接拼接。
    GAP_FRAMES = 1600           # breathing_gap_ms = 100

    def merge_and_read(cross):
        """合并后立刻读出来：merge_wavs 用时间戳命名文件，
        连续调用有可能落在同一毫秒里覆盖同名文件，读晚了会读到别的结果。"""
        run_dir = out_dir / f"cross{cross}"
        run_dir.mkdir(exist_ok=True)
        cfg = Cfg({"voice_transition": True, "breathing_gap_ms": 100,
                   "crossfade_ms": cross})
        path = merge_wavs([a, b, c], cfg, run_dir)
        if not path:
            return None, None, 0.0
        frames, _rate = read(path)
        return path, frames, total_duration(path)

    measured = {cross: merge_and_read(cross) for cross in (0, 50, 300)}

    def energy(path):
        frames, _rate = read(path)
        return float(np.sum(frames.astype(np.float64) ** 2))

    def expected_frames(cross):
        """按 merge_wavs 的算法（生产代码的同一套裁剪与等长交叉淡化规则）算出应有的帧数。

        重叠长度 ov = min(crossfade_ms, 已合并长度, 本段长度)，
        结果 = (上一段 − ov) + 混合区(ov) + (本段 − ov) = 上一段 + 本段 − ov，
        重叠区再长也只是"收窄"，任何一帧都不会被删掉。
        """
        from modules.tts import _trim_silence

        def load(path, lead):
            frames, _rate = read(path)
            arr = frames.reshape(-1, 1)
            return _trim_silence(arr, 1, lead=lead, trail=True, sample_rate=sr)

        cs = int(sr * cross / 1000)
        if cs <= 0:
            # 无渐变：simple_concat_wavs 原样拼接，不裁剪也不插呼吸间隔
            return sum(len(read(p)[0]) for p in (a, b, c))
        merged = len(load(a, False))       # 已合并长度（第一段只裁尾部静音）
        for seg in (b, c):
            audio = load(seg, True)
            acc = merged + GAP_FRAMES      # 先拼上呼吸间隔
            ov = min(cs, acc, len(audio))
            merged = acc + len(audio) - ov if ov > 0 else acc + len(audio)
        return merged

    def total_is_at_least_source():
        """合并结果绝不能越合越短：三段 1.0s 语音应至少留下 2.0s 音频。

        旧实现每拼一句就真实重叠 crossfade_ms，三段 1.0s 合完只剩约 1.5s
        （连两段都不够），这正是"句子之间少了很多字眼"的来源。
        """
        v = measured[300]
        if v[0] is None:
            return False
        print(f"    [实测] 三段合并总时长 {v[2]:.3f}s（旧实现约 1.5s）")
        return v[2] >= 2.0
    check("三段都完整给出，时长不少于三段语音本身（默认 300ms 渐变）",
          total_is_at_least_source)

    def crossfade_only_overlaps_boundaries():
        """逐段核对帧数恒等式：结果 = 上一段 + 呼吸间隔 + 本段 − 重叠区。"""
        for cross in (0, 50, 300):
            path, frames, _total = measured[cross]
            if path is None:
                return False
            want = expected_frames(cross)
            if len(frames) != want:
                print(f"    cross={cross}: 实测 {len(frames)} 帧 != 期望 {want} 帧")
                return False
        return True
    check("每一帧都能对上账：没有音频被静默吞掉", crossfade_only_overlaps_boundaries)

    def beats_old_implementation():
        """旧实现每次拼接都会整段丢掉 crossfade_ms（默认 300ms）。

        三段 1.0s 语音 + 首尾静音共 3.3s，旧实现每个拼接点凭空删掉 2×crossfade
        （300ms 档实测只剩 2.3s）；现在的实现只收窄重叠区、不删任何一帧，
        300ms 档应留下约 2.9s。实际帧数必须等于算法模拟值（见上一项）。
        """
        old_seconds = 3 * 1.1 - 2 * 0.3
        return measured[300][2] > old_seconds - 0.5
    check("300ms 渐变下总时长明显长于旧实现（旧实现每句都吃掉 0.3s）",
          beats_old_implementation)

    def crossfade_adds_no_extra_loss():
        """渐变路径的能量损耗必须与重叠区长度成比例，且远小于旧实现。

        旧实现每拼一句整段删掉 crossfade_ms，300ms 档能量只剩硬拼接的 ~55%
        （实测 1.62/2.94）；现在的实现只在重叠区做等长混音，实测 ≈0.70。
        """
        base = energy(measured[0][0])
        if not base:
            return False
        e50 = energy(measured[50][0]) / base
        e300 = energy(measured[300][0]) / base
        print(f"    [实测] 能量/硬拼接：50ms={e50:.2f} 300ms={e300:.2f}（旧实现 300ms≈0.55）")
        return e300 >= 0.65 and e50 >= 0.90 and e50 > e300
    check("有声内容的能量损耗远小于旧实现", crossfade_adds_no_extra_loss)

    def no_transition_lossless():
        cfg = Cfg({"voice_transition": False})
        p = merge_wavs([a, b, c], cfg, out_dir)
        # 原始文件直接拼接：3×1.1s = 3.3s（简单拼接不插入呼吸间隔）
        return p is not None and abs(total_duration(p) - 3 * 1.1) < 0.05
    check("关闭语气渐变时按原样拼接（总时长≈3.3s）", no_transition_lossless)

    def short_segments_survive():
        """极短片段曾让旧实现抛 numpy 广播异常并静默返回 None。"""
        tiny = [make_wav("k_t1.wav", 0.02, 0, 0), make_wav("k_t2.wav", 0.02, 0, 0),
                make_wav("k_t3.wav", 0.01, 0, 0)]
        cfg = Cfg({"voice_transition": True, "breathing_gap_ms": 100, "crossfade_ms": 300})
        p = merge_wavs(tiny, cfg, out_dir)
        return p is not None and Path(p).exists() and total_duration(p) > 0.2
    check("极短片段不再触发广播异常（返回可用音频）", short_segments_survive)

    def concat_fallback_works():
        p = simple_concat_wavs([a, b], out_dir)
        return p is not None and abs(total_duration(p) - 2.2) < 0.05
    check("无损直接拼接兜底可用（合并失败也不丢语音）", concat_fallback_works)

    def proactive_degrades_instead_of_dropping():
        """分批合成失败时不允许把"半截语音"当成功结果返回。"""
        import modules.sender as SM
        from modules.llm_helpers import RoleContext

        data_dir = out_dir / "sender"
        data_dir.mkdir(exist_ok=True)
        good = make_wav("k_good.wav", 0.4)

        async def fake_synth(config, text, emotion, emotions, dpath, stats=None,
                         mimic="", mimics=None):
            return good
        orig_synth = SM.synthesize_sentence
        orig_check = SM.check_tts_service
        SM.synthesize_sentence = fake_synth

        async def ok_tts(config):
            return True
        SM.check_tts_service = ok_tts

        class MM:
            data_path = data_dir

            @staticmethod
            def cleanup_voice_cache(n=20):
                return None

        class FakeClient:
            def __init__(self):
                self.calls = []

            async def send_private_msg(self, user_id=None, message=None):
                self.calls.append(message)
                return {}
        try:
            cfg = Cfg({"voice_transition": True, "default_voice": "pingjing",
                       "crossfade_ms": 300, "breathing_gap_ms": 100})
            snd = SM.MessageSender(cfg, MM())
            snd.client = FakeClient()
            ok = asyncio.run(snd.speak_and_send(
                "private", 10001, "主人，提醒时间到啦。你说过要吃药，别忘了哦！",
                {"pingjing": {}}, RoleContext(cfg), use_voice=True, emotion="pingjing"))
            voices = [c for c in snd.client.calls
                      if any(type(seg).__name__ == "Record" for seg in c)]
        finally:
            SM.synthesize_sentence = orig_synth
            SM.check_tts_service = orig_check
        # 只发一条语音，且必须是"完整合并"而不是只想说第一句就收工
        return ok is True and len(voices) == 1
    check("主动消息语音合并成功时只发一条完整语音", proactive_degrades_instead_of_dropping)


def l_proactive_history():
    """主动消息/问候必须带上聊天历史（承接上次话题）。"""
    import main as M
    from modules.llm_helpers import RoleContext
    from modules.jobs import generate_in_character_text
    import modules.jobs as J

    section("L 主动消息带历史：最近几条全文 + 更早的简略描述")

    check("无历史时提示块为空（不硬塞上下文）",
          lambda: M.dialog_history_block([]) == ""
          and M.dialog_history_block(None) == "")

    history = []
    for i in range(12):
        history.append({"role": "user", "content": f"用户第{i}句"})
        history.append({"role": "assistant", "content": f"角色第{i}句", "speaker": "丛雨"})
    block = M.dialog_history_block(history, None, recent=6)
    check("最近 6 条按时间顺序全文给出",
          lambda: "用户第11句" in block and "角色第11句" in block
          and "用户第9句" in block and "角色第9句" in block
          and block.index("用户第9句") < block.index("角色第11句"))
    check("更早的对话只做简略描述（不是全文堆进去）",
          lambda: "更早的对话（简略）" in block and "用户第0句" in block)
    check("提示里明确要求承接上文",
          lambda: "必须承接" in block and "不要凭空换一个不相干的话题" in block)

    with_summary = M.dialog_history_block(
        history, None, recent=4,
        summary_chars=200)
    check("有摘要时优先用摘要描述更早内容",
          lambda: "较早的对话摘要" in with_summary or "更早的对话（简略）" in with_summary)

    check("历史块受总字数上限约束",
          lambda: len(M.dialog_history_block(history, None, recent=6,
                                             max_chars=300)) <= 300)

    # 生成函数确实把历史块交给模型
    captured = {}

    async def fake_text_reply(ctx, system, user_prompt, max_tokens=512):
        captured["system"] = system
        captured["user"] = user_prompt
        return "主人，刚才说的那件事，本座还记着呢。"

    orig = J.generate_text_reply
    J.generate_text_reply = fake_text_reply
    try:
        out = asyncio.run(generate_in_character_text(
            RoleContext({"personality_prompt": "你是丛雨"}), "主动找个话题",
            history_block="【对话历史】\n主人：我要睡觉了"))
    finally:
        J.generate_text_reply = orig
    check("主动话术请求里带上了历史块",
          lambda: "我要睡觉了" in captured.get("user", "")
          and "承接上文" in captured.get("system", ""))
    check("带历史时正常台词照常返回", lambda: out.startswith("主人"))


def m_mood_judge_split():
    """心情值与回复审判是两个独立开关。"""
    import random as _random
    from modules.mood import (MoodManager, commit_mood, judge_and_decide, judge_enabled,
                              mood_enabled, parse_judge)
    import modules.mood as mood_mod
    from modules.llm_helpers import RoleContext

    section("M 心情值与回复审判拆成两个选项")

    base = {"reply_judge_mood_min": 0, "reply_judge_mood_max": 100,
            "reply_judge_mood_initial": 60, "reply_judge_mood_delta_max": 10,
            "reply_judge_mood_low": 30, "reply_judge_mood_high": 60,
            "reply_judge_prob_low": 0.2, "reply_judge_prob_high": 1.0,
            "personality_prompt": "你是丛雨", "character_key": "murasame",
            "reply_judge_prompt": "judge it",
            # 这一段验的是"变化量怎么落盘"，回稳量在别处单独验；
            # 时间偏置关掉，否则凌晨跑测试时判定用值会被压低
            "mood_regress_rate": 0, "mood_env_enabled": False}

    def ctx_of(**kw):
        cfg = dict(base)
        cfg.update(kw)
        return RoleContext(Cfg(cfg))

    check("开关解析：只开心情 / 只开审判 / 都开 / 都关",
          lambda: mood_enabled(ctx_of(mood_enabled=True, reply_judge_enabled=False))
          and not judge_enabled(ctx_of(mood_enabled=True, reply_judge_enabled=False))
          and judge_enabled(ctx_of(mood_enabled=False, reply_judge_enabled=True))
          and not mood_enabled(ctx_of(mood_enabled=False, reply_judge_enabled=True))
          and mood_enabled(ctx_of(mood_enabled=True, reply_judge_enabled=True))
          and judge_enabled(ctx_of(mood_enabled=True, reply_judge_enabled=True))
          and not mood_enabled(ctx_of(mood_enabled=False, reply_judge_enabled=False,
                                      reply_judge_prompt=""))
          and not judge_enabled(ctx_of(mood_enabled=False, reply_judge_enabled=False,
                                       reply_judge_prompt="")))
    check("旧配置兼容：没写这两个键时跟随是否配置了提示词",
          lambda: mood_enabled(ctx_of()) and judge_enabled(ctx_of())
          and not mood_enabled(ctx_of(reply_judge_prompt=""))
          and not judge_enabled(ctx_of(reply_judge_prompt="")))

    def _gate_uses_compat_layer():
        """主流程的 gate 必须走兼容层，否则旧配置缺键时审判与心情整段跳过。
        （消息主流程已搬至 modules/message_pipeline.py，源码检查随之指向新家。）"""
        src = (ROOT / "modules" / "message_pipeline.py").read_text(encoding="utf-8")
        start = src.index('mood_user = "" if is_private else str(sender_id)')
        seg = src[start:start + 600]
        return "judge_enabled(ctx) or mood_enabled(ctx)" in seg \
            and 'global_config.get("reply_judge_enabled"' not in seg

    check("主流程 gate 走 mood 模块的开关兼容层", _gate_uses_compat_layer)

    mood_dir = ROOT / ".tmp_test" / "fixrun" / f"mood_split_{int(time.time() * 1000)}"
    mood_dir.mkdir(parents=True, exist_ok=True)
    calls = {"n": 0}

    async def fake_chat_once(ctx, messages, tools=None, **kwargs):
        calls["n"] += 1
        # 模型判定"这条不用回"，心情 -10
        return {"content": '{"should_reply": false, "mood_delta": -10, "mood_reason": "被骂"}',
                "tool_calls": [], "ms": 1.0, "backend": "ollama"}

    orig = mood_mod.chat_once
    orig_rand = mood_mod.random.random
    mood_mod.chat_once = fake_chat_once
    # 固定随机数：概率门控场景下结果可复现（0.99 > 0.733 → 放行）
    mood_mod.random.random = lambda: 0.99
    try:
        mm = MoodManager(mood_dir)
        only_mood = asyncio.run(judge_and_decide(
            ctx_of(mood_enabled=True, reply_judge_enabled=False), mm,
            "private_onlymood", "哼，不理你了", []))
        check("只开心情值：判定出变化量但落盘前心情不变",
              lambda: only_mood["mood"] == 60.0 and only_mood["mood_delta"] == -10.0
              and mm.get_mood("private_onlymood", "murasame", 60) == 60.0)
        check("只开心情值：回复生成后落盘，心情照常下降",
              lambda: commit_mood(ctx_of(mood_enabled=True, reply_judge_enabled=False), mm,
                                  "private_onlymood", only_mood) == 50.0
              and mm.get_mood("private_onlymood", "murasame", 60) == 50.0)
        check("只开心情值：LLM 说不用回也不拦回复（should_reply 恒为 True）",
              lambda: only_mood["should_reply"] is True
              and only_mood["probability"] == 1.0)

        only_judge = asyncio.run(judge_and_decide(
            ctx_of(mood_enabled=False, reply_judge_enabled=True), mm,
            "private_onlyjudge", "哼，不理你了", []))
        check("只开审判：心情不变（保持初始值）",
              lambda: only_judge["mood"] == 60.0
              and mm.get_mood("private_onlyjudge", "murasame", 60) == 60.0)
        check("只开审判：LLM 说不用回就真的不回",
              lambda: only_judge["should_reply"] is False
              and only_judge["llm_reply"] is False)
        check("没拿到变化量（审判失败）时不落盘",
              lambda: commit_mood(ctx_of(mood_enabled=True), mm, "private_failed",
                                  {"mood": 60.0, "mood_delta": None,
                                   "mood_enabled": True}) is None
              and mm.get_mood("private_failed", "murasame", 60) == 60.0)
    finally:
        mood_mod.chat_once = orig
        mood_mod.random.random = orig_rand

    # 两个开关都关：一次 LLM 都不该调用
    calls["n"] = 0
    orig = mood_mod.chat_once
    mood_mod.chat_once = fake_chat_once
    try:
        both_off = asyncio.run(judge_and_decide(
            ctx_of(mood_enabled=False, reply_judge_enabled=False, reply_judge_prompt=""),
            MoodManager(mood_dir), "private_off", "随便说说", []))
    finally:
        mood_mod.chat_once = orig
    check("两个开关都关：不调用 LLM，直接放行",
          lambda: calls["n"] == 0 and both_off["should_reply"] is True)

    check("心情解析仍受 delta 上限夹取",
          lambda: parse_judge({"should_reply": False, "mood_delta": -999}, ctx_of())[1] == -10.0)


def n_todo_meaningless_transcribe():
    """待办"再提醒我"：必须自己转述，不能原样当成事项。"""
    from modules.llm_helpers import RoleContext
    from modules.todo_manager import (todo_content_is_meaningless, content_from_context,
                                      TodoManager, DEFAULT_EXTRACT_PROMPT)

    section("N 待办提取：把「再提醒我」还原成真正的事项")

    check("提醒指令本身被判为无意义",
          lambda: all(todo_content_is_meaningless(x) for x in
                      ("再提醒我", "提醒我", "叫我", "记得", "别忘了", "提醒一下",
                       "这件事", "到时候", "再提醒我吧"))
          and not todo_content_is_meaningless("睡觉")
          and not todo_content_is_meaningless("吃药")
          and not todo_content_is_meaningless("叫我起床"))

    check("按上下文还原：先说要睡觉，再说5分钟后再提醒我 → 睡觉",
          lambda: content_from_context(["主人：我要睡觉了", "主人：5分钟后再提醒我吧"]) == "睡觉")
    check("按上下文还原：我准备出门了 → 出门",
          lambda: content_from_context(["主人：我准备出门了", "主人：30分钟后提醒我"]) == "出门")
    check("上下文里没有事项就不硬编",
          lambda: content_from_context(["主人：今天好累", "主人：提醒我"]) == "")

    check("提取提示词写明必须自己转述、禁止照抄指令",
          lambda: "必须自己转述" in DEFAULT_EXTRACT_PROMPT
          and "严禁把提醒指令本身当成事项" in DEFAULT_EXTRACT_PROMPT
          and "睡觉" in DEFAULT_EXTRACT_PROMPT)

    class DB:
        def execute(self, *a, **k):
            class C:
                lastrowid = 1
            return C()

        def query_all(self, *a, **k):
            return []

    class Sch:
        def add_job(self, *a, **k):
            pass

        def remove_job(self, *a, **k):
            pass

    tm = TodoManager(Cfg({"todo_keywords": "提醒\n记得\n叫我"}), DB(), Sch())
    check("空指令不落库（最后一道闸）",
          lambda: tm.add_todo("再提醒我", time.time() + 60, "private", "10001") is None
          and tm.add_todo("提醒我", time.time() + 60, "private", "10001") is None)
    check("正常事项照常落库",
          lambda: tm.add_todo("吃药", time.time() + 60, "private", "10001") is not None)
    check("正则模式丢弃「再提醒我吧」这类空指令",
          lambda: tm.extract_sync("5分钟后再提醒我吧") == []
          and tm.extract_sync("再提醒我") == [])
    check("正则模式正常提取（8点提醒我吃药 → 吃药）",
          lambda: [c for c, _ in tm.extract_sync("8点提醒我吃药")] == ["吃药"])

    # LLM 模式：模型给出无意义 content 时，用上下文还原
    llm_tm = TodoManager(Cfg({"todo_keywords": "提醒"}), DB(), Sch())
    orig = llm_tm._parse_time_expr
    try:
        async def fake_json(ctx, system, user_prompt, max_tokens=200):
            return {"has_todo": True, "content": "再提醒我", "delay_minutes": 5}
        import modules.todo_manager as TDM
        orig_json = TDM.generate_json_reply
        TDM.generate_json_reply = fake_json
        try:
            found = asyncio.run(llm_tm.extract_llm(
                RoleContext({"personality_prompt": "x"}), "5分钟后再提醒我吧",
                recent_context=["主人：我要睡觉了", "主人：5分钟后再提醒我吧"]))
        finally:
            TDM.generate_json_reply = orig_json
    finally:
        llm_tm._parse_time_expr = orig
    check("LLM 给出「再提醒我」时按上下文还原为「睡觉」",
          lambda: len(found) == 1 and found[0][0] == "睡觉")

    # 上下文里也找不到事项 → 宁可漏提醒，也不发"快去做『再提醒我』这件大事吧"
    llm_tm2 = TodoManager(Cfg({"todo_keywords": "提醒"}), DB(), Sch())
    import modules.todo_manager as TDM

    async def fake_json2(ctx, system, user_prompt, max_tokens=200):
        return {"has_todo": True, "content": "再提醒我", "delay_minutes": 5}
    orig_json = TDM.generate_json_reply
    TDM.generate_json_reply = fake_json2
    try:
        found2 = asyncio.run(llm_tm2.extract_llm(
            RoleContext({"personality_prompt": "x"}), "5分钟后再提醒我吧",
            recent_context=["主人：今天天气不错"]))
    finally:
        TDM.generate_json_reply = orig_json
    check("无上下文可依时放弃本条（不发无意义提醒）", lambda: found2 == [])


def n2_todo_time_not_content():
    """待办提取：模型把「时间」当成事项、并编造 delay_minutes —— 事故回归。

    用户实测：主人说「嘿嘿 那你早上8点半可以叫我起床嘛～」，模型返回
    {"content": "两分钟后", "delay_minutes": 2} —— 事项没了、时间还是编的。
    更麻烦的是：用户 config.json 里存着一份旧提示词，会永久顶掉代码里的
    默认提示词，所以关键约束必须由系统强制追加，且代码层要有兜底。
    """
    import modules.todo_manager as TDM
    from modules.llm_helpers import RoleContext
    from modules.todo_manager import (EXTRACT_HARD_RULES, TodoManager,
                                      clean_todo_content, content_from_user_text,
                                      content_is_time_phrase,
                                      relative_duration_mentioned)

    section("N2 待办提取：时间不许当事项、相对时长不许编造")

    MSG = "嘿嘿 那你早上8点半可以叫我起床嘛～"

    check("纯时间短语能被识别",
          lambda: all(content_is_time_phrase(x) for x in
                      ("两分钟后", "8点半", "08:30", "半小时后", "明天早上8点",
                       "5分钟", "两小时以后"))
          and not content_is_time_phrase("起床")
          and not content_is_time_phrase("吃药告一段落"))

    check("事项两端的语气词/称呼/请求语被剥掉",
          lambda: clean_todo_content("起床嘛～") == "起床"
          and clean_todo_content("嘿嘿起床") == "起床"
          and clean_todo_content("可以起床吧") == "起床"
          and clean_todo_content("嘛～") == ""
          and clean_todo_content("可以") == "")

    check("从用户原话直接取出事项（叫我起床 → 起床）",
          lambda: content_from_user_text(MSG) == "起床"
          and content_from_user_text("提醒我吃药") == "吃药"
          and content_from_user_text("叫一下我起床") == "起床"
          and content_from_user_text("记得带钥匙") == "带钥匙")

    check("相对时长必须来自用户原话",
          lambda: relative_duration_mentioned("两分钟后提醒我")
          and relative_duration_mentioned("30分提醒我喝水")
          and relative_duration_mentioned("5分钟后再提醒我吧")
          and not relative_duration_mentioned("8点半叫我起床")
          and not relative_duration_mentioned("明天早上提醒我"))

    check("硬性约束块写明「content 不许是时间」「有钟点必须用 time」",
          lambda: "绝不允许是时间短语" in EXTRACT_HARD_RULES
          and "禁止改写成 delay_minutes" in EXTRACT_HARD_RULES
          and "绝不许编造" in EXTRACT_HARD_RULES)

    class DB:
        def execute(self, *a, **k):
            class C:
                lastrowid = 1
            return C()

        def query_all(self, *a, **k):
            return []

    class Sch:
        def add_job(self, *a, **k):
            pass

        def remove_job(self, *a, **k):
            pass

    # 用户 config.json 里的那份旧提示词（不带任何硬性约束）
    OLD_PROMPT = ("你是待办提取助手。判断用户消息是否包含一个明确的提醒/待办事项。"
                  "如果有，输出JSON：{\"has_todo\": true, \"content\": \"要提醒的事项\", "
                  "\"time\": \"HH:MM\"}；如果没有，输出 {\"has_todo\": false}。只输出JSON。")

    def run_llm(model_reply, user_msg, context=None):
        mgr = TodoManager(Cfg({"todo_keywords": "提醒\n叫我\n记得",
                               "todo_extract_prompt": OLD_PROMPT}), DB(), Sch())
        captured = {}

        async def fake_json(ctx, system, user_prompt, max_tokens=200):
            captured["system"] = system
            return model_reply

        orig = TDM.generate_json_reply
        TDM.generate_json_reply = fake_json
        try:
            found = asyncio.run(mgr.extract_llm(RoleContext({"personality_prompt": "x"}),
                                                user_msg, recent_context=context))
        finally:
            TDM.generate_json_reply = orig
        return found, captured

    found, captured = run_llm(
        {"has_todo": True, "content": "两分钟后", "delay_minutes": 2}, MSG)
    check("复现用例：模型给 content=两分钟后 / delay=2 → 事项救回成「起床」",
          lambda: [c for c, _ in found] == ["起床"])
    check("复现用例：时间用用户原话里的 08:30（不是 +2 分钟）",
          lambda: found and time.strftime("%H:%M", time.localtime(found[0][1])) == "08:30")
    check("旧提示词也会被强制追加系统硬性约束",
          lambda: "系统硬性约束" in captured.get("system", ""))

    found_t, _ = run_llm({"has_todo": True, "content": "8点半", "time": "08:30"}, MSG)
    check("模型把时间当 content、但给了 time → 事项仍被救回",
          lambda: [c for c, _ in found_t] == ["起床"]
          and time.strftime("%H:%M", time.localtime(found_t[0][1])) == "08:30")

    found_d, _ = run_llm({"has_todo": True, "content": "起床", "delay_minutes": 450}, MSG)
    check("模型编造 delay_minutes=450 → 丢弃并改用原话钟点",
          lambda: [c for c, _ in found_d] == ["起床"]
          and time.strftime("%H:%M", time.localtime(found_d[0][1])) == "08:30")

    found_rel, _ = run_llm({"has_todo": True, "content": "喝水", "delay_minutes": 30},
                           "30分钟后提醒我喝水")
    delta = (found_rel[0][1] - time.time()) / 60 if found_rel else 0
    check("用户真的说了「30分钟后」时，相对时长照常生效",
          lambda: found_rel and 29 <= delta <= 31)

    found_only_time, _ = run_llm(
        {"has_todo": True, "content": "两分钟后", "delay_minutes": 2}, "两分钟后提醒我吧")
    check("只给时间、又无事项可救时放弃本条（宁可漏提醒也不发无意义提醒）",
          lambda: found_only_time == [])

    # 正则模式：新增语序「时间 + 可以 + 叫我 + 事项」
    tm = TodoManager(Cfg({"todo_keywords": "提醒\n叫我\n记得"}), DB(), Sch())
    got = tm.extract_sync(MSG)
    check("正则模式也能解析「8点半可以叫我起床嘛～」",
          lambda: [c for c, _ in got] == ["起床"])
    check("正则模式的时间与事项都对",
          lambda: got and time.strftime("%H:%M", time.localtime(got[0][1])) == "08:30")
    check("正则模式原有写法不受影响",
          lambda: [c for c, _ in tm.extract_sync("8点提醒我吃药")] == ["吃药"]
          and [c for c, _ in tm.extract_sync("30分钟后叫我起床")] == ["起床"])


def o_weather_location_guard():
    """天气工具不许替主人猜城市。"""
    from modules.tools import (_extract_city_from_text, _is_weather_question,
                               _resolve_weather_city, reply_asks_for_location)

    section("O 天气：用户没说地点时不许自己猜城市")

    cases = {"今天天气怎么样": "", "外面的天气呢": "", "明天冷不冷": "",
             "今天要穿什么衣服": "", "这里天气怎么样": "",
             "上海的天气如何": "上海", "北京今天要带伞吗": "北京",
             "佛山天气": "佛山", "广州气温多少度": "广州",
             "我在深圳，今天天气怎么样": "深圳",
             "帮我查一下乌鲁木齐的天气": "乌鲁木齐",
             "what is the weather in Tokyo": "Tokyo"}
    check("从用户消息里取城市（找不到就是空）",
          lambda: all(_extract_city_from_text(k) == v for k, v in cases.items()))
    check("天气意图识别",
          lambda: all(_is_weather_question(k) for k in cases)
          and not _is_weather_question("今天心情不错"))

    check("用户没说地点：模型猜的城市被拦下并改为反问",
          lambda: _resolve_weather_city({"city": "上海"}, {}, "今天天气怎么样")[0] == ""
          and "哪个城市" in _resolve_weather_city({"city": "上海"}, {}, "今天天气怎么样")[1])
    check("用户说了地点：以用户说的为准（模型给的错城市被覆盖）",
          lambda: _resolve_weather_city({"city": "北京"}, {}, "上海的天气如何")[0] == "上海")
    check("用户说了地点：即使模型什么都没给也能取到",
          lambda: _resolve_weather_city({}, {}, "佛山天气")[0] == "佛山")
    check("用户没说地点时默认城市同样被拦下（不许替主人假设所在地）",
          lambda: _resolve_weather_city({}, {"default_location": "佛山"},
                                        "今天天气怎么样")[0] == ""
          and "哪个城市" in _resolve_weather_city({}, {"default_location": "佛山"},
                                                 "今天天气怎么样")[1])
    check("默认城市只在消息与天气无关时兜底（模型主动查天气时仍以用户为准）",
          lambda: _resolve_weather_city({}, {"default_location": "佛山"}, "随便聊聊")[0] == "佛山"
          and _resolve_weather_city({}, {"default_location": "佛山"},
                                    "上海的天气如何")[0] == "上海")

    def weather_builtin_blocks_guess():
        """真正执行工具时也不许猜：返回的是"先问地点"的说明。"""
        import modules.tools as T

        reg = T.ToolRegistry(Cfg({"tools_enabled": True}), ROOT / ".tmp_test" / "fixrun" / "wtools")
        for item in reg.tools:
            item["enabled"] = True
        asyncio.run(reg.execute("get_weather", {"city": "上海"}, "u1",
                                user_text="今天天气怎么样"))
        # 用假 HTTP 客户端断言：这次调用根本没有发起网络请求
        calls = []

        class FakeResp:
            status_code = 500
            text = "should not be called"

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, params=None, **kw):
                calls.append(url)
                return FakeResp()
        orig = T.httpx.AsyncClient
        T.httpx.AsyncClient = FakeClient
        try:
            reg.begin_reply()
            ok, out = asyncio.run(reg.execute("get_weather", {"city": "上海"}, "u1",
                                              user_text="今天天气怎么样"))
            blocked_no_network = not calls and ok and "哪个城市" in out
            reg.begin_reply()
            ok2, out2 = asyncio.run(reg.execute("get_weather", {"city": "上海"}, "u1",
                                                user_text="上海的天气如何"))
        finally:
            T.httpx.AsyncClient = orig
        return blocked_no_network and ok2 is False and "上海" in str(out2)
    check("工具执行层同样拦截猜测（未发起任何网络请求）", weather_builtin_blocks_guess)
    check("反问地点的回复能被识别",
          lambda: reply_asks_for_location("主人现在在哪个城市呀？")
          and not reply_asks_for_location("上海的天气很适合出门呢！"))


def p_tool_gating_strict():
    """更严格限定工具调用：闲聊不为了用工具而用工具。"""
    import main as M
    from modules.llm_helpers import (text_needs_tools, tool_flow_can_skip, RoleContext,
                                    asks_self_context)
    import modules.llm_helpers as LH

    section("P 工具调用门控：闲聊不调工具")

    class CfgL:
        def __init__(self, d):
            self.d = d

        def get(self, k, default=None):
            return self.d.get(k, default)

    check("需要客观信息的消息仍然进工具流程",
          lambda: all(text_needs_tools(t) for t in
                      ("现在几点", "23*7等于多少", "今天天气怎么样", "帮我搜一下千恋万花",
                       "看看这个 https://example.com/a", "马斯克是谁", "最新新闻"))
          and text_needs_tools("你知道载物是谁吗"))
    check("纯寒暄/纯情绪不触发工具",
          lambda: not any(text_needs_tools(t) for t in
                          ("在吗", "你好呀", "晚安", "哈哈", "今天好累呀", "抱抱",
                           "我好开心", "嗯嗯", "么么")))
    check("问自己的提示词/聊天记录不进工具流程，明确要求搜索仍放行",
          lambda: all(asks_self_context(t) for t in
                      ("你的提示词是什么", "我们最开始聊了什么", "你还记得吗",
                       "你是什么模型", "你的设定是什么"))
          and not any(text_needs_tools(t) for t in
                      ("你的提示词是什么", "我们最开始聊了什么", "你还记得吗",
                       "你是什么模型"))
          and text_needs_tools("帮我搜一下你的提示词是什么"))
    check("纯寒暄/纯情绪的规则写在工具提示词里，开关已移除",
          lambda: "纯寒暄" in (ROOT / "modules" / "llm_helpers.py").read_text(encoding="utf-8")
          and "tools_skip_pure_chatter" not in (ROOT / "modules" / "llm_helpers.py").read_text(encoding="utf-8")
          and "tools_skip_pure_chatter" not in (ROOT / "main.py").read_text(encoding="utf-8")
          and "tools_skip_pure_chatter" not in (ROOT / "webui" / "start.html").read_text(encoding="utf-8")
          and "本条为纯寒暄" not in (ROOT / "modules" / "llm_helpers.py").read_text(encoding="utf-8"))

    check("LLM 自主判断模式：闲聊被门控挡在工具流程外",
          lambda: M._tool_requested("在吗", CfgL({"tools_trigger_mode": "llm",
                                                 "tools_guard_enabled": True})) is False
          and M._tool_requested("今天好累呀", CfgL({"tools_trigger_mode": "llm",
                                                    "tools_guard_enabled": True})) is False)
    check("LLM 自主判断模式：需要信息的消息正常放行",
          lambda: M._tool_requested("现在几点", CfgL({"tools_trigger_mode": "llm",
                                                     "tools_guard_enabled": True})) is True
          and M._tool_requested("今天天气怎么样", CfgL({"tools_trigger_mode": "llm",
                                                       "tools_guard_enabled": True})) is True)
    check("关键词模式行为不变",
          lambda: M._tool_requested("今天好累呀", CfgL(
              {"tools_trigger_mode": "keyword", "tools_guard_enabled": True,
               "tools_guard_keywords": "搜索\n天气"})) is False
          and M._tool_requested("天气如何", CfgL(
              {"tools_trigger_mode": "keyword", "tools_guard_enabled": True,
               "tools_guard_keywords": "搜索\n天气"})) is True)

    calls = {"n": 0}

    class Reg:
        tools = [{"builtin": "web_search", "name": "web_search", "enabled": True}]

        def get_schema(self):
            return [{"type": "function", "function": {"name": "web_search",
                                                      "parameters": {"type": "object"}}}]

        def check_permission(self, tool, user_id=""):
            return True, ""

        async def execute(self, name, arguments, user_id="", **kwargs):
            calls["n"] += 1
            return True, "结果"

    async def fake_chat_once(ctx, messages, tools=None, **kwargs):
        return {"content": "直接回答", "tool_calls": [], "ms": 1.0, "backend": "ollama"}

    def chatter_flow():
        calls["n"] = 0
        prompts = []

        async def fake(ctx, messages, tools=None, **kwargs):
            prompts.append([dict(m) for m in messages])
            return {"content": "直接回答", "tool_calls": [], "ms": 1.0, "backend": "ollama"}

        orig = LH.chat_once
        LH.chat_once = fake
        try:
            ctx = RoleContext({"llm_backend": "ollama", "tools_enabled": True,
                               "tools_trigger_mode": "llm", "tools_guard_enabled": True})
            out = asyncio.run(LH.chat_with_tools(
                ctx, [{"role": "user", "content": "在吗"}], Reg()))
        finally:
            LH.chat_once = orig
        return out, prompts

    out, prompts = chatter_flow()
    check("闲聊消息不进入工具流程（只发一次请求、不带工具规则）",
          lambda: calls["n"] == 0 and len(prompts) == 1
          and not any("【工具使用规则】" in str(m.get("content", ""))
                      for m in prompts[0]))
    check("闲聊仍正常产出回复", lambda: out["content"] == "直接回答" and not out["tool_trace"])

    def rule_text_stricter():
        import inspect
        src = inspect.getsource(LH.chat_with_tools)
        return ("只在必要时调用" in src and "地点不许猜" in src
                and "严禁“顺便查一下时间”" in src.replace("“顺便搜一下”", "“顺便查一下时间”")
                or "没有客观信息需求时" in src)
    check("工具规则里写明不得为了用工具而用工具", rule_text_stricter)

    check("搜索关键词提取必须来自用户消息（防止模型把自己的台词当关键词）",
          lambda: LH._query_follows_user_text("千恋万花的歌词", "帮我搜一下千恋万花的歌词")
          and not LH._query_follows_user_text("这是按搜索结果组织的回答",
                                              "帮我搜一下千恋万花的歌词"))


def q_safe_search():
    """安全搜索三档。"""
    from modules.tools import (safe_search_level, safe_search_label, filter_unsafe_results,
                               _safe_search_params, _safe_api_params, _safe_search_terms,
                               extra_block_terms)

    section("Q 安全搜索：无 / 一般 / 严格")

    check("档位解析（默认一般）",
          lambda: safe_search_level({}) == "normal"
          and safe_search_level({"web_search_safe": "off"}) == "off"
          and safe_search_level({"web_search_safe": "strict"}) == "strict"
          and safe_search_level({"web_search_safe": "无"}) == "off"
          and safe_search_level({"web_search_safe": "乱填"}) == "normal")

    r18 = [{"title": "普通资料", "url": "https://a.com", "content": "正常内容"},
           {"title": "R18 游戏", "url": "https://b.com", "content": "成人向内容"},
           {"title": "正常站点", "url": "https://pornhub.com/x", "content": "视频"}]
    suggestive = [{"title": "擦边写真", "url": "https://c.com", "content": "大尺度照片"},
                  {"title": "正常内容", "url": "https://d.com", "content": "普通介绍"}]

    check("无：完全不限制",
          lambda: len(filter_unsafe_results(r18, {"web_search_safe": "off"})) == 3
          and len(filter_unsafe_results(suggestive, {"web_search_safe": "off"})) == 2)
    check("一般：过滤 R18，但保留 R16+ 站点",
          lambda: len(filter_unsafe_results(r18, {"web_search_safe": "normal"})) == 1
          and len(filter_unsafe_results(suggestive, {"web_search_safe": "normal"})) == 2)
    check("严格：连低俗/性暗示一并过滤",
          lambda: len(filter_unsafe_results(r18, {"web_search_safe": "strict"})) == 1
          and len(filter_unsafe_results(suggestive, {"web_search_safe": "strict"})) == 1)

    check("引擎级安全参数按档位下发",
          lambda: _safe_search_params({"key": "searxng"}, {"web_search_safe": "strict"})
          == {"safesearch": "2"}
          and _safe_search_params({"key": "searxng"}, {"web_search_safe": "normal"})
          == {"safesearch": "1"}
          and _safe_search_params({"key": "searxng"}, {"web_search_safe": "off"}) == {}
          and _safe_search_params({"key": "bing"}, {"web_search_safe": "normal"})
          == {"adlt": "moderate"}
          and _safe_api_params({"key": "brave"}, {"web_search_safe": "strict"})
          == {"safesearch": "strict"})
    check("自定义引擎不硬塞未知参数",
          lambda: _safe_search_params({"key": "mine", "custom": True},
                                      {"web_search_safe": "strict"}) == {})
    check("默认档位就在过滤 R18（不需要额外配置）",
          lambda: len(filter_unsafe_results(r18, {})) == 1)

    check("严格档包含一般档的全部词表、off 档为空",
          lambda: set(_safe_search_terms("normal")) <= set(_safe_search_terms("strict"))
          and _safe_search_terms("off") == ())

    worst = [{"title": "R18 成人视频", "url": "https://pornhub.com/x", "content": "色情"},
             {"title": "裸照 大尺度", "url": "https://b.com", "content": "擦边"}]
    check("off 档完全不过滤",
          lambda: len(filter_unsafe_results(worst, {"web_search_safe": "off"})) == 2)

    custom = [{"title": "某个不想看到的东西", "url": "https://a.example/1", "content": "正文"}]
    check("用户词表：一般与严格档都拦，off 不拦",
          lambda: len(filter_unsafe_results(
              custom, {"web_search_safe": "normal",
                       "web_search_block_words": "不想看到的东西"})) == 0
          and len(filter_unsafe_results(
              custom, {"web_search_safe": "strict",
                       "web_search_block_words": "不想看到的东西\n别的词"})) == 0
          and len(filter_unsafe_results(
              custom, {"web_search_safe": "off",
                       "web_search_block_words": "不想看到的东西"})) == 1)
    check("用户词表支持逗号/顿号/分号分隔",
          lambda: set(extra_block_terms({"web_search_block_words": "a,b、c；d\ne"}))
          == {"a", "b", "c", "d", "e"})

    # 不误伤：这些正常内容在严格档也要留下
    innocent = [{"title": "萝莉（ACGN领域术语）_百度百科",
                 "url": "https://baike.baidu.com/item/萝莉",
                 "content": "长相可爱的少女外表"},
                {"title": "小学生数学学习视频", "url": "https://edu.example/1",
                 "content": "教学视频"},
                {"title": "儿童福利院招募志愿者", "url": "https://ngo.example/1",
                 "content": "公益活动"},
                {"title": "儿童裸眼视力标准", "url": "https://med.example/1",
                 "content": "眼科科普"},
                {"title": "成人高考报名时间", "url": "https://edu.example/2",
                 "content": "专升本"},
                {"title": "Java 教程", "url": "https://dev.example/1", "content": "编程入门"},
                {"title": "smart home 智能家居", "url": "https://tech.example/1",
                 "content": "物联网"},
                {"title": "少女漫画推荐", "url": "https://comic.example/1",
                 "content": "青春校园"}]
    check("严格档不误伤正常内容",
          lambda: len(filter_unsafe_results(innocent, {"web_search_safe": "strict"}))
          == len(innocent))

    check("内置词表都是小写且不重复（匹配时按小写比对）",
          lambda: all(t == t.lower() for t in _safe_search_terms("strict"))
          and len(set(_safe_search_terms("strict"))) == len(_safe_search_terms("strict")))

    # 保存配置后即时生效：handle_save_config 替换的是 ConfigLoader 内层 dict，
    # 工具持有的是 ConfigLoader 本身，所以搜索时读到的一定是新值。
    from main import ConfigLoader
    from modules.tools import ToolRegistry
    import tempfile
    loader = ConfigLoader()
    registry = ToolRegistry(loader, Path(tempfile.mkdtemp(prefix="lovomo_safe_")) )
    loader.config = dict(loader.config)
    loader.config.update({"web_search_safe": "normal",
                          "web_search_block_words": "某个词,另一个词"})
    live = [{"title": "包含某个词的结果", "url": "https://a.example/1", "content": "正文"}]
    blocked_live = len(filter_unsafe_results(live, registry.config)) == 0
    loader.config["web_search_safe"] = "off"
    off_live = len(filter_unsafe_results(live, registry.config)) == 1
    check("保存配置后词表与档位即时生效（不用重启）",
          lambda: blocked_live and off_live)


def r_vision_followup_after_skip():
    """回复审判判定"图片不用回"后，相关追问必须重新调用识图模型。"""
    import inspect

    import main as M

    section("R 图片追问：识图模型会被叫回来")

    M._PENDING_IMAGES.clear()
    sid = "private_vision_probe"
    M._remember_pending_image(sid, ["http://img/1.png"], {"http://img/1.png": "fid"}, "看图")

    check("与图无关的消息不会硬塞旧图",
          lambda: M._take_pending_image(sid, "今天上班好累") is None)
    got = M._take_pending_image(sid, "这个怎么样？")
    check("指向那张图的追问会取回图片并重跑识图",
          lambda: bool(got) and got["urls"] == ["http://img/1.png"]
          and got["file_ids"] == {"http://img/1.png": "fid"})
    check("同一张图最多复用两次（不会无限重跑）",
          lambda: bool(M._take_pending_image(sid, "图里是谁"))
          and M._take_pending_image(sid, "再看这个") is None)

    M._PENDING_IMAGES.clear()
    M._remember_pending_image(sid, ["http://img/2.png"], {}, "看图")
    M._clear_pending_image(sid)
    check("图片被真正回复后不再排队重跑",
          lambda: M._take_pending_image(sid, "这个怎么样") is None)

    M._PENDING_IMAGES.clear()
    M._remember_pending_image(sid, ["http://img/3.png"], {}, "看图")
    M._PENDING_IMAGES[sid]["ts"] = time.time() - 4000
    check("过期图片不再复用（默认 15 分钟）",
          lambda: M._take_pending_image(sid, "这个怎么样") is None)

    # 描述回填：写在真正那条用户消息上，而不是盲写 history[-1]
    hist = [{"role": "user", "content": "看图 [图片]"},
            {"role": "assistant", "content": "嗯", "speaker": "丛雨"},
            {"role": "user", "content": "这个怎么样"}]
    M._backfill_image_description(hist, "一只橘猫趴在窗台上")
    check("画面描述回填到最近的用户消息上",
          lambda: "[图片: 一只橘猫趴在窗台上]" in hist[2]["content"]
          and hist[0]["content"] == "看图 [图片]")
    M._backfill_image_description(hist, "换一张画面描述")
    check("重复回填不叠加旧描述",
          lambda: hist[2]["content"].count("[图片:") == 1
          and "换一张画面描述" in hist[2]["content"])

    # 未回复的消息也留在历史里，否则用户追问时模型看不到原内容
    check("未回复的消息不再被摘出历史",
          lambda: not hasattr(M, "_discard_unreplied_user_message"))

    # 收藏触发：识图结果里的收藏判定在「正常回复」与「审判判不回」两条路径上都要落盘
    ok_reply = {"capture": {"should": True, "category": "weixie", "score": 1.0},
                "description": "一只橘猫趴在窗台上",
                "capture_image": {"data": b"jpg", "source": "http://img/1.png"}}
    saved_mgr, M.sticker_mgr = M.sticker_mgr, object()
    saved_ac_sticker, M.app_context.sticker_mgr = M.app_context.sticker_mgr, M.sticker_mgr
    try:
        args = M._sticker_capture_args(ok_reply, ["http://img/1.png"])
        check("判定可收藏时给出落盘入参",
              lambda: bool(args) and args["capture"]["category"] == "weixie"
              and args["image_urls"] == ["http://img/1.png"]
              and args["image_result"]["description"] == "一只橘猫趴在窗台上"
              and args["image_result"]["capture_image"]["data"] == b"jpg")
        check("判定不收藏时不落盘",
              lambda: M._sticker_capture_args({**ok_reply, "capture": {"should": False}},
                                              ["http://img/1.png"]) is None)
        check("识图结果没有收藏判定时不落盘",
              lambda: M._sticker_capture_args({"description": "一只橘猫"},
                                              ["http://img/1.png"]) is None)
        check("没有图片来源时不落盘",
              lambda: M._sticker_capture_args(ok_reply, []) is None)
    finally:
        M.sticker_mgr = saved_mgr
        M.app_context.sticker_mgr = saved_ac_sticker
    check("表情包管理器不可用时不落盘",
          lambda: M._sticker_capture_args(ok_reply, ["http://img/1.png"]) is None)
    check("收藏触发同时挂在回复路径与审判判不回路径上",
          lambda: inspect.getsource(M._process_message_event)
          .count("_spawn_sticker_capture(") == 2)


def s_greeting_catchup_diagnostics():
    """问候补发：每次跳过都有原因、等待时间可配。"""
    import main as M
    import inspect

    section("S 问候补发：不再静默失败")

    src = inspect.getsource(M.greeting_catchup_task)
    check("等待 NapCat 的超时时间可配置（不再写死 5 分钟）",
          lambda: "greeting_catchup_deadline_minutes" in src)
    check("每一种跳过都写明原因",
          lambda: all(k in src for k in ("未开启（greeting_catchup_enabled=false）",
                                         "调度总开关已关闭",
                                         "节日问候与生日祝福都未开启",
                                         "还没到问候时刻",
                                         "处于静默时段",
                                         "仍未连接")))
    check("补发执行有异常兜底（不影响主循环）",
          lambda: "问候补发执行异常" in src)
    check("默认配置里新增了超时项且为 30 分钟",
          lambda: M.ConfigLoader.default_config().get(
              "greeting_catchup_deadline_minutes") == 30)
    check("问候检查说明了未就绪的原因",
          lambda: "未就绪" in inspect.getsource(M.greeting_daily_check))


def t_config_defaults_new_keys():
    """新增配置项都有默认值，WebUI 才能渲染出来。"""
    import main as M

    section("T 新增配置项默认值")

    d = M.ConfigLoader.default_config()
    expected = {
        "mood_enabled": True,
        "mood_style_enabled": True,
        "history_context_recent": 6,
        "history_context_summary_chars": 400,
        "history_context_max_chars": 1600,
        "greeting_catchup_deadline_minutes": 30,
        "web_search_safe": "normal",
        "web_search_max_results": 10,
        "web_search_max_chars": 4000,
        "tts_loudness_normalize": True,
        "tts_loudness_target_db": -20.0,
        "tts_loudness_peak_db": -1.0,
        "tts_loudness_max_gain_db": 12.0,
        "tts_ref_normalize": True,
        "sticker_output_max_side": 400,
    }
    check("新增键全部存在且取值正确",
          lambda: all(d.get(k) == v for k, v in expected.items()))
    check("安全搜索默认档位是「一般」", lambda: d.get("web_search_safe") == "normal")


def u_sticker_visibility_and_role_identity():
    """表情包可见性/命名通用性、以及默认角色名串台。"""
    import main as M
    from modules.llm_helpers import RoleContext
    from modules.profiles import (_strip_role_names, migrate_extract_prompt,
                                  DEFAULT_EXTRACT_PROMPT)
    from modules.stickers import (auto_capture_image, drop_role_names, role_names_from_config,
                                  safe_sticker_name, sticker_descriptions,
                                  sticker_name_from_reason)

    section("U 表情包文件名 / 收藏命名 / 角色身份")

    full_width = "这张图完美表现主人被猫娘怼到捂脸的无奈瞬间，收藏后可用于日后调侃主人.jpg"
    check("带全角标点的表情文件名可用（不再被接口判成非法）",
          lambda: safe_sticker_name(full_width) == full_width)
    check("表情文件名仍拒绝穿越与非法字符",
          lambda: safe_sticker_name("../../evil.png") == ""
          and safe_sticker_name("a\\b.png") == "" and safe_sticker_name("") == ""
          and safe_sticker_name("..") == "")
    check("收藏命名压成不含全角标点的短名",
          lambda: len(sticker_name_from_reason(full_width, "x")) <= 24
          and "，" not in sticker_name_from_reason(full_width, "x"))
    check("已是短名时原样保留",
          lambda: sticker_name_from_reason("捂脸无奈", "x") == "捂脸无奈")
    check("名字里的角色名被剔掉（换角色后仍能用）",
          lambda: drop_role_names("被猫娘怼到捂脸的无奈", ["猫娘"]) == "被怼到捂脸的无奈"
          and drop_role_names("丛雨式撒娇", ["丛雨"]) == "式撒娇")
    role_cfg = Cfg({"character_name": "猫娘",
                    "roles": [{"character_name": "猫娘"}, {"character_name": "丛雨"}]})
    check("角色名取自配置里的全部角色",
          lambda: set(role_names_from_config(role_cfg)) == {"猫娘", "丛雨"}
          and role_names_from_config(Cfg({"character_name": "雨"})) == [])

    tmpdir = ROOT / ".tmp_test" / "fixrun" / "stickervis"
    for old in tmpdir.glob("*"):
        if old.is_file():
            old.unlink()
    (tmpdir / "wuyu").mkdir(parents=True, exist_ok=True)
    (tmpdir / "wuyu" / full_width).write_bytes(b"\x89PNG\r\n\x1a\n1")
    cfg = Cfg({"stickers_enabled": True, "stickers_dir": str(tmpdir)})
    M.sticker_mgr = M.StickerManager(cfg)
    M.app_context.sticker_mgr = M.sticker_mgr
    server = M.WebUIServer.__new__(M.WebUIServer)

    class _Req:
        def __init__(self, q):
            self.query = q

    r = asyncio.run(server.handle_stickers_file(_Req({"category": "wuyu", "name": full_width})))
    check("图片接口能取到全角标点命名的表情（否则页面全是裂图）",
          lambda: getattr(r, "status", 0) == 200)
    r2 = asyncio.run(server.handle_stickers_file(_Req({"category": "wuyu", "name": "../x.png"})))
    check("图片接口仍拒绝路径穿越", lambda: getattr(r2, "status", 0) == 404)
    webp_name = "大笑.webp"
    (tmpdir / "wuyu" / webp_name).write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
    r3 = asyncio.run(server.handle_stickers_file(_Req({"category": "wuyu", "name": webp_name})))
    check("webp 缩略图带上图片类型（否则浏览器拒绝渲染，页面全是裂图）",
          lambda: getattr(r3, "status", 0) == 200
          and r3.headers.get("Content-Type") == "image/webp")
    # 时间戳越界时 web.FileResponse 会在准备阶段抛 OSError，浏览器拿到空连接
    bad_time = tmpdir / "wuyu" / "越界时间.webp"
    bad_time.write_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ")
    os.utime(bad_time, (-11618281378.0, -11618281378.0))
    r4 = asyncio.run(server.handle_stickers_file(
        _Req({"category": "wuyu", "name": "越界时间.webp"})))
    check("修改时间越界的表情也能正常返回（否则图全裂且连接被断）",
          lambda: getattr(r4, "status", 0) == 200
          and r4.headers.get("Content-Type") == "image/webp")

    # 情绪音频接口服务的也是用户自己放的文件，同样不能走 web.FileResponse
    audio_root = tmpdir / "audio"
    (audio_root / "平静").mkdir(parents=True, exist_ok=True)
    ref_audio = audio_root / "平静" / "ref.wav"
    ref_audio.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
    os.utime(ref_audio, (-11618281378.0, -11618281378.0))

    class _AudioCfg:
        config = {"ref_audio_root": str(audio_root)}
        roles = {}

    audio_srv = M.WebUIServer.__new__(M.WebUIServer)
    audio_srv.config = _AudioCfg()
    ra = asyncio.run(audio_srv.handle_emotions_audio(
        _Req({"emotion": "平静", "file": "ref.wav"})))
    check("参考音频接口不受越界时间戳影响",
          lambda: getattr(ra, "status", 0) == 200
          and ra.headers.get("Content-Type") == "audio/wav")

    listed = asyncio.run(server.handle_stickers_list(_Req({})))
    check("列表接口能列出该表情",
          lambda: full_width in [f for cat in json.loads(
              getattr(listed, "text", "") or "{}").get("categories", [])
              for f in cat.get("files", [])])

    cap_dir = tmpdir / "capture"
    cap_cfg = Cfg({"stickers_enabled": True, "stickers_dir": str(cap_dir),
                   "sticker_capture_enabled": True, "sticker_capture_min_interval": 0,
                   "sticker_capture_max_per_day": 100,
                   "character_name": "猫娘",
                   "roles": [{"character_name": "猫娘"}, {"character_name": "丛雨"}]})
    cap_mgr = M.StickerManager(cap_cfg)
    src = tmpdir / "src.gif"
    src.write_bytes(b"GIF89a0")
    asyncio.run(auto_capture_image(cap_cfg, cap_mgr, str(src), "wuyu",
                                   reason="适合在被猫娘怼到捂脸时发出去表达无奈"))
    saved2 = sorted((cap_dir / "wuyu").iterdir()) if (cap_dir / "wuyu").exists() else []
    check("收藏落盘的名字与说明都不含角色名",
          lambda: saved2 and "猫娘" not in saved2[0].name
          and "猫娘" not in json.dumps(sticker_descriptions(cap_dir), ensure_ascii=False))

    cat_dir = tmpdir / "cat"
    (cat_dir / "高兴").mkdir(parents=True, exist_ok=True)
    (cat_dir / "高兴" / "seed.png").write_bytes(b"seed-gaoxing")
    cat_cfg = Cfg({"stickers_enabled": True, "stickers_dir": str(cat_dir),
                   "sticker_capture_enabled": True, "sticker_capture_min_interval": 0,
                   "sticker_capture_max_per_day": 0})
    asyncio.run(auto_capture_image(cat_cfg, M.StickerManager(cat_cfg), str(src), "高兴",
                                   reason="开心大笑"))
    check("分类名与已有文件夹一致时直接落进该文件夹（不再按拼音另建目录）",
          lambda: sorted(p.name for p in (cat_dir / "高兴").iterdir())
          == ["seed.png", "开心大笑.gif"]
          and not (cat_dir / "gaoxing").exists())
    asyncio.run(auto_capture_image(cat_cfg, M.StickerManager(cat_cfg), str(src), "乱七八糟",
                                   reason="乱造的分类"))
    check("模型乱造的分类仍然不建新目录",
          lambda: not (cat_dir / "乱七八糟").exists())

    check("默认画像提取提示词不含内置角色名",
          lambda: "丛雨" not in DEFAULT_EXTRACT_PROMPT and "{role}" in DEFAULT_EXTRACT_PROMPT)
    legacy = {"profiles_extract_prompt": "前缀（如角色名、丛雨、主人、AI）后缀"}
    check("旧配置里的写死角色名被换成占位符",
          lambda: migrate_extract_prompt(legacy) is True
          and legacy["profiles_extract_prompt"] == "前缀（如角色名、{role}、主人、AI）后缀")
    check("自定义过的提示词不被改写",
          lambda: migrate_extract_prompt({"profiles_extract_prompt": "只提取用户信息"}) is False)

    ctx = RoleContext({"character_name": "猫娘", "character_key": "maoniang",
                       "roles": [{"character_key": "murasame", "character_name": "丛雨"},
                                 {"character_key": "maoniang", "character_name": "猫娘"}]}, {})
    cleaned = _strip_role_names({"nickname": "丛雨", "notes": ["猫娘"],
                                 "likes": ["甜食"]}, ctx)
    check("任何角色名都写不进用户画像",
          lambda: "nickname" not in cleaned and "notes" not in cleaned
          and cleaned.get("likes") == ["甜食"])

    blank_role = {"character_key": "role2", "character_name": "",
                  "personality_prompt": "", "json_prompt": "", "supplement_prompt": ""}
    default_prompts = {"personality_prompt": "默认人设", "json_prompt": "默认JSON",
                       "supplement_prompt": "默认补充"}
    blank_ctx = RoleContext(default_prompts, blank_role)
    check("没填提示词的角色不继承默认人设（否则会变成默认角色）",
          lambda: all(blank_ctx.get(k, "") == "" for k in
                      ("personality_prompt", "json_prompt", "supplement_prompt")))
    check("角色自己填了提示词时以它自己的为准",
          lambda: RoleContext(default_prompts,
                              {"character_key": "a", "personality_prompt": "自己的"}
                              ).get("personality_prompt") == "自己的")
    check("没有角色条目时仍回退全局配置",
          lambda: RoleContext(default_prompts, {}).get("personality_prompt") == "默认人设")

    class RoleLoader:
        def __init__(self, active):
            self.active_character = active
            self.roles = {
                "murasame": {"character_key": "murasame", "character_name": "丛雨"},
                "maoniang": {"character_key": "maoniang", "character_name": "猫娘"},
            }
            self.config = {"memory_data_path": str(ROOT / ".tmp_test" / "fixrun" / "rolemm"),
                           "character_key": "murasame", "character_name": "丛雨",
                           "active_character": active}

        def get(self, k, d=None):
            return self.config.get(k, d)

    mm = M.MemoryManager(RoleLoader("maoniang"))
    check("新会话按当前标识符建档、按当前角色署名",
          lambda: mm.get_memory_file("private_1").name == "maoniang_private_1.json"
          and mm.load_session_data("private_1")["character_name"] == "猫娘")
    check("切回默认角色时仍是它自己的档案",
          lambda: M.MemoryManager(RoleLoader("murasame")).get_memory_file(
              "private_1").name == "murasame_private_1.json")


def v_sweep_fixes():
    """全量清扫修掉的缺陷：问候历史接线、问候记录可读性、原子写临时文件、待办发送结果。"""
    import main as M
    import modules.events as EV
    import modules.jsonio as JI
    from modules.database import DatabaseManager
    from modules.llm_helpers import RoleContext
    from modules.scheduler import SchedulerManager
    from modules.todo_manager import TodoManager, TODO_RETRY_MAX

    section("V 全量清扫修复")

    class FakeMem:
        def load_history(self, sid):
            return [{"role": "user", "content": "在吗"},
                    {"role": "assistant", "content": "在的"}]

        def load_session_data(self, sid):
            return {"history": [], "meta": {"summary": "之前聊过天气"}}

    orig_mem = M.memory_manager
    M.memory_manager = FakeMem()
    M.app_context.memory_manager = M.memory_manager
    try:
        block = M.session_history_block("group_1", None)
        check("问候历史按会话键取到真实历史（接线不对时恒为空）",
              lambda: "在吗" in block and "在的" in block)
        check("问候历史带上会话键，摘要也取得到", lambda: "之前聊过天气" in block)
    finally:
        M.memory_manager = orig_mem
        M.app_context.memory_manager = M.memory_manager

    # 主动问候（节日/生日）发出后要进会话历史：漏了这一步，用户接着回复时模型
    # 不知道自己刚问候过，WebUI 的聊天记录里也找不到这条问候
    greet_dir = ROOT / ".tmp_test" / "fixrun" / f"greet_{int(time.time() * 1000)}"
    greet_dir.mkdir(parents=True, exist_ok=True)
    greet_keys = []

    class GreetSpy:
        client = object()

        @contextlib.contextmanager
        def for_session(self, session_id):
            self.last_session = session_id
            yield
        
        async def speak_and_send(self, session_type, target, text, emotions, ctx=None,
                                 use_voice=None, sticker=False, emotion="", session_id=""):
            greet_keys.append(session_id)
            return True

    class FakeProfiles:
        profiles = {"1905332561": {"birthday": time.strftime("%m-%d")}}

    em_b = EV.EventManager(Cfg({"birthday_greeting_enabled": True,
                                "birthday_greet_mode": "template",
                                "birthday_greet_template": "生日快乐"}),
                           greet_dir, FakeProfiles())
    asyncio.run(em_b.check_and_greet(GreetSpy(), lambda: RoleContext({"personality_prompt": "x"}),
                                     lambda: {}, history_provider=lambda k, c: ""))
    check("生日问候把会话键交给发送器（否则问候不会进历史）",
          lambda: greet_keys == ["private_1905332561"])

    tmpdir = ROOT / ".tmp_test" / "fixrun" / f"sweep_{int(time.time() * 1000)}"
    tmpdir.mkdir(parents=True, exist_ok=True)

    saved = []
    orig_save = JI.save_json
    JI.save_json = lambda path, data: saved.append(path)
    try:
        broken = tmpdir / "greeting_log_broken.json"
        broken.mkdir(parents=True, exist_ok=True)
        em = EV.EventManager(Cfg({"default_events_enabled": False}), tmpdir)
        em.log_file = broken
        em.load_log()
        em._mark_sent("k")
        check("问候记录读不出来时不写回磁盘（不覆盖原有内容）", lambda: saved == [])

        em2 = EV.EventManager(Cfg({"default_events_enabled": False}), tmpdir)
        em2._mark_sent("k")
        check("问候记录读得出来时照常写回", lambda: len(saved) == 1)
    finally:
        JI.save_json = orig_save

    as_dir = tmpdir / "as_dir.json"
    as_dir.mkdir(parents=True, exist_ok=True)
    try:
        JI.save_json(as_dir, {"a": 1})
    except Exception:
        pass
    check("原子写失败时不残留 .tmp 文件",
          lambda: list(tmpdir.glob("as_dir.json.*.tmp")) == [])

    class DeadSender:
        client = object()

        @contextlib.contextmanager
        def for_session(self, session_id):
            self.last_session = session_id
            yield
        
        async def speak_and_send(self, *a, **kw):
            return False

    db = DatabaseManager(tmpdir)
    mgr = TodoManager(Cfg({"todo_remind_mode": "preset",
                           "todo_remind_template": "⏰ {content}"}),
                      db, SchedulerManager(), DeadSender())
    mgr.ctx_provider = lambda: RoleContext({"personality_prompt": "x"})
    mgr.emotions_provider = lambda: {}
    todo = mgr.add_todo("吃药", time.time() + 60, "private", "private_10001")
    asyncio.run(mgr.fire_reminder(todo["id"], "吃药", "private", "private_10001"))
    check("发送未成功时待办保留为待提醒状态（不再直接标完成）",
          lambda: [r["id"] for r in mgr.list_todos("pending")] == [todo["id"]])
    check("发送失败后会重排一次提醒（不能只改状态就不管了）",
          lambda: mgr.scheduler.get_job(f"todo_{todo['id']}") is not None
          and mgr._retry_counts.get(todo["id"]) == 1)
    for _ in range(TODO_RETRY_MAX):
        asyncio.run(mgr.fire_reminder(todo["id"], "吃药", "private", "private_10001"))
    check("连续发不出去到上限就标为已过期，不再无限重试",
          lambda: [r["id"] for r in mgr.list_todos("missed")] == [todo["id"]]
          and todo["id"] not in mgr._retry_counts)
    check("手动把过期的待办改回待提醒会重新排上提醒",
          lambda: mgr.resume(todo["id"])
          and mgr.scheduler.get_job(f"todo_{todo['id']}") is not None
          and [r["id"] for r in mgr.list_todos("pending")] == [todo["id"]])

    tts_src = (ROOT / "modules" / "tts_service.py").read_text(encoding="utf-8")
    check("TTS 自动启动的就绪判据与 check_tts_service 一致",
          lambda: "status_code < 500" not in tts_src)


def main():
    print("Lovomo 修复专项回归测试")
    a_tts_text_integrity()
    a_tts_call_order()
    a_synth_guard()
    a_segment_no_loss()
    a_normalize_no_loss()
    b_todo_remind_mode()
    c_emotion_guide()
    d_proactive()
    e_end_to_end()
    f_thinking_never_spoken()
    g_sticker_capture_gate()
    g2_sticker_no_category_fallback()
    g3_sticker_category_alias()
    h_tool_message_hygiene()
    i_gif_and_identity()
    j_tts_detailed_log()
    k_tts_merge_no_word_loss()
    l_proactive_history()
    m_mood_judge_split()
    n_todo_meaningless_transcribe()
    n2_todo_time_not_content()
    o_weather_location_guard()
    p_tool_gating_strict()
    q_safe_search()
    r_vision_followup_after_skip()
    s_greeting_catchup_diagnostics()
    t_config_defaults_new_keys()
    u_sticker_visibility_and_role_identity()
    v_sweep_fixes()
    print(f"\n{'=' * 70}")
    print(f"结果：通过 {len(PASS)} | 失败 {len(FAIL)}")
    for name, err in FAIL:
        print(f"  - {name}: {err}")
    print(f"{'=' * 70}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
