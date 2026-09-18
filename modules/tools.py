"""工具调用（Function Calling）：内置工具 + 自定义 HTTP/命令工具，全部可在 WebUI 配置与授权。

工具定义存于 data/tools.json（WebUI「高级功能-工具调用」页可视化编辑）：
  {
    "name": "get_weather",
    "type": "builtin|http|command",
    "description": "给 LLM 看的功能描述",
    "parameters": {JSON Schema},
    "enabled": true,
    "allowed_users": [],           # 空 = 所有用户可用；填写 QQ 号则仅这些用户可触发
    "max_calls_per_reply": 1,      # 单次回复内最多调用次数
    # http 类型专用
    "url": "https://.../?city={city}",
    "method": "GET",
    "timeout": 10,
    # command 类型专用
    "command": "python {script} --arg {value}",
    # builtin 类型专用
    "builtin": "time|calculate|random|weather|web_fetch"
  }

权限控制：全局开关 tools_enabled → 工具级 enabled → 用户白名单 allowed_users
→ 单回复调用次数限制 → 命令类工具需额外打开 tools_allow_commands。
"""
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
        "description": "查询某个城市的当前天气。【必须调用】用户询问**明确地点**的天气、气温、温度、降雨、下雨、下雪、要不要带伞等与天气相关的话题时，必须调用本工具并把用户问题里的城市名原样传给 city，严禁凭常识或印象回答。city 只能填【用户本条消息里真的出现过的城市名】或下面【默认城市】里配置的城市；如果用户没说是哪里、而你也不知道主人在哪个城市，绝对不要自己猜（严禁拿上海/北京等任何城市顶替），此时不要调用本工具，直接以角色身份问一句主人在哪个城市。",
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
        "description": "用关键词在互联网上搜索资料。【必须调用】只要用户消息表达出以下任一意图，都必须调用本工具搜索后再回答，绝不能凭记忆、凭常识直接作答，也不能以“我没有联网”之类的借口推托：① 明确的搜索请求（搜/搜索/搜一下/查一下/查找/查查/帮我搜/帮我查/找一下/找找/看看有没有）；② 询问新闻、时事、最新消息、最新进展、近期发生的事；③ 询问具体人物/作品/产品/事件/名词/名词解释（如“xx是什么”“xx是谁”“xx怎么样”“xx有没有出”“你知道xx吗”）；④ 询问价格、评价、推荐、攻略、教程；⑤ 询问任何你不确定或不知道答案的问题（此时必须先搜再答，不确定就承认不确定，绝不能编造）。query 必须严格取自【当前这一条用户消息】的核心搜索意图，用最简洁的关键词表述（例如用户说“帮我搜搜今天有什么新闻呗”就填“今天的新闻”；用户说“A游戏好玩吗”就填“A游戏 评价”），绝对不允许复制、沿用或参考历史对话中曾经出现过的旧搜索词。可选 engine 指定搜索引擎（bing/baidu/360/sogou 等，不填用默认引擎）；只有需要打开某个搜索结果看全文时，才接着用 web_fetch。",
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


# 时间工具短 TTL 缓存：模型常在一条回复里反复调用当前时间工具，一分钟内
# 重复查询没有意义，直接复用结果
_TIME_TOOL_CACHE = {"ts": 0.0, "value": ""}

# 合法算式：至少含一个数字和一个运算符（含中文运算词）。没有运算符说明模型
# 只是在无关内容里幻觉出"我算了一下"，照常执行只会助长它胡说
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
    """从文本中提取 http(s) 链接（去重、保序、剥离尾部标点）。"""
    out: List[str] = []
    for raw in re.findall(r"https?://[^\s<>\"'）)】\]]+", str(text or ""), re.I):
        url = raw.rstrip(".,;:!?。，、；：！？")
        if url and url not in out:
            out.append(url)
        if len(out) >= max(1, int(limit)):
            break
    return out


