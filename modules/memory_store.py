# -*- coding: utf-8 -*-
import json
import os
import re
from pathlib import Path

from .adapters import friendly_user_label
from .app_paths import _resolve_data_dir
from .config_loader import ConfigLoader


class MemoryManager:
    def __init__(self, config: ConfigLoader):
        self.config = config
        self.data_path = _resolve_data_dir(config)
        self.data_path.mkdir(parents=True, exist_ok=True)
        roles = getattr(config, "roles", None) or {}
        active = str(getattr(config, "active_character", "")
                     or config.get("active_character", "") or "")
        role = roles.get(active) or (next(iter(roles.values())) if roles else {}) or {}
        key = str(role.get("character_key") or config.get("character_key", "") or "").strip()
        self.character_key = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", key) or "default"
        self.character_name = str(role.get("character_name")
                                  or config.get("character_name", "")
                                  or self.character_key)
        self.isolated_session = config.get("isolated_session", False)

    def get_memory_file(self, session_id: str) -> Path:
        safe_session = re.sub(r'[^A-Za-z0-9_\-]', '_', session_id)
        return self.data_path / f"{self.character_key}_{safe_session}.json"

    def load_session_data(self, session_id: str) -> dict:
        file_path = self.get_memory_file(session_id)
        if file_path.exists():
            try:
                data = json.loads(file_path.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    if not isinstance(data.get("history"), list):
                        data["history"] = []
                    else:
                        data["history"] = [m for m in data["history"] if isinstance(m, dict)]
                    if not isinstance(data.get("meta"), dict):
                        data["meta"] = {}
                    return data
            except Exception as e:
                try:
                    os.replace(str(file_path), str(file_path) + ".corrupt")
                    print(f"会话文件损坏已备份为 {file_path.name}.corrupt：{e}")
                except Exception:
                    pass
        return {"character_name": self.character_name, "history": [], "meta": {}}

    def save_session_data(self, session_id: str, data: dict):
        file_path = self.get_memory_file(session_id)
        data["character_name"] = data.get("character_name", self.character_name)
        data.setdefault("meta", {})
        from modules.jsonio import save_json
        save_json(file_path, data)

    def load_history(self, session_id: str) -> list:
        return self.load_session_data(session_id).get("history", [])

    def save_history(self, session_id: str, history: list):
        data = self.load_session_data(session_id)
        data["history"] = history
        self.save_session_data(session_id, data)

    def get_meta(self, session_id: str) -> dict:
        return self.load_session_data(session_id).get("meta", {})

    def update_meta(self, session_id: str, **kwargs):
        data = self.load_session_data(session_id)
        data.setdefault("meta", {}).update(kwargs)
        self.save_session_data(session_id, data)

    def cleanup_voice_cache(self, max_cache: int = 20):
        try:
            cache_files = (list(self.data_path.glob("temp_*.wav"))
                           + list(self.data_path.glob("combined_*.wav"))
                           + list(self.data_path.glob("temp_img_*")))
            if len(cache_files) <= max_cache:
                return
            cache_files.sort(key=lambda x: x.stat().st_mtime)
            to_delete = len(cache_files) - max_cache
            for old_file in cache_files[:to_delete]:
                try:
                    temp_name = old_file.with_suffix('.tmp_del')
                    old_file.rename(temp_name)
                    temp_name.unlink(missing_ok=True)
                except PermissionError:
                    print(f"文件 {old_file.name} 被占用，跳过删除")
                except Exception as e:
                    print(f"删除 {old_file.name} 时异常: {e}")
        except Exception as e:
            print(f"清理语音缓存失败: {e}")

    def migrate_legacy_memory(self, session_id: str):
        legacy_file = self.data_path / f"{self.character_key}DATA.json"
        if not legacy_file.exists():
            return
        current_file = self.get_memory_file(session_id)
        if current_file.exists():
            return
        try:
            with open(legacy_file, 'r', encoding='utf-8') as f:
                legacy_data = json.load(f)
            history = legacy_data.get("history", [])
            character_name = legacy_data.get("character_name", self.character_name)
            with open(current_file, 'w', encoding='utf-8') as f:
                json.dump({"character_name": character_name, "history": history}, f, ensure_ascii=False, indent=2)
            legacy_file.unlink()
            print(f"已迁移旧记忆文件到 {current_file.name}，并删除旧文件。")
        except Exception as e:
            print(f"迁移旧记忆文件失败: {e}")

    def _role_display_names(self) -> dict:
        names = {}
        try:
            from .model_library import load_library_cached
            for role in (load_library_cached(self.data_path).get("roles") or []):
                if not isinstance(role, dict):
                    continue
                key = str(role.get("character_key") or "").strip()
                name = str(role.get("name") or "").strip()
                if key and name:
                    names[key] = name
        except Exception as e:
            print(f"读取角色库失败: {type(e).__name__}: {e}")
        roles = getattr(self.config, "roles", None) or {}
        if isinstance(roles, dict):
            for key, role in roles.items():
                if not isinstance(role, dict):
                    continue
                display = str(role.get("character_name") or "").strip()
                role_key = str(role.get("character_key") or key).strip()
                if role_key and display:
                    names.setdefault(role_key, display)
        if self.character_key and self.character_name:
            names.setdefault(self.character_key, self.character_name)
        return names

    def _display_name_of(self, role_key: str, stored_name, role_display: dict) -> str:
        name = str(stored_name or "").strip()
        display = role_display.get(role_key)
        if display and (not name or name.lower() == str(role_key).lower()):
            return display
        return name or role_key

    def list_memories(self):
        memories = []
        role_display = self._role_display_names()
        if self.data_path.exists():
            for f in self.data_path.glob("*.json"):
                if not _is_memory_filename(f.name):
                    continue
                try:
                    role_name = f.name.split("_")[0] if "_" in f.name else "未知"
                    with open(f, 'r', encoding='utf-8') as fh:
                        data = json.load(fh)
                    character_name = self._display_name_of(
                        role_name, data.get("character_name"), role_display)
                    history = data.get("history", [])
                    last_sentence = ""
                    for msg in reversed(history):
                        if msg.get("role") == "assistant":
                            last_sentence = str(msg.get("content", ""))[:50]
                            break
                    if "private_" in f.name:
                        sender_id = f.name.split("private_")[-1].replace(".json", "")
                        sender_name = None
                        for msg in reversed(history):
                            if msg.get("role") == "user" and msg.get("sender_name"):
                                sender_name = msg["sender_name"]
                                break
                        if not sender_name:
                            sender_name = sender_id
                        display_name = (f"{character_name}和"
                                        f"{friendly_user_label('', str(sender_name))}的聊天")
                    else:
                        group_id = f.name.split("group_")[-1].replace(".json", "")
                        group_name = f"群聊{group_id}"
                        display_name = f"{character_name}在{group_name}的聊天"
                    memories.append({
                        "filename": f.name,
                        "display_name": display_name,
                        "last_sentence": last_sentence,
                        "modified_time": f.stat().st_mtime,
                        "role_name": role_name,
                        "character_name": character_name,
                        "message_count": len(history)
                    })
                except Exception as e:
                    print(f"读取记忆文件 {f.name} 失败: {e}")
        return memories

    def get_history(self, filename: str):
        if not _is_memory_filename(filename):
            return {"success": False, "error": "非法文件名"}
        file_path = (self.data_path / filename).resolve()
        if self.data_path.resolve() not in file_path.parents:
            return {"success": False, "error": "路径不安全"}
        if not file_path.exists():
            return {"success": False, "error": "文件不存在"}
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            role_key = filename.split("_")[0] if "_" in filename else ""
            character_name = self._display_name_of(
                role_key, data.get("character_name"), self._role_display_names()) or "未知"
            return {"success": True, "character_name": character_name, "history": data.get("history", [])}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def delete_memory_file(self, filename: str):
        if not _is_memory_filename(filename):
            return False
        target = (self.data_path / filename).resolve()
        if self.data_path.resolve() in target.parents and target.exists():
            try:
                target.unlink()
                return True
            except Exception as e:
                print(f"删除失败 {filename}: {e}")
        return False

    def delete_messages(self, filename: str, indices: list):
        if not _is_memory_filename(filename):
            return {"success": False, "error": "非法文件名"}
        if not isinstance(indices, list) or not all(isinstance(i, int) for i in indices):
            return {"success": False, "error": "索引必须为整数列表"}
        file_path = (self.data_path / filename).resolve()
        if self.data_path.resolve() not in file_path.parents:
            return {"success": False, "error": "路径不安全"}
        if not file_path.exists():
            return {"success": False, "error": "文件不存在"}
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            history = data.get("history", [])
            valid_indices = sorted(set(indices), reverse=True)
            deleted_count = 0
            for idx in valid_indices:
                if 0 <= idx < len(history):
                    history.pop(idx)
                    deleted_count += 1
            data["history"] = history
            from modules.jsonio import save_json
            save_json(file_path, data)
            return {"success": True, "deleted_count": deleted_count}
        except Exception as e:
            return {"success": False, "error": str(e)}


_MEMORY_FILE_RE = re.compile(r'^[^\\/:*?"<>|]+?_(private|group)_[A-Za-z0-9_\-]+\.json$')


def _is_memory_filename(name) -> bool:
    return bool(_MEMORY_FILE_RE.match(str(name or "")))


_SANITIZED_ID_SUFFIXES = (("_im_wechat", "@im.wechat"),
                          ("_chatroom", "@chatroom"),
                          ("_im_bot", "@im.bot"))


def restore_session_id(session_id: str) -> str:
    text = str(session_id or "")
    for safe, real in _SANITIZED_ID_SUFFIXES:
        if text.endswith(safe):
            return text[: -len(safe)] + real
    return text


def merge_restored_keys(raw, combine=max):
    out = {}
    for key, value in (raw or {}).items():
        fixed = restore_session_id(str(key))
        if fixed in out:
            try:
                out[fixed] = combine(out[fixed], value)
            except TypeError:
                continue
            continue
        out[fixed] = value
    return out


def _memory_session_id(filename: str) -> str:
    """murasame_private_10001.json → private_10001"""
    stem = str(filename or "").rsplit(".", 1)[0]
    for tag in ("private_", "group_"):
        idx = stem.find(tag)
        if idx >= 0:
            return restore_session_id(stem[idx:])
    return ""
