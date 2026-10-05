# -*- coding: utf-8 -*-
"""精简日志空行 + 语音批量勾选 回归测试。

覆盖：
  A 日志缓冲：一行输出只留一条记录，print 的分次写入不再凭空多出空行
  B 精简模式：被隐藏的行连同它占的换行一起去掉，不留下突兀空白
  C 语音批量：定时任务 / 节日问候 / 待办三处的批量条都能改「是否合成语音」
  D 冒烟回归：前端无悬空 DOM id、只改语音不清空目标、favicon 路由、
    事件保存保留 default 标记、空会话 ID 不落成空目标
  E app.log 大小上限：超限丢弃最早的记录、处理器按配置重建、配置项三处同步

运行: python tests/test_log_voice.py      （全通过退出码 0）
"""
import asyncio
import contextlib
import io
import json
import logging
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
sys.stdin = io.StringIO()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import main as M  # noqa: E402
from modules.database import DatabaseManager  # noqa: E402
from modules.events import EventManager  # noqa: E402
from modules.jobs import ScheduledJobManager  # noqa: E402
from modules.scheduler import SchedulerManager  # noqa: E402
from modules.todo_manager import TodoManager  # noqa: E402

PASS, FAIL = [], []
HTML = (ROOT / "webui" / "start.html").read_text(encoding="utf-8")


def check(name, fn):
    try:
        ok = bool(fn())
    except Exception as e:
        ok = False
        print(f"  !! {name} 抛异常: {type(e).__name__}: {e}")
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


# ---------------------------------------------------------------- A 日志缓冲


def _capture(fn):
    """把 print 收进真实缓冲，返回缓冲条目列表。"""
    buf = M.global_log_buffer
    with M.log_lock:
        buf.clear()
    original = sys.stdout
    sys.stdout = M.StdoutRedirector(original)
    try:
        fn()
    finally:
        sys.stdout = original
    return list(buf)


def _capture_bodies(fn):
    """同上，但去掉统一前缀，只比较正文。"""
    return [M._LOG_PREFIX_RE.sub("", line) for line in _capture(fn)]


def a0():
    """每一行都带上 [时:分:秒.毫秒][来源][级别] 前缀。"""
    lines = _capture(lambda: print("正常日志A"))
    return (len(lines) == 1
            and re.match(r"^\[\d{2}:\d{2}:\d{2}\.\d{3}\]\[Lovomo\]\[INFO\] 正常日志A$",
                         lines[0]) is not None)


def a1():
    return _capture_bodies(lambda: print("正常日志A")) == ["正常日志A"]


def a2():
    def run():
        print("A")
        print("B")
        print("C")
    return _capture_bodies(run) == ["A", "B", "C"]


def a3():
    """print 实际是「先写正文、再单独写换行」，不能切成两条记录。"""
    def run():
        sys.stdout.write("hello")
        sys.stdout.write("\n")
    return _capture_bodies(run) == ["hello"]


def a4():
    return _capture_bodies(lambda: sys.stdout.write("win\r\nline\r\n")) == ["win", "line"]


def a5():
    def run():
        sys.stdout.write("abc\r")
        sys.stdout.write("\n")
    return _capture_bodies(run) == ["abc"]


def a6():
    """真正空的 print() 仍然保留成一个空行。"""
    def run():
        print("x")
        print()
        print("y")
    return _capture_bodies(run) == ["x", "", "y"]


def a7():
    return _capture_bodies(lambda: print("    缩进内容")) == ["    缩进内容"]


def a8():
    def run():
        sys.stdout.write("part1")
        sys.stdout.write("part2\n")
    return _capture_bodies(run) == ["part1part2"]


def a9():
    long_line = "z" * (M._LOG_PENDING_MAX_CHARS + 10)
    return _capture_bodies(lambda: sys.stdout.write(long_line)) == [long_line]


# ---------------------------------------------------------------- B 精简模式


class _Req:
    def __init__(self, query=None):
        self.query = query or {}


