# -*- coding: utf-8 -*-

import re
import json
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
    match = re.match(r"^Lovomo_Setup[_-]?v?(\d+(?:\.\d+)*(?:[-+][A-Za-z0-9.-]+)?)$", Path(filename).stem, re.I)
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
        if not verified_installer(path, check_hash=False):
            continue
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
    from urllib.parse import urlsplit
    parsed = urlsplit(str(url or ""))
    return (parsed.scheme == "https" and parsed.netloc.lower() == "github.com"
            and parsed.path.lower().startswith(f"/{_APP_RELEASE_REPO.lower()}/releases/download/")
            and not parsed.query and not parsed.fragment)


def record_verified_installer(path, version, source, digest):
    from .jsonio import save_json
    from .security import _encrypt_value
    from .updater import sha256_file, trusted_digest
    path = Path(path).resolve()
    if path.parent != update_dir().resolve() or not is_own_release_asset(source):
        raise ValueError("安装包路径或来源非法")
    trusted_digest(digest)
    record = {"path": path.name, "version": version, "source": source,
              "source_digest": digest, "sha256": sha256_file(path),
              "size": path.stat().st_size}
    save_json(path.with_suffix(".verified.json"), {"record": _encrypt_value(json.dumps(record))})


def verified_installer(path, *, check_hash=True):
    from .jsonio import load_json_ex
    from .security import _decrypt_value
    from .updater import sha256_file, trusted_digest
    path = Path(path).resolve()
    if path.parent != update_dir().resolve() or path.suffix.lower() != ".exe":
        return False
    try:
        raw, readable = load_json_ex(path.with_suffix(".verified.json"), {})
        if not readable or not str(raw.get("record", "")).startswith("enc3:"):
            return False
        record = json.loads(_decrypt_value(raw["record"]))
        trusted_digest(record["source_digest"])
        return (record["path"] == path.name and record["size"] == path.stat().st_size
                and record["version"] == parse_installer_version(path.name)
                and is_own_release_asset(record["source"])
                and (not check_hash or record["sha256"] == sha256_file(path)))
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False
