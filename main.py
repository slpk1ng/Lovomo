# 注：代码大部分由 Deepseek、GLM、混元等大模型协助生成，可能存在不准确或不完整的地方，请谨慎使用。 

import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import random
import re
import shutil
import sys
import threading
import time
import traceback
import zipfile
from base64 import b64encode, b64decode
from pathlib import Path
from typing import Optional, Dict, Any, List
from urllib.parse import urlsplit

from modules.app_paths import (
    _data_signature,
    _harden_stdio,
    _migrate_user_data,
    _probe_writable,
    _remove_path,
    _resolve_data_dir,
    adopt_user_data,
    app_dir,
    get_resource_path,
    runtime_path,
    user_data_dir,
)

from modules.security import (
    _API_KEY_KEYS,
    _CONNECTION_SECRET_KEYS,
    _SECRET_FIELD_KEYS,
    _WEBUI_PASSWORD_KEYS,
    _decrypt_api_keys,
    _decrypt_webui_password,
    _encrypt_api_keys,
    _encrypt_webui_password,
    _is_masked_value,
    _looks_like_plaintext_secret,
    _mask_preview,
    _needs_second_password,
    _password_matches,
    _scrub_plaintext_secrets,
    _SECRET_REDACTION,
    _decrypt_value,
    _encrypt_value,
)


_harden_stdio()


import httpx
try:
    from napcat import (NapCatClient, PrivateMessageEvent, GroupMessageEvent, Text, Record,
                        Image, At, Reply, Forward, FriendPokeEvent, GroupPokeEvent)
except ImportError:
    print("错误：未安装 napcat-sdk，请先运行 pip install napcat-sdk")
    raise

# ---------------- 功能模块 ----------------
from modules.app_context import (
    HAS_AIOHTTP, HAS_WEBVIEW,
    _APP_HOOKS, ROLE_CONNECTIONS, _SESSION_LOCKS, _WEBUI_SERVER_HOLDER,
    _WEBVIEW_WINDOW_HOLDER, _member_cache, _proactive_state_date,
    _role_emotions_cache, _role_mimics_cache, _spam_log,
    affection_mgr, db, encounter_mgr, event_mgr, global_config,
    global_emotion_manager, job_mgr, last_interaction, last_proactive_sent,
    last_user_activity, lexicon_mgr, memory_manager, mood_mgr, napcat_client,
    profile_mgr, promise_mgr, proactive_awaiting, proactive_counts,
    proactive_pending, rag_mgr, recall_mgr, scheduler, sender, stats_mgr,
    sticker_mgr, todo_mgr, tool_registry,
)
from modules import app_context

from modules.companion_tasks import (
    _SELF_REFERENCE_RE, _ATTRIBUTED_SELF_RE, _REAL_SESSION_RE,
    CONTEXT_RECHECK_MIN_INTERVAL, RELATION_RECHECK_PROMPT,
    MOOD_DIARY_CATCHUP_DAYS, MOOD_DIARY_MAX_SESSIONS_PER_DAY,
    MOOD_DIARY_SILENT_MILESTONES, MOOD_DIARY_MAX_DELIVER_PER_RUN,
    DIARY_MAX_OVERLAP, DIARY_NO_COPY_WARNING, MOOD_DIARY_MAX_AGE_DAYS,
    in_character_background, background_ready, generate_neutral_text,
    _json_true, session_user_id, recheck_relationship, recheck_profile,
    recheck_lexicon, context_recheck, recheck_enabled, _recheck_due,
    _take_recall_block, post_reply_context_tasks, proactive_idle_check,
    _companion_send, _detect_rival_mention, _to_float, _mood_bounds_now,
    _is_real_session, _diary_material, _diary_overlap, _private_sessions_of,
    _session_last_user_day, write_silent_mood_diaries, write_mood_diaries,
    deliver_mood_diaries, mood_diary_note, mood_diary_catchup_task,
    companion_daily_check, companion_catchup_task, _extract_and_store_promise,
    _summary_keep_count, dialog_history_block, session_history_block,
    _known_sessions, greeting_daily_check, greeting_catchup_task,
    send_task_lock, _guarded, register_feature_jobs, hot_reload_managers,
)
from modules.proactive_state import (
    _PROACTIVE_STATE_FILE, _proactive_state_path, load_proactive_state,
    save_proactive_state, _session_last_user_ts, seed_proactive_sessions,
    session_memory_exists, forget_proactive_session,
)
from modules.log_console import (
    LOG_MAX_SIZE_MB_DEFAULT, LOG_SOURCE_MAIN, LOG_SOURCE_PLUGIN,
    LOG_LEVEL_INFO, LOG_LEVEL_WARN, LOG_LEVEL_ERROR, _LOG_TRIM_KEEP_RATIO,
    _LOG_PREFIX_RE, _LOG_ERROR_RE, _LOG_ERROR_ANYWHERE_RE, _LOG_WARN_RE,
    _LOG_ARROW_RE, _LOG_CONTINUATION_RE, _LOG_BANNER_RE,
    _CONSOLE_LEVEL_MARK_RE, _CONSOLE_CN_LEVEL_MARK_RE, _CONSOLE_WARN_SIGN_RE,
    _CONSOLE_WINDOW_MARK_RE, _CONSOLE_PLUGIN_MARK_RE, _LOG_LEVEL_NAMES,
    _last_console_level, log_stamp, log_prefix, _log_head,
    console_log_level, console_log_line, _LogFormatter, _CappedFileHandler,
    apply_log_max_size, global_log_buffer, LOG_BUFFER_MAX, LOG_TAIL_DEFAULT,
    _LOG_FULL_MAX_CHARS, _LOG_PENDING_MAX_CHARS, log_lock,
    _runtime_log_limits, StdoutRedirector, log_window_event, dump_log_buffer,
)
# __main__ 块 install_update_package、tests 与 _ui_server 经 import main 使用）
from modules.updater_tools import (
    _APP_RELEASE_REPO, _PROGRESS_LINE, _UPDATE_LOCK, _UPDATE_STATE,
    cleanup_old_installers, find_ready_installer, is_own_release_asset,
    log_progress, parse_installer_version, update_dir, update_progress_line,
)
from modules.single_instance import (
    _pid_alive, _kernel32, _acquire_single_instance,
    _signal_existing_instance, _start_instance_show_waiter,
    _candidate_icon_paths,
    _INSTANCE_MUTEX_NAME, _INSTANCE_SHOW_EVENT_NAME, _INSTANCE_STATE,
)
if HAS_WEBVIEW:
    import webview
if HAS_AIOHTTP:
    from aiohttp import web
from modules.database import DatabaseManager
from modules.scheduler import get_scheduler, SchedulerManager
from modules.stats import StatsManager
from modules.stickers import (StickerManager, IMAGE_EXTS, MIME_BY_EXT, safe_sticker_name,
                              collect_import_images, start_import_job, get_import_job,
                              DEFAULT_CAPTURE_PROMPT)
from modules.audio_level import (SPEECH_MIN_RATIO, measure as measure_audio,
                                 pitch_note, quality_notes as audio_quality_notes)
from modules.tools import ToolRegistry
from modules.profiles import UserProfileManager, DEFAULT_EXTRACT_PROMPT
from modules.lexicon import LexiconManager, DEFAULT_LEARN_PROMPT, DEFAULT_INJECT_TEMPLATE
from modules.rag import RAGManager, extract_text_from_file
from modules.recall import HistoryRecallManager
from modules.todo_manager import TodoManager, DEFAULT_EXTRACT_PROMPT as TODO_EXTRACT_PROMPT
from modules.jobs import ScheduledJobManager, generate_proactive_text
from modules.events import EventManager
from modules.plugin_publisher import (publish_plugin as _publish_to_github,
                                       unpublish_plugin as _unpublish_from_github,
                                       verify_token as _verify_github_token,
                                       PublishError as _PublishError)
from modules.mood import (MoodManager, clamp, commit_mood, current_mood, judge_and_decide,
                          judge_enabled, mood_enabled, mood_style, affection_enabled,
                          stored_mood, _parse_key as _parse_mood_key)
from modules.affection import (AffectionManager, SCORE_MAX, commitment_warning,
                               looks_like_acceptance, stage_of)
from modules.promises import PromiseManager, PROMISE_HINT_RE, extract_promise
from modules.encounters import EncounterManager
from modules.sender import (MessageSender, VoicePacer, voice_enabled_for, recall_delay_of,
                            DEFAULT_RECALL_DELAY_SECONDS)
from modules.adapters import (ILinkSessionExpired, build_client, client_supports,
                              friendly_user_label, ilink_login_qr, ilink_login_status,
                              recent_ilink_target, make_event, _first as _adapter_first)
from modules.ghmirror import DEFAULT_MIRRORS, parse_mirrors
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

from modules.llm_helpers import (RoleContext, build_chat_messages, chat_once,
                                chat_with_tools, normalize_sentences,
                                normalize_single, sentence_obj_has_text,
                                sentence_obj_has_action,
                                split_multi_clause_sentences,
                                stream_chat, extract_json, strip_thinking,
                                SentenceStreamParser, get_image_reply, download_image,
                                sniff_image_mime, repair_sentence_lang, strip_quote_note,
                                segment_for_tts, speaker_labeled_lines, build_speaker_labels,
                                identity_note, recent_user_ids, wants_mention_request,
                                MENTION_ALL_ID,
                                wants_quote_request, MENTION_PLACEHOLDER,
                                strip_mention_placeholder, POKE_MESSAGE_TEXT,
                                recall_request_kind,
                                SENTENCE_ACTION_KEYS, DELIVERY_MODES, DELIVERY_PLAIN,
                                apply_literal_mention,
                                text_needs_tools, tool_flow_can_skip,
                                asks_self_context,
                                image_self_claim, image_identity_note,
                                IMAGE_CLAIM_WARNING, sent_links, record_sent_links,
                                urls_in_text, is_search_request, is_search_dissatisfied,
                                lang_text_broken, translate_to_lang,
                                available_mimics, set_mimics_provider,
                                model_list_endpoints, looks_like_full_endpoint,
                                error_reply_text, auto_load_local_models)
