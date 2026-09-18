"""GitHub 加速镜像：给匿名只读请求准备候选地址。

只用于**不带任何凭据**的读请求（插件市场、raw 文件、插件包下载、版本信息）。
带 token 的写操作（发布插件）必须直连官方 —— 请求头里的 Authorization 一旦
经过镜像，就等于把 token 交给镜像运营方，所以那边一行镜像代码都不接。

模板写法（每行一个，配置项 `github_mirrors`）：

    {url}                     前缀式：把完整官方地址拼在后面
    {repo} / {ref} / {path}   raw 文件式：只对 raw.githubusercontent.com 生效
"""
import re

# 默认镜像：按实测可用性与能力排序（前缀式里 gh-proxy 连 api/codeload 都能过，
# 另两个只过 raw；jsDelivr 是 CDN，最稳但会缓存，放在最后兜底）
DEFAULT_MIRRORS = (
    "https://gh-proxy.com/{url}",
    "https://ghfast.top/{url}",
    "https://ghproxy.net/{url}",
    "https://cdn.jsdelivr.net/gh/{repo}@{ref}/{path}",
    "https://gcore.jsdelivr.net/gh/{repo}@{ref}/{path}",
)

# raw.githubusercontent.com/<owner>/<repo>/<ref>/<path...>
_RAW_RE = re.compile(r"^https://raw\.githubusercontent\.com/"
                     r"([^/]+/[^/]+)/([^/]+)/(.+)$")
_URL_RE = re.compile(r"^https?://[^\s]+$")
# 上次成功过的候选：同一主机下次优先用它，省掉反复等直连超时
_PREFERRED = {}
# 上次用过的镜像列表：配置一变（比如用户按测速结果重排过）就丢掉上面的记忆，
# 否则用户排好的顺序会被旧偏好顶掉
_LAST_MIRRORS = None


def parse_mirrors(raw) -> tuple:
    """把配置里的镜像模板解析成元组（一行一个，去空去重，只留合法地址）。"""
    items = raw if isinstance(raw, (list, tuple)) else str(raw or "").splitlines()
    out = []
    for item in items:
        text = str(item or "").strip()
        if not text or text.startswith("#"):
            continue
        if "{url}" not in text and "{repo}" not in text:
            continue
        if not _URL_RE.match(text.replace("{url}", "https://x").replace("{repo}", "x")
                             .replace("{ref}", "x").replace("{path}", "x")):
            continue
        if text not in out:
            out.append(text)
    return tuple(out)


def apply_template(template: str, url: str) -> str:
    """按模板拼出一个候选地址；模板跟这个地址不匹配时返回空串。"""
    if "{url}" in template:
        return template.replace("{url}", url)
    m = _RAW_RE.match(url)
    if not m:
        return ""
    repo, ref, path = m.groups()
    return (template.replace("{repo}", repo).replace("{ref}", ref)
            .replace("{path}", path))


def candidates(url: str, mirrors) -> list:
    """列出一个官方地址的候选（官方原址 + 各镜像），上次成功的排最前。"""
    global _LAST_MIRRORS
    url = str(url or "").strip()
    if not _URL_RE.match(url):
        return []
    key = tuple(str(m) for m in (mirrors or ()))
    if key != _LAST_MIRRORS:
        _PREFERRED.clear()
        _LAST_MIRRORS = key
    opts = [url]
    for template in mirrors or ():
        made = apply_template(str(template), url)
        if made and made not in opts:
            opts.append(made)
    preferred = _PREFERRED.get(_host(url))
    if preferred in opts and opts[0] != preferred:
        opts.remove(preferred)
        opts.insert(0, preferred)
    return opts


def note_success(url: str, used: str) -> None:
    """记住这次成功用的是哪个候选（换候选时返回 True，调用方据此打日志）。"""
    host = _host(url)
    if not host:
        return False
    changed = _PREFERRED.get(host) != used
    _PREFERRED[host] = used
    return changed


def _host(url: str) -> str:
    m = re.match(r"^https?://([^/]+)", str(url or ""))
    return m.group(1).lower() if m else ""


def mirror_label(url: str, base: str) -> str:
    """给日志用的短名：官方地址显示 `直连`，镜像显示它的主机名。"""
    return "直连" if str(url) == str(base) else (_host(url) or str(url))


def template_host(template: str) -> str:
    """模板对应的主机名（前缀式模板取前缀那一段的主机）。"""
    return _host(str(template).split("{", 1)[0])


def probe_urls(repo: str) -> tuple:
    """测速用的探测地址：raw 与 api 各一条（HEAD 不依赖具体分支名）。"""
    return (("raw", f"https://raw.githubusercontent.com/{repo}/HEAD/README.md"),
            ("api", f"https://api.github.com/repos/{repo}"))
