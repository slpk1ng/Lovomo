# -*- coding: utf-8 -*-
"""设备状态插件：QQ 里发「/截图」「/截屏」回整屏截图（可选带一条本机状态），
发「/录屏 秒数」按时间间隔连拍一段时间，把这一批截图合成动图或视频发回来。

状态里的内存、CPU 占用率、电池走 ctypes 读 Windows 系统接口；
图片和视频发送复用主程序已连上的 NapCat 客户端（插件桥接只提供文本和语音）。
"""

import asyncio
import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from fractions import Fraction
from pathlib import Path

COMMANDS = ("截图", "截屏")
RECORD_COMMAND = "录屏"
SETTINGS_FILE = "settings.json"
# 功能页的「录音设备」下拉从这份文件读（清单里用 options_file 指过来）
AUDIO_OPTIONS_FILE = "audio_devices.json"
# webui.html 读的状态快照（每次截图/录屏后刷新）
STATUS_FILE = "status.json"
WEBUI_SHOTS_LIMIT = 12
SHOTS_DIRNAME = "shots"
TMP_DIRNAME = "tmp"
SHOT_PREFIX = "shot_"
FRAME_PREFIX = "rec_"
STAMP_FORMAT = "%Y%m%d_%H%M%S_%f"
PNG_SUFFIX = ".png"
GIF_SUFFIX = ".gif"
MP4_SUFFIX = ".mp4"
GIF_FORMAT = "gif"
MP4_FORMAT = "mp4"
RECORD_FORMATS = (GIF_FORMAT, MP4_FORMAT)
FFMPEG_NAME = "ffmpeg"
FFMPEG_LOGLEVEL = "error"
FFMPEG_RATE_LIMIT = 1000
H264_CODEC = "libx264"
H264_PRESET = "veryfast"
H264_CRF = "23"
H264_PIX_FMT = "yuv420p"
EVEN_DIMENSIONS_FILTER = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
GIF_LOOP_FOREVER = 0
GIF_MIN_FRAME_MS = 20
# 录屏直接走 ffmpeg 的 gdigrab 抓桌面，不再一张张截图拼
GDIGRAB_INPUT_TYPE = "gdigrab"
GDIGRAB_INPUT = "desktop"
DSHOW_INPUT_TYPE = "dshow"
AUDIO_CODEC = "aac"
AUDIO_BITRATE = "128k"
# GIF 是给聊天窗口看的：限帧率 + 限宽度，否则整屏原分辨率动图又大又慢
GIF_MAX_FPS = 12
GIF_MAX_WIDTH = 1280
# 录屏比合成长，ffmpeg 的超时要跟着秒数走
FFMPEG_RECORD_EXTRA_SECONDS = 60
FFMPEG_DEVICE_LIST_TIMEOUT_SECONDS = 20
# 录「系统正在放的声音」要回环设备（立体声混音 / 虚拟声卡），各机名字不同，按关键字认
LOOPBACK_HINTS = ("立体声混音", "stereo mix", "what u hear",
                  "virtual-audio-capturer", "cable output")
# ffmpeg 是控制台程序，主程序是无窗口的 GUI，不带上这个标志会闪黑框
CREATE_NO_WINDOW = 0x08000000

DEFAULT_SETTINGS = {
    "reply_status": True,
    "keep_shots": 10,
    "capture_interval": 1.0,
    "record_fps": 15,
    "record_audio": True,
    "record_audio_device": "",
    "record_format": GIF_FORMAT,
}
MIN_KEEP_SHOTS = 1
MAX_KEEP_SHOTS = 200
MIN_CAPTURE_INTERVAL = 0.01
MAX_CAPTURE_INTERVAL = 60.0
MIN_RECORD_FPS = 5
MAX_RECORD_FPS = 30
MAX_BURST_SHOTS = 60
MAX_RECORD_FRAMES = 300
MAX_RECORD_SECONDS = 300
RECORD_USAGE = "用法：/录屏 秒数，比如 /录屏 10。"
BUSY_REPLY = "上一次截图或录屏还没结束，稍等一下再发。"
SHOT_FAILED_REPLY = "截图失败，这次没有发出去。"
SEND_FAILED_NOTE = "（截图发送失败，本次只发状态）"
COMPOSE_FAILED_REPLY = "截图合成动图/视频失败，这次没有发出去。"