from modules.reply_pipeline import (
    _URL_RE, _LINK_REQUEST_RE, _SEARCH_ENTRY_URL_RE, _SEARCH_ENTRY_ABS_RE,
    _SEARCH_ENTRY_TITLE_RE, _USER_ECHO_MIN_CHARS, _RETRY_TEMPERATURE,
    _RETRY_TEMPERATURE_MAX, _REPEAT_GUARD_KEYS,
    SentenceSink,
    _tool_notes_from_trace, _tool_requested, _search_entries,
    _search_entry_titles, _entry_title_terms, _title_mentioned, _reply_terms,
    _append_missing_links, _sticker_judgement_from_llm,
    generate_reply, generate_reply_stream,
    _spawn, _norm_text, _repeat_ratio, _strip_image_claims,
    _recent_assistant_replies, _max_repeat_ratio,
    repeat_thresholds, repeat_guard_flags, repeat_guard_summary,
    repeat_guard_active, _log_mood_commit, _judge_mood_text,
    _judge_gate_cause, _image_reply_override,
)
from modules.media_cache import (
    _IMAGE_EXT, VOICE_OUT_FORMAT, VOICE_FALLBACK_SUFFIX,
    _PENDING_IMAGES, _PENDING_IMAGE_TTL, _PENDING_IMAGE_MAX_TRIES,
    _IMAGE_REF_RE, _IMAGE_CACHE_CHECK_INTERVAL, _image_cache_last_check,
    _AVATAR_CACHE_TTL, _AVATAR_CACHE_MAX_FILES, _QQ_AVATAR_URL, _QQ_NUMBER_RE,
    FORWARD_MAX_NODES, FORWARD_MAX_CHARS, FORWARD_MAX_IMAGES,
    auto_capture_from_images, _sticker_capture_args, _spawn_sticker_capture,
    pick_media_source, refresh_image_urls,
    _remember_pending_image, _take_pending_image, _clear_pending_image,
    _backfill_image_description, _image_cache_dir, _cache_image_bytes,
    _attach_history_image, _avatar_cache_dir, _prune_avatar_cache,
    _active_bot_qq, qq_avatar_bytes, _diary_recognition_note,
    _download_image_to_cache, _download_audio_to_temp, transcribe_voice_message,
    _render_ob11_segments, fetch_forward_text, fetch_quoted_context,
)
from modules.tts import synthesize_sentence, resolve_tts_path
from modules.tls import verified_context
from modules.audio_trim import normalize_audio
from modules.asr import (start_job as start_asr_job, get_job as get_asr_job,
                         AUDIO_MIMES)
from modules.tts_service import (process_manager, ensure_tts_service,
                                 auto_start_and_switch_tts, mark_exiting)
from modules import machine_capacity
from modules.tts_cloud import cloud_emotion_names, is_cloud_tts
from modules.config_presets import (DEFAULT_PROFILE as DEFAULT_CONFIG_PROFILE,
                                    delete_profile as delete_config_profile,
                                    forget_profile_loaders,
                                    list_profiles as list_config_profiles,
                                    load_profile as load_config_profile,
                                    preset_dir as config_preset_dir,
                                    profile_loader as config_profile_loader,
                                    save_profile as save_config_profile)
from modules.window_geometry import (
    _GEOMETRY_VERSION,
    _WINDOW_GEOMETRY,
    _WINDOW_GEOMETRY_FILE,
    _apply_saved_geometry,
    _default_normal_geometry,
    _ensure_dpi_aware,
    _finish_geometry_save,
    _geometry_path,
    _geom_looks_like_fullscreen,
    _install_taskbar_activate_hook,
    _is_geometry_valid,
    _load_window_geometry,
    _logical_to_phys,
    _normal_rect,
    _phys_to_logical,
    _primary_screen_size,
    _raise_to_foreground,
    _rect_is_degenerate,
    _sanitize_normal_geometry,
    _save_window_geometry,
    _schedule_geometry_save,
    _screen_rects,
    _show_state,
    _uninstall_taskbar_activate_hook,
    _webview_profile_dir,
    _window_hwnd,
    _window_hide,
    _window_rect,
    _window_scale,
    _window_state_name,
    _window_visible,
)
import modules.webview_runtime
# 三个模块内可变状态（_WEBVIEW_PROCESS_DEAD/_FAILED_WATCHED/_WEBVIEW_LOCK）
from modules.webview_runtime import (
    _WEBVIEW_CACHE_DIRS,
    _WEBVIEW_CACHE_KEY_FILE,
    _webview2_form,
    _webview2_on_ui,
    _webview2_release,
    _webview2_rebuild,
    _remove_cache_dir,
    attach_process_failed_watch,
    clear_webview_cache_and_reload,
    purge_webview_cache,
    webview2_control_alive,
    webview_cache_key,
)
from modules.config_loader import (
    CHAT_STYLE_PROMPT,
    LEGACY_AFFECTION_PHRASES,
    CONNECTION_PLATFORMS,
    CONNECTION_PLATFORM_NAMES,
    ConfigLoader,
    _migrate_chat_style_prompt,
    _migrate_murasame_intimacy,
    _migrate_sticker_mode,
    _migrate_profile_prompts,
    _migrate_emotion_prompts,
    _migrate_sticker_capture_prompt,
    _migrate_learn_prompts,
    _migrate_market_repo,
    connection_defaults,
    client_snapshot,
    typing_client,
    connection_target_label,
    connection_identity,
    _migrate_connections,
    _sanitize_connections,
    _dedupe_connections,
)
from modules.emotion_voices import (
    EmotionManager,
    _AUDIO_EXTS,
    _SYSTEM_DIRS,
    _MAX_QUALITY_CHECK,
    _is_reserved,
    _read_sidecar_text,
    _is_emotion_folder,
    _audio_note_kind,
    _report_audio_quality,
    _check_ref_audio_quality,
    _median_pitch,
    _scan_ref_root,
)
from modules.memory_store import (
    MemoryManager,
    _MEMORY_FILE_RE,
    _SANITIZED_ID_SUFFIXES,
    _is_memory_filename,
    restore_session_id,
    merge_restored_keys,
    _memory_session_id,
)
from modules.session_context import (
    list_known_sessions,
    _log_hide_patterns,
    _log_hidden,
    whitelist_ids,
    _session_whitelisted,
    _allow_message,
    get_active_role,
    get_active_ctx,
    reply_ctx_of,
    role_memory,
    get_active_emotions,
    get_active_mimics,
    _load_role_voices,
    get_role_emotions,
    get_role_mimics,
    parse_session_target,
    _fetch_member_name,
    _in_quiet_hours,
    _parse_jitter_minutes,
    connections_of,
    connection_role_key,
    config_for_connection,
    role_connection_snapshot,
    build_connection_profiles,
    resolve_target_roles,
)
from modules.webui_common import (
    _json_file_response,
    _local_file_response,
    _brief_response,
    _safe_name,
    _safe_subdir,
    _emotion_audio_file,
    _write_text_file,
    _webui_error_middleware,
    _same_origin,
    _make_auth_middleware,
    _plugin_runtime,
    _parse_plugin_command,
    _dispatch_plugin_command,
    _dispatch_plugin_message,
    _plugin_log,
    _gguf_general_name,
)

from modules.message_pipeline import (
    _SESSION_PENDING, _COALESCE_WINDOW,
    _touch_pending, _extract_event_info, _merge_event_info, _merged_payload,
    _spawn_drainer, _remember_unaddressed_message, _queue_unaddressed_message,
    _fetch_user_nickname, _poke_to_message_event, handle_message_event,
    _process_message_event,
)


# tests 与 _ui_server 经 import main 使用）
from modules.webui_server import (
    MAX_REACTION_LOOKUPS, MAX_BRANCH_PAGES, MARKET_SOURCE_REPOS_MAX,
    PLUGIN_BRIDGE_TEMPLATE, PUBLISH_STATE_TTL, SELFTEST_TEXT, TODO_STATUSES,
    _ROLE_EXPORT_KEYS, TEST_CHAT_SESSION, TEST_CHAT_USER, _CONNECTION_LOGIN_STATE,
    WebUIServer, ensure_tts_service_enabled_check,
)


try:
    logging.basicConfig(filename=str(runtime_path("app.log")), encoding="utf-8",
                        level=logging.INFO)
except Exception:
    logging.basicConfig(level=logging.INFO)
for _handler in logging.getLogger().handlers:
    _handler.setFormatter(_LogFormatter())


# ============================================================================
# 主入口
# ============================================================================

