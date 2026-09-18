"""零依赖的 YAML 子集读写，专门服务插件清单（plugin.yaml）。

为什么不直接上 PyYAML
--------------------
主程序是 PyInstaller 单文件打包发布，多一个第三方依赖就多一份"打包环境里
忘了装"的风险。插件清单只用得到 YAML 里很小的一块语法，自己实现反而更可控。

支持的语法（覆盖清单需要的全部形态）
------------------------------------
- `key: value` 映射，靠缩进嵌套；缩进用空格，同一层必须对齐
- `- item` 序列；`- key: value` 的映射序列（后续行缩进比 `-` 深即属同一条）
- 序列可以与其父键同缩进（`features:` 换行后直接 `- key: ...`）
- 行内集合 `[a, b]` / `{a: b}`
- 单引号 / 双引号字符串（含 `\\n` `\\t` `\\uXXXX` 转义、`''` 表示单引号）
- 块标量 `|` / `>`（可带 `-` 去尾），常用于长描述
- `true` / `false`（大小写不敏感）、`null` / `~`、整数、小数
- 行尾 `#` 注释（引号内与块标量内不算注释）

明确**不**支持的（清单里也用不到）：锚点/别名、多文档、复杂键、标签、
时间戳类型、`yes/no/on/off` 这类 YAML 1.1 布尔别名 —— 后者一律当字符串，
免得把 `author: on` 这种正常文本吃掉。

`dumps()` 只负责输出程序自己生成的那一份清单（缺清单的 zip 兜底），
形态固定、可被 `loads()` 原样读回（有测试钉住往返一致）。
"""
import re

_PLAIN_SAFE = re.compile(r"^[A-Za-z0-9\u4e00-\u9fff]"
                         r"[A-Za-z0-9\u4e00-\u9fff _.\-+/()（）·:：]*$")
_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")
_RESERVED_WORDS = {"true", "false", "null", "~", "yes", "no", "on", "off"}

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", '"': '"',
            "\\": "\\", "/": "/", "b": "\b", "f": "\f"}


class YamlError(ValueError):
    """清单不是合法 YAML 时抛出，消息里带行号，便于定位。"""


# ------------------------------------------------------------------ 读取


def _strip_comment(text: str) -> str:
    """去掉行尾注释（引号内的 # 不算）。"""
    in_s = in_d = False
    for i, ch in enumerate(text):
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif ch == "#" and not in_s and not in_d:
            if i == 0 or text[i - 1] in " \t":
                return text[:i].rstrip()
    return text.rstrip()


def _is_seq(text: str) -> bool:
    return text == "-" or text.startswith("- ")


def _split_key(text: str):
    """在第一个"顶层"的 `:` 处切开键与值，切不开返回 (None, "")。"""
    in_s = in_d = False
    depth = 0
    for i, ch in enumerate(text):
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif not in_s and not in_d:
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
            elif ch == ":" and depth == 0:
                if i + 1 >= len(text) or text[i + 1] in " \t":
                    return _unquote(text[:i].strip()), text[i + 1:]
    return None, ""


def _unquote(text: str) -> str:
    t = text.strip()
    if len(t) >= 2 and t[0] == t[-1] == "'":
        return t[1:-1].replace("''", "'")
    if len(t) >= 2 and t[0] == t[-1] == '"':
        return _unescape_double(t[1:-1])
    return t


def _unescape_double(body: str) -> str:
    out = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\" or i + 1 >= len(body):
            out.append(ch)
            i += 1
            continue
        nxt = body[i + 1]
        if nxt == "u" and i + 6 <= len(body):
            try:
                out.append(chr(int(body[i + 2:i + 6], 16)))
                i += 6
                continue
            except ValueError:
                pass
        out.append(_ESCAPES.get(nxt, nxt))
        i += 2
    return "".join(out)


def _split_flow(body: str) -> list:
    """把 `a, b, c` 按顶层逗号切开（引号与嵌套括号内的逗号不切）。"""
    parts, buf = [], []
    in_s = in_d = False
    depth = 0
    for ch in body:
        if ch == "'" and not in_d:
            in_s = not in_s
        elif ch == '"' and not in_s:
            in_d = not in_d
        elif not in_s and not in_d:
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
            elif ch == "," and depth == 0:
                parts.append("".join(buf))
                buf = []
                continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [p for p in (x.strip() for x in parts) if p != ""]


def _parse_flow(text: str):
    body = text.strip()
    if body.startswith("[") and body.endswith("]"):
        return [_scalar(p) for p in _split_flow(body[1:-1])]
    if body.startswith("{") and body.endswith("}"):
        out = {}
        for item in _split_flow(body[1:-1]):
            key, rest = _split_key(item)
            if key is not None:
                out[key] = _scalar(rest)
        return out
    return _scalar(body)


