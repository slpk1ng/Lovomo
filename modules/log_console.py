import logging
import os
import re
import threading
import time
from pathlib import Path

from modules import app_context
from modules.app_paths import runtime_path

LOG_MAX_SIZE_MB_DEFAULT = 5
# 每条日志的前缀统一是 [时:分:秒.毫秒][来源][级别]，来源只有主程序与插件两种
LOG_SOURCE_MAIN = "Lovomo"
LOG_SOURCE_PLUGIN = "插件"
LOG_LEVEL_INFO = "INFO"
LOG_LEVEL_WARN = "WARN"
LOG_LEVEL_ERROR = "ERROR"
# 裁剪时保留的比例：留出余量，否则每写一行都要重写一次整个日志文件
_LOG_TRIM_KEEP_RATIO = 0.8

_LOG_PREFIX_RE = re.compile(
    r"^\[\d{2}:\d{2}:\d{2}\.\d{3}\]\[[^\]]+\]\[(?:%s)\] ?"
    % "|".join((LOG_LEVEL_INFO, LOG_LEVEL_WARN, LOG_LEVEL_ERROR)))
# print() 出来的行没有级别字段，只能按内容判定（先报错后警告）。
# 判定只看冒号/箭头之前的正文：后面跟的是话题、用户消息、工具输出等自由文本，
# 里面出现「失败/拒绝」等词并不代表这条日志本身出错。
_LOG_ERROR_RE = re.compile(
    r"失败|出错|错误|异常|无法|拒绝|超时|未找到|崩溃|Traceback|Error\b|Exception\b|failed|ok=False",
    re.I)
# 异常类型名 + 冒号（HTTPStatusError: Client error …）出现在冒号之后，也是真的出错：
# 「只看冒号前正文」是怕引用文本里的"失败/拒绝"误判，而异常名不会这么出现。
_LOG_ERROR_ANYWHERE_RE = re.compile(
    r"Traceback|(?:[A-Za-z_][A-Za-z0-9_.]*)?(?:Error|Exception)\s*:|ok=False", re.I)
_LOG_WARN_RE = re.compile(r"警告|跳过|降级|忽略|未配置|未启用|重试|熔断", re.I)
_LOG_ARROW_RE = re.compile(r"\s→\s")
# 续行：报错信息与 traceback 本身就是多行的，第一行之外的行看不出级别，
# 只能跟着上一行走，否则同一段报错里会出现「只有第一行是错误」。
_LOG_CONTINUATION_RE = re.compile(
    r"^\s+\S|^Traceback\b|^During handling\b|^The above exception\b|^Caused by\b"
    r"|^For more information\b")
# 启动横幅是块状字符画（方块 + 盲文点阵），加前缀会把画面割裂，
# 界面对这些行也单独排版
_LOG_BANNER_RE = re.compile(r"[\u2500-\u259f\u2800-\u28ff]{2,}")
# 行首已有的标记直接并入前缀：级别标记与 [窗口] 丢掉，[插件] 决定来源
# 代码里还有中文写法（「警告：…」「错误：…」以及 ⚠️ 前缀），
# 判定级别时一并当作行首级别标记，再重新按标准前缀输出
_CONSOLE_LEVEL_MARK_RE = re.compile(r"^\[(%s)\] *"
                                    % "|".join((LOG_LEVEL_INFO, LOG_LEVEL_WARN,
                                                LOG_LEVEL_ERROR)))
# 代码里还留着中文写法（「警告：…」「错误：…」「[警告] …」以及 ⚠️ 前缀），
# 判定级别时一并当作行首级别标记，丢掉后重新按标准前缀输出
_CONSOLE_CN_LEVEL_MARK_RE = re.compile(r"^\[?(信息|警告|错误)\]?[：:]? *")
_CONSOLE_WARN_SIGN_RE = re.compile(r"^⚠️? *")
_CONSOLE_WINDOW_MARK_RE = re.compile(r"^\[窗口\] *")
_CONSOLE_PLUGIN_MARK_RE = re.compile(r"^\[插件(?::[^\]]*)?\] *")
_LOG_LEVEL_NAMES = {logging.DEBUG: LOG_LEVEL_INFO, logging.INFO: LOG_LEVEL_INFO,
                    logging.WARNING: LOG_LEVEL_WARN, logging.ERROR: LOG_LEVEL_ERROR,
                    logging.CRITICAL: LOG_LEVEL_ERROR}
# 上一行的级别：多行报错的续行跟着它标级别
_last_console_level = LOG_LEVEL_INFO


def log_stamp(when: float = None) -> str:
    """日志前缀里的时间：时:分:秒.毫秒。"""
    now = time.time() if when is None else float(when)
    return time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"


def log_prefix(level: str, source: str = LOG_SOURCE_MAIN, when: float = None) -> str:
    return f"[{log_stamp(when)}][{source}][{level}] "


