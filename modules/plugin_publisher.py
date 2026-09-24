"""把本地插件源包推到插件市场仓库的默认分支上。

走 GitHub Git Data API：插件文件写到 `plugins/<分类>/<插件id>/` 下（分类来自
清单的 `category`），以默认分支 HEAD 的 tree 作 base_tree，仓库里其它内容原样
保留，同一提交里更新 `plugins/index.json` 里本插件那一条，再把默认分支指到这
个新提交。发布完给这个版本打一个不可变标签（`<插件id>-v<版本>`），并把插件目录
打成 zip 传成该版本的 Release 附件 —— 下载量与收藏量就是从 Release 上汇总来的。

排查约定：所有出错的调用都会打一行 `[插件发布]` 日志（动作、URL、状态码、
返回内容摘要），并把同一份上下文塞进 PublishError 的提示里 —— 调用方拿到的
永远是可读错误，不会是 KeyError 之类的裸异常。
"""
import base64
import io
import json
import re
import time
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

import httpx

from modules.market import (build_index_entry, plugin_category, plugin_folder,
                            remove_index_entry, upsert_index_entry)
from modules.plugins import ALLOWED_EXTS as ALLOWED, parse_manifest_text
from modules.tls import verified_context, is_cert_error


API_ROOT = "https://api.github.com"

# 插件清单：plugin.yaml 优先，plugin.json 兼容老包
MANIFEST_NAMES = ("plugin.yaml", "plugin.yml", "plugin.json")

SKIP_DIRS = {"data", "__pycache__", ".git"}
TIMEOUT = 30.0

# Git 引用名不接受空格、~ ^ : ? * [ \ 这类字符，版本号是用户自己写的，先收敛
_TAG_BAD_RE = re.compile(r"[^0-9A-Za-z._-]+")

CERT_HINT = ("证书校验失败：系统信任库里没有出网设备的根证书。"
             "若确认网络可信，可把该根证书导入系统证书库后再试。")

# 按状态码给出的排查提示，命中就给用户一句人话
_STATUS_HINTS = {
    401: "Token 无效或已过期，请在「插件」→「发布到市场」里重新保存",
    403: "权限不足，或触发了 GitHub 限流（未认证调用每小时只有 60 次）",
    404: "目标不存在，或当前 Token 没有访问权限",
    409: "仓库还是空的（没有任何提交），先在仓库里建一个初始提交",
    422: "请求内容被 GitHub 拒绝（同名标签或附件已存在 / SHA 不合法等）",
}


def list_publish_files(src_dir: Path) -> List[Tuple[str, bytes]]:
    """白名单扫描，返回 [(仓库内相对路径, 字节)]。

    与 pack_plugin.py 同源同一份白名单，避免打进非代码文件。
    """
    out = []
    for f in sorted(src_dir.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(src_dir)
        if any(p in SKIP_DIRS for p in rel.parts):
            continue
        if f.suffix.lower() not in ALLOWED:
            continue
        out.append((rel.as_posix(), f.read_bytes()))
    return out


def find_manifest(src_dir: Path) -> Optional[Path]:
    """在插件目录里找清单文件，plugin.yaml 优先。"""
    if src_dir is None or not src_dir.is_dir():
        return None
    for name in MANIFEST_NAMES:
        f = src_dir / name
        if f.is_file():
            return f
    return None


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Lovomo-Plugin-Publisher",
    }


class PublishError(Exception):
    pass


def _body_brief(resp, limit: int = 200) -> str:
    """返回内容摘要：压成单行并截断，避免把整页 HTML 灌进日志。"""
    try:
        text = resp.text
    except Exception:
        return ""
    return " ".join(str(text or "").split())[:limit]


def _dig(data, *keys):
    """按路径安全取值，任何一层缺失或类型不符都返回 None。"""
    cur = data
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _json_dict(resp) -> dict:
    """把响应体解析成字典；结构不是对象时给可读错误，不做裸下标。"""
    try:
        data = resp.json()
    except Exception as e:
        raise PublishError(
            f"GitHub 返回的不是合法 JSON（HTTP {resp.status_code}）："
            f"{_body_brief(resp)}") from e
    if not isinstance(data, dict):
        raise PublishError(
            f"GitHub 返回的结构不是对象而是 {type(data).__name__}"
            f"（HTTP {resp.status_code}）：{_body_brief(resp)}")
    return data


