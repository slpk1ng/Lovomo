# -*- coding: utf-8 -*-
"""会话记忆存取与会话 ID 净化/还原辅助（MemoryManager）。

自 main.py 原样搬迁（refactor_plan 第 9 节）：会话记忆文件的读写/删除/清理
（MemoryManager）与会话 ID 净化写法的还原辅助函数。
"""
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
        # 会话按当前激活的角色建档：用全局 character_key 的话，
        # 换了角色之后新对话仍会被算成默认角色的会话
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
                    # 只兜底"缺失"不够：history 是 dict、meta 是字符串、或 history 里
                    # 混进非 dict 条目时，后续 append / 下标赋值会每轮都抛异常，
                    # 表现为该会话的消息永远不落盘、机器人也不回复
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
        # 会话文件保存完整聊天记录，不在这里截断：送给模型的上下文另由
        # history_length / summary_max_history 等限制，截断只会让 WebUI 看不到旧记录
        # 走统一原子写：临时名唯一 + fsync，避免与 WebUI 线程的写盘互相覆盖
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

    def list_memories(self):
        memories = []
        if self.data_path.exists():
            for f in self.data_path.glob("*.json"):
                # 仅匹配角色会话记忆文件，排除 tools.json / scheduled_jobs.json 等功能数据
                if not _is_memory_filename(f.name):
                    continue
                try:
                    role_name = f.name.split("_")[0] if "_" in f.name else "未知"
                    with open(f, 'r', encoding='utf-8') as fh:
                        data = json.load(fh)
                    character_name = data.get("character_name", role_name)
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
                        # 微信/QQ 的 sender_id 是长串 openid，直接当名字很难看
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
                        "role_name": role_name
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
            return {"success": True, "character_name": data.get("character_name", "未知"), "history": data.get("history", [])}
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


# 会话记忆文件名：<角色>_<private|group>_<会话号>.json。
# data 目录下同时存放 webui_auth.json / user_profiles.json 等功能数据文件，
# 凡是"按目录批量读写"的地方都必须用它过滤，不能把功能数据一起卷进来。
_MEMORY_FILE_RE = re.compile(r'^[^\\/:*?"<>|]+?_(private|group)_[A-Za-z0-9_\-]+\.json$')


def _is_memory_filename(name) -> bool:
    return bool(_MEMORY_FILE_RE.match(str(name or "")))


# 记忆文件名里的会话ID被净化过（非 [A-Za-z0-9_-] 一律变成 _），openid 的 @ 与 . 都丢了。
# 同一个会话因此有两种写法：收到消息时是 …@im.wechat，从文件名反推出来的是 …_im_wechat。
_SANITIZED_ID_SUFFIXES = (("_im_wechat", "@im.wechat"),
                          ("_chatroom", "@chatroom"),
                          ("_im_bot", "@im.bot"))


def restore_session_id(session_id: str) -> str:
    """把净化过的会话ID还原成运行时那一种写法（本来就正常的原样返回）。

    两种写法混用会把同一个会话当成两个：按会话选连接时查不到（主动消息落到默认的
    NapCat 上，报「无法获取用户信息」），解析发送目标时还会把 openid 从 _ 处截断。
    """
    text = str(session_id or "")
    for safe, real in _SANITIZED_ID_SUFFIXES:
        if text.endswith(safe):
            return text[: -len(safe)] + real
    return text


def merge_restored_keys(raw, combine=max):
    """把状态字典里净化过的会话ID并回运行时写法；同一个会话只留一条，按 combine 合并。"""
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
