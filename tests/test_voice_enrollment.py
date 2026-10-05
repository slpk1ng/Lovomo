# -*- coding: utf-8 -*-
"""云 TTS 声音复刻（用上传的音频克隆音色）回归测试。

覆盖：
  A 音频处理：无 ffmpeg 时单文件原样提交、多文件报错、非法后缀、空文件、空列表、
    超过 10MB、有 ffmpeg 时多文件合并成单声道 24kHz WAV、超过 60 秒
  B 创建音色：请求地址与请求体字段，audio 走 data URI，text 可选，返回 voice
  C 音色列表与删除：action 与字段
  D 上游报错：HTTP 非 2xx 时把上游 message 抛成 VoiceEnrollmentError
  E HTTP 处理函数：前端 payload 的 base64 音频能解回并透传，失败回 400
  F 链路接入：路由存在、创建与删除在敏感接口清单里、前端入口齐全

运行: python tests/test_voice_enrollment.py      （全通过退出码 0）
"""
import asyncio
import base64
import io
import json
import math
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
import modules.tts_voice_enrollment as E  # noqa: E402

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


def _wav_bytes(seconds=1.0, rate=44100):
    frames = int(seconds * rate)
    data = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                    for i in range(frames))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data)
    return buf.getvalue()


WAV = _wav_bytes()

POSTS = []
FAIL_MODE = [False]


class EnrollHandler(BaseHTTPRequestHandler):
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
                                        "message": "audio duration exceeds limit"}).encode(),
                       "application/json")
            return
        src = (body or {}).get("input") or {}
        action = src.get("action")
        if action == "create":
            out = {"voice": src.get("preferred_name"),
                   "target_model": src.get("target_model")}
        elif action == "list":
            out = {"page_index": 0, "page_size": 50, "total_count": 1,
                   "voice_list": [{"voice": "guanyu", "gmt_create": "2026-01-22 10:00:00",
                                   "target_model": "qwen3-tts-vc-2026-01-22"}]}
        else:
            out = {"voice": src.get("voice")}
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
           "cloud_tts_api_key": "sk-enroll",
           "cloud_tts_model": "qwen3-tts-vc-2026-01-22",
           "timeout_seconds": 10}
    cfg.update(over)
    return cfg


def _err_message(coro_fn):
    try:
        asyncio.run(coro_fn())
    except E.VoiceEnrollmentError as e:
        return str(e)
    return ""


def _data_uri_audio(body):
    data = ((body.get("input") or {}).get("audio") or {}).get("data") or ""
    head, _, payload = data.partition(",")
    try:
        return head, base64.b64decode(payload)
    except Exception:
        return head, b""


class _FakeReq:
    """只喂 JSON 的假请求，够 handle_voice_enrollment_* 用。"""

    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


def _resp_json(resp):
    return json.loads(resp.text)


