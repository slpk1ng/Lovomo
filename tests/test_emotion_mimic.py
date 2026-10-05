# -*- coding: utf-8 -*-
"""情绪模仿（语气音色 + 说话情绪）回归测试。

覆盖：
  A 配置扫描：情绪模仿根目录独立于语气根目录扫描，留空表示不模仿
  B 字段透传：LLM 给的 mimic 经 normalize → 分句 → 分段全程不丢
  C 合成参数：命中 mimic 时情绪音频当主参考、语气音频加权当辅助参考；
    多段情绪音频随机挑一个；模仿参考音频被拒时退回纯语气；未命中时与不开该功能完全一致
  D WebUI 接口：kind=mimic 走情绪模仿根目录，缺省仍是语气目录；列表给出全部音频
  E 配置项三处同步 + 角色编辑器字段

运行: python tests/test_emotion_mimic.py      （全通过退出码 0）
"""
import asyncio
import contextlib
import io
import os
import re
import socket
import struct
import sys
import tempfile
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

import httpx  # noqa: E402
import main as M  # noqa: E402
from modules.llm_helpers import (RoleContext, available_mimics, normalize_single,  # noqa: E402
                                 normalize_sentences, segment_for_tts,
                                 set_mimics_provider, split_multi_clause_sentences,
                                 migrate_emotion_rules, resolve_emotion_key,
                                 _emotion_guide, _resolve_mimic_key,
                                 _emotion_miss_warned)
import modules.tts as T  # noqa: E402

PASS, FAIL = [], []
HTML = (ROOT / "webui" / "start.html").read_text(encoding="utf-8")


def check(name, fn):
    try:
        ok = bool(fn())
    except Exception as e:
        ok = False
        print(f"  !! {name} 抛异常: {type(e).__name__}: {e}")
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


def _norm(p):
    return str(p).replace("\\", "/").rstrip("/")


