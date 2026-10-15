# -*- coding: utf-8 -*-
"""参考音频的语音识别：本地 GPT-SoVITS 自带 ASR 与线上 DashScope 两条通道。

本地走 GPT-SoVITS 的 tools/asr 脚本（中文用达摩 ASR，其余语种与自动交给 Faster Whisper），
线上走 DashScope 的 qwen3-asr-flash（OpenAI 兼容接口）。识别在后台线程里跑，
前端轮询进度；识别结果写回与音频同名的 <音频名>.txt。
"""
import asyncio
import base64
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx

from .llm_helpers import chat_endpoint
from .tls import verified_context
from .tts_service import no_window_kwargs

ASR_LANGS = ("auto", "zh", "ja", "en", "ko")
# 达摩 ASR 只认中文，其余语种（含自动）交给 Faster Whisper
FUNASR_LANGS = {"zh"}
LOCAL_SCRIPTS = {"funasr": "tools/asr/funasr_asr.py",
                 "whisper": "tools/asr/fasterwhisper_asr.py"}
WHISPER_MODEL_SIZES = ("medium", "medium.en", "large-v2", "large-v3", "large-v3-turbo")
DEFAULT_WHISPER_SIZE = "medium"
# GPT-SoVITS 的 runtime 是 embeddable Python：目录里的 python39._pth 会让解释器忽略
# PYTHONPATH，也不会把脚本所在目录放进 sys.path。Faster Whisper 脚本要 import
# tools.*，直接运行必然 ModuleNotFoundError（达摩脚本只 import 第三方库，所以
# 中文那条路看不出问题）。用一段引导代码把根目录塞进 sys.path 再跑脚本。
SCRIPT_BOOTSTRAP = (
    "import runpy, sys;"
    "sys.path.insert(0, sys.argv.pop(1));"
    "script = sys.argv.pop(1);"
    "sys.argv[0] = script;"
    "runpy.run_path(script, run_name='__main__')"
)
DEFAULT_DASHSCOPE_MODEL = "qwen3-asr-flash"
# DashScope 兼容接口单次音频上限 10MB（base64 之后），留出编码膨胀的余量
DASHSCOPE_MAX_AUDIO_BYTES = 7 * 1024 * 1024
DASHSCOPE_TIMEOUT_SECONDS = 180.0
# 本地首次使用要下载模型，给足时间
LOCAL_TIMEOUT_SECONDS = 3600.0
AUDIO_MIMES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
               ".flac": "audio/flac", ".m4a": "audio/mp4"}
JOB_LOG_LINES = 40
JOBS_KEPT = 20


def normalize_lang(lang) -> str:
    value = str(lang or "").strip().lower()
    return value if value in ASR_LANGS else "auto"


def normalize_engine(engine) -> str:
    return "dashscope" if str(engine or "").strip().lower() == "dashscope" else "local"


def whisper_model_size(config) -> str:
    value = str(config.get("asr_local_model_size", "") or "").strip()
    return value if value in WHISPER_MODEL_SIZES else DEFAULT_WHISPER_SIZE