async def main(stop_event: threading.Event = None):
    global db, stats_mgr, sticker_mgr, tool_registry, profile_mgr, rag_mgr
    global lexicon_mgr
    global todo_mgr, job_mgr, event_mgr, sender, mood_mgr, affection_mgr
    global promise_mgr, recall_mgr, encounter_mgr
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding='utf-8')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding='utf-8')

    sys.stdout = StdoutRedirector(sys.stdout)
    os.environ["NAP_CAT_PLUGIN_INDEX_URL"] = ""
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    os.environ["no_proxy"] = "localhost,127.0.0.1"

    print("=" * 180)
    print(
        "                   ⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠟⣛⣩⣤⣶⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣶⣦⣬⣉⠛⠀⠀⠀⠀⠀⢛⣋⣩⣥⠴⠶⠶⠟⠛⠛⠛⠛⠛⠛⠛⠻⠿⠷⠶⢶⣦⣤⣍⣉⡛⠛⠿⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "                   ⣿⣿⣿⣿⣿⣿⣿⣿⣿⠟⣋⣥⣶⠿⣛⣭⣷⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠟⣋⠁⠀⠀⠄⢒⣋⣩⣥⣴⣶⣶⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣶⣶⣦⣭⣍⣛⠻⢷⣶⣤⣍⣙⠛⠿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "                   ⣿⣿⣿⣿⣿⣿⠟⣋⣴⡾⢟⣫⣴⠾⣻⣿⣿⣿⣿⠿⠿⠿⠟⠛⠛⠛⠛⠛⠉⠀⠉⣀⣤⣴⣶⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣮⣝⣿⣿⣿⣶⣦⣌⡙⠻⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "                   ⠿⠿⠿⠿⢛⣡⡾⢟⣩⣶⠿⠋⠗⣛⣉⣥⣤⠤⠶⣒⣒⣚⡯⠭⣉⡭⠛⢁⣤⣶⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣦⣌⠙⠿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "                   ⣀⣀⢀⡴⠟⣋⣐⣩⡤⢴⣒⣻⣭⣵⣶⠿⢟⣛⡭⠽⠖⠚⠋⠉⣁⣴⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣻⠿⣶⣄⡙⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "                   ⣫⡥⠖⣚⣩⣵⣶⣾⣿⠿⣿⣛⠭⠖⠚⣉⣩⣤⣶⡶⠟⢋⣤⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠿⣟⣛⣯⣽⣷⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣶⣭⡛⢦⣌⠙⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "                   ⣥⣾⣿⣿⠿⣟⡫⠵⠚⣋⣡⣤⣶⣾⣿⡿⠟⠋⠁⢀⣴⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠿⣟⣯⣵⣶⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣌⠳⣤⡉⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "                   ⢿⣛⠭⠒⣉⢅⣴⣾⣿⣿⣿⣿⠿⠋⠁⠀⠀⢀⣴⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⢛⣭⣶⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠿⣻⣽⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣎⠻⣦⡈⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "                   ⣩⡴⢠⡿⣣⣾⣿⣿⠿⠛⠉⠀⠀⠀⠀⢀⣴⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⣻⣵⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠟⣫⣶⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⣫⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣮⣝⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣌⢿⣦⡈⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "                   ⠿⣱⡟⣵⡿⠟⠋⠁⠀⠀⠀⢀⡤⠂⣴⣿⣿⣿⣿⣿⣿⣿⣿⡿⣛⣵⣿⣿⣿⣿⣿⣿⣿⣿⢟⣿⣿⡿⣋⣴⣿⣿⣿⣿⣿⣿⣿⠟⣫⣾⣿⣿⣿⣿⡿⣫⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣌⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣧⡹⣿⣆⠙⠀⠀⠀⢿⣿⣿⣿⣿⣿⣿⣿\n"
        "                   ⠀⠟⠘⠉⠀⠀⠀⠀⢀⣤⣾⠟⣠⣾⣿⣿⣿⣿⣿⣿⣿⡿⣫⣾⣿⣿⣿⣿⣿⣿⣿⣿⣯⣾⣿⠟⣡⣾⣿⣿⣿⣿⣿⣿⣿⠟⣡⣾⣿⣿⣿⣿⣿⢏⣼⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⡌⠻⣿⣿⣿⣿⣿⣿⣿⣿⣷⡌⢻⣷⡈⠛⠛⠛⠛⠛⠻⠿\n"
        "                   ⣇⠀⠀⠀⠀⣀⣴⣾⣿⡿⢃⣴⣿⣿⣿⣿⣿⣿⣿⡿⣫⣾⣿⣿⣿⣿⣿⣿⣿⡿⣫⣾⣿⠟⣡⣾⣿⣿⣿⣿⣿⣿⣿⠟⣡⣾⣿⣿⣿⣿⣿⡟⣱⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣦⡘⢿⣿⣿⣿⣿⣿⣿⣿⣿⡄⠳⠟⢠⡒⢦⠄⣀⣀⣤\n"
        "                   ⣞⣆⢀⣴⣾⣿⣿⣿⠟⢡⣾⣿⣿⣿⣿⣿⣿⣿⣫⣾⣿⣿⣿⣿⣿⣿⣿⡿⣫⣾⣿⠟⣡⣾⣿⣿⣿⣿⣿⣿⣿⡿⡡⣾⣿⣿⣿⣿⣿⣿⢋⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣄⢻⣿⣿⣿⣿⣿⣿⣿⣿⡄⢠⣦⠙⠎⣰⣷⣿⣿\n"
        "                   ⠿⠜⣄⠻⣿⣿⣿⠏⣰⣿⣿⣻⣿⣿⣿⣿⣟⣵⣿⣿⣿⣿⣿⣿⣿⣿⢫⣾⣿⡿⢋⣾⣿⣿⣿⣿⣿⣿⣿⣿⢏⢴⣾⣿⣿⣿⣿⣿⡿⣱⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢹⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣯⢦⠹⠿⠿⣿⣿⣿⣿⣿⣿⡀⠃⠀⠀⠹⣿⣿⣿\n"
        "                   ⠉⠉⠙⠂⠹⣿⠃⣼⣿⡿⣱⣿⣿⣿⣿⢯⣾⣿⣿⣿⣿⣿⣿⣿⢟⣵⣿⣿⠏⣴⣿⣿⣿⣿⣿⣿⢿⢿⠟⠡⢢⣿⣿⣿⣿⣿⣿⠟⡼⣽⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠇⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡟⣿⣿⣿⣿⣿⣿⣿⢎⣴⣾⣷⡹⣿⣿⣿⣿⣿⣧⠀⠀⠀⢠⠘⣿⣿\n"
        "                   ⣦⡀⠀⠀⠀⢀⣼⣿⡿⣱⣿⣿⣿⡿⣳⣿⣿⣿⣿⣿⣿⣿⡿⢫⣾⣿⡿⢡⣾⣿⣿⣿⣿⣿⣿⣿⡿⠃⡴⣱⣿⣿⣿⣿⣿⣿⢏⣞⣽⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⣹⡟⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠸⣿⣿⣿⡿⡿⢡⣾⣿⣿⣿⣇⢹⣿⣿⣿⣿⣿⡄⠀⠀⢸⢣⠘⣿\n"
        "                   ⣿⣿⣦⡀⢀⣾⣿⣿⢡⣿⣿⣿⡿⣱⣿⣿⣿⣿⣿⣿⣿⡟⣱⣿⣿⠟⣰⣿⣿⣿⣿⣿⣿⣿⣿⢟⡔⡜⣼⣿⣿⣿⣿⣿⣿⢏⣞⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢃⣿⢁⣿⣿⣿⣿⣿⣿⣿⣿⢿⣿⣿⣿⣿⣿⡆⢿⣿⣿⣿⢁⣾⣿⠿⠟⠛⠛⠈⣿⣿⣿⣿⣿⣧⠀⠀⠈⣏⢧⠸\n"
        "                   ⣿⣿⣿⠃⣼⣿⣿⢣⣿⣿⣿⣿⣱⣿⣿⣿⣿⣿⣿⣿⢏⣼⣿⣿⠏⣼⣿⣿⣿⣿⣿⣿⣿⣿⢃⠞⢜⣾⣿⣿⣿⣿⣿⣿⢏⡞⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⣼⠃⢸⣿⣿⣿⣿⣿⣿⣿⡟⣾⣿⣿⣿⣿⣿⡇⢸⣿⣿⡏⢸⢿⣧⠀⠀⠀⠀⠀⢹⣿⣿⣿⣿⣿⠀⠀⠀⠸⡌⢧\n"
        "                   ⠻⣿⠃⣼⣿⣿⢇⣾⣿⣿⣿⢳⣿⣿⣿⣿⣿⣿⣿⢋⣾⣿⣿⢋⣾⣿⣿⣿⣿⣿⣿⣿⡿⢡⡏⢌⣾⣿⣿⣿⣿⣿⣿⢏⡞⣼⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⢰⡟⠀⣾⣟⢿⣿⣿⣿⣿⣿⢃⣿⣽⣿⣿⣿⣿⡇⢸⣿⣿⡇⠀⠀⢀⠀⠀⠀⠀⠀⠸⣿⣿⣿⣿⣿⡇⠀⠀⣆⠗⢋\n"
        "                   ⣷⠆⣸⣿⣿⡟⣼⣿⣿⣿⢧⣿⣿⣿⣿⣿⣿⡿⠃⠞⠛⠻⠁⠘⠛⠿⠿⣿⣿⣿⣿⡿⣱⡟⢈⣾⣿⣿⣿⣿⣿⣿⡏⡼⣹⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢃⡿⡡⢸⣿⣿⣷⣿⡻⣿⣿⡟⣸⣧⣿⣿⣿⣿⣿⡇⢸⣿⣿⡇⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⣿⡇⠀⣠⠴⠚⠉\n"
        "                   ⡟⢠⣿⣿⣿⢱⣿⣿⣿⡟⣾⣿⣿⣿⣿⣿⣦⢀⣀⠀⠠⠁⠀⠀⠀⠀⠀⠀⠉⠛⠿⣱⣿⢁⣾⣿⣿⣿⣿⣿⣿⡟⣸⢳⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡏⣾⢣⢇⣿⣿⣿⣿⣿⣿⣿⣿⢡⡿⣼⣿⣿⣿⣿⣿⡇⣼⣿⣿⣷⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣿⡿⠋⠀⠀⠀⠀⠀⠀\n"
        "                   ⠀⣿⣿⣿⠇⣿⣿⣿⣿⢱⣿⣿⣿⣿⣿⣿⢣⣿⣿⣿⠀⣀⣁⢤⣤⣄⣀⡀⠀⠀⠀⠈⠁⢼⣿⣿⣿⣿⣿⣿⣿⢡⡏⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⣸⠏⡞⣸⣿⣿⣿⣿⣿⣿⣿⠇⣾⢳⣿⣿⣿⣿⣿⣿⠃⣿⣿⣿⡟⠂⠀⠀⠀⠀⠀⠀⠀⠀⣿⡿⠋⠀⠀⠀⠀⠀⠀⠀⠀\n"
        "                   ⣸⣿⣿⡿⣸⣿⣿⣿⡇⣾⣿⣿⣿⣿⣿⢏⣾⣿⣿⡇⢠⣿⣿⣷⣮⣝⡻⠿⠋⠀⠀⠀⠀⠀⠙⢿⣿⣿⣿⣿⠇⡾⣸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣱⡟⣼⢣⣿⣿⣿⣿⣿⣿⣿⡟⣰⡏⣿⣿⣿⣿⣿⣿⣿⢠⣿⣿⡟⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠊⠀⠀⠀⠀⠀⠀⠀⡀⢀⣼\n"
        "                   ⣿⡿⣿⠇⣿⣿⣿⣿⢠⣿⣿⣿⣿⣿⡟⣾⣿⣿⣿⠁⣼⣿⣿⣿⣿⣿⣿⠁⠀⠀⠀⠀⠀⠀⠀⠀⠹⣿⣿⡟⢰⣇⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢣⡿⣰⡏⣼⣿⣿⣿⣿⣿⣿⡟⣰⡿⣽⣿⣿⣿⣿⣿⣿⡇⢸⣿⢸⡃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣀⡀⠐⢈⣴⡿⢋\n"
        "                   ⣿⢻⣿⢸⣿⣿⣿⡿⢸⣿⣿⣿⣿⣿⣹⣿⣿⣿⡏⠀⣿⣿⣿⣿⣿⣿⠃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠹⣿⡇⣾⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢯⡿⢡⣿⢳⣿⣿⣿⣿⣿⣿⡿⣰⣿⢳⣿⣿⣿⣿⣿⣿⡿⠀⣾⡇⣿⡇⢠⣀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠐⠉⠉⠀⠀⠉⠉⠀⢻\n"
        "                   ⡏⣿⡇⣾⣿⣿⣿⡇⣼⣿⣿⣿⣿⢯⣿⣿⣿⣿⢡⣿⣿⣿⣿⣿⣿⡟⢀⠀⠂⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⡇⡿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢏⣾⢣⣿⣗⣾⣿⣿⣿⣿⣿⡟⣱⣿⢯⣿⣿⣿⣿⣿⣿⣿⢡⠂⣿⢰⣿⣿⣆⠻⣿⣦⠒⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘\n"
        "                   ⢹⣿⢃⣿⣿⣿⣿⡇⣿⣿⣿⣿⡏⣸⣿⣿⣿⡿⢸⣿⣿⣿⣿⣿⣿⣧⣿⣷⡀⠀⠀⠀⠀⠀⠀⣶⣦⢀⢠⣷⣧⣿⣿⣿⣿⣿⣿⣿⣿⣿⢏⣾⢣⣿⡟⢸⣿⣿⠿⠿⠿⠟⠘⠛⠟⠿⠿⣿⣿⣿⣿⣿⢃⣿⢸⡇⣾⣿⣿⣿⡗⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣆⠀⠀⠀⠀\n"
        "                   ⣾⣿⢸⣿⣿⣿⣿⡇⣿⣿⣿⣿⡇⣿⣿⣿⣿⡇⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⠀⠀⠀⠀⠀⠀⠈⠁⢸⣿⣿⢿⣿⣿⣿⣿⣿⣿⣿⣿⢏⡾⣣⣿⠟⠋⠉⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠉⠙⠃⠺⠇⡿⢰⣿⣿⣿⡏⠀⠀⠀⠀⠀⠀⢀⢀⣠⡀⣀⢒⡉⠀⣿⣿⠀⠀⠀⠀\n"
        "                   ⣿⡟⢸⣿⣿⣿⣿⡇⢿⣿⣿⣿⡧⡝⣿⣿⣿⡇⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠟⠀⠀⠀⠀⠀⠀⠀⠀⢸⣿⣿⣸⣿⣿⣿⣿⣿⣿⣿⢏⡾⣵⠟⠁⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⣀⡀⠀⠀⠀⠀⠀⠰⠁⢿⣿⣿⡿⠀⢿⡴⢚⣡⡞⠿⠺⡏⢸⡇⢸⣿⠁⡆⠸⣿⡇⠀⠀⠀\n"
        "                   ⣿⡇⣿⣿⣿⣿⣿⣧⢸⣿⣿⣿⡇⣿⡌⢿⣿⡇⣿⣿⣿⣿⣿⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢋⣿⣾⣿⣿⡿⠁⠀⡀⠀⠀⠀⠀⠀⠀⠀⢀⡀⠀⢿⣿⣶⣤⣀⠀⠀⠀⠀⠀⠙⠿⠁⠀⢋⣴⣿⢰⣶⢼⡶⢻⡼⢃⣾⡇⢸⣧⠠⠻⠷⠀⠀⠀\n"
        "                   ⣿⡇⣿⣿⣿⣿⣿⣿⠸⡿⠟⣻⣧⢻⣿⠀⡹⣿⣿⣿⣿⣿⣿⣿⣿⡆⠀⣠⣴⣤⣀⡀⠀⣀⠀⠀⣸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣦⡀⠀⠀⠀⠀⠀⠀⠀⠀⠻⡿⠂⢸⣿⣿⣿⣿⣷⠄⡀⠀⠀⠀⠀⠑⣾⣿⣿⢟⣕⢲⢇⣼⡈⠇⣿⡟⠀⠀⠀⠀⠀⠀⠀⠀⠀\n"
        "                   ⣿⡇⣿⣿⣿⣿⣿⣿⣷⣿⣿⣿⣿⡈⢿⡀⣿⣾⣿⣿⣿⣿⣿⣿⣿⣿⣆⠙⣿⣿⣿⣿⡇⣴⣄⣰⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢸⣿⣿⣿⣿⡟⣰⣿⣦⠐⠀⠀⠀⠘⣿⣿⢬⢋⡞⣨⢫⢷⣄⣿⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀\n"
        "                   ⣿⡇⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡕⣌⢧⢻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣶⣭⣿⣿⣧⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣸⣿⣿⣿⡿⣱⣿⣿⠃⣠⣾⣷⣶⣦⣽⣇⠿⡺⣱⣏⠺⢗⣿⠃⡤⢤⣤⡄⢶⣦⠰⣶⣄⠀\n"
        "                   ⣿⡇⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⠈⠈⡋⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⠈⠉⠉⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣿⣿⣿⡟⣱⣿⣿⠃⣴⣿⣿⣿⣿⣿⣿⣫⣾⣱⣿⣿⣯⣼⣧⢰⣧⢸⣿⣿⡄⠻⣷⡘⢿⡄\n"
        "                   ⣿⡇⢻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⢀⠀⢷⣮⣻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⠀⠀⠀⣀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣾⣿⣿⢟⣼⣿⠟⢡⣾⣿⣿⣿⣿⣿⡿⢃⢜⡱⣿⣿⣿⣷⠎⣠⣏⢻⡄⢿⣿⣷⡐⢌⡛⢮⡳\n"
        "                   ⣿⡇⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠈⢄⠈⢻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣄⠠⣾⣿⣿⣶⣶⣦⠐⣂⠀⠀⣠⣾⣿⣿⢯⣟⣫⢅⣴⣿⣿⣿⣿⣿⣿⠟⣱⠏⡹⣛⣿⣿⣿⡏⢠⣝⡋⣚⡻⡘⣿⣿⣷⡘⢿⣶⣤\n"
        "                   ⣿⣷⠘⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡄⠃⠠⠀⠙⠿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣾⣿⣿⣿⣿⣿⠞⣿⣧⣾⣿⣿⣿⣿⣿⠟⣡⣾⣿⣿⣿⣿⣿⡿⢋⣾⢫⣾⢵⣯⣿⣿⡟⢠⣿⠟⢞⡿⡃⣳⠘⣿⣿⣷⡈⢿⣿\n"
        "                   ⣿⣿⠀⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣧⠘⠀⠀⠃⢀⠈⠻⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢟⣡⣾⣿⣿⣿⣿⣿⡿⢋⣴⢟⣵⣿⣿⡖⣤⡿⡟⢀⣿⣿⣷⣾⣿⣜⠿⡣⣘⡻⣿⣿⣄⠙\n"
        "                   ⣿⣿⡆⠸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡆⢡⠀⠀⠀⠁⠀⠀⠉⠻⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡵⣿⣿⣿⣿⣿⣿⡿⢋⣴⠟⣱⣿⣿⣿⣿⣧⡟⡟⠀⠀⣿⣿⣿⣿⣏⣹⣿⣜⠿⣇⣩⣝⢿⣦\n"
        "                   ⣿⣿⣧⠀⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡈⠀⠀⠀⠀⠄⢀⣤⣶⣄⡈⠛⠿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢛⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡷⠂⣠⣴⡤⣩⡴⢛⣥⣾⣿⣿⣿⣿⣿⣏⡸⡿⢂⠀⣿⣿⣿⣿⣿⣿⣿⣿⣏⣡⣙⣋⢸⣶\n"
        "                   ⣿⣿⣿⡀⡘⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⡀⠀⢀⠂⣠⣿⣿⣿⣿⣿⣷⢠⡄⠉⠛⠿⣿⣿⣿⣿⣿⣭⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠋⣠⡾⠟⠵⣊⣥⣾⣿⣿⣿⣿⣿⣿⣿⢯⡟⠀⢴⣶⡄⠸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣭\n"
        "                   ⣿⣿⣿⣧⠘⣢⡙⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⡀⠈⢰⣿⣿⣿⡏⣿⣿⣿⢸⠁⠀⠀⠀⠀⠈⠙⠛⠿⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠟⠋⢀⣤⣥⣶⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⣳⠏⢀⠂⠈⢉⣬⡀⠙⢿⣿⠿⠿⠛⠛⠻⣿⣿⣿⣿⣿\n"
        "                   ⢻⣿⣿⣿⣆⠩⢧⠑⠨⣙⠻⢿⣿⣿⣿⣿⣿⣿⣷⡄⢿⣿⣿⣿⢸⣿⣿⣿⠘⠀⠀⠀⠀⠀⠀⠀⠀⣤⣤⣤⣄⣉⣉⡙⠛⠛⠛⠛⠿⠿⠿⠿⠿⠿⠟⠛⠛⠛⠋⠉⠉⠀⢀⣴⣿⡿⣫⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣟⣽⠏⠀⠀⠀⡀⠌⠛⢃⣁⠀⠀⠀⠀⠀⠀⠀⠈⢿⣿⣿⣿\n"
        "                   ⣌⠻⠿⣿⣿⣆⠩⣧⠀⠀⠁⠂⢬⠉⠛⠿⢿⣿⣿⣿⣎⠻⣿⡇⡾⠋⠙⢿⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⣿⣿⣿⣿⡿⠁⠀⠀⠀⠀⠀⠀⠀⠀⠐⣰⣿⣿⣿⢖⣴⣿⡿⣫⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣫⣾⠏⠀⠐⠂⠁⠀⠀⠀⠙⠟⢁⣀⠀⠀⠀⠀⠀⠀⠘⣿⣿⣿\n"
        "                   ⣿⣿⣷⣶⣭⣍⣃⠈⢷⡀⠄⣂⣴⣶⣦⣑⠲⢠⠈⣭⣍⣓⡙⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢹⣿⣿⣿⣿⣿⣿⠟⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣰⣿⡿⢋⣵⣿⢟⣵⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢟⣵⣿⠏⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⠟⢁⣤⡀⠀⠀⠀⠀⠈⣉⡛\n"
        "                   ⣿⣿⣿⣿⣿⣿⣿⣦⡀⠋⣾⣿⣿⣿⣿⠿⠃⣉⡀⣿⣿⣿⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢿⣿⣿⣿⠟⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣰⠿⣋⣴⢟⢏⣴⣿⡿⣫⣿⣿⣿⣿⣿⣿⣿⣿⡿⣫⣾⣿⠋⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠛⠃⢴⣶⠀⣠⣄⠉⣁\n"
        "                   ⣿⣿⣿⣿⣿⣿⣿⣿⡿⣂⣽⣿⣷⡍⣥⣚⡛⠿⠇⣿⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⢿⠟⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢘⡥⢞⣫⢔⣵⣿⢟⣭⣾⣿⣿⣿⣿⣿⣿⣿⣿⢋⣾⣿⡿⢃⣶⣦⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠁⠀⠙⠋⠀⠻\n"
        "                   ⠻⣿⣿⣿⣿⣿⣿⣿⢸⣿⣿⣿⣿⠀⣿⣿⣿⣿⣶⣍⡛⠿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠂⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⡾⢽⡾⣋⣴⠿⣫⣵⣿⣿⣿⣿⣿⣿⣿⣿⣿⢟⣵⣿⣿⡿⠡⢿⣿⣿⣧⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀\n"
        "                   ⢷⣬⡛⢿⣿⣿⣿⣿⡎⢿⣿⣿⣿⡄⣿⣿⣿⣿⣿⣿⣿⣷⣦⡀⢀⡴⠂⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⡤⢞⣫⣷⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢟⣵⣿⣿⣿⡟⣱⣿⣷⡝⣿⡿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀\n"
        "                   ⠀⠙⠻⢶⣬⡙⠛⠉⠀⠀⠈⠀⠀⠀⢿⣿⣿⣿⣿⣿⣿⣿⢏⣴⠏⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠑⠦⣄⡀⢠⣾⣷⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⢛⣵⣿⣿⣿⣿⠟⣰⣿⣿⣿⣷⣆⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀\n"

        "\n\n\n"

        "                                           ██╗           ██████╗      ██╗   ██╗      ██████╗      ███╗   ███╗      ██████╗ \n"
        "                                           ██║          ██╔═══██╗     ██║   ██║     ██╔═══██╗     ████╗ ████║     ██╔═══██╗\n"
        "                                           ██║          ██║   ██║     ██║   ██║     ██║   ██║     ██╔████╔██║     ██║   ██║\n"
        "                                           ██║          ██║   ██║     ╚██╗ ██╔╝     ██║   ██║     ██║╚██╔╝██║     ██║   ██║\n"
        "                                           ███████╗     ╚██████╔╝      ╚████╔╝      ╚██████╔╝     ██║ ╚═╝ ██║     ╚██████╔╝\n"
        "                                           ╚══════╝      ╚═════╝        ╚═══╝        ╚═════╝      ╚═╝     ╚═╝      ╚═════╝ \n"


        "\n\n                                                       启动成功啦！                                                              "
    )
    print("=" * 180)

    """
    aSBsb3ZlIG11cmFzYW1l
    """

    app_context.global_config = ConfigLoader()
    apply_log_max_size(app_context.global_config)
    app_context.global_emotion_manager = EmotionManager(app_context.global_config)
    set_mimics_provider(get_role_mimics)
    if not app_context.global_emotion_manager.emotions:
        print("\n[警告] 未找到任何情绪配置（请检查 ref_audio_root 目录），将降级为纯文本模式。")

    app_context.memory_manager = MemoryManager(app_context.global_config)
    mood_mgr = MoodManager(app_context.memory_manager.data_path)
    app_context.mood_mgr = mood_mgr
    affection_mgr = AffectionManager(app_context.global_config, app_context.memory_manager.data_path)
    app_context.affection_mgr = affection_mgr
    promise_mgr = PromiseManager(app_context.memory_manager.data_path)
    app_context.promise_mgr = promise_mgr
    encounter_mgr = EncounterManager(app_context.global_config, app_context.memory_manager.data_path)
    app_context.encounter_mgr = encounter_mgr
    try:
        from modules.llm_helpers import check_llm_service
        llm_ok, llm_detail = await check_llm_service(get_active_ctx())
        if llm_ok:
            print(f"LLM 服务连接正常：{llm_detail}")
        else:
            print(f"[警告] LLM 服务不可达：{llm_detail}\n"
                  "回复生成 / 回复审判 / RAG 嵌入 / 文本生成等功能将失败。"
                  "请确认服务已启动，并检查 llm_backend、llm_base_url、"
                  "llm_model_name 配置是否与你的服务匹配。")
    except Exception as e:
        print(f"LLM 服务探测异常: {type(e).__name__}: {e}")

    preload_configs = []
    tts_first = (bool(app_context.global_config.get("auto_start_tts", False))
                 and bool(app_context.global_config.get("llm_auto_load_local", True))
                 and machine_capacity.tts_first_recommended())
    try:
        preload_configs = [app_context.global_config]
        for conn in connections_of(app_context.global_config):
            if not conn.get("enabled", True):
                continue
            cfg = config_for_connection(str(conn.get("id") or ""))
            if cfg is not None and cfg not in preload_configs:
                preload_configs.append(cfg)
        if tts_first:
            print(f"[启动顺序] {machine_capacity.summary()}：内存/显存偏紧，先启动 TTS，就绪后再加载本地模型")
        else:
            auto_load_local_models(preload_configs)
    except Exception as e:
        print(f"本地模型预加载异常: {type(e).__name__}: {e}")

    # 初始化功能模块
    db = DatabaseManager(app_context.memory_manager.data_path)
    app_context.db = db
    stats_mgr = StatsManager(db)
    app_context.stats_mgr = stats_mgr
    sticker_mgr = StickerManager(app_context.global_config)
    app_context.sticker_mgr = sticker_mgr
    tool_registry = ToolRegistry(app_context.global_config, app_context.memory_manager.data_path)
    app_context.tool_registry = tool_registry
    profile_mgr = UserProfileManager(app_context.global_config, app_context.memory_manager.data_path)
    app_context.profile_mgr = profile_mgr
    rag_mgr = RAGManager(app_context.global_config, app_context.memory_manager.data_path)
    app_context.rag_mgr = rag_mgr
    lexicon_mgr = LexiconManager(app_context.global_config, app_context.memory_manager.data_path)
    app_context.lexicon_mgr = lexicon_mgr
    recall_mgr = HistoryRecallManager(app_context.global_config, app_context.memory_manager.data_path,
                                      embedder=rag_mgr)
    app_context.recall_mgr = recall_mgr
    sender = MessageSender(app_context.global_config, app_context.memory_manager, app_context.sticker_mgr, stats_mgr)
    app_context.sender = sender
    todo_mgr = TodoManager(app_context.global_config, db, scheduler, sender,
                           emotions_provider=get_active_emotions)
    app_context.todo_mgr = todo_mgr
    todo_mgr.ctx_provider = get_active_ctx
    todo_mgr.restore_pending()
    job_mgr = ScheduledJobManager(app_context.global_config, app_context.memory_manager.data_path, scheduler,
                                  sender, get_active_ctx, get_active_emotions,
                                  list_known_sessions, session_history_block)
    app_context.job_mgr = job_mgr
    event_mgr = EventManager(app_context.global_config, app_context.memory_manager.data_path, profile_mgr)
    app_context.event_mgr = event_mgr

    # 启动调度器与功能任务
    load_proactive_state()
    seed_proactive_sessions()
    scheduler.start()
    register_feature_jobs()
    _spawn(greeting_catchup_task())
    _spawn(companion_catchup_task())
    _spawn(mood_diary_catchup_task())
    job_mgr.register_all()

    if app_context.global_config.get("auto_start_tts", False):
        if tts_first and preload_configs:
            def _tts_then_models():
                auto_start_and_switch_tts(app_context.global_config)
                auto_load_local_models(preload_configs)
            threading.Thread(target=_tts_then_models, daemon=True).start()
        else:
            threading.Thread(target=auto_start_and_switch_tts,
                             args=(app_context.global_config,), daemon=True).start()

    def _run_on_main_loop(coro_factory, timeout: float = 10.0) -> bool:
        async def _do():
            try:
                return bool(await coro_factory())
            except Exception as e:
                print(f"[插件] 异步操作失败: {type(e).__name__}: {e}")
                return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            try:
                loop.create_task(_do())
                return True
            except Exception:
                return False
        main_loop = app_context.MAIN_EVENT_LOOP
        if main_loop is not None and not main_loop.is_closed():
            try:
                fut = asyncio.run_coroutine_threadsafe(_do(), main_loop)
                return bool(fut.result(timeout=timeout))
            except Exception as e:
                print(f"[插件] 异步操作超时/失败: {type(e).__name__}: {e}")
                return False
        print("[插件] 事件循环尚未就绪，操作未执行")
        return False

    def _as_plugin_target(value) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        match = re.fullmatch(r"(?:private|group)_(.+)", text)
        return match.group(1) if match else text

    async def _plugin_send_with_session(session_id: str, action):
        with sender.for_session(session_id):
            return bool(await action())

    def plugin_send_text(session_type, target_id, text: str) -> bool:
        tid = _as_plugin_target(target_id)
        if not tid:
            print(f"[插件] 发送目标为空或非法：{target_id!r}")
            return False
        kind = "private" if str(session_type) == "private" else "group"
        return _run_on_main_loop(
            lambda: _plugin_send_with_session(
                f"{kind}_{tid}", lambda: sender.send_text(kind, tid, str(text))))

    def plugin_send_voice(session_type, target_id, text: str,
                          emotion: str = "") -> bool:
        tid = _as_plugin_target(target_id)
        if not tid:
            print(f"[插件] 发送目标为空或非法：{target_id!r}")
            return False
        kind = "private" if str(session_type) == "private" else "group"
        emotions = get_active_emotions()
        data_path = app_context.memory_manager.data_path

        async def _do():
            if not client_supports(sender._active_client() or sender.client, "voice"):
                return False
            if not await ensure_tts_service(app_context.global_config):
                return False
            wav = await synthesize_sentence(app_context.global_config, str(text),
                                            str(emotion or ""), emotions,
                                            data_path, stats=stats_mgr)
            if not wav:
                return False
            try:
                ok = await sender.send_voice(kind, tid, wav)
            finally:
                try:
                    Path(wav).unlink(missing_ok=True)
                except Exception:
                    pass
            return ok

        async def _send():
            with sender.for_session(f"{kind}_{tid}"):
                return await _do()

        return _run_on_main_loop(_send, timeout=120.0)

    def plugin_send_message(group_id, text: str) -> bool:
        return plugin_send_text("group", group_id, text)

    webui_server = None
    if HAS_AIOHTTP and app_context.global_config.get("webui_enabled", True):
        webui_server = WebUIServer(app_context.global_config, app_context.memory_manager)
        _WEBUI_SERVER_HOLDER["server"] = webui_server
        if app_context.global_config.get("plugins_enabled", True):
            try:
                from modules.plugin_runtime import PluginRuntime
                _plugin_rt = PluginRuntime(
                    webui_server.plugin_manager,
                    logger=_plugin_log,
                    config_getter=lambda: (app_context.global_config.config or {}),
                    sender=plugin_send_text,
                    voice_sender=plugin_send_voice,
                    emotions_getter=get_active_emotions)
                webui_server.attach_plugin_runtime(_plugin_rt)
                _plugin_errors = _plugin_rt.load_all()
                for _pid, _err in (_plugin_errors or {}).items():
                    print(f"[插件] {_pid} 加载失败: {_err}")
            except Exception as e:
                print(f"[插件] 运行时初始化失败（插件功能不可用，主程序继续）：{e}")
        else:
            print("[插件] 插件系统已关闭（config: plugins_enabled=false）")
        try:
            await webui_server.start()
        except Exception as e:
            print(f"WebUI 启动异常，继续运行其他功能：{e}")


    profiles = build_connection_profiles(app_context.global_config)
    if profiles:
        print("正在连接：" + "；".join(
            f"{p['label']}→{connection_target_label(p)}" for p in profiles))
    else:
        print("还没有启用的接入方式，去左侧栏「接入方式」里加一条吧。")

    active_clients = []

    async def _stop_watcher():
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    for c in list(active_clients):
                        closer = getattr(c, "close", None)
                        if closer is None:
                            continue
                        try:
                            result = closer()
                            if hasattr(result, "__await__"):
                                await result
                        except Exception:
                            pass
                    active_clients.clear()
                    return
                await asyncio.sleep(0.5)
        except Exception:
            pass

    def _consume_task_result(task):
        try:
            err = task.exception()
        except (asyncio.CancelledError, Exception):
            return
        if err is None:
            return
        print(f"[消息处理异常] {type(err).__name__}: {err}")
        traceback.print_exception(type(err), err, err.__traceback__)

    _cursor_written = {}

    def _persist_cursor(profile: dict, client):
        snap = client_snapshot(client)
        cursor = str(snap.get("cursor") or "")
        contexts = snap.get("context_tokens") or {}
        conn_id = str(profile.get("key") or "")
        if not cursor or not conn_id or _cursor_written.get(conn_id) == cursor:
            return
        _cursor_written[conn_id] = cursor
        data = app_context.global_config.config
        for conn in (data.get("connections") or []):
            if str(conn.get("id")) == conn_id:
                conn["cursor"] = cursor
                if contexts:
                    conn["context_tokens"] = dict(list(contexts.items())[-200:])
                break
        else:
            return
        try:
            app_context.global_config._atomic_save(data)
        except Exception as e:
            print(f"保存收消息游标失败: {type(e).__name__}: {e}")

    async def _run_profile(profile: dict):
        label = profile["label"]
        role_key = profile["role_key"]
        platform = str(profile.get("platform") or "napcat")
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            if platform == "napcat":
                client = NapCatClient(ws_url=profile["ws_url"], token=profile["token"])
            else:
                conn = profile.get("connection") or {}
                missing = ("还没扫码登录，去接入方式里点「刷新二维码」扫码"
                           if platform == "wechat_clawbot" and not conn.get("bot_token")
                           else "还没填 AppID / AppSecret" if platform == "qq_official"
                           and not (conn.get("app_id") and conn.get("app_secret")) else "")
                if missing:
                    print(f"{label}：{missing}，本条跳过。")
                    return
                client = build_client(platform, conn)
                if client is None:
                    print(f"{label}：暂不支持的接入方式（{platform}），本条跳过。")
                    return
            active_clients.append(client)
            channel_key = str((profile.get("connection") or {}).get("id")
                              or f"{platform}:{label}")
            try:
                async with client:
                    sender.set_channel_client(channel_key, client)
                    if role_key:
                        sender.set_role_client(role_key, client)
                        ROLE_CONNECTIONS[id(client)] = role_key
                    if profile.get("default"):
                        sender.client = client
                    print(f"已连接！{label}（{client.self_id}）")
                    print("等待消息中...")
                    async for event in client:
                        if stop_event is not None and stop_event.is_set():
                            return
                        task = asyncio.create_task(handle_message_event(event, client))
                        task.add_done_callback(_consume_task_result)
                        _persist_cursor(profile, client)
                if stop_event is None or not stop_event.is_set():
                    print(f"{label} 连接已断开，3秒后重连...")
                    await asyncio.sleep(3)
            except ILinkSessionExpired as e:
                print(f"{label}：{e}")
                return
            except Exception as e:
                print(f"{label} 连接失败: {e}")
                print("10秒后尝试重新连接...")
                await asyncio.sleep(10)
            finally:
                sender.set_channel_client(channel_key, None)
                if role_key:
                    sender.set_role_client(role_key, None)
                    ROLE_CONNECTIONS.pop(id(client), None)
                if sender.client is client:
                    sender.client = None  # 避免主动消息/定时任务使用失效连接
                try:
                    active_clients.remove(client)
                except ValueError:
                    pass

    _CONN_VOLATILE_KEYS = ("cursor", "context_tokens")

    def _profile_signature(profiles):
        def strip_one(prof):
            prof = dict(prof)
            conn = {k: v for k, v in (prof.get("connection") or {}).items()
                    if k not in _CONN_VOLATILE_KEYS}
            prof["connection"] = conn
            return prof

        if isinstance(profiles, dict):
            profiles = [profiles]
        return json.dumps([strip_one(p) for p in profiles],
                          sort_keys=True, ensure_ascii=False, default=str)

    async def _connections_supervisor():
        running = {}
        last_sig = None
        while not (stop_event is not None and stop_event.is_set()):
            try:
                current = build_connection_profiles(app_context.global_config)
            except Exception as e:
                print(f"接入方式清单构建失败（沿用现状）: {type(e).__name__}: {e}")
                current = []
            sig = _profile_signature(current)
            if sig != last_sig:
                wanted = {str(p["key"]): p for p in current}
                stopped = [key for key in list(running)
                           if key not in wanted
                           or _profile_signature(running[key][1])
                           != _profile_signature(wanted[key])]
                for key in stopped:
                    running.pop(key)[0].cancel()
                started = []
                for key, prof in wanted.items():
                    if key not in running:
                        t = asyncio.create_task(_run_profile(prof))
                        t.add_done_callback(_consume_task_result)
                        running[key] = (t, prof)
                        started.append(prof["label"])
                if last_sig is not None and (started or stopped):
                    print(f"接入方式配置已变化：新增 {len(started)} 条"
                          f"（{'、'.join(started) or '无'}），停止 {len(stopped)} 条，"
                          f"当前启用 {len(wanted)} 条。")
                last_sig = sig
            await asyncio.sleep(2)
        for _key, (task, _prof) in running.items():
            task.cancel()

    watcher = asyncio.create_task(_stop_watcher())
    try:
        await _connections_supervisor()
    except asyncio.CancelledError:
        pass
    finally:
        watcher.cancel()
    print("正在关闭所有子进程...")
    save_proactive_state()
    if webui_server:
        try:
            await webui_server.shutdown()
        except Exception as e:
            print(f"关闭 WebUI 失败（忽略）：{e}")
        _WEBUI_SERVER_HOLDER["server"] = None
    try:
        await scheduler.stop()
    except Exception as e:
        print(f"停止调度器失败（忽略）：{e}")
    process_manager.shutdown_all(budget=3.0)
    if db is not None:
        db.close()


