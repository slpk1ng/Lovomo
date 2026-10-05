# Lovomo 应用路径模块：可执行目录/用户数据目录定位、数据目录迁移采纳、资源路径解析（纯叶子）。
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Optional


def _harden_stdio():
    """让 print 在任何终端/无终端环境下都不会把程序带崩。

    console=False 打包时 sys.stdout 是 None（print 直接 AttributeError）；
    从 GBK 代码页的 cmd 启动时，这类字符编码不了会抛 UnicodeEncodeError。
    两种都在任何 print 之前处理掉。
    """
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
            # 保留终端原本的编码（中文才能正常显示），只把编码不了的字符换成 ?
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def _probe_writable(directory: Path) -> bool:
    """目录是否真的能写：os.access 在 Windows 上会误报，实测一次最准。"""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".lovomo_write_{os.getpid()}"
        probe.write_text("1", encoding="utf-8")
    except Exception:
        return False
    # 写成功就说明可写。清理失败（杀软/索引器正占着这个刚建的文件）不该改判，
    # 否则日志会被静默改写到用户目录；尽力删干净，删不掉也不影响结论。
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
    """程序目录不可写时的兜底目录（装进 Program Files 且没提权就会走到这里）。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = Path(base) / "Lovomo"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return path


def runtime_path(filename: str, preferred_dir: Optional[Path] = None) -> Path:
    """运行时文件（日志等）的落点：程序目录能写就用它，否则落到用户目录。"""
    directory = (Path(preferred_dir) if preferred_dir else Path.cwd()).resolve()
    if _probe_writable(directory):
        return directory / filename
    return user_data_dir() / filename


def app_dir() -> Path:
    """程序自身所在目录：打包后是 exe 目录，源码运行时是项目根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # 源码运行时锚定项目根（本模块在 modules/ 下，须上跳一级，与搬迁前 main.py 的 __file__ 语义一致）
    return Path(__file__).resolve().parent.parent


def _remove_path(path: Path) -> None:
    """尽力删掉一个文件或目录，失败不抛。"""
    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    except Exception:
        pass


def _data_signature(path: Path) -> tuple:
    """（文件数, 总字节数），用来核对搬运前后的内容是否一致。"""
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
    """把老版本写在程序目录里的数据搬到用户目录，返回最终该用的落点。

    防御式搬运：整份复制到临时位置 → 核对文件数与字节数 → 原子改名到目标 →
    最后才删旧位置。任何一步不对就回退、保留原位置并继续用它。
    宁可「没搬成」，也不能为了搬家把用户数据弄丢。
    """
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
    """用户数据（data 文件夹 / config.json）的最终落点。

    打包运行时固定放 %LOCALAPPDATA%\\Lovomo：数据不跟安装目录绑在一起，换目录重装、
    覆盖升级都能接上（写在程序目录里的话，换了目录就成了「聊天记录全没了」）。
    源码运行时沿用程序目录，免得开发与测试被搬来搬去。
    """
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
        # 源码运行时锚定项目根（本模块在 modules/ 下，须上跳一级，与搬迁前 main.py 的 __file__ 语义一致）
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
