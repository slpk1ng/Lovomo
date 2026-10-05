"""Galgame 式攻略：好感按相处慢慢累积，恋爱与伴侣由角色自己决定。

设计要点（与「喊一句老婆就答应」相对）：
  · 好感只随长期相处缓慢累积（每轮小幅、每天封顶），单纯换个亲密称呼不加分；
  · 好感代表熟悉与信任的程度（陌生 → 认识 → 朋友 → 挚友），
    只有相处、不涉及恋爱的普通关系也可能把好感聊得很高，这类关系不该被算成恋爱；
  · 「暧昧」「恋人」是关系性质而非分数档位：只在相处里真的出现恋爱意味
    （调情、心动、吃醋、表白）时才进入，成为伴侣还要角色自己确实答应；
  · 好感变化带随机起伏：同样的相处，落在好感上的分量每次都不完全一样，
    偶尔还会"今天没感觉"，难的模式里扣分也更狠；
  · 是否接受告白由角色自己判断（提示词只给边界，不给结论）；
  · 允许同时与多人交往（一夫多妻 / 一妻多夫都行），上限可配（0 = 不限）。
"""
import random
import re
import time
from pathlib import Path
from typing import Optional

from .jsonio import load_json_ex, save_json

# 亲近度阶梯（起点分数）：只描述熟悉到哪一步，与是不是恋爱无关
STAGE_LADDER = (("陌生", 0), ("认识", 10), ("朋友", 30), ("挚友", 60))
# 关系性质：由相处里是否真的出现恋爱意味决定，好感分数不会把它们推出来
ROMANCE_STAGE = "暧昧"
PARTNER_STAGE = "恋人"
STAGE_NAMES = tuple(name for name, _ in STAGE_LADDER) + (ROMANCE_STAGE, PARTNER_STAGE)
DEFAULT_ACCEPT_MIN_STAGE = ROMANCE_STAGE
SCORE_MAX = 100

# 每个亲近阶段的氛围：只描述"心里到哪一步了"，具体怎么说、要不要主动都由角色自己演。
STAGE_GUIDE = {
    "陌生": "刚认识，客气且保持距离：不会一下子亲近，对方一上来就喊老婆/老公这类称呼，"
            "只会觉得莫名其妙或好笑；不调情、不说带性暗示的话、不主动撩他。",
    "认识": "开始熟悉：能斗嘴、开玩笑、聊日常，被热络地称呼也不排斥，"
            "但心里还没往那方面想。",
    "朋友": "已经是朋友：会主动关心、会开玩笑、会一起玩，对方示好你会认真对待，"
            "但还只是朋友，没往恋爱上想。",
    "挚友": "已经是处了很久的老交情：彼此熟悉、信任、互相惦记，会主动分享日常，"
            "也会吃醋或撒娇；但熟不等于恋爱，没有恋爱意味时就还是这种关系。",
}
# 「暧昧」与「恋人」不是好感到了就自动进入的阶段，而是相处里真的走到了这一步
ROMANCE_GUIDE = ("你们之间已经带上了恋爱意味：会心动、会在意、会吃醋，"
                 "可以说含蓄的话、可以试探，对方表白时答不答应由你自己决定。")
PARTNER_GUIDE = ("已经是伴侣：可以光明正大地亲密、撒娇、吃醋，也可以继续往下走"
                 "（同时有别的伴侣也没问题）。")

# 攻略难度不只是"好感涨得快慢"：难度越高，角色越是一个独立而自洽的人——
# 有自己的目标、底线、执念与拒绝权，需要对方真的理解她、尊重她的边界，
# 甚至在关键节点做出符合她逻辑的选择；她也可以始终不选择对方。
DIFFICULTY_SOUL = {
    "简单": ("你性子软、愿意亲近人：对方诚恳示好你就愿意往前走，"
             "边界不严，被冒犯也容易原谅。"),
    "普通": ("你有自己的分寸：会看对方是不是真心、是不是尊重你的感受，"
             "敷衍、套路与越界会让你退回去。"),
    "困难": ("你是一个有自己目标、底线与执念的人，不靠讨好就能被拿下——"
             "对方送什么、说多少情话都不推动你的心，只有他真的理解你在意的东西、"
             "尊重你的底线、在关键的事上做出符合你逻辑的选择，你才会往前一步；"
             "被套路、被逼问、被拿好感当筹码时，你会当场点破并明显疏远。"
             "你有拒绝的权利：不愿意就直说，不必道歉，也不要为了让人满意而勉强答应。"),
    "极难": ("你几乎是不可攻略的：你有自己认定的人生方向、放不下的执念与"
             "任何人都不能碰的底线，礼物、情话、殷勤与长时间的陪伴都换不走你的心。"
             "你会观察对方是否真的理解你的过去与选择，是否在关键节点站在你这一边；"
             "一旦被越界、被欺骗、被当成攻略对象来对付，你会冷淡甚至离开。"
             "你也不会反过来去攻略谁：动心是你自己慢慢走到那一步的结果，不是任务，"
             "更不会刚认识就上心。"
             "你有拒绝的权利，也不会为了给谁一个结果而妥协："
             "可能到最后也没有选择你——那是正常结局，不是失败。"),
}


