"""把本地插件源包推到 GitHub 仓库的 lovomo_plugin_<id> 分支上。

走 GitHub Git Data API：blob → 不挂 base_tree 的 tree → 不接 parents 的
commit → 指向该提交的分支引用。分支里只有插件文件，不会继承默认分支的内容；
分支已存在时以新提交强制覆盖。

排查约定：所有出错的调用都会打一行 `[插件发布]` 日志（动作、URL、状态码、
返回内容摘要），并把同一份上下文塞进 PublishError 的提示里 —— 调用方拿到的
永远是可读错误，不会是 KeyError 之类的裸异常。
"""
import base64
from pathlib import Path
from typing import List, Optional, Tuple

import httpx

from modules.plugins import ALLOWED_EXTS as ALLOWED
from modules.tls import verified_context, is_cert_error


API_ROOT = "https://api.github.com"

# 插件清单：plugin.yaml 优先，plugin.json 兼容老包
MANIFEST_NAMES = ("plugin.yaml", "plugin.yml", "plugin.json")

SKIP_DIRS = {"data", "__pycache__", ".git"}
TIMEOUT = 30.0

CERT_HINT = ("证书校验失败：系统信任库里没有出网设备的根证书。"
             "若确认网络可信，可把该根证书导入系统证书库后再试。")

# 按状态码给出的排查提示，命中就给用户一句人话
_STATUS_HINTS = {
    401: "Token 无效或已过期，请在「插件」→「发布到分支」里重新保存",
    403: "权限不足，或触发了 GitHub 限流（未认证调用每小时只有 60 次）",
    404: "目标不存在，或当前 Token 没有访问权限",
    409: "仓库还是空的（没有任何提交），先在仓库里建一个初始提交",
    422: "请求内容被 GitHub 拒绝（分支已存在 / SHA 不合法等）",
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


async def _request(token: str, method: str, url: str, json_body=None, params=None):
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, proxy=None, trust_env=False,
                                     verify=verified_context()) as client:
            r = await client.request(method, url, headers=_headers(token),
                                     json=json_body, params=params)
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


async def branch_head(token: str, repo: str, branch: str) -> str:
    """取分支当前的 HEAD，分支不存在时返回空串。"""
    url = f"{API_ROOT}/repos/{repo}/git/ref/heads/{branch}"
    r = await _request(token, "GET", url)
    if r.status_code == 404:
        return ""
    if r.status_code != 200:
        raise _api_error(f"查询分支 {branch}", url, r)
    sha = _dig(_json_dict(r), "object", "sha")
    if not isinstance(sha, str) or not sha:
        raise _missing_field(f"查询分支 {branch}", url, r, "object.sha")
    return sha


async def commit_files(token: str, repo: str, files: List[Tuple[str, bytes]],
                       message: str, parents: List[str]) -> Tuple[str, list]:
    """把一组文件建成一个提交，返回 (提交 SHA, [(路径, blob SHA)])。

    树是不挂 `base_tree` 新建的，所以提交里**只有这些文件**；
    `parents` 留空就是根提交 —— 插件分支必须这样建，否则会把
    默认分支的整个仓库内容一起继承过去。
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

    url = f"{API_ROOT}/repos/{repo}/git/trees"
    r = await _request(token, "POST", url, json_body={
        "tree": [{"path": p, "mode": "100644", "type": "blob", "sha": s}
                 for p, s in entries]})
    if r.status_code not in (200, 201):
        raise _api_error("创建目录树", url, r)
    tree = _dig(_json_dict(r), "sha")
    if not isinstance(tree, str) or not tree:
        raise _missing_field("创建目录树", url, r, "sha")

    url = f"{API_ROOT}/repos/{repo}/git/commits"
    body = {"message": message, "tree": tree}
    if parents:
        body["parents"] = parents
    r = await _request(token, "POST", url, json_body=body)
    if r.status_code not in (200, 201):
        raise _api_error("创建提交", url, r)
    commit = _dig(_json_dict(r), "sha")
    if not isinstance(commit, str) or not commit:
        raise _missing_field("创建提交", url, r, "sha")
    return commit, entries


async def point_branch(token: str, repo: str, branch: str, sha: str,
                       exists: bool) -> None:
    """把分支指到提交上；已存在的分支强制覆盖，保证内容以本次发布为准。"""
    if exists:
        url = f"{API_ROOT}/repos/{repo}/git/refs/heads/{branch}"
        r = await _request(token, "PATCH", url, json_body={"sha": sha, "force": True})
        if r.status_code != 200:
            raise _api_error(f"更新分支 {branch}", url, r)
        return
    url = f"{API_ROOT}/repos/{repo}/git/refs"
    r = await _request(token, "POST", url,
                       json_body={"ref": f"refs/heads/{branch}", "sha": sha})
    if r.status_code not in (200, 201):
        raise _api_error(f"创建分支 {branch}", url, r)


async def publish_plugin(token: str, repo: str, plugin_id: str,
                         sources_dir: Path,
                         message: Optional[str] = None,
                         branch_prefix: str = "lovomo_plugin_") -> dict:
    """主流程：把插件目录推成一条独立分支（分支里只有插件文件）。"""
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
    default_branch, _ = await fetch_default(token, repo)
    branch_name = branch_prefix + plugin_id
    head = await branch_head(token, repo, branch_name)
    msg = message or f"Publish {plugin_id}"
    commit, entries = await commit_files(token, repo, files, msg,
                                         [head] if head else [])
    await point_branch(token, repo, branch_name, commit, bool(head))
    return {
        "branch": branch_name,
        "default_branch": default_branch,
        "manifest": manifest.name,
        "commit": commit,
        "files": [{"path": p, "sha": s} for p, s in entries],
        "url": f"https://github.com/{repo}/tree/{branch_name}",
        "raw_url": f"https://raw.githubusercontent.com/{repo}/{branch_name}/"
                   f"{manifest.name}",
    }
