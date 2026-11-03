# -*- coding: utf-8 -*-
import copy
import json
import os
import uuid
from pathlib import Path

from .jsonio import load_json_ex
from .security import _decrypt_value, _encrypt_value, _is_kept_secret

LIBRARY_FILE_NAME = "model_library.json"
PRESET_DIR_NAME = "config_presets"

# 库版本：结构与迁移逻辑变化时递增
LIBRARY_VERSION = 1

# ---------------- 用途 ----------------
PROVIDER_KINDS = ("chat", "caption", "embedding", "cloud_tts", "asr")
KIND_LABELS = {
    "chat": "聊天模型",
    "caption": "识图模型",
    "embedding": "向量模型",
    "cloud_tts": "云端 TTS",
    "asr": "语音识别",
}

PROVIDER_FIELD_MAP = {
    "chat": {
        "backend": "llm_backend",
        "base_url": "llm_base_url",
        "api_key": "llm_api_key",
        "model": "llm_model_name",
        "num_ctx": "num_ctx",
        "enable_think": "enable_think",
        "sampling_enabled": "llm_sampling_enabled",
        "top_p": "llm_top_p",
        "top_k": "llm_top_k",
        "repeat_penalty": "llm_repeat_penalty",
        "extra_body": "llm_extra_body",
        "timeout": "llm_timeout",
    },
    "caption": {
        "backend": "image_caption_backend",
        "base_url": "image_caption_base_url",
        "api_key": "image_caption_api_key",
        "model": "image_caption_model_name",
        "timeout": "image_caption_timeout",
    },
    "embedding": {
        "base_url": "llm_embedding_url",
        "model": "llm_embedding_model",
    },
    "cloud_tts": {
        "base_url": "cloud_tts_base_url",
        "protocol": "cloud_tts_protocol",
        "api_key": "cloud_tts_api_key",
        "model": "cloud_tts_model",
        "voice": "cloud_tts_voice",
        "voice_map": "cloud_tts_voice_map",
    },
    "asr": {
        "base_url": "asr_base_url",
        "api_key": "asr_api_key",
        "model": "asr_model",
        "api_style": "asr_api_style",
        "lang": "asr_lang",
    },
}

PROVIDER_FIELDS = ("backend", "base_url", "api_key", "model", "num_ctx",
                   "enable_think", "sampling_enabled", "top_p", "top_k",
                   "repeat_penalty", "extra_body", "timeout", "protocol",
                   "voice", "voice_map", "api_style", "lang")

DERIVED_CONFIG_KEYS = sorted({k for m in PROVIDER_FIELD_MAP.values() for k in m.values()})

ROLE_FIELDS = ("character_key", "personality_prompt", "json_prompt",
               "supplement_prompt", "default_voice", "ref_audio_root",
               "emotion_mimic_root", "text_lang", "prompt_lang", "connection_id")

_ROLE_LIBRARY_FIELDS = {
    "character_key": "character_key",
    "character_name": "name",
    "personality_prompt": "personality_prompt",
    "json_prompt": "json_prompt",
    "supplement_prompt": "supplement_prompt",
    "default_voice": "default_voice",
    "ref_audio_root": "ref_audio_root",
    "emotion_mimic_root": "emotion_mimic_root",
    "text_lang": "text_lang",
    "prompt_lang": "prompt_lang",
    "connection_id": "connection_id",
}

def _normalize_role(item: dict) -> dict:
    entry = {}
    entry["id"] = _text(item.get("id")) or _new_id("r")
    for field in ROLE_FIELDS:
        entry[field] = str(item.get(field, "") or "")
    entry["name"] = _text(item.get("name")) or _text(item.get("character_name")) \
        or _text(entry.get("character_key")) or "未命名角色"
    return entry

_MIGRATED_DIRS = set()


class ModelLibraryError(Exception):
    pass


# ================= 存取 =================

def library_path(data_path) -> Path:
    return Path(data_path) / LIBRARY_FILE_NAME


def _empty_library() -> dict:
    return {"version": LIBRARY_VERSION, "migrated": False, "roles": [], "providers": []}


def _unique_role_key(key: str, taken: set) -> str:
    if key not in taken:
        return key
    base, n = key, 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def _dedupe_role_keys(lib: dict) -> bool:
    seen = set()
    changed = False
    for entry in lib.get("roles") or []:
        key = str(entry.get("character_key") or "").strip()
        if not key:
            continue
        if key not in seen:
            seen.add(key)
            continue
        new_key = _unique_role_key(key, seen)
        entry["character_key"] = new_key
        seen.add(new_key)
        changed = True
        print(f"[模型库] 角色标识符 {key} 重复，已把"
              f"「{entry.get('name') or entry.get('id') or ''}」改为 {new_key}")
    return changed


