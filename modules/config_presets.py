import re
import time
from pathlib import Path
from typing import List

from .jsonio import load_json_ex, save_json

PRESET_DIR_NAME = "config_presets"
DEFAULT_PROFILE = "default"
MAX_NAME_CHARS = 40

_ILLEGAL_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

_LOADERS = {}


class ConfigPresetError(Exception):
    pass


def preset_dir(data_path) -> Path:
    return Path(data_path) / PRESET_DIR_NAME


def _name_ok(value: str) -> bool:
    return (bool(value) and value != DEFAULT_PROFILE
            and len(value) <= MAX_NAME_CHARS
            and not _ILLEGAL_CHARS_RE.search(value)
            and value not in (".", "..") and not value.endswith("."))


def clean_name(name) -> str:
    value = " ".join(str(name or "").split())
    if not value:
        raise ConfigPresetError("请填写配置文件名")
    if len(value) > MAX_NAME_CHARS:
        raise ConfigPresetError(f"配置文件名不能超过 {MAX_NAME_CHARS} 个字符")
    if value == DEFAULT_PROFILE:
        raise ConfigPresetError("default 是内置配置文件名，不能占用")
    if not _name_ok(value):
        raise ConfigPresetError('配置文件名不能包含 \\ / : * ? " < > | 等字符')
    return value


def profile_path(data_path, name) -> Path:
    value = str(name or "")
    if not _name_ok(value):
        raise ConfigPresetError("配置文件名不合法")
    return preset_dir(data_path) / f"{value}.json"


def _read_config(path: Path) -> dict:
    data = load_json_ex(path, None)[0]
    if not isinstance(data, dict):
        raise ConfigPresetError("配置文件读不出来，可能已损坏")
    inner = data.get("config")
    if isinstance(inner, dict) and ("note" in data or "created_at" in data):
        return inner
    return data


def _entry(name: str, path: Path, builtin: bool = False) -> dict:
    size, updated_at = 0, ""
    try:
        stat = path.stat()
        size = stat.st_size
        updated_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime))
    except OSError:
        pass
    return {"name": name, "builtin": builtin, "updated_at": updated_at, "size": size}


def list_profiles(data_path) -> List[dict]:
    out = [{"name": DEFAULT_PROFILE, "builtin": True, "updated_at": "", "size": 0}]
    work = preset_dir(data_path)
    if not work.is_dir():
        return out
    for path in work.glob("*.json"):
        if not _name_ok(path.stem):
            continue
        out.append(_entry(path.stem, path))
    out[1:] = sorted(out[1:], key=lambda item: item["name"])
    return out


def save_profile(data_path, name, config, rename_from="") -> dict:
    target = clean_name(name)
    payload = dict(config or {})
    payload.pop("connections", None)
    path = profile_path(data_path, target)
    source = str(rename_from or "").strip()
    if source and source != target:
        old = profile_path(data_path, source)
        if old.is_file():
            old.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    save_json(path, payload)
    return _entry(target, path)


def load_profile(data_path, name) -> dict:
    value = str(name or "")
    if not value or value == DEFAULT_PROFILE:
        return {}
    path = profile_path(data_path, value)
    if not path.is_file():
        raise ConfigPresetError("配置文件不存在，可能已经被删除")
    return _read_config(path)


def delete_profile(data_path, name) -> str:
    value = str(name or "")
    if value == DEFAULT_PROFILE:
        raise ConfigPresetError("default 是内置配置文件，不能删除")
    path = profile_path(data_path, value)
    if not path.is_file():
        raise ConfigPresetError("配置文件不存在，可能已经被删除")
    path.unlink()
    return value


def profile_loader(data_path, name):
    value = str(name or "")
    if not value or value == DEFAULT_PROFILE:
        return None
    path = profile_path(data_path, value).resolve()
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return None
    cached = _LOADERS.get(str(path))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    from .config_loader import ProfileConfigLoader
    loader = ProfileConfigLoader(str(path))
    _LOADERS[str(path)] = (stamp, loader)
    return loader


def forget_profile_loaders(data_path) -> None:
    work = str(preset_dir(data_path).resolve())
    for key in [k for k in _LOADERS if k.startswith(work)]:
        _LOADERS.pop(key, None)
