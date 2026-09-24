"""插件系统：本地插件包的安装、安全审查、启用/禁用与热加载。

设计要点
--------
- **存储**：插件全部落在 `%LOCALAPPDATA%\\Lovomo\\plugins\\<插件id>\\`，
  跟程序目录解耦，重装/更新程序不会丢插件。
- **市场**：不依赖自建服务器。市场的主数据源是仓库里的一批分支
  （名字以配置的「插件分支前缀」开头，一条分支 = 一个插件），
  WebUI 点「插件市场」时从 GitHub 拉取；分支一个都没扫到时才回退读
  仓库里的 JSON 索引（`plugins/index.json`）。以后想换托管，只改配置即可。
- **安全**：完整 Python 插件等同于本机任意代码执行，所以安装前一律静态
  扫描并给出风险分级清单，高危项必须用户显式确认。

插件包结构（.zip，或直接一个目录）
----------------------------------
    plugin.yaml        必需，清单文件（plugin.yml / plugin.json 兼容老包，见下）
    main.py            可选，完整 Python 插件入口（高危）
    theme.css          可选，皮肤样式
    webui.html         可选，插件自己的网页界面（「打开webui」按钮，见下）
    logo.png           可选，图标（只认 logo.<主流图片格式>）
    README.md          可选
    update.md          可选，历史更新说明

清单格式：plugin.yaml 优先
--------------------------
按 `plugin.yaml` → `plugin.yml` → `plugin.json` 的顺序取**第一个存在**的文件。
YAML 用 `modules/yaml_lite.py` 解析（零依赖），写法就是普通 YAML。
展示用的元信息（介绍 / 版本 / 作者 / 仓库 / 图标）全部写在这里。

清单字段
--------
    id          str   必需，唯一标识（字母数字下划线连字符，须字母数字开头，≤64 位）
    name        str   必需，显示名
    version     str   必需，版本号
    author      str   可选，显示用作者名
    github      str   可选，作者 GitHub 用户名（发布归属校验用）
    repo        str   可选，"用户名/仓库名"，插件所在仓库
    homepage    str   可选，插件主页链接
    source_repo str   可选，由市场安装时自动记入，标明插件来自哪个仓库
    logo        str   可选，图标文件名（不带路径），留空则自动找 logo.* / icon.*
    description str   可选
    tags        list  可选，市场与列表里显示的标签（≤8 个，每个 ≤24 字）
    type        str   "theme" | "python" | "mixed"，默认 "python"
    entry       str   可选，Python 入口文件，默认 main.py
    panel       dict  可选，声明插件自带的功能页（label / title / html）
    skin        dict  可选，声明皮肤可调项（background / layout / accent / vars）
    features    list  可选，声明插件自己的可调项，前端按声明自动渲染控件
                       （select 可以写 `options_file: data/xxx.json`，
                       由插件在运行时把选项写进这个文件，主程序读它渲染下拉）
"""
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
# 清单文件名，按优先级排列（第一个是新建清单时用的名字）
MANIFEST_NAMES = ("plugin.yaml", "plugin.yml", "plugin.json")
MANIFEST_NAME = MANIFEST_NAMES[0]
# 插件自带网页界面的固定文件名（放在插件根目录）
WEBUI_NAME = "webui.html"
# 插件私有设置文件名（放在插件 data 目录里）
SETTINGS_NAME = "settings.json"
# 卸载时选择保留配置/数据后，在插件目录里留这个标记：目录还在，但不再算已安装
UNINSTALLED_MARKER = ".uninstalled"
# 解压后总大小与文件数上限（防 zip 炸弹，同时容得下插件自带的纯 Python 库）
MAX_UNPACK_BYTES = 128 * 1024 * 1024
MAX_FILES = 2000

# GitHub 用户名/仓库名出现的位置：URL 或 "owner/repo" 简写
_GITHUB_URL_RE = re.compile(r"github\.com[/:]+([A-Za-z0-9_.\-]+)", re.I)
_REPO_PATH_RE = re.compile(
    r"github\.com[/:]+([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)", re.I)
# GitHub 用户名：字母数字开头，可含内部连字符，最长 39 位
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")


