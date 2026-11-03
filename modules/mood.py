import random
import time
from pathlib import Path
from typing import Optional

from .llm_helpers import RoleContext, build_merged_history, chat_once, extract_json


def _to_float(value, default: float) -> float:
    try:
        if isinstance(value, bool):
            return default
        return float(str(value).strip())
    except Exception:
        return default


def clamp(value: float, lo: float, hi: float) -> float:
    if hi < lo:
        hi = lo
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def mood_bounds(ctx) -> tuple:
    lo = _to_float(ctx.get("reply_judge_mood_min", 0), 0.0)
    hi = _to_float(ctx.get("reply_judge_mood_max", 100), 100.0)
    if hi < lo:
        hi = lo
    return lo, hi


def parse_judge(obj: Optional[dict], ctx) -> tuple:
    if not isinstance(obj, dict):
        return True, 0.0
    raw = obj.get("should_reply", True)
    if isinstance(raw, bool):
        should_reply = raw
    else:
        should_reply = str(raw).strip().lower() in ("true", "1", "yes", "y", "是", "需要")
    delta_max = abs(_to_float(ctx.get("reply_judge_mood_delta_max", 10), 10.0))
    delta = _to_float(obj.get("mood_delta", 0), 0.0)
    return should_reply, clamp(delta, -delta_max, delta_max)


def _parse_clock(value: str, default_minutes: int) -> int:
    parts = str(value or "").split(":")
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except (IndexError, TypeError, ValueError):
        return default_minutes


def env_bias(ctx) -> float:
    if not _truthy(ctx.get("mood_env_enabled", True)):
        return 0.0
    bias = 0.0
    lt = time.localtime()
    start = _parse_clock(ctx.get("mood_env_night_start", "00:00"), 0)
    end = _parse_clock(ctx.get("mood_env_night_end", "06:00"), 6 * 60)
    minute = lt.tm_hour * 60 + lt.tm_min
    in_night = (start <= minute < end) if start <= end \
        else (minute >= start or minute < end)
    if in_night:
        bias -= abs(_to_float(ctx.get("mood_env_night_drop", 5), 5.0))
    if lt.tm_wday >= 5:
        bias += abs(_to_float(ctx.get("mood_env_weekend_boost", 5), 5.0))
    return bias


def reply_probability(ctx, mood: float) -> float:
    lo = _to_float(ctx.get("reply_judge_mood_low", 30), 30.0)
    hi = _to_float(ctx.get("reply_judge_mood_high", 60), 60.0)
    prob_lo = clamp(_to_float(ctx.get("reply_judge_prob_low", 0.2), 0.2), 0.0, 1.0)
    prob_hi = clamp(_to_float(ctx.get("reply_judge_prob_high", 1.0), 1.0), 0.0, 1.0)
    mood = _to_float(mood, lo)
    if hi <= lo:
        p = prob_hi if mood >= lo else prob_lo
    elif mood <= lo:
        p = prob_lo
    elif mood >= hi:
        p = prob_hi
    else:
        p = prob_lo + (prob_hi - prob_lo) * (mood - lo) / (hi - lo)
        if p >= prob_hi - 1e-3:
            p = prob_hi
    return p


DEFAULT_REPLY_PROBABILITY = 0.5


def fixed_reply_probability(ctx) -> Optional[float]:
    try:
        raw = ctx.get("reply_probability_enabled", None)
    except Exception:
        raw = None
    if raw is None or raw == "" or not _truthy(raw):
        return None
    return clamp(_to_float(ctx.get("reply_probability", DEFAULT_REPLY_PROBABILITY),
                           DEFAULT_REPLY_PROBABILITY), 0.0, 1.0)


def _parse_key(key: str) -> tuple:
    parts = str(key).split("::")
    if len(parts) >= 3:
        return "::".join(parts[:-2]), parts[-2], parts[-1]
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return "", "", key


