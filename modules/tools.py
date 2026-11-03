import ast
import asyncio
import ipaddress
import json
import locale
import math
import operator
import os
import re
import shlex
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse

import httpx

from .tls import verified_context

DEFAULT_TOOLS = [
    {
        "name": "get_current_time",
        "type": "builtin", "builtin": "time",
        "description": "获取当前的日期、时间和星期几。【必须调用】只要用户消息涉及“现在几点”“今天几号”“星期几”“今年是哪一年”“现在是什么时候”等任何与当下时间/日期有关的问题，都必须调用本工具获取真实时间后再回答，严禁凭印象编造日期或时间。",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "enabled": False, "allowed_users": [], "max_calls_per_reply": 2,
    },
    {
        "name": "calculate",
        "type": "builtin", "builtin": "calculate",
        "description": "计算任意数学表达式。【必须调用】只要用户消息里出现了需要进行四则运算、百分比、乘方、开方、三角函数等数学计算的表述（例如“算一下”“等于多少”“多少钱”“打几折”“涨了多少”），都必须把用户问题里的数值与运算符原样抽出来构造 expression 调用本工具，再用结果作答，严禁心算或凭感觉给出数字。不要使用与用户问题无关的固定算式。",
        "parameters": {"type": "object", "properties": {"expression": {"type": "string", "description": "用户问题对应的数学表达式，按问题内容填写"}}, "required": ["expression"]},
        "enabled": False, "allowed_users": [], "max_calls_per_reply": 3,
    },
    {
        "name": "get_weather",
        "type": "builtin", "builtin": "weather",
        "description": "查询某个城市的当前天气。【必须调用】用户询问**明确地点**的天气、气温、温度、降雨、下雨、下雪、要不要带伞等与天气相关的话题时，必须调用本工具并把用户问题里的城市名原样传给 city，严禁凭常识或印象回答。city 只能填【用户本条消息里真的出现过的城市名】或下面【默认城市】里配置的城市；如果用户没说是哪里、而你也不知道对方在哪个城市，绝对不要自己猜（严禁拿上海/北京等任何城市顶替），此时不要调用本工具，直接以角色身份问一句对方在哪个城市。",
        "parameters": {"type": "object", "properties": {"city": {"type": "string", "description": "用户本条消息里明确说出的城市名（中文或拼音）；用户没说地点时留空，不要猜"}}, "required": []},
        "enabled": False, "allowed_users": [], "max_calls_per_reply": 2,
    },
    {
        "name": "web_fetch",
        "type": "builtin", "builtin": "web_fetch",
        "description": "打开一个已知网址并把网页正文读回来。只有当消息里（或上一步搜索/工具结果里）已经有具体网址时才用它，url 必须是那个网址。要按关键词找资料请用 web_search，本工具不接受搜索词。",
        "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "要打开的完整网址（http/https，可省略协议头）"}}, "required": ["url"]},
        "enabled": True, "allowed_users": [], "max_calls_per_reply": 2,
    },
    {
        "name": "web_search",
        "type": "builtin", "builtin": "web_search",
        "description": "用关键词在互联网上搜索资料。【必须调用】只要用户消息表达出以下任一意图，都必须调用本工具搜索后再回答，绝不能凭记忆、凭常识直接作答，也不能以“我没有联网”之类的借口推托：① 明确的搜索请求（搜/搜索/搜一下/查一下/查找/查查/帮我搜/帮我查/找一下/找找/看看有没有）；② 询问新闻、时事、最新消息、最新进展、近期发生的事；③ 询问具体人物/作品/产品/事件/名词/名词解释（如“xx是什么”“xx是谁”“xx怎么样”“xx有没有出”“你知道xx吗”）；④ 询问价格、评价、推荐、攻略、教程；⑤ 询问任何你不确定或不知道答案的问题（此时必须先搜再答，不确定就承认不确定，绝不能编造）；⑥ 用户只丢来一个话题或名词、没有完整问句（版本更新公告、活动兑换码、卡池、赛程比分这类时效性内容）时，同样要先搜索再回答。query 必须严格取自【当前这一条用户消息】的核心搜索意图，用最简洁的关键词表述（例如用户说“帮我搜搜今天有什么新闻呗”就填“今天的新闻”；用户说“A游戏好玩吗”就填“A游戏 评价”），绝对不允许复制、沿用或参考历史对话中曾经出现过的旧搜索词；结果里会带「检索时间」，拿不准新旧时以它为准，优先采用与检索时间接近、版本最新的那条，旧版本的攻略/兑换码不要当成本期答案；只把与用户所问直接相关、确实包含答案的链接写进回复，只匹配日期或年份的百科页、日历页、导航页不要发；结果或正文里混着多个主体（不同游戏/产品）时，只取与用户所问主体一致的部分，用户没说清是哪一个就先问一句，不要拿别的主体的兑换码当答案。结果末尾可能附有【网页正文】，那是系统替你打开页面抓回来的正文，回答时以正文里的内容为准（摘要可能只是标题党，正文里没有的码或数据不要编）。可选 engine 指定搜索引擎（bing/baidu/360/sogou 等，不填用默认引擎）；只有需要打开某个搜索结果看全文时，才接着用 web_fetch。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "当前这条用户消息对应的搜索关键词，必须只用这条消息里的信息，禁止使用历史对话中的旧搜索词"},
            "queries": {"type": "array", "items": {"type": "string"}, "description": "需要同时了解几件事时用的多个搜索词（最多 4 个）；一次把主题都列全，不要只搜第一个"},
            "engine": {"type": "string", "description": "可选：指定搜索引擎 key（如 bing/baidu/360/sogou），不填则用默认引擎"}
        }, "required": ["query"]},
        "enabled": True, "allowed_users": [], "max_calls_per_reply": 2,
    },
]

# ------------------------- 内置工具实现 -------------------------

_CALC_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.FloorDiv: operator.floordiv, ast.USub: operator.neg, ast.UAdd: operator.pos,
}
_CALC_FUNCS = {
    "sqrt": math.sqrt, "abs": abs, "round": round, "sin": math.sin,
    "cos": math.cos, "tan": math.tan, "log": math.log, "log10": math.log10,
    "pow": math.pow, "max": max, "min": min, "pi": math.pi, "e": math.e,
}


# 重复查询没有意义，直接复用结果
_TIME_TOOL_CACHE = {"ts": 0.0, "value": ""}

_CALC_OPERATOR_RE = re.compile(r"[+\-*/×÷^%(）)]|加|减|乘|除|平方|根号|开方|次方|百分比")


def _get_cached_time() -> str:
    now = time.time()
    if now - _TIME_TOOL_CACHE["ts"] < 60 and _TIME_TOOL_CACHE["value"]:
        print("[工具] 时间查询命中 60s 缓存，直接复用")
        return _TIME_TOOL_CACHE["value"]
    lt = time.localtime()
    try:
        week = "周" + "一二三四五六日"[lt.tm_wday % 7]
    except Exception:
        week = ""
    value = time.strftime(f"%Y-%m-%d %H:%M:%S {week}", lt)
    _TIME_TOOL_CACHE["ts"] = now
    _TIME_TOOL_CACHE["value"] = value
    return value


