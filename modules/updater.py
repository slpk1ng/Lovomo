import asyncio
import os
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import Tuple

import httpx

from .tls import unverified_context, verified_context

APP_VERSION = "1.2.2.0"

# 发布里可能直接挂 .exe，也可能打成压缩包；压缩包的扩展名（下载后自动解压取 exe）
ARCHIVE_SUFFIXES = (".zip",)

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


def _asset_size(item: dict) -> int:
    try:
        return int((item or {}).get("size") or 0)
    except (TypeError, ValueError):
        return 0


def pick_installer_asset(release: dict) -> dict:
    """从一次发布里挑出安装包资源。

    优先直接给 .exe；只挂了压缩包（.zip）时就返回压缩包，下载后由
    extract_installer 自动解出里面的 exe —— 发布时打不打包都能更新。
    返回 {"name","url","size","kind"}，kind 为 "exe" 或 "archive"；没有可用资源时 {}。
    """
    assets = (release or {}).get("assets")
    if not isinstance(assets, list):
        return {}
    archive = {}
    for item in assets:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        url = str(item.get("browser_download_url") or "").strip()
        if not name or not url:
            continue
        low = name.lower()
        if low.endswith(".exe"):
            return {"name": name, "url": url, "size": _asset_size(item), "kind": "exe"}
        if not archive and low.endswith(ARCHIVE_SUFFIXES):
            archive = {"name": name, "url": url, "size": _asset_size(item),
                       "kind": "archive"}
    return archive


def extract_installer(archive_path, dest_path) -> dict:
    """把压缩包里的安装包解出来写进 dest_path，返回 {"entry","size"}。

    挑法：名字带 Lovomo 的优先，其次取体积最大的那个 .exe（压缩包里常同时放着
    说明文档与依赖，只看体积容易挑错，所以先按名字认）。解出来还要是 PE 文件
    （头两个字节 MZ）且不是几百字节的占位物。
    """
    archive_path = Path(archive_path)
    dest_path = Path(dest_path)
    if archive_path.suffix.lower() not in ARCHIVE_SUFFIXES:
        raise RuntimeError(f"不支持这种压缩包：{archive_path.name}")
    with zipfile.ZipFile(archive_path) as zf:
        members = [m for m in zf.infolist()
                   if not m.is_dir() and m.filename.lower().endswith(".exe")]
        if not members:
            raise RuntimeError(f"压缩包里没有 .exe：{archive_path.name}")

        def rank(member):
            base = Path(member.filename).name.lower()
            return (0 if base.startswith("lovomo") else 1, -member.file_size)

        picked = sorted(members, key=rank)[0]
        with zf.open(picked) as src, open(dest_path, "wb") as dst:
            shutil.copyfileobj(src, dst)
    size = dest_path.stat().st_size
    if size < 1024 or open(dest_path, "rb").read(2) != b"MZ":
        _remove(dest_path)
        raise RuntimeError("压缩包里的 .exe 不是可执行文件")
    return {"entry": picked.filename, "size": size}


def pick_latest_release(releases, include_prerelease: bool = False) -> dict:
    """从 GitHub 发布列表里挑出最新的一个版本。

    include_prerelease 关闭时只看正式版；打开时把 beta/rc 这类预发布版也算进来
    （同一版本号的预发布版永远小于正式版，由 version_key 保证）。
    返回 {"tag", "url", "name", "prerelease", "installer"}；没有可用发布时返回空字典。
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
            "prerelease": bool(best.get("prerelease")),
            "installer": pick_installer_asset(best)}


async def probe_candidate(client, url: str) -> tuple:
    """只取 1 字节（Range），以「拿到响应头」的耗时当测速结果。"""
    started = time.time()
    async with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as resp:
        if resp.status_code >= 400:
            raise RuntimeError(f"{url} 返回 HTTP {resp.status_code}")
    return url, time.time() - started


async def race_candidates(urls, *, timeout: float = 8.0, verify: bool = True) -> tuple:
    """并发试所有候选地址，**谁先响应就用谁**，不等其余跑完。

    返回 (胜出的地址, 耗时秒)。全都不通时抛最后一个异常 —— 一个慢镜像不该拖住
    整次更新，所以是「先到先得」而不是「全部测完再挑最快」。
    """
    urls = [str(u).strip() for u in (urls or []) if str(u or "").strip()]
    if not urls:
        raise RuntimeError("没有可用的下载地址")
    errors = []
    ctx = verified_context() if verify else unverified_context()
    async with httpx.AsyncClient(timeout=timeout, proxy=None, trust_env=False,
                                 follow_redirects=True, verify=ctx) as client:
        pending = {asyncio.create_task(probe_candidate(client, url)) for url in urls}
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.exception() is None:
                        return task.result()
                    errors.append(task.exception())
        finally:
            for task in pending:
                task.cancel()
    raise errors[-1] if errors else RuntimeError("所有下载地址都不可用")


async def download_asset(url: str, dest, *, expected_size: int = 0, verify: bool = True,
                         timeout: float = 60.0, chunk: int = 256 * 1024,
                         on_progress=None) -> int:
    """流式下载到 dest（先写 .part 再原子改名），返回拿到的字节数。

    这里只管「完整地拿下来」；拿到的东西是不是那个类型，交给
    check_downloaded_head 按资源类型判定（exe 看 MZ、压缩包看 PK）。
    """
    dest = Path(dest)
    tmp = dest.with_name(dest.name + ".part")
    ctx = verified_context() if verify else unverified_context()
    received = 0
    total = int(expected_size or 0)
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(15.0, timeout)),
                                 proxy=None, trust_env=False, follow_redirects=True,
                                 verify=ctx) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            try:
                total = int(resp.headers.get("content-length") or total)
            except (TypeError, ValueError):
                pass
            with open(tmp, "wb") as fh:
                async for piece in resp.aiter_bytes(chunk):
                    fh.write(piece)
                    received += len(piece)
                    if on_progress is not None:
                        on_progress(received, total)
    if not received:
        raise RuntimeError("下载到的内容是空的")
    if total and received != total:
        _remove(tmp)
        raise RuntimeError(f"下载不完整（{received}/{total} 字节），已丢弃")
    os.replace(tmp, dest)
    return received


def check_downloaded_head(path, kind: str = "exe") -> None:
    """校验下下来的是不是该有的类型 —— 镜像挂掉时经常回一个 200 的 HTML 错误页。"""
    magic, what = (b"PK", "压缩包") if kind == "archive" else (b"MZ", "可执行文件")
    path = Path(path)
    with open(path, "rb") as fh:
        head = fh.read(len(magic))
    if head != magic:
        _remove(path)
        raise RuntimeError(f"下载到的不是{what}（镜像可能返回了错误页）")


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
