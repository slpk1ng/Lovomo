"""会话回忆：把「未了话题」置顶，并按需检索更早的相关往事注入。

- 未了话题：定期让模型维护一份「提起过、但还没结清」的清单，每轮带进提示词，
  解决「说好要去海边、聊到别处就忘了」这类问题；
- 相关往事：历史按话题段建索引，每轮用当前消息检索最相关的几段注入，
  解决「用户提起旧事、角色想不起来细节」。

检索有两条路：embedding 走语义（换说法也能命中，需要配嵌入模型）；
lexical 走字符重合（零依赖、离线可用，但换了说法就抓不住）。
"""
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from .jsonio import load_json_ex, save_json
from .llm_helpers import RoleContext, chat_once, extract_json, speaker_labeled_lines

# 话题分段：相邻消息间隔超过这个时长就切一段，单段条数也封顶
SEGMENT_GAP_SECONDS = 30 * 60
SEGMENT_MAX_MESSAGES = 8
# 注入块的默认字数上限
DEFAULT_MAX_CHARS = 600
# 相似度下限：低于它宁可不注入，也不要塞一堆无关内容把话题带偏。
# 字符重合的分数受查询长度稀释（一个关键词命中也只有 1/查询长度），所以它的下限
# 只是「至少要有实打实的二元组重合」，真正的取舍交给 top-k 排序。
MIN_SIMILARITY_EMBEDDING = 0.35
MIN_SIMILARITY_LEXICAL = 0.05
# 检索结果的时间跨度：相隔小于这个时长的段算「同一个话题时段」，只取其中一段。
# 相似度检索容易把同一段讨论里的几条近义段全捞上来（都在「聊这件事」，
# 却没有一段是「这件事本身」），按时间拉开才覆盖得到更早的往事。
RECALL_TIME_SPREAD_SECONDS = SEGMENT_GAP_SECONDS
# 未了话题：每隔几轮维护一次、最多留几条、每条多长
OPEN_TOPICS_EVERY_N = 3
OPEN_TOPICS_MAX = 5
OPEN_TOPICS_MAX_CHARS = 40
OPEN_TOPICS_HISTORY_LINES = 20

OPEN_TOPICS_SYSTEM = (
    "你负责维护一份「还没结清的事」清单：两人在对话里提起过、但还没做完或还没说定的事"
    "（约好要去哪玩却还没定时间、说好一起去某家店、答应了还没兑现的事）。"
    "已经做完、已经取消、或只是随口一提没有下文的不算。"
    '只输出一个 JSON 对象：{"topics": ["...", "..."]}，'
    "每条一句话、不超过 40 字、写清「要做什么」；没有就输出 {\"topics\": []}，不要输出其它文字。"
)

OPEN_TOPICS_HEADER = (
    "【还没结清的事】下面这些是你和主人之前提起过、到现在还没做完或还没说定的事。"
    "它们不是主人刚刚说的话；如果自然可以顺口提一句，但不要凭空宣布已经做过，"
    "也不要硬把话题拽回去。\n"
)

RECALL_HEADER = (
    "【相关往事】以下是你们更早聊过、与当前话题相关的片段。"
    "它们不是刚刚发生的事，也不是主人现在说的话，只是供你回忆细节；"
    "如果主人没有接着这个话题，就不要硬把话题拽回去。\n"
)


def split_segments(history: list, gap_seconds: float = SEGMENT_GAP_SECONDS,
                   max_messages: int = SEGMENT_MAX_MESSAGES) -> List[list]:
    """把历史切成话题段：间隔过久或条数够多就切一刀。"""
    segments: List[list] = []
    current: list = []
    for msg in history or []:
        if not isinstance(msg, dict) or msg.get("role") not in ("user", "assistant"):
            continue
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        ts = float(msg.get("timestamp") or 0)
        if current and (ts - current[-1]["ts"] > gap_seconds
                        or len(current) >= max_messages):
            segments.append(current)
            current = []
        current.append({"ts": ts, "role": msg.get("role"), "content": content,
                        "speaker": str(msg.get("speaker") or ""),
                        "sender_name": str(msg.get("sender_name") or "")})
    if current:
        segments.append(current)
    return segments


