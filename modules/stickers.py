import asyncio
import hashlib
import json
import os
import random
import re
import tempfile
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Optional

from .llm_helpers import RoleContext, resolve_emotion_key, sniff_image_mime

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_MIME_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
             "image/webp": ".webp", "image/bmp": ".bmp"}
MIME_BY_EXT = {ext: mime for mime, ext in _MIME_EXT.items()}
MIME_BY_EXT[".jpeg"] = "image/jpeg"

STRICT_ALLOWED = {"gaoxing", "shengqi", "haixiu", "wuyu", "jingya", "sajiao", "weixie", "pingjing"}

SCREEN_SHOT_ASPECT_MIN = 0.6
SCREEN_SHOT_ASPECT_MAX = 1.7

CATEGORY_GUIDE = {
    "gaoxing": "开心大笑、庆祝、起哄、搞笑沙雕、炫耀",
    "shengqi": "生气、不满、警告、凶人",
    "haixiu": "害羞、脸红、被夸不好意思",
    "wuyu": "无语、嫌弃、吐槽、敷衍、怼人",
    "jingya": "惊讶、震惊、没想到",
    "sajiao": "撒娇、卖萌、黏人、讨要",
    "weixie": "威胁、挑逗、撩拨、阴阳怪气",
    "pingjing": "平静的日常回应；只在确实找不到更贴切分类时才用",
}

CATEGORY_DISAMBIGUATION = (
    "区分要点："
    "① 阴森、恐怖、诡异、压抑、病态的画面绝不算开心/庆祝一类的正向情绪分类，"
    "它更接近挑逗/阴阳怪气或无语一类，"
    "若画面只是氛围压抑而无互动用途，应判定为不收藏；"
    "② 只有画面里真的存在开心/爆笑/炫耀的互动用途才选正向情绪分类；"
    "③ 单纯可爱只是基础条件，不足以选撒娇类，必须能用于撒娇讨要；"
    "④ 「睁大眼睛」「惊喜」这类单个神态词不足以选惊讶类。"
)


def sticker_root(config) -> Path:
    return Path(config.get("stickers_dir", "") or Path("data/stickers"))


_NON_CATEGORY_DIRS = {"any", "default"}


def _has_images(folder: Path) -> bool:
    try:
        return any(p.suffix.lower() in IMAGE_EXTS for p in folder.iterdir())
    except OSError:
        return False


def _category_dirs(root) -> dict:
    out = {}
    try:
        folders = list(Path(root).iterdir())
    except OSError:
        return out
    for folder in folders:
        if folder.is_dir() and folder.name.lower() not in _NON_CATEGORY_DIRS \
                and _has_images(folder):
            out[folder.name.lower()] = folder.name
    return out


def category_folders(root) -> list:
    return sorted(_category_dirs(root).values())


def category_candidates(config) -> list:
    return category_folders(sticker_root(config)) or sorted(STRICT_ALLOWED)


def category_candidates_text(config) -> str:
    return "\n".join(
        f"- {n}({CATEGORY_GUIDE[n]})" if n in CATEGORY_GUIDE else f"- {n}"
        for n in category_candidates(config))


DEFAULT_CAPTURE_PROMPT = (
    "附加收藏指令：如果你认为这张图片有趣、可爱、有梗或有纪念意义，"
    "请在输出完主要回复JSON之后，再单独输出一个JSON对象（不要放进sentences数组），"
    "格式：{\"sticker_capture\": true, \"category\": \"分类名\", "
    "\"reason\": \"通用用途名（6~12字，不含角色名）\"}。"
    "category 必须照抄【表情包收藏判定】里给出的分类清单中的名称，"
    "禁止自己新造分类，禁止填 default 或 any。"
    "不适合当表情包的图片一律不要收藏，直接输出 {\"sticker_capture\": false}。"
)

