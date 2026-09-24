"""表情包管理：按情绪目录扫描本地表情图片，回复时按概率附加。"""
import hashlib
import json
import os
import random
import re
import tempfile
import time
from pathlib import Path

from .llm_helpers import resolve_emotion_key, sniff_image_mime

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_MIME_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
             "image/webp": ".webp", "image/bmp": ".bmp"}
# 扩展名 → MIME 的反查表：Windows 的 mimetypes 认不出 .webp，交给它猜会退回
# application/octet-stream，浏览器就不再把缩略图当图片渲染
MIME_BY_EXT = {ext: mime for mime, ext in _MIME_EXT.items()}
MIME_BY_EXT[".jpeg"] = "image/jpeg"

STRICT_ALLOWED = {"gaoxing", "shengqi", "haixiu", "wuyu", "jingya", "sajiao", "weixie", "pingjing"}

# 每个分类「适合什么互动场景」的说明，用于指导 LLM 归类。
# 事故背景：纯风景/阴森图被判成 gaoxing，根因是分类说明太粗糙 +
# 指令强调"必须给出分类"，模型于是随便挑了个正向情绪。
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

# 各分类之间的区分要点（减少"一律高兴/一律平静"的误判）。
# 分类名取自表情库的实际文件夹（可能是中文），所以这里只用通用说法描述，不写死分类名。
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
    """表情库根目录（与 StickerManager.dir 同一套解析）。"""
    return Path(config.get("stickers_dir", "") or Path("data/stickers"))


# any 是「任意情绪都能用」的通用池、default 是内置兜底池，两者都不是分类
_NON_CATEGORY_DIRS = {"any", "default"}


def _has_images(folder: Path) -> bool:
    """目录里是否真有一张图片：一张都没有的目录不算分类。"""
    try:
        return any(p.suffix.lower() in IMAGE_EXTS for p in folder.iterdir())
    except OSError:
        return False


def _category_dirs(root) -> dict:
    """分类目录：小写名 → 真实目录名。只算有图片的目录。

    空目录不算分类：面板不显示它，用户看不见；把它当分类会让模型选中它，
    收藏落进一个用户看不到的目录。
    """
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
    """表情库根目录下的分类文件夹名（按名称排序，不含 any/default 池）。"""
    return sorted(_category_dirs(root).values())


def category_candidates(config) -> list:
    """可作为收藏分类的候选名。

    以磁盘上实际存在的分类文件夹为准（用户可以把目录命名成中文或任何词）；
    一个分类目录都没有时（全新安装）退回内置拼音分类，否则模型无从可选。
    """
    return category_folders(sticker_root(config)) or sorted(STRICT_ALLOWED)


def category_candidates_text(config) -> str:
    """候选分类清单：一行一个，便于模型逐一比较后再选。"""
    return "\n".join(
        f"- {n}({CATEGORY_GUIDE[n]})" if n in CATEGORY_GUIDE else f"- {n}"
        for n in category_candidates(config))


# 「收藏判定指令」的默认值。分类清单由 category_candidates_text 在运行时按表情库
# 实际文件夹生成，所以这里不写死分类名，只要求模型照抄清单里的名称。
DEFAULT_CAPTURE_PROMPT = (
    "附加收藏指令：如果你认为这张图片有趣、可爱、有梗或有纪念意义，"
    "请在输出完主要回复JSON之后，再单独输出一个JSON对象（不要放进sentences数组），"
    "格式：{\"sticker_capture\": true, \"category\": \"分类名\", "
    "\"reason\": \"通用用途名（6~12字，不含角色名）\"}。"
    "category 必须照抄【表情包收藏判定】里给出的分类清单中的名称，"
    "禁止自己新造分类，禁止填 default 或 any。"
    "如果拿不准，直接输出 {\"sticker_capture\": false}。"
)

# 完整映射表：模型说中文/英文，自动翻译成白名单里的拼音
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
    # 冷静/疲惫/等等（不在白名单，映射后会自动转成wuyu兜底，绝不建新文件夹）
    "平静": "pingjing", "淡定": "pingjing", "冷漠": "lengdan", "严肃": "yansu",
    "疲惫": "pibei", "困": "pibei", "无聊": "wuliao", "犯困": "fankun",
}

_capture_state = {"date": "", "count": 0, "last": 0.0}

def normalize_capture_category(name) -> str:
    """清洗模型给的表情分类名。

    非法名（含路径分隔符、`.`/`..`、空）不能返回空串——空串会被当成"目录名"使用，
    也可能让上层误以为分类有效。统一回退到白名单里的兜底分类 wuyu（无语），
    与下面「不认识的词强制进 wuyu」的策略保持一致，绝不新建 default/any 之外的怪目录。
    """
    s = str(name or "").strip()
    if not s or s in (".", "..") or any(ch in s for ch in '/\\:*?"<>|'):
        return "wuyu"
    return s[:48] if len(s) > 48 else s

