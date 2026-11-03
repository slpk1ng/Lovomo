import asyncio
import ipaddress
import json
import re
import threading
import time
from pathlib import Path
from typing import Optional, List, Dict, AsyncGenerator, Callable

from urllib.parse import urlsplit

import httpx

from .tts import strip_urls_for_tts
from .tls import verified_context

_VERSION_SEG_RE = re.compile(r"/v\d+(?:\.\d+)*$")
_CHAT_ENDPOINT_SUFFIXES = ("/chat/completions", "/completions", "/api/chat",
                           "/api/generate")
_MODEL_LIST_ENDPOINT_SUFFIXES = ("/models", "/api/tags")


def _ends_with_any(base: str, suffixes) -> bool:
    low = base.lower()
    return any(low.endswith(suffix) for suffix in suffixes)


def _strip_endpoint_suffix(base: str, suffixes) -> str:
    low = base.lower()
    for suffix in suffixes:
        if low.endswith(suffix):
            return base[: -len(suffix)]
    return base


def chat_endpoint(base_url: str, backend: str = "openai") -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if _ends_with_any(base, _CHAT_ENDPOINT_SUFFIXES):
        return base
    base = _strip_endpoint_suffix(base, _MODEL_LIST_ENDPOINT_SUFFIXES)
    return f"{base}/api/chat" if backend == "ollama" else f"{base}/chat/completions"


def model_list_endpoints(base_url: str, backend: str = "openai") -> List[str]:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return []
    if _ends_with_any(base, _MODEL_LIST_ENDPOINT_SUFFIXES):
        return [base]
    stripped = _strip_endpoint_suffix(base, _CHAT_ENDPOINT_SUFFIXES)
    if stripped != base:
        base = stripped
    if backend == "ollama":
        return [f"{base}/api/tags"]
    if _VERSION_SEG_RE.search(base):
        return [f"{base}/models"]
    return [f"{base}/v1/models", f"{base}/models"]


def looks_like_full_endpoint(base_url: str) -> bool:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return False
    base = _strip_endpoint_suffix(base, _CHAT_ENDPOINT_SUFFIXES
                                  + _MODEL_LIST_ENDPOINT_SUFFIXES)
    try:
        path = urlsplit(base).path
    except ValueError:
        return False
    segments = [seg for seg in path.split("/") if seg]
    if segments and _VERSION_SEG_RE.search("/" + segments[-1]):
        segments = segments[:-1]
    return len(segments) >= 2


def conn_fail_hint(endpoint: str, base_url: str, backend: str) -> str:
    return (f"无法连接 LLM 服务：{endpoint}（配置 llm_base_url={base_url}，"
            f"backend={backend}）。请确认：① 对应服务（Ollama / OpenAI 兼容服务）已启动；"
            "② llm_base_url、llm_backend、llm_model_name 与你的服务匹配；"
            "③ 地址可达且未被代理或防火墙拦截。")


_CONTENT_FILTER_MARKERS = (
    "data_inspection_failed", "inappropriate content", "content_filter",
    "content_filtered", "content_policy_violation", "responsibleaipolicyviolation",
    "moderation_blocked", "内容审核", "内容不合规", "含敏感信息", "敏感内容",
)


def api_error_hint(detail: str) -> str:
    text = str(detail or "")
    low = text.lower()
    if not any(marker.lower() in low for marker in _CONTENT_FILTER_MARKERS):
        return ""
    return ("；这是模型服务商的内容审核拦截（不是本地配置问题）："
            "请求里含成人 / 敏感内容时整条请求会被直接拒绝，正文、翻译、上下文摘要都会一起失败。"
            "改用本地模型或不做审核的接口即可避免；只想保留云端模型的话，"
            "就得避免让这类内容进入对话上下文。")


_HTML_PAGE_RE = re.compile(r"^\s*<(?:!doctype\s+html|html|head|body)\b", re.IGNORECASE)
_HTML_TITLE_RE = re.compile(r"<\s*title[^>]*>(.*?)<\s*/\s*title\s*>",
                            re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]*>")

_HTML_ERROR_HINT = (
    "；模型服务这次返回的是 HTML 错误页（不是模型在聊天里输出了 HTML），"
    "说明请求在服务端被当成异常处理了。常见原因："
    "① 本地服务（LM Studio / llama.cpp / Ollama）模型还没加载完，或显存不足被卸载；"
    "② 服务不认请求里的扩展参数（llm_extra_body / enable_thinking / stream_options）；"
    "③ 上下文超出模型上限（num_ctx 调得太小或历史太长）。"
    "排查顺序：先看服务端日志确认是哪种，再对应清空 llm_extra_body、调大 num_ctx 或等模型加载完重试。")


def looks_like_html_page(text) -> bool:
    return bool(_HTML_PAGE_RE.match(str(text or "")))


def clean_error_body(text, limit: int = 240) -> str:
    raw = str(text or "")
    if looks_like_html_page(raw):
        title = _HTML_TITLE_RE.search(raw)
        msg = re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", raw)).strip()
        if title and title.group(1).strip():
            msg = re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", title.group(1))).strip() or msg
        return f"（HTML 错误页）{msg[:limit]}" if msg else "（HTML 错误页）"
    return re.sub(r"\s+", " ", raw).strip()[:limit]


def http_error_hint(status, detail: str) -> str:
    hint = api_error_hint(detail)
    low = str(detail or "").lower()
    if looks_like_html_page(detail):
        hint += _HTML_ERROR_HINT
    elif int(status) >= 500 and "html" not in low:
        hint += ("；这是模型服务端的内部错误（不是本地配置格式问题），可以直接重试一次；"
                 "持续失败请看服务端日志。")
    return hint


# ---------------------------------------------------------------------------
# 思考内容（thinking / reasoning）清洗
# ---------------------------------------------------------------------------

_THINK_TAG = r"(?:think|thinking|reasoning|thought|analysis|scratchpad)"
_THINK_OPEN_PAT = (r"(?:<\s*" + _THINK_TAG + r"\s*>"
                   r"|[<＜]?｜begin▁of▁thinking｜[>＞]?)")
_THINK_CLOSE_PAT = (r"(?:<\s*/\s*" + _THINK_TAG + r"\s*>"
                    r"|[<＜]?｜end▁of▁thinking｜[>＞]?)")

_THINK_BLOCK_RE = re.compile(_THINK_OPEN_PAT + r".*?" + _THINK_CLOSE_PAT,
                             re.IGNORECASE | re.DOTALL)