_COMMON_EMOTION_MAP = {
    # 高兴/搞笑/沙雕
    "高兴": "gaoxing", "开心": "gaoxing", "快乐": "gaoxing", "兴奋": "gaoxing", "喜悦": "gaoxing",
    "搞笑": "gaoxing", "沙雕": "gaoxing", "整蛊": "gaoxing", "搞怪": "gaoxing", "坏笑": "gaoxing", "滑稽": "gaoxing",
    "happy": "gaoxing", "joy": "gaoxing", "excited": "gaoxing",
    # 伤心
    "伤心": "shangxin", "难过": "shangxin", "悲伤": "shangxin", "哭泣": "shangxin", "委屈": "weiqu",
    "sad": "shangxin", "cry": "shangxin", "upset": "weiqu",
    # 生气/威胁
    "生气": "shengqi", "愤怒": "shengqi", "恼火": "shengqi", "暴躁": "shengqi",
    "威胁": "weixie", "恐吓": "weixie", "阴险": "weixie", "色气": "weixie", "猥琐": "weixie", "变态": "weixie",
    "angry": "shengqi", "threat": "weixie", "furious": "shengqi",
    # 惊讶
    "惊讶": "jingya", "吃惊": "jingya", "震惊": "jingya", "错愕": "jingya",
    "surprised": "jingya", "shocked": "jingya",
    # 无语
    "无语": "wuyu", "无奈": "wuyu", "翻白眼": "wuyu", "无语凝噎": "wuyu", "敷衍": "wuyu",
    "speechless": "wuyu", "helpless": "wuyu",
    # 害羞/撒娇/卖萌/调情
    "害羞": "haixiu", "娇羞": "haixiu", "脸红": "haixiu", 
    "撒娇": "sajiao", "调情": "sajiao", "挑逗": "sajiao", "撩": "sajiao", "发情": "sajiao", 
    "可爱": "sajiao", "卖萌": "sajiao", "好萌": "sajiao", "萌": "sajiao", "心动": "sajiao",
    "shy": "haixiu", "flirty": "sajiao", "teasing": "sajiao", "cute": "sajiao",
    # 害怕
    "害怕": "haipa", "恐惧": "haipa", "慌张": "huangzhang", "瑟瑟发抖": "haipa",
    "scared": "haipa", "terrified": "haipa", "panic": "huangzhang",
    "平静": "pingjing", "淡定": "pingjing", "冷漠": "lengdan", "严肃": "yansu",
    "疲惫": "pibei", "困": "pibei", "无聊": "wuliao", "犯困": "fankun",
}

_capture_state = {"date": "", "count": 0, "last": 0.0}

def normalize_capture_category(name) -> str:
    s = str(name or "").strip()
    if not s or s in (".", "..") or any(ch in s for ch in '/\\:*?"<>|'):
        return "wuyu"
    return s[:48] if len(s) > 48 else s

def _capture_quota_block(config, now: float) -> str:
    st = _capture_state
    today = time.strftime("%Y-%m-%d")
    if st["date"] != today:
        st["date"] = today
        st["count"] = 0
    try:
        interval = max(0.0, float(config.get("sticker_capture_min_interval", 300) or 0))
    except (TypeError, ValueError):
        interval = 300.0
    if interval > 0 and now - st["last"] < interval:
        wait = int(interval - (now - st["last"])) + 1
        return (f"距上次收藏不足最小间隔（还需 {wait} 秒；"
                f"sticker_capture_min_interval={interval:.0f}，设为 0 可关闭该限制）。")
    try:
        cap = max(0, int(config.get("sticker_capture_max_per_day", 20) or 0))
    except (TypeError, ValueError):
        cap = 20
    if cap > 0 and st["count"] >= cap:
        return f"今日收藏已达上限（{st['count']}/{cap}，sticker_capture_max_per_day）。"
    return ""

def _mark_capture(now: float):
    st = _capture_state
    if st["date"] != time.strftime("%Y-%m-%d"): st["date"] = time.strftime("%Y-%m-%d"); st["count"] = 0
    st["count"] += 1; st["last"] = now

def _ext_for_bytes(data: bytes, hint: str) -> str:
    mime = sniff_image_mime(data)
    if mime in _MIME_EXT: return _MIME_EXT[mime]
    ext = Path(str(hint or "")).suffix.lower()
    return ext if ext in IMAGE_EXTS else ""

def _index_path(root: Path) -> Path: return root / ".auto_index.json"

_REASON_NAME_MAX = 24
_REASON_SEP_RE = re.compile(r"[，。、；：！？…～〜·—―「」『』（）〔〕()\[\]【】“”‘’\s]+")
_NAME_ILLEGAL_CHARS = set('/\\:*?"<>|')

SEND_MODES = ("off", "random", "emotion", "description")


def safe_sticker_name(name) -> str:
    s = str(name or "").strip()
    if not s or s in (".", "..") or s != Path(s).name or (_NAME_ILLEGAL_CHARS & set(s)):
        return ""
    return s


