"""性能监控与统计：内存中的实时指标 + 数据库聚合查询。"""
import time
from collections import deque
from typing import Optional

from .database import DatabaseManager


class StatsManager:
    # 聚合粒度：区间本身按天，下钻到某天后按小时、再下钻到某小时后按分钟
    BUCKET_SECONDS = {"day": 86400, "hour": 3600, "minute": 60}

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

    def _range_bounds(self, range_key: str, table: str = "llm_usage"):
        """把前端传来的区间标识换算成 (start, end, days)。

        today/yesterday 以本地 0 点为界；7/14/30 为"含今天的最近 N 天"；
        all 从最早一条记录算起，并把起点对齐到当天的本地 0 点，
        这样"逐天"分桶的格子在图上就是真实的日期。
        """
        now = time.time()
        lt = time.localtime(now)
        today0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        if range_key == "today":
            return today0, now, 1
        if range_key == "yesterday":
            return today0 - 86400, today0, 1
        if range_key == "all":
            row = self.db.query_one(f"SELECT MIN(ts) AS first_ts FROM {table}") or {}
            first = float(row.get("first_ts") or 0) or today0
            lt_first = time.localtime(first)
            first = time.mktime((lt_first.tm_year, lt_first.tm_mon, lt_first.tm_mday,
                                 0, 0, 0, 0, 0, -1))
            return first, now, max(1, int((now - first) // 86400) + 1)
        try:
            days = int(range_key)
        except (TypeError, ValueError):
            days = 30
        if days not in (7, 14, 30):
            days = 30
        return today0 - (days - 1) * 86400, now, days

    def record_tokens(self, label: str, prompt_tokens: int, completion_tokens: int,
                      model: str = "", local: bool = False):
        """记一次 LLM 调用的 token 用量（带上打到哪个模型、是本地还是云端）。"""
        prompt_tokens = int(prompt_tokens or 0)
        completion_tokens = int(completion_tokens or 0)
        self.db.record_llm_usage(label, prompt_tokens, completion_tokens,
                                 prompt_tokens + completion_tokens, model=model,
                                 local=local)

    def get_token_stats(self, range_key: str = "30", window=None, granularity: str = "") -> dict:
        """区间内 token 用量：逐桶趋势 + 按用途/模型拆分，供统计面板画图与下钻。"""
        start, end, days = self._range_bounds(range_key, table="llm_usage")
        if window:
            start, end = float(window[0]), float(window[1])
            days = max(1, (int(end) - int(start) + 86399) // 86400)
        if not granularity:
            # 不足两天的窗口按天聚合只会得到一个点：改成按小时
            granularity = "hour" if (range_key in ("today", "yesterday") or days <= 1) \
                else "day"
        bucket = self.BUCKET_SECONDS.get(granularity, 86400)
        out = {"range": {"key": range_key, "start": start, "end": end, "days": days,
                         "granularity": granularity}}
        try:
            out["totals"] = self.db.query_one(
                "SELECT COUNT(*) AS calls, IFNULL(SUM(prompt_tokens),0) AS prompt,"
                " IFNULL(SUM(completion_tokens),0) AS completion,"
                " IFNULL(SUM(total_tokens),0) AS total,"
                " IFNULL(SUM(CASE WHEN local=1 THEN total_tokens ELSE 0 END),0) AS local,"
                " IFNULL(SUM(CASE WHEN local=0 THEN total_tokens ELSE 0 END),0) AS cloud"
                " FROM llm_usage WHERE ts >= ? AND ts < ?", (start, end)) or {}
            out["per_bucket"] = self.db.query_all(
                "SELECT CAST((ts - ?) / ? AS INTEGER) AS idx, COUNT(*) AS calls,"
                " IFNULL(SUM(prompt_tokens),0) AS prompt,"
                " IFNULL(SUM(completion_tokens),0) AS completion,"
                " IFNULL(SUM(total_tokens),0) AS total FROM llm_usage"
                " WHERE ts >= ? AND ts < ? GROUP BY idx ORDER BY idx",
                (start, bucket, start, end))
            out["by_label"] = self.db.query_all(
                "SELECT IFNULL(label,'') AS label, COUNT(*) AS calls,"
                " IFNULL(SUM(prompt_tokens),0) AS prompt,"
                " IFNULL(SUM(completion_tokens),0) AS completion,"
                " IFNULL(SUM(total_tokens),0) AS total FROM llm_usage"
                " WHERE ts >= ? AND ts < ? GROUP BY label ORDER BY total DESC", (start, end))
            # 按模型与云端/本地拆分：老记录没有模型名，归到「未知模型」
            out["by_model"] = self.db.query_all(
                "SELECT IFNULL(NULLIF(model,''),'未知模型') AS model, local, COUNT(*) AS calls,"
                " IFNULL(SUM(prompt_tokens),0) AS prompt,"
                " IFNULL(SUM(completion_tokens),0) AS completion,"
                " IFNULL(SUM(total_tokens),0) AS total FROM llm_usage"
                " WHERE ts >= ? AND ts < ? GROUP BY model, local ORDER BY total DESC",
                (start, end))
            out["all_total"] = self.db.query_one(
                "SELECT COUNT(*) AS calls, IFNULL(SUM(total_tokens),0) AS total FROM llm_usage") or {}
        except Exception as e:
            out["error"] = str(e)
        return out

    def get_stats(self, range_key: str = "30", window=None, granularity: str = "") -> dict:
        now = time.time()
        day = 86400
        start, end, days = self._range_bounds(range_key, table="interactions")
        # 点开某一天/某一小时下钻时，由前端给出具体窗口与粒度
        if window:
            start, end = float(window[0]), float(window[1])
            span = int(end) - int(start)
            days = max(1, (span + day - 1) // day)
        if not granularity:
            # 今天/昨天、以及不足两天的窗口（比如「总共」只跨了一天）只跨一天，
            # 按天聚合只会得到一个点：改成按小时
            granularity = "hour" if (range_key in ("today", "yesterday") or days <= 1) else "day"
        bucket = self.BUCKET_SECONDS.get(granularity, day)
        out = {"range": {"key": range_key, "start": start, "end": end, "days": days,
                         "granularity": granularity}}
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
            out["per_bucket"] = self.db.query_all(
                "SELECT CAST((ts - ?) / ? AS INTEGER) AS idx, COUNT(*) AS n,"
                " IFNULL(SUM(llm_calls),0) AS llm_calls, IFNULL(SUM(tts_calls),0) AS tts_calls,"
                " IFNULL(SUM(tool_calls),0) AS tool_calls"
                " FROM interactions WHERE ts >= ? AND ts < ? GROUP BY idx ORDER BY idx",
                (start, bucket, start, end))
            out["emotions"] = self.db.query_all(
                "SELECT emotion, COUNT(*) AS n FROM interactions"
                " WHERE ts >= ? AND ts < ? AND IFNULL(emotion,'') != ''"
                " GROUP BY emotion ORDER BY n DESC LIMIT 12", (start, end))
            out["emotion_trend"] = self.db.query_all(
                "SELECT CAST((ts - ?) / ? AS INTEGER) AS idx, emotion, COUNT(*) AS n"
                " FROM interactions WHERE ts >= ? AND ts < ? AND IFNULL(emotion,'') != ''"
                " GROUP BY idx, emotion ORDER BY idx",
                (start, bucket, start, end))
            # 三个 TOP 榜同样只在所选区间内统计，之前无 WHERE 会与区间汇总对不上
            # 会话榜的「用户」列取该会话里发言最多的那个人（群聊才有意义，
            # 私聊就是会话对方）
            out["top_sessions"] = self.db.query_all(
                "SELECT session_id, MAX(session_type) AS session_type, COUNT(*) AS n,"
                " MAX(ts) AS last_ts,"
                " (SELECT user_name FROM interactions AS u"
                "   WHERE u.session_id = interactions.session_id"
                "     AND u.ts >= ? AND u.ts < ? AND IFNULL(u.user_name,'') != ''"
                "   GROUP BY u.user_name ORDER BY COUNT(*) DESC, MAX(u.ts) DESC"
                "   LIMIT 1) AS user_name"
                " FROM interactions WHERE ts >= ? AND ts < ?"
                " GROUP BY session_id ORDER BY n DESC LIMIT 10", (start, end, start, end))
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