def _fail(action: str, url: str, resp, message: str) -> PublishError:
    """统一的失败出口：日志留上下文，错误给人话。"""
    brief = _body_brief(resp)
    print(f"[插件发布] {action}失败：HTTP {resp.status_code} {url}"
          + (f" → {brief}" if brief else ""))
    return PublishError(message)


def _api_error(action: str, url: str, resp) -> PublishError:
    """把一次失败的调用转成可读错误，并按状态码补一句排查提示。"""
    hint = _STATUS_HINTS.get(resp.status_code, "")
    return _fail(action, url, resp,
                 f"{action}失败：HTTP {resp.status_code}"
                 + (f"（{hint}）" if hint else ""))


def _missing_field(action: str, url: str, resp, field: str) -> PublishError:
    """响应缺关键字段时统一出口：日志留上下文，错误给人话。"""
    print(f"[插件发布] {action}：响应缺少 {field}（HTTP {resp.status_code} {url}）"
          f" → {_body_brief(resp)}")
    return PublishError(f"{action}：GitHub 的响应里没有 {field}，无法继续")


async def _request(token: str, method: str, url: str, json_body=None, params=None,
                   content: bytes = None, content_type: str = ""):
    headers = _headers(token)
    if content_type:
        headers["Content-Type"] = content_type
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, proxy=None, trust_env=False,
                                     verify=verified_context()) as client:
            r = await client.request(method, url, headers=headers,
                                     json=json_body, params=params, content=content)
    except httpx.RequestError as e:
        # PAT 就在这个请求头上，宁可报错也不降级成不校验
        hint = CERT_HINT if is_cert_error(e) else ""
        print(f"[插件发布] 网络错误：{method} {url} → {type(e).__name__}: {e}")
        raise PublishError(f"网络错误：{type(e).__name__}: {e}{hint}") from e
    return r


async def verify_token(token: str) -> dict:
    """校验 PAT 并返回用户信息（login / name）。"""
    url = f"{API_ROOT}/user"
    r = await _request(token, "GET", url)
    if r.status_code == 401:
        raise _fail("校验 Token", url, r, "Token 无效或已过期")
    if r.status_code != 200:
        raise _api_error("校验 Token", url, r)
    data = _json_dict(r)
    login = str(data.get("login") or "").strip()
    if not login:
        raise _missing_field("校验 Token", url, r, "login")
    return {"login": login, "name": str(data.get("name") or "").strip()}


async def fetch_default(token: str, repo: str) -> Tuple[str, str]:
    """返回 (默认分支名, 该分支 HEAD 的 commit SHA)。

    GitHub 的仓库对象本身**没有** `sha` 字段 —— `GET /repos/{repo}` 只给
    `default_branch` 这类元信息，HEAD 提交要另外查一次分支引用。所以这里
    分两步走，任何一步拿不到关键字段都转成可读错误而不是 KeyError。
    """
    url = f"{API_ROOT}/repos/{repo}"
    r = await _request(token, "GET", url)
    if r.status_code == 404:
        raise _fail("查询仓库", url, r,
                    f"仓库 {repo} 不存在或 Token 没有访问权限")
    if r.status_code != 200:
        raise _api_error("查询仓库", url, r)
    data = _json_dict(r)
    branch = str(data.get("default_branch") or "").strip() or "main"

    ref_url = f"{API_ROOT}/repos/{repo}/git/ref/heads/{branch}"
    r = await _request(token, "GET", ref_url)
    if r.status_code == 404:
        raise _fail(f"查询默认分支 {branch}", ref_url, r,
                    f"仓库 {repo} 的默认分支 {branch} 不存在"
                    "（空仓库请先建一个初始提交）")
    if r.status_code != 200:
        raise _api_error(f"查询默认分支 {branch}", ref_url, r)
    sha = _dig(_json_dict(r), "object", "sha")
    if not isinstance(sha, str) or not sha:
        raise _missing_field(f"查询默认分支 {branch}", ref_url, r, "object.sha")
    return branch, sha


async def commit_tree(token: str, repo: str, commit_sha: str) -> str:
    """取一个提交的 tree SHA。"""
    url = f"{API_ROOT}/repos/{repo}/git/commits/{commit_sha}"
    r = await _request(token, "GET", url)
    if r.status_code != 200:
        raise _api_error("查询提交", url, r)
    sha = _dig(_json_dict(r), "tree", "sha")
    if not isinstance(sha, str) or not sha:
        raise _missing_field("查询提交", url, r, "tree.sha")
    return sha


