# -*- coding: utf-8 -*-
"""云 TTS 声音设计（自定义音色）回归测试。

覆盖：
  A 入参校验：缺地址/密钥/驱动模型、音色名非法、描述或预览文本为空
  B 创建音色：请求地址与请求体字段，返回 voice / target_model / 预览音频字节
  C 音色列表与删除：action 与字段
  D 上游报错：HTTP 非 2xx 时把上游 message 抛成 VoiceDesignError
  E 链路接入：路由存在、创建与删除在敏感接口清单里、前端入口齐全

运行: python tests/test_voice_design.py      （全通过退出码 0）
"""
import asyncio
import base64
import io
import json
import os
import struct
import sys
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
from modules.tts_voice_design import (DEFAULT_LANGUAGE, LANGUAGES,  # noqa: E402
                                      VoiceDesignError, create_voice, delete_voice,
                                      list_voices)

PASS, FAIL = [], []
HTML = (ROOT / "webui" / "start.html").read_text(encoding="utf-8")


def check(name, fn):
    try:
        ok = fn()
    except Exception as e:
        FAIL.append(f"{name} -> {type(e).__name__}: {e}")
        print(f"  [FAIL] {name} -> {type(e).__name__}: {e}")
        return
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


def section(title):
    print(f"\n{title}")


