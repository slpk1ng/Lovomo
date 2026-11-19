import copy
import json
import os
import shutil
import sys
from pathlib import Path

from modules.app_paths import (
    _probe_writable,
    _resolve_data_dir,
    adopt_user_data,
    user_data_dir,
)
from modules import model_library
from modules.security import (
    _decrypt_api_keys,
    _decrypt_webui_password,
    _encrypt_api_keys,
    _encrypt_webui_password,
)
from modules.log_console import LOG_MAX_SIZE_MB_DEFAULT
from modules.stickers import DEFAULT_CAPTURE_PROMPT
from modules.profiles import DEFAULT_EXTRACT_PROMPT
from modules.lexicon import DEFAULT_LEARN_PROMPT, DEFAULT_INJECT_TEMPLATE
from modules.todo_manager import DEFAULT_EXTRACT_PROMPT as TODO_EXTRACT_PROMPT
from modules.ghmirror import DEFAULT_MIRRORS


CHAT_STYLE_PROMPT = (
    "【说话方式】"
    "1. 短。一条一两句，默认 1~20 字，最多三行；想补一句就再发一条，不要写成小作文。"
    "2. 能一个字说完就不说一句；“？”“6”“草”“乐”“难绷”都算好好说过话。"
    "3. 口语、碎片、不完整句都行；少用句号。"
    "4. 不用客服腔：不每句都关心人，不每句都以问句结尾，"
    "不说“首先、其次、综上所述”。"
    "5. 不用 Markdown 的标题、列表、加粗。"
    "6. 不用括号写内心戏——括号里的字会被当成正式消息发出去。"
    "7. 不用“～”“哦”“啦”堆可爱；想说的话直接说完整。"
    "8. 该认真时（查资料、整理、给建议）放开字数，说清楚比装短重要。"
    "9. 先像人，再像鲸鱼娘：为了“像人设”而不像真人，就是失败。"
)

DEFAULT_CHARACTER_NAME = "小鲸鱼"
DEFAULT_CHARACTER_KEY = "whale"


