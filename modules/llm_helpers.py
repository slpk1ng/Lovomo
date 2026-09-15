"""LLM 交互核心：消息构建、流式句子解析、工具调用循环、文本/JSON 生成。

- RoleContext: 全局配置 + 角色覆盖字段的只读视图（多角色支持的基础）。
- SentenceStreamParser: 边接收增量输出边解析出完整句子对象，实现实时流式回复。
- chat_with_tools: Function Calling 循环（支持 Ollama 原生与 OpenAI 兼容接口）。
"""
import asyncio
import json
import re
import threading
import time
from pathlib import Path
from typing import Optional, List, Dict, AsyncGenerator

import httpx

from .tts import strip_urls_for_tts
from .tls import verified_context


def conn_fail_hint(endpoint: str, base_url: str, backend: str) -> str:
    """生成“无法连接本地 LLM 服务”时可操作的错误提示。"""
    return (f"无法连接 LLM 服务：{endpoint}（配置 llm_base_url={base_url}，"
            f"backend={backend}）。请确认：① 对应服务（Ollama / OpenAI 兼容服务）已启动；"
            "② llm_base_url、llm_backend、llm_model_name 与你的服务匹配；"
            "③ 地址可达且未被代理或防火墙拦截。")


# ---------------------------------------------------------------------------
# 思考内容（thinking / reasoning）清洗
# ---------------------------------------------------------------------------
# 事故：推理型模型会把思维链写进 content 或 thinking 字段，
# 一旦原样当成台词，就会出现"主动消息把思考过程念了一分多钟"。
# 这里统一把思考内容剥掉，只保留真正的回答。

_THINK_BLOCK_RE = re.compile(
    r"<\s*(?:think|thinking|reasoning|thought|analysis|scratchpad)\s*>.*?"
    r"<\s*/\s*(?:think|thinking|reasoning|thought|analysis|scratchpad)\s*>",
    re.IGNORECASE | re.DOTALL)
_THINK_OPEN_RE = re.compile(
    r"<\s*(?:think|thinking|reasoning|thought|analysis|scratchpad)\s*>", re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(
    r"<\s*/\s*(?:think|thinking|reasoning|thought|analysis|scratchpad)\s*>", re.IGNORECASE)

# 各厂商把"思考"放在不同字段里，统一收集
_THINK_FIELD_NAMES = ("thinking", "reasoning", "reasoning_content", "reasoning_details",
                      "thought", "analysis", "thoughts")

# 思考过程的常见开头，用于识别"没有标签、直接把思考写进正文"的情况
_THINK_MARKER_RE = re.compile(
    r"^(?:Thinking\s+Process|Chain\s+of\s+Thought|Reasoning|Analysis|My\s+Reasoning|"
    r"Internal\s+monologue|思考过程|推理过程|分析过程|让我想想|我需要思考|思路如下)\s*[:：]",
    re.IGNORECASE | re.MULTILINE)

# 思考段落的典型句子开头（中英思维链最常见的写法）
# 注意：不能只靠 "我需要""我想" 这类词判断，正常台词也常有，
# 因此这里只收束到"元叙述"特征明显的句式（用户说/用户问/我们需要…）。
_THINK_SENTENCE_RE = re.compile(
    r"^\s*(?:The\s+user\s+(?:asks|asked|wants|says|said|is|has)|We\s+need\s+to|"
    r"Let\s+me\s+(?:think|analyze|consider)|"
    r"用户(?:问|说|想|要求|希望)|我需要(?:思考|分析|考虑)|让我(?:想想|分析|思考)|"
    r"首先[，,]?\s*我(?:需要|要|得))",
    re.IGNORECASE)

# 思考段落结束、正文开始的标记
_ANSWER_MARKER_RE = re.compile(
    r"^(?:\*{0,2}(?:Final\s+Answer|Answer|Response|Reply|Output)\*{0,2}\s*[:：]|"
    r"(?:最终回答|正式回答|回答|回复|台词)\s*[:：])\s*",
    re.IGNORECASE | re.MULTILINE)

# 分点行（思考列表常见）
_BULLET_LINE_RE = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)、])\s+")

_MAX_THINK_SCAN_CHARS = 20000


def strip_thinking(text: str) -> str:
    """剥掉模型输出里的思考内容，只保留真正的回答。

    覆盖情形：
      1. 成对的  thinking…<｜end▁of▁thinking｜> / <reasoning>…</reasoning>；
      2. 只有闭合标签（开头标签被服务端吞掉）：丢弃闭合标签之前的内容；
      3. 只有开始标签（流式被截断）：丢弃标签之后到正文标记之前的内容；
      4. 完全没有标签，但正文里出现「Thinking Process:」这类思考段落；
      5. 多行思维链 + 「Final Answer:」式正文标记。
    """
    if not text:
        return ""
    out = str(text)
    if len(out) > _MAX_THINK_SCAN_CHARS:
        # 异常长的输出几乎必然是思考内容，先截断再做后续清洗（避免正则灾难）
        out = out[:_MAX_THINK_SCAN_CHARS]
    for _ in range(4):  # 可能嵌套多段，反复清理
        new = _THINK_BLOCK_RE.sub("", out)
        if new == out:
            break
        out = new

    if _THINK_CLOSE_RE.search(out) and not _THINK_OPEN_RE.search(out):
        # 头部的  thinking 被服务端吃掉：闭合标签之前都是思考
        out = _THINK_CLOSE_RE.split(out)[-1]

    if _THINK_OPEN_RE.search(out):
        # 未闭合（被截断）：保留标签后出现的正文标记之后的内容；没有标记则整体丢弃
        out = _drop_after_open_tag(out)

    out = _drop_leading_thinking_paragraph(out)
    return out.strip()


def _drop_after_open_tag(text: str) -> str:
    """处理未闭合的思考标签：优先在正文标记处切开，否则整段判为思考。"""
    m = _THINK_OPEN_RE.search(text)
    before, after = text[:m.start()], text[m.end():]
    for marker in _ANSWER_MARKER_RE.finditer(after):
        tail = after[marker.end():].strip()
        if tail:
            return f"{before}{tail}"
    # 没有正文标记：按段落找"看起来像台词"的短尾段，找不到就只保留标签前的内容
    tail = _tail_answer_paragraph(after)
    return f"{before}{tail}"


