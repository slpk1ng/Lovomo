import json
import os
import uuid
from pathlib import Path


def load_json_ex(path, default):
    path = Path(path)
    try:
        if not path.exists():
            return default, True
        raw = path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[jsonio] 状态文件读取失败，本次沿用默认值：{path}：{e}")
        return default, False
    try:
        data = json.loads(raw)
        if not isinstance(data, (dict, list)):
            raise ValueError("顶层不是 dict/list")
        return data, True
    except Exception as e:
        try:
            os.replace(str(path), str(path) + ".corrupt")
            print(f"[jsonio] 状态文件损坏，已备份为 {path}.corrupt：{e}")
        except Exception:
            pass
    return default, True


def load_json(path, default):
    return load_json_ex(path, default)[0]


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, indent=2))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