def resolve_sovits_root(config) -> Optional[Path]:
    """从 tts_start_script 推出 GPT-SoVITS 根目录（该键可填目录，也可填 api_v2.py 路径）。"""
    raw = str(config.get("tts_start_script", "") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if path.is_dir():
        return path
    if path.suffix.lower() == ".py" or path.is_file():
        return path.parent
    return path.parent if path.parent.is_dir() else None


def _runtime_python(root: Path) -> Optional[Path]:
    for name in ("python.exe", "python"):
        candidate = root / "runtime" / name
        if candidate.exists():
            return candidate
    return None


def transcribe_local(config, targets: List[Tuple[str, Path]], lang: str,
                     log=None) -> Dict[str, str]:
    """本地批量识别，返回 {key: 文字}；key 由调用方给定，未识别出的不在结果里。"""
    root = resolve_sovits_root(config)
    if root is None:
        raise RuntimeError("未配置 tts_start_script，无法定位 GPT-SoVITS 目录")
    python_exe = _runtime_python(root)
    if python_exe is None:
        raise RuntimeError(f"找不到 {root / 'runtime'} 下的 python，无法调用本地 ASR")
    if lang in FUNASR_LANGS:
        script, script_lang = LOCAL_SCRIPTS["funasr"], lang
    else:
        # Whisper 传 auto 让模型自己判语种；判成中文时脚本内部会转达摩 ASR
        script, script_lang = LOCAL_SCRIPTS["whisper"], lang
    script_path = root / script
    if not script_path.exists():
        raise RuntimeError(f"找不到 ASR 脚本：{script_path}")

    # 脚本会把输入目录里的每个文件都识别一遍，所以只把选中的音频复制进临时目录
    tmp_dir = Path(tempfile.mkdtemp(prefix="lovomo_asr_"))
    mapping = {}
    try:
        for index, (key, audio) in enumerate(targets):
            name = f"{index}_{Path(audio).name}"
            shutil.copyfile(audio, tmp_dir / name)
            mapping[name] = key
        cmd = [str(python_exe), "-c", SCRIPT_BOOTSTRAP, str(root), str(script_path),
               "-i", str(tmp_dir), "-o", str(tmp_dir), "-l", script_lang]
        if script == LOCAL_SCRIPTS["whisper"]:
            cmd += ["-s", whisper_model_size(config)]
        proc = subprocess.Popen(cmd, cwd=str(root), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", **no_window_kwargs())
        # 超时必须在读流的同时生效：子进程卡住不退出时读 stdout 会一直阻塞，
        # 写在后面的 proc.wait(timeout=) 永远轮不到，任务会一直停在 running
        watchdog = threading.Timer(LOCAL_TIMEOUT_SECONDS, proc.kill)
        watchdog.daemon = True
        watchdog.start()
        tail = deque(maxlen=8)
        try:
            for line in proc.stdout:
                tail.append(line.rstrip())
                if log:
                    log(line)
            proc.wait(timeout=LOCAL_TIMEOUT_SECONDS)
        finally:
            watchdog.cancel()
            if proc.poll() is None:
                proc.kill()
        results, rows, empty_rows, found = _collect_asr_results(tmp_dir, mapping)
        if not results:
            detail = " / ".join(x for x in tail if x.strip())[:400]
            if found and (not rows or empty_rows == rows):
                # 脚本正常收尾（退出码 0）却没留下任何文字：短语音、静音、纯噪声，
                # 或音频解不开。识别不出文字属于语音识别的正常结果，不能当成
                # 脚本故障报错——调用方按「没转出文字」处理即可
                if log:
                    log(f"识别脚本没有给出文字（退出码 {proc.returncode}）："
                        + (detail or "结果文件为空"))
                return {}
            if rows:
                # 识别出了文字却一行都没对上输入文件：脚本的输出命名与读取端不一致
                raise RuntimeError(
                    f"ASR 脚本识别出了 {rows} 行结果但都没对上输入文件（退出码 "
                    f"{proc.returncode}）")
            # 压根没写出结果文件：模型没下好、参数被拒、缺依赖…
            raise RuntimeError(f"ASR 脚本没有产出结果（退出码 {proc.returncode}）"
                               + (f"：{detail}" if detail else ""))
        return results
    finally:
        _cleanup_tmp_dir(tmp_dir)


def _collect_asr_results(tmp_dir: Path, mapping: Dict[str, str]) -> Tuple[dict, int, int, bool]:
    """从脚本的输出目录收识别结果，返回 (结果, 结果行数, 空文本行数, 是否找到结果文件)。

    标准位置是 <输出目录>/<输入目录名>.list；不同版本的 GPT-SoVITS 脚本
    命名略有出入，找不到时按 *.list 兜底再扫一遍。脚本跑完却没识别出文字时
    会写出一个空结果文件，那种情况要能和「压根没写出文件」区分开。
    """
    list_text = _read_list_file(tmp_dir)
    found = bool(list_text)
    if not list_text.strip():
        for alt in sorted(tmp_dir.glob("*.list")):
            try:
                text = alt.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            found = True
            if text.strip():
                list_text = text
                break
    results: Dict[str, str] = {}
    rows = empty_rows = 0
    for raw in list_text.splitlines():
        if not raw.strip():
            continue
        parts = raw.split("|", 3)
        if len(parts) < 4:
            continue
        rows += 1
        key = mapping.get(Path(parts[0]).name)
        text = parts[3].strip()
        if not text:
            empty_rows += 1
            continue
        if key:
            results[key] = text
    return results, rows, empty_rows, found


def _cleanup_tmp_dir(tmp_dir: Path) -> None:
    """删掉识别用的临时目录。

    文件被杀软/索引器占着时 rmtree 会静默失败（ignore_errors=True），
    目录就此留在临时区；这里再试一次，仍然删不掉就把位置打出来。
    """
    shutil.rmtree(tmp_dir, ignore_errors=True)
    if not tmp_dir.exists():
        return
    time.sleep(0.2)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    if tmp_dir.exists():
        print(f"ASR 临时目录未能删除（可能被杀软占用），已留下：{tmp_dir}")


def _read_list_file(tmp_dir: Path) -> str:
    """脚本把结果写成 <输出目录>/<输入目录名>.list，每行 path|目录|语种|文字。"""
    target = tmp_dir / f"{tmp_dir.name}.list"
    try:
        return target.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


async def transcribe_dashscope(config, audio: Path, lang: str) -> str:
    model = str(config.get("asr_dashscope_model", "") or "").strip() or DEFAULT_DASHSCOPE_MODEL
    base_url = (str(config.get("asr_base_url", "") or "").strip()
                or str(config.get("llm_base_url", "") or "").strip())
    endpoint = chat_endpoint(base_url, "openai")
    if not endpoint:
        raise RuntimeError("未配置 asr_base_url / llm_base_url，无法调用线上语音识别")
    api_key = str(config.get("llm_api_key", "") or "").strip()
    if not api_key:
        raise RuntimeError("未配置 llm_api_key，无法调用线上语音识别")
    raw = Path(audio).read_bytes()
    if len(raw) > DASHSCOPE_MAX_AUDIO_BYTES:
        raise RuntimeError(f"音频过大（{len(raw) / 1048576:.1f}MB），线上识别上限约 7MB")
    mime = AUDIO_MIMES.get(Path(audio).suffix.lower(), "audio/wav")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "input_audio",
             "input_audio": {"data": f"data:{mime};base64,"
                                     f"{base64.b64encode(raw).decode('ascii')}"}}]}],
        "stream": False,
    }
    if lang != "auto":
        payload["asr_options"] = {"language": lang, "enable_itn": False}
    async with httpx.AsyncClient(timeout=DASHSCOPE_TIMEOUT_SECONDS, trust_env=False,
                                 verify=verified_context()) as client:
        resp = await client.post(endpoint, json=payload,
                                 headers={"Authorization": f"Bearer {api_key}"})
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {str(resp.text)[:200]}")
    choices = resp.json().get("choices") or []
    if not choices:
        raise RuntimeError(f"返回内容异常：{str(resp.json())[:200]}")
    return str((choices[0].get("message") or {}).get("content") or "").strip()


