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


def _parse_key(key: str) -> tuple:
    parts = str(key).split("::")
    if len(parts) >= 3:
        return "::".join(parts[:-2]), parts[-2], parts[-1]
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return "", "", key


class MoodManager:
    def __init__(self, data_path):
        self.file = Path(data_path) / "moods.json"
        self.records: dict = {}
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

    def _save(self):
        if self._load_failed:
            print("心情存档本次未能读取，已跳过保存以免覆盖磁盘上的原有内容。")
            return
        try:
            from .jsonio import save_json
            save_json(self.file, self.records)
        except Exception as e:
            print(f"保存心情存档失败: {e}")

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
        self.records[key] = rec
        self._save()

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
    """是否让 LLM 审判"这条消息要不要回复"。

    兼容旧配置：老版本没有 reply_judge_enabled 这个键，而是"填了提示词就等于开启"。
    因此显式开关为真、或者显式开关缺失但提示词已配置时，都算开启。
    """
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
    """是否更新/记录角色心情值（与"要不要回复"完全无关的独立开关）。

    兼容旧配置：老版本心情判定是搭在回复审判里的，没有独立开关。
    显式配置了 mood_enabled 就以它为准；没配时跟随"是否在跑审判"，
    保证升级前后行为一致。
    """
    try:
        raw = ctx.get("mood_enabled", None)
    except Exception:
        raw = None
    if raw is None or raw == "":
        return judge_enabled(ctx)
    return _truthy(raw)


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
    """风格约束是否生效：心情记录与风格映射都开启时才生效。"""
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
    """按心情值给出（档位名, 本轮风格指令）；未生效或心情正常时返回两个空串。

    档位边界直接复用「回复审判」的心情低迷下限/上限，让"心情低"在概率门控
    与语气表现两处含义一致，不必再单独配一套阈值。
    """
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


def current_mood(ctx, mood_mgr, session_id: str, user_id: str = "") -> float:
    """读取会话当前心情值；没有记录时用配置的初始心情值。"""
    lo, hi = mood_bounds(ctx)
    initial = clamp(_to_float(ctx.get("reply_judge_mood_initial", 60), 60.0), lo, hi)
    character_key = ctx.character_key or str(ctx.get("character_key", ""))
    return mood_mgr.get_mood(session_id, character_key, initial, user_id=user_id)


MOOD_PROMPT_CUSTOMIZED = "请判断对话记录中最后一条用户消息是否需要角色回复，" \
                         "并评估这条消息会让角色心情变化多少，然后按系统指令只输出JSON。"
MOOD_PROMPT_OFF = ("请判断对话记录中最后一条用户消息是否需要角色回复，"
                   "然后按系统指令只输出JSON（不要评估心情）。")


async def _ask_judge(ctx: RoleContext, mood_mgr: MoodManager, session_id: str,
                     user_text: str, history: list, user_id: str,
                     want_mood: bool) -> Optional[dict]:
    """向 LLM 询问一次 verdict；失败返回 None（调用方自行决定回退行为）。"""
    prompt = str(ctx.get("reply_judge_prompt", "") or "").strip()
    if not prompt:
        return None
    personality = str(ctx.get("personality_prompt", "") or "").strip()
    parts = [p for p in (personality, prompt) if p]
    system = "\n".join(parts) if parts else ""
    if not want_mood:
        system += ("\n【本次只需判断是否回复】不要评估心情，也不要输出 mood_delta / "
                   "mood_reason，只输出 {\"should_reply\": true 或 false}。")
    messages = [{"role": "system", "content": system}]
    messages.extend(build_merged_history(history, ctx))
    instruction = MOOD_PROMPT_CUSTOMIZED if want_mood else MOOD_PROMPT_OFF
    if str(user_text or "").strip():
        instruction = f"对话中最后一条用户消息是：{user_text}\n" + instruction
    messages.append({"role": "user", "content": instruction})
    result = await chat_once(ctx, messages)
    return extract_json(result.get("content") or "")


async def judge_and_decide(ctx: RoleContext, mood_mgr: MoodManager, session_id: str,
                           user_text: str, history: list, user_id: str = "") -> dict:
    """回复审判 +（可选）心情判定。

    两个开关彻底拆开：
      reply_judge_enabled —— 让 LLM 决定"这条要不要回"（本函数返回的 should_reply）；
      mood_enabled        —— 是否顺带判定心情变化量。
    只开心情、不开审判时，心情照常判定，但回复永远交给角色自己（不会被拦）。

    本函数只**判定**变化量（verdict["mood_delta"]），不落盘：
    变化量要等 LLM 回复生成之后由 commit_mood 落盘，本轮的回复概率与语气
    都使用更新前的心情值。
    """
    lo, hi = mood_bounds(ctx)
    character_key = ctx.character_key or str(ctx.get("character_key", ""))
    initial = _to_float(ctx.get("reply_judge_mood_initial", 60), 60.0)
    mood = mood_mgr.get_mood(session_id, character_key, clamp(initial, lo, hi), user_id=user_id)
    want_mood = mood_enabled(ctx)
    want_judge = judge_enabled(ctx)
    verdict = {"should_reply": True, "mood": mood, "mood_delta": None, "probability": 1.0,
               "gated": False, "llm_reply": True, "mood_enabled": want_mood,
               "judge_enabled": want_judge, "mood_committed": False}
    if not want_judge and not want_mood:
        return verdict                      # 两个开关都关：完全不调用 LLM
    try:
        obj = await _ask_judge(ctx, mood_mgr, session_id, user_text, history, user_id,
                               want_mood=want_mood)
    except Exception as e:
        print(f"回复审判调用失败: {type(e).__name__}: {e}")
        return verdict
    if not isinstance(obj, dict):
        return verdict
    should_reply, delta = parse_judge(obj, ctx)
    if want_mood:
        verdict["mood_delta"] = delta
        if not want_judge:
            return verdict                  # 只记心情，不拦回复
    probability = reply_probability(ctx, mood) if want_mood else 1.0
    if probability >= 1.0:
        decide = bool(should_reply)
    elif probability <= 0.0:
        decide = False
    else:
        decide = bool(should_reply) and random.random() < probability
    verdict.update({"should_reply": decide, "mood": mood, "probability": probability,
                    "gated": bool(should_reply and not decide), "llm_reply": bool(should_reply)})
    return verdict


def commit_mood(ctx, mood_mgr, session_id: str, verdict, user_id: str = "") -> Optional[float]:
    """把本轮审判判定的心情变化量落盘，返回更新后的心情值；无需更新时返回 None。

    一轮只落一次（verdict["mood_committed"] 标记）。调用点在 LLM 回复生成之后：
    本轮的语气与回复概率都用更新前的心情值判定，否则角色会拿"被这条消息改变之后"
    的心情去回应这条消息本身。
    """
    if not isinstance(verdict, dict) or verdict.get("mood_committed") \
            or not verdict.get("mood_enabled") or verdict.get("mood_delta") is None:
        return None
    verdict["mood_committed"] = True
    lo, hi = mood_bounds(ctx)
    character_key = ctx.character_key or str(ctx.get("character_key", ""))
    delta = _to_float(verdict.get("mood_delta"), 0.0)
    new_mood = clamp(_to_float(verdict.get("mood"), lo) + delta, lo, hi)
    mood_mgr.set_mood(session_id, character_key, new_mood, user_id=user_id)
    return new_mood