async def tree_blob_paths(token: str, repo: str, tree_sha: str) -> Tuple[list, bool]:
    """递归列出一棵树里的全部文件路径，返回 (路径列表, 是否被截断)。

    超大仓库的递归结果会被 GitHub 截断；截断时调用方不能拿它算
    "哪些文件已经不存在"，否则会误删仓库里的文件。
    """
    url = f"{API_ROOT}/repos/{repo}/git/trees/{tree_sha}"
    r = await _request(token, "GET", url, params={"recursive": "1"})
    if r.status_code != 200:
        raise _api_error("列出仓库文件", url, r)
    data = _json_dict(r)
    entries = data.get("tree")
    if not isinstance(entries, list):
        raise _missing_field("列出仓库文件", url, r, "tree")
    paths = [str(e.get("path")) for e in entries
             if isinstance(e, dict) and e.get("type") == "blob" and e.get("path")]
    return paths, bool(data.get("truncated"))


async def read_file_text(token: str, repo: str, ref: str, path: str) -> str:
    """读仓库里一个文本文件的内容，文件不存在时返回空串。"""
    url = f"{API_ROOT}/repos/{repo}/contents/{path}"
    r = await _request(token, "GET", url, params={"ref": ref})
    if r.status_code == 404:
        return ""
    if r.status_code != 200:
        raise _api_error(f"读取 {path}", url, r)
    content = _dig(_json_dict(r), "content")
    if not isinstance(content, str):
        raise _missing_field(f"读取 {path}", url, r, "content")
    try:
        return base64.b64decode(content).decode("utf-8", "replace")
    except Exception as e:
        raise PublishError(
            f"读取 {path}：内容不是合法的 base64（{type(e).__name__}）") from e


async def commit_files(token: str, repo: str, files: List[Tuple[str, bytes]],
                       message: str, parents: List[str],
                       base_tree: str = "", deleted: List[str] = None) -> Tuple[str, list]:
    """把一组文件建成一个提交，返回 (提交 SHA, [(路径, blob SHA)])。

    `base_tree` 给定时新树挂在它下面，仓库里其它内容原样保留；留空则
    是不挂 base_tree 的空树（提交里只有这些文件）。`deleted` 里的路径
    从新树里删掉（GitHub 用 sha 为 null 表示删除）。
    """
    entries = []
    for path, content in files:
        url = f"{API_ROOT}/repos/{repo}/git/blobs"
        r = await _request(token, "POST", url,
                           json_body={"content": base64.b64encode(content).decode(),
                                      "encoding": "base64"})
        if r.status_code not in (200, 201):
            raise _api_error(f"上传 {path}", url, r)
        sha = _dig(_json_dict(r), "sha")
        if not isinstance(sha, str) or not sha:
            raise _missing_field(f"上传 {path}", url, r, "sha")
        entries.append((path, sha))

    tree = [{"path": p, "mode": "100644", "type": "blob", "sha": s}
            for p, s in entries]
    tree += [{"path": p, "mode": "100644", "type": "blob", "sha": None}
             for p in (deleted or [])]
    body = {"tree": tree}
    if base_tree:
        body["base_tree"] = base_tree
    url = f"{API_ROOT}/repos/{repo}/git/trees"
    r = await _request(token, "POST", url, json_body=body)
    if r.status_code not in (200, 201):
        raise _api_error("创建目录树", url, r)
    tree_sha = _dig(_json_dict(r), "sha")
    if not isinstance(tree_sha, str) or not tree_sha:
        raise _missing_field("创建目录树", url, r, "sha")

    url = f"{API_ROOT}/repos/{repo}/git/commits"
    body = {"message": message, "tree": tree_sha}
    if parents:
        body["parents"] = parents
    r = await _request(token, "POST", url, json_body=body)
    if r.status_code not in (200, 201):
        raise _api_error("创建提交", url, r)
    commit = _dig(_json_dict(r), "sha")
    if not isinstance(commit, str) or not commit:
        raise _missing_field("创建提交", url, r, "sha")
    return commit, entries


async def update_branch(token: str, repo: str, branch: str, sha: str) -> None:
    """把分支指到提交上（不加 force：默认分支只能快进，不能覆盖历史）。"""
    url = f"{API_ROOT}/repos/{repo}/git/refs/heads/{branch}"
    r = await _request(token, "PATCH", url, json_body={"sha": sha, "force": False})
    if r.status_code != 200:
        raise _api_error(f"更新分支 {branch}", url, r)