BYTE_STEP = 1024
BYTE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")
CPU_SAMPLE_SECONDS = 0.1
SEND_TIMEOUT_SECONDS = 20.0
MEDIA_SEND_TIMEOUT_SECONDS = 60.0
FFMPEG_TIMEOUT_SECONDS = 180
IS_WINDOWS = platform.system() == "Windows"
BATTERY_FLAG_NO_SYSTEM = 128
BATTERY_PERCENT_UNKNOWN = 255
AC_LINE_ONLINE = 1

_settings_cache = {"data": None, "mtime": object()}
_session_state = {"lock": threading.Lock(), "busy": False}
_ffmpeg_state = {"path": None, "checked": False}


class _FileTime(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_ulong),
                ("dwHighDateTime", ctypes.c_ulong)]


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


class _PowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte),
                ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong),
                ("BatteryFullLifeTime", ctypes.c_ulong)]


def _human_size(num):
    size = float(num)
    for unit in BYTE_UNITS[:-1]:
        if size < BYTE_STEP:
            return f"{size:.1f} {unit}"
        size /= BYTE_STEP
    return f"{size:.1f} {BYTE_UNITS[-1]}"


def _kernel32():
    try:
        return ctypes.windll.kernel32
    except (AttributeError, OSError):
        return None


def _filetime_value(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def _cpu_line():
    cores = os.cpu_count()
    head = f"CPU：{cores} 个逻辑核心" if cores else "CPU：未知"
    if not IS_WINDOWS:
        return head
    k32 = _kernel32()
    if k32 is None:
        return head

    def sample():
        idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
        ok = k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel),
                                ctypes.byref(user))
        if not ok:
            return None
        return (_filetime_value(idle), _filetime_value(kernel),
                _filetime_value(user))

    first = sample()
    if first is None:
        return head
    time.sleep(CPU_SAMPLE_SECONDS)
    second = sample()
    if second is None:
        return head
    total = (second[1] - first[1]) + (second[2] - first[2])
    if total <= 0:
        return head
    busy = total - (second[0] - first[0])
    percent = max(0.0, min(100.0, busy / total * 100))
    return f"{head}，当前占用 {percent:.1f}%"


def _memory_line():
    if not IS_WINDOWS:
        return None
    k32 = _kernel32()
    if k32 is None:
        return None
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(_MemoryStatus)
    if not k32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    total = status.ullTotalPhys
    used = total - status.ullAvailPhys
    percent = (used / total * 100) if total else 0.0
    return (f"内存：共 {_human_size(total)}，已用 {_human_size(used)}"
            f"（{percent:.1f}%），可用 {_human_size(status.ullAvailPhys)}")


def _battery_line():
    if not IS_WINDOWS:
        return None
    k32 = _kernel32()
    if k32 is None:
        return None
    status = _PowerStatus()
    if not k32.GetSystemPowerStatus(ctypes.byref(status)):
        return None
    if status.BatteryFlag == BATTERY_FLAG_NO_SYSTEM:
        return None
    power = "外接电源" if status.ACLineStatus == AC_LINE_ONLINE else "电池供电"
    if status.BatteryLifePercent == BATTERY_PERCENT_UNKNOWN:
        return f"电源：{power}，电量未知"
    return f"电源：{power}，电量 {status.BatteryLifePercent}%"


def _disk_line(path):
    try:
        usage = shutil.disk_usage(path)
    except OSError as e:
        return f"磁盘 {path}：读取失败（{e}）"
    percent = (usage.used / usage.total * 100) if usage.total else 0.0
    return (f"磁盘 {path}：共 {_human_size(usage.total)}，"
            f"已用 {_human_size(usage.used)}（{percent:.1f}%），"
            f"剩余 {_human_size(usage.free)}")