def load_library(data_path) -> dict:
    data, readable = load_json_ex(library_path(data_path), None)
    if not readable or not isinstance(data, dict):
        lib = _empty_library()
    else:
        lib = _empty_library()
        lib.update({k: v for k, v in data.items() if k in ("version", "migrated")})
        lib["roles"] = [r for r in (data.get("roles") or []) if isinstance(r, dict)]
        lib["providers"] = [p for p in (data.get("providers") or []) if isinstance(p, dict)]
    for provider in lib["providers"]:
        key = provider.get("api_key")
        if isinstance(key, str) and key.startswith(("enc:", "enc2:")):
            provider["api_key"] = _decrypt_value(key) or ""
    if _dedupe_role_keys(lib):
        try:
            save_library(data_path, lib)
        except OSError as e:
            print(f"[模型库] 角色标识符去重后写回失败：{type(e).__name__}: {e}")
    return lib


def save_library(data_path, lib: dict) -> None:
    payload = copy.deepcopy(lib)
    for provider in payload.get("providers") or []:
        key = provider.get("api_key")
        if isinstance(key, str) and key:
            provider["api_key"] = _encrypt_value(key)
    path = library_path(data_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))
    _LIB_CACHE.pop(str(path.resolve()), None)


_LIB_CACHE = {}


def load_library_cached(data_path) -> dict:
    path = library_path(data_path)
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        return _empty_library()
    key = str(path.resolve())
    hit = _LIB_CACHE.get(key)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    lib = load_library(data_path)
    _LIB_CACHE[key] = (stamp, lib)
    return lib


def connection_overrides(conn: dict, lib: dict) -> dict:
    if not isinstance(conn, dict) or not isinstance(lib, dict):
        return {}
    out = {}
    role_id = str(conn.get("role_id") or "").strip()
    if role_id:
        roles = resolve_roles(lib, [role_id])
        if roles:
            out["role_ids"] = [role_id]
            out["roles"] = roles
            out["active_character"] = roles[0].get("character_key") or ""
    for kind in PROVIDER_KINDS:
        picked = str(conn.get(f"{kind}_provider") or "").strip()
        entry = selected_provider(lib, kind, picked)
        if not entry:
            continue
        out[f"{kind}_provider"] = picked
        for field, cfg_key in PROVIDER_FIELD_MAP[kind].items():
            out[cfg_key] = entry.get(field, "")
    return out


def connection_role_entry(conn: dict, lib: dict) -> dict:
    if not isinstance(conn, dict) or not isinstance(lib, dict):
        return {}
    role_id = str(conn.get("role_id") or "").strip()
    if not role_id:
        return {}
    for entry in lib.get("roles") or []:
        if str(entry.get("id") or "") == role_id:
            return entry
    return {}


def preset_dir(data_path) -> Path:
    return Path(data_path) / PRESET_DIR_NAME


# ================= 条目辅助 =================

def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _text(value) -> str:
    return str(value if value is not None else "").strip()


def _provider_entry(kind: str, name: str, values: dict) -> dict:
    entry = {"id": _new_id("p"), "name": name, "kind": kind}
    for field in PROVIDER_FIELDS:
        entry[field] = values.get(field, "")
    return entry


def _role_entry(values: dict) -> dict:
    entry = {"id": _new_id("r")}
    for config_field, lib_field in _ROLE_LIBRARY_FIELDS.items():
        entry[lib_field] = str(values.get(config_field, "") or "")
    if not entry.get("name"):
        entry["name"] = entry.get("character_key") or "未命名角色"
    return entry


def _provider_content(kind: str, values: dict) -> dict:
    return {field: values.get(field, "") for field in PROVIDER_FIELDS
            if field in PROVIDER_FIELD_MAP[kind]}


def _find_provider(lib: dict, kind: str, values: dict):
    want = _provider_content(kind, values)
    for entry in lib.get("providers") or []:
        if entry.get("kind") != kind:
            continue
        if all(str(entry.get(field, "") or "") == str(value or "")
               for field, value in want.items()):
            return entry
    return None


def _find_role(lib: dict, values: dict):
    want = {lib_field: str(values.get(config_field, "") or "")
            for config_field, lib_field in _ROLE_LIBRARY_FIELDS.items()
            if lib_field != "name"}
    for entry in lib.get("roles") or []:
        if all(str(entry.get(field, "") or "") == value for field, value in want.items()):
            return entry
    return None


PROVIDER_NAME_MAX = 60