def _logs_response(query=None):
    srv = object.__new__(M.WebUIServer)
    resp = asyncio.run(srv.handle_get_logs(_Req(query)))
    return json.loads(resp.body.decode())


def _with_buffer(lines, patterns, fn):
    saved_cfg = M.global_config
    M.global_config = {"webui_log_hide_patterns": patterns,
                       "webui_log_tail_lines": 500}
    M.app_context.global_config = M.global_config
    try:
        with M.log_lock:
            M.global_log_buffer[:] = list(lines)
        return fn()
    finally:
        with M.log_lock:
            M.global_log_buffer[:] = []
        M.global_config = saved_cfg
        M.app_context.global_config = M.global_config


def b1():
    """精简模式剔除命中隐藏片段的整行。"""
    def run():
        return _logs_response()["logs"]
    text = _with_buffer(["保留1", "【隐藏】噪音", "保留2"], "【隐藏】", run)
    return text == "保留1\n保留2"


def b2():
    """隐藏行占的那一行换行要一起去掉，不留空行。"""
    def run():
        return _logs_response()["logs"]
    text = _with_buffer(["保留1", "【隐藏】噪音", "", "保留2"], "【隐藏】", run)
    return "\n\n" not in text and text == "保留1\n保留2"


def b3():
    """完整模式一行都不藏。"""
    def run():
        return _logs_response({"full": "1"})["logs"]
    text = _with_buffer(["保留1", "【隐藏】噪音"], "【隐藏】", run)
    return text == "保留1\n【隐藏】噪音"


def b4():
    """完整模式标记与总数照常回传。"""
    def run():
        return _logs_response({"full": "1"})
    d = _with_buffer(["a", "b", "c"], "【隐藏】", run)
    return d["full"] is True and d["total_lines"] == 3


def b5():
    """没配隐藏片段时精简模式原样返回。"""
    def run():
        return _logs_response()["logs"]
    text = _with_buffer(["a", "b"], "", run)
    return text == "a\nb"


# ---------------------------------------------------------------- C 语音批量


def c1():
    return all(f'id="{p}-batch-voice"' in HTML for p in ("job", "event", "todo"))


def c2():
    return HTML.count("<th>语音</th>") == 3


def c3():
    return "voiceId: prefix + '-batch-voice'" in HTML


def c4():
    return "payload.use_voice = voiceRaw === '1'" in HTML


def c5():
    """真调 handle_jobs_batch 改语音。"""
    tmp = Path(tempfile.mkdtemp(prefix="lv_jobs_"))
    mgr = ScheduledJobManager({"scheduler_enabled": True}, tmp, SchedulerManager())
    mgr.jobs = [
        {"id": "a", "name": "A", "enabled": True, "trigger": {"type": "daily", "time": "08:00"},
         "target": {"session_type": "private", "session_id": "1"},
         "action": {"mode": "template", "template": "早", "use_voice": False}},
        {"id": "b", "name": "B", "enabled": True, "trigger": {"type": "daily", "time": "09:00"},
         "target": {"session_type": "private", "session_id": "2"},
         "action": {"mode": "template", "template": "午", "use_voice": False}},
    ]
    saved = M.job_mgr
    M.job_mgr = mgr
    M.app_context.job_mgr = M.job_mgr
    try:
        class Req:
            def __init__(self, payload):
                self._p = payload

            async def json(self):
                return self._p

        class Srv:
            handle_jobs_batch = M.WebUIServer.handle_jobs_batch

        srv = Srv()

        async def run():
            r = await srv.handle_jobs_batch(Req({"ids": ["a"], "use_voice": True}))
            d = json.loads(r.body.decode())
            if d.get("changed") != 1 or not mgr.jobs[0]["action"]["use_voice"]:
                return False
            if mgr.jobs[1]["action"]["use_voice"]:
                return False
            r = await srv.handle_jobs_batch(Req({"use_voice": False}))
            if json.loads(r.body.decode()).get("changed") != 2:
                return False
            if any(j["action"]["use_voice"] for j in mgr.jobs):
                return False
            disk = json.loads((tmp / "scheduled_jobs.json").read_text(encoding="utf-8"))
            return all(j["action"]["use_voice"] is False for j in disk)

        return asyncio.run(run())
    finally:
        M.job_mgr = saved
        M.app_context.job_mgr = M.job_mgr


