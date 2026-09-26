"""TTS 合成与音频处理工具（自 main.py 迁出，供主流程与主动消息共用）。"""
import asyncio
import random
import re
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Optional

import httpx
import numpy as np

from .tls import verified_context
from .audio_level import DEFAULT_MAX_GAIN_DB, DEFAULT_PEAK_DB, DEFAULT_TARGET_DB
from .audio_level import normalize_file as _normalize_level
from .audio_level import prepare_reference as _prepare_reference


def _level_setting(config, key: str, fallback: float) -> float:
    try:
        return float(config.get(key, fallback))
    except (TypeError, ValueError):
        return fallback


def _tts_reference(path, config, data_path: Path) -> str:
    """送进 TTS 的参考音频先统一电平（单声道 + 裁静音 + 响度归一），结果缓存复用。"""
    if not bool(config.get("tts_ref_normalize", True)):
        return str(path)
    try:
        prepared = _prepare_reference(
            path, Path(data_path) / "ref_cache",
            target_db=_level_setting(config, "tts_loudness_target_db", DEFAULT_TARGET_DB),
            peak_db=_level_setting(config, "tts_loudness_peak_db", DEFAULT_PEAK_DB),
            max_gain_db=_level_setting(config, "tts_loudness_max_gain_db", DEFAULT_MAX_GAIN_DB))
    except Exception as e:
        _safe_print(f"参考音频预处理失败（按原文件合成）: {type(e).__name__}: {e}")
        return str(path)
    if prepared != str(path) and bool(config.get("tts_debug_log", False)):
        _safe_print(f"参考音频电平统一: {Path(path).name} -> {Path(prepared).name}")
    return prepared


def _normalize_tts_output(path, config) -> None:
    """把这一段语音的响度对齐到统一目标，避免段与段之间忽大忽小。"""
    if not bool(config.get("tts_loudness_normalize", True)):
        return
    try:
        result = _normalize_level(
            path,
            target_db=_level_setting(config, "tts_loudness_target_db", DEFAULT_TARGET_DB),
            peak_db=_level_setting(config, "tts_loudness_peak_db", DEFAULT_PEAK_DB),
            max_gain_db=_level_setting(config, "tts_loudness_max_gain_db", DEFAULT_MAX_GAIN_DB),
            warn="tts_output")
    except Exception as e:
        _safe_print(f"响度统一失败（保持原样）: {type(e).__name__}: {e}")
        return
    if result.get("ok") and bool(config.get("tts_debug_log", False)):
        _safe_print(f"响度统一: {Path(path).name} 调整 {result['gain_db']:+.1f}dB")


def get_audio_duration(file_path: str) -> float:
    try:
        with wave.open(file_path, 'rb') as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate > 0:
                return frames / rate
    except Exception:
        pass
    return 1.0


def resolve_tts_path(input_path: str) -> str:
    if not input_path:
        return "C:/tts"
    if re.match(r'^[A-Za-z]:[/\\]tts', input_path.strip()):
        target_dir = input_path.strip()
        try:
            Path(target_dir).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return target_dir
    input_path = input_path.strip().replace("/", "\\").rstrip("\\")
    if 1 < len(input_path) <= 3 and input_path[1] == ":":
        target_dir = f"{input_path}\\tts"
    else:
        target_dir = input_path
    try:
        Path(target_dir).mkdir(parents=True, exist_ok=True)
    except OSError:
        target_dir = "C:/tts"
    return target_dir


# 送进 TTS 的字符白名单。CJK / 假名 / 谚文 / 全角标点 / 半角可打印字符。
# 注意：这里的白名单**不再决定"哪些字可以念"**，只用于兜底剔除
# 真正无法合成的字符（表情符号、控制字符等）。
# 历史事故：旧白名单缺少 …—―♪♡ 等符号，导致它们被静默删除，
# 表现为"语音比 LLM 实际发送的文本少了某个词/某句话"。
_TTS_ALLOWED_RE = re.compile(
    r'[\u3040-\u30FF\u4E00-\u9FFF\u3400-\u4DBF\uF900-\uFAFF'
    r'\uAC00-\uD7AF\u3000-\u303F\uFF01-\uFF5E'
    r'\u2018\u2019\u201c\u201d'      # ‘’“”
    r'\u2026\u2027'                  # … ‚
    r'\u2013\u2014\u2015'            # – — ―
    r'\u00b7\u2022'                  # · •
    r'\u266a\u266b'                  # ♪ ♫
    r'\u300c\u300d\u300e\u300f'      # 「」『』
    r'a-zA-Z0-9 \n\r\t.,!?;:()\[\]{}<>@#$%^&*+=|\\/~`\'"\-_]+'
)

