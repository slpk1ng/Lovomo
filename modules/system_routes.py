"""Desktop operations routes and secret-free health checks."""
import asyncio
import sys
import tempfile
from pathlib import Path
import time
import zipfile
from urllib.parse import urlsplit

import httpx
from aiohttp import web

from . import app_context, model_library, portable_backup
from .config_presets import ConfigPresetError, DEFAULT_PROFILE, list_profiles, profile_loader
from .emotion_voices import EmotionManager, nested_voice_roots
from .session_context import ConnectionConfigView, connections_of
from .updater import APP_VERSION
from .tts import synthesize_sentence
from .tts_cloud import is_cloud_tts
from .llm_helpers import RoleContext
from .model_endpoints import model_list_endpoints
from .tls import verified_context
from .upload_limits import MAX_FILE_BYTES

VOICE_PREVIEW_MAX_CHARS = 500


def _preview_models(server):
    """Resolve saved synthesis settings; client input can only select an ID."""
    base = server.config
    data_path = server.memory_manager.data_path
    library = model_library.load_library_cached(data_path)
    models = {"": ("current", "", base)}
    profiles = {}
    for profile in list_profiles(data_path):
        name = profile["name"]
        if profile["builtin"]:
            continue
        try:
            saved = profile_loader(data_path, name)
        except (ConfigPresetError, OSError, ValueError):
            continue
        if saved is not None:
            profiles[name] = saved
            models[f"profile:{name}"] = ("profile", name, saved)
    for conn in connections_of(base):
        name = str(conn.get("config_profile") or DEFAULT_PROFILE)
        target = base if name == DEFAULT_PROFILE else profiles.get(name)
        overrides = model_library.connection_overrides(conn, library)
        if target is None or not (name != DEFAULT_PROFILE or any(
            field in overrides for field in ("roles", "tts_backend", "cloud_tts_provider")
        )):
            continue
        if "model_dir" in overrides and overrides["model_dir"] != target.get("model_dir"):
            overrides.update(tts_gpt_weights="", tts_sovits_weights="")
        models[f"connection:{conn['id']}"] = (
            "connection", str(conn.get("name") or conn["id"]), ConnectionConfigView(target, overrides))
    for provider in library.get("providers") or []:
        if provider.get("kind") != "cloud_tts" or not provider.get("id"):
            continue
        fields = {key: provider.get(field, "")
                  for field, key in model_library.PROVIDER_FIELD_MAP["cloud_tts"].items()}
        fields["tts_backend"] = "cloud"
        models[f"cloud:{provider['id']}"] = (
            "cloud", str(provider.get("name") or provider["id"]), ConnectionConfigView(base, fields))
    return models


def _preview_context(config):
    roles = getattr(config, "roles", None) or {}
    role = roles.get(getattr(config, "active_character", "")) or next(iter(roles.values()), None)
    return RoleContext(dict(config.config), role) if role else None


async def _endpoint_status(url):
    """Check TCP reachability without sending credentials or generating content."""
    try:
        parsed = urlsplit(str(url or ""))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return "missing"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        _, connection = await asyncio.wait_for(asyncio.open_connection(parsed.hostname, port), 2)
        connection.close()
        return "reachable"
    except (OSError, TimeoutError, ValueError):
        return "unreachable"


async def health_snapshot(server):
    config = server.config
    sender = app_context.sender
    clients = dict(getattr(sender, "channel_clients", {}))
    tts_cloud = config.get("tts_backend") == "cloud"
    asr_local = config.get("asr_engine", "local") == "local"
    llm_status, tts_status, asr_status = await asyncio.gather(
        _endpoint_status(config.get("llm_base_url")),
        _endpoint_status(config.get("cloud_tts_base_url") if tts_cloud else config.get("client_base_url")),
        _endpoint_status(config.get("asr_base_url") or config.get("llm_base_url"))
        if not asr_local else asyncio.sleep(0, "local"),
    )
    connections = [{"platform": conn.get("platform", "napcat"),
                    "status": ("disabled" if not conn.get("enabled", True)
                               else "connected" if str(conn.get("id")) in clients else "disconnected")}
                   for conn in connections_of(config)]
    runtime = server._plugin_runtime
    return {
        "version": APP_VERSION, "python": sys.version.split()[0], "checked_at": time.time(),
        "llm": {"status": llm_status, "model_configured": bool(config.get("llm_model_name"))},
        "tts": {"status": tts_status, "mode": "cloud" if tts_cloud else "local"},
        "asr": {"status": asr_status, "mode": "local" if asr_local else "remote"},
        "connections": connections,
        "plugins": {"enabled": bool(config.get("plugins_enabled")),
                    "loaded": len(runtime.loaded) if runtime else 0},
        "scheduler": {"enabled": bool(config.get("scheduler_enabled")),
                      "active": bool(getattr(app_context.scheduler, "_running", False))},
        "delivery": sender.delivery_snapshot() if sender and hasattr(sender, "delivery_snapshot") else {},
    }


