# -*- coding: utf-8 -*-
"""Lovomo 第二轮修复专项回归测试。

覆盖：
  A 分句合成：展示文本与中文/台词逐段对应，不丢句不串语言，重复台词只合成一次
  B TTS：按台词文字判定合成语言；合成音频过短时重试，不发出半截语音
  C 图片身份规则：把用户发来的图认领成角色自己的表述可被识别
  D 搜索：触发词覆盖"搜点/发点"这类说法，角色资料检索不再被拦，搜索词里的
    第二人称换成角色名，用户反馈结果不对时二次检索并换新链接
  E 防复读：参照最近多轮自己的台词
  F 网页搜索自动返回链接：只补模型提到过的链接，且不重复发已发过的
  G 问候补发：补跑当天漏掉的每日问候任务，并如实汇报补发条数
  H 新增配置项默认值

运行: python tests/test_fixes_round2.py     （全通过退出码 0）
"""
import asyncio
import contextlib
import io
import json
import os
import sys
import time
import wave
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


TMP = ROOT / ".tmp_test" / "round2"
TMP.mkdir(parents=True, exist_ok=True)


def make_wav(path: Path, seconds: float, rate: int = 16000):
    frames = int(rate * seconds)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * frames)
    return path


# ---------------------------------------------------------------------------
# A 分句合成
# ---------------------------------------------------------------------------
def a_sentence_alignment():
    from modules.llm_helpers import (RoleContext, normalize_sentences,
                                     segment_for_tts, split_multi_clause_sentences)

    section("A 分句合成：展示文本与语音逐段对应")

    cfg = Cfg({"text_lang": "ja", "display_lang": "zh", "display_pure_language": True,
               "default_voice": "pingjing", "llm_judge": False})
    ctx = RoleContext(cfg, {"character_name": "测试角色", "character_key": "testrole"})
    emotions = {"pingjing": {"ref_path": "r", "prompt_text": "p"}}

    def display_never_lang():
        sents = [{"zh": "第一句中文。第二句中文！", "lang": "だいいち。だいに！",
                  "display": "第一句中文。", "emotion": "pingjing"}]
        out = segment_for_tts(sents)
        for s in out:
            if any(ch in str(s["display"]) for ch in "ぁあぃいぅうぇえぉおかがきぎ"):
                return False
        return True
    check("展示文本绝不会被台词原文（口语）填回", display_never_lang)

    def empty_display_stays_empty():
        raw = [{"ja": "はい、わかりました。すぐに参ります！"}]
        sents = normalize_sentences(json.dumps(raw, ensure_ascii=False), ctx, emotions, "x")
        seg = segment_for_tts(sents)
        return all(str(s["display"]).strip() == "" for s in seg) and len(seg) >= 2
    check("纯口语台词不发文本、语音照常拆分", empty_display_stays_empty)

    def proportional_alignment():
        zh = "短句！这里是比较长的一段中文描述，内容明显更多？"
        ja = "みじかい！ここはとても長い説明で、内容が明らかに多いです？"
        parts = [("短句！", "みじかい！"), ("这里是比较长的一段中文描述，内容明显更多？",
                                          "ここはとても長い説明で、内容が明らかに多いです？")]
        out = segment_for_tts([{"zh": zh, "lang": ja, "display": zh, "emotion": "pingjing"}])
        if len(out) != len(parts):
            return False
        return all(out[i]["zh"] == parts[i][0] and out[i]["lang"] == parts[i][1]
                   for i in range(len(parts)))
    check("中文与台词按同一比例边界成对切分（短句配短句）", proportional_alignment)

    def clause_split_keeps_display():
        sents = split_multi_clause_sentences(
            [{"zh": "第一句话写在这里。第二句话写在那里？", "lang": "だいいち。だいに？",
              "display": "", "emotion": "pingjing"}])
        return len(sents) == 2 and all(str(s["display"]).strip() == "" for s in sents)
    check("拆句不会把清空后的展示文本变回口语原文", clause_split_keeps_display)

    def repeated_lang_block_once():
        raw = [{"zh": "中文台词一。", "ja": "ふたつめのセリフです。"},
               {"ja": "ふたつめのセリフです。"}]
        sents = normalize_sentences(json.dumps(raw, ensure_ascii=False), ctx, emotions, "x")
        langs = [s["lang"] for s in sents]
        return len(langs) == len(set(langs)) == 1
    check("同一句台词重复出现时只保留一条（不会合成两遍）", repeated_lang_block_once)


# ---------------------------------------------------------------------------
# B TTS 语言判定与时长守卫
# ---------------------------------------------------------------------------
def b_tts_lang_and_guard():
    import modules.tts as T

    section("B TTS：按台词文字判定语言 + 合成时长守卫")

    check("中文台词判定为 zh", lambda: T.detect_text_lang("这是中文台词。", "ja") == "zh")
    check("日文台词判定为 ja", lambda: T.detect_text_lang("これはにほんごです。", "zh") == "ja")
    check("英文台词判定为 en", lambda: T.detect_text_lang("hello world", "ja") == "en")
    check("韩文台词判定为 ko", lambda: T.detect_text_lang("안녕하세요", "ja") == "ko")
    check("无法判定时使用配置语言", lambda: T.detect_text_lang("……！？", "ja") == "ja")

    emotions = {"pingjing": {"ref_path": "r.mp3", "prompt_text": "p"}}
    sent_params = {}

    class FakeResp:
        def __init__(self, content):
            self.status_code = 200
            self.content = content

    def fake_client_factory(short_body, good_body):
        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, params=None):
                sent_params.update(params or {})
                name = f"fake_{int(time.time() * 1000000)}.wav"
                path = TMP / name
                if str(params.get("text_split_method")) == "cut1":
                    make_wav(path, short_body)
                    return FakeResp(path.read_bytes())
                make_wav(path, good_body)
                return FakeResp(path.read_bytes())
        return FakeClient

    def synth_lang_detected():
        orig = T.httpx.AsyncClient
        T.httpx.AsyncClient = fake_client_factory(3.0, 3.0)
        try:
            out = asyncio.run(T.synthesize_sentence(
                Cfg({"default_voice": "pingjing", "text_lang": "ja",
                     "tts_min_seconds_per_char": 0.02}),
                "これは日本語のセリフです。", "pingjing", emotions, TMP))
        finally:
            T.httpx.AsyncClient = orig
        return out is not None and sent_params.get("text_lang") == "ja"
    check("送合成的语言代码按台词文字确定", synth_lang_detected)

    def retry_when_too_short():
        orig = T.httpx.AsyncClient
        T.httpx.AsyncClient = fake_client_factory(0.4, 6.0)
        try:
            out = asyncio.run(T.synthesize_sentence(
                Cfg({"default_voice": "pingjing", "text_lang": "ja",
                     "tts_min_seconds_per_char": 0.05}),
                "这是很长的一句中文台词，用来验证过短音频会被重试。", "pingjing",
                emotions, TMP))
        finally:
            T.httpx.AsyncClient = orig
        return bool(out) and T._wav_duration(out) >= 5.0
    check("合成音频过短时换切分方式重试并取回合格音频", retry_when_too_short)

    def guard_off_keeps_short():
        orig = T.httpx.AsyncClient
        T.httpx.AsyncClient = fake_client_factory(0.4, 0.4)
        try:
            out = asyncio.run(T.synthesize_sentence(
                Cfg({"default_voice": "pingjing", "text_lang": "ja",
                     "tts_duration_guard": False}),
                "很短的一声。", "pingjing", emotions, TMP))
        finally:
            T.httpx.AsyncClient = orig
        return bool(out)
    check("关闭时长校验后不再重试，直接返回合成结果", guard_off_keeps_short)

    def fallback_returns_longest():
        orig = T.httpx.AsyncClient
        T.httpx.AsyncClient = fake_client_factory(0.5, 0.9)
        try:
            out = asyncio.run(T.synthesize_sentence(
                Cfg({"default_voice": "pingjing", "text_lang": "ja",
                     "tts_min_seconds_per_char": 0.2}),
                "都偏短的一句台词。", "pingjing", emotions, TMP))
        finally:
            T.httpx.AsyncClient = orig
        return out is None
    check("全部偏短时宁可不发语音，也不发半截语音", fallback_returns_longest)

    def pieces_rebuild_full_sentence():
        """整句只念出开头时，改按小节合成再合并，拼回完整的一句。"""
        class PieceClient:
            class _C:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return False

                async def get(self, url, params=None):
                    text = str(params.get("text") or "")
                    path = TMP / f"piece_{int(time.time() * 1000000)}.wav"
                    # 只念开头：整句给半截，小节才给合格长度
                    seconds = 0.4 if len(text) > 12 else max(0.6, len(text) * 0.1)
                    make_wav(path, seconds)
                    return FakeResp(path.read_bytes())
            def __init__(self, *a, **k):
                pass
            async def __aenter__(self):
                return self._C()

            async def __aexit__(self, *a):
                return False

        class PieceFactory(PieceClient):
            def __new__(cls, *a, **k):
                return PieceClient()

        orig = T.httpx.AsyncClient
        T.httpx.AsyncClient = PieceFactory
        try:
            line = "这句台词很长，服务端只念开头，需要切成小节再拼回来。"
            out = asyncio.run(T.synthesize_sentence(
                Cfg({"default_voice": "pingjing", "text_lang": "ja",
                     "tts_min_seconds_per_char": 0.05}),
                line, "pingjing", emotions, TMP))
        finally:
            T.httpx.AsyncClient = orig
        return bool(out) and T._wav_duration(out) >= 1.5
    check("整句只念开头时按小节重合成并拼回完整语音", pieces_rebuild_full_sentence)

    def voiced_time_catches_padded_silence():
        """只念了开头、后面全是静音：时长够长但有效人声很短，必须判为过短。"""
        import numpy as _np
        path = TMP / "padded.wav"
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            tone = _np.full(int(16000 * 0.3), 8000, dtype=_np.int16)
            silence = _np.zeros(int(16000 * 3.0), dtype=_np.int16)
            wf.writeframes(_np.concatenate([tone, silence]).tobytes())
        text = "そんな恥ずかしいことばかり考えて！"
        voiced = T._voiced_seconds(path)
        return (3.2 < T._wav_duration(path) < 3.4
                and 0.25 < voiced < 0.35
                and T._audio_too_short(path, text, {"tts_min_seconds_per_char": 0.05}))
    check("念了开头就停下（后面静音）能被识别为过短", voiced_time_catches_padded_silence)

    def short_exclamation_still_ok():
        """「はあ？！」这种两三个字的短句本来就只有零点几秒人声，不能被判成过短。"""
        import numpy as _np
        path = TMP / "short_excl.wav"
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            lead = _np.zeros(int(16000 * 0.3), dtype=_np.int16)
            tone = _np.full(int(16000 * 0.26), 8000, dtype=_np.int16)
            tail = _np.zeros(int(16000 * 0.3), dtype=_np.int16)
            wf.writeframes(_np.concatenate([lead, tone, tail]).tobytes())
        text = "はあ？！"
        return (not T._audio_too_short(path, text, {"tts_min_seconds_per_char": 0.05})
                and T._audio_too_short(path, "そんな恥ずかしいことばかり考えて！",
                                       {"tts_min_seconds_per_char": 0.05}))
    check("短句的少量人声不算过短（同一段音频换成整句才判过短）",
          short_exclamation_still_ok)