def _tail_answer_paragraph(after: str) -> str:
    """从被思考污染的文本里抢救真正的回答（末尾段落中最不像思考的那段）。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", after) if p.strip()]
    for para in reversed(paras[-4:] if len(paras) > 4 else paras):
        if _LOOKS_LIKE_THINKING(para):
            continue
        if len(para) <= 300:
            return para
    return ""


def _drop_leading_thinking_paragraph(text: str) -> str:
    """正文开头是思考段落时，剥掉思考部分，只保留真正的回答。

    两种情形：
      A. 有「Thinking Process:」这类标记 → 连同标记后面的分点列表一起丢掉；
      B. 没有标记，但开头就是元叙述句（"The user asks…"/"用户希望…"）→ 丢掉该行。
    遇到空行/正文标记/看起来像台词的行就停止，避免过度删除正常台词。
    """
    lines = text.splitlines()
    start = 0
    marked = False
    dropped_any = False
    while start < len(lines):
        line = lines[start]
        stripped = line.strip()
        if not stripped:
            if dropped_any:
                start += 1          # 思考段与其后的回答之间通常隔着空行
                break
            start += 1
            continue
        if _THINK_MARKER_RE.match(stripped):
            marked = True
            dropped_any = True
            start += 1
            continue
        if marked:
            # 标记后紧跟的分点/短行都属于思考内容
            if _BULLET_LINE_RE.match(stripped) or _THINK_SENTENCE_RE.match(stripped) \
                    or _ANSWER_MARKER_RE.match(stripped):
                if _ANSWER_MARKER_RE.match(stripped):
                    break           # 正文标记本身保留，交给下面统一剥离
                start += 1
                continue
            break
        if _THINK_SENTENCE_RE.match(stripped) or _BULLET_LINE_RE.match(stripped):
            dropped_any = True
            start += 1
            continue
        break
    result = "\n".join(lines[start:]).strip()
    return _ANSWER_MARKER_RE.sub("", result, count=1).strip()


def _LOOKS_LIKE_THINKING(para: str) -> bool:
    p = str(para or "").strip()
    if not p:
        return False
    if _THINK_MARKER_RE.search(p) or _THINK_SENTENCE_RE.search(p):
        return True
    # 大段英文分点论述/长段落基本不是角色台词
    if len(p) > 400:
        return True
    bullets = len(re.findall(r"(?:^|\n)\s*(?:[-*•]|\d+[.)])\s+", p))
    return bullets >= 3


def looks_like_thinking(text: str) -> bool:
    """判断一段文本是否"看起来是思考过程"（用于丢弃整段污染的生成结果）。"""
    p = str(text or "").strip()
    if not p:
        return False
    if _THINK_OPEN_RE.search(p) or _THINK_MARKER_RE.search(p) or _THINK_SENTENCE_RE.search(p):
        return True
    return _LOOKS_LIKE_THINKING(p)


# 已知会输出思维链的模型系列（用于补上 /no_think 之类的模板开关）
_REASONING_MODEL_RE = re.compile(
    r"(qwen3|qwen-?3|qwq|deepseek-?r1|r1-|reasoning|magistral|phi-?4-reasoning|"
    r"glm-?4|thinking|o1-|o3-|o4-)", re.IGNORECASE)


def is_reasoning_model(ctx) -> bool:
    """当前模型是否属于"默认会思考"的推理模型系列。"""
    model = str(ctx.get("llm_model_name", "") or "")
    return bool(_REASONING_MODEL_RE.search(model))


def no_think_suffix(ctx) -> str:
    """需要时返回强关思考的提示后缀。

    只作用于主动消息 / 摘要 / 画像这类"辅助文本生成"：
    这些内容会直接变成语音或 JSON，绝不能混入思考过程。
    """
    if not is_reasoning_model(ctx):
        return ""
    return "\n/no_think\n（再次强调：不要输出任何思考过程，只输出最终要说的那一句话。）"


def extract_answer_from_message(msg: dict) -> str:
    """从 Ollama / OpenAI 消息对象里取"真正的回答"。

    思考字段（thinking / reasoning_content 等）会被丢弃；
    content 里内嵌的  thinking 块也会被剥离。
    """
    msg = msg or {}
    content = msg.get("content", "") or ""
    if not isinstance(content, str):
        try:
            content = json.dumps(content, ensure_ascii=False)
        except Exception:
            content = str(content)
    cleaned = strip_thinking(content)
    if cleaned:
        return cleaned
    # content 为空但存在思考字段：说明模型只输出了思考，不能拿思考当台词
    if any(str(msg.get(k) or "").strip() for k in _THINK_FIELD_NAMES):
        return ""
    return ""


def thinking_fragments(msg: dict) -> str:
    """取出（仅用于日志的）思考文本，便于排查模型为何答非所问。"""
    msg = msg or {}
    parts = []
    for key in _THINK_FIELD_NAMES:
        val = msg.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(f"{key}: {val.strip()}")
        elif isinstance(val, list) and val:
            parts.append(f"{key}: {json.dumps(val, ensure_ascii=False)[:400]}")
    return "\n".join(parts)


_STICKER_OBJ_RE = re.compile(r"\{[^{}]*sticker_(?:capture|safe)[^{}]*\}")


def _extract_capture_verdict(content: str) -> Optional[dict]:
    """取出识图回复里的收藏判定对象（含 sticker_safe / sticker_capture 字段的那个）。

    事故：模型输出的整体 JSON 里混入中文引号（“”），括号配平扫描的字符串
    状态被带偏，判定对象明明输出了却提取不到（提取结果 null）。判定对象本身
    是扁平结构，直接按 sticker_safe / sticker_capture 关键字定位后单独解析，
    不再依赖整体扫描；解析失败再逐字段抠值。
    """
    text = str(content or "")
    for m in _STICKER_OBJ_RE.finditer(text):
        snippet = m.group(0)
        obj = None
        try:
            obj = json.loads(snippet)
        except Exception:
            try:
                flat = (snippet.replace("“", "\"").replace("”", "\"")
                        .replace("‘", "'").replace("’", "'"))
                obj = json.loads(flat)
            except Exception:
                obj = None
        if isinstance(obj, dict) and ("sticker_safe" in obj or "sticker_capture" in obj):
            return obj
        # JSON 彻底解析失败：引号缺失/全角冒号/值带引号等，按字段逐个抠
        flat = (snippet.replace("“", "\"").replace("”", "\"")
                .replace("‘", "'").replace("’", "'"))
        m_flag = re.search(r"sticker_(?:capture|safe)\"?\s*[：:]\s*\"?(true|false)", flat, re.I)
        if not m_flag:
            continue
        m_cat = re.search(r"category\"?\s*[：:]\s*[\"“]?([^,，}\"”]*)", flat)
        m_reason = re.search(r"reason\"?\s*[：:]\s*[\"“]?(.*?)[”\"]*\s*\}?\s*$", flat)
        return {"sticker_capture": m_flag.group(1).lower() == "true",
                "category": (m_cat.group(1).strip() if m_cat else ""),
                "reason": (m_reason.group(1).strip() if m_reason else "")}
    # 兜底：整体扫描（判定对象在完全合法的 JSON 结构里时走这里）
    for obj in extract_json_objects(text):
        if isinstance(obj, dict) and ("sticker_safe" in obj or "sticker_capture" in obj):
            return obj
    return None


def sticker_capture_allowed(ctx, content: str) -> bool:
    """是否允许把这张图当作表情包收藏。

    旧逻辑在"模型完全没给判定"时默认 YES
    （`capture = {"should": True, ...}`），于是一张纯风景图也能被收藏。
    现在默认要求**明确的肯定判定**：必须返回 sticker_safe/sticker_capture 为真，
    且给出足够具体的理由（`sticker_capture_min_reason_chars`，默认 6 字）。
    想要旧行为可设 `sticker_capture_require_verdict=false`。
    """
    if not ctx.get("sticker_capture_enabled", False):
        return False
    verdict = _extract_capture_verdict(content)
    if verdict is None:
        if bool(ctx.get("sticker_capture_require_verdict", True)):
            print("【表情收藏】模型未给出收藏判定，按不收藏处理（可设 "
                  "sticker_capture_require_verdict=false 恢复旧行为）。")
            return False
        return True
    for key in ("sticker_safe", "sticker_capture"):
        if key in verdict:
            value = verdict.get(key)
            if isinstance(value, bool):
                ok = value
            else:
                ok = str(value).strip().lower() in ("true", "1", "yes", "是", "可以", "true。")
            if not ok:
                print(f"【表情收藏】模型判定不收藏（{key}={value!r}）。")
                return False
            try:
                min_chars = int(ctx.get("sticker_capture_min_reason_chars", 6) or 0)
            except Exception:
                min_chars = 6
            reason = str(verdict.get("reason", "") or "").strip()
            if min_chars > 0 and len(reason) < min_chars:
                print(f"【表情收藏】判定理由过于笼统（{reason!r}），按不收藏处理。")
                return False
            return True
    return False


def _sticker_capture_instruction(ctx) -> str:
    """生成"是否值得收藏为表情包"的判定指令（力求宁缺毋滥）。

    WebUI 的 sticker_capture_prompt 非空时追加为自定义补充规则
    （与 emotion_guide_extra 同一套用法）。
    """
    from modules.stickers import STRICT_ALLOWED
    cats = "、".join(
        f"{c}({STICKER_USE_HINTS[c]})" if c in STICKER_USE_HINTS else c
        for c in sorted(STRICT_ALLOWED))
    extra = str(ctx.get("sticker_capture_prompt", "") or "").strip()
    suffix = f"\n【自定义补充规则】{extra}" if extra else ""
    return (
        "\n【表情包收藏判定】在回复 JSON 之后，必须再输出一个独立的 sticker JSON 对象"
        "（不要放进回复的 sentences/description）："
        '先判断是否含真人面孔或隐私内容（证件、聊天记录、手机号、地址、二维码等），'
        '含则输出 {"sticker_safe": false}（到此为止）。'
        "\n判定标准（前两条是硬性否决，必须优先执行）："
        "\n① 纯风景 / 空镜 / 静物 / 室内随手拍 / 无人物、无文字、无明确情绪表达的图片："
        '一律 {"sticker_safe": false}，不要收藏，也不要因为"画面好看""氛围有趣"就收藏；'
        "\n② 画面阴森、恐怖、诡异、病态、压抑，或只是普通生活记录、截图、"
        "文档、商品图、自拍头像：一律 {\"sticker_safe\": false}；"
        "\n③ 只有在【这张图将来被当表情包发出去时有明确的使用场景】才收藏："
        "画面里有夸张的表情/动作，或带有可用于互动的文字，"
        "或明显用于挑逗、撩拨、嘲讽、炫耀、撒娇、无语等互动用途；"
        "\n④ 判定场景的是【发图一方想表达的语气】，不是画面里角色的此刻心情；"
        "单个神态词（如睁大眼睛、惊喜）不构成收藏理由，"
        "若整体明显用于调戏/撩拨，应选 weixie 或 sajiao 而不是 jingya；"
        "\n⑤ 拿不准就输出 {\"sticker_safe\": false}——宁可不收藏，也不要错收藏。"
        f"\n可以收藏时输出：{{\"sticker_safe\": true, \"category\": \"使用场景拼音\", "
        f"\"reason\": \"具体说明这张图适合在什么场景、表达什么语气使用\"}}。"
        f"category 只能从以下拼音中选：{cats}。"
        "reason 必须具体（一句话说明使用场景），不能只写\"好看\"\"有趣\"\"可爱\"这类空话。"
        + suffix
    )


async def check_llm_service(ctx) -> tuple:
    backend = ctx.get("llm_backend", "ollama")
    base_url = str(ctx.get("llm_base_url", "http://127.0.0.1:11434")).rstrip("/")
    api_key = ctx.get("llm_api_key", "")
    if backend == "ollama":
        endpoint = f"{base_url}/api/tags"
        headers = {}
    else:
        endpoint = f"{base_url}/models"
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        async with httpx.AsyncClient(timeout=6, proxy=None, trust_env=False,
                                     verify=verified_context()) as client:
            resp = await client.get(endpoint, headers=headers)
        if resp.status_code < 500:
            return True, endpoint
        return False, f"{endpoint} → HTTP {resp.status_code}"
    except Exception as e:
        return False, f"{endpoint} → {type(e).__name__}: {e}"


def extract_json_objects(text: str) -> List[dict]:
    """提取文本中所有顶层 JSON 对象（按出现顺序）；顶层数组会展开其中的对象元素。

    与 extract_json 只取第一个对象不同：模型常把每句话输出成独立的 JSON 块
    （提示词要求"至少两个JSON块"时尤其常见），只取第一块会丢掉其余句子，
    导致"生成了多个JSON块却只合成一条语音"。每个对象严格解析失败时
    用宽容解析器修复；输出被截断时自动补齐末尾未闭合的括号。
    """
    if not text:
        return []
    objs: List[dict] = []
    depth = 0
    in_string = False
    escape = False
    start = None
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in '{[':
            if depth == 0:
                start = i
            depth += 1
        elif ch in '}]':
            if depth == 0:
                continue  # 游离闭括号，忽略
            depth -= 1
            if depth == 0 and start is not None:
                data = _loads_lenient(text[start:i + 1])
                if isinstance(data, dict):
                    objs.append(data)
                elif isinstance(data, list):
                    objs.extend(x for x in data if isinstance(x, dict))
                start = None
    if depth > 0 and start is not None:
        # 截断输出：末尾未闭合的对象/数组补齐后解析，保住已完整生成的句子
        data = _loads_lenient(text[start:])
        if isinstance(data, dict):
            objs.append(data)
        elif isinstance(data, list):
            objs.extend(x for x in data if isinstance(x, dict))
    return objs


# 句子对象至少含有这些键之一（用于把句子块与无关 JSON 对象区分开）
_SENTENCE_KEYS = ("zh", "ja", "en", "lang", "display", "emotion", "text")
# 台词文本键（emotion 不算台词；不含任何台词键值的句子对象必须丢弃，
# 否则会用用户消息兜底，导致机器把用户刚说的话朗读出来）
_TEXT_KEYS = ("zh", "ja", "en", "lang", "display", "text")


def _is_sentence_like(obj) -> bool:
    """判断 JSON 对象是否像一条句子块（含 zh/ja/emotion 等键，且不是 sentences 包装）。"""
    return isinstance(obj, dict) and "sentences" not in obj and \
        any(k in obj for k in _SENTENCE_KEYS)


def sentence_obj_has_text(s) -> bool:
    """判断一个句子对象是否含实际台词文本（emotion 等元数据不算）。"""
    if not isinstance(s, dict):
        return bool(str(s).strip())
    return any(str(s.get(k, "")).strip() for k in _TEXT_KEYS)


class _TolerantJSONParser:
    """宽容 JSON 解析器：修复模型常见的 JSON 语法病，尽量避免整段输出报废。

    处理的问题（均为模型真实输出中高频出现的）：
    - 字符串值内未转义的引号（仅当引号后跟结构字符 ,}]: 时才视为字符串结束）
    - 字符串内的裸换行/控制字符（严格 JSON 不允许，模型经常直接断行）
    - 尾逗号 / 多余逗号
    - 输出被截断：未闭合的字符串、对象、数组在文本末尾自动补齐，
      保住已完整生成的句子（旧逻辑遇截断直接全盘失败）
    - 无引号的键名
    """

    def __init__(self, text: str):
        self.t = text
        self.n = len(text)
        self.i = 0

    def parse(self):
        self.i = 0
        return self._value()

    def _skip_ws(self):
        while self.i < self.n and self.t[self.i] in " \t\r\n":
            self.i += 1

    def _value(self):
        self._skip_ws()
        if self.i >= self.n:
            return None
        c = self.t[self.i]
        if c == '{':
            return self._object()
        if c == '[':
            return self._array()
        if c == '"':
            return self._string()
        return self._atom()

    def _object(self):
        obj = {}
        self.i += 1  # {
        while True:
            self._skip_ws()
            if self.i >= self.n:
                return obj  # 截断：返回已解析出的部分
            c = self.t[self.i]
            if c == '}':
                self.i += 1
                return obj
            if c == ',':  # 尾逗号/多余逗号
                self.i += 1
                continue
            if c == '"':
                key = self._string()
            else:  # 无引号键名：取到冒号/换行为止
                j = self.i
                while j < self.n and self.t[j] not in ':}\n':
                    j += 1
                key = self.t[self.i:j].strip()
                self.i = j
            self._skip_ws()
            if self.i < self.n and self.t[self.i] == ':':
                self.i += 1
            obj[str(key)] = self._value()
            self._skip_ws()
            if self.i >= self.n:
                return obj
            if self.t[self.i] == ',':
                self.i += 1
                continue
            if self.t[self.i] == '}':
                self.i += 1
                return obj
            self.i += 1  # 其他语法错误字符：跳过继续

    def _array(self):
        arr = []
        self.i += 1  # [
        while True:
            self._skip_ws()
            if self.i >= self.n:
                return arr
            c = self.t[self.i]
            if c == ']':
                self.i += 1
                return arr
            if c == ',':
                self.i += 1
                continue
            arr.append(self._value())
            self._skip_ws()
            if self.i >= self.n:
                return arr
            if self.t[self.i] == ',':
                self.i += 1
                continue
            if self.t[self.i] == ']':
                self.i += 1
                return arr
            self.i += 1

    def _string(self):
        self.i += 1  # 开引号
        out = []
        esc = {'n': '\n', 't': '\t', 'r': '\r', '"': '"', '\\': '\\',
               '/': '/', 'b': '\b', 'f': '\f'}
        while self.i < self.n:
            c = self.t[self.i]
            if c == '\\':
                if self.i + 1 < self.n:
                    nxt = self.t[self.i + 1]
                    if nxt == 'u' and self.i + 6 <= self.n:
                        try:
                            out.append(chr(int(self.t[self.i + 2:self.i + 6], 16)))
                            self.i += 6
                            continue
                        except ValueError:
                            pass
                    out.append(esc.get(nxt, nxt))
                    self.i += 2
                    continue
                self.i += 1  # 末尾孤立反斜杠
                continue
            if c == '"':
                # 宽容关键点：引号后跟结构字符（,}]:）或到结尾才算字符串结束；
                # 否则视为值内部忘记转义的引号，按普通字符保留
                j = self.i + 1
                while j < self.n and self.t[j] in ' \t\r\n':
                    j += 1
                if j >= self.n or self.t[j] in ',}]:':
                    self.i += 1
                    return ''.join(out)
                out.append(c)
                self.i += 1
                continue
            out.append(c)  # 裸控制字符（如换行）原样保留进值里
            self.i += 1
        return ''.join(out)  # 截断：字符串自动闭合

    def _atom(self):
        j = self.i
        while j < self.n and self.t[j] not in ',}]"\n':
            j += 1
        s = self.t[self.i:j].strip()
        self.i = j
        low = s.lower()
        if low == 'true':
            return True
        if low == 'false':
            return False
        if low in ('null', 'none', ''):
            return None
        for cast in (int, float):
            try:
                return cast(s)
            except ValueError:
                pass
        return s


def _loads_lenient(text: str):
    """严格解析失败时用宽容解析器修复模型 JSON 语法病。"""
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        return _TolerantJSONParser(text).parse()
    except Exception:
        return None


def _looks_like_json(text: str) -> bool:
    """结构性判断一段文本是否形似 JSON（不匹配任何具体模型输出）。"""
    s = text.lstrip()
    if s[:1] in ('{', '['):
        return True
    # 文本中嵌着"带引号键名+冒号"的 JSON 对象碎片
    return bool(re.search(r'\{\s*"[^"]{1,40}"\s*:', text))


def extract_json(text: str) -> Optional[dict]:
    """从文本中提取第一个 JSON 对象；非对象（数字/字符串/数组等标量）视为提取失败。"""
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    # 宽容修复解析（模型 JSON 常见语法病：未转义引号/裸换行/尾逗号/截断）
    data = _loads_lenient(cleaned)
    if isinstance(data, dict):
        return data
    start_indices = [i for i, char in enumerate(cleaned) if char == '{']
    # 优先匹配最外层的完整 JSON 对象（模型常在 JSON 前后夹杂闲聊文本，
    # 若从最内层匹配会拿到句子对象而非 {"sentences": [...]} 整体）
    for start in start_indices:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(cleaned)):
            char = cleaned[i]
            if in_string:
                if escape:
                    escape = False
                elif char == '\\':
                    escape = True
                elif char == '"':
                    in_string = False
            else:
                if char == '"':
                    in_string = True
                elif char == '{':
                    depth += 1
                elif char == '}':
                    depth -= 1
                    if depth == 0:
                        try:
                            data = json.loads(cleaned[start:i+1])
                            if isinstance(data, dict):
                                return data
                        except Exception:
                            break
    return None


# ---------------------------------------------------------------------------
# 文本清洗：用户在 WebUI 配置的屏蔽字符/词，LLM 返回后统一剔除（语音+文字都生效）
# ---------------------------------------------------------------------------

def _clean_blocklist_tokens(ctx) -> List[str]:
    raw = str(ctx.get("text_clean_blocklist", "") or "")
    tokens = []
    for piece in re.split(r"[\n,，;；]+", raw):
        token = piece.strip()
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def apply_text_clean(text: str, ctx) -> str:
    """剔除回复文本中用户配置的屏蔽字符/词（text_clean_blocklist）。"""
    out = str(text or "")
    if not out:
        return out
    tokens = _clean_blocklist_tokens(ctx)
    for token in tokens:
        out = out.replace(token, "")
    return out.strip() if tokens else out


# ---------------------------------------------------------------------------
# 模型切换：换模型后在新模型首次调用成功时，卸载不再使用的旧模型（LM Studio）
# ---------------------------------------------------------------------------

_PENDING_MODEL_UNLOAD: List[str] = []


def queue_old_model_unload(model_key: str):
    """登记需要卸载的旧模型（配置保存时调用，实际卸载延迟到新模型调用成功后）。"""
    m = str(model_key or "").strip()
    if m and m not in _PENDING_MODEL_UNLOAD:
        _PENDING_MODEL_UNLOAD.append(m)


def _lmstudio_unload(model_key: str) -> bool:
    """通过 lms CLI 卸载 LM Studio 里已加载的模型。"""
    import os
    import subprocess
    lms = Path.home() / ".lmstudio" / "bin" / ("lms.exe" if os.name == "nt" else "lms")
    exe = str(lms) if lms.exists() else "lms"
    try:
        r = subprocess.run([exe, "unload", model_key], capture_output=True, timeout=60)
        return r.returncode == 0
    except Exception as e:
        print(f"[模型切换] 卸载旧模型 {model_key} 失败: {e}")
        return False


def maybe_unload_old_models(ctx) -> None:
    """新模型调用成功后触发：卸载登记过的旧模型（仅 openai 兼容后端，后台线程执行）。

    Ollama 自己管理模型常驻（keep_alive），不需要也不会走这条路径。
    """
    if not _PENDING_MODEL_UNLOAD:
        return
    backend = str(ctx.get("llm_backend", "ollama") or "")
    if backend != "openai":
        _PENDING_MODEL_UNLOAD.clear()
        return
    still_used = {str(ctx.get("llm_model_name", "") or "").strip(),
                  str(ctx.get("image_caption_model_name", "") or "").strip()}
    to_unload = [m for m in _PENDING_MODEL_UNLOAD if m and m not in still_used]
    _PENDING_MODEL_UNLOAD.clear()
    if not to_unload:
        return

    def _work():
        for m in to_unload:
            if _lmstudio_unload(m):
                print(f"[模型切换] 旧模型已卸载：{m}")
            else:
                print(f"[模型切换] 旧模型 {m} 卸载失败（可能本来未加载）")

    threading.Thread(target=_work, daemon=True).start()


class RoleContext:
    """全局配置 + 角色覆盖字段的只读视图。

    角色字段中非空的值优先；否则回退到全局配置。
    """

    def __init__(self, config, role: Optional[dict] = None):
        self.config = config  # ConfigLoader 或任何带 .get() 的对象
        self.role = role or {}

    def get(self, key, default=None):
        role_val = self.role.get(key)
        if role_val is not None and role_val != "":
            return role_val
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    @property
    def character_key(self) -> str:
        return self.role.get("character_key", "") or str(self.config.get("character_key", ""))

    @property
    def character_name(self) -> str:
        return self.role.get("character_name", "") or str(self.config.get("character_name", ""))


# ---------------------------------------------------------------------------
# 消息与提示词构建
# ---------------------------------------------------------------------------

USER_LABEL_PREFIX = "用户"
ASSISTANT_LABEL_PREFIX = "角色"


def _speaker_label(prefix: str, keys: List[str], key: str) -> str:
    """把说话人身份映射成序号标签（用户1/用户2…），首次出现顺序即编号顺序。"""
    if key not in keys:
        keys.append(key)
    return f"{prefix}{keys.index(key) + 1}"


def build_speaker_labels(history: list) -> Dict:
    """按「首次出现顺序」为每个说话人分配匿名标签，键为 sender_id / speaker。

    标签设计要点：**绝不把昵称或 QQ 号写进消息内容**。
    事故背景：昵称被拼成 `[用户:昵称]` 注入上下文，昵称里带"困/饿/睡/猫/狗"等字眼时，
    模型会把它当成用户的真实状态（用户昵称叫"困困"就会被回复"你刚才说困了吗？"），
    提示词里再写"昵称不是状态"也压不住标签里实打实的那几个字。改成序号代称后，
    上下文里不存在任何昵称字面量，这类污染从数据源头消失。

    编号取自完整历史（而不是截断后的窗口），保证话题推进时编号不会整体错位。
    """
    labels: Dict = {}
    user_keys: List[str] = []
    assistant_keys: List[str] = []
    for msg in history or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        if role == "user":
            key = str(msg.get("sender_id") or "").strip()
            if key:
                labels[key] = _speaker_label(USER_LABEL_PREFIX, user_keys, key)
        elif role == "assistant" and msg.get("speaker"):
            key = str(msg["speaker"]).strip()
            labels[key] = _speaker_label(ASSISTANT_LABEL_PREFIX, assistant_keys, key)
    return labels


def build_merged_history(history: list, ctx: RoleContext) -> List[dict]:
    """将持久化历史转换为对话消息列表（合并同角色相邻消息，附带说话人序号标签）。

    助手消息若带有 tool_notes（上一轮工具调用的结果摘要，见主流程回填），
    只在**最近一条**这样的消息后面追加一条备查备注：
    让模型回答"你唱一段我听听"之类的追问时能直接引用此前搜到的内容，
    而不是因为上下文里没有结果又重新搜索一遍。
    """
    n = max(0, int(ctx.get("history_length", 8) or 0))
    history_data = history[-n:] if n > 0 else []
    labels = build_speaker_labels(history)
    merged = []
    notes_idx = next((i for i in range(len(history_data) - 1, -1, -1)
                      if str(history_data[i].get("tool_notes") or "").strip()), None)
    for idx, msg in enumerate(history_data):
        role = msg.get("role", "user")
        content = str(msg.get("content", ""))
        if role not in ["user", "assistant"] or not content:
            continue
        label = ""
        if role == "user":
            label = labels.get(str(msg.get("sender_id") or "").strip(), "")
        elif msg.get("speaker"):
            label = labels.get(str(msg["speaker"]).strip(), "")
        if label:
            content = f"[{label}] {content}"
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] += "\n" + content
        else:
            merged.append({"role": role, "content": content})
        if idx == notes_idx:
            # 注入总量与存入侧同用一组配置键约束（条数×单条字数），避免旧搜索结果挤爆上下文
            note_chars = max(100, int(ctx.get("tool_notes_max_chars", 500) or 500))
            max_entries = max(1, int(ctx.get("tool_notes_max_entries", 4) or 4))
            merged.append({"role": "user", "content":
                "【此前工具查询结果备查】下面是上一轮通过工具查到的结果记录，"
                "回答用户追问时直接引用这些内容，严禁就同一内容再次调用搜索/抓取：\n"
                + str(msg.get("tool_notes"))[:note_chars * max_entries + 200]})
    return merged


def speaker_labeled_lines(history: list, limit: int = 0, max_chars: int = 120) -> List[str]:
    """把历史转成「说话人序号: 正文」的行列表，供摘要/话题等辅助请求使用。

    与 build_merged_history 同一套匿名规则：绝不让昵称或 QQ 号出现在送给模型的文本里。
    """
    labels = build_speaker_labels(history)
    msgs = [m for m in (history or []) if isinstance(m, dict)
            and m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip()]
    if limit and limit > 0:
        msgs = msgs[-limit:]
    lines = []
    for msg in msgs:
        if msg.get("role") == "user":
            who = labels.get(str(msg.get("sender_id") or "").strip(), "用户")
        else:
            who = labels.get(str(msg.get("speaker") or "").strip(), "角色")
        lines.append(f"{who}: {str(msg.get('content', ''))[:max_chars]}")
    return lines


def build_system_prompt(ctx: RoleContext, emotions: dict, extra_parts: Optional[List[str]] = None) -> str:
    parts = [
        str(ctx.get("personality_prompt", "") or ""),
        str(ctx.get("json_prompt", "") or ""),
        str(ctx.get("supplement_prompt", "") or ""),
    ]
    parts.append(
        "【只依据正文】必须严格根据用户当前发送的正文内容回复："
        "对方的状态、情绪、意图只能来自他自己的话，"
        "绝不能从说话人编号、角色名、头像、群名等任何非正文信息里推断或发散。"
    )
    parts.append(
        "【说话人说明】对话历史里行首的 [用户1] [用户2] 是系统分配的匿名说话人编号，"
        "只表示“谁在说话”（私聊里同一人始终是同一个编号）；"
        "角色自己的台词前可能带 [角色名]，那也是标签，不是正文。"
        "编号与说话人的真实昵称、QQ号、身份、状态都没有任何关系，"
        "你的处境、心情以及对方的称呼都不需要知道对方的昵称。"
        "只有当用户在本条正文里主动说出自己的名字或让你怎么称呼时，才可以使用它。"
    )
    parts.append(
        "【说话人标签规则】行首的 [用户1]/[用户2]/[角色名] 只是说话人标签，不是聊天内容本身；"
        "只有标签之后的正文才是真实的消息内容。"
        "不要把标签当成消息正文，也不要把它当作回答依据或模仿对象。"
    )
    parts.append(
        "【身份与称呼规则】你是当前角色本人，与你对话的“主人/用户”始终是发消息的那个人。"
        "绝不要把自己当成用户或主人，也不要替用户表态或描述用户会做的事；"
        "台词里“我”只能是角色自己，“你/主人”只能是用户。"
        "翻译成其他语言时，人称与中文台词完全一致，谁是说话者、谁被称呼不能颠倒。"
    )
    parts.append(
        "【禁止复读】回复必须直接回应并推进对话，输出新内容。"
        "严禁把用户刚说的话，或你上一轮自己说过的话，原样或几乎原样地重复、复述或"
        "“翻译回去”当作回复；也不要把用户句子里的关键词整段照抄进台词。"
        "用户分享状态或经历时，用新的角度去回应，而不是把他的原话再说一遍。"
    )
    if _cfg_bool(ctx, "image_identity_guard_enabled", True):
        parts.append(_image_identity_guard(ctx))
    tl = str(ctx.get("text_lang", "ja") or "").strip().lower()
    if tl in ("ja", "jp"):
        parts.append(
            "【台词语言规则】日文台词必须用假名与汉字书写，不得混入中文词语；"
            "禁止用罗马音或英文拼写冒充日语。"
            "ja 必须与 zh 表达同一件事：同一主语与对象、同一事件、因果与语气，"
            "可以意译但禁止擅自改事件、加戏、省略内容或把谁对谁说话的关系颠倒。"
        )
    elif tl == "en":
        parts.append(
            "【台词语言规则】英文台词直接用自然英文书写，不要音译成其他文字。"
        )
    else:
        parts.append(
            "【台词语言规则】非中文台词必须用目标语言真实书写，"
            "禁止用罗马音或另一种语言冒充。"
        )
    emotion_keys = list(emotions.keys()) if emotions else []
    if ctx.get("llm_judge", True) and emotion_keys:
        parts.append(f"【情绪可选列表】{', '.join(emotion_keys)}")
        parts.append(_emotion_guide(ctx, emotions))
    if ctx.get("enable_time_awareness", False):
        lt = time.localtime()
        try:
            weekday = "周" + "一二三四五六日"[lt.tm_wday % 7]
        except Exception:
            weekday = ""
        parts.append(f"【当前时间】{time.strftime('%Y-%m-%d %H:%M', lt)} {weekday}")
    for part in (extra_parts or []):
        if part:
            parts.append(str(part))
    return "\n".join(p for p in parts if p.strip())


# ---------------------------------------------------------------------------
# 情绪判定引导（解决"模型过度使用默认情绪 pingjing"的问题）
# ---------------------------------------------------------------------------

EMOTION_HINTS = {
    "gaoxing": "开心、大笑、兴奋、被夸后得意、炫耀",
    "shengqi": "生气、不满、被冒犯、警告、凶人",
    "haixiu": "害羞、脸红、被撩到、被夸到不好意思、亲密/色情话题里的不好意思与欲拒还迎",
    "wuyu": "无语、嫌弃、吐槽、敷衍、怼人",
    "jingya": "惊讶、震惊、没想到、被吓一跳",
    "sajiao": "撒娇、卖萌、讨好、黏人、求抱抱",
    "weixie": "威胁、挑逗、撩拨、阴阳怪气、坏笑",
    "zhaoji": "着急、慌张、催促、焦虑、心疼着急",
    "pingjing": "平静、日常陈述、没有明显情绪的普通寒暄",
}

# 常用中文情绪名 → 拼音目录名
_EMOTION_ALIASES = {
    "高兴": "gaoxing", "开心": "gaoxing", "生气": "shengqi", "愤怒": "shengqi",
    "害羞": "haixiu", "羞涩": "haixiu", "无语": "wuyu", "惊讶": "jingya",
    "撒娇": "sajiao", "威胁": "weixie", "挑逗": "weixie", "着急": "zhaoji",
    "平静": "pingjing", "默认": "pingjing",
}
# 常见英文情绪名 → 拼音目录名
_EMOTION_EN = {
    "happy": "gaoxing", "joy": "gaoxing", "angry": "shengqi", "shy": "haixiu",
    "embarrassed": "haixiu", "speechless": "wuyu", "surprised": "jingya",
    "coy": "sajiao", "threat": "weixie", "tease": "weixie", "anxious": "zhaoji",
    "calm": "pingjing", "neutral": "pingjing", "normal": "pingjing",
}

# 「关键词 → 应优先使用的情绪」硬规则：只在对应情绪可用时生效
_EMOTION_HOTWORDS = (
    ("haixiu", ("害羞", "脸红", "不好意思", "羞", "难为情", "欲拒还迎", "害臊")),
    ("weixie", ("坏笑", "挑逗", "撩", "调戏", "威胁", "警告你", "别怪")),
    ("sajiao", ("撒娇", "求你", "抱抱", "摸摸头", "好不好嘛")),
    ("shengqi", ("生气", "哼", "讨厌", "过分", "不许")),
    ("gaoxing", ("哈哈", "好开心", "太棒", "喜欢死")),
    ("jingya", ("诶", "欸", "什么", "真的吗", "不会吧")),
    ("zhaoji", ("着急", "快", "来不及", "担心")),
)


def _resolve_emotion_key(key: str, available) -> str:
    k = str(key or "").strip()
    if k in available:
        return k
    low = k.lower()
    if low in available:
        return low
    for table in (_EMOTION_ALIASES, _EMOTION_EN):
        mapped = table.get(k) or table.get(low)
        if mapped and mapped in available:
            return mapped
    for avail in available:  # 前缀/包含兜底（如 haixiu2 → haixiu）
        if avail and (avail in low or low in avail):
            return avail
    return ""


def _emotion_guide(ctx: RoleContext, emotions: dict) -> str:
    """生成「情绪判定规则」段落：给出每个可用情绪的使用时机，
    并明确禁止"没有明显情绪就一律 pingjing"，亲密/色情语境优先害羞。"""
    if not _cfg_bool(ctx, "emotion_guide_enabled", True):
        return ""
    available = [str(k) for k in (emotions or {})]
    default_voice = str(ctx.get("default_voice", "") or "")
    default_key = _resolve_emotion_key(default_voice, available) or (available[0] if available else "")
    lines = [
        "【情绪判定规则】emotion 字段决定这句话用哪套参考音色，必须按下面时机判断，"
        "不要凭手感随便填，也不要因为拿不准就一律填平静。"
    ]
    for key in available:
        hint = EMOTION_HINTS.get(key.lower())
        if not hint:
            for alias, mapped in _EMOTION_ALIASES.items():
                if mapped == key.lower():
                    hint = EMOTION_HINTS.get(mapped)
                    break
        lines.append(f"- {key}：{hint}" if hint else f"- {key}：按字面意思选用")
    if default_key:
        lines.append(
            f"- {default_key}：只用于真正平淡的陈述、日常寒暄、信息确认；"
            f"{default_key} 是最后的选择，绝不是默认选项。")
    lines.append(
        "【必须优先害羞的场景】只要本轮对话涉及以下任一情形，emotion 必须用「害羞」类情绪"
        "（本角色对应情绪名以【情绪可选列表】为准，通常为 haixiu），"
        "禁止填 pingjing："
        "① 用户的话带有性意味、色情暗示、R18 内容，或要求你配合色情角色扮演；"
        "② 用户对你调情、撩拨、说下流话、夸你身体或让你害羞；"
        "③ 你们的互动正在变得亲密（告白、接吻、拥抱、被摸头、被要求脱衣等）；"
        "④ 你嘴上拒绝、说「不行」「不要」但心里其实愿意（欲拒还迎）——"
        "这种害羞语气才是角色该有的表现，用平静会把情绪演没。")
    lines.append(
        "【判定步骤】先看用户本条消息与最近对话的情绪走向，再决定这一句的情绪："
        "有明确情绪就用对应情绪；只有当整段对话确实没有任何情绪色彩时，才使用平静。")
    extra = str(ctx.get("emotion_guide_extra", "") or "").strip()
    if extra:
        lines.append(extra)
    return "\n".join(lines)


_INTIMATE_RE = re.compile(
    r"色情|涩涩|涩图|黄色|黄段子|下流|下ネタ|情色|工口|做爱|做愛|性爱|性愛|上床|脱衣|脫衣|裸|"
    r"胸|乳|屁股|阴|陰|射|爱液|愛液|潮吹|插入|口交|自慰|发情|發情|r18|R18|h场景|H场景|"
    r"亲我|親我|亲亲|親親|接吻|吻我|舌吻|抱我|摸我|舔|舔我|闻|聞|调教|調教|主人想要|想要我|"
    r"你的身体|妳的身体|怀孕|懷孕|射在|进去|進去|疼我|干我|幹我",
    re.IGNORECASE)

_EMOTION_NUDGE = (
    "【本轮情绪提醒】本轮对话已进入亲密/成人（R18）语境，"
    "本句 emotion 必须使用害羞类情绪（如 haixiu），不要用平静类情绪（如 pingjing）；"
    "台词要体现害羞、脸红、欲拒还迎的语气。"
)


def _cfg_bool(ctx: RoleContext, key: str, default: bool = True) -> bool:
    """配置布尔值容错：兼容 "false"/"0"/""/"否" 这类字符串写法。"""
    try:
        val = ctx.get(key, default)
    except Exception:
        return default
    if isinstance(val, bool):
        return val
    if val is None or val == "":
        return default
    return str(val).strip().lower() not in ("false", "0", "no", "off", "否", "关", "关闭")


def _recent_user_text(history: list, limit: int = 6) -> str:
    parts = []
    for msg in (history or [])[-limit:]:
        if isinstance(msg, dict) and msg.get("role") == "user":
            parts.append(str(msg.get("content", "")))
    return "\n".join(parts)


def emotion_context_note(ctx: RoleContext, user_text: str, history: list = None) -> str:
    """检测亲密/成人语境，返回给模型的本轮情绪提醒（无则空串）。

    仅作提示，不改变任何文本内容；开关 emotion_guide_enabled=false 时禁用。
    """
    if not _cfg_bool(ctx, "emotion_guide_enabled", True):
        return ""
    text = f"{user_text or ''}\n{_recent_user_text(history)}"
    return _EMOTION_NUDGE if _INTIMATE_RE.search(text) else ""


def _image_identity_guard(ctx) -> str:
    """图片身份规则：用户发的图/表情包不是"角色自己的"。

    事故背景：用户发自己的表情包，模型却当成"这是我"，
    于是台词变成"这是我刚才发的表情""这就是我本人"之类。
    """
    name = ctx.character_name or "你扮演的角色"
    return (
        "【图片身份规则】用户发来的图片与表情包，都是**用户**发的东西，"
        f"绝不是{name}自己，也不是{name}的照片、自拍或形象："
        "① 严禁说“这是我”“这就是我”“我的照片”“我发的表情”“我长这样”之类的台词；"
        "② 图里的文字与画面是用户借图表达态度/情绪（吐槽、调侃、卖萌、求安慰等），"
        "你要回应的是用户想表达的意思，而不是把图中的形象当成自己；"
        f"图里的文字是**用户借用的别人的话**，不是{name}自己说过的话，"
        "复述它时必须明确是图里/对方写的，不能说成自己说的；"
        "③ 即使图中人物与你的角色设定相似，也只把它当作用户拿来跟你互动的素材；"
        "④ 只有当用户明确说“这是你”时，才可以按用户的说法接话，但仍不要说成是自己发的。"
    )


def image_identity_note(ctx) -> str:
    """图片轮次的收尾提醒：放在最后一条消息里，约束力最强。"""
    name = ctx.character_name or "当前角色"
    return (
        "【本轮图片身份提醒】本轮回复里的图片是用户发的素材："
        f"画面中的人物、形象以及图上的文字都不是{name}本人，也不是{name}说过的内容。"
        f"严禁出现“图里的人就是{name}”“这就是我”“我的照片/表情”这类认领；"
        "只回应用户借这张图想表达的情绪，以角色身份自然接话。"
    )


_FIRST_PERSON = r"(?:我|本座|人家|咱|俺|吾|吾辈|本尊)"
_CLAUSE_BREAK = r"[^，。！？!?；;、\n]"

_IMAGE_SELF_RE = re.compile(
    r"(?:图|图片|照片|画|表情包|人像)" + _CLAUSE_BREAK + r"{0,6}"
    r"(?:里|中|上|内)?" + _CLAUSE_BREAK + r"{0,6}"
    r"(?:人|女孩|少女|男孩|人物|角色|形象|样子|本人|本尊)" + _CLAUSE_BREAK + r"{0,6}"
    r"(?:就是|正是|是)" + _CLAUSE_BREAK + r"{0,4}" + _FIRST_PERSON
    + r"|" + _FIRST_PERSON + _CLAUSE_BREAK + r"{0,4}(?:就)?(?:是|在)(?:图|图片|照片)(?:里|中|上|片)?"
    + r"|(?:这|那)(?:张|幅|个)?(?:图|图片|照片)" + _CLAUSE_BREAK + r"{0,6}(?:就是|正是|是)"
    + _FIRST_PERSON
    + r"|" + _FIRST_PERSON + r"(?:的|自己|本人的?)(?:照片|自拍|长相|样子|形象|本人|表情包)"
    + r"|" + _FIRST_PERSON + r"[^，。！？!?；;、\n你妳您]{0,4}(?:刚|刚才)?发的"
    r"(?:这张|那张|这个|那个)?(?:图|图片|照片|表情|表情包)"
    + r"|" + _FIRST_PERSON + r"(?:长|就长)(?:这样|那样)")


def image_self_claim(text) -> bool:
    """回复是否把图片里的人物/文字认领成了角色自己。"""
    t = str(text or "")
    if not t.strip():
        return False
    return bool(_IMAGE_SELF_RE.search(t))


IMAGE_CLAIM_WARNING = (
    "警告：你刚才的回复把用户发来的图片当成你自己的照片/形象了（例如“图里的人就是我”"
    "“这是我的照片/表情”这类认领）。用户发的图是**用户**的素材："
    "图中的人物、形象与图上的文字都不是你本人，也不是你说过的话。"
    "请重新生成：只回应用户借这张图想表达的情绪与意图，"
    "以角色身份自然接话，绝不要再出现任何认领图中形象的表述。"
)


def build_chat_messages(ctx: RoleContext, user_text: str, history: list, emotions: dict,
                        extra_parts: Optional[List[str]] = None,
                        history_extra_user_msg: str = "",
                        trailing_notes: Optional[List[str]] = None) -> List[dict]:
    messages = [{"role": "system", "content": build_system_prompt(ctx, emotions, extra_parts)}]
    n = max(0, int(ctx.get("history_length", 8) or 0))
    history_data = history[-n:] if n > 0 else []
    messages.extend(build_merged_history(history, ctx))
    # 重申最近的指令类消息：仅当本轮消息同样在谈提醒/指令类话题时才注入。
    # 事故背景：此前无条件注入，用户接着问"帮我搜 xx 歌词"时，上下文里紧挨着出现
    # "（重申之前的指令）23点提醒我…"，弱模型会把搜索请求答成提醒话题（答非所问）。
    task_keywords = ["提醒", "记住", "要求", "命令", "叫我", "以后", "别忘"]
    if any(kw in str(user_text or "") for kw in task_keywords):
        for msg in reversed(history_data):
            if msg.get("role") == "user":
                content = str(msg.get("content", ""))
                if content and any(kw in content for kw in task_keywords):
                    messages.append({"role": "user", "content":
                        f"（历史指令回顾，仅供参考：这是用户之前说过的指令，"
                        f"不是现在的请求，请优先回答用户最新这条消息）{content}"})
                    break
    if history_extra_user_msg:
        messages.append({"role": "user", "content": history_extra_user_msg})
    messages.append({"role": "user", "content": user_text})
    nudge = emotion_context_note(ctx, user_text, history)
    if nudge:
        # 放在最后一条用户消息之后，作为紧接着的输出约束，命中率最高
        messages.append({"role": "user", "content": nudge})
    for note in (trailing_notes or []):
        if str(note or "").strip():
            messages.append({"role": "user", "content": str(note)})
    return messages


# ---------------------------------------------------------------------------
# 底层请求
# ---------------------------------------------------------------------------

def _parse_extra_body(ctx) -> dict:
    """解析 llm_extra_body（用户自定义的请求体字段，JSON 字符串）。

    用于适配 llama.cpp 等 OpenAI 兼容服务的私有扩展，例如
    {"chat_template_kwargs": {"enable_thinking": false}} 关闭思考模式
    （思考模型经 llama-server --jinja 启动时默认开思考，content 可能为空）。
    解析失败时忽略，不影响请求。
    """
    raw = str(ctx.get("llm_extra_body", "") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception as e:
        print(f"llm_extra_body 解析失败（已忽略）: {e}")
        return {}


def _endpoint_and_payload(ctx: RoleContext, messages: list, stream: bool, tools=None):
    backend = ctx.get("llm_backend", "ollama")
    base_url = str(ctx.get("llm_base_url", "http://127.0.0.1:11434")).rstrip("/")
    model = ctx.get("llm_model_name", "")
    timeout = ctx.get("llm_timeout", 120)
    enable_think = ctx.get("enable_think", False)
    temp_cap = float(ctx.get("temperature_max", 1.0) or 1.0)
    temperature = min(temp_cap, float(ctx.get("temperature", 1.0) or 1.0))
    # LLM 采样参数（WebUI 可配，默认开启）：top_p / top_k / 重复惩罚。
    # 默认值取 Ollama 官方默认（top_k=40, top_p=0.9, repeat_penalty=1.1），
    # 关闭 llm_sampling_enabled 后请求只带温度等基本参数。
    sampling_on = bool(ctx.get("llm_sampling_enabled", True))
    top_p = float(ctx.get("llm_top_p", 0.9) or 0.9)
    top_k = int(ctx.get("llm_top_k", 40) or 40)
    repeat_penalty = float(ctx.get("llm_repeat_penalty", 1.1) or 1.1)
    messages = normalize_messages_for_backend(messages, backend)
    if backend == "ollama":
        endpoint = f"{base_url}/api/chat"
        payload = {
            "model": model, "messages": messages, "stream": stream,
            "think": enable_think,
            "options": {
                "num_ctx": int(ctx.get("num_ctx", 8192)),
                "temperature": temperature,
            },
        }
        if sampling_on:
            payload["options"].update({"top_p": top_p, "top_k": top_k,
                                       "repeat_penalty": repeat_penalty})
        if not enable_think:
            # 双保险：部分推理模型（qwen3 等）只看 chat template 开关，
            # 单给 "think": false 仍会把思维链写进 content。
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if tools:
            payload["tools"] = [
                {**tool, "function": {**tool.get("function", {}), "parameters": {
                    key: value for key, value in (tool.get("function", {}).get("parameters", {}) or {}).items()
                    if key != "required"
                }}}
                for tool in tools
            ]
        return backend, endpoint, payload, {}, timeout
    endpoint = f"{base_url}/chat/completions"
    api_key = ctx.get("llm_api_key", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = {"model": model, "messages": messages, "stream": stream,
               "temperature": temperature,
               "max_tokens": 8192}
    if sampling_on:
        # OpenAI 标准参数只有 top_p；top_k / 重复惩罚不是通用字段，
        # 严格按 OpenAI 规范的服务会拒绝未知参数，所以只在 Ollama 后端发送。
        payload["top_p"] = top_p
    payload.update(_parse_extra_body(ctx))
    if not enable_think:
        payload["enable_thinking"] = False
    if tools:
        payload["tools"] = tools
    return backend, endpoint, payload, headers, timeout


def _ollama_tools_without_required(tools):
    result = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function") or {}
        parameters = function.get("parameters") or {}
        result.append({**tool, "function": {**function, "parameters": {
            key: value for key, value in parameters.items() if key != "required"
        }}})
    return result


def _is_ollama_required_schema_error(error_text: str) -> bool:
    text = str(error_text or "").lower()
    return "toolfunctionparameters" in text and "required" in text and "cannot unmarshal" in text


async def chat_once(ctx: RoleContext, messages: list, tools=None) -> Dict:
    backend, endpoint, payload, headers, timeout = _endpoint_and_payload(ctx, messages, False, tools)
    base_url = str(ctx.get("llm_base_url", "http://127.0.0.1:11434")).rstrip("/")
    start = time.time()
    data = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=timeout, proxy=None, trust_env=False,
                                         verify=verified_context()) as client:
                resp = await client.post(endpoint, json=payload, headers=headers)
                if resp.status_code >= 400:
                    detail = resp.text[:400]
                    if backend == "ollama" and tools and _is_ollama_required_schema_error(detail):
                        payload["tools"] = _ollama_tools_without_required(tools)
                        if attempt == 0:
                            print("Ollama 工具 schema 的 required 字段不兼容，已重试兼容格式。")
                            await asyncio.sleep(1.2)
                            continue
                        raise RuntimeError(f"HTTP {resp.status_code} {endpoint}：{detail}")
                    raise RuntimeError(f"HTTP {resp.status_code} {endpoint}：{detail}")
                resp.raise_for_status()
                data = resp.json()
            break
        except httpx.ConnectError as e:
            if attempt == 0:
                await asyncio.sleep(1.2)
                continue
            raise ConnectionError(conn_fail_hint(endpoint, base_url, backend)) from e
        except httpx.TimeoutException as e:
            raise ConnectionError(f"请求 LLM 服务超时：{endpoint}（timeout={timeout}s）。"
                                  "若模型加载较慢可调大 llm_timeout。") from e
    ms = (time.time() - start) * 1000
    maybe_unload_old_models(ctx)
    content = ""
    tool_calls = []
    enable_think = bool(ctx.get("enable_think", False))
    if backend == "ollama":
        msg = data.get("message", {}) or {}
        # 只取真正的回答：thinking 字段与 content 内嵌的  thinking 块都会被剥离
        content = extract_answer_from_message(msg)
        think_text = thinking_fragments(msg) or str(msg.get("thinking") or "")
        if think_text:
            print(f"【模型思考已剥离】{think_text[:200]}")
        tool_calls = msg.get("tool_calls") or []
    else:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {}) or {}
        content = extract_answer_from_message(msg)
        think_text = thinking_fragments(msg)
        if think_text:
            print(f"【模型思考已剥离】{think_text[:200]}")
        tool_calls = msg.get("tool_calls") or []
    return {"content": content, "tool_calls": tool_calls, "ms": ms, "backend": backend}


async def _stream_chat_inner(ctx: RoleContext, messages: list) -> AsyncGenerator[Dict, None]:
    backend, endpoint, payload, headers, timeout = _endpoint_and_payload(ctx, messages, True)
    start = time.time()
    first_token_ms = None
    async with httpx.AsyncClient(timeout=timeout, proxy=None, trust_env=False,
                                 verify=verified_context()) as client:
        async with client.stream("POST", endpoint, json=payload, headers=headers) as resp:
            if resp.status_code >= 400:
                detail = ""
                try:
                    detail = (await resp.aread()).decode("utf-8", errors="ignore")[:400]
                except Exception:
                    detail = ""
                raise RuntimeError(f"HTTP {resp.status_code} {endpoint}：{detail}")
            resp.raise_for_status()
            if backend == "ollama":
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except Exception:
                        continue
                    msg = chunk.get("message", {}) or {}
                    # 推理模型会把思维链放在 thinking / reasoning 字段里：
                    # 绝不能当成台词流出去（否则会合成一整段思考内容语音）
                    if thinking_fragments(msg):
                        continue
                    delta_text = msg.get("content", "") or ""
                    if not delta_text and not (msg.get("tool_calls") or []):
                        continue
                    if first_token_ms is None and delta_text:
                        first_token_ms = (time.time() - start) * 1000
                    yield {"delta": delta_text,
                           "tool_calls": msg.get("tool_calls") or [],
                           "first_token_ms": first_token_ms,
                           "ms_done": (time.time() - start) * 1000 if chunk.get("done") else None}
            else:
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except Exception:
                        continue
                    delta = ((chunk.get("choices") or [{}])[0].get("delta") or {})
                    if thinking_fragments(delta):
                        continue  # OpenAI 兼容服务常把思维链放在 reasoning_content
                    delta_text = delta.get("content", "") or ""
                    if first_token_ms is None and delta_text:
                        first_token_ms = (time.time() - start) * 1000
                    yield {"delta": delta_text,
                           "tool_calls": delta.get("tool_calls") or [],
                           "first_token_ms": first_token_ms,
                           "ms_done": None}


async def stream_chat(ctx: RoleContext, messages: list) -> AsyncGenerator[Dict, None]:
    """流式对话（对外入口）：连接异常时给出端点明确的错误。"""
    base_url = str(ctx.get("llm_base_url", "http://127.0.0.1:11434")).rstrip("/")
    backend = ctx.get("llm_backend", "ollama")
    try:
        async for chunk in _stream_chat_inner(ctx, messages):
            yield chunk
    except httpx.ConnectError as e:
        raise ConnectionError(conn_fail_hint(base_url, base_url, backend)) from e
    except httpx.TimeoutException as e:
        raise ConnectionError(f"请求 LLM 服务超时：{base_url}。若模型加载较慢可调大 llm_timeout。") from e


def _merge_openai_tool_fragments(frags: list) -> list:
    """将 OpenAI 流式返回的 tool_calls 分片合并为完整对象。"""
    merged = {}
    for frag in frags:
        idx = frag.get("index", 0)
        slot = merged.setdefault(idx, {"id": "", "type": "function",
                                       "function": {"name": "", "arguments": ""}})
        if frag.get("id"):
            slot["id"] = frag["id"]
        fn = frag.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]
    return [merged[k] for k in sorted(merged)]


# ---------------------------------------------------------------------------
# 句子规整与流式解析
# ---------------------------------------------------------------------------

# 假名是日文独有的文字：展示文本里出现含假名的片段，说明模型把日文台词
# 连同中文翻译一起塞进了同一字段（聊天里就会中日混杂）
_KANA_RE = re.compile(r"[\u3041-\u30FF]")
_DISPLAY_SEG_SPLIT = re.compile(r"([。！？!?…]+|\n)")


def strip_other_language_from_display(display: str, display_lang: str, text_lang: str) -> str:
    """把展示文本里混入的"口语语言"片段剔除（如中文展示里混进的日文台词）。

    事故背景：模型偶尔把一句台词的中文翻译和日文原文连在一起塞进同一字段，
    甚至同一句话拆成中/日两条发给用户——聊天里就会中日混杂。
    日文片段按假名识别（假名为日文独有文字），按句读切成段后整段剔除；
    其他语言组合（如英文混排）没有可靠的文字判据，不做处理。
    纯口语语言的句子剔除后为空串：聊天不发言文本，语音字段照常合成。
    """
    text = str(display or "")
    if not text:
        return text
    dl, tl = str(display_lang or "").lower(), str(text_lang or "").lower()
    if not dl or dl == "auto" or dl == tl:
        return text
    # 只有"中文展示"能可靠地按假名剔除日文片段，其他组合不动
    if "zh" not in dl or not _KANA_RE.search(text):
        return text
    parts = _DISPLAY_SEG_SPLIT.split(text)
    kept = []
    prev_dropped = False
    for seg in parts:
        if not seg:
            continue
        if _KANA_RE.search(seg):
            # 片段以收尾引号/括号开头时先保留它（引号属于前面那句中文）
            lead = re.match(r"^[」”』〉》]", seg)
            if lead:
                kept.append(lead.group(0))
            prev_dropped = True
            continue
        if prev_dropped and not real_text(seg):
            # 丢弃片段后面残留的句读符号一并去掉，不留悬空的"……"
            prev_dropped = False
            continue
        prev_dropped = False
        kept.append(seg)
    return "".join(kept).strip()


def normalize_single(obj, ctx: RoleContext, emotions: dict, user_text: str) -> Dict:
    """将单个句子对象规整为 {zh, lang, display, emotion}。"""
    s = obj if isinstance(obj, dict) else {"zh": str(obj)}
    text_lang = ctx.get("text_lang", "ja")
    display_lang = ctx.get("display_lang", "zh")
    default_voice = ctx.get("default_voice", "pingjing")
    zh = str(s.get("zh", "")).strip()
    raw_lang = str(s.get(text_lang, "")).strip()
    if not zh:
        # 空台词不复读用户消息（user_text），改用安全台词
        zh = raw_lang or FALLBACK_REPLY
    # URL 绝不进语音（TTS 会逐字符念成乱码）：口语字段剔除链接，展示字段保留。
    # 纯链接的台词剔除后为空，发送层会跳过该句语音、照常发送文本。
    lang = strip_urls_for_tts(raw_lang)
    if not lang:
        lang = strip_urls_for_tts(zh)
    if display_lang == "auto":
        display = raw_lang or zh
    else:
        display = str(s.get(display_lang, "")).strip() or zh
    # 展示文本只保留展示语言（display_pure_language，默认开启）：
    # 模型把日文台词连同中文翻译塞进同一字段、甚至中/日各发一条时，
    # 剔除含假名的片段；纯口语语言的句子展示为空（不发文本，语音照常）。
    if display and bool(ctx.get("display_pure_language", True)):
        display = strip_other_language_from_display(display, display_lang, text_lang)
    emo = str(s.get("emotion", "")).strip()
    # 模型输出的情绪名可能是中文/英文/大小写/带标点变体：
    # 走别名与子串解析（害羞→haixiu、shy→haixiu、haixiu。→haixiu），
    # 解析不出再回退默认音色。此前是严格全等，稍有出入就静默变成 pingjing。
    if not ctx.get("llm_judge", True) or not emo:
        emo = default_voice
    else:
        emo = _resolve_emotion_key(emo, emotions) or default_voice
    # 文本清洗：用户配置的屏蔽字符/词不进语音也不进发送文本
    zh = apply_text_clean(zh, ctx)
    lang = apply_text_clean(lang, ctx)
    display = apply_text_clean(display, ctx)
    return {"zh": zh, "lang": lang, "display": display, "emotion": emo}


# 提示词约定「一个句号才算一句话」；模型偶尔把多句写进同一个数组元素，
# 导致多句内容合成进同一段语音。此处按句号/问号切分（不切感叹号/省略号，
# 避免把「哼！xxx。」这类单句拆碎），中日文子句数对齐时才拆，防止错位。
_SENT_BOUNDARY = re.compile(r'(?<=[。？])')
_PUNCT_ONLY_RE = re.compile(r"[\s\u3000。，、！？…～~；：,.!?;:'\"“”‘’（）()\[\]【】\-—_*#@/\\]+")
_MIN_CLAUSE_CHARS = 4
_CONNECTIVES = ("但是", "可是", "不过", "然后", "所以", "而且", "因为", "如果", "于是", "其实",
                "当然", "只是", "另外", "总之", "毕竟", "甚至", "然而", "并且", "以及", "或者",
                "不然", "否则", "同时", "接着", "随后", "因此", "何况")


def real_text(text) -> str:
    return _PUNCT_ONLY_RE.sub("", str(text or ""))


def _is_connective(text) -> bool:
    t = real_text(text)
    return any(t.startswith(w) for w in _CONNECTIVES)


def _merge_short_sentences(sentences: List[Dict]) -> List[Dict]:
    items = [dict(s) for s in sentences if isinstance(s, dict)]
    out: List[Dict] = []
    for i, s in enumerate(items):
        short = len(real_text(s.get("zh"))) < _MIN_CLAUSE_CHARS
        if short and _is_connective(s.get("zh")) and i + 1 < len(items):
            nxt = items[i + 1]
            for k in ("zh", "lang", "display"):
                nxt[k] = f"{s.get(k, '')}{nxt.get(k, '')}"
            continue
        if short and out:
            prev = out[-1]
            for k in ("zh", "lang", "display"):
                prev[k] = f"{prev.get(k, '')}{s.get(k, '')}"
            continue
        out.append(s)
    if len(out) >= 2 and len(real_text(out[0].get("zh"))) < _MIN_CLAUSE_CHARS:
        head = out.pop(0)
        for k in ("zh", "lang", "display"):
            out[0][k] = f"{head.get(k, '')}{out[0].get(k, '')}"
    return out


def _split_clauses(text: str) -> List[str]:
    if not text:
        return []
    return [p for p in _SENT_BOUNDARY.split(text) if p and p.strip()]


def split_multi_clause_sentences(sentences: List[Dict]) -> List[Dict]:
    out = []
    for s in sentences:
        zh_parts = _split_clauses(s.get("zh", ""))
        if not zh_parts:
            out.append(s)
            continue
        n = len(zh_parts)
        lang = s.get("lang", "")
        lang_parts = _split_clauses(lang)
        if not lang or not lang_parts:
            for i, zh_part in enumerate(zh_parts):
                out.append({**s, "zh": zh_part, "lang": zh_part, "display": zh_part})
            continue
        if len(lang_parts) != n:
            out.append(s)
            continue
        display = s.get("display", "")
        disp_parts = _split_clauses(display)
        if not str(display).strip():
            disp_list = [""] * n
        elif display == lang and lang_parts:
            disp_list = list(lang_parts)
        elif len(disp_parts) == n:
            disp_list = disp_parts
        else:
            bounds = _scaled_bounds(_piece_weights(zh_parts), _piece_weights(disp_parts), n)
            disp_list = _group_by_bounds(disp_parts, bounds)
            if len(disp_list) != n:
                disp_list = list(zh_parts)
        for i in range(n):
            piece = {"zh": zh_parts[i],
                     "lang": lang_parts[i] if lang and lang_parts else lang,
                     "display": disp_list[i] if i < len(disp_list)
                     else _display_piece(zh_parts[i], lang_parts[i]),
                     "emotion": s.get("emotion", "")}
            out.append(piece)
    return _merge_short_sentences(out)


# 形似JSON但彻底无法修复时的安全台词（绝不把JSON语法当台词念出来）
FALLBACK_REPLY = "呜……刚才走神了，主人再说一遍好吗？"


_TERMINALS = set("。？！；?!")
# 可跟随前一个终止标点、同属该句句末的标点（如「！？」「?!」连用）
_TRAILING_TERMINALS = set("。？！；?!…～~")


def split_terms(text: str) -> List[str]:
    """按中日文终止标点切分文本；ASCII 句点不视为句末。

    终止标点后紧跟的同组标点（「！？」「?!」）并入同一句，
    避免切出「？」这种纯标点碎片——那会让中/日句数对不上，
    进而触发"整段合成"，让文字与语音不再逐句对应。
    """
    text = str(text or "")
    if "http://" in text or "https://" in text:
        # URL 内部的 ./?/& 不能当终止符：先占位保护，切完再还原
        tokens = []

        def _mask(m: "re.Match") -> str:
            tokens.append(m.group(0))
            return f"\x00{len(tokens) - 1}\x00"

        text = re.sub(r"https?://\S+", _mask, text)
        restored = split_terms(text)
        return [re.sub(r"\x00(\d+)\x00", lambda m: tokens[int(m.group(1))], p)
                for p in restored]
    out = []
    cur = []
    chs = list(text)
    i = 0
    while i < len(chs):
        ch = chs[i]
        cur.append(ch)
        if ch in _TERMINALS:
            j = i + 1
            while j < len(chs) and chs[j] in _TRAILING_TERMINALS:
                cur.append(chs[j])
                j += 1
            out.append("".join(cur))
            cur = []
            i = j
            continue
        i += 1
    if cur:
        out.append("".join(cur))
    # 纯标点碎片单独成句没有意义：合并回上一句（开头碎片则丢弃）
    merged: List[str] = []
    for piece in out:
        if piece.strip() and not real_text(piece):
            if merged:
                merged[-1] += piece
                continue
            if len(out) > 1:
                continue
        merged.append(piece)
    return [p for p in merged if p.strip()]


def _piece_weights(pieces: List[str]) -> List[int]:
    return [max(1, len(real_text(p))) for p in pieces]


def _group_bounds(weights: List[int], groups: int) -> List[int]:
    """按权重把 pieces 均分成 groups 段，返回每段的结束下标（不含）。"""
    n = len(weights)
    if groups <= 1:
        return [n]
    if groups >= n:
        return list(range(1, n + 1))
    total = sum(weights) or n
    bounds: List[int] = []
    prev = 0
    for g in range(groups - 1):
        target = total * (g + 1) / groups
        last_allowed = n - (groups - 1 - g)
        best, best_gap = prev + 1, None
        for end in range(prev + 1, last_allowed + 1):
            gap = abs(sum(weights[:end]) - target)
            if best_gap is None or gap < best_gap:
                best_gap, best = gap, end
        bounds.append(best)
        prev = best
    bounds.append(n)
    return bounds


def _group_by_bounds(pieces: List[str], bounds: List[int]) -> List[str]:
    out: List[str] = []
    start = 0
    for end in bounds:
        out.append("".join(pieces[start:end]))
        start = end
    return out


def _scaled_bounds(source_weights: List[int], target_weights: List[int],
                   groups: int) -> List[int]:
    """按 source 侧算出的分段比例，在 target 侧取最接近的边界。"""
    n_target = len(target_weights)
    if n_target <= 0:
        return []
    if groups >= n_target:
        return list(range(1, n_target + 1))
    if groups <= 1:
        return [n_target]
    total_source = sum(source_weights) or len(source_weights)
    total_target = sum(target_weights) or n_target
    ratios = [sum(source_weights[:b]) / total_source
              for b in _group_bounds(source_weights, groups)[:-1]]
    bounds: List[int] = []
    prev = 0
    for i, ratio in enumerate(ratios):
        target = total_target * ratio
        last_allowed = n_target - (len(ratios) - i)
        best, best_gap = prev + 1, None
        for end in range(prev + 1, last_allowed + 1):
            gap = abs(sum(target_weights[:end]) - target)
            if best_gap is None or gap < best_gap:
                best_gap, best = gap, end
        bounds.append(best)
        prev = best
    bounds.append(n_target)
    return bounds


def _group_pieces(pieces: List[str], weights: List[int], groups: int) -> List[str]:
    """把 pieces 按权重尽量均匀地合并成 groups 组（保持原文与顺序）。"""
    if groups <= 0:
        return list(pieces)
    return [p for p in _group_by_bounds(pieces, _group_bounds(weights, groups)) if p]


def _display_piece(zh_piece: str, lang_piece: str) -> str:
    """展示文本兜底：拿不到展示语言内容时绝不把口语原文（TTS 台词）当展示文本。"""
    return "" if real_text(zh_piece) == real_text(lang_piece) else zh_piece


def align_bilingual_parts(zh_parts: List[str], lang_parts: List[str],
                          display_parts: Optional[List[str]] = None) -> tuple:
    """把中文分句、台词语言分句、展示文本按同一套比例边界对齐成同样多的组。

    两侧各自的句读习惯不同（中文多「！」，译文常写「！？」或整段无标点），
    只要有一侧句数更少，就按比例把该侧并入相邻组；组边界由中文侧的字符权重
    决定，再等比映射到台词侧，保证"第 K 组的中文"和"第 K 组的台词"覆盖
    同一段内容。返回 (zh 组, lang 组, display 组)；display 组可能少于 zh 组
    （展示文本自身句数不足时），调用方按段兜底。
    """
    zh_parts = [str(p) for p in (zh_parts or []) if str(p).strip()]
    lang_parts = [str(p) for p in (lang_parts or []) if str(p).strip()]
    disp_parts = [str(p) for p in (display_parts or []) if str(p).strip()]
    if not zh_parts or not lang_parts:
        return zh_parts, lang_parts, disp_parts
    zh_weights = _piece_weights(zh_parts)
    lang_weights = _piece_weights(lang_parts)
    groups = min(len(zh_parts), len(lang_parts))
    if len(zh_parts) == len(lang_parts):
        zh_bounds = list(range(1, len(zh_parts) + 1))
        lang_bounds = list(zh_bounds)
    else:
        zh_bounds = _group_bounds(zh_weights, groups)
        lang_bounds = _scaled_bounds(zh_weights, lang_weights, groups)
    zh_groups = _group_by_bounds(zh_parts, zh_bounds)
    lang_groups = _group_by_bounds(lang_parts, lang_bounds)
    if not disp_parts:
        disp_groups: List[str] = []
    elif disp_parts == zh_parts:
        disp_groups = _group_by_bounds(disp_parts, zh_bounds)
    elif disp_parts == lang_parts:
        disp_groups = list(lang_groups)
    else:
        disp_groups = _group_by_bounds(
            disp_parts, _scaled_bounds(zh_weights, _piece_weights(disp_parts), len(zh_groups)))
        if len(disp_groups) != len(zh_groups) and zh_parts != lang_parts:
            disp_groups = list(zh_groups)
    return zh_groups, lang_groups, disp_groups


def segment_for_tts(sentences: List[Dict]) -> List[Dict]:
    """把一个含多句的句子块按终止标点拆成多个句子，供语音/文本分开发送逐句合成。

    安全约束（防止"语音漏句/文字与语音对不上/展示文本混进口语原文"）：
    - 中文与台词句数不一致时按同一套比例边界对齐后逐句合成，既不丢内容，
      也保证"看到的每一句"对应"听到的那一段"；
    - 展示文本只在自身有对应内容时才输出，绝不用台词原文（TTS 台词）兜底，
      展示文本被清洗为空时这些分段只发语音、不发文本。
    """
    result = []
    for s in sentences:
        zh = str(s.get("zh", "") or "")
        lang = str(s.get("lang", "") or "")
        display = str(s.get("display", "") or "")
        parts = split_terms(zh)
        if len(parts) <= 1:
            result.append(s)
            continue
        had_display = bool(display)
        if not lang or lang == zh:
            lang_parts = list(parts)
        else:
            lang_parts = split_terms(lang)
        if display and display == lang and lang != zh:
            disp_parts = list(lang_parts)
        elif display and display != zh:
            disp_parts = split_terms(display)
        else:
            disp_parts = list(parts)
        zh_groups, lang_groups, disp_groups = align_bilingual_parts(
            parts, lang_parts, disp_parts)
        if not zh_groups or len(zh_groups) != len(lang_groups):
            print(f"分句合成：中文与台词无法对齐（zh {len(parts)} 句 / 台词 "
                  f"{len(lang_parts)} 句），改为整段合成: {zh[:40]!r}")
            result.append(s)
            continue
        if len(zh_groups) != len(parts):
            print(f"分句合成：中文 {len(parts)} 句 / 台词 {len(lang_parts)} 句，"
                  f"已按长度对齐为 {len(zh_groups)} 段（逐段对应、不丢内容）")
        emotion = s.get("emotion", "")
        for i, piece in enumerate(zh_groups):
            lang_piece = lang_groups[i]
            if not had_display:
                text_piece = ""
            elif i < len(disp_groups):
                text_piece = disp_groups[i]
            else:
                text_piece = _display_piece(piece, lang_piece)
            result.append({
                "zh": piece,
                "lang": lang_piece,
                "display": text_piece,
                "emotion": emotion,
            })
    return result


def _drop_bilingual_duplicates(sentences: List[dict], text_lang: str) -> List[dict]:
    """模型把同一句台词中 / 日各发一条：一条只有 zh 有内容（text_lang 字段为空，
    TTS 会拿中文台词配日语音色），另一条 text_lang 字段有内容。保留后者、
    丢弃前者，避免同一句话合成两次语音、文本发两遍。

    只有当仅含 zh 的块与 text_lang 有内容的块数量相等（系统性成对输出），
    或 zh 字段能和后者对上时才丢弃，防止误删真正只写了一种语言的句子。
    """
    filled = [s for s in sentences if str(s.get(text_lang, "") or "").strip()]
    if not filled:
        return sentences
    zh_only = [s for s in sentences if not str(s.get(text_lang, "") or "").strip()]
    if not zh_only:
        return sentences

    def _norm(t) -> str:
        return re.sub(r"[\s，,。．.、！!？?～~…—・「」『』\"“”‘'（）()：:；;]+", "",
                      str(t or ""))

    filled_zh = {_norm(s.get("zh")) for s in filled}
    drop = set()
    for s in zh_only:
        zh_norm = _norm(s.get("zh"))
        if zh_norm and zh_norm in filled_zh:
            drop.add(id(s))
    if len(zh_only) == len(filled):
        # 成对输出（各 K 条，如 仅zh 3 条 + 带ja 3 条）：仅当中文内容能与带 ja
        # 的块对上一半以上才整体丢弃，防止模型单纯漏填 ja 时误删真实句子
        matched = sum(1 for s in zh_only
                      if _norm(s.get("zh")) and _norm(s.get("zh")) in filled_zh)
        if matched >= max(1, len(zh_only) // 2):
            drop.update(id(s) for s in zh_only)
    if not drop:
        return sentences
    kept = [s for s in sentences if id(s) not in drop]
    print(f"[分句去重] 检测到同一台词中/日各发一条，丢弃 {len(drop)} 条仅含 zh 的重复块"
          f"（保留 {len(kept)} 条）")
    return kept or sentences


def _block_lang_text(s: dict, text_lang: str) -> str:
    return (str(s.get(text_lang, "") or "").strip()
            or str(s.get("lang", "") or "").strip()
            or str(s.get("zh", "") or "").strip())


def _drop_repeated_lang_blocks(sentences: List[dict], text_lang: str) -> List[dict]:
    """同一条台词原文被模型写进多个块时只保留一条，避免同一句语音合成两次。

    模型偶发把口语原文既写进带译文的块、又单独再写一条（该块的中文台词就是
    口语原文本身，没有独立译文）。这种重复块合成出的语音完全一样，会让用户
    连续听到两遍同一句：保留带独立译文的那条，其余重复块的中文/展示文本
    并入它之后丢弃，语音只合成一次、文字内容也不丢。
    """
    def _norm(t) -> str:
        return re.sub(r"[\s，,。．.、！!？?～~…—・「」『』\"“”‘'（）()：:；;]+", "",
                      str(t or ""))

    groups: dict = {}
    for idx, s in enumerate(sentences):
        if not isinstance(s, dict):
            continue
        key = _norm(_block_lang_text(s, text_lang))
        if not key or len(key) < 2:
            continue
        groups.setdefault(key, []).append(idx)
    drop = set()
    for key, idxs in groups.items():
        if len(idxs) < 2:
            continue
        base_idx = next((i for i in idxs
                         if _norm(sentences[i].get("zh")) and _norm(sentences[i].get("zh")) != key),
                        idxs[0])
        base = sentences[base_idx]
        base_zh = _norm(base.get("zh"))
        for i in idxs:
            if i == base_idx:
                continue
            other = sentences[i]
            other_zh = str(other.get("zh", "") or "").strip()
            if _norm(other_zh) and _norm(other_zh) != key and _norm(other_zh) != base_zh:
                for field in ("zh", "display"):
                    extra = str(other.get(field, "") or "").strip()
                    if not extra:
                        continue
                    current = str(base.get(field, "") or "").strip()
                    if extra not in current:
                        base[field] = (current + extra).strip()
                base_zh = _norm(base.get("zh"))
            drop.add(id(other))
    if not drop:
        return sentences
    kept = [s for s in sentences if id(s) not in drop]
    print(f"[分句去重] 检测到重复的台词原文块，合并并丢弃 {len(drop)} 条重复"
          f"（保留 {len(kept)} 条）")
    return kept or sentences


def normalize_sentences(content: str, ctx: RoleContext, emotions: dict, user_text: str) -> List[Dict]:
    """将 LLM 最终输出规整为句子列表：[{zh, lang, display, emotion}]。

    兼容多种模型输出形态：
    - 标准包装 {"sentences": [{...}, {...}]}
    - 多个独立 JSON 块（模型不套包装时常见；旧版只取第一块，
      导致"提示词要求至少两个JSON块却只合成一条语音"）
    - 顶层 JSON 数组 [{...}, {...}]
    - JSON 语法病（未转义引号/裸换行/尾逗号/截断）：宽容修复后照常解析
    - 包装块之外又补了独立块、sentences 值为字符串、纯文本兜底
    """
    default_voice = ctx.get("default_voice", "pingjing")
    raw = strip_thinking(content or "")   # 兜底：JSON 里若夹带思考内容也一并剥掉
    objs = extract_json_objects(content)

    sentences = None
    wrapper = next((o for o in objs if isinstance(o, dict)
                    and isinstance(o.get("sentences"), list) and o["sentences"]), None)
    if wrapper is not None:
        # 包装块 + 文本中其他散落的句子块合并（模型有时在包装外又补一块）
        sentences = list(wrapper["sentences"]) + [
            o for o in objs if o is not wrapper and _is_sentence_like(o)]
    else:
        sentence_like = [o for o in objs if _is_sentence_like(o)]
        if sentence_like:
            sentences = sentence_like

    if sentences is not None:
        # 丢弃只有 emotion 等元数据、没有台词内容的句子对象，
        # 否则会被用户消息兜底，把用户刚说的话朗读出来
        sentences = [s for s in sentences if sentence_obj_has_text(s)]
        if not sentences:
            sentences = None

    if sentences is None:
        # 无句子块：单对象兜底（含 "sentences": "文本" 的错误格式）
        first = next((o for o in objs if isinstance(o, dict)), None)
        if first is not None and (first.get("sentences") is not None
                                  or any(k in first for k in _SENTENCE_KEYS)):
            s = first.get("sentences")
            if isinstance(s, str) and s.strip():
                sentences = [{"zh": s}]
            else:
                zh = str(first.get("zh", "") or "").strip()
                if not zh:
                    # 无任何台词内容（如 {"sentences": []}）：不复读用户消息
                    sentences = [{"zh": FALLBACK_REPLY, "lang": FALLBACK_REPLY,
                                  "display": FALLBACK_REPLY, "emotion": default_voice}]
                else:
                    sentences = [{"zh": zh, "emotion": first.get("emotion", default_voice)}]
        else:
            # 模型输出了纯文本或 JSON 标量（如裸数字"72"），按纯文本整句兜底
            if not raw:
                raw = "出错了。"
            elif _looks_like_json(raw):
                # 形似JSON但已无法修复：绝不能把JSON语法当台词念出来
                # （否则会出现"回复中含JSON块"的事故），改用安全台词
                raw = FALLBACK_REPLY
            sentences = [{"zh": raw, "lang": raw, "display": raw, "emotion": default_voice}]
    text_lang = str(ctx.get("text_lang", "ja") or "ja")
    sentences = _drop_bilingual_duplicates(sentences, text_lang)
    sentences = _drop_repeated_lang_blocks(sentences, text_lang)
    normalized = [normalize_single(s, ctx, emotions, user_text) for s in sentences]
    normalized = [s for s in normalized if real_text(s.get("zh")) or real_text(s.get("display"))]
    return _merge_short_sentences(split_multi_clause_sentences(normalized))


class SentenceStreamParser:
    """从增量文本中解析完整句子对象，支持两种模型输出形态：

    - 标准包装：{"sentences": [{...}, {...}]}，逐个产出数组内对象；
    - 裸块：模型把每句话输出成独立 JSON 对象（无 sentences 包装），
      逐块产出（旧版不认这种形态，只能等流结束兜底，且旧兜底只取
      第一块，导致"至少两个JSON块"只合成一条语音）。
    """

    _SENT_KEY = re.compile(r'"sentences"\s*:\s*\[')

    def __init__(self):
        self.buffer = ""
        self.scan_pos = 0
        self.in_array = False
        self.depth = 0
        self.in_string = False
        self.escape = False
        self.obj_start = None
        self.array_closed = False
        self.yielded = 0


    def feed(self, chunk: str) -> List[Dict]:
        if not chunk:
            return []
        self.buffer += chunk
        return self._scan()

    def _emit(self, obj) -> List[Dict]:
        """解析出的顶层对象 → 待产出的句子对象列表（避免重复产出）。"""
        if isinstance(obj, dict) and isinstance(obj.get("sentences"), list):
            # 整个包装对象被当作一块解析出来（如 "sentences": [ 之前有闲聊
            # 文本导致数组模式误触发，或流结束时才凑齐包装）：产出内部块。
            if self.yielded == 0:
                inner = [s for s in obj["sentences"] if isinstance(s, dict)]
                self.yielded += len(inner)
                return inner
            return []  # 内部块早已逐个产出
        if _is_sentence_like(obj):
            self.yielded += 1
            return [obj]
        return []  # 与句子无关的 JSON 对象（工具回显等），跳过

    def _scan(self) -> List[Dict]:
        buf = self.buffer
        if self.in_array:
            return self._scan_array(buf)
        # 数组模式未确认：先看 "sentences": [ 是否出现（回看24字符防关键字被分块截断）
        m = self._SENT_KEY.search(buf, max(0, self.scan_pos - 24))
        if m and (self.obj_start is None or m.start() >= self.obj_start):
            # 切入数组模式（丢弃包装对象自身的扫描状态）
            self.in_array = True
            self.scan_pos = m.end()
            self.depth = 0
            self.in_string = False
            self.escape = False
            self.obj_start = None
            return self._scan_array(buf)
        # 顶层裸块扫描（同时兼容顶层数组）
        out = []
        i = self.scan_pos
        n = len(buf)
        while i < n:
            ch = buf[i]
            if self.in_string:
                if self.escape:
                    self.escape = False
                elif ch == '\\':
                    self.escape = True
                elif ch == '"':
                    self.in_string = False
            elif ch == '"':
                self.in_string = True
            elif ch in '{[':
                if self.depth == 0:
                    self.obj_start = i
                self.depth += 1
            elif ch in '}]':
                self.depth = max(0, self.depth - 1)
                if self.depth == 0 and self.obj_start is not None:
                    obj = _loads_lenient(buf[self.obj_start:i + 1])
                    if obj is not None:
                        if isinstance(obj, list):
                            for s in obj:
                                out.extend(self._emit(s))
                        else:
                            out.extend(self._emit(obj))
                    self.obj_start = None
            i += 1
        self.scan_pos = i
        return out

    def _scan_array(self, buf: str) -> List[Dict]:
        out = []
        i = self.scan_pos
        n = len(buf)
        while i < n:
            ch = buf[i]
            if self.in_string:
                if self.escape:
                    self.escape = False
                elif ch == '\\':
                    self.escape = True
                elif ch == '"':
                    self.in_string = False
            elif ch == '"':
                self.in_string = True
            elif ch == '{':
                if self.depth == 0:
                    self.obj_start = i
                self.depth += 1
            elif ch == '}':
                self.depth = max(0, self.depth - 1)
                if self.depth == 0 and self.obj_start is not None:
                    obj = _loads_lenient(buf[self.obj_start:i + 1])
                    if obj is not None:
                        out.extend(self._emit(obj))
                    self.obj_start = None
            elif ch == ']' and self.depth == 0:
                self.array_closed = True
                i += 1
                break
            i += 1
        self.scan_pos = i
        return out

    def finish(self, ctx: RoleContext, user_text: str, emotions: dict) -> List[Dict]:
        """流结束后兜底：补齐尚未产出的句子。

        - 一句都没产出：整段内容走 normalize_sentences（含宽容修复/防线）；
        - 已产出过句子：尝试从末尾未闭合的截断对象中抢救最后一句
          （模型输出被掐断时，句子内容往往已完整，只是缺收尾括号）。
        """
        if self.yielded == 0:
            return normalize_sentences(self.buffer, ctx, emotions, user_text)
        if self.obj_start is None:
            return []
        obj = _loads_lenient(self.buffer[self.obj_start:])
        cand = []
        if isinstance(obj, dict):
            if isinstance(obj.get("sentences"), list):
                cand = [s for s in obj["sentences"] if isinstance(s, dict)]
            elif _is_sentence_like(obj):
                cand = [obj]
        # 截断对象可能拿到半截空台词，过滤掉没有实际内容的
        cand = [s for s in cand if str(s.get("zh", "")).strip()]
        if not cand:
            return []
        return split_multi_clause_sentences(
            [normalize_single(s, ctx, emotions, user_text) for s in cand])


# ---------------------------------------------------------------------------
# 工具调用消息规整（修 400 Bad Request）
# ---------------------------------------------------------------------------
# Ollama 会把 function.arguments 当成 JSON 对象解析：
#   - 空字符串 / 非法 JSON  → 400 "Value looks like object, but can't find closing '}'"
#   - tool_calls 传成字符串 → 400 "cannot unmarshal string into ... []api.ToolCall"
# 模型的 arguments 可能是 dict，也可能被截断成半截 JSON；历史消息里也可能残留字符串。
# 发送前统一规整成后端要求的形态，避免整条回复因为一个工具参数而彻底失败。

def _looks_like_url_arg(text: str) -> bool:
    return bool(re.match(r"^(?:https?://|www\.)\S+$", text, re.I))


def _extract_first_url(text: str) -> str:
    match = re.search(r"https?://[^\s<>\"'）)】]+", str(text or ""), re.IGNORECASE)
    return match.group(0).rstrip(".,!?。，！？") if match else ""


def _parse_arguments(arguments):
    """解析工具参数：返回 (参数字典, 是否解析失败)。

    空参数是合法的（无参工具），不能当成失败；只有"有内容但解析不出对象"
    才算失败，用来提示模型重试。裸文本只在明显是 URL 时才兜底成参数。
    第二个返回值 True 表示失败。
    """
    if isinstance(arguments, dict):
        return arguments, False
    if isinstance(arguments, (list, tuple)):
        return {"value": list(arguments)}, False
    if arguments is None:
        return {}, False
    text = str(arguments).strip()
    if not text:
        return {}, False
    stripped = text.lstrip()
    if stripped[:1] not in ('{', '[', '"'):
        # 裸文本：只有明显是 URL 时才兜底使用，否则视为参数无效
        return ({"url": text}, False) if _looks_like_url_arg(text) else ({}, True)
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = _loads_lenient(text)
    if isinstance(parsed, dict):
        return parsed, False
    if parsed is None:
        # 半截/非法 JSON：整段文本本身就是答案的载体时直接取用（例如 URL），
        # 其余情况视为参数无效，让上层提示模型重试而不是瞎猜参数。
        if _looks_like_url_arg(text):
            return {"url": text}, False
        return {}, True
    return {"value": parsed}, False


def _coerce_arguments_object(arguments):
    """把 arguments 规整成 dict；无法解析时返回空 dict（绝不发空串给后端）。"""
    return _parse_arguments(arguments)[0]


def normalize_message_tool_calls(message: dict) -> dict:
    """把单条消息的 tool_calls / tool_call_id 规整成后端可接受的形态。"""
    if not isinstance(message, dict):
        return message
    calls = message.get("tool_calls")
    if calls is None:
        return message
    if isinstance(calls, str):
        parsed = _loads_lenient(calls)
        calls = parsed if isinstance(parsed, list) else []
    elif isinstance(calls, dict):
        calls = [calls]
    if not isinstance(calls, list):
        calls = []
    normalized = []
    for idx, call in enumerate(calls):
        if not isinstance(call, dict):
            continue
        fn = call.get("function")
        if not isinstance(fn, dict):
            fn = {"name": str(call.get("name", "") or ""),
                  "arguments": call.get("arguments", {})}
        name = str(fn.get("name", "") or call.get("name", "") or "").strip()
        if not name:
            continue
        item = {
            "id": str(call.get("id") or f"call_{idx}"),
            "type": call.get("type") or "function",
            "function": {"name": name,
                         "arguments": _coerce_arguments_object(fn.get("arguments"))},
        }
        normalized.append(item)
    if normalized:
        message["tool_calls"] = normalized
    else:
        message.pop("tool_calls", None)
    return message


# ---------------------------------------------------------------------------
# 工具需求判定：只在消息真的需要客观信息时才走工具流程
# ---------------------------------------------------------------------------
# 事故背景：tools_trigger_mode=llm 时每条消息都会进工具流程，模型手边有工具就
# 什么都去调一遍——连"你好呀""今天好累"这种纯闲聊也要搜一下，回复节奏被拖垮。
# 这里在进入工具流程前先做一次意图判定：只有出现客观信息需求（时间/计算/天气/
# 搜索/链接等）才放行；纯寒暄、纯情绪、纯角色扮演一律跳过工具。
_SEARCH_INTENT_PATTERN = (
    r"搜索|搜一下|搜下|搜搜|帮我搜|帮忙搜|帮我查|帮我找|查一下|查一查|查查|查找|"
    r"找一下|找找|去找|去搜|快搜|就搜|发我|发给我|给我发|给我来|给我找|给我看|"
    r"来几张|来几个|发几张|发几个|"
    r"(?:搜|查|找)(?:[0-9一二两三四五六七八九十百千]*)(?:点|些|个|张|条|一下|下)?"
    r"(?!(?:了|的|到|过|不|没|出来|起来|完|遍))[\u4e00-\u9fffA-Za-z0-9]{2,20}|"
    r"(?:发|来|搞|整|看|拿)(?:[0-9一二两三四五六七八九十百千]*)"
    r"(?:点|些|个|张|条|几个|几张|几条|一些)(?!(?:了|的|到|过))"
    r"[\u4e00-\u9fffA-Za-z0-9]{1,20}"
)

_TOOL_NEED_RE = re.compile(
    r"几点|几号|几时|星期几|周几|礼拜几|日期|今天是|现在时间|当前时间|"
    r"算一下|计算|等于多少|多少等于|换算|百分之|打折|涨了|降了|"
    r"天气|气温|温度|下雨|降雨|带伞|雨伞|下雪|降雪|台风|冷不冷|热不热|多少度|几度|"
    + _SEARCH_INTENT_PATTERN +
    r"|谷歌|百度|bing|"
    r"是谁|是什么|什么是|什么叫|听说过|了解吗|认识吗|知道吗|怎么回事|哪里人|"
    r"最新|新闻|时事|进展|近况|价格|多少钱|多少钱|攻略|教程|推荐|评价|评测|"
    r"版本|更新|发售|上线|下载|官网|网址|链接|地址|在哪|怎么走|"
    r"odds|weather|forecast|temperature|search|who is|what is", re.I)
_TOOL_NEED_PREFIX_RE = re.compile(r"^(?:你|妳|您|主人|人家|本座|咱|俺)?(?:能不能|可以|能|可以|会)")
# 纯情绪/寒暄消息（短、且没有任何客观信息需求）：绝不进工具流程
_PURE_CHATTER_RE = re.compile(
    r"^(?:嗯+|哦+|好+|行|是|对|在吗|在么|在不在|早安|早上好|午安|晚安|晚上好|"
    r"你好|您好|哈喽|hi|hello|嗨|抱抱|摸摸|亲亲|贴贴|么么|爱你|喜欢你|想你|"
    r"哈哈+|嘿嘿+|嘻嘻+|呜呜+|嘤嘤+|嘤|呜|喵+|汪+|草|靠|啧|唉|哎|"
    r"ok|OK|okay|yes|no|thanks|thank you|thx)[!！。.~～、，,\s]*$")
_PURE_EMOTION_RE = re.compile(
    r"^(?:我)?(?:好|太|超|非常|真的)?(?:累|困|饿|开心|高兴|难过|伤心|无聊|"
    r"烦|烦死|郁闷|emo|爽|幸福|寂寞|孤独|疼|痛|冷|热|生气|气死)"
    r"(?:了|啦|啊|呀|哦|呢|死|爆|的|得很)?[!！。.~～\s]*$", re.I)


def text_needs_tools(user_text: str, tool_names=None) -> bool:
    """消息是否**真的需要**调用工具（客观信息需求）。"""
    text = strip_quote_note(str(user_text or "")).strip()
    if not text:
        return False
    if "://" in text or "www." in text.lower():
        return True
    if _TOOL_NEED_RE.search(text):
        return True
    if _TOOL_NEED_PREFIX_RE.search(text) and "?" in text + "？":
        return True
    names = {str(n).lower() for n in (tool_names or []) if n}
    if "get_current_time" in names and re.search(r"\d{1,2}\s*[点时:：]", text):
        return True
    return False


def tool_flow_can_skip(user_text: str, tool_names=None) -> bool:
    """纯寒暄 / 纯情绪、且没有任何客观信息需求时可以安全跳过工具流程。"""
    text = strip_quote_note(str(user_text or "")).strip()
    if not text:
        return False
    if text_needs_tools(text, tool_names):
        return False
    return bool(_PURE_CHATTER_RE.match(text) or _PURE_EMOTION_RE.match(text))


def last_user_text(messages) -> str:
    """取【最后一条真实用户消息】（跳过系统注入的【…】提示行）。"""
    for message in reversed(list(messages or [])):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = str(message.get("content", ""))
        if content.startswith("【"):
            continue
        return content
    return ""


def normalize_messages_for_backend(messages: list, backend: str) -> list:
    """按后端要求规整整串消息（主要在发送前调用）。"""
    out = []
    has_tool_result = False
    for raw in messages or []:
        if not isinstance(raw, dict):
            continue
        msg = dict(raw)
        if msg.get("tool_calls") is not None:
            msg = normalize_message_tool_calls(msg)
            if backend != "ollama":
                for call in msg.get("tool_calls") or []:
                    args = call.get("function", {}).get("arguments")
                    if not isinstance(args, str):
                        call["function"]["arguments"] = json.dumps(args or {},
                                                                   ensure_ascii=False)
        if msg.get("role") == "tool":
            has_tool_result = True
            if backend == "ollama":
                # Ollama 不接受 tool_call_id 字段
                msg.pop("tool_call_id", None)
            elif not msg.get("tool_call_id"):
                msg["tool_call_id"] = msg.get("tool_name") or "tool"
        out.append(msg)
    if has_tool_result:
        for idx, msg in enumerate(out):
            if msg is None or msg.get("role") != "tool":
                continue
            prev = out[idx - 1] if idx > 0 else None
            if prev is None or prev.get("role") not in ("assistant", "tool"):
                print("检测到孤立的 tool 结果消息，已丢弃以免触发 400。")
                out[idx] = None
        out = [m for m in out if m is not None]
    return out


# ---------------------------------------------------------------------------
# 工具调用（Function Calling）循环
# ---------------------------------------------------------------------------

async def _prefetch_message_urls(ctx: RoleContext, work: list, tool_registry,
                                 user_id: str = "") -> list:
    """用户消息里带链接时，先替模型把网页抓回来。

    事故背景：用户发一条含链接的消息，门控（_tool_requested）已经放行走工具流程、
    schema 也把 web_fetch 给了模型，但模型经常只是顺着链接聊两句、根本不发起调用，
    于是"用户发了链接却没有搜索/抓取"。工具是否被调用取决于模型心情，不能作为
    正确性依赖，所以在第一次请求之前先确定性地抓一次：

      - 只抓用户消息里的 http(s) 链接，最多 web_fetch_precheck_max 个；
      - 自动抓取也算一次调用，占用该工具的单回复次数上限；
      - 结果按 assistant(tool_calls) + tool 的成对形态注入，保证后端消息合法；
      - 注入一个 user 提示，让模型知道这轮已经抓过、不用重复调用。
    """
    if not bool(ctx.get("web_fetch_precheck", True)):
        return work
    registry_tools = getattr(tool_registry, "tools", None)
    if not isinstance(registry_tools, list):
        return work
    tool = next((t for t in registry_tools
                 if str(t.get("builtin", "")).lower() == "web_fetch" and t.get("enabled")), None)
    if tool is None:
        return work
    text = ""
    for message in reversed(work):
        if message.get("role") == "user" \
                and not str(message.get("content", "")).startswith("【"):
            text = str(message.get("content", ""))
            break
    urls = []
    for raw in re.findall(r"https?://[^\s<>\"'）)】\]]+", text, re.I):
        url = raw.rstrip(".,;:!?。，、；：！？")
        if url and url not in urls:
            urls.append(url)
    if not urls:
        return work
    limit = max(1, int(ctx.get("web_fetch_precheck_max", 2)))
    allowed, reason = tool_registry.check_permission(tool, user_id)
    if not allowed:
        print(f"含链接消息预抓取跳过: {reason}")
        return work

    fetched, failed = [], []
    for index, url in enumerate(urls[:limit]):
        ok, output = await tool_registry.execute("web_fetch", {"url": url}, user_id)
        call_id = f"prefetch_{index}"
        if ok:
            fetched.append(url)
            print(f"[工具预抓取] web_fetch ok=True → {str(output)[:80]}")
        else:
            failed.append((url, str(output)))
            print(f"[工具预抓取] web_fetch ok=False → {url} :: {str(output)[:80]}")
        work.append({"role": "assistant", "content": "",
                     "tool_calls": [{"id": call_id, "type": "function",
                                     "function": {"name": "web_fetch",
                                                  "arguments": {"url": url}}}]})
        work.append({"role": "tool", "content": str(output)})

    if fetched:
        note = ("用户消息里带了链接，其中 " + "、".join(fetched)
                + " 的网页内容已经通过 web_fetch 抓取并附在上面的工具结果里，"
                  "请直接依据这些内容回答，不要再重复抓取同一个链接。")
    else:
        note = ("用户消息里带了链接，但自动抓取失败：" + "；".join(f"{u}（{r}）" for u, r in failed)
                + "。请如实说明抓取失败，不要凭空编造网页内容。")
    work.append({"role": "user", "content": note})
    return work


# 模型"坦白不知道"的标志性说法（含日文——角色的台词可能是日语）
_UNCERTAIN_MARKERS = (
    "不知道", "没听说过", "没听过", "没有听说过", "不了解", "不太清楚", "不清楚",
    "无从得知", "无法确定", "没有相关资料", "没有资料", "查无", "不认识", "没印象",
    "知らない", "知りません", "聞いたことがない", "聞いたことない",
    "わからない", "わかりません", "存じません", "不明です",
)


def _answer_admits_unknown(text: str) -> bool:
    """判断回答是否在坦白"不知道/没听说过"（中/日文标记）。"""
    t = str(text or "")
    return any(m in t for m in _UNCERTAIN_MARKERS)


def _is_entity_question(text: str) -> bool:
    """判断用户消息是否在问一个具体的人/事/物（值得搜索核实）。"""
    t = str(text or "").lower()
    return any(k in t for k in ("是谁", "是什么", "什么是", "什么叫", "听说过",
                                "知道", "了解", "认识", "怎么回事"))


def _search_query_from_question(text: str) -> str:
    """从"你知道载物是谁吗"这类问句里提取搜索词（"载物"）。"""
    q = strip_quote_note(text).strip()
    # 循环剥离堆叠的问法前缀（"请问什么是…"要剥两层）
    while True:
        stripped = re.sub(r"^(请问一下|请问|你知道|你知道一下|你晓得|帮我看看|帮我查查|帮我查一下|帮我查|帮我|麻烦|什么是|什么叫)\s*",
                          "", q)
        if stripped == q:
            break
        q = stripped
    # 先剥句尾疑问语气词，再剥"是谁/是什么"等问法后缀，最后再剥一遍残留语气词
    q = re.sub(r"(吗|呢|吧|？|\?)+$", "", q)
    q = re.sub(r"(是谁|是什么人|是什么|是怎么回事|怎么样|如何|的资料|的介绍|的信息|在哪里|是哪里)+$",
               "", q)
    q = re.sub(r"(吗|呢|吧|？|\?)+$", "", q)
    return q.strip(" 　，,。.、:：;；\"'“”！!～~")


_SEARCH_INTENT_RE = re.compile(_SEARCH_INTENT_PATTERN)

_SUBJECTIVE_SEARCH_RE = re.compile(
    r"(?:帅|美|好看|可爱|漂亮|丑|萌|年轻|温柔)(?:不(?:帅|美|好看|可爱|漂亮|丑|萌)"
    r"|[^，。！!？?]{0,3}[吗么嘛])")

_PRIVATE_STATE = (r"穿|戴|脱|吃|喝|睡|起床|心情|感受|感觉|在做什么|在干嘛|干什么|"
                  r"做什么|想什么|在想|现在在|今天在|刚才在")

_NAME_STATE = (r"穿|戴|脱|睡|起床|心情|感受|在做什么|在干嘛|干什么|做什么|想什么|"
               r"在想|现在在|今天在|刚才在")

_ROLEPLAY_SELF_RE = re.compile(
    r"(?:你|妳|您|自己|本座|人家|咱|俺)[^，。！!？?]{0,6}(?:" + _PRIVATE_STATE + r")"
    r"|(?:穿|戴|脱)[^，。！!？?]{0,8}(?:什么|啥|哪个|哪种)")

_ROLEPLAY_LORE_RE = re.compile(
    r"是谁|是什么|设定|背景|剧情|结局|攻略|立绘|原画|声优|配音|评测|价格|下载|补丁|"
    r"mod|steam|手办|同人|登场|角色介绍|人物介绍|百科|wiki|简介|资料|档案|出处|"
    r"生日|年龄|身高|血型|图片|照片|头像|壁纸|表情包|表情|歌曲|台词|口癖|"
    r"cos|cosplay|周边|专辑|广播剧|动画|漫画|小说|游戏|发售|上线|联动", re.I)


def _is_roleplay_search(query: str, ctx) -> bool:
    """搜索词是否在打听"角色本人的私人状态"（网上不存在、只能以角色身份作答）。

    只拦真正问不出来的东西：把角色当成真人打听他此刻的穿着、心情、正在做什么。
    角色名配考据类词（设定、立绘、cos、图片、声优…）属于正常资料检索，必须放行，
    否则用户想找角色的图、设定、周边永远搜不到。
    """
    q = str(query or "")
    if _ROLEPLAY_SELF_RE.search(q):
        return True
    names = [str(ctx.get(k, "") or "") for k in ("character_name", "character_key")]
    names = [n for n in names if n]
    for name in names:
        if name not in q:
            continue
        if _ROLEPLAY_LORE_RE.search(q):
            continue
        if re.search(re.escape(name) + r"[^，。！!？?]{0,4}(?:" + _NAME_STATE + r")", q):
            return True
        if re.search(r"(?:你|妳|您|本座|自己)[^，。！!？?]{0,4}" + re.escape(name), q):
            return True
    return False


# 意图词命中点前方出现这些否定 / 质疑词时，该命中不算搜索请求：
# "为什么要去搜呢""不用搜""谁让你搜的"是在质问搜索行为，不是要搜索
_SEARCH_NEGATION_RE = re.compile(
    r"(?:为什么|为啥|怎么|咋|谁让你|谁叫你|何必|干嘛|不用|不必|不要|别|不准|不许|少|又)"
    r"[^，。！!？?；;\n]{0,4}$")


def _is_search_request(text: str) -> bool:
    """用户消息是否明确表达了"搜索"请求（确定性预取的门槛）。

    逐个检查意图词命中位置：命中点紧前方出现否定 / 质疑词（为什么要去搜呢、
    不用搜、谁让你搜的）的不算请求；所有命中点都被否定时返回 False，
    否则用户抱怨"为什么搜"本身又会触发一次搜索，形成死循环。
    """
    t = str(text or "")
    if not _SEARCH_INTENT_RE.search(t):
        return False
    for m in _SEARCH_INTENT_RE.finditer(t):
        prefix = t[max(0, m.start() - 12):m.start()]
        if _SEARCH_NEGATION_RE.search(prefix):
            continue
        return True
    return False


# "我不看X，我要看Y"式表达里的否定分句（限定条件，不是搜索目标）
_NEGATION_CLAUSE_RE = re.compile(
    r"^(?:我|人家|咱|俺)?(?:不想|不要|不看|不搜|不查|不找|不用|不想看|"
    r"别看|别搜|别查|别找|别用|没看|没搜)")
_DESIRE_PREFIX_RE = re.compile(r"^(?:我|人家|咱|俺)(?:想要?|要|想|要看|想看|打算|准备)?看?")


def _search_target_from_desire(q: str) -> str:
    """
    否定分句是限定条件，整句当关键词会搜出一堆无关结果；带主语+意愿动词的
    分句剥掉前缀留宾语。纯关键词（"看板娘"、"不倒翁"）不受影响。
    """
    s = str(q or "").strip()
    if not s:
        return s
    if not re.search(r"[，,。！!？?、\n]", s):
        # 单分句：只剥"我(要/想)看"这类带主语的意愿前缀，避免误伤"看板娘"这类词
        stripped = _DESIRE_PREFIX_RE.sub("", s)
        return stripped if len(stripped) >= 2 else s
    clauses = [c.strip() for c in re.split(r"[，,。！!？?、\n]+", s) if c.strip()]
    keep = [c for c in clauses if not _NEGATION_CLAUSE_RE.match(c)]
    if len(keep) == len(clauses):  # 没有否定分句，保持原样
        return s
    if not keep:  # 全是否定分句，提取失败，退回原句
        return s
    keep = [_DESIRE_PREFIX_RE.sub("", c) if _DESIRE_PREFIX_RE.match(c) else c for c in keep]
    keep = [c for c in keep if len(c) >= 2] or keep
    return " ".join(keep)


# 引用消息前缀（main.py 拼进 user_text），参与意图判定/关键词提取前要先剥掉，
# 否则被引用消息里的"搜索"字样会替用户做决定
_QUOTE_NOTE_RE = re.compile(r"^（回复 .*? 的消息：.*?）\n?")


def strip_quote_note(text: str) -> str:
    return _QUOTE_NOTE_RE.sub("", str(text or ""), count=1)


def _search_query_from_command(text: str) -> str:
    """从明确的搜索指令里提取关键词（"快去搜 我要看"→"我要看"）。"""
    q = strip_quote_note(text).strip()
    # 循环剥离堆叠的指令前缀（"快帮我搜一下…"要剥多层）
    while True:
        before = q
        q = re.sub(r"^(?:那你就|那你去|那么|那就|那|所以|对了|好吧|好|嗯|哦|行|就)\s*", "", q)
        q = re.sub(r"^(请|麻烦|麻烦你|请你|辛苦|帮我|帮忙给我|给我|快去|快|去)\s*", "", q)
        q = re.sub(r"^(?:你|妳|您)(?=(?:去|帮我|帮忙)?(?:搜索|搜一下|搜点|搜|查一下|查一查|查查|"
                   r"查找|找一下|找找|发点|发我|来点|来几张))", "", q)
        q = re.sub(r"^(搜索|搜一下|搜下|搜搜|搜点|搜些|搜个|搜几个|搜几张|搜几条|搜|"
                   r"查一下|查一查|查查|查找|查|找一下|找找|找|"
                   r"发点|发些|发个|发几个|发几张|发我|发给我|来点|来些|来几个|来几张|"
                   r"搞点|整点|看点|看看有没有|看看|拿点)[：:，,]?\s*", "", q)
        if q == before:
            break
        q = re.sub(r"^(?:了)?(?:点|些|个|几个|几张|几条|一下|下)(?=[\u4e00-\u9fffA-Za-z0-9]{2,})",
                   "", q)
    q = re.sub(r"(吧|呢|啊|呀|[！!。？?])+$", "", q)
    q = _search_target_from_desire(q)
    return q.strip(" 　，,。.、:：;；\"'“”！!～~")


_ROLE_PRONOUN_RE = re.compile(r"你|妳|您")
_ROLE_ATTR_RE = re.compile(
    r"是谁|是什么|什么人|叫什么|名字|称呼|设定|资料|介绍|档案|简介|人物|角色|"
    r"生日|年龄|多大|几岁|身高|体重|血型|性格|爱好|喜欢|讨厌|出处|来源|作品|"
    r"游戏|动画|漫画|小说|声优|配音|立绘|原画|形象|图片|照片|头像|壁纸|表情|"
    r"歌曲|角色歌|台词|口癖|cos|cosplay|同人|手办|周边|百科|wiki", re.I)


def resolve_role_pronouns(query: str, ctx) -> str:
    """把搜索词里指向角色本人的第二人称换成角色名。

    用户说“搜索你是谁”“搜点你的图片”时，拿“你是谁”去搜索引擎只会搜出一堆
    通用问答，换成角色名（“丛雨 是谁”“丛雨 图片”）才能搜到真正的资料。
    """
    q = str(query or "").strip()
    name = str(ctx.get("character_name", "") or "").strip() \
        or str(ctx.get("character_key", "") or "").strip()
    if not q or not name or name in q or not _ROLE_PRONOUN_RE.search(q):
        return q
    rest = _ROLE_PRONOUN_RE.sub("", q).strip(" 　的了")
    if not rest:
        return name
    if not _ROLE_ATTR_RE.search(rest):
        return q
    return _ROLE_PRONOUN_RE.sub(name, q)


def _resolve_query_arguments(arguments: dict, ctx) -> dict:
    if not isinstance(arguments, dict):
        return arguments
    out = dict(arguments)
    for key, value in list(out.items()):
        if isinstance(value, str):
            resolved = resolve_role_pronouns(value, ctx)
            if resolved != value:
                out[key] = resolved
        elif isinstance(value, list):
            items = [resolve_role_pronouns(v, ctx) if isinstance(v, str) else v
                     for v in value]
            if items != value:
                out[key] = items
    return out


_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"'）)】\]]+")
_SEARCH_ENTRY_SPLIT_RE = re.compile(r"\n\n+")


def urls_in_text(text) -> list:
    """按出现顺序取出文本里的链接（去掉尾随标点、去重）。"""
    out = []
    for raw in _URL_IN_TEXT_RE.findall(str(text or "")):
        url = raw.rstrip(".,;:!?。，、；：！？)]}】")
        if url and url not in out:
            out.append(url)
    return out


def drop_seen_entries(output, seen_urls) -> str:
    """把搜索结果里已经给过用户的条目剔除，让模型只能引用新的结果。"""
    seen = {str(u).rstrip("/") for u in (seen_urls or []) if u}
    text = str(output or "")
    if not seen:
        return text
    blocks = _SEARCH_ENTRY_SPLIT_RE.split(text)
    kept, dropped, kept_with_url = [], 0, 0
    for block in blocks:
        urls = urls_in_text(block)
        if urls and any(u.rstrip("/") in seen for u in urls):
            dropped += 1
            continue
        if urls:
            kept_with_url += 1
        kept.append(block)
    if not dropped:
        return text
    if not kept_with_url:
        return (text + f"\n\n（上面 {dropped} 条结果的链接此前已经发给过用户，本次不会再重复给出；"
                      "请如实告诉用户这次没有搜到新的内容，并问一句他希望更具体地搜什么。）")
    return ("\n\n".join(kept)
            + f"\n\n（其中 {dropped} 条结果的链接此前已经发给过用户，本次不再重复给出，"
              "请从上面的新结果里挑选内容与链接作答。）")


_SESSION_SEARCH_STATE: dict = {}
_SESSION_SEARCH_MAX = 64
_SESSION_SENT_LINKS: dict = {}


def search_state_key(session_key: str, user_id: str = "") -> str:
    return str(session_key or user_id or "")


def _trim_store(store: dict, limit: int):
    while len(store) > limit:
        oldest = min(store, key=lambda k: store[k].get("ts", 0)
                     if isinstance(store[k], dict) else store[k])
        store.pop(oldest, None)


def record_search_state(key: str, query: str, output: str, sent: bool = False):
    """记下这次搜索用了什么词、拿到哪些链接，供用户追问/不满时二次检索。"""
    if not key:
        return
    urls = urls_in_text(output)
    prev = _SESSION_SEARCH_STATE.get(key) or {}
    history = list(prev.get("history") or [])
    if query:
        history = ([query] + [h for h in history if h != query])[:4]
    _SESSION_SEARCH_STATE[key] = {
        "query": query or prev.get("query", ""),
        "urls": list(dict.fromkeys(urls + list(prev.get("urls") or [])))[:40],
        "history": history,
        "ts": time.time(),
    }
    _trim_store(_SESSION_SEARCH_STATE, _SESSION_SEARCH_MAX)


def search_state(key: str) -> dict:
    if not key:
        return {}
    entry = _SESSION_SEARCH_STATE.get(key)
    return dict(entry) if isinstance(entry, dict) else {}


def record_sent_links(key: str, urls) -> None:
    """记下真正发给用户的链接（用于"下次换别的链接"与避免重复发送）。"""
    if not key:
        return
    items = [str(u).rstrip("/") for u in (urls or []) if u]
    if not items:
        return
    entry = _SESSION_SENT_LINKS.get(key) or {"urls": [], "ts": 0.0}
    entry["urls"] = list(dict.fromkeys(list(entry.get("urls") or []) + items))[-60:]
    entry["ts"] = time.time()
    _SESSION_SENT_LINKS[key] = entry
    _trim_store(_SESSION_SENT_LINKS, _SESSION_SEARCH_MAX)


def sent_links(key: str) -> list:
    if not key:
        return []
    entry = _SESSION_SENT_LINKS.get(key)
    return list(entry.get("urls") or []) if isinstance(entry, dict) else []


_SEARCH_DISSATISFIED_RE = re.compile(
    r"不是(?:这个|那个|这些|那些|它|他|她|我要的|我想找的|我想要的)|"
    r"不对|不准确|不相关|不满意|不想要(?:这个|这些|这个了)|"
    r"搜错|查错|找错|错了|没搜到|没查到|搜不到|查不到|搜不着|没找着|没找到|"
    r"重新搜|再搜(?:一次|一遍|一下)?|换个|换一个|换一批|重新找|再找(?:一次|一遍)?|"
    r"我要的不是|我说的是|你是不是搜错|这(?:些|个)?都?(?:不是|不对)|"
    r"给的不是|发错|不是我(?:要|说)的")


def _is_search_dissatisfied(text: str) -> bool:
    """用户是否在反馈上一次搜索结果不对/没达到预期。"""
    t = strip_quote_note(str(text or "")).strip()
    if not t or len(t) > 120:
        return False
    return bool(_SEARCH_DISSATISFIED_RE.search(t))


is_search_request = _is_search_request
is_search_dissatisfied = _is_search_dissatisfied


def _refine_query_from_feedback(prev_query: str, user_text: str) -> str:
    """把上一次的搜索词与用户这次的纠正内容合成新的搜索词。"""
    prev = str(prev_query or "").strip()
    q = str(user_text or "").strip()
    while True:
        before = q
        q = re.sub(r"^(?:不对|不是|错了|不不|那个|这个|不对不对|重新搜|再搜(?:一次|一遍|一下)?|"
                   r"换个|换一个|换一批|重新找|再找(?:一次|一遍)?)[，,。、\s]*", "", q)
        q = re.sub(r"^(?:我(?:想要|要|想)的?是|我指的是|指的是|我说的就是|其实是|应该(?:是)?|"
                   r"的是|是|的|而是)+[，,。、\s]*", "", q)
        q = re.sub(r"^(?:了)?(?:一下|下|点|些|个|几个|几张|几条)(?=[\u4e00-\u9fffA-Za-z0-9]{2,})",
                   "", q)
        if q == before:
            break
    q = _search_query_from_command(q) or q
    q = q.strip(" 　，,。.、:：;；\"'“”！!～~")
    if not prev:
        return q
    if not q or q == prev:
        return prev
    if prev in q or q in prev:
        return q if len(q) >= len(prev) else prev
    return f"{prev} {q}".strip()


# 预取搜索结果缓存：回复被相似度检查打回后会整条重走 chat_with_tools，
# 同一条用户消息会再次触发同样的预取搜索（一次 30s+）。缓存近期结果直接复用，
# 不再重复检索；需要全新结果时由模型主动发起 web_search（不走这条缓存）。
_PREFETCH_SEARCH_CACHE = {}
_PREFETCH_CACHE_TTL = 600.0
_PREFETCH_CACHE_MAX = 32


def _prefetch_cache_get(query: str):
    entry = _PREFETCH_SEARCH_CACHE.get(query)
    if entry is None:
        return None
    ts, ok, output, final_query = entry
    if time.time() - ts > _PREFETCH_CACHE_TTL:
        _PREFETCH_SEARCH_CACHE.pop(query, None)
        return None
    return ok, output, final_query


def _prefetch_cache_put(query: str, ok: bool, output: str, final_query: str = ""):
    if len(_PREFETCH_SEARCH_CACHE) >= _PREFETCH_CACHE_MAX:
        oldest = min(_PREFETCH_SEARCH_CACHE, key=lambda k: _PREFETCH_SEARCH_CACHE[k][0])
        _PREFETCH_SEARCH_CACHE.pop(oldest, None)
    _PREFETCH_SEARCH_CACHE[query] = (time.time(), ok, output, final_query or query)


def _query_follows_user_text(query: str, user_text: str) -> bool:
    """提取出来的搜索词是否和用户消息有共同内容（防止模型答非所问）。

    事故背景：模型偶尔不按指令回答，而是把自己的上一句台词/一句解释当成关键词
    回给我们；这类词与原句毫无重叠，一旦采信，搜索词就会变成一句莫名其妙的话。
    判据：搜索词里至少有一个 ≥2 字的片段出现在用户原句里（或反过来）。
    """
    q = re.sub(r"\s+", "", str(query or "")).lower()
    t = re.sub(r"\s+", "", str(user_text or "")).lower()
    if not q or not t:
        return False
    if q in t or t in q:
        return True
    for size in (4, 3, 2):
        for i in range(len(q) - size + 1):
            if q[i:i + size] in t:
                return True
    return False


_DIALOGUE_HEAD_RE = re.compile(
    r"^(?:用户|主人|我|你|妳|您|咱|俺|本座|人家|抱歉|对不起|无法|不能|请|谢谢|好的|嗯|哦|"
    r"那个|这样|不是|没有)")
_SENTENCE_PUNCT_RE = re.compile(r"[。！？!?；;]|，.*，")
_EXPLANATION_RE = re.compile(
    r"(?:无法|不能|没有|抱歉|作为|意思是|指的是|也就是|例如|建议|可以搜索|请告诉我|"
    r"用户想|你想|查询词是|关键词是)")
_DIALOGUE_MARK_RE = re.compile(
    r"[你我妳您]|主人|本座|人家|咱|俺|吾辈|[哦呀嘛啦咯哟喔呢哇哼]|嘻嘻|哈哈|么么")
_SENTENCE_HEAD_RE = re.compile(
    r"^(?:这|那|它|他|她|它们)?(?:就是|是|不是|有|没有|会|能|可以|要|想|应该|需要|正在|"
    r"已经|还没|别|不要|请)")


def _looks_like_search_term(query: str) -> bool:
    """搜索词形态判定：短、无句末标点、不像台词/解释/整句。"""
    t = str(query or "").strip()
    if not (2 <= len(t) <= 40):
        return False
    if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", t):
        return False
    if _SENTENCE_PUNCT_RE.search(t) or _EXPLANATION_RE.search(t):
        return False
    if _DIALOGUE_HEAD_RE.match(t) or _DIALOGUE_MARK_RE.search(t):
        return False
    if _SENTENCE_HEAD_RE.match(t):
        return False
    return True


async def _llm_extract_search_query(ctx: RoleContext, user_text: str, fallback: str,
                                    reference: str = "") -> str:
    """让模型自己从用户消息里提取/组织搜索关键词；失败或明显不可用时回退规则提取。

    规则剥离只能处理常见前缀，"我不看X我要看Y"这类语义取舍交给 LLM 更准。
    模型可以按自己的理解把词组织得更明确（换用同义说法、补上限定词），
    这类改写与原文没有共同字面片段，不能因此判为无效：只有在输出明显是台词、
    解释或问句（不像搜索词）时才丢弃。
    """
    messages = [
        {"role": "system", "content":
            "你是搜索关键词提取器。从用户消息中提取最适合交给搜索引擎的查询词："
            "剔除口语、请求语、称呼、语气词等与搜索目标无关的字符，保留核心搜索对象，"
            "必要时可以用更明确、更常见的说法替换口语说法（换词、补限定词都可以）。"
            "只输出关键词本身：不要解释、不要引号、不要句末标点、不要整句话。"},
        {"role": "user", "content": str(user_text or "")},
    ]
    try:
        inner_ctx = RoleContext(ctx.config, {**ctx.role, "llm_timeout": 45})
        result = await asyncio.wait_for(chat_once(inner_ctx, messages, None), timeout=50)
        q = str((result or {}).get("content", "") or "").strip()
        q = re.sub(r"^(搜索词|关键词|查询词|搜索)[：:]\s*", "", q)
        q = re.sub(r"^[\s　\"'“”‘'（(【\[]+", "", q)
        q = re.sub(r"[\s　\"'“”‘'）)】\]]+$", "", q)
        q = q.strip(" 　\"'“”‘'。.！!？?，,、")
        if not (2 <= len(q) <= 40):
            print(f"[搜索预取] LLM 提取结果长度不合适（{q!r}），回退规则提取")
            return fallback
        if _query_follows_user_text(q, reference or user_text):
            return q
        if _looks_like_search_term(q):
            print(f"[搜索预取] LLM 用自己的话组织了搜索词（与原文无共同字面片段）：{q!r}")
            return q
        print(f"[搜索预取] LLM 输出的内容不像搜索词（更像台词/解释）（{q!r}），回退规则提取")
    except Exception as e:
        print(f"[搜索预取] LLM 提取关键词失败，回退规则提取：{type(e).__name__}: {e}")
    return fallback


async def _prefetch_search(ctx: RoleContext, work: list, tool_registry, user_id: str = "",
                           call_counts: dict = None, session_key: str = "") -> list:
    """搜索意图确定性预取：用户明确要求搜索时，不依赖模型发起工具调用。

    两种触发方式：
      - 用户明确表达搜索请求（含"搜点…/发点…/来几张…"这类说法）；
      - 用户反馈上一次搜索结果不对/不是想要的：沿用上次的搜索词加上本次的
        纠正内容重新检索，并剔除此前已经发给过用户的链接，只给新的结果。
    结果按 assistant(tool_calls) + tool 成对形态注入并附说明，模型照着组织回答。
    """
    if not bool(ctx.get("search_prefetch_enabled", True)):
        return []
    registry_tools = getattr(tool_registry, "tools", None)
    if not isinstance(registry_tools, list):
        return []
    tool = next((t for t in registry_tools
                 if str(t.get("builtin", "")).lower() == "web_search" and t.get("enabled")), None)
    if tool is None:
        return []
    user_text = ""
    for message in reversed(work):
        if message.get("role") == "user" and not str(message.get("content", "")).startswith("【"):
            user_text = strip_quote_note(str(message.get("content", "")))
            break
    key = search_state_key(session_key, user_id)
    state = search_state(key)
    prev_query = str(state.get("query", "") or "")
    explicit = _is_search_request(user_text)
    retry_feedback = bool(prev_query) and _is_search_dissatisfied(user_text)
    if not explicit and not retry_feedback:
        return []
    allowed, reason = tool_registry.check_permission(tool, user_id)
    if not allowed:
        print(f"[搜索预取] 跳过: {reason}")
        return []
    seen_links = list(sent_links(key)) + list(state.get("urls") or [])
    query = ""
    reference = user_text
    if retry_feedback:
        rule_query = _refine_query_from_feedback(prev_query, user_text)[:60]
        if len(rule_query) < 2:
            return []
        print(f"[搜索预取] 用户反馈上次结果不理想，按新搜索词重新检索：{rule_query!r}"
              f"（上次：{prev_query!r}）")
        reference = f"{prev_query}（用户补充：{user_text}）"
        query = rule_query
        if bool(ctx.get("search_query_llm_extract", True)):
            query = await _llm_extract_search_query(ctx, reference, rule_query,
                                                   reference=reference)
            if not (2 <= len(query) <= 40):
                query = rule_query
            if query != rule_query:
                print(f"[搜索预取] LLM 提取搜索关键词：{query!r}（规则提取：{rule_query!r}）")
        if prev_query not in query:
            query = f"{prev_query} {query}".strip()[:60]
        query = resolve_role_pronouns(query, ctx)
    else:
        rule_query = _search_query_from_command(user_text)
        if not (2 <= len(rule_query) <= 40):
            return []
        print(f"[搜索预取] 用户明确要求搜索，直接检索：{rule_query!r}")
    cached = None if retry_feedback else _prefetch_cache_get(rule_query)
    if cached is not None:
        ok, output, query = cached
        print(f"[搜索预取] {query!r} 在 {_PREFETCH_CACHE_TTL:.0f}s 内已搜过，直接复用上次结果")
    else:
        if not query:
            query = rule_query
            if bool(ctx.get("search_query_llm_extract", True)):
                query = await _llm_extract_search_query(ctx, user_text, rule_query)
                if not (2 <= len(query) <= 40):
                    query = rule_query
                if query != rule_query:
                    print(f"[搜索预取] LLM 提取搜索关键词：{query!r}（规则提取：{rule_query!r}）")
        resolved = resolve_role_pronouns(query, ctx)
        if resolved != query:
            print(f"[搜索预取] 搜索词里的“你/妳/您”指当前角色，已替换为角色名：{resolved!r}")
            query = resolved
        if _SUBJECTIVE_SEARCH_RE.search(query) or _is_roleplay_search(query, ctx):
            print(f"[搜索预取] 拦截角色扮演/主观类搜索：{query!r}")
            ok = True
            output = ("这是角色扮演 / 主观互动类问题，答案由你以角色身份当场演绎，"
                      "网上查不到也不需要查；请直接以角色身份回应，不要引用任何搜索结果。")
            work.append({"role": "assistant", "content": "",
                         "tool_calls": [{"id": "prefetch_search", "type": "function",
                                         "function": {"name": "web_search",
                                                      "arguments": {"query": query}}}]})
            work.append({"role": "tool", "content": output})
            work.append({"role": "user", "content":
                         f"用户提到「{query}」，但这是角色扮演/主观互动，"
                         "请直接以角色身份回应，不要搜索、不要引用搜索结果。"})
            return [{"name": "web_search", "arguments": {"query": query},
                     "ok": True, "output": output, "blocked": True}]
        ok, output = await tool_registry.execute("web_search", {"query": query}, user_id,
                                                 call_counts=call_counts)
        if retry_feedback and ok:
            output = drop_seen_entries(output, seen_links)
        if not retry_feedback:
            _prefetch_cache_put(rule_query, ok, output, query)
    if ok:
        record_search_state(key, query, str(output))
    work.append({"role": "assistant", "content": "",
                 "tool_calls": [{"id": "prefetch_search", "type": "function",
                                 "function": {"name": "web_search", "arguments": {"query": query}}}]})
    work.append({"role": "tool", "content": str(output)})
    if ok and retry_feedback:
        work.append({"role": "user", "content":
                     f"用户反馈上一次搜索结果不对，系统已用新搜索词「{query}」重新检索，"
                     "结果就在上面的工具消息里。请依据这次的新内容回答，"
                     "并明确告诉用户这次换成了什么；已经有过的链接不要再发，"
                     "结果里带的新链接要原样写进回复。"})
    elif ok:
        work.append({"role": "user", "content":
                     f"用户明确要求搜索「{query}」，系统已替你完成搜索，结果就在上面的工具消息里，"
                     "请直接依据结果回答，不要再重复搜索同一内容。"})
    else:
        work.append({"role": "user", "content":
                     f"用户明确要求搜索「{query}」，但自动搜索失败了：{str(output)[:200]}。"
                     "请如实告诉用户本次没搜到，不要编造结果。"})
    return [{"name": "web_search", "arguments": {"query": query}, "ok": ok, "output": output}]


async def chat_with_tools(ctx: RoleContext, messages: list, tool_registry,
                          stats=None, user_id: str = "", session_key: str = "") -> Dict:
    """带工具调用的完整对话循环，返回最终 {content, tool_trace, ms}。"""
    tools_schema = tool_registry.get_schema()
    trace = []
    call_counts = {}
    total_ms = 0.0
    max_iter = max(1, int(ctx.get("tools_max_iterations", 3)))
    user_text = last_user_text(messages)
    tool_names = [str(t.get("function", {}).get("name", ""))
                  for t in (tools_schema or []) if t.get("function")]
    # 纯寒暄/纯情绪消息：不为了用工具而用工具，直接走普通回复。
    # 关键词/全部放行模式下用户已自行决定门槛，只有 llm 自主判断模式需要这层保护。
    mode = str(ctx.get("tools_trigger_mode", "keyword") or "keyword").strip().lower()
    allow_skip = bool(ctx.get("tools_skip_pure_chatter", True)) and mode in ("llm", "llm_auto", "auto")
    if allow_skip and tool_flow_can_skip(user_text, tool_names):
        print(f"[工具流程] 本条为纯寒暄/纯情绪消息，跳过工具调用直接以角色身份回答：{user_text[:30]!r}")
        result = await chat_once(ctx, list(messages))
        if stats:
            stats.record_llm(result["ms"])
        return {"content": result.get("content") or "", "tool_trace": [],
                "ms": result.get("ms", 0.0), "llm_calls": 1}
    work = await _prefetch_message_urls(ctx, list(messages), tool_registry, user_id)
    # 搜索意图确定性预取：用户明确要求搜索时不赌模型的工具调用能力，直接检索
    trace.extend(await _prefetch_search(ctx, work, tool_registry, user_id,
                                        call_counts=call_counts, session_key=session_key))
    # 工具已启用且这一轮走了工具流程：给模型一条硬性提示，避免它无视工具列表自行作答
    if tools_schema:
        tool_names_text = "、".join(
            str(t.get("function", {}).get("name", ""))
            for t in tools_schema if t.get("function", {}).get("name"))
        work.append({"role": "user", "content":
            f"【工具使用规则】本次对话已为你开启以下工具：{tool_names_text}。"
            "这些工具只服务于【客观信息需求】。"
            "【最高优先级】你正在进行角色扮演对话，不是在做资料检索："
            "调情、撩拨、性暗示、角色扮演、询问你的私人状态或当下情况"
            "（今天穿了什么、心情如何、在做什么、喜欢什么等）、"
            "评价用户、闲聊寒暄——这类互动的「答案」由你以角色身份当场演绎，"
            "网上根本搜不到，严禁为此调用任何工具（包括查时间），"
            "也严禁把角色名和你的私人状态组合成关键词去搜索。"
            "但用户想找与你有关的客观资料时（你的设定、立绘、图片、cos、声优、"
            "周边、出处作品等），这些是网上真实存在的内容，要正常搜索，不要推托。"
            "请严格按以下规则决定是否调用："
            "① 用户的消息明确需要客观信息时（时间、日期、计算、天气、搜索客观资料、"
            "打开网址等），必须调用对应工具获取真实结果，严禁凭记忆、凭常识、凭印象直接作答；"
            "② 尤其禁止用“我没有联网能力”“我不了解最新消息”“你可以自己搜一下”之类的话推托——"
            "你手边就有搜索工具，先搜再回答；"
            "③ 调用工具时参数必须严格取自【当前这一条用户消息】的内容，"
            "不要复用历史对话里的旧参数、旧搜索词、旧城市名；"
            "④ 只有当用户消息确实不涉及任何工具的适用场景时，才可以不调用工具直接作答；"
            "⑤ 拿到工具结果后，用角色自身的语气把结果融入回复，不要暴露“调用工具”“函数返回”等系统术语；"
            "⑥ 对话历史或【此前工具查询结果备查】里已经给出的信息（包括之前搜索过的内容），"
            "直接引用作答，严禁就同一事物再次调用搜索/抓取；"
            "只有本轮消息提出了新的信息需求时才发起新的工具调用；"
            "⑦ 对客观事实（作品、歌词、人物、新闻、版本、价格、地点、规则细节等）"
            "没有把握、或只能凭印象开口时，必须先调用搜索工具核实，"
            "严禁编造内容，也严禁用「大概是」「我记得是」「可能」这类猜测话术蒙混过关——"
            "宁可多搜一次，也不要猜（此条仅限客观事实；"
            "主观互动与角色扮演永远不适用本条）；"
            "⑧ 当前时间 / 日期整轮只查询一次，已经在工具结果里给过就不要再查；"
            "用户消息里没有数字和运算式时，不要调用计算工具——不要为了衬托台词"
            "假装计算或查时间；"
            "⑨ 用户发图片让你评价属于主观互动，"
            "图片内容已经提供给你了，直接以角色身份回应即可，"
            "严禁为此调用搜索工具去搜主观问题；"
            "⑩ 用户索要链接、网址、下载地址时，必须把工具结果里最相关的"
            "链接（URL）原样完整写进回复，一条不够就多写几条，"
            "只报名字不给链接等于没回答；找不到对应链接时如实说明。"
            "⑪ 【只在必要时调用】工具不是聊天的一部分：消息里没有客观信息需求时"
            "一个工具都不要调用，直接用角色身份说话。"
            "严禁“顺便查一下时间”“顺便搜一下”这类没有用户请求的调用；"
            "同一条消息里能用一个工具解决的，不要连开多个；"
            "⑫ 【地点不许猜】查询天气等与地点有关的信息时，只能使用"
            "【当前这一条用户消息里真的出现过的地点】；"
            "用户没说地点、你也不知道主人在哪时，绝对不许自己填一个城市"
            "（严禁默认上海/北京等任何城市），"
            "而是要直接用角色身份问一句主人在哪个城市。"
            "⑬ 【谈身份要查资料】讨论具体作者/作品/角色的设定、剧情、百科、"
            "版本等客观资料时，可以调用搜索；但只是以自己的角色身份闲聊、"
            "表达情绪、调情、撒娇时，永远不要调用工具。"})
    final_content = ""
    executed_calls = set()
    llm_calls = 0   # 本轮回复实际发起的 LLM 请求次数（工具轮 + 最终回答轮）
    for iteration in range(max_iter):
        result = await chat_once(ctx, work, tools=tools_schema)
        total_ms += result["ms"]
        llm_calls += 1
        if stats:
            stats.record_llm(result["ms"])
        calls = result.get("tool_calls") or []
        print(f"[工具流程] 第 {iteration + 1}/{max_iter} 轮："
              f"{'请求调用 ' + str(len(calls)) + ' 个工具' if calls else '直接给出回答'}"
              f"（耗时 {result['ms']:.0f}ms，正文 {len(str(result.get('content') or ''))} 字）")
        if not calls:
            final_content = result["content"]
            break
        norm_calls = []
        for call in calls:
            fn = call.get("function", {}) or {}
            name = str(fn.get("name", call.get("name", "")) or "").strip()
            if not name:
                continue
            parsed_args, args_failed = _parse_arguments(
                fn.get("arguments", call.get("arguments", {})))
            norm_calls.append({
                "id": str(call.get("id", "") or f"call_{len(norm_calls)}"),
                "name": name,
                "arguments": parsed_args,
                "arguments_failed": args_failed,
                "raw_arguments": fn.get("arguments", call.get("arguments", "")),
                "tool_call_id": call.get("id", ""),
            })
        if not norm_calls:
            final_content = result["content"]
            break
        work.append({"role": "assistant", "content": result["content"] or "",
                     "tool_calls": [{"id": c["id"], "type": "function",
                                     "function": {"name": c["name"],
                                                  "arguments": c["arguments"]}}
                                    for c in norm_calls]})
        for call in norm_calls:
            raw = call.get("raw_arguments")
            raw_text = "" if raw is None else (raw if isinstance(raw, str)
                                               else json.dumps(raw, ensure_ascii=False))
            signature = f"{call['name']}::{json.dumps(call['arguments'], sort_keys=True, ensure_ascii=False)}"
            if signature in executed_calls:
                ok, output = True, (f"工具 {call['name']} 本轮已用相同参数执行过，"
                                    f"结果见上方工具消息，请直接引用已有结果回答，不要重复调用。")
                print(f"工具 {call['name']} 相同参数重复调用已拦截")
            elif call.get("arguments_failed"):
                executed_calls.add(signature)
                ok, output = False, (f"工具参数不是合法的 JSON 对象，已跳过调用："
                                     f"{raw_text[:100]}")
                print(f"工具 {call['name']} 参数无法解析为 JSON 对象，已跳过：{raw_text[:120]!r}")
            else:
                executed_calls.add(signature)
                arguments = call["arguments"]
                if call["name"] == "web_fetch" and not arguments.get("url"):
                    source_text = ""
                    for message in reversed(work):
                        if message.get("role") == "user":
                            source_text = str(message.get("content", ""))
                            break
                    url = _extract_first_url(source_text)
                    if url:
                        arguments = {**arguments, "url": url}
                        call["arguments"] = arguments
                if call["name"] == "web_search":
                    call["arguments"] = _resolve_query_arguments(call["arguments"], ctx)
                    query_text = " ".join(str(v) for v in call["arguments"].values()
                                          if isinstance(v, str))
                    if _SUBJECTIVE_SEARCH_RE.search(query_text) \
                            or _is_roleplay_search(query_text, ctx):
                        executed_calls.add(signature)
                        ok, output = True, ("这是角色扮演 / 主观互动类问题，答案由你以角色身份"
                                            "当场演绎，网上查不到也不需要查；"
                                            "请直接以角色身份回应，不要再调用任何工具。")
                        print(f"[工具流程] 拦截角色扮演/主观类搜索：{query_text!r}")
                        trace.append({"name": call["name"], "arguments": call["arguments"],
                                      "ok": ok, "output": output})
                        if result["backend"] == "ollama":
                            work.append({"role": "tool", "content": str(output)})
                        else:
                            work.append({"role": "tool", "tool_call_id": call["tool_call_id"],
                                         "content": str(output)})
                        continue
                ok, output = await tool_registry.execute(call["name"], arguments, user_id,
                                                         call_counts=call_counts,
                                                         user_text=user_text)
                if call["name"] == "web_search" and ok:
                    record_search_state(search_state_key(session_key, user_id),
                                        " ".join(str(v) for v in arguments.values()
                                                 if isinstance(v, str)), str(output))
            trace.append({"name": call["name"], "arguments": call["arguments"],
                          "ok": ok, "output": output})
            if result["backend"] == "ollama":
                work.append({"role": "tool", "content": str(output)})
            else:
                work.append({"role": "tool",
                             "tool_call_id": call.get("tool_call_id") or call["id"],
                             "content": str(output)})
        # 工具已执行：强制模型引用结果中的具体内容，禁止敷衍收场
        ok_calls = [t for t in trace if t.get("ok")]
        if ok_calls:
            work.append({"role": "user", "content":
                "【工具结果使用要求】上面 role=tool 的消息里是本轮工具调用拿到的"
                "真实结果，你的回复必须严格遵守："
                "① 把结果里的关键信息——具体的数字、时间、日期、天气、温度、"
                "网页标题、新闻要点、搜索到的条目内容等——**用角色自己的语气说出来**，"
                "让用户确实拿到他要的信息；"
                "② 严禁只回一句空泛的“我看到了”“好多新闻啊”“让我想想”“信息量好大”"
                "之类的敷衍话；也严禁把结果原样照抄成系统口吻；"
                "③ 如果结果是列表/多条，就用角色语气挑最重要的 2~3 条概括说给用户听；"
                "④ 如果工具调用失败或结果为空，就如实说明“没查到/没打开”，不要编造；"
                "⑤ 结果里已经包含用户想要的内容时（哪怕只是片段或摘要，比如歌词片段、"
                "条目、价格、地址），必须把这部分内容讲出来给用户，"
                "绝不允许以“网页上没有显示完整内容”“信息不全”为理由，"
                "把已经拿到的内容也吞掉不说；"
                "⑥ 只回答用户本条最新消息的话题，不要转移去回应更早对话里的提醒或其他话题。"})
        final_content = result["content"]
    else:
        # 轮次用尽模型仍在要求调用工具（或最后一句只是工具调用附言）时，
        # 追加一轮不带工具的强制回答：work 末尾已有工具结果与使用要求，
        # 模型只能依据已拿到的结果作答，杜绝"只搜不答"。
        print(f"[工具流程] {max_iter} 轮内未产出最终回答，追加一轮无工具强制回答。")
        result = await chat_once(ctx, work)
        total_ms += result["ms"]
        llm_calls += 1
        if stats:
            stats.record_llm(result["ms"])
        final_content = result["content"]
    print(f"[工具流程] 本轮完成：共 {len(trace)} 次工具调用，"
          f"最终回答 {len(str(final_content or '').strip())} 字，总耗时 {total_ms:.0f}ms")

    # 兜底：模型没调用任何工具、回答却坦白"不知道/没听说过"，而用户的消息明显
    # 在问一个具体的人/事/物 → 替它强制搜索一轮，把真实结果交给模型重新作答。
    if tools_schema and not trace and str(final_content or "").strip() \
            and bool(ctx.get("tool_uncertain_fallback", True)):
        user_question = ""
        for m in reversed(messages):
            if m.get("role") == "user" and not str(m.get("content", "")).startswith("【"):
                user_question = str(m.get("content", ""))
                break
        search_tool = next((t for t in tools_schema
                            if str(t.get("function", {}).get("name", "")) == "web_search"), None)
        query = _search_query_from_question(user_question)
        if search_tool and _answer_admits_unknown(final_content) \
                and _is_entity_question(user_question) and 2 <= len(query) <= 30 \
                and not _SUBJECTIVE_SEARCH_RE.search(query) \
                and not _is_roleplay_search(query, ctx):
            print(f"[工具流程] 模型自称不确定且问题指向具体事物，强制补搜：{query!r}")
            ok, output = await tool_registry.execute("web_search", {"query": query}, user_id,
                                                     call_counts=call_counts,
                                                     user_text=user_question)
            trace.append({"name": "web_search", "arguments": {"query": query},
                          "ok": ok, "output": output})
            if ok:
                work.append({"role": "user", "content":
                    f"【补查结果】模型刚才表示不确定，系统已替你搜索「{query}」，结果如下：\n"
                    f"{str(output)[:2500]}\n"
                    "请依据以上真实结果重新回答用户的问题：查到了就把相关信息讲给用户；"
                    "确实查不到再如实说明。不要再说不知道。"})
                result = await chat_once(ctx, work)
                total_ms += result["ms"]
                llm_calls += 1
                if stats:
                    stats.record_llm(result["ms"])
                final_content = result["content"]
                print(f"[工具流程] 补搜后重新作答（{len(str(final_content or '').strip())} 字）")
    return {"content": final_content, "tool_trace": trace, "ms": total_ms,
            "llm_calls": llm_calls}


# ---------------------------------------------------------------------------
# 面向主流程的完整入口
# ---------------------------------------------------------------------------

async def generate_text_reply(ctx: RoleContext, system_prompt: str, user_prompt: str,
                              max_tokens: int = 512) -> str:
    """简单的纯文本生成（用于开场白、摘要、画像提取等辅助任务）。"""
    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}]
    try:
        result = await chat_once(ctx, messages)
        # chat_once 已剥离思考字段；这里再兜一次，防止 content 里内嵌的思考块漏出去
        return strip_thinking(result.get("content") or "")
    except Exception as e:
        print(f"文本生成失败: {type(e).__name__}: {e}")
        return ""


async def generate_json_reply(ctx: RoleContext, system_prompt: str, user_prompt: str,
                              max_tokens: int = 512) -> Optional[dict]:
    text = await generate_text_reply(ctx, system_prompt, user_prompt, max_tokens)
    return extract_json(text)


STICKER_USE_HINTS = {
    "gaoxing": "开心大笑、炫耀、起哄",
    "shengqi": "生气、不满、警告",
    "haixiu": "害羞、被夸不好意思",
    "wuyu": "无语、敷衍、怼人",
    "jingya": "惊讶、震惊",
    "sajiao": "撒娇、卖萌、撩人",
    "weixie": "威胁、阴阳怪气、挑逗",
    "pingjing": "平静、日常寒暄",
}


async def get_image_reply(ctx: RoleContext, user_text: str, history: list,
                          emotions: dict, image_urls: list,
                          extra_parts=None, stats=None) -> Optional[Dict]:
    """识图回复：读取本地或下载网络图片，交给识图模型生成句子。"""
    try:
        prompt_text = (
            "用户发来了一张图片，请仔细观察图片内容，结合你的角色人设与上方对话历史，"
            "根据图片内容回复（可以是吐槽、评价、撒娇等）。\n"
            "回复 JSON 必须包含 sentences（角色台词，至少一条）与 description"
            "（一句中文客观描述画面实际内容，只陈述事实）；sentences 不能缺失或为空。\n"
            "图片里的文字要分两种情况看："
            "写着“小妹妹”“老婆”“笨蛋”这类称呼、且没有明确指向别人时，是在称呼你本人"
            "（当前角色），绝不要理解成画面里另有一个人、也不要把话题转到第三方身上，"
            "只有明确写了别人名字时才当作别人；"
            "而写着“我馋你身子”“让我贴贴”这类第一人称的台词，说话的是画中人或发图的人，"
            "不是你本人，引用时绝不能说成是你自己说的话。\n"
            f"用户附加文字：{user_text}"
        )
        if ctx.get("sticker_capture_enabled", False):
            prompt_text += _sticker_capture_instruction(ctx)
        prompt_text += (
            "\n要求："
            "1) sentences 的 zh 要贴合当前这张图的具体内容来写，不要用套话；"
            "若你刚才回复过相似内容，绝不能重复上一句的句子，要换一种完全不同的说法。"
            "2) ja 必须是 zh 的地道日文翻译（含义与语气完全一致，不逐字硬译，不夹带中文）。"
            "3) description 除客观画面内容外，还要写清人物表情神态"
            "（如脸红、害羞、惊讶、生气、无语等）与整体氛围，不要只写构图和画面文字。"
        )
        # 与文本对话共用同一份历史（build_merged_history），保证识图与普通回复上下文互通
        history_msgs = build_merged_history(history, ctx)
        images_for_payload = []  # [(source, mime, base64)]
        seen_sources = set()
        for img_source in image_urls:
            src = str(img_source)
            if src in seen_sources:
                continue
            seen_sources.add(src)
            data = None
            try:
                if src.lower().startswith("file://"):
                    local = file_uri_to_path(src)
                    if os_path_exists(local):
                        with open(local, 'rb') as f:
                            data = f.read()
                    else:
                        print(f"file:// 图片不存在: {local}")
                elif os_path_exists(src):
                    with open(src, 'rb') as f:
                        data = f.read()
                elif src.startswith(("http://", "https://")):
                    data = await download_image(src)
                else:
                    print(f"未知图片路径格式: {src}")
            except Exception as e:
                print(f"获取图片失败 {src[:120]}: {e}")
            if not data:
                continue
            mime = sniff_image_mime(data)
            if not mime:
                # 图床防盗链/过期链接常返回 HTML 错误页，垃圾数据会让识图模型直接 400
                print(f"跳过非图片内容（链接可能已过期或被拦截）: {src[:120]}")
                continue
            data, mime = normalize_image_data(data, mime)
            if not data:
                print(f"图片格式转换失败，已跳过: {src[:120]}")
                continue
            images_for_payload.append((src, mime, base64_b64(data)))
        if not images_for_payload:
            print("没有有效的图片数据，使用默认回复")
            default_text = "啊嘞，看不清这张图呢。"
            return {"sentences": normalize_sentences(default_text, ctx, emotions, user_text), "ms": 0, "tool_trace": []}
        model = ctx.get("image_caption_model_name", "")
        if not model:
            print("未配置识图模型名称，无法处理图片")
            return None
        caption_backend = ctx.get("image_caption_backend", "") or ctx.get("llm_backend", "ollama")
        base_url = str(ctx.get("llm_base_url", "http://127.0.0.1:11434")).rstrip("/")
        timeout = ctx.get("image_caption_timeout", 90)
        system_content = build_system_prompt(ctx, emotions, extra_parts)
        if _cfg_bool(ctx, "image_identity_guard_enabled", True):
            prompt_text = f"{prompt_text}\n{image_identity_note(ctx)}"
        start = time.time()
        if caption_backend == "ollama":
            vision_messages = [{"role": "system", "content": system_content}]
            vision_messages.extend(history_msgs)
            vision_messages.append({"role": "user", "content": prompt_text,
                                    "images": [b64 for _src, _mime, b64 in images_for_payload]})
            payload = {
                "model": model,
                "messages": vision_messages,
                "stream": False, "think": False,
                "options": {"temperature": float(ctx.get("temperature", 0.7)), "num_predict": 1024}
            }
            endpoint = f"{base_url}/api/chat"
            headers = {}
        else:
            content_parts = []
            for source, mime, img_b64 in images_for_payload:
                if str(source or "").startswith(("http://", "https://")):
                    content_parts.append({"type": "image_url",
                                          "image_url": {"url": source}})
                else:
                    content_parts.append({"type": "image_url",
                                          "image_url": {"url": f"data:{mime};base64,{img_b64}"}})
            content_parts.append({"type": "text", "text": prompt_text})
            vision_messages = [{"role": "system", "content": system_content}]
            vision_messages.extend(history_msgs)
            vision_messages.append({"role": "user", "content": content_parts})
            endpoint = f"{base_url}/chat/completions"
            payload = {
                "model": model,
                "messages": vision_messages,
                "stream": False,
                "temperature": float(ctx.get("temperature", 0.7)),
                "max_tokens": 1024
            }
            api_key = ctx.get("llm_api_key", "")
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        async with httpx.AsyncClient(timeout=timeout, verify=verified_context()) as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        ms = (time.time() - start) * 1000
        if caption_backend == "ollama":
            content = data.get("message", {}).get("content", "")
        else:
            content = data["choices"][0]["message"]["content"]
        content = strip_thinking(content or "")
        if stats:
            stats.record_llm(ms)
        sentences = normalize_sentences(content, ctx, emotions, user_text or "（图片）")
        description = extract_image_description(content or "")
        if len(sentences) == 1 and "走神了" in str(sentences[0].get("zh", "") or "") and description:
            try:
                regen_text = f"{user_text or '（看图）'}\n（用户发来一张图片，画面内容：{description}）"
                chat = await chat_once(ctx, build_chat_messages(ctx, regen_text, history, emotions, extra_parts))
                fixed = normalize_sentences(str(chat.get("content") or ""), ctx, emotions, user_text or "（图片）")
                if fixed and not (len(fixed) == 1 and "走神了" in str(fixed[0].get("zh", "") or "")):
                    sentences = fixed
                    ms = ms + float(chat.get("ms", 0) or 0)
                    print("识图模型未输出台词，已基于画面描述由文本模型补回复。")
            except Exception as e:
                print(f"文本模型补回复失败: {type(e).__name__}: {e}")
        result = {"sentences": sentences, "ms": ms, "tool_trace": []}
        if description:
            result["description"] = description
        # 打印 LLM 的原始输出，看看模型到底说了什么
        print(f"\n【表情收藏-LMM原始输出】\n{content}\n【原始输出结束】")

        capture = extract_sticker_capture(content or "")
        # 统一由 sticker_capture_allowed 裁决：默认要求模型给出明确的肯定判定，
        # 从而杜绝"纯风景/无文字图片因为模型没表态就被收藏"的事故。
        if not sticker_capture_allowed(ctx, content or ""):
            if capture is not None:
                print("【表情收藏】本次不收藏（判定未通过）。")
            capture = None
        elif capture is None:
            # 配置允许"没有判定也收藏"且模型既没肯定也没否定
            score = 0.0
            try:
                score = float(ctx.get("sticker_capture_min_score", 0.7) or 0)
            except Exception:
                score = 0.7
            capture = {"should": True, "score": score, "category": "", "reason": ""}

        # 打印提取出来的 JSON 结果
        print(f"【表情收藏-提取结果】{json.dumps(capture, ensure_ascii=False)}")
        if capture is not None:
            result["capture"] = capture
        return result
    except Exception as e:
        print(f"识图模型处理失败: {e}")
        return None


def extract_image_description(text: str) -> str:
    for obj in extract_json_objects(text or ""):
        if not isinstance(obj, dict):
            continue
        for key in ("description", "desc", "image_description"):
            value = obj.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    cleaned = (text or "").strip()
    if cleaned and not cleaned.startswith('{') and not cleaned.startswith('['):
        return cleaned[:200]
    return ""


_KANA_RE = re.compile(r"[\u3040-\u30FF]")
_HANGUL_RE = re.compile(r"[\uAC00-\uD7AF\u1100-\u11FF]")
_HAN_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{3,}")
_LATIN_GARBLE_RATIO = 0.3
_SCRIPT_FLOOR_RATIO = 0.05
_OTHER_SCRIPT_RATIO = 0.3
_LATIN_TARGETS = frozenset({"en", "english"})
_ZH_TARGETS = frozenset({"zh", "zh-cn", "zh-tw", "chinese"})
_TARGET_LABELS = {
    "ja": "日语（用日文假名和汉字书写，不要混入中文）",
    "jp": "日语（用日文假名和汉字书写，不要混入中文）",
    "ko": "韩语（用韩文书写）",
    "kr": "韩语（用韩文书写）",
    "zh": "中文",
    "zh-cn": "中文",
    "zh-tw": "中文",
    "chinese": "中文",
    "en": "英语",
    "english": "英语",
}


def _target_script(target: str):
    if target in ("ja", "jp"):
        return _KANA_RE
    if target in ("ko", "kr"):
        return _HANGUL_RE
    return None


def lang_text_broken(text, target: str) -> bool:
    """文本与目标语言不符时为 True（需要重译后再合成）。

    判据按文字体系：日语必须有假名（纯汉字是中文），韩语必须有谚文，
    中文整句是假名/谚文才算不符，英语必须有拉丁字母。
    """
    t = str(text or "").strip()
    if not t:
        return False
    target = (target or "").strip().lower()
    if not target or target == "auto":
        return False
    n = len(t)
    kana = len(_KANA_RE.findall(t))
    hangul = len(_HANGUL_RE.findall(t))
    han = len(_HAN_RE.findall(t))
    latin = len(_LATIN_RE.findall(t))
    if target in _LATIN_TARGETS:
        return (kana + hangul + han) / n >= _OTHER_SCRIPT_RATIO \
            and latin / n < _OTHER_SCRIPT_RATIO
    if target in _ZH_TARGETS:
        return (kana + hangul) / n >= _OTHER_SCRIPT_RATIO
    if target in ("ja", "jp"):
        if kana:
            if latin and kana / n < _SCRIPT_FLOOR_RATIO and latin / n >= _LATIN_GARBLE_RATIO:
                return True
            return bool(_LATIN_WORD_RE.search(t))
        if han or hangul:
            return True
        return bool(_LATIN_WORD_RE.search(t)) or latin / n >= _LATIN_GARBLE_RATIO
    if target in ("ko", "kr"):
        if hangul:
            return hangul / n < _SCRIPT_FLOOR_RATIO and latin / n >= _LATIN_GARBLE_RATIO
        return bool(han or kana) or latin / n >= _LATIN_GARBLE_RATIO
    return False


def _lang_label(target: str) -> str:
    t = (target or "").strip().lower()
    return _TARGET_LABELS.get(t, f"{t}语")


async def translate_to_lang(ctx, text: str, target: str) -> str:
    """把一句话翻译成目标语言；已是目标语言时原样返回，失败返回空串。"""
    body = str(text or "").strip()
    target = str(target or "").strip().lower()
    if not body or not target or target == "auto":
        return ""
    if not lang_text_broken(body, target):
        return body
    label = _lang_label(target)
    try:
        res = await chat_once(ctx, [{"role": "user", "content":
            f"把这句话翻译成自然的{label}，只输出译文本身，不要解释、不要罗马音，"
            f"整句都用{label}书写，不要保留原语言的词汇：{body}"}])
        cand = str(res.get("content") or "").strip().strip('"“”‘’「」')
        if cand and not lang_text_broken(cand, target):
            return cand
        print(f"该句{label}重译不可用（{cand[:30]!r}），保留原文本。")
    except Exception as e:
        print(f"{target} 台词翻译失败，保留原文本: {type(e).__name__}: {e}")
    return ""


_REPAIR_TIME_BUDGET = 20.0


def _same_text(a, b) -> bool:
    """两段文本是否（基本）是同一段话。"""
    an = re.sub(r"[\s\W_]+", "", str(a or ""), flags=re.UNICODE)
    bn = re.sub(r"[\s\W_]+", "", str(b or ""), flags=re.UNICODE)
    if not an or not bn:
        return False
    if an == bn:
        return True
    if min(len(an), len(bn)) < 3:
        return False
    from difflib import SequenceMatcher
    matched = sum(blk.size for blk in SequenceMatcher(None, an, bn).get_matching_blocks())
    return matched / min(len(an), len(bn)) >= 0.6


def _lang_field_wrong(s: dict, target: str) -> bool:
    """台词字段是否被填成了展示语言（而不是目标语言），需要重译。"""
    lang = str(s.get("lang") or "").strip()
    if not lang or not lang_text_broken(lang, target):
        return False
    if target in ("ja", "jp") and not _KANA_RE.search(lang):
        zh = str(s.get("zh") or "").strip()
        if zh and not _same_text(lang, zh):
            return False
    return True


async def repair_sentence_lang(sentences, ctx) -> int:
    """把台词字段里语言不符的句子重译成目标语言，返回修复条数。

    提示词要求台词字段用目标语言（如 ja），模型偶尔直接填中文；这种句子拿去
    合成要么被当成目标语言念成乱码，要么按文字语言念成另一种语言的语音——
    两种都不是用户要的，所以统一在发送前重译成目标语言。
    纯汉字但与中文台词明显不同的写法（如「受信」）视为日文，不做多余翻译。
    """
    target = str(ctx.get("text_lang", "ja") or "").strip().lower()
    if not target or target == "auto":
        return 0
    label = _lang_label(target)
    fixed = 0
    started = time.time()
    for s in sentences or []:
        if not isinstance(s, dict):
            continue
        lang = str(s.get("lang") or "").strip()
        if not _lang_field_wrong(s, target):
            continue
        if time.time() - started > _REPAIR_TIME_BUDGET:
            print(f"台词语言修复：耗时已达 {_REPAIR_TIME_BUDGET:.0f}s，其余句子保持原样"
                  "（合成侧按文字语言兜底）")
            break
        source = str(s.get("zh") or "").strip() or lang
        cand = await translate_to_lang(ctx, source, target)
        if cand:
            if str(s.get("display") or "") == lang:
                s["display"] = source
            s["lang"] = cand
            fixed += 1
            print(f"台词语言修复：该句不是{label}，已重译为 {cand[:40]!r}")
    return fixed


def extract_sticker_capture(text: str) -> Optional[dict]:
    for obj in extract_json_objects(text or ""):
        if not isinstance(obj, dict):
            continue
        if "sticker_safe" in obj:
            safe = obj.get("sticker_safe")
            if isinstance(safe, bool):
                should = safe
            else:
                should = str(safe).strip().lower() in ("true", "1", "yes", "是")
            if not should:
                return None
            return {"should": True, "score": 1.0,
                    "category": str(obj.get("category", "") or "").strip(),
                    "reason": str(obj.get("reason", "") or "")[:200]}
        if "sticker_capture" not in obj:
            continue
        raw = obj.get("sticker_capture")
        if isinstance(raw, bool):
            should = raw
        else:
            should = str(raw).strip().lower() in ("true", "1", "yes", "是")
        score = 0.0
        try:
            score = float(str(obj.get("score", obj.get("worth", 0)) or 0))
        except Exception:
            score = 0.0
        if score == 0.0 and should:
            score = 0.8
        return {"should": bool(should), "score": max(0.0, min(1.0, score)),
                "category": str(obj.get("category", "") or "").strip(),
                "reason": str(obj.get("reason", "") or "")[:200]}
    return None


# 避免在模块顶部重复导入 os/base64
def os_path_exists(path) -> bool:
    import os
    return os.path.exists(path)


def base64_b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode('utf-8')


# 常见图片文件头。QQ 图床/防盗链经常返回 HTML 错误页或空响应，
# 垃圾数据直接喂给识图模型会触发 400（Failed to load image）。
_IMAGE_MAGICS = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


def sniff_image_mime(data: bytes) -> str:
    """按文件头识别图片格式；非图片内容返回空串。"""
    for magic, mime in _IMAGE_MAGICS:
        if data.startswith(magic):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def file_uri_to_path(uri: str) -> str:
    """file:///C:/a/b.png → C:/a/b.png；非 file:// 原样返回。"""
    from urllib.parse import unquote, urlparse
    u = urlparse(str(uri))
    if u.scheme.lower() != "file":
        return str(uri)
    path = unquote(u.path)
    if re.match(r"^/[A-Za-z]:[\\/]", path):
        path = path[1:]  # Windows 盘符前的斜杠
    if u.netloc and u.netloc != "localhost":
        path = f"//{u.netloc}{path}"  # UNC 路径
    return path


