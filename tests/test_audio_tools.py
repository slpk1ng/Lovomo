# -*- coding: utf-8 -*-
"""参考音频时长规整 + 一键语音识别的回归测试。

覆盖：
  A 时长规整：短音频补静音、长音频压缩内部停顿、已合规不动、压不下去时给出提示、
    没有 ffmpeg 时退回内置 WAV、非 WAV 且无 ffmpeg 时不动文件
  B 语音识别：语言/引擎归一化、GPT-SoVITS 目录推导、本地脚本参数与结果解析、
    线上 DashScope 请求体、任务进度与结果写回、临时目录删不掉时的重试与提示
  C 后端接口：规整与识别接口、鉴权外的参数校验、结果落盘
  D 配置项三处同步 + WebUI 控件

运行: python tests/test_audio_tools.py      （全通过退出码 0）
"""
import array
import asyncio
import contextlib
import io
import json
import os
import re
import socket
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
import modules.asr as ASR  # noqa: E402
import modules.audio_trim as TRIM  # noqa: E402

PASS, FAIL = [], []
HTML = (ROOT / "webui" / "start.html").read_text(encoding="utf-8")
RATE = 16000


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


def _make_wav(path: Path, seconds=2.0, silences=(), rate=RATE):
    """生成一段方波 WAV；silences 里的秒数位置填静音。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = array.array("h")
    total = int(seconds)
    for second in range(total):
        if second in silences:
            frames.extend([0] * rate)
        else:
            frames.extend([8000 if i % 40 < 20 else -8000 for i in range(rate)])
    tail = int((seconds - total) * rate)
    if tail > 0:
        frames.extend([8000 if i % 40 < 20 else -8000 for i in range(tail)])
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(frames.tobytes())
    return path


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------- A 时长规整


def build_a(tmp: Path):
    def a1():
        p = _make_wav(tmp / "a1.wav", 2.0)
        r = TRIM.normalize_audio(p)
        return (r["action"] == "padded" and TRIM.MIN_SECONDS <= r["after"] <= TRIM.MAX_SECONDS
                and abs(TRIM.probe_duration(p) - r["after"]) < 0.05)

    def a2():
        p = _make_wav(tmp / "a2.wav", 12.0, silences=(3, 4, 8))
        r = TRIM.normalize_audio(p)
        return r["action"] == "trimmed" and r["after"] <= TRIM.MAX_SECONDS

    def a3():
        p = _make_wav(tmp / "a3.wav", 5.0)
        before = p.read_bytes()
        r = TRIM.normalize_audio(p)
        return r["action"] == "ok" and p.read_bytes() == before

    def a4():
        # 全程有声的 12 秒音频没有停顿可压，必须给出提示且不改动文件
        p = _make_wav(tmp / "a4.wav", 12.0)
        before = p.read_bytes()
        lines = []
        r = TRIM.normalize_audio(p, log=lines.append)
        return (r["action"] == "failed" and "建议更换" in r["message"]
                and p.read_bytes() == before and any("a4.wav" in line for line in lines))

    def a5():
        # 没有 ffmpeg 时退回内置 WAV：补静音与压缩都要能用
        keep = (TRIM._ffmpeg, TRIM._ffprobe)
        TRIM._ffmpeg, TRIM._ffprobe = (lambda: None), (lambda: None)
        try:
            short = _make_wav(tmp / "a5_short.wav", 2.0)
            long = _make_wav(tmp / "a5_long.wav", 12.0, silences=(3, 4, 8))
            r1 = TRIM.normalize_audio(short)
            r2 = TRIM.normalize_audio(long)
        finally:
            TRIM._ffmpeg, TRIM._ffprobe = keep
        return (r1["action"] == "padded" and r1["after"] >= TRIM.MIN_SECONDS
                and r2["action"] == "trimmed" and r2["after"] <= TRIM.MAX_SECONDS)

    def a6():
        # 内置通道只认 WAV：给个 mp3 应该明确失败而不是写坏文件
        keep = (TRIM._ffmpeg, TRIM._ffprobe)
        TRIM._ffmpeg, TRIM._ffprobe = (lambda: None), (lambda: None)
        try:
            p = tmp / "a6.mp3"
            p.write_bytes(b"\x00" * 200)
            r = TRIM.normalize_audio(p)
        finally:
            TRIM._ffmpeg, TRIM._ffprobe = keep
        return r["action"] == "failed" and p.read_bytes() == b"\x00" * 200

    def a7():
        p = _make_wav(tmp / "a7.wav", 2.0)
        lines = []
        TRIM.normalize_audio(p, log=lines.append)
        return any("a7.wav" in line and "补静音" in line for line in lines)

    def a8():
        # 压缩后如果短于下限就不该落地（12 秒里 11.9 秒是静音）
        p = _make_wav(tmp / "a8.wav", 12.0, silences=tuple(range(0, 11)))
        before = p.read_bytes()
        r = TRIM.normalize_audio(p)
        return r["action"] == "failed" and p.read_bytes() == before

    return [("短音频补静音到下限", a1), ("长音频压缩内部停顿到上限内", a2),
            ("已合规音频不动文件", a3), ("没有停顿可压时给出提示且不动文件", a4),
            ("无 ffmpeg 时退回内置 WAV 处理", a5), ("无 ffmpeg 时非 WAV 明确失败", a6),
            ("规整结果写进日志", a7), ("压缩后会过短时不动文件", a8)]


# ------------------------------------------------- B 语音识别


def _fake_root(tmp: Path) -> Path:
    root = tmp / "GPT-SoVITS"
    (root / "runtime").mkdir(parents=True, exist_ok=True)
    (root / "runtime" / "python.exe").write_bytes(b"")
    for rel in ("tools/asr/funasr_asr.py", "tools/asr/fasterwhisper_asr.py"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# fake\n", encoding="utf-8")
    return root


class FakeProc:
    """替身进程：按真实脚本的行为把结果写成 <输入目录名>.list。"""

    captured = []

    def __init__(self, cmd, **kwargs):
        self.cmd = list(cmd)
        FakeProc.captured.append(self.cmd)
        in_dir = Path(self.cmd[self.cmd.index("-i") + 1])
        lines = [f"{f}|{in_dir.name}|ZH|这是{f.stem}的文字"
                 for f in sorted(in_dir.iterdir()) if f.is_file()]
        (in_dir / f"{in_dir.name}.list").write_text("\n".join(lines), encoding="utf-8")
        self.stdout = ["fake asr line\n"]

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0

    def kill(self):
        pass

    returncode = 0


class FakeResp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    @property
    def text(self):
        return json.dumps(self._payload, ensure_ascii=False)

    def json(self):
        return self._payload


class FakeAsyncClient:
    captured = {}

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        FakeAsyncClient.captured = {"url": url, "json": json, "headers": headers}
        return FakeResp({"choices": [{"message": {"content": "你好呀"}}]})


def build_b(tmp: Path):
    root = _fake_root(tmp)
    cfg = {"tts_start_script": str(root), "asr_local_model_size": "large-v3",
           "llm_base_url": "http://127.0.0.1:9999/v1", "llm_api_key": "k"}

    def b1():
        return (ASR.normalize_lang("JA") == "ja" and ASR.normalize_lang("xx") == "auto"
                and ASR.normalize_lang(None) == "auto"
                and ASR.normalize_engine("DashScope") == "dashscope"
                and ASR.normalize_engine("") == "local")

    def b2():
        return (ASR.resolve_sovits_root(cfg) == root
                and ASR.resolve_sovits_root({"tts_start_script": str(root / "api_v2.py")}) == root
                and ASR.resolve_sovits_root({}) is None)

    def b3():
        # 中文走达摩 ASR，不传 -s
        audio = _make_wav(tmp / "b3.wav", 4.0)
        FakeProc.captured = []
        keep = ASR.subprocess.Popen
        ASR.subprocess.Popen = FakeProc
        try:
            out = ASR.transcribe_local(cfg, [("k1", audio)], "zh")
        finally:
            ASR.subprocess.Popen = keep
        cmd = FakeProc.captured[-1]
        return (out.get("k1") == "这是0_b3的文字" and "-s" not in cmd
                and "funasr_asr.py" in " ".join(cmd))

    def b4():
        # 其它语种走 Faster Whisper，并把模型规格带上
        audio = _make_wav(tmp / "b4.wav", 4.0)
        FakeProc.captured = []
        keep = ASR.subprocess.Popen
        ASR.subprocess.Popen = FakeProc
        try:
            ASR.transcribe_local(cfg, [("k1", audio)], "ja")
        finally:
            ASR.subprocess.Popen = keep
        cmd = FakeProc.captured[-1]
        return ("fasterwhisper_asr.py" in " ".join(cmd)
                and cmd[cmd.index("-s") + 1] == "large-v3"
                and cmd[cmd.index("-l") + 1] == "ja")

    def b5():
        audio = _make_wav(tmp / "b5.wav", 4.0)
        keep = ASR.httpx.AsyncClient
        ASR.httpx.AsyncClient = FakeAsyncClient
        try:
            text = asyncio.run(ASR.transcribe_dashscope(cfg, audio, "zh"))
            body = FakeAsyncClient.captured["json"]
            url = FakeAsyncClient.captured["url"]
        finally:
            ASR.httpx.AsyncClient = keep
        return (text == "你好呀" and url.endswith("/chat/completions")
                and body["model"] == ASR.DEFAULT_DASHSCOPE_MODEL
                and "input_audio" in body["messages"][0]["content"][0]
                and body["asr_options"]["language"] == "zh")

    def b6():
        audio = _make_wav(tmp / "b6.wav", 4.0)
        keep = ASR.httpx.AsyncClient
        ASR.httpx.AsyncClient = FakeAsyncClient
        try:
            asyncio.run(ASR.transcribe_dashscope(cfg, audio, "auto"))
            body = FakeAsyncClient.captured["json"]
        finally:
            ASR.httpx.AsyncClient = keep
        return "asr_options" not in body

    def b7():
        audio = _make_wav(tmp / "b7.wav", 4.0)
        keep = ASR.transcribe_local
        ASR.transcribe_local = lambda config, pairs, lang, log=None: {str(audio): "本地文字"}
        try:
            job = ASR.AsrJob(cfg, [{"audio": str(audio), "emotion": "gaoxing"}], "zh", "local")
            job.start()
            job._thread.join(20)
        finally:
            ASR.transcribe_local = keep
        snap = job.snapshot()
        return (not snap["running"] and snap["done"] == 1
                and snap["results"][0]["text"] == "本地文字"
                and snap["results"][0]["emotion"] == "gaoxing"
                and audio.with_suffix(".txt").read_text(encoding="utf-8") == "本地文字")

    def b8():
        good = _make_wav(tmp / "b8_good.wav", 4.0)
        bad = _make_wav(tmp / "b8_bad.wav", 4.0)

        async def fake(config, audio, lang):
            if Path(audio).name.startswith("b8_bad"):
                raise RuntimeError("boom")
            return "线上文字"

        keep = ASR.transcribe_dashscope
        ASR.transcribe_dashscope = fake
        try:
            job = ASR.AsrJob(cfg, [{"audio": str(good)}, {"audio": str(bad)}], "zh", "dashscope")
            job.start()
            job._thread.join(20)
        finally:
            ASR.transcribe_dashscope = keep
        snap = job.snapshot()
        texts = {r["name"]: r for r in snap["results"]}
        return (snap["done"] == 2 and texts["b8_good.wav"]["text"] == "线上文字"
                and texts["b8_bad.wav"]["text"] == ""
                and "boom" in texts["b8_bad.wav"]["error"])

    def b9():
        audio = _make_wav(tmp / "b9.wav", 4.0)
        keep = ASR.transcribe_local
        ASR.transcribe_local = lambda config, pairs, lang, log=None: {}
        try:
            job = ASR.start_job(cfg, [{"audio": str(audio)}], "zh", "local")
            job._thread.join(20)
        finally:
            ASR.transcribe_local = keep
        snap = ASR.get_job(job.id).snapshot()
        return (not snap["running"] and snap["results"][0]["text"] == ""
                and "未识别出文字" in snap["results"][0]["error"]
                and ASR.get_job("nope") is None)

    def b10():
        # 规格名不在脚本允许的范围里时回落默认值，别把非法参数丢给脚本
        return (ASR.whisper_model_size(cfg) == "large-v3"
                and ASR.whisper_model_size({"asr_local_model_size": "small"})
                == ASR.DEFAULT_WHISPER_SIZE
                and ASR.whisper_model_size({}) == ASR.DEFAULT_WHISPER_SIZE)

    def b11():
        """临时目录第一次删不掉（被杀软占着）时会重试，仍失败就说明残留位置。"""
        work = tmp / "b11_tmp"
        work.mkdir(parents=True, exist_ok=True)
        (work / "a.txt").write_text("x", encoding="utf-8")
        calls = {"n": 0}
        real_rmtree = ASR.shutil.rmtree

        def flaky_rmtree(path, ignore_errors=False):
            calls["n"] += 1
            if calls["n"] == 1:
                return                   # 第一次"删不掉"
            real_rmtree(path, ignore_errors=ignore_errors)

        ASR.shutil.rmtree = flaky_rmtree
        try:
            ASR._cleanup_tmp_dir(work)
            retried = calls["n"] == 2 and not work.exists()
            # 三次都删不掉：必须把残留位置打出来，别静默占着磁盘
            calls["n"] = 0
            ASR.shutil.rmtree = lambda path, ignore_errors=False: calls.__setitem__(
                "n", calls["n"] + 1)
            work.mkdir(parents=True, exist_ok=True)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                ASR._cleanup_tmp_dir(work)
            told = str(work) in buf.getvalue()
        finally:
            ASR.shutil.rmtree = real_rmtree
        return retried and told

    def b12():
        """脚本要靠引导代码拿到 GPT-SoVITS 根目录：runtime 的 ._pth 会忽略 PYTHONPATH，
        Faster Whisper 脚本里的 import tools.* 否则直接 ModuleNotFoundError。"""
        audio = _make_wav(tmp / "b12.wav", 4.0)
        FakeProc.captured = []
        keep = ASR.subprocess.Popen
        ASR.subprocess.Popen = FakeProc
        try:
            ASR.transcribe_local(cfg, [("k1", audio)], "auto")
        finally:
            ASR.subprocess.Popen = keep
        cmd = FakeProc.captured[-1]
        return (cmd[1:3] == ["-c", ASR.SCRIPT_BOOTSTRAP]
                and "sys.path.insert" in ASR.SCRIPT_BOOTSTRAP
                and cmd[3] == str(root)
                and cmd[4].replace("\\", "/").endswith(ASR.LOCAL_SCRIPTS["whisper"])
                and cmd[cmd.index("-l") + 1] == "auto")

    def b13():
        """脚本没产出任何结果时必须报出退出码与最后几行，而不是含糊的"未识别出文字"。"""
        class DeadProc(FakeProc):
            def __init__(self, cmd, **kwargs):
                FakeProc.captured.append(list(cmd))
                self.stdout = ["Traceback (most recent call last):\n",
                               "ModuleNotFoundError: No module named 'tools'\n"]
                self.returncode = 1

            def wait(self, timeout=None):
                return 1

            def poll(self):
                return 1

        audio = _make_wav(tmp / "b13.wav", 4.0)
        keep = ASR.subprocess.Popen
        ASR.subprocess.Popen = DeadProc
        try:
            ASR.transcribe_local(cfg, [("k1", audio)], "auto")
            return False
        except RuntimeError as e:
            return "退出码 1" in str(e) and "No module named 'tools'" in str(e)
        finally:
            ASR.subprocess.Popen = keep

    return [("语言与引擎归一化", b1), ("从 tts_start_script 推出 GPT-SoVITS 目录", b2),
            ("中文走达摩 ASR", b3), ("其它语种走 Faster Whisper 并带模型规格", b4),
            ("线上识别请求体", b5), ("线上识别自动语种不带 asr_options", b6),
            ("本地任务写回同名 txt", b7), ("线上任务逐条记录成败", b8),
            ("任务注册表可查且识别不出时给原因", b9), ("非法模型规格回落默认值", b10),
            ("临时目录删不掉时重试并报出残留", b11),
            ("本地脚本以根目录为模块根启动（auto/非中文不再报错）", b12),
            ("脚本没产出结果时报出退出码与原因", b13)]


# ------------------------------------------------- C 后端接口


def build_c(tmp: Path):
    mimic = tmp / "cmimic"
    _make_wav(mimic / "gaoxing" / "gaoxing.wav", 2.0)
    _make_wav(mimic / "gaoxing" / "gaoxing2.wav", 12.0, silences=(3, 4, 8))

    results = []

    async def _run():
        port = _free_port()
        cfg = M.ConfigLoader(str(tmp / "api_cfg.json"))
        cfg.config.update({"webui_port": port, "webui_password": "",
                           "memory_data_path": str(tmp / "wdata"),
                           "emotion_mimic_root": _norm(mimic),
                           "auto_start_tts": False, "proactive_enabled": False,
                           "greeting_events_enabled": False})
        cfg.config["roles"] = [{"character_name": "丛雨", "character_key": "murasame",
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
        keep = ASR.transcribe_local
        ASR.transcribe_local = lambda config, pairs, lang, log=None: {str(p): "识别文字"
                                                                     for _, p in pairs}
        try:
            async with httpx.AsyncClient(timeout=60, trust_env=False) as hc:
                r = await hc.post(base + "/api/emotions/normalize",
                                  json={"role": "murasame", "kind": "mimic",
                                        "items": [{"emotion": "gaoxing",
                                                   "files": ["gaoxing.wav", "gaoxing2.wav"]}]})
                d = r.json()
                actions = {i["name"]: i["action"] for i in d.get("results", [])}
                results.append(("规整接口按选择处理两个音频",
                                r.status_code == 200 and d.get("success")
                                and actions == {"gaoxing.wav": "padded",
                                                "gaoxing2.wav": "trimmed"}))

                r2 = await hc.post(base + "/api/emotions/normalize",
                                   json={"role": "murasame", "kind": "mimic",
                                         "items": [{"emotion": "gaoxing",
                                                    "files": ["../api_cfg.json"]}]})
                results.append(("规整接口拒绝越界文件名",
                                r2.status_code == 400
                                and "文件名非法" in r2.json().get("error", "")))

                r3 = await hc.post(base + "/api/asr/start",
                                   json={"role": "murasame", "kind": "mimic",
                                         "items": [{"emotion": "gaoxing",
                                                    "files": ["gaoxing.wav"]}],
                                         "engine": "local", "lang": "zh"})
                started = r3.json()
                for _ in range(60):
                    status = await hc.get(base + "/api/asr/status",
                                          params={"job_id": started.get("job_id", "")})
                    snap = status.json()
                    if not snap.get("running"):
                        break
                    await asyncio.sleep(0.2)
                results.append(("识别接口返回任务并在完成后给出结果",
                                r3.status_code == 200 and not snap.get("running")
                                and snap["results"][0]["text"] == "识别文字"))
                results.append(("识别结果写成同名 txt",
                                (mimic / "gaoxing" / "gaoxing.txt").read_text(encoding="utf-8")
                                == "识别文字"))

                r4 = await hc.get(base + "/api/asr/status", params={"job_id": "missing"})
                results.append(("查询不存在的任务返回 404", r4.status_code == 404))

                r5 = await hc.get(base + "/api/emotions/list",
                                  params={"role": "murasame", "kind": "mimic"})
                em = {e["name"]: e for e in r5.json()["emotions"]}["gaoxing"]
                results.append(("列表带上每个音频的识别文字",
                                em.get("texts", {}).get("gaoxing.wav") == "识别文字"))

                r6 = await hc.post(base + "/api/emotions/text",
                                   json={"role": "murasame", "kind": "mimic",
                                         "emotion": "gaoxing",
                                         "texts": {"gaoxing2.wav": "手写的文字"}})
                results.append(("每段音频的文字可单独保存",
                                r6.status_code == 200
                                and (mimic / "gaoxing" / "gaoxing2.txt")
                                .read_text(encoding="utf-8") == "手写的文字"))

                r7 = await hc.post(base + "/api/emotions/text",
                                   json={"role": "murasame", "kind": "mimic",
                                         "emotion": "gaoxing",
                                         "texts": {"gaoxing2.wav": "   "}})
                results.append(("文字留空则删掉那个 txt",
                                r7.status_code == 200
                                and not (mimic / "gaoxing" / "gaoxing2.txt").exists()))

                r8 = await hc.post(base + "/api/emotions/text",
                                   json={"role": "murasame", "kind": "mimic",
                                         "emotion": "gaoxing",
                                         "texts": {"nope.wav": "x"}})
                results.append(("文字接口拒绝不存在的音频",
                                r8.status_code == 400
                                and "文件不存在" in r8.json().get("error", "")))

                r9 = await hc.post(base + "/api/emotions/text",
                                   json={"role": "murasame", "kind": "mimic",
                                         "emotion": "gaoxing", "texts": {}})
                results.append(("文字接口拒绝空请求",
                                r9.status_code == 400))

                r10 = await hc.post(base + "/api/emotions/text",
                                    json={"role": "murasame", "kind": "mimic",
                                          "emotion": "gaoxing", "texts": {},
                                          "shared": "文件夹共用文字"})
                results.append(("只改文件夹共用文字也能保存",
                                r10.status_code == 200
                                and (mimic / "gaoxing" / "asr.txt")
                                .read_text(encoding="utf-8") == "文件夹共用文字"))

                r11 = await hc.post(base + "/api/emotions/text",
                                    json={"role": "murasame", "kind": "mimic",
                                          "emotion": "gaoxing", "texts": {},
                                          "shared": "   "})
                results.append(("共用文字留空则删掉 asr.txt",
                                r11.status_code == 200
                                and not (mimic / "gaoxing" / "asr.txt").exists()))
        finally:
            ASR.transcribe_local = keep
            await server.shutdown()

    asyncio.run(_run())
    return [(n, (lambda v=v: v)) for n, v in results]


# ------------------------------------------------- D 每段音频自己的文字


def build_d(tmp: Path):
    tone = tmp / "dtone"
    _make_wav(tone / "pingjing" / "ref.wav", 4.0)
    (tone / "pingjing" / "asr.txt").write_text("文件夹共用", encoding="utf-8")
    (tone / "pingjing" / "ref.txt").write_text("这段音频自己的", encoding="utf-8")
    _make_wav(tone / "haixiu" / "ref.wav", 4.0)
    (tone / "haixiu" / "asr.txt").write_text("只有共用", encoding="utf-8")
    _make_wav(tone / "zhaoji" / "ref.wav", 4.0)

    mimic = tmp / "dmimic"
    _make_wav(mimic / "gaoxing" / "gaoxing.wav", 4.0)
    _make_wav(mimic / "gaoxing" / "gaoxing2.wav", 4.0)
    (mimic / "gaoxing" / "gaoxing2.txt").write_text("第二段自己的", encoding="utf-8")

    cfg = M.ConfigLoader(str(tmp / "d_cfg.json"))
    cfg.config.update({"ref_audio_root": _norm(tone), "emotion_mimic_root": _norm(mimic),
                       "prompt_text": "兜底文字"})
    em = M.EmotionManager(cfg)

    def d1():
        return em.emotions["pingjing"]["prompt_text"] == "这段音频自己的"

    def d2():
        return em.emotions["haixiu"]["prompt_text"] == "只有共用"

    def d3():
        return em.emotions["zhaoji"]["prompt_text"] == "兜底文字"

    def d4():
        return em.mimics["gaoxing"]["candidate_texts"] == {
            _norm(mimic / "gaoxing" / "gaoxing2.wav"): "第二段自己的"}

    def d5():
        # 主参考没有自己的文字时，仍回落到全局兜底，而不是另一段音频的文字
        return em.mimics["gaoxing"]["prompt_text"] == "兜底文字"

    return [("音频自己的文字优先于文件夹共用文字", d1),
            ("没有自己的文字时用文件夹共用文字", d2),
            ("两者都没有时用全局兜底文字", d3),
            ("候选音频各自的文字都收进配置", d4),
            ("不会串用别的音频的文字", d5)]


# ------------------------------------------------- E 配置项与前端


def build_e():
    KEYS = ["asr_engine", "asr_lang", "asr_base_url", "asr_dashscope_model",
            "asr_local_model_size"]

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

    def e1():
        d = M.ConfigLoader.default_config()
        return all(k in d for k in KEYS)

    def e2():
        decl = _declared()
        return all(k in decl for k in KEYS)

    def e3():
        return not (_declared() - set(M.ConfigLoader.default_config()))

    def e4():
        return ("api/emotions/normalize" in HTML and "api/asr/start" in HTML
                and "api/asr/status" in HTML and "api/emotions/text" in HTML)

    def e5():
        return ("ref-check" in HTML and "collectAudioSelection" in HTML
                and "check-all-audio" in HTML and "uncheck-all-audio" in HTML)

    def e6():
        return ("asr-engine-select" in HTML and "asr-lang-select" in HTML
                and 'value="ko"' in HTML and 'value="dashscope"' in HTML)

    def e7():
        return ("asr-run-btn" in HTML and "normalize-btn" in HTML
                and "pollAsrJob" in HTML and "asr-progress" in HTML)

    def e8():
        # 路由与处理器都要真的接上
        return (hasattr(M.WebUIServer, "handle_emotions_normalize")
                and hasattr(M.WebUIServer, "handle_asr_start")
                and hasattr(M.WebUIServer, "handle_asr_status")
                and hasattr(M.WebUIServer, "handle_emotions_text"))

    def e9():
        # _read_sidecar_text/_scan_ref_root 已搬至 modules/emotion_voices.py
        src = (ROOT / "modules" / "emotion_voices.py").read_text(encoding="utf-8")
        return "_read_sidecar_text" in src and "candidate_texts" in src

    def e10():
        src = (ROOT / "modules" / "tts.py").read_text(encoding="utf-8")
        return "candidate_texts" in src

    def e11():
        # 每段音频一个可编辑的文字框，而不是全文件夹共用一个 asr.txt
        return ('class="ref-text"' in HTML and "save-texts" in HTML
                and "该情绪参考音频所念的文字（asr.txt）" not in HTML
                and "card.querySelectorAll('.ref-text')" in HTML)

    def e12():
        # 文字框要预填「这段音频实际生效的文字」，否则用户会以为自己的 asr.txt 丢了
        return ("texts[f] || sharedText" in HTML and 'data-init="${esc(value)}"' in HTML
                and '${esc(value)}</textarea>' in HTML)

    def e13():
        # 文件夹共用文字是真实的 asr.txt 文件（可折叠、可编辑），不再是一句硬编码说明
        return ('<details class="ref-shared">' in HTML
                and '<summary>asr.txt</summary>' in HTML
                and 'class="shared-text"' in HTML
                and "文件夹共用文字（某段音频没单独填写时用它）" not in HTML)

    def e14():
        # 只提交改动过的框（避免没动过也生成一堆同名 txt），共用文字单独提交
        return ("payload.shared" in HTML and "payload.texts[t.dataset.file]" in HTML
                and "(t.dataset.init || '').trim()" in HTML)

    return [("默认配置含语音识别键", e1), ("配置项声明含语音识别键", e2),
            ("声明项都在默认配置里", e3), ("前端调用了规整与识别接口", e4),
            ("音频可多选与全选", e5), ("引擎与语言下拉齐全", e6),
            ("一键识别与规整时长按钮齐全", e7), ("路由处理器已实现", e8),
            ("扫描会读取同名 txt", e9), ("合成会使用所选音频的文字", e10),
            ("每段音频有自己的文字框", e11), ("文字框预填实际生效的文字", e12),
            ("共用文字是可编辑的真实文件", e13), ("只提交改动过的文字框", e14)]


# ------------------------------------------------- F 音量统一与参考音频电平


def build_f(tmp: Path):
    import numpy as np
    import modules.tts as T
    from modules import audio_level as AL

    KEYS = ["tts_loudness_normalize", "tts_loudness_target_db", "tts_loudness_peak_db",
            "tts_loudness_max_gain_db", "tts_ref_normalize"]

    def _wav(path, rms_db, seconds=4.0, rate=32000, freq=220.0, speech_seconds=None):
        """生成指定响度的正弦 WAV；speech_seconds 之后的都是静音。"""
        n = int(rate * seconds)
        sig = np.sin(2 * np.pi * freq * np.arange(n) / rate).astype(np.float32)
        sig *= (10 ** (rms_db / 20)) / max(float(np.sqrt(np.mean(sig ** 2))), 1e-9)
        if speech_seconds is not None:
            sig[int(rate * speech_seconds):] = 0.0
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(np.clip(sig * 32767, -32768, 32767).astype("<i2").tobytes())
        return path

    def f1():
        loud = AL.measure(_wav(tmp / "f_loud.wav", -12.0))
        quiet = AL.measure(_wav(tmp / "f_quiet.wav", -33.0))
        short = AL.measure(_wav(tmp / "f_short.wav", -18.0, seconds=1.5))
        return (loud["ok"] and abs(loud["rms_db"] + 12) < 1.0
                and abs(quiet["rms_db"] + 33) < 1.0
                and any("偏轻" in n for n in AL.quality_notes(quiet))
                and any("时长" in n for n in AL.quality_notes(short)))

    def f2():
        a = _wav(tmp / "f_seg_a.wav", -30.0, seconds=2.0)
        b = _wav(tmp / "f_seg_b.wav", -14.0, seconds=2.0)
        cfg = {"tts_loudness_normalize": True, "tts_loudness_target_db": -20.0,
               "tts_loudness_peak_db": -1.0, "tts_loudness_max_gain_db": 12.0}
        T._normalize_tts_output(a, cfg)
        T._normalize_tts_output(b, cfg)
        ra, rb = AL.measure(a)["rms_db"], AL.measure(b)["rms_db"]
        off = _wav(tmp / "f_seg_off.wav", -30.0, seconds=2.0)
        T._normalize_tts_output(off, {"tts_loudness_normalize": False})
        return abs(ra + 20) < 0.6 and abs(rb + 20) < 0.6 and abs(ra - rb) < 0.2 \
            and abs(AL.measure(off)["rms_db"] + 30) < 0.6

    def f3():
        ref = _wav(tmp / "f_ref.wav", -31.0, seconds=4.0)
        cfg = {"tts_ref_normalize": True, "tts_loudness_target_db": -20.0,
               "tts_loudness_peak_db": -1.0, "tts_loudness_max_gain_db": 12.0}
        first = T._tts_reference(ref, cfg, tmp / "f_data")
        again = T._tts_reference(ref, cfg, tmp / "f_data")
        stats = AL.measure(first)
        return (Path(first).name.startswith("ref_") and "ref_cache" in _norm(first)
                and first == again and abs(stats["rms_db"] + 20) < 1.5
                and T._tts_reference(ref, {"tts_ref_normalize": False}, tmp / "f_data")
                == str(ref))

    def f4():
        # 只有 0.6 秒人声、其余是静音的 3.2 秒参考音频：裁静音不能裁到 3 秒以下
        src = _wav(tmp / "f_short_speech.wav", -20.0, seconds=3.2, speech_seconds=0.6)
        out = AL.prepare_reference(src, tmp / "f_data2")
        return abs(AL.measure(out)["duration"] - 3.2) < 0.1

    def f5():
        voiced = AL.measure(_wav(tmp / "f_f0.wav", -18.0, seconds=3.0, freq=200.0))
        low = AL.measure(_wav(tmp / "f_f0_low.wav", -18.0, seconds=3.0, freq=110.0))
        return (abs(voiced["f0_hz"] - 200) < 30 and abs(low["f0_hz"] - 110) < 25
                and "音高" in AL.pitch_note(low, voiced["f0_hz"])
                and AL.pitch_note(voiced, voiced["f0_hz"]) == "")

    def f6():
        d = M.ConfigLoader.default_config()
        return all(k in d for k in KEYS) and all(k in HTML for k in KEYS) \
            and d["tts_loudness_normalize"] is True and d["tts_ref_normalize"] is True

    def f7():
        # 扫描期体检：多文件按问题归类成一行，单文件写细节
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            M._report_audio_quality("参考音频根目录", "着急",
                                    [(Path("a.mp3"), ["峰值 +0.2dB 已削波，合成容易发哑"], {})])
            M._report_audio_quality("情绪模仿根目录", "高兴", [
                (Path("b.WAV"), ["峰值 +1.0dB 已削波，合成容易发哑"], {}),
                (Path("c.WAV"), ["峰值 +1.5dB 已削波，合成容易发哑"], {}),
                (Path("d.WAV"), ["整体偏轻（RMS -31.0dB），合成声音也会偏小"], {})])
        text = buf.getvalue()
        return ("着急/a.mp3" in text and "+0.2dB" in text
                and "削波失真 × 2" in text and "整体偏轻 × 1" in text)

    return [("测量峰值/响度/时长问题", f1),
            ("每段语音归一到同一响度（可关）", f2),
            ("参考音频做单声道+裁静音+归一并缓存", f3),
            ("裁静音不会把参考音频裁到 3 秒以下", f4),
            ("音高估计与「不像同一个人」提醒", f5),
            ("新增配置项三处同步", f6),
            ("扫描期质量问题汇总成日志", f7)]


def build_g():
    """非台词内容剔除：动作/表情/场景/颜文字不进语音，台词原样保留。"""
    import modules.tts as T

    class _Cfg(dict):
        def get(self, k, d=None):
            return dict.get(self, k, d)

    section = print
    section("G 非台词内容剔除")

    def g1():
        strip = T.strip_non_dialogue_text
        return (strip("(笑) 主人，你回来啦～", log=False) == "主人，你回来啦～"
                and strip("[旁白] 雨下得很大。", log=False) == "雨下得很大。"
                and strip("【惊讶】诶？！", log=False) == "诶？！"
                and strip("（走到门口）我们走吧。", log=False) == "我们走吧。"
                and strip("呜…(*/ω＼*) 好害羞", log=False) == "呜… 好害羞")

    def g2():
        strip = T.strip_non_dialogue_text
        return (strip("「不过…」这是台词，不要动。", log=False)
                == "「不过…」这是台词，不要动。"
                and strip("主人～ ～～ 嗯。", log=False) == "主人～ ～～ 嗯。"
                and strip("普通台词，没有括号。", log=False) == "普通台词，没有括号。")

    def g3():
        return (T.strip_non_dialogue_enabled(_Cfg({})) is True
                and T.strip_non_dialogue_enabled(_Cfg({"tts_strip_non_dialogue": False})) is False
                and T.strip_non_dialogue_enabled(_Cfg({"tts_strip_non_dialogue": "off"})) is False)

    def g4():
        on = T.split_tts_chunks("(笑)主人，欢迎回来～", config=_Cfg({}))
        off = T.split_tts_chunks("(笑)主人，欢迎回来～",
                                 config=_Cfg({"tts_strip_non_dialogue": False}))
        return (all("笑" not in p for p in on)
                and any("笑" in p for p in off))

    def g5():
        """剔除动作后不应再报「清洗前后内容字不一致」。"""
        logs = []
        orig = T._safe_print
        T._safe_print = lambda *a, **k: logs.append(" ".join(str(x) for x in a))
        try:
            text = T.strip_non_dialogue_text("(笑)主人，欢迎回来。", log=False)
            clean = T._sanitize_tts_text(text)
            T._log_tts_payload(_Cfg({}), "(笑)主人，欢迎回来。", clean,
                               "haixiu", "ref.wav", spoken_source=text)
        finally:
            T._safe_print = orig
        return not any("内容字不一致" in line for line in logs)

    return [("括号/方括号里的动作表情场景被剔除", g1),
            ("引号台词与普通标点不受影响", g2),
            ("开关默认开启、可关闭", g3),
            ("切分后的片段也已剔除", g4),
            ("剔除非台词内容不算丢内容", g5)]


def main():
    with tempfile.TemporaryDirectory(prefix="lovomo_audio_") as td:
        tmp = Path(td)
        print("\nA 参考音频时长规整")
        for n, f in build_a(tmp):
            check(n, f)
        print("\nB 语音识别通道")
        for n, f in build_b(tmp):
            check(n, f)
        print("\nC 后端接口")
        for n, f in build_c(tmp):
            check(n, f)
        print("\nD 每段音频自己的文字")
        for n, f in build_d(tmp):
            check(n, f)
        print("\nE 配置项与前端")
        for n, f in build_e():
            check(n, f)
        print("\nF 音量统一与参考音频电平")
        for n, f in build_f(tmp):
            check(n, f)
        print("\nG 非台词内容剔除")
        for n, f in build_g():
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
