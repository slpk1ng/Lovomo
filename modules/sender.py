"""统一消息发送器：文本/语音/表情包发送，供主消息管线与定时、主动消息共用。"""
import asyncio
import contextlib
import contextvars
import json
import random
import re
import time
from pathlib import Path
from typing import List, Optional

from .llm_helpers import (MENTION_PLACEHOLDER, RoleContext, segment_for_tts, lang_text_broken,
                          translate_to_lang, available_mimics, apply_literal_mention,
                          DELIVERY_CHARS, DELIVERY_MODES)
from .tts import synthesize_sentence, merge_wavs, get_audio_duration
from .tts_service import check_tts_service
from .stickers import resize_for_output
from .adapters import client_supports, client_reply_limit


# 本次发送使用的 NapCat 连接（多账号时按角色临时切换；未设置则用默认连接）
_CURRENT_CLIENT = contextvars.ContextVar("lovomo_send_client", default=None)

# 语音合成一次只服务一条消息：主动消息/问候/提醒可能同时开火，一起挤进 TTS
# 会把服务占满、大部分请求超时后降级成纯文本。这里按事件循环排队，
# 让每条消息都能等到自己的语音（同一条消息内部仍可并发合成多句）。
_VOICE_LOCKS: dict = {}

# 只能发文字的接入方式（微信 ClawBot / QQ 官方）两条消息之间的最小间隔与随机抖动。
# 一条回复按句发好几条，句间隔只有几百毫秒——端上看到的正常节奏是 1.5 秒上下一条。
TEXT_ONLY_SEND_MIN_GAP = 1.5
TEXT_ONLY_SEND_GAP_JITTER = 1.0

# 撤回：模型要求把这条回复发出去之后再撤掉（说错了想收回，或故意让主人看一眼）。
# 延迟夹在上下限之间，免得模型填出「等一小时」这种离谱的秒数。
RECALL_MIN_DELAY_SECONDS = 1.0
RECALL_MAX_DELAY_SECONDS = 60.0
DEFAULT_RECALL_DELAY_SECONDS = 3.0
# 主人要求撤回的那条不是她发的消息：系统撤不了，这时也不能让她把刚发的这条撤掉，
# 否则撤掉的是她自己的话，看着像撤错了对象
RECALL_DENY = "deny"
# 撤回别人的消息要靠管理员权限：QQ 只有管理员和群主能删别人的消息
GROUP_ADMIN_ROLES = ("owner", "admin")

# 禁言时长：模型没填就用默认值，超出范围夹到上下限（QQ 单次最长 30 天）
DEFAULT_MUTE_SECONDS = 600
MUTE_MIN_SECONDS = 60
MUTE_MAX_SECONDS = 2592000

# 主人说「撤回刚才那条」时要撤的是已经发出去的消息：每个会话留最近几条的 id 备查，
# 超时或超过条数的丢掉，不落盘（重启后本来也无从撤回）。
# 条数要留够：主人引用一条几分钟前的消息让撤回时，得能在那条 id 上找到同一句话的
# 其它消息，只留十条的话早就被挤出去了。
RECENT_SENT_MAX = 200
RECENT_SENT_TTL_SECONDS = 600.0

# 逐字发送的条数与节奏：模型要求「一个字一个字说话」时才会走到这条路，
# 拆得太碎会把会话刷屏（条数封顶），每条之间留一点打字间隔才像真人。
DELIVERY_MAX_MESSAGES = 20
DELIVERY_CHAR_GAP_SECONDS = 0.2


def _action_receipt(action: str, ok: bool, **extra) -> dict:
    """动作执行回执。

    动作是"模型声明、发送层执行"的：只有回执能说清它到底做了没有。
    失败也要带原因（unsupported / no_client / error / …），
    上层据此判定，并把结果回灌给模型，而不是让它以为做过。
    """
    receipt = {"action": action, "ok": bool(ok), "at": time.time()}
    receipt.update({key: value for key, value in extra.items() if value is not None})
    return receipt


def sent_action_receipts(quote_used: bool, at_ids=None) -> List[dict]:
    """引用与@人是"挂在这条消息上"的动作：消息真的发出去了才算做到。

    这两种动作没有单独的执行入口（不像禁言、撤回那样先调一次接口），
    只有补上回执，"她有没有真做"的判定才不会把做过的当成只在嘴上答应。
    """
    receipts = []
    if quote_used:
        receipts.append(_action_receipt("引用", True))
    ids = [str(q).strip() for q in (at_ids or []) if str(q).strip()]
    if ids:
        receipts.append(_action_receipt("@人", True,
                                        target=ids[0] if len(ids) == 1 else None))
    return receipts


def _char_pieces(text) -> List[str]:
    """逐字发送：把台词拆成单字，空白丢掉。

    标点必须保留——「！」「？」「@」都是角色有意写出来的，丢掉会让句子读起来变味
    （以前的判据按「纯标点」整字过滤，单个 @ 或单个感叹号会凭空消失）。
    @ 占位符不是台词，要整块留着交给发送层换成真正的@，不能拆成「a」「t」发出去。
    """
    rest = str(text or "")
    pieces: List[str] = []
    while rest:
        idx = rest.find(MENTION_PLACEHOLDER)
        if idx < 0:
            pieces.extend(ch for ch in rest if ch.strip())
            break
        pieces.extend(ch for ch in rest[:idx] if ch.strip())
        pieces.append(MENTION_PLACEHOLDER)
        rest = rest[idx + len(MENTION_PLACEHOLDER):]
    return pieces

# 选择性发送语音：不是每条回复都值得配一段语音（又慢又机械），
# 由「语音发送方式」决定——总是发 / 按概率 / 只在私聊发。
VOICE_MODE_ALWAYS = "always"
VOICE_MODE_CHANCE = "chance"
VOICE_MODE_PRIVATE = "private"
VOICE_MODES = (VOICE_MODE_ALWAYS, VOICE_MODE_CHANCE, VOICE_MODE_PRIVATE)
DEFAULT_VOICE_CHANCE = 0.3


def voice_enabled_for(config, session_type: str) -> bool:
    """这条回复要不要带语音。

    always：每条都发（默认，保持原有行为）；chance：按 reply_voice_chance 掷一次；
    private：只在私聊发语音，群聊只发文字。整条回复只该调用一次（概率模式按条掷）。
    """
    mode = str((config or {}).get("reply_voice_mode") or VOICE_MODE_ALWAYS).strip().lower()
    if mode not in VOICE_MODES:
        mode = VOICE_MODE_ALWAYS
    if mode == VOICE_MODE_PRIVATE:
        return str(session_type or "") == "private"
    if mode == VOICE_MODE_CHANCE:
        try:
            chance = float((config or {}).get("reply_voice_chance", DEFAULT_VOICE_CHANCE))
        except (TypeError, ValueError):
            chance = DEFAULT_VOICE_CHANCE
        return random.random() < max(0.0, min(1.0, chance))
    return True


def voice_lock() -> asyncio.Lock:
    """当前事件循环上的语音合成排队锁（不同循环各有一把，测试里反复起循环也安全）。"""
    loop = asyncio.get_running_loop()
    lock = _VOICE_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _VOICE_LOCKS[loop] = lock
    return lock


def _normalize_target(session_type: str, target_id) -> tuple:
    """兼容传入完整会话ID的情形：private_10001 / group_456 / group_456_789
    （待办提醒等模块保存的是 session_id，而发送需要纯数字号码）。"""
    s = str(target_id)
    if s.startswith("private_"):
        return "private", s.split("_", 1)[1]
    if s.startswith("group_"):
        return "group", s.split("_", 1)[1].split("_")[0]
    return session_type, target_id


def _as_target(target_id):
    """NapCat（OneBot）要数字号码，微信 ClawBot / QQ 官方给的是字符串 ID。

    数字就转成 int（保持原有行为），否则原样传下去。
    """
    s = str(target_id)
    return int(s) if s.lstrip("-").isdigit() else s


def _extract_message_id(result) -> str:
    """从发送接口的返回值里取刚发出去那条消息的 id（各接入方式字段名不同）。

    撤回要用它，取不到就当这条消息没法撤回。
    """
    if isinstance(result, dict):
        for key in ("message_id", "msg_id", "id"):
            value = result.get(key)
            if value not in (None, "", 0):
                return str(value)
    return ""