def _safe_calc(expression: str):
    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _CALC_OPS:
            return _CALC_OPS[type(node.op)](_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _CALC_OPS:
            return _CALC_OPS[type(node.op)](_eval(node.operand))
        if isinstance(node, ast.Name) and node.id in _CALC_FUNCS:
            return _CALC_FUNCS[node.id]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _CALC_FUNCS:
            args = [_eval(a) for a in node.args]
            return _CALC_FUNCS[node.func.id](*args)
        raise ValueError("不支持的表达式")
    node = ast.parse(str(expression), mode="eval")
    value = _eval(node)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return value


def _strip_html(text: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _blocked_host(host: str) -> bool:
    host = str(host or "").strip().lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addresses = {ipaddress.ip_address(item[4][0])
                         for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
        except OSError:
            return False
    return any(address.is_private or address.is_loopback or address.is_link_local
               or address.is_reserved or address.is_unspecified for address in addresses)


def _validate_url(url: str, allow_private: bool = False) -> str:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("仅允许访问 http/https URL")
    if not allow_private and _blocked_host(parsed.hostname):
        host = parsed.hostname
        resolved = ""
        try:
            resolved = socket.gethostbyname(host)
        except Exception:
            resolved = ""
        detail = f"{host} → {resolved}" if resolved else host
        raise ValueError(
            f"禁止访问本机、内网或保留地址（{detail}）。"
            "如果这是个正常的公网网址，多半是本机的 hosts 被网络加速/代理软件改写了"
            "（常见于 Steam 加速工具把域名指向 127.0.0.1），"
            "在该软件里关掉这个域名的加速即可正常访问")
    return parsed.geturl()


def _validate_redirect(response: httpx.Response):
    if response.next_request is not None:
        _validate_url(str(response.next_request.url))


def _pick_arg(args: dict, *keys) -> str:
    for k in keys:
        v = args.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def extract_urls(text: str, limit: int = 3) -> List[str]:
    out: List[str] = []
    for raw in re.findall(r"https?://[^\s<>\"'）)】\]]+", str(text or ""), re.I):
        url = raw.rstrip(".,;:!?。，、；：！？")
        if url and url not in out:
            out.append(url)
        if len(out) >= max(1, int(limit)):
            break
    return out


def _looks_like_url_arg(text: str) -> bool:
    text = str(text or "").strip()
    if not text or re.search(r"\s", text):
        return False
    if re.match(r"^(?:https?://|www\.)\S+$", text, re.I):
        return True
    return bool(re.match(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|cn|net|org|io|dev|ai|edu|gov|me|top|xyz|info|co)(?:[/:?#]\S*)?$",
                         text, re.I))


# ------------------------- 搜索引擎注册表 -------------------------
# 每个引擎一个 spec：
#   parse_json    JSON 响应 -> [{title, url, snippet}]
#   parse_html    HTML 响应 -> [{title, url, snippet}]
#   api_url       官方 API 地址
#   api_headers   官方 API 认证头

_ENTITIES = {
    "amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'", "nbsp": " ",
    "ensp": " ", "emsp": " ", "thinsp": " ", "middot": "·", "hellip": "…",
    "mdash": "—", "ndash": "–", "ldquo": "“", "rdquo": "”", "lsquo": "‘",
    "rsquo": "’", "copy": "©", "reg": "®", "trade": "™", "times": "×",
    "divide": "÷", "bull": "•", "deg": "°", "plusmn": "±", "sect": "§",
}


def _decode_entities(text: str) -> str:
    def _numeric(match):
        body = match.group(1)
        try:
            code = int(body[1:], 16) if body[:1].lower() == "x" else int(body)
            return chr(code) if 0 < code <= 0x10FFFF else match.group(0)
        except Exception:
            return match.group(0)

    text = re.sub(r"&#(x?[0-9A-Fa-f]+);", _numeric, str(text or ""))
    return re.sub(r"&([A-Za-z]+);",
                  lambda m: _ENTITIES.get(m.group(1).lower(), m.group(0)), text)


def _html_to_text(fragment: str) -> str:
    text = re.sub(r"<(script|style)[\s\S]*?</\1>", " ", str(fragment or ""), flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", _decode_entities(text)).strip()


def _attr_of(tag: str, name: str) -> str:
    match = re.search(r'\b' + re.escape(name) + r'\s*=\s*"([^"]*)"', str(tag or ""), re.I)
    if not match:
        match = re.search(r"\b" + re.escape(name) + r"\s*=\s*'([^']*)'", str(tag or ""), re.I)
    return _decode_entities(match.group(1)) if match else ""


def _abs_url(href: str, base: str) -> str:
    href = _decode_entities(str(href or "")).strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return ""
    try:
        return urljoin(base, href)
    except Exception:
        return ""


def _search_terms(query: str) -> List[str]:
    text = re.sub(r"[\s\u3000]+", " ", str(query or "")).strip().lower()
    terms = []
    for token in re.split(r"[\s,，、;；/|]+", text):
        if not token:
            continue
        if len(token) <= 4:
            if token not in terms:
                terms.append(token)
            continue
        for size in (4, 3, 2):
            for i in range(len(token) - size + 1):
                gram = token[i:i + size]
                if gram not in terms:
                    terms.append(gram)
    return terms


def _result_matches(item: dict, terms: List[str]) -> bool:
    haystack = f"{item.get('title', '')} {item.get('content', '')} {item.get('url', '')}".lower()
    return any(term in haystack for term in terms)


def _core_lookup(query: str) -> str:
    text = re.sub(r"[\s\u3000]+", " ", str(query or "")).strip()
    text = re.sub(r"^(帮我|请|麻烦|给我)?\s*(搜索|搜一下|搜搜|搜|查一下|查查|查|找一下|找找|找)\s*(一下|下|一哈)?\s*", "", text)
    text = re.sub(r"(是谁|是什么人|是什么|怎么样|如何|的资料|的介绍|的信息|的意思|在哪里|是哪里|吗|呢|？|\?)+$", "", text)
    text = text.strip(" 　，,。.、:：;；\"'“”")
    return text or str(query or "").strip()


def _is_short_lookup(query: str) -> bool:
    text = re.sub(r"[\s\u3000]+", "", str(query or ""))
    return 0 < len(text) <= 8


_RECENCY_QUERY_RE = re.compile(
    r"兑换码|激活码|礼包码|前瞻|预告|爆料|情报|卡池|祈愿|复刻|返场|"
    r"开服|停服|维护|停机|补偿|公告|版本|更新|赛季|赛程|比分|战报|"
    r"汇率|股价|票价|开售|预售|定档|上映|排期|开奖|榜单|"
    r"最新|新闻|时事|进展|近况|价格|多少钱|今天|本周|本月|今年")


_CODE_QUERY_RE = re.compile(r"兑换码|激活码|礼包码")
_LOOKUP_QUERY_RE = re.compile(r"兑换码|激活码|礼包码|公告|赛程|比分|汇率|股价|价格|多少钱|前瞻|预告")
_NO_FETCH_HOSTS = ("bilibili.com", "douyin.com", "iesdouyin.com", "youtube.com",
                   "youtu.be", "kuaishou.com", "weibo.com", "xiaohongshu.com")


def _with_freshness_hint(query: str) -> str:
    q = str(query or "").strip()
    if not q or not _RECENCY_QUERY_RE.search(q):
        return q
    if re.search(r"最新|今日|今天|刚刚|近期|本周|本月|今年", q):
        return q
    if re.search(r"(?:19|20)\d{2}|\d{1,2}\s*月", q):
        return q
    return f"{q} 最新"


_VERSION_CTX_RE = re.compile(r"版本|前瞻|卡池|兑换码|更新|上线|复刻|祈愿")
_VERSION_RE = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})(?![\d.])")
_VERSION_TOPIC_RE = re.compile(
    r"最新|兑换码|激活码|礼包码|前瞻|卡池|祈愿|复刻|返场|版本|更新|公告|攻略|"
    r"大全|汇总|整理|一览|什么时候|上线|开服|停机|维护|补偿|活动|礼包|福利|码")
_VERSION_CACHE = {}
_VERSION_CACHE_TTL = 3600.0


def _extract_version(text: str) -> str:
    lines = [ln for ln in str(text or "").splitlines() if ln.strip()]

    def best_from(needles) -> str:
        best = None
        for line in lines:
            if needles and not any(n in line for n in needles):
                continue
            for m in _VERSION_RE.finditer(line):
                before = line[m.start() - 1:m.start()]
                after = line[m.end():m.end() + 1]
                if before in "-~—－" or after in "-~—－." or after.isdigit():
                    continue
                major, minor = int(m.group(1)), int(m.group(2))
                if not (0 < major <= 20 and minor <= 30):
                    continue
                if best is None or (major, minor) > best:
                    best = (major, minor)
        return f"{best[0]}.{best[1]}" if best else ""

    return best_from(("前瞻",)) or best_from(("版本",))


_VERSION_SUBJECT_STOP_RE = re.compile(
    r"^(?:剩余|剩下|其他|其它|还有|这个|那个|这些|那些|刚才|上面|下面|之前|之后|"
    r"前面|后面|全部|所有|更多|新的|旧的|几个|一个|两个|三个|多少|哪个|什么|怎么|为什么|"
    r"我们|你们|他们|我的|你的|他的|她的|我们的|你们的|我|你|他|她|它)+$")


def _version_subject(query: str) -> str:
    s = re.sub(r"site:\S+", " ", str(query or ""), flags=re.I)
    parts = [p for p in re.split(r"[\s\u3000]+", s) if p]
    candidates = ([parts[0]] if parts else []) + [s]
    for cand in candidates:
        t = _VERSION_TOPIC_RE.sub(" ", cand)
        t = re.sub(r"(?<![\d.])\d{1,2}\.\d{1,2}(?![\d.])", " ", t)
        t = re.sub(r"[\s\u3000]+", " ", t).strip(" 　，,。.、:：;；呢吗啊呀吧")
        if not 2 <= len(t) <= 20:
            continue
        if _VERSION_SUBJECT_STOP_RE.match(t):
            continue
        if re.fullmatch(r"[\d一二三四五六七八九十百两几多余个只件种批套]+", t):
            continue
        return t
    return ""


def _version_filter(results: list, target: str) -> list:
    if not target:
        return results
    kept = []
    for item in results:
        text = f"{item.get('title', '')} {item.get('content', '')}"
        if target in text:
            kept.append(item)
    return kept

_DATE_IN_TEXT_RE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
                              r"|(20\d{2})-(\d{1,2})-(\d{1,2})")
_QUERY_DATE_RE = re.compile(
    r"(?:19|20)\d{2}\s*年(?:\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?)?"
    r"|(?:19|20)\d{2}[-/.]\d{1,2}(?:[-/.]\d{1,2})?"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"|今天|今日|最新")


def _result_matches_query(item: dict, query: str) -> bool:
    topic = _QUERY_DATE_RE.sub(" ", str(query or "")).strip() or str(query or "")
    core = re.sub(r"[\s\u3000]+", "", _core_lookup(topic)).lower()
    if not core:
        return True
    haystack = f"{item.get('title', '')}{item.get('content', '')}".lower().replace(" ", "")
    if _is_short_lookup(core):
        grams = {core[i:i + 2] for i in range(len(core) - 1)} if len(core) > 2 else {core}
        return core in haystack or sum(1 for gram in grams if gram in haystack) >= 2
    terms = _search_terms(topic)
    return not terms or _result_matches(item, terms)


_REL_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*(天|小时|分钟)\s*之前")
_MD_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[月/]\s*(\d{1,2})(?!\d)")


def _text_date_ts(text: str):
    s = str(text or "")
    m = _DATE_IN_TEXT_RE.search(s)
    if m:
        parts = [p for p in m.groups() if p]
        try:
            return time.mktime((int(parts[0]), int(parts[1]), int(parts[2]),
                                0, 0, 0, 0, 0, -1))
        except (ValueError, OverflowError):
            return None
    m = _REL_DATE_RE.search(s)
    if m:
        seconds = {"天": 86400, "小时": 3600, "分钟": 60}.get(m.group(2), 0)
        return time.time() - int(m.group(1)) * seconds
    m = _MD_RE.search(s)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            now = time.localtime()
            try:
                ts = time.mktime((now.tm_year, month, day, 0, 0, 0, 0, 0, -1))
                if ts > time.time() + 86400:      # 写的是"明年"的日期，按去年算
                    ts = time.mktime((now.tm_year - 1, month, day, 0, 0, 0, 0, 0, -1))
                return ts
            except (ValueError, OverflowError):
                return None
    return None


def _sort_by_recency(results: list) -> list:
    dated, undated = [], []
    for idx, item in enumerate(results):
        ts = _text_date_ts(f"{item.get('title', '')} {item.get('content', '')}")
        if ts is None:
            undated.append((idx, item))
        else:
            dated.append((ts, idx, item))
    dated.sort(key=lambda x: (-x[0], x[1]))
    return [item for _, _, item in dated] + [item for _, item in undated]


def _looks_relevant(query: str, results: list, minimum: int = 1) -> bool:
    if not results:
        return False
    core = re.sub(r"[\s\u3000]+", "", _core_lookup(query)).lower()
    if not core:
        return True
    minimum = max(1, int(minimum))
    if _is_short_lookup(core):
        grams = {core[i:i + 2] for i in range(len(core) - 1)} if len(core) > 2 else {core}
        hits = 0
        for item in results:
            haystack = f"{item.get('title', '')}{item.get('content', '')}".lower().replace(" ", "")
            if core in haystack or sum(1 for gram in grams if gram in haystack) >= 2:
                hits += 1
        return hits >= minimum
    terms = _search_terms(query)
    if not terms:
        return True
    hits = sum(1 for item in results if _result_matches(item, terms))
    return hits >= max(2, int(minimum))


def _query_variants(query: str, limit: int = 2) -> List[str]:
    raw = str(query or "").strip()
    core = re.sub(r"[\s\u3000]+", "", str(_core_lookup(raw) or ""))
    if not (2 <= len(core) <= 8):
        return []
    if _looks_like_url_arg(core) or re.match(r"^[A-Za-z0-9.\-:/]+$", core):
        return []
    out: List[str] = []
    for suffix in ("是谁", "个人资料", "介绍"):
        candidate = f"{core} {suffix}"
        if candidate != raw and candidate not in out:
            out.append(candidate)
    return out[:max(0, int(limit))]


def _looks_like_verify_page(html_text: str) -> bool:
    sample = str(html_text or "")[:6000]
    if not sample:
        return False
    markers = ("verify.css", "anti.min.css", "anti.min.js", "请输入验证码", "滑动验证",
               "安全验证", "人机验证", "访问过于频繁", "unusual traffic", "captcha",
               "g-recaptcha", "cf-challenge", "checking your browser")
    return any(marker in sample.lower() or marker in sample for marker in markers)


def _is_verify_host(host: str) -> bool:
    host = str(host or "").lower()
    return host.startswith("wappass.") or "captcha" in host or "verify" in host


def _is_verify_url(url: str) -> bool:
    return _is_verify_host(urlparse(str(url or "")).hostname or "")


class SearchVerifyRequired(Exception):
    pass


def _real_url(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if parsed.hostname and parsed.hostname.endswith("baidu.com") and parsed.path.startswith("/link"):
        target = (parse_qs(parsed.query).get("url") or [""])[0]
        if target.startswith("http"):
            return target
    return url


def _unwrap_meta_redirect(html_text: str) -> str:
    text = str(html_text or "")[:4000]
    patterns = (
        r'window\.location(?:\.href)?\s*(?:=|\.replace\()\s*[\'"](https?://[^\'"]+)[\'"]',
        r'http-equiv=["\']?refresh["\']?[^>]*content=["\'][^"\'>]*?url=([^"\'>\s]+)',
        r'location\.replace\(\s*[\'"](https?://[^\'"]+)[\'"]',
    )
    for pattern in patterns:
        found = re.search(pattern, text, re.I)
        if found:
            candidate = _decode_entities(found.group(1)).strip()
            if re.match(r"^https?://", candidate, re.I):
                return candidate
    return ""


def _first(pattern: str, text: str, flags=re.I | re.S) -> str:
    match = re.search(pattern, str(text or ""), flags)
    if not match:
        return ""
    return match.group(1) if match.groups() else match.group(0)


def _clean_results(items: list, limit: int) -> list:
    out = []
    seen = set()
    for item in items or []:
        title = _html_to_text(item.get("title", ""))
        url = str(item.get("url", "") or "").strip()
        snippet = _html_to_text(item.get("snippet", ""))[:500]
        if not title or not re.match(r"^https?://", url, re.I):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append({"title": title, "url": url, "content": snippet})
        if len(out) >= limit:
            break
    return out


_SITE_SUFFIX_RE = re.compile(
    r"[\s_\-–—·|]*(?:百度百科|维基百科|wikipedia|百度知道|百度文库|百度经验|抖音百科|快懂百科|搜狗百科|360百科|知乎|豆瓣|萌娘百科|哔哩哔哩|bilibili|github|游侠网|游民星空|3dm)\s*$",
    re.I)


def _title_core(title: str) -> str:
    text = str(title or "").strip()
    while True:
        stripped = _SITE_SUFFIX_RE.sub("", text).strip()
        if stripped == text:
            return text
        text = stripped


def _demote_degenerate_results(results: list, query: str) -> list:
    if not results:
        return results
    core = re.sub(r"[\s\u3000]+", "", str(_core_lookup(query) or "")).lower()
    if len(core) < 3:
        return results

    def _is_junk(item: dict) -> bool:
        tcore = re.sub(r"[\s\u3000]+", "", str(_title_core(item.get("title", "")) or "")).lower()
        return 0 < len(tcore) <= 2 and len(tcore) < len(core) and core.startswith(tcore)

    normal = [item for item in results if not _is_junk(item)]
    junk = [item for item in results if _is_junk(item)]
    if not normal or not junk:
        return results
    print(f"[搜索] 拆字退化结果沉底 {len(junk)} 条："
          f"{[str(item.get('title', ''))[:24] for item in junk]}")
    return normal + junk


def _body_on_topic(body: str, query: str) -> bool:
    topic = _QUERY_DATE_RE.sub(" ", str(query or "")).strip() or str(query or "")
    core = re.sub(r"[\s\u3000]+", "", _core_lookup(topic)).lower()
    text = re.sub(r"[\s\u3000]+", "", str(body or "")).lower()
    if not core or not text:
        return True
    if _is_short_lookup(core):
        grams = {core[i:i + 2] for i in range(len(core) - 1)} if len(core) > 2 else {core}
        return core in text or sum(1 for gram in grams if gram in text) >= 2
    terms = _search_terms(topic)
    if not terms:
        return True
    return sum(1 for term in terms if term in text) >= 2


def _demote_failed_results(results: list, failed_urls: set) -> list:
    if not results or not failed_urls:
        return results
    good = [item for item in results if item.get("url") not in failed_urls]
    bad = [item for item in results if item.get("url") in failed_urls]
    if not good or not bad:
        return results
    print(f"[搜索] 正文核对不通过的结果沉底 {len(bad)} 条："
          f"{[str(item.get('title', ''))[:24] for item in bad]}")
    return good + bad


async def _verify_bodies(items: list, query: str, must_contain=(),
                         limit: int = 3, chars: int = 1200) -> Tuple[List[Tuple[dict, str]], set]:
    picked = []
    for item in items:
        url = str(item.get("url") or "")
        host = (urlparse(url).hostname or "").lower()
        if not url.startswith("http"):
            continue
        if any(host == h or host.endswith("." + h) for h in _NO_FETCH_HOSTS):
            continue
        picked.append((item, url))
        if len(picked) >= limit:
            break
    if not picked:
        return [], set()

    async def _one(pair):
        item, url = pair
        try:
            ok, body = await _fetch_page({"max_chars": chars, "timeout": 10}, url)
        except Exception:
            return item, url, ""
        return item, url, str(body or "").strip() if ok else ""

    verified, failed = [], set()
    for item, url, body in await asyncio.gather(*[_one(pair) for pair in picked]):
        if not body or len(body) < 200:
            continue
        if must_contain and not all(m in body for m in must_contain):
            failed.add(url)
            continue
        if not _body_on_topic(body, query):
            failed.add(url)
            continue
        verified.append((item, body))
    return verified, failed


def _baidu_parse_html(text: str, limit: int) -> list:
    items = []
    for chunk in re.findall(r'<div[^>]*class="result[^"]*c-container[\s\S]*?(?=<div[^>]*class="result[^"]*c-container|<div[^>]*id="page")', text, re.I):
        head = chunk[:chunk.find(">") + 1]
        title = _html_to_text(_first(r"<h3[\s\S]*?</h3>", chunk))
        if not title:
            continue
        url = _attr_of(head, "mu")
        if not re.match(r"^https?://", url, re.I):
            url = _abs_url(_attr_of(_first(r"<h3[^>]*>\s*<a[^>]*>", chunk, re.I | re.S) or chunk, "href"), "https://www.baidu.com")
        snippet = (_html_to_text(_first(r'<span[^>]*class="[^"]*content-right[^"]*"[^>]*>([\s\S]*?)</span>', chunk))
                   or _html_to_text(_first(r'<div[^>]*class="[^"]*c-abstract[^"]*"[^>]*>([\s\S]*?)</div>', chunk))
                   or _html_to_text(_first(r'<span[^>]*class="[^"]*c-color-gray[^"]*"[^>]*>([\s\S]*?)</span>', chunk)))
        items.append({"title": title, "url": url, "snippet": snippet})
    return _clean_results(items, limit)


def _bing_parse_html(text: str, limit: int) -> list:
    items = []
    for block in re.findall(r'<li class="b_algo"[\s\S]*?</li>', text, re.I):
        heading = _first(r"<h2[\s\S]*?</h2>", block)
        if not heading:
            continue
        anchor = _first(r"<a[^>]*>", heading, re.I | re.S)
        items.append({
            "title": _html_to_text(heading),
            "url": _abs_url(_attr_of(anchor, "href"), "https://www.bing.com"),
            "snippet": _html_to_text(_first(r'<div class="b_caption"[\s\S]*?</div>', block)),
        })
    return _clean_results(items, limit)


def _so360_parse_html(text: str, limit: int) -> list:
    items = []
    for block in re.findall(r'<li class="res-list"[\s\S]*?</li>', text, re.I):
        heading = _first(r"<h3[\s\S]*?</h3>", block)
        anchor = _first(r"<a[^>]*>", heading, re.I | re.S)
        url = _attr_of(anchor, "data-mdurl") or _abs_url(_attr_of(anchor, "href"), "https://www.so.com")
        items.append({
            "title": _html_to_text(heading),
            "url": url,
            "snippet": _html_to_text(_first(r'<span class="res-list-summary"[^>]*>([\s\S]*?)</span>', block)),
        })
    return _clean_results(items, limit)


def _sogou_parse_html(text: str, limit: int) -> list:
    items = []
    for block in re.findall(r'<div class="(?:vrwrap|rb)"[\s\S]*?(?=<div class="(?:vrwrap|rb)"|</body>)', text, re.I):
        heading = _first(r'<h3[^>]*class="[^"]*vr-title[^"]*"[\s\S]*?</h3>', block) or _first(r"<h3[\s\S]*?</h3>", block)
        anchor = _first(r"<a[^>]*>", heading, re.I | re.S)
        items.append({
            "title": _html_to_text(heading),
            "url": _abs_url(_attr_of(anchor, "href"), "https://www.sogou.com"),
            "snippet": _html_to_text(_first(r'<div[^>]*class="[^"]*(?:space-txt|text-layout)[^"]*"[^>]*>([\s\S]*?)</div>', block)),
        })
    return _clean_results(items, limit)


def _rss_parse_xml(text: str, limit: int) -> list:
    items = []
    for entry in re.findall(r"<item[\s\S]*?</item>|<entry[\s\S]*?</entry>", str(text or ""), re.I):
        link = _first(r'<link[^>]*href="([^"]+)"', entry, re.I) or _html_to_text(_first(r"<link[^>]*>([\s\S]*?)</link>", entry))
        items.append({
            "title": _first(r"<title[^>]*>([\s\S]*?)</title>", entry),
            "url": _decode_entities(link),
            "snippet": _first(r"<(?:description|summary|content)[^>]*>([\s\S]*?)</(?:description|summary|content)>", entry),
        })
    return _clean_results(items, limit)


def _generic_parse_html(text: str, limit: int) -> list:
    items = []
    for block in re.findall(r"<li[\s\S]*?</li>", str(text or ""), re.I):
        heading = _first(r"<h[1-4][^>]*>([\s\S]*?)</h[1-4]>", block)
        anchor = _first(r"<a[^>]*>", heading or block, re.I | re.S)
        href = _attr_of(anchor, "href")
        if not heading or not href:
            continue
        items.append({"title": heading, "url": _abs_url(href, ""), "snippet": _html_to_text(block)})
    return _clean_results(items, limit)


def _searxng_params(query: str, page: int, language: str) -> dict:
    return {"q": query, "format": "json", "no_html": 1, "language": language, "pageno": page}


def _searxng_parse_json(data, limit: int) -> list:
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return []
    return _clean_results([{
        "title": item.get("title", ""), "url": item.get("url", ""),
        "snippet": item.get("content", "") or item.get("snippet", ""),
    } for item in results if isinstance(item, dict)], limit)


def _searxng_parse_html_text(text, limit: int) -> list:
    if isinstance(text, (dict, list)):
        return _searxng_parse_json(text, limit)
    try:
        data = json.loads(str(text or ""))
    except (ValueError, TypeError):
        return []
    return _searxng_parse_json(data, limit)


def _bing_params(query: str, page: int, language: str) -> dict:
    return {"q": query, "count": 20, "setlang": "zh-Hans", "ensearch": 0,
            "first": (page - 1) * 10 + 1}


def _bing_api_params(query: str, page: int, language: str) -> dict:
    return {"q": query, "count": 20, "mkt": "zh-CN", "offset": (page - 1) * 20}


def _bing_api_parse_json(data, limit: int) -> list:
    pages = (data or {}).get("webPages") or {}
    values = pages.get("value") if isinstance(pages, dict) else None
    if not isinstance(values, list):
        return []
    return _clean_results([{
        "title": item.get("name", ""), "url": item.get("url", ""),
        "snippet": item.get("snippet", ""),
    } for item in values if isinstance(item, dict)], limit)


def _brave_api_parse_json(data, limit: int) -> list:
    values = ((data or {}).get("web") or {}).get("results")
    if not isinstance(values, list):
        return []
    return _clean_results([{
        "title": item.get("title", ""), "url": item.get("url", ""),
        "snippet": item.get("description", ""),
    } for item in values if isinstance(item, dict)], limit)


def _baidu_api_parse_json(data, limit: int) -> list:
    refs = (data or {}).get("references")
    if not isinstance(refs, list):
        return []
    return _clean_results([{
        "title": item.get("title", ""), "url": item.get("url", ""),
        "snippet": item.get("content", ""),
    } for item in refs if isinstance(item, dict)], limit)


SEARCH_ENGINES: Dict[str, dict] = {
    "searxng": {
        "label": "SearXNG / 自建接口",
        "desc": "自建或公共 SearXNG 的 JSON 接口（默认地址，可在上方自定义 URL 覆盖）",
        "url": "https://searx.be/search",
        "params": _searxng_params,
        "parse_json": _searxng_parse_json,
        "parse_html": _searxng_parse_html_text,
        "url_env": ("web_search_url",),
        "allow_private": True,
    },
    "bing": {
        "label": "Bing 国际版",
        "desc": "Bing 网页检索（有 BING_SEARCH_API_KEY 走官方接口，否则解析网页）",
        "url": "https://www.bing.com/search",
        "params": _bing_params,
        "parse_html": _bing_parse_html,
        "follow_hosts": ("cn.bing.com",),
        "api_url": "https://api.bing.microsoft.com/v7.0/search",
        "api_key_env": "BING_SEARCH_API_KEY",
        "api_params": _bing_api_params,
        "api_headers": lambda key: {"Ocp-Apim-Subscription-Key": key},
        "parse_json": _bing_api_parse_json,
    },
    "bing-en": {
        "label": "Bing 英文",
        "desc": "Bing 英文结果，适合查英文资料",
        "url": "https://www.bing.com/search",
        "params": lambda q, page, lang: {"q": q, "count": 20, "setlang": "en",
                                         "first": (page - 1) * 10 + 1},
        "parse_html": _bing_parse_html,
        "follow_hosts": ("cn.bing.com",),
        "api_url": "https://api.bing.microsoft.com/v7.0/search",
        "api_key_env": "BING_SEARCH_API_KEY",
        "api_params": _bing_api_params,
        "api_headers": lambda key: {"Ocp-Apim-Subscription-Key": key},
        "parse_json": _bing_api_parse_json,
    },
    "baidu": {
        "label": "百度",
        "desc": "百度网页检索；经常要求安全验证（届时请换引擎），配置 BAIDU_SEARCH_API_KEY 走千帆接口可稳定使用",
        "url": "https://www.baidu.com/s",
        "params": lambda q, page, lang: {"wd": q, "ie": "utf-8", "rn": 20,
                                         "pn": (page - 1) * 10},
        "parse_html": _baidu_parse_html,
        "api_url": "https://qianfan.baidubce.com/v2/ai_search/web_search",
        "api_key_env": "BAIDU_SEARCH_API_KEY",
        "api_method": "POST",
        "api_headers": lambda key: {"Authorization": "Bearer " + key},
        "api_json": lambda q, page, lang: {
            "messages": [{"role": "user", "content": q}],
            "search_source": "baidu_search_v2",
            "resource_type_filter": [{"type": "web", "top_k": 20}],
        },
        "parse_json": _baidu_api_parse_json,
    },
    "360": {
        "label": "360 搜索",
        "desc": "so.com 网页检索；短时间多次搜索可能要求人机验证，届时请换引擎或稍后再试",
        "url": "https://www.so.com/s",
        "params": lambda q, page, lang: {"q": q, "pn": page},
        "parse_html": _so360_parse_html,
    },
    "sogou": {
        "label": "搜狗",
        "desc": "搜狗网页检索（解析网页，结果跳转链接会自动解析成真实地址）",
        "url": "https://www.sogou.com/web",
        "params": lambda q, page, lang: {"query": q, "page": page},
        "parse_html": _sogou_parse_html,
        "redirect_hosts": ("sogou.com",),
    },
    "google-news": {
        "label": "Google News RSS",
        "desc": "谷歌新闻 RSS 检索，适合查最新资讯",
        "url": "https://news.google.com/rss/search",
        "params": lambda q, page, lang: {"q": q, "hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans"},
        "parse_html": _rss_parse_xml,
        "parse_json": lambda data, limit: _rss_parse_xml(data if isinstance(data, str) else "", limit),
    },
    "brave": {
        "label": "Brave Search API",
        "desc": "Brave 官方接口（需要 BRAVE_SEARCH_API_KEY）",
        "url": "",
        "api_url": "https://api.search.brave.com/res/v1/web/search",
        "api_key_env": "BRAVE_SEARCH_API_KEY",
        "api_params": lambda q, page, lang: {"q": q, "count": 20, "offset": (page - 1) * 20},
        "api_headers": lambda key: {"X-Subscription-Token": key},
        "parse_json": _brave_api_parse_json,
    },
}


def invalid_custom_engines(raw) -> List[str]:
    bad = []
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, url = line.partition("=")
        name, url = name.strip(), url.strip()
        if not name or "http" not in url.lower():
            continue
        if "{query}" not in url:
            bad.append(name)
    return bad


def _parse_custom_engines(raw) -> Dict[str, dict]:
    engines: Dict[str, dict] = {}
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, url = line.partition("=")
        name, url = name.strip(), url.strip()
        if not name or "http" not in url.lower():
            continue
        if "{query}" not in url:
            print(f"自定义搜索引擎「{name}」的地址模板缺少 {{query}} 占位符，已忽略该引擎。")
            continue
        engines[name] = {
            "label": name,
            "desc": f"自定义引擎 {name}",
            "url": url,
            "params": lambda q, page, lang, _tpl=url: None,
            "parse_html": _generic_parse_html,
            "custom": True,
        }
    return engines


def available_engines(config) -> Dict[str, dict]:
    engines = {k: {**v, "key": k} for k, v in SEARCH_ENGINES.items()}
    engines.update(_parse_custom_engines((config or {}).get("web_search_custom_engines", "")))
    return engines


def engine_labels(config) -> List[tuple]:
    return [(key, spec.get("label", key)) for key, spec in available_engines(config).items()]


def default_engine_key(config) -> str:
    engines = available_engines(config)
    if not engines:
        return ""
    wanted = str((config or {}).get("web_search_engine", "") or "").strip()
    return wanted if wanted in engines else next(iter(engines))


def parse_api_keys(raw) -> dict:
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if str(v).strip()}
    keys = {}
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if name and value:
            keys[name] = value
    return keys


def engine_api_key(config, env_name: str) -> str:
    if not env_name:
        return ""
    configured = parse_api_keys((config or {}).get("web_search_api_keys", {}))
    return str(configured.get(env_name, "") or os.environ.get(env_name, "") or "").strip()


def _engine_request_url(spec: dict, query: str, page: int, language: str, config) -> str:
    template = str(spec.get("url", "") or "")
    for key in spec.get("url_env", ()):
        configured = str((config or {}).get(key, "") or "").strip()
        if configured:
            template = configured
            break
    if not template:
        return ""
    url = template.replace("{query}", quote(query)).replace("{page}", str(page))
    builder = spec.get("params")
    params = builder(query, page, language) if callable(builder) else None
    if params is None:  # 模板自己带参数，不再追加
        return url
    parsed = urlparse(url)
    query_pairs = [(k, str(v)) for k, v in params.items() if v not in (None, "")]
    merged = parse_qs(parsed.query)
    for k, v in query_pairs:
        merged[k] = [v]
    for key, value in _safe_search_params(spec, config).items():
        merged.setdefault(key, [])
        if value not in merged[key]:
            merged[key].append(value)
    flat = [(k, item) for k, values in merged.items() for item in values]
    return parsed._replace(query=urlencode(flat)).geturl()


# ---------------------------------------------------------------------------
# 安全搜索（三档）
# ---------------------------------------------------------------------------
#   off    —— 不限制任何搜索
SAFE_SEARCH_LEVELS = ("off", "normal", "strict")
_SAFE_SEARCH_LABELS = {"off": "无", "normal": "一般", "strict": "严格"}

_ENGINE_SAFE_PARAMS = {
    "searxng": {"off": {"safesearch": "0"}, "normal": {"safesearch": "1"},
                "strict": {"safesearch": "2"}},
    "bing": {"off": {"adlt": "off"}, "normal": {"adlt": "moderate"},
             "strict": {"adlt": "strict"}},
    "bing-en": {"off": {"adlt": "off"}, "normal": {"adlt": "moderate"},
                "strict": {"adlt": "strict"}},
    "google-news": {"off": {}, "normal": {"safe": "active"}, "strict": {"safe": "active"}},
}

_ADULT_TERMS = (
    "r18", "r-18", "18禁", "十八禁", "18+", "限制级",
    "成人内容", "成人向", "成人视频", "成人影片", "成人电影", "成人动画",
    "成人动漫", "成人漫画", "成人小说", "成人游戏", "成人网站", "成人论坛",
    "成人社区", "成人图片", "成人套图", "成人自拍", "成人直播", "成人用品",
    "成人片", "色情", "色情片", "色情网站", "色情视频", "色情图片", "色情小说",
    "色情游戏", "色情直播", "色情服务", "色情动漫", "情色", "情色片",
    "黄色网站", "黄色电影", "黄色小说", "黄色图片", "黄片", "毛片", "黄网",
    "黄站", "三级片", "三级电影", "エロ",
    "av女优", "av番号", "av在线", "av下载", "av资源", "av网站", "av影片",
    "av电影", "无码", "有码", "无码流出", "无码破解", "无修正", "无修版",
    "里番", "工口",
    "hentai", "hanime", "nhentai", "e-hentai", "exhentai", "porn", "porno",
    "pornography", "nsfw", "xvideos", "xhamster", "pornhub", "spankbang",
    "eporner", "missav", "supjav", "jable", "javbus", "javdb", "javlibrary",
    "avmoo", "avsee", "madou", "onlyfans", "fansly", "chaturbate", "cam4",
    "stripchat", "bongacams", "livejasmin", "erome", "thisvid", "motherless",
    "beeg", "brazzers", "realitykings", "fanza", "duga", "erotic",
    "adult video", "adult site", "adult content","奸淫", 
    "淫秽", "淫乱", "淫荡", "淫水", "淫叫", "淫妻", "淫娃", "淫魔",
    "嫖娼", "卖淫", "招妓", "楼凤", "外围女", "站街女",
    "性服务", "性交易", "性奴", "性虐", "约炮", "约啪", "炮友", "一夜情",
    "援交", "sex video", "sex toy", "sex chat", "sex cam",
)
_ADULT_SITES = (
    "91爆料", "91porn", "91av", "91视频", "91国产", "91制片厂", "51吃瓜",
    "吃瓜网", "黑料网", "黑料不打烊", "爆料网", "草榴", "1024社区", "t66y",
    "色花堂", "sehuatang", "麻豆传媒", "麻豆映画", "天美传媒", "精东影业",
    "蜜桃传媒", "星空传媒", "果冻传媒", "糖心vlog", "糖心传媒", "国产精品",
    "精品国产", "自拍偷拍", "偷拍自拍", "里番库", "91传媒"
)
_SUGGESTIVE_TERMS = (
    "性暗示", "大尺度", "露点", "走光", "艳照", "裸照", "裸聊", "裸舞", "裸体",
    "性感写真", "性感主播", "擦边", "擦边球", "软色情", "情色小说", "情欲",
    "艳情", "床戏", "香艳",
    "福利姬", "女优", "风俗店", "sugar baby", "escort", "camgirl",
    "nsfw18", "nude", "naked",
    "性交", "性爱", "做爱", "性行为", "自慰", "手淫", "口交", "肛交", "颜射",
    "巨乳", "爆乳", "痴女", "肉棒", "肉便器", "潮吹", "调教", "制服诱惑", "鸡巴", 
    "小穴", "骚货", "骚逼", "骚女", "骚穴", "双飞", "群交", "后入式","69式", "舔阴",
    "舔奶", "舔肛", "足交", "乳交", "打飞机", "破处", "大奶", "大屌", "屄", "户外露出",
    "野外露出", "野战", "群p", "多人性行为", "多人性交", "多人性爱", "车震", "激情床戏",
    "摸奶", "摸胸", "操逼", "淫穴"
)
_ADULT_DOMAINS = (
    "91porn", "91av", "91zuida", "51chigua", "chigua", "heiliao", "madou",
    "sehuatang", "t66y", "xvideo", "pornhub", "missav", "javbus", "javdb",
    "hanime", "nhentai", "rule34", "onlyfans", "chaturbate", "cam4",
    "spankbang", "eporner", "agedm", "asmr18",
)


def safe_search_level(config) -> str:
    raw = str((config or {}).get("web_search_safe", "normal") or "normal").strip().lower()
    aliases = {"0": "off", "off": "off", "none": "off", "无": "off", "关闭": "off",
               "1": "normal", "normal": "normal", "moderate": "normal", "一般": "normal",
               "2": "strict", "strict": "strict", "严格": "strict", "高": "strict"}
    return aliases.get(raw, "normal")


def safe_search_label(config) -> str:
    return _SAFE_SEARCH_LABELS.get(safe_search_level(config), "一般")


def _safe_search_params(spec: dict, config) -> dict:
    level = safe_search_level(config)
    if level == "off":
        return {}
    if spec.get("custom") or spec.get("api_url"):
        return {}
    mapping = _ENGINE_SAFE_PARAMS.get(str(spec.get("key", "") or "").lower())
    if not mapping:
        return {"safe": "1"} if level == "strict" else {}
    return dict(mapping.get(level, {}))


def _safe_search_terms(level: str) -> tuple:
    if level == "off":
        return ()
    if level == "strict":
        return _ADULT_TERMS + _ADULT_SITES + _SUGGESTIVE_TERMS
    return _ADULT_TERMS + _ADULT_SITES


_WORD_SPLIT_RE = re.compile(r"[\n\r,，、;；]+")


def _split_words(raw) -> tuple:
    out = []
    for chunk in _WORD_SPLIT_RE.split(str(raw or "")):
        word = chunk.strip().lower()
        if word and word not in out:
            out.append(word)
    return tuple(out)


def extra_block_terms(config) -> tuple:
    return _split_words((config or {}).get("web_search_block_words", ""))


def extra_allow_terms(config) -> tuple:
    return _split_words((config or {}).get("web_search_allow_words", ""))


def _adult_domain(url: str) -> str:
    low = str(url or "").lower()
    return next((d for d in _ADULT_DOMAINS if d in low), "")


def filter_unsafe_results(results: list, config, query: str = "") -> list:
    level = safe_search_level(config)
    if level == "off" or not results:
        return list(results or [])
    terms = _safe_search_terms(level) + extra_block_terms(config)
    allow = extra_allow_terms(config)
    kept, dropped = [], []
    for item in results:
        if not isinstance(item, dict):
            continue
        haystack = " ".join(str(item.get(k, "") or "")
                            for k in ("title", "url", "content", "snippet")).lower()
        if allow and any(t in haystack for t in allow):
            kept.append(item)
            continue
        domain = _adult_domain(str(item.get("url", "") or ""))
        if domain:
            dropped.append((str(item.get("title", ""))[:40], f"域名 {domain}"))
            continue
        hit = next((t for t in terms if t in haystack), "")
        if hit:
            dropped.append((str(item.get("title", ""))[:40], hit))
        else:
            kept.append(item)
    if dropped:
        print(f"[安全搜索-{_SAFE_SEARCH_LABELS.get(level, level)}] 已过滤 {len(dropped)} 条"
              f"命中安全搜索黑名单的结果："
              + "；".join(f"{t}（命中「{k}」）" for t, k in dropped[:5]))
    return kept


def safe_search_hint(config) -> str:
    level = safe_search_level(config)
    if level == "off":
        return ""
    if level == "strict":
        return ("\n\n（安全搜索：严格 —— 已过滤低俗、性暗示与成人向内容；"
                "如结果偏少可换用更中性的关键词。）")
    return ("\n\n（安全搜索：一般 —— 已过滤成人向（R18）内容；"
            "如结果偏少可换用更中性的关键词。）")


_API_SAFE_PARAMS = {
    "bing": {"off": {"adultPreference": "Off"}, "normal": {"adultPreference": "Moderate"},
             "strict": {"adultPreference": "Strict"}},
    "bing-en": {"off": {"adultPreference": "Off"}, "normal": {"adultPreference": "Moderate"},
                "strict": {"adultPreference": "Strict"}},
    "brave": {"off": {"safesearch": "off"}, "normal": {"safesearch": "moderate"},
              "strict": {"safesearch": "strict"}},
}


def _safe_api_params(spec: dict, config) -> dict:
    level = safe_search_level(config)
    if level == "off":
        return {}
    mapping = _API_SAFE_PARAMS.get(str(spec.get("key", "") or "").lower())
    return dict(mapping.get(level, {})) if mapping else {}


def _with_api_safe_search(payload, spec: dict, config):
    extra = _safe_api_params(spec, config)
    if not extra or not isinstance(payload, dict):
        return payload
    return {**payload, **extra}



def _normalize_calc_expr(text: str) -> str:
    s = str(text or "")
    for sep in ("=", "＝"):
        if sep in s:
            s = s.split(sep, 1)[0]
    s = re.split(r"[？?]", s, maxsplit=1)[0]
    for a, b in (("乘以", "*"), ("乘上", "*"), ("乘", "*"), ("除以", "/"),
                 ("除", "/"), ("加上", "+"), ("加", "+"), ("减去", "-"), ("减", "-"),
                 ("×", "*"), ("＊", "*"), ("÷", "/"), ("＋", "+"), ("－", "-"),
                 ("（", "("), ("）", ")"), ("^", "**"),
                 ("²", "**2"), ("³", "**3"),
                 ("的平方", "**2"), ("平方", "**2"), ("的立方", "**3"), ("立方", "**3")):
        s = s.replace(a, b)
    return re.sub(r"[\s\u3000]+", "", s)


def _calc_value(raw: str):
    expr = _normalize_calc_expr(raw)
    try:
        return _safe_calc(expr)
    except Exception:
        chunks = re.findall(r"[0-9+\-*/%.^()a-zA-Z]+", expr)
        chunks.sort(key=len, reverse=True)
        for chunk in chunks:
            try:
                return _safe_calc(chunk)
            except Exception:
                continue
        raise ValueError(f"无法识别的表达式: {str(raw)[:60]}")


_WMO_ZH = {
    0: "晴", 1: "大致晴朗", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨", 56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒", 80: "小阵雨",
    81: "中阵雨", 82: "强阵雨", 85: "小阵雪", 86: "大阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹",
}

TOOL_TEST_SAMPLES = {
    "time": {},
    "calculate": {"expression": "23*7+sqrt(144)"},
    "weather": {"city": "北京"},
    "random": {"min": 1, "max": 100},
    "web_fetch": {"url": "https://example.com"},
    "web_search": {"query": "人工智能"},
}


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
_WEATHER_INTENT_RE = re.compile(
    r"天气|气温|温度|下雨|降雨|带伞|雨伞|下雪|降雪|台风|冷不冷|热不热|穿什么|要穿|"
    r"多少度|几度|(?<![A-Za-z])weather(?![A-Za-z])|(?<![A-Za-z])forecast(?![A-Za-z])|"
    r"(?<![A-Za-z])temperature(?![A-Za-z])|(?<![A-Za-z])rain(?![A-Za-z])", re.I)

_CITY_SUFFIXES = ("市", "县", "区", "州", "盟", "旗", "镇")
_VAGUE_PLACES = ("这里", "这边", "那里", "那边", "本地", "当地", "我这", "你那",
                 "家里", "外面", "附近", "外面", "国内", "国外")
_KNOWN_CITIES = (
    "北京", "上海", "广州", "深圳", "天津", "重庆", "成都", "杭州", "南京", "武汉",
    "西安", "苏州", "长沙", "郑州", "青岛", "沈阳", "大连", "厦门", "福州", "济南",
    "合肥", "昆明", "哈尔滨", "长春", "石家庄", "太原", "南昌", "贵阳", "南宁", "兰州",
    "乌鲁木齐", "呼和浩特", "银川", "西宁", "拉萨", "海口", "三亚", "无锡", "宁波",
    "温州", "佛山", "东莞", "珠海", "中山", "惠州", "泉州", "烟台", "常州", "徐州",
    "唐山", "洛阳", "香港", "澳门", "台北", "高雄", "东京", "大阪", "京都", "首尔",
    "新加坡", "纽约", "伦敦", "巴黎", "洛杉矶",
)
_WEATHER_NOISE_WORDS = ("天气", "气温", "温度", "下雨", "降雨", "雨伞", "带伞", "下雪",
                        "降雪", "台风", "多少度", "几度", "今天", "明天", "后天", "昨天",
                        "现在", "怎么样", "如何", "怎样", "冷不冷", "热不热", "需不需",
                        "要不要", "记得", "帮我", "查一下", "查查", "看看", "一下",
                        "主人", "会不会", "适合", "穿什么", "要穿", "出门", "外面")
_NON_CITY_CHARS = set("了的地得吗呢吧啊呀哦喔嘛么啥谁哪怎")
_EN_NON_CITY = {"what", "how", "when", "where", "which", "who", "why", "is", "are",
                "the", "in", "at", "of", "today", "tomorrow", "please", "tell", "me",
                "weather", "forecast", "temperature", "rain"}


def _is_weather_question(text: str) -> bool:
    return bool(_WEATHER_INTENT_RE.search(str(text or "")))


def _clean_city_token(token: str) -> str:
    t = str(token or "").strip().strip("的了地得吗呢吧啊呀哦喔嘛么")
    for _ in range(6):
        before = t
        if not t:
            break
        if t in _VAGUE_PLACES:
            return ""
        for word in ("今天", "明天", "后天", "昨天", "现在", "帮我", "主人", "查一下",
                     "查查", "看看", "我在", "我在", "你在", "他在", "我在"):
            if t.startswith(word) and len(t) - len(word) >= 2:
                t = t[len(word):]
            if t.endswith(word) and len(t) - len(word) >= 2:
                t = t[: -len(word)]
        if len(t) > 3 and t[-1] in "要会需想去的了嘛吗呢":
            t = t[:-1]
        t = t.strip().strip("的了地得吗呢吧啊呀哦喔嘛么")
        if t == before:
            break
    return t


def _looks_like_city(token: str) -> bool:
    t = _clean_city_token(token)
    if len(t) < 2 or t in _VAGUE_PLACES:
        return False
    if t.lower() in _EN_NON_CITY:
        return False
    if any(t == w or t.startswith(w) or t.endswith(w) for w in _WEATHER_NOISE_WORDS):
        return False
    if t[-1] in _NON_CITY_CHARS:
        return False
    if any(t.startswith(c) or t.endswith(c) for c in _KNOWN_CITIES):
        return True
    cjk = re.sub(r"[^\u4e00-\u9fff]", "", t)
    if cjk != t:
        return bool(re.fullmatch(r"[A-Za-z][A-Za-z\s\-]{1,20}", t))
    if t.endswith(_CITY_SUFFIXES):
        return len(cjk) >= 2
    return False


def _extract_city_from_text(text: str) -> str:
    from .llm_helpers import strip_quote_note   # 延迟导入，避免模块级循环依赖
    body = strip_quote_note(str(text or ""))
    body = _WEATHER_INTENT_RE.sub(" ", body)
    for token in re.split(r"[\s,，。.、；;：:！!？?～~（）()【】\[\]\"'“”‘’]+", body):
        token = token.strip()
        if _looks_like_city(token):
            return _clean_city_token(token)
    return ""


def _resolve_weather_city(args: dict, tool: dict, user_text: str) -> Tuple[str, str]:
    city = _pick_arg(args, "city", "location", "name", "place", "城市", "地点")
    default = str((tool or {}).get("default_location", "") or "").strip()
    text = str(user_text or "")
    said = _extract_city_from_text(text)
    if not text:
        return city or default, ""
    if said:
        return said, ""          # 用户明确说了地点：以用户说的为准
    if _is_weather_question(text):
        msg = ("对方本条消息里没有说明地点，你也不知道对方在哪个城市。"
               "绝不允许自行假设（例如上海/北京）报出任何城市的天气："
               "请直接用角色身份问一句「你现在在哪个城市呀？」，"
               "不要调用任何工具、不要提任何城市名。")
        if city or default:
            print(f"[工具] 天气：模型想用「{city or default}」，但用户本条消息没说地点"
                  f"（消息={text[:60]!r}），已拦下改为反问地点")
        else:
            print(f"[工具] 天气：用户没给地点（消息={text[:60]!r}），已拦下查询并让角色反问地点")
        return "", msg
    if default:
        return default, ""
    if city:
        print(f"[工具] 天气：模型给的城市「{city}」不在用户本条消息里，"
              f"判定为模型自行猜测，已拦下（消息={text[:60]!r}）")
        return "", ("你填的城市并不在对方本条消息里，属于你自己猜的地点。"
                    "请直接用角色身份问一句对方现在在哪个城市，不要调用任何工具。")
    return "", ""


def test_sample_for(tool: dict) -> dict:
    return dict(TOOL_TEST_SAMPLES.get(str((tool or {}).get("builtin", "")).lower(), {}) or {})


_ASK_LOCATION_RE = re.compile(r"哪个城市|哪座城市|在哪儿|在哪里|什么城市|哪里的|所在城市|"
                              r"どこの|どの都市|どこに住|where do you live|which city", re.I)


def last_user_message(messages) -> str:
    for message in reversed(list(messages or [])):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = str(message.get("content", ""))
        if content.startswith("【"):
            continue
        return content
    return ""


def reply_asks_for_location(text) -> bool:
    return bool(_ASK_LOCATION_RE.search(str(text or "")))


def _geo_queries(city: str) -> list:
    c = str(city or "").strip()
    if not c:
        return []
    base = c
    for suf in ("市", "县", "区", "盟", "州"):
        if base.endswith(suf):
            base = base[: -len(suf)]
            break
    queries = [c]
    if re.search(r"[\u4e00-\u9fff]", c) and not c.endswith(("市", "县", "区")):
        queries.append(c + "市")
    if base and base != c:
        queries.append(base)
        if re.search(r"[\u4e00-\u9fff]", base) and not base.endswith(("市", "县", "区")):
            queries.append(base + "市")
    out = []
    for q in queries:
        if q not in out:
            out.append(q)
    return out


def _best_geo(results: list) -> Optional[dict]:
    best = None
    for r in results or []:
        if not isinstance(r, dict):
            continue
        if best is None:
            best = r
            continue

        def _pop(x):
            try:
                return int(x.get("population") or 0)
            except Exception:
                return 0

        if _pop(r) > _pop(best):
            best = r
    return best


async def _fetch_direct(query: str, tool_registry) -> Tuple[bool, str]:
    search_tool = _search_tool(tool_registry)
    if search_tool is None:
        return False, (f"「{query}」不是网址：web_fetch 只能读取具体网页，"
                       f"需要关键词搜索请在 WebUI 里启用 web_search 工具")
    engine = _pick_arg(search_tool, "engine") or str(search_tool.get("engine", "") or "")
    ok, output = await tool_registry._web_search(query, search_tool, engine)
    if ok:
        return True, output + "\n（本条由 web_fetch 收到搜索词后自动改用搜索引擎）"
    return ok, output


async def _fetch_page(tool: dict, url: str) -> Tuple[bool, str]:
    url = url.strip().strip('"').strip("'").strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    url = await asyncio.to_thread(_validate_url, url)
    max_chars = int(tool.get("max_chars", 1500) or 1500)
    timeout = int(tool.get("timeout", 15) or 15)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    body = ""
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False,
                                     follow_redirects=False,
                                     verify=verified_context()) as client:
            current = url
            resp = None
            for _ in range(5):
                resp = await client.get(current, headers=headers)
                _validate_redirect(resp)
                if resp.status_code in (301, 302, 303, 307, 308) \
                        and resp.next_request is not None:
                    current = str(resp.next_request.url)
                    continue
                break
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "").lower()
            if "json" in ctype:
                try:
                    body = json.dumps(resp.json(), ensure_ascii=False)
                except Exception:
                    body = resp.text
            else:
                body = _strip_html(resp.text)
                if not body:
                    try:
                        body = _strip_html(resp.content.decode("utf-8", errors="ignore"))
                    except Exception:
                        body = ""
    except httpx.HTTPStatusError as e:
        return False, f"网页返回 HTTP {e.response.status_code}：{url[:80]}"
    except httpx.RequestError as e:
        return False, f"网页请求失败: {type(e).__name__}：{url[:80]}"
    if not body.strip():
        return False, "网页内容为空（可能需要 JS 渲染或站点拒绝访问）"
    return True, body[:max_chars]