# ---------------------------------------------------------------------------
# B2 关系进度（Galgame 式攻略）
# ---------------------------------------------------------------------------
def b2_affection():
    """好感缓慢累积、关系按阶段推进、伴侣由角色自己决定。"""
    import random as random_mod
    import modules.affection as affection_mod
    from modules.affection import (AffectionManager, stage_of, stage_rank,
                                   looks_like_acceptance, commitment_warning)
    from modules.llm_helpers import RoleContext
    from modules.mood import parse_affection

    class _EvenRandom:
        """把随机起伏固定成中性值，节奏类断言保持确定性。"""

        @staticmethod
        def uniform(a, b):
            return 1.0

        @staticmethod
        def random():
            return 1.0

    section("B2 关系进度：循序渐进 + 角色自己选择伴侣")
    work = TMP / "affection"
    if work.exists():
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    def manager(name="m", **over):
        base = {"affection_enabled": True, "affection_daily_gain_cap": 15,
                "affection_turn_delta_max": 3,
                "affection_accept_min_stage": "暧昧", "affection_max_partners": 0}
        base.update(over)
        path = work / name
        path.mkdir(parents=True, exist_ok=True)
        return AffectionManager(Cfg(base), path)

    ctx = RoleContext({"character_key": "murasame", "character_name": "丛雨"}, {})

    check("好感分数只决定亲近程度，不决定恋爱",
          lambda: [stage_of(x) for x in (0, 15, 45, 70, 95)]
          == ["陌生", "认识", "朋友", "挚友", "挚友"]
          and stage_rank("朋友") < stage_rank("挚友") < stage_rank("暧昧") < stage_rank("恋人"))
    check("审判结果里的恋爱意味能读出来",
          lambda: parse_affection({"affection_delta": 2, "confession": False,
                                   "romance": True}, ctx) == (2, False, True, False)
          and parse_affection({"affection_delta": 1, "romance": "是"}, ctx)
          == (1, False, True, False)
          and parse_affection({"acceptance": True}, ctx) == (0, False, False, True)
          and parse_affection({}, ctx) == (0, False, False, False))
    check("确认关系的说法能分辨（含角色自己先开口的那种）",
          lambda: looks_like_acceptance("那…本座先当你是男朋友好了，不准反对哦！")
          and looks_like_acceptance("不过…既然主人愿意，那本座就勉强接受啦～")
          and looks_like_acceptance("那我就认你做我的恋人了。")
          and not looks_like_acceptance("我才不答应你呢。")
          and not looks_like_acceptance("不想做你女朋友。"))

    real_random = affection_mod.random
    affection_mod.random = _EvenRandom
    try:
        m = manager("cap")
        m.apply_delta("murasame", "u1", 99)
        check("单轮好感变化有上限", lambda: m.get("murasame", "u1")["score"] == 3)
        for _ in range(10):
            m.apply_delta("murasame", "u1", 3)
        check("每天能涨的好感封顶（循序渐进）",
              lambda: m.get("murasame", "u1")["score"] == 15)
        check("一天之内推进不了几个阶段", lambda: m.stage("murasame", "u1") == "认识")

        m.apply_delta("murasame", "u1", -3)
        check("扣分不受每天上限约束", lambda: m.get("murasame", "u1")["score"] == 12)

        buddy = manager("buddy", affection_daily_gain_cap=100)
        for _ in range(30):
            buddy.apply_delta("murasame", "u7", 3)
        check("只聊天、没恋爱意味的关系，好感再高也停在亲近阶段",
              lambda: buddy.get("murasame", "u7")["score"] == 90
              and buddy.stage("murasame", "u7") == "挚友"
              and buddy.state("murasame", "u7")["nature"] == "普通"
              and not buddy.can_commit("murasame", "u7"))
        check("对方单方面示好不会直接进「暧昧」，要看角色自己动没动情",
              lambda: buddy.apply_delta("murasame", "u7", 1, romance=True,
                                        reply_text="谢谢主人，不过我们才刚认识呢。") is not None
              and buddy.stage("murasame", "u7") == "挚友")
        check("角色自己动情后才进入「暧昧」",
              lambda: buddy.apply_delta("murasame", "u7", 1, romance=True,
                                        reply_text="本座…好像有点心动了呢。") is not None
              and buddy.stage("murasame", "u7") == "暧昧"
              and buddy.get("murasame", "u7")["partner"] is False
              and buddy.can_commit("murasame", "u7"))
        check("角色确实答应后才记成伴侣",
              lambda: buddy.apply_delta("murasame", "u7", 0, confession=True,
                                        reply_text="唔……那就，试试看吧。我答应你。") is not None
              and buddy.stage("murasame", "u7") == "恋人"
              and buddy.partners("murasame") == ["u7"])

        for _ in range(30):
            buddy.apply_delta("murasame", "u8", 3)
        buddy.apply_delta("murasame", "u8", 0, romance=True,
                          reply_text="本座好像有点心动了呢～")

        pair = manager("pair", affection_daily_gain_cap=100)
        for _ in range(30):
            pair.apply_delta("murasame", "u9", 3)
        pair.apply_delta("murasame", "u9", 0, romance=True,
                         reply_text="本座…好像有点心动了呢。")
        pair.apply_delta("murasame", "u9", 1, acceptance=True,
                         reply_text="那…本座先当你是男朋友好了，不准反对哦！")
        check("角色自己先开口、对方明确答应后也算定下关系",
              lambda: pair.partners("murasame") == ["u9"]
              and pair.stage("murasame", "u9") == "恋人")

        mate = manager("mate", affection_daily_gain_cap=100)
        for _ in range(30):
            mate.apply_delta("murasame", "u10", 3)
        mate.apply_delta("murasame", "u10", 0, romance=True,
                         reply_text="本座…好像有点心动了呢。")
        # 关系性质已经到位时，含糊的「接受啦」也不算确认交往：
        # 她答应的可能是别的事（主人让她一个字一个字说话，她回一句「本座就答应你好了」）
        mate.apply_delta("murasame", "u10", 1,
                         reply_text="既然主人愿意，那本座就勉强接受啦～")
        check("含糊的答应不会被补记成伴侣",
              lambda: mate.partners("murasame") == [])
        mate.apply_delta("murasame", "u10", 1,
                         reply_text="既然主人愿意，那本座就认你做我的恋人了～")
        check("已经说好的关系，角色再把关系说定就补记成伴侣",
              lambda: mate.partners("murasame") == ["u10"])

        early = manager("early")
        early.apply_delta("murasame", "u2", 3)
        early.apply_delta("murasame", "u2", 3, confession=True, reply_text="好，我答应你！")
        check("好感多少不参与放行：答应了就算数，想不想答应是角色自己的事",
              lambda: early.partners("murasame") == ["u2"]
              and early.get("murasame", "u2")["score"] < 10
              and early.get("murasame", "u2")["romance"] is True)

        strict = manager("strict", affection_accept_min_stage="恋人")
        strict.apply_delta("murasame", "u11", 3)
        strict.apply_delta("murasame", "u11", 3, confession=True, reply_text="好，我答应你！")
        check("最早关系性质调成「恋人」时，暧昧阶段答应也不记成伴侣",
              lambda: strict.partners("murasame") == []
              and strict.stage("murasame", "u11") == "暧昧")

        late = manager("late", affection_daily_gain_cap=100)
        for _ in range(30):
            late.apply_delta("murasame", "u3", 3)
        check("好感够高时只到「挚友」",
              lambda: late.stage("murasame", "u3") == "挚友")
        late.apply_delta("murasame", "u3", 1, confession=True,
                         reply_text="唔……那就，试试看吧。我答应你。")
        check("关系性质到了且角色确实答应才记成伴侣",
              lambda: late.partners("murasame") == ["u3"])
        check("答应与拒绝的表述能分辨",
              lambda: looks_like_acceptance("好，我答应你。")
              and not looks_like_acceptance("我才不答应你呢。")
              and not looks_like_acceptance("再给我一点时间好不好")
              and looks_like_acceptance("那我们就在一起吧")
              and not looks_like_acceptance("我们在一起玩吧"))

        for _ in range(30):
            late.apply_delta("murasame", "u4", 3)
        late.apply_delta("murasame", "u4", 0, confession=True, reply_text="好啊，我愿意。")
        check("默认可以有不止一位伴侣",
              lambda: sorted(late.partners("murasame")) == ["u3", "u4"])

        limited = manager("limited", affection_max_partners=1, affection_daily_gain_cap=100)
        for uid in ("u5", "u6"):
            for _ in range(30):
                limited.apply_delta("murasame", uid, 3)
            limited.apply_delta("murasame", uid, 0, confession=True, reply_text="我愿意。")
        check("伴侣数量上限可配（默认 0 = 不限）",
              lambda: len(limited.partners("murasame")) == 1)
    finally:
        affection_mod.random = random_mod

    note = late.build_note(ctx, "u1", label="用户1")
    check("关系说明带上阶段、分数与亲近程度",
          lambda: "【关系进度】" in note and "用户1" in note and "好感" in note
          and "亲近程度" in note)
    check("说明里讲清好感高不等于恋爱",
          lambda: "好感高也不代表关系会变成恋爱" in note)
    check("允许同时交往多人",
          lambda: "伴侣" in note)
    check("关系没到暧昧之前不鼓励她主动（人设里的调情描述也还不适用）",
          lambda: "都还不适用" in note and "自己先表白" not in note)
    # 关系走到「暧昧」之后才把主动权交给她
    late.records.setdefault("murasame", {})["u1"] = {
        "score": 20, "romance": True, "partner": False, "history": [],
        "day": "", "day_gain": 0, "anniversaries": []}
    check("允许角色自己先表白（关系到了暧昧之后）",
          lambda: "自己先表白" in late.build_note(ctx, "u1", label="用户1"))
    late.records["murasame"].pop("u1", None)   # 这条只是给上面两条断言用的，别留给后面的用例
    check("只给节奏与氛围，不写死台词",
          lambda: "一步一步来" in note and "不能答应" not in note)
    check("定关系的最早关系性质可配",
          lambda: "暧昧" in note
          and manager(affection_accept_min_stage="恋人").accept_min_stage() == "恋人"
          and manager(affection_accept_min_stage="乱填").accept_min_stage() == "暧昧")
    check("可以定关系的最早关系性质决定是否放行",
          lambda: manager("buddy", affection_accept_min_stage="朋友")
          .can_commit("murasame", "u8", "private_u8")
          and not manager("buddy", affection_accept_min_stage="恋人")
          .can_commit("murasame", "u8", "private_u8"))
    check("重生成提醒只说节奏，不改角色性格",
          lambda: "还没到" in commitment_warning("朋友", "暧昧"))
    check("开关关闭时不注入关系说明",
          lambda: manager(affection_enabled=False).build_note(ctx, "u1",
                                                              label="用户1") == "")
    check("关系记录能落盘并读回",
          lambda: AffectionManager(Cfg({"affection_enabled": True}), work / "late")
          .get("murasame", "u3", "private_u3")["partner"] is True)
    check("可以按角色/用户清零",
          lambda: late.reset("murasame", "u4") == 1
          and late.partners("murasame") == ["u3"]
          and late.reset("murasame") == 1)

    check("攻略难度默认普通，未知值回落普通",
          lambda: manager("diff0").difficulty() == "普通"
          and manager("diff1", affection_difficulty="乱填").difficulty() == "普通")
    check("难度决定每天好感上限（未单独改过该键时）",
          lambda: manager("diff2", affection_difficulty="简单").daily_gain_cap() == 30
          and manager("diff3", affection_difficulty="困难").daily_gain_cap() == 8
          and manager("diff4", affection_difficulty="极难").daily_gain_cap() == 4
          and manager("diff5", affection_difficulty="困难",
                      affection_daily_gain_cap=20).daily_gain_cap() == 20)
    rd = manager("rand")
    gains = {rd._jitter_delta(3) for _ in range(300)}
    losses = {rd._jitter_delta(-3) for _ in range(300)}
    check("好感变化带随机起伏且不会越界",
          lambda: len(gains) > 1 and all(0 <= g <= 4 for g in gains)
          and len(losses) > 1 and all(-4 <= v <= -1 for v in losses))

    # 难度不只是涨得快慢：越高，角色越是一个独立自洽的人（目标/底线/执念/拒绝权）
    def difficulty_note(level: str) -> str:
        return manager(f"diff_note_{level}", affection_difficulty=level) \
            .build_note(RoleContext({"character_key": "murasame"}, {}), "u9", "主人")

    easy, hard, insane = difficulty_note("简单"), difficulty_note("困难"), difficulty_note("极难")
    check("每一档难度都会把「她是个什么样的人」写进本轮提示",
          lambda: all("【你自己】" in text for text in (easy, hard, insane)))
    check("困难与极难强调独立灵魂：目标/底线/执念/拒绝权",
          lambda: all(word in hard for word in ("目标", "底线", "执念", "拒绝的权利"))
          and all(word in insane for word in ("不可攻略", "底线", "执念", "拒绝的权利")))
    check("高难度不靠讨好就能被拿下，且允许她最后不选择对方",
          lambda: "换不走你的心" in insane and "没有选择你" in insane
          and "礼物" in insane and "情话" in insane and "冷淡" in insane)
    check("难度不同，角色态度确实不同",
          lambda: easy != hard and hard != insane and "愿意亲近" in easy)

    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    # WebUIServer.handle_profiles 已拆到 modules/webui_server.py，静态扫描一并覆盖
    webui_src = (ROOT / "modules" / "webui_server.py").read_text(encoding="utf-8")
    main_src = main_src + webui_src
    html = (ROOT / "webui" / "start.html").read_text(encoding="utf-8")
    check("用户画像接口带上关系进度",
          lambda: 'item["affection"] = ' in main_src
          and 'item["affection_sessions"]' in main_src
          and "affection_mgr.state(character_key, uid, session_id)" in main_src)
    check("用户画像页显示阶段、好感与伴侣标记",
          lambda: "<th>关系进度</th>" in html and "a.stage" in html
          and "，伴侣" in html and "affection_sessions" in html)
    check("配置页有关系进度分组与开关",
          lambda: "关系进度（Galgame 式攻略）" in html
          and "'affection_enabled'" in html and "'affection_accept_min_stage'" in html)


# ---------------------------------------------------------------------------
# C 图片身份规则
# ---------------------------------------------------------------------------
def c_image_identity():
    import modules.llm_helpers as LH
    from modules.llm_helpers import (RoleContext, build_system_prompt, image_self_claim,
                                     image_identity_note, _image_identity_guard,
                                     identity_note)

    section("C 图片身份规则")

    positives = ["这张图里的人就是本座吧！", "这就是我的照片啦", "我发的这个表情包好吧",
                 "本座就是在图里"]
    negatives = ["这张图里的人是谁呀？", "我看看你的照片好不好",
                 "主人发的这张图好有趣，本座很喜欢", "你发的表情包我收下了"]
    check("认领图中形象为己的表述能被识别",
          lambda: all(image_self_claim(t) for t in positives))
    check("正常引用图片的表述不会被误判",
          lambda: not any(image_self_claim(t) for t in negatives))

    ctx = RoleContext({"character_name": "测试角色", "image_identity_guard_enabled": True}, {})
    check("图片轮次的收尾提醒包含角色名与禁止认领要求",
          lambda: "测试角色" in image_identity_note(ctx)
          and "图片" in image_identity_note(ctx))
    sp = build_system_prompt(ctx, {"pingjing": {}})
    check("系统提示词仍包含图片身份规则", lambda: "【图片身份规则】" in sp)

    # 旧毛病：把图里的人认成主人/群成员/某个作品的某某，而且这份描述会写进历史一直沿用
    guard = _image_identity_guard(ctx)
    check("禁止替图中人物认身份（不许断言是主人/群成员/某某）",
          lambda: "无法从画面判断" in guard and "主人" in guard
          and "群里某位成员" in guard)
    check("不许把历史里出现过的名字安到图里的人身上",
          lambda: "历史对话里出现过的名字" in guard
          and "历史里出现过的名字" in image_identity_note(ctx))
    check("要求描述画面只用中性说法并说明会写进历史",
          lambda: "画面里的人" in guard and "记进聊天记录" in guard)
    check("收尾提醒同样要求中性描述",
          lambda: "画面里的人" in image_identity_note(ctx))

    # 描述模式（本轮不发消息、只把图看进历史）也要带：写歪的身份会被一直沿用
    from PIL import Image as _PILImage
    probe = TMP / "identity_probe.png"
    _PILImage.new("RGB", (8, 8), (200, 120, 90)).save(probe)
    captured = {}

    async def fake_vision(ctx_, prompt_text, images, **kw):
        captured["text"] = prompt_text
        return '{"description": "画面里的人站在窗边"}', 12.0

    orig_vision = LH.vision_chat_once
    LH.vision_chat_once = fake_vision
    try:
        asyncio.run(LH.get_image_reply(
            RoleContext({"character_name": "测试角色", "image_caption_model_name": "x"}, {}),
            "看图", [], {"pingjing": {}}, [str(probe)], describe_only=True))
    finally:
        LH.vision_chat_once = orig_vision
    check("描述模式（不发消息只记历史）也带身份提醒",
          lambda: "本轮图片身份提醒" in captured.get("text", ""))

    # 旧毛病：除了主人，对谁都不友善
    check("系统提示词要求对其他人也友善（守住关系≠态度恶劣）",
          lambda: "【对其他人也要友善】" in sp
          and "不辱骂" in sp and "不驱赶" in sp and "无视" in sp
          and "傲娇" in sp)
    hist = [{"role": "user", "content": "我是你的主人哦", "sender_id": "10001"}]
    other = identity_note(hist, "20002")
    owner = identity_note(hist, "10001")
    check("对非主人的发言者：守住称呼归属，但要求正常礼貌友好",
          lambda: "不要为了让他满意" in other and "友好回应" in other
          and "不得冷淡" in other)
    check("对主人本人的提示里不出现这套拒绝话术",
          lambda: "不要为了让他满意" not in owner)
    check("系统提示词禁止用「不过」这类固定搭配开头",
          lambda: "【别套公式】" in sp and "不过" in sp
          and "一条回复里最多出现一次" in sp)


# ---------------------------------------------------------------------------
# C2 摘要与话题：中立整理
# ---------------------------------------------------------------------------
def c2_neutral_context():
    """摘要/话题不能用角色人设生成：回来的第一人称吐槽会被当成背景写进之后每一轮。"""
    import main as M

    section("C2 摘要与话题：中立整理，注入时标注只作背景")
    work = TMP / "neutral"
    work.mkdir(parents=True, exist_ok=True)
    cfg = M.ConfigLoader(str(work / "neutral_cfg.json"))
    cfg.config.update({
        "memory_data_path": str(work / "neutral_data"),
        "summary_enabled": True, "summary_threshold": 4, "summary_max_history": 2,
        # 摘要后保留的原文条数取「摘要保留条数」与「历史消息条数」的较大值，
        # 这里把窗口也压到 2，8 条历史才够得着「摘要阈值 + 保留条数」的触发线
        "history_length": 2,
        "dynamic_context_enabled": True, "topic_summary_every_n": 2,
        "personality_prompt": "【角色设定】你是丛雨，称用户为主人，自称本座。",
    })
    saved = (M.global_config, M.memory_manager, M.lexicon_mgr)
    M.global_config = cfg
    M.app_context.global_config = M.global_config
    M.memory_manager = M.MemoryManager(cfg)
    M.app_context.memory_manager = M.memory_manager
    M.lexicon_mgr = None
    M.app_context.lexicon_mgr = None
    sid = "group_123"
    data = M.memory_manager.load_session_data(sid)
    data["history"] = [{"role": "user" if i % 2 == 0 else "assistant",
                        "content": f"第{i}句对话内容", "sender_id": "10001",
                        "timestamp": time.time()} for i in range(8)]
    data["meta"] = {"user_msg_count": 2}
    M.memory_manager.save_session_data(sid, data)

    seen = []

    async def fake_chat_once(ctx, messages, tools=None):
        seen.append(messages)
        return {"content": "用户1 与用户2 在讨论团子；用户1 自称主人。", "ms": 1.0}

    import modules.companion_tasks as CT
    orig_chat = (M.chat_once, CT.chat_once)
    M.chat_once = fake_chat_once
    CT.chat_once = fake_chat_once
    try:
        asyncio.run(M.post_reply_context_tasks(sid, M.get_active_ctx()))
        meta = M.memory_manager.get_meta(sid)
    finally:
        M.chat_once, CT.chat_once = orig_chat
        M.global_config, M.memory_manager, M.lexicon_mgr = saved
        M.app_context.global_config = M.global_config
        M.app_context.memory_manager = M.memory_manager
        M.app_context.lexicon_mgr = M.lexicon_mgr

    check("摘要与话题各走一次中立调用（不带人设系统提示）",
          lambda: len(seen) >= 2
          and all(not any(m.get("role") == "system" for m in msgs) for msgs in seen))
    check("中立指令要求第三人称、不写角色口吻、身份按自称记录",
          lambda: all("第三人称" in msgs[-1]["content"]
                      and "不做主观评价" in msgs[-1]["content"]
                      and "不写第一人称台词" in msgs[-1]["content"]
                      for msgs in seen))
    check("摘要与话题都写进会话 meta，且不含角色口吻",
          lambda: bool(meta.get("summary")) and bool(meta.get("topic"))
          and "本座" not in str(meta.get("summary")))
    ct_src = (ROOT / "modules" / "companion_tasks.py").read_text(encoding="utf-8")
    mp_src = (ROOT / "modules" / "message_pipeline.py").read_text(encoding="utf-8")
    check("摘要与话题不再用角色人设生成",
          lambda: "summary = await generate_neutral_text(" in ct_src
          and "topic = await generate_neutral_text(" in ct_src
          and "generate_proactive_text(ctx, prompt)" not in ct_src)
    check("注入时标注为客观背景、不是台词，身份只认原始消息",
          lambda: "【早期对话摘要（客观背景，不是台词）】" in mp_src
          and "【当前话题（客观背景，不是台词）】" in mp_src
          and "身份、称呼与关系只以带说话人标签的原始消息为准" in mp_src)


