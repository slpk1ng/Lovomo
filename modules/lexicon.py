"""自主学习：从历史对话里学习黑话、俚语与专有表达，供角色理解与复用。

存于 data/learned_terms.json：
  {"terms": {"<表达>": {term, meaning, category, confidence, evidence, count, updated_at}},
   "pending": [{id, term, meaning, category, confidence, evidence, reason, created_at}]}
"""
import json
import re
import time
import uuid
from pathlib import Path

from .llm_helpers import generate_json_reply, speaker_labeled_lines

DEFAULT_LEARN_PROMPT = (
    "你是对话黑话学习助手。任务：扫描下面这段用户与虚拟角色之间的历史对话，"
    "找出其中【字面看不出意思、但在这群人的对话里有确定含义】的表达，并推断它的含义。\n"
    "一、学习触发条件（不满足就不要输出这一条）\n"
    "1. 该表达必须在对话里被真实使用过，不能只凭你自己见过这个词就写出来。\n"
    "2. 必须能在对话里找到判定含义的依据：它周围的上下文、说话人随后给出的解释、"
    "或角色回复中对它的回应方式，三者至少有一条。\n"
    "3. 只学本次对话新出现的表达，或旧含义需要修正的表达；已经理解且没有新证据的不要重复输出。\n"
    "二、识别范围（只收这四类）\n"
    "1. 网络黑话与缩写：如 yyds、xswl、栓Q、绝绝子这类。\n"
    "2. 俚语与口头禅：特定圈子内部才懂的用法、调侃式说法。\n"
    "3. 专有表达：某个游戏、作品、事件或人物在这群人里的固定叫法。\n"
    "4. 指代称呼：对话里用来指代具体人或事物的外号、谐音代称。\n"
    "三、排除项（一律不学）\n"
    "1. 普通话常用词、成语、常规书面语，以及查字典就能解释的普通词。\n"
    "2. 角色的人设、名字与口头禅，那是角色自己的表达，不是对话中用户的表达。\n"
    "3. 错别字、输入法误触、纯表情符号、纯数字、链接与文件名。\n"
    "4. 只出现过一次、且上下文完全看不出含义的临时玩笑，除非它在对话里被明确解释过。\n"
    "四、含义推断判定规则\n"
    "1. 只能依据对话内的证据推断，禁止凭先验知识编造一个「听起来合理」的含义。\n"
    "2. meaning 用一句普通话写清它在这段对话里表达什么（情绪、态度或指代对象），"
    "不写词源考据，不用「可能」「大概」这类含糊措辞。\n"
    "3. category 只能从「网络黑话」「俚语」「专有表达」「指代称呼」四个里选一个。\n"
    "4. confidence 是你对「这个含义正确」的把握，取 0~1："
    "对话里有说话人直接解释、或该用法多次一致，给 0.8~1.0；"
    "上下文能推出大致方向但存在第二种合理解释，给 0.4~0.7；只能靠猜，给 0.4 以下。\n"
    "5. 不确定时按上面的分值如实给分，不要为了让它被采纳而抬高分数，"
    "并在 uncertain_reason 里写清为什么拿不准；"
    "程序会把低分条目放进「待确认」列表，在 WebUI 上交给用户裁决，确认前不参与回复。\n"
    "6. 先看字面：该表达在普通话里本来的具体含义优先——它本身就有明确的具体所指"
    "（某种动作、物品或现象）时，就按这个本义写，不要按网络流行义、调侃义改写成别的意思；"
    "只有对话里明确把它当作别的东西使用时，才写对话里的用法。\n"
    "7. 含义要写清它指的具体动作、东西或对象，"
    "不要把具体现象概括成「一种状态」「一种氛围」「一种关系」这类抽象说法。\n"
    "五、与已有词典的关系（存储与更新）\n"
    "输入里会给出当前已学到的词典，按下面的规则决定 action：\n"
    "1. add：词典里没有这个表达，新增。\n"
    "2. update：词典里有，但本次对话的证据说明旧含义不准确，用更准确的含义替换。\n"
    "3. remove：本次对话明确否定、纠正或已经废弃了旧含义（如用户说「别再用这个词了」"
    "「那不是说这个」），删除该条。\n"
    "4. 含义一致、只是又用了一次，不要输出，程序会自行累计出现次数。\n"
    "只输出JSON，格式："
    '{"learned": [{"term": "表达原文", "meaning": "一句普通话含义", "category": "网络黑话", '
    '"confidence": 0.9, "evidence": "对话中出现的原话片段", "action": "add", '
    '"uncertain_reason": "把握不足的原因，把握足够时留空字符串"}]}，'
    "没有可学内容时输出 {\"learned\": []}。禁止输出任何其它文字。"
)

