"""Bounded encrypted storage for queued WeChat messages."""
import hashlib
import json
import math
import time
from pathlib import Path

from .jsonio import save_json
from .security import _decrypt_value, _encrypt_value

MAX_TARGETS = 100
MAX_TEXT = 4000
MAX_MESSAGES_PER_TARGET = 3
MAX_FILE_SIZE = 8 * 1024 * 1024
MAX_AGE = 7 * 24 * 60 * 60


class WeChatOutbox:
    def __init__(self, root, channel, account):
        key = hashlib.sha256(json.dumps([channel, account]).encode()).hexdigest()
        folder = Path(root) / "wechat_outbox"
        # Older files can contain sends that were already accepted without a receipt.
        self.legacy_path = folder / f"{key}.json"
        self.path = folder / f"{key}.v2.json"
        self.writable = True

    def load(self):
        try:
            with self.path.open("rb") as file:
                stored = file.read(MAX_FILE_SIZE + 1)
            if len(stored) > MAX_FILE_SIZE:
                raise ValueError("待补发文件超限")
            raw = json.loads(stored)
            if not isinstance(raw, dict):
                raise ValueError("待补发文件不可读")
            text = _decrypt_value(raw.get("encrypted"))
            if not text:
                raise ValueError("无法解密待补发文件")
            snapshot = json.loads(text)
            cutoff = time.time() - MAX_AGE
            if not isinstance(snapshot, dict) or len(snapshot) > MAX_TARGETS:
                raise ValueError("待补发会话格式错误")
            active = {}
            for target, entries in snapshot.items():
                if not isinstance(entries, list) or len(entries) > MAX_MESSAGES_PER_TARGET:
                    raise ValueError("待补发条目格式错误")
                for text, client_id, added in entries:
                    if (not isinstance(text, str) or not 0 < len(text) <= MAX_TEXT
                            or not isinstance(client_id, str) or not client_id
                            or type(added) not in (int, float) or not math.isfinite(added)):
                        raise ValueError("待补发条目格式错误")
                    if added >= cutoff:
                        active.setdefault(target, []).append([text, client_id, added])
            return active
        except FileNotFoundError:
            return {}
        except (ValueError, OSError, TypeError, OverflowError):
            self.writable = False
            print("[微信补发] 待发送文件读取失败，保留原文件并改用临时队列")
            return {}

    def save(self, entries):
        if not self.writable:
            return False
        try:
            payload = _encrypt_value(json.dumps(entries, ensure_ascii=False))
            if len(payload) + 64 > MAX_FILE_SIZE:
                raise ValueError("待补发文件超限")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            save_json(self.path, {"encrypted": payload})
            return True
        except (OSError, ValueError):
            print("[微信补发] 写入失败，本次排队状态仅保存在内存")
            return False