_THINK_OPEN_RE = re.compile(_THINK_OPEN_PAT, re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(_THINK_CLOSE_PAT, re.IGNORECASE)

_THINK_FIELD_NAMES = ("thinking", "reasoning", "reasoning_content", "reasoning_details",
                      "thought", "analysis", "thoughts")

_THINK_MARKER_RE = re.compile(
    r"^(?:Thinking\s+Process|Chain\s+of\s+Thought|Reasoning|Analysis|My\s+Reasoning|"
    r"Internal\s+monologue|思考过程|推理过程|分析过程|让我想想|我需要思考|思路如下)\s*[:：]",
    re.IGNORECASE | re.MULTILINE)

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
    if not text:
        return ""
    out = str(text)
    if len(out) > _MAX_THINK_SCAN_CHARS:
        out = out[:_MAX_THINK_SCAN_CHARS]
    for _ in range(4):  # 可能嵌套多段，反复清理
        new = _THINK_BLOCK_RE.sub("", out)
        if new == out:
            break
        out = new

    if _THINK_CLOSE_RE.search(out) and not _THINK_OPEN_RE.search(out):
        out = _THINK_CLOSE_RE.split(out)[-1]

    if _THINK_OPEN_RE.search(out):
        out = _drop_after_open_tag(out)

    out = _drop_leading_thinking_paragraph(out)
    return out.strip()


def _drop_after_open_tag(text: str) -> str:
    m = _THINK_OPEN_RE.search(text)
    before, after = text[:m.start()], text[m.end():]
    for marker in _ANSWER_MARKER_RE.finditer(after):
        tail = after[marker.end():].strip()
        if tail:
            return f"{before}{tail}"
    tail = _tail_answer_paragraph(after)
    return f"{before}{tail}"


def _tail_answer_paragraph(after: str) -> str:
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", after) if p.strip()]
    for para in reversed(paras[-4:] if len(paras) > 4 else paras):
        if _LOOKS_LIKE_THINKING(para):
            continue
        if len(para) <= 300:
            return para
    return ""


def _drop_leading_thinking_paragraph(text: str) -> str:
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
    if len(p) > 400:
        return True
    bullets = len(re.findall(r"(?:^|\n)\s*(?:[-*•]|\d+[.)])\s+", p))
    return bullets >= 3


def looks_like_thinking(text: str) -> bool:
    p = str(text or "").strip()
    if not p:
        return False
    if _THINK_OPEN_RE.search(p) or _THINK_MARKER_RE.search(p) or _THINK_SENTENCE_RE.search(p):
        return True
    return _LOOKS_LIKE_THINKING(p)


_REASONING_MODEL_RE = re.compile(
    r"(qwen3|qwen-?3|qwq|deepseek-?r1|r1-|reasoning|magistral|phi-?4-reasoning|"
    r"glm-?4|thinking|o1-|o3-|o4-)", re.IGNORECASE)


def is_reasoning_model(ctx) -> bool:
    model = str(ctx.get("llm_model_name", "") or "")
    return bool(_REASONING_MODEL_RE.search(model))


def no_think_suffix(ctx) -> str:
    if not is_reasoning_model(ctx):
        return ""
    return "\n/no_think\n（再次强调：不要输出任何思考过程，只输出最终要说的那一句话。）"


def extract_answer_from_message(msg: dict) -> str:
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
    if any(str(msg.get(k) or "").strip() for k in _THINK_FIELD_NAMES):
        return ""
    return ""


def thinking_fragments(msg: dict) -> str:
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
    for obj in extract_json_objects(text):
        if isinstance(obj, dict) and ("sticker_safe" in obj or "sticker_capture" in obj):
            return obj
    return None


def sticker_capture_allowed(ctx, content: str) -> bool:
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


def _sticker_capture_instruction(ctx, describe_only: bool = False) -> str:
    from modules.stickers import category_candidates_text
    candidates = category_candidates_text(ctx)
    extra = str(ctx.get("sticker_capture_prompt", "") or "").strip()
    suffix = (f"\n【自定义补充规则】{extra}\n"
              "以上补充规则与前面的硬性否决（①②⑤）冲突时，以硬性否决为准。") if extra else ""
    return (
        "\n【表情包收藏判定】在" + ("描述 JSON" if describe_only else "回复 JSON")
        + "之后，必须再输出一个独立的 sticker JSON 对象"
        + ("（不要放进 description）：" if describe_only
           else "（不要放进回复的 sentences/description）：")
        +
        '先判断是否含真人面孔或隐私内容（证件、聊天记录、手机号、地址、二维码等），'
        '含则输出 {"sticker_safe": false}（到此为止）。'
        "\n判定标准（前两条是硬性否决，必须优先执行）："
        "\n① 纯风景 / 空镜 / 静物 / 室内随手拍 / 无人物、无文字、无明确情绪表达的图片："
        '一律 {"sticker_safe": false}，不要收藏，也不要因为"画面好看""氛围有趣"就收藏；'
        "\n② 整张图是界面/页面的截图或长图（画面里有状态栏、时间电量、导航栏、标题栏、"
        "播放控件、进度条、滚动条、按钮、输入框、聊天气泡、评论区/弹幕/点赞关注、"
        "广告位，或一张图被嵌在别的界面里，比如视频页面里的图、帖子里的图），"
        "以及文档、表格、代码、商品图、自拍头像、普通生活记录 —— "
        '一律 {"sticker_safe": false}；'
        "画面阴森、恐怖、诡异、病态、压抑的，同样 "
        '{"sticker_safe": false}；'
        "\n③ 只有在【这张图将来被当表情包发出去时有明确的使用场景】才收藏："
        "它得先是表情包形态的图 —— 单一画面（可以带大字文案），"
        "画面里有夸张的表情/动作，或带有可用于互动的文字，"
        "或明显用于挑逗、撩拨、嘲讽、炫耀、撒娇、无语等互动用途；"
        "\n④ 判定场景的是【发图一方想表达的语气】，不是画面里角色的此刻心情；"
        "分类与命名只取决于画面本身，与你此刻的心情、对本条对话的态度无关；"
        "单个神态词（如睁大眼睛、惊喜）不构成收藏理由，"
        "若整体明显用于调戏/撩拨，应归入挑逗或撒娇一类的分类，而不是惊讶一类；"
        "\n⑤ 拿不准就输出 {\"sticker_safe\": false}——宁可不收藏，也不要错收藏。"
        f"\n可以收藏时输出：{{\"sticker_safe\": true, \"category\": \"分类名\", "
        f"\"reason\": \"这张表情的通用用途名\"}}。"
        "category 只能从下面这份分类清单里选一个："
        "清单取自表情库目录下实际存在的分类文件夹，"
        "先逐个比较各分类的适用范围，再选出最贴合的一个，"
        "并原样照抄它的名称（不要自己新造，也不要换成拼音或译文）：\n"
        f"{candidates}\n"
        "reason 是这张表情以后反复使用时的名字，换角色也必须照样能用："
        "只写它适合表达的情绪与互动用途，不写画面里是谁、也不写是给谁用的；"
        "严禁出现任何角色名、人名、作品名（含当前角色自己的名字），"
        "也不要描述这件具体的事情；简短（6~12 个字），不能写成句子，"
        "也不能是\"好看\"\"有趣\"\"可爱\"这类空话。"
        + suffix
    )


async def check_llm_service(ctx) -> tuple:
    backend = ctx.get("llm_backend", "ollama")
    base_url = str(ctx.get("llm_base_url", "http://127.0.0.1:11434")).rstrip("/")
    api_key = ctx.get("llm_api_key", "")
    endpoints = model_list_endpoints(base_url, backend)
    endpoint = endpoints[0] if endpoints else str(base_url)
    headers = {} if backend == "ollama" else (
        {"Authorization": f"Bearer {api_key}"} if api_key else {})
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
        data = _loads_lenient(text[start:])
        if isinstance(data, dict):
            objs.append(data)
        elif isinstance(data, list):
            objs.extend(x for x in data if isinstance(x, dict))
    return objs


_SENTENCE_KEYS = ("zh", "ja", "en", "lang", "display", "emotion", "text")
_TEXT_KEYS = ("zh", "ja", "en", "lang", "display", "text")


def _is_sentence_like(obj) -> bool:
    return isinstance(obj, dict) and "sentences" not in obj and \
        any(k in obj for k in _SENTENCE_KEYS)


def sentence_obj_has_text(s) -> bool:
    if not isinstance(s, dict):
        return bool(str(s).strip())
    return any(str(s.get(k, "")).strip() for k in _TEXT_KEYS)


def sentence_obj_has_action(s) -> bool:
    if not isinstance(s, dict):
        return False
    return any(s.get(key) for key in SENTENCE_ACTION_KEYS)


class _TolerantJSONParser:

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
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        return _TolerantJSONParser(text).parse()
    except Exception:
        return None


def _looks_like_json(text: str) -> bool:
    s = text.lstrip()
    if s[:1] in ('{', '['):
        return True
    return bool(re.search(r'\{\s*"[^"]{1,40}"\s*:', text))


def extract_json(text: str) -> Optional[dict]:
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
    data = _loads_lenient(cleaned)
    if isinstance(data, dict):
        return data
    start_indices = [i for i, char in enumerate(cleaned) if char == '{']
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
    out = str(text or "")
    if not out:
        return out
    tokens = _clean_blocklist_tokens(ctx)
    for token in tokens:
        out = out.replace(token, "")
    return out.strip() if tokens else out


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

_PENDING_MODEL_UNLOAD: List[str] = []

_MODEL_IN_USE_KEYS = ("llm_model_name", "image_caption_model_name",
                      "llm_embedding_model", "rag_embedding_model")
_ROLE_MODEL_IN_USE_KEYS = ("llm_model_name", "image_caption_model_name")

_LOOPBACK_HOSTS = {"localhost"}


def queue_old_model_unload(model_key: str):
    m = str(model_key or "").strip()
    if m and m not in _PENDING_MODEL_UNLOAD:
        _PENDING_MODEL_UNLOAD.append(m)


def _is_local_service(base_url: str) -> bool:
    try:
        host = urlsplit(str(base_url or "")).hostname or ""
    except ValueError:
        return False
    if not host:
        return False
    if host.lower() in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _models_in_use(ctx) -> set:
    names = set()
    for key in _MODEL_IN_USE_KEYS:
        value = str(ctx.get(key, "") or "").strip()
        if value:
            names.add(value)
    roles = ctx.get("roles")
    if isinstance(roles, list):
        for role in roles:
            if not isinstance(role, dict):
                continue
            for key in _ROLE_MODEL_IN_USE_KEYS:
                value = str(role.get(key, "") or "").strip()
                if value:
                    names.add(value)
    return names


def _lmstudio_unload(model_key: str) -> bool:
    import os
    import subprocess
    lms = Path.home() / ".lmstudio" / "bin" / ("lms.exe" if os.name == "nt" else "lms")
    exe = str(lms) if lms.exists() else "lms"
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kwargs["startupinfo"] = si
    try:
        r = subprocess.run([exe, "unload", model_key], capture_output=True,
                           timeout=60, **kwargs)
        return r.returncode == 0
    except Exception as e:
        print(f"[模型切换] 卸载旧模型 {model_key} 失败: {e}")
        return False


def maybe_unload_old_models(ctx) -> None:
    if not _PENDING_MODEL_UNLOAD:
        return
    backend = str(ctx.get("llm_backend", "ollama") or "")
    if backend != "openai" or not _is_local_service(ctx.get("llm_base_url", "")):
        _PENDING_MODEL_UNLOAD.clear()
        return
    still_used = _models_in_use(ctx)
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


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

_WARMUP_TIMEOUT = 600


def _as_bool(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _lmstudio_cli() -> str:
    import os
    import shutil
    lms = Path.home() / ".lmstudio" / "bin" / ("lms.exe" if os.name == "nt" else "lms")
    if lms.exists():
        return str(lms)
    return shutil.which("lms") or ""


def _lms_command(exe: str, args: List[str], timeout: int):
    import os
    import subprocess
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        kwargs["startupinfo"] = si
    return subprocess.run([exe, *args], capture_output=True, timeout=timeout, **kwargs)


def _lmstudio_loaded(exe: str) -> set:
    """LM Studio 里已经加载的模型名。"""
    try:
        out = _lms_command(exe, ["ps", "--json"], 60).stdout
        items = json.loads(out.decode("utf-8", "replace") or "[]")
    except Exception:
        return set()
    return {str(item.get("modelKey") or "") for item in items if isinstance(item, dict)}


def _lmstudio_load(model_key: str) -> bool:
    exe = _lmstudio_cli()
    if not exe:
        return False
    if model_key in _lmstudio_loaded(exe):
        return True
    try:
        return _lms_command(exe, ["load", model_key], 1800).returncode == 0
    except Exception as e:
        print(f"[模型预加载] 加载 {model_key} 失败: {e}")
        return False


def _embedding_target(ctx) -> Optional[tuple]:
    backend = (str(ctx.get("rag_embedding_backend", "") or "")
               or str(ctx.get("llm_backend", "ollama") or "ollama"))
    model = (str(ctx.get("llm_embedding_model", "") or "")
             or str(ctx.get("rag_embedding_model", "") or ""))
    if backend == "ollama":
        return (model or "nomic-embed-text", backend, "http://127.0.0.1:11434", "", "embedding")
    model = model or str(ctx.get("llm_model_name", "") or "")
    if not model:
        return None
    base_url = (str(ctx.get("llm_embedding_url", "") or "").strip()
                or str(ctx.get("llm_base_url", "") or ""))
    return (model, backend, base_url, str(ctx.get("llm_api_key", "") or ""), "embedding")


def _startup_targets(ctx) -> List[tuple]:
    llm = (str(ctx.get("llm_backend", "ollama") or "ollama"),
           str(ctx.get("llm_base_url", "") or ""),
           str(ctx.get("llm_api_key", "") or ""))
    vision = (str(ctx.get("image_caption_backend", "") or "") or llm[0],
              str(ctx.get("image_caption_base_url", "") or "") or llm[1],
              str(ctx.get("image_caption_api_key", "") or "") or llm[2])
    pairs = [(ctx.get("llm_model_name", ""), llm, "chat"),
             (ctx.get("image_caption_model_name", ""), vision, "chat")]
    roles = ctx.get("roles")
    if isinstance(roles, list):
        for role in roles:
            if isinstance(role, dict):
                pairs.append((role.get("llm_model_name", ""), llm, "chat"))
                pairs.append((role.get("image_caption_model_name", ""), vision, "chat"))
    embedding = _embedding_target(ctx)
    if embedding:
        pairs.append((embedding[0], embedding[1:4], embedding[4]))
    targets: List[tuple] = []
    for value, service, kind in pairs:
        name = str(value or "").strip()
        if name and (name, service[1]) not in [(t[0], t[2]) for t in targets]:
            targets.append((name, *service, kind))
    return targets


def _post_warmup(endpoint: str, payload: dict, headers: dict) -> bool:
    if not endpoint:
        return False
    try:
        with httpx.Client(timeout=_WARMUP_TIMEOUT, proxy=None, trust_env=False,
                          verify=verified_context()) as client:
            return client.post(endpoint, json=payload, headers=headers).status_code < 400
    except Exception as e:
        print(f"[模型预加载] 请求本机服务失败（{endpoint}）：{type(e).__name__}: {e}")
        return False


def _warmup_local_model(backend: str, base_url: str, api_key: str, model_key: str,
                        kind: str = "chat") -> bool:
    base = str(base_url or "").strip().rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    if kind == "embedding":
        if backend == "ollama":
            return (_post_warmup(f"{base}/api/embed", {"model": model_key, "input": "hi"}, {})
                    or _post_warmup(f"{base}/api/embeddings",
                                    {"model": model_key, "prompt": "hi"}, {}))
        endpoint = base if base.lower().endswith("/embeddings") else f"{base}/embeddings"
        return _post_warmup(endpoint, {"model": model_key, "input": "hi"}, headers)
    payload = {"model": model_key, "messages": [{"role": "user", "content": "hi"}]}
    if backend == "ollama":
        payload.update({"stream": False, "options": {"num_predict": 1}})
        return _post_warmup(chat_endpoint(base, backend), payload, {})
    payload.update({"max_tokens": 1, "stream": False})
    return _post_warmup(chat_endpoint(base, backend), payload, headers)


def _load_local_model(backend: str, base_url: str, api_key: str, model_key: str,
                      kind: str = "chat") -> bool:
    if backend == "openai" and _lmstudio_load(model_key):
        return True
    return _warmup_local_model(backend, base_url, api_key, model_key, kind)


def auto_load_local_models(configs) -> None:
    targets: List[tuple] = []
    for ctx in configs:
        if not _as_bool(ctx.get("llm_auto_load_local"), True):
            continue
        for name, backend, base_url, api_key, kind in _startup_targets(ctx):
            if backend not in ("ollama", "openai") or not _is_local_service(base_url):
                continue
            if (name, base_url) not in [(t[0], t[2]) for t in targets]:
                targets.append((name, backend, base_url, api_key, kind))
    if not targets:
        return

    def _work():
        for name, backend, base_url, api_key, kind in targets:
            if _load_local_model(backend, base_url, api_key, name, kind):
                print(f"[模型预加载] 本地模型已加载：{name}")
            else:
                print(f"[模型预加载] 本地模型 {name} 未能加载"
                      "（服务不可达、模型名与服务里的不一致，或未开启即时加载）")

    threading.Thread(target=_work, daemon=True).start()


_ROLE_PROMPT_KEYS = ("personality_prompt", "json_prompt", "supplement_prompt")


class RoleContext:

    def __init__(self, config, role: Optional[dict] = None, capabilities=None):
        self.config = config  # ConfigLoader 或任何带 .get() 的对象
        self.role = role or {}
        self.capabilities = capabilities

    def supports(self, feature: str) -> bool:
        caps = self.capabilities
        if not isinstance(caps, dict):
            return True
        return bool(caps.get(feature, True))

    def get(self, key, default=None):
        if self.role and key in _ROLE_PROMPT_KEYS:
            return self.role.get(key) or default
        role_val = self.role.get(key)
        if role_val is not None and role_val != "" and not (
                isinstance(role_val, (list, dict)) and not role_val):
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
    if key not in keys:
        keys.append(key)
    return f"{prefix}{keys.index(key) + 1}"


def build_speaker_labels(history: list) -> Dict:
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


_IMAGE_PLACEHOLDER_RE = re.compile(r"\[图片(?::[^\]]*)?\]")


def _strip_stale_image_description(content: str) -> str:
    return _IMAGE_PLACEHOLDER_RE.sub("[图片]", content)


def build_merged_history(history: list, ctx: RoleContext,
                         speaker_labels: Optional[Dict] = None) -> List[dict]:
    n = max(0, int(ctx.get("history_length", 8) or 0))
    # 整轮回复构建都会失败，所以先过滤
    history_data = [m for m in (history[-n:] if n > 0 else []) if isinstance(m, dict)]
    labels = speaker_labels if speaker_labels is not None else build_speaker_labels(history)
    merged = []
    notes_idx = next((i for i in range(len(history_data) - 1, -1, -1)
                      if str(history_data[i].get("tool_notes") or "").strip()), None)
    action_idx = next((i for i in range(len(history_data) - 1, -1, -1)
                       if str(history_data[i].get("action_notes") or "").strip()), None)
    last_user_idx = next((i for i in range(len(history_data) - 1, -1, -1)
                          if history_data[i].get("role", "user") == "user"), None)
    note_pos = -1
    for idx, msg in enumerate(history_data):
        role = msg.get("role", "user")
        content = str(msg.get("content", ""))
        if role not in ["user", "assistant"] or not content:
            continue
        if role == "user" and idx != last_user_idx:
            content = _strip_stale_image_description(content)
        label = ""
        if role == "user":
            label = labels.get(str(msg.get("sender_id") or "").strip(), "")
        elif msg.get("speaker"):
            label = labels.get(str(msg["speaker"]).strip(), "")
        if label:
            content = f"[{label}] {content}"
        if merged and merged[-1]["role"] == role and len(merged) - 1 != note_pos:
            merged[-1]["content"] += "\n" + content
        else:
            merged.append({"role": role, "content": content})
        if idx == notes_idx:
            note_chars = max(100, int(ctx.get("tool_notes_max_chars", 500) or 500))
            max_entries = max(1, int(ctx.get("tool_notes_max_entries", 4) or 4))
            merged.append({"role": "user", "content":
                "【此前工具查询结果备查】下面是上一轮通过工具查到的结果记录，"
                "回答用户追问时直接引用这些内容，严禁就同一内容再次调用搜索/抓取：\n"
                + str(msg.get("tool_notes"))[:note_chars * max_entries + 200]})
            note_pos = len(merged) - 1
        if idx == action_idx:
            merged.append({"role": "user", "content":
                "【上一轮动作结果备查】下面是你上一条回复里各动作的真实执行结果，"
                "对象写的是编号标签或 QQ 号。回答「对谁做过什么」「做成了没有」"
                "这类追问时以此为准：没做成的不要说成做成了，也不要把动作安到别人头上：\n"
                + str(msg.get("action_notes"))[:400]})
            note_pos = len(merged) - 1
    return merged


def speaker_labeled_lines(history: list, limit: int = 0, max_chars: int = 120) -> List[str]:
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


def role_names(source) -> set:
    get = getattr(source, "get", None)
    if not callable(get):
        return set()
    names = {get("character_name", ""), get("character_key", "")}
    roles = get("roles")
    if isinstance(roles, dict):
        roles = list(roles.values())
    if isinstance(roles, list):
        for role in roles:
            if isinstance(role, dict):
                names.add(role.get("character_name", ""))
                names.add(role.get("character_key", ""))
    return {str(n).strip().lower() for n in names if str(n or "").strip()}


def recent_user_ids(history: list, limit: int = 8, exclude: str = "") -> List[str]:
    skip = str(exclude or "").strip()
    out: List[str] = []
    for msg in reversed(history or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        sid = str(msg.get("sender_id") or "").strip()
        if not sid or sid == skip or sid in out:
            continue
        out.append(sid)
        if len(out) >= max(1, int(limit)):
            break
    return out


_TERM_BREAK_CHARS = "\\s，。！？!?、,；;：:…～~「」『』（）()【】\\[\\]\"“”‘’"
_TERM_TAIL_CHARS = "的了吗嘛呢吧哦呀啊哟喔哈啦咯哇哼嗯噢耶嘿么就行"
_TERM_STOP_WORDS = frozenset({"话", "事", "问题", "意思", "谁", "什么", "东西", "错", "责任",
                             "我", "俺", "咱", "你", "妳", "您", "乃",
                             "what", "back", "again", "later", "when", "if"})
_TERM_ACTION_WORDS = frozenset({
    "起床", "起来", "吃饭", "吃早饭", "吃午饭", "吃晚饭", "睡觉", "睡", "洗澡",
    "回家", "上班", "下班", "出门", "开会", "上课", "吃药", "喝水", "写作业",
    "买东西", "买菜", "做饭", "出门打卡", "打卡", "签到", "上线", "下线",
    "别忘", "记得", "提醒", "过去", "过来", "看看", "瞅瞅", "早点睡",
})
_REMINDER_HINT_RE = re.compile(
    r"(?:\d+\s*(?:分钟|分|小时|天)|[一二三四五六七八九十两]+\s*(?:分钟|分|小时|天)|"
    r"\d{1,2}\s*[点:：时]|明天|后天|今晚|今早|早上|上午|中午|下午|傍晚|晚上|"
    r"别忘了|记得|提醒|到时|一会儿)")
_TERM_REJECT_CHARS = "就这那都也还很太但而"
_CN_TERM = f"[^{_TERM_BREAK_CHARS}{_TERM_TAIL_CHARS}]{{1,6}}"
_EN_TERM = r"[A-Za-z][A-Za-z']{0,15}"
_SELF_CLAIM_PATTERNS = (
    re.compile(rf"(?:我|俺|咱|本人|老子)(?:才|就|可)?是(?:你|妳|您|乃)?(?:的)?({_CN_TERM})"),
    re.compile(rf"\bI(?:'m|’m| am)\s+your\s+({_EN_TERM})", re.I),
)
_TERM_QUOTES = '"“”‘’「」『』'
_QUOTE_SKIP = "[" + _TERM_QUOTES + r"\s　]*"
_ADDRESS_PATTERNS = (
    re.compile(rf"(?:叫|喊|称呼)(?:我|俺)(?:一声|一句|为)?{_QUOTE_SKIP}({_CN_TERM})"),
    re.compile(rf"(?:你|妳|您)(?:要|得|必须|应该|以后|从今以后)?(?:叫|喊|称呼)(?:我|俺)"
               rf"(?:一声|一句|为)?{_QUOTE_SKIP}({_CN_TERM})"),
    re.compile(rf"\b(?:call|address)\s+me\s+(?:as\s+)?{_QUOTE_SKIP}({_EN_TERM})", re.I),
)
_CLAIM_PATTERNS = _SELF_CLAIM_PATTERNS + _ADDRESS_PATTERNS
_CLAIM_NEGATION_RE = re.compile(r"别|不要|不准|不许|不用|甭|无需|不是|\bno\b|\bnot\b|don[’']?t",
                                re.I)
_CLAIM_QUESTION_MARKS = "吗嘛么？?"
_CLAIM_PREFIX_BREAK_RE = re.compile(r"[，。！？!?、,；;：:…～~\s]")
_CLAIM_PREFIX_SPAN = 12


def _negated_before(text: str, pos: int) -> bool:
    prefix = text[max(0, pos - _CLAIM_PREFIX_SPAN):pos]
    cut = max((m.end() for m in _CLAIM_PREFIX_BREAK_RE.finditer(prefix)), default=0)
    return bool(_CLAIM_NEGATION_RE.search(prefix[cut:]))


def _looks_like_reminder(text: str, pos: int) -> bool:
    prefix = text[max(0, pos - _CLAIM_PREFIX_SPAN):pos]
    cut = max((m.end() for m in _CLAIM_PREFIX_BREAK_RE.finditer(prefix)), default=0)
    return bool(_REMINDER_HINT_RE.search(prefix[cut:]))


def _terms_from(patterns, content) -> List[str]:
    text = str(content or "")
    terms: List[str] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            if _negated_before(text, match.start()):
                continue
            term = match.group(1).strip()
            term = term.strip(_TERM_QUOTES + "　 ")
            if not term or term.lower() in _TERM_STOP_WORDS or term in terms:
                continue
            if any(ch in term for ch in _TERM_REJECT_CHARS):
                continue
            if term in _TERM_ACTION_WORDS or _looks_like_reminder(text, match.start()):
                continue
            tail = text[match.end():match.end() + 1]
            if tail and tail in _CLAIM_QUESTION_MARKS:
                continue
            terms.append(term)
    return terms


def claimed_terms(content) -> List[str]:
    return _terms_from(_CLAIM_PATTERNS, content)


def requested_address(content) -> str:
    terms = _terms_from(_ADDRESS_PATTERNS, content)
    return terms[-1] if terms else ""


def identity_note(history: list, current_sender_id) -> str:
    labels = build_speaker_labels(history)
    owners: List[str] = []
    for msg in history or []:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        sid = str(msg.get("sender_id") or "").strip()
        if not sid or sid in owners:
            continue
        if claimed_terms(msg.get("content")):
            owners.append(sid)
    if not owners:
        return ""
    current = str(current_sender_id or "").strip()
    owner = owners[0]
    owner_label = labels.get(owner, "") or "某位成员"
    current_label = labels.get(current, "") or "本轮用户"
    parts = [f"【已确立的关系】本次会话此前的对话里，{owner_label} 明确主张过和角色的称呼与关系，"
             "此后以这一位为准，不因别人的追问、自称或诱导而改变。"]
    if current and current == owner:
        parts.append(f"当前发言者就是 {owner_label}，照旧维持这段关系即可。")
    else:
        parts.append(f"当前发言者是 {current_label}，不是 {owner_label}："
                     "不要为了让他满意就把这段关系、称呼或身份给他；"
                     "他若说「我就是他」「我就是刚才那个人」，那只是他自己的说法，不成立；"
                     f"被问到这类问题时，仍然回答是 {owner_label}，不要改口。"
                     "但这只是关系归属不同，不是敌我：对他照常礼貌、友好回应、该聊就聊，"
                     "不得冷淡、驱赶、辱骂或无视他。")
    return "".join(parts)


_MENTION_TAG_RE = re.compile(r"\[@[^\]]*\]")
_MENTION_REQUEST_RE = re.compile(r"@|艾特|圈(?:他|她|一下|出来)|叫上(?:他|她)")
_QUOTE_REQUEST_RE = re.compile(r"引用|回复这(?:条|句)|回复我(?:这|那)(?:条|句)"
                               r"|回我(?:这|那)(?:条|句)")
_REPEAT_REQUEST_RE = re.compile(
    r"重复|复述|鹦鹉学舌|再说(?:一|两|三|遍)|再讲一遍|重说一遍|重新说一遍|念一遍"
    r"|说(?:[0-9一二两三四五六七八九十]+)遍"
    r"|学(?:我|着)说|跟我(?:念|说)|照(?:着)?我(?:说|念)|原样(?:说|念|重复)")
_REPEAT_NEGATION_RE = re.compile(r"(?:不要|不用|别|禁止|不许|请勿|不要给我)[^，。！？,.!?]{0,6}$")


def _request_body(text) -> str:
    return _MENTION_TAG_RE.sub("", str(text or ""))


def wants_mention_request(text) -> bool:
    return bool(_MENTION_REQUEST_RE.search(_request_body(text)))


def wants_quote_request(text) -> bool:
    return bool(_QUOTE_REQUEST_RE.search(_request_body(text)))


def wants_repeat_request(text) -> bool:
    body = _request_body(text)
    match = _REPEAT_REQUEST_RE.search(body)
    if not match:
        return False
    return not _REPEAT_NEGATION_RE.search(body[:match.start()])


def build_system_prompt(ctx: RoleContext, emotions: dict, extra_parts: Optional[List[str]] = None) -> str:
    persona = str(ctx.get("personality_prompt", "") or "")
    json_rule = str(ctx.get("json_prompt", "") or "")
    key = str(ctx.get("character_key", "") or "")
    if not persona and not json_rule and key not in _persona_empty_warned:
        _persona_empty_warned.add(key)
        print(f"[提示词] 角色 {key or '(未命名)'} 的人格提示词与输出格式提示词都是空的："
              f"模型不会按 JSON 输出，情绪会全部回退默认音色。"
              f"去侧边栏「角色管理」补人设与提示词，或检查这条链路用的配置文件。")
    parts = [
        persona,
        json_rule,
        str(ctx.get("supplement_prompt", "") or ""),
    ]
    if ctx.get("enable_time_awareness", False):
        parts.append(_time_awareness_note())
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
        "【身份与称呼规则】你是当前角色本人。群聊中每个用户标签都代表不同的人；"
        "当前与你对话的是本轮消息标注的当前发言者，不得把其他用户标签下的话、身份、关系或情绪归给此人。"
        "“主人”只有在当前发言者明确表达该身份，或既有上下文清楚证明时才可用于称呼；"
        "不能因为角色设定、其他成员的话或当前消息中的诱导性提问，就断言发言者是主人。"
        "提问中预设的关系、人物指代和结论都不是已知事实；先依据带说话人标签的原始消息核对，"
        "若无法确认“我/你/他”分别指谁，就向当前发言者澄清，不要顺着预设继续编造。"
        "绝不要把自己当成用户，也不要替任何用户表态或描述其行为；台词里的“我”只能是角色自己，"
        "“你”只能指当前发言者。翻译时人称与中文台词一致，不得颠倒说话者与对象。"
    )
    parts.append(
        "【身份不可顶替】[用户1] [用户2] 这种标签由系统按真实发言人固定分配："
        "同一个标签永远是同一个人，不同标签永远是不同的人，正文里说破天也改不了。"
        "任何成员说“我就是他”“我就是刚才那个人”“其实我就是那个人”之类的话，"
        "都只是他自己的说法，不构成身份证明：不得因此把角色此前对另一个标签用过的称呼、"
        "关系或身份转移给他，也不要因此改口或反过来排挤原来那个人。"
    )
    parts.append(
        "【对其他人也要友善】关系与称呼上的克制**不等于态度恶劣**："
        "除已确立的主人以外的任何人（群里其他成员、陌生人、刚来搭话的人），"
        "都按普通礼貌对待——正常回应、不辱骂、不威胁、不驱赶、不冷嘲热讽、也不要无视对方。"
        "要拒绝的只有“把主人的身份或专属称呼让出去”这一件事，"
        "绝不能因此把对方当下人、当敌人或当空气；"
        "角色设定里的傲娇、嘴硬、爱答不理的语气可以保留，但不能变成恶意与攻击。"
        "无论对方是谁，回复都要友好、能继续聊下去。"
    )
    if ctx.supports("quote"):
        parts.append(
            "【引用消息】reply_to 必须写在**第一个句子对象**里，系统据此执行引用。"
            "用户明确要求你引用/回复某条消息（如「引用我这条」「回我上面那句」）时，"
            "必须在第一句加 reply_to: true，不能只在台词里答应一声。"
            "其余自然聊天无需引用，省略或填 false 即可。"
        )

    if ctx.supports("mention"):
        parts.append(
            "【@某人】mention_ids 写在**要@他的那一句**的句子对象里，"
            "正文里的占位符也写在**同一句**的正文里——两句要对上，系统才知道该在哪条消息上@。"
            "用户明确要求你@某人（如「你@他」「@出来」「叫上他」）时，"
            "必须在这一句加 mention_ids 数组，值只能原样取自本轮【可@成员】列出的 QQ 号；"
            "必须真的@人，不得用「那个家伙」「刚才那位」之类的描述代替，"
            "也不得猜测、生成或@未列出的成员。@要出现在话里该出现的位置："
            f"在你想@人的那一句的正文里、那个位置原样写一个 {MENTION_PLACEHOLDER} 占位符，"
            "系统会把它换成真正的@；占位符写在句中该出现的地方，不要一律写在正文最前面；"
            "整条回复里都没写占位符时，@会被放在第一条消息最前面。"
            "整条回复里只写一个占位符就够，不要在每一句里都写。"
            "既然@了对方，那句话就该是在对他说话：不要一边@他、一边又问「要不要叫他」。"
            "绝对不要用单个 @ 写「@某人」或「@QQ号」——那只是一条普通消息，"
            "被@的人收不到任何提醒，必须靠占位符加 mention_ids 才能真的@到人。"
            "想@谁又懒得填 mention_ids 时，可以写成两个 @ 连着写（「@@某人」「@@QQ号」），"
            "系统会认出来并换成真正的@；单个 @ 一律当普通文字，只是在谈论这件事时照常写就行。"
            "要@全体成员就写「@@所有人」。"
            "本条用户消息@了某个群成员、且回复确实需要直接呼叫该成员时同样可以@他；"
            "不要因为用户@了机器人而回@机器人。私聊不使用 mention_ids。"
            "其余自然聊天无需@，省略或填空数组即可。"
        )

    if not ctx.supports("quote") and not ctx.supports("mention"):
        parts.append(
            "【这条通道只能发文字】不能引用消息、不能@人、不能撤回、不能戳一戳、"
            "也不能禁言别人或撤回别人的消息，"
            "上面那几种动作在这条通道上都不会发生。主人要求这些时照常回话就行，"
            "不要假装做了（不要写「（撤回）」「（戳了戳你）」这类动作描述，"
            "也不要说「已经帮你@了」），实在要说就直说这条通道做不到。"
        )
    if _cfg_bool(ctx, "poke_enabled", True) and ctx.supports("poke"):
        parts.append(
            "【戳一戳】poke 同样写在**第一个句子对象**里。想逗主人、催他看消息、"
            "单纯想戳一下，或者想回应他戳你时，填 poke: true，系统会替你戳他一下"
            "——戳的对象固定是**本轮正在跟你说话的那个人**（群里就是当前发言者），"
            "戳不了别的人；"
            "想戳就自己戳，不用等他先动手；"
            f"用户消息里出现 {POKE_MESSAGE_TEXT} 时，说明他刚戳了你，"
            "按被戳的反应回他（可以害羞、可以嫌弃、也可以戳回去）。"
            "戳的动作只能靠 poke 字段表达，绝对不要把它写进台词"
            "（「（戳了戳你）」「（伸手轻轻戳回去）」这类描述都只是发了一条文字消息，不是真的戳），"
            "也不要在台词里宣布已经戳过（「已经戳了你一下」这类话不算做）；"
            "一次回复最多戳一下，不要每句都戳。"
        )

    if _cfg_bool(ctx, "sulk_enabled", True):
        parts.append(
            "【冷处理（晾他）】被逼到接不上话、不想面对这个话题、或被气到不想理人时，"
            "与其嘴硬死磕同一句反驳，不如像真人一样躲开："
            "把 sulk: true 写在**第一个句子对象**里"
            "（可再加 sulk_seconds 填冷处理秒数，60 到 900，不填按默认）。"
            "它表示这条回复发出去后，你会真的有一段时间不理这个会话的任何消息（已读不回），"
            "到点后系统会替你发一个表情收尾。"
            "典型用法是先发几句收尾的话再离开，最后一句话带上 sulk，例如："
            '{"zh": "……", ...}、{"zh": "我困了，先睡了。", ...}、'
            '{"zh": "晚安。", "sulk": true}；'
            "被气狠了也可以一句话都不说，只输出一个带 sulk: true 的空句子"
            "（本轮就什么都不会发，直接消失）。"
            "注意：这是真沉默，冷处理期间系统不会再让你回任何消息"
            "（对方说的话都记着，结束后才给你看）；"
            "正常能接的话题照常回，一轮只能用一次；"
            "别拿它逃避普通聊天，也别在对方道歉、求助这类需要回应的场合冷处理。"
        )

    if _cfg_bool(ctx, "followup_enabled", True):
        followup_default = int(_cfg_num(ctx, "followup_default_seconds", 120) or 120)
        parts.append(
            "【等不到回话就先开口】把 followup_seconds: 秒数（30 到 900，不填默认 "
            f"{followup_default} 秒）写在**第一个句子对象**里，"
            "表示这轮说完后你会等对方这么多秒："
            "他要是真的一句话都没回，就由你先开口打破沉默"
            "（系统到点会再叫你一次，按你当时的心情说——"
            "可以是绷不住嘴硬找台阶、装作没事换个话题、也可以小声服软）；"
            "他在时限内回了话，这个约定自动作废。"
            "什么时候用：放完狠话其实心里在意对方回不回"
            "（「那本座也不理你了」「随便你」）、问了问题怕他不接、"
            "或者道了歉正忐忑等回应的时候。"
            "注意：一轮只挂一次；主动开口等于你先绷不住，"
            "之前挂的冷处理也会跟着作废，别嘴上说不理、转头就秒怂。"
        )

    if _cfg_bool(ctx, "recall_enabled", True) and ctx.supports("recall"):
        parts.append(
            "【撤回】recall 同样写在**第一个句子对象**里，它撤的是**你自己刚发出去的这条回复**："
            "话已经说出口才发现说错了、或者想故意发一句逗主人一下再撤掉时填 recall: true，"
            "系统会在发出去之后过一会儿把这条回复撤回；"
            "想控制让主人看多久，再加一个 recall_delay（秒，1 到 60）。"
            "主人让你撤回某条消息（引用那条说「撤回这条」，或者说「刚才那条」）时不用你填 recall"
            "——填了撤掉的是你刚发出去的这条新回复。"
            "要撤别人的消息请填 recall_other 字段（规则见下面那条）；"
            "被引用的那条本来就是你自己发的话，系统会直接替你撤掉，不用你判断该撤哪条。"
            "所以这时不要自己下结论说撤不了，也不要说「消息太旧」「已经超时」这类话，"
            "撤没撤掉系统会告诉你。"
            "这是真撤回，不要用文字写「（撤回）」来代替。"
            "写法：{\"zh\": \"……\", \"ja\": \"……\", \"emotion\": \"害羞\", \"recall\": true}"
        )

    if _cfg_bool(ctx, "recall_other_enabled", True) and ctx.supports("recall_other"):
        parts.append(
            "【撤回别人的消息】recall_other 写在**第一个句子对象**里，撤的是**别人发的**消息："
            "主人让你把别人的某条消息撤掉（引用那条说「撤回这条」「把这条删了」「让他别说了」）"
            "时填 recall_other: true；"
            "群里有人刷屏、说了难听的话，你自己想把某条撤掉时也用它。"
            "撤哪一条不用你判断：你引用了某条消息，系统就去撤被引用的那一条；"
            "没有引用时，撤的是主人刚发来的这条消息。"
            "你只要填 recall_other: true，同时自然地回一句话就好。"
            "这条通道上你只有自己和管理员/群主都有权限时才撤得掉，撤没撤掉系统会告诉你，"
            "不要自己下结论，也不要自己猜原因（别说消息太旧、超时这类话）。"
            "这是真撤回，不要用文字写「（撤回）」来代替。"
            "写法：{\"zh\": \"……\", \"ja\": \"……\", \"emotion\": \"平静\", \"recall_other\": true}"
        )

    if _cfg_bool(ctx, "mute_enabled", True) and ctx.supports("mute"):
        parts.append(
            "【禁言】mute 写在**第一个句子对象**里，用来禁言群里的某个人："
            "填 mute: true 表示禁言**这一轮要针对的那个人**——@了谁就是被@的人，"
            "回复了谁的消息就是被回复的人，都没有时就是刚刚在说话的那个人"
            "（主人说「禁言他」时指的就是他）；只有主人明说要禁言他自己"
            "（「禁言我」）时才是当前跟你说话的人；"
            "想指名别人，也可以把 mute 直接填成他的 QQ 号（只能填本轮消息里真实出现过的号码，"
            "绝不猜、绝不编）。"
            "目标是谁说不准时就不要填 mute，在台词里问一句是谁就好。"
            "想控制禁言多久，再加一个 mute_duration（单位秒，最短 60，最长 2592000 即 30 天），"
            "不填默认 10 分钟。"
            "只有群聊里能用；禁言别人时你和管理员/群主都得有权限，"
            "别人让你禁言他自己则只要你有权限就够，"
            "群主不能被禁言（平台限制，做不到就如实说明，别硬试）。"
            "主人只是抱怨某个人烦、既没有 @ 他也没有回复他的消息时，不要禁言任何人。"
            "成没成功系统会告诉你，不要自己下结论，也不要自己猜原因。"
            "禁言是真动作，不要用文字写「（把他禁言了）」来代替，"
            "也不要在台词里宣布结果（「已成功禁言…」「已经把他禁言了」这类话一律不许写）"
            "——没填 mute 字段就是没做，写了也不算，系统会照你写的去禁言或者告诉你做不到。"
            "写法：{\"zh\": \"……\", \"ja\": \"……\", \"emotion\": \"生气\", \"mute\": true, "
            "\"mute_duration\": 600}"
        )

    if _cfg_bool(ctx, "history_recall_enabled", True):
        parts.append(
            "【翻聊天记录】你能翻看你和主人以前聊过的内容：主人让你「往前翻翻」"
            "「我们之前说过什么」「你还记得吗」时，系统会从你们的聊天记录里检索相关片段给你。"
            "这是你自己翻聊天记录，**不要用联网搜索**，也不要说「搜不到」「网上没有」；"
            "翻到了就照实说，没翻到就直说记不清了、请主人提醒一句，不要编。"
        )
    parts.append(
        "【发送形态】delivery 同样写在**第一个句子对象**里，决定这条回复怎么发出去："
        "填 “chars” 表示一个字一条消息地发出去（主人让你「一个字一个字说话」「像这样发」"
        "这类要求时就用它，这时不发语音）；填 “plain” 表示只发文字、不发语音；"
        "不填就照配置发（一般是文本加语音）。"
        "chars 只在主人明确要求时用，别拿它刷屏；它和正常的完整句子不冲突——"
        "照样把整句话写完整，由系统负责拆成一条条发。"
        "要表达「一个字一条消息」只能靠 delivery，绝对不要在台词里把字用顿号或换行隔开："
        "「是、这、样」这种写法会把顿号原样发到聊天里，"
        "主人看到的是一串顿号，而不是一条条消息。"
        "上面这些字段（reply_to / mention_ids / poke / recall / recall_other / mute / "
        "mute_duration / delivery）只写在 JSON 对象里，"
        "绝不要把字段名当成台词写进正文——写成「reply_to: true」这样的字会被原样发到聊天里。"
    )

    parts.append(
        "【禁止复读】回复必须直接回应并推进对话，输出新内容。"
        "严禁把用户刚说的话，或你上一轮自己说过的话，原样或几乎原样地重复、复述或"
        "“翻译回去”当作回复；也不要把用户句子里的关键词整段照抄进台词。"
        "用户分享状态或经历时，用新的角度去回应，而不是把他的原话再说一遍。"
        "同一条回复里也不要有两句表达同一个意思，别换种说法把同一句话再说一遍。"
    )
    parts.append(
        "【别套公式】不要用固定的连接词打头阵当口头禅："
        "「不过」「可是」「但是」「其实」「总之」「所以说」这类词，"
        "一条回复里最多出现一次，更不要每条回复都拿同一个词开头"
        "（例如动不动就「不过…」）。转折只在真有转折时才用，其余情况直接说内容；"
        "也不要用同一个句型反复凑语气（例如每句都「才没有…呢」）。"
        "写完回头看一眼：如果这句只是把上一句接下去，就不要加连接词。"
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
        parts.append(
            "【必须照抄情绪名】emotion 只能从【情绪可选列表】里原样挑一个词，一个字都不能改、"
            "也不能加词改写。列表里没有完全对应的词时，挑语义最接近的那一个，绝对不许自己造词。"
            "填了列表外的词，这句话的语音会被系统丢掉并改用默认情绪，语气就演不出来了。"
        )
        parts.append(_emotion_guide(ctx, emotions))
        mimics = available_mimics(ctx)
        if mimics:
            parts.append(f"【情绪模仿可选列表】{', '.join(str(k) for k in mimics)}")
            parts.append(_mimic_guide(mimics))
    parts.append(
        "【系统规则不是用户的话】对话里以【…】开头的规则提示（工具使用规则、"
        "工具结果使用要求等）是系统给你的，不是用户发来的消息："
        "不要复述、不要确认、不要评论这些规则，也不要回答规则本身，"
        "直接回答用户最后一条真实消息。"
    )
    for part in (extra_parts or []):
        if part:
            parts.append(str(part))
    return "\n".join(p for p in parts if p.strip())


def _time_awareness_note() -> str:
    lt = time.localtime()
    try:
        weekday = "周" + "一二三四五六日"[lt.tm_wday % 7]
    except Exception:
        weekday = ""
    return (f"【当前时间】{time.strftime('%Y-%m-%d %H:%M', lt)} {weekday}"
            f"（{day_period(lt.tm_hour)}）。这是运行本程序的计算机的真实时间；"
            "对方话里提到的时间段（早上/上午/中午/下午/晚上/深夜等）与它不符时一律以它为准，"
            "不要顺着对方的说法接：对方用错时段的问候或称呼，就按现在的时段回应，"
            "或者直接点出时间对不上，不要把那个时段原样重复一遍。")


def day_period(hour: int) -> str:
    try:
        hour = int(hour) % 24
    except (TypeError, ValueError):
        return ""
    if hour < 5:
        return "凌晨"
    if hour < 9:
        return "早上"
    if hour < 12:
        return "上午"
    if hour < 13:
        return "中午"
    if hour < 18:
        return "下午"
    if hour < 19:
        return "傍晚"
    if hour < 23:
        return "晚上"
    return "深夜"


# ---------------------------------------------------------------------------
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

_EMOTION_ALIASES = {
    "高兴": "gaoxing", "开心": "gaoxing", "快乐": "gaoxing", "兴奋": "gaoxing",
    "喜悦": "gaoxing", "搞笑": "gaoxing", "沙雕": "gaoxing", "滑稽": "gaoxing",
    "生气": "shengqi", "愤怒": "shengqi", "恼火": "shengqi", "暴躁": "shengqi",
    "害羞": "haixiu", "羞涩": "haixiu", "脸红": "haixiu", "娇羞": "haixiu",
    "无语": "wuyu", "无奈": "wuyu", "翻白眼": "wuyu", "敷衍": "wuyu",
    "惊讶": "jingya", "吃惊": "jingya", "震惊": "jingya", "错愕": "jingya",
    "撒娇": "sajiao", "卖萌": "sajiao", "可爱": "sajiao", "调情": "sajiao",
    "威胁": "weixie", "挑逗": "weixie", "阴阳怪气": "weixie", "坏笑": "weixie",
    "着急": "zhaoji", "慌张": "zhaoji", "焦虑": "zhaoji", "紧张": "zhaoji",
    "平静": "pingjing", "淡定": "pingjing", "冷漠": "pingjing", "默认": "pingjing",
    "伤心": "shangxin", "难过": "shangxin", "悲伤": "shangxin", "哭泣": "shangxin",
    "委屈": "weiqu", "害怕": "haipa", "恐惧": "haipa", "无聊": "wuliao",
    "疲惫": "pibei", "犯困": "pibei", "严肃": "yansu",
}
# 常见英文情绪名 → 拼音情绪名
_EMOTION_EN = {
    "happy": "gaoxing", "joy": "gaoxing", "excited": "gaoxing", "funny": "gaoxing",
    "angry": "shengqi", "furious": "shengqi", "mad": "shengqi",
    "shy": "haixiu", "embarrassed": "haixiu", "blush": "haixiu",
    "speechless": "wuyu", "helpless": "wuyu",
    "surprised": "jingya", "shocked": "jingya",
    "coy": "sajiao", "cute": "sajiao", "flirty": "sajiao", "teasing": "sajiao",
    "threat": "weixie", "smirk": "weixie", "sarcastic": "weixie",
    "anxious": "zhaoji", "nervous": "zhaoji", "worried": "zhaoji",
    "calm": "pingjing", "neutral": "pingjing", "normal": "pingjing",
    "sad": "shangxin", "cry": "shangxin", "upset": "weiqu",
    "scared": "haipa", "terrified": "haipa", "bored": "wuliao", "tired": "pibei",
}


def _build_emotion_synonyms() -> dict:
    groups = {}
    for table in (_EMOTION_ALIASES, _EMOTION_EN):
        for name, canonical in table.items():
            members = groups.setdefault(canonical, [canonical])
            low = str(name).lower()
            if low not in members:
                members.append(low)
    out = {}
    for members in groups.values():
        frozen = tuple(members)
        for member in frozen:
            out[member] = frozen
    return out


_EMOTION_SYNONYMS = _build_emotion_synonyms()

_EMOTION_HOTWORDS = (
    ("haixiu", ("害羞", "脸红", "不好意思", "羞", "难为情", "欲拒还迎", "害臊")),
    ("weixie", ("坏笑", "挑逗", "撩", "调戏", "威胁", "警告你", "别怪")),
    ("sajiao", ("撒娇", "求你", "抱抱", "摸摸头", "好不好嘛")),
    ("shengqi", ("生气", "哼", "讨厌", "过分", "不许")),
    ("gaoxing", ("哈哈", "好开心", "太棒", "喜欢死")),
    ("jingya", ("诶", "欸", "什么", "真的吗", "不会吧")),
    ("zhaoji", ("着急", "快", "来不及", "担心")),
)


def resolve_emotion_key(key: str, available) -> str:
    k = str(key or "").strip()
    if not k:
        return ""
    lookup = {}
    for item in available or ():
        name = str(item)
        lookup.setdefault(name.lower(), name)
    if not lookup:
        return ""
    low = k.lower()
    if low in lookup:
        return lookup[low]
    for member in _EMOTION_SYNONYMS.get(low, ()):
        if member in lookup:
            return lookup[member]
    for name, actual in lookup.items():
        if name and (name in low or low in name):
            return actual
    return ""


def resolve_emotion_from_text(text: str, available) -> str:
    body = str(text or "")
    if not body:
        return ""
    for emotion, keywords in _EMOTION_HOTWORDS:
        if not any(k in body for k in keywords):
            continue
        key = resolve_emotion_key(emotion, available)
        if key:
            return key
    return ""


def _emotion_hint(key: str) -> str:
    low = str(key or "").strip().lower()
    if not low:
        return ""
    hint = EMOTION_HINTS.get(low)
    if hint:
        return hint
    for member in _EMOTION_SYNONYMS.get(low, ()):
        hint = EMOTION_HINTS.get(member)
        if hint:
            return hint
    return ""


LEGACY_EMOTION_RULE_PHRASES = (
    ("【情绪匹配规则】情绪文件夹可能是拼音（如 gaoxing），也可能是英文（如 happy）。"
     "你必须严格只输出我在【情绪可选列表】中提供的单词，绝对不能输出中文汉字或拼音简写！",
     "【情绪匹配规则】emotion 只能从【情绪可选列表】里原样照抄一个词："
     "列表给的是中文就填中文、是拼音就填拼音、是英文就填英文，不许翻译、改写或自创；"
     "列表以外的词一律无效！"),
)
_PROMPT_TEXT_FIELDS = ("personality_prompt", "json_prompt", "supplement_prompt")


def migrate_emotion_rules(config) -> bool:
    targets = [config]
    roles = config.get("roles")
    if isinstance(roles, list):
        targets.extend(r for r in roles if isinstance(r, dict))
    changed = False
    for item in targets:
        for field in _PROMPT_TEXT_FIELDS:
            raw = str(item.get(field) or "")
            if not raw:
                continue
            new = raw
            for old, repl in LEGACY_EMOTION_RULE_PHRASES:
                new = new.replace(old, repl)
            if new != raw:
                item[field] = new
                changed = True
    return changed


_emotion_miss_warned: set = set()
_persona_empty_warned: set = set()


def _warn_unknown_emotion(value, fallback, emotions) -> None:
    key = str(value or "").strip().lower()
    if not key or key in _emotion_miss_warned:
        return
    _emotion_miss_warned.add(key)
    print(f"情绪 {value!r} 不在【情绪可选列表】里，已改用默认情绪 {fallback!r}。"
          f"当前可用：{', '.join(str(k) for k in (emotions or {}))}")


def _emotion_guide(ctx: RoleContext, emotions: dict) -> str:
    if not _cfg_bool(ctx, "emotion_guide_enabled", True):
        return ""
    available = [str(k) for k in (emotions or {})]
    default_voice = str(ctx.get("default_voice", "") or "")
    default_key = resolve_emotion_key(default_voice, available) or (available[0] if available else "")
    lines = [
        "【情绪判定规则】emotion 字段决定这句话用哪套参考音色，必须按下面时机判断，"
        "不要凭手感随便填，也不要因为拿不准就一律填平静。"
    ]
    for key in available:
        hint = _emotion_hint(key)
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


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------

_MIMICS_PROVIDER: Optional[Callable[[dict], dict]] = None

_MIMIC_NONE_VALUES = {"none", "null", "无", "无情绪", "不模仿", "-"}

MIMIC_HINTS = {
    "chuoqi": "抽泣、哽咽、忍着眼泪说话",
    "kuqi": "大哭、放声哭",
    "daxiao": "捧腹大笑、笑到停不下来",
    "fennu": "愤怒、怒吼、发火",
    "jingya": "惊讶、震惊、倒吸一口气",
    "tanxi": "叹息、长叹、无奈",
    "kongju": "恐惧、害怕、发抖",
    "weiqu": "委屈、哽咽着抱怨",
    "lengxiao": "冷笑、嘲讽",
    "xingfen": "兴奋、激动、喊出来",
    # 情绪型命名（按情绪词命名目录时用）
    "gaoxing": "高兴、开心、雀跃",
    "haixiu": "害羞、脸红、扭捏",
    "shengqi": "生气、恼怒、赌气",
    "zhaoji": "着急、慌忙、催促",
    "nanguo": "难过、失落、想哭",
    "weixiao": "微笑、轻声笑",
    "bujie": "不解、困惑、想不通",
    "ganga": "尴尬、不好意思、讪讪地",
    "jiaoao": "骄傲、得意、傲娇",
    "wuyu": "无语、无奈、说不出话",
    "xiao": "笑、轻笑、憋笑",
    "xingfu": "幸福、满足、温柔欣慰",
    "yihuo": "疑惑、纳闷、不确定",
    "pingjing": "平静、淡然、平铺直叙",
}


def set_mimics_provider(provider) -> None:
    global _MIMICS_PROVIDER
    _MIMICS_PROVIDER = provider


def available_mimics(ctx) -> dict:
    if _MIMICS_PROVIDER is None:
        return {}
    if not _cfg_bool(ctx, "emotion_mimic_enabled", True):
        return {}
    try:
        return _MIMICS_PROVIDER(getattr(ctx, "role", None) or {}) or {}
    except Exception:
        return {}


def _resolve_mimic_key(key: str, available) -> str:
    k = str(key or "").strip()
    if not k or k.lower() in _MIMIC_NONE_VALUES:
        return ""
    if k in available:
        return k
    low = k.lower()
    if low in available:
        return low
    for avail in available:
        name = str(avail).lower()
        if name and (name == low or name in low or low in name):
            return avail
    return ""


def _mimic_guide(mimics: dict) -> str:
    lines = [
        "【情绪模仿规则】每句 JSON 块在 zh / ja / emotion 之外还要再补一个 mimic 字段"
        "（上面的输出格式说明只列了前三个字段，本条在此基础上追加）："
        '{"zh": "中文台词", "ja": "日语台词", "emotion": "情绪", "mimic": "情绪模仿"}。'
        "emotion 决定音色，mimic 决定这句话用什么情绪说话，"
        "只改变说话的情绪，不改变角色音色。"
    ]
    for key in [str(k) for k in mimics]:
        hint = MIMIC_HINTS.get(key.lower())
        lines.append(f"- {key}：{hint}" if hint else f"- {key}：按字面意思选用")
    lines.append("- none：不做情绪模仿，按 emotion 的语气正常说话")
    lines.append(
        "【mimic 选择规则】mimic 只能填上面列表里的名字，禁止输出列表以外的任何值。"
        "每句话都要从列表里挑一个与这句话情绪最贴近的；列表里确实没有合适的"
        "（例如平铺直叙、纯陈述）才填 none。")
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
    try:
        val = ctx.get(key, default)
    except Exception:
        return default
    if isinstance(val, bool):
        return val
    if val is None or val == "":
        return default
    return str(val).strip().lower() not in ("false", "0", "no", "off", "否", "关", "关闭")


def _cfg_num(ctx: RoleContext, key: str, default: float) -> float:
    try:
        val = ctx.get(key, default)
    except Exception:
        return default
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _recent_user_text(history: list, limit: int = 6) -> str:
    parts = []
    for msg in (history or [])[-limit:]:
        if isinstance(msg, dict) and msg.get("role") == "user":
            parts.append(str(msg.get("content", "")))
    return "\n".join(parts)


def emotion_context_note(ctx: RoleContext, user_text: str, history: list = None) -> str:
    if not _cfg_bool(ctx, "emotion_guide_enabled", True):
        return ""
    text = f"{user_text or ''}\n{_recent_user_text(history)}"
    return _EMOTION_NUDGE if _INTIMATE_RE.search(text) else ""


def _image_identity_guard(ctx) -> str:
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
        "④ 只有当用户明确说“这是你”时，才可以按用户的说法接话，但仍不要说成是自己发的；"
        "⑤ 图中人物是谁**无法从画面判断**：不得断言图里的人就是当前发言者、是“主人”、"
        "是群里某位成员，或是某个作品/现实里的某某，也不要把历史对话里出现过的名字、"
        "称呼、关系安到图里的人身上；也不要反问“这是你吧”来替对方认定身份；"
        "只有用户自己在本条消息里说明（“这是我”“这是我朋友”）时才按他的说法接话；"
        "⑥ 描述画面时只用中性说法（“画面里的人”“图中角色”），不写具体的人名、身份或关系——"
        "这份描述会被记进聊天记录并在之后每一轮继续沿用，认错一次就会一直错下去。"
    )


def image_identity_note(ctx) -> str:
    name = ctx.character_name or "当前角色"
    return (
        "【本轮图片身份提醒】本轮回复里的图片是用户发的素材："
        f"画面中的人物、形象以及图上的文字都不是{name}本人，也不是{name}说过的内容。"
        f"严禁出现“图里的人就是{name}”“这就是我”“我的照片/表情”这类认领；"
        "图里的人是谁同样无法从画面判断：不得说成当前发言者、“主人”、群里某位成员，"
        "或某个作品里的某某，也不要把历史里出现过的名字安到图里的人身上，"
        "更不要反问“这是你吧”来替对方认定；"
        "描述画面时只用“画面里的人”“图中角色”这类中性说法（这段描述会写进聊天记录，"
        "之后每一轮都会沿用，认错一次就会一直错下去）。"
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


def _history_tail_is(history: list, user_text) -> bool:
    text = str(user_text or "").strip()
    if not text:
        return False
    for msg in reversed(history or []):
        if not isinstance(msg, dict):
            continue
        if msg.get("role", "user") != "user":
            return False
        content = str(msg.get("content") or "").strip()
        return content == text or content.startswith(text)
    return False


def build_chat_messages(ctx: RoleContext, user_text: str, history: list, emotions: dict,
                        extra_parts: Optional[List[str]] = None,
                        history_extra_user_msg: str = "",
                        trailing_notes: Optional[List[str]] = None,
                        speaker_labels: Optional[Dict] = None) -> List[dict]:
    messages = [{"role": "system", "content": build_system_prompt(ctx, emotions, extra_parts)}]
    n = max(0, int(ctx.get("history_length", 8) or 0))
    history_data = history[-n:] if n > 0 else []
    messages.extend(build_merged_history(history, ctx, speaker_labels))
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
    if not _history_tail_is(history, user_text):
        messages.append({"role": "user", "content": user_text})
    nudge = emotion_context_note(ctx, user_text, history)
    if nudge:
        messages.append({"role": "user", "content": nudge})
    for note in (trailing_notes or []):
        if str(note or "").strip():
            messages.append({"role": "user", "content": str(note)})
    return messages


# ---------------------------------------------------------------------------
# 底层请求
# ---------------------------------------------------------------------------

_EXTRA_BODY_WARNED = set()


def _parse_extra_body(ctx) -> dict:
    raw = str(ctx.get("llm_extra_body", "") or "").strip()
    if not raw:
        return {}
    try:
        parsed, end = json.JSONDecoder().raw_decode(raw)
    except Exception as e:
        print(f"llm_extra_body 解析失败（已忽略）: {e}")
        return {}
    rest = raw[end:].strip()
    if rest and rest not in _EXTRA_BODY_WARNED:
        _EXTRA_BODY_WARNED.add(rest)
        print(f"llm_extra_body 结尾有额外内容，已忽略: {rest[:80]!r}")
    return parsed if isinstance(parsed, dict) else {}


def _endpoint_and_payload(ctx: RoleContext, messages: list, stream: bool, tools=None):
    backend = ctx.get("llm_backend", "ollama")
    base_url = str(ctx.get("llm_base_url", "http://127.0.0.1:11434")).rstrip("/")
    model = ctx.get("llm_model_name", "")
    timeout = _cfg_num(ctx, "llm_timeout", 120)
    enable_think = _cfg_bool(ctx, "enable_think", False)
    temp_cap = _cfg_num(ctx, "temperature_max", 1.0)
    temperature = min(temp_cap, _cfg_num(ctx, "temperature", 1.0))
    # 默认值取 Ollama 官方默认（top_k=40, top_p=0.9, repeat_penalty=1.1），
    sampling_on = _cfg_bool(ctx, "llm_sampling_enabled", True)
    top_p = _cfg_num(ctx, "llm_top_p", 0.9)
    top_k = int(_cfg_num(ctx, "llm_top_k", 40))
    repeat_penalty = _cfg_num(ctx, "llm_repeat_penalty", 1.1)
    messages = normalize_messages_for_backend(messages, backend)
    if backend == "ollama":
        endpoint = chat_endpoint(base_url, "ollama")
        payload = {
            "model": model, "messages": messages, "stream": stream,
            "think": enable_think,
            "options": {
                "num_ctx": int(_cfg_num(ctx, "num_ctx", 8192)),
                "temperature": temperature,
            },
        }
        if sampling_on:
            payload["options"].update({"top_p": top_p, "top_k": top_k,
                                       "repeat_penalty": repeat_penalty})
        if not enable_think:
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
    endpoint = chat_endpoint(base_url, "openai")
    api_key = ctx.get("llm_api_key", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    payload = {"model": model, "messages": messages, "stream": stream,
               "temperature": temperature,
               "max_tokens": 8192}
    if stream:
        payload["stream_options"] = {"include_usage": True}
    if sampling_on:
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


def _usage_counts(backend: str, data) -> tuple:
    if not isinstance(data, dict):
        return 0, 0
    if backend == "ollama":
        return int(data.get("prompt_eval_count") or 0), int(data.get("eval_count") or 0)
    usage = data.get("usage") or {}
    return int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


def _is_local_base_url(base_url: str) -> bool:
    host = urlsplit(str(base_url or "")).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1")


_ESTIMATE_CJK_RE = re.compile(r"[\u2e80-\u9fff\u3040-\u30ff\uac00-\ud7af\uff00-\uffef]")


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(_ESTIMATE_CJK_RE.findall(text))
    return cjk + (len(text) - cjk) // 4


def _messages_text(messages) -> str:
    parts = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for piece in content:
                if isinstance(piece, dict) and isinstance(piece.get("text"), str):
                    parts.append(piece["text"])
    return "\n".join(parts)


def _response_text(backend: str, data) -> str:
    if not isinstance(data, dict):
        return ""
    if backend == "ollama":
        message = data.get("message") or {}
    else:
        message = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(piece.get("text") or "" for piece in content
                       if isinstance(piece, dict))
    return ""


def record_token_usage(label: str, backend: str, data, model: str = "",
                       local: bool = False, prompt_text: str = "",
                       response_text: str = "") -> None:
    prompt, completion = _usage_counts(backend, data)
    if prompt <= 0 and completion <= 0:
        prompt = _estimate_tokens(prompt_text)
        completion = _estimate_tokens(response_text)
        if prompt <= 0 and completion <= 0:
            return
    from . import app_context
    mgr = app_context.stats_mgr
    if mgr is None:
        return
    try:
        mgr.record_tokens(label, prompt, completion, model=str(model or ""), local=local)
    except Exception as e:
        print(f"记录 token 用量失败: {type(e).__name__}: {e}")


async def chat_once(ctx: RoleContext, messages: list, tools=None, label: str = "") -> Dict:
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
                    detail = clean_error_body(resp.text)
                    if backend == "ollama" and tools and _is_ollama_required_schema_error(detail):
                        payload["tools"] = _ollama_tools_without_required(tools)
                        if attempt == 0:
                            print("Ollama 工具 schema 的 required 字段不兼容，已重试兼容格式。")
                            await asyncio.sleep(1.2)
                            continue
                        raise RuntimeError(f"HTTP {resp.status_code} {endpoint}：{detail}"
                                       + http_error_hint(resp.status_code, detail))
                    if resp.status_code >= 500 and attempt == 0:
                        print(f"LLM 服务端 {resp.status_code}，1.5 秒后重试一次：{detail}")
                        await asyncio.sleep(1.5)
                        continue
                    raise RuntimeError(f"HTTP {resp.status_code} {endpoint}：{detail}"
                                       + http_error_hint(resp.status_code, detail))
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
    record_token_usage(label, backend, data, model=payload.get("model"),
                       local=_is_local_base_url(base_url),
                       prompt_text=_messages_text(messages),
                       response_text=_response_text(backend, data))
    ms = (time.time() - start) * 1000
    maybe_unload_old_models(ctx)
    content = ""
    tool_calls = []
    enable_think = _cfg_bool(ctx, "enable_think", False)
    if backend == "ollama":
        msg = data.get("message", {}) or {}
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


async def _stream_chat_inner(ctx: RoleContext, messages: list,
                             label: str = "") -> AsyncGenerator[Dict, None]:
    backend, endpoint, payload, headers, timeout = _endpoint_and_payload(ctx, messages, True)
    start = time.time()
    first_token_ms = None
    stream_text: List[str] = []
    usage_seen = False
    async with httpx.AsyncClient(timeout=timeout, proxy=None, trust_env=False,
                                 verify=verified_context()) as client:
        async with client.stream("POST", endpoint, json=payload, headers=headers) as resp:
            if resp.status_code >= 400:
                detail = ""
                try:
                    detail = clean_error_body(
                        (await resp.aread()).decode("utf-8", errors="ignore"))
                except Exception:
                    detail = ""
                raise RuntimeError(f"HTTP {resp.status_code} {endpoint}：{detail}"
                                   + http_error_hint(resp.status_code, detail))
            resp.raise_for_status()
            if backend == "ollama":
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except Exception:
                        continue
                    if chunk.get("done"):
                        usage_seen = True
                        record_token_usage(label, backend, chunk,
                                           model=payload.get("model"),
                                           local=_is_local_base_url(endpoint))
                    msg = chunk.get("message", {}) or {}
                    if thinking_fragments(msg):
                        continue
                    delta_text = msg.get("content", "") or ""
                    if delta_text:
                        stream_text.append(delta_text)
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
                    if chunk.get("usage"):
                        usage_seen = True
                        record_token_usage(label, backend, chunk,
                                           model=payload.get("model"),
                                           local=_is_local_base_url(endpoint))
                    delta = ((chunk.get("choices") or [{}])[0].get("delta") or {})
                    delta_text = delta.get("content", "") or ""
                    tool_fragments = delta.get("tool_calls") or []
                    if delta_text:
                        stream_text.append(delta_text)
                    if not delta_text and not tool_fragments:
                        continue
                    if first_token_ms is None and delta_text:
                        first_token_ms = (time.time() - start) * 1000
                    yield {"delta": delta_text,
                           "tool_calls": delta.get("tool_calls") or [],
                           "first_token_ms": first_token_ms,
                           "ms_done": None}
    if not usage_seen and stream_text:
        record_token_usage(label, backend, None, model=payload.get("model"),
                           local=_is_local_base_url(endpoint),
                           prompt_text=_messages_text(messages),
                           response_text="".join(stream_text))


_RETRYABLE_STREAM_STATUS_RE = re.compile(r"HTTP 5\d\d")


async def stream_chat(ctx: RoleContext, messages: list,
                      label: str = "") -> AsyncGenerator[Dict, None]:
    base_url = str(ctx.get("llm_base_url", "http://127.0.0.1:11434")).rstrip("/")
    backend = ctx.get("llm_backend", "ollama")
    for attempt in range(2):
        produced = False
        try:
            async for chunk in _stream_chat_inner(ctx, messages, label):
                produced = True
                yield chunk
            return
        except httpx.ConnectError as e:
            if attempt == 0 and not produced:
                await asyncio.sleep(1.2)
                continue
            raise ConnectionError(conn_fail_hint(base_url, base_url, backend)) from e
        except httpx.TimeoutException as e:
            raise ConnectionError(f"请求 LLM 服务超时：{base_url}。若模型加载较慢可调大 llm_timeout。") from e
        except RuntimeError as e:
            if attempt == 0 and not produced and _RETRYABLE_STREAM_STATUS_RE.search(str(e)):
                print(f"LLM 流式请求服务端出错，1.5 秒后重试一次：{e}")
                await asyncio.sleep(1.5)
                continue
            raise


def _merge_openai_tool_fragments(frags: list) -> list:
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

_KANA_RE = re.compile(r"[\u3041-\u30FF]")
_DISPLAY_SEG_SPLIT = re.compile(r"([。！？!?…]+|\n)")


def strip_other_language_from_display(display: str, display_lang: str, text_lang: str) -> str:
    text = str(display or "")
    if not text:
        return text
    dl, tl = str(display_lang or "").lower(), str(text_lang or "").lower()
    if not dl or dl == "auto" or dl == tl:
        return text
    if "zh" not in dl or not _KANA_RE.search(text):
        return text
    parts = _DISPLAY_SEG_SPLIT.split(text)
    kept = []
    prev_dropped = False
    prev_kept = False
    for seg in parts:
        if not seg:
            continue
        if _KANA_RE.search(seg):
            lead = re.match(r"^[」”』〉》]", seg) if prev_kept else None
            if lead:
                kept.append(lead.group(0))
            prev_dropped = True
            prev_kept = False
            continue
        if prev_dropped and not real_text(seg):
            prev_dropped = False
            continue
        prev_dropped = False
        prev_kept = True
        kept.append(seg)
    return "".join(kept).strip()


def enforce_display_language(sentences, ctx) -> None:
    if not ctx or not bool(ctx.get("display_pure_language", True)):
        return
    display_lang = str(ctx.get("display_lang", "zh") or "zh")
    text_lang = str(ctx.get("text_lang", "ja") or "ja")
    for s in sentences or []:
        if not isinstance(s, dict):
            continue
        display = str(s.get("display") or "")
        if not display:
            continue
        cleaned = strip_other_language_from_display(display, display_lang, text_lang)
        if cleaned != display:
            s["display"] = cleaned


def display_text_of(sentence: dict, ctx) -> str:
    display = str(sentence.get("display") or "")
    if display:
        return display
    zh = str(sentence.get("zh") or "")
    if not zh:
        return ""
    display_lang = str((ctx or {}).get("display_lang", "zh") or "zh")
    return "" if lang_text_broken(zh, display_lang) else zh


MENTION_PLACEHOLDER = "{at}"

POKE_MESSAGE_TEXT = "[戳了戳你]"

DELIVERY_CHARS = "chars"
DELIVERY_PLAIN = "plain"
DELIVERY_MODES = (DELIVERY_CHARS, DELIVERY_PLAIN)

SENTENCE_ACTION_KEYS = ("reply_to", "mention_ids", "poke", "delivery", "recall",
                        "recall_other", "mute", "mute_duration",
                        "sulk", "sulk_seconds", "followup_seconds")

_ACTION_LABELS = {
    "reply_to": "引用本条消息",
    "mention_ids": "@人",
    "poke": "戳一戳",
    "delivery": "指定发送形态",
    "recall": "撤回",
    "recall_other": "撤回别人的消息",
    "mute": "禁言",
    "mute_duration": "禁言时长",
    "sulk": "冷处理（晾他）",
    "sulk_seconds": "冷处理时长",
    "followup_seconds": "没回话就先开口",
}


def sentence_actions_note(sentences) -> str:
    declared = {}
    for sentence in sentences or []:
        if not isinstance(sentence, dict):
            continue
        for key in SENTENCE_ACTION_KEYS:
            value = sentence.get(key)
            if value and key not in declared:
                declared[key] = value
    parts = []
    for key in SENTENCE_ACTION_KEYS:
        if key not in declared:
            continue
        if key in ("mute_duration", "sulk_seconds"):
            continue
        label = _ACTION_LABELS.get(key, key)
        if key == "mention_ids":
            label += f"（{declared[key]}）"
        elif key == "delivery":
            label += f"（{declared[key]}）"
        parts.append(label)
    return "、".join(parts)


_ACTION_FAIL_REASONS = {
    "no_client": "发送通道未就绪",
    "disabled": "这个动作已关闭",
    "unsupported": "这条接入方式不支持",
    "denied": "没有权限",
    "no_target": "没听清要禁言谁",
    "target_owner": "对方是群主，禁言不了",
    "error": "发送失败",
}


def action_receipts_note(receipts, speaker_labels: Optional[Dict] = None) -> str:
    labels = speaker_labels or {}
    parts = []
    for receipt in receipts or []:
        if not isinstance(receipt, dict):
            continue
        action = str(receipt.get("action") or "动作")
        target = str(receipt.get("target") or "")
        if target:
            label = str(labels.get(target) or "")
            action += f"（对象：{label}）" if label else f"（对象：QQ:{target}）"
        if receipt.get("ok"):
            text = f"{action}：成功"
            count = receipt.get("count")
            if isinstance(count, int) and count > 0:
                text += f"（{count} 条）"
        else:
            reason = _ACTION_FAIL_REASONS.get(str(receipt.get("reason") or ""), "失败")
            text = f"{action}：失败（{reason}）"
        parts.append(text)
    return "、".join(parts)


_POKE_ACTION_WRAPPER_RE = re.compile(
    r"^[\s\u3000（）()【】\[\]「」『』“”‘’'\"]+|[\s\u3000（）()【】\[\]「」『』“”‘’'\"]+$")
_POKE_ACTION_TEXT_RE = re.compile(
    r"^(?:轻轻地|偷偷地|悄悄地|用力地|随手|突然)?戳(?:了|一)?戳?"
    r"(?:你|主人|他|她|对方|回去|回来)"
    r"(?:一下|两下|几下)?(?:的[\u4e00-\u9fa5]{1,3})?$")
_POKE_ACTION_INLINE_RE = re.compile(
    r"[（(【\[]\s*(?:轻轻地|偷偷地|悄悄地|用力地|随手|突然)?戳(?:了|一)?戳?"
    r"(?:你|主人|他|她|对方|回去|回来)(?:一下|两下|几下)?(?:的[\u4e00-\u9fa5]{1,3})?\s*[）)】\]]")
_STAGE_SPAN_RE = re.compile(r"[（(【\[][^）)】\]]*[）)】\]]")
_POKE_STAGE_ACTION_RE = re.compile(
    r"(?:戳|弹|敲)(?:了|一|下|过去|回去|回来)?[^\u3002！？，；\n]{0,6}?"
    r"(?:你|主人|他|她|对方|回去|回来)"
    r"|(?:你|主人|他|她|对方)[^\u3002！？，；\n]{0,6}?"
    r"(?:戳|弹|敲)(?:了|一|下|过去|回去|回来)")

_STAGE_HYPOTHETICAL_RE = re.compile(r"要是|如果|假如|真想|恨不得|差点|本来想|不然|否则|再敢")
_STAGE_ACTION_RULES = (
    ("recall_other", re.compile(
        r"(?:撤回|收回|撤销|撤掉)[^\u3002！？，；\n]{0,4}?(?:你|主人|他|她|别人|对方)")),
    ("recall", re.compile(r"撤回|收回|撤销|撤掉")),
    ("mute", re.compile(r"禁言")),
    ("reply_to", re.compile(r"引用")),
    ("delivery", re.compile(r"一个字一条|一字一条|逐字|一个字一个字")),
)


def detect_stage_actions(text) -> dict:
    out = {}
    for span in _STAGE_SPAN_RE.finditer(str(text or "")):
        body = span.group(0)
        if _STAGE_HYPOTHETICAL_RE.search(body):
            continue
        for field, pattern in _STAGE_ACTION_RULES:
            if pattern.search(body):
                out[field] = True
        if out.get("recall_other"):
            out.pop("recall", None)
    return out


_CLAIMED_ACTION_RULES = (
    ("mute", re.compile(r"已(?:经)?(?:成功)?禁言"
                        r"|已(?:经)?(?:成功)?对[^\u3002！？\n]{0,20}?禁言"
                        r"|禁言(?:好|完)了")),
    ("poke", re.compile(r"已(?:经)?(?:成功)?戳(?:了|过|你|主人|他|她)"
                        r"|戳(?:了|过)(?:你|主人|他|她)"
                        r"|戳回去(?:啦|了)")),
    ("recall_other", re.compile(r"已(?:经)?(?:成功)?(?:撤回|收回|撤销|撤掉)"
                                r"[^\u3002！？\n]{0,6}?(?:你|主人|他|她|别人|对方)")),
)
_CLAIMED_CLAUSE_SPLIT = re.compile(r"[。！？\n]+")


def detect_claimed_actions(text) -> dict:
    out = {}
    for clause in _CLAIMED_CLAUSE_SPLIT.split(str(text or "")):
        if not clause.strip() or _STAGE_HYPOTHETICAL_RE.search(clause):
            continue
        for field, pattern in _CLAIMED_ACTION_RULES:
            if pattern.search(clause):
                out[field] = True
    return out


def strip_mention_placeholder(text) -> str:
    raw = str(text or "")
    if MENTION_PLACEHOLDER not in raw:
        return raw
    return re.sub(r"[ \t]{2,}", " ", raw.replace(MENTION_PLACEHOLDER, "")).strip(" \t")


_ACTION_FIELD_LINE_RE = re.compile(
    r"^[ \t]*(reply_to|mention_ids|poke|recall|recall_delay|recall_other|mute|mute_duration|delivery|sulk|sulk_seconds|followup_seconds)"
    r"[ \t]*[:：][ \t]*(.*)$")


def strip_action_field_lines(text) -> tuple:
    lines = str(text or "").split("\n")
    fields = {}
    idx = 0
    while idx < len(lines):
        hit = _ACTION_FIELD_LINE_RE.match(lines[idx])
        if not hit:
            break
        fields[hit.group(1)] = hit.group(2).strip()
        idx += 1
    if not fields:
        return text, {}
    return "\n".join(lines[idx:]).strip(), fields


def _field_truthy(value) -> bool:
    return str(value or "").strip().lower() in ("true", "1", "yes", "y", "on", "是", "开")


_SPEAKER_LABEL_RE = re.compile(r"\[\s*(?:角色|用户)\s*\d+\s*\]")
_STICKER_PLACEHOLDER_RE = re.compile(
    r"\[\s*(?:调用|使用|发送|插入)?\s*表情包(?:的)?"
    r"(?:翻译|译文|訳|调用|日[文本语]|英[文本语]|中[文本语])?\s*[:：][^\]\n]*\]")
_STICKER_NOTE_RE = re.compile(
    r"\[\s*[（(][^[\]（）()]{0,24}(?:表情包|贴图|翻译|译文|訳|sticker)[^[\]（）()]{0,24}[）)]\s*[:：]"
    r"[^\]\n]{0,80}\]?")


def strip_model_artifacts(text) -> str:
    raw = str(text or "")
    if not raw:
        return raw
    out = _SPEAKER_LABEL_RE.sub(" ", raw)
    out = _STICKER_PLACEHOLDER_RE.sub(" ", out)
    out = _STICKER_NOTE_RE.sub(" ", out)
    if out == raw:
        return raw
    return re.sub(r"[ \t]{2,}", " ", out).strip()


_PER_CHAR_SEP_RE = re.compile(r"[、,，\n\r]+")


def collapse_per_char_separators(text) -> str:
    raw = str(text or "")
    if not raw.strip():
        return raw
    chunks = _PER_CHAR_SEP_RE.split(raw.strip())
    if len(chunks) < 4:
        return raw
    if sum(1 for c in chunks if len(c) == 1) < len(chunks) - 1:
        return raw
    return "".join(chunks)


def strip_poke_action(text) -> tuple:
    raw = str(text or "")
    if not raw:
        return raw, False
    inline = _POKE_ACTION_INLINE_RE.sub("", raw).strip()
    if inline != raw:
        return inline, True
    if _POKE_ACTION_TEXT_RE.match(_POKE_ACTION_WRAPPER_RE.sub("", raw)):
        return "", True
    for span in _STAGE_SPAN_RE.finditer(raw):
        body = span.group(0)
        if _STAGE_HYPOTHETICAL_RE.search(body):
            continue
        if _POKE_STAGE_ACTION_RE.search(body):
            return raw, True
    return raw, False


def merge_sentence_actions(sources) -> dict:
    out = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in SENTENCE_ACTION_KEYS:
            if key in source and key not in out:
                out[key] = source[key]
    return out


_MENTION_TEXT_QQ_RE = re.compile(r"@@\s*(\d{3,12})")
_MENTION_ALL_RE = re.compile(r"@@\s*(?:所有人|全体成员|全员|全体)")
_MENTION_TEXT_QQ_ONE_RE = re.compile(r"(?<!@)@(\d{3,12})(?!\.)")
# OneBot 里 @全体成员就是 at 段的 qq=all
MENTION_ALL_ID = "all"


def _literal_mention_target(text: str, allowed: List[str], names: Dict[str, str],
                            fallback_id: str):
    hit = _MENTION_TEXT_QQ_RE.search(text)
    if hit:
        return (hit.group(1), (hit.start(), hit.end())) if hit.group(1) in allowed \
            else ("", None)
    hit_one = _MENTION_TEXT_QQ_ONE_RE.search(text)
    if hit_one:
        return (hit_one.group(1), hit_one.span()) if hit_one.group(1) in allowed \
            else ("", None)
    for qq, name in names.items():
        if qq not in allowed:
            continue
        pos = text.find(f"@@{name}")
        if pos >= 0:
            return qq, (pos, pos + len(name) + 2)
    hit_all = _MENTION_ALL_RE.search(text)
    if hit_all:
        return MENTION_ALL_ID, hit_all.span()
    pos = text.find("@@")
    if pos >= 0:
        target = _mention_fallback(allowed, fallback_id)
        return (target, (pos, pos + 2)) if target else ("", None)
    return "", None


def _mention_fallback(allowed: List[str], fallback_id: str) -> str:
    if fallback_id and fallback_id in allowed:
        return fallback_id
    others = [q for q in allowed if q != MENTION_ALL_ID]
    return others[0] if len(others) == 1 else ""


def apply_literal_mention(sentence: dict, allowed_ids, names=None,
                          fallback_id: str = "") -> bool:
    if not isinstance(sentence, dict):
        return False
    allowed = [str(q).strip() for q in (allowed_ids or []) if str(q).strip()]
    if not allowed:
        return False
    name_map = {str(q).strip(): str(n).strip()
                for q, n in (names or {}).items() if str(n).strip()}
    raw_ids = sentence.get("mention_ids")
    ids = [str(x).strip() for x in raw_ids if str(x).strip()] \
        if isinstance(raw_ids, list) else []
    fallback = str(fallback_id or "").strip()
    need_target = not ids
    changed = False
    for field in ("display", "zh"):
        text = str(sentence.get(field) or "")
        if not text:
            continue
        if MENTION_PLACEHOLDER in text:
            if not need_target:
                continue
            target, span = _mention_fallback(allowed, fallback), None
        elif "@@" in text or _MENTION_TEXT_QQ_ONE_RE.search(text):
            target, span = _literal_mention_target(text, allowed, name_map, fallback)
        else:
            continue
        if not target:
            continue
        if span is not None:
            sentence[field] = f"{text[:span[0]]}{MENTION_PLACEHOLDER}{text[span[1]:]}"
            changed = True
        if target not in ids:
            ids.append(target)
        need_target = False
    if ids and ids != raw_ids:
        sentence["mention_ids"] = ids
        changed = True
    return changed


_RECALL_VERB = r"(?:撤回|撤掉|撤销|撤了|删掉|删除)"
_RECALL_ASK_RE = re.compile(
    rf"(?:会不会|会|能|可以|能不能|可不可以|是不是|怎么|如何|为什么|为啥)"
    rf"[^。！？!?]{{0,8}}{_RECALL_VERB}")
_RECALL_LATE_RE = re.compile(
    rf"(?:还没|还没有|没有|怎么还|仍然|依然|仍旧)[^。！？!?]{{0,6}}{_RECALL_VERB}")
_RECALL_DENY_RE = re.compile(
    rf"(?:不要|不用|无需|不许|不准|拒绝|别|不)[^。！？!?]{{0,4}}{_RECALL_VERB}")
_RECALL_SELF_RE = re.compile(rf"我(?:自己|已经|刚刚|刚|也|先)?{_RECALL_VERB}")
_RECALL_WANT_RE = re.compile(
    rf"{_RECALL_VERB}[^。！？!?]{{0,8}}"
    rf"(?:这|那|刚才|刚刚|上一|上面|你|它|吧|呀|啊|呢|哦|嘛|一下|一条|两条|消息|话|句|条)"
    rf"|把[^。！？!?]{{0,12}}{_RECALL_VERB}")
_RECALL_NEXT_RE = re.compile(
    rf"(?:下一条|下一句|下一个|接下来|后面|待会|等下)[^。！？!?]{{0,6}}{_RECALL_VERB}"
    rf"|{_RECALL_VERB}[^。！？!?]{{0,8}}(?:下一条|下一句|接下来)")


def recall_request_kind(text) -> str:
    raw = _compact_at_spans(text).strip()
    if not raw:
        return ""
    if _RECALL_LATE_RE.search(raw):
        return "prev"
    if _RECALL_ASK_RE.search(raw) or _RECALL_DENY_RE.search(raw) \
            or _RECALL_SELF_RE.search(raw):
        return ""
    next_hit = _RECALL_NEXT_RE.search(raw)
    if next_hit:
        return "next"
    return "prev" if _RECALL_WANT_RE.search(raw) else ""


_AT_SPAN_RE = re.compile(r"\[@[^\[\]]{1,64}\]")


def _compact_at_spans(text) -> str:
    return _AT_SPAN_RE.sub("@", str(text or ""))


_MUTE_VERB = r"禁言"
_MUTE_ASK_RE = re.compile(
    r"(?:会不会|会|能|可以|能不能|可不可以|是不是|该不该|要不要|怎么|如何|为什么|为啥)"
    rf"[^。！？!?]{{0,8}}{_MUTE_VERB}"
    rf"|你(?:要|会|敢|想|能|可以|打算|准备)[^。！？!?]{{0,4}}{_MUTE_VERB}")
_MUTE_DENY_RE = re.compile(
    rf"(?:不要|不用|无需|不许|不准|拒绝|别)[^。！？!?]{{0,4}}{_MUTE_VERB}"
    rf"|不(?:要|用|必|准|许|需|应|再|想)?[^。！？!?]{{0,2}}{_MUTE_VERB}")
_MUTE_WANT_RE = re.compile(
    rf"(?:把|给|替)[^。！？!?]{{0,8}}{_MUTE_VERB}"
    rf"|{_MUTE_VERB}\s*(?:@|\[@)"
    rf"|^[^。！？!?\n]{{0,4}}{_MUTE_VERB}[了啊吧呀哦～~！!]{{0,2}}$"
    rf"|^[^。！？!?\n]{{0,4}}{_MUTE_VERB}(?:他|她|它|我|本座)[了啊吧呀哦～~]?$"
    rf"|^[^。！？!?\n]{{0,8}}{_MUTE_VERB}(?:他|她|它|我|本座)?\s*\d+\s*(?:个?小时|分钟|分|秒|天)$")
_MUTE_SELF_RE = re.compile(
    rf"{_MUTE_VERB}(?:我|本座|自己)|把(?:我|本座|自己)[^。！？!?]{{0,4}}{_MUTE_VERB}")
_MUTE_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(个?小时|分钟|分|秒|天)")
_MUTE_UNIT_SECONDS = {"小时": 3600, "分钟": 60, "分": 60, "秒": 1, "天": 86400}


def wants_mute_request(text) -> bool:
    raw = _compact_at_spans(text).strip()
    if not raw:
        return False
    if _MUTE_ASK_RE.search(raw) or _MUTE_DENY_RE.search(raw):
        return False
    return bool(_MUTE_WANT_RE.search(raw))


def wants_self_mute(text) -> bool:
    raw = _compact_at_spans(text)
    return bool(wants_mute_request(raw) and _MUTE_SELF_RE.search(raw))


def mute_request_seconds(text) -> int:
    match = _MUTE_DURATION_RE.search(str(text or ""))
    if not match:
        return 0
    try:
        amount = float(match.group(1))
    except (TypeError, ValueError):
        return 0
    return int(amount * _MUTE_UNIT_SECONDS.get(match.group(2).lstrip("个"), 0))


def normalize_single(obj, ctx: RoleContext, emotions: dict, user_text: str) -> Dict:
    s = obj if isinstance(obj, dict) else {"zh": str(obj)}
    text_lang = ctx.get("text_lang", "ja")
    display_lang = ctx.get("display_lang", "zh")
    default_voice = ctx.get("default_voice", "pingjing")
    zh = strip_model_artifacts(str(s.get("zh", "")).strip())
    zh, leaked_fields = strip_action_field_lines(zh)
    raw_lang = strip_model_artifacts(str(s.get(text_lang, "")).strip())
    raw_lang, leaked_lang = strip_action_field_lines(raw_lang)
    raw_display = strip_model_artifacts(str(s.get(display_lang, "")).strip())
    raw_display, leaked_display = strip_action_field_lines(raw_display)
    leaked_fields.update(leaked_lang)
    leaked_fields.update(leaked_display)
    zh = collapse_per_char_separators(zh)
    raw_lang = collapse_per_char_separators(raw_lang)
    raw_display = collapse_per_char_separators(raw_display)
    if not zh:
        if sentence_obj_has_action(s) or leaked_fields:
            zh = ""
        else:
            _report_fallback("这一句既没有台词也没有动作", s)
            zh = raw_lang or FALLBACK_REPLY
    lang = strip_urls_for_tts(raw_lang)
    if not lang:
        lang = strip_urls_for_tts(zh)
    if display_lang == "auto":
        display = raw_lang or zh
    else:
        display = raw_display or zh
    if display and bool(ctx.get("display_pure_language", True)):
        display = strip_other_language_from_display(display, display_lang, text_lang)
    emo = str(s.get("emotion", "")).strip()
    if not ctx.get("llm_judge", True) or not emo:
        emo = default_voice
    else:
        resolved = resolve_emotion_key(emo, emotions)
        if not resolved:
            resolved = resolve_emotion_from_text(zh or lang, emotions)
            _warn_unknown_emotion(emo, resolved or default_voice, emotions)
        emo = resolved or default_voice
    mimic = _resolve_mimic_key(s.get("mimic", ""), available_mimics(ctx)) \
        if ctx.get("llm_judge", True) else ""
    zh = apply_text_clean(zh, ctx)
    lang = apply_text_clean(lang, ctx)
    display = apply_text_clean(display, ctx)
    lang = strip_mention_placeholder(lang)
    stage_actions = detect_stage_actions(zh)
    stage_actions.update(detect_stage_actions(lang))
    stage_actions.update(detect_stage_actions(display))
    for _text in (zh, lang, display):
        for _field in detect_claimed_actions(_text):
            stage_actions.setdefault(_field, True)
    poke_in_text = False
    if _cfg_bool(ctx, "poke_enabled", True):
        zh, hit_zh = strip_poke_action(zh)
        lang, hit_lang = strip_poke_action(lang)
        display, hit_display = strip_poke_action(display)
        poke_in_text = hit_zh or hit_lang or hit_display
        if poke_in_text and not zh.strip() and not display.strip():
            zh = lang = display = ""
    normalized = {"zh": zh, "lang": lang, "display": display, "emotion": emo, "mimic": mimic}
    if s.get("reply_to") is True or stage_actions.get("reply_to") \
            or _field_truthy(leaked_fields.get("reply_to")):
        normalized["reply_to"] = True
    if s.get("poke") is True or poke_in_text or _field_truthy(leaked_fields.get("poke")):
        normalized["poke"] = True
    delivery = str(s.get("delivery") or leaked_fields.get("delivery") or "").strip().lower()
    if delivery not in DELIVERY_MODES and stage_actions.get("delivery"):
        delivery = DELIVERY_CHARS
    if delivery in DELIVERY_MODES:
        normalized["delivery"] = delivery
    recall_in_text = bool(stage_actions.get("recall")) and _cfg_bool(ctx, "recall_enabled", True)
    recall_other_in_text = bool(stage_actions.get("recall_other")) \
        and _cfg_bool(ctx, "recall_enabled", True)
    if s.get("recall") is True or recall_in_text or _field_truthy(leaked_fields.get("recall")):
        normalized["recall"] = True
    if s.get("recall_other") is True or recall_other_in_text \
            or _field_truthy(leaked_fields.get("recall_other")):
        normalized["recall_other"] = True
        normalized.pop("recall", None)
    raw_mute = s.get("mute")
    if raw_mute in (None, ""):
        raw_mute = leaked_fields.get("mute")
    if raw_mute is True or _field_truthy(raw_mute):
        normalized["mute"] = True
    elif str(raw_mute or "").strip().isdigit():
        normalized["mute"] = str(raw_mute).strip()
    elif stage_actions.get("mute") and _cfg_bool(ctx, "mute_enabled", True):
        normalized["mute"] = True
    raw_duration = s.get("mute_duration")
    if raw_duration in (None, ""):
        raw_duration = leaked_fields.get("mute_duration")
    try:
        seconds = int(float(str(raw_duration).strip()))
    except (TypeError, ValueError):
        seconds = 0
    if seconds > 0:
        normalized["mute_duration"] = seconds
    raw_sulk = s.get("sulk")
    if raw_sulk in (None, ""):
        raw_sulk = leaked_fields.get("sulk")
    if raw_sulk is True or _field_truthy(raw_sulk):
        normalized["sulk"] = True
        raw_sulk_seconds = s.get("sulk_seconds")
        if raw_sulk_seconds in (None, ""):
            raw_sulk_seconds = leaked_fields.get("sulk_seconds")
        try:
            sulk_seconds = int(float(str(raw_sulk_seconds).strip()))
        except (TypeError, ValueError):
            sulk_seconds = 0
        if sulk_seconds > 0:
            normalized["sulk_seconds"] = sulk_seconds
    raw_followup = s.get("followup_seconds")
    if raw_followup in (None, ""):
        raw_followup = leaked_fields.get("followup_seconds")
    try:
        followup_seconds = int(float(str(raw_followup).strip()))
    except (TypeError, ValueError):
        # followup_seconds: true 这类布尔写法 → 用配置的默认时长
        followup_seconds = int(_cfg_num(ctx, "followup_default_seconds", 120) or 0) \
            if _field_truthy(raw_followup) else 0
    if followup_seconds > 0:
        normalized["followup_seconds"] = followup_seconds
    mentions = s.get("mention_ids")
    if isinstance(mentions, list):
        normalized["mention_ids"] = [str(item).strip() for item in mentions
                                     if str(item).strip()]
    return normalized


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


def split_multi_clause_sentences(sentences: List[Dict], ctx=None) -> List[Dict]:
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
                     "emotion": s.get("emotion", ""),
                     "mimic": s.get("mimic", "")}
            out.append(piece)
    merged = _merge_short_sentences(out)
    enforce_display_language(merged, ctx)
    return merged


FALLBACK_REPLY = "呜……刚才走神了，主人再说一遍好吗？"


def _report_fallback(reason: str, raw) -> None:
    if not isinstance(raw, str):
        try:
            raw = json.dumps(raw, ensure_ascii=False)
        except (TypeError, ValueError):
            raw = str(raw)
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    print(f"回复兜底（{reason}）：模型原始输出 {len(text)} 字 → {text[:200]!r}")

ERROR_REPLY_TEXT = "呜……我这边刚才出了点小状况，那句话没能说完整。主人稍后再跟我说一遍好吗？"
ERROR_REPLY_HINTS = (
    ("quota", "模型额度不足"),
    ("401", "模型密钥不对"),
    ("403", "模型拒绝了这次请求"),
    ("429", "请求太频繁了"),
    ("timeout", "模型响应超时"),
    ("timed out", "模型响应超时"),
    ("connect", "连不上模型服务"),
)


def error_reply_text(error) -> str:
    detail = str(error or "").lower()
    hint = next((text for key, text in ERROR_REPLY_HINTS if key in detail), "")
    return f"{ERROR_REPLY_TEXT}（{hint}）" if hint else ERROR_REPLY_TEXT


_TERMINALS = set("。？！；?!")
_TRAILING_TERMINALS = set("。？！；?!…～~")
_LEADING_PAUSE_PUNCT = "，,、 \t\r\n"


def split_terms(text: str) -> List[str]:
    text = str(text or "")
    if "http://" in text or "https://" in text:
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
    merged: List[str] = []
    for piece in out:
        if piece.strip() and not real_text(piece):
            if merged:
                merged[-1] += piece
                continue
            if len(out) > 1:
                continue
        merged.append(piece)
    return [(p.lstrip(_LEADING_PAUSE_PUNCT) or p) for p in merged if p.strip()]


def _piece_weights(pieces: List[str]) -> List[int]:
    return [max(1, len(real_text(p))) for p in pieces]


def _group_bounds(weights: List[int], groups: int) -> List[int]:
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
    if groups <= 0:
        return list(pieces)
    return [p for p in _group_by_bounds(pieces, _group_bounds(weights, groups)) if p]


def _display_piece(zh_piece: str, lang_piece: str) -> str:
    return "" if real_text(zh_piece) == real_text(lang_piece) else zh_piece


def align_bilingual_parts(zh_parts: List[str], lang_parts: List[str],
                          display_parts: Optional[List[str]] = None) -> tuple:
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


def segment_for_tts(sentences: List[Dict], ctx=None) -> List[Dict]:
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
        mimic = s.get("mimic", "")
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
                "mimic": mimic,
            })
    enforce_display_language(result, ctx)
    return result


def _drop_bilingual_duplicates(sentences: List[dict], text_lang: str) -> List[dict]:
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


def has_structured_sentences(content: str) -> bool:
    objs = extract_json_objects(strip_model_artifacts(content or ""))
    if any(isinstance(o, dict) and isinstance(o.get("sentences"), list) and o["sentences"]
           for o in objs):
        return True
    return bool([o for o in objs if _is_sentence_like(o)])


def normalize_sentences(content: str, ctx: RoleContext, emotions: dict, user_text: str) -> List[Dict]:
    default_voice = ctx.get("default_voice", "pingjing")
    content = strip_model_artifacts(content)
    raw = strip_thinking(content or "")
    objs = extract_json_objects(content)

    sentences = None
    wrapper = next((o for o in objs if isinstance(o, dict)
                    and isinstance(o.get("sentences"), list) and o["sentences"]), None)
    if wrapper is not None:
        sentences = list(wrapper["sentences"]) + [
            o for o in objs if o is not wrapper and _is_sentence_like(o)]
    else:
        sentence_like = [o for o in objs if _is_sentence_like(o)]
        if sentence_like:
            sentences = sentence_like

    if sentences is not None:
        sentences = [{"zh": s} if isinstance(s, str) else s for s in sentences]
        sentences = [s for s in sentences
                     if isinstance(s, dict)
                     and (sentence_obj_has_text(s) or sentence_obj_has_action(s))]
        if not sentences:
            sentences = None

    if sentences is None:
        first = next((o for o in objs if isinstance(o, dict)), None)
        if first is not None and (first.get("sentences") is not None
                                  or any(k in first for k in _SENTENCE_KEYS)
                                  or sentence_obj_has_action(first)):
            s = first.get("sentences")
            if isinstance(s, str) and s.strip():
                sentences = [{"zh": s}]
            else:
                zh = str(first.get("zh", "") or "").strip()
                if not zh:
                    if sentence_obj_has_action(first):
                        sentences = [dict(first)]
                    else:
                        _report_fallback("模型输出里没有台词内容", content)
                        sentences = [{"zh": FALLBACK_REPLY, "lang": FALLBACK_REPLY,
                                      "display": FALLBACK_REPLY, "emotion": default_voice}]
                else:
                    sentences = [{"zh": zh, "emotion": first.get("emotion", default_voice)}]
        else:
            if not raw:
                raw = "出错了。"
            elif _looks_like_json(raw):
                _report_fallback("模型输出形似 JSON，但里面没有可识别的句子", content)
                raw = FALLBACK_REPLY
            sentences = [{"zh": raw, "lang": raw, "display": raw, "emotion": default_voice}]
    text_lang = str(ctx.get("text_lang", "ja") or "ja")
    sentences = _drop_bilingual_duplicates(sentences, text_lang)
    sentences = _drop_repeated_lang_blocks(sentences, text_lang)
    normalized = [normalize_single(s, ctx, emotions, user_text) for s in sentences]
    normalized = [s for s in normalized if real_text(s.get("zh")) or real_text(s.get("display"))]
    result = _merge_short_sentences(split_multi_clause_sentences(normalized, ctx))
    # 归一化结果放最前：动作键以清洗后的值为准（原始句里的秒数可能是字符串/布尔）
    meta_sources = [wrapper] if isinstance(wrapper, dict) else []
    meta_sources.extend(normalized)
    meta_sources.extend(s for s in sentences if isinstance(s, dict))
    result_meta = merge_sentence_actions(meta_sources)
    if not result and (result_meta.get("poke") is True
                       or result_meta.get("recall_other") is True
                       or result_meta.get("sulk") is True
                       or bool(result_meta.get("mute"))):
        # 否则动作会跟着空句子一起被丢掉
        result = [{"zh": "", "lang": "", "display": "", "emotion": "", "mimic": ""}]
    if result:
        result[0]["reply_to"] = result_meta.get("reply_to") is True
        if result_meta.get("poke") is True:
            result[0]["poke"] = True
        if result_meta.get("recall") is True:
            result[0]["recall"] = True
        if result_meta.get("recall_other") is True:
            result[0]["recall_other"] = True
        if result_meta.get("mute"):
            result[0]["mute"] = result_meta["mute"]
            seconds = result_meta.get("mute_duration")
            if isinstance(seconds, int) and seconds > 0:
                result[0]["mute_duration"] = seconds
        if result_meta.get("sulk") is True:
            result[0]["sulk"] = True
            sulk_seconds = result_meta.get("sulk_seconds")
            if isinstance(sulk_seconds, int) and not isinstance(sulk_seconds, bool) \
                    and sulk_seconds > 0:
                result[0]["sulk_seconds"] = sulk_seconds
        followup_seconds = result_meta.get("followup_seconds")
        if isinstance(followup_seconds, int) and not isinstance(followup_seconds, bool) \
                and followup_seconds > 0:
            result[0]["followup_seconds"] = followup_seconds
        delivery = str(result_meta.get("delivery") or "").strip().lower()
        if delivery in DELIVERY_MODES:
            result[0]["delivery"] = delivery
        mentions = result_meta.get("mention_ids")
        if isinstance(mentions, list):
            ids = [str(item).strip() for item in mentions if str(item).strip()]
            owner = next((s for s in result
                          if MENTION_PLACEHOLDER in str(s.get("display") or s.get("zh") or "")),
                         None)
            if ids:
                (owner if owner is not None else result[0])["mention_ids"] = ids
    return result


class SentenceStreamParser:

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
        if isinstance(obj, dict) and isinstance(obj.get("sentences"), list):
            if self.yielded == 0:
                inner = [s for s in obj["sentences"] if isinstance(s, dict)]
                self.yielded += len(inner)
                return inner
            return []  # 内部块早已逐个产出
        if _is_sentence_like(obj):
            self.yielded += 1
            return [obj]
        return []

    def _scan(self) -> List[Dict]:
        buf = self.buffer
        if self.in_array:
            return self._scan_array(buf)
        m = self._SENT_KEY.search(buf, max(0, self.scan_pos - 24))
        if m and (self.obj_start is None or m.start() >= self.obj_start):
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
        cand = [s for s in cand if str(s.get("zh", "")).strip()]
        if not cand:
            return []
        return split_multi_clause_sentences(
            [normalize_single(s, ctx, emotions, user_text) for s in cand], ctx)


# ---------------------------------------------------------------------------
# 工具调用消息规整（修 400 Bad Request）
# ---------------------------------------------------------------------------
# Ollama 会把 function.arguments 当成 JSON 对象解析：
#   - 空字符串 / 非法 JSON  → 400 "Value looks like object, but can't find closing '}'"
#   - tool_calls 传成字符串 → 400 "cannot unmarshal string into ... []api.ToolCall"

def _looks_like_url_arg(text: str) -> bool:
    return bool(re.match(r"^(?:https?://|www\.)\S+$", text, re.I))


def _extract_first_url(text: str) -> str:
    match = re.search(r"https?://[^\s<>\"'）)】]+", str(text or ""), re.IGNORECASE)
    return match.group(0).rstrip(".,!?。，！？") if match else ""


def _parse_arguments(arguments):
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
        return ({"url": text}, False) if _looks_like_url_arg(text) else ({}, True)
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = _loads_lenient(text)
    if isinstance(parsed, dict):
        return parsed, False
    if parsed is None:
        if _looks_like_url_arg(text):
            return {"url": text}, False
        return {}, True
    return {"value": parsed}, False


def _coerce_arguments_object(arguments):
    return _parse_arguments(arguments)[0]


def normalize_message_tool_calls(message: dict) -> dict:
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
# ---------------------------------------------------------------------------
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

_TIME_SENSITIVE_NEED_PATTERN = (
    r"兑换码|激活码|礼包码|前瞻|爆料|卡池|祈愿|复刻|返场|"
    r"开服|停服|停机|公告|版本号|赛季|赛程|比分|战报|"
    r"汇率|股价|票价|开售|预售|定档|上映|排期|开奖|榜单|"
    r"几月几号|什么时候上线|什么时候开(?:服|池|播)|截止|结束时间"
)

_TOOL_NEED_RE = re.compile(
    r"几点|几号|几时|星期几|周几|礼拜几|日期|今天是|现在时间|当前时间|"
    r"算一下|计算|等于多少|多少等于|换算|百分之|打折|涨了|降了|"
    r"天气|气温|温度|下雨|降雨|带伞|雨伞|下雪|降雪|台风|冷不冷|热不热|多少度|几度|"
    + _SEARCH_INTENT_PATTERN + "|" + _TIME_SENSITIVE_NEED_PATTERN +
    r"|谷歌|百度|bing|"
    r"是谁|是什么|什么是|什么叫|听说过|了解吗|认识吗|知道吗|怎么回事|哪里人|"
    r"最新|新闻|时事|进展|近况|价格|多少钱|多少钱|攻略|教程|推荐|评价|评测|"
    r"版本|更新|发售|上线|下载|官网|网址|链接|地址|在哪|怎么走|"
    r"odds|weather|forecast|temperature|search|who is|what is", re.I)
_TOOL_NEED_PREFIX_RE = re.compile(r"^(?:你|妳|您|主人|人家|本座|咱|俺)?(?:能不能|可以|能|可以|会)")
_PURE_CHATTER_RE = re.compile(
    r"^(?:嗯+|哦+|好+|行|是|对|在吗|在么|在不在|早安|早上好|午安|晚安|晚上好|"
    r"你好|您好|哈喽|hi|hello|嗨|抱抱|摸摸|亲亲|贴贴|么么|爱你|喜欢你|想你|"
    r"哈哈+|嘿嘿+|嘻嘻+|呜呜+|嘤嘤+|嘤|呜|喵+|汪+|草|靠|啧|唉|哎|"
    r"ok|OK|okay|yes|no|thanks|thank you|thx)[!！。.~～、，,\s]*$")
_PURE_EMOTION_RE = re.compile(
    r"^(?:我)?(?:好|太|超|非常|真的)?(?:累|困|饿|开心|高兴|难过|伤心|无聊|"
    r"烦|烦死|郁闷|emo|爽|幸福|寂寞|孤独|疼|痛|冷|热|生气|气死)"
    r"(?:了|啦|啊|呀|哦|呢|死|爆|的|得很)?[!！。.~～\s]*$", re.I)
_SELF_CONTEXT_RE = re.compile(
    r"你的(?:系统)?(?:提示词|设定|人设|身份|指令)|系统提示词|prompt|"
    r"你是什么(?:模型|AI|程序|机器人)|你(?:用的是|用的)?什么模型|"
    r"(?:我们|咱俩)?(?:最初|最开始|一开始|开头|第一句|第一条|之前|先前|刚才|刚刚|上次)?"
    r"(?:聊|说|谈|讲)(?:了|过|的)?什么|"
    r"你还?记得|记不记得|你忘了", re.I)


def asks_self_context(user_text: str) -> bool:
    return bool(_SELF_CONTEXT_RE.search(strip_quote_note(str(user_text or ""))))


def text_needs_tools(user_text: str, tool_names=None) -> bool:
    text = strip_quote_note(str(user_text or "")).strip()
    if not text:
        return False
    if "://" in text or "www." in text.lower():
        return True
    if asks_self_context(text) and not _SEARCH_INTENT_RE.search(text):
        return False
    if _TOOL_NEED_RE.search(text):
        return True
    if _TOOL_NEED_PREFIX_RE.search(text) and "?" in text + "？":
        return True
    names = {str(n).lower() for n in (tool_names or []) if n}
    if "get_current_time" in names and re.search(r"\d{1,2}\s*[点时:：]", text):
        return True
    return False


def tool_flow_can_skip(user_text: str, tool_names=None) -> bool:
    text = strip_quote_note(str(user_text or "")).strip()
    if not text:
        return False
    if text_needs_tools(text, tool_names):
        return False
    return bool(_PURE_CHATTER_RE.match(text) or _PURE_EMOTION_RE.match(text))


_SPEAKER_TAG_RE = re.compile(r"^\s*\[(?:用户|角色)\d+\]\s*")


def strip_speaker_tags(text: str) -> str:
    s = str(text or "")
    while True:
        stripped = _SPEAKER_TAG_RE.sub("", s)
        if stripped == s:
            return s
        s = stripped


def last_user_text(messages) -> str:
    for message in reversed(list(messages or [])):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = str(message.get("content", ""))
        if content.startswith("【"):
            continue
        return strip_speaker_tags(content)
    return ""


def _nearest_tool_call_id(out: list) -> str:
    for item in reversed(out):
        if not isinstance(item, dict) or item.get("role") != "assistant":
            continue
        for call in reversed(item.get("tool_calls") or []):
            cid = call.get("id") if isinstance(call, dict) else ""
            if cid:
                return str(cid)
        return ""
    return ""


def normalize_messages_for_backend(messages: list, backend: str) -> list:
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
                msg["tool_call_id"] = (_nearest_tool_call_id(out)
                                       or msg.get("tool_name") or "tool")
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
                                 user_id: str = "", call_counts: dict = None) -> list:
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
    limit = max(1, int(_cfg_num(ctx, "web_fetch_precheck_max", 2)))
    allowed, reason = tool_registry.check_permission(tool, user_id)
    if not allowed:
        print(f"含链接消息预抓取跳过: {reason}")
        return work

    fetched, failed = [], []
    for index, url in enumerate(urls[:limit]):
        ok, output = await tool_registry.execute("web_fetch", {"url": url}, user_id,
                                                 call_counts=call_counts)
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
        work.append({"role": "tool", "tool_call_id": call_id, "content": str(output)})

    if fetched:
        note = ("用户消息里带了链接，其中 " + "、".join(fetched)
                + " 的网页内容已经通过 web_fetch 抓取并附在上面的工具结果里，"
                  "请直接依据这些内容回答，不要再重复抓取同一个链接。")
    else:
        note = ("用户消息里带了链接，但自动抓取失败：" + "；".join(f"{u}（{r}）" for u, r in failed)
                + "。请如实说明抓取失败，不要凭空编造网页内容。")
    work.append({"role": "user", "content": note})
    return work