def _wav_bytes(seconds=0.5, rate=16000):
    frames = int(seconds * rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<%dh" % frames, *([800] * frames)))
    return buf.getvalue()


WAV = _wav_bytes()

POSTS = []
FAIL_MODE = [False]


class DesignHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            body = None
        POSTS.append({"path": self.path,
                      "auth": self.headers.get("Authorization", ""),
                      "body": body})
        if FAIL_MODE[0]:
            self._send(400, json.dumps({"code": "InvalidParameter",
                                        "message": "voice_prompt is too long"}).encode(),
                       "application/json")
            return
        action = ((body or {}).get("input") or {}).get("action")
        if action == "create":
            src = body["input"]
            out = {"voice": src["preferred_name"], "target_model": src["target_model"],
                   "preview_audio": {"data": base64.b64encode(WAV).decode(),
                                     "sample_rate": 24000, "response_format": "wav"}}
        elif action == "list":
            out = {"page_index": 0, "page_size": 50, "total_count": 1,
                   "voice_list": [{"voice": "narrator", "language": "zh",
                                   "target_model": "qwen3-tts-vd-2026-01-26",
                                   "voice_prompt": "沉稳的中年男性", "preview_text": "各位好"}]}
        else:
            out = {"voice": (body.get("input") or {}).get("voice")}
        self._send(200, json.dumps({"output": out, "usage": {"count": 1}}).encode(),
                   "application/json")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _cfg(port, **over):
    cfg = {"cloud_tts_base_url": f"http://127.0.0.1:{port}/api/v1",
           "cloud_tts_api_key": "sk-design",
           "cloud_tts_model": "qwen3-tts-vd-2026-01-26",
           "timeout_seconds": 10}
    cfg.update(over)
    return cfg


def _err_message(coro_fn):
    try:
        asyncio.run(coro_fn())
    except VoiceDesignError as e:
        return str(e)
    return ""


def main() -> int:
    print("=" * 70)
    print("云 TTS 声音设计：创建 / 列表 / 删除自定义音色")
    print("=" * 70)

    section("A 入参校验")
    check("缺服务地址时报错",
          lambda: "服务地址" in _err_message(
              lambda: create_voice({"cloud_tts_model": "m"}, voice_prompt="p",
                                   preview_text="t", name="n")))
    check("缺 API Key 时报错",
          lambda: "API Key" in _err_message(
              lambda: create_voice({"cloud_tts_base_url": "http://x",
                                    "cloud_tts_model": "m"}, voice_prompt="p",
                                   preview_text="t", name="n")))
    check("缺驱动模型时报错",
          lambda: "云 TTS 模型" in _err_message(
              lambda: create_voice({"cloud_tts_base_url": "http://x",
                                    "cloud_tts_api_key": "k"},
                                   voice_prompt="p", preview_text="t", name="n")))
    check("音色名含非法字符时报错",
          lambda: "音色名称" in _err_message(
              lambda: create_voice({"cloud_tts_base_url": "http://x",
                                    "cloud_tts_api_key": "k",
                                    "cloud_tts_model": "m"},
                                   voice_prompt="p", preview_text="t", name="带中文")))
    check("音色名超过 16 位时报错",
          lambda: "音色名称" in _err_message(
              lambda: create_voice({"cloud_tts_base_url": "http://x",
                                    "cloud_tts_api_key": "k",
                                    "cloud_tts_model": "m"},
                                   voice_prompt="p", preview_text="t", name="a" * 17)))
    check("描述为空时报错",
          lambda: "声音描述" in _err_message(
              lambda: create_voice({"cloud_tts_base_url": "http://x",
                                    "cloud_tts_api_key": "k",
                                    "cloud_tts_model": "m"},
                                   voice_prompt="  ", preview_text="t", name="n")))
    check("预览文本为空时报错",
          lambda: "预览文本" in _err_message(
              lambda: create_voice({"cloud_tts_base_url": "http://x",
                                    "cloud_tts_api_key": "k",
                                    "cloud_tts_model": "m"},
                                   voice_prompt="p", preview_text="", name="n")))
    check("支持的语言表含中英日",
          lambda: {"zh", "en", "ja"} <= set(LANGUAGES) and DEFAULT_LANGUAGE in LANGUAGES)

    srv = HTTPServer(("127.0.0.1", 0), DesignHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        section("B 创建音色")
        POSTS.clear()
        FAIL_MODE[0] = False
        got = asyncio.run(create_voice(_cfg(port), voice_prompt="沉稳的中年男性播音员",
                                       preview_text="各位听众朋友们大家好",
                                       name="announcer", language="zh"))
        req = POSTS[-1] if POSTS else {}
        body = req.get("body") or {}
        check("请求打到百炼的音色定制接口",
              lambda: req.get("path") == "/api/v1/services/audio/tts/customization")
        check("鉴权头带上云 TTS 的密钥",
              lambda: req.get("auth") == "Bearer sk-design")
        check("用 qwen-voice-design 的 create 动作",
              lambda: body.get("model") == "qwen-voice-design"
              and (body.get("input") or {}).get("action") == "create")
        check("驱动模型默认取「云 TTS 模型」",
              lambda: (body.get("input") or {}).get("target_model")
              == "qwen3-tts-vd-2026-01-26")
        check("描述 / 预览文本 / 名称 / 语言都带上",
              lambda: (body.get("input") or {}).get("voice_prompt") == "沉稳的中年男性播音员"
              and (body.get("input") or {}).get("preview_text") == "各位听众朋友们大家好"
              and (body.get("input") or {}).get("preferred_name") == "announcer"
              and (body.get("input") or {}).get("language") == "zh")
        check("返回 voice 与驱动模型",
              lambda: got.get("voice") == "announcer"
              and got.get("target_model") == "qwen3-tts-vd-2026-01-26")
        check("预览音频解成 wav 字节", lambda: got.get("preview_audio") == WAV)
        # 预览音频来自上游响应：异常大的 Base64 不该被整段解码进内存
        from modules.tts_voice_design import MAX_PREVIEW_BASE64_CHARS, _preview_bytes
        check("超大预览音频被丢弃而不是解码",
              lambda: _preview_bytes({"preview_audio": {
                  "data": "A" * (MAX_PREVIEW_BASE64_CHARS + 4)}}) == b"")
        check("正常大小的预览照常解码",
              lambda: _preview_bytes({"preview_audio": {
                  "data": base64.b64encode(WAV).decode()}}) == WAV)
        POSTS.clear()
        asyncio.run(create_voice(_cfg(port), voice_prompt="p", preview_text="t",
                                 name="n", language=""))
        check("语言留空时回落默认语言",
              lambda: (POSTS[-1]["body"]["input"] or {}).get("language") == DEFAULT_LANGUAGE)

        section("C 音色列表与删除")
        POSTS.clear()
        voices = asyncio.run(list_voices(_cfg(port)))
        check("列表用 list 动作且不带 create 的字段",
              lambda: (POSTS[-1]["body"].get("input") or {}).get("action") == "list"
              and "voice_prompt" not in (POSTS[-1]["body"].get("input") or {}))
        check("返回 voice_list 条目",
              lambda: len(voices) == 1 and voices[0].get("voice") == "narrator")
        POSTS.clear()
        removed = asyncio.run(delete_voice(_cfg(port), "narrator"))
        check("删除用 delete 动作并回传音色名",
              lambda: (POSTS[-1]["body"].get("input") or {}).get("action") == "delete"
              and (POSTS[-1]["body"].get("input") or {}).get("voice") == "narrator"
              and removed == "narrator")

        section("D 上游报错")
        FAIL_MODE[0] = True
        check("HTTP 400 时把上游 message 抛出来",
              lambda: _err_message(lambda: create_voice(_cfg(port), voice_prompt="p",
                                                        preview_text="t", name="n"))
              == "voice_prompt is too long")
        FAIL_MODE[0] = False
    finally:
        srv.shutdown()

    section("E 链路接入")
    check("三个接口都注册了路由",
          lambda: all(hasattr(M.WebUIServer, n) for n in
                      ("handle_voice_design_list", "handle_voice_design_create",
                       "handle_voice_design_delete")))
    check("创建与删除在敏感接口清单里（列表只读，不拦）",
          lambda: M._needs_second_password("/api/tts/voice_design/create")
          and M._needs_second_password("/api/tts/voice_design/delete")
          and not M._needs_second_password("/api/tts/voice_design/list"))
    check("前端有声音设计入口与弹窗",
          lambda: all(k in HTML for k in
                      ("voice-design-modal", "voiceDesigner", "makeVoiceDesignButton",
                       "api/tts/voice_design/list", "api/tts/voice_design/create",
                       "api/tts/voice_design/delete")))
    check("音色字段带声音设计按钮标记",
          lambda: "'cloud_tts_voice'" in HTML and "voiceDesigner: true" in HTML)
    check("弹窗里的 DOM id 都能在 HTML 里找到",
          lambda: all(f'id="{i}"' in HTML for i in
                      ("voice-design-modal", "voice-design-close", "vd-prompt", "vd-preview",
                       "vd-name", "vd-lang", "vd-create", "vd-refresh", "vd-msg",
                       "vd-preview-box", "vd-preview-audio", "vd-list")))

    print("\n" + "=" * 70)
    print(f"结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
    print("=" * 70)
    if FAIL:
        for name in FAIL:
            print(f"  FAIL: {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