def _log_head(text: str) -> str:
    """判定级别时只看冒号/箭头之前的正文。

    方括号标记里的冒号不算分隔符，否则 [插件:名字] 会被从中间截断，
    后面那句「入口执行失败」就判不出来了。
    """
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
    """按正文判定 print 出来的这一行是信息、警告还是错误。"""
    if _LOG_ERROR_ANYWHERE_RE.search(str(text or "")):
        return LOG_LEVEL_ERROR
    head = _log_head(text)
    if _LOG_ERROR_RE.search(head):
        return LOG_LEVEL_ERROR
    if _LOG_WARN_RE.search(head):
        return LOG_LEVEL_WARN
    return LOG_LEVEL_INFO


def console_log_line(line: str) -> str:
    """给界面日志的一行补上统一前缀；横幅与已有前缀的行原样返回。"""
    global _last_console_level
    if not line.strip() or _LOG_BANNER_RE.search(line) or _LOG_PREFIX_RE.match(line):
        return line
    source = LOG_SOURCE_MAIN
    mark = _CONSOLE_PLUGIN_MARK_RE.match(line)
    if mark:
        source = LOG_SOURCE_PLUGIN
        # 裸 [插件] 与来源栏重复，去掉；[插件:名字] 带着插件名，保留在正文里
        if mark.group(0).strip() == "[插件]":
            line = line[mark.end():]
    explicit = _CONSOLE_LEVEL_MARK_RE.match(line)
    if explicit:
        # 行首写明的级别优先于正文推断：作者已经标了「警告」就别再按字面改判
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
    """app.log 的每一行也走同一套前缀：[时:分:秒.毫秒][来源][级别] 正文。"""

    def __init__(self, source: str = LOG_SOURCE_MAIN):
        super().__init__()
        self.source = source

    def formatTime(self, record, datefmt=None):
        return log_stamp(record.created)

    def format(self, record):
        # 换掉级别名不能改 record 本身：同一个记录还会被别的处理器格式化
        clone = logging.makeLogRecord(record.__dict__)
        clone.levelname = _LOG_LEVEL_NAMES.get(record.levelno, record.levelname)
        return log_prefix(clone.levelname, self.source, record.created) \
            + super().format(clone)


class _CappedFileHandler(logging.FileHandler):
    """日志文件超过字节上限时丢弃文件里最早的记录，只保留尾部内容。"""

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
        # 处理器以追加模式打开，截断文件后后续写入仍落在文件末尾，
        # 所以不需要关掉再重开 stream（重开失败会丢日志）。
        self.stream.flush()
        path = Path(self.baseFilename)
        keep = path.read_bytes()[-int(self.max_bytes * _LOG_TRIM_KEEP_RATIO):]
        head, sep, tail = keep.partition(b"\n")
        path.write_bytes(tail if sep else keep)


def apply_log_max_size(config) -> None:
    """按配置的 MB 上限重建 app.log 的处理器，配置改完立即生效。

    日志设置失败不该拦住程序启动或配置热重载，所以这里吞掉异常只留提示。
    """
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
LOG_TAIL_DEFAULT = 500    # 精简模式尾部行数默认值（config: webui_log_tail_lines）
_LOG_FULL_MAX_CHARS = 400_000   # "显示完整日志"的极端字符上限（仅防响应撑爆）
_LOG_PENDING_MAX_CHARS = 8192   # 未换行的半行日志最长保留多少字符
log_lock = threading.Lock()


def _runtime_log_limits() -> tuple:
    """日志缓冲行数与精简模式尾部行数，均可在 config.json 调整。

    程序最早期（配置尚未加载）的输出走默认值，任何读取异常都按默认值兜底，
    绝不能因为取配置失败影响日志记录本身。
    """
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
            # print() 先写正文、再单独写一个换行：按单次 write 切行会把同一个
            # 换行切成一条空记录，日志里每行后面就多出一个空行，隐藏掉的行
            # 更会留下一整片空白。这里把没写完的半行留到下一次，凑够一整行
            # 才进缓冲，保证「一行输出 = 一条记录」。
            self._pending += message
            lines = self._pending.split("\n")
            self._pending = lines.pop()
            if len(self._pending) > _LOG_PENDING_MAX_CHARS:
                lines.append(self._pending)
                self._pending = ""
            for line in lines:
                # 只删行尾换行，别用 strip()：行首缩进是日志的一部分。
                # \r 也一起去掉，否则 Windows 下每行都留一个裸 \r。
                global_log_buffer.append(console_log_line(line.rstrip('\r\n')))
                if len(global_log_buffer) > buffer_cap:
                    del global_log_buffer[:len(global_log_buffer) - buffer_cap]

    def flush(self):
        if self.original_stream is not None:
            try:
                self.original_stream.flush()
            except Exception:
                pass


def log_window_event(message: str) -> None:
    """窗口生命周期事件：既进界面日志，也写进 app.log。

    「进程莫名消失」这类问题只能事后看日志，而界面日志只在内存里（程序一退就
    没了），所以这里同时交给 logging（落 app.log）。
    """
    print(message)
    try:
        logging.getLogger("lovomo.window").info(message)
    except Exception:
        pass
