# -*- coding: utf-8 -*-
"""WebUI 服务（自 main.py 原样搬迁）：WebUIServer 整类 + WebUI 域常量 +
TTS 服务可用性检查。

本模块是方案第 19 节（第 5 批）的落点：main.py 的 WebUIServer 类体逐字搬运，
跨域可变全局（global_config / memory_manager / 各管理器槽位）一律经
modules.app_context 读写；在线更新状态机取自 modules.updater_tools，
WebView2 运行时辅助取自 modules.webview_runtime，依赖方向单向。
"""

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import threading
import time
import zipfile
from base64 import b64decode, b64encode
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import httpx

from napcat import Text

from modules import app_context
from modules.app_context import _APP_HOOKS
from modules.app_paths import get_resource_path, runtime_path, user_data_dir
from modules.log_console import (
    _LOG_FULL_MAX_CHARS, _runtime_log_limits, apply_log_max_size,
    console_log_line, global_log_buffer, log_lock,
)
from modules.security import (
    _API_KEY_KEYS, _CONNECTION_SECRET_KEYS, _SECRET_FIELD_KEYS,
    _WEBUI_PASSWORD_KEYS, _decrypt_api_keys, _decrypt_value,
    _decrypt_webui_password, _encrypt_api_keys, _encrypt_webui_password,
    _is_kept_secret, _is_masked_value,
    _mask_preview, _password_matches, _scrub_plaintext_secrets, _scrub_value,
)
from modules.single_instance import _candidate_icon_paths
from modules.proactive_state import forget_proactive_session
from modules.config_loader import (CONNECTION_PLATFORMS, CONNECTION_PLATFORM_NAMES,
                                   ConfigLoader, connection_defaults,
                                   connection_identity)
from modules.database import DatabaseManager
from modules.memory_store import MemoryManager, _is_memory_filename, _memory_session_id
from modules.emotion_voices import (EmotionManager, _AUDIO_EXTS, _is_emotion_folder,
                                    _read_sidecar_text)
from modules.stats import StatsManager
from modules.stickers import (IMAGE_EXTS, MIME_BY_EXT, collect_import_images,
                              get_import_job, safe_sticker_name, start_import_job)
from modules.tools import ToolRegistry
from modules.profiles import UserProfileManager
from modules.lexicon import LexiconManager
from modules.rag import RAGManager, extract_text_from_file
from modules.recall import HistoryRecallManager
from modules.todo_manager import TodoManager
from modules.jobs import ScheduledJobManager, generate_proactive_text
from modules.events import EventManager
from modules.mood import (MoodManager, affection_enabled, clamp, commit_mood,
                          current_mood, judge_and_decide, judge_enabled,
                          mood_enabled, mood_style, stored_mood,
                          _parse_key as _parse_mood_key)
from modules.affection import (AffectionManager, commitment_warning,
                               looks_like_acceptance, stage_of)
from modules.promises import PromiseManager, PROMISE_HINT_RE, extract_promise
from modules.encounters import EncounterManager
from modules.sender import MessageSender, VoicePacer, recall_delay_of, voice_enabled_for
from modules.adapters import (build_client, client_supports, friendly_user_label,
                              ilink_login_qr, ilink_login_status, recent_ilink_target,
                              qq_bind_create, qq_bind_poll,
                              make_event, _first as _adapter_first)
from modules.ghmirror import DEFAULT_MIRRORS
from modules.market import parse_market_repos
from modules.updater_tools import (
    _APP_RELEASE_REPO, _PROGRESS_LINE, _UPDATE_LOCK, _UPDATE_STATE,
    cleanup_old_installers, find_ready_installer, is_own_release_asset,
    log_progress, parse_installer_version, update_dir, update_progress_line,
)
from modules.webui_common import (
    _brief_response, _dispatch_plugin_command, _dispatch_plugin_message,
    _emotion_audio_file, _gguf_general_name, _json_file_response,
    _local_file_response, _make_auth_middleware, _plugin_log, _plugin_runtime,
    _safe_name, _safe_subdir, _webui_error_middleware, _write_text_file,
)
from modules.webview_runtime import (
    clear_webview_cache_and_reload, webview2_control_alive, webview_cache_key,
)
from modules.reply_pipeline import generate_reply
from modules.companion_tasks import (
    _extract_and_store_promise, _companion_send, send_task_lock,
    hot_reload_managers, in_character_background, session_user_id,
    _is_real_session, _known_sessions, session_history_block,
)
from modules.media_cache import (
    qq_avatar_bytes, _active_bot_qq, _avatar_cache_dir, _prune_avatar_cache,
    fetch_forward_text, fetch_quoted_context, transcribe_voice_message,
    _download_image_to_cache, _download_audio_to_temp, _take_pending_image,
    _remember_pending_image, refresh_image_urls, _sticker_capture_args,
    _spawn_sticker_capture, pick_media_source, _diary_recognition_note,
    _backfill_image_description, _render_ob11_segments, VOICE_OUT_FORMAT,
)
from modules.llm_helpers import (
    RoleContext, build_chat_messages, chat_once, chat_with_tools,
    normalize_sentences, normalize_single, split_multi_clause_sentences,
    stream_chat, extract_json, strip_thinking, SentenceStreamParser,
    get_image_reply, download_image, sniff_image_mime, repair_sentence_lang,
    segment_for_tts, speaker_labeled_lines, build_speaker_labels, identity_note,
    recent_user_ids, wants_mention_request, MENTION_ALL_ID,
    wants_quote_request, MENTION_PLACEHOLDER, strip_mention_placeholder,
    POKE_MESSAGE_TEXT, recall_request_kind, SENTENCE_ACTION_KEYS,
    apply_literal_mention, text_needs_tools, tool_flow_can_skip,
    asks_self_context, image_self_claim, image_identity_note,
    IMAGE_CLAIM_WARNING, sent_links, record_sent_links, urls_in_text,
    is_search_request, is_search_dissatisfied, lang_text_broken,
    translate_to_lang, available_mimics, model_list_endpoints,
    looks_like_full_endpoint, error_reply_text,
)
from modules.tts import synthesize_sentence, resolve_tts_path
from modules.tts_cloud import cloud_emotion_names, is_cloud_tts
from modules.tls import verified_context
from modules.audio_trim import normalize_audio
from modules.asr import (start_job as start_asr_job, get_job as get_asr_job,
                         AUDIO_MIMES)
from modules.tts_service import (process_manager, ensure_tts_service,
                                 auto_start_and_switch_tts, mark_exiting)
from modules.tts_voice_design import (create_voice, delete_voice, list_voices,
                                      LANGUAGES as VOICE_DESIGN_LANGUAGES)
from modules.tts_voice_enrollment import (create_voice as create_enrolled_voice,
                                          delete_voice as delete_enrolled_voice,
                                          list_voices as list_enrolled_voices,
                                          LANGUAGES as VOICE_ENROLLMENT_LANGUAGES)
from modules.plugins import (PluginManager, ALLOWED_EXTS as ALLOWED_ASSET_EXTS,
                             MAX_ASSET_BYTES as MAX_PLUGIN_ASSET_BYTES,
                             WEBUI_NAME as PLUGIN_WEBUI_NAME,
                             safe_asset_name, unique_asset_name)
from modules.plugin_publisher import (publish_plugin as _publish_to_github,
                                       unpublish_plugin as _unpublish_from_github,
                                       verify_token as _verify_github_token,
                                       PublishError as _PublishError)
from modules.session_context import (
    list_known_sessions, get_active_role, get_active_ctx, reply_ctx_of,
    role_memory, get_active_emotions, get_active_mimics, get_role_emotions,
    get_role_mimics, connections_of, connection_role_key,
    role_connection_snapshot, build_connection_profiles, resolve_target_roles,
    _fetch_member_name, whitelist_ids,
    _log_hidden, _log_hide_patterns,
)
from modules.config_presets import (ConfigPresetError,
                                    DEFAULT_PROFILE as DEFAULT_CONFIG_PROFILE,
                                    delete_profile as delete_config_profile,
                                    forget_profile_loaders,
                                    list_profiles as list_config_profiles,
                                    load_profile as load_config_profile,
                                    preset_dir as config_preset_dir,
                                    save_profile as save_config_profile)

from modules.app_context import HAS_AIOHTTP
if HAS_AIOHTTP:
    from aiohttp import web

# 每次刷新最多查几个 Release 的点赞数（GitHub 匿名接口有次数限制）
MAX_REACTION_LOOKUPS = 12

# 市场扫分支最多翻几页（每页 100 条），只是防止仓库异常时无限翻页
MAX_BRANCH_PAGES = 10

# 官方市场的第三方来源清单最多认几个仓库（清单靠别人提 PR 维护，防它跑飞）
MARKET_SOURCE_REPOS_MAX = 20

# 插件自带页面（功能页 / webui.html）注入的桥接脚本：同源 iframe 直接调父窗口上的
# lovomoHost。必须插在插件自己的脚本之前，否则插件在解析阶段拿不到 window.lovomo。
PLUGIN_BRIDGE_TEMPLATE = """<script>
(function () {
  var info = /*__LOVOMO_INFO__*/ null;
  var host = (parent !== window && parent.lovomoHost) ? parent.lovomoHost : null;
  var api = Object.assign({}, info, {
    asset: function (name) {
      return '/api/plugins/asset?id=' + encodeURIComponent(info.id)
           + '&name=' + encodeURIComponent(name || '');
    },
    theme: function () {
      var root = parent.document.documentElement;
      return {skinOn: root.classList.contains('skin-on'),
              accent: parent.getComputedStyle(root).getPropertyValue('--skin-accent').trim()};
    },
    log: function () {
      if (host) { host.log(info.id, Array.prototype.slice.call(arguments)); }
      else { console.log.apply(console, arguments); }
    },
    toast: function (text, isErr) { if (host) { host.toast(info.id, text, isErr); } },
    save: function (patch) {
      if (!host) { return Promise.resolve(api.settings); }
      return Promise.resolve(host.save(info.id, patch)).then(function (merged) {
        if (merged) { api.settings = merged; }
        return api.settings;
      });
    },
    apply: function () { return host ? host.apply(info.id) : null; },
    dirty: function () { return host ? host.dirty(info.id) : false; },
    reload: function () { return api.settings; },
    close: function () { if (host) { host.close(info.id); } }
  });
  window.lovomo = api;
  document.addEventListener('DOMContentLoaded', function () {
    window.dispatchEvent(new CustomEvent('lovomo:ready', {detail: api}));
  });
})();
</script>
"""


# 扫码登录的临时状态：接入方式 id -> {"qrcode": ...}
_CONNECTION_LOGIN_STATE: dict = {}
# 投递自检发出去的那句话：既当测试，也直接告诉对方"看到它就说明能正常送达"
SELFTEST_TEXT = "（自检消息）如果你在微信里看到这一条，说明这台机器人现在能正常把消息发出来。"


# 本机「已推送 / 已下架」记录的有效期：够撑过市场镜像与索引的缓存，
# 又不会在很久以后还压着市场里的真实状态
PUBLISH_STATE_TTL = 24 * 3600




# 聊天测试台专用会话：与真实 QQ 会话隔离，方便随时清掉重测
TEST_CHAT_SESSION = "webui_test"
TEST_CHAT_USER = "webui_test"
# 角色档案导出/导入时随档案带走的角色字段
_ROLE_EXPORT_KEYS = ("character_name", "character_key", "personality_prompt", "json_prompt",
                     "supplement_prompt", "default_voice", "ref_audio_root", "text_lang",
                     "prompt_lang")


# 待办状态取值（与 TodoManager 使用的一致，供 WebUI 更新接口做白名单校验）
TODO_STATUSES = ("pending", "done", "cancelled", "missed")