def c6():
    """真调 handle_events_batch 改语音。"""
    tmp = Path(tempfile.mkdtemp(prefix="lv_events_"))
    mgr = EventManager({"default_events_enabled": False}, tmp)
    mgr.events = [
        {"id": "e1", "name": "元旦", "date": "01-01", "enabled": True,
         "mode": "template", "template": "快乐", "use_voice": False,
         "targets": [{"session_type": "private", "session_id": "1"}]},
        {"id": "e2", "name": "中秋", "date": "08-15", "enabled": True,
         "mode": "template", "template": "快乐", "use_voice": False, "targets": []},
    ]
    saved = M.event_mgr
    M.event_mgr = mgr
    M.app_context.event_mgr = M.event_mgr
    try:
        class Req:
            def __init__(self, payload):
                self._p = payload

            async def json(self):
                return self._p

        class Srv:
            handle_events_batch = M.WebUIServer.handle_events_batch

        srv = Srv()

        async def run():
            r = await srv.handle_events_batch(Req({"ids": ["e1"], "use_voice": True}))
            if json.loads(r.body.decode()).get("changed") != 1:
                return False
            if not mgr.events[0]["use_voice"] or mgr.events[1]["use_voice"]:
                return False
            await srv.handle_events_batch(Req({"use_voice": False}))
            if any(e["use_voice"] for e in mgr.events):
                return False
            disk = json.loads((tmp / "events.json").read_text(encoding="utf-8"))
            return all(e["use_voice"] is False for e in disk)

        return asyncio.run(run())
    finally:
        M.event_mgr = saved
        M.app_context.event_mgr = M.event_mgr


def c7():
    """真调 handle_todos_batch 改语音。"""
    tmp = Path(tempfile.mkdtemp(prefix="lv_todos_"))
    db = DatabaseManager(tmp)
    mgr = TodoManager({"todo_voice": False}, db, SchedulerManager())
    ids = [mgr.add_todo("吃药", 4102444800, "private", "1")["id"],
           mgr.add_todo("开会", 4102444800, "private", "2")["id"]]
    saved = M.todo_mgr
    M.todo_mgr = mgr
    M.app_context.todo_mgr = M.todo_mgr
    try:
        class Req:
            def __init__(self, payload):
                self._p = payload

            async def json(self):
                return self._p

        class Srv:
            handle_todos_batch = M.WebUIServer.handle_todos_batch

        srv = Srv()

        async def run():
            r = await srv.handle_todos_batch(Req({"ids": [ids[0]], "use_voice": True}))
            if json.loads(r.body.decode()).get("changed") != 1:
                return False
            rows = {t["id"]: t for t in mgr.list_todos()}
            if rows[ids[0]]["use_voice"] != 1 or rows[ids[1]]["use_voice"] is not None:
                return False
            await srv.handle_todos_batch(Req({"use_voice": False}))
            rows = {t["id"]: t for t in mgr.list_todos()}
            return all(t["use_voice"] == 0 for t in rows.values())

        return asyncio.run(run())
    finally:
        M.todo_mgr = saved
        M.app_context.todo_mgr = M.todo_mgr


def c8():
    """真调 handle_todos_add 落 per-todo 语音。"""
    tmp = Path(tempfile.mkdtemp(prefix="lv_add_"))
    db = DatabaseManager(tmp)
    mgr = TodoManager({"todo_voice": False}, db, SchedulerManager())
    saved = M.todo_mgr
    M.todo_mgr = mgr
    M.app_context.todo_mgr = M.todo_mgr
    try:
        class Req:
            def __init__(self, payload):
                self._p = payload

            async def json(self):
                return self._p

        class Srv:
            handle_todos_add = M.WebUIServer.handle_todos_add

        srv = Srv()

        async def run():
            r = await srv.handle_todos_add(Req({"content": "喝水",
                                                "remind_time": "2030-01-01 08:00",
                                                "session_type": "private",
                                                "session_id": "1", "use_voice": True}))
            if not json.loads(r.body.decode()).get("success"):
                return False
            rows = mgr.list_todos()
            if rows[0]["use_voice"] != 1:
                return False
            r = await srv.handle_todos_add(Req({"content": "散步",
                                                "remind_time": "2030-01-01 09:00",
                                                "session_type": "private",
                                                "session_id": "1"}))
            rows = {t["content"]: t for t in mgr.list_todos()}
            return json.loads(r.body.decode()).get("success") \
                and rows["散步"]["use_voice"] is None

        return asyncio.run(run())
    finally:
        M.todo_mgr = saved
        M.app_context.todo_mgr = M.todo_mgr