def _looks_like_url_arg(text: str) -> bool:
    """判断一段裸文本是不是网址（含 www. 开头与裸域名）。"""
    text = str(text or "").strip()
    if not text or re.search(r"\s", text):
        return False
    if re.match(r"^(?:https?://|www\.)\S+$", text, re.I):
        return True
    return bool(re.match(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|cn|net|org|io|dev|ai|edu|gov|me|top|xyz|info|co)(?:[/:?#]\S*)?$",
                         text, re.I))


# ------------------------- 搜索引擎注册表 -------------------------
# 每个引擎一个 spec：
#   url_template  HTML 检索地址模板，{query} 查询词 / {page} 页码
#   build_params  JSON 接口参数构造（返回 None 表示不支持 JSON 接口）
#   parse_json    JSON 响应 -> [{title, url, snippet}]
#   parse_html    HTML 响应 -> [{title, url, snippet}]
#   api_key_env   官方 API key 环境变量（有 key 优先走接口，无 key 回退 HTML）
#   api_url       官方 API 地址
#   api_headers   官方 API 认证头
#   resolve_redirect  结果里的跳转链接是否需要在返回时解析成真实地址
# 新增引擎只需在这里加一项即可，WebUI 下拉框、文案、测试都会自动带上。

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
    """把搜索词拆成用于相关性判断的词元（中文按 2 字滑窗，避免整句比对）。"""
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
    """从"李祖祥是谁"这类问句里取出真正要找的对象（"李祖祥"），用于提示与相关性判断。

    直接拿整句去判"有没有命中"会把正常结果判成不相关（结果里写的是名字，不会写"是谁"），
    提示里也不必把整个问题重复三遍。
    """
    text = re.sub(r"[\s\u3000]+", " ", str(query or "")).strip()
    text = re.sub(r"^(帮我|请|麻烦|给我)?\s*(搜索|搜一下|搜搜|搜|查一下|查查|查|找一下|找找|找)\s*(一下|下|一哈)?\s*", "", text)
    text = re.sub(r"(是谁|是什么人|是什么|怎么样|如何|的资料|的介绍|的信息|的意思|在哪里|是哪里|吗|呢|？|\?)+$", "", text)
    text = text.strip(" 　，,。.、:：;；\"'“”")
    return text or str(query or "").strip()


def _is_short_lookup(query: str) -> bool:
    """短查询（人名/专有名词，如"李祖祥"）需要整体命中，才允许说"搜到了"。"""
    text = re.sub(r"[\s\u3000]+", "", str(query or ""))
    return 0 < len(text) <= 8


def _looks_relevant(query: str, results: list, minimum: int = 1) -> bool:
    """判断这批结果是否真的对上了搜索词。

    事故背景：中文姓名会被引擎悄悄放宽成单字命中——搜"李祖祥"，Bing 返回的全是
    "李（汉语汉字）""李姓"这类词条，一条都不含"李祖祥"（实测确认：请求没问题，
    是 Bing 对这个名字没有索引，于是退化成姓氏词条）。引擎返回了结果 ≠ 这些结果
    回答了问题；若这里判为"没命中"，上层会换引擎重试，并在结果里写明"没有真正包含
    搜索词的条目"，模型才不会把"李姓的百科"当成"李祖祥"的资料编出一段介绍。

    短查询按**整体**判断（人名必须整个出现）；长问句按词元判断，避免误杀正常结果。
    """
    if not results:
        return False
    core = re.sub(r"[\s\u3000]+", "", _core_lookup(query)).lower()
    if not core:
        return True
    minimum = max(1, int(minimum))
    if _is_short_lookup(core):
        # 短查询（人名、专有名词）：整体出现最理想；否则看它拆出的相邻二字片段
        # 有几个真的出现——"李祖祥"在"李（汉语汉字）"里 0 个，判为没命中；
        # "量子纠缠"遇到只写"量子"的资料时仍有 1 个命中，不会被误杀。
        # 命中门槛是 1 条：只要有一条真的对上了就算搜索成功（要求 2 条会把
        # "只有一条结果真的相关"的正常情况误判成失败，进而白白多跑一遍引擎）。
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
    # 长问句用更高的命中门槛（2 条）：避免"只要有一条沾边"就给结果加上
    # "资料不可靠"的提示，反而让模型不敢回答。
    hits = sum(1 for item in results if _result_matches(item, terms))
    return hits >= max(2, int(minimum))


def _query_variants(query: str, limit: int = 2) -> List[str]:
    """给短查询（人名/专有名词）准备"更像在找这个实体"的改写版本。

    事故背景：搜"李祖祥"，引擎会把三个字拆开、退化成"李（汉语汉字）""李姓"这类词条，
    一条都不含名字本身。同一个引擎换个问法（"李祖祥 个人资料"）往往就能命中，
    所以不相关时先用改写版本重试一次，再考虑换引擎。
    """
    raw = str(query or "").strip()
    core = re.sub(r"[\s\u3000]+", "", str(_core_lookup(raw) or ""))
    if not (2 <= len(core) <= 8):
        return []
    # 注意：Python 的 \w 也匹配中文，所以"像网址"的判断必须限定 ASCII，
    # 否则"李祖祥"会被当成域名而被跳过（这个坑实际踩到过）。
    if _looks_like_url_arg(core) or re.match(r"^[A-Za-z0-9.\-:/]+$", core):
        return []
    out: List[str] = []
    for suffix in ("是谁", "个人资料", "介绍"):
        candidate = f"{core} {suffix}"
        if candidate != raw and candidate not in out:
            out.append(candidate)
    return out[:max(0, int(limit))]


def _looks_like_verify_page(html_text: str) -> bool:
    """识别"HTTP 200 但其实是人机校验页"的响应。

    事故背景：搜狗等引擎触发风控时返回 200 + 一个校验页（含 verify.css / anti.min.js），
    解析器自然一条结果都拿不到，工具于是回"没有找到结果"——用户会以为真的没有资料，
    而不是"被拦了、换个引擎就好"。这里按页面特征认出来并明确报错。
    """
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
    """搜索引擎把我们跳到了安全验证页（人机校验），不是地址错误。"""


def _real_url(url: str) -> str:
    """把搜索引擎跳转链接里的真实地址掏出来。"""
    parsed = urlparse(str(url or ""))
    if parsed.hostname and parsed.hostname.endswith("baidu.com") and parsed.path.startswith("/link"):
        target = (parse_qs(parsed.query).get("url") or [""])[0]
        if target.startswith("http"):
            return target
    return url


def _unwrap_meta_redirect(html_text: str) -> str:
    """从跳转页里读出真实地址：JS replace / meta refresh / 直链。

    搜狗等引擎返回的是 200 + 一段跳转脚本（`window.location.replace("真实地址")`），
    HTTP 层看不到 30x，需要在返回文本里再挖一次，否则模型只能拿到 /link?url=…
    这种自己打不开的地址。
    """
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


# 常见站点后缀：用于把"以_百度百科"这类标题还原成正文主体"以"
_SITE_SUFFIX_RE = re.compile(
    r"[\s_\-–—·|]*(?:百度百科|维基百科|wikipedia|百度知道|百度文库|百度经验|抖音百科|快懂百科|搜狗百科|360百科|知乎|豆瓣|萌娘百科|哔哩哔哩|bilibili|github|游侠网|游民星空|3dm)\s*$",
    re.I)


def _title_core(title: str) -> str:
    """去掉标题尾部的站点名，得到页面的主体词（"以_百度百科"→"以"）。"""
    text = str(title or "").strip()
    while True:
        stripped = _SITE_SUFFIX_RE.sub("", text).strip()
        if stripped == text:
            return text
        text = stripped


def _demote_degenerate_results(results: list, query: str) -> list:
    """把"引擎拆字退化"出来的单字/双字词条沉到结果列表末尾（不删除）。

    事故背景：搜"以恋结缘 歌词"，引擎把首字"以"单独的百科词条排在第 1 位——
    模型最先读到的就是垃圾结果，还占掉搜索字符预算。判定标准：标题去掉站点
    后缀后，只剩搜索核心词的 1~2 字**真前缀**（查询"以恋结缘…"，标题主体只剩
    "以"），即是拆字词条。

    只在列表里存在正常结果时沉底；整批全是拆字词条（如搜生僻人名只返回
    "李（汉语汉字）""李姓"）则保持原样——那时"没查到"的相关性提示需要这些
    词条在场才能如实说明引擎到底返回了什么。
    """
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
    """自定义引擎的兜底解析：抓正文里的标题链接。"""
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
    """供"网页解析"分支使用：SearXNG 返回的其实是 JSON 文本，这里解码后再解析。

    SearXNG 没有官方 API key，tools.py 里两条分支（API 分支 / 网页解析分支）
    它永远只能走后者，而后者的解析器取的是 spec["parse_html"]。之前只注册了
    parse_json，于是解析器为 None、结果被静默丢弃 —— HTTP 200、拿到 17KB JSON、
    却报"没有找到与…相关的搜索结果"。这个包装函数把 JSON 文本接上同一个解析器。
    """
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
        # 【必须有】SearXNG 没有 api_url/api_key，所以它永远走"网页解析"那条分支；
        # 而那个分支只认 spec["parse_html"]，解析器为 None 时结果会被**静默丢掉**，
        # 表现为 HTTP 200、拿到 17KB JSON、却报"没有找到相关结果"。
        # 把 parse_html 指向同一个 JSON 解析器，"网页解析"分支就能正确解析 JSON。
        "parse_html": _searxng_parse_html_text,
        "url_env": ("web_search_url",),
        # 自建 SearXNG 最常见就是跑在本机 127.0.0.1，这正是 SSRF 防护会拦下的地址。
        # 把它标记为"允许本机/内网"，否则填 http://127.0.0.1:8888/search 会直接抛
        # ValueError: 禁止访问本机、内网或保留地址。
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
    """返回地址模板缺少 {query} 的自定义引擎别名。

    这类模板拿不到搜索词，请求会带着空查询去检索、返回一堆无关结果，
    而且不会有任何提示；必须在保存时就拦下来。
    """
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
    """自定义引擎：`别名=地址模板`，每行一个（模板用 {query} / {page}）。"""
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
    """内置引擎 + 配置文件里的自定义引擎（每项都带上自己的 key，供安全搜索等按引擎定制）。"""
    engines = {k: {**v, "key": k} for k, v in SEARCH_ENGINES.items()}
    engines.update(_parse_custom_engines((config or {}).get("web_search_custom_engines", "")))
    return engines


def engine_labels(config) -> List[tuple]:
    return [(key, spec.get("label", key)) for key, spec in available_engines(config).items()]


def default_engine_key(config) -> str:
    """当前默认引擎：显式配置优先，否则取注册表顺序里的第一个。

    不做"必须叫 bing"这类硬编码——新增/删除引擎后默认值自动跟着注册表走。
    """
    engines = available_engines(config)
    if not engines:
        return ""
    wanted = str((config or {}).get("web_search_engine", "") or "").strip()
    return wanted if wanted in engines else next(iter(engines))


def parse_api_keys(raw) -> dict:
    """解析 API key 配置：每行 `ENV_NAME=值`（也兼容 JSON 对象）。"""
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
    """引擎的 API key：配置文件里的 web_search_api_keys 优先，其次环境变量。"""
    if not env_name:
        return ""
    configured = parse_api_keys((config or {}).get("web_search_api_keys", {}))
    return str(configured.get(env_name, "") or os.environ.get(env_name, "") or "").strip()


def _engine_request_url(spec: dict, query: str, page: int, language: str, config) -> str:
    template = str(spec.get("url", "") or "")
    for key in spec.get("url_env", ()):  # 旧的 web_search_url 仍可覆盖默认地址
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
    # 安全搜索参数：同一个 key 可能带多个值（如 tbs=explicitOff 与 tbs=qdr:w 并存）
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
#   normal —— 允许搜到 R16+ 的站点，但不允许 R18 内容（默认）
#   strict —— 连低俗、性暗示的站点与内容一并过滤
SAFE_SEARCH_LEVELS = ("off", "normal", "strict")
_SAFE_SEARCH_LABELS = {"off": "无", "normal": "一般", "strict": "严格"}

# 引擎级安全搜索参数：level -> {参数名: 值}
_ENGINE_SAFE_PARAMS = {
    "searxng": {"off": {"safesearch": "0"}, "normal": {"safesearch": "1"},
                "strict": {"safesearch": "2"}},
    "bing": {"off": {"adlt": "off"}, "normal": {"adlt": "moderate"},
             "strict": {"adlt": "strict"}},
    "bing-en": {"off": {"adlt": "off"}, "normal": {"adlt": "moderate"},
                "strict": {"adlt": "strict"}},
    "google-news": {"off": {}, "normal": {"safe": "active"}, "strict": {"safe": "active"}},
}

# 成人向（一般档就拦）：成人站点、成人品牌、明确的成人内容描述。
# 刻意不收「成人」「色」「黄」「性」「av」「sm」这类单字/短词——「成人高考」「裸眼视力」
# 「Java」「smart」「性教育」都会被它们误伤。
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
# 成人聚合站 / 黑料站 / 里番站站名（一般档就拦）
_ADULT_SITES = (
    "91爆料", "91porn", "91av", "91视频", "91国产", "91制片厂", "51吃瓜",
    "吃瓜网", "黑料网", "黑料不打烊", "爆料网", "草榴", "1024社区", "t66y",
    "色花堂", "sehuatang", "麻豆传媒", "麻豆映画", "天美传媒", "精东影业",
    "蜜桃传媒", "星空传媒", "果冻传媒", "糖心vlog", "糖心传媒", "国产精品",
    "精品国产", "自拍偷拍", "偷拍自拍", "里番", "里番库", "91传媒"
)
# 低俗 / 性暗示 + 露骨但属客观词的词（只有严格档才拦）。
# 客观词（性交/性爱/自慰/性行为…）放严格档：查医学、性教育资料时一般档还能用。
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
    "野外露出", "野战", "群P", "多人性行为", "多人性交", "多人性爱", "车震", "激情床戏",
    "摸奶", "摸胸", "操逼", "淫穴"
)
# 成人站域名：一般档起拦（只在网址里匹配）
_ADULT_DOMAINS = (
    "91porn", "91av", "91zuida", "51chigua", "chigua", "heiliao", "madou",
    "sehuatang", "t66y", "xvideo", "pornhub", "missav", "javbus", "javdb",
    "hanime", "nhentai", "rule34", "onlyfans", "chaturbate", "cam4",
    "spankbang", "eporner", "agedm", "asmr18",
)