DEFAULT_INJECT_TEMPLATE = (
    "【黑话词典】下列表达是当前对话中的人约定俗成的说法，"
    "按这些含义理解，不要向对方解释你在查词典：\n{terms}"
)

# 旧版默认提示词没写「先按字面理解」，模型会把具体现象概括成「一种状态」，
# 或按网络流行义理解，升级时按原样补上这两条规则。
LEGACY_LEARN_TAIL_PHRASES = (
    ("确认前不参与回复。\n五、与已有词典的关系（存储与更新）\n",
     "确认前不参与回复。\n"
     "6. 先看字面：该表达在普通话里本来的具体含义优先——它本身就有明确的具体所指"
     "（某种动作、物品或现象）时，就按这个本义写，不要按网络流行义、调侃义改写成别的意思；"
     "只有对话里明确把它当作别的东西使用时，才写对话里的用法。\n"
     "7. 含义要写清它指的具体动作、东西或对象，"
     "不要把具体现象概括成「一种状态」「一种氛围」「一种关系」这类抽象说法。\n"
     "五、与已有词典的关系（存储与更新）\n"),
)


def migrate_learn_prompt(config) -> bool:
    """把旧版默认学习提示词补上「先按字面理解」两条规则。"""
    raw = str(config.get("learning_prompt", "") or "")
    if not raw:
        return False
    new = raw
    for old, repl in LEGACY_LEARN_TAIL_PHRASES:
        new = new.replace(old, repl)
    if new == raw:
        return False
    config["learning_prompt"] = new
    return True

CATEGORIES = ("网络黑话", "俚语", "专有表达", "指代称呼")

MAX_TERM_CHARS = 32
MAX_MEANING_CHARS = 120
MAX_EVIDENCE_CHARS = 80
MAX_REASON_CHARS = 80
MAX_PENDING = 100
MAX_PROMPT_TERMS = 80
MAX_PROMPT_TERMS_CHARS = 2000


