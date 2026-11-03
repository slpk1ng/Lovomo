# -*- coding: utf-8 -*-
import asyncio
import base64
import os
import re
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
FUNASR_LANGS = {"zh"}
LOCAL_SCRIPTS = {"funasr": "tools/asr/funasr_asr.py",
                 "whisper": "tools/asr/fasterwhisper_asr.py"}
WHISPER_MODEL_SIZES = ("medium", "medium.en", "large-v2", "large-v3", "large-v3-turbo")
DEFAULT_WHISPER_SIZE = "medium"
SCRIPT_BOOTSTRAP = (
    "import runpy, sys, os\n"
    "sys.path.insert(0, sys.argv.pop(1))\n"
    "if os.environ.get('LOVOMO_ASR_OFFLINE'):\n"
    "    try:\n"
    "        import modelscope as _ms\n"
    "        _sd = _ms.snapshot_download\n"
    "        def _local_snapshot(*a, **k):\n"
    "            d = k.get('local_dir') or (a[1] if len(a) > 1 else '')\n"
    "            if d and os.path.isfile(os.path.join(d, 'model.pt')):\n"
    "                return d\n"
    "            return _sd(*a, **k)\n"
    "        _ms.snapshot_download = _local_snapshot\n"
    "    except Exception:\n"
    "        pass\n"
    "script = sys.argv.pop(1)\n"
    "sys.argv[0] = script\n"
    "runpy.run_path(script, run_name='__main__')\n"
)
DEFAULT_DASHSCOPE_MODEL = "qwen3-asr-flash"
DEFAULT_WHISPER_API_MODEL = "whisper-1"
_VERSION_SEG_RE = re.compile(r"/v\d+(?:\.\d+)*$")
WHISPER_URL_HINTS = ("api.openai.com", "api.groq.com", "api.siliconflow.cn",
                     "/audio/transcriptions", "speaches")
WHISPER_MODEL_HINTS = ("whisper", "sensevoice", "transcribe")
WHISPER_API_MAX_AUDIO_BYTES = 24 * 1024 * 1024
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


def asr_api_key(config) -> str:
    return (str(config.get("asr_api_key", "") or "").strip()
            or str(config.get("llm_api_key", "") or "").strip())


def asr_model_name(config, style=None) -> str:
    value = (str(config.get("asr_model", "") or "").strip()
             or str(config.get("asr_dashscope_model", "") or "").strip())
    if value:
        return value
    use_style = style if style is not None else config.get("asr_api_style", "auto")
    if normalize_api_style(use_style) == "whisper":
        return DEFAULT_WHISPER_API_MODEL
    return DEFAULT_DASHSCOPE_MODEL


def normalize_api_style(style) -> str:
    """识别接口风格：auto / openai / dashscope / ollama / whisper。"""
    value = str(style or "").strip().lower()
    return value if value in ("auto", "openai", "dashscope", "ollama", "whisper") else "auto"


def looks_like_transcription_service(config) -> bool:
    base = (str(config.get("asr_base_url", "") or "").strip()
            or str(config.get("llm_base_url", "") or "").strip()).lower()
    model = (str(config.get("asr_model", "") or "").strip()
             or str(config.get("asr_dashscope_model", "") or "").strip()).lower()
    if any(hint in base for hint in WHISPER_URL_HINTS):
        return True
    return any(hint in model for hint in WHISPER_MODEL_HINTS)


def is_whisper_style(config) -> bool:
    style = normalize_api_style(config.get("asr_api_style", "auto"))
    if style == "whisper":
        return True
    if style == "auto":
        return looks_like_transcription_service(config)
    return False