def safe_search_level(config) -> str:
    """当前安全搜索档位；非法/缺失一律按「一般」处理。"""
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
        return {}          # 自定义地址 / 官方 API：参数由对方约定，用关键词过滤兜底
    mapping = _ENGINE_SAFE_PARAMS.get(str(spec.get("key", "") or "").lower())
    if not mapping:
        # 未登记参数的引擎（百度/360/搜狗…）走它们通用的站点级过滤参数
        return {"safe": "1"} if level == "strict" else {}
    return dict(mapping.get(level, {}))


def _safe_search_terms(level: str) -> tuple:
    """该档位的内置词表；严格档一定包含一般档的全部内容。"""
    if level == "off":
        return ()
    if level == "strict":
        return _ADULT_TERMS + _ADULT_SITES + _SUGGESTIVE_TERMS
    return _ADULT_TERMS + _ADULT_SITES


_WORD_SPLIT_RE = re.compile(r"[\n\r,，、;；]+")


def _split_words(raw) -> tuple:
    """把词表文本切成小写去重的词元（换行 / 逗号 / 顿号 / 分号分隔）。"""
    out = []
    for chunk in _WORD_SPLIT_RE.split(str(raw or "")):
        word = chunk.strip().lower()
        if word and word not in out:
            out.append(word)
    return tuple(out)


