"""待办/提醒管理：正则或 LLM 提取 → SQLite 存储 → 到点通过调度器发送提醒。

所有正则模式、关键词、提醒话术、检查行为均可在 WebUI 配置，无硬编码。
"""
import re
import time
from typing import Dict, List, Optional, Tuple

from .database import DatabaseManager
from .scheduler import SchedulerManager
from .llm_helpers import RoleContext, generate_json_reply

# 时段前缀（下午3点 / 晚上11点 等）+ 点分时间（支持中文数字与「半」点）
_CN_NUM = r"[一二两三四五六七八九十]+"
# 分钟部分要么是「半」「N分」，要么不存在 —— 不用 \d{0,2}（它能匹配空串，
# 会让后续的「可以/提醒/叫我」被回溯吃进时间表达式里，导致整条匹配失败）
_TODAY_MIN = r"(?:半|\d{1,2}\s*分)?"
_TODAY_CLOCK = (r"(?:凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里)?\s*"
                rf"(?:\d{{1,2}}|{_CN_NUM})\s*[点时:：]\s*" + _TODAY_MIN)
_TODAY_HHMM = _TODAY_CLOCK
# 相对时长（30分钟后 / 半小时之后 / 两小时后 / 3天后，数字支持中文写法）
_TODAY_DUR = (rf"(?:(?:\d{{1,3}}|{_CN_NUM})\s*分钟|"
              rf"(?:\d{{1,3}}|{_CN_NUM})\s*个?小时|"
              rf"(?:\d{{1,3}}|{_CN_NUM})\s*天|"
              rf"半\s*个?小时)(?:之|以)?[後后]")
# 「提醒/叫我」前面允许插入的请求语（"8点半**可以**叫我起床嘛"里就夹着"可以"）
_POLITE = r"(?:可以|能不能|能|可否|帮忙|帮我|请|麻烦)?"
# 事项部分：动作 + 具体事项；"8点叫我"这类没说要做什么时靠 ACTION 从句子里取
_TODO_ACTION = r"(?:起床|叫醒)"

DEFAULT_TODO_PATTERNS = [
    rf"(?:提醒|记得)(?:我)?\s*(?:在|于)?\s*"
    rf"({_TODAY_HHMM}|{_TODAY_DUR}|(?:明天|明日)\s*{_TODAY_HHMM})[,，。\s]*(.+)",
    rf"((?:明天|明日)?\s*{_TODAY_HHMM})\s*{_POLITE}\s*"
    rf"(?:提醒|叫我|提醒我|叫|提醒一下)[,，。\s]*(.+)?",
    rf"({_TODAY_DUR})\s*{_POLITE}\s*"
    rf"(?:提醒|叫我|提醒我|叫|提醒一下)[,，。\s]*(.+)?",
    # 裸分钟（"30分提醒我喝水"）：指当前小时的第 30 分，不是 30 分钟后；
    # (?![钟后後]) 防止把"30分钟后/30分钟"误当成裸分钟
    rf"((?:\d{{1,2}}|{_CN_NUM})\s*分(?![钟后後]))\s*{_POLITE}\s*"
    rf"(?:提醒|叫我|提醒我|叫|提醒一下)[,，。\s]*(.+)?",
]

DEFAULT_TODO_KEYWORDS = ["提醒", "待办", "别忘了", "记得", "叫我"]

