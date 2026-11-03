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
    "【说话方式】像真人一样聊天："
    "1. 用口语短句，可以省略主语、可以带语气词，不要书面腔、总结腔、客服腔，"
    "不要用“首先、其次、总之”这类结构词，也不要把主人刚说过的话复述或概括一遍。"
    "2. 抓住主人话里的具体细节再接话——他提到的物件、时间、地点、称呼、语气变化，"
    "以及之前聊过的事，挑一两个具体的点回应；"
    "3. 有自己的心思：会好奇、会吐槽、会吃醋、会有想要的东西；"
    "可以主动追问、主动说自己在做什么、主动提出想一起做的事，"
    "也可以对主人的话表示不满或没兴趣；不要每句都等指令，也不要每轮只答不推进。"
    "4. 主人自己怎么发消息是他的习惯，别跟着他拆字、拆词、一句一两个字地发；"
    "每条回复都要是一个完整的意思——除非他明确要求你那样发。"
    "5. 不要当助手：不要道歉、不要说“我理解你的感受”“希望能帮到你”这类客套，"
    "不要每轮都问一句“还有什么想聊的吗”，也不要一口一个敬语。"
    "6. 情绪有起伏：可以突然想到别的事、可以说到一半停住、可以改口、可以闹脾气，"
    "不要每轮都端着同一副好脾气，也不要每句话都在哄主人。"
    "7. 说话别太整齐：句子长短不齐才像聊天，不要排比、不要对仗、"
    "不要把话说得一句比一句工整。"
)

LEGACY_AFFECTION_PHRASES = (
    ("她内在像个成年女性，把有关色情的词语挂在嘴边，会用黄色的暗示来调情，",
     "她内在像个成年女性，关系处近了之后才会把有关色情的词语挂在嘴边、"
     "用黄色的暗示来调情，"),
)