def _device_status():
    lines = [
        "【设备状态】",
        f"主机名：{platform.node() or '未知'}",
        f"系统：{platform.system() or '未知'} {platform.release()}".rstrip(),
        f"架构：{platform.machine() or '未知'}",
        _cpu_line(),
        _memory_line(),
        _battery_line(),
        _disk_line(os.path.abspath(os.sep)),
        f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
    ]
    return "\n".join(line for line in lines if line)


def _settings_path(ctx):
    return ctx.data_dir() / SETTINGS_FILE


def _load_settings(ctx):
    """读插件设置。主程序只在功能页提交时写这个文件，这里只读并按修改时间缓存。"""
    path = _settings_path(ctx)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = None
    if isinstance(_settings_cache["data"], dict) and _settings_cache["mtime"] == mtime:
        return _settings_cache["data"]
    data = dict(DEFAULT_SETTINGS)
    try:
        if mtime is not None:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update({k: v for k, v in raw.items() if v is not None})
    except Exception as e:
        ctx.log("读取设置失败，使用默认值:", e)
    _settings_cache["data"] = data
    _settings_cache["mtime"] = mtime
    return data


def _as_float(value, fallback):
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _clamp(value, low, high):
    return max(low, min(high, value))


def _reply_status(settings):
    value = settings.get("reply_status")
    return DEFAULT_SETTINGS["reply_status"] if value is None else bool(value)


def _keep_count(settings):
    value = _as_float(settings.get("keep_shots"), DEFAULT_SETTINGS["keep_shots"])
    return int(_clamp(value, MIN_KEEP_SHOTS, MAX_KEEP_SHOTS))


def _capture_interval(settings):
    value = _as_float(settings.get("capture_interval"),
                      DEFAULT_SETTINGS["capture_interval"])
    return _clamp(value, MIN_CAPTURE_INTERVAL, MAX_CAPTURE_INTERVAL)


def _record_format(settings):
    value = str(settings.get("record_format") or "").strip().lower()
    return value if value in RECORD_FORMATS else DEFAULT_SETTINGS["record_format"]


def _record_fps(settings):
    value = _as_float(settings.get("record_fps"), DEFAULT_SETTINGS["record_fps"])
    return int(_clamp(value, MIN_RECORD_FPS, MAX_RECORD_FPS))


def _record_audio(settings):
    value = settings.get("record_audio")
    return DEFAULT_SETTINGS["record_audio"] if value is None else bool(value)


def _gif_filter(fps):
    """录屏转 GIF 的滤镜：限帧率、限宽度，再生成调色板。"""
    rate = min(GIF_MAX_FPS, fps)
    return (f"fps={rate},scale=w='min({GIF_MAX_WIDTH},iw)':h=-2:flags=lanczos,"
            "split[a][b];[a]palettegen[p];[b][p]paletteuse")


def _positive_int(args):
    """把指令参数解析成正整数；不是数字或不是正数时返回 None。"""
    text = str(args or "").strip()
    if not text:
        return None
    try:
        value = int(float(text))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _number_text(value):
    return f"{value:g}"


def _shots_dir(ctx):
    d = ctx.data_dir() / SHOTS_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _tmp_dir(ctx):
    d = ctx.data_dir() / TMP_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _capture(ctx, target):
    """把整屏截到指定路径，成功返回路径，失败返回 None。"""
    try:
        from PIL import ImageGrab
    except ImportError as e:
        ctx.log("缺少 Pillow，无法截图:", e)
        return None
    try:
        shot = ImageGrab.grab(all_screens=True) if IS_WINDOWS else ImageGrab.grab()
        shot.save(target, format="PNG")
        return target
    except Exception as e:
        ctx.log("截图失败:", e)
        return None