def _provider_name(kind: str, values: dict, label: str, taken: set) -> str:
    kind_label = KIND_LABELS.get(kind, kind)
    model = _text(values.get("model"))
    host = _text(values.get("base_url")).replace("http://", "").replace("https://", "")
    host = host.split("/")[0]
    base = model or host
    suffix = f"（{label}）" if label and label != "主配置" else ""
    if not base:
        name = kind_label + suffix
    else:
        tail = f" · {kind_label}{suffix}"
        room = PROVIDER_NAME_MAX - len(tail)
        if len(base) > room:
            base = base[:max(4, room - 1)].rstrip(" ·-") + "…"
        name = base + tail
    name = name[:PROVIDER_NAME_MAX] or kind_label
    candidate, n = name, 2
    while candidate in taken:
        candidate = f"{name} {n}"
        n += 1
    return candidate


# ================= 迁移 =================

def _unique_role_name(name: str, label: str, lib: dict) -> str:
    taken = {str(r.get("name") or "") for r in (lib.get("roles") or [])}
    if name not in taken:
        return name
    base = f"{name}（{label}）" if label and label != "主配置" else f"{name}（{label or '副本'}）"
    candidate, n = base, 2
    while candidate in taken:
        candidate = f"{base} {n}"
        n += 1
    return candidate


def migrate_config_dict(cfg: dict, lib: dict, label: str = "主配置") -> bool:
    if not isinstance(cfg, dict):
        return False
    changed = False

    # ---- 角色：roles 列表 → 角色库 + role_ids ----
    if "role_ids" not in cfg:
        roles = cfg.get("roles")
        if isinstance(roles, dict):
            roles = list(roles.values())
        roles = [r for r in (roles or []) if isinstance(r, dict) and _text(r.get("character_key"))]
        if not roles:
            from .config_loader import DEFAULT_ROLES
            roles = copy.deepcopy(DEFAULT_ROLES)
        ids = []
        for role in roles:
            entry = _find_role(lib, role)
            if entry is None:
                entry = _role_entry(role)
                entry["name"] = _unique_role_name(entry["name"], label, lib)
                taken_keys = {str(r.get("character_key") or "").strip()
                              for r in (lib.get("roles") or [])}
                old_key = entry.get("character_key") or ""
                new_key = _unique_role_key(old_key, taken_keys)
                if new_key != old_key:
                    entry["character_key"] = new_key
                    print(f"[模型库] 迁移「{label}」时角色标识符 {old_key} 已被占用，改用 {new_key}")
                lib.setdefault("roles", []).append(entry)
            ids.append(entry["id"])
        cfg["role_ids"] = ids
        cfg.pop("roles", None)
        changed = True

    for kind in PROVIDER_KINDS:
        sel_key = f"{kind}_provider"
        if sel_key in cfg:
            continue
        field_map = PROVIDER_FIELD_MAP[kind]
        values = {field: cfg.get(cfg_key, "") for field, cfg_key in field_map.items()}
        entry = None
        if any(_text(v) for v in values.values()):
            entry = _find_provider(lib, kind, values)
            if entry is None:
                taken = {p.get("name") for p in lib.get("providers") or []}
                entry = _provider_entry(kind, _provider_name(kind, values, label, taken), values)
                lib.setdefault("providers", []).append(entry)
        cfg[sel_key] = entry["id"] if entry else ""
        changed = True

    for key in DERIVED_CONFIG_KEYS:
        if key in cfg:
            cfg.pop(key, None)
            changed = True
    return changed


def _read_preset_file(path: Path):
    data, readable = load_json_ex(path, None)
    if not readable or not isinstance(data, dict):
        return None
    inner = data.get("config")
    if isinstance(inner, dict) and ("note" in data or "created_at" in data):
        return inner
    return data


def _write_preset_file(path: Path, cfg: dict) -> None:
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))


def ensure_migrated(data_path, config_loader=None) -> dict:
    root = Path(data_path)
    key = str(root.resolve())
    if key in _MIGRATED_DIRS:
        return load_library(root)
    lib = load_library(root)
    if not lib.get("migrated"):
        try:
            if config_loader is not None and isinstance(config_loader.config, dict):
                if migrate_config_dict(config_loader.config, lib, "主配置"):
                    config_loader._atomic_save(config_loader.config)
            work = preset_dir(root)
            if work.is_dir():
                for path in sorted(work.glob("*.json")):
                    cfg = _read_preset_file(path)
                    if cfg is None:
                        continue
                    if migrate_config_dict(cfg, lib, path.stem):
                        _write_preset_file(path, cfg)
            lib["migrated"] = True
            save_library(root, lib)
            print(f"[模型库] 已把角色与模型配置集中到 {library_path(root).name}："
                  f"{len(lib.get('roles') or [])} 个角色、{len(lib.get('providers') or [])} 条模型服务。")
        except Exception as e:
            print(f"[模型库] 迁移失败（本次沿用旧配置形态，下次启动再试）：{type(e).__name__}: {e}")
    _MIGRATED_DIRS.add(key)
    return lib