def safe_version(version: str) -> str:
    """版本号里 Git 与文件名不接受的字符换成 -，收敛不出东西时返回空串。"""
    raw = str(version or "").strip()
    if not raw:
        return ""
    return _TAG_BAD_RE.sub("-", raw).replace("..", ".").strip(".-")


def version_tag(name: str, version: str) -> str:
    """版本标签名 `<名字>-v<版本>`；空版本返回空串。"""
    safe = safe_version(version)
    return f"{name}-v{safe}" if safe else ""


def parse_version_tag(tag: str, prefix: str = "") -> Tuple[str, str]:
    """`version_tag()` 的反解：把标签名拆回 (插件 id, 版本)。

    不是 `<前缀>_<id>-v<版本>` 这个形状（比如程序自己的版本标签）时返回
    ("", "")。插件 id 与版本号里都可能有 `-`，所以从**最后一个** `-v` 切开。
    """
    name = str(tag or "").strip()
    head = str(prefix or "").strip()
    if head and name.lower().startswith(head.lower()):
        name = name[len(head):]
    name = name.strip().lstrip("_-./ ").strip()
    if not name:
        return "", ""
    body, sep, version = name.rpartition("-v")
    if not sep or not body or not version:
        return "", ""
    return body, version


async def tag_exists(token: str, repo: str, tag: str) -> bool:
    """标签是否已存在。"""
    url = f"{API_ROOT}/repos/{repo}/git/ref/tags/{tag}"
    r = await _request(token, "GET", url)
    if r.status_code == 404:
        return False
    if r.status_code != 200:
        raise _api_error(f"查询标签 {tag}", url, r)
    return True


async def ensure_version_tag(token: str, repo: str, tag: str, sha: str) -> bool:
    """保证版本标签存在，返回本次是否新建。

    已存在的标签不动：同一个版本号永远指向它第一次发布时的提交，
    标签留档的仍是那一刻的内容。
    """
    if not tag or await tag_exists(token, repo, tag):
        return False
    url = f"{API_ROOT}/repos/{repo}/git/refs"
    r = await _request(token, "POST", url,
                       json_body={"ref": f"refs/tags/{tag}", "sha": sha})
    if r.status_code not in (200, 201):
        raise _api_error(f"创建标签 {tag}", url, r)
    return True


async def find_release_by_tag(token: str, repo: str, tag: str) -> Optional[dict]:
    """按标签查 Release，没有返回 None。"""
    url = f"{API_ROOT}/repos/{repo}/releases/tags/{tag}"
    r = await _request(token, "GET", url)
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise _api_error(f"查询 Release {tag}", url, r)
    return _json_dict(r)


async def create_release(token: str, repo: str, tag: str, name: str,
                         body: str = "") -> dict:
    """按标签建一个 Release。"""
    url = f"{API_ROOT}/repos/{repo}/releases"
    r = await _request(token, "POST", url, json_body={
        "tag_name": tag, "name": name or tag, "body": body, "draft": False})
    if r.status_code not in (200, 201):
        raise _api_error("创建 Release", url, r)
    return _json_dict(r)


async def upload_release_asset(token: str, upload_url: str, name: str,
                               data: bytes) -> dict:
    """把 zip 传成 Release 附件。"""
    # 上传地址是模板形式（尾部带 {?name,label}），要先把模板尾巴切掉
    url = str(upload_url or "").split("{", 1)[0]
    if not url:
        raise PublishError("创建 Release：GitHub 的响应里没有 upload_url，无法上传安装包")
    r = await _request(token, "POST", url, params={"name": name},
                       content=data, content_type="application/zip")
    if r.status_code not in (200, 201):
        raise _api_error(f"上传安装包 {name}", url, r)
    return _json_dict(r)