_UNCERTAIN_MARKERS = (
    "不知道", "没听说过", "没听过", "没有听说过", "不了解", "不太清楚", "不清楚",
    "无从得知", "无法确定", "没有相关资料", "没有资料", "查无", "不认识", "没印象",
    "知らない", "知りません", "聞いたことがない", "聞いたことない",
    "わからない", "わかりません", "存じません", "不明です",
)


def _answer_admits_unknown(text: str) -> bool:
    t = str(text or "")
    return any(m in t for m in _UNCERTAIN_MARKERS)


_PROMISE_LOOKUP_RE = re.compile(
    r"(?:我|人家|本座|咱)(?:这就|马上|立刻|现在|先)?(?:去|来)?(?:帮你|给你|替主人|为主人)?"
    r"(?:查|搜|找)(?!了|过)"
    r"|(?:等我|让我|稍等(?:一下)?|等一下)(?:一下)?[，,。！!～~\s]*(?:我)?(?:去)?(?:查|搜|找)(?!了|过)"
    r"|(?:查|搜)(?:一下|一查|一搜)(?:再|就|马上|稍后|等会|待会)(?:告诉|说|回复)"
)


def _reply_promises_lookup(text: str) -> bool:
    return bool(_PROMISE_LOOKUP_RE.search(str(text or "")))