def _prune_shots(ctx, keep):
    """只留最新的 keep 个截图（含录屏帧），更早的删掉。"""
    d = _shots_dir(ctx)
    entries = []
    try:
        for path in d.iterdir():
            try:
                if path.is_file():
                    entries.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except OSError as e:
        ctx.log("读取截图目录失败:", e)
        return 0
    entries.sort()
    removed = 0
    for _, path in entries[:max(0, len(entries) - keep)]:
        try:
            path.unlink()
            removed += 1
        except OSError as e:
            ctx.log("删除旧截图失败:", e)
    return removed


def _remove_file(path):
    try:
        Path(path).unlink()
        return True
    except OSError:
        return False


def _napcat_client():
    """主程序在每条消息事件上都会把当前连接的客户端记到 __main__.napcat_client。"""
    main = sys.modules.get("__main__")
    return getattr(main, "napcat_client", None) if main is not None else None


def _run_on_main_loop(coro_factory, ctx, timeout=SEND_TIMEOUT_SECONDS):
    """插件的钩子是同步函数，主循环在前端线程里跑，这里做跨线程投递。"""
    async def _do():
        try:
            await coro_factory()
            return True
        except Exception as e:
            ctx.log("异步操作失败:", e)
            return False

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        # 已经在循环线程里，只能交给任务调度（run_until_complete 会死锁）
        loop.create_task(_do())
        return True
    main = sys.modules.get("__main__")
    main_loop = getattr(main, "MAIN_EVENT_LOOP", None) if main is not None else None
    if main_loop is None or main_loop.is_closed():
        ctx.log("事件循环尚未就绪，文件未发送")
        return False
    try:
        future = asyncio.run_coroutine_threadsafe(_do(), main_loop)
        return bool(future.result(timeout=timeout))
    except Exception as e:
        ctx.log("文件发送超时或失败:", e)
        return False


def _media_segment(path):
    """mp4 走视频消息段，png / gif 走图片消息段。"""
    from napcat import Image, Video
    target = str(Path(path).resolve())
    if Path(path).suffix.lower() == MP4_SUFFIX:
        return Video(file=target)
    return Image(file=target)


def _send_media(payload, path, ctx):
    client = _napcat_client()
    if client is None:
        ctx.log("拿不到 NapCat 客户端，文件未发送")
        return False
    session_type = str((payload or {}).get("session_type") or "group")
    try:
        target_id = int((payload or {}).get("target_id"))
    except (TypeError, ValueError):
        ctx.log("会话标识无效，文件未发送:", (payload or {}).get("target_id"))
        return False
    try:
        message = [_media_segment(path)]
    except Exception as e:
        ctx.log("构造消息段失败:", e)
        return False

    async def _do():
        if session_type == "private":
            await client.send_private_msg(user_id=target_id, message=message)
        else:
            await client.send_group_msg(group_id=target_id, message=message)

    return _run_on_main_loop(_do, ctx, timeout=MEDIA_SEND_TIMEOUT_SECONDS)


def _send_notice(ctx, payload, text):
    session_type = str((payload or {}).get("session_type") or "group")
    target_id = (payload or {}).get("target_id")
    if target_id is None:
        return False
    return ctx.send_text(session_type, target_id, text)


def _send_status(ctx, payload, settings):
    if not _reply_status(settings):
        return False
    return _send_notice(ctx, payload, _device_status())


def _recent_shots(ctx):
    """截图目录里最新的若干张文件名（新到旧），供 webui 展示。"""
    d = _shots_dir(ctx)
    try:
        entries = [p for p in d.iterdir() if p.is_file()]
    except OSError as e:
        ctx.log("读取截图目录失败:", e)
        return []
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in entries[:WEBUI_SHOTS_LIMIT]]


