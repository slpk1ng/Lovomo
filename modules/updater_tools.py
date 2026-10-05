# -*- coding: utf-8 -*-
"""应用在线更新状态机与安装包准备（自 main.py 原样搬迁）。

下载/安装状态、就绪安装包检测、进度行刷新。WebUIServer 的在线更新方法群
与 main.py 的 __main__ 块（install_update_package）都引用这里的状态；
本模块严禁反向 import webui / main。
"""

import re
import sys
import threading
from pathlib import Path

from modules.app_paths import user_data_dir
from modules.log_console import (
    console_log_level, global_log_buffer, log_lock, log_prefix,
)

# ---------------------------------------------------------------------------
# 在线更新：下载安装包 / 安装 / 收尾
# ---------------------------------------------------------------------------
# 安装包落在 <用户数据目录>/update/Lovomo_Setup_<版本>.exe。用户在「下载已完成，
# 是否立即安装？」选「否」时它得留着（下次点「检查更新」还要接着用），所以只在
# 「已经装上或更旧」时才删；彻底清掉由卸载程序负责（安装脚本里有 [UninstallDelete]）。
_UPDATE_STATE = {
    "active": False, "phase": "idle", "version": "", "path": "", "source": "",
    "received": 0, "total": 0, "error": "", "started_at": 0.0, "finished": False,
}
_UPDATE_LOCK = threading.Lock()
# 进度日志固定替换同一行，免得日志面板被百分比刷满
_PROGRESS_LINE = {"index": -1, "text": "", "head": ""}
_APP_RELEASE_REPO = "slpk1ng/Lovomo"


def update_dir() -> Path:
    """在线更新的安装包落点（用户目录，卸载时由安装脚本清整目录）。"""
    return user_data_dir() / "update"


def parse_installer_version(filename) -> str:
    """从 Lovomo_Setup_1.2.3.0.exe 里取出 1.2.3.0；不是这个命名就返回空串。"""
    match = re.match(r"^Lovomo_Setup[_-]?v?(\d[\d.]*)$", Path(filename).stem, re.I)
    return match.group(1).rstrip(".") if match else ""


def find_ready_installer() -> dict:
    """已经下好、且比当前版本新的安装包（没有就返回空字典）。"""
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
    """删掉「已经装上或更旧」的安装包 —— 更新之后由新版本自己收尾。"""
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
    """进度日志的文案（同一行反复替换，所以信息要能一口气看完）。"""
    got = received / 1048576
    if total:
        return (f"[更新] 下载中 {received * 100 // total}%"
                f"（{got:.1f}/{total / 1048576:.1f} MB，来源 {source}）")
    return f"[更新] 下载中 {got:.1f} MB（来源 {source}）"


def log_progress(text: str) -> None:
    """原地刷新一条进度日志：日志面板里那一行数字在跳，而不是一屏一屏刷。

    只在「上一行还是我们自己写的那条」时才替换 —— 中间夹了别的日志（比如切换
    镜像的提示）就另起一行，否则进度会往回跳到旧位置上去。
    """
    with log_lock:
        index = _PROGRESS_LINE["index"]
        if index == len(global_log_buffer) - 1 and index >= 0 \
                and global_log_buffer[index] == _PROGRESS_LINE["head"] + _PROGRESS_LINE["text"]:
            global_log_buffer[index] = _PROGRESS_LINE["head"] + text
        else:
            # 时间戳只在起一行时取一次：原地刷新时它不该跟着跳
            head = log_prefix(console_log_level(text))
            global_log_buffer.append(head + text)
            _PROGRESS_LINE["index"] = len(global_log_buffer) - 1
            _PROGRESS_LINE["head"] = head
        _PROGRESS_LINE["text"] = text
    # 源码运行时还有真控制台：那边也用同一行滚动
    stream = getattr(sys.stdout, "original_stream", None)
    if stream is not None:
        try:
            stream.write("\r" + text)
            stream.flush()
        except Exception:
            pass


def is_own_release_asset(url: str) -> bool:
    """安装包必须来自本仓库的 Releases（GitHub 转发出来的域名也算）。"""
    head = str(url or "").lower()
    return (head.startswith(f"https://github.com/{_APP_RELEASE_REPO.lower()}/releases/download/")
            or head.startswith("https://objects.githubusercontent.com/")
            or head.startswith("https://github-releases.githubusercontent.com/"))