def _search_tool(tool_registry) -> Optional[dict]:
    for item in getattr(tool_registry, "tools", []) or []:
        if str(item.get("builtin", "")).lower() == "web_search" and item.get("enabled"):
            return item
    return None


def _pick_queries(args: dict, limit: int = 4) -> List[str]:
    out: List[str] = []

    def _add(value):
        if isinstance(value, (list, tuple, set)):
            for item in value:
                _add(item)
            return
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)

    for key in ("query", "queries", "q", "keywords", "keyword", "关键词", "搜索词", "问题"):
        if key in (args or {}):
            _add(args.get(key))
    return out[:max(1, int(limit))]


async def _run_queries(tool_registry, queries: List[str], tool: dict, engine: str = "",
                       total_chars: int = 0) -> Tuple[bool, str]:
    per_query = 0
    if total_chars and len(queries) > 1:
        share = int(total_chars) // len(queries)
        per_query = min(1000, max(500, int(share * 0.95)))
    blocks, failures = [], []
    used = 0
    for item in queries:
        if total_chars and used >= total_chars:
            failures.append(f"「{item}」未搜索：已达到本次结果长度上限")
            continue
        ok, output = await tool_registry._web_search(item, tool, engine,
                                                     max_chars=per_query or None)
        if ok:
            blocks.append(output)
            used += len(output)
        else:
            failures.append(f"「{item}」搜索失败：{output}")
    if not blocks:
        return False, "；".join(failures) or "搜索失败"
    text = "\n\n".join(blocks)
    if failures:
        text += "\n\n（以下子查询未成功：" + "；".join(failures) + "）"
    return True, text