def extra_block_terms(config) -> tuple:
    """配置里用户自己加的过滤词，一般档与严格档都拦。"""
    return _split_words((config or {}).get("web_search_block_words", ""))


def extra_allow_terms(config) -> tuple:
    """配置里用户自己加的白名单词：命中就放行，优先于黑名单与域名判断。"""
    return _split_words((config or {}).get("web_search_allow_words", ""))


def _adult_domain(url: str) -> str:
    low = str(url or "").lower()
    return next((d for d in _ADULT_DOMAINS if d in low), "")


def filter_unsafe_results(results: list, config, query: str = "") -> list:
    """按安全搜索档位过滤搜索结果；off 档不做任何过滤。"""
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
        # 白名单优先：命中就放行，不再看黑名单与域名
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
    """给模型看的安全搜索说明（只在非 off 档追加）。"""
    level = safe_search_level(config)
    if level == "off":
        return ""
    if level == "strict":
        return ("\n\n（安全搜索：严格 —— 已过滤低俗、性暗示与成人向内容；"
                "如结果偏少可换用更中性的关键词。）")
    return ("\n\n（安全搜索：一般 —— 已过滤成人向（R18）内容；"
            "如结果偏少可换用更中性的关键词。）")


# 官方 API 的安全搜索参数（字段名各不相同）
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
    """把安全搜索参数并进 JSON 请求体（字段名按各家接口约定）。"""
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
# 天气地点校验：用户没说城市时不许模型自己猜一个
# ---------------------------------------------------------------------------
# 事故背景：用户只问"今天天气怎么样"，本地模型随手把 city 填成"上海"，
# 于是回出"上海的天气…很适合…！"——凭空替主人搬到了另一个城市。
# 现在：city 必须能在【用户本条消息】里找到，或用配置里明确写下的默认城市，
# 否则不查天气，直接把"问主人在哪"的结论交给模型。
_WEATHER_INTENT_RE = re.compile(
    r"天气|气温|温度|下雨|降雨|带伞|雨伞|下雪|降雪|台风|冷不冷|热不热|穿什么|要穿|"
    r"多少度|几度|(?<![A-Za-z])weather(?![A-Za-z])|(?<![A-Za-z])forecast(?![A-Za-z])|"
    r"(?<![A-Za-z])temperature(?![A-Za-z])|(?<![A-Za-z])rain(?![A-Za-z])", re.I)