def _make_wav(path: Path, seconds=2.0, rate=16000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<h", 0) * int(rate * seconds))


def _wav_bytes(seconds=2.0, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<h", 0) * int(rate * seconds))
    return buf.getvalue()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


MIMICS = {
    "daxiao": {"ref_path": "C:/mimic/daxiao/ref.wav", "prompt_text": "哈哈哈"},
    "kuqi": {"ref_path": "C:/mimic/kuqi/ref.wav", "prompt_text": "呜呜呜"},
}
EMOTIONS = {
    "pingjing": {"ref_path": "C:/tone/pingjing/ref.wav", "prompt_text": "平静"},
    "haixiu": {"ref_path": "C:/tone/haixiu/ref.wav", "prompt_text": "害羞"},
}
MIMICS_MULTI = {
    "daxiao": {"ref_path": "C:/mimic/daxiao/daxiao.wav", "prompt_text": "哈哈哈",
               "candidates": ["C:/mimic/daxiao/daxiao.wav",
                              "C:/mimic/daxiao/daxiao2.wav"],
               "candidate_texts": {"C:/mimic/daxiao/daxiao2.wav": "第二段的文字"}},
}


def _ctx(**over):
    base = {"text_lang": "ja", "display_lang": "zh", "default_voice": "pingjing",
            "llm_judge": True, "emotion_mimic_enabled": True,
            "emotion_mimic_voice_weight": 3}
    base.update(over)
    return RoleContext(base, {"character_key": "murasame"})


# ------------------------------------------------- A 配置扫描


def build_a(tmp: Path):
    tone = tmp / "tone"
    mimic = tmp / "mimic"
    for name in ("pingjing", "haixiu"):
        _make_wav(tone / name / "ref.wav")
        (tone / name / "asr.txt").write_text(f"语气{name}", encoding="utf-8")
    _make_wav(tone / "haixiu" / "ref2.wav")
    for name in ("daxiao", "kuqi"):
        _make_wav(mimic / name / f"{name}.wav")
        (mimic / name / "asr.txt").write_text(f"情绪{name}", encoding="utf-8")
    _make_wav(mimic / "daxiao" / "daxiao2.wav")
    _make_wav(mimic / "daxiao" / "daxiao3.wav")

    def _mgr(root_mimic):
        return M.EmotionManager({"ref_audio_root": _norm(tone),
                                 "emotion_mimic_root": root_mimic,
                                 "prompt_text": "f", "default_voice": "pingjing"})

    def a1():
        mgr = _mgr(_norm(mimic))
        return sorted(mgr.emotions) == ["haixiu", "pingjing"] and \
            sorted(mgr.mimics) == ["daxiao", "kuqi"]

    def a2():
        mgr = _mgr(_norm(mimic))
        return mgr.mimics["daxiao"]["ref_path"].endswith("daxiao.wav") and \
            mgr.mimics["daxiao"]["prompt_text"] == "情绪daxiao"

    def a3():
        mgr = _mgr("")
        return mgr.mimics == {} and sorted(mgr.emotions) == ["haixiu", "pingjing"]

    def a4():
        return _mgr(_norm(tmp / "nope")).mimics == {}

    def a5():
        set_mimics_provider(lambda role: MIMICS)
        return available_mimics(_ctx()) == MIMICS

    def a6():
        set_mimics_provider(lambda role: MIMICS)
        return available_mimics(_ctx(emotion_mimic_enabled=False)) == {}

    def a7():
        return (_resolve_mimic_key("daxiao", MIMICS) == "daxiao"
                and _resolve_mimic_key("DaXiao", MIMICS) == "daxiao"
                and _resolve_mimic_key("none", MIMICS) == ""
                and _resolve_mimic_key("", MIMICS) == ""
                and _resolve_mimic_key("happy", MIMICS) == "")

    def a8():
        cfg = M.ConfigLoader(str(tmp / "roles_cfg.json"))
        cfg.config["ref_audio_root"] = _norm(tone)
        cfg.config["roles"] = [
            {"character_key": "r1", "ref_audio_root": _norm(tone),
             "emotion_mimic_root": _norm(mimic)},
            {"character_key": "r2", "ref_audio_root": _norm(tone),
             "emotion_mimic_root": ""},
        ]
        cfg.roles = cfg._parse_roles()
        old_cfg, old_mgr = M.global_config, M.global_emotion_manager
        try:
            M.global_config = cfg
            M.app_context.global_config = M.global_config
            M.global_emotion_manager = M.EmotionManager(cfg)
            M.app_context.global_emotion_manager = M.global_emotion_manager
            M._role_emotions_cache.clear()
            M._role_mimics_cache.clear()
            has = sorted(M.get_role_mimics(cfg.roles["r1"]))
            none = M.get_role_mimics(cfg.roles["r2"])
            return has == ["daxiao", "kuqi"] and none == {}
        finally:
            M.global_config, M.global_emotion_manager = old_cfg, old_mgr
            M.app_context.global_config = M.global_config
            M.app_context.global_emotion_manager = M.global_emotion_manager
            M._role_emotions_cache.clear()
            M._role_mimics_cache.clear()

    def a9():
        mgr = _mgr(_norm(mimic))
        cands = mgr.mimics["daxiao"]["candidates"]
        return len(cands) == 3 and cands[0].endswith("daxiao.wav") and \
            all(c.startswith(_norm(mimic / "daxiao")) for c in cands)

    def a10():
        mgr = _mgr(_norm(mimic))
        return mgr.emotions["haixiu"]["candidates"] == [mgr.emotions["haixiu"]["ref_path"]]

    def a11():
        return (resolve_emotion_key("yandere", ["Yandere"]) == "Yandere"
                and resolve_emotion_key("YANDERE", ["Yandere"]) == "Yandere"
                and resolve_emotion_key("haixiu。", ["haixiu"]) == "haixiu"
                and resolve_emotion_key("不在列表里", ["高兴"]) == "")

    def a15():
        # 目录名用中文时，模型写拼音/英文/别的中文说法都要落到同一个情绪上
        cn = ["高兴", "害羞", "平静"]
        return (resolve_emotion_key("高兴", cn) == "高兴"
                and resolve_emotion_key("gaoxing", cn) == "高兴"
                and resolve_emotion_key("happy", cn) == "高兴"
                and resolve_emotion_key("开心", cn) == "高兴"
                and resolve_emotion_key("shy", cn) == "害羞")

    def a16():
        return (resolve_emotion_key("高兴", ["gaoxing"]) == "gaoxing"
                and resolve_emotion_key("害羞", ["haixiu"]) == "haixiu")

    def a17():
        g = _emotion_guide(_ctx(), {"高兴": {}, "自创名": {}})
        return "- 高兴：开心、大笑" in g and "- 自创名：按字面意思选用" in g

    def a18():
        legacy = ("前缀【情绪匹配规则】情绪文件夹可能是拼音（如 gaoxing），也可能是英文（如 happy）。"
                 "你必须严格只输出我在【情绪可选列表】中提供的单词，"
                 "绝对不能输出中文汉字或拼音简写！后缀")
        cfg = {"supplement_prompt": legacy,
               "roles": [{"character_key": "a", "json_prompt": legacy},
                         {"character_key": "b", "supplement_prompt": "我自己的规则"}]}
        ok = migrate_emotion_rules(cfg) is True
        return (ok
                and "绝对不能输出中文汉字" not in cfg["supplement_prompt"]
                and "原样照抄" in cfg["supplement_prompt"]
                and "原样照抄" in cfg["roles"][0]["json_prompt"]
                and cfg["supplement_prompt"].startswith("前缀")
                and cfg["supplement_prompt"].endswith("后缀")
                and cfg["roles"][1]["supplement_prompt"] == "我自己的规则"
                and migrate_emotion_rules({"supplement_prompt": "没有旧句"}) is False)

    def a12():
        root = tmp / "tone_any"
        _make_wav(root / "zidingyi" / "sample.mp3")
        (root / "meiyouyinpin").mkdir(parents=True, exist_ok=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mgr = M.EmotionManager({"ref_audio_root": _norm(root), "prompt_text": "f",
                                    "default_voice": "zidingyi"})
        out = buf.getvalue()
        return (sorted(mgr.emotions) == ["zidingyi"]
                and mgr.emotions["zidingyi"]["ref_path"].endswith("sample.mp3")
                and "跳过目录 meiyouyinpin" in out
                and "改用目录里的 sample.mp3" in out)

    def a13():
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mgr = M.EmotionManager({"ref_audio_root": _norm(tone), "prompt_text": "f",
                                    "default_voice": "buzai"})
        return "默认情绪" in buf.getvalue() and mgr.get_emotion("随便写的") == {}

    def a14():
        ctx = _ctx(default_voice="pingjing")
        _emotion_miss_warned.discard("wuyu")
        before = len(_emotion_miss_warned)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            s1 = normalize_single({"zh": "测试", "emotion": "wuyu"}, ctx, {"pingjing": {}}, "x")
            normalize_single({"zh": "测试", "emotion": "wuyu"}, ctx, {"pingjing": {}}, "x")
        logged = buf.getvalue()
        return (s1["emotion"] == "pingjing"
                and len(_emotion_miss_warned) == before + 1
                and logged.count("不在【情绪可选列表】里") == 1
                and "当前可用" in logged)

    return [("扫描到语气与情绪模仿两套配置", a1),
            ("情绪模仿取 <名字>.<ext> 与 asr.txt", a2),
            ("根目录留空则不启用模仿", a3),
            ("根目录不存在时静默为空", a4),
            ("available_mimics 返回目录内容", a5),
            ("关闭开关后不提供模仿列表", a6),
            ("mimic 取值收敛（精确/大小写/none/列表外）", a7),
            ("每个角色可配独立模仿根目录", a8),
            ("模仿目录收全部音频当候选", a9),
            ("语气目录只留选中的那一个", a10),
            ("情绪名收敛：自定义名/大小写/别名/列表外", a11),
            ("目录名用中文时模型写拼音/英文也能命中", a15),
            ("目录名用拼音时模型写中文也能命中", a16),
            ("中文目录名照样带出对应的情绪说明", a17),
            ("旧提示词的「只能输出拼音」规则被迁移掉", a18),
            ("自定义目录：任意音频可用，无音频的目录点名跳过", a12),
            ("默认情绪不在目录里时告警且不抛错", a13),
            ("列表外的情绪回落默认音色并提示一次", a14)]


# ------------------------------------------------- B 字段透传


def build_b():
    set_mimics_provider(lambda role: MIMICS)

    def b1():
        s = normalize_single({"zh": "不要笑啦。", "ja": "笑わないで。",
                              "emotion": "haixiu", "mimic": "daxiao"},
                             _ctx(), EMOTIONS, "u")
        return s["mimic"] == "daxiao" and s["emotion"] == "haixiu"

    def b2():
        s = normalize_single({"zh": "不要笑啦。", "ja": "笑わないで。",
                              "emotion": "haixiu", "mimic": "none"},
                             _ctx(), EMOTIONS, "u")
        return s["mimic"] == ""

    def b3():
        s = normalize_single({"zh": "不要笑啦。", "ja": "笑わないで。",
                              "emotion": "haixiu", "mimic": "daxiao"},
                             _ctx(llm_judge=False), EMOTIONS, "u")
        return s["mimic"] == ""

    def b4():
        s = normalize_single({"zh": "不要笑啦。", "ja": "笑わないで。",
                              "emotion": "haixiu", "mimic": "daxiao"},
                             _ctx(), EMOTIONS, "u")
        out = split_multi_clause_sentences([s])
        return bool(out) and all(p.get("mimic") == "daxiao" for p in out)

    def b5():
        s = normalize_single({"zh": "不要笑啦。", "ja": "笑わないで。",
                              "emotion": "haixiu", "mimic": "daxiao"},
                             _ctx(), EMOTIONS, "u")
        out = segment_for_tts([s])
        return bool(out) and all(p.get("mimic") == "daxiao" for p in out)

    def b6():
        out = normalize_sentences(
            '{"sentences":[{"zh":"哈哈哈。","ja":"ははは。",'
            '"emotion":"haixiu","mimic":"daxiao"}]}', _ctx(), EMOTIONS, "u")
        return len(out) == 1 and out[0]["mimic"] == "daxiao"

    def b7():
        out = normalize_sentences(
            '{"sentences":[{"zh":"嗯。","ja":"うん。","emotion":"haixiu"}]}',
            _ctx(), EMOTIONS, "u")
        return len(out) == 1 and out[0]["mimic"] == ""

    def b8():
        out = normalize_sentences(
            '{"sentences":[{"zh":"哈哈哈。","ja":"ははは。",'
            '"emotion":"haixiu","mimic":"kuqi"}]}', _ctx(), EMOTIONS, "u")
        return len(out) == 1 and out[0]["mimic"] == "kuqi"

    return [("normalize_single 带出 mimic", b1),
            ("none 收敛成空", b2),
            ("关闭 LLM 判别时不模仿", b3),
            ("分句后 mimic 不丢", b4),
            ("分段后 mimic 不丢", b5),
            ("normalize_sentences 全链路保留", b6),
            ("模型没给 mimic 时为空", b7),
            ("另一情绪模仿同样生效", b8)]


# ------------------------------------------------- C 合成参数


def build_c():
    captured = {}

    class FakeResp:
        status_code = 200
        content = _wav_bytes()

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None):
            captured["params"] = dict(params or {})
            captured.setdefault("all", []).append(dict(params or {}))
            return FakeResp()

    class FakeRespBadRef:
        status_code = 400
        content = b""
        text = '{"message":"参考音频在3~10秒范围外，请更换！"}'

    class FakeClientBadRef(FakeClient):
        async def get(self, url, params=None):
            captured["params"] = dict(params or {})
            captured.setdefault("all", []).append(dict(params or {}))
            if (params or {}).get("aux_ref_audio_paths"):
                return FakeRespBadRef()
            return FakeResp()

    base_cfg = {"default_voice": "pingjing", "client_base_url": "http://127.0.0.1:9880",
                "timeout_seconds": 5, "text_lang": "ja", "prompt_lang": "ja",
                "emotion_mimic_voice_weight": 3, "tts_auto_lang": False,
                "tts_duration_guard": False}

    def _synth(mimic="daxiao", mimics=None, **over):
        cfg = dict(base_cfg)
        cfg.update(over)
        captured.clear()
        with tempfile.TemporaryDirectory() as td:
            out = asyncio.run(T.synthesize_sentence(
                cfg, "笑わないで。", "haixiu", EMOTIONS, Path(td),
                mimic=mimic, mimics=MIMICS if mimics is None else mimics))
        return out, dict(captured.get("params", {}))

    orig = T.httpx.AsyncClient
    orig_choice = T.random.choice
    T.httpx.AsyncClient = FakeClient
    try:
        o1, p1 = _synth()
        _, p2 = _synth()
        _, p3 = _synth()
        _, p4 = _synth(mimic="")
        _, p5 = _synth(mimic="")
        _, p6 = _synth(emotion_mimic_voice_weight=1)
        _, p7 = _synth(emotion_mimic_voice_weight="bad")
        _, p8 = _synth(mimic="不存在的情绪")
        T.random.choice = lambda seq: list(seq)[-1]
        _, p9 = _synth(mimics=MIMICS_MULTI)
        T.random.choice = lambda seq: list(seq)[0]
        _, p11 = _synth(mimics=MIMICS_MULTI)
        T.random.choice = orig_choice
        T.httpx.AsyncClient = FakeClientBadRef
        o10, p10 = _synth()
        all10 = list(captured.get("all", []))
    finally:
        T.httpx.AsyncClient = orig
        T.random.choice = orig_choice

    return [
        ("命中 mimic 时情绪音频当主参考",
         lambda: o1 is not None and p1.get("ref_audio_path") == "C:/mimic/daxiao/ref.wav"),
        ("命中 mimic 时用情绪音频的 asr",
         lambda: p2.get("prompt_text") == "哈哈哈"),
        ("语气音频按权重重复当辅助参考",
         lambda: p3.get("aux_ref_audio_paths") == ["C:/tone/haixiu/ref.wav"] * 3),
        ("未命中 mimic 时不发辅助参考",
         lambda: "aux_ref_audio_paths" not in p4
         and p4.get("ref_audio_path") == "C:/tone/haixiu/ref.wav"),
        ("未命中 mimic 时 asr 仍是语气音频的",
         lambda: p5.get("prompt_text") == "害羞"),
        ("权重为 1 时只重复一次",
         lambda: p6.get("aux_ref_audio_paths") == ["C:/tone/haixiu/ref.wav"]),
        ("权重非法时回落默认 4",
         lambda: p7.get("aux_ref_audio_paths") == ["C:/tone/haixiu/ref.wav"] * 4),
        ("mimic 解析不出时退回纯语气",
         lambda: "aux_ref_audio_paths" not in p8
         and p8.get("ref_audio_path") == "C:/tone/haixiu/ref.wav"),
        ("多段情绪音频里随机挑一个并用它自己的文字",
         lambda: p9.get("ref_audio_path") == "C:/mimic/daxiao/daxiao2.wav"
         and p9.get("prompt_text") == "第二段的文字"
         and p9.get("aux_ref_audio_paths") == ["C:/tone/haixiu/ref.wav"] * 3),
        ("挑中的音频没有自己的文字时用文件夹共用的",
         lambda: p11.get("ref_audio_path") == "C:/mimic/daxiao/daxiao.wav"
         and p11.get("prompt_text") == "哈哈哈"),
        ("模仿参考音频被拒时退回纯语气",
         lambda: o10 is not None and len(all10) == 4
         and all10[0].get("aux_ref_audio_paths")
         and "aux_ref_audio_paths" not in p10
         and p10.get("ref_audio_path") == "C:/tone/haixiu/ref.wav"
         and p10.get("prompt_text") == "害羞"),
    ]


# ------------------------------------------------- D WebUI 接口


def build_d(tmp: Path):
    tone = tmp / "wtone"
    mimic = tmp / "wmimic"
    for name in ("pingjing", "haixiu"):
        _make_wav(tone / name / "ref.wav")
    for name in ("daxiao", "kuqi"):
        _make_wav(mimic / name / f"{name}.wav")
    _make_wav(mimic / "daxiao" / "daxiao2.wav")

    results = []

    async def _run():
        port = _free_port()
        cfg = M.ConfigLoader(str(tmp / "api_cfg.json"))
        cfg.config.update({"webui_port": port, "webui_password": "",
                           "memory_data_path": str(tmp / "wdata"),
                           "ref_audio_root": _norm(tone),
                           "emotion_mimic_root": _norm(mimic),
                           "auto_start_tts": False,
                           "proactive_enabled": False,
                           "greeting_events_enabled": False})
        cfg.config["roles"] = [{"character_name": "丛雨", "character_key": "murasame",
                                "ref_audio_root": _norm(tone),
                                "emotion_mimic_root": _norm(mimic)}]
        cfg.roles = cfg._parse_roles()
        cfg.active_character = "murasame"
        M.global_config = cfg
        M.app_context.global_config = M.global_config
        M.global_emotion_manager = M.EmotionManager(cfg)
        M.app_context.global_emotion_manager = M.global_emotion_manager
        M.memory_manager = M.MemoryManager(cfg)
        M.app_context.memory_manager = M.memory_manager
        server = M.WebUIServer(cfg, M.memory_manager)
        await server.start()
        base = f"http://127.0.0.1:{port}"
        try:
            async with httpx.AsyncClient(timeout=30, trust_env=False) as hc:
                r = await hc.get(base + "/api/emotions/list",
                                 params={"role": "murasame", "kind": "mimic"})
                d = r.json()
                results.append(("kind=mimic 用模仿根目录",
                                r.status_code == 200 and d.get("kind") == "mimic"
                                and _norm(d.get("root")) == _norm(mimic)))
                results.append(("kind=mimic 列出情绪目录",
                                sorted(e["name"] for e in d["emotions"]) == ["daxiao", "kuqi"]))

                audios = {e["name"]: e.get("audios", []) for e in d["emotions"]}
                results.append(("列表给出文件夹里全部音频",
                                audios.get("daxiao") == ["daxiao.wav", "daxiao2.wav"]
                                and audios.get("kuqi") == ["kuqi.wav"]))

                r2 = await hc.get(base + "/api/emotions/list",
                                  params={"role": "murasame"})
                d2 = r2.json()
                results.append(("缺省 kind 仍是语气目录",
                                d2.get("kind") == "tone"
                                and _norm(d2.get("root")) == _norm(tone)
                                and sorted(e["name"] for e in d2["emotions"])
                                == ["haixiu", "pingjing"]))

                r3 = await hc.get(base + "/api/emotions/audio",
                                  params={"role": "murasame", "kind": "mimic",
                                          "emotion": "daxiao", "file": "daxiao.wav"})
                results.append(("kind=mimic 音频可播放",
                                r3.status_code == 200 and len(r3.content) > 40))

                r4 = await hc.post(base + "/api/emotions/create",
                                   json={"role": "murasame", "kind": "mimic",
                                         "emotion": "fennu"})
                created = r4.status_code == 200 and (mimic / "fennu").is_dir()
                results.append(("kind=mimic 在模仿根目录新建", created))

                r5 = await hc.post(base + "/api/emotions/delete",
                                   json={"role": "murasame", "kind": "mimic",
                                         "emotion": "fennu"})
                results.append(("kind=mimic 删除走模仿根目录",
                                r5.status_code == 200 and not (mimic / "fennu").exists()))

                # 情绪目录名放开到"只要不含路径分隔符"：中文、标点、括号都能用
                r4b = await hc.post(base + "/api/emotions/create",
                                    json={"role": "murasame", "kind": "tone",
                                          "emotion": "开心！"})
                results.append(("情绪名可以用中文与标点",
                                r4b.status_code == 200 and (tone / "开心！").is_dir()))
                r4c = await hc.get(base + "/api/emotions/list", params={"role": "murasame"})
                results.append(("新建的中文目录出现在列表里",
                                "开心！" in [e["name"] for e in r4c.json()["emotions"]]))
                r4d = await hc.post(base + "/api/emotions/create",
                                    json={"role": "murasame", "kind": "tone",
                                          "emotion": "../evil"})
                results.append(("带路径穿越的名字仍被拒绝",
                                r4d.status_code == 400 and not (tmp / "evil").exists()
                                and not (tone.parent / "evil").exists()))
                r4e = await hc.get(base + "/api/emotions/audio",
                                   params={"role": "murasame", "emotion": "开心！",
                                           "file": "../api_cfg.json"})
                results.append(("音频接口仍拒绝穿越取文件", r4e.status_code == 404))
                r4f = await hc.post(base + "/api/emotions/delete",
                                    json={"role": "murasame", "kind": "tone",
                                          "emotion": "开心！"})
                results.append(("中文名目录可以删除",
                                r4f.status_code == 200 and not (tone / "开心！").exists()))

                cfg.config["emotion_mimic_root"] = ""
                cfg.roles["murasame"]["emotion_mimic_root"] = ""
                r6 = await hc.get(base + "/api/emotions/list",
                                  params={"role": "murasame", "kind": "mimic"})
                results.append(("未配置模仿根目录时列表不报错",
                                r6.status_code == 200 and r6.json()["root"] == ""
                                and r6.json()["emotions"] == []))
                r7 = await hc.post(base + "/api/emotions/create",
                                   json={"role": "murasame", "kind": "mimic",
                                         "emotion": "x"})
                results.append(("未配置模仿根目录时新建报错",
                                r7.status_code == 400
                                and "情绪模仿根目录" in r7.json().get("error", "")))
        finally:
            await server.shutdown()

    asyncio.run(_run())
    return [(n, (lambda v=v: v)) for n, v in results]


# ------------------------------------------------- E 配置项同步


def build_e():
    def _declared():
        gs = HTML.index("const configGroups = [")
        ms = HTML.index("const configMeta = {")
        depth, me = 0, len(HTML)
        for i in range(ms, len(HTML)):
            if HTML[i] == "{":
                depth += 1
            elif HTML[i] == "}":
                depth -= 1
                if depth == 0:
                    me = i + 1
                    break
        return (set(re.findall(r"'([a-z_][a-z0-9_]*)'", HTML[gs:ms]))
                | set(re.findall(r"^\s{12}'([a-z_][a-z0-9_]*)':", HTML[ms:me], re.M)))

    KEYS = ["emotion_mimic_enabled", "emotion_mimic_root", "emotion_mimic_voice_weight"]

    def e1():
        d = M.ConfigLoader.default_config()
        return all(k in d for k in KEYS)

    def e2():
        decl = _declared()
        return all(k in decl for k in KEYS)

    def e3():
        return all(f"'{k}'" in HTML for k in KEYS)

    def e4():
        return 'data-role-key="emotion_mimic_root"' in HTML

    def e5():
        return not (_declared() - set(M.ConfigLoader.default_config()))

    def e6():
        return "em.audios" in HTML and "ref-file" in HTML and "player.play()" in HTML

    return [("default_config 含三个情绪模仿键", e1),
            ("configGroups/configMeta 已声明", e2),
            ("前端出现三个键名", e3),
            ("角色编辑器有情绪模仿根目录字段", e4),
            ("前端声明的键都在 default_config 里", e5),
            ("音频管理页可切换试听", e6)]


def main():
    with tempfile.TemporaryDirectory(prefix="lovomo_mimic_") as td:
        tmp = Path(td)
        print("\nA 情绪模仿配置扫描")
        for n, f in build_a(tmp):
            check(n, f)
        print("\nB mimic 字段透传")
        for n, f in build_b():
            check(n, f)
        print("\nC 合成参考音频组合")
        for n, f in build_c():
            check(n, f)
        print("\nD WebUI 接口 kind 切换")
        for n, f in build_d(tmp):
            check(n, f)
        print("\nE 配置项三处同步")
        for n, f in build_e():
            check(n, f)

    print("\n" + "=" * 70)
    print(f"结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        for n in FAIL:
            print(f"  - {n}")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