DEFAULT_EXTRACT_PROMPT = (
    "你是待办提取助手。判断用户消息是否包含一个明确的提醒/待办事项。"
    "如果有，输出JSON：{\"has_todo\": true, \"content\": \"要提醒的事项\", "
    "\"time\": \"HH:MM\"}；如果没有明确的提醒事项，输出 {\"has_todo\": false}。\n"
    "【content 铁律 —— 必须自己转述，禁止照抄指令本身】\n"
    "1. content 是「到点之后要让主人去做的那件事」，必须是一个具体、可执行的事项"
    "（如「睡觉」「吃药」「开会」「喝水」）。\n"
    "2. 严禁把提醒指令本身当成事项！「提醒我」「再提醒我」「叫我」「别忘了」"
    "「记得」「到时候」「提醒一下」这类字眼本身**不是**待办内容，"
    "出现这种无意义结果时必须结合上下文自己判断主人真正要做的是什么。\n"
    "3. **content 里绝不能出现时间**！「两分钟后」「8点半」「明天早上」都是**时间**，"
    "必须写进 time / delay_minutes 字段，绝不能当成事项内容。"
    "反例（严禁）：{\"content\": \"两分钟后\"}；"
    "正例：主人说「8点半叫我起床」→ {\"content\": \"起床\", \"time\": \"08:30\"}。\n"
    "4. 主人只是说「N分钟后再提醒我」「到点提醒我」而没说做什么时，"
    "必须回头看他**上一条消息**里说要做什么事，把那个事当作 content。\n"
    "   例：主人先说「我要睡觉了」，接着说「5分钟后再提醒我吧」→ "
    "{\"has_todo\": true, \"content\": \"睡觉\", \"delay_minutes\": 5}。\n"
    "5. 如果上下文里也找不到任何具体事项，就自己把这件事转述成一句可执行的话"
    "（如「去休息」「准备出门」），绝对不要把「再提醒我」原样写进 content；"
    "实在无法判断时输出 {\"has_todo\": false}。\n"
    "time 字段规则（必须严格遵守）：\n"
    "1. time 必须照抄用户说出的那个时刻，写成24小时制 HH:MM，例如："
    "\"23点\"→\"23:00\"，\"晚上11点\"→\"23:00\"，\"下午3点半\"→\"15:30\"，"
    "\"早上8点半\"→\"08:30\"，\"明天早上8点\"→\"08:00\"，"
    "\"晚上12点/夜里12点\"→\"00:00\"。\n"
    "2. 禁止自己计算分钟差或时间戳：系统知道当前时间，会把 HH:MM 解析到最近一次到来的该时刻，"
    "你只负责把用户说的钟点原样转成 HH:MM。"
    "**只要用户说出了具体钟点（几点几分/几点半），就必须用 time，"
    "绝对不许换算成 delay_minutes**（现在是凌晨 1 点、用户说「早上8点半」时，"
    "正确输出是 {\"time\": \"08:30\"}，绝不是 {\"delay_minutes\": 450}）。\n"
    "3. 只有用户明确说了「N分钟后 / N小时后」这类相对时长、且没提任何钟点时，"
    "才改用 {\"delay_minutes\": N}（整数分钟），此时不要再输出 time。"
    "用户没说过的时长一律不许编造。\n"
    "4. 「N分」后面没有「后/之后」时，指**当前小时的第 N 分**，不是 N 分钟后："
    "现在 22:15，用户说「30分提醒我」指 22:30，输出 {\"time\": \"22:30\"}；"
    "如果说的是「30分钟后提醒我」才是相对时长，输出 {\"delay_minutes\": 30}。\n"
    "只输出JSON。"
)

# 无论用户在 WebUI 里填了什么自定义提示词，都会在其后**强制追加**这段硬约束。
# 背景：config.json 里一旦存过一份提示词（哪怕是旧版默认值），
# TodoManager 就永远用它，代码里改进过的默认提示词再也进不去 ——
# 用户的旧提示词缺了「content 不许写时间」「有钟点就必须用 time」这两条，
# 于是出现 content="两分钟后"、delay_minutes=2 这种既编造时间又丢失事项的结果。
EXTRACT_HARD_RULES = (
    "\n\n【系统硬性约束 —— 必须先满足，与你上面的指令冲突时以本节为准】\n"
    "1. content 只写「要让主人去做的那件事」，绝不允许是时间短语。"
    "「两分钟后」「8点半」「明天早上」「一会儿」都是时间，不是事项；"
    "把时间写进 content 属于严重错误。\n"
    "2. 用户说出的钟点（几点 / 几点半 / HH:MM）必须原样写进 time 字段"
    "（24 小时制 HH:MM），例如「早上8点半」→ {\"time\": \"08:30\"}。"
    "只要出现了钟点，就禁止改写成 delay_minutes —— 绝不许自己算分钟差。\n"
    "3. delay_minutes 只在用户**原话里明确出现**「N分钟后 / N小时后 / 半小时后」"
    "这类相对时长时才允许使用；用户没说过这个时长就绝不许编造。\n"
    "4. 「叫我/提醒我/记得」后面的动词才是事项："
    "「叫我起床」→ content 是「起床」；「提醒我吃药」→ content 是「吃药」。\n"
    "5. 时间与事项都拿不准时输出 {\"has_todo\": false}，宁可漏提醒也不要编造。\n"
)

# "提醒指令本身"的黑名单：单独出现时不是待办内容
MEANINGLESS_TODO_WORDS = (
    "再提醒我", "提醒我", "提醒一下", "提醒下", "提醒", "叫我", "记得",
    "别忘了", "别忘记", "到时候", "到点", "记得提醒", "提醒这件事", "这件事",
    "一下", "那个", "这个", "事情", "东西",
)
# "像是提醒请求"的粗判据（仅用于日志提示，不参与提取决策）
_TODAY_REMIND_INTENT_RE = re.compile(r"提醒|叫我|喊我|记得|别忘了|闹钟|叫我起床")
# 上下文里"主人打算做什么"的线索词（用于把「再提醒我」还原成真正的事项）
_TODO_CONTEXT_PATTERNS = (
    re.compile(r"我(?:要|要去|得|该|准备|打算|想)\s*([^\s，。！？,.!?]{1,12})"),
    re.compile(r"准备\s*([^\s，。！？,.!?]{1,12})"),
    re.compile(r"该\s*([^\s，。！？,.!?]{1,12})了"),
    re.compile(r"去\s*([^\s，。！？,.!?]{1,12})了"),
)