# 1) 送 TTS 前的字符归一化：把"能表达语气但朗读器不认"的符号换成等效写法，
#    而不是直接删掉。凡是有语义/语气意义的标点都保留其停顿含义。
_TTS_CHAR_MAP = {
    "\u301c": "\uff5e",   # 〜 → ～
    "\u2015": "\uff5e",   # ― 水平线 → ～（长音，比顿号更自然）
    "\u2014": "\uff5e",   # — 破折号 → ～
    "\u2013": "\uff5e",   # – 连接号 → ～
    "\u2012": "\uff5e",
    "\u2010": "-",        # ‐ 连字符
    "\u2011": "-",
    "\u00b7": "\u3001",   # · → 、
    "\u2027": "\u3001",
    "\u2022": "\u3001",   # • → 、
    "\u266a": "",         # ♪ 哼唱记号：无对应的朗读音，去掉不影响内容
    "\u266b": "",
    "\u2665": "",         # ♥ 纯装饰，删掉不影响内容
    "\u2661": "",
    "\u2764": "",
    "\uff5e": "\uff5e",
}

# 2) TTS 参考音频尾部会拼接，行首标点容易被误读；只剥离行首的纯标点，
#    保留「『（ 等开引号/开括号。
_LEADING_JUNK_RE = re.compile(r'^(?:[\s\u3000。，、,.!?！？…～~；;：:\-—―_*#]+)')
_TRAILING_WS_RE = re.compile(r'[\s\u3000]+$')

# 3) 语气拖音标记：全角波浪线（含归一化后的 — ― – 〜）与片假名长音符「ー」。
#    连续 3 个以上一律压到 2 个。模型写「————————」时上面那条映射会把它变成
#    十几个 ～ 送进合成，引擎会把这一串当成一个超长元音，直接进入
#    "同一个音节无限重复"的失控状态（表现为一句台词末尾拖着几十秒的「に」「呜」）。
_TONE_MARK_KEEP = 2
_TONE_MARK_RUN_RE = re.compile(r'([\uff5e\u301c\u30fc])\1{2,}')


def _collapse_tone_marks(text: str, log: bool = True) -> str:
    """把连续的拖音标记压到 _TONE_MARK_KEEP 个（只缩短，不删内容）。"""
    collapsed = _TONE_MARK_RUN_RE.sub(
        lambda m: m.group(1) * _TONE_MARK_KEEP, text)
    if collapsed != text and log:
        hit = _TONE_MARK_RUN_RE.search(text)
        _safe_print(f"TTS 拖音标记压缩: {hit.group(0)!r} → "
                    f"{hit.group(1) * _TONE_MARK_KEEP!r}（长串拖音会让合成引擎失控拖腔）")
    return collapsed


def _normalize_tts_chars(text: str, extra_map: dict = None) -> str:
    """按映射表归一化字符；extra_map 来自配置，可覆盖/扩展默认映射。"""
    mapping = _TTS_CHAR_MAP if not extra_map else {**_TTS_CHAR_MAP, **extra_map}
    out = []
    for ch in str(text or ""):
        out.append(mapping.get(ch, ch))
    return "".join(out)


def _configured_char_map(config) -> dict:
    """从配置读取自定义字符映射（格式：字符=替换；每行一条）。

    默认映射只是"常见符号"的兜底，用户可以在 WebUI 里按自己的 TTS 服务
    调整（例如把某个符号改成空格），不必改代码。
    """
    raw = config.get("tts_char_map", None) if config is not None else None
    if raw is None:
        return {}
    result = {}
    items = raw if isinstance(raw, (list, tuple)) else str(raw).splitlines()
    for item in items:
        text = str(item).strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, _, value = text.partition("=")
        key = key.strip()
        if not key:
            continue
        if key in ("\\s", "space", "空格"):
            result[" "] = value
            continue
        result[key[0]] = value.strip()
    return result


def _safe_print(message: str):
    """控制台可能是 GBK（Windows 中文默认）：打印特殊符号可能抛 UnicodeEncodeError。
    日志绝不能因为编码问题打断 TTS 流程。"""
    try:
        print(message)
    except UnicodeEncodeError:
        try:
            encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
            data = str(message).encode(encoding, "replace")
            print(data.decode(encoding, "replace"))
        except Exception:
            pass
    except Exception:
        pass


# 送进 TTS 的台词里的 URL 一律剔除：合成引擎会把 "https://xxx" 逐字符念出来
# （ASCII 全在字符白名单里，常规清洗拦不住）。链接只保留在聊天展示文本里。
# 采用标准 URL 字符集：?id=xx、%E4 这类查询串/转义都是链接的一部分；
# 中文字符不在集合里，所以"链接在这里https://x.com/a。下一句"会在句号/汉字处自然截断，
# 绝不会吞掉链接后面的中文。
_TTS_URL_RE = re.compile(
    r'(?:https?://|www\.)[A-Za-z0-9\-._~:/?#@!$&*+,;=%]+',
    re.I)