# 地点后缀：带这些字且不是泛指时，认为用户在指某个地方
_CITY_SUFFIXES = ("市", "县", "区", "州", "盟", "旗", "镇")
_VAGUE_PLACES = ("这里", "这边", "那里", "那边", "本地", "当地", "我这", "你那",
                 "家里", "外面", "附近", "外面", "国内", "国外")
# 常见城市名（判"用户没提地点却在聊天气"时的兜底依据；不追求全，够用即可）
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
# 这些字只可能是语气/助词/疑问词，收尾就说明这串不是地名
_NON_CITY_CHARS = set("了的地得吗呢吧啊呀哦喔嘛么啥谁哪怎")
# 拉丁词元里不可能是地名的常见疑问/功能词
_EN_NON_CITY = {"what", "how", "when", "where", "which", "who", "why", "is", "are",
                "the", "in", "at", "of", "today", "tomorrow", "please", "tell", "me",
                "weather", "forecast", "temperature", "rain"}


def _is_weather_question(text: str) -> bool:
    return bool(_WEATHER_INTENT_RE.search(str(text or "")))


def _clean_city_token(token: str) -> str:
    """把"北京的""北京今天要"这类粘连词元修剪成"北京"。"""
    t = str(token or "").strip().strip("的了地得吗呢吧啊呀哦喔嘛么")
    for _ in range(6):
        before = t
        if not t:
            break
        if t in _VAGUE_PLACES:
            return ""
        # 剥掉粘连在首/尾的常见词（"今天""要""我在""主人"…），但必须留下 >=2 个字
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
    """一个词元看起来是不是具体地名（而不是泛指或天气词汇）。"""
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
        # 含非汉字：按纯拉丁地名处理，但排除功能词（Tokyo / New York）
        return bool(re.fullmatch(r"[A-Za-z][A-Za-z\s\-]{1,20}", t))
    # 「XX市/XX县/XX区/XX州」这类带后缀的写法直接算地名
    if t.endswith(_CITY_SUFFIXES):
        return len(cjk) >= 2
    # 其余（含"XX今天"这种粘连）：没命中已知城市就一律不算，
    # 宁可让角色回问一句"主人在哪个城市"，也不能猜错城市报错天气
    return False