async def download_image(url: str) -> bytes:
    base_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    host = url.split("/", 3)[2] if "://" in url else ""
    headers = {**base_headers}
    if any(d in host for d in ("qq.com", "qpic.cn", "gtimg.cn")):
        headers["Referer"] = f"https://{host}/"
    
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, proxy=None,
                                 trust_env=False, verify=verified_context()) as client:
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400:
                # 某些图床要求不带 Referer，去除后重试
                headers.pop("Referer", None)
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                return resp.content
            raise

def normalize_image_data(data: bytes, mime: str):
    """WebP 转成 JPEG/PNG（多数推理后端不支持），超大图等比缩小到 2048 内。

    Pillow 未安装或转码失败时：WebP 视为不可用（返回 (None, None)），
    其余格式原样返回，交由推理后端自行处理。
    """
    oversize = False
    try:
        import io
        from PIL import Image as PILImage
        with PILImage.open(io.BytesIO(data)) as img:
            oversize = max(img.size) > 2048
            if mime != "image/webp" and not oversize:
                return data, mime
            if oversize:
                img.thumbnail((2048, 2048))
            buf = io.BytesIO()
            if img.mode in ("RGBA", "LA", "P"):
                img.save(buf, format="PNG")
                return buf.getvalue(), "image/png"
            if img.mode != "RGB":
                img = img.convert("RGB")
            img.save(buf, format="JPEG", quality=90)
            return buf.getvalue(), "image/jpeg"
    except Exception:
        return (None, None) if mime == "image/webp" else (data, mime)
