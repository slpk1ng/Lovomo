# -*- coding: utf-8 -*-
import asyncio
import json
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from modules import app_context
from modules.app_paths import runtime_path
from modules.security import _needs_second_password
from modules.emotion_voices import _AUDIO_EXTS
from modules.app_context import HAS_AIOHTTP

if HAS_AIOHTTP:
    from aiohttp import web


def _json_file_response(data, filename: str):
    body = json.dumps(data, ensure_ascii=False, indent=2)
    return web.Response(body=body.encode("utf-8"), content_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


async def _local_file_response(path: Path, content_type: str):
    try:
        body = await asyncio.to_thread(path.read_bytes)
    except OSError:
        return web.Response(status=404, text="not found")
    resp = web.Response(body=body, content_type=content_type)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def _brief_response(resp, limit: int = 200) -> str:
    try:
        text = resp.text
    except Exception:
        return ""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


_NAME_ILLEGAL_CHARS = set('/\\:*?"<>|')


def _safe_name(name: str) -> str:
    s = str(name or "")
    if not s or s in (".", "..") or s != Path(s).name or (_NAME_ILLEGAL_CHARS & set(s)):
        return ""
    return s


def _safe_subdir(root: Path, name: str) -> Optional[Path]:
    safe = _safe_name(name)
    if not safe:
        return None
    p = (root / safe).resolve()
    if root.resolve() not in p.parents:
        return None
    return p


def _emotion_audio_file(folder: Path, file_name):
    name = _safe_name(file_name)
    if not name:
        return None, f"文件名非法：{file_name}"
    target = (folder / name).resolve()
    if folder.resolve() not in target.parents or not target.is_file():
        return None, f"文件不存在：{name}"
    if target.suffix.lower() not in _AUDIO_EXTS:
        return None, f"不是音频文件：{name}"
    return target, ""


def _write_text_file(path: Path, text) -> None:
    text = str(text or "").strip()
    if text:
        path.write_text(text, encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


@web.middleware
async def _webui_error_middleware(request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as e:
        import traceback
        text = f"[WebUI 异常] {request.method} {request.path}: {type(e).__name__}: {e}"
        trace = traceback.format_exc()
        print(text)
        print(trace)
        try:
            with open(runtime_path("webui_error.log"), "a", encoding="utf-8") as f:
                f.write(f"{time.ctime()} - {text}\n{trace}\n")
        except Exception:
            pass
        return web.json_response(
            {"success": False, "error": f"{type(e).__name__}: {e}"}, status=500)


_CSRF_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _same_origin(request) -> bool:
    if request.method in _CSRF_SAFE_METHODS:
        return True
    origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
    if not origin:
        return True
    host = request.headers.get("Host", "")
    if not host:
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if not parsed.netloc:
        return False
    if parsed.netloc == host:
        return True
    default_port = 443 if parsed.scheme == "https" else 80
    return parsed.port in (None, default_port) and host.split(":")[0] == parsed.hostname


def _make_auth_middleware(server: "WebUIServer"):
    @web.middleware
    async def _auth(request, handler):
        path = request.path
        if not _same_origin(request):
            print(f"已拒绝跨站请求：{request.method} {path}"
                  f"（Origin/Referer={request.headers.get('Origin') or request.headers.get('Referer')}）")
            return web.json_response({"success": False, "error": "跨站请求已被拒绝"}, status=403)
        if path.startswith("/api") and path not in ("/api/auth/login", "/api/auth/status",
                                                    "/api/auth/second"):
            token = server._auth_token
            if token:
                if request.cookies.get("lovomo_auth") != token:
                    return web.json_response({"success": False, "error": "需要密码"}, status=401)
            if _needs_second_password(path) and server._second_password \
                    and not server._second_unlocked():
                return web.json_response({"success": False, "error": "需要二级密码",
                                          "need_second_password": True}, status=403)
        return await handler(request)
    return _auth


def _plugin_runtime():
    server = app_context._WEBUI_SERVER_HOLDER.get("server")
    if server is None:
        return None
    return getattr(server, "_plugin_runtime", None)


_COMMAND_RE = re.compile(r"^\s*[#/]\s*(\S+)\s*(.*)$", re.S)


def _parse_plugin_command(text: str):
    m = _COMMAND_RE.match(str(text or ""))
    if not m:
        return None
    name = m.group(1).strip()
    if not name or len(name) > 32:
        return None
    return name, m.group(2).strip()


def _dispatch_plugin_command(user_text, session_type, target_id,
                             session_id, sender_id, sender_name, event):
    if not app_context.global_config.get("plugins_enabled", True):
        return False, None
    parsed = _parse_plugin_command(user_text)
    if not parsed:
        return False, None
    name, args = parsed
    rt = _plugin_runtime()
    if rt is None:
        return False, None
    if name not in rt.command_names():
        return False, None
    payload = {
        "session_type": session_type, "target_id": target_id,
        "session_id": session_id, "sender_id": sender_id,
        "sender_name": sender_name, "text": user_text,
        "group_id": target_id if session_type == "group" else None,
        "user_id": sender_id, "raw": event,
    }
    print(f"[插件] 指令 #{name} 命中，交由插件处理。")
    try:
        out = rt.dispatch_command(name, args, payload)
    except Exception as e:
        print(f"[插件] 指令 #{name} 分发失败: {type(e).__name__}: {e}")
        return True, None
    if out is None:
        return True, None
    return True, str(out)


def _dispatch_plugin_message(event_info: dict) -> list:
    if not app_context.global_config.get("plugins_enabled", True):
        return []
    rt = _plugin_runtime()
    if rt is None:
        return []
    try:
        return list(rt.dispatch_message(event_info) or [])
    except Exception as e:
        print(f"[插件] on_message 派发失败: {type(e).__name__}: {e}")
        return []


def _plugin_log(text: str) -> None:
    try:
        print(f"[插件] {text}")
    except Exception:
        pass


def _gguf_general_name(path) -> str:
    import struct
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF":
                return ""
            f.read(4)  # version
            tensor_count, kv_count = struct.unpack("<QQ", f.read(16))

            def rd_str():
                n, = struct.unpack("<Q", f.read(8))
                return f.read(n).decode("utf-8", "replace")

            sizes = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}

            def skip_val(t):
                if t == 8:
                    rd_str()
                elif t == 9:
                    etype, = struct.unpack("<I", f.read(4))
                    cnt, = struct.unpack("<Q", f.read(8))
                    if etype == 8:
                        for _ in range(cnt):
                            rd_str()
                    elif etype == 9:
                        for _ in range(cnt):
                            skip_val(9)
                    else:
                        f.seek(cnt * sizes[etype], 1)
                else:
                    f.seek(sizes[t], 1)

            for _ in range(min(kv_count, 4096)):
                key = rd_str()
                t, = struct.unpack("<I", f.read(4))
                if key == "general.name":
                    return rd_str().strip()
                skip_val(t)
    except Exception:
        return ""
    return ""