def render_segment(messages: list) -> str:
    """一段话题渲染成给模型看的转录（时间只标在段首）。"""
    lines = []
    for i, msg in enumerate(messages):
        who = msg["speaker"] if msg["role"] == "assistant" \
            else (msg["sender_name"] or "对方")
        head = ""
        if i == 0 and msg["ts"]:
            head = f"[{time.strftime('%m-%d %H:%M', time.localtime(msg['ts']))}] "
        lines.append(f"{head}{who}：{msg['content']}")
    return "\n".join(lines)


def _bigrams(text: str) -> set:
    plain = re.sub(r"[\s\W_]+", "", str(text or ""), flags=re.UNICODE)
    if len(plain) < 2:
        return {plain} if plain else set()
    return {plain[i:i + 2] for i in range(len(plain) - 1)}


def lexical_score(query: str, text: str) -> float:
    """查询的字符二元组在往事段里的覆盖率（0~1）。

    用覆盖率而不是 Jaccard：往事段通常比当前消息长得多，Jaccard 会被段长稀释，
    同一个查询在长段和短段上得到的分完全不可比。
    """
    q = _bigrams(query)
    if not q:
        return 0.0
    return len(q & _bigrams(text)) / len(q)


def _normalize(vec) -> np.ndarray:
    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm else arr