def role_names_from_config(config) -> list:
    names = {str(config.get("character_name", "") or "").strip()}
    roles = config.get("roles")
    if isinstance(roles, list):
        for role in roles:
            if isinstance(role, dict):
                names.add(str(role.get("character_name", "") or "").strip())
    return [n for n in sorted(names, key=len, reverse=True) if len(n) >= 2]


def drop_role_names(text, names) -> str:
    out = str(text or "")
    for name in names or ():
        out = out.replace(name, "")
    return re.sub(r"\s{2,}", " ", out).strip()


def sticker_name_from_reason(reason: str, fallback: str = "") -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", str(reason or ""))
    name = _REASON_SEP_RE.sub(" ", name).strip().strip(". ")
    if len(name) > _REASON_NAME_MAX:
        name = name[:_REASON_NAME_MAX].rstrip()
    return name or fallback


def _unique_path(folder: Path, base: str, ext: str) -> Path:
    target = folder / f"{base}{ext}"
    if not target.exists():
        return target
    for i in range(2, 1000):
        candidate = folder / f"{base}-{i}{ext}"
        if not candidate.exists():
            return candidate
    return folder / f"{base}-{int(time.time() * 1000)}{ext}"


def _entry_path(entry) -> str:
    if isinstance(entry, dict):
        return str(entry.get("path") or "")
    return str(entry or "")


def _entry_reason(entry) -> str:
    return str(entry.get("reason") or "") if isinstance(entry, dict) else ""


def sticker_descriptions(root: Path) -> dict:
    index = _load_index(root)
    by_path = {}
    for entry in index.values():
        path = _entry_path(entry)
        if path:
            by_path[path] = _entry_reason(entry)
    out = {}
    try:
        folders = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        folders = []
    for folder in folders:
        guide = CATEGORY_GUIDE.get(folder.name.lower(), folder.name)
        for img in sorted(folder.iterdir()):
            if img.suffix.lower() not in IMAGE_EXTS:
                continue
            rel = f"{folder.name}/{img.name}"
            out[rel] = by_path.get(rel) or f"{guide}（{img.stem}）"
    return out

DEFAULT_PRESERVE_FORMATS = (".gif", ".webp")


def send_mode(config) -> str:
    mode = str(config.get("sticker_send_mode", "") or "").strip().lower()
    if mode in SEND_MODES:
        return mode
    return "emotion" if config.get("stickers_enabled", False) else "off"


def _preserve_formats(config) -> set:
    raw = config.get("sticker_capture_preserve_formats", None)
    if raw is None or raw == "":
        raw = DEFAULT_PRESERVE_FORMATS
    if isinstance(raw, str):
        parts = re.split(r"[,，\s;；|]+", raw)
    else:
        parts = list(raw or [])
    out = set()
    for item in parts:
        name = str(item).strip().lower()
        if not name:
            continue
        out.add(name if name.startswith(".") else f".{name}")
    return out


def _sticker_max_side(config) -> int:
    try:
        return max(0, int(config.get("sticker_capture_max_side", 400) or 0))
    except (TypeError, ValueError):
        return 400


def screen_shot_size(data: bytes):
    try:
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(data)) as img:
            width, height = img.size
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None
    ratio = width / height
    if ratio < SCREEN_SHOT_ASPECT_MIN or ratio > SCREEN_SHOT_ASPECT_MAX:
        return (width, height)
    return None


def _reencode_for_sticker(data: bytes, ext: str, config) -> tuple:
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(data))
        w, h = img.size
        max_side = _sticker_max_side(config)
        if max_side and (w > max_side or h > max_side):
            ratio = max_side / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGBA")
            buf = BytesIO()
            img.save(buf, format="PNG", optimize=True)
            return buf.getvalue(), ".png"
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True)
        return buf.getvalue(), ".jpg"
    except Exception as e:
        print(f"表情收藏重编码失败，保留原始字节: {type(e).__name__}: {e}")
        return data, ext


def output_max_side(config) -> int:
    try:
        return max(0, int(config.get("sticker_output_max_side", 400) or 0))
    except (TypeError, ValueError):
        return 400


