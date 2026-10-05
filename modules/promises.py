"""承诺追踪：角色在对话里答应过用户的事记下来，之后由每日检查主动兑现。

承诺的识别搭在回复定稿之后：先用触发词粗筛，命中才发一次 LLM 提取，
避免每轮回复都多一次调用。兑现走主动消息通道，发完即标记完成。
"""
import re
import time
from pathlib import Path
from typing import Optional

from .llm_helpers import RoleContext, chat_once, extract_json

# 角色台词里出现这些字样才值得跑一次承诺提取
PROMISE_HINT_RE = re.compile(r"下次|改天|回头|以后|之后|答应你|保证|明天|后天|周末|择日")

EXTRACT_SYSTEM = (
    "你负责从角色的台词里找出角色自己向对方许下的承诺。"
    "只有角色明确答应「自己之后要做某件具体的事」才算承诺（例如自己讲故事、陪对方逛街、给对方做料理）；"
    "客套话、随口一提或没有具体内容的都不算。"
    "以下都不算角色的承诺，一律输出 null："
    "① 角色在提醒、催促或质问对方曾经答应过的事（对方说好要做什么、让对方别忘了、拿旧账挤兑对方）；"
    "② 角色转述、引用对方说过的话，哪怕里面带着「答应」「说好」「约好」这类字眼；"
    "③ 约定里角色是被履行的一方（对方要带给角色、陪角色、给角色做）——那是对方的承诺，不是角色的。"
    "拿不准就不算。"
    '只输出一个 JSON 对象：{"promise": "用角色台词的原话概括角色自己要做的那件事"} 或 {"promise": null}，'
    "不要输出其它文字。"
)


class PromiseManager:
    def __init__(self, data_path):
        self.file = Path(data_path) / "promises.json"
        self.items: list = []
        self._load_failed = False
        self._load()

    # ---------------- 持久化 ----------------
    def _load(self):
        from .jsonio import load_json_ex
        data, readable = load_json_ex(self.file, [])
        self._load_failed = not readable
        self.items = data if isinstance(data, list) else []

    def _save(self):
        if self._load_failed:
            print("承诺记录本次未能读取，已跳过保存以免覆盖磁盘上的原有内容。")
            return
        try:
            from .jsonio import save_json
            save_json(self.file, self.items)
        except Exception as e:
            print(f"保存承诺记录失败: {e}")

    # ---------------- 记录 ----------------
    def has_pending(self, character_key: str, content: str) -> bool:
        text = str(content or "").strip()
        return any(p.get("status") == "pending"
                   and p.get("character_key") == character_key
                   and str(p.get("content", "")).strip() == text
                   for p in self.items)

    def add(self, character_key: str, session_id: str, user_id: str,
            content: str) -> Optional[dict]:
        text = str(content or "").strip()[:120]
        if not text or self.has_pending(character_key, text):
            return None
        promise = {"character_key": character_key, "session_id": session_id,
                   "user_id": user_id, "content": text,
                   "created": time.time(), "status": "pending"}
        self.items.append(promise)
        self._save()
        return promise

    def due(self, min_age_days: float, limit: int) -> list:
        """到期未兑现的承诺（创建时间早于 N 天前），每次检查每条只取一次由调用方控制。"""
        cutoff = time.time() - max(0.0, float(min_age_days)) * 86400
        out = [p for p in self.items
               if p.get("status") == "pending" and float(p.get("created") or 0) <= cutoff]
        return out[:max(0, int(limit))]

    def mark_fulfilled(self, promise: dict):
        promise["status"] = "fulfilled"
        promise["fulfilled_at"] = time.time()
        # 只保留最近 200 条，兑现已久的历史没有留档价值
        self.items = self.items[-200:]
        self._save()

    def drop_session(self, session_id: str) -> int:
        """清掉某个会话的承诺；返回清掉几条。"""
        keep = [p for p in self.items if p.get("session_id") != session_id]
        removed = len(self.items) - len(keep)
        if removed:
            self.items = keep
            self._save()
        return removed


async def extract_promise(ctx: RoleContext, reply_text: str) -> Optional[str]:
    """从角色回复里提取承诺内容；没有承诺或调用失败返回 None。"""
    if not str(reply_text or "").strip():
        return None
    messages = [{"role": "system", "content": EXTRACT_SYSTEM},
                {"role": "user", "content": f"角色台词：{reply_text}\n请判断并输出 JSON。"}]
    result = await chat_once(ctx, messages)
    obj = extract_json(result.get("content") or "")
    if isinstance(obj, dict):
        promise = str(obj.get("promise") or "").strip()
        return promise or None
    return None