class MoodManager:
    HISTORY_MAX_POINTS = 500
    # 每个角色最多保留多少篇心情日记
    DIARY_MAX_ENTRIES = 200

    def __init__(self, data_path):
        self.file = Path(data_path) / "moods.json"
        self.diary_file = Path(data_path) / "mood_diary.json"
        self.records: dict = {}
        self.diary: dict = {}
        self._load_failed = False
        self._load()

    def _load(self):
        from .jsonio import load_json_ex
        data, readable = load_json_ex(self.file, {})
        self._load_failed = not readable
        if not isinstance(data, dict):
            print("心情存档顶层不是对象，已按空存档处理。")
            return
        self.records = data
        diary, diary_ok = load_json_ex(self.diary_file, {})
        self.diary = diary if isinstance(diary, dict) else {}

    def _save(self):
        if self._load_failed:
            print("心情存档本次未能读取，已跳过保存以免覆盖磁盘上的原有内容。")
            return
        try:
            from .jsonio import save_json
            save_json(self.file, self.records)
        except Exception as e:
            print(f"保存心情存档失败: {e}")

    def _save_diary(self):
        if self._load_failed:
            return
        try:
            from .jsonio import save_json
            save_json(self.diary_file, self.diary)
        except Exception as e:
            print(f"保存心情日记失败: {e}")

    @staticmethod
    def _key(session_id: str, character_key: str, user_id: str = "") -> str:
        if user_id:
            return f"{session_id}::{user_id}::{character_key}"
        return f"{session_id}::{character_key}"

    def get_mood(self, session_id: str, character_key: str, default: float,
                 user_id: str = "") -> float:
        rec = self.records.get(self._key(session_id, character_key, user_id))
        if not isinstance(rec, dict):
            return default
        return _to_float(rec.get("mood", default), default)

    def set_mood(self, session_id: str, character_key: str, mood: float,
                 user_id: str = ""):
        key = self._key(session_id, character_key, user_id)
        rec = self.records.get(key)
        if not isinstance(rec, dict):
            rec = {}
        rec["mood"] = mood
        rec["updated"] = time.time()
        hist = rec.get("history")
        if not isinstance(hist, list):
            hist = []
        hist.append([round(time.time(), 3), float(mood)])
        rec["history"] = hist[-self.HISTORY_MAX_POINTS:]
        self.records[key] = rec
        self._save()

    def decay_lonely(self, idle_days: int, drop: float, lo: float) -> int:
        cutoff = time.time() - max(1, int(idle_days)) * 86400
        drop = abs(float(drop or 0))
        if drop <= 0:
            return 0
        changed = 0
        for rec in self.records.values():
            if not isinstance(rec, dict) or float(rec.get("updated") or 0) >= cutoff:
                continue
            mood = _to_float(rec.get("mood", lo), lo)
            if mood <= lo:
                continue
            rec["mood"] = max(lo, mood - drop)
            changed += 1
        if changed:
            self._save()
        return changed

    def curve_points(self, character_key: str, session_id: str = "") -> list:
        target = str(session_id or "")
        pts = []
        for key, rec in self.records.items():
            session, _user_id, char = _parse_key(key)
            if char != character_key or not isinstance(rec, dict) \
                    or (target and session != target):
                continue
            for p in (rec.get("history") or []):
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    pts.append([float(p[0]), float(p[1])])
        pts.sort(key=lambda p: p[0])
        return pts

    def mood_points_for_day(self, character_key: str, day: str, session_id: str = "") -> list:
        start = 0.0
        try:
            start = time.mktime(time.strptime(day, "%Y-%m-%d"))
        except ValueError:
            return []
        end = start + 86400
        pts = []
        for key, rec in self.records.items():
            if not isinstance(rec, dict):
                continue
            sid, _user_id, char = _parse_key(key)
            if char != character_key or (session_id and sid != session_id):
                continue
            for p in (rec.get("history") or []):
                if isinstance(p, (list, tuple)) and len(p) >= 2 \
                        and start <= _to_float(p[0], 0.0) < end:
                    pts.append([_to_float(p[0], 0.0), _to_float(p[1], 0.0)])
        pts.sort(key=lambda p: p[0])
        return pts

    # ---------------- 心情日记 ----------------
    @staticmethod
    def _day_bounds(day: str) -> tuple:
        try:
            start = time.mktime(time.strptime(day, "%Y-%m-%d"))
        except ValueError:
            return 0.0, 0.0
        return start, start + 86400

    def sessions_for_day(self, character_key: str, day: str) -> list:
        start, end = self._day_bounds(day)
        if start <= 0:
            return []
        counts: dict = {}
        for key, rec in self.records.items():
            session_id, _user_id, char = _parse_key(key)
            if char != character_key or not isinstance(rec, dict):
                continue
            hits = sum(1 for p in (rec.get("history") or [])
                       if isinstance(p, (list, tuple)) and len(p) >= 2
                       and start <= _to_float(p[0], 0.0) < end)
            if hits:
                counts[session_id] = counts.get(session_id, 0) + hits
        return [sid for sid, _n in sorted(counts.items(), key=lambda kv: -kv[1])]

    def has_diary(self, character_key: str, date: str, session_id: str = "") -> bool:
        return any(entry.get("date") == date
                   and (not session_id
                        or str(entry.get("session_id") or "") == session_id)
                   for entry in (self.diary.get(character_key) or []))

    def drop_diary_session(self, session_id: str) -> int:
        target = str(session_id or "")
        if not target:
            return 0
        removed = 0
        for character_key, entries in list(self.diary.items()):
            rows = [e for e in (entries or []) if isinstance(e, dict)]
            keep = [e for e in rows if str(e.get("session_id") or "") != target]
            if len(keep) != len(rows):
                removed += len(rows) - len(keep)
                self.diary[character_key] = keep
        if removed:
            self._save_diary()
        return removed

    def add_diary(self, character_key: str, date: str, text: str,
                  summary: Optional[dict] = None, session_id: str = "",
                  sent: bool = False):
        entries = self.diary.get(character_key)
        if not isinstance(entries, list):
            entries = []
        entries = [e for e in entries if not (isinstance(e, dict)
                                              and e.get("date") == date
                                              and str(e.get("session_id") or "") == session_id)]
        entries.append({"date": date, "text": str(text or "").strip(),
                        "summary": summary or {}, "session_id": session_id,
                        "sent": bool(sent)})
        entries.sort(key=lambda e: (str(e.get("date", "")), str(e.get("session_id", ""))))
        self.diary[character_key] = entries[-self.DIARY_MAX_ENTRIES:]
        self._save_diary()

    def unsent_diaries(self, limit: int = 3) -> list:
        out = []
        for character_key, entries in self.diary.items():
            for entry in (entries or []):
                if isinstance(entry, dict) and entry.get("text") and not entry.get("sent"):
                    out.append((character_key, entry))
        out.sort(key=lambda item: str(item[1].get("date", "")))
        return out[:max(0, int(limit))]

    def mark_diary_sent(self, character_key: str, date: str, session_id: str = "") -> bool:
        changed = False
        for entry in (self.diary.get(character_key) or []):
            if not isinstance(entry, dict) or entry.get("date") != date:
                continue
            if str(entry.get("session_id") or "") != session_id:
                continue
            if not entry.get("sent"):
                entry["sent"] = True
                changed = True
        if changed:
            self._save_diary()
        return changed

    def get_diary(self, limit: int = 30, session_id: str = "") -> dict:
        sid = str(session_id or "")
        out = {}
        for character_key, entries in self.diary.items():
            if not isinstance(entries, list):
                continue
            rows = [e for e in entries if isinstance(e, dict) and e.get("text")
                    and (not sid or str(e.get("session_id") or "") == sid)]
            rows.sort(key=lambda e: str(e.get("date", "")), reverse=True)
            out[character_key] = rows[:max(1, int(limit or 30))]
        return out

    def recover_daily(self, session_id: str, character_key: str, initial: float,
                      amount: float, today: str, user_id: str = "") -> float:
        key = self._key(session_id, character_key, user_id)
        rec = self.records.get(key)
        if not isinstance(rec, dict):
            return initial
        mood = _to_float(rec.get("mood", initial), initial)
        last_day = time.strftime(
            "%Y-%m-%d", time.localtime(_to_float(rec.get("updated", 0), 0.0)))
        if last_day == today or mood >= initial:
            return mood
        mood = min(initial, mood + abs(amount))
        rec["mood"] = mood
        rec["updated"] = time.time()
        self.records[key] = rec
        self._save()
        return mood

    def delete_session(self, session_id: str):
        hit = False
        for key in [k for k in self.records if _parse_key(k)[0] == session_id]:
            del self.records[key]
            hit = True
        if hit:
            self._save()

    def role_moods(self) -> dict:
        counts: dict = {}
        latest: dict = {}
        for key, rec in self.records.items():
            if not isinstance(rec, dict):
                continue
            character_key = _parse_key(key)[2]
            ts = _to_float(rec.get("updated", 0), 0.0)
            mood = _to_float(rec.get("mood", 0), 0.0)
            counts[character_key] = counts.get(character_key, 0) + 1
            cur = latest.get(character_key)
            if cur is None or ts >= cur["updated"]:
                latest[character_key] = {"mood": mood, "updated": ts}
        return {k: {**latest[k], "sessions": counts.get(k, 0)} for k in latest}

    def role_mood_records(self) -> dict:
        out: dict = {}
        for key, rec in self.records.items():
            if not isinstance(rec, dict):
                continue
            session_id, user_id, character_key = _parse_key(key)
            out.setdefault(character_key, []).append({
                "session_id": session_id,
                "user_id": user_id,
                "mood": _to_float(rec.get("mood", 0), 0.0),
                "updated": _to_float(rec.get("updated", 0), 0.0),
            })
        for character_key, recs in out.items():
            indexed = list(enumerate(recs))
            indexed.sort(key=lambda x: (-float(x[1]["updated"]), -x[0]))
            out[character_key] = [r for _, r in indexed]
        return out


