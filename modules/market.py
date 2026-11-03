import posixpath
import re

SORT_KEYS = ("latest", "downloads", "favorites")

ARCHIVE_TMPL = "https://codeload.github.com/{repo}/zip/refs/heads/{branch}"
RAW_TMPL = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"

DEFAULT_CATEGORY = "其他"

_CATEGORY_BAD_RE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def clean_category(name) -> str:
    raw = _CATEGORY_BAD_RE.sub("", str(name or "")).strip().strip(".")
    if not raw or raw in (".", ".."):
        return DEFAULT_CATEGORY
    return raw[:24]


def plugin_category(manifest) -> str:
    data = manifest if isinstance(manifest, dict) else {}
    return clean_category(data.get("category"))


def plugin_folder(market_path, category, plugin_id) -> str:
    root = posixpath.dirname(str(market_path or "").strip().lstrip("/"))
    pid = str(plugin_id or "").strip()
    return "/".join(p for p in (root, clean_category(category), pid) if p)


REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


def parse_market_repos(text) -> list:
    out = []
    for line in str(text or "").splitlines():
        item = line.split("#", 1)[0].strip()
        if not item:
            continue
        if "://" in item:
            item = item.split("://", 1)[1]
        item = item.split("?", 1)[0].strip()
        if item.lower().startswith("www."):
            item = item[4:]
        if item.lower().startswith("github.com/"):
            item = item[len("github.com/"):]
        item = item.strip().strip("/")
        if item.lower().endswith(".git"):
            item = item[:-4]
        parts = [p for p in item.split("/") if p]
        item = "/".join(parts[:2])
        if REPO_SLUG_RE.match(item) and item not in out:
            out.append(item)
    return out


def matches_branch_prefix(branch_name: str, prefix: str) -> bool:
    name = str(branch_name or "").strip()
    if not name or name.startswith(".") or name.endswith("/"):
        return False
    head = str(prefix or "").strip()
    if not head:
        return True
    return name.lower().startswith(head.lower()) and len(name) > len(head)


def plugin_id_from_branch(branch_name: str, prefix: str, fallback: str = "") -> str:
    name = str(branch_name or "").strip()
    head = str(prefix or "").strip()
    tail = name[len(head):] if head and name.lower().startswith(head.lower()) else ""
    tail = tail.strip().lstrip("_-./ ").strip()
    return tail or str(fallback or "").strip() or name


def release_for_plugin(release: dict, pid: str) -> bool:
    pid = str(pid or "").strip().lower()
    tag = str((release or {}).get("tag_name") or "").strip().lower()
    if not pid or not tag:
        return False
    return (tag == pid or tag.startswith(pid + "-") or tag.startswith(pid + "_")
            or tag.startswith(pid + "v") or tag.startswith(pid + "/"))


def release_belongs_to(release: dict, branch: str, pid: str) -> bool:
    if not isinstance(release, dict):
        return False
    branch = str(branch or "").strip().lower()
    if not branch:
        return False
    commitish = str(release.get("target_commitish") or "").strip().lower()
    if commitish and commitish == branch:
        return True
    tag = str(release.get("tag_name") or "").strip().lower()
    if tag and (tag == branch or (tag.startswith(branch)
               and tag[len(branch):len(branch) + 1] in ("-", "_", "/", "v"))):
        return True
    return release_for_plugin(release, pid)


def release_downloads(release: dict) -> int:
    total = 0
    for asset in (release or {}).get("assets") or []:
        try:
            total += int(asset.get("download_count") or 0)
        except (TypeError, ValueError):
            continue
    return total


def release_favorites(release: dict, reactions: int = 0) -> int:
    """一个 Release 的收藏量（GitHub 点赞数）。"""
    try:
        return max(0, int(reactions or 0))
    except (TypeError, ValueError):
        return 0


