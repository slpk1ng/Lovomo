# -*- coding: utf-8 -*-
"""批量扩展（事件 / 待办）+ 插件一键发布到 GitHub 分支的回归测试。

覆盖：
  A 节日问候批量接口
  B 待办批量接口
  C 前端批量条在事件/待办面板都出现，复用了同一个 bindBatchBar
  D 插件发布模块：白名单扫描、token 校验失败传播、分支只含插件文件（mock httpx）
  E publish_token 保存/清除接口（含无效 token 拒绝 400）
  F main.py 路由全部注册
  G plugin_publish_token 已在 _API_KEY_KEYS 里，会被加密落盘

运行: python tests/test_publish_batch.py      （全通过退出码 0）
"""
import asyncio
import base64
import io
import json
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
sys.stdin = io.StringIO()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PASS, FAIL = [], []


def check(name, fn):
    try:
        ok = bool(fn())
    except Exception as e:
        ok = False
        print(f"  !! {name} 抛异常: {type(e).__name__}: {e}")
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


MAIN_SRC = (ROOT / "main.py").read_text(encoding="utf-8")
# WebUI 服务类（WebUIServer 整类）已拆到 modules/webui_server.py，静态扫描换到新源文件
WEBUI_SRC = (ROOT / "modules" / "webui_server.py").read_text(encoding="utf-8")
HTML = (ROOT / "webui" / "start.html").read_text(encoding="utf-8")
# _API_KEY_KEYS（密钥字段清单）已拆到 modules/security.py，扫描换到新源文件
SECURITY_SRC = (ROOT / "modules" / "security.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------- A 事件批量接口


def a1():
    return "async def handle_events_batch(" in WEBUI_SRC


def a2():
    return 'r.add_post("/api/events/batch"' in WEBUI_SRC


def a3():
    """会话类型要过白名单。"""
    i = WEBUI_SRC.find("async def handle_events_batch(")
    return 'session_type not in ("private", "group")' in WEBUI_SRC[i:i + 2500]


def a4():
    """ids 缺失 = 应用到全部。"""
    i = WEBUI_SRC.find("async def handle_events_batch(")
    return 'wanted is not None and str(ev.get("id")) not in wanted' in WEBUI_SRC[i:i + 2500]


def a5():
    """留空 ID 是被允许的（=清空，发给全部）。"""
    i = WEBUI_SRC.find("async def handle_events_batch(")
    return 'has_id = "session_id" in payload' in WEBUI_SRC[i:i + 2500]


def _events_handler_probe():
    try:
        import main as M
    except Exception as e:
        print(f"  !! 导入 main 失败: {type(e).__name__}: {e}")
        return False
    class FakeMgr:
        def __init__(self, evs):
            self.events = evs
        def save_events(self):
            pass
    M.event_mgr = FakeMgr([
            {"id": "a", "name": "元旦", "enabled": True, "targets": [{"session_type": "private", "session_id": "10001"}]},
            {"id": "b", "name": "春节", "enabled": True, "targets": [{"session_type": "private", "session_id": "10002"}]},
        ])
    M.app_context.event_mgr = M.event_mgr  # WebUIServer 读的是 app_context 槽位，镜像同步

    class Req:
        def __init__(self, p): self._p = p
        async def json(self): return self._p

    class Cfg:
        def __init__(self): self.config = {}

    class Srv:
        config = Cfg()
        handle_events_batch = M.WebUIServer.handle_events_batch

    srv = Srv()

    async def run():
        r = await srv.handle_events_batch(Req({"ids": ["a"], "session_type": "group",
                                              "session_id": "555000"}))
        d = json.loads(r.body.decode())
        if d.get("changed") != 1 or M.event_mgr.events[0]["targets"][0]["session_type"] != "group":
            return False
        if M.event_mgr.events[1]["targets"][0]["session_id"] != "10002":
            return False
        r = await srv.handle_events_batch(Req({"session_id": ""}))
        d = json.loads(r.body.decode())
        # 清空会话 ID = 目标整体清掉，回到"发给所有聊过的会话"；留一个空目标
        # 会顶掉默认节日的兜底，问候从此发不出去
        if d.get("changed") != 2 or any(e["targets"] for e in M.event_mgr.events):
            return False
        r = await srv.handle_events_batch(Req({"session_type": "evil"}))
        return r.status == 400
    try:
        return asyncio.run(run())
    except Exception as e:
        print(f"  !! 事件批量探针异常: {type(e).__name__}: {e}")
        return False


# ---------------------------------------------------------------- B 待办批量接口


def b1():
    return "async def handle_todos_batch(" in WEBUI_SRC


def b2():
    return 'r.add_post("/api/todos/batch"' in WEBUI_SRC


def b3():
    i = WEBUI_SRC.find("async def handle_todos_batch(")
    return "{int(i) for i in ids}" in WEBUI_SRC[i:i + 2500]


def b4():
    i = WEBUI_SRC.find("async def handle_todos_batch(")
    return "UPDATE todos SET" in WEBUI_SRC[i:i + 3000]


def _todos_handler_probe():
    try:
        import main as M
    except Exception as e:
        print(f"  !! 导入 main 失败: {type(e).__name__}: {e}")
        return False
    class FakeDB:
        def __init__(self): self.queries = []
        def execute(self, sql, params): self.queries.append((sql, params))
    db = FakeDB()
    class FakeMgr:
        def __init__(self, db): self.db = db
        def list_todos(self, status=None):
            return [
                {"id": 1, "session_type": "private", "session_id": "10001"},
                {"id": 2, "session_type": "private", "session_id": "10002"},
                {"id": 3, "session_type": "private", "session_id": "10003"},
            ]
    M.todo_mgr = FakeMgr(db)
    M.app_context.todo_mgr = M.todo_mgr  # WebUIServer 读的是 app_context 槽位，镜像同步

    class Req:
        def __init__(self, p): self._p = p
        async def json(self): return self._p

    class Cfg:
        def __init__(self): self.config = {}

    class Srv:
        config = Cfg()
        handle_todos_batch = M.WebUIServer.handle_todos_batch
    srv = Srv()

    async def run():
        r = await srv.handle_todos_batch(Req({"ids": [1, 3], "session_type": "group",
                                              "session_id": "555000"}))
        d = json.loads(r.body.decode())
        if d.get("changed") != 2:
            return False
        ids_touched = {params[-1] for sql, params in db.queries}
        if ids_touched != {1, 3}:
            return False
        db.queries.clear()
        r = await srv.handle_todos_batch(Req({"session_id": ""}))
        d = json.loads(r.body.decode())
        if d.get("changed") != 3:
            return False
        if not all("session_id=?" in s for s, _ in db.queries):
            return False
        r = await srv.handle_todos_batch(Req({"session_type": "nope"}))
        return r.status == 400
    try:
        return asyncio.run(run())
    except Exception as e:
        print(f"  !! 待办批量探针异常: {type(e).__name__}: {e}")
        return False


# ---------------------------------------------------------------- C 前端批量条复用


def c1():
    return "function bindBatchBar(" in HTML


def c2():
    return "bindBatchBar('event'" in HTML


def c3():
    return "bindBatchBar('todo'" in HTML


def c4():
    return "bindBatchBar('job'" in HTML


def c5():
    return 'id="event-batch-bar"' in HTML and 'id="event-batch-id"' in HTML and \
        'id="event-batch-apply-all"' in HTML


def c6():
    return 'id="todo-batch-bar"' in HTML and 'id="todo-batch-id"' in HTML and \
        'id="todo-batch-apply-all"' in HTML


def c7():
    return 'id="event-check-all"' in HTML and 'class="event-check"' in HTML


def c8():
    return 'id="todo-check-all"' in HTML and 'class="todo-check"' in HTML


def c9():
    return "selectedTableIds(" in HTML and "function selectedTableIds" in HTML


def c10():
    return "let todosState = []" in HTML and "todosState[i]" in HTML


# ---------------------------------------------------------------- D plugin_publisher


def d1():
    from modules.plugin_publisher import list_publish_files
    tmp = Path(tempfile.mkdtemp(prefix="pub_"))
    (tmp / "plugin.json").write_bytes(b"{}")
    (tmp / "main.py").write_bytes(b"x = 1")
    (tmp / "theme.css").write_bytes(b"body{}")
    (tmp / "data").mkdir()
    (tmp / "data" / "settings.json").write_bytes(b"{}")
    (tmp / "__pycache__").mkdir()
    (tmp / "__pycache__" / "x.pyc").write_bytes(b"")
    (tmp / "evil.exe").write_bytes(b"")
    files = list_publish_files(tmp)
    paths = sorted(p for p, _ in files)
    return paths == ["main.py", "plugin.json", "theme.css"]


def d2():
    from modules.plugin_publisher import list_publish_files
    tmp = Path(tempfile.mkdtemp(prefix="pub_"))
    (tmp / "main.py").write_bytes(b"")
    return len(list_publish_files(tmp)) == 1


def d3():
    """发布白名单必须与安装白名单同源（打包脚本也走同一份）。"""
    from modules.plugin_publisher import ALLOWED
    from modules.plugins import ALLOWED_EXTS
    if ALLOWED != ALLOWED_EXTS:
        print("      发布白名单与安装白名单不一致：",
              sorted(ALLOWED ^ ALLOWED_EXTS))
        return False
    src = (ROOT / ".tmp_test" / "pack_plugin.py").read_text(encoding="utf-8")
    return "from modules.plugins import ALLOWED_EXTS as ALLOWED" in src


def d4():
    from modules.plugin_publisher import PublishError
    try:
        raise PublishError("x")
    except PublishError as e:
        return str(e) == "x"
    return False


def _publish_mock_probe():
    """发布全流程（mock httpx）：插件写进 plugins/<分类>/<id>/，同一提交更新索引。"""
    from modules.plugin_publisher import publish_plugin

    sources = Path(tempfile.mkdtemp(prefix="plugin_src_"))
    tmp = sources / "X"
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "plugin.json").write_bytes('{"name": "X", "category": "工具"}'.encode("utf-8"))
    (tmp / "main.py").write_bytes(b"x = 1")
    (tmp / "README.md").write_text("hello", encoding="utf-8")

    REPO = "user/repo"
    DEFAULT = "main"
    HEAD = "headsha"
    FOLDER = "plugins/工具/X"
    WANT = ["plugins/index.json", "plugins/工具/X/README.md",
            "plugins/工具/X/main.py", "plugins/工具/X/plugin.json"]
    STALE = "plugins/工具/X/old.py"

    def run_once(index_body):
        calls = {"blobs": [], "tree": None, "commit": None, "patch_ref": None}

        def handler(request):
            url = str(request.url)
            method = request.method
            if method == "GET" and url.endswith(f"/repos/{REPO}"):
                # 真实 GitHub 的仓库对象**没有** sha 字段，这里刻意不给：
                # 直接下标 data["sha"] 的老写法在这条用例上必然 KeyError
                return httpx.Response(200, json={"default_branch": DEFAULT,
                                                 "full_name": REPO})
            if method == "GET" and url.endswith(f"/git/ref/heads/{DEFAULT}"):
                return httpx.Response(200, json={"object": {"sha": HEAD}})
            if method == "GET" and url.endswith(f"/git/commits/{HEAD}"):
                return httpx.Response(200, json={"tree": {"sha": "basetree"}})
            if method == "GET" and "/git/trees/basetree" in url:
                return httpx.Response(200, json={
                    "truncated": False,
                    "tree": [{"path": STALE, "type": "blob"},
                             {"path": "README.md", "type": "blob"}]})
            if method == "GET" and "/contents/plugins/index.json" in url:
                if index_body is None:
                    return httpx.Response(404)
                return httpx.Response(200, json={
                    "content": base64.b64encode(index_body).decode()})
            if method == "POST" and url.endswith("/git/blobs"):
                body = json.loads(request.content)
                calls["blobs"].append(base64.b64decode(body["content"]))
                return httpx.Response(201, json={"sha": f"blob{len(calls['blobs'])}"})
            if method == "POST" and url.endswith("/git/trees"):
                calls["tree"] = json.loads(request.content)
                return httpx.Response(201, json={"sha": "treesha"})
            if method == "POST" and url.endswith("/git/commits"):
                calls["commit"] = json.loads(request.content)
                return httpx.Response(201, json={"sha": "commitsha"})
            if method == "PATCH" and url.endswith(f"/git/refs/heads/{DEFAULT}"):
                calls["patch_ref"] = json.loads(request.content)
                return httpx.Response(200, json={"object": {"sha": "commitsha"}})
            return httpx.Response(418, json={"msg": "unexpected", "url": url,
                                             "method": method})

        transport = httpx.MockTransport(handler)

        async def run():
            import modules.plugin_publisher as pub
            orig_client = httpx.AsyncClient

            def patched_client(*args, **kwargs):
                kwargs["transport"] = transport
                return orig_client(*args, **kwargs)

            pub.httpx.AsyncClient = patched_client
            try:
                return await publish_plugin("TOKEN", REPO, "X", sources)
            finally:
                pub.httpx.AsyncClient = orig_client

        return asyncio.run(run()), calls

    # ---- 首次发布：目录与索引在同一个提交里，接在默认分支 HEAD 之后 ----
    r, calls = run_once(None)
    if r["folder"] != FOLDER or r["default_branch"] != DEFAULT:
        print("      folder/default_branch 不对:", r.get("folder"), r.get("default_branch"))
        return False
    if r["url"] != f"https://github.com/{REPO}/tree/{DEFAULT}/{FOLDER}":
        print("      目录链接不对:", r.get("url"))
        return False
    if sorted(p["path"] for p in r["files"]) != WANT:
        print("      发布文件清单不对:", r["files"])
        return False
    if calls["blobs"][:3] != [b"x = 1", '{"name": "X", "category": "工具"}'.encode("utf-8"),
                             b"hello"]:
        print("      上传的插件文件不对:", calls["blobs"][:3])
        return False
    index = json.loads(calls["blobs"][3].decode("utf-8"))
    if [p.get("id") for p in index.get("plugins") or []] != ["X"]:
        print("      索引里没有本插件那一条:", index)
        return False
    if (index["plugins"][0].get("category") != "工具"
            or index["plugins"][0].get("folder") != FOLDER):
        print("      索引条目没记下分类目录:", index["plugins"][0])
        return False
    tree = calls["tree"] or {}
    if tree.get("base_tree") != "basetree":
        # 不挂 base_tree 会把默认分支的整仓文件从新提交里抹掉
        print("      !! 建树时没挂默认分支的 base_tree:", tree.get("base_tree"))
        return False
    items = {e.get("path"): e for e in (tree.get("tree") or [])}
    if sorted(items) != sorted(WANT + [STALE]):
        print("      树里的路径不对:", sorted(items))
        return False
    if items[STALE].get("sha") is not None:
        print("      !! 目录里已不存在的旧文件没被删掉:", items[STALE])
        return False
    if calls["commit"].get("parents") != [HEAD]:
        print("      提交没接在默认分支 HEAD 之后:", calls["commit"])
        return False
    if calls["patch_ref"] != {"sha": "commitsha", "force": False}:
        print("      更新默认分支的请求不对:", calls["patch_ref"])
        return False

    # ---- 已有索引：只覆盖本插件那一条，别的条目原样保留 ----
    old_index = json.dumps({"plugins": [{"id": "other", "version": "1.0.0"}]},
                           ensure_ascii=False).encode("utf-8")
    r2, calls2 = run_once(old_index)
    index = json.loads(calls2["blobs"][-1].decode("utf-8"))
    return ([p.get("id") for p in index.get("plugins") or []] == ["X", "other"]
            and r2["entry"].get("id") == "X"
            and r2["entry"].get("source_repo") == REPO)