def c9():
    """待办表补出 use_voice 列（老库也能升上来）。"""
    db = DatabaseManager(Path(tempfile.mkdtemp(prefix="lv_col_")))
    cols = {r["name"] for r in db.query_all("PRAGMA table_info(todos)")}
    return "use_voice" in cols


def c10():
    """per-todo 语音优先，没设过才回落到配置项。"""
    tmp = Path(tempfile.mkdtemp(prefix="lv_fire_"))
    db = DatabaseManager(tmp)
    box = []

    class Sender:
        client = object()

        @contextlib.contextmanager
        def for_session(self, session_id):
            self.last_session = session_id
            yield
        
        async def speak_and_send(self, session_type, target, text, emotions, ctx,
                                 use_voice=False, sticker=False, emotion="", session_id=""):
            box.append(use_voice)
            return True

    mgr = TodoManager({"todo_voice": True, "todo_remind_mode": "preset",
                       "todo_remind_template": "{content}"},
                      db, SchedulerManager(), Sender())
    mgr.ctx_provider = lambda: None
    mgr.emotions_provider = lambda: {}
    on = mgr.add_todo("吃药", 4102444800, "private", "1", use_voice=True)
    off = mgr.add_todo("开会", 4102444800, "private", "1", use_voice=False)
    auto = mgr.add_todo("散步", 4102444800, "private", "1")
    asyncio.run(mgr.fire_reminder(on["id"], "吃药", "private", "1", use_voice=True))
    asyncio.run(mgr.fire_reminder(off["id"], "开会", "private", "1", use_voice=False))
    asyncio.run(mgr.fire_reminder(auto["id"], "散步", "private", "1"))
    return box == [True, False, True]


# ---------------- D 真实冒烟抓到的两个坑 ----------------
def d1():
    """前端引用的 DOM id 必须真的存在（只允许运行时新建的那几个）。"""
    import re
    dynamic = {"plugin-theme", "toast-wrap", "chart-tip"}
    refs = set(re.findall(r"getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)", HTML))
    ids = set(re.findall(r"id=['\"]([^'\"]+)['\"]", HTML))
    return not (refs - ids - dynamic)


def d2():
    """只改语音时不能把发送目标一起清掉。"""
    i = HTML.index("if (stype) payload.session_type = stype;")
    return "if (stype || sid || !voiceRaw) payload.session_id = sid;" in HTML[i:i + 400]


def d3():
    """favicon 路由已注册，浏览器打开页面不再刷 404。"""
    # WebUI 路由注册已搬迁至 modules/webui_server.py
    return 'add_get("/favicon.ico"' in (ROOT / "modules" / "webui_server.py").read_text(encoding="utf-8")


