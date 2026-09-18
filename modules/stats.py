"""性能监控与统计：内存中的实时指标 + 数据库聚合查询。"""
import time
from collections import deque
from typing import Optional

from .database import DatabaseManager


class StatsManager:
    def __init__(self, db: DatabaseManager, start_time: float = None):
        self.db = db
        self.start_time = start_time or time.time()
        self.llm_times = deque(maxlen=100)   # 最近 100 次 LLM 耗时(ms)
        self.tts_times = deque(maxlen=200)   # 最近 200 次 TTS 耗时(ms)
        self.msg_count = 0
        self.err_count = 0
        self.active_sessions = set()

    def record_llm(self, ms: float):
        if ms and ms > 0:
            self.llm_times.append(float(ms))

    def record_tts(self, ms: float):
        if ms and ms > 0:
            self.tts_times.append(float(ms))

    def record_message(self, session_id: str, ok: bool = True):
        self.msg_count += 1
        if not ok:
            self.err_count += 1
        if session_id:
            self.active_sessions.add(session_id)

    def get_performance(self) -> dict:
        def stats(dq):
            if not dq:
                return {"count": 0, "avg": 0, "min": 0, "max": 0}
            return {"count": len(dq), "avg": round(sum(dq) / len(dq), 1),
                    "min": round(min(dq), 1), "max": round(max(dq), 1)}
        return {
            "uptime_seconds": int(time.time() - self.start_time),
            "llm": stats(self.llm_times),
            "tts": stats(self.tts_times),
            "messages_total": self.msg_count,
            "errors_total": self.err_count,
            "active_sessions": len(self.active_sessions),
        }

    def _range_bounds(self, range_key: str):
        """把前端传来的区间标识换算成 (start, end, days)。

        today/yesterday 以本地 0 点为界；7/14/30 为"含今天的最近 N 天"。
        """
        now = time.time()
        lt = time.localtime(now)
        today0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        if range_key == "today":
            return today0, now, 1
        if range_key == "yesterday":
            return today0 - 86400, today0, 1
        try:
            days = int(range_key)
        except (TypeError, ValueError):
            days = 30
        if days not in (7, 14, 30):
            days = 30
        return today0 - (days - 1) * 86400, now, days

    def get_stats(self, range_key: str = "30") -> dict:
        now = time.time()
        day = 86400
        start, end, days = self._range_bounds(range_key)
        out = {"range": {"key": range_key, "start": start, "end": end, "days": days}}
        try:
            out["totals"] = self.db.query_one(
                "SELECT COUNT(*) AS n, IFNULL(AVG(llm_ms),0) AS avg_llm, IFNULL(AVG(tts_ms),0) AS avg_tts,"
                " IFNULL(AVG(sentence_count),0) AS avg_sentences FROM interactions") or {}
            # 区间内汇总：回复数与 LLM/TTS/工具 API 调用次数
            out["range_totals"] = self.db.query_one(
                "SELECT COUNT(*) AS n, IFNULL(SUM(llm_calls),0) AS llm_calls,"
                " IFNULL(SUM(tts_calls),0) AS tts_calls, IFNULL(SUM(tool_calls),0) AS tool_calls,"
                " IFNULL(AVG(llm_ms),0) AS avg_llm, IFNULL(AVG(tts_ms),0) AS avg_tts"
                " FROM interactions WHERE ts >= ? AND ts < ?", (start, end)) or {}
            lt_now = time.localtime(now)
            today0 = time.mktime((lt_now.tm_year, lt_now.tm_mon, lt_now.tm_mday,
                                  0, 0, 0, 0, 0, -1))
            out["today"] = self.db.query_one(
                "SELECT COUNT(*) AS n FROM interactions WHERE ts >= ?", (today0,)) or {}
            out["per_day"] = self.db.query_all(
                "SELECT CAST((? - ts + 86399)/86400 AS INTEGER) AS day_idx, COUNT(*) AS n,"
                " IFNULL(SUM(llm_calls),0) AS llm_calls, IFNULL(SUM(tts_calls),0) AS tts_calls,"
                " IFNULL(SUM(tool_calls),0) AS tool_calls"
                " FROM interactions WHERE ts >= ? AND ts < ? GROUP BY day_idx ORDER BY day_idx",
                (today0, start, end))
            out["emotions"] = self.db.query_all(
                "SELECT emotion, COUNT(*) AS n FROM interactions"
                " WHERE ts >= ? AND ts < ? AND IFNULL(emotion,'') != ''"
                " GROUP BY emotion ORDER BY n DESC LIMIT 12", (start, end))
            out["emotion_trend"] = self.db.query_all(
                "SELECT CAST((? - ts + 86399)/86400 AS INTEGER) AS day_idx, emotion, COUNT(*) AS n"
                " FROM interactions WHERE ts >= ? AND ts < ? AND IFNULL(emotion,'') != ''"
                " GROUP BY day_idx, emotion ORDER BY day_idx",
                (today0, start, end))
            # 三个 TOP 榜同样只在所选区间内统计，之前无 WHERE 会与区间汇总对不上
            out["top_sessions"] = self.db.query_all(
                "SELECT session_id, MAX(session_type) AS session_type, COUNT(*) AS n,"
                " MAX(ts) AS last_ts FROM interactions WHERE ts >= ? AND ts < ?"
                " GROUP BY session_id ORDER BY n DESC LIMIT 10", (start, end))
            out["top_users"] = self.db.query_all(
                "SELECT user_id, IFNULL(MAX(user_name),'') AS user_name, COUNT(*) AS n FROM interactions"
                " WHERE ts >= ? AND ts < ? AND IFNULL(user_id,'') != ''"
                " GROUP BY user_id ORDER BY n DESC LIMIT 10", (start, end))
            out["top_characters"] = self.db.query_all(
                "SELECT character_key, COUNT(*) AS n FROM interactions"
                " WHERE ts >= ? AND ts < ? AND IFNULL(character_key,'') != ''"
                " GROUP BY character_key ORDER BY n DESC LIMIT 10", (start, end))
        except Exception as e:
            out["error"] = str(e)
        out["performance"] = self.get_performance()
        return out