def register_system_routes(server):
    async def health(_request):
        return web.json_response(await health_snapshot(server), headers={"Cache-Control": "no-store"})

    async def diagnostics(_request):
        return web.json_response(await health_snapshot(server), headers={
            "Cache-Control": "no-store", "Content-Disposition": 'attachment; filename="lovomo_diagnostics.json"'})

    async def verify_model(_request):
        config = server.config
        model = str(config.get("llm_model_name") or "")
        backend = str(config.get("llm_backend") or "ollama")
        urls = model_list_endpoints(config.get("llm_base_url"), backend)
        if not model or not urls:
            return web.json_response({"status": "missing"})
        key = str(config.get("llm_api_key") or "")
        headers = {"Authorization": f"Bearer {key}"} if key and backend != "ollama" else {}
        try:
            async with httpx.AsyncClient(timeout=4, trust_env=False, verify=verified_context(),
                                         headers=headers) as client:
                for url in urls:
                    response = await client.get(url)
                    if response.status_code in (401, 403):
                        return web.json_response({"status": "credentials_rejected"})
                    if not response.is_success:
                        continue
                    try:
                        payload = response.json()
                    except ValueError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    models = payload.get("models" if backend == "ollama" else "data")
                    if not isinstance(models, list):
                        continue
                    names = {str(entry.get("id") or entry.get("name") or entry.get("model") or "")
                             for entry in models if isinstance(entry, dict)}
                    if model in names or (backend == "ollama" and f"{model}:latest" in names):
                        return web.json_response({"status": "model_listed"})
                    return web.json_response({"status": "model_missing"})
        except (httpx.HTTPError, ValueError):
            pass
        return web.json_response({"status": "model_list_unavailable"})

    tts_lock = asyncio.Lock()

    def preview_options():
        roots = {}
        options = []
        for key, (source, name, config) in _preview_models(server).items():
            ctx = _preview_context(config)
            effective = ctx or config
            cloud = is_cloud_tts(effective)
            voices = []
            if not cloud:
                root = str(effective.get("ref_audio_root", "") or "")
                if root not in roots:
                    roots[root] = list(nested_voice_roots(root))
                voices = roots[root]
            options.append({"id": key, "source": source, "name": name, "cloud": cloud, "voices": voices})
        return options

    async def sample_tts_options(_request):
        return web.json_response({"models": await asyncio.to_thread(preview_options)},
                                 headers={"Cache-Control": "no-store"})

    async def sample_tts(request):
        samples = {
            "zh": "你好，这是一段语音试听。",
            "ja": "こんにちは。音声のテストです。",
            "en": "Hello, this is a voice test.",
            "yue": "你好，呢段係語音試聽。",
            "ko": "안녕하세요. 음성 테스트입니다.",
        }
        try:
            payload = await request.json() if request.can_read_body else {}
            if not isinstance(payload, dict):
                raise ValueError
        except (TypeError, ValueError):
            return web.json_response({"error": "试听参数无效"}, status=400)
        language = payload.get("language")
        if "language" in payload and (not isinstance(language, str) or language not in samples):
            return web.json_response({"error": "不支持的试听语言"}, status=400)
        model, voice, text = (payload.get(field, "") for field in ("model", "voice", "text"))
        if not all(isinstance(value, str) for value in (model, voice, text)):
            return web.json_response({"error": "试听参数无效"}, status=400)
        if len(text.strip()) > VOICE_PREVIEW_MAX_CHARS:
            return web.json_response({"error": "试听文本最多 500 字符"}, status=400)
        if tts_lock.locked():
            return web.json_response({"error": "已有试听正在运行"}, status=409)
        try:
            async with tts_lock:
                selected = (await asyncio.to_thread(_preview_models, server)).get(model)
                if selected is None:
                    return web.json_response({"error": "请选择已保存的合成模型"}, status=400)
                ctx = _preview_context(selected[2])
                if ctx is None:
                    return web.json_response({"error": "请先设置角色和音色"}, status=400)
                language = language or str(ctx.get("text_lang", "zh")).lower()
                if language not in samples:
                    language = "zh"
                ctx.role.update(text_lang=language, tts_auto_lang=False)
                emotions = {}
                if is_cloud_tts(ctx):
                    if voice:
                        return web.json_response({"error": "请选择可用的参考音色"}, status=400)
                else:
                    if voice:
                        root = await asyncio.to_thread(nested_voice_roots, str(ctx.get("ref_audio_root", "") or ""))
                        if voice not in root:
                            return web.json_response({"error": "请选择可用的参考音色"}, status=400)
                        ctx.role["ref_audio_root"] = str(root[voice])
                    # Read the selected root directly: runtime fallback could use the active character's voice.
                    ctx.role.pop("emotion_mimic_root", None)
                    ctx.config["emotion_mimic_root"] = ""
                    emotions = (await asyncio.to_thread(EmotionManager, ctx)).emotions
                    if not emotions:
                        return web.json_response({"error": "没有找到该音色的参考音频"}, status=400)
                    if voice and ctx.get("default_voice") not in emotions:
                        for neutral in ("平静", "pingjing"):
                            if neutral in emotions:
                                ctx.role["default_voice"] = neutral
                                break
                with tempfile.TemporaryDirectory(prefix="lovomo-voice-check-") as tmp:
                    path = await asyncio.wait_for(synthesize_sentence(
                        ctx, text.strip() or samples[language], "", emotions, Path(tmp)), timeout=60)
                    if path and Path(path).is_file() and Path(path).stat().st_size <= MAX_FILE_BYTES:
                        return web.Response(body=Path(path).read_bytes(), content_type="audio/wav",
                                            headers={"Cache-Control": "no-store"})
        except (OSError, ValueError, RuntimeError, TimeoutError, httpx.HTTPError):
            pass
        return web.json_response({"error": "合成失败，请检查语音服务和角色音色"}, status=503)

    backup_lock = asyncio.Lock()

    async def export(_request):
        try:
            async with backup_lock:
                content = await asyncio.to_thread(portable_backup.create_archive,
                    server.memory_manager.data_path, server.config.config.copy())
            return web.Response(body=content, content_type="application/zip", headers={
                "Cache-Control": "no-store", "Content-Disposition": 'attachment; filename="lovomo_backup.zip"'})
        except (ValueError, OSError) as error:
            return web.json_response({"error": str(error)}, status=400)

    async def import_backup(request):
        try:
            if request.content_type != "application/zip":
                raise ValueError("请选择 Lovomo ZIP 备份包")
            # Check decoded bytes as well as Content-Length.
            if request.content_length and request.content_length > MAX_FILE_BYTES:
                raise ValueError("备份包超过 20 MB")
            async with backup_lock:
                raw = bytearray()
                async for chunk in request.content.iter_chunked(64 * 1024):
                    if len(raw) + len(chunk) > MAX_FILE_BYTES:
                        raise ValueError("备份包超过 20 MB")
                    raw.extend(chunk)
                args = (server.memory_manager.data_path, server.config.config_path)
                if request.path.endswith("/stage"):
                    report = await asyncio.to_thread(portable_backup.stage, raw,
                        request.headers.get("X-Lovomo-Backup-SHA256", ""), *args)
                    return web.json_response({"success": True, "restart_required": True, **report})
                report = await asyncio.to_thread(portable_backup.preview, raw, *args)
                return web.json_response(report, headers={"Cache-Control": "no-store"})
        except (ValueError, OSError, RuntimeError, zipfile.BadZipFile):
            return web.json_response({"error": "无法读取或校验备份包。请选择 20 MB 以内的 Lovomo 备份并重新预览。"}, status=400)

    async def backup_status(_request):
        return web.json_response({"pending": portable_backup.pending_status(server.memory_manager.data_path)},
                                 headers={"Cache-Control": "no-store"})

    async def cancel_backup(_request):
        try:
            async with backup_lock:
                await asyncio.to_thread(portable_backup.cancel_pending, server.memory_manager.data_path)
            return web.json_response({"success": True})
        except (OSError, ValueError) as error:
            return web.json_response({"error": str(error)}, status=400)

    router = server.app.router
    router.add_get("/api/system/health", health)
    router.add_get("/api/system/diagnostics", diagnostics)
    router.add_post("/api/system/verify-model", verify_model)
    router.add_get("/api/system/sample-tts/options", sample_tts_options)
    router.add_post("/api/system/sample-tts", sample_tts)
    router.add_get("/api/backup/export", export)
    router.add_post("/api/backup/preview", import_backup)
    router.add_post("/api/backup/stage", import_backup)

    router.add_get("/api/backup/status", backup_status)
    router.add_post("/api/backup/cancel", cancel_backup)
