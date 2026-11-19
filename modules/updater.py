import asyncio
import hashlib
import uuid
import os
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import Tuple

import httpx

from .tls import verified_context

MAX_INSTALLER_BYTES = 2 * 1024 ** 3

APP_VERSION = "1.2.6.0"

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
    if not str(latest or "").strip():
        return False
    return version_key(latest) > version_key(current)


def is_prerelease_version(v) -> bool:
    return version_key(v)[1] == 0


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
            return {"name": name, "url": url, "size": _asset_size(item), "kind": "exe", "digest": str(item.get("digest") or "")}
        if not archive and low.endswith(ARCHIVE_SUFFIXES):
            archive = {"name": name, "url": url, "size": _asset_size(item),
                       "kind": "archive", "digest": str(item.get("digest") or "")}
    return archive


def extract_installer(archive_path, dest_path) -> dict:
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
        if picked.file_size < 1024 or picked.file_size > MAX_INSTALLER_BYTES:
            raise RuntimeError("压缩包里的安装包大小超限")
        tmp = dest_path.with_name(dest_path.name + "." + uuid.uuid4().hex + ".part")
        try:
            received = 0
            with zf.open(picked) as src, open(tmp, "wb") as dst:
                while True:
                    data = src.read(256 * 1024)
                    if not data:
                        break
                    received += len(data)
                    if received > picked.file_size or received > MAX_INSTALLER_BYTES:
                        raise RuntimeError("压缩包解压大小超限")
                    dst.write(data)
            if received != picked.file_size:
                raise RuntimeError("压缩包解压不完整")
            check_downloaded_head(tmp, "exe")
            os.replace(tmp, dest_path)
        finally:
            _remove(tmp)
    size = dest_path.stat().st_size
    return {"entry": picked.filename, "size": size}


def pick_latest_release(releases, include_prerelease: bool = False) -> dict:
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
            "body": str(best.get("body") or "")[:4000],
            "installer": pick_installer_asset(best)}


async def probe_candidate(client, url: str) -> tuple:
    started = time.time()
    async with client.stream("GET", url, headers={"Range": "bytes=0-0"}) as resp:
        if resp.status_code >= 400:
            raise RuntimeError(f"{url} 返回 HTTP {resp.status_code}")
    return url, time.time() - started


async def race_candidates(urls, *, timeout: float = 8.0, verify: bool = True) -> tuple:
    urls = [str(u).strip() for u in (urls or []) if str(u or "").strip()]
    if not urls:
        raise RuntimeError("没有可用的下载地址")
    errors = []
    if not verify:
        raise ValueError("自动更新不允许关闭证书校验")
    ctx = verified_context()
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
            await asyncio.gather(*pending, return_exceptions=True)
    raise errors[-1] if errors else RuntimeError("所有下载地址都不可用")


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


def trusted_digest(value):
    text = str(value or "").lower()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", text):
        raise ValueError("官方发布未提供 SHA-256 校验值，请手动更新")
    return text.split(":", 1)[1]


async def download_asset(url: str, dest, *, expected_size: int = 0, verify: bool = True,
                         expected_digest: str = "", timeout: float = 60.0,
                         chunk: int = 256 * 1024, on_progress=None) -> int:
    expected_hash = trusted_digest(expected_digest)
    total = int(expected_size or 0)
    if not verify or not 0 < total <= MAX_INSTALLER_BYTES:
        raise ValueError("安装包必须使用证书校验且具有合法的官方大小")
    dest = Path(dest)
    tmp = dest.with_name(dest.name + "." + uuid.uuid4().hex + ".part")
    received = 0
    digest = hashlib.sha256()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(15.0, timeout)),
                                     proxy=None, trust_env=False, follow_redirects=True,
                                     verify=verified_context()) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                header_size = resp.headers.get("content-length")
                if header_size and int(header_size) != total:
                    raise RuntimeError("下载大小与官方发布记录不一致")
                with open(tmp, "wb") as fh:
                    async for piece in resp.aiter_bytes(chunk):
                        received += len(piece)
                        if received > total:
                            raise RuntimeError("下载大小超出官方发布记录")
                        digest.update(piece)
                        fh.write(piece)
                        if on_progress is not None:
                            on_progress(received, total)
                    fh.flush()
                    os.fsync(fh.fileno())
        if received != total or digest.hexdigest() != expected_hash:
            raise RuntimeError("安装包完整性校验失败，已丢弃下载")
        os.replace(tmp, dest)
        return received
    finally:
        _remove(tmp)


def check_downloaded_head(path, kind: str = "exe") -> None:
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
