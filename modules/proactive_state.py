# -*- coding: utf-8 -*-
"""主动消息状态簇的落盘/加载/播种/遗忘。

自 main.py 原样搬迁（refactor_plan 第 10 节）：只含函数；7 个主动消息状态
全局本体（last_interaction、last_user_activity、proactive_counts、
proactive_pending、proactive_awaiting、last_proactive_sent、
_proactive_state_date）在 modules.app_context，本模块经 app_context 读写。
"""
import json
import time
from pathlib import Path

from modules import app_context
from modules.memory_store import restore_session_id, merge_restored_keys, _memory_session_id

_PROACTIVE_STATE_FILE = "proactive_state.json"


def _proactive_state_path() -> Path:
    return Path(app_context.memory_manager.data_path) / _PROACTIVE_STATE_FILE


def load_proactive_state():
    """载入主动消息状态（当日计数 + 各会话用户最后发言时间）。

    历史 bug：last_interaction 只在"收到消息"时写入内存，程序重启后为空，
    导致重启后闲置会话永远不会被主动消息检查命中 —— 表现就是"从来不主动发消息"。
    """
    app_context._proactive_state_date = time.strftime("%Y-%m-%d")
    path = _proactive_state_path()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"读取主动消息状态失败（忽略，重新统计）: {e}")
        return
    # "等待用户回复"标记跨天仍然有效（用户没回复就一直没有主动资格），先于当日计数恢复
    for key in (data.get("awaiting") or []):
        app_context.proactive_awaiting.add(restore_session_id(str(key)))
    # 已排定的主动消息发送时刻一并恢复（过期的丢弃，由闲置检查重新排期）
    for key, target in merge_restored_keys(data.get("pending") or {}).items():
        try:
            target_ts = float(target)
        except (TypeError, ValueError):
            continue
        if str(data.get("date", "")) == app_context._proactive_state_date and target_ts > time.time():
            app_context.proactive_pending[str(key)] = target_ts
    # 用户最后发言时间与上次主动发送时间存的都是绝对时间戳，不是"当日"数据：
    # 跨天也要恢复，否则重启后闲置计时归零，会立刻重复主动搭话
    def _restore_ts(raw, target: dict):
        if not isinstance(raw, dict):
            return
        for k, v in merge_restored_keys(raw).items():
            try:
                target[str(k)] = float(v)
            except (TypeError, ValueError):
                continue

    _restore_ts(data.get("user_activity"), app_context.last_user_activity)
    _restore_ts(data.get("last_proactive_sent"), app_context.last_proactive_sent)
    if str(data.get("date", "")) != app_context._proactive_state_date:
        print("主动消息状态为往日数据，已重置当日计数。")
        return
    counts = data.get("counts", {})
    if isinstance(counts, dict):
        # 键是「日期|会话ID」，还原后缀不影响前面的日期
        for k, v in merge_restored_keys(counts).items():
            if str(k).startswith(app_context._proactive_state_date):
                try:
                    app_context.proactive_counts[str(k)] = int(v)
                except (TypeError, ValueError):
                    continue
    if app_context.proactive_counts or app_context.last_user_activity or app_context.proactive_awaiting:
        print(f"已载入主动消息状态：{len(app_context.last_user_activity)} 个会话记录，"
              f"今日已发送 {sum(app_context.proactive_counts.values())} 条"
              + (f"，{len(app_context.proactive_awaiting)} 个会话等待用户回复。" if app_context.proactive_awaiting else ""))


def save_proactive_state():
    """原子写盘，避免程序重启后当日上限失效、闲置时间丢失。"""
    if app_context.memory_manager is None:
        return
    try:
        # 实际生效的日期必须先取出来：日期为空时 startswith("") 恒真，
        # 会把往日的计数一并算到当天头上
        today = app_context._proactive_state_date or time.strftime("%Y-%m-%d")
        payload = {
            "date": today,
            "counts": {k: v for k, v in app_context.proactive_counts.items()
                       if str(k).startswith(today)},
            "user_activity": app_context.last_user_activity,
            "awaiting": sorted(app_context.proactive_awaiting),
            "pending": {k: v for k, v in app_context.proactive_pending.items()
                        if isinstance(v, (int, float))},
        }
        from modules.jsonio import save_json
        save_json(_proactive_state_path(), payload)
    except Exception as e:
        print(f"保存主动消息状态失败: {type(e).__name__}: {e}")


def _session_last_user_ts(session_id: str) -> float:
    """从会话历史里取用户最后一次发言时间（重启后恢复闲置计时的依据）。"""
    try:
        data = app_context.memory_manager.load_session_data(session_id) if app_context.memory_manager else {}
    except Exception:
        return 0.0
    for msg in reversed(data.get("history", []) or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        try:
            ts = float(msg.get("timestamp") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts > 0:
            return ts
    return 0.0


def seed_proactive_sessions():
    """启动时把所有历史会话纳入主动消息候选，避免"重启后再也不主动说话"。"""
    if app_context.memory_manager is None:
        return
    seeded = 0
    for item in app_context.memory_manager.list_memories():
        session_id = _memory_session_id(item.get("filename", ""))
        if not session_id:
            continue
        ts = _session_last_user_ts(session_id)
        if ts <= 0:
            continue
        prev = app_context.last_user_activity.get(session_id, 0.0)
        if ts > prev:
            app_context.last_user_activity[session_id] = ts
            seeded += 1
        app_context.last_interaction.setdefault(session_id, ts)
    if seeded:
        idle_min = app_context.global_config.get("proactive_idle_minutes", 30) if app_context.global_config else 30
        print(f"已恢复 {seeded} 个会话的闲置计时（超过 {idle_min} 分钟未互动即纳入主动消息候选）。")
    save_proactive_state()


def session_memory_exists(session_id: str) -> bool:
    """该会话的记忆文件是否还在。

    会话被删除后，闲置计时与主动消息计数仍留在内存里，到点就会继续往
    已删除的对话发主动消息；发送前必须确认会话本身还存在。
    记忆实现没有 get_memory_file 接口时无法判定，按存在处理。
    """
    if app_context.memory_manager is None:
        return False
    getter = getattr(app_context.memory_manager, "get_memory_file", None)
    if getter is None:
        return True
    try:
        return Path(getter(session_id)).exists()
    except Exception as e:
        print(f"检查会话 {session_id} 记忆文件失败（按存在处理）: {type(e).__name__}: {e}")
        return True


def forget_proactive_session(session_id: str):
    """会话被删除时一并清掉它的主动消息状态，避免删完还继续被搭话。"""
    forgotten = (session_id in app_context.last_user_activity or session_id in app_context.last_proactive_sent
                 or session_id in app_context.proactive_pending or session_id in app_context.proactive_awaiting)
    app_context.last_user_activity.pop(session_id, None)
    app_context.last_proactive_sent.pop(session_id, None)
    app_context.last_interaction.pop(session_id, None)
    app_context.proactive_pending.pop(session_id, None)
    app_context.proactive_awaiting.discard(session_id)
    for key in [k for k in app_context.proactive_counts if str(k).endswith(f"|{session_id}")]:
        app_context.proactive_counts.pop(key, None)
    if forgotten:
        print(f"会话 {session_id} 已删除，主动消息状态一并清除。")
        save_proactive_state()