def d4():
    """节日批量：只改语音不动发送目标；清空会话 ID 要真的回到「发给全部会话」。"""
    tmp = Path(tempfile.mkdtemp(prefix="lv_evb_"))
    mgr = EventManager({"default_events_enabled": False}, tmp)
    mgr.events = [
        {"id": "e1", "name": "元旦", "date": "01-01", "enabled": True,
         "mode": "template", "template": "快乐", "use_voice": False, "targets": []},
        {"id": "e2", "name": "中秋", "date": "08-15", "enabled": True,
         "mode": "template", "template": "快乐", "use_voice": False,
         "targets": [{"session_type": "private", "session_id": "123"}]},
    ]
    saved = M.event_mgr
    M.event_mgr = mgr
    M.app_context.event_mgr = M.event_mgr
    try:
        class Req:
            def __init__(self, payload):
                self._p = payload

            async def json(self):
                return self._p

        class Srv:
            handle_events_batch = M.WebUIServer.handle_events_batch

        srv = Srv()

        async def run():
            # 只改语音：没有目标的事件不能被补上一个空目标
            await srv.handle_events_batch(Req({"ids": ["e1"], "use_voice": True}))
            if mgr.events[0]["targets"] or not mgr.events[0]["use_voice"]:
                return False
            # 清空会话 ID：目标整体清掉，回到「发给全部会话」
            await srv.handle_events_batch(Req({"ids": ["e2"], "session_id": ""}))
            if mgr.events[1]["targets"]:
                return False
            # 填了会话 ID：照常落进去
            await srv.handle_events_batch(Req({"ids": ["e2"], "session_type": "group",
                                               "session_id": "999"}))
            tgs = mgr.events[1]["targets"]
            return len(tgs) == 1 and tgs[0]["session_id"] == "999" \
                and tgs[0]["session_type"] == "group"

        return asyncio.run(run())
    finally:
        M.event_mgr = saved
        M.app_context.event_mgr = M.event_mgr


def d5():
    """保存事件不能丢掉内置节日的 default 标记（丢了问候就不再发给全部会话）。"""
    tmp = Path(tempfile.mkdtemp(prefix="lv_evs_"))
    mgr = EventManager({"default_events_enabled": False}, tmp)
    mgr.events = [
        {"id": "e1", "name": "元旦", "date": "01-01", "enabled": True, "mode": "template",
         "template": "快乐", "use_voice": False, "targets": [], "default": True},
    ]
    saved = M.event_mgr
    M.event_mgr = mgr
    try:
        class Req:
            def __init__(self, payload):
                self._p = payload

            async def json(self):
                return self._p

        class Srv:
            handle_events_save = M.WebUIServer.handle_events_save

        srv = Srv()

        async def run():
            # 请求里带 default：原样保留
            await srv.handle_events_save(Req({"events": [
                {"id": "e1", "name": "元旦", "date": "01-01", "default": True, "targets": []}]}))
            if not mgr.events[0].get("default"):
                return False
            # 请求里没带 default（缓存里的旧页面）：沿用原来那份的值
            await srv.handle_events_save(Req({"events": [
                {"id": "e1", "name": "元旦", "date": "01-01", "targets": []}]}))
            if not mgr.events[0].get("default"):
                return False
            # 新建的事件没有这个标记，不能被凭空补上
            await srv.handle_events_save(Req({"events": [
                {"id": "e9", "name": "自定义", "date": "08-15", "targets": []}]}))
            return not any(e.get("default") for e in mgr.events if e["id"] == "e9")

        return asyncio.run(run())
    finally:
        M.event_mgr = saved


def d6():
    """节日编辑器保存时要带上原事件的其它字段（否则 default 标记被表单抹掉）。"""
    i = HTML.index("const evNew = {")
    return "...ev," in HTML[i:i + 60]


def d7():
    """会话 ID 留空的事件不能被存成「空目标占位」——它会顶掉默认节日的兜底。"""
    tmp = Path(tempfile.mkdtemp(prefix="lv_evt_"))
    mgr = EventManager({"default_events_enabled": False}, tmp)
    mgr.events = []
    saved = M.event_mgr
    M.event_mgr = mgr
    M.app_context.event_mgr = M.event_mgr
    try:
        class Req:
            def __init__(self, payload):
                self._p = payload

            async def json(self):
                return self._p

        class Srv:
            handle_events_save = M.WebUIServer.handle_events_save

        srv = Srv()

        async def run():
            # 前端编辑器永远塞一个 targets 元素，会话 ID 没填就是空串
            await srv.handle_events_save(Req({"events": [
                {"id": "e1", "name": "元旦", "date": "01-01", "default": True,
                 "targets": [{"session_type": "private", "session_id": ""}]}]}))
            if mgr.events[0]["targets"]:
                return False
            # 填了 ID 的照常留下，空的那个被剔除
            await srv.handle_events_save(Req({"events": [
                {"id": "e1", "name": "元旦", "date": "01-01", "default": True,
                 "targets": [{"session_type": "private", "session_id": "123"},
                             {"session_type": "group", "session_id": ""}]}]}))
            tgs = mgr.events[0]["targets"]
            return len(tgs) == 1 and tgs[0]["session_id"] == "123"

        return asyncio.run(run())
    finally:
        M.event_mgr = saved
        M.app_context.event_mgr = M.event_mgr