def d7():
    """版本标签名要收敛 Git 不接受的字符；空版本不建标签。"""
    from modules.plugin_publisher import version_tag
    if version_tag("lovomo_plugin_x", "1.2.3") != "lovomo_plugin_x-v1.2.3":
        return False
    if version_tag("lovomo_plugin_x", "") != "" or version_tag("lovomo_plugin_x", "  ") != "":
        return False
    bad = version_tag("lovomo_plugin_x", "1.0 beta/2~rc")
    if bad != "lovomo_plugin_x-v1.0-beta-2-rc":
        print("      非法版本号没收敛:", bad)
        return False
    return version_tag("lovomo_plugin_x", "..") == ""


def _publish_tag_probe():
    """发布带版本的插件要打版本标签并建带 zip 附件的 Release；同名的不重发。"""
    from modules.plugin_publisher import publish_plugin

    sources = Path(tempfile.mkdtemp(prefix="plugin_tag_"))
    tmp = sources / "T"
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "plugin.yaml").write_bytes("id: T\nname: T\nversion: 1.2.0\n".encode("utf-8"))

    REPO = "user/repo"
    TAG = "T-v1.2.0"
    ZIP = "T-1.2.0.zip"
    UPLOAD = f"https://uploads.github.com/repos/{REPO}/releases/1/assets{{?name,label}}"

    def run_once(already_published):
        calls = {"tag_refs": [], "patch_ref": None, "releases": [], "assets": []}

        def handler(request):
            url = str(request.url)
            method = request.method
            if method == "GET" and url.endswith(f"/repos/{REPO}"):
                return httpx.Response(200, json={"default_branch": "main"})
            if method == "GET" and url.endswith("/git/ref/heads/main"):
                return httpx.Response(200, json={"object": {"sha": "d"}})
            if method == "GET" and url.endswith("/git/commits/d"):
                return httpx.Response(200, json={"tree": {"sha": "basetree"}})
            if method == "GET" and "/git/trees/basetree" in url:
                return httpx.Response(200, json={"truncated": False, "tree": []})
            if method == "GET" and "/contents/plugins/index.json" in url:
                return httpx.Response(404)
            if method == "GET" and url.endswith(f"/git/ref/tags/{TAG}"):
                return httpx.Response(200 if already_published else 404,
                                      json={"object": {"sha": "commitsha"}})
            if method == "GET" and url.endswith(f"/releases/tags/{TAG}"):
                return httpx.Response(200 if already_published else 404,
                                      json={"id": 1, "tag_name": TAG})
            if method == "POST" and url.endswith("/git/blobs"):
                return httpx.Response(201, json={"sha": "blob1"})
            if method == "POST" and url.endswith("/git/trees"):
                return httpx.Response(201, json={"sha": "tree"})
            if method == "POST" and url.endswith("/git/commits"):
                return httpx.Response(201, json={"sha": "commitsha"})
            if method == "POST" and url.endswith("/git/refs"):
                body = json.loads(request.content)
                calls["tag_refs"].append((body.get("ref"), body.get("sha")))
                return httpx.Response(201, json={"object": {"sha": body.get("sha")}})
            if method == "POST" and url.endswith("/releases"):
                calls["releases"].append(json.loads(request.content))
                return httpx.Response(201, json={"id": 1, "tag_name": TAG,
                                                 "upload_url": UPLOAD})
            if method == "POST" and "/releases/1/assets" in url:
                calls["assets"].append((url, request.content))
                return httpx.Response(201, json={"id": 9, "name": ZIP})
            if method == "PATCH" and url.endswith("/git/refs/heads/main"):
                calls["patch_ref"] = json.loads(request.content)
                return httpx.Response(200, json={"object": {"sha": "commitsha"}})
            return httpx.Response(418, json={"url": url, "method": method})

        transport = httpx.MockTransport(handler)

        async def run():
            import modules.plugin_publisher as pub
            orig = httpx.AsyncClient

            def patched(*args, **kwargs):
                kwargs["transport"] = transport
                return orig(*args, **kwargs)

            pub.httpx.AsyncClient = patched
            try:
                return await publish_plugin("TOKEN", REPO, "T", sources)
            finally:
                pub.httpx.AsyncClient = orig

        return asyncio.run(run()), calls

    r, calls = run_once(False)
    if r.get("tag") != TAG or r.get("tagged") is not True:
        print("      没打版本标签:", r.get("tag"), r.get("tagged"))
        return False
    if calls["tag_refs"] != [("refs/tags/" + TAG, "commitsha")]:
        print("      标签请求不对:", calls["tag_refs"])
        return False
    if calls["patch_ref"] != {"sha": "commitsha", "force": False}:
        print("      更新默认分支的请求不对:", calls["patch_ref"])
        return False
    if r.get("release") != TAG or r.get("zip") != ZIP:
        print("      Release 没建上:", r.get("release"), r.get("zip"))
        return False
    if [rel.get("tag_name") for rel in calls["releases"]] != [TAG]:
        print("      建 Release 的请求不对:", calls["releases"])
        return False
    if len(calls["assets"]) != 1 or not calls["assets"][0][1].startswith(b"PK"):
        print("      zip 附件没传上去:", calls["assets"])
        return False
    if f"name={ZIP}" not in calls["assets"][0][0]:
        print("      附件名不对:", calls["assets"][0][0])
        return False

    r2, calls2 = run_once(True)
    if (r2.get("tagged") is not False or calls2["tag_refs"]
            or calls2["releases"] or calls2["assets"]):
        print("      已发过的版本被重发:", calls2)
        return False
    return r2.get("release") == TAG