def _decode_console_output(raw) -> str:
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw
    candidates = ["utf-8"]
    preferred = locale.getpreferredencoding(False) or ""
    if preferred:
        candidates.append(preferred)
    for enc in candidates:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


class ToolRegistry:
    def __init__(self, config, data_path: Path):
        self.config = config
        self.data_path = Path(data_path)
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.file = self.data_path / "tools.json"
        self.tools: List[dict] = []
        self._call_counts: Dict[str, int] = {}
        self._load_failed = False
        self.load()

    # ---------------- 持久化 ----------------
    def load(self):
        try:
            if self.file.exists():
                from .jsonio import load_json_ex
                data, readable = load_json_ex(self.file, [])
                self._load_failed = not readable
                self.tools = data if isinstance(data, list) else []
            known = {str(t.get("name", "")) for t in self.tools if isinstance(t, dict)}
            missing = [json.loads(json.dumps(t)) for t in DEFAULT_TOOLS
                       if t.get("name") not in known]
            refreshed = 0
            for tool in self.tools:
                if not isinstance(tool, dict):
                    continue
                shipped = next((d for d in DEFAULT_TOOLS
                                if d.get("name") == tool.get("name")
                                and d.get("type") == "builtin"
                                and d.get("builtin") == tool.get("builtin")), None)
                if shipped is None:
                    continue
                if tool.get("description") != shipped.get("description"):
                    tool["description"] = shipped.get("description")
                    refreshed += 1
                if tool.get("parameters") != shipped.get("parameters"):
                    tool["parameters"] = json.loads(json.dumps(shipped.get("parameters")))
                    refreshed += 1
                if "engine" not in tool and "engine" in shipped:
                    tool["engine"] = shipped["engine"]
                    refreshed += 1
            if missing:
                self.tools.extend(missing)
                self.save()
            elif not self.tools or refreshed:
                self.save()
        except Exception as e:
            print(f"加载工具配置失败: {e}")
            self.tools = [json.loads(json.dumps(t)) for t in DEFAULT_TOOLS]

    def save(self):
        if self._load_failed:
            print("工具配置本次未能读取，已跳过保存以免覆盖磁盘上的原有内容。")
            return
        try:
            from .jsonio import save_json
            save_json(self.file, self.tools)
        except Exception as e:
            print(f"保存工具配置失败: {e}")

    # ---------------- 权限与模式 ----------------
    def has_enabled_tools(self) -> bool:
        return any(t.get("enabled") for t in self.tools)

    def get_schema(self) -> List[dict]:
        schema = []
        for t in self.tools:
            if not t.get("enabled"):
                continue
            schema.append({
                "type": "function",
                "function": {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters") or {"type": "object", "properties": {}},
                },
            })
        return schema

    def check_permission(self, tool: dict, user_id: str) -> Tuple[bool, str]:
        if not self.config.get("tools_enabled", False):
            return False, "工具调用功能未开启"
        if not tool.get("enabled"):
            return False, "该工具未启用"
        allowed = tool.get("allowed_users") or []
        if allowed and str(user_id) not in [str(u) for u in allowed]:
            return False, "当前用户无权使用该工具"
        if tool.get("type") == "command" and not self.config.get("tools_allow_commands", False):
            return False, "命令类工具已被全局禁用（tools_allow_commands）"
        return True, ""

    # ---------------- 执行 ----------------
    async def execute(self, name: str, arguments, user_id: str = "",
                      call_counts: dict = None, user_text: str = "") -> Tuple[bool, str]:
        counts = self._call_counts if call_counts is None else call_counts
        tool = next((t for t in self.tools if t.get("name") == name), None)
        if tool is None:
            return False, f"未找到工具: {name}"
        ok, reason = self.check_permission(tool, user_id)
        if not ok:
            return False, reason
        used = counts.get(name, 0)
        limit = int(tool.get("max_calls_per_reply", 3))
        if used >= limit:
            return False, f"工具 {name} 本次回复调用次数已达上限({limit})"
        args = {}
        if isinstance(arguments, str):
            try:
                args = json.loads(arguments) if arguments.strip() else {}
            except Exception:
                return False, f"参数解析失败: {arguments[:100]}"
        elif isinstance(arguments, dict):
            args = arguments
        counts[name] = used + 1
        try:
            ttype = tool.get("type", "builtin")
            if ttype == "builtin":
                return await self._run_builtin(tool.get("builtin", ""), args, tool,
                                               user_text=str(user_text or ""))
            elif ttype == "http":
                return await self._run_http(tool, args)
            elif ttype == "command":
                return await self._run_command(tool, args)
            return False, f"未知工具类型: {ttype}"
        except Exception as e:
            return False, f"工具执行异常: {type(e).__name__}: {e}"

    def begin_reply(self):
        self._call_counts.clear()

    async def _run_builtin(self, builtin: str, args: dict, tool: dict,
                           user_text: str = "") -> Tuple[bool, str]:
        builtin = (builtin or "").lower()
        if builtin == "web_search":
            queries = _pick_queries(args, int(self.config.get("web_search_max_queries", 4) or 4))
            if not queries:
                return False, ("缺少 query 参数，示例：{\"query\": \"人工智能\"}；"
                               "要一次查多个主题可以传 {\"queries\": [\"主题一\", \"主题二\"]}")
            engine = _pick_arg(args, "engine", "engine_name", "搜索引擎")
            if len(queries) == 1:
                return await self._web_search(queries[0], tool, engine)
            return await _run_queries(self, queries, tool, engine,
                                      total_chars=int(self.config.get("web_search_max_chars", 3000) or 3000))
        if builtin == "time":
            return True, _get_cached_time()
        if builtin == "calculate":
            expr = _pick_arg(args, "expression", "formula", "expr", "算式", "公式")
            if not expr:
                return False, "缺少 expression 参数，示例：{\"expression\": \"23*7+sqrt(144)\"}"
            if not (re.search(r"\d", str(expr)) and _CALC_OPERATOR_RE.search(str(expr))):
                # 明确告知，避免它顺着编造计算过程
                return True, (f"无需计算：「{expr}」不是包含数字与运算符的算式。"
                              "请直接以角色身份回应，不要再调用计算工具。")
            try:
                return True, f"{expr} = {_calc_value(expr)}"
            except Exception as e:
                return False, f"计算失败: {e}"
        if builtin == "random":
            lo = float(args.get("min", 0)); hi = float(args.get("max", 1))
            import random as _random
            val = _random.uniform(lo, hi)
            return True, str(int(val) if val.is_integer() else round(val, 6))
        if builtin == "weather":
            city = _pick_arg(args, "city", "location", "name", "place", "城市", "地点")
            lat = args.get("latitude", args.get("lat"))
            lon = args.get("longitude", args.get("lon"))
            try:
                lat = float(lat) if lat not in (None, "") else None
            except Exception:
                lat = None
            try:
                lon = float(lon) if lon not in (None, "") else None
            except Exception:
                lon = None
            resolved, hint = _resolve_weather_city(args, tool, user_text)
            city = resolved
            if not city and (lat is None or lon is None):
                return True, (hint or
                              "对方本条消息里没有说明地点，系统也不知道对方在哪个城市。"
                              "绝不允许自行假设（例如上海/北京）：请直接用角色身份问一句"
                              "「你现在在哪个城市呀？」，不要调用任何工具、"
                              "不要报任何城市的天气。")
            custom = str(tool.get("url", "") or "").strip()
            if custom and "{city}" in custom and city:
                from urllib.parse import quote
                try:
                    out = await self._weather_from_url(
                        custom.replace("{city}", quote(city)), city,
                        int(tool.get("timeout", 10) or 10))
                    return True, out
                except Exception as e:
                    print(f"天气自定义数据源失败，改用备用源: {e}")
            try:
                out = await self._open_meteo_weather(city, lat, lon)
                return True, out
            except Exception as e:
                return False, f"天气查询失败: {type(e).__name__}: {e}"
        if builtin == "web_fetch":
            url = _pick_arg(args, "url", "link", "website", "网址", "链接")
            if url:
                return await _fetch_page(tool, url)
            fallback_query = _pick_arg(args, "query", "q", "keyword", "keywords", "search",
                                       "关键词", "搜索词", "内容", "text", "name", "value")
            if fallback_query:
                return await _fetch_direct(fallback_query, self)
            raw_text = " ".join(str(v) for v in (args or {}).values() if str(v).strip()).strip()
            if raw_text and not _looks_like_url_arg(raw_text):
                return await _fetch_direct(raw_text, self)
            return False, ("缺少 url 参数：web_fetch 只能读取具体网址；"
                           "如果是想按关键词搜索，请改用 web_search（或用 {\"query\": \"关键词\"}）")
        return False, f"未知内置工具: {builtin}"

    async def _current_version(self, query: str, engines: dict, tool: dict) -> str:
        subject = _version_subject(query)
        if not subject:
            return ""
        hit = _VERSION_CACHE.get(subject)
        if hit and time.time() - hit[1] < _VERSION_CACHE_TTL:
            return hit[0]
        key = default_engine_key(self.config)
        if key not in engines:
            return ""
        ok, text = await self._search_one_engine(f"{subject} 最新版本", engines[key], key,
                                                 tool, max_chars=1500)
        if not ok or subject not in str(text):
            return ""
        version = _extract_version(text)
        if version:
            _VERSION_CACHE[subject] = (version, time.time())
            print(f"[搜索] 探测到「{subject}」当前版本 {version}，按这个版本检索")
        return version

    async def _web_search(self, query: str, tool: dict, engine: str = "",
                          max_chars: Optional[int] = None) -> Tuple[bool, str]:
        engines = available_engines(self.config)
        if not engines:
            return False, "没有可用的搜索引擎"
        if _CODE_QUERY_RE.search(str(query or "")) and not _version_subject(query):
            print(f"[搜索] 查询缺少主体，跳过检索：{query!r}")
            return False, ("这条消息只说了要兑换码，没说是哪个游戏或产品："
                           "如果你能从上面的对话确定是哪一个，就用「名字 + 兑换码」再搜一次；"
                           "确定不了就问一句是哪一个，不要拿搜索结果里其他游戏/产品的内容作答。")
        requested = str(engine or tool.get("engine", "") or "").strip()
        explicit = requested in engines
        first = requested if explicit else default_engine_key(self.config)
        custom = [k for k in engines if engines[k].get("custom")]
        rest = [k for k in engines if k != first and k not in custom]
        order = [first] + [k for k in custom if k != first] + rest
        if explicit or not self.config.get("web_search_auto_fallback", True):
            order = [first]
        best: Optional[Tuple[bool, str]] = None
        attempts: List[str] = []
        ask = _with_freshness_hint(query)
        if _VERSION_CTX_RE.search(query) and not _VERSION_RE.search(query):
            version = await self._current_version(query, engines, tool)
            if version:
                ask = f"{version} {query}"
        for key in order:
            ok, text = await self._search_one_engine(query, engines[key], key, tool, max_chars,
                                                     ask_query=ask)
            tag = "" if ok else "[失败]"
            attempts.append(f"{engines[key].get('label', key)}：{'命中' if ok else '未命中'}{tag}")
            if best is None or (ok and not best[0]):
                best = (ok, text)
            if ok:
                break
        if best is not None and not best[0] and self.config.get("web_search_query_rewrite", True):
            for alternative in _query_variants(query):
                ok, text = await self._search_one_engine(alternative, engines[order[0]],
                                                         order[0], tool, max_chars,
                                                         ask_query=_with_freshness_hint(alternative))
                attempts.append(f"改写「{alternative}」：{'命中' if ok else '未命中'}")
                if ok:
                    best = (True, text)
                    break
        assert best is not None
        ok, text = best
        print(f"[搜索] query={query!r}"
              f"{'' if ask == query else f'（实际检索 {ask!r}）'}"
              f" 命中={ok} 尝试过程：{'；'.join(attempts)}")
        if not ok and order:
            text = f"本次搜索未取到可用结果。最后一个引擎的返回：{text}"
        if len(order) > 1:
            text += "\n\n（引擎尝试情况：" + "；".join(attempts) + "）"
        elif len(attempts) > 1:
            text += "\n\n（已尝试换问法重搜：" + "；".join(attempts[1:]) + "）"
        elif not ok:
            text += "\n\n（该引擎没有给出与搜索词真正相关的结果，可换个引擎再试）"
        return ok, text

    async def _search_one_engine(self, query: str, spec: dict, key: str, tool: dict,
                                 max_chars: Optional[int] = None,
                                 ask_query: str = "") -> Tuple[bool, str]:
        ask = str(ask_query or query)
        timeout = float(self.config.get("web_search_timeout", tool.get("timeout", 15)) or 15)
        max_results = max(1, int(self.config.get("web_search_max_results", 8) or 8))
        budget = int(max_chars or 0) or max(200, int(self.config.get("web_search_max_chars", 3000) or 3000))
        max_chars = max(200, budget)
        language = str(self.config.get("web_search_language", "zh") or "zh")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        results, channel_hint = [], ""
        safe_dropped = 0
        pool = max(max_results * 3, 12)
        per_result_chars = max(60, int(self.config.get("web_search_result_chars", 240) or 240))

        api_url = str(spec.get("api_url", "") or "")
        api_key = engine_api_key(self.config, spec.get("api_key_env", "")) if api_url else ""
        if api_url and api_key:
            try:
                api_headers = dict(headers)
                api_headers.update({"Accept": "application/json"})
                builder = spec.get("api_headers")
                if callable(builder):
                    api_headers.update(builder(api_key))
                if str(spec.get("api_method", "GET")).upper() == "POST":
                    body_builder = spec.get("api_json")
                    if callable(body_builder):
                        payload = body_builder(ask, 1, language)
                    else:
                        payload = {"query": ask}
                    payload = _with_api_safe_search(payload, spec, self.config)
                    api_headers["Content-Type"] = "application/json"
                    raw = await self._fetch_text(api_url, api_headers, timeout, params=None,
                                                 method="POST", json_body=payload)
                else:
                    param_builder = spec.get("api_params")
                    params = param_builder(ask, 1, language) if callable(param_builder) else {"q": ask}
                    if isinstance(params, dict):
                        params = {**params, **_safe_api_params(spec, self.config)}
                    raw = await self._fetch_text(api_url, api_headers, timeout, params=params)
                parser = spec.get("parse_json")
                if callable(parser):
                    try:
                        results = parser(json.loads(raw), pool)
                    except (ValueError, TypeError):
                        results = []
                channel_hint = "API"
            except Exception as e:
                print(f"搜索引擎 {key} 接口调用失败，改用网页解析: {type(e).__name__}: {e}")

        if not results:
            request_url = _engine_request_url(spec, ask, 1, language, self.config)
            if not request_url:
                return False, f"搜索引擎「{spec.get('label', key)}」未配置检索地址"
            request_url = await asyncio.to_thread(
                _validate_url,
                request_url,
                allow_private=bool(spec.get("custom") or spec.get("allow_private")),
            )
            try:
                html_text = await self._fetch_text(request_url, headers, timeout, params=None,
                                                   follow_hosts=spec.get("follow_hosts"))
            except httpx.HTTPStatusError as e:
                return False, f"搜索引擎「{spec.get('label', key)}」返回 HTTP {e.response.status_code}"
            except SearchVerifyRequired:
                return False, (f"搜索引擎「{spec.get('label', key)}」要求安全验证（人机校验），"
                               f"本次没有取到结果")
            except ValueError as e:
                return False, f"搜索引擎地址被拒绝: {e}"
            except (httpx.RequestError, httpx.TimeoutException) as e:
                return False, f"搜索请求失败: {type(e).__name__}: {e}"
            blocked = _looks_like_verify_page(html_text)
            parser = spec.get("parse_html")
            results = parser(html_text, pool) if callable(parser) else []
            if not results and blocked:
                return False, (f"搜索引擎「{spec.get('label', key)}」要求安全验证（人机校验），"
                               f"本次没有取到结果")

        before_safe = len(results)
        results = filter_unsafe_results(results, self.config, query)
        safe_dropped = before_safe - len(results)

        results = _demote_degenerate_results(results, query)
        engine_total = len(results) + safe_dropped

        matched = [item for item in results if _result_matches_query(item, query)]
        if matched and len(matched) < len(results):
            print(f"[搜索] 过滤掉 {len(results) - len(matched)} 条与搜索词无关的结果")
            results = matched
        target = ""
        ver_head = re.match(r"^(\d{1,2}\.\d{1,2})\s", ask)
        if ver_head:
            target = ver_head.group(1)
        elif _VERSION_CTX_RE.search(query):
            found = _VERSION_RE.search(query)
            if found:
                target = f"{int(found.group(1))}.{int(found.group(2))}"
        if target:
            same_version = _version_filter(results, target)
            if results and not same_version:
                return False, (f"引擎只返回了其他版本的页面，没有与“{query}”对应版本的内容。")
            if same_version and len(same_version) < len(results):
                print(f"[搜索] 过滤掉 {len(results) - len(same_version)} 条其他版本的结果")
                results = same_version
        subject = _version_subject(query) if _VERSION_CTX_RE.search(query) else ""
        if subject:
            keys = [k for k in re.split(r"[\s\u3000]+", subject) if k]
            same_subject = [it for it in results
                            if all(k in f"{it.get('title', '')}{it.get('content', '')}"
                                   for k in keys)]
            if len(same_subject) < len(results):
                print(f"[搜索] 过滤掉 {len(results) - len(same_subject)} 条与「{subject}」无关的结果")
                if not same_subject:
                    return False, (f"引擎返回的结果都和“{subject}”无关，没有可用内容。")
                results = same_subject
        if _LOOKUP_QUERY_RE.search(query):
            this_year = time.localtime().tm_year
            cutoff = time.time() - 180 * 86400
            fresh = []
            for it in results:
                title = str(it.get("title", ""))
                if any(int(y) < this_year for y in re.findall(r"20\d{2}", title)):
                    continue
                ts = _text_date_ts(f"{title}{it.get('content', '')}")
                if ts is None or ts >= cutoff:
                    fresh.append(it)
            if len(fresh) < len(results):
                print(f"[搜索] 丢弃 {len(results) - len(fresh)} 条过期结果")
                if not fresh:
                    return False, "引擎返回的结果都太旧了，没有可用的最新内容。"
                results = fresh
        if _RECENCY_QUERY_RE.search(query) and not re.search(r"(?:19|20)\d{2}", query):
            results = _sort_by_recency(results)

        lines = []
        redirect_hosts = tuple(spec.get("redirect_hosts") or ())
        if redirect_hosts:
            await self._resolve_result_urls(results, redirect_hosts, headers, timeout)
        head = (f"搜索关键词：{ask}\n搜索引擎：{spec.get('label', key)}"
                f"\n检索时间：{time.strftime('%Y-%m-%d %H:%M')}")
        if subject:
            head += f"\n主体：{subject}（回答里只能用这个主体名，不要换成别的游戏或产品）"
        if channel_hint:
            head += f"（{channel_hint}）"
        must = [x for x in (subject, target) if x]
        relevant = _looks_relevant(query, results)
        selected = results[:max_results]
        verified: List[Tuple[dict, str]] = []
        failed_urls: set = set()
        if relevant:
            verified, failed_urls = await _verify_bodies(selected, query, must_contain=must)
            if failed_urls:
                results = _demote_failed_results(results, failed_urls)
                selected = results[:max_results]
        used = len(head)
        kept = 0
        for item in selected:
            entry = f"{kept + 1}. {item['title']}\n网址: {item['url']}"
            snippet = str(item.get("content", "") or "")[:per_result_chars]
            if snippet:
                entry += f"\n摘要: {snippet}"
            cost = len(entry) + 2
            if kept > 0 and used + cost > max_chars:
                break
            lines.append(entry)
            used += cost
            kept += 1
        if not lines:
            if safe_dropped:
                return False, (f"安全搜索（{safe_search_label(self.config)}）过滤掉了本次搜到的 "
                               f"{safe_dropped} 条结果，没有留下可用内容。"
                               "请换用更中性、更明确的关键词，或在 WebUI 里调整「安全搜索」档位。")
            return False, f"没有找到与“{query}”相关的搜索结果（引擎：{spec.get('label', key)}）。"
        text = head + "\n" + "\n\n".join(lines)
        omitted = len(results) - kept
        if omitted > 0:
            text += (f"\n\n（引擎共返回 {engine_total} 条"
                     f"（其中 {safe_dropped} 条被安全搜索过滤），"
                     f"本次给出 {kept} 条完整结果；"
                     f"其余 {omitted} 条因长度限制省略，摘要未被截断。"
                     f"把搜索词写得更具体，或多传几个 queries 分开搜，可以拿到更多结果。）")
        if safe_dropped:
            text += (f"\n\n（安全搜索：{safe_search_label(self.config)} —— "
                     f"已按安全搜索设置过滤 {safe_dropped} 条成人向/低俗结果。）")
        text += safe_search_hint(self.config)
        print(f"[搜索] 引擎={spec.get('label', key)} query={query!r} "
              f"返回 {len(results)} 条（给出 {kept} 条），相关性判定={relevant}，渠道={channel_hint or '网页'}")
        if not relevant:
            core = _core_lookup(query)
            if _is_short_lookup(core):
                text += (f"\n\n【重要】以上结果里没有任何一条真正包含“{core}”，"
                         f"说明引擎没检索到这个具体名字/词条，返回的只是它拆出来的普通词条。"
                         f"请如实告诉用户“没有查到「{core}」的资料”，"
                         f"绝不能把这些词条的内容当成「{core}」本人的信息来介绍或编造。")
            else:
                text += (f"\n\n【重要】以上结果几乎没有真正包含“{query}”的内容，"
                         f"属于引擎放宽后的近似结果。回答时要说明这是近似资料，"
                         f"不要当作该主题的确切事实。")
        shown_urls = {item.get("url") for item in selected[:kept]}
        bodies = [(item, body) for item, body in verified if item.get("url") in shown_urls][:3]
        bad_shown = [i + 1 for i, item in enumerate(selected[:kept])
                     if item.get("url") in failed_urls]
        if bad_shown:
            text += ("\n\n【注意】第 " + "、".join(str(n) for n in bad_shown)
                     + " 条结果抓回的正文与所问主题对不上，属于摘要与正文不符的标题党页面："
                       "不要把它们标题或摘要里的内容当事实复述；"
                       "下面如有【网页正文】，以核对过的正文为准，没有佐证就说没查到。")
        if bodies:
            parts = []
            for item, body in bodies:
                host = (urlparse(str(item.get("url", ""))).hostname or "").lower()
                note = (f"（正文里可能同时列出多个主体，只采用与「{must[0]}」相关的内容）\n"
                        if must else "")
                parts.append(f"【网页正文】{item.get('title', '')}\n（来源：{host}）\n{note}{body}")
            text += "\n\n" + "\n\n".join(parts)
            if _LOOKUP_QUERY_RE.search(query):
                hosts = {urlparse(str(item.get("url", ""))).hostname or "" for item, _ in bodies}
                if len(hosts) >= 2:
                    text += ("\n\n【多源核对】以上正文来自不同网站：兑换码、价格、比分这类关键数据，"
                             "只有两个及以上不同网站都写到的才算可靠；只在一个网站出现的，"
                             "很可能是引流页编造的假数据，不要直接当答案，"
                             "确实只有单一来源时要说明来源单一、真实性待确认。")
                else:
                    text += ("\n\n【多源核对】上面正文只有单一来源，无法交叉核对："
                             "其中的兑换码/价格等关键数据可能是引流页编造的假信息，"
                             "给出时要说明只有一处来源、真实性待确认。")
            print(f"[搜索] 已回源核对 {len(parts)} 条结果的正文")
        return relevant, text

    async def _resolve_result_urls(self, results: list, redirect_hosts: tuple,
                                   headers: dict, timeout: float) -> None:

        async def _one(item: dict) -> None:
            url = item.get("url", "")
            host = urlparse(url).hostname or ""
            if not any(host == h or host.endswith("." + h) for h in redirect_hosts):
                return
            try:
                async with httpx.AsyncClient(timeout=min(timeout, 6), trust_env=False,
                                             follow_redirects=True,
                                             verify=verified_context()) as client:
                    resp = await client.get(url, headers=headers)
                    final = str(resp.url)
                    resolved = "" if _is_verify_url(final) else final
                    if not resolved or resolved == url:
                        resolved = _unwrap_meta_redirect(resp.text)
                if resolved and resolved != url and not _is_verify_url(resolved):
                    item["url"] = resolved
            except Exception:
                return

        await asyncio.gather(*[_one(item) for item in results[:10]], return_exceptions=True)

    async def _fetch_text(self, url: str, headers: dict, timeout: float, params=None,
                          method: str = "GET", json_body=None, follow_hosts=None) -> str:
        template_host = urlparse(url).hostname
        allowed = {template_host} | {h for h in (follow_hosts or ()) if h}
        current = url
        current_params = params
        current_method = method
        async with httpx.AsyncClient(timeout=timeout, trust_env=False,
                                     follow_redirects=False,
                                     verify=verified_context()) as client:
            for _ in range(4):
                if current_method == "POST":
                    resp = await client.post(current, json=json_body, headers=headers,
                                             params=current_params)
                else:
                    resp = await client.get(current, headers=headers, params=current_params)
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location", "")
                    if not location:
                        raise ValueError("搜索服务重定向地址为空")
                    target = urlparse(urljoin(current, location))
                    if _is_verify_url(target.geturl()):
                        raise SearchVerifyRequired("搜索引擎要求安全验证")
                    if target.hostname not in allowed:
                        raise ValueError(f"搜索服务重定向到其他主机已拒绝: {target.hostname}")
                    current, current_params = target.geturl(), None
                    if resp.status_code == 303:
                        current_method = "GET"
                        json_body = None
                    continue
                resp.raise_for_status()
                return resp.text
        raise ValueError("搜索服务重定向次数过多")

    async def _weather_from_url(self, url: str, city: str, timeout: int) -> str:
        url = await asyncio.to_thread(_validate_url, url)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False,
                                     follow_redirects=True,
                                     verify=verified_context()) as client:
            resp = await client.get(url, headers={"User-Agent": "curl/8.0"})
            resp.raise_for_status()
            data = resp.json()
        cur = (data.get("current_condition") or [{}])[0]
        desc = ""
        try:
            desc = (cur.get("lang_zh", [{}])[0].get("value")
                    or cur.get("weatherDesc", [{}])[0].get("value", ""))
        except Exception:
            pass
        return (f"{city} 当前天气: {desc}, 气温 {cur.get('temp_C', '?')}°C, "
                f"体感 {cur.get('FeelsLikeC', '?')}°C, 湿度 {cur.get('humidity', '?')}%")

    async def _open_meteo_weather(self, city: str, lat=None, lon=None) -> str:
        async with httpx.AsyncClient(timeout=15, trust_env=False,
                                     follow_redirects=True,
                                     verify=verified_context()) as client:
            name = city or "当前位置"
            if lat is None or lon is None:
                best = None
                for q in _geo_queries(city):
                    geo = await client.get("https://geocoding-api.open-meteo.com/v1/search",
                                           params={"name": q, "count": 5,
                                                   "language": "zh", "format": "json"})
                    try:
                        geo.raise_for_status()
                    except Exception:
                        continue
                    cand = _best_geo(geo.json().get("results") or [])
                    if cand is None:
                        continue
                    if best is None or int(cand.get("population") or 0) > int(best.get("population") or 0):
                        best = cand
                if best is None:
                    raise RuntimeError(f"未找到城市: {city}")
                lat, lon = best["latitude"], best["longitude"]
                name = best.get("name") or city
            wx = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={"latitude": lat, "longitude": lon, "timezone": "auto",
                        "current": "temperature_2m,relative_humidity_2m,"
                                   "apparent_temperature,weather_code"})
            wx.raise_for_status()
            cur = wx.json().get("current") or {}
        code = cur.get("weather_code")
        desc = _WMO_ZH.get(code, f"天气代码 {code}")
        return (f"{name} 当前天气: {desc}, 气温 {cur.get('temperature_2m', '?')}°C, "
                f"体感 {cur.get('apparent_temperature', '?')}°C, "
                f"湿度 {cur.get('relative_humidity_2m', '?')}%")

    async def _run_http(self, tool: dict, args: dict) -> Tuple[bool, str]:
        url_tpl = tool.get("url", "")
        if not url_tpl:
            return False, "未配置 url"
        template_url = await asyncio.to_thread(_validate_url, url_tpl, True)
        template_host = urlparse(template_url).hostname
        url = template_url
        for k, v in args.items():
            url = url.replace("{" + str(k) + "}", str(v))
        method = str(tool.get("method", "GET")).upper()
        timeout = float(tool.get("timeout", 10))
        headers = tool.get("headers") or {}
        max_chars = int(tool.get("max_chars", 800))
        parsed_url = urlparse(url)
        if parsed_url.hostname != template_host:
            return False, "HTTP 工具不允许通过参数修改目标主机"
        url = await asyncio.to_thread(_validate_url, url, True)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False,
                                     verify=verified_context()) as client:
            if method == "POST":
                resp = await client.post(url, json=args, headers=headers)
            else:
                resp = await client.get(url, headers=headers)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location", "")
                if not location:
                    return False, "HTTP 工具重定向地址为空"
                redirect_url = urlparse(location)
                if redirect_url.hostname and redirect_url.hostname != template_host:
                    return False, "HTTP 工具重定向到其他主机已拒绝"
                return False, (f"目标返回重定向({resp.status_code})，自定义 HTTP 工具不跟随跳转，"
                               "请把工具 url 直接改为重定向后的最终地址")
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if "json" in ctype:
                try:
                    body = json.dumps(resp.json(), ensure_ascii=False)
                except Exception:
                    body = resp.text
            else:
                body = _strip_html(resp.text)
        return True, body[:max_chars]

    async def _run_command(self, tool: dict, args: dict) -> Tuple[bool, str]:
        cmd_tpl = tool.get("command", "")
        if not cmd_tpl:
            return False, "未配置 command"
        cmd = str(cmd_tpl)
        for k, v in args.items():
            cmd = cmd.replace("{" + str(k) + "}", str(v))
        if "{" in cmd or "}" in cmd:
            return False, "命令参数不完整"
        try:
            argv = shlex.split(cmd, posix=os.name != "nt")
        except ValueError as e:
            return False, f"命令格式错误: {e}"
        if not argv:
            return False, "未配置 command"
        timeout = float(tool.get("timeout", 20))
        max_chars = int(tool.get("max_chars", 800))

        executable = shutil.which(argv[0])
        if executable:
            argv[0] = executable
        elif os.name != "nt" or not os.environ.get("COMSPEC"):
            return False, f"找不到命令: {argv[0]}"
        elif any(re.search(r"[&|<>^()\r\n]", str(arg)) for arg in argv[1:]):
            return False, "命令参数包含不允许的 shell 控制字符"

        def _run():
            creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
            if executable:
                return subprocess.run(argv, shell=False, capture_output=True,
                                      timeout=timeout, creationflags=creationflags)
            quoted = subprocess.list2cmdline(argv)
            return subprocess.run([os.environ["COMSPEC"], "/d", "/s", "/c", quoted],
                                  shell=False, capture_output=True,
                                  timeout=timeout, creationflags=creationflags)

        try:
            proc = await asyncio.to_thread(_run)
        except subprocess.TimeoutExpired:
            return False, f"命令超时({timeout}s)"
        stdout = _decode_console_output(proc.stdout)
        stderr = _decode_console_output(proc.stderr)
        output = stdout + ("\n" + stderr if stderr.strip() else "")
        return proc.returncode == 0, output.strip()[:max_chars] or "(无输出)"