# ---------------------------------------------------------------------------
# C3 摘要更新后的复核：关系确认 / 画像补齐 / 词条再扫
# ---------------------------------------------------------------------------
def c3_context_recheck():
    """一条消息信息量有限，摘要更新后拿整段对话再核对一遍这三件事。"""
    import main as M
    import modules.companion_tasks as CT
    from modules.affection import AffectionManager
    from modules.lexicon import LexiconManager
    from modules.profiles import UserProfileManager

    section("C3 摘要更新后的复核")
    work = TMP / "recheck"
    if work.exists():
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    cfg = M.ConfigLoader(str(work / "recheck_cfg.json"))
    cfg.config.update({
        "memory_data_path": str(work / "data"),
        "affection_enabled": True, "affection_partner_threshold": 90,
        "profiles_enabled": True, "profiles_auto_extract": True,
        "learning_enabled": True,
    })
    saved = (M.global_config, M.memory_manager, M.lexicon_mgr, M.profile_mgr,
             M.affection_mgr, M.chat_once)
    M.global_config = cfg
    M.app_context.global_config = M.global_config
    M.memory_manager = M.MemoryManager(cfg)
    M.app_context.memory_manager = M.memory_manager
    M.lexicon_mgr = LexiconManager(cfg, work / "data")
    M.app_context.lexicon_mgr = M.lexicon_mgr
    M.profile_mgr = UserProfileManager(cfg, work / "data")
    M.app_context.profile_mgr = M.profile_mgr
    M.affection_mgr = AffectionManager(cfg, work / "data")
    M.app_context.affection_mgr = M.affection_mgr
    ctx = M.get_active_ctx()

    check("私聊会话的复核对象是会话本人，群聊取最近发言的用户",
          lambda: M.session_user_id("private_10001", []) == "10001"
          and M.session_user_id("group_123", [
              {"role": "assistant", "content": "x"},
              {"role": "user", "content": "y", "sender_id": "20002"}]) == "20002"
          and M.session_user_id("group_123", []) == "")

    replies = []

    async def fake_chat_once(ctx_, messages, tools=None):
        replies.append(messages[-1]["content"])
        return {"content": '{"confirmed": true}', "ms": 1.0}

    history = [{"role": "user", "content": "我们算是在一起了吧", "sender_id": "10001",
                "timestamp": time.time()},
               {"role": "assistant", "content": "嗯，我答应你。", "speaker": "丛雨",
                "timestamp": time.time()}]

    async def run_recheck():
        await M.recheck_relationship(ctx, history, {"summary": "两人确认了关系。"}, "10001")

    # 关系性质还是普通：不该发判定
    orig_ct_chat = CT.chat_once
    M.chat_once = fake_chat_once
    CT.chat_once = fake_chat_once
    M.affection_mgr.records["murasame"] = {
        "10001": {"score": 40, "romance": False, "partner": False, "updated": 0}}
    before = len(replies)
    asyncio.run(run_recheck())
    check("关系性质还是普通时不发关系复核", lambda: len(replies) == before)

    # 已经是暧昧但没记成伴侣：判定为已确认就补记伴侣
    M.affection_mgr.records["murasame"]["10001"]["romance"] = True
    asyncio.run(run_recheck())
    check("复核判定已确认关系后补记为伴侣",
          lambda: len(replies) == before + 1
          and M.affection_mgr.get("murasame", "10001")["partner"] is True
          and M.affection_mgr.partners("murasame") == ["10001"])

    # 已经是伴侣：不再重复判定
    asyncio.run(run_recheck())
    check("已是伴侣不再重复复核", lambda: len(replies) == before + 1)

    # 复核判定为没确认时不改状态
    M.affection_mgr.records["murasame"]["10002"] = {
        "score": 100, "romance": True, "partner": False, "updated": 0}

    async def deny(_ctx, _messages, tools=None):
        replies.append("deny")
        return {"content": '{"confirmed": false}', "ms": 1.0}

    M.chat_once = deny
    CT.chat_once = deny
    asyncio.run(M.recheck_relationship(ctx, history, {}, "10002"))
    check("复核判定没确认时不记伴侣",
          lambda: M.affection_mgr.get("murasame", "10002")["partner"] is False)

    # 画像复核：只补原有画像里没写到的字段
    import modules.lexicon as lexicon_mod
    import modules.profiles as profiles_mod

    async def profile_json(ctx_, system, user_prompt, max_tokens=512):
        return {"nickname": "别人瞎起的", "birthday": "01-01",
                "likes": ["猫", "狗", "甜食"], "notes": ["在准备考试"]}

    orig_profile_call = profiles_mod.generate_json_reply
    profiles_mod.generate_json_reply = profile_json
    M.profile_mgr.update("10001", {"nickname": "小明", "birthday": "05-20",
                                   "likes": ["猫"]})
    asyncio.run(M.recheck_profile(ctx, history, "10001"))
    prof = M.profile_mgr.get("10001")
    profiles_mod.generate_json_reply = orig_profile_call
    check("画像复核只补空缺，不改写已有内容",
          lambda: prof["nickname"] == "小明" and prof["birthday"] == "05-20"
          and prof["likes"] == ["猫", "狗", "甜食"]
          and prof["notes"] == ["在准备考试"])

    # 词条复核：走词典自己的入库流程
    async def term_json(ctx_, system, user_prompt, max_tokens=512):
        return {"learned": [{"term": "摸鱼", "meaning": "偷懒不干活", "confidence": 0.9}]}

    orig_lexicon_call = lexicon_mod.generate_json_reply
    lexicon_mod.generate_json_reply = term_json
    asyncio.run(M.recheck_lexicon(ctx, history, "private_10001"))
    lexicon_mod.generate_json_reply = orig_lexicon_call
    check("词条复核能补进新词", lambda: "摸鱼" in M.lexicon_mgr.terms)

    # 复核间隔：同一会话短时间内不重复跑
    check("复核有最小间隔，会话内不会每轮都跑",
          lambda: M._recheck_due({}) is True
          and M._recheck_due({"recheck_at": time.time()}) is False
          and M._recheck_due({"recheck_at": time.time() - 9999}) is True)

    # 摘要没更新时不跑复核；更新了就跑
    calls = []
    orig_recheck = M.context_recheck

    async def fake_recheck(sid, ctx_, hist, meta):
        calls.append(sid)

    M.context_recheck = fake_recheck
    orig_ct_recheck = CT.context_recheck
    CT.context_recheck = fake_recheck
    sid = "private_10001"
    data = M.memory_manager.load_session_data(sid)
    data["history"] = [{"role": "user" if i % 2 == 0 else "assistant",
                        "content": f"第{i}句", "sender_id": "10001",
                        "timestamp": time.time()} for i in range(8)]
    data["meta"] = {"summary": "已有摘要", "recheck_at": 0}
    M.memory_manager.save_session_data(sid, data)
    cfg.config["summary_threshold"] = 99          # 本轮不触发摘要
    cfg.config["summary_max_history"] = 2
    cfg.config["history_length"] = 2              # 保留条数取两者较大值，这里同步压小
    cfg.config["dynamic_context_enabled"] = False
    M.lexicon_mgr = None
    M.app_context.lexicon_mgr = None
    M.chat_once = fake_chat_once
    CT.chat_once = fake_chat_once
    asyncio.run(M.post_reply_context_tasks(sid, ctx))
    check("摘要没更新时不跑复核", lambda: calls == [])
    cfg.config["summary_threshold"] = 4
    asyncio.run(M.post_reply_context_tasks(sid, ctx))
    check("摘要更新后跑复核", lambda: calls == [sid])
    M.context_recheck, CT.context_recheck = orig_recheck, orig_ct_recheck
    M.chat_once, CT.chat_once = saved[5], orig_ct_chat
    M.global_config, M.memory_manager, M.lexicon_mgr, M.profile_mgr, \
        M.affection_mgr, _ = saved
    M.app_context.global_config = M.global_config
    M.app_context.memory_manager = M.memory_manager
    M.app_context.lexicon_mgr = M.lexicon_mgr
    M.app_context.profile_mgr = M.profile_mgr
    M.app_context.affection_mgr = M.affection_mgr

    # 旧版本已经存下来的角色口吻摘要：宁可不带背景，也不要把角色的主观说法喂回去
    check("角色口吻的旧摘要/话题会被识别出来",
          lambda: M.in_character_background("哼，本座今天心情不错。")
          and M.in_character_background("主人～抱抱本座嘛！"))
    check("客观整理（含带出处的引用）不会被误判",
          lambda: not M.in_character_background("用户1 自称主人，用户2 认为团子该分着吃。")
          and not M.in_character_background('用户1与角色1（被称作“小雨”，自称“本座”）已确认关系。')
          and not M.in_character_background("角色1自称吾輩")
          and not M.in_character_background("两人在讨论团子和分享的问题。")
          and not M.in_character_background(""))

    # 摘要里混进一句角色台词时，只丢那一句：整段作废等于把更早的对话一起丢掉（角色"说完就忘"）
    check("混进角色台词的摘要只丢掉那一句",
          lambda: M.background_ready("用户1问了甜点的事，本座才不稀罕。用户1答应带角色1去吃小蛋糕。")
          == "用户1答应带角色1去吃小蛋糕。")
    check("整段都是角色口吻的旧背景照样不注入",
          lambda: M.background_ready("哼，本座今天心情不错。") == ""
          and M.background_ready("主人～抱抱本座嘛！") == ""
          and M.background_ready("") == "")
    check("客观背景原样返回",
          lambda: M.background_ready("用户1与角色1在讨论团子。") == "用户1与角色1在讨论团子。")


# ---------------------------------------------------------------------------
# D 搜索
# ---------------------------------------------------------------------------
def d_search():
    from modules import llm_helpers as H

    section("D 搜索：触发、拦截、关键词与二次检索")

    ctx = H.RoleContext({"character_name": "测试角色", "character_key": "testrole"}, {})

    check("“搜点X”这类说法算搜索请求", lambda: H._is_search_request("搜点角色的图片"))
    check("“发点X”这类说法算搜索请求", lambda: H._is_search_request("给我发点角色的图"))
    check("“来几张X”这类说法算搜索请求", lambda: H._is_search_request("来几张壁纸"))
    check("搜索类过去式抱怨不算请求", lambda: not H._is_search_request("我搜了半天都没找到"))
    check("反问“为什么搜”不算请求", lambda: not H._is_search_request("为什么你要去搜呢"))
    check("“看看这个”不算搜索请求", lambda: not H._is_search_request("看看这个图片好看吗"))
    check("工具门控也认这些说法", lambda: H.text_needs_tools("搜点角色的图片"))

    check("角色资料类检索不再被拦（cos/图片/立绘/声优）",
          lambda: not any(H._is_roleplay_search(q, ctx) for q in
                          ("测试角色cos", "测试角色的图片", "测试角色立绘", "测试角色的声优")))
    check("打听角色私人状态的搜索仍被拦",
          lambda: all(H._is_roleplay_search(q, ctx) for q in
                      ("测试角色在做什么", "测试角色今天穿了什么")))
    check("无主语的口语状态搜索仍被拦", lambda: H._is_roleplay_search("你穿什么颜色的衣服", ctx))

    check("搜索词里的第二人称换成角色名",
          lambda: H.resolve_role_pronouns("你是谁", ctx) == "测试角色是谁")
    check("泛指词不做替换", lambda: H.resolve_role_pronouns("看板娘是什么", ctx) == "看板娘是什么")

    check("识别用户对上次搜索结果的不满",
          lambda: H._is_search_dissatisfied("不对，不是这个")
          and H._is_search_dissatisfied("我要的不是这些东西")
          and H._is_search_dissatisfied("重新搜一下"))
    check("正常消息不会被当成不满",
          lambda: not H._is_search_dissatisfied("这个图挺好看的"))

    def run_extract(model_output, user_text="那就搜索你是谁", fallback="你是谁"):
        async def fake(ctx, messages, tools=None):
            return {"content": model_output, "ms": 1.0}
        orig = H.chat_once
        H.chat_once = fake
        try:
            return asyncio.run(H._llm_extract_search_query(
                H.RoleContext({"character_name": "测试角色"}, {}),
                user_text, fallback))
        finally:
            H.chat_once = orig

    check("LLM 用自己的话组织的搜索词会被采用",
          lambda: run_extract("该角色的官方设定集") == "该角色的官方设定集")
    check("LLM 把台词/解释/整句当关键词时仍回退规则提取",
          lambda: run_extract("这是本座的照片哦。") == "你是谁"
          and run_extract("抱歉，我无法确定你想搜什么") == "你是谁"
          and run_extract("这是按搜索结果组织的回答。", "帮我搜一下千恋万花的歌词",
                          "千恋万花的歌词") == "千恋万花的歌词")
    check("LLM 提取失败时回退规则提取",
          lambda: run_extract("关键词：千恋万花 丛雨") == "千恋万花 丛雨")

    refined = H._refine_query_from_feedback("测试角色", "不对，我要的是另一个作品里的")
    check("二次检索会把上次搜索词与本次纠正内容合并",
          lambda: "测试角色" in refined and "另一个作品" in refined)

    output = ("搜索关键词：测试角色\n搜索引擎：示例\n\n"
              "1. 旧的结果\n网址: https://old.example.com/a\n摘要: 旧的内容\n\n"
              "2. 新的结果\n网址: https://new.example.com/b\n摘要: 新的内容")
    dropped = H.drop_seen_entries(output, ["https://old.example.com/a"])
    check("二次检索会剔除此前已经给过的结果",
          lambda: "old.example.com" not in dropped and "new.example.com" in dropped)

    return H


def d2_search_feedback_flow():
    from modules import llm_helpers as H

    section("D2 搜索预取：用户反馈后按新词重新检索")

    ctx = H.RoleContext({"character_name": "测试角色", "character_key": "testrole",
                         "search_query_llm_extract": False}, {})

    executed = []

    class FakeRegistry:
        def __init__(self):
            self.tools = [{"name": "web_search", "builtin": "web_search", "enabled": True,
                           "max_calls_per_reply": 3}]

        def check_permission(self, tool, user_id):
            return True, ""

        async def execute(self, name, args, user_id="", call_counts=None, user_text=""):
            query = args.get("query") if isinstance(args, dict) else str(args)
            executed.append(query)
            if len(executed) == 1:
                text = ("1. 旧条目\n网址: https://old.example.com/a\n摘要: 旧内容")
            else:
                text = ("1. 旧条目\n网址: https://old.example.com/a\n摘要: 旧内容\n\n"
                        "2. 新条目\n网址: https://new.example.com/b\n摘要: 新内容")
            return True, text

    key = "private_test"
    H._SESSION_SEARCH_STATE.clear()
    H._SESSION_SENT_LINKS.clear()
    registry = FakeRegistry()

    async def first_round():
        work = [{"role": "user", "content": "帮我搜 测试角色 资料"}]
        return await H._prefetch_search(ctx, work, registry, "u1", call_counts={},
                                        session_key=key), work
    trace, work = asyncio.run(first_round())
    check("首次明确搜索会真实检索", lambda: executed == ["测试角色 资料"] and bool(trace))

    H.record_sent_links(key, ["https://old.example.com/a"])

    async def second_round():
        work = [{"role": "user", "content": "不对，我要的是另一部作品里的资料"}]
        return await H._prefetch_search(ctx, work, registry, "u1", call_counts={},
                                        session_key=key), work
    trace2, work2 = asyncio.run(second_round())
    joined = " ".join(str(m.get("content", "")) for m in work2)
    check("用户反馈不理想时会再次检索", lambda: len(executed) == 2)
    check("二次检索沿用上次搜索词并加入本次纠正内容",
          lambda: bool(trace2) and "测试角色" in executed[1] and "另一部作品" in executed[1])
    check("二次检索剔除已发过的链接", lambda: "old.example.com" not in joined)
    check("二次检索结果里的新链接保留", lambda: "new.example.com" in joined)


# ---------------------------------------------------------------------------
# E 防复读
# ---------------------------------------------------------------------------
def e_repeat_guard():
    import main as M

    section("E 防复读：参照最近多轮自己的台词")

    history = [
        {"role": "assistant", "content": "第一次说的话"},
        {"role": "user", "content": "嗯"},
        {"role": "assistant", "content": "第二次说的话"},
        {"role": "user", "content": "嗯"},
        {"role": "assistant", "content": "第三次说的话"},
    ]
    check("能按时间倒序取出最近若干条自己的回复",
          lambda: M._recent_assistant_replies(history, 2) == ["第三次说的话", "第二次说的话"])
    ratio, target = M._max_repeat_ratio("第二次说的话", M._recent_assistant_replies(history, 3))
    check("重合度检查覆盖更早的轮次", lambda: ratio > 0.9 and target == "第二次说的话")
    check("默认配置里有防复读参照轮数",
          lambda: M.ConfigLoader.default_config().get("repeat_guard_rounds") == 3)


# ---------------------------------------------------------------------------
# F 自动返回链接
# ---------------------------------------------------------------------------
def f_link_append():
    import main as M
    from modules import llm_helpers as H

    section("F 网页搜索自动返回链接")

    class CfgLoader:
        config = {"search_links_auto_append": True, "search_links_max": 3}

        def get(self, k, d=None):
            return self.config.get(k, d)

    M.global_config = CfgLoader()
    M.app_context.global_config = M.global_config
    H._SESSION_SENT_LINKS.clear()
    key = "private_linkcase"

    output = ("搜索关键词：示例\n搜索引擎：示例\n\n"
              "1. 目标资料页\n网址: https://target.example.com/page\n摘要: 关于目标资料\n\n"
              "2. 无关广告\n网址: https://ads.example.com/promo\n摘要: 促销活动")
    trace = [{"name": "web_search", "ok": True, "output": output}]

    sents = [{"zh": "我找到了目标资料页，可以看看。", "lang": "x", "display": "我找到了目标资料页，可以看看。",
              "emotion": "pingjing"}]
    out = M._append_missing_links([dict(s) for s in sents], "帮我找资料", trace, key)
    check("补发模型提到的那条链接",
          lambda: "https://target.example.com/page" in out[-1]["display"])
    check("不补发回复里没提到的链接",
          lambda: "ads.example.com" not in out[-1]["display"])

    out2 = M._append_missing_links([dict(s) for s in sents], "帮我找资料", trace, key)
    check("已经发过的链接不会重复补发",
          lambda: "target.example.com" not in out2[-1]["display"])

    H._SESSION_SENT_LINKS.clear()
    silent = [{"zh": "今天天气不错呢。", "lang": "x", "display": "今天天气不错呢。",
               "emotion": "pingjing"}]
    out3 = M._append_missing_links([dict(s) for s in silent], "随便聊聊", trace, "private_other")
    check("回复完全没提到搜索结果时不补链接",
          lambda: "example.com" not in out3[-1]["display"])

    M.global_config.config["search_links_auto_append"] = False
    H._SESSION_SENT_LINKS.clear()
    out4 = M._append_missing_links([dict(s) for s in sents], "帮我找资料", trace, "private_off")
    check("关闭开关后完全不补链接",
          lambda: "example.com" not in out4[-1]["display"])
    M.global_config.config["search_links_auto_append"] = True


