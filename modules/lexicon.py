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

from .llm_helpers import generate_json_reply, role_names, speaker_labeled_lines

# 各条规则单独定义：默认提示词与「补回旧默认提示词」的迁移共用同一份文本。
# 两处各写一遍时字面稍有出入，迁移就会匹配不上或重复插入。
_RULE_SELF_LITERAL = (
    "6. 先看字面：该表达在普通话里本来的具体含义优先——它本身就有明确的具体所指"
    "（某种动作、物品或现象）时，就按这个本义写，不要按网络流行义、调侃义改写成别的意思；"
    "只有对话里明确把它当作别的东西使用时，才写对话里的用法。\n"
    "7. 含义要写清它指的具体动作、东西或对象，"
    "不要把具体现象概括成「一种状态」「一种氛围」「一种关系」这类抽象说法。\n"
)
_RULE_EXCLUDE_CHARACTER = (
    "5. 角色的名字、昵称、外号，以及本次对话里临时给角色起的名字或称呼"
    "（换个角色、换一群人就没有意义）。\n"
)
_RULE_GENERIC_MEANING = (
    "8. 含义必须通用：这本词典是所有角色、所有会话共用的，"
    "meaning 要写成脱离本次对话、换一个角色、换一群人也成立的说法。"
    "禁止出现角色名、昵称、外号，禁止出现「用户1」「角色1」这类编号；"
    "禁止出现「本次对话」「这次对话里」「被角色拒绝」这类只对某一次互动成立的说法；"
    "描述一般使用场景（如「在暧昧时用来骂人」）没关系，"
    "称呼类表达只写它在一般语境里指什么、通常怎么用，"
    "不要写成「某人对某角色的专属称呼」「被角色唯一承认的伴侣」这种绑定某段关系的说法。\n"
)
_RULE_REWRITE_BOUND = (
    "5. 已有词典里若某条含义写了角色名、昵称或编号，或是围绕某段关系/某次对话写的"
    "（如「专属称呼」「被角色唯一承认的」），一律按 update 用通用含义重写它。\n"
)
_PROMPT_BEFORE_DICT = "确认前不参与回复。\n"
_DICT_HEADING = "五、与已有词典的关系（存储与更新）\n"
_REUSE_NOTE = "4. 含义一致、只是又用了一次，不要输出，程序会自行累计出现次数。\n"

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
    + _RULE_EXCLUDE_CHARACTER +
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
    + _RULE_SELF_LITERAL
    + _RULE_GENERIC_MEANING
    + _DICT_HEADING +
    "输入里会给出当前已学到的词典，按下面的规则决定 action：\n"
    "1. add：词典里没有这个表达，新增。\n"
    "2. update：词典里有，但本次对话的证据说明旧含义不准确，用更准确的含义替换。\n"
    "3. remove：本次对话明确否定、纠正或已经废弃了旧含义（如用户说「别再用这个词了」"
    "「那不是说这个」），删除该条。\n"
    + _REUSE_NOTE
    + _RULE_REWRITE_BOUND +
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

# 旧版默认提示词缺了后加的规则（先按字面理解、含义必须通用、排除角色名）：
# 不补的话模型会把角色名或只对某次互动成立的说法写进含义，换个角色就完全用不了。
# 规则文案只有上面这一份：这里的旧片段要与它逐字对应，否则补不进去或重复插入。
_LEGACY_GENERIC_MEANING = (
    "8. 含义必须通用：这本词典是所有角色、所有会话共用的，"
    "meaning 要写成脱离本次对话、换一个角色、换一群人也成立的说法。"
    "禁止出现角色名、昵称、外号，禁止出现「用户1」「角色1」这类编号；"
    "禁止出现「此处」「本次对话」「这次对话里」「被角色拒绝」这类只对某一次互动成立的说法；"
    "称呼类表达只写它在一般语境里指什么、通常怎么用，"
    "不要写成「某人对某角色的专属称呼」「被角色唯一承认的伴侣」这种绑定某段关系的说法。\n"
)
LEGACY_LEARN_TAIL_PHRASES = (
    # 这一条要排在最前：先按整段替换掉上一版规则文本，再判断是否需要补插
    (_LEGACY_GENERIC_MEANING, _RULE_GENERIC_MEANING),
    (_PROMPT_BEFORE_DICT, _PROMPT_BEFORE_DICT + _RULE_SELF_LITERAL),
    ("4. 只出现过一次、且上下文完全看不出含义的临时玩笑，除非它在对话里被明确解释过。\n",
     "4. 只出现过一次、且上下文完全看不出含义的临时玩笑，除非它在对话里被明确解释过。\n"
     + _RULE_EXCLUDE_CHARACTER),
    (_RULE_SELF_LITERAL, _RULE_SELF_LITERAL + _RULE_GENERIC_MEANING),
    (_REUSE_NOTE, _REUSE_NOTE + _RULE_REWRITE_BOUND),
)