def _cap_log(path: Path, max_bytes: int, lines: int) -> str:
    handler = M._CappedFileHandler(path, max_bytes)
    handler.setFormatter(M._LogFormatter())
    logger = logging.getLogger(f"lv_cap_{path.name}_{max_bytes}")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        for i in range(lines):
            logger.info("第 %d 行，填充内容让文件长大 %s", i, "x" * 80)
        handler.flush()
    finally:
        logger.removeHandler(handler)
        handler.close()
    return path.read_text(encoding="utf-8")


def e1():
    """超过上限时丢弃文件里最早的记录。"""
    tmp = Path(tempfile.mkdtemp(prefix="lv_cap_")) / "app.log"
    text = _cap_log(tmp, 20 * 1024, 400)
    return tmp.stat().st_size <= 20 * 1024 and "第 0 行" not in text


def e2():
    """保留最新的记录，且裁剪处不留半截行。"""
    tmp = Path(tempfile.mkdtemp(prefix="lv_cap_")) / "app.log"
    text = _cap_log(tmp, 20 * 1024, 400)
    first = text.splitlines()[0]
    return ("第 399 行" in text and re.match(r"^\[\d{2}:\d{2}:\d{2}\.\d{3}\]"
                                             r"\[Lovomo\]\[INFO\] ", first))


def e3():
    """按配置重建 app.log 处理器，旧的同类处理器不残留。"""
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        M.apply_log_max_size({"log_max_size_mb": 7})
        capped = [h for h in root.handlers if isinstance(h, M._CappedFileHandler)]
        if len(capped) != 1 or capped[0].max_bytes != 7 * 1024 * 1024:
            return False
        M.apply_log_max_size({"log_max_size_mb": 7})
        return len([h for h in root.handlers
                    if isinstance(h, M._CappedFileHandler)]) == 1
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved:
            root.addHandler(h)


def e4():
    """非法或过小的上限夹到合法值。"""
    root = logging.getLogger()
    saved = list(root.handlers)
    try:
        M.apply_log_max_size({"log_max_size_mb": "abc"})
        h = [x for x in root.handlers if isinstance(x, M._CappedFileHandler)][0]
        if h.max_bytes != M.LOG_MAX_SIZE_MB_DEFAULT * 1024 * 1024:
            return False
        M.apply_log_max_size({"log_max_size_mb": 0})
        h = [x for x in root.handlers if isinstance(x, M._CappedFileHandler)][0]
        return h.max_bytes == 1024 * 1024
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved:
            root.addHandler(h)


def e5():
    """配置项三处同步：default_config + configGroups + configMeta。"""
    if M.ConfigLoader.default_config().get("log_max_size_mb") != 5:
        return False
    group = "{ title: 'WebUI 设置'"
    if group not in HTML:
        return False
    # 只看这一组自己的 keys 行，别依赖它在列表里的位置（后面加键就会挪）
    keys_line = HTML[HTML.index(group):].split("\n", 1)[0]
    return "'log_max_size_mb'" in keys_line and "'log_max_size_mb': {" in HTML


# ---------------------------------------------------------------- F 统一前缀


def _prefix_of(line):
    m = M._LOG_PREFIX_RE.match(line)
    return m.group(0) if m else ""