def d5():
    import httpx
    from modules.plugin_publisher import verify_token, PublishError
    def handler(req):
        return httpx.Response(401)
    orig = httpx.AsyncClient
    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return orig(*args, **kwargs)
    import modules.plugin_publisher as pub
    pub.httpx.AsyncClient = patched
    async def run():
        try:
            await verify_token("BAD")
            return False
        except PublishError as e:
            return "Token" in str(e)
        finally:
            pub.httpx.AsyncClient = orig
    return asyncio.run(run())


def d6():
    import httpx
    from modules.plugin_publisher import verify_token, PublishError
    def handler(req):
        raise httpx.ConnectError("no net")
    orig = httpx.AsyncClient
    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return orig(*args, **kwargs)
    import modules.plugin_publisher as pub
    pub.httpx.AsyncClient = patched
    async def run():
        try:
            await verify_token("X")
            return False
        except PublishError:
            return True
        finally:
            pub.httpx.AsyncClient = orig
    return asyncio.run(run())


# ---------------------------------------------------------------- E publish 接口


def e1():
    return "async def handle_plugins_publish_token(" in WEBUI_SRC


def e2():
    return "async def handle_plugins_publish_token_clear(" in WEBUI_SRC


def e3():
    return "async def handle_plugins_publish_status(" in WEBUI_SRC


