# -*- coding: utf-8 -*-
"""插件市场：官方 / 第三方分流、第三方地址、搜索排序、发布提示。

覆盖：
  A 地址解析：纯 slug / 网址 / 注释 / 去重 / 非法行
  B 分流：官方只读配置的那一个仓库，第三方按地址逐个读并合并
  C 容错：单个仓库拉不动不牵连别的仓库；warning 不重复套仓库前缀
  D 缓存：官方与第三方各自分键，切换不串条目，同市场不重复打网络
  E 保存接口：解析结果回给前端、落盘、清缓存；坏请求体给 400
  F 从第三方市场安装也能找到条目
  G 发布成功要同时落日志（print 会被重定向进 app.log）
  H 前端：官方/第三方切换、第三方地址框、排序旁的搜索、toast
  I 程序历史版本已挪到 logo 入口，插件历史页不再有它
  J 搜索打分（把前端函数抽出来用 node 真跑一遍）
  K 分支分页：超过 100 条按页取全，不足一页即停，页数有上限
  L 发布状态：市场索引 ∪ 版本标签，版本取大；索引读仓库的默认分支
  M 来源清单：官方市场按市场仓库里的清单再扫第三方仓库，别人提 PR 加一行即可上架
  N 分类目录（plugins/<分类>/<插件id>）、版本只许往上走、下架、二级密码、市场按分类筛选、
    发布状态按最新索引判定、本机推送/下架记录落盘

运行: python tests/test_market_split.py      （全通过退出码 0）
"""
import asyncio
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
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
from modules.market import parse_market_repos  # noqa: E402
from modules import webui_server as W  # noqa: E402  # WebUIServer 迁入后按定义模块打补丁

PASS, FAIL = [], []

HTML_SRC = (ROOT / "webui" / "start.html").read_text(encoding="utf-8")
MAIN_SRC = (ROOT / "main.py").read_text(encoding="utf-8")
# 二级密码常量与判定函数已拆到 modules/security.py，静态扫描需同时覆盖
SECURITY_SRC = (ROOT / "modules" / "security.py").read_text(encoding="utf-8")
# WebUI 认证中间件已拆到 modules/webui_common.py，静态扫描需同时覆盖
WEBCOMMON_SRC = (ROOT / "modules" / "webui_common.py").read_text(encoding="utf-8")
# 默认配置字面量已拆到 modules/config_loader.py
CONFIG_LOADER_SRC = (ROOT / "modules" / "config_loader.py").read_text(encoding="utf-8")
# WebUI 服务类（WebUIServer 整类 + WebUI 域常量）已拆到 modules/webui_server.py，静态扫描需同时覆盖
WEBUI_SRC = (ROOT / "modules" / "webui_server.py").read_text(encoding="utf-8")


def check(name, fn):
    try:
        result = fn()
        if not result:
            raise AssertionError(f"断言为假: {result!r}")
        PASS.append(name)
        print(f"  [PASS] {name}")
    except Exception as e:
        FAIL.append((name, repr(e)))
        print(f"  [FAIL] {name}: {e!r}")