def _truthy(raw) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("true", "1", "yes", "y", "on", "是", "开启")


def judge_enabled(ctx) -> bool:
    try:
        raw = ctx.get("reply_judge_enabled", None)
    except Exception:
        return False
    if raw is None or raw == "":
        try:
            return bool(str(ctx.get("reply_judge_prompt", "") or "").strip())
        except Exception:
            return False
    return _truthy(raw)


def mood_enabled(ctx) -> bool:
    try:
        raw = ctx.get("mood_enabled", None)
    except Exception:
        raw = None
    if raw is None or raw == "":
        return judge_enabled(ctx)
    return _truthy(raw)


def affection_enabled(ctx) -> bool:
    try:
        raw = ctx.get("affection_enabled", None)
    except Exception:
        raw = None
    if raw is None or raw == "":
        return False
    return _truthy(raw)


DEFAULT_MOOD_REGRESS_RATE = 0.1
MOOD_STYLE_COLD = (
    "【心情影响语气·冷淡】你此刻心情偏低，提不起兴致：语气平淡、话少、略显敷衍，"
    "不要热情卖萌，也不要主动找话题或长篇大论。回复只允许 1~2 句话。"
)
MOOD_STYLE_IRRITATED = (
    "【心情影响语气·烦躁】你此刻心情很差，正处于不耐烦、烦躁的状态："
    "语气明显冷淡、没好气、带刺，可以抱怨、怼人、催促或敷衍，"
    "绝不温柔体贴，也不主动找话题。回复必须极短：只允许 1~2 句话，"
    "能一句说完就一句，禁止展开、举例、解释或补充说明。"
)