def _extract_city_from_text(text: str) -> str:
    """从用户消息里找出用户说出的城市名；找不到返回空串（= 不许猜）。"""
    from .llm_helpers import strip_quote_note   # 延迟导入，避免模块级循环依赖
    body = strip_quote_note(str(text or ""))
    body = _WEATHER_INTENT_RE.sub(" ", body)
    for token in re.split(r"[\s,，。.、；;：:！!？?～~（）()【】\[\]\"'“”‘’]+", body):
        token = token.strip()
        if _looks_like_city(token):
            return _clean_city_token(token)
    return ""


def _resolve_weather_city(args: dict, tool: dict, user_text: str) -> Tuple[str, str]:
    """确定本次天气查询的城市；**以用户本条消息为准**，绝不允许模型自己猜。

    返回 (city, hint)：city 为空表示"不知道地点"，hint 是给模型的说明。
    """
    city = _pick_arg(args, "city", "location", "name", "place", "城市", "地点")
    default = str((tool or {}).get("default_location", "") or "").strip()
    text = str(user_text or "")
    said = _extract_city_from_text(text)
    if not text:
        # 拿不到用户消息（工具测试等场景）：只能信任模型给的参数
        return city or default, ""
    if said:
        return said, ""          # 用户明确说了地点：以用户说的为准
    if _is_weather_question(text):
        msg = ("主人本条消息里没有说明地点，你也不知道主人在哪个城市。"
               "绝不允许自行假设（例如上海/北京）报出任何城市的天气："
               "请直接用角色身份问一句「主人现在在哪个城市呀？」，"
               "不要调用任何工具、不要提任何城市名。")
        if city or default:
            print(f"[工具] 天气：模型想用「{city or default}」，但用户本条消息没说地点"
                  f"（消息={text[:60]!r}），已拦下改为反问地点")
        else:
            print(f"[工具] 天气：用户没给地点（消息={text[:60]!r}），已拦下查询并让角色反问地点")
        return "", msg
    # 不是天气问题：允许用配置的默认城市，否则仍不猜
    if default:
        return default, ""
    if city:
        print(f"[工具] 天气：模型给的城市「{city}」不在用户本条消息里，"
              f"判定为模型自行猜测，已拦下（消息={text[:60]!r}）")
        return "", ("你填的城市并不在主人本条消息里，属于你自己猜的地点。"
                    "请直接用角色身份问一句主人现在在哪个城市，不要调用任何工具。")
    return "", ""


def test_sample_for(tool: dict) -> dict:
    return dict(TOOL_TEST_SAMPLES.get(str((tool or {}).get("builtin", "")).lower(), {}) or {})


# 最终回答里表示"正在问用户地点"的收尾句（用于校验模型是否照做了）
_ASK_LOCATION_RE = re.compile(r"哪个城市|哪座城市|在哪儿|在哪里|什么城市|哪里的|所在城市|"
                              r"どこの|どの都市|どこに住|where do you live|which city", re.I)


def last_user_message(messages) -> str:
    """从消息列表里取出【最后一条真实用户消息】（跳过系统注入的【…】提示）。"""
    for message in reversed(list(messages or [])):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = str(message.get("content", ""))
        if content.startswith("【"):
            continue
        return content
    return ""


def reply_asks_for_location(text) -> bool:
    """回复是否只是在问主人所在的城市（用于天气"不许猜地点"的收尾校验）。"""
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
    """web_fetch 收到的是搜索词而不是网址时，自动改走搜索引擎。

    事故背景：用户说"帮我搜索 xx"，模型有时会把 web_search 和 web_fetch 记混，
    用 web_fetch 传 {"query": "xx"}，于是回一句"缺少 url 参数"、什么也没搜到。
    参数里没有任何网址、只有关键词时，用户要的就是搜索，直接搜比报错有用。
    """
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
    """同一个注册表里启用着的 web_search 工具（没有就返回 None）。"""
    for item in getattr(tool_registry, "tools", []) or []:
        if str(item.get("builtin", "")).lower() == "web_search" and item.get("enabled"):
            return item
    return None


