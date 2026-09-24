"""插件市场的数据形状与汇总规则（纯函数，不联网不落盘）。

官方市场的条目来自市场仓库里的 `plugins/index.json`，插件源码放在
`plugins/<分类>/<插件id>/`；来源清单与第三方市场仍把「名字以配置前缀开头的分支」
当成插件，清单（`plugin.yaml`，兼容 `plugin.yml` / `plugin.json`）放分支
根目录，安装包优先用该分支 Release 里的 zip 资源，没有就退回分支归档。
下载量 = Release 资源下载数之和，收藏量 = Release 点赞数之和。

每条市场条目都带 `source_repo`（插件来自哪个仓库），安装时会拿它和插件包内
清单声明的归属比对，挡住"把别人的插件挂到自己仓库上"这种情况。
"""
import posixpath
import re

SORT_KEYS = ("latest", "downloads", "favorites")

# GitHub 的分支归档地址（与网页上「Download ZIP」同一个入口）
ARCHIVE_TMPL = "https://codeload.github.com/{repo}/zip/refs/heads/{branch}"
RAW_TMPL = "https://raw.githubusercontent.com/{repo}/{branch}/{path}"

# 插件在市场仓库里的分类目录：清单没写 category（或写得不能用）时归到这里
DEFAULT_CATEGORY = "其他"

# 分类名是目录名：路径分隔符、通配符、控制字符一律去掉
_CATEGORY_BAD_RE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def clean_category(name) -> str:
    """清洗清单里的插件分类名；洗不出东西时用默认分类。"""
    raw = _CATEGORY_BAD_RE.sub("", str(name or "")).strip().strip(".")
    if not raw or raw in (".", ".."):
        return DEFAULT_CATEGORY
    return raw[:24]


def plugin_category(manifest) -> str:
    """插件清单里声明的分类（`category`），缺省为默认分类。"""
    data = manifest if isinstance(manifest, dict) else {}
    return clean_category(data.get("category"))


def plugin_folder(market_path, category, plugin_id) -> str:
    """插件在市场仓库里的目录：索引旁边的 `<分类>/<插件id>`。"""
    root = posixpath.dirname(str(market_path or "").strip().lstrip("/"))
    pid = str(plugin_id or "").strip()
    return "/".join(p for p in (root, clean_category(category), pid) if p)


# 仓库标识：两段「用户名/仓库名」，允许点、下划线、短横线
REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


def parse_market_repos(text) -> list:
    """把「每行一个市场地址」的文本收敛成 owner/repo 列表（去重保序）。

    每行写 `用户名/仓库名` 或直接粘 GitHub 地址都行；空行、`#` 注释、
    以及识别不出仓库的行一律跳过，由调用方决定怎么提示。
    """
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
    """分支名是否符合插件前缀（前缀为空表示不筛选）。"""
    name = str(branch_name or "").strip()
    if not name or name.startswith(".") or name.endswith("/"):
        return False
    head = str(prefix or "").strip()
    if not head:
        return True
    return name.lower().startswith(head.lower()) and len(name) > len(head)


def plugin_id_from_branch(branch_name: str, prefix: str, fallback: str = "") -> str:
    """从分支名推出插件 id（去掉前缀与紧随的分隔符）。"""
    name = str(branch_name or "").strip()
    head = str(prefix or "").strip()
    tail = name[len(head):] if head and name.lower().startswith(head.lower()) else ""
    tail = tail.strip().lstrip("_-./ ").strip()
    return tail or str(fallback or "").strip() or name


def release_for_plugin(release: dict, pid: str) -> bool:
    """Release 是否属于这个插件（只看标签名，边界处必须是分隔符）。

    插件 id 互为前缀时（如 chat / chatgpt）不能按裸子串判断，否则会把别的
    插件的 Release 算到本插件头上、甚至指向别人的安装包。
    """
    pid = str(pid or "").strip().lower()
    tag = str((release or {}).get("tag_name") or "").strip().lower()
    if not pid or not tag:
        return False
    return (tag == pid or tag.startswith(pid + "-") or tag.startswith(pid + "_")
            or tag.startswith(pid + "v") or tag.startswith(pid + "/"))


def release_belongs_to(release: dict, branch: str, pid: str) -> bool:
    """判断 Release 是否属于这条分支（看 target_commitish 或 tag）。"""
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
    """一个 Release 里所有资源的下载次数之和。"""
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
    """把一条 Release 计入统计：下载量、收藏量、最新安装包地址。"""
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
    """按分支汇总下载量、收藏量、最新版本与安装包地址。

    releases   GitHub `/releases` 的返回
    branches   {branch_name: {"id": pid}}
    返回       {branch_name: {downloads, favorites, release_tag,
                             asset_url, released_at, releases}}
    """
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
    """按插件 id 汇总 Release 统计（索引条目的下载量/收藏量/安装包地址）。"""
    stats = _empty_stats()
    for rel in releases or []:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        if not release_for_plugin(rel, pid):
            continue
        _accumulate_release(stats, rel)
    return stats


def _zip_asset_url(release: dict) -> str:
    """Release 里第一个 zip 资源的下载地址。"""
    for asset in (release or {}).get("assets") or []:
        url = str((asset or {}).get("browser_download_url") or "").strip()
        if url.lower().split("?")[0].endswith(".zip"):
            return url
    return ""


def _release_time(release: dict) -> float:
    """Release 的时间戳（优先发布时间）。"""
    for key in ("published_at", "created_at"):
        raw = str((release or {}).get(key) or "").strip()
        if raw:
            return parse_iso_time(raw)
    return 0.0


def parse_iso_time(text: str) -> float:
    """把 GitHub 的 ISO 时间转成时间戳，解析不了返回 0.0。"""
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
    """清单里 `logo:` 声明的图标路径，收敛成仓库内的安全相对路径。"""
    rel = str(raw or "").strip().replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return ""
    name = parts[-1]
    if "." not in name or name.startswith("."):
        return ""
    return "/".join(parts)


def _normalize_tags(raw) -> list:
    """清单里的 `tags:` 可能是列表，也可能是逗号/空格分隔的一行字符串。"""
    if isinstance(raw, str):
        raw = re.split(r"[,，;；\s]+", raw)
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(t)[:24] for t in raw if str(t).strip()][:8]


def build_entry(branch: str, prefix: str, manifest: dict, raw_base: str,
                repo: str, stats: dict, commit_at: float = 0.0) -> dict:
    """把分支、清单与统计组装成前端要的一条市场条目。

    `source_repo` 是这条插件的来源仓库（安装时会拿它和插件自己声明的
    归属比对，防止有人把别人的插件挂到自己的仓库上）。
    """
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
    """把插件清单组装成 `plugins/index.json` 里的一条条目。

    `folder` 是插件在市场仓库里的目录（如 `plugins/换肤/meow-skin`），
    图标地址与主页都由它推出来；`category` 由清单的 `category` 决定，
    市场按它分类筛选；`download` 是 Release 附件的直链。
    """
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
    """把一条条目写进索引：同 id 覆盖，其余条目原样保留，按 id 排序。"""
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
    """从索引里删掉一条条目（其余条目原样保留），下架用。"""
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
    """按最新发布 / 最多下载 / 最多收藏排序（同值按名字稳定排序）。"""
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
    """数一下 reactions 接口返回的点赞条数。"""
    if isinstance(reactions, list):
        return len(reactions)
    if isinstance(reactions, dict):
        total = 0
        for value in reactions.values():
            if isinstance(value, int):
                total += value
        return total
    return 0
