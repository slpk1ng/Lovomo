# -*- coding: utf-8 -*-
"""会话/角色运行时上下文辅助与多角色连接路由。

自 main.py 原样搬迁（refactor_plan 第 11 节）：活跃角色/回复上下文、白名单/
防刷、角色情绪缓存、群成员昵称、会话目标解析与连接路由函数。管理器与容器
槽位（memory_manager、global_config、sender、global_emotion_manager、
_spam_log、_role_emotions_cache、_role_mimics_cache、_member_cache）在
modules.app_context，本模块经 app_context 读写。
"""
import re
import time
from typing import List, Optional

from modules import app_context
from modules.memory_store import MemoryManager, _MEMORY_FILE_RE, restore_session_id
from modules.llm_helpers import RoleContext
from modules.emotion_voices import EmotionManager
from modules.config_loader import CONNECTION_PLATFORMS, CONNECTION_PLATFORM_NAMES
from modules.config_presets import DEFAULT_PROFILE, profile_loader


def list_known_sessions() -> list:
    """列出所有留下过聊天记录的会话（定时任务/事件/待办选发送目标用）。

    会话记忆文件名是 <角色>_<private|group>_<号码>.json，一个会话可能有多个
    角色的记忆文件，所以按号码去重。返回的 session_id 是纯数字号码。
    """
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
        # 记忆文件名把 @ 与 . 净化成了下划线，这里要还原成运行时写法，
        # 否则同一个微信会话会被当成两个（选目标时查不到、openid 还会被截断）
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
    """回复用的上下文：带上当前通道的能力位，提示词据此只讲这条通道做得到的动作。

    给了 session_id 就按这条会话该走的连接取能力位与配置（主动消息不在 for_session 里）；
    给了 client 就按这条连接取（按角色生成、没有具体会话的主动内容）；
    都没有就用当前正在发送的连接，配置同样跟随当前处理链路（消息链路里它已经是那条
    连接绑定的配置）——否则接入方式绑了配置文件，回复却仍按主配置生成。
    角色内容一律以这份配置里的同标识角色为准，
    免得接入方式绑了配置文件、人设却还是主配置那份。

    能力位必须查**类**属性：NapCat 客户端的 __getattr__ 会给任意属性返回函数，
    hasattr / getattr 探不出真假。没声明的能力位一律按支持处理。
    """
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
    """按角色拿一份记忆访问器（会话文件按角色建档，档案导入导出用）。"""
    if app_context.global_config is None or app_context.memory_manager is None:
        return None
    key = str((role or {}).get("character_key") or "").strip()
    if not key:
        return None
    mem = MemoryManager(app_context.global_config)
    mem.character_key = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", key) or "default"
    mem.character_name = str((role or {}).get("character_name") or mem.character_key)
    return mem


def get_active_emotions() -> dict:
    return app_context.global_emotion_manager.emotions if app_context.global_emotion_manager else {}


def get_active_mimics() -> dict:
    return app_context.global_emotion_manager.mimics if app_context.global_emotion_manager else {}


def _load_role_voices(role: dict) -> None:
    """按角色扫描参考音频目录，一次缓存语气与情绪模仿两套配置。"""
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
    """按角色获取情绪配置（各角色可有独立 ref_audio_root），带缓存。"""
    _load_role_voices(role)
    return app_context._role_emotions_cache.get((role or {}).get("character_key", "")) \
        or get_active_emotions()


def get_role_mimics(role: dict) -> dict:
    """按角色获取情绪模仿配置（各角色可有独立 emotion_mimic_root），带缓存。"""
    _load_role_voices(role)
    return app_context._role_mimics_cache.get((role or {}).get("character_key", "")) \
        or get_active_mimics()


def parse_session_target(session_id: str):
    """从会话ID解析发送目标：private_123 → (private,123)；group_456[_789] → (group,456)

    会话ID可能来自记忆文件名（非 [A-Za-z0-9_-] 都被净化成 _）：先还原成运行时写法，
    否则微信 openid 会被从 _ 处截断成半截，发给一个不存在的用户。
    """
    text = restore_session_id(str(session_id))
    if text.startswith("private_"):
        return "private", text[len("private_"):]
    if text.startswith("group_"):
        # 群号本身不含 _；group_456_789 是开了隔离会话，只取群号
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
    """解析主动消息时间抖动区间（分钟）。支持单个数字或区间，分隔符：~ ～ 空格 , ， 、"""
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
    """接入方式清单（按 id 稳定排序，方便界面与日志对照）。"""
    raw = config.get("connections", []) or []
    out = [c for c in raw if isinstance(c, dict) and c.get("id")]
    return sorted(out, key=lambda c: str(c.get("id")))