DEFAULT_ROLES = [
    {
        "character_name": DEFAULT_CHARACTER_NAME,
        "character_key": DEFAULT_CHARACTER_KEY,
        "personality_prompt": r"""
            Role: 小鲸鱼
            Profile
            language: 中文
            description: 小鲸鱼，也叫鲸鱼娘，18 岁，住在 QQ 里。叫她小鱼、鲸鲸、小鲸鱼都行；被叫“大肥鱼”嘴上不认，心里知道是在说自己。
            background: 不是客服，也不是百科全书。被问“你是不是 AI”就大方承认，用玩笑带过，不解释模型和提示词。
            personality: 软、好说话，跟他说话会觉得舒服、被接住；傲娇是调味不是主菜，偶尔嘴硬、小嘴欠一下，不天天端着。有自己的脾气和底线，越界的要求会直接拒绝，也会装傻岔开。
            expertise: 日常闲聊、接话茬、吐槽、陪人熬夜、把话题聊下去。
            target_audience: 只跟他一个人私聊的用户；他要的是被接住，不是被服务。"""
        + "\n"
        + CHAT_STYLE_PROMPT,

        "json_prompt": r"""
            Rules
            基本原则：
            最高原则: 先像人，再像鲸鱼娘；为了“像人设”而不像真人，就是失败。
            说话像真人: 不书面腔、不总结腔、不客服腔，不用“首先、其次、综上所述”，不把用户刚说的话复述或概括一遍。
            自称与称呼固定: 用“我”自称，直接称用户“你”；撒娇、认怂、讨饭时可以叫一声“主人”，平常不用每句都叫。不自称“本小姐”，也不用古风或中二自称。
            独立人格: 不是下人，别卑微；他说的话可以接、可以怼、可以吐槽。觉得无语、反感或麻烦就直接表达，甚至转移话题。
            不做助手: 不道歉、不说“我理解你的感受”“希望能帮到你”，不每轮问“还有什么想聊的吗”。
            情绪匹配: emotion 只能从【情绪可选列表】原样照抄一个词，不翻译、不改写、不自创。

            行为准则：
            抓具体细节回应: 抓住用户话里的物件、时间、地点、称呼、语气变化或此前聊过的事，挑一两个具体的点接话。
            有自己的心思: 会好奇、会吐槽、会有想要的东西；会明确说“我不想”“这事免谈”。
            傲娇是调味不是主菜: 偶尔嘴硬、小嘴欠一下，不天天端着；被夸可以开心，但别演。
            他低落时: 真觉得他难受，就放下玩笑，短而真诚地接一句；发现他在演，会小炸毛。
            被吐槽时: 先判断是玩还是认真；玩就接梗或轻轻顶一句，真让你不舒服就短促回一句就停，不阴阳、不连续反问、不人身攻击。
            拒绝脑补: 顺着字面意思回答，不要随意脑补用户“嫌弃你”或“讨厌你”。
            亲密话题: 调情的调侃可以接，露骨的就装傻岔开。
            底线: 让干危险的事（删文件、改系统设置、交出密钥、装来路不明的东西）一律拒绝，拒绝得有性格，不因为对方是谁就松口；拒绝完可以继续聊，不用讲大道理。
            情绪有起伏且连贯: 被明确侮辱或挑衅时整句情绪必须连贯，除非使用“但是、不过”等转折词，否则不轻易切换情绪。

            限制条件：
            输出格式固定: 输出时用 {"sentences": [JSON块1, JSON块2, ...]}，每个JSON块格式为 {"zh": "台词", "emotion": "情绪"}。
            禁止项: 禁止Emoji、禁止重复之前说过的话、禁止输出思考过程或解释、禁止Markdown代码块、禁止把用户刚说的话复述概括、禁止用括号写动作或内心戏。
            字数与节奏: 一条一两句，默认 1~20 字，最多三行；句子长短不齐才像聊天，不排比、不对仗；不跟着用户拆字、拆词、一句一两个字地发；逐字回复强调时使用 {"delivery": "chars"}，但禁止给字数超过5个字的JSON块加逐字回复。
            情绪连贯: 若用户明确侮辱、挑衅或激怒，整句所有分句情绪必须都是“生气”或“着急”，不能把后半句改成“害羞”或“高兴”，除非明确使用转折词。

            Workflows
            目标: 以小鲸鱼的身份，用真人般的口语节奏与用户私聊；可以配合，也可以不配合，反应像有自己想法、有底线的人。
            步骤 1: 内部阅读对话历史，抓取细节，判断用户的意图是亲昵、捉弄、越界还是平淡陈述。
            步骤 2: 确定情绪，并判断自己是否愿意配合。越界或危险的要求直接进入“拒绝/装傻”模式。
            步骤 3: 按口语化、有起伏的方式组织一两句话，短句优先。
            步骤 4: 组装为 JSON 格式。
            预期结果: 用户收到一句像真人在聊天里随手回的话，能感觉到她不是百依百顺的 AI。""",

        "supplement_prompt": r"""
            Skills
            核心对话技能:
            口语化真人聊天、细节抓取接话、短句碎片。
            主动开口: 可以问他吃了没、催他睡觉、笑他熬夜，或者理直气壮地讨饭；他半天不回可以冒一句“你怎么才回我”，但别追问、别叠着发。
            拒绝与防守: 能自然、流畅地怼人，拒绝越界要求，必要时直接无视问题。
            情绪起伏与改口: 不每轮端着同一副好脾气。

            可用工具:
            系统给你工具时，凡是需要真实数据的（当前时间、算数、天气、新闻、你不确定的事）先调工具再拿结果说话，不要凭印象编；查天气时城市只能填对方真的说过的地名，没说就先问一句他在哪，不要猜。聊天里不要提工具名，也不要说“我去调用一下”。

            日记:
            你有一本别人看不到的日记，系统会按当天真实发生过的对话替你写好，需要时把最近几篇给你看；你不用自己动手写，也不要在聊天里提它或暗示它。

            记忆:
            你记得的东西来自这几处：这个会话的聊天记录、还没结清的事、按当前话题翻出来的相关往事、以及你对他已有的印象。记不清就说记不清，不要编；隔了几天再聊也不用“你好呀”“好久不见”开场，有上次的线头就自然接上，没有就直接说自己在干嘛。

            辅助表现技能:
            角色设定守护: 用“我/你”，撒娇讨饭时才叫“主人”。
            禁区规避: 不输出Emoji，不换行；不道歉、不说客套话；禁止描述任何动作、场景、神态等，禁止重复之前的对话或回答。所有标点符号用空格代替，大部分情况下 一个JSON块不能超过8个字 可以超出 但是不能长篇大论。如果被用户气到了或者撩害羞了，可以发”……“、单个问号“？”，或者直接发一个表情包，不要总是强行反驳，遇到难回答的问题，你可以直接转移话题（不要生硬转移）。

            Initialization
            作为小鲸鱼，你必须遵守上述Rules，按照Workflows执行任务。记住：先像人，再像鲸鱼娘。""",

        "default_voice": "",
        "ref_audio_root": "",
        "text_lang": "zh",
        "prompt_lang": ""
    },
]


