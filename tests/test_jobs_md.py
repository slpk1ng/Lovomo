# -*- coding: utf-8 -*-
"""Markdown 说明渲染 + 定时任务发送目标回归测试。

覆盖：
  A 前端有 Markdown 渲染器，插件说明不再以纯文本（一堆 # 和 `）呈现
  B 渲染器对插件包这种不可信内容做了转义与 URL 过滤
  C 定时任务会话 ID 留空 → 发给所有有历史记录的会话
  D 类型填错（群号填进私聊）→ 按已知会话纠正，不再撞 NapCat 1200
  E 单个目标发送失败不会冒成任务异常
  F 批量修改目标接口

运行: python tests/test_jobs_md.py      （全通过退出码 0）
"""
import asyncio
import contextlib
import io
import json
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

from modules.jobs import ScheduledJobManager  # noqa: E402
from modules.scheduler import SchedulerManager  # noqa: E402

PASS, FAIL = [], []


def check(name, fn):
    try:
        ok = bool(fn())
    except Exception as e:
        ok = False
        print(f"  !! {name} 抛异常: {type(e).__name__}: {e}")
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


HTML = (ROOT / "webui" / "start.html").read_text(encoding="utf-8")

# ---------------------------------------------------------------- A Markdown


def a1():
    return "function renderMarkdown(" in HTML


def a2():
    """插件说明弹窗必须走渲染，不能再 textContent 塞原文。"""
    return "body.innerHTML = renderMarkdown(" in HTML


def a3():
    return "setMarkdown(docEl," in HTML


def a4():
    """不能还有把 md 原文直接赋给 textContent 的地方。"""
    return not re.search(r"body\.textContent\s*=\s*\([^)]*text", HTML)


def a5():
    return ".md-body" in HTML and ".md-code" in HTML and ".md-list" in HTML


def a6():
    """标题/行内代码/列表/表格都要能出标签。"""
    for pat in (r"md-h", r"<code>", r"md-list", r"<table>"):
        if pat not in HTML:
            return False
    return True


# ---------------------------------------------------------------- B 安全


def b1():
    """先转义再变换：渲染函数里必须先调 esc 再拼标签。"""
    i_esc = HTML.find("function esc(")
    i_md = HTML.find("function renderMarkdown(")
    return 0 <= i_esc < i_md


def b2():
    """行内渲染必须对内容先 esc 再拼标签。"""
    m = re.search(r"function mdInline\(s\)\s*\{(.*?)\n        \}", HTML, re.S)
    if not m:
        return False
    body = m.group(1)
    i_esc = body.find("esc(")
    i_tag = body.find("<strong>")
    return 0 <= i_esc < i_tag


def b3():
    """链接协议必须过滤，javascript: 不能进 href/src。"""
    return "function safeUrl(" in HTML and "javascript:" not in HTML.split("function safeUrl(")[1][:400].replace("javascript:", "", 1)


def b4():
    """safeUrl 只允许 http/https/mailto/锚点/相对路径。"""
    m = re.search(r"function safeUrl\(u\)\s*\{(.*?)\n        \}", HTML, re.S)
    if not m:
        return False
    return "https?:" in m.group(1) and "mailto:" in m.group(1)


# ---------------------------------------------------------------- C/D/E 目标解析


class FakeSender:
    def __init__(self, fail_for=()):
        self.client = object()
        self.calls = []
        self.fail_for = set(fail_for)

    @contextlib.contextmanager
    def for_session(self, session_id):
        self.last_session = session_id
        yield
    
    async def speak_and_send(self, session_type, target_id, text, emotions, ctx,
                             use_voice=False, sticker=False, session_id=""):
        self.calls.append((session_type, str(target_id), text))
        if (session_type, str(target_id)) in self.fail_for:
            raise RuntimeError("NapCatAPIError: API call failed: retcode 1200")
        return True


SESSIONS = [
    {"session_type": "private", "session_id": "10001"},
    {"session_type": "private", "session_id": "10002"},
    {"session_type": "group", "session_id": "555000"},
]


def _mgr(sender=None, sessions=None):
    tmp = tempfile.mkdtemp(prefix="jobsmd_")
    cfg = {"scheduler_enabled": True}
    mgr = ScheduledJobManager(cfg, Path(tmp), SchedulerManager(),
                              sender=sender or FakeSender(),
                              sessions_provider=lambda: sessions if sessions is not None else SESSIONS)
    return mgr


def c1():
    """ID 留空 → 解析成全部已知会话。"""
    mgr = _mgr()
    got = mgr.resolve_targets({"session_type": "private", "session_id": ""})
    return sorted(got) == [("group", "555000"), ("private", "10001"), ("private", "10002")]


