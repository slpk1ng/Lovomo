"""JSON 状态文件的原子写入与损坏保护：进程被强杀时不会留下半截文件，
损坏的旧文件会被改名保留（.corrupt）而不是静默当作空数据。"""
import json
import os
from pathlib import Path


def load_json(path, default):
    path = Path(path)
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, (dict, list)):
                return data
            raise ValueError("顶层不是 dict/list")
    except Exception as e:
        try:
            os.replace(str(path), str(path) + ".corrupt")
            print(f"[jsonio] 状态文件损坏，已备份为 {path}.corrupt：{e}")
        except Exception:
            pass
    return default


def save_json(path, data):
    path = Path(path)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))