def _capture_quota_block(config, now: float) -> str:
    """返回被节流拦住的原因；可以收藏时返回空串。

    返回原因而不是布尔值：收藏被静默跳过的样子和"功能坏了"完全一样，
    用户只能看到识图的提取结果，之后什么都没有。
    """
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
# 文件名只当短标签：全角标点换成空格，完整说明存在 .auto_index.json 里
_REASON_SEP_RE = re.compile(r"[，。、；：！？…～〜·—―「」『』（）〔〕()\[\]【】“”‘’\s]+")
_NAME_ILLEGAL_CHARS = set('/\\:*?"<>|')

SEND_MODES = ("off", "random", "emotion", "description")


def safe_sticker_name(name) -> str:
    """校验表情包文件名：合法返回原名，空/带路径分隔符/穿越时返回空串。"""
    s = str(name or "").strip()
    if not s or s in (".", "..") or s != Path(s).name or (_NAME_ILLEGAL_CHARS & set(s)):
        return ""
    return s


def role_names_from_config(config) -> list:
    """配置里所有角色的名字（收藏的表情名里不该出现它们）。"""
    names = {str(config.get("character_name", "") or "").strip()}
    roles = config.get("roles")
    if isinstance(roles, list):
        for role in roles:
            if isinstance(role, dict):
                names.add(str(role.get("character_name", "") or "").strip())
    return [n for n in sorted(names, key=len, reverse=True) if len(n) >= 2]


def drop_role_names(text, names) -> str:
    """去掉文本里的角色名：表情是按用途命名才能换角色继续用。"""
    out = str(text or "")
    for name in names or ():
        out = out.replace(name, "")
    return re.sub(r"\s{2,}", " ", out).strip()


def sticker_name_from_reason(reason: str, fallback: str = "") -> str:
    """把模型给的 reason 洗成简短通用的表情名（同时用作文件名）。"""
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", str(reason or ""))
    name = _REASON_SEP_RE.sub(" ", name).strip().strip(". ")
    if len(name) > _REASON_NAME_MAX:
        name = name[:_REASON_NAME_MAX].rstrip()
    return name or fallback


def _unique_path(folder: Path, base: str, ext: str) -> Path:
    """同一句 reason 被多次命中时加序号，不覆盖已有的表情。"""
    target = folder / f"{base}{ext}"
    if not target.exists():
        return target
    for i in range(2, 1000):
        candidate = folder / f"{base}-{i}{ext}"
        if not candidate.exists():
            return candidate
    return folder / f"{base}-{int(time.time() * 1000)}{ext}"


def _entry_path(entry) -> str:
    """索引值兼容两种形态：新版的 {path, reason, category} 与旧版的纯路径字符串。"""
    if isinstance(entry, dict):
        return str(entry.get("path") or "")
    return str(entry or "")


def _entry_reason(entry) -> str:
    return str(entry.get("reason") or "") if isinstance(entry, dict) else ""


def sticker_descriptions(root: Path) -> dict:
    """相对路径 -> 说明文字（模型给的 reason；没有则退回分类说明）。"""
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

# 默认不重编码的格式：这些格式可能带动画（多帧），重编码会丢帧。
# 可通过 sticker_capture_preserve_formats 配置（逗号/空格/换行分隔）。
DEFAULT_PRESERVE_FORMATS = (".gif", ".webp")


def send_mode(config) -> str:
    """发送方式：off=关闭 / random=随机 / emotion=按情绪 / description=按描述让模型挑。

    老配置只有 stickers_enabled 没有 sticker_send_mode，这里按开关折算一次，
    避免升级后表情包被静默关掉。
    """
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


def _reencode_for_sticker(data: bytes, ext: str, config) -> tuple:
    """静态图重编码：缩小体积并统一为 PNG/JPEG。失败时原样返回。"""
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
    """发送表情包时的最长边上限；0 表示不缩放。"""
    try:
        return max(0, int(config.get("sticker_output_max_side", 400) or 0))
    except (TypeError, ValueError):
        return 400


def resize_for_output(path, config) -> tuple:
    """把要发送的表情包缩到统一边长，返回 (发送用文件, 临时文件或 None)。

    表情库里的图尺寸参差不齐，直接发出去在聊天窗里忽大忽小。
    不超过上限时原样返回，不重编码；动图跳过，重编码会丢帧。
    返回的临时文件由调用方发送完删除。
    """
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
    """把表情库里已有的图片按内容摘要补进索引，返回新增条数。

    索引原本只记「本程序收藏过」的图：用户自己放进目录的图不在其中，
    同一张图会被当成新图再收藏一次，库里于是出现两份。
    """
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