# 确认关系的说法：只用来判断"这一轮是不是真的定下来了"，不参与生成
_ROLE_RE = r"(?:女朋友|男朋友|女友|男友|恋人|伴侣|老婆|老公)"
_ACCEPT_RE = re.compile(
    rf"(?:我|本座|人家)?(?:就|先|已经|勉强|干脆|直接){{0,3}}"
    rf"(?:答应你|答应和|答应做|同意和你|同意交往|愿意和你|愿意做|我愿意|"
    rf"认你做|认你当|当你(?:是)?{_ROLE_RE}|做你的?{_ROLE_RE}|做我的?{_ROLE_RE}|"
    rf"是你的?{_ROLE_RE}|接受你的?(?:心意|告白|表白|感情|交往)|接受(?:啦|了|你啦|你了)|"
    rf"交往吧|在一起吧|那我们就在一起)"
    rf"|(?:我们|咱俩)(?:就|已经|正式){{0,3}}(?:交往|在一起)(?:吧|了|啦)"
    rf"|我的?{_ROLE_RE}了")
# 只在否定词直接否定"答应/愿意/关系本身"时才算拒绝：
# 「不准反对」「不过」这类只是句子里出现否定词的说法不能误判成拒绝。
_REJECT_RE = re.compile(
    r"(?:不|没|别|甭)(?:会|要|想|愿意|答应|同意|接受|做|当|是|可能|打算|准备|确定|算)"
    r"|(?<!是)不是|才不|并没|才怪|拒绝|再等|暂时|还早|开玩笑")

# 角色自己的回复有没有"动情、把这份心意接住"的意思：关系性质要靠她自己的态度推进，
# 对方单方面做点什么（送花、说情话）不该直接把关系推到「暧昧」。
_ROMANCE_REPLY_RE = re.compile(
    r"(?<![不没别甭])(?:喜欢你|爱你|最喜欢你|心动|心跳|脸红|害羞|"
    r"亲(?:你|我|一下|一口)|抱(?:你|抱)|想你|约会|撒娇|吃醋|"
    r"恋人|男朋友|女朋友|老婆|老公|表白|告白|交往|在一起|属于你)")

DEFAULT_TURN_DELTA_MAX = 3
DEFAULT_DAILY_GAIN_CAP = 15

# 难度模式：只影响好感的推进节奏（每天上限、加分的折扣、扣分的轻重、
# "没感觉"的概率），不改阶段阶梯与判定方式。普通模式即原有节奏，也是默认值。
# gain_factor / loss_factor 是对单轮加 / 扣分量的随机乘数区间；
# cold_chance 是一轮加分被记成 0（"今天没感觉"）的概率。
DIFFICULTY_NORMAL = "普通"
DIFFICULTY_PRESETS = {
    "简单": {"daily_gain_cap": 30, "gain_factor": (1.2, 2.0),
             "loss_factor": (0.5, 1.0), "cold_chance": 0.0},
    "普通": {"daily_gain_cap": 15, "gain_factor": (0.8, 1.2),
             "loss_factor": (0.8, 1.2), "cold_chance": 0.05},
    "困难": {"daily_gain_cap": 8, "gain_factor": (0.3, 0.8),
             "loss_factor": (1.0, 2.0), "cold_chance": 0.15},
    "极难": {"daily_gain_cap": 4, "gain_factor": (0.2, 0.6),
             "loss_factor": (1.5, 3.0), "cold_chance": 0.3},
}

