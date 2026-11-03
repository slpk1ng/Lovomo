import re

DEFAULT_MIRRORS = (
    "https://gh-proxy.com/{url}",
    "https://githubproxy.cc/{url}",
    "https://ghproxy.link/{url}",
)

# raw.githubusercontent.com/<owner>/<repo>/<ref>/<path...>
_RAW_RE = re.compile(r"^https://raw\.githubusercontent\.com/"
                     r"([^/]+/[^/]+)/([^/]+)/(.+)$")
_URL_RE = re.compile(r"^https?://[^\s]+$")
_PREFERRED = {}
# 否则用户排好的顺序会被旧偏好顶掉
_LAST_MIRRORS = None


def parse_mirrors(raw) -> tuple:
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
    if "{url}" in template:
        return template.replace("{url}", url)
    m = _RAW_RE.match(url)
    if not m:
        return ""
    repo, ref, path = m.groups()
    return (template.replace("{repo}", repo).replace("{ref}", ref)
            .replace("{path}", path))


def candidates(url: str, mirrors) -> list:
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
    return "直连" if str(url) == str(base) else (_host(url) or str(url))


def template_host(template: str) -> str:
    return _host(str(template).split("{", 1)[0])


def probe_urls(repo: str) -> tuple:
    return (("raw", f"https://raw.githubusercontent.com/{repo}/HEAD/README.md"),
            ("api", f"https://api.github.com/repos/{repo}"))
