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
import zipfile
from base64 import b64encode, b64decode
from pathlib import Path
from typing import Optional, Dict, Any, List

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
from modules.stickers import StickerManager, IMAGE_EXTS
from modules.tools import ToolRegistry
from modules.profiles import UserProfileManager, DEFAULT_EXTRACT_PROMPT
from modules.rag import RAGManager, extract_text_from_file
from modules.todo_manager import TodoManager, DEFAULT_EXTRACT_PROMPT as TODO_EXTRACT_PROMPT
from modules.jobs import ScheduledJobManager, generate_proactive_text
from modules.events import EventManager
from modules.mood import MoodManager, judge_and_decide
from modules.sender import MessageSender
from modules.llm_helpers import (RoleContext, build_chat_messages, chat_once,
                                chat_with_tools, normalize_sentences,
                                normalize_single, sentence_obj_has_text,
                                split_multi_clause_sentences,
                                stream_chat,
                                SentenceStreamParser, get_image_reply, download_image,
                                sniff_image_mime, repair_sentence_lang, strip_quote_note,
                                segment_for_tts, speaker_labeled_lines,
                                text_needs_tools, tool_flow_can_skip,
                                image_self_claim, image_identity_note,
                                IMAGE_CLAIM_WARNING, sent_links, record_sent_links,
                                urls_in_text, is_search_request, is_search_dissatisfied,
                                lang_text_broken, translate_to_lang)
from modules.tts import synthesize_sentence, get_audio_duration, resolve_tts_path
from modules.tts_service import (process_manager, ensure_tts_service,
                                 auto_start_and_switch_tts)

logging.basicConfig(filename='app.log', level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

last_proactive_sent: Dict[str, float] = {}

def get_resource_path(relative_path):
    if hasattr(sys, '_MEIPASS'):
        base_path = Path(sys._MEIPASS)
    else:
        base_path = Path(__file__).parent
    return base_path / relative_path


def _pid_alive(pid) -> bool:
    """跨平台判断 pid 对应的进程是否仍在运行。"""
    if not pid or int(pid) <= 0:
        return False
    pid = int(pid)
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
                return bool(ok) and code.value == 259  # STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
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

global_log_buffer = []
LOG_BUFFER_MAX = 5000     # 日志缓冲行数默认值（config: webui_log_buffer_lines）
LOG_TAIL_DEFAULT = 500    # 精简模式尾部行数默认值（config: webui_log_tail_lines）
_LOG_FULL_MAX_CHARS = 400_000   # "显示完整日志"的极端字符上限（仅防响应撑爆）
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
            for line in message.splitlines(True):
                global_log_buffer.append(line.rstrip('\n'))
                if len(global_log_buffer) > buffer_cap:
                    del global_log_buffer[:len(global_log_buffer) - buffer_cap]

    def flush(self):
        if self.original_stream is not None:
            try:
                self.original_stream.flush()
            except Exception:
                pass


_API_KEY_KEYS = ("llm_api_key", "napcat_token", "web_search_api_keys")

_ENC_PREFIX = "enc2:"
_ENC_KEY_CACHE = None


def _enc_key() -> bytes:
    """加密密钥绑定本机（Windows MachineGuid 等），config.json 拷到别的机器解不开。"""
    global _ENC_KEY_CACHE
    if _ENC_KEY_CACHE is None:
        material = b"LTVM-KEYSTORE-v2"
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
                                             b"LTVM-KEYSTORE-SALT-v2", 100_000)
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
    raw = v.encode("utf-8")
    nonce = os.urandom(16)
    checksum = hashlib.sha256(raw).digest()[:8]
    ks = _keystream(nonce, len(raw))
    ct = bytes(a ^ b for a, b in zip(raw, ks))
    return _ENC_PREFIX + b64encode(nonce + checksum + ct).decode("ascii")


def _decrypt_value(value: str) -> str:
    v = str(value or "")
    if v.startswith("enc:"):
        try:
            return b64decode(v[4:].encode("ascii")).decode("utf-8")
        except Exception:
            return ""
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
        print(f"[加密] API 密钥解密失败，已置空（请重新在 WebUI 填写）：{e}")
        return ""


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
    return config


def _decrypt_api_keys(config: dict) -> dict:
    for key in _API_KEY_KEYS:
        val = config.get(key)
        if isinstance(val, dict):
            config[key] = {k: _decrypt_value(v) if isinstance(v, str) else v
                           for k, v in val.items()}
        elif isinstance(val, str):
            config[key] = _decrypt_value(val)
    return config


