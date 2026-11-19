import hashlib
import json
import re
import time
import threading
import copy
from functools import wraps
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from .jsonio import load_json_ex, save_json
from .llm_helpers import RoleContext, chat_once, extract_json, speaker_labeled_lines


def _locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call

SEGMENT_GAP_SECONDS = 30 * 60
SEGMENT_MAX_MESSAGES = 8
# 注入块的默认字数上限
DEFAULT_MAX_CHARS = 600
MIN_SIMILARITY_EMBEDDING = 0.35
MIN_SIMILARITY_LEXICAL = 0.05
RECALL_TIME_SPREAD_SECONDS = SEGMENT_GAP_SECONDS
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
    "【还没结清的事】下面这些是你和用户之前提起过、到现在还没做完或还没说定的事。"
    "它们不是用户刚刚说的话；如果自然可以顺口提一句，但不要凭空宣布已经做过，"
    "也不要硬把话题拽回去。\n"
)

RECALL_HEADER = (
    "【相关往事】以下是你们更早聊过、与当前话题相关的片段。"
    "它们不是刚刚发生的事，也不是用户现在说的话，只是供你回忆细节；"
    "如果用户没有接着这个话题，就不要硬把话题拽回去。\n"
)


def split_segments(history: list, gap_seconds: float = SEGMENT_GAP_SECONDS,
                   max_messages: int = SEGMENT_MAX_MESSAGES) -> List[list]:
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
    q = _bigrams(query)
    if not q:
        return 0.0
    return len(q & _bigrams(text)) / len(q)


def _normalize(vec) -> np.ndarray:
    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm else arr