def transcriptions_endpoint(base_url: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.lower().endswith("/audio/transcriptions"):
        return base
    normalized = chat_endpoint(base, "openai")
    for suffix in ("/chat/completions", "/completions"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    # 服务根地址没带版本段就补一个
    if not _VERSION_SEG_RE.search(normalized):
        normalized = f"{normalized}/v1"
    return f"{normalized}/audio/transcriptions"


def api_style_backend(style) -> str:
    return "ollama" if normalize_api_style(style) == "ollama" else "openai"


def local_whisper_size(config) -> str:
    name = str(config.get("asr_model", "") or "").strip()
    if name in WHISPER_MODEL_SIZES:
        return name
    return whisper_model_size(config)


def local_asr_root(config) -> Optional[Path]:
    raw = str(config.get("asr_local_root", "") or "").strip()
    if raw:
        path = Path(raw)
        if path.is_dir():
            return path
        if path.is_file():
            return path.parent
    return resolve_sovits_root(config)


def resolve_sovits_root(config) -> Optional[Path]:
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


LOCAL_DEVICE_MODES = ("cpu", "auto", "gpu")


def local_device_mode(config) -> str:
    mode = str(config.get("asr_local_device", "") or "").strip().lower()
    return mode if mode in LOCAL_DEVICE_MODES else "cpu"


def _asr_env(force_cpu: bool, offline: bool) -> dict:
    env = dict(os.environ)
    if force_cpu:
        env["CUDA_VISIBLE_DEVICES"] = ""
    else:
        env.pop("CUDA_VISIBLE_DEVICES", None)
    if offline:
        env["LOVOMO_ASR_OFFLINE"] = "1"
    else:
        env.pop("LOVOMO_ASR_OFFLINE", None)
    return env


def transcribe_local(config, targets: List[Tuple[str, Path]], lang: str,
                     log=None) -> Dict[str, str]:
    root = local_asr_root(config)
    if root is None:
        raise RuntimeError("未配置「识别根目录」/tts_start_script，无法定位本地 ASR 目录")
    python_exe = _runtime_python(root)
    if python_exe is None:
        raise RuntimeError(f"找不到 {root / 'runtime'} 下的 python，无法调用本地 ASR")
    local_engine = str(config.get("asr_local_engine", "") or "").strip().lower()
    if local_engine == "funasr":
        script, script_lang = LOCAL_SCRIPTS["funasr"], (lang if lang in FUNASR_LANGS else "zh")
    elif local_engine == "whisper":
        script, script_lang = LOCAL_SCRIPTS["whisper"], lang
    elif lang in FUNASR_LANGS:
        script, script_lang = LOCAL_SCRIPTS["funasr"], lang
    else:
        script, script_lang = LOCAL_SCRIPTS["whisper"], lang
    script_path = root / script
    if not script_path.exists():
        raise RuntimeError(f"找不到 ASR 脚本：{script_path}")

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
            cmd += ["-s", local_whisper_size(config)]

        def run_once(env):
            proc = subprocess.Popen(cmd, cwd=str(root), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    encoding="utf-8", errors="replace", env=env,
                                    **no_window_kwargs())
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
            return proc.returncode, tail

        mode = local_device_mode(config)
        plans = [(True, True)] if mode == "cpu" \
            else ([(False, True), (True, True)] if mode == "auto" else [(False, True)])
        plans.append((plans[-1][0], False))
        for index, (force_cpu, offline) in enumerate(plans):
            if index:
                for stale in tmp_dir.glob("*.list"):
                    try:
                        stale.unlink()
                    except OSError:
                        pass
                if log:
                    log("本地识别上一遍没出结果，换一套参数重跑"
                        + ("（放开模型仓库核对）" if not offline else "（改用 CPU）"))
            returncode, tail = run_once(_asr_env(force_cpu, offline))
            results, rows, empty_rows, found = _collect_asr_results(tmp_dir, mapping)
            if results:
                return results
            detail = " / ".join(x for x in tail if x.strip())[:400]
            if found and (not rows or empty_rows == rows):
                if log:
                    log(f"识别脚本没有给出文字（退出码 {returncode}）："
                        + (detail or "结果文件为空"))
                return {}
            if rows:
                raise RuntimeError(
                    f"ASR 脚本识别出了 {rows} 行结果但都没对上输入文件（退出码 "
                    f"{returncode}）")
            failure = RuntimeError(f"ASR 脚本没有产出结果（退出码 {returncode}）"
                                   + (f"：{detail}" if detail else ""))
            if index == len(plans) - 1:
                raise failure
        return results
    finally:
        _cleanup_tmp_dir(tmp_dir)


def _collect_asr_results(tmp_dir: Path, mapping: Dict[str, str]) -> Tuple[dict, int, int, bool]:
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
    shutil.rmtree(tmp_dir, ignore_errors=True)
    if not tmp_dir.exists():
        return
    time.sleep(0.2)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    if tmp_dir.exists():
        print(f"ASR 临时目录未能删除（可能被杀软占用），已留下：{tmp_dir}")


def _read_list_file(tmp_dir: Path) -> str:
    target = tmp_dir / f"{tmp_dir.name}.list"
    try:
        return target.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


async def transcribe_dashscope(config, audio: Path, lang: str) -> str:
    model = asr_model_name(config)
    base_url = (str(config.get("asr_base_url", "") or "").strip()
                or str(config.get("llm_base_url", "") or "").strip())
    endpoint = chat_endpoint(base_url, api_style_backend(config.get("asr_api_style", "auto")))
    if not endpoint:
        raise RuntimeError("未配置「识别服务地址」/llm_base_url，无法调用线上语音识别")
    api_key = asr_api_key(config)
    if not api_key:
        raise RuntimeError("未配置「识别 API Key」/llm_api_key，无法调用线上语音识别")
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


async def transcribe_whisper_api(config, audio: Path, lang: str) -> str:
    base_url = (str(config.get("asr_base_url", "") or "").strip()
                or str(config.get("llm_base_url", "") or "").strip())
    endpoint = transcriptions_endpoint(base_url)
    if not endpoint:
        raise RuntimeError("未配置「识别服务地址」/llm_base_url，无法调用线上语音识别")
    api_key = asr_api_key(config)
    raw = Path(audio).read_bytes()
    if len(raw) > WHISPER_API_MAX_AUDIO_BYTES:
        raise RuntimeError(f"音频过大（{len(raw) / 1048576:.1f}MB），标准识别接口上限约 24MB")
    mime = AUDIO_MIMES.get(Path(audio).suffix.lower(), "audio/wav")
    data = {"model": asr_model_name(config, "whisper")}
    if lang != "auto":
        data["language"] = lang
    files = {"file": (Path(audio).name or "audio.wav", raw, mime)}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    async with httpx.AsyncClient(timeout=DASHSCOPE_TIMEOUT_SECONDS, trust_env=False,
                                 verify=verified_context()) as client:
        resp = await client.post(endpoint, data=data, files=files, headers=headers)
    if resp.status_code in (401, 403) and not api_key:
        raise RuntimeError("识别服务拒绝了请求：本机识别服务可以留空密钥，云端要在「识别 API Key」里填密钥")
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {str(resp.text)[:200]}")
    payload = resp.json()
    if isinstance(payload, dict):
        text = payload.get("text")
        if text is None:
            # 有的服务（whisper.cpp server 的 verbose_json）只给分段
            segments = payload.get("segments") or []
            text = "".join(str(seg.get("text") or "")
                           for seg in segments if isinstance(seg, dict))
        return str(text or "").strip()
    return str(payload or "").strip()


async def transcribe_online(config, audio: Path, lang: str) -> str:
    if is_whisper_style(config):
        return await transcribe_whisper_api(config, audio, lang)
    return await transcribe_dashscope(config, audio, lang)


async def transcribe_file(config, audio, lang=None) -> str:
    path = Path(audio)
    use_lang = normalize_lang(lang if lang is not None
                              else config.get("asr_lang", "auto"))
    if normalize_engine(config.get("asr_engine", "local")) == "dashscope":
        return (await transcribe_online(config, path, use_lang)).strip()
    key = str(path)
    texts = await asyncio.to_thread(transcribe_local, config, [(key, path)], use_lang)
    return str(texts.get(key, "") or "").strip()


class AsrJob:

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
                text = await transcribe_online(self._config, Path(target["audio"]), self.lang)
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
