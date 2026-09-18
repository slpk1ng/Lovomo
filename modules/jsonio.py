"""JSON 状态文件的原子写入与损坏保护：进程被强杀时不会留下半截文件，
损坏的旧文件会被改名保留（.corrupt）而不是静默当作空数据。"""
import json
import os
import uuid
from pathlib import Path


def load_json_ex(path, default):
    """返回 (data, readable)。

    readable=False 表示文件没能读进来（被占用/权限/IO 错误），此时调用方
    应当放弃本次自动保存，否则空数据会被写回、覆盖掉磁盘上完好的内容。
    内容损坏（JSON 解析失败）会先备份成 .corrupt，再按"可读"处理。
    """
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
    # 临时文件名必须唯一：同一文件的两次并发保存（WebUI 线程与 bot 协程）
    # 若共用同一个 .tmp，会互相覆盖，最终提交的内容可能来自另一次写入
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=2))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(str(tmp), str(path))