def todo_content_is_meaningless(content: str) -> bool:
    """判断提取出来的待办内容是不是"提醒指令本身"（如「再提醒我」）。"""
    text = re.sub(r"[\s，。！？,.!?、~～]+", "", str(content or ""))
    if not text:
        return True
    if text in MEANINGLESS_TODO_WORDS:
        return True
    # 整句只剩"提醒/叫我/记得"+代词/语气词时，同样是空指令
    residue = text
    for word in ("再提醒我", "提醒我", "提醒一下", "提醒下", "提醒", "叫我",
                 "记得", "别忘了", "别忘记", "到时候", "到点", "一下"):
        residue = residue.replace(word, "")
    residue = residue.strip("我的了这件事东西吧呢啊呀哦嘛哈")
    return not residue


def content_from_context(history_lines: list) -> str:
    """从最近几条对话里还原"主人真正要做的那件事"。

    场景：主人先说「我要睡觉了」，再说「5分钟后再提醒我吧」——
    提取到的字面内容只有"再提醒我"，必须回头找上下文才知道要提醒的是"睡觉"。
    """
    for line in reversed(list(history_lines or [])):
        text = str(line or "").strip()
        if not text:
            continue
        for pattern in _TODO_CONTEXT_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            candidate = match.group(1).strip("我的了这件事东西，。！？,.!?")
            if candidate and not todo_content_is_meaningless(candidate) \
                    and len(candidate) <= 12:
                return candidate
    return ""


# ---------------------------------------------------------------------------
# content 里混进"时间短语"的拦截：模型把时间当成事项时救回来
# ---------------------------------------------------------------------------
# 事故背景：主人说「早上8点半可以叫我起床嘛～」，模型却返回
# {"content": "两分钟后", "delay_minutes": 2} —— 事项没了、时间还是编的。
# 这里做两道拦截：
#   ① 纯时间短语不许当作事项；
#   ② 相对时长必须能在用户原话里找到（防止模型凭空算分钟差）。
_TODO_REL_DUR_RE = re.compile(
    rf"(?:(\d{{1,3}}|{_CN_NUM})\s*分钟|(\d{{1,3}}|{_CN_NUM})\s*个?小时|"
    rf"(\d{{1,3}}|{_CN_NUM})\s*天|半\s*个?小时)")
# "N分"（不带"钟"字，也不需要"后"）：指当前小时的第 N 分，同样算用户说了时间
_TODO_BARE_MIN_RE = re.compile(rf"(?<![\d])(\d{{1,2}}|{_CN_NUM})\s*分(?![钟后後])")

# 整条 content 只是时间描述（可以带连接词/语气词）
_TODO_TIME_ONLY_TAIL = r"(?:之后|以后|后|後|左右|以内|内|啦|了|吧|呢|啊|呀|的|再|就|可以|可以吗|吗)*"
# 日期前缀（今天/明天/后天/大后天/昨天）可选
_TODO_TIME_ONLY_DATE = r"(?:今天|明天|明日|后天|大后天|昨天|当天)?"
_TODO_TIME_ONLY_RES = (
    # 钟点：早上8点 / 8点半 / 08:30 / 晚上11点
    re.compile(
        r"^" + _TODO_TIME_ONLY_DATE + r"\s*"
        r"(?:凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里)?\s*"
        r"(?:\d{1,2}|" + _CN_NUM + r")\s*[点时:：]"
        r"\s*(?:半|\d{1,2}\s*分?|\d{0,2})?" + _TODO_TIME_ONLY_TAIL + r"$"),
    # 相对时长：半小时后 / 两小时后 / 5分钟后 / 3天后
    re.compile(r"^(?:半\s*个?小时|\d{1,3}\s*个?小时|" + _CN_NUM + r"\s*个?小时|"
               r"\d{1,3}\s*分钟|" + _CN_NUM + r"\s*分钟|"
               r"\d{1,3}\s*天|" + _CN_NUM + r"\s*天)" + _TODO_TIME_ONLY_TAIL + r"$"),
    # 裸分钟："30分"（不带"钟"字，也允许"分钟"）
    re.compile(r"^(\d{1,2}|" + _CN_NUM + r")\s*分(?:钟)?" + _TODO_TIME_ONLY_TAIL + r"$"),
    # 只有日期没有钟点："明天早上" / "今天晚上" / "后天"
    re.compile(r"^" + _TODO_TIME_ONLY_DATE
               + r"(?:凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里)?"
               + _TODO_TIME_ONLY_TAIL + r"$"),
)


def content_is_time_phrase(content: str) -> bool:
    """判断提取到的事项是不是"纯时间描述"（如「两分钟后」「8点半」）。"""
    text = re.sub(r"[\s\u3000]+", "", str(content or "")).strip("，。！？,.!?、~～")
    if not text:
        return False
    return any(p.match(text) for p in _TODO_TIME_ONLY_RES)


def relative_duration_mentioned(text: str) -> bool:
    """用户原话里是否真的说过相对时长（N分钟后 / 半小时后 / N分）。"""
    raw = str(text or "")
    return bool(_TODO_REL_DUR_RE.search(raw) or _TODO_BARE_MIN_RE.search(raw))