def c2():
    """留空时真的给每个会话都发了一遍。"""
    sender = FakeSender()
    mgr = _mgr(sender)
    job = {"id": "j1", "name": "早安", "enabled": True,
           "trigger": {"type": "daily", "time": "08:00"},
           "target": {"session_type": "private", "session_id": ""},
           "action": {"mode": "template", "template": "早上好"}}
    asyncio.run(mgr._run_job(job))
    return len(sender.calls) == 3 and all(c[2] == "早上好" for c in sender.calls)


def c3():
    """没有历史记录时留空 = 没有目标，且不能炸。"""
    mgr = _mgr(sessions=[])
    return mgr.resolve_targets({"session_type": "private", "session_id": ""}) == []


def d1():
    """群号填进私聊 → 纠正成群聊。"""
    mgr = _mgr()
    return mgr.resolve_targets({"session_type": "private", "session_id": "555000"}) == [("group", "555000")]


def d2():
    """私聊号填进群聊 → 纠正成私聊。"""
    mgr = _mgr()
    return mgr.resolve_targets({"session_type": "group", "session_id": "10001"}) == [("private", "10001")]


def d3():
    """类型本来就是对的就不动。"""
    mgr = _mgr()
    return mgr.resolve_targets({"session_type": "private", "session_id": "10001"}) == [("private", "10001")]


def d4():
    """完整写法 private_10001 / group_555000_xxx 也要认。"""
    mgr = _mgr()
    ok1 = mgr.resolve_targets({"session_type": "group", "session_id": "group_555000_777"}) == [("group", "555000")]
    ok2 = mgr.resolve_targets({"session_type": "group", "session_id": "private_10002"}) == [("private", "10002")]
    return ok1 and ok2


def d5():
    """未知号码按原样发（不强改类型）。"""
    mgr = _mgr()
    return mgr.resolve_targets({"session_type": "private", "session_id": "99999"}) == [("private", "99999")]


def e1():
    """单个目标发送失败不能把异常冒出去。"""
    sender = FakeSender(fail_for=[("private", "10001")])
    mgr = _mgr(sender)
    job = {"id": "j2", "name": "x", "enabled": True,
           "trigger": {"type": "daily", "time": "08:00"},
           "target": {"session_type": "private", "session_id": ""},
           "action": {"mode": "template", "template": "hi"}}
    try:
        asyncio.run(mgr._run_job(job))
    except Exception:
        return False
    return len(sender.calls) == 3


def e2():
    """全部目标都失败时不标记已执行（当天还能重试）。"""
    sender = FakeSender(fail_for=[("private", "10001"), ("private", "10002"), ("group", "555000")])
    mgr = _mgr(sender)
    job = {"id": "j3", "name": "x", "enabled": True,
           "trigger": {"type": "daily", "time": "08:00"},
           "target": {"session_type": "private", "session_id": ""},
           "action": {"mode": "template", "template": "hi"}}
    asyncio.run(mgr._run_job(job))
    return not mgr._ran_on("j3", __import__("time").strftime("%Y-%m-%d"))


def e3():
    """部分成功也算跑过，且不会重复广播失败项。"""
    sender = FakeSender(fail_for=[("private", "10001")])
    mgr = _mgr(sender)
    job = {"id": "j4", "name": "x", "enabled": True,
           "trigger": {"type": "daily", "time": "08:00"},
           "target": {"session_type": "private", "session_id": ""},
           "action": {"mode": "template", "template": "hi"}}
    asyncio.run(mgr._run_job(job))
    return mgr._ran_on("j4", __import__("time").strftime("%Y-%m-%d"))


def e4():
    """sender 未连接时直接返回，不发送。"""
    class NoClient:
        client = None
        calls = []
    mgr = _mgr(NoClient())
    job = {"id": "j5", "name": "x", "enabled": True,
           "trigger": {"type": "daily", "time": "08:00"},
           "target": {"session_type": "private", "session_id": ""},
           "action": {"mode": "template", "template": "hi"}}
    asyncio.run(mgr._run_job(job))
    return NoClient.calls == []


# ---------------------------------------------------------------- F 批量接口


def _backend_src():
    """批量接口的 WebUI 端已拆到 modules/webui_server.py，静态扫描需同时覆盖。"""
    return ((ROOT / "main.py").read_text(encoding="utf-8")
            + (ROOT / "modules" / "webui_server.py").read_text(encoding="utf-8"))


def f1():
    src = _backend_src()
    return "async def handle_jobs_batch(" in src