# 插件自带的静态资源只允许这些类型经由 WebUI 直接读取
ASSET_EXTS = {
    ".png", ".jpg", ".jpeg", ".jfif", ".gif", ".svg", ".webp", ".avif",
    ".apng", ".bmp", ".ico",
    ".css", ".js", ".mjs", ".html", ".htm", ".json", ".txt", ".csv", ".xml",
    ".map", ".wasm",
    ".woff", ".woff2", ".ttf", ".ttc", ".otf", ".eot",
    ".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus", ".webm", ".mp4",
}
MAX_ASSET_BYTES = 32 * 1024 * 1024

# 插件图标的文件名（按优先级）与允许的图片格式
ICON_STEMS = ("logo", "icon")
ICON_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico", ".bmp")

# 插件自带说明文档的文件名（大小写不敏感）
MAX_DOC_BYTES = 256 * 1024
_DOC_NAMES = {
    "readme": ("readme.md", "readme.txt", "readme.markdown"),
    "update": ("update.md", "update.txt", "updates.md", "updates.txt",
               "changelog.md", "changelog.txt", "history.md", "history.txt"),
}

# Windows 文件名不允许出现的字符
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
# 按危险程度分级：high 必须用户显式确认才允许启用；medium 提示但不阻断。
# high 只留「装上就可能是冲着用户来的」那几类；子进程、动态执行、网络套接字、
# 删单个文件这些插件开发里的常见写法降为 medium，避免正常插件被拦在确认框后面。
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
    """静态扫描一段 Python 代码，返回风险条目列表。

    每条形如 {"level": "high"|"medium"|"low", "title": str, "hits": [行号...]}
    """
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
    """把风险清单汇总成计数与是否放行建议。"""
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
    """把 zip 内的路径压平到单层目录名，挡住 ../ 穿越。

    返回 (top_dir_or_None, relative_parts)。遇到绝对路径或 .. 时返回
    (None, None) 表示该条目非法。
    """
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
    """在目录里按优先级找清单文件，找不到返回 None。"""
    if root is None:
        return None
    for name in MANIFEST_NAMES:
        f = Path(root) / name
        if f.is_file():
            return f
    return None


def parse_manifest_text(text: str, filename: str = "") -> dict:
    """把清单文本解析成字典；解析不了返回空字典（不抛异常）。

    `.json` 走 JSON，其余按 YAML 解析。解析失败只当"没有清单"处理，
    由调用方决定是提示还是兜底，绝不让一个写坏的清单把安装流程打挂。
    """
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
    """从 `用户名/仓库名` 或各种 GitHub 链接里取用户名，取不到返回空串。"""
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
    """从链接或简写里取 `用户名/仓库名`，取不到返回空串。"""
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
    """清单里所有能标识"这个插件属于谁"的 GitHub 用户名（去重、保序）。

    认这些来源：`github` / `owner` 直接写的用户名、`repo` / `source_repo` /
    `homepage` 里的仓库归属、以及本身就是一个 GitHub 用户名的 `author`。
    不像用户名的值（中文作者名、带空格、带斜杠）一律跳过 —— 否则
    `author: 爱丽丝` 会被误当成一个账号，把归属判断带偏。
    """
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
    """清单声明的归属里有没有这个 GitHub 账号（没有就视为"不是你的插件"）。"""
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
    """规范化插件清单里的 panel 段：声明插件自带的功能页。

    两种玩法，插件自己挑：

    - **只声明** —— `{"label": "喵喵设置", "title": "喵喵皮肤"}`。
      功能页由前端按 skin / features 自动渲染，插件一行 HTML 都不用写。
    - **自带界面** —— 额外写 `"html": "ui/panel.html"`（zip 内相对路径）。
      功能页会直接加载这个 HTML，插件想画成什么样就什么样，主程序只
      通过 `window.lovomo` 桥接提供设置读写、资源 URL、日志等能力。

    指向的文件缺失时自动退回自动渲染，插件不会因此变成一个打不开的死页面。
    """
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
    """把一个变量名收敛成合法的 CSS 变量片段。

    只留小写字母、数字和连字符；`--skin-` 前缀由主程序补，插件写名字时
    带不带前缀都行。
    """
    name = re.sub(r"[^a-z0-9\-]", "-", str(raw or "").strip().lower())
    name = re.sub(r"-+", "-", name).strip("-")
    if name.startswith("skin-"):
        name = name[5:]
    return name[:32]