class WebUIServer:
    def __init__(self, config: ConfigLoader, memory_manager: MemoryManager):
        self.config = config
        # 必须先用当前配置初始化：为 None 时"首次保存"的 diff 恒为空，
        # 用户第一次改 TTS 参数保存不会触发重启，第二次才补上
        self._last_saved_config = dict(config.config or {})
        self.memory_manager = memory_manager
        self.html_path = get_resource_path("webui") / "start.html"
        self.app = web.Application(client_max_size=200 * 1080 * 1080)
        self._update_state_file = memory_manager.data_path / "update_check.json"
        self._update_checked_this_run = False
        self._update_last_result = None
        self._auth_file = memory_manager.data_path / "webui_auth.json"
        self._password = ""
        self._auth_token = None
        # 二级密码：设了才生效，解锁一次在有效期内不再拦敏感操作
        self._second_password = ""
        self._second_unlocked_until = 0.0
        self._second_once = 0
        # 插件系统：插件目录跟着用户数据目录走（%LOCALAPPDATA%\Lovomo\plugins），
        # 这样重装/覆盖更新程序不会把用户的插件一起删掉。
        self._plugins_root = user_data_dir() / "plugins"
        self.plugin_manager = PluginManager(
            self._plugins_root,
            self._plugins_root / "state.json",
            on_change=self._on_plugins_changed)
        self._plugin_runtime = None      # 由 run_backend 注入（能发消息时才建）
        self._plugin_market_cache = {}   # {缓存键: {entries, fetched_at, ...}}
        # 本机推送/下架过哪些插件：市场索引与镜像都有缓存，刚推上去或刚删掉的
        # 未必立刻读得到。落盘保存，重启后仍然算数，避免同一版本被重复推送。
        self._publish_state_file = user_data_dir() / "publish_state.json"
        self._publish_state = {"published": {}, "unpublished": {}}
        self._publish_state_readable = True
        self._load_publish_state()
        self._release_list_cache = {"fetched_at": 0.0, "data": None}
        self._refresh_auth_state()
        self.app.middlewares.append(_webui_error_middleware)
        self.setup_routes()
        self.app.middlewares.append(_make_auth_middleware(self))
        self.app.router.add_post("/api/auth/login", self.handle_auth_login)
        self.app.router.add_get("/api/auth/status", self.handle_auth_status)
        self.app.router.add_post("/api/auth/second", self.handle_auth_second)
        self.app.router.add_get("/api/update/status", self.handle_update_status)
        self.app.router.add_get("/api/update/check", self.handle_update_check)
        self.app.router.add_post("/api/update/download", self.handle_update_download)
        self.app.router.add_get("/api/update/progress", self.handle_update_progress)
        self.app.router.add_post("/api/update/install", self.handle_update_install)

    def _refresh_auth_state(self):
        """按当前 webui_password 重建会话令牌。「记住我」的令牌与密码哈希一起
        持久化：重启后密码未变则沿用令牌（浏览器旧 Cookie 继续有效），
        密码改变即作废。"""
        import secrets
        password = str(self.config.get("webui_password", "") or "")
        self._password = password
        second = str(self.config.get("webui_second_password", "") or "")
        if second != self._second_password:
            # 二级密码变了（或刚被清空），之前的解锁一律作废
            self._second_unlocked_until = 0.0
            self._second_once = 0
        self._second_password = second
        if not password:
            self._auth_token = None
            return
        phash = hashlib.sha256(password.encode("utf-8")).hexdigest()
        remembered = None
        try:
            if self._auth_file.exists():
                data = json.loads(self._auth_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("password_hash") == phash \
                        and float(data.get("expires_at", 0)) > time.time():
                    remembered = str(data.get("secret", "") or "") or None
        except Exception:
            remembered = None
        if remembered and remembered == getattr(self, "_auth_token", None):
            return
        self._auth_token = remembered or secrets.token_hex(16)

    def _second_unlocked(self) -> bool:
        """二级密码是否已解锁。有效时长填 0 时只放行紧接着的那一次请求
        （前端解锁后会自动重放被拦的那次），之后每次敏感操作都要重输。"""
        if self._second_unlocked_until and time.time() < self._second_unlocked_until:
            return True
        if self._second_once:
            self._second_once -= 1
            return True
        return False

    def _auth_remember(self) -> float:
        try:
            if self._auth_file.exists():
                data = json.loads(self._auth_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return float(data.get("expires_at", 0))
        except Exception:
            pass
        return 0.0

    def _auth_remember_save(self, minutes: int):
        try:
            expires = time.time() + minutes * 60 if minutes > 0 else 0
            self._auth_file.write_text(json.dumps({
                "expires_at": expires,
                "secret": self._auth_token,
                "password_hash": hashlib.sha256(
                    (self._password or "").encode("utf-8")).hexdigest(),
            }), encoding="utf-8")
        except Exception:
            pass

    async def handle_auth_status(self, request):
        # 这个接口在页面加载时必被调用一次，版本号搭车返回，
        # 省得为了左上角那行小字再开一个请求（关掉更新检查时也要能显示）
        from modules.updater import APP_VERSION
        version = APP_VERSION
        second = {"second_enabled": bool(self._second_password)}
        if not self._password:
            return web.json_response({"enabled": False, "authed": False,
                                      "version": version, **second})
        remaining = self._auth_remember() - time.time()
        has_valid_cookie = bool(self._auth_token) \
            and request.cookies.get("lovomo_auth") == self._auth_token
        if remaining > 0 and has_valid_cookie:
            return web.json_response({"enabled": True, "authed": True,
                                      "expires_at": self._auth_remember(),
                                      "version": version, **second})
        return web.json_response({"enabled": True, "authed": False,
                                  "version": version, **second})

    async def handle_auth_login(self, request):
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "参数错误"}, status=400)
        if _password_matches(payload.get("password", ""), self._password):
            try:
                raw = payload.get("remember_minutes")
                if raw is None or raw == "":
                    raw = self.config.get("webui_auth_ttl_minutes", 30)
                # 0 是合法值（关掉页面即失效），不能当"没填"退回默认
                minutes = max(0, min(int(raw or 0), 60 * 24 * 30))
            except (TypeError, ValueError):
                minutes = max(0, int(self.config.get("webui_auth_ttl_minutes", 30) or 30))
            self._auth_remember_save(minutes)
            resp = web.json_response({"success": True})
            resp.set_cookie("lovomo_auth", self._auth_token,
                            max_age=minutes * 60 if minutes > 0 else None,
                            samesite="Lax", httponly=True)
            return resp
        return web.json_response({"success": False, "error": "密码错误"}, status=401)

    async def handle_auth_second(self, request):
        """校验二级密码并解锁敏感操作。没设二级密码时直接放行。"""
        if not self._second_password:
            return web.json_response({"success": True, "unlocked": False,
                                      "second_enabled": False})
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "参数错误"}, status=400)
        if not _password_matches(payload.get("password", ""), self._second_password):
            return web.json_response({"success": False, "error": "二级密码错误"}, status=401)
        try:
            raw = payload.get("minutes")
            if raw is None or raw == "":
                raw = self.config.get("webui_second_unlock_minutes", 30)
            # 0 是合法值（每次都要重输），不能当"没填"退回默认
            minutes = max(0, min(int(raw or 0), 60 * 24 * 30))
        except (TypeError, ValueError):
            minutes = max(0, int(self.config.get("webui_second_unlock_minutes", 30) or 0))
        if minutes > 0:
            self._second_unlocked_until = time.time() + minutes * 60
            self._second_once = 0
        else:
            self._second_unlocked_until = 0.0
            self._second_once = 1
        return web.json_response({"success": True, "unlocked": True,
                                  "minutes": minutes, "second_enabled": True})

    def _update_cache(self) -> dict:
        try:
            if self._update_state_file.exists():
                data = json.loads(self._update_state_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _update_save(self, data: dict):
        try:
            self._update_state_file.write_text(json.dumps(data, ensure_ascii=False),
                                               encoding="utf-8")
        except Exception:
            pass

    async def handle_update_status(self, request):
        if not self.config.get("update_check_enabled", True):
            return web.json_response({"enabled": False, "has_update": False})
        from modules.updater import APP_VERSION
        include_pre = bool(self.config.get("update_include_prerelease", False))
        try:
            interval = max(0.0, float(self.config.get("update_check_interval_hours", 24)))
        except (TypeError, ValueError):
            interval = 24.0
        interval *= 3600
        first_this_run = not self._update_checked_this_run
        self._update_checked_this_run = True
        if not first_this_run and interval > 0:
            if self._update_last_result \
                    and time.time() - self._update_last_result[0] < interval:
                return web.json_response(
                    self._with_update_download(self._update_last_result[1]))
            cache = self._update_cache()
            cached_result = cache.get("result") or {}
            try:
                cached_age = time.time() - float(cache.get("checked_at") or 0)
            except (TypeError, ValueError):
                cached_age = interval
            if cached_result.get("current") == APP_VERSION \
                    and bool(cache.get("include_prerelease", False)) == include_pre \
                    and cache.get("checked_at") and cached_age < interval:
                return web.json_response(self._with_update_download(cached_result))
        payload = await self._update_check_payload()
        self._update_last_result = (time.time(), payload)
        return web.json_response(self._with_update_download(payload))

    async def handle_update_check(self, request):
        if not self.config.get("update_check_enabled", True):
            return web.json_response({"enabled": False, "has_update": False})
        return web.json_response(
            self._with_update_download(await self._update_check_payload()))

    # ---------------- 在线更新（下载 / 进度 / 安装） ----------------
    def _update_download_payload(self) -> dict:
        """下载状态：内存里的进度优先；没有就把磁盘上已下好的安装包报上来。"""
        with _UPDATE_LOCK:
            state = dict(_UPDATE_STATE)
        ready = find_ready_installer()
        if ready and not state["active"]:
            state.update({"finished": True, "version": ready["version"],
                          "path": ready["path"], "total": ready["size"],
                          "received": ready["size"]})
        state["ready"] = bool(ready)
        return state

    def _with_update_download(self, payload: dict) -> dict:
        """把「有没有下好的安装包」并进检查结果：前端据此直接跳到安装提示。"""
        result = dict(payload or {})
        result["download"] = self._update_download_payload()
        return result

    async def handle_update_progress(self, request):
        return web.json_response({"ok": True, **self._update_download_payload()})

    async def handle_update_download(self, request):
        """开始下载安装包（后台跑，前端轮询进度、日志里看同一行）。"""
        if not self.config.get("update_check_enabled", True):
            return web.json_response({"ok": False, "error": "更新检查已在配置里关闭"},
                                     status=400)
        with _UPDATE_LOCK:
            if _UPDATE_STATE["active"]:
                return web.json_response({"ok": True, "already": True,
                                          **self._update_download_payload()})
        ready = find_ready_installer()
        if ready:
            return web.json_response({"ok": True, "already": True,
                                      **self._update_download_payload()})
        with _UPDATE_LOCK:
            _UPDATE_STATE.update({"active": True, "phase": "准备中", "error": "",
                                  "received": 0, "total": 0, "finished": False,
                                  "started_at": time.time(), "source": ""})
        asyncio.create_task(self._update_download_task())
        return web.json_response({"ok": True, "started": True})

    async def _race_mirrors(self, url: str):
        """镜像测速：直连与各镜像并发试，谁先响应就用谁；证书问题降级再试一遍。"""
        from modules import updater as U
        from modules.ghmirror import candidates
        from modules.tls import is_cert_error
        urls = candidates(url, self._github_mirrors())
        try:
            winner, elapsed = await U.race_candidates(urls, verify=True)
            return winner, elapsed, False
        except Exception as e:
            if not is_cert_error(e):
                raise
            print("[更新] 证书校验失败，改用不校验证书的方式测速（本机可能装了"
                  "自签根证书的安全软件/加速器）")
            winner, elapsed = await U.race_candidates(urls, verify=False)
            return winner, elapsed, True

    async def _update_download_task(self):
        """后台任务：查发布 → 测速挑最快镜像 → 流式下载 → 落盘。"""
        from modules import updater as U
        from modules.ghmirror import mirror_label, note_success
        try:
            payload = await self._update_check_payload()
            installer = (payload or {}).get("installer") or {}
            version = str((payload or {}).get("latest") or "")
            url = str(installer.get("url") or "")
            if not (payload or {}).get("has_update"):
                raise RuntimeError("当前已是最新版本，不需要下载")
            if not url:
                raise RuntimeError("这个版本没有上传安装包（.exe）资源，"
                                   "请点「前往 GitHub 更新」手动下载")
            if not is_own_release_asset(url):
                raise RuntimeError("安装包地址不在本仓库的 Releases 里")
            try:
                expected = int(installer.get("size") or 0)
            except (TypeError, ValueError):
                expected = 0
            kind = str(installer.get("kind") or "exe")
            suffix = ".zip" if kind == "archive" else ".exe"
            update_dir().mkdir(parents=True, exist_ok=True)
            dest = update_dir() / f"Lovomo_Setup_{version}{suffix}"
            with _UPDATE_LOCK:
                _UPDATE_STATE.update({"phase": "测速中", "version": version,
                                      "path": str(dest), "total": expected,
                                      "received": 0, "source": ""})
            print(f"[更新] 开始在线更新：{version}"
                  f"（{installer.get('name') or dest.name}）")
            winner, elapsed, insecure = await self._race_mirrors(url)
            label = mirror_label(winner, url)
            if note_success(url, winner):
                print("[GitHub] " + (f"下载改用镜像 {label}" if winner != url
                                     else "直连已恢复"))
            print(f"[更新] 镜像测速完成：{label} 最快"
                  f"（{elapsed * 1000:.0f} ms，候选 {len(self._github_mirrors()) + 1} 个）")
            with _UPDATE_LOCK:
                _UPDATE_STATE.update({"phase": "下载中", "source": label})

            last = {"at": 0.0}

            def on_progress(received, total):
                with _UPDATE_LOCK:
                    _UPDATE_STATE.update({"received": received, "total": total})
                now = time.time()
                if now - last["at"] < 1.0 and (not total or received < total):
                    return
                last["at"] = now
                log_progress(update_progress_line(received, total, label))

            started = time.time()
            size = await U.download_asset(winner, dest, expected_size=expected,
                                          verify=not insecure, on_progress=on_progress)
            used = max(0.001, time.time() - started)
            # 类型校验按资源类型来：exe 看 MZ、压缩包看 PK（镜像可能回 HTML 错误页）
            await asyncio.to_thread(U.check_downloaded_head, dest, kind)
            print(f"[更新] 下载完成：{dest.name}（{size / 1048576:.1f} MB，"
                  f"用时 {used:.0f} 秒，平均 {size / used / 1048576:.1f} MB/s，"
                  f"来源 {label}）")
            if kind == "archive":
                # 发布的是压缩包：自动解出里面的 exe 再用；解完把压缩包删掉，
                # 留着只会让用户目录里躺双份
                with _UPDATE_LOCK:
                    _UPDATE_STATE.update({"phase": "解压中"})
                target = update_dir() / f"Lovomo_Setup_{version}.exe"
                info = await asyncio.to_thread(U.extract_installer, dest, target)
                print(f"[更新] 已从压缩包解出安装包：{info['entry']} → {target.name}"
                      f"（{info['size'] / 1048576:.1f} MB）")
                try:
                    dest.unlink()
                    print(f"[更新] 压缩包已删除：{dest.name}")
                except OSError as e:
                    print(f"[更新] 删除压缩包失败：{e}")
                dest, size = target, info["size"]
            with _UPDATE_LOCK:
                _UPDATE_STATE.update({"active": False, "finished": True,
                                      "received": size, "total": size, "error": "",
                                      "phase": "已完成", "path": str(dest)})
        except Exception as e:
            with _UPDATE_LOCK:
                _UPDATE_STATE.update({"active": False, "finished": False,
                                      "phase": "失败",
                                      "error": f"{type(e).__name__}: {e}"})
            print(f"[更新] 下载失败：{type(e).__name__}: {e}")

    async def handle_update_install(self, request):
        """立即安装：把安装包交给主程序那边的入口（起助手 → 退出本进程）。"""
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        path = str(payload.get("path") or "").strip()
        ready = find_ready_installer()
        if not path:
            path = str(ready.get("path") or "")
        if not path or not Path(path).is_file():
            return web.json_response({"ok": False, "error": "安装包不存在（可能已被删除）"},
                                     status=400)
        hook = _APP_HOOKS.get("install_update")
        if hook is None:
            return web.json_response({"ok": False, "error": "当前模式不支持自动安装"
                                                        "（请点「前往 GitHub 更新」手动下载）"},
                                     status=400)
        error = hook(path) or ""
        if error:
            return web.json_response({"ok": False, "error": error}, status=400)
        return web.json_response({"ok": True})

    def _github_mirrors(self) -> tuple:
        from modules.ghmirror import parse_mirrors
        return parse_mirrors(self.config.get("github_mirrors", ""))

    async def _github_fetch(self, url: str, kind: str = "json", *,
                            timeout: float = 10.0, headers=None):
        """取一个 GitHub 地址，直连不通时按配置里的镜像重试。

        返回 `(数据, 是否跳过了证书校验)`；kind 决定怎么解析响应
        （json / text / bytes），解析不了的响应（镜像返回错误页之类）也算失败，
        换下一个候选。只服务**匿名读**请求 —— 带 token 的写操作一律直连官方。
        """
        from modules.ghmirror import candidates, mirror_label, note_success
        from modules.tls import verified_context, unverified_context, is_cert_error
        hdrs = headers or {}
        last_err = None
        insecure = False
        for cand in candidates(url, self._github_mirrors()):
            for ctx, unverified in ((verified_context(), False),
                                    (unverified_context(), True)):
                try:
                    async with httpx.AsyncClient(timeout=timeout, proxy=None,
                                                 trust_env=False,
                                                 follow_redirects=True,
                                                 verify=ctx) as client:
                        resp = await client.get(cand, headers=hdrs)
                        resp.raise_for_status()
                        if kind == "json":
                            data = resp.json()
                        elif kind == "text":
                            data = resp.text
                        else:
                            data = resp.content
                except Exception as e:
                    last_err = e
                    # 证书问题（本机自签根证书/加速器/公司代理）换个不校验的方式再试；
                    # 其它错误直接换下一个候选地址。
                    if not unverified and is_cert_error(e):
                        continue
                    break
                insecure = insecure or unverified
                if note_success(url, cand):
                    print("[GitHub] " + (f"直连不可用，改用镜像 {mirror_label(cand, url)}"
                                         if cand != url else "直连已恢复"))
                return data, insecure
        raise last_err if last_err else RuntimeError("没有可用的 GitHub 地址")

    async def _fetch_releases(self, api_url: str):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "lovomo-update-check"}
        data, insecure = await self._github_fetch(api_url, "json", headers=headers)
        if insecure:
            print("[更新检查] 证书校验失败，已改用不校验证书的方式取回："
                  "常见原因是本机装了自签根证书（安全软件/网络加速器/公司代理），"
                  "系统证书库与 certifi 都没有它")
        return data, insecure

    async def _update_check_payload(self) -> dict:
        """向 GitHub 查一次最新版本并返回结果（含失败原因），成功时写入缓存。"""
        from modules.updater import APP_VERSION, is_newer, pick_latest_release
        include_pre = bool(self.config.get("update_include_prerelease", False))
        try:
            repo = "slpk1ng/Lovomo"
            api_url = f"https://api.github.com/repos/{repo}/releases?per_page=30"
            release_home = f"https://github.com/{repo}/releases/latest"
            releases, insecure = await self._fetch_releases(api_url)
            items = releases if isinstance(releases, list) else ([releases] if releases else [])
            visible = [r for r in items if isinstance(r, dict) and not r.get("draft")]
            seen = len(visible)
            pre_seen = len([r for r in visible if r.get("prerelease")])
            picked = pick_latest_release(items, include_pre)
            checked_at = time.time()
            if not picked.get("tag"):
                print(f"[更新检查] 未找到可用发布（可见发布 {seen} 个）")
                return {"enabled": True, "current": APP_VERSION, "has_update": False,
                        "checked_at": checked_at, "include_prerelease": include_pre,
                        "releases_seen": seen, "insecure": insecure,
                        "error": "GitHub 上没有可用的发布版本"
                                 "（草稿状态的发布对检查接口不可见，需要先正式发布）"}
            latest = picked["tag"]
            current = APP_VERSION
            has_update = is_newer(latest, current)
            page_url = str(picked.get("url") or "")
            if "github.com" not in page_url:
                page_url = release_home
            result = {"enabled": True, "current": current, "latest": latest,
                      "has_update": has_update, "url": page_url,
                      "installer": picked.get("installer", {}),
                      "prerelease": picked.get("prerelease", False),
                      "checked_at": checked_at, "include_prerelease": include_pre,
                      "releases_seen": seen, "prerelease_seen": pre_seen,
                      "insecure": insecure, "name": picked.get("name", ""),
                      "notes": picked.get("body", "")}
            self._update_save({"checked_at": checked_at, "result": result,
                               "include_prerelease": include_pre})
            print(f"[更新检查] 可见发布 {seen} 个（标记为预发布 {pre_seen} 个，"
                  f"检查时{'包含' if include_pre else '不含'}预发布）："
                  f"最新 {latest}{'（预发布）' if result['prerelease'] else ''}，"
                  f"当前 {current} → {'发现新版本' if has_update else '已是最新'}"
                  + ("（证书未校验）" if insecure else ""))
            return result
        except Exception as e:
            print(f"[更新检查] 失败: {type(e).__name__}: {e}")
            return {"enabled": True, "current": APP_VERSION, "has_update": False,
                    "checked_at": time.time(), "include_prerelease": include_pre,
                    "error": f"{type(e).__name__}: {e}"}

    def setup_routes(self):
        r = self.app.router
        r.add_get("/api/list", self.handle_list)
        r.add_post("/api/history", self.handle_history)
        r.add_get("/api/cache/image", self.handle_cache_image)
        r.add_post("/api/cache/images/clear", self.handle_cache_images_clear)
        r.add_get("/api/character/avatar", self.handle_character_avatar)
        r.add_get("/api/user/avatar", self.handle_user_avatar)
        r.add_post("/api/delete", self.handle_delete)
        r.add_post("/api/history/delete_messages", self.handle_delete_messages)
        r.add_get("/api/config", self.handle_get_config)
        r.add_post("/api/config/save", self.handle_save_config)
        r.add_get("/api/config/export", self.handle_export_config)
        r.add_post("/api/config/import", self.handle_import_config)
        r.add_get("/api/config/profiles", self.handle_config_profiles_list)
        r.add_post("/api/config/profiles/read", self.handle_config_profiles_read)
        r.add_post("/api/config/profiles/save", self.handle_config_profiles_save)
        r.add_post("/api/config/profiles/delete", self.handle_config_profiles_delete)
        r.add_post("/api/config/profiles/open_dir", self.handle_config_profiles_open_dir)
        # 插件系统
        r.add_get("/api/plugins/list", self.handle_plugins_list)
        r.add_post("/api/plugins/upload", self.handle_plugins_upload)
        r.add_post("/api/plugins/inspect", self.handle_plugins_inspect)
        r.add_post("/api/plugins/toggle", self.handle_plugins_toggle)
        r.add_post("/api/plugins/pin", self.handle_plugins_pin)
        r.add_post("/api/github/mirrors/test", self.handle_github_mirror_test)
        r.add_post("/api/plugins/delete", self.handle_plugins_delete)
        r.add_get("/api/plugins/theme", self.handle_plugins_theme)
        r.add_get("/api/plugins/panel", self.handle_plugins_panel)
        r.add_get("/api/plugins/webui", self.handle_plugins_webui)
        r.add_get("/api/plugins/icon", self.handle_plugins_icon)
        r.add_get("/api/plugins/asset", self.handle_plugins_asset)
        r.add_get("/api/plugins/settings", self.handle_plugins_settings_get)
        r.add_post("/api/plugins/settings", self.handle_plugins_settings_set)
        r.add_post("/api/plugins/reload", self.handle_plugins_reload)
        r.add_post("/api/plugins/open_dir", self.handle_plugins_open_dir)
        r.add_post("/api/plugins/publish_token", self.handle_plugins_publish_token)
        r.add_post("/api/plugins/publish_token_clear", self.handle_plugins_publish_token_clear)
        r.add_get("/api/plugins/publish_status", self.handle_plugins_publish_status)
        r.add_post("/api/plugins/publish", self.handle_plugins_publish)
        r.add_post("/api/plugins/unpublish", self.handle_plugins_unpublish)
        r.add_get("/api/plugins/market", self.handle_plugins_market)
        r.add_get("/api/plugins/updates", self.handle_plugins_updates)
        r.add_post("/api/plugins/market_sources", self.handle_plugins_market_sources)
        r.add_get("/api/plugins/readme", self.handle_plugins_readme)
        r.add_get("/api/releases", self.handle_releases)
        r.add_post("/api/releases/download", self.handle_release_download)
        r.add_post("/api/plugins/install_remote", self.handle_plugins_install_remote)
        # 文件夹选择 / 模型列表
        r.add_post("/api/dialog/pick_folder", self.handle_pick_folder)
        r.add_post("/api/dialog/pick_file", self.handle_pick_file)
        r.add_post("/api/llm/scan_models", self.handle_scan_models)
        r.add_post("/api/llm/list_remote_models", self.handle_list_remote_models)
        r.add_get("/api/roles", self.handle_get_roles)
        r.add_post("/api/roles/save", self.handle_save_roles)
        r.add_get("/api/logs", self.handle_get_logs)
        # 情绪音频管理
        r.add_get("/api/emotions/list", self.handle_emotions_list)
        r.add_post("/api/emotions/upload", self.handle_emotions_upload)
        r.add_post("/api/emotions/create", self.handle_emotions_create)
        r.add_post("/api/emotions/delete", self.handle_emotions_delete)
        r.add_get("/api/emotions/audio", self.handle_emotions_audio)
        r.add_post("/api/emotions/text", self.handle_emotions_text)
        r.add_post("/api/emotions/normalize", self.handle_emotions_normalize)
        # 云 TTS 声音设计（自定义音色）
        r.add_get("/api/tts/voice_design/list", self.handle_voice_design_list)
        r.add_post("/api/tts/voice_design/create", self.handle_voice_design_create)
        r.add_post("/api/tts/voice_design/delete", self.handle_voice_design_delete)
        # 云 TTS 声音复刻（用上传的音频克隆音色）
        r.add_get("/api/tts/voice_enrollment/list", self.handle_voice_enrollment_list)
        r.add_post("/api/tts/voice_enrollment/create", self.handle_voice_enrollment_create)
        r.add_post("/api/tts/voice_enrollment/delete", self.handle_voice_enrollment_delete)
        r.add_post("/api/asr/start", self.handle_asr_start)
        r.add_get("/api/asr/status", self.handle_asr_status)
        # 聊天记录导入导出
        r.add_get("/api/memory/export", self.handle_memory_export)
        r.add_post("/api/memory/import", self.handle_memory_import)
        r.add_get("/api/memory/export_all", self.handle_memory_export_all)
        # 统计
        r.add_get("/api/stats", self.handle_stats)
        r.add_get("/api/token-stats", self.handle_token_stats)
        r.add_get("/api/performance", self.handle_performance)
        r.add_post("/api/mood/set", self.handle_mood_set)
        r.add_get("/api/companion/diary", self.handle_companion_diary)
        # 聊天测试台
        r.add_get("/api/test/roles", self.handle_test_roles)
        r.add_post("/api/test/chat", self.handle_test_chat)
        r.add_get("/api/test/history", self.handle_test_history)
        r.add_post("/api/test/chat/clear", self.handle_test_chat_clear)
        # 角色档案导出 / 导入
        r.add_get("/api/characters/export", self.handle_character_export)
        r.add_post("/api/characters/import", self.handle_character_import)
        r.add_get("/api/sessions", self.handle_sessions)
        # 定时任务 / 待办 / 事件
        r.add_get("/api/jobs", self.handle_jobs)
        r.add_post("/api/jobs/save", self.handle_jobs_save)
        r.add_post("/api/jobs/batch", self.handle_jobs_batch)
        r.add_post("/api/jobs/run", self.handle_jobs_run)
        r.add_get("/api/todos", self.handle_todos)
        r.add_post("/api/todos/add", self.handle_todos_add)
        r.add_post("/api/todos/update", self.handle_todos_update)
        r.add_post("/api/todos/delete", self.handle_todos_delete)
        r.add_post("/api/todos/batch", self.handle_todos_batch)
        r.add_get("/api/events", self.handle_events)
        r.add_post("/api/events/save", self.handle_events_save)
        r.add_post("/api/events/batch", self.handle_events_batch)
        r.add_post("/api/events/test", self.handle_events_test)
        # 工具调用
        r.add_get("/api/tools", self.handle_tools)
        r.add_post("/api/tools/save", self.handle_tools_save)
        r.add_post("/api/tools/test", self.handle_tools_test)
        r.add_get("/api/search/engines", self.handle_search_engines)
        r.add_post("/api/search/engine", self.handle_search_engine_save)
        # RAG
        r.add_get("/api/rag/docs", self.handle_rag_docs)
        r.add_post("/api/rag/upload", self.handle_rag_upload)
        r.add_post("/api/rag/delete", self.handle_rag_delete)
        r.add_post("/api/rag/query", self.handle_rag_query)
        # 用户画像
        r.add_get("/api/profiles", self.handle_profiles)
        r.add_get("/api/connections", self.handle_connections)
        r.add_get("/api/connections/qr", self.handle_connection_qr)
        r.add_post("/api/connections/login_qr", self.handle_connection_login_qr)
        r.add_post("/api/connections/login_status", self.handle_connection_login_status)
        r.add_post("/api/connections/selftest", self.handle_connection_selftest)
        r.add_post("/api/connections/save", self.handle_connections_save)
        r.add_post("/api/connections/delete", self.handle_connections_delete)
        r.add_post("/api/profiles/save", self.handle_profiles_save)
        r.add_post("/api/profiles/delete", self.handle_profiles_delete)
        # 自主学习
        r.add_get("/api/lexicon", self.handle_lexicon)
        r.add_post("/api/lexicon/confirm", self.handle_lexicon_confirm)
        r.add_post("/api/lexicon/reject", self.handle_lexicon_reject)
        r.add_post("/api/lexicon/save", self.handle_lexicon_save)
        r.add_post("/api/lexicon/delete", self.handle_lexicon_delete)
        # 表情包
        r.add_get("/api/stickers/list", self.handle_stickers_list)
        r.add_post("/api/stickers/upload", self.handle_stickers_upload)
        r.add_post("/api/stickers/auto_import", self.handle_stickers_auto_import)
        r.add_get("/api/stickers/auto_import/status",
                  self.handle_stickers_auto_import_status)
        r.add_post("/api/stickers/delete", self.handle_stickers_delete)
        r.add_get("/api/stickers/file", self.handle_stickers_file)
        r.add_get("/favicon.ico", self.handle_favicon)
        r.add_get("/", self.handle_index)
        r.add_post("/api/clear-ui-cache", self.handle_clear_ui_cache)

    async def handle_clear_ui_cache(self, request):
        """「清理界面缓存」按钮：把界面缓存真的删掉。

        拆界面进程会把当前页面一起带走，所以先把响应发回去，再在后台线程里
        拆 → 删 → 重建；页面自己会带着新界面回来。
        """
        window = app_context._WEBVIEW_WINDOW_HOLDER.get("window")
        try:
            alive = webview2_control_alive(window) if window is not None else False
        except Exception:
            alive = False
        url = str(request.url.origin())

        def _run():
            try:
                result = clear_webview_cache_and_reload(window, url)
                print(f"[窗口] 手动清理界面缓存：释放 {result['freed'] / 1048576:.1f} MB"
                      f"（拆界面={result['released']}，重建={result['rebuilt']}）")
            except Exception as e:
                print(f"清理界面缓存失败: {type(e).__name__}: {e}")

        threading.Thread(target=_run, daemon=True).start()
        return web.json_response({"ok": True, "desktop": alive})

    async def handle_favicon(self, request):
        """浏览器每次打开页面都会自己来要 favicon，没有就报 404 刷控制台。"""
        paths = _candidate_icon_paths()
        if not paths:
            return web.Response(status=404)
        return web.FileResponse(paths[0], headers={"Cache-Control": "max-age=86400"})

    async def handle_index(self, request):
        if self.html_path.exists():
            # 界面是单文件前端，整个界面都在这个页面里，缓存它没有好处：
            # 前端更新后旧页面会继续命中缓存，用户重装程序都看不到新界面。
            # （WebView2 还有自己独立的一份缓存，由 purge_webview_cache 收拾。）
            return web.FileResponse(self.html_path, headers={"Cache-Control": "no-store"})
        return web.Response(text="WebUI 页面未找到", status=404)

    # ---------------- 配置 ----------------
    async def handle_get_config(self, request):
        if not self.config.config:
            self.config.config = self.config.default_config()
        else:
            self.config.config = {**self.config.default_config(), **self.config.config}
        masked = {**self.config.default_config(), **(self.config.config or {})}
        for key in _API_KEY_KEYS + _WEBUI_PASSWORD_KEYS:
            if isinstance(masked.get(key), dict):
                masked[key] = {k: _mask_preview(v) if isinstance(v, str) else v
                               for k, v in masked[key].items()}
            elif masked.get(key):
                masked[key] = _mask_preview(masked[key])
        return web.json_response({**masked, "defaults": self.config.default_config()})

    def _config_export_payload(self) -> dict:
        """导出用配置：密钥一律保持 config.json 的 enc:.../enc2:... 密文形态。

        内存里的配置是解密后的明文，直接导出等于把密钥写成明文送出去；这里做
        两层处理：

        1. 走 _encrypt_api_keys：已是 enc:/enc2: 密文的值原样保留，明文（无论
           sk- 还是别的形式）全部重新加密；
        2. 再做一次兜底扫描 _scrub_plaintext_secrets：凡是出现在敏感字段里、
           或以 sk-/sk_ 之类已知密钥前缀开头的残留明文，一律替换成对应密文，
           并对无法确认来源的值做打码，避免任何明文密钥出现在导出结果里。

        导出结果里 WebUI 访问密码与 API 密钥只能是密文。
        """
        payload = json.loads(json.dumps(self.config.config or {}, ensure_ascii=False))
        _encrypt_api_keys(payload)
        return _scrub_plaintext_secrets(payload)

    async def _save_export_via_dialog(self, default_name: str, content: bytes) -> str:
        """桌面窗口模式下弹系统保存框写盘。

        WebView2 默认禁止页面下载，前端 blob 下载在桌面窗口里是静默无效的；
        所以桌面模式由服务端落盘，返回 "saved" / "cancelled"；浏览器访问没有
        保存框可用，返回 "browser"，由前端按普通下载处理。
        """
        try:
            import webview
        except Exception:
            return "browser"
        window = app_context._WEBVIEW_WINDOW_HOLDER.get("window")
        if window is None:
            return "browser"
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: window.create_file_dialog(webview.SAVE_DIALOG,
                                                    save_filename=default_name))
        if not result:
            return "cancelled"
        path = result if isinstance(result, str) else str(result[0])
        Path(path).write_bytes(content)
        return path

    def _export_status_response(self, mode: str, path: str = ""):
        """导出结果状态。

        mode 取值：saved（服务端已写盘）/ cancelled（用户取消）/ error（导出出错）
        / download（前端按浏览器下载处理）。error 时 path 承载错误信息。
        """
        return web.json_response({"mode": mode, "path": path})

    async def handle_export_config(self, request):
        """导出配置。任何异常都必须以 error 状态回给前端，不能静默失败。"""
        try:
            payload = self._config_export_payload()
            content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            saved = await self._save_export_via_dialog("lovomo_config_export.json", content)
            if saved == "browser":
                return _json_file_response(payload, "lovomo_config_export.json")
            if saved == "cancelled":
                return self._export_status_response("cancelled")
            return self._export_status_response("saved", saved)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[配置导出] 失败: {type(e).__name__}: {e}")
            return self._export_status_response("error", f"{type(e).__name__}: {e}")

    def _apply_imported_config(self, data: dict) -> None:
        """把一份配置并进当前配置、落盘并热重载。

        导入的配置里密钥可能是打码值（来自 WebUI 导出）或密文，两者都要处理：
        打码值沿用内存里已有的真密钥；密文统一走与读盘相同的解密路径 —— 内存里的
        配置约定是明文（webui_password 也要一起解密，否则导入后原密码就再也登不
        进来）。解不开的密文（换过电脑）由 _decrypt_value 返回 None，调用方保留原
        密文而不是置空 —— 置空会让这份密文在下一次落盘时被空串永久覆盖，密钥不
        可恢复。
        """
        stored = self.config.config or {}
        for key in _SECRET_FIELD_KEYS:
            masked_value = data.get(key)
            if isinstance(masked_value, dict):
                stored_keys = stored.get(key) or {}
                if isinstance(stored_keys, dict) and any(
                        _is_masked_value(v) for v in masked_value.values()):
                    data[key] = {k: (stored_keys.get(k, "") if _is_masked_value(v) else v)
                                 for k, v in masked_value.items()}
            elif _is_masked_value(masked_value):
                data[key] = stored.get(key, "")
        _decrypt_api_keys(data)
        _decrypt_webui_password(data)
        merged = {**self.config.default_config(), **stored, **data}
        self.config.config = merged
        self.config._atomic_save(merged)
        self._after_config_reload()

    async def handle_import_config(self, request):
        try:
            ctype = request.content_type or ""
            if "multipart" in ctype:
                reader = await request.multipart()
                data = None
                async for part in reader:
                    if part.name == "file":
                        raw = await part.read(decode=False)
                        data = json.loads(raw.decode("utf-8"))
                        break
            else:
                data = await request.json()
            if not isinstance(data, dict):
                return web.json_response({"success": False, "error": "配置文件格式错误"}, status=400)
            self._apply_imported_config(data)
            return web.json_response({"success": True, "message": "配置已导入并热重载生效！"})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    # ------------------------------------------------------------------
    # 配置文件：整套配置按名字存成 data 下的 JSON，接入方式各用一份
    # ------------------------------------------------------------------
    def _config_preset_dir(self):
        return self.memory_manager.data_path

    def _config_profile_usage(self) -> dict:
        """配置文件 → 正在用它的接入方式名字，供页面提示「谁在用」。"""
        usage = {}
        for conn in (self.config.get("connections", []) or []):
            if not isinstance(conn, dict):
                continue
            name = str(conn.get("config_profile") or "").strip()
            if not name or name == DEFAULT_CONFIG_PROFILE:
                continue
            label = str(conn.get("name") or conn.get("id") or "")
            usage.setdefault(name, []).append(label)
        return usage

    def _masked_config(self, data: dict, hide_secrets: bool = False) -> dict:
        """读配置给前端时的密钥处理。

        hide_secrets=True（配置文件页）：密钥一律回空串 —— 那一页不显示也不改动密钥，
        密钥只在设置页统一管理；否则回头尾打码预览，让设置页能看出「填过了」。
        """
        def render(value):
            if not isinstance(value, str):
                return value
            return "" if hide_secrets else _mask_preview(value)

        masked = {**self.config.default_config(), **(data or {})}
        for key in _API_KEY_KEYS + _WEBUI_PASSWORD_KEYS:
            value = masked.get(key)
            if isinstance(value, dict):
                masked[key] = {k: render(v) for k, v in value.items()}
            elif value:
                masked[key] = render(value)
        return masked

    def _write_config_profile(self, name: str, data: dict, rename_from: str = "") -> dict:
        """把页面提交的配置写进配置文件。

        密钥字段空着（或原样带回打码值）表示这一项没被改动，沿用这份配置文件里已有的
        值 —— 配置文件页根本不显示密钥，没有这条规则的话改一个开关就会把密钥清空；
        其余统一加密后再落盘。
        """
        payload = dict(data or {})
        stored = {}
        for source in (name, rename_from):
            if not source or source == DEFAULT_CONFIG_PROFILE:
                continue
            try:
                stored = load_config_profile(self._config_preset_dir(), source)
                break
            except ConfigPresetError:
                continue
        for key in _SECRET_FIELD_KEYS:
            value = payload.get(key)
            if isinstance(value, dict):
                stored_keys = stored.get(key)
                if not isinstance(stored_keys, dict):
                    stored_keys = {}
                payload[key] = {k: (stored_keys.get(k, "") if _is_kept_secret(v) else v)
                                for k, v in value.items()} or stored_keys
            elif _is_kept_secret(value):
                payload[key] = stored.get(key, "")
        _encrypt_api_keys(payload)
        _encrypt_webui_password(payload)
        profile = save_config_profile(self._config_preset_dir(), name, payload, rename_from)
        forget_profile_loaders(self._config_preset_dir())
        return profile

    async def handle_config_profiles_list(self, request):
        try:
            profiles = list_config_profiles(self._config_preset_dir())
            usage = self._config_profile_usage()
            for item in profiles:
                item["used_by"] = usage.get(item["name"], [])
            return web.json_response({"success": True, "profiles": profiles,
                                      "default": DEFAULT_CONFIG_PROFILE})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_config_profiles_read(self, request):
        """读一份配置文件的内容；default 返回当前主配置，页面可以直接照着改。

        密钥字段一律回空串，并把字段名清单一起给前端：那一页不显示密钥，保存时前端
        据此把空着的密钥项摘掉，服务端按「没提交就沿用原值」处理。
        """
        try:
            payload = await request.json()
            name = str(payload.get("name") or "")
            if name == DEFAULT_CONFIG_PROFILE:
                data = self.config.config or {}
            else:
                data = load_config_profile(self._config_preset_dir(), name)
            return web.json_response({"success": True, "name": name,
                                      "config": self._masked_config(data, hide_secrets=True),
                                      "secret_keys": list(_SECRET_FIELD_KEYS),
                                      "defaults": self.config.default_config()})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_config_profiles_save(self, request):
        try:
            payload = await request.json()
            if not isinstance(payload.get("config"), dict):
                return web.json_response({"success": False, "error": "配置内容格式错误"},
                                         status=400)
            profile = self._write_config_profile(str(payload.get("name") or ""),
                                                 payload["config"],
                                                 str(payload.get("rename_from") or ""))
            return web.json_response({"success": True, "profile": profile})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_config_profiles_delete(self, request):
        try:
            payload = await request.json()
            name = delete_config_profile(self._config_preset_dir(),
                                         payload.get("name", ""))
            forget_profile_loaders(self._config_preset_dir())
            return web.json_response({"success": True, "name": name})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_config_profiles_open_dir(self, request):
        """在资源管理器里打开配置文件目录，方便用户直接看/备份这些 JSON。"""
        try:
            work = config_preset_dir(self._config_preset_dir())
            work.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                os.startfile(str(work))  # noqa: S606
            return web.json_response({"success": True, "path": str(work)})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    # ==================================================================
    # 插件系统
    # ==================================================================
    def attach_plugin_runtime(self, runtime) -> None:
        """由 run_backend 注入运行时（那时才拿得到 sender / 配置读取器）。"""
        self._plugin_runtime = runtime

    def _on_plugins_changed(self) -> None:
        """插件启用/禁用/安装/卸载后的热重载入口。"""
        rt = self._plugin_runtime
        if rt is None:
            return
        if not self.config.get("plugins_enabled", True):
            # 总开关关掉时，把已加载的全部卸掉（皮肤也会随之失效）
            try:
                rt.unload_all()
            except Exception as e:
                print(f"[插件] 停用清理失败: {e}")
            return
        try:
            errors = rt.reload_all()
            if errors:
                for pid, err in errors.items():
                    print(f"[插件] {pid} 重载失败: {err}")
        except Exception as e:
            print(f"[插件] 热重载失败: {e}")

    async def handle_plugins_list(self, request):
        """列出已安装插件及其启用状态。"""
        try:
            items = self.plugin_manager.list_plugins()
            for it in items:
                # 皮肤设置面板、features 开关、插件自带功能页都要读插件私有设置，
                # 少任一条件都会让前端拿到空 settings、开关显示错状态。
                if it.get("skin") or it.get("features") or it.get("panel"):
                    it["settings"] = self.plugin_manager.plugin_settings(it["id"])
            return web.json_response({
                "success": True,
                "plugins": items,
                "root": str(self.plugin_manager.root),
                # 打开了「应用外观」的皮肤插件：前端据此给 html 加 skin-on，
                # 没开的插件不改程序原本的背景。
                "active_skins": (self.plugin_manager.active_skin_ids()
                                 if self.config.get("plugins_enabled", True) else []),
                "loaded": sorted((self._plugin_runtime.loaded.keys()
                                  if self._plugin_runtime else [])),
                "commands": (self._plugin_runtime.command_names()
                             if self._plugin_runtime else []),
            })
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_plugins_upload(self, request):
        """上传 .zip 安装插件。带 force=1 表示用户已确认风险清单。"""
        try:
            reader = await request.multipart()
            data = None
            force = False
            file_name = ""
            async for part in reader:
                if part.name == "file":
                    file_name = part.filename or ""
                    data = await part.read(decode=False)
                elif part.name == "force":
                    force = (await part.text()).strip().lower() in ("1", "true", "yes")
            if not data:
                return web.json_response({"success": False, "error": "没有收到文件"}, status=400)
            if file_name and not file_name.lower().endswith(".zip"):
                return web.json_response(
                    {"success": False, "error": "插件包必须是 .zip 格式"}, status=400)
            result = self.plugin_manager.install_zip(data, force=force)
            if result.get("success"):
                print(f"[插件] 已安装：{result.get('name')} "
                      f"(id={result.get('id')}, 覆盖={result.get('replaced')})")
            return web.json_response(result,
                                     status=200 if result.get("success") else 400)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_plugins_inspect(self, request):
        """只审查不安装：让用户在装之前看到风险清单。"""
        try:
            reader = await request.multipart()
            data = None
            async for part in reader:
                if part.name == "file":
                    data = await part.read(decode=False)
            if not data:
                return web.json_response({"success": False, "error": "没有收到文件"}, status=400)
            report = self.plugin_manager.inspect_zip(data)
            return web.json_response({"success": bool(report.get("ok")),
                                      "report": report},
                                     status=200 if report.get("ok") else 400)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_plugins_toggle(self, request):
        """启用 / 禁用某个插件。"""
        try:
            payload = await request.json()
            pid = str(payload.get("id") or "").strip()
            enabled = bool(payload.get("enabled", True))
            info = self.plugin_manager.get(pid)
            if not info:
                return web.json_response({"success": False, "error": "插件不存在"}, status=404)
            self.plugin_manager.set_enabled(pid, enabled)
            return web.json_response({"success": True, "id": pid, "enabled": enabled})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def _timed_get(self, url: str, timeout: float = 6.0):
        """取一次地址并计时，返回 (毫秒, 是否成功, 说明)。"""
        from modules.tls import verified_context
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout, proxy=None, trust_env=False,
                                         follow_redirects=True,
                                         verify=verified_context()) as client:
                resp = await client.get(url, headers={"User-Agent": "lovomo-mirror-test"})
            ms = int((time.monotonic() - t0) * 1000)
            if resp.status_code == 200:
                return ms, True, ""
            return ms, False, f"HTTP {resp.status_code}"
        except Exception as e:
            return int((time.monotonic() - t0) * 1000), False, type(e).__name__

    async def _probe_mirror(self, template: str, repo: str) -> dict:
        """测一个镜像模板：raw 与 api 各探一次，取最快的一次当它的速度。"""
        from modules.ghmirror import apply_template, probe_urls, template_host
        best = None
        kinds = []
        note = ""
        for kind, url in probe_urls(repo):
            cand = apply_template(str(template), url)
            if not cand:
                continue
            ms, ok, why = await self._timed_get(cand)
            if ok:
                kinds.append(kind)
                best = ms if best is None else min(best, ms)
            else:
                note = note or why
        return {
            "template": str(template),
            "host": template_host(str(template)) or str(template),
            "ms": int(best) if best is not None else 0,
            "ok": best is not None,
            "kinds": "+".join(kinds),
            "note": "" if best is not None else (note or "不可用"),
        }

    async def handle_github_mirror_test(self, request):
        """给配置里的每个镜像测一次延迟，按快的在前返回。"""
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        from modules.ghmirror import parse_mirrors
        raw = payload.get("mirrors")
        templates = parse_mirrors(raw if raw is not None
                                  else self.config.get("github_mirrors", ""))
        if not templates:
            return web.json_response({"success": False, "error": "没有配置镜像地址"},
                                     status=400)
        repo = (str(self.config.get("plugin_market_repo", "") or "").strip()
                or str(self.config.get("plugin_release_repo", "") or "").strip())
        if not repo:
            return web.json_response({"success": False, "error": "没有可用的仓库地址"},
                                     status=400)
        items = await asyncio.gather(*[self._probe_mirror(t, repo) for t in templates])
        items = sorted(items, key=lambda r: (0 if r["ok"] else 1,
                                             r["ms"] if r["ok"] else 10 ** 6))
        print("[GitHub] 镜像测速：" + "；".join(
            f"{r['host']} {r['ms']}ms" if r["ok"] else f"{r['host']} {r['note']}"
            for r in items))
        return web.json_response({"success": True, "repo": repo, "mirrors": items})

    async def handle_plugins_pin(self, request):
        """置顶 / 取消置顶某个插件（只影响「插件」页的排序）。"""
        try:
            payload = await request.json()
            pid = str(payload.get("id") or "").strip()
            pinned = bool(payload.get("pinned", True))
            if not self.plugin_manager.get(pid):
                return web.json_response({"success": False, "error": "插件不存在"}, status=404)
            self.plugin_manager.set_pinned(pid, pinned)
            print(f"[插件] {'已置顶' if pinned else '已取消置顶'}：{pid}")
            return web.json_response({"success": True, "id": pid, "pinned": pinned})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_plugins_delete(self, request):
        """卸载插件。keep_settings / keep_data 为真时保留对应的配置与数据。"""
        try:
            payload = await request.json()
            pid = str(payload.get("id") or "").strip()
            keep_settings = bool(payload.get("keep_settings"))
            keep_data = bool(payload.get("keep_data"))
            if not self.plugin_manager.get(pid):
                return web.json_response({"success": False, "error": "插件不存在"}, status=404)
            # 先卸载运行时再删目录：Windows 上插件模块还在 sys.modules 里时
            # 文件句柄没释放，rmtree 会失败（现象是"点卸载没反应"）。
            ok = self.plugin_manager.uninstall(pid, keep_settings=keep_settings,
                                               keep_data=keep_data)
            if ok:
                kept = [name for name, flag in (("配置", keep_settings),
                                                ("数据", keep_data)) if flag]
                print(f"[插件] 已卸载：{pid}"
                      + (f"（保留{'、'.join(kept)}）" if kept else ""))
            return web.json_response({"success": ok,
                                      "error": "" if ok else "删除目录失败"})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_plugins_theme(self, request):
        """把所有启用插件的皮肤 CSS 拼起来给前端注入。"""
        try:
            if not self.config.get("plugins_enabled", True):
                return web.Response(text="/* 插件系统已关闭 */",
                                    content_type="text/css", charset="utf-8")
            css = self.plugin_manager.theme_css()
            return web.Response(text=css, content_type="text/css",
                                charset="utf-8")
        except Exception as e:
            return web.Response(text=f"/* 读取皮肤失败: {e} */",
                                content_type="text/css", charset="utf-8")

    async def handle_plugins_icon(self, request):
        """返回插件图标，供列表展示。"""
        try:
            pid = str(request.query.get("id") or "").strip()
            f = self.plugin_manager.icon_file(pid)
            if f is None:
                return web.json_response({"success": False, "error": "没有图标"},
                                         status=404)
            mime = {".png": "image/png", ".jpg": "image/jpeg",
                    ".svg": "image/svg+xml", ".ico": "image/x-icon"}.get(
                f.suffix.lower(), "application/octet-stream")
            return web.Response(body=f.read_bytes(), content_type=mime)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_plugins_open_dir(self, request):
        """在资源管理器里打开插件目录，方便用户手动放插件。"""
        try:
            root = self.plugin_manager.ensure_root()
            if os.name == "nt":
                os.startfile(str(root))  # noqa: S606
            return web.json_response({"success": True, "path": str(root)})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_plugins_asset(self, request):
        """读取插件 data 目录下的静态资源（背景图、样式、字体等）。"""
        try:
            pid = str(request.query.get("id") or "").strip()
            name = str(request.query.get("name") or "").strip()
            f, mime = self.plugin_manager.asset_file(pid, name)
            if f is None:
                return web.Response(status=404, text="not found")
            return web.Response(body=f.read_bytes(), content_type=mime)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_plugins_settings_get(self, request):
        """读取某个插件的私有设置（插件未安装时返回空对象）。"""
        try:
            pid = str(request.query.get("id") or "").strip()
            if not self.plugin_manager.get(pid):
                return web.json_response({"success": False, "error": "插件不存在"},
                                         status=404)
            return web.json_response({"success": True, "id": pid,
                                      "settings": self.plugin_manager.plugin_settings(pid)})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_plugins_settings_set(self, request):
        """保存某个插件的私有设置（只落盘，不立即生效）。

        落盘前按插件声明的字段筛一遍：只有插件在清单里声明过的键会被保留，
        值也按声明的类型收敛。否则设置文件会变成任意 JSON 的暂存区 ——
        插件写进去什么，下次读出来就是什么。

        落盘不等于生效：插件运行时与皮肤 CSS 都读磁盘上的这份文件，但要让
        改动真的作用到已加载的插件上，得由「保存并重载」走 `plugins/reload`
        重新加载运行时。这样用户一次调好几项也不会中途反复生效。
        """
        try:
            payload = await request.json()
            pid = str(payload.get("id") or "").strip()
            if not self.plugin_manager.get(pid):
                return web.json_response({"success": False, "error": "插件不存在"},
                                         status=404)
            settings = payload.get("settings")
            if not isinstance(settings, dict):
                return web.json_response({"success": False, "error": "settings 必须是对象"},
                                         status=400)
            clean = self.plugin_manager.filter_settings(pid, settings)
            ok = self.plugin_manager.save_plugin_settings(pid, clean)
            return web.json_response({"success": ok,
                                      "draft": clean if ok else {},
                                      "error": "" if ok else "写入设置失败"})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_plugins_reload(self, request):
        """重载插件运行时 + 提交设置草稿 + 刷新皮肤。

        三件事缺一不可：
          1. `commit_settings()` 把磁盘上的设置草稿提交成生效值 —— 皮肤变量
             与插件读到的设置都以此为准；
          2. `_on_plugins_changed()` 重新加载插件运行时（钩子、指令、开关）；
          3. 回传最新 active_skins，前端重挂 theme 链接与 skin-on 类。
        """
        try:
            pid = ""
            try:
                payload = await request.json()
                pid = str(payload.get("id") or "").strip()
            except Exception:
                pid = ""
            if pid and not self.plugin_manager.get(pid):
                return web.json_response({"success": False, "error": "插件不存在"},
                                         status=404)
            if not self.config.get("plugins_enabled", True):
                return web.json_response({"success": False,
                                          "error": "插件系统已在配置里关闭"},
                                         status=400)
            self.plugin_manager.commit_settings(pid or None)
            self._on_plugins_changed()
            return web.json_response({
                "success": True,
                "id": pid,
                "settings": self.plugin_manager.plugin_settings(pid) if pid else {},
                "active_skins": self.plugin_manager.active_skin_ids(),
                "loaded": sorted(self._plugin_runtime.loaded.keys()
                                 if self._plugin_runtime else []),
                "commands": (self._plugin_runtime.command_names()
                             if self._plugin_runtime else []),
            })
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def _serve_plugin_page_file(self, request, default_name: str):
        """把插件目录里的界面文件原样回给浏览器（自带功能页 / webui 及其同目录资源）。

        `?id=<插件>&name=<相对路径>`；name 留空时用 default_name。
        页面本体（name 就是 default_name 的那次请求）会先注入桥接脚本。
        """
        try:
            pid = str(request.query.get("id") or "").strip()
            info = self.plugin_manager.get(pid)
            if not info:
                return web.Response(status=404, text="plugin not found")
            name = str(request.query.get("name") or "").strip() or default_name
            if not name:
                return web.Response(status=404, text="no page file")
            f, mime = self.plugin_manager.asset_file(pid, name, root_scope=True)
            if f is None:
                return web.Response(status=404, text="not found")
            body = f.read_bytes()
            if name == default_name and "html" in mime:
                body = self._inject_plugin_bridge(body, pid, info)
            return web.Response(body=body, content_type=mime)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    def _inject_plugin_bridge(self, body: bytes, pid: str, info: dict) -> bytes:
        """把桥接脚本插到插件页面最前面（插不进就当普通页面返回）。"""
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return body
        payload = {
            "id": pid,
            "name": str(info.get("name") or pid),
            "version": str(info.get("version") or ""),
            "enabled": bool(info.get("enabled")),
            "settings": self.plugin_manager.plugin_settings(pid),
        }
        script = PLUGIN_BRIDGE_TEMPLATE.replace(
            "/*__LOVOMO_INFO__*/ null",
            json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"))
        head = text.lower().find("<head>")
        if head >= 0:
            at = head + len("<head>")
            text = text[:at] + script + text[at:]
        else:
            text = script + text
        return text.encode("utf-8")

    async def handle_plugins_panel(self, request):
        """插件自带的功能页界面（清单里的 panel.html）。"""
        pid = str(request.query.get("id") or "").strip()
        info = self.plugin_manager.get(pid) or {}
        return await self._serve_plugin_page_file(
            request, str((info.get("panel") or {}).get("html") or "").strip())

    async def handle_plugins_webui(self, request):
        """插件自带的 webui（插件根目录的 webui.html）。"""
        return await self._serve_plugin_page_file(request, PLUGIN_WEBUI_NAME)

    async def _market_raw_text(self, url: str):
        """从 raw.githubusercontent.com 取一段文本，取不到返回 None。"""
        try:
            text, _ = await self._github_fetch(
                url, "text", headers={"User-Agent": "lovomo-plugin-market"})
            return text
        except Exception:
            return None

    async def _market_manifest(self, repo: str, branch: str):
        """取某条插件分支的清单，返回 (清单字典, 清单文件名)。

        分支根目录放 plugin.yaml 或 plugin.json 都行，yaml 优先 —— 与
        本地安装走的是同一套优先级（modules.plugins.MANIFEST_NAMES）。
        """
        from modules.market import RAW_TMPL
        from modules.plugins import MANIFEST_NAMES, parse_manifest_text
        for name in MANIFEST_NAMES:
            text = await self._market_raw_text(
                RAW_TMPL.format(repo=repo, branch=branch, path=name))
            if text is None:
                continue
            data = parse_manifest_text(text, name)
            if data:
                return data, name
        return None, ""

    async def _market_commit_time(self, repo: str, sha: str) -> float:
        """按 SHA 单独查提交时间。

        插件分支是独立根提交，不在默认分支的提交列表里，靠
        `/commits` 那一次批量查询拿不到它的时间。
        """
        from modules.market import parse_iso_time
        try:
            data, _ = await self._fetch_releases(
                f"https://api.github.com/repos/{repo}/git/commits/{sha}")
        except Exception:
            return 0.0
        if not isinstance(data, dict):
            return 0.0
        return float(parse_iso_time(str((data.get("committer") or {}).get("date") or "")) or 0.0)


    async def _market_branches(self, repo: str):
        """列出仓库的全部分支（分页拉全，返回 (分支列表, 是否跳过证书校验)）。

        GitHub 一次最多给 100 条，插件多了后面的会被截断成"不存在"，
        所以按页取到不足一页为止。
        """
        out, insecure = [], False
        for page in range(1, MAX_BRANCH_PAGES + 1):
            data, insecure_p = await self._fetch_releases(
                f"https://api.github.com/repos/{repo}/branches"
                f"?per_page=100&page={page}")
            insecure = insecure or insecure_p
            batch = [b for b in (data if isinstance(data, list) else [])
                     if isinstance(b, dict)]
            out += batch
            if len(batch) < 100:
                break
        return out, insecure

    async def _market_tags(self, repo: str, prefix: str) -> list:
        """列出仓库里以插件前缀开头的标签名（分页拉全）。

        只按前缀筛选，形状合不合法交给 `parse_version_tag()` 判定 ——
        程序自己的版本标签不会被误认成插件。
        """
        out = []
        for page in range(1, MAX_BRANCH_PAGES + 1):
            data, _ = await self._fetch_releases(
                f"https://api.github.com/repos/{repo}/git/matching-refs/tags/{prefix}"
                f"?per_page=100&page={page}")
            batch = [str(r.get("ref") or "").rsplit("/", 1)[-1]
                     for r in (data if isinstance(data, list) else [])
                     if isinstance(r, dict) and r.get("ref")]
            out += [t for t in batch if t]
            if len(batch) < 100:
                break
        return out

    async def _market_branch_entries(self, repo: str, prefix: str) -> dict:
        """扫插件分支并组装市场条目（branches / commits / releases / raw）。"""
        from modules.market import (ARCHIVE_TMPL, RAW_TMPL, build_entry,
                                    count_reactions, matches_branch_prefix,
                                    plugin_id_from_branch, release_belongs_to,
                                    parse_iso_time, summarize_releases)
        warnings = []
        insecure = False
        branches, insecure_b = await self._market_branches(repo)
        insecure = insecure or insecure_b
        picked = [str(b.get("name") or "") for b in (branches or [])
                  if isinstance(b, dict) and matches_branch_prefix(b.get("name"), prefix)]
        if not picked:
            return {"entries": [], "warnings":
                    [f"{repo} 上没有以「{prefix}」开头的分支"], "insecure": insecure}

        times = {}
        try:
            commits, insecure_c = await self._fetch_releases(
                f"https://api.github.com/repos/{repo}/commits?per_page=100")
            insecure = insecure or insecure_c
            for c in (commits if isinstance(commits, list) else []):
                if not isinstance(c, dict):
                    continue
                sha = str(c.get("sha") or "")
                date = str(((c.get("commit") or {}).get("committer") or {})
                           .get("date") or "")
                if sha and date:
                    times[sha] = parse_iso_time(date)
        except Exception as e:
            warnings.append(f"拿不到分支提交时间：{type(e).__name__}")

        releases = []
        try:
            data, insecure_r = await self._fetch_releases(
                f"https://api.github.com/repos/{repo}/releases?per_page=100")
            insecure = insecure or insecure_r
            releases = [r for r in (data if isinstance(data, list) else [])
                        if isinstance(r, dict)]
        except Exception as e:
            warnings.append(f"拿不到 Release 信息（下载量/收藏量会显示 0）：{type(e).__name__}")

        branch_meta = {b: {"id": plugin_id_from_branch(b, prefix)} for b in picked}
        live = [r for r in releases if not r.get("draft")]
        # 收藏量要按 Release 单独查点赞数（列表接口不给总数），
        # 只查确实属于这些插件的那些，并且设上限，别把配额烧光。
        targets = [r for r in live if any(release_belongs_to(r, b, branch_meta[b]["id"])
                                          for b in picked)]
        for rel in targets[:MAX_REACTION_LOOKUPS]:
            rid = rel.get("id")
            if not rid:
                continue
            try:
                rx, _ = await self._fetch_releases(
                    f"https://api.github.com/repos/{repo}/releases/{rid}"
                    "/reactions?per_page=100")
                rel["_reactions"] = count_reactions(rx)
            except Exception:
                rel["_reactions"] = 0
        stats = summarize_releases(live, branch_meta)

        entries = []
        for branch in picked:
            manifest, _manifest_name = await self._market_manifest(repo, branch)
            if not manifest:
                from modules.plugins import MANIFEST_NAMES
                warnings.append(f"{branch}：分支根目录没有 "
                                + " / ".join(MANIFEST_NAMES) + "，已跳过")
                continue
            sha = ""
            for b in (branches or []):
                if isinstance(b, dict) and str(b.get("name")) == branch:
                    sha = str(((b.get("commit") or {}).get("sha")) or "")
                    break
            entries.append(build_entry(
                branch, prefix, manifest,
                raw_base=RAW_TMPL.format(repo=repo, branch=branch, path="").rstrip("/"),
                repo=repo, stats=stats.get(branch) or {},
                commit_at=(float(times.get(sha) or 0.0)
                           or (await self._market_commit_time(repo, sha) if sha else 0.0))))
        return {"entries": entries, "warnings": warnings, "insecure": insecure}

    async def _market_default_branch(self, repo: str) -> str:
        """仓库的默认分支名（取不到就退回 main）。"""
        try:
            data, _ = await self._fetch_releases(
                f"https://api.github.com/repos/{repo}")
        except Exception:
            return "main"
        if not isinstance(data, dict):
            return "main"
        return str(data.get("default_branch") or "").strip() or "main"

    async def _market_sources(self, repo: str, path: str) -> list:
        """读市场仓库里的第三方来源清单（每行一个「用户名/仓库名」）。

        清单放在仓库里而不是用户配置里，是为了让别人能提 PR 加一行就上架 ——
        改的是市场仓库、不是每个用户的本机配置。读的是仓库的默认分支，与
        市场索引同一套口径；写回市场仓库自己的行会被剔掉，免得扫两遍。
        """
        from modules.market import RAW_TMPL, parse_market_repos
        branch = await self._market_default_branch(repo)
        url = RAW_TMPL.format(repo=repo, branch=branch, path=path)
        try:
            text, _ = await self._github_fetch(url, "text")
        except Exception as e:
            print(f"[插件市场] 来源清单 {path} 读不到：{type(e).__name__}: {e}")
            return []
        return [r for r in parse_market_repos(text)
                if r.lower() != repo.lower()][:MARKET_SOURCE_REPOS_MAX]


    async def _market_releases(self, repo: str, pids) -> list:
        """仓库里属于这些插件的 Release 列表，顺带补上点赞数。

        下载量与收藏量都来自 Release：列表接口不给点赞总数，所以只对确实
        属于这些插件的 Release 单独查一次，并且设上限，别把配额烧光。
        """
        from modules.market import count_reactions, release_for_plugin
        try:
            data, _ = await self._fetch_releases(
                f"https://api.github.com/repos/{repo}/releases?per_page=100")
        except Exception as e:
            print(f"[插件市场] 拿不到 {repo} 的 Release：{type(e).__name__}: {e}")
            return []
        wanted = [str(p) for p in (pids or []) if str(p or "").strip()]
        live = [r for r in (data if isinstance(data, list) else [])
                if isinstance(r, dict) and not r.get("draft")]
        targets = [r for r in live
                   if any(release_for_plugin(r, p) for p in wanted)]
        for rel in targets[:MAX_REACTION_LOOKUPS]:
            rid = rel.get("id")
            if not rid:
                continue
            try:
                rx, _ = await self._fetch_releases(
                    f"https://api.github.com/repos/{repo}/releases/{rid}"
                    "/reactions?per_page=100")
                rel["_reactions"] = count_reactions(rx)
            except Exception:
                rel["_reactions"] = 0
        return live


    async def _market_index_entries(self, repo: str, market_path: str) -> dict:
        """读市场仓库里的 index.json 索引（官方市场的条目来源）。

        索引条目要带 `source_repo`（或 `repo`）：安装时会拿它校验"插件是不是
        真从这个仓库来的"。没写就按 `download` 链接的归属推断，再退回归属
        索引自己所在的仓库。下载量与收藏量按插件 id 从 Release 汇总，
        download 优先用 Release 里的 zip 附件。
        """
        from modules.market import plugin_category, summarize_plugin_releases
        from modules.plugins import repo_slug
        branch = await self._market_default_branch(repo)
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/{market_path}"
        try:
            data, insecure = await self._fetch_releases(url)
        except Exception as e:
            return {"entries": [], "insecure": False,
                    "warnings": [f"市场索引 {market_path} 读不到（{type(e).__name__}）"]}
        entries = data if isinstance(data, list) else (
            data.get("plugins") if isinstance(data, dict) else None)
        out = []
        for raw in (entries or []):
            if not isinstance(raw, dict):
                continue
            pid = str(raw.get("id") or "").strip()
            if not pid:
                continue
            item = {k: raw.get(k) for k in
                    ("name", "version", "author", "description", "type",
                     "download", "homepage", "page_url", "tags")}
            item["id"] = pid
            item["source"] = "index"
            item.setdefault("branch", "")
            item["category"] = plugin_category(raw)
            item["logo_url"] = str(raw.get("logo_url") or "")
            item["icon_url"] = str(raw.get("icon_url") or "")
            item["github"] = str(raw.get("github") or "")
            item["source_repo"] = (
                str(raw.get("source_repo") or raw.get("repo") or "").strip()
                or repo_slug(item.get("download")) or repo)
            out.append(item)
        releases = await self._market_releases(repo, [i["id"] for i in out])
        for item in out:
            one = summarize_plugin_releases(releases, item["id"])
            item["downloads"] = int(one["downloads"] or 0)
            item["favorites"] = int(one["favorites"] or 0)
            item["download"] = str(one["asset_url"] or item.get("download") or "")
            item["release_tag"] = str(one["release_tag"] or "")
            item["released_at"] = float(one["released_at"] or 0.0)
            item["updated_at"] = float(one["released_at"] or 0.0)
        return {"entries": out, "warnings": [], "insecure": insecure}


    async def _market_scan_repos(self, repos, prefix: str,
                                 market_path: str = "") -> dict:
        """逐个仓库取插件条目并合并（来源清单与第三方市场用）。

        每个仓库先读索引（「发布到市场」写的就是它），索引里没有条目时再按
        分支扫 —— 两种上架方式都能被扫到。单个仓库出错只记一条 warning，
        不牵连别的仓库：地址是用户自己填的，写错一个不该让整个市场打不开。
        """
        index_path = str(market_path or "").strip() or "plugins/index.json"
        entries, warnings, insecure = [], [], False
        for repo in repos:
            try:
                got = await self._market_index_entries(repo, index_path)
                insecure = insecure or bool(got.get("insecure"))
                warns = list(got.get("warnings") or [])
                if not got["entries"]:
                    # 索引里没有条目：这个仓库可能是按分支上架的，回退扫分支。
                    # 分支里有插件时就不再提索引读不到 —— 那是这种上架方式的正常情况
                    by_branch = await self._market_branch_entries(repo, prefix)
                    insecure = insecure or bool(by_branch.get("insecure"))
                    if by_branch["entries"]:
                        warns = []
                    got = by_branch
                    warns += list(by_branch.get("warnings") or [])
            except Exception as e:
                warnings.append(f"{repo}：拉取失败（{type(e).__name__}）")
                continue
            entries += got["entries"]
            warnings += [w if w.startswith(repo) else f"{repo}：{w}" for w in warns]
        return {"entries": entries, "warnings": warnings, "insecure": insecure}


    async def _plugins_market_payload(self, force: bool = False, sort: str = "latest",
                                      market: str = "official") -> dict:
        """插件市场数据：扫索引/分支、按排序键返回（条目缓存 30 分钟）。

        market = official 读官方市场仓库里的索引（索引里没有条目时回退扫它的
        分支），再叠加来源清单里的仓库；thirdparty 按 plugin_market_thirdparty
        逐行列出的仓库扫分支。
        """
        from modules.market import SORT_KEYS, parse_market_repos, sort_entries
        kind = "thirdparty" if str(market or "").strip().lower() == "thirdparty" \
            else "official"
        raw_sources = str(self.config.get("plugin_market_thirdparty", "") or "")
        repos = parse_market_repos(raw_sources) if kind == "thirdparty" else [
            str(self.config.get("plugin_market_repo", "") or "").strip()
            or "slpk1ng/Lovomo"]
        prefix = str(self.config.get("plugin_market_branch_prefix", "") or "").strip() \
            or "lovomo_plugin"
        market_path = str(self.config.get("plugin_market_path", "") or "").strip() \
            or "plugins/index.json"
        sources_path = str(self.config.get("plugin_market_sources_path", "") or "").strip()
        sort_key = str(sort or "latest").strip().lower()
        if sort_key not in SORT_KEYS:
            sort_key = "latest"

        caches = self._plugin_market_cache
        cache = caches.get(kind + "|" + ",".join(repos)) or {}
        now = time.time()
        if (not force and cache.get("entries") is not None
                and now - float(cache.get("fetched_at") or 0) < 1800):
            entries = cache["entries"]
            warnings = list(cache.get("warnings") or [])
            source = cache.get("source") or "branches"
            insecure = bool(cache.get("insecure"))
            error = str(cache.get("error") or "")
        else:
            source, warnings, insecure, error = "branches", [], False, ""
            entries = []
            if kind == "thirdparty" and not repos:
                warnings.append("还没有配置第三方市场地址，"
                                "按每行一个「用户名/仓库名」填好再刷新")
            elif kind == "thirdparty":
                got = await self._market_scan_repos(repos, prefix, market_path)
                entries, warnings = got["entries"], got["warnings"]
                insecure = bool(got.get("insecure"))
            else:
                repo = repos[0]
                try:
                    index = await self._market_index_entries(repo, market_path)
                    if index["entries"]:
                        entries = index["entries"]
                        warnings = warnings + list(index.get("warnings") or [])
                        source = "index"
                    else:
                        # 自建市场仍可能只靠分支上架：索引里没东西时回退扫分支
                        got = await self._market_branch_entries(repo, prefix)
                        entries, warnings = got["entries"], got["warnings"]
                        source = "branches"
                        insecure = insecure or bool(got.get("insecure"))
                        if not entries:
                            warnings = warnings + list(index.get("warnings") or [])
                    insecure = insecure or bool(index.get("insecure"))
                    if sources_path:
                        extra = await self._market_scan_repos(
                            await self._market_sources(repo, sources_path),
                            prefix, market_path)
                        entries = entries + extra["entries"]
                        warnings = warnings + extra["warnings"]
                        insecure = insecure or bool(extra.get("insecure"))
                except Exception as e:
                    error = f"拉取市场失败：{type(e).__name__}: {e}"
                    print(f"[插件市场] {error}")
            for stale in [k for k, v in caches.items()
                          if now - float(v.get("fetched_at") or 0) >= 1800]:
                caches.pop(stale, None)
            caches[kind + "|" + ",".join(repos)] = {
                "entries": entries, "fetched_at": now, "source": source,
                "warnings": warnings, "insecure": insecure, "error": error}

        installed = {p["id"]: p for p in self.plugin_manager.list_plugins()}
        plugins = []
        for e in entries:
            item = dict(e)
            cur = installed.get(item.get("id") or "")
            item["installed"] = bool(cur)
            item["installed_version"] = (cur or {}).get("version", "")
            item["enabled"] = bool((cur or {}).get("enabled"))
            plugins.append(item)
        plugins = sort_entries(plugins, sort_key)
        if warnings:
            print("[插件市场] " + "；".join(warnings[:5]))
        result = {"success": not error, "plugins": plugins, "count": len(plugins),
                  "sort": sort_key, "source": source, "market": kind,
                  "market_text": raw_sources, "markets": repos,
                  "repo": "、".join(repos), "branch_prefix": prefix,
                  "warnings": warnings, "insecure": insecure, "fetched_at": now,
                  "url": f"https://github.com/{repos[0]}" if repos else ""}
        if error:
            result["error"] = error
            result["hint"] = ("可以先手动上传插件 zip 安装；"
                              "GitHub 未登录访问接口有 60 次/小时的限制，"
                              "过一会儿再刷新即可。")
        return result

    async def handle_plugins_market(self, request):
        force = str(request.query.get("refresh") or "") in ("1", "true")
        sort = str(request.query.get("sort") or "latest")
        market = str(request.query.get("market") or "official")
        return web.json_response(
            await self._plugins_market_payload(force, sort, market))

    async def _plugins_updates_payload(self, force: bool = False) -> dict:
        """已安装插件里，市场上有更新版本的那些。

        版本号按版本段比较（modules.updater.is_newer），市场里同一个插件出现
        在多条来源时取版本最高的那一条。
        """
        from modules.updater import is_newer
        installed = self.plugin_manager.list_plugins()
        found = {}
        warnings = []
        errors = []
        for kind in ("official", "thirdparty"):
            market = await self._plugins_market_payload(force=force, market=kind)
            warnings.extend(market.get("warnings") or [])
            if market.get("error"):
                errors.append(str(market["error"]))
            for entry in market.get("plugins") or []:
                pid = str(entry.get("id") or "")
                if not pid:
                    continue
                entry = dict(entry, market_kind=kind)
                best = found.get(pid)
                if best is None or is_newer(entry.get("version"), best.get("version")):
                    found[pid] = entry
        updates = []
        for cur in installed:
            pid = str(cur.get("id") or "")
            entry = found.get(pid)
            if not entry or not is_newer(entry.get("version"), cur.get("version")):
                continue
            updates.append({
                "id": pid,
                "name": entry.get("name") or cur.get("name") or pid,
                "installed_version": str(cur.get("version") or ""),
                "version": str(entry.get("version") or ""),
                "description": str(entry.get("description") or ""),
                "author": str(entry.get("author") or ""),
                "market": entry.get("market_kind") or "official",
                "download": str(entry.get("download") or ""),
                "release_tag": str(entry.get("release_tag") or ""),
                "enabled": bool(cur.get("enabled")),
            })
        result = {"success": True, "count": len(updates), "plugins": updates,
                  "warnings": warnings}
        if errors and not updates:
            # 市场都没拉到就别报「全部最新」：那会让用户以为已经检查过了
            result["success"] = False
            result["error"] = "；".join(errors[:2])
        return result

    async def handle_plugins_updates(self, request):
        force = str(request.query.get("refresh") or "") in ("1", "true")
        try:
            return web.json_response(await self._plugins_updates_payload(force))
        except Exception as e:
            return web.json_response(
                {"success": False, "plugins": [], "count": 0,
                 "error": f"检查插件更新失败：{type(e).__name__}: {e}"}, status=400)

    async def handle_plugins_market_sources(self, request):
        """保存第三方市场地址（每行一个「用户名/仓库名」）。"""
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "请求体不是合法 JSON"},
                                     status=400)
        from modules.market import parse_market_repos
        text = str(payload.get("text") or "").strip()
        markets = parse_market_repos(text)
        self.config.config["plugin_market_thirdparty"] = text
        self.config._atomic_save(self.config.config)
        self._plugin_market_cache.clear()
        print(f"[插件市场] 第三方市场地址已保存：识别到 {len(markets)} 个"
              + (f"（{'、'.join(markets)}）" if markets else ""))
        return web.json_response({"success": True, "markets": markets,
                                  "count": len(markets), "text": text})

    async def handle_plugins_readme(self, request):
        """插件包内自带文档的内容（kind = readme / update）。"""
        pid = str(request.query.get("id") or "").strip()
        kind = str(request.query.get("kind") or "readme").strip().lower()
        if kind not in ("readme", "update"):
            return web.json_response({"success": False, "error": "kind 只能是 readme 或 update"},
                                     status=400)
        if not self.plugin_manager.get(pid):
            return web.json_response({"success": False, "error": "插件不存在"}, status=404)
        doc = self.plugin_manager.read_doc_text(pid, kind)
        return web.json_response({"success": True, "id": pid, "kind": kind, **doc})

    async def handle_releases(self, request):
        """仓库的全部 Releases（含各版本的资源与下载量）。"""
        from modules.updater import is_prerelease_version
        repo = str(self.config.get("plugin_release_repo", "") or "").strip() \
            or "slpk1ng/Lovomo"
        force = str(request.query.get("refresh") or "") in ("1", "true")
        cache = self._release_list_cache
        now = time.time()
        if (not force and cache.get("data") is not None
                and now - float(cache.get("fetched_at") or 0) < 1800):
            return web.json_response(cache["data"])
        try:
            data, insecure = await self._fetch_releases(
                f"https://api.github.com/repos/{repo}/releases?per_page=100")
            items = data if isinstance(data, list) else []
            releases = []
            for rel in items:
                if not isinstance(rel, dict):
                    continue
                assets = []
                for a in (rel.get("assets") or []):
                    if not isinstance(a, dict):
                        continue
                    assets.append({
                        "name": str(a.get("name") or "")[:120],
                        "size": int(a.get("size") or 0),
                        "downloads": int(a.get("download_count") or 0),
                        "url": str(a.get("browser_download_url") or ""),
                    })
                tag = str(rel.get("tag_name") or "")
                releases.append({
                    "tag": tag,
                    "name": str(rel.get("name") or tag or ""),
                    "draft": bool(rel.get("draft")),
                    # 只看 GitHub 的 prerelease 标记会漏：作者常把 -beta 只写进 tag，
                    # 没勾「预发布」，测试版就在历史版本页被标成正式版
                    "prerelease": bool(rel.get("prerelease")) or is_prerelease_version(tag),
                    "published_at": str(rel.get("published_at") or ""),
                    "created_at": str(rel.get("created_at") or ""),
                    "body": str(rel.get("body") or "")[:4000],
                    "url": str(rel.get("html_url") or ""),
                    "downloads": sum(x["downloads"] for x in assets),
                    "assets": assets,
                })
            result = {"success": True, "repo": repo, "releases": releases,
                      "count": len(releases), "insecure": insecure,
                      "fetched_at": now}
        except Exception as e:
            result = {"success": False, "repo": repo, "releases": [],
                      "error": f"拿不到 Releases：{type(e).__name__}: {e}",
                      "fetched_at": now}
            print(f"[历史更新] {result['error']}")
        cache.update({"data": result, "fetched_at": now})
        return web.json_response(result)

    async def handle_release_download(self, request):
        """下载 Release 资源并保存到本地（由服务端完成下载）。"""
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "参数错误"}, status=400)
        url = str(payload.get("url") or "").strip()
        name = str(payload.get("name") or "").strip() or "download.bin"
        repo = str(self.config.get("plugin_release_repo", "") or "").strip() \
            or "slpk1ng/Lovomo"
        # 只允许下本仓库 Release 的资源
        head = url.lower()
        allowed = (
            head.startswith(f"https://github.com/{repo.lower()}/releases/download/")
            or head.startswith("https://objects.githubusercontent.com/")
            or head.startswith("https://github-releases.githubusercontent.com/")
        )
        if not allowed:
            return web.json_response(
                {"success": False, "error": "只允许下载本仓库 Releases 里的资源"},
                status=400)
        try:
            content, insecure = await self._fetch_bytes(url)
        except Exception as e:
            return web.json_response({"success": False,
                                      "error": f"下载失败：{type(e).__name__}: {e}"},
                                     status=400)
        safe = safe_asset_name(name, Path(name).suffix)
        mode = await self._save_export_via_dialog(safe, content)
        if mode == "cancelled":
            return web.json_response({"success": False, "mode": "cancelled"})
        if mode != "browser":
            print(f"已保存 Release 资源：{safe} → {mode}")
            return web.json_response({"success": True, "mode": "saved",
                                      "path": mode, "name": safe})
        # 浏览器访问：没有系统保存框，直接回流给浏览器下载
        from urllib.parse import quote as _quote
        return web.Response(
            body=content, content_type="application/octet-stream",
            headers={"Content-Disposition":
                     f"attachment; filename*=UTF-8''{_quote(safe)}"})

    async def _fetch_bytes(self, url: str):
        """下载二进制内容，返回 (bytes, insecure)。"""
        return await self._github_fetch(url, "bytes", timeout=120,
                                        headers={"User-Agent": "lovomo-release-download"})



    @staticmethod
    def _check_market_source(entry: dict, manifest: dict) -> str:
        """校验"这个包是不是真来自市场条目记录的仓库"。

        返回空串表示放行，否则返回给用户看的拒绝原因。

        规则：
        - 包内清单**一个来源都没声明**（github / repo / source_repo /
          homepage / author 全空）→ 放行。全新插件本来就无从比对，
          不能因为作者没写就把人挡在门外。
        - 声明了来源，且能对上市场仓库（仓库名一致，或归属用户名一致）→ 放行。
        - 声明了来源却对不上 → 拒绝。这正是"把别人的插件换个壳挂到自己
          仓库上"的特征。
        """
        from modules.plugins import manifest_logins, repo_owner, repo_slug
        want = repo_slug((entry or {}).get("source_repo"))
        if not want:
            return ""
        owners = {v.lower() for v in manifest_logins(manifest)}
        if not owners:
            return ""
        declared = repo_slug(manifest.get("repo") or manifest.get("source_repo"))
        if declared and declared.lower() == want.lower():
            return ""
        if repo_owner(want).lower() in owners:
            return ""
        return (f"插件声明的来源（{'、'.join(sorted(owners))}）与市场仓库 {want} "
                "不一致，已拒绝安装。插件只能从作者本人的仓库安装，"
                "如果你就是作者，请在清单里写上自己的 github / repo。")

    async def handle_plugins_install_remote(self, request):
        """从市场索引里下载并安装（同样过一遍安全审查）。"""
        try:
            payload = await request.json()
            pid = str(payload.get("id") or "").strip()
            force = bool(payload.get("force", False))
            entry = None
            for kind in ("official", "thirdparty"):
                market = await self._plugins_market_payload(market=kind)
                entry = next((p for p in market.get("plugins", []) if p["id"] == pid), None)
                if entry:
                    break
            if not entry:
                return web.json_response({"success": False, "error": "市场里没有这个插件"},
                                         status=404)
            url = entry.get("download") or ""
            if not url.startswith(("http://", "https://")):
                return web.json_response(
                    {"success": False, "error": "该插件没有提供有效的下载地址"}, status=400)
            try:
                content, _ = await self._github_fetch(url, "bytes", timeout=60)
            except Exception as e:
                return web.json_response(
                    {"success": False, "error": f"下载失败：{type(e).__name__}"}, status=400)
            report = self.plugin_manager.inspect_zip(content)
            if not report.get("ok"):
                return web.json_response(
                    {"success": False, "error": report.get("error") or "包不可用",
                     "report": report}, status=400)
            manifest = report.get("manifest") or {}
            got_id = str(manifest.get("id") or "").strip()
            if got_id and got_id != pid:
                return web.json_response(
                    {"success": False,
                     "error": f"包内插件 id（{got_id}）与市场条目（{pid}）不一致，已拒绝安装",
                     "report": report}, status=400)
            why = self._check_market_source(entry, manifest)
            if why:
                print(f"[插件市场] 拒绝安装 {pid}：{why}")
                return web.json_response({"success": False, "error": why,
                                          "report": report}, status=400)
            result = self.plugin_manager.install_zip(content, force=force)
            if result.get("success"):
                # 记一笔来源，便于事后追溯插件是从哪条仓库/分支装来的
                self.plugin_manager.set_source(result["id"],
                                               entry.get("source_repo") or "",
                                               entry.get("branch") or "",
                                               entry.get("source") or "")
                print(f"[插件市场] 已安装：{result.get('name')} (id={result.get('id')})")
            return web.json_response(result,
                                     status=200 if result.get("success") else 400)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_plugins_publish_token(self, request):
        """保存 GitHub PAT。配置落地走标准加密落盘流程。"""
        try:
            payload = await request.json()
            token = str(payload.get("token") or "").strip()
        except Exception:
            return web.json_response({"success": False, "error": "请求体不是合法 JSON"}, status=400)
        if not token:
            return web.json_response({"success": False, "error": "Token 不能为空"}, status=400)
        try:
            info = await _verify_github_token(token)
        except _PublishError as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)
        self.config.config["plugin_publish_token"] = token
        self.config.config["plugin_publish_login"] = info.get("login", "")
        self.config.config["plugin_publish_name"] = info.get("name", "")
        self.config._atomic_save(self.config.config)
        return web.json_response({"success": True, "login": info.get("login", ""),
                                  "name": info.get("name", "")})

    async def handle_plugins_publish_token_clear(self, request):
        self.config.config.pop("plugin_publish_token", None)
        self.config.config.pop("plugin_publish_login", None)
        self.config.config.pop("plugin_publish_name", None)
        self.config._atomic_save(self.config.config)
        return web.json_response({"success": True})

    def _publish_state_get(self) -> dict:
        state = getattr(self, "_publish_state", None)
        if not isinstance(state, dict):
            state = {"published": {}, "unpublished": {}}
            self._publish_state = state
        for key in ("published", "unpublished"):
            if not isinstance(state.get(key), dict):
                state[key] = {}
        return state

    def _load_publish_state(self):
        """读本机的推送记录。读不出来就沿用空记录，并且本次不再回写，
        免得一次读取失败就把磁盘上完好的记录覆盖掉。"""
        from modules.jsonio import load_json_ex
        data, readable = load_json_ex(self._publish_state_file,
                                      {"published": {}, "unpublished": {}})
        self._publish_state_readable = readable
        state = {"published": {}, "unpublished": {}}
        if isinstance(data, dict):
            for key in state:
                got = data.get(key)
                if isinstance(got, dict):
                    state[key] = got
        self._publish_state = state

    def _save_publish_state(self):
        path = getattr(self, "_publish_state_file", None)
        if path is None or not getattr(self, "_publish_state_readable", True):
            return
        from modules.jsonio import save_json
        now = time.time()
        fresh = {"published": {}, "unpublished": {}}
        for key in fresh:
            for pid, rec in self._publish_state_get()[key].items():
                if not isinstance(rec, dict) or not pid:
                    continue
                if now - float(rec.get("ts") or 0) > PUBLISH_STATE_TTL:
                    continue
                fresh[key][pid] = rec
        self._publish_state = fresh
        save_json(path, fresh)

    def _publish_memory(self) -> dict:
        """本机记下的「已推送」版本 {插件 id: 版本}，过期的不算。"""
        now = time.time()
        out = {}
        for pid, rec in self._publish_state_get()["published"].items():
            if not isinstance(rec, dict) or now - float(rec.get("ts") or 0) > PUBLISH_STATE_TTL:
                continue
            version = str(rec.get("version") or "")
            if pid and version:
                out[str(pid)] = version
        return out

    def _unpublish_memory(self) -> set:
        """本机记下的「已下架」插件 id，过期的不算。"""
        now = time.time()
        out = set()
        for pid, rec in self._publish_state_get()["unpublished"].items():
            if not isinstance(rec, dict) or now - float(rec.get("ts") or 0) > PUBLISH_STATE_TTL:
                continue
            if pid:
                out.add(str(pid))
        return out

    def _remember_published(self, pid: str, version: str):
        state = self._publish_state_get()
        state["published"][pid] = {"version": str(version or ""), "ts": time.time()}
        state["unpublished"].pop(pid, None)
        self._save_publish_state()

    def _remember_unpublished(self, pid: str):
        state = self._publish_state_get()
        state["unpublished"][pid] = {"ts": time.time()}
        state["published"].pop(pid, None)
        self._save_publish_state()

    async def _published_versions(self, payload: dict = None) -> dict:
        """已上架插件的版本号 {插件 id: 版本}。

        官方市场的索引由「发布」写入，是权威来源；来源清单里的仓库仍靠
        分支与版本标签判定（分支被删掉时标签仍在，两边取版本较大的那个）。
        索引与镜像都有缓存，所以这里不吃条目缓存（payload 由调用方传入，
        免得一次请求里重复拉两轮）；本机刚推送/刚下架的记录最后叠加，
        撑住缓存还没刷新的那段时间。
        """
        from modules.plugin_publisher import parse_version_tag
        from modules.updater import is_newer
        out = {}
        try:
            if payload is None:
                payload = await self._plugins_market_payload(force=True)
            for p in (payload.get("plugins") or []):
                pid = str(p.get("id") or "")
                if pid:
                    out[pid] = str(p.get("version") or "")
        except Exception as e:
            print(f"[插件发布] 读取已上架版本失败：{type(e).__name__}: {e}")
        for pid, version in self._publish_memory().items():
            if pid not in out or is_newer(version, out[pid]):
                out[pid] = version
        repo = str(self.config.get("plugin_market_repo", "") or "").strip() \
            or "slpk1ng/Lovomo"
        prefix = str(self.config.get("plugin_market_branch_prefix", "") or "").strip() \
            or "lovomo_plugin"
        sources_path = str(self.config.get("plugin_market_sources_path", "") or "").strip()
        repos = [repo]
        if sources_path:
            repos += [r for r in await self._market_sources(repo, sources_path)
                      if r not in repos]
        for one in repos:
            try:
                for tag in await self._market_tags(one, prefix):
                    pid, version = parse_version_tag(tag, prefix)
                    if pid and (pid not in out or is_newer(version, out[pid])):
                        out[pid] = version
            except Exception as e:
                print(f"[插件发布] 读取版本标签失败（{one}）：{type(e).__name__}: {e}")
        for pid in self._unpublish_memory():
            out.pop(pid, None)
        return out

    @staticmethod
    def _market_entry_owned_by(entry, login: str) -> bool:
        """市场索引条目是否属于这个 GitHub 账号（下架列表用）。

        索引条目没有「已装插件」那套 owners 字段，只能看它自己声明的
        github / author / source_repo 仓库主。
        """
        who = str(login or "").strip().lower()
        if not who:
            return False
        cands = [str((entry or {}).get("github") or ""),
                 str((entry or {}).get("author") or "")]
        repo = str((entry or {}).get("source_repo") or "")
        if "/" in repo:
            cands.append(repo.split("/", 1)[0])
        return any(c.strip().lower() == who for c in cands if c.strip())

    async def _published_entries(self, login: str, market_path: str,
                                 payload: dict = None) -> list:
        """已上架的、属于当前账号的插件条目（下架列表的数据来源）。

        市场索引有缓存，所以这里不吃条目缓存；本机刚下架的从列表里剔掉，
        本机刚推送、索引还没更新的补进来（否则刚发布完没有「下架」按钮）。
        """
        from modules.market import plugin_category, plugin_folder
        try:
            if payload is None:
                payload = await self._plugins_market_payload(force=True)
        except Exception as e:
            print(f"[插件下架] 读取已上架列表失败：{type(e).__name__}: {e}")
            payload = {}
        dropped = self._unpublish_memory()
        out = []
        for entry in (payload.get("plugins") or []):
            if not isinstance(entry, dict) or not self._market_entry_owned_by(entry, login):
                continue
            pid = str(entry.get("id") or "").strip()
            if not pid or pid in dropped:
                continue
            category = plugin_category(entry)
            out.append({
                "id": pid,
                "name": str(entry.get("name") or pid),
                "version": str(entry.get("version") or ""),
                "category": category,
                "folder": str(entry.get("folder") or "").strip().strip("/")
                          or plugin_folder(market_path, category, pid),
            })
        seen = {e["id"] for e in out}
        manager = getattr(self, "plugin_manager", None)
        for pid, version in self._publish_memory().items():
            if pid in seen or pid in dropped:
                continue
            info = manager.get(pid) if manager else None
            category = plugin_category(info or {})
            out.append({
                "id": pid,
                "name": str((info or {}).get("name") or pid),
                "version": version,
                "category": category,
                "folder": plugin_folder(market_path, category, pid),
            })
        out.sort(key=lambda e: e["id"])
        return out

    def _publish_scope(self, login: str, published: dict = None):
        """按 GitHub 登录身份把已装插件分成"能发布"和"被挡下"两拨。

        归属校验必须在服务端做：前端隐藏只是顺手，真正的门禁在这里 ——
        直接 POST /api/plugins/publish 也绕不过去。登录名为空（还没认证）
        时全部归入"被挡下"，也就是"未认证就不参与插件制作"。

        published 是已上架插件的版本号。本地版本不高于已上架版本（重复发布、
        本地是 beta、或线上已经更高）时标成 needs_publish=False，前端据此把
        「发布」换成「当前版本已发布过」。拿不到已上架版本时不挡人。
        """
        from modules.market import plugin_category
        from modules.plugins import ownership_matches
        from modules.updater import is_newer
        published = published or {}
        owned, blocked = [], []
        manager = getattr(self, "plugin_manager", None)
        plugins = manager.list_plugins() if manager else []
        for info in plugins:
            item = {"id": info["id"], "name": info.get("name") or info["id"],
                    "version": info.get("version") or "",
                    "category": plugin_category(info),
                    "owners": list(info.get("owners") or [])}
            if login and ownership_matches(info, login):
                was = str(published.get(item["id"]) or "")
                item["published_version"] = was
                item["needs_publish"] = bool(
                    not was or not item["version"]
                    or is_newer(item["version"], was))
                owned.append(item)
            else:
                blocked.append(item)
        return owned, blocked

    async def handle_plugins_publish_status(self, request):
        token = str(self.config.config.get("plugin_publish_token") or "").strip()
        repo = str(self.config.config.get("plugin_market_repo") or "").strip()
        login = str(self.config.config.get("plugin_publish_login") or "").strip()
        market_path = str(self.config.config.get("plugin_market_path") or "").strip() \
            or "plugins/index.json"
        from modules.market import plugin_folder
        # 发布面板是决策面：不吃 30 分钟的市场条目缓存（别人在 GitHub 上改过
        # 市场就得立刻看到），一次请求只强制拉一轮，两个列表共用
        payload = {}
        if login:
            try:
                payload = await self._plugins_market_payload(force=True)
            except Exception as e:
                print(f"[插件发布] 读取市场失败：{type(e).__name__}: {e}")
        published = await self._published_versions(payload) if login else {}
        owned, blocked = self._publish_scope(login, published)
        for p in owned:
            p["folder"] = plugin_folder(market_path, p.get("category"), p["id"])
        on_market = await self._published_entries(login, market_path, payload) if login else []
        info = {
            # 只有「Token 在 + 身份校验过」才算认证完成：缺一个都退回
            # 让用户重新填 Token，免得出现"显示已登录却什么都发不了"
            "configured": bool(token and login),
            "login": login,
            "name": str(self.config.config.get("plugin_publish_name") or ""),
            "repo": repo,
            "market_path": market_path,
            "publishable": [p["id"] for p in owned],
            "plugins": owned,
            "blocked": blocked,
            "published": on_market,
        }
        return web.json_response(info)

    async def handle_plugins_publish(self, request):
        """把 plugins/sources/<id>/ 写进市场仓库的分类目录并更新索引。"""
        try:
            payload = await request.json()
            pid = str(payload.get("id") or "").strip()
            message = str(payload.get("message") or "").strip() or None
        except Exception:
            return web.json_response({"success": False, "error": "请求体不是合法 JSON"}, status=400)
        if not pid:
            return web.json_response({"success": False, "error": "缺少插件 id"}, status=400)
        token = str(self.config.config.get("plugin_publish_token") or "").strip()
        repo = str(self.config.config.get("plugin_market_repo") or "").strip()
        market_path = str(self.config.config.get("plugin_market_path") or "").strip()
        login = str(self.config.config.get("plugin_publish_login") or "").strip()
        if not token or not login:
            return web.json_response({"success": False,
                                      "error": "尚未认证 GitHub 身份，请先在「插件」→「发布到市场」里填写 Personal Access Token"},
                                     status=400)
        if not repo:
            return web.json_response({"success": False,
                                      "error": "尚未配置插件市场仓库"}, status=400)
        from modules.plugins import ownership_matches
        from modules.updater import is_newer
        manager = getattr(self, "plugin_manager", None)
        info = manager.get(pid) if manager else None
        if info is None:
            return web.json_response({"success": False, "error": f"插件 {pid} 不存在"},
                                     status=404)
        if not ownership_matches(info, login):
            owners = "、".join(info.get("owners") or []) or "未声明"
            reason = (f"插件「{info.get('name') or pid}」声明的归属是 {owners}，"
                      f"与当前登录的 GitHub 账号 {login} 不符，已拒绝发布。"
                      "要发布请先在插件清单（plugin.yaml）里写上自己的 "
                      "github / repo / homepage。")
            print(f"[插件发布] 拒绝发布 {pid}：{reason}")
            return web.json_response({"success": False, "error": reason}, status=403)
        local_version = str(info.get("version") or "").strip()
        was = str((await self._published_versions()).get(pid) or "")
        if was and not is_newer(local_version, was):
            reason = (f"本地版本 {local_version or '(空)'} 不高于已上架的 {was}，"
                      "版本只能往上走；请先改 plugin.yaml 里的 version 再发布。")
            print(f"[插件发布] 拒绝发布 {pid}：{reason}")
            return web.json_response({"success": False, "error": reason}, status=400)
        sources_dir = (self.plugin_manager.root if self.plugin_manager
                       else Path(__file__).resolve().parent / "plugins" / "sources")
        try:
            result = await _publish_to_github(
                token=token, repo=repo, plugin_id=pid,
                sources_dir=sources_dir, message=message,
                market_path=market_path)
        except _PublishError as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)
        print(f"[插件发布] 发布成功：{pid} → {result.get('folder')}"
              f"（提交 {str(result.get('commit') or '')[:7]}，"
              f"{len(result.get('files') or [])} 个文件）")
        if local_version:
            self._remember_published(pid, local_version)
        # 刚推上去的版本还没进市场缓存，清掉才能让发布状态立刻看到新版本
        self._plugin_market_cache.clear()
        return web.json_response({"success": True, "version": local_version, **result})

    async def handle_plugins_unpublish(self, request):
        """把插件从市场下架：删索引条目、插件目录与该插件的版本标签、Release。"""
        try:
            payload = await request.json()
            pid = str(payload.get("id") or "").strip()
        except Exception:
            return web.json_response({"success": False, "error": "请求体不是合法 JSON"}, status=400)
        if not pid:
            return web.json_response({"success": False, "error": "缺少插件 id"}, status=400)
        token = str(self.config.config.get("plugin_publish_token") or "").strip()
        repo = str(self.config.config.get("plugin_market_repo") or "").strip()
        market_path = str(self.config.config.get("plugin_market_path") or "").strip()
        if not token:
            return web.json_response({"success": False,
                                      "error": "尚未认证 GitHub 身份，请先在「插件」→「发布到市场」里填写 Personal Access Token"},
                                     status=400)
        if not repo:
            return web.json_response({"success": False,
                                      "error": "尚未配置插件市场仓库"}, status=400)
        try:
            result = await _unpublish_from_github(
                token=token, repo=repo, plugin_id=pid, market_path=market_path)
        except _PublishError as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)
        print(f"[插件下架] 已下架：{pid}（目录 {result.get('folder')}，"
              f"删除 {len(result.get('removed') or [])} 个文件、"
              f"{len(result.get('tags') or [])} 个版本标签）")
        self._remember_unpublished(pid)
        self._plugin_market_cache.clear()
        return web.json_response({"success": True, **result})

    def _after_config_reload(self):
        """配置变更后的统一热重载。"""
        app_context.global_config = self.config
        apply_log_max_size(self.config)
        self._refresh_auth_state()
        self.config.roles = self.config._parse_roles()
        if not app_context.global_config.active_character or app_context.global_config.active_character not in self.config.roles:
            if self.config.roles:
                app_context.global_config.active_character = list(self.config.roles.keys())[0]
        app_context.global_emotion_manager = EmotionManager(self.config)
        app_context.memory_manager = MemoryManager(self.config)
        if app_context.sender is not None:
            app_context.sender.memory_manager = app_context.memory_manager
        # WebUI 侧持有的也是构造时的快照：不同步的话，改了记忆目录/角色之后
        # 页面上读写的仍是旧目录，只能重启才能自愈
        self.memory_manager = app_context.memory_manager
        self._update_state_file = app_context.memory_manager.data_path / "update_check.json"
        self._auth_file = app_context.memory_manager.data_path / "webui_auth.json"
        hot_reload_managers()

    # ---------------- 文件夹选择 / 模型列表 ----------------
    async def _pick_dialog(self, dialog_kind, file_types=None, allow_multiple=False):
        """弹出系统选择对话框，返回 (ok, path_or_error)。

        对话框是阻塞调用，丢进线程池避免卡住 WebUI 事件循环。
        allow_multiple=True 时按 pywebview 的约定返回多条路径（用 \n 连接）。
        """
        try:
            import webview
        except Exception:
            return False, "pywebview 未安装，无法打开文件选择"
        window = app_context._WEBVIEW_WINDOW_HOLDER.get("window")
        if window is None:
            return False, "该功能仅在 Lovomo 桌面窗口模式下可用（浏览器访问不支持）"
        kind = getattr(webview, dialog_kind)
        kwargs = {}
        if file_types:
            kwargs["file_types"] = tuple(file_types)
        if allow_multiple:
            kwargs["allow_multiple"] = True
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, lambda: window.create_file_dialog(kind, **kwargs))
        except Exception as e:
            return False, f"打开选择对话框失败: {e}"
        if isinstance(result, (list, tuple)):
            return True, "\n".join(str(item) for item in result if item)
        return True, (str(result) if result else "")

    async def handle_pick_folder(self, request):
        """弹出系统"选择文件夹"对话框；仅在 Lovomo 桌面窗口模式下可用。"""
        ok, path = await self._pick_dialog("FOLDER_DIALOG")
        if not ok:
            return web.json_response({"ok": False, "error": path}, status=400)
        return web.json_response({"ok": bool(path), "path": path})

    async def handle_pick_file(self, request):
        """弹出系统"选择文件"对话框，可选把选中的文件复制到某个插件的数据目录。

        payload:
            filter    "图片 (*.png;*.jpg)" 这类过滤器描述，可选
            exts      允许的扩展名列表（不含点），可选
            multiple  true 时允许多选，返回 paths（换行分隔的绝对路径列表）
            plugin_id 给了就把文件复制进该插件 data 目录，返回相对文件名
        """
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        exts = [str(e).lstrip(".").lower() for e in (payload.get("exts") or []) if str(e).strip()]
        desc = str(payload.get("filter") or "文件").strip()
        file_types = (f"{desc} ({';'.join('*.' + e for e in exts)})",)
        if exts:
            file_types += ("所有文件 (*.*)",)
        multiple = bool(payload.get("multiple"))
        ok, path = await self._pick_dialog("OPEN_DIALOG", file_types,
                                           allow_multiple=multiple)
        if not ok:
            return web.json_response({"ok": False, "error": path}, status=400)
        if multiple and not str(payload.get("plugin_id") or "").strip():
            paths = [p for p in str(path or "").split("\n") if p.strip()]
            bad = [p for p in paths
                   if exts and Path(p).suffix.lower().lstrip(".") not in exts]
            if bad:
                return web.json_response(
                    {"ok": False, "error": f"只支持这些格式：{', '.join(exts)}"}, status=400)
            return web.json_response({"ok": True, "paths": paths,
                                      "cancelled": not paths})
        if not path:
            return web.json_response({"ok": False, "path": "", "cancelled": True})
        src = Path(path)
        if exts and src.suffix.lower().lstrip(".") not in exts:
            return web.json_response(
                {"ok": False, "error": f"只支持这些格式：{', '.join(exts)}"}, status=400)
        pid = str(payload.get("plugin_id") or "").strip()
        if not pid:
            return web.json_response({"ok": True, "path": path, "name": src.name,
                                      "cancelled": False})
        data_dir = self.plugin_manager.data_dir(pid)
        if data_dir is None:
            return web.json_response({"ok": False, "error": "插件不存在"}, status=404)
        if src.suffix.lower() not in ALLOWED_ASSET_EXTS:
            return web.json_response(
                {"ok": False, "error": f"不支持的文件类型：{src.suffix}"}, status=400)
        try:
            if src.stat().st_size > MAX_PLUGIN_ASSET_BYTES:
                limit_mb = MAX_PLUGIN_ASSET_BYTES // (1024 * 1024)
                return web.json_response(
                    {"ok": False, "error": f"文件超过 {limit_mb}MB 上限"}, status=400)
            # 保留原文件名（只清非法字符），重名自动编号
            safe = unique_asset_name(data_dir, safe_asset_name(src.name, src.suffix))
            dst = data_dir / safe
            shutil.copyfile(src, dst)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"复制文件失败: {e}"}, status=500)
        return web.json_response({"ok": True, "path": str(dst), "name": safe,
                                  "orig_name": src.name, "cancelled": False})

    async def handle_scan_models(self, request):
        """扫描文件夹里的 .gguf 模型，返回可填入 llm_model_name 的候选标识符。

        候选键依次取 GGUF 元数据 general.name（LM Studio 模型 ID 的主要来源）、
        发布者/模型文件夹名、文件名去量化后缀——按此顺序去重。
        """
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "参数错误"}, status=400)
        folder = str(payload.get("folder", "") or "").strip()
        root = Path(folder)
        if not folder or not root.is_dir():
            return web.json_response({"ok": False, "error": "文件夹不存在"}, status=400)
        models = []
        try:
            for gguf in sorted(root.rglob("*.gguf")):
                if "mmproj" in gguf.name.lower():
                    continue  # 视觉投影适配器（mmproj-* 或 *.mmproj-*），不是主模型
                parts = [p for p in gguf.parent.relative_to(root).parts if p]
                candidates = []
                meta_name = _gguf_general_name(gguf)
                if meta_name:
                    # "Huihui Qwen3.5 9B Abliterated" → huihui-qwen3.5-9b-abliterated
                    candidates.append(re.sub(r"\s+", "-", meta_name).lower())
                    # "Qwen_Qwen3.5 9B" 的下划线是发布者分隔符 → qwen/qwen3.5-9b
                    candidates.append(re.sub(r"\s+", "-", meta_name.replace("_", "/")).lower())
                if len(parts) >= 2:
                    candidates.append(f"{parts[0]}/{parts[1]}".lower())
                if parts:
                    base = re.sub(r"[-_.]?gguf$", "", parts[-1], flags=re.IGNORECASE)
                    candidates.append(base.lower())
                keys, seen = [], set()
                for c in candidates:
                    if c and c not in seen:
                        seen.add(c)
                        keys.append(c)
                models.append({"file": gguf.name, "keys": keys})
            return web.json_response({"ok": True, "models": models})
        except Exception as e:
            return web.json_response({"ok": False, "error": f"扫描失败: {e}"}, status=500)

    async def handle_list_remote_models(self, request):
        """从 LLM 服务拉取可用模型 ID 列表（Ollama 与 OpenAI 兼容服务都支持）。"""
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "参数错误"}, status=400)
        base = str(payload.get("base_url", "") or "").strip().rstrip("/")
        backend = str(payload.get("backend", "ollama") or "ollama")
        if not base:
            return web.json_response({"ok": False, "error": "服务地址为空"}, status=400)
        headers = {}
        api_key = ""
        if payload.get("vision"):
            api_key = str(self.config.get("image_caption_api_key", "") or "")
        api_key = api_key or str(self.config.get("llm_api_key", "") or "")
        if backend != "ollama" and api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        tried = []
        for url in model_list_endpoints(base, backend):
            try:
                async with httpx.AsyncClient(timeout=10, trust_env=False, headers=headers,
                                             verify=verified_context()) as client:
                    resp = await client.get(url)
            except Exception as e:
                tried.append(f"{url} → {type(e).__name__}: {e}")
                continue
            # 服务端会在响应体里写明原因（url error / 模型不存在等）；
            # 只报 raise_for_status 的异常，用户就只看到一个自己从没填过的地址
            if resp.status_code >= 400:
                tried.append(f"{url} → HTTP {resp.status_code} {_brief_response(resp)}")
                continue
            try:
                data = resp.json()
            except Exception:
                tried.append(f"{url} → 响应不是 JSON {_brief_response(resp)}")
                continue
            if backend == "ollama":
                ids = [str(m.get("name") or m.get("model") or "") for m in data.get("models", [])]
            else:
                ids = [str(m.get("id") or "") for m in data.get("data", [])]
            ids = [i for i in ids if i]
            if not ids:
                tried.append(f"{url} → 响应里没有模型列表 {_brief_response(resp)}")
                continue
            return web.json_response({"ok": True, "models": ids})
        error = "获取模型列表失败：\n" + "\n".join(tried)
        if looks_like_full_endpoint(base):
            error += ("\n\n当前地址看起来是带业务路径的接口（…/services/xxx/xxx）。"
                      "这里填「服务根地址」（补上 /v1/models 或 /api/tags 之前的那一段，"
                      "例如 http://127.0.0.1:8080/v1），或者填对话端点的完整地址"
                      "（例如 …/v1/chat/completions），都可以。")
        return web.json_response({"ok": False, "error": error, "tried": tried}, status=502)

    async def handle_save_config(self, request):
        try:
            new_config = await request.json()
            restart_tts = new_config.pop("restart_tts", False)
            for key in _API_KEY_KEYS + _WEBUI_PASSWORD_KEYS:
                masked_value = new_config.get(key)
                if isinstance(masked_value, dict):
                    stored = self.config.config.get(key)
                    if isinstance(stored, dict) and any(
                            _is_masked_value(v) for v in masked_value.values()):
                        new_config[key] = {k: (stored.get(k, "") if _is_masked_value(v) else v)
                                           for k, v in masked_value.items()}
                elif _is_masked_value(masked_value):
                    # 前端提交的是头尾掩码预览（******** 或 abcd****wxyz（N位））：回填真实密钥
                    new_config[key] = self.config.config.get(key, "")
            # WebUI 只提交它渲染出的字段：以【当前生效配置】为底合并，表单字段覆盖。
            # 之前与 default_config() 合并——表单里没有的键（如搜索引擎设置
            # web_search_engine / web_search_url / web_search_custom_engines）会被
            # 默认值顶掉，用户保存过的 SearXNG 接口每次保存配置页都被抹回 bing。
            base_config = self.config.config or {}
            new_config = {**base_config, **new_config}
            self.config.config = new_config
            self.config._atomic_save(new_config)
            try:
                self._after_config_reload()
            except Exception as reload_err:
                return web.json_response(
                    {"success": True,
                     "message": f"配置已保存，但热重载失败（{reload_err}），请重启程序使运行状态与配置一致。"})

            old_config = self._last_saved_config
            tts_changed = False
            if old_config is not None:
                tts_keys = ['client_base_url', 'model_dir', 'ref_audio_root', 'device',
                            'auto_start_tts', 'tts_start_script', 'timeout_seconds',
                            'prompt_text', 'prompt_lang', 'text_lang', 'top_k', 'top_p',
                            'temperature', 'text_split_method', 'batch_size',
                            'batch_threshold', 'split_bucket', 'speed_factor',
                            'fragment_interval', 'streaming_mode', 'seed',
                            'parallel_infer', 'repetition_penalty', 'media_type',
                            'llm_emotion_intensity', 'intensity_to_temperature', 'intensity_to_top_k']
                for key in tts_keys:
                    if old_config.get(key) != new_config.get(key):
                        tts_changed = True
                        break

            force_restart = restart_tts and app_context.global_config.get("auto_start_tts", False)
            tts_restart_message = ""
            if (tts_changed or force_restart) and app_context.global_config.get("auto_start_tts", False):
                valid, error_msg = self.validate_tts_config(app_context.global_config)
                if not valid:
                    print(f"TTS 配置验证失败，跳过重启：{error_msg}")
                    tts_restart_message = f"配置已保存，但 TTS 服务未重启：{error_msg}"
                else:
                    print("检测到 TTS 相关配置变化或用户强制重启，正在重启 TTS 服务...")
                    process_manager.shutdown_all()
                    threading.Thread(target=auto_start_and_switch_tts, args=(app_context.global_config,), daemon=True).start()
                    tts_restart_message = "TTS 服务正在重启，请稍候..."
            elif tts_changed:
                tts_restart_message = "配置已保存，但 auto_start_tts 为 False，不会自动重启 TTS。"
            else:
                tts_restart_message = "TTS 配置未变化或未选择强制重启，无需重启 TTS 服务。"

            napcat_changed = False
            if old_config is not None:
                if (old_config.get('napcat_ws_url') != new_config.get('napcat_ws_url') or
                        old_config.get('napcat_token') != new_config.get('napcat_token') or
                        role_connection_snapshot(old_config) != role_connection_snapshot(new_config)):
                    napcat_changed = True
                # 换模型：登记旧模型，等新模型首次调用成功后由 llm_helpers 自动卸载
                if self.config.get("llm_auto_unload_old", True):
                    from modules.llm_helpers import queue_old_model_unload
                    for key in ("llm_model_name", "image_caption_model_name"):
                        old_m = str(old_config.get(key, "") or "").strip()
                        new_m = str(new_config.get(key, "") or "").strip()
                        if old_m and old_m != new_m:
                            queue_old_model_unload(old_m)
                            print(f"[模型切换] {key}：{old_m} → {new_m}，"
                                  "新模型调用成功后将自动卸载旧模型")

            self._last_saved_config = new_config.copy()
            response = {"success": True}
            if napcat_changed:
                response['message'] = "配置已保存，但 NapCat 连接参数修改需重启程序才能生效。" + tts_restart_message
            else:
                response['message'] = "配置已保存并热重载生效。" + tts_restart_message
            return web.json_response(response)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return web.json_response({"success": False, "error": str(e)}, status=400)

    def validate_tts_config(self, config: ConfigLoader) -> tuple:
        ref_audio_root = config.get("ref_audio_root", "")
        if not ref_audio_root or not Path(ref_audio_root).exists():
            return False, "参考音频根目录无效或不存在，请检查路径后重试"
        model_dir = config.get("model_dir", "")
        if not model_dir or not Path(model_dir).exists():
            return False, "模型文件夹路径无效或不存在，请检查路径后重试"
        return True, ""

    # ---------------- 角色 ----------------
    async def handle_get_roles(self, request):
        roles = []
        for key, role in self.config.roles.items():
            roles.append({
                "character_key": key,
                "character_name": role["character_name"],
                "active": key == self.config.active_character
            })
        return web.json_response({"roles": roles})

    async def handle_save_roles(self, request):
        try:
            new_data = await request.json()
            roles = new_data.get("roles", [])
            active = new_data.get("active_character", "")
            self.config.config["roles"] = roles
            self.config.config["active_character"] = active
            self.config._atomic_save(self.config.config)
            self._after_config_reload()
            return web.json_response({"success": True, "message": "角色配置已保存！"})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_get_logs(self, request):
        """WebUI 日志接口。

        精简模式（默认）先按 webui_log_hide_patterns 剔除噪音行，再回尾部
        webui_log_tail_lines 行；?full=1（前端"显示完整日志"开关）返回内存里
        缓存的全部日志（上限 webui_log_buffer_lines 行），一行都不藏。

        完整模式另有一道 40 万字符的极端上限（约等于上万行），只为了避免
        极端情况下把整个响应撑爆；触发时会返回 truncated=true 并在日志里写明，
        这样"搜索结果只显示前几条"不再可能是 WebUI 截断造成的。
        """
        full = request.query.get("full", "") in ("1", "true", "yes")
        _, tail_lines = _runtime_log_limits()
        with log_lock:
            total = len(global_log_buffer)
            if full:
                logs = "\n".join(global_log_buffer)
            else:
                patterns = _log_hide_patterns()
                # 被隐藏的那一行原本占着一行换行，直接丢掉会留下一个空行，
                # 看起来就是「隐藏内容处莫名其妙空了一片」，所以连同它后面
                # 紧跟的空白行一起去掉。
                kept = []
                blank_after_hidden = False
                for line in global_log_buffer:
                    if _log_hidden(line, patterns):
                        blank_after_hidden = True
                        continue
                    if blank_after_hidden and not line.strip():
                        blank_after_hidden = False
                        continue
                    blank_after_hidden = False
                    kept.append(line)
                logs = "\n".join(kept[-tail_lines:])
        truncated = False
        if full and len(logs) > _LOG_FULL_MAX_CHARS:
            logs = logs[-_LOG_FULL_MAX_CHARS:]
            truncated = True
            print(f"完整日志超过 {_LOG_FULL_MAX_CHARS} 字符上限，已返回尾部内容"
                  f"（可调小 webui_log_buffer_lines 控制日志体积）。")
        return web.json_response({"logs": logs, "total_lines": total,
                                  "tail_lines": tail_lines, "full": full,
                                  "truncated": truncated})

    # ---------------- 聊天记录 ----------------
    async def handle_list(self, request):
        memories = self.memory_manager.list_memories()
        return web.json_response({"memories": memories})

    async def handle_cache_image(self, request):
        """聊天记录里的真实图片（缓存目录按内容哈希命名，只认哈希名防路径穿越）。"""
        if app_context.memory_manager is None:
            return web.json_response({"error": "unavailable"}, status=503)
        name = str(request.query.get("name") or "")
        if not re.fullmatch(r"[0-9a-f]{16}\.(?:png|jpg|jpeg|gif|webp)", name):
            return web.json_response({"error": "bad name"}, status=400)
        path = app_context.memory_manager.data_path / "image_cache" / name
        if not path.is_file():
            return web.json_response({"error": "not found"}, status=404)
        mime = sniff_image_mime(path.read_bytes()) or "application/octet-stream"
        return web.Response(body=path.read_bytes(), content_type=mime,
                            headers={"Cache-Control": "max-age=86400"})

    async def handle_cache_images_clear(self, request):
        """清空图片缓存：缓存文件全删，聊天记录里的引用同步移除，退回文字描述。"""
        if app_context.memory_manager is None:
            return web.json_response({"success": False, "error": "unavailable"}, status=503)
        freed = 0
        count = 0
        cache_dir = app_context.memory_manager.data_path / "image_cache"
        if cache_dir.is_dir():
            for f in cache_dir.iterdir():
                if not f.is_file():
                    continue
                try:
                    freed += f.stat().st_size
                    f.unlink()
                    count += 1
                except OSError:
                    pass
        cleaned = 0
        try:
            for f in app_context.memory_manager.data_path.glob("*.json"):
                if not _is_memory_filename(f.name):
                    continue
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                history = data.get("history") if isinstance(data, dict) else None
                if not isinstance(history, list):
                    continue
                changed = False
                for msg in history:
                    if isinstance(msg, dict) and msg.pop("image", None) is not None:
                        changed = True
                if changed:
                    sid = _memory_session_id(f.name)
                    if sid:
                        app_context.memory_manager.save_session_data(sid, data)
                        cleaned += 1
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)
        return web.json_response({"success": True, "count": count, "freed": freed,
                                  "sessions": cleaned})

    async def handle_character_avatar(self, request):
        """角色头像：数据目录或 ref_audio_root 下的 avatar.*（聊天记录页展示用）。

        没有本地 avatar.* 时退回机器人 QQ 号的头像：QQ 里角色就是这个账号，
        聊天气泡旁边显示的本来就是它的头像。
        """
        candidates = []
        if app_context.memory_manager is not None:
            candidates.append(app_context.memory_manager.data_path)
        root = str(self.config.get("ref_audio_root", "") or "")
        if root:
            candidates.append(Path(root))
        for directory in candidates:
            directory = Path(directory)
            for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                path = directory / f"avatar{ext}"
                if path.is_file():
                    mime = sniff_image_mime(path.read_bytes()) or "image/png"
                    return web.Response(body=path.read_bytes(), content_type=mime,
                                        headers={"Cache-Control": "max-age=300"})
        data = await qq_avatar_bytes(_active_bot_qq())
        if data:
            return web.Response(body=data, content_type=sniff_image_mime(data) or "image/jpeg",
                                headers={"Cache-Control": "max-age=300"})
        return web.json_response({"error": "not found"}, status=404)

    async def handle_user_avatar(self, request):
        """聊天记录里的用户头像：按 QQ 号取真实头像，取不到返回 404（前端退回首字）。"""
        data = await qq_avatar_bytes(request.query.get("user_id", ""))
        if not data:
            return web.json_response({"error": "not found"}, status=404)
        return web.Response(body=data, content_type=sniff_image_mime(data) or "image/jpeg",
                            headers={"Cache-Control": "max-age=600"})

    async def handle_history(self, request):
        try:
            payload = await request.json()
            filename = payload.get("filename", "")
            result = self.memory_manager.get_history(filename)
            return web.json_response(result)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_delete(self, request):
        try:
            payload = await request.json()
            filenames = payload.get("files", [])
            # 清理范围必须在删文件之前算出来：文件名里的会话名把 @ / . 等字符换成了下划线，
            # 照文件名回推的用户 ID 和运行时的对不上（微信 openid 尤其明显），
            # 真实 ID 只能从会话文件里取
            scopes = self._deleted_session_scopes(filenames)
            deleted = []
            for filename in filenames:
                if self.memory_manager.delete_memory_file(filename):
                    deleted.append(filename)
            if deleted:
                private_users = 0
                for filename in deleted:
                    scope = scopes.get(filename)
                    if scope is None:
                        continue
                    if self._purge_session_state(scope):
                        private_users += 1
                if private_users:
                    print(f"已删除 {len(deleted)} 个会话记录：心情、日记、承诺与主动消息状态已清理，"
                          f"{private_users} 位用户的用户画像与关系进度一并清空。")
                else:
                    print(f"已删除 {len(deleted)} 个会话记录：心情、日记、承诺与主动消息状态已清理。")
            return web.json_response({"success": True, "deleted": deleted})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    def _purge_session_state(self, scope) -> bool:
        """清掉一个会话依附的状态，返回它是不是私聊会话。

        私聊就是「和这个人聊过」的全部记录，连用户画像一起清掉，当成从没聊过；
        群聊只清这个群自己的记录：同一个人可能同时在私聊里聊过，
        他在私聊里的关系与用户画像不该被牵连。
        """
        character_key, sid, stype, target_id = scope
        if app_context.mood_mgr is not None:
            app_context.mood_mgr.delete_session(sid)
            app_context.mood_mgr.drop_diary_session(sid)
        forget_proactive_session(sid)
        if app_context.promise_mgr is not None:
            app_context.promise_mgr.drop_session(sid)
        if app_context.recall_mgr is not None:
            app_context.recall_mgr.drop_session(sid)
        if app_context.affection_mgr is not None:
            if stype == "private":
                app_context.affection_mgr.reset(character_key, str(target_id), sid)
            else:
                app_context.affection_mgr.drop_session(character_key, sid)
        if stype != "private":
            return False
        if app_context.profile_mgr is not None:
            app_context.profile_mgr.delete(str(target_id))
        return True

    def _purge_connection_sessions(self, session_ids: list) -> int:
        """删掉这些会话的聊天记录与依附状态，返回清掉的会话数。

        session_channels.json 是「会话属于哪条接入方式」的唯一线索，
        接入方式被删掉后这些会话再也发不出去，记录也不该继续留在界面上。
        """
        if not session_ids:
            return 0
        wanted = {str(sid) for sid in session_ids if str(sid)}
        purged = 0
        for f in app_context.memory_manager.data_path.glob("*.json"):
            if not _is_memory_filename(f.name):
                continue
            sid = _memory_session_id(f.name)
            if sid not in wanted:
                continue
            scope = self._deleted_session_scopes([f.name]).get(f.name)
            if not self.memory_manager.delete_memory_file(f.name):
                continue
            if scope is not None:
                self._purge_session_state(scope)
            purged += 1
        return purged

    def _deleted_session_scopes(self, filenames: list) -> dict:
        """被删记忆文件 → (角色, 会话ID, 会话类型, 目标ID)。

        会话 ID 里的前缀由记忆文件名拆出来：私聊的目标是那个人，群聊目标是群号
        （群里的成员是各自独立的记录，不跟着群聊走）。私聊的用户 ID 以文件里
        记的发送者为准，调用时必须赶在文件被删掉之前。
        """
        out = {}
        for name in filenames:
            m = re.match(r'^([A-Za-z0-9_\-]+)_(private|group)_(.+)\.json$', str(name))
            if not m:
                continue
            character_key, stype, rest = m.group(1), m.group(2), m.group(3)
            if stype == "private":
                uid = self._session_user_id(name, rest)
                out[name] = (character_key, f"private_{uid}", "private", uid)
            else:
                out[name] = (character_key, f"group_{rest}", "group", rest.split("_")[0])
        return out

    def _session_user_id(self, filename: str, fallback: str) -> str:
        """会话文件里最后一条用户消息的发送者 ID；取不到时退回文件名里解析出来的。"""
        try:
            result = self.memory_manager.get_history(filename)
        except Exception as e:
            print(f"读取会话 {filename} 的发送者失败: {type(e).__name__}: {e}")
            return fallback
        for msg in reversed((result or {}).get("history") or []):
            if isinstance(msg, dict) and msg.get("role") == "user":
                uid = str(msg.get("sender_id") or "").strip()
                if uid:
                    return uid
        return fallback

    async def handle_delete_messages(self, request):
        try:
            payload = await request.json()
            filename = payload.get("filename", "")
            indices = payload.get("indices", [])
            result = self.memory_manager.delete_messages(filename, indices)
            return web.json_response(result)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_memory_export(self, request):
        filename = request.query.get("filename", "")
        result = self.memory_manager.get_history(filename)
        if not result.get("success"):
            return web.json_response(result, status=404)
        payload = {"filename": filename,
                   "character_name": result.get("character_name"),
                   "history": result.get("history", [])}
        content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        saved = await self._save_export_via_dialog(filename, content)
        if saved == "browser":
            return _json_file_response(payload, filename)
        if saved == "cancelled":
            return self._export_status_response("cancelled")
        return self._export_status_response("saved", saved)

    async def handle_memory_import(self, request):
        try:
            reader = await request.multipart()
            imported = []
            skipped = []
            async for part in reader:
                if not part.filename or not part.filename.endswith(".json"):
                    continue
                raw = await part.read(decode=False)
                data = json.loads(raw.decode("utf-8"))
                if not isinstance(data, dict) or "history" not in data:
                    continue
                name = Path(part.filename).name
                # 只允许写入会话记忆文件：data 目录里还躺着 webui_auth.json 等功能数据，
                # 同一目录下的整份覆盖等于把认证态等数据交给上传者改写
                if not _is_memory_filename(name):
                    skipped.append(name)
                    continue
                from modules.jsonio import save_json
                save_json(self.memory_manager.data_path / name, data)
                imported.append(name)
            return web.json_response({"success": True, "imported": imported,
                                      "skipped": skipped})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_memory_export_all(self, request):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            # 只打包会话记忆：同一目录下的 webui_auth.json 含 WebUI 令牌与口令散列，
            # 打包进去等于把登录凭据随导出文件一起送出去
            for f in sorted(self.memory_manager.data_path.glob("*.json")):
                if _is_memory_filename(f.name):
                    zf.write(f, f.name)
        content = buf.getvalue()
        saved = await self._save_export_via_dialog("lovomo_memories.zip", content)
        if saved == "browser":
            return web.Response(body=content, content_type="application/zip",
                                headers={"Content-Disposition": 'attachment; filename="lovomo_memories.zip"'})
        if saved == "cancelled":
            return self._export_status_response("cancelled")
        return self._export_status_response("saved", saved)

    async def handle_sessions(self, request):
        """返回已知会话列表（供定时任务/事件/待办选择发送目标）。"""
        return web.json_response({"sessions": list_known_sessions()})

    # ---------------- 情绪音频管理 ----------------
    def _role_root(self, role_key: str, kind: str = "tone"):
        """情绪音频目录：kind=mimic 取情绪模仿根目录（未配置返回 None），其余取语气根目录。"""
        role = self.config.roles.get(role_key, {}) if role_key else {}
        ctx = RoleContext(self.config.config, role or {})
        if str(kind or "") == "mimic":
            root = str(ctx.get("emotion_mimic_root", "") or "").strip()
            return Path(root) if root else None
        return Path(resolve_tts_path(ctx.get("ref_audio_root", "")))

    async def handle_voice_design_list(self, request):
        try:
            voices = await list_voices(self.config)
            return web.json_response({
                "success": True, "voices": voices,
                "languages": list(VOICE_DESIGN_LANGUAGES),
                "model": str(self.config.get("cloud_tts_model", "") or "")})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_voice_design_create(self, request):
        try:
            payload = await request.json()
            result = await create_voice(
                self.config,
                voice_prompt=payload.get("voice_prompt", ""),
                preview_text=payload.get("preview_text", ""),
                name=payload.get("name", ""),
                language=payload.get("language", ""),
                target_model=payload.get("target_model", ""))
            preview = result.pop("preview_audio", b"")
            result["preview_audio"] = b64encode(preview).decode("ascii") if preview else ""
            return web.json_response({"success": True, **result})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_voice_design_delete(self, request):
        try:
            payload = await request.json()
            name = str(payload.get("name", "") or "").strip()
            if not name:
                return web.json_response({"success": False, "error": "音色名不能为空"},
                                         status=400)
            return web.json_response({"success": True,
                                      "voice": await delete_voice(self.config, name)})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_voice_enrollment_list(self, request):
        try:
            voices = await list_enrolled_voices(self.config)
            return web.json_response({
                "success": True, "voices": voices,
                "languages": list(VOICE_ENROLLMENT_LANGUAGES),
                "model": str(self.config.get("cloud_tts_model", "") or "")})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_voice_enrollment_create(self, request):
        try:
            payload = await request.json()
            audios = []
            for item in payload.get("audios") or []:
                if not isinstance(item, dict):
                    continue
                try:
                    audios.append((str(item.get("name", "") or ""),
                                   b64decode(str(item.get("data", "") or ""))))
                except Exception:
                    return web.json_response(
                        {"success": False, "error": "音频数据解码失败，请重新选择文件"},
                        status=400)
            return web.json_response({"success": True, **await create_enrolled_voice(
                self.config, audios=audios, name=payload.get("name", ""),
                text=payload.get("text", ""), language=payload.get("language", ""),
                target_model=payload.get("target_model", ""))})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_voice_enrollment_delete(self, request):
        try:
            payload = await request.json()
            name = str(payload.get("name", "") or "").strip()
            if not name:
                return web.json_response({"success": False, "error": "音色名不能为空"},
                                         status=400)
            return web.json_response({"success": True,
                                      "voice": await delete_enrolled_voice(self.config, name)})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_emotions_list(self, request):
        role_key = request.query.get("role", "")
        kind = "mimic" if request.query.get("kind", "") == "mimic" else "tone"
        root = self._role_root(role_key, kind)
        emotions = []
        if root is not None and root.exists():
            for folder in sorted(root.iterdir()):
                if not folder.is_dir():
                    continue
                if not _is_emotion_folder(folder):
                    continue
                files = [f.name for f in sorted(folder.iterdir()) if f.is_file()]
                asr = ""
                asr_path = folder / "asr.txt"
                if asr_path.exists():
                    asr = asr_path.read_text(encoding="utf-8", errors="ignore").strip()
                ref = next((f for f in files if f.lower().startswith("ref.")), None)
                if ref is None:
                    ref = next((f for f in files if f.lower().split(".")[-1] in
                                ("mp3", "wav", "ogg", "flac", "m4a")), None)
                audios = [f for f in files if Path(f).suffix.lower() in _AUDIO_EXTS]
                texts = {f: _read_sidecar_text(folder / f) for f in audios}
                emotions.append({"name": folder.name, "files": files, "audios": audios,
                                 "texts": texts, "asr": asr, "ref": ref})
        return web.json_response({"root": str(root) if root is not None else "",
                                  "role": role_key, "kind": kind, "emotions": emotions})

    async def handle_emotions_upload(self, request):
        try:
            reader = await request.multipart()
            role = emotion = None
            kind = ""
            file_data = None
            file_name = ""
            async for part in reader:
                if part.name == "role":
                    role = (await part.text()).strip()
                elif part.name == "kind":
                    kind = (await part.text()).strip()
                elif part.name == "emotion":
                    emotion = (await part.text()).strip()
                elif part.name == "file":
                    file_name = part.filename or ""
                    file_data = await part.read(decode=False)
            root = self._role_root(role or "", kind)
            if root is None:
                return web.json_response({"success": False, "error": "未配置情绪模仿根目录"}, status=400)
            folder = _safe_subdir(root, emotion or "")
            if folder is None:
                return web.json_response({"success": False, "error": "情绪名称非法"}, status=400)
            folder.mkdir(parents=True, exist_ok=True)
            saved = []
            if file_data:
                ext = Path(file_name).suffix.lower() or ".mp3"
                if ext not in (".mp3", ".wav", ".ogg", ".flac", ".m4a"):
                    return web.json_response({"success": False, "error": "仅支持音频文件"}, status=400)
                target = folder / f"ref{ext}"
                target.write_bytes(file_data)
                saved.append(target.name)
                await asyncio.to_thread(normalize_audio, target, log=print)
            self._after_config_reload()
            return web.json_response({"success": True, "saved": saved})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_emotions_create(self, request):
        try:
            payload = await request.json()
            root = self._role_root(payload.get("role", ""), payload.get("kind", ""))
            if root is None:
                return web.json_response({"success": False, "error": "未配置情绪模仿根目录"}, status=400)
            folder = _safe_subdir(root, payload.get("emotion", ""))
            if folder is None:
                return web.json_response({"success": False, "error": "情绪名称非法"}, status=400)
            folder.mkdir(parents=True, exist_ok=True)
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_emotions_delete(self, request):
        try:
            payload = await request.json()
            root = self._role_root(payload.get("role", ""), payload.get("kind", ""))
            if root is None:
                return web.json_response({"success": False, "error": "未配置情绪模仿根目录"}, status=404)
            folder = _safe_subdir(root, payload.get("emotion", ""))
            if folder is None or not folder.exists():
                return web.json_response({"success": False, "error": "目录不存在"}, status=404)
            shutil.rmtree(folder)
            self._after_config_reload()
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_emotions_audio(self, request):
        role = request.query.get("role", "")
        emotion = request.query.get("emotion", "")
        file = request.query.get("file", "")
        root = self._role_root(role, request.query.get("kind", ""))
        if root is None:
            return web.Response(status=404, text="not found")
        folder = _safe_subdir(root, emotion)
        if folder is None or not _safe_name(file):
            return web.Response(status=404, text="not found")
        try:
            target = (folder / file).resolve()
        except (OSError, ValueError):
            return web.Response(status=404, text="not found")
        # 再确认落在情绪目录内且确实是文件
        if folder.resolve() not in target.parents or not target.is_file():
            return web.Response(status=404, text="not found")
        return await _local_file_response(
            target, AUDIO_MIMES.get(target.suffix.lower(), "application/octet-stream"))

    def _emotion_audio_targets(self, root: Path, payload: dict):
        """解析请求里要处理的音频；返回 [(情绪名, 路径)]，出错时返回错误文案。"""
        items = payload.get("items")
        if not items:
            items = [{"emotion": payload.get("emotion", ""), "files": payload.get("files")}]
        targets = []
        for item in items:
            item = item or {}
            name = str(item.get("emotion", "") or "")
            folder = _safe_subdir(root, name)
            if folder is None or not folder.is_dir():
                return f"情绪目录不存在：{name}"
            names = item.get("files")
            if not names:
                names = [f.name for f in sorted(folder.iterdir())
                         if f.is_file() and f.suffix.lower() in _AUDIO_EXTS]
            for file_name in names:
                target, error = _emotion_audio_file(folder, file_name)
                if target is None:
                    return error
                targets.append((name, target))
        if not targets:
            return "没有可处理的音频文件"
        return targets

    async def handle_emotions_text(self, request):
        """保存参考文字：texts 是每段音频自己的同名 txt，shared 是文件夹共用的 asr.txt；留空即删掉该文件。"""
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "请求体不是 JSON"}, status=400)
        root = self._role_root(payload.get("role", ""), payload.get("kind", ""))
        if root is None:
            return web.json_response({"success": False, "error": "未配置情绪模仿根目录"}, status=400)
        folder = _safe_subdir(root, str(payload.get("emotion", "") or ""))
        if folder is None or not folder.is_dir():
            return web.json_response({"success": False, "error": "情绪目录不存在"}, status=400)
        texts = payload.get("texts") or {}
        shared = payload.get("shared")
        if not isinstance(texts, dict) or (not texts and shared is None):
            return web.json_response({"success": False, "error": "没有要保存的文字"}, status=400)
        saved = []
        for file_name, text in texts.items():
            audio, error = _emotion_audio_file(folder, file_name)
            if audio is None:
                return web.json_response({"success": False, "error": error}, status=400)
            try:
                _write_text_file(audio.with_suffix(".txt"), text)
            except OSError as e:
                return web.json_response({"success": False, "error": f"写入失败：{e}"}, status=400)
            saved.append(audio.name)
        if shared is not None:
            try:
                _write_text_file(folder / "asr.txt", shared)
            except OSError as e:
                return web.json_response({"success": False, "error": f"写入失败：{e}"}, status=400)
            saved.append("asr.txt")
        self._after_config_reload()
        return web.json_response({"success": True, "saved": saved})

    async def handle_emotions_normalize(self, request):
        """把参考音频规整到 GPT-SoVITS 要求的 3~10 秒。"""
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "请求体不是 JSON"}, status=400)
        root = self._role_root(payload.get("role", ""), payload.get("kind", ""))
        if root is None:
            return web.json_response({"success": False, "error": "未配置情绪模仿根目录"}, status=400)
        targets = self._emotion_audio_targets(root, payload)
        if isinstance(targets, str):
            return web.json_response({"success": False, "error": targets}, status=400)
        results = []
        for name, target in targets:
            item = await asyncio.to_thread(normalize_audio, target, log=print)
            item["emotion"] = name
            results.append(item)
        return web.json_response({"success": True, "results": results})

    async def handle_asr_start(self, request):
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "请求体不是 JSON"}, status=400)
        root = self._role_root(payload.get("role", ""), payload.get("kind", ""))
        if root is None:
            return web.json_response({"success": False, "error": "未配置情绪模仿根目录"}, status=400)
        targets = self._emotion_audio_targets(root, payload)
        if isinstance(targets, str):
            return web.json_response({"success": False, "error": targets}, status=400)
        job = start_asr_job(self.config.config,
                            [{"audio": str(path), "emotion": name} for name, path in targets],
                            payload.get("lang", ""), payload.get("engine", ""))
        return web.json_response({"success": True, **job.snapshot()})

    async def handle_asr_status(self, request):
        job = get_asr_job(request.query.get("job_id", ""))
        if job is None:
            return web.json_response({"success": False, "error": "任务不存在"}, status=404)
        return web.json_response({"success": True, **job.snapshot()})

    # ---------------- 统计 ----------------

    async def handle_stats(self, request):
        range_key = str(request.query.get("range", "30") or "30")
        # 下钻：前端点击某天/某小时后带上具体窗口与粒度，只统计这个窗口
        window = None
        try:
            start_q = request.query.get("start")
            end_q = request.query.get("end")
            if start_q and end_q:
                window = (float(start_q), float(end_q))
        except (TypeError, ValueError):
            window = None
        granularity = str(request.query.get("granularity", "") or "")
        stats = (app_context.stats_mgr.get_stats(range_key, window, granularity)
                 if app_context.stats_mgr else None) or {}
        moods = []
        mood_records = app_context.mood_mgr.role_mood_records() if app_context.mood_mgr is not None else {}
        if app_context.mood_mgr is not None:
            roles = getattr(app_context.global_config, "roles", None) or {}
            for key, recs in mood_records.items():
                role = roles.get(key) or {}
                latest = recs[0] if recs else {}
                moods.append({
                    "character_key": key,
                    "character_name": role.get("character_name") or key,
                    "mood": round(latest.get("mood", 0)) if recs else None,
                    "sessions": len(recs),
                    "updated": latest.get("updated", 0) if recs else 0,
                    "records": [{"session_id": r.get("session_id", ""),
                                 "user_id": r.get("user_id", ""),
                                 "mood": round(r.get("mood", 0)),
                                 "updated": r.get("updated", 0)} for r in recs],
                })
            moods.sort(key=lambda m: -float(m["updated"]))
        stats["moods"] = moods
        roles_map = getattr(self.config, "roles", None) or {}
        # 曲线按会话分开：同一个角色跟不同的人各有一条线，不再是所有会话揉成一条
        curves = []

        def add_curves(kind: str, manager, character_key: str, name: str, scopes: list):
            for session_id, user_id in scopes:
                pts = manager.curve_points(character_key, session_id)
                if pts:
                    curves.append({"character_key": character_key, "character_name": name,
                                   "kind": kind, "session_id": str(session_id or ""),
                                   "user_id": str(user_id or ""), "points": pts})

        for key in roles_map:
            name = (roles_map.get(key) or {}).get("character_name") or key
            if app_context.mood_mgr is not None:
                add_curves("mood", app_context.mood_mgr, key, name,
                           [(r.get("session_id", ""), r.get("user_id", ""))
                            for r in mood_records.get(key, [])])
            if app_context.affection_mgr is not None:
                add_curves("affection", app_context.affection_mgr, key, name,
                           [app_context.affection_mgr.split_scope(scope)
                            for scope in (app_context.affection_mgr.records.get(key) or {})])
        stats["curves"] = curves
        return web.json_response(stats)

    async def handle_mood_set(self, request):
        try:
            payload = await request.json()
            session_id = str(payload.get("session_id", "") or "")
            character_key = str(payload.get("character_key", "") or "")
            if not session_id or not character_key or app_context.mood_mgr is None:
                return web.json_response({"success": False, "error": "参数不完整"}, status=400)
            lo = float(self.config.get("reply_judge_mood_min", 0) or 0)
            hi = float(self.config.get("reply_judge_mood_max", 100) or 100)
            if hi < lo:
                hi = lo
            mood = max(lo, min(hi, float(payload.get("mood", 0))))
            app_context.mood_mgr.set_mood(session_id, character_key, mood,
                              user_id=str(payload.get("user_id", "") or ""))
            return web.json_response({"success": True, "mood": mood})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_token_stats(self, request):
        range_key = str(request.query.get("range", "30") or "30")
        # 下钻：前端点击某天/某小时后带上具体窗口与粒度，只统计这个窗口
        window = None
        try:
            start_q = request.query.get("start")
            end_q = request.query.get("end")
            if start_q and end_q:
                window = (float(start_q), float(end_q))
        except (TypeError, ValueError):
            window = None
        granularity = str(request.query.get("granularity", "") or "")
        data = (app_context.stats_mgr.get_token_stats(range_key, window, granularity)
                if app_context.stats_mgr else None) or {}
        return web.json_response(data)

    async def handle_performance(self, request):
        perf = app_context.stats_mgr.get_performance() if app_context.stats_mgr else {}
        perf["tts_online"] = await ensure_tts_service_enabled_check()
        perf["napcat_connected"] = bool(app_context.sender and app_context.sender.client is not None)
        perf["scheduler_jobs"] = len([j for j in app_context.scheduler.jobs.values() if j.enabled])
        return web.json_response(perf)

    # ---------------- 聊天测试台 ----------------
    async def handle_test_roles(self, request):
        roles = getattr(self.config, "roles", None) or {}
        return web.json_response({"roles": [
            {"character_key": key, "character_name": (role or {}).get("character_name") or key}
            for key, role in roles.items()]})

    async def handle_test_chat(self, request):
        """聊天测试台：不走 QQ，直接跑「审判 → 生成 → 心情/好感落盘」这条链路。"""
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "参数错误"}, status=400)
        character_key = str(payload.get("character_key", "") or "").strip()
        text = str(payload.get("text", "") or "").strip()
        role = (getattr(self.config, "roles", None) or {}).get(character_key)
        if role is None or not text:
            return web.json_response({"success": False, "error": "参数不完整"}, status=400)
        mem = role_memory(role)
        if mem is None:
            return web.json_response({"success": False, "error": "角色记忆不可用"}, status=400)
        session_id = TEST_CHAT_SESSION
        user_id = TEST_CHAT_USER
        lock = app_context._SESSION_LOCKS.setdefault(f"{session_id}::{character_key}", asyncio.Lock())
        async with lock:
            ctx = RoleContext(self.config.config, role)
            emotions = get_role_emotions(role)
            history = mem.load_history(session_id)
            extra_parts = []
            verdict = None
            judge_info = None
            if app_context.mood_mgr is not None and (judge_enabled(ctx) or mood_enabled(ctx)):
                try:
                    verdict = await asyncio.wait_for(
                        judge_and_decide(ctx, app_context.mood_mgr, session_id, text, history,
                                         user_id=user_id,
                                         reply_floor=(app_context.affection_mgr.stage_reply_floor(
                                             character_key, user_id, session_id)
                                             if app_context.affection_mgr is not None else 0.0)),
                        timeout=30)
                    judge_info = {"should_reply": bool(verdict.get("should_reply")),
                                  "mood": round(float(verdict.get("mood", 0))),
                                  "mood_delta": verdict.get("mood_delta"),
                                  "probability": round(float(verdict.get("probability", 1.0)), 3)}
                except Exception as e:
                    print(f"聊天测试台：审判失败（忽略）: {type(e).__name__}: {e}")
            mood_now = current_mood(ctx, app_context.mood_mgr, session_id, user_id) \
                if app_context.mood_mgr is not None else None
            if app_context.mood_mgr is not None:
                _tier, mood_note = mood_style(ctx, mood_now)
                if mood_note:
                    extra_parts.append(mood_note)
            if app_context.affection_mgr is not None and affection_enabled(ctx):
                aff_note = app_context.affection_mgr.build_note(ctx, user_id, label="测试用户",
                                                    session_id=session_id)
                if aff_note:
                    extra_parts.append(aff_note)
            try:
                reply = await asyncio.wait_for(
                    generate_reply(ctx, emotions, text, history, None, extra_parts,
                                   user_id, session_id=session_id),
                    timeout=max(90.0, float(self.config.get("llm_timeout", 120) or 120) + 60.0))
            except asyncio.TimeoutError:
                return web.json_response({"success": False, "error": "回复生成超时"}, status=504)
            except Exception as e:
                return web.json_response({"success": False, "error": str(e)}, status=500)
            reply = reply or {}
            sentences = [{"zh": str(s.get("zh") or s.get("display") or ""),
                          "emotion": str(s.get("emotion") or "")}
                         for s in (reply.get("sentences") or [])]
            zh_text = "".join(s["zh"] for s in sentences)
            history.append({"role": "user", "content": text, "sender_id": user_id,
                            "sender_name": "测试用户", "timestamp": time.time()})
            history.append({"role": "assistant", "content": zh_text, "timestamp": time.time(),
                            "speaker": role.get("character_name", character_key),
                            "emotion": sentences[0]["emotion"] if sentences else ""})
            mem.save_session_data(session_id, {"history": history})
            # 承诺追踪与真实消息同链路：台词命中承诺字样就提取入库
            if app_context.promise_mgr is not None \
                    and bool(self.config.get("promise_enabled", True)) \
                    and PROMISE_HINT_RE.search(zh_text):
                await _extract_and_store_promise(ctx, session_id, user_id, zh_text)
            if app_context.mood_mgr is not None and verdict is not None:
                commit_mood(ctx, app_context.mood_mgr, session_id, verdict, user_id=user_id)
            mood_after = stored_mood(ctx, app_context.mood_mgr, session_id, user_id=user_id) \
                if app_context.mood_mgr is not None else None
            aff_rec = None
            if app_context.affection_mgr is not None:
                if verdict is not None and verdict.get("affection_enabled") \
                        and verdict.get("affection_delta") is not None:
                    app_context.affection_mgr.apply_delta(character_key, user_id,
                                              verdict.get("affection_delta"),
                                              confession=bool(verdict.get("confession")),
                                              romance=bool(verdict.get("romance")),
                                              acceptance=bool(verdict.get("acceptance")),
                                              reply_text=zh_text,
                                              session_id=session_id)
                aff_rec = app_context.affection_mgr.get(character_key, user_id, session_id)
            aff_state = app_context.affection_mgr.state(character_key, user_id, session_id) \
                if app_context.affection_mgr is not None else None
            return web.json_response({
                "success": True,
                "sentences": sentences,
                "mood": round(float(mood_after), 1) if mood_after is not None else None,
                "judge": judge_info,
                "affection": ({"score": aff_rec["score"],
                               "stage": aff_state["stage"],
                               "band": aff_state["band"],
                               "nature": aff_state["nature"],
                               "partner": aff_rec["partner"]}
                              if aff_rec is not None else None)})

    async def handle_test_history(self, request):
        character_key = str(request.query.get("character_key", "") or "").strip()
        role = (getattr(self.config, "roles", None) or {}).get(character_key)
        mem = role_memory(role) if role else None
        if mem is None:
            return web.json_response({"success": False, "error": "角色不存在"}, status=404)
        history = [m for m in mem.load_history(TEST_CHAT_SESSION)
                   if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
        mood_after = None
        if app_context.mood_mgr is not None:
            ctx = RoleContext(self.config.config, role)
            mood_after = stored_mood(ctx, app_context.mood_mgr, TEST_CHAT_SESSION, user_id=TEST_CHAT_USER)
        aff_rec = app_context.affection_mgr.get(character_key, TEST_CHAT_USER, TEST_CHAT_SESSION) \
            if app_context.affection_mgr is not None else None
        return web.json_response({
            "success": True, "history": history,
            "mood": round(float(mood_after), 1) if mood_after is not None else None,
            "affection": ({"score": aff_rec["score"],
                           "stage": app_context.affection_mgr.stage(character_key, TEST_CHAT_USER,
                                                        TEST_CHAT_SESSION),
                           "partner": aff_rec["partner"]}
                          if aff_rec is not None else None)})

    async def handle_test_chat_clear(self, request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        character_key = str(payload.get("character_key", "") or "").strip()
        role = (getattr(self.config, "roles", None) or {}).get(character_key)
        mem = role_memory(role) if role else None
        if mem is None:
            return web.json_response({"success": False, "error": "角色不存在"}, status=404)
        try:
            mem.get_memory_file(TEST_CHAT_SESSION).unlink(missing_ok=True)
        except OSError as e:
            return web.json_response({"success": False, "error": str(e)}, status=500)
        # 清空对话时把测试会话的状态一起清掉：只删记录会让心情、好感、承诺留着
        cleared = {}
        try:
            if app_context.mood_mgr is not None:
                app_context.mood_mgr.delete_session(TEST_CHAT_SESSION)
            if app_context.affection_mgr is not None:
                cleared["affection"] = app_context.affection_mgr.reset(character_key, TEST_CHAT_USER,
                                                          TEST_CHAT_SESSION)
            if app_context.promise_mgr is not None:
                cleared["promises"] = app_context.promise_mgr.drop_session(TEST_CHAT_SESSION)
            if app_context.recall_mgr is not None:
                cleared["recall"] = app_context.recall_mgr.drop_session(TEST_CHAT_SESSION)
        except Exception as e:
            print(f"聊天测试台清空状态失败（忽略）: {type(e).__name__}: {e}")
        return web.json_response({"success": True, "cleared": cleared})

    # ---------------- 陪伴功能数据 ----------------
    async def handle_companion_diary(self, request):
        limit = 30
        try:
            limit = max(1, int(request.query.get("limit", 30)))
        except (TypeError, ValueError):
            pass
        roles = getattr(self.config, "roles", None) or {}
        diary = app_context.mood_mgr.get_diary(limit=limit) if app_context.mood_mgr is not None else {}
        out = []
        for key, entries in diary.items():
            out.append({"character_key": key,
                        "character_name": (roles.get(key) or {}).get("character_name") or key,
                        "entries": entries})
        out.sort(key=lambda item: item["character_key"])
        return web.json_response({"success": True, "diary": out})

    # ---------------- 角色档案导出 / 导入 ----------------
    async def handle_character_export(self, request):
        from modules.updater import APP_VERSION
        character_key = str(request.query.get("character_key", "") or "").strip()
        role = (getattr(self.config, "roles", None) or {}).get(character_key)
        if role is None:
            return web.json_response({"success": False, "error": "角色不存在"}, status=404)
        payload = {"app": "Lovomo", "kind": "character",
                   "version": APP_VERSION,
                   "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "character": {k: role.get(k) for k in _ROLE_EXPORT_KEYS},
                   "memories": {}, "affection": {}, "moods": {}}
        mem = role_memory(role)
        if mem is not None and mem.data_path.exists():
            for f in sorted(mem.data_path.glob(f"{mem.character_key}_*.json")):
                session_part = f.name[len(mem.character_key) + 1:-len(".json")]
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(data, dict):
                    payload["memories"][session_part] = data
        if app_context.affection_mgr is not None:
            payload["affection"] = (app_context.affection_mgr.records.get(character_key) or {})
        if app_context.mood_mgr is not None:
            payload["moods"] = {k: rec for k, rec in app_context.mood_mgr.records.items()
                                if _parse_mood_key(k)[2] == character_key}
        return _json_file_response(payload, f"Lovomo_角色档案_{character_key}.json")

    async def handle_character_import(self, request):
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "参数错误"}, status=400)
        character = payload.get("character")
        if not isinstance(character, dict) \
                or not str(character.get("character_key") or "").strip():
            return web.json_response({"success": False,
                                      "error": "档案里没有 character_key"}, status=400)
        key = str(character["character_key"]).strip()
        role = {k: character.get(k) for k in _ROLE_EXPORT_KEYS if k in character}
        role["character_key"] = key
        roles = self.config.roles
        if key in roles and not bool(payload.get("overwrite", False)):
            return web.json_response({"success": False,
                                      "error": f"角色 {key} 已存在；勾选覆盖后重试"}, status=409)
        roles[key] = role
        self.config.config["roles"] = list(roles.values())
        self.config._atomic_save(self.config.config)
        mem = role_memory(role)
        if mem is not None and isinstance(payload.get("memories"), dict):
            for session_part, data in payload["memories"].items():
                safe = re.sub(r'[^A-Za-z0-9_\-]', '_', str(session_part or ""))
                if not safe:
                    continue
                mem.save_session_data(safe, data if isinstance(data, dict) else {})
        if isinstance(payload.get("affection"), dict) and app_context.affection_mgr is not None:
            app_context.affection_mgr.records[key] = payload["affection"]
            app_context.affection_mgr.save()
        if isinstance(payload.get("moods"), dict) and app_context.mood_mgr is not None:
            for k, rec in payload["moods"].items():
                if _parse_mood_key(k)[2] == key:
                    app_context.mood_mgr.records[k] = rec
            app_context.mood_mgr._save()
        self._after_config_reload()
        return web.json_response({"success": True, "character_key": key,
                                  "memories": len(payload.get("memories") or {})})

    # ---------------- 定时任务 ----------------
    async def handle_jobs(self, request):
        return web.json_response({"jobs": app_context.job_mgr.describe() if app_context.job_mgr else [],
                                  "scheduler_enabled": bool(self.config.get("scheduler_enabled", False))})

    async def handle_jobs_save(self, request):
        try:
            payload = await request.json()
            jobs = payload.get("jobs", [])
            cleaned = []
            for job in jobs:
                jid = str(job.get("id") or f"job_{int(time.time()*1000)}")
                cleaned.append({
                    "id": jid, "name": str(job.get("name") or jid),
                    "enabled": bool(job.get("enabled", True)),
                    "trigger": job.get("trigger") or {"type": "daily", "time": "08:00"},
                    "target": job.get("target") or {},
                    "action": job.get("action") or {"mode": "llm", "llm_prompt": ""},
                })
            app_context.job_mgr.jobs = cleaned
            app_context.job_mgr.save()
            app_context.job_mgr.reload()
            return web.json_response({"success": True, "jobs": app_context.job_mgr.describe()})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_jobs_batch(self, request):
        """批量修改定时任务的发送目标（会话类型 / 会话 ID）。

        ids 为空表示应用到全部任务；两个字段都留空表示"清空会话 ID"，
        也就是回到"发给所有聊过的会话"。
        """
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "请求体不是合法 JSON"}, status=400)
        ids = payload.get("ids")
        wanted = {str(i) for i in ids} if isinstance(ids, list) else None
        has_type = "session_type" in payload
        session_type = str(payload.get("session_type") or "").strip()
        if has_type and session_type not in ("private", "group"):
            return web.json_response({"success": False, "error": "会话类型只能是 private 或 group"},
                                     status=400)
        has_id = "session_id" in payload
        session_id = str(payload.get("session_id") or "").strip()
        has_voice = "use_voice" in payload
        use_voice = bool(payload.get("use_voice"))
        changed = 0
        for job in app_context.job_mgr.jobs:
            if wanted is not None and str(job.get("id")) not in wanted:
                continue
            target = job.setdefault("target", {})
            if has_type:
                target["session_type"] = session_type
            if has_id:
                target["session_id"] = session_id
            if has_voice:
                job.setdefault("action", {})["use_voice"] = use_voice
            changed += 1
        app_context.job_mgr.save()
        app_context.job_mgr.reload()
        return web.json_response({"success": True, "changed": changed,
                                  "jobs": app_context.job_mgr.describe()})

    async def handle_jobs_run(self, request):
        try:
            payload = await request.json()
            jid = str(payload.get("id", ""))
            job = next((j for j in app_context.job_mgr.jobs if str(j.get("id")) == jid), None)
            if not job:
                return web.json_response({"success": False, "error": "任务不存在"}, status=404)
            ok = await app_context.job_mgr._run_job(job)
            return web.json_response(
                {"success": True, "sent": bool(ok),
                 "message": "任务已手动执行一次" if ok else
                 "任务已执行，但这条没有发送出去（详见日志：常见原因是模型返回了空话术，或发送目标不可用）"})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    # ---------------- 待办 ----------------
    async def handle_todos(self, request):
        status = request.query.get("status")
        todos = app_context.todo_mgr.list_todos(status) if app_context.todo_mgr else []
        return web.json_response({"todos": todos})

    async def handle_todos_add(self, request):
        try:
            payload = await request.json()
            content = str(payload.get("content", "")).strip()
            if not content:
                return web.json_response({"success": False, "error": "内容不能为空"}, status=400)
            remind = payload.get("remind_time")
            remind_ts = None
            if isinstance(remind, (int, float)):
                remind_ts = float(remind)
            elif isinstance(remind, str) and remind.strip():
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
                    try:
                        remind_ts = time.mktime(time.strptime(remind.strip(), fmt))
                        break
                    except ValueError:
                        continue
                if remind_ts is None:
                    return web.json_response({"success": False, "error": "时间格式应为 YYYY-MM-DD HH:MM"}, status=400)
            if remind_ts is None:
                return web.json_response({"success": False, "error": "请填写提醒时间"}, status=400)
            todo = app_context.todo_mgr.add_todo(content, remind_ts,
                                     payload.get("session_type", "private"),
                                     payload.get("session_id", ""),
                                     payload.get("user_id", ""), source="manual",
                                     use_voice=(bool(payload["use_voice"])
                                                if "use_voice" in payload else None))
            return web.json_response({"success": bool(todo), "todo": todo})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_todos_update(self, request):
        try:
            payload = await request.json()
            todo_id = int(payload.get("id", 0))
            # status 必填且必须在白名单内：漏传时默认标成"已完成"会静默改错状态，
            # 任意字符串又会直接落库
            status = str(payload.get("status", "") or "")
            if status not in TODO_STATUSES:
                return web.json_response(
                    {"success": False, "error": f"状态非法：{status or '(未提供)'}"}, status=400)
            if status == "done":
                app_context.todo_mgr.complete(todo_id)
            elif status == "cancelled":
                app_context.todo_mgr.delete(todo_id)
            elif status == "pending":
                # 改回「待提醒」要同时重排提醒，否则这条待办在本次运行里不会再触发
                app_context.todo_mgr.resume(todo_id)
            else:
                app_context.todo_mgr.db.execute("UPDATE todos SET status=? WHERE id=?", (status, todo_id))
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_todos_delete(self, request):
        try:
            payload = await request.json()
            app_context.todo_mgr.delete(int(payload.get("id", 0)))
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_todos_batch(self, request):
        """批量修改待办的会话类型 / 会话 ID（与定时任务/节日共用同一套规则）。"""
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "请求体不是合法 JSON"}, status=400)
        ids = payload.get("ids")
        wanted = {int(i) for i in ids} if isinstance(ids, list) else None
        has_type = "session_type" in payload
        session_type = str(payload.get("session_type") or "").strip()
        if has_type and session_type not in ("private", "group"):
            return web.json_response({"success": False, "error": "会话类型只能是 private 或 group"},
                                     status=400)
        has_id = "session_id" in payload
        session_id = str(payload.get("session_id") or "").strip()
        has_voice = "use_voice" in payload
        use_voice = 1 if bool(payload.get("use_voice")) else 0
        changed = 0
        if app_context.todo_mgr is not None:
            rows = app_context.todo_mgr.list_todos()
            for row in rows:
                if wanted is not None and int(row.get("id", 0)) not in wanted:
                    continue
                sets = []
                params = []
                if has_type:
                    sets.append("session_type=?")
                    params.append(session_type)
                if has_id:
                    sets.append("session_id=?")
                    params.append(session_id)
                if has_voice:
                    sets.append("use_voice=?")
                    params.append(use_voice)
                if not sets:
                    continue
                params.append(int(row["id"]))
                app_context.todo_mgr.db.execute("UPDATE todos SET " + ", ".join(sets)
                                    + " WHERE id=?", tuple(params))
                changed += 1
        return web.json_response({"success": True, "changed": changed,
                                  "todos": (app_context.todo_mgr.list_todos() if app_context.todo_mgr else [])})

    # ---------------- 事件问候 ----------------
    async def handle_events(self, request):
        return web.json_response({"events": app_context.event_mgr.events if app_context.event_mgr else []})

    async def handle_events_save(self, request):
        try:
            payload = await request.json()
            events = payload.get("events", [])
            known = {str(e.get("id")): e for e in (app_context.event_mgr.events if app_context.event_mgr else [])}
            cleaned = []
            for ev in events:
                eid = str(ev.get("id") or f"evt_{int(time.time()*1000)}")
                cleaned.append({
                    "id": eid, "name": str(ev.get("name") or eid),
                    "type": ev.get("type", "date"),
                    "date": str(ev.get("date", "")),
                    "enabled": bool(ev.get("enabled", True)),
                    "mode": ev.get("mode", "template"),
                    "template": str(ev.get("template", "")),
                    "llm_prompt": str(ev.get("llm_prompt", "")),
                    "use_voice": bool(ev.get("use_voice", False)),
                    # 内置默认节日标记：请求里没带就沿用原值，丢了它"无目标时发给全部会话"的兜底会失效
                    "default": bool(ev.get("default", known.get(eid, {}).get("default", False))),
                    # 会话 ID 留空等于清空目标，存成空目标占位会顶掉默认节日的兜底
                    "targets": [t for t in (ev.get("targets") or [])
                                if isinstance(t, dict) and str(t.get("session_id") or "").strip()],
                })
            app_context.event_mgr.events = cleaned
            app_context.event_mgr.save_events()
            return web.json_response({"success": True, "events": cleaned})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_events_batch(self, request):
        """批量修改节日/纪念日问候的发送目标（会话类型 / 会话 ID）。

        ids 为空表示应用到全部事件；两个字段都留空表示"清空会话 ID"
        即回到"发给所有聊过的会话"。
        """
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "请求体不是合法 JSON"}, status=400)
        ids = payload.get("ids")
        wanted = {str(i) for i in ids} if isinstance(ids, list) else None
        has_type = "session_type" in payload
        session_type = str(payload.get("session_type") or "").strip()
        if has_type and session_type not in ("private", "group"):
            return web.json_response({"success": False, "error": "会话类型只能是 private 或 group"},
                                     status=400)
        has_id = "session_id" in payload
        session_id = str(payload.get("session_id") or "").strip()
        has_voice = "use_voice" in payload
        use_voice = bool(payload.get("use_voice"))
        changed = 0
        for ev in (app_context.event_mgr.events if app_context.event_mgr else []):
            if wanted is not None and str(ev.get("id")) not in wanted:
                continue
            # 只改语音时别碰发送目标：给本来没有目标的事件补一个空目标，会顶掉
            # "内置默认节日发给全部会话"的兜底，问候从此永远发不出去。
            if has_type or has_id:
                targets = ev.get("targets") or []
                if not targets:
                    targets = [{"session_type": "private", "session_id": ""}]
                tg = targets[0]
                if has_type:
                    tg["session_type"] = session_type
                if has_id:
                    tg["session_id"] = session_id
                # 会话 ID 留空 = 清空目标，回到"发给所有聊过的会话"，不能留空目标占位
                ev["targets"] = targets if str(tg.get("session_id") or "").strip() else []
            if has_voice:
                ev["use_voice"] = use_voice
            changed += 1
        if app_context.event_mgr is not None:
            app_context.event_mgr.save_events()
        return web.json_response({"success": True, "changed": changed,
                                  "events": (app_context.event_mgr.events if app_context.event_mgr else [])})

    async def handle_events_test(self, request):
        try:
            payload = await request.json()
            ok = await app_context.event_mgr.greet_event_now(str(payload.get("id", "")), app_context.sender,
                                                 get_active_ctx, get_active_emotions)
            return web.json_response({"success": bool(ok),
                                      "message": "已发送测试问候" if ok else
                                      "该事件没有可发送的目标会话（内置节日会在当天发给全部会话），或问候内容为空"})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    # ---------------- 工具调用 ----------------
    async def handle_tools(self, request):
        return web.json_response({"tools": app_context.tool_registry.tools if app_context.tool_registry else [],
                                  "enabled": bool(self.config.get("tools_enabled", False))})

    async def handle_search_engines(self, request):
        """搜索引擎下拉框数据：内置 + 自定义引擎，全部来自注册表（不硬编码）。"""
        from modules.tools import available_engines, default_engine_key, parse_api_keys
        engines = available_engines(self.config)
        key_names = sorted({spec.get("api_key_env", "") for spec in engines.values()
                            if spec.get("api_key_env")})
        configured = parse_api_keys(self.config.get("web_search_api_keys", {}))
        return web.json_response({
            "engines": [{"key": key, "label": spec.get("label", key),
                         "description": spec.get("desc", ""),
                         "api_key_env": spec.get("api_key_env", ""),
                         "needs_api": bool(spec.get("api_url")) and not spec.get("parse_html")}
                        for key, spec in engines.items()],
            "current": default_engine_key(self.config),
            "custom": str(self.config.get("web_search_custom_engines", "") or ""),
            "url_override": str(self.config.get("web_search_url", "") or ""),
            "api_key_envs": key_names,
            "api_keys_set": sorted(configured.keys()),
        })

    async def handle_search_engine_save(self, request):
        """保存默认搜索引擎、自定义引擎、API key（写进 config.json 后热重载）。"""
        try:
            from modules.tools import available_engines, parse_api_keys, invalid_custom_engines
            payload = await request.json()
            engine = str(payload.get("engine", "") or "").strip()
            custom = str(payload.get("custom", self.config.get("web_search_custom_engines", "")) or "")
            bad = invalid_custom_engines(custom)
            if bad:
                return web.json_response(
                    {"success": False,
                     "error": "自定义引擎地址模板缺少 {query} 占位符，无法带上搜索词："
                              + "、".join(bad)}, status=400)
            if engine and engine not in available_engines({**self.config.config,
                                                          "web_search_custom_engines": custom}):
                return web.json_response({"success": False, "error": f"未知搜索引擎: {engine}"}, status=400)
            new_config = {**self.config.default_config(), **self.config.config}
            if "custom" in payload:
                new_config["web_search_custom_engines"] = custom
            if "url_override" in payload:
                new_config["web_search_url"] = str(payload.get("url_override", "") or "").strip()
            if "api_keys" in payload:
                # 只并入非空值：WebUI 回显不了已保存的 key，空值不能把旧 key 抹掉
                merged = {**parse_api_keys(self.config.get("web_search_api_keys", {})),
                          **parse_api_keys(payload.get("api_keys"))}
                new_config["web_search_api_keys"] = merged
            if engine:
                new_config["web_search_engine"] = engine
            self.config.config = new_config
            self.config._atomic_save(new_config)
            try:
                self._after_config_reload()
            except Exception as reload_err:
                return web.json_response(
                    {"success": True,
                     "message": f"配置已保存，但热重载失败（{reload_err}），请重启程序使运行状态与配置一致。"})
            return web.json_response({"success": True, "engine": new_config.get("web_search_engine", "")})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_tools_save(self, request):
        try:
            payload = await request.json()
            tools = payload.get("tools", [])
            for t in tools:
                if not str(t.get("name", "")).strip():
                    return web.json_response({"success": False, "error": "工具名不能为空"}, status=400)
            app_context.tool_registry.tools = tools
            app_context.tool_registry.save()
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_tools_test(self, request):
        try:
            payload = await request.json()
            name = payload.get("name", "")
            arguments = payload.get("arguments", {})
            if not arguments or (isinstance(arguments, dict) and not arguments):
                tool = next((x for x in app_context.tool_registry.tools if x.get("name") == name), None)
                if tool:
                    from modules.tools import test_sample_for
                    sample = test_sample_for(tool)
                    if sample:
                        arguments = sample
            user_id = str(payload.get("user_id", "") or "")
            app_context.tool_registry.begin_reply()
            ok, output = await app_context.tool_registry.execute(name, arguments, user_id)
            return web.json_response({"success": ok, "output": output})
        except Exception as e:
            return web.json_response({"success": False, "output": str(e)}, status=400)

    # ---------------- RAG ----------------
    async def handle_rag_docs(self, request):
        return web.json_response({"docs": app_context.rag_mgr.list_docs() if app_context.rag_mgr else [],
                                  "enabled": bool(self.config.get("rag_enabled", False))})

    async def handle_rag_upload(self, request):
        try:
            results = []
            reader = await request.multipart()
            async for part in reader:
                if not part.filename:
                    continue
                raw = await part.read(decode=False)
                if len(raw) > 20 * 1024 * 1024:
                    results.append({"file": part.filename, "success": False,
                                    "error": "文件超过 20MB 上限，请拆分后上传"})
                    continue
                tmp = self.memory_manager.data_path / f"rag_upload_{int(time.time()*1000)}_{Path(part.filename).name}"
                tmp.write_bytes(raw)
                try:
                    text = extract_text_from_file(tmp)
                    r = await app_context.rag_mgr.add_document(Path(part.filename).stem, text)
                    results.append({"file": part.filename, **r})
                except Exception as e:
                    results.append({"file": part.filename, "success": False, "error": str(e)})
                finally:
                    tmp.unlink(missing_ok=True)
            return web.json_response({"success": True, "results": results})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_rag_delete(self, request):
        try:
            payload = await request.json()
            ok = app_context.rag_mgr.delete_document(str(payload.get("id", "")))
            return web.json_response({"success": ok})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_rag_query(self, request):
        try:
            payload = await request.json()
            hits = await app_context.rag_mgr.search(str(payload.get("question", "")), verbose=True)
            return web.json_response({"success": True, "hits": hits})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    # ---------------- 接入方式 ----------------
    async def handle_connection_qr(self, request):
        """把一段文本渲染成二维码 PNG（接入方式的扫码登录用）。"""
        text = str(request.query.get("text", "") or "")
        if not text:
            return web.Response(status=400, text="缺少 text")
        try:
            import io
            import qrcode
            buf = io.BytesIO()
            qrcode.make(text).save(buf, format="PNG")
        except Exception as e:
            return web.Response(status=500, text=f"生成二维码失败: {e}")
        return web.Response(body=buf.getvalue(), content_type="image/png",
                            headers={"Cache-Control": "no-store"})

    async def handle_connection_login_qr(self, request):
        """取一张扫码用的二维码：微信 ClawBot 扫码登录、QQ 官方机器人扫码绑定。"""
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        platform = str(payload.get("platform") or "wechat_clawbot")
        conn_id = str(payload.get("id") or "").strip()
        if conn_id:
            conn = next((c for c in connections_of(self.config)
                         if str(c.get("id")) == conn_id), None)
            if conn is None:
                return web.json_response({"success": False, "error": "没有这条接入方式"},
                                         status=404)
            platform = str(conn.get("platform") or platform)
        if platform == "qq_official":
            return await self._qq_bind_start(conn_id)
        if platform != "wechat_clawbot":
            return web.json_response(
                {"success": False,
                 "error": "这个平台没有可编程的扫码登录，请填 AppID 与 AppSecret"}, status=400)
        try:
            data = await ilink_login_qr()
        except Exception as e:
            return web.json_response(
                {"success": False, "error": f"取二维码失败: {type(e).__name__}: {e}"},
                status=400)
        # qrcode 是轮询票据，qrcode_img_content 才是要编进二维码的内容
        qrcode = str(_adapter_first(data, "qrcode") or "")
        qr_content = str(_adapter_first(data, "qrcode_img_content", "qrcode_url") or "")
        if not qrcode or not qr_content:
            return web.json_response(
                {"success": False,
                 "error": f"二维码响应里没有可用字段: {data.get('err_msg') or data}"},
                status=400)
        # 用一次性 token 认这次登录：新建时还没有接入方式 id，也能先把码取回来
        token = os.urandom(6).hex()
        _CONNECTION_LOGIN_STATE[token] = {"qrcode": qrcode, "id": conn_id}
        return web.json_response({"success": True, "token": token, "qrcode": qrcode,
                                  "qr_content": qr_content})

    async def handle_connection_login_status(self, request):
        """轮询扫码状态；扫到就把 bot_token / bot_id 写回接入方式。"""
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        token = str(payload.get("token") or "").strip()
        state = _CONNECTION_LOGIN_STATE.get(token) or {}
        if state.get("kind") == "qq_bind":
            return await self._qq_bind_status(token, state)
        if not state.get("qrcode"):
            return web.json_response({"success": False, "error": "登录会话已失效，请重新取码"},
                                     status=400)
        try:
            data = await ilink_login_status(state["qrcode"])
        except Exception as e:
            return web.json_response(
                {"success": False, "error": f"查询扫码状态失败: {type(e).__name__}: {e}"},
                status=400)
        status = str(_adapter_first(data, "status", "state") or "wait")
        bot_token = _adapter_first(data, "bot_token", "token", "access_token")
        if status != "confirmed" or not bot_token:
            if status == "expired":
                _CONNECTION_LOGIN_STATE.pop(token, None)
            return web.json_response({"success": True, "confirmed": False,
                                      "status": status})
        account_id = str(_adapter_first(data, "ilink_bot_id", "bot_id") or "")
        base_url = str(_adapter_first(data, "baseurl", "base_url") or "")
        conn_id = str(state.get("id") or "")
        target = None
        if conn_id:
            data_cfg = self.config.config
            target = next((c for c in (data_cfg.get("connections") or [])
                           if str(c.get("id")) == conn_id), None)
            if target is not None:
                target["bot_token"] = str(bot_token)
                if account_id:
                    target["account_id"] = account_id
                if base_url:
                    target["base_url"] = base_url
                # 重新登录等于换了一次会话：旧游标与旧 context_token 都不能再用
                target["cursor"] = ""
                target["context_tokens"] = {}
                self.config._atomic_save(data_cfg)
                _CONNECTION_LOGIN_STATE.pop(token, None)
                return web.json_response({"success": True, "confirmed": True,
                                          "bot_id": account_id, "saved": True})
        # 这条接入方式已经不在（或本来就还没保存）：凭据交给前端随这条一起存。
        # 这里绝不能回 saved=True —— 界面会显示"已登录"，磁盘上却什么都没有。
        _CONNECTION_LOGIN_STATE.pop(token, None)
        return web.json_response({"success": True, "confirmed": True, "bot_id": account_id,
                                  "bot_token": str(bot_token), "base_url": base_url,
                                  "saved": False})

    async def _qq_bind_start(self, conn_id: str):
        """取一张 QQ 机器人的扫码绑定二维码（扫完自动拿回 AppID 与 AppSecret）。"""
        try:
            bind = await qq_bind_create()
        except Exception as e:
            return web.json_response(
                {"success": False, "error": f"取二维码失败: {type(e).__name__}: {e}"},
                status=400)
        # 与微信那边一样用一次性 token 认这次绑定：新建时还没有接入方式 id
        token = os.urandom(6).hex()
        _CONNECTION_LOGIN_STATE[token] = {"kind": "qq_bind", "id": conn_id,
                                          "task_id": bind["task_id"],
                                          "bind_key": bind["bind_key"]}
        return web.json_response({"success": True, "token": token,
                                  "qr_content": bind["qr_content"]})

    async def _qq_bind_status(self, token: str, state: dict):
        """轮询 QQ 机器人扫码绑定；扫到就把 AppID / AppSecret 写回接入方式。"""
        try:
            result = await qq_bind_poll(str(state.get("task_id") or ""),
                                        str(state.get("bind_key") or ""))
        except Exception as e:
            return web.json_response(
                {"success": False, "error": f"查询扫码状态失败: {type(e).__name__}: {e}"},
                status=400)
        status = str(result.get("status") or "pending")
        if status != "completed":
            if status == "expired":
                _CONNECTION_LOGIN_STATE.pop(token, None)
            return web.json_response({"success": True, "confirmed": False,
                                      "status": status, "platform": "qq_official"})
        app_id = str(result.get("app_id") or "")
        app_secret = str(result.get("app_secret") or "")
        conn_id = str(state.get("id") or "")
        _CONNECTION_LOGIN_STATE.pop(token, None)
        if conn_id:
            data_cfg = self.config.config
            target = next((c for c in (data_cfg.get("connections") or [])
                           if str(c.get("id")) == conn_id), None)
            if target is not None:
                target["app_id"] = app_id
                target["app_secret"] = app_secret
                self.config._atomic_save(data_cfg)
                return web.json_response({"success": True, "confirmed": True,
                                          "platform": "qq_official", "app_id": app_id,
                                          "saved": True})
        # 这条接入方式还没保存：凭据交给前端随这条一起存
        return web.json_response({"success": True, "confirmed": True,
                                  "platform": "qq_official", "app_id": app_id,
                                  "app_secret": app_secret, "saved": False})

    async def handle_connection_selftest(self, request):
        """给这条接入方式最近说过话的人发一条自检消息，看看服务端到底怎么答。

        微信侧会「收下请求却不投递」，App 侧完全看不出来——这条自检把
        「服务端收下了」与「微信里真的收到了」分开：前者看返回值，后者只能人看，
        所以自检消息本身就是那句"看到这条说明能正常送达"。
        """
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        conn_id = str(payload.get("id") or "").strip()
        conn = None
        for item in connections_of(self.config):
            if str(item.get("id")) == conn_id:
                conn = item
                break
        if conn is None:
            return web.json_response({"success": False, "error": "接入方式不存在"},
                                     status=404)
        if str(conn.get("platform") or "") != "wechat_clawbot":
            return web.json_response({"success": False, "error": "这条接入方式不支持投递自检"},
                                     status=400)
        target = recent_ilink_target(conn)
        if not target:
            return web.json_response(
                {"success": False, "error": "还没收到过这台机器人的消息，先在微信里发一条再自检"},
                status=400)
        try:
            client = build_client("wechat_clawbot", conn)
            async with client:
                data = await client.send_private_msg(target, [Text(text=SELFTEST_TEXT)])
        except Exception as e:
            return web.json_response(
                {"success": False, "error": f"自检发送失败: {type(e).__name__}: {e}"},
                status=400)
        ret = int((data or {}).get("ret") or 0)
        errcode = int((data or {}).get("errcode") or 0)
        message_id = str((data or {}).get("message_id") or (data or {}).get("msg_id") or "")
        if ret == 0 and errcode == 0 and not message_id:
            return web.json_response({
                "success": False,
                "target": _mask_preview(target),
                "ack": f"ret={ret} errcode={errcode}",
                "error": "服务端收下了却没返回 message_id，说明没有投递到微信；重新扫码登录一次再来自检",
            })
        return web.json_response({
            "success": ret == 0 and errcode == 0,
            "target": _mask_preview(target),
            "message_id": message_id,
            "ack": f"ret={ret} errcode={errcode}" + (f" message_id={message_id}" if message_id else ""),
            "error": "" if ret == 0 and errcode == 0 else f"服务端拒绝：{data}",
        })

    async def handle_connections(self, request):
        """接入方式清单；凭据只回掩码预览，不把明文发给前端。"""
        out = []
        for conn in connections_of(self.config):
            item = dict(conn)
            for key in _CONNECTION_SECRET_KEYS:
                if item.get(key):
                    item[key] = _mask_preview(item[key])
            out.append(item)
        return web.json_response({
            "success": True, "connections": out,
            "platforms": [{"id": p, "name": CONNECTION_PLATFORM_NAMES[p]}
                          for p in CONNECTION_PLATFORMS]})

    async def handle_connections_save(self, request):
        """新建或修改一条接入方式。"""
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "参数错误"}, status=400)
        platform = str(payload.get("platform") or "napcat")
        if platform not in CONNECTION_PLATFORMS:
            return web.json_response({"success": False, "error": "不支持的接入方式"},
                                     status=400)
        data = self.config.config
        conns = data.setdefault("connections", [])
        conn_id = str(payload.get("id") or "").strip()
        target = next((c for c in conns if str(c.get("id")) == conn_id), None)
        if target is None:
            # 同一个平台的同一个账号只留一条，重复触发不会堆出第二条
            identity = connection_identity(platform, payload)
            if identity:
                target = next((c for c in conns
                               if str(c.get("platform")) == platform
                               and connection_identity(platform, c) == identity), None)
        if target is None and platform == "wechat_clawbot":
            # 微信是先建一条空的、扫完码再回填凭据：还没有账号标识时认不出是同一个账号，
            # 扫码回填会另起一条，界面里那条空的就永远显示「未登录（需扫码）」
            target = next((c for c in conns
                           if str(c.get("platform")) == platform
                           and not c.get("bot_token")), None)
        if target is None:
            conn_id = conn_id or f"{platform}_{os.urandom(4).hex()}"
            target = {"id": conn_id, "platform": platform}
            conns.append(target)
        target["platform"] = platform
        target["name"] = str(payload.get("name") or "").strip() \
            or CONNECTION_PLATFORM_NAMES[platform]
        target["enabled"] = bool(payload.get("enabled", True))
        if "config_profile" in payload:
            target["config_profile"] = (str(payload.get("config_profile") or "").strip()
                                        or DEFAULT_CONFIG_PROFILE)
        for key in connection_defaults(platform):
            if key not in payload:
                continue
            value = payload[key]
            # 掩码回填表示"这次不改这个凭据"
            if key in _CONNECTION_SECRET_KEYS and _is_masked_value(value):
                continue
            target[key] = value
        self.config._atomic_save(data)
        try:
            # 只重解析角色绑定：接入方式由连接监督器周期对齐，保存后几秒内
            # 自动重连/停用，不必走整份配置的热重载（那会重建各管理器）
            self.config.roles = self.config._parse_roles()
        except Exception as e:
            return web.json_response({"success": True, "id": conn_id,
                                      "message": f"已保存，但角色绑定刷新失败（{e}），请重启程序。"})
        return web.json_response({"success": True, "id": conn_id,
                                  "message": "已保存，接入方式将在几秒内自动重连或停用。"})

    async def handle_connections_delete(self, request):
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "参数错误"}, status=400)
        conn_id = str(payload.get("id") or "").strip()
        data = self.config.config
        conns = data.get("connections") or []
        keep = [c for c in conns if str(c.get("id")) != conn_id]
        if len(keep) == len(conns):
            return web.json_response({"success": False, "error": "没有这条接入方式"},
                                     status=404)
        data["connections"] = keep
        # 角色还指着它的话一起解绑，避免留下悬空引用
        roles = data.get("roles")
        roles = list(roles.values()) if isinstance(roles, dict) else (roles or [])
        for role in roles:
            if isinstance(role, dict) and str(role.get("connection_id") or "") == conn_id:
                role.pop("connection_id", None)
        self.config._atomic_save(data)
        purged = 0
        if app_context.sender is not None:
            purged = self._purge_connection_sessions(
                app_context.sender.drop_channel_sessions(conn_id))
        if purged:
            return web.json_response(
                {"success": True,
                 "message": f"已删除，该接入方式将在几秒内自动断开，"
                            f"它的 {purged} 个会话记录与数据已一并清空。"})
        return web.json_response({"success": True,
                                  "message": "已删除，该接入方式将在几秒内自动断开。"})

    # ---------------- 用户画像 ----------------
    async def handle_profiles(self, request):
        """用户画像列表；顺带带上当前角色与这位用户的关系进度。

        关系按会话分开，所以这里给的是这位用户在每个会话里各自的一份，
        表格里那一列取关系最靠前的一份。
        """
        character_key = get_active_ctx().character_key
        profiles = []
        for uid, p in (app_context.profile_mgr.profiles or {}).items():
            item = {"user_id": uid, **(p or {})}
            if app_context.affection_mgr is not None:
                sessions = []
                for scope in (app_context.affection_mgr.records.get(character_key) or {}):
                    session_id, scope_uid = app_context.affection_mgr.split_scope(scope)
                    if scope_uid != uid:
                        continue
                    st = app_context.affection_mgr.state(character_key, uid, session_id)
                    sessions.append({
                        "session_id": session_id or f"private_{uid}",
                        "score": st["score"], "stage": st["stage"],
                        "partner": st["partner"],
                        "partner_days": (app_context.affection_mgr.partner_days(character_key, uid, session_id)
                                         if st["partner"] else 0),
                    })
                sessions.sort(key=lambda x: x["session_id"])
                item["affection_sessions"] = sessions
                if sessions:
                    item["affection"] = max(
                        sessions, key=lambda x: (x["partner"], x["score"]))
                else:
                    st = app_context.affection_mgr.state(character_key, uid)
                    item["affection"] = {"session_id": f"private_{uid}", "score": st["score"],
                                         "stage": st["stage"], "partner": st["partner"],
                                         "partner_days": 0}
            profiles.append(item)
        return web.json_response({"profiles": profiles,
                                  "affection_enabled": bool(affection_enabled(get_active_ctx()))})

    async def handle_profiles_save(self, request):
        try:
            payload = await request.json()
            uid = str(payload.get("user_id", "")).strip()
            if not uid:
                return web.json_response({"success": False, "error": "user_id 不能为空"}, status=400)
            app_context.profile_mgr.update(uid, payload.get("profile", {}), replace=True)
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_profiles_delete(self, request):
        try:
            payload = await request.json()
            ok = app_context.profile_mgr.delete(str(payload.get("user_id", "")))
            return web.json_response({"success": ok})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    # ---------------- 自主学习 ----------------
    async def handle_lexicon(self, request):
        if app_context.lexicon_mgr is None:
            return web.json_response({"terms": [], "pending": [], "enabled": False})
        return web.json_response({"terms": app_context.lexicon_mgr.list_terms(),
                                  "pending": app_context.lexicon_mgr.list_pending(),
                                  "enabled": app_context.lexicon_mgr.enabled})

    async def handle_lexicon_confirm(self, request):
        try:
            payload = await request.json()
            ok = app_context.lexicon_mgr is not None and app_context.lexicon_mgr.confirm(
                str(payload.get("id", "")), payload.get("term"),
                payload.get("meaning"), payload.get("category"))
            return web.json_response({"success": ok})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_lexicon_reject(self, request):
        try:
            payload = await request.json()
            ok = app_context.lexicon_mgr is not None and app_context.lexicon_mgr.reject(str(payload.get("id", "")))
            return web.json_response({"success": ok})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_lexicon_save(self, request):
        try:
            payload = await request.json()
            ok = app_context.lexicon_mgr is not None and app_context.lexicon_mgr.upsert_term(
                payload.get("term", ""), payload.get("meaning", ""), payload.get("category", ""))
            return web.json_response({"success": ok})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_lexicon_delete(self, request):
        try:
            payload = await request.json()
            ok = app_context.lexicon_mgr is not None and app_context.lexicon_mgr.delete_term(payload.get("term", ""))
            return web.json_response({"success": ok})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    # ---------------- 表情包 ----------------
    async def handle_stickers_list(self, request):
        root = app_context.sticker_mgr.dir if app_context.sticker_mgr else Path("data/stickers")
        categories = []
        if root.exists():
            for folder in sorted(root.iterdir()):
                if folder.is_dir():
                    files = [f.name for f in sorted(folder.iterdir())
                             if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}]
                    if files:
                        categories.append({"name": folder.name, "files": files})
        return web.json_response({"root": str(root), "categories": categories,
                                  "enabled": bool(app_context.sticker_mgr and app_context.sticker_mgr.enabled),
                                  "mode": app_context.sticker_mgr.mode if app_context.sticker_mgr else "off"})

    async def handle_stickers_upload(self, request):
        try:
            reader = await request.multipart()
            category = ""
            saved = []
            async for part in reader:
                if part.name == "category":
                    category = (await part.text()).strip()
                elif part.filename:
                    folder = _safe_subdir(app_context.sticker_mgr.dir, category)
                    if folder is None:
                        return web.json_response({"success": False, "error": "分类名非法"}, status=400)
                    folder.mkdir(parents=True, exist_ok=True)
                    fname = safe_sticker_name(Path(part.filename).name)
                    if not fname or Path(fname).suffix.lower() not in IMAGE_EXTS:
                        continue
                    payload_bytes = await part.read(decode=False)
                    if len(payload_bytes) > 10 * 1024 * 1024:
                        continue
                    (folder / fname).write_bytes(payload_bytes)
                    saved.append(f"{category}/{fname}")
            app_context.sticker_mgr.rescan()
            return web.json_response({"success": True, "saved": saved})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_stickers_auto_import(self, request):
        """「一键识别」：把选中的图片/文件夹交给识图模型分类命名后归档。"""
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "请求体不是 JSON"}, status=400)
        paths = payload.get("paths") or []
        if isinstance(paths, str):
            paths = [paths]
        paths = [str(p) for p in paths if str(p or "").strip()]
        if not paths:
            return web.json_response({"success": False, "error": "先选择图片或文件夹"}, status=400)
        if app_context.sticker_mgr is None:
            return web.json_response({"success": False, "error": "表情包管理器不可用"}, status=400)
        images, skipped = collect_import_images(paths, library=app_context.sticker_mgr.dir)
        if not images:
            reason = "；".join(skipped[:3]) or "选中的内容里没有图片"
            return web.json_response({"success": False, "error": reason}, status=400)
        job = start_import_job(app_context.global_config, app_context.sticker_mgr, paths)
        return web.json_response({"success": True, **job.snapshot()})

    async def handle_stickers_auto_import_status(self, request):
        job = get_import_job(request.query.get("job_id", ""))
        if job is None:
            return web.json_response({"success": False, "error": "任务不存在"}, status=404)
        return web.json_response({"success": True, **job.snapshot()})

    async def handle_stickers_delete(self, request):
        try:
            payload = await request.json()
            folder = _safe_subdir(app_context.sticker_mgr.dir, payload.get("category", ""))
            fname = safe_sticker_name(payload.get("name", ""))
            if folder is None or not fname:
                return web.json_response({"success": False, "error": "参数非法"}, status=400)
            target = folder / fname
            if target.exists():
                target.unlink()
            app_context.sticker_mgr.rescan()
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_stickers_file(self, request):
        category = request.query.get("category", "")
        fname = safe_sticker_name(request.query.get("name", ""))
        folder = _safe_subdir(app_context.sticker_mgr.dir, category) if app_context.sticker_mgr else None
        if folder is None or not fname:
            return web.Response(status=404, text="not found")
        if Path(fname).suffix.lower() not in IMAGE_EXTS:
            return web.Response(status=404, text="not found")
        target = folder / fname
        # 类型按扩展名给出：aiohttp 内置的 MIME 表里没有 .webp，
        # 猜不出类型会退回 application/octet-stream，配上 nosniff 后浏览器拒绝渲染
        return await _local_file_response(
            target, MIME_BY_EXT.get(target.suffix.lower(), "application/octet-stream"))

    async def start(self):
        host = self.config.get("webui_host", "127.0.0.1")
        base_port = int(self.config.get("webui_port", 11500))
        if not HAS_AIOHTTP:
            print("未安装 aiohttp，WebUI 不可用")
            return

        # 尝试多个端口，若被占用则自动递增，最多尝试 10 次
        max_tries = 10
        for attempt in range(max_tries):
            port = base_port + attempt
            try:
                print(f"正在启动 WebUI：http://{host}:{port}")
                runner = web.AppRunner(self.app)
                await runner.setup()
                site = web.TCPSite(runner, host, port)
                await site.start()
                print(f"WebUI 已启动：http://{host}:{port}")
                self.runner = runner
                # 若使用了非默认端口，更新配置
                if port != base_port:
                    self.config.config["webui_port"] = port
                    # 可选：持久化到配置文件
                    self.config._atomic_save(self.config.config)
                return
            except OSError as e:
                # 端口被占用或其他系统错误，尝试下一个端口
                print(f"端口 {port} 不可用（{e}），尝试下一个端口...")
                await runner.cleanup()  # 清理失败的 runner
            except Exception as e:
                error_msg = f"WebUI 启动失败（端口 {port}）：{type(e).__name__}: {e}"
                print(error_msg)
                try:
                    with open(runtime_path("webui_error.log"), "a", encoding="utf-8") as f:
                        f.write(f"{time.ctime()} - {error_msg}\n")
                except Exception:
                    pass
                return  # 其他异常不自动切换，直接记录并退出

        # 所有端口尝试失败
        print("错误：无法找到可用端口，WebUI 启动失败。")
        try:
            with open(runtime_path("webui_error.log"), "a", encoding="utf-8") as f:
                f.write(f"{time.ctime()} - 所有端口被占用，WebUI 启动失败。\n")
        except Exception:
            pass

    async def shutdown(self):
        if not HAS_AIOHTTP:
            return
        runner = getattr(self, "runner", None)
        if runner is not None:
            # 只 cleanup app 不会停止 TCPSite 的监听，端口仍被占用；
            # 热重启/测试流程会因此碰到 "Address already in use"
            try:
                await runner.cleanup()
            except Exception as e:
                print(f"关闭 WebUI 监听失败: {type(e).__name__}: {e}")
            self.runner = None
        await self.app.cleanup()


async def ensure_tts_service_enabled_check() -> bool:
    try:
        from modules.tts_service import check_tts_service
        return await check_tts_service(app_context.global_config)
    except Exception:
        return False