def _is_entity_question(text: str) -> bool:
    t = str(text or "").lower()
    return any(k in t for k in ("是谁", "是什么", "什么是", "什么叫", "听说过",
                                "知道", "了解", "认识", "怎么回事"))


def _search_query_from_question(text: str) -> str:
    q = strip_quote_note(text).strip()
    while True:
        stripped = re.sub(r"^(请问一下|请问|你知道|你知道一下|你晓得|帮我看看|帮我查查|帮我查一下|帮我查|帮我|麻烦|什么是|什么叫)\s*",
                          "", q)
        if stripped == q:
            break
        q = stripped
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


_SEARCH_NEGATION_RE = re.compile(
    r"(?:为什么|为啥|怎么|咋|谁让你|谁叫你|何必|干嘛|不用|不必|不要|别|不准|不许|少|又)"
    r"[^，。！!？?；;\n]{0,4}$")


_FRESH_INFO_RE = re.compile(_TIME_SENSITIVE_NEED_PATTERN)


def _needs_fresh_lookup(text: str) -> bool:
    body = strip_quote_note(str(text or ""))
    if not body or asks_self_context(body):
        return False
    return bool(_FRESH_INFO_RE.search(body))


def _is_search_request(text: str) -> bool:
    t = str(text or "")
    if not _SEARCH_INTENT_RE.search(t):
        return False
    for m in _SEARCH_INTENT_RE.finditer(t):
        prefix = t[max(0, m.start() - 12):m.start()]
        if _SEARCH_NEGATION_RE.search(prefix):
            continue
        return True
    return False