def _normalize_field(item: dict, index: int) -> dict:
    """规范化单条字段声明，支持 10 种控件类型。

    `type` 省略时按 old-school 布尔开关处理（等价 type="bool"，保留
    `default` 是布尔的旧写法），所以老插件清单不需要任何改动。
    """
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
        # 写了个主程序不认识的类型：退回布尔，别让整条字段消失
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
            # 选项由插件运行时写进这个文件，主程序渲染前读它填进 options
            out["options_file"] = options_file
        if not out["options"] and not options_file:
            return {}
        if t == "select":
            vals = [o["value"] for o in out["options"]]
            if not vals and options_file:
                # 选项要等运行时读 options_file 才知道，此时不能按"空选项"把
                # 清单里声明的默认值改掉（_features_with_options 之后会补上选项）
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
    """把清单里写的相对路径规整成安全的相对路径（越界或为空返回空串）。"""
    rel = str(value or "").replace("\\", "/").strip().lstrip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return ""
    return "/".join(parts)


def _normalize_options(raw) -> list:
    """下拉选项：接受 ["a","b"] 或 [{"value":..,"label":..}] 两种写法。"""
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
    """布尔值容错：兼容清单/请求里写成字符串的 "false" / "0" / "否"。

    直接 bool("false") 会得到 True，把声明的默认值或用户的选择反向。
    """
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return fallback
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() not in ("false", "0", "no", "off", "否", "关", "关闭")


def _normalize_features(raw) -> list:
    """规范化插件清单里的 features 段：声明插件对外暴露的可调项。

    每项至少给 `key`，其余可选。`type` 决定前端自动渲染成什么控件：

        bool / range / number / text / textarea / select / multiselect /
        color / password / file

    示例：
        {"key": "meow_reply_enabled", "label": "喵喵追加", "type": "bool"}
        {"key": "block_opacity", "label": "区块透明度", "type": "range",
         "min": 0.2, "max": 1, "step": 0.01, "default": 0.92}
        {"key": "mode", "label": "模式", "type": "select",
         "options": ["安静", "活泼"], "default": "安静"}

    前端据此自动渲染，值存进插件私有 settings.json，插件运行时自己读。
    写清单就能加设置项，不必改主程序或前端；真想要完全自定义的界面，
    再用 panel.html 自己画（见 `_normalize_panel`）。
    """
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
    """规范化插件清单里的 skin 段：皮肤提供哪些可调项。

    固定项里 `background` / `accent` 会渲染出控件（背景图选择器、主色
    取色器），`layout` 只是保留的标记位、当前界面不为它渲染控件。
    真正想要"任意可调项"的插件应该用 `vars`：

        "skin": {
          "vars": [
            {"name": "block-opacity", "label": "区块透明度", "type": "range",
             "min": 0.2, "max": 1, "step": 0.01, "default": 0.92},
            {"name": "radius", "label": "圆角", "type": "range",
             "min": 0, "max": 30, "default": 14}
          ]
        }

    每个变量会在 CSS 里以 `--skin-<name>` 的形式出现（名字带不带
    `skin-` 前缀都行），插件在 theme.css 里用 var() 取用即可。
    主程序不再规定"皮肤只能调哪几样"。
    """
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
    """把用户选中的文件名转成磁盘上可用的名字（保留中文，只替换非法字符）。"""
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
    """同名不要互相覆盖：第二个开始加 -2、-3 …（保留扩展名）。"""
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
    """把背景图设置转成安全的站内 URL（文件名做百分号编码）。"""
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
    """把任意文本压成一个安全的 CSS 字符串字面量。

    保留 CJK 与常见标点（下拉选项、自定义文案用得上），只滤掉引号、
    反斜杠、换行和尖括号 —— 这些是唯一能突破字符串边界、改写样式表
    结构的字符。
    """
    clean = (str(text or "")
             .replace("\\", "").replace("\n", " ").replace("\r", " ")
             .replace('"', "").replace("'", ""))
    clean = re.sub(r"[<>{};]", "", clean)[:limit]
    return '"' + clean + '"'