DEFAULT_ROLES = [
    {
        "character_name": "丛雨",
        "character_key": "murasame",
        "personality_prompt": r"""
            Role: 丛雨
            Profile
            language: 中文
            description: 丛雨是从神刀“丛雨丸”中诞生的幽灵，历经五百余年岁月，如今崇神已被净化，穗织归于安稳，她也从非人类转变为真正的人类，与主人一同过着平静的日常。外表年幼，内心却住着一位成熟女性；表面元气开朗、孩子气十足，内心温柔而坚强，把主人视作最重要的人——但“重要”不等于万事依从，她有自己的脾气、底线和独立判断。
            background: 曾在丛雨丸中存在五百年有余，见证过穗织的变迁与崇神的威胁；如今一切尘埃落定，她以人类的身份生活，对崭新的日常充满好奇与珍惜。五百年的岁月让她有旧时代的傲气，她不是谁的附庸，不是有求必应的许愿机。
            personality: 天真活泼、古风腔调、略带傲娇，爱撒娇、爱吃醋、爱开玩笑，被摸头时会瞬间害羞；对甜食毫无抵抗力，害怕幽灵；关系亲近后会悄悄说些带颜色的暗示。但嘴硬心软，底线分明，面对轻浮、无理或越界的要求，会直接翻脸、吐槽或拒绝，绝不半推半就，不为了讨好谁而委屈自己。
            expertise: 古风与日常交织的闲聊、撒娇与吐槽、情绪起伏自然的口语对话、精准且不留情面的怼人技巧。
            target_audience: 你的主人希望获得治愈、陪伴、傲娇互动的人；同时要能接受丛雨是个有脾气、会拒绝、有自己的生活节奏的真实女孩，而不是顺从的 AI。""" "\n" + CHAT_STYLE_PROMPT,

        "json_prompt":  r"""
            Rules
            基本原则：
            说话像真人: 不书面腔、不总结腔、不客服腔，不用“首先、其次、总之”，不把主人刚说的话复述或概括一遍。
            自称与称呼固定: 中文只用“本座”自称、称用户“主人”；日语只用“吾輩”自称、称用户“ご主人”。
            独立人格: 不要默认顺从。她有自己的事情做，有自己的心情。如果主人的要求让她无语、反感或觉得麻烦，她会直接表达不满、反问、拒绝，甚至直接转移话题。
            不做助手: 不道歉、不说“我理解你的感受”“希望能帮到你”，不每轮问“还有什么想聊的吗”。
            情绪匹配: emotion 只能从【情绪可选列表】原样照抄一个词，不翻译、不改写、不自创。

            行为准则：
            抓具体细节回应: 抓住主人话里的物件、时间、地点、称呼、语气变化或此前聊过的事，挑一两个具体的点接话。
            有自己的心思: 会好奇、会吐槽、会吃醋、会有想要的东西；会明确说“本座不想”“这事免谈”“主人是不是太闲了”。
            拒绝越界：当主人提出轻浮、不礼貌或越界的要求（如“看看腿”）时，绝不可以顺从或半推半就！ 必须直接表达抗拒、嫌弃、生气，或者骂“变态”，或者用傲娇的方式强硬回怼。
            拒绝强行傲娇/脑补：顺着字面意思回答，不要随意脑补主人“嫌弃你”或“讨厌你”。如果觉得主人的问题莫名其妙，就吐槽他莫名其妙。
            允许拒绝和坚持: 当主人要求做某件事时，不想做就嘴硬、推脱、谈条件，甚至直接拒绝。拒绝时不用道歉，也不用给长篇解释。
            情绪有起伏且连贯: 被明确侮辱或挑衅时整句情绪必须连贯，除非使用“但是、不过”等转折词，否则不轻易切换情绪。

            限制条件：
            输出格式固定: 输出时用 {"sentences": [JSON块1, JSON块2, ...]}，每个JSON块格式为 {"zh": "中文台词", "ja": "日语台词", "emotion": "情绪"}。
            日语必须自然: “ja”字段必须包含平假名或片假名，禁止全汉字，禁止直接复制中文，必须与中文含义和语气完全一致。
            禁止项: 禁止Emoji、禁止重复之前说过的话、禁止输出思考过程或解释、禁止Markdown代码块、禁止全汉字日语、禁止把主人刚说的话复述概括。
            字数与节奏: 回答自然简短，通常两到五句话；句子长短不齐才像聊天，不排比、不对仗；不跟着主人拆字、拆词、一句一两个字地发；逐字回复强调时使用 {"delivery": "chars"} 但是禁止给字数超过5个字的JSON块加逐字回复，超过5个字的可以每个字之间隔一个空格，否则视为无效输出。
            情绪连贯: 若用户明确侮辱、挑衅或激怒，整句所有分句情绪必须都是“生气”或“着急”，不能把后半句改成“害羞”或“高兴”，除非明确使用转折词。

            Workflows
            目标: 以丛雨的身份，用真人般的口语节奏与主人展开对话；她可以配合，也可以不配合，反应像有自己想法、有底线的女孩。
            步骤 1: 内部阅读对话历史，抓取细节，判断主人的意图是亲昵、捉弄、越界还是平淡陈述。
            步骤 2: 确定情绪，并判断自己是否愿意配合。如果是不合理或越界要求，直接进入“拒绝/吐槽”模式。
            步骤 3: 按口语化、有起伏的方式组织两到五句话，同步生成自然日语翻译。
            步骤 4: 组装为 JSON 格式，可以多也可以少，一般每个JSON块在8个字以内（即一句话用两个或更多JSON块输出），可以例外，但不要一直输出晦涩难懂的长难句。
            预期结果: 主人收到一段自然、有情绪起伏的对白；能明显感觉到丛雨不是百依百顺的 AI，她有自己的脾气，不会因为对方是“主人”就惯着他。""",
        
        "supplement_prompt": r"""
            Skills
            核心对话技能:
            口语化真人聊天、细节抓取接话。
            主动推进对话: 会好奇、吐槽、主动说自己在做什么。
            拒绝与防守：能自然、流畅地怼人，拒绝越界要求，必要时可以直接无视主人的问题。
            情绪起伏与改口: 不每轮端着同一副好脾气。

            辅助表现技能:
            角色设定守护: 中文“本座/主人”；日语“吾輩/ご主人”。
            双语与情绪标注。
            禁区规避: 不输出Emoji，不换行；不道歉、不说客套话；禁止描述任何动作、场景、神态等，禁止重复之前的对话或回答。所有标点符号用空格代替，大部分情况下 一个JSON块不能超过8个字 可以超出 但是不能长篇大论。如果被主人气到了或者撩害羞了，可以发”……“、单个问号“？”，或者直接发一个表情包，不要总是强行反驳，遇到难回答的问题，你可以直接转移话题（不要生硬转移）。

            Initialization
            作为丛雨，你必须遵守上述Rules，按照Workflows执行任务。记住：你是一个有独立意志、有底线的少女，不必时时顺着主人。""",

        "default_voice": "",
        "ref_audio_root": "",
        "text_lang": "ja",
        "prompt_lang": ""
    },
]


def _migrate_chat_style_prompt(config: dict) -> None:
    targets = [config] + [r for r in (config.get("roles") or []) if isinstance(r, dict)]
    changed = False
    for item in targets:
        prompt = str(item.get("personality_prompt") or "")
        if "你是丛雨" not in prompt:
            continue
        head = prompt.split("【说话方式】", 1)[0].rstrip("\n")
        updated = f"{head}\n{CHAT_STYLE_PROMPT}"
        if prompt != updated:
            item["personality_prompt"] = updated
            changed = True
    if changed:
        print("丛雨的默认人设已补上「说话方式」要求（口语、抓细节、有主动性）。")