async def transcribe_file(config, audio, lang=None) -> str:
    """识别单个音频文件，返回文字（识别不出来返回空串）。

    语种与引擎都取「语音识别」那几项配置；调用方（用户发来的语音）不留文件，
    所以本地通道也走临时目录那套批量脚本，只是只放一个文件进去。
    """
    path = Path(audio)
    use_lang = normalize_lang(lang if lang is not None
                              else config.get("asr_lang", "auto"))
    if normalize_engine(config.get("asr_engine", "local")) == "dashscope":
        return (await transcribe_dashscope(config, path, use_lang)).strip()
    key = str(path)
    texts = await asyncio.to_thread(transcribe_local, config, [(key, path)], use_lang)
    return str(texts.get(key, "") or "").strip()


class AsrJob:
    """一次批量识别任务：后台线程跑识别，逐条写回 <音频名>.txt。"""

    def __init__(self, config, targets: List[dict], lang: str, engine: str):
        self.id = uuid.uuid4().hex
        self.lang = lang
        self.engine = engine
        self.total = len(targets)
        self.done = 0
        self.stage = "准备中"
        self.running = True
        self.error = ""
        self.results: List[dict] = []
        self._logs = deque(maxlen=JOB_LOG_LINES)
        self._targets = targets
        self._config = config
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def log(self, line):
        line = str(line or "").strip()
        if line:
            with self._lock:
                self._logs.append(line)

    def snapshot(self) -> dict:
        with self._lock:
            logs = list(self._logs)
        return {"job_id": self.id, "running": self.running, "done": self.done,
                "total": self.total, "lang": self.lang, "engine": self.engine,
                "stage": self.stage, "error": self.error,
                "logs": logs, "results": list(self.results)}

    def _run(self):
        try:
            if self.engine == "dashscope":
                self.stage = f"线上识别 0/{self.total}"
                asyncio.run(self._run_dashscope())
            else:
                self.stage = f"本地模型识别中（{self.total} 个音频，首次使用需下载模型）"
                self.log("正在调用 GPT-SoVITS 自带 ASR，首次使用会下载模型，请耐心等待…")
                self._run_local()
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            self.log(f"识别失败：{self.error}")
        finally:
            self.running = False

    def _run_local(self):
        pairs = [(str(t["audio"]), Path(t["audio"])) for t in self._targets]
        texts = transcribe_local(self._config, pairs, self.lang, log=self.log)
        for target in self._targets:
            text = texts.get(str(target["audio"]), "")
            self._record(target, text, "" if text else "未识别出文字")
        self.done = self.total
        self.stage = f"完成 {self.done}/{self.total}"

    async def _run_dashscope(self):
        for target in self._targets:
            try:
                text = await transcribe_dashscope(self._config, Path(target["audio"]), self.lang)
                self._record(target, text, "" if text else "未识别出文字")
            except Exception as e:
                self._record(target, "", f"{type(e).__name__}: {e}")
            self.done += 1
            self.stage = f"线上识别 {self.done}/{self.total}"

    def _record(self, target, text: str, error: str):
        audio = Path(target["audio"])
        if text:
            try:
                audio.with_suffix(".txt").write_text(text, encoding="utf-8")
            except OSError as e:
                error = f"写入文字失败：{e}"
        self.results.append({"audio": str(audio), "name": audio.name,
                             "emotion": str(target.get("emotion", "") or ""),
                             "text": text, "error": error})
        if text:
            self.log(f"识别完成：{audio.name} → {text[:60]}")
        else:
            self.log(f"识别失败：{audio.name} {error}")


_JOBS: Dict[str, AsrJob] = {}
_JOBS_LOCK = threading.Lock()


def start_job(config, targets: List[dict], lang, engine) -> AsrJob:
    job = AsrJob(config, targets, normalize_lang(lang), normalize_engine(engine))
    with _JOBS_LOCK:
        _JOBS[job.id] = job
        while len(_JOBS) > JOBS_KEPT:
            _JOBS.pop(next(iter(_JOBS)), None)
    return job.start()


def get_job(job_id) -> Optional[AsrJob]:
    with _JOBS_LOCK:
        return _JOBS.get(str(job_id or ""))