class HistoryRecallManager:

    def __init__(self, config, data_path, embedder=None, role_key=""):
        self.config = config
        self.data_path = Path(data_path)
        self.role_key = str(role_key or "")
        if self.role_key:
            data_path = self.data_path / "role_recall" / hashlib.sha256(self.role_key.encode()).hexdigest()
        self.dir = Path(data_path) / "history_recall"
        self.embedder = embedder          # 复用 RAGManager.embed_texts
        self._cache: Dict[str, dict] = {}
        self._index_load_failed: set = set()
        self._open_file = Path(data_path) / "open_topics.json"
        self._open: dict = {}
        self._open_load_failed = False
        self._turns: Dict[str, int] = {}
        self._lock = threading.RLock()
        self._epochs = {}
        self._refresh_versions = {}
        self._topic_versions = {}
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

    @_locked
    def _save_open(self):
        if self._open_load_failed:
            print("未了话题本次未能读取，已跳过保存以免覆盖磁盘上的原有内容。")
            return False
        try:
            save_json(self._open_file, self._open)
            return True
        except Exception as e:
            print(f"保存未了话题失败: {e}")
            return False

    @_locked
    def open_topics_block(self, session_id: str) -> str:
        if not self._enabled():
            return ""
        items = self._open.get(str(session_id or "")) or []
        lines = [f"- {str(i.get('text') or '').strip()}"
                 for i in items if str(i.get("text") or "").strip()]
        return OPEN_TOPICS_HEADER + "\n".join(lines) if lines else ""

    async def update_open_topics(self, ctx: RoleContext, session_id: str, history: list):
        sid = str(session_id or "")
        if not self._enabled() or not sid:
            return
        with self._lock:
            epoch = self._epochs.get(sid, 0)
            self._turns[sid] = self._turns.get(sid, 0) + 1
            if self._turns[sid] % max(1, OPEN_TOPICS_EVERY_N) != 0:
                return
            version = self._topic_versions[sid] = self._topic_versions.get(sid, 0) + 1
            current = [str(i.get("text") or "") for i in (self._open.get(sid) or [])]
        valid = [m for m in (history or []) if isinstance(m, dict)
                 and m.get("role") in ("user", "assistant")
                 and str(m.get("content") or "").strip()]
        if len(valid) < 4:
            return
        recent = speaker_labeled_lines(valid, limit=OPEN_TOPICS_HISTORY_LINES)
        prompt = (f"现有清单：{json.dumps(current, ensure_ascii=False)}\n\n"
                  f"最近对话：\n{recent}\n\n"
                  "请结合最近对话增删，输出更新后的完整清单。")
        try:
            result = await chat_once(ctx, [{"role": "system", "content": OPEN_TOPICS_SYSTEM},
                                           {"role": "user", "content": prompt}],
                                     label="未了话题")
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
        with self._lock:
            if epoch != self._epochs.get(sid, 0) or version != self._topic_versions.get(sid):
                return
            previous = copy.deepcopy(self._open)
            self._open[sid] = [{"text": t, "updated": time.time()}
                               for t in topics[:OPEN_TOPICS_MAX]]
            if not self._save_open():
                self._open = previous
                return
            print(f"未了话题已更新（{len(self._open[sid])} 条）")

    @_locked
    def drop_session(self, session_id: str) -> int:
        sid = str(session_id or "")
        self._epochs[sid] = self._epochs.get(sid, 0) + 1
        index = self.dir / f"{self._key(sid)}.json"
        existed = sid in self._open or sid in self._cache or index.exists()
        previous = copy.deepcopy(self._open)
        self._open.pop(sid, None)
        if not self._save_open():
            self._open = previous
            raise OSError("无法保存清空后的未了话题，请检查文件访问权限或磁盘空间")
        self._turns.pop(sid, None)
        try:
            index.unlink(missing_ok=True)
        except OSError as exc:
            raise OSError("回忆索引删除失败，请重试清空操作") from exc
        self._cache.pop(sid, None)
        return int(existed)

    # ---------------- 索引 ----------------
    @staticmethod
    def _key(session_id: str) -> str:
        return hashlib.sha1(str(session_id or "").encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _segment_id(entry):
        return entry.get("id") or hashlib.sha256(
            json.dumps([entry.get("ts", 0), entry.get("text", "")], ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:24]

    @_locked
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
        corrections = data.get("corrections")
        index = {"session_id": sid,
                 "segments": segments if isinstance(segments, list) else [],
                 "vectors": vectors if isinstance(vectors, list) else [],
                 "corrections": corrections if isinstance(corrections, dict) else {}}
        self._cache[sid] = index
        return index

    @_locked
    def _save_index(self, session_id: str):
        sid = str(session_id or "")
        if sid in self._index_load_failed:
            print("回忆索引本次未能读取，已跳过保存以免覆盖磁盘上的原有内容。")
            return False
        index = self._cache.get(sid)
        if index is None:
            return False
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            save_json(self.dir / f"{self._key(sid)}.json", index)
            return True
        except Exception as e:
            print(f"保存回忆索引失败: {e}")
            return False

    @_locked
    def list_memories(self, session_id):
        index = self._load_index(session_id)
        return [{"id": self._segment_id(entry), "ts": entry.get("ts", 0),
                 "text": str(entry.get("text") or ""), "pinned": bool(entry.get("pinned"))}
                for entry in reversed(index["segments"]) if isinstance(entry, dict)]

    @_locked
    def change_memory(self, session_id, identifier, action, text=""):
        sid = str(session_id)
        original = self._load_index(sid)
        if action not in {"edit", "pin", "forget"}:
            raise ValueError("回忆操作无效")
        if action == "edit" and not str(text or "").strip():
            raise ValueError("回忆内容不能为空")
        updated = copy.deepcopy(original)
        segments = updated["segments"]
        position = next((i for i, entry in enumerate(segments)
                         if isinstance(entry, dict) and self._segment_id(entry) == identifier), None)
        if position is None:
            return False
        entry = segments[position]
        entry["id"] = identifier
        updated["vectors"] += [None] * max(0, len(segments) - len(updated["vectors"]))
        correction = {"ts": entry.get("ts", 0), "end": entry.get("end", entry.get("ts", 0))}
        if action == "forget":
            segments.pop(position)
            updated["vectors"].pop(position)
            correction["forgotten"] = True
        else:
            if action == "edit":
                entry["text"] = text
                updated["vectors"][position] = None
            elif action == "pin":
                entry["pinned"] = not entry.get("pinned", False)
            correction.update(text=entry["text"], pinned=bool(entry.get("pinned")))
        updated["corrections"][identifier] = correction
        self._cache[sid] = updated
        if not self._save_index(sid):
            self._cache[sid] = original
            raise OSError("回忆索引写入失败")
        self._epochs[sid] = self._epochs.get(sid, 0) + 1
        return True

    async def refresh(self, session_id: str, history: list):
        if not self._enabled():
            return
        sid = str(session_id or "")
        groups = split_segments(history)
        if len(groups) <= 1:
            return
        done = groups[:-1]
        with self._lock:
            epoch = self._epochs.get(sid, 0)
            version = self._refresh_versions[sid] = self._refresh_versions.get(sid, 0) + 1
            index = copy.deepcopy(self._load_index(sid))
        edits = index["corrections"]
        metadata = {}
        for group in done:
            ts, end = group[0]["ts"], group[-1]["ts"]
            text = render_segment(group)
            identifier = self._segment_id({"text": text, "ts": ts})
            matching = {identifier: edits[identifier]} if identifier in edits else {}
            # A history trim can shift a segment boundary. Suppress overlapping
            # source text even then, and retain the user's replacement.
            if ts > 0 and not matching:
                matching = {key: rule for key, rule in edits.items()
                            if isinstance(rule, dict) and rule.get("ts", 0) > 0
                            and ts <= rule.get("end", 0) and end >= rule["ts"]}
            if not matching:
                metadata[identifier] = {"id": identifier, "text": text, "ts": ts, "end": end}
            for key, rule in matching.items():
                if not rule.get("forgotten"):
                    metadata[key] = {"id": key, "text": rule["text"], "ts": rule["ts"],
                                     "end": rule["end"], "pinned": bool(rule.get("pinned"))}
        for key, rule in edits.items():
            if isinstance(rule, dict) and rule.get("pinned") and key not in metadata:
                metadata[key] = {"id": key, "text": rule["text"], "ts": rule["ts"],
                                 "end": rule["end"], "pinned": True}
        known = {}
        for entry, vec in zip(index["segments"], index["vectors"]):
            if isinstance(entry, dict):
                known[str(entry.get("text") or "")] = vec
        todo = list(dict.fromkeys(item["text"] for item in metadata.values()
                                  if item["text"] and not known.get(item["text"])))
        if todo and self._mode() == "embedding" and self.embedder is not None:
            try:
                fresh = await self.embedder.embed_texts(todo)
            except Exception as e:
                fresh = None
                print(f"回忆索引向量化失败（下次再试）: {type(e).__name__}: {e}")
            if fresh is not None and len(fresh) == len(todo):
                for text, vec in zip(todo, fresh):
                    known[text] = [float(x) for x in vec]
        index["segments"] = sorted(metadata.values(), key=lambda item: item["ts"])
        index["vectors"] = [known.get(item["text"]) for item in index["segments"]]
        with self._lock:
            if epoch != self._epochs.get(sid, 0) or version != self._refresh_versions.get(sid):
                return
            previous = self._cache.get(sid)
            self._cache[sid] = index
            if not self._save_index(sid):
                if previous is None:
                    self._cache.pop(sid, None)
                else:
                    self._cache[sid] = previous
                return
        if todo:
            print(f"回忆索引已更新：{sid} 共 {len(metadata)} 段"
                  f"（新增 {len(todo)} 段，检索方式：{self._mode()}）")

    # ---------------- 检索 ----------------
    async def recall(self, session_id: str, query: str, top_k: int = None,
                     exclude_after_ts: float = 0.0) -> List[dict]:
        if not self._enabled() or not str(query or "").strip():
            return []
        sid = str(session_id or "")
        with self._lock:
            epoch = self._epochs.get(sid, 0)
            index = copy.deepcopy(self._load_index(sid))
        segments = index["segments"]
        if not segments:
            return []
        try:
            top_k = max(1, int(top_k or self.config.get("history_recall_top_k", 3) or 3))
        except (TypeError, ValueError):
            top_k = 3
        scored: List[tuple] = []
        query_vector = None
        if self._mode() == "embedding" and self.embedder is not None \
                and any(v for v in index["vectors"]):
            try:
                query_vec = await self.embedder.embed_texts([query])
            except Exception as e:
                query_vec = None
                print(f"回忆检索向量化失败，本次改用字符重合: {type(e).__name__}: {e}")
            if query_vec is not None and len(query_vec):
                query_vector = _normalize(query_vec[0])
        for position, entry in enumerate(segments):
            if not isinstance(entry, dict):
                continue
            vec = index["vectors"][position] if position < len(index["vectors"]) else None
            if query_vector is not None and vec and len(vec) == len(query_vector):
                scored.append((float(_normalize(vec) @ query_vector), MIN_SIMILARITY_EMBEDDING, entry))
            else:
                scored.append((lexical_score(query, str(entry.get("text") or "")), MIN_SIMILARITY_LEXICAL, entry))
        hits = []
        for score, floor, entry in scored:
            if score < floor and not entry.get("pinned"):
                continue
            ts = float(entry.get("ts") or 0)
            if exclude_after_ts and ts >= exclude_after_ts:
                continue
            hits.append({"sim": round(score, 3), "ts": ts,
                         "text": str(entry.get("text") or ""), "pinned": bool(entry.get("pinned"))})
        hits.sort(key=lambda hit: (not hit["pinned"], -hit["sim"]))
        picked: List[dict] = []
        for hit in hits:
            if hit["ts"] and any(p["ts"] and abs(hit["ts"] - p["ts"]) < RECALL_TIME_SPREAD_SECONDS
                                 for p in picked):
                continue
            picked.append(hit)
            if len(picked) >= top_k:
                break
        picked.sort(key=lambda hit: (not hit["pinned"], hit["ts"]))
        with self._lock:
            return picked if self._epochs.get(sid, 0) == epoch else []

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


_ROLE_RECALL_LOCK = threading.RLock()
_ROLE_RECALL = {}


def recall_for_context(ctx):
    from . import app_context
    template = app_context.recall_mgr
    if template is None:
        return None
    root = getattr(template, "data_path", None)
    if root is None:
        return None
    key = str(ctx.character_key or "").strip()
    if not key:
        raise ValueError("回忆上下文缺少角色标识")
    identity = (str(root.resolve()), key)
    with _ROLE_RECALL_LOCK:
        manager = _ROLE_RECALL.get(identity)
        if manager is None:
            # 旧的全局索引保留原文件，新角色索引从各自有归属的历史重建。
            manager = HistoryRecallManager(ctx, root, template.embedder, role_key=key)
            _ROLE_RECALL[identity] = manager
        manager.config = ctx
        return manager


def drop_session_all_roles(session_id):
    from . import app_context
    template = app_context.recall_mgr
    if template is None:
        return 0
    total = template.drop_session(session_id)
    with _ROLE_RECALL_LOCK:
        managers = [m for m in _ROLE_RECALL.values() if m.data_path == template.data_path]
        loaded_dirs = {m._open_file.parent for m in managers}
        for manager in managers:
            total += manager.drop_session(session_id)
        for directory in (template.data_path / "role_recall").glob("*"):
            if not directory.is_dir() or directory in loaded_dirs:
                continue
            manager = HistoryRecallManager(template.config, directory, template.embedder)
            total += manager.drop_session(session_id)
    return total
