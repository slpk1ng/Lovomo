"""表情包管理：按情绪目录扫描本地表情图片，回复时按概率附加。"""
import hashlib
import json
import random
import re
import time
from pathlib import Path

from .llm_helpers import sniff_image_mime

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_MIME_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
             "image/webp": ".webp", "image/bmp": ".bmp"}

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

# 各分类之间的区分要点（减少"一律 gaoxing/一律 pingjing"的误判）
CATEGORY_DISAMBIGUATION = (
    "区分要点："
    "① 阴森、恐怖、诡异、压抑、病态的画面绝不算 gaoxing（开心），"
    "它更接近 weixie（挑逗/阴阳怪气）或 wuyu（无语），"
    "若画面只是氛围压抑而无互动用途，应判定为不收藏；"
    "② 只有画面里真的存在开心/爆笑/炫耀的互动用途才写 gaoxing；"
    "③ 单纯可爱只是基础条件，不足以判 sajiao，必须能用于撒娇讨要；"
    "④ 「睁大眼睛」「惊喜」这类单个神态词不足以判 jingya。"
)


def category_guide_text() -> str:
    """给 LLM 看的分类清单（拼音 + 使用场景）。"""
    return "、".join(f"{c}({CATEGORY_GUIDE[c]})" if c in CATEGORY_GUIDE else c
                    for c in sorted(STRICT_ALLOWED))

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

def _capture_quota_ok(config, now: float) -> bool:
    st = _capture_state
    today = time.strftime("%Y-%m-%d")
    if st["date"] != today: st["date"] = today; st["count"] = 0
    interval = max(0.0, float(config.get("sticker_capture_min_interval", 300) or 0))
    if interval > 0 and now - st["last"] < interval: return False
    cap = max(0, int(config.get("sticker_capture_max_per_day", 20) or 0))
    if cap > 0 and st["count"] >= cap: return False
    return True

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

# 默认不重编码的格式：这些格式可能带动画（多帧），重编码会丢帧。
# 可通过 sticker_capture_preserve_formats 配置（逗号/空格/换行分隔）。
DEFAULT_PRESERVE_FORMATS = (".gif", ".webp")


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

def _load_index(root: Path) -> dict:
    try:
        p = _index_path(root)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception: pass
    return {}

def _save_index(root: Path, index: dict):
    try: _index_path(root).write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    except Exception: pass

def _prune_index(root: Path) -> int:
    index = _load_index(root)
    stale = [d for d, rel in index.items() if not (root / str(rel)).exists()]
    if not stale: return 0
    for d in stale: index.pop(d, None)
    _save_index(root, index)
    return len(stale)

async def auto_capture_image(config, sticker_manager, source, category_hint="") -> bool:
    if sticker_manager is None or not getattr(sticker_manager, "enabled", False): return False
    if not config.get("sticker_capture_enabled", False): return False
    src = str(source or "").strip()
    if not src: return False
    now = time.time()
    if not _capture_quota_ok(config, now): return False

    data = None
    try:
        if src.startswith(("http://", "https://")):
            from .llm_helpers import download_image
            data = await download_image(src)
        else:
            p = Path(src)
            if not p.exists(): return False
            data = p.read_bytes()
    except Exception as e:
        print(f"表情收藏读取图片失败: {type(e).__name__}: {e}") 
        return False
    if not data: return False

    ext = _ext_for_bytes(data, src)
    if not ext:
        print("表情收藏跳过：无法识别的图片格式。") 
        return False

    if ext in _preserve_formats(config):
        print(f"表情收藏保留原格式不重编码（{ext}），避免动图被压成单帧静态图。")
    else:
        data, ext = _reencode_for_sticker(data, ext, config)

    digest = hashlib.sha1(data).hexdigest()
    root = sticker_manager.dir
    index = _load_index(root)
    existing = index.get(digest)
    if existing:
        old = root / existing
        if old.exists():
            print("[表情收藏] 相同图片此前已收藏，跳过重复保存。") 
            return True
        index.pop(digest, None)

    # --- 核心分类逻辑 ---
    cat = normalize_capture_category(category_hint).lower()
    existing_folders = {d.name.lower() for d in root.iterdir() if d.is_dir()}

    # 打印模型原本给的是什么
    print(f"【表情收藏-进入保存】模型提供的原始分类 hint: '{category_hint}'")

    # 1. 翻译：模型说中文/英文，转成拼音
    mapped_cat = _COMMON_EMOTION_MAP.get(cat, "") or _COMMON_EMOTION_MAP.get(category_hint.lower(), "")
    if mapped_cat:
        cat = mapped_cat
        print(f"【表情收藏-映射成功】'{category_hint}' -> '{cat}'")
    else:
        print(f"【表情收藏-映射失败】'{category_hint}' 未找到对应拼音，进入白名单校验")

    # 2. 白名单校验：绝对不进 any / default，不认识的词全部强制扔进 wuyu
    if cat not in STRICT_ALLOWED:
        print(f"【表情收藏-白名单拦截】'{cat}' 不在白名单中，强制改为 'wuyu'")
        cat = "wuyu"
    else:
        print(f"【表情收藏-最终归类】图片将保存到: '{cat}' 文件夹")

    existing_folders = {d.name.lower() for d in root.iterdir() if d.is_dir()}
    if cat not in existing_folders:
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

    base = f"auto_{int(now * 1000)}"
    saved, primary = [], None
    try:
        for folder_name in dirs:
            folder = root / folder_name
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / f"{base}_{random.randint(1000, 9999)}{ext}"
            target.write_bytes(data)
            saved.append(f"{folder_name}/{target.name}")
            if primary is None: primary = f"{folder_name}/{target.name}"
    except Exception as e:
        print(f"表情收藏保存失败: {type(e).__name__}: {e}") 
        return False

    if primary is not None:
        index[digest] = primary
        _save_index(root, index)

    try: sticker_manager.rescan()
    except Exception as e: print(f"表情收藏后重扫失败: {type(e).__name__}: {e}")
    _mark_capture(now)
    print(f"[表情收藏] 已自动保存 {'、'.join(saved)}")
    return True


class StickerManager:
    # 这部分原封不动
    def __init__(self, config):
        self.config = config
        self.enabled = bool(config.get("stickers_enabled", False))
        self.dir = Path(config.get("stickers_dir", "") or Path("data/stickers"))
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
    def pick(self, emotion: str):
        if not self.enabled: return None
        removed = _prune_index(self.dir)
        if removed:
            print(f"[表情收藏] 清理了 {removed} 条已被删除表情包的索引记录。")
        if self.probability < 1.0 and random.random() > self.probability: return None
        candidates = []
        key = str(emotion or "").strip().lower()
        if key and key in self.map: candidates = [p for p in self.map[key] if p.exists()]
        if not candidates and self.any_pool: candidates = [p for p in self.any_pool if p.exists()]
        if not candidates: candidates = [p for p in self.default_pool if p.exists()]
        if not candidates:
            self._scan()
            if key and key in self.map: candidates = [p for p in self.map[key] if p.exists()]
            if not candidates and self.any_pool: candidates = [p for p in self.any_pool if p.exists()]
            if not candidates: candidates = [p for p in self.default_pool if p.exists()]
        if not candidates: return None
        sticker = random.choice(candidates)
        if not sticker.exists():
            _prune_index(self.dir)
            return None
        return sticker