def _mood_style_active(ctx) -> bool:
    if not mood_enabled(ctx):
        return False
    try:
        raw = ctx.get("mood_style_enabled", None)
    except Exception:
        return True
    if raw is None or raw == "":
        return True
    return _truthy(raw)


def mood_style(ctx, mood) -> tuple:
    if not _mood_style_active(ctx):
        return "", ""
    lo, hi = mood_bounds(ctx)
    low = clamp(_to_float(ctx.get("reply_judge_mood_low", 30), 30.0), lo, hi)
    high = clamp(_to_float(ctx.get("reply_judge_mood_high", 60), 60.0), lo, hi)
    if high < low:
        high = low
    value = clamp(_to_float(mood, low), lo, hi)
    if value <= low:
        return "烦躁", MOOD_STYLE_IRRITATED
    if value < high:
        return "冷淡", MOOD_STYLE_COLD
    return "", ""


def _apply_daily_recover(ctx, mood_mgr, session_id: str, user_id: str) -> Optional[float]:
    if not mood_enabled(ctx) or not mood_mgr:
        return None
    amount = _to_float(ctx.get("mood_daily_recover", 0), 0.0)
    if amount <= 0:
        return None
    lo, hi = mood_bounds(ctx)
    initial = clamp(_to_float(ctx.get("reply_judge_mood_initial", 60), 60.0), lo, hi)
    character_key = ctx.character_key or str(ctx.get("character_key", ""))
    return mood_mgr.recover_daily(session_id, character_key, initial, amount,
                                  time.strftime("%Y-%m-%d"), user_id=user_id)


def stored_mood(ctx, mood_mgr, session_id: str, user_id: str = "") -> float:
    lo, hi = mood_bounds(ctx)
    initial = clamp(_to_float(ctx.get("reply_judge_mood_initial", 60), 60.0), lo, hi)
    recovered = _apply_daily_recover(ctx, mood_mgr, session_id, user_id)
    if recovered is not None:
        return recovered
    character_key = ctx.character_key or str(ctx.get("character_key", ""))
    return mood_mgr.get_mood(session_id, character_key, initial, user_id=user_id)


