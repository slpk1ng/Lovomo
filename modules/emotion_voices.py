# -*- coding: utf-8 -*-
"""情绪/模仿参考音频扫描、音频质量体检与 EmotionManager（情绪目录发现）。

自 main.py 原样搬迁（refactor_plan 第 8 节）：参考音频根目录扫描、
参考音频质量体检、情绪模仿候选收集与情绪/模仿目录发现管理器。
"""
import os
from pathlib import Path

from .audio_level import (SPEECH_MIN_RATIO, measure as measure_audio,
                          pitch_note, quality_notes as audio_quality_notes)
from .tts import resolve_tts_path
from .tts_cloud import cloud_emotion_names, is_cloud_tts


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
        if is_cloud_tts(self.config):
            # 云端合成不看参考音频，情绪清单改由「默认情绪 + 音色映射表」推导
            for name in cloud_emotion_names(self.config):
                self.emotions.setdefault(name, {"ref_path": "", "prompt_text": ""})
            print(f"云端 TTS：可用情绪 {list(self.emotions.keys())}")
            return
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