async def auto_capture_image(config, sticker_manager, source, category_hint="",
                             image_data=None, reason="") -> bool:
    """把一张图收藏进表情库。

    每一步跳过都必须写明原因：全部静默 return False 的时候，
    "设了开关却没收藏"根本无从排查（用户只能看到提取结果，之后什么都没了）。
    """
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
    # 库里存的是原图（用户自己放的）或本程序重编码后的图，两种摘要都查一次
    hit = digest if digest in index else raw_digest
    existing = index.get(hit)
    if existing:
        old = root / _entry_path(existing)
        if old.exists():
            print("[表情收藏] 相同图片此前已收藏，跳过重复保存。") 
            return True
        index.pop(hit, None)

    # --- 核心分类逻辑 ---
    cat = normalize_capture_category(category_hint)
    # 文件夹名 → 原名，分类名由用户自己命名（可以是中文），大小写不敏感地匹配；
    # 空目录不算分类，否则模型会选中一个用户看不见的目录
    on_disk = _category_dirs(root)
    # 内置拼音分类 → 磁盘上的真实目录名：模型给拼音（或中文被翻译成拼音）时
    # 对回用户自己命名的那个文件夹，否则同一种情绪会被拆成两个目录
    pinyin_alias = {}
    for low, real in on_disk.items():
        builtin = _COMMON_EMOTION_MAP.get(low, "")
        if builtin:
            pinyin_alias.setdefault(builtin, real)

    # 打印模型原本给的是什么
    print(f"【表情收藏-进入保存】模型提供的原始分类 hint: '{category_hint}'")

    # 1. 先认表情库里已有的文件夹：分类名由用户命名，绕过它去查内置拼音表
    #    会按拼音另建一个目录，把同一种情绪拆成两个文件夹
    if cat.lower() in on_disk:
        cat = on_disk[cat.lower()]
        print(f"【表情收藏-命中已有分类】'{category_hint}' -> '{cat}'")
    else:
        cat = cat.lower()
        # 2. 翻译：模型说中文/英文，转成内置分类名
        mapped_cat = _COMMON_EMOTION_MAP.get(cat, "") or _COMMON_EMOTION_MAP.get(str(category_hint or "").lower(), "")
        if mapped_cat:
            cat = mapped_cat
            print(f"【表情收藏-映射成功】'{category_hint}' -> '{cat}'")
        elif cat in STRICT_ALLOWED:
            # 模型直接给了内置分类名：本来就合法，别打印成"映射失败"，会被误读成分类被拒
            print(f"【表情收藏-分类合法】'{cat}' 已是内置分类，无需映射")
        else:
            print(f"【表情收藏-映射失败】'{category_hint}' 未找到对应分类，进入兜底校验")

        # 3. 兜底校验：绝对不进 any / default，不认识的词全部强制扔进 wuyu
        if cat not in STRICT_ALLOWED:
            print(f"【表情收藏-兜底拦截】'{cat}' 不是已有分类，强制改为 'wuyu'")
            cat = "wuyu"
        else:
            print(f"【表情收藏-最终归类】图片将保存到: '{cat}' 文件夹")

        # 4. 收敛到内置分类名之后，再对一次磁盘上的原名
        if cat.lower() not in on_disk and cat in pinyin_alias:
            print(f"【表情收藏-对回已有目录】'{cat}' -> '{pinyin_alias[cat]}'")
            cat = pinyin_alias[cat]

    if cat.lower() not in on_disk:
        try:
            (root / cat).mkdir(parents=True, exist_ok=True)
            print(f"[表情收藏] 已自动创建分类文件夹: {cat}")
        except Exception: pass

    dirs = [cat]
    # 同时放一份进 any 池：情绪分类目录是"这个表情适合什么情绪"，
    # any 池是"任意情绪都可用的通用池"。只存情绪目录的话，
    # 其他情绪回复时 any_pool 里看不到这张图，收藏等于白收。
    if config.get("sticker_capture_any_pool", True) and cat != "any":
        dirs.append("any")

    # 表情名与说明都按用途来：带上角色名的话，换了角色这套名字就不好用了
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
        # 情绪名可能是中文（角色情绪目录自定义），分类目录是拼音时靠同义组对上
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
        """按当前发送方式挑一张；description 模式会问一次模型。"""
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
            result = await chat_once(ctx, [{"role": "user", "content": payload}])
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