def e4():
    return "async def handle_plugins_publish(" in WEBUI_SRC


def e5():
    i = WEBUI_SRC.find("async def handle_plugins_publish_token(")
    return "Token 不能为空" in WEBUI_SRC[i:i + 2000]


def e6():
    i = WEBUI_SRC.find("async def handle_plugins_publish_token(")
    return "_verify_github_token(token)" in WEBUI_SRC[i:i + 2000]


def e7():
    i = WEBUI_SRC.find("async def handle_plugins_publish(")
    return "self.plugin_manager.root" in WEBUI_SRC[i:i + 3000]


def e8():
    i = WEBUI_SRC.find("async def handle_plugins_publish(")
    return "尚未认证 GitHub 身份" in WEBUI_SRC[i:i + 3000]


def _publish_token_probe():
    try:
        import main as M
    except Exception as e:
        print(f"  !! 导入 main 失败: {type(e).__name__}: {e}")
        return False
    import httpx
    from modules.plugin_publisher import verify_token

    def handler(req):
        return httpx.Response(200, json={"login": "alice", "name": "Alice"})
    orig = httpx.AsyncClient
    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return orig(*args, **kwargs)
    import modules.plugin_publisher as pub
    pub.httpx.AsyncClient = patched

    class Req:
        def __init__(self, p): self._p = p
        async def json(self): return self._p

    # 这里必须用真实 ConfigLoader：假的 Cfg 若自带 save()，
    # 会盖住 "ConfigLoader 没有 save 方法" 这个真实 bug
    from main import ConfigLoader
    cfg_path = Path(tempfile.mkdtemp(prefix="pubtok_")) / "config.json"
    cfg = ConfigLoader(str(cfg_path))

    class Srv:
        def __init__(self): self.config = cfg

        async def _plugins_market_payload(self, force=False, sort="latest",
                                         market="official"):
            # 发布状态会强制拉一次市场，测试里给个空市场
            return {"plugins": []}

        async def _published_versions(self, payload=None):
            # 已上架版本要联网扫市场，测试里不查
            return {}

        async def _published_entries(self, login, market_path, payload=None):
            # 已上架列表同样来自市场索引，测试里不查
            return []

        _publish_scope = M.WebUIServer._publish_scope
        handle_plugins_publish_token = M.WebUIServer.handle_plugins_publish_token
        handle_plugins_publish_token_clear = M.WebUIServer.handle_plugins_publish_token_clear
        handle_plugins_publish_status = M.WebUIServer.handle_plugins_publish_status
    srv = Srv()

    async def run():
        r = await srv.handle_plugins_publish_token(Req({"token": "ghp_xxx"}))
        d = json.loads(r.body.decode())
        if not d.get("success") or d.get("login") != "alice":
            return False
        if srv.config.config.get("plugin_publish_token") != "ghp_xxx":
            return False
        # 必须真落盘，而且是密文
        if not cfg_path.is_file():
            print("  !! token 没写进磁盘")
            return False
        disk = json.loads(cfg_path.read_text(encoding="utf-8"))
        on_disk = str(disk.get("plugin_publish_token") or "")
        if not on_disk.startswith(("enc:", "enc2:")):
            print(f"  !! token 明文落盘: {on_disk[:20]}")
            return False
        r = await srv.handle_plugins_publish_token(Req({"token": ""}))
        if r.status != 400:
            return False
        r = await srv.handle_plugins_publish_status(Req({}))
        d = json.loads(r.body.decode())
        if not d.get("configured"):
            return False
        r = await srv.handle_plugins_publish_token_clear(Req({}))
        d = json.loads(r.body.decode())
        if not d.get("success") or "plugin_publish_token" in srv.config.config:
            return False
        r = await srv.handle_plugins_publish_status(Req({}))
        d = json.loads(r.body.decode())
        return d.get("configured") == False
    try:
        ok = asyncio.run(run())
    finally:
        pub.httpx.AsyncClient = orig
    return ok