def _accumulate_release(stats: dict, rel: dict) -> None:
    stats["releases"] += 1
    stats["downloads"] += release_downloads(rel)
    stats["favorites"] += release_favorites(rel, rel.get("_reactions") or 0)
    when = _release_time(rel)
    zip_asset = _zip_asset_url(rel)
    if when >= stats["released_at"]:
        stats["released_at"] = when
        stats["release_tag"] = str(rel.get("tag_name") or "")
        if zip_asset:
            stats["asset_url"] = zip_asset
    elif zip_asset and not stats["asset_url"]:
        stats["asset_url"] = zip_asset


def _empty_stats() -> dict:
    return {"downloads": 0, "favorites": 0, "release_tag": "",
            "asset_url": "", "released_at": 0.0, "releases": 0}


def summarize_releases(releases, branches: dict) -> dict:
    out = {}
    for branch, meta in (branches or {}).items():
        stats = _empty_stats()
        for rel in releases or []:
            if not isinstance(rel, dict) or rel.get("draft"):
                continue
            if not release_belongs_to(rel, branch, (meta or {}).get("id", "")):
                continue
            _accumulate_release(stats, rel)
        out[branch] = stats
    return out


def summarize_plugin_releases(releases, pid: str) -> dict:
    stats = _empty_stats()
    for rel in releases or []:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        if not release_for_plugin(rel, pid):
            continue
        _accumulate_release(stats, rel)
    return stats


def _zip_asset_url(release: dict) -> str:
    for asset in (release or {}).get("assets") or []:
        url = str((asset or {}).get("browser_download_url") or "").strip()
        if url.lower().split("?")[0].endswith(".zip"):
            return url
    return ""


def _release_time(release: dict) -> float:
    for key in ("published_at", "created_at"):
        raw = str((release or {}).get(key) or "").strip()
        if raw:
            return parse_iso_time(raw)
    return 0.0


def parse_iso_time(text: str) -> float:
    import datetime
    raw = str(text or "").strip()
    if not raw:
        return 0.0
    stamp = raw.replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(stamp)
    except ValueError:
        try:
            base = stamp.split(".")[0].split("+")[0]
            dt = datetime.datetime.strptime(base, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    try:
        return dt.timestamp()
    except Exception:
        return 0.0


def safe_logo_path(raw: str) -> str:
    rel = str(raw or "").strip().replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return ""
    name = parts[-1]
    if "." not in name or name.startswith("."):
        return ""
    return "/".join(parts)


def _normalize_tags(raw) -> list:
    if isinstance(raw, str):
        raw = re.split(r"[,，;；\s]+", raw)
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(t)[:24] for t in raw if str(t).strip()][:8]


def build_entry(branch: str, prefix: str, manifest: dict, raw_base: str,
                repo: str, stats: dict, commit_at: float = 0.0) -> dict:
    manifest = manifest if isinstance(manifest, dict) else {}
    branch = str(branch)
    repo = str(repo or "").strip()
    pid = str(manifest.get("id") or "").strip() or plugin_id_from_branch(
        branch, prefix, fallback=branch)
    stats = stats if isinstance(stats, dict) else {}
    logo = safe_logo_path(manifest.get("logo")) or "logo.png"
    homepage = str(manifest.get("homepage") or "").strip()
    return {
        "id": pid,
        "branch": branch,
        "source_repo": repo,
        "name": str(manifest.get("name") or pid)[:80],
        "version": str(manifest.get("version") or "")[:32],
        "author": str(manifest.get("author") or "")[:80],
        "github": str(manifest.get("github") or "")[:80],
        "description": str(manifest.get("description") or "")[:400],
        "type": str(manifest.get("type") or "python")[:16],
        "tags": _normalize_tags(manifest.get("tags")),
        "category": plugin_category(manifest),
        "homepage": homepage or f"https://github.com/{repo}/tree/{branch}",
        "page_url": f"https://github.com/{repo}/tree/{branch}",
        "download": str(stats.get("asset_url") or "") or ARCHIVE_TMPL.format(
            repo=repo, branch=branch),
        "download_kind": "asset" if stats.get("asset_url") else "branch_archive",
        "raw_base": raw_base,
        "logo": logo,
        "logo_url": raw_base + "/" + logo,
        "icon_url": raw_base + "/icon.png",
        "downloads": int(stats.get("downloads") or 0),
        "favorites": int(stats.get("favorites") or 0),
        "release_tag": str(stats.get("release_tag") or ""),
        "released_at": float(stats.get("released_at") or 0.0),
        "updated_at": float(commit_at or stats.get("released_at") or 0.0),
    }