def _write_status_snapshot(ctx, status_text=None):
    """把最近截图与状态文本写成 data/status.json，供 webui.html 读取。

    status_text 为空（比如关掉了「回复设备状态」）时沿用文件里已有的那份，
    免得一次没有状态的操作就把 webui 上的状态清空。
    """
    target = ctx.data_dir() / STATUS_FILE
    text = status_text
    if not text and target.is_file():
        try:
            text = str(json.loads(target.read_text(encoding="utf-8")).get("status") or "")
        except Exception:
            text = ""
    payload = {
        "updated": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
        "status": text or "",
        "shots": _recent_shots(ctx),
    }
    try:
        tmp = Path(str(target) + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(str(tmp), str(target))
    except Exception as e:
        ctx.log("写入状态快照失败:", e)


def _begin_session():
    with _session_state["lock"]:
        if _session_state["busy"]:
            return False
        _session_state["busy"] = True
        return True


def _end_session():
    with _session_state["lock"]:
        _session_state["busy"] = False


def _ffmpeg_exe():
    """找可用的 ffmpeg：插件环境自带的优先，其次系统 PATH 里的。"""
    if _ffmpeg_state["checked"]:
        return _ffmpeg_state["path"]
    _ffmpeg_state["checked"] = True
    try:
        import imageio_ffmpeg
        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and Path(bundled).is_file():
            _ffmpeg_state["path"] = bundled
            return bundled
    except Exception:
        pass
    _ffmpeg_state["path"] = shutil.which(FFMPEG_NAME)
    return _ffmpeg_state["path"]


def _compose_gif(ctx, frames, out, interval):
    try:
        from PIL import Image
        images = []
        for path in frames:
            with Image.open(path) as frame:
                images.append(frame.convert("RGB"))
        if not images:
            return None
        images[0].save(out, format="GIF", save_all=True,
                       append_images=images[1:], loop=GIF_LOOP_FOREVER,
                       duration=max(GIF_MIN_FRAME_MS,
                                    int(round(interval * 1000))))
        return out if out.is_file() else None
    except Exception as e:
        ctx.log("合成 GIF 失败:", e)
        return None


def _compose_mp4(ctx, pattern, out, interval):
    exe = _ffmpeg_exe()
    if not exe:
        return None
    rate = Fraction(1.0 / interval).limit_denominator(FFMPEG_RATE_LIMIT)
    command = [exe, "-y", "-loglevel", FFMPEG_LOGLEVEL,
               "-start_number", "1",
               "-framerate", f"{rate.numerator}/{rate.denominator}",
               "-i", str(pattern),
               "-c:v", H264_CODEC, "-preset", H264_PRESET, "-crf", H264_CRF,
               "-pix_fmt", H264_PIX_FMT, "-vf", EVEN_DIMENSIONS_FILTER,
               str(out)]
    flags = CREATE_NO_WINDOW if IS_WINDOWS else 0
    try:
        done = subprocess.run(command, capture_output=True,
                              timeout=FFMPEG_TIMEOUT_SECONDS,
                              creationflags=flags)
    except (OSError, subprocess.SubprocessError) as e:
        ctx.log("调用 ffmpeg 失败:", e)
        return None
    if done.returncode != 0 or not out.is_file():
        ctx.log("ffmpeg 合成失败:", done.stderr.decode("utf-8", "replace")[-300:])
        return None
    return out


def _compose_clip(ctx, frames, pattern, session, interval, settings):
    """把这一批录屏帧合成一个文件，返回路径；两种格式都不行时返回 None。"""
    out_dir = _tmp_dir(ctx)
    if _record_format(settings) == MP4_FORMAT:
        clip = _compose_mp4(ctx, pattern,
                            out_dir / f"{FRAME_PREFIX}{session}{MP4_SUFFIX}",
                            interval)
        if clip is not None:
            return clip
        ctx.log("没有可用的 ffmpeg，本次改用 GIF")
    return _compose_gif(ctx, frames,
                        out_dir / f"{FRAME_PREFIX}{session}{GIF_SUFFIX}",
                        interval)


def _list_audio_devices(ctx):
    """列出可用的录音设备名（dshow 把设备清单打在 stderr 上，退出码是错的，忽略）。"""
    exe = _ffmpeg_exe()
    if not exe:
        return []
    command = [exe, "-hide_banner", "-list_devices", "true",
               "-f", DSHOW_INPUT_TYPE, "-i", "dummy"]
    flags = CREATE_NO_WINDOW if IS_WINDOWS else 0
    try:
        done = subprocess.run(command, capture_output=True,
                              timeout=FFMPEG_DEVICE_LIST_TIMEOUT_SECONDS,
                              creationflags=flags)
    except (OSError, subprocess.SubprocessError) as e:
        ctx.log("列出录音设备失败:", e)
        return []
    names = []
    for line in done.stderr.decode("utf-8", "replace").splitlines():
        if "(audio)" not in line or '"' not in line:
            continue
        name = line.split('"')[1].strip()
        if name and name not in names:
            names.append(name)
    return names


def _write_audio_options(ctx, devices, wanted):
    """把可用录音设备写进 data/audio_devices.json，功能页的下拉读它渲染。"""
    options = [{"value": "", "label": "自动（优先能录系统声音的设备）"}]
    seen = {""}
    for name in devices:
        if name not in seen:
            options.append({"value": name, "label": name})
            seen.add(name)
    if wanted and wanted not in seen:
        # 当前配置的设备这次没枚举到也留着，否则保存时会被下拉的取值校验丢掉
        options.append({"value": wanted, "label": f"{wanted}（当前未检测到）"})
    target = ctx.data_dir() / AUDIO_OPTIONS_FILE
    try:
        tmp = Path(str(target) + ".tmp")
        tmp.write_text(json.dumps({"options": options}, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(str(tmp), str(target))
    except Exception as e:
        ctx.log("写入录音设备列表失败:", e)


def _refresh_audio_options(ctx, settings=None):
    """枚举一次录音设备：写好下拉选项，返回本次要用的设备名（没有就 None）。

    配置里填了名字就按子串匹配，留空优先回环设备（能录到系统声音的那种），
    再退回第一个。挑不中时日志里会列出全部可用设备名。
    """
    devices = _list_audio_devices(ctx)
    wanted = str((settings or {}).get("record_audio_device") or "").strip()
    _write_audio_options(ctx, devices, wanted)
    if not devices:
        ctx.log("没有可用的录音设备，本次只录画面")
        return None
    if wanted:
        for name in devices:
            if wanted.lower() in name.lower():
                return name
        ctx.log(f"没找到录音设备「{wanted}」，可用的是：{'、'.join(devices)}")
        return None
    for name in devices:
        if any(hint in name.lower() for hint in LOOPBACK_HINTS):
            return name
    return devices[0]


def _run_ffmpeg(ctx, command, timeout):
    flags = CREATE_NO_WINDOW if IS_WINDOWS else 0
    try:
        done = subprocess.run(command, capture_output=True, timeout=timeout,
                              creationflags=flags)
    except (OSError, subprocess.SubprocessError) as e:
        ctx.log("调用 ffmpeg 失败:", e)
        return False
    if done.returncode != 0:
        ctx.log("ffmpeg 执行失败:", done.stderr.decode("utf-8", "replace")[-300:])
        return False
    return True


def _record_command(exe, seconds, fps, gif, device, out):
    """拼录屏命令；GIF 没有音轨，device 会被忽略。"""
    command = [exe, "-y", "-loglevel", FFMPEG_LOGLEVEL]
    if device and not gif:
        # 录音设备要放在录屏输入之前：dshow 打开慢，排在后面会挤掉开头几秒的画面
        command += ["-f", DSHOW_INPUT_TYPE, "-i", f"audio={device}"]
    command += ["-f", GDIGRAB_INPUT_TYPE, "-framerate", str(fps),
                "-t", str(seconds), "-i", GDIGRAB_INPUT]
    if gif:
        command += ["-vf", _gif_filter(fps), "-loop", str(GIF_LOOP_FOREVER)]
    else:
        command += ["-vf", EVEN_DIMENSIONS_FILTER, "-c:v", H264_CODEC,
                    "-preset", H264_PRESET, "-crf", H264_CRF,
                    "-pix_fmt", H264_PIX_FMT]
        if device:
            command += ["-c:a", AUDIO_CODEC, "-b:a", AUDIO_BITRATE]
        # 实时音频输入没有结尾，必须再给输出侧一个 -t，否则 ffmpeg 不会自己停
        command += ["-t", str(seconds)]
    command.append(str(out))
    return command


def _record_with_ffmpeg(ctx, seconds, fps, settings):
    """用 ffmpeg 的 gdigrab 录屏，返回产物路径；ffmpeg 不可用或失败时返回 None。"""
    exe = _ffmpeg_exe()
    if not exe:
        return None
    gif = _record_format(settings) == GIF_FORMAT
    device = None
    if not gif and _record_audio(settings):
        device = _refresh_audio_options(ctx, settings)
    out_dir = _tmp_dir(ctx)
    session = f"{datetime.now():{STAMP_FORMAT}}"
    out = out_dir / f"{FRAME_PREFIX}{session}{GIF_SUFFIX if gif else MP4_SUFFIX}"
    timeout = seconds + FFMPEG_RECORD_EXTRA_SECONDS
    if device:
        ctx.log(f"录屏同时录音：{device}")
    if _run_ffmpeg(ctx, _record_command(exe, seconds, fps, gif, device, out),
                   timeout) and out.is_file():
        return out
    if device:
        ctx.log("录音失败，这次只录画面")
        if _run_ffmpeg(ctx, _record_command(exe, seconds, fps, gif, None, out),
                       timeout) and out.is_file():
            return out
    return None


def _run_burst(ctx, payload, count, interval, settings):
    """后台线程：每隔 interval 秒截一张发一张，全部结束后才清理旧截图。"""
    sent = 0
    failed = False
    try:
        for index in range(count):
            if index:
                time.sleep(interval)
            name = f"{SHOT_PREFIX}{datetime.now():{STAMP_FORMAT}}{PNG_SUFFIX}"
            path = _capture(ctx, _shots_dir(ctx) / name)
            if path is None or not _send_media(payload, path, ctx):
                failed = True
                break
            sent += 1
    finally:
        _prune_shots(ctx, _keep_count(settings))
        if failed:
            _send_notice(ctx, payload,
                         SEND_FAILED_NOTE if sent else SHOT_FAILED_REPLY)
        _write_status_snapshot(ctx, _device_status())
        _send_status(ctx, payload, settings)
        _end_session()


def _playback_interval(stamps, fallback):
    """按实测采集时间算每帧时长，让产物播放时长等于真实录制时长。

    截图本身要花时间（整屏 2560×1440 一张约 0.2~0.3 秒），「截图时间间隔」
    设得再小也做不到，所以每帧真实间隔只能靠实测，不能直接用设置值。
    """
    if len(stamps) >= 2:
        elapsed = stamps[-1] - stamps[0]
        if elapsed > 0:
            return elapsed / len(stamps)
    return fallback


def _record_by_shots(ctx, payload, seconds, interval, settings):
    """录屏不可用时的兜底：连拍到 seconds 秒为止，再合成动图/视频发出去。"""
    session = f"{datetime.now():{STAMP_FORMAT}}"
    shots_dir = _shots_dir(ctx)
    pattern = shots_dir / f"{FRAME_PREFIX}{session}_%04d{PNG_SUFFIX}"
    captured = []
    stamps = []
    deadline = time.monotonic() + seconds
    while len(captured) < MAX_RECORD_FRAMES:
        name = f"{FRAME_PREFIX}{session}_{len(captured) + 1:04d}{PNG_SUFFIX}"
        path = _capture(ctx, shots_dir / name)
        if path is None:
            break
        captured.append(path)
        stamps.append(time.monotonic())
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
    if len(captured) >= MAX_RECORD_FRAMES:
        ctx.log(f"录屏帧数达到上限 {MAX_RECORD_FRAMES} 张，提前结束")
    if not captured:
        _send_notice(ctx, payload, SHOT_FAILED_REPLY)
        return
    clip = _compose_clip(ctx, captured, pattern, session,
                         _playback_interval(stamps, interval), settings)
    if clip is None:
        _send_notice(ctx, payload, COMPOSE_FAILED_REPLY)
    elif not _send_media(payload, clip, ctx):
        _send_notice(ctx, payload, SEND_FAILED_NOTE)
    else:
        _remove_file(clip)


def _run_record(ctx, payload, seconds, interval, settings):
    """后台线程：录一段发出去；能真录屏就真录屏，录不了才退回连拍合成。"""
    try:
        clip = _record_with_ffmpeg(ctx, seconds, _record_fps(settings), settings)
        if clip is None:
            ctx.log("录屏不可用，本次退回连拍合成")
            _record_by_shots(ctx, payload, seconds, interval, settings)
        elif not _send_media(payload, clip, ctx):
            _send_notice(ctx, payload, SEND_FAILED_NOTE)
        else:
            _remove_file(clip)
    finally:
        _prune_shots(ctx, _keep_count(settings))
        _write_status_snapshot(ctx, _device_status())
        _send_status(ctx, payload, settings)
        _end_session()


def _handle_shot(ctx, name=None, args=None, event=None):
    """#截图 / #截屏：不带数字截一张，带数字就按间隔连拍那么多张。"""
    payload = event if isinstance(event, dict) else {}
    settings = _load_settings(ctx)
    interval = _capture_interval(settings)
    count = min(_positive_int(args) or 1, MAX_BURST_SHOTS)

    if count > 1:
        if not _begin_session():
            return BUSY_REPLY
        threading.Thread(
            target=_run_burst,
            args=(ctx, payload, count, interval, settings),
            daemon=True).start()
        return f"开始连拍：{count} 张，每 {_number_text(interval)} 秒一张。"

    shot_name = f"{SHOT_PREFIX}{datetime.now():{STAMP_FORMAT}}{PNG_SUFFIX}"
    path = _capture(ctx, _shots_dir(ctx) / shot_name)
    notes = []
    if path is None:
        notes.append(SHOT_FAILED_REPLY)
    elif not _send_media(payload, path, ctx):
        notes.append(SEND_FAILED_NOTE)
    _prune_shots(ctx, _keep_count(settings))
    status_text = _device_status() if _reply_status(settings) else ""
    _write_status_snapshot(ctx, status_text)
    return "\n".join([status_text] + notes) if status_text else "\n".join(notes)


def _handle_record(ctx, name=None, args=None, event=None):
    """#录屏 秒数：录这么多秒，合成动图或视频发回来。"""
    payload = event if isinstance(event, dict) else {}
    seconds = _positive_int(args)
    if seconds is None:
        return RECORD_USAGE
    settings = _load_settings(ctx)
    interval = _capture_interval(settings)
    seconds = min(seconds, MAX_RECORD_SECONDS)
    if not _begin_session():
        return BUSY_REPLY
    threading.Thread(
        target=_run_record,
        args=(ctx, payload, seconds, interval, settings),
        daemon=True).start()
    kind = "视频" if _record_format(settings) == MP4_FORMAT else "动图"
    note = "（含声音）" if (kind == "视频" and _record_audio(settings)) else ""
    return f"开始录屏：录 {seconds} 秒{note}，结束后合成{kind}发回来。"


def _startup_refresh(ctx):
    """加载后补一次录音设备列表与状态快照（要开 ffmpeg，放后台线程跑）。"""
    _refresh_audio_options(ctx, _load_settings(ctx))
    _write_status_snapshot(ctx, _device_status())


def on_load(ctx):
    for name in COMMANDS:
        ctx.register_command(name, _handle_shot)
    ctx.register_command(RECORD_COMMAND, _handle_record)
    ctx.log("设备状态插件已加载，可用指令：/截图、/截屏、/录屏")
    threading.Thread(target=_startup_refresh, args=(ctx,), daemon=True).start()