# ---------------------------------------------------------------- F 路由


def f1():
    return 'r.add_post("/api/plugins/publish_token"' in WEBUI_SRC


def f2():
    return 'r.add_post("/api/plugins/publish_token_clear"' in WEBUI_SRC


def f3():
    return 'r.add_get("/api/plugins/publish_status"' in WEBUI_SRC


def f4():
    return 'r.add_post("/api/plugins/publish"' in WEBUI_SRC


def f5():
    return all(p in WEBUI_SRC for p in (
        '"/api/jobs/batch"', '"/api/events/batch"', '"/api/todos/batch"'))


# ---------------------------------------------------------------- G 密钥字段


def g1():
    m = re.search(r'_API_KEY_KEYS\s*=\s*\(([^)]*)\)', SECURITY_SRC)
    return m and "plugin_publish_token" in m.group(1)


def g2():
    m = re.search(r'_API_KEY_KEYS\s*=\s*\(([^)]*)\)', SECURITY_SRC)
    keys = m.group(1)
    return all(k in keys for k in ("llm_api_key", "napcat_token", "web_search_api_keys"))


# ---------------------------------------------------------------- H 前端 UI


def h1():
    return 'id="plugin-publish-block"' in HTML and 'id="plugin-publish-token"' in HTML


def h2():
    return "bindPublishUi();" in HTML


def h3():
    return "plugin-publish-btn" in HTML


def h4():
    return "window.prompt" in HTML and "提交信息" in HTML


def h5():
    return "查看目录" in HTML


def h6():
    return "Token 已保存" in HTML


def h7():
    """版本已发布过时换成提示文案，不再给发布按钮。"""
    i = HTML.rfind("当前版本已发布过")
    if i < 0 or "needs_publish" not in HTML:
        return False
    body = HTML[max(0, i - 300):i + 400]
    return "plugin-publish-btn" in body and "p.published_version" in body


# -------------------------------------------- I 证书校验与配置落盘


def i1():
    """发布请求要走项目统一的 verified_context（系统库 + certifi）。"""
    src = (ROOT / "modules" / "plugin_publisher.py").read_text(encoding="utf-8")
    return ("from modules.tls import verified_context" in src
            and "verify=verified_context()" in src)


def i2():
    """PAT 在请求头上，证书失败时不能降级成不校验。"""
    src = (ROOT / "modules" / "plugin_publisher.py").read_text(encoding="utf-8")
    return "unverified_context" not in src


def i3():
    """不能再出现 config.save()：ConfigLoader 没有这个方法。"""
    return not re.search(r"self\.config\.save\(\)", WEBUI_SRC)


def i4():
    """落盘要过 _atomic_save，保证密钥被加密。"""
    i = WEBUI_SRC.find("async def handle_plugins_publish_token(")
    return "_atomic_save" in WEBUI_SRC[i:i + 1200]


def i5():
    """ConfigLoader 确实没有 save 方法（钉住 i3 的前提）。"""
    from main import ConfigLoader
    return not hasattr(ConfigLoader, "save")


# ------------------------------------------- J 发布健壮性（KeyError: 'sha'）

def _mock_publisher(handler):
    """把 publisher 用的 httpx.AsyncClient 换成 MockTransport。"""
    import httpx
    import modules.plugin_publisher as pub
    orig = pub.httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return orig(*args, **kwargs)

    pub.httpx.AsyncClient = patched
    return pub, orig


def _fetch_default_with(handler):
    """跑一次 fetch_default，返回 (异常, 返回值, 打出来的日志)。"""
    import contextlib
    import io as _io
    from modules.plugin_publisher import fetch_default
    buf = _io.StringIO()
    pub, orig = _mock_publisher(handler)
    err, value = None, None
    try:
        with contextlib.redirect_stdout(buf):
            value = asyncio.run(fetch_default("T", "o/r"))
    except Exception as e:
        err = e
    finally:
        pub.httpx.AsyncClient = orig
    return err, value, buf.getvalue()


def j1():
    """仓库对象没有 sha 字段时不能再抛 KeyError：改查默认分支引用。"""
    from modules.plugin_publisher import PublishError
    seen = []

    def handler(req):
        url = str(req.url)
        seen.append(url)
        if url.endswith("/repos/o/r"):
            return httpx.Response(200, json={"default_branch": "dev"})
        if url.endswith("/repos/o/r/git/ref/heads/dev"):
            return httpx.Response(200, json={"object": {"sha": "deadbeef"}})
        return httpx.Response(418, json={"msg": "unexpected"})

    err, value, _ = _fetch_default_with(handler)
    if isinstance(err, PublishError):
        print(f"  !! fetch_default 报错: {err}")
        return False
    if err is not None:
        print(f"  !! 抛了 {type(err).__name__}: {err}")
        return False
    if value != ("dev", "deadbeef"):
        print("      返回值不对:", value)
        return False
    return any(u.endswith("/git/ref/heads/dev") for u in seen)