# ================= 解析（库 → 配置扁平键） =================

def resolve_roles(lib: dict, ids) -> list:
    by_id = {str(r.get("id") or ""): r for r in (lib.get("roles") or [])}
    out = []
    for rid in ids or []:
        entry = by_id.get(str(rid))
        if not entry:
            continue
        role = {config_field: str(entry.get(lib_field, "") or "")
                for config_field, lib_field in _ROLE_LIBRARY_FIELDS.items()}
        role["character_key"] = role.get("character_key") or ""
        role["napcat_ws_url"] = ""
        role["napcat_token"] = ""
        out.append(role)
    return out


def selected_provider(lib: dict, kind: str, sel) -> dict:
    sel = str(sel or "").strip()
    if not sel:
        return {}
    for entry in lib.get("providers") or []:
        if entry.get("kind") == kind and str(entry.get("id") or "") == sel:
            return entry
    return {}


def apply_library(config: dict, lib: dict) -> dict:
    if not isinstance(config, dict):
        return config
    if "role_ids" in config:
        roles = resolve_roles(lib, config.get("role_ids"))
        config["roles"] = roles
        keys = [r.get("character_key") for r in roles if r.get("character_key")]
        active = str(config.get("active_character") or "")
        if keys and active not in keys:
            config["active_character"] = keys[0]
    for kind in PROVIDER_KINDS:
        entry = selected_provider(lib, kind, config.get(f"{kind}_provider"))
        if not entry:
            continue
        for field, cfg_key in PROVIDER_FIELD_MAP[kind].items():
            config[cfg_key] = entry.get(field, "")
    return config


def strip_derived(config: dict) -> dict:
    if not isinstance(config, dict):
        return config
    config.pop("roles", None)
    for key in DERIVED_CONFIG_KEYS:
        config.pop(key, None)
    return config


def strip_derived_if_managed(config: dict) -> dict:
    if not isinstance(config, dict):
        return config
    if "role_ids" in config or any(f"{kind}_provider" in config for kind in PROVIDER_KINDS):
        strip_derived(config)
    return config


# ================= 保存接口的净化 =================

def _normalize_role(item: dict) -> dict:
    entry = {}
    rid = _text(item.get("id"))
    entry["id"] = rid or _new_id("r")
    for field in ROLE_FIELDS:
        entry[field] = str(item.get(field, "") or "")
    entry["name"] = _text(item.get("name")) or _text(entry.get("character_name")) \
        or _text(entry.get("character_key")) or "未命名角色"
    return entry


def _normalize_provider(item: dict, keep_api_key_of: dict) -> dict:
    kind = _text(item.get("kind"))
    if kind not in PROVIDER_KINDS:
        raise ModelLibraryError(f"未知的模型用途：{kind or '（空）'}")
    entry = {"id": _text(item.get("id")) or _new_id("p"), "name": _text(item.get("name")), "kind": kind}
    for field in PROVIDER_FIELDS:
        entry[field] = str(item.get(field, "") if item.get(field) is not None else "")
    if _is_kept_secret(entry.get("api_key")):
        stored = keep_api_key_of.get(entry["id"])
        if stored is not None:
            entry["api_key"] = stored
    if not entry["name"]:
        taken = set()
        entry["name"] = _provider_name(kind, entry, "", taken) or KIND_LABELS[kind]
    return entry


def normalize_library_items(roles=None, providers=None, current: dict = None):
    current = current or {}
    keep_api_key_of = {str(p.get("id") or ""): p.get("api_key") or ""
                       for p in (current.get("providers") or []) if isinstance(p, dict)}
    out_roles = None if roles is None else []
    out_providers = None if providers is None else []
    if roles is not None:
        if not isinstance(roles, list):
            return [], [], "角色列表格式错误"
        seen = set()
        for item in roles:
            if not isinstance(item, dict):
                return [], [], "角色条目格式错误"
            entry = _normalize_role(item)
            if entry["character_key"] and entry["character_key"] in seen:
                return [], [], f"角色标识符重复：{entry['character_key']}"
            if entry["character_key"]:
                seen.add(entry["character_key"])
            out_roles.append(entry)
    if providers is not None:
        if not isinstance(providers, list):
            return [], [], "模型服务列表格式错误"
        for item in providers:
            if not isinstance(item, dict):
                return [], [], "模型服务条目格式错误"
            try:
                out_providers.append(_normalize_provider(item, keep_api_key_of))
            except ModelLibraryError as e:
                return [], [], str(e)
    return out_roles, out_providers, ""