_NEGATION_CLAUSE_RE = re.compile(
    r"^(?:我|人家|咱|俺)?(?:不想|不要|不看|不搜|不查|不找|不用|不想看|"
    r"别看|别搜|别查|别找|别用|没看|没搜)")
_DESIRE_PREFIX_RE = re.compile(r"^(?:我|人家|咱|俺)(?:想要?|要|想|要看|想看|打算|准备)?看?")


def _search_target_from_desire(q: str) -> str:
    s = str(q or "").strip()
    if not s:
        return s
    if not re.search(r"[，,。！!？?、\n]", s):
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


_QUOTE_NOTE_RE = re.compile(r"^（回复 .*? 的消息：.*?）\n?")


def strip_quote_note(text: str) -> str:
    return _QUOTE_NOTE_RE.sub("", str(text or ""), count=1)


def _search_query_from_command(text: str) -> str:
    q = strip_quote_note(text).strip()
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
    out = []
    for raw in _URL_IN_TEXT_RE.findall(str(text or "")):
        url = raw.rstrip(".,;:!?。，、；：！？)]}】")
        if url and url not in out:
            out.append(url)
    return out


def drop_seen_entries(output, seen_urls) -> str:
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
    t = strip_quote_note(str(text or "")).strip()
    if not t or len(t) > 120:
        return False
    return bool(_SEARCH_DISSATISFIED_RE.search(t))