def f1():
    """前缀格式：[时:分:秒.毫秒][来源][级别]。"""
    line = M.console_log_line("正在连接 NapCat：ws://127.0.0.1:3001")
    return (re.match(r"^\[\d{2}:\d{2}:\d{2}\.\d{3}\]\[Lovomo\]\[INFO\] 正在连接",
                     line) is not None)


def f2():
    """正文里出现报错字样的行判成错误，含跳过/降级的判成警告。"""
    err = M.console_log_line("回复审判调用失败: RuntimeError: boom")
    warn = M.console_log_line(
        "[参考音频根目录] 跳过目录 Modle：里面没有可用的音频文件（支持 .flac、.mp3）")
    return ("[Lovomo][ERROR] 回复审判调用失败" in err
            and "[Lovomo][WARN] [参考音频根目录] 跳过目录" in warn)


def f3():
    """冒号后面的自由文本不参与级别判定。"""
    line = M.console_log_line("已更新会话 group_123 的当前话题：角色拒绝用户2，改为聊别的")
    return "[Lovomo][INFO]" in line and "拒绝用户2" in line


def f4():
    """行首的 [警告]/[窗口] 并入前缀，[插件] 决定来源栏。"""
    warn = M.console_log_line("[警告] 未找到任何情绪配置（请检查 ref_audio_root 目录）")
    win = M.console_log_line("[窗口] 手动清理界面缓存：释放 12.3 MB")
    plug = M.console_log_line("[插件] abc 加载失败: Traceback")
    other = M.console_log_line("[插件市场] 已安装：X (id=y)")
    return ("[Lovomo][WARN] 未找到任何情绪配置" in warn
            and "[Lovomo][INFO] 手动清理界面缓存" in win
            and "[插件][ERROR] abc 加载失败" in plug
            and "[Lovomo][INFO] [插件市场] 已安装" in other)


def f5():
    """插件名里的冒号不算分隔符：后面那句「入口执行失败」要判得出来。"""
    line = M.console_log_line("[插件:喵开关] 入口执行失败:")
    return "[插件][ERROR] [插件:喵开关] 入口执行失败:" in line


def f6():
    """启动横幅（字符画）不加前缀，也不重复加。"""
    art = "                   ⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⣿⡿⠟⣛⣩⣤⣶⣾⣿⣿⣿⣿⣿⣿"
    once = M.console_log_line("普通日志")
    return (M.console_log_line(art) == art
            and M.console_log_line(once) == once)


def f6b():
    """多行报错的续行跟着上一行标级别，别只错第一行。"""
    head = M.console_log_line("工具流程执行失败: RuntimeError: boom")
    frame = M.console_log_line('  File "main.py", line 3, in f')
    code = M.console_log_line("    raise RuntimeError('x')")
    tail = M.console_log_line("RuntimeError: x")
    normal = M.console_log_line("普通信息一行")
    indented = M.console_log_line("  缩进的普通输出")
    return ("[Lovomo][ERROR] 工具流程执行失败" in head
            and "[Lovomo][ERROR]   File" in frame
            and "[Lovomo][ERROR]     raise" in code
            and "[Lovomo][ERROR] RuntimeError" in tail
            and "[Lovomo][INFO] 普通信息一行" in normal
            and "[Lovomo][INFO]   缩进的普通输出" in indented)


def f6c():
    """异常出现在冒号后面（HTTPStatusError: …）也算出错；引用文本里的词不算。"""
    first = M.console_log_line(
        "[插件市场] 拿不到 owner/repo 的 Release：HTTPStatusError: "
        "Client error '403 FORBIDDEN' for url 'https://example/api'")
    cont = M.console_log_line(
        "For more information check: https://developer.mozilla.org/docs/Web/HTTP/Status/403")
    quoted = M.console_log_line("收到私聊 [10001] 来自 [10001]: 我遇到 Error 了")
    topic = M.console_log_line("已更新会话 group_1 的当前话题：角色拒绝用户2，改为聊别的")
    return ("[Lovomo][ERROR] [插件市场] 拿不到" in first
            and "[Lovomo][ERROR] For more information" in cont
            and "[Lovomo][INFO] 收到私聊" in quoted
            and "[Lovomo][INFO] 已更新会话" in topic)


