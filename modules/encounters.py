"""随机奇遇：每天按概率给角色安排一件小遭遇，由角色在后续对话里自然聊起。

遭遇由角色自己现场想（不预置池子）：每天只安排一次，角色开口讲过之后就消耗掉，
后续走向交回给对话本身。
"""
import random
import time
from pathlib import Path


class EncounterManager:
    def __init__(self, config, data_path):
        self.config = config
        self.file = Path(data_path) / "adventures.json"
        self.state: dict = {}
        self._load_failed = False
        self._load()

    def _load(self):
        from .jsonio import load_json_ex
        data, readable = load_json_ex(self.file, {})
        self._load_failed = not readable
        self.state = data if isinstance(data, dict) else {}

    def _save(self):
        if self._load_failed:
            print("奇遇记录本次未能读取，已跳过保存以免覆盖磁盘上的原有内容。")
            return
        try:
            from .jsonio import save_json
            save_json(self.file, self.state)
        except Exception as e:
            print(f"保存奇遇记录失败: {e}")

    def enabled(self) -> bool:
        raw = self.config.get("adventure_enabled", True)
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("true", "1", "yes", "y", "on", "是", "开启")

    def daily_chance(self) -> float:
        try:
            value = float(self.config.get("adventure_daily_chance", 0.2))
        except (TypeError, ValueError):
            value = 0.2
        return min(1.0, max(0.0, value))

    def should_roll(self, character_key: str, today: str) -> bool:
        """今天还没安排过、且概率命中时，该由角色现场想一件遭遇。"""
        if not self.enabled():
            return False
        current = self.state.get(character_key)
        if isinstance(current, dict) and current.get("date") == today:
            return False
        return random.random() < self.daily_chance()

    def mark_none(self, character_key: str, today: str):
        """今天没有遭遇：记一个空档期，避免同一天反复掷骰子。"""
        self.state[character_key] = {"date": today, "text": "", "used": True}
        self._save()

    def set_event(self, character_key: str, today: str, text: str):
        """把角色想好的遭遇记进当天。"""
        text = str(text or "").strip()
        if not text:
            self.mark_none(character_key, today)
            return
        self.state[character_key] = {"date": today, "text": text, "used": False}
        self._save()

    def take(self, character_key: str, today: str) -> str:
        """取走今天尚未讲过的奇遇；没有返回空串。"""
        current = self.state.get(character_key)
        if not isinstance(current, dict) or current.get("date") != today:
            return ""
        if current.get("used") or not str(current.get("text") or "").strip():
            return ""
        current["used"] = True
        self._save()
        return str(current.get("text") or "").strip()