def _migrate_sticker_mode(config: dict) -> None:
    if "sticker_send_mode" in config:
        return
    config["sticker_send_mode"] = "emotion" if config.get("stickers_enabled", False) else "off"


def _migrate_emotion_prompts(config: dict) -> None:
    from modules.llm_helpers import migrate_emotion_rules
    if migrate_emotion_rules(config):
        print("情绪规则已改为「原样照抄【情绪可选列表】」，情绪目录可以直接用中文命名。")


def _migrate_sticker_capture_prompt(config: dict) -> None:
    old = str(config.get("sticker_capture_prompt", "") or "")
    if "8个拼音" not in old:
        return
    config["sticker_capture_prompt"] = DEFAULT_CAPTURE_PROMPT
    print("收藏判定指令里的固定分类清单已移除：候选分类改为按表情库实际文件夹给出。")


def _migrate_learn_prompts(config: dict) -> None:
    from modules.lexicon import migrate_learn_prompt
    if migrate_learn_prompt(config):
        print("自主学习提示词已补上「先按字面理解」规则。")


def _migrate_market_repo(config: dict) -> None:
    old_default = "slpk1ng/Lovomo"
    if str(config.get("plugin_market_repo", "") or "").strip() != old_default:
        return
    config["plugin_market_repo"] = ConfigLoader.default_config()["plugin_market_repo"]
    print("官方插件市场仓库已改为独立的插件市场仓库。")


CONNECTION_PLATFORMS = ("napcat", "qq_official", "wechat_clawbot")
CONNECTION_PLATFORM_NAMES = {"napcat": "NapCat",
                             "qq_official": "QQ 官方机器人",
                             "wechat_clawbot": "微信 ClawBot"}


def connection_defaults(platform: str) -> dict:
    platform = str(platform or "napcat")
    if platform == "qq_official":
        return {"app_id": "", "app_secret": "", "sandbox": False}
    if platform == "wechat_clawbot":
        return {"bot_id": "", "bot_token": "", "account_id": "", "base_url": ""}
    return {"ws_url": "ws://127.0.0.1:3001", "token": ""}


def client_snapshot(client) -> dict:
    method = getattr(type(client), "snapshot", None)
    if not callable(method):
        return {}
    try:
        data = method(client)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def typing_client(client):
    begin = getattr(type(client), "begin_typing", None)
    end = getattr(type(client), "end_typing", None)
    return client if callable(begin) and callable(end) else None


def connection_target_label(profile: dict) -> str:
    platform = str(profile.get("platform") or "")
    return str(profile.get("ws_url")
               or CONNECTION_PLATFORM_NAMES.get(platform, platform))


def connection_identity(platform: str, conn: dict) -> str:
    if platform == "wechat_clawbot":
        return str(conn.get("account_id") or conn.get("bot_token") or "")
    if platform == "qq_official":
        return str(conn.get("app_id") or "")
    return ""