def _as_bool(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def _as_int(value, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean(value, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


class LexiconManager:
    def __init__(self, config, data_path: Path):
        self.config = config
        self.data_path = Path(data_path)
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.file = self.data_path / "learned_terms.json"
        self.terms = {}
        self.pending = []
        self._load_failed = False
        self.load()

    # ---------------- 存取 ----------------
    def load(self):
        from .jsonio import load_json_ex
        data, readable = load_json_ex(self.file, {})
        self._load_failed = not readable
        if not isinstance(data, dict):
            data = {}
        terms = data.get("terms")
        self.terms = terms if isinstance(terms, dict) else {}
        pending = data.get("pending")
        self.pending = pending if isinstance(pending, list) else []

    def save(self):
        if self._load_failed:
            print("黑话词典本次未能读取，已跳过保存以免覆盖磁盘上的原有内容。")
            return
        try:
            from .jsonio import save_json
            save_json(self.file, {"terms": self.terms, "pending": self.pending})
        except Exception as e:
            print(f"保存黑话词典失败: {e}")

    @property
    def enabled(self) -> bool:
        return _as_bool(self.config.get("learning_enabled"), True)

    # ---------------- 触发 ----------------
    def should_learn(self, user_msg_count: int) -> bool:
        """按会话累计的用户消息数触发，避免每条消息都调用一次 LLM。"""
        if not self.enabled:
            return False
        every = _as_int(self.config.get("learning_trigger_messages"), 20)
        try:
            count = int(user_msg_count)
        except (TypeError, ValueError):
            return False
        return count > 0 and count % every == 0

    # ---------------- 学习 ----------------
    async def learn_from_history(self, ctx, history: list, session_id: str = "") -> dict:
        """扫描近期历史并更新词典，失败静默。"""
        if not self.enabled or not history:
            return {}
        limit = _as_int(self.config.get("learning_history_lines"), 30, minimum=4)
        lines = speaker_labeled_lines(history, limit=limit)
        if len(lines) < 2:
            return {}
        prompt = str(self.config.get("learning_prompt", "") or DEFAULT_LEARN_PROMPT)
        system = (f"{prompt}\n已有词典（判断 action 用）："
                  f"{json.dumps(self._prompt_terms(), ensure_ascii=False)}")
        user_prompt = "对话：\n" + "\n".join(lines)
        try:
            data = await generate_json_reply(ctx, system, user_prompt, max_tokens=512)
        except Exception as e:
            print(f"黑话学习失败: {e}")
            return {}
        if not isinstance(data, dict) or not isinstance(data.get("learned"), list):
            return {}
        result = self.apply_items(data["learned"])
        if any(result.values()):
            print(f"黑话学习：新增 {result['added']}、更新 {result['updated']}、"
                  f"删除 {result['removed']}、待确认 {result['held']}")
        return result

    def apply_items(self, items: list) -> dict:
        """按置信度分流模型输出：达标入库，不达标进待确认。"""
        threshold = _as_float(self.config.get("learning_min_confidence"), 0.6)
        result = {"added": 0, "updated": 0, "removed": 0, "held": 0}
        now = time.time()
        # 落盘依据是"内存有没有被改动"，不能拿 result 计数代替：
        # 刷新已有待确认项、或删除只存在于待确认里的词条时计数为 0，
        # 但内存已经变了，不落盘就会在重启后回退。
        changed = False
        for item in items:
            if not isinstance(item, dict):
                continue
            term = _clean(item.get("term"), MAX_TERM_CHARS)
            if not term:
                continue
            if str(item.get("action", "add") or "add").strip().lower() == "remove":
                if self.terms.pop(term, None) is not None:
                    result["removed"] += 1
                    changed = True
                if self._drop_pending(term):
                    changed = True
                continue
            meaning = _clean(item.get("meaning"), MAX_MEANING_CHARS)
            if not meaning:
                continue
            confidence = min(1.0, max(0.0, _as_float(item.get("confidence"), 0.0)))
            category = self._category(item.get("category"))
            evidence = _clean(item.get("evidence"), MAX_EVIDENCE_CHARS)
            if confidence < threshold:
                if self._hold(term, meaning, category, confidence, evidence,
                              item.get("uncertain_reason"), now):
                    result["held"] += 1
                changed = True
                continue
            existing = self.terms.get(term)
            self.terms[term] = {
                "term": term,
                "meaning": meaning,
                "category": category,
                "confidence": round(confidence, 2),
                "evidence": evidence,
                "count": _as_int((existing or {}).get("count"), 0, minimum=0) + 1,
                "updated_at": now,
            }
            result["updated" if existing else "added"] += 1
            changed = True
            self._drop_pending(term)
        if changed:
            self._trim()
            self.save()
        return result

    # ---------------- 待确认 ----------------
    def list_pending(self) -> list:
        return sorted(self.pending, key=lambda p: p.get("created_at", 0), reverse=True)

    def confirm(self, pending_id: str, term=None, meaning=None, category=None) -> bool:
        """采纳待确认词条；允许用户在页面上改完再采纳。"""
        item = next((p for p in self.pending if p.get("id") == pending_id), None)
        if item is None:
            return False
        final_term = _clean(term, MAX_TERM_CHARS) or _clean(item.get("term"), MAX_TERM_CHARS)
        final_meaning = _clean(meaning, MAX_MEANING_CHARS) or _clean(item.get("meaning"), MAX_MEANING_CHARS)
        if not final_term or not final_meaning:
            return False
        existing = self.terms.get(final_term)
        self.terms[final_term] = {
            "term": final_term,
            "meaning": final_meaning,
            "category": self._category(category or item.get("category")),
            "confidence": round(min(1.0, max(0.0, _as_float(item.get("confidence"), 0.0))), 2),
            "evidence": _clean(item.get("evidence"), MAX_EVIDENCE_CHARS),
            "count": _as_int((existing or {}).get("count"), 0, minimum=0) + 1,
            "updated_at": time.time(),
        }
        self.pending = [p for p in self.pending if p.get("id") != pending_id]
        self._trim()
        self.save()
        return True

    def reject(self, pending_id: str) -> bool:
        before = len(self.pending)
        self.pending = [p for p in self.pending if p.get("id") != pending_id]
        if len(self.pending) == before:
            return False
        self.save()
        return True

    def _hold(self, term, meaning, category, confidence, evidence, reason, now) -> bool:
        """不确定的候选入队；同词同义只刷新证据，不重复堆叠。"""
        reason = _clean(reason, MAX_REASON_CHARS) or "对话里没有足够依据判断含义"
        for item in self.pending:
            if item.get("term") == term and item.get("meaning") == meaning:
                item.update({"confidence": round(confidence, 2),
                             "evidence": evidence or item.get("evidence", ""),
                             "reason": reason, "updated_at": now})
                return False
        self.pending.append({
            "id": uuid.uuid4().hex,
            "term": term,
            "meaning": meaning,
            "category": category,
            "confidence": round(confidence, 2),
            "evidence": evidence,
            "reason": reason,
            "created_at": now,
        })
        if len(self.pending) > MAX_PENDING:
            self.pending = self.pending[-MAX_PENDING:]
        return True

    def _drop_pending(self, term: str) -> bool:
        before = len(self.pending)
        self.pending = [p for p in self.pending if p.get("term") != term]
        return len(self.pending) != before

    # ---------------- 词条维护 ----------------
    def list_terms(self) -> list:
        return sorted(self.terms.values(), key=lambda t: t.get("updated_at", 0), reverse=True)

    def upsert_term(self, term, meaning, category="") -> bool:
        """页面手动新增或改写词条。"""
        final_term = _clean(term, MAX_TERM_CHARS)
        final_meaning = _clean(meaning, MAX_MEANING_CHARS)
        if not final_term or not final_meaning:
            return False
        existing = self.terms.get(final_term)
        self.terms[final_term] = {
            "term": final_term,
            "meaning": final_meaning,
            "category": self._category(category),
            "confidence": _as_float((existing or {}).get("confidence"), 1.0),
            "evidence": (existing or {}).get("evidence", ""),
            "count": _as_int((existing or {}).get("count"), 1, minimum=1),
            "updated_at": time.time(),
        }
        self._drop_pending(final_term)
        self._trim()
        self.save()
        return True

    def delete_term(self, term) -> bool:
        key = _clean(term, MAX_TERM_CHARS)
        if self.terms.pop(key, None) is None:
            return False
        self.save()
        return True

    def _trim(self):
        """超出上限时先淘汰出现次数最少、最久未更新的词条。"""
        limit = _as_int(self.config.get("learning_max_terms"), 200, minimum=10)
        if len(self.terms) <= limit:
            return
        order = sorted(self.terms.values(),
                       key=lambda t: (_as_int(t.get("count"), 0, minimum=0),
                                      t.get("updated_at", 0)))
        for item in order[:len(self.terms) - limit]:
            self.terms.pop(item.get("term"), None)

    # ---------------- 注入 ----------------
    def build_injection(self) -> str:
        if not self.enabled or not self.terms:
            return ""
        max_chars = _as_int(self.config.get("learning_max_chars"), 400, minimum=80)
        lines, used = [], 0
        for item in self.list_terms():
            line = f"{item.get('term', '')}：{item.get('meaning', '')}"
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
        if not lines:
            return ""
        template = str(self.config.get("learning_inject_template", "") or DEFAULT_INJECT_TEMPLATE)
        return template.replace("{terms}", "\n".join(lines))

    def _prompt_terms(self) -> dict:
        """喂给模型的已有词典摘要，按最近更新截断，避免提示词无限膨胀。"""
        brief, used = {}, 0
        for item in self.list_terms()[:MAX_PROMPT_TERMS]:
            term = str(item.get("term", ""))
            meaning = str(item.get("meaning", ""))
            if used + len(term) + len(meaning) > MAX_PROMPT_TERMS_CHARS:
                break
            brief[term] = meaning
            used += len(term) + len(meaning)
        return brief

    @staticmethod
    def _category(value) -> str:
        text = str(value or "").strip()
        return text if text in CATEGORIES else CATEGORIES[0]