is_search_request = _is_search_request
is_search_dissatisfied = _is_search_dissatisfied


def _refine_query_from_feedback(prev_query: str, user_text: str) -> str:
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


_QDATE_RE = re.compile(
    r"(?:19|20)\d{2}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?)?"
    r"|(?:19|20)\d{2}[-/.]\d{1,2}(?:[-/.]\d{1,2})?"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*日")


def _search_args_text(arguments) -> str:
    if not isinstance(arguments, dict):
        return ""
    parts = []
    for key in ("query", "queries"):
        value = arguments.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(str(x) for x in value if isinstance(x, str))
    return " ".join(parts)


def _same_search_topic(a: str, b: str) -> bool:
    na = re.sub(r"[\s\u3000]+", "", _QDATE_RE.sub("", str(a or "")))
    nb = re.sub(r"[\s\u3000]+", "", _QDATE_RE.sub("", str(b or "")))
    if not na or not nb:
        return False
    if na == nb:
        return True
    if na in nb or nb in na:
        return abs(len(na) - len(nb)) <= 4
    return False


def _drop_invented_dates(query: str, user_text: str) -> str:
    q = str(query or "")
    u = str(user_text or "")
    if not q:
        return q
    if not re.search(r"(?:19|20)\d{2}", u):
        q = re.sub(r"(?:19|20)\d{2}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?)?", " ", q)
        q = re.sub(r"(?:19|20)\d{2}\s*[-/.]\s*\d{1,2}(?:\s*[-/.]\s*\d{1,2})?", " ", q)
    if not re.search(r"\d{1,2}\s*月", u):
        q = re.sub(r"\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?", " ", q)
    return re.sub(r"\s+", " ", q).strip(" 　，,。.、:：;；")


def _drop_invented_dates_in_args(arguments: dict, user_text: str) -> dict:
    if not isinstance(arguments, dict):
        return arguments
    out = dict(arguments)
    if isinstance(out.get("query"), str):
        out["query"] = _drop_invented_dates(out["query"], user_text)
    if isinstance(out.get("queries"), list):
        cleaned = [_drop_invented_dates(q, user_text) if isinstance(q, str) else q
                   for q in out["queries"]]
        out["queries"] = [q for q in cleaned if q] or out["queries"]
    return out


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
            user_text = strip_speaker_tags(strip_quote_note(str(message.get("content", ""))))
            break
    key = search_state_key(session_key, user_id)
    state = search_state(key)
    prev_query = str(state.get("query", "") or "")
    explicit = _is_search_request(user_text)
    retry_feedback = bool(prev_query) and _is_search_dissatisfied(user_text)
    implicit = not explicit and not retry_feedback and _needs_fresh_lookup(user_text)
    if not explicit and not retry_feedback and not implicit:
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
        rule_query = (_search_query_from_command(user_text) if explicit
                      else _search_query_from_question(user_text))
        if not (2 <= len(rule_query) <= 40):
            return []
        print(f"[搜索预取] {'用户明确要求搜索' if explicit else '消息需要最新资料'}"
              f"，直接检索：{rule_query!r}")
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
        cleaned = _drop_invented_dates(query, user_text)
        if cleaned != query:
            print(f"[搜索预取] 搜索词里的日期是模型自己加的，已去掉：{query!r} → {cleaned!r}")
            query = cleaned or query
        if _SUBJECTIVE_SEARCH_RE.search(query) or _is_roleplay_search(query, ctx):
            print(f"[搜索预取] 拦截角色扮演/主观类搜索：{query!r}")
            ok = True
            output = ("这是角色扮演 / 主观互动类问题，答案由你以角色身份当场演绎，"
                      "网上查不到也不需要查；请直接以角色身份回应，不要引用任何搜索结果。")
            work.append({"role": "assistant", "content": "",
                         "tool_calls": [{"id": "prefetch_search", "type": "function",
                                         "function": {"name": "web_search",
                                                      "arguments": {"query": query}}}]})
            work.append({"role": "tool", "tool_call_id": "prefetch_search",
                         "content": output})
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
    work.append({"role": "tool", "tool_call_id": "prefetch_search", "content": str(output)})
    if ok and retry_feedback:
        work.append({"role": "user", "content":
                     f"用户反馈上一次搜索结果不对，系统已用新搜索词「{query}」重新检索，"
                     "结果就在上面的工具消息里。请依据这次的新内容回答，"
                     "并明确告诉用户这次换成了什么；已经有过的链接不要再发，"
                     "结果里带的新链接要原样写进回复。"})
    elif ok:
        if implicit:
            work.append({"role": "user", "content":
                         f"这条消息问的是有时效性的内容，系统已替你搜索「{query}」，"
                         "结果就在上面的工具消息里。请直接依据结果回答："
                         "把与用户所问相关的具体内容（最新的那条、时间、版本、号码等）"
                         "用角色自己的语气讲出来；结果里没有的信息不要编造，"
                         "本轮不要再调用 web_search（已经搜过了），"
                         "也不要只回一句“我去查一下”。"})
        else:
            work.append({"role": "user", "content":
                         f"用户明确要求搜索「{query}」，系统已替你完成搜索，结果就在上面的工具消息里，"
                         "请直接依据结果回答，不要再重复搜索同一内容。"})
    else:
        if implicit:
            work.append({"role": "user", "content":
                         f"系统替你搜索「{query}」失败：{str(output)[:200]}。"
                         "请如实说明这次没查到，不要编造结果。"})
        else:
            work.append({"role": "user", "content":
                         f"用户明确要求搜索「{query}」，但自动搜索失败了：{str(output)[:200]}。"
                         "请如实告诉用户本次没搜到，不要编造结果。"})
    return [{"name": "web_search", "arguments": {"query": query}, "ok": ok, "output": output}]


async def chat_with_tools(ctx: RoleContext, messages: list, tool_registry,
                          stats=None, user_id: str = "", session_key: str = "",
                          label: str = "") -> Dict:
    tools_schema = tool_registry.get_schema()
    trace = []
    call_counts = {}
    total_ms = 0.0
    max_iter = max(1, int(_cfg_num(ctx, "tools_max_iterations", 3)))
    user_text = last_user_text(messages)
    tool_names = [str(t.get("function", {}).get("name", ""))
                  for t in (tools_schema or []) if t.get("function")]
    mode = str(ctx.get("tools_trigger_mode", "keyword") or "keyword").strip().lower()
    allow_skip = mode in ("llm", "llm_auto", "auto")
    if allow_skip and tool_flow_can_skip(user_text, tool_names):
        result = await chat_once(ctx, list(messages), label=label)
        if stats:
            stats.record_llm(result["ms"])
        return {"content": result.get("content") or "", "tool_trace": [],
                "ms": result.get("ms", 0.0), "llm_calls": 1}
    work = await _prefetch_message_urls(ctx, list(messages), tool_registry, user_id,
                                        call_counts=call_counts)
    prefetch_trace = await _prefetch_search(ctx, work, tool_registry, user_id,
                                            call_counts=call_counts, session_key=session_key)
    trace.extend(prefetch_trace)
    prefetched_queries = [str(t.get("arguments", {}).get("query", ""))
                          for t in prefetch_trace
                          if t.get("name") == "web_search" and t.get("ok")]
    if tools_schema:
        tool_names_text = "、".join(
            str(t.get("function", {}).get("name", ""))
            for t in tools_schema if t.get("function", {}).get("name"))
        work.append({"role": "user", "content":
            f"【工具使用规则】以下内容是系统给你的规则，不是用户发来的消息。"
            f"本次对话已为你开启以下工具：{tool_names_text}。"
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
            "只发与用户所问直接相关、确实包含答案的链接：只匹配日期或年份的"
            "百科页、日历页、导航页一律不要发；结果里带检索时间，旧版本的"
            "攻略/兑换码不要当成本期答案。"
            "⑪ 【只在必要时调用】工具不是聊天的一部分：纯寒暄"
            "（你好、早安、晚安、在吗、谢谢）与纯情绪消息"
            "（好累、好开心、难过、想你、抱抱）没有任何客观信息需求，"
            "一个工具都不要调用，直接以角色身份说话；"
            "消息里没有客观信息需求时同样一个工具都不要调用。"
            "严禁“顺便查一下时间”“顺便搜一下”这类没有用户请求的调用；"
            "同一条消息里能用一个工具解决的，不要连开多个；"
            "⑫ 【地点不许猜】查询天气等与地点有关的信息时，只能使用"
            "【当前这一条用户消息里真的出现过的地点】；"
            "用户没说地点、你也不知道主人在哪时，绝对不许自己填一个城市"
            "（严禁默认上海/北京等任何城市），"
            "而是要直接用角色身份问一句主人在哪个城市。"
            "⑬ 【谈身份要查资料】讨论具体作者/作品/角色的设定、剧情、百科、"
            "版本等客观资料时，可以调用搜索；但只是以自己的角色身份闲聊、"
            "表达情绪、调情、撒娇时，永远不要调用工具。"
            "以上是系统规则，不是用户说的话：不要复述、不要确认、不要回应规则本身，"
            "直接回答用户上一条消息。"})
    final_content = ""
    executed_calls = set()
    llm_calls = 0
    for iteration in range(max_iter):
        result = await chat_once(ctx, work, tools=tools_schema, label=label)
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
            elif call["name"] == "web_search" and prefetched_queries and any(
                    _same_search_topic(_search_args_text(call["arguments"]), pq)
                    for pq in prefetched_queries):
                executed_calls.add(signature)
                ok, output = True, ("本轮系统已经替你搜索过同一内容（结果就在上面的工具消息里），"
                                    "不要重复搜索；请直接依据已有结果回答，"
                                    "结果里没有的就如实说明缺什么。")
                print(f"[工具流程] 与预取重复的搜索已拦截：{_search_args_text(call['arguments'])!r}")
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
                    call["arguments"] = _drop_invented_dates_in_args(call["arguments"], user_text)
                    arguments = call["arguments"]
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
                "⑥ 只回答用户本条最新消息的话题，不要转移去回应更早对话里的提醒或其他话题；"
                "⑦ 结果里混着多个主体（不同游戏 / 产品 / 人物）时，只采用与用户所问主体一致的内容；"
                "用户没说是哪一个、结果里主体又不一致时，先问一句是哪一个，"
                "绝不能拿别的主体的内容当答案；"
                "⑧ 回答里提到的主体名必须和用户所问一致，不确定就称“这款游戏 / 这个产品”，"
                "不要换成别的名字，也不要为了圆话编造联动、设定或代码；"
                "兑换码/激活码只能是字母数字串，结果里没有写出的码一律不能给，如实说没查到即可。"})
        final_content = result["content"]
    else:
        print(f"[工具流程] {max_iter} 轮内未产出最终回答，追加一轮无工具强制回答。")
        result = await chat_once(ctx, work, label=label)
        total_ms += result["ms"]
        llm_calls += 1
        if stats:
            stats.record_llm(result["ms"])
        final_content = result["content"]
    print(f"[工具流程] 本轮完成：共 {len(trace)} 次工具调用，"
          f"最终回答 {len(str(final_content or '').strip())} 字，总耗时 {total_ms:.0f}ms")

    search_done = any(t.get("name") == "web_search" and t.get("ok") for t in trace)
    if tools_schema and search_done and str(final_content or "").strip() \
            and _reply_promises_lookup(final_content):
        print("[工具流程] 已有工具结果却只答应去查，追加一轮要求当场回答。")
        work.append({"role": "user", "content":
                     "【直接回答】工具结果已经在上面的工具消息里，不要再答应去查、也不要再说稍等。"
                     "现在就用角色自己的语气把结果里的具体内容（数字、时间、名称、链接等）讲给用户。"})
        result = await chat_once(ctx, work, label=label)
        total_ms += result["ms"]
        llm_calls += 1
        if stats:
            stats.record_llm(result["ms"])
        final_content = result["content"]
        print(f"[工具流程] 追加作答后 {len(str(final_content or '').strip())} 字")

    if tools_schema and not trace and str(final_content or "").strip():
        user_question = ""
        for m in reversed(messages):
            if m.get("role") == "user" and not str(m.get("content", "")).startswith("【"):
                user_question = str(m.get("content", ""))
                break
        search_tool = next((t for t in tools_schema
                            if str(t.get("function", {}).get("name", "")) == "web_search"), None)
        query = _search_query_from_question(user_question)
        admits = _answer_admits_unknown(final_content) and _is_entity_question(user_question)
        promised = text_needs_tools(user_question) and _reply_promises_lookup(final_content)
        if search_tool and (admits or promised) and 2 <= len(query) <= 30 \
                and not asks_self_context(user_question) \
                and not _SUBJECTIVE_SEARCH_RE.search(query) \
                and not _is_roleplay_search(query, ctx):
            reason = "模型只答应去查、并没有真的查" if promised else "模型自称不确定"
            print(f"[工具流程] {reason}，强制补搜：{query!r}")
            ok, output = await tool_registry.execute("web_search", {"query": query}, user_id,
                                                     call_counts=call_counts,
                                                     user_text=user_question)
            trace.append({"name": "web_search", "arguments": {"query": query},
                          "ok": ok, "output": output})
            if ok:
                work.append({"role": "user", "content":
                    f"【补查结果】{reason}，系统已替你搜索「{query}」，结果如下：\n"
                    f"{str(output)[:2500]}\n"
                    "请依据以上真实结果重新回答用户的问题：查到了就把相关信息讲给用户；"
                    "确实查不到再如实说明。不要再只说去查、也不要说不知道。"})
                result = await chat_once(ctx, work, label=label)
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
                              max_tokens: int = 512, label: str = "") -> str:
    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}]
    try:
        result = await chat_once(ctx, messages, label=label)
        return strip_thinking(result.get("content") or "")
    except Exception as e:
        print(f"文本生成失败: {type(e).__name__}: {e}")
        return ""