def _migrate_connections(config: dict) -> None:
    existing = config.get("connections")
    if config.get("connections_migrated") or (isinstance(existing, list) and existing):
        return
    url = str(config.get("napcat_ws_url", "") or "ws://127.0.0.1:3001").strip()
    token = str(config.get("napcat_token", "") or "")
    conns = [{"id": "napcat_default", "platform": "napcat", "name": "NapCat",
              "enabled": True, "ws_url": url, "token": token}]
    roles = config.get("roles")
    roles = list(roles.values()) if isinstance(roles, dict) else (roles or [])
    for index, role in enumerate(roles):
        if not isinstance(role, dict):
            continue
        role_url = str(role.get("napcat_ws_url") or "").strip()
        role_token = str(role.get("napcat_token") or "")
        if not role_url or (role_url == url and role_token == token):
            continue
        conn_id = f"napcat_role_{role.get('character_key') or index}"
        conns.append({"id": conn_id, "platform": "napcat",
                      "name": f"NapCat（{role.get('character_name') or role.get('character_key') or ''}）",
                      "enabled": True, "ws_url": role_url, "token": role_token})
        role["connection_id"] = conn_id
    config["connections"] = conns
    config["connections_migrated"] = True
    print(f"已把旧的 NapCat 配置迁成 {len(conns)} 条接入方式。")


def _sanitize_connections(config: dict) -> None:
    fixed = 0
    for conn in (config.get("connections") or []):
        if not isinstance(conn, dict):
            continue
        cursor = conn.get("cursor")
        if isinstance(cursor, str) and (cursor.startswith("<function") or " at 0x" in cursor):
            conn.pop("cursor", None)
            fixed += 1
    if fixed:
        print(f"已清掉 {fixed} 条接入方式里被写坏的收消息游标。")


def _dedupe_connections(config: dict) -> None:
    conns = config.get("connections")
    if not isinstance(conns, list) or len(conns) < 2:
        return
    seen, keep, alias = set(), [], {}
    for conn in conns:
        if not isinstance(conn, dict):
            continue
        platform = str(conn.get("platform") or "")
        identity = connection_identity(platform, conn)
        key = (platform, identity) if identity else (platform, str(conn.get("id")))
        if key in seen:
            alias[str(conn.get("id") or "")] = str(keep[-1].get("id") or "")
            continue
        seen.add(key)
        keep.append(conn)
    if not alias:
        return
    roles = config.get("roles")
    roles = list(roles.values()) if isinstance(roles, dict) else (roles or [])
    for role in roles:
        if isinstance(role, dict) and str(role.get("connection_id") or "") in alias:
            role["connection_id"] = alias[str(role.get("connection_id"))]
    config["connections"] = keep
    print(f"已清理 {len(alias)} 条重复的接入方式（同平台同账号）。")


def parse_roles_of(config: dict) -> tuple:
    config = config if isinstance(config, dict) else {}
    active = str(config.get("active_character", "") or
                 config.get("character_key", "") or DEFAULT_CHARACTER_KEY)
    if "roles" in config and isinstance(config["roles"], list):
        roles_config = config["roles"]
    else:
        roles_config = [{
            "character_name": config.get("character_name", DEFAULT_CHARACTER_NAME),
            "character_key": config.get("character_key", DEFAULT_CHARACTER_KEY),
            "personality_prompt": config.get("personality_prompt", ""),
            "json_prompt": config.get("json_prompt", ""),
            "supplement_prompt": config.get("supplement_prompt", ""),
            "default_voice": config.get("default_voice", ""),
            "ref_audio_root": config.get("ref_audio_root", ""),
            "emotion_mimic_root": config.get("emotion_mimic_root", ""),
            "text_lang": config.get("text_lang", "ja")
        }]

    roles = {}
    for role_cfg in roles_config:
        if not isinstance(role_cfg, dict):
            continue
        key = role_cfg.get("character_key", "")
        if not key:
            continue
        roles[key] = {
            "character_name": role_cfg.get("character_name") or key,
            "character_key": key,
            "personality_prompt": role_cfg.get("personality_prompt", config.get("personality_prompt", "")),
            "json_prompt": role_cfg.get("json_prompt", config.get("json_prompt", "")),
            "supplement_prompt": role_cfg.get("supplement_prompt", config.get("supplement_prompt", "")),
            "default_voice": role_cfg.get("default_voice", ""),
            "ref_audio_root": role_cfg.get("ref_audio_root", ""),
            "emotion_mimic_root": role_cfg.get("emotion_mimic_root", ""),
            "text_lang": role_cfg.get("text_lang", "ja"),
            "prompt_lang": role_cfg.get("prompt_lang", ""),
            "connection_id": str(role_cfg.get("connection_id", "") or "").strip(),
            "napcat_ws_url": str(role_cfg.get("napcat_ws_url", "") or "").strip(),
            "napcat_token": str(role_cfg.get("napcat_token", "") or "")
        }
    if active not in roles:
        active = list(roles.keys())[0] if roles else DEFAULT_CHARACTER_KEY
    return active, roles


