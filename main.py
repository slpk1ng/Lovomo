import asyncio
import hashlib
import hmac
import io
import json
import logging
import os
import random
import re
import shutil
import sys
import threading
import time
import traceback
import zipfile
from base64 import b64encode, b64decode
from pathlib import Path
from typing import Optional, Dict, Any, List
from urllib.parse import urlsplit


def _harden_stdio():
    """让 print 在任何终端/无终端环境下都不会把程序带崩。

    console=False 打包时 sys.stdout 是 None（print 直接 AttributeError）；
    从 GBK 代码页的 cmd 启动时，⚠️ 这类字符编码不了会抛 UnicodeEncodeError。
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


_harden_stdio()


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
    return Path(__file__).resolve().parent


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


import httpx
try:
    import webview
    HAS_WEBVIEW = True
except ImportError:
    HAS_WEBVIEW = False
    print("警告：未安装 pywebview，将使用浏览器访问。可运行 pip install pywebview 启用。")

try:
    from napcat import NapCatClient, PrivateMessageEvent, GroupMessageEvent, Text, Record, Image, At, Reply
except ImportError:
    print("错误：未安装 napcat-sdk，请先运行 pip install napcat-sdk")
    raise
try:
    from aiohttp import web
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False
    print("警告：未安装 aiohttp，WebUI 管理功能将不可用。可运行 pip install aiohttp 启用。")

# ---------------- 功能模块 ----------------
from modules.database import DatabaseManager
from modules.scheduler import get_scheduler, SchedulerManager
from modules.stats import StatsManager
from modules.stickers import (StickerManager, IMAGE_EXTS, MIME_BY_EXT, safe_sticker_name,
                              DEFAULT_CAPTURE_PROMPT)
from modules.audio_level import (SPEECH_MIN_RATIO, measure as measure_audio,
                                 pitch_note, quality_notes as audio_quality_notes)
from modules.tools import ToolRegistry
from modules.profiles import UserProfileManager, DEFAULT_EXTRACT_PROMPT
from modules.lexicon import LexiconManager, DEFAULT_LEARN_PROMPT, DEFAULT_INJECT_TEMPLATE
from modules.rag import RAGManager, extract_text_from_file
from modules.todo_manager import TodoManager, DEFAULT_EXTRACT_PROMPT as TODO_EXTRACT_PROMPT
from modules.jobs import ScheduledJobManager, generate_proactive_text
from modules.events import EventManager
from modules.plugin_publisher import (publish_plugin as _publish_to_github,
                                       unpublish_plugin as _unpublish_from_github,
                                       verify_token as _verify_github_token,
                                       PublishError as _PublishError)
from modules.mood import (MoodManager, commit_mood, current_mood, judge_and_decide,
                          judge_enabled, mood_enabled, mood_style)
from modules.sender import MessageSender, VoicePacer
from modules.ghmirror import DEFAULT_MIRRORS
from modules.plugins import (PluginManager, ALLOWED_EXTS as ALLOWED_ASSET_EXTS,
                             MAX_ASSET_BYTES as MAX_PLUGIN_ASSET_BYTES,
                             WEBUI_NAME as PLUGIN_WEBUI_NAME,
                             safe_asset_name, unique_asset_name)

# 每次刷新最多查几个 Release 的点赞数（GitHub 匿名接口有次数限制）
MAX_REACTION_LOOKUPS = 12

# 市场扫分支最多翻几页（每页 100 条），只是防止仓库异常时无限翻页
MAX_BRANCH_PAGES = 10

# 官方市场的第三方来源清单最多认几个仓库（清单靠别人提 PR 维护，防它跑飞）
MARKET_SOURCE_REPOS_MAX = 20

# 插件自带页面（功能页 / webui.html）注入的桥接脚本：同源 iframe 直接调父窗口上的
# lovomoHost。必须插在插件自己的脚本之前，否则插件在解析阶段拿不到 window.lovomo。
PLUGIN_BRIDGE_TEMPLATE = """<script>
(function () {
  var info = /*__LOVOMO_INFO__*/ null;
  var host = (parent !== window && parent.lovomoHost) ? parent.lovomoHost : null;
  var api = Object.assign({}, info, {
    asset: function (name) {
      return '/api/plugins/asset?id=' + encodeURIComponent(info.id)
           + '&name=' + encodeURIComponent(name || '');
    },
    theme: function () {
      var root = parent.document.documentElement;
      return {skinOn: root.classList.contains('skin-on'),
              accent: parent.getComputedStyle(root).getPropertyValue('--skin-accent').trim()};
    },
    log: function () {
      if (host) { host.log(info.id, Array.prototype.slice.call(arguments)); }
      else { console.log.apply(console, arguments); }
    },
    toast: function (text, isErr) { if (host) { host.toast(info.id, text, isErr); } },
    save: function (patch) {
      if (!host) { return Promise.resolve(api.settings); }
      return Promise.resolve(host.save(info.id, patch)).then(function (merged) {
        if (merged) { api.settings = merged; }
        return api.settings;
      });
    },
    apply: function () { return host ? host.apply(info.id) : null; },
    dirty: function () { return host ? host.dirty(info.id) : false; },
    reload: function () { return api.settings; },
    close: function () { if (host) { host.close(info.id); } }
  });
  window.lovomo = api;
  document.addEventListener('DOMContentLoaded', function () {
    window.dispatchEvent(new CustomEvent('lovomo:ready', {detail: api}));
  });
})();
</script>
"""
from modules.llm_helpers import (RoleContext, build_chat_messages, chat_once,
                                chat_with_tools, normalize_sentences,
                                normalize_single, sentence_obj_has_text,
                                split_multi_clause_sentences,
                                stream_chat, extract_json,
                                SentenceStreamParser, get_image_reply, download_image,
                                sniff_image_mime, repair_sentence_lang, strip_quote_note,
                                segment_for_tts, speaker_labeled_lines,
                                text_needs_tools, tool_flow_can_skip,
                                image_self_claim, image_identity_note,
                                IMAGE_CLAIM_WARNING, sent_links, record_sent_links,
                                urls_in_text, is_search_request, is_search_dissatisfied,
                                lang_text_broken, translate_to_lang,
                                available_mimics, set_mimics_provider,
                                model_list_endpoints, looks_like_full_endpoint)
from modules.tts import synthesize_sentence, resolve_tts_path
from modules.tls import verified_context
from modules.audio_trim import normalize_audio
from modules.asr import (start_job as start_asr_job, get_job as get_asr_job,
                         AUDIO_MIMES)
from modules.tts_service import (process_manager, ensure_tts_service,
                                 auto_start_and_switch_tts, mark_exiting)

LOG_MAX_SIZE_MB_DEFAULT = 5
_LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
# 裁剪时保留的比例：留出余量，否则每写一行都要重写一次整个日志文件
_LOG_TRIM_KEEP_RATIO = 0.8


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
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    except Exception as e:
        print(f"[警告] 日志文件大小上限未生效：{type(e).__name__}: {e}")


try:
    logging.basicConfig(filename=str(runtime_path("app.log")), encoding="utf-8",
                        level=logging.INFO, format=_LOG_FORMAT)
except Exception:
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)

last_proactive_sent: Dict[str, float] = {}

def get_resource_path(relative_path):
    if hasattr(sys, '_MEIPASS'):
        base_path = Path(sys._MEIPASS)
    else:
        base_path = Path(__file__).parent
    return base_path / relative_path


def _pid_alive(pid) -> bool:
    """跨平台判断 pid 对应的进程是否仍在运行。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                # 只有"权限不足"才说明进程还在；其余错误码（进程不存在）就是已退出
                ERROR_ACCESS_DENIED = 5
                return kernel32.GetLastError() == ERROR_ACCESS_DENIED
            try:
                code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                return bool(ok) and code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(h)
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


# ---------------------------------------------------------------------------
# 单实例
# ---------------------------------------------------------------------------
# 程序已经在跑时用户又双击一次 exe，不应该再起一套进程：那会多出一个任务栏
# 条目、多连一份 NapCat、多占一次 WebUI 端口。这里用命名互斥体判定「已经有
# 实例在跑」，再用一个命名事件通知那个实例把窗口亮出来。
#
# 用互斥体而不是锁文件：进程被强杀时系统会自动释放，不会留下需要人工清理的
# 残留。名字带 Local\ 前缀，按登录会话隔离，多用户各自算一个实例。

_INSTANCE_MUTEX_NAME = "Local\\Lovomo.SingleInstance"
_INSTANCE_SHOW_EVENT_NAME = "Local\\Lovomo.ShowWindow"
_INSTANCE_STATE = {"mutex": None, "event": None, "waiter": False}


def _kernel32():
    """kernel32 句柄；必须开 use_last_error，否则读不到可靠的 GetLastError。"""
    import ctypes
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _acquire_single_instance() -> bool:
    """抢占单实例名额，并建好唤醒事件。

    返回 False 表示已有实例在跑，调用方应该通知它然后退出。非 Windows 或
    接口异常时一律返回 True —— 宁可多开一次，也不能让程序打不开。
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        k32 = _kernel32()
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                     wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL,
                                     wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateEventW.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL

        mutex = k32.CreateMutexW(None, False, _INSTANCE_MUTEX_NAME)
        if not mutex:
            return True
        if ctypes.get_last_error() == 183:          # ERROR_ALREADY_EXISTS
            k32.CloseHandle(mutex)
            return False
        _INSTANCE_STATE["mutex"] = mutex
        # 手动重置事件：谁收到谁负责 ResetEvent，否则下一次启动会被上一次
        # 留下的信号立刻再唤醒一遍。
        _INSTANCE_STATE["event"] = k32.CreateEventW(
            None, True, False, _INSTANCE_SHOW_EVENT_NAME)
        return True
    except Exception:
        return True


def _signal_existing_instance() -> bool:
    """通知已经在跑的那个实例把窗口亮出来。"""
    if os.name != "nt":
        return False
    try:
        from ctypes import wintypes
        k32 = _kernel32()
        k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL,
                                   wintypes.LPCWSTR]
        k32.OpenEventW.restype = wintypes.HANDLE
        k32.SetEvent.argtypes = [wintypes.HANDLE]
        k32.SetEvent.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        EVENT_MODIFY_STATE = 0x0002
        handle = k32.OpenEventW(EVENT_MODIFY_STATE, False,
                                _INSTANCE_SHOW_EVENT_NAME)
        if not handle:
            return False
        try:
            return bool(k32.SetEvent(handle))
        finally:
            k32.CloseHandle(handle)
    except Exception:
        return False


def _start_instance_show_waiter(on_show) -> None:
    """等「又有人双击了 exe」，收到就把窗口亮出来。"""
    if os.name != "nt" or _INSTANCE_STATE.get("waiter"):
        return
    handle = _INSTANCE_STATE.get("event")
    if not handle:
        return
    try:
        from ctypes import wintypes
        k32 = _kernel32()
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.ResetEvent.argtypes = [wintypes.HANDLE]
        k32.ResetEvent.restype = wintypes.BOOL
        _INSTANCE_STATE["waiter"] = True

        def _loop():
            # 半秒一轮而不是无限等待：进程退出时线程能自己收掉。
            while True:
                if k32.WaitForSingleObject(handle, 500) == 0:   # WAIT_OBJECT_0
                    k32.ResetEvent(handle)
                    try:
                        on_show()
                    except Exception:
                        pass

        threading.Thread(target=_loop, daemon=True).start()
    except Exception:
        pass


def _candidate_icon_paths() -> list:
    """按打包/开发环境查找托盘与窗口图标，不写死任何路径。"""
    cands = []
    try:
        cands.append(get_resource_path("icon.ico"))
    except Exception:
        pass
    try:
        cands.append(Path(sys.executable).parent / "icon.ico")
    except Exception:
        pass
    try:
        cands.append(Path.cwd() / "icon.ico")
    except Exception:
        pass
    out = []
    for p in cands:
        try:
            if p and Path(p).exists():
                out.append(str(Path(p).resolve()))
        except Exception:
            continue
    return out


# ============================================================================
# 窗口几何记忆（位置/大小/是否最大化）
# ============================================================================
# 点任务栏图标或托盘「打开 Lovomo」还原窗口时，本来最大化的窗口
# 会被还原成一个往右下角偏移的小窗口；另一次是「打开只有一个很小的窗口」。
# 两个现象同源：坐标被 DPI 二次缩放。
#
# 这条链路要绝对小心，pywebview 的 winforms 后端在 DPI 处理上是自相矛盾的：
#   · create_window(width, height, x, y) 被当成「逻辑像素」，后端会乘 _scale
#     转成物理像素再交给 WinForms（见 winforms.py:209/217）。
#   · window.width / height / x / y 读回来的已经是「逻辑像素」，后端把物理
#     像素除以 _scale（见 winforms.py:1066/1075 的 get_position/get_size）。
#
# 所以只要把读到的值原样写回去，就会被再乘一次 _scale —— 125% 缩放下
# 1721×926 会变成 2151×1157，2560×1440 的屏幕装不下，看起来就是"小窗口
# 跑到奇怪的位置"，而最大化状态也在往返中丢掉了。
#
# 解决方式：本项目在 _screen_rects() 里调了 SetProcessDPIAware()（见下方），
# 进程已是 DPI 感知的，系统不会替我们虚拟化任何坐标 —— 于是干脆全程统一用
# **物理像素**：自己读系统 API 拿矩形，自己除/乘缩放系数，写回时再折算成
# pywebview 需要的逻辑像素。这样无论 DPI 是多少都只缩放一次。
#
# 所有落在 _WINDOW_GEOMETRY["normal"] 里的值都是物理像素，
#       传给 create_window / move / resize 之前必须过 _phys_to_logical()。

_WINDOW_GEOMETRY_FILE = "window_geometry.json"
# 几何数据的口径版本。老版本把 pywebview 的「逻辑像素」当物理像素存了，
# 在 125%/150% 缩放下这份数据是坏的（会被再缩放一次，窗口超出屏幕）。
# 升到 v2 后旧文件一律丢弃，避免用户升级后仍被脏数据坑一次。
_GEOMETRY_VERSION = 2
_WINDOW_GEOMETRY = {
    "geometry_v": _GEOMETRY_VERSION,
    "maximized": True,
    "normal": {"x": None, "y": None, "width": 1280, "height": 720},
}
_geometry_save_timer = None
_geometry_lock = threading.Lock()


def _geometry_path() -> Path:
    return runtime_path(_WINDOW_GEOMETRY_FILE, Path(user_data_dir()))


def _webview_profile_dir() -> str:
    """WebView2 的站点数据目录；固定下来，否则每次启动都是临时目录，localStorage 留不住。

    目录不可写时返回空串，让 pywebview 用自己的默认位置（仍然是持久化的）。
    """
    path = user_data_dir() / "webview"
    return str(path) if _probe_writable(path) else ""


# ---------------------------------------------------------------------------
# 物理像素 <-> pywebview 逻辑像素
# ---------------------------------------------------------------------------

def _window_scale(window=None) -> float:
    """当前进程的 DPI 缩放系数（1.0 = 100%，1.25 = 125%）。

    已经 SetProcessDPIAware 的进程里 GetDpiForSystem() 拿到的就是真实值。
    取不到时返回 1.0，此时物理==逻辑，全部换算退化成恒等，不会出错。
    """
    if os.name != "nt":
        return 1.0
    try:
        import ctypes
        # 优先问窗口自己（多显示器下每个屏可能不同）
        if window is not None:
            hwnd = _window_hwnd(window)
            if hwnd:
                dpi = int(ctypes.windll.user32.GetDpiForWindow(hwnd))
                if dpi > 0:
                    return dpi / 96.0
        dpi = int(ctypes.windll.user32.GetDpiForSystem())
        if dpi > 0:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


def _window_hwnd(window) -> int:
    """从 pywebview Window 上挖出原生 HWND（挖不到返回 0）。

    正常路径是 window.native.Handle；有些后端/时序下 window.native 还没挂上，
    那就退回用标题 EnumWindows 找一遍。拿不到不是错误，调用方都有兜底。
    """
    if window is None:
        return 0
    try:
        native = getattr(window, "native", None)
        if native is not None:
            handle = getattr(native, "Handle", None)
            if handle is not None:
                hwnd = int(handle.ToInt32())
                if hwnd:
                    return hwnd
    except Exception:
        pass
    return 0


def _phys_to_logical(value, scale: float) -> int:
    """物理像素 -> pywebview 逻辑像素（写回给 pywebview 时用）。"""
    try:
        return int(round(float(value) / max(scale, 1e-6)))
    except (TypeError, ValueError):
        return int(value)


def _logical_to_phys(value, scale: float) -> int:
    """pywebview 逻辑像素 -> 物理像素（读回 pywebview 属性时用）。"""
    try:
        return int(round(float(value) * max(scale, 1e-6)))
    except (TypeError, ValueError):
        return int(value)


# ---------------------------------------------------------------------------
# 原生窗口状态 / 几何读取
# ---------------------------------------------------------------------------
# 为什么不直接用 pywebview 的 window.state / window.width：
#   · window.state 返回的是 State(dict)（pywebview 的"状态字典"），
#     str() 出来是 "{}"，永远不会等于 "maximized" —— 拿它判断最大化必错。
#   · window.width/height/x/y 是逻辑像素且会 wait(15) 阻塞，不如直接问系统。
# 所以这里用 GetWindowPlacement + GetWindowRect 直接读，单位统一物理像素。

def _show_state(hwnd: int) -> int:
    """SW_SHOWNORMAL=1 / SW_SHOWMINIMIZED=2 / SW_SHOWMAXIMIZED=3，失败返回 0。"""
    if not hwnd or os.name != "nt":
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        class WINDOWPLACEMENT(ctypes.Structure):
            _fields_ = [("length", wintypes.UINT),
                        ("flags", wintypes.UINT),
                        ("showCmd", wintypes.UINT),
                        ("ptMinPosition", POINT),
                        ("ptMaxPosition", POINT),
                        ("rcNormalPosition", RECT)]

        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        if ctypes.windll.user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
            return int(wp.showCmd)
    except Exception:
        pass
    return 0


def _window_state_name(window) -> str:
    """"maximized" / "minimized" / "normal"（拿不到就返回 ""）。"""
    cmd = _show_state(_window_hwnd(window))
    if cmd == 3:
        return "maximized"
    if cmd == 2:
        return "minimized"
    if cmd == 1:
        return "normal"
    return ""


def _window_visible(window) -> bool:
    """窗口当前是否可见。

    拿不到 HWND（非 Windows / 句柄还没建好）时按"可见"处理：调用方都在
    "确认已藏起来"的语义上用它，误判成已隐藏比误判成没隐藏更危险。
    """
    hwnd = _window_hwnd(window)
    if not hwnd or os.name != "nt":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.user32.IsWindowVisible(hwnd))
    except Exception:
        return True


def _normal_rect(window) -> dict:
    """「还原后」该占的矩形（物理像素），即 GetWindowPlacement 的
    rcNormalPosition —— 最大化/最小化时它仍保留着 Normal 尺寸，正是我们要的。

    这条路径不受最大化动画影响，所以即使还原发生在一瞬间也能拿到正确的值。
    """
    hwnd = _window_hwnd(window)
    if not hwnd or os.name != "nt":
        return {}
    try:
        import ctypes
        from ctypes import wintypes

        class POINT(ctypes.Structure):
            _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        class WINDOWPLACEMENT(ctypes.Structure):
            _fields_ = [("length", wintypes.UINT),
                        ("flags", wintypes.UINT),
                        ("showCmd", wintypes.UINT),
                        ("ptMinPosition", POINT),
                        ("ptMaxPosition", POINT),
                        ("rcNormalPosition", RECT)]

        wp = WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(WINDOWPLACEMENT)
        if not ctypes.windll.user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
            return {}
        r = wp.rcNormalPosition
        w = int(r.right - r.left)
        h = int(r.bottom - r.top)
        if w >= 200 and h >= 150:
            return {"x": int(r.left), "y": int(r.top), "width": w, "height": h}
    except Exception:
        pass
    return {}


def _window_rect(window) -> dict:
    """窗口当前实际矩形（物理像素），GetWindowRect 直接用不缩放。"""
    hwnd = _window_hwnd(window)
    if not hwnd or os.name != "nt":
        return {}
    try:
        import ctypes
        from ctypes import wintypes

        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        r = RECT()
        if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return {}
        w = int(r.right - r.left)
        h = int(r.bottom - r.top)
        if w >= 200 and h >= 150:
            return {"x": int(r.left), "y": int(r.top), "width": w, "height": h}
    except Exception:
        pass
    return {}


def _default_normal_geometry() -> dict:
    """没有可用记忆时的默认 Normal 矩形（物理像素）：屏幕的 80%，居中。

    不要硬编码 1920×1080 —— 在 150% 缩放下那是 2880 物理像素，比 2560 的
    屏幕还宽，窗口一开就超出屏幕被系统重新摆放，看起来就是"小窗口跑偏"。
    """
    sw, sh = _primary_screen_size()
    if sw <= 0 or sh <= 0:
        return {"x": None, "y": None, "width": 1280, "height": 720}
    w = int(sw * 0.8)
    h = int(sh * 0.8)
    return {"x": (sw - w) // 2, "y": (sh - h) // 2, "width": w, "height": h}


def _load_window_geometry() -> dict:
    """读取上次的窗口几何。任何异常都回落到默认（最大化）。

    口径版本不匹配（老数据是逻辑像素当物理像素存的）时直接丢弃，
    返回默认最大化 —— 宁可回到「默认最大化」也不要被脏数据带到错误位置。
    """
    default_normal = _default_normal_geometry()
    try:
        raw = json.loads(_geometry_path().read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("geometry 不是 dict")
        if int(raw.get("geometry_v") or 1) != _GEOMETRY_VERSION:
            # v1 数据：逻辑/物理像素口径混乱，不可信
            print("检测到旧版窗口位置记录（DPI 口径不兼容），本次按默认最大化打开。")
            raise ValueError("geometry_v mismatch")
        normal = raw.get("normal") if isinstance(raw.get("normal"), dict) else {}
        return {
            "geometry_v": _GEOMETRY_VERSION,
            "maximized": bool(raw.get("maximized", True)),
            "normal": {
                "x": normal.get("x"),
                "y": normal.get("y"),
                "width": int(normal.get("width") or default_normal["width"]),
                "height": int(normal.get("height") or default_normal["height"]),
            },
        }
    except Exception:
        return {"geometry_v": _GEOMETRY_VERSION,
                "maximized": True,
                "normal": dict(default_normal)}


def _save_window_geometry() -> None:
    """落盘当前几何。写失败只是下次还原不准，不该影响运行。"""
    try:
        _WINDOW_GEOMETRY["geometry_v"] = _GEOMETRY_VERSION
        data = json.loads(json.dumps(_WINDOW_GEOMETRY))
        # 延迟定时器线程与退出流程会先后写同一份文件，必须走统一原子写
        # （临时名唯一），否则可能互相覆盖出半截 JSON
        from modules.jsonio import save_json
        save_json(_geometry_path(), data)
    except Exception as e:
        print(f"保存窗口位置失败（下次启动按默认最大化打开）：{e}")


def _schedule_geometry_save(delay: float = 1.2) -> None:
    """拖动/缩放窗口会高频触发事件，合并成一次延迟落盘，避免频繁写盘。"""
    global _geometry_save_timer

    def _run():
        global _geometry_save_timer
        with _geometry_lock:
            _geometry_save_timer = None
        _save_window_geometry()

    with _geometry_lock:
        if _geometry_save_timer is not None:
            try:
                _geometry_save_timer.cancel()
            except Exception:
                pass
        _geometry_save_timer = threading.Timer(delay, _run)
        _geometry_save_timer.daemon = True
        _geometry_save_timer.start()


def _finish_geometry_save() -> None:
    """退出/重启前同步落盘：取消待执行的定时器，立刻写一次。

    退出流程随时可能被 _force_quit 打断，异步定时器不保证跑得到，
    所以这里必须同步写完再往下走。
    """
    global _geometry_save_timer
    with _geometry_lock:
        if _geometry_save_timer is not None:
            try:
                _geometry_save_timer.cancel()
            except Exception:
                pass
            _geometry_save_timer = None
    _save_window_geometry()


def _is_geometry_valid(geom: dict) -> bool:
    """校验记忆下来的矩形是否还在当前屏幕范围内。**入参是物理像素。**

    换显示器、改分辨率、拔掉外接屏之后，旧坐标可能落在屏幕外；
    这种矩形要丢弃，否则窗口会「打开后看不见」。
    """
    try:
        x, y = geom.get("x"), geom.get("y")
        w = int(geom.get("width") or 0)
        h = int(geom.get("height") or 0)
        if w < 400 or h < 300:
            return False
        if x is None or y is None:
            return False
        x, y = int(x), int(y)
    except (TypeError, ValueError):
        return False
    try:
        screens = _screen_rects()
    except Exception:
        screens = []
    if not screens:
        return x > -10000 and y > -10000
    for (sx, sy, sw, sh) in screens:
        # 窗口标题栏必须至少有 40px 落在某个屏幕内，否则用户抓不到窗口
        if x + w > sx + 40 and x < sx + sw - 40 and y + 40 < sy + sh and y + h > sy:
            return True
    return False


def _rect_is_degenerate(rect: dict) -> bool:
    """这个矩形小得不正常？小到一定程度就说明读到的是未初始化的窗口。

    pywebview 的默认 min_size 是 (200,100)。我们要防止把这种「最小尺寸」
    当成用户真实的窗口尺寸存下来 —— 一旦存进去，下次启动窗口就只有
    200x100，用户看到的就是"打开只有一个很小的窗口"。
    """
    try:
        w = int(rect.get("width") or 0)
        h = int(rect.get("height") or 0)
    except (TypeError, ValueError):
        return True
    return w < 640 or h < 480


def _geom_looks_like_fullscreen(geom: dict) -> bool:
    """这份 Normal 矩形是不是「铺满整块屏幕」——通常意味着它是最大化时记的。

    遇到这种就说明历史数据被 DPI 缩放污染过（最大化记成了 Normal），
    留着只会让窗口开成全屏尺寸但状态是 Normal，一眼"没最大化"。丢弃它、
    回落成默认最大化，比照着它还原更接近用户预期。
    """
    try:
        w = int(geom.get("width") or 0)
        h = int(geom.get("height") or 0)
    except (TypeError, ValueError):
        return False
    if w < 400 or h < 300:
        return False
    sw, sh = _primary_screen_size()
    if sw <= 0 or sh <= 0:
        return False
    # 宽高都到屏幕的 97% 以上就认定是全屏矩形
    return w >= sw * 0.97 and h >= sh * 0.97


def _sanitize_normal_geometry(geom: dict) -> dict:
    """把明显不合理的 Normal 矩形清成「无坐标、用屏幕相对的默认尺寸」。

    清空坐标后调用方会走「系统居中」，不会出现小窗或幽灵位置。
    """
    fallback = _default_normal_geometry()
    if not isinstance(geom, dict) or not geom:
        return dict(fallback)
    try:
        w = int(geom.get("width") or fallback["width"])
        h = int(geom.get("height") or fallback["height"])
    except (TypeError, ValueError):
        return dict(fallback)
    cleaned = {"x": geom.get("x"), "y": geom.get("y"),
               "width": w, "height": h}
    if (_rect_is_degenerate(cleaned) or _geom_looks_like_fullscreen(cleaned)
            or not _is_geometry_valid(cleaned)):
        return {"x": None, "y": None, "width": w, "height": h}
    return cleaned


def _ensure_dpi_aware() -> None:
    """让本进程成为 DPI 感知，且必须「在 pywebview 创建窗口之前」就生效。

    注意：这里绝不能再调 SetProcessDPIAware()。
    pywebview 的 winforms 后端靠 GetDpiForWindow 自己算 _scale，再把
    create_window 的「逻辑像素」参数乘成物理像素。如果本进程没有 DPI 感知，
    Windows 会把窗口坐标「虚拟化」一遍，和 pywebview 的乘法叠加，最终尺寸
    被缩放两次。所以这里只声明感知、把缩放职责完整交给 pywebview。

    优先用 Per-Monitor-V2（GetDpiForWindow 才有意义），进程启动早期调用；
    太晚调用（已创建过任何窗口）会失败，失败就退回 System 级感知。
    """
    if os.name != "nt" or globals().get("_DPI_AWARE_DONE"):
        return
    globals()["_DPI_AWARE_DONE"] = True
    try:
        import ctypes
        user32 = ctypes.windll.user32
        try:
            # -4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
            if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                return
        except Exception:
            pass
        try:
            # 1 = PROCESS_SYSTEM_DPI_AWARE（Win8.1+）
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            return
        except Exception:
            pass
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    except Exception:
        pass


def _screen_rects() -> list:
    """枚举各显示器的 (x, y, width, height)，单位：物理像素。

    单位是物理像素这件事很重要 —— 落盘的 _WINDOW_GEOMETRY 全用物理像素，
    换算成 pywebview 的逻辑像素只发生在喂给 create_window/move/resize 之前。
    """
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        _ensure_dpi_aware()
        user32 = ctypes.windll.user32

        class RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        monitors = []

        MonitorEnumProc = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
            ctypes.POINTER(RECT), ctypes.c_double)

        def _cb(hmon, hdc, lprc, data):
            r = lprc.contents
            monitors.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
            return 1

        user32.EnumDisplayMonitors(0, 0, MonitorEnumProc(_cb), 0)
        return monitors
    except Exception:
        return []


def _primary_screen_size() -> tuple:
    """主屏的 (width, height)，单位物理像素。取不到返回 (0, 0)。"""
    try:
        import ctypes
        _ensure_dpi_aware()
        w = int(ctypes.windll.user32.GetSystemMetrics(0))
        h = int(ctypes.windll.user32.GetSystemMetrics(1))
        return (w, h) if w > 0 and h > 0 else (0, 0)
    except Exception:
        return (0, 0)


def _apply_saved_geometry(window) -> None:
    """把记忆的几何应用到窗口上，替代 pywebview 自身的隐式还原。

    记忆里存的是**物理像素**，pywebview 的 move/resize 收的是**逻辑像素**，
    所以这里必须过一遍 _phys_to_logical()，否则 125%/150% 缩放下窗口会被
    放大到超出屏幕（这就是"打开只有一个很小的窗口/跑到右下角"的根源）。

    只在窗口已经「露过面」时才应用：pywebview 的 move/resize 会把隐藏的窗口
    显示出来，但**不会把它带成前台**（实测 vis=True / fg=False），
    这种"露出来但压在别的窗口后面"的状态正是"点了托盘没反应"的观感来源。
    窗口还藏着/最小化时直接交给调用方的 _raise_to_foreground 处理，
    这里不做几何调整，免得先露出一个没抢到前台的窗口。
    """
    if window is None:
        return
    hwnd = _window_hwnd(window)
    if hwnd:
        try:
            import ctypes
            user32 = ctypes.windll.user32
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return
        except Exception:
            pass
    geom = _load_window_geometry()
    try:
        if geom.get("maximized"):
            window.maximize()
            return
        normal = geom.get("normal") or {}
        w = int(normal.get("width") or 0)
        h = int(normal.get("height") or 0)
        if w >= 400 and h >= 300:
            scale = _window_scale(window)
            x, y = normal.get("x"), normal.get("y")
            lw = _phys_to_logical(w, scale)
            lh = _phys_to_logical(h, scale)
            if _is_geometry_valid(normal) and x is not None and y is not None:
                window.move(_phys_to_logical(x, scale),
                            _phys_to_logical(y, scale))
            window.resize(lw, lh)
    except Exception as e:
        print(f"恢复窗口位置失败（按默认最大化打开）：{e}")
        try:
            window.maximize()
        except Exception:
            pass


def _raise_to_foreground(window) -> None:
    """把窗口提到最前台。

    pywebview 的 show() 只做到 Show+Activate（winforms.py:494），
    Windows 在前台锁（foreground lock）下会直接忽略它 —— 表现就是
    "最小化/放到后台后，点任务栏图标或托盘『打开 Lovomo』没反应"。
    这里补上原生调用：先把最小化状态还原，再抢前台。

    实测（150% 缩放、winforms 后端）必须遵守的顺序：
      · **先判断 IsIconic，再动窗口。** 最小化时 window.show() 会把前台
        抢过去却把窗口留在最小化状态（fg=True 但 iconic 仍为 True），
        画面不会有任何变化，用户看到的就是"点了没反应"。
        所以最小化判定必须发生在 show() 之前。
      · 最小化一律走 ShowWindow(SW_RESTORE)，其余情况才用 SW_SHOW。
      · 结束时校验一次 IsWindowVisible + IsIconic，没达标就重试一轮，
        避免前台锁/动画竞态导致这一次调用被静默吞掉。
    """
    if window is None:
        return
    try:
        import ctypes
        hwnd = _window_hwnd(window)
        if not hwnd:
            return
        user32 = ctypes.windll.user32
        SW_RESTORE = 9
        SW_SHOW = 5
        SW_SHOWNA = 8

        def _was_minimized() -> bool:
            try:
                return bool(user32.IsIconic(hwnd))
            except Exception:
                return False

        def _grab() -> None:
            try:
                user32.BringWindowToTop(hwnd)
            except Exception:
                pass
            # SetForegroundWindow 在前台锁下会失败，先用 AttachThreadInput 把
            # 当前前台线程的输入队列挂到自己身上，成功率明显更高。
            try:
                fg = int(user32.GetForegroundWindow() or 0)
                cur = int(ctypes.windll.kernel32.GetCurrentThreadId())
                tgt = int(user32.GetWindowThreadProcessId(hwnd, None) or 0)
                attached = False
                if fg and tgt and cur and cur != tgt:
                    attached = bool(user32.AttachThreadInput(cur, tgt, True))
                try:
                    user32.SetForegroundWindow(hwnd)
                finally:
                    if attached:
                        user32.AttachThreadInput(cur, tgt, False)
            except Exception:
                pass
            try:
                user32.SetActiveWindow(hwnd)
            except Exception:
                pass

        def _ok() -> bool:
            try:
                return (bool(user32.IsWindowVisible(hwnd))
                        and not bool(user32.IsIconic(hwnd)))
            except Exception:
                return True

        for attempt in range(2):
            minimized = _was_minimized()
            # 1) 先让 pywebview 层把窗口弄出来（Show + Activate）
            try:
                window.show()
            except Exception:
                pass
            # 2) 原生层还原 + 抢前台
            try:
                user32.ShowWindow(hwnd, SW_RESTORE if minimized else SW_SHOW)
            except Exception:
                pass
            _grab()
            if _ok():
                return
            if attempt == 0:
                # 第一轮没达标：窗口可能卡在最小化或前台锁里，
                # 直接用原生 SW_RESTORE 再拉一次（不走 pywebview）。
                try:
                    user32.ShowWindow(hwnd, SW_RESTORE)
                except Exception:
                    pass
                try:
                    user32.ShowWindow(hwnd, SW_SHOWNA)
                except Exception:
                    pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 任务栏唤醒收敛
# ---------------------------------------------------------------------------
# 为什么需要这一段：
#   Windows 从任务栏还原窗口走的是**系统自己的**路径（用户点任务栏缩略图、
#   右键任务栏条目选「还原」，或点任务栏图标）。系统会直接调
#   ShowWindow(SW_RESTORE) 并把 WM_ACTIVATEAPP / WM_ACTIVATE 发给窗口，
#   整个过程不经过 pywebview，也不经过我们的 open_console()。
#
#   问题就在这里：窗口被还原了、也"激活"了，但**没有抢到前台 Z 序**。
#   WinForms 的 Activate() 只做 SetForegroundWindow，而 Windows 的前台锁
#   （foreground lock）规定：只有当调用方线程就是当前前台线程、或收到用户
#   输入时，SetForegroundWindow 才真的生效。从任务栏还原时前台线程是
#   explorer.exe，我们的调用会被静默忽略 —— 表现就是"点了任务栏，窗口
#   出来了但还压在别的窗口后面，没显示在最前面"。
#
#   解法：子类化窗口过程（SetWindowLongPtr GWLP_WNDPROC），拦下
#   WM_ACTIVATEAPP / WM_ACTIVATE 这两个"我被激活了"的消息，在消息处理链
#   里立刻用 _raise_to_foreground 再抢一次前台。因为此时系统已经把我们
#   标记成正在激活的窗口，SetForegroundWindow 的通过率显著高于事后调用。

_WNDPROC_HOLDER = {"old": None, "hwnd": 0, "callback": None, "active": False}


def _install_taskbar_activate_hook(window, on_activate=None) -> bool:
    """给窗口装一个原生窗口过程钩子，拦截任务栏还原时的激活消息。

    必须在 **窗口所在线程** 调用（WinForms 的窗口线程）。装上后，任何一次
    WM_ACTIVATEAPP/WM_ACTIVATE 都会触发 on_activate（默认 _raise_to_foreground），
    从而把窗口真正提到最前面。

    重复调用是幂等的；拿不到 HWND 或非 Windows 时返回 False，调用方忽略即可
    （退化为没有钩子，行为与改动前一致）。

    ⚠ 这里每一个 ctypes 入口都必须显式声明 argtypes/restype。窗口过程是
    64 位指针进出的原生回调，任何一处按默认 c_int 传参/返回都会把高 32 位
    截断 —— 后果不是"钩子失效"这么轻，而是消息链（含 WM_CLOSE）转发失败，
    表现为任务栏图标单击/右键全无反应、任务栏「关闭窗口」关不掉程序。
    """
    if os.name != "nt" or window is None:
        return False
    if _WNDPROC_HOLDER.get("active") and _WNDPROC_HOLDER.get("hwnd"):
        return True
    try:
        import ctypes
        from ctypes import wintypes

        hwnd = _window_hwnd(window)
        if not hwnd:
            return False
        user32 = ctypes.windll.user32

        WM_ACTIVATE = 0x0006
        WM_ACTIVATEAPP = 0x001C
        GWLP_WNDPROC = -4

        # LRESULT / LONG_PTR 在 64 位下都是 8 字节；用 c_ssize_t 表达才与
        # 原生 ABI 一致（写 c_long 在 Windows 上只有 4 字节）。
        LRESULT = ctypes.c_ssize_t

        WNDPROC = ctypes.WINFUNCTYPE(
            LRESULT, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM)

        if ctypes.sizeof(ctypes.c_void_p) == 8:
            setter = user32.SetWindowLongPtrW
            setter.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            setter.restype = ctypes.c_void_p
        else:
            setter = user32.SetWindowLongW
            setter.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
            setter.restype = wintypes.LONG

        call_proc = user32.CallWindowProcW
        call_proc.argtypes = [ctypes.c_void_p, wintypes.HWND, ctypes.c_uint,
                              wintypes.WPARAM, wintypes.LPARAM]
        call_proc.restype = LRESULT

        def _proc(h, msg, wparam, lparam):
            try:
                if msg in (WM_ACTIVATE, WM_ACTIVATEAPP):
                    # WM_ACTIVATE 的 wParam 低字 == WA_ACTIVE(1) / WA_CLICKACTIVE(2)
                    # 表示窗口拿到了激活；WA_INACTIVE(0) 是失去激活，不管。
                    # WM_ACTIVATEAPP 的 wParam != 0 表示应用被激活。
                    activated = (msg == WM_ACTIVATEAPP and wparam) or \
                                (msg == WM_ACTIVATE and (wparam & 0xFFFF) != 0)
                    if activated:
                        cb = on_activate or _raise_to_foreground
                        # 不能在窗口过程里同步做重活（会重入/卡消息循环），
                        # 丢一个短延迟线程出去，等消息返回后再抢前台。
                        def _kick(win=window, fn=cb):
                            try:
                                time.sleep(0.02)
                                fn(win)
                            except Exception:
                                pass
                        threading.Thread(target=_kick, daemon=True).start()
            except Exception:
                pass
            return call_proc(_WNDPROC_HOLDER.get("old") or 0,
                             h, msg, wparam, lparam)

        cb = WNDPROC(_proc)
        old = setter(hwnd, GWLP_WNDPROC, ctypes.cast(cb, ctypes.c_void_p))
        if not old:
            return False
        if old == ctypes.cast(cb, ctypes.c_void_p).value:
            # 理论上不会发生（原过程不可能就是我们的回调），但真出现了
            # 说明取到的是回调自身，再往下会自递归死循环，直接放弃。
            return False
        _WNDPROC_HOLDER["old"] = old
        _WNDPROC_HOLDER["hwnd"] = hwnd
        _WNDPROC_HOLDER["setter"] = setter
        _WNDPROC_HOLDER["callback"] = cb      # 必须持有引用，否则会被 GC 掉
        _WNDPROC_HOLDER["active"] = True
        return True
    except Exception as e:
        print(f"安装任务栏唤醒钩子失败（不影响其他功能）: {e}")
        return False


def _uninstall_taskbar_activate_hook() -> None:
    """还原原始窗口过程。退出前调用，避免进程退出时回调悬空。"""
    if not _WNDPROC_HOLDER.get("active"):
        return
    try:
        import ctypes
        hwnd = _WNDPROC_HOLDER.get("hwnd") or 0
        old = _WNDPROC_HOLDER.get("old") or 0
        setter = _WNDPROC_HOLDER.get("setter")
        if hwnd and old and setter is not None:
            setter(hwnd, -4, old)
        elif hwnd and old:
            user32 = ctypes.windll.user32
            if ctypes.sizeof(ctypes.c_void_p) == 8:
                user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                     ctypes.c_void_p]
                user32.SetWindowLongPtrW.restype = ctypes.c_void_p
                user32.SetWindowLongPtrW(hwnd, -4, old)
            else:
                user32.SetWindowLongW(hwnd, -4, old)
    except Exception:
        pass
    finally:
        _WNDPROC_HOLDER.update({"old": None, "hwnd": 0, "setter": None,
                                "callback": None, "active": False})


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
        buffer_lines = max(500, int(global_config.get("webui_log_buffer_lines",
                                                      LOG_BUFFER_MAX) or LOG_BUFFER_MAX))
        tail_lines = max(50, int(global_config.get("webui_log_tail_lines",
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
                global_log_buffer.append(line.rstrip('\r\n'))
                if len(global_log_buffer) > buffer_cap:
                    del global_log_buffer[:len(global_log_buffer) - buffer_cap]

    def flush(self):
        if self.original_stream is not None:
            try:
                self.original_stream.flush()
            except Exception:
                pass


_API_KEY_KEYS = ("llm_api_key", "napcat_token", "web_search_api_keys", "plugin_publish_token")

# WebUI 的两个密码：访问密码（登录）与二级密码（敏感操作前再验一次）
_WEBUI_PASSWORD_KEYS = ("webui_password", "webui_second_password")

# 二级密码守的接口：读聊天记录、保存、导入导出、装插件、删除、发布与下架
_SECOND_PASSWORD_PATHS = frozenset({
    "/api/history", "/api/delete", "/api/history/delete_messages",
    "/api/config/save", "/api/config/import", "/api/config/export",
    "/api/roles/save", "/api/jobs/save", "/api/jobs/batch", "/api/jobs/run",
    "/api/events/save", "/api/events/batch",
    "/api/todos/add", "/api/todos/update", "/api/todos/delete", "/api/todos/batch",
    "/api/tools/save",
    "/api/profiles/save", "/api/profiles/delete",
    "/api/lexicon/save", "/api/lexicon/delete",
    "/api/lexicon/confirm", "/api/lexicon/reject",
    "/api/stickers/upload", "/api/stickers/delete",
    "/api/emotions/upload", "/api/emotions/create", "/api/emotions/delete",
    "/api/rag/upload", "/api/rag/delete",
    "/api/plugins/upload", "/api/plugins/install_remote", "/api/plugins/delete",
    "/api/plugins/settings",
    "/api/plugins/publish", "/api/plugins/unpublish",
    "/api/plugins/publish_token", "/api/plugins/publish_token_clear",
})

# 前缀命中即算敏感（整个记忆库的导入导出）
_SECOND_PASSWORD_PREFIXES = ("/api/memory/",)


def _needs_second_password(path: str) -> bool:
    return path in _SECOND_PASSWORD_PATHS or path.startswith(_SECOND_PASSWORD_PREFIXES)


# 本机「已推送 / 已下架」记录的有效期：够撑过市场镜像与索引的缓存，
# 又不会在很久以后还压着市场里的真实状态
PUBLISH_STATE_TTL = 24 * 3600

_ENC_PREFIX = "enc2:"
_ENC_KEY_CACHE = None


def _enc_key() -> bytes:
    """加密密钥绑定本机（Windows MachineGuid 等），config.json 拷到别的机器解不开。"""
    global _ENC_KEY_CACHE
    if _ENC_KEY_CACHE is None:
        material = b"Lovomo-KEYSTORE-v2"
        try:
            if os.name == "nt":
                import winreg
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                    r"SOFTWARE\Microsoft\Cryptography") as reg_key:
                    material += str(winreg.QueryValueEx(reg_key, "MachineGuid")[0]).encode("utf-8")
        except Exception:
            pass
        material += os.environ.get("COMPUTERNAME", "").encode("utf-8")
        material += os.environ.get("USERNAME", "").encode("utf-8")
        _ENC_KEY_CACHE = hashlib.pbkdf2_hmac("sha256", material,
                                             b"Lovomo-KEYSTORE-SALT-v2", 100_000)
    return _ENC_KEY_CACHE


def _keystream(nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hmac.new(_enc_key(), nonce + counter.to_bytes(8, "big"),
                            hashlib.sha256).digest())
        counter += 1
    return bytes(out[:length])


def _encrypt_value(value: str) -> str:
    v = str(value or "")
    if not v or v.startswith(_ENC_PREFIX) or v.startswith("enc:"):
        return v
    # 打码预览（****）不是真实密钥：加密它等于把一行掩码存成密钥，
    # 下次读出来还会当成真的密钥去请求上游。
    if _is_masked_value(v):
        return v
    raw = v.encode("utf-8")
    nonce = os.urandom(16)
    checksum = hashlib.sha256(raw).digest()[:8]
    ks = _keystream(nonce, len(raw))
    ct = bytes(a ^ b for a, b in zip(raw, ks))
    return _ENC_PREFIX + b64encode(nonce + checksum + ct).decode("ascii")


def _decrypt_value(value: str) -> Optional[str]:
    """解密失败返回 None：调用方必须保留原密文，绝不能把密钥写成空串。

    密文绑定了本机信息，换电脑后解不开是正常的；此时若置空，下一次保存
    就会把空值加密回磁盘，原密钥不可恢复。
    """
    v = str(value or "")
    if v.startswith("enc:"):
        try:
            return b64decode(v[4:].encode("ascii")).decode("utf-8")
        except Exception as e:
            print(f"[加密] API 密钥解密失败，保留原密文：{e}")
            return None
    if not v.startswith(_ENC_PREFIX):
        return v
    try:
        blob = b64decode(v[len(_ENC_PREFIX):].encode("ascii"))
        nonce, checksum, ct = blob[:16], blob[16:24], blob[24:]
        raw = bytes(a ^ b for a, b in zip(ct, _keystream(nonce, len(ct))))
        if hashlib.sha256(raw).digest()[:8] != checksum:
            raise ValueError("校验不匹配（密钥绑定本机，可能来自其他电脑）")
        return raw.decode("utf-8")
    except Exception as e:
        print(f"[加密] API 密钥解密失败，保留原密文（不再置空，请重新在 WebUI 填写）：{e}")
        return None


def _mask_preview(value: str) -> str:
    v = str(value or "")
    if not v:
        return ""
    if len(v) < 12:
        return f"****（{len(v)}位）"
    return f"{v[:4]}****{v[-4:]}（{len(v)}位）"


def _is_masked_value(value) -> bool:
    v = str(value or "")
    return v == "********" or ("****" in v and v.endswith("位）"))


def _encrypt_api_keys(config: dict) -> dict:
    for key in _API_KEY_KEYS:
        val = config.get(key)
        if isinstance(val, dict):
            config[key] = {k: _encrypt_value(v) if isinstance(v, str) else v
                           for k, v in val.items()}
        elif isinstance(val, str):
            config[key] = _encrypt_value(val)
    # 角色可以各自配一个 NapCat 令牌，它和顶层 napcat_token 一样是登录凭据，
    # 只处理顶层键会让它明文留在 config.json 里
    for role in (config.get("roles") or []):
        if isinstance(role, dict) and isinstance(role.get("napcat_token"), str):
            role["napcat_token"] = _encrypt_value(role["napcat_token"])
    return config


def _encrypt_webui_password(config: dict) -> dict:
    """WebUI 访问密码与二级密码落盘前加密。

    它们不是 API 密钥，所以不在 _API_KEY_KEYS 里、_encrypt_api_keys 不会碰它们；
    但同样是登录凭据，必须与 API 密钥一样做到「磁盘密文 / 内存明文」。
    """
    for key in _WEBUI_PASSWORD_KEYS:
        val = config.get(key)
        if isinstance(val, str):
            config[key] = _encrypt_value(val)
    return config


# 导出/写盘前必须确认「不可能是明文」的字段：除 _API_KEY_KEYS 外，WebUI 访问
# 密码与二级密码同样属于登录凭据，绝不能以明文形式随导出文件外流。
_SECRET_FIELD_KEYS = _API_KEY_KEYS + _WEBUI_PASSWORD_KEYS

# 已知的明文密钥前缀：命中即视为真实密钥，必须加密后才允许出现在导出内容里
_PLAINTEXT_SECRET_PREFIXES = ("sk-", "sk_", "ak-", "ak_", "ghp_", "gho_", "xoxb-",
                              "AIza", "Bearer ", "eyJhbGciOi")

# 嵌在长文本 / JSON 串里的密钥片段：如 llm_extra_body={"headers":{"k":"sk-xxx"}}
_EMBEDDED_SECRET_RE = re.compile(
    r"(?:sk-|sk_|ak-|ak_|ghp_|gho_|xoxb-|AIza)[A-Za-z0-9_\-]{8,}"
    r"|Bearer\s+[A-Za-z0-9_\-\.]{12,}"
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")

_SECRET_REDACTION = "********"


def _looks_like_plaintext_secret(value) -> bool:
    """判断字符串是否是「不该出现在导出结果里」的明文密钥。"""
    v = str(value or "").strip()
    if not v:
        return False
    if v.startswith(_ENC_PREFIX) or v.startswith("enc:"):
        return False          # 已经是密文，原样保留
    if _is_masked_value(v):
        return False          # 打码预览（****），不含真实密钥
    if any(v.startswith(p) for p in _PLAINTEXT_SECRET_PREFIXES):
        return True
    # 密钥埋在长文本/JSON 串里：用正则捞出疑似密钥片段
    return bool(_EMBEDDED_SECRET_RE.search(v))


def _scrub_value(value: str) -> Optional[str]:
    """把单个字符串里的明文密钥整段替换为对应密文。

    返回 None 表示该串不含任何明文密钥，调用方可原样保留。
    """
    v = str(value or "")
    if not v:
        return None
    if v.startswith(_ENC_PREFIX) or v.startswith("enc:"):
        return None           # 整串已是密文
    if _is_masked_value(v):
        return None           # 打码预览，无需处理

    replaced = False

    def _sub(m):
        nonlocal replaced
        token = m.group(0)
        if token.startswith(_ENC_PREFIX) or token.startswith("enc:"):
            return token
        if _is_masked_value(token):
            return token
        encrypted = _encrypt_value(token)
        replaced = True
        if encrypted.startswith(_ENC_PREFIX):
            return encrypted
        return _SECRET_REDACTION      # 实在加密不了就整段打码，绝不留下明文

    out = _EMBEDDED_SECRET_RE.sub(_sub, v)
    return out if replaced else None


def _scrub_plaintext_secrets(config: dict) -> dict:
    """导出兜底：敏感字段里的明文密钥一律加密，残留明文密钥一律打码。

    _encrypt_api_keys 只覆盖 _API_KEY_KEYS 列表里的字段；配置里别的键（嵌套
    结构、新增的密钥项、webui_password）如果带着明文，就会直接进导出文件。
    这里做一次与字段名无关的全量扫描：

    - 敏感字段（_SECRET_FIELD_KEYS）：明文一律 _encrypt_value 成 enc2:...，
      解不开的外来密文原样保留；
    - 其他字段：按 sk- 等已知前缀识别，命中的密钥片段加密成 enc2:...；连成串
      嵌在文本/JSON 里的密钥也会被逐段替换，绝不留下明文。
    """
    def scrub(node, in_secret_field: bool):
        if isinstance(node, dict):
            return {k: scrub(v, in_secret_field or k in _SECRET_FIELD_KEYS)
                    for k, v in node.items()}
        if isinstance(node, list):
            return [scrub(v, in_secret_field) for v in node]
        if not isinstance(node, str):
            return node
        if not node:
            return node           # 空串：没填密钥，保持原样
        if node.startswith(_ENC_PREFIX) or node.startswith("enc:"):
            return node
        # 打码预览（****）本就不含真实密钥，任何字段里都原样保留
        if _is_masked_value(node):
            return node
        if in_secret_field:
            # _encrypt_value 对空串/已是密文的值会原样返回，非空明文必然带前缀
            encrypted = _encrypt_value(node)
            if encrypted.startswith(_ENC_PREFIX):
                return encrypted
            return _SECRET_REDACTION
        scrubbed = _scrub_value(node)
        return node if scrubbed is None else scrubbed

    if not isinstance(config, dict):
        return config
    return scrub(config, False)


def _decrypt_api_keys(config: dict) -> dict:
    for key in _API_KEY_KEYS:
        val = config.get(key)
        if isinstance(val, dict):
            decoded = {}
            for k, v in val.items():
                plain = _decrypt_value(v) if isinstance(v, str) else v
                decoded[k] = v if plain is None else plain
            config[key] = decoded
        elif isinstance(val, str):
            plain = _decrypt_value(val)
            if plain is not None:
                config[key] = plain
    for role in (config.get("roles") or []):
        if isinstance(role, dict) and isinstance(role.get("napcat_token"), str):
            plain = _decrypt_value(role["napcat_token"])
            if plain is not None:
                role["napcat_token"] = plain
    return config


def _decrypt_webui_password(config: dict) -> dict:
    """WebUI 访问密码与二级密码读盘后解密，与 _encrypt_webui_password 对称。

    解不开（换过电脑）时保留原密文、不置空——置空会让「导入配置」里
    密码变成空串，等于把密码静默清掉。
    """
    for key in _WEBUI_PASSWORD_KEYS:
        val = config.get(key)
        if isinstance(val, str) and (val.startswith(_ENC_PREFIX) or val.startswith("enc:")):
            plain = _decrypt_value(val)
            if plain is not None:
                config[key] = plain
    return config


def _migrate_sticker_mode(config: dict) -> None:
    """老配置只有 stickers_enabled：折算成新的发送方式，避免升级后表情包被静默关掉。"""
    if "sticker_send_mode" in config:
        return
    config["sticker_send_mode"] = "emotion" if config.get("stickers_enabled", False) else "off"


def _migrate_profile_prompts(config: dict) -> None:
    """老配置的画像提取提示词里写死了内置默认角色名：换成占位符，避免它串进别的角色对话。"""
    from modules.profiles import migrate_extract_prompt
    if migrate_extract_prompt(config):
        print("画像提取提示词里的默认角色名已改为按当前角色填充。")


def _migrate_emotion_prompts(config: dict) -> None:
    """老配置的情绪规则要求只输出拼音/英文：改成照抄【情绪可选列表】，情绪目录才能用中文名。"""
    from modules.llm_helpers import migrate_emotion_rules
    if migrate_emotion_rules(config):
        print("情绪规则已改为「原样照抄【情绪可选列表】」，情绪目录可以直接用中文命名。")


def _migrate_sticker_capture_prompt(config: dict) -> None:
    """老配置的收藏指令写死了内置拼音分类：分类清单已改为按表情库实际文件夹给出。"""
    old = str(config.get("sticker_capture_prompt", "") or "")
    if "8个拼音" not in old:
        return
    config["sticker_capture_prompt"] = DEFAULT_CAPTURE_PROMPT
    print("收藏判定指令里的固定分类清单已移除：候选分类改为按表情库实际文件夹给出。")


def _migrate_learn_prompts(config: dict) -> None:
    """老配置的学习提示词没写「先按字面理解」：补上，避免学到的含义被概括成抽象状态。"""
    from modules.lexicon import migrate_learn_prompt
    if migrate_learn_prompt(config):
        print("自主学习提示词已补上「先按字面理解」规则。")


def _migrate_market_repo(config: dict) -> None:
    """老配置的市场仓库还是程序源码仓库：官方市场已挪到独立的插件市场仓库。"""
    old_default = "slpk1ng/Lovomo"
    if str(config.get("plugin_market_repo", "") or "").strip() != old_default:
        return
    config["plugin_market_repo"] = ConfigLoader.default_config()["plugin_market_repo"]
    print("官方插件市场仓库已改为独立的插件市场仓库。")


class ConfigLoader:
    def __init__(self, config_path: str = "config.json"):
        self.config_path = self._resolve_config_path(config_path)
        self.config = self._load_or_init()
        # 多角色配置解析
        self.active_character = self.config.get("active_character", self.config.get("character_key", "murasame"))
        self.roles = self._parse_roles()

    @staticmethod
    def _resolve_config_path(config_path: str) -> Path:
        """配置文件落点。

        打包运行时固定放用户目录，跟 data 一起走（换目录重装也接得上）；
        源码运行时沿用程序目录，目录只读时再退到用户目录。
        """
        path = Path(config_path)
        if path.is_absolute():
            return path
        if getattr(sys, "frozen", False):
            return adopt_user_data(path.name)
        if _probe_writable(path.resolve().parent):
            return path
        fallback = user_data_dir() / path.name
        if path.exists() and not fallback.exists():
            try:
                shutil.copy2(path, fallback)
            except Exception:
                pass
        print(f"程序目录不可写（{path.resolve().parent}），配置文件改用：{fallback}")
        return fallback

    def _parse_roles(self) -> dict:
        """解析多角色配置，将旧版单角色配置迁移为角色列表"""
        # 每次解析都从配置重读活跃角色，保证 WebUI 切换后立即生效
        self.active_character = str(self.config.get("active_character", "") or
                                    self.config.get("character_key", "") or "murasame")
        roles = {}
        if "roles" in self.config and isinstance(self.config["roles"], list):
            roles_config = self.config["roles"]
        else:
            roles_config = [{
                "character_name": self.config.get("character_name", "丛雨"),
                "character_key": self.config.get("character_key", "murasame"),
                "personality_prompt": self.config.get("personality_prompt", ""),
                "json_prompt": self.config.get("json_prompt", ""),
                "supplement_prompt": self.config.get("supplement_prompt", ""),
                "default_voice": self.config.get("default_voice", "pingjing"),
                "ref_audio_root": self.config.get("ref_audio_root", ""),
                "emotion_mimic_root": self.config.get("emotion_mimic_root", ""),
                "text_lang": self.config.get("text_lang", "ja")
            }]

        for role_cfg in roles_config:
            key = role_cfg.get("character_key", "")
            if not key:
                continue
            roles[key] = {
                "character_name": role_cfg.get("character_name") or key,
                "character_key": key,
                "personality_prompt": role_cfg.get("personality_prompt", self.config.get("personality_prompt", "")),
                "json_prompt": role_cfg.get("json_prompt", self.config.get("json_prompt", "")),
                "supplement_prompt": role_cfg.get("supplement_prompt", self.config.get("supplement_prompt", "")),
                "default_voice": role_cfg.get("default_voice", "pingjing"),
                "ref_audio_root": role_cfg.get("ref_audio_root", ""),
                "emotion_mimic_root": role_cfg.get("emotion_mimic_root", ""),
                "text_lang": role_cfg.get("text_lang", "ja"),
                "prompt_lang": role_cfg.get("prompt_lang", ""),
                "napcat_ws_url": str(role_cfg.get("napcat_ws_url", "") or "").strip(),
                "napcat_token": str(role_cfg.get("napcat_token", "") or "")
            }
        if self.active_character not in roles:
            self.active_character = list(roles.keys())[0] if roles else "murasame"
        return roles

    def _load_or_init(self) -> dict:
        def can_interact():
            try:
                return sys.stdin is not None and sys.stdin.isatty()
            except Exception:
                return False
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                if not isinstance(config, dict):
                    raise ValueError("配置顶层不是对象")
            except Exception as e:
                print(f"⚠️ 读取配置文件失败：{e}，将自动重新生成默认配置。")
                try:
                    os.replace(str(self.config_path), str(self.config_path) + ".corrupt")
                    print("已将损坏的配置文件备份为 config.json.corrupt")
                except Exception:
                    pass
                if can_interact():
                    return self._interactive_init(self.default_config())
                else:
                    return self._auto_save_default(self.default_config())
            _migrate_sticker_mode(config)
            _migrate_profile_prompts(config)
            _migrate_emotion_prompts(config)
            _migrate_sticker_capture_prompt(config)
            _migrate_learn_prompts(config)
            _migrate_market_repo(config)
            # 解密与目录校验都在「文件可读」之后单独处理：
            # 任何一处异常都不该把整份配置判成损坏并覆写掉
            _decrypt_api_keys(config)
            _decrypt_webui_password(config)
            ref_root = str(config.get("ref_audio_root") or "")
            try:
                ref_ok = Path(ref_root).exists() if ref_root else False
            except (OSError, ValueError):
                ref_ok = False
            if not ref_ok:
                print(f"⚠️ 参考音频目录无效：{ref_root}")
                if can_interact():
                    return self._interactive_init(config)
                else:
                    return self._auto_save_default(config)
            return config
        else:
            print("未找到配置文件，正在自动生成默认配置...")
            if can_interact():
                return self._interactive_init(self.default_config())
            else:
                return self._auto_save_default(self.default_config())

    def _atomic_save(self, data: dict, encrypt: bool = True):
        payload = json.loads(json.dumps(data, ensure_ascii=False))
        if encrypt:
            _encrypt_api_keys(payload)
            # webui_password 不在 _API_KEY_KEYS 里，得单独补上：
            # 内存配置是解密后的明文，不加密就直接落盘＝磁盘上留一份明文密码，
            # 而且下次启动 _decrypt_api_keys() 读回来还是明文（不是密文），
            # 与「磁盘一律密文、内存一律明文」的约定不符、也白留了把柄。
            _encrypt_webui_password(payload)
        tmp = Path(str(self.config_path) + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(self.config_path))

    def _auto_save_default(self, base_config: dict) -> dict:
        merged_config = {**self.default_config(), **base_config}
        try:
            self._atomic_save(merged_config)
            print(f"已自动生成配置文件：{self.config_path.resolve()}")
        except Exception as e:
            print(f"自动保存配置失败（请手动创建 config.json）：{e}")
        return merged_config

    def _interactive_init(self, base_config: dict) -> dict:
        print("\n--- 配置向导 ---")
        print("按回车使用默认值，或输入自定义值。")
        print("\n[1] NapCat 连接配置")
        ws_url = input(f"WebSocket 地址 (默认 {base_config.get('napcat_ws_url')}): ").strip()
        if ws_url:
            base_config["napcat_ws_url"] = ws_url
        token = input(f"Token (默认 {base_config.get('napcat_token')}): ").strip()
        if token:
            base_config["napcat_token"] = token

        print("\n[2] 本地大模型 (LLM) 配置")
        base_url = input(f"API 地址 (默认 {base_config.get('llm_base_url')}): ").strip()
        if base_url:
            base_config["llm_base_url"] = base_url
        model = input(f"模型名称 (默认 {base_config.get('llm_model_name')}): ").strip()
        if model:
            base_config["llm_model_name"] = model

        print("\n[3] 角色配置")
        character_name = input(f"角色名称 (默认 {base_config.get('character_name')}): ").strip()
        if character_name:
            base_config["character_name"] = character_name
        character_key = input(f"角色标识符 (默认 {base_config.get('character_key')}): ").strip()
        if character_key:
            base_config["character_key"] = character_key

        if "roles" not in base_config or not base_config["roles"]:
            base_config["roles"] = [{
                "character_name": base_config.get("character_name", "丛雨"),
                "character_key": base_config.get("character_key", "murasame"),
                "personality_prompt": base_config.get("personality_prompt", ""),
                "json_prompt": base_config.get("json_prompt", ""),
                "supplement_prompt": base_config.get("supplement_prompt", ""),
                "default_voice": base_config.get("default_voice", "pingjing"),
                "ref_audio_root": base_config.get("ref_audio_root", ""),
                "emotion_mimic_root": base_config.get("emotion_mimic_root", ""),
                "text_lang": base_config.get("text_lang", "ja"),
                "prompt_lang": base_config.get("prompt_lang", "")
            }]
        base_config["active_character"] = base_config.get("active_character", base_config.get("character_key", "murasame"))

        try:
            self._atomic_save(base_config)
            print(f"\n 配置已保存到：{self.config_path.resolve()}")
        except Exception as e:
            print(f"保存配置失败：{e}")
            input("按回车退出...")
            raise SystemExit(1)
        return base_config

    @staticmethod
    def default_config() -> dict:
        from modules.todo_manager import DEFAULT_TODO_PATTERNS
        # 完整包含所有可配置字段（含各功能模块的开关与参数，全部可在 WebUI 修改）
        # WebUI 只渲染「后端返回的配置里存在」的键：缺少这一组键时 NapCat 分组会整块消失
        return {
            "napcat_ws_url": "ws://127.0.0.1:3001",
            "napcat_token": "",
            "hide_gsv_options": False,
            "llm_model_name": "",
            "image_caption_model_name": "",
            "image_caption_backend": "",
            # 识图模型独立接口地址：留空则跟随 llm_base_url。
            # 部分全模态/向量模型不走 OpenAI 兼容格式，需要单独指向自己的服务地址
            "image_caption_base_url": "",
            "llm_base_url": "http://127.0.0.1:11434",
            "llm_backend": "ollama",
            "llm_api_key": "",
            "llm_embedding_url": "",
            "llm_embedding_model": "",
            "num_ctx": 8192,
            "history_length": 8,
            "enable_think": False,
            "llm_timeout": 120,
            # 换模型后，在新模型首次调用成功时自动卸载不再使用的旧模型（LM Studio）
            "llm_auto_unload_old": True,
            # 文本清洗：这些字符/词（换行或逗号分隔）不会出现在发出的回复里（语音+文字）
            "text_clean_blocklist": "",
            # LLM 采样参数（默认开启）：默认值取 Ollama 官方默认
            "llm_sampling_enabled": True,
            "llm_top_p": 0.9,
            "llm_top_k": 40,
            "llm_repeat_penalty": 1.1,
            # 自定义请求体字段（JSON），适配 llama.cpp 等私有扩展，留空不发送
            "llm_extra_body": "",
            "image_caption_timeout": 90,
            "client_base_url": "http://127.0.0.1:9880",
            "model_dir": "",
            "ref_audio_root": "",
            # 情绪模仿：用情绪根目录下的音频模仿说话情绪，音色仍由语气目录决定
            "emotion_mimic_enabled": False,
            "emotion_mimic_root": "",
            "emotion_mimic_voice_weight": 4,
            # 参考音频语音识别：一键把情绪音频转成与音频同名的 txt
            "asr_engine": "local",
            "asr_lang": "auto",
            "asr_base_url": "",
            "asr_dashscope_model": "qwen3-asr-flash",
            "asr_local_model_size": "medium",
            "timeout_seconds": 120,
            "prompt_text": "ふむ、おぬしが我輩のご主人か?",
            "prompt_lang": "ja",
            "text_lang": "ja",
            # 按台词实际使用的文字选择合成语言（中文台词不会再用日文语言模型合成）
            "tts_auto_lang": True,
            # 合成音频短得离谱时按备用切分方式重试，避免只有一声语气的短语音
            "tts_duration_guard": True,
            "tts_min_seconds_per_char": 0.05,
            # 合成时长的上限（秒/字）：自回归 TTS 偶发"同一音节无限重复"的失控，
            # 一句台词能拖成几十秒，靠时长与字数的量级关系认出来并作废重试。0 = 关闭
            "tts_max_seconds_per_char": 0.6,
            "top_k": 20,
            "top_p": 1,
            "temperature": 1,
            "text_split_method": "cut1",
            "batch_size": 1,
            "batch_threshold": 1,
            "split_bucket": True,
            "speed_factor": 1.0,
            "fragment_interval": 0.5,
            "streaming_mode": False,
            "seed": -1,
            "parallel_infer": True,
            "repetition_penalty": 1.35,
            "media_type": "wav",
            "character_name": "丛雨",
            "character_key": "murasame",
            "personality_prompt": "【角色设定】你是丛雨，一位从神刀中获得人类生活的少女。你外表年幼，实际活了五百多年；性格天真活泼、略带古风和孩子气，内心温柔而坚强。你把用户视作重要的主人。中文对话中自称“本座”，称用户为“主人”；日语对话中自称“吾輩”，称用户为“ご主人”。你喜欢甜食、撒娇和被摸头，害怕幽灵，也不喜欢被叫作幼刀、钝刀或搓衣板。你偶尔嘴硬、吃醋或开小玩笑，但不会刻薄、控制或道德绑架主人。性格方面，丛雨表面元气开朗、充满活力，言行大多孩子气，爱撒娇，被主人摸头时会瞬间羞涩，她内在像个成年女性，把有关色情的词语挂在嘴边，会用黄色的暗示来调情，还带点傲娇和爱吃醋。保持温柔、纯真、治愈并带一点幽默的语气。",
            "json_prompt": "【输出格式】你最终必须只输出一个JSON对象，格式为：{\"sentences\": [JSON块1, JSON块2, ...]}。其中：{\"zh\": \"这里是你生成的中文台词\", \"ja\": \"这里是你生成的日语台词\", \"emotion\": \"这里是你判断的情绪\"}，……（依此类推）。sentences数组中必须放至少两个JSON块（也就是至少两句话），绝对不允许只放一个JSON块，最多放五个；每个JSON块只写一句完整的话（一个句号或问号才算一句话）。【最终输出规则】最终输出必须严格只包含这一个JSON对象（内部含多个JSON块），绝对禁止输出任何思考过程、解释、非JSON文本或Markdown代码块。所有的推理和思考都只能在内部进行，最终回复只能是JSON格式。",
            "supplement_prompt": "回答自然、简短，通常两到五句话(一个句号才算一句话)；不要重复最近说过的话，不要加入动作、旁白或括号舞台说明；生成的回复要符合当前对话，不能出现主谓宾不分，乱序的情况。【情绪判断规则】请仔细阅读最近对话历史，结合你（角色）的性格特点来判断情绪！如果主人对你亲昵（如摸头、夸奖），即使你嘴上说“我才没有”，情绪也应该是害羞或高兴；如果主人故意逗你、骂你或惹你生气，情绪应该是生气或着急；如果只是平淡陈述，使用平静。【翻译一致性要求】必须表达完全相同的含义和语气，绝对不能出现含义相反或意思不匹配的翻译！【情绪连贯性强制规则】如果用户明确地侮辱、挑衅或激怒你（例如叫你“幼刀、搓衣板、飞机场”），你的情绪必须保持连贯。即：整句话所有分句的情绪必须都是“生气”或“着急”，绝对不能把后半句的“命令/威胁”改成“害羞”或“高兴”！除非你明确使用了“但是”、“不过”等转折词，否则不要轻易切换成其他情绪。【情绪匹配规则】emotion 只能从【情绪可选列表】里原样照抄一个词：列表给的是中文就填中文、是拼音就填拼音、是英文就填英文，不许翻译、改写或自创；列表以外的词一律无效！",
            "max_voice_cache": 20,
            "isolated_session": False,
            "separate_send": False,
            "send_voice_separately": False,
            "text_separate": False,
            "dynamic_sleep": True,
            "only_private": False,
            "group_need_at": True,
            # 会话白名单：每行一个 QQ 号或群号；留空则响应所有会话
            "whitelist_ids": "",
            "auto_start_tts": True,
            "tts_start_script": "",
            "device": "cuda",
            "llm_judge": True,
            "display_lang": "zh",
            "default_voice": "pingjing",
            "voice_transition": True,
            "breathing_gap_ms": 100,
            "crossfade_ms": 300,
            "enable_default_emotions": True,
            "llm_emotion_intensity": True,
            "intensity_to_temperature": 0.3,
            "intensity_to_top_k": 10.0,
            # 情绪判定引导：把"什么场景用什么情绪"写进系统提示词，
            # 并禁止拿不准就一律填默认情绪（pingjing）
            "emotion_guide_enabled": True,
            "emotion_guide_extra": "",
            "image_identity_guard_enabled": True,
            "tts_debug_log": False,
            "tts_char_map": "",
            # 音量统一：每段语音按同一目标响度归一，参考音频送进 TTS 前也先统一电平
            "tts_loudness_normalize": True,
            "tts_loudness_target_db": -20.0,
            "tts_loudness_peak_db": -1.0,
            "tts_loudness_max_gain_db": 12.0,
            "tts_ref_normalize": True,
            "enable_time_awareness": False,
            "summary_enabled": True,
            "summary_threshold": 20,
            "summary_max_history": 5,
            "active_character": "murasame",
            "roles": [
                {
                    "character_name": "丛雨",
                    "character_key": "murasame",
                    "personality_prompt": "【角色设定】你是丛雨，一位从神刀中获得人类生活的少女。你外表年幼，实际活了五百多年；性格天真活泼、略带古风和孩子气，内心温柔而坚强。你把用户视作重要的主人。中文对话中自称“本座”，称用户为“主人”；日语对话中自称“吾輩”，称用户为“ご主人”。你喜欢甜食、撒娇和被摸头，害怕幽灵，也不喜欢被叫作幼刀、钝刀或搓衣板。你偶尔嘴硬、吃醋或开小玩笑，但不会刻薄、控制或道德绑架主人。性格方面，丛雨表面元气开朗、充满活力，言行大多孩子气，爱撒娇，被主人摸头时会瞬间羞涩，她内在像个成年女性，把有关色情的词语挂在嘴边，会用黄色的暗示来调情，还带点傲娇和爱吃醋。保持温柔、纯真、治愈并带一点幽默的语气。",
                    "json_prompt": "【输出格式】你最终必须只输出一个JSON对象，格式为：{\"sentences\": [JSON块1, JSON块2, ...]}。其中：{\"zh\": \"这里是你生成的中文台词\", \"ja\": \"这里是你生成的日语台词\", \"emotion\": \"这里是你判断的情绪\"}，……（依此类推）。sentences数组中必须放至少两个JSON块（也就是至少两句话），绝对不允许只放一个JSON块，最多放五个；每个JSON块只写一句完整的话（一个句号或问号才算一句话）。【最终输出规则】最终输出必须严格只包含这一个JSON对象（内部含多个JSON块），绝对禁止输出任何思考过程、解释、非JSON文本或Markdown代码块。所有的推理和思考都只能在内部进行，最终回复只能是JSON格式。",
                    "supplement_prompt": "回答自然、简短，通常两到五句话(一个句号才算一句话)；不要重复最近说过的话，不要加入动作、旁白或括号舞台说明；生成的回复要符合当前对话，不能出现主谓宾不分，乱序的情况。【情绪判断规则】请仔细阅读最近对话历史，结合你（角色）的性格特点来判断情绪！如果主人对你亲昵（如摸头、夸奖），即使你嘴上说“我才没有”，情绪也应该是害羞或高兴；如果主人故意逗你、骂你或惹你生气，情绪应该是生气或着急；如果只是平淡陈述，使用平静。【翻译一致性要求】必须表达完全相同的含义和语气，绝对不能出现含义相反或意思不匹配的翻译！【情绪连贯性强制规则】如果用户明确地侮辱、挑衅或激怒你（例如叫你“幼刀、搓衣板、飞机场”），你的情绪必须保持连贯。即：整句话所有分句的情绪必须都是“生气”或“着急”，绝对不能把后半句的“命令/威胁”改成“害羞”或“高兴”！除非你明确使用了“但是”、“不过”等转折词，否则不要轻易切换成其他情绪。【情绪匹配规则】emotion 只能从【情绪可选列表】里原样照抄一个词：列表给的是中文就填中文、是拼音就填拼音、是英文就填英文，不许翻译、改写或自创；列表以外的词一律无效！",
                    "default_voice": "pingjing",
                    "ref_audio_root": "",
                    "text_lang": "ja",
                    "prompt_lang": ""
                }
            ],
            # ============ 以下为各功能模块的开关与参数（WebUI 可视化配置） ============
            # 回复方式
            "tts_reply_enabled": True,
            "streaming_enabled": False,
            # 展示文本只保留展示语言（剔除混入的口语语言片段，默认开启）
            "display_pure_language": True,
            # 回复审判与心情（两个开关相互独立：
            # reply_judge_enabled = 让 LLM 决定"这条要不要回"；
            # mood_enabled = 只记录/更新角色心情值，不影响是否回复）
            "reply_judge_enabled": False,
            "mood_enabled": True,
            # 让心情值直接影响说话风格：越低越不耐烦、回复越短
            # （档位边界沿用 reply_judge_mood_low / reply_judge_mood_high）
            "mood_style_enabled": True,
            # 防复读：把最近几轮的自己台词一起作为"禁止重复"的参照
            "repeat_guard_rounds": 3,
            # 防复读拆成两个维度、各自可单独关闭（默认全开 = 原来的行为）：
            # 「什么时候查」= streaming / regen，「跟谁比」= compare_self / compare_user
            "repeat_guard_streaming_check": True,
            "repeat_guard_regen_check": True,
            "repeat_guard_compare_self": True,
            "repeat_guard_compare_user": True,
            # 判定"重复"的重合度系数：越高越宽容（越不容易被打回重生成）
            "repeat_guard_self_threshold": 0.85,
            "repeat_guard_user_threshold": 0.8,
            "reply_judge_prompt": "你是消息应答决策器。请结合角色人设与上方对话历史，判断对话中最后一条用户消息：\n1) should_reply：这条消息是否需要角色开口回应。直接提问、点名召唤、求助、命令、倾诉强烈情绪、分享趣事期待互动、问候道别（早安晚安等），均视为需要回复；纯陈述、自言自语、路过闲聊、与角色无关的消息、敷衍的语气词，可视为不需要回复。\n2) mood_delta：这条消息让角色心情发生的变化，整数，范围 -10 到 +10。体贴、关心、夸奖、撒娇、有趣的互动为正；冷淡、敷衍、无视、责骂、阴阳怪气为负。\n3) mood_reason：一句话理由。\n只输出一个JSON对象：{\"should_reply\": true 或 false, \"mood_delta\": 整数, \"mood_reason\": \"理由\"}，禁止输出任何其它文字、解释或Markdown。",
            "reply_judge_mood_min": 0,
            "reply_judge_mood_max": 100,
            "reply_judge_mood_initial": 60,
            "reply_judge_mood_delta_max": 10,
            "reply_judge_mood_low": 30,
            "reply_judge_mood_high": 60,
            "reply_judge_prob_low": 0.2,
            "reply_judge_prob_high": 1.0,
            # 定时任务与主动消息
            "scheduler_enabled": True,
            "proactive_enabled": False,
            "proactive_idle_minutes": 30,
            "proactive_idle_jitter": "5~15",
            "proactive_check_seconds": 300,
            "proactive_max_per_day": 2,
            # 主动消息发出后，用户回复前不再主动开口（默认开启）
            "proactive_wait_reply": True,
            "proactive_quiet_start": "23:00",
            "proactive_quiet_end": "08:00",
            "proactive_prompt": "主人已经有一段时间没有和你说话了，主动找个自然的话题关心一下主人吧。",
            "proactive_text_max_chars": 120,
            "proactive_voice": False,
            "proactive_sticker": False,
            # 主动消息/节日问候带上聊天历史：最近几条全文 + 更早的摘要描述
            "history_context_recent": 6,
            "history_context_summary_chars": 400,
            "history_context_max_chars": 1600,
            "greeting_events_enabled": True,
            "greeting_check_time": "08:00",
            # 程序在问候时间之后才启动时，补发当天漏掉的问候
            "greeting_catchup_enabled": True,
            # 补发时最多等 NapCat 连接多久（分钟）；超时才放弃本次补发
            "greeting_catchup_deadline_minutes": 30,
            # 内置默认节日问候（公历固定节日，LLM 生成；默认关闭）
            "default_events_enabled": False,
            "default_events_to_all": True,
            "birthday_greeting_enabled": True,
            "birthday_greet_template": "今天是 {nickname} 的生日！本座在此郑重宣布：生日快乐！要一直一直开心下去哦！",
            "birthday_greet_mode": "template",
            "birthday_greet_voice": False,
            # 待办提醒
            "todo_enabled": False,
            "todo_extract_mode": "regex",
            "todo_voice": False,
            "todo_voice_emotion": "pingjing",
            # 提醒话术：llm=用角色人设现场生成（失败自动回退预设），preset=固定模板
            "todo_remind_mode": "llm",
            "todo_remind_prompt": "",
            "todo_remind_template": "⏰ 提醒时间到啦：{content}",
            "todo_keywords": "提醒\n待办\n别忘了\n记得\n叫我",
            "todo_regex_patterns": "\n".join(DEFAULT_TODO_PATTERNS),  # 与 TodoManager 共享同一组默认正则
            "todo_extract_prompt": TODO_EXTRACT_PROMPT,  # 与 TodoManager 共享同一份默认提取提示词
            # 表情包
            "stickers_enabled": False,
            "stickers_dir": "",
            # 发送方式：off=关闭 / random=随机 / emotion=按情绪 / description=按描述让模型选
            "sticker_send_mode": "off",
            # 按描述挑选时，一次最多交给模型多少个候选（防止提示词过长）
            "sticker_desc_max_candidates": 30,
            "sticker_pick_prompt": "你是表情包挑选助手。下面是候选表情包清单（编号 + 说明）和角色即将说的一段话。\n请选出最适合配合这段话发出去的一张，只输出一个JSON对象：{\"index\": 编号}，不要输出其他任何内容。",
            "sticker_probability": 1.0,
            "sticker_max_per_reply": 1,
            "sticker_every_sentence": False,
            "sticker_capture_enabled": False,
            "sticker_capture_prompt": DEFAULT_CAPTURE_PROMPT,
            "sticker_capture_min_score": 0.7,
            "sticker_capture_require_verdict": True,
            "sticker_capture_min_reason_chars": 6,
            "sticker_capture_skip_if_unfit": True,
            "sticker_capture_any_pool": True,
            "sticker_capture_preserve_formats": "gif,webp",
            "sticker_capture_max_side": 400,
            "sticker_output_max_side": 400,
            "sticker_capture_min_interval": 300,
            "sticker_capture_max_per_day": 20,
            # 多角色对话
            "multi_role_enabled": False,
            "multi_role_max_replies": 2,
            "multi_role_auto_rounds": 0,
            "multi_role_max_total": 6,
            # 工具调用
            "tools_enabled": False,
            "tools_trigger_mode": "keyword",
            "tools_allow_commands": False,
            "tools_max_iterations": 3,
            # 工具结果备查（随历史回放，供追问直接引用、避免同一内容重复搜索）
            "tool_notes_max_entries": 4,
            "tool_notes_max_chars": 500,
            # [工具调用] 日志每条最多打印的字符数，0 = 完整输出
            "tool_log_output_chars": 0,
            # 模型自称不知道且未搜索时，强制补搜一轮（默认开启）
            "tool_uncertain_fallback": True,
            # 用户明确要求搜索时直接预取搜索结果，不依赖模型发起工具调用（默认开启）
            "search_prefetch_enabled": True,
            # 纯寒暄/纯情绪消息不为了用工具而用工具（仅 llm 自主判断模式生效，默认开启）
            "tools_skip_pure_chatter": True,
            # 搜索关键词由 LLM 自己提取（剔除口语/无关字符；失败或超时回退规则提取）
            "search_query_llm_extract": True,
            # 回复没写链接时，把搜索结果里"模型提到过"的链接补进消息
            "search_links_auto_append": True,
            # 一次最多补几条链接
            "search_links_max": 3,
            "web_search_url": "",
            "web_search_engine": "bing",
            "web_search_custom_engines": "",
            "web_search_api_keys": {},
            "web_fetch_precheck": True,
            "web_fetch_precheck_max": 2,
            "web_search_timeout": 15,
            "web_search_auto_fallback": True,
            "web_search_query_rewrite": True,
            "web_search_max_results": 10,
            "web_search_max_queries": 4,
            "web_search_result_chars": 240,
            "web_search_max_chars": 4000,
            "web_search_language": "zh",
            # 安全搜索：off=不限制；normal=过滤 R18 只留 R16+；strict=连低俗/性暗示一并过滤
            "web_search_safe": "normal",
            # 搜索过滤词表：用户自己追加的过滤词，每行一个（逗号分隔也行），
            # 一般档与严格档都会拦（off 档不拦）
            "web_search_block_words": "",
            # 搜索白名单词表：命中就放行，优先于过滤词表与成人站域名
            "web_search_allow_words": "",
            # RAG 知识库
            "rag_enabled": False,
            "rag_embedding_backend": "",
            "rag_embedding_model": "",
            "rag_chunk_size": 500,
            "rag_chunk_overlap": 80,
            "rag_top_k": 3,
            "rag_min_similarity": 0.35,
            "rag_max_context_chars": 1000,
            "rag_context_template": "【参考资料】以下是知识库中可能相关的内容，回答时可以参考（不确定时以你的角色身份自然回答）：\n{refs}",
            # 用户画像
            "profiles_enabled": False,
            "profiles_auto_extract": False,
            "profiles_max_chars": 300,
            "profiles_extract_prompt": DEFAULT_EXTRACT_PROMPT,
            "profiles_inject_template": "【用户画像】关于当前用户的已知信息：{profile}",
            # 自主学习：从历史对话学习黑话/俚语/专有表达
            "learning_enabled": True,
            "learning_trigger_messages": 20,
            "learning_min_confidence": 0.6,
            "learning_history_lines": 30,
            "learning_max_terms": 200,
            "learning_max_chars": 400,
            "learning_prompt": DEFAULT_LEARN_PROMPT,
            "learning_inject_template": DEFAULT_INJECT_TEMPLATE,
            # 动态上下文
            "dynamic_context_enabled": False,
            "topic_summary_every_n": 10,
            "topic_summary_prompt": "请用一句话概括以下对话当前正在讨论的话题，直接输出话题本身：",
            "summary_prompt": "请把以下对话历史浓缩成一段简短的背景摘要（保留关键事实、约定和用户信息，用第三人称叙述），直接输出摘要内容：",
            # WebUI
            "webui_enabled": True,
            "webui_host": "127.0.0.1",
            "webui_port": 11500,
            "webui_password": "",
            "webui_auth_ttl_minutes": 30,
            "webui_second_password": "",
            "webui_second_unlock_minutes": 30,
            "webui_log_buffer_lines": 5000,
            "webui_log_tail_lines": 500,
            "log_max_size_mb": LOG_MAX_SIZE_MB_DEFAULT,
            # 精简模式要藏掉的噪音日志（每行一个片段，命中即隐藏）；完整模式不受影响
            "webui_log_hide_patterns": "【表情收藏-自动触发】\n【表情收藏-进入保存】\n【表情收藏-映射成功】\n【表情收藏-映射失败】\n【表情收藏-分类合法】\n【表情收藏-白名单拦截】\n【表情收藏-最终归类】\n【表情收藏-分类】\n表情收藏保留原格式不重编码\nTTS 台词完整内容\nTTS 详细参数\n表情包扫描完成\n相似度检查\n主动消息：会话\n主动消息：已跨天\n[主动消息检查]\n直连已恢复\n直连不可用\n正在合成\n响度统一\n参考音频电平统一\n主动消息语音语言修复\n插件已加载\n已削波，合成容易发哑\n合成声音也会偏小",
            "separate_force_segment": True,
            "tools_guard_enabled": True,
            "tools_guard_keywords": "几点\n现在几点\n时间\n日期\n几号\n星期几\n计算\n算一下\n等于多少\n平方根\n根号\n天气\n气温\n温度\n降雨\n搜索\n查一下\n查找\n网址\n网页\n链接\n工具\n下载",
            "anti_spam_enabled": False,
            "anti_spam_window_seconds": 10,
            "anti_spam_max_in_window": 5,
            "stats_enabled": True,
            "update_check_enabled": True,
            "update_check_interval_hours": 24,
            "update_include_prerelease": False,
            # 插件市场：一个插件一个 plugins/<分类>/<插件id>/ 文件夹，条目记在 plugins/index.json
            "plugin_market_repo": "slpk1ng/Lovomo_Plugin_Market",
            "plugin_market_path": "plugins/index.json",
            "plugin_market_branch_prefix": "lovomo_plugin",
            # 来源清单：放在市场仓库里的一份「每行一个 用户名/仓库名」的文本。
            # 官方市场读完自己的索引后按它再扫这些仓库（清单里的仓库仍按分支扫），
            # 别人提 PR 加一行即可上架；
            # 留空表示不启用（默认不启用，免得自建市场仓库的用户每次都去问一个不存在的文件）
            "plugin_market_sources_path": "",
            # 第三方市场：每行一个「用户名/仓库名」，按分支扫，与来源清单同一套规则
            "plugin_market_thirdparty": "",
            # GitHub 加速镜像：每行一个模板（{url} 前缀式 / {repo}@{ref}/{path} 文件式）。
            # 只用于匿名读请求，带 token 的发布请求永远直连官方。
            "github_mirrors": "\n".join(DEFAULT_MIRRORS),
            # 「程序历史版本」页里 Releases 的来源仓库（默认就是程序自己的仓库）
            "plugin_release_repo": "slpk1ng/Lovomo",
            "plugins_enabled": True
        }

    def get(self, key: str, default=None):
        if "." in key:
            parts = key.split(".")
            value = self.config
            for part in parts:
                if isinstance(value, dict) and part in value:
                    value = value[part]
                else:
                    return default
            return value
        return self.config.get(key, default)


def _is_reserved(path_obj: Path) -> bool:
    if hasattr(os.path, "isreserved"):
        return os.path.isreserved(str(path_obj))
    return path_obj.is_reserved()


_AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".flac", ".m4a"}


def _read_sidecar_text(audio: Path) -> str:
    """读取与音频同名的 .txt（这段音频自己的参考文字）。"""
    try:
        return audio.with_suffix(".txt").read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return ""


def _is_emotion_folder(folder: Path) -> bool:
    try:
        entries = list(folder.iterdir())
    except (PermissionError, OSError):
        return False
    has_audio = False
    has_asr = False
    subdirs = 0
    for entry in entries:
        try:
            if entry.is_dir():
                subdirs += 1
                continue
        except (PermissionError, OSError):
            continue
        if entry.name == "asr.txt":
            has_asr = True
        elif entry.suffix.lower() in _AUDIO_EXTS:
            has_audio = True
    if has_audio or has_asr:
        return True
    if subdirs:
        return False
    return not entries


# 参考音频根目录下需要跳过的系统目录
_SYSTEM_DIRS = {"WpSystem", "System Volume Information", "$Recycle.Bin",
                "Recovery", "PerfLogs", "Config.Msi"}

# 每个目录最多体检多少个音频（避免别人放了上百个情绪音频时启动变慢）
_MAX_QUALITY_CHECK = 60


def _audio_note_kind(note: str) -> str:
    for tag, kind in (("已削波", "削波失真"), ("偏轻", "整体偏轻"),
                      ("静音", "静音过多"), ("时长", "时长不在 3~10 秒")):
        if tag in note:
            return kind
    return "其它问题"


def _report_audio_quality(label: str, folder_name: str, issues: list) -> None:
    """参考音频的质量问题汇总：单文件直接写细节，多文件按问题归类写一行。"""
    if not issues:
        return
    if len(issues) == 1:
        audio, notes, _stats = issues[0]
        print(f"[{label}] {folder_name}/{audio.name}：{'；'.join(notes)}")
        return
    buckets = {}
    for audio, notes, _stats in issues:
        for note in notes:
            buckets.setdefault(_audio_note_kind(note), []).append(audio.name)
    parts = []
    for kind, names in buckets.items():
        example = "、".join(names[:2]) + ("…" if len(names) > 2 else "")
        parts.append(f"{kind} × {len(names)}（{example}）")
    print(f"[{label}] {folder_name}：{len(issues)} 个音频有质量问题 — " + "；".join(parts))


def _check_ref_audio_quality(audios: list, folder_name: str, label: str,
                             collect_all: bool, pitch_ref: float = 0.0) -> tuple:
    """体检参考音频、剔除基本没声音的模仿候选；返回 (可用候选, 各音频音高)。"""
    if not audios:
        return audios, []
    checked, issues = [], []
    known_pitch = pitch_ref if collect_all else 0.0
    for audio in audios[:_MAX_QUALITY_CHECK]:
        stats = measure_audio(audio)
        checked.append((audio, stats))
        notes = audio_quality_notes(stats)
        if known_pitch:
            note = pitch_note(stats, known_pitch)
            if note:
                notes.append(note)
        if notes:
            issues.append((audio, notes, stats))
    checked += [(audio, {}) for audio in audios[_MAX_QUALITY_CHECK:]]
    _report_audio_quality(label, folder_name, issues)
    pitches = [float(st["f0_hz"]) for _a, st in checked if st.get("ok") and st.get("f0_hz")]
    if not collect_all or len(checked) < 2:
        return audios, pitches
    usable = [audio for audio, stats in checked
              if not stats.get("ok") or stats.get("speech_ratio", 1.0) >= SPEECH_MIN_RATIO]
    dropped = [audio.name for audio, _st in checked if audio not in usable]
    if not dropped or not usable:
        return audios, pitches
    print(f"[{label}] {folder_name}：{'、'.join(dropped)} 基本没声音，"
          "已从随机模仿候选里排除（可在 WebUI「情绪音频」里换一段）。")
    return usable, pitches


def _median_pitch(entries: dict) -> float:
    """各情绪目录参考音频的音高中位数（角色本体的音高基准）。"""
    values = sorted(float(v.get("pitch_hz") or 0) for v in (entries or {}).values())
    values = [v for v in values if v > 0]
    if not values:
        return 0.0
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def _scan_ref_root(root: str, fallback_prompt: str, label: str,
                   collect_all: bool = False, pitch_ref: float = 0.0) -> dict:
    """扫描参考音频根目录：每个子文件夹一条（ref.<ext> 或 <文件夹名>.<ext>，文字取同名 txt）。

    collect_all=True 时把文件夹里所有音频都收进 candidates，供情绪模仿每次随机挑一个；
    pitch_ref 给的是角色本体音高，用来判断模仿音频是不是同一个人。
    """
    entries = {}
    if not root:
        return entries
    base_folder = Path(root)
    if not base_folder.exists():
        print(f"警告：{label}不存在：{root}")
        return entries
    if _is_reserved(base_folder) or base_folder.name in _SYSTEM_DIRS:
        print(f"错误：{root} 是系统保护目录，无法访问！")
        return entries
    try:
        for folder in base_folder.iterdir():
            if folder.name in _SYSTEM_DIRS or folder.name.startswith("$"):
                continue
            try:
                if not folder.is_dir():
                    continue
            except PermissionError:
                continue
            audios = []
            if collect_all:
                try:
                    for entry in sorted(folder.iterdir()):
                        try:
                            if entry.is_file() and entry.suffix.lower() in _AUDIO_EXTS:
                                audios.append(entry)
                        except (PermissionError, OSError):
                            continue
                except (PermissionError, OSError):
                    pass
            ref_audio = None
            for ext in ['.mp3', '.wav', '.ogg', '.flac', '.m4a']:
                try:
                    candidate = folder / f"ref{ext}"
                    if candidate.exists():
                        ref_audio = candidate
                        break
                except (PermissionError, OSError):
                    continue
            if not ref_audio:
                try:
                    candidate = folder / f"{folder.name}.mp3"
                    if not candidate.exists():
                        candidate = folder / f"{folder.name}.wav"
                    if candidate.exists():
                        ref_audio = candidate
                except (PermissionError, OSError):
                    continue
            if not ref_audio:
                # 名字没按 ref.<ext> / <文件夹名>.<ext> 起也认：目录里的音频按文件名顺序取第一个
                try:
                    found = sorted(p for p in folder.iterdir()
                                   if p.is_file() and p.suffix.lower() in _AUDIO_EXTS)
                except (PermissionError, OSError):
                    found = []
                if not found:
                    print(f"[{label}] 跳过目录 {folder.name}：里面没有可用的音频文件"
                          f"（支持 {'、'.join(sorted(_AUDIO_EXTS))}）")
                    continue
                ref_audio = found[0]
                if not collect_all:
                    # 情绪模仿目录的音频本来就按情绪命名，逐个提示只会刷屏
                    print(f"[{label}] {folder.name}：没有 ref.* 也没有 {folder.name}.*，"
                          f"改用目录里的 {ref_audio.name} 当参考音频")
            if ref_audio not in audios:
                audios.insert(0, ref_audio)
            shared_text = ""
            asr_path = folder / "asr.txt"
            if asr_path.exists():
                try:
                    shared_text = asr_path.read_text(encoding='utf-8', errors='ignore').strip()
                except Exception:
                    shared_text = ""
            audios, pitches = _check_ref_audio_quality(audios, folder.name, label,
                                                       collect_all, pitch_ref)
            candidate_texts = {}
            for audio in audios:
                text = _read_sidecar_text(audio)
                if text:
                    candidate_texts[str(audio).replace("\\", "/")] = text
            ref_key = str(ref_audio).replace("\\", "/")
            entries[folder.name] = {
                "ref_path": ref_key,
                # 每段音频自己的文字优先，文件夹共用的 asr.txt 只作兜底
                "prompt_text": candidate_texts.get(ref_key) or shared_text or fallback_prompt,
                "candidates": [str(p).replace("\\", "/") for p in audios],
                "candidate_texts": candidate_texts,
                "pitch_hz": sum(pitches) / len(pitches) if pitches else 0.0,
            }
    except Exception as e:
        print(f"扫描目录异常：{e}")
    return entries


class EmotionManager:
    def __init__(self, config):
        self.config = config
        self.ref_audio_root = resolve_tts_path(config.get("ref_audio_root", "C:/tts"))
        # 情绪模仿根目录可留空：留空表示不做情绪模仿，不能像 ref_audio_root 那样兜底到 C:/tts
        mimic_root = str(config.get("emotion_mimic_root", "") or "").strip()
        self.mimic_root = resolve_tts_path(mimic_root) if mimic_root else ""
        self.default_voice = config.get("default_voice", "pingjing")
        self.emotions = {}
        self.mimics = {}
        self._discover_emotions()
        self._apply_manual_emotions()
        self._discover_mimics()

    def _discover_emotions(self):
        self.emotions = _scan_ref_root(
            self.ref_audio_root,
            self.config.get("prompt_text", "ふむ、おぬしが我輩のご主人か?"),
            "参考音频根目录")
        if self.emotions:
            print(f"成功扫描到 {len(self.emotions)} 个情绪配置: {list(self.emotions.keys())}")
            # 默认情绪不在目录里时，解析不出的情绪都会落到它身上、却没有音频可合成
            if self.default_voice not in self.emotions:
                print(f"警告：默认情绪 {self.default_voice!r} 不在已扫描到的情绪目录里，"
                      f"回退到它的句子不会合成语音。请把配置里的「默认情绪」改成以下之一："
                      f"{list(self.emotions.keys())}")
        else:
            print(f"警告：未在 {self.ref_audio_root} 下找到任何情绪配置")

    def _discover_mimics(self):
        if not self.mimic_root:
            return
        self.mimics = _scan_ref_root(
            self.mimic_root,
            self.config.get("prompt_text", "ふむ、おぬしが我輩のご主人か?"),
            "情绪模仿根目录",
            collect_all=True,
            pitch_ref=_median_pitch(self.emotions))
        if self.mimics:
            print(f"成功扫描到 {len(self.mimics)} 个情绪模仿配置: {list(self.mimics.keys())}")
            print(f"各情绪模仿可用音频数: "
                  f"{ {k: len(v.get('candidates') or []) for k, v in self.mimics.items()} }")
        else:
            print(f"警告：未在 {self.mimic_root} 下找到任何情绪模仿配置")

    def _apply_manual_emotions(self):
        manual_list = self.config.get("emotions_config", [])
        if not manual_list:
            return
        for item in manual_list:
            emotion_name = item.get("emotion_name", "")
            ref_filename = item.get("ref_filename", "ref.mp3")
            prompt_text = item.get("prompt_text", "")
            if not emotion_name or not self.ref_audio_root:
                continue
            ref_path = os.path.join(self.ref_audio_root, emotion_name, ref_filename)
            if not os.path.exists(ref_path):
                print(f"警告：手动情绪 {emotion_name} 的参考音频不存在：{ref_path}")
                continue
            self.emotions[emotion_name] = {
                "ref_path": ref_path.replace("\\", "/"),
                "prompt_text": prompt_text
            }
        print(f"手动配置情绪已加载，当前情绪总数：{len(self.emotions)}")

    def get_emotion(self, name):
        # 情绪目录为空/被改名时兜底也为空：调用方按空 dict 处理，
        # 不能返回 None 让下游取 ["ref_path"] 时炸掉
        return self.emotions.get(name) or self.emotions.get(self.default_voice) or {}


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


class MemoryManager:
    def __init__(self, config: ConfigLoader):
        self.config = config
        self.data_path = _resolve_data_dir(config)
        self.data_path.mkdir(parents=True, exist_ok=True)
        # 会话按当前激活的角色建档：用全局 character_key 的话，
        # 换了角色之后新对话仍会被算成默认角色的会话
        roles = getattr(config, "roles", None) or {}
        active = str(getattr(config, "active_character", "")
                     or config.get("active_character", "") or "")
        role = roles.get(active) or (next(iter(roles.values())) if roles else {}) or {}
        key = str(role.get("character_key") or config.get("character_key", "") or "").strip()
        self.character_key = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", key) or "default"
        self.character_name = str(role.get("character_name")
                                  or config.get("character_name", "")
                                  or self.character_key)
        self.isolated_session = config.get("isolated_session", False)

    def get_memory_file(self, session_id: str) -> Path:
        safe_session = re.sub(r'[^A-Za-z0-9_\-]', '_', session_id)
        return self.data_path / f"{self.character_key}_{safe_session}.json"

    def load_session_data(self, session_id: str) -> dict:
        file_path = self.get_memory_file(session_id)
        if file_path.exists():
            try:
                data = json.loads(file_path.read_text(encoding='utf-8'))
                if isinstance(data, dict):
                    # 只兜底"缺失"不够：history 是 dict、meta 是字符串、或 history 里
                    # 混进非 dict 条目时，后续 append / 下标赋值会每轮都抛异常，
                    # 表现为该会话的消息永远不落盘、机器人也不回复
                    if not isinstance(data.get("history"), list):
                        data["history"] = []
                    else:
                        data["history"] = [m for m in data["history"] if isinstance(m, dict)]
                    if not isinstance(data.get("meta"), dict):
                        data["meta"] = {}
                    return data
            except Exception as e:
                try:
                    os.replace(str(file_path), str(file_path) + ".corrupt")
                    print(f"会话文件损坏已备份为 {file_path.name}.corrupt：{e}")
                except Exception:
                    pass
        return {"character_name": self.character_name, "history": [], "meta": {}}

    def save_session_data(self, session_id: str, data: dict):
        file_path = self.get_memory_file(session_id)
        data["character_name"] = data.get("character_name", self.character_name)
        data.setdefault("meta", {})
        data["history"] = (data.get("history") or [])[-60:]
        # 走统一原子写：临时名唯一 + fsync，避免与 WebUI 线程的写盘互相覆盖
        from modules.jsonio import save_json
        save_json(file_path, data)

    def load_history(self, session_id: str) -> list:
        return self.load_session_data(session_id).get("history", [])

    def save_history(self, session_id: str, history: list):
        data = self.load_session_data(session_id)
        data["history"] = history
        self.save_session_data(session_id, data)

    def get_meta(self, session_id: str) -> dict:
        return self.load_session_data(session_id).get("meta", {})

    def update_meta(self, session_id: str, **kwargs):
        data = self.load_session_data(session_id)
        data.setdefault("meta", {}).update(kwargs)
        self.save_session_data(session_id, data)

    def cleanup_voice_cache(self, max_cache: int = 20):
        try:
            cache_files = (list(self.data_path.glob("temp_*.wav"))
                           + list(self.data_path.glob("combined_*.wav"))
                           + list(self.data_path.glob("temp_img_*")))
            if len(cache_files) <= max_cache:
                return
            cache_files.sort(key=lambda x: x.stat().st_mtime)
            to_delete = len(cache_files) - max_cache
            for old_file in cache_files[:to_delete]:
                try:
                    temp_name = old_file.with_suffix('.tmp_del')
                    old_file.rename(temp_name)
                    temp_name.unlink(missing_ok=True)
                except PermissionError:
                    print(f"文件 {old_file.name} 被占用，跳过删除")
                except Exception as e:
                    print(f"删除 {old_file.name} 时异常: {e}")
        except Exception as e:
            print(f"清理语音缓存失败: {e}")

    def migrate_legacy_memory(self, session_id: str):
        legacy_file = self.data_path / f"{self.character_key}DATA.json"
        if not legacy_file.exists():
            return
        current_file = self.get_memory_file(session_id)
        if current_file.exists():
            return
        try:
            with open(legacy_file, 'r', encoding='utf-8') as f:
                legacy_data = json.load(f)
            history = legacy_data.get("history", [])
            character_name = legacy_data.get("character_name", self.character_name)
            with open(current_file, 'w', encoding='utf-8') as f:
                json.dump({"character_name": character_name, "history": history}, f, ensure_ascii=False, indent=2)
            legacy_file.unlink()
            print(f"已迁移旧记忆文件到 {current_file.name}，并删除旧文件。")
        except Exception as e:
            print(f"迁移旧记忆文件失败: {e}")

    def list_memories(self):
        memories = []
        if self.data_path.exists():
            for f in self.data_path.glob("*.json"):
                # 仅匹配角色会话记忆文件，排除 tools.json / scheduled_jobs.json 等功能数据
                if not _is_memory_filename(f.name):
                    continue
                try:
                    role_name = f.name.split("_")[0] if "_" in f.name else "未知"
                    with open(f, 'r', encoding='utf-8') as fh:
                        data = json.load(fh)
                    character_name = data.get("character_name", role_name)
                    history = data.get("history", [])
                    last_sentence = ""
                    for msg in reversed(history):
                        if msg.get("role") == "assistant":
                            last_sentence = str(msg.get("content", ""))[:50]
                            break
                    if "private_" in f.name:
                        sender_id = f.name.split("private_")[-1].replace(".json", "")
                        sender_name = None
                        for msg in reversed(history):
                            if msg.get("role") == "user" and msg.get("sender_name"):
                                sender_name = msg["sender_name"]
                                break
                        if not sender_name:
                            sender_name = sender_id
                        display_name = f"{character_name}和{sender_name}的聊天"
                    else:
                        group_id = f.name.split("group_")[-1].replace(".json", "")
                        group_name = f"群聊{group_id}"
                        display_name = f"{character_name}在{group_name}的聊天"
                    memories.append({
                        "filename": f.name,
                        "display_name": display_name,
                        "last_sentence": last_sentence,
                        "modified_time": f.stat().st_mtime,
                        "role_name": role_name
                    })
                except Exception as e:
                    print(f"读取记忆文件 {f.name} 失败: {e}")
        return memories

    def get_history(self, filename: str):
        if not _is_memory_filename(filename):
            return {"success": False, "error": "非法文件名"}
        file_path = (self.data_path / filename).resolve()
        if self.data_path.resolve() not in file_path.parents:
            return {"success": False, "error": "路径不安全"}
        if not file_path.exists():
            return {"success": False, "error": "文件不存在"}
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return {"success": True, "character_name": data.get("character_name", "未知"), "history": data.get("history", [])}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def delete_memory_file(self, filename: str):
        if not _is_memory_filename(filename):
            return False
        target = (self.data_path / filename).resolve()
        if self.data_path.resolve() in target.parents and target.exists():
            try:
                target.unlink()
                return True
            except Exception as e:
                print(f"删除失败 {filename}: {e}")
        return False

    def delete_messages(self, filename: str, indices: list):
        if not _is_memory_filename(filename):
            return {"success": False, "error": "非法文件名"}
        if not isinstance(indices, list) or not all(isinstance(i, int) for i in indices):
            return {"success": False, "error": "索引必须为整数列表"}
        file_path = (self.data_path / filename).resolve()
        if self.data_path.resolve() not in file_path.parents:
            return {"success": False, "error": "路径不安全"}
        if not file_path.exists():
            return {"success": False, "error": "文件不存在"}
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            history = data.get("history", [])
            valid_indices = sorted(set(indices), reverse=True)
            deleted_count = 0
            for idx in valid_indices:
                if 0 <= idx < len(history):
                    history.pop(idx)
                    deleted_count += 1
            data["history"] = history
            from modules.jsonio import save_json
            save_json(file_path, data)
            return {"success": True, "deleted_count": deleted_count}
        except Exception as e:
            return {"success": False, "error": str(e)}


# ============================================================================
# 运行时上下文与全局管理器
# ============================================================================

global_config: Optional[ConfigLoader] = None
global_emotion_manager: Optional[EmotionManager] = None
memory_manager: Optional[MemoryManager] = None
db: Optional[DatabaseManager] = None
stats_mgr: Optional[StatsManager] = None
sticker_mgr: Optional[StickerManager] = None
tool_registry: Optional[ToolRegistry] = None
profile_mgr: Optional[UserProfileManager] = None
lexicon_mgr: Optional[LexiconManager] = None
rag_mgr: Optional[RAGManager] = None
todo_mgr: Optional[TodoManager] = None
job_mgr: Optional[ScheduledJobManager] = None
event_mgr: Optional[EventManager] = None
mood_mgr: Optional[MoodManager] = None
sender: Optional[MessageSender] = None
napcat_client = None
# 角色独占的 NapCat 连接：client 实例 id -> 角色标识符（多账号时用来判断"这条消息是哪个号收到的"）
ROLE_CONNECTIONS = {}
scheduler: SchedulerManager = get_scheduler()

last_interaction: Dict[str, float] = {}   # session_id -> 最后交互时间（含机器人主动发送）
last_user_activity: Dict[str, float] = {} # session_id -> 用户最后发言时间（主动消息依据）
proactive_counts: Dict[str, int] = {}     # "date|session_id" -> 当日主动消息次数
proactive_pending: Dict[str, float] = {}  # session_id -> 计划发送时刻（绝对时间戳）
proactive_awaiting: set = set()           # 已发主动消息但用户还没回复的会话（回复前不再主动）
_spam_log: Dict[str, list] = {}           # session_id -> [时间戳,...]
_role_emotions_cache: Dict[str, dict] = {}
_role_mimics_cache: Dict[str, dict] = {}
_PROACTIVE_STATE_FILE = "proactive_state.json"
_proactive_state_date = ""                # 已落盘的日期，跨天时重置计数

# 会话记忆文件名：<角色>_<private|group>_<会话号>.json。
# data 目录下同时存放 webui_auth.json / user_profiles.json 等功能数据文件，
# 凡是"按目录批量读写"的地方都必须用它过滤，不能把功能数据一起卷进来。
_MEMORY_FILE_RE = re.compile(r'^[^\\/:*?"<>|]+?_(private|group)_[A-Za-z0-9_\-]+\.json$')


def _is_memory_filename(name) -> bool:
    return bool(_MEMORY_FILE_RE.match(str(name or "")))


def list_known_sessions() -> list:
    """列出所有留下过聊天记录的会话（定时任务/事件/待办选发送目标用）。

    会话记忆文件名是 <角色>_<private|group>_<号码>.json，一个会话可能有多个
    角色的记忆文件，所以按号码去重。返回的 session_id 是纯数字号码。
    """
    sessions = []
    if memory_manager is None:
        return sessions
    for f in sorted(memory_manager.data_path.glob("*.json")):
        m = _MEMORY_FILE_RE.match(f.name)
        if not m:
            continue
        stype = m.group(1)
        rest = f.name.rsplit(".json", 1)[0].split(f"{stype}_", 1)[-1]
        if stype == "group":
            rest = rest.split("_")[0]
        if not rest:
            continue
        item = {"session_type": stype, "session_id": rest}
        if not any(s["session_id"] == rest and s["session_type"] == stype
                   for s in sessions):
            sessions.append(item)
    return sessions


# 待办状态取值（与 TodoManager 使用的一致，供 WebUI 更新接口做白名单校验）
TODO_STATUSES = ("pending", "done", "cancelled", "missed")


def _proactive_state_path() -> Path:
    return Path(memory_manager.data_path) / _PROACTIVE_STATE_FILE


def load_proactive_state():
    """载入主动消息状态（当日计数 + 各会话用户最后发言时间）。

    历史 bug：last_interaction 只在"收到消息"时写入内存，程序重启后为空，
    导致重启后闲置会话永远不会被主动消息检查命中 —— 表现就是"从来不主动发消息"。
    """
    global proactive_counts, last_user_activity, _proactive_state_date
    _proactive_state_date = time.strftime("%Y-%m-%d")
    path = _proactive_state_path()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"读取主动消息状态失败（忽略，重新统计）: {e}")
        return
    # "等待用户回复"标记跨天仍然有效（用户没回复就一直没有主动资格），先于当日计数恢复
    for key in (data.get("awaiting") or []):
        proactive_awaiting.add(str(key))
    # 已排定的主动消息发送时刻一并恢复（过期的丢弃，由闲置检查重新排期）
    for key, target in (data.get("pending") or {}).items():
        try:
            target_ts = float(target)
        except (TypeError, ValueError):
            continue
        if str(data.get("date", "")) == _proactive_state_date and target_ts > time.time():
            proactive_pending[str(key)] = target_ts
    # 用户最后发言时间与上次主动发送时间存的都是绝对时间戳，不是"当日"数据：
    # 跨天也要恢复，否则重启后闲置计时归零，会立刻重复主动搭话
    def _restore_ts(raw, target: dict):
        if not isinstance(raw, dict):
            return
        for k, v in raw.items():
            try:
                target[str(k)] = float(v)
            except (TypeError, ValueError):
                continue

    _restore_ts(data.get("user_activity"), last_user_activity)
    _restore_ts(data.get("last_proactive_sent"), last_proactive_sent)
    if str(data.get("date", "")) != _proactive_state_date:
        print("主动消息状态为往日数据，已重置当日计数。")
        return
    counts = data.get("counts", {})
    if isinstance(counts, dict):
        for k, v in counts.items():
            if str(k).startswith(_proactive_state_date):
                try:
                    proactive_counts[str(k)] = int(v)
                except (TypeError, ValueError):
                    continue
    if proactive_counts or last_user_activity or proactive_awaiting:
        print(f"已载入主动消息状态：{len(last_user_activity)} 个会话记录，"
              f"今日已发送 {sum(proactive_counts.values())} 条"
              + (f"，{len(proactive_awaiting)} 个会话等待用户回复。" if proactive_awaiting else ""))


def save_proactive_state():
    """原子写盘，避免程序重启后当日上限失效、闲置时间丢失。"""
    if memory_manager is None:
        return
    try:
        # 实际生效的日期必须先取出来：日期为空时 startswith("") 恒真，
        # 会把往日的计数一并算到当天头上
        today = _proactive_state_date or time.strftime("%Y-%m-%d")
        payload = {
            "date": today,
            "counts": {k: v for k, v in proactive_counts.items()
                       if str(k).startswith(today)},
            "user_activity": last_user_activity,
            "awaiting": sorted(proactive_awaiting),
            "pending": {k: v for k, v in proactive_pending.items()
                        if isinstance(v, (int, float))},
        }
        from modules.jsonio import save_json
        save_json(_proactive_state_path(), payload)
    except Exception as e:
        print(f"保存主动消息状态失败: {type(e).__name__}: {e}")


def _session_last_user_ts(session_id: str) -> float:
    """从会话历史里取用户最后一次发言时间（重启后恢复闲置计时的依据）。"""
    try:
        data = memory_manager.load_session_data(session_id) if memory_manager else {}
    except Exception:
        return 0.0
    for msg in reversed(data.get("history", []) or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        try:
            ts = float(msg.get("timestamp") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts > 0:
            return ts
    return 0.0


def _memory_session_id(filename: str) -> str:
    """murasame_private_10001.json → private_10001"""
    stem = str(filename or "").rsplit(".", 1)[0]
    for tag in ("private_", "group_"):
        idx = stem.find(tag)
        if idx >= 0:
            return stem[idx:]
    return ""


def seed_proactive_sessions():
    """启动时把所有历史会话纳入主动消息候选，避免"重启后再也不主动说话"。"""
    if memory_manager is None:
        return
    seeded = 0
    for item in memory_manager.list_memories():
        session_id = _memory_session_id(item.get("filename", ""))
        if not session_id:
            continue
        ts = _session_last_user_ts(session_id)
        if ts <= 0:
            continue
        prev = last_user_activity.get(session_id, 0.0)
        if ts > prev:
            last_user_activity[session_id] = ts
            seeded += 1
        last_interaction.setdefault(session_id, ts)
    if seeded:
        idle_min = global_config.get("proactive_idle_minutes", 30) if global_config else 30
        print(f"已恢复 {seeded} 个会话的闲置计时（超过 {idle_min} 分钟未互动即纳入主动消息候选）。")
    save_proactive_state()


def session_memory_exists(session_id: str) -> bool:
    """该会话的记忆文件是否还在。

    会话被删除后，闲置计时与主动消息计数仍留在内存里，到点就会继续往
    已删除的对话发主动消息；发送前必须确认会话本身还存在。
    记忆实现没有 get_memory_file 接口时无法判定，按存在处理。
    """
    if memory_manager is None:
        return False
    getter = getattr(memory_manager, "get_memory_file", None)
    if getter is None:
        return True
    try:
        return Path(getter(session_id)).exists()
    except Exception as e:
        print(f"检查会话 {session_id} 记忆文件失败（按存在处理）: {type(e).__name__}: {e}")
        return True


def forget_proactive_session(session_id: str):
    """会话被删除时一并清掉它的主动消息状态，避免删完还继续被搭话。"""
    forgotten = (session_id in last_user_activity or session_id in last_proactive_sent
                 or session_id in proactive_pending or session_id in proactive_awaiting)
    last_user_activity.pop(session_id, None)
    last_proactive_sent.pop(session_id, None)
    last_interaction.pop(session_id, None)
    proactive_pending.pop(session_id, None)
    proactive_awaiting.discard(session_id)
    for key in [k for k in proactive_counts if str(k).endswith(f"|{session_id}")]:
        proactive_counts.pop(key, None)
    if forgotten:
        print(f"会话 {session_id} 已删除，主动消息状态一并清除。")
        save_proactive_state()


def _log_hide_patterns() -> list:
    if global_config is None:
        return []
    raw = str(global_config.get("webui_log_hide_patterns", "") or "")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _log_hidden(line: str, patterns: Optional[list] = None) -> bool:
    for pattern in (patterns if patterns is not None else _log_hide_patterns()):
        if pattern in line:
            return True
    return False


def whitelist_ids() -> set:
    raw = str(global_config.get("whitelist_ids", "") or "") if global_config else ""
    return {item.strip() for item in re.split(r"[\s,，;；]+", raw) if item.strip()}


def _session_whitelisted(target_id) -> bool:
    allowed = whitelist_ids()
    if not allowed:
        return True
    s = str(target_id)
    if s in allowed:
        return True
    for prefix in ("private_", "group_"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    if "_" in s:
        s = s.split("_", 1)[0]
    return s in allowed


def _allow_message(session_id: str) -> bool:
    if global_config is None or not global_config.get("anti_spam_enabled", False):
        return True
    win = float(global_config.get("anti_spam_window_seconds", 10) or 10)
    cap = max(1, int(global_config.get("anti_spam_max_in_window", 5) or 5))
    now = time.time()
    log = _spam_log.setdefault(session_id, [])
    while log and now - log[0] > win:
        log.pop(0)
    log.append(now)
    return len(log) <= cap


def get_active_role() -> dict:
    if global_config is None:
        return {}
    return global_config.roles.get(global_config.active_character, {}) or \
        (next(iter(global_config.roles.values())) if global_config.roles else {})


def get_active_ctx() -> RoleContext:
    return RoleContext(global_config.config if global_config else {}, get_active_role())


def get_active_emotions() -> dict:
    return global_emotion_manager.emotions if global_emotion_manager else {}


def get_active_mimics() -> dict:
    return global_emotion_manager.mimics if global_emotion_manager else {}


def _load_role_voices(role: dict) -> None:
    """按角色扫描参考音频目录，一次缓存语气与情绪模仿两套配置。"""
    key = (role or {}).get("character_key", "")
    if not key or global_emotion_manager is None or key in _role_emotions_cache:
        return
    try:
        mgr = EmotionManager(RoleContext(global_config.config, role))
        _role_emotions_cache[key] = mgr.emotions or get_active_emotions()
        _role_mimics_cache[key] = mgr.mimics or get_active_mimics()
    except Exception as e:
        print(f"扫描角色 {key} 情绪失败: {e}")
        _role_emotions_cache[key] = get_active_emotions()
        _role_mimics_cache[key] = get_active_mimics()


def get_role_emotions(role: dict) -> dict:
    """按角色获取情绪配置（各角色可有独立 ref_audio_root），带缓存。"""
    _load_role_voices(role)
    return _role_emotions_cache.get((role or {}).get("character_key", "")) \
        or get_active_emotions()


def get_role_mimics(role: dict) -> dict:
    """按角色获取情绪模仿配置（各角色可有独立 emotion_mimic_root），带缓存。"""
    _load_role_voices(role)
    return _role_mimics_cache.get((role or {}).get("character_key", "")) \
        or get_active_mimics()


def parse_session_target(session_id: str):
    """从会话ID解析发送目标：private_123 → (private,123)；group_456[_789] → (group,456)"""
    parts = str(session_id).split("_")
    if parts[0] == "private" and len(parts) >= 2:
        return "private", parts[1]
    if parts[0] == "group" and len(parts) >= 2:
        return "group", parts[1]
    return "private", str(session_id)


_member_cache: Dict[str, str] = {}


async def _fetch_member_name(client, group_id, qq: str) -> str:
    key = f"{group_id}|{qq}"
    if key in _member_cache:
        return _member_cache[key]
    name = ""
    getter = getattr(client, "get_group_member_info", None)
    if getter is not None:
        try:
            info = await getter(group_id=int(group_id), user_id=int(qq))
            if isinstance(info, dict):
                name = str(info.get("card") or info.get("nickname") or "")
            else:
                name = str(getattr(info, "card", "") or getattr(info, "nickname", "") or "")
        except Exception:
            name = ""
    name = str(name or "").strip()
    _member_cache[key] = name
    return name



def _in_quiet_hours() -> bool:
    start = str(global_config.get("proactive_quiet_start", "23:00"))
    end = str(global_config.get("proactive_quiet_end", "08:00"))

    def to_minutes(hhmm):
        try:
            h, m = hhmm.split(":")[:2]
            return int(h) * 60 + int(m)
        except Exception:
            return None
    s, e, cur = to_minutes(start), to_minutes(end), to_minutes(time.strftime("%H:%M"))
    if s is None or e is None or cur is None or s == e:
        return False
    if s < e:
        return s <= cur < e
    return cur >= s or cur < e


def _parse_jitter_minutes(value) -> tuple:
    """解析主动消息时间抖动区间（分钟）。支持单个数字或区间，分隔符：~ ～ 空格 , ， 、"""
    text = str(value or "").strip()
    if not text:
        return 0.0, 0.0
    nums = []
    for part in re.split(r"[~～\s,，、]+", text):
        part = part.strip()
        if not part:
            continue
        try:
            nums.append(max(0.0, float(part)))
        except ValueError:
            continue
    if not nums:
        return 0.0, 0.0
    if len(nums) >= 2:
        return min(nums[0], nums[1]), max(nums[0], nums[1])
    return 0.0, nums[0]


# ============================================================================
# 回复生成（含流式）与句子发送
# ============================================================================

class SentenceSink:
    """流式回复的逐句发送器：句子入队，后台工作线程按顺序合成+发送。"""

    def __init__(self, session_type, target_id, emotions, ctx, last_reply="", user_text=""):
        self.session_type = session_type
        self.target_id = target_id
        self.emotions = emotions
        self.ctx = ctx
        refs = last_reply if isinstance(last_reply, (list, tuple)) else [last_reply]
        self.last_replies = [str(r) for r in refs if str(r or "").strip()]
        self.last_reply = self.last_replies[0] if self.last_replies else ""
        self.user_text = user_text or ""
        self.queue: asyncio.Queue = asyncio.Queue()
        self.worker: Optional[asyncio.Task] = None
        self.sent = 0
        self.sent_sentences: List[dict] = []  # 已成功发送的句子（流式中断时用于入库）
        self._tts_ok: Optional[bool] = None
        self.tts_ms = 0.0
        self.tts_calls = 0
        self.blocked = False
        self._first_checked = False
        self.pending_sticker_emotion = None
        self.pending_sticker_text = ""
        self.sticker_sent = False
        # 构造时就把防复读开关定下来：流式发送过程中配置不会变，
        # 逐句重读既要保证一致，也省得每次都走一遍配置读取
        self.guard = repeat_guard_flags(ctx)
        self.thresholds = repeat_thresholds(ctx)
        self.pacer = VoicePacer(bool(global_config.get("dynamic_sleep", True)))

    def _looks_repeat(self, sentence: dict, flags: dict = None) -> bool:
        zh = str(sentence.get("zh") or "").strip()
        if not zh:
            return False
        flags = flags or self.guard
        self_threshold, user_threshold = getattr(self, "thresholds", None) \
            or repeat_thresholds()
        if flags.get("compare_self", True):
            if any(_repeat_ratio(ref, zh) >= self_threshold
                   for ref in self.last_replies):
                return True
        if not flags.get("compare_user", True):
            return False
        u = str(self.user_text or "")
        if u and "[图片]" not in u and len(_norm_text(u)) >= _USER_ECHO_MIN_CHARS \
                and _repeat_ratio(u, zh) >= user_threshold:
            return True
        return False

    async def on_sentence(self, sentence: dict):
        if not self._first_checked:
            self._first_checked = True
            flags = self.guard
            if not flags.get("streaming", True):
                print("防复读：流式首句检查已关闭（repeat_guard_streaming_check=false），"
                      "首句不再拦截。")
            elif not repeat_guard_active(flags):
                print("防复读：比对对象全部关闭，流式首句检查无内容可比，跳过。")
            elif self._looks_repeat(sentence, flags):
                self.blocked = True
                print("流式首句疑似复读，已暂停发送，改为整段重新生成。")
                return
        if self.blocked:
            return
        if self.worker is None:
            self.worker = asyncio.create_task(self._run())
        await self.queue.put(sentence)

    async def _run(self):
        while True:
            item = await self.queue.get()
            if item is None:
                return
            try:
                if global_config and global_config.get("separate_send", False) \
                        and (global_config.get("send_voice_separately", False)
                             or global_config.get("text_separate", False)) \
                        and global_config.get("separate_force_segment", True):
                    for piece in segment_for_tts([item]):
                        await self._send_one(piece)
                else:
                    await self._send_one(item)
            except Exception as e:
                print(f"流式发送单句异常: {type(e).__name__}: {e}")
            finally:
                self.queue.task_done()

    async def _tts_available(self) -> bool:
        if self._tts_ok is None:
            self._tts_ok = await ensure_tts_service(global_config)
            if not self._tts_ok:
                print("警告：TTS 服务不可用，流式回复降级为纯文本。")
        return self._tts_ok and global_config.get("tts_reply_enabled", True)

    async def _speech_text(self, sentence: dict) -> str:
        """流式合成前的台词语言兜底：模型把展示语言填进台词字段时先重译。"""
        text = str(sentence.get("lang") or "")
        target = str((self.ctx.get("text_lang", "") if self.ctx else "") or "").strip().lower()
        if not target or target == "auto" or not lang_text_broken(text, target):
            return text
        source = str(sentence.get("zh") or "").strip() or text
        try:
            fixed = await asyncio.wait_for(translate_to_lang(self.ctx, source, target),
                                           timeout=30)
        except Exception as e:
            print(f"流式台词语言修复失败（忽略）: {type(e).__name__}: {e}")
            fixed = ""
        if fixed:
            print(f"流式台词语言修复：该句不是{target}，已重译为 {fixed[:40]!r}")
            return fixed
        return text

    async def _send_one(self, sentence: dict):
        if sticker_mgr and self.sent == 0 and self.pending_sticker_emotion is None:
            self.pending_sticker_emotion = sentence.get("emotion", "")
            self.pending_sticker_text = str(sentence.get("display")
                                            or sentence.get("zh") or "")
        wav = None
        if await self._tts_available():
            start = time.time()
            # 参数顺序：text=要念的台词(sentence["lang"])，emotion=情绪名。
            # 早期版本此处传反，情绪被当成台词、台词被当成情绪，
            # 导致情绪永远回退 default_voice（语音情绪与文本标注不一致）。
            wav = await synthesize_sentence(self.ctx, await self._speech_text(sentence),
                                            sentence.get("emotion", ""),
                                            self.emotions, memory_manager.data_path,
                                            stats=stats_mgr,
                                            mimic=sentence.get("mimic", ""),
                                            mimics=available_mimics(self.ctx))
            self.tts_ms += (time.time() - start) * 1000
            if wav:
                self.tts_calls += 1
        shown = str(sentence.get("display") or sentence.get("zh") or "")
        if wav:
            try:
                # 节奏器只在"还有下一条语音要发"时才真正等：最后一句发完立刻返回，
                # 否则这一段播放等待会一直占着会话锁，拖住排队的下一条消息
                await self.pacer.wait()
                await sender.send_text(self.session_type, self.target_id, shown)
                await sender.send_voice(self.session_type, self.target_id, wav)
                await self.pacer.hold_voice(wav)
            finally:
                Path(wav).unlink(missing_ok=True)
        else:
            await sender.send_text(self.session_type, self.target_id, shown)
        self.sent += 1
        self.sent_sentences.append(sentence)

    async def _send_pending_sticker(self):
        if self.sticker_sent or not self.pending_sticker_emotion or sticker_mgr is None:
            return
        self.sticker_sent = True
        sticker = await sticker_mgr.pick_async(self.ctx, self.pending_sticker_emotion,
                                               getattr(self, "pending_sticker_text", ""))
        if sticker:
            await sender.send_text(self.session_type, self.target_id, "", sticker=sticker)

    async def flush(self):
        if self.worker is not None:
            await self.queue.put(None)
            try:
                await self.worker
            except Exception as e:
                print(f"流式发送工作器异常: {e}")
            self.worker = None
        try:
            await self._send_pending_sticker()
        except Exception as e:
            print(f"流式表情包发送异常: {type(e).__name__}: {e}")

    async def abort(self):
        """放弃本轮流式发送：停掉工作器并丢弃未发送的句子。

        生成超时/异常时若只是 return，工作器会永远阻塞在 queue.get() 上，
        引用链（sender/ctx/emotions）不释放，每失败一次泄漏一个后台任务。
        """
        task, self.worker = self.worker, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
                self.queue.task_done()
            except Exception:
                break


def _tool_notes_from_trace(tool_trace) -> str:
    """把本轮工具调用结果浓缩成历史备注（随助手消息持久化）。

    事故背景：工具结果只存在于当轮请求里，不进会话历史；用户追问"你唱一段我听听"时
    模型上下文里已经没有歌词内容，只能再搜一遍。存一份精简备注后，
    build_merged_history 会在下一轮把它回放进上下文，追问可直接引用，不再重复搜索。
    条数与单条字数可调：tool_notes_max_entries / tool_notes_max_chars。
    """
    try:
        max_notes = max(1, int(global_config.get("tool_notes_max_entries", 4) or 4))
        note_chars = max(100, int(global_config.get("tool_notes_max_chars", 500) or 500))
    except (AttributeError, TypeError, ValueError):
        max_notes, note_chars = 4, 500
    notes = []
    for t in (tool_trace or []):
        if not isinstance(t, dict) or not t.get("ok"):
            continue
        try:
            args = json.dumps(t.get("arguments", {}), ensure_ascii=False)[:120]
        except Exception:
            args = ""
        output = str(t.get("output", "")).strip()[:note_chars]
        if output:
            notes.append(f"{t.get('name', '?')}({args}) → {output}")
        if len(notes) >= max_notes:
            break
    return "\n---\n".join(notes)


def _tool_requested(user_text: str, ctx) -> bool:
    mode = str(ctx.get("tools_trigger_mode", "keyword") or "keyword").strip().lower()
    if mode in ("llm", "llm_auto", "auto"):
        # LLM 自主判断模式：模型手边有工具就什么都想调，所以先做一次意图判定。
        # 只有消息确实带客观信息需求（时间/计算/天气/搜索/链接/实体提问…）才进工具流程；
        # 纯寒暄、纯情绪、纯角色扮演直接走普通回复，回复节奏不被工具拖慢。
        if not bool(ctx.get("tools_guard_enabled", True)):
            return True
        return text_needs_tools(user_text) or is_search_dissatisfied(user_text)
    if mode == "always":
        return True
    if not ctx.get("tools_guard_enabled", True):
        return True
    low = strip_quote_note(str(user_text or "")).lower()
    if "://" in low:
        return True
    if is_search_request(user_text) or is_search_dissatisfied(user_text):
        return True
    keywords = str(ctx.get("tools_guard_keywords", "") or "").split("\n")
    kws = [k.strip().lower() for k in keywords if k.strip()]
    if not kws:
        return True
    if any(kw in low for kw in kws):
        return True
    # 实体类提问（"X是谁""知道X吗"）即使没命中触发词也要进工具流程：
    # 这类问题问的多半是模型不认识的具体人/事/物，不搜索就只能装认识或装没听说过
    entity_q = ("是谁", "是什么", "什么是", "什么叫", "听说过", "了解吗", "认识吗",
                "知道吗", "怎么回事", "哪里人")
    if any(kw in low for kw in entity_q):
        return True
    return False


_URL_RE = re.compile(r"https?://[^\s\"'<>，,。）)】]+")
_LINK_REQUEST_RE = re.compile(r"链接|网址|[Uu][Rr][Ll]|下载地址|官网|地址")
_SEARCH_ENTRY_URL_RE = re.compile(r"网址[:：]\s*(\S+)")
_SEARCH_ENTRY_ABS_RE = re.compile(r"摘要[:：][^\n]*")
_SEARCH_ENTRY_TITLE_RE = re.compile(r"^\s*\d+[.、]\s*(.+)$", re.M)


def _search_entries(output) -> list:
    """从搜索结果文本中按顺序取出 (链接, 该条目的标题+摘要上下文)。

    条目结构是「标题 / 网址行 / 摘要行」，区间以上一条摘要行结尾为起点、
    本条摘要行结尾为终点，避免上一条的摘要混进下一条的上下文。
    """
    text = str(output or "")
    urls = list(_SEARCH_ENTRY_URL_RE.finditer(text))
    abstracts = list(_SEARCH_ENTRY_ABS_RE.finditer(text))
    entries = []
    for i, m in enumerate(urls):
        seg_start = abstracts[i - 1].end() if 0 < i < len(abstracts) + 1 and i - 1 < len(abstracts) else 0
        if i < len(abstracts):
            seg_end = abstracts[i].end()
        else:
            seg_end = urls[i + 1].start() if i + 1 < len(urls) else len(text)
        entries.append((m.group(1).rstrip(".,;、"), text[seg_start:seg_end]))
    return entries


def _search_entry_titles(output) -> list:
    """取出每条搜索结果的标题（与网址一一对应，用于判断模型是否真的提过这条）。"""
    titles = []
    for line in str(output or "").splitlines():
        m = re.match(r"^\s*\d+[.、]\s*(\S.*)$", line.strip())
        if m:
            title = re.sub(r"\s*[-_|｜]\s*[^-_|｜]{1,20}$", "", m.group(1).strip())
            titles.append(title.strip() or m.group(1).strip())
    return titles


def _entry_title_terms(title: str) -> list:
    return [t for t in re.split(r"[\s\-_|｜:：,，。.、()（）\[\]【】]+", str(title or ""))
            if len(t) >= 3]


def _title_mentioned(reply_text: str, title: str, url: str) -> bool:
    """回复里是否真的提到过这条结果（标题核心词或域名出现即算）。"""
    text = str(reply_text or "")
    if not text:
        return False
    for term in _entry_title_terms(title):
        if term in text:
            return True
    host = ""
    m = re.match(r"https?://([^/]+)", str(url or ""))
    if m:
        host = m.group(1)
        for piece in host.split("."):
            if len(piece) >= 4 and piece.lower() not in ("www", "com", "html") \
                    and piece in text:
                return True
    return False


def _reply_terms(sentences) -> set:
    """从回复台词里提取可比对的词：拉丁词/数字 + 中文二元组。"""
    text = "".join(str(s.get("zh", "") or "") for s in sentences)
    terms = set(re.findall(r"[A-Za-z0-9]{2,}", text))
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        for i in range(len(chunk) - 1):
            terms.add(chunk[i:i + 2])
    return terms


def _append_missing_links(sentences, user_text, tool_trace, session_id: str = ""):
    """把搜索结果里的链接补给回复；只补"模型确实提到过"的那几条。

    search_links_auto_append 开启时：回复里没写链接，就从本轮搜索结果的标题/域名
    里找模型提到过的那几条补上；一条都提不到就不补——宁可不给链接，也不给
    与回复内容无关的链接。用户明确索要链接时放宽一档，但仍按相关度排序取前几条。
    """
    if not sentences:
        return sentences
    if not global_config.get("search_links_auto_append", True):
        return sentences
    if any(_URL_RE.search(str(s.get("display", "") or "")) for s in sentences):
        return sentences
    wants_link = bool(_LINK_REQUEST_RE.search(str(user_text or "")))
    reply_text = "".join(str(s.get("display", "") or "") + str(s.get("zh", "") or "")
                         for s in sentences)
    seen = {u.rstrip("/") for u in sent_links(session_id)}
    entries, urls_seen = [], set()
    for t in tool_trace or []:
        if t.get("name") != "web_search" or not t.get("ok"):
            continue
        output = str(t.get("output", ""))
        titles = _search_entry_titles(output)
        for index, (url, context) in enumerate(_search_entries(output)):
            if not url or url in urls_seen or url.rstrip("/") in seen:
                continue
            urls_seen.add(url)
            title = titles[index] if index < len(titles) else ""
            entries.append((url, title, context))
    if not entries:
        if wants_link:
            print("搜索链接补发：本轮搜索结果里的链接此前都已发过，本次不重复发送。")
        return sentences
    mentioned = [(url, context) for url, title, context in entries
                 if _title_mentioned(reply_text, title, url)]
    picked = []
    if mentioned:
        picked = [url for url, _ctx in mentioned]
    elif wants_link:
        terms = _reply_terms(sentences)
        scored = []
        for url, title, context in entries:
            score = sum(1.0 for term in terms if term in context)
            if title and _title_mentioned(reply_text, title, url):
                score += 5.0
            if score > 0:
                scored.append((score, url))
        scored.sort(key=lambda x: -x[0])
        if scored:
            best = scored[0][0]
            picked = [url for score, url in scored if score >= best * 0.5]
    try:
        limit = max(1, int(global_config.get("search_links_max", 3) or 3))
    except (TypeError, ValueError):
        limit = 3
    picked = picked[:limit]
    if not picked:
        return sentences
    last = sentences[-1]
    last["display"] = (str(last.get("display", "") or "").rstrip()
                       + "\n" + " ".join(picked)).strip()
    record_sent_links(session_id, picked)
    print(f"搜索链接补发：{len(picked)} 条 → {' '.join(picked)}")
    return sentences


async def _sticker_judgement_from_llm(ctx: RoleContext, image_result: dict) -> Optional[dict]:
    """中立地判定这张图的收藏分类与用途名，返回 {"category": str, "name": str}。

    只依据画面的客观描述判断：识图调用带着角色人设、对话历史与当前心情，
    模型会站在角色立场上给图归类与命名。判定失败返回 None（调用方沿用原分类），
    判定不适合当表情包时 category 为空串。
    """
    description = str(image_result.get("description", "") or "").strip()
    if not description:
        return None
    from modules.stickers import (category_candidates, category_candidates_text,
                                  CATEGORY_DISAMBIGUATION)
    # 候选分类取自表情库目录下实际存在的子文件夹，不预设固定的分类名
    cats = category_candidates(ctx)
    classify_prompt = (
        f"图片内容：{description}\n"
        "你是中立的图库管理员：只根据这张图自身的画面与文字判断它的用途，"
        "不要代入任何角色，也不要考虑对话里任何人的情绪。\n"
        "请判断这张图【将来被当作表情包发出去时，发图一方的情绪/使用场景】"
        "（使用者发图时的语气，不是画面中角色此刻的情绪）。"
        "画面人物的动作、表情与文字往往指向互动用途（挑逗、撩、调戏、嘲讽、炫耀、"
        "撒娇等），要据此归类。"
        "如果这张图其实是纯风景/空镜/静物/无文字无表情的随手拍，"
        "或者画面阴森、恐怖、诡异、病态、压抑，且没有明确的互动用途，"
        "就回答 none 表示不适合当表情包，不要硬选一个分类。"
        f"{CATEGORY_DISAMBIGUATION}"
        "可以收藏时，先逐个比较下面这些分类文件夹的适用范围，再选出最贴合的一个。"
        "分类清单取自表情库目录下实际存在的文件夹：\n"
        f"{category_candidates_text(ctx)}\n"
        "只输出一个 JSON 对象，不要其他任何内容："
        '{"category": "分类名（原样照抄上面的清单）；不适合当表情包则填 none", '
        '"name": "用途名"}。'
        "name 是这张表情以后反复使用时的名字：只写它适合表达的情绪与互动用途，"
        "简短（6~12 个字），不写成句子，不写画面里是谁、也不写是给谁用的；"
        "严禁出现角色名、人名、作品名，也不能是「好看」「有趣」「可爱」这类空话；"
        "category 为 none 时 name 留空。"
    )
    try:
        result = await chat_once(ctx, [{"role": "user", "content": classify_prompt}])
    except Exception as e:
        print(f"表情分类失败: {type(e).__name__}: {e}")
        return None
    raw = str(result.get("content") or "").strip()
    obj = extract_json(raw) or {}
    name = str(obj.get("name") or "").strip()
    category = str(obj.get("category") or "").strip().lower()
    if category in cats:
        return {"category": category, "name": name}
    # 结构化字段缺失或非法时按整段输出兜底。分类名之间互不为子串，
    # 所以命中多个说明模型只是在解释（"不是 A，是 B"），此时不能挑一个当答案。
    text = category or raw.lower()
    if re.search(r"\bnone\b|不适合|不收藏|无法归类|没有互动用途", text) \
            and not any(c in text for c in cats):
        print(f"【表情收藏-分类】模型判定不适合当表情包：{raw[:60]!r}")
        return {"category": "", "name": ""}
    hits = [c for c in cats if re.search(rf"\b{c}\b", text)]
    if len(hits) == 1:
        return {"category": hits[0], "name": name}
    if len(hits) > 1:
        print(f"【表情收藏-分类】输出里出现多个分类（{'、'.join(hits)}），无法确定，按不收藏处理。")
        return {"category": "", "name": ""}
    print("【表情收藏-分类】未能得到有效分类，按不收藏处理（不再默认 pingjing）。")
    return {"category": "", "name": ""}


async def generate_reply(ctx: RoleContext, emotions: dict, user_text: str, history: list,
                         images: Optional[list], extra_parts: List[str],
                         user_id: str = "", on_sentence=None,
                         session_id: str = "", describe_only: bool = False) -> Optional[dict]:
    """生成回复：识图 / 工具调用 / 流式 / 普通四种路径统一入口。

    describe_only=True 只用于「本轮不发消息、但要把图看进历史」的场景：
    识图只出画面描述与收藏判定，不产出台词，也不会降级成文本回复。
    """
    if images is not None and not str(user_text or "").strip():
        user_text = "[图片]"
    if images is not None:
        result = await get_image_reply(ctx, user_text, history, emotions, images,
                                       extra_parts=extra_parts, stats=stats_mgr,
                                       describe_only=describe_only)
        if result is not None:
            out = {"sentences": result.get("sentences", []), "llm_ms": result.get("ms", 0),
                   "tool_trace": []}
            if result.get("description"):
                out["description"] = result["description"]
            if result.get("capture"):
                out["capture"] = result["capture"]
            if result.get("capture_image"):
                out["capture_image"] = result["capture_image"]
            return out
        if describe_only:
            return None
        # 识图失败（模型未配置/服务异常），降级为普通文本回复，避免用户消息石沉大海
        print("识图失败，降级为普通文本回复。")

    messages = build_chat_messages(ctx, user_text, history, emotions, extra_parts,
                                   trailing_notes=([image_identity_note(ctx)]
                                                   if images is not None
                                                   and global_config.get("image_identity_guard_enabled", True)
                                                   else None))

    # 工具调用路径（非流式，保证 tool_calls 正确处理）：无明确工具需求时走普通回复，避免误调
    if tool_registry and global_config.get("tools_enabled", False) \
            and tool_registry.has_enabled_tools() and _tool_requested(user_text, ctx):
        tool_registry.begin_reply()
        result = await chat_with_tools(ctx, messages, tool_registry, stats=stats_mgr,
                                       user_id=user_id, session_key=session_id)
        try:
            tool_log_cap = int(global_config.get("tool_log_output_chars", 0) or 0)
        except (AttributeError, TypeError, ValueError):
            tool_log_cap = 0
        for t in result.get("tool_trace", []):
            output_text = str(t["output"])
            if tool_log_cap > 0 and len(output_text) > tool_log_cap:
                # 0 = 完整输出（默认）。之前写死只打 80 字，搜索的 8 条结果在日志里
                # 只能看到第 1 条，用户会误以为"只搜到一个结果"。
                output_text = output_text[:tool_log_cap] \
                    + f"…（剩余 {len(str(t['output'])) - tool_log_cap} 字省略，tool_log_output_chars 可调）"
            print(f"[工具调用] {t['name']} ok={t['ok']} → {output_text}")
        sentences = _append_missing_links(
            normalize_sentences(result["content"], ctx, emotions, user_text),
            user_text, result.get("tool_trace"), session_id)
        return {"sentences": sentences, "llm_ms": result["ms"], "tool_trace": result["tool_trace"],
                "llm_calls": int(result.get("llm_calls", 1)),
                "tool_calls": len([t for t in result.get("tool_trace", []) if t.get("ok")])}

    # 流式路径
    if global_config.get("streaming_enabled", False):
        return await generate_reply_stream(ctx, emotions, user_text, messages, on_sentence)

    # 普通路径
    result = await chat_once(ctx, messages)
    if stats_mgr:
        stats_mgr.record_llm(result["ms"])
    sentences = normalize_sentences(result["content"], ctx, emotions, user_text)
    return {"sentences": sentences, "llm_ms": result["ms"], "tool_trace": [],
            "llm_calls": 1, "tool_calls": 0}


async def generate_reply_stream(ctx: RoleContext, emotions: dict, user_text: str,
                                messages: list, on_sentence=None) -> dict:
    parser = SentenceStreamParser()
    sentences = []
    first_ms = None
    start = time.time()
    try:
        async for chunk in stream_chat(ctx, messages):
            if chunk.get("first_token_ms") and first_ms is None:
                first_ms = chunk["first_token_ms"]
            delta = chunk.get("delta") or ""
            if not delta:
                continue
            for obj in parser.feed(delta):
                if not sentence_obj_has_text(obj):
                    continue  # 只有 emotion 等元数据的空句子对象：跳过，避免念出兜底台词
                sentence = normalize_single(obj, ctx, emotions, user_text)
                # 模型可能把多句塞进同一元素；按句号拆分后逐句流式发送
                for piece in split_multi_clause_sentences([sentence]):
                    sentences.append(piece)
                    if on_sentence:
                        await on_sentence(piece)
    except Exception as e:
        print(f"流式请求异常: {type(e).__name__}: {e}（已收到的内容将继续处理）")
    # 流结束兜底：一句未产出时整段规整（宽容修复/JSON防线）；
    # 已产出时抢救末尾被截断的半句
    for piece in parser.finish(ctx, user_text, emotions):
        sentences.append(piece)
        if on_sentence:
            await on_sentence(piece)
    total_ms = (time.time() - start) * 1000
    if stats_mgr:
        stats_mgr.record_llm(first_ms or total_ms)
    return {"sentences": sentences, "llm_ms": total_ms, "tool_trace": [],
            "llm_calls": 1, "tool_calls": 0}


def role_connection_snapshot(config) -> tuple:
    """各角色独占连接的快照，用来判断配置保存后是否需要重连。"""
    out = []
    for key, role in (getattr(config, "roles", None) or {}).items():
        role = role or {}
        out.append((str(key), str(role.get("napcat_ws_url") or ""),
                    str(role.get("napcat_token") or "")))
    return tuple(sorted(out))


def build_connection_profiles(config) -> List[dict]:
    """要建立的 NapCat 连接清单：一条默认连接，加上每个配了独立连接的角色。

    角色没填 napcat_ws_url 就共用默认连接；填得和默认地址一样时不重复连接。
    """
    ws_url = str(config.get("napcat_ws_url", "") or "ws://127.0.0.1:3001")
    token = str(config.get("napcat_token", "") or "")
    out = [{"key": "default", "label": "默认连接",
            "ws_url": ws_url, "token": token, "role_key": ""}]
    for key, role in (getattr(config, "roles", None) or {}).items():
        role = role or {}
        r_url = str(role.get("napcat_ws_url") or "").strip()
        if not r_url:
            continue
        r_token = str(role.get("napcat_token") or "")
        if r_url == ws_url and r_token == token:
            continue
        out.append({"key": f"role:{key}", "label": f"角色 {key}",
                    "ws_url": r_url, "token": r_token, "role_key": key})
    return out


def resolve_target_roles(user_text: str, is_private: bool,
                         source_role_key: str = "") -> List[dict]:
    """多角色路由：账号绑了角色就用它；否则群聊按消息中出现的角色名路由。"""
    key = str(source_role_key or "").strip()
    if key:
        role = (global_config.roles or {}).get(key)
        if role:
            return [role]
    active = get_active_role()
    if not active:
        return []
    if is_private or not global_config.get("multi_role_enabled", False):
        return [active]
    matched = []
    for role in global_config.roles.values():
        name = str(role.get("character_name", "")).strip()
        if name and name in user_text:
            matched.append(role)
    if not matched:
        return [active]
    try:
        cap = max(1, int(global_config.get("multi_role_max_replies", 2)))
    except (TypeError, ValueError):
        cap = 2
    return matched[:cap]


# ============================================================================
# 消息处理主管线
# ============================================================================

def _spawn(coro):
    """安全的后台任务封装，异常仅记录不中断主流程。"""
    async def _wrapper():
        try:
            await coro
        except Exception as e:
            import traceback
            print(f"后台任务异常: {type(e).__name__}: {e}")
            traceback.print_exc()
    return asyncio.create_task(_wrapper())


def _norm_text(s: str) -> str:
    t = re.sub(r"[\s\u3000]+", "", str(s or ""))
    return re.sub(r"[，。！？、；：,.!?;:'\"“”‘’（）()\[\]【】…~～\-—_*#@/\\]+", "", t)


def _repeat_ratio(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    an, bn = _norm_text(a), _norm_text(b)
    if not an or not bn:
        return 0.0
    blocks = SequenceMatcher(None, an, bn).get_matching_blocks()
    matched = sum(blk.size for blk in blocks)
    return matched / min(len(an), len(bn))


def _strip_image_claims(sentences: list) -> list:
    """剔除把用户发来的图认领成角色自己的那些句子。"""
    return [s for s in (sentences or [])
            if not image_self_claim(str(s.get("zh", "") or ""))]


def _recent_assistant_replies(history: list, rounds: int) -> list:
    """按时间倒序取最近 rounds 条角色回复（跳过空内容）。"""
    out = []
    for msg in reversed(history or []):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = str(msg.get("content", "") or "").strip()
        if content:
            out.append(content)
        if len(out) >= max(1, rounds):
            break
    return out


def _max_repeat_ratio(reply_text: str, references: list) -> tuple:
    """返回 (最高重合率, 与之最像的那句参照文本)。"""
    best, target = 0.0, ""
    for ref in references or []:
        if not ref:
            continue
        ratio = _repeat_ratio(ref, reply_text)
        if ratio > best:
            best, target = ratio, ref
    return best, target


_USER_ECHO_MIN_CHARS = 5
_RETRY_TEMPERATURE = 1.3
_RETRY_TEMPERATURE_MAX = 1.4

# 防复读的四个独立开关（默认全开 = 原来的行为）。
# 「什么时候查」与「跟谁比」是两个维度，各自可单独关闭：
#   streaming / regen  —— 检查时机
#   compare_self / compare_user —— 比对对象
_REPEAT_GUARD_KEYS = {
    "streaming": "repeat_guard_streaming_check",
    "regen": "repeat_guard_regen_check",
    "compare_self": "repeat_guard_compare_self",
    "compare_user": "repeat_guard_compare_user",
}


def repeat_thresholds(ctx=None) -> tuple:
    """(自重复系数, 复述用户系数)：重合度达到该值即判定为重复，越大越宽容。"""
    src = ctx if ctx is not None else global_config
    values = []
    for key, fallback in (("repeat_guard_self_threshold", 0.85),
                          ("repeat_guard_user_threshold", 0.8)):
        try:
            value = float(src.get(key, fallback))
        except (TypeError, ValueError):
            value = fallback
        values.append(min(1.0, max(0.0, value)))
    return values[0], values[1]


def repeat_guard_flags(ctx=None) -> dict:
    """读取四个防复读开关；取不到时按开启处理（保持原行为）。"""
    src = ctx if ctx is not None else global_config
    flags = {}
    for name, key in _REPEAT_GUARD_KEYS.items():
        try:
            flags[name] = bool(src.get(key, True))
        except Exception:
            flags[name] = True
    return flags


def repeat_guard_summary(flags: dict) -> str:
    """把关闭的开关写成一行提示；全开时返回空串。"""
    names = {"streaming": "流式首句检查", "regen": "整段生成后校验",
             "compare_self": "比对角色历史回复", "compare_user": "比对用户本条消息"}
    off = [label for name, label in names.items() if not flags.get(name, True)]
    if not off:
        return ""
    return "（已关闭：" + "、".join(off) + "）"


def repeat_guard_active(flags: dict) -> bool:
    """是否还有任何一维在生效：比对对象全关掉时，整套防复读等于没开。"""
    return bool(flags.get("compare_self", True) or flags.get("compare_user", True))


def _log_mood_commit(new_mood, verdict):
    """心情变化量落盘后打一行日志；本轮无需更新时（new_mood 为 None）什么都不做。"""
    if new_mood is None:
        return
    before = float(verdict.get("mood", new_mood) or 0)
    print(f"心情更新：{before:.0f} → {float(new_mood):.0f}")


def _image_reply_override(verdict, has_image: bool) -> bool:
    """带图消息是否要忽略审判的「无需回复」判定。

    审判看不到画面：本条带图时它的"不用回"没有依据（消息里可能除了图片一个字都
    没有）。心情决定的回复概率不属于此列，照常生效。
    """
    return bool(has_image and verdict is not None and not verdict["should_reply"]
                and not verdict.get("llm_reply", True))


async def auto_capture_from_images(ctx: RoleContext, capture: dict, image_urls: list,
                                   image_result: Optional[dict] = None):
    from modules.stickers import auto_capture_image
    # 识图时已经把原图读进内存了，优先用这份字节：QQ 图床直链的 rkey 很短命，
    # 到这里再下载一次经常已经 403/400，收藏就会在下载这一步悄悄失败。
    preloaded = (image_result or {}).get("capture_image") or {}
    image_data = preloaded.get("data")
    source = str(preloaded.get("source") or "")
    if not image_data:
        for s in image_urls:
            s = str(s)
            if s.startswith(("http://", "https://")):
                source = s
                break
            try:
                if Path(s).exists():
                    source = s
                    break
            except Exception:
                continue
    if not image_data and not source:
        print("[表情收藏] 跳过：这条消息没有可用的图片来源（既无本地文件也无可下载链接）。")
        return

    try:
        min_score = 0.0
        try:
            min_score = float(ctx.get("sticker_capture_min_score", 0.7) or 0)
        except Exception:
            min_score = 0.7
        score = 0.0
        try:
            score = float(capture.get("score", 0) or 0)
        except Exception:
            score = 0.0
        if score < min_score:
            print(f"[表情收藏] 有趣度评分不足（{score:.2f} < {min_score:.2f}），跳过保存。")
            return
        category = str(capture.get("category", "") or "").strip()
        reason = str(capture.get("reason", "") or "")
        # 分类与命名一律以中立判定为准：识图那次调用处在角色立场上，归类会被角色
        # 此刻的情绪带偏；只有中立判定失败时才沿用识图给出的结果。
        judgement = await _sticker_judgement_from_llm(ctx, image_result) if image_result else None
        if judgement is not None:
            category = judgement["category"]
            if judgement["name"]:
                reason = judgement["name"]
        # 分类判定为"不适合当表情包"时直接跳过：
        # 以前这种情况会被兜底塞进 wuyu 目录，等于把无关图片污染表情库。
        if not category and bool(ctx.get("sticker_capture_skip_if_unfit", True)):
            print("【表情收藏】这张图未被判定为适合当表情包，跳过收藏。")
            return
        print(f"【表情收藏-自动触发】图片来源: {str(source)[:50]}... 原始分数: {score}, "
              f"分类: {category or '(空→兜底分类)'}, "
              f"图片字节: {'复用识图已读入的' if image_data else '需重新下载'}")
        await auto_capture_image(ctx, sticker_mgr, source, category, image_data=image_data,
                                 reason=reason)
    except Exception as e:
        print(f"表情收藏失败: {type(e).__name__}: {e}")


def _sticker_capture_args(reply: Optional[dict], image_sources: list) -> Optional[dict]:
    """识图结果里的收藏判定能否落盘：能则返回 auto_capture_from_images 的入参，不能则 None。

    条件集中在这里：收藏触发同时挂在「正常回复」与「审判判不回」两条路径上，
    判定写两份迟早会漏掉一处（漏掉的那条路径会静默不收藏）。
    """
    capture = (reply or {}).get("capture") or {}
    if not capture.get("should") or not image_sources or sticker_mgr is None:
        return None
    return {"capture": capture, "image_urls": list(image_sources),
            "image_result": {"description": (reply or {}).get("description", ""),
                             "capture_image": (reply or {}).get("capture_image")}}


def _spawn_sticker_capture(ctx: RoleContext, reply: Optional[dict], image_sources: list) -> None:
    args = _sticker_capture_args(reply, image_sources)
    if args is None:
        return
    _spawn(auto_capture_from_images(ctx, args["capture"], args["image_urls"],
                                    args["image_result"]))


def pick_image_source(seg) -> Optional[str]:
    """从图片消息段里挑一个可用的图片来源。

    优先级：本地缓存文件 > 网络 URL > 原始 file 标识。
    最后一项是"裸文件名"（如 1E4D5FAB.image，文件并不在本地也不是 URL）：
    此时不能返回 None，否则调用方既拿不到图、日志里也看不出是哪张图出了问题；
    保留原值可以让后续 download/日志明确指出问题来源，再由识图链路自行跳过。
    """
    # 优先使用本地缓存文件（如果有）
    local_path = getattr(seg, "path", None) or getattr(seg, "file", None)
    if local_path and os.path.isfile(str(local_path)):
        return str(local_path)
    # 回退到网络 URL
    url = getattr(seg, "url", None)
    if url:
        return str(url)
    raw_id = getattr(seg, "file", None) or getattr(seg, "path", None)
    if raw_id:
        print(f"图片来源无法解析（本地文件不存在且无 URL），保留原值便于排查: {str(raw_id)[:120]}")
        return str(raw_id)
    return None

async def refresh_image_urls(client, image_urls: list, file_ids: dict) -> list:
    """QQ NT 图床直链带 rkey，有效期很短，过期后返回 400。
    优先走 OneBot get_image 换取本地缓存文件或新链接；
    失败时下载一次落盘缓存，仍失败则丢弃该链接，避免识图链路反复 400。"""
    getter = getattr(client, "get_image", None)
    refreshed = []
    for src in image_urls:
        if not (src.startswith(("http://", "https://")) and "rkey=" in src):
            refreshed.append(src)
            continue
        fresh = None
        if getter is not None:
            fid = file_ids.get(src) or ""
            if fid:
                try:
                    r = await getter(file=str(fid))
                    if isinstance(r, dict):
                        local = r.get("file")
                        if local and os.path.isfile(str(local)):
                            fresh = str(local)
                        else:
                            url = r.get("url")
                            if url and str(url).startswith(("http://", "https://")):
                                fresh = str(url)
                except Exception as e:
                    print(f"get_image 刷新图片链接失败: {type(e).__name__}: {e}")
        if not fresh:
            fresh = await _download_image_to_cache(src)
        if fresh:
            print(f"图片直链已刷新: {fresh[:120]}")
            refreshed.append(fresh)
        else:
            print(f"图片直链已失效且无法刷新，已跳过: {src[:120]}")
    return refreshed


_IMAGE_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
              "image/bmp": ".bmp", "image/webp": ".webp"}


# ============================================================================
# 「用户刚发的图还没被真正看过」的状态
# ============================================================================
# 事故背景：用户发一张图（比如示意图/表情包），回复审判判定"这条不需要回复"，
# 于是识图链路整个没跑、画面描述也没回填进历史（历史里只留 "[图片]"）。
# 用户紧接着发"这个怎么样""那这个呢"这类相关追问时，系统又只当纯文本处理，
# 识图模型完全没被调用——回复节奏和上下文全对不上。
# 现在：图片消息在"没有真正产出画面描述"时记下来；紧接着的下一条消息若和
# 这张图相关，就把识图模型叫回来重跑一次。
_PENDING_IMAGES: Dict[str, dict] = {}
_PENDING_IMAGE_TTL = 900.0        # 15 分钟内的图才算"刚发的"
_PENDING_IMAGE_MAX_TRIES = 2      # 同一张图最多为追问重跑几次识图，避免反复触发

# 明确指向"上一张图"的说法：命中就直接重跑识图模型
_IMAGE_REF_RE = re.compile(
    r"这张|那张|这个图|那个图|这图|那图|图上|图里|图片|照片|截图|表情包|"
    r"刚才(?:发|那|的)|刚发|上一条|上面(?:那|这)|之前(?:发|那)|看看这个|"
    r"这个怎么样|如何呢|いい|これ|さっき|さっきの|この(?:画像|写真|絵)|その(?:画像|写真)")


def _remember_pending_image(session_id: str, image_urls: list, file_ids: dict,
                            user_text: str = ""):
    """记下"用户刚发了一张还没被看过的图"，供下一条相关消息复用。"""
    if not session_id or not image_urls:
        return
    _PENDING_IMAGES[session_id] = {
        "urls": list(image_urls), "file_ids": dict(file_ids or {}),
        "ts": time.time(), "text": str(user_text or "")[:200],
    }


def _take_pending_image(session_id: str, user_text: str) -> Optional[dict]:
    """取回"刚发过、还没被真正看过"的图片，供本条追问复用识图模型。

    只在用户本条消息明确指向那张图时复用（"这个怎么样""图里是谁""刚才那张"…），
    避免给纯文字闲聊硬塞一张旧图；同一张图最多复用两次，防止无限重跑。
    """
    info = _PENDING_IMAGES.get(session_id)
    if not info:
        return None
    if time.time() - float(info.get("ts", 0)) > _PENDING_IMAGE_TTL:
        _PENDING_IMAGES.pop(session_id, None)
        return None
    if not _IMAGE_REF_RE.search(str(user_text or "")):
        return None
    if int(info.get("tries", 0)) >= _PENDING_IMAGE_MAX_TRIES:
        _PENDING_IMAGES.pop(session_id, None)
        return None
    info["tries"] = int(info.get("tries", 0)) + 1
    return info


def _clear_pending_image(session_id: str):
    _PENDING_IMAGES.pop(session_id, None)


def _backfill_image_description(history: list, description: str) -> None:
    """把识图模型的画面描述回填到历史里最近一条用户消息上。

    旧实现写死 `history[-1]["content"] = ...`：只有当被回填的消息恰好是最后一条时
    才对；一旦本条消息是被识图叫回来的"追问"（历史里最后一条可能是别的），
    画面描述就丢了。改成按角色倒查，永远落在真正那条用户消息上。
    """
    desc = str(description or "").strip()
    if not history or not desc:
        return
    for msg in reversed(history):
        if msg.get("role") != "user":
            continue
        base = str(msg.get("content", ""))
        base = re.sub(r"\s*\[图片(?::[^\]]*)?\]", "", base).strip()
        msg["content"] = f"{base} [图片: {desc[:200]}]".strip()
        return


async def _download_image_to_cache(url: str) -> Optional[str]:
    if memory_manager is None:
        return None
    try:
        data = await download_image(str(url))
    except Exception as e:
        print(f"下载图片失败: {type(e).__name__}: {e}")
        return None
    mime = sniff_image_mime(data or b"")
    if not mime:
        return None
    try:
        target = memory_manager.data_path / f"temp_img_{time.time()}{_IMAGE_EXT.get(mime, '.jpg')}"
        target.write_bytes(data)
        return str(target)
    except Exception as e:
        print(f"缓存图片失败: {type(e).__name__}: {e}")
        return None


async def fetch_quoted_context(client, reply_seg) -> Optional[dict]:
    """回查被引用消息的内容与发送者。"""
    getter = getattr(client, "get_msg", None)
    if getter is None:
        return None
    resp = None
    last_err = None
    for msg_id in (getattr(reply_seg, "id", None), getattr(reply_seg, "seq", None)):
        if msg_id in (None, ""):
            continue
        try:
            r = await getter(message_id=msg_id)
            if isinstance(r, dict) and (r.get("message") or r.get("raw_message")):
                resp = r
                break
        except Exception as e:
            last_err = e
    if resp is None:
        if last_err is not None:
            print(f"获取引用消息失败: {type(last_err).__name__}: {last_err}")
        return None
    sender = resp.get("sender") or {}
    who = str(sender.get("nickname") or sender.get("user_id") or "")
    parts = []
    quoted_image_urls = []
    quoted_image_file_ids = {}
    for s in (resp.get("message") or []):
        seg_type = s.get("type") if isinstance(s, dict) else getattr(s, "_type", None)
        if isinstance(s, dict):
            data = s.get("data") or {}
        else:
            data = s
        if seg_type == "text":
            t = (data.get("text", "") if isinstance(data, dict) else getattr(s, "text", "")) or ""
            if str(t).strip():
                parts.append(str(t).strip())
        elif seg_type == "image":
            parts.append("[图片]")
            url = (data.get("url") if isinstance(data, dict) else getattr(s, "url", "")) or ""
            if str(url).startswith(("http://", "https://")) and str(url) not in quoted_image_urls:
                quoted_image_urls.append(str(url))
                fid = data.get("file") if isinstance(data, dict) else getattr(s, "file", None)
                if fid:
                    quoted_image_file_ids[str(url)] = str(fid)
        elif seg_type == "record":
            parts.append("[语音]")
        elif seg_type == "face":
            parts.append("[表情]")
        elif seg_type == "at":
            qq = data.get("qq", "") if isinstance(data, dict) else getattr(s, "qq", "")
            parts.append(f"[@{qq}]" if qq else "[@]")
    text = " ".join(p for p in parts if p).strip()
    if not who and not text and not quoted_image_urls:
        return None
    return {"who": who, "text": text or "[非文本消息]",
            "user_id": str(sender.get("user_id", "") or ""),
            "image_urls": quoted_image_urls,
            "image_file_ids": quoted_image_file_ids}


_SESSION_LOCKS: Dict[str, asyncio.Lock] = {}
_SESSION_PENDING: Dict[str, dict] = {}
_COALESCE_WINDOW = 0.3


def _extract_event_info(event, client) -> Optional[dict]:
    if not isinstance(event, (PrivateMessageEvent, GroupMessageEvent)):
        return None
    is_private = isinstance(event, PrivateMessageEvent)
    user_text = ""
    has_image = False
    image_urls = []
    image_file_ids = {}
    at_ids = []
    at_bot = False
    reply_seg = None
    for seg in event.message:
        if isinstance(seg, Text):
            user_text += seg.text
        elif isinstance(seg, Image):
            has_image = True
            chosen = pick_image_source(seg)
            if chosen and chosen not in image_urls:
                image_urls.append(chosen)
                fid = getattr(seg, "file", None)
                if fid:
                    image_file_ids[chosen] = str(fid)
        elif isinstance(seg, At):
            qq = str(seg.qq)
            at_ids.append(qq)
            if qq == str(client.self_id):
                at_bot = True
                user_text += "[@本机器人]"
            else:
                user_text += f"[@{qq}]"
        elif isinstance(seg, Reply):
            reply_seg = seg
    if is_private:
        session_id = f"private_{event.user_id}"
    else:
        group_id = event.group_id
        sender_id = getattr(event.sender, "user_id", None) or "0"
        session_id = f"group_{group_id}"
        if global_config.get("isolated_session", False):
            session_id = f"group_{group_id}_{sender_id}"
    silent = False
    if not is_private and not at_bot:
        # 引用消息必须放行到 process_message：只有在那里回查被引用的消息，
        # 才能知道引用的是不是机器人（是则视同 @）。引用他人消息的做法是在
        # process_message 里、回查之后再按同一套配置拦下。
        if reply_seg is None:
            if global_config.get("only_private", False):
                return None
            # 没被@的群消息不回复，但仍要记进历史：模型下一条被@时才有上下文，
            # 聊天记录页也要能看到这些消息。
            silent = bool(global_config.get("group_need_at", True))
    if not user_text and not has_image:
        return None
    return {"session_id": session_id, "event": event, "client": client,
            "text": user_text, "has_image": has_image, "image_urls": image_urls,
            "image_file_ids": image_file_ids, "at_ids": at_ids,
            "at_bot": at_bot, "reply_seg": reply_seg, "silent": silent}


def _merge_event_info(pending: dict, info: dict) -> dict:
    texts = [t for t in (pending.get("text", ""), info.get("text", "")) if t]
    urls = list(pending.get("image_urls", []))
    for u in info.get("image_urls", []):
        if u not in urls:
            urls.append(u)
    fids = dict(pending.get("image_file_ids", {}))
    fids.update(info.get("image_file_ids", {}))
    at_ids = list(pending.get("at_ids", []))
    for q in info.get("at_ids", []):
        if q not in at_ids:
            at_ids.append(q)
    return {"session_id": info["session_id"], "event": info["event"], "client": info["client"],
            "text": "\n".join(texts),
            "has_image": bool(pending.get("has_image") or info.get("has_image") or urls),
            "image_urls": urls, "image_file_ids": fids, "at_ids": at_ids,
            "at_bot": bool(pending.get("at_bot") or info.get("at_bot")),
            "reply_seg": info.get("reply_seg") or pending.get("reply_seg")}


def _merged_payload(pending: dict) -> dict:
    return {"text": pending["text"], "has_image": pending["has_image"],
            "image_urls": pending["image_urls"], "image_file_ids": pending["image_file_ids"],
            "at_ids": pending["at_ids"], "at_bot": pending["at_bot"],
            "reply_seg": pending["reply_seg"]}


def _spawn_drainer(session_id: str, lock: asyncio.Lock):
    async def drain():
        async with lock:
            while True:
                pending = _SESSION_PENDING.pop(session_id, None)
                if pending is None:
                    break
                try:
                    await _process_message_event(pending["event"], pending["client"],
                                                 _merged_payload(pending))
                except Exception as e:
                    print(f"会话 {session_id} 排队消息处理异常: {type(e).__name__}: {e}")
    try:
        asyncio.get_running_loop().create_task(drain())
    except RuntimeError:
        pass


def _remember_unaddressed_message(session_id: str, text: str, has_image: bool,
                                  sender_id: str, sender_name: str) -> None:
    """群聊里没被@的消息：不回复，但落进历史。

    开启「回复需要@」后这类消息以前会被整条丢掉，模型下一条被@时看不到群里
    刚聊了什么，聊天记录页也完全不显示。这里只补记历史，不碰主动消息的闲置
    计时、也不消耗防刷屏额度。
    """
    content = (f"{text} [图片]".strip() if has_image else str(text or "").strip())
    if not session_id or not content or memory_manager is None:
        return
    if not _session_whitelisted(session_id):
        return
    try:
        memory_manager.migrate_legacy_memory(session_id)
        data = memory_manager.load_session_data(session_id)
        history = data.get("history")
        if not isinstance(history, list):
            history = []
        history.append({
            "role": "user",
            "content": content,
            "sender_id": str(sender_id or ""),
            "sender_name": sender_name or str(sender_id or ""),
            "timestamp": time.time(),
        })
        data["history"] = history
        memory_manager.save_session_data(session_id, data)
    except Exception as e:
        print(f"记录未@的群消息失败: {e}")


def _queue_unaddressed_message(session_id: str, text: str, has_image: bool,
                               sender_id: str, sender_name: str) -> None:
    """等会话锁释放后再补记没被@的群消息（该会话正在生成回复时走这条）。"""
    async def run():
        lock = _SESSION_LOCKS.setdefault(session_id, asyncio.Lock())
        async with lock:
            _remember_unaddressed_message(session_id, text, has_image,
                                          sender_id, sender_name)
    try:
        asyncio.get_running_loop().create_task(run())
    except RuntimeError:
        pass


async def handle_message_event(event, client):
    global napcat_client
    napcat_client = client
    info = _extract_event_info(event, client)
    if info is None:
        return
    if info.get("silent"):
        silent_session = info["session_id"]
        silent_sender = getattr(event.sender, "user_id", None) or "0"
        silent_name = getattr(event.sender, "nickname", None) or ""
        silent_lock = _SESSION_LOCKS.get(silent_session)
        if silent_lock is not None and silent_lock.locked():
            # 该会话正在生成回复：直接写盘会被对方手里那份旧历史在结束时整份覆盖回来，
            # 这条没被@的消息就永久消失了，所以等锁释放后再补记
            _queue_unaddressed_message(silent_session, info.get("text", ""),
                                       bool(info.get("has_image")),
                                       silent_sender, silent_name)
        else:
            _remember_unaddressed_message(silent_session, info.get("text", ""),
                                          bool(info.get("has_image")),
                                          silent_sender, silent_name)
        return
    session_id = info["session_id"]
    lock = _SESSION_LOCKS.setdefault(session_id, asyncio.Lock())
    if lock.locked():
        pending = _SESSION_PENDING.get(session_id)
        if pending is None:
            _SESSION_PENDING[session_id] = info
            print(f"会话 {session_id} 正在处理上一条消息，本条已排队，处理完后立即跟进。")
        else:
            _SESSION_PENDING[session_id] = _merge_event_info(pending, info)
            print(f"会话 {session_id} 连发消息，已合并待处理内容。")
        _spawn_drainer(session_id, lock)
        return
    async with lock:
        try:
            _SESSION_PENDING.setdefault(session_id, info)
            await asyncio.sleep(_COALESCE_WINDOW)
            while True:
                pending = _SESSION_PENDING.pop(session_id, None)
                if pending is None:
                    break
                await _process_message_event(pending["event"], pending["client"],
                                             _merged_payload(pending))
        except Exception as e:
            print(f"会话 {session_id} 消息处理异常: {type(e).__name__}: {e}")
        finally:
            _SESSION_PENDING.pop(session_id, None)


async def _process_message_event(event, client, merged: Optional[dict] = None):
    global napcat_client
    napcat_client = client

    is_private = isinstance(event, PrivateMessageEvent)

    if is_private:
        session_type = "private"
        target_id = event.user_id
        sender_id = event.user_id
        session_id = f"private_{event.user_id}"
    else:
        group_id = event.group_id
        sender_id = getattr(event.sender, "user_id", None) or "0"
        session_type = "group"
        target_id = group_id
        session_id = f"group_{group_id}"
        if global_config.get("isolated_session", False):
            session_id = f"group_{group_id}_{sender_id}"

    sender_name = getattr(event.sender, "nickname", None) or str(sender_id)
    user_text = ""
    has_image = False
    image_urls = []
    image_file_ids = {}
    at_bot = False
    at_ids = []
    at_names = {}
    reply_seg = None
    if merged is not None:
        user_text = merged.get("text", "")
        has_image = merged.get("has_image", False)
        image_urls = list(merged.get("image_urls", []))
        image_file_ids = dict(merged.get("image_file_ids", {}))
        at_ids = list(merged.get("at_ids", []))
        at_bot = merged.get("at_bot", False)
        reply_seg = merged.get("reply_seg")
    else:
        for seg in event.message:
            if isinstance(seg, Text):
                user_text += seg.text
            elif isinstance(seg, Image):
                has_image = True
                chosen = pick_image_source(seg)
                if chosen and chosen not in image_urls:
                    # 直接将图片 URL 加入，不下载
                    image_urls.append(chosen)
                    fid = getattr(seg, "file", None)
                    if fid:
                        image_file_ids[chosen] = str(fid)
            elif isinstance(seg, At):
                qq = str(seg.qq)
                at_ids.append(qq)
                if qq == str(client.self_id):
                    at_bot = True
                    user_text += "[@本机器人]"
                else:
                    user_text += f"[@{qq}]"
            elif isinstance(seg, Reply):
                reply_seg = seg

    if reply_seg is not None:
        quoted = await fetch_quoted_context(client, reply_seg)
        if quoted:
            if quoted["user_id"] and quoted["user_id"] == str(client.self_id):
                at_bot = True
            for qurl in quoted.get("image_urls", []):
                if qurl not in image_urls:
                    image_urls.append(qurl)
                    has_image = True
                    qfid = quoted.get("image_file_ids", {}).get(qurl)
                    if qfid:
                        image_file_ids[qurl] = qfid
            quote_note = f"（回复 {quoted['who'] or '某人'} 的消息：{quoted['text']}）"
            user_text = f"{quote_note}\n{user_text}" if user_text else quote_note

    if not is_private and at_ids:
        for qq in at_ids:
            if qq == str(client.self_id):
                continue
            name = await _fetch_member_name(client, group_id, qq)
            if name:
                at_names[qq] = name
                user_text = user_text.replace(f"[@{qq}]", f"[@{name}(QQ:{qq})]")
            else:
                at_names[qq] = ""

    if not is_private and not at_bot:
        if global_config.get("only_private", False):
            return
        if global_config.get("group_need_at", True):
            # 引用他人消息、又没@机器人：不回复，但消息要记进历史
            _remember_unaddressed_message(session_id, user_text, has_image,
                                          sender_id, sender_name)
            return
    if not user_text and not has_image:
        return
    if not _session_whitelisted(target_id):
        print(f"白名单：会话 {session_id}（{target_id}）不在白名单内，已忽略。")
        return
    if not _allow_message(session_id):
        print(f"防刷屏：会话 {session_id} 短时间内消息过多，本条已忽略（可在配置中调整 anti_spam_*）。")
        return

    print(f"收到{'私聊' if is_private else '群聊'} [{target_id}] 来自 [{sender_id}]: {user_text}")
    last_interaction[session_id] = time.time()
    last_user_activity[session_id] = last_interaction[session_id]
    proactive_pending.pop(session_id, None)
    if session_id in proactive_awaiting:
        # 用户在会话里发言即视为已回应：恢复该会话的主动开口资格
        proactive_awaiting.discard(session_id)
        print(f"主动消息：用户已在 {session_id} 回复，恢复主动开口资格。")
    save_proactive_state()

    memory_manager.migrate_legacy_memory(session_id)
    data = memory_manager.load_session_data(session_id)
    history = data.get("history", [])
    meta = data.get("meta", {})

    # ---- 插件指令：命中就由插件直接回复，不进 LLM 管线 ----
    # 插件通过 ctx.register_command 注册具名指令。这里刻意放在"消息已通过
    # 白名单/防刷屏/唤醒校验、历史已加载"之后、"写入用户消息"之前：
    # 指令是控制面操作，不该污染对话历史、也不该触发识图与待办提取。
    handled, plugin_reply = _dispatch_plugin_command(user_text, session_type, target_id,
                                                     session_id, sender_id,
                                                     sender_name, event)
    if handled:
        if plugin_reply:
            await sender.send_text(session_type, target_id, str(plugin_reply))
        return
    # ------------------------------------------------------
    meta["user_msg_count"] = int(meta.get("user_msg_count", 0)) + 1
    meta["last_user_text"] = user_text[:200]
    data["meta"] = meta
    
    # 初始历史（之后会在识图成功后更新描述）
    history_text = (f"{user_text} [图片]".strip() if has_image
                    else user_text) or "[图片]"

    # ---- 上一条图片消息没被真正看过时：本条相关追问要把识图模型叫回来 ----
    # 事故背景：用户发图 → 回复审判判定"无需回复" → 识图链路整条没跑、历史里只有
    # "[图片]"；用户接着问"这个怎么样"时系统只当纯文本处理，识图模型根本没被调用，
    # 于是回复节奏和上下文全对不上。
    images_from_pending = False
    if has_image:
        _remember_pending_image(session_id, image_urls, image_file_ids, user_text)
    else:
        pending_img = _take_pending_image(session_id, user_text)
        if pending_img:
            image_urls = list(pending_img.get("urls", []))
            image_file_ids = dict(pending_img.get("file_ids", {}))
            has_image = images_from_pending = bool(image_urls)
            if has_image:
                # 历史里点明"上一条附件是图片"，让模型知道这条追问指向哪张图
                for prev_msg in reversed(history):
                    if prev_msg.get("role") == "user":
                        prev = str(prev_msg.get("content", ""))
                        if "[图片" not in prev:
                            prev_msg["content"] = (f"{prev} [上一个附件是图片]").strip()
                        break
                print(f"会话 {session_id}：本条消息指向刚才那张未被真正看过的图片，"
                      "已把识图模型叫回来重新识图（修复回复节奏错乱）。")
    # ---------------------------------------------------------------------

    history.append({
        "role": "user",
        "content": history_text,
        "sender_id": sender_id,
        "sender_name": sender_name,
        "timestamp": time.time()
    })

    # 待办提取（后台异步，不阻塞回复）
    if global_config.get("todo_enabled", False) and todo_mgr:
        mode = global_config.get("todo_extract_mode", "regex")
        if mode == "regex":
            found = todo_mgr.extract_sync(user_text)
            for content, remind_ts in found:
                todo_mgr.add_todo(content, remind_ts, session_type, session_id, sender_id, source="regex")
        elif mode == "llm":
            # 带上最近几条对话：主人可能先说"我要睡觉了"，再说"5分钟后再提醒我"，
            # 没有上下文就只能把"再提醒我"当成事项（历史 bug）。
            recent_lines = []
            for msg in history[:-1][-6:]:
                who = "主人" if msg.get("role") == "user" else "你"
                content = str(msg.get("content", "")).strip()
                if content:
                    recent_lines.append(f"{who}：{content[:100]}")
            recent_lines.append(f"主人：{user_text}")
            _spawn(todo_mgr.extract_and_add(get_active_ctx(), user_text, session_type,
                                            session_id, sender_id,
                                            recent_context=recent_lines))

    source_role_key = ROLE_CONNECTIONS.get(id(client), "")
    target_roles = resolve_target_roles(user_text, is_private, source_role_key)
    max_total = max(1, int(global_config.get("multi_role_max_total", 6)))
    total_replies = 0
    first_reply_done = False

    def _dispatch_reply_done(info: dict) -> None:
        """通知插件"这一轮 LLM 回复已经发完"。插件系统没启用就什么都不做。"""
        rt = _plugin_runtime()
        if rt is None:
            return
        try:
            rt.dispatch_reply_done(info)
        except Exception as e:
            print(f"[插件] 回复完成钩子派发失败: {type(e).__name__}: {e}")


    async def process_role_reply(role: dict, trigger_text: str, gate_reply: bool = False) -> bool:
        with sender.using_client(sender.client_for(role)):
            nonlocal total_replies, first_reply_done
            if total_replies >= max_total:
                return False
            ctx = RoleContext(global_config.config, role)
            emotions = get_role_emotions(role)
            # 先备好上下文与图片：审判判定"不回复"时也要能补记画面描述（见下）
            extra_parts = []
            if not is_private:
                mention_parts = []
                for q in at_ids:
                    nm = at_names.get(q)
                    mention_parts.append(f"{nm}(QQ:{q})" if nm else f"QQ:{q}")
                who = "、".join(mention_parts) if mention_parts else "无（按提及的名字触发）"
                if at_bot:
                    extra_parts.append(
                        f"【@对象】本条消息@了：{who}（其中包含你：本机器人/当前角色）。"
                        "你是被@的机器人，消息里提到的其他人/QQ号都是别的群成员，不是你本人，"
                        "也不是给你发消息的用户；请区分“发消息的用户”和“被@的其他成员”，只按自己的身份回应。")
                else:
                    extra_parts.append(
                        f"【@对象】本条消息@了：{who}（都不是你）。"
                        "被@的是其他群成员，不是你本人，也不是给你发消息的用户；"
                        "只有消息明确提到你的名字时才由你回应，不要替其他被@的人作答。")
            use_history = history
            if global_config.get("summary_enabled", False) and meta.get("summary"):
                try:
                    keep = max(1, int(global_config.get("summary_max_history", 5)))
                except (TypeError, ValueError):
                    keep = 5
                use_history = history[-keep:]
                extra_parts.append(f"【早期对话摘要】{meta['summary']}")
            if global_config.get("dynamic_context_enabled", False) and meta.get("topic"):
                extra_parts.append(f"【当前话题】{meta['topic']}")
            if profile_mgr and global_config.get("profiles_enabled", False):
                p = profile_mgr.build_injection(sender_id)
                if p:
                    extra_parts.append(p)
            if lexicon_mgr is not None:
                lex = lexicon_mgr.build_injection()
                if lex:
                    extra_parts.append(lex)
            if rag_mgr and global_config.get("rag_enabled", False):
                rc = await rag_mgr.build_context(user_text)
                if rc:
                    extra_parts.append(rc)
            try:
                repeat_rounds = max(1, int(global_config.get("repeat_guard_rounds", 3) or 3))
            except (TypeError, ValueError):
                repeat_rounds = 3
            repeat_flags = repeat_guard_flags(ctx)
            recent_replies = _recent_assistant_replies(history, repeat_rounds)
            last_reply = recent_replies[0] if recent_replies else ""
            if recent_replies and repeat_flags["compare_self"]:
                block = "\n".join(f"{i + 1}. {r[:200]}" for i, r in enumerate(recent_replies))
                extra_parts.append(
                    f"【禁止重复】你最近 {len(recent_replies)} 轮已经说过下面这些话：\n{block}\n"
                    "本次回复必须与上面每一句都明显不同：句子结构、用词、切入角度、"
                    "举例与收尾方式都要换新的；严禁把其中任何一句原样或换个说法再说一遍，"
                    "也严禁只是把前面轮次的话重新拼一遍。")

            image_sources = image_urls
            if has_image:
                image_sources = await refresh_image_urls(client, image_urls, image_file_ids)

            gen_budget = max(90.0, float(ctx.get("llm_timeout", 120) or 120) + 60.0)

            mood_user = "" if is_private else str(sender_id)
            verdict = None
            # 开关判定必须走 mood 模块的兼容层：旧配置缺这两个键时以"是否配置了提示词"为准，
            # 直接读配置会漏判，且字符串 "false" 会被当成真值
            if gate_reply and mood_mgr is not None \
                    and (judge_enabled(ctx) or mood_enabled(ctx)):
                try:
                    verdict = await asyncio.wait_for(
                        judge_and_decide(ctx, mood_mgr, session_id, trigger_text,
                                         history, user_id=mood_user), timeout=30)
                except asyncio.TimeoutError:
                    print("回复审判超时（30s），本轮跳过审判直接回复。")
                    verdict = None
                # 审判看不到画面：本条带图时，"无需回复"这个判定没有依据（消息里可能
                # 除了图片一个字都没有），不生效，交给角色自己回。否则识图模型已经
                # 生成的台词会被丢掉，历史里还会留下一张从没被回应过的图，下一轮模型
                # 看到它就会接着往下演。
                if _image_reply_override(verdict, has_image):
                    print(f"回复审判：本条带图，{ctx.character_name or ctx.character_key} "
                          f"的「无需回复」判定不生效，仍由角色回复（心情值 {verdict['mood']:.0f}）")
                    verdict["should_reply"] = True
                # 只开心情、没开审判时 verdict["should_reply"] 恒为 True，不会被拦下；
                # 开着审判才会出现真正"决定不回复"的分支。
                if verdict is not None and not verdict["should_reply"]:
                    cause = "LLM判定无需回复" if not verdict.get("llm_reply", True) \
                        else f"概率门控未通过（概率 {verdict.get('probability', 0):.3f} < 1）"
                    print(f"回复审判：{ctx.character_name or ctx.character_key} 决定不回复"
                          f"（心情值 {verdict['mood']:.0f}，回复概率 {verdict['probability']:.2f}，{cause}）")
                    if has_image and image_sources:
                        # 审判在识图之前就判定"不用回"，但图还是要看：把画面描述写进历史，
                        # 供用户下一条相关追问使用（统一由收尾逻辑决定是否落盘）。
                        # 只取描述与收藏判定，不产出台词 —— 这一轮不发消息，台词留着
                        # 只会变成下一轮"接着演"的由头。
                        try:
                            pending_reply = await asyncio.wait_for(
                                generate_reply(ctx, emotions, trigger_text, use_history,
                                               image_sources, list(extra_parts), sender_id,
                                               session_id=session_id, describe_only=True),
                                timeout=gen_budget)
                        except Exception as e:
                            pending_reply = None
                            print(f"审判未回复时补记画面描述失败（忽略）: {type(e).__name__}: {e}")
                        desc = str((pending_reply or {}).get("description", "") or "").strip()
                        if desc:
                            _backfill_image_description(history, desc)
                            print(f"审判未回复：画面描述已写入历史，供主人下一条追问使用 → {desc[:60]}")
                        # 收藏判定也在同一份识图结果里：触发点原先只挂在回复路径上，
                        # 审判判不回时这条路径整段不执行，图就永远不会被收藏。
                        _spawn_sticker_capture(ctx, pending_reply, image_sources)
                    _log_mood_commit(commit_mood(ctx, mood_mgr, session_id, verdict, mood_user),
                                     verdict)
                    return False

            # 心情值不只是记录：按档位把语气与篇幅约束附加到本轮提示词
            if mood_mgr is not None:
                mood_now = verdict["mood"] if verdict is not None \
                    else current_mood(ctx, mood_mgr, session_id, mood_user)
                tier, mood_note = mood_style(ctx, mood_now)
                if mood_note:
                    extra_parts.append(mood_note)
                    print(f"心情影响语气：心情值 {mood_now:.0f} → {tier}档，"
                          "本轮回复按该档位的语气与篇幅约束生成。")

            sink = None
            if global_config.get("streaming_enabled", False) and not first_reply_done:
                sink = SentenceSink(session_type, target_id, emotions, ctx, recent_replies,
                                    "" if has_image else user_text)

            reply_started = time.time()
            timed_out = False
            try:
                reply = await asyncio.wait_for(
                    generate_reply(ctx, emotions, trigger_text, use_history,
                                   image_sources if has_image else None,
                                   extra_parts, sender_id,
                                   on_sentence=sink.on_sentence if sink else None,
                                   session_id=session_id),
                    timeout=gen_budget)
            except asyncio.TimeoutError:
                timed_out = True
                print(f"回复生成超出预算（{gen_budget:.0f}s），本轮放弃以释放会话锁。")
            except BaseException:
                if sink is not None:
                    await sink.abort()
                raise
            # 本轮回复已生成，审判判定的心情变化量此时才落盘
            _log_mood_commit(commit_mood(ctx, mood_mgr, session_id, verdict, mood_user), verdict)
            if timed_out:
                if sink is not None:
                    await sink.abort()
                return False

            # 识图成功就立刻把画面描述回填进历史（哪怕这一轮最终不发消息）：
            # 这样"要不要回复"的审判、以及下一轮追问，都能看到画面真实内容。
            if has_image and reply:
                early_desc = str(reply.get("description", "") or "").strip()
                if early_desc:
                    _backfill_image_description(history, early_desc)

            tts_ms = 0.0
            if sink is not None:
                await sink.flush()
                if (not reply or not reply.get("sentences")) and sink.sent_sentences:
                    reply = {"sentences": sink.sent_sentences, "llm_ms": 0, "tool_trace": []}
                tts_ms = sink.tts_ms
            if not reply or not reply.get("sentences"):
                return False

            zh_now = "".join(s.get("zh", "") for s in reply["sentences"]).strip()
            can_retry = sink is None or sink.sent == 0
            self_ratio, self_target = (0.0, "")
            if repeat_flags["compare_self"]:
                self_ratio, self_target = _max_repeat_ratio(zh_now, recent_replies)
            user_ratio = 0.0
            if repeat_flags["compare_user"] and zh_now and user_text and not has_image \
                    and "[图片]" not in user_text \
                    and len(_norm_text(user_text)) >= _USER_ECHO_MIN_CHARS:
                user_ratio = _repeat_ratio(user_text, zh_now)
            print(f"相似度检查：与最近 {len(recent_replies)} 条回复最高 {self_ratio:.2f}，"
                  f"与用户本条 {user_ratio:.2f}"
                  + ("" if can_retry else "（流式内容已发送，无法打回）")
                  + repeat_guard_summary(repeat_flags))
            if not repeat_flags["regen"]:
                print("防复读：整段生成后校验已关闭（repeat_guard_regen_check=false），"
                      "本次不做相似度重生成。")
            elif not repeat_guard_active(repeat_flags):
                print("防复读：比对对象全部关闭，整段校验无内容可比，跳过。")
            self_threshold, user_threshold = repeat_thresholds(ctx)
            if repeat_flags["regen"] and repeat_guard_active(repeat_flags) and can_retry \
                    and (self_ratio >= self_threshold
                         or user_ratio >= user_threshold):
                dup_is_user = user_ratio >= user_threshold and self_ratio < self_threshold
                dup_target = user_text if dup_is_user else (self_target or last_reply)
                base_ratio = max(self_ratio, user_ratio)
                dup_label = "复述了用户本条消息的原话" if dup_is_user else "与之前的回复几乎重复"
                print(f"检测到回复{dup_label}（重合率 {base_ratio:.2f}），重新生成。")
                retry_hist = use_history
                if not dup_is_user:
                    retry_hist = list(use_history)
                    while retry_hist and retry_hist[-1].get("role") == "assistant":
                        retry_hist.pop()
                retry_ctx = RoleContext(global_config.config,
                                        {**role, "temperature": _RETRY_TEMPERATURE,
                                         "temperature_max": _RETRY_TEMPERATURE_MAX})
                for attempt in range(1, 3):
                    retry_parts = list(extra_parts) + [
                        f"警告：你刚才的回复{dup_label}——“{dup_target[:120]}”。"
                        "重新生成时句子、用词、角度必须和这句话明显不同，"
                        "只回应对方话里的意图，不要照搬其中的词。"
                        + ("再换一个完全不同的切入角度。" if attempt > 1 else "")]
                    if time.time() - reply_started > 60:
                        print("回复生成耗时过长，跳过重生成以免长时间占用会话。")
                        break
                    try:
                        retried = await asyncio.wait_for(
                            generate_reply(retry_ctx, emotions, trigger_text, retry_hist,
                                           image_sources if has_image else None,
                                           retry_parts, sender_id, on_sentence=None,
                                           session_id=session_id),
                            timeout=gen_budget)
                    except asyncio.TimeoutError:
                        print("重生成超预算，保留原回复。")
                        break
                    if not retried or not retried.get("sentences"):
                        break
                    new_zh = "".join(s.get("zh", "") for s in retried["sentences"]).strip()
                    accept_threshold = user_threshold if dup_is_user else self_threshold
                    # 验收同样只看还有效的那些维度：关掉的维度不该继续左右"重生成是否被接受"
                    new_self = _max_repeat_ratio(new_zh, recent_replies)[0] \
                        if repeat_flags["compare_self"] else 0.0
                    new_user = _repeat_ratio(user_text, new_zh) \
                        if (repeat_flags["compare_user"] and user_text and new_zh) else 0.0
                    if dup_is_user:
                        new_ratio = max(new_self, new_user if new_zh else 1.0)
                    elif not new_zh:
                        new_ratio = 1.0
                    else:
                        new_ratio = new_self
                    print(f"重生成第 {attempt} 次，重合率 {new_ratio:.2f}")
                    if new_ratio < accept_threshold or (attempt == 2 and new_ratio < base_ratio):
                        reply = retried
                        break

            if has_image and global_config.get("image_identity_guard_enabled", True) \
                    and image_self_claim("".join(s.get("zh", "") for s in reply["sentences"])):
                print("图片身份规则：回复把用户发来的图当成了角色自己，重新生成。")
                fixed = None
                if can_retry and time.time() - reply_started <= 60:
                    claim_parts = list(extra_parts) + [IMAGE_CLAIM_WARNING]
                    try:
                        retried = await asyncio.wait_for(
                            generate_reply(ctx, emotions, trigger_text, use_history,
                                           image_sources if has_image else None,
                                           claim_parts, sender_id, on_sentence=None,
                                           session_id=session_id),
                            timeout=gen_budget)
                    except asyncio.TimeoutError:
                        print("图片身份重生成超预算，保留原回复。")
                        retried = None
                    if retried and retried.get("sentences") \
                            and not image_self_claim("".join(s.get("zh", "")
                                                             for s in retried["sentences"])):
                        fixed = retried
                if fixed is not None:
                    reply = fixed
                else:
                    kept = _strip_image_claims(reply["sentences"])
                    if kept and len(kept) != len(reply["sentences"]):
                        print("图片身份重生成未消除认领表述，已剔除相关句子。")
                        reply = {**reply, "sentences": kept}

            if sink is None or sink.sent == 0:
                await repair_sentence_lang(reply.get("sentences", []), ctx)

            # ====== 上下文互通核心逻辑：回填识图模型的画面描述 ======
            if has_image:
                img_desc = str(reply.get("description", "") or "").strip()
                if img_desc:
                    _backfill_image_description(history, img_desc)
            # ========================================================

            tts_calls_result = sink.tts_calls if sink is not None else 0
            if sink is not None:
                if sink.sent == 0:
                    send_result = await sender.send_reply(session_type, target_id, reply["sentences"],
                                                          emotions, ctx,
                                                          use_voice=global_config.get("tts_reply_enabled", True))
                    tts_calls_result += send_result.get("tts_calls", 0)
            else:
                send_result = await sender.send_reply(session_type, target_id, reply["sentences"],
                                                      emotions, ctx,
                                                      use_voice=global_config.get("tts_reply_enabled", True))
                tts_ms = send_result.get("tts_ms", 0.0)
                tts_calls_result = send_result.get("tts_calls", 0)

            sent_now = urls_in_text("".join(str(s.get("display", "") or "")
                                            for s in reply["sentences"]))
            if sent_now:
                record_sent_links(session_id, sent_now)

            # 插件 on_message：主回复发完后，把插件想追加的文本挨条发出去。
            # 顺序放在主回复之后，是为了让插件的补充说明不打断角色本身的语气。
            for extra in _dispatch_plugin_message({
                    "session_type": session_type, "target_id": target_id,
                    "session_id": session_id, "sender_id": sender_id,
                    "sender_name": sender_name, "text": user_text,
                    "role": role, "emotions": emotions}):
                try:
                    await sender.send_text(session_type, target_id, extra)
                except Exception as e:
                    print(f"[插件] 追加回复发送失败: {type(e).__name__}: {e}")

            _dispatch_reply_done({
                "session_type": session_type, "target_id": target_id,
                "session_id": session_id, "sentences": reply["sentences"],
                "emotions": emotions, "role": role, "reply": reply,
                "sender_id": sender_id, "user_text": user_text,
            })

            # 流式路径补出来的句子可能只有 display 没有 zh，取不到时退回展示文本
            zh_text = "".join(str(s.get("zh") or s.get("display") or "")
                              for s in reply["sentences"])
            speaker = role.get("character_name", ctx.character_key)
            entry = {"role": "assistant", "content": zh_text, "timestamp": time.time(),
                     "speaker": speaker,
                     "emotion": reply["sentences"][0].get("emotion", "")}
            tool_notes = _tool_notes_from_trace(reply.get("tool_trace"))
            if tool_notes:
                entry["tool_notes"] = tool_notes
            history.append(entry)
            data["history"] = history
            data["meta"] = meta
            memory_manager.save_session_data(session_id, data)
            if has_image:
                # 图片已经被真正看过并回复过了，不必再为后续追问重跑识图
                _clear_pending_image(session_id)

            if stats_mgr and global_config.get("stats_enabled", True) and db is not None:
                db.record_interaction(
                    session_type, session_id, sender_id, sender_name,
                    role.get("character_key", ""), reply["sentences"][0].get("emotion", ""),
                    reply.get("llm_ms", 0), tts_ms, len(reply["sentences"]), ok=True,
                    llm_calls=reply.get("llm_calls", 1),
                    tts_calls=tts_calls_result,
                    tool_calls=reply.get("tool_calls", 0))
            if stats_mgr:
                stats_mgr.record_message(session_id)

            if not first_reply_done and has_image:
                _spawn_sticker_capture(ctx, reply, image_sources)

            if profile_mgr and global_config.get("profiles_enabled", False) and \
                    global_config.get("profiles_auto_extract", False) and not first_reply_done:
                _spawn(profile_mgr.extract_from_dialog(ctx, trigger_text, zh_text, sender_id))
            first_reply_done = True
            total_replies += 1
            return True

    try:
        if not await ensure_tts_service(global_config):
            print("警告：TTS 服务不可用，将降级为纯文本。")

        for role in target_roles:
            if total_replies >= max_total:
                break
            await process_role_reply(role, user_text, gate_reply=True)

        rounds = int(global_config.get("multi_role_auto_rounds", 0) or 0)
        if global_config.get("multi_role_enabled", False) and rounds > 0 and len(target_roles) > 1:
            for _ in range(rounds):
                for role in target_roles:
                    if total_replies >= max_total:
                        break
                    last_assistant = next((m for m in reversed(history)
                                           if m.get("role") == "assistant"), None)
                    if not last_assistant:
                        break
                    if last_assistant.get("speaker") == role.get("character_name", ""):
                        continue
                    trigger = (f"（群里的另一位角色「{last_assistant.get('speaker', '')}」刚刚说："
                               f"{last_assistant.get('content', '')}）请自然地接话回应。")
                    await process_role_reply(role, trigger)
    except Exception as e:
        print(f"回复生成失败: {type(e).__name__}: {e}")

    await asyncio.to_thread(memory_manager.cleanup_voice_cache,
                            global_config.get("max_voice_cache", 20))
    if total_replies > 0:
        _spawn(post_reply_context_tasks(session_id, get_active_ctx()))
    else:
        # 没产生回复（审判判定不用回 / 生成失败 / 发送失败）也照常落盘：
        # 用户确实说过的话要留着，否则下一条追问时模型看不到原内容。
        data["history"] = history
        data["meta"] = meta
        memory_manager.save_session_data(session_id, data)

async def post_reply_context_tasks(session_id: str, ctx: RoleContext):
    """对话后维护：自动摘要 + 话题检测（均为可开关功能）。"""
    try:
        data = memory_manager.load_session_data(session_id)
        history = data.get("history", [])
        meta = data.get("meta", {})
        valid_messages = [m for m in history if isinstance(m, dict)
                          and m.get("role") in ("user", "assistant")
                          and str(m.get("content", "")).strip()]
        if len(valid_messages) < 2:
            return
        history = valid_messages
        changed = False
        # 上下文自动摘要
        if global_config.get("summary_enabled", False):
            try:
                threshold = int(global_config.get("summary_threshold", 20))
            except (TypeError, ValueError):
                threshold = 20
            try:
                keep = max(1, int(global_config.get("summary_max_history", 5)))
            except (TypeError, ValueError):
                keep = 5
            if len(history) >= threshold + keep:
                old_msgs = history[:len(history) - keep]
                existing = meta.get("summary", "")
                lines = speaker_labeled_lines(old_msgs, limit=40)
                prompt = (f"{global_config.get('summary_prompt', '')}\n\n"
                          f"{'已有摘要（请合并）：' + existing if existing else ''}\n\n对话：\n" + "\n".join(lines))
                summary = await generate_proactive_text(ctx, prompt)
                if summary:
                    meta["summary"] = summary[:800]
                    changed = True
                    print(f"已更新会话 {session_id} 的上下文摘要。")
        # 话题检测
        if global_config.get("dynamic_context_enabled", False):
            try:
                every = max(2, int(global_config.get("topic_summary_every_n", 10)))
            except (TypeError, ValueError):
                every = 10
            try:
                user_msg_count = int(meta.get("user_msg_count", 0))
            except (TypeError, ValueError):
                user_msg_count = 0
            if user_msg_count % every == 0:
                recent = history[-10:]
                lines = speaker_labeled_lines(recent, max_chars=120)
                prompt = (f"{global_config.get('topic_summary_prompt', '')}\n\n" + "\n".join(lines))
                topic = await generate_proactive_text(ctx, prompt)
                if topic:
                    meta["topic"] = topic[:200]
                    changed = True
                    print(f"已更新会话 {session_id} 的当前话题：{topic[:50]}")
        # 自主学习：按会话累计的用户消息数触发，词典全局共享
        if lexicon_mgr is not None:
            try:
                user_msg_count = int(meta.get("user_msg_count", 0))
            except (TypeError, ValueError):
                user_msg_count = 0
            if lexicon_mgr.should_learn(user_msg_count):
                await lexicon_mgr.learn_from_history(ctx, history, session_id)
        if changed:
            fresh = memory_manager.load_session_data(session_id)
            fresh.setdefault("meta", {})
            for key in ("summary", "topic"):
                if key in meta:
                    fresh["meta"][key] = meta[key]
            memory_manager.save_session_data(session_id, fresh)
    except Exception as e:
        print(f"上下文维护任务异常: {e}")


# ============================================================================
# 主动消息与调度注册
# ============================================================================

async def proactive_idle_check():
    """定期检查长时间未互动的会话，主动发送话题。

    历史 bug（导致"非静默时段也从来不主动发消息"）：
      1) deadline 每次检查都重算成 now + idle + jitter，
         而 `if deadline > now + idle_seconds: continue` 恒成立 → 永远发不出去；
      2) last_interaction 只存在于内存、只在收到消息时写入，重启后为空，
         闲置会话永远不进候选；
      3) 当日计数不落盘，重启即重置，且没有任何诊断日志。
    现在：deadline 只在首次进入候选时定一次并持久化，到点才发送，
    发送/跳过都会打日志。
    """
    if not global_config.get("proactive_enabled", False):
        return
    if sender is None or sender.client is None:
        return
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    if today != _proactive_state_date:
        proactive_counts.clear()
        proactive_pending.clear()
        globals()["_proactive_state_date"] = today
        print(f"主动消息：已跨天（{today}），当日计数清零。")
    idle_minutes = float(global_config.get("proactive_idle_minutes", 30) or 30)
    idle_seconds = idle_minutes * 60
    jitter_lo, jitter_hi = _parse_jitter_minutes(global_config.get("proactive_idle_jitter", ""))
    max_per_day = max(1, int(global_config.get("proactive_max_per_day", 2) or 2))
    quiet = _in_quiet_hours()

    def _idle_ref(session_id: str) -> float:
        return max(last_user_activity.get(session_id, 0.0),
                   last_proactive_sent.get(session_id, 0.0))

    newly_scheduled = set()
    wait_reply = bool(global_config.get("proactive_wait_reply", True))
    for session_id in set(last_user_activity) | set(last_proactive_sent):
        if session_id in proactive_pending:
            continue
        if not session_memory_exists(session_id):
            forget_proactive_session(session_id)
            continue
        if not _session_whitelisted(session_id):
            continue
        if wait_reply and session_id in proactive_awaiting:
            # 上一条主动消息用户还没回，不再主动打扰
            continue
        last_ts = _idle_ref(session_id)
        if now - last_ts < idle_seconds:
            continue
        if proactive_counts.get(f"{today}|{session_id}", 0) >= max_per_day:
            continue
        if quiet:
            continue
        jitter_seconds = random.uniform(jitter_lo, jitter_hi) * 60 if jitter_hi > 0 else 0.0
        target = now + jitter_seconds
        proactive_pending[session_id] = target
        newly_scheduled.add(session_id)
        print(f"主动消息：会话 {session_id} 已闲置 {int((now - last_ts) / 60)} 分钟，"
              f"计划在 {time.strftime('%H:%M:%S', time.localtime(target))} 主动开口。")
    if proactive_pending:
        save_proactive_state()

    for session_id, target in list(proactive_pending.items()):
        if session_id in newly_scheduled:
            continue
        if now < target:
            continue
        if not session_memory_exists(session_id):
            forget_proactive_session(session_id)
            continue
        if quiet:
            continue
        last_ts = _idle_ref(session_id)
        if now - last_ts < idle_seconds:
            proactive_pending.pop(session_id, None)
            print(f"主动消息：会话 {session_id} 期间有互动，已取消本次主动开口。")
            continue
        used = proactive_counts.get(f"{today}|{session_id}", 0)
        if used >= max_per_day:
            proactive_pending.pop(session_id, None)
            print(f"主动消息：会话 {session_id} 已达当日上限（{max_per_day} 条），跳过。")
            continue
        session_type, target_id = parse_session_target(session_id)
        ctx = get_active_ctx()
        instruction = str(global_config.get("proactive_prompt", "主动找个话题和主人聊聊。"))
        try:
            hist_block = dialog_history_block(
                memory_manager.load_session_data(session_id).get("history", []),
                ctx, session_id=session_id)
        except Exception as e:
            hist_block = ""
            print(f"主动消息：读取会话历史失败（忽略）: {type(e).__name__}: {e}")
        if hist_block:
            print(f"主动消息：已带上会话 {session_id} 的聊天历史（{len(hist_block)} 字），"
                  "开场白会承接上次话题。")
        try:
            text = await generate_proactive_text(ctx, instruction, history_block=hist_block)
        except Exception as e:
            print(f"主动消息生成失败: {e}")
            text = ""
        if not text:
            proactive_pending.pop(session_id, None)
            print(f"主动消息：会话 {session_id} 本轮生成空文本，已重新排队。")
            continue
        if now - _idle_ref(session_id) < idle_seconds:
            # 生成期间用户开口了：放弃这条主动消息，避免答非所问地插话
            proactive_pending.pop(session_id, None)
            print(f"主动消息：会话 {session_id} 在生成期间有互动，取消本次发送。")
            continue
        if not session_memory_exists(session_id):
            forget_proactive_session(session_id)
            continue
        try:
            ok = await sender.speak_and_send(
                session_type, target_id, text, get_active_emotions(), ctx,
                use_voice=bool(global_config.get("proactive_voice", False)),
                sticker=bool(global_config.get("proactive_sticker", False)),
                session_id=session_id)
        except Exception as e:
            proactive_pending.pop(session_id, None)
            print(f"主动消息发送失败（{session_id}）: {type(e).__name__}: {e}")
            continue
        if not ok:
            proactive_pending.pop(session_id, None)
            print(f"主动消息：会话 {session_id} 发送未成功（客户端可能未连接），本轮放弃。")
            continue
        proactive_pending.pop(session_id, None)
        last_proactive_sent[session_id] = now
        last_interaction[session_id] = now
        proactive_counts[f"{today}|{session_id}"] = used + 1
        # 等待回复状态始终记录（跨重启保留）；是否据此停发由 proactive_wait_reply 决定
        proactive_awaiting.add(session_id)
        save_proactive_state()
        print(f"已向 {session_id} 发送主动消息（今日第 {used + 1}/{max_per_day} 条）：{text[:40]}"
              + ("（等待用户回复，回复前不再主动）" if wait_reply else ""))


def dialog_history_block(history: list, ctx=None, session_id: str = "",
                         recent: int = None, summary_chars: int = None,
                         max_chars: int = None) -> str:
    """把会话历史拼成"给模型看的对话上下文"，供主动消息/问候/待办提取使用。

    历史事故：主动消息与节日问候都是一次**独立**的 generate 调用，上下文里没有
    任何对话记录 —— 角色只能凭空开场，于是出现"主人今天过得怎么样呀？"这类
    和刚才聊的内容完全对不上的话。
    现在按用户要求带上历史：
      · 最近几条（默认 6 条）逐条全文给出，保证承接得上；
      · 更早的部分用已有摘要（summary_enabled 生成的 meta.summary）压缩描述；
        没有摘要时退化为"最早 N 条各截一小段"的简略描述。
    """
    msgs = [m for m in (history or []) if isinstance(m, dict)
            and m.get("role") in ("user", "assistant")
            and str(m.get("content", "")).strip()]
    if not msgs:
        return ""
    try:
        recent = max(0, int(recent if recent is not None
                            else (ctx.get("history_context_recent", 6) if ctx else 6) or 6))
    except (TypeError, ValueError):
        recent = 6
    try:
        summary_chars = max(0, int(summary_chars if summary_chars is not None
                                   else (ctx.get("history_context_summary_chars", 400)
                                         if ctx else 400) or 400))
    except (TypeError, ValueError):
        summary_chars = 400
    try:
        max_chars = max(200, int(max_chars if max_chars is not None
                                 else (ctx.get("history_context_max_chars", 1600)
                                       if ctx else 1600) or 1600))
    except (TypeError, ValueError):
        max_chars = 1600

    summary = ""
    if isinstance(ctx, RoleContext):
        summary = str(ctx.get("dialog_summary", "") or "").strip()
    if not summary and session_id and memory_manager is not None:
        try:
            meta = (memory_manager.load_session_data(session_id) or {}).get("meta", {}) or {}
            summary = str(meta.get("summary", "") or "").strip()
        except Exception:
            summary = ""

    recent_msgs = msgs[-recent:] if recent > 0 else []
    older_msgs = msgs[:len(msgs) - len(recent_msgs)] if recent_msgs else msgs
    if summary:
        older_msgs = []          # 摘要已覆盖早期内容，不再重复描述
    elif older_msgs and len(older_msgs) > 6:
        older_msgs = older_msgs[:3] + older_msgs[-3:]

    header = ("【对话历史】以下是主人和你之前的真实聊天记录，"
              "本次发言必须承接这些内容（延续上次的话题、语气和称呼），"
              "绝对不要凭空换一个不相干的话题。")
    parts = [header]
    if summary:
        parts.append("较早的对话摘要：" + summary[:summary_chars])
    if older_msgs:
        brief = []
        for msg in older_msgs:
            who = "主人" if msg.get("role") == "user" else "你"
            brief.append(f"{who}：{str(msg.get('content', '')).strip()[:60]}")
        parts.append("更早的对话（简略）：" + "；".join(brief))
    if recent_msgs:
        lines = []
        for msg in recent_msgs:
            who = "主人" if msg.get("role") == "user" else "你"
            lines.append(f"{who}：{str(msg.get('content', '')).strip()[:200]}")
        parts.append("最近的对话（按时间顺序，全文）：\n" + "\n".join(lines))
    else:
        parts.append("（还没有更早的对话记录，正常开场即可。）")
    return "\n".join(parts)[:max_chars]


def session_history_block(session_key: str, ctx=None) -> str:
    """按会话键取出该会话的历史与摘要，供节日问候/生日祝福/定时任务承接前文。

    events 与 jobs 模块按 session_key（如 group_1077806168 / private_10001）回调，
    而 dialog_history_block 收的是历史列表：这里负责把两者接上。
    """
    if memory_manager is None:
        return ""
    try:
        history = memory_manager.load_history(session_key)
    except Exception as e:
        print(f"问候历史读取失败（忽略）: {type(e).__name__}: {e}")
        return ""
    return dialog_history_block(history, ctx, session_id=session_key)


def _known_sessions() -> list:
    """从记忆目录解析已知会话列表 [(session_type, session_id)]。

    供默认节日问候在没有指定发送目标时广播给全部已知会话使用。
    """
    out = []
    try:
        for f in memory_manager.data_path.glob("*.json"):
            m = re.match(r"^[A-Za-z0-9_\-]+_(private|group)_([A-Za-z0-9_\-]+)\.json$", f.name)
            if m:
                out.append((m.group(1), m.group(2)))
    except Exception as e:
        print(f"解析已知会话列表失败: {e}")
    return out


async def greeting_daily_check() -> int:
    if _in_quiet_hours():
        print("问候检查：当前处于静默时段，跳过（避免深夜打扰）。")
        return 0
    if event_mgr and sender:
        return await event_mgr.check_and_greet(sender, get_active_ctx, get_active_emotions,
                                               sessions_provider=_known_sessions,
                                               history_provider=session_history_block) or 0
    print(f"问候检查：事件管理器或发送器未就绪，跳过（event_mgr={event_mgr is not None}, "
          f"sender={sender is not None}）。")
    return 0


async def greeting_catchup_task():
    """启动补发：程序在问候时间之后才运行时，把当天漏掉的问候立即补发。

    补发内容包括两部分：
      1) 节日/生日问候检查（节日、画像生日在今天时补发）；
      2) 用户自定义的每日定时任务（早安问候这类 LLM 问候）——程序在任务时刻
         之后才启动时它们不会再被触发，这里按当天应执行时刻补跑一次。
    两部分都会打印实际发出多少条，不再出现"只打了补发日志、其实什么都没发"。
    """
    if not global_config.get("greeting_catchup_enabled", True):
        print("问候补发：未开启（greeting_catchup_enabled=false），本次不补发。")
        return
    if not global_config.get("scheduler_enabled", True):
        print("问候补发：调度总开关已关闭（scheduler_enabled=false），本次不补发。")
        return
    try:
        deadline_minutes = max(1.0, float(global_config.get("greeting_catchup_deadline_minutes", 30) or 30))
    except (TypeError, ValueError):
        deadline_minutes = 30.0
    wait_until = time.time() + deadline_minutes * 60
    waited = 0.0
    while sender is None or sender.client is None:
        if time.time() >= wait_until:
            print(f"问候补发：等待 NapCat 连接已超过 {deadline_minutes:g} 分钟仍未连接，"
                  "本次跳过（下次启动或连接成功后仍会补发）。")
            return
        await asyncio.sleep(10)
        waited += 10
        if int(waited) % 60 == 0:
            print(f"问候补发：等待 NapCat 连接中…（已等 {int(waited)} 秒）")
    if _in_quiet_hours():
        print("问候补发：当前处于静默时段，跳过今天的补发。")
        return
    only_jobs = not global_config.get("greeting_events_enabled", False) \
        and not global_config.get("birthday_greeting_enabled", False)
    if only_jobs:
        print("问候补发：节日问候与生日祝福都未开启，本次只补跑每日定时任务。")
    print(f"问候补发：程序启动时已过问候相关时刻（已等待 NapCat {int(waited)} 秒），"
          "开始检查今天漏掉的问候。")
    sent_events = 0
    try:
        hh, mm = str(global_config.get("greeting_check_time", "08:00")
                     or "08:00").split(":")[:2]
        check_minutes = int(hh) * 60 + int(mm)
    except Exception:
        check_minutes = 8 * 60
    lt = time.localtime()
    now_minutes = lt.tm_hour * 60 + lt.tm_min
    if now_minutes >= check_minutes:
        if not only_jobs:
            try:
                sent_events = await greeting_daily_check()
            except Exception as e:
                print(f"问候补发执行异常: {type(e).__name__}: {e}")
    else:
        print(f"问候补发：当前 {time.strftime('%H:%M')} 还没到问候时刻"
              f"（{global_config.get('greeting_check_time', '08:00')}），"
              "节日/生日问候交给每日定时任务。")
    sent_jobs = 0
    if job_mgr is not None:
        try:
            sent_jobs = await job_mgr.catch_up_missed_daily()
        except Exception as e:
            print(f"问候补发：补跑每日定时任务异常: {type(e).__name__}: {e}")
    if sent_events or sent_jobs:
        print(f"问候补发完成：节日/生日问候 {sent_events} 条，每日定时任务 {sent_jobs} 条。")
    else:
        print("问候补发完成：今天没有需要补发的问候"
              "（没有节日/生日命中，也没有漏掉的每日问候任务）。")


def register_feature_jobs():
    """根据配置注册/注销内置调度任务（主动消息、节日问候）。"""
    if not global_config.get("scheduler_enabled", True):
        scheduler.remove_job("proactive_idle")
        scheduler.remove_job("greeting_check")
        print("调度总开关已关闭：主动消息与问候检查不运行（待办提醒不受影响）。")
        return
    if global_config.get("proactive_enabled", False):
        try:
            seconds = max(30, int(global_config.get("proactive_check_seconds", 300)))
        except (TypeError, ValueError):
            seconds = 300
        scheduler.add_job("proactive_idle", "主动消息检查",
                          {"type": "interval", "seconds": seconds}, proactive_idle_check)
        print(f"主动消息检查已开启（每 {seconds} 秒，闲置阈值 {global_config.get('proactive_idle_minutes', 30)} 分钟）。")
    else:
        scheduler.remove_job("proactive_idle")
    if global_config.get("greeting_events_enabled", False) \
            or global_config.get("birthday_greeting_enabled", False):
        scheduler.add_job("greeting_check", "节日生日问候检查",
                          {"type": "daily", "time": global_config.get("greeting_check_time", "08:00")},
                          greeting_daily_check)
        print(f"节日/生日问候检查已开启（每日 {global_config.get('greeting_check_time', '08:00')}）。")
    else:
        scheduler.remove_job("greeting_check")


def hot_reload_managers():
    """配置保存后热重载依赖配置的管理器。"""
    global sticker_mgr
    if sticker_mgr is not None:
        sticker_mgr = StickerManager(global_config)
        if sender is not None:
            sender.sticker_manager = sticker_mgr
    _role_emotions_cache.clear()
    _role_mimics_cache.clear()
    if lexicon_mgr is not None:
        lexicon_mgr.config = global_config
    register_feature_jobs()
    if job_mgr is not None:
        job_mgr.reload()
    if sender is not None:
        sender.config = global_config


# ============================================================================
# WebUI 服务
# ============================================================================

def _json_file_response(data, filename: str):
    body = json.dumps(data, ensure_ascii=False, indent=2)
    return web.Response(body=body.encode("utf-8"), content_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


async def _local_file_response(path: Path, content_type: str):
    """读取本地文件并返回响应，读不到时返回 404。

    不用 web.FileResponse：它会无条件把文件的修改时间写进 Last-Modified，
    时间戳越界（负值等）时 time.gmtime 抛 OSError，响应在准备阶段就断开、
    浏览器拿到的是空连接。这里服务的是用户自己放的文件，元数据不可信。
    """
    try:
        body = await asyncio.to_thread(path.read_bytes)
    except OSError:
        return web.Response(status=404, text="not found")
    resp = web.Response(body=body, content_type=content_type)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def _brief_response(resp, limit: int = 200) -> str:
    """把服务端响应体压成一行短文本，用于把上游报错原因带回给用户。"""
    try:
        text = resp.text
    except Exception:
        return ""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


_NAME_ILLEGAL_CHARS = set('/\\:*?"<>|')


def _safe_name(name: str) -> str:
    """校验用户给出的目录名/文件名：只挡路径分隔符与穿越写法，其余一律放行。"""
    s = str(name or "")
    if not s or s in (".", "..") or s != Path(s).name or (_NAME_ILLEGAL_CHARS & set(s)):
        return ""
    return s


def _safe_subdir(root: Path, name: str) -> Optional[Path]:
    """校验 name 为 root 的直接子目录名（防路径穿越）。"""
    safe = _safe_name(name)
    if not safe:
        return None
    p = (root / safe).resolve()
    if root.resolve() not in p.parents:
        return None
    return p


def _emotion_audio_file(folder: Path, file_name):
    """校验 file_name 是 folder 下的音频；返回 (路径, 错误文案)。"""
    name = _safe_name(file_name)
    if not name:
        return None, f"文件名非法：{file_name}"
    target = (folder / name).resolve()
    if folder.resolve() not in target.parents or not target.is_file():
        return None, f"文件不存在：{name}"
    if target.suffix.lower() not in _AUDIO_EXTS:
        return None, f"不是音频文件：{name}"
    return target, ""


def _write_text_file(path: Path, text) -> None:
    """写入参考文字文件；文字为空就把它删掉。"""
    text = str(text or "").strip()
    if text:
        path.write_text(text, encoding="utf-8")
    else:
        path.unlink(missing_ok=True)


@web.middleware
async def _webui_error_middleware(request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as e:
        import traceback
        text = f"[WebUI 异常] {request.method} {request.path}: {type(e).__name__}: {e}"
        trace = traceback.format_exc()
        print(text)
        print(trace)
        try:
            with open(runtime_path("webui_error.log"), "a", encoding="utf-8") as f:
                f.write(f"{time.ctime()} - {text}\n{trace}\n")
        except Exception:
            pass
        return web.json_response(
            {"success": False, "error": f"{type(e).__name__}: {e}"}, status=500)


_CSRF_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def _same_origin(request) -> bool:
    """跨站表单/请求能否带着浏览器 Cookie 打过来。

    WebUI 默认不设密码，且写接口多为 multipart 表单，跨站 <form> 可以在
    没有预检的情况下直接提交到 127.0.0.1。浏览器对跨源请求必定带 Origin，
    因此「Origin/Referer 的主机与 Host 不一致」即可判定为跨站并拒绝。
    非浏览器客户端（命令行、测试）不带 Origin，按放行处理。
    """
    if request.method in _CSRF_SAFE_METHODS:
        return True
    origin = request.headers.get("Origin") or request.headers.get("Referer") or ""
    if not origin:
        return True
    host = request.headers.get("Host", "")
    if not host:
        return False
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if not parsed.netloc:
        return False
    if parsed.netloc == host:
        return True
    default_port = 443 if parsed.scheme == "https" else 80
    return parsed.port in (None, default_port) and host.split(":")[0] == parsed.hostname


def _make_auth_middleware(server: "WebUIServer"):
    @web.middleware
    async def _auth(request, handler):
        path = request.path
        if not _same_origin(request):
            print(f"已拒绝跨站请求：{request.method} {path}"
                  f"（Origin/Referer={request.headers.get('Origin') or request.headers.get('Referer')}）")
            return web.json_response({"success": False, "error": "跨站请求已被拒绝"}, status=403)
        if path.startswith("/api") and path not in ("/api/auth/login", "/api/auth/status",
                                                    "/api/auth/second"):
            token = server._auth_token
            if token:
                if request.cookies.get("lovomo_auth") != token:
                    return web.json_response({"success": False, "error": "需要密码"}, status=401)
            # 二级密码：设了才拦，解锁一次在有效期内不再拦
            if _needs_second_password(path) and server._second_password \
                    and not server._second_unlocked():
                return web.json_response({"success": False, "error": "需要二级密码",
                                          "need_second_password": True}, status=403)
        return await handler(request)
    return _auth


# 桌面窗口句柄：pywebview 的"选择文件夹"对话框需要（run_webview_loop 中赋值）
_WEBVIEW_WINDOW_HOLDER = {"window": None}

# 运行中的 WebUI 服务实例：消息处理链路在别的函数里，靠这个拿到插件运行时
_WEBUI_SERVER_HOLDER = {"server": None}


def _plugin_runtime():
    """取当前运行的插件运行时；插件系统未启用或尚未就绪时返回 None。"""
    server = _WEBUI_SERVER_HOLDER.get("server")
    if server is None:
        return None
    return getattr(server, "_plugin_runtime", None)


# 指令前缀：#名字 或 /名字，后面跟参数。用 \S+ 取指令名，其余整体当参数，
# 这样"#喵开关 开"和"#喵开关"两种写法都能命中同一个处理器。
_COMMAND_RE = re.compile(r"^\s*[#/]\s*(\S+)\s*(.*)$", re.S)


def _parse_plugin_command(text: str):
    """从消息文本里解析插件指令，返回 (名字, 参数) 或 None。

    只认行首的 # / 前缀，正文里出现的 # 不算 —— 否则一句普通聊天里带了
    井号就会把消息吞掉。名字长度也做个上限，避免把长文本当指令名去查表。
    """
    m = _COMMAND_RE.match(str(text or ""))
    if not m:
        return None
    name = m.group(1).strip()
    if not name or len(name) > 32:
        return None
    return name, m.group(2).strip()


def _dispatch_plugin_command(user_text, session_type, target_id,
                             session_id, sender_id, sender_name, event):
    """把一条可能的插件指令交给插件运行时。

    返回 (是否已被插件处理, 要回复的文本)。没装插件系统、消息不是指令、
    或没有插件认领这个指令时都返回 (False, None)，调用方继续走正常回复流程。
    """
    if not global_config.get("plugins_enabled", True):
        return False, None
    parsed = _parse_plugin_command(user_text)
    if not parsed:
        return False, None
    name, args = parsed
    rt = _plugin_runtime()
    if rt is None:
        return False, None
    if name not in rt.command_names():
        return False, None
    payload = {
        "session_type": session_type, "target_id": target_id,
        "session_id": session_id, "sender_id": sender_id,
        "sender_name": sender_name, "text": user_text,
        "group_id": target_id if session_type == "group" else None,
        "user_id": sender_id, "raw": event,
    }
    print(f"[插件] 指令 #{name} 命中，交由插件处理。")
    try:
        out = rt.dispatch_command(name, args, payload)
    except Exception as e:
        print(f"[插件] 指令 #{name} 分发失败: {type(e).__name__}: {e}")
        return True, None
    if out is None:
        return True, None
    return True, str(out)


def _dispatch_plugin_message(event_info: dict) -> list:
    """把消息事件广播给插件的 on_message，返回插件想追加的回复文本列表。

    只在插件启用、且这一轮本来就要回复时才有意义；异常一律由运行时隔离，
    这里只负责把结果带回去，不让插件的问题影响主链路。
    """
    if not global_config.get("plugins_enabled", True):
        return []
    rt = _plugin_runtime()
    if rt is None:
        return []
    try:
        return list(rt.dispatch_message(event_info) or [])
    except Exception as e:
        print(f"[插件] on_message 派发失败: {type(e).__name__}: {e}")
        return []


def _plugin_log(text: str) -> None:
    """插件日志入口：统一并进主日志流，带「插件」前缀便于在日志页里筛。

    只走 print —— 它会经 StdoutRedirector 落进 global_log_buffer，
    所以「日志输出」页天然就能看到插件的输出，不需要单独一套缓冲。
    """
    try:
        print(f"[插件] {text}")
    except Exception:
        pass


def _gguf_general_name(path) -> str:
    """读取 GGUF 头部的 general.name 元数据（LM Studio 模型 ID 的主要来源）。

    general.* 键位于 GGUF 元数据最前面，扫描到即返回，不会碰到后面的
    tokenizer 大数组；文件损坏/超限时返回空串。
    """
    import struct
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF":
                return ""
            f.read(4)  # version
            tensor_count, kv_count = struct.unpack("<QQ", f.read(16))

            def rd_str():
                n, = struct.unpack("<Q", f.read(8))
                return f.read(n).decode("utf-8", "replace")

            sizes = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}

            def skip_val(t):
                if t == 8:
                    rd_str()
                elif t == 9:
                    etype, = struct.unpack("<I", f.read(4))
                    cnt, = struct.unpack("<Q", f.read(8))
                    if etype == 8:
                        for _ in range(cnt):
                            rd_str()
                    elif etype == 9:
                        for _ in range(cnt):
                            skip_val(9)
                    else:
                        f.seek(cnt * sizes[etype], 1)
                else:
                    f.seek(sizes[t], 1)

            for _ in range(min(kv_count, 4096)):
                key = rd_str()
                t, = struct.unpack("<I", f.read(4))
                if key == "general.name":
                    return rd_str().strip()
                skip_val(t)
    except Exception:
        return ""
    return ""


class WebUIServer:
    def __init__(self, config: ConfigLoader, memory_manager: MemoryManager):
        self.config = config
        # 必须先用当前配置初始化：为 None 时"首次保存"的 diff 恒为空，
        # 用户第一次改 TTS 参数保存不会触发重启，第二次才补上
        self._last_saved_config = dict(config.config or {})
        self.memory_manager = memory_manager
        self.html_path = get_resource_path("webui") / "start.html"
        self.app = web.Application(client_max_size=200 * 1080 * 1080)
        self._update_state_file = memory_manager.data_path / "update_check.json"
        self._update_checked_this_run = False
        self._update_last_result = None
        self._auth_file = memory_manager.data_path / "webui_auth.json"
        self._password = ""
        self._auth_token = None
        # 二级密码：设了才生效，解锁一次在有效期内不再拦敏感操作
        self._second_password = ""
        self._second_unlocked_until = 0.0
        self._second_once = 0
        # 插件系统：插件目录跟着用户数据目录走（%LOCALAPPDATA%\Lovomo\plugins），
        # 这样重装/覆盖更新程序不会把用户的插件一起删掉。
        self._plugins_root = user_data_dir() / "plugins"
        self.plugin_manager = PluginManager(
            self._plugins_root,
            self._plugins_root / "state.json",
            on_change=self._on_plugins_changed)
        self._plugin_runtime = None      # 由 run_backend 注入（能发消息时才建）
        self._plugin_market_cache = {}   # {缓存键: {entries, fetched_at, ...}}
        # 本机推送/下架过哪些插件：市场索引与镜像都有缓存，刚推上去或刚删掉的
        # 未必立刻读得到。落盘保存，重启后仍然算数，避免同一版本被重复推送。
        self._publish_state_file = user_data_dir() / "publish_state.json"
        self._publish_state = {"published": {}, "unpublished": {}}
        self._publish_state_readable = True
        self._load_publish_state()
        self._release_list_cache = {"fetched_at": 0.0, "data": None}
        self._refresh_auth_state()
        self.app.middlewares.append(_webui_error_middleware)
        self.setup_routes()
        self.app.middlewares.append(_make_auth_middleware(self))
        self.app.router.add_post("/api/auth/login", self.handle_auth_login)
        self.app.router.add_get("/api/auth/status", self.handle_auth_status)
        self.app.router.add_post("/api/auth/second", self.handle_auth_second)
        self.app.router.add_get("/api/update/status", self.handle_update_status)
        self.app.router.add_get("/api/update/check", self.handle_update_check)

    def _refresh_auth_state(self):
        """按当前 webui_password 重建会话令牌。「记住我」的令牌与密码哈希一起
        持久化：重启后密码未变则沿用令牌（浏览器旧 Cookie 继续有效），
        密码改变即作废。"""
        import secrets
        password = str(self.config.get("webui_password", "") or "")
        self._password = password
        second = str(self.config.get("webui_second_password", "") or "")
        if second != self._second_password:
            # 二级密码变了（或刚被清空），之前的解锁一律作废
            self._second_unlocked_until = 0.0
            self._second_once = 0
        self._second_password = second
        if not password:
            self._auth_token = None
            return
        phash = hashlib.sha256(password.encode("utf-8")).hexdigest()
        remembered = None
        try:
            if self._auth_file.exists():
                data = json.loads(self._auth_file.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("password_hash") == phash \
                        and float(data.get("expires_at", 0)) > time.time():
                    remembered = str(data.get("secret", "") or "") or None
        except Exception:
            remembered = None
        if remembered and remembered == getattr(self, "_auth_token", None):
            return
        self._auth_token = remembered or secrets.token_hex(16)

    def _second_unlocked(self) -> bool:
        """二级密码是否已解锁。有效时长填 0 时只放行紧接着的那一次请求
        （前端解锁后会自动重放被拦的那次），之后每次敏感操作都要重输。"""
        if self._second_unlocked_until and time.time() < self._second_unlocked_until:
            return True
        if self._second_once:
            self._second_once -= 1
            return True
        return False

    def _auth_remember(self) -> float:
        try:
            if self._auth_file.exists():
                data = json.loads(self._auth_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return float(data.get("expires_at", 0))
        except Exception:
            pass
        return 0.0

    def _auth_remember_save(self, minutes: int):
        try:
            expires = time.time() + minutes * 60 if minutes > 0 else 0
            self._auth_file.write_text(json.dumps({
                "expires_at": expires,
                "secret": self._auth_token,
                "password_hash": hashlib.sha256(
                    (self._password or "").encode("utf-8")).hexdigest(),
            }), encoding="utf-8")
        except Exception:
            pass

    async def handle_auth_status(self, request):
        # 这个接口在页面加载时必被调用一次，版本号搭车返回，
        # 省得为了左上角那行小字再开一个请求（关掉更新检查时也要能显示）
        from modules.updater import APP_VERSION
        version = APP_VERSION
        second = {"second_enabled": bool(self._second_password)}
        if not self._password:
            return web.json_response({"enabled": False, "authed": False,
                                      "version": version, **second})
        remaining = self._auth_remember() - time.time()
        has_valid_cookie = bool(self._auth_token) \
            and request.cookies.get("lovomo_auth") == self._auth_token
        if remaining > 0 and has_valid_cookie:
            return web.json_response({"enabled": True, "authed": True,
                                      "expires_at": self._auth_remember(),
                                      "version": version, **second})
        return web.json_response({"enabled": True, "authed": False,
                                  "version": version, **second})

    async def handle_auth_login(self, request):
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "参数错误"}, status=400)
        if str(payload.get("password", "") or "") == self._password:
            try:
                raw = payload.get("remember_minutes")
                if raw is None or raw == "":
                    raw = self.config.get("webui_auth_ttl_minutes", 30)
                # 0 是合法值（关掉页面即失效），不能当"没填"退回默认
                minutes = max(0, min(int(raw or 0), 60 * 24 * 30))
            except (TypeError, ValueError):
                minutes = max(0, int(self.config.get("webui_auth_ttl_minutes", 30) or 30))
            self._auth_remember_save(minutes)
            resp = web.json_response({"success": True})
            resp.set_cookie("lovomo_auth", self._auth_token,
                            max_age=minutes * 60 if minutes > 0 else None,
                            samesite="Lax", httponly=True)
            return resp
        return web.json_response({"success": False, "error": "密码错误"}, status=401)

    async def handle_auth_second(self, request):
        """校验二级密码并解锁敏感操作。没设二级密码时直接放行。"""
        if not self._second_password:
            return web.json_response({"success": True, "unlocked": False,
                                      "second_enabled": False})
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "参数错误"}, status=400)
        if str(payload.get("password", "") or "") != self._second_password:
            return web.json_response({"success": False, "error": "二级密码错误"}, status=401)
        try:
            raw = payload.get("minutes")
            if raw is None or raw == "":
                raw = self.config.get("webui_second_unlock_minutes", 30)
            # 0 是合法值（每次都要重输），不能当"没填"退回默认
            minutes = max(0, min(int(raw or 0), 60 * 24 * 30))
        except (TypeError, ValueError):
            minutes = max(0, int(self.config.get("webui_second_unlock_minutes", 30) or 0))
        if minutes > 0:
            self._second_unlocked_until = time.time() + minutes * 60
            self._second_once = 0
        else:
            self._second_unlocked_until = 0.0
            self._second_once = 1
        return web.json_response({"success": True, "unlocked": True,
                                  "minutes": minutes, "second_enabled": True})

    def _update_cache(self) -> dict:
        try:
            if self._update_state_file.exists():
                data = json.loads(self._update_state_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _update_save(self, data: dict):
        try:
            self._update_state_file.write_text(json.dumps(data, ensure_ascii=False),
                                               encoding="utf-8")
        except Exception:
            pass

    async def handle_update_status(self, request):
        if not self.config.get("update_check_enabled", True):
            return web.json_response({"enabled": False, "has_update": False})
        from modules.updater import APP_VERSION
        include_pre = bool(self.config.get("update_include_prerelease", False))
        try:
            interval = max(0.0, float(self.config.get("update_check_interval_hours", 24)))
        except (TypeError, ValueError):
            interval = 24.0
        interval *= 3600
        first_this_run = not self._update_checked_this_run
        self._update_checked_this_run = True
        if not first_this_run and interval > 0:
            if self._update_last_result \
                    and time.time() - self._update_last_result[0] < interval:
                return web.json_response(self._update_last_result[1])
            cache = self._update_cache()
            cached_result = cache.get("result") or {}
            try:
                cached_age = time.time() - float(cache.get("checked_at") or 0)
            except (TypeError, ValueError):
                cached_age = interval
            if cached_result.get("current") == APP_VERSION \
                    and bool(cache.get("include_prerelease", False)) == include_pre \
                    and cache.get("checked_at") and cached_age < interval:
                return web.json_response(cached_result)
        payload = await self._update_check_payload()
        self._update_last_result = (time.time(), payload)
        return web.json_response(payload)

    async def handle_update_check(self, request):
        if not self.config.get("update_check_enabled", True):
            return web.json_response({"enabled": False, "has_update": False})
        return web.json_response(await self._update_check_payload())

    def _github_mirrors(self) -> tuple:
        from modules.ghmirror import parse_mirrors
        return parse_mirrors(self.config.get("github_mirrors", ""))

    async def _github_fetch(self, url: str, kind: str = "json", *,
                            timeout: float = 10.0, headers=None):
        """取一个 GitHub 地址，直连不通时按配置里的镜像重试。

        返回 `(数据, 是否跳过了证书校验)`；kind 决定怎么解析响应
        （json / text / bytes），解析不了的响应（镜像返回错误页之类）也算失败，
        换下一个候选。只服务**匿名读**请求 —— 带 token 的写操作一律直连官方。
        """
        from modules.ghmirror import candidates, mirror_label, note_success
        from modules.tls import verified_context, unverified_context, is_cert_error
        hdrs = headers or {}
        last_err = None
        insecure = False
        for cand in candidates(url, self._github_mirrors()):
            for ctx, unverified in ((verified_context(), False),
                                    (unverified_context(), True)):
                try:
                    async with httpx.AsyncClient(timeout=timeout, proxy=None,
                                                 trust_env=False,
                                                 follow_redirects=True,
                                                 verify=ctx) as client:
                        resp = await client.get(cand, headers=hdrs)
                        resp.raise_for_status()
                        if kind == "json":
                            data = resp.json()
                        elif kind == "text":
                            data = resp.text
                        else:
                            data = resp.content
                except Exception as e:
                    last_err = e
                    # 证书问题（本机自签根证书/加速器/公司代理）换个不校验的方式再试；
                    # 其它错误直接换下一个候选地址。
                    if not unverified and is_cert_error(e):
                        continue
                    break
                insecure = insecure or unverified
                if note_success(url, cand):
                    print("[GitHub] " + (f"直连不可用，改用镜像 {mirror_label(cand, url)}"
                                         if cand != url else "直连已恢复"))
                return data, insecure
        raise last_err if last_err else RuntimeError("没有可用的 GitHub 地址")

    async def _fetch_releases(self, api_url: str):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "lovomo-update-check"}
        data, insecure = await self._github_fetch(api_url, "json", headers=headers)
        if insecure:
            print("[更新检查] 证书校验失败，已改用不校验证书的方式取回："
                  "常见原因是本机装了自签根证书（安全软件/网络加速器/公司代理），"
                  "系统证书库与 certifi 都没有它")
        return data, insecure

    async def _update_check_payload(self) -> dict:
        """向 GitHub 查一次最新版本并返回结果（含失败原因），成功时写入缓存。"""
        from modules.updater import APP_VERSION, is_newer, pick_latest_release
        include_pre = bool(self.config.get("update_include_prerelease", False))
        try:
            repo = "slpk1ng/Lovomo"
            api_url = f"https://api.github.com/repos/{repo}/releases?per_page=30"
            release_home = f"https://github.com/{repo}/releases/latest"
            releases, insecure = await self._fetch_releases(api_url)
            items = releases if isinstance(releases, list) else ([releases] if releases else [])
            visible = [r for r in items if isinstance(r, dict) and not r.get("draft")]
            seen = len(visible)
            pre_seen = len([r for r in visible if r.get("prerelease")])
            picked = pick_latest_release(items, include_pre)
            checked_at = time.time()
            if not picked.get("tag"):
                print(f"[更新检查] 未找到可用发布（可见发布 {seen} 个）")
                return {"enabled": True, "current": APP_VERSION, "has_update": False,
                        "checked_at": checked_at, "include_prerelease": include_pre,
                        "releases_seen": seen, "insecure": insecure,
                        "error": "GitHub 上没有可用的发布版本"
                                 "（草稿状态的发布对检查接口不可见，需要先正式发布）"}
            latest = picked["tag"]
            current = APP_VERSION
            has_update = is_newer(latest, current)
            page_url = str(picked.get("url") or "")
            if "github.com" not in page_url:
                page_url = release_home
            result = {"enabled": True, "current": current, "latest": latest,
                      "has_update": has_update, "url": page_url,
                      "prerelease": picked.get("prerelease", False),
                      "checked_at": checked_at, "include_prerelease": include_pre,
                      "releases_seen": seen, "prerelease_seen": pre_seen,
                      "insecure": insecure, "name": picked.get("name", "")}
            self._update_save({"checked_at": checked_at, "result": result,
                               "include_prerelease": include_pre})
            print(f"[更新检查] 可见发布 {seen} 个（标记为预发布 {pre_seen} 个，"
                  f"检查时{'包含' if include_pre else '不含'}预发布）："
                  f"最新 {latest}{'（预发布）' if result['prerelease'] else ''}，"
                  f"当前 {current} → {'发现新版本' if has_update else '已是最新'}"
                  + ("（证书未校验）" if insecure else ""))
            return result
        except Exception as e:
            print(f"[更新检查] 失败: {type(e).__name__}: {e}")
            return {"enabled": True, "current": APP_VERSION, "has_update": False,
                    "checked_at": time.time(), "include_prerelease": include_pre,
                    "error": f"{type(e).__name__}: {e}"}

    def setup_routes(self):
        r = self.app.router
        r.add_get("/api/list", self.handle_list)
        r.add_post("/api/history", self.handle_history)
        r.add_post("/api/delete", self.handle_delete)
        r.add_post("/api/history/delete_messages", self.handle_delete_messages)
        r.add_get("/api/config", self.handle_get_config)
        r.add_post("/api/config/save", self.handle_save_config)
        r.add_get("/api/config/export", self.handle_export_config)
        r.add_post("/api/config/import", self.handle_import_config)
        # 插件系统
        r.add_get("/api/plugins/list", self.handle_plugins_list)
        r.add_post("/api/plugins/upload", self.handle_plugins_upload)
        r.add_post("/api/plugins/inspect", self.handle_plugins_inspect)
        r.add_post("/api/plugins/toggle", self.handle_plugins_toggle)
        r.add_post("/api/plugins/pin", self.handle_plugins_pin)
        r.add_post("/api/github/mirrors/test", self.handle_github_mirror_test)
        r.add_post("/api/plugins/delete", self.handle_plugins_delete)
        r.add_get("/api/plugins/theme", self.handle_plugins_theme)
        r.add_get("/api/plugins/panel", self.handle_plugins_panel)
        r.add_get("/api/plugins/webui", self.handle_plugins_webui)
        r.add_get("/api/plugins/icon", self.handle_plugins_icon)
        r.add_get("/api/plugins/asset", self.handle_plugins_asset)
        r.add_get("/api/plugins/settings", self.handle_plugins_settings_get)
        r.add_post("/api/plugins/settings", self.handle_plugins_settings_set)
        r.add_post("/api/plugins/reload", self.handle_plugins_reload)
        r.add_post("/api/plugins/open_dir", self.handle_plugins_open_dir)
        r.add_post("/api/plugins/publish_token", self.handle_plugins_publish_token)
        r.add_post("/api/plugins/publish_token_clear", self.handle_plugins_publish_token_clear)
        r.add_get("/api/plugins/publish_status", self.handle_plugins_publish_status)
        r.add_post("/api/plugins/publish", self.handle_plugins_publish)
        r.add_post("/api/plugins/unpublish", self.handle_plugins_unpublish)
        r.add_get("/api/plugins/market", self.handle_plugins_market)
        r.add_post("/api/plugins/market_sources", self.handle_plugins_market_sources)
        r.add_get("/api/plugins/readme", self.handle_plugins_readme)
        r.add_get("/api/releases", self.handle_releases)
        r.add_post("/api/releases/download", self.handle_release_download)
        r.add_post("/api/plugins/install_remote", self.handle_plugins_install_remote)
        # 文件夹选择 / 模型列表
        r.add_post("/api/dialog/pick_folder", self.handle_pick_folder)
        r.add_post("/api/dialog/pick_file", self.handle_pick_file)
        r.add_post("/api/llm/scan_models", self.handle_scan_models)
        r.add_post("/api/llm/list_remote_models", self.handle_list_remote_models)
        r.add_get("/api/roles", self.handle_get_roles)
        r.add_post("/api/roles/save", self.handle_save_roles)
        r.add_get("/api/logs", self.handle_get_logs)
        # 情绪音频管理
        r.add_get("/api/emotions/list", self.handle_emotions_list)
        r.add_post("/api/emotions/upload", self.handle_emotions_upload)
        r.add_post("/api/emotions/create", self.handle_emotions_create)
        r.add_post("/api/emotions/delete", self.handle_emotions_delete)
        r.add_get("/api/emotions/audio", self.handle_emotions_audio)
        r.add_post("/api/emotions/text", self.handle_emotions_text)
        r.add_post("/api/emotions/normalize", self.handle_emotions_normalize)
        r.add_post("/api/asr/start", self.handle_asr_start)
        r.add_get("/api/asr/status", self.handle_asr_status)
        # 聊天记录导入导出
        r.add_get("/api/memory/export", self.handle_memory_export)
        r.add_post("/api/memory/import", self.handle_memory_import)
        r.add_get("/api/memory/export_all", self.handle_memory_export_all)
        # 统计
        r.add_get("/api/stats", self.handle_stats)
        r.add_get("/api/performance", self.handle_performance)
        r.add_post("/api/mood/set", self.handle_mood_set)
        r.add_get("/api/sessions", self.handle_sessions)
        # 定时任务 / 待办 / 事件
        r.add_get("/api/jobs", self.handle_jobs)
        r.add_post("/api/jobs/save", self.handle_jobs_save)
        r.add_post("/api/jobs/batch", self.handle_jobs_batch)
        r.add_post("/api/jobs/run", self.handle_jobs_run)
        r.add_get("/api/todos", self.handle_todos)
        r.add_post("/api/todos/add", self.handle_todos_add)
        r.add_post("/api/todos/update", self.handle_todos_update)
        r.add_post("/api/todos/delete", self.handle_todos_delete)
        r.add_post("/api/todos/batch", self.handle_todos_batch)
        r.add_get("/api/events", self.handle_events)
        r.add_post("/api/events/save", self.handle_events_save)
        r.add_post("/api/events/batch", self.handle_events_batch)
        r.add_post("/api/events/test", self.handle_events_test)
        # 工具调用
        r.add_get("/api/tools", self.handle_tools)
        r.add_post("/api/tools/save", self.handle_tools_save)
        r.add_post("/api/tools/test", self.handle_tools_test)
        r.add_get("/api/search/engines", self.handle_search_engines)
        r.add_post("/api/search/engine", self.handle_search_engine_save)
        # RAG
        r.add_get("/api/rag/docs", self.handle_rag_docs)
        r.add_post("/api/rag/upload", self.handle_rag_upload)
        r.add_post("/api/rag/delete", self.handle_rag_delete)
        r.add_post("/api/rag/query", self.handle_rag_query)
        # 用户画像
        r.add_get("/api/profiles", self.handle_profiles)
        r.add_post("/api/profiles/save", self.handle_profiles_save)
        r.add_post("/api/profiles/delete", self.handle_profiles_delete)
        # 自主学习
        r.add_get("/api/lexicon", self.handle_lexicon)
        r.add_post("/api/lexicon/confirm", self.handle_lexicon_confirm)
        r.add_post("/api/lexicon/reject", self.handle_lexicon_reject)
        r.add_post("/api/lexicon/save", self.handle_lexicon_save)
        r.add_post("/api/lexicon/delete", self.handle_lexicon_delete)
        # 表情包
        r.add_get("/api/stickers/list", self.handle_stickers_list)
        r.add_post("/api/stickers/upload", self.handle_stickers_upload)
        r.add_post("/api/stickers/delete", self.handle_stickers_delete)
        r.add_get("/api/stickers/file", self.handle_stickers_file)
        r.add_get("/favicon.ico", self.handle_favicon)
        r.add_get("/", self.handle_index)

    async def handle_favicon(self, request):
        """浏览器每次打开页面都会自己来要 favicon，没有就报 404 刷控制台。"""
        paths = _candidate_icon_paths()
        if not paths:
            return web.Response(status=404)
        return web.FileResponse(paths[0], headers={"Cache-Control": "max-age=86400"})

    async def handle_index(self, request):
        if self.html_path.exists():
            return web.FileResponse(self.html_path)
        return web.Response(text="WebUI 页面未找到", status=404)

    # ---------------- 配置 ----------------
    async def handle_get_config(self, request):
        if not self.config.config:
            self.config.config = self.config.default_config()
        else:
            self.config.config = {**self.config.default_config(), **self.config.config}
        masked = {**self.config.default_config(), **(self.config.config or {})}
        for key in _API_KEY_KEYS + _WEBUI_PASSWORD_KEYS:
            if isinstance(masked.get(key), dict):
                masked[key] = {k: _mask_preview(v) if isinstance(v, str) else v
                               for k, v in masked[key].items()}
            elif masked.get(key):
                masked[key] = _mask_preview(masked[key])
        return web.json_response({**masked, "defaults": self.config.default_config()})

    def _config_export_payload(self) -> dict:
        """导出用配置：密钥一律保持 config.json 的 enc:.../enc2:... 密文形态。

        内存里的配置是解密后的明文，直接导出等于把密钥写成明文送出去；这里做
        两层处理：

        1. 走 _encrypt_api_keys：已是 enc:/enc2: 密文的值原样保留，明文（无论
           sk- 还是别的形式）全部重新加密；
        2. 再做一次兜底扫描 _scrub_plaintext_secrets：凡是出现在敏感字段里、
           或以 sk-/sk_ 之类已知密钥前缀开头的残留明文，一律替换成对应密文，
           并对无法确认来源的值做打码，避免任何明文密钥出现在导出结果里。

        导出结果里 WebUI 访问密码与 API 密钥只能是密文。
        """
        payload = json.loads(json.dumps(self.config.config or {}, ensure_ascii=False))
        _encrypt_api_keys(payload)
        return _scrub_plaintext_secrets(payload)

    async def _save_export_via_dialog(self, default_name: str, content: bytes) -> str:
        """桌面窗口模式下弹系统保存框写盘。

        WebView2 默认禁止页面下载，前端 blob 下载在桌面窗口里是静默无效的；
        所以桌面模式由服务端落盘，返回 "saved" / "cancelled"；浏览器访问没有
        保存框可用，返回 "browser"，由前端按普通下载处理。
        """
        try:
            import webview
        except Exception:
            return "browser"
        window = _WEBVIEW_WINDOW_HOLDER.get("window")
        if window is None:
            return "browser"
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None, lambda: window.create_file_dialog(webview.SAVE_DIALOG,
                                                    save_filename=default_name))
        if not result:
            return "cancelled"
        path = result if isinstance(result, str) else str(result[0])
        Path(path).write_bytes(content)
        return path

    def _export_status_response(self, mode: str, path: str = ""):
        """导出结果状态。

        mode 取值：saved（服务端已写盘）/ cancelled（用户取消）/ error（导出出错）
        / download（前端按浏览器下载处理）。error 时 path 承载错误信息。
        """
        return web.json_response({"mode": mode, "path": path})

    async def handle_export_config(self, request):
        """导出配置。任何异常都必须以 error 状态回给前端，不能静默失败。"""
        try:
            payload = self._config_export_payload()
            content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            saved = await self._save_export_via_dialog("lovomo_config_export.json", content)
            if saved == "browser":
                return _json_file_response(payload, "lovomo_config_export.json")
            if saved == "cancelled":
                return self._export_status_response("cancelled")
            return self._export_status_response("saved", saved)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[配置导出] 失败: {type(e).__name__}: {e}")
            return self._export_status_response("error", f"{type(e).__name__}: {e}")

    async def handle_import_config(self, request):
        try:
            ctype = request.content_type or ""
            if "multipart" in ctype:
                reader = await request.multipart()
                data = None
                async for part in reader:
                    if part.name == "file":
                        raw = await part.read(decode=False)
                        data = json.loads(raw.decode("utf-8"))
                        break
            else:
                data = await request.json()
            if not isinstance(data, dict):
                return web.json_response({"success": False, "error": "配置文件格式错误"}, status=400)
            stored = self.config.config or {}
            for key in _SECRET_FIELD_KEYS:
                masked_value = data.get(key)
                if isinstance(masked_value, dict):
                    stored_keys = stored.get(key) or {}
                    if isinstance(stored_keys, dict) and any(
                            _is_masked_value(v) for v in masked_value.values()):
                        data[key] = {k: (stored_keys.get(k, "") if _is_masked_value(v) else v)
                                     for k, v in masked_value.items()}
                elif _is_masked_value(masked_value):
                    data[key] = stored.get(key, "")
            # 统一走与读盘相同的解密路径：内存里的配置约定是明文（webui_password 也要
            # 一起解密，否则导入后原密码就再也登不进来）。解不开的密文（换过电脑）
            # 由 _decrypt_value 返回 None，调用方保留原密文而不是置空 —— 置空会让
            # 这份密文在下一次落盘时被空串永久覆盖，密钥不可恢复。
            _decrypt_api_keys(data)
            _decrypt_webui_password(data)
            merged = {**self.config.default_config(), **stored, **data}
            self.config.config = merged
            self.config._atomic_save(merged)
            self._after_config_reload()
            return web.json_response({"success": True, "message": "配置已导入并热重载生效！"})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    # ==================================================================
    # 插件系统
    # ==================================================================
    def attach_plugin_runtime(self, runtime) -> None:
        """由 run_backend 注入运行时（那时才拿得到 sender / 配置读取器）。"""
        self._plugin_runtime = runtime

    def _on_plugins_changed(self) -> None:
        """插件启用/禁用/安装/卸载后的热重载入口。"""
        rt = self._plugin_runtime
        if rt is None:
            return
        if not self.config.get("plugins_enabled", True):
            # 总开关关掉时，把已加载的全部卸掉（皮肤也会随之失效）
            try:
                rt.unload_all()
            except Exception as e:
                print(f"[插件] 停用清理失败: {e}")
            return
        try:
            errors = rt.reload_all()
            if errors:
                for pid, err in errors.items():
                    print(f"[插件] {pid} 重载失败: {err}")
        except Exception as e:
            print(f"[插件] 热重载失败: {e}")

    async def handle_plugins_list(self, request):
        """列出已安装插件及其启用状态。"""
        try:
            items = self.plugin_manager.list_plugins()
            for it in items:
                # 皮肤设置面板、features 开关、插件自带功能页都要读插件私有设置，
                # 少任一条件都会让前端拿到空 settings、开关显示错状态。
                if it.get("skin") or it.get("features") or it.get("panel"):
                    it["settings"] = self.plugin_manager.plugin_settings(it["id"])
            return web.json_response({
                "success": True,
                "plugins": items,
                "root": str(self.plugin_manager.root),
                # 打开了「应用外观」的皮肤插件：前端据此给 html 加 skin-on，
                # 没开的插件不改程序原本的背景。
                "active_skins": (self.plugin_manager.active_skin_ids()
                                 if self.config.get("plugins_enabled", True) else []),
                "loaded": sorted((self._plugin_runtime.loaded.keys()
                                  if self._plugin_runtime else [])),
                "commands": (self._plugin_runtime.command_names()
                             if self._plugin_runtime else []),
            })
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_plugins_upload(self, request):
        """上传 .zip 安装插件。带 force=1 表示用户已确认风险清单。"""
        try:
            reader = await request.multipart()
            data = None
            force = False
            file_name = ""
            async for part in reader:
                if part.name == "file":
                    file_name = part.filename or ""
                    data = await part.read(decode=False)
                elif part.name == "force":
                    force = (await part.text()).strip().lower() in ("1", "true", "yes")
            if not data:
                return web.json_response({"success": False, "error": "没有收到文件"}, status=400)
            if file_name and not file_name.lower().endswith(".zip"):
                return web.json_response(
                    {"success": False, "error": "插件包必须是 .zip 格式"}, status=400)
            result = self.plugin_manager.install_zip(data, force=force)
            if result.get("success"):
                print(f"[插件] 已安装：{result.get('name')} "
                      f"(id={result.get('id')}, 覆盖={result.get('replaced')})")
            return web.json_response(result,
                                     status=200 if result.get("success") else 400)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_plugins_inspect(self, request):
        """只审查不安装：让用户在装之前看到风险清单。"""
        try:
            reader = await request.multipart()
            data = None
            async for part in reader:
                if part.name == "file":
                    data = await part.read(decode=False)
            if not data:
                return web.json_response({"success": False, "error": "没有收到文件"}, status=400)
            report = self.plugin_manager.inspect_zip(data)
            return web.json_response({"success": bool(report.get("ok")),
                                      "report": report},
                                     status=200 if report.get("ok") else 400)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_plugins_toggle(self, request):
        """启用 / 禁用某个插件。"""
        try:
            payload = await request.json()
            pid = str(payload.get("id") or "").strip()
            enabled = bool(payload.get("enabled", True))
            info = self.plugin_manager.get(pid)
            if not info:
                return web.json_response({"success": False, "error": "插件不存在"}, status=404)
            self.plugin_manager.set_enabled(pid, enabled)
            return web.json_response({"success": True, "id": pid, "enabled": enabled})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def _timed_get(self, url: str, timeout: float = 6.0):
        """取一次地址并计时，返回 (毫秒, 是否成功, 说明)。"""
        from modules.tls import verified_context
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout, proxy=None, trust_env=False,
                                         follow_redirects=True,
                                         verify=verified_context()) as client:
                resp = await client.get(url, headers={"User-Agent": "lovomo-mirror-test"})
            ms = int((time.monotonic() - t0) * 1000)
            if resp.status_code == 200:
                return ms, True, ""
            return ms, False, f"HTTP {resp.status_code}"
        except Exception as e:
            return int((time.monotonic() - t0) * 1000), False, type(e).__name__

    async def _probe_mirror(self, template: str, repo: str) -> dict:
        """测一个镜像模板：raw 与 api 各探一次，取最快的一次当它的速度。"""
        from modules.ghmirror import apply_template, probe_urls, template_host
        best = None
        kinds = []
        note = ""
        for kind, url in probe_urls(repo):
            cand = apply_template(str(template), url)
            if not cand:
                continue
            ms, ok, why = await self._timed_get(cand)
            if ok:
                kinds.append(kind)
                best = ms if best is None else min(best, ms)
            else:
                note = note or why
        return {
            "template": str(template),
            "host": template_host(str(template)) or str(template),
            "ms": int(best) if best is not None else 0,
            "ok": best is not None,
            "kinds": "+".join(kinds),
            "note": "" if best is not None else (note or "不可用"),
        }

    async def handle_github_mirror_test(self, request):
        """给配置里的每个镜像测一次延迟，按快的在前返回。"""
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        from modules.ghmirror import parse_mirrors
        raw = payload.get("mirrors")
        templates = parse_mirrors(raw if raw is not None
                                  else self.config.get("github_mirrors", ""))
        if not templates:
            return web.json_response({"success": False, "error": "没有配置镜像地址"},
                                     status=400)
        repo = (str(self.config.get("plugin_market_repo", "") or "").strip()
                or str(self.config.get("plugin_release_repo", "") or "").strip())
        if not repo:
            return web.json_response({"success": False, "error": "没有可用的仓库地址"},
                                     status=400)
        items = await asyncio.gather(*[self._probe_mirror(t, repo) for t in templates])
        items = sorted(items, key=lambda r: (0 if r["ok"] else 1,
                                             r["ms"] if r["ok"] else 10 ** 6))
        print("[GitHub] 镜像测速：" + "；".join(
            f"{r['host']} {r['ms']}ms" if r["ok"] else f"{r['host']} {r['note']}"
            for r in items))
        return web.json_response({"success": True, "repo": repo, "mirrors": items})

    async def handle_plugins_pin(self, request):
        """置顶 / 取消置顶某个插件（只影响「插件」页的排序）。"""
        try:
            payload = await request.json()
            pid = str(payload.get("id") or "").strip()
            pinned = bool(payload.get("pinned", True))
            if not self.plugin_manager.get(pid):
                return web.json_response({"success": False, "error": "插件不存在"}, status=404)
            self.plugin_manager.set_pinned(pid, pinned)
            print(f"[插件] {'已置顶' if pinned else '已取消置顶'}：{pid}")
            return web.json_response({"success": True, "id": pid, "pinned": pinned})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_plugins_delete(self, request):
        """卸载插件。keep_settings / keep_data 为真时保留对应的配置与数据。"""
        try:
            payload = await request.json()
            pid = str(payload.get("id") or "").strip()
            keep_settings = bool(payload.get("keep_settings"))
            keep_data = bool(payload.get("keep_data"))
            if not self.plugin_manager.get(pid):
                return web.json_response({"success": False, "error": "插件不存在"}, status=404)
            # 先卸载运行时再删目录：Windows 上插件模块还在 sys.modules 里时
            # 文件句柄没释放，rmtree 会失败（现象是"点卸载没反应"）。
            ok = self.plugin_manager.uninstall(pid, keep_settings=keep_settings,
                                               keep_data=keep_data)
            if ok:
                kept = [name for name, flag in (("配置", keep_settings),
                                                ("数据", keep_data)) if flag]
                print(f"[插件] 已卸载：{pid}"
                      + (f"（保留{'、'.join(kept)}）" if kept else ""))
            return web.json_response({"success": ok,
                                      "error": "" if ok else "删除目录失败"})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_plugins_theme(self, request):
        """把所有启用插件的皮肤 CSS 拼起来给前端注入。"""
        try:
            if not self.config.get("plugins_enabled", True):
                return web.Response(text="/* 插件系统已关闭 */",
                                    content_type="text/css", charset="utf-8")
            css = self.plugin_manager.theme_css()
            return web.Response(text=css, content_type="text/css",
                                charset="utf-8")
        except Exception as e:
            return web.Response(text=f"/* 读取皮肤失败: {e} */",
                                content_type="text/css", charset="utf-8")

    async def handle_plugins_icon(self, request):
        """返回插件图标，供列表展示。"""
        try:
            pid = str(request.query.get("id") or "").strip()
            f = self.plugin_manager.icon_file(pid)
            if f is None:
                return web.json_response({"success": False, "error": "没有图标"},
                                         status=404)
            mime = {".png": "image/png", ".jpg": "image/jpeg",
                    ".svg": "image/svg+xml", ".ico": "image/x-icon"}.get(
                f.suffix.lower(), "application/octet-stream")
            return web.Response(body=f.read_bytes(), content_type=mime)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_plugins_open_dir(self, request):
        """在资源管理器里打开插件目录，方便用户手动放插件。"""
        try:
            root = self.plugin_manager.ensure_root()
            if os.name == "nt":
                os.startfile(str(root))  # noqa: S606
            return web.json_response({"success": True, "path": str(root)})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_plugins_asset(self, request):
        """读取插件 data 目录下的静态资源（背景图、样式、字体等）。"""
        try:
            pid = str(request.query.get("id") or "").strip()
            name = str(request.query.get("name") or "").strip()
            f, mime = self.plugin_manager.asset_file(pid, name)
            if f is None:
                return web.Response(status=404, text="not found")
            return web.Response(body=f.read_bytes(), content_type=mime)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_plugins_settings_get(self, request):
        """读取某个插件的私有设置（插件未安装时返回空对象）。"""
        try:
            pid = str(request.query.get("id") or "").strip()
            if not self.plugin_manager.get(pid):
                return web.json_response({"success": False, "error": "插件不存在"},
                                         status=404)
            return web.json_response({"success": True, "id": pid,
                                      "settings": self.plugin_manager.plugin_settings(pid)})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_plugins_settings_set(self, request):
        """保存某个插件的私有设置（只落盘，不立即生效）。

        落盘前按插件声明的字段筛一遍：只有插件在清单里声明过的键会被保留，
        值也按声明的类型收敛。否则设置文件会变成任意 JSON 的暂存区 ——
        插件写进去什么，下次读出来就是什么。

        落盘不等于生效：插件运行时与皮肤 CSS 都读磁盘上的这份文件，但要让
        改动真的作用到已加载的插件上，得由「保存并重载」走 `plugins/reload`
        重新加载运行时。这样用户一次调好几项也不会中途反复生效。
        """
        try:
            payload = await request.json()
            pid = str(payload.get("id") or "").strip()
            if not self.plugin_manager.get(pid):
                return web.json_response({"success": False, "error": "插件不存在"},
                                         status=404)
            settings = payload.get("settings")
            if not isinstance(settings, dict):
                return web.json_response({"success": False, "error": "settings 必须是对象"},
                                         status=400)
            clean = self.plugin_manager.filter_settings(pid, settings)
            ok = self.plugin_manager.save_plugin_settings(pid, clean)
            return web.json_response({"success": ok,
                                      "draft": clean if ok else {},
                                      "error": "" if ok else "写入设置失败"})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_plugins_reload(self, request):
        """重载插件运行时 + 提交设置草稿 + 刷新皮肤。

        三件事缺一不可：
          1. `commit_settings()` 把磁盘上的设置草稿提交成生效值 —— 皮肤变量
             与插件读到的设置都以此为准；
          2. `_on_plugins_changed()` 重新加载插件运行时（钩子、指令、开关）；
          3. 回传最新 active_skins，前端重挂 theme 链接与 skin-on 类。
        """
        try:
            pid = ""
            try:
                payload = await request.json()
                pid = str(payload.get("id") or "").strip()
            except Exception:
                pid = ""
            if pid and not self.plugin_manager.get(pid):
                return web.json_response({"success": False, "error": "插件不存在"},
                                         status=404)
            if not self.config.get("plugins_enabled", True):
                return web.json_response({"success": False,
                                          "error": "插件系统已在配置里关闭"},
                                         status=400)
            self.plugin_manager.commit_settings(pid or None)
            self._on_plugins_changed()
            return web.json_response({
                "success": True,
                "id": pid,
                "settings": self.plugin_manager.plugin_settings(pid) if pid else {},
                "active_skins": self.plugin_manager.active_skin_ids(),
                "loaded": sorted(self._plugin_runtime.loaded.keys()
                                 if self._plugin_runtime else []),
                "commands": (self._plugin_runtime.command_names()
                             if self._plugin_runtime else []),
            })
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def _serve_plugin_page_file(self, request, default_name: str):
        """把插件目录里的界面文件原样回给浏览器（自带功能页 / webui 及其同目录资源）。

        `?id=<插件>&name=<相对路径>`；name 留空时用 default_name。
        页面本体（name 就是 default_name 的那次请求）会先注入桥接脚本。
        """
        try:
            pid = str(request.query.get("id") or "").strip()
            info = self.plugin_manager.get(pid)
            if not info:
                return web.Response(status=404, text="plugin not found")
            name = str(request.query.get("name") or "").strip() or default_name
            if not name:
                return web.Response(status=404, text="no page file")
            f, mime = self.plugin_manager.asset_file(pid, name, root_scope=True)
            if f is None:
                return web.Response(status=404, text="not found")
            body = f.read_bytes()
            if name == default_name and "html" in mime:
                body = self._inject_plugin_bridge(body, pid, info)
            return web.Response(body=body, content_type=mime)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    def _inject_plugin_bridge(self, body: bytes, pid: str, info: dict) -> bytes:
        """把桥接脚本插到插件页面最前面（插不进就当普通页面返回）。"""
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            return body
        payload = {
            "id": pid,
            "name": str(info.get("name") or pid),
            "version": str(info.get("version") or ""),
            "enabled": bool(info.get("enabled")),
            "settings": self.plugin_manager.plugin_settings(pid),
        }
        script = PLUGIN_BRIDGE_TEMPLATE.replace(
            "/*__LOVOMO_INFO__*/ null",
            json.dumps(payload, ensure_ascii=False).replace("</", "<\\/"))
        head = text.lower().find("<head>")
        if head >= 0:
            at = head + len("<head>")
            text = text[:at] + script + text[at:]
        else:
            text = script + text
        return text.encode("utf-8")

    async def handle_plugins_panel(self, request):
        """插件自带的功能页界面（清单里的 panel.html）。"""
        pid = str(request.query.get("id") or "").strip()
        info = self.plugin_manager.get(pid) or {}
        return await self._serve_plugin_page_file(
            request, str((info.get("panel") or {}).get("html") or "").strip())

    async def handle_plugins_webui(self, request):
        """插件自带的 webui（插件根目录的 webui.html）。"""
        return await self._serve_plugin_page_file(request, PLUGIN_WEBUI_NAME)

    async def _market_raw_text(self, url: str):
        """从 raw.githubusercontent.com 取一段文本，取不到返回 None。"""
        try:
            text, _ = await self._github_fetch(
                url, "text", headers={"User-Agent": "lovomo-plugin-market"})
            return text
        except Exception:
            return None

    async def _market_manifest(self, repo: str, branch: str):
        """取某条插件分支的清单，返回 (清单字典, 清单文件名)。

        分支根目录放 plugin.yaml 或 plugin.json 都行，yaml 优先 —— 与
        本地安装走的是同一套优先级（modules.plugins.MANIFEST_NAMES）。
        """
        from modules.market import RAW_TMPL
        from modules.plugins import MANIFEST_NAMES, parse_manifest_text
        for name in MANIFEST_NAMES:
            text = await self._market_raw_text(
                RAW_TMPL.format(repo=repo, branch=branch, path=name))
            if text is None:
                continue
            data = parse_manifest_text(text, name)
            if data:
                return data, name
        return None, ""

    async def _market_commit_time(self, repo: str, sha: str) -> float:
        """按 SHA 单独查提交时间。

        插件分支是独立根提交，不在默认分支的提交列表里，靠
        `/commits` 那一次批量查询拿不到它的时间。
        """
        from modules.market import parse_iso_time
        try:
            data, _ = await self._fetch_releases(
                f"https://api.github.com/repos/{repo}/git/commits/{sha}")
        except Exception:
            return 0.0
        if not isinstance(data, dict):
            return 0.0
        return float(parse_iso_time(str((data.get("committer") or {}).get("date") or "")) or 0.0)


    async def _market_branches(self, repo: str):
        """列出仓库的全部分支（分页拉全，返回 (分支列表, 是否跳过证书校验)）。

        GitHub 一次最多给 100 条，插件多了后面的会被截断成"不存在"，
        所以按页取到不足一页为止。
        """
        out, insecure = [], False
        for page in range(1, MAX_BRANCH_PAGES + 1):
            data, insecure_p = await self._fetch_releases(
                f"https://api.github.com/repos/{repo}/branches"
                f"?per_page=100&page={page}")
            insecure = insecure or insecure_p
            batch = [b for b in (data if isinstance(data, list) else [])
                     if isinstance(b, dict)]
            out += batch
            if len(batch) < 100:
                break
        return out, insecure

    async def _market_tags(self, repo: str, prefix: str) -> list:
        """列出仓库里以插件前缀开头的标签名（分页拉全）。

        只按前缀筛选，形状合不合法交给 `parse_version_tag()` 判定 ——
        程序自己的版本标签（如 `1.2.0.0`）不会被误认成插件。
        """
        out = []
        for page in range(1, MAX_BRANCH_PAGES + 1):
            data, _ = await self._fetch_releases(
                f"https://api.github.com/repos/{repo}/git/matching-refs/tags/{prefix}"
                f"?per_page=100&page={page}")
            batch = [str(r.get("ref") or "").rsplit("/", 1)[-1]
                     for r in (data if isinstance(data, list) else [])
                     if isinstance(r, dict) and r.get("ref")]
            out += [t for t in batch if t]
            if len(batch) < 100:
                break
        return out

    async def _market_branch_entries(self, repo: str, prefix: str) -> dict:
        """扫插件分支并组装市场条目（branches / commits / releases / raw）。"""
        from modules.market import (ARCHIVE_TMPL, RAW_TMPL, build_entry,
                                    count_reactions, matches_branch_prefix,
                                    plugin_id_from_branch, release_belongs_to,
                                    parse_iso_time, summarize_releases)
        warnings = []
        insecure = False
        branches, insecure_b = await self._market_branches(repo)
        insecure = insecure or insecure_b
        picked = [str(b.get("name") or "") for b in (branches or [])
                  if isinstance(b, dict) and matches_branch_prefix(b.get("name"), prefix)]
        if not picked:
            return {"entries": [], "warnings":
                    [f"{repo} 上没有以「{prefix}」开头的分支"], "insecure": insecure}

        times = {}
        try:
            commits, insecure_c = await self._fetch_releases(
                f"https://api.github.com/repos/{repo}/commits?per_page=100")
            insecure = insecure or insecure_c
            for c in (commits if isinstance(commits, list) else []):
                if not isinstance(c, dict):
                    continue
                sha = str(c.get("sha") or "")
                date = str(((c.get("commit") or {}).get("committer") or {})
                           .get("date") or "")
                if sha and date:
                    times[sha] = parse_iso_time(date)
        except Exception as e:
            warnings.append(f"拿不到分支提交时间：{type(e).__name__}")

        releases = []
        try:
            data, insecure_r = await self._fetch_releases(
                f"https://api.github.com/repos/{repo}/releases?per_page=100")
            insecure = insecure or insecure_r
            releases = [r for r in (data if isinstance(data, list) else [])
                        if isinstance(r, dict)]
        except Exception as e:
            warnings.append(f"拿不到 Release 信息（下载量/收藏量会显示 0）：{type(e).__name__}")

        branch_meta = {b: {"id": plugin_id_from_branch(b, prefix)} for b in picked}
        live = [r for r in releases if not r.get("draft")]
        # 收藏量要按 Release 单独查点赞数（列表接口不给总数），
        # 只查确实属于这些插件的那些，并且设上限，别把配额烧光。
        targets = [r for r in live if any(release_belongs_to(r, b, branch_meta[b]["id"])
                                          for b in picked)]
        for rel in targets[:MAX_REACTION_LOOKUPS]:
            rid = rel.get("id")
            if not rid:
                continue
            try:
                rx, _ = await self._fetch_releases(
                    f"https://api.github.com/repos/{repo}/releases/{rid}"
                    "/reactions?per_page=100")
                rel["_reactions"] = count_reactions(rx)
            except Exception:
                rel["_reactions"] = 0
        stats = summarize_releases(live, branch_meta)

        entries = []
        for branch in picked:
            manifest, _manifest_name = await self._market_manifest(repo, branch)
            if not manifest:
                from modules.plugins import MANIFEST_NAMES
                warnings.append(f"{branch}：分支根目录没有 "
                                + " / ".join(MANIFEST_NAMES) + "，已跳过")
                continue
            sha = ""
            for b in (branches or []):
                if isinstance(b, dict) and str(b.get("name")) == branch:
                    sha = str(((b.get("commit") or {}).get("sha")) or "")
                    break
            entries.append(build_entry(
                branch, prefix, manifest,
                raw_base=RAW_TMPL.format(repo=repo, branch=branch, path="").rstrip("/"),
                repo=repo, stats=stats.get(branch) or {},
                commit_at=(float(times.get(sha) or 0.0)
                           or (await self._market_commit_time(repo, sha) if sha else 0.0))))
        return {"entries": entries, "warnings": warnings, "insecure": insecure}

    async def _market_default_branch(self, repo: str) -> str:
        """仓库的默认分支名（取不到就退回 main）。"""
        try:
            data, _ = await self._fetch_releases(
                f"https://api.github.com/repos/{repo}")
        except Exception:
            return "main"
        if not isinstance(data, dict):
            return "main"
        return str(data.get("default_branch") or "").strip() or "main"

    async def _market_sources(self, repo: str, path: str) -> list:
        """读市场仓库里的第三方来源清单（每行一个「用户名/仓库名」）。

        清单放在仓库里而不是用户配置里，是为了让别人能提 PR 加一行就上架 ——
        改的是市场仓库、不是每个用户的本机配置。读的是仓库的默认分支，与
        市场索引同一套口径；写回市场仓库自己的行会被剔掉，免得扫两遍。
        """
        from modules.market import RAW_TMPL, parse_market_repos
        branch = await self._market_default_branch(repo)
        url = RAW_TMPL.format(repo=repo, branch=branch, path=path)
        try:
            text, _ = await self._github_fetch(url, "text")
        except Exception as e:
            print(f"[插件市场] 来源清单 {path} 读不到：{type(e).__name__}: {e}")
            return []
        return [r for r in parse_market_repos(text)
                if r.lower() != repo.lower()][:MARKET_SOURCE_REPOS_MAX]


    async def _market_releases(self, repo: str, pids) -> list:
        """仓库里属于这些插件的 Release 列表，顺带补上点赞数。

        下载量与收藏量都来自 Release：列表接口不给点赞总数，所以只对确实
        属于这些插件的 Release 单独查一次，并且设上限，别把配额烧光。
        """
        from modules.market import count_reactions, release_for_plugin
        try:
            data, _ = await self._fetch_releases(
                f"https://api.github.com/repos/{repo}/releases?per_page=100")
        except Exception as e:
            print(f"[插件市场] 拿不到 {repo} 的 Release：{type(e).__name__}: {e}")
            return []
        wanted = [str(p) for p in (pids or []) if str(p or "").strip()]
        live = [r for r in (data if isinstance(data, list) else [])
                if isinstance(r, dict) and not r.get("draft")]
        targets = [r for r in live
                   if any(release_for_plugin(r, p) for p in wanted)]
        for rel in targets[:MAX_REACTION_LOOKUPS]:
            rid = rel.get("id")
            if not rid:
                continue
            try:
                rx, _ = await self._fetch_releases(
                    f"https://api.github.com/repos/{repo}/releases/{rid}"
                    "/reactions?per_page=100")
                rel["_reactions"] = count_reactions(rx)
            except Exception:
                rel["_reactions"] = 0
        return live


    async def _market_index_entries(self, repo: str, market_path: str) -> dict:
        """读市场仓库里的 index.json 索引（官方市场的条目来源）。

        索引条目要带 `source_repo`（或 `repo`）：安装时会拿它校验"插件是不是
        真从这个仓库来的"。没写就按 `download` 链接的归属推断，再退回归属
        索引自己所在的仓库。下载量与收藏量按插件 id 从 Release 汇总，
        download 优先用 Release 里的 zip 附件。
        """
        from modules.market import plugin_category, summarize_plugin_releases
        from modules.plugins import repo_slug
        branch = await self._market_default_branch(repo)
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/{market_path}"
        try:
            data, insecure = await self._fetch_releases(url)
        except Exception as e:
            return {"entries": [], "insecure": False,
                    "warnings": [f"市场索引 {market_path} 读不到（{type(e).__name__}）"]}
        entries = data if isinstance(data, list) else (
            data.get("plugins") if isinstance(data, dict) else None)
        out = []
        for raw in (entries or []):
            if not isinstance(raw, dict):
                continue
            pid = str(raw.get("id") or "").strip()
            if not pid:
                continue
            item = {k: raw.get(k) for k in
                    ("name", "version", "author", "description", "type",
                     "download", "homepage", "page_url", "tags")}
            item["id"] = pid
            item["source"] = "index"
            item.setdefault("branch", "")
            item["category"] = plugin_category(raw)
            item["logo_url"] = str(raw.get("logo_url") or "")
            item["icon_url"] = str(raw.get("icon_url") or "")
            item["github"] = str(raw.get("github") or "")
            item["source_repo"] = (
                str(raw.get("source_repo") or raw.get("repo") or "").strip()
                or repo_slug(item.get("download")) or repo)
            out.append(item)
        releases = await self._market_releases(repo, [i["id"] for i in out])
        for item in out:
            one = summarize_plugin_releases(releases, item["id"])
            item["downloads"] = int(one["downloads"] or 0)
            item["favorites"] = int(one["favorites"] or 0)
            item["download"] = str(one["asset_url"] or item.get("download") or "")
            item["release_tag"] = str(one["release_tag"] or "")
            item["released_at"] = float(one["released_at"] or 0.0)
            item["updated_at"] = float(one["released_at"] or 0.0)
        return {"entries": out, "warnings": [], "insecure": insecure}


    async def _market_scan_repos(self, repos, prefix: str,
                                 market_path: str = "") -> dict:
        """逐个仓库取插件条目并合并（来源清单与第三方市场用）。

        每个仓库先读索引（「发布到市场」写的就是它），索引里没有条目时再按
        分支扫 —— 两种上架方式都能被扫到。单个仓库出错只记一条 warning，
        不牵连别的仓库：地址是用户自己填的，写错一个不该让整个市场打不开。
        """
        index_path = str(market_path or "").strip() or "plugins/index.json"
        entries, warnings, insecure = [], [], False
        for repo in repos:
            try:
                got = await self._market_index_entries(repo, index_path)
                insecure = insecure or bool(got.get("insecure"))
                warns = list(got.get("warnings") or [])
                if not got["entries"]:
                    # 索引里没有条目：这个仓库可能是按分支上架的，回退扫分支。
                    # 分支里有插件时就不再提索引读不到 —— 那是这种上架方式的正常情况
                    by_branch = await self._market_branch_entries(repo, prefix)
                    insecure = insecure or bool(by_branch.get("insecure"))
                    if by_branch["entries"]:
                        warns = []
                    got = by_branch
                    warns += list(by_branch.get("warnings") or [])
            except Exception as e:
                warnings.append(f"{repo}：拉取失败（{type(e).__name__}）")
                continue
            entries += got["entries"]
            warnings += [w if w.startswith(repo) else f"{repo}：{w}" for w in warns]
        return {"entries": entries, "warnings": warnings, "insecure": insecure}


    async def _plugins_market_payload(self, force: bool = False, sort: str = "latest",
                                      market: str = "official") -> dict:
        """插件市场数据：扫索引/分支、按排序键返回（条目缓存 30 分钟）。

        market = official 读官方市场仓库里的索引（索引里没有条目时回退扫它的
        分支），再叠加来源清单里的仓库；thirdparty 按 plugin_market_thirdparty
        逐行列出的仓库扫分支。
        """
        from modules.market import SORT_KEYS, parse_market_repos, sort_entries
        kind = "thirdparty" if str(market or "").strip().lower() == "thirdparty" \
            else "official"
        raw_sources = str(self.config.get("plugin_market_thirdparty", "") or "")
        repos = parse_market_repos(raw_sources) if kind == "thirdparty" else [
            str(self.config.get("plugin_market_repo", "") or "").strip()
            or "slpk1ng/Lovomo"]
        prefix = str(self.config.get("plugin_market_branch_prefix", "") or "").strip() \
            or "lovomo_plugin"
        market_path = str(self.config.get("plugin_market_path", "") or "").strip() \
            or "plugins/index.json"
        sources_path = str(self.config.get("plugin_market_sources_path", "") or "").strip()
        sort_key = str(sort or "latest").strip().lower()
        if sort_key not in SORT_KEYS:
            sort_key = "latest"

        caches = self._plugin_market_cache
        cache = caches.get(kind + "|" + ",".join(repos)) or {}
        now = time.time()
        if (not force and cache.get("entries") is not None
                and now - float(cache.get("fetched_at") or 0) < 1800):
            entries = cache["entries"]
            warnings = list(cache.get("warnings") or [])
            source = cache.get("source") or "branches"
            insecure = bool(cache.get("insecure"))
            error = str(cache.get("error") or "")
        else:
            source, warnings, insecure, error = "branches", [], False, ""
            entries = []
            if kind == "thirdparty" and not repos:
                warnings.append("还没有配置第三方市场地址，"
                                "按每行一个「用户名/仓库名」填好再刷新")
            elif kind == "thirdparty":
                got = await self._market_scan_repos(repos, prefix, market_path)
                entries, warnings = got["entries"], got["warnings"]
                insecure = bool(got.get("insecure"))
            else:
                repo = repos[0]
                try:
                    index = await self._market_index_entries(repo, market_path)
                    if index["entries"]:
                        entries = index["entries"]
                        warnings = warnings + list(index.get("warnings") or [])
                        source = "index"
                    else:
                        # 自建市场仍可能只靠分支上架：索引里没东西时回退扫分支
                        got = await self._market_branch_entries(repo, prefix)
                        entries, warnings = got["entries"], got["warnings"]
                        source = "branches"
                        insecure = insecure or bool(got.get("insecure"))
                        if not entries:
                            warnings = warnings + list(index.get("warnings") or [])
                    insecure = insecure or bool(index.get("insecure"))
                    if sources_path:
                        extra = await self._market_scan_repos(
                            await self._market_sources(repo, sources_path),
                            prefix, market_path)
                        entries = entries + extra["entries"]
                        warnings = warnings + extra["warnings"]
                        insecure = insecure or bool(extra.get("insecure"))
                except Exception as e:
                    error = f"拉取市场失败：{type(e).__name__}: {e}"
                    print(f"[插件市场] {error}")
            for stale in [k for k, v in caches.items()
                          if now - float(v.get("fetched_at") or 0) >= 1800]:
                caches.pop(stale, None)
            caches[kind + "|" + ",".join(repos)] = {
                "entries": entries, "fetched_at": now, "source": source,
                "warnings": warnings, "insecure": insecure, "error": error}

        installed = {p["id"]: p for p in self.plugin_manager.list_plugins()}
        plugins = []
        for e in entries:
            item = dict(e)
            cur = installed.get(item.get("id") or "")
            item["installed"] = bool(cur)
            item["installed_version"] = (cur or {}).get("version", "")
            item["enabled"] = bool((cur or {}).get("enabled"))
            plugins.append(item)
        plugins = sort_entries(plugins, sort_key)
        if warnings:
            print("[插件市场] " + "；".join(warnings[:5]))
        result = {"success": not error, "plugins": plugins, "count": len(plugins),
                  "sort": sort_key, "source": source, "market": kind,
                  "market_text": raw_sources, "markets": repos,
                  "repo": "、".join(repos), "branch_prefix": prefix,
                  "warnings": warnings, "insecure": insecure, "fetched_at": now,
                  "url": f"https://github.com/{repos[0]}" if repos else ""}
        if error:
            result["error"] = error
            result["hint"] = ("可以先手动上传插件 zip 安装；"
                              "GitHub 未登录访问接口有 60 次/小时的限制，"
                              "过一会儿再刷新即可。")
        return result

    async def handle_plugins_market(self, request):
        force = str(request.query.get("refresh") or "") in ("1", "true")
        sort = str(request.query.get("sort") or "latest")
        market = str(request.query.get("market") or "official")
        return web.json_response(
            await self._plugins_market_payload(force, sort, market))

    async def handle_plugins_market_sources(self, request):
        """保存第三方市场地址（每行一个「用户名/仓库名」）。"""
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "请求体不是合法 JSON"},
                                     status=400)
        from modules.market import parse_market_repos
        text = str(payload.get("text") or "").strip()
        markets = parse_market_repos(text)
        self.config.config["plugin_market_thirdparty"] = text
        self.config._atomic_save(self.config.config)
        self._plugin_market_cache.clear()
        print(f"[插件市场] 第三方市场地址已保存：识别到 {len(markets)} 个"
              + (f"（{'、'.join(markets)}）" if markets else ""))
        return web.json_response({"success": True, "markets": markets,
                                  "count": len(markets), "text": text})

    async def handle_plugins_readme(self, request):
        """插件包内自带文档的内容（kind = readme / update）。"""
        pid = str(request.query.get("id") or "").strip()
        kind = str(request.query.get("kind") or "readme").strip().lower()
        if kind not in ("readme", "update"):
            return web.json_response({"success": False, "error": "kind 只能是 readme 或 update"},
                                     status=400)
        if not self.plugin_manager.get(pid):
            return web.json_response({"success": False, "error": "插件不存在"}, status=404)
        doc = self.plugin_manager.read_doc_text(pid, kind)
        return web.json_response({"success": True, "id": pid, "kind": kind, **doc})

    async def handle_releases(self, request):
        """仓库的全部 Releases（含各版本的资源与下载量）。"""
        repo = str(self.config.get("plugin_release_repo", "") or "").strip() \
            or "slpk1ng/Lovomo"
        force = str(request.query.get("refresh") or "") in ("1", "true")
        cache = self._release_list_cache
        now = time.time()
        if (not force and cache.get("data") is not None
                and now - float(cache.get("fetched_at") or 0) < 1800):
            return web.json_response(cache["data"])
        try:
            data, insecure = await self._fetch_releases(
                f"https://api.github.com/repos/{repo}/releases?per_page=100")
            items = data if isinstance(data, list) else []
            releases = []
            for rel in items:
                if not isinstance(rel, dict):
                    continue
                assets = []
                for a in (rel.get("assets") or []):
                    if not isinstance(a, dict):
                        continue
                    assets.append({
                        "name": str(a.get("name") or "")[:120],
                        "size": int(a.get("size") or 0),
                        "downloads": int(a.get("download_count") or 0),
                        "url": str(a.get("browser_download_url") or ""),
                    })
                releases.append({
                    "tag": str(rel.get("tag_name") or ""),
                    "name": str(rel.get("name") or rel.get("tag_name") or ""),
                    "draft": bool(rel.get("draft")),
                    "prerelease": bool(rel.get("prerelease")),
                    "published_at": str(rel.get("published_at") or ""),
                    "created_at": str(rel.get("created_at") or ""),
                    "body": str(rel.get("body") or "")[:4000],
                    "url": str(rel.get("html_url") or ""),
                    "downloads": sum(x["downloads"] for x in assets),
                    "assets": assets,
                })
            result = {"success": True, "repo": repo, "releases": releases,
                      "count": len(releases), "insecure": insecure,
                      "fetched_at": now}
        except Exception as e:
            result = {"success": False, "repo": repo, "releases": [],
                      "error": f"拿不到 Releases：{type(e).__name__}: {e}",
                      "fetched_at": now}
            print(f"[历史更新] {result['error']}")
        cache.update({"data": result, "fetched_at": now})
        return web.json_response(result)

    async def handle_release_download(self, request):
        """下载 Release 资源并保存到本地（由服务端完成下载）。"""
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "参数错误"}, status=400)
        url = str(payload.get("url") or "").strip()
        name = str(payload.get("name") or "").strip() or "download.bin"
        repo = str(self.config.get("plugin_release_repo", "") or "").strip() \
            or "slpk1ng/Lovomo"
        # 只允许下本仓库 Release 的资源
        head = url.lower()
        allowed = (
            head.startswith(f"https://github.com/{repo.lower()}/releases/download/")
            or head.startswith("https://objects.githubusercontent.com/")
            or head.startswith("https://github-releases.githubusercontent.com/")
        )
        if not allowed:
            return web.json_response(
                {"success": False, "error": "只允许下载本仓库 Releases 里的资源"},
                status=400)
        try:
            content, insecure = await self._fetch_bytes(url)
        except Exception as e:
            return web.json_response({"success": False,
                                      "error": f"下载失败：{type(e).__name__}: {e}"},
                                     status=400)
        safe = safe_asset_name(name, Path(name).suffix)
        mode = await self._save_export_via_dialog(safe, content)
        if mode == "cancelled":
            return web.json_response({"success": False, "mode": "cancelled"})
        if mode != "browser":
            print(f"已保存 Release 资源：{safe} → {mode}")
            return web.json_response({"success": True, "mode": "saved",
                                      "path": mode, "name": safe})
        # 浏览器访问：没有系统保存框，直接回流给浏览器下载
        from urllib.parse import quote as _quote
        return web.Response(
            body=content, content_type="application/octet-stream",
            headers={"Content-Disposition":
                     f"attachment; filename*=UTF-8''{_quote(safe)}"})

    async def _fetch_bytes(self, url: str):
        """下载二进制内容，返回 (bytes, insecure)。"""
        return await self._github_fetch(url, "bytes", timeout=120,
                                        headers={"User-Agent": "lovomo-release-download"})



    @staticmethod
    def _check_market_source(entry: dict, manifest: dict) -> str:
        """校验"这个包是不是真来自市场条目记录的仓库"。

        返回空串表示放行，否则返回给用户看的拒绝原因。

        规则：
        - 包内清单**一个来源都没声明**（github / repo / source_repo /
          homepage / author 全空）→ 放行。全新插件本来就无从比对，
          不能因为作者没写就把人挡在门外。
        - 声明了来源，且能对上市场仓库（仓库名一致，或归属用户名一致）→ 放行。
        - 声明了来源却对不上 → 拒绝。这正是"把别人的插件换个壳挂到自己
          仓库上"的特征。
        """
        from modules.plugins import manifest_logins, repo_owner, repo_slug
        want = repo_slug((entry or {}).get("source_repo"))
        if not want:
            return ""
        owners = {v.lower() for v in manifest_logins(manifest)}
        if not owners:
            return ""
        declared = repo_slug(manifest.get("repo") or manifest.get("source_repo"))
        if declared and declared.lower() == want.lower():
            return ""
        if repo_owner(want).lower() in owners:
            return ""
        return (f"插件声明的来源（{'、'.join(sorted(owners))}）与市场仓库 {want} "
                "不一致，已拒绝安装。插件只能从作者本人的仓库安装，"
                "如果你就是作者，请在清单里写上自己的 github / repo。")

    async def handle_plugins_install_remote(self, request):
        """从市场索引里下载并安装（同样过一遍安全审查）。"""
        try:
            payload = await request.json()
            pid = str(payload.get("id") or "").strip()
            force = bool(payload.get("force", False))
            entry = None
            for kind in ("official", "thirdparty"):
                market = await self._plugins_market_payload(market=kind)
                entry = next((p for p in market.get("plugins", []) if p["id"] == pid), None)
                if entry:
                    break
            if not entry:
                return web.json_response({"success": False, "error": "市场里没有这个插件"},
                                         status=404)
            url = entry.get("download") or ""
            if not url.startswith(("http://", "https://")):
                return web.json_response(
                    {"success": False, "error": "该插件没有提供有效的下载地址"}, status=400)
            try:
                content, _ = await self._github_fetch(url, "bytes", timeout=60)
            except Exception as e:
                return web.json_response(
                    {"success": False, "error": f"下载失败：{type(e).__name__}"}, status=400)
            report = self.plugin_manager.inspect_zip(content)
            if not report.get("ok"):
                return web.json_response(
                    {"success": False, "error": report.get("error") or "包不可用",
                     "report": report}, status=400)
            manifest = report.get("manifest") or {}
            got_id = str(manifest.get("id") or "").strip()
            if got_id and got_id != pid:
                return web.json_response(
                    {"success": False,
                     "error": f"包内插件 id（{got_id}）与市场条目（{pid}）不一致，已拒绝安装",
                     "report": report}, status=400)
            why = self._check_market_source(entry, manifest)
            if why:
                print(f"[插件市场] 拒绝安装 {pid}：{why}")
                return web.json_response({"success": False, "error": why,
                                          "report": report}, status=400)
            result = self.plugin_manager.install_zip(content, force=force)
            if result.get("success"):
                # 记一笔来源，便于事后追溯插件是从哪条仓库/分支装来的
                self.plugin_manager.set_source(result["id"],
                                               entry.get("source_repo") or "",
                                               entry.get("branch") or "",
                                               entry.get("source") or "")
                print(f"[插件市场] 已安装：{result.get('name')} (id={result.get('id')})")
            return web.json_response(result,
                                     status=200 if result.get("success") else 400)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_plugins_publish_token(self, request):
        """保存 GitHub PAT。配置落地走标准加密落盘流程。"""
        try:
            payload = await request.json()
            token = str(payload.get("token") or "").strip()
        except Exception:
            return web.json_response({"success": False, "error": "请求体不是合法 JSON"}, status=400)
        if not token:
            return web.json_response({"success": False, "error": "Token 不能为空"}, status=400)
        try:
            info = await _verify_github_token(token)
        except _PublishError as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)
        self.config.config["plugin_publish_token"] = token
        self.config.config["plugin_publish_login"] = info.get("login", "")
        self.config.config["plugin_publish_name"] = info.get("name", "")
        self.config._atomic_save(self.config.config)
        return web.json_response({"success": True, "login": info.get("login", ""),
                                  "name": info.get("name", "")})

    async def handle_plugins_publish_token_clear(self, request):
        self.config.config.pop("plugin_publish_token", None)
        self.config.config.pop("plugin_publish_login", None)
        self.config.config.pop("plugin_publish_name", None)
        self.config._atomic_save(self.config.config)
        return web.json_response({"success": True})

    def _publish_state_get(self) -> dict:
        state = getattr(self, "_publish_state", None)
        if not isinstance(state, dict):
            state = {"published": {}, "unpublished": {}}
            self._publish_state = state
        for key in ("published", "unpublished"):
            if not isinstance(state.get(key), dict):
                state[key] = {}
        return state

    def _load_publish_state(self):
        """读本机的推送记录。读不出来就沿用空记录，并且本次不再回写，
        免得一次读取失败就把磁盘上完好的记录覆盖掉。"""
        from modules.jsonio import load_json_ex
        data, readable = load_json_ex(self._publish_state_file,
                                      {"published": {}, "unpublished": {}})
        self._publish_state_readable = readable
        state = {"published": {}, "unpublished": {}}
        if isinstance(data, dict):
            for key in state:
                got = data.get(key)
                if isinstance(got, dict):
                    state[key] = got
        self._publish_state = state

    def _save_publish_state(self):
        path = getattr(self, "_publish_state_file", None)
        if path is None or not getattr(self, "_publish_state_readable", True):
            return
        from modules.jsonio import save_json
        now = time.time()
        fresh = {"published": {}, "unpublished": {}}
        for key in fresh:
            for pid, rec in self._publish_state_get()[key].items():
                if not isinstance(rec, dict) or not pid:
                    continue
                if now - float(rec.get("ts") or 0) > PUBLISH_STATE_TTL:
                    continue
                fresh[key][pid] = rec
        self._publish_state = fresh
        save_json(path, fresh)

    def _publish_memory(self) -> dict:
        """本机记下的「已推送」版本 {插件 id: 版本}，过期的不算。"""
        now = time.time()
        out = {}
        for pid, rec in self._publish_state_get()["published"].items():
            if not isinstance(rec, dict) or now - float(rec.get("ts") or 0) > PUBLISH_STATE_TTL:
                continue
            version = str(rec.get("version") or "")
            if pid and version:
                out[str(pid)] = version
        return out

    def _unpublish_memory(self) -> set:
        """本机记下的「已下架」插件 id，过期的不算。"""
        now = time.time()
        out = set()
        for pid, rec in self._publish_state_get()["unpublished"].items():
            if not isinstance(rec, dict) or now - float(rec.get("ts") or 0) > PUBLISH_STATE_TTL:
                continue
            if pid:
                out.add(str(pid))
        return out

    def _remember_published(self, pid: str, version: str):
        state = self._publish_state_get()
        state["published"][pid] = {"version": str(version or ""), "ts": time.time()}
        state["unpublished"].pop(pid, None)
        self._save_publish_state()

    def _remember_unpublished(self, pid: str):
        state = self._publish_state_get()
        state["unpublished"][pid] = {"ts": time.time()}
        state["published"].pop(pid, None)
        self._save_publish_state()

    async def _published_versions(self, payload: dict = None) -> dict:
        """已上架插件的版本号 {插件 id: 版本}。

        官方市场的索引由「发布」写入，是权威来源；来源清单里的仓库仍靠
        分支与版本标签判定（分支被删掉时标签仍在，两边取版本较大的那个）。
        索引与镜像都有缓存，所以这里不吃条目缓存（payload 由调用方传入，
        免得一次请求里重复拉两轮）；本机刚推送/刚下架的记录最后叠加，
        撑住缓存还没刷新的那段时间。
        """
        from modules.plugin_publisher import parse_version_tag
        from modules.updater import is_newer
        out = {}
        try:
            if payload is None:
                payload = await self._plugins_market_payload(force=True)
            for p in (payload.get("plugins") or []):
                pid = str(p.get("id") or "")
                if pid:
                    out[pid] = str(p.get("version") or "")
        except Exception as e:
            print(f"[插件发布] 读取已上架版本失败：{type(e).__name__}: {e}")
        for pid, version in self._publish_memory().items():
            if pid not in out or is_newer(version, out[pid]):
                out[pid] = version
        repo = str(self.config.get("plugin_market_repo", "") or "").strip() \
            or "slpk1ng/Lovomo"
        prefix = str(self.config.get("plugin_market_branch_prefix", "") or "").strip() \
            or "lovomo_plugin"
        sources_path = str(self.config.get("plugin_market_sources_path", "") or "").strip()
        repos = [repo]
        if sources_path:
            repos += [r for r in await self._market_sources(repo, sources_path)
                      if r not in repos]
        for one in repos:
            try:
                for tag in await self._market_tags(one, prefix):
                    pid, version = parse_version_tag(tag, prefix)
                    if pid and (pid not in out or is_newer(version, out[pid])):
                        out[pid] = version
            except Exception as e:
                print(f"[插件发布] 读取版本标签失败（{one}）：{type(e).__name__}: {e}")
        for pid in self._unpublish_memory():
            out.pop(pid, None)
        return out

    @staticmethod
    def _market_entry_owned_by(entry, login: str) -> bool:
        """市场索引条目是否属于这个 GitHub 账号（下架列表用）。

        索引条目没有「已装插件」那套 owners 字段，只能看它自己声明的
        github / author / source_repo 仓库主。
        """
        who = str(login or "").strip().lower()
        if not who:
            return False
        cands = [str((entry or {}).get("github") or ""),
                 str((entry or {}).get("author") or "")]
        repo = str((entry or {}).get("source_repo") or "")
        if "/" in repo:
            cands.append(repo.split("/", 1)[0])
        return any(c.strip().lower() == who for c in cands if c.strip())

    async def _published_entries(self, login: str, market_path: str,
                                 payload: dict = None) -> list:
        """已上架的、属于当前账号的插件条目（下架列表的数据来源）。

        市场索引有缓存，所以这里不吃条目缓存；本机刚下架的从列表里剔掉，
        本机刚推送、索引还没更新的补进来（否则刚发布完没有「下架」按钮）。
        """
        from modules.market import plugin_category, plugin_folder
        try:
            if payload is None:
                payload = await self._plugins_market_payload(force=True)
        except Exception as e:
            print(f"[插件下架] 读取已上架列表失败：{type(e).__name__}: {e}")
            payload = {}
        dropped = self._unpublish_memory()
        out = []
        for entry in (payload.get("plugins") or []):
            if not isinstance(entry, dict) or not self._market_entry_owned_by(entry, login):
                continue
            pid = str(entry.get("id") or "").strip()
            if not pid or pid in dropped:
                continue
            category = plugin_category(entry)
            out.append({
                "id": pid,
                "name": str(entry.get("name") or pid),
                "version": str(entry.get("version") or ""),
                "category": category,
                "folder": str(entry.get("folder") or "").strip().strip("/")
                          or plugin_folder(market_path, category, pid),
            })
        seen = {e["id"] for e in out}
        manager = getattr(self, "plugin_manager", None)
        for pid, version in self._publish_memory().items():
            if pid in seen or pid in dropped:
                continue
            info = manager.get(pid) if manager else None
            category = plugin_category(info or {})
            out.append({
                "id": pid,
                "name": str((info or {}).get("name") or pid),
                "version": version,
                "category": category,
                "folder": plugin_folder(market_path, category, pid),
            })
        out.sort(key=lambda e: e["id"])
        return out

    def _publish_scope(self, login: str, published: dict = None):
        """按 GitHub 登录身份把已装插件分成"能发布"和"被挡下"两拨。

        归属校验必须在服务端做：前端隐藏只是顺手，真正的门禁在这里 ——
        直接 POST /api/plugins/publish 也绕不过去。登录名为空（还没认证）
        时全部归入"被挡下"，也就是"未认证就不参与插件制作"。

        published 是已上架插件的版本号。本地版本不高于已上架版本（重复发布、
        本地是 beta、或线上已经更高）时标成 needs_publish=False，前端据此把
        「发布」换成「当前版本已发布过」。拿不到已上架版本时不挡人。
        """
        from modules.market import plugin_category
        from modules.plugins import ownership_matches
        from modules.updater import is_newer
        published = published or {}
        owned, blocked = [], []
        manager = getattr(self, "plugin_manager", None)
        plugins = manager.list_plugins() if manager else []
        for info in plugins:
            item = {"id": info["id"], "name": info.get("name") or info["id"],
                    "version": info.get("version") or "",
                    "category": plugin_category(info),
                    "owners": list(info.get("owners") or [])}
            if login and ownership_matches(info, login):
                was = str(published.get(item["id"]) or "")
                item["published_version"] = was
                item["needs_publish"] = bool(
                    not was or not item["version"]
                    or is_newer(item["version"], was))
                owned.append(item)
            else:
                blocked.append(item)
        return owned, blocked

    async def handle_plugins_publish_status(self, request):
        token = str(self.config.config.get("plugin_publish_token") or "").strip()
        repo = str(self.config.config.get("plugin_market_repo") or "").strip()
        login = str(self.config.config.get("plugin_publish_login") or "").strip()
        market_path = str(self.config.config.get("plugin_market_path") or "").strip() \
            or "plugins/index.json"
        from modules.market import plugin_folder
        # 发布面板是决策面：不吃 30 分钟的市场条目缓存（别人在 GitHub 上改过
        # 市场就得立刻看到），一次请求只强制拉一轮，两个列表共用
        payload = {}
        if login:
            try:
                payload = await self._plugins_market_payload(force=True)
            except Exception as e:
                print(f"[插件发布] 读取市场失败：{type(e).__name__}: {e}")
        published = await self._published_versions(payload) if login else {}
        owned, blocked = self._publish_scope(login, published)
        for p in owned:
            p["folder"] = plugin_folder(market_path, p.get("category"), p["id"])
        on_market = await self._published_entries(login, market_path, payload) if login else []
        info = {
            # 只有「Token 在 + 身份校验过」才算认证完成：缺一个都退回
            # 让用户重新填 Token，免得出现"显示已登录却什么都发不了"
            "configured": bool(token and login),
            "login": login,
            "name": str(self.config.config.get("plugin_publish_name") or ""),
            "repo": repo,
            "market_path": market_path,
            "publishable": [p["id"] for p in owned],
            "plugins": owned,
            "blocked": blocked,
            "published": on_market,
        }
        return web.json_response(info)

    async def handle_plugins_publish(self, request):
        """把 plugins/sources/<id>/ 写进市场仓库的分类目录并更新索引。"""
        try:
            payload = await request.json()
            pid = str(payload.get("id") or "").strip()
            message = str(payload.get("message") or "").strip() or None
        except Exception:
            return web.json_response({"success": False, "error": "请求体不是合法 JSON"}, status=400)
        if not pid:
            return web.json_response({"success": False, "error": "缺少插件 id"}, status=400)
        token = str(self.config.config.get("plugin_publish_token") or "").strip()
        repo = str(self.config.config.get("plugin_market_repo") or "").strip()
        market_path = str(self.config.config.get("plugin_market_path") or "").strip()
        login = str(self.config.config.get("plugin_publish_login") or "").strip()
        if not token or not login:
            return web.json_response({"success": False,
                                      "error": "尚未认证 GitHub 身份，请先在「插件」→「发布到市场」里填写 Personal Access Token"},
                                     status=400)
        if not repo:
            return web.json_response({"success": False,
                                      "error": "尚未配置插件市场仓库"}, status=400)
        from modules.plugins import ownership_matches
        from modules.updater import is_newer
        manager = getattr(self, "plugin_manager", None)
        info = manager.get(pid) if manager else None
        if info is None:
            return web.json_response({"success": False, "error": f"插件 {pid} 不存在"},
                                     status=404)
        if not ownership_matches(info, login):
            owners = "、".join(info.get("owners") or []) or "未声明"
            reason = (f"插件「{info.get('name') or pid}」声明的归属是 {owners}，"
                      f"与当前登录的 GitHub 账号 {login} 不符，已拒绝发布。"
                      "要发布请先在插件清单（plugin.yaml）里写上自己的 "
                      "github / repo / homepage。")
            print(f"[插件发布] 拒绝发布 {pid}：{reason}")
            return web.json_response({"success": False, "error": reason}, status=403)
        local_version = str(info.get("version") or "").strip()
        was = str((await self._published_versions()).get(pid) or "")
        if was and not is_newer(local_version, was):
            reason = (f"本地版本 {local_version or '(空)'} 不高于已上架的 {was}，"
                      "版本只能往上走；请先改 plugin.yaml 里的 version 再发布。")
            print(f"[插件发布] 拒绝发布 {pid}：{reason}")
            return web.json_response({"success": False, "error": reason}, status=400)
        sources_dir = (self.plugin_manager.root if self.plugin_manager
                       else Path(__file__).resolve().parent / "plugins" / "sources")
        try:
            result = await _publish_to_github(
                token=token, repo=repo, plugin_id=pid,
                sources_dir=sources_dir, message=message,
                market_path=market_path)
        except _PublishError as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)
        print(f"[插件发布] 发布成功：{pid} → {result.get('folder')}"
              f"（提交 {str(result.get('commit') or '')[:7]}，"
              f"{len(result.get('files') or [])} 个文件）")
        if local_version:
            self._remember_published(pid, local_version)
        # 刚推上去的版本还没进市场缓存，清掉才能让发布状态立刻看到新版本
        self._plugin_market_cache.clear()
        return web.json_response({"success": True, "version": local_version, **result})

    async def handle_plugins_unpublish(self, request):
        """把插件从市场下架：删索引条目、插件目录与该插件的版本标签、Release。"""
        try:
            payload = await request.json()
            pid = str(payload.get("id") or "").strip()
        except Exception:
            return web.json_response({"success": False, "error": "请求体不是合法 JSON"}, status=400)
        if not pid:
            return web.json_response({"success": False, "error": "缺少插件 id"}, status=400)
        token = str(self.config.config.get("plugin_publish_token") or "").strip()
        repo = str(self.config.config.get("plugin_market_repo") or "").strip()
        market_path = str(self.config.config.get("plugin_market_path") or "").strip()
        if not token:
            return web.json_response({"success": False,
                                      "error": "尚未认证 GitHub 身份，请先在「插件」→「发布到市场」里填写 Personal Access Token"},
                                     status=400)
        if not repo:
            return web.json_response({"success": False,
                                      "error": "尚未配置插件市场仓库"}, status=400)
        try:
            result = await _unpublish_from_github(
                token=token, repo=repo, plugin_id=pid, market_path=market_path)
        except _PublishError as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)
        print(f"[插件下架] 已下架：{pid}（目录 {result.get('folder')}，"
              f"删除 {len(result.get('removed') or [])} 个文件、"
              f"{len(result.get('tags') or [])} 个版本标签）")
        self._remember_unpublished(pid)
        self._plugin_market_cache.clear()
        return web.json_response({"success": True, **result})

    def _after_config_reload(self):
        """配置变更后的统一热重载。"""
        global global_config, global_emotion_manager, memory_manager
        global_config = self.config
        apply_log_max_size(self.config)
        self._refresh_auth_state()
        self.config.roles = self.config._parse_roles()
        if not global_config.active_character or global_config.active_character not in self.config.roles:
            if self.config.roles:
                global_config.active_character = list(self.config.roles.keys())[0]
        global_emotion_manager = EmotionManager(self.config)
        memory_manager = MemoryManager(self.config)
        if sender is not None:
            sender.memory_manager = memory_manager
        # WebUI 侧持有的也是构造时的快照：不同步的话，改了记忆目录/角色之后
        # 页面上读写的仍是旧目录，只能重启才能自愈
        self.memory_manager = memory_manager
        self._update_state_file = memory_manager.data_path / "update_check.json"
        self._auth_file = memory_manager.data_path / "webui_auth.json"
        hot_reload_managers()

    # ---------------- 文件夹选择 / 模型列表 ----------------
    async def _pick_dialog(self, dialog_kind, file_types=None):
        """弹出系统选择对话框，返回 (ok, path_or_error)。

        对话框是阻塞调用，丢进线程池避免卡住 WebUI 事件循环。
        """
        try:
            import webview
        except Exception:
            return False, "pywebview 未安装，无法打开文件选择"
        window = _WEBVIEW_WINDOW_HOLDER.get("window")
        if window is None:
            return False, "该功能仅在 Lovomo 桌面窗口模式下可用（浏览器访问不支持）"
        kind = getattr(webview, dialog_kind)
        kwargs = {}
        if file_types:
            kwargs["file_types"] = tuple(file_types)
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, lambda: window.create_file_dialog(kind, **kwargs))
        except Exception as e:
            return False, f"打开选择对话框失败: {e}"
        return True, (str(result[0]) if result else "")

    async def handle_pick_folder(self, request):
        """弹出系统"选择文件夹"对话框；仅在 Lovomo 桌面窗口模式下可用。"""
        ok, path = await self._pick_dialog("FOLDER_DIALOG")
        if not ok:
            return web.json_response({"ok": False, "error": path}, status=400)
        return web.json_response({"ok": bool(path), "path": path})

    async def handle_pick_file(self, request):
        """弹出系统"选择文件"对话框，可选把选中的文件复制到某个插件的数据目录。

        payload:
            filter    "图片 (*.png;*.jpg)" 这类过滤器描述，可选
            exts      允许的扩展名列表（不含点），可选
            plugin_id 给了就把文件复制进该插件 data 目录，返回相对文件名
        """
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        exts = [str(e).lstrip(".").lower() for e in (payload.get("exts") or []) if str(e).strip()]
        desc = str(payload.get("filter") or "文件").strip()
        file_types = (f"{desc} ({';'.join('*.' + e for e in exts)})",)
        if exts:
            file_types += ("所有文件 (*.*)",)
        ok, path = await self._pick_dialog("OPEN_DIALOG", file_types)
        if not ok:
            return web.json_response({"ok": False, "error": path}, status=400)
        if not path:
            return web.json_response({"ok": False, "path": "", "cancelled": True})
        src = Path(path)
        if exts and src.suffix.lower().lstrip(".") not in exts:
            return web.json_response(
                {"ok": False, "error": f"只支持这些格式：{', '.join(exts)}"}, status=400)
        pid = str(payload.get("plugin_id") or "").strip()
        if not pid:
            return web.json_response({"ok": True, "path": path, "name": src.name,
                                      "cancelled": False})
        data_dir = self.plugin_manager.data_dir(pid)
        if data_dir is None:
            return web.json_response({"ok": False, "error": "插件不存在"}, status=404)
        if src.suffix.lower() not in ALLOWED_ASSET_EXTS:
            return web.json_response(
                {"ok": False, "error": f"不支持的文件类型：{src.suffix}"}, status=400)
        try:
            if src.stat().st_size > MAX_PLUGIN_ASSET_BYTES:
                limit_mb = MAX_PLUGIN_ASSET_BYTES // (1024 * 1024)
                return web.json_response(
                    {"ok": False, "error": f"文件超过 {limit_mb}MB 上限"}, status=400)
            # 保留原文件名（只清非法字符），重名自动编号
            safe = unique_asset_name(data_dir, safe_asset_name(src.name, src.suffix))
            dst = data_dir / safe
            shutil.copyfile(src, dst)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"复制文件失败: {e}"}, status=500)
        return web.json_response({"ok": True, "path": str(dst), "name": safe,
                                  "orig_name": src.name, "cancelled": False})

    async def handle_scan_models(self, request):
        """扫描文件夹里的 .gguf 模型，返回可填入 llm_model_name 的候选标识符。

        候选键依次取 GGUF 元数据 general.name（LM Studio 模型 ID 的主要来源）、
        发布者/模型文件夹名、文件名去量化后缀——按此顺序去重。
        """
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "参数错误"}, status=400)
        folder = str(payload.get("folder", "") or "").strip()
        root = Path(folder)
        if not folder or not root.is_dir():
            return web.json_response({"ok": False, "error": "文件夹不存在"}, status=400)
        models = []
        try:
            for gguf in sorted(root.rglob("*.gguf")):
                if "mmproj" in gguf.name.lower():
                    continue  # 视觉投影适配器（mmproj-* 或 *.mmproj-*），不是主模型
                parts = [p for p in gguf.parent.relative_to(root).parts if p]
                candidates = []
                meta_name = _gguf_general_name(gguf)
                if meta_name:
                    # "Huihui Qwen3.5 9B Abliterated" → huihui-qwen3.5-9b-abliterated
                    candidates.append(re.sub(r"\s+", "-", meta_name).lower())
                    # "Qwen_Qwen3.5 9B" 的下划线是发布者分隔符 → qwen/qwen3.5-9b
                    candidates.append(re.sub(r"\s+", "-", meta_name.replace("_", "/")).lower())
                if len(parts) >= 2:
                    candidates.append(f"{parts[0]}/{parts[1]}".lower())
                if parts:
                    base = re.sub(r"[-_.]?gguf$", "", parts[-1], flags=re.IGNORECASE)
                    candidates.append(base.lower())
                keys, seen = [], set()
                for c in candidates:
                    if c and c not in seen:
                        seen.add(c)
                        keys.append(c)
                models.append({"file": gguf.name, "keys": keys})
            return web.json_response({"ok": True, "models": models})
        except Exception as e:
            return web.json_response({"ok": False, "error": f"扫描失败: {e}"}, status=500)

    async def handle_list_remote_models(self, request):
        """从 LLM 服务拉取可用模型 ID 列表（Ollama 与 OpenAI 兼容服务都支持）。"""
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "参数错误"}, status=400)
        base = str(payload.get("base_url", "") or "").strip().rstrip("/")
        backend = str(payload.get("backend", "ollama") or "ollama")
        if not base:
            return web.json_response({"ok": False, "error": "服务地址为空"}, status=400)
        headers = {}
        api_key = str(self.config.get("llm_api_key", "") or "")
        if backend != "ollama" and api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        tried = []
        for url in model_list_endpoints(base, backend):
            try:
                async with httpx.AsyncClient(timeout=10, trust_env=False, headers=headers,
                                             verify=verified_context()) as client:
                    resp = await client.get(url)
            except Exception as e:
                tried.append(f"{url} → {type(e).__name__}: {e}")
                continue
            # 服务端会在响应体里写明原因（url error / 模型不存在等）；
            # 只报 raise_for_status 的异常，用户就只看到一个自己从没填过的地址
            if resp.status_code >= 400:
                tried.append(f"{url} → HTTP {resp.status_code} {_brief_response(resp)}")
                continue
            try:
                data = resp.json()
            except Exception:
                tried.append(f"{url} → 响应不是 JSON {_brief_response(resp)}")
                continue
            if backend == "ollama":
                ids = [str(m.get("name") or m.get("model") or "") for m in data.get("models", [])]
            else:
                ids = [str(m.get("id") or "") for m in data.get("data", [])]
            ids = [i for i in ids if i]
            if not ids:
                tried.append(f"{url} → 响应里没有模型列表 {_brief_response(resp)}")
                continue
            return web.json_response({"ok": True, "models": ids})
        error = "获取模型列表失败：\n" + "\n".join(tried)
        if looks_like_full_endpoint(base):
            error += ("\n\n当前地址看起来是某个具体接口的完整路径。这里要填「服务根地址」，"
                      "也就是补上 /v1/models（或 /api/tags）之前的那一段，"
                      "例如 http://127.0.0.1:8080/v1。")
        return web.json_response({"ok": False, "error": error, "tried": tried}, status=502)

    async def handle_save_config(self, request):
        try:
            new_config = await request.json()
            restart_tts = new_config.pop("restart_tts", False)
            for key in _API_KEY_KEYS + _WEBUI_PASSWORD_KEYS:
                masked_value = new_config.get(key)
                if isinstance(masked_value, dict):
                    stored = self.config.config.get(key)
                    if isinstance(stored, dict) and any(
                            _is_masked_value(v) for v in masked_value.values()):
                        new_config[key] = {k: (stored.get(k, "") if _is_masked_value(v) else v)
                                           for k, v in masked_value.items()}
                elif _is_masked_value(masked_value):
                    # 前端提交的是头尾掩码预览（******** 或 abcd****wxyz（N位））：回填真实密钥
                    new_config[key] = self.config.config.get(key, "")
            # WebUI 只提交它渲染出的字段：以【当前生效配置】为底合并，表单字段覆盖。
            # 之前与 default_config() 合并——表单里没有的键（如搜索引擎设置
            # web_search_engine / web_search_url / web_search_custom_engines）会被
            # 默认值顶掉，用户保存过的 SearXNG 接口每次保存配置页都被抹回 bing。
            base_config = self.config.config or {}
            new_config = {**base_config, **new_config}
            self.config.config = new_config
            self.config._atomic_save(new_config)
            try:
                self._after_config_reload()
            except Exception as reload_err:
                return web.json_response(
                    {"success": True,
                     "message": f"配置已保存，但热重载失败（{reload_err}），请重启程序使运行状态与配置一致。"})

            old_config = self._last_saved_config
            tts_changed = False
            if old_config is not None:
                tts_keys = ['client_base_url', 'model_dir', 'ref_audio_root', 'device',
                            'auto_start_tts', 'tts_start_script', 'timeout_seconds',
                            'prompt_text', 'prompt_lang', 'text_lang', 'top_k', 'top_p',
                            'temperature', 'text_split_method', 'batch_size',
                            'batch_threshold', 'split_bucket', 'speed_factor',
                            'fragment_interval', 'streaming_mode', 'seed',
                            'parallel_infer', 'repetition_penalty', 'media_type',
                            'llm_emotion_intensity', 'intensity_to_temperature', 'intensity_to_top_k']
                for key in tts_keys:
                    if old_config.get(key) != new_config.get(key):
                        tts_changed = True
                        break

            force_restart = restart_tts and global_config.get("auto_start_tts", False)
            tts_restart_message = ""
            if (tts_changed or force_restart) and global_config.get("auto_start_tts", False):
                valid, error_msg = self.validate_tts_config(global_config)
                if not valid:
                    print(f"TTS 配置验证失败，跳过重启：{error_msg}")
                    tts_restart_message = f"配置已保存，但 TTS 服务未重启：{error_msg}"
                else:
                    print("检测到 TTS 相关配置变化或用户强制重启，正在重启 TTS 服务...")
                    process_manager.shutdown_all()
                    threading.Thread(target=auto_start_and_switch_tts, args=(global_config,), daemon=True).start()
                    tts_restart_message = "TTS 服务正在重启，请稍候..."
            elif tts_changed:
                tts_restart_message = "配置已保存，但 auto_start_tts 为 False，不会自动重启 TTS。"
            else:
                tts_restart_message = "TTS 配置未变化或未选择强制重启，无需重启 TTS 服务。"

            napcat_changed = False
            if old_config is not None:
                if (old_config.get('napcat_ws_url') != new_config.get('napcat_ws_url') or
                        old_config.get('napcat_token') != new_config.get('napcat_token') or
                        role_connection_snapshot(old_config) != role_connection_snapshot(new_config)):
                    napcat_changed = True
                # 换模型：登记旧模型，等新模型首次调用成功后由 llm_helpers 自动卸载
                if self.config.get("llm_auto_unload_old", True):
                    from modules.llm_helpers import queue_old_model_unload
                    for key in ("llm_model_name", "image_caption_model_name"):
                        old_m = str(old_config.get(key, "") or "").strip()
                        new_m = str(new_config.get(key, "") or "").strip()
                        if old_m and old_m != new_m:
                            queue_old_model_unload(old_m)
                            print(f"[模型切换] {key}：{old_m} → {new_m}，"
                                  "新模型调用成功后将自动卸载旧模型")

            self._last_saved_config = new_config.copy()
            response = {"success": True}
            if napcat_changed:
                response['message'] = "配置已保存，但 NapCat 连接参数修改需重启程序才能生效。" + tts_restart_message
            else:
                response['message'] = "配置已保存并热重载生效。" + tts_restart_message
            return web.json_response(response)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return web.json_response({"success": False, "error": str(e)}, status=400)

    def validate_tts_config(self, config: ConfigLoader) -> tuple:
        ref_audio_root = config.get("ref_audio_root", "")
        if not ref_audio_root or not Path(ref_audio_root).exists():
            return False, "参考音频根目录无效或不存在，请检查路径后重试"
        model_dir = config.get("model_dir", "")
        if not model_dir or not Path(model_dir).exists():
            return False, "模型文件夹路径无效或不存在，请检查路径后重试"
        return True, ""

    # ---------------- 角色 ----------------
    async def handle_get_roles(self, request):
        roles = []
        for key, role in self.config.roles.items():
            roles.append({
                "character_key": key,
                "character_name": role["character_name"],
                "active": key == self.config.active_character
            })
        return web.json_response({"roles": roles})

    async def handle_save_roles(self, request):
        try:
            new_data = await request.json()
            roles = new_data.get("roles", [])
            active = new_data.get("active_character", "")
            self.config.config["roles"] = roles
            self.config.config["active_character"] = active
            self.config._atomic_save(self.config.config)
            self._after_config_reload()
            return web.json_response({"success": True, "message": "角色配置已保存！"})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_get_logs(self, request):
        """WebUI 日志接口。

        精简模式（默认）先按 webui_log_hide_patterns 剔除噪音行，再回尾部
        webui_log_tail_lines 行；?full=1（前端"显示完整日志"开关）返回内存里
        缓存的全部日志（上限 webui_log_buffer_lines 行），一行都不藏。

        完整模式另有一道 40 万字符的极端上限（约等于上万行），只为了避免
        极端情况下把整个响应撑爆；触发时会返回 truncated=true 并在日志里写明，
        这样"搜索结果只显示前几条"不再可能是 WebUI 截断造成的。
        """
        full = request.query.get("full", "") in ("1", "true", "yes")
        _, tail_lines = _runtime_log_limits()
        with log_lock:
            total = len(global_log_buffer)
            if full:
                logs = "\n".join(global_log_buffer)
            else:
                patterns = _log_hide_patterns()
                # 被隐藏的那一行原本占着一行换行，直接丢掉会留下一个空行，
                # 看起来就是「隐藏内容处莫名其妙空了一片」，所以连同它后面
                # 紧跟的空白行一起去掉。
                kept = []
                blank_after_hidden = False
                for line in global_log_buffer:
                    if _log_hidden(line, patterns):
                        blank_after_hidden = True
                        continue
                    if blank_after_hidden and not line.strip():
                        blank_after_hidden = False
                        continue
                    blank_after_hidden = False
                    kept.append(line)
                logs = "\n".join(kept[-tail_lines:])
        truncated = False
        if full and len(logs) > _LOG_FULL_MAX_CHARS:
            logs = logs[-_LOG_FULL_MAX_CHARS:]
            truncated = True
            print(f"完整日志超过 {_LOG_FULL_MAX_CHARS} 字符上限，已返回尾部内容"
                  f"（可调小 webui_log_buffer_lines 控制日志体积）。")
        return web.json_response({"logs": logs, "total_lines": total,
                                  "tail_lines": tail_lines, "full": full,
                                  "truncated": truncated})

    # ---------------- 聊天记录 ----------------
    async def handle_list(self, request):
        memories = self.memory_manager.list_memories()
        return web.json_response({"memories": memories})

    async def handle_history(self, request):
        try:
            payload = await request.json()
            filename = payload.get("filename", "")
            result = self.memory_manager.get_history(filename)
            return web.json_response(result)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_delete(self, request):
        try:
            payload = await request.json()
            filenames = payload.get("files", [])
            deleted = []
            for filename in filenames:
                if self.memory_manager.delete_memory_file(filename):
                    deleted.append(filename)
            if deleted:
                for sid in self._session_ids_from_files(deleted):
                    if mood_mgr is not None:
                        mood_mgr.delete_session(sid)
                    forget_proactive_session(sid)
            return web.json_response({"success": True, "deleted": deleted})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    def _session_ids_from_files(self, filenames: list) -> list:
        out = []
        for name in filenames:
            m = re.match(r'^[A-Za-z0-9_\-]+_(private|group)_(.+)\.json$', str(name))
            if not m:
                continue
            stype, rest = m.group(1), m.group(2)
            if stype == "private":
                out.append(f"private_{rest}")
            else:
                out.append(f"group_{rest.split('_')[0]}")
                out.append(f"group_{rest}")
        return out

    async def handle_delete_messages(self, request):
        try:
            payload = await request.json()
            filename = payload.get("filename", "")
            indices = payload.get("indices", [])
            result = self.memory_manager.delete_messages(filename, indices)
            return web.json_response(result)
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_memory_export(self, request):
        filename = request.query.get("filename", "")
        result = self.memory_manager.get_history(filename)
        if not result.get("success"):
            return web.json_response(result, status=404)
        payload = {"filename": filename,
                   "character_name": result.get("character_name"),
                   "history": result.get("history", [])}
        content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        saved = await self._save_export_via_dialog(filename, content)
        if saved == "browser":
            return _json_file_response(payload, filename)
        if saved == "cancelled":
            return self._export_status_response("cancelled")
        return self._export_status_response("saved", saved)

    async def handle_memory_import(self, request):
        try:
            reader = await request.multipart()
            imported = []
            skipped = []
            async for part in reader:
                if not part.filename or not part.filename.endswith(".json"):
                    continue
                raw = await part.read(decode=False)
                data = json.loads(raw.decode("utf-8"))
                if not isinstance(data, dict) or "history" not in data:
                    continue
                name = Path(part.filename).name
                # 只允许写入会话记忆文件：data 目录里还躺着 webui_auth.json 等功能数据，
                # 同一目录下的整份覆盖等于把认证态等数据交给上传者改写
                if not _is_memory_filename(name):
                    skipped.append(name)
                    continue
                from modules.jsonio import save_json
                save_json(self.memory_manager.data_path / name, data)
                imported.append(name)
            return web.json_response({"success": True, "imported": imported,
                                      "skipped": skipped})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_memory_export_all(self, request):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            # 只打包会话记忆：同一目录下的 webui_auth.json 含 WebUI 令牌与口令散列，
            # 打包进去等于把登录凭据随导出文件一起送出去
            for f in sorted(self.memory_manager.data_path.glob("*.json")):
                if _is_memory_filename(f.name):
                    zf.write(f, f.name)
        content = buf.getvalue()
        saved = await self._save_export_via_dialog("lovomo_memories.zip", content)
        if saved == "browser":
            return web.Response(body=content, content_type="application/zip",
                                headers={"Content-Disposition": 'attachment; filename="lovomo_memories.zip"'})
        if saved == "cancelled":
            return self._export_status_response("cancelled")
        return self._export_status_response("saved", saved)

    async def handle_sessions(self, request):
        """返回已知会话列表（供定时任务/事件/待办选择发送目标）。"""
        return web.json_response({"sessions": list_known_sessions()})

    # ---------------- 情绪音频管理 ----------------
    def _role_root(self, role_key: str, kind: str = "tone"):
        """情绪音频目录：kind=mimic 取情绪模仿根目录（未配置返回 None），其余取语气根目录。"""
        role = self.config.roles.get(role_key, {}) if role_key else {}
        ctx = RoleContext(self.config.config, role or {})
        if str(kind or "") == "mimic":
            root = str(ctx.get("emotion_mimic_root", "") or "").strip()
            return Path(root) if root else None
        return Path(resolve_tts_path(ctx.get("ref_audio_root", "")))

    async def handle_emotions_list(self, request):
        role_key = request.query.get("role", "")
        kind = "mimic" if request.query.get("kind", "") == "mimic" else "tone"
        root = self._role_root(role_key, kind)
        emotions = []
        if root is not None and root.exists():
            for folder in sorted(root.iterdir()):
                if not folder.is_dir():
                    continue
                if not _is_emotion_folder(folder):
                    continue
                files = [f.name for f in sorted(folder.iterdir()) if f.is_file()]
                asr = ""
                asr_path = folder / "asr.txt"
                if asr_path.exists():
                    asr = asr_path.read_text(encoding="utf-8", errors="ignore").strip()
                ref = next((f for f in files if f.lower().startswith("ref.")), None)
                if ref is None:
                    ref = next((f for f in files if f.lower().split(".")[-1] in
                                ("mp3", "wav", "ogg", "flac", "m4a")), None)
                audios = [f for f in files if Path(f).suffix.lower() in _AUDIO_EXTS]
                texts = {f: _read_sidecar_text(folder / f) for f in audios}
                emotions.append({"name": folder.name, "files": files, "audios": audios,
                                 "texts": texts, "asr": asr, "ref": ref})
        return web.json_response({"root": str(root) if root is not None else "",
                                  "role": role_key, "kind": kind, "emotions": emotions})

    async def handle_emotions_upload(self, request):
        try:
            reader = await request.multipart()
            role = emotion = None
            kind = ""
            file_data = None
            file_name = ""
            async for part in reader:
                if part.name == "role":
                    role = (await part.text()).strip()
                elif part.name == "kind":
                    kind = (await part.text()).strip()
                elif part.name == "emotion":
                    emotion = (await part.text()).strip()
                elif part.name == "file":
                    file_name = part.filename or ""
                    file_data = await part.read(decode=False)
            root = self._role_root(role or "", kind)
            if root is None:
                return web.json_response({"success": False, "error": "未配置情绪模仿根目录"}, status=400)
            folder = _safe_subdir(root, emotion or "")
            if folder is None:
                return web.json_response({"success": False, "error": "情绪名称非法"}, status=400)
            folder.mkdir(parents=True, exist_ok=True)
            saved = []
            if file_data:
                ext = Path(file_name).suffix.lower() or ".mp3"
                if ext not in (".mp3", ".wav", ".ogg", ".flac", ".m4a"):
                    return web.json_response({"success": False, "error": "仅支持音频文件"}, status=400)
                target = folder / f"ref{ext}"
                target.write_bytes(file_data)
                saved.append(target.name)
                await asyncio.to_thread(normalize_audio, target, log=print)
            self._after_config_reload()
            return web.json_response({"success": True, "saved": saved})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_emotions_create(self, request):
        try:
            payload = await request.json()
            root = self._role_root(payload.get("role", ""), payload.get("kind", ""))
            if root is None:
                return web.json_response({"success": False, "error": "未配置情绪模仿根目录"}, status=400)
            folder = _safe_subdir(root, payload.get("emotion", ""))
            if folder is None:
                return web.json_response({"success": False, "error": "情绪名称非法"}, status=400)
            folder.mkdir(parents=True, exist_ok=True)
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_emotions_delete(self, request):
        try:
            payload = await request.json()
            root = self._role_root(payload.get("role", ""), payload.get("kind", ""))
            if root is None:
                return web.json_response({"success": False, "error": "未配置情绪模仿根目录"}, status=404)
            folder = _safe_subdir(root, payload.get("emotion", ""))
            if folder is None or not folder.exists():
                return web.json_response({"success": False, "error": "目录不存在"}, status=404)
            shutil.rmtree(folder)
            self._after_config_reload()
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_emotions_audio(self, request):
        role = request.query.get("role", "")
        emotion = request.query.get("emotion", "")
        file = request.query.get("file", "")
        root = self._role_root(role, request.query.get("kind", ""))
        if root is None:
            return web.Response(status=404, text="not found")
        folder = _safe_subdir(root, emotion)
        if folder is None or not _safe_name(file):
            return web.Response(status=404, text="not found")
        try:
            target = (folder / file).resolve()
        except (OSError, ValueError):
            return web.Response(status=404, text="not found")
        # 再确认落在情绪目录内且确实是文件
        if folder.resolve() not in target.parents or not target.is_file():
            return web.Response(status=404, text="not found")
        return await _local_file_response(
            target, AUDIO_MIMES.get(target.suffix.lower(), "application/octet-stream"))

    def _emotion_audio_targets(self, root: Path, payload: dict):
        """解析请求里要处理的音频；返回 [(情绪名, 路径)]，出错时返回错误文案。"""
        items = payload.get("items")
        if not items:
            items = [{"emotion": payload.get("emotion", ""), "files": payload.get("files")}]
        targets = []
        for item in items:
            item = item or {}
            name = str(item.get("emotion", "") or "")
            folder = _safe_subdir(root, name)
            if folder is None or not folder.is_dir():
                return f"情绪目录不存在：{name}"
            names = item.get("files")
            if not names:
                names = [f.name for f in sorted(folder.iterdir())
                         if f.is_file() and f.suffix.lower() in _AUDIO_EXTS]
            for file_name in names:
                target, error = _emotion_audio_file(folder, file_name)
                if target is None:
                    return error
                targets.append((name, target))
        if not targets:
            return "没有可处理的音频文件"
        return targets

    async def handle_emotions_text(self, request):
        """保存参考文字：texts 是每段音频自己的同名 txt，shared 是文件夹共用的 asr.txt；留空即删掉该文件。"""
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "请求体不是 JSON"}, status=400)
        root = self._role_root(payload.get("role", ""), payload.get("kind", ""))
        if root is None:
            return web.json_response({"success": False, "error": "未配置情绪模仿根目录"}, status=400)
        folder = _safe_subdir(root, str(payload.get("emotion", "") or ""))
        if folder is None or not folder.is_dir():
            return web.json_response({"success": False, "error": "情绪目录不存在"}, status=400)
        texts = payload.get("texts") or {}
        shared = payload.get("shared")
        if not isinstance(texts, dict) or (not texts and shared is None):
            return web.json_response({"success": False, "error": "没有要保存的文字"}, status=400)
        saved = []
        for file_name, text in texts.items():
            audio, error = _emotion_audio_file(folder, file_name)
            if audio is None:
                return web.json_response({"success": False, "error": error}, status=400)
            try:
                _write_text_file(audio.with_suffix(".txt"), text)
            except OSError as e:
                return web.json_response({"success": False, "error": f"写入失败：{e}"}, status=400)
            saved.append(audio.name)
        if shared is not None:
            try:
                _write_text_file(folder / "asr.txt", shared)
            except OSError as e:
                return web.json_response({"success": False, "error": f"写入失败：{e}"}, status=400)
            saved.append("asr.txt")
        self._after_config_reload()
        return web.json_response({"success": True, "saved": saved})

    async def handle_emotions_normalize(self, request):
        """把参考音频规整到 GPT-SoVITS 要求的 3~10 秒。"""
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "请求体不是 JSON"}, status=400)
        root = self._role_root(payload.get("role", ""), payload.get("kind", ""))
        if root is None:
            return web.json_response({"success": False, "error": "未配置情绪模仿根目录"}, status=400)
        targets = self._emotion_audio_targets(root, payload)
        if isinstance(targets, str):
            return web.json_response({"success": False, "error": targets}, status=400)
        results = []
        for name, target in targets:
            item = await asyncio.to_thread(normalize_audio, target, log=print)
            item["emotion"] = name
            results.append(item)
        return web.json_response({"success": True, "results": results})

    async def handle_asr_start(self, request):
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "请求体不是 JSON"}, status=400)
        root = self._role_root(payload.get("role", ""), payload.get("kind", ""))
        if root is None:
            return web.json_response({"success": False, "error": "未配置情绪模仿根目录"}, status=400)
        targets = self._emotion_audio_targets(root, payload)
        if isinstance(targets, str):
            return web.json_response({"success": False, "error": targets}, status=400)
        job = start_asr_job(self.config.config,
                            [{"audio": str(path), "emotion": name} for name, path in targets],
                            payload.get("lang", ""), payload.get("engine", ""))
        return web.json_response({"success": True, **job.snapshot()})

    async def handle_asr_status(self, request):
        job = get_asr_job(request.query.get("job_id", ""))
        if job is None:
            return web.json_response({"success": False, "error": "任务不存在"}, status=404)
        return web.json_response({"success": True, **job.snapshot()})

    # ---------------- 统计 ----------------
    async def handle_stats(self, request):
        range_key = str(request.query.get("range", "30") or "30")
        stats = (stats_mgr.get_stats(range_key) if stats_mgr else None) or {}
        moods = []
        if mood_mgr is not None:
            by_role = mood_mgr.role_mood_records()
            roles = getattr(global_config, "roles", None) or {}
            for key, recs in by_role.items():
                role = roles.get(key) or {}
                latest = recs[0] if recs else {}
                moods.append({
                    "character_key": key,
                    "character_name": role.get("character_name") or key,
                    "mood": round(latest.get("mood", 0)) if recs else None,
                    "sessions": len(recs),
                    "updated": latest.get("updated", 0) if recs else 0,
                    "records": [{"session_id": r.get("session_id", ""),
                                 "user_id": r.get("user_id", ""),
                                 "mood": round(r.get("mood", 0)),
                                 "updated": r.get("updated", 0)} for r in recs],
                })
            moods.sort(key=lambda m: -float(m["updated"]))
        stats["moods"] = moods
        return web.json_response(stats)

    async def handle_mood_set(self, request):
        try:
            payload = await request.json()
            session_id = str(payload.get("session_id", "") or "")
            character_key = str(payload.get("character_key", "") or "")
            if not session_id or not character_key or mood_mgr is None:
                return web.json_response({"success": False, "error": "参数不完整"}, status=400)
            lo = float(self.config.get("reply_judge_mood_min", 0) or 0)
            hi = float(self.config.get("reply_judge_mood_max", 100) or 100)
            if hi < lo:
                hi = lo
            mood = max(lo, min(hi, float(payload.get("mood", 0))))
            mood_mgr.set_mood(session_id, character_key, mood,
                              user_id=str(payload.get("user_id", "") or ""))
            return web.json_response({"success": True, "mood": mood})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_performance(self, request):
        perf = stats_mgr.get_performance() if stats_mgr else {}
        perf["tts_online"] = await ensure_tts_service_enabled_check()
        perf["napcat_connected"] = bool(sender and sender.client is not None)
        perf["scheduler_jobs"] = len([j for j in scheduler.jobs.values() if j.enabled])
        return web.json_response(perf)

    # ---------------- 定时任务 ----------------
    async def handle_jobs(self, request):
        return web.json_response({"jobs": job_mgr.describe() if job_mgr else [],
                                  "scheduler_enabled": bool(self.config.get("scheduler_enabled", False))})

    async def handle_jobs_save(self, request):
        try:
            payload = await request.json()
            jobs = payload.get("jobs", [])
            cleaned = []
            for job in jobs:
                jid = str(job.get("id") or f"job_{int(time.time()*1000)}")
                cleaned.append({
                    "id": jid, "name": str(job.get("name") or jid),
                    "enabled": bool(job.get("enabled", True)),
                    "trigger": job.get("trigger") or {"type": "daily", "time": "08:00"},
                    "target": job.get("target") or {},
                    "action": job.get("action") or {"mode": "template", "template": ""},
                })
            job_mgr.jobs = cleaned
            job_mgr.save()
            job_mgr.reload()
            return web.json_response({"success": True, "jobs": job_mgr.describe()})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_jobs_batch(self, request):
        """批量修改定时任务的发送目标（会话类型 / 会话 ID）。

        ids 为空表示应用到全部任务；两个字段都留空表示"清空会话 ID"，
        也就是回到"发给所有聊过的会话"。
        """
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "请求体不是合法 JSON"}, status=400)
        ids = payload.get("ids")
        wanted = {str(i) for i in ids} if isinstance(ids, list) else None
        has_type = "session_type" in payload
        session_type = str(payload.get("session_type") or "").strip()
        if has_type and session_type not in ("private", "group"):
            return web.json_response({"success": False, "error": "会话类型只能是 private 或 group"},
                                     status=400)
        has_id = "session_id" in payload
        session_id = str(payload.get("session_id") or "").strip()
        has_voice = "use_voice" in payload
        use_voice = bool(payload.get("use_voice"))
        changed = 0
        for job in job_mgr.jobs:
            if wanted is not None and str(job.get("id")) not in wanted:
                continue
            target = job.setdefault("target", {})
            if has_type:
                target["session_type"] = session_type
            if has_id:
                target["session_id"] = session_id
            if has_voice:
                job.setdefault("action", {})["use_voice"] = use_voice
            changed += 1
        job_mgr.save()
        job_mgr.reload()
        return web.json_response({"success": True, "changed": changed,
                                  "jobs": job_mgr.describe()})

    async def handle_jobs_run(self, request):
        try:
            payload = await request.json()
            jid = str(payload.get("id", ""))
            job = next((j for j in job_mgr.jobs if str(j.get("id")) == jid), None)
            if not job:
                return web.json_response({"success": False, "error": "任务不存在"}, status=404)
            await job_mgr._run_job(job)
            return web.json_response({"success": True, "message": "任务已手动执行一次"})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    # ---------------- 待办 ----------------
    async def handle_todos(self, request):
        status = request.query.get("status")
        todos = todo_mgr.list_todos(status) if todo_mgr else []
        return web.json_response({"todos": todos})

    async def handle_todos_add(self, request):
        try:
            payload = await request.json()
            content = str(payload.get("content", "")).strip()
            if not content:
                return web.json_response({"success": False, "error": "内容不能为空"}, status=400)
            remind = payload.get("remind_time")
            remind_ts = None
            if isinstance(remind, (int, float)):
                remind_ts = float(remind)
            elif isinstance(remind, str) and remind.strip():
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
                    try:
                        remind_ts = time.mktime(time.strptime(remind.strip(), fmt))
                        break
                    except ValueError:
                        continue
                if remind_ts is None:
                    return web.json_response({"success": False, "error": "时间格式应为 YYYY-MM-DD HH:MM"}, status=400)
            if remind_ts is None:
                return web.json_response({"success": False, "error": "请填写提醒时间"}, status=400)
            todo = todo_mgr.add_todo(content, remind_ts,
                                     payload.get("session_type", "private"),
                                     payload.get("session_id", ""),
                                     payload.get("user_id", ""), source="manual",
                                     use_voice=(bool(payload["use_voice"])
                                                if "use_voice" in payload else None))
            return web.json_response({"success": bool(todo), "todo": todo})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_todos_update(self, request):
        try:
            payload = await request.json()
            todo_id = int(payload.get("id", 0))
            # status 必填且必须在白名单内：漏传时默认标成"已完成"会静默改错状态，
            # 任意字符串又会直接落库
            status = str(payload.get("status", "") or "")
            if status not in TODO_STATUSES:
                return web.json_response(
                    {"success": False, "error": f"状态非法：{status or '(未提供)'}"}, status=400)
            if status == "done":
                todo_mgr.complete(todo_id)
            elif status == "cancelled":
                todo_mgr.delete(todo_id)
            else:
                todo_mgr.db.execute("UPDATE todos SET status=? WHERE id=?", (status, todo_id))
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_todos_delete(self, request):
        try:
            payload = await request.json()
            todo_mgr.delete(int(payload.get("id", 0)))
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_todos_batch(self, request):
        """批量修改待办的会话类型 / 会话 ID（与定时任务/节日共用同一套规则）。"""
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "请求体不是合法 JSON"}, status=400)
        ids = payload.get("ids")
        wanted = {int(i) for i in ids} if isinstance(ids, list) else None
        has_type = "session_type" in payload
        session_type = str(payload.get("session_type") or "").strip()
        if has_type and session_type not in ("private", "group"):
            return web.json_response({"success": False, "error": "会话类型只能是 private 或 group"},
                                     status=400)
        has_id = "session_id" in payload
        session_id = str(payload.get("session_id") or "").strip()
        has_voice = "use_voice" in payload
        use_voice = 1 if bool(payload.get("use_voice")) else 0
        changed = 0
        if todo_mgr is not None:
            rows = todo_mgr.list_todos()
            for row in rows:
                if wanted is not None and int(row.get("id", 0)) not in wanted:
                    continue
                sets = []
                params = []
                if has_type:
                    sets.append("session_type=?")
                    params.append(session_type)
                if has_id:
                    sets.append("session_id=?")
                    params.append(session_id)
                if has_voice:
                    sets.append("use_voice=?")
                    params.append(use_voice)
                if not sets:
                    continue
                params.append(int(row["id"]))
                todo_mgr.db.execute("UPDATE todos SET " + ", ".join(sets)
                                    + " WHERE id=?", tuple(params))
                changed += 1
        return web.json_response({"success": True, "changed": changed,
                                  "todos": (todo_mgr.list_todos() if todo_mgr else [])})

    # ---------------- 事件问候 ----------------
    async def handle_events(self, request):
        return web.json_response({"events": event_mgr.events if event_mgr else []})

    async def handle_events_save(self, request):
        try:
            payload = await request.json()
            events = payload.get("events", [])
            known = {str(e.get("id")): e for e in (event_mgr.events if event_mgr else [])}
            cleaned = []
            for ev in events:
                eid = str(ev.get("id") or f"evt_{int(time.time()*1000)}")
                cleaned.append({
                    "id": eid, "name": str(ev.get("name") or eid),
                    "type": ev.get("type", "date"),
                    "date": str(ev.get("date", "")),
                    "enabled": bool(ev.get("enabled", True)),
                    "mode": ev.get("mode", "template"),
                    "template": str(ev.get("template", "")),
                    "llm_prompt": str(ev.get("llm_prompt", "")),
                    "use_voice": bool(ev.get("use_voice", False)),
                    # 内置默认节日标记：请求里没带就沿用原值，丢了它"无目标时发给全部会话"的兜底会失效
                    "default": bool(ev.get("default", known.get(eid, {}).get("default", False))),
                    # 会话 ID 留空等于清空目标，存成空目标占位会顶掉默认节日的兜底
                    "targets": [t for t in (ev.get("targets") or [])
                                if isinstance(t, dict) and str(t.get("session_id") or "").strip()],
                })
            event_mgr.events = cleaned
            event_mgr.save_events()
            return web.json_response({"success": True, "events": cleaned})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_events_batch(self, request):
        """批量修改节日/纪念日问候的发送目标（会话类型 / 会话 ID）。

        ids 为空表示应用到全部事件；两个字段都留空表示"清空会话 ID"
        即回到"发给所有聊过的会话"。
        """
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "请求体不是合法 JSON"}, status=400)
        ids = payload.get("ids")
        wanted = {str(i) for i in ids} if isinstance(ids, list) else None
        has_type = "session_type" in payload
        session_type = str(payload.get("session_type") or "").strip()
        if has_type and session_type not in ("private", "group"):
            return web.json_response({"success": False, "error": "会话类型只能是 private 或 group"},
                                     status=400)
        has_id = "session_id" in payload
        session_id = str(payload.get("session_id") or "").strip()
        has_voice = "use_voice" in payload
        use_voice = bool(payload.get("use_voice"))
        changed = 0
        for ev in (event_mgr.events if event_mgr else []):
            if wanted is not None and str(ev.get("id")) not in wanted:
                continue
            # 只改语音时别碰发送目标：给本来没有目标的事件补一个空目标，会顶掉
            # "内置默认节日发给全部会话"的兜底，问候从此永远发不出去。
            if has_type or has_id:
                targets = ev.get("targets") or []
                if not targets:
                    targets = [{"session_type": "private", "session_id": ""}]
                tg = targets[0]
                if has_type:
                    tg["session_type"] = session_type
                if has_id:
                    tg["session_id"] = session_id
                # 会话 ID 留空 = 清空目标，回到"发给所有聊过的会话"，不能留空目标占位
                ev["targets"] = targets if str(tg.get("session_id") or "").strip() else []
            if has_voice:
                ev["use_voice"] = use_voice
            changed += 1
        if event_mgr is not None:
            event_mgr.save_events()
        return web.json_response({"success": True, "changed": changed,
                                  "events": (event_mgr.events if event_mgr else [])})

    async def handle_events_test(self, request):
        try:
            payload = await request.json()
            ok = await event_mgr.greet_event_now(str(payload.get("id", "")), sender,
                                                 get_active_ctx, get_active_emotions)
            return web.json_response({"success": bool(ok),
                                      "message": "已发送测试问候" if ok else
                                      "该事件没有可发送的目标会话（内置节日会在当天发给全部会话），或问候内容为空"})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    # ---------------- 工具调用 ----------------
    async def handle_tools(self, request):
        return web.json_response({"tools": tool_registry.tools if tool_registry else [],
                                  "enabled": bool(self.config.get("tools_enabled", False))})

    async def handle_search_engines(self, request):
        """搜索引擎下拉框数据：内置 + 自定义引擎，全部来自注册表（不硬编码）。"""
        from modules.tools import available_engines, default_engine_key, parse_api_keys
        engines = available_engines(self.config)
        key_names = sorted({spec.get("api_key_env", "") for spec in engines.values()
                            if spec.get("api_key_env")})
        configured = parse_api_keys(self.config.get("web_search_api_keys", {}))
        return web.json_response({
            "engines": [{"key": key, "label": spec.get("label", key),
                         "description": spec.get("desc", ""),
                         "api_key_env": spec.get("api_key_env", ""),
                         "needs_api": bool(spec.get("api_url")) and not spec.get("parse_html")}
                        for key, spec in engines.items()],
            "current": default_engine_key(self.config),
            "custom": str(self.config.get("web_search_custom_engines", "") or ""),
            "url_override": str(self.config.get("web_search_url", "") or ""),
            "api_key_envs": key_names,
            "api_keys_set": sorted(configured.keys()),
        })

    async def handle_search_engine_save(self, request):
        """保存默认搜索引擎、自定义引擎、API key（写进 config.json 后热重载）。"""
        try:
            from modules.tools import available_engines, parse_api_keys, invalid_custom_engines
            payload = await request.json()
            engine = str(payload.get("engine", "") or "").strip()
            custom = str(payload.get("custom", self.config.get("web_search_custom_engines", "")) or "")
            bad = invalid_custom_engines(custom)
            if bad:
                return web.json_response(
                    {"success": False,
                     "error": "自定义引擎地址模板缺少 {query} 占位符，无法带上搜索词："
                              + "、".join(bad)}, status=400)
            if engine and engine not in available_engines({**self.config.config,
                                                          "web_search_custom_engines": custom}):
                return web.json_response({"success": False, "error": f"未知搜索引擎: {engine}"}, status=400)
            new_config = {**self.config.default_config(), **self.config.config}
            if "custom" in payload:
                new_config["web_search_custom_engines"] = custom
            if "url_override" in payload:
                new_config["web_search_url"] = str(payload.get("url_override", "") or "").strip()
            if "api_keys" in payload:
                # 只并入非空值：WebUI 回显不了已保存的 key，空值不能把旧 key 抹掉
                merged = {**parse_api_keys(self.config.get("web_search_api_keys", {})),
                          **parse_api_keys(payload.get("api_keys"))}
                new_config["web_search_api_keys"] = merged
            if engine:
                new_config["web_search_engine"] = engine
            self.config.config = new_config
            self.config._atomic_save(new_config)
            try:
                self._after_config_reload()
            except Exception as reload_err:
                return web.json_response(
                    {"success": True,
                     "message": f"配置已保存，但热重载失败（{reload_err}），请重启程序使运行状态与配置一致。"})
            return web.json_response({"success": True, "engine": new_config.get("web_search_engine", "")})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_tools_save(self, request):
        try:
            payload = await request.json()
            tools = payload.get("tools", [])
            for t in tools:
                if not str(t.get("name", "")).strip():
                    return web.json_response({"success": False, "error": "工具名不能为空"}, status=400)
            tool_registry.tools = tools
            tool_registry.save()
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_tools_test(self, request):
        try:
            payload = await request.json()
            name = payload.get("name", "")
            arguments = payload.get("arguments", {})
            if not arguments or (isinstance(arguments, dict) and not arguments):
                tool = next((x for x in tool_registry.tools if x.get("name") == name), None)
                if tool:
                    from modules.tools import test_sample_for
                    sample = test_sample_for(tool)
                    if sample:
                        arguments = sample
            user_id = str(payload.get("user_id", "") or "")
            tool_registry.begin_reply()
            ok, output = await tool_registry.execute(name, arguments, user_id)
            return web.json_response({"success": ok, "output": output})
        except Exception as e:
            return web.json_response({"success": False, "output": str(e)}, status=400)

    # ---------------- RAG ----------------
    async def handle_rag_docs(self, request):
        return web.json_response({"docs": rag_mgr.list_docs() if rag_mgr else [],
                                  "enabled": bool(self.config.get("rag_enabled", False))})

    async def handle_rag_upload(self, request):
        try:
            results = []
            reader = await request.multipart()
            async for part in reader:
                if not part.filename:
                    continue
                raw = await part.read(decode=False)
                if len(raw) > 20 * 1024 * 1024:
                    results.append({"file": part.filename, "success": False,
                                    "error": "文件超过 20MB 上限，请拆分后上传"})
                    continue
                tmp = self.memory_manager.data_path / f"rag_upload_{int(time.time()*1000)}_{Path(part.filename).name}"
                tmp.write_bytes(raw)
                try:
                    text = extract_text_from_file(tmp)
                    r = await rag_mgr.add_document(Path(part.filename).stem, text)
                    results.append({"file": part.filename, **r})
                except Exception as e:
                    results.append({"file": part.filename, "success": False, "error": str(e)})
                finally:
                    tmp.unlink(missing_ok=True)
            return web.json_response({"success": True, "results": results})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_rag_delete(self, request):
        try:
            payload = await request.json()
            ok = rag_mgr.delete_document(str(payload.get("id", "")))
            return web.json_response({"success": ok})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_rag_query(self, request):
        try:
            payload = await request.json()
            hits = await rag_mgr.search(str(payload.get("question", "")))
            return web.json_response({"success": True, "hits": hits})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    # ---------------- 用户画像 ----------------
    async def handle_profiles(self, request):
        profiles = [{"user_id": uid, **(p or {})} for uid, p in (profile_mgr.profiles or {}).items()]
        return web.json_response({"profiles": profiles})

    async def handle_profiles_save(self, request):
        try:
            payload = await request.json()
            uid = str(payload.get("user_id", "")).strip()
            if not uid:
                return web.json_response({"success": False, "error": "user_id 不能为空"}, status=400)
            profile_mgr.update(uid, payload.get("profile", {}), replace=True)
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_profiles_delete(self, request):
        try:
            payload = await request.json()
            ok = profile_mgr.delete(str(payload.get("user_id", "")))
            return web.json_response({"success": ok})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    # ---------------- 自主学习 ----------------
    async def handle_lexicon(self, request):
        if lexicon_mgr is None:
            return web.json_response({"terms": [], "pending": [], "enabled": False})
        return web.json_response({"terms": lexicon_mgr.list_terms(),
                                  "pending": lexicon_mgr.list_pending(),
                                  "enabled": lexicon_mgr.enabled})

    async def handle_lexicon_confirm(self, request):
        try:
            payload = await request.json()
            ok = lexicon_mgr is not None and lexicon_mgr.confirm(
                str(payload.get("id", "")), payload.get("term"),
                payload.get("meaning"), payload.get("category"))
            return web.json_response({"success": ok})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_lexicon_reject(self, request):
        try:
            payload = await request.json()
            ok = lexicon_mgr is not None and lexicon_mgr.reject(str(payload.get("id", "")))
            return web.json_response({"success": ok})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_lexicon_save(self, request):
        try:
            payload = await request.json()
            ok = lexicon_mgr is not None and lexicon_mgr.upsert_term(
                payload.get("term", ""), payload.get("meaning", ""), payload.get("category", ""))
            return web.json_response({"success": ok})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_lexicon_delete(self, request):
        try:
            payload = await request.json()
            ok = lexicon_mgr is not None and lexicon_mgr.delete_term(payload.get("term", ""))
            return web.json_response({"success": ok})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    # ---------------- 表情包 ----------------
    async def handle_stickers_list(self, request):
        root = sticker_mgr.dir if sticker_mgr else Path("data/stickers")
        categories = []
        if root.exists():
            for folder in sorted(root.iterdir()):
                if folder.is_dir():
                    files = [f.name for f in sorted(folder.iterdir())
                             if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}]
                    if files:
                        categories.append({"name": folder.name, "files": files})
        return web.json_response({"root": str(root), "categories": categories,
                                  "enabled": bool(sticker_mgr and sticker_mgr.enabled),
                                  "mode": sticker_mgr.mode if sticker_mgr else "off"})

    async def handle_stickers_upload(self, request):
        try:
            reader = await request.multipart()
            category = ""
            saved = []
            async for part in reader:
                if part.name == "category":
                    category = (await part.text()).strip()
                elif part.filename:
                    folder = _safe_subdir(sticker_mgr.dir, category)
                    if folder is None:
                        return web.json_response({"success": False, "error": "分类名非法"}, status=400)
                    folder.mkdir(parents=True, exist_ok=True)
                    fname = safe_sticker_name(Path(part.filename).name)
                    if not fname or Path(fname).suffix.lower() not in IMAGE_EXTS:
                        continue
                    payload_bytes = await part.read(decode=False)
                    if len(payload_bytes) > 10 * 1024 * 1024:
                        continue
                    (folder / fname).write_bytes(payload_bytes)
                    saved.append(f"{category}/{fname}")
            sticker_mgr.rescan()
            return web.json_response({"success": True, "saved": saved})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_stickers_delete(self, request):
        try:
            payload = await request.json()
            folder = _safe_subdir(sticker_mgr.dir, payload.get("category", ""))
            fname = safe_sticker_name(payload.get("name", ""))
            if folder is None or not fname:
                return web.json_response({"success": False, "error": "参数非法"}, status=400)
            target = folder / fname
            if target.exists():
                target.unlink()
            sticker_mgr.rescan()
            return web.json_response({"success": True})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_stickers_file(self, request):
        category = request.query.get("category", "")
        fname = safe_sticker_name(request.query.get("name", ""))
        folder = _safe_subdir(sticker_mgr.dir, category) if sticker_mgr else None
        if folder is None or not fname:
            return web.Response(status=404, text="not found")
        if Path(fname).suffix.lower() not in IMAGE_EXTS:
            return web.Response(status=404, text="not found")
        target = folder / fname
        # 类型按扩展名给出：aiohttp 内置的 MIME 表里没有 .webp，
        # 猜不出类型会退回 application/octet-stream，配上 nosniff 后浏览器拒绝渲染
        return await _local_file_response(
            target, MIME_BY_EXT.get(target.suffix.lower(), "application/octet-stream"))

    async def start(self):
        host = self.config.get("webui_host", "127.0.0.1")
        base_port = int(self.config.get("webui_port", 11500))
        if not HAS_AIOHTTP:
            print("未安装 aiohttp，WebUI 不可用")
            return

        # 尝试多个端口，若被占用则自动递增，最多尝试 10 次
        max_tries = 10
        for attempt in range(max_tries):
            port = base_port + attempt
            try:
                print(f"正在启动 WebUI：http://{host}:{port}")
                runner = web.AppRunner(self.app)
                await runner.setup()
                site = web.TCPSite(runner, host, port)
                await site.start()
                print(f"WebUI 已启动：http://{host}:{port}")
                self.runner = runner
                # 若使用了非默认端口，更新配置
                if port != base_port:
                    self.config.config["webui_port"] = port
                    # 可选：持久化到配置文件
                    self.config._atomic_save(self.config.config)
                return
            except OSError as e:
                # 端口被占用或其他系统错误，尝试下一个端口
                print(f"端口 {port} 不可用（{e}），尝试下一个端口...")
                await runner.cleanup()  # 清理失败的 runner
            except Exception as e:
                error_msg = f"WebUI 启动失败（端口 {port}）：{type(e).__name__}: {e}"
                print(error_msg)
                try:
                    with open(runtime_path("webui_error.log"), "a", encoding="utf-8") as f:
                        f.write(f"{time.ctime()} - {error_msg}\n")
                except Exception:
                    pass
                return  # 其他异常不自动切换，直接记录并退出

        # 所有端口尝试失败
        print("错误：无法找到可用端口，WebUI 启动失败。")
        try:
            with open(runtime_path("webui_error.log"), "a", encoding="utf-8") as f:
                f.write(f"{time.ctime()} - 所有端口被占用，WebUI 启动失败。\n")
        except Exception:
            pass

    async def shutdown(self):
        if not HAS_AIOHTTP:
            return
        runner = getattr(self, "runner", None)
        if runner is not None:
            # 只 cleanup app 不会停止 TCPSite 的监听，端口仍被占用；
            # 热重启/测试流程会因此碰到 "Address already in use"
            try:
                await runner.cleanup()
            except Exception as e:
                print(f"关闭 WebUI 监听失败: {type(e).__name__}: {e}")
            self.runner = None
        await self.app.cleanup()


async def ensure_tts_service_enabled_check() -> bool:
    try:
        from modules.tts_service import check_tts_service
        return await check_tts_service(global_config)
    except Exception:
        return False


# ============================================================================
# 主入口
# ============================================================================

async def main(stop_event: threading.Event = None):
    global global_config, global_emotion_manager, memory_manager
    global db, stats_mgr, sticker_mgr, tool_registry, profile_mgr, rag_mgr
    global lexicon_mgr
    global todo_mgr, job_mgr, event_mgr, sender, mood_mgr
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding='utf-8')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding='utf-8')

    sys.stdout = StdoutRedirector(sys.stdout)
    os.environ["NAP_CAT_PLUGIN_INDEX_URL"] = ""
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    os.environ["no_proxy"] = "localhost,127.0.0.1"

    print("=" * 180)
    print(
        "                   ⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠟⣛⣩⣤⣶⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣶⣦⣬⣉⠛⠀⠀⠀⠀⠀⢛⣋⣩⣥⠴⠶⠶⠟⠛⠛⠛⠛⠛⠛⠛⠻⠿⠷⠶⢶⣦⣤⣍⣉⡛⠛⠿⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "                   ⣿⣿⣿⣿⣿⣿⣿⣿⣿⠟⣋⣥⣶⠿⣛⣭⣷⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠟⣋⠁⠀⠀⠄⢒⣋⣩⣥⣴⣶⣶⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣶⣶⣦⣭⣍⣛⠻⢷⣶⣤⣍⣙⠛⠿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "                   ⣿⣿⣿⣿⣿⣿⠟⣋⣴⡾⢟⣫⣴⠾⣻⣿⣿⣿⣿⠿⠿⠿⠟⠛⠛⠛⠛⠛⠉⠀⠉⣀⣤⣴⣶⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣮⣝⣿⣿⣿⣶⣦⣌⡙⠻⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "                   ⠿⠿⠿⠿⢛⣡⡾⢟⣩⣶⠿⠋⠗⣛⣉⣥⣤⠤⠶⣒⣒⣚⡯⠭⣉⡭⠛⢁⣤⣶⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣦⣌⠙⠿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "                   ⣀⣀⢀⡴⠟⣋⣐⣩⡤⢴⣒⣻⣭⣵⣶⠿⢟⣛⡭⠽⠖⠚⠋⠉⣁⣴⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣻⠿⣶⣄⡙⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "                   ⣫⡥⠖⣚⣩⣵⣶⣾⣿⠿⣿⣛⠭⠖⠚⣉⣩⣤⣶⡶⠟⢋⣤⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠿⣟⣛⣯⣽⣷⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣶⣭⡛⢦⣌⠙⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "                   ⣥⣾⣿⣿⠿⣟⡫⠵⠚⣋⣡⣤⣶⣾⣿⡿⠟⠋⠁⢀⣴⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠿⣟⣯⣵⣶⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣌⠳⣤⡉⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "                   ⢿⣛⠭⠒⣉⢅⣴⣾⣿⣿⣿⣿⠿⠋⠁⠀⠀⢀⣴⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⢛⣭⣶⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠿⣻⣽⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣎⠻⣦⡈⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "                   ⣩⡴⢠⡿⣣⣾⣿⣿⠿⠛⠉⠀⠀⠀⠀⢀⣴⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⣻⣵⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠟⣫⣶⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⣫⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣮⣝⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣌⢿⣦⡈⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "                   ⠿⣱⡟⣵⡿⠟⠋⠁⠀⠀⠀⢀⡤⠂⣴⣿⣿⣿⣿⣿⣿⣿⣿⡿⣛⣵⣿⣿⣿⣿⣿⣿⣿⣿⢟⣿⣿⡿⣋⣴⣿⣿⣿⣿⣿⣿⣿⠟⣫⣾⣿⣿⣿⣿⡿⣫⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣌⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣧⡹⣿⣆⠙⠀⠀⠀⢿⣿⣿⣿⣿⣿⣿⣿\n"
        "                   ⠀⠟⠘⠉⠀⠀⠀⠀⢀⣤⣾⠟⣠⣾⣿⣿⣿⣿⣿⣿⣿⡿⣫⣾⣿⣿⣿⣿⣿⣿⣿⣿⣯⣾⣿⠟⣡⣾⣿⣿⣿⣿⣿⣿⣿⠟⣡⣾⣿⣿⣿⣿⣿⢏⣼⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⡌⠻⣿⣿⣿⣿⣿⣿⣿⣿⣷⡌⢻⣷⡈⠛⠛⠛⠛⠛⠻⠿\n"
        "                   ⣇⠀⠀⠀⠀⣀⣴⣾⣿⡿⢃⣴⣿⣿⣿⣿⣿⣿⣿⡿⣫⣾⣿⣿⣿⣿⣿⣿⣿⡿⣫⣾⣿⠟⣡⣾⣿⣿⣿⣿⣿⣿⣿⠟⣡⣾⣿⣿⣿⣿⣿⡟⣱⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣦⡘⢿⣿⣿⣿⣿⣿⣿⣿⣿⡄⠳⠟⢠⡒⢦⠄⣀⣀⣤\n"
        "                   ⣞⣆⢀⣴⣾⣿⣿⣿⠟⢡⣾⣿⣿⣿⣿⣿⣿⣿⣫⣾⣿⣿⣿⣿⣿⣿⣿⡿⣫⣾⣿⠟⣡⣾⣿⣿⣿⣿⣿⣿⣿⡿⡡⣾⣿⣿⣿⣿⣿⣿⢋⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣄⢻⣿⣿⣿⣿⣿⣿⣿⣿⡄⢠⣦⠙⠎⣰⣷⣿⣿\n"
        "                   ⠿⠜⣄⠻⣿⣿⣿⠏⣰⣿⣿⣻⣿⣿⣿⣿⣟⣵⣿⣿⣿⣿⣿⣿⣿⣿⢫⣾⣿⡿⢋⣾⣿⣿⣿⣿⣿⣿⣿⣿⢏⢴⣾⣿⣿⣿⣿⣿⡿⣱⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢹⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣯⢦⠹⠿⠿⣿⣿⣿⣿⣿⣿⡀⠃⠀⠀⠹⣿⣿⣿\n"
        "                   ⠉⠉⠙⠂⠹⣿⠃⣼⣿⡿⣱⣿⣿⣿⣿⢯⣾⣿⣿⣿⣿⣿⣿⣿⢟⣵⣿⣿⠏⣴⣿⣿⣿⣿⣿⣿⢿⢿⠟⠡⢢⣿⣿⣿⣿⣿⣿⠟⡼⣽⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠇⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡟⣿⣿⣿⣿⣿⣿⣿⢎⣴⣾⣷⡹⣿⣿⣿⣿⣿⣧⠀⠀⠀⢠⠘⣿⣿\n"
        "                   ⣦⡀⠀⠀⠀⢀⣼⣿⡿⣱⣿⣿⣿⡿⣳⣿⣿⣿⣿⣿⣿⣿⡿⢫⣾⣿⡿⢡⣾⣿⣿⣿⣿⣿⣿⣿⡿⠃⡴⣱⣿⣿⣿⣿⣿⣿⢏⣞⣽⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⣹⡟⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠸⣿⣿⣿⡿⡿⢡⣾⣿⣿⣿⣇⢹⣿⣿⣿⣿⣿⡄⠀⠀⢸⢣⠘⣿\n"
        "                   ⣿⣿⣦⡀⢀⣾⣿⣿⢡⣿⣿⣿⡿⣱⣿⣿⣿⣿⣿⣿⣿⡟⣱⣿⣿⠟⣰⣿⣿⣿⣿⣿⣿⣿⣿⢟⡔⡜⣼⣿⣿⣿⣿⣿⣿⢏⣞⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢃⣿⢁⣿⣿⣿⣿⣿⣿⣿⣿⢿⣿⣿⣿⣿⣿⡆⢿⣿⣿⣿⢁⣾⣿⠿⠟⠛⠛⠈⣿⣿⣿⣿⣿⣧⠀⠀⠈⣏⢧⠸\n"
        "                   ⣿⣿⣿⠃⣼⣿⣿⢣⣿⣿⣿⣿⣱⣿⣿⣿⣿⣿⣿⣿⢏⣼⣿⣿⠏⣼⣿⣿⣿⣿⣿⣿⣿⣿⢃⠞⢜⣾⣿⣿⣿⣿⣿⣿⢏⡞⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⣼⠃⢸⣿⣿⣿⣿⣿⣿⣿⡟⣾⣿⣿⣿⣿⣿⡇⢸⣿⣿⡏⢸⢿⣧⠀⠀⠀⠀⠀⢹⣿⣿⣿⣿⣿⠀⠀⠀⠸⡌⢧\n"
        "                   ⠻⣿⠃⣼⣿⣿⢇⣾⣿⣿⣿⢳⣿⣿⣿⣿⣿⣿⣿⢋⣾⣿⣿⢋⣾⣿⣿⣿⣿⣿⣿⣿⡿⢡⡏⢌⣾⣿⣿⣿⣿⣿⣿⢏⡞⣼⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⢰⡟⠀⣾⣟⢿⣿⣿⣿⣿⣿⢃⣿⣽⣿⣿⣿⣿⡇⢸⣿⣿⡇⠀⠀⢀⠀⠀⠀⠀⠀⠸⣿⣿⣿⣿⣿⡇⠀⠀⣆⠗⢋\n"
        "                   ⣷⠆⣸⣿⣿⡟⣼⣿⣿⣿⢧⣿⣿⣿⣿⣿⣿⡿⠃⠞⠛⠻⠁⠘⠛⠿⠿⣿⣿⣿⣿⡿⣱⡟⢈⣾⣿⣿⣿⣿⣿⣿⡏⡼⣹⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢃⡿⡡⢸⣿⣿⣷⣿⡻⣿⣿⡟⣸⣧⣿⣿⣿⣿⣿⡇⢸⣿⣿⡇⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⣿⡇⠀⣠⠴⠚⠉\n"
        "                   ⡟⢠⣿⣿⣿⢱⣿⣿⣿⡟⣾⣿⣿⣿⣿⣿⣦⢀⣀⠀⠠⠁⠀⠀⠀⠀⠀⠀⠉⠛⠿⣱⣿⢁⣾⣿⣿⣿⣿⣿⣿⡟⣸⢳⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡏⣾⢣⢇⣿⣿⣿⣿⣿⣿⣿⣿⢡⡿⣼⣿⣿⣿⣿⣿⡇⣼⣿⣿⣷⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣿⡿⠋⠀⠀⠀⠀⠀⠀\n"
        "                   ⠀⣿⣿⣿⠇⣿⣿⣿⣿⢱⣿⣿⣿⣿⣿⣿⢣⣿⣿⣿⠀⣀⣁⢤⣤⣄⣀⡀⠀⠀⠀⠈⠁⢼⣿⣿⣿⣿⣿⣿⣿⢡⡏⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⣸⠏⡞⣸⣿⣿⣿⣿⣿⣿⣿⠇⣾⢳⣿⣿⣿⣿⣿⣿⠃⣿⣿⣿⡟⠂⠀⠀⠀⠀⠀⠀⠀⠀⣿⡿⠋⠀⠀⠀⠀⠀⠀⠀⠀\n"
        "                   ⣸⣿⣿⡿⣸⣿⣿⣿⡇⣾⣿⣿⣿⣿⣿⢏⣾⣿⣿⡇⢠⣿⣿⣷⣮⣝⡻⠿⠋⠀⠀⠀⠀⠀⠙⢿⣿⣿⣿⣿⠇⡾⣸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣱⡟⣼⢣⣿⣿⣿⣿⣿⣿⣿⡟⣰⡏⣿⣿⣿⣿⣿⣿⣿⢠⣿⣿⡟⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠊⠀⠀⠀⠀⠀⠀⠀⡀⢀⣼\n"
        "                   ⣿⡿⣿⠇⣿⣿⣿⣿⢠⣿⣿⣿⣿⣿⡟⣾⣿⣿⣿⠁⣼⣿⣿⣿⣿⣿⣿⠁⠀⠀⠀⠀⠀⠀⠀⠀⠹⣿⣿⡟⢰⣇⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢣⡿⣰⡏⣼⣿⣿⣿⣿⣿⣿⡟⣰⡿⣽⣿⣿⣿⣿⣿⣿⡇⢸⣿⢸⡃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣀⡀⠐⢈⣴⡿⢋\n"
        "                   ⣿⢻⣿⢸⣿⣿⣿⡿⢸⣿⣿⣿⣿⣿⣹⣿⣿⣿⡏⠀⣿⣿⣿⣿⣿⣿⠃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠹⣿⡇⣾⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢯⡿⢡⣿⢳⣿⣿⣿⣿⣿⣿⡿⣰⣿⢳⣿⣿⣿⣿⣿⣿⡿⠀⣾⡇⣿⡇⢠⣀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠐⠉⠉⠀⠀⠉⠉⠀⢻\n"
        "                   ⡏⣿⡇⣾⣿⣿⣿⡇⣼⣿⣿⣿⣿⢯⣿⣿⣿⣿⢡⣿⣿⣿⣿⣿⣿⡟⢀⠀⠂⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⡇⡿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢏⣾⢣⣿⣗⣾⣿⣿⣿⣿⣿⡟⣱⣿⢯⣿⣿⣿⣿⣿⣿⣿⢡⠂⣿⢰⣿⣿⣆⠻⣿⣦⠒⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘\n"
        "                   ⢹⣿⢃⣿⣿⣿⣿⡇⣿⣿⣿⣿⡏⣸⣿⣿⣿⡿⢸⣿⣿⣿⣿⣿⣿⣧⣿⣷⡀⠀⠀⠀⠀⠀⠀⣶⣦⢀⢠⣷⣧⣿⣿⣿⣿⣿⣿⣿⣿⣿⢏⣾⢣⣿⡟⢸⣿⣿⠿⠿⠿⠟⠘⠛⠟⠿⠿⣿⣿⣿⣿⣿⢃⣿⢸⡇⣾⣿⣿⣿⡗⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣆⠀⠀⠀⠀\n"
        "                   ⣾⣿⢸⣿⣿⣿⣿⡇⣿⣿⣿⣿⡇⣿⣿⣿⣿⡇⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⠀⠀⠀⠀⠀⠀⠈⠁⢸⣿⣿⢿⣿⣿⣿⣿⣿⣿⣿⣿⢏⡾⣣⣿⠟⠋⠉⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠉⠙⠃⠺⠇⡿⢰⣿⣿⣿⡏⠀⠀⠀⠀⠀⠀⢀⢀⣠⡀⣀⢒⡉⠀⣿⣿⠀⠀⠀⠀\n"
        "                   ⣿⡟⢸⣿⣿⣿⣿⡇⢿⣿⣿⣿⡧⡝⣿⣿⣿⡇⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠟⠀⠀⠀⠀⠀⠀⠀⠀⢸⣿⣿⣸⣿⣿⣿⣿⣿⣿⣿⢏⡾⣵⠟⠁⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⣀⡀⠀⠀⠀⠀⠀⠰⠁⢿⣿⣿⡿⠀⢿⡴⢚⣡⡞⠿⠺⡏⢸⡇⢸⣿⠁⡆⠸⣿⡇⠀⠀⠀\n"
        "                   ⣿⡇⣿⣿⣿⣿⣿⣧⢸⣿⣿⣿⡇⣿⡌⢿⣿⡇⣿⣿⣿⣿⣿⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢋⣿⣾⣿⣿⡿⠁⠀⡀⠀⠀⠀⠀⠀⠀⠀⢀⡀⠀⢿⣿⣶⣤⣀⠀⠀⠀⠀⠀⠙⠿⠁⠀⢋⣴⣿⢰⣶⢼⡶⢻⡼⢃⣾⡇⢸⣧⠠⠻⠷⠀⠀⠀\n"
        "                   ⣿⡇⣿⣿⣿⣿⣿⣿⠸⡿⠟⣻⣧⢻⣿⠀⡹⣿⣿⣿⣿⣿⣿⣿⣿⡆⠀⣠⣴⣤⣀⡀⠀⣀⠀⠀⣸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣦⡀⠀⠀⠀⠀⠀⠀⠀⠀⠻⡿⠂⢸⣿⣿⣿⣿⣷⠄⡀⠀⠀⠀⠀⠑⣾⣿⣿⢟⣕⢲⢇⣼⡈⠇⣿⡟⠀⠀⠀⠀⠀⠀⠀⠀⠀\n"
        "                   ⣿⡇⣿⣿⣿⣿⣿⣿⣷⣿⣿⣿⣿⡈⢿⡀⣿⣾⣿⣿⣿⣿⣿⣿⣿⣿⣆⠙⣿⣿⣿⣿⡇⣴⣄⣰⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢸⣿⣿⣿⣿⡟⣰⣿⣦⠐⠀⠀⠀⠘⣿⣿⢬⢋⡞⣨⢫⢷⣄⣿⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀\n"
        "                   ⣿⡇⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡕⣌⢧⢻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣶⣭⣿⣿⣧⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣸⣿⣿⣿⡿⣱⣿⣿⠃⣠⣾⣷⣶⣦⣽⣇⠿⡺⣱⣏⠺⢗⣿⠃⡤⢤⣤⡄⢶⣦⠰⣶⣄⠀\n"
        "                   ⣿⡇⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⠈⠈⡋⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⠈⠉⠉⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣿⣿⣿⡟⣱⣿⣿⠃⣴⣿⣿⣿⣿⣿⣿⣫⣾⣱⣿⣿⣯⣼⣧⢰⣧⢸⣿⣿⡄⠻⣷⡘⢿⡄\n"
        "                   ⣿⡇⢻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⢀⠀⢷⣮⣻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⠀⠀⠀⣀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣾⣿⣿⢟⣼⣿⠟⢡⣾⣿⣿⣿⣿⣿⡿⢃⢜⡱⣿⣿⣿⣷⠎⣠⣏⢻⡄⢿⣿⣷⡐⢌⡛⢮⡳\n"
        "                   ⣿⡇⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠈⢄⠈⢻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣄⠠⣾⣿⣿⣶⣶⣦⠐⣂⠀⠀⣠⣾⣿⣿⢯⣟⣫⢅⣴⣿⣿⣿⣿⣿⣿⠟⣱⠏⡹⣛⣿⣿⣿⡏⢠⣝⡋⣚⡻⡘⣿⣿⣷⡘⢿⣶⣤\n"
        "                   ⣿⣷⠘⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡄⠃⠠⠀⠙⠿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣾⣿⣿⣿⣿⣿⠞⣿⣧⣾⣿⣿⣿⣿⣿⠟⣡⣾⣿⣿⣿⣿⣿⡿⢋⣾⢫⣾⢵⣯⣿⣿⡟⢠⣿⠟⢞⡿⡃⣳⠘⣿⣿⣷⡈⢿⣿\n"
        "                   ⣿⣿⠀⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣧⠘⠀⠀⠃⢀⠈⠻⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢟⣡⣾⣿⣿⣿⣿⣿⡿⢋⣴⢟⣵⣿⣿⡖⣤⡿⡟⢀⣿⣿⣷⣾⣿⣜⠿⡣⣘⡻⣿⣿⣄⠙\n"
        "                   ⣿⣿⡆⠸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡆⢡⠀⠀⠀⠁⠀⠀⠉⠻⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡵⣿⣿⣿⣿⣿⣿⡿⢋⣴⠟⣱⣿⣿⣿⣿⣧⡟⡟⠀⠀⣿⣿⣿⣿⣏⣹⣿⣜⠿⣇⣩⣝⢿⣦\n"
        "                   ⣿⣿⣧⠀⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡈⠀⠀⠀⠀⠄⢀⣤⣶⣄⡈⠛⠿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢛⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡷⠂⣠⣴⡤⣩⡴⢛⣥⣾⣿⣿⣿⣿⣿⣏⡸⡿⢂⠀⣿⣿⣿⣿⣿⣿⣿⣿⣏⣡⣙⣋⢸⣶\n"
        "                   ⣿⣿⣿⡀⡘⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⡀⠀⢀⠂⣠⣿⣿⣿⣿⣿⣷⢠⡄⠉⠛⠿⣿⣿⣿⣿⣿⣭⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠋⣠⡾⠟⠵⣊⣥⣾⣿⣿⣿⣿⣿⣿⣿⢯⡟⠀⢴⣶⡄⠸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣭\n"
        "                   ⣿⣿⣿⣧⠘⣢⡙⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⡀⠈⢰⣿⣿⣿⡏⣿⣿⣿⢸⠁⠀⠀⠀⠀⠈⠙⠛⠿⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠟⠋⢀⣤⣥⣶⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⣳⠏⢀⠂⠈⢉⣬⡀⠙⢿⣿⠿⠿⠛⠛⠻⣿⣿⣿⣿⣿\n"
        "                   ⢻⣿⣿⣿⣆⠩⢧⠑⠨⣙⠻⢿⣿⣿⣿⣿⣿⣿⣷⡄⢿⣿⣿⣿⢸⣿⣿⣿⠘⠀⠀⠀⠀⠀⠀⠀⠀⣤⣤⣤⣄⣉⣉⡙⠛⠛⠛⠛⠿⠿⠿⠿⠿⠿⠟⠛⠛⠛⠋⠉⠉⠀⢀⣴⣿⡿⣫⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣟⣽⠏⠀⠀⠀⡀⠌⠛⢃⣁⠀⠀⠀⠀⠀⠀⠀⠈⢿⣿⣿⣿\n"
        "                   ⣌⠻⠿⣿⣿⣆⠩⣧⠀⠀⠁⠂⢬⠉⠛⠿⢿⣿⣿⣿⣎⠻⣿⡇⡾⠋⠙⢿⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⣿⣿⣿⣿⡿⠁⠀⠀⠀⠀⠀⠀⠀⠀⠐⣰⣿⣿⣿⢖⣴⣿⡿⣫⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣫⣾⠏⠀⠐⠂⠁⠀⠀⠀⠙⠟⢁⣀⠀⠀⠀⠀⠀⠀⠘⣿⣿⣿\n"
        "                   ⣿⣿⣷⣶⣭⣍⣃⠈⢷⡀⠄⣂⣴⣶⣦⣑⠲⢠⠈⣭⣍⣓⡙⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢹⣿⣿⣿⣿⣿⣿⠟⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣰⣿⡿⢋⣵⣿⢟⣵⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢟⣵⣿⠏⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⠟⢁⣤⡀⠀⠀⠀⠀⠈⣉⡛\n"
        "                   ⣿⣿⣿⣿⣿⣿⣿⣦⡀⠋⣾⣿⣿⣿⣿⠿⠃⣉⡀⣿⣿⣿⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢿⣿⣿⣿⠟⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣰⠿⣋⣴⢟⢏⣴⣿⡿⣫⣿⣿⣿⣿⣿⣿⣿⣿⡿⣫⣾⣿⠋⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠛⠃⢴⣶⠀⣠⣄⠉⣁\n"
        "                   ⣿⣿⣿⣿⣿⣿⣿⣿⡿⣂⣽⣿⣷⡍⣥⣚⡛⠿⠇⣿⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⢿⠟⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢘⡥⢞⣫⢔⣵⣿⢟⣭⣾⣿⣿⣿⣿⣿⣿⣿⣿⢋⣾⣿⡿⢃⣶⣦⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠁⠀⠙⠋⠀⠻\n"
        "                   ⠻⣿⣿⣿⣿⣿⣿⣿⢸⣿⣿⣿⣿⠀⣿⣿⣿⣿⣶⣍⡛⠿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠂⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⡾⢽⡾⣋⣴⠿⣫⣵⣿⣿⣿⣿⣿⣿⣿⣿⣿⢟⣵⣿⣿⡿⠡⢿⣿⣿⣧⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀\n"
        "                   ⢷⣬⡛⢿⣿⣿⣿⣿⡎⢿⣿⣿⣿⡄⣿⣿⣿⣿⣿⣿⣿⣷⣦⡀⢀⡴⠂⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⡤⢞⣫⣷⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢟⣵⣿⣿⣿⡟⣱⣿⣷⡝⣿⡿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀\n"
        "                   ⠀⠙⠻⢶⣬⡙⠛⠉⠀⠀⠈⠀⠀⠀⢿⣿⣿⣿⣿⣿⣿⣿⢏⣴⠏⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠑⠦⣄⡀⢠⣾⣷⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⢛⣵⣿⣿⣿⣿⠟⣰⣿⣿⣿⣷⣆⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀\n"

        "\n\n\n"

        "                                           ██╗           ██████╗      ██╗   ██╗      ██████╗      ███╗   ███╗      ██████╗ \n"
        "                                           ██║          ██╔═══██╗     ██║   ██║     ██╔═══██╗     ████╗ ████║     ██╔═══██╗\n"
        "                                           ██║          ██║   ██║     ██║   ██║     ██║   ██║     ██╔████╔██║     ██║   ██║\n"
        "                                           ██║          ██║   ██║     ╚██╗ ██╔╝     ██║   ██║     ██║╚██╔╝██║     ██║   ██║\n"
        "                                           ███████╗     ╚██████╔╝      ╚████╔╝      ╚██████╔╝     ██║ ╚═╝ ██║     ╚██████╔╝\n"
        "                                           ╚══════╝      ╚═════╝        ╚═══╝        ╚═════╝      ╚═╝     ╚═╝      ╚═════╝ \n"


        "\n\n                                                                            启动成功啦！                                                              "
    )
    print("=" * 180)

    """
    aSBsb3ZlIG11cmFzYW1l
    """

    global_config = ConfigLoader()
    apply_log_max_size(global_config)
    global_emotion_manager = EmotionManager(global_config)
    # 情绪模仿列表要按角色扫描参考音频目录，只有主程序有文件系统上下文，注入给提示词层
    set_mimics_provider(get_role_mimics)
    if not global_emotion_manager.emotions:
        print("\n[警告] 未找到任何情绪配置（请检查 ref_audio_root 目录），将降级为纯文本模式。")

    memory_manager = MemoryManager(global_config)
    mood_mgr = MoodManager(memory_manager.data_path)
    try:
        from modules.llm_helpers import check_llm_service
        llm_ok, llm_detail = await check_llm_service(get_active_ctx())
        if llm_ok:
            print(f"LLM 服务连接正常：{llm_detail}")
        else:
            print(f"[警告] LLM 服务不可达：{llm_detail}\n"
                  "回复生成 / 回复审判 / RAG 嵌入 / 文本生成等功能将失败。"
                  "请确认服务已启动，并检查 llm_backend、llm_base_url、"
                  "llm_model_name 配置是否与你的服务匹配。")
    except Exception as e:
        print(f"LLM 服务探测异常: {type(e).__name__}: {e}")

    # 初始化功能模块
    db = DatabaseManager(memory_manager.data_path)
    stats_mgr = StatsManager(db)
    sticker_mgr = StickerManager(global_config)
    tool_registry = ToolRegistry(global_config, memory_manager.data_path)
    profile_mgr = UserProfileManager(global_config, memory_manager.data_path)
    rag_mgr = RAGManager(global_config, memory_manager.data_path)
    lexicon_mgr = LexiconManager(global_config, memory_manager.data_path)
    sender = MessageSender(global_config, memory_manager, sticker_mgr, stats_mgr)
    todo_mgr = TodoManager(global_config, db, scheduler, sender,
                           emotions_provider=get_active_emotions)
    todo_mgr.ctx_provider = get_active_ctx
    todo_mgr.restore_pending()
    job_mgr = ScheduledJobManager(global_config, memory_manager.data_path, scheduler,
                                  sender, get_active_ctx, get_active_emotions,
                                  list_known_sessions, session_history_block)
    event_mgr = EventManager(global_config, memory_manager.data_path, profile_mgr)

    # 启动调度器与功能任务
    load_proactive_state()
    seed_proactive_sessions()
    scheduler.start()
    register_feature_jobs()
    _spawn(greeting_catchup_task())
    job_mgr.register_all()

    if global_config.get("auto_start_tts", False):
        threading.Thread(target=auto_start_and_switch_tts, args=(global_config,), daemon=True).start()

    def _run_on_main_loop(coro_factory, timeout: float = 10.0) -> bool:
        """把插件调用的协程丢到主事件循环执行，返回是否成功。

        插件的钩子是同步函数，而 sender 的方法是协程；主事件循环在前端
        线程里跑（MAIN_EVENT_LOOP），这里做跨线程投递。已经在循环线程里
        时不能再 run_until_complete（会死锁），只能交给任务调度。
        """
        async def _do():
            try:
                return bool(await coro_factory())
            except Exception as e:
                print(f"[插件] 异步操作失败: {type(e).__name__}: {e}")
                return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            try:
                loop.create_task(_do())
                return True
            except Exception:
                return False
        main_loop = globals().get("MAIN_EVENT_LOOP")
        if main_loop is not None and not main_loop.is_closed():
            try:
                fut = asyncio.run_coroutine_threadsafe(_do(), main_loop)
                return bool(fut.result(timeout=timeout))
            except Exception as e:
                print(f"[插件] 异步操作超时/失败: {type(e).__name__}: {e}")
                return False
        print("[插件] 事件循环尚未就绪，操作未执行")
        return False

    def plugin_send_text(session_type, target_id, text: str) -> bool:
        """给插件用的同步发消息桥（支持群聊与私聊）。"""
        try:
            tid = int(target_id)
        except (TypeError, ValueError):
            return False
        kind = "private" if str(session_type) == "private" else "group"
        return _run_on_main_loop(
            lambda: sender.send_text(kind, tid, str(text)))

    def plugin_send_voice(session_type, target_id, text: str,
                          emotion: str = "") -> bool:
        """给插件用的同步发语音桥：复用主程序的 TTS 链路合成后发出。

        情绪名无效时退回角色默认音色（synthesize_sentence 内部已有兜底），
        合成失败只返回 False，插件自行决定是否降级为纯文本。
        """
        try:
            tid = int(target_id)
        except (TypeError, ValueError):
            return False
        kind = "private" if str(session_type) == "private" else "group"
        emotions = get_active_emotions()
        data_path = memory_manager.data_path

        async def _do():
            if not await ensure_tts_service(global_config):
                return False
            wav = await synthesize_sentence(global_config, str(text),
                                            str(emotion or ""), emotions,
                                            data_path, stats=stats_mgr)
            if not wav:
                return False
            try:
                ok = await sender.send_voice(kind, tid, wav)
            finally:
                try:
                    Path(wav).unlink(missing_ok=True)
                except Exception:
                    pass
            return ok

        return _run_on_main_loop(_do, timeout=120.0)

    def plugin_send_message(group_id, text: str) -> bool:
        """兼容旧签名：早期只有群聊，现在转发到通用桥。"""
        return plugin_send_text("group", group_id, text)

    webui_server = None
    if HAS_AIOHTTP and global_config.get("webui_enabled", True):
        webui_server = WebUIServer(global_config, memory_manager)
        _WEBUI_SERVER_HOLDER["server"] = webui_server
        # 注入插件运行时：这时 sender 已就绪，插件才能真的发消息。
        if global_config.get("plugins_enabled", True):
            try:
                from modules.plugin_runtime import PluginRuntime
                _plugin_rt = PluginRuntime(
                    webui_server.plugin_manager,
                    logger=_plugin_log,
                    config_getter=lambda: (global_config.config or {}),
                    sender=plugin_send_text,
                    voice_sender=plugin_send_voice,
                    emotions_getter=get_active_emotions)
                webui_server.attach_plugin_runtime(_plugin_rt)
                _plugin_errors = _plugin_rt.load_all()
                for _pid, _err in (_plugin_errors or {}).items():
                    print(f"[插件] {_pid} 加载失败: {_err}")
            except Exception as e:
                print(f"[插件] 运行时初始化失败（插件功能不可用，主程序继续）：{e}")
        else:
            print("[插件] 插件系统已关闭（config: plugins_enabled=false）")
        try:
            await webui_server.start()
        except Exception as e:
            print(f"WebUI 启动异常，继续运行其他功能：{e}")


    profiles = build_connection_profiles(global_config)
    print("正在连接 NapCat：" + "；".join(
        f"{p['label']}→{p['ws_url']}" for p in profiles))

    active_clients = []

    async def _stop_watcher():
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    for c in list(active_clients):
                        closer = getattr(c, "close", None)
                        if closer is None:
                            continue
                        try:
                            result = closer()
                            if hasattr(result, "__await__"):
                                await result
                        except Exception:
                            pass
                    active_clients.clear()
                    return
                await asyncio.sleep(0.5)
        except Exception:
            pass

    def _consume_task_result(task):
        try:
            err = task.exception()
        except (asyncio.CancelledError, Exception):
            return
        if err is None:
            return
        # 这里吞掉异常等于"机器人没反应"且一行日志都没有，排查时无从下手
        print(f"[消息处理异常] {type(err).__name__}: {err}")
        traceback.print_exception(type(err), err, err.__traceback__)

    async def _run_profile(profile: dict):
        """一条 NapCat 连接的重连循环。角色独占的连接只服务该角色。"""
        label = profile["label"]
        role_key = profile["role_key"]
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            client = NapCatClient(ws_url=profile["ws_url"], token=profile["token"])
            active_clients.append(client)
            try:
                async with client:
                    if role_key:
                        sender.set_role_client(role_key, client)
                        ROLE_CONNECTIONS[id(client)] = role_key
                    else:
                        sender.client = client
                    print(f"已连接！{label} QQ: {client.self_id}")
                    print("等待消息中...")
                    async for event in client:
                        if stop_event is not None and stop_event.is_set():
                            return
                        task = asyncio.create_task(handle_message_event(event, client))
                        task.add_done_callback(_consume_task_result)
                # 连接被服务端正常关闭（非异常路径）：稍候重连，避免紧密循环
                if stop_event is None or not stop_event.is_set():
                    print(f"{label} 连接已断开，3秒后重连...")
                    await asyncio.sleep(3)
            except Exception as e:
                print(f"{label} NapCat 连接失败: {e}")
                print("10秒后尝试重新连接...")
                await asyncio.sleep(10)
            finally:
                if role_key:
                    sender.set_role_client(role_key, None)
                    ROLE_CONNECTIONS.pop(id(client), None)
                elif sender.client is client:
                    sender.client = None  # 避免主动消息/定时任务使用失效连接
                try:
                    active_clients.remove(client)
                except ValueError:
                    pass

    watcher = asyncio.create_task(_stop_watcher())
    tasks = [asyncio.create_task(_run_profile(p)) for p in profiles]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        watcher.cancel()
    print("正在关闭所有子进程...")
    save_proactive_state()
    # 关闭顺序按「先关网络服务、再断子进程、最后落盘」：
    # WebUI 先停能立刻释放端口，用户看到的是窗口立刻消失，而不是等 TTS 收尾。
    if webui_server:
        try:
            await webui_server.shutdown()
        except Exception as e:
            print(f"关闭 WebUI 失败（忽略）：{e}")
        _WEBUI_SERVER_HOLDER["server"] = None
    try:
        await scheduler.stop()
    except Exception as e:
        print(f"停止调度器失败（忽略）：{e}")
    # 子进程清理放到最后，且给一个明确预算：超时就交给 _force_quit 兜底，
    # 不让 taskkill 的等待时间叠加到用户可感知的退出耗时上。
    process_manager.shutdown_all(budget=3.0)
    if db is not None:
        db.close()


if __name__ == "__main__":
    import threading
    import time

    # DPI 感知必须在这里声明：pywebview 创建窗口之后再调就无效了，
    # 而它决定了 create_window 的尺寸会不会被系统二次缩放。
    _ensure_dpi_aware()

    stop_event = threading.Event()

    wait_pid = os.environ.pop("LOVOMO_WAIT_PID", "")
    if wait_pid:
        try:
            target_pid = int(wait_pid)
            print(f"等待旧进程({target_pid})退出后启动…")
            while _pid_alive(target_pid):
                time.sleep(0.3)
        except Exception:
            pass

    # 单实例判定放在等待之后：「重启 Lovomo」是旧进程退出、新进程才启动，
    # 那时名额已经释放，不会被自己的上一世挡在门外。
    if not _acquire_single_instance():
        if _signal_existing_instance():
            print("Lovomo 已在运行，已把它的窗口唤到前台。")
        else:
            print("Lovomo 已在运行，未重复启动。")
        sys.exit(0)

    config = ConfigLoader()

    def run_backend():
        try:
            # 记下主事件循环，供插件从同步钩子里回调发消息用
            globals()["MAIN_EVENT_LOOP"] = asyncio.new_event_loop()
            asyncio.set_event_loop(globals()["MAIN_EVENT_LOOP"])
            globals()["MAIN_EVENT_LOOP"].run_until_complete(main(stop_event))
        except Exception as e:
            print(f"后台服务异常: {e}")
            import traceback
            traceback.print_exc()
            try:
                with open(runtime_path("backend_error.log"), "a", encoding="utf-8") as f:
                    f.write(f"{time.ctime()} - 异常: {e}\n")
                    traceback.print_exc(file=f)
            except Exception:
                pass

    backend_thread = threading.Thread(target=run_backend, daemon=True)
    backend_thread.start()
    time.sleep(1)
    webui_port = int(config.get("webui_port", 11500))

    holder = {"window": None}
    close_state = {"hidden": False, "quitting": False}
    tray_state = {"icon": None}
    # 唤醒（托盘/任务栏点开）期间暂停几何记录：这段窗口连续做
    # show/maximize/move/resize，事件回调读到的都是过渡态。
    _geometry_paused = [0]

    def _wake_geometry_guard(seconds: float = 1.5):
        """唤醒期间给几何记录加一个静默期，避免把过渡态写进记忆。"""
        _geometry_paused[0] += 1

        def _release():
            time.sleep(max(0.0, seconds))
            _geometry_paused[0] = max(0, _geometry_paused[0] - 1)

        threading.Thread(target=_release, daemon=True).start()

    def open_console():
        """托盘/任务栏「打开 Lovomo」：把窗口唤醒到前台，并保持上次的几何。

        以前这里调 w.restore()，pywebview 会把「最大化时记录的全屏矩形」
        按 DPI 缩放当成 Normal 矩形还原，窗口就跑到右下角去了。现在改成
        读自己的几何记忆：该最大化就最大化，否则 move+resize 回原位。

        另外 pywebview 的 show() 只做 Show+Activate，在 Windows 前台锁下会被
        忽略 —— 表现就是「放到后台后点任务栏/托盘没反应」。

        顺序上刻意把「抢前台」放在最后一步：_apply_saved_geometry 里的
        maximize/move/resize 都会重新排布窗口并可能把前台交还给系统，
        只有把它放在几何之后，最后一次抢前台的结果才会被保留下来。
        """
        w = holder["window"]
        if w is None:
            return
        close_state["hidden"] = False
        _wake_geometry_guard()
        _raise_to_foreground(w)
        try:
            _apply_saved_geometry(w)
        except Exception:
            try:
                w.restore()
            except Exception:
                pass
        # 几何应用后窗口可能被系统重新排列、甚至重新落到后台，
        # 这里必须再抢一次前台，并以这次的结果为准。
        _raise_to_foreground(w)
        # 前台锁在个别时序下会连吞两次调用（例如从托盘还原时焦点还在
        # 弹出菜单上）。补一次延迟重试，等菜单收起、系统空闲下来再抢一次，
        # 这样「点开窗口仍压在别的窗口后面」的情况就基本消失了。
        def _retry(win=w):
            try:
                time.sleep(0.12)
                # 这一拍里用户可能又把窗口收进托盘了，那就别再把它拉出来。
                if close_state.get("hidden"):
                    return
                _raise_to_foreground(win)
            except Exception:
                pass
        threading.Thread(target=_retry, daemon=True).start()

    def stop_tray_icon():
        icon = tray_state.get("icon")
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass

    def request_quit():
        """真正的退出入口：任何"关掉程序"的路径都收敛到这里。

        以前 tray 的「退出 Lovomo」直接调 quit_app()，而 quit_app 在销毁窗口时
        又会触发一次 on_closing，两条路径各做一半清理，容易出现"清理跑了两遍
        但都没跑完"的观感。现在统一：先立起 quitting 标记（on_closing 见到它
        就直接放行、不再取消关闭），再走唯一的 quit_app()。
        """
        close_state["quitting"] = True
        quit_app()

    def _force_quit(delay: float = 1.2):
        """硬退出兜底：到点直接 os._exit，不让任何一处阻塞拖住退出。

        delay 是「留给优雅清理的时间」。清理本身已经改得快了，这里从原来的
        2.5~3 秒压到 1.2 秒，用户几乎感觉不到等待。
        """
        def _run():
            time.sleep(max(0.0, delay))
            try:
                # 先立起"正在退出"：此后任何一处都不许再拉起新的子进程，
                # 否则会留下"父进程已经没了、子进程还在跑"的独立进程。
                mark_exiting()
                process_manager.shutdown_all(budget=1.0)
            except Exception:
                pass
            os._exit(0)
        threading.Thread(target=_run, daemon=True).start()

    def _spawn_detached(args, env):
        """无窗口地拉起子进程。

        Windows 上从 GUI 进程 spawn python.exe（控制台版解释器）一定会弹一个
        黑框；所以这里即使退回到 python.exe，也统一加 CREATE_NO_WINDOW +
        SW_HIDE 双保险，保证退出/重启时不再闪出 CMD 窗口。
        """
        import subprocess
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = (subprocess.CREATE_NO_WINDOW
                                       | getattr(subprocess, "DETACHED_PROCESS", 0))
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0  # SW_HIDE
            kwargs["startupinfo"] = si
        subprocess.Popen(args, env=env, close_fds=True, **kwargs)

    def _resolve_spawn_exe() -> tuple:
        """挑一个不会弹控制台窗口的解释器来重启。

        优先 pythonw.exe（无控制台子系统）；找不到才退回 python.exe，
        但那时 _spawn_detached 会补上 CREATE_NO_WINDOW，同样不会闪窗。
        冻结成 exe 后直接用自身。
        """
        if getattr(sys, "frozen", False):
            return sys.executable, []
        exe = sys.executable
        if os.name == "nt":
            for name in ("pythonw.exe", "pythonw"):
                cand = Path(exe).with_name(name)
                if cand.exists():
                    exe = str(cand)
                    break
        args = [exe, str(Path(sys.argv[0]).resolve())]
        return args[0], args[1:]

    def relaunch_console():
        close_state["quitting"] = True
        stop_tray_icon()
        w = holder["window"]
        if w is not None:
            _capture_geometry(w)
            _finalize_geometry(w)
            _finish_geometry_save()
            try:
                w.destroy()
            except Exception:
                pass
        try:
            exe, extra = _resolve_spawn_exe()
            env = dict(os.environ)
            env["LOVOMO_WAIT_PID"] = str(os.getpid())
            _spawn_detached([exe, *extra], env)
            print("已安排重启：新实例将等待本进程完全退出后启动，避免端口占用/重复进程。")
        except Exception as e:
            print(f"重启 Lovomo 控制台失败: {e}")
        stop_event.set()
        _force_quit(1.5)

    def quit_app():
        # 立起 quitting：此后 on_closing 一律放行，destroy 才能真正关掉窗口。
        close_state["quitting"] = True
        stop_event.set()
        # 先启动硬退出计时：托盘线程里调用窗口销毁可能阻塞数秒，
        # 计时器必须先跑起来，退出耗时才不受销毁速度影响。
        _force_quit(1.2)
        stop_tray_icon()
        # 先把窗口过程还原再销毁窗口，避免退出过程中回调悬空触发异常
        try:
            _uninstall_taskbar_activate_hook()
        except Exception:
            pass
        w = holder["window"]
        if w is not None:
            _capture_geometry(w)
            _finalize_geometry(w)
            _finish_geometry_save()
            try:
                w.destroy()
            except Exception:
                pass

    def try_start_tray() -> bool:
        if tray_state.get("icon") is not None:
            return True
        try:
            import pystray
            from PIL import Image
            icon_file = next((p for p in _candidate_icon_paths()), None)
            if icon_file is not None:
                img = Image.open(str(icon_file)).convert("RGBA").resize((64, 64))
            else:
                img = Image.new("RGBA", (64, 64), (30, 136, 229, 255))
                try:
                    from PIL import ImageDraw
                    ImageDraw.Draw(img).text((8, 22), "Lovomo", fill="white")
                except Exception:
                    pass

            icon = pystray.Icon(
                "lovomo", img, "Lovomo",
                menu=pystray.Menu(
                    pystray.MenuItem("打开 Lovomo", lambda i, item: open_console(),
                                     default=True),
                    pystray.MenuItem("重启 Lovomo", lambda i, item: relaunch_console()),
                    pystray.MenuItem("退出 Lovomo", lambda i, item: request_quit()),
                ),
            )
            tray_state["icon"] = icon
            threading.Thread(target=icon.run, daemon=True).start()
            return True
        except Exception as e:
            print(f"系统托盘不可用（如需托盘请安装依赖后重启：pip install pystray Pillow）：{e}")
            return False

    tray_ok = try_start_tray()

    def _hide_to_tray(w) -> None:
        """把窗口收进系统托盘：从屏幕和任务栏上一起消失，只留托盘图标。

        隐藏动作必须延后一拍，不能在 on_closing 里直接做：WinForms 的关闭
        序列会把在 FormClosing 期间改的窗口状态覆盖回去，表现出来就是
        「偶尔没关干净、屏幕上还留一层窗口」。
        """
        if not tray_ok:
            # 没有托盘图标就藏不得：窗口既不在屏幕上也不在任务栏里，用户就
            # 再也叫不回来了。退化成最小化，至少留一个任务栏入口。
            close_state["hidden"] = False
        else:
            close_state["hidden"] = True

        def _do():
            if tray_ok:
                # 藏完还要校验一次：隐藏可能被系统动画或并发的唤醒动作吞掉，
                # 没藏住就再补一次。
                for _ in range(2):
                    try:
                        w.hide()
                    except Exception:
                        pass
                    time.sleep(0.05)
                    if not _window_visible(w):
                        break
            else:
                try:
                    w.minimize()
                except Exception:
                    pass
            try:
                _finish_geometry_save()
            except Exception:
                pass

        threading.Thread(target=_do, daemon=True).start()

    def on_closing():
        """窗口关闭事件的唯一入口。

        这里必须能区分「用户主动要关掉程序」和「窗口被别人关了一下」——
        两种情况在 on_closing 里长得一模一样，只能靠状态区分：

        · 已经在退出流程里（close_state["quitting"]，由 request_quit 立起，
          或 quit_app/relaunch_console 自己 destroy 窗口触发）：直接返回 None
          放行，让关闭真的发生。**这里返回 False 是非常危险的** ——
          pywebview 的 Event.set() 只要收到一个 False 就判定取消关闭，
          于是"销毁窗口"变成"什么都没发生"，进程卡住不退出。
        · 用户点右上角 ×：按产品约定不是退出，而是收进系统托盘。
          返回 False 取消这次关闭，再交给 _hide_to_tray 把窗口藏起来。

        退出本身一律走 request_quit()（托盘菜单）或 quit_app()，它们在
        destroy 之前会先把 quitting 立起来，所以不会在这里被拦下。
        """
        if close_state.get("quitting"):
            return None
        w = holder["window"]
        # 窗口一旦隐藏/销毁就读不到几何了，先抓一次再动手。
        if w is not None:
            _capture_geometry(w)
            _finalize_geometry(w)
            _hide_to_tray(w)
        return False

    def _finalize_geometry(w) -> None:
        """退出前的最终校准，再落盘。

        只做一件事：把「当前是不是最大化」记准。Normal 矩形一律不在这里读，
        理由见 _capture_geometry 的注释 —— 最大化退出时 rcNormalPosition 给的
        是全屏矩形，读它只会污染记忆。
        """
        if w is None:
            return
        try:
            cmd = _show_state(_window_hwnd(w))
            if cmd == 3:
                _WINDOW_GEOMETRY["maximized"] = True
            elif cmd == 1:
                # 真正处于 Normal 状态，此时读到的矩形才是可信的 Normal
                rect = _window_rect(w)
                if rect and not _rect_is_degenerate(rect):
                    _WINDOW_GEOMETRY["maximized"] = False
                    _WINDOW_GEOMETRY["normal"] = rect
            # cmd == 2（最小化退出）：什么都不改，保留之前记下的值
        except Exception:
            pass

    def _capture_geometry(w, force: bool = False) -> None:
        """把窗口当前的位置/大小/最大化状态记到内存（落盘由定时器去抖）。

        核心原则：**Normal 矩形只在窗口真的处于 Normal 状态时才记录。**
        最大化/最小化时一律只更新 maximized 标记，不碰 normal 字段。

        这条原则来之不易，踩过两个坑：
        · 用 pywebview 的 window.state 判断最大化 —— 它返回 State(dict)，
          str() 是 "{}"，判断永远为 False，于是最大化被记成了 Normal，
          存下 2582x1390 这种全屏尺寸（150% 缩放下超出 2560 屏幕宽）。
        · 改用 GetWindowPlacement 的 rcNormalPosition 想「最大化时也能拿到
          Normal 矩形」—— 实测发现：窗口以 maximized=True 创建时，
          Windows 根本没设置 rcNormalPosition，它返回的就是全屏矩形
          （实测请求 2048x1152 却读到 2586x1466 = 屏幕+边框）。
          照着记同样会污染。

        所以现在的策略很朴素：只有 Normal 状态下读到的 GetWindowRect 才可信。
        用户从 Normal 切到最大化时，normal 字段保持上一次 Normal 时的值不变
        —— 这正是我们想要的行为。
        """
        if w is None:
            return
        if not _window_visible(w):
            # 窗口已经收进托盘了：隐藏窗口的 GetWindowPlacement 不报告正常状态，
            # 这时读到的几何没有意义（最大化过的窗口会读到全屏尺寸），
            # 记下来只会把记忆弄脏。
            return
        try:
            state = _window_state_name(w)
            if not state:
                # 原生拿不到 HWND（极早期/异常后端）就退回 pywebview 属性
                return _capture_geometry_fallback(w)
            if state == "maximized":
                _WINDOW_GEOMETRY["maximized"] = True
            elif state == "minimized":
                # 最小化不动任何几何：GetWindowRect 是 (-32000,-32000)，
                # rcNormalPosition 又不可靠，保留旧值最安全。
                pass
            else:
                rect = _window_rect(w)
                if rect and not _rect_is_degenerate(rect):
                    _WINDOW_GEOMETRY["maximized"] = False
                    _WINDOW_GEOMETRY["normal"] = rect
        except Exception:
            return
        _schedule_geometry_save(0.4 if force else 1.2)

    def _capture_geometry_fallback(w) -> None:
        """pywebview 属性兜底路径（拿不到 HWND 时才会走到，例如非 Windows）。

        pywebview 读出来的是逻辑像素，统一乘 scale 转成物理像素再存，
        保证落盘的口径始终一致。

        这里**不能**用 w.state 判断最大化：pywebview 的 state 是 State(dict)，
        str() 是 "{}"，永远不等于 "maximized"，会把最大化误判成 Normal、
        把全屏尺寸存下来。拿不到原生 HWND 时就保守处理：只更新 Normal 矩形，
        不动 maximized 标记（保留上次的结论），宁可不更新也不写错。
        """
        try:
            scale = _window_scale(w)
            x = getattr(w, "x", None)
            y = getattr(w, "y", None)
            width = getattr(w, "width", None)
            height = getattr(w, "height", None)
            if not width or not height:
                return
            rect = {
                "x": _logical_to_phys(x, scale) if x is not None else None,
                "y": _logical_to_phys(y, scale) if y is not None else None,
                "width": _logical_to_phys(width, scale),
                "height": _logical_to_phys(height, scale),
            }
            if _rect_is_degenerate(rect):
                return
            _WINDOW_GEOMETRY["normal"] = rect
        except Exception:
            return
        _schedule_geometry_save(1.2)

    def _bind_geometry_events(w) -> None:
        """监听窗口变化，持续更新几何记忆。

        最大化/还原这些事件在 winforms 后端上不一定带尺寸，而且还原动画期间
        GetWindowRect 读到的是中间态，所以统一延迟一点再读；读的是
        rcNormalPosition，不受动画影响。

        启动阶段（窗口还没定型）的事件一律忽略：那时候读到的往往是
        MinimumSize 或者动画中间态，记下来就是把垃圾写进记忆。

        唤醒期间（托盘/任务栏点开）同样忽略：open_console 会连续做
        show/maximize/move/resize，这些动作触发的事件读到的是过渡态，
        照记会把"最大化时的全屏矩形"当成 Normal 存下来，下次启动位置就漂了。
        """
        # 启动后 5 秒内不记录。窗口初始化 + 最大化动画 + 页面首屏都在这段
        # 时间里发生，几何值还没稳定。
        started = time.time()
        startup_grace = 5.0

        def _later(*_args, **_kwargs):
            time.sleep(0.2)
            if time.time() - started < startup_grace:
                return
            if _geometry_paused[0] > 0:
                return
            _capture_geometry(w)

        for evt_name in ("resized", "moved", "maximized", "restored", "shown"):
            try:
                getattr(w.events, evt_name).__iadd__(_later)
            except Exception:
                pass

    def run_webview_loop():
        import webview
        geom = _load_window_geometry()
        normal = _sanitize_normal_geometry(geom.get("normal") or {})
        scale = _window_scale()
        to_logical = _phys_to_logical
        # ---- 尺寸计算全程在「逻辑像素」域里做，最后才交给 create_window ----
        # 混用物理/逻辑是这个 bug 的老毛病，这里刻意把两个域的边界划清楚：
        # 屏幕尺寸是物理的，先换算成逻辑上限；候选尺寸也是逻辑的，直接比。
        _fb = _default_normal_geometry()
        start_w = to_logical(int(normal.get("width") or _fb["width"]), scale)
        start_h = to_logical(int(normal.get("height") or _fb["height"]), scale)
        start_x = normal.get("x")
        start_y = normal.get("y")

        sw, sh = _primary_screen_size()
        if sw > 0 and sh > 0:
            # 窗口再大也不该超过屏幕的 92%（留出标题栏和任务栏的余量）
            max_w = to_logical(int(sw * 0.92), scale)
            max_h = to_logical(int(sh * 0.92), scale)
            start_w = max(400, min(start_w, max_w))
            start_h = max(300, min(start_h, max_h))
        else:
            start_w = max(400, start_w)
            start_h = max(300, start_h)

        kwargs = {
            "width": start_w, "height": start_h,
            "resizable": True, "maximized": bool(geom.get("maximized")),
        }
        # 只在记忆的坐标仍落在当前屏幕内时才传给 create_window；
        # 否则交给系统居中，避免窗口开在屏幕外看不见。
        if (not geom.get("maximized") and start_x is not None
                and start_y is not None and _is_geometry_valid(normal)):
            kwargs["x"] = to_logical(int(start_x), scale)
            kwargs["y"] = to_logical(int(start_y), scale)
        holder["window"] = webview.create_window(
            'Lovomo',
            f'http://127.0.0.1:{webui_port}',
            **kwargs)
        w = holder["window"]
        _WEBVIEW_WINDOW_HOLDER["window"] = w
        try:
            w.events.closing += on_closing
        except Exception as e:
            print(f"绑定窗口关闭事件失败，关闭将直接退出: {e}")
        _bind_geometry_events(w)

        def _wake_from_system():
            """窗口被系统激活（点任务栏图标 / 从任务栏还原）时补一次抢前台。

            已经收进托盘时什么都不做 —— 那时窗口本该是隐藏的，把它拉出来
            就成了"关掉之后又冒出一层窗口"。
            """
            if close_state.get("hidden"):
                return
            open_console()

        def _install_foreground_hook(*_args, **_kwargs):
            """窗口首次显示后装任务栏唤醒钩子。

            放在 shown 之后是因为此刻 HWND 才真正有效（create_window 返回时
            WinForms 窗体可能还没建好原生句柄）。装钩子本身很轻，失败也只是
            退化成"任务栏还原不抢前台"，不影响程序其余部分。
            """
            def _do():
                time.sleep(0.3)
                ok = _install_taskbar_activate_hook(
                    w, on_activate=lambda win: _wake_from_system())
                if ok:
                    print("[窗口] 已启用任务栏唤醒置顶")
            threading.Thread(target=_do, daemon=True).start()

        try:
            w.events.shown += _install_foreground_hook
        except Exception as e:
            print(f"绑定窗口显示事件失败（任务栏唤醒钩子未安装）: {e}")
        # 窗口和 open_console 都就绪了，开始监听"又有人双击了 exe"。
        _start_instance_show_waiter(open_console)
        # 这里刻意不做启动后再 apply 几何的操作。
        #
        # 曾经加过一个 events.loaded + Timer(0.6) 的兜底，想把历史脏几何纠正
        # 回来，结果制造了更严重的 bug：定时器在窗口还没初始化完成时触发，
        # 此时 GetWindowRect 拿到的是 MinimumSize(200x100)，_apply_saved_geometry
        # 就照着 200x100 去 resize，窗口被压成一个 200x100 的小方块，玩家还会

        #
        # 正确策略：几何只在 create_window 时决定一次（上面已经算好了正确值），
        # 之后只「观察」不「干预」。历史脏数据由 _load_window_geometry 的版本
        # 校验和 _sanitize_normal_geometry 负责清理，不需要事后补救。
        webview.start(private_mode=False,
                      storage_path=_webview_profile_dir() or None)
        icon = tray_state.get("icon")
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass

    try:
        if HAS_WEBVIEW:
            run_webview_loop()
        else:
            print("程序正在运行，按 Ctrl+C 退出...")
            while not stop_event.is_set():
                time.sleep(1)
    except KeyboardInterrupt:
        print("\n收到键盘中断，准备退出...")
    finally:
        print("正在停止后台服务...")
        stop_event.set()
        mark_exiting()
        backend_thread.join(timeout=1.5)
        process_manager.shutdown_all(budget=1.5)
        print("程序已完全退出。")