if __name__ == "__main__":
    import threading
    import time

    _ensure_dpi_aware()

    stop_event = threading.Event()

    run_exe = os.environ.pop("LOVOMO_RUN_EXE", "")
    wait_pid = os.environ.pop("LOVOMO_WAIT_PID", "")
    if wait_pid:
        try:
            target_pid = int(wait_pid)
            print(f"等待旧进程({target_pid})退出后启动…")
            deadline = time.time() + 20.0
            while _pid_alive(target_pid) and time.time() < deadline:
                time.sleep(0.3)
            if _pid_alive(target_pid):
                print(f"旧进程({target_pid}) 20 秒仍未退出，放弃本次"
                      f"{'安装' if run_exe else '重启'}（不会启动第二个实例）")
                sys.exit(0)
        except Exception:
            pass
    if run_exe:
        try:
            import subprocess
            subprocess.Popen([run_exe], close_fds=True)
            print(f"[更新] 已拉起安装包：{run_exe}")
        except Exception as e:
            print(f"[更新] 拉起安装包失败：{type(e).__name__}: {e}")
        sys.exit(0)

    acquired = _acquire_single_instance()
    if not acquired and wait_pid:
        deadline = time.time() + 5.0
        while time.time() < deadline:
            time.sleep(0.25)
            acquired = _acquire_single_instance()
            if acquired:
                break
    if not acquired:
        if _signal_existing_instance():
            print("Lovomo 已在运行，已把它的窗口唤到前台。")
        else:
            print("Lovomo 已在运行，未重复启动。")
        sys.exit(0)

    try:
        cleanup_old_installers()
    except Exception as e:
        print(f"[更新] 清理旧安装包失败：{type(e).__name__}: {e}")

    config = ConfigLoader()

    def run_backend():
        try:
            app_context.MAIN_EVENT_LOOP = asyncio.new_event_loop()
            asyncio.set_event_loop(app_context.MAIN_EVENT_LOOP)
            app_context.MAIN_EVENT_LOOP.run_until_complete(main(stop_event))
        except Exception as e:
            print(f"后台服务异常: {e}")
            import traceback
            traceback.print_exc()
            try:
                with open(runtime_path("backend_error.log"), "a", encoding="utf-8") as f:
                    f.write(f"{time.ctime()} - 异常: {e}\n")
                    traceback.print_exc(file=f)
            except Exception:
                pass

    backend_thread = threading.Thread(target=run_backend, daemon=True)
    backend_thread.start()
    time.sleep(1)
    webui_port = int(config.get("webui_port", 11500))

    def window_url() -> str:
        server = _WEBUI_SERVER_HOLDER.get("server")
        if server is not None:
            try:
                url = str(server.desktop_url() or "")
                if url:
                    return url
            except Exception:
                pass
        return f"http://127.0.0.1:{webui_port}"

    def _wait_webui_ready(timeout: float = 20.0) -> None:
        if not config.get("webui_enabled", True):
            return
        deadline = time.time() + max(0.0, float(timeout))
        server = None
        while time.time() < deadline:
            server = _WEBUI_SERVER_HOLDER.get("server")
            if server is not None:
                break
            time.sleep(0.05)
        if server is None:
            print("等 WebUI 服务对象超时：窗口先按设置里的地址打开")
            return
        event = getattr(server, "ready", None)
        if event is not None and not event.wait(max(0.0, deadline - time.time())):
            print(f"等 WebUI 就绪超时（{timeout:.0f}s）：窗口先按设置里的地址打开")

    holder = {"window": None}
    close_state = {"hidden": False, "quitting": False, "quitting_at": 0.0}
    tray_state = {"icon": None}
    relaunch_pending = {"flag": False}
    _geometry_paused = [0]

    def _wake_geometry_guard(seconds: float = 1.5):
        _geometry_paused[0] += 1

        def _release():
            time.sleep(max(0.0, seconds))
            _geometry_paused[0] = max(0, _geometry_paused[0] - 1)

        threading.Thread(target=_release, daemon=True).start()

    def _release_webview_now() -> None:
        with modules.webview_runtime._WEBVIEW_LOCK:
            if not close_state.get("hidden"):
                return
            window = holder["window"]
            if window is None:
                return
            if _window_visible(window):
                log_window_event("窗口仍在屏幕上，跳过释放（避免白屏）")
                return
            if not _webview2_release(window):
                log_window_event("释放界面进程失败：保持原样，下次打开时再重建")
                return

    def _ensure_webview_alive() -> None:
        window = holder["window"]
        if window is None:
            return
        need = (bool(modules.webview_runtime._WEBVIEW_PROCESS_DEAD["flag"])
                or not webview2_control_alive(window))
        if not need:
            return
        with modules.webview_runtime._WEBVIEW_LOCK:
            need = (bool(modules.webview_runtime._WEBVIEW_PROCESS_DEAD["flag"])
                    or not webview2_control_alive(window))
            if not need:
                return
            if _webview2_rebuild(window, window_url(),
                                 _webview_profile_dir()):
                modules.webview_runtime._WEBVIEW_PROCESS_DEAD["flag"] = False
            else:
                log_window_event("打开窗口时界面没重建起来：先隐藏窗口，再重启一次程序")
                try:
                    window.hide()
                except Exception:
                    pass
                hook = _APP_HOOKS.get("relaunch")
                if hook is not None:
                    hook()
                else:
                    log_window_event("没有可用的重启入口，请手动退出后重开")

    def open_console():
        w = holder["window"]
        if w is None:
            return
        close_state["hidden"] = False
        _ensure_webview_alive()
        _wake_geometry_guard()
        _raise_to_foreground(w)
        try:
            _apply_saved_geometry(w)
        except Exception:
            try:
                w.restore()
            except Exception:
                pass
        _raise_to_foreground(w)
        def _retry(win=w):
            try:
                time.sleep(0.12)
                if close_state.get("hidden"):
                    return
                _raise_to_foreground(win)
            except Exception:
                pass
        threading.Thread(target=_retry, daemon=True).start()

    def stop_tray_icon():
        icon = tray_state.get("icon")
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass

    def request_quit():
        close_state["quitting"] = True
        close_state["quitting_at"] = time.time()
        quit_app()

    def _force_quit(delay: float = 1.2):
        def _run():
            time.sleep(max(0.0, delay))
            log_window_event("进程正在退出（硬退出兜底到点）")
            try:
                dump_log_buffer(user_data_dir() / "last_session.log")
            except Exception:
                pass
            try:
                mark_exiting()
                process_manager.shutdown_all(budget=1.0)
            except Exception:
                pass
            os._exit(0)
        threading.Thread(target=_run, daemon=True).start()

    def _spawn_detached(args, env):
        import subprocess
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = (subprocess.CREATE_NO_WINDOW
                                       | getattr(subprocess, "DETACHED_PROCESS", 0))
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0  # SW_HIDE
            kwargs["startupinfo"] = si
        subprocess.Popen(args, env=env, close_fds=True, **kwargs)

    def _resolve_spawn_exe() -> tuple:
        if getattr(sys, "frozen", False):
            return sys.executable, []
        exe = sys.executable
        if os.name == "nt":
            for name in ("pythonw.exe", "pythonw"):
                cand = Path(exe).with_name(name)
                if cand.exists():
                    exe = str(cand)
                    break
        args = [exe, str(Path(sys.argv[0]).resolve())]
        return args[0], args[1:]

    def relaunch_console():
        if relaunch_pending["flag"]:
            log_window_event("重启已在进行，忽略重复的重启请求")
            return
        relaunch_pending["flag"] = True
        close_state["quitting"] = True
        close_state["quitting_at"] = time.time()
        threading.Thread(target=_do_relaunch, daemon=True).start()

    def _do_relaunch():
        stop_tray_icon()
        w = holder["window"]
        if w is not None:
            _capture_geometry(w)
            _finalize_geometry(w)
            _finish_geometry_save()
        try:
            exe, extra = _resolve_spawn_exe()
            env = dict(os.environ)
            env["LOVOMO_WAIT_PID"] = str(os.getpid())
            _spawn_detached([exe, *extra], env)
            log_window_event("已安排重启：新实例等待本进程退出后启动")
        except Exception as e:
            log_window_event(f"重启失败，继续使用当前进程：{type(e).__name__}: {e}")
            relaunch_pending["flag"] = False
            close_state["quitting"] = False
            close_state["quitting_at"] = 0.0
            if w is not None and not webview2_control_alive(w):
                _webview2_rebuild(w, window_url(),
                                  _webview_profile_dir())
            if w is not None:
                try:
                    w.show()
                except Exception:
                    pass
            return
        stop_event.set()
        _force_quit(1.5)
        if w is not None:
            try:
                _webview2_release(w)
            except Exception:
                pass
            try:
                w.destroy()
            except Exception:
                pass

    def quit_app():
        close_state["quitting"] = True
        close_state["quitting_at"] = time.time()
        stop_event.set()
        _force_quit(1.2)
        stop_tray_icon()
        try:
            _uninstall_taskbar_activate_hook()
        except Exception:
            pass
        w = holder["window"]
        if w is not None:
            _capture_geometry(w)
            _finalize_geometry(w)
            _finish_geometry_save()
            try:
                _webview2_release(w)
            except Exception:
                pass
            try:
                w.destroy()
            except Exception:
                pass

    def install_update_package(installer_path: str) -> str:
        path = Path(str(installer_path or ""))
        if not path.is_file():
            return "安装包不存在或已被删除"
        if relaunch_pending["flag"]:
            print("安装已在进行，跳过重复的安装请求。")
            return ""
        relaunch_pending["flag"] = True
        try:
            exe, extra = _resolve_spawn_exe()
            env = dict(os.environ)
            env["LOVOMO_WAIT_PID"] = str(os.getpid())
            env["LOVOMO_RUN_EXE"] = str(path)
            _spawn_detached([exe, *extra], env)
        except Exception as e:
            return f"启动安装程序失败：{type(e).__name__}: {e}"
        print(f"[更新] 已安排安装：{path.name}（本程序退出后由助手拉起安装包）")
        request_quit()
        return ""

    _APP_HOOKS["install_update"] = install_update_package
    _APP_HOOKS["relaunch"] = relaunch_console

    def try_start_tray() -> bool:
        if tray_state.get("icon") is not None:
            return True
        try:
            import pystray
            from PIL import Image
            icon_file = next((p for p in _candidate_icon_paths()), None)
            if icon_file is not None:
                img = Image.open(str(icon_file)).convert("RGBA").resize((64, 64))
            else:
                img = Image.new("RGBA", (64, 64), (30, 136, 229, 255))
                try:
                    from PIL import ImageDraw
                    ImageDraw.Draw(img).text((8, 22), "Lovomo", fill="white")
                except Exception:
                    pass

            icon = pystray.Icon(
                "lovomo", img, "Lovomo",
                menu=pystray.Menu(
                    pystray.MenuItem("打开 Lovomo", lambda i, item: open_console(),
                                     default=True),
                    pystray.MenuItem("重启 Lovomo", lambda i, item: relaunch_console()),
                    pystray.MenuItem("退出 Lovomo", lambda i, item: request_quit()),
                ),
            )
            tray_state["icon"] = icon
            threading.Thread(target=icon.run, daemon=True).start()
            return True
        except Exception as e:
            print(f"系统托盘不可用（如需托盘请安装依赖后重启：pip install pystray Pillow）：{e}")
            return False

    tray_ok = try_start_tray()

    def _hide_to_tray(w) -> None:
        if not tray_ok:
            close_state["hidden"] = False
        else:
            close_state["hidden"] = True

        def _do():
            if tray_ok:
                for _ in range(5):
                    try:
                        _window_hide(w)
                    except Exception:
                        pass
                    time.sleep(0.1)
                    if not _window_visible(w):
                        break
                if _window_visible(w):
                    log_window_event("窗口没能藏住（仍在屏幕上）：后续不会释放界面进程")
            else:
                try:
                    w.minimize()
                except Exception:
                    pass
            try:
                _finish_geometry_save()
            except Exception:
                pass
            _release_webview_now()

        threading.Thread(target=_do, daemon=True).start()

    def on_closing():
        if close_state.get("quitting") and time.time() - close_state.get("quitting_at", 0) < 15:
            return None
        if close_state.get("quitting"):
            log_window_event("上一次退出流程没有完成，已重置退出标记（本次仍收进托盘）")
            close_state["quitting"] = False
            close_state["quitting_at"] = 0.0
        w = holder["window"]
        if w is not None:
            _capture_geometry(w)
            _finalize_geometry(w)
            _hide_to_tray(w)
        return False

    def _finalize_geometry(w) -> None:
        if w is None:
            return
        try:
            cmd = _show_state(_window_hwnd(w))
            if cmd == 3:
                _WINDOW_GEOMETRY["maximized"] = True
            elif cmd == 1:
                rect = _window_rect(w)
                if rect and not _rect_is_degenerate(rect):
                    _WINDOW_GEOMETRY["maximized"] = False
                    _WINDOW_GEOMETRY["normal"] = rect
        except Exception:
            pass

    def _capture_geometry(w, force: bool = False) -> None:
        if w is None:
            return
        if not _window_visible(w):
            return
        try:
            state = _window_state_name(w)
            if not state:
                return _capture_geometry_fallback(w)
            if state == "maximized":
                _WINDOW_GEOMETRY["maximized"] = True
            elif state == "minimized":
                pass
            else:
                rect = _window_rect(w)
                if rect and not _rect_is_degenerate(rect):
                    _WINDOW_GEOMETRY["maximized"] = False
                    _WINDOW_GEOMETRY["normal"] = rect
        except Exception:
            return
        _schedule_geometry_save(0.4 if force else 1.2)

    def _capture_geometry_fallback(w) -> None:
        try:
            scale = _window_scale(w)
            x = getattr(w, "x", None)
            y = getattr(w, "y", None)
            width = getattr(w, "width", None)
            height = getattr(w, "height", None)
            if not width or not height:
                return
            rect = {
                "x": _logical_to_phys(x, scale) if x is not None else None,
                "y": _logical_to_phys(y, scale) if y is not None else None,
                "width": _logical_to_phys(width, scale),
                "height": _logical_to_phys(height, scale),
            }
            if _rect_is_degenerate(rect):
                return
            _WINDOW_GEOMETRY["normal"] = rect
        except Exception:
            return
        _schedule_geometry_save(1.2)

    def _bind_geometry_events(w) -> None:
        started = time.time()
        startup_grace = 5.0

        def _later(*_args, **_kwargs):
            time.sleep(0.2)
            if time.time() - started < startup_grace:
                return
            if _geometry_paused[0] > 0:
                return
            _capture_geometry(w)

        for evt_name in ("resized", "moved", "maximized", "restored", "shown"):
            try:
                getattr(w.events, evt_name).__iadd__(_later)
            except Exception:
                pass

    def run_webview_loop():
        import webview
        freed = purge_webview_cache()
        if freed:
            print(f"[窗口] 界面已更新，已清理 WebView2 缓存（{freed / 1048576:.1f} MB）")
        geom = _load_window_geometry()
        normal = _sanitize_normal_geometry(geom.get("normal") or {})
        scale = _window_scale()
        to_logical = _phys_to_logical
        _fb = _default_normal_geometry()
        start_w = to_logical(int(normal.get("width") or _fb["width"]), scale)
        start_h = to_logical(int(normal.get("height") or _fb["height"]), scale)
        start_x = normal.get("x")
        start_y = normal.get("y")

        sw, sh = _primary_screen_size()
        if sw > 0 and sh > 0:
            max_w = to_logical(int(sw * 0.92), scale)
            max_h = to_logical(int(sh * 0.92), scale)
            start_w = max(400, min(start_w, max_w))
            start_h = max(300, min(start_h, max_h))
        else:
            start_w = max(400, start_w)
            start_h = max(300, start_h)

        kwargs = {
            "width": start_w, "height": start_h,
            "resizable": True, "maximized": bool(geom.get("maximized")),
        }
        if (not geom.get("maximized") and start_x is not None
                and start_y is not None and _is_geometry_valid(normal)):
            kwargs["x"] = to_logical(int(start_x), scale)
            kwargs["y"] = to_logical(int(start_y), scale)
        _wait_webui_ready()
        holder["window"] = webview.create_window(
            'Lovomo',
            window_url(),
            **kwargs)
        w = holder["window"]
        _WEBVIEW_WINDOW_HOLDER["window"] = w
        try:
            w.events.closing += on_closing
        except Exception as e:
            print(f"绑定窗口关闭事件失败，关闭将直接退出: {e}")
        _bind_geometry_events(w)
        try:
            w.events.loaded += lambda: attach_process_failed_watch(w)
        except Exception:
            pass

        def _wake_from_system():
            if close_state.get("hidden"):
                return
            open_console()

        def _install_foreground_hook(*_args, **_kwargs):
            def _do():
                time.sleep(0.3)
                _install_taskbar_activate_hook(
                    w, on_activate=lambda win: _wake_from_system())
            threading.Thread(target=_do, daemon=True).start()

        try:
            w.events.shown += _install_foreground_hook
        except Exception as e:
            print(f"绑定窗口显示事件失败（任务栏唤醒钩子未安装）: {e}")
        _start_instance_show_waiter(open_console)
        #
        # 此时 GetWindowRect 拿到的是 MinimumSize(200x100)，_apply_saved_geometry

        #
        webview.start(private_mode=False,
                      storage_path=_webview_profile_dir() or None)
        icon = tray_state.get("icon")
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass

    try:
        if HAS_WEBVIEW:
            run_webview_loop()
        else:
            print("程序正在运行，按 Ctrl+C 退出...")
            while not stop_event.is_set():
                time.sleep(1)
    except KeyboardInterrupt:
        print("\n收到键盘中断，准备退出...")
    finally:
        print("正在停止后台服务...")
        stop_event.set()
        mark_exiting()
        backend_thread.join(timeout=1.5)
        process_manager.shutdown_all(budget=1.5)
        print("程序已完全退出。")
