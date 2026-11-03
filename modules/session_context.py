# -*- coding: utf-8 -*-
import re
import time
from typing import List, Optional

from modules import app_context
from modules.memory_store import MemoryManager, _MEMORY_FILE_RE, restore_session_id
from modules.llm_helpers import RoleContext
from modules.emotion_voices import EmotionManager
from modules.config_loader import CONNECTION_PLATFORMS, CONNECTION_PLATFORM_NAMES
from modules.config_presets import DEFAULT_PROFILE, profile_loader
from modules.model_library import PROVIDER_KINDS


def list_known_sessions() -> list:
    sessions = []
    if app_context.memory_manager is None:
        return sessions
    for f in sorted(app_context.memory_manager.data_path.glob("*.json")):
        m = _MEMORY_FILE_RE.match(f.name)
        if not m:
            continue
        stype = m.group(1)
        rest = f.name.rsplit(".json", 1)[0].split(f"{stype}_", 1)[-1]
        if stype == "group":
            rest = rest.split("_")[0]
        rest = restore_session_id(rest)
        if not rest:
            continue
        item = {"session_type": stype, "session_id": rest}
        if not any(s["session_id"] == rest and s["session_type"] == stype
                   for s in sessions):
            sessions.append(item)
    return sessions


def _log_hide_patterns() -> list:
    config = app_context.active_config()
    if config is None:
        return []
    raw = str(config.get("webui_log_hide_patterns", "") or "")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _log_hidden(line: str, patterns: Optional[list] = None) -> bool:
    for pattern in (patterns if patterns is not None else _log_hide_patterns()):
        if pattern in line:
            return True
    return False


def whitelist_ids() -> set:
    config = app_context.active_config()
    raw = str(config.get("whitelist_ids", "") or "") if config else ""
    return {item.strip() for item in re.split(r"[\s,，;；]+", raw) if item.strip()}