def resize_for_output(path, config) -> tuple:
    src = Path(path)
    limit = output_max_side(config)
    if not limit or not src.exists():
        return src, None
    try:
        from io import BytesIO
        from PIL import Image
        with Image.open(src) as img:
            if getattr(img, "is_animated", False):
                return src, None
            w, h = img.size
            if w <= limit and h <= limit:
                return src, None
            fmt = (img.format or "").upper()
            ratio = limit / max(w, h)
            resized = img.resize((max(1, round(w * ratio)), max(1, round(h * ratio))),
                                 Image.LANCZOS)
            buf = BytesIO()
            if fmt == "JPEG":
                if resized.mode != "RGB":
                    resized = resized.convert("RGB")
                resized.save(buf, format="JPEG", quality=90, optimize=True)
                suffix = ".jpg"
            elif fmt == "WEBP":
                if resized.mode not in ("RGB", "RGBA"):
                    resized = resized.convert("RGBA")
                resized.save(buf, format="WEBP", quality=90)
                suffix = ".webp"
            else:
                if resized.mode not in ("RGB", "RGBA", "P", "L"):
                    resized = resized.convert("RGBA")
                resized.save(buf, format="PNG", optimize=True)
                suffix = ".png"
    except Exception as e:
        print(f"表情包发送缩放失败，按原图发送: {type(e).__name__}: {e}")
        return src, None
    try:
        fd, name = tempfile.mkstemp(prefix="lovomo_sticker_", suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(buf.getvalue())
    except OSError as e:
        print(f"表情包缩放结果写入失败，按原图发送: {type(e).__name__}: {e}")
        return src, None
    temp = Path(name)
    return temp, temp


def _load_index(root: Path) -> dict:
    from .jsonio import load_json_ex
    data, _ = load_json_ex(_index_path(root), {})
    return data if isinstance(data, dict) else {}

def _save_index(root: Path, index: dict) -> bool:
    try:
        from .jsonio import save_json
        save_json(_index_path(root), index)
        return True
    except Exception as e:
        print(f"[表情收藏] 保存索引失败: {type(e).__name__}: {e}")
        return False

def _prune_index(root: Path) -> int:
    index = _load_index(root)
    stale = [d for d, rel in index.items() if not (root / _entry_path(rel)).exists()]
    if not stale: return 0
    for d in stale: index.pop(d, None)
    _save_index(root, index)
    return len(stale)

def _sync_index_digests(root: Path, index: dict) -> int:
    known = {_entry_path(entry) for entry in index.values()}
    added = 0
    try:
        folders = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return 0
    for folder in folders:
        try:
            images = [p for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXTS]
        except OSError:
            continue
        for img in images:
            rel = f"{folder.name}/{img.name}"
            if rel in known:
                continue
            try:
                digest = hashlib.sha1(img.read_bytes()).hexdigest()
            except OSError:
                continue
            index.setdefault(digest, {"path": rel, "reason": "", "category": folder.name})
            added += 1
    return added


def resolve_capture_category(root, category_hint) -> str:
    cat = normalize_capture_category(category_hint)
    on_disk = _category_dirs(root)
    pinyin_alias = {}
    for low, real in on_disk.items():
        builtin = _COMMON_EMOTION_MAP.get(low, "")
        if builtin:
            pinyin_alias.setdefault(builtin, real)

    print(f"【表情分类】模型提供的原始分类 hint: '{category_hint}'")

    if cat.lower() in on_disk:
        cat = on_disk[cat.lower()]
        print(f"【表情分类-命中已有分类】'{category_hint}' -> '{cat}'")
    else:
        cat = cat.lower()
        mapped_cat = _COMMON_EMOTION_MAP.get(cat, "") or _COMMON_EMOTION_MAP.get(str(category_hint or "").lower(), "")
        if mapped_cat:
            cat = mapped_cat
            print(f"【表情分类-映射成功】'{category_hint}' -> '{cat}'")
        elif cat in STRICT_ALLOWED:
            print(f"【表情分类-分类合法】'{cat}' 已是内置分类，无需映射")
        else:
            print(f"【表情分类-映射失败】'{category_hint}' 未找到对应分类，进入兜底校验")

        if cat not in STRICT_ALLOWED:
            print(f"【表情分类-兜底拦截】'{cat}' 不是已有分类，强制改为 'wuyu'")
            cat = "wuyu"
        else:
            print(f"【表情分类-最终归类】图片将保存到: '{cat}' 文件夹")

        if cat.lower() not in on_disk and cat in pinyin_alias:
            print(f"【表情分类-对回已有目录】'{cat}' -> '{pinyin_alias[cat]}'")
            cat = pinyin_alias[cat]

    if cat.lower() not in on_disk:
        try:
            (root / cat).mkdir(parents=True, exist_ok=True)
            print(f"[表情分类] 已自动创建分类文件夹: {cat}")
        except OSError as e:
            print(f"[表情分类] 创建分类文件夹失败（{cat}）: {e}")
    return cat


async def auto_capture_image(config, sticker_manager, source, category_hint="",
                             image_data=None, reason="") -> bool:
    if sticker_manager is None:
        print("[表情收藏] 跳过：表情包管理器不可用。")
        return False
    if not getattr(sticker_manager, "enabled", False):
        print("[表情收藏] 跳过：表情包功能未开启（stickers_enabled=false）。")
        return False
    if not config.get("sticker_capture_enabled", False):
        print("[表情收藏] 跳过：识图自动收藏未开启（sticker_capture_enabled=false）。")
        return False
    src = str(source or "").strip()
    if not src and not image_data:
        print("[表情收藏] 跳过：没有可用的图片来源。")
        return False
    now = time.time()
    quota_blocked = _capture_quota_block(config, now)
    if quota_blocked:
        print(f"[表情收藏] 跳过：{quota_blocked}")
        return False

    data = image_data
    if not data:
        try:
            if src.startswith(("http://", "https://")):
                from .llm_helpers import download_image
                data = await download_image(src)
            else:
                p = Path(src)
                if not p.exists():
                    print(f"[表情收藏] 跳过：本地图片文件不存在（{src[:120]}）。")
                    return False
                data = p.read_bytes()
        except Exception as e:
            print(f"表情收藏读取图片失败: {type(e).__name__}: {e}")
            return False
    if not data:
        print("[表情收藏] 跳过：没有取到图片数据（下载失败或文件为空）。")
        return False

    shot = screen_shot_size(data)
    if shot:
        print(f"[表情收藏] 跳过：{shot[0]}x{shot[1]} 是屏幕截图/长图的长宽比，"
              "不像表情包（表情包是接近方形的单一画面）。")
        return False

    ext = _ext_for_bytes(data, src)
    if not ext:
        print("表情收藏跳过：无法识别的图片格式。") 
        return False

    raw_digest = hashlib.sha1(data).hexdigest()
    if ext in _preserve_formats(config):
        print(f"表情收藏保留原格式不重编码（{ext}），避免动图被压成单帧静态图。")
    else:
        data, ext = _reencode_for_sticker(data, ext, config)

    digest = hashlib.sha1(data).hexdigest()
    root = sticker_manager.dir
    index = _load_index(root)
    if _sync_index_digests(root, index):
        _save_index(root, index)
    hit = digest if digest in index else raw_digest
    existing = index.get(hit)
    if existing:
        old = root / _entry_path(existing)
        if old.exists():
            print("[表情收藏] 相同图片此前已收藏，跳过重复保存。") 
            return True
        index.pop(hit, None)

    # --- 核心分类逻辑 ---
    cat = resolve_capture_category(root, category_hint)

    dirs = [cat]

    reason = drop_role_names(reason, role_names_from_config(config))
    base = sticker_name_from_reason(reason, f"auto_{int(now * 1000)}")
    saved, primary = [], None
    try:
        for folder_name in dirs:
            folder = root / folder_name
            folder.mkdir(parents=True, exist_ok=True)
            target = _unique_path(folder, base, ext)
            target.write_bytes(data)
            saved.append(f"{folder_name}/{target.name}")
            if primary is None: primary = f"{folder_name}/{target.name}"
    except Exception as e:
        print(f"表情收藏保存失败: {type(e).__name__}: {e}") 
        return False

    if primary is not None:
        index[digest] = {"path": primary, "reason": str(reason or "").strip(),
                         "category": cat}
        _save_index(root, index)

    try: sticker_manager.rescan()
    except Exception as e: print(f"表情收藏后重扫失败: {type(e).__name__}: {e}")
    _mark_capture(now)
    print(f"[表情收藏] 已自动保存 {'、'.join(saved)}")
    return True


class StickerManager:
    def __init__(self, config):
        self.config = config
        self.mode = send_mode(config)
        self.enabled = self.mode != "off"
        self.dir = sticker_root(config)
        try:
            self.probability = float(config.get("sticker_probability", 1.0))
        except (TypeError, ValueError):
            self.probability = 1.0
        try:
            self.max_per_reply = max(1, int(config.get("sticker_max_per_reply", 1)))
        except (TypeError, ValueError):
            self.max_per_reply = 1
        self.map = {}; self.default_pool = []; self.any_pool = []
        self._scan()
    def _scan(self):
        self.map.clear(); self.default_pool.clear(); self.any_pool.clear()
        root = self.dir
        try: root.mkdir(parents=True, exist_ok=True)
        except Exception as e: print(f"表情包目录不可用: {e}"); return
        if not root.exists(): return
        for folder in root.iterdir():
            if not folder.is_dir(): continue
            images = sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXTS)
            if not images: continue
            name = folder.name.lower()
            if name == "default": self.default_pool = images
            elif name == "any": self.any_pool = images
            else: self.map[name] = images
        total = sum(len(v) for v in self.map.values()) + len(self.default_pool) + len(self.any_pool)
        if total: print(f"表情包扫描完成：{len(self.map)} 个情绪分类，共 {total} 张图片（目录：{root}）")
        else: print(f"表情包目录为空（{root}），可在 WebUI「表情包」页上传图片。")
    def rescan(self): self.__init__(self.config)
    def _candidates_for(self, emotion: str) -> list:
        key = resolve_emotion_key(emotion, self.map)
        candidates = []
        if key and key in self.map: candidates = [p for p in self.map[key] if p.exists()]
        if not candidates and self.any_pool: candidates = [p for p in self.any_pool if p.exists()]
        if not candidates: candidates = [p for p in self.default_pool if p.exists()]
        return candidates

    def _all_candidates(self) -> list:
        items = []
        for pool in list(self.map.values()) + [self.any_pool, self.default_pool]:
            items.extend(p for p in pool if p.exists())
        return items

    def _candidates(self, emotion: str) -> list:
        if self.mode == "random":
            return self._all_candidates()
        return self._candidates_for(emotion)

    def pick(self, emotion: str):
        if not self.enabled: return None
        removed = _prune_index(self.dir)
        if removed:
            print(f"[表情收藏] 清理了 {removed} 条已被删除表情包的索引记录。")
        if self.probability < 1.0 and random.random() > self.probability: return None
        candidates = self._candidates(emotion)
        if not candidates:
            self._scan()
            candidates = self._candidates(emotion)
        if not candidates: return None
        sticker = random.choice(candidates)
        if not sticker.exists():
            _prune_index(self.dir)
            return None
        return sticker

    async def pick_async(self, ctx=None, emotion: str = "", text: str = ""):
        if self.mode == "off":
            return None
        if self.mode == "description":
            picked = await self._pick_by_description(ctx, text, emotion)
            if picked is not None:
                return picked
        return self.pick(emotion)

    async def _pick_by_description(self, ctx, text: str, emotion: str):
        if ctx is None:
            return None
        try:
            _prune_index(self.dir)
            items = list(sticker_descriptions(self.dir).items())
        except Exception as e:
            print(f"表情包描述读取失败: {type(e).__name__}: {e}")
            return None
        if not items:
            return None
        try:
            limit = max(1, int(self.config.get("sticker_desc_max_candidates", 30) or 30))
        except (TypeError, ValueError):
            limit = 30
        if len(items) > limit:
            items = random.sample(items, limit)
        listing = "\n".join(f"{i}. {desc}" for i, (_, desc) in enumerate(items))
        prompt = str(self.config.get("sticker_pick_prompt", "") or "").strip()
        payload = (f"{prompt}\n\n候选表情包：\n{listing}\n\n"
                   f"角色要说的话：{str(text or '')[:200]}"
                   + (f"\n（当前情绪：{emotion}）" if emotion else ""))
        try:
            from .llm_helpers import chat_once
            result = await chat_once(ctx, [{"role": "user", "content": payload}],
                                     label="表情挑选")
        except Exception as e:
            print(f"按描述挑表情包失败，回退按情绪选择: {type(e).__name__}: {e}")
            return None
        raw = str((result or {}).get("content") or "").strip()
        match = re.search(r"\{[^{}]*\"index\"\s*:\s*(-?\d+)[^{}]*\}", raw) \
            or re.search(r"\b(\d+)\b", raw)
        if not match:
            print(f"按描述挑表情包：模型未给出有效编号（{raw[:60]!r}），回退按情绪选择。")
            return None
        try:
            index = int(match.group(1))
        except (TypeError, ValueError):
            return None
        if index < 0 or index >= len(items):
            print(f"按描述挑表情包：编号 {index} 超出候选范围，回退按情绪选择。")
            return None
        path = self.dir / items[index][0]
        return path if path.exists() else None