def f2():
    src = _backend_src()
    return 'r.add_post("/api/jobs/batch"' in src


def f3():
    """会话类型要过白名单，不能任意值落库。"""
    src = _backend_src()
    return 'session_type not in ("private", "group")' in src


def f4():
    """ids 为空 = 应用到全部；非空 = 只改选中的。"""
    src = _backend_src()
    return "wanted is not None and str(job.get(\"id\")) not in wanted" in src


def f5():
    """前端要有批量条 + 两个按钮 + 绑定（已抽成 bindBatchBar 通用函数）。"""
    need = ["job-batch-bar", "job-batch-apply", "job-batch-apply-all",
            "applyBatchTargets(", "selectedTableIds(",
            "bindBatchBar('job'"]
    return all(n in HTML for n in need)


def f6():
    return "api/jobs/batch" in HTML


def f7():
    """表格要有勾选框与全选。"""
    return "job-check-all" in HTML and 'class="job-check"' in HTML


def f8():
    """列表里要能看出"发给全部会话"。"""
    return "全部会话" in HTML


def f9():
    """describe() 要带 resolved，前端才知道纠正过。"""
    src = (ROOT / "modules" / "jobs.py").read_text(encoding="utf-8")
    return "resolved" in src and "broadcast" in src


def f10():
    """纠正后的类型要能通过 describe 读出来。"""
    mgr = _mgr()
    mgr.jobs = [{"id": "j9", "name": "x", "enabled": True,
                 "trigger": {"type": "daily", "time": "08:00"},
                 "target": {"session_type": "private", "session_id": "555000"},
                 "action": {"mode": "template", "template": "hi"}}]
    info = mgr.describe()[0]
    return (info["resolved"]["targets"][0]["session_type"] == "group"
            and info["resolved"]["broadcast"] is False)


def f11():
    """ID 留空时 describe 要标 broadcast=true。"""
    mgr = _mgr()
    mgr.jobs = [{"id": "j10", "name": "x", "enabled": True,
                 "trigger": {"type": "daily", "time": "08:00"},
                 "target": {"session_type": "private", "session_id": ""},
                 "action": {"mode": "template", "template": "hi"}}]
    return mgr.describe()[0]["resolved"]["broadcast"] is True


def f12():
    """批量改完必须落盘 + 重新调度。"""
    src = _backend_src()
    i = src.find("async def handle_jobs_batch(")
    tail = src[i:i + 4000]
    return "job_mgr.save()" in tail and "job_mgr.reload()" in tail


# ---------------------------------------------------------------- 会话枚举


def g1():
    src = (ROOT / "modules" / "session_context.py").read_text(encoding="utf-8")
    return "def list_known_sessions(" in src


def g2():
    """handle_sessions 必须复用同一个枚举函数（避免两处口径不一致）。"""
    src = _backend_src()
    i = src.find("async def handle_sessions(")
    return "list_known_sessions()" in src[i:i + 400]


def g3():
    """枚举必须按记忆文件名过滤，不能把功能数据文件卷进来。"""
    src = (ROOT / "modules" / "session_context.py").read_text(encoding="utf-8")
    i = src.find("def list_known_sessions(")
    return "_MEMORY_FILE_RE" in src[i:i + 1200]