def _scalar(text: str):
    """把一个标量字面量转成 Python 值。"""
    t = text.strip()
    if t == "":
        return None
    if t[0] in "[{":
        return _parse_flow(t)
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"":
        return _unquote(t)
    low = t.lower()
    if low in ("null", "~"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if _INT_RE.match(t):
        return int(t)
    if _FLOAT_RE.match(t) and "." in t:
        return float(t)
    return t


def _collect_block(raw_lines: list, start: int, parent_indent: int):
    """收集块标量的正文行（缩进比父键深，空行保留）。"""
    body, i = [], start
    while i < len(raw_lines):
        line = raw_lines[i]
        if line.strip() == "":
            body.append("")
            i += 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= parent_indent:
            break
        body.append(line)
        i += 1
    while body and body[-1].strip() == "":
        body.pop()
    if not body:
        return [], i
    pad = min(len(ln) - len(ln.lstrip(" ")) for ln in body if ln.strip())
    return [ln[pad:] if len(ln) >= pad else "" for ln in body], i


def _tokenize(text: str) -> list:
    """把文本切成 [indent, content, block_payload] 三元组列表。"""
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    tokens = []
    i = 0
    while i < len(raw):
        line = raw[i]
        if line.strip() == "" or line.strip().startswith("#"):
            i += 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        content = _strip_comment(line.strip())
        if not content:
            i += 1
            continue
        m = re.search(r":\s*([|>])([+-]?)\s*$", content) or \
            re.match(r"^-\s*([|>])([+-]?)\s*$", content)
        if m:
            body, nxt = _collect_block(raw, i + 1, indent)
            tokens.append([indent, content, (m.group(1), m.group(2), body)])
            i = nxt
            continue
        tokens.append([indent, content, None])
        i += 1
    return tokens


def _render_block(payload) -> str:
    style, _chomp, body = payload
    if style == "|":
        text = "\n".join(body)
    else:
        parts = []
        for ln in body:
            parts.append("\n" if ln.strip() == "" else ln.strip())
        text = " ".join(parts)
        text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    return text.strip("\n")


def _parse_block(tokens: list, pos: int, min_indent: int):
    """从 pos 开始解析一层块，返回 (值, 下一个位置)。"""
    if pos >= len(tokens):
        return None, pos
    indent = tokens[pos][0]
    if indent < min_indent:
        return None, pos
    if _is_seq(tokens[pos][1]):
        return _parse_seq(tokens, pos, indent)
    return _parse_map(tokens, pos, indent)


def _parse_seq(tokens: list, pos: int, indent: int):
    items = []
    while pos < len(tokens) and tokens[pos][0] == indent and _is_seq(tokens[pos][1]):
        text, payload = tokens[pos][1], tokens[pos][2]
        rest = text[1:].strip()
        if payload is not None:
            items.append(_render_block(payload))
            pos += 1
        elif rest == "":
            val, pos = _parse_block(tokens, pos + 1, indent + 1)
            items.append(val)
        elif _split_key(rest)[0] is not None:
            # "- key: value"：把剩余部分当成缩进 +2 的一行，与后续行一起解析
            sub = [[indent + 2, rest, payload]]
            pos += 1
            while pos < len(tokens) and tokens[pos][0] > indent:
                sub.append(tokens[pos])
                pos += 1
            val, _ = _parse_map(sub, 0, indent + 2)
            items.append(val)
        else:
            items.append(_scalar(rest))
            pos += 1
    return items, pos


def _parse_map(tokens: list, pos: int, indent: int):
    out = {}
    while pos < len(tokens) and tokens[pos][0] == indent and not _is_seq(tokens[pos][1]):
        text, payload = tokens[pos][1], tokens[pos][2]
        key, rest = _split_key(text)
        if key is None:
            raise YamlError(f"清单里有无法解析的行（第 {pos + 1} 条语句）：{text}")
        if payload is not None:
            out[key] = _render_block(payload)
            pos += 1
            continue
        if rest.strip() == "":
            nxt = tokens[pos + 1] if pos + 1 < len(tokens) else None
            if nxt and (nxt[0] > indent or (nxt[0] == indent and _is_seq(nxt[1]))):
                child_min = indent if _is_seq(nxt[1]) else indent + 1
                val, pos = _parse_block(tokens, pos + 1, child_min)
                out[key] = val
            else:
                out[key] = None
                pos += 1
            continue
        out[key] = _scalar(rest)
        pos += 1
    return out, pos


def loads(text: str):
    """解析一段 YAML 文本，返回 dict / list / 标量。"""
    tokens = _tokenize(str(text or ""))
    if not tokens:
        return {}
    value, _ = _parse_block(tokens, 0, tokens[0][0])
    return value


def load_file(path):
    """读一个 YAML 文件，读不了或解析失败时抛 YamlError / OSError。"""
    with open(path, "r", encoding="utf-8") as f:
        return loads(f.read())


# ------------------------------------------------------------------ 写出


def _quote(text: str) -> str:
    """需要引号时用双引号并转义，否则给裸标量（可读性优先）。"""
    t = str(text)
    if not t:
        return '""'
    if (t.lower() in _RESERVED_WORDS or _INT_RE.match(t) or _FLOAT_RE.match(t)
            or t != t.strip() or t[-1] == ":" or t.startswith("- ")
            or not _PLAIN_SAFE.match(t)):
        return '"' + (t.replace("\\", "\\\\").replace('"', '\\"')
                      .replace("\n", "\\n").replace("\t", "\\t")) + '"'
    return t


def _scalar_out(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return _quote(value)


def _dump_lines(value, indent: int) -> list:
    pad = " " * indent
    if isinstance(value, dict):
        out = []
        for key, val in value.items():
            k = _quote(key) if not _PLAIN_SAFE.match(str(key)) else str(key)
            if isinstance(val, dict) and val:
                out.append(f"{pad}{k}:")
                out.extend(_dump_lines(val, indent + 2))
            elif isinstance(val, list) and val:
                out.append(f"{pad}{k}:")
                out.extend(_dump_lines(val, indent))
            else:
                out.append(f"{pad}{k}: {_scalar_out(val)}")
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, dict) and item:
                sub = _dump_lines(item, indent + 2)
                out.append(f"{pad}- {sub[0].strip()}")
                out.extend(sub[1:])
            elif isinstance(item, list) and item:
                out.append(f"{pad}-")
                out.extend(_dump_lines(item, indent + 2))
            else:
                out.append(f"{pad}- {_scalar_out(item)}")
        return out
    return [f"{pad}{_scalar_out(value)}"]


def dumps(value) -> str:
    """把 dict / list 序列化成 YAML 文本（末尾带一个换行）。"""
    return "\n".join(_dump_lines(value, 0)) + "\n"
