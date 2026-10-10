import contextvars

from modules.scheduler import get_scheduler

try:
    import webview
    HAS_WEBVIEW = True
except ImportError:
    HAS_WEBVIEW = False
    print("警告：未安装 pywebview，将使用浏览器访问。可运行 pip install pywebview 启用。")

try:
    from aiohttp import web
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False
    print("警告：未安装 aiohttp，WebUI 管理功能将不可用。可运行 pip install aiohttp 启用。")

# ============================================================================
# 运行时上下文与全局管理器
# ============================================================================

global_config = None
global_emotion_manager = None
memory_manager = None
db = None
stats_mgr = None
sticker_mgr = None
tool_registry = None
profile_mgr = None
lexicon_mgr = None
rag_mgr = None
todo_mgr = None
job_mgr = None
event_mgr = None
mood_mgr = None
affection_mgr = None
promise_mgr = None
recall_mgr = None
encounter_mgr = None
sender = None
napcat_client = None
# 角色独占的 NapCat 连接：client 实例 id -> 角色标识符（多账号时用来判断"这条消息是哪个号收到的"）
ROLE_CONNECTIONS = {}
scheduler = get_scheduler()

last_interaction = {}   # session_id -> 最后交互时间（含机器人主动发送）
last_user_activity = {} # session_id -> 用户最后发言时间（主动消息依据）
proactive_counts = {}     # "date|session_id" -> 当日主动消息次数
proactive_pending = {}  # session_id -> 计划发送时刻（绝对时间戳）
proactive_awaiting = set()           # 已发主动消息但用户还没回复的会话（回复前不再主动）
_spam_log = {}           # session_id -> [时间戳,...]
_role_emotions_cache = {}
_role_mimics_cache = {}
_proactive_state_date = ""                # 已落盘的日期，跨天时重置计数

last_proactive_sent = {}

_member_cache = {}

_SESSION_LOCKS = {}

# 桌面窗口句柄：pywebview 的"选择文件夹"对话框需要（run_webview_loop 中赋值）
_WEBVIEW_WINDOW_HOLDER = {"window": None}

# 运行中的 WebUI 服务实例：消息处理链路在别的函数里，靠这个拿到插件运行时
_WEBUI_SERVER_HOLDER = {"server": None}

# main() 里注册的「安装并退出」入口：WebUI 线程不能直接调窗口那边的局部函数
# （原 main.py 模块级常量；webview_runtime 的 clear_webview_cache_and_reload
#  也读它，收拢到这里避免反向 import main。方案第 18 节原定归 updater_tools，
# 迁移 updater_tools 时可再挪过去。）
_APP_HOOKS = {"install_update": None}

MAIN_EVENT_LOOP = None

# 当前处理链路该用哪份配置：接入方式绑了配置文件时，处理这条连接的消息期间
# 指向那份配置；没绑（或不在消息链路里，如后台线程）时为空，一律回落到主配置。
_ACTIVE_CONFIG = contextvars.ContextVar("lovomo_active_config", default=None)


def active_config():
    """当前处理链路该用的配置（可能是一份命名配置文件）。"""
    return _ACTIVE_CONFIG.get() or global_config


def set_active_config(config):
    """把配置装进当前上下文，返回用于还原的 token。"""
    return _ACTIVE_CONFIG.set(config)


def reset_active_config(token) -> None:
    if token is not None:
        _ACTIVE_CONFIG.reset(token)