def j2():
    """仓库对象里没有 default_branch 也要能跑（退回 main）。"""
    def handler(req):
        url = str(req.url)
        if url.endswith("/repos/o/r"):
            return httpx.Response(200, json={"full_name": "o/r"})
        if url.endswith("/repos/o/r/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "abc"}})
        return httpx.Response(418, json={"msg": "unexpected"})

    err, value, _ = _fetch_default_with(handler)
    if err is not None:
        print(f"  !! 抛了 {type(err).__name__}: {err}")
        return False
    return value == ("main", "abc")


def j3():
    """分支引用缺 object.sha → 可读 PublishError，而不是 KeyError。"""
    from modules.plugin_publisher import PublishError

    def handler(req):
        if str(req.url).endswith("/repos/o/r"):
            return httpx.Response(200, json={"default_branch": "main"})
        return httpx.Response(200, json={"ref": "refs/heads/main"})

    err, _, log = _fetch_default_with(handler)
    if not isinstance(err, PublishError):
        print(f"  !! 期望 PublishError，实际 {type(err).__name__}: {err}")
        return False
    if "object.sha" not in str(err):
        print(f"      提示里没说是哪个字段缺失: {err}")
        return False
    return "object.sha" in log


def j4():
    """404 / 500 都要给带状态码的可读错误。"""
    from modules.plugin_publisher import PublishError
    for code, word in ((404, "不存在"), (500, "HTTP 500")):
        err, _, log = _fetch_default_with(
            lambda req, code=code: httpx.Response(code, json={"message": "nope"}))
        if not isinstance(err, PublishError):
            print(f"  !! {code} 期望 PublishError，实际 {type(err).__name__}: {err}")
            return False
        if word not in str(err):
            print(f"      {code} 的提示不对: {err}")
            return False
        if "插件发布" not in log or "api.github.com" not in log:
            print(f"      {code} 没留下排查日志")
            return False
    return True


def j5():
    """响应体不是 JSON 对象（比如 HTML 错误页）也不能崩。"""
    from modules.plugin_publisher import PublishError

    def handler(req):
        return httpx.Response(200, text="<html>gateway</html>")

    err, _, log = _fetch_default_with(handler)
    if not isinstance(err, PublishError):
        print(f"  !! 期望 PublishError，实际 {type(err).__name__}: {err}")
        return False
    return "JSON" in str(err) or "结构" in str(err)


def j6():
    """commit_tree / commit_files 也不裸下标，缺字段给可读错误。"""
    from modules.plugin_publisher import (commit_tree, commit_files, PublishError)

    # 提交对象缺 tree.sha → 可读错误，不是 KeyError
    pub, orig = _mock_publisher(
        lambda req: httpx.Response(200, json={"sha": "c"}))
    try:
        try:
            asyncio.run(commit_tree("T", "o/r", "x"))
            print("  !! 缺 tree.sha 时 commit_tree 没报错")
            return False
        except PublishError as e:
            if "tree.sha" not in str(e):
                print("      commit_tree 的提示不对:", e)
                return False
        except Exception as e:
            print(f"  !! commit_tree 抛了 {type(e).__name__}: {e}")
            return False
    finally:
        pub.httpx.AsyncClient = orig

    # 建树回包缺 sha → 可读错误
    pub, orig = _mock_publisher(lambda req: httpx.Response(201, json={"ok": True}))
    try:
        try:
            asyncio.run(commit_files("T", "o/r", [("a.py", b"x")], "m", []))
            print("  !! 缺 sha 时 commit_files 没报错")
            return False
        except PublishError as e:
            if "sha" not in str(e):
                print("      commit_files 的提示不对:", e)
                return False
        except Exception as e:
            print(f"  !! commit_files 抛了 {type(e).__name__}: {e}")
            return False
    finally:
        pub.httpx.AsyncClient = orig

    # 回包不是 JSON → 可读错误
    pub, orig = _mock_publisher(
        lambda req: httpx.Response(200, text="<html>oops</html>"))
    try:
        try:
            asyncio.run(commit_tree("T", "o/r", "x"))
            print("  !! 回包不是 JSON 时 commit_tree 没报错")
            return False
        except PublishError as e:
            return "JSON" in str(e) or "结构" in str(e)
        except Exception as e:
            print(f"  !! commit_tree 抛了 {type(e).__name__}: {e}")
            return False
    finally:
        pub.httpx.AsyncClient = orig


def j7():
    """插件清单只认 plugin.yaml / plugin.json，缺清单给可读错误。"""
    from modules.plugin_publisher import find_manifest, MANIFEST_NAMES
    if MANIFEST_NAMES[0] != "plugin.yaml":
        print("      plugin.yaml 不是首选清单名:", MANIFEST_NAMES)
        return False
    tmp = Path(tempfile.mkdtemp(prefix="pubman_"))
    if find_manifest(tmp) is not None:
        return False
    (tmp / "plugin.json").write_text("{}", encoding="utf-8")
    if (find_manifest(tmp) or Path()).name != "plugin.json":
        return False
    (tmp / "plugin.yaml").write_text("id: a\n", encoding="utf-8")
    return (find_manifest(tmp) or Path()).name == "plugin.yaml"


# ------------------------------- K 发布归属门禁 + 市场来源校验


def _make_pm(prefix):
    from modules.plugins import PluginManager
    tmp = Path(tempfile.mkdtemp(prefix=prefix))
    return PluginManager(tmp / "plugins", state_file=tmp / "state.json")


def _install_plugin(pm, pid, manifest_text, name="plugin.yaml"):
    """装一个只含清单 + theme.css 的插件，返回安装结果。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, manifest_text)
        zf.writestr("theme.css", ".a{}")
    return pm.install_zip(buf.getvalue(), force=True)


def _publish_srv(cfg_dict, pm, published=None, on_market=None):
    import main as M

    class Cfg:
        def __init__(self, data): self.config = data

    class Srv:
        def __init__(self):
            self.config = Cfg(cfg_dict)
            self.plugin_manager = pm

        async def _plugins_market_payload(self, force=False, sort="latest",
                                         market="official"):
            # 发布状态会强制拉一次市场，测试里给个空市场
            return {"plugins": []}

        async def _published_versions(self, payload=None):
            # 已上架版本要联网扫市场，测试里用假数据顶掉
            return dict(published or {})

        async def _published_entries(self, login, market_path, payload=None):
            # 已上架列表同样来自市场索引，测试里用假数据顶掉
            return [dict(e) for e in (on_market or [])]

        _publish_scope = M.WebUIServer._publish_scope
        _check_market_source = M.WebUIServer._check_market_source
        handle_plugins_publish = M.WebUIServer.handle_plugins_publish
        handle_plugins_publish_status = M.WebUIServer.handle_plugins_publish_status
    return Srv()


class _Req:
    def __init__(self, payload): self._payload = payload

    async def json(self): return self._payload


def k1():
    """归属判断：github / repo / homepage 里任意一个对上就算自己的插件。"""
    from modules.plugins import ownership_matches, manifest_logins
    mine = {"author": "爱丽丝", "github": "alice", "repo": "alice/my-plugin"}
    if not ownership_matches(mine, "alice") or not ownership_matches(mine, "ALICE"):
        return False
    if ownership_matches(mine, "bob"):
        return False
    if manifest_logins({"author": "Lovomo 官方示例"}) != []:
        print("      中文作者名不该被当成 GitHub 用户名")
        return False
    from_url = {"homepage": "https://github.com/carol/x"}
    return ownership_matches(from_url, "carol") and not ownership_matches(from_url, "alice")


def k2():
    """仓库归属解析：URL、简写、带 .git 后缀都要认。"""
    from modules.plugins import repo_owner, repo_slug
    if repo_owner("https://github.com/slpk1ng/Lovomo/tree/main") != "slpk1ng":
        return False
    if repo_owner("slpk1ng/Lovomo") != "slpk1ng" or repo_owner("") != "":
        return False
    if repo_slug("https://github.com/slpk1ng/Lovomo.git") != "slpk1ng/Lovomo":
        return False
    if repo_slug("https://codeload.github.com/o/r/zip/refs/heads/b") != "o/r":
        return False
    return repo_slug("o/r") == "o/r" and repo_slug("随便写点什么") == ""


def k3():
    """发布状态只列出属于当前登录账号的插件，其余归入 blocked。"""
    pm = _make_pm("own_")
    _install_plugin(pm, "mine", "id: mine\nname: 我的\ngithub: alice\n")
    _install_plugin(pm, "theirs", "id: theirs\nname: 别人的\ngithub: bob\n")
    _install_plugin(pm, "unknown", "id: unknown\nname: 没写归属\n")
    srv = _publish_srv({"plugin_publish_token": "T", "plugin_publish_login": "alice",
                        "plugin_market_repo": "alice/Lovomo"}, pm)
    d = json.loads(asyncio.run(srv.handle_plugins_publish_status(_Req({}))).body.decode())
    if d.get("publishable") != ["mine"]:
        print("      可发布列表不对:", d.get("publishable"))
        return False
    if sorted(p["id"] for p in d.get("blocked") or []) != ["theirs", "unknown"]:
        print("      被挡下的列表不对:", d.get("blocked"))
        return False
    return d.get("configured") is True and d.get("login") == "alice"


def k4():
    """未认证（没有登录身份）时一个插件都不给发布。"""
    pm = _make_pm("noauth_")
    _install_plugin(pm, "mine", "id: mine\ngithub: alice\n")
    srv = _publish_srv({"plugin_publish_token": "T", "plugin_publish_login": "",
                        "plugin_market_repo": "alice/Lovomo"}, pm)
    d = json.loads(asyncio.run(srv.handle_plugins_publish_status(_Req({}))).body.decode())
    if d.get("configured") or d.get("publishable"):
        print("      未认证却给了发布权限:", d)
        return False
    r = asyncio.run(srv.handle_plugins_publish(_Req({"id": "mine"})))
    return r.status == 400 and "尚未认证" in json.loads(r.body.decode())["error"]


def k5():
    """归属不符的插件即使直接调接口也发不出去（不能只靠前端隐藏）。"""
    pm = _make_pm("gate_")
    _install_plugin(pm, "theirs", "id: theirs\nname: 别人的\ngithub: bob\n")
    srv = _publish_srv({"plugin_publish_token": "T", "plugin_publish_login": "alice",
                        "plugin_market_repo": "alice/Lovomo"}, pm)

    def boom(req):
        raise AssertionError("归属校验没过就不该发网络请求")

    pub, orig = _mock_publisher(boom)
    try:
        r = asyncio.run(srv.handle_plugins_publish(_Req({"id": "theirs"})))
    finally:
        pub.httpx.AsyncClient = orig
    d = json.loads(r.body.decode())
    if r.status != 403 or "归属" not in d.get("error", ""):
        print("      期望 403 + 归属提示，实际:", r.status, d)
        return False
    return "bob" in d["error"]


def k6():
    """市场来源校验：对得上放行，对不上拒绝，没声明来源的放行。"""
    import main as M
    check = M.WebUIServer._check_market_source
    entry = {"id": "a", "source_repo": "alice/Lovomo"}
    if check(entry, {"id": "a", "repo": "alice/Lovomo"}):
        print("      仓库一致却被拒")
        return False
    if check(entry, {"id": "a", "github": "alice"}):
        print("      归属用户名一致却被拒")
        return False
    if check(entry, {"id": "a"}):
        print("      没声明来源的插件应放行（全新插件无从比对）")
        return False
    bad = check(entry, {"id": "a", "github": "bob", "repo": "bob/Lovomo"})
    if not bad or "不一致" not in bad:
        print("      归属不符却没被拒:", bad)
        return False
    # 索引条目没写 source_repo 时无从校验，放行
    return check({"id": "a"}, {"id": "a", "github": "bob"}) == ""


def k7():
    """来源记录：市场安装后能查到插件是从哪条仓库/分支来的。"""
    pm = _make_pm("src_")
    _install_plugin(pm, "a", "id: a\ngithub: alice\n")
    if pm.source_of("a"):
        return False
    pm.set_source("a", "alice/Lovomo", "lovomo_plugin_a", "branches")
    src = pm.source_of("a")
    if src.get("repo") != "alice/Lovomo" or src.get("branch") != "lovomo_plugin_a":
        print("      来源没记对:", src)
        return False
    info = pm.get("a")
    if info.get("source_repo") != "alice/Lovomo":
        print("      插件信息里没带上来源:", info.get("source_repo"))
        return False
    pm.uninstall("a")
    return pm.source_of("a") == {}


def k8():
    """plugin.yaml 是首选清单：字段能读出来，且 yaml 优先于 json。"""
    pm = _make_pm("yaml_")
    yaml_text = ("id: y1\nname: YAML 插件\nversion: 2.1.0\nauthor: 爱丽丝\n"
                 "github: alice\nhomepage: https://github.com/alice/y1\n"
                 "logo: logo.png\ndescription: 用 yaml 写的清单\n"
                 "features:\n  - key: on\n    label: 开关\n    type: bool\n"
                 "    default: true\n")
    r = _install_plugin(pm, "y1", yaml_text)
    if not r.get("success"):
        print("      安装失败:", r)
        return False
    info = pm.get("y1")
    if (info.get("name") != "YAML 插件" or info.get("version") != "2.1.0"
            or info.get("github") != "alice" or info.get("logo") != "logo.png"):
        print("      yaml 清单没解析对:", info)
        return False
    if [f["key"] for f in info.get("features") or []] != ["on"]:
        print("      features 没解析出来:", info.get("features"))
        return False
    if info.get("owners") != ["alice"]:
        print("      归属没算对:", info.get("owners"))
        return False
    # 两个清单同时存在时以 yaml 为准
    _install_plugin(pm, "y2", "id: y2\nname: 来自 YAML\n",
                    name="plugin.yaml")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("plugin.yaml", "id: y2\nname: 来自 YAML\n")
        zf.writestr("plugin.json", json.dumps({"id": "y2", "name": "来自 JSON"}))
    pm.install_zip(buf.getvalue(), force=True)
    return pm.get("y2").get("name") == "来自 YAML"


def k9():
    """缺清单时补出来的也必须是 plugin.yaml（不是 plugin.json）。"""
    pm = _make_pm("gen_")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("theme.css", ".a{}")
    r = pm.install_zip(buf.getvalue(), force=True)
    if not r.get("success"):
        return False
    d = pm.plugin_dir(r["id"])
    return (d / "plugin.yaml").is_file() and not (d / "plugin.json").is_file()


def k10():
    """版本没往上走就不给发布按钮：同版本、本地 beta、线上已经更高都算已发布过。"""
    pm = _make_pm("ver_")
    _install_plugin(pm, "same", "id: same\nname: 同版本\ngithub: alice\nversion: 1.0.0\n")
    _install_plugin(pm, "beta", "id: beta\nname: 预发布\ngithub: alice\nversion: 1.0.0-beta.2\n")
    _install_plugin(pm, "old", "id: old\nname: 落后\ngithub: alice\nversion: 0.9.0\n")
    _install_plugin(pm, "new", "id: new\nname: 新版本\ngithub: alice\nversion: 1.1.0\n")
    _install_plugin(pm, "fresh", "id: fresh\nname: 没上架过\ngithub: alice\nversion: 1.0.0\n")
    srv = _publish_srv({"plugin_publish_token": "T", "plugin_publish_login": "alice",
                        "plugin_market_repo": "alice/Lovomo"}, pm,
                       {"same": "1.0.0", "beta": "1.0.0", "old": "1.0.0", "new": "1.0.0"})
    d = json.loads(asyncio.run(srv.handle_plugins_publish_status(_Req({}))).body.decode())
    got = {p["id"]: p.get("needs_publish") for p in d.get("plugins") or []}
    if got != {"same": False, "beta": False, "old": False, "new": True, "fresh": True}:
        print("      可发布判断不对:", got)
        return False
    was = {p["id"]: p.get("published_version") for p in d.get("plugins") or []}
    if was.get("same") != "1.0.0" or was.get("fresh") != "":
        print("      已上架版本没带上:", was)
        return False
    # 名单本身还是"归属对得上的全部插件"，只是其中一部分不可发
    return sorted(d.get("publishable") or []) == ["beta", "fresh", "new", "old", "same"]


def k11():
    """市场拉不到时（拿不到已上架版本）不能把发布全禁掉。"""
    pm = _make_pm("nover_")
    _install_plugin(pm, "mine", "id: mine\ngithub: alice\nversion: 1.0.0\n")
    srv = _publish_srv({"plugin_publish_token": "T", "plugin_publish_login": "alice",
                        "plugin_market_repo": "alice/Lovomo"}, pm, None)
    d = json.loads(asyncio.run(srv.handle_plugins_publish_status(_Req({}))).body.decode())
    return [p.get("needs_publish") for p in d.get("plugins") or []] == [True]


def main():
    print("=" * 70)
    print("批量（事件/待办）+ 插件发布到市场")
    print("=" * 70)
    print("\nA 事件批量接口")
    for n, f in [("handle_events_batch", a1), ("路由注册", a2),
                 ("类型白名单", a3), ("ids 缺失=全部", a4),
                 ("留空 ID 允许清空", a5), ("真调接口", _events_handler_probe)]:
        check(n, f)
    print("\nB 待办批量接口")
    for n, f in [("handle_todos_batch", b1), ("路由注册", b2),
                 ("id 是整数", b3), ("SQL 更新", b4), ("真调接口", _todos_handler_probe)]:
        check(n, f)
    print("\nC 前端批量条复用")
    for n, f in [("bindBatchBar 已抽出来", c1), ("节日面板用", c2),
                 ("待办面板用", c3), ("定时任务面板仍用", c4),
                 ("节日批量条 DOM", c5), ("待办批量条 DOM", c6),
                 ("节日勾选框", c7), ("待办勾选框", c8),
                 ("selectedTableIds 存在", c9), ("todosState 已声明", c10)]:
        check(n, f)
    print("\nD plugin_publisher 文件枚举与 API 流程")
    for n, f in [("白名单扫描", d1), ("无 plugin.json 也能枚举", d2),
                 ("白名单与 pack_plugin 一致", d3), ("PublishError 类型", d4),
                 ("publish_plugin 全流程（mock httpx）", _publish_mock_probe),
                 ("发布打版本标签", _publish_tag_probe), ("版本标签名收敛", d7),
                 ("token 401 失败传播", d5), ("网络异常转为 PublishError", d6)]:
        check(n, f)
    print("\nE publish 接口")
    for n, f in [("handle_plugins_publish_token", e1),
                 ("handle_plugins_publish_token_clear", e2),
                 ("handle_plugins_publish_status", e3),
                 ("handle_plugins_publish", e4),
                 ("空 token 拒绝", e5), ("走 verify_token", e6),
                 ("publish 用 plugin_manager.root", e7),
                 ("publish 无 token 400", e8),
                 ("真调 publish_token", _publish_token_probe)]:
        check(n, f)
    print("\nF 路由")
    for n, f in [("publish_token 路由", f1), ("publish_token_clear 路由", f2),
                 ("publish_status 路由", f3), ("publish 路由", f4),
                 ("三个批量接口都注册", f5)]:
        check(n, f)
    print("\nG 密钥字段")
    for n, f in [("plugin_publish_token 在 _API_KEY_KEYS", g1),
                 ("原有密钥字段未丢", g2)]:
        check(n, f)
    print("\nH 前端发布 UI")
    for n, f in [("DOM 完整", h1), ("bindPublishUi 已绑定", h2),
                 ("每插件有发布按钮", h3), ("提交信息 prompt", h4),
                 ("结果链接展示", h5), ("Token 保存反馈", h6),
                 ("已发布过的版本不给按钮", h7)]:
        check(n, f)
    print("\nI 证书校验与配置落盘")
    for n, f in [("走 verified_context", i1), ("不降级成不校验", i2),
                 ("没有 config.save()", i3), ("落盘过 _atomic_save", i4),
                 ("ConfigLoader 无 save", i5)]:
        check(n, f)
    print("\nJ 发布健壮性（KeyError: 'sha'）")
    for n, f in [("仓库对象无 sha 也不崩", j1), ("无 default_branch 退回 main", j2),
                 ("缺 object.sha 给可读错误", j3), ("404/500 提示带状态码", j4),
                 ("非 JSON 响应不崩", j5), ("提交/建树接口安全取值", j6),
                 ("清单名优先级", j7)]:
        check(n, f)
    print("\nK 发布归属门禁 + 市场来源校验")
    for n, f in [("归属判断", k1), ("仓库归属解析", k2),
                 ("只列自己的插件", k3), ("未认证不给发布", k4),
                 ("归属不符直接调接口也发不出去", k5), ("市场来源校验", k6),
                 ("来源可追溯", k7), ("plugin.yaml 优先", k8),
                 ("补的清单是 yaml", k9), ("版本没更新不给发布", k10),
                 ("拿不到已上架版本时不挡人", k11)]:
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