class ConfigLoader:
    def __init__(self, config_path: str = "config.json"):
        self.config_path = Path(config_path)
        self.config = self._load_or_init()
        # 多角色配置解析
        self.active_character = self.config.get("active_character", self.config.get("character_key", "murasame"))
        self.roles = self._parse_roles()

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
                "text_lang": self.config.get("text_lang", "ja")
            }]

        for role_cfg in roles_config:
            key = role_cfg.get("character_key", "")
            if not key:
                continue
            roles[key] = {
                "character_name": role_cfg.get("character_name", "丛雨"),
                "character_key": key,
                "personality_prompt": role_cfg.get("personality_prompt", self.config.get("personality_prompt", "")),
                "json_prompt": role_cfg.get("json_prompt", self.config.get("json_prompt", "")),
                "supplement_prompt": role_cfg.get("supplement_prompt", self.config.get("supplement_prompt", "")),
                "default_voice": role_cfg.get("default_voice", "pingjing"),
                "ref_audio_root": role_cfg.get("ref_audio_root", ""),
                "text_lang": role_cfg.get("text_lang", "ja"),
                "prompt_lang": role_cfg.get("prompt_lang", "")
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
                _decrypt_api_keys(config)
                if not Path(config.get("ref_audio_root", "")).exists():
                    print(f"⚠️ 参考音频目录无效：{config.get('ref_audio_root')}")
                    if can_interact():
                        return self._interactive_init(config)
                    else:
                        return self._auto_save_default(config)
                return config
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
        return {
            "hide_gsv_options": False,
            "llm_model_name": "",
            "image_caption_model_name": "",
            "image_caption_backend": "",
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
            "timeout_seconds": 120,
            "prompt_text": "ふむ、おぬしが我輩のご主人か?",
            "prompt_lang": "ja",
            "text_lang": "ja",
            # 按台词实际使用的文字选择合成语言（中文台词不会再用日文语言模型合成）
            "tts_auto_lang": True,
            # 合成音频短得离谱时按备用切分方式重试，避免只有一声语气的短语音
            "tts_duration_guard": True,
            "tts_min_seconds_per_char": 0.05,
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
            "supplement_prompt": "回答自然、简短，通常两到五句话(一个句号才算一句话)；不要重复最近说过的话，不要加入动作、旁白或括号舞台说明；生成的回复要符合当前对话，不能出现主谓宾不分，乱序的情况。【情绪判断规则】请仔细阅读最近对话历史，结合你（角色）的性格特点来判断情绪！如果主人对你亲昵（如摸头、夸奖），即使你嘴上说“我才没有”，情绪也应该是害羞或高兴；如果主人故意逗你、骂你或惹你生气，情绪应该是生气或着急；如果只是平淡陈述，使用平静。【翻译一致性要求】必须表达完全相同的含义和语气，绝对不能出现含义相反或意思不匹配的翻译！【情绪连贯性强制规则】如果用户明确地侮辱、挑衅或激怒你（例如叫你“幼刀、搓衣板、飞机场”），你的情绪必须保持连贯。即：整句话所有分句的情绪必须都是“生气”或“着急”，绝对不能把后半句的“命令/威胁”改成“害羞”或“高兴”！除非你明确使用了“但是”、“不过”等转折词，否则不要轻易切换成其他情绪。【情绪匹配规则】情绪文件夹可能是拼音（如 gaoxing），也可能是英文（如 happy）。你必须严格只输出我在【情绪可选列表】中提供的单词，绝对不能输出中文汉字或拼音简写！",
            "max_voice_cache": 20,
            "isolated_session": False,
            "separate_send": False,
            "send_voice_separately": False,
            "text_separate": False,
            "dynamic_sleep": True,
            "only_private": False,
            "group_need_at": True,
            "auto_start_tts": True,
            "tts_start_script": "",
            "device": "cuda",
            "llm_judge": True,
            "display_lang": "zh",
            "default_voice": "pingjing",
            "voice_transition": True,
            "breathing_gap_ms": 100,
            "crossfade_ms": 300,
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
                    "supplement_prompt": "回答自然、简短，通常两到五句话(一个句号才算一句话)；不要重复最近说过的话，不要加入动作、旁白或括号舞台说明；生成的回复要符合当前对话，不能出现主谓宾不分，乱序的情况。【情绪判断规则】请仔细阅读最近对话历史，结合你（角色）的性格特点来判断情绪！如果主人对你亲昵（如摸头、夸奖），即使你嘴上说“我才没有”，情绪也应该是害羞或高兴；如果主人故意逗你、骂你或惹你生气，情绪应该是生气或着急；如果只是平淡陈述，使用平静。【翻译一致性要求】必须表达完全相同的含义和语气，绝对不能出现含义相反或意思不匹配的翻译！【情绪连贯性强制规则】如果用户明确地侮辱、挑衅或激怒你（例如叫你“幼刀、搓衣板、飞机场”），你的情绪必须保持连贯。即：整句话所有分句的情绪必须都是“生气”或“着急”，绝对不能把后半句的“命令/威胁”改成“害羞”或“高兴”！除非你明确使用了“但是”、“不过”等转折词，否则不要轻易切换成其他情绪。【情绪匹配规则】情绪文件夹可能是拼音（如 gaoxing），也可能是英文（如 happy）。你必须严格只输出我在【情绪可选列表】中提供的单词，绝对不能输出中文汉字或拼音简写！",
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
            # 防复读：把最近几轮的自己台词一起作为"禁止重复"的参照
            "repeat_guard_rounds": 3,
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
            "sticker_probability": 1.0,
            "sticker_max_per_reply": 1,
            "sticker_every_sentence": False,
            "sticker_capture_enabled": False,
            "sticker_capture_prompt": "附加收藏指令：如果你认为这张图片有趣、可爱、有梗或有纪念意义，请在输出完主要回复JSON之后，再单独输出一个JSON对象（不要放进sentences数组），格式：{\"sticker_capture\": true, \"category\": \"分类名\", \"reason\": \"一句话理由\"}。category必须严格只从以下8个拼音中选一个：gaoxing, shengqi, haixiu, wuyu, jingya, sajiao, weixie, pingjing。绝对禁止输出其他任何拼音、中文或英文！禁止填default，禁止填any！如果拿不准，直接填pingjing！",
            "sticker_capture_min_score": 0.7,
            "sticker_capture_require_verdict": True,
            "sticker_capture_min_reason_chars": 6,
            "sticker_capture_skip_if_unfit": True,
            "sticker_capture_any_pool": True,
            "sticker_capture_preserve_formats": "gif,webp",
            "sticker_capture_max_side": 400,
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
            "webui_log_buffer_lines": 5000,
            "webui_log_tail_lines": 500,
            "separate_force_segment": True,
            "tools_guard_enabled": True,
            "tools_guard_keywords": "几点\n现在几点\n时间\n日期\n几号\n星期几\n计算\n算一下\n等于多少\n平方根\n根号\n天气\n气温\n温度\n降雨\n搜索\n查一下\n查找\n网址\n网页\n链接\n工具\n下载",
            "anti_spam_enabled": False,
            "anti_spam_window_seconds": 10,
            "anti_spam_max_in_window": 5,
            "update_check_enabled": True,
            "update_check_interval_hours": 24,
            "update_include_prerelease": False
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


class EmotionManager:
    def __init__(self, config):
        self.config = config
        self.ref_audio_root = resolve_tts_path(config.get("ref_audio_root", "C:/tts"))
        self.default_voice = config.get("default_voice", "pingjing")
        self.emotions = {}
        self._discover_emotions()
        self._apply_manual_emotions()

    def _discover_emotions(self):
        base_folder = Path(self.ref_audio_root)
        if not base_folder.exists():
            print(f"警告：参考音频根目录不存在：{self.ref_audio_root}")
            return
        if _is_reserved(base_folder) or base_folder.name in {"WpSystem", "System Volume Information", "$Recycle.Bin", "Recovery", "PerfLogs", "Config.Msi"}:
            print(f"错误：{self.ref_audio_root} 是系统保护目录，无法访问！")
            return
        ignore_dirs = {"WpSystem", "System Volume Information", "$Recycle.Bin", "Recovery", "PerfLogs", "Config.Msi"}
        try:
            for folder in base_folder.iterdir():
                if folder.name in ignore_dirs or folder.name.startswith("$"):
                    continue
                try:
                    if not folder.is_dir():
                        continue
                except PermissionError:
                    continue
                emotion_name = folder.name
                ref_audio = None
                prompt_text = ""
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
                        candidate = folder / f"{emotion_name}.mp3"
                        if not candidate.exists():
                            candidate = folder / f"{emotion_name}.wav"
                        if candidate.exists():
                            ref_audio = candidate
                    except (PermissionError, OSError):
                        continue
                if ref_audio:
                    asr_path = folder / "asr.txt"
                    if asr_path.exists():
                        try:
                            prompt_text = asr_path.read_text(encoding='utf-8', errors='ignore').strip()
                        except Exception:
                            prompt_text = ""
                    if not prompt_text:
                        prompt_text = self.config.get("prompt_text", "ふむ、おぬしが我輩のご主人か?")
                    self.emotions[emotion_name] = {
                        "ref_path": str(ref_audio).replace("\\", "/"),
                        "prompt_text": prompt_text
                    }
        except Exception as e:
            print(f"扫描目录异常：{e}")
        if self.emotions:
            print(f"成功扫描到 {len(self.emotions)} 个情绪配置: {list(self.emotions.keys())}")
        else:
            print(f"警告：未在 {self.ref_audio_root} 下找到任何情绪配置")

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
        return self.emotions.get(name, self.emotions.get(self.default_voice))


class MemoryManager:
    def __init__(self, config: ConfigLoader):
        self.config = config
        self.data_path = Path(config.get("memory_data_path", "./data")).resolve()
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.character_key = config.get("character_key", "murasame")
        self.character_name = config.get("character_name", "丛雨")
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
                    data.setdefault("history", [])
                    data.setdefault("meta", {})
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
        tmp = Path(str(file_path) + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(str(tmp), str(file_path))

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
                if not re.match(r'^[A-Za-z0-9_\-]+_(private|group)_[A-Za-z0-9_\-]+\.json$', f.name):
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
        if not re.match(r'^[A-Za-z0-9_\-]+\.json$', filename):
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
        if not re.match(r'^[A-Za-z0-9_\-]+\.json$', filename):
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
        if not re.match(r'^[A-Za-z0-9_\-]+\.json$', filename):
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
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
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
rag_mgr: Optional[RAGManager] = None
todo_mgr: Optional[TodoManager] = None
job_mgr: Optional[ScheduledJobManager] = None
event_mgr: Optional[EventManager] = None
mood_mgr: Optional[MoodManager] = None
sender: Optional[MessageSender] = None
napcat_client = None
scheduler: SchedulerManager = get_scheduler()

last_interaction: Dict[str, float] = {}   # session_id -> 最后交互时间（含机器人主动发送）
last_user_activity: Dict[str, float] = {} # session_id -> 用户最后发言时间（主动消息依据）
proactive_counts: Dict[str, int] = {}     # "date|session_id" -> 当日主动消息次数
proactive_pending: Dict[str, float] = {}  # session_id -> 计划发送时刻（绝对时间戳）
proactive_awaiting: set = set()           # 已发主动消息但用户还没回复的会话（回复前不再主动）
_spam_log: Dict[str, list] = {}           # session_id -> [时间戳,...]
_role_emotions_cache: Dict[str, dict] = {}
_PROACTIVE_STATE_FILE = "proactive_state.json"
_proactive_state_date = ""                # 已落盘的日期，跨天时重置计数


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
    acts = data.get("user_activity", {})
    if isinstance(acts, dict):
        for k, v in acts.items():
            try:
                last_user_activity[str(k)] = float(v)
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
        payload = {
            "date": _proactive_state_date or time.strftime("%Y-%m-%d"),
            "counts": {k: v for k, v in proactive_counts.items()
                       if k.startswith(_proactive_state_date)},
            "user_activity": last_user_activity,
            "awaiting": sorted(proactive_awaiting),
            "pending": {k: v for k, v in proactive_pending.items()
                        if isinstance(v, (int, float))},
        }
        path = _proactive_state_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
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


def get_role_emotions(role: dict) -> dict:
    """按角色获取情绪配置（各角色可有独立 ref_audio_root），带缓存。"""
    key = (role or {}).get("character_key", "")
    if not key or global_emotion_manager is None:
        return get_active_emotions()
    if key not in _role_emotions_cache:
        try:
            mgr = EmotionManager(RoleContext(global_config.config, role))
            _role_emotions_cache[key] = mgr.emotions or get_active_emotions()
        except Exception as e:
            print(f"扫描角色 {key} 情绪失败: {e}")
            _role_emotions_cache[key] = get_active_emotions()
    return _role_emotions_cache[key]


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
        self.sticker_sent = False

    def _looks_repeat(self, sentence: dict) -> bool:
        zh = str(sentence.get("zh") or "").strip()
        if not zh:
            return False
        if any(_repeat_ratio(ref, zh) >= _SELF_REPEAT_THRESHOLD
               for ref in self.last_replies):
            return True
        u = str(self.user_text or "")
        if u and "[图片]" not in u and len(_norm_text(u)) >= _USER_ECHO_MIN_CHARS \
                and _repeat_ratio(u, zh) >= _USER_ECHO_THRESHOLD:
            return True
        return False

    async def on_sentence(self, sentence: dict):
        if not self._first_checked:
            self._first_checked = True
            if self._looks_repeat(sentence):
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
        wav = None
        if await self._tts_available():
            start = time.time()
            # 参数顺序：text=要念的台词(sentence["lang"])，emotion=情绪名。
            # 早期版本此处传反，情绪被当成台词、台词被当成情绪，
            # 导致情绪永远回退 default_voice（语音情绪与文本标注不一致）。
            wav = await synthesize_sentence(self.ctx, await self._speech_text(sentence),
                                            sentence.get("emotion", ""),
                                            self.emotions, memory_manager.data_path,
                                            stats=stats_mgr)
            self.tts_ms += (time.time() - start) * 1000
            if wav:
                self.tts_calls += 1
        if wav:
            await sender.send_text(self.session_type, self.target_id, sentence["display"])
            await sender.send_voice(self.session_type, self.target_id, wav)
            if global_config.get("dynamic_sleep", True):
                await asyncio.sleep(get_audio_duration(str(wav)) + 0.5)
            else:
                await asyncio.sleep(0.2)
            wav.unlink(missing_ok=True)
        else:
            await sender.send_text(self.session_type, self.target_id, sentence["display"])
        self.sent += 1
        self.sent_sentences.append(sentence)

    async def _send_pending_sticker(self):
        if self.sticker_sent or not self.pending_sticker_emotion or sticker_mgr is None:
            return
        self.sticker_sent = True
        sticker = sticker_mgr.pick(self.pending_sticker_emotion)
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


async def _sticker_category_from_llm(ctx: RoleContext, image_result: dict) -> str:
    """让模型给这张图挑一个表情包分类。
      1. 分类说明换成带"使用场景"的版本，并给出分类之间的区分要点；
      2. 模型输出不合法时不再无脑回退 pingjing，而是返回空串 ——
         空串会让上层走"不收藏/默认分类"，避免把无法判断的图硬塞进某个情绪目录。
    """
    description = str(image_result.get("description", "") or "").strip()
    reply_text = "".join(s.get("zh", "") for s in image_result.get("sentences", []))
    if not description and not reply_text:
        return ""
    from modules.stickers import STRICT_ALLOWED, category_guide_text, CATEGORY_DISAMBIGUATION
    cats = sorted(STRICT_ALLOWED)
    try:
        context_parts = []
        if description:
            context_parts.append(f"图片内容：{description}")
        if reply_text:
            context_parts.append(f"角色回复：{reply_text}")
        classify_prompt = (
            f"{'；'.join(context_parts)}\n"
            "请判断这张图【将来被当作表情包发出去时，发图一方的情绪/使用场景】"
            "（使用者发图时的语气，不是画面中角色此刻的情绪）。"
            "画面人物的动作、表情与文字往往指向互动用途（挑逗、撩、调戏、嘲讽、炫耀、"
            "撒娇等），要据此归类。"
            "如果这张图其实是纯风景/空镜/静物/无文字无表情的随手拍，"
            "或者画面阴森、恐怖、诡异、病态、压抑，且没有明确的互动用途，"
            "就回答「none」表示不适合当表情包，不要硬选一个分类。"
            f"{CATEGORY_DISAMBIGUATION}"
            f"可以收藏时，从以下拼音中选择最匹配的一个：{category_guide_text()}。"
            "只输出一个分类名（拼音）或 none，不要其他任何内容。"
        )
        result = await chat_once(ctx, [{"role": "user", "content": classify_prompt}])
        raw = str(result.get("content") or "").strip().lower()
        if re.search(r"\bnone\b|不适合|不收藏|无法归类|没有互动用途", raw) \
                and not any(c in raw for c in cats):
            print(f"【表情收藏-分类】模型判定不适合当表情包：{raw[:60]!r}")
            return ""
        for cat in cats:
            if cat in raw:
                return cat
    except Exception as e:
        print(f"表情分类失败: {type(e).__name__}: {e}")
    print("【表情收藏-分类】未能得到有效分类，按不收藏处理（不再默认 pingjing）。")
    return ""


async def generate_reply(ctx: RoleContext, emotions: dict, user_text: str, history: list,
                         images: Optional[list], extra_parts: List[str],
                         user_id: str = "", on_sentence=None,
                         session_id: str = "") -> Optional[dict]:
    """生成回复：识图 / 工具调用 / 流式 / 普通四种路径统一入口。"""
    if images is not None and not str(user_text or "").strip():
        user_text = "[图片]"
    if images is not None:
        result = await get_image_reply(ctx, user_text, history, emotions, images,
                                       extra_parts=extra_parts, stats=stats_mgr)
        if result is not None:
            out = {"sentences": result["sentences"], "llm_ms": result.get("ms", 0), "tool_trace": []}
            if result.get("description"):
                out["description"] = result["description"]
            if result.get("capture"):
                out["capture"] = result["capture"]
            return out
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


def resolve_target_roles(user_text: str, is_private: bool) -> List[dict]:
    """多角色路由：私聊始终当前角色；群聊按消息中出现的角色名路由。"""
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


_SELF_REPEAT_THRESHOLD = 0.85
_USER_ECHO_THRESHOLD = 0.8
_USER_ECHO_MIN_CHARS = 5
_RETRY_TEMPERATURE = 1.3
_RETRY_TEMPERATURE_MAX = 1.4


async def auto_capture_from_images(ctx: RoleContext, capture: dict, image_urls: list,
                                   image_result: Optional[dict] = None):
    from modules.stickers import auto_capture_image
    source = ""
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
    if not source:
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
        if not category and image_result:
            category = await _sticker_category_from_llm(ctx, image_result)
        # 分类判定为"不适合当表情包"时直接跳过：
        # 以前这种情况会被兜底塞进 wuyu 目录，等于把无关图片污染表情库。
        if not category and bool(ctx.get("sticker_capture_skip_if_unfit", True)):
            print("【表情收藏】这张图未被判定为适合当表情包，跳过收藏。")
            return
        print(f"【表情收藏-自动触发】图片来源: {source[:50]}... 原始分数: {score}, "
              f"分类: {category or '(空→兜底分类)'}")
        await auto_capture_image(ctx, sticker_mgr, source, category)
    except Exception as e:
        print(f"表情收藏失败: {type(e).__name__}: {e}")


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


def _discard_unreplied_user_message(history: list, user_text: str) -> bool:
    """本轮一条回复都没产生时，把刚追加的这条用户消息从历史里摘掉。

    历史语义：写进历史的只该是"真正发生过的对话"。被回复审判判定"不用回"、
    或生成/发送彻底失败的消息都不算对话，留着会污染下一轮的上下文
    （模型会以为自己在某句话之后没吭声是"故意的"）。
    例外：如果这条消息已经带上了识图模型的画面描述（`[图片: ...]`），
    就保留它 —— 画面内容对用户的下一条追问至关重要。
    """
    if not history:
        return False
    last = history[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        return False
    content = str(last.get("content", ""))
    if "[图片:" in content:
        return False                      # 已带回画面描述：保留，供下一条追问使用
    if (user_text or "").strip() and (user_text or "").strip() not in content:
        return False                      # 不是本条消息，别误删
    history.pop()
    return True



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
    if not is_private and not at_bot:
        if reply_seg is None and (global_config.get("group_need_at", True)
                                  or global_config.get("only_private", False)):
            return None
    if not user_text and not has_image:
        return None
    return {"session_id": session_id, "event": event, "client": client,
            "text": user_text, "has_image": has_image, "image_urls": image_urls,
            "image_file_ids": image_file_ids, "at_ids": at_ids,
            "at_bot": at_bot, "reply_seg": reply_seg}


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


async def handle_message_event(event, client):
    global napcat_client
    napcat_client = client
    info = _extract_event_info(event, client)
    if info is None:
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
        if global_config.get("group_need_at", True) or global_config.get("only_private", False):
            return
    if not user_text and not has_image:
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

    target_roles = resolve_target_roles(user_text, is_private)
    max_total = max(1, int(global_config.get("multi_role_max_total", 6)))
    total_replies = 0
    first_reply_done = False

    async def process_role_reply(role: dict, trigger_text: str, gate_reply: bool = False) -> bool:
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
        if rag_mgr and global_config.get("rag_enabled", False):
            rc = await rag_mgr.build_context(user_text)
            if rc:
                extra_parts.append(rc)
        try:
            repeat_rounds = max(1, int(global_config.get("repeat_guard_rounds", 3) or 3))
        except (TypeError, ValueError):
            repeat_rounds = 3
        recent_replies = _recent_assistant_replies(history, repeat_rounds)
        last_reply = recent_replies[0] if recent_replies else ""
        if recent_replies:
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

        if gate_reply and (global_config.get("reply_judge_enabled", False)
                           or global_config.get("mood_enabled", False)) and mood_mgr is not None:
            mood_user = "" if is_private else str(sender_id)
            try:
                verdict = await asyncio.wait_for(
                    judge_and_decide(ctx, mood_mgr, session_id, trigger_text,
                                     history, user_id=mood_user), timeout=30)
            except asyncio.TimeoutError:
                print("回复审判超时（30s），本轮跳过审判直接回复。")
                verdict = None
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
                    try:
                        pending_reply = await asyncio.wait_for(
                            generate_reply(ctx, emotions, trigger_text, use_history,
                                           image_sources, list(extra_parts), sender_id,
                                           session_id=session_id),
                            timeout=gen_budget)
                    except Exception as e:
                        pending_reply = None
                        print(f"审判未回复时补记画面描述失败（忽略）: {type(e).__name__}: {e}")
                    desc = str((pending_reply or {}).get("description", "") or "").strip()
                    if desc:
                        _backfill_image_description(history, desc)
                        print(f"审判未回复：画面描述已写入历史，供主人下一条追问使用 → {desc[:60]}")
                return False

        sink = None
        if global_config.get("streaming_enabled", False) and not first_reply_done:
            sink = SentenceSink(session_type, target_id, emotions, ctx, recent_replies,
                                "" if has_image else user_text)

        reply_started = time.time()
        try:
            reply = await asyncio.wait_for(
                generate_reply(ctx, emotions, trigger_text, use_history,
                               image_sources if has_image else None,
                               extra_parts, sender_id,
                               on_sentence=sink.on_sentence if sink else None,
                               session_id=session_id),
                timeout=gen_budget)
        except asyncio.TimeoutError:
            print(f"回复生成超出预算（{gen_budget:.0f}s），本轮放弃以释放会话锁。")
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
        self_ratio, self_target = _max_repeat_ratio(zh_now, recent_replies)
        user_ratio = 0.0
        if zh_now and user_text and not has_image and "[图片]" not in user_text \
                and len(_norm_text(user_text)) >= _USER_ECHO_MIN_CHARS:
            user_ratio = _repeat_ratio(user_text, zh_now)
        print(f"相似度检查：与最近 {len(recent_replies)} 条回复最高 {self_ratio:.2f}，"
              f"与用户本条 {user_ratio:.2f}"
              + ("" if can_retry else "（流式内容已发送，无法打回）"))
        if can_retry and (self_ratio >= _SELF_REPEAT_THRESHOLD or user_ratio >= _USER_ECHO_THRESHOLD):
            dup_is_user = user_ratio >= _USER_ECHO_THRESHOLD and self_ratio < _SELF_REPEAT_THRESHOLD
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
                accept_threshold = _USER_ECHO_THRESHOLD if dup_is_user else _SELF_REPEAT_THRESHOLD
                new_ratio, _new_target = _max_repeat_ratio(new_zh, recent_replies)
                if dup_is_user:
                    new_ratio = max(new_ratio, _repeat_ratio(user_text, new_zh) if new_zh else 1.0)
                elif not new_zh:
                    new_ratio = 1.0
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

        zh_text = "".join(s["zh"] for s in reply["sentences"])
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

        if not first_reply_done and has_image and reply.get("capture") \
                and reply["capture"].get("should") and image_sources and sticker_mgr is not None:
            _spawn(auto_capture_from_images(
                ctx, reply["capture"], image_sources,
                {"description": reply.get("description", ""), "sentences": reply["sentences"]}))

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

    memory_manager.cleanup_voice_cache(global_config.get("max_voice_cache", 20))
    if total_replies > 0:
        _spawn(post_reply_context_tasks(session_id, get_active_ctx()))
    else:
        # 一条回复都没产生（审判判定不用回 / 生成失败 / 发送失败）：
        # 把这条用户消息从历史里摘掉，保持"历史里只有真正发生过的对话"这一语义。
        # 识图带回了画面描述时例外——那条要留着，供用户的下一条追问使用。
        if _discard_unreplied_user_message(history, user_text):
            print(f"会话 {session_id}：本轮没有产生回复，本条消息不计入历史。")
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
        try:
            ok = await sender.speak_and_send(
                session_type, target_id, text, get_active_emotions(), ctx,
                use_voice=bool(global_config.get("proactive_voice", False)),
                sticker=bool(global_config.get("proactive_sticker", False)))
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
        try:
            data = memory_manager.load_session_data(session_id)
            data.setdefault("history", []).append({
                "role": "assistant", "content": text, "timestamp": now,
                "speaker": ctx.character_name, "proactive": True})
            memory_manager.save_session_data(session_id, data)
        except Exception as e:
            print(f"主动消息写入历史失败: {type(e).__name__}: {e}")


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
                                               history_provider=dialog_history_block) or 0
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


def _safe_subdir(root: Path, name: str) -> Optional[Path]:
    """校验 name 为 root 的直接子目录名（防路径穿越）。"""
    if not name or not re.match(r'^[\w\u4e00-\u9fff\- ]+$', name):
        return None
    p = (root / name).resolve()
    if root.resolve() not in p.parents:
        return None
    return p


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
            with open("webui_error.log", "a", encoding="utf-8") as f:
                f.write(f"{time.ctime()} - {text}\n{trace}\n")
        except Exception:
            pass
        return web.json_response(
            {"success": False, "error": f"{type(e).__name__}: {e}"}, status=500)


def _make_auth_middleware(server: "WebUIServer"):
    @web.middleware
    async def _auth(request, handler):
        path = request.path
        if path.startswith("/api") and path not in ("/api/auth/login", "/api/auth/status"):
            token = server._auth_token
            if not token:
                return await handler(request)
            if request.cookies.get("ltvm_auth") != token:
                return web.json_response({"success": False, "error": "需要密码"}, status=401)
        return await handler(request)
    return _auth


# 桌面窗口句柄：pywebview 的"选择文件夹"对话框需要（run_webview_loop 中赋值）
_WEBVIEW_WINDOW_HOLDER = {"window": None}


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
        self._last_saved_config = None
        self.memory_manager = memory_manager
        self.html_path = get_resource_path("webui") / "start.html"
        self.app = web.Application(client_max_size=200 * 1080 * 1080)
        self._update_state_file = memory_manager.data_path / "update_check.json"
        self._auth_file = memory_manager.data_path / "webui_auth.json"
        self._password = ""
        self._auth_token = None
        self._refresh_auth_state()
        self.app.middlewares.append(_webui_error_middleware)
        self.setup_routes()
        self.app.middlewares.append(_make_auth_middleware(self))
        self.app.router.add_post("/api/auth/login", self.handle_auth_login)
        self.app.router.add_get("/api/auth/status", self.handle_auth_status)
        self.app.router.add_get("/api/update/status", self.handle_update_status)
        self.app.router.add_get("/api/update/check", self.handle_update_check)

    def _refresh_auth_state(self):
        """按当前 webui_password 重建会话令牌。「记住我」的令牌与密码哈希一起
        持久化：重启后密码未变则沿用令牌（浏览器旧 Cookie 继续有效），
        密码改变即作废。"""
        import secrets
        password = str(self.config.get("webui_password", "") or "")
        self._password = password
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
        if not self._password:
            return web.json_response({"enabled": False, "authed": False})
        remaining = self._auth_remember() - time.time()
        has_valid_cookie = bool(self._auth_token) \
            and request.cookies.get("ltvm_auth") == self._auth_token
        if remaining > 0 and has_valid_cookie:
            return web.json_response({"enabled": True, "authed": True,
                                      "expires_at": self._auth_remember()})
        return web.json_response({"enabled": True, "authed": False})

    async def handle_auth_login(self, request):
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"success": False, "error": "参数错误"}, status=400)
        if str(payload.get("password", "") or "") == self._password:
            try:
                minutes = max(0, min(int(payload.get("remember_minutes")
                                          or self.config.get("webui_auth_ttl_minutes", 30) or 30),
                                     60 * 24 * 30))
            except Exception:
                minutes = max(0, int(self.config.get("webui_auth_ttl_minutes", 30) or 30))
            self._auth_remember_save(minutes)
            resp = web.json_response({"success": True})
            resp.set_cookie("ltvm_auth", self._auth_token,
                            max_age=minutes * 60 if minutes > 0 else None,
                            samesite="Lax", httponly=True)
            return resp
        return web.json_response({"success": False, "error": "密码错误"}, status=401)

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
        cache = self._update_cache()
        cached_result = cache.get("result") or {}
        if cached_result.get("current") and cached_result.get("current") != APP_VERSION:
            cache = {}  # 旧版本缓存作废，避免显示过期版本号
        interval = float(self.config.get("update_check_interval_hours", 24) or 24) * 3600
        if cache.get("checked_at") and time.time() - float(cache["checked_at"]) < interval:
            return web.json_response(cached_result)
        return await self.handle_update_check(request)

    async def handle_update_check(self, request):
        if not self.config.get("update_check_enabled", True):
            return web.json_response({"enabled": False, "has_update": False})
        from modules.updater import APP_VERSION, is_newer, pick_latest_release
        try:
            repo = "slpk1ng/Local_TTS_Voice_Modulation.exe"
            api_url = f"https://api.github.com/repos/{repo}/releases?per_page=30"
            release_home = f"https://github.com/{repo}/releases/latest"
            include_pre = bool(self.config.get("update_include_prerelease", False))
            async with httpx.AsyncClient(timeout=10, proxy=None, trust_env=False,
                                         follow_redirects=True) as client:
                resp = await client.get(api_url, headers={"Accept": "application/vnd.github+json",
                                                          "User-Agent": "ltvm-update-check"})
                resp.raise_for_status()
                releases = resp.json()
            picked = pick_latest_release(releases, include_pre)
            if not picked.get("tag"):
                return web.json_response({"enabled": True, "current": APP_VERSION,
                                          "has_update": False,
                                          "error": "仓库里没有可用的发布版本"})
            latest = picked["tag"]
            page_url = picked.get("url") or release_home

            current = APP_VERSION
            has_update = is_newer(latest, current)
            result = {"enabled": True, "current": current, "latest": latest,
                      "has_update": has_update, "url": page_url,
                      "prerelease": picked.get("prerelease", False),
                      "name": picked.get("name", "")}
            self._update_save({"checked_at": time.time(), "result": result})
            return web.json_response(result)
        except Exception as e:
            return web.json_response({"enabled": True, "current": APP_VERSION,
                                      "has_update": False,
                                      "error": f"{type(e).__name__}: {e}"})

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
        # 文件夹选择 / 模型列表
        r.add_post("/api/dialog/pick_folder", self.handle_pick_folder)
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
        r.add_post("/api/jobs/run", self.handle_jobs_run)
        r.add_get("/api/todos", self.handle_todos)
        r.add_post("/api/todos/add", self.handle_todos_add)
        r.add_post("/api/todos/update", self.handle_todos_update)
        r.add_post("/api/todos/delete", self.handle_todos_delete)
        r.add_get("/api/events", self.handle_events)
        r.add_post("/api/events/save", self.handle_events_save)
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
        # 表情包
        r.add_get("/api/stickers/list", self.handle_stickers_list)
        r.add_post("/api/stickers/upload", self.handle_stickers_upload)
        r.add_post("/api/stickers/delete", self.handle_stickers_delete)
        r.add_get("/api/stickers/file", self.handle_stickers_file)
        r.add_get("/", self.handle_index)

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
        for key in _API_KEY_KEYS + ("webui_password",):
            if isinstance(masked.get(key), dict):
                masked[key] = {k: _mask_preview(v) if isinstance(v, str) else v
                               for k, v in masked[key].items()}
            elif masked.get(key):
                masked[key] = _mask_preview(masked[key])
        return web.json_response({**masked, "defaults": self.config.default_config()})

    async def handle_export_config(self, request):
        payload = json.loads(json.dumps(self.config.config or {}, ensure_ascii=False))
        for key in _API_KEY_KEYS + ("webui_password",):
            if isinstance(payload.get(key), dict):
                payload[key] = {k: _mask_preview(v) if isinstance(v, str) else v
                                for k, v in payload[key].items()}
            elif payload.get(key):
                payload[key] = _mask_preview(payload[key])
        return _json_file_response(payload, "ltvm_config_export.json")

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
            for key in _API_KEY_KEYS + ("webui_password",):
                masked_value = data.get(key)
                if isinstance(masked_value, dict):
                    stored_keys = stored.get(key) or {}
                    if isinstance(stored_keys, dict) and any(
                            _is_masked_value(v) for v in masked_value.values()):
                        data[key] = {k: (stored_keys.get(k, "") if _is_masked_value(v) else v)
                                     for k, v in masked_value.items()}
                elif _is_masked_value(masked_value):
                    data[key] = stored.get(key, "")
            for key in _API_KEY_KEYS:
                val = data.get(key)
                if isinstance(val, dict):
                    data[key] = {k: (_decrypt_value(v) if isinstance(v, str) and
                                     (v.startswith("enc2:") or v.startswith("enc:")) else v)
                                 for k, v in val.items()}
                elif isinstance(val, str) and (val.startswith("enc2:") or val.startswith("enc:")):
                    data[key] = _decrypt_value(val)
            merged = {**self.config.default_config(), **stored, **data}
            self.config.config = merged
            self.config._atomic_save(merged)
            self._after_config_reload()
            return web.json_response({"success": True, "message": "配置已导入并热重载生效！"})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    def _after_config_reload(self):
        """配置变更后的统一热重载。"""
        global global_config, global_emotion_manager, memory_manager
        global_config = self.config
        self._refresh_auth_state()
        self.config.roles = self.config._parse_roles()
        if not global_config.active_character or global_config.active_character not in self.config.roles:
            if self.config.roles:
                global_config.active_character = list(self.config.roles.keys())[0]
        global_emotion_manager = EmotionManager(self.config)
        memory_manager = MemoryManager(self.config)
        if sender is not None:
            sender.memory_manager = memory_manager
        hot_reload_managers()

    # ---------------- 文件夹选择 / 模型列表 ----------------
    async def handle_pick_folder(self, request):
        """弹出系统"选择文件夹"对话框；仅在 LTVM 桌面窗口模式下可用。"""
        try:
            import webview
        except Exception:
            return web.json_response({"ok": False,
                                      "error": "pywebview 未安装，无法打开文件夹选择"}, status=400)
        window = _WEBVIEW_WINDOW_HOLDER.get("window")
        if window is None:
            return web.json_response({"ok": False,
                                      "error": "文件夹选择仅在 LTVM 桌面窗口模式下可用（浏览器访问不支持）"},
                                     status=400)
        try:
            loop = asyncio.get_running_loop()
            # 对话框是阻塞调用，丢进线程池避免卡住 WebUI 事件循环
            result = await loop.run_in_executor(
                None, lambda: window.create_file_dialog(webview.FOLDER_DIALOG))
            path = str(result[0]) if result else ""
            return web.json_response({"ok": bool(path), "path": path})
        except Exception as e:
            return web.json_response({"ok": False, "error": f"打开文件夹选择失败: {e}"}, status=500)

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
            return web.json_response({"ok": False, "error": "llm_base_url 为空"}, status=400)
        if backend == "ollama":
            url = f"{base}/api/tags"
        else:
            url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
        try:
            headers = {}
            api_key = str(self.config.get("llm_api_key", "") or "")
            if backend != "ollama" and api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            async with httpx.AsyncClient(timeout=10, trust_env=False, headers=headers) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
            if backend == "ollama":
                ids = [str(m.get("name") or m.get("model") or "") for m in data.get("models", [])]
            else:
                ids = [str(m.get("id") or "") for m in data.get("data", [])]
            return web.json_response({"ok": True, "models": [i for i in ids if i]})
        except Exception as e:
            return web.json_response({"ok": False, "error": f"获取模型列表失败: {e}"}, status=500)

    async def handle_save_config(self, request):
        try:
            new_config = await request.json()
            restart_tts = new_config.pop("restart_tts", False)
            for key in _API_KEY_KEYS + ("webui_password",):
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
                        old_config.get('napcat_token') != new_config.get('napcat_token')):
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

        精简模式（默认）只回尾部 webui_log_tail_lines 行，轮询负担小；
        ?full=1（前端"显示完整日志"开关）返回内存中缓存的全部日志（上限
        webui_log_buffer_lines 行），方便排查问题时翻完整过程。

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
                logs = "\n".join(global_log_buffer[-tail_lines:])
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
            if deleted and mood_mgr is not None:
                for sid in self._session_ids_from_files(deleted):
                    mood_mgr.delete_session(sid)
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
        return _json_file_response({"filename": filename,
                                    "character_name": result.get("character_name"),
                                    "history": result.get("history", [])}, filename)

    async def handle_memory_import(self, request):
        try:
            reader = await request.multipart()
            imported = []
            async for part in reader:
                if part.filename and part.filename.endswith(".json"):
                    raw = await part.read(decode=False)
                    data = json.loads(raw.decode("utf-8"))
                    if not isinstance(data, dict) or "history" not in data:
                        continue
                    name = Path(part.filename).name
                    if not re.match(r'^[A-Za-z0-9_\-]+\.json$', name):
                        name = re.sub(r'[^A-Za-z0-9_\-]', '_', Path(name).stem) + ".json"
                    (self.memory_manager.data_path / name).write_text(
                        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                    imported.append(name)
            return web.json_response({"success": True, "imported": imported})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_memory_export_all(self, request):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in self.memory_manager.data_path.glob("*.json"):
                zf.write(f, f.name)
        buf.seek(0)
        return web.Response(body=buf.read(), content_type="application/zip",
                            headers={"Content-Disposition": 'attachment; filename="ltvm_memories.zip"'})

    async def handle_sessions(self, request):
        """返回已知会话列表（供定时任务/事件/待办选择发送目标）。"""
        sessions = []
        for f in self.memory_manager.data_path.glob("*.json"):
            m = re.match(r'^[A-Za-z0-9_\-]+_(private|group)_[A-Za-z0-9_\-]+\.json$', f.name)
            if not m:
                continue
            stype = m.group(1)
            rest = f.name.split(f"{stype}_", 1)[1].replace(".json", "")
            if stype == "group":
                sid = f"group_{rest.split('_')[0]}"
            else:
                sid = f"private_{rest}"
            item = {"session_id": sid, "session_type": stype}
            if not any(s["session_id"] == sid for s in sessions):
                sessions.append(item)
        return web.json_response({"sessions": sessions})

    # ---------------- 情绪音频管理 ----------------
    def _role_root(self, role_key: str) -> Path:
        role = self.config.roles.get(role_key, {}) if role_key else {}
        ctx = RoleContext(self.config.config, role or {})
        return Path(resolve_tts_path(ctx.get("ref_audio_root", "")))

    async def handle_emotions_list(self, request):
        role_key = request.query.get("role", "")
        root = self._role_root(role_key)
        emotions = []
        if root.exists():
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
                emotions.append({"name": folder.name, "files": files, "asr": asr, "ref": ref})
        return web.json_response({"root": str(root), "role": role_key, "emotions": emotions})

    async def handle_emotions_upload(self, request):
        try:
            reader = await request.multipart()
            role = emotion = asr_text = None
            file_data = None
            file_name = ""
            async for part in reader:
                if part.name == "role":
                    role = (await part.text()).strip()
                elif part.name == "emotion":
                    emotion = (await part.text()).strip()
                elif part.name == "asr":
                    asr_text = await part.text()
                elif part.name == "file":
                    file_name = part.filename or ""
                    file_data = await part.read(decode=False)
            root = self._role_root(role or "")
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
            if asr_text is not None and asr_text.strip():
                (folder / "asr.txt").write_text(asr_text.strip(), encoding="utf-8")
                saved.append("asr.txt")
            self._after_config_reload()
            return web.json_response({"success": True, "saved": saved})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_emotions_create(self, request):
        try:
            payload = await request.json()
            root = self._role_root(payload.get("role", ""))
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
            root = self._role_root(payload.get("role", ""))
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
        root = self._role_root(role)
        folder = _safe_subdir(root, emotion)
        if folder is None or not file or not re.match(r'^[\w\u4e00-\u9fff\-. ]+$', file):
            return web.Response(status=404, text="not found")
        target = folder / file
        if not target.exists():
            return web.Response(status=404, text="not found")
        return web.FileResponse(target)

    # ---------------- 统计 ----------------
    async def handle_stats(self, request):
        range_key = str(request.query.get("range", "30") or "30")
        stats = stats_mgr.get_stats(range_key) if stats_mgr else {}
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
                                     payload.get("user_id", ""), source="manual")
            return web.json_response({"success": bool(todo), "todo": todo})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_todos_update(self, request):
        try:
            payload = await request.json()
            todo_id = int(payload.get("id", 0))
            status = payload.get("status", "done")
            if status in ("done", "cancelled"):
                if status == "done":
                    todo_mgr.complete(todo_id)
                else:
                    todo_mgr.delete(todo_id)
                return web.json_response({"success": True})
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

    # ---------------- 事件问候 ----------------
    async def handle_events(self, request):
        return web.json_response({"events": event_mgr.events if event_mgr else []})

    async def handle_events_save(self, request):
        try:
            payload = await request.json()
            events = payload.get("events", [])
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
                    "targets": ev.get("targets") or [],
                })
            event_mgr.events = cleaned
            event_mgr.save_events()
            return web.json_response({"success": True, "events": cleaned})
        except Exception as e:
            return web.json_response({"success": False, "error": str(e)}, status=400)

    async def handle_events_test(self, request):
        try:
            payload = await request.json()
            ok = await event_mgr.greet_event_now(str(payload.get("id", "")), sender,
                                                 get_active_ctx, get_active_emotions)
            return web.json_response({"success": bool(ok),
                                      "message": "已发送测试问候" if ok else "未找到事件或生成内容为空"})
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
            from modules.tools import available_engines, parse_api_keys
            payload = await request.json()
            engine = str(payload.get("engine", "") or "").strip()
            custom = str(payload.get("custom", self.config.get("web_search_custom_engines", "")) or "")
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
                                  "enabled": bool(self.config.get("stickers_enabled", False))})

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
                    fname = Path(part.filename).name
                    if not re.match(r'^[\w\u4e00-\u9fff\-. ]+$', fname):
                        continue
                    if Path(fname).suffix.lower() not in IMAGE_EXTS:
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
            fname = payload.get("name", "")
            if folder is None or not re.match(r'^[\w\u4e00-\u9fff\-. ]+$', fname):
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
        fname = request.query.get("name", "")
        folder = _safe_subdir(sticker_mgr.dir, category) if sticker_mgr else None
        if folder is None or not re.match(r'^[\w\u4e00-\u9fff\-. ]+$', fname):
            return web.Response(status=404, text="not found")
        if Path(fname).suffix.lower() not in IMAGE_EXTS:
            return web.Response(status=404, text="not found")
        target = folder / fname
        if not target.exists():
            return web.Response(status=404, text="not found")
        resp = web.FileResponse(target)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

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
                    with open("webui_error.log", "a", encoding="utf-8") as f:
                        f.write(f"{time.ctime()} - {error_msg}\n")
                except Exception:
                    pass
                return  # 其他异常不自动切换，直接记录并退出

        # 所有端口尝试失败
        print("错误：无法找到可用端口，WebUI 启动失败。")
        with open("webui_error.log", "a", encoding="utf-8") as f:
            f.write(f"{time.ctime()} - 所有端口被占用，WebUI 启动失败。\n")

    async def shutdown(self):
        if HAS_AIOHTTP:
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
    global todo_mgr, job_mgr, event_mgr, sender, mood_mgr
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding='utf-8')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding='utf-8')

    sys.stdout = StdoutRedirector(sys.stdout)
    os.environ["NAP_CAT_PLUGIN_INDEX_URL"] = ""
    os.environ["NO_PROXY"] = "localhost,127.0.0.1"
    os.environ["no_proxy"] = "localhost,127.0.0.1"

    print("=" * 160)
    print(
        "⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠟⣛⣩⣤⣶⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣶⣦⣬⣉⠛⠀⠀⠀⠀⠀⢛⣋⣩⣥⠴⠶⠶⠟⠛⠛⠛⠛⠛⠛⠛⠻⠿⠷⠶⢶⣦⣤⣍⣉⡛⠛⠿⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "⣿⣿⣿⣿⣿⣿⣿⣿⣿⠟⣋⣥⣶⠿⣛⣭⣷⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠟⣋⠁⠀⠀⠄⢒⣋⣩⣥⣴⣶⣶⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣶⣶⣦⣭⣍⣛⠻⢷⣶⣤⣍⣙⠛⠿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "⣿⣿⣿⣿⣿⣿⠟⣋⣴⡾⢟⣫⣴⠾⣻⣿⣿⣿⣿⠿⠿⠿⠟⠛⠛⠛⠛⠛⠉⠀⠉⣀⣤⣴⣶⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣮⣝⣿⣿⣿⣶⣦⣌⡙⠻⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "⠿⠿⠿⠿⢛⣡⡾⢟⣩⣶⠿⠋⠗⣛⣉⣥⣤⠤⠶⣒⣒⣚⡯⠭⣉⡭⠛⢁⣤⣶⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣦⣌⠙⠿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "⣀⣀⢀⡴⠟⣋⣐⣩⡤⢴⣒⣻⣭⣵⣶⠿⢟⣛⡭⠽⠖⠚⠋⠉⣁⣴⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣻⠿⣶⣄⡙⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "⣫⡥⠖⣚⣩⣵⣶⣾⣿⠿⣿⣛⠭⠖⠚⣉⣩⣤⣶⡶⠟⢋⣤⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠿⣟⣛⣯⣽⣷⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣶⣭⡛⢦⣌⠙⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "⣥⣾⣿⣿⠿⣟⡫⠵⠚⣋⣡⣤⣶⣾⣿⡿⠟⠋⠁⢀⣴⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠿⣟⣯⣵⣶⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣌⠳⣤⡉⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "⢿⣛⠭⠒⣉⢅⣴⣾⣿⣿⣿⣿⠿⠋⠁⠀⠀⢀⣴⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⢛⣭⣶⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠿⣻⣽⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣎⠻⣦⡈⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "⣩⡴⢠⡿⣣⣾⣿⣿⠿⠛⠉⠀⠀⠀⠀⢀⣴⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⣻⣵⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠟⣫⣶⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⣫⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣮⣝⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣌⢿⣦⡈⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿\n"
        "⠿⣱⡟⣵⡿⠟⠋⠁⠀⠀⠀⢀⡤⠂⣴⣿⣿⣿⣿⣿⣿⣿⣿⡿⣛⣵⣿⣿⣿⣿⣿⣿⣿⣿⢟⣿⣿⡿⣋⣴⣿⣿⣿⣿⣿⣿⣿⠟⣫⣾⣿⣿⣿⣿⡿⣫⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣌⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣧⡹⣿⣆⠙⢿⣿⣿⣿⣿⣿⣿⣿\n"
        "⠀⠟⠘⠉⠀⠀⠀⠀⢀⣤⣾⠟⣠⣾⣿⣿⣿⣿⣿⣿⣿⡿⣫⣾⣿⣿⣿⣿⣿⣿⣿⣿⣯⣾⣿⠟⣡⣾⣿⣿⣿⣿⣿⣿⣿⠟⣡⣾⣿⣿⣿⣿⣿⢏⣼⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⡌⠻⣿⣿⣿⣿⣿⣿⣿⣿⣷⡌⢻⣷⡈⠛⠛⠛⠛⠛⠻⠿\n"
        "⣇⠀⠀⠀⠀⣀⣴⣾⣿⡿⢃⣴⣿⣿⣿⣿⣿⣿⣿⡿⣫⣾⣿⣿⣿⣿⣿⣿⣿⡿⣫⣾⣿⠟⣡⣾⣿⣿⣿⣿⣿⣿⣿⠟⣡⣾⣿⣿⣿⣿⣿⡟⣱⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣦⡘⢿⣿⣿⣿⣿⣿⣿⣿⣿⡄⠳⠟⢠⡒⢦⠄⣀⣀⣤\n"
        "⣞⣆⢀⣴⣾⣿⣿⣿⠟⢡⣾⣿⣿⣿⣿⣿⣿⣿⣫⣾⣿⣿⣿⣿⣿⣿⣿⡿⣫⣾⣿⠟⣡⣾⣿⣿⣿⣿⣿⣿⣿⡿⡡⣾⣿⣿⣿⣿⣿⣿⢋⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣄⢻⣿⣿⣿⣿⣿⣿⣿⣿⡄⢠⣦⠙⠎⣰⣷⣿⣿\n"
        "⠿⠜⣄⠻⣿⣿⣿⠏⣰⣿⣿⣻⣿⣿⣿⣿⣟⣵⣿⣿⣿⣿⣿⣿⣿⣿⢫⣾⣿⡿⢋⣾⣿⣿⣿⣿⣿⣿⣿⣿⢏⢴⣾⣿⣿⣿⣿⣿⡿⣱⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢹⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣯⢦⠹⠿⠿⣿⣿⣿⣿⣿⣿⡀⠃⠀⠀⠹⣿⣿⣿\n"
        "⠉⠉⠙⠂⠹⣿⠃⣼⣿⡿⣱⣿⣿⣿⣿⢯⣾⣿⣿⣿⣿⣿⣿⣿⢟⣵⣿⣿⠏⣴⣿⣿⣿⣿⣿⣿⢿⢿⠟⠡⢢⣿⣿⣿⣿⣿⣿⠟⡼⣽⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠇⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡟⣿⣿⣿⣿⣿⣿⣿⢎⣴⣾⣷⡹⣿⣿⣿⣿⣿⣧⠀⠀⠀⢠⠘⣿⣿\n"
        "⣦⡀⠀⠀⠀⢀⣼⣿⡿⣱⣿⣿⣿⡿⣳⣿⣿⣿⣿⣿⣿⣿⡿⢫⣾⣿⡿⢡⣾⣿⣿⣿⣿⣿⣿⣿⡿⠃⡴⣱⣿⣿⣿⣿⣿⣿⢏⣞⣽⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⣹⡟⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠸⣿⣿⣿⡿⡿⢡⣾⣿⣿⣿⣇⢹⣿⣿⣿⣿⣿⡄⠀⠀⢸⢣⠘⣿\n"
        "⣿⣿⣦⡀⢀⣾⣿⣿⢡⣿⣿⣿⡿⣱⣿⣿⣿⣿⣿⣿⣿⡟⣱⣿⣿⠟⣰⣿⣿⣿⣿⣿⣿⣿⣿⢟⡔⡜⣼⣿⣿⣿⣿⣿⣿⢏⣞⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢃⣿⢁⣿⣿⣿⣿⣿⣿⣿⣿⢿⣿⣿⣿⣿⣿⡆⢿⣿⣿⣿⢁⣾⣿⠿⠟⠛⠛⠈⣿⣿⣿⣿⣿⣧⠀⠀⠈⣏⢧⠸\n"
        "⣿⣿⣿⠃⣼⣿⣿⢣⣿⣿⣿⣿⣱⣿⣿⣿⣿⣿⣿⣿⢏⣼⣿⣿⠏⣼⣿⣿⣿⣿⣿⣿⣿⣿⢃⠞⢜⣾⣿⣿⣿⣿⣿⣿⢏⡞⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⣼⠃⢸⣿⣿⣿⣿⣿⣿⣿⡟⣾⣿⣿⣿⣿⣿⡇⢸⣿⣿⡏⢸⢿⣧⠀⠀⠀⠀⠀⢹⣿⣿⣿⣿⣿⠀⠀⠀⠸⡌⢧\n"
        "⠻⣿⠃⣼⣿⣿⢇⣾⣿⣿⣿⢳⣿⣿⣿⣿⣿⣿⣿⢋⣾⣿⣿⢋⣾⣿⣿⣿⣿⣿⣿⣿⡿⢡⡏⢌⣾⣿⣿⣿⣿⣿⣿⢏⡞⣼⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⢰⡟⠀⣾⣟⢿⣿⣿⣿⣿⣿⢃⣿⣽⣿⣿⣿⣿⡇⢸⣿⣿⡇⠀⠀⢀⠀⠀⠀⠀⠀⠸⣿⣿⣿⣿⣿⡇⠀⠀⣆⠗⢋\n"
        "⣷⠆⣸⣿⣿⡟⣼⣿⣿⣿⢧⣿⣿⣿⣿⣿⣿⡿⠃⠞⠛⠻⠁⠘⠛⠿⠿⣿⣿⣿⣿⡿⣱⡟⢈⣾⣿⣿⣿⣿⣿⣿⡏⡼⣹⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢃⡿⡡⢸⣿⣿⣷⣿⡻⣿⣿⡟⣸⣧⣿⣿⣿⣿⣿⡇⢸⣿⣿⡇⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⣿⡇⠀⣠⠴⠚⠉\n"
        "⡟⢠⣿⣿⣿⢱⣿⣿⣿⡟⣾⣿⣿⣿⣿⣿⣦⢀⣀⠀⠠⠁⠀⠀⠀⠀⠀⠀⠉⠛⠿⣱⣿⢁⣾⣿⣿⣿⣿⣿⣿⡟⣸⢳⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡏⣾⢣⢇⣿⣿⣿⣿⣿⣿⣿⣿⢡⡿⣼⣿⣿⣿⣿⣿⡇⣼⣿⣿⣷⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣿⡿⠋⠀⠀⠀⠀⠀⠀\n"
        "⠀⣿⣿⣿⠇⣿⣿⣿⣿⢱⣿⣿⣿⣿⣿⣿⢣⣿⣿⣿⠀⣀⣁⢤⣤⣄⣀⡀⠀⠀⠀⠈⠁⢼⣿⣿⣿⣿⣿⣿⣿⢡⡏⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⣸⠏⡞⣸⣿⣿⣿⣿⣿⣿⣿⠇⣾⢳⣿⣿⣿⣿⣿⣿⠃⣿⣿⣿⡟⠂⠀⠀⠀⠀⠀⠀⠀⠀⣿⡿⠋⠀⠀⠀⠀⠀⠀⠀⠀\n"
        "⣸⣿⣿⡿⣸⣿⣿⣿⡇⣾⣿⣿⣿⣿⣿⢏⣾⣿⣿⡇⢠⣿⣿⣷⣮⣝⡻⠿⠋⠀⠀⠀⠀⠀⠙⢿⣿⣿⣿⣿⠇⡾⣸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣱⡟⣼⢣⣿⣿⣿⣿⣿⣿⣿⡟⣰⡏⣿⣿⣿⣿⣿⣿⣿⢠⣿⣿⡟⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠊⠀⠀⠀⠀⠀⠀⠀⡀⢀⣼\n"
        "⣿⡿⣿⠇⣿⣿⣿⣿⢠⣿⣿⣿⣿⣿⡟⣾⣿⣿⣿⠁⣼⣿⣿⣿⣿⣿⣿⠁⠀⠀⠀⠀⠀⠀⠀⠀⠹⣿⣿⡟⢰⣇⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢣⡿⣰⡏⣼⣿⣿⣿⣿⣿⣿⡟⣰⡿⣽⣿⣿⣿⣿⣿⣿⡇⢸⣿⢸⡃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣀⡀⠐⢈⣴⡿⢋\n"
        "⣿⢻⣿⢸⣿⣿⣿⡿⢸⣿⣿⣿⣿⣿⣹⣿⣿⣿⡏⠀⣿⣿⣿⣿⣿⣿⠃⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠹⣿⡇⣾⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢯⡿⢡⣿⢳⣿⣿⣿⣿⣿⣿⡿⣰⣿⢳⣿⣿⣿⣿⣿⣿⡿⠀⣾⡇⣿⡇⢠⣀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠐⠉⠉⠀⠀⠉⠉⠀⢻\n"
        "⡏⣿⡇⣾⣿⣿⣿⡇⣼⣿⣿⣿⣿⢯⣿⣿⣿⣿⢡⣿⣿⣿⣿⣿⣿⡟⢀⠀⠂⠀⠀⠀⠀⠀⠀⠀⠀⠀⠙⡇⡿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢏⣾⢣⣿⣗⣾⣿⣿⣿⣿⣿⡟⣱⣿⢯⣿⣿⣿⣿⣿⣿⣿⢡⠂⣿⢰⣿⣿⣆⠻⣿⣦⠒⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘\n"
        "⢹⣿⢃⣿⣿⣿⣿⡇⣿⣿⣿⣿⡏⣸⣿⣿⣿⡿⢸⣿⣿⣿⣿⣿⣿⣧⣿⣷⡀⠀⠀⠀⠀⠀⠀⣶⣦⢀⢠⣷⣧⣿⣿⣿⣿⣿⣿⣿⣿⣿⢏⣾⢣⣿⡟⢸⣿⣿⠿⠿⠿⠟⠘⠛⠟⠿⠿⣿⣿⣿⣿⣿⢃⣿⢸⡇⣾⣿⣿⣿⡗⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣆⠀⠀⠀⠀\n"
        "⣾⣿⢸⣿⣿⣿⣿⡇⣿⣿⣿⣿⡇⣿⣿⣿⣿⡇⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⠀⠀⠀⠀⠀⠀⠈⠁⢸⣿⣿⢿⣿⣿⣿⣿⣿⣿⣿⣿⢏⡾⣣⣿⠟⠋⠉⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠉⠙⠃⠺⠇⡿⢰⣿⣿⣿⡏⠀⠀⠀⠀⠀⠀⢀⢀⣠⡀⣀⢒⡉⠀⣿⣿⠀⠀⠀⠀\n"
        "⣿⡟⢸⣿⣿⣿⣿⡇⢿⣿⣿⣿⡧⡝⣿⣿⣿⡇⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠟⠀⠀⠀⠀⠀⠀⠀⠀⢸⣿⣿⣸⣿⣿⣿⣿⣿⣿⣿⢏⡾⣵⠟⠁⡀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⣀⡀⠀⠀⠀⠀⠀⠰⠁⢿⣿⣿⡿⠀⢿⡴⢚⣡⡞⠿⠺⡏⢸⡇⢸⣿⠁⡆⠸⣿⡇⠀⠀⠀\n"
        "⣿⡇⣿⣿⣿⣿⣿⣧⢸⣿⣿⣿⡇⣿⡌⢿⣿⡇⣿⣿⣿⣿⣿⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢋⣿⣾⣿⣿⡿⠁⠀⡀⠀⠀⠀⠀⠀⠀⠀⢀⡀⠀⢿⣿⣶⣤⣀⠀⠀⠀⠀⠀⠙⠿⠁⠀⢋⣴⣿⢰⣶⢼⡶⢻⡼⢃⣾⡇⢸⣧⠠⠻⠷⠀⠀⠀\n"
        "⣿⡇⣿⣿⣿⣿⣿⣿⠸⡿⠟⣻⣧⢻⣿⠀⡹⣿⣿⣿⣿⣿⣿⣿⣿⡆⠀⣠⣴⣤⣀⡀⠀⣀⠀⠀⣸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣦⡀⠀⠀⠀⠀⠀⠀⠀⠀⠻⡿⠂⢸⣿⣿⣿⣿⣷⠄⡀⠀⠀⠀⠀⠑⣾⣿⣿⢟⣕⢲⢇⣼⡈⠇⣿⡟⠀⠀⠀⠀⠀⠀⠀⠀⠀\n"
        "⣿⡇⣿⣿⣿⣿⣿⣿⣷⣿⣿⣿⣿⡈⢿⡀⣿⣾⣿⣿⣿⣿⣿⣿⣿⣿⣆⠙⣿⣿⣿⣿⡇⣴⣄⣰⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢸⣿⣿⣿⣿⡟⣰⣿⣦⠐⠀⠀⠀⠘⣿⣿⢬⢋⡞⣨⢫⢷⣄⣿⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀\n"
        "⣿⡇⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡕⣌⢧⢻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣶⣭⣿⣿⣧⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣸⣿⣿⣿⡿⣱⣿⣿⠃⣠⣾⣷⣶⣦⣽⣇⠿⡺⣱⣏⠺⢗⣿⠃⡤⢤⣤⡄⢶⣦⠰⣶⣄⠀\n"
        "⣿⡇⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⠈⠈⡋⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⠈⠉⠉⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣿⣿⣿⡟⣱⣿⣿⠃⣴⣿⣿⣿⣿⣿⣿⣫⣾⣱⣿⣿⣯⣼⣧⢰⣧⢸⣿⣿⡄⠻⣷⡘⢿⡄\n"
        "⣿⡇⢻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⢀⠀⢷⣮⣻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡇⠀⠀⠀⣀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⣾⣿⣿⢟⣼⣿⠟⢡⣾⣿⣿⣿⣿⣿⡿⢃⢜⡱⣿⣿⣿⣷⠎⣠⣏⢻⡄⢿⣿⣷⡐⢌⡛⢮⡳\n"
        "⣿⡇⢸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠈⢄⠈⢻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣄⠠⣾⣿⣿⣶⣶⣦⠐⣂⠀⠀⣠⣾⣿⣿⢯⣟⣫⢅⣴⣿⣿⣿⣿⣿⣿⠟⣱⠏⡹⣛⣿⣿⣿⡏⢠⣝⡋⣚⡻⡘⣿⣿⣷⡘⢿⣶⣤\n"
        "⣿⣷⠘⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡄⠃⠠⠀⠙⠿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣾⣿⣿⣿⣿⣿⠞⣿⣧⣾⣿⣿⣿⣿⣿⠟⣡⣾⣿⣿⣿⣿⣿⡿⢋⣾⢫⣾⢵⣯⣿⣿⡟⢠⣿⠟⢞⡿⡃⣳⠘⣿⣿⣷⡈⢿⣿\n"
        "⣿⣿⠀⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣧⠘⠀⠀⠃⢀⠈⠻⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢟⣡⣾⣿⣿⣿⣿⣿⡿⢋⣴⢟⣵⣿⣿⡖⣤⡿⡟⢀⣿⣿⣷⣾⣿⣜⠿⡣⣘⡻⣿⣿⣄⠙\n"
        "⣿⣿⡆⠸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡆⢡⠀⠀⠀⠁⠀⠀⠉⠻⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡵⣿⣿⣿⣿⣿⣿⡿⢋⣴⠟⣱⣿⣿⣿⣿⣧⡟⡟⠀⠀⣿⣿⣿⣿⣏⣹⣿⣜⠿⣇⣩⣝⢿⣦\n"
        "⣿⣿⣧⠀⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡈⠀⠀⠀⠀⠄⢀⣤⣶⣄⡈⠛⠿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢛⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡷⠂⣠⣴⡤⣩⡴⢛⣥⣾⣿⣿⣿⣿⣿⣏⡸⡿⢂⠀⣿⣿⣿⣿⣿⣿⣿⣿⣏⣡⣙⣋⢸⣶\n"
        "⣿⣿⣿⡀⡘⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⡀⠀⢀⠂⣠⣿⣿⣿⣿⣿⣷⢠⡄⠉⠛⠿⣿⣿⣿⣿⣿⣭⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠋⣠⡾⠟⠵⣊⣥⣾⣿⣿⣿⣿⣿⣿⣿⢯⡟⠀⢴⣶⡄⠸⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⣭\n"
        "⣿⣿⣿⣧⠘⣢⡙⠻⣿⣿⣿⣿⣿⣿⣿⣿⣿⣷⡀⠈⢰⣿⣿⣿⡏⣿⣿⣿⢸⠁⠀⠀⠀⠀⠈⠙⠛⠿⢿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⠟⠋⢀⣤⣥⣶⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⣳⠏⢀⠂⠈⢉⣬⡀⠙⢿⣿⠿⠿⠛⠛⠻⣿⣿⣿⣿⣿\n"
        "⢻⣿⣿⣿⣆⠩⢧⠑⠨⣙⠻⢿⣿⣿⣿⣿⣿⣿⣷⡄⢿⣿⣿⣿⢸⣿⣿⣿⠘⠀⠀⠀⠀⠀⠀⠀⠀⣤⣤⣤⣄⣉⣉⡙⠛⠛⠛⠛⠿⠿⠿⠿⠿⠿⠟⠛⠛⠛⠋⠉⠉⠀⢀⣴⣿⡿⣫⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣟⣽⠏⠀⠀⠀⡀⠌⠛⢃⣁⠀⠀⠀⠀⠀⠀⠀⠈⢿⣿⣿⣿\n"
        "⣌⠻⠿⣿⣿⣆⠩⣧⠀⠀⠁⠂⢬⠉⠛⠿⢿⣿⣿⣿⣎⠻⣿⡇⡾⠋⠙⢿⠀⠀⠀⠀⠀⠀⠀⠀⠀⣿⣿⣿⣿⣿⣿⣿⣿⡿⠁⠀⠀⠀⠀⠀⠀⠀⠀⠐⣰⣿⣿⣿⢖⣴⣿⡿⣫⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣫⣾⠏⠀⠐⠂⠁⠀⠀⠀⠙⠟⢁⣀⠀⠀⠀⠀⠀⠀⠘⣿⣿⣿\n"
        "⣿⣿⣷⣶⣭⣍⣃⠈⢷⡀⠄⣂⣴⣶⣦⣑⠲⢠⠈⣭⣍⣓⡙⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢹⣿⣿⣿⣿⣿⣿⠟⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣰⣿⡿⢋⣵⣿⢟⣵⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢟⣵⣿⠏⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⠟⢁⣤⡀⠀⠀⠀⠀⠈⣉⡛\n"
        "⣿⣿⣿⣿⣿⣿⣿⣦⡀⠋⣾⣿⣿⣿⣿⠿⠃⣉⡀⣿⣿⣿⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢿⣿⣿⣿⠟⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⣰⠿⣋⣴⢟⢏⣴⣿⡿⣫⣿⣿⣿⣿⣿⣿⣿⣿⡿⣫⣾⣿⠋⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠈⠛⠃⢴⣶⠀⣠⣄⠉⣁\n"
        "⣿⣿⣿⣿⣿⣿⣿⣿⡿⣂⣽⣿⣷⡍⣥⣚⡛⠿⠇⣿⣿⣿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠘⢿⠟⠁⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢘⡥⢞⣫⢔⣵⣿⢟⣭⣾⣿⣿⣿⣿⣿⣿⣿⣿⢋⣾⣿⡿⢃⣶⣦⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠁⠀⠙⠋⠀⠻\n"
        "⠻⣿⣿⣿⣿⣿⣿⣿⢸⣿⣿⣿⣿⠀⣿⣿⣿⣿⣶⣍⡛⠿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠂⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢠⡾⢽⡾⣋⣴⠿⣫⣵⣿⣿⣿⣿⣿⣿⣿⣿⣿⢟⣵⣿⣿⡿⠡⢿⣿⣿⣧⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀\n"
        "⢷⣬⡛⢿⣿⣿⣿⣿⡎⢿⣿⣿⣿⡄⣿⣿⣿⣿⣿⣿⣿⣷⣦⡀⢀⡴⠂⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⢀⡤⢞⣫⣷⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⢟⣵⣿⣿⣿⡟⣱⣿⣷⡝⣿⡿⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀\n"
        "⠀⠙⠻⢶⣬⡙⠛⠉⠀⠀⠈⠀⠀⠀⢿⣿⣿⣿⣿⣿⣿⣿⢏⣴⠏⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠑⠦⣄⡀⢠⣾⣷⣾⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⢛⣵⣿⣿⣿⣿⠟⣰⣿⣿⣿⣷⣆⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀\n"

        "\n\n                                                            启动成功啦！                                                              "
    )
    print("=" * 160)

    global_config = ConfigLoader()
    global_emotion_manager = EmotionManager(global_config)
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
    sender = MessageSender(global_config, memory_manager, sticker_mgr, stats_mgr)
    todo_mgr = TodoManager(global_config, db, scheduler, sender,
                           emotions_provider=get_active_emotions)
    todo_mgr.ctx_provider = get_active_ctx
    todo_mgr.restore_pending()
    job_mgr = ScheduledJobManager(global_config, memory_manager.data_path, scheduler,
                                  sender, get_active_ctx, get_active_emotions)
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

    webui_server = None
    if HAS_AIOHTTP and global_config.get("webui_enabled", True):
        webui_server = WebUIServer(global_config, memory_manager)
        try:
            await webui_server.start()
        except Exception as e:
            print(f"WebUI 启动异常，继续运行其他功能：{e}")

    ws_url = global_config.get("napcat_ws_url", "ws://127.0.0.1:3001")
    token = global_config.get("napcat_token", "")

    print(f"正在连接 NapCat ({ws_url})...")

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
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    watcher = asyncio.create_task(_stop_watcher())

    while True:
        if stop_event is not None and stop_event.is_set():
            print("收到停止信号，正在退出消息循环...")
            break

        try:
            client = NapCatClient(ws_url=ws_url, token=token)
            active_clients.append(client)
            async with client:
                sender.client = client  # 注入统一发送器，供消息回复与主动消息使用
                print(f"已连接！机器人 QQ: {client.self_id}")
                print("等待消息中...")
                async for event in client:
                    if stop_event is not None and stop_event.is_set():
                        print("收到停止信号，正在退出消息循环...")
                        break
                    task = asyncio.create_task(handle_message_event(event, client))
                    task.add_done_callback(_consume_task_result)
                # 连接被服务端正常关闭（非异常路径）：稍候重连，避免紧密循环
                if stop_event is None or not stop_event.is_set():
                    print("连接已断开，3秒后重连...")
                    await asyncio.sleep(3)

        except Exception as e:
            print(f"NapCat 连接失败: {e}")
            print("10秒后尝试重新连接...")
            await asyncio.sleep(10)
            continue
        finally:
            sender.client = None  # 连接断开后置空，避免主动消息/定时任务使用失效连接
            try:
                active_clients.remove(client)
            except (ValueError, NameError):
                pass

    watcher.cancel()
    print("正在关闭所有子进程...")
    save_proactive_state()
    process_manager.shutdown_all()
    await scheduler.stop()
    if db is not None:
        db.close()
    if webui_server:
        await webui_server.shutdown()