def build_index_entry(manifest: dict, plugin_id: str, repo: str, branch: str,
                      folder: str, download: str, updated: str) -> dict:
    manifest = manifest if isinstance(manifest, dict) else {}
    repo = str(repo or "").strip()
    folder = str(folder or "").strip().strip("/")
    pid = str(plugin_id or "").strip() or str(manifest.get("id") or "").strip()
    base = RAW_TMPL.format(repo=repo, branch=branch, path=folder).rstrip("/")
    page = f"https://github.com/{repo}/tree/{branch}/{folder}"
    logo = safe_logo_path(manifest.get("logo")) or "logo.png"
    homepage = str(manifest.get("homepage") or "").strip()
    return {
        "id": pid,
        "name": str(manifest.get("name") or pid)[:80],
        "version": str(manifest.get("version") or "")[:32],
        "author": str(manifest.get("author") or "")[:80],
        "github": str(manifest.get("github") or "")[:80],
        "description": str(manifest.get("description") or "")[:400],
        "type": str(manifest.get("type") or "python")[:16],
        "tags": _normalize_tags(manifest.get("tags")),
        "category": plugin_category(manifest),
        "folder": folder,
        "homepage": homepage or page,
        "page_url": page,
        "source_repo": repo,
        "download": str(download or ""),
        "logo_url": base + "/" + logo,
        "icon_url": base + "/icon.png",
        "updated": str(updated or ""),
    }


def upsert_index_entry(index, entry) -> dict:
    data = dict(index) if isinstance(index, dict) else {}
    pid = str((entry or {}).get("id") or "").strip()
    items = [p for p in (data.get("plugins") or []) if isinstance(p, dict)]
    kept = [p for p in items if str(p.get("id") or "").strip() != pid]
    kept.append(dict(entry or {}))
    kept.sort(key=lambda p: str(p.get("id") or ""))
    data["plugins"] = kept
    try:
        data["version"] = max(2, int(data.get("version") or 0))
    except (TypeError, ValueError):
        data["version"] = 2
    data["updated"] = str((entry or {}).get("updated") or "")
    return data


def remove_index_entry(index, plugin_id) -> dict:
    data = dict(index) if isinstance(index, dict) else {}
    pid = str(plugin_id or "").strip()
    data["plugins"] = [p for p in (data.get("plugins") or [])
                       if isinstance(p, dict)
                       and str(p.get("id") or "").strip() != pid]
    try:
        data["version"] = max(2, int(data.get("version") or 0))
    except (TypeError, ValueError):
        data["version"] = 2
    return data


def sort_entries(entries, key: str = "latest") -> list:
    items = list(entries or [])
    key = str(key or "latest").strip().lower()
    if key == "downloads":
        return sorted(items, key=lambda e: (-int(e.get("downloads") or 0),
                                            str(e.get("name") or "")))
    if key == "favorites":
        return sorted(items, key=lambda e: (-int(e.get("favorites") or 0),
                                            str(e.get("name") or "")))
    return sorted(items, key=lambda e: (-float(e.get("updated_at") or 0),
                                        str(e.get("name") or "")))


def count_reactions(reactions) -> int:
    if isinstance(reactions, list):
        return len(reactions)
    if isinstance(reactions, dict):
        total = 0
        for value in reactions.values():
            if isinstance(value, int):
                total += value
        return total
    return 0