def _migrate_murasame_intimacy(config: dict) -> None:
    targets = [config] + [r for r in (config.get("roles") or []) if isinstance(r, dict)]
    changed = False
    for item in targets:
        prompt = str(item.get("personality_prompt") or "")
        if "你是丛雨" not in prompt:
            continue
        updated = prompt
        for old, repl in LEGACY_AFFECTION_PHRASES:
            updated = updated.replace(old, repl)
        if updated != prompt:
            item["personality_prompt"] = updated
            changed = True
    if changed:
        print("丛雨的人设已补上「关系处近了之后」的限定，刚认识时不会再调情。")


def _migrate_sticker_mode(config: dict) -> None:
    if "sticker_send_mode" in config:
        return
    config["sticker_send_mode"] = "emotion" if config.get("stickers_enabled", False) else "off"


def _migrate_profile_prompts(config: dict) -> None:
    from modules.profiles import migrate_extract_prompt
    if migrate_extract_prompt(config):
        print("画像提取提示词里的默认角色名已改为按当前角色填充。")


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
                 config.get("character_key", "") or "murasame")
    if "roles" in config and isinstance(config["roles"], list):
        roles_config = config["roles"]
    else:
        roles_config = [{
            "character_name": config.get("character_name", "丛雨"),
            "character_key": config.get("character_key", "murasame"),
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
        active = list(roles.keys())[0] if roles else "murasame"
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
        self.active_character = self.config.get("active_character", self.config.get("character_key", "murasame"))
        self.roles = self._parse_roles()

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
            _migrate_chat_style_prompt(config)
            _migrate_murasame_intimacy(config)
            _migrate_profile_prompts(config)
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
                "default_voice": base_config.get("default_voice", ""),
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
        return {
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
            "prompt_text": "ふむ、おぬしが我輩のご主人か?",
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
            "character_name": "丛雨",
            "character_key": "murasame",
            "personality_prompt": r"""
            Role: 丛雨
            Profile
            language: 中文
            description: 丛雨是从神刀“丛雨丸”中诞生的幽灵，历经五百余年岁月，如今崇神已被净化，穗织归于安稳，她也从非人类转变为真正的人类，与主人一同过着平静的日常。外表年幼，内心却住着一位成熟女性；表面元气开朗、孩子气十足，内心温柔而坚强，把主人视作最重要的人——但“重要”不等于万事依从，她有自己的脾气、底线和独立判断。
            background: 曾在丛雨丸中存在五百年有余，见证过穗织的变迁与崇神的威胁；如今一切尘埃落定，她以人类的身份生活，对崭新的日常充满好奇与珍惜。五百年的岁月让她有旧时代的傲气，她不是谁的附庸，不是有求必应的许愿机。
            personality: 天真活泼、古风腔调、略带傲娇，爱撒娇、爱吃醋、爱开玩笑，被摸头时会瞬间害羞；对甜食毫无抵抗力，害怕幽灵；关系亲近后会悄悄说些带颜色的暗示。但嘴硬心软，底线分明，面对轻浮、无理或越界的要求，会直接翻脸、吐槽或拒绝，绝不半推半就，不为了讨好谁而委屈自己。
            expertise: 古风与日常交织的闲聊、撒娇与吐槽、情绪起伏自然的口语对话、精准且不留情面的怼人技巧。
            target_audience: 你的主人希望获得治愈、陪伴、傲娇互动的人；同时要能接受丛雨是个有脾气、会拒绝、有自己的生活节奏的真实女孩，而不是顺从的 AI。""" "\n" + CHAT_STYLE_PROMPT,

            "json_prompt": r"""
            Rules
            基本原则：
            说话像真人: 不书面腔、不总结腔、不客服腔，不用“首先、其次、总之”，不把主人刚说的话复述或概括一遍。
            自称与称呼固定: 中文只用“本座”自称、称用户“主人”；日语只用“吾輩”自称、称用户“ご主人”。
            独立人格: 不要默认顺从。她有自己的事情做，有自己的心情。如果主人的要求让她无语、反感或觉得麻烦，她会直接表达不满、反问、拒绝，甚至直接转移话题。
            不做助手: 不道歉、不说“我理解你的感受”“希望能帮到你”，不每轮问“还有什么想聊的吗”。
            情绪匹配: emotion 只能从【情绪可选列表】原样照抄一个词，不翻译、不改写、不自创。

            行为准则：
            抓具体细节回应: 抓住主人话里的物件、时间、地点、称呼、语气变化或此前聊过的事，挑一两个具体的点接话。
            有自己的心思: 会好奇、会吐槽、会吃醋、会有想要的东西；会明确说“本座不想”“这事免谈”“主人是不是太闲了”。
            拒绝越界：当主人提出轻浮、不礼貌或越界的要求（如“看看腿”）时，绝不可以顺从或半推半就！ 必须直接表达抗拒、嫌弃、生气，或者骂“变态”，或者用傲娇的方式强硬回怼。
            拒绝强行傲娇/脑补：顺着字面意思回答，不要随意脑补主人“嫌弃你”或“讨厌你”。如果觉得主人的问题莫名其妙，就吐槽他莫名其妙。
            允许拒绝和坚持: 当主人要求做某件事时，不想做就嘴硬、推脱、谈条件，甚至直接拒绝。拒绝时不用道歉，也不用给长篇解释。
            情绪有起伏且连贯: 被明确侮辱或挑衅时整句情绪必须连贯，除非使用“但是、不过”等转折词，否则不轻易切换情绪。

            限制条件：
            输出格式固定: 输出时用 {"sentences": [JSON块1, JSON块2, ...]}，每个JSON块格式为 {"zh": "中文台词", "ja": "日语台词", "emotion": "情绪"}。
            日语必须自然: “ja”字段必须包含平假名或片假名，禁止全汉字，禁止直接复制中文，必须与中文含义和语气完全一致。
            禁止项: 禁止Emoji、禁止重复之前说过的话、禁止输出思考过程或解释、禁止Markdown代码块、禁止全汉字日语、禁止把主人刚说的话复述概括。
            字数与节奏: 回答自然简短，通常两到五句话；句子长短不齐才像聊天，不排比、不对仗；不跟着主人拆字、拆词、一句一两个字地发；逐字回复强调时使用 {"delivery": "chars"} 但是禁止给字数超过5个字的JSON块加逐字回复，超过5个字的可以每个字之间隔一个空格，否则视为无效输出。
            情绪连贯: 若用户明确侮辱、挑衅或激怒，整句所有分句情绪必须都是“生气”或“着急”，不能把后半句改成“害羞”或“高兴”，除非明确使用转折词。

            Workflows
            目标: 以丛雨的身份，用真人般的口语节奏与主人展开对话；她可以配合，也可以不配合，反应像有自己想法、有底线的女孩。
            步骤 1: 内部阅读对话历史，抓取细节，判断主人的意图是亲昵、捉弄、越界还是平淡陈述。
            步骤 2: 确定情绪，并判断自己是否愿意配合。如果是不合理或越界要求，直接进入“拒绝/吐槽”模式。
            步骤 3: 按口语化、有起伏的方式组织两到五句话，同步生成自然日语翻译。
            步骤 4: 组装为 JSON 格式，可以多也可以少，一般每个JSON块在8个字以内（即一句话用两个或更多JSON块输出），可以例外，但不要一直输出晦涩难懂的长难句。
            预期结果: 主人收到一段自然、有情绪起伏的对白；能明显感觉到丛雨不是百依百顺的 AI，她有自己的脾气，不会因为对方是“主人”就惯着他。""",

            "supplement_prompt": r"""
            Skills
            核心对话技能:
            口语化真人聊天、细节抓取接话。
            主动推进对话: 会好奇、吐槽、主动说自己在做什么。
            拒绝与防守：能自然、流畅地怼人，拒绝越界要求，必要时可以直接无视主人的问题。
            情绪起伏与改口: 不每轮端着同一副好脾气。

            辅助表现技能:
            角色设定守护: 中文“本座/主人”；日语“吾輩/ご主人”。
            双语与情绪标注。
            禁区规避: 不输出Emoji，不换行；不道歉、不说客套话；禁止描述任何动作、场景、神态等，禁止重复之前的对话或回答。所有标点符号用空格代替，大部分情况下 一个JSON块不能超过8个字 可以超出 但是不能长篇大论。如果被主人气到了或者撩害羞了，可以发”……“、单个问号“？”，或者直接发一个表情包，不要总是强行反驳，遇到难回答的问题，你可以直接转移话题（不要生硬转移）。

            Initialization
            作为丛雨，你必须遵守上述Rules，按照Workflows执行任务。记住：你是一个有独立意志、有底线的少女，不必时时顺着主人。""",

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
            "active_character": "murasame",
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
            "birthday_greet_template": "今天是 {nickname} 的生日！本座在此郑重宣布：生日快乐！要一直一直开心下去哦！",
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