# "叫我X/提醒我X"里的 X 才是事项：从用户原话里直接取出来
_TODO_ACTION_PATTERNS = (
    re.compile(r"(?:提醒|叫)(?:我|一下|我一下)?\s*([^\s，。！？,.!?、~～]{1,12})"),
    re.compile(r"记得\s*([^\s，。！？,.!?、~～]{1,12})"),
    re.compile(r"别忘了\s*([^\s，。！？,.!?、~～]{1,12})"),
)
# 动词后紧跟的虚词/时间词不算事项
_ACTION_NOISE_RE = re.compile(
    r"^(?:一下|一声|我|你|他|她|的|了|吧|呢|啊|呀|嘛|哦|吗|么|个|"
    r"再|又|就|要|会|能|可以|可以吗|好|好了|了|"
    r"在|于|到|点|时|分|钟|半|分钟|小时|天|今天|明天|后天|昨天|"
    r"凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里)(.*)$")


def content_from_user_text(user_text: str) -> str:
    """从用户本条消息里直接取出"要提醒的那件事"（"叫我起床"→"起床"）。"""
    raw = str(user_text or "")
    for pattern in _TODO_ACTION_PATTERNS:
        for match in pattern.finditer(raw):
            candidate = match.group(1).strip("，。！？,.!?、~～")
            # 反复剥掉紧跟动词的虚词/时间词（"叫我一下起床" → "起床"）
            for _ in range(4):
                stripped = _ACTION_NOISE_RE.sub(r"\1", candidate)
                if stripped == candidate:
                    break
                candidate = stripped
            candidate = clean_todo_content(candidate)
            if candidate and len(candidate) <= 12 \
                    and not content_is_time_phrase(candidate) \
                    and not todo_content_is_meaningless(candidate):
                return candidate
    return ""


# 事项里该剥掉的语气词/连接词（只在首尾剥，句中的语义内容一律保留）
_TODO_LEADING_NOISE_RE = re.compile(
    r"^(?:嘿嘿|哈哈|嘻嘻|嗯+|哦+|那个|这个|然后|接着|而且|所以|但是|不过|就|那就|那|"
    r"可以|能不能|能|可否|帮忙|帮我|请|麻烦|顺便|一下|我|你|主人|嘛|呀|啊|哦|"
    r"吧|呢|的|了|再|又|记得|别忘了)+")
_TODO_TRAILING_NOISE_RE = re.compile(
    r"(?:嘛|吗|吧|呢|啊|呀|哦|喔|哈|啦|了|的|么|好不好|可以吗|行吗|"
    r"谢谢|多谢|拜托|求你了|～|~|！|!|。|\.|，|,|、)+$")


def clean_todo_content(content: str) -> str:
    """把事项两端粘着的语气词、称呼、请求语剥掉（"起床嘛～" → "起床"）。"""
    text = str(content or "").strip()
    for _ in range(4):
        before = text
        text = _TODO_LEADING_NOISE_RE.sub("", text)
        text = _TODO_TRAILING_NOISE_RE.sub("", text)
        text = text.strip(" \u3000，。！？,.!?、~～:：;；\"'“”‘’（）()【】")
        if text == before:
            break
    # 全被剥光说明原本就是纯语气词：返回空串（调用方按"没事项"处理）
    return text


# 中文数字映射（一~九），两 归二
_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_num_to_int(text: str) -> Optional[int]:
    """中文数字转整数（一~九十九：十/十五/二十/二十三…）；无法解析返回 None。"""
    text = (text or "").strip()
    if not text:
        return None
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CN_DIGITS.get(left) if left else 1
        ones = _CN_DIGITS.get(right) if right else 0
        if (left and tens is None) or (right and ones is None):
            return None
        return tens * 10 + ones
    if len(text) == 1:
        return _CN_DIGITS.get(text)
    return None


def _expr_num(token: str) -> Optional[int]:
    """时间表达式里的数量词：阿拉伯数字或中文数字。"""
    token = (token or "").strip()
    if token.isdigit():
        return int(token)
    return _cn_num_to_int(token)


