# -*- coding: utf-8 -*-
# 消息媒体处理（自 main.py 搬迁）：图片缓存/待识图状态/头像缓存、语音转写、
# OB11 段渲染、转发/引用拉取、表情自动收藏。
# 管理器槽位（sticker_mgr/memory_manager/global_config/sender/mood_mgr）统一经
# modules.app_context 读写；_PENDING_IMAGES 与 _image_cache_last_check 是本模块
# 自有的可变状态，随模块走。
import asyncio
import hashlib
import io
import os
import re
import shutil
import subprocess
import time
import wave
from pathlib import Path
from typing import Optional, Dict
from urllib.parse import urlsplit

import httpx

from modules import app_context
from modules.reply_pipeline import _spawn, _sticker_judgement_from_llm, _norm_text
from modules.llm_helpers import RoleContext, download_image, sniff_image_mime
from modules.tls import verified_context
from modules.asr import AUDIO_MIMES
from modules.tts_service import no_window_kwargs


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
        # 分类判定为"不适合当表情包"时直接跳过：以前这种情况会被兜底塞进 wuyu 目录，
        # 等于把无关图片污染表情库。这条规则写在收藏指令里，不再单独配置。
        if not category:
            print("【表情收藏】这张图未被判定为适合当表情包，跳过收藏。")
            return
        print(f"【表情收藏-自动触发】图片来源: {str(source)[:50]}... 原始分数: {score}, "
              f"分类: {category or '(空→兜底分类)'}, "
              f"图片字节: {'复用识图已读入的' if image_data else '需重新下载'}")
        await auto_capture_image(ctx, app_context.sticker_mgr, source, category, image_data=image_data,
                                 reason=reason)
    except Exception as e:
        print(f"表情收藏失败: {type(e).__name__}: {e}")


def _sticker_capture_args(reply: Optional[dict], image_sources: list) -> Optional[dict]:
    """识图结果里的收藏判定能否落盘：能则返回 auto_capture_from_images 的入参，不能则 None。

    条件集中在这里：收藏触发同时挂在「正常回复」与「审判判不回」两条路径上，
    判定写两份迟早会漏掉一处（漏掉的那条路径会静默不收藏）。
    """
    capture = (reply or {}).get("capture") or {}
    if not capture.get("should") or not image_sources or app_context.sticker_mgr is None:
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


