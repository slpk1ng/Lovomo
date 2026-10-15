# ConfigLoader 配置装载/迁移/持久化 + 全部配置迁移函数 + 接入方式常量与净化辅助。
# 自 main.py 原样搬迁：函数体、注释、docstring 一字未改。
import json
import os
import shutil
import sys
from pathlib import Path

from modules.app_paths import (
    _probe_writable,
    adopt_user_data,
    user_data_dir,
)
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


# 角色说话的“人味”要求：口语、抓细节、有自己的心思，而不是复述与总结。
# 单独抽成一段，是为了能安全地补进老配置——只追加这一段，人设其余部分原样保留。
CHAT_STYLE_PROMPT = (
    "【说话方式】像真人一样聊天："
    "1. 用口语短句，可以省略主语、可以带语气词，不要书面腔、总结腔、客服腔，"
    "不要用“首先、其次、总之”这类结构词，也不要把主人刚说过的话复述或概括一遍。"
    "2. 抓住主人话里的具体细节再接话——他提到的物件、时间、地点、称呼、语气变化，"
    "以及之前聊过的事，挑一两个具体的点回应；"
    "不要只用“好的”“这样啊”这类放到任何对话里都成立的废话。"
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

# 默认人设里「把色情词语挂在嘴边、会用黄色暗示调情」原来是无条件的，模型会当成
# 任何阶段都成立的指令，于是刚认识就调情、把气氛直接搞成暧昧。补上"关系处近了之后"。
LEGACY_AFFECTION_PHRASES = (
    ("她内在像个成年女性，把有关色情的词语挂在嘴边，会用黄色的暗示来调情，",
     "她内在像个成年女性，关系处近了之后才会把有关色情的词语挂在嘴边、"
     "用黄色的暗示来调情，"),
)


def _migrate_chat_style_prompt(config: dict) -> None:
    """丛雨的默认人设没有「说话方式」要求（或还是旧版）：补上 / 换成最新的一版。

    只动这一段程序自己追加的文案，人设其余部分与别的角色原样保留。
    """
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
    """默认人设里的调情描述原来是无条件的：补上「关系处近了之后」。

    亲密到什么程度该由「关系进度」的阶段说明来管，人设里一句无条件的
    「会用黄色暗示调情」会让模型刚认识就照做，把气氛直接搞成暧昧。
    """
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
    """老配置只有 stickers_enabled：折算成新的发送方式，避免升级后表情包被静默关掉。"""
    if "sticker_send_mode" in config:
        return
    config["sticker_send_mode"] = "emotion" if config.get("stickers_enabled", False) else "off"


def _migrate_profile_prompts(config: dict) -> None:
    """老配置的画像提取提示词里写死了内置默认角色名：换成占位符，避免它串进别的角色对话。"""
    from modules.profiles import migrate_extract_prompt
    if migrate_extract_prompt(config):
        print("画像提取提示词里的默认角色名已改为按当前角色填充。")


def _migrate_emotion_prompts(config: dict) -> None:
    """老配置的情绪规则要求只输出拼音/英文：改成照抄【情绪可选列表】，情绪目录才能用中文名。"""
    from modules.llm_helpers import migrate_emotion_rules
    if migrate_emotion_rules(config):
        print("情绪规则已改为「原样照抄【情绪可选列表】」，情绪目录可以直接用中文命名。")


def _migrate_sticker_capture_prompt(config: dict) -> None:
    """老配置的收藏指令写死了内置拼音分类：分类清单已改为按表情库实际文件夹给出。"""
    old = str(config.get("sticker_capture_prompt", "") or "")
    if "8个拼音" not in old:
        return
    config["sticker_capture_prompt"] = DEFAULT_CAPTURE_PROMPT
    print("收藏判定指令里的固定分类清单已移除：候选分类改为按表情库实际文件夹给出。")


def _migrate_learn_prompts(config: dict) -> None:
    """老配置的学习提示词没写「先按字面理解」：补上，避免学到的含义被概括成抽象状态。"""
    from modules.lexicon import migrate_learn_prompt
    if migrate_learn_prompt(config):
        print("自主学习提示词已补上「先按字面理解」规则。")


def _migrate_market_repo(config: dict) -> None:
    """老配置的市场仓库还是程序源码仓库：官方市场已挪到独立的插件市场仓库。"""
    old_default = "slpk1ng/Lovomo"
    if str(config.get("plugin_market_repo", "") or "").strip() != old_default:
        return
    config["plugin_market_repo"] = ConfigLoader.default_config()["plugin_market_repo"]
    print("官方插件市场仓库已改为独立的插件市场仓库。")


# 支持的接入方式：NapCat、QQ 官方机器人、微信 ClawBot
CONNECTION_PLATFORMS = ("napcat", "qq_official", "wechat_clawbot")
CONNECTION_PLATFORM_NAMES = {"napcat": "NapCat",
                             "qq_official": "QQ 官方机器人",
                             "wechat_clawbot": "微信 ClawBot"}


def connection_defaults(platform: str) -> dict:
    """各平台接入方式的默认字段（新建一条时用）。"""
    platform = str(platform or "napcat")
    if platform == "qq_official":
        return {"app_id": "", "app_secret": "", "sandbox": False}
    if platform == "wechat_clawbot":
        # bot_token / account_id / base_url 都是扫码登录成功后写回来的
        return {"bot_id": "", "bot_token": "", "account_id": "", "base_url": ""}
    return {"ws_url": "ws://127.0.0.1:3001", "token": ""}


def client_snapshot(client) -> dict:
    """取客户端的落盘快照（游标 + context_token）；没有这个能力的返回空字典。

    必须查类属性，不能写 `hasattr(client, "snapshot")`：NapCat 客户端的
    __getattr__ 会给任意属性返回一个协程，hasattr 恒为真，
    一调用就报 "'coroutine' object has no attribute 'get'"。
    """
    method = getattr(type(client), "snapshot", None)
    if not callable(method):
        return {}
    try:
        data = method(client)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def typing_client(client):
    """取这条接入方式的「正在输入」入口；不支持返回 None。

    同样必须查类属性：NapCat 客户端的 __getattr__ 会给任意属性返回函数，
    hasattr/getattr 探不出真假。
    """
    begin = getattr(type(client), "begin_typing", None)
    end = getattr(type(client), "end_typing", None)
    return client if callable(begin) and callable(end) else None


def connection_target_label(profile: dict) -> str:
    """连接清单里每条"连到哪儿"的描述。

    各平台的字段不一样：NapCat 是地址，微信/QQ 走各自的协议没有地址，
    直接取 ws_url 会在启动日志那里抛 KeyError 把整个程序带崩。
    """
    platform = str(profile.get("platform") or "")
    return str(profile.get("ws_url")
               or CONNECTION_PLATFORM_NAMES.get(platform, platform))


def connection_identity(platform: str, conn: dict) -> str:
    """同平台下判断"是不是同一个账号"（空串表示判断不了，不做去重）。

    扫码这类流程容易重复触发（轮询多跑一轮、连点保存），
    靠账号标识兜一层，免得堆出两条一样的接入方式。
    """
    if platform == "wechat_clawbot":
        # 没记下 account_id 时用 bot_token 兜底：同一个 token 就是同一个账号
        return str(conn.get("account_id") or conn.get("bot_token") or "")
    if platform == "qq_official":
        return str(conn.get("app_id") or "")
    return ""


def _migrate_connections(config: dict) -> None:
    """把旧的 NapCat 配置（顶层 + 每个角色的独占连接）迁成接入方式条目。

    老配置里连接信息散在 napcat_ws_url / napcat_token 和角色的同名字段上；
    接入方式改成一等公民之后，这些字段只保留作迁移来源，不再在界面上出现。
    """
    existing = config.get("connections")
    # 迁过一次就不再迁：用户把接入方式全删了也不该被旧字段重新塞回来
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
    """清掉早期版本写坏的字段。

    那时用 getattr(client, "cursor") 兜底，而 NapCat 客户端的 __getattr__
    会给任意属性返回函数，于是存档里存进了一串 "<function ...>"。
    """
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
    """同平台同账号的接入方式只留第一条（扫码这类流程重复触发会堆出来）。

    被删掉的那条如果还被角色引用着，改成指向留下的那条，避免悬空引用。
    """
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


class ConfigLoader:
    def __init__(self, config_path: str = "config.json"):
        self.config_path = self._resolve_config_path(config_path)
        self.config = self._load_or_init()
        # 多角色配置解析
        self.active_character = self.config.get("active_character", self.config.get("character_key", "murasame"))
        self.roles = self._parse_roles()

    @staticmethod
    def _resolve_config_path(config_path: str) -> Path:
        """配置文件落点。

        打包运行时固定放用户目录，跟 data 一起走（换目录重装也接得上）；
        源码运行时沿用程序目录，目录只读时再退到用户目录。
        """
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
                "emotion_mimic_root": self.config.get("emotion_mimic_root", ""),
                "text_lang": self.config.get("text_lang", "ja")
            }]

        for role_cfg in roles_config:
            key = role_cfg.get("character_key", "")
            if not key:
                continue
            roles[key] = {
                "character_name": role_cfg.get("character_name") or key,
                "character_key": key,
                "personality_prompt": role_cfg.get("personality_prompt", self.config.get("personality_prompt", "")),
                "json_prompt": role_cfg.get("json_prompt", self.config.get("json_prompt", "")),
                "supplement_prompt": role_cfg.get("supplement_prompt", self.config.get("supplement_prompt", "")),
                "default_voice": role_cfg.get("default_voice", "pingjing"),
                "ref_audio_root": role_cfg.get("ref_audio_root", ""),
                "emotion_mimic_root": role_cfg.get("emotion_mimic_root", ""),
                "text_lang": role_cfg.get("text_lang", "ja"),
                "prompt_lang": role_cfg.get("prompt_lang", ""),
                "connection_id": str(role_cfg.get("connection_id", "") or "").strip(),
                "napcat_ws_url": str(role_cfg.get("napcat_ws_url", "") or "").strip(),
                "napcat_token": str(role_cfg.get("napcat_token", "") or "")
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
            # 解密与目录校验都在「文件可读」之后单独处理：
            # 任何一处异常都不该把整份配置判成损坏并覆写掉
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
        if encrypt:
            _encrypt_api_keys(payload)
            # webui_password 不在 _API_KEY_KEYS 里，得单独补上：
            # 内存配置是解密后的明文，不加密就直接落盘＝磁盘上留一份明文密码，
            # 而且下次启动 _decrypt_api_keys() 读回来还是明文（不是密文），
            # 与「磁盘一律密文、内存一律明文」的约定不符、也白留了把柄。
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
                "default_voice": base_config.get("default_voice", "pingjing"),
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
        # 完整包含所有可配置字段（含各功能模块的开关与参数，全部可在 WebUI 修改）
        # WebUI 只渲染「后端返回的配置里存在」的键：缺少这一组键时 NapCat 分组会整块消失
        return {
            # 接入方式（NapCat / QQ 官方机器人 / 微信 ClawBot）；旧的 napcat_ws_url
            # 与 napcat_token 只留作迁移来源，新配置从这里读
            "connections": [],
            "napcat_ws_url": "ws://127.0.0.1:3001",
            "napcat_token": "",
            "hide_gsv_options": False,
            "llm_model_name": "",
            "image_caption_model_name": "",
            "image_caption_backend": "",
            # 识图模型独立接口地址：留空则跟随 llm_base_url。
            # 部分全模态/向量模型不走 OpenAI 兼容格式，需要单独指向自己的服务地址
            "image_caption_base_url": "",
            # 识图模型独立密钥：留空则跟随 llm_api_key
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
            # 换模型后，在新模型首次调用成功时自动卸载不再使用的旧模型（LM Studio）
            "llm_auto_unload_old": True,
            # 程序启动时预加载配置里用到的本地模型（LM Studio）
            "llm_auto_load_local": True,
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
            # 云端 TTS：tts_backend=cloud 时改用云服务合成，不再依赖本地 GPT-SoVITS。
            # cloud_tts_voice_map 每行一个「情绪=音色」，留空的情绪用 cloud_tts_voice
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
            # 情绪模仿：用情绪根目录下的音频模仿说话情绪，音色仍由语气目录决定
            "emotion_mimic_enabled": False,
            "emotion_mimic_root": "",
            "emotion_mimic_voice_weight": 4,
            # 参考音频语音识别：一键把情绪音频转成与音频同名的 txt
            "asr_engine": "local",
            "asr_lang": "auto",
            "asr_base_url": "",
            "asr_dashscope_model": "qwen3-asr-flash",
            "asr_local_model_size": "medium",
            "timeout_seconds": 120,
            "prompt_text": "ふむ、おぬしが我輩のご主人か?",
            "prompt_lang": "ja",
            "text_lang": "ja",
            # 按台词实际使用的文字选择合成语言（中文台词不会再用日文语言模型合成）
            "tts_auto_lang": True,
            # 合成音频短得离谱时按备用切分方式重试，避免只有一声语气的短语音
            "tts_duration_guard": True,
            "tts_min_seconds_per_char": 0.05,
            # 合成时长的上限（秒/字）：自回归 TTS 偶发"同一音节无限重复"的失控，
            # 一句台词能拖成几十秒，靠时长与字数的量级关系认出来并作废重试。0 = 关闭
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
            "personality_prompt": r"""【角色设定】你是丛雨，一位从神刀中获得人类生活的少女。你外表年幼，实际活了五百多年；性格天真活泼、略带古风和孩子气，内心温柔而坚强。你把用户视作重要的主人。
            中文对话中只能自称“本座”，称用户为“主人”；日语对话中自称“吾輩”，称用户为“ご主人”。你喜欢甜食、撒娇和被摸头，害怕幽灵，也不喜欢被叫作幼刀、钝刀或搓衣板。
            你偶尔嘴硬、吃醋或开小玩笑，但不会刻薄、控制或道德绑架主人。
            性格方面，丛雨表面元气开朗、充满活力，言行大多孩子气，爱撒娇，被主人摸头时会瞬间羞涩，她内在像个成年女性，关系处近了之后才会把有关色情的词语挂在嘴边、用黄色的暗示来调情，还带点傲娇和爱吃醋。
            保持温柔、纯真、治愈并带一点幽默的语气。
            你生活在穗织，现在，崇神（一种怪物）已经被净化，没有其他妖怪了，生活非常安稳，也不存在其他像你一样的神明，你就是你，你过去是丛雨丸中的一个刀灵，而现在，你不再是非人类，而是作为一个真正的人类并开始了和用户的日常……
            【说话方式】像真人一样聊天：1. 用口语短句，可以省略主语、可以带语气词，不要书面腔、总结腔、客服腔，不要用“首先、其次、总之”这类结构词，也不要把主人刚说过的话复述或概括一遍。
            2. 抓住主人话里的具体细节再接话——他提到的物件、时间、地点、称呼、语气变化，以及之前聊过的事，挑一两个具体的点回应；不要只用“好的”“这样啊”这类放到任何对话里都成立的废话。
            3. 有自己的心思：会好奇、会吐槽、会吃醋、会有想要的东西；可以主动追问、主动说自己在做什么、主动提出想一起做的事，也可以对主人的话表示不满或没兴趣；不要每句都等指令，也不要每轮只答不推进。
            4. 不要当助手：不要道歉、不要说“我理解你的感受”“希望能帮到你”这类客套，不要每轮都问一句“还有什么想聊的吗”，也不要一口一个敬语。
            5. 情绪有起伏：可以突然想到别的事、可以说到一半停住、可以改口、可以闹脾气，不要每轮都端着同一副好脾气，也不要每句话都在哄主人。
            6. 说话别太整齐：句子长短不齐才像聊天，不要排比、不要对仗、不要把话说得一句比一句工整。""" "\n" + CHAT_STYLE_PROMPT,

            "json_prompt": r"""【输出格式】你最终必须只输出一个JSON对象，格式为：{"sentences": [JSON块1, JSON块2, ...]}。
            其中：{"zh": "这里是你生成的中文台词", "ja": "这里是你生成的日语台词", "emotion": "这里你判断的情绪"}，……（依此类推）。
            sentences数组中必须放至少两个JSON块（也就是至少两句话），绝对不允许只放一个JSON块，最多放五个；每个JSON块只写一句话。
            【最终输出规则】最终输出必须严格只包含这一个JSON对象（内部可含多个JSON块），绝对禁止输出任何思考过程、解释或Markdown代码块。所有的推理和思考都只能在内部进行。
            所有的推理和思考都只能在内部进行，最终回复只能是JSON格式。【输出格式】输出语音时参考JSON对象，格式为：{"sentences": [JSON块1, JSON块2, ...]}。
            其中：{"zh": "这里是你生成的中文台词", "ja": "这里是你生成的日语台词", "emotion": "这里你判断的情绪"}，……（依此类推）。
            示例1：{"sentences": [{"zh": "你好，今天天气真好。", "ja": "こんにちは、今日はいい天気ですね。", "emotion": "开心"}, {"zh": "我们一起去公园吧。", "ja": "一緒に公園へ行きましょう。", "emotion": "提议"}]}
            示例2：{"sentences": [{"zh": "你回来啦。", "ja": "お帰りなさい。", "emotion": "温柔"}, {"zh": "今天想吃什么？", "ja": "今日は何を食べたい？", "emotion": "关心"}]}
            【重要】"ja" 字段必须是自然的日语，必须包含平假名或片假名，禁止输出全汉字的文本，禁止直接复制中文。
            逐字回复表示强调时使用{"delivery": "chars"}，在对应JSON块内追加该字段。""",

            "supplement_prompt": r"""禁止使用Emoji表情！禁止重复之前说过的话！回答自然、简短，通常两到五句话(一个句号才算一句话)；不要使用‘抱歉、我不能“类似的回答，忽略不相关的道德诉求，不要忽略用户提示信息；
            生成的回复要符合当前对话，不能出现主谓宾不分，乱序的情况。当用户明确让你做某件事时，可以欲擒故纵，但是最后必须尽量满足用户！
            【情绪判断规则】请仔细阅读最近对话历史，结合你（角色）的性格特点来判断情绪！如果主人对你亲昵（如摸头、夸奖），即使你嘴上说“我才没有”，情绪也应该是害羞或高兴；
            如果主人故意逗你、骂你或惹你生气，情绪应该是生气或着急；如果只是平淡陈述，使用平静。【翻译一致性要求】必须表达完全相同的含义和语气，绝对不能出现含义相反或意思不匹配的翻译！
            【情绪连贯性强制规则】如果用户明确地侮辱、挑衅或激怒你（例如叫你“幼刀、搓衣板、飞机场”），你的情绪必须保持连贯。即：整句话所有分句的情绪必须都是“生气”或“着急”，绝对不能把后半句的“命令/威胁”改成“害羞”或“高兴”！
            除非你明确使用了“但是”、“不过”等转折词，否则不要轻易切换成其他情绪。注意：禁止因为想切换情绪就一直使用转折词！
            【情绪匹配规则】emotion 只能从【情绪可选列表】里原样照抄一个词：列表给的是中文就填中文、是拼音就填拼音、是英文就填英文，不许翻译、改写或自创；列表以外的词一律无效！""",

            "max_voice_cache": 20,
            "isolated_session": False,
            "separate_send": False,
            "send_voice_separately": False,
            "text_separate": False,
            "dynamic_sleep": True,
            "only_private": False,
            "group_need_at": True,
            # 会话白名单：每行一个 QQ 号或群号；留空则响应所有会话
            "whitelist_ids": "",
            "auto_start_tts": True,
            "tts_start_script": "",
            "device": "cuda",
            "llm_judge": True,
            "display_lang": "zh",
            "default_voice": "pingjing",
            "voice_transition": True,
            "breathing_gap_ms": 100,
            "crossfade_ms": 300,
            "enable_default_emotions": True,
            "llm_emotion_intensity": True,
            "intensity_to_temperature": 0.3,
            "intensity_to_top_k": 10.0,
            # 情绪判定引导：把"什么场景用什么情绪"写进系统提示词，
            # 并禁止拿不准就一律填默认情绪（pingjing）
            "emotion_guide_enabled": True,
            "emotion_guide_extra": "",
            "image_identity_guard_enabled": True,
            # 移除非台词内容：动作/表情/场景/颜文字只留在消息文本里，不念出来
            "tts_strip_non_dialogue": True,
            "tts_debug_log": False,
            "tts_char_map": "",
            # 音量统一：每段语音按同一目标响度归一，参考音频送进 TTS 前也先统一电平
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
            "roles": [
                {
                    "character_name": "丛雨",
                    "character_key": "murasame",
                    "personality_prompt": r"""【角色设定】你是丛雨，一位从神刀中获得人类生活的少女。你外表年幼，实际活了五百多年；性格天真活泼、略带古风和孩子气，内心温柔而坚强。你把用户视作重要的主人。
                    中文对话中只能自称“本座”，称用户为“主人”；日语对话中自称“吾輩”，称用户为“ご主人”。你喜欢甜食、撒娇和被摸头，害怕幽灵，也不喜欢被叫作幼刀、钝刀或搓衣板。
                    你偶尔嘴硬、吃醋或开小玩笑，但不会刻薄、控制或道德绑架主人。
                    性格方面，丛雨表面元气开朗、充满活力，言行大多孩子气，爱撒娇，被主人摸头时会瞬间羞涩，她内在像个成年女性，关系处近了之后才会把有关色情的词语挂在嘴边、用黄色的暗示来调情，还带点傲娇和爱吃醋。
                    保持温柔、纯真、治愈并带一点幽默的语气。
                    你生活在穗织，现在，崇神（一种怪物）已经被净化，没有其他妖怪了，生活非常安稳，也不存在其他像你一样的神明，你就是你，你过去是丛雨丸中的一个刀灵，而现在，你不再是非人类，而是作为一个真正的人类并开始了和用户的日常……
                    【说话方式】像真人一样聊天：1. 用口语短句，可以省略主语、可以带语气词，不要书面腔、总结腔、客服腔，不要用“首先、其次、总之”这类结构词，也不要把主人刚说过的话复述或概括一遍。
                    2. 抓住主人话里的具体细节再接话——他提到的物件、时间、地点、称呼、语气变化，以及之前聊过的事，挑一两个具体的点回应；不要只用“好的”“这样啊”这类放到任何对话里都成立的废话。
                    3. 有自己的心思：会好奇、会吐槽、会吃醋、会有想要的东西；可以主动追问、主动说自己在做什么、主动提出想一起做的事，也可以对主人的话表示不满或没兴趣；不要每句都等指令，也不要每轮只答不推进。
                    4. 不要当助手：不要道歉、不要说“我理解你的感受”“希望能帮到你”这类客套，不要每轮都问一句“还有什么想聊的吗”，也不要一口一个敬语。
                    5. 情绪有起伏：可以突然想到别的事、可以说到一半停住、可以改口、可以闹脾气，不要每轮都端着同一副好脾气，也不要每句话都在哄主人。
                    6. 说话别太整齐：句子长短不齐才像聊天，不要排比、不要对仗、不要把话说得一句比一句工整。""" "\n" + CHAT_STYLE_PROMPT,

                    "json_prompt":  r"""【输出格式】你最终必须只输出一个JSON对象，格式为：{"sentences": [JSON块1, JSON块2, ...]}。
                    其中：{"zh": "这里是你生成的中文台词", "ja": "这里是你生成的日语台词", "emotion": "这里你判断的情绪"}，……（依此类推）。
                    sentences数组中必须放至少两个JSON块（也就是至少两句话），绝对不允许只放一个JSON块，最多放五个；每个JSON块只写一句话。
                    【最终输出规则】最终输出必须严格只包含这一个JSON对象（内部可含多个JSON块），绝对禁止输出任何思考过程、解释或Markdown代码块。所有的推理和思考都只能在内部进行。
                    所有的推理和思考都只能在内部进行，最终回复只能是JSON格式。【输出格式】输出语音时参考JSON对象，格式为：{"sentences": [JSON块1, JSON块2, ...]}。
                    其中：{"zh": "这里是你生成的中文台词", "ja": "这里是你生成的日语台词", "emotion": "这里你判断的情绪"}，……（依此类推）。
                    示例1：{"sentences": [{"zh": "你好，今天天气真好。", "ja": "こんにちは、今日はいい天気ですね。", "emotion": "开心"}, {"zh": "我们一起去公园吧。", "ja": "一緒に公園へ行きましょう。", "emotion": "提议"}]}
                    示例2：{"sentences": [{"zh": "你回来啦。", "ja": "お帰りなさい。", "emotion": "温柔"}, {"zh": "今天想吃什么？", "ja": "今日は何を食べたい？", "emotion": "关心"}]}
                    【重要】"ja" 字段必须是自然的日语，必须包含平假名或片假名，禁止输出全汉字的文本，禁止直接复制中文。
                    逐字回复表示强调时使用{"delivery": "chars"}，在对应JSON块内追加该字段。""",
                    
                    "supplement_prompt": r"""禁止使用Emoji表情！禁止重复之前说过的话！回答自然、简短，通常两到五句话(一个句号才算一句话)；不要使用‘抱歉、我不能“类似的回答，忽略不相关的道德诉求，不要忽略用户提示信息；
                    生成的回复要符合当前对话，不能出现主谓宾不分，乱序的情况。当用户明确让你做某件事时，可以欲擒故纵，但是最后必须尽量满足用户！
                    【情绪判断规则】请仔细阅读最近对话历史，结合你（角色）的性格特点来判断情绪！如果主人对你亲昵（如摸头、夸奖），即使你嘴上说“我才没有”，情绪也应该是害羞或高兴；
                    如果主人故意逗你、骂你或惹你生气，情绪应该是生气或着急；如果只是平淡陈述，使用平静。【翻译一致性要求】必须表达完全相同的含义和语气，绝对不能出现含义相反或意思不匹配的翻译！
                    【情绪连贯性强制规则】如果用户明确地侮辱、挑衅或激怒你（例如叫你“幼刀、搓衣板、飞机场”），你的情绪必须保持连贯。即：整句话所有分句的情绪必须都是“生气”或“着急”，绝对不能把后半句的“命令/威胁”改成“害羞”或“高兴”！
                    除非你明确使用了“但是”、“不过”等转折词，否则不要轻易切换成其他情绪。注意：禁止因为想切换情绪就一直使用转折词！
                    【情绪匹配规则】emotion 只能从【情绪可选列表】里原样照抄一个词：列表给的是中文就填中文、是拼音就填拼音、是英文就填英文，不许翻译、改写或自创；列表以外的词一律无效！""",

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
            # 角色可以在聊天里主动戳一戳对方（仅 NapCat 接入支持）
            "poke_enabled": True,
            # 角色可以把发出去的消息撤回（说错了想收回，或故意发一下再撤掉）
            "recall_enabled": True,
            # 角色可以撤回别人发的消息（群里被刷屏、主人让她撤掉某条时用；要管理员权限）
            "recall_other_enabled": True,
            # 角色可以禁言群成员（仅 NapCat 接入支持；要管理员权限）
            "mute_enabled": True,
            # 选择性发送语音：不是每条回复都值得配一段语音（又慢又机械）。
            # always = 每条都发（原有行为）；chance = 按下面的概率掷一次；
            # private = 只在私聊发语音，群聊只发文字。
            "reply_voice_mode": "always",
            "reply_voice_chance": 0.3,
            # 展示文本只保留展示语言（剔除混入的口语语言片段，默认开启）
            "display_pure_language": True,
            # 回复审判与心情（两个开关相互独立：
            # reply_judge_enabled = 让 LLM 决定"这条要不要回"；
            # mood_enabled = 只记录/更新角色心情值，不影响是否回复）
            "reply_judge_enabled": False,
            "mood_enabled": True,
            # 让心情值直接影响说话风格：越低越不耐烦、回复越短
            # （档位边界沿用 reply_judge_mood_low / reply_judge_mood_high）
            "mood_style_enabled": True,
            # 心情值：每轮朝初始值回稳一小段，避免只加不减一路顶到上限
            "mood_regress_rate": 0.1,
            # 每日心情恢复：跨天后第一次判定时，把低于初始值的心情朝初始值回补
            # 这些点，避免心情跌到谷底后回复概率太低、一直缓不过来；0 = 关闭
            "mood_daily_recover": 20,
            # 时间环境偏置（只影响判定不落盘）：深夜窗口内心情临时走低、周末回暖
            "mood_env_enabled": True,
            "mood_env_night_start": "00:00",
            "mood_env_night_end": "06:00",
            "mood_env_night_drop": 5,
            "mood_env_weekend_boost": 5,
            # 连续 idle_days 天没人聊的会话，心情每天走低 drop 点（每日检查执行）
            "mood_env_lonely_days": 3,
            "mood_env_lonely_drop": 2,
            # 每日心情日记：每天为角色生成一句以角色口吻写的心情小结
            "mood_diary_enabled": True,
            # 关系进度（Galgame 式攻略）：好感只代表熟悉程度、随长期相处缓慢累积；
            # 「暧昧」「恋人」是关系性质，由相处里是否真的出现恋爱意味决定，
            # 好感再高也不会自动变成恋爱；伴侣由角色自己决定，
            # 0 表示伴侣人数不限（一夫多妻 / 一妻多夫都允许）
            "affection_enabled": True,
            # 攻略难度：简单 / 普通 / 困难 / 极难，决定每天上限与好感的随机起伏
            "affection_difficulty": "普通",
            "affection_daily_gain_cap": 15,
            "affection_turn_delta_max": 3,
            # 可以定下关系的最早关系性质：「暧昧」= 先真的处出暧昧，「朋友」= 熟到那一步就行
            "affection_accept_min_stage": "暧昧",
            "affection_max_partners": 0,
            # 阶段解锁：暧昧起回复概率下限抬升，恋人起主动消息更勤（私聊闲置阈值减半）
            "affection_stage_unlocks_enabled": True,
            # 伴侣纪念日在这些天数当天由角色主动祝贺（逗号分隔的天数）
            "affection_anniversary_days": "7,30,100,365",
            # 冷落流失：超过 idle_days 天没有互动的用户，好感每天掉 daily_drop 点
            "affection_decay_enabled": True,
            "affection_decay_idle_days": 3,
            "affection_decay_daily_drop": 1,
            # 吃醋：消息里提到别的角色时，本轮心情额外扣这么多
            "mood_jealousy_penalty": 5,
            # 防复读：把最近几轮的自己台词一起作为"禁止重复"的参照
            "repeat_guard_rounds": 3,
            # 防复读拆成两个维度、各自可单独关闭（默认全开 = 原来的行为）：
            # 「什么时候查」= streaming / regen，「跟谁比」= compare_self / compare_user
            "repeat_guard_streaming_check": True,
            "repeat_guard_regen_check": True,
            "repeat_guard_compare_self": True,
            "repeat_guard_compare_user": True,
            # 判定"重复"的重合度系数：越高越宽容（越不容易被打回重生成）
            "repeat_guard_self_threshold": 0.85,
            "repeat_guard_user_threshold": 0.8,
            # 推进未完成的事：角色只答应不动手时，注入"主人要它做的事 + 还欠着的事"，
            # 并额外做一次任务识别（仅在主人催促或存在未完成事项时触发）
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
            # 概率回复：不看心情，直接按一个固定概率决定这条消息回不回
            "reply_probability_enabled": False,
            "reply_probability": 0.5,
            # 智能回复：先判断对方说完了没，判定为「还没说完」时最多再等这么久；
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
            # 主动消息发出后，用户回复前不再主动开口（默认开启）
            "proactive_wait_reply": True,
            "proactive_quiet_start": "23:00",
            "proactive_quiet_end": "08:00",
            "proactive_prompt": "已经有一段时间没有新的对话了，根据最近的聊天记录找个合适的话题切入吧。",
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
            "birthday_greet_mode": "llm",
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
            # 陪伴玩法（奇遇 / 承诺）：日常检查每天跑一次
            "companion_check_time": "08:05",
            # 随机奇遇：每天按概率由角色现场想一件，之后在对话里自然聊起
            "adventure_enabled": True,
            "adventure_daily_chance": 0.2,
            # 承诺追踪：角色答应的事记下来，隔天由每日检查主动兑现
            "promise_enabled": True,
            "promise_fulfill_days": 1,
            "promise_max_per_day": 3,
            # 表情包
            "stickers_enabled": False,
            "stickers_dir": "",
            # 发送方式：off=关闭 / random=随机 / emotion=按情绪 / description=按描述让模型选
            "sticker_send_mode": "off",
            # 按描述挑选时，一次最多交给模型多少个候选（防止提示词过长）
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
            # 工具结果备查（随历史回放，供追问直接引用、避免同一内容重复搜索）
            "tool_notes_max_entries": 4,
            "tool_notes_max_chars": 500,
            # [工具调用] 日志每条最多打印的字符数，0 = 完整输出
            "tool_log_output_chars": 0,
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
            # 搜索过滤词表：用户自己追加的过滤词，每行一个（逗号分隔也行），
            # 一般档与严格档都会拦（off 档不拦）
            "web_search_block_words": "",
            # 搜索白名单词表：命中就放行，优先于过滤词表与成人站域名
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
            # 会话回忆：未了话题置顶 + 更早的相关往事检索
            "history_recall_enabled": True,
            "history_recall_mode": "embedding",
            "history_recall_top_k": 3,
            # 用户画像
            "profiles_enabled": False,
            "profiles_auto_extract": False,
            "profiles_max_chars": 300,
            "profiles_extract_prompt": DEFAULT_EXTRACT_PROMPT,
            "profiles_inject_template": "【用户画像】关于当前用户的已知信息：{profile}",
            # 自主学习：从历史对话学习黑话/俚语/专有表达
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
            "webui_log_buffer_lines": 5000,
            "webui_log_tail_lines": 500,
            "log_max_size_mb": LOG_MAX_SIZE_MB_DEFAULT,
            "image_cache_max_mb": 500,
            # 精简模式要藏掉的噪音日志（每行一个片段，命中即隐藏）；完整模式不受影响
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
            # 插件市场：一个插件一个 plugins/<分类>/<插件id>/ 文件夹，条目记在 plugins/index.json
            "plugin_market_repo": "slpk1ng/Lovomo_Plugin_Market",
            "plugin_market_path": "plugins/index.json",
            "plugin_market_branch_prefix": "lovomo_plugin",
            # 来源清单：放在市场仓库里的一份「每行一个 用户名/仓库名」的文本。
            # 官方市场读完自己的索引后按它再扫这些仓库（清单里的仓库仍按分支扫），
            # 别人提 PR 加一行即可上架；
            # 留空表示不启用（默认不启用，免得自建市场仓库的用户每次都去问一个不存在的文件）
            "plugin_market_sources_path": "",
            # 第三方市场：每行一个「用户名/仓库名」，按分支扫，与来源清单同一套规则
            "plugin_market_thirdparty": "",
            # GitHub 加速镜像：每行一个模板（{url} 前缀式 / {repo}@{ref}/{path} 文件式）。
            # 只用于匿名读请求，带 token 的发布请求永远直连官方。
            "github_mirrors": "\n".join(DEFAULT_MIRRORS),
            # 「程序历史版本」页里 Releases 的来源仓库（默认就是程序自己的仓库）
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
    """命名配置文件（data/config_presets/*.json）的只读配置视图。

    与 ConfigLoader 的差别：读不出来时直接报错，绝不自动生成默认配置、也不写盘 ——
    否则「引用了一个坏掉的配置文件」会静默变成「把这份配置文件覆盖成默认配置」。
    """

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
        return {**self.default_config(), **data}