def section(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


class _Cfg:
    """够用的 ConfigLoader 替身：get + 配置字典 + 落盘计数。"""

    def __init__(self, data=None):
        self.config = dict(data or {})
        self.saved = 0

    def get(self, key, default=None):
        return self.config.get(key, default)

    def _atomic_save(self, data):
        self.saved += 1


class _Mgr:
    def list_plugins(self):
        return []


class _FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


def _entry(pid, repo, name=None, tags=None, updated=0.0):
    return {"id": pid, "name": name or pid, "source_repo": repo,
            "tags": list(tags or []), "downloads": 0, "favorites": 0,
            "updated_at": updated}


def _state_srv(srv, path=None):
    """给替身摆好本机推送记录（走 object.__new__ 没跑 __init__，得自己补）。"""
    srv._publish_state_file = path
    srv._publish_state = {"published": {}, "unpublished": {}}
    srv._publish_state_readable = True
    return srv


def _make_server(by_repo=None, cfg=None, sources=None, branches=None):
    """造一个只带市场数据层依赖的 WebUIServer（不起服务、不监听端口）。

    by_repo 是各仓库的索引条目（每个仓库先读的就是它）；索引为空时才会去问
    branches，那里放按分支上架的仓库条目。
    """
    srv = object.__new__(M.WebUIServer)
    srv.config = _Cfg(cfg)
    srv.plugin_manager = _Mgr()
    srv._plugin_market_cache = {}
    _state_srv(srv)
    calls = []
    by_repo = dict(by_repo or {})
    branches = dict(branches or {})

    async def index_entries(repo, path):
        calls.append((repo, path))
        got = by_repo.get(repo)
        if got is None:
            raise RuntimeError("模拟拉取失败")
        return got

    async def branch_entries(repo, prefix):
        calls.append((repo, prefix))
        got = branches.get(repo)
        if got is None:
            raise RuntimeError("模拟拉取失败")
        return got

    async def market_sources(repo, path):
        calls.append(("<sources>", repo, path))
        return list(sources or [])

    srv._market_branch_entries = branch_entries
    srv._market_index_entries = index_entries
    srv._market_sources = market_sources
    return srv, calls


def _method(name):
    """从 modules/webui_server.py 里整段抠出一个方法的源码（到下一个同缩进的方法为止）。"""
    i = WEBUI_SRC.find("    async def " + name + "(")
    if i < 0:
        i = WEBUI_SRC.find("    def " + name + "(")
    if i < 0:
        return ""
    ends = [WEBUI_SRC.find(m, i + 1) for m in ("\n    async def ", "\n    def ")]
    ends = [e for e in ends if e > 0]
    return WEBUI_SRC[i:min(ends) if ends else len(WEBUI_SRC)]


def _js_func(name):
    """从页面脚本里整段抠出一个函数（用于真跑前端的搜索逻辑）。"""
    start = HTML_SRC.find("function " + name + "(")
    if start < 0:
        return ""
    end = HTML_SRC.find("\n        }\n", start)
    if end < 0:
        return ""
    return HTML_SRC[start:end + len("\n        }")]


# ---------------------------------------------------------------- A 地址解析

def a1():
    """地址解析：纯 slug、网址、带子路径、.git 后缀、注释、去重、非法行。"""
    p = parse_market_repos
    return (p("a/b") == ["a/b"]
            and p("a/b\nc/d") == ["a/b", "c/d"]
            and p("https://github.com/a/b") == ["a/b"]
            and p("https://github.com/a/b/tree/main") == ["a/b"]
            and p("github.com/a/b.git") == ["a/b"]
            and p("  a/b  ") == ["a/b"]
            and p("a/b # 说明") == ["a/b"]
            and p("a/b/a/b") == ["a/b"]
            and p("a/b\n\na/b\n") == ["a/b"]
            and p("没斜杠") == []
            and p("") == []
            and p(None) == [])


# ---------------------------------------------------------------- B 分流

def b1():
    """第三方没填地址：成功返回空列表并明确提示，一次网络都不打。"""
    srv, calls = _make_server()
    out = asyncio.run(srv._plugins_market_payload(False, "latest", "thirdparty"))
    return (out["success"] is True and out["count"] == 0 and not calls
            and out["market"] == "thirdparty"
            and any("第三方市场地址" in w for w in out["warnings"]))


def b2():
    """第三方按地址逐个扫：条目合并，来源各自记在 source_repo 上。"""
    srv, calls = _make_server({
        "a/one": {"entries": [_entry("p1", "a/one")], "warnings": [], "insecure": False},
        "b/two": {"entries": [_entry("p2", "b/two")], "warnings": [], "insecure": False},
    }, {"plugin_market_thirdparty": "a/one\nb/two"})
    out = asyncio.run(srv._plugins_market_payload(False, "latest", "thirdparty"))
    return (sorted(p["id"] for p in out["plugins"]) == ["p1", "p2"]
            and sorted(p["source_repo"] for p in out["plugins"]) == ["a/one", "b/two"]
            and [c[0] for c in calls] == ["a/one", "b/two"]
            and out["markets"] == ["a/one", "b/two"])


def b3():
    """官方市场只扫配置里的那一个仓库。"""
    srv, calls = _make_server({
        "own/repo": {"entries": [_entry("p1", "own/repo")], "warnings": [], "insecure": False},
        "other/x": {"entries": [_entry("p2", "other/x")], "warnings": [], "insecure": False},
    }, {"plugin_market_repo": "own/repo", "plugin_market_thirdparty": "other/x"})
    out = asyncio.run(srv._plugins_market_payload(False, "latest", "official"))
    return ([p["id"] for p in out["plugins"]] == ["p1"]
            and [c[0] for c in calls] == ["own/repo"]
            and out["market"] == "official")


# ---------------------------------------------------------------- C 容错

def c1():
    """第三方里有一个仓库拉不动：只记一条带仓库名的 warning，其它照常。"""
    srv, _ = _make_server({
        "a/one": {"entries": [_entry("p1", "a/one")], "warnings": [], "insecure": False},
    }, {"plugin_market_thirdparty": "a/one\nb/two"})
    out = asyncio.run(srv._plugins_market_payload(False, "latest", "thirdparty"))
    return (out["success"] is True and [p["id"] for p in out["plugins"]] == ["p1"]
            and any(w.startswith("b/two") for w in out["warnings"]))


def c2():
    """已经带了仓库名的 warning 不再套一层前缀，其它 warning 补上仓库名。"""
    srv, _ = _make_server({
        "a/one": {"entries": [], "warnings": [], "insecure": False},
        "b/two": {"entries": [], "warnings": [], "insecure": False},
    }, {"plugin_market_thirdparty": "a/one\nb/two"}, branches={
        "a/one": {"entries": [], "warnings": ["a/one 上没有以「lovomo_plugin」开头的分支"],
                  "insecure": False},
        "b/two": {"entries": [], "warnings": ["拿不到分支提交时间：X"], "insecure": False},
    })
    out = asyncio.run(srv._plugins_market_payload(False, "latest", "thirdparty"))
    return (out["warnings"][0] == "a/one 上没有以「lovomo_plugin」开头的分支"
            and out["warnings"][1] == "b/two：拿不到分支提交时间：X")


# ---------------------------------------------------------------- D 缓存

def d1():
    """官方与第三方各自缓存：切换市场不串条目，同市场第二次不再打网络。"""
    srv, calls = _make_server({
        "own/repo": {"entries": [_entry("official-one", "own/repo")],
                     "warnings": [], "insecure": False},
        "a/one": {"entries": [_entry("third-one", "a/one")],
                  "warnings": [], "insecure": False},
    }, {"plugin_market_repo": "own/repo", "plugin_market_thirdparty": "a/one"})
    off = asyncio.run(srv._plugins_market_payload(False, "latest", "official"))
    third = asyncio.run(srv._plugins_market_payload(False, "latest", "thirdparty"))
    off2 = asyncio.run(srv._plugins_market_payload(False, "latest", "official"))
    return ([p["id"] for p in off["plugins"]] == ["official-one"]
            and [p["id"] for p in third["plugins"]] == ["third-one"]
            and [p["id"] for p in off2["plugins"]] == ["official-one"]
            and len(calls) == 2)


def d2():
    """改了第三方地址就等于换了缓存键，不会拿到旧地址的结果。"""
    srv, calls = _make_server({
        "a/one": {"entries": [_entry("p1", "a/one")], "warnings": [], "insecure": False},
        "b/two": {"entries": [_entry("p2", "b/two")], "warnings": [], "insecure": False},
    }, {"plugin_market_thirdparty": "a/one"})
    first = asyncio.run(srv._plugins_market_payload(False, "latest", "thirdparty"))
    srv.config.config["plugin_market_thirdparty"] = "b/two"
    second = asyncio.run(srv._plugins_market_payload(False, "latest", "thirdparty"))
    return ([p["id"] for p in first["plugins"]] == ["p1"]
            and [p["id"] for p in second["plugins"]] == ["p2"]
            and len(calls) == 2)


def d3():
    """索引里有条目就用索引；索引空时回退扫分支，官方与第三方同一套口径。"""
    srv, _ = _make_server({
        "own/repo": {"entries": [_entry("from-index", "own/repo")],
                     "warnings": [], "insecure": False},
    }, {"plugin_market_repo": "own/repo"})
    off = asyncio.run(srv._plugins_market_payload(False, "latest", "official"))
    srv2, _ = _make_server({
        "a/one": {"entries": [], "warnings": [], "insecure": False},
    }, {"plugin_market_thirdparty": "a/one"}, branches={
        "a/one": {"entries": [_entry("from-branch", "a/one")],
                  "warnings": [], "insecure": False},
    })
    third = asyncio.run(srv2._plugins_market_payload(False, "latest", "thirdparty"))
    return (off["source"] == "index"
            and [p["id"] for p in off["plugins"]] == ["from-index"]
            and third["source"] == "branches"
            and [p["id"] for p in third["plugins"]] == ["from-branch"])


def d4():
    """市场数据里带回原始地址文本，供前端回填输入框。"""
    srv, _ = _make_server({}, {"plugin_market_thirdparty": "a/one\nb/two"})
    out = asyncio.run(srv._plugins_market_payload(False, "latest", "thirdparty"))
    return out["market_text"] == "a/one\nb/two"


# ---------------------------------------------------------------- E 保存接口

def e1():
    """保存第三方地址：解析结果回给前端、落盘、清空市场缓存。"""
    srv = object.__new__(M.WebUIServer)
    srv.config = _Cfg({"plugin_market_thirdparty": "old/x"})
    srv.plugin_manager = _Mgr()
    srv._plugin_market_cache = {"official|a/b": {"entries": [], "fetched_at": 1.0}}
    req = _FakeRequest({"text": "a/one\nb/two\n# 注释\n没斜杠\n"})
    resp = asyncio.run(srv.handle_plugins_market_sources(req))
    out = json.loads(resp.body)
    return (out["success"] is True and out["markets"] == ["a/one", "b/two"]
            and out["count"] == 2
            and out["text"] == "a/one\nb/two\n# 注释\n没斜杠"
            and srv.config.config["plugin_market_thirdparty"] == out["text"]
            and srv.config.saved == 1
            and srv._plugin_market_cache == {})


def e2():
    """地址全都不合法时也照常保存（原文留着），只是识别结果为 0 个。"""
    srv = object.__new__(M.WebUIServer)
    srv.config = _Cfg()
    srv.plugin_manager = _Mgr()
    srv._plugin_market_cache = {}
    _state_srv(srv)
    out = json.loads(asyncio.run(
        srv.handle_plugins_market_sources(_FakeRequest({"text": "随便写的"}))).body)
    return (out["success"] is True and out["count"] == 0
            and srv.config.config["plugin_market_thirdparty"] == "随便写的")


def e3():
    """请求体不是 JSON 时给 400，不写脏配置。"""
    srv = object.__new__(M.WebUIServer)
    srv.config = _Cfg()
    srv.plugin_manager = _Mgr()
    srv._plugin_market_cache = {}
    _state_srv(srv)

    class Bad:
        async def json(self):
            raise ValueError("不是 JSON")

    resp = asyncio.run(srv.handle_plugins_market_sources(Bad()))
    return resp.status == 400 and srv.config.saved == 0


def e4():
    """市场接口把 ?market= 透传到数据层，并挂上了保存路由。"""
    i = WEBUI_SRC.find("async def handle_plugins_market(")
    body = WEBUI_SRC[i:i + 400]
    return ('request.query.get("market")' in body
            and "_plugins_market_payload(force, sort, market)" in body
            and '"/api/plugins/market_sources", self.handle_plugins_market_sources'
            in WEBUI_SRC)


# ---------------------------------------------------------------- F 安装

def f1():
    """从第三方市场安装也能找到条目：官方找不到就接着找第三方。"""
    i = WEBUI_SRC.find("async def handle_plugins_install_remote(")
    body = WEBUI_SRC[i:i + 900]
    return ('for kind in ("official", "thirdparty")' in body
            and "_plugins_market_payload(market=kind)" in body)


# ---------------------------------------------------------------- G 发布提示

def g1():
    """发布成功要落一行 [插件发布] 日志，且这行 print 确实挂在日志重定向上。"""
    i = WEBUI_SRC.find("async def handle_plugins_publish(")
    body = WEBUI_SRC[i:i + 3200]
    return ('print(f"[插件发布] 发布成功' in body
            and "sys.stdout = StdoutRedirector(" in MAIN_SRC)


def g2():
    """前端发布成功时要弹 toast（界面提示），失败也要有反馈。"""
    i = HTML_SRC.find("api/plugins/publish',")
    body = HTML_SRC[i:i + 900]
    return "showToast(`插件" in body and "showToast('发布失败" in body


class _OwnerMgr:
    """发布路径要用的最小 plugin_manager：一个归属正确的插件。"""

    def __init__(self, root=None):
        self.root = Path(root or tempfile.mkdtemp(prefix="mkt_split_"))

    def get(self, pid):
        if pid != "meow-skin":
            return None
        return {"id": "meow-skin", "name": "喵喵皮肤", "version": "1.0.0",
                "github": "tester"}

    def list_plugins(self):
        return []


def g3():
    """发布成功要真的写出一条日志记录（走 print → 日志页读的那份缓冲）。

    用假发布函数顶掉真网络，只验「成功分支」的日志与响应。
    """
    srv = object.__new__(M.WebUIServer)
    srv.config = _Cfg({"plugin_publish_token": "t", "plugin_publish_login": "tester",
                       "plugin_market_repo": "own/repo",
                       "plugin_market_path": "plugins/index.json"})
    srv.plugin_manager = _OwnerMgr()
    srv._plugin_market_cache = {}
    _state_srv(srv)

    async def market(force=False, sort="latest", market="official"):
        return {"plugins": []}

    async def tag_names(repo, prefix):
        return []

    srv._plugins_market_payload = market
    srv._market_tags = tag_names

    async def fake_publish(**kwargs):
        return {"folder": "plugins/换肤/meow-skin", "commit": "abcdef1234567890",
                "files": [{"path": "plugins/换肤/meow-skin/plugin.yaml", "sha": "x"},
                          {"path": "plugins/换肤/meow-skin/theme.css", "sha": "y"}],
                "url": "https://github.com/own/repo/tree/main/plugins/换肤/meow-skin"}

    original = W._publish_to_github
    W._publish_to_github = fake_publish
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            resp = asyncio.run(srv.handle_plugins_publish(
                _FakeRequest({"id": "meow-skin", "message": "test"})))
    finally:
        W._publish_to_github = original
    out = json.loads(resp.body)
    return (out["success"] is True and out["folder"] == "plugins/换肤/meow-skin"
            and srv._publish_memory() == {"meow-skin": "1.0.0"}
            and buf.getvalue().strip() ==
            "[插件发布] 发布成功：meow-skin → plugins/换肤/meow-skin"
            "（提交 abcdef1，2 个文件）")


def g4():
    """这行 print 确实会落进「日志输出」页读的那份缓冲，日志记录真的看得见。"""
    redirector = M.StdoutRedirector(None)
    original = M.global_log_buffer[:]
    try:
        with contextlib.redirect_stdout(redirector):
            print("[插件发布] 发布成功：meow-skin → plugins/meow-skin")
        hit = [ln for ln in M.global_log_buffer if "发布成功" in ln]
    finally:
        M.global_log_buffer[:] = original
    return len(hit) == 1 and hit[0].endswith("plugins/meow-skin")


# ---------------------------------------------------------------- H 前端市场

def h1():
    """市场面板：官方/第三方切换 + 第三方地址输入框 + 保存按钮。"""
    for need in ('id="plugin-market-kind"', 'value="official"', 'value="thirdparty"',
                 'id="plugin-market-thirdparty"', 'id="plugin-market-sources"',
                 'id="plugin-market-sources-save"', "api/plugins/market_sources",
                 "market=${encodeURIComponent(marketKind)}"):
        if need not in HTML_SRC:
            print("      前端缺少:", need)
            return False
    return True


def h2():
    """搜索与排序并入「市场 / 分类」那一行：输入框在排序之前，打分函数齐备。"""
    for need in ('id="plugin-market-search"', "function marketSearchScore",
                 "function isSubsequence", "function renderMarketList", "marketQuery"):
        if need not in HTML_SRC:
            print("      前端缺少:", need)
            return False
    i = HTML_SRC.find('id="plugin-market-search"')
    j = HTML_SRC.find('id="plugin-market-sort"')
    bar = HTML_SRC.rfind('class="inline-form"', 0, i)
    if not (0 <= i < j and bar != -1):
        return False
    # 市场 / 分类 / 搜索 / 排序 必须落在同一个工具栏里
    for need in ('id="plugin-market-kind"', 'id="plugin-market-category"'):
        if not bar < HTML_SRC.find(need, bar) < j:
            return False
    return True


def h3():
    """搜索要在「当前市场全部插件」上做，并按分数优先展示。"""
    i = HTML_SRC.find("function renderMarketList()")
    body = HTML_SRC[i:i + 900]
    return ("marketEntries" in body and ".filter(x => x.score > 0)" in body
            and ".sort((a, b) => b.score - a.score)" in body
            and "marketEntries = data.plugins" in HTML_SRC)


def h4():
    """第三方地址框只在第三方市场下显示，回填时不覆盖未保存的改动。"""
    i = HTML_SRC.find("function syncMarketSources(")
    body = HTML_SRC[i:i + 400]
    return ("marketTextSaved" in body and "ta.value === marketTextSaved" in body
            and "marketKind === 'thirdparty'" in HTML_SRC)


# ---------------------------------------------------------------- I 版本入口

def i1():
    """程序历史版本已搬到独立面板，由 logo（Lovomo + 版本号）进入。"""
    for need in ('id="panel-app-releases"', 'id="logo-btn"',
                 "switchPanel('app-releases')",
                 "if (panelId === 'app-releases') loadReleases(false);"):
        if need not in HTML_SRC:
            print("      前端缺少:", need)
            return False
    return True


def i2():
    """插件历史页不再展示 Lovomo 项目历史版本与下载入口。"""
    i = HTML_SRC.find('id="panel-plugin-history"')
    j = HTML_SRC.find('id="panel-app-releases"')
    if i < 0 or j < i:
        return False
    seg = HTML_SRC[i:j]
    if "程序历史版本" in seg or "plugin-releases" in seg:
        print("      插件历史页里还留着:", "程序历史版本" if "程序历史版本" in seg
              else "plugin-releases")
        return False
    k = HTML_SRC.find("async function loadPluginHistoryPage")
    body = HTML_SRC[k:HTML_SRC.find("\n        }\n", k)]
    return "loadReleases" not in body


def i3():
    """插件历史页只讲插件自己的更新说明。"""
    return ("插件更新说明" in HTML_SRC and "程序历史版本" in HTML_SRC
            and "内容来自插件包内的" in HTML_SRC)


# ---------------------------------------------------------------- J 搜索打分

def j1():
    """把前端打分函数抠出来用 node 真跑：完全一致 > 前缀 > 标签 > 包含 > 模糊。"""
    node = shutil.which("node")
    if not node:
        print("      跳过：环境里没有 node")
        return True
    js = _js_func("isSubsequence") + "\n" + _js_func("marketSearchScore")
    if "function marketSearchScore" not in js:
        print("      没抠出打分函数")
        return False
    script = js + """
const items = {
  a: {id: 'a', name: '喵喵皮肤', tags: []},
  b: {id: 'b', name: '喵喵皮肤·回复追加', tags: ['meow']},
  c: {id: 'c', name: '樱花粉主题', tags: ['meow-skin']},
  d: {id: 'd', name: 'meow-skin', tags: []},
};
const out = {};
for (const q of ['喵喵皮肤', 'meow', '皮肤', 'msk', '不存在']) {
  out[q] = Object.keys(items).map(k => marketSearchScore(items[k], q));
}
console.log(JSON.stringify(out));
"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "score.js"
        path.write_text(script, encoding="utf-8")
        proc = subprocess.run([node, str(path)], capture_output=True,
                              text=True, encoding="utf-8", timeout=60)
    if proc.returncode != 0:
        print("      node 退出码:", proc.returncode, proc.stderr[:200])
        return False
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    want = {
        "喵喵皮肤": [100, 80, 0, 0],   # 完全一致 / 前缀 / 不沾边 / 不沾边
        "meow": [0, 70, 50, 80],       # 标签命中 / 标签包含 / 名字前缀
        "皮肤": [60, 60, 0, 0],        # 名字包含
        "msk": [0, 0, 20, 30],         # 标签模糊 / 名字模糊
        "不存在": [0, 0, 0, 0],
    }
    for q, expect in want.items():
        if got.get(q) != expect:
            print(f"      查询 {q!r}: 期望 {expect}，实际 {got.get(q)}")
            return False
    return True


def j2():
    """排序键的语义：分数高的排前面，0 分的被滤掉。"""
    scores = {"a": 100, "b": 80, "c": 20}
    order = sorted(scores.items(), key=lambda kv: -kv[1])
    kept = [k for k, v in order if v > 0]
    return kept == ["a", "b", "c"] and [k for k in scores if scores[k] == 0] == []


# ---------------------------------------------------------------- K 分支分页

def _branch_srv(pages):
    """只带 _fetch_releases 的服务器替身，pages 是逐页返回的分支名列表。"""
    srv = object.__new__(M.WebUIServer)
    seen = []

    async def fetch(api_url):
        seen.append(api_url)
        idx = int(api_url.rsplit("page=", 1)[1]) - 1
        names = pages[idx] if idx < len(pages) else []
        return [{"name": n} for n in names], False

    srv._fetch_releases = fetch
    return srv, seen


def k1():
    """分支超过一页时按页取全，第 101 个之后不会被当成不存在。"""
    pages = [[f"b{i}" for i in range(100)], [f"b{i}" for i in range(100, 105)]]
    srv, seen = _branch_srv(pages)
    out, insecure = asyncio.run(srv._market_branches("me/repo"))
    return (len(out) == 105 and len(seen) == 2 and insecure is False
            and "page=1" in seen[0] and "page=2" in seen[1])


def k2():
    """不足一页就停手；一直满页时也只在页数上限处收手。"""
    srv, seen = _branch_srv([[f"b{i}" for i in range(5)]])
    out, _ = asyncio.run(srv._market_branches("me/repo"))
    if len(out) != 5 or len(seen) != 1:
        return False
    srv2, seen2 = _branch_srv([[f"b{p}_{i}" for i in range(100)]
                               for p in range(M.MAX_BRANCH_PAGES + 3)])
    out2, _ = asyncio.run(srv2._market_branches("me/repo"))
    return (len(seen2) == M.MAX_BRANCH_PAGES
            and len(out2) == 100 * M.MAX_BRANCH_PAGES)


# ------------------------------------------- L 发布状态与市场索引

def _publish_srv(payload, tags):
    """只带发布状态依赖的服务：市场 payload 与版本标签都由调用方给。"""
    srv = object.__new__(M.WebUIServer)
    srv.config = _Cfg({"plugin_market_repo": "own/repo",
                       "plugin_market_branch_prefix": "lovomo_plugin"})
    srv.plugin_manager = _Mgr()
    srv._plugin_market_cache = {}
    _state_srv(srv)

    async def market(force=False, sort="latest", market="official"):
        return payload

    async def tag_names(repo, prefix):
        return list(tags)

    srv._plugins_market_payload = market
    srv._market_tags = tag_names
    return srv


def l1():
    """市场索引里的版本就是「已上架」版本 —— 发布写的就是这份索引。"""
    srv = _publish_srv({"plugins": [{"id": "meow-skin", "version": "9.9.9",
                                     "source": "index"}]}, [])
    return asyncio.run(srv._published_versions()) == {"meow-skin": "9.9.9"}


def l2():
    """索引与版本标签取并集、版本取大的那个；程序自己的版本标签不算插件。"""
    srv = _publish_srv({"plugins": [{"id": "meow-skin", "version": "1.0.0",
                                     "branch": "lovomo_plugin_meow-skin"}]},
                       ["lovomo_plugin_meow-skin-v1.0.1",
                        "lovomo_plugin_device-status-v1.0.0",
                        "1.2.0.0"])
    return asyncio.run(srv._published_versions()) == {"meow-skin": "1.0.1",
                                                     "device-status": "1.0.0"}


def l3():
    """分支被删掉（市场整条拉不动）时，仍能从版本标签认出已发布过的版本。"""
    srv = object.__new__(M.WebUIServer)
    srv.config = _Cfg({"plugin_market_repo": "own/repo",
                       "plugin_market_branch_prefix": "lovomo_plugin"})
    srv.plugin_manager = _Mgr()
    srv._plugin_market_cache = {}
    _state_srv(srv)

    async def boom(*args, **kwargs):
        raise RuntimeError("模拟市场拉取失败")

    async def tag_names(repo, prefix):
        return ["lovomo_plugin_meow-skin-v1.0.1"]

    srv._plugins_market_payload = boom
    srv._market_tags = tag_names
    return asyncio.run(srv._published_versions()) == {"meow-skin": "1.0.1"}


def l4():
    """版本标签反解：只认 `<前缀>_<id>-v<版本>`，程序版本标签不算。"""
    from modules.plugin_publisher import parse_version_tag, version_tag
    for pid, ver in (("meow-skin", "1.0.1"), ("device-status", "1.0.0"),
                     ("a-vb", "2.0.0"), ("meow-skin", "1.0.1-beta")):
        if parse_version_tag(version_tag("lovomo_plugin_" + pid, ver),
                             "lovomo_plugin") != (pid, ver):
            print("      反解不对:", pid, ver)
            return False
    return (parse_version_tag("1.2.0.0", "lovomo_plugin") == ("", "")
            and parse_version_tag("lovomo_plugin_meow-skin",
                                  "lovomo_plugin") == ("", ""))


def l5():
    """市场索引读的是仓库的默认分支，不再写死 main。"""
    srv = object.__new__(M.WebUIServer)
    srv.config = _Cfg({})
    seen = []

    async def fetch(url):
        seen.append(url)
        if "/repos/own/repo" in url:
            return {"default_branch": "trunk"}, False
        return [{"id": "x", "name": "x", "version": "1.0",
                 "download": "https://github.com/own/repo/raw/trunk/x.zip"}], False

    srv._fetch_releases = fetch
    got = asyncio.run(srv._market_index_entries("own/repo", "plugins/index.json"))
    return (len(got["entries"]) == 1
            and any("/own/repo/trunk/plugins/index.json" in u for u in seen)
            and not any("/own/repo/main/" in u for u in seen))


def l6():
    """默认分支查不到时退回 main，不因为多打一次接口就把市场索引整条废掉。"""
    srv = object.__new__(M.WebUIServer)
    srv.config = _Cfg({})
    seen = []

    async def fetch(url):
        seen.append(url)
        if "/repos/own/repo" in url:
            raise RuntimeError("模拟网络失败")
        return [], False

    srv._fetch_releases = fetch
    asyncio.run(srv._market_index_entries("own/repo", "plugins/index.json"))
    return any("/own/repo/main/plugins/index.json" in u for u in seen)


def l7():
    """接线守卫：发布状态真的去读版本标签，索引与标签取版本较大的那个。"""
    body = _method("_published_versions")
    return ("_market_tags(" in body and "parse_version_tag" in body
            and "is_newer(" in body)


# ------------------------------------------- M 来源清单（第三方进官方市场）

SOURCES_CFG = {"plugin_market_repo": "own/repo",
               "plugin_market_sources_path": "plugins/market-sources.txt"}


def _real_sources(text, default_branch="trunk"):
    """带真 `_market_sources` 的服务：默认分支与清单原文都由调用方给。"""
    srv = object.__new__(M.WebUIServer)
    srv.config = _Cfg({})
    seen = []

    async def fetch(api_url):
        return {"default_branch": default_branch}, False

    async def gh(url, kind="json", **kw):
        seen.append(url)
        if isinstance(text, Exception):
            raise text
        return text, False

    srv._fetch_releases = fetch
    srv._github_fetch = gh
    return srv, seen


def m1():
    """官方市场读完自己的索引后，再按清单扫第三方仓库并合并。"""
    srv, calls = _make_server({
        "own/repo": {"entries": [_entry("p1", "own/repo")], "warnings": [], "insecure": False},
        "a/one": {"entries": [_entry("p2", "a/one")], "warnings": [], "insecure": False},
    }, SOURCES_CFG, sources=["a/one"])
    out = asyncio.run(srv._plugins_market_payload(False, "latest", "official"))
    return (sorted(p["id"] for p in out["plugins"]) == ["p1", "p2"]
            and sorted(p["source_repo"] for p in out["plugins"]) == ["a/one", "own/repo"]
            and [c[0] for c in calls] == ["own/repo", "<sources>", "a/one"]
            and calls[1] == ("<sources>", "own/repo", "plugins/market-sources.txt"))


def m2():
    """没配来源清单路径就一次都不去问它，官方市场只读自己那份索引。"""
    srv, calls = _make_server({
        "own/repo": {"entries": [_entry("p1", "own/repo")], "warnings": [], "insecure": False},
    }, {"plugin_market_repo": "own/repo"})
    out = asyncio.run(srv._plugins_market_payload(False, "latest", "official"))
    return ([p["id"] for p in out["plugins"]] == ["p1"]
            and [c[0] for c in calls] == ["own/repo"])


def m3():
    """清单读不到（没这个文件 / 网络失败）当没有额外来源，不牵连官方市场。"""
    srv, _ = _real_sources(RuntimeError("模拟 404"))
    return asyncio.run(srv._market_sources("own/repo", "plugins/market-sources.txt")) == []


def m4():
    """清单里写回市场仓库自己会被剔掉，读的是仓库的默认分支。"""
    srv, seen = _real_sources("own/repo\na/one\n\n# 注释\n不是地址\na/one\n")
    got = asyncio.run(srv._market_sources("own/repo", "plugins/market-sources.txt"))
    return (got == ["a/one"]
            and seen == ["https://raw.githubusercontent.com/own/repo/trunk"
                         "/plugins/market-sources.txt"])


def m5():
    """清单再长也只认前 N 个仓库（GitHub 匿名接口有次数限制）。"""
    text = "\n".join(f"u{i}/r{i}" for i in range(M.MARKET_SOURCE_REPOS_MAX + 10))
    srv, _ = _real_sources(text)
    got = asyncio.run(srv._market_sources("own/repo", "plugins/market-sources.txt"))
    return len(got) == M.MARKET_SOURCE_REPOS_MAX and got[0] == "u0/r0"


def m6():
    """来源仓库里有一个拉不动：只记一条带仓库名的 warning，官方条目照常。"""
    srv, _ = _make_server({
        "own/repo": {"entries": [_entry("p1", "own/repo")], "warnings": [], "insecure": False},
        "a/one": {"entries": [_entry("p2", "a/one")], "warnings": [], "insecure": False},
    }, SOURCES_CFG, sources=["a/one", "b/two"])
    out = asyncio.run(srv._plugins_market_payload(False, "latest", "official"))
    return (out["success"] is True
            and sorted(p["id"] for p in out["plugins"]) == ["p1", "p2"]
            and any(w.startswith("b/two") for w in out["warnings"]))


def m7():
    """发布状态也要认来源仓库的版本标签，否则别人的插件会一直亮发布按钮。"""
    srv = object.__new__(M.WebUIServer)
    srv.config = _Cfg(dict(SOURCES_CFG))
    srv.plugin_manager = _Mgr()
    srv._plugin_market_cache = {}
    _state_srv(srv)

    async def market(force=False, sort="latest", market="official"):
        return {"plugins": []}

    async def sources(repo, path):
        return ["a/one"]

    async def tag_names(repo, prefix):
        return (["lovomo_plugin_meow-skin-v1.0.1"] if repo == "own/repo"
                else ["lovomo_plugin_other-v2.0.0"])

    srv._plugins_market_payload = market
    srv._market_sources = sources
    srv._market_tags = tag_names
    return asyncio.run(srv._published_versions()) == {"meow-skin": "1.0.1",
                                                     "other": "2.0.0"}


def m8():
    """接线守卫：官方市场真的按清单去扫，发布状态也真的带上了这些仓库。"""
    reader = _method("_market_sources")
    if not reader:
        print("      没有 _market_sources")
        return False
    for need in ("parse_market_repos", "MARKET_SOURCE_REPOS_MAX", "RAW_TMPL"):
        if need not in reader:
            print("      _market_sources 里缺少:", need)
            return False
    return (M.MARKET_SOURCE_REPOS_MAX > 0
            and "_market_sources(repo, sources_path)" in _method("_plugins_market_payload")
            and "extra = await self._market_scan_repos(" in _method("_plugins_market_payload")
            and "plugin_market_sources_path" in _method("_published_versions")
            and "_market_sources(repo, sources_path)" in _method("_published_versions"))


# ------------------------------------------- N 分类目录 / 版本回退 / 下架 / 二级密码

def _publisher_src():
    return (ROOT / "modules" / "plugin_publisher.py").read_text(encoding="utf-8")


class _InstalledMgr:
    """已装插件：一个归属正确、写了分类的插件。"""

    def __init__(self, category="换肤"):
        self.root = Path(tempfile.mkdtemp(prefix="mkt_pub_"))
        self.category = category

    def _info(self, pid):
        return {"id": pid, "name": "喵喵皮肤", "version": "1.0.0",
                "github": "tester", "owners": ["tester"],
                "category": self.category}

    def get(self, pid):
        return self._info(pid)

    def list_plugins(self):
        return [self._info("meow-skin")]


def _market_index_srv(state, cfg=None):
    """市场索引内容可变的替身：state 里的 entries/tags 就是「市场现在长什么样」。

    发布状态必须按最新索引判定，所以这里刻意不碰 _plugin_market_cache。
    """
    srv = object.__new__(M.WebUIServer)
    srv.config = _Cfg(cfg or {"plugin_market_repo": "own/repo",
                              "plugin_market_path": "plugins/index.json",
                              "plugin_market_branch_prefix": "lovomo_plugin",
                              "plugin_publish_token": "t",
                              "plugin_publish_login": "tester"})
    srv.plugin_manager = _Mgr()
    srv._plugin_market_cache = {}
    _state_srv(srv)

    async def index_entries(repo, path):
        return {"entries": [dict(e) for e in state["entries"]],
                "warnings": [], "insecure": False}

    async def branch_entries(repo, prefix):
        return {"entries": [], "warnings": [], "insecure": False}

    async def releases(repo, ids):
        return []

    async def default_branch(repo):
        return "main"

    async def tag_names(repo, prefix):
        return list(state.get("tags") or [])

    srv._market_index_entries = index_entries
    srv._market_branch_entries = branch_entries
    srv._market_releases = releases
    srv._market_default_branch = default_branch
    srv._market_tags = tag_names
    return srv


def n1():
    """分类目录：插件写进 `plugins/<分类>/<插件id>`，清单没写分类就归「其他」。"""
    from modules.market import clean_category, plugin_category, plugin_folder
    return (plugin_folder("plugins/index.json", "换肤", "meow-skin")
            == "plugins/换肤/meow-skin"
            and plugin_folder("plugins/index.json", "", "meow-skin")
            == "plugins/其他/meow-skin"
            and plugin_folder("plugins/index.json", "换肤", "")
            == "plugins/换肤"
            and plugin_category({"category": "换肤"}) == "换肤"
            and plugin_category({}) == "其他"
            and clean_category("a/b:c") == "abc"
            and clean_category("..") == "其他"
            and clean_category("x" * 40) == "x" * 24)


def n2():
    """分类进得了索引条目与市场条目，市场才能按它筛选。"""
    from modules.market import build_entry, build_index_entry
    idx = build_index_entry({"id": "meow-skin", "category": "换肤"}, "meow-skin",
                            "own/repo", "main", "plugins/换肤/meow-skin",
                            "https://x/y.zip", "2026-09-24")
    ent = build_entry("main", "", {"id": "p", "category": "工具"},
                      "https://raw.githubusercontent.com/own/repo/main",
                      "own/repo", {})
    return (idx["category"] == "换肤"
            and "plugins/换肤/meow-skin" in idx["logo_url"]
            and ent["category"] == "工具")


def n3():
    """发布真的按分类落目录，并且把改分类前的旧目录一并删掉。"""
    src = _publisher_src()
    return ("folder = plugin_folder(index_path, plugin_category(manifest_data), plugin_id)"
            in src
            and "old_folders" in src
            and "deleted=stale" in src
            and "plugins/<分类>/<插件id>" in src)


def n4():
    """本地版本不高于已上架版本时后端直接拦下，不只靠前端藏按钮。"""
    srv = object.__new__(M.WebUIServer)
    srv.config = _Cfg({"plugin_publish_token": "t", "plugin_publish_login": "tester",
                       "plugin_market_repo": "own/repo",
                       "plugin_market_path": "plugins/index.json"})
    srv.plugin_manager = _OwnerMgr()
    srv._plugin_market_cache = {}
    _state_srv(srv)

    async def market(force=False, sort="latest", market="official"):
        return {"plugins": [{"id": "meow-skin", "version": "9.9.9"}]}

    async def tag_names(repo, prefix):
        return []

    srv._plugins_market_payload = market
    srv._market_tags = tag_names

    async def boom(**kwargs):
        raise AssertionError("版本没往上走时不该真的去推送")

    original = W._publish_to_github
    W._publish_to_github = boom
    try:
        resp = asyncio.run(srv.handle_plugins_publish(_FakeRequest({"id": "meow-skin"})))
    finally:
        W._publish_to_github = original
    out = json.loads(resp.body)
    return (resp.status == 400 and out["success"] is False
            and "版本只能往上走" in out["error"])


def n5():
    """刚发布成功的版本立刻算「已上架」——市场镜像有缓存，否则会重复推送。"""
    srv = _publish_srv({"plugins": [{"id": "meow-skin", "version": "1.0.0"}]}, [])
    srv._remember_published("meow-skin", "1.0.1")
    newer = asyncio.run(srv._published_versions()) == {"meow-skin": "1.0.1"}
    srv._publish_state = {"published": {"meow-skin": {"version": "0.9",
                                                      "ts": time.time()}},
                          "unpublished": {}}
    older = asyncio.run(srv._published_versions()) == {"meow-skin": "1.0.0"}
    return newer and older


def n6():
    """下架要撤掉四样东西：索引条目、插件目录、版本标签与对应的 Release。"""
    src = _publisher_src()
    return ("async def unpublish_plugin(" in src
            and "remove_index_entry" in src
            and "delete_ref(token, repo, f\"tags/{tag}\")" in src
            and "delete_release(" in src
            and 'r.add_post("/api/plugins/unpublish", self.handle_plugins_unpublish)'
            in WEBUI_SRC
            and "self._remember_unpublished(pid)" in WEBUI_SRC)


def n7():
    """下架列表来自市场索引里属于当前账号的条目，本地没装的也要能下架。"""
    srv = object.__new__(M.WebUIServer)
    srv._plugin_market_cache = {}

    async def market(force=False, sort="latest", market="official"):
        return {"plugins": [
            {"id": "mine", "name": "我的", "version": "1.0.0", "github": "tester",
             "category": "换肤"},
            {"id": "theirs", "name": "别人的", "version": "2.0.0",
             "source_repo": "bob/repo"},
            {"id": "anon", "name": "没写归属", "version": "3.0.0"},
        ]}

    srv._plugins_market_payload = market
    got = asyncio.run(srv._published_entries("tester", "plugins/index.json"))
    return (len(got) == 1 and got[0]["id"] == "mine"
            and got[0]["folder"] == "plugins/换肤/mine"
            and asyncio.run(srv._published_entries("", "plugins/index.json")) == [])


def n8():
    """发布面板真的长了「下架」按钮，且真的去调接口。"""
    return ("api/plugins/unpublish'," in HTML_SRC
            and "plugin-unpublish-btn" in HTML_SRC
            and "已上架 v" in HTML_SRC
            and "本地未安装" in HTML_SRC)


def n9():
    """二级密码：敏感接口清单 + 中间件拦截 + 解锁接口 + 改密码即作废。"""
    return ('_WEBUI_PASSWORD_KEYS = ("webui_password", "webui_second_password")' in SECURITY_SRC
            and "_SECOND_PASSWORD_PATHS" in SECURITY_SRC
            and "def _needs_second_password(" in SECURITY_SRC
            and "_needs_second_password(path) and server._second_password" in WEBCOMMON_SRC
            and "need_second_password" in WEBCOMMON_SRC
            and 'self.app.router.add_post("/api/auth/second", self.handle_auth_second)'
            in WEBUI_SRC
            and "def _second_unlocked" in WEBUI_SRC
            and "second != self._second_password" in WEBUI_SRC
            and '"webui_second_password": ""' in CONFIG_LOADER_SRC
            and '"webui_second_unlock_minutes": 30' in CONFIG_LOADER_SRC)


def n10():
    """二级密码前端：被拦下时弹解锁框，解锁成功后自动重放那次请求。"""
    return ('id="second-modal"' in HTML_SRC and 'id="second-pass"' in HTML_SRC
            and "function askSecondPassword" in HTML_SRC
            and "async function fetchGuarded" in HTML_SRC
            and "need_second_password" in HTML_SRC
            and "api/auth/second" in HTML_SRC)


def n10b():
    """刚解锁成功后的一小段时间内不再重复弹框。

    解锁前就已发出的请求，它们的 403 会在解锁完成之后才回到页面；再弹一次框
    就是用户说的「不知道为什么要输两次才行」。
    """
    js = _js_func("askSecondPassword")
    if "function askSecondPassword" not in js:
        print("      没抠出 askSecondPassword")
        return False
    return ("Date.now() - secondUnlockedAt < " in js
            and "if (ok) secondUnlockedAt = Date.now()" in js)


def n11():
    """市场按分类筛选：下拉里带数量，且与搜索叠加。"""
    return ('id="plugin-market-category"' in HTML_SRC
            and "function renderMarketCategories" in HTML_SRC
            and "function marketCatLabel" in HTML_SRC
            and "marketEntries.filter(inMarketCategory)" in HTML_SRC)


def n12():
    """本地示例插件与索引都按分类目录摆好了。"""
    root = ROOT / "plugins"
    return ((root / "换肤" / "meow-skin" / "plugin.yaml").is_file()
            and (root / "换肤" / "sakura-theme" / "plugin.yaml").is_file()
            and (root / "工具" / "device-status" / "plugin.yaml").is_file()
            and "category: 换肤" in (root / "sources" / "meow-skin"
                                     / "plugin.yaml").read_text(encoding="utf-8")
            and "plugins/换肤/meow-skin" in (root / "index.json").read_text(
                encoding="utf-8"))


def _run_node(script, name):
    """把一段前端代码交给 node 真跑，返回 stdout 最后一行解析出的 JSON。"""
    node = shutil.which("node")
    if not node:
        print("      跳过：环境里没有 node")
        return None
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / (name + ".js")
        path.write_text(script, encoding="utf-8")
        proc = subprocess.run([node, str(path)], capture_output=True,
                              text=True, encoding="utf-8", timeout=60)
    if proc.returncode != 0:
        print("      node 退出码:", proc.returncode, proc.stderr[:300])
        return None
    return json.loads(proc.stdout.strip().splitlines()[-1])


def n13():
    """分类筛选真跑：没写分类的算「其他」，标签按语言换括号。"""
    js = _js_func("marketCatLabel") + "\n" + _js_func("inMarketCategory")
    if "function marketCatLabel" not in js or "function inMarketCategory" not in js:
        print("      没抠出分类函数")
        return False
    got = _run_node("let i18nLang = 'zh';\n"
                    "function t(s){ return s; }\n"
                    "let marketCategory = '';\n"
                    "const marketEntries = [\n"
                    "  {id:'a', category:'换肤'}, {id:'b', category:'换肤'},\n"
                    "  {id:'c', category:'工具'}, {id:'d'},\n"
                    "];\n" + js + """
const all = marketEntries.filter(inMarketCategory).map(p => p.id);
marketCategory = '换肤';
const skin = marketEntries.filter(inMarketCategory).map(p => p.id);
marketCategory = '其他';
const other = marketEntries.filter(inMarketCategory).map(p => p.id);
const zh = marketCatLabel('换肤', 2);
i18nLang = 'en';
console.log(JSON.stringify({all, skin, other, zh, en: marketCatLabel('换肤', 2)}));
""", "cat")
    if got is None:
        return False
    want = {"all": ["a", "b", "c", "d"], "skin": ["a", "b"], "other": ["d"],
            "zh": "换肤（2）", "en": "换肤 (2)"}
    if got != want:
        print("      期望", want, "实际", got)
        return False
    return True


def n14():
    """二级密码真跑：被拦下时解锁并自动重放；普通 403 不弹框；取消就不重放。"""
    js = _js_func("fetchGuarded")
    if "function fetchGuarded" not in js:
        print("      没抠出 fetchGuarded")
        return False
    js = "async " + js   # _js_func 只从 function 起截，async 得补回来
    got = _run_node("""
const API_BASE = '';
let calls = 0, asked = 0, answer = true, serverUnlocked = false;
async function askSecondPassword(){
  asked++;
  if (answer) serverUnlocked = true;   // 输对密码 = 服务端解锁
  return answer;
}
function mk(status, body){
  return {status, clone(){ return {json: async () => body}; }};
}
async function fetch(url, opts){
  calls++;
  if (url === '/locked') return mk(serverUnlocked ? 200 : 403,
                                   {need_second_password: !serverUnlocked});
  if (url === '/cross') return mk(403, {error: '跨站请求已被拒绝'});
  return mk(200, {});
}
""" + js + """
(async () => {
  const a = await fetchGuarded('/ok', {});
  const b = await fetchGuarded('/locked', {});
  answer = false;                      // 用户点了取消
  serverUnlocked = false;
  let cancelled = '';
  try { await fetchGuarded('/locked', {}); } catch (e) { cancelled = e.message; }
  const c = await fetchGuarded('/cross', {});
  console.log(JSON.stringify({a: a.status, b: b.status, c: c.status,
                              calls, asked, cancelled}));
})();
""", "guard")
    if got is None:
        return False
    want = {"a": 200, "b": 200, "c": 403, "calls": 5, "asked": 2,
            "cancelled": "已取消：需要二级密码"}
    if got != want:
        print("      期望", want, "实际", got)
        return False
    return True


def n15():
    """发布状态不吃 30 分钟的市场条目缓存：别人在 GitHub 上把插件删掉，
    立刻再打开面板就不能还显示「已上架」。"""
    state = {"entries": [{"id": "meow-skin", "name": "喵喵皮肤", "version": "1.0.0",
                          "github": "tester", "category": "换肤",
                          "folder": "plugins/换肤/meow-skin"}], "tags": []}
    srv = _market_index_srv(state)
    srv.plugin_manager = _InstalledMgr()
    first = json.loads(asyncio.run(
        srv.handle_plugins_publish_status(_FakeRequest({}))).body)
    state["entries"] = []
    second = json.loads(asyncio.run(
        srv.handle_plugins_publish_status(_FakeRequest({}))).body)
    return (len(first["published"]) == 1
            and first["plugins"][0]["needs_publish"] is False
            and second["published"] == []
            and second["plugins"][0]["needs_publish"] is True
            and second["plugins"][0]["published_version"] == "")


def n16():
    """本机推送/下架记录要落盘：程序重启、镜像还没刷新时，同一版本仍拦得住，
    下架过的也不再显示「已上架」。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "publish_state.json"
        srv = _market_index_srv({"entries": [], "tags": []})
        srv._publish_state_file = path
        srv._remember_published("meow-skin", "1.0.0")
        saved = path.is_file()

        fresh = _market_index_srv({"entries": [], "tags": []})
        fresh._publish_state_file = path
        fresh._load_publish_state()
        got = fresh._publish_memory()

        fresh._remember_unpublished("meow-skin")
        stale = _market_index_srv({"entries": [
            {"id": "meow-skin", "name": "喵喵皮肤", "version": "1.0.0",
             "github": "tester", "category": "换肤"}], "tags": []})
        stale._publish_state_file = path
        stale._load_publish_state()
        versions = asyncio.run(stale._published_versions())
        entries = asyncio.run(stale._published_entries("tester", "plugins/index.json"))
        return (saved and got == {"meow-skin": "1.0.0"}
                and versions == {} and entries == [])


def n17():
    """刚推送成功、市场索引还没更新的插件，也要立刻出现在「已上架」里，
    否则发布完拿不到「下架」按钮。"""
    srv = _market_index_srv({"entries": [], "tags": []})
    srv.plugin_manager = _InstalledMgr()
    srv._remember_published("meow-skin", "1.0.0")
    got = asyncio.run(srv._published_entries("tester", "plugins/index.json"))
    return (len(got) == 1 and got[0]["id"] == "meow-skin"
            and got[0]["version"] == "1.0.0"
            and got[0]["name"] == "喵喵皮肤"
            and got[0]["folder"] == "plugins/换肤/meow-skin")


def main():
    section("A 第三方市场地址解析")
    check("纯 slug / 网址 / 子路径 / .git / 注释 / 去重 / 非法行", a1)

    section("B 官方与第三方分流")
    check("第三方没填地址：空列表 + 明确提示", b1)
    check("第三方按地址逐个扫并合并，来源各自记好", b2)
    check("官方只扫配置的那一个仓库", b3)

    section("C 容错")
    check("单个仓库拉不动不牵连其它仓库", c1)
    check("warning 不重复套仓库前缀", c2)

    section("D 缓存")
    check("官方与第三方各自分键，同市场不重复打网络", d1)
    check("改了地址就换缓存键，不拿旧结果", d2)
    check("索引优先、为空回退分支（官方与第三方同口径）", d3)
    check("回传原始地址文本供前端回填", d4)

    section("E 保存接口")
    check("保存地址：解析回传 + 落盘 + 清缓存", e1)
    check("地址全不合法也照常保存", e2)
    check("请求体不是 JSON 给 400", e3)
    check("?market= 透传 + 保存路由已挂", e4)

    section("F 从市场安装")
    check("第三方市场的插件也能装上", f1)

    section("G 发布成功提示")
    check("发布成功落 [插件发布] 日志（print → 日志输出页）", g1)
    check("发布成功/失败都弹 toast", g2)
    check("成功分支真的写出一条日志记录", g3)
    check("这条记录会出现在「日志输出」页", g4)

    section("H 前端市场面板")
    check("官方/第三方切换 + 地址框 + 保存按钮", h1)
    check("排序旁的搜索入口与打分函数齐备", h2)
    check("搜索在全部插件上做并按分数优先", h3)
    check("地址框回填不覆盖未保存改动", h4)

    section("I 程序历史版本入口")
    check("搬到独立面板并由 logo 进入", i1)
    check("插件历史页不再有项目历史版本", i2)
    check("插件历史页只讲插件自己的更新说明", i3)

    section("J 搜索打分（node 实跑）")
    check("完全一致 > 前缀 > 标签 > 包含 > 模糊", j1)
    check("分数高的排前面、0 分滤掉", j2)

    section("K 分支分页")
    check("分支超过一页时按页取全", k1)
    check("不足一页就停手，满页也有页数上限", k2)

    section("L 发布状态与市场索引")
    check("市场索引里的版本就是已上架版本", l1)
    check("索引与版本标签取并集、版本取大", l2)
    check("市场拉不动时靠标签认出已发布版本", l3)
    check("版本标签反解：只认 <前缀>_<id>-v<版本>", l4)
    check("市场索引读仓库默认分支", l5)
    check("默认分支查不到时退回 main", l6)
    check("发布状态真的接了版本标签", l7)

    section("M 来源清单（第三方进官方市场）")
    check("按清单扫第三方仓库并合并进官方市场", m1)
    check("没配清单路径就一次都不去问", m2)
    check("清单读不到当没有额外来源", m3)
    check("剔掉写回市场仓库自己的行，读默认分支", m4)
    check("清单再长也只认前 N 个仓库", m5)
    check("单个来源仓库拉不动不牵连官方条目", m6)
    check("发布状态也认来源仓库的版本标签", m7)

    section("N 分类目录 / 版本回退 / 下架 / 二级密码")
    check("分类目录：plugins/<分类>/<插件id>，缺省归「其他」", n1)
    check("分类进得了索引条目与市场条目", n2)
    check("发布按分类落目录并清掉改分类前的旧目录", n3)
    check("本地版本不高于已上架版本时后端拦下", n4)
    check("刚发布的版本立刻算已上架", n5)
    check("下架撤掉索引 / 目录 / 标签 / Release", n6)
    check("下架列表认市场索引里属于当前账号的条目", n7)
    check("发布面板长出了「下架」按钮", n8)
    check("二级密码：敏感清单 + 中间件 + 解锁接口", n9)
    check("二级密码：弹层解锁并自动重放请求", n10)
    check("二级密码：刚解锁后不再重复弹框", n10b)
    check("市场按分类筛选（带数量、与搜索叠加）", n11)
    check("本地示例插件与索引都按分类目录摆好", n12)
    check("分类筛选真跑（缺省「其他」+ 数量）", n13)
    check("二级密码真跑（拦下→解锁→重放）", n14)
    check("发布状态不吃市场条目缓存（删掉就立刻不再是已上架）", n15)
    check("推送/下架记录落盘：重启后仍拦重复推送、下架的不再算已上架", n16)
    check("刚推送的立刻能下架（索引没更新也算已上架）", n17)
    check("接线守卫：市场与发布状态都带上了清单", m8)

    print("\n" + "=" * 70)
    print(f"结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
    for name, err in FAIL:
        print(f"  - {name}: {err}")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