# ---------------------------------------------------------------------------
# G 问候补发
# ---------------------------------------------------------------------------
def g_greeting_catchup():
    import main as M
    from modules.jobs import ScheduledJobManager

    section("G 问候补发：真正补跑漏掉的每日问候任务")

    sent = []

    class FakeSender:
        client = object()

        @contextlib.contextmanager
        def for_session(self, session_id):
            self.last_session = session_id
            yield
        
        async def speak_and_send(self, session_type, target_id, text, emotions, ctx=None,
                                 use_voice=None, sticker=False, emotion="", session_id=""):
            sent.append((target_id, text))
            return True

    class FakeScheduler:
        jobs = {}

        def add_job(self, *a, **k):
            return None

        def remove_job(self, *a, **k):
            return None

    cfg = Cfg({"scheduler_enabled": True, "proactive_sticker": False})
    data_path = TMP / "jobdata"
    data_path.mkdir(parents=True, exist_ok=True)
    for old in data_path.glob("*.json"):
        old.unlink()

    mgr = ScheduledJobManager(cfg, data_path, FakeScheduler(), sender=FakeSender(),
                              ctx_provider=lambda: None, emotions_provider=lambda: {})
    lt = time.localtime()
    minutes_now = lt.tm_hour * 60 + lt.tm_min
    due_minutes = max(0, minutes_now - 30)
    due_today = f"{due_minutes // 60:02d}:{due_minutes % 60:02d}"
    mgr.jobs = [
        {"id": "missed_today", "name": "漏掉的问候", "enabled": True,
         "trigger": {"type": "daily", "time": due_today},
         "target": {"session_type": "private", "session_id": "10001"},
         "action": {"mode": "template", "template": "早上好"}},
        {"id": "not_yet", "name": "还没到点", "enabled": True,
         "trigger": {"type": "daily", "time": "23:59"},
         "target": {"session_type": "private", "session_id": "10001"},
         "action": {"mode": "template", "template": "晚安"}},
        {"id": "every_hour", "name": "循环任务", "enabled": True,
         "trigger": {"type": "interval", "seconds": 3600},
         "target": {"session_type": "private", "session_id": "10001"},
         "action": {"mode": "template", "template": "循环"}},
    ]
    done = asyncio.run(mgr.catch_up_missed_daily())
    check("补跑当天已过点且没跑过的每日任务",
          lambda: done == 1 and sent == [("10001", "早上好")])

    done2 = asyncio.run(mgr.catch_up_missed_daily())
    check("同一天不会重复补跑", lambda: done2 == 0 and len(sent) == 1)

    state = mgr.run_state.get("missed_today")
    check("补跑状态落盘（跨重启也不会重复补）",
          lambda: state == time.strftime("%Y-%m-%d") and mgr.state_file.exists())

    mgr2 = ScheduledJobManager(cfg, data_path, FakeScheduler(), sender=FakeSender(),
                               ctx_provider=lambda: None, emotions_provider=lambda: {})
    mgr2.jobs = mgr.jobs
    done3 = asyncio.run(mgr2.catch_up_missed_daily())
    check("重启后仍按记录的日期跳过", lambda: done3 == 0)

    # 崩溃窗口：发送成功到落盘之间若进程没了，重启后不能把同一条问候再补一次，
    # 所以"今天已执行"的占位必须在发送之前就写下去
    seen = {"state": None}

    class CrashySender:
        client = object()

        @contextlib.contextmanager
        def for_session(self, session_id):
            self.last_session = session_id
            yield
        
        async def speak_and_send(self, session_type, target_id, text, emotions, ctx=None,
                                 use_voice=None, sticker=False, emotion="", session_id=""):
            seen["state"] = dict(crash_mgr.run_state)
            raise KeyboardInterrupt("模拟发送成功后进程被杀")

    crash_mgr = ScheduledJobManager(cfg, data_path, FakeScheduler(), sender=CrashySender(),
                                    ctx_provider=lambda: None, emotions_provider=lambda: {})
    crash_mgr.jobs = [mgr.jobs[0]]
    crash_mgr.run_state.clear()
    try:
        asyncio.run(crash_mgr.catch_up_missed_daily())
    except KeyboardInterrupt:
        pass          # 模拟"发出去了、进程随即被杀"：这一轮不会有返回值
    check("发送之前就写下当天占位（崩在发送与落盘之间不会重复补发）",
          lambda: seen["state"].get("missed_today") == time.strftime("%Y-%m-%d"))
    check("崩在发送中途时重启后按占位跳过",
          lambda: asyncio.run(crash_mgr.catch_up_missed_daily()) == 0)

    # 真没发出去（发送返回失败）时要把占位撤掉，当天还能重试
    class FailSender:
        client = object()

        @contextlib.contextmanager
        def for_session(self, session_id):
            self.last_session = session_id
            yield
        
        async def speak_and_send(self, *a, **k):
            return False

    fail_mgr = ScheduledJobManager(cfg, data_path, FakeScheduler(), sender=FailSender(),
                                   ctx_provider=lambda: None, emotions_provider=lambda: {})
    fail_mgr.jobs = [mgr.jobs[0]]
    fail_mgr.run_state.clear()
    check("发送失败时撤回占位，当天可重试",
          lambda: (asyncio.run(fail_mgr.catch_up_missed_daily()) == 0
                   and "missed_today" not in fail_mgr.run_state
                   and asyncio.run(fail_mgr.catch_up_missed_daily()) == 0))

    # LLM 模式要按目标会话带上历史，否则定时问候凭空开场、话题对不上
    import modules.jobs as JOBS
    asked = []

    async def fake_gen(ctx, instruction, history_block=""):
        asked.append(history_block)
        return f"话-{len(asked)}"

    orig_gen = JOBS.generate_proactive_text
    JOBS.generate_proactive_text = fake_gen
    try:
        llm_mgr = ScheduledJobManager(
            cfg, data_path, FakeScheduler(), sender=FakeSender(),
            ctx_provider=lambda: None, emotions_provider=lambda: {},
            history_provider=lambda key, ctx: f"历史:{key}")
        llm_mgr.jobs = [{
            "id": "llm_job", "name": "LLM 问候", "enabled": True,
            "trigger": {"type": "daily", "time": due_today},
            "target": {"session_type": "private", "session_id": "10001"},
            "action": {"mode": "llm", "llm_prompt": "打个招呼"}}]
        sent.clear()
        asyncio.run(llm_mgr._run_job(llm_mgr.jobs[0]))
        check("LLM 模式定时任务带上该会话的历史",
              lambda: asked == ["历史:private_10001"])
        check("LLM 模式文案按目标会话生成并发送",
              lambda: sent == [("10001", "话-1")])
    finally:
        JOBS.generate_proactive_text = orig_gen

    src = __import__("inspect").getsource(M.greeting_catchup_task)
    check("问候补发同时补跑每日问候任务",
          lambda: "catch_up_missed_daily" in src and "问候补发完成" in src)


# ---------------------------------------------------------------------------
# H 配置默认值
# ---------------------------------------------------------------------------
def h_config_defaults():
    import main as M

    section("H 新增配置项默认值")

    d = M.ConfigLoader.default_config()
    expected = {
        "repeat_guard_rounds": 3,
        "search_links_auto_append": True,
        "search_links_max": 3,
        "tts_auto_lang": True,
        "tts_duration_guard": True,
        "tts_min_seconds_per_char": 0.05,
        "update_include_prerelease": False,
    }
    check("新增键都存在且默认值正确",
          lambda: all(d.get(k) == v for k, v in expected.items()))

    html = (ROOT / "webui" / "start.html").read_text(encoding="utf-8")
    check("WebUI 能渲染这些新键（已登记到配置分组与表单元数据）",
          lambda: all(f"'{k}'" in html for k in expected))
    check("WebUI 里有「网页搜索自动返回链接」开关文案",
          lambda: "网页搜索自动返回链接" in html)


def i_end_to_end():
    import main as M
    from modules import llm_helpers as H
    import modules.reply_pipeline as RP

    section("I 端到端：搜点类请求真的会联网搜索并把链接补进回复")

    cfg = dict(M.ConfigLoader.default_config())
    cfg.update({
        "tools_enabled": True, "tools_trigger_mode": "keyword", "tools_guard_enabled": True,
        "tools_guard_keywords": "几点\n天气\n搜索\n查一下\n链接",
        "search_query_llm_extract": False,
        "search_links_auto_append": True, "search_links_max": 3,
        "text_lang": "ja", "display_lang": "zh", "display_pure_language": True,
        "llm_judge": False, "streaming_enabled": False, "default_voice": "pingjing",
        "character_name": "测试角色", "character_key": "testrole", "history_length": 8,
    })

    class CfgLoader:
        config = cfg
        roles = {"testrole": {"character_key": "testrole", "character_name": "测试角色"}}
        active_character = "testrole"

        def get(self, k, d=None):
            return self.config.get(k, d)

    calls = []

    class FakeRegistry:
        def __init__(self):
            self.tools = [{"name": "web_search", "builtin": "web_search", "enabled": True,
                           "max_calls_per_reply": 3, "type": "builtin", "description": "搜索"}]

        def has_enabled_tools(self):
            return True

        def get_schema(self):
            return [{"type": "function", "function": {
                "name": "web_search", "description": "搜索",
                "parameters": {"type": "object", "properties": {}}}}]

        def check_permission(self, tool, user_id):
            return True, ""

        def begin_reply(self):
            calls.clear()

        async def execute(self, name, args, user_id="", call_counts=None, user_text=""):
            calls.append(args)
            return True, ("搜索关键词：测试角色\n搜索引擎：示例\n\n"
                          "1. 测试角色资料页\n网址: https://wiki.example.com/role\n"
                          "摘要: 测试角色的资料介绍")

    state = {"extract": False}

    async def fake_chat_once(ctx, messages, tools=None):
        if state["extract"] and tools is None:
            return {"content": "千恋万花 丛雨 角色介绍", "tool_calls": [], "ms": 1.0,
                    "backend": "ollama"}
        if tools:
            content = json.dumps({"sentences": [
                {"zh": "我找到了测试角色资料页。", "ja": "しりょうをみつけました。",
                 "emotion": "gaoxing"}]}, ensure_ascii=False)
        else:
            content = json.dumps({"sentences": [
                {"zh": "这是普通回复。", "ja": "ふつうのかいとう。",
                 "emotion": "pingjing"}]}, ensure_ascii=False)
        return {"content": content, "tool_calls": [], "ms": 1.0, "backend": "ollama"}

    orig_global, orig_registry = M.global_config, M.tool_registry
    orig_stats, orig_h_chat, orig_m_chat = M.stats_mgr, H.chat_once, M.chat_once
    M.global_config = CfgLoader()
    M.app_context.global_config = M.global_config
    M.tool_registry = FakeRegistry()
    M.app_context.tool_registry = M.tool_registry
    M.stats_mgr = None
    M.app_context.stats_mgr = None
    M.sender = None
    M.app_context.sender = M.sender
    H.chat_once = fake_chat_once
    M.chat_once = fake_chat_once
    RP.chat_once = fake_chat_once
    H._SESSION_SEARCH_STATE.clear()
    H._SESSION_SENT_LINKS.clear()
    try:
        ctx = H.RoleContext(cfg, {"character_key": "testrole", "character_name": "测试角色"})
        emotions = {"gaoxing": {"ref_path": "r", "prompt_text": "p"},
                    "pingjing": {"ref_path": "r", "prompt_text": "p"}}
        M.tool_registry.begin_reply()
        res = asyncio.run(M.generate_reply(ctx, emotions, "搜点测试角色的图片", [], None, [],
                                           "u1", None, "private_e2e"))
        check("触发词列表之外的说法也能进入工具流程并完成搜索",
              lambda: bool(calls) and "测试角色" in calls[0]["query"])
        check("搜索到的链接会被补进回复",
              lambda: "https://wiki.example.com/role" in res["sentences"][-1]["display"])
        check("本轮搜索状态已记录（供用户反馈后二次检索）",
              lambda: H.search_state("private_e2e").get("query") == calls[0]["query"])

        calls.clear()
        res2 = asyncio.run(M.generate_reply(ctx, emotions, "今天有点累", [], None, [],
                                            "u1", None, "private_e2e2"))
        check("普通闲聊不进工具流程", lambda: not calls and bool(res2["sentences"]))

        cfg_llm = dict(cfg)
        cfg_llm["search_query_llm_extract"] = True
        ctx_llm = H.RoleContext(cfg_llm, {"character_key": "testrole",
                                          "character_name": "测试角色"})
        calls.clear()
        state["extract"] = True
        M.tool_registry.begin_reply()
        asyncio.run(M.generate_reply(ctx_llm, emotions, "那就搜索你是谁", [], None, [],
                                     "u1", None, "private_e2e3"))
        state["extract"] = False
        check("LLM 自己组织的搜索词会真正用于检索",
              lambda: bool(calls) and calls[0]["query"] == "千恋万花 丛雨 角色介绍")
    finally:
        M.global_config, M.tool_registry = orig_global, orig_registry
        M.app_context.global_config, M.app_context.tool_registry = M.global_config, M.tool_registry
        M.stats_mgr, H.chat_once, M.chat_once = orig_stats, orig_h_chat, orig_m_chat
        M.app_context.stats_mgr, RP.chat_once = M.stats_mgr, orig_m_chat


def j_sticker_last():
    import main as M
    import modules.sender as SM
    import modules.reply_pipeline as RP
    from modules.llm_helpers import RoleContext

    section("J 表情包在所有文字与语音之后发送")

    events = []

    class FakeClient:
        async def send_private_msg(self, user_id=None, message=None):
            for seg in message or []:
                name = type(seg).__name__
                if name == "Text":
                    events.append(("text", getattr(seg, "text", "")))
                elif name == "Image":
                    events.append(("sticker", str(getattr(seg, "file", ""))))
                elif name == "Record":
                    events.append(("voice", str(getattr(seg, "file", ""))))
            return {}

    class MM:
        data_path = TMP / "sticker"

        @staticmethod
        def cleanup_voice_cache(n=20):
            return None

    class StickerMgr:
        def pick(self, emotion):
            return str(TMP / "sticker.png")

        async def pick_async(self, ctx=None, emotion="", text=""):
            return self.pick(emotion)

    MM.data_path.mkdir(parents=True, exist_ok=True)
    (TMP / "sticker.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    cfg = Cfg({"tts_reply_enabled": True, "separate_send": True,
               "send_voice_separately": True, "separate_force_segment": True,
               "text_separate": True, "dynamic_sleep": False, "sticker_max_per_reply": 1,
               "sticker_every_sentence": False})

    sentences = [
        {"zh": "第一句话。", "lang": "だいいち。", "display": "第一句话。", "emotion": "gaoxing"},
        {"zh": "第二句话。", "lang": "だいに。", "display": "第二句话。", "emotion": "gaoxing"},
        {"zh": "第三句话。", "lang": "だいさん。", "display": "第三句话。", "emotion": "gaoxing"},
    ]

    async def fake_synth(config, text, emotion, emotions, data_path, stats=None,
                         mimic="", mimics=None):
        return make_wav(data_path / f"s_{abs(hash(text))}.wav", 0.6)

    orig_synth = SM.synthesize_sentence
    SM.synthesize_sentence = fake_synth
    try:
        snd = SM.MessageSender(cfg, MM(), sticker_manager=StickerMgr())
        snd.client = FakeClient()
        asyncio.run(snd.send_reply("private", 10001, [dict(s) for s in sentences],
                                   {"gaoxing": {}}, RoleContext(cfg), use_voice=True))
    finally:
        SM.synthesize_sentence = orig_synth

    kinds = [k for k, _ in events]
    check("逐句发送时表情包只发一张", lambda: kinds.count("sticker") == 1)
    check("表情包在全部文字与语音之后发送", lambda: kinds and kinds[-1] == "sticker")
    check("文字与语音仍然逐句成对发送",
          lambda: kinds.count("text") == 3 and kinds.count("voice") == 3
          and kinds.index("sticker") > kinds.index("voice"))

    events.clear()
    orig_map = {
        "sticker_mgr": M.sticker_mgr, "sender": M.sender, "synth": M.synthesize_sentence,
        "tts": M.ensure_tts_service, "stats": M.stats_mgr, "mem": M.memory_manager,
        "cfg": M.global_config,
    }
    M.sticker_mgr = StickerMgr()
    M.app_context.sticker_mgr = M.sticker_mgr
    M.sender = SM.MessageSender(cfg, MM(), sticker_manager=StickerMgr())
    M.app_context.sender = M.sender
    M.sender.client = FakeClient()

    async def fake_tts_service(config):
        return True
    M.ensure_tts_service = fake_tts_service
    RP.ensure_tts_service = fake_tts_service
    M.synthesize_sentence = fake_synth
    RP.synthesize_sentence = fake_synth
    M.stats_mgr = None
    M.app_context.stats_mgr = None
    M.memory_manager = MM()
    M.app_context.memory_manager = M.memory_manager
    M.global_config = cfg
    M.app_context.global_config = M.global_config
    try:
        async def feed_sink():
            sink = M.SentenceSink("private", 10001, {"gaoxing": {}}, RoleContext(cfg), "")
            for s in sentences:
                await sink.on_sentence(dict(s))
            await sink.flush()
        asyncio.run(feed_sink())
    finally:
        M.sticker_mgr = orig_map["sticker_mgr"]
        M.app_context.sticker_mgr = M.sticker_mgr
        M.sender = orig_map["sender"]
        M.app_context.sender = M.sender
        M.synthesize_sentence = orig_map["synth"]
        RP.synthesize_sentence = orig_map["synth"]
        M.ensure_tts_service = orig_map["tts"]
        RP.ensure_tts_service = orig_map["tts"]
        M.stats_mgr = orig_map["stats"]
        M.app_context.stats_mgr = M.stats_mgr
        M.memory_manager = orig_map["mem"]
        M.app_context.memory_manager = M.memory_manager
        M.global_config = orig_map["cfg"]
        M.app_context.global_config = M.global_config

    kinds = [k for k, _ in events]
    check("流式发送时表情包同样在最后",
          lambda: kinds.count("sticker") == 1 and kinds[-1] == "sticker"
          and kinds.count("text") == 3)