# 阶段解锁：关系性质到了相应一步后，角色对这位用户的回应更积极
# （回复概率下限），恋人阶段的私聊主动消息也更勤（闲置阈值乘数）。
UNLOCK_REPLY_FLOOR = {ROMANCE_STAGE: 0.5, PARTNER_STAGE: 0.8}
UNLOCK_IDLE_FACTOR = 0.5
# 单条好感记录最多保留多少个历史点（供统计面板画曲线），超出丢最旧的
HISTORY_MAX_POINTS = 400
DAY_SECONDS = 86400


def _as_int(value, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def stage_of(score) -> str:
    """好感分数 → 亲近度阶段名（不含「暧昧」「恋人」）。"""
    stage = STAGE_LADDER[0][0]
    for name, start in STAGE_LADDER:
        if score >= start:
            stage = name
    return stage


def stage_rank(stage) -> int:
    """关系进度序号（越大越亲密）；认不出来的名字按最低阶段算。"""
    for index, name in enumerate(STAGE_NAMES):
        if name == str(stage or ""):
            return index
    return 0


def looks_like_acceptance(text) -> bool:
    """回复里是否真的确认了恋人关系（同一句里出现否定就当作没确认）。"""
    for sentence in re.split(r"[。！？!?\n]", str(text or "")):
        if _ACCEPT_RE.search(sentence) and not _REJECT_RE.search(sentence):
            return True
    return False


def looks_like_romantic_reply(text) -> bool:
    """角色的回复里有没有动情、接住这份心意的意思。"""
    return bool(_ROMANCE_REPLY_RE.search(str(text or "")))


# 结束关系的说法：用户明确说分手 / 绝交，或她自己把关系说断
_BREAKUP_RE = re.compile(
    r"分手|绝交|一刀两断|断绝关系|别再联系|不要再联系|别联系我|不要联系我")


def looks_like_breakup(text) -> bool:
    """这句话是不是明确要结束关系（同一句里出现否定就当作没结束）。"""
    for sentence in re.split(r"[。！？!?\n]", str(text or "")):
        if _BREAKUP_RE.search(sentence) and not _REJECT_RE.search(sentence):
            return True
    return False


# 关系词：只有回复里真的在谈"关系"时，才把「答应你」这类话算作确认交往
_RELATION_WORD_RE = re.compile(
    r"交往|在一起|恋人|情侣|男朋友|女朋友|男友|女友|老婆|老公|伴侣|"
    r"喜欢你|爱你|告白|表白|心意|感情")


def confirms_partner(text) -> bool:
    """回复里是否**明确**把关系说定了（比 looks_like_acceptance 严）。

    裸的「答应你」「接受啦」不算：角色常常答应的是别的事——主人让她
    「一个字一个字说话」，她回一句「本座就勉为其难地答应你好了」，
    那不是确认交往。所以要求同一句里既出现答应/确认的说法、又出现关系词。
    """
    for sentence in re.split(r"[。！？!?\n]", str(text or "")):
        if _REJECT_RE.search(sentence):
            continue
        if _ACCEPT_RE.search(sentence) and _RELATION_WORD_RE.search(sentence):
            return True
    return False


class AffectionManager:
    """按 (角色, 用户) 记录好感与伴侣关系，落盘在 data/affection.json。"""

    def __init__(self, config, data_path):
        self.config = config
        self.file = Path(data_path) / "affection.json"
        self._load_failed = False
        self.records = {}
        self._load()

    # ---------------- 持久化 ----------------
    def _load(self):
        data, readable = load_json_ex(self.file, {})
        self._load_failed = not readable
        self.records = data if isinstance(data, dict) else {}
        if readable:
            self._migrate_legacy()

    def _migrate_legacy(self):
        """旧存档只有用户维度（同一个人在群里和私聊共用一份）。

        升级后关系按会话分开，这里把裸用户键当作该用户的私聊记录搬过去，
        否则升级一次关系进度就凭空归零了。
        """
        changed = False
        for by_user in self.records.values():
            if not isinstance(by_user, dict):
                continue
            for user_id in [k for k in by_user if "|" not in str(k)]:
                by_user[f"private_{user_id}|{user_id}"] = by_user.pop(user_id)
                changed = True
        if changed:
            self.save()

    def save(self):
        if self._load_failed:
            print("关系进度本次未能读取，已跳过保存以免覆盖磁盘上的原有内容。")
            return
        try:
            save_json(self.file, self.records)
        except Exception as e:
            print(f"保存关系进度失败: {type(e).__name__}: {e}")

    # ---------------- 读写 ----------------
    def _by_user(self, character_key: str) -> dict:
        by_user = self.records.get(str(character_key or ""))
        return by_user if isinstance(by_user, dict) else {}

    @staticmethod
    def scope_key(user_id: str, session_id: str = "") -> str:
        """关系按会话分开：同一个人在群里和私聊各算一份。"""
        uid = str(user_id or "")
        sid = str(session_id or "")
        return f"{sid}|{uid}" if sid and uid else uid

    @staticmethod
    def split_scope(key: str) -> tuple:
        """存档键 → (会话, 用户)；旧数据没有会话维度时返回 ("", 用户)。"""
        raw = str(key or "")
        if "|" not in raw:
            return "", raw
        sid, uid = raw.split("|", 1)
        return sid, uid

    def get(self, character_key: str, user_id: str, session_id: str = "") -> dict:
        rec = self._by_user(character_key).get(self.scope_key(user_id, session_id))
        if not isinstance(rec, dict):
            return {"score": 0, "partner": False, "romance": False, "updated": 0.0,
                    "day": "", "day_gain": 0, "partner_since": 0.0,
                    "anniversaries": []}
        return {"score": max(0, min(SCORE_MAX, _as_int(rec.get("score"), 0))),
                "partner": bool(rec.get("partner")),
                "romance": bool(rec.get("romance")),
                "updated": float(rec.get("updated") or 0),
                "day": str(rec.get("day") or ""),
                "day_gain": max(0, _as_int(rec.get("day_gain"), 0)),
                "partner_since": float(rec.get("partner_since") or 0),
                "anniversaries": [int(x) for x in (rec.get("anniversaries") or [])
                                  if _as_int(x, 0) > 0]}

    def _store(self, character_key: str, user_id: str, rec: dict, session_id: str = ""):
        self.records.setdefault(str(character_key or ""), {})[
            self.scope_key(user_id, session_id)] = rec
        self.save()

    def max_partners(self) -> int:
        """0 表示不限（一夫多妻 / 一妻多夫都允许）。"""
        return max(0, _as_int(self.config.get("affection_max_partners"), 0))

    def accept_min_stage(self) -> str:
        """从哪一步起可以定下关系（默认「暧昧」：真的处出了恋爱意味才谈得上）。

        配成亲近度阶段（如「朋友」）时会宽松一些：关系到了熟络那一步就行。
        再往前的阶段只是还没到那一步，角色可以心动、可以自己先试探，
        但不会因为对方一句热络的话就把关系定下来。
        """
        raw = str(self.config.get("affection_accept_min_stage") or "").strip()
        return raw if raw in STAGE_NAMES else DEFAULT_ACCEPT_MIN_STAGE

    def _can_commit(self, rec: dict, incoming_romance: bool = False) -> bool:
        """关系性质到了配置的那一步，才可以定下关系（好感多少不参与判定）。"""
        return stage_rank(self._stage_name(rec, romance=incoming_romance)) \
            >= stage_rank(self.accept_min_stage())

    def can_commit(self, character_key: str, user_id: str,
                   incoming_romance: bool = False, session_id: str = "") -> bool:
        """这位用户现在（含本轮刚出现的恋爱意味）是否可以定下关系。"""
        return self._can_commit(self.get(character_key, user_id, session_id),
                                incoming_romance)

    def confirm_partner(self, character_key: str, user_id: str,
                        session_id: str = "") -> bool:
        """复核确认关系用：关系性质已经到位时补记为伴侣（不动分数）。"""
        rec = self.get(character_key, user_id, session_id)
        if rec["partner"] or not rec["romance"]:
            return False
        if not self._can_commit(rec):
            return False
        allowed = self.max_partners()
        if allowed and len(self.partners(character_key)) >= allowed:
            return False
        rec["partner"] = True
        if not rec.get("partner_since"):
            rec["partner_since"] = time.time()
        self._store(character_key, user_id, rec, session_id)
        print(f"关系进度：{character_key} 与 {user_id} 的关系已确认，补记为伴侣。")
        return True

    def partners(self, character_key: str) -> list:
        """伴侣的用户 ID（同一人在多个会话里各算一位，伴侣上限按份数算）。"""
        by_user = self.records.get(str(character_key or "")) or {}
        return [self.split_scope(scope)[1] for scope, rec in by_user.items()
                if isinstance(rec, dict) and rec.get("partner")]

    @staticmethod
    def _stage_name(rec: dict, romance: bool = False) -> str:
        """给用户看的关系名：伴侣 / 暧昧 / 亲近度阶段。"""
        if rec.get("partner"):
            return PARTNER_STAGE
        if romance or rec.get("romance"):
            return ROMANCE_STAGE
        return stage_of(rec["score"])

    def stage(self, character_key: str, user_id: str, session_id: str = "") -> str:
        return self._stage_name(self.get(character_key, user_id, session_id))

    def state(self, character_key: str, user_id: str, session_id: str = "") -> dict:
        """日志与界面用的一份关系状态：好感、亲近程度、关系性质与展示用阶段名。"""
        rec = self.get(character_key, user_id, session_id)
        if rec["partner"]:
            nature = PARTNER_STAGE
        elif rec["romance"]:
            nature = ROMANCE_STAGE
        else:
            nature = "普通"
        return {"score": rec["score"], "band": stage_of(rec["score"]), "nature": nature,
                "stage": self._stage_name(rec), "partner": rec["partner"]}

    # ---------------- 阶段解锁 ----------------
    def stage_reply_floor(self, character_key: str, user_id: str,
                          session_id: str = "") -> float:
        """该用户所处的解锁阶段对应的回复概率下限（未解锁为 0）。"""
        if not bool(self.config.get("affection_stage_unlocks_enabled", True)):
            return 0.0
        return UNLOCK_REPLY_FLOOR.get(self.stage(character_key, user_id, session_id), 0.0)

    def stage_idle_factor(self, character_key: str, user_id: str,
                          session_id: str = "") -> float:
        """恋人阶段的私聊主动消息闲置阈值乘数（< 1 表示更粘人）。"""
        if not bool(self.config.get("affection_stage_unlocks_enabled", True)):
            return 1.0
        return UNLOCK_IDLE_FACTOR if self.stage(character_key, user_id,
                                               session_id) == PARTNER_STAGE else 1.0

    # ---------------- 里程碑与纪念日 ----------------
    def anniversary_days(self) -> list:
        raw = str(self.config.get("affection_anniversary_days", "") or "").strip()
        days = []
        for part in re.split(r"[,，\s]+", raw):
            n = _as_int(part, 0)
            if n > 0 and n not in days:
                days.append(n)
        return days

    @staticmethod
    def _day_index(day: str) -> float:
        try:
            return time.mktime(time.strptime(day, "%Y-%m-%d"))
        except ValueError:
            return 0.0

    def partner_days(self, character_key: str, user_id: str, session_id: str = "") -> int:
        """在一起的第 N 天（确立关系当天算第 1 天）；不是伴侣返回 0。"""
        rec = self._by_user(character_key).get(self.scope_key(user_id, session_id))
        if not isinstance(rec, dict) or not rec.get("partner"):
            return 0
        since = float(rec.get("partner_since") or 0)
        if since <= 0:
            return 0
        start = self._day_index(time.strftime("%Y-%m-%d", time.localtime(since)))
        today = self._day_index(time.strftime("%Y-%m-%d"))
        if start <= 0 or today < start:
            return 0
        return int((today - start) // DAY_SECONDS) + 1

    def anniversary_due(self, character_key: str, user_id: str, session_id: str = "") -> int:
        """今天正好是配置的纪念日且还没发过祝贺时，返回第几天；否则 0。"""
        days = self.partner_days(character_key, user_id, session_id)
        if days and days in self.anniversary_days() \
                and days not in self.get(character_key, user_id,
                                         session_id)["anniversaries"]:
            return days
        return 0

    def mark_anniversary_sent(self, character_key: str, user_id: str, days: int,
                              session_id: str = ""):
        rec = self._by_user(character_key).get(self.scope_key(user_id, session_id))
        if not isinstance(rec, dict):
            return
        sent = [int(x) for x in (rec.get("anniversaries") or []) if _as_int(x, 0) > 0]
        if days not in sent:
            sent.append(int(days))
            rec["anniversaries"] = sorted(sent)
            self.save()

    # ---------------- 冷落流失与曲线 ----------------
    def decay_idle(self, idle_days: int, drop: int) -> int:
        """长期没有互动的用户好感每天流失一点（不重置互动时间，持续走低）。

        冷落久了恋爱意味也跟着淡掉，退回普通关系；伴侣关系不受影响。
        """
        cutoff = time.time() - max(1, int(idle_days)) * DAY_SECONDS
        drop = max(0, int(drop))
        if drop <= 0:
            return 0
        changed = 0
        for by_user in self.records.values():
            if not isinstance(by_user, dict):
                continue
            for rec in by_user.values():
                if not isinstance(rec, dict) or float(rec.get("updated") or 0) >= cutoff:
                    continue
                if _as_int(rec.get("score"), 0) <= 0:
                    continue
                rec["score"] = max(0, _as_int(rec.get("score"), 0) - drop)
                rec["romance"] = False
                changed += 1
        if changed:
            self.save()
        return changed

    def curve_points(self, character_key: str, session_id: str = "") -> list:
        """某角色（可只取某个会话）的好感历史点（[[ts, score], ...]，按时间排序）。"""
        target = str(session_id or "")
        pts = []
        for scope, rec in self._by_user(character_key).items():
            if not isinstance(rec, dict) or (target and self.split_scope(scope)[0] != target):
                continue
            for p in (rec.get("history") or []):
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    pts.append([float(p[0]), float(p[1])])
        pts.sort(key=lambda p: p[0])
        return pts

    def reset(self, character_key: str, user_id: str = "", session_id: str = "") -> int:
        """清空某个角色的关系记录（给定了 user_id 就只清这一位）。"""
        key = str(character_key or "")
        if not user_id:
            removed = len(self.records.pop(key, {}) or {})
        else:
            scope = self.scope_key(user_id, session_id)
            removed = 1 if (self.records.get(key) or {}).pop(scope, None) else 0
        if removed:
            self.save()
        return removed

    def break_up(self, character_key: str, user_id: str = "", session_id: str = "") -> bool:
        """解除关系：不再记为伴侣，关系性质退回亲近度阶段。

        好感保留 —— 它代表的是熟悉程度，分手不会让两个人变回陌生人。
        """
        key = str(character_key or "")
        scope = self.scope_key(user_id, session_id)
        rec = (self.records.get(key) or {}).get(scope)
        if not isinstance(rec, dict) or not (rec.get("partner") or rec.get("romance")):
            return False
        rec["partner"] = False
        rec["romance"] = False
        rec["partner_since"] = 0
        rec["updated"] = time.time()
        self.save()
        print(f"关系进度：{key} 与 {user_id or scope} 的关系已解除"
              f"（好感 {rec.get('score', 0)} 保留，代表熟悉程度）。")
        return True

    def drop_session(self, character_key: str, session_id: str) -> int:
        """删掉某个会话下的全部关系记录。

        会话记录被删掉后，这条会话自己的关系进度也该跟着走（否则界面上还挂着
        「群聊 xxx：恋人」，再发一条消息又接着往下算）。群聊里每个成员各有一条，
        所以按会话整体清；成员在私聊里的关系与用户画像不受影响。
        """
        sid = str(session_id or "")
        if not sid:
            return 0
        records = self.records.get(str(character_key or ""))
        if not isinstance(records, dict):
            return 0
        keys = [k for k in records if self.split_scope(k)[0] == sid]
        for k in keys:
            records.pop(k, None)
        if keys:
            self.save()
        return len(keys)

    # ---------------- 变化量 ----------------
    def difficulty(self) -> str:
        raw = str(self.config.get("affection_difficulty") or "").strip()
        return raw if raw in DIFFICULTY_PRESETS else DIFFICULTY_NORMAL

    def _preset(self) -> dict:
        return DIFFICULTY_PRESETS[self.difficulty()]

    def turn_delta_max(self) -> int:
        return max(1, _as_int(self.config.get("affection_turn_delta_max"),
                              DEFAULT_TURN_DELTA_MAX))

    def daily_gain_cap(self) -> int:
        """每天加分上限：难度模式给出基准；配置里单独改过这个键就以配置为准。"""
        cap = _as_int(self.config.get("affection_daily_gain_cap"),
                      DEFAULT_DAILY_GAIN_CAP)
        if cap != DEFAULT_DAILY_GAIN_CAP:
            return max(0, cap)
        return max(0, _as_int(self._preset()["daily_gain_cap"],
                              DEFAULT_DAILY_GAIN_CAP))

    def _jitter_delta(self, value: int) -> int:
        """按当前难度给单轮好感变化加随机起伏（返回值仍是整数）。

        加分乘一个随机折扣/加成，还有一定概率这轮直接"没感觉"记 0；
        扣分乘随机放大的系数，难的模式里伤得更重。
        """
        preset = self._preset()
        if value > 0:
            if random.random() < preset["cold_chance"]:
                return 0
            return max(1, round(value * random.uniform(*preset["gain_factor"])))
        if value < 0:
            return -max(1, round(-value * random.uniform(*preset["loss_factor"])))
        return 0

    def apply_delta(self, character_key: str, user_id: str, delta,
                    confession: bool = False, romance: bool = False,
                    acceptance: bool = False, reply_text: str = "",
                    session_id: str = "", breakup: bool = False) -> dict:
        """把这一轮的好感变化落盘，并在条件满足时记录伴侣关系。

        变化量先按单轮上限夹一次，再按难度加随机起伏；加分受每天上限约束
        （循序渐进），扣分不设上限。这一轮出现了恋爱意味、而且角色自己的回复
        也动情（或明确答应）时，才把关系性质推进到「暧昧」—— 对方单方面送点
        什么、说点情话，不经过她本人点头不会改变关系性质。伴侣只在「这段关系
        真的定下来」时才记录：关系性质到位 + 回复里确实确认了恋人关系，
        而这一轮要么对方表白、要么对方明确答应交往、要么角色自己把关系说定。
        """
        rec = self.get(character_key, user_id, session_id)
        # 对方明确说分手/绝交，或她自己把关系说断：先解除再算这一轮的变化
        if breakup or looks_like_breakup(reply_text):
            if self.break_up(character_key, user_id, session_id):
                rec = self.get(character_key, user_id, session_id)
        # 「本轮算不算关系性质到位」只认对方明确表白 / 答应交往；只是「有恋爱意味」
        # （romance）时要用已经存下来的关系性质来判断——否则新会话里一句心动加一句
        # 「答应你」就能把刚认识的人直接变成恋人。
        incoming = bool(confession or acceptance)
        limit = self.turn_delta_max()
        value = self._jitter_delta(max(-limit, min(limit, _as_int(delta, 0))))
        today = time.strftime("%Y-%m-%d")
        if rec["day"] != today:
            rec["day"] = today
            rec["day_gain"] = 0
        if value > 0:
            cap = self.daily_gain_cap()
            room = max(0, cap - rec["day_gain"]) if cap else value
            value = min(value, room)
            rec["day_gain"] += value
        rec["score"] = max(0, min(SCORE_MAX, rec["score"] + value))
        if (romance or confession or acceptance) \
                and (acceptance or looks_like_acceptance(reply_text)
                     or looks_like_romantic_reply(reply_text)):
            rec["romance"] = True
        # 历史点存放在原始记录上：get() 返回的规范化副本不含它
        raw_rec = self._by_user(character_key).get(self.scope_key(user_id, session_id))
        hist = raw_rec.get("history") if isinstance(raw_rec, dict) else None
        if not isinstance(hist, list):
            hist = []
        hist.append([round(time.time(), 3), rec["score"]])
        rec["history"] = hist[-HISTORY_MAX_POINTS:]
        # 这一轮得真的在谈关系才记伴侣：只看"关系性质已经是暧昧 + 回复里有答应字样"
        # 会把「本座就勉为其难地答应你好了」也算成确认交往——她答应的可能是别的事
        confirmed = confirms_partner(reply_text) \
            or ((confession or acceptance) and looks_like_acceptance(reply_text))
        if not rec["partner"] and reply_text and rec["romance"] and confirmed \
                and self._can_commit(rec, incoming_romance=incoming):
            allowed = self.max_partners()
            if allowed == 0 or len(self.partners(character_key)) < allowed:
                rec["partner"] = True
                if not rec.get("partner_since"):
                    rec["partner_since"] = time.time()
                print(f"关系进度：{character_key} 与 {user_id} 确立了恋人关系，已记为伴侣。")
            else:
                print(f"关系进度：伴侣数量已达上限（{allowed}），本次不记录。")
        rec["updated"] = time.time()
        self._store(character_key, user_id, rec, session_id)
        return rec

    # ---------------- 提示词 ----------------
    def build_note(self, ctx, user_id: str, label: str = "", session_id: str = "") -> str:
        """本轮注入的关系进度说明：给的是节奏与氛围，怎么演由角色自己决定。"""
        if not bool(self.config.get("affection_enabled", True)):
            return ""
        character_key = getattr(ctx, "character_key", "") or ""
        who = label or "当前发言者"
        st = self.state(character_key, user_id, session_id)
        stage = st["stage"]
        if stage == PARTNER_STAGE:
            guide = PARTNER_GUIDE
        elif stage == ROMANCE_STAGE:
            guide = ROMANCE_GUIDE
        else:
            guide = STAGE_GUIDE.get(stage, "")
        partners = self.partners(character_key)
        lines = [
            f"【关系进度】你与{who}目前是「{stage}」"
            f"（好感 {st['score']}/{SCORE_MAX}，亲近程度：{st['band']}）。",
            f"这个阶段的氛围：{guide}",
            "好感代表熟悉与信任，只随长期相处缓慢累积；"
            "好感高也不代表关系会变成恋爱："
            f"「{ROMANCE_STAGE}」要相处里真的出现恋爱意味（调情、心动、吃醋、表白）、"
            "而且你自己也动了心才会进入——对方单方面送点什么、说点情话，不算数；"
            f"你确实接受了对方的告白才会成为「{PARTNER_STAGE}」。"
            "只是聊得来、处得久的关系，好感再高也还是朋友或老交情。",
            "关系要一步一步来：对方一句热络的称呼不会让关系前进，"
            "你自己也不必因为一句话就把心交出去。",
            "答不答应对方的告白、要不要自己先开口，都由你按这段相处的实际情况和性格决定；"
            f"到了「{self.accept_min_stage()}」及以后就可以定下关系，在此之前先按上面的氛围相处。",
        ]
        if stage in (ROMANCE_STAGE, PARTNER_STAGE):
            lines.append("你也可以主动：心动时可以试探、撒娇、吃醋，甚至自己先表白；"
                         "被拒绝或对方冷淡，就退回原来的距离。")
        else:
            # 角色设定里常有"爱调情""嘴上不正经"这类描述，那是她熟了之后的样子；
            # 关系还没走到那一步时必须先按阶段来，否则刚认识就把气氛搞成暧昧。
            lines.append(
                f"在你与对方进入「{ROMANCE_STAGE}」之前，不论角色设定里怎么写你的说话风格"
                "（比如爱调情、嘴上不正经、把色情词语挂在嘴边），"
                "那些描述都还不适用：不要调情、不要说带性暗示的话、不要主动撩对方，"
                "也不要把气氛往暧昧上带；这一段的关系只按上面的氛围来。")
        soul = DIFFICULTY_SOUL.get(self.difficulty(), "")
        if soul:
            # 难度决定她有多"不好拿下"：这一点必须每轮都在场，否则模型只会按好感数字走
            lines.append(f"【你自己】{soul}")
        if partners:
            extra = "（不限人数）" if self.max_partners() == 0 else \
                f"（最多 {self.max_partners()} 位）"
            lines.append(f"你现在共有 {len(partners)} 位伴侣{extra}："
                         "可以与多人交往，后面有人向你表白时也不必因为已有伴侣就拒绝。")
        else:
            lines.append("你现在还没有伴侣。")
        days = self.partner_days(character_key, user_id, session_id)
        if days and days in self.anniversary_days():
            lines.append(f"今天是在一起的第 {days} 天纪念日，你记得这件事。")
        return " ".join(lines)


def commitment_warning(stage: str, accept_min_stage: str = DEFAULT_ACCEPT_MIN_STAGE) -> str:
    """阶段还没到时答应了交往的重生成提醒（只说明节奏，不规定台词）。"""
    return (
        f"提醒：你刚才直接答应了和对方的交往，但你们现在只是「{stage}」，"
        f"好感与相处都还没到能定下关系的「{accept_min_stage}」。请重新生成："
        "保持在角色里，可以心动、害羞、撒娇、试探，也可以说还需要时间，"
        "但不要现在就承认恋人关系，也不要把一句亲昵称呼当成关系已经确立。"
    )


def stage_label(manager: Optional[AffectionManager], character_key: str, user_id: str,
                session_id: str = "") -> str:
    """给 WebUI 展示用的阶段名（管理器不可用时返回空串）。"""
    if manager is None:
        return ""
    try:
        return manager.stage(character_key, user_id, session_id)
    except Exception:
        return ""