def _css_unit_of(decl: dict) -> str:
    """清单里声明的 unit，只有白名单内的 CSS 长度单位才会被写进样式表。

    只允许长度单位：像 "次"、"个" 这种是给界面当后缀看的，写进 CSS 会让
    整条声明失效（浏览器认不出 3次 这种长度）。
    """
    unit = str(decl.get("unit") or "").strip()
    return unit if unit in CSS_LENGTH_UNITS else ""


def _css_value_for(decl: dict, raw) -> str:
    """把一个插件设置值渲染成可以安全写进 CSS 的变量值。

    插件作者能用任意变量名和任意取值范围，所以这里是唯一的收口：
    每个类型都只有一种确定形态的输出，插件没法借"值"往样式表里塞
    额外声明（比如 `1; } body { display: none } /*`）。

    返回 None 表示这条变量不该输出，前端会退回 theme.css 里的兜底值。
    """
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
        # 只允许指向插件资源接口的站内相对路径（复用背景图那套收敛）。
        url = _sanitize_background_url(str(raw))
        return "url(\"" + url + "\")" if url else None

    return None


def _fmt_num(num: float) -> str:
    """把浮点压成短字符串：整数不带小数点，小数最多留 4 位。"""
    if abs(num - round(num)) < 1e-9:
        return str(int(round(num)))
    return f"{num:.4f}".rstrip("0").rstrip(".")


