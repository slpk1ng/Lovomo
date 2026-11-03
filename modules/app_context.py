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
ROLE_CONNECTIONS = {}
scheduler = get_scheduler()

last_interaction = {}
last_user_activity = {}
proactive_counts = {}     # "date|session_id" -> 当日主动消息次数
proactive_pending = {}
proactive_awaiting = set()
_spam_log = {}           # session_id -> [时间戳,...]
_role_emotions_cache = {}
_role_mimics_cache = {}
_proactive_state_date = ""                # 已落盘的日期，跨天时重置计数

last_proactive_sent = {}

_member_cache = {}

_SESSION_LOCKS = {}

_WEBVIEW_WINDOW_HOLDER = {"window": None}

_WEBUI_SERVER_HOLDER = {"server": None}

# （原 main.py 模块级常量；webview_runtime 的 clear_webview_cache_and_reload
# 迁移 updater_tools 时可再挪过去。）
_APP_HOOKS = {"install_update": None}

MAIN_EVENT_LOOP = None

_ACTIVE_CONFIG = contextvars.ContextVar("lovomo_active_config", default=None)


def active_config():
    return _ACTIVE_CONFIG.get() or global_config


def set_active_config(config):
    return _ACTIVE_CONFIG.set(config)


def reset_active_config(token) -> None:
    if token is not None:
        _ACTIVE_CONFIG.reset(token)