def recall_delay_of(sentence) -> float:
    """这次撤回等多少秒：模型没填就用默认值，超出范围夹到上下限。"""
    raw = sentence.get("recall_delay") if isinstance(sentence, dict) else None
    try:
        delay = float(raw)
    except (TypeError, ValueError):
        delay = DEFAULT_RECALL_DELAY_SECONDS
    return max(RECALL_MIN_DELAY_SECONDS, min(RECALL_MAX_DELAY_SECONDS, delay))


def mute_seconds_of(sentence) -> int:
    """这次禁言多少秒：模型没填就用默认值，超出范围夹到上下限。"""
    raw = sentence.get("mute_duration") if isinstance(sentence, dict) else None
    try:
        seconds = int(float(raw))
    except (TypeError, ValueError):
        seconds = DEFAULT_MUTE_SECONDS
    return max(MUTE_MIN_SECONDS, min(MUTE_MAX_SECONDS, seconds))


def mute_target_of(sentence, allowed_ids, speaker_id) -> str:
    """禁言谁：模型写明的 QQ 号优先（必须在本轮出现的号码里），否则取名单里第一个。

    名单由上层按「@到的人 → 被引用的人 → 当前发言者」排好，也就是这一轮针对的那个人。
    模型编一个号码出来就把无辜群成员禁言了，代价太大，所以号码一律要核对。
    """
    allowed = [str(q).strip() for q in (allowed_ids or []) if str(q).strip()]
    raw = sentence.get("mute") if isinstance(sentence, dict) else None
    picked = str(raw).strip() if isinstance(raw, (str, int)) and not isinstance(raw, bool) else ""
    if picked:
        # 写明了号码就只认它：号码不在本轮出现过的名单里说明是模型编的，
        # 宁可这次禁言失败，也不能拿别人顶上
        return picked if picked in allowed else ""
    if allowed:
        return allowed[0]
    # 名单是空的说明这一轮根本没指名道姓：宁可这次禁不了（回执会写「没听清要禁言谁」），
    # 也不能拿当前发言者顶上——主人说「禁言他」时那个人不是他自己
    return ""


async def group_member_role(client, group_id, user_id) -> str:
    """查群成员角色（owner / admin / member）；查不到返回空串。

    撤回别人的消息要靠它判权限。只发文字的接入方式没有这个接口，取不到就当没权限。
    """
    getter = getattr(client, "get_group_member_info", None)
    if getter is None or not group_id or not user_id:
        return ""
    try:
        resp = await getter(group_id=_as_target(group_id), user_id=_as_target(user_id))
    except Exception as e:
        print(f"查群成员角色失败 ({group_id}/{user_id}): {type(e).__name__}: {e}")
        return ""
    # NapCat 的 call_action 已经把响应里的 data 取出来了，拿到的就是成员信息本身；
    # 这里兼容仍然带 {"data": {...}} 外层的情况
    if isinstance(resp, dict) and isinstance(resp.get("data"), dict):
        resp = resp["data"]
    if not isinstance(resp, dict):
        return ""
    return str(resp.get("role") or "")


async def bot_can_manage(client, group_id) -> bool:
    """机器人自己在群里是不是管理员或群主；查不到角色就当不是。"""
    if not group_id:
        return False
    role = await group_member_role(client, group_id, getattr(client, "self_id", ""))
    return role.lower() in GROUP_ADMIN_ROLES


async def can_recall_others(client, group_id, requester_id, requester_role="") -> bool:
    """请求撤回的人与机器人自己都是管理员或群主时，才允许撤别人的消息。"""
    if not group_id or not requester_id:
        return False
    role = str(requester_role or "").lower()
    if role not in GROUP_ADMIN_ROLES:
        role = (await group_member_role(client, group_id, requester_id)).lower()
    if role not in GROUP_ADMIN_ROLES:
        return False
    return await bot_can_manage(client, group_id)


async def can_manage_group(client, group_id, requester_id, requester_role="") -> bool:
    """能不能执行群管理类动作（撤回别人的消息 / 禁言别人）。

    判据与撤回别人的消息完全一致：提出要求的人与机器人自己都得是管理员或群主。
    机器人是管理员但提要求的是普通群成员时，等于谁都能借她的号去禁言别人。
    """
    return await can_recall_others(client, group_id, requester_id, requester_role)


async def mute_denied_reason(client, group_id, requester_id, requester_role,
                             target_id) -> str:
    """不能禁言时的原因（空串表示可以）。

    机器人自己是管理员或群主是前提。禁言别人时还要求提出要求的人也是管理员或群主
    （否则普通群成员能借她的号去禁言别人）；禁言**他自己**时谁提都算，
    他自己要求把自己禁言谈不上越权。群主不能被禁言——这是平台限制，
    硬发过去只会拿到 cannot ban owner，不如直接说清做不到。
    """
    if not await bot_can_manage(client, group_id):
        return "denied"
    target = str(target_id or "").strip()
    if (await group_member_role(client, group_id, target)).lower() == "owner":
        return "target_owner"
    requester = str(requester_id or "").strip()
    if target and target == requester:
        return ""
    if await can_manage_group(client, group_id, requester_id, requester_role):
        return ""
    return "denied"


async def can_mute(client, group_id, requester_id, requester_role, target_id) -> bool:
    """能不能禁言 target_id；具体原因见 mute_denied_reason。"""
    return not await mute_denied_reason(client, group_id, requester_id,
                                        requester_role, target_id)


_VOICE_GAP_LEAD = 0.5     # 语音播完后额外留的间隔
_VOICE_GAP_FIXED = 0.2    # 关闭动态等待时每条语音之间的固定间隔


class VoicePacer:
    """语音发送节奏：只在「下一条语音要发之前」等上一条播完。

    历史实现是"发完一条就 sleep(音频时长)"，连最后一条也照睡。这段等待发生在
    会话锁里，于是排队中的下一条用户消息要等上一条语音播完才送进 LLM，
    表现为"回复延迟恰好等于上一条语音的长度"。改成发下一条前才等之后：
    句间节奏完全不变，收尾那次等待被去掉，发送时长一律取自刚发出去的那条语音。
    """

    def __init__(self, dynamic: bool = True, gap: float = _VOICE_GAP_LEAD,
                 fixed_gap: float = _VOICE_GAP_FIXED):
        self.dynamic = bool(dynamic)
        self.gap = gap
        self.fixed_gap = fixed_gap
        self._until = 0.0

    async def wait(self):
        """发下一条之前调用：上一条还在播就在这里等到播完。"""
        delay = self._until - time.monotonic()
        self._until = 0.0
        if delay > 0:
            await asyncio.sleep(delay)

    async def hold_voice(self, path) -> float:
        """登记刚发出去的这条语音占用的播放时间（时长取自这条语音本身）。"""
        if self.dynamic and path is not None:
            seconds = await asyncio.to_thread(get_audio_duration, str(path))
            span = float(seconds or 0.0) + self.gap
        else:
            span = self.fixed_gap
        self._until = max(self._until, time.monotonic() + span)
        return span