def pick_media_source(seg, kind: str = "图片") -> Optional[str]:
    """从图片/语音消息段里挑一个可用的来源。

    优先级：本地缓存文件 > 网络 URL > 原始 file 标识。
    最后一项是"裸文件名"（如 1E4D5FAB.image，文件并不在本地也不是 URL）：
    此时不能返回 None，否则调用方既拿不到文件、日志里也看不出是哪一个出了问题；
    保留原值可以让后续 download/日志明确指出问题来源，再由调用方自行跳过。
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
        print(f"{kind}来源无法解析（本地文件不存在且无 URL），保留原值便于排查: {str(raw_id)[:120]}")
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
# 让 OneBot 把用户语音转成 wav：本地 ASR 脚本与线上接口都吃这个格式
VOICE_OUT_FORMAT = "wav"
VOICE_FALLBACK_SUFFIX = ".wav"
# 识别脚本要的采样率，转码（含 silk 解码）一律出 16k 单声道 wav
VOICE_SAMPLE_RATE = 16000


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


def _image_cache_dir():
    """聊天记录真实图片的缓存目录（data/image_cache，按内容哈希去重存放）。"""
    return app_context.memory_manager.data_path / "image_cache"


_IMAGE_CACHE_CHECK_INTERVAL = 60.0
_image_cache_last_check = 0.0


def _cache_image_bytes(data: bytes, ext: str = ".jpg") -> str:
    """图片字节存进缓存（内容哈希命名），返回文件名；失败返回空串。

    聊天记录里只存文件名：缓存被清理或图片丢失时，记录页自动退回文字描述。
    超过 image_cache_max_mb 后按最旧先删（检查按间隔做，避免每条消息全目录扫描）。
    """
    global _image_cache_last_check
    if app_context.memory_manager is None or not data:
        return ""
    try:
        cache_dir = _image_cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        name = f"{hashlib.sha1(data).hexdigest()[:16]}{ext}"
        target = cache_dir / name
        if not target.exists():
            target.write_bytes(data)
        now = time.time()
        if now - _image_cache_last_check > _IMAGE_CACHE_CHECK_INTERVAL:
            _image_cache_last_check = now
            try:
                limit_mb = max(1, int(app_context.active_config().get("image_cache_max_mb", 500) or 500))
            except (TypeError, ValueError):
                limit_mb = 500
            files = [f for f in cache_dir.iterdir() if f.is_file()]
            files.sort(key=lambda f: f.stat().st_mtime)
            total = sum(f.stat().st_size for f in files)
            for f in files:
                if total <= limit_mb * 1024 * 1024:
                    break
                try:
                    total -= f.stat().st_size
                    f.unlink(missing_ok=True)
                except OSError:
                    pass
        return name
    except Exception as e:
        print(f"图片缓存写入失败: {type(e).__name__}: {e}")
        return ""


async def _attach_history_image(history: list, source) -> None:
    """把收到的真实图片放进缓存，并在最近一条用户消息上记下缓存文件名。

    内容里的 [图片: 描述] 保持原样：缓存清理或图片丢失时自动退回文字描述。
    """
    if app_context.memory_manager is None or not source:
        return
    src_text = str(source or "")
    data = b""
    if os.path.isfile(src_text):
        try:
            data = Path(src_text).read_bytes()
        except OSError:
            data = b""
    else:
        try:
            data = await download_image(src_text)
        except Exception:
            data = b""
    mime = sniff_image_mime(data or b"")
    if not mime:
        return
    name = _cache_image_bytes(data, _IMAGE_EXT.get(mime, ".jpg"))
    if not name:
        return
    for msg in reversed(history):
        if msg.get("role") != "user":
            continue
        msg["image"] = name
        return


_AVATAR_CACHE_TTL = 7 * 86400
_AVATAR_CACHE_MAX_FILES = 300
# QQ 号头像：qlogo 按号码直接返回，不需要登录态
_QQ_AVATAR_URL = "https://q1.qlogo.cn/g?b=qq&nk={qq}&s=100"
_QQ_NUMBER_RE = re.compile(r"^\d{5,12}$")


def _avatar_cache_dir():
    """聊天记录头像的缓存目录（data/avatar_cache，按 QQ 号存放）。"""
    return app_context.memory_manager.data_path / "avatar_cache"


def _prune_avatar_cache(cache_dir):
    try:
        files = [f for f in cache_dir.iterdir() if f.is_file()]
    except OSError:
        return
    if len(files) <= _AVATAR_CACHE_MAX_FILES:
        return
    files.sort(key=lambda f: f.stat().st_mtime)
    for f in files[:len(files) - _AVATAR_CACHE_MAX_FILES]:
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass


def _active_bot_qq() -> str:
    """当前接入方式登录的机器人 QQ 号（拿不到或不是 QQ 号时返回空串）。"""
    if app_context.sender is None:
        return ""
    self_id = str(getattr(app_context.sender._active_client(), "self_id", "") or "")
    return self_id if _QQ_NUMBER_RE.match(self_id) else ""


async def qq_avatar_bytes(user_id) -> bytes:
    """取用户头像字节（目前只有 QQ 号能直接取），带本地缓存。

    微信 openid 这类 ID 取不到真实头像，返回空字节由前端退回名字首字。
    """
    uid = str(user_id or "").strip()
    if app_context.memory_manager is None or not _QQ_NUMBER_RE.match(uid):
        return b""
    path = _avatar_cache_dir() / f"qq_{uid}.img"
    try:
        if path.is_file() and time.time() - path.stat().st_mtime < _AVATAR_CACHE_TTL:
            data = path.read_bytes()
            if sniff_image_mime(data):
                return data
        data = await download_image(_QQ_AVATAR_URL.format(qq=uid))
        mime = sniff_image_mime(data or b"")
        if mime:
            cache_dir = _avatar_cache_dir()
            cache_dir.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            _prune_avatar_cache(cache_dir)
            return data
    except Exception as e:
        print(f"取 {uid} 的头像失败（改用名字首字）: {type(e).__name__}: {e}")
    return b""


def _diary_recognition_note(ctx, user_text: str) -> str:
    """对方把角色自己写的日记（或其中一段）贴回来时，让角色认出这是自己的文字。

    日记只发出去、角色自己仍以为那是私密的东西；但用户把原文贴回来时角色
    "毫无印象"就很出戏。按归一化文本的包含关系在最近若干篇日记里找命中。
    """
    if app_context.mood_mgr is None or not str(user_text or "").strip():
        return ""
    norm_user = _norm_text(user_text)
    if len(norm_user) < 8:
        return ""
    try:
        diary = app_context.mood_mgr.get_diary(limit=30) or {}
    except Exception:
        return ""
    for entry in (diary.get(ctx.character_key) or []):
        if not isinstance(entry, dict):
            continue
        norm = _norm_text(str(entry.get("text") or ""))
        if not norm:
            continue
        shorter, longer = ((norm_user, norm) if len(norm_user) <= len(norm)
                           else (norm, norm_user))
        if len(shorter) >= 8 and shorter in longer:
            return ("【你自己的日记】对方发来的这段话，是你自己之前写下的心情日记"
                    "——你记得写过它，一眼就能认出来。回应时你清楚这是你自己的文字："
                    "不要当成对方写的、不要装作第一次见到，也不要否认是你写的；"
                    "可以害羞、得意或调侃，但那本就是你想说却没好意思说出口的话。")
    return ""


async def _download_image_to_cache(url: str) -> Optional[str]:
    if app_context.memory_manager is None:
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
        target = app_context.memory_manager.data_path / f"temp_img_{time.time()}{_IMAGE_EXT.get(mime, '.jpg')}"
        target.write_bytes(data)
        return str(target)
    except Exception as e:
        print(f"缓存图片失败: {type(e).__name__}: {e}")
        return None


async def _download_audio_to_temp(url: str) -> Optional[str]:
    """把语音直链下载到 data 目录下，返回本地路径（失败返回 None）。"""
    if app_context.memory_manager is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True, trust_env=False,
                                     verify=verified_context()) as client:
            resp = await client.get(str(url))
            resp.raise_for_status()
            data = resp.content
    except Exception as e:
        print(f"下载语音失败: {type(e).__name__}: {e}")
        return None
    suffix = Path(urlsplit(str(url)).path).suffix.lower()
    if suffix not in AUDIO_MIMES:
        suffix = VOICE_FALLBACK_SUFFIX
    try:
        target = app_context.memory_manager.data_path / f"temp_voice_{time.time()}{suffix}"
        target.write_bytes(data)
        return str(target)
    except Exception as e:
        print(f"缓存语音失败: {type(e).__name__}: {e}")
        return None


def _find_ffmpeg() -> str:
    """找 ffmpeg：优先 GPT-SoVITS 自带的（识别脚本那套 runtime 里有），其次 PATH。"""
    try:
        from modules.asr import resolve_sovits_root
        root = resolve_sovits_root(app_context.active_config() or {})
        if root is not None:
            bundled = root / "runtime" / "ffmpeg.exe"
            if bundled.exists():
                return str(bundled)
            bundled = root / "ffmpeg.exe"
            if bundled.exists():
                return str(bundled)
    except Exception:
        pass
    return shutil.which("ffmpeg") or ""


def _decode_silk_to_wav(path: str) -> str:
    """把腾讯 silk 解成 16k 单声道 wav。

    silk 是腾讯私有格式，ffmpeg 没有它的解码器，只能靠 pysilk（SILK SDK 的
    绑定）解；解出来的采样率由 pysilk 自己认，传入的值只决定输出重采样到多少。
    """
    import pysilk

    raw = Path(path).read_bytes()
    # 微信/QQ 的语音在 SILK 头前多带一个 0x02 标记，解之前要去掉
    if raw[:1] == b"\x02":
        raw = raw[1:]
    pcm = io.BytesIO()
    pysilk.decode(io.BytesIO(raw), pcm, VOICE_SAMPLE_RATE)
    target = str(Path(path).with_name(f"asr_{time.time_ns()}.wav"))
    with wave.open(target, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(VOICE_SAMPLE_RATE)
        wav.writeframes(pcm.getvalue())
    return target


def _convert_audio_to_wav(path: str) -> str:
    """把识别脚本啃不动的语音格式（silk/amr/mp3…）转成 16k 单声道 wav。

    转换失败时原样返回原路径：识别那边至少还能试一次，错误信息也更真实。
    """
    suffix = Path(path).suffix.lower()
    if suffix == ".wav":
        return path
    if suffix == ".silk":
        try:
            return _decode_silk_to_wav(path)
        except ImportError:
            print("语音转码跳过：silk 需要 pysilk 才能解（当前环境没有装），"
                  "这条语音识别不了")
            return path
        except Exception as e:
            print(f"语音转码失败: {type(e).__name__}: {e}")
            return path
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        print(f"语音转码跳过：找不到 ffmpeg（{suffix} 可能识别不了，"
              "可在 GPT-SoVITS 的 runtime 目录放一个 ffmpeg.exe）")
        return path
    target = str(Path(path).with_name(f"asr_{time.time_ns()}.wav"))
    try:
        proc = subprocess.run(
            [ffmpeg, "-y", "-loglevel", "error", "-i", path,
             "-ar", str(VOICE_SAMPLE_RATE), "-ac", "1", target],
            capture_output=True, text=True, timeout=60, **no_window_kwargs())
        if proc.returncode == 0 and os.path.isfile(target) \
                and Path(target).stat().st_size > 0:
            return target
        print(f"语音转码失败: {str(proc.stderr or '')[:200]}")
    except Exception as e:
        print(f"语音转码失败: {type(e).__name__}: {e}")
    return path


async def transcribe_voice_message(client, sources: list, file_ids: dict) -> str:
    """用户发来的语音转文字，取「语音识别」那几项配置（识别不出来返回空串）。

    QQ 里的语音常是 silk/amr 这类识别脚本啃不动的格式，先让 OneBot 转成 wav
    并落到本地；转不了就退回直接下载直链。微信 ClawBot 没有转码接口，
    语音按原始格式落盘后在这里统一转成 wav（silk 走 pysilk，其余走 ffmpeg）
    再交给识别。
    多段语音只取第一段识别出文字的。
    """
    from modules.asr import transcribe_file
    getter = getattr(client, "get_record", None)
    temp_files = []
    try:
        for src in sources:
            path = str(src) if os.path.isfile(str(src)) else ""
            if not path:
                url = str(src)
                if getter is not None:
                    try:
                        got = await getter(file=str((file_ids or {}).get(str(src)) or src),
                                           out_format=VOICE_OUT_FORMAT)
                        if isinstance(got, dict):
                            local = str(got.get("file") or "")
                            if local and os.path.isfile(local):
                                path = local
                            else:
                                url = str(got.get("url") or url)
                    except Exception as e:
                        print(f"语音转码失败（改用直链）: {type(e).__name__}: {e}")
                if not path and url.startswith(("http://", "https://")):
                    path = await _download_audio_to_temp(url) or ""
                    if path:
                        temp_files.append(path)
            if not path:
                continue
            try:
                wav = await asyncio.to_thread(_convert_audio_to_wav, path)
                if wav != path:
                    temp_files.append(wav)
                text = await transcribe_file(app_context.active_config(), wav)
            except Exception as e:
                print(f"语音识别失败: {type(e).__name__}: {e}")
                continue
            if text:
                return text
        return ""
    finally:
        for item in temp_files:
            try:
                Path(item).unlink(missing_ok=True)
            except OSError:
                pass


def _render_ob11_segments(segments) -> dict:
    """把 OneBot 形态的消息段渲染成纯文本（图片/语音/表情只留占位符）。

    返回 {"text", "image_urls", "image_file_ids"}。引用消息与转发的聊天记录
    拿到的是同一种段列表，渲染规则只需要一份。
    """
    parts = []
    image_urls = []
    image_file_ids = {}
    for s in (segments or []):
        seg_type = s.get("type") if isinstance(s, dict) else getattr(s, "_type", None)
        data = (s.get("data") or {}) if isinstance(s, dict) else s
        if seg_type == "text":
            t = (data.get("text", "") if isinstance(data, dict) else getattr(s, "text", "")) or ""
            if str(t).strip():
                parts.append(str(t).strip())
        elif seg_type == "image":
            parts.append("[图片]")
            url = (data.get("url") if isinstance(data, dict) else getattr(s, "url", "")) or ""
            if str(url).startswith(("http://", "https://")) and str(url) not in image_urls:
                image_urls.append(str(url))
                fid = data.get("file") if isinstance(data, dict) else getattr(s, "file", None)
                if fid:
                    image_file_ids[str(url)] = str(fid)
        elif seg_type == "record":
            parts.append("[语音]")
        elif seg_type == "face":
            parts.append("[表情]")
        elif seg_type == "at":
            qq = data.get("qq", "") if isinstance(data, dict) else getattr(s, "qq", "")
            parts.append(f"[@{qq}]" if qq else "[@]")
        elif seg_type == "forward":
            parts.append("[聊天记录]")
    return {"text": " ".join(p for p in parts if p).strip(),
            "image_urls": image_urls, "image_file_ids": image_file_ids}


# 转发的聊天记录可能很长：只取前若干条、总字数也封顶，免得挤掉整段上下文
FORWARD_MAX_NODES = 20
FORWARD_MAX_CHARS = 1500
# 转发里的图片同样交给识图，但一张图就是一次识图调用，张数必须封顶
FORWARD_MAX_IMAGES = 4


async def fetch_forward_text(client, forward_ids) -> Optional[dict]:
    """回查合并转发消息的内容。

    转发过来的聊天记录在消息里只有一个 id，正文得再问一次服务端才拿得到。
    取不到就返回 None，这条消息按原样继续走。
    """
    getter = getattr(client, "get_forward_msg", None)
    if getter is None:
        return None
    lines = []
    image_urls = []
    image_file_ids = {}
    for fid in forward_ids or []:
        try:
            resp = await getter(message_id=fid)
        except Exception as e:
            print(f"获取转发消息失败: {type(e).__name__}: {e}")
            continue
        nodes = resp.get("messages") if isinstance(resp, dict) else None
        for node in (nodes or [])[:FORWARD_MAX_NODES]:
            if not isinstance(node, dict):
                continue
            sender = node.get("sender") or {}
            who = str(sender.get("card") or sender.get("nickname") or "") or "某人"
            rendered = _render_ob11_segments(node.get("message") or [])
            lines.append(f"{who}: {rendered['text'] or '[非文本消息]'}")
            for url in rendered["image_urls"]:
                if len(image_urls) >= FORWARD_MAX_IMAGES:
                    break
                if url not in image_urls:
                    image_urls.append(url)
                    fid2 = rendered["image_file_ids"].get(url)
                    if fid2:
                        image_file_ids[url] = fid2
    text = "\n".join(lines).strip()
    if not text and not image_urls:
        return None
    if len(text) > FORWARD_MAX_CHARS:
        text = text[:FORWARD_MAX_CHARS] + "……"
    return {"text": text, "image_urls": image_urls, "image_file_ids": image_file_ids}


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
    rendered = _render_ob11_segments(resp.get("message") or [])
    text = rendered["text"]
    quoted_image_urls = rendered["image_urls"]
    if not who and not text and not quoted_image_urls:
        return None
    return {"who": who, "text": text or "[非文本消息]",
            "user_id": str(sender.get("user_id", "") or ""),
            "message_id": str(resp.get("message_id") or getattr(reply_seg, "id", "") or ""),
            "image_urls": quoted_image_urls,
            "image_file_ids": rendered["image_file_ids"]}