if __name__ == "__main__":
    import threading
    import time

    stop_event = threading.Event()

    wait_pid = os.environ.pop("LTVM_WAIT_PID", "")
    if wait_pid:
        try:
            target_pid = int(wait_pid)
            print(f"等待旧进程({target_pid})退出后启动…")
            while _pid_alive(target_pid):
                time.sleep(0.3)
        except Exception:
            pass

    config = ConfigLoader()

    def run_backend():
        try:
            asyncio.run(main(stop_event))
        except Exception as e:
            print(f"后台服务异常: {e}")
            import traceback
            traceback.print_exc()
            try:
                with open("backend_error.log", "a", encoding="utf-8") as f:
                    f.write(f"{time.ctime()} - 异常: {e}\n")
                    traceback.print_exc(file=f)
            except Exception:
                pass

    backend_thread = threading.Thread(target=run_backend, daemon=True)
    backend_thread.start()
    time.sleep(1)
    webui_port = int(config.get("webui_port", 11500))

    holder = {"window": None}
    close_state = {"minimized": False}
    tray_state = {"icon": None}

    def open_console():
        w = holder["window"]
        if w is not None:
            try:
                w.show()
                w.restore()
            except Exception:
                pass

    def stop_tray_icon():
        icon = tray_state.get("icon")
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass

    def _force_quit(delay: float = 2.5):
        def _run():
            try:
                process_manager.shutdown_all()  # 兜底：确保 TTS 子进程被杀
            except Exception:
                pass
            time.sleep(max(0.0, delay))
            os._exit(0)
        threading.Thread(target=_run, daemon=True).start()

    def relaunch_console():
        stop_tray_icon()
        w = holder["window"]
        if w is not None:
            try:
                w.destroy()
            except Exception:
                pass
        try:
            import subprocess
            exe = sys.executable
            if not getattr(sys, "frozen", False) and os.name == "nt":
                pyw = str(Path(sys.executable).with_name("pythonw.exe"))
                if os.path.exists(pyw):
                    exe = pyw
            args = [exe]
            if not getattr(sys, "frozen", False):
                args.append(str(Path(sys.argv[0]).resolve()))
            env = dict(os.environ)
            env["LTVM_WAIT_PID"] = str(os.getpid())
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            subprocess.Popen(args, env=env, close_fds=False,
                             creationflags=creationflags)
            print("已安排重启：新实例将等待本进程完全退出后启动，避免端口占用/重复进程。")
        except Exception as e:
            print(f"重启 LTVM 控制台失败: {e}")
        stop_event.set()
        _force_quit(3.0)

    def quit_app():
        stop_event.set()
        # 先启动硬退出计时：托盘线程里调用窗口销毁可能阻塞数秒，
        # 计时器必须先跑起来，退出耗时才不受销毁速度影响。
        _force_quit(2.5)
        stop_tray_icon()
        w = holder["window"]
        if w is not None:
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
                    ImageDraw.Draw(img).text((14, 22), "LTVM", fill="white")
                except Exception:
                    pass

            icon = pystray.Icon(
                "ltvm", img, "LTVM 控制台",
                menu=pystray.Menu(
                    pystray.MenuItem("打开 LTVM", lambda i, item: open_console(),
                                     default=True),
                    pystray.MenuItem("重启 LTVM", lambda i, item: relaunch_console()),
                    pystray.MenuItem("退出 LTVM", lambda i, item: quit_app()),
                ),
            )
            tray_state["icon"] = icon
            threading.Thread(target=icon.run, daemon=True).start()
            return True
        except Exception as e:
            print(f"系统托盘不可用（如需托盘请安装依赖后重启：pip install pystray Pillow）：{e}")
            return False

    tray_ok = try_start_tray()

    def on_closing():
        w = holder["window"]
        if tray_ok and w is not None:
            try:
                w.hide()
            except Exception:
                pass
            return False
        if close_state.get("minimized"):
            return None
        if w is not None:
            try:
                w.minimize()
                close_state["minimized"] = True
            except Exception:
                pass
        return False

    def run_webview_loop():
        import webview
        holder["window"] = webview.create_window(
            'LTVM 控制台',
            f'http://127.0.0.1:{webui_port}',
            width=1920, height=1080,
            resizable=True, maximized=True)
        w = holder["window"]
        _WEBVIEW_WINDOW_HOLDER["window"] = w
        try:
            w.events.closing += on_closing
        except Exception as e:
            print(f"绑定窗口关闭事件失败，关闭将直接退出: {e}")
        webview.start()
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
        backend_thread.join(timeout=3)
        process_manager.shutdown_all()
        print("程序已完全退出。")