def connection_role_key(config, connection_id: str) -> str:
    """绑定到这条接入方式上的角色（没有绑定返回空串）。

    老配置里「每个角色一条独占连接」就是这么表达的，迁移后变成角色引用连接 id。
    """
    target = str(connection_id or "")
    if not target:
        return ""
    for key, role in (getattr(config, "roles", None) or {}).items():
        if str((role or {}).get("connection_id") or "") == target:
            return str(key)
    return ""


def connection_config_profile(config, connection_id: str) -> str:
    """这条接入方式绑定的配置文件名（没绑定返回空串，表示跟随主配置）。"""
    target = str(connection_id or "")
    if not target:
        return ""
    for conn in connections_of(config):
        if str(conn.get("id")) == target:
            return str(conn.get("config_profile") or "").strip()
    return ""


def config_for_connection(connection_id: str):
    """这条接入方式该用的配置访问器；没绑配置文件或读不出来时用主配置。"""
    base = app_context.global_config
    if base is None:
        return None
    name = connection_config_profile(base, connection_id)
    if not name or name == DEFAULT_PROFILE or app_context.memory_manager is None:
        return base
    try:
        return profile_loader(app_context.memory_manager.data_path, name) or base
    except Exception as e:
        print(f"[配置文件] {name} 读不出来，这条接入方式改用主配置：{e}")
        return base


def config_for_client(client):
    """这条客户端对应的接入方式该用的配置。"""
    sender = app_context.sender
    lookup = getattr(sender, "channel_key_of", None) if sender is not None else None
    if client is None or not callable(lookup):
        return app_context.global_config
    return config_for_connection(lookup(client))


def config_for_session(session_id: str):
    """这条会话走的接入方式该用的配置（主动消息也按它选配置）。"""
    sender = app_context.sender
    lookup = getattr(sender, "session_channel_key", None) if sender is not None else None
    key = lookup(session_id) if (callable(lookup) and session_id) else ""
    return config_for_connection(key)


def role_connection_snapshot(config) -> tuple:
    """接入方式快照，用来判断配置保存后是否需要重连。"""
    out = []
    for conn in connections_of(config):
        out.append((str(conn.get("id")), str(conn.get("platform") or "napcat"),
                    bool(conn.get("enabled", True)),
                    str(conn.get("ws_url") or ""), str(conn.get("token") or ""),
                    str(conn.get("app_id") or ""), str(conn.get("app_secret") or ""),
                    str(conn.get("bot_id") or ""),
                    str(conn.get("config_profile") or ""),
                    connection_role_key(config, conn.get("id"))))
    return tuple(sorted(out))


def build_connection_profiles(config) -> List[dict]:
    """要建立的连接清单：只取启用的接入方式，按平台交给对应的适配器。"""
    out = []
    default_taken = False
    for conn in connections_of(config):
        if not conn.get("enabled", True):
            continue
        platform = str(conn.get("platform") or "napcat")
        if platform not in CONNECTION_PLATFORMS:
            continue
        role_key = connection_role_key(config, conn.get("id"))
        # 没绑角色的第一条作为默认发送连接（其余只在收到消息时用）
        is_default = not role_key and not default_taken
        if is_default:
            default_taken = True
        profile = {"key": str(conn.get("id")),
                   "label": str(conn.get("name")
                                or CONNECTION_PLATFORM_NAMES.get(platform, platform)),
                   "platform": platform,
                   "role_key": role_key, "default": is_default,
                   "connection": dict(conn)}
        if platform == "napcat":
            profile["ws_url"] = str(conn.get("ws_url") or "ws://127.0.0.1:3001")
            profile["token"] = str(conn.get("token") or "")
        out.append(profile)
    return out


def resolve_target_roles(user_text: str, is_private: bool,
                         source_role_key: str = "") -> List[dict]:
    """多角色路由：账号绑了角色就用它；否则群聊按消息中出现的角色名路由。"""
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