def migrate_learn_prompt(config) -> bool:
    """把旧版默认学习提示词补上后加的规则。

    逐条判断目标文本是否已经存在：这些片段补进去之后原文案仍在，
    不看这一层的话每次启动都会把同一段规则重复插一遍。
    """
    raw = str(config.get("learning_prompt", "") or "")
    if not raw:
        return False
    new = raw
    for old, repl in LEGACY_LEARN_TAIL_PHRASES:
        if repl in new:
            continue
        new = new.replace(old, repl)
    if new == raw:
        return False
    config["learning_prompt"] = new
    return True

CATEGORIES = ("网络黑话", "俚语", "专有表达", "指代称呼")

# 只对某一次对话成立的说法：词典是跨角色共用的，这类含义换个角色就读不通。
# 「此处」「这次」这类描述一般使用场景的措辞不算——它只是说明了这个说法怎么用，
# 换个角色照样成立（如「指代被轻视的对象，此处用于在暧昧时骂人」）。
_SESSION_BOUND_RE = re.compile(r"本次对话|这次对话里|本对话|这轮对话|用户\d+|角色\d+")
QUARANTINE_REASON = "含义绑定了具体角色或某次对话，换个角色不适用，请改成通用含义"


def _bound_to_character(term: str, meaning: str, names) -> bool:
    """词条本身或含义里出现了角色名/会话编号：换个角色就没法用。"""
    text = f"{term} {meaning}".lower()
    if _SESSION_BOUND_RE.search(text):
        return True
    return any(name in text for name in names or ())

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
        changed = self._restore_no_longer_bound()
        changed = self._quarantine_character_bound() or changed
        if changed:
            self.save()

    def _restore_no_longer_bound(self) -> bool:
        """把之前因「绑定角色」被隔离、按当前规则其实通用的条目放回词典。

        判定规则收紧过一次又放宽过一次（如「此处用于…」这种说明使用场景的写法
        不再算绑定），早先被隔离的条目会一直挂在待确认里，理由也已经不成立。
        """
        names = role_names(self.config)
        keep, restored = [], []
        for item in self.pending:
            if not isinstance(item, dict) or str(item.get("reason") or "") != QUARANTINE_REASON:
                keep.append(item)
                continue
            term = str(item.get("term") or "")
            if _bound_to_character(term, str(item.get("meaning") or ""), names):
                keep.append(item)
            else:
                restored.append(item)
        if not restored:
            return False
        self.pending = keep
        now = time.time()
        for item in restored:
            term = str(item.get("term") or "")
            self.terms[term] = {
                "term": term,
                "meaning": str(item.get("meaning") or ""),
                "category": item.get("category") or CATEGORIES[0],
                "confidence": _as_float(item.get("confidence"), 0.0),
                "evidence": str(item.get("evidence") or ""),
                "count": 1,
                "updated_at": now,
            }
        print(f"黑话词典：{len(restored)} 条含义按当前规则是通用的，已从待确认放回词典。")
        return True

    def _quarantine_character_bound(self) -> bool:
        """把含义绑定了具体角色的旧词条移入「待确认」。

        词典是所有角色共用的：含义里写了角色名、昵称或某次互动才成立的说法，
        换个角色就读不通。移入待确认后它不再参与回复，用户可在页面上改成通用含义再采纳；
        重新学到通用含义时也会自动把这条待确认顶掉。

        用户自己采纳或手写的词条（confirmed）一律不再动：上一次点过「通过」，
        重启后又被打回待确认，等于白确认一遍。
        """
        names = role_names(self.config)
        bad = [key for key, item in self.terms.items()
               if isinstance(item, dict) and not item.get("confirmed")
               and _bound_to_character(str(item.get("term") or key),
                                       str(item.get("meaning") or ""), names)]
        if not bad:
            return False
        now = time.time()
        for key in bad:
            item = self.terms.pop(key) or {}
            self._hold(item.get("term") or key, str(item.get("meaning") or ""),
                       item.get("category"), _as_float(item.get("confidence"), 0.0),
                       item.get("evidence", ""), QUARANTINE_REASON, now)
        print(f"黑话词典：{len(bad)} 条含义绑定了具体角色，已移入待确认"
              "（这类含义换个角色不适用，可改成通用含义后采纳）")
        return True

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
                  f"删除 {result['removed']}、待确认 {result['held']}、"
                  f"跳过（含义绑定具体角色）{result['skipped']}")
        return result

    def apply_items(self, items: list) -> dict:
        """按置信度分流模型输出：达标入库，不达标进待确认。"""
        threshold = _as_float(self.config.get("learning_min_confidence"), 0.6)
        names = role_names(self.config)
        result = {"added": 0, "updated": 0, "removed": 0, "held": 0, "skipped": 0}
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
            # 含义里带角色名/编号的条目一律不要：词典跨角色共用，这种含义换个角色就读不通
            if _bound_to_character(term, meaning, names):
                result["skipped"] += 1
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
            # 用户亲自采纳过：之后不再被自动清理，否则重启就打回待确认
            "confirmed": True,
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
            # 手动写/改的词条同样不再被自动清理
            "confirmed": True,
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