# ===========================================================================
# ===========================================================================

IMPORT_MAX_IMAGES = 300
IMPORT_JOB_LOG_LINES = 40
_IMPORT_JOBS = {}
_IMPORT_JOBS_LOCK = threading.Lock()
IMPORT_JOBS_KEPT = 10


def collect_import_images(paths, library=None) -> tuple:
    images, skipped, seen = [], [], set()
    roots = []
    for raw in paths or []:
        text = str(raw or "").strip().strip('"')
        if not text:
            continue
        path = Path(text)
        if path.is_dir():
            roots.append(path.resolve())
        elif path.is_file():
            roots.append(path)
        else:
            skipped.append(f"{text}：路径不存在")
    try:
        library = Path(library).resolve() if library else None
    except OSError:
        library = None
    for path in roots:
        try:
            if path.is_dir():
                candidates = sorted(p for p in path.rglob("*") if p.is_file())
            else:
                candidates = [path]
        except OSError as e:
            skipped.append(f"{path}：读取失败（{e}）")
            continue
        for item in candidates:
            if item.suffix.lower() not in IMAGE_EXTS:
                if len(candidates) == 1:
                    skipped.append(f"{item.name}：不是支持的图片格式")
                continue
            key = str(item.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            if library is not None and library in item.resolve().parents:
                skipped.append(f"{item.name}：已在表情包目录里，跳过")
                continue
            images.append(item)
    if len(images) > IMPORT_MAX_IMAGES:
        skipped.append(f"一次最多识别 {IMPORT_MAX_IMAGES} 张，其余 {len(images) - IMPORT_MAX_IMAGES} 张未处理")
        images = images[:IMPORT_MAX_IMAGES]
    return images, skipped


def _import_prompt(candidates_text: str) -> str:
    return (
        "看这张图，判断它以后当表情包发出去时该放进哪个情绪文件夹，"
        "并给它起一个通用名字。\n"
        "只输出一个 JSON 对象，格式：{\"category\": \"分类名\", \"name\": \"通用用途名\"}；"
        "不要输出台词、画面描述、解释或其他字段。\n"
        "category 只能从下面这份清单里挑一个，并把名称原样照抄"
        "（清单取自表情包目录下实际存在的文件夹，不要自己新造，也不要换成拼音或译文）：\n"
        f"{candidates_text}\n"
        f"{CATEGORY_DISAMBIGUATION}\n"
        "name 是这张表情以后反复使用时的名字：只写它适合表达的情绪与互动用途，"
        "不写画面里是谁、也不写是给谁用的；严禁出现任何角色名、人名、作品名；"
        "简短（4~12 个字），不能写成句子，也不能是“好看”“有趣”“可爱”这类空话。"
    )


async def classify_sticker_image(ctx, source: str, data: bytes, mime: str) -> dict:
    from .llm_helpers import base64_b64, extract_json_objects, vision_chat_once
    prompt = _import_prompt(category_candidates_text(ctx))
    content, _ms = await vision_chat_once(
        ctx, prompt, [(str(source), mime, base64_b64(data))], max_tokens=256,
        label="表情分类")
    for obj in extract_json_objects(content or ""):
        if isinstance(obj, dict) and ("category" in obj or "name" in obj):
            return obj
    print(f"【表情一键识别】模型没给出可用的分类与命名（{str(content or '')[:80]!r}）")
    return {}


async def import_sticker_images(config, sticker_manager, paths, log=None) -> dict:
    emit = log or (lambda line: None)
    root = sticker_manager.dir if sticker_manager is not None else None
    images, skipped_paths = collect_import_images(paths, library=root)
    result = {"total": len(images), "saved": 0, "skipped": 0, "failed": 0,
              "items": [], "skipped_paths": skipped_paths}
    for reason in skipped_paths:
        print(f"【表情一键识别】跳过 {reason}")
    if not images:
        return result
    if sticker_manager is None:
        raise RuntimeError("表情包管理器不可用")
    ctx = RoleContext(config)
    role_names = role_names_from_config(config)
    index = _load_index(root)
    if _sync_index_digests(root, index):
        _save_index(root, index)
    known = {_entry_path(entry) for entry in index.values()}

    for position, image in enumerate(images, start=1):
        emit(f"[{position}/{len(images)}] 识别 {image.name} …")
        item = {"path": str(image), "name": image.name, "category": "", "saved": "",
                "error": ""}
        try:
            data = image.read_bytes()
        except OSError as e:
            item["error"] = f"读取失败：{e}"
            result["failed"] += 1
            result["items"].append(item)
            continue
        if not data:
            item["error"] = "文件为空"
            result["failed"] += 1
            result["items"].append(item)
            continue
        mime = sniff_image_mime(data)
        ext = _ext_for_bytes(data, str(image))
        if not mime or not ext:
            item["error"] = "无法识别的图片格式"
            result["failed"] += 1
            result["items"].append(item)
            continue
        raw_digest = hashlib.sha1(data).hexdigest()
        try:
            verdict = await classify_sticker_image(ctx, str(image), data, mime)
        except Exception as e:
            item["error"] = f"识图失败：{type(e).__name__}: {e}"
            result["failed"] += 1
            result["items"].append(item)
            continue
        if not verdict:
            item["error"] = "识图模型没有给出分类与命名"
            result["failed"] += 1
            result["items"].append(item)
            continue
        category = resolve_capture_category(root, verdict.get("category", ""))
        item["category"] = category
        stored = data
        if ext not in _preserve_formats(config):
            stored, ext = _reencode_for_sticker(data, ext, config)
        digest = hashlib.sha1(stored).hexdigest()
        existing = index.get(digest) or index.get(raw_digest)
        if existing:
            rel = _entry_path(existing)
            if rel and rel not in known and (root / rel).exists():
                known.add(rel)
            item["saved"] = rel
            item["error"] = "表情库里已有同一张图"
            result["skipped"] += 1
            result["items"].append(item)
            continue
        base = sticker_name_from_reason(
            drop_role_names(verdict.get("name", ""), role_names), "") \
            or sticker_name_from_reason(verdict.get("name", ""), "") \
            or f"sticker_{int(time.time() * 1000)}"
        try:
            folder = root / category
            folder.mkdir(parents=True, exist_ok=True)
            target = _unique_path(folder, base, ext)
            target.write_bytes(stored)
        except OSError as e:
            item["error"] = f"写入失败：{e}"
            result["failed"] += 1
            result["items"].append(item)
            continue
        rel = f"{category}/{target.name}"
        item["saved"] = rel
        index[digest] = {"path": rel, "reason": str(verdict.get("name") or "").strip(),
                         "category": category}
        _save_index(root, index)
        known.add(rel)
        result["saved"] += 1
        result["items"].append(item)
        emit(f"[{position}/{len(images)}] {image.name} → {rel}")
    if result["saved"]:
        try:
            sticker_manager.rescan()
        except Exception as e:
            print(f"【表情一键识别】归档后重扫失败: {type(e).__name__}: {e}")
    return result


class StickerImportJob:

    def __init__(self, config, sticker_manager, paths):
        self.id = uuid.uuid4().hex
        self.total = len(paths or [])
        self.done = 0
        self.stage = "准备中"
        self.running = True
        self.error = ""
        self.result = None
        self._logs = deque(maxlen=IMPORT_JOB_LOG_LINES)
        self._config = config
        self._manager = sticker_manager
        self._paths = list(paths or [])
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def log(self, line):
        text = str(line or "").strip()
        if text:
            with self._lock:
                self._logs.append(text)

    def snapshot(self) -> dict:
        with self._lock:
            logs = list(self._logs)
        return {"job_id": self.id, "running": self.running, "done": self.done,
                "total": self.total, "stage": self.stage, "error": self.error,
                "logs": logs, "result": self.result}

    def _run(self):
        try:
            self.stage = "识别中"
            result = asyncio.run(import_sticker_images(
                self._config, self._manager, self._paths, log=self.log))
            self.result = result
            self.total = int(result.get("total") or 0)
            self.done = self.total
            self.stage = (f"完成：归档 {result['saved']} 张，重复 {result['skipped']} 张，"
                          f"失败 {result['failed']} 张")
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            self.log(f"识别失败：{self.error}")
            self.stage = "失败"
        finally:
            self.running = False


def start_import_job(config, sticker_manager, paths) -> StickerImportJob:
    job = StickerImportJob(config, sticker_manager, paths)
    with _IMPORT_JOBS_LOCK:
        _IMPORT_JOBS[job.id] = job
        while len(_IMPORT_JOBS) > IMPORT_JOBS_KEPT:
            _IMPORT_JOBS.pop(next(iter(_IMPORT_JOBS)), None)
    return job.start()


def get_import_job(job_id) -> Optional[StickerImportJob]:
    with _IMPORT_JOBS_LOCK:
        return _IMPORT_JOBS.get(str(job_id or ""))
