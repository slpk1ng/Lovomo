# -*- coding: utf-8 -*-
"""云端 TTS（OpenAI 兼容 /audio/speech 与阿里云百炼）回归测试。

覆盖：
  A 配置解析：tts_backend 判定、音色映射解析、情绪→音色回退
  B 情绪清单：云端模式下由「默认情绪 + 映射表」推导
  C OpenAI 协议：请求地址/鉴权/请求体（字段白名单），wav 落盘并返回
  D 百炼协议：Base64 与 URL 下载两条取音频路径，language_type 随台词语言，
    请求体不含情绪/参考音频等 GPT-SoVITS 参数；URL 下载偶发失败会重试
  E 失败处理：配置缺失、HTTP 报错、非音频内容一律返回 None（调用方只发文本）
  F 链路接入：synthesize_sentence 在云端模式分流、不要求参考音频；
    check_tts_service 在云端模式不再探测本地端口
  G 配置项三处同步与密钥加密

运行: python tests/test_cloud_tts.py      （全通过退出码 0）
"""
import asyncio
import base64
import contextlib
import io
import json
import math
import os
import struct
import sys
import tempfile
import threading
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
sys.stdin = io.StringIO()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import main as M  # noqa: E402
import modules.tts as T  # noqa: E402
from modules.tts_cloud import (DOWNLOAD_ATTEMPTS, cloud_emotion_names,  # noqa: E402
                               is_cloud_tts, parse_voice_map, resolve_voice,
                               synthesize_cloud)
from modules.tts_service import check_tts_service  # noqa: E402

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


def section(title):
    print(f"\n{title}")


def _wav_bytes(seconds=1.0, rate=16000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(6000 * math.sin(2 * math.pi * 220 * i / rate)))
            for i in range(int(rate * seconds)))
        wf.writeframes(frames)
    return buf.getvalue()


WAV = _wav_bytes()


class _Recorder:
    def __init__(self):
        self.posts = []
        self.mode = "raw"       # raw / base64 / url / error / notaudio
        self.base = ""
        self.download_hits = 0      # 音频地址被下载了几次
        self.download_failures = 0  # 前几次下载直接断连
        self.download_status = 0    # 非 0 时按这个状态码回错误


REC = _Recorder()


class CloudHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = None
        REC.posts.append({"path": self.path,
                          "auth": self.headers.get("Authorization", ""),
                          "body": body})
        if REC.mode == "error":
            self._send(401, b'{"error":"bad key"}', "application/json")
        elif REC.mode == "notaudio":
            self._send(200, b"<html>upstream error page</html>", "text/html")
        elif self.path.endswith("/audio/speech"):
            self._send(200, WAV, "audio/wav")
        else:
            audio = {"data": base64.b64encode(WAV).decode(), "url": ""} \
                if REC.mode == "base64" else {"data": "", "url": f"{REC.base}/audio.wav"}
            self._send(200, json.dumps({"output": {"audio": audio}}).encode(),
                       "application/json")

    def do_GET(self):
        if self.path == "/audio.wav":
            REC.download_hits += 1
            if REC.download_failures > 0:
                REC.download_failures -= 1
                self.close_connection = True
                return
            if REC.download_status:
                self._send(REC.download_status, b"nope", "text/plain")
            else:
                self._send(200, WAV, "audio/wav")
        else:
            self._send(404, b"nope", "text/plain")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _cloud_cfg(port, **over):
    cfg = {"tts_backend": "cloud",
           "cloud_tts_protocol": "openai",
           "cloud_tts_base_url": f"http://127.0.0.1:{port}/v1",
           "cloud_tts_api_key": "sk-cloud",
           "cloud_tts_model": "tts-1",
           "cloud_tts_voice": "alloy",
           "timeout_seconds": 10,
           "tts_auto_lang": False,
           "text_lang": "zh"}
    cfg.update(over)
    return cfg


