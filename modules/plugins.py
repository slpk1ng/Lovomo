import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

from modules import yaml_lite

PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
ALLOWED_EXTS = {
    # 代码 / 配置
    ".py", ".pyi", ".pyw", ".pyx", ".css", ".scss", ".less", ".js", ".mjs",
    ".cjs", ".jsx", ".ts", ".tsx", ".map", ".wasm", ".json", ".jsonc", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".conf", ".properties",
    # 文本 / 数据
    ".md", ".txt", ".csv", ".tsv", ".xml", ".sqlite", ".db",
    # 网页 / 图片
    ".html", ".htm", ".png", ".jpg", ".jpeg", ".jfif", ".gif", ".svg", ".webp",
    ".avif", ".apng", ".bmp", ".ico",
    # 字体
    ".woff", ".woff2", ".ttf", ".ttc", ".otf", ".eot",
    # 音视频
    ".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus", ".webm", ".mp4",
}
MANIFEST_NAMES = ("plugin.yaml", "plugin.yml", "plugin.json")
MANIFEST_NAME = MANIFEST_NAMES[0]
WEBUI_NAME = "webui.html"
SETTINGS_NAME = "settings.json"
UNINSTALLED_MARKER = ".uninstalled"
MAX_UNPACK_BYTES = 128 * 1024 * 1024
MAX_FILES = 2000

_GITHUB_URL_RE = re.compile(r"github\.com[/:]+([A-Za-z0-9_.\-]+)", re.I)
_REPO_PATH_RE = re.compile(
    r"github\.com[/:]+([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)", re.I)
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")


ASSET_EXTS = {
    ".png", ".jpg", ".jpeg", ".jfif", ".gif", ".svg", ".webp", ".avif",
    ".apng", ".bmp", ".ico",
    ".css", ".js", ".mjs", ".html", ".htm", ".json", ".txt", ".csv", ".xml",
    ".map", ".wasm",
    ".woff", ".woff2", ".ttf", ".ttc", ".otf", ".eot",
    ".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus", ".webm", ".mp4",
}
MAX_ASSET_BYTES = 32 * 1024 * 1024

ICON_STEMS = ("logo", "icon")
ICON_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico", ".bmp")

MAX_DOC_BYTES = 256 * 1024
_DOC_NAMES = {
    "readme": ("readme.md", "readme.txt", "readme.markdown"),
    "update": ("update.md", "update.txt", "updates.md", "updates.txt",
               "changelog.md", "changelog.txt", "history.md", "history.txt"),
}

_ILLEGAL_NAME_CHARS = '<>:"/\\|?*'
_RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                   *(f"lpt{i}" for i in range(1, 10))}

_MIME_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".jfif": "image/jpeg", ".gif": "image/gif", ".svg": "image/svg+xml",
    ".webp": "image/webp", ".avif": "image/avif", ".apng": "image/apng",
    ".ico": "image/x-icon", ".bmp": "image/bmp", ".css": "text/css",
    ".js": "application/javascript", ".mjs": "application/javascript",
    ".html": "text/html", ".htm": "text/html", ".json": "application/json",
    ".txt": "text/plain; charset=utf-8", ".csv": "text/csv; charset=utf-8",
    ".xml": "application/xml", ".map": "application/json",
    ".wasm": "application/wasm",
    ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
    ".ttc": "font/collection", ".otf": "font/otf", ".eot": "application/vnd.ms-fontobject",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".flac": "audio/flac", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".opus": "audio/ogg", ".webm": "video/webm", ".mp4": "video/mp4",
}

# ---------------------------------------------------------------- 安全扫描
_RISK_RULES = [
    ("high", "读取进程环境与凭据",
     re.compile(r"(?:config\.json|\.env\b|api_key|password|token)\s*[\"']?\s*\)?"
                r"|os\.environ\.get\s*\(\s*[\"'](?:LOVOMO|NAP CAT|OPENAI)", re.I)),
    ("high", "疑似反弹连接",
     re.compile(r"\bconnect\s*\(\s*\(\s*[\"'][\d.]+[\"']")),
    ("high", "访问注册表/系统 API",
     re.compile(r"\b(?:winreg|ctypes\.windll|ctypes\.WinDLL|_winapi)\b")),
    ("high", "删除整个目录树",
     re.compile(r"\bshutil\.rmtree\s*\(")),
    ("medium", "执行系统命令",
     re.compile(r"\b(?:os\.system|os\.popen|os\.exec[lv]\w*|commands\.getoutput)\s*\(")),
    ("medium", "创建子进程",
     re.compile(r"\b(?:subprocess|multiprocessing)\.\w+")),
    ("medium", "动态执行代码",
     re.compile(r"\b(?:eval|exec|compile)\s*\(|__import__\s*\(")),
    ("medium", "篡改解释器内建",
     re.compile(r"\bsetattr\s*\(\s*(?:__builtins__|builtins)")),
    ("medium", "删除文件",
     re.compile(r"\b(?:os\.remove|os\.removedirs|os\.unlink|os\.rmdir)\s*\(")),
    ("medium", "建立网络套接字",
     re.compile(r"\bsocket\.socket\s*\(")),
    ("medium", "网络请求",
     re.compile(r"\b(?:requests\.(?:get|post|put|delete)|httpx\.|urllib\.request"
                r"|urlopen|aiohttp)")),
    ("medium", "写文件",
     re.compile(r"\bopen\s*\([^)]*[\"'][wa]\+?[\"']|\bwrite_text\s*\(|\bwrite_bytes\s*\(")),
    ("medium", "改写导入路径",
     re.compile(r"\bsys\.path\.(?:insert|append)\s*\(")),
    ("medium", "访问剪贴板/键鼠钩子",
     re.compile(r"\b(?:pyperclip|keyboard|pynput|pyautogui)\b")),
    ("medium", "加载原生库",
     re.compile(r"\bctypes\.CDLL|\bcffi\.|LoadLibrary")),
    ("medium", "解压/写入任意路径",
     re.compile(r"\b(?:zipfile|tarfile)\.\w*(?:extract|open)")),
    ("low", "后台线程/定时器",
     re.compile(r"\bthreading\.(?:Thread|Timer)|\basyncio\.create_task")),
    ("low", "环境变量读取",
     re.compile(r"\bos\.environ\b")),
]