def strip_urls_for_tts(text: str) -> str:
    """剔除台词里的 URL，返回供合成的纯文本；纯链接的台词会变成空串（调用方跳过合成）。

    事故背景：LLM 回复里带链接时，整段文本被送进 TTS，合成出
    "h t t p s 冒号 斜杠…"式的乱码语音。链接本身只该出现在聊天展示文本里。
    """
    raw = str(text or "")
    if "://" not in raw and "www." not in raw.lower():
        return raw
    dropped = [m.group(0) for m in _TTS_URL_RE.finditer(raw)]
    stripped = _TTS_URL_RE.sub("", raw)
    if dropped:
        _safe_print(f"TTS 剔除台词中的链接（不进语音，仅保留在消息文本里）: {dropped}")
    return re.sub(r'[ \t\u3000]+', ' ', stripped).strip()


def _mapping_changes(original: str, extra_map: dict = None) -> list:
    """列出会被替换/删除的字符及替换结果（仅用于日志）。"""
    mapping = _TTS_CHAR_MAP if not extra_map else {**_TTS_CHAR_MAP, **extra_map}
    changes = []
    for ch in str(original or ""):
        rep = mapping.get(ch)
        if rep is not None and rep != ch:
            pair = (ch, rep)
            if pair not in changes:
                changes.append(pair)
    return changes


def _content_chars(text: str) -> str:
    """只保留"能念出来的内容字"，用于判断清洗前后是否丢了内容。"""
    return re.sub(r'[\W_]+', '', str(text or ""), flags=re.UNICODE)


def _log_tts_payload(config, original: str, clean_text: str, emotion: str, ref_path: str,
                     cloud: bool = False):
    """打印"LLM 交过来的完整内容 → 实际送去合成的文本"，便于排查少词问题。

    只要合成文本长度与原文不同（含只剩装饰符号被去掉的情况），就打印两边完整内容，
    这样"语音少词"的排查可以只看日志定位；内容与长度都没变时不刷屏。

    cloud=True 时不打印参考音频、文本语言、切分、语速这几项：它们是 GPT-SoVITS 的
    请求参数，云端请求里没有，打出来会让人误以为它们被送进了云端接口。
    """
    debug = str((config.get("tts_debug_log", "") if config is not None else "") or "").lower() \
        in ("true", "1", "yes", "on")
    changed = clean_text != original
    content_lost = _content_chars(original) != _content_chars(clean_text)
    length_changed = len(clean_text) != len(original)
    if not (changed or length_changed or debug or content_lost):
        return
    _safe_print(f"TTS 台词完整内容（LLM 传入，{len(original)} 字）: {original}")
    if changed:
        _safe_print(f"TTS 实际送去合成的文本（{len(clean_text)} 字）: {clean_text}")
    if content_lost:
        _safe_print(f"TTS 警告：清洗前后内容字不一致！"
                    f"原文={_content_chars(original)!r} 送合成={_content_chars(clean_text)!r}")
    elif length_changed:
        _safe_print(f"TTS 说明：仅去掉了装饰符号，内容字未变"
                    f"（{_content_chars(original)!r}）")
    if debug:
        if cloud:
            _safe_print(f"TTS 详细参数(云): 音色={ref_path} 字符数={len(clean_text)}")
        else:
            _safe_print(f"TTS 详细参数: 情绪={emotion} 参考音频={ref_path} "
                        f"text_lang={config.get('text_lang')} "
                        f"split={config.get('text_split_method')} "
                        f"speed={config.get('speed_factor')} 字符数={len(clean_text)}")


