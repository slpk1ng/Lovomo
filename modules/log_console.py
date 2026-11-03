import logging
import os
import re
import threading
import time
from pathlib import Path

from modules import app_context
from modules.app_paths import runtime_path

LOG_MAX_SIZE_MB_DEFAULT = 5
LOG_SOURCE_MAIN = "Lovomo"
LOG_SOURCE_PLUGIN = "插件"
LOG_LEVEL_INFO = "INFO"
LOG_LEVEL_WARN = "WARN"
LOG_LEVEL_ERROR = "ERROR"
_LOG_TRIM_KEEP_RATIO = 0.8

_LOG_PREFIX_RE = re.compile(
    r"^\[\d{2}:\d{2}:\d{2}\.\d{3}\]\[[^\]]+\]\[(?:%s)\] ?"
    % "|".join((LOG_LEVEL_INFO, LOG_LEVEL_WARN, LOG_LEVEL_ERROR)))
_LOG_ERROR_RE = re.compile(
    r"失败|出错|错误|异常|无法|拒绝|超时|未找到|崩溃|Traceback|Error\b|Exception\b|failed|ok=False",
    re.I)
_LOG_ERROR_ANYWHERE_RE = re.compile(
    r"Traceback|(?:[A-Za-z_][A-Za-z0-9_.]*)?(?:Error|Exception)\s*:|ok=False", re.I)
_LOG_WARN_RE = re.compile(r"警告|跳过|降级|忽略|未配置|未启用|重试|熔断", re.I)
_LOG_ARROW_RE = re.compile(r"\s→\s")
_LOG_CONTINUATION_RE = re.compile(
    r"^\s+\S|^Traceback\b|^During handling\b|^The above exception\b|^Caused by\b"
    r"|^For more information\b")
# 界面对这些行也单独排版
_LOG_BANNER_RE = re.compile(r"[\u2500-\u259f\u2800-\u28ff]{2,}")
_CONSOLE_LEVEL_MARK_RE = re.compile(r"^\[(%s)\] *"
                                    % "|".join((LOG_LEVEL_INFO, LOG_LEVEL_WARN,
                                                LOG_LEVEL_ERROR)))
_CONSOLE_CN_LEVEL_MARK_RE = re.compile(r"^\[?(信息|警告|错误)\]?[：:]? *")
_CONSOLE_WARN_SIGN_RE = re.compile(r"^⚠️? *")
_CONSOLE_WINDOW_MARK_RE = re.compile(r"^\[窗口\] *")
_CONSOLE_PLUGIN_MARK_RE = re.compile(r"^\[插件(?::[^\]]*)?\] *")
_LOG_LEVEL_NAMES = {logging.DEBUG: LOG_LEVEL_INFO, logging.INFO: LOG_LEVEL_INFO,
                    logging.WARNING: LOG_LEVEL_WARN, logging.ERROR: LOG_LEVEL_ERROR,
                    logging.CRITICAL: LOG_LEVEL_ERROR}
_last_console_level = LOG_LEVEL_INFO


def log_stamp(when: float = None) -> str:
    now = time.time() if when is None else float(when)
    return time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"


def log_prefix(level: str, source: str = LOG_SOURCE_MAIN, when: float = None) -> str:
    return f"[{log_stamp(when)}][{source}][{level}] "


def _log_head(text: str) -> str:
    depth = 0
    for index, char in enumerate(text):
        if char == "[":
            depth += 1
        elif char == "]":
            depth = max(0, depth - 1)
        elif depth == 0 and char in "：:":
            return text[:index]
    sep = _LOG_ARROW_RE.search(text)
    return text[:sep.start()] if sep else text


def console_log_level(text: str) -> str:
    if _LOG_ERROR_ANYWHERE_RE.search(str(text or "")):
        return LOG_LEVEL_ERROR
    head = _log_head(text)
    if _LOG_ERROR_RE.search(head):
        return LOG_LEVEL_ERROR
    if _LOG_WARN_RE.search(head):
        return LOG_LEVEL_WARN
    return LOG_LEVEL_INFO


def console_log_line(line: str) -> str:
    global _last_console_level
    if not line.strip() or _LOG_BANNER_RE.search(line) or _LOG_PREFIX_RE.match(line):
        return line
    source = LOG_SOURCE_MAIN
    mark = _CONSOLE_PLUGIN_MARK_RE.match(line)
    if mark:
        source = LOG_SOURCE_PLUGIN
        if mark.group(0).strip() == "[插件]":
            line = line[mark.end():]
    explicit = _CONSOLE_LEVEL_MARK_RE.match(line)
    if explicit:
        level = explicit.group(1)
        line = line[explicit.end():]
    elif _CONSOLE_CN_LEVEL_MARK_RE.match(line):
        cn_mark = _CONSOLE_CN_LEVEL_MARK_RE.match(line)
        level = {"信息": LOG_LEVEL_INFO, "警告": LOG_LEVEL_WARN,
                 "错误": LOG_LEVEL_ERROR}[cn_mark.group(1)]
        line = line[cn_mark.end():]
    elif _CONSOLE_WARN_SIGN_RE.match(line):
        level = LOG_LEVEL_WARN
        line = _CONSOLE_WARN_SIGN_RE.sub("", line)
    else:
        line = _CONSOLE_WINDOW_MARK_RE.sub("", line)
        level = console_log_level(line)
        if level == LOG_LEVEL_INFO and _last_console_level != LOG_LEVEL_INFO \
                and _LOG_CONTINUATION_RE.match(line):
            level = _last_console_level
    _last_console_level = level
    return log_prefix(level, source) + line