_OBFUSCATION_RULES = [
    ("high", "超长单行（疑似混淆）", lambda t: max(
        (len(ln) for ln in t.splitlines()), default=0) > 800),
    ("medium", "大量转义/十六进制串",
     lambda t: len(re.findall(r"(?:\\x[0-9a-fA-F]{2}){6,}", t)) > 0),
    ("medium", "Base64 解码后执行",
     lambda t: bool(re.search(r"b64decode|base64\.b64decode|fromhex", t))),
]


def scan_python_source(code: str) -> list:
    findings = []
    lines = code.splitlines()
    for level, title, pattern in _RISK_RULES:
        hit_lines = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if pattern.search(line):
                hit_lines.append(i)
        if hit_lines:
            findings.append({"level": level, "title": title, "hits": hit_lines[:20]})
    for level, title, detector in _OBFUSCATION_RULES:
        try:
            if detector(code):
                findings.append({"level": level, "title": title, "hits": []})
        except Exception:
            pass
    return findings


def summarize_risks(findings: list) -> dict:
    high = [f for f in findings if f.get("level") == "high"]
    medium = [f for f in findings if f.get("level") == "medium"]
    low = [f for f in findings if f.get("level") == "low"]
    return {
        "high": len(high), "medium": len(medium), "low": len(low),
        "safe": not high and not medium,
        "requires_confirm": bool(high),
        "items": findings,
    }


def _safe_extract_root(name: str):
    norm = name.replace("\\", "/").strip("/")
    if not norm:
        return None, None
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if not parts:
        return None, None
    if any(p == ".." for p in parts):
        return None, None
    if re.match(r"^[A-Za-z]:", parts[0]):
        return None, None
    return parts[0], parts


def find_manifest(root: Path):
    if root is None:
        return None
    for name in MANIFEST_NAMES:
        f = Path(root) / name
        if f.is_file():
            return f
    return None


def parse_manifest_text(text: str, filename: str = "") -> dict:
    raw = str(text or "").lstrip("\ufeff")
    if not raw.strip():
        return {}
    name = str(filename or "").lower()
    try:
        if name.endswith(".json"):
            data = json.loads(raw)
        else:
            data = yaml_lite.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _read_manifest_from_dir(root: Path) -> dict:
    mf = find_manifest(root)
    if mf is None:
        return {}
    try:
        return parse_manifest_text(mf.read_text(encoding="utf-8"), mf.name)
    except Exception:
        return {}


