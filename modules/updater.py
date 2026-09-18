import re
from typing import Tuple

APP_VERSION = "1.2.0.0"

_VERSION_RE = re.compile(
    r"^[vV]?\s*([0-9]+(?:\.[0-9]+)*)\s*(?:[-_+.\s]+(.*))?$", re.S)


def version_key(v) -> Tuple:
    text = str(v or "").strip()
    match = _VERSION_RE.match(text)
    if not match:
        return ((0,), 0, ())
    numbers = [int(x) for x in match.group(1).split(".")]
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    tail = match.group(2) or ""
    parts = []
    for token in re.split(r"[.\-_+\s]+", tail):
        token = token.strip()
        if not token:
            continue
        if token.isdigit():
            parts.append((0, int(token), ""))
        else:
            parts.append((1, 0, token.lower()))
    return (tuple(numbers), 0 if parts else 1, tuple(parts))


def is_newer(latest, current) -> bool:
    """latest 是否比 current 更新（支持 beta/rc 等预发布版本号）。"""
    if not str(latest or "").strip():
        return False
    return version_key(latest) > version_key(current)


def _release_tag(release: dict) -> str:
    tag = re.sub(r"^v", "", str((release or {}).get("tag_name", "") or "").strip(),
                 flags=re.I).strip()
    return tag or str((release or {}).get("name", "") or "").strip()


def pick_latest_release(releases, include_prerelease: bool = False) -> dict:
    """从 GitHub 发布列表里挑出最新的一个版本。

    include_prerelease 关闭时只看正式版；打开时把 beta/rc 这类预发布版也算进来
    （同一版本号的预发布版永远小于正式版，由 version_key 保证）。
    返回 {"tag", "url", "name", "prerelease"}；没有可用发布时返回空字典。
    """
    items = releases if isinstance(releases, list) else ([releases] if releases else [])
    candidates = [r for r in items if isinstance(r, dict) and not r.get("draft")
                  and (include_prerelease or not r.get("prerelease"))]
    if not candidates:
        return {}
    best = max(candidates, key=lambda r: version_key(_release_tag(r)))
    return {"tag": _release_tag(best),
            "url": str(best.get("html_url", "") or ""),
            "name": str(best.get("name", "") or "")[:200],
            "prerelease": bool(best.get("prerelease"))}
