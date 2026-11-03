import os
import shutil
import sys
import time
from pathlib import Path
from typing import Optional


def _harden_stdio():
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8",
                                       errors="replace"))
            except Exception:
                pass
            continue
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def _probe_writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".lovomo_write_{os.getpid()}"
        probe.write_text("1", encoding="utf-8")
    except Exception:
        return False
    try:
        probe.unlink()
    except Exception:
        try:
            time.sleep(0.05)
            probe.unlink()
        except Exception:
            pass
    return True


def user_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = Path(base) / "Lovomo"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return path


def runtime_path(filename: str, preferred_dir: Optional[Path] = None) -> Path:
    directory = (Path(preferred_dir) if preferred_dir else Path.cwd()).resolve()
    if _probe_writable(directory):
        return directory / filename
    return user_data_dir() / filename


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _remove_path(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    except Exception:
        pass


def _data_signature(path: Path) -> tuple:
    if path.is_file():
        try:
            return 1, path.stat().st_size
        except OSError:
            return 0, 0
    count = 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                count += 1
                total += item.stat().st_size
            except OSError:
                pass
    return count, total


def _migrate_user_data(legacy: Path, target: Path, label: str) -> Path:
    staging = target.with_name(target.name + ".migrating")
    try:
        _remove_path(staging)
        target.parent.mkdir(parents=True, exist_ok=True)
        if legacy.is_dir():
            shutil.copytree(legacy, staging)
        else:
            shutil.copy2(legacy, staging)
        if _data_signature(legacy) != _data_signature(staging):
            raise OSError("复制结果与源不一致")
        if target.exists():
            _remove_path(staging)
            return target
        os.replace(str(staging), str(target))
    except Exception as e:
        _remove_path(staging)
        print(f"⚠️ {label} 迁移到用户目录失败，继续使用原位置 {legacy}：{e}")
        return legacy
    _remove_path(legacy)
    if legacy.exists():
        print(f"{label} 已迁移到 {target}，旧位置未能清理（可手动删除）：{legacy}")
    else:
        print(f"已把 {label} 迁移到用户目录：{target}")
    return target


def adopt_user_data(name: str) -> Path:
    root = user_data_dir()
    if getattr(sys, "frozen", False):
        legacy = app_dir() / name
        target = root / name
        if legacy.exists() and not target.exists():
            return _migrate_user_data(legacy, target, name)
        return target
    return root / name


def get_resource_path(relative_path):
    if hasattr(sys, '_MEIPASS'):
        base_path = Path(sys._MEIPASS)
    else:
        base_path = Path(__file__).parent.parent
    return base_path / relative_path


def _resolve_data_dir(config) -> Path:
    configured = str(config.get("memory_data_path", "") or "").strip()
    if configured:
        return Path(configured).resolve()
    if getattr(sys, "frozen", False):
        return adopt_user_data("data")
    default_dir = Path("./data").resolve()
    if _probe_writable(default_dir.parent):
        return default_dir
    return adopt_user_data("data")