def current_mood(ctx, mood_mgr, session_id: str, user_id: str = "") -> float:
    lo, hi = mood_bounds(ctx)
    return clamp(stored_mood(ctx, mood_mgr, session_id, user_id=user_id)
                 + env_bias(ctx), lo, hi)


def _flag(obj: dict, key: str, words: tuple) -> bool:
    raw = obj.get(key, False)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("true", "1", "yes", "y") + words


def parse_affection(obj: Optional[dict], ctx) -> tuple:
    if not isinstance(obj, dict):
        return 0, False, False, False
    limit = abs(_to_float(ctx.get("affection_turn_delta_max", 3), 3.0)) or 3.0
    delta = _to_float(obj.get("affection_delta", 0), 0.0)
    confession = _flag(obj, "confession", ("是", "表白"))
    romance = _flag(obj, "romance", ("是", "暧昧"))
    acceptance = _flag(obj, "acceptance", ("是", "答应"))
    return clamp(delta, -limit, limit), confession, romance, acceptance


MOOD_PROMPT_CUSTOMIZED = "请判断对话记录中最后一条用户消息是否需要角色回复，" \
                         "并评估这条消息会让角色心情变化多少，然后按系统指令只输出JSON。"
MOOD_PROMPT_OFF = ("请判断对话记录中最后一条用户消息是否需要角色回复，"
                   "然后按系统指令只输出JSON（不要评估心情）。")

AFFECTION_FIELDS_NOTE = (
    "\n【本次输出字段】除 should_reply 外还要给出："
    "affection_delta —— 这条消息让你对当前发言者的好感变化，整数 -3~3："
    "真诚的关心、陪伴、分享、认真回应他在意的事为正；"
    "普通闲聊、闲聊式搭话、单纯索要为 0；冒犯、越界、贬低、敷衍为负；"
    "单纯喊老婆/老公/宝贝这类称呼本身不加分，最多 0。"
    "confession —— 这条消息是不是明确的表白或求婚（true/false），"
    "只换个亲密称呼不算表白。"
    "romance —— 你们之间的互动是否已经带上恋爱意味（调情、撩拨、吃醋、想亲近、"
    "心动、表白等，也包括你自己刚说过的话）：true/false；"
    "普通的关心、撒娇、玩笑、亲密称呼都不算。"
    "acceptance —— 这条消息是不是在明确答应或确认与你的恋人关系"
    "（同意交往、愿意做你的恋人、答应在一起等）：true/false；"
    "只是暧昧、调情、示好、亲密称呼都不算。"
    "breakup —— 这条消息是不是在明确结束关系（分手、绝交、别再联系、一刀两断等）："
    "true/false；只是闹别扭、生气、说气话都不算。"
    '只输出一个 JSON 对象，例如 {"should_reply": true, "mood_delta": 0, '
    '"affection_delta": 1, "confession": false, "romance": false, '
    '"acceptance": false, "breakup": false}，不要输出其它文字。'
)


async def _ask_judge(ctx: RoleContext, mood_mgr: MoodManager, session_id: str,
                     user_text: str, history: list, user_id: str,
                     want_mood: bool, want_affection: bool = False) -> Optional[dict]:
    prompt = str(ctx.get("reply_judge_prompt", "") or "").strip()
    if not prompt:
        return None
    personality = str(ctx.get("personality_prompt", "") or "").strip()
    parts = [p for p in (personality, prompt) if p]
    system = "\n".join(parts) if parts else ""
    if want_affection:
        system += AFFECTION_FIELDS_NOTE
        if not want_mood:
            system += "本次不评估心情，省略 mood_delta 即可。"
    elif not want_mood:
        system += ("\n【本次只需判断是否回复】不要评估心情，也不要输出 mood_delta / "
                   "mood_reason，只输出 {\"should_reply\": true 或 false}。")
    messages = [{"role": "system", "content": system}]
    messages.extend(build_merged_history(history, ctx))
    instruction = MOOD_PROMPT_CUSTOMIZED if want_mood else MOOD_PROMPT_OFF
    if str(user_text or "").strip():
        instruction = f"对话中最后一条用户消息是：{user_text}\n" + instruction
    messages.append({"role": "user", "content": instruction})
    result = await chat_once(ctx, messages, label="心情判定")
    return extract_json(result.get("content") or "")


