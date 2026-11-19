# -*- coding: utf-8 -*-
import json
import os
import re
from pathlib import Path
from urllib.parse import quote, unquote

from .jsonio import file_lock, load_json_ex, save_json

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
        self.character_key = key or "default"
        self.character_name = str(role.get("character_name")
                                  or config.get("character_name", "")
                                  or self.character_key)
        self.isolated_session = config.get("isolated_session", False)

    def get_memory_file(self, session_id: str) -> Path:
        # Percent encoding is reversible; punctuation and Unicode no longer collide.
        safe_session = quote(str(session_id), safe="")
        return self.data_path / f"{_encode_role_key(self.character_key)}_{safe_session}.json"

    def _migrate_session_file(self, session_id: str, target: Path):
        if target.exists():
            return
        legacy_id = re.sub(r'[^A-Za-z0-9_\-]', '_', session_id)
        legacy_key = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", self.character_key)
        legacy = self.data_path / f"{legacy_key}_{legacy_id}.json"
        if legacy == target or not legacy.is_file():
            return
        with file_lock(legacy):
            raw, readable = load_json_ex(legacy, {})
            if not readable:
                raise OSError(f"旧会话文件不可读，已停止写入：{legacy.name}")
            recorded_id = raw.get("session_id") if isinstance(raw, dict) else None
            if legacy_key != _encode_role_key(self.character_key) and raw.get("character_key") != self.character_key:
                raise ValueError(f"旧会话角色名存在歧义，已保留原文件：{legacy.name}")
            known_id = restore_session_id(legacy_id)
            if recorded_id != session_id and not (known_id != legacy_id and known_id == session_id):
                # Unknown lossy filenames cannot safely be assigned to a conversation.
                raise ValueError(f"旧会话文件名存在歧义，需确认归属后迁移：{legacy.name}")
            save_json(target, raw)
            legacy.unlink()

    def load_session_data(self, session_id: str) -> dict:
        file_path = self.get_memory_file(session_id)
        with file_lock(file_path):
            self._migrate_session_file(session_id, file_path)
            data, readable = load_json_ex(file_path, {})
            if not readable:
                raise OSError(f"会话文件暂时不可读，已停止本次读写：{file_path.name}")
            if not isinstance(data, dict):
                raise ValueError(f"会话文件格式错误，已保留原文件：{file_path.name}")
            if "history" in data and not isinstance(data["history"], list):
                raise ValueError(f"会话历史格式错误：{file_path.name}")
            if "meta" in data and not isinstance(data["meta"], dict):
                raise ValueError(f"会话元数据格式错误：{file_path.name}")
            data.setdefault("character_name", self.character_name)
            data.setdefault("history", [])
            data.setdefault("meta", {})
            data["history"] = [m for m in data["history"] if isinstance(m, dict)]
            return data

    def save_session_data(self, session_id: str, data: dict):
        file_path = self.get_memory_file(session_id)
        with file_lock(file_path):
            # Refuse to replace a file that cannot currently be read.
            self.load_session_data(session_id)
            payload = dict(data)
            payload.setdefault("character_name", self.character_name)
            payload.setdefault("meta", {})
            payload["session_id"] = session_id
            payload["character_key"] = self.character_key
            save_json(file_path, payload)

    def load_history(self, session_id: str) -> list:
        return self.load_session_data(session_id).get("history", [])

    def save_history(self, session_id: str, history: list):
        with file_lock(self.get_memory_file(session_id)):
            data = self.load_session_data(session_id)
            data["history"] = history
            self.save_session_data(session_id, data)

    def get_meta(self, session_id: str) -> dict:
        return self.load_session_data(session_id).get("meta", {})

    def update_meta(self, session_id: str, **kwargs):
        with file_lock(self.get_memory_file(session_id)):
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
        legacy_file = self.data_path / f"{_encode_role_key(self.character_key)}DATA.json"
        if not legacy_file.exists():
            return
        current_file = self.get_memory_file(session_id)
        if current_file.exists():
            return
        try:
            with file_lock(current_file), file_lock(legacy_file):
                if current_file.exists():
                    return
                legacy_data, readable = load_json_ex(legacy_file, {})
                if not readable or not isinstance(legacy_data, dict):
                    raise ValueError("旧记忆不可读或格式错误")
                history = legacy_data.get("history", [])
                if not isinstance(history, list):
                    raise ValueError("旧记忆 history 格式错误")
                self.save_session_data(session_id, {
                    "character_name": legacy_data.get("character_name", self.character_name),
                    "history": history, "meta": legacy_data.get("meta", {})})
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
                    with open(f, 'r', encoding='utf-8') as fh:
                        data = json.load(fh)
                    role_name = str(data.get("character_key") or _memory_role_key(f.name))
                    session_id = str(data.get("session_id") or _memory_session_id(f.name))
                    character_name = self._display_name_of(
                        role_name, data.get("character_name"), role_display)
                    history = data.get("history", [])
                    last_sentence = ""
                    for msg in reversed(history):
                        if msg.get("role") == "assistant":
                            last_sentence = str(msg.get("content", ""))[:50]
                            break
                    if session_id.startswith("private_"):
                        sender_id = session_id[len("private_"):]
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
                        group_id = session_id[len("group_"):]
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
            role_key = _memory_role_key(filename)
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
            with file_lock(file_path):
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
                save_json(file_path, data)
                return {"success": True, "deleted_count": deleted_count}
        except Exception as e:
            return {"success": False, "error": str(e)}


_MEMORY_FILE_RE = re.compile(r'^[^\\/:*?"<>|]+?_(private|group)_[A-Za-z0-9_%~.\-]+\.json$')


def _is_memory_filename(name) -> bool:
    return bool(_MEMORY_FILE_RE.match(str(name or "")))


_SANITIZED_ID_SUFFIXES = (("_im_wechat", "@im.wechat"),
                          ("_chatroom", "@chatroom"),
                          ("_im_bot", "@im.bot"))


def restore_session_id(session_id: str) -> str:
    text = str(session_id or "")
    if "%" in text:
        return unquote(text)
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


def _memory_role_key(filename: str) -> str:
    match = re.match(r"^(.+?)_((?:private|group)_.+)\.json$", str(filename or ""))
    key = match.group(1) if match else ""
    if key.startswith("~r~"):
        try:
            return bytes.fromhex(key[3:]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            pass
    return key


def _encode_role_key(key):
    key = str(key or "default")
    if (re.search(r'[\\/:*?"<>|\r\n\t]', key) or "_private_" in key
            or "_group_" in key or key.startswith("~r~")):
        return "~r~" + key.encode("utf-8").hex()
    return key


def _memory_session_id(filename: str) -> str:
    match = re.match(r"^(.+?)_((?:private|group)_.+)\.json$", str(filename or ""))
    return restore_session_id(match.group(2)) if match else ""