def repo_owner(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    m = _GITHUB_URL_RE.search(raw)
    if m:
        return m.group(1)
    head = raw.split("?")[0].split("#")[0].strip().strip("/")
    if head.count("/") == 1 and " " not in head:
        return head.split("/")[0]
    return ""


def repo_slug(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    m = _REPO_PATH_RE.search(raw)
    if m:
        return m.group(1)[:-4] if m.group(1).lower().endswith(".git") else m.group(1)
    head = raw.split("?")[0].split("#")[0].strip().strip("/")
    if head.count("/") == 1 and " " not in head:
        return head[:-4] if head.lower().endswith(".git") else head
    return ""


def manifest_logins(manifest: dict) -> list:
    raw = manifest if isinstance(manifest, dict) else {}
    found = []
    for key in ("github", "owner"):
        found.append(raw.get(key))
    for key in ("repo", "source_repo", "homepage"):
        found.append(repo_owner(raw.get(key)))
    found.append(raw.get("author"))
    seen, out = set(), []
    for value in found:
        login = str(value or "").strip()
        if not _LOGIN_RE.match(login):
            continue
        low = login.lower()
        if low not in seen:
            seen.add(low)
            out.append(login)
    return out


def ownership_matches(manifest: dict, login: str) -> bool:
    who = str(login or "").strip().lower()
    if not who:
        return False
    return who in {v.lower() for v in manifest_logins(manifest)}


def _normalize_manifest(raw: dict, fallback_id: str = "") -> dict:
    pid = str(raw.get("id") or fallback_id or "").strip()
    if not PLUGIN_ID_RE.match(pid):
        pid = re.sub(r"[^A-Za-z0-9_\-]", "-", pid)[:64]
    ptype = str(raw.get("type") or "").strip().lower()
    if ptype not in ("theme", "python", "mixed"):
        ptype = "python"
    raw_tags = raw.get("tags")
    tags = [str(t).strip()[:24] for t in raw_tags if str(t).strip()][:8] \
        if isinstance(raw_tags, list) else []
    return {
        "id": pid,
        "name": str(raw.get("name") or pid or "未命名插件").strip()[:80],
        "version": str(raw.get("version") or "0.0.0").strip()[:32],
        "author": str(raw.get("author") or "未知作者").strip()[:80],
        "github": str(raw.get("github") or "").strip()[:80],
        "repo": str(raw.get("repo") or "").strip()[:200],
        "source_repo": str(raw.get("source_repo") or "").strip()[:200],
        "homepage": str(raw.get("homepage") or "").strip()[:400],
        "logo": str(raw.get("logo") or "").strip().replace("\\", "/")[:120],
        "description": str(raw.get("description") or "").strip()[:400],
        "tags": tags,
        "type": ptype,
        "category": str(raw.get("category") or "").strip()[:24],
        "entry": str(raw.get("entry") or "main.py").strip()[:120],
        "skin": _normalize_skin(raw.get("skin")),
        "features": _normalize_features(raw.get("features")),
        "panel": _normalize_panel(raw.get("panel")),
    }


FIELD_TYPES = ("bool", "range", "number", "text", "textarea", "select",
               "multiselect", "color", "password", "file")
MAX_FEATURES = 60
MAX_VARS = 80

CSS_LENGTH_UNITS = ("px", "rem", "em", "%", "vw", "vh", "vmin", "vmax",
                    "pt", "ch", "ex", "cm", "mm", "in")


def _normalize_panel(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    label = str(raw.get("label") or "").strip()[:40]
    if not label:
        return {}
    out = {"label": label, "title": str(raw.get("title") or label).strip()[:80]}
    html = str(raw.get("html") or raw.get("entry") or "").strip()[:200]
    if html:
        out["html"] = html
    return out


def _clean_var_name(raw) -> str:
    name = re.sub(r"[^a-z0-9\-]", "-", str(raw or "").strip().lower())
    name = re.sub(r"-+", "-", name).strip("-")
    if name.startswith("skin-"):
        name = name[5:]
    return name[:32]


def _normalize_field(item: dict, index: int) -> dict:
    key = re.sub(r"[^A-Za-z0-9_\-]", "", str(item.get("key") or ""))[:64]
    if not key:
        return {}
    out = {
        "key": key,
        "label": str(item.get("label") or key).strip()[:60],
        "description": str(item.get("description") or "").strip()[:500],
        "type": "bool",
    }
    raw_type = str(item.get("type") or "").strip().lower()
    default = item.get("default")
    if raw_type in FIELD_TYPES:
        out["type"] = raw_type
    elif raw_type:
        out["type"] = "bool"
    elif isinstance(default, bool):
        out["type"] = "bool"
    elif isinstance(default, (int, float)):
        out["type"] = "range" if ("min" in item or "max" in item) else "number"
    elif isinstance(default, str):
        out["type"] = "text"
    elif isinstance(default, list):
        out["type"] = "multiselect"

    t = out["type"]
    if t == "bool":
        out["default"] = _as_bool(default)
    elif t in ("range", "number"):
        lo = _as_float(item.get("min"), 0.0)
        hi = _as_float(item.get("max"), 1.0 if t == "range" else 100.0)
        if hi < lo:
            lo, hi = hi, lo
        out["min"] = lo
        out["max"] = hi
        step = _as_float(item.get("step"), 0.0)
        out["step"] = step if step > 0 else (0.01 if t == "range" and hi - lo <= 10 else 1)
        unit = str(item.get("unit") or "").strip()[:8]
        if unit:
            out["unit"] = unit
        out["default"] = _clamp(_as_float(default, lo if t == "range" else lo), lo, hi)
        if t == "number":
            out["default"] = int(out["default"]) if out["step"] >= 1 else out["default"]
    elif t in ("text", "textarea", "password"):
        out["default"] = str(default if default is not None else "")[:2000]
        ph = str(item.get("placeholder") or "").strip()[:120]
        if ph:
            out["placeholder"] = ph
        if t == "text":
            out["maxlength"] = int(_clamp(_as_float(item.get("maxlength"), 200.0), 1, 2000))
    elif t == "color":
        color = _sanitize_color(str(item.get("default") or "#888888"))
        out["default"] = color or "#888888"
    elif t in ("select", "multiselect"):
        out["options"] = _normalize_options(item.get("options"))
        options_file = _safe_rel_path(item.get("options_file"))
        if options_file:
            out["options_file"] = options_file
        if not out["options"] and not options_file:
            return {}
        if t == "select":
            vals = [o["value"] for o in out["options"]]
            if not vals and options_file:
                out["default"] = str(default if default is not None else "")
            else:
                fallback = vals[0] if vals else ""
                dv = str(default if default is not None else fallback)
                out["default"] = dv if dv in vals else fallback
        else:
            picked = default if isinstance(default, list) else ([default] if default else [])
            out["default"] = [str(v) for v in picked if str(v) in
                              [o["value"] for o in out["options"]]]
    elif t == "file":
        exts = item.get("exts") or []
        out["exts"] = [re.sub(r"[^A-Za-z0-9]", "", str(e))[:8]
                       for e in exts if str(e).strip()][:20]
        out["default"] = str(default or "")[:200]
    out.setdefault("order", index)
    return out


def _safe_rel_path(value) -> str:
    rel = str(value or "").replace("\\", "/").strip().lstrip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return ""
    return "/".join(parts)


def _normalize_options(raw) -> list:
    if not isinstance(raw, list):
        return []
    out = []
    for opt in raw[:50]:
        if isinstance(opt, dict):
            value = str(opt.get("value") if opt.get("value") is not None else "")[:200]
            if not value:
                continue
            label = str(opt.get("label") or value).strip()[:60]
        else:
            value = str(opt).strip()[:200]
            if not value:
                continue
            label = value
        out.append({"value": value, "label": label})
    return out


def _as_float(value, fallback: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return fallback
    if num != num or num in (float("inf"), float("-inf")):
        return fallback
    return num


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _as_bool(value, fallback: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return fallback
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() not in ("false", "0", "no", "off", "否", "关", "关闭")


def _normalize_features(raw) -> list:
    if not isinstance(raw, list):
        return []
    out = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        field = _normalize_field(item, i)
        if field:
            out.append(field)
        if len(out) >= MAX_FEATURES:
            break
    return out


def _normalize_skin(raw) -> dict:
    if not isinstance(raw, dict):
        return {}
    out = {}
    if raw.get("background"):
        out["background"] = True
    if raw.get("layout"):
        out["layout"] = True
    accent = raw.get("accent")
    if accent:
        out["accent"] = str(accent).strip()[:32]
    label = str(raw.get("label") or "").strip()[:60]
    if label:
        out["label"] = label
    vars_out = []
    for i, item in enumerate(raw.get("vars") or []):
        if not isinstance(item, dict):
            continue
        name = _clean_var_name(item.get("name") or item.get("key"))
        if not name:
            continue
        field = _normalize_field({**item, "key": name}, i)
        if not field:
            continue
        field["var"] = f"--skin-{name}"
        vars_out.append(field)
        if len(vars_out) >= MAX_VARS:
            break
    if vars_out:
        out["vars"] = vars_out
    return out


def safe_asset_name(raw_name: str, fallback_ext: str = "") -> str:
    raw = str(raw_name or "")
    stem = Path(raw.replace("\\", "/")).name.strip()
    ext = (Path(stem).suffix or "").lower() or fallback_ext.lower()
    if ext and not ext.startswith("."):
        ext = "." + ext
    base = stem[: len(stem) - len(Path(stem).suffix)] if Path(stem).suffix else stem
    base = "".join("_" if ch in _ILLEGAL_NAME_CHARS or ord(ch) < 32 else ch
                   for ch in base)
    base = re.sub(r"\s+", " ", base).strip().rstrip(".")
    base = base[:80].strip().rstrip(".")
    if not base or base in (".", ".."):
        base = "asset_" + hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:8]
    if base.lower() in _RESERVED_NAMES:
        base = "_" + base
    if ext not in ASSET_EXTS:
        ext = fallback_ext.lower() if fallback_ext.lower() in ASSET_EXTS else ""
    return base + ext


def unique_asset_name(directory, name: str) -> str:
    target = Path(directory) / name
    if not target.exists():
        return name
    stem, ext = Path(name).stem, Path(name).suffix
    for i in range(2, 1000):
        candidate = f"{stem}-{i}{ext}"
        if not (Path(directory) / candidate).exists():
            return candidate
    return f"{stem}-{int(time.time())}{ext}"


def _sanitize_background_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("/") or raw.startswith("api/"):
        cleaned = re.sub(r"[\s\"'()\\<>]", "", raw)
        if "//" in cleaned[1:] or ":" in cleaned.split("?")[0]:
            return ""
        return cleaned
    name = Path(raw.replace("\\", "/")).name
    if not name or name in (".", "..") or name.startswith("."):
        return ""
    # 带路径分隔符的参数直接拒绝
    if "/" in raw or "\\" in raw:
        return ""
    if any(ch in _ILLEGAL_NAME_CHARS or ord(ch) < 32 for ch in name):
        return ""
    if Path(name).suffix.lower() not in ASSET_EXTS:
        return ""
    return "/api/plugins/asset?name=" + quote(name, safe="")


def _sanitize_color(value: str) -> str:
    """只放行 #rgb / #rrggbb / #rrggbbaa 三种颜色写法。"""
    text = str(value or "").strip()
    if re.match(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$", text):
        return text.lower()
    return ""


def _css_string(text: str, limit: int = 200) -> str:
    clean = (str(text or "")
             .replace("\\", "").replace("\n", " ").replace("\r", " ")
             .replace('"', "").replace("'", ""))
    clean = re.sub(r"[<>{};]", "", clean)[:limit]
    return '"' + clean + '"'


def _css_unit_of(decl: dict) -> str:
    unit = str(decl.get("unit") or "").strip()
    return unit if unit in CSS_LENGTH_UNITS else ""


def _css_value_for(decl: dict, raw) -> str:
    t = decl.get("type") or "bool"
    if raw is None:
        raw = decl.get("default")

    if t == "bool":
        return "1" if raw else "0"

    if t in ("range", "number"):
        lo = _as_float(decl.get("min"), 0.0)
        hi = _as_float(decl.get("max"), 1.0)
        if hi < lo:
            lo, hi = hi, lo
        num = _clamp(_as_float(raw, lo), lo, hi)
        return _fmt_num(num) + _css_unit_of(decl)

    if t == "color":
        return _sanitize_color(str(raw)) or None

    if t in ("text", "textarea", "password"):
        return _css_string(raw, 500)

    if t == "select":
        opts = decl.get("options") or []
        text = str(raw or "")
        if text not in [str(o.get("value")) for o in opts if isinstance(o, dict)]:
            text = str(decl.get("default") or "")
        if not text:
            return None
        return _css_string(text, 60)

    if t == "multiselect":
        opts = decl.get("options") or []
        valid = {str(o.get("value")) for o in opts if isinstance(o, dict)}
        picked = raw if isinstance(raw, list) else []
        joined = ", ".join(str(v) for v in picked if str(v) in valid)
        return _css_string(joined, 200) if joined else None

    if t == "file":
        url = _sanitize_background_url(str(raw))
        return "url(\"" + url + "\")" if url else None

    return None


def _fmt_num(num: float) -> str:
    if abs(num - round(num)) < 1e-9:
        return str(int(round(num)))
    return f"{num:.4f}".rstrip("0").rstrip(".")


def _validate_setting(field: dict, value):
    t = field.get("type") or "bool"
    if t == "bool":
        return True, _as_bool(value)
    if t in ("range", "number"):
        lo = _as_float(field.get("min"), 0.0)
        hi = _as_float(field.get("max"), 1.0)
        if hi < lo:
            lo, hi = hi, lo
        return True, _clamp(_as_float(value, lo), lo, hi)
    if t == "color":
        safe = _sanitize_color(str(value))
        return (True, safe) if safe else (False, None)
    if t in ("text", "textarea", "password"):
        return True, str(value)[:2000]
    if t == "select":
        valid = [str(o.get("value")) for o in field.get("options") or []
                 if isinstance(o, dict)]
        text = str(value)
        return (True, text) if text in valid else (False, None)
    if t == "multiselect":
        valid = {str(o.get("value")) for o in field.get("options") or []
                 if isinstance(o, dict)}
        if not isinstance(value, list):
            return False, None
        return True, [str(v) for v in value if str(v) in valid][:50]
    if t == "file":
        safe = _sanitize_background_url(str(value)) if value else ""
        return True, safe
    return False, None


def _rmtree_retry(path, attempts: int = 3, delay: float = 0.25) -> bool:
    for i in range(max(1, attempts)):
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except Exception:
            if i == attempts - 1:
                return False
            time.sleep(delay * (i + 1))
    return False


class PluginManager:

    def __init__(self, plugins_root: Path, state_file: Path = None,
                 loader=None, on_change=None):
        self.root = Path(plugins_root)
        self.state_file = Path(state_file) if state_file else self.root / "state.json"
        self._loader = loader
        self._on_change = on_change      # 启用状态变化后的回调（热重载）
        self._state_cache = None
        self._applied: dict = {}
        self._settings_seeded: set = set()
        self.purge_temp_dirs()

    # ------------------------------------------------------------ 基础
    def ensure_root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    _TEMP_PREFIXES = (".staging-", ".backup-", ".keep-")

    def purge_temp_dirs(self) -> int:
        self.ensure_root()
        removed = 0
        try:
            entries = list(self.root.iterdir())
        except Exception:
            return 0
        for d in entries:
            if not d.is_dir():
                continue
            if not any(d.name.startswith(p) for p in self._TEMP_PREFIXES):
                continue
            if _rmtree_retry(d):
                removed += 1
        if removed:
            print(f"[插件] 已清理 {removed} 个残留的安装临时目录。")
        return removed

    def _state(self) -> dict:
        if self._state_cache is not None:
            return self._state_cache
        data = {}
        try:
            if self.state_file.is_file():
                raw = json.loads(self.state_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data = raw
        except Exception:
            data = {}
        data.setdefault("enabled", {})
        if not isinstance(data.get("enabled"), dict):
            data["enabled"] = {}
        data.setdefault("source", {})
        if not isinstance(data.get("source"), dict):
            data["source"] = {}
        data.setdefault("pinned", [])
        if not isinstance(data.get("pinned"), list):
            data["pinned"] = []
        data["pinned"] = [str(p) for p in data["pinned"] if str(p)]
        self._state_cache = data
        return data

    def _save_state(self) -> None:
        try:
            self.ensure_root()
            tmp = Path(f"{self.state_file}.{os.getpid()}.{time.time_ns()}.tmp")
            tmp.write_text(json.dumps(self._state(), ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(str(tmp), str(self.state_file))
        except Exception as e:
            print(f"[插件] 保存启用状态失败: {e}")

    def is_enabled(self, pid: str) -> bool:
        return bool(self._state()["enabled"].get(pid))

    def set_enabled(self, pid: str, enabled: bool) -> bool:
        if not self.get(pid):
            return False
        self._state()["enabled"][pid] = bool(enabled)
        self._save_state()
        if callable(self._on_change):
            try:
                self._on_change()
            except Exception as e:
                print(f"[插件] 启用状态回调失败: {e}")
        return True

    def pinned_ids(self) -> list:
        return list(self._state()["pinned"])

    def set_pinned(self, pid: str, pinned: bool) -> bool:
        if not self.get(pid):
            return False
        ids = [p for p in self._state()["pinned"] if p != pid]
        if pinned:
            ids.insert(0, pid)
        self._state()["pinned"] = ids
        self._save_state()
        return True

    def plugin_dir(self, pid: str):
        if not PLUGIN_ID_RE.match(str(pid or "")):
            return None
        d = self.root / pid
        try:
            d.resolve().relative_to(self.root.resolve())
        except Exception:
            return None
        return d

    def set_source(self, pid: str, repo: str, branch: str = "", kind: str = "") -> None:
        repo = str(repo or "").strip()
        if not repo or self.get(pid) is None:
            return
        self._state()["source"][pid] = {
            "repo": repo[:200],
            "branch": str(branch or "")[:120],
            "kind": str(kind or "")[:16],
            "at": int(time.time()),
        }
        self._save_state()

    def source_of(self, pid: str) -> dict:
        src = self._state()["source"].get(pid)
        return dict(src) if isinstance(src, dict) else {}

    # ------------------------------------------------------------ 枚举
    def list_plugins(self) -> list:
        self.ensure_root()
        out = []
        try:
            entries = sorted(self.root.iterdir(), key=lambda p: p.name.lower())
        except Exception:
            return out
        for d in entries:
            if not d.is_dir() or d.name.startswith(".") or d.name == "__pycache__":
                continue
            info = self._describe(d)
            if info:
                out.append(info)
        return out

    def get(self, pid: str):
        d = self.plugin_dir(pid)
        if d is None or not d.is_dir():
            return None
        return self._describe(d)

    def _describe(self, d: Path):
        if (d / UNINSTALLED_MARKER).exists():
            return None
        raw = _read_manifest_from_dir(d)
        manifest = _normalize_manifest(raw, fallback_id=d.name)
        if not manifest.get("id"):
            return None
        entry = d / manifest["entry"]
        info = dict(manifest)
        info["dir"] = str(d)
        info["enabled"] = self.is_enabled(manifest["id"])
        info["has_python"] = entry.is_file() and entry.suffix.lower() == ".py"
        info["has_theme"] = (d / "theme.css").is_file()
        icon = self._declared_icon(d, manifest.get("logo")) or self._find_icon(d)
        info["has_icon"] = icon is not None
        info["icon"] = icon.name if icon else ""
        info["readme"] = (self._find_doc(d, "readme") or Path("")).name
        info["update_note"] = (self._find_doc(d, "update") or Path("")).name
        info["webui_ok"] = (d / WEBUI_NAME).is_file()
        pinned = self._state()["pinned"]
        info["pinned"] = manifest["id"] in pinned
        info["pin_order"] = pinned.index(manifest["id"]) if info["pinned"] else -1
        info["features"] = self._features_with_options(d, info.get("features"))
        info["size"] = _dir_size(d)
        try:
            info["installed_at"] = int(d.stat().st_mtime)
        except Exception:
            info["installed_at"] = 0
        info["manifest_ok"] = bool(raw)
        info["owners"] = manifest_logins(manifest)
        src = self.source_of(manifest["id"])
        if src.get("repo"):
            info["source_repo"] = src["repo"]
            info["source_branch"] = str(src.get("branch") or "")
        panel = info.get("panel") or {}
        if panel.get("html"):
            info["panel"] = {**panel, "html_ok": self.panel_html_path(d, panel) is not None}
        return info

    def panel_html_path(self, d: Path, panel: dict):
        rel = str((panel or {}).get("html") or "").replace("\\", "/").strip()
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            return None
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if not parts or not parts[-1].lower().endswith((".html", ".htm")):
            return None
        target = d.joinpath(*parts)
        try:
            target = target.resolve()
            target.relative_to(d.resolve())
        except Exception:
            return None
        return target if target.is_file() else None

    def _features_with_options(self, d: Path, features):
        out = []
        for field in features or []:
            field = dict(field)
            rel = str(field.pop("options_file", "") or "").strip()
            if rel:
                field["options"] = (self._read_options_file(d, rel)
                                    or field.get("options") or [])
            out.append(field)
        return out

    def _read_options_file(self, d: Path, rel: str):
        safe = _safe_rel_path(rel)
        if not safe:
            return []
        try:
            target = d.joinpath(*safe.split("/")).resolve()
            target.relative_to(d.resolve())
            raw = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            return []
        if isinstance(raw, dict):
            raw = raw.get("options")
        if not isinstance(raw, list):
            return []
        options = []
        for item in raw:
            if isinstance(item, dict):
                value = str(item.get("value", ""))
                options.append({"value": value,
                                "label": str(item.get("label") or value)})
            elif isinstance(item, (str, int, float)):
                options.append({"value": str(item), "label": str(item)})
        return options

    # ------------------------------------------------------------ 扫描（安装前预览）
    def inspect_zip(self, data: bytes) -> dict:
        result = {"ok": False, "error": "", "manifest": {}, "manifest_name": "",
                  "risks": {}, "files": [], "warnings": []}
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except Exception as e:
            result["error"] = f"不是有效的 zip 包：{e}"
            return result
        with zf:
            names = zf.namelist()
            if len(names) > MAX_FILES:
                result["error"] = f"包内文件过多（{len(names)} > {MAX_FILES}）"
                return result
            total = 0
            py_sources = {}
            manifest_raw = None
            manifest_files = {}
            theme_css = ""
            keep = []
            for n in names:
                top, parts = _safe_extract_root(n)
                if parts is None:
                    result["warnings"].append(f"跳过非法路径条目：{n}")
                    continue
                if n.endswith("/"):
                    continue
                ext = Path(parts[-1]).suffix.lower()
                if ext not in ALLOWED_EXTS:
                    result["warnings"].append(f"跳过不支持的文件类型：{n}")
                    continue
                try:
                    info = zf.getinfo(n)
                except KeyError:
                    continue
                total += info.file_size
                if total > MAX_UNPACK_BYTES:
                    result["error"] = (f"解压后体积超过 "
                                       f"{MAX_UNPACK_BYTES // (1024 * 1024)}MB 上限")
                    return result
                keep.append(n)
                low = parts[-1].lower()
                if low in MANIFEST_NAMES:
                    manifest_files.setdefault(low, zf.read(n).decode("utf-8", "replace"))
                elif low == "theme.css":
                    try:
                        theme_css = zf.read(n).decode("utf-8", "replace")
                    except Exception:
                        pass
                elif ext == ".py":
                    try:
                        py_sources[n] = zf.read(n).decode("utf-8", "replace")
                    except Exception:
                        pass
            if not keep:
                result["error"] = "包里没有任何可识别的插件文件"
                return result
            for name in MANIFEST_NAMES:
                if name in manifest_files:
                    manifest_raw = parse_manifest_text(manifest_files[name], name)
                    result["manifest_name"] = name
                    if not manifest_raw:
                        result["warnings"].append(f"{name} 解析失败，已忽略")
                    break
            tops = {_safe_extract_root(n)[0] for n in keep}
            result["wrapped"] = len(tops) == 1 and not any(
                Path(n).name.lower() in MANIFEST_NAMES
                for n in keep if len(n.split("/")) == 1)
            fallback = next(iter(tops)) if len(tops) == 1 else ""
            if not isinstance(manifest_raw, dict):
                manifest_raw = {}
                result["warnings"].append(
                    "缺少 " + " / ".join(MANIFEST_NAMES)
                    + "，将按文件名推断插件 id（建议补上清单）")
            result["manifest"] = _normalize_manifest(manifest_raw, fallback)
            result["files"] = sorted(keep)[:200]
            result["file_count"] = len(keep)
            result["total_bytes"] = total

            findings = []
            for fname, src in py_sources.items():
                for item in scan_python_source(src):
                    item = dict(item)
                    item["file"] = fname
                    findings.append(item)
            if theme_css and re.search(r"@import\s+url\s*\(\s*[\"']?https?://",
                                       theme_css, re.I):
                findings.append({"level": "medium", "title": "皮肤引用外部样式表",
                                 "hits": [], "file": "theme.css"})
            result["risks"] = summarize_risks(findings)
            result["has_python"] = bool(py_sources)
            result["has_theme"] = bool(theme_css)
            result["ok"] = True
        return result

    # ------------------------------------------------------------ 安装
    def install_zip(self, data: bytes, *, force: bool = False,
                    enable: bool = False) -> dict:
        report = self.inspect_zip(data)
        if not report.get("ok"):
            return {"success": False, "error": report.get("error") or "包不可用",
                    "report": report}
        pid = report["manifest"]["id"]
        if not PLUGIN_ID_RE.match(pid or ""):
            return {"success": False, "error": "插件 id 非法（仅允许字母数字下划线连字符）",
                    "report": report}
        risks = report.get("risks") or {}
        if risks.get("requires_confirm") and not force:
            return {"success": False, "needs_confirm": True,
                    "error": "该插件包含高危操作，需要确认后才能安装",
                    "report": report}

        self.ensure_root()
        target = self.plugin_dir(pid)
        if target is None:
            return {"success": False, "error": "插件 id 非法", "report": report}
        staging = self.root / f".staging-{pid}-{int(time.time() * 1000)}"
        backup = self.root / f".backup-{pid}-{int(time.time() * 1000)}"
        had_old = target.exists()
        kept_data = None
        if had_old:
            old_data = target / "data"
            if old_data.is_dir():
                kept_data = self.root / f".keep-{pid}-{int(time.time() * 1000)}"
                try:
                    os.replace(str(old_data), str(kept_data))
                except Exception:
                    kept_data = None
        try:
            if staging.exists():
                _rmtree_retry(staging)
            staging.mkdir(parents=True, exist_ok=True)
            self._extract_into(data, staging, report.get("wrapped", False))
            if find_manifest(staging) is None:
                (staging / MANIFEST_NAME).write_text(
                    yaml_lite.dumps(report["manifest"]), encoding="utf-8")
            if kept_data is not None:
                os.replace(str(kept_data), str(staging / "data"))
                kept_data = None
            if had_old:
                os.replace(str(target), str(backup))
            os.replace(str(staging), str(target))
            if had_old:
                _rmtree_retry(backup)
            self._state()["enabled"].setdefault(pid, bool(enable))
            self._save_state()
            self._applied.pop(pid, None)
            self._settings_seeded.discard(pid)
            if callable(self._on_change):
                try:
                    self._on_change()
                except Exception as e:
                    print(f"[插件] 安装后回调失败: {e}")
            return {"success": True, "id": pid,
                    "name": report["manifest"]["name"],
                    "replaced": had_old, "report": report}
        except Exception as e:
            _rmtree_retry(staging)
            try:
                if backup.exists() and not target.exists():
                    os.replace(str(backup), str(target))
                if kept_data is not None and target.exists() \
                        and not (target / "data").exists():
                    os.replace(str(kept_data), str(target / "data"))
                    kept_data = None
            except Exception:
                pass
            return {"success": False, "error": f"安装失败：{e}", "report": report}
        finally:
            _rmtree_retry(backup)
            if kept_data is not None:
                _rmtree_retry(kept_data)

    def _extract_into(self, data: bytes, dest: Path, wrapped: bool) -> None:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                top, parts = _safe_extract_root(info.filename)
                if parts is None:
                    continue
                ext = Path(parts[-1]).suffix.lower()
                if ext not in ALLOWED_EXTS:
                    continue
                rel = parts[1:] if (wrapped and len(parts) > 1) else parts
                if not rel:
                    continue
                out = dest.joinpath(*rel)
                try:
                    out.resolve().relative_to(dest.resolve())
                except Exception:
                    continue
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)

    # ------------------------------------------------------------ 卸载
    def uninstall(self, pid: str, keep_settings: bool = False,
                  keep_data: bool = False) -> bool:
        d = self.plugin_dir(pid)
        if d is None or not d.is_dir():
            return False
        saved_enabled = self._state()["enabled"].pop(pid, None)
        saved_source = self._state()["source"].pop(pid, None)
        saved_pinned = pid in self._state()["pinned"]
        self._state()["pinned"] = [p for p in self._state()["pinned"] if p != pid]
        self._save_state()
        self._applied.pop(pid, None)
        self._settings_seeded.discard(pid)
        if callable(self._on_change):
            try:
                self._on_change()
            except Exception:
                pass
        stash = self._stash_kept_data(d, keep_settings, keep_data)
        last_err = None
        ok = _rmtree_retry(d, attempts=3, delay=0.15)
        if not ok:
            self._restore_stash(d, stash)
            if saved_enabled is not None:
                self._state()["enabled"][pid] = saved_enabled
            if saved_source is not None:
                self._state()["source"][pid] = saved_source
            if saved_pinned:
                self._state()["pinned"].insert(0, pid)
            self._save_state()
            last_err = "目录被占用（可能有程序正在读里面的文件）"
            print(f"[插件] 卸载失败: {last_err}")
            return False
        self._restore_kept_data(d, stash)
        return True

    def _restore_stash(self, d: Path, stash) -> None:
        if stash is None:
            return
        kept = stash / "data"
        try:
            if kept.is_dir():
                d.mkdir(parents=True, exist_ok=True)
                target = d / "data"
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                shutil.move(str(kept), str(target))
        except Exception as e:
            print(f"[插件] 卸载失败后恢复配置/数据失败: {e}")
        shutil.rmtree(stash, ignore_errors=True)

    def _stash_kept_data(self, d: Path, keep_settings: bool, keep_data: bool):
        data_dir = d / "data"
        if not data_dir.is_dir() or not (keep_settings or keep_data):
            return None
        stash = Path(tempfile.mkdtemp(prefix="lovomo_plugin_keep_"))
        kept = stash / "data"
        try:
            if keep_data:
                shutil.move(str(data_dir), str(kept))
            else:
                src = data_dir / SETTINGS_NAME
                if src.is_file():
                    kept.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(kept / SETTINGS_NAME))
        except Exception as e:
            print(f"[插件] 卸载前保留配置/数据失败: {e}")
            shutil.rmtree(stash, ignore_errors=True)
            return None
        return stash

    def _restore_kept_data(self, d: Path, stash) -> None:
        if stash is None:
            return
        kept = stash / "data"
        try:
            if kept.is_dir() and any(kept.iterdir()):
                d.mkdir(parents=True, exist_ok=True)
                shutil.move(str(kept), str(d / "data"))
                (d / UNINSTALLED_MARKER).write_text("", encoding="utf-8")
        except Exception as e:
            print(f"[插件] 卸载后恢复配置/数据失败: {e}")
        shutil.rmtree(stash, ignore_errors=True)

    # ------------------------------------------------------------ 皮肤聚合
    def theme_css(self) -> str:
        chunks = []
        for info in self.list_plugins():
            if not info.get("enabled"):
                continue
            d = Path(info["dir"])
            css_file = d / "theme.css"
            if not css_file.is_file():
                continue
            vars_css = self._skin_vars_css(info)
            if vars_css:
                chunks.append(vars_css)
            try:
                css = css_file.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            chunks.append(f"/* ===== 插件皮肤: {info['name']} "
                          f"(v{info['version']}) ===== */\n{css.strip()}")
        return "\n\n".join(chunks)

    def _skin_vars_css(self, info: dict) -> str:
        meta = info.get("skin")
        if not meta:
            return ""
        st = self.plugin_settings(info["id"])
        lines = []
        bg = st.get("background")
        if isinstance(bg, dict):
            url = _sanitize_background_url(bg.get("url") or bg.get("name") or "")
            if url:
                lines.append(f"  --skin-bg-image: url(\"{url}\");")
            size = str(bg.get("size") or "cover").lower()
            if size not in ("cover", "contain", "repeat", "auto"):
                size = "cover"
            if size == "repeat":
                lines.append("  --skin-bg-repeat: repeat;")
                lines.append("  --skin-bg-size: auto;")
            else:
                lines.append("  --skin-bg-repeat: no-repeat;")
                lines.append(f"  --skin-bg-size: {size};")
            try:
                opacity = max(0, min(100, int(bg.get("opacity", 100))))
            except (TypeError, ValueError):
                opacity = 100
            lines.append(f"  --skin-bg-opacity: {opacity / 100:.2f};")
        accent = st.get("accent") or meta.get("accent")
        if accent:
            safe = _sanitize_color(accent)
            if safe:
                lines.append(f"  --skin-accent: {safe};")
        lines.extend(self._custom_var_lines(info, st, meta.get("vars") or []))
        if not lines:
            return ""
        return ("/* ===== 插件皮肤设置: " + str(info.get("name")) + " ===== */\n"
                ":root {\n" + "\n".join(lines) + "\n}")

    @staticmethod
    def _custom_var_lines(info: dict, settings: dict, decls: list) -> list:
        lines = []
        for decl in decls:
            if not isinstance(decl, dict):
                continue
            name = decl.get("var") or ""
            if not name.startswith("--skin-"):
                continue
            raw = settings.get(decl.get("key"))
            value = _css_value_for(decl, raw)
            if value is None:
                continue
            lines.append(f"  {name}: {value};")
        return lines

    def active_skin_ids(self) -> list:
        out = []
        for info in self.list_plugins():
            if not info.get("enabled") or not info.get("skin"):
                continue
            if self.plugin_settings(info["id"]).get("appearance_enabled"):
                out.append(info["id"])
        return out

    # ------------------------------------------------------------ 图标
    @staticmethod
    def _declared_icon(d: Path, name):
        rel = str(name or "").replace("\\", "/").strip()
        if not rel or "/" in rel or rel.startswith("."):
            return None
        if Path(rel).suffix.lower() not in ICON_EXTS:
            return None
        f = Path(d) / rel
        return f if f.is_file() else None

    @staticmethod
    def _find_icon(d: Path):
        if d is None or not d.is_dir():
            return None
        try:
            files = {f.name.lower(): f for f in d.iterdir() if f.is_file()}
        except Exception:
            return None
        for stem in ICON_STEMS:
            for ext in ICON_EXTS:
                f = files.get(stem + ext)
                if f is not None:
                    return f
        return None

    @staticmethod
    def _find_doc(d: Path, kind: str):
        wanted = _DOC_NAMES.get(kind) or ()
        if d is None or not d.is_dir():
            return None
        try:
            files = {f.name.lower(): f for f in d.iterdir() if f.is_file()}
        except Exception:
            return None
        for name in wanted:
            f = files.get(name)
            if f is not None:
                return f
        return None

    def read_doc_text(self, pid: str, kind: str, limit: int = MAX_DOC_BYTES) -> dict:
        d = self.plugin_dir(pid)
        f = self._find_doc(d, kind) if d else None
        if f is None:
            return {"found": False, "name": "", "text": "", "truncated": False}
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {"found": False, "name": f.name, "text": "",
                    "error": f"读取失败：{type(e).__name__}: {e}"}
        truncated = len(text) > limit
        return {"found": True, "name": f.name, "text": text[:limit],
                "truncated": truncated, "size": len(text)}

    def icon_file(self, pid: str):
        info = self.get(pid)
        d = self.plugin_dir(pid)
        if info is None or d is None:
            return None
        return self._declared_icon(d, info.get("logo")) or self._find_icon(d)

    # ------------------------------------------------------------ 插件私有设置
    def data_dir(self, pid: str) -> Path:
        d = self.plugin_dir(pid)
        if d is None:
            return None
        target = d / "data"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def plugin_settings_file(self, pid: str) -> Path:
        d = self.data_dir(pid)
        return None if d is None else d / SETTINGS_NAME

    def _settings_of(self, pid: str) -> dict:
        f = self.plugin_settings_file(pid)
        if f is None or not f.is_file():
            return {}
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return raw if isinstance(raw, dict) else {}

    def commit_settings(self, pid: str = None) -> dict:
        if pid:
            self._applied[pid] = self._settings_of(pid)
            self._settings_seeded.add(pid)
            return {pid: self._applied[pid]}
        out = {}
        for info in self.list_plugins():
            self._applied[info["id"]] = self._settings_of(info["id"])
            self._settings_seeded.add(info["id"])
            out[info["id"]] = self._applied[info["id"]]
        return out

    def _applied_for(self, pid: str) -> dict:
        if pid not in self._applied:
            self._seed_applied(pid)
        return self._applied.get(pid) or {}

    def _seed_applied(self, pid: str) -> None:
        if pid in self._settings_seeded:
            return
        self._settings_seeded.add(pid)
        self._applied[pid] = self._settings_of(pid)

    def save_plugin_settings(self, pid: str, data) -> bool:
        self._seed_applied(pid)
        f = self.plugin_settings_file(pid)
        if f is None or not isinstance(data, dict):
            return False
        try:
            tmp = Path(f"{f}.{os.getpid()}.{time.time_ns()}.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(str(tmp), str(f))
            return True
        except Exception as e:
            print(f"[插件] 保存插件设置失败: {e}")
            return False

    def plugin_settings(self, pid: str) -> dict:
        if self.get(pid) is None:
            return {}
        return dict(self._applied_for(pid))

    def field_specs(self, pid: str) -> dict:
        info = self.get(pid)
        if not info:
            return {}
        specs = {}
        for field in info.get("features") or []:
            specs[field["key"]] = field
        for field in (info.get("skin") or {}).get("vars") or []:
            specs[field["key"]] = field
        return specs

    def filter_settings(self, pid: str, data) -> dict:
        if not isinstance(data, dict):
            return {}
        specs = self.field_specs(pid)
        out = {}
        for key, value in data.items():
            field = specs.get(key)
            if field is None:
                if key == "appearance_enabled":
                    out[key] = _as_bool(value)
                elif key == "background" and isinstance(value, dict):
                    out[key] = value
                elif key == "accent":
                    color = _sanitize_color(str(value or ""))
                    if color:
                        out[key] = color
                continue
            ok, clean = _validate_setting(field, value)
            if ok:
                out[key] = clean
        return out

    def asset_file(self, pid: str, name: str, root_scope: bool = False):
        d = self.plugin_dir(pid) if root_scope else self.data_dir(pid)
        if d is None or not name:
            return None, ""
        rel = str(name).replace("\\", "/").lstrip("/")
        if not rel or ".." in rel.split("/"):
            return None, ""
        target = d.joinpath(*[p for p in rel.split("/") if p not in ("", ".")])
        try:
            target = target.resolve()
            target.relative_to(d.resolve())
        except Exception:
            return None, ""
        if not target.is_file():
            return None, ""
        ext = target.suffix.lower()
        if ext not in ASSET_EXTS:
            return None, ""
        try:
            if target.stat().st_size > MAX_ASSET_BYTES:
                return None, ""
        except Exception:
            return None, ""
        return target, _MIME_TYPES.get(ext, "application/octet-stream")

    # ------------------------------------------------------------ 校验辅助
    @staticmethod
    def sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()


def _dir_size(d: Path) -> int:
    total = 0
    try:
        for p in d.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
    except Exception:
        pass
    return total