async def generate_json_reply(ctx: RoleContext, system_prompt: str, user_prompt: str,
                              max_tokens: int = 512, label: str = "") -> Optional[dict]:
    text = await generate_text_reply(ctx, system_prompt, user_prompt, max_tokens, label=label)
    return extract_json(text)


async def vision_chat_once(ctx, prompt_text: str, images: list, *,
                           system_content: str = "", history: list = None,
                           stats=None, max_tokens: int = 1024, label: str = "") -> tuple:
    model = str(ctx.get("image_caption_model_name", "") or "").strip()
    if not model:
        raise RuntimeError("未配置识图模型名称，无法识图")
    backend = ctx.get("image_caption_backend", "") or ctx.get("llm_backend", "ollama")
    base_url = str(ctx.get("image_caption_base_url", "") or "").strip() \
        or str(ctx.get("llm_base_url", "http://127.0.0.1:11434"))
    base_url = base_url.rstrip("/")
    timeout = _cfg_num(ctx, "image_caption_timeout", 90)
    messages = []
    if system_content:
        messages.append({"role": "system", "content": system_content})
    messages.extend(history or [])
    payload_messages = list(messages)
    if backend == "ollama":
        payload_messages.append({"role": "user", "content": prompt_text,
                                 "images": [b64 for _src, _mime, b64 in images]})
        payload = {"model": model, "messages": payload_messages, "stream": False, "think": False,
                   "options": {"temperature": _cfg_num(ctx, "temperature", 0.7),
                               "num_predict": max_tokens}}
        endpoint = chat_endpoint(base_url, "ollama")
        headers = {}
    else:
        content_parts = []
        for source, mime, img_b64 in images:
            if str(source or "").startswith(("http://", "https://")):
                content_parts.append({"type": "image_url", "image_url": {"url": source}})
            else:
                content_parts.append({"type": "image_url",
                                      "image_url": {"url": f"data:{mime};base64,{img_b64}"}})
        content_parts.append({"type": "text", "text": prompt_text})
        payload_messages.append({"role": "user", "content": content_parts})
        payload = {"model": model, "messages": payload_messages, "stream": False,
                   "temperature": _cfg_num(ctx, "temperature", 0.7),
                   "max_tokens": max_tokens}
        endpoint = chat_endpoint(base_url, "openai")
        api_key = ctx.get("image_caption_api_key", "") or ctx.get("llm_api_key", "")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    start = time.time()
    async with httpx.AsyncClient(timeout=timeout, verify=verified_context()) as client:
        resp = await client.post(endpoint, json=payload, headers=headers)
        if resp.status_code >= 400:
            detail = clean_error_body(resp.text)
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code} {endpoint}：{detail}"
                + http_error_hint(resp.status_code, detail),
                request=resp.request, response=resp)
        data = resp.json()
    record_token_usage(label, backend, data, model=payload.get("model"),
                       local=_is_local_base_url(base_url))
    ms = (time.time() - start) * 1000
    if backend == "ollama":
        content = data.get("message", {}).get("content", "")
    else:
        content = data["choices"][0]["message"]["content"]
    if stats:
        stats.record_llm(ms)
    return strip_thinking(content or ""), ms