class _LogFormatter(logging.Formatter):

    def __init__(self, source: str = LOG_SOURCE_MAIN):
        super().__init__()
        self.source = source

    def formatTime(self, record, datefmt=None):
        return log_stamp(record.created)

    def format(self, record):
        clone = logging.makeLogRecord(record.__dict__)
        clone.levelname = _LOG_LEVEL_NAMES.get(record.levelno, record.levelname)
        return log_prefix(clone.levelname, self.source, record.created) \
            + super().format(clone)


class _CappedFileHandler(logging.FileHandler):

    def __init__(self, filename, max_bytes: int):
        super().__init__(filename, mode="a", encoding="utf-8")
        self.max_bytes = max(1, int(max_bytes))

    def emit(self, record):
        super().emit(record)
        try:
            if self.stream is not None and self.stream.tell() > self.max_bytes:
                self._trim()
        except Exception:
            self.handleError(record)

    def _trim(self):
        self.stream.flush()
        path = Path(self.baseFilename)
        keep = path.read_bytes()[-int(self.max_bytes * _LOG_TRIM_KEEP_RATIO):]
        head, sep, tail = keep.partition(b"\n")
        path.write_bytes(tail if sep else keep)


def apply_log_max_size(config) -> None:
    try:
        try:
            max_mb = int(config.get("log_max_size_mb", LOG_MAX_SIZE_MB_DEFAULT))
        except (TypeError, ValueError):
            max_mb = LOG_MAX_SIZE_MB_DEFAULT
        log_path = runtime_path("app.log")
        root = logging.getLogger()
        for handler in list(root.handlers):
            if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == log_path:
                root.removeHandler(handler)
                handler.close()
        handler = _CappedFileHandler(log_path, max(1, max_mb) * 1024 * 1024)
        handler.setFormatter(_LogFormatter())
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    except Exception as e:
        print(f"[警告] 日志文件大小上限未生效：{type(e).__name__}: {e}")


global_log_buffer = []
LOG_BUFFER_MAX = 5000     # 日志缓冲行数默认值（config: webui_log_buffer_lines）
LOG_TAIL_DEFAULT = 500
_LOG_FULL_MAX_CHARS = 400_000
_LOG_PENDING_MAX_CHARS = 8192   # 未换行的半行日志最长保留多少字符
log_lock = threading.Lock()


def _runtime_log_limits() -> tuple:
    try:
        buffer_lines = max(500, int(app_context.global_config.get("webui_log_buffer_lines",
                                                      LOG_BUFFER_MAX) or LOG_BUFFER_MAX))
        tail_lines = max(50, int(app_context.global_config.get("webui_log_tail_lines",
                                                   LOG_TAIL_DEFAULT) or LOG_TAIL_DEFAULT))
        return buffer_lines, tail_lines
    except (AttributeError, TypeError, ValueError):
        return LOG_BUFFER_MAX, LOG_TAIL_DEFAULT


class StdoutRedirector:
    def __init__(self, original_stream):
        # 兼容 console=False 时 sys.stdout 为 None 的情况
        if original_stream is None:
            try:
                original_stream = open(os.devnull, 'w', encoding='utf-8')
            except Exception:
                original_stream = None
        self.original_stream = original_stream
        self._last_saved_config = None
        self._pending = ""

    def write(self, message):
        if not message:
            return
        with log_lock:
            if self.original_stream is not None:
                try:
                    self.original_stream.write(message)
                    self.original_stream.flush()
                except Exception:
                    pass  # 无控制台时忽略写入错误
            buffer_cap, _ = _runtime_log_limits()
            self._pending += message
            lines = self._pending.split("\n")
            self._pending = lines.pop()
            if len(self._pending) > _LOG_PENDING_MAX_CHARS:
                lines.append(self._pending)
                self._pending = ""
            for line in lines:
                global_log_buffer.append(console_log_line(line.rstrip('\r\n')))
                if len(global_log_buffer) > buffer_cap:
                    del global_log_buffer[:len(global_log_buffer) - buffer_cap]

    def flush(self):
        if self.original_stream is not None:
            try:
                self.original_stream.flush()
            except Exception:
                pass


def dump_log_buffer(path) -> str:
    try:
        with log_lock:
            lines = list(global_log_buffer)
        if not lines:
            return ""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(target)
    except Exception as e:
        print(f"[日志] 退出时保存界面日志失败：{type(e).__name__}: {e}")
        return ""


def log_window_event(message: str) -> None:
    print(message)
    try:
        logging.getLogger("lovomo.window").info(message)
    except Exception:
        pass