def _sanitize_tts_text(text: str, emotion: str = "", log: bool = True,
                       extra_map: dict = None) -> str:
    """清洗待合成文本：只做等价替换与无害剔除，绝不静默丢内容。

    凡是"能表达语气但朗读器不认"的符号都做等价替换（…—―♪ 等），
    而不是直接删除；只有真正无法合成的字符（emoji 等）才会被剔除，
    且一定打印日志，便于排查"语音比模型输出少了内容"这类问题。
    """
    original = str(text or "")
    mapped = _normalize_tts_chars(original, extra_map)
    if mapped != original and log:
        _safe_print(f"TTS 标点归一化: {_mapping_changes(original, extra_map)}")
    mapped = _collapse_tone_marks(mapped, log=log)
    cleaned = "".join(_TTS_ALLOWED_RE.findall(mapped))
    if cleaned != mapped:
        dropped = "".join(sorted(set(mapped) - set(cleaned)))
        core = re.sub(r'[\W_]+', '', cleaned, flags=re.UNICODE)
        if log:
            _safe_print(f"TTS 文本剔除了无法合成的字符 {dropped!r}"
                        f"（原文编码 {original[:40].encode('unicode_escape').decode('ascii')}）")
        if not core:
            return ""
    cleaned = _LEADING_JUNK_RE.sub("", cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return _TRAILING_WS_RE.sub("", cleaned)


# 句子终止标点（句号/问号/叹号/分号/半角对应符号）。
# 注意不把「…」「——」当句末：它们只是句中停顿，按它们切会切出"…"这种碎片片段。
_TTS_SENT_END_RE = re.compile(r'[。？！；?!;]')
_TTS_FRAGMENT_RE = re.compile(r'^[\s\u3000。，、,.!?！？…～~；;：:\-—―_*#]*$')


def split_tts_chunks(text: str, max_chars: int = 120, config=None) -> list:
    """把一段文本切成适合逐句 TTS 的片段。

    - 只在真正的句末标点（。？！；等）处切分，保证每句话完整；
    - 超长无标点的连续文本按 max_chars 硬切，避免 TTS 自己截断；
    - 纯标点碎片（如省略号被切开剩下的 "…"）会被丢弃，不单独合成。
    """
    extra_map = _configured_char_map(config)
    text = strip_urls_for_tts(text)
    text = _sanitize_tts_text(text, extra_map=extra_map)
    if not text:
        return []
    parts, cur = [], []
    for ch in text:
        cur.append(ch)
        if _TTS_SENT_END_RE.match(ch):
            parts.append("".join(cur))
            cur = []
    if cur:
        tail = "".join(cur)
        if parts:
            parts[-1] += tail
        else:
            parts.append(tail)
    out = []
    for part in parts:
        part = part.strip()
        while len(part) > max_chars:
            head, part = part[:max_chars], part[max_chars:].lstrip()
            out.append(head)
        if part:
            out.append(part)
    # 逐片再清洗一次：切句后新出现的行首标点（如「。～～でも」）需要剥离
    kept = [_sanitize_tts_text(p, log=False, extra_map=extra_map) for p in out]
    kept = [p for p in kept if p and not _TTS_FRAGMENT_RE.match(p)]
    # 全是标点碎片时退回原文本，至少保证"发出声音"而不是静默丢弃
    return kept or [text]


_LANG_PATTERNS = (
    ("ja", re.compile(r"[\u3040-\u30FF]")),
    ("ko", re.compile(r"[\uAC00-\uD7AF\u1100-\u11FF]")),
    ("zh", re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]")),
    ("en", re.compile(r"[A-Za-z]")),
)


def detect_text_lang(text: str, default: str = "ja") -> str:
    """按文本实际使用的文字判断合成语言（假名优先、其次谚文、汉字、拉丁）。"""
    t = str(text or "")
    counts = {code: len(pattern.findall(t)) for code, pattern in _LANG_PATTERNS}
    total = sum(counts.values())
    if not total:
        return default
    for code in ("ja", "ko"):
        if counts[code]:
            return code
    if counts["zh"] / total >= 0.25:
        return "zh"
    if counts["en"] / total >= 0.5:
        return "en"
    return default


def _readable_chars(text: str) -> int:
    return len(re.sub(r"[\W_]+", "", str(text or ""), flags=re.UNICODE))


def _min_expected_seconds(text: str, config) -> float:
    try:
        per_char = float(config.get("tts_min_seconds_per_char", 0.05) or 0.05)
    except (TypeError, ValueError, AttributeError):
        per_char = 0.05
    return max(0.3, _readable_chars(text) * per_char)


# 时长上限的绝对下限：短句（大量停顿标点、结巴式重复）本身字数少，
# 只按字数算上限会把正常音频误判成失控。
_MAX_SECONDS_FLOOR = 6.0


def _max_expected_seconds(text: str, config) -> float:
    """这条台词的合成时长上限；0 表示不做上限判断。"""
    try:
        per_char = float(config.get("tts_max_seconds_per_char", 0.6) or 0)
    except (TypeError, ValueError, AttributeError):
        per_char = 0.6
    if per_char <= 0:
        return 0.0
    return max(_MAX_SECONDS_FLOOR, _readable_chars(text) * per_char)


def _wav_duration(path) -> float:
    try:
        with wave.open(str(path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate > 0 and frames > 0:
                return frames / rate
    except Exception:
        return 0.0
    return 0.0


_AUDIO_CONTAINER_MAGIC = (b"RIFF", b"OggS", b"fLaC", b"ID3", b"FORM", b"ADIF")


def _looks_like_audio(path) -> bool:
    """文件头是已知音频容器时才认，避免把服务端报错页当语音发给发送层。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
    except Exception:
        return False
    if len(head) < 2:
        return False
    if head.startswith(_AUDIO_CONTAINER_MAGIC):
        return True
    return head[0] == 0xFF and (head[1] & 0xE0) == 0xE0


def _audio_too_long(path, text: str, config) -> bool:
    """合成时长远超台词本身的合理上限。

    事故背景：自回归 TTS 偶发进入"同一个音节无限重复"的失控状态，
    一句 30 多字的台词能合成出 27 秒的拖腔（末尾全是「に」「呜」）。
    音频本身是合法 wav，只能靠时长与台词的量级关系认出来 —— 这种结果
    必须丢弃并换切分方式重试，绝不能当成功语音发出去。
    """
    limit = _max_expected_seconds(text, config)
    if limit <= 0:
        return False
    duration = _wav_duration(path)
    if duration <= limit:
        return False
    _safe_print(f"TTS 合成时长异常偏长（{duration:.1f}s > 上限 {limit:.1f}s，"
                f"台词 {_readable_chars(text)} 字），疑似拖腔/复读，本段作废")
    return True


def _audio_too_short(path, text: str, config) -> bool:
    if not bool(config.get("tts_duration_guard", True)):
        return False
    duration = _wav_duration(path)
    if duration <= 0:
        return True
    expected = _min_expected_seconds(text, config)
    if duration + 1e-6 >= expected:
        return False
    _safe_print(f"TTS 合成音频过短（{duration:.2f}s < 期望 {expected:.2f}s），"
                f"文本={str(text)[:40]!r}")
    return True


async def synthesize_sentence(config, text: str, emotion: str, emotions: dict,
                              data_path: Path, stats=None,
                              mimic: str = "", mimics: dict = None) -> Optional[Path]:
    """合成一句话语音，返回 wav 路径；纯标点/空句子返回 None。

    参数顺序：config, text(要念的台词), emotion(情绪名), emotions(情绪表)。
    语言按台词实际使用的文字判定（tts_auto_lang），避免拿中文台词配日文语言
    模型合成出无法辨认的语音；合成结果过短时按备用切分方式重试，
    仍然拿不到可用音频就返回 None（调用方只发文本），绝不把半截语音发出去。

    tts_backend=cloud 时整句交给 modules.tts_cloud（云端不看参考音频与情绪表）。

    mimic 命中 mimics 时启用情绪模仿：情绪音频当主参考决定说话情绪，
    语气音频当辅助参考（按 emotion_mimic_voice_weight 重复加权）决定音色。
    """
    if isinstance(text, (dict, list)) or isinstance(emotion, (dict, list)):
        _safe_print("TTS arg order looks swapped (text/emotion); auto-corrected.")
        text, emotion = emotion, text
    text = str(text or "")
    emotion = str(emotion or "")
    text = strip_urls_for_tts(text)
    if not text:
        _safe_print("TTS skipped: 台词剔除链接后为空（纯链接句不合成，只发文本）")
        return None
    if emotion and emotion not in emotions and text in emotions:
        _safe_print(f"TTS arg order looks swapped (text={text!r}, emotion={emotion!r}); "
                    "auto-corrected.")
        text, emotion = emotion, text
    from .tts_cloud import is_cloud_tts, synthesize_cloud
    if is_cloud_tts(config):
        # 云端不需要参考音频，情绪只用来查音色映射，因此不校验它是否在情绪表里
        return await synthesize_cloud(config, text, emotion, data_path, stats=stats)
    default_voice = str(config.get("default_voice", "pingjing") or "pingjing")
    if emotion not in emotions:
        if emotion and emotion != default_voice:
            _safe_print(f"TTS emotion {emotion!r} not found; fallback to default_voice "
                        f"{default_voice!r}. available={list(emotions.keys())}")
        emotion = default_voice
    emotion_data = emotions.get(emotion)
    if not emotion_data:
        _safe_print(f"TTS emotion config missing: {emotion!r}")
        return None
    ref_path = emotion_data["ref_path"]
    prompt_text = emotion_data["prompt_text"]
    tone_ref_path, tone_prompt_text = ref_path, prompt_text
    mimic_key = ""
    aux_paths = []
    mimic_data = (mimics or {}).get(str(mimic or "")) if mimic else None
    if mimic_data:
        # 情绪音频当主参考（决定说话情绪），语气音频当辅助参考并重复加权：
        # GPT-SoVITS 对多参考音频的音色取平均，重复次数决定语气音色的占比
        try:
            weight = max(1, int(config.get("emotion_mimic_voice_weight", 4) or 1))
        except (TypeError, ValueError):
            weight = 4
        aux_paths = [ref_path] * weight
        mimic_key = str(mimic)
        candidates = [p for p in (mimic_data.get("candidates") or []) if p]
        # 一个情绪文件夹里可能放了多段情绪音频，每次随机挑一段
        ref_path = random.choice(candidates) if len(candidates) > 1 else (
            candidates[0] if candidates else mimic_data["ref_path"])
        prompt_text = mimic_data["prompt_text"]
        # 每段音频可以有自己的识别文字，用它才能对上所选音频的语调
        sidecar = (mimic_data.get("candidate_texts") or {}).get(
            str(ref_path).replace("\\", "/"))
        if sidecar:
            prompt_text = sidecar
    voice_tag = f"{emotion}+模仿:{mimic_key}" if mimic_key else emotion
    if not re.sub(r'[\s。，！？、,.!?…～~；;：:]+', '', text):
        _safe_print(f"TTS skipped: punctuation-only sentence "
                    f"({text.encode('unicode_escape').decode('ascii')})")
        return None
    clean_text = _sanitize_tts_text(text, emotion=emotion,
                                    extra_map=_configured_char_map(config))
    if not clean_text:
        _safe_print("TTS skipped: empty after sanitize "
                    f"({text[:30].encode('unicode_escape').decode('ascii')})")
        return None
    _log_tts_payload(config, text, clean_text, emotion, ref_path)
    cfg_lang = str(config.get("text_lang", "ja") or "ja")
    if bool(config.get("tts_auto_lang", True)):
        lang = detect_text_lang(clean_text, cfg_lang)
    else:
        lang = cfg_lang
    if lang != cfg_lang:
        _safe_print(f"TTS 语言按台词文字判定为 {lang}（配置为 {cfg_lang}）："
                    f"该句台词不是 {cfg_lang}（重译未成功或未启用），"
                    "按文字语言合成以免念成乱码")
    base_url = config.get("client_base_url", "http://127.0.0.1:9880")
    timeout = config.get("timeout_seconds", 120)
    split_default = str(config.get("text_split_method", "cut1") or "cut1")
    variants = ([split_default] + [v for v in ("cut5", "cut0", "cut2", "cut3")
                                   if v != split_default])[:3]
    attempts = [(variant, True) for variant in variants]
    if mimic_key:
        # 模仿用的参考音频不合规（如时长不在 3~10 秒）时退回纯语气，别让整句丢掉语音
        attempts.append((split_default, False))
    best_path = None
    best_duration = 0.0
    raw_path = None
    retry_delay = 1.0
    start = time.time()

    def _build_params(variant, use_mimic) -> dict:
        params = {
            "text": clean_text,
            "text_lang": lang,
            "ref_audio_path": _tts_reference(
                ref_path if use_mimic else tone_ref_path, config, data_path),
            "prompt_text": prompt_text if use_mimic else tone_prompt_text,
            "prompt_lang": config.get("prompt_lang", "ja"),
            "device": config.get("device", "cuda"),
            "top_k": config.get("top_k", 20),
            "top_p": config.get("top_p", 1),
            "temperature": config.get("temperature", 1),
            "text_split_method": variant,
            "batch_size": config.get("batch_size", 1),
            "batch_threshold": config.get("batch_threshold", 1),
            "split_bucket": config.get("split_bucket", True),
            "speed_factor": config.get("speed_factor", 1.0),
            "fragment_interval": config.get("fragment_interval", 0.5),
            "streaming_mode": config.get("streaming_mode", False),
            "seed": config.get("seed", -1),
            "parallel_infer": config.get("parallel_infer", True),
            "repetition_penalty": config.get("repetition_penalty", 1.35),
            "media_type": config.get("media_type", "wav")
        }
        if use_mimic and aux_paths:
            params["aux_ref_audio_paths"] = [
                _tts_reference(p, config, data_path) for p in aux_paths]
        return params

    for index, (variant, use_mimic) in enumerate(attempts):
        transient_left = 2 if index == 0 else 1
        while True:
            try:
                print(f"正在合成: 情绪={voice_tag if use_mimic else emotion} | "
                      f"语言={lang} | 切分={variant} | "
                      f"文本={clean_text} (第 {index + 1}/{len(attempts)} 次)")
                async with httpx.AsyncClient(timeout=timeout, trust_env=False,
                                             verify=verified_context()) as client:
                    resp = await client.get(f"{base_url}/tts",
                                            params=_build_params(variant, use_mimic))
            except Exception as e:
                if transient_left > 0:
                    transient_left -= 1
                    print(f"TTS 连接异常 ({type(e).__name__})，等待 {retry_delay:.0f} 秒后重试...")
                    await asyncio.sleep(retry_delay)
                    retry_delay += 1.0
                    from .tts_service import ensure_tts_service
                    await ensure_tts_service(config)
                    continue
                print(f"TTS 连接异常 ({type(e).__name__}: {e})，本次放弃该句语音。")
                break
            if resp.status_code == 200:
                temp_path = data_path / f"temp_{emotion}_{_unique_stamp()}.wav"
                temp_path.write_bytes(resp.content)
                _normalize_tts_output(temp_path, config)
                duration = _wav_duration(temp_path)
                too_long = _audio_too_long(temp_path, clean_text, config)
                if not too_long and not _audio_too_short(temp_path, clean_text, config):
                    if stats:
                        stats.record_tts((time.time() - start) * 1000)
                    print(f"合成完成: {voice_tag} | {clean_text[:30]}...")
                    for extra in (best_path, raw_path):
                        if extra is not None:
                            extra.unlink(missing_ok=True)
                    return temp_path
                if duration <= 0:
                    if raw_path is None:
                        raw_path = temp_path
                    else:
                        temp_path.unlink(missing_ok=True)
                    print("TTS 返回的内容不是可解析的音频，换一种切分方式重试。")
                elif too_long:
                    # 偏长的一律不进 best_path：宁可这句不发语音，
                    # 也不能把几十秒的拖腔当成"最长的一段"挑出来发出去
                    temp_path.unlink(missing_ok=True)
                    print("本次合成作废，换一种切分方式重试。")
                elif duration > best_duration:
                    if best_path is not None:
                        best_path.unlink(missing_ok=True)
                    best_path, best_duration = temp_path, duration
                else:
                    temp_path.unlink(missing_ok=True)
                break
            retryable = resp.status_code in (408, 429) or resp.status_code >= 500
            if retryable and transient_left > 0:
                transient_left -= 1
                print(f"TTS 服务繁忙（HTTP {resp.status_code}），等待 {retry_delay:.0f} 秒后重试...")
                await asyncio.sleep(retry_delay)
                retry_delay += 1.0
                if transient_left == 0:
                    from .tts_service import ensure_tts_service
                    await ensure_tts_service(config)
                continue
            print(f"TTS 合成失败: {resp.status_code} - {str(resp.text)[:120]} | 文本={clean_text[:60]}")
            break
    if best_path is not None:
        _safe_print(f"TTS 多次合成都偏短，仍返回其中最长的一段（{best_duration:.2f}s），"
                    "以免整句语音完全缺失。")
        if raw_path is not None:
            raw_path.unlink(missing_ok=True)
        if stats:
            stats.record_tts((time.time() - start) * 1000)
        return best_path
    if raw_path is not None:
        if _looks_like_audio(raw_path):
            _safe_print("TTS 返回的不是 wav 容器（如 mp3/ogg），无法按时长校验，原样交给发送层。")
            if stats:
                stats.record_tts((time.time() - start) * 1000)
            return raw_path
        raw_path.unlink(missing_ok=True)
        _safe_print("TTS 返回的内容不是音频（多为服务端报错页），已丢弃，本句改为只发文本。")
    print(f"TTS 合成失败: 未能得到可用音频 | 文本={clean_text[:60]}")
    return None


_STAMP_SEQ = 0
_STAMP_LOCK = threading.Lock()


def _unique_stamp() -> int:
    """毫秒时间戳 + 自增序号，保证同一毫秒内多次合并也不会覆盖同名文件。

    事故背景：合并与合成都用 `int(time.time()*1000)` 命名，同一毫秒内连续两次
    调用会拿到同样的文件名，后一次直接覆盖前一次 —— 表现为"刚合成的语音内容
    对不上/被截断"。加一个进程内自增序号即可根治；序号是读-改-写，
    多线程（合成线程与缓存清理线程）并发时会读到同一个值，因此必须持锁。
    """
    global _STAMP_SEQ
    with _STAMP_LOCK:
        now = int(time.time() * 1000)
        if now <= _STAMP_SEQ:
            now = _STAMP_SEQ + 1
        _STAMP_SEQ = now
        return now


def simple_concat_wavs(wav_paths: list, data_path: Path) -> Optional[Path]:
    """无损直接拼接（不做任何渐变/裁剪）：兜底用，保证一句都不会少。"""
    wav_paths = [w for w in (wav_paths or []) if w]
    if not wav_paths:
        return None
    output_path = data_path / f"concat_{_unique_stamp()}.wav"
    try:
        data = []
        for wav_path in wav_paths:
            with wave.open(str(wav_path), 'rb') as wf:
                data.append([wf.getparams(), wf.readframes(wf.getnframes())])
        with wave.open(str(output_path), 'wb') as out:
            out.setparams(data[0][0])
            for _params, frames in data:
                out.writeframes(frames)
        return output_path
    except Exception as e:
        print(f"合并音频失败: {type(e).__name__}: {e}")
        return None


# 段间裁剪静音的判定阈值（int16 幅度）。TTS 每段首尾都会带一段数字静音，
# 先裁掉它再交叉渐变，渐变就不会啃掉真实语音。
_SILENCE_LEVEL = 240


def _trim_silence(audio, channels: int, lead: bool = False, trail: bool = False,
                  keep_ms: int = 60, sample_rate: int = 16000):
    """裁掉音频开头/结尾的**纯数字静音**（保留 keep_ms 的呼吸感）。

    只裁无声区，任何有声内容（哪怕只是气声）都不会被裁掉；整段静音时原样返回。

    注意：判定数组必须压成一维。numpy 的 argmax 在多维数组上返回的是**扁平索引**，
    直接拿它当帧号会在多声道（(N,2)）音频上算出偏大的位置，把该裁的静音留下来
    （表现为"合并后多出一段空白"）。
    """
    if audio is None or len(audio) == 0:
        return audio
    mono = np.abs(audio.astype(np.int64)).max(axis=1) if channels > 1 \
        else audio[:, 0] if audio.ndim > 1 else audio
    loud = np.abs(np.asarray(mono, dtype=np.int64)) > _SILENCE_LEVEL
    loud = np.ravel(loud)
    if not loud.any():
        return audio
    keep = max(0, int(sample_rate * keep_ms / 1000))
    start, end = 0, len(audio)
    if lead:
        start = max(0, int(np.argmax(loud)) - keep)
    if trail:
        last = len(loud) - 1 - int(np.argmax(loud[::-1]))
        end = min(len(audio), last + 1 + keep)
    if end <= start:
        return audio
    return audio[start:end]


def merge_wavs(wav_paths: list, config, data_path: Path,
               crossfade_ms=None) -> Optional[Path]:
    """把多段语音合并成一条。

    语气渐变（voice_transition）下的历史 bug ——「每个句子之间少了很多字眼」：
    旧实现把「上一段的结尾」和「下一段的开头」**真实重叠** crossfade_ms 后拼接，
    等于每拼一句就凭空丢掉 crossfade_ms 的音频；而 TTS 每段首尾本就带静音，
    重叠区里常常还有真实语音，默认 300ms 正好是半个词到一个词的长度。

    现在：先裁掉各段首尾的纯数字静音，再做**等长交叉淡化**，重叠长度同时受
    上一段可用长度与下一段总长度约束，任何情况下都不丢音频内容。
    """
    wav_paths = [w for w in (wav_paths or []) if w]
    if not wav_paths:
        return None
    if crossfade_ms is None:
        try:
            crossfade_ms = float(config.get("crossfade_ms", 300) or 0)
        except (TypeError, ValueError):
            crossfade_ms = 300.0
    if not config.get("voice_transition", True) or crossfade_ms <= 0:
        return simple_concat_wavs(wav_paths, data_path)
    output_path = data_path / f"combined_{_unique_stamp()}.wav"
    try:
        with wave.open(str(wav_paths[0]), 'rb') as wf:
            sample_rate = wf.getframerate()
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            all_frames = wf.readframes(wf.getnframes())
        if not sample_rate or not n_channels:
            return simple_concat_wavs(wav_paths, data_path)
        if sampwidth != 2:
            print(f"音频位深为 {sampwidth * 8} bit，非 16 bit PCM，改用无损直接拼接。")
            return simple_concat_wavs(wav_paths, data_path)
        try:
            breathing_gap_samples = max(0, int(
                sample_rate * float(config.get("breathing_gap_ms", 100)) / 1000))
        except (TypeError, ValueError):
            breathing_gap_samples = int(sample_rate * 0.1)
        crossfade_samples = max(0, int(sample_rate * float(crossfade_ms) / 1000))
        all_audio = np.frombuffer(all_frames, dtype=np.int16).copy().reshape(-1, n_channels)
        all_audio = _trim_silence(all_audio, n_channels, trail=True, sample_rate=sample_rate)
        for i in range(1, len(wav_paths)):
            with wave.open(str(wav_paths[i]), 'rb') as wf:
                if wf.getframerate() != sample_rate or wf.getnchannels() != n_channels \
                        or wf.getsampwidth() != sampwidth:
                    print("检测到采样率/声道数/位深不一致的音频，已跳过该段以免合并错乱。")
                    continue
                frames = wf.readframes(wf.getnframes())
            audio = np.frombuffer(frames, dtype=np.int16).copy().reshape(-1, n_channels)
            audio = _trim_silence(audio, n_channels, lead=True, trail=True,
                                  sample_rate=sample_rate)
            if len(audio) == 0:
                continue
            if breathing_gap_samples > 0:
                all_audio = np.concatenate(
                    (all_audio,
                     np.zeros((breathing_gap_samples, n_channels), dtype=np.int16)),
                    axis=0)
            # 等长交叉淡化：重叠区取「上一段尾部 ov」与「下一段头部 ov」等长混合，
            # 输出长度 = 上段 + 下段 - ov，任何一帧都不会被丢下
            ov = min(crossfade_samples, len(all_audio), len(audio))
            if getattr(config, "_debug_merge", False):
                print(f"[merge] i={i} acc={len(all_audio)} audio={len(audio)} "
                      f"cs={crossfade_samples} ov={ov}")
            if ov <= 0:
                all_audio = np.concatenate((all_audio, audio), axis=0)
                continue
            head = all_audio[:-ov]
            tail = all_audio[-ov:]
            seg_out = tail.astype(np.float32)
            seg_in = audio[:ov].astype(np.float32)
            gradient = ((1 - np.cos(np.linspace(0, np.pi, ov))) / 2).reshape(-1, 1)
            mixed = (seg_out * (1.0 - gradient) + seg_in * gradient).astype(np.int16)
            all_audio = np.concatenate((head, mixed, audio[ov:]), axis=0)
        with wave.open(str(output_path), 'wb') as out:
            out.setnchannels(n_channels)
            out.setsampwidth(sampwidth)
            out.setframerate(sample_rate)
            out.writeframes(all_audio.tobytes())
        return output_path
    except ImportError:
        print("未安装 numpy，正在使用基础拼接。建议 pip install numpy 以启用平滑语气渐变。")
        return simple_concat_wavs(wav_paths, data_path)
    except Exception as e:
        print(f"合并音频失败（改用无损直接拼接）: {type(e).__name__}: {e}")
        return simple_concat_wavs(wav_paths, data_path)