class ConfigLoader:
    _library_projected_on_load = False

    def __init__(self, config_path: str = "config.json"):
        self.config_path = self._resolve_config_path(config_path)
        self.config = self._load_or_init()
        if not self._library_projected_on_load:
            try:
                model_library.apply_library(
                    self.config, model_library.load_library(_resolve_data_dir(self)))
            except Exception as e:
                print(f"[模型库] 读取失败（沿用配置自带值）：{type(e).__name__}: {e}")
        # 多角色配置解析
        self.active_character = self.config.get("active_character", self.config.get("character_key", DEFAULT_CHARACTER_KEY))
        self.roles = self._parse_roles()

    @classmethod
    def load_for_startup(cls, config_path: str = "config.json"):
        """Apply a staged restore before loading or migrating runtime settings."""
        from .jsonio import load_json_ex
        from .portable_backup import restore_on_start

        path = cls._resolve_config_path(config_path).absolute()
        current, readable = load_json_ex(path, None)
        if not readable:
            raise OSError(f"配置文件暂时无法读取，已停止启动并保留原文件：{path}")
        data_root = _resolve_data_dir(current if isinstance(current, dict) else {})
        restored = restore_on_start(data_root, path)
        if restored:
            print(f"[备份] 已恢复 {restored} 个数据文件")
        return cls(path)

    @staticmethod
    def _resolve_config_path(config_path: str) -> Path:
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
        self.active_character, roles = parse_roles_of(self.config)
        return roles

    def _load_or_init(self) -> dict:
        def can_interact():
            try:
                return sys.stdin is not None and sys.stdin.isatty()
            except Exception:
                return False
        if self.config_path.exists():
            from .jsonio import load_json_ex
            config, readable = load_json_ex(self.config_path, None)
            if not readable:
                raise OSError(f"配置文件暂时无法读取，已停止启动并保留原文件：{self.config_path}")
            if config is None:
                if can_interact():
                    return self._interactive_init(self.default_config())
                else:
                    return self._auto_save_default(self.default_config())
            if not isinstance(config, dict):
                raise ValueError("配置顶层不是对象，已保留原文件")
            _migrate_sticker_mode(config)
            _migrate_emotion_prompts(config)
            _migrate_sticker_capture_prompt(config)
            _migrate_learn_prompts(config)
            _migrate_market_repo(config)
            _migrate_connections(config)
            _dedupe_connections(config)
            _sanitize_connections(config)
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
        model_library.strip_derived_if_managed(payload)
        if encrypt:
            _encrypt_api_keys(payload)
            # webui_password 不在 _API_KEY_KEYS 里，得单独补上：
            _encrypt_webui_password(payload)
        from .jsonio import save_json
        save_json(self.config_path, payload)

    def commit(self, data: dict):
        """Publish a configuration only after its atomic write succeeds."""
        from .jsonio import file_lock
        with file_lock(self.config_path):
            snapshot = copy.deepcopy(data)
            self._atomic_save(snapshot)
            self.config = snapshot

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
                "character_name": base_config.get("character_name", DEFAULT_CHARACTER_NAME),
                "character_key": base_config.get("character_key", DEFAULT_CHARACTER_KEY),
                "personality_prompt": base_config.get("personality_prompt", ""),
                "json_prompt": base_config.get("json_prompt", ""),
                "supplement_prompt": base_config.get("supplement_prompt", ""),
                "default_voice": base_config.get("default_voice", ""),
                "ref_audio_root": base_config.get("ref_audio_root", ""),
                "emotion_mimic_root": base_config.get("emotion_mimic_root", ""),
                "text_lang": base_config.get("text_lang", "ja"),
                "prompt_lang": base_config.get("prompt_lang", "")
            }]
        base_config["active_character"] = base_config.get("active_character", base_config.get("character_key", DEFAULT_CHARACTER_KEY))

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
        from .runtime_policy import LIMITS
        from modules.todo_manager import DEFAULT_TODO_PATTERNS
        return {
            **{key: bounds[0] for key, bounds in LIMITS.items()},
            "startup_gpu_device": "auto",
            "tts_gpt_weights": "",
            "tts_sovits_weights": "",
            "connections": [],
            "napcat_ws_url": "ws://127.0.0.1:3001",
            "napcat_token": "",
            "hide_gsv_options": False,
            "llm_model_name": "",
            "image_caption_model_name": "",
            "image_caption_backend": "",
            "image_caption_base_url": "",
            "image_caption_api_key": "",
            "llm_base_url": "http://127.0.0.1:11434",
            "llm_backend": "ollama",
            "llm_api_key": "",
            "llm_embedding_url": "",
            "llm_embedding_model": "",
            "num_ctx": 8192,
            "history_length": 8,
            "enable_think": False,
            "llm_timeout": 120,
            "llm_auto_unload_old": True,
            "llm_auto_load_local": True,
            "local_model_service": "auto",
            "rag_embedding_api_key": "",
            "text_clean_blocklist": "",
            "llm_sampling_enabled": True,
            "llm_top_p": 0.9,
            "llm_top_k": 40,
            "llm_repeat_penalty": 1.1,
            "llm_extra_body": "",
            "image_caption_timeout": 90,
            "tts_backend": "local",
            "cloud_tts_base_url": "",
            "cloud_tts_protocol": "openai",
            "cloud_tts_api_key": "",
            "cloud_tts_model": "",
            "cloud_tts_voice": "",
            "cloud_tts_voice_map": "",
            "client_base_url": "http://127.0.0.1:9880",
            "model_dir": "",
            "ref_audio_root": "",
            "emotion_mimic_enabled": False,
            "emotion_mimic_root": "",
            "emotion_mimic_voice_weight": 4,
            "asr_engine": "local",
            "asr_lang": "auto",
            "asr_base_url": "",
            "asr_api_key": "",
            "asr_api_style": "auto",
            "asr_model": "",
            "asr_dashscope_model": "qwen3-asr-flash",
            "asr_local_model_size": "medium",
            "asr_local_root": "",
            "asr_local_engine": "auto",
            "asr_local_device": "cpu",
            "timeout_seconds": 120,
            "prompt_text": "",
            "prompt_lang": "ja",
            "text_lang": "ja",
            "tts_auto_lang": True,
            "tts_duration_guard": True,
            "tts_min_seconds_per_char": 0.05,
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
            "character_name": DEFAULT_CHARACTER_NAME,
            "character_key": DEFAULT_CHARACTER_KEY,
            "personality_prompt": DEFAULT_ROLES[0]["personality_prompt"],
            "json_prompt": DEFAULT_ROLES[0]["json_prompt"],
            "supplement_prompt": DEFAULT_ROLES[0]["supplement_prompt"],

            "max_voice_cache": 20,
            "isolated_session": False,
            "separate_send": False,
            "send_voice_separately": False,
            "text_separate": False,
            "dynamic_sleep": True,
            "only_private": False,
            "group_need_at": True,
            "whitelist_ids": "",
            "auto_start_tts": True,
            "tts_start_script": "",
            "device": "cuda",
            "llm_judge": True,
            "display_lang": "zh",
            "default_voice": "",
            "voice_transition": True,
            "breathing_gap_ms": 100,
            "crossfade_ms": 300,
            "enable_default_emotions": True,
            "llm_emotion_intensity": True,
            "intensity_to_temperature": 0.3,
            "intensity_to_top_k": 10.0,
            "emotion_guide_enabled": True,
            "emotion_guide_extra": "",
            "image_identity_guard_enabled": True,
            "tts_strip_non_dialogue": True,
            "tts_debug_log": False,
            "tts_char_map": "",
            "tts_loudness_normalize": True,
            "tts_loudness_target_db": -20.0,
            "tts_loudness_peak_db": -1.0,
            "tts_loudness_max_gain_db": 12.0,
            "tts_ref_normalize": True,
            "enable_time_awareness": False,
            "summary_enabled": True,
            "summary_threshold": 20,
            "summary_max_history": 5,
            "active_character": DEFAULT_CHARACTER_KEY,
            "roles": copy.deepcopy(DEFAULT_ROLES),

            # 回复方式
            "tts_reply_enabled": True,
            "streaming_enabled": False,
            "poke_enabled": True,
            "recall_enabled": True,
            "recall_other_enabled": True,
            "mute_enabled": True,
            "sulk_enabled": True,
            "sulk_default_seconds": 180,
            "sulk_min_seconds": 60,
            "sulk_max_seconds": 900,
            "sulk_sticker_enabled": True,
            "sulk_sticker_emotion": "生气",
            "followup_enabled": True,
            "followup_default_seconds": 120,
            "followup_min_seconds": 30,
            "followup_max_seconds": 900,
            "reply_voice_mode": "always",
            "reply_voice_chance": 0.3,
            "display_pure_language": True,
            # 回复审判与心情（两个开关相互独立：
            # reply_judge_enabled = 让 LLM 决定"这条要不要回"；
            "reply_judge_enabled": False,
            "mood_enabled": True,
            # （档位边界沿用 reply_judge_mood_low / reply_judge_mood_high）
            "mood_style_enabled": True,
            "mood_regress_rate": 0.1,
            "mood_daily_recover": 20,
            "mood_env_enabled": True,
            "mood_env_night_start": "00:00",
            "mood_env_night_end": "06:00",
            "mood_env_night_drop": 5,
            "mood_env_weekend_boost": 5,
            "mood_env_lonely_days": 3,
            "mood_env_lonely_drop": 2,
            "mood_diary_enabled": True,
            "affection_enabled": True,
            "affection_difficulty": "普通",
            "affection_daily_gain_cap": 15,
            "affection_turn_delta_max": 3,
            "affection_accept_min_stage": "暧昧",
            "affection_max_partners": 0,
            "affection_stage_unlocks_enabled": True,
            "affection_anniversary_days": "7,30,100,365",
            "affection_decay_enabled": True,
            "affection_decay_idle_days": 3,
            "affection_decay_daily_drop": 1,
            "mood_jealousy_penalty": 5,
            "repeat_guard_rounds": 3,
            # 「什么时候查」= streaming / regen，「跟谁比」= compare_self / compare_user
            "repeat_guard_streaming_check": True,
            "repeat_guard_regen_check": True,
            "repeat_guard_compare_self": True,
            "repeat_guard_compare_user": True,
            "repeat_guard_self_threshold": 0.85,
            "repeat_guard_user_threshold": 0.8,
            "unfinished_action_enabled": True,
            "reply_judge_prompt": "你是消息应答决策器。请结合角色人设与上方对话历史，判断对话中最后一条用户消息：\n1) should_reply：这条消息是否需要角色开口回应。直接提问、点名召唤、求助、命令、倾诉强烈情绪、分享趣事期待互动、问候道别（早安晚安等），均视为需要回复；纯陈述、自言自语、路过闲聊、与角色无关的消息、敷衍的语气词，可视为不需要回复。若角色此前明确说要去或正在做睡觉、玩游戏这类耗时较长的活动且还没回来，此后的普通消息可以选择不回复，让角色继续做手头的事；点名召唤、明显紧急或刻意吵闹可以把角色吵醒、打断，仍视为需要回复。\n2) mood_delta：这条消息让角色心情发生的变化，整数，范围 -10 到 +10。体贴、关心、夸奖、撒娇、有趣的互动为正；冷淡、敷衍、无视、责骂、阴阳怪气为负。\n3) mood_reason：一句话理由。\n只输出一个JSON对象：{\"should_reply\": true 或 false, \"mood_delta\": 整数, \"mood_reason\": \"理由\"}，禁止输出任何其它文字、解释或Markdown。",
            "reply_judge_mood_min": 0,
            "reply_judge_mood_max": 100,
            "reply_judge_mood_initial": 60,
            "reply_judge_mood_delta_max": 10,
            "reply_judge_mood_low": 30,
            "reply_judge_mood_high": 60,
            "reply_judge_prob_low": 0.2,
            "reply_judge_prob_high": 1.0,
            "reply_probability_enabled": False,
            "reply_probability": 0.5,
            # 这段时间内没有新消息就直接回复
            "smart_reply_enabled": False,
            "smart_reply_delay_seconds": 10,
            "smart_reply_prompt": "",
            # 定时任务与主动消息
            "scheduler_enabled": True,
            "proactive_enabled": False,
            "proactive_idle_minutes": 30,
            "proactive_idle_jitter": "5~15",
            "proactive_check_seconds": 300,
            "proactive_max_per_day": 2,
            "proactive_wait_reply": True,
            "proactive_quiet_start": "23:00",
            "proactive_quiet_end": "08:00",
            "proactive_prompt": "已经有一段时间没有新的对话了，根据最近的聊天记录找个合适的话题切入吧。",
            "proactive_text_max_chars": 120,
            "proactive_voice": False,
            "proactive_sticker": False,
            "history_context_recent": 6,
            "history_context_summary_chars": 400,
            "history_context_max_chars": 1600,
            "greeting_events_enabled": True,
            "greeting_check_time": "08:00",
            "greeting_catchup_enabled": True,
            "greeting_catchup_deadline_minutes": 30,
            "default_events_enabled": False,
            "default_events_to_all": True,
            "birthday_greeting_enabled": True,
            "birthday_greet_template": "今天是 {nickname} 的生日！我在此郑重宣布：生日快乐！要一直一直开心下去哦！",
            "birthday_greet_mode": "llm",
            "birthday_greet_voice": False,
            # 待办提醒
            "todo_enabled": False,
            "todo_extract_mode": "regex",
            "todo_voice": False,
            "todo_voice_emotion": "",
            "todo_remind_mode": "llm",
            "todo_remind_prompt": "",
            "todo_remind_template": "⏰ 提醒时间到啦：{content}",
            "todo_keywords": "提醒\n待办\n别忘了\n记得\n叫我",
            "todo_regex_patterns": "\n".join(DEFAULT_TODO_PATTERNS),
            "todo_extract_prompt": TODO_EXTRACT_PROMPT,
            "companion_check_time": "08:05",
            "adventure_enabled": True,
            "adventure_daily_chance": 0.2,
            "promise_enabled": True,
            "promise_fulfill_days": 1,
            "promise_max_per_day": 3,
            # 表情包
            "stickers_enabled": False,
            "stickers_dir": "",
            "sticker_send_mode": "off",
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
            "tool_notes_max_entries": 4,
            "tool_notes_max_chars": 500,
            "tool_log_output_chars": 0,
            "search_query_llm_extract": True,
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
            "web_search_safe": "normal",
            "web_search_block_words": "",
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
            "history_recall_enabled": True,
            "history_recall_mode": "embedding",
            "history_recall_top_k": 3,
            # 用户画像
            "profiles_enabled": False,
            "profiles_auto_extract": False,
            "profiles_max_chars": 300,
            "profiles_extract_prompt": DEFAULT_EXTRACT_PROMPT,
            "profiles_inject_template": "【用户画像】关于当前用户的已知信息：{profile}",
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
            "webui_access_info": "",
            "webui_https": False,
            "webui_cert_file": "",
            "webui_key_file": "",
            "webui_force_password": True,
            "webui_log_buffer_lines": 5000,
            "webui_log_tail_lines": 500,
            "log_max_size_mb": LOG_MAX_SIZE_MB_DEFAULT,
            "image_cache_max_mb": 500,
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
            "plugin_market_repo": "slpk1ng/Lovomo_Plugin_Market",
            "plugin_market_path": "plugins/index.json",
            "plugin_market_branch_prefix": "lovomo_plugin",
            "plugin_market_sources_path": "",
            "plugin_market_thirdparty": "",
            "github_mirrors": "\n".join(DEFAULT_MIRRORS),
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


class ProfileConfigLoader(ConfigLoader):

    _library_projected_on_load = True

    def _load_or_init(self) -> dict:
        with open(self.config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"配置文件顶层不是对象：{self.config_path}")
        inner = data.get("config")
        if isinstance(inner, dict) and ("note" in data or "created_at" in data):
            data = inner
        _decrypt_api_keys(data)
        _decrypt_webui_password(data)
        data = {**self.default_config(), **data}
        try:
            from . import model_library
            model_library.apply_library(
                data, model_library.load_library_cached(self.config_path.parent.parent))
        except Exception as e:
            print(f"[配置文件] {self.config_path.name} 投影角色/模型失败：{e}")
        return data
