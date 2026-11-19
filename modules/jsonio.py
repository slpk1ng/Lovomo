import json
import os
import uuid
import threading
import weakref
from contextlib import contextmanager
from pathlib import Path


_LOCKS = weakref.WeakValueDictionary()
_LOCKS_GUARD = threading.Lock()


@contextmanager
def file_lock(path):
    """Serialize a read/modify/write transaction within this process."""
    key = os.path.normcase(os.path.abspath(path))
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.RLock())
    with lock:
        yield


def load_json_ex(path, default):
    with file_lock(path):
        return _load_json_unlocked(path, default)


def _load_json_unlocked(path, default):
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default, True
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
            backup = path.with_name(f"{path.name}.{uuid.uuid4().hex}.corrupt")
            os.replace(str(path), str(backup))
            print(f"[jsonio] 状态文件损坏，已备份为 {backup}：{e}")
        except OSError:
            return default, False
    return default, True


def load_json(path, default):
    return load_json_ex(path, default)[0]


def save_json(path, data):
    with file_lock(path):
        _save_json_unlocked(path, data)


def _save_json_unlocked(path, data):
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