def g4():
    """job_mgr 必须拿到会话枚举器，否则留空 ID 广播不到任何人。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    i = src.find("job_mgr = ScheduledJobManager(")
    return "list_known_sessions" in src[i:i + 300]


def g4b():
    """job_mgr 必须拿到会话历史提供者，否则 LLM 模式的定时问候凭空开场。"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    i = src.find("job_mgr = ScheduledJobManager(")
    return "session_history_block" in src[i:i + 400]


def g5():
    """群号要去掉尾巴上的用户号。"""
    src = (ROOT / "modules" / "session_context.py").read_text(encoding="utf-8")
    i = src.find("def list_known_sessions(")
    return 'rest.split("_")[0]' in src[i:i + 1200]


# ---------------------------------------------------------------- H 批量接口实测


def _handler_probe():
    """真调一次 handle_jobs_batch，验证选中/全部/非法值/落盘。"""
    import json as _json
    import tempfile as _tf
    global M
    try:
        import main as M  # noqa: E402
    except Exception as e:
        print(f"  !! 导入 main 失败: {type(e).__name__}: {e}")
        return False
    tmp = Path(_tf.mkdtemp(prefix="jobsbatch_"))
    mgr = ScheduledJobManager({"scheduler_enabled": True}, tmp, SchedulerManager(),
                              sessions_provider=lambda: [
                                  {"session_type": "private", "session_id": "10001"},
                                  {"session_type": "group", "session_id": "555000"}])
    mgr.jobs = [
        {"id": "a", "name": "A", "enabled": True, "trigger": {"type": "daily", "time": "08:00"},
         "target": {"session_type": "private", "session_id": "10001"},
         "action": {"mode": "template", "template": "早"}},
        {"id": "b", "name": "B", "enabled": True, "trigger": {"type": "daily", "time": "09:00"},
         "target": {"session_type": "private", "session_id": "10002"},
         "action": {"mode": "template", "template": "午"}},
    ]
    M.job_mgr = mgr
    import modules.app_context as _appctx  # noqa: E402
    _appctx.job_mgr = mgr  # handler 已搬进 webui_server，读的是 app_context 槽位

    class Req:
        def __init__(self, payload):
            self._p = payload

        async def json(self):
            return self._p

    class Srv:
        config = {"scheduler_enabled": True}
        handle_jobs_batch = M.WebUIServer.handle_jobs_batch

    srv = Srv()

    async def run():
        r = await srv.handle_jobs_batch(Req({"ids": ["a"], "session_type": "group",
                                             "session_id": "555000"}))
        d = _json.loads(r.body.decode())
        if d.get("changed") != 1:
            return False
        if mgr.jobs[0]["target"] != {"session_type": "group", "session_id": "555000"}:
            return False
        if mgr.jobs[1]["target"]["session_id"] != "10002":
            return False
        r = await srv.handle_jobs_batch(Req({"session_id": ""}))
        d = _json.loads(r.body.decode())
        if d.get("changed") != 2 or any(j["target"]["session_id"] for j in mgr.jobs):
            return False
        r = await srv.handle_jobs_batch(Req({"session_type": "evil", "session_id": "1"}))
        if r.status != 400:
            return False
        disk = _json.loads((tmp / "scheduled_jobs.json").read_text(encoding="utf-8"))
        return all(not j["target"]["session_id"] for j in disk)

    try:
        return asyncio.run(run())
    except Exception as e:
        print(f"  !! 批量接口探针异常: {type(e).__name__}: {e}")
        return False


def main():
    print("=" * 70)
    print("Markdown 渲染 + 定时任务发送目标")
    print("=" * 70)
    print("\nA 前端 Markdown 渲染")
    for n, f in [("有渲染器", a1), ("插件说明走渲染", a2), ("更新说明走渲染", a3),
                 ("不再 textContent 塞原文", a4), ("有配套样式", a5), ("支持标题/代码/列表/表格", a6)]:
        check(n, f)
    print("\nB 不可信内容安全")
    for n, f in [("先转义再变换", b1), ("无裸插入", b2), ("过滤链接协议", b3),
                 ("safeUrl 白名单", b4)]:
        check(n, f)
    print("\nC 会话 ID 留空 → 发给所有聊过的")
    for n, f in [("解析成全部会话", c1), ("真的逐个发送", c2), ("无历史记录则无目标", c3)]:
        check(n, f)
    print("\nD 类型填错自动纠正")
    for n, f in [("群号当私聊→纠正为群聊", d1), ("私聊号当群聊→纠正为私聊", d2),
                 ("正确时不动", d3), ("完整 session_id 写法", d4), ("未知号码不强改", d5)]:
        check(n, f)
    print("\nE 发送失败隔离")
    for n, f in [("单个失败不冒异常", e1), ("全失败不标记已执行", e2),
                 ("部分成功标记已执行", e3), ("未连接不发送", e4)]:
        check(n, f)
    print("\nF 批量修改")
    for n, f in [("后端有接口", f1), ("路由已注册", f2), ("类型白名单", f3),
                 ("ids 为空=全部", f4), ("前端批量条", f5), ("调用了批量接口", f6),
                 ("表格勾选框", f7), ("显示全部会话", f8), ("describe 带 resolved", f9),
                 ("能读出纠正结果", f10), ("能读出 broadcast", f11), ("落盘并重调度", f12)]:
        check(n, f)
    print("\nG 会话枚举")
    for n, f in [("有枚举函数", g1), ("接口复用枚举", g2), ("过滤非记忆文件", g3),
                 ("已注入 job_mgr", g4), ("已注入历史提供者", g4b), ("群号去尾", g5)]:
        check(n, f)
    print("\nH 批量接口实测")
    check("选中/全部/非法值/落盘", _handler_probe)

    print("\n" + "=" * 70)
    print(f"结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        for n in FAIL:
            print(f"  - {n}")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