class MessageSender:
    """封装 NapCat 客户端发送行为。client 由主程序注入（连接后设置）。

    多账号时（每个角色一条 NapCat 连接）由 using_client() 指定本次发送用哪条
    连接：它往 contextvars 里写，所以并发的消息任务互不干扰。
    """

    def __init__(self, config, memory_manager, sticker_manager=None, stats=None):
        self.config = config
        self.memory_manager = memory_manager
        self.sticker_manager = sticker_manager
        # 最近一次发出的表情包（键=会话，值=路径）：聊天记录把真实图片记进历史用，
        # 主流程取用后置空。只保留最后一条，避免跨会话误记。
        self.last_sent_sticker = None
        self.stats = stats
        self.client = None  # NapCatClient，主循环连接后注入
        self.role_clients = {}  # 角色标识符 -> 该角色独立的 NapCatClient
        # 会话 -> 收到该会话消息的那条连接：主动消息/提醒要知道该往哪条接入方式发
        self.session_clients = {}
        # 接入方式 id -> 客户端：重启后靠它把上次记下的"会话属于哪条接入方式"接回来
        self.channel_clients = {}
        self._session_channels = {}
        self._session_channels_loaded = False
        # 只能发文字的接入方式：每条会话上次发出去的时刻，用来拉开两条消息的间隔
        self._text_only_last_sent = {}
        # 最近一次戳一戳的回执（失败带原因），供上层判断动作到底做了没有
        self.last_poke_receipt: Optional[dict] = None
        # 刚发出去那条消息的 id（撤回要用，发送后立刻取，别的协程插不进来）
        self.last_message_id = ""
        # 挂在后台等待撤回的任务，留着引用免得被回收
        self._recall_tasks = set()
        # 会话 -> 最近发出去的消息 [(时刻, id, 组号)]，主人说「撤回刚才那条」时按它找
        self._recent_sent = {}
        # 同一句话拆成多条消息（文字 + 语音）时共用一个组号：撤回要整组一起撤，
        # 只撤掉其中一条会剩下半截。没指定组号的消息各自成组。
        self._sent_group = 0
        # 最近一次发送是不是"超时、结果未知"：超时只是没等到发送回执，消息很可能
        # 已经发出去了。要不要按这个结果重发的调用方（心情日记）看这个标记。
        self.last_send_uncertain = False
        # 本次发送只发一次（不做超时重试），由 send_once() 打开
        self._send_once = False

    # ---- 会话属于哪条接入方式（要跨重启记住，否则主动消息会落到默认连接）----
    def _session_channels_path(self) -> Optional[Path]:
        data_path = getattr(self.memory_manager, "data_path", None)
        return Path(data_path) / "session_channels.json" if data_path else None

    def _load_session_channels(self) -> None:
        if self._session_channels_loaded:
            return
        self._session_channels_loaded = True
        path = self._session_channels_path()
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"会话接入方式映射读取失败（按空处理）: {type(e).__name__}: {e}")
            return
        if isinstance(data, dict):
            self._session_channels = {str(k): str(v) for k, v in data.items() if v}

    def _save_session_channels(self) -> None:
        path = self._session_channels_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._session_channels, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        except OSError as e:
            print(f"会话接入方式映射保存失败（忽略）: {type(e).__name__}: {e}")

    def drop_channel_sessions(self, channel_key: str) -> List[str]:
        """摘掉某条接入方式负责的全部会话映射，返回这些会话 ID。

        接入方式被删掉后这些映射永远发不出去，留着只会让聊天记录页
        继续显示一个已经不存在的来源。
        """
        key = str(channel_key or "").strip()
        if not key:
            return []
        self._load_session_channels()
        dropped = [sid for sid, value in self._session_channels.items() if value == key]
        if not dropped:
            return []
        for sid in dropped:
            self._session_channels.pop(sid, None)
        self._save_session_channels()
        return dropped

    def set_channel_client(self, channel_key: str, client) -> None:
        """登记/注销"接入方式 id → 客户端"，连接建立与断开时各调一次。"""
        key = str(channel_key or "").strip()
        if not key:
            return
        if client is None:
            self.channel_clients.pop(key, None)
        else:
            self.channel_clients[key] = client

    def channel_key_of(self, client) -> str:
        """这条客户端对应的接入方式 id：优先问连接自己，其次按登记表反查。"""
        if client is None:
            return ""
        conn = getattr(client, "connection", None)
        if isinstance(conn, dict) and str(conn.get("id") or "").strip():
            return str(conn["id"]).strip()
        for key, value in self.channel_clients.items():
            if value is client:
                return key
        return ""

    def remember_session(self, session_id: str, client) -> None:
        """记下这条会话是从哪条接入方式来的（落盘，重启后仍然算数）。"""
        sid = str(session_id or "")
        if not sid or client is None:
            return
        self.session_clients[sid] = client
        key = self.channel_key_of(client)
        if key and self._session_channels.get(sid) != key:
            self._load_session_channels()
            self._session_channels[sid] = key
            self._save_session_channels()

    # 会话 ID 里带这些标记的目标一定来自微信 ClawBot：私聊是 openid（…@im.wechat），
    # 群聊是 …@chatroom，机器人自己是 …@im.bot。QQ 号是纯数字，看不出是哪条 QQ 连接。
    # 带下划线的写法是记忆文件名净化过的（@ 与 . 都变成 _），两种都要认。
    _WECHAT_TARGET_MARKERS = ("@im.wechat", "@chatroom", "@im.bot",
                              "_im_wechat", "_chatroom", "_im_bot")

    @classmethod
    def platform_of_session(cls, session_id: str) -> str:
        """按会话 ID 判断它属于哪类接入方式（判断不了返回空串）。"""
        raw = str(session_id or "")
        target = raw.split("_", 1)[1] if raw.startswith(("private_", "group_")) else raw
        if any(marker in target for marker in cls._WECHAT_TARGET_MARKERS):
            return "wechat_clawbot"
        return ""

    def _client_for_platform(self, platform: str):
        """已登记的接入方式里挑一条该平台的连接（多条时按 id 取第一条，行为稳定）。"""
        if not platform:
            return None
        matches = sorted((key, client) for key, client in self.channel_clients.items()
                         if str(getattr(type(client), "platform", "") or "") == platform)
        return matches[0][1] if matches else None

    def session_channel_key(self, session_id: str) -> str:
        """这条会话走的接入方式 id（没有记录返回空串）。"""
        sid = str(session_id or "")
        if not sid:
            return ""
        client = self.session_clients.get(sid)
        if client is not None:
            key = self.channel_key_of(client)
            if key:
                return key
        self._load_session_channels()
        return str(self._session_channels.get(sid, "") or "")

    def session_client(self, session_id: str):
        """这条会话该走哪条连接（找不到返回 None，由调用方决定回落）。

        不这么做的话，微信会话的提醒会走默认的 NapCat，报「无法获取用户信息」。
        重启后内存里没有映射，就按落盘的"会话 → 接入方式 id"找回来；连映射都没有
        （老会话、换过接入方式）时按目标格式判断平台再挑连接 —— 只有默认连接可用
        时才回落，否则微信会话的提醒/日记照样发不出去。
        """
        sid = str(session_id or "")
        client = self.session_clients.get(sid)
        if client is None:
            self._load_session_channels()
            key = self._session_channels.get(sid, "")
            client = self.channel_clients.get(key) if key else None
        if client is None:
            platform = self.platform_of_session(sid)
            client = self._client_for_platform(platform)
            if client is not None:
                print(f"会话 {sid} 没有接入方式记录，按 ID 判断走「{platform}」这条连接。")
                self.remember_session(sid, client)
        return client

    @contextlib.contextmanager
    def for_session(self, session_id: str):
        """按会话自动选连接：消息从哪条接入方式来，主动消息就往哪条发。"""
        with self.using_client(self.session_client(session_id)):
            yield

    # ---------------- 连接选择 ----------------
    def set_role_client(self, role_key: str, client) -> None:
        """登记/注销某个角色独占的 NapCat 连接。"""
        key = str(role_key or "").strip()
        if not key:
            return
        if client is None:
            self.role_clients.pop(key, None)
        else:
            self.role_clients[key] = client

    def client_for(self, role) -> object:
        """取某个角色发送时该用的连接：绑了接入方式就用它，否则用默认连接。"""
        role = role or {}
        key = str(role.get("character_key", "") or "").strip()
        if key and str(role.get("connection_id") or "").strip():
            client = self.role_clients.get(key)
            if client is not None:
                return client
        return self.client

    @contextlib.contextmanager
    def using_client(self, client):
        """临时把后续发送切到指定连接（为 None 时回到默认连接）。"""
        token = _CURRENT_CLIENT.set(client)
        try:
            yield
        finally:
            _CURRENT_CLIENT.reset(token)

    def _active_client(self):
        return _CURRENT_CLIENT.get() or self.client

    @contextlib.contextmanager
    def send_once(self):
        """这一段发送不做超时重试。

        超时（等不到发送回执）不等于消息没发出去：重试一次就可能让对方收到两遍。
        不需要即时性的消息（心情日记）宁可漏一条，也不重发。
        """
        prev = self._send_once
        self._send_once = True
        try:
            yield
        finally:
            self._send_once = prev

    # ---------------- 基础发送 ----------------
    async def send_segments(self, session_type: str, target_id, segments: list,
                            group=None) -> bool:
        self.last_send_uncertain = False
        client = self._active_client()
        if client is None:
            print("发送失败：NapCat 客户端尚未连接")
            return False
        session_type, target_id = _normalize_target(session_type, target_id)
        self.last_message_id = ""
        try:
            result = await self._send_with_retry(session_type, target_id, segments)
            self.last_message_id = _extract_message_id(result)
            self._remember_sent(session_type, target_id, self.last_message_id, group)
            return True
        except Exception as e:
            # 超时只是没等到回执，消息可能已经发出去了，跟"确定没发出去"要分开
            self.last_send_uncertain = isinstance(e, (asyncio.TimeoutError, TimeoutError))
            print(f"发送消息失败 ({session_type} {target_id}): {type(e).__name__}: {e}")
            return False

    def new_message_group(self) -> int:
        """开一个新的消息组，返回组号。

        同一句话会拆成文字与语音两条消息，撤回时要整组一起撤；把组号传给这几次
        发送即可。不传组号的消息各自成组（纯文字通道每句话本来就只有一条）。
        """
        self._sent_group += 1
        return self._sent_group

    def _remember_sent(self, session_type: str, target_id, message_id: str,
                       group=None) -> None:
        """记下这个会话最近发出去的消息 id（撤回刚才那条时按它找）。"""
        mid = str(message_id or "").strip()
        if not mid:
            return
        key = f"{session_type}|{target_id}"
        now = time.monotonic()
        items = [(t, m, g) for t, m, g in self._recent_sent.get(key, [])
                 if now - t < RECENT_SENT_TTL_SECONDS]
        items.append((now, mid, self.new_message_group() if group is None else group))
        self._recent_sent[key] = items[-RECENT_SENT_MAX:]

    async def _send_with_retry(self, session_type: str, target_id, segments: list):
        """NTQQ 的 sendMsg 偶发等不到消息列表更新确认（NapCat 报 NTEvent Timeout），
        此时消息通常并未发出。稍候重试，避免整句丢失；次数由 send_retry 配置，默认 1。"""
        client = self._active_client()
        retries = 0 if self._send_once else max(0, int(self.config.get("send_retry", 1) or 0))
        for attempt in range(retries + 1):
            try:
                if session_type == "private":
                    return await client.send_private_msg(user_id=_as_target(target_id),
                                                         message=segments)
                return await client.send_group_msg(group_id=_as_target(target_id),
                                                   message=segments)
            except (asyncio.TimeoutError, TimeoutError):
                if attempt >= retries:
                    raise
                delay = 2.0 * (attempt + 1)
                print(f"发送超时 ({session_type} {target_id})，{delay:.0f}s 后重试 "
                      f"({attempt + 1}/{retries})")
                await asyncio.sleep(delay)

    async def send_text(self, session_type: str, target_id, text: str,
                        sticker=None, reply_id=None, at_ids=None, group=None) -> bool:
        from napcat import Text, Image, At, Reply
        normalized_type, _ = _normalize_target(session_type, target_id)
        text = "" if text is None else str(text)
        # 这条消息实际发出去之后才有 id：没走到发送那一步就保持为空，
        # 免得撤回拿到上一条消息的 id
        self.last_message_id = ""
        mentions = []
        if normalized_type == "group":
            seen_at = set()
            for qq in at_ids or []:
                value = str(qq or "").strip()
                if value and value not in seen_at:
                    mentions.append(At(qq=value))
                    seen_at.add(value)
        # @ 的位置由正文里的占位符决定（模型把它写在想@人的地方）；
        # 没有占位符时退回把@放在消息开头，正文里残留的占位符一律清掉。
        body = []
        if mentions and MENTION_PLACEHOLDER in text:
            head, _, tail = text.partition(MENTION_PLACEHOLDER)
            tail = tail.replace(MENTION_PLACEHOLDER, "")
            if head.strip():
                body.append(Text(text=head))
            body.extend(mentions)
            # @ 段与后面的正文之间留一个空格：客户端不会自动补，
            # 不补的话「@某人」和紧接着的字会粘成一句
            tail = tail.lstrip(" \t")
            if tail:
                body.append(Text(text=" " + tail))
        else:
            if MENTION_PLACEHOLDER in text:
                text = re.sub(r"[ \t]{2,}", " ",
                              text.replace(MENTION_PLACEHOLDER, "")).strip(" \t")
            body.extend(mentions)
            # 同上：@ 挂在消息开头时也要和正文隔开
            if mentions and text.strip():
                text = " " + text
            # 只在有文本时才加 Text 段：带表情包但不带文字时若塞入空 Text，
            # 消息段列表会出现一个空文本段（部分客户端会显示空白气泡）。
            if text.strip():
                body.append(Text(text=text))
        segments = []
        # 群聊一律能引用；私聊引用只有声明了 quote_private 的通道发得出来
        # （QQ 官方机器人 C2C 支持，NapCat 的私聊回复不带 Reply 段）
        if reply_id is not None and (normalized_type == "group"
                                     or client_supports(self._active_client(),
                                                        "quote_private", default=False)):
            segments.append(Reply(id=str(reply_id)))
        segments.extend(body)
        temp_sticker = None
        # 只发文字的通道（微信 ClawBot / QQ 官方）收不到图片段：适配器会静默丢掉，
        # 上层却看到"发送成功"。这里直接不发，连缩放都省掉。
        if sticker is not None and not client_supports(self._active_client(), "sticker"):
            sticker = None
        if sticker is not None:
            try:
                send_sticker, temp_sticker = resize_for_output(sticker, self.config)
                segments.append(Image(file=str(Path(send_sticker).resolve())))
            except Exception as e:
                if temp_sticker is not None:
                    Path(temp_sticker).unlink(missing_ok=True)
                    temp_sticker = None
                print(f"表情包发送失败: {e}")
        if not segments:
            return True
        await self._pace_text_only(session_type, target_id)
        try:
            ok = await self.send_segments(session_type, target_id, segments, group=group)
            if ok and sticker is not None:
                # 供聊天记录把表情包以真实图片记进历史（主流程取用后清除）
                self.last_sent_sticker = {"key": f"{normalized_type}|{target_id}",
                                          "path": str(sticker)}
            return ok
        finally:
            if temp_sticker is not None:
                Path(temp_sticker).unlink(missing_ok=True)

    async def pace_text_only(self, session_type: str, target_id) -> None:
        """整条回复没有语音时，纯文字也按同一条节奏逐条发。"""
        await self._pace_text_only(session_type, target_id, force=True)

    async def _pace_text_only(self, session_type: str, target_id,
                              force: bool = False) -> None:
        """发文字前拉开间隔：连着蹦好几条容易被服务端当成刷屏。

        微信 ClawBot 这类通道一条回复会按句发好几条，句间隔只有几百毫秒；
        端上看到的正常节奏是 1.5 秒上下一条（别的实现也是这么节流的）。
        能发语音的通道本来靠语音时长天然隔开，但整条回复没有语音时（TTS 关掉、
        合成失败，或这一句就是纯文字）同样会连着蹦，这时按 force 走同一条节奏。
        """
        if not force and client_supports(self._active_client(), "voice"):
            return
        key = f"{session_type}|{target_id}"
        now = time.monotonic()
        gap = TEXT_ONLY_SEND_MIN_GAP + random.uniform(0, TEXT_ONLY_SEND_GAP_JITTER)
        last = self._text_only_last_sent.get(key)
        wait = 0.0 if last is None else max(0.0, last + gap - now)
        if wait:
            await asyncio.sleep(wait)
        self._text_only_last_sent[key] = time.monotonic()

    async def send_voice(self, session_type: str, target_id, wav_path, group=None) -> bool:
        from napcat import Record
        return await self.send_segments(session_type, target_id,
                                        [Record(file=str(Path(wav_path).resolve()))],
                                        group=group)

    async def poke_receipt(self, session_type: str, target_id, user_id) -> dict:
        """戳一戳对方并返回结构化回执；被挡下时带上原因。

        只返回 True/False 的话，被挡下的那次戳一戳在链路上完全无声：
        模型以为戳了、判定以为没戳。回执让上层的判定与下一轮上下文看到真相。
        """
        client = self._active_client()
        if client is None:
            return _action_receipt("戳一戳", False, reason="no_client")
        if not self.config.get("poke_enabled", True):
            return _action_receipt("戳一戳", False, reason="disabled")
        if not client_supports(client, "poke"):
            return _action_receipt("戳一戳", False, reason="unsupported")
        session_type, target_id = _normalize_target(session_type, target_id)
        poke_id = str(user_id or "").strip() or str(target_id)
        try:
            if session_type == "group":
                await client.group_poke(group_id=_as_target(target_id),
                                        user_id=_as_target(poke_id))
            else:
                await client.friend_poke(user_id=_as_target(poke_id))
        except Exception as e:
            print(f"戳一戳发送失败 ({session_type} {target_id}): {type(e).__name__}: {e}")
            return _action_receipt("戳一戳", False, reason="error")
        return _action_receipt("戳一戳", True, target=poke_id)

    async def send_poke(self, session_type: str, target_id, user_id) -> bool:
        """戳一戳对方（群聊里 target_id 是群号、user_id 是被戳的人）。

        回执见 poke_receipt：这里只保留"成没成功"的布尔结果，
        详细原因留在 self.last_poke_receipt 上供上层取用。
        """
        receipt = await self.poke_receipt(session_type, target_id, user_id)
        self.last_poke_receipt = receipt
        return bool(receipt.get("ok"))

    async def _poke_and_receipt(self, session_type: str, target_id, target) -> dict:
        """戳一戳并返回回执。

        send_poke 会被插件或测试替换成别的实现，那时拿不到 last_poke_receipt，
        这里退回按布尔结果拼一份最小回执。
        """
        ok = await self.send_poke(session_type, target_id, target)
        receipt = getattr(self, "last_poke_receipt", None)
        if isinstance(receipt, dict) and receipt:
            return receipt
        # 替换过的 send_poke 拿不到详细回执：至少把对象带上，
        # 否则群里几个人时判定分不清这次戳的是谁
        return _action_receipt("戳一戳", bool(ok), target=str(target or ""))

    def _track_sent_id(self, bucket: List[str]) -> None:
        """把刚发出去那条消息的 id 记下来（撤回时要用）。"""
        mid = str(self.last_message_id or "").strip()
        if mid:
            bucket.append(mid)

    async def delete_message(self, session_type: str, target_id, message_id) -> bool:
        """撤回一条消息（自己的，或管理员权限下的别人的）。

        只发文字的接入方式（微信 ClawBot / QQ 官方）没有撤回接口，直接跳过。
        """
        client = self._active_client()
        mid = str(message_id or "").strip()
        if client is None or not mid:
            return False
        if not client_supports(client, "recall"):
            return False
        try:
            await client.delete_msg(message_id=_as_target(mid))
        except Exception as e:
            print(f"撤回消息失败 ({session_type} {target_id}): {type(e).__name__}: {e}")
            return False
        return True

    def schedule_recall(self, session_type: str, target_id, message_ids,
                        delay: float) -> None:
        """过一会儿再撤回刚发出去的消息（模型要求撤回时走这里）。

        从发出去到撤回隔着好几秒，不能占着会话锁干等，所以挂一个后台任务；
        任务里 contextvars 已经还原了，要显式带上当时用的那条连接。
        """
        ids = [str(m) for m in (message_ids or []) if str(m or "").strip()]
        if not ids:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        client = self._active_client()

        async def _worker():
            await asyncio.sleep(delay)
            with self.using_client(client):
                done = 0
                for mid in ids:
                    if await self.delete_message(session_type, target_id, mid):
                        done += 1
                if done < len(ids):
                    print(f"撤回：{done}/{len(ids)} 条撤回成功"
                          "（发不出去的通常是这条接入方式不支持撤回）。")

        task = loop.create_task(_worker())
        self._recall_tasks.add(task)
        task.add_done_callback(self._recall_tasks.discard)

    async def recall_recent(self, session_type: str, target_id, count: int = 1) -> int:
        """立刻撤回这个会话最近发出去的消息，返回真的撤掉了几条。

        主人说「撤回刚才那条」时走这里：模型填不填 recall 字段都不影响。
        """
        session_type, target_id = _normalize_target(session_type, target_id)
        key = f"{session_type}|{target_id}"
        now = time.monotonic()
        items = [(t, m, g) for t, m, g in self._recent_sent.get(key, [])
                 if now - t < RECENT_SENT_TTL_SECONDS]
        self._recent_sent[key] = items
        done = 0
        for _ in range(max(1, int(count or 1))):
            if not items:
                break
            _, mid, _ = items.pop()
            if await self.delete_message(session_type, target_id, mid):
                done += 1
        self._recent_sent[key] = items
        return done

    async def recall_message(self, session_type: str, target_id, message_id) -> int:
        """撤回指定的一条消息，连同与它同一句话的其它消息。

        主人引用某条消息说「撤回这条」时走这里：同一条回复的一句话会拆成文字与
        语音两条独立消息，只撤其中一条会剩下半截。这条消息太旧、记录里已经找不到
        时至少把它自己撤掉。
        """
        session_type, target_id = _normalize_target(session_type, target_id)
        wanted = str(message_id or "").strip()
        if not wanted:
            return 0
        key = f"{session_type}|{target_id}"
        now = time.monotonic()
        items = [(t, m, g) for t, m, g in self._recent_sent.get(key, [])
                 if now - t < RECENT_SENT_TTL_SECONDS]
        group = next((g for _, m, g in items if m == wanted), None)
        ids = [m for _, m, g in items if g == group] if group is not None else [wanted]
        done = 0
        for mid in ids:
            if await self.delete_message(session_type, target_id, mid):
                done += 1
        if done:
            gone = set(ids)
            self._recent_sent[key] = [(t, m, g) for t, m, g in items if m not in gone]
        else:
            self._recent_sent[key] = items
        return done

    async def recall_other_receipt(self, session_type: str, target_id, message_id,
                                   requester_id: str = "",
                                   requester_role: str = "") -> dict:
        """撤回**别人发的**一条消息，返回回执。

        目标由上层定好：引用了某条就撤那条，没引用就撤本轮触发消息。
        那条如果是机器人自己发的，本来就不需要管理员权限；是别人的则要求
        提出要求的人与机器人自己都是管理员或群主。
        """
        client = self._active_client()
        if client is None:
            return _action_receipt("撤回别人的消息", False, reason="no_client")
        if not self.config.get("recall_other_enabled", True):
            return _action_receipt("撤回别人的消息", False, reason="disabled")
        if str(session_type) != "group" or not client_supports(client, "recall_other"):
            return _action_receipt("撤回别人的消息", False, reason="unsupported")
        mid = str(message_id or "").strip()
        if not mid:
            return _action_receipt("撤回别人的消息", False, reason="error")
        session_type, target_id = _normalize_target(session_type, target_id)
        own = any(m == mid for _, m, _ in self._recent_sent.get(
            f"{session_type}|{target_id}", []))
        if not own and not await can_manage_group(client, target_id, requester_id,
                                                 requester_role):
            return _action_receipt("撤回别人的消息", False, reason="denied")
        done = await self.recall_message(session_type, target_id, mid)
        if not done:
            return _action_receipt("撤回别人的消息", False, reason="error")
        return _action_receipt("撤回别人的消息", True, count=done)

    async def mute_receipt(self, session_type: str, target_id, user_id, seconds: int = 0,
                           requester_id: str = "", requester_role: str = "") -> dict:
        """禁言群里某个人，返回回执。

        只有群聊里有这件事；要机器人自己是管理员或群主，禁言别人时还要求提要求的人
        也是（否则普通群成员能借她的号去禁言别人），禁言他自己则谁提都算。
        """
        client = self._active_client()
        if client is None:
            return _action_receipt("禁言", False, reason="no_client")
        if not self.config.get("mute_enabled", True):
            return _action_receipt("禁言", False, reason="disabled")
        if str(session_type) != "group" or not client_supports(client, "mute"):
            return _action_receipt("禁言", False, reason="unsupported")
        session_type, target_id = _normalize_target(session_type, target_id)
        target = str(user_id or "").strip()
        # 禁言自己是没意义的事，模型偶尔会把目标填成机器人自己的号
        if not target or target == str(getattr(client, "self_id", "") or ""):
            return _action_receipt("禁言", False, reason="no_target")
        reason = await mute_denied_reason(client, target_id, requester_id,
                                          requester_role, target)
        if reason:
            # 失败回执同样带上对象：判定与下一轮才知道"谁没禁成、为什么"
            return _action_receipt("禁言", False, reason=reason, target=target)
        try:
            await client.set_group_ban(group_id=_as_target(target_id),
                                       user_id=_as_target(target),
                                       duration=int(seconds or DEFAULT_MUTE_SECONDS))
        except Exception as e:
            print(f"禁言失败 ({target_id}/{target}): {type(e).__name__}: {e}")
            return _action_receipt("禁言", False, reason="error", target=target)
        return _action_receipt("禁言", True, target=target)

    async def _send_plain_reply(self, session_type: str, target_id, sentences: List[dict],
                                delivery: str, reply_id=None, at_ids=None) -> dict:
        """只发文字、不合成语音的回复；delivery=chars 时一个字一条消息。

        模型只有被主人明确要求「一个字一个字说话」时才会选 chars。回复被拆成多条
        消息，引用与 @ 只挂在第一条上；拆出来的条数超过上限就退回按句发。
        """
        result = {"tts_ms": 0.0, "voice_ok": False, "tts_calls": 0, "sent_texts": [],
                  "message_ids": [], "action_receipts": []}
        texts: List[str] = []
        if delivery == DELIVERY_CHARS:
            for s in sentences:
                texts.extend(_char_pieces(s.get("display") or s.get("zh") or ""))
        if not texts or len(texts) > DELIVERY_MAX_MESSAGES:
            texts = [str(s.get("display") or s.get("zh") or "").strip() for s in sentences]
            texts = [t for t in texts if t]
        # 正文里写了占位符时 @ 只挂在它所在的那条消息上，见 send_reply 同名变量
        mention_in_body = any(MENTION_PLACEHOLDER in t for t in texts)
        for idx, text in enumerate(texts):
            if idx:
                await asyncio.sleep(DELIVERY_CHAR_GAP_SECONDS)
            # @ 只挂在第一条或承载占位符的那一条上：逐字发时占位符自成一格，
            # 真正被@的是它所在的那条消息
            at_used = at_ids if (at_ids and (
                MENTION_PLACEHOLDER in text
                or (idx == 0 and not mention_in_body))) else None
            ok = await self.send_text(
                session_type, target_id, text,
                reply_id=reply_id if idx == 0 else None,
                at_ids=at_used)
            if ok:
                result["action_receipts"].extend(sent_action_receipts(
                    reply_id is not None and idx == 0, at_used))
                result["sent_texts"].append(text)
                self._track_sent_id(result["message_ids"])
        return result

    # ---------------- 回复合送（保持原有分合逻辑 + 表情包） ----------------
    async def send_reply(self, session_type: str, target_id: str, sentences: List[dict],
                        emotions: dict, ctx: RoleContext, use_voice: bool = True,
                        reply_id=None, allowed_at_ids=None, poke_target=None,
                        recall_request: str = "", at_names=None,
                        speaker_id: str = "", speaker_role: str = "",
                        mute_ids=None, recall_other_id: str = "",
                        mute_done_ids=None) -> dict:
        """按配置发送整组句子（文本+语音），返回 {tts_ms, voice_ok}。

        sentences: [{zh, lang, display, emotion}]
        recall_request 为 "next" 时，无论模型填没填 recall，这条回复都会被撤回。
        at_names / speaker_id 只用于把台词里写成普通文字的 @ 还原成真正的@。
        speaker_role / mute_ids / recall_other_id 供禁言与撤回别人的消息用：
        mute_ids 是这次允许禁言的号码（本轮@到的人 + 被引用消息的发送者 + 当前发言者）。
        mute_done_ids 是系统路径本轮已经禁言过的号码：模型再填同一个目标时不重复执行。
        """
        result = {"tts_ms": 0.0, "voice_ok": False, "tts_calls": 0, "sent_texts": [],
                  "message_ids": [], "action_receipts": []}
        # 这条接入方式发不了语音（微信 ClawBot、QQ 官方）就别去合成
        if use_voice and not client_supports(self._active_client(), "voice"):
            use_voice = False
        # 选择性发送语音：整条回复只掷一次，逐句合成时不会再变
        if use_voice and not voice_enabled_for(self.config, session_type):
            use_voice = False
        if not sentences:
            return result
        session_type, target_id = _normalize_target(session_type, target_id)
        # 禁言与撤回别人的消息跟"这条回复发出去什么"无关：在开头发一次就行，
        # 不用在每条发送分支里各写一遍。回执收进 result，判定与下一轮据此知道做没做成。
        mute_sentence = next((s for s in sentences if s.get("mute")), None)
        if mute_sentence is not None:
            mute_target = mute_target_of(mute_sentence, mute_ids, speaker_id)
            done_ids = {str(q) for q in (mute_done_ids or [])}
            if mute_target and str(mute_target) in done_ids:
                # 主人明确吩咐的禁言系统路径已经执行过：同一个目标再执行一遍
                # 是重复动作，回执也会从"一份"变成"两份"
                print(f"禁言：本轮系统已对 {mute_target} 执行过，台词里的 mute 不再重复执行。")
            else:
                result["action_receipts"].append(await self.mute_receipt(
                    session_type, target_id, mute_target,
                    mute_seconds_of(mute_sentence), speaker_id, speaker_role))
        recall_other_sentence = next((s for s in sentences if s.get("recall_other")), None)
        # recall_request 为 RECALL_DENY 表示"主人这条消息要撤的那条，系统在生成回复之前
        # 就已经撤掉了"：这时再撤一遍必然失败（消息已经不在了），回执还会骗到判定那边
        if recall_other_sentence is not None and recall_request != RECALL_DENY:
            result["action_receipts"].append(await self.recall_other_receipt(
                session_type, target_id, recall_other_id, speaker_id, speaker_role))
        # 动作字段只认第一句：@ 也在这里补，改完再取动作，免得改写出来的 mention_ids 白填。
        # 字面写的两个 @ 可能落在后面任何一句上，所以每一句都要过一遍。
        literal_mention = False
        for sentence in sentences:
            if apply_literal_mention(sentence, allowed_at_ids, at_names, speaker_id):
                literal_mention = True
        if literal_mention:
            print("发送：台词里写成文字的@，已换成真正的@。")
        action_sentence = next((s for s in sentences
                                if s.get("reply_to") or s.get("mention_ids") or s.get("poke")
                                or s.get("delivery") or s.get("recall")), {})
        # @ 的目标取全部句子声明的合集：声明写在后面某一句时也要能认出来
        selected_mentions = []
        for sentence in sentences:
            for qq in sentence.get("mention_ids") or []:
                value = str(qq)
                if value in {str(x) for x in (allowed_at_ids or [])} \
                        and value not in selected_mentions:
                    selected_mentions.append(value)
        action_reply_id = reply_id if action_sentence.get("reply_to") else None
        actions_pending = bool(action_reply_id is not None or selected_mentions)
        # 正文里写了占位符时，@ 只跟着那条消息走：再顶到第一条最前面会让整条回复
        # 里出现两个 @，而且第一个 @ 挂在并没有@人的那句上
        mention_in_body = any(MENTION_PLACEHOLDER in str(s.get("display") or "")
                              for s in sentences)
        poke_pending = bool(action_sentence.get("poke")) and bool(poke_target)
        recall_pending = (recall_request == "next"
                          or (bool(action_sentence.get("recall"))
                              and recall_request != RECALL_DENY)) \
            and bool(self.config.get("recall_enabled", True))
        recall_delay = recall_delay_of(action_sentence)
        # 发送形态是逐字 / 纯文字时整条走纯文字分支：逐句流式发会把它拆碎，
        # 语音合成也会白跑一遍
        delivery = str(action_sentence.get("delivery") or "").strip().lower()
        if delivery not in DELIVERY_MODES:
            delivery = ""
        # 这条通道一次回复能发几条消息是定死的（QQ 官方机器人：5 条）：
        # 逐字发送一条回复会拆出十几条，直接超频，退回合并发送
        reply_limit = client_reply_limit(self._active_client())
        if reply_limit and delivery:
            delivery = ""
            print(f"发送：这条通道一次回复最多 {reply_limit} 条消息，逐字发送已改回合并发送。")
        if delivery:
            if poke_pending:
                result["action_receipts"].append(
                    await self._poke_and_receipt(session_type, target_id, poke_target))
            plain = await self._send_plain_reply(
                session_type, target_id, sentences, delivery,
                reply_id=action_reply_id, at_ids=selected_mentions)
            if recall_pending:
                self.schedule_recall(session_type, target_id,
                                     plain.get("message_ids"), recall_delay)
            # 纯文字分支自己另起了一份 result：把开头收下的动作回执并回去，
            # 否则禁言/撤回别人的消息明明做了，判定那边看到的却是"什么都没声明"
            plain["action_receipts"] = list(result["action_receipts"]) + list(
                plain.get("action_receipts") or [])
            return plain

        # 真的逐条发出去的文本（合并发送时只有一条）：调用方据此决定聊天记录
        # 要不要跟着拆成多条，免得界面上看到的是"一整段"，和 QQ 里的样子对不上
        sent_texts: List[str] = []
        # 这条回复真正发出去的消息 id：模型要求撤回时按它逐条撤
        sent_ids: List[str] = []

        action_receipts: List[dict] = []

        async def _send_text(text, sticker=None, zh=None, group=None, pace=False):
            nonlocal actions_pending, poke_pending
            if poke_pending:
                poke_pending = False
                action_receipts.append(
                    await self._poke_and_receipt(session_type, target_id, poke_target))
            # 整条只有动作、没有台词时这里拿到的是空串：动作上面已经发出去了，
            # 不再发一条空消息
            if not str(text or "").strip() and sticker is None:
                actions_pending = False
                return True
            if pace:
                await self.pace_text_only(session_type, target_id)
            # @ 跟着带占位符的那条消息走：占位符可能不在第一条上；
            # 正文里根本没写占位符时才退回把 @ 顶在第一条最前面
            quote_used = bool(actions_pending and action_reply_id is not None)
            at_used = selected_mentions if (selected_mentions and (
                MENTION_PLACEHOLDER in str(text)
                or (actions_pending and not mention_in_body))) else None
            ok = await self.send_text(
                session_type, target_id, text, sticker=sticker,
                reply_id=action_reply_id if quote_used else None,
                at_ids=at_used,
                group=group)
            if ok:
                action_receipts.extend(sent_action_receipts(quote_used, at_used))
                actions_pending = False
                sent_texts.append(str(text if zh is None else zh))
                self._track_sent_id(sent_ids)
            return ok

        data_path = self.memory_manager.data_path
        separate_send = self.config.get("separate_send", False)
        send_voice_separately = self.config.get("send_voice_separately", False)
        text_separate = self.config.get("text_separate", False)
        # 逐句分开发：每句文字、语音各一条。超过这条通道一次回复的条数上限时
        # 不做合并——QQ 官方的富媒体语音消息不显示 content，把文字并进语音会
        # 凭空丢字；超出的条数由适配器改按主动消息接着发出去。
        dynamic_sleep = self.config.get("dynamic_sleep", True)
        use_tts = use_voice and self.config.get("tts_reply_enabled", True)
        if separate_send and (send_voice_separately or text_separate) \
                and self.config.get("separate_force_segment", True):
            sentences = segment_for_tts(sentences)

        # 1. 合成语音（全部句子一次性合成，或逐个合成，取决于是否需要分开）
        wavs: List[Optional[Path]] = [None] * len(sentences)
        if use_tts and self._active_client() is not None:
            synth_started = time.time()
            synth_sem = asyncio.Semaphore(2)
            async def _synthesize(s):
                async with synth_sem:
                    # 参数顺序：text=要念的台词(lang)，emotion=情绪名。
                    # 早期版本这里传反了，导致情绪被当成文本、文本被当成情绪，
                    # 情绪永远回退默认音色。synthesize_sentence 内另有兜底纠正。
                    return await synthesize_sentence(ctx, s["lang"], s.get("emotion", ""), emotions,
                                                     data_path, stats=self.stats,
                                                     mimic=s.get("mimic", ""),
                                                     mimics=available_mimics(ctx))
            async with voice_lock():
                results = await asyncio.gather(*[_synthesize(s) for s in sentences],
                                               return_exceptions=True)
            # 这条路径的耗时以前从不赋值，interactions.tts_ms 恒为 0，
            # 统计页的「平均 TTS 耗时」被系统性低估
            result["tts_ms"] = (time.time() - synth_started) * 1000
            for idx, item in enumerate(results):
                if isinstance(item, BaseException):
                    print(f"第 {idx + 1} 句语音合成异常，该句降级为纯文本: "
                          f"{type(item).__name__}: {item}")
                    wavs[idx] = None
                else:
                    wavs[idx] = item
        valid_wavs = [w for w in wavs if w]
        result["voice_ok"] = bool(valid_wavs)
        result["tts_calls"] = len(valid_wavs)

        # 表情包选择：默认只挑第一句情绪；sticker_every_sentence 开启时逐句挑，
        # 累计张数受 sticker_max_per_reply 限制
        sticker_sent = 0

        async def _pick_sticker_for(idx: int):
            nonlocal sticker_sent
            if self.sticker_manager is None or self._active_client() is None:
                return None
            # 挑之前先看这条通道发不发得了图：只发文字的通道连挑都别挑
            # （description 模式下挑一次要多调一次 LLM）
            if not client_supports(self._active_client(), "sticker"):
                return None
            try:
                max_stickers = max(1, int(self.config.get("sticker_max_per_reply", 1)))
            except (TypeError, ValueError):
                max_stickers = 1
            if sticker_sent >= max_stickers:
                return None
            if not bool(self.config.get("sticker_every_sentence", False)) and idx != 0:
                return None
            sentence = sentences[idx]
            st = await self.sticker_manager.pick_async(
                ctx, sentence.get("emotion", ""),
                str(sentence.get("display") or sentence.get("zh") or ""))
            if st:
                sticker_sent += 1
            return st

        # ========== 分开发送 + 语音分开发送 ==========
        if separate_send and send_voice_separately:
            missing = []
            done_stickers = []
            pacer = VoicePacer(dynamic_sleep)
            for idx, wav in enumerate(wavs):
                sentence_text = sentences[idx]["display"]
                # ① 语音失败的句子先记下，稍后统一补发文本，避免文本重复发送
                if not wav or not wav.exists():
                    missing.append(idx)
                    continue
                # ② 逐句发送文本 + 对应语音（等上一条播完，用的是上一条自己的时长）
                await pacer.wait()
                # 这一句的文字与语音算同一条消息的两半：撤回要一起撤
                msg_group = self.new_message_group()
                if sentence_text:
                    await _send_text(sentence_text, zh=sentences[idx].get("zh"),
                                     group=msg_group)
                await self.send_voice(session_type, target_id, wav, group=msg_group)
                self._track_sent_id(sent_ids)
                await pacer.hold_voice(wav)
                wav.unlink(missing_ok=True)
                sticker_i = await _pick_sticker_for(idx)
                if sticker_i:
                    done_stickers.append(sticker_i)

            # ③ 没有可用语音（这条接入方式发不了语音，或整段合成都失败）：文本仍按句逐条发。
            # 合成不了语音就退回合并文本的话，「分开发送」在纯文字通道上等于没开，
            # 整段回复会挤成一条（与逐句发送时每句都带文本的行为也不一致）
            if not valid_wavs:
                for s in sentences:
                    text = s["display"]
                    if text:
                        await _send_text(text, zh=s.get("zh"), pace=True)
            else:
                # ④ 处理语音失败的句子（补发文本，只发一次）
                for idx in missing:
                    text = sentences[idx]["display"]
                    if text:
                        await _send_text(text, zh=sentences[idx].get("zh"), pace=True)

            # ⑤ 最后统一发送表情包
            for sticker_path in done_stickers:
                await self.send_text(session_type, target_id, "", sticker=sticker_path)
                self._track_sent_id(sent_ids)

        # ========== 合并发送（默认或文字分开但语音合并） ==========
        else:
            # 合并是 CPU 密集的同步操作（numpy 拼接可能耗时数百毫秒），
            # 放进线程池避免阻塞事件循环
            combined_audio = await asyncio.to_thread(
                merge_wavs, valid_wavs, self.config, data_path) if valid_wavs else None
            if valid_wavs and not combined_audio:
                # 合并失败（历史上 merge_wavs 遇到异常会静默返回 None）时，
                # 退回无损直接拼接，绝不因为合并失败就把整条语音丢掉。
                from .tts import simple_concat_wavs
                combined_audio = await asyncio.to_thread(
                    simple_concat_wavs, valid_wavs, data_path)
                if combined_audio:
                    print("合并音频已降级为无损直接拼接（内容不丢）。")
            combined_text = "".join(s["display"] for s in sentences)

            if combined_audio:
                # 如果开启了文字分开发送（但语音合并），则先发语音，再逐句发文字
                if separate_send and text_separate:
                    await self.send_voice(session_type, target_id, combined_audio)
                    self._track_sent_id(sent_ids)
                    for i, s in enumerate(sentences):
                        await _send_text(s["display"], zh=s.get("zh"))
                        # 文字之间采用固定间隔（如果希望基于语音时长，可改为语音时长）
                        await asyncio.sleep(0.2)
                else:
                    # 正常合并发送：文字+语音一起发。
                    # 这条语音是本轮的最后一条，后面没有要发的语音，
                    # 再等它播完只会把整条会话锁住、拖慢排队的下一条消息
                    msg_group = self.new_message_group()
                    await _send_text(combined_text, group=msg_group)
                    await self.send_voice(session_type, target_id, combined_audio,
                                          group=msg_group)
                    self._track_sent_id(sent_ids)

                # 清理临时文件
                for w in valid_wavs:
                    w.unlink(missing_ok=True)
                combined_audio.unlink(missing_ok=True)
            else:
                # 语音合成失败，降级纯文本
                print("TTS 合成失败或未启用，降级为纯文本。")
                await _send_text(combined_text)
                for w in valid_wavs:
                    w.unlink(missing_ok=True)

            # 最后发送表情包（合并路径最多一张）
            sticker = await _pick_sticker_for(0)
            if sticker:
                await self.send_text(session_type, target_id, "", sticker=sticker)
                self._track_sent_id(sent_ids)

        await asyncio.to_thread(self.memory_manager.cleanup_voice_cache,
                                self.config.get("max_voice_cache", 20))
        if recall_pending:
            self.schedule_recall(session_type, target_id, sent_ids, recall_delay)
            action_receipts.append({"action": "撤回", "ok": bool(sent_ids),
                                    "count": len(sent_ids), "at": time.time()})
        result["sent_texts"] = sent_texts
        result["message_ids"] = sent_ids
        # 开头收下的（禁言 / 撤回别人的消息）要留着，不能被这里的列表覆盖掉
        result["action_receipts"] = list(result["action_receipts"]) + action_receipts
        return result

    # ---------------- 主动消息（定时/提醒/问候） ----------------
    async def speak_and_send(self, session_type: str, target_id, text: str,
                             emotions: dict, ctx: Optional[RoleContext] = None,
                             use_voice: bool = None, sticker: bool = False,
                             emotion: str = "", session_id: str = "",
                             record_history: bool = True) -> bool:
        if not text:
            return False
        self.last_send_uncertain = False
        if self._active_client() is None:
            print("主动消息发送失败：NapCat 客户端未连接")
            return False
        if ctx is None:
            ctx = RoleContext(self.config)
        if use_voice is None:
            use_voice = bool(self.config.get("proactive_voice", False))
        # 这条接入方式发不了语音/贴纸（微信 ClawBot、QQ 官方）就别去做这些：
        # 语音那边会白跑一遍 TTS，贴纸则直接发失败
        active = self._active_client()
        if use_voice and not client_supports(active, "voice"):
            use_voice = False
        if sticker and not client_supports(active, "sticker"):
            sticker = False
        sticker_path = (await self.sticker_manager.pick_async(ctx, emotion, text)
                        if (sticker and self.sticker_manager) else None)
        if use_voice and await check_tts_service(self.config):
            voice = str(emotion or ctx.get("default_voice", "pingjing") or "pingjing")
            speech_text = await self._speech_text_for(ctx, text)
            wav = await self._synthesize_proactive(ctx, speech_text, voice, emotions)
            if wav:
                try:
                    # 文本是正文，语音只是载体：正文发出即视为成功，
                    # 否则调用方会把同一句话重发一遍
                    text_ok = await self.send_text(session_type, target_id, text)
                    await self.send_voice(session_type, target_id, wav)
                    if sticker_path:
                        await self.send_text(session_type, target_id, "", sticker=sticker_path)
                    if text_ok and record_history:
                        self.record_outgoing_history(session_id, text, ctx)
                    return bool(text_ok)
                finally:
                    wav.unlink(missing_ok=True)
        ok = await self.send_text(session_type, target_id, text, sticker=sticker_path)
        if ok and record_history:
            self.record_outgoing_history(session_id, text, ctx)
        return ok

    def record_outgoing_history(self, session_id: str, text: str, ctx=None) -> None:
        """把主动发出的消息记进会话历史（问候/提醒/主动开口走这条）。

        这些消息不经过回复管线，此前只发不记：用户接着回复时模型看不到自己
        刚说过什么，WebUI 的聊天记录里也找不到这条主动问候。
        """
        if not session_id or self.memory_manager is None or not str(text or "").strip():
            return
        try:
            data = self.memory_manager.load_session_data(session_id)
            data.setdefault("history", []).append({
                "role": "assistant", "content": text, "timestamp": time.time(),
                "speaker": ctx.character_name if ctx is not None else "",
                "proactive": True})
            self.memory_manager.save_session_data(session_id, data)
        except Exception as e:
            print(f"主动消息写入历史失败: {type(e).__name__}: {e}")

    async def _speech_text_for(self, ctx, text: str) -> str:
        """纯文本消息的合成台词：语音要念角色语言，展示文本是别种语言时先译过去。

        主动消息/提醒/问候的话术是展示语言（如中文）写出来的，而角色的语音
        参考音频与台词语言是另一种语言（如日语）：直接拿展示文本去合成，念出来
        就是另一种语言的语音。这里按配置的文本语言重译一份专供合成，
        译不出来就退回原文（由合成侧按文字判定语言，保证能听懂）。
        """
        target = str(ctx.get("text_lang", "") or "").strip().lower()
        if not target or target == "auto":
            return text
        if not lang_text_broken(text, target):
            return text
        translated = await translate_to_lang(ctx, text, target, label="台词翻译")
        if translated and translated != text:
            print(f"主动消息语音语言修复：展示文本不是{target}，已按{target}重译后合成"
                  f"（{translated[:40]!r}）")
            return translated
        print(f"主动消息语音语言修复失败，改按台词文字判定语言合成：{text[:40]!r}")
        return text

    async def _synthesize_proactive(self, ctx, text: str, emotion: str, emotions: dict):
        """主动消息语音：按句切分逐句合成，任一句失败则整段兜底重试。"""
        from .tts import split_tts_chunks
        data_path = self.memory_manager.data_path
        chunks = split_tts_chunks(text, config=self.config)
        if len(chunks) > 1:
            wavs = []
            for chunk in chunks:
                w = await synthesize_sentence(ctx, chunk, emotion, emotions,
                                              data_path, stats=self.stats)
                if not w:
                    print(f"主动消息分段合成失败，改用整段合成: {chunk[:40]!r}")
                    for done in wavs:
                        done.unlink(missing_ok=True)
                    wavs = []
                    break
                wavs.append(w)
            if wavs:
                merged = await self._merge_voice_chunks(wavs, data_path, keep_on_fail=True)
                if merged:
                    return merged
        return await synthesize_sentence(ctx, text, emotion, emotions,
                                         data_path, stats=self.stats)

    async def _merge_voice_chunks(self, wavs: list, data_path, keep_on_fail: bool = False):
        """合并分段音频：优先带语气渐变，失败则逐级降级，绝不返回"半截语音"。

        同时校验输出文件真实存在且非空——旧版本的 merge_wavs 在缓冲区极短时
        会抛 numpy 广播异常并返回 None，调用方却直接把它当成功结果使用。

        keep_on_fail：合并彻底失败时保留第一段（供调用方退回单段语音），
        而不是把它一起删掉、返回一个已不存在的路径。
        """
        import wave as _wave
        from .tts import merge_wavs, simple_concat_wavs

        def _usable(path) -> bool:
            if not path:
                return False
            try:
                p = Path(path)
                if not p.exists() or p.stat().st_size <= 44:
                    return False
                with _wave.open(str(p), 'rb') as wf:
                    return wf.getnframes() > 0
            except Exception:
                return False

        if len(wavs) == 1:
            return wavs[0] if _usable(wavs[0]) else None
        attempts = [
            ("语气渐变合并", lambda: merge_wavs(wavs, self.config, data_path)),
            ("无渐变无损拼接", lambda: merge_wavs(wavs, self.config, data_path,
                                                crossfade_ms=0)),
            ("直接拼接兜底", lambda: simple_concat_wavs(wavs, data_path)),
        ]
        merged = None
        for label, fn in attempts:
            try:
                candidate = await asyncio.to_thread(fn)
            except Exception as e:
                print(f"{label} 异常: {type(e).__name__}: {e}")
                continue
            if _usable(candidate):
                merged = candidate
                if label != attempts[0][0]:
                    print(f"分段音频合并已降级为「{label}」。")
                break
            if candidate:
                print(f"{label} 产出不可用（空文件或读取失败），尝试下一种合并方式。")
        if merged:
            for w in wavs:
                w.unlink(missing_ok=True)
            return merged
        if keep_on_fail and _usable(wavs[0]):
            for w in wavs[1:]:
                w.unlink(missing_ok=True)
            print("分段音频合并失败，退回第一段（文本内容仍完整）。")
            return wavs[0]
        for w in wavs:
            w.unlink(missing_ok=True)
        return None