def _synth(cfg, text, emotion, tmp):
    return asyncio.run(synthesize_cloud(cfg, text, emotion, tmp))


def main() -> int:
    print("=" * 70)
    print("云端 TTS：OpenAI 兼容 /audio/speech 与阿里云百炼")
    print("=" * 70)

    section("A 配置解析")
    check("tts_backend 缺省与 local 都判定为本地",
          lambda: not is_cloud_tts({}) and not is_cloud_tts({"tts_backend": "local"})
          and not is_cloud_tts({"tts_backend": "LOCAL"}))
    check("tts_backend=cloud 判定为云端", lambda: is_cloud_tts({"tts_backend": "cloud"}))
    check("音色映射支持 = : ：三种分隔，忽略空行与 # 注释",
          lambda: parse_voice_map("pingjing = Cherry\n# 注释\n\ngaoxing:Chelsie\n"
                                  "shy：Ethan\n这行没有分隔符")
          == {"pingjing": "Cherry", "gaoxing": "Chelsie", "shy": "Ethan"})
    check("音色值里的冒号不会被当成分隔符（maxsplit=1）",
          lambda: parse_voice_map("pingjing=FunAudioLLM/CosyVoice2-0.5B:alex")
          == {"pingjing": "FunAudioLLM/CosyVoice2-0.5B:alex"})
    check("映射命中的情绪用自己的音色",
          lambda: resolve_voice({"cloud_tts_voice": "Ethan",
                                 "cloud_tts_voice_map": "pingjing=Cherry"}, "pingjing")
          == "Cherry")
    check("映射没写的情绪回退全局默认音色",
          lambda: resolve_voice({"cloud_tts_voice": "Ethan",
                                 "cloud_tts_voice_map": "pingjing=Cherry"}, "gaoxing")
          == "Ethan")
    check("既没有映射也没有默认音色时返回空串",
          lambda: resolve_voice({"cloud_tts_voice_map": "pingjing=Cherry"}, "gaoxing") == "")

    section("B 云端情绪清单")
    check("默认情绪排在最前且与映射表去重",
          lambda: cloud_emotion_names({"default_voice": "pingjing",
                                       "cloud_tts_voice_map":
                                           "pingjing=Cherry\ngaoxing=Chelsie"})
          == ["pingjing", "gaoxing"])
    check("没有映射表时只剩默认情绪",
          lambda: cloud_emotion_names({"default_voice": "pingjing"}) == ["pingjing"])

    srv = HTTPServer(("127.0.0.1", 0), CloudHandler)
    REC.base = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    tmp = Path(tempfile.mkdtemp(prefix="lovomo_cloud_tts_"))
    try:
        section("C OpenAI 兼容协议")
        REC.posts.clear()
        REC.mode = "raw"
        got = _synth(_cloud_cfg(port), "你好呀", "pingjing", tmp)
        check("返回可播放的 wav 文件", lambda: got is not None and got.exists()
              and T._wav_duration(got) > 0)
        req = REC.posts[-1] if REC.posts else {}
        check("请求打到 {base_url}/audio/speech",
              lambda: req.get("path") == "/v1/audio/speech")
        check("鉴权头带上云 TTS 的密钥",
              lambda: req.get("auth") == "Bearer sk-cloud")
        check("请求体带 model/input/voice 且要 wav",
              lambda: (req.get("body") or {}).get("model") == "tts-1"
              and (req.get("body") or {}).get("input") == "你好呀"
              and (req.get("body") or {}).get("voice") == "alloy"
              and (req.get("body") or {}).get("response_format") == "wav")
        check("OpenAI 请求体只有云端字段，不带 GPT-SoVITS 参数",
              lambda: set(req.get("body") or {})
              == {"model", "input", "voice", "response_format"})
        REC.posts.clear()
        _synth(_cloud_cfg(port, cloud_tts_voice_map="pingjing=Cherry"), "你好呀",
               "pingjing", tmp)
        check("每情绪映射覆盖默认音色",
              lambda: (REC.posts[-1]["body"] or {}).get("voice") == "Cherry")
        REC.posts.clear()
        _synth(_cloud_cfg(port, cloud_tts_api_key=""), "你好呀", "pingjing", tmp)
        check("没填密钥时不发 Authorization 头", lambda: REC.posts[-1]["auth"] == "")

        section("D 阿里云百炼协议")
        REC.posts.clear()
        REC.mode = "base64"
        dcfg = _cloud_cfg(port, cloud_tts_protocol="dashscope",
                          cloud_tts_base_url=f"http://127.0.0.1:{port}/api/v1",
                          cloud_tts_model="qwen3-tts-flash", cloud_tts_voice="Cherry",
                          tts_auto_lang=True)
        got = _synth(dcfg, "你好呀", "pingjing", tmp)
        check("Base64 音频能落成 wav", lambda: got is not None and T._wav_duration(got) > 0)
        req = REC.posts[-1] if REC.posts else {}
        check("请求打到百炼的多模态生成接口",
              lambda: req.get("path") == "/api/v1/services/aigc/multimodal-generation/generation")
        check("请求体用 input.text / input.voice 结构",
              lambda: (req.get("body") or {}).get("input", {}).get("text") == "你好呀"
              and (req.get("body") or {}).get("input", {}).get("voice") == "Cherry")
        check("中文台词带上 language_type=Chinese",
              lambda: (req.get("body") or {}).get("input", {}).get("language_type") == "Chinese")
        check("百炼请求体只有云端字段，不带情绪/参考音频等本地参数",
              lambda: set(req.get("body") or {}) == {"model", "input"}
              and set((req.get("body") or {}).get("input") or {})
              <= {"text", "voice", "language_type"})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _synth(_cloud_cfg(port, cloud_tts_protocol="dashscope",
                              cloud_tts_base_url=f"http://127.0.0.1:{port}/api/v1",
                              cloud_tts_model="qwen3-tts-flash", cloud_tts_voice="Cherry",
                              tts_debug_log=True), "你好呀", "pingjing", tmp)
        logged = buf.getvalue()
        check("云端调试日志不打印 GPT-SoVITS 专属参数",
              lambda: "TTS 详细参数(云)" in logged and "参考音频" not in logged
              and "text_lang" not in logged and "speed=" not in logged
              and "split=" not in logged)
        REC.mode = "url"
        got = _synth(dcfg, "你好呀", "pingjing", tmp)
        check("只给 URL 时再下载一次音频",
              lambda: got is not None and T._wav_duration(got) > 0)

        REC.download_hits = 0
        REC.download_failures = 2
        got = _synth(dcfg, "你好呀", "pingjing", tmp)
        check("下载音频偶发断连会重试到成功",
              lambda: got is not None and T._wav_duration(got) > 0
              and REC.download_hits == 3)
        REC.download_hits = 0
        REC.download_failures = 99
        check("下载一直失败时重试到上限才放弃",
              lambda: _synth(dcfg, "你好呀", "pingjing", tmp) is None
              and REC.download_hits == DOWNLOAD_ATTEMPTS)
        REC.download_hits = 0
        REC.download_failures = 0
        REC.download_status = 404
        check("下载返回 4xx 不重试",
              lambda: _synth(dcfg, "你好呀", "pingjing", tmp) is None
              and REC.download_hits == 1)
        REC.download_status = 0
        REC.download_hits = 0
        REC.download_failures = 1
        lines = [_synth(dcfg, f"第{i}句", "pingjing", tmp) for i in range(5)]
        check("连续多句合成时偶发下载失败不会丢句",
              lambda: all(x is not None and T._wav_duration(x) > 0 for x in lines))

        section("E 失败处理")
        REC.posts.clear()
        check("缺服务地址直接返回 None 且不发请求",
              lambda: _synth(_cloud_cfg(port, cloud_tts_base_url=""), "你好呀",
                             "pingjing", tmp) is None and not REC.posts)
        check("缺模型直接返回 None 且不发请求",
              lambda: _synth(_cloud_cfg(port, cloud_tts_model=""), "你好呀",
                             "pingjing", tmp) is None and not REC.posts)
        check("缺音色直接返回 None 且不发请求",
              lambda: _synth(_cloud_cfg(port, cloud_tts_voice=""), "你好呀",
                             "pingjing", tmp) is None and not REC.posts)
        REC.mode = "error"
        check("上游报错返回 None（调用方只发文本）",
              lambda: _synth(_cloud_cfg(port), "你好呀", "pingjing", tmp) is None)
        REC.mode = "notaudio"
        check("返回的不是音频时丢弃", lambda: _synth(_cloud_cfg(port), "你好呀",
                                                     "pingjing", tmp) is None)
        REC.mode = "raw"
        check("纯标点句不合成", lambda: _synth(_cloud_cfg(port), "。。。", "pingjing",
                                               tmp) is None)

        section("F 链路接入")
        check("云端模式下 synthesize_sentence 分流到云合成（情绪表为空也能合成）",
              lambda: T.synthesize_sentence(_cloud_cfg(port), "你好呀", "pingjing", {},
                                            tmp) is not None)
        check("云端模式下 check_tts_service 直接判为就绪（不探测本地端口）",
              lambda: asyncio.run(check_tts_service({"tts_backend": "cloud"})) is True)
        check("本地模式下 check_tts_service 仍会探测本地服务",
              lambda: asyncio.run(check_tts_service(
                  {"tts_backend": "local", "client_base_url": "http://127.0.0.1:1"})) is False)
        check("云端模式下情绪管理器由映射表推导情绪清单",
              lambda: "pingjing" in M.EmotionManager(
                  {"tts_backend": "cloud", "default_voice": "pingjing",
                   "ref_audio_root": str(tmp), "cloud_tts_voice_map": "gaoxing=Cherry"}
              ).emotions)
    finally:
        srv.shutdown()

    section("G 配置项与密钥")
    d = M.ConfigLoader.default_config()
    cloud_keys = ("tts_backend", "cloud_tts_base_url", "cloud_tts_protocol",
                  "cloud_tts_api_key", "cloud_tts_model", "cloud_tts_voice",
                  "cloud_tts_voice_map")
    check("七个新配置项都在 default_config() 里",
          lambda: all(k in d for k in cloud_keys))
    check("默认仍是本地 GPT-SoVITS", lambda: d["tts_backend"] == "local")
    check("WebUI 三处都声明了云 TTS 配置项",
          lambda: all(f"'{k}'" in HTML for k in cloud_keys)
          and "'cloud_tts_voice_map'" in HTML and "const CLOUD_TTS_PROVIDERS" in HTML)
    check("云 TTS 密钥走加密链路", lambda: _encrypt_roundtrip())
    check("云端字段只在 tts_backend=cloud 时显示",
          lambda: "condition: { tts_backend: 'cloud' }" in HTML)
    check("本地 GPT-SoVITS 字段在云端模式下隐藏",
          lambda: "condition: { hide_gsv_options: false, tts_backend: 'local' }" in HTML)

    print("\n" + "=" * 70)
    print(f"结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print("失败明细:")
        for name in FAIL:
            print(f"  - {name}")
    print("=" * 70)
    return 1 if FAIL else 0


def _encrypt_roundtrip():
    payload = {"cloud_tts_api_key": "sk-cloud-secret"}
    M._encrypt_api_keys(payload)
    stored = payload["cloud_tts_api_key"]
    return stored != "sk-cloud-secret" and M._decrypt_value(stored) == "sk-cloud-secret"


if __name__ == "__main__":
    sys.exit(main())