def main() -> int:
    print("=" * 70)
    print("云 TTS 声音复刻：用上传的音频克隆音色")
    print("=" * 70)

    real_ffmpeg = E._ffmpeg

    section("A 音频处理")
    check("没有音频时报错",
          lambda: "选择" in _err_message(
              lambda: E.create_voice({"cloud_tts_model": "m"}, audios=[], name="n")))
    check("不支持的后缀时报错",
          lambda: "只支持" in _err_message(
              lambda: E._prepare_audio([("a.txt", b"x")])))
    check("空文件时报错",
          lambda: "空的" in _err_message(
              lambda: E._prepare_audio([("a.wav", b"")])))
    E._ffmpeg = lambda: None
    try:
        check("无 ffmpeg 时单文件原样提交且 MIME 按后缀给",
              lambda: E._prepare_audio([("a.mp3", b"ID3data")]) == (b"ID3data", "audio/mpeg"))
        check("无 ffmpeg 时多文件报错",
              lambda: "ffmpeg" in _err_message(
                  lambda: E._prepare_audio([("a.wav", WAV), ("b.wav", WAV)])))
        check("无 ffmpeg 时超过 10MB 也拦下",
              lambda: "10MB" in _err_message(
                  lambda: E._prepare_audio([("a.wav", b"\0" * (E.MAX_AUDIO_BYTES + 1))])))
    finally:
        E._ffmpeg = real_ffmpeg

    if real_ffmpeg():
        merged, mime = E._prepare_audio([("a.wav", WAV), ("b.wav", WAV)])
        with wave.open(io.BytesIO(merged), "rb") as w:
            params = (w.getnchannels(), w.getframerate(), w.getnframes() / w.getframerate())
        check("有 ffmpeg 时多文件合并成单声道 24kHz WAV",
              lambda: mime == "audio/wav" and params[0] == 1
              and params[1] == E.TARGET_SAMPLE_RATE and 1.8 < params[2] < 2.2)
        check("合并后长度是两段之和",
              lambda: len(merged) > len(WAV))
        check("超过 60 秒时报错",
              lambda: "60" in _err_message(
                  lambda: E._prepare_audio([("a.wav", _wav_bytes(31.0)),
                                            ("b.wav", _wav_bytes(31.0))])))
    else:
        print("  [SKIP] 本机没有 ffmpeg，跳过合并相关用例")

    srv = HTTPServer(("127.0.0.1", 0), EnrollHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    try:
        section("B 创建音色")
        POSTS.clear()
        FAIL_MODE[0] = False
        got = asyncio.run(E.create_voice(_cfg(port), audios=[("voice.wav", WAV)],
                                         name="guanyu", text="大家好",
                                         language="zh"))
        req = POSTS[-1] if POSTS else {}
        body = req.get("body") or {}
        head, audio = _data_uri_audio(body)
        check("请求打到百炼的音色定制接口",
              lambda: req.get("path") == "/api/v1/services/audio/tts/customization")
        check("鉴权头带上云 TTS 的密钥",
              lambda: req.get("auth") == "Bearer sk-enroll")
        check("用 qwen-voice-enrollment 的 create 动作",
              lambda: body.get("model") == "qwen-voice-enrollment"
              and (body.get("input") or {}).get("action") == "create")
        check("驱动模型默认取「云 TTS 模型」",
              lambda: (body.get("input") or {}).get("target_model") == "qwen3-tts-vc-2026-01-22")
        check("音频走 data URI 且解出来是音频",
              lambda: head.startswith("data:audio/") and len(audio) > 1000)
        check("音色名与语言都带上",
              lambda: (body.get("input") or {}).get("preferred_name") == "guanyu"
              and (body.get("input") or {}).get("language") == "zh")
        check("填了音频文本时带上 text",
              lambda: (body.get("input") or {}).get("text") == "大家好")
        check("返回 voice 与驱动模型",
              lambda: got.get("voice") == "guanyu"
              and got.get("target_model") == "qwen3-tts-vc-2026-01-22")
        POSTS.clear()
        asyncio.run(E.create_voice(_cfg(port), audios=[("voice.wav", WAV)], name="n"))
        check("音频文本留空时不发 text 字段",
              lambda: "text" not in (POSTS[-1]["body"].get("input") or {}))
        check("语言留空时回落默认语言",
              lambda: (POSTS[-1]["body"]["input"] or {}).get("language") == E.DEFAULT_LANGUAGE)

        section("C 音色列表与删除")
        POSTS.clear()
        voices = asyncio.run(E.list_voices(_cfg(port)))
        check("列表用 list 动作且不带 create 的字段",
              lambda: (POSTS[-1]["body"].get("input") or {}).get("action") == "list"
              and "audio" not in (POSTS[-1]["body"].get("input") or {}))
        check("返回 voice_list 条目",
              lambda: len(voices) == 1 and voices[0].get("voice") == "guanyu")
        POSTS.clear()
        removed = asyncio.run(E.delete_voice(_cfg(port), "guanyu"))
        check("删除用 delete 动作并回传音色名",
              lambda: (POSTS[-1]["body"].get("input") or {}).get("action") == "delete"
              and (POSTS[-1]["body"].get("input") or {}).get("voice") == "guanyu"
              and removed == "guanyu")

        section("D 上游报错")
        FAIL_MODE[0] = True
        check("HTTP 400 时把上游 message 抛出来",
              lambda: _err_message(lambda: E.create_voice(_cfg(port),
                                                          audios=[("v.wav", WAV)], name="n"))
              == "audio duration exceeds limit")
        FAIL_MODE[0] = False

        section("E HTTP 处理函数（前端 payload → 模块）")
        view = M.WebUIServer.__new__(M.WebUIServer)
        view.config = _cfg(port)
        POSTS.clear()
        created = asyncio.run(view.handle_voice_enrollment_create(_FakeReq({
            "audios": [{"name": "voice.wav", "data": base64.b64encode(WAV).decode()}],
            "name": "guanyu", "text": "大家好", "language": "zh"})))
        head, audio = _data_uri_audio(POSTS[-1]["body"])
        check("处理函数把 base64 解回音频再交给模块",
              lambda: head.startswith("data:audio/") and audio[:4] == b"RIFF")
        check("处理函数把名称/文本/语言透传下去",
              lambda: (POSTS[-1]["body"]["input"] or {}).get("preferred_name") == "guanyu"
              and (POSTS[-1]["body"]["input"] or {}).get("text") == "大家好"
              and (POSTS[-1]["body"]["input"] or {}).get("language") == "zh")
        check("处理函数返回 success 与 voice",
              lambda: created.status == 200 and _resp_json(created).get("voice") == "guanyu")
        POSTS.clear()
        removed = asyncio.run(view.handle_voice_enrollment_delete(_FakeReq({"name": "guanyu"})))
        check("删除处理函数把音色名交给模块",
              lambda: (POSTS[-1]["body"]["input"] or {}).get("voice") == "guanyu"
              and _resp_json(removed).get("voice") == "guanyu")
        listed = asyncio.run(view.handle_voice_enrollment_list(_FakeReq({})))
        payload = _resp_json(listed)
        check("列表处理函数返回 voices / languages / model",
              lambda: payload.get("success") and len(payload.get("voices") or []) == 1
              and "zh" in (payload.get("languages") or [])
              and payload.get("model") == "qwen3-tts-vc-2026-01-22")
        bad = asyncio.run(view.handle_voice_enrollment_create(_FakeReq({
            "audios": [{"name": "a.wav", "data": "abc"}], "name": "n"})))
        check("音频 base64 非法时返回 400 而不是抛异常",
              lambda: bad.status == 400 and "解码失败" in _resp_json(bad).get("error", ""))
        empty = asyncio.run(view.handle_voice_enrollment_delete(_FakeReq({"name": "  "})))
        check("删除时音色名为空返回 400", lambda: empty.status == 400)
        FAIL_MODE[0] = True
        fail = asyncio.run(view.handle_voice_enrollment_create(_FakeReq({
            "audios": [{"name": "voice.wav", "data": base64.b64encode(WAV).decode()}],
            "name": "guanyu"})))
        check("上游失败时处理函数回 400 并带上原因",
              lambda: fail.status == 400
              and "audio duration exceeds limit" in _resp_json(fail).get("error", ""))
        FAIL_MODE[0] = False
    finally:
        srv.shutdown()

    section("F 链路接入")
    check("三个接口都注册了路由",
          lambda: all(hasattr(M.WebUIServer, n) for n in
                      ("handle_voice_enrollment_list", "handle_voice_enrollment_create",
                       "handle_voice_enrollment_delete")))
    check("创建与删除在敏感接口清单里（列表只读，不拦）",
          lambda: M._needs_second_password("/api/tts/voice_enrollment/create")
          and M._needs_second_password("/api/tts/voice_enrollment/delete")
          and not M._needs_second_password("/api/tts/voice_enrollment/list"))
    check("前端有声音复刻入口与弹窗",
          lambda: all(k in HTML for k in
                      ("voice-enroll-modal", "voiceEnroller", "makeVoiceEnrollButton",
                       "api/tts/voice_enrollment/list", "api/tts/voice_enrollment/create",
                       "api/tts/voice_enrollment/delete")))
    check("音色字段同时带声音设计与声音复刻两个按钮",
          lambda: "voiceDesigner: true" in HTML and "voiceEnroller: true" in HTML)
    check("弹窗里的 DOM id 都能在 HTML 里找到",
          lambda: all(f'id="{i}"' in HTML for i in
                      ("voice-enroll-modal", "voice-enroll-close", "ve-files",
                       "ve-file-list", "ve-name", "ve-text", "ve-lang", "ve-create",
                       "ve-refresh", "ve-msg", "ve-list")))
    check("复刻弹窗是多选文件输入",
          lambda: 'id="ve-files"' in HTML and "multiple" in HTML)
    check("每次选中的文件都追加进待上传列表（对话框只给单选也能凑多个）",
          lambda: "addEnrollFiles(veFiles.files)" in HTML
          and "function addEnrollFiles(" in HTML
          and "enrollFiles.push(file)" in HTML)
    check("创建音色用累积的文件列表，每个文件可单独移除",
          lambda: "readAudioFiles(enrollFiles)" in HTML
          and "function renderEnrollFileList(" in HTML
          and "enrollFiles.splice(index, 1)" in HTML)

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