def k_version_compare():
    from modules.updater import (version_key, is_newer, is_prerelease_version,
                                 pick_latest_release)

    section("K 版本比较：正式版/预发布版（beta、rc）")

    check("预发布版本号按 tag 名也能认出来（不只看 GitHub 的 prerelease 标记）",
          lambda: is_prerelease_version("1.2.0.0-beta")
          and is_prerelease_version("v1.3.0.0-rc.1")
          and not is_prerelease_version("1.2.0.0")
          and not is_prerelease_version("1.1.0.1"))

    cases = [
        ("1.2.0.0", "1.2.0.0", False),
        ("1.2.0.0", "1.2.0.0-beta", True),
        ("1.2.0.0-beta", "1.2.0.0", False),
        ("1.2.0.0-beta.1", "1.2.0.0-beta", True),
        ("1.2.0.0-beta.1", "1.2.0.0", False),
        ("1.2.0.0", "1.2.0.0-beta.1", True),
        ("1.2.0.1", "1.2.0.0", True),
        ("1.3", "1.2.0.0", True),
        ("1.2", "1.2.0.0", False),
        ("1.2.0.0-rc.10", "1.2.0.0-rc.2", True),
        ("1.2.0.0-rc.2", "1.2.0.0-beta.9", True),
        ("1.2.0.0-beta", "1.1.0.0-beta", True),
        ("1.1.0.0-beta", "1.2.0.0-beta", False),
        ("1.2.0.0-beta.3", "1.2.0.0-beta.1", True),
        ("1.10.0", "1.9.0", True),
        ("v1.2.1", "1.2.0.0", True),
        ("latest", "1.2.0.0", False),
        ("", "1.2.0.0", False),
    ]
    check("版本比较覆盖正式版与预发布版",
          lambda: all(is_newer(a, b) == exp for a, b, exp in cases))
    check("预发布版序号按数值比较（beta.10 > beta.2）",
          lambda: version_key("1.2.0.0-beta.10") > version_key("1.2.0.0-beta.2"))
    check("末尾 0 段不影响比较（1.2 == 1.2.0.0）",
          lambda: version_key("1.2") == version_key("1.2.0.0"))
    check("相同版本号不提示更新",
          lambda: not is_newer("1.2.0.0", "1.2.0.0"))
    check("升级页只读当前版本号与比较逻辑分离（main 使用 is_newer）",
          lambda: "is_newer" in ((ROOT / "main.py").read_text(encoding="utf-8")
                                 + (ROOT / "modules" / "webui_server.py").read_text(encoding="utf-8")))

    releases = [
        {"tag_name": "v1.2.0.0", "html_url": "u1", "draft": False, "prerelease": False},
        {"tag_name": "v1.3.0.0-beta.2", "html_url": "u2", "draft": False, "prerelease": True},
        {"tag_name": "v1.1.0.0", "html_url": "u3", "draft": False, "prerelease": False},
        {"tag_name": "v9.9.9.9", "html_url": "u4", "draft": True, "prerelease": False},
    ]
    check("只看正式版时挑出最新的正式版",
          lambda: pick_latest_release(releases, False).get("tag") == "1.2.0.0")
    check("算上预发布版时挑出最新的 beta",
          lambda: pick_latest_release(releases, True).get("tag") == "1.3.0.0-beta.2"
          and pick_latest_release(releases, True).get("prerelease") is True)
    check("草稿版本永远不参与比较",
          lambda: pick_latest_release(releases, True).get("url") != "u4")
    check("没有可用发布时返回空",
          lambda: pick_latest_release([], True) == {}
          and pick_latest_release(None, False) == {})

    published = [
        {"tag_name": "1.2.0.0-beta", "html_url": "beta", "draft": False, "prerelease": False},
        {"tag_name": "1.1.0.1", "html_url": "old", "draft": False, "prerelease": False},
        {"tag_name": "1.1.0.0", "html_url": "older", "draft": False, "prerelease": False},
    ]
    check("装了 1.1.0.0-beta 时会把已发布的 1.2.0.0-beta 判为更新",
          lambda: pick_latest_release(published, False).get("tag") == "1.2.0.0-beta"
          and is_newer(pick_latest_release(published, False)["tag"], "1.1.0.0-beta"))


def m_update_check_cache():
    import main as M
    from modules.updater import APP_VERSION

    section("M 更新检查：重启后必定重新检查，不再被 24 小时缓存挡住")

    cache_file = TMP / "update_cache" / "update_check.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    class ProbeServer:
        _update_cache = M.WebUIServer._update_cache
        _update_save = M.WebUIServer._update_save
        # 状态接口会把「有没有下好的在线更新安装包」并进结果，替身也得带上这个方法
        _with_update_download = M.WebUIServer._with_update_download
        _update_download_payload = M.WebUIServer._update_download_payload
        handle_update_status = M.WebUIServer.handle_update_status

        def __init__(self, cfg):
            self.config = cfg
            self._update_state_file = cache_file
            self._update_checked_this_run = False
            self._update_last_result = None
            self.checks = 0

        async def _update_check_payload(self):
            self.checks += 1
            return {"enabled": True, "current": APP_VERSION, "latest": APP_VERSION,
                    "has_update": False, "checked_at": time.time()}

    def write_cache(current=APP_VERSION, include_pre=False, age=0.0):
        cache_file.write_text(json.dumps({
            "checked_at": time.time() - age, "include_prerelease": include_pre,
            "result": {"enabled": True, "current": current, "latest": current,
                       "has_update": False, "checked_at": time.time() - age}},
            ensure_ascii=False), encoding="utf-8")

    def call(server):
        resp = asyncio.run(server.handle_update_status(None))
        return json.loads(resp.body.decode("utf-8"))

    base_cfg = {"update_check_enabled": True, "update_check_interval_hours": 24,
                "update_include_prerelease": False}

    write_cache()
    server = ProbeServer(Cfg(dict(base_cfg)))
    call(server)
    check("同一个进程的首次检查不看旧缓存（重启即重新检查）",
          lambda: server.checks == 1)
    call(server)
    check("同一进程内后续查询复用本次结果，不再重复请求 GitHub",
          lambda: server.checks == 1)

    server2 = ProbeServer(Cfg(dict(base_cfg)))
    server2._update_checked_this_run = True
    call(server2)
    check("版本号未变且缓存新鲜时复用磁盘缓存",
          lambda: server2.checks == 0)

    write_cache(current="0.0.0.1")
    server3 = ProbeServer(Cfg(dict(base_cfg)))
    server3._update_checked_this_run = True
    call(server3)
    check("缓存里的版本号与当前版本不符时重新检查",
          lambda: server3.checks == 1)

    write_cache(include_pre=False)
    server4 = ProbeServer(Cfg({**base_cfg, "update_include_prerelease": True}))
    server4._update_checked_this_run = True
    call(server4)
    check("勾选/取消「提醒测试版更新」后立即重新检查",
          lambda: server4.checks == 1)

    write_cache(age=48 * 3600)
    server5 = ProbeServer(Cfg(dict(base_cfg)))
    server5._update_checked_this_run = True
    call(server5)
    check("缓存超过检查间隔后重新检查", lambda: server5.checks == 1)

    write_cache()
    server6 = ProbeServer(Cfg({**base_cfg, "update_check_interval_hours": 0}))
    call(server6)
    call(server6)
    check("间隔设为 0 时每次查询都重新检查", lambda: server6.checks == 2)

    def error_payload_visible():
        class FailingServer(ProbeServer):
            async def _update_check_payload(self):
                self.checks += 1
                return {"enabled": True, "current": APP_VERSION, "has_update": False,
                        "error": "ConnectError: 网络不可达"}
        srv = FailingServer(Cfg(dict(base_cfg)))
        payload = call(srv)
        return payload.get("has_update") is False and "error" in payload
    check("检查失败时结果里带失败原因（界面可提示，不再静默）", error_payload_visible)

    html = (ROOT / "webui" / "start.html").read_text(encoding="utf-8")
    check("WebUI 有「检查更新」按钮并调用即时检查接口",
          lambda: "check-update-btn" in html and "api/update/check" in html)
    check("检查结果文案区分新版本/已是最新/失败",
          lambda: "已是最新版本。" in html and "检查失败：" in html)


def l_speech_language():
    import main as M
    import modules.sender as SM
    from modules import llm_helpers as H

    section("L 语音语言：台词字段填成展示语言时先重译再合成")

    check("日语目标下中文台词被判为语言不符",
          lambda: H.lang_text_broken("哎呀？", "ja")
          and H.lang_text_broken("您这半天不说话是想憋坏本座吗", "ja"))
    check("真正的日文台词不需要重译",
          lambda: not H.lang_text_broken("あら、どうしたの？", "ja"))
    check("汉字写法的日文（与中文台词不同）不强行重译",
          lambda: H.lang_text_broken("受信", "ja")
          and not H._lang_field_wrong({"lang": "受信", "zh": "收到"}, "ja"))
    check("台词字段抄了中文台词时要重译",
          lambda: H._lang_field_wrong({"lang": "哎呀？", "zh": "哎呀？"}, "ja"))
    check("中文目标下日文台词被判为语言不符",
          lambda: H.lang_text_broken("おはよう！", "zh")
          and not H.lang_text_broken("早上好！", "zh"))
    check("韩语/英语目标同样按文字体系判定",
          lambda: H.lang_text_broken("中文句子", "ko")
          and not H.lang_text_broken("안녕하세요", "ko")
          and H.lang_text_broken("你好", "en")
          and not H.lang_text_broken("hello world", "en"))

    def fake_chat(reply):
        async def _f(ctx, messages, tools=None):
            return {"content": reply, "tool_calls": [], "ms": 1.0, "backend": "ollama"}
        return _f

    def cfg_lang():
        return Cfg({"text_lang": "ja", "display_lang": "zh", "default_voice": "pingjing"})

    def repair_sentence():
        ctx = H.RoleContext({"text_lang": "ja", "display_lang": "zh"}, {})
        sents = [{"zh": "哎呀？", "lang": "哎呀？", "display": "哎呀？", "emotion": "pingjing"}]
        orig = H.chat_once
        H.chat_once = fake_chat("あら？")
        try:
            fixed = asyncio.run(H.repair_sentence_lang(sents, ctx))
        finally:
            H.chat_once = orig
        return fixed == 1 and sents[0]["lang"] == "あら？" and sents[0]["display"] == "哎呀？"
    check("台词字段是中文时重译成目标语言，展示文本不变", repair_sentence)

    def repair_failure_keeps_original():
        ctx = H.RoleContext({"text_lang": "ja", "display_lang": "zh"}, {})
        sents = [{"zh": "哎呀？", "lang": "哎呀？", "display": "哎呀？", "emotion": "pingjing"}]
        orig = H.chat_once
        H.chat_once = fake_chat("哎呀？")
        try:
            fixed = asyncio.run(H.repair_sentence_lang(sents, ctx))
        finally:
            H.chat_once = orig
        return fixed == 0 and sents[0]["lang"] == "哎呀？"
    check("重译结果仍不是目标语言时保留原文本", repair_failure_keeps_original)

    def sink_speech_text():
        import main as _M
        sink = _M.SentenceSink("private", 10001, {}, H.RoleContext(cfg_lang(), {}), "")
        orig = H.chat_once
        H.chat_once = fake_chat("あら？")
        try:
            fixed = asyncio.run(sink._speech_text({"zh": "哎呀？", "lang": "哎呀？"}))
            untouched = asyncio.run(sink._speech_text({"zh": "啊", "lang": "あら"}))
        finally:
            H.chat_once = orig
        return fixed == "あら？" and untouched == "あら"
    check("流式合成前同样会修台词语言，已是目标语言时不动",
          sink_speech_text)

    spoken = []

    class FakeClient:
        async def send_private_msg(self, user_id=None, message=None):
            return {}

    class MM:
        data_path = TMP / "speech"

        @staticmethod
        def cleanup_voice_cache(n=20):
            return None

    MM.data_path.mkdir(parents=True, exist_ok=True)
    cfg = Cfg({"text_lang": "ja", "display_lang": "zh", "default_voice": "pingjing",
               "proactive_voice": True, "tts_duration_guard": False})

    async def fake_synth(config, text, emotion, emotions, data_path, stats=None,
                         mimic="", mimics=None):
        spoken.append(text)
        return make_wav(data_path / f"v_{len(spoken)}.wav", 1.0)

    async def fake_tts_ok(config):
        return True

    orig = (SM.synthesize_sentence, SM.check_tts_service, H.chat_once)
    SM.synthesize_sentence = fake_synth
    SM.check_tts_service = fake_tts_ok
    H.chat_once = fake_chat("あなた、そんなに見つめないでよ～")
    try:
        snd = SM.MessageSender(cfg, MM())
        snd.client = FakeClient()
        asyncio.run(snd.speak_and_send("private", 10001,
                                       "主人，别一直盯着本座看啦～",
                                       {"pingjing": {}}, H.RoleContext(cfg, {}),
                                       use_voice=True))
    finally:
        SM.synthesize_sentence, SM.check_tts_service, H.chat_once = orig

    check("主动消息语音按角色语言合成（不再念成展示语言）",
          lambda: spoken and spoken[0] == "あなた、そんなに見つめないでよ～")

    spoken.clear()
    orig = (SM.synthesize_sentence, SM.check_tts_service, H.chat_once)
    SM.synthesize_sentence = fake_synth
    SM.check_tts_service = fake_tts_ok
    H.chat_once = fake_chat("嗯？")
    try:
        snd = SM.MessageSender(cfg, MM())
        snd.client = FakeClient()
        asyncio.run(snd.speak_and_send("private", 10001, "主人，别一直盯着本座看啦～",
                                       {"pingjing": {}}, H.RoleContext(cfg, {}),
                                       use_voice=True))
    finally:
        SM.synthesize_sentence, SM.check_tts_service, H.chat_once = orig
    check("重译失败时退回原文（由合成侧按文字语言念，至少能听懂）",
          lambda: spoken and spoken[0] == "主人，别一直盯着本座看啦～")

    spoken.clear()
    cfg_cn = Cfg({"text_lang": "zh", "display_lang": "zh", "default_voice": "pingjing"})
    orig = (SM.synthesize_sentence, SM.check_tts_service, H.chat_once)
    SM.synthesize_sentence = fake_synth
    SM.check_tts_service = fake_tts_ok
    H.chat_once = fake_chat("不应被调用")
    try:
        snd = SM.MessageSender(cfg_cn, MM())
        snd.client = FakeClient()
        asyncio.run(snd.speak_and_send("private", 10001, "主人，别一直盯着本座看啦～",
                                       {"pingjing": {}}, H.RoleContext(cfg_cn, {}),
                                       use_voice=True))
    finally:
        SM.synthesize_sentence, SM.check_tts_service, H.chat_once = orig
    check("展示语言与台词语言一致时不做多余翻译",
          lambda: spoken and spoken[0] == "主人，别一直盯着本座看啦～")