async def judge_and_decide(ctx: RoleContext, mood_mgr: MoodManager, session_id: str,
                           user_text: str, history: list, user_id: str = "",
                           reply_floor: float = 0.0) -> dict:
    lo, hi = mood_bounds(ctx)
    base = stored_mood(ctx, mood_mgr, session_id, user_id=user_id)
    mood = clamp(base + env_bias(ctx), lo, hi)
    want_mood = mood_enabled(ctx)
    want_judge = judge_enabled(ctx)
    want_affection = affection_enabled(ctx)
    fixed_prob = fixed_reply_probability(ctx)
    verdict = {"should_reply": True, "mood": mood, "mood_base": base,
               "mood_delta": None, "probability": 1.0,
               "gated": False, "llm_reply": True, "roll": None,
               "mood_enabled": want_mood,
               "judge_enabled": want_judge, "mood_committed": False,
               "affection_enabled": want_affection, "affection_delta": None,
               "confession": False, "romance": False, "acceptance": False,
               "breakup": False, "affection_committed": False}
    if not want_judge and not want_mood and not want_affection and fixed_prob is None:
        return verdict                      # 全部关闭：完全不调用 LLM
    should_reply = True
    if want_judge or want_mood or want_affection:
        try:
            obj = await _ask_judge(ctx, mood_mgr, session_id, user_text, history, user_id,
                                   want_mood=want_mood, want_affection=want_affection)
        except Exception as e:
            print(f"回复审判调用失败: {type(e).__name__}: {e}")
            return verdict
        if not isinstance(obj, dict):
            return verdict
        should_reply, delta = parse_judge(obj, ctx)
        if want_affection:
            aff_delta, confession, romance, acceptance = parse_affection(obj, ctx)
            verdict["affection_delta"] = aff_delta
            verdict["confession"] = confession
            verdict["romance"] = romance
            verdict["acceptance"] = acceptance
            verdict["breakup"] = _flag(obj, "breakup", ("是", "分手", "绝交"))
        if want_mood:
            verdict["mood_delta"] = delta
            if not want_judge and fixed_prob is None:
                return verdict              # 只记心情，不拦回复
    if fixed_prob is not None:
        probability = fixed_prob
    else:
        probability = reply_probability(ctx, mood) if want_mood else 1.0
    if probability > 0 and reply_floor > 0:
        probability = min(1.0, max(probability, reply_floor))
    if verdict.get("breakup"):
        probability = 1.0
    if probability >= 1.0:
        decide = bool(should_reply)
    elif probability <= 0.0:
        decide = False
    else:
        # 会被当成概率是假的
        verdict["roll"] = random.random()
        decide = bool(should_reply) and verdict["roll"] < probability
    verdict.update({"should_reply": decide, "mood": mood, "probability": probability,
                    "gated": bool(should_reply and not decide), "llm_reply": bool(should_reply)})
    return verdict


def mood_regress_rate(ctx) -> float:
    rate = _to_float(ctx.get("mood_regress_rate", DEFAULT_MOOD_REGRESS_RATE),
                     DEFAULT_MOOD_REGRESS_RATE)
    return min(1.0, max(0.0, rate))


def commit_mood(ctx, mood_mgr, session_id: str, verdict, user_id: str = "") -> Optional[float]:
    if not isinstance(verdict, dict) or verdict.get("mood_committed") \
            or not verdict.get("mood_enabled") or verdict.get("mood_delta") is None:
        return None
    verdict["mood_committed"] = True
    lo, hi = mood_bounds(ctx)
    character_key = ctx.character_key or str(ctx.get("character_key", ""))
    delta = _to_float(verdict.get("mood_delta"), 0.0)
    base = _to_float(verdict.get("mood_base", verdict.get("mood")), lo)
    new_mood = clamp(base + delta, lo, hi)
    initial = clamp(_to_float(ctx.get("reply_judge_mood_initial", 60), 60.0), lo, hi)
    regress = (initial - new_mood) * mood_regress_rate(ctx)
    if abs(regress) >= 0.05:
        new_mood = clamp(new_mood + regress, lo, hi)
        verdict["mood_regress"] = new_mood - clamp(base + delta, lo, hi)
    else:
        verdict["mood_regress"] = 0.0
    mood_mgr.set_mood(session_id, character_key, new_mood, user_id=user_id)
    return new_mood