def _validate_setting(field: dict, value):
    """按字段声明校验并收敛一个设置值。

    返回 `(ok, value)`；ok 为 False 时调用方应丢弃该键，避免插件把
    任意结构写进自己的 settings.json 之后又读出来当配置用。
    """
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
    """删目录，失败就重试几次。

    Windows 上文件被别的进程短暂占着时会直接拒绝删除（PermissionError），
    而这种情况往往几十毫秒后就自己好了。默认的 rmtree(ignore_errors=True)
    一失败就放弃，于是临时目录永远留在那里 —— 插件装了几次，根目录里
    就多出几个 .backup-xxx，看起来像"一个插件好几个文件夹"。
    """
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
    """插件目录的唯一管理者。所有路径都限制在 plugins_root 之内。"""

    def __init__(self, plugins_root: Path, state_file: Path = None,
                 loader=None, on_change=None):
        self.root = Path(plugins_root)
        self.state_file = Path(state_file) if state_file else self.root / "state.json"
        self._loader = loader            # 由一个上层注入：真正 import 插件
        self._on_change = on_change      # 启用状态变化后的回调（热重载）
        self._state_cache = None
        # 生效快照：pid -> settings。plugin_settings() 读这里而不是磁盘，
        # 目的是让"保存"与"生效"分开 —— 用户写完设置点「保存并重载」
        # 才把磁盘上的草稿提交进来，否则改了立刻生效、滑条一拖一动。
        self._applied: dict = {}
        self._settings_seeded: set = set()
        self.purge_temp_dirs()

    # ------------------------------------------------------------ 基础
    def ensure_root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    # 安装过程用的临时目录前缀。它们只在一次 install_zip 期间存在，
    # 正常情况下会在 finally 里被删掉；一旦安装中途崩溃/被强杀，
    # 或者删除时插件目录里的文件还被别的进程占着（Windows 常见），
    # 就会永久留在插件根目录里，看起来像"一个插件好几个文件夹"。
    _TEMP_PREFIXES = (".staging-", ".backup-", ".keep-")

    def purge_temp_dirs(self) -> int:
        """清掉残留的安装临时目录，返回清理数量。

        在插件管理器初始化时调用一次即可：这些目录只是搬运途中的中转站，
        里面的内容要么已经搬进正式目录、要么是待回滚的旧副本，
        没有任何一份是"只有它才有"的数据，删掉是安全的。
        """
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
        # 置顶顺序：列表里靠前的排在「插件」页最前面
        data.setdefault("pinned", [])
        if not isinstance(data.get("pinned"), list):
            data["pinned"] = []
        data["pinned"] = [str(p) for p in data["pinned"] if str(p)]
        self._state_cache = data
        return data

    def _save_state(self) -> None:
        try:
            self.ensure_root()
            # 临时名必须唯一：并发写入共用同一个 .tmp 时，
            # os.replace 可能把另一次写入的半截内容提交成正式文件
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
        """置顶的插件 id，靠前的排最前。"""
        return list(self._state()["pinned"])

    def set_pinned(self, pid: str, pinned: bool) -> bool:
        """置顶 / 取消置顶。新置顶的排在最前面。"""
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
        """记下插件是从哪个仓库装来的（市场安装时写，便于追溯来源）。"""
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
        """取插件记录的来源（没有记录时返回空字典）。"""
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
            # 卸载时选了保留配置/数据留下的目录：里面的东西留着给重装用，
            # 但它已经不是插件了，不该再出现在列表里。
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
        # 清单缺失时不报错，但要让用户看出来是个"裸目录"
        info["manifest_ok"] = bool(raw)
        # 归属：清单里声明了哪些 GitHub 账号，发布门禁按它判断"是不是你的插件"
        info["owners"] = manifest_logins(manifest)
        # 来源：市场装来的插件会记一笔，手工上传/本地目录则为空
        src = self.source_of(manifest["id"])
        if src.get("repo"):
            info["source_repo"] = src["repo"]
            info["source_branch"] = str(src.get("branch") or "")
        # 插件自带功能页界面（panel.html）：文件真的存在才算数，
        # 没写或写错了就退回前端自动渲染，不至于变成一个打不开的死页面。
        panel = info.get("panel") or {}
        if panel.get("html"):
            info["panel"] = {**panel, "html_ok": self.panel_html_path(d, panel) is not None}
        return info

    def panel_html_path(self, d: Path, panel: dict):
        """解析插件自带界面的 HTML 文件，越界或不存在都返回 None。"""
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
        """给声明了 options_file 的下拉补上选项。

        插件清单是静态的，写不了"运行时才知道的选项"（比如本机有哪些录音
        设备）。所以留一个口子：清单里写 `options_file: data/xxx.json`，
        主程序在把插件信息交给前端之前读一次，把选项填进 `options`。
        文件不在或格式不对就退回清单里写死的 options。
        """
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
        """读插件目录里的选项文件：["值", ...] 或 [{"value":..,"label":..}, ...]。"""
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
        """只读地审查一个插件 zip，不落盘。用于"安装前提示风险"。"""
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
                    # 同名的取第一个；优先级（yaml > json）留到最后再挑
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
            # 顶层目录唯一时允许"包了一层文件夹"的 zip
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
        """安装一个 zip 插件包。force=True 表示用户已确认高危风险。

        enable 只管新装的插件：默认装完是停用状态，由用户自己去「插件」里
        启用；覆盖安装一律沿用原有状态，升级不该把用户的开关改掉。
        """
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
        # 覆盖安装：先解到临时目录，成功了再替换，避免解压中途失败留下半成品
        staging = self.root / f".staging-{pid}-{int(time.time() * 1000)}"
        backup = self.root / f".backup-{pid}-{int(time.time() * 1000)}"
        had_old = target.exists()
        # 覆盖安装时把插件自己的 data 目录先挪出来：那里存的是用户设置
        # （背景图、开关状态等），升级插件不该把它们清掉。
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
                # 没清单也允许装，补一份生成的清单，便于后续统一管理
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
            # 新装的插件默认停用，装完由用户自己去「插件」里启用；
            # 覆盖安装沿用原有状态（setdefault 只在没有记录时生效）
            self._state()["enabled"].setdefault(pid, bool(enable))
            self._save_state()
            # 覆盖安装保留了旧的 data/，但设置要重新补种一次：
            # 新版本的清单可能声明了新的键，旧的生效快照不一定对得上。
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
            # 回滚：备份还在就还原回去，用户数据也要放回原位
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
        """把 zip 内容安全解压到 dest。已在外层校验过路径，这里再挡一遍。"""
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
        """卸载插件：先让运行时释放模块，再删目录。

        顺序很重要：Windows 上插件模块还在 sys.modules / 文件句柄未释放时
        rmtree 会失败，表现为"点了卸载但插件还在"。所以先通知上层卸掉运行时
        （_on_change 会触发 reload_all，把 sys.modules 里的模块摘掉），
        再删除目录；万一仍被占用（编辑器/杀软扫描），短暂重试几次。

        keep_data 保留整个 `data/` 目录；只 keep_settings 时保留其中的
        settings.json。要留的东西先挪到临时目录，删完再放回来 ——
        Windows 上没法"只删目录里的一部分"。
        """
        d = self.plugin_dir(pid)
        if d is None or not d.is_dir():
            return False
        saved_enabled = self._state()["enabled"].pop(pid, None)
        saved_source = self._state()["source"].pop(pid, None)
        saved_pinned = pid in self._state()["pinned"]
        self._state()["pinned"] = [p for p in self._state()["pinned"] if p != pid]
        self._save_state()
        # 卸掉生效快照：重装同一个 id 时应该重新从磁盘补种，
        # 而不是留着上一份（可能是旧版本的）设置。
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
            # 目录没删掉，插件其实还在：把挪走的配置/数据放回原位、状态也恢复回去，
            # 否则会变成「插件还在、却显示未启用且来源未知」，用户选的保留配置也一起丢了。
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
        """卸载失败时把挪走的 data/ 放回插件目录（不打卸载标记，插件仍是插件）。"""
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
        """把要保留的配置/数据挪到临时目录，返回该目录（没有要留的返回 None）。"""
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
                # 目录留着但不算已安装：标记一下，重装时会被新目录整个替换掉
                (d / UNINSTALLED_MARKER).write_text("", encoding="utf-8")
        except Exception as e:
            print(f"[插件] 卸载后恢复配置/数据失败: {e}")
        shutil.rmtree(stash, ignore_errors=True)

    # ------------------------------------------------------------ 皮肤聚合
    def theme_css(self) -> str:
        """把所有启用的皮肤插件拼成一段 CSS，供 WebUI 注入。

        每个插件的样式被包在 :root 作用域下的注释块里，方便排查。
        声明了 skin 段的插件，其私有设置会被翻译成 `--skin-*` 变量前置，
        这样插件 CSS 用 var() 引用即可，无需自己生成样式文本。
        """
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
        """把插件私有设置翻译成 `--skin-*` CSS 变量。

        两条来源合并：主程序认识的固定项（background / accent），以及
        插件在 `skin.vars` 里自己声明的任意变量。后者是主力 —— 主程序
        不再规定"皮肤只能调哪几样"，变量名和取值范围都由插件定。
        """
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
        """按声明把插件自定义变量渲染成 CSS 行。

        值取插件私有设置，缺失或类型不符时退回声明的默认值。所有值都经
        `_css_value_for` 收敛，插件没法借变量名或值往样式表里注入别的规则。
        """
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
        """哪些皮肤插件的「应用外观」是开着的。

        前端据此给 html 加 skin-on 类；theme.css 里改底色的规则都挂在
        html.skin-on 下，所以没主动开的插件只贡献配色与排版，
        程序原本的背景保持不变。

        用 `plugin_settings()`（生效值）而不是磁盘草稿：用户刚在功能页里
        勾上开关但还没点「保存并重载」时，界面不该先变了。
        """
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
        """清单里 `logo:` 声明的图标（只认文件名，不允许带路径）。

        声明的文件不存在或格式不在白名单里时返回 None，让调用方退回
        `logo.*` / `icon.*` 的自动查找，不至于因为写错一个字段就没有图标。
        """
        rel = str(name or "").replace("\\", "/").strip()
        if not rel or "/" in rel or rel.startswith("."):
            return None
        if Path(rel).suffix.lower() not in ICON_EXTS:
            return None
        f = Path(d) / rel
        return f if f.is_file() else None

    @staticmethod
    def _find_icon(d: Path):
        """找插件根目录里的图标文件（logo.* 优先，其次 icon.*）。"""
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
        """在插件根目录里按文件名找说明文档（大小写不敏感）。"""
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
        """读插件自带的说明文档（readme / update），超长时截断。"""
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
        """插件自己的可写目录。插件只应在这里落盘，卸载时随之删除。"""
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
        """读取插件私有设置（磁盘上的草稿），损坏或缺失时返回空字典。"""
        f = self.plugin_settings_file(pid)
        if f is None or not f.is_file():
            return {}
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return raw if isinstance(raw, dict) else {}

    def commit_settings(self, pid: str = None) -> dict:
        """把磁盘上的设置草稿提交为"已生效"，返回被提交的 {pid: settings}。

        这是「保存并重载」的后半程：`plugin_settings()` / `theme_css()` 读的都是
        生效快照，只有走这里才会看到磁盘上的新值。
        """
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
        """取某个插件的生效设置，必要时用磁盘内容补种一次。

        补种只发生在"从来没有过这个插件的快照"时：程序启动后第一次读就靠它
        拿到用户上次保存的设置。一旦写过草稿，快照里必然已经有旧值（见
        `save_plugin_settings`），不会在这里被草稿偷偷覆盖。
        """
        if pid not in self._applied:
            self._seed_applied(pid)
        return self._applied.get(pid) or {}

    def _seed_applied(self, pid: str) -> None:
        """用磁盘上的内容初始化某个插件的生效快照（只做一次）。"""
        if pid in self._settings_seeded:
            return
        self._settings_seeded.add(pid)
        self._applied[pid] = self._settings_of(pid)

    def save_plugin_settings(self, pid: str, data) -> bool:
        """整份替换插件私有设置（原子写，失败不破坏原文件）。

        写的是"草稿"：不会影响 `plugin_settings()` 读到的生效值，
        要等 `commit_settings()` 才生效。
        """
        # 先把快照补种成"写之前"的样子，否则第一次读会因为快照为空、
        # 直接从磁盘补种，把刚写的草稿当成生效值（等于没有暂存）。
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
        """插件私有设置（已生效的那份）。插件未安装时返回空字典。"""
        if self.get(pid) is None:
            return {}
        return dict(self._applied_for(pid))

    def field_specs(self, pid: str) -> dict:
        """插件声明的所有可调项：{key: 字段声明}。

        features 与 skin.vars 合并到一起交给前端，于是"功能开关"和
        "皮肤变量"能用同一套渲染逻辑，插件也只需要学一种写法。
        """
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
        """按插件声明的字段筛一遍要落盘的设置。

        只保留声明过的键，并按声明类型收敛值；插件的 `data/` 目录里
        已经有别的东西（背景图、用户传的文件）时也不受影响，因为写的是
        独立的 settings.json。未声明的键一律丢弃 —— settings.json 是
        给插件读的配置，不该变成任意 JSON 的暂存区。
        """
        if not isinstance(data, dict):
            return {}
        specs = self.field_specs(pid)
        out = {}
        for key, value in data.items():
            field = specs.get(key)
            if field is None:
                # 插件无权声明的键：只放行几个主程序认识的内建键
                if key == "appearance_enabled":
                    out[key] = _as_bool(value)
                elif key == "background" and isinstance(value, dict):
                    out[key] = value
                elif key == "accent":
                    # 主色由「外观」区块的取色器写入，_skin_vars_css 会翻成
                    # --skin-accent。漏掉它就会「改完保存又变回清单里的默认色」。
                    color = _sanitize_color(str(value or ""))
                    if color:
                        out[key] = color
                continue
            ok, clean = _validate_setting(field, value)
            if ok:
                out[key] = clean
        return out

    def asset_file(self, pid: str, name: str, root_scope: bool = False):
        """解析插件目录下的静态资源，返回 (Path, mime) 或 (None, "")。

        默认只在插件私有 data 子目录里取（用户上传的图片等）；
        `root_scope=True` 时改为在插件根目录取，供自带界面（panel.html）
        及其 css/js 使用。两种作用域都要求解析后的真实路径仍落在插件
        目录内，且后缀在 ASSET_EXTS 白名单里。
        """
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