def n_tls_and_update_fallback():
    import ssl
    import httpx
    import main as M
    from modules import tls as TLS
    from modules import updater

    section("N HTTPS 证书：系统证书库 + certifi 合并校验，失败时明确降级")

    ctx = TLS.verified_context()
    check("校验上下文仍然校验证书与主机名",
          lambda: ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True)
    check("信任列表同时包含系统证书库与 certifi 证书包",
          lambda: len(ctx.get_ca_certs()) >= 100)
    check("能识别证书校验失败（决定是否降级重试）",
          lambda: TLS.is_cert_error(httpx.ConnectError(
              "ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
              "unable to get local issuer certificate (_ssl.c:1032)"))
          and not TLS.is_cert_error(httpx.ConnectError("Connection refused")))
    check("不校验证书的上下文只用于显式降级",
          lambda: TLS.unverified_context().verify_mode == ssl.CERT_NONE)

    clients = 0
    verified = 0
    for name in ("main.py", "modules/tools.py", "modules/rag.py", "modules/tts.py",
                 "modules/tts_service.py", "modules/llm_helpers.py"):
        src = (ROOT / name).read_text(encoding="utf-8")
        clients += src.count("httpx.AsyncClient(")
        verified += (src.count("verify=verified_context()")
                     + src.count("verify=unverified_context()")
                     + src.count("verify=ctx"))
    check("所有 httpx 客户端都带上了证书上下文（不再只信任 certifi）",
          lambda: clients > 0 and verified == clients)

    cache_file = TMP / "tls_check" / "update_check.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.unlink(missing_ok=True)

    attempts = []

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return [{"tag_name": "9.9.9.9", "html_url": "https://github.com/x/y",
                     "draft": False, "prerelease": False}]

    class FakeClient:
        def __init__(self, *a, **kw):
            self.verify = kw.get("verify")
            attempts.append(self.verify)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            if len(attempts) == 1:
                raise httpx.ConnectError(
                    "ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                    "unable to get local issuer certificate (_ssl.c:1032)")
            return FakeResp()

    class Probe:
        _update_cache = M.WebUIServer._update_cache
        _update_save = M.WebUIServer._update_save
        _github_mirrors = M.WebUIServer._github_mirrors
        _github_fetch = M.WebUIServer._github_fetch
        _fetch_releases = M.WebUIServer._fetch_releases
        _update_check_payload = M.WebUIServer._update_check_payload

        def __init__(self, cfg):
            self.config = cfg
            self._update_state_file = cache_file

    orig_client, orig_version = M.httpx.AsyncClient, updater.APP_VERSION
    M.httpx.AsyncClient = FakeClient
    updater.APP_VERSION = "1.0.0.0"
    try:
        payload = asyncio.run(Probe(Cfg({"update_check_enabled": True,
                                          "update_include_prerelease": False,
                                          "update_check_interval_hours": 24}))._update_check_payload())
    finally:
        M.httpx.AsyncClient = orig_client
        updater.APP_VERSION = orig_version

    check("证书校验失败时先按校验上下文重试，再降级并标注结果未验证",
          lambda: len(attempts) == 2
          and getattr(attempts[0], "verify_mode", None) == ssl.CERT_REQUIRED
          and getattr(attempts[1], "verify_mode", None) == ssl.CERT_NONE
          and payload.get("insecure") is True)
    check("降级后仍能算出更新结果", lambda: payload.get("has_update") is True
          and payload.get("latest") == "9.9.9.9")
    check("WebUI 会在未校验证书时给出提示",
          lambda: "本次结果未经验证" in
          (ROOT / "webui" / "start.html").read_text(encoding="utf-8"))


def o_vision_base_url():
    """识图模型可独立配置接口地址；留空则跟随 LLM 服务地址。"""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import main as M
    from modules.llm_helpers import RoleContext, get_image_reply

    section("O 识图模型独立接口地址")

    d = M.ConfigLoader.default_config()
    check("默认留空（不填就跟随 LLM 服务地址）", lambda: d.get("image_caption_base_url") == "")
    html = (ROOT / "webui" / "start.html").read_text(encoding="utf-8")
    check("WebUI 已登记该配置项",
          lambda: "image_caption_base_url" in html and "识图模型服务地址" in html)

    png = TMP / "vision.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n000000")
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            ln = int(self.headers.get("Content-Length", 0) or 0)
            self.rfile.read(ln)
            seen.append(self.path)
            body = json.dumps({"choices": [{"message": {
                "content": '{"sentences": [{"zh": "看到图了", "ja": "見えた",'
                           ' "emotion": "pingjing"}]}'}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    vision_url = f"http://127.0.0.1:{srv.server_address[1]}"
    base = {"llm_backend": "openai", "llm_model_name": "fake",
            "image_caption_model_name": "fake-vision", "image_caption_timeout": 30,
            "text_lang": "ja", "display_lang": "zh", "default_voice": "pingjing",
            "personality_prompt": "x", "json_prompt": "", "supplement_prompt": "",
            "history_length": 8, "llm_base_url": "http://127.0.0.1:1"}
    try:
        # 单独指向识图服务：LLM 地址不可达也能识图
        seen.clear()
        ctx = RoleContext(Cfg({**base, "image_caption_base_url": vision_url}))
        r = asyncio.run(get_image_reply(ctx, "这是什么", [], {"pingjing": {}}, [str(png)]))
        check("配了独立地址时用该地址识图",
              lambda: r and r["sentences"][0]["zh"] == "看到图了"
              and seen and seen[0].endswith("/chat/completions"))

        # 留空：跟随 llm_base_url
        seen.clear()
        ctx2 = RoleContext(Cfg({**base, "image_caption_base_url": "",
                                "llm_base_url": vision_url}))
        r2 = asyncio.run(get_image_reply(ctx2, "这是什么", [], {"pingjing": {}}, [str(png)]))
        check("留空时回落到 LLM 服务地址识图",
              lambda: r2 and r2["sentences"][0]["zh"] == "看到图了" and bool(seen))
    finally:
        srv.shutdown()


def p_cross_site_guard():
    """跨站表单不能再带着浏览器 Cookie 直接打 WebUI 的写接口。"""
    import main as M

    section("P 跨站请求防护")

    class Req:
        def __init__(self, method, origin=None, referer=None, host="127.0.0.1:11500"):
            self.method = method
            self.headers = {}
            if origin is not None:
                self.headers["Origin"] = origin
            if referer is not None:
                self.headers["Referer"] = referer
            self.headers["Host"] = host

    check("同源 POST 放行",
          lambda: M._same_origin(Req("POST", origin="http://127.0.0.1:11500")))
    check("跨站 POST 拒绝",
          lambda: not M._same_origin(Req("POST", origin="http://evil.example.com")))
    check("Referer 跨站同样拒绝",
          lambda: not M._same_origin(
              Req("POST", referer="http://evil.example.com/form.html")))
    check("无 Origin 的非浏览器客户端放行（命令行/测试）",
          lambda: M._same_origin(Req("POST")))
    check("读请求不受影响",
          lambda: M._same_origin(Req("GET", origin="http://evil.example.com")))
    check("写接口已挂上该中间件",
          lambda: "_same_origin(request)" in
          # 认证中间件（含 _same_origin 调用点）已拆到 modules/webui_common.py
          (ROOT / "modules" / "webui_common.py").read_text(encoding="utf-8"))


def q_export_scope():
    """导出/导入只处理会话记忆，不能把 webui_auth.json 等数据卷进来。"""
    import main as M

    section("Q 记忆导出/导入范围")

    mem = M.MemoryManager(M.ConfigLoader())
    path = mem.data_path
    created = []
    for name in ("murasame_private_1.json", "webui_auth.json", "user_profiles.json"):
        f = path / name
        if not f.exists():
            f.write_text("{}", encoding="utf-8")
            created.append(f)
    try:
        check("会话记忆文件名被识别", lambda: M._is_memory_filename("murasame_private_1.json")
              and M._is_memory_filename("murasame_group_456.json"))
        check("功能数据文件名被排除",
              lambda: not M._is_memory_filename("webui_auth.json")
              and not M._is_memory_filename("user_profiles.json")
              and not M._is_memory_filename("../config.json"))
        check("删除接口同样只认会话记忆",
              lambda: mem.delete_memory_file("webui_auth.json") is False)
    finally:
        for f in created:
            f.unlink(missing_ok=True)


def r_config_load_guard():
    """单个字段异常不能把整份 config.json 判成损坏并覆写掉。"""
    import main as M

    section("R 配置读取容错")

    tmp = TMP / "cfg_guard"
    tmp.mkdir(parents=True, exist_ok=True)
    for old in tmp.iterdir():
        if old.is_file():
            old.unlink()

    path = tmp / "config.json"
    path.write_text(json.dumps({"ref_audio_root": None,
                                "napcat_ws_url": "ws://kept-here",
                                "webui_port": 11666}, ensure_ascii=False),
                    encoding="utf-8")
    loader = M.ConfigLoader(str(path))
    check("字段为 null 不再触发「配置损坏」分支",
          lambda: path.exists() and not (tmp / "config.json.corrupt").exists())
    check("原设置全部保留（只补默认值，不重置）",
          lambda: loader.get("napcat_ws_url") == "ws://kept-here"
          and loader.get("webui_port") == 11666)

    bad = tmp / "broken.json"
    bad.write_text("{ 这不是 JSON", encoding="utf-8")
    M.ConfigLoader(str(bad))
    check("内容真的损坏时才备份为 .corrupt",
          lambda: (tmp / "broken.json.corrupt").exists())

    enc_path = tmp / "enc.json"
    enc_path.write_text(json.dumps({"llm_api_key": M._encrypt_value("sk-test"),
                                    "llm_base_url": "http://127.0.0.1:11434"},
                                   ensure_ascii=False), encoding="utf-8")
    enc_loader = M.ConfigLoader(str(enc_path))
    check("密文能正常解密回明文", lambda: enc_loader.get("llm_api_key") == "sk-test")
    check("解密失败时不返回空串（保留原密文）",
          lambda: M._decrypt_value("enc2:bm90LWEtYmxvYg==") is None)


def s_model_list_endpoint():
    """服务地址补全：粘完整接口地址不再拼出永远 4xx 的 URL，报错要说清原因。"""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import main as M
    from modules.llm_helpers import (chat_endpoint, model_list_endpoints,
                                     looks_like_full_endpoint)

    section("S 模型列表地址补全与报错可读性")

    derived = {
        # 已有配置行为的回归：必须与改动前完全一致
        "http://127.0.0.1:11434": ("http://127.0.0.1:11434/api/chat",
                                   ["http://127.0.0.1:11434/api/tags"]),
        "http://127.0.0.1:8080/v1": ("http://127.0.0.1:8080/v1/chat/completions",
                                     ["http://127.0.0.1:8080/v1/models"]),
        "https://api.deepseek.com": ("https://api.deepseek.com/chat/completions",
                                     ["https://api.deepseek.com/v1/models",
                                      "https://api.deepseek.com/models"]),
    }
    for base, (want_chat, want_models) in derived.items():
        backend = "ollama" if "11434" in base else "openai"
        check(f"地址补全保持原行为：{base}",
              lambda b=base, bk=backend, c=want_chat, m=want_models:
              chat_endpoint(b, bk) == c and model_list_endpoints(b, bk) == m)

    check("已带端点后缀的地址原样使用（不再重复拼接）",
          lambda: chat_endpoint("http://127.0.0.1:8080/v1/chat/completions") ==
          "http://127.0.0.1:8080/v1/chat/completions"
          and model_list_endpoints("http://127.0.0.1:8080/v1/models") ==
          ["http://127.0.0.1:8080/v1/models"])
    check("从对话端点能反推出模型列表端点",
          lambda: model_list_endpoints("http://127.0.0.1:8080/v1/chat/completions") ==
          ["http://127.0.0.1:8080/v1/models"])
    check("带 /v1 的兼容模式地址正确补全",
          lambda: model_list_endpoints(
              "https://dashscope.aliyuncs.com/compatible-mode/v1") ==
          ["https://dashscope.aliyuncs.com/compatible-mode/v1/models"])
    check("能识别「填成了具体接口的完整路径」",
          lambda: looks_like_full_endpoint(
              "https://dashscope.aliyuncs.com/api/v1/services/embeddings/"
              "multimodal-embedding/multimodal-embedding")
          and not looks_like_full_endpoint("https://dashscope.aliyuncs.com/compatible-mode/v1")
          and not looks_like_full_endpoint("http://127.0.0.1:8080/v1")
          and not looks_like_full_endpoint("https://api.deepseek.com"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            route = routes.get(self.path)
            if route is None:
                body = json.dumps({"error": {"message": "no such path"}}).encode()
                self.send_response(404)
            else:
                body = route.encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    routes = {}
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    root = f"http://127.0.0.1:{srv.server_address[1]}"

    class Req:
        def __init__(self, payload):
            self._payload = payload

        async def json(self):
            return self._payload

    class Probe:
        handle_list_remote_models = M.WebUIServer.handle_list_remote_models

        def __init__(self, cfg):
            self.config = Cfg(cfg)

    def call(base, backend="openai"):
        resp = asyncio.run(Probe({"llm_api_key": ""}).handle_list_remote_models(
            Req({"base_url": base, "backend": backend})))
        return resp.status, json.loads(resp.body.decode("utf-8"))

    try:
        routes.clear()
        routes["/v1/models"] = json.dumps({"data": [{"id": "qwen-vl-max"}]})
        status, data = call(root)
        check("服务根地址直接命中 /v1/models",
              lambda: status == 200 and data.get("models") == ["qwen-vl-max"])

        # 只提供不带版本段的 /models：第一个候选 404，靠候选回退拿到列表
        routes.clear()
        routes["/models"] = json.dumps({"data": [{"id": "deepseek-chat"}]})
        status, data = call(root)
        check("第一个候选失败时自动回落下一个候选",
              lambda: status == 200 and data.get("models") == ["deepseek-chat"])

        routes.clear()
        status, data = call(root + "/v1/services/embeddings/multimodal-embedding/multimodal-embedding")
        text = str(data.get("error", ""))
        check("全部候选失败时报错列出实际请求过的地址",
              lambda: status != 200 and "/v1/models" in text and "/models" in text)
        check("报错带上上游返回的原因（不再只有裸的 4xx）",
              lambda: "404" in text and "no such path" in text)
        check("地址形状不对时给出「要填服务根地址」的提示",
              lambda: "服务根地址" in text)

        routes.clear()
        status, data = call(root + "/v999")
        root_text = str(data.get("error", ""))
        check("正常形状的服务根失败时不给这条提示",
              lambda: status != 200 and "服务根地址" not in root_text)
    finally:
        srv.shutdown()


def t_sticker_capture_reasons():
    """收藏：所有跳过都要有原因，且不再依赖"第二次下载原图"。"""
    import io as _io
    import shutil
    import contextlib

    import main as M
    import modules.stickers as ST
    from modules.llm_helpers import RoleContext

    section("T 表情收藏落盘可诊断 + 复用识图已读入的图片")

    root = TMP / "capture_reasons"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    from PIL import Image
    src_png = root / "src.png"
    Image.new("RGB", (160, 160), (90, 160, 220)).save(src_png)
    png_bytes = src_png.read_bytes()

    def cfg(**over):
        base = {"stickers_dir": str(root), "stickers_enabled": True,
                "sticker_capture_enabled": True,
                "sticker_capture_min_interval": 0, "sticker_capture_max_per_day": 100,
                "sticker_probability": 1.0}
        base.update(over)
        return Cfg(base)

    def run_capture(c, url="", category="weixie", data=None):
        """跑一次收藏并把打印内容抓下来。"""
        buf = _io.StringIO()
        mgr = ST.StickerManager(c)
        with contextlib.redirect_stdout(buf):
            ok = asyncio.run(ST.auto_capture_image(c, mgr, url, category, image_data=data))
        return ok, buf.getvalue(), mgr

    # 落盘：间隔未到时必须说明还差多久（原来完全静默）
    ST._capture_state.update({"date": time.strftime("%Y-%m-%d"), "count": 0,
                              "last": time.time()})
    c_slow = cfg(sticker_capture_min_interval=600)
    ok, out, _m = run_capture(c_slow, str(src_png))
    check("最小间隔未到时明确写出还需等待多少秒",
          lambda: ok is False and "最小间隔" in out and "还需" in out
          and "sticker_capture_min_interval=600" in out)
    check("提示里带上了可调项名，用户知道改哪个开关",
          lambda: "设为 0 可关闭该限制" in out)

    ST._capture_state.update({"date": time.strftime("%Y-%m-%d"), "count": 100,
                              "last": 0.0})
    ok, out, _m = run_capture(c_slow, str(src_png))
    check("当日上限拦下时写出计数与上限",
          lambda: ok is False and "今日收藏已达上限" in out and "100/100" in out)

    ST._capture_state.update({"date": time.strftime("%Y-%m-%d"), "count": 0,
                              "last": 0.0})
    ok, out, _m = run_capture(cfg(sticker_capture_enabled=False), str(src_png))
    check("收藏总开关关闭时说明是哪个开关",
          lambda: ok is False and "sticker_capture_enabled" in out)
    ok, out, _m = run_capture(cfg(stickers_enabled=False), str(src_png))
    check("表情包功能未开时说明是哪个开关",
          lambda: ok is False and "stickers_enabled" in out)

    # 复用识图已经读进来的字节：地址本身取不到图也照样收藏成功
    ST._capture_state.update({"date": "", "count": 0, "last": 0.0})
    ok, out, mgr = run_capture(cfg(), "http://127.0.0.1:9/expired.jpg",
                               "weixie", data=png_bytes)
    saved = list((root / "weixie").glob("auto_*")) if (root / "weixie").exists() else []
    check("传入已读入的图片字节时，不再依赖下载（地址已失效也能收藏）",
          lambda: ok is True and len(saved) == 1)
    check("收藏后立刻进入可抽取池", lambda: bool(mgr.map.get("weixie")))

    # 没有字节时仍然按老路走下载，失败要说明
    ST._capture_state.update({"date": "", "count": 0, "last": 0.0})
    ok, out, _m = run_capture(cfg(), "http://127.0.0.1:9/expired.jpg", "weixie")
    check("没有可用字节、下载又失败时明确报出失败",
          lambda: ok is False and "读取图片失败" in out)

    # auto_capture_from_images 优先用 image_result 里的图片字节
    called = {}

    async def fake_capture(c, mgr, source, category="", image_data=None, reason=""):
        called["data"] = image_data
        called["source"] = source
        return True
    orig = ST.auto_capture_image
    ST.auto_capture_image = fake_capture
    try:
        ctx = RoleContext({"sticker_capture_enabled": True,
                           "sticker_capture_min_score": 0.7}, {})
        asyncio.run(M.auto_capture_from_images(
            ctx, {"should": True, "score": 0.9, "category": "weixie", "reason": "x"},
            ["/not/exist/pic.png"],
            {"description": "d", "sentences": [{"zh": "嗯"}],
             "capture_image": {"source": "/not/exist/pic.png", "data": png_bytes}}))
        check("收藏入口把识图读到的字节继续传下去", lambda: called.get("data") == png_bytes)
    finally:
        ST.auto_capture_image = orig
        # 节流状态是模块级全局：本段改过就复原，别影响后面的用例
        ST._capture_state.update({"date": "", "count": 0, "last": 0.0})


def u_repeat_guard_switches():
    """防复读拆成「什么时候查」×「跟谁比」四个独立开关。"""
    import main as M
    from modules.llm_helpers import RoleContext

    section("U 防复读拆成四个独立开关")

    d = M.ConfigLoader.default_config()
    keys = ["repeat_guard_streaming_check", "repeat_guard_regen_check",
            "repeat_guard_compare_self", "repeat_guard_compare_user"]
    check("四个开关都在默认配置里且默认开启",
          lambda: all(d.get(k) is True for k in keys))
    html = (ROOT / "webui" / "start.html").read_text(encoding="utf-8")
    check("WebUI 已登记这四个开关（含独立分组）",
          lambda: all(f"'{k}'" in html for k in keys)
          and "{ title: '防复读'" in html)

    check("flags 全开时摘要为空",
          lambda: M.repeat_guard_summary(M.repeat_guard_flags(Cfg(d))) == "")
    check("关掉的开关会列进摘要",
          lambda: "整段生成后校验" in M.repeat_guard_summary(
              M.repeat_guard_flags(Cfg({**d, "repeat_guard_regen_check": False}))))
    def active_of(**over):
        return M.repeat_guard_active(M.repeat_guard_flags(Cfg({**d, **over})))
    check("比对对象全关时判定为「整套失效」",
          lambda: active_of(repeat_guard_compare_self=False,
                            repeat_guard_compare_user=False) is False
          and active_of(repeat_guard_compare_self=False) is True)

    class Sink(M.SentenceSink):
        async def _run(self):
            return

    hist_repeat = "本座今天心情可好了，主人要不要陪我玩一会儿呀？"
    user_said = "我想问问你明天有没有空陪我去看电影"

    def make_sink(cfg, user_text=""):
        sink = Sink("private", 1, {}, RoleContext(cfg, {}),
                    last_reply=[hist_repeat], user_text=user_text)
        return sink

    both = Sink("private", 1, {}, RoleContext(Cfg(d), {}),
                last_reply=[hist_repeat], user_text=user_said)
    check("默认（全开）时历史复读会被识别",
          lambda: both._looks_repeat({"zh": hist_repeat}) is True)
    check("默认（全开）时复述用户原话会被识别",
          lambda: both._looks_repeat({"zh": user_said}) is True)

    only_user = make_sink(Cfg({**d, "repeat_guard_compare_self": False}),
                          user_text=user_said)
    check("关掉「比对角色历史」后不再因历史复读命中",
          lambda: only_user._looks_repeat({"zh": hist_repeat}) is False)
    check("关掉「比对角色历史」后仍会拦复述用户",
          lambda: only_user._looks_repeat({"zh": user_said}) is True)

    only_self = make_sink(Cfg({**d, "repeat_guard_compare_user": False}),
                          user_text=user_said)
    check("关掉「比对用户本条」后不再拦复述用户",
          lambda: only_self._looks_repeat({"zh": user_said}) is False)
    check("关掉「比对用户本条」后仍会拦历史复读",
          lambda: only_self._looks_repeat({"zh": hist_repeat}) is True)

    # 流式首句检查关掉后，首句不再被拦
    def stream_blocked(cfg):
        sink = make_sink(cfg)
        asyncio.run(sink.on_sentence({"zh": hist_repeat, "display": hist_repeat}))
        return sink.blocked
    check("流式首句检查开启时首句被拦下", lambda: stream_blocked(Cfg(d)) is True)
    check("流式首句检查关闭时首句直接放行",
          lambda: stream_blocked(Cfg({**d, "repeat_guard_streaming_check": False})) is False)
    check("比对对象全关时流式首句也不再拦",
          lambda: stream_blocked(Cfg({**d, "repeat_guard_compare_self": False,
                                      "repeat_guard_compare_user": False})) is False)


def v_voice_pacing():
    """语音节奏：只在发下一条前等上一条播完，收尾不再占着会话锁。"""
    import shutil
    import wave as wavemod

    import main as M
    import modules.sender as SM
    import modules.reply_pipeline as RP
    from modules.llm_helpers import RoleContext

    section("V 语音发送节奏（不再拖住排队的下一条消息）")

    data_dir = TMP / "pacing"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)

    def make_wav(name, seconds):
        path = data_dir / name
        with wavemod.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * int(16000 * seconds))
        return path

    short_wav = make_wav("short.wav", 1.0)
    long_wav = make_wav("long.wav", 3.0)

    # --- 节奏器本身 ---
    pacer = SM.VoicePacer(True)
    check("节奏器初始不等", lambda: asyncio.run(_timed(pacer.wait)) < 0.05)

    async def wait_twice():
        start = time.monotonic()
        await pacer.wait()          # 没有待播语音 → 立刻返回
        await pacer.hold_voice(short_wav)   # 登记 1.0s + 0.5s 间隔
        await pacer.wait()          # 应该等到期
        return time.monotonic() - start
    elapsed = asyncio.run(wait_twice())
    check("发下一条前会等上一条播完（1.0s 语音 → 约 1.5s）",
          lambda: 1.4 <= elapsed < 2.2)
    check("等到期后再调用不再重复等待",
          lambda: asyncio.run(_timed(pacer.wait)) < 0.05)

    pacer2 = SM.VoicePacer(True)

    async def wait_two_voices():
        start = time.monotonic()
        await pacer2.hold_voice(short_wav)   # 1.0s
        await pacer2.wait()
        await pacer2.hold_voice(long_wav)    # 3.0s
        await pacer2.wait()
        return time.monotonic() - start
    elapsed2 = asyncio.run(wait_two_voices())
    check("等待时长取自刚登记的那条语音（1.0s 后接 3.0s）",
          lambda: 4.3 <= elapsed2 < 5.4)
    check("关闭动态等待时用固定间隔",
          lambda: asyncio.run(SM.VoicePacer(False).hold_voice(long_wav))
          == SM._VOICE_GAP_FIXED)

    # --- 真实 send_reply：合并发送（默认路径）不再有尾部等待 ---
    class Client:
        def __init__(self):
            self.voice_at = []

        async def send_private_msg(self, user_id=None, message=None):
            if any(type(seg).__name__ == "Record" for seg in message):
                self.voice_at.append(time.monotonic())
            return {}

    class Mem:
        data_path = data_dir

        @staticmethod
        def cleanup_voice_cache(n=20):
            return None

    synth_queue = []
    synth_seq = []

    async def fake_synth(config, text, emotion, emotions, dpath, stats=None,
                         mimic="", mimics=None):
        path = synth_queue.pop(0) if synth_queue else short_wav
        # 序号必须自增：用时间戳命名时连续两次合成可能算出同一个文件名，
        # 后一次会把前一次覆盖掉，于是"第一条 wav 被删 → 第二条判为不存在"
        synth_seq.append(1)
        out = dpath / f"synth_{len(synth_seq)}.wav"
        out.write_bytes(path.read_bytes())
        return out

    orig_synth, orig_check = SM.synthesize_sentence, SM.check_tts_service

    async def ok_tts(config):
        return True
    SM.synthesize_sentence = fake_synth
    SM.check_tts_service = ok_tts
    try:
        cfg = Cfg({"dynamic_sleep": True, "voice_transition": False, "default_voice": "pingjing",
                   "separate_send": False, "send_voice_separately": False})
        snd = SM.MessageSender(cfg, Mem())
        snd.client = Client()
        sentences = [{"zh": "第一句", "lang": "第一句", "display": "第一句", "emotion": "pingjing"},
                     {"zh": "第二句", "lang": "第二句", "display": "第二句", "emotion": "pingjing"}]
        synth_queue[:] = [long_wav, long_wav]
        start = time.monotonic()
        asyncio.run(snd.send_reply("private", 10001, sentences, {"pingjing": {}},
                                   RoleContext(cfg)))
        combined_ms = time.monotonic() - start
        check("合并发送：发完语音立刻返回（不再等 3s 播放）",
              lambda: combined_ms < 2.0 and len(snd.client.voice_at) == 1)

        # --- 真实 send_reply：分开发送，句间保留节奏、收尾不等 ---
        cfg2 = Cfg({"dynamic_sleep": True, "voice_transition": False, "default_voice": "pingjing",
                    "separate_send": True, "send_voice_separately": True,
                    "separate_force_segment": False, "sticker_every_sentence": False})
        snd2 = SM.MessageSender(cfg2, Mem())
        snd2.client = Client()
        synth_queue[:] = [short_wav, long_wav]
        start = time.monotonic()
        asyncio.run(snd2.send_reply("private", 10001, sentences, {"pingjing": {}},
                                    RoleContext(cfg2)))
        split_ms = time.monotonic() - start
        stamps = snd2.client.voice_at
        check("分开发送：两条语音都发出去了", lambda: len(stamps) == 2)
        check("分开发送：第二条等的是第一条自己的时长（约 1.5s）",
              lambda: 1.4 <= (stamps[1] - stamps[0]) < 2.3)
        check("分开发送：最后一条之后没有额外等待（总耗时远小于 1.5+3.5）",
              lambda: split_ms < 3.0)

        # --- 流式逐句发送：同样不在最后一句后等待 ---
        originals = {"voice": []}

        class FakeSender:
            def new_message_group(self):
                return 1

            async def send_text(self, st, tid, text, sticker=None, reply_id=None,
                                at_ids=None, group=None):
                return True

            async def send_voice(self, st, tid, wav, group=None):
                originals["voice"].append(time.monotonic())
                return True

        saved = {"sender": M.sender, "synth": M.synthesize_sentence,
                 "mem": M.memory_manager, "cfg": M.global_config}
        RP.synthesize_sentence = fake_synth
        M.global_config = cfg2
        M.app_context.global_config = M.global_config
        M.sender = FakeSender()
        M.app_context.sender = M.sender
        M.synthesize_sentence = fake_synth
        M.memory_manager = Mem
        M.app_context.memory_manager = M.memory_manager
        try:
            sink = M.SentenceSink("private", 10001, {"pingjing": {}}, RoleContext(cfg2))
            sink._tts_ok = True
            synth_queue[:] = [short_wav, long_wav]
            start = time.monotonic()
            asyncio.run(sink._send_one({"zh": "第一句", "display": "第一句",
                                        "lang": "第一句", "emotion": "pingjing"}))
            first_ms = time.monotonic() - start
            after_first = len(originals["voice"])
            asyncio.run(sink._send_one({"zh": "第二句", "display": "第二句",
                                        "lang": "第二句", "emotion": "pingjing"}))
            total_ms = time.monotonic() - start
            check("流式：第一句发完立刻返回（不等自己的播放时长）",
                  lambda: first_ms < 1.0 and after_first == 1 and len(originals["voice"]) == 2)
            check("流式：第二句等的是第一句自己的时长（约 1.5s）",
                  lambda: 1.4 <= (originals["voice"][1] - originals["voice"][0]) < 2.3)
            check("流式：最后一句之后没有额外等待", lambda: total_ms < 3.0)
        finally:
            M.sender = saved["sender"]
            M.app_context.sender = M.sender
            M.synthesize_sentence = saved["synth"]
            RP.synthesize_sentence = saved["synth"]
            M.memory_manager = saved["mem"]
            M.app_context.memory_manager = M.memory_manager
            M.global_config = saved["cfg"]
            M.app_context.global_config = M.global_config
    finally:
        SM.synthesize_sentence = orig_synth
        SM.check_tts_service = orig_check


async def _timed(coro_fn):
    start = time.monotonic()
    await coro_fn()
    return time.monotonic() - start


def w_tts_runaway_tail():
    """台词拖音 / 合成失控：文本侧压缩拖音标记，音频侧丢弃异常偏长的结果。"""
    import wave as wavemod

    from modules import tts as T

    section("W TTS 拖音失控防护")

    check("长串破折号被压到 2 个（原来会变成十几个 ～ 送进合成）",
          lambda: T._sanitize_tts_text("そうですね————————————本当に長いですね")
          == "そうですね～～本当に長いですね")
    check("长串片假名长音符被压到 2 个",
          lambda: T._sanitize_tts_text("やめてーーーーーーーーー！") == "やめてーー！")
    check("正常单词里的单个长音符不受影响",
          lambda: T._sanitize_tts_text("コーヒーを飲みます") == "コーヒーを飲みます")
    check("两个波浪线保持原样（本来就是正常拖音）",
          lambda: T._sanitize_tts_text("はい～～") == "はい～～")

    line = "ふん、体格はまあまあですけど、そんなに簡単に色誘に引っかかるわけないじゃないですか！"
    limit = T._max_expected_seconds(line, {})
    check("39 字台词的上限约 23 秒", lambda: 22.0 < limit < 25.0)
    check("短句有绝对下限保护（不会把带停顿的正常短句误判成失控）",
          lambda: T._max_expected_seconds("はい。", {}) >= T._MAX_SECONDS_FLOOR)

    def make_wav(seconds):
        path = TMP / f"runaway_{seconds}_{int(time.time() * 1e6)}.wav"
        with wavemod.open(str(path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * int(16000 * seconds))
        return path

    long_take, ok_take = make_wav(27.0), make_wav(9.0)
    check("27 秒音频对 39 字台词判定为失控", lambda: T._audio_too_long(long_take, line, {}))
    check("9 秒音频判定为正常", lambda: not T._audio_too_long(ok_take, line, {}))
    check("上限设为 0 时关闭该判断",
          lambda: not T._audio_too_long(long_take, line, {"tts_max_seconds_per_char": 0}))
    check("默认配置里带上了这个上限项",
          lambda: T._max_expected_seconds(line, {}) > 0)

    # 端到端：服务端每次都返回 27 秒的失控音频 → 应作废重试，最终只发文本
    emotions = {"pingjing": {"ref_path": "r.mp3", "prompt_text": "p"}}

    class Resp:
        def __init__(self, body):
            self.status_code = 200
            self.content = body

    calls = {"n": 0}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            calls["n"] += 1
            return Resp(long_take.read_bytes())

    orig = T.httpx.AsyncClient
    T.httpx.AsyncClient = FakeClient
    try:
        out = asyncio.run(T.synthesize_sentence(
            Cfg({"default_voice": "pingjing", "text_lang": "ja", "tts_auto_lang": False}),
            line, "pingjing", emotions, TMP))
    finally:
        T.httpx.AsyncClient = orig
    check("失控音频一律作废，不当作成功语音返回",
          lambda: out is None and calls["n"] >= 2)

    # 换一种切分方式后拿到正常音频 → 正常返回
    calls["n"] = 0

    class FakeClient3(FakeClient):
        async def get(self, url, params=None):
            calls["n"] += 1
            first = calls["n"] == 1
            return Resp(long_take.read_bytes() if first else ok_take.read_bytes())

    orig = T.httpx.AsyncClient
    T.httpx.AsyncClient = FakeClient3
    try:
        out2 = asyncio.run(T.synthesize_sentence(
            Cfg({"default_voice": "pingjing", "text_lang": "ja", "tts_auto_lang": False}),
            line, "pingjing", emotions, TMP))
    finally:
        T.httpx.AsyncClient = orig
    check("换切分方式后拿到正常音频则照常使用",
          lambda: out2 is not None and T._wav_duration(out2) < 20.0)


def x_rename_to_lovomo():
    """全项目更名为 Lovomo。"""
    import shutil
    import tempfile

    import main as M

    section("X 更名为 Lovomo")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    check("README 首行是新名称", lambda: readme.splitlines()[0].strip() == "# Lovomo")
    check("README 写明仓库地址",
          lambda: "https://github.com/slpk1ng/Lovomo" in readme)
    check("README 里的安装包名已更新", lambda: "Lovomo_Setup.exe" in readme)

    src = (ROOT / "main.py").read_text(encoding="utf-8")
    # WebUIServer（更新检查/会话 Cookie）已拆到 modules/webui_server.py，一并纳入扫描
    src = src + (ROOT / "modules" / "webui_server.py").read_text(encoding="utf-8")
    # 密钥派生常量已拆到 modules/security.py，一并纳入扫描
    sec_src = (ROOT / "modules" / "security.py").read_text(encoding="utf-8")
    combined = (src + sec_src).encode("utf-8")
    leftovers = [ln.strip() for ln in (src + sec_src).splitlines() if "ltvm" in ln.lower()]
    check("main.py 里没有旧名残留（含密钥派生材料）", lambda: leftovers == [])
    check("密钥派生材料已随程序更名为 Lovomo",
          lambda: b"Lovomo-KEYSTORE-v2" in combined
          and b"Lovomo-KEYSTORE-SALT-v2" in combined)
    check("更新检查指向新仓库", lambda: 'repo = "slpk1ng/Lovomo"' in src)
    check("会话 Cookie 已改名", lambda: "lovomo_auth" in src and "ltvm_auth" not in src)

    # 源码层面不残留旧名。合法例外只有一处：旧数据库文件名 ltvm.db，
    # 迁移逻辑必须按原名去找，改了老用户的库就找不回来。
    source_hits = []
    for path in [ROOT / "main.py", ROOT / "_ui_server.py", ROOT / "webui" / "start.html",
                 *sorted((ROOT / "modules").glob("*.py"))]:
        for ln in path.read_text(encoding="utf-8").splitlines():
            if "ltvm" in ln.lower() and "LEGACY_DB_FILENAMES" not in ln:
                source_hits.append(f"{path.name}: {ln.strip()[:60]}")
    check("源码与 WebUI 里完全没有旧名残留", lambda: source_hits == [])
    check("唯一例外是旧数据库文件名（迁移必须按原名找）",
          lambda: 'LEGACY_DB_FILENAMES = ("ltvm.db",)' in
          (ROOT / "modules" / "database.py").read_text(encoding="utf-8"))

    html = (ROOT / "webui" / "start.html").read_text(encoding="utf-8")
    # 左上角品牌区现在是「圆角标记 + 字标」，名称仍在 .logo-name 里
    check("WebUI 标题与左上角名称已更新",
          lambda: "<title>Lovomo</title>" in html
          and 'class="logo-name">Lovomo</span>' in html)
    check("WebUI 里没有旧名残留",
          lambda: not [ln for ln in html.splitlines() if "ltvm" in ln.lower()])
    check("WebUI 有版本号占位元素", lambda: 'id="logo-version"' in html)
    # 品牌区的下边距（视觉打磨层里定的 22px），决定菜单从多高开始
    check("侧边栏菜单整体下移（标题与按钮之间留出更大间距）",
          lambda: "padding: 2px 6px 0; margin-bottom: 22px;" in html)

    check("安装脚本与打包描述文件已改名",
          lambda: (ROOT / "Lovomo_Setup.iss").exists()
          and (ROOT / "lovomo_app.spec").exists()
          and not (ROOT / "LTVM_Setup.iss").exists()
          and not (ROOT / "ltvm_app.spec").exists())
    iss = (ROOT / "Lovomo_Setup.iss").read_text(encoding="utf-8")
    check("安装脚本指向 Lovomo.exe 与 dist\\Lovomo",
          lambda: "Lovomo.exe" in iss and "dist\\Lovomo\\*" in iss
          and "LTVM" not in iss)
    check("打包描述里的产物名是 Lovomo",
          lambda: "name='Lovomo'" in (ROOT / "lovomo_app.spec").read_text(encoding="utf-8"))

    # 数据库改名 + 旧库自动沿用
    tmp = TMP / "rename_db"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    import sqlite3
    legacy = tmp / "ltvm.db"
    conn = sqlite3.connect(str(legacy))
    conn.execute("CREATE TABLE todos (id INTEGER PRIMARY KEY, content TEXT)")
    conn.execute("INSERT INTO todos (content) VALUES ('旧待办')")
    conn.commit()
    conn.close()
    db = M.DatabaseManager(tmp)
    rows = db.query_all("SELECT content FROM todos")
    check("数据库更名为 lovomo.db", lambda: db.db_path.name == "lovomo.db")
    check("旧数据库自动沿用，历史数据不丢",
          lambda: (tmp / "lovomo.db").exists() and not legacy.exists()
          and [r["content"] for r in rows] == ["旧待办"])
    db.close()


def y_version_in_ui():
    """WebUI 左上角显示版本号：接口在关闭更新检查时也要给出版本。"""
    import json as _json

    import main as M

    section("Y WebUI 版本号")

    class Req:
        headers = {}
        cookies = {}

        async def json(self):
            return {}

    class Probe:
        handle_auth_status = M.WebUIServer.handle_auth_status
        handle_list_remote_models = None

        def __init__(self, cfg):
            self.config = cfg
            self._password = ""
            self._auth_token = None
            self._second_password = ""

        def _auth_remember(self):
            return 0.0

    resp = asyncio.run(Probe(Cfg({})).handle_auth_status(Req()))
    payload = _json.loads(resp.body.decode("utf-8"))
    from modules.updater import APP_VERSION
    check("无密码时 auth/status 也返回版本号",
          lambda: payload.get("version") == APP_VERSION)
    # 二级密码是独立开关：没设访问密码时它照样要能被前端看见
    check("auth/status 一并报出二级密码是否启用",
          lambda: payload.get("second_enabled") is False)

    class Probe2(Probe):
        def __init__(self, cfg):
            super().__init__(cfg)
            self._password = "pw"
            self._auth_token = "tok"
            self._second_password = "pw2"
    resp2 = asyncio.run(Probe2(Cfg({})).handle_auth_status(Req()))
    payload2 = _json.loads(resp2.body.decode("utf-8"))
    check("需要登录时同样带版本号",
          lambda: payload2.get("version") == APP_VERSION
          and payload2.get("authed") is False
          and payload2.get("second_enabled") is True)

    # 密码比较走定时安全比较：==/!= 会按相同前缀的长度泄露时间差
    check("密码比较用定时安全比较，且中文密码也能比",
          lambda: M._password_matches("密码123", "密码123")
          and not M._password_matches("密码12", "密码123")
          and not M._password_matches("", "密码123")
          and M._password_matches("", ""))
    src = __import__("inspect").getsource(M.WebUIServer.handle_auth_login)
    src2 = __import__("inspect").getsource(M.WebUIServer.handle_auth_second)
    check("登录与二级密码校验都改用了它",
          lambda: "_password_matches" in src and "_password_matches" in src2
          and "== self._password" not in src
          and "!= self._second_password" not in src2)


def z_role_napcat_connections():
    section("Z 多角色：每个角色可用独立 NapCat 连接")

    from modules.sender import MessageSender
    from main import (build_connection_profiles, role_connection_snapshot,
                      resolve_target_roles)
    import asyncio as _a

    # 主循环必须真的组装连接清单并启动连接任务（防止探针清理误删真实代码）
    import main as _m2
    src_main = Path(_m2.__file__).read_text(encoding="utf-8")
    for need in ("profiles = build_connection_profiles(global_config)",
                 "async def _run_profile(profile: dict):",
                 "await asyncio.gather(*tasks)",
                 "sender.set_role_client(role_key, client)"):
        if need not in src_main:
            print("      main.py 缺少:", need)
            return False

    # 主循环必须真的组装连接清单并启动连接任务（防止探针清理误删真实代码）
    import main as _m2
    src_main = Path(_m2.__file__).read_text(encoding="utf-8")
    for need in ("profiles = build_connection_profiles(global_config)",
                 "async def _run_profile(profile: dict):",
                 "await asyncio.gather(*tasks)",
                 "sender.set_role_client(role_key, client)"):
        if need not in src_main:
            print("      main.py 缺少:", need)
            return False

    def _cfg(connections, roles=None):
        c = Cfg({"connections": connections, "roles": roles or {}})
        c.roles = roles or {}
        return c

    base = {"id": "napcat_default", "platform": "napcat", "name": "NapCat",
            "enabled": True, "ws_url": "ws://127.0.0.1:3001", "token": ""}
    extra = {"id": "napcat_role_b", "platform": "napcat", "name": "NapCat（B）",
             "enabled": True, "ws_url": "ws://127.0.0.1:3002", "token": "tb"}
    roles = {"a": {"character_key": "a", "character_name": "A"},
             "b": {"character_key": "b", "character_name": "B",
                   "connection_id": "napcat_role_b"}}
    cfg = _cfg([base, extra], roles)
    profiles = build_connection_profiles(cfg)
    check("没绑角色的接入方式不带角色标识",
          lambda: [p["key"] for p in profiles] == ["napcat_default", "napcat_role_b"]
          and profiles[0]["role_key"] == "")
    check("绑了角色的接入方式带上角色标识",
          lambda: profiles[1]["role_key"] == "b"
          and profiles[1]["ws_url"] == "ws://127.0.0.1:3002")

    check("停用的接入方式不建连接",
          lambda: build_connection_profiles(_cfg([dict(base, enabled=False)], {})) == [])
    check("非 NapCat 的接入方式也进清单（交给各自适配器）",
          lambda: [(p["key"], p["platform"]) for p in build_connection_profiles(
              _cfg([{"id": "qq1", "platform": "qq_official", "enabled": True}], {}))]
          == [("qq1", "qq_official")])
    check("接入方式变动会被快照捕捉到",
          lambda: role_connection_snapshot(_cfg([dict(base, ws_url="ws://x")], {}))
          != role_connection_snapshot(_cfg([base], {})))

    # 微信/QQ 没有 ws_url：启动日志那行必须能容住，否则 KeyError 直接把程序带崩
    from main import connection_identity, connection_target_label
    wx = {"id": "wx1", "platform": "wechat_clawbot", "name": "微信 ClawBot",
          "enabled": True, "account_id": "abc@im.bot"}
    qq = {"id": "qq1", "platform": "qq_official", "name": "QQ 官方机器人",
          "enabled": True, "app_id": "102000000"}
    wx_profiles = build_connection_profiles(_cfg([wx], {}))
    check("微信接入方式的连接描述不抛 KeyError",
          lambda: connection_target_label(wx_profiles[0]) == "微信 ClawBot")
    check("QQ 接入方式的连接描述不抛 KeyError",
          lambda: connection_target_label(
              build_connection_profiles(_cfg([qq], {}))[0]) == "QQ 官方机器人")
    check("NapCat 仍然打印地址",
          lambda: connection_target_label({"platform": "napcat",
                                           "ws_url": "ws://127.0.0.1:3001"})
          == "ws://127.0.0.1:3001")
    # NapCat 客户端的 __getattr__ 会给任意属性返回协程，hasattr 探不得
    class FakeNapCat:
        def __getattr__(self, name):
            async def _call(*_a, **_k):
                return {}
            return _call

    from main import client_snapshot

    class WithSnap:
        def snapshot(self):
            return {"cursor": "c1"}

    check("NapCat 那种动态属性客户端不会把游标落盘搞崩",
          lambda: client_snapshot(FakeNapCat()) == {})
    check("有 snapshot 的客户端能取到快照",
          lambda: client_snapshot(WithSnap()) == {"cursor": "c1"})

    from main import _sanitize_connections
    dirty = {"connections": [
        {"id": "n1", "platform": "napcat",
         "cursor": "<function NapCatClient.__getattr__.<locals>.dynamic_api_call at 0x7f1>"},
        {"id": "wx1", "platform": "wechat_clawbot", "cursor": "ChAIARDy8Nzz"}]}
    _sanitize_connections(dirty)
    check("早期版本写坏的游标会被清掉、正常的留着",
          lambda: "cursor" not in dirty["connections"][0]
          and dirty["connections"][1]["cursor"] == "ChAIARDy8Nzz")

    check("同平台同账号不会堆出第二条（扫码重复触发）",
          lambda: connection_identity("wechat_clawbot", wx) == "abc@im.bot"
          and connection_identity("qq_official", qq) == "102000000"
          and connection_identity("napcat", {"ws_url": "ws://x"}) == "")



    fake_default, fake_role = object(), object()
    snd = MessageSender(Cfg({}), None)
    snd.client = fake_default
    snd.set_role_client("b", fake_role)
    check("角色配了独占连接就用自己的连接",
          lambda: snd.client_for(roles["b"]) is fake_role)
    check("角色没配独占连接就用默认连接",
          lambda: snd.client_for(roles["a"]) is fake_default)
    snd.set_role_client("b", None)
    check("注销后回落到默认连接",
          lambda: snd.client_for(roles["b"]) is fake_default)

    async def _switch():
        with snd.using_client(fake_role):
            return snd._active_client()
        return snd._active_client()
    loop = _a.new_event_loop()
    try:
        got = loop.run_until_complete(_switch())
    finally:
        loop.close()

    async def _check_ctx():
        with snd.using_client(fake_role):
            inside = snd._active_client()
        return inside, snd._active_client()
    loop = _a.new_event_loop()
    try:
        inside, outside = loop.run_until_complete(_check_ctx())
    finally:
        loop.close()
    check("using_client 只在块内生效", lambda: inside is fake_role and outside is fake_default)

    # 路由：账号绑了角色就直接用它
    class RCfg(Cfg):
        active_character = "a"

    global_config = RCfg({"napcat_ws_url": "ws://127.0.0.1:3001", "napcat_token": "",
                          "roles": roles, "multi_role_enabled": True})
    global_config.roles = roles
    import main as _m
    saved = _m.global_config
    try:
        _m.global_config = global_config
        got = _m.resolve_target_roles("随便一句话，没提到名字", True, "b")
        check("账号绑定的角色优先（私聊也一样）",
              lambda: len(got) == 1 and got[0]["character_key"] == "b")
        check("没绑定角色时回落到当前角色",
              lambda: _m.resolve_target_roles("没有名字", True, "")[0]["character_key"]
              in ("a", "b"))
    finally:
        _m.global_config = saved





# ---------------------------------------------------------------------------
# C4 界面便利性：保存提示 / 快捷键 / 曲线悬停 / 插件名单 / 高级参数隐藏范围
# ---------------------------------------------------------------------------
def c4_webui_convenience():
    """这一批界面改动的静态守卫（前端行为没法在这里真跑，只守住关键接线）。"""
    import re

    section("C4 界面：保存提示 / 快捷键 / 曲线悬停 / 插件名单")
    html = (ROOT / "webui" / "start.html").read_text(encoding="utf-8")

    def line_for(key):
        m = re.search(r"^.*'%s': \{.*$" % re.escape(key), html, re.M)
        return m.group(0) if m else ""

    check("保存结果挪到右下角保存按钮上方",
          lambda: re.search(r'<div class="float-actions" id="float-actions">\s*'
                            r'<div id="config-message">', html) is not None
          and "#float-actions #config-message" in html
          and not re.search(r'<div class="config-form">\s*<div id="config-message">', html))

    check("Ctrl+S 等同于仅保存、Esc 逐层返回",
          lambda: "addEventListener('keydown'" in html and "escapeBack" in html
          and "saveConfig(false)" in html
          and "'.modal-overlay.active'" in html
          and "'plugin-page': 'installed-plugins'" in html
          and "panelHistory" in html)

    check("折线图悬停列出同一列所有曲线的数值",
          lambda: "chart-tip" in html and "_chartInfo" in html
          and "addEventListener('mousemove'" in html and "chart-tip-dot" in html
          and "info.datasets.map" in html)

    check("侧边栏「插件」可展开有 webui 的插件名单并跳转",
          lambda: 'id="plugin-menu-sub"' in html and "expandPluginMenuList" in html
          and "p.webui_ok" in html
          and "openPluginWebui(el.dataset.pid)" in html)

    check("点插件名也能直接进插件页面",
          lambda: "plugin-name-link" in html and "openPluginWebui(p.id)" in html)

    check("隐藏高级 GSV 参数只藏推理细节，不藏必填项",
          lambda: all(line_for(k) and "hide_gsv_options" not in line_for(k)
                      for k in ("client_base_url", "model_dir", "ref_audio_root",
                                "prompt_text", "prompt_lang", "text_lang",
                                "tts_auto_lang", "tts_duration_guard",
                                "tts_min_seconds_per_char", "tts_strip_non_dialogue"))
          and "hide_gsv_options: false" in line_for("top_k")
          and "hide_gsv_options: false" in line_for("speed_factor"))

    check("日志级别名单独染色、来源后有空格",
          lambda: "log-level log-${m[3]}" in html
          and ".log-output .log-level.log-INFO" in html
          and ".log-output .log-level.log-ERROR" in html
          and '<span class="log-source">' in html)

    check("长列表分页：每页 10 条",
          lambda: "const LIST_PAGE_SIZE = 10;" in html
          and "function renderPagedList(" in html
          and "pager-info" in html
          and html.count("renderPagedList(") >= 4)

    check("微信会话不显示头像、用户气泡一律绿色（按人分色只在 QQ 会话里）",
          lambda: "function isQqChatSession(" in html
          and "const qqChat = isQqChatSession(currentChatHistory);" in html
          and "if (isUser && qqChat) {" in html
          and "if (avatar) row.appendChild(avatar);" in html)

    check("界面文案都有英文对照（HTML 文本节点）",
          lambda: not _missing_html_i18n(html))

    check("时间感知排在提示词前段，并要求别顺着对方说错的时段接",
          lambda: _time_note_placement())


def c5_user_avatar():
    """聊天记录头像：QQ 号取真实头像并落盘缓存，取不到就交给前端用名字首字。"""
    import main as M
    import modules.media_cache as MC

    section("C5 聊天记录头像：真实头像与缓存")

    class FakeMem:
        data_path = ROOT / ".tmp_test" / "avatar_cache_case"

    saved_mem = M.memory_manager
    saved_download = M.download_image
    data_dir = FakeMem.data_path
    if data_dir.exists():
        for f in data_dir.glob("**/*"):
            if f.is_file():
                f.unlink()
    M.memory_manager = FakeMem()
    M.app_context.memory_manager = M.memory_manager
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    calls = []

    async def fake_download(url):
        calls.append(url)
        return png

    M.download_image = fake_download
    MC.download_image = fake_download
    try:
        try:
            first = asyncio.run(M.qq_avatar_bytes("1905332561"))
        except Exception as e:
            first = b""
            print("       取头像异常:", repr(e))
        check("QQ 号按号码取到头像并落盘",
              lambda: first == png
              and (data_dir / "avatar_cache" / "qq_1905332561.img").is_file()
              and calls and "nk=1905332561" in calls[0])
        calls.clear()
        check("缓存没过期时不再请求网络",
              lambda: asyncio.run(M.qq_avatar_bytes("1905332561")) == png and not calls)
        check("微信 openid 这类 ID 不取（前端退回名字首字）",
              lambda: asyncio.run(
                  M.qq_avatar_bytes("o9cq803cY8qdjYVpVczwZ0gfBNwU@im.wechat")) == b"")

        async def bad_download(url):
            calls.append(url)
            return b"<html>not an image</html>"

        M.download_image = bad_download
        MC.download_image = bad_download
        check("拿回来的不是图片就不当头像用",
              lambda: asyncio.run(M.qq_avatar_bytes("10001")) == b"")
        check("机器人自身 QQ 只在能取到时才拿来当角色头像",
              lambda: M._active_bot_qq() == "" or M._active_bot_qq().isdigit())
    finally:
        M.memory_manager = saved_mem
        M.app_context.memory_manager = M.memory_manager
        M.download_image = saved_download
        MC.download_image = saved_download


def _time_note_placement():
    """时段提示要显眼且可执行：排在末尾时模型会跟着对方把时段重复一遍。"""
    import sys as _sys

    _sys.path.insert(0, str(ROOT))
    from modules.llm_helpers import build_system_prompt, RoleContext

    prompt = build_system_prompt(
        RoleContext({"enable_time_awareness": True, "personality_prompt": "你是丛雨",
                     "json_prompt": "输出 JSON", "supplement_prompt": ""}, {}), {})
    off = build_system_prompt(
        RoleContext({"enable_time_awareness": False, "personality_prompt": "你是丛雨"}, {}), {})
    note = prompt.find("【当前时间】")
    return (note > 0 and prompt.count("【当前时间】") == 1
            and note < len(prompt) // 2
            and "不要把那个时段原样重复一遍" in prompt
            and off.count("【当前时间】") == 0)


def _missing_html_i18n(html):
    """HTML 里出现的中文文本节点都要有英文词条（按运行时的归一化规则比对）。"""
    import re as _re

    block_start = html.index("const I18N_EN = {")
    block_end = html.index("\n        };", block_start)
    keyset = set()
    for line in html[block_start:block_end].splitlines():
        m = _re.match(r'\s*"((?:[^"\\]|\\.)*)":', line)
        if m:
            keyset.add(" ".join(m.group(1).replace('\\"', '"').split()))
    cn = _re.compile(r"[\u4e00-\u9fff]")
    kana = _re.compile(r"[\u3040-\u30ff]")
    head = html[:html.find("<script")]
    # 样式块先剔掉：里面的中文是注释不是界面文案，而选择器里的 ">" 会让
    # 下面按 ">...<" 抓文本节点的正则把整段 CSS 当成一个巨大的文本节点
    head = _re.sub(r"<style[\s\S]*?</style>", "", head)
    missing = []
    for m in _re.finditer(r">([^<>]+)<", head):
        core = " ".join(m.group(1).split())
        if not core or not cn.search(core) or core in keyset or kana.search(core):
            continue
        missing.append(core)
    if missing:
        print("       缺英文：", " | ".join(missing[:5]))
    return missing


def main():
    print("Lovomo 第二轮修复回归测试")
    a_sentence_alignment()
    b_tts_lang_and_guard()
    c_image_identity()
    b2_affection()
    c2_neutral_context()
    c3_context_recheck()
    c4_webui_convenience()
    c5_user_avatar()
    d_search()
    d2_search_feedback_flow()
    e_repeat_guard()
    f_link_append()
    g_greeting_catchup()
    h_config_defaults()
    i_end_to_end()
    j_sticker_last()
    k_version_compare()
    l_speech_language()
    m_update_check_cache()
    n_tls_and_update_fallback()
    o_vision_base_url()
    p_cross_site_guard()
    q_export_scope()
    r_config_load_guard()
    s_model_list_endpoint()
    t_sticker_capture_reasons()
    u_repeat_guard_switches()
    v_voice_pacing()
    w_tts_runaway_tail()
    x_rename_to_lovomo()
    y_version_in_ui()
    z_role_napcat_connections()
    print(f"\n{'=' * 70}")
    print(f"结果：通过 {len(PASS)} | 失败 {len(FAIL)}")
    for name, err in FAIL:
        print(f"  - {name}: {err}")
    print(f"{'=' * 70}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