def f7():
    """app.log 的每一行也是同一套前缀。"""
    import io as _io
    handler = logging.StreamHandler(_io.StringIO())
    handler.setFormatter(M._LogFormatter())
    logger = logging.getLogger("lv_prefix_fmt")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        logger.warning("配置里缺少 X")
        text = handler.stream.getvalue().strip()
    finally:
        logger.removeHandler(handler)
    return re.match(r"^\[\d{2}:\d{2}:\d{2}\.\d{3}\]\[Lovomo\]\[WARN\] 配置里缺少 X$",
                    text) is not None


def f8():
    """窗口生命周期事件不再自带 [窗口] 前缀，且三条提示已去掉。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    # log_console 搬迁后 def 落在 modules/log_console.py，main.py 经 import 再导出
    mod = (ROOT / "modules" / "log_console.py").read_text(encoding="utf-8")
    for gone in ("已释放 WebView2，界面进程退出",
                 "界面已重建", "已启用任务栏唤醒置顶"):
        if gone in src or gone in mod:
            return False
    if 'text = f"[窗口] {message}"' in src or 'text = f"[窗口] {message}"' in mod:
        return False
    i = mod.find("def log_window_event(")
    return i > 0 and "print(message)" in mod[i:i + 400]


def main():
    print("=" * 70)
    print("精简日志空行 + 语音批量勾选")
    print("=" * 70)
    print("\nA 日志缓冲：一行输出 = 一条记录")
    for n, f in [("统一前缀格式", a0), ("单行 print 一条记录", a1), ("连续 print 无空行", a2),
                 ("分次写入不产生空行", a3), ("CRLF 去掉 \\r", a4),
                 ("跨写 CRLF", a5), ("显式空行保留", a6), ("行首缩进保留", a7),
                 ("半行拼接", a8), ("超长半行强制落盘", a9)]:
        check(n, f)
    print("\nB 精简模式不留下空白")
    for n, f in [("隐藏行被剔除", b1), ("隐藏行不留空行", b2), ("完整模式不隐藏", b3),
                 ("完整模式标记与总数", b4), ("无隐藏片段时原样返回", b5)]:
        check(n, f)
    print("\nC 语音批量勾选")
    for n, f in [("三个面板都有语音批量条", c1), ("三张表都有语音列", c2),
                 ("批量条传 voiceId", c3), ("批量提交 use_voice", c4),
                 ("定时任务接口真改语音", c5), ("节日问候接口真改语音", c6),
                 ("待办接口真改语音", c7), ("新增待办可指定语音", c8),
                 ("待办表有 use_voice 列", c9), ("per-todo 语音优先于配置", c10)]:
        check(n, f)
    print("\nD 真实冒烟回归")
    for n, f in [("前端无悬空 DOM id", d1), ("只改语音不清空目标", d2),
                 ("favicon 路由已注册", d3), ("节日批量只改语音不动目标", d4),
                 ("保存事件保留 default 标记", d5), ("编辑器带全原事件字段", d6),
                 ("空会话 ID 不落成空目标", d7)]:
        check(n, f)

    print("\nE app.log 大小上限")
    for n, f in [("超限丢弃最早的记录", e1), ("保留最新记录且不留半截行", e2),
                 ("按配置重建处理器不残留", e3), ("非法上限夹到合法值", e4),
                 ("配置项三处同步", e5)]:
        check(n, f)

    print("\nF 统一日志前缀")
    for n, f in [("前缀格式", f1), ("错误/警告判定", f2), ("只看冒号前的正文", f3),
                 ("旧标记并入前缀", f4), ("方括号里的冒号不算分隔", f5),
                 ("横幅不加前缀", f6), ("多行报错的续行跟着标错", f6b),
                 ("冒号后的异常名也算出错", f6c),
                 ("app.log 同一套前缀", f7),
                 ("窗口事件去掉 [窗口] 与三条提示", f8)]:
        check(n, f)

    print("\n" + "=" * 70)
    print(f"结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        for n in FAIL:
            print(f"  - {n}")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