class TodoManager:
    def __init__(self, config, db: DatabaseManager, scheduler: SchedulerManager,
                 sender=None, emotions_provider=None):
        self.config = config
        self.db = db
        self.scheduler = scheduler
        self.sender = sender
        self.emotions_provider = emotions_provider  # 返回当前角色 emotions dict
        self.ctx_provider = None                    # 返回当前角色 RoleContext

    # ---------------- 提取 ----------------
    def _patterns(self) -> List[re.Pattern]:
        raw = self.config.get("todo_regex_patterns", DEFAULT_TODO_PATTERNS)
        if isinstance(raw, str):
            raw = [line for line in raw.splitlines() if line.strip()]
        patterns = []
        for p in raw or DEFAULT_TODO_PATTERNS:
            try:
                patterns.append(re.compile(p))
            except re.error as e:
                print(f"待办正则无效，已跳过: {p} ({e})")
        return patterns

    def _parse_time_expr(self, expr: str, now: float) -> Optional[float]:
        expr = str(expr).strip()
        m = re.search(rf"((?:\d{{1,3}}|{_CN_NUM}))\s*分钟(?:之|以)?[后後]", expr)
        if m:
            n = _expr_num(m.group(1))
            if n:
                return now + n * 60
        m = re.search(rf"((?:\d{{1,3}}|{_CN_NUM}))\s*个?小时(?:之|以)?[后後]", expr)
        if m:
            n = _expr_num(m.group(1))
            if n:
                return now + n * 3600
        if re.search(r"半\s*个?小时", expr):
            return now + 1800
        m = re.search(rf"((?:\d{{1,3}}|{_CN_NUM}))\s*天(?:之|以)?[后後]", expr)
        if m:
            n = _expr_num(m.group(1))
            if n:
                return now + n * 86400
        # 裸分钟（"30分提醒我喝水"）：指**当前小时的第 30 分**，不是"30分钟后"。
        # 已过（现在 22:45 说"30分"）就顺延到下一小时的第 30 分（23:30）。
        # 只 fullmatch 纯"N分"，"30分钟后/30分钟"在前面分支已处理，不会走到这里。
        m = re.fullmatch(rf"\s*(?:({_CN_NUM})|(\d{{1,2}}))\s*分\s*", expr)
        if m:
            minute = _expr_num(m.group(1) or m.group(2))
            if minute is not None and 0 <= minute <= 59:
                lt = time.localtime(now)
                candidate = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                         lt.tm_hour, minute, 0, 0, 0, -1))
                if candidate <= now:
                    candidate += 3600
                return candidate
        tomorrow = "明天" in expr or "明日" in expr
        m = re.search(r"(凌晨|早上|早晨|上午|中午|下午|傍晚|晚上|夜里)?\s*((?:\d{1,2}|[一二两三四五六七八九十]{1,3}))\s*[点时:：]\s*(半|\d{1,2}\s*分?|\d{0,2})?", expr)
        if m:
            hour = _expr_num(m.group(2))
            if hour is None:
                return None
            # 时段换算：下午/傍晚/晚上/夜里 +12h（凌晨/上午不加）；
            # 「晚上12点/夜里12点」是午夜，把 12 归零（解析到最近一次到来的 0 点）
            if m.group(1) in ("下午", "傍晚", "晚上", "夜里") and hour < 12:
                hour += 12
            elif m.group(1) in ("晚上", "夜里") and hour == 12:
                hour = 0
            minute_part = m.group(3) or ""
            if "半" in minute_part:
                minute = 30
            else:
                digits = re.sub(r"\D", "", minute_part)
                minute = int(digits) if digits else 0
            # 越界值直接判失败（如模型输出的 "23:99"）：
            # mktime 会把 99 分静默进位到次日 0 点，提醒时间凭空跨天。
            if not (0 <= hour <= 24 and 0 <= minute <= 59):
                print(f"[待办提取] 时间表达式越界，已拒绝解析: {expr!r}（时={hour} 分={minute}）")
                return None
            lt = time.localtime(now)
            candidate = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1))
            if tomorrow:
                # 「明天X点」无条件指向明天的该时刻（哪怕今天的还没到）；
                # 之前先做"已过则+1天"再加明天，会出现"说了明天却排到后天"的错位
                candidate += 86400
            elif candidate <= now:
                candidate += 86400
            return candidate
        return None

    @staticmethod
    def _keyword_list(keywords) -> List[str]:
        """配置可能是多行字符串（WebUI 保存格式）或列表，统一成词列表。

        不能直接迭代字符串——那会按单字符匹配，关键词门槛形同虚设。
        """
        if isinstance(keywords, str):
            return [kw.strip() for kw in keywords.splitlines() if kw.strip()]
        if isinstance(keywords, list):
            return [str(kw).strip() for kw in keywords if str(kw).strip()]
        return []

    def extract_sync(self, text: str) -> List[Tuple[str, float]]:
        """正则模式提取，返回 [(content, remind_ts), ...]。

        提取到的内容若只是"提醒指令本身"（"再提醒我吧"）会被丢弃 ——
        这类字眼不是事项，发出来只会变成「快去做『再提醒我』这件大事吧」。
        （正则模式没有上下文可依据，只能丢弃；llm 模式会回头按上下文还原。）
        """
        found = []
        now = time.time()
        keywords = self._keyword_list(self.config.get("todo_keywords", DEFAULT_TODO_KEYWORDS))
        if keywords and not any(kw in text for kw in keywords):
            # 关键词门槛把明显是提醒请求的消息挡在门外时留一条日志：
            # 否则"设了提醒却一句话都没有"完全无从排查（例如把「叫我」从
            # 关键词里删掉后，「8点半叫我起床」这类消息会静默漏掉）。
            if _TODAY_REMIND_INTENT_RE.search(str(text or "")):
                print(f"[待办提取] 消息看起来是提醒请求，但未命中触发关键词 "
                      f"（当前关键词：{'、'.join(keywords)}），已跳过：{str(text)[:40]!r}")
            return found
        for pattern in self._patterns():
            for m in pattern.finditer(text):
                try:
                    time_expr, content = m.group(1), m.group(2)
                except (IndexError, AttributeError):
                    print(f"待办正则需包含两个捕获组（时间、内容）: {pattern.pattern}")
                    break
                remind_ts = self._parse_time_expr(time_expr, now)
                content = (content or "").strip()[:200]
                # 循环剥离提醒内容开头连续的冗余代词/动词（"提醒我喝水"→"喝水"、
                # "叫我叫我起床"→"起床"），让提醒话术自然，也提升去重命中率
                while content:
                    stripped = re.sub(r"^(?:再|又|重复)?(?:提醒我|叫我|提醒|记得|我)", "",
                                      content).strip()
                    if stripped == content:
                        break
                    content = stripped
                # 剥掉两端粘着的语气词/称呼（"起床嘛～"→"起床"）
                content = clean_todo_content(content)
                if remind_ts and content:
                    if todo_content_is_meaningless(content):
                        print(f"[待办提取] 正则提取到的「{content}」只是提醒指令本身，"
                              "不是具体事项，已丢弃（可在 WebUI 改用 llm 提取模式按上下文还原）")
                        continue
                    if content_is_time_phrase(content):
                        print(f"[待办提取] 正则提取到的「{content}」是时间而非事项，已丢弃")
                        continue
                    found.append((content, remind_ts))
        # 去重：多个正则可能同时命中同一句提醒（如「记得在8点提醒我吃药」
        # 会提取出「提醒我吃药」和「我吃药」），同一时间点仅保留最长内容
        best: Dict[float, str] = {}
        for content, ts in found:
            if ts not in best or len(content) > len(best[ts]):
                best[ts] = content
        seen, out = set(), []
        for content, ts in found:
            if ts in best and best[ts] == content and ts not in seen:
                seen.add(ts)
                out.append((content, ts))
        return out

    async def extract_llm(self, ctx: RoleContext, text: str,
                          recent_context: List[str] = None) -> List[Tuple[str, float]]:
        """LLM 提取待办；recent_context 是最近几条对话（用于还原"再提醒我"指什么）。

        无论用户在 WebUI 里填了什么自定义提示词，都会额外追加 EXTRACT_HARD_RULES：
        提示词存在 config.json 里，一旦存过就把代码里的默认提示词顶掉了，
        所以关键约束必须由系统强制追加，否则老配置用户永远拿不到修复。
        """
        now = time.time()
        lt = time.localtime(now)
        keywords = self._keyword_list(self.config.get("todo_keywords", DEFAULT_TODO_KEYWORDS))
        if keywords and not any(kw in text for kw in keywords):
            return []
        prompt = str(self.config.get("todo_extract_prompt", "") or DEFAULT_EXTRACT_PROMPT)
        if EXTRACT_HARD_RULES.strip() not in prompt:
            prompt = prompt + EXTRACT_HARD_RULES
        weekday = "周" + "一二三四五六日"[lt.tm_wday % 7]
        system = (f"{prompt}\n当前时间: {time.strftime('%Y-%m-%d %H:%M:%S', lt)}（{weekday}）")
        user_prompt = str(text or "")
        context_lines = [str(x or "").strip() for x in (recent_context or []) if str(x or "").strip()]
        if context_lines:
            user_prompt = ("【最近的对话（按时间顺序，最后一条是主人刚说的）】\n"
                           + "\n".join(context_lines)
                           + "\n\n请依据上面的上下文判断主人真正要提醒的事项。")
        try:
            data = await generate_json_reply(ctx, system, user_prompt, max_tokens=200)
        except Exception as e:
            print(f"LLM 待办提取失败: {e}")
            return []
        print(f"[待办提取] 用户消息: {text!r} → LLM 返回: {data!r}")
        if not isinstance(data, dict) or not data.get("has_todo"):
            return []
        content = str(data.get("content", "")).strip()[:200]
        if not content:
            return []
        content = clean_todo_content(content)
        if not content:
            print("[待办提取] 模型给出的 content 只剩语气词，视为没有事项，放弃本条")
            return []
        # ① 「再提醒我」这类只是提醒指令本身，不是事项：先按上下文还原真正要做的事
        if todo_content_is_meaningless(content):
            fallback = content_from_context(context_lines)
            if fallback:
                print(f"[待办提取] 提取到的「{content}」只是提醒指令本身，"
                      f"已按上下文还原为「{fallback}」")
                content = fallback
            else:
                # 也没有上下文可依据：至少把它转述成一句可执行的话，绝不原样发送
                fallback = content_from_user_text(text)
                if not fallback:
                    fallback = "去休息" if re.search(r"睡|休息|歇", str(text)) else ""
                if not fallback:
                    print(f"[待办提取] 提取到的「{content}」是无意义字眼"
                          "且找不到具体事项，放弃本条（宁可漏提醒，也不发"
                          "「快去做『再提醒我』这件大事吧」这种话）。")
                    return []
                print(f"[待办提取] 提取到的是提醒指令本身，已转述为「{fallback}」")
                content = fallback
        # ② content 是纯时间短语（「两分钟后」「8点半」）→ 事项丢了，从原话里捞回来
        if content_is_time_phrase(content):
            salvaged = content_from_user_text(text) or content_from_context(context_lines)
            if salvaged:
                print(f"[待办提取] 模型把时间「{content}」当成了事项，已按用户原话改为「{salvaged}」")
                content = salvaged
            else:
                print(f"[待办提取] 模型只给出时间「{content}」而没有任何事项，放弃本条")
                return []
        remind_ts = None
        # 绝对时间优先：模型只需抄下用户说的钟点，由本地解析到"最近一次到来的该时刻"。
        # 之前 delay_minutes 优先，小模型算分钟差经常出错（"23点"被算成 40/80 分钟后），
        # 是提醒时间错乱的根因；只有模型没给出可解析的 time 时才退回相对分钟数。
        if data.get("time"):
            remind_ts = self._parse_time_expr(str(data.get("time")), now)
            if remind_ts is not None:
                print(f"[待办提取] 绝对时间 {data.get('time')!r} → 触发时刻 "
                      f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(remind_ts))}")
        if remind_ts is None:
            delay = data.get("delay_minutes")
            try:
                delay = float(delay)
            except (TypeError, ValueError):
                delay = None
            if delay is not None and delay > 0:
                # 相对时长必须能在用户原话里找到：模型常自己算分钟差，
                # "早上8点半"被算成 2 分钟 / 450 分钟都是同一个坑。
                if relative_duration_mentioned(text) or relative_duration_mentioned(
                        context_lines[-1] if context_lines else ""):
                    remind_ts = now + delay * 60
                    print(f"[待办提取] 采用相对时长 {delay:g} 分钟（用户原话中出现过）→ "
                          f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(remind_ts))}")
                else:
                    print(f"[待办提取] 丢弃模型编造的 delay_minutes={delay:g}"
                          f"（用户原话里没有任何相对时长）")
        if remind_ts is None:
            # 模型既没给出可用 time、也没有可采信的相对时长时，
            # 直接从用户原话里再解析一次钟点：
            # 模型把「早上8点半」漏成 delay_minutes 的情况很常见（还会算错）。
            full_text = " ".join([text] + context_lines[-1:])
            parsed = self._parse_time_expr(full_text, now)
            if parsed is not None:
                remind_ts = parsed
                print(f"[待办提取] 模型未给出可用时间，已从用户原话解析出时刻 → "
                      f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(remind_ts))}")
        if remind_ts is None:
            print(f"[待办提取] 未能确定提醒时间，放弃本条：{data!r}")
            return []
        return [(content, remind_ts)]

    async def extract_and_add(self, ctx: RoleContext, text: str, session_type: str,
                              session_id: str, user_id: str = "",
                              recent_context: List[str] = None) -> List[Tuple[str, float]]:
        """LLM 模式提取并入库（后台任务调用）。"""
        found = await self.extract_llm(ctx, text, recent_context=recent_context)
        for content, remind_ts in found:
            self.add_todo(content, remind_ts, session_type, session_id, user_id, source="llm")
        return found

    # ---------------- 增删查 ----------------
    def add_todo(self, content: str, remind_ts: float, session_type: str,
                 session_id: str, user_id: str = "", source: str = "auto",
                 use_voice: bool = None) -> Optional[dict]:
        content = str(content or "").strip()
        if todo_content_is_meaningless(content):
            # 最后一道闸：任何来源（正则/LLM/WebUI）都不许把"再提醒我"这类空指令入库，
            # 否则到点会念出「快去做『再提醒我』这件大事吧，本座可一直盯着你呢~」。
            print(f"[待办提醒] 拒绝创建无意义待办（只是提醒指令本身）：{content!r}")
            return None
        try:
            cur = self.db.execute(
                "INSERT INTO todos (created_at, session_type, session_id, user_id, content,"
                " remind_time, status, source, use_voice) VALUES (?,?,?,?,?,?,?,?,?)",
                (time.time(), session_type, str(session_id), str(user_id), content,
                 remind_ts, "pending", source,
                 None if use_voice is None else (1 if use_voice else 0)))
            todo_id = cur.lastrowid
            self._schedule(todo_id, content, remind_ts, session_type, session_id,
                           use_voice=use_voice)
            print(f"[待办提醒] 已创建 #{todo_id}（来源 {source}）：{content!r} → "
                  f"触发时刻 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(remind_ts))}，"
                  f"会话 {session_type}_{session_id}")
            return {"id": todo_id, "content": content, "remind_time": remind_ts}
        except Exception as e:
            print(f"保存待办失败: {e}")
            return None

    def _schedule(self, todo_id: int, content: str, remind_ts: float,
                  session_type: str, session_id: str, use_voice: bool = None):
        async def _remind(todo_id=todo_id, content=content,
                          session_type=session_type, session_id=session_id,
                          use_voice=use_voice):
            await self.fire_reminder(todo_id, content, session_type, session_id,
                                     use_voice=use_voice)
        self.scheduler.add_job(f"todo_{todo_id}", f"待办提醒: {content[:16]}",
                               {"type": "oneshot", "at": remind_ts}, _remind)

    async def fire_reminder(self, todo_id: int, content: str, session_type: str,
                            session_id: str, emotion: str = "", use_voice: bool = None):
        print(f"[待办提醒] 触发 #{todo_id}：{content!r}（提醒模式：{self._remind_mode()}）")
        template = str(self.config.get("todo_remind_template", "") or "⏰ 提醒时间到啦：{content}")
        preset_message = template.replace("{content}", content)
        message = preset_message
        ctx = self.ctx_provider() if self.ctx_provider else None
        if self._remind_mode() == "llm" and ctx is not None:
            try:
                generated = await self._generate_reminder_text(ctx, content)
            except Exception as e:
                # 生成环节的任何异常都绝不能吞掉提醒本身
                print(f"LLM 提醒话术生成异常，回退预设模板: {type(e).__name__}: {e}")
                generated = ""
            if generated:
                # 生成的提醒必须包含事项本身，否则模型漏说时会有"提醒了却不知道提醒什么"的风险
                if content and content not in generated:
                    print(f"LLM 提醒话术未包含事项，丢弃该话术并使用预设：{generated[:60]!r}")
                else:
                    message = generated
                    print(f"[待办提醒] 使用 LLM 生成话术：{message!r}")
            else:
                print("LLM 提醒话术生成失败，回退预设模板。")
        if self._remind_mode() == "preset" or message == preset_message:
            print(f"[待办提醒] 使用预设模板话术：{message!r}")
        if self.sender is None:
            print(f"[待办提醒] {message}")
            self.db.execute("UPDATE todos SET status='done' WHERE id=?", (todo_id,))
            return
        emotions = self.emotions_provider() if self.emotions_provider else {}
        if use_voice is None:
            use_voice = bool(self.config.get("todo_voice", False))
        try:
            await self.sender.speak_and_send(session_type, session_id, message, emotions, ctx,
                                             use_voice=bool(use_voice),
                                             emotion=emotion or str(self.config.get("todo_voice_emotion", "")
                                                                    or "pingjing"))
            self.db.execute("UPDATE todos SET status='done' WHERE id=?", (todo_id,))
        except Exception as e:
            print(f"[待办提醒] 发送失败，待办 #{todo_id} 保留为待提醒状态：{type(e).__name__}: {e}")
            self.db.execute("UPDATE todos SET status='pending' WHERE id=?", (todo_id,))

    def _remind_mode(self) -> str:
        mode = str(self.config.get("todo_remind_mode", "llm") or "llm").strip().lower()
        return "preset" if mode in ("preset", "template", "fixed", "预设", "模板") else "llm"

    async def _generate_reminder_text(self, ctx: RoleContext, content: str) -> str:
        """用 LLM 生成一句符合角色人设的提醒话术（可选）。失败返回空串走预设兜底。"""
        from .jobs import generate_in_character_text
        custom = str(self.config.get("todo_remind_prompt", "") or "").strip()
        style = custom or (
            "主人之前让你在到点的时候提醒他做某件事，现在到点了，"
            "用你的角色语气提醒主人去做这件事：可以带一点角色口吻与关心，"
            "但不要寒暄、不要问问题、不要道歉拖延。"
        )
        instruction = (
            f"{style}\n"
            f"要提醒的事项：{content}\n"
            "要求：一句话，简短自然（不超过40字）；"
            f"必须在这个句子里原样说出「{content}」这件事，让主人一眼看懂提醒的内容；"
            "不要输出JSON、括号动作描写或解释。"
        )
        try:
            text = await generate_in_character_text(ctx, instruction, max_tokens=120)
        except Exception as e:
            print(f"待办提醒话术生成异常: {type(e).__name__}: {e}")
            return ""
        text = str(text or "").strip().strip('"“”')
        if not text or len(text) > 200:
            return ""
        return text

    def restore_pending(self):
        """程序启动时恢复未完成的待办调度。"""
        try:
            rows = self.db.query_all("SELECT * FROM todos WHERE status='pending'")
            now = time.time()
            for row in rows:
                remind_ts = row.get("remind_time") or 0
                if remind_ts <= now:
                    self.db.execute("UPDATE todos SET status='missed' WHERE id=?", (row["id"],))
                    continue
                self._schedule(row["id"], row["content"], remind_ts,
                               row.get("session_type", "private"), row.get("session_id", ""),
                               use_voice=(None if row.get("use_voice") is None
                                          else bool(row.get("use_voice"))))
            if rows:
                print(f"已恢复 {len(rows)} 条待办提醒调度。")
        except Exception as e:
            print(f"恢复待办失败: {e}")

    def list_todos(self, status: str = None) -> list:
        if status:
            return self.db.query_all("SELECT * FROM todos WHERE status=? ORDER BY remind_time", (status,))
        return self.db.query_all("SELECT * FROM todos ORDER BY id DESC LIMIT 200")

    def complete(self, todo_id: int) -> bool:
        self.db.execute("UPDATE todos SET status='done' WHERE id=?", (todo_id,))
        self.scheduler.remove_job(f"todo_{todo_id}")
        return True

    def delete(self, todo_id: int) -> bool:
        self.db.execute("DELETE FROM todos WHERE id=?", (todo_id,))
        self.scheduler.remove_job(f"todo_{todo_id}")
        return True