def zip_bytes(files: List[Tuple[str, bytes]]) -> bytes:
    """把待发布文件打成一个 zip（文件放在包根目录，与安装时的解压口径一致）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files:
            zf.writestr(path, content)
    return buf.getvalue()


async def ensure_release(token: str, repo: str, tag: str, title: str,
                         body: str, zip_name: str, data: bytes) -> str:
    """保证该版本有一个带 zip 附件的 Release，成功返回标签名。

    已发过的版本原样留着 —— 同一个版本号对应的内容以第一次发布为准。
    """
    if await find_release_by_tag(token, repo, tag) is not None:
        return tag
    try:
        rel = await create_release(token, repo, tag, title, body)
        await upload_release_asset(token, str(rel.get("upload_url") or ""),
                                   zip_name, data)
    except PublishError as e:
        # 文件夹与索引已经推上去了，Release 只是安装包与统计的来源，
        # 没建上不该把整次发布判成失败，但必须留痕
        print(f"[插件发布] Release 没建上：{e}")
        return ""
    return tag


async def list_version_tags(token: str, repo: str, plugin_id: str) -> list:
    """列出某个插件的版本标签（`<插件id>-v<版本>`）。"""
    url = f"{API_ROOT}/repos/{repo}/git/matching-refs/tags/{plugin_id}-v"
    r = await _request(token, "GET", url)
    if r.status_code == 404:
        return []
    if r.status_code != 200:
        raise _api_error(f"查询 {plugin_id} 的版本标签", url, r)
    try:
        data = r.json()
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [str(item.get("ref") or "").rsplit("/", 1)[-1]
            for item in data if isinstance(item, dict)]


async def delete_ref(token: str, repo: str, ref: str) -> None:
    """删掉一个引用（标签）；已经不存在时当成功。"""
    url = f"{API_ROOT}/repos/{repo}/git/refs/{ref}"
    r = await _request(token, "DELETE", url)
    if r.status_code not in (200, 204, 404):
        raise _api_error(f"删除标签 {ref}", url, r)


async def delete_release(token: str, repo: str, release_id) -> None:
    """删掉一个 Release；已经不存在时当成功。"""
    url = f"{API_ROOT}/repos/{repo}/releases/{release_id}"
    r = await _request(token, "DELETE", url)
    if r.status_code not in (200, 204, 404):
        raise _api_error("删除 Release", url, r)


async def unpublish_plugin(token: str, repo: str, plugin_id: str,
                           market_path: str = "plugins/index.json") -> dict:
    """把插件从市场下架：删索引条目、仓库里的插件目录，以及版本标签与 Release。

    标签与 Release 是「是否已上架」的另一半判据，留着会让插件一直显示
    「当前版本已发布过」而发不出去，所以一并撤掉。
    """
    index_path = str(market_path or "").strip().lstrip("/") or "plugins/index.json"
    default_branch, head = await fetch_default(token, repo)
    base_tree = await commit_tree(token, repo, head)
    index_text = await read_file_text(token, repo, default_branch, index_path)
    index_data = {}
    if index_text.strip():
        try:
            index_data = json.loads(index_text)
        except Exception as e:
            raise PublishError(f"{index_path} 不是合法 JSON（{type(e).__name__}），"
                               "先修好市场索引再下架") from e
    entry = next((p for p in (index_data.get("plugins") or [])
                  if isinstance(p, dict)
                  and str(p.get("id") or "").strip() == plugin_id), None)
    if entry is None:
        raise PublishError(f"市场索引里没有 {plugin_id}，它已经下架了")

    folder = str(entry.get("folder") or "").strip().strip("/") \
        or plugin_folder(index_path, entry.get("category"), plugin_id)
    existing, truncated = await tree_blob_paths(token, repo, base_tree)
    if truncated:
        print(f"[插件下架] 仓库文件过多，GitHub 截断了目录树：本次跳过清理 {folder}/")
        removed = []
    else:
        removed = sorted(p for p in existing if p.startswith(folder + "/"))

    index_bytes = (json.dumps(remove_index_entry(index_data, plugin_id),
                              ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    commit, entries = await commit_files(
        token, repo, [(index_path, index_bytes)], f"Unpublish {plugin_id}",
        [head], base_tree=base_tree, deleted=removed)
    await update_branch(token, repo, default_branch, commit)

    deleted_tags = []
    try:
        tags = await list_version_tags(token, repo, plugin_id)
    except PublishError as e:
        tags = []
        print(f"[插件下架] 版本标签没读到：{e}")
    for tag in tags:
        try:
            release = await find_release_by_tag(token, repo, tag)
            if release is not None and release.get("id") is not None:
                await delete_release(token, repo, release["id"])
            await delete_ref(token, repo, f"tags/{tag}")
            deleted_tags.append(tag)
        except PublishError as e:
            print(f"[插件下架] 标签 {tag} 没删掉：{e}")
    return {
        "id": plugin_id,
        "folder": folder,
        "removed": removed,
        "tags": deleted_tags,
        "index": index_path,
        "default_branch": default_branch,
        "commit": commit,
        "files": [{"path": p, "sha": s} for p, s in entries],
    }


async def publish_plugin(token: str, repo: str, plugin_id: str,
                         sources_dir: Path,
                         message: Optional[str] = None,
                         market_path: str = "plugins/index.json") -> dict:
    """主流程：把插件写进市场仓库的 `plugins/<分类>/<插件id>/`，并更新索引与 Release。"""
    src = sources_dir / plugin_id
    if not src.is_dir():
        raise PublishError(f"插件源码目录不存在：{src}")
    manifest = find_manifest(src)
    if manifest is None:
        raise PublishError(f"插件缺少清单文件：{plugin_id}（需要 "
                           + " / ".join(MANIFEST_NAMES) + "）")
    files = list_publish_files(src)
    if not files:
        raise PublishError("插件没有任何白名单内的文件可发布")
    # 清单已经在待发布文件里，直接就地解析，不再读一次盘
    manifest_data = parse_manifest_text(
        dict(files).get(manifest.name, b"").decode("utf-8", "replace"), manifest.name)

    index_path = str(market_path or "").strip().lstrip("/") or "plugins/index.json"
    folder = plugin_folder(index_path, plugin_category(manifest_data), plugin_id)
    published = [(f"{folder}/{rel}", content) for rel, content in files]

    default_branch, head = await fetch_default(token, repo)
    base_tree = await commit_tree(token, repo, head)
    existing, truncated = await tree_blob_paths(token, repo, base_tree)
    if truncated:
        print(f"[插件发布] 仓库文件过多，GitHub 截断了目录树："
              f"本次跳过清理 {folder}/ 下已不存在的文件")

    index_text = await read_file_text(token, repo, default_branch, index_path)
    index_data = {}
    if index_text.strip():
        try:
            index_data = json.loads(index_text)
        except Exception as e:
            print(f"[插件发布] {index_path} 不是合法 JSON（{type(e).__name__}）："
                  f"本次按空索引重写")
            index_data = {}

    # 改过分类（或老版本把插件直接放在 plugins/<插件id>/）时旧目录要一并删掉，
    # 否则市场里会同时留下两份，用户分不清该装哪个
    old_folders = []
    for item in (index_data.get("plugins") or []):
        if not isinstance(item, dict) or str(item.get("id") or "").strip() != plugin_id:
            continue
        old = str(item.get("folder") or "").strip().strip("/")
        if old and old != folder:
            old_folders.append(old)

    keep = {p for p, _ in published}
    prefixes = tuple(f"{f}/" for f in [folder] + old_folders)
    stale = [] if truncated else sorted(
        p for p in existing if p.startswith(prefixes) and p not in keep)

    version = str(manifest_data.get("version") or "").strip()
    safe_ver = safe_version(version)
    tag = version_tag(plugin_id, version)
    zip_name = f"{plugin_id}-{safe_ver}.zip" if safe_ver else f"{plugin_id}.zip"
    download = (f"https://github.com/{repo}/releases/download/{tag}/{zip_name}"
                if tag else "")
    entry = build_index_entry(manifest_data, plugin_id, repo, default_branch,
                              folder, download, time.strftime("%Y-%m-%d"))
    index_bytes = (json.dumps(upsert_index_entry(index_data, entry),
                              ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    msg = message or f"Publish {plugin_id}"
    commit, entries = await commit_files(
        token, repo, published + [(index_path, index_bytes)], msg, [head],
        base_tree=base_tree, deleted=stale)
    await update_branch(token, repo, default_branch, commit)

    tagged = False
    if tag:
        try:
            tagged = await ensure_version_tag(token, repo, tag, commit)
        except PublishError as e:
            print(f"[插件发布] 版本标签没建上：{e}")
    release = ""
    if tag:
        release = await ensure_release(
            token, repo, tag,
            f"{manifest_data.get('name') or plugin_id} {safe_ver}".strip(),
            str(manifest_data.get("description") or ""),
            zip_name, zip_bytes(files))
    return {
        "folder": folder,
        "index": index_path,
        "entry": entry,
        "default_branch": default_branch,
        "manifest": manifest.name,
        "commit": commit,
        "tag": tag,
        "tagged": tagged,
        "release": release,
        "zip": zip_name if tag else "",
        "files": [{"path": p, "sha": s} for p, s in entries],
        "url": f"https://github.com/{repo}/tree/{default_branch}/{folder}",
        "raw_url": f"https://raw.githubusercontent.com/{repo}/{default_branch}/"
                   f"{folder}/{manifest.name}",
    }
