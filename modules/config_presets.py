"""配置预设：把当前配置导出成一份 JSON 存在 data 下，需要时再切回去。

每个预设是一个自带备注与导出时间的 JSON 文件，放在 data/config_presets/ 下，
用户可以随时删除；文件名（不含后缀）就是预设编号。
"""
import re
import time
from pathlib import Path
from typing import List

from .jsonio import load_json_ex, save_json

PRESET_DIR_NAME = "config_presets"
MAX_NOTE_CHARS = 100

# 编号只认导出时生成的时间戳格式，避免传进来的值拼出目录外的路径
_ID_RE = re.compile(r"[0-9]{8}-[0-9]{6}(?:_[0-9]+)?\Z")


class ConfigPresetError(Exception):
    """预设操作失败的原因，文案可直接展示给用户。"""


def preset_dir(data_path) -> Path:
    return Path(data_path) / PRESET_DIR_NAME


def _clean_note(note) -> str:
    return " ".join(str(note or "").split())[:MAX_NOTE_CHARS]


def _preset_path(data_path, preset_id) -> Path:
    value = str(preset_id or "")
    if not _ID_RE.match(value):
        raise ConfigPresetError("预设编号不合法")
    return preset_dir(data_path) / f"{value}.json"


def _read(path: Path) -> dict:
    data = load_json_ex(path, None)[0]
    if not isinstance(data, dict) or not isinstance(data.get("config"), dict):
        raise ConfigPresetError("预设文件读不出来，可能已损坏")
    return data


def _new_id(work: Path) -> str:
    base = time.strftime("%Y%m%d-%H%M%S")
    preset_id, index = base, 1
    while (work / f"{preset_id}.json").exists():
        index += 1
        preset_id = f"{base}_{index}"
    return preset_id


def save_preset(data_path, config_payload: dict, note: str = "") -> dict:
    """把一份导出配置存成新预设，返回它的信息。"""
    work = preset_dir(data_path)
    work.mkdir(parents=True, exist_ok=True)
    preset_id = _new_id(work)
    body = {"note": _clean_note(note),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "config": config_payload}
    path = work / f"{preset_id}.json"
    save_json(path, body)
    return {"id": preset_id, "note": body["note"], "created_at": body["created_at"],
            "size": path.stat().st_size}


def list_presets(data_path) -> List[dict]:
    work = preset_dir(data_path)
    if not work.is_dir():
        return []
    presets = []
    for path in work.glob("*.json"):
        if not _ID_RE.match(path.stem):
            continue
        try:
            body = _read(path)
        except ConfigPresetError as e:
            print(f"[配置预设] 跳过读不出来的预设 {path.name}：{e}")
            continue
        presets.append({"id": path.stem, "note": str(body.get("note") or ""),
                        "created_at": str(body.get("created_at") or ""),
                        "size": path.stat().st_size})
    presets.sort(key=lambda item: item["id"], reverse=True)
    return presets


def load_preset(data_path, preset_id) -> dict:
    path = _preset_path(data_path, preset_id)
    if not path.is_file():
        raise ConfigPresetError("预设不存在，可能已经被删除")
    return _read(path)["config"]


def update_note(data_path, preset_id, note: str) -> dict:
    path = _preset_path(data_path, preset_id)
    if not path.is_file():
        raise ConfigPresetError("预设不存在，可能已经被删除")
    body = _read(path)
    body["note"] = _clean_note(note)
    save_json(path, body)
    return {"id": path.stem, "note": body["note"]}


def delete_preset(data_path, preset_id) -> str:
    path = _preset_path(data_path, preset_id)
    if not path.is_file():
        raise ConfigPresetError("预设不存在，可能已经被删除")
    path.unlink()
    return path.stem