class HistoryRecallManager:
    """会话回忆：话题段索引 + 检索注入 + 未了话题清单。

    索引按会话分文件存放，且**只索引已经结束的话题段**（最后一段还在进行中，
    既在最近窗口里、每轮又会变长，索引它只会反复重算）。
    """

    def __init__(self, config, data_path, embedder=None):
        self.config = config
        self.dir = Path(data_path) / "history_recall"
        self.embedder = embedder          # 复用 RAGManager.embed_texts
        self._cache: Dict[str, dict] = {}
        self._index_load_failed: set = set()
        self._open_file = Path(data_path) / "open_topics.json"
        self._open: dict = {}
        self._open_load_failed = False
        self._turns: Dict[str, int] = {}
        self._load_open()

    # ---------------- 开关 ----------------
    def enabled(self) -> bool:
        return bool(self.config.get("history_recall_enabled", True))

    def _enabled(self) -> bool:
        return self.enabled()

    def _mode(self) -> str:
        mode = str(self.config.get("history_recall_mode", "embedding") or "embedding")
        return "lexical" if mode.strip().lower() in ("lexical", "word", "字符", "词面") \
            else "embedding"

    # ---------------- 未了话题持久化 ----------------
    def _load_open(self):
        data, readable = load_json_ex(self._open_file, {})
        self._open_load_failed = not readable
        self._open = data if isinstance(data, dict) else {}

    def _save_open(self):
        if self._open_load_failed:
            print("未了话题本次未能读取，已跳过保存以免覆盖磁盘上的原有内容。")
            return
        try:
            save_json(self._open_file, self._open)
        except Exception as e:
            print(f"保存未了话题失败: {e}")

    def open_topics_block(self, session_id: str) -> str:
        if not self._enabled():
            return ""
        items = self._open.get(str(session_id or "")) or []
        lines = [f"- {str(i.get('text') or '').strip()}"
                 for i in items if str(i.get("text") or "").strip()]
        return OPEN_TOPICS_HEADER + "\n".join(lines) if lines else ""

    async def update_open_topics(self, ctx: RoleContext, session_id: str, history: list):
        """定期让模型把未了话题清单更新一遍；失败就沿用旧清单。"""
        sid = str(session_id or "")
        if not self._enabled() or not sid:
            return
        self._turns[sid] = self._turns.get(sid, 0) + 1
        if self._turns[sid] % max(1, OPEN_TOPICS_EVERY_N) != 0:
            return
        valid = [m for m in (history or []) if isinstance(m, dict)
                 and m.get("role") in ("user", "assistant")
                 and str(m.get("content") or "").strip()]
        if len(valid) < 4:
            return
        current = [str(i.get("text") or "") for i in (self._open.get(sid) or [])]
        recent = speaker_labeled_lines(valid, limit=OPEN_TOPICS_HISTORY_LINES)
        prompt = (f"现有清单：{json.dumps(current, ensure_ascii=False)}\n\n"
                  f"最近对话：\n{recent}\n\n"
                  "请结合最近对话增删，输出更新后的完整清单。")
        try:
            result = await chat_once(ctx, [{"role": "system", "content": OPEN_TOPICS_SYSTEM},
                                           {"role": "user", "content": prompt}])
        except Exception as e:
            print(f"未了话题更新失败（沿用旧清单）: {type(e).__name__}: {e}")
            return
        obj = extract_json(str(result.get("content") or ""))
        if not isinstance(obj, dict) or not isinstance(obj.get("topics"), list):
            return
        topics = []
        for item in obj["topics"]:
            text = str(item or "").strip()[:OPEN_TOPICS_MAX_CHARS]
            if text and text not in topics:
                topics.append(text)
        self._open[sid] = [{"text": t, "updated": time.time()}
                           for t in topics[:OPEN_TOPICS_MAX]]
        self._save_open()
        if self._open[sid]:
            print(f"未了话题已更新（{len(self._open[sid])} 条）："
                  + "；".join(i["text"] for i in self._open[sid]))
        else:
            print("未了话题已清空：之前提起的事都结清了。")

    def drop_session(self, session_id: str) -> int:
        sid = str(session_id or "")
        if sid not in self._open:
            return 0
        self._open.pop(sid, None)
        self._save_open()
        index = self.dir / f"{self._key(sid)}.json"
        try:
            index.unlink(missing_ok=True)
        except OSError:
            pass
        self._cache.pop(sid, None)
        return 1

    # ---------------- 索引 ----------------
    @staticmethod
    def _key(session_id: str) -> str:
        return hashlib.sha1(str(session_id or "").encode("utf-8")).hexdigest()[:16]

    def _load_index(self, session_id: str) -> dict:
        sid = str(session_id or "")
        if sid in self._cache:
            return self._cache[sid]
        data, readable = load_json_ex(self.dir / f"{self._key(sid)}.json", {})
        if not readable:
            self._index_load_failed.add(sid)
            data = {}
        if not isinstance(data, dict) or str(data.get("session_id") or "") != sid:
            data = {}
        segments = data.get("segments")
        vectors = data.get("vectors")
        index = {"session_id": sid,
                 "segments": segments if isinstance(segments, list) else [],
                 "vectors": vectors if isinstance(vectors, list) else []}
        self._cache[sid] = index
        return index

    def _save_index(self, session_id: str):
        sid = str(session_id or "")
        if sid in self._index_load_failed:
            print("回忆索引本次未能读取，已跳过保存以免覆盖磁盘上的原有内容。")
            return
        index = self._cache.get(sid)
        if index is None:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            save_json(self.dir / f"{self._key(sid)}.json", index)
        except Exception as e:
            print(f"保存回忆索引失败: {e}")

    async def refresh(self, session_id: str, history: list):
        """把已经结束的话题段增量索引一遍（正在进行的最后一段不索引）。"""
        if not self._enabled():
            return
        sid = str(session_id or "")
        groups = split_segments(history)
        if len(groups) <= 1:
            return
        done = groups[:-1]
        texts = [render_segment(g) for g in done]
        index = self._load_index(sid)
        known = {}
        for entry, vec in zip(index["segments"], index["vectors"]):
            if isinstance(entry, dict):
                known[str(entry.get("text") or "")] = vec
        todo = [t for t in texts if t and t not in known]
        if todo and self._mode() == "embedding" and self.embedder is not None:
            try:
                fresh = await self.embedder.embed_texts(todo)
            except Exception as e:
                fresh = None
                print(f"回忆索引向量化失败（下次再试）: {type(e).__name__}: {e}")
            if fresh is not None and len(fresh) == len(todo):
                for text, vec in zip(todo, fresh):
                    # 嵌入服务回的是 numpy 数组，元素是 float32，直接落盘会 JSON 序列化失败
                    known[text] = [float(x) for x in vec]
        index["segments"] = [{"text": t, "ts": float(g[0]["ts"] or 0)}
                             for t, g in zip(texts, done)]
        index["vectors"] = [known.get(t) for t in texts]
        self._save_index(sid)
        if todo:
            print(f"回忆索引已更新：{sid} 共 {len(texts)} 段"
                  f"（新增 {len(todo)} 段，检索方式：{self._mode()}）")

    # ---------------- 检索 ----------------
    async def recall(self, session_id: str, query: str, top_k: int = None,
                     exclude_after_ts: float = 0.0) -> List[dict]:
        if not self._enabled() or not str(query or "").strip():
            return []
        index = self._load_index(str(session_id or ""))
        segments = index["segments"]
        if not segments:
            return []
        try:
            top_k = max(1, int(top_k or self.config.get("history_recall_top_k", 3) or 3))
        except (TypeError, ValueError):
            top_k = 3
        scored: List[tuple] = []
        floor = MIN_SIMILARITY_LEXICAL
        if self._mode() == "embedding" and self.embedder is not None \
                and any(v for v in index["vectors"]):
            try:
                query_vec = await self.embedder.embed_texts([query])
            except Exception as e:
                query_vec = None
                print(f"回忆检索向量化失败，本次改用字符重合: {type(e).__name__}: {e}")
            if query_vec is not None and len(query_vec):
                q = _normalize(query_vec[0])
                for entry, vec in zip(segments, index["vectors"]):
                    if not vec:
                        continue
                    scored.append((float(_normalize(vec) @ q), entry))
                floor = MIN_SIMILARITY_EMBEDDING
        if not scored:
            # 嵌入不可用（没配模型、维度不符、还没建过向量）就退回字符重合，
            # 别让整个回忆功能因为嵌入这条路的依赖而失效
            scored = [(lexical_score(query, str(e.get("text") or "")), e)
                      for e in segments]
        hits = []
        for score, entry in scored:
            if score < floor:
                continue
            ts = float(entry.get("ts") or 0)
            if exclude_after_ts and ts >= exclude_after_ts:
                continue
            hits.append({"sim": round(score, 3), "ts": ts,
                         "text": str(entry.get("text") or "")})
        hits.sort(key=lambda h: -h["sim"])
        picked: List[dict] = []
        for hit in hits:
            if hit["ts"] and any(p["ts"] and abs(hit["ts"] - p["ts"]) < RECALL_TIME_SPREAD_SECONDS
                                 for p in picked):
                continue
            picked.append(hit)
            if len(picked) >= top_k:
                break
        # 按时间从早到晚注入：往事是按时间发生的，「第几个 / 最开始」这类问题要靠先后推
        picked.sort(key=lambda h: h["ts"])
        return picked

    def build_context(self, hits: List[dict], max_chars: int = DEFAULT_MAX_CHARS) -> str:
        lines, used = [], 0
        for hit in hits or []:
            text = str(hit.get("text") or "").strip()
            if not text or used + len(text) > max_chars:
                continue
            lines.append(text)
            used += len(text)
        return RECALL_HEADER + "\n---\n".join(lines) if lines else ""

    async def recall_context(self, session_id: str, query: str,
                             exclude_after_ts: float = 0.0) -> str:
        """检索并拼成可直接注入的段落；出错一律返回空串。"""
        try:
            hits = await self.recall(session_id, query, exclude_after_ts=exclude_after_ts)
        except Exception as e:
            print(f"回忆检索失败（忽略）: {type(e).__name__}: {e}")
            return ""
        block = self.build_context(hits)
        if block:
            print(f"回忆检索：命中 {len(hits)} 段往事"
                  f"（{', '.join(str(h['sim']) for h in hits)}），已注入本轮上下文。")
        return block