def _session_whitelisted(target_id) -> bool:
    allowed = whitelist_ids()
    if not allowed:
        return True
    s = str(target_id)
    if s in allowed:
        return True
    for prefix in ("private_", "group_"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    if "_" in s:
        s = s.split("_", 1)[0]
    return s in allowed


def _allow_message(session_id: str) -> bool:
    config = app_context.active_config()
    if config is None or not config.get("anti_spam_enabled", False):
        return True
    win = float(config.get("anti_spam_window_seconds", 10) or 10)
    cap = max(1, int(config.get("anti_spam_max_in_window", 5) or 5))
    now = time.time()
    log = app_context._spam_log.setdefault(session_id, [])
    while log and now - log[0] > win:
        log.pop(0)
    log.append(now)
    return len(log) <= cap


def get_active_role() -> dict:
    config = app_context.active_config()
    if config is None:
        return {}
    roles = getattr(config, "roles", None) or {}
    return roles.get(getattr(config, "active_character", ""), {}) or \
        (next(iter(roles.values())) if roles else {})


def get_active_ctx() -> RoleContext:
    config = app_context.active_config()
    return RoleContext(config.config if config else {}, get_active_role())


def reply_ctx_of(role: dict, overrides: Optional[dict] = None,
                 session_id: str = "", client=None) -> RoleContext:
    sender = app_context.sender
    if session_id:
        config = config_for_session(session_id)
        pick = getattr(sender, "session_client", None) if sender is not None else None
        active = pick(session_id) if callable(pick) else None
    elif client is not None:
        config = config_for_client(client)
        active = client
    else:
        config = app_context.active_config()
        pick = getattr(sender, "_active_client", None) if sender is not None else None
        active = pick() if callable(pick) else None
    config = config or app_context.global_config
    if config is None:
        return RoleContext({}, {**(role or {}), **(overrides or {})})
    key = str((role or {}).get("character_key") or "")
    if key:
        role = (getattr(config, "roles", None) or {}).get(key, role)
    return RoleContext(config.config, {**(role or {}), **(overrides or {})},
                       getattr(type(active), "capabilities", None))


def role_memory(role: dict) -> Optional[MemoryManager]:
    if app_context.global_config is None or app_context.memory_manager is None:
        return None
    key = str((role or {}).get("character_key") or "").strip()
    if not key:
        return None
    mem = MemoryManager(app_context.global_config)
    mem.character_key = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", key) or "default"
    mem.character_name = str((role or {}).get("character_name") or mem.character_key)
    return mem


def memory_for_session(session_id: str = "", client=None) -> Optional[MemoryManager]:
    """会话所在接入方式绑定了专属角色时，返回该角色的记忆管理器；否则回退全局管理器。"""
    fallback = app_context.memory_manager
    if fallback is None:
        return None
    sender = app_context.sender
    channel_key = ""
    if client is not None and sender is not None:
        lookup = getattr(sender, "channel_key_of", None)
        if callable(lookup):
            channel_key = str(lookup(client) or "")
    if not channel_key and session_id and sender is not None:
        lookup = getattr(sender, "session_channel_key", None)
        if callable(lookup):
            channel_key = str(lookup(session_id) or "")
    if not channel_key:
        return fallback
    role_key = connection_role_key(app_context.global_config, channel_key)
    if not role_key:
        return fallback
    config = config_for_connection(channel_key) or app_context.global_config
    role = (getattr(config, "roles", None) or {}).get(role_key)
    if not role:
        return fallback
    mem = role_memory(role)
    return mem or fallback


def get_active_emotions() -> dict:
    return app_context.global_emotion_manager.emotions if app_context.global_emotion_manager else {}


def get_active_mimics() -> dict:
    return app_context.global_emotion_manager.mimics if app_context.global_emotion_manager else {}


def _load_role_voices(role: dict) -> None:
    key = (role or {}).get("character_key", "")
    if not key or app_context.global_emotion_manager is None or key in app_context._role_emotions_cache:
        return
    try:
        mgr = EmotionManager(RoleContext(app_context.global_config.config, role))
        app_context._role_emotions_cache[key] = mgr.emotions or get_active_emotions()
        app_context._role_mimics_cache[key] = mgr.mimics or get_active_mimics()
    except Exception as e:
        print(f"扫描角色 {key} 情绪失败: {e}")
        app_context._role_emotions_cache[key] = get_active_emotions()
        app_context._role_mimics_cache[key] = get_active_mimics()


def get_role_emotions(role: dict) -> dict:
    _load_role_voices(role)
    return app_context._role_emotions_cache.get((role or {}).get("character_key", "")) \
        or get_active_emotions()


def get_role_mimics(role: dict) -> dict:
    _load_role_voices(role)
    return app_context._role_mimics_cache.get((role or {}).get("character_key", "")) \
        or get_active_mimics()


def parse_session_target(session_id: str):
    text = restore_session_id(str(session_id))
    if text.startswith("private_"):
        return "private", text[len("private_"):]
    if text.startswith("group_"):
        return "group", text[len("group_"):].split("_", 1)[0]
    return "private", text


async def _fetch_member_name(client, group_id, qq: str) -> str:
    key = f"{group_id}|{qq}"
    if key in app_context._member_cache:
        return app_context._member_cache[key]
    name = ""
    getter = getattr(client, "get_group_member_info", None)
    if getter is not None:
        try:
            info = await getter(group_id=int(group_id), user_id=int(qq))
            if isinstance(info, dict):
                name = str(info.get("card") or info.get("nickname") or "")
            else:
                name = str(getattr(info, "card", "") or getattr(info, "nickname", "") or "")
        except Exception:
            name = ""
    name = str(name or "").strip()
    app_context._member_cache[key] = name
    return name



def _in_quiet_hours() -> bool:
    start = str(app_context.global_config.get("proactive_quiet_start", "23:00"))
    end = str(app_context.global_config.get("proactive_quiet_end", "08:00"))

    def to_minutes(hhmm):
        try:
            h, m = hhmm.split(":")[:2]
            return int(h) * 60 + int(m)
        except Exception:
            return None
    s, e, cur = to_minutes(start), to_minutes(end), to_minutes(time.strftime("%H:%M"))
    if s is None or e is None or cur is None or s == e:
        return False
    if s < e:
        return s <= cur < e
    return cur >= s or cur < e


def _parse_jitter_minutes(value) -> tuple:
    text = str(value or "").strip()
    if not text:
        return 0.0, 0.0
    nums = []
    for part in re.split(r"[~～\s,，、]+", text):
        part = part.strip()
        if not part:
            continue
        try:
            nums.append(max(0.0, float(part)))
        except ValueError:
            continue
    if not nums:
        return 0.0, 0.0
    if len(nums) >= 2:
        return min(nums[0], nums[1]), max(nums[0], nums[1])
    return 0.0, nums[0]


def connections_of(config) -> List[dict]:
    raw = config.get("connections", []) or []
    out = [c for c in raw if isinstance(c, dict) and c.get("id")]
    return sorted(out, key=lambda c: str(c.get("id")))


def connection_role_key(config, connection_id: str) -> str:
    target = str(connection_id or "")
    if not target:
        return ""
    for conn in connections_of(config):
        if str(conn.get("id")) != target:
            continue
        entry = _connection_role_entry(conn)
        if entry:
            return str(entry.get("character_key") or "")
        break
    for key, role in (getattr(config, "roles", None) or {}).items():
        if str((role or {}).get("connection_id") or "") == target:
            return str(key)
    return ""


def _connection_of(config, connection_id: str):
    target = str(connection_id or "")
    if not target:
        return None
    for conn in connections_of(config):
        if str(conn.get("id")) == target:
            return conn
    return None


def connection_config_profile(config, connection_id: str) -> str:
    conn = _connection_of(config, connection_id)
    return str((conn or {}).get("config_profile") or "").strip()


def _library():
    manager = app_context.memory_manager
    data_path = getattr(manager, "data_path", None) if manager is not None else None
    if not data_path:
        return None
    try:
        from .model_library import load_library_cached
        return load_library_cached(data_path)
    except Exception:
        return None


def _connection_role_entry(conn) -> dict:
    lib = _library()
    if not lib:
        return {}
    try:
        from .model_library import connection_role_entry
        return connection_role_entry(conn or {}, lib)
    except Exception:
        return {}


class ConnectionConfigView:

    def __init__(self, base, overrides: dict):
        object.__setattr__(self, "_base", base)
        object.__setattr__(self, "_overrides", dict(overrides or {}))
        object.__setattr__(self, "_data", None)
        object.__setattr__(self, "_parsed", None)

    def _merged(self) -> dict:
        data = object.__getattribute__(self, "_data")
        if data is None:
            base = object.__getattribute__(self, "_base")
            data = dict(getattr(base, "config", {}) or {})
            data.update(object.__getattribute__(self, "_overrides"))
            object.__setattr__(self, "_data", data)
        return data

    def _roles(self):
        cached = object.__getattribute__(self, "_parsed")
        if cached is None:
            from .config_loader import parse_roles_of
            cached = parse_roles_of(self._merged())
            object.__setattr__(self, "_parsed", cached)
        return cached

    @property
    def config(self) -> dict:
        return self._merged()

    def get(self, key: str, default=None):
        data = self._merged()
        if "." in key:
            value = data
            for part in key.split("."):
                if isinstance(value, dict) and part in value:
                    value = value[part]
                else:
                    return default
            return value
        return data.get(key, default)

    @property
    def roles(self):
        return self._roles()[1]

    @property
    def active_character(self):
        return self._roles()[0]

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_base"), name)


def config_for_connection(connection_id: str):
    base = app_context.global_config
    if base is None:
        return None
    conn = _connection_of(base, connection_id)
    name = str((conn or {}).get("config_profile") or "").strip()
    target = base
    if name and name != DEFAULT_PROFILE and app_context.memory_manager is not None:
        try:
            target = profile_loader(app_context.memory_manager.data_path, name) or base
        except Exception as e:
            print(f"[配置文件] {name} 读不出来，这条接入方式改用主配置：{e}")
            target = base
    overrides = {}
    lib = _library()
    if conn and lib:
        try:
            from .model_library import connection_overrides
            overrides = connection_overrides(conn, lib)
        except Exception:
            overrides = {}
    if not overrides:
        return target
    return ConnectionConfigView(target, overrides)


def config_for_client(client):
    sender = app_context.sender
    lookup = getattr(sender, "channel_key_of", None) if sender is not None else None
    if client is None or not callable(lookup):
        return app_context.global_config
    return config_for_connection(lookup(client))


def config_for_session(session_id: str):
    sender = app_context.sender
    lookup = getattr(sender, "session_channel_key", None) if sender is not None else None
    key = lookup(session_id) if (callable(lookup) and session_id) else ""
    return config_for_connection(key)


def role_connection_snapshot(config) -> tuple:
    out = []
    for conn in connections_of(config):
        out.append((str(conn.get("id")), str(conn.get("platform") or "napcat"),
                    bool(conn.get("enabled", True)),
                    str(conn.get("ws_url") or ""), str(conn.get("token") or ""),
                    str(conn.get("app_id") or ""), str(conn.get("app_secret") or ""),
                    str(conn.get("bot_id") or ""),
                    str(conn.get("config_profile") or ""),
                    connection_role_key(config, conn.get("id")),
                    str(conn.get("role_id") or ""),
                    tuple(str(conn.get(f"{k}_provider") or "") for k in PROVIDER_KINDS)))
    return tuple(sorted(out))


def build_connection_profiles(config) -> List[dict]:
    out = []
    for conn in connections_of(config):
        if not conn.get("enabled", True):
            continue
        platform = str(conn.get("platform") or "napcat")
        if platform not in CONNECTION_PLATFORMS:
            continue
        role_key = connection_role_key(config, conn.get("id"))
        profile = {"key": str(conn.get("id")),
                   "label": str(conn.get("name")
                                or CONNECTION_PLATFORM_NAMES.get(platform, platform)),
                   "platform": platform,
                   "role_key": role_key, "default": False,
                   "connection": dict(conn)}
        if platform == "napcat":
            profile["ws_url"] = str(conn.get("ws_url") or "ws://127.0.0.1:3001")
            profile["token"] = str(conn.get("token") or "")
        out.append(profile)
    pick = next((p for p in out if not p["role_key"]), out[0] if out else None)
    if pick is not None:
        pick["default"] = True
    return out


def resolve_target_roles(user_text: str, is_private: bool,
                         source_role_key: str = "") -> List[dict]:
    config = app_context.active_config()
    if config is None:
        return []
    key = str(source_role_key or "").strip()
    if key:
        role = (getattr(config, "roles", None) or {}).get(key)
        if role:
            return [role]
    active = get_active_role()
    if not active:
        return []
    if is_private or not config.get("multi_role_enabled", False):
        return [active]
    matched = []
    for role in (getattr(config, "roles", None) or {}).values():
        name = str(role.get("character_name", "")).strip()
        if name and name in user_text:
            matched.append(role)
    if not matched:
        return [active]
    try:
        cap = max(1, int(config.get("multi_role_max_replies", 2)))
    except (TypeError, ValueError):
        cap = 2
    return matched[:cap]