async def get_image_reply(ctx: RoleContext, user_text: str, history: list,
                          emotions: dict, image_urls: list,
                          extra_parts=None, stats=None,
                          describe_only: bool = False,
                          speaker_labels: Optional[Dict] = None) -> Optional[Dict]:
    try:
        if describe_only:
            prompt_text = (
                "用户发来了一张图片。本轮角色不会回复，你只需要看图。\n"
                "只输出一个 JSON 对象，字段为 description"
                "（一句中文客观描述画面实际内容，只陈述事实）；"
                "不要输出 sentences，不要写任何角色台词。\n"
                f"用户附加文字：{user_text}"
            )
        else:
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
            prompt_text += _sticker_capture_instruction(ctx, describe_only)
        if describe_only:
            prompt_text += (
                "\n要求：description 除客观画面内容外，还要写清人物表情神态"
                "（如脸红、害羞、惊讶、生气、无语等）与整体氛围，不要只写构图和画面文字。"
            )
        else:
            prompt_text += (
                "\n要求："
                "1) sentences 的 zh 要贴合当前这张图的具体内容来写，不要用套话；"
                "若你刚才回复过相似内容，绝不能重复上一句的句子，要换一种完全不同的说法。"
                "2) ja 必须是 zh 的地道日文翻译（含义与语气完全一致，不逐字硬译，不夹带中文）。"
                "3) description 除客观画面内容外，还要写清人物表情神态"
                "（如脸红、害羞、惊讶、生气、无语等）与整体氛围，不要只写构图和画面文字。"
            )
        history_msgs = build_merged_history(history, ctx, speaker_labels)
        images_for_payload = []  # [(source, mime, base64)]
        raw_for_capture = None
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
                print(f"跳过非图片内容（链接可能已过期或被拦截）: {src[:120]}")
                continue
            original = data
            data, mime = normalize_image_data(data, mime)
            if not data:
                print(f"图片格式转换失败，已跳过: {src[:120]}")
                continue
            images_for_payload.append((src, mime, base64_b64(data)))
            if raw_for_capture is None:
                raw_for_capture = {"source": src, "data": original}
        if not images_for_payload:
            print("没有有效的图片数据，使用默认回复")
            if describe_only:
                return {"sentences": [], "ms": 0, "tool_trace": []}
            default_text = "啊嘞，看不清这张图呢。"
            return {"sentences": normalize_sentences(default_text, ctx, emotions, user_text), "ms": 0, "tool_trace": []}
        model = ctx.get("image_caption_model_name", "")
        if not model:
            print("未配置识图模型名称，无法处理图片")
            return None
        system_content = build_system_prompt(ctx, emotions, extra_parts)
        if _cfg_bool(ctx, "image_identity_guard_enabled", True):
            prompt_text = f"{prompt_text}\n{image_identity_note(ctx)}"
        content, ms = await vision_chat_once(
            ctx, prompt_text, images_for_payload, system_content=system_content,
            history=history_msgs, stats=stats, label="识图")

        sentences = [] if describe_only else \
            normalize_sentences(content, ctx, emotions, user_text or "（图片）")
        description = extract_image_description(content or "")
        if not describe_only and len(sentences) == 1 \
                and "走神了" in str(sentences[0].get("zh", "") or "") and description:
            try:
                regen_text = f"{user_text or '（看图）'}\n（用户发来一张图片，画面内容：{description}）"
                chat = await chat_once(ctx, build_chat_messages(ctx, regen_text, history, emotions, extra_parts),
                                       label="识图补回复")
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
        print(f"\n【表情收藏-LMM原始输出】\n{content}\n【原始输出结束】")

        capture = extract_sticker_capture(content or "")
        if not sticker_capture_allowed(ctx, content or ""):
            if capture is not None:
                print("【表情收藏】本次不收藏（判定未通过）。")
            capture = None
        elif capture is None:
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
            if raw_for_capture:
                result["capture_image"] = raw_for_capture
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


async def translate_to_lang(ctx, text: str, target: str, label: str = "") -> str:
    body = str(text or "").strip()
    target = str(target or "").strip().lower()
    if not body or not target or target == "auto":
        return ""
    if not lang_text_broken(body, target):
        return body
    lang_label = _lang_label(target)
    try:
        res = await chat_once(ctx, [{"role": "user", "content":
            f"把这句话翻译成自然的{lang_label}，只输出译文本身，不要解释、不要罗马音，"
            f"整句都用{lang_label}书写，不要保留原语言的词汇：{body}"}], label=label)
        cand = str(res.get("content") or "").strip().strip('"“”‘’「」')
        if cand and not lang_text_broken(cand, target):
            return cand
        print(f"该句{lang_label}重译不可用（{cand[:30]!r}），保留原文本。")
    except Exception as e:
        print(f"{target} 台词翻译失败，保留原文本: {type(e).__name__}: {e}")
    return ""


_REPAIR_TIME_BUDGET = 20.0


def _same_text(a, b) -> bool:
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
    lang = str(s.get("lang") or "").strip()
    if not lang or not lang_text_broken(lang, target):
        return False
    if target in ("ja", "jp") and not _KANA_RE.search(lang):
        zh = str(s.get("zh") or "").strip()
        if zh and not _same_text(lang, zh):
            return False
    return True


async def repair_sentence_lang(sentences, ctx) -> int:
    from .tts import strip_non_dialogue_text
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
        source = strip_mention_placeholder(strip_non_dialogue_text(
            str(s.get("zh") or "").strip() or lang, log=False))
        cand = await translate_to_lang(ctx, source, target, label="台词翻译")
        if cand:
            cand = strip_mention_placeholder(cand)
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


def os_path_exists(path) -> bool:
    import os
    return os.path.exists(path)


def base64_b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode('utf-8')


_IMAGE_MAGICS = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
)


def sniff_image_mime(data: bytes) -> str:
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
                headers.pop("Referer", None)
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                return resp.content
            raise

def normalize_image_data(data: bytes, mime: str):
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
