# -*- coding: utf-8 -*-

import re
import sys
import threading
from pathlib import Path

from modules.app_paths import user_data_dir
from modules.log_console import (
    console_log_level, global_log_buffer, log_lock, log_prefix,
)

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
_UPDATE_STATE = {
    "active": False, "phase": "idle", "version": "", "path": "", "source": "",
    "received": 0, "total": 0, "error": "", "started_at": 0.0, "finished": False,
}
_UPDATE_LOCK = threading.Lock()
_PROGRESS_LINE = {"index": -1, "text": "", "head": ""}
_APP_RELEASE_REPO = "slpk1ng/Lovomo"


def update_dir() -> Path:
    return user_data_dir() / "update"


def parse_installer_version(filename) -> str:
    match = re.match(r"^Lovomo_Setup[_-]?v?(\d[\d.]*)$", Path(filename).stem, re.I)
    return match.group(1).rstrip(".") if match else ""


def find_ready_installer() -> dict:
    from modules.updater import APP_VERSION, is_newer
    best = {}
    try:
        files = sorted(update_dir().glob("*.exe"))
    except OSError:
        return {}
    for path in files:
        version = parse_installer_version(path.name)
        if not version or not is_newer(version, APP_VERSION):
            continue
        if best and not is_newer(version, best["version"]):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        best = {"version": version, "path": str(path), "size": size}
    return best


def cleanup_old_installers() -> int:
    from modules.updater import APP_VERSION, is_newer
    freed = 0
    try:
        files = (sorted(update_dir().glob("*.exe"))
                 + sorted(update_dir().glob("*.zip")))
    except OSError:
        return 0
    for path in files:
        version = parse_installer_version(path.name)
        if version and is_newer(version, APP_VERSION):
            continue  # 还没装的新版本，留着
        try:
            size = path.stat().st_size
            path.unlink()
            freed += size
            print(f"[更新] 已删除用过的安装包 {path.name}（{size / 1048576:.1f} MB）")
        except OSError as e:
            print(f"[更新] 清理安装包失败 {path.name}: {e}")
    return freed


def update_progress_line(received: int, total: int, source: str) -> str:
    got = received / 1048576
    if total:
        return (f"[更新] 下载中 {received * 100 // total}%"
                f"（{got:.1f}/{total / 1048576:.1f} MB，来源 {source}）")
    return f"[更新] 下载中 {got:.1f} MB（来源 {source}）"


def log_progress(text: str) -> None:
    with log_lock:
        index = _PROGRESS_LINE["index"]
        if index == len(global_log_buffer) - 1 and index >= 0 \
                and global_log_buffer[index] == _PROGRESS_LINE["head"] + _PROGRESS_LINE["text"]:
            global_log_buffer[index] = _PROGRESS_LINE["head"] + text
        else:
            head = log_prefix(console_log_level(text))
            global_log_buffer.append(head + text)
            _PROGRESS_LINE["index"] = len(global_log_buffer) - 1
            _PROGRESS_LINE["head"] = head
        _PROGRESS_LINE["text"] = text
    stream = getattr(sys.stdout, "original_stream", None)
    if stream is not None:
        try:
            stream.write("\r" + text)
            stream.flush()
        except Exception:
            pass


def is_own_release_asset(url: str) -> bool:
    head = str(url or "").lower()
    return (head.startswith(f"https://github.com/{_APP_RELEASE_REPO.lower()}/releases/download/")
            or head.startswith("https://objects.githubusercontent.com/")
            or head.startswith("https://github-releases.githubusercontent.com/"))