def _pick_queries(args: dict, limit: int = 4) -> List[str]:
    """收集本次调用要搜的关键词：兼容 query 单值、queries 数组以及中英文参数名。

    事故背景：web_search 的 schema 原先只有 query 一个字段，而模型（照别的工具的
    习惯）经常一次传 queries 数组来表达"同时查几件事"，于是整条调用直接以
    "缺少 query 参数"失败——用户看到的就是"搜不全/只搜到第一项"。这里两种形态都接受。
    """
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
    """一次调用里搜索多个关键词，把各自的结果拼成一份完整结果返回。

    整体字符预算按子查询数量平分（下限 600 字符），避免"查 4 件事、每件都返回一屏"
    把上下文塞爆；每个子查询仍保留完整多条结果。
    """
    per_query = 0
    if total_chars and len(queries) > 1:
        # 每个子查询给"略高于均分"的额度（下限 500、上限 1100 字符）：
        # 额度太小会把结果砍到只剩一两条（"搜不全"换个形式回来），太大则几个子查询
        # 加起来把上下文塞爆，所以上限取均分的 1.1 倍左右。
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
    """解码子进程输出。

    中文 Windows 的控制台工具默认按本机代码页（cp936）输出，而多数跨平台
    命令按 UTF-8 输出；固定按 UTF-8 解码会把前者整段变成替换字符且不报错。
    依次尝试候选编码，取第一个能完整解码的结果。
    """
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
        self.load()

    # ---------------- 持久化 ----------------
    def load(self):
        try:
            if self.file.exists():
                self.tools = json.loads(self.file.read_text(encoding="utf-8"))
                if not isinstance(self.tools, list):
                    self.tools = []
            known = {str(t.get("name", "")) for t in self.tools if isinstance(t, dict)}
            missing = [dict(t) for t in DEFAULT_TOOLS if t.get("name") not in known]
            refreshed = 0
            # 内置工具的说明/参数属于随版本更新的代码资产（写死在 tools.json 里会让
            # 升级后的新说明永远不生效，导致模型分不清 web_search 与 web_fetch）；
            # 这里以随程序发布的定义为准刷新，用户自己的 enabled/授权/次数限制保留。
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
                tool["parameters"] = shipped.get("parameters")
                if "engine" not in tool and "engine" in shipped:
                    tool["engine"] = shipped["engine"]
            if missing:
                self.tools.extend(missing)
                self.save()
            elif not self.tools or refreshed:
                self.save()
        except Exception as e:
            print(f"加载工具配置失败: {e}")
            self.tools = [dict(t) for t in DEFAULT_TOOLS]

    def save(self):
        try:
            self.file.write_text(json.dumps(self.tools, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
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
        """每次回复开始时重置调用计数。"""
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
                # 消息里没有数字与运算式时模型的"我算了一下"是幻觉：拒绝执行并
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
            # 地点校验：用户没说城市时绝不替主人猜一个（历史 bug：凭空报"上海的天气"）。
            # 只有用户本条消息里真的说出了地点，或配置里明确写了默认城市，才允许查。
            resolved, hint = _resolve_weather_city(args, tool, user_text)
            city = resolved
            if not city and (lat is None or lon is None):
                return True, (hint or
                              "主人本条消息里没有说明地点，系统也不知道主人在哪个城市。"
                              "绝不允许自行假设（例如上海/北京）：请直接用角色身份问一句"
                              "「主人现在在哪个城市呀？」，不要调用任何工具、"
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
            # 模型把搜索词塞给了 web_fetch：用户要的是搜索，直接搜
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

    async def _web_search(self, query: str, tool: dict, engine: str = "",
                          max_chars: Optional[int] = None) -> Tuple[bool, str]:
        """搜索入口：按需在多个引擎间兜底，挑出"真的对得上搜索词"的那一份结果。

        事故背景：引擎返回了结果 ≠ 结果回答了问题。中文姓名常被引擎放宽成单字命中
        （搜"李祖祥"全是"李（汉语汉字）""李姓"），模型只好回答"没查到这个人"。
        所以：默认引擎的结果若一条都不含搜索词，就自动换下一个已启用引擎重试，
        并把"哪些引擎搜过、哪个命中了"写进结果，模型才能据此如实作答。
        """
        engines = available_engines(self.config)
        if not engines:
            return False, "没有可用的搜索引擎"
        requested = str(engine or tool.get("engine", "") or "").strip()
        explicit = requested in engines
        first = requested if explicit else default_engine_key(self.config)
        # 兜底顺序：选中的引擎 → 用户自定义引擎 → 其余内置引擎。
        # 自定义引擎放前面，是因为那通常是用户特意配的可用渠道。
        custom = [k for k in engines if engines[k].get("custom")]
        rest = [k for k in engines if k != first and k not in custom]
        order = [first] + [k for k in custom if k != first] + rest
        # 模型/工具配置显式点名了引擎就只用它（尊重明确选择，也避免一次调用打爆多个引擎）；
        # 用默认引擎时允许在引擎之间兜底——中文姓名常被某个引擎放宽成单字命中。
        if explicit or not self.config.get("web_search_auto_fallback", True):
            order = [first]
        best: Optional[Tuple[bool, str]] = None
        attempts: List[str] = []
        # 第一轮：按当前引擎顺序，先用原始搜索词
        for key in order:
            ok, text = await self._search_one_engine(query, engines[key], key, tool, max_chars)
            # 失败时区分"被验证页拦住"和"结果不相关"：前者值得换引擎，后者值得换搜索词
            tag = "[验证]" if _looks_like_verify_page(text) else "" if ok else "[失败]"
            attempts.append(f"{engines[key].get('label', key)}：{'命中' if ok else '未命中'}{tag}")
            if best is None or (ok and not best[0]):
                best = (ok, text)
            if ok:
                break
        # 第二轮：一个引擎都没给出相关结果时，用"更像在找这个实体"的问法重试
        # （中文人名/专有名词被引擎拆字退化是常见现象，换个问法比换引擎更有效）
        if best is not None and not best[0] and self.config.get("web_search_query_rewrite", True):
            for alternative in _query_variants(query):
                ok, text = await self._search_one_engine(alternative, engines[order[0]],
                                                         order[0], tool, max_chars)
                attempts.append(f"改写「{alternative}」：{'命中' if ok else '未命中'}")
                if ok:
                    best = (True, text)
                    break
        assert best is not None
        ok, text = best
        print(f"[搜索] query={query!r} 命中={ok} 尝试过程：{'；'.join(attempts)}")
        if not ok and order:  # 一份结果都没取到时，把失败原因放在最前面（模型要看到真实原因）
            text = f"本次搜索未取到可用结果。最后一个引擎的返回：{text}"
        if len(order) > 1:
            text += "\n\n（引擎尝试情况：" + "；".join(attempts) + "）"
        elif len(attempts) > 1:
            # 只指定了一个引擎、又做过改写重试：说明改写过，但不冒充"换过引擎"
            text += "\n\n（已尝试换问法重搜：" + "；".join(attempts[1:]) + "）"
        elif not ok:
            text += "\n\n（该引擎没有给出与搜索词真正相关的结果，可换个引擎再试）"
        return ok, text

    async def _search_one_engine(self, query: str, spec: dict, key: str, tool: dict,
                                 max_chars: Optional[int] = None) -> Tuple[bool, str]:
        """用指定引擎搜一次；返回 (结果是否真的对得上搜索词, 结果文本或失败原因)。"""
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
        # 多取一些候选再截断：引擎返回的结果里有导航页/聚合页，去重后常常不够 max_results 条
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
                        payload = body_builder(query, 1, language)
                    else:
                        payload = {"query": query}
                    payload = _with_api_safe_search(payload, spec, self.config)
                    api_headers["Content-Type"] = "application/json"
                    raw = await self._fetch_text(api_url, api_headers, timeout, params=None,
                                                 method="POST", json_body=payload)
                else:
                    param_builder = spec.get("api_params")
                    params = param_builder(query, 1, language) if callable(param_builder) else {"q": query}
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
            request_url = _engine_request_url(spec, query, 1, language, self.config)
            if not request_url:
                return False, f"搜索引擎「{spec.get('label', key)}」未配置检索地址"
            # 自定义引擎和 SearXNG 都是"用户自己指定地址"的检索服务，允许指向本机/内网；
            # 其余内置引擎仍是公网站点，保持 SSRF 防护（不许被改成本机地址）。
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

        # 安全搜索：按档位过滤成人向（及更严格档下的低俗/性暗示）结果
        before_safe = len(results)
        results = filter_unsafe_results(results, self.config, query)
        safe_dropped = before_safe - len(results)

        # 引擎拆字退化的单字词条（"以_百度百科"）沉底，别让垃圾占据第 1 条
        results = _demote_degenerate_results(results, query)

        lines = []
        redirect_hosts = tuple(spec.get("redirect_hosts") or ())
        if redirect_hosts:
            await self._resolve_result_urls(results, redirect_hosts, headers, timeout)
        head = f"搜索关键词：{query}\n搜索引擎：{spec.get('label', key)}"
        if channel_hint:
            head += f"（{channel_hint}）"
        # 按整条结果累加预算：宁可少给几条完整结果，也不要把某条摘要截成半句——
        # 半截摘要会让模型以为"资料就这么多"，答出来的东西自然不全面。
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
            text += (f"\n\n（引擎共返回 {len(results) + safe_dropped} 条"
                     f"（其中 {safe_dropped} 条被安全搜索过滤），"
                     f"本次给出 {kept} 条完整结果；"
                     f"其余 {omitted} 条因长度限制省略，摘要未被截断。"
                     f"把搜索词写得更具体，或多传几个 queries 分开搜，可以拿到更多结果。）")
        if safe_dropped:
            text += (f"\n\n（安全搜索：{safe_search_label(self.config)} —— "
                     f"已按安全搜索设置过滤 {safe_dropped} 条成人向/低俗结果。）")
        text += safe_search_hint(self.config)
        relevant = _looks_relevant(query, results)
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
        return relevant, text

    async def _resolve_result_urls(self, results: list, redirect_hosts: tuple,
                                   headers: dict, timeout: float) -> None:
        """把引擎的跳转链接（如搜狗 /link?url=…）解析成真实地址，失败就保留原链接。

        只对 spec 里声明 `redirect_hosts` 的引擎做这一步：每个结果一次 HEAD/GET，
        并发执行但整体限时，避免拖慢搜索。
        """

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
        """带主机校验的一次性 HTTP 取文：默认只跟随同主机跳转，跨主机拒绝。

        `follow_hosts` 用于已知的地区跳转（如 www.bing.com → cn.bing.com），
        只有显式列出的主机才允许跟随，避免把 SSRF 边界放宽成任意跳转。
        """
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
        template_url = _validate_url(url_tpl, allow_private=True)
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
        url = _validate_url(url, allow_private=True)
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
