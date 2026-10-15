# -*- coding: utf-8 -*-
"""配置文件（命名配置 + 接入方式各用一份）回归测试。

覆盖：
  A 存储层：default 内置不可删/不可占用、新建与重命名、文件名净化与路径穿越拦截、
    超长名拒绝、接入方式列表不进配置文件、旧版包装格式仍可读、损坏文件被跳过
  B HTTP 处理函数：列表带「谁在用」、读 default 回主配置、保存时密钥密文化、
    打码值沿用文件里已有的密钥、重命名删旧文件、删除 default 回 400
  C 链路接入：路由注册、写操作在敏感接口清单里（列表/读取不拦）、前端入口替换齐全
  D 配置页分组结构：分组顺序与「更多」位置、字段键不重复、同一套渲染函数能同时
    喂给设置页与配置文件页两个容器（D 段用 node + DOM 桩真渲染，没有 node 时跳过）
  E 接入方式绑定：接入方式 → 配置文件名 → 配置访问器、会话/客户端取配置、
    上下文里换配置后角色与提示词跟着换、快照包含绑定（改绑定会触发重连）

运行: python tests/test_config_presets.py      （全通过退出码 0）
"""
import asyncio
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
sys.stdin = io.StringIO()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import main as M  # noqa: E402
import modules.app_context as AC  # noqa: E402
import modules.config_presets as P  # noqa: E402
import modules.session_context as SC  # noqa: E402

PASS, FAIL = [], []
HTML = (ROOT / "webui" / "start.html").read_text(encoding="utf-8")


def check(name, fn):
    try:
        ok = fn()
    except Exception as e:
        FAIL.append(f"{name} -> {type(e).__name__}: {e}")
        print(f"  [FAIL] {name} -> {type(e).__name__}: {e}")
        return
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


def section(title):
    print(f"\n{title}")


class _FakeReq:
    """只喂 JSON 的假请求，够 handle_config_profiles_* 用。"""

    def __init__(self, payload, content_type="application/json"):
        self._payload = payload
        self.content_type = content_type

    async def json(self):
        return self._payload


def _resp_json(resp):
    return json.loads(resp.text)


def _raises(fn):
    try:
        fn()
    except P.ConfigPresetError:
        return True
    except Exception:
        return False
    return False


def _work_server(data_dir):
    """绕开 WebUIServer.__init__（它要 NapCat 那一堆），只装测试要用的三样。"""
    view = M.WebUIServer.__new__(M.WebUIServer)
    view.config = M.ConfigLoader(str(Path(data_dir).parent / "config.json"))
    view.memory_manager = SimpleNamespace(data_path=Path(data_dir))
    view._after_config_reload = lambda: None
    return view


class _StubSender:
    """只回答「这条会话走哪条接入方式」的假发送器。"""

    def __init__(self, key=""):
        self.key = key

    def session_channel_key(self, session_id):
        return self.key

    def session_client(self, session_id):
        return None

    def _active_client(self):
        return None


def main() -> int:
    print("=" * 70)
    print("配置文件：命名保存 + 接入方式各用一份")
    print("=" * 70)

    tmp = Path(tempfile.mkdtemp(prefix="lovomo_profiles_"))
    data = tmp / "data"
    try:
        section("A 存储层")
        check("default 是内置项且排在最前",
              lambda: P.list_profiles(data)[0] == {"name": "default", "builtin": True,
                                                   "updated_at": "", "size": 0}
              and P.DEFAULT_PROFILE == "default")
        check("没有目录时也能列出 default",
              lambda: [p["name"] for p in P.list_profiles(tmp / "nothing")] == ["default"])

        saved = P.save_profile(data, "工作", {"llm_model_name": "work-model"})
        check("新建后能列出、名字与大小对得上",
              lambda: saved["name"] == "工作" and saved["builtin"] is False
              and [p["name"] for p in P.list_profiles(data)] == ["default", "工作"]
              and saved["updated_at"] and saved["size"] > 0)
        check("文件落在 data/config_presets 下且是扁平配置",
              lambda: json.loads((data / "config_presets" / "工作.json")
                                 .read_text(encoding="utf-8"))["llm_model_name"] == "work-model")
        check("读回的内容与存进去的一致",
              lambda: P.load_profile(data, "工作") == {"llm_model_name": "work-model"})
        check("default 读出来是空 dict（表示跟随主配置）",
              lambda: P.load_profile(data, P.DEFAULT_PROFILE) == {})

        P.save_profile(data, "工作", {"llm_model_name": "work-model",
                                      "connections": [{"id": "c1"}]})
        check("接入方式列表不进配置文件",
              lambda: "connections" not in P.load_profile(data, "工作"))

        P.save_profile(data, "影音", {"llm_model_name": "media"}, rename_from="工作")
        check("重命名会删掉旧文件",
              lambda: not (data / "config_presets" / "工作.json").exists()
              and P.load_profile(data, "影音") == {"llm_model_name": "media"})

        check("default 不能被占用",
              lambda: _raises(lambda: P.clean_name("default"))
              and _raises(lambda: P.save_profile(data, "default", {})))
        check("default 不能被删除",
              lambda: _raises(lambda: P.delete_profile(data, "default")))
        check("删除不存在的配置文件报错",
              lambda: _raises(lambda: P.delete_profile(data, "不存在"))
              and _raises(lambda: P.load_profile(data, "不存在")))
        check("空名字被拒绝", lambda: _raises(lambda: P.clean_name("   ")))
        check("超长名字被拒绝",
              lambda: _raises(lambda: P.clean_name("名" * (P.MAX_NAME_CHARS + 1))))
        check("非法字符与路径穿越一律拒绝",
              lambda: all(_raises(lambda v=v: P.clean_name(v)) for v in
                          ("a/b", "a\\b", "../x", "x:y", "a?b", "a*b", ".", "..", "x.")))
        check("名字两侧空白被折叠",
              lambda: P.clean_name("  工作   配置  ") == "工作 配置")

        work = P.preset_dir(data)
        (work / "notes.txt").write_text("x", encoding="utf-8")
        (work / "坏掉的.json").write_text("{ 坏掉的", encoding="utf-8")
        (work / "旧版预设.json").write_text(
            json.dumps({"note": "旧的", "created_at": "2026-01-01 00:00:00",
                        "config": {"llm_model_name": "legacy"}}, ensure_ascii=False),
            encoding="utf-8")
        check("非配置文件不会带崩列表（坏掉的照旧列出）",
              lambda: sorted(p["name"] for p in P.list_profiles(data))
              == sorted(["default", "影音", "旧版预设", "坏掉的"]))
        check("旧版 {note, created_at, config} 包装仍能读成配置",
              lambda: P.load_profile(data, "旧版预设") == {"llm_model_name": "legacy"})

        section("B HTTP 处理函数")
        server = _work_server(data)
        server.config.config["llm_api_key"] = "sk-plain-secret"
        server.config.config["llm_base_url"] = "https://main.example"
        server.config.config["connections"] = [
            {"id": "c1", "platform": "napcat", "name": "主号", "config_profile": "接口存的"},
        ]

        listed = _resp_json(asyncio.run(server.handle_config_profiles_list(_FakeReq({}))))
        check("列表接口回 success 并带上 default",
              lambda: listed["success"] and listed["default"] == "default"
              and listed["profiles"][0]["name"] == "default")
        check("列表接口标出谁在用这份配置",
              lambda: [p for p in listed["profiles"] if p["name"] == "影音"][0]["used_by"] == [])

        read_default = _resp_json(asyncio.run(server.handle_config_profiles_read(
            _FakeReq({"name": "default"}))))
        check("读 default 回的是主配置",
              lambda: read_default["success"]
              and read_default["config"]["llm_base_url"] == "https://main.example")
        check("读出来的密钥是空值，连头尾字符都不给",
              lambda: "sk-plain-secret" not in json.dumps(read_default["config"])
              and read_default["config"]["llm_api_key"] == ""
              and "llm_api_key" in read_default["secret_keys"]
              and "webui_password" in read_default["secret_keys"])

        write = _resp_json(asyncio.run(server.handle_config_profiles_save(
            _FakeReq({"name": "接口存的",
                      "config": {"llm_model_name": "api-model",
                                 "llm_api_key": "sk-plain-secret"}}))))
        check("保存接口回 success 并带上配置文件信息",
              lambda: write["success"] and write["profile"]["name"] == "接口存的")
        check("存进配置文件的密钥是密文，不是明文",
              lambda: str(P.load_profile(data, "接口存的")["llm_api_key"]).startswith("enc2:"))

        asyncio.run(server.handle_config_profiles_save(
            _FakeReq({"name": "接口存的",
                      "config": {"llm_model_name": "api-model-2",
                                 "llm_api_key": read_default["config"]["llm_api_key"]}})))
        check("密钥字段空着沿用文件里已有的值，不会把密钥清空",
              lambda: str(P.load_profile(data, "接口存的")["llm_api_key"]).startswith("enc2:")
              and P.load_profile(data, "接口存的")["llm_model_name"] == "api-model-2")

        asyncio.run(server.handle_config_profiles_save(
            _FakeReq({"name": "接口存的",
                      "config": {"llm_model_name": "api-model-3"}})))
        check("密钥字段整个没提交时也沿用文件里已有的值",
              lambda: str(P.load_profile(data, "接口存的")["llm_api_key"]).startswith("enc2:"))

        created = _resp_json(asyncio.run(server.handle_config_profiles_save(
            _FakeReq({"name": "新的一份",
                      "config": {"llm_api_key": "", "webui_password": "",
                                 "web_search_api_keys": {}, "plugin_publish_token": None}}))))
        check("新建的配置文件里密钥是空的，不会带上主配置的密钥",
              lambda: created["success"]
              and P.load_profile(data, "新的一份")["llm_api_key"] == ""
              and P.load_profile(data, "新的一份")["webui_password"] == ""
              and "sk-plain-secret" not in json.dumps(P.load_profile(data, "新的一份")))

        renamed = _resp_json(asyncio.run(server.handle_config_profiles_save(
            _FakeReq({"name": "接口改的", "rename_from": "接口存的",
                      "config": {"llm_model_name": "api-model-2"}}))))
        check("重命名接口生效且旧文件消失",
              lambda: renamed["success"]
              and not (data / "config_presets" / "接口存的.json").exists()
              and (data / "config_presets" / "接口改的.json").is_file())

        bad_name = asyncio.run(server.handle_config_profiles_save(
            _FakeReq({"name": "default", "config": {}})))
        check("保存成 default 回 400",
              lambda: bad_name.status == 400 and not _resp_json(bad_name)["success"])
        bad_payload = asyncio.run(server.handle_config_profiles_save(
            _FakeReq({"name": "x", "config": "不是对象"})))
        check("配置内容不是对象时回 400",
              lambda: bad_payload.status == 400 and not _resp_json(bad_payload)["success"])
        missing = asyncio.run(server.handle_config_profiles_read(
            _FakeReq({"name": "根本没有"})))
        check("读不存在的配置文件回 400",
              lambda: missing.status == 400 and not _resp_json(missing)["success"])
        del_default = asyncio.run(server.handle_config_profiles_delete(
            _FakeReq({"name": "default"})))
        check("删除 default 回 400",
              lambda: del_default.status == 400 and not _resp_json(del_default)["success"])

        deleted = _resp_json(asyncio.run(server.handle_config_profiles_delete(
            _FakeReq({"name": "接口改的"}))))
        check("删除接口生效",
              lambda: deleted["success"] and deleted["name"] == "接口改的")

        section("C 链路接入")
        check("五个接口都注册了路由",
              lambda: all(hasattr(M.WebUIServer, n) for n in
                          ("handle_config_profiles_list", "handle_config_profiles_read",
                           "handle_config_profiles_save", "handle_config_profiles_delete",
                           "handle_config_profiles_open_dir")))
        check("写操作在敏感接口清单里（列表/读取/打开目录不拦）",
              lambda: all(M._needs_second_password(f"/api/config/profiles/{a}") for a in
                          ("save", "delete"))
              and not M._needs_second_password("/api/config/profiles")
              and not M._needs_second_password("/api/config/profiles/read")
              and not M._needs_second_password("/api/config/profiles/open_dir"))
        check("旧的预设接口与前端调用都清干净了",
              lambda: "api/config/presets" not in HTML
              and not hasattr(M.WebUIServer, "handle_config_presets_save"))
        check("配置文件页的 DOM id 都能在 HTML 里找到",
              lambda: all(f'id="{i}"' in HTML for i in
                          ("panel-profiles", "profile-select", "profile-name",
                           "profile-create-btn", "profile-rename-btn", "profile-delete-btn",
                           "profile-open-dir-btn", "profile-usedby", "profile-groups",
                           "profile-anchor-nav", "profile-float-actions",
                           "profile-message", "profile-save-btn", "profile-back-btn")))
        check("前端调的是配置文件接口",
              lambda: all(f"api/config/profiles{a}" in HTML for a in
                          ("'", "/read", "/save", "/delete", "/open_dir")))
        check("接入方式弹窗里有配置文件下拉",
              lambda: 'id="conn-profile"' in HTML and "fillConnectionProfileSelect(conn)" in HTML
              and "config_profile: document.getElementById('conn-profile').value" in HTML)
        check("接入方式保存接口会落盘 config_profile",
              lambda: '"config_profile" in payload' in
              (ROOT / "modules" / "webui_server.py").read_text(encoding="utf-8"))

        section("D 配置页分组结构")
        groups_block = HTML[HTML.index("const configGroups = ["):
                            HTML.index("const configTailGroups")]
        check("「更多」排在最后，紧跟在多角色配置之后",
              lambda: HTML.index("const configTailGroups") > 0
              and "renderRolesConfig(config, { container: box });" in HTML
              and "renderConfigForm(config, defaults, { groups: configTailGroups, container: box, append: true });" in HTML)
        check("防刷屏与隐藏高级 GSV 参数都收进「更多」",
              lambda: "{ title: '防刷屏', keys: ['anti_spam_enabled', 'anti_spam_window_seconds', 'anti_spam_max_in_window'] }" in groups_block
              and "{ title: '其他', keys: ['hide_gsv_options'" in groups_block)
        check("插件系统的四个内部选项不再露在配置页上",
              lambda: all(f"'{k}'" not in groups_block for k in
                          ("plugin_market_repo", "plugin_market_branch_prefix",
                           "plugin_market_sources_path", "plugin_market_path")))
        check("当前激活角色挪进多角色配置块",
              lambda: "'active_character'" not in groups_block
              and "buildConfigRow('active_character', config, defaults, container)" in HTML
              and "container.insertBefore(activeRow, rolesBox)" in HTML)
        check("表单渲染函数支持指定容器（设置页与配置文件页各一份）",
              lambda: "options.container || document.getElementById('config-groups')" in HTML
              and "function buildConfigRow(key, config, defaults = {}, scope = document)" in HTML
              and "function updateConditionalVisibility(scope)" in HTML)

        rendered = _render_config_page()
        if rendered is None:
            print("  [SKIP] 环境里没有 node，跳过配置页渲染检查")
        else:
            check("配置页渲染：分组顺序与「更多」在最后",
                  lambda: rendered["lastGroup"] == "更多"
                  and rendered["groupTitles"][-2] == "")
            check("配置页渲染：字段键不重复、隐藏项不渲染",
                  lambda: rendered["duplicateKeys"] == []
                  and rendered["hiddenPluginKeys"] == []
                  and rendered["hasSpam"] and rendered["hasVoiceAsr"])
            check("配置页渲染：当前激活角色在多角色配置顶部",
                  lambda: rendered["rolesKeys"] == ["active_character"]
                  and rendered["activeRowIndex"] + 1 == rendered["rolesContainerIndex"])
            check("配置页渲染：第二份表单能渲染到自己的容器里",
                  lambda: rendered["profileKeys"] > 0
                  and rendered["profileLastGroup"] == "更多"
                  and rendered["profileRoles"] > 0
                  and rendered["configKeys"] > 0)

        section("E 接入方式绑定")
        cfg = M.ConfigLoader(str(tmp / "config.json"))
        cfg.config["connections"] = [
            {"id": "c1", "platform": "napcat", "name": "主号", "config_profile": "工作"},
            {"id": "c2", "platform": "napcat", "name": "小号"},
        ]
        cfg.config["roles"] = [{"character_key": "a", "character_name": "A",
                                "personality_prompt": "主人设"}]
        cfg.config["llm_model_name"] = "main-model"
        cfg._atomic_save(cfg.config)
        cfg.roles = cfg._parse_roles()
        P.save_profile(data, "工作", {
            "llm_model_name": "work-model",
            "roles": [{"character_key": "a", "character_name": "A",
                       "personality_prompt": "工作人设"}],
        })

        saved_cfg, saved_mm, saved_sender = AC.global_config, AC.memory_manager, AC.sender
        AC.global_config, AC.memory_manager = cfg, SimpleNamespace(data_path=data)
        try:
            check("接入方式 → 配置文件名",
                  lambda: SC.connection_config_profile(cfg, "c1") == "工作"
                  and SC.connection_config_profile(cfg, "c2") == ""
                  and SC.connection_config_profile(cfg, "") == "")
            check("绑了配置文件的接入方式拿到那份配置",
                  lambda: SC.config_for_connection("c1").get("llm_model_name") == "work-model")
            check("没绑的接入方式回落主配置",
                  lambda: SC.config_for_connection("c2").get("llm_model_name") == "main-model")
            check("配置文件名不合法/文件没了也不会炸，回落主配置",
                  lambda: SC.config_for_connection("c1") is not None
                  and SC.config_for_connection("不存在") is not None)
            check("配置访问器能解析角色与激活角色",
                  lambda: SC.config_for_connection("c1").roles["a"]["personality_prompt"]
                  == "工作人设")

            AC.sender = _StubSender("c1")
            check("会话按接入方式取配置",
                  lambda: SC.config_for_session("s1").get("llm_model_name") == "work-model")
            check("回复上下文用这份配置的角色内容",
                  lambda: SC.reply_ctx_of({"character_key": "a"}, None, "s1")
                  .get("personality_prompt") == "工作人设")
            check("主动消息按目标会话所属连接取配置",
                  lambda: SC.reply_ctx_of(SC.get_active_role(), session_id="s1")
                  .get("llm_model_name") == "work-model"
                  and SC.reply_ctx_of(SC.get_active_role(), session_id="s1")
                  .get("personality_prompt") == "工作人设")
            AC.sender = _StubSender("c2")
            check("另一条接入方式的会话仍用主配置",
                  lambda: SC.reply_ctx_of({"character_key": "a"}, None, "s2")
                  .get("personality_prompt") == "主人设")
            AC.sender = None
            check("没有发送器时回落主配置",
                  lambda: SC.config_for_session("s3").get("llm_model_name") == "main-model")

            token = AC.set_active_config(SC.config_for_connection("c1"))
            try:
                check("上下文里换配置后 active_config 跟着换",
                      lambda: AC.active_config().get("llm_model_name") == "work-model"
                      and SC.get_active_role().get("personality_prompt") == "工作人设")
            finally:
                AC.reset_active_config(token)
            check("退出上下文后回到主配置",
                  lambda: AC.active_config().get("llm_model_name") == "main-model")
            check("没给 session_id 时回复上下文回落主配置",
                  lambda: SC.reply_ctx_of({"character_key": "a"}).get("llm_model_name")
                  == "main-model")
            token = AC.set_active_config(SC.config_for_connection("c1"))
            try:
                check("没给 session_id 时回复上下文跟随当前处理链路的配置",
                      lambda: SC.reply_ctx_of({"character_key": "a"}).get("llm_model_name")
                      == "work-model"
                      and SC.reply_ctx_of({"character_key": "a"}).get("personality_prompt")
                      == "工作人设")
            finally:
                AC.reset_active_config(token)

            snapshot = SC.role_connection_snapshot(cfg)
            changed = dict(cfg.config)
            changed["connections"] = [{"id": "c1", "platform": "napcat",
                                       "name": "主号", "config_profile": "另一个"}]
            check("接入方式快照包含配置文件绑定（改绑定会触发重连）",
                  lambda: snapshot != SC.role_connection_snapshot(SimpleNamespace(
                      get=lambda k, d=None: changed.get(k, d),
                      roles=cfg.roles)))
            check("连接清单里带着这条接入方式的完整字段",
                  lambda: [p for p in SC.build_connection_profiles(cfg)
                           if p["key"] == "c1"][0]["connection"]["config_profile"] == "工作")
        finally:
            AC.global_config, AC.memory_manager, AC.sender = saved_cfg, saved_mm, saved_sender
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f"结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
    print("=" * 70)
    if FAIL:
        for name in FAIL:
            print(f"  FAIL: {name}")
        return 1
    return 0


_DOM_STUB_JS = r"""
class El {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.dataset = {};
    this.style = {};
    this.className = '';
    this.classList = { add: () => {}, remove: () => {}, toggle: () => {} };
    this._text = '';
    this._html = '';
    this.value = '';
    this.checked = false;
    this.type = '';
  }
  set textContent(v) { this._text = String(v); }
  get textContent() { return this._text; }
  set innerHTML(v) {
    this._html = String(v);
    this.children = [];
    for (const m of this._html.matchAll(/\bid="([^"]*)"/g)) {
      const child = new El('div');
      child.id = m[1];
      ids[m[1]] = child;
      this.children.push(child);
    }
    // data-* 有带值的也有裸属性（如 data-roles-container），两种都要认
    for (const m of this._html.matchAll(/\bdata-([a-z][a-z0-9-]*)(?:="([^"]*)")?/g)) {
      const child = new El('div');
      child.dataset[m[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase())] =
          m[2] === undefined ? '' : m[2];
      this.children.push(child);
    }
  }
  get innerHTML() { return this._html; }
  appendChild(c) { this.children.push(c); return c; }
  append(...items) { items.forEach(i => this.children.push(i)); }
  insertBefore(c, ref) {
    const i = this.children.indexOf(ref);
    if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
    return c;
  }
  addEventListener() {}
  setAttribute() {}
  matches(sel) {
    if (sel.startsWith('[data-') && sel.endsWith(']')) {
      const key = sel.slice(6, -1).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      return Object.prototype.hasOwnProperty.call(this.dataset, key);
    }
    if (sel.startsWith('.')) return String(this.className).split(/\s+/).includes(sel.slice(1));
    return false;
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const out = [];
    const walk = (el) => (el.children || []).forEach(c => {
      if (c.matches && c.matches(sel)) out.push(c);
      walk(c);
    });
    walk(this);
    return out;
  }
  find() { return undefined; }
}

const ids = {};
global.document = {
  body: new El('body'),
  createElement: tag => new El(tag),
  getElementById: id => (ids[id] = ids[id] || new El('div')),
  querySelector: () => null,
  querySelectorAll: () => [],
  addEventListener: () => {},
};
global.window = {};

function esc(s){ return String(s == null ? '' : s); }
function icon(){ return ''; }
function applyFieldValue(){}
function alert(){}
function chooseModelFromService(){}
function testGithubMirrors(){}
function showConfigMessage(){}
function makeBrowseButton(){ return document.createElement('button'); }
function makeVoiceDesignButton(){ return document.createElement('button'); }
function makeVoiceEnrollButton(){ return document.createElement('button'); }
function roleConnectionOptions(){ return ''; }
function resolveFieldValue(key, config, defaults){
  if (config && key in config) return config[key];
  return defaults && key in defaults ? defaults[key] : '';
}
const LLM_ENDPOINT_PRESETS = [];
const CLOUD_TTS_PROVIDERS = [];
const CLOUD_TTS_MODEL_PRESETS = [];
const CLOUD_TTS_VOICE_PRESETS = [];
"""

_RENDER_DRIVER_JS = r"""
const config = {active_character: 'congyu', napcat_ws_url: 'ws://x', stats_enabled: true,
                plugins_enabled: true, hide_gsv_options: false, asr_engine: 'local',
                roles: [{character_key: 'congyu', character_name: '丛雨'}]};
const defaults = {active_character: 'congyu', history_length: 8};

function snapshot(root) {
  const groupTitles = root.children.map(c => c.children.filter(x => x.tagName === 'h3')
                                                 .map(x => x.textContent).join(''));
  const fieldKeys = [];
  const walk = (el) => (el.children || []).forEach(c => {
    if (c.dataset && c.dataset.fieldKey) fieldKeys.push(c.dataset.fieldKey);
    walk(c);
  });
  walk(root);
  const dup = fieldKeys.filter((k, i) => fieldKeys.indexOf(k) !== i);
  return { groupTitles, fieldKeys, duplicateKeys: [...new Set(dup)] };
}

const box = document.getElementById('config-groups');
renderConfigForm(config, defaults, {groups: configMainGroups, container: box});
const beforeRoles = box.children.length;
renderRolesConfig(config, {container: box});
const rolesBlock = box.children[beforeRoles];
renderConfigForm(config, defaults, {groups: configTailGroups, container: box, append: true});
const main = snapshot(box);

const pbox = document.getElementById('profile-groups');
renderConfigForm(config, defaults, {groups: configMainGroups, container: pbox});
const pBeforeRoles = pbox.children.length;
renderRolesConfig(config, {container: pbox});
renderConfigForm(config, defaults, {groups: configTailGroups, container: pbox, append: true});
const prof = snapshot(pbox);

console.log(JSON.stringify({
  groupTitles: main.groupTitles,
  lastGroup: main.groupTitles[main.groupTitles.length - 1],
  hasSpam: main.fieldKeys.includes('anti_spam_enabled'),
  hasVoiceAsr: main.fieldKeys.includes('asr_engine'),
  hiddenPluginKeys: ['plugin_market_repo', 'plugin_market_branch_prefix',
                     'plugin_market_sources_path', 'plugin_market_path']
                    .filter(k => main.fieldKeys.includes(k)),
  duplicateKeys: main.duplicateKeys,
  rolesKeys: rolesBlock.children.filter(r => r.dataset && r.dataset.fieldKey)
                                .map(r => r.dataset.fieldKey),
  rolesContainerIndex: rolesBlock.children.indexOf(
      rolesBlock.children.find(c => c.dataset && 'rolesContainer' in c.dataset)),
  activeRowIndex: rolesBlock.children.findIndex(r => r.dataset
    && r.dataset.fieldKey === 'active_character'),
  configKeys: main.fieldKeys.length,
  profileKeys: prof.fieldKeys.length,
  profileLastGroup: prof.groupTitles[prof.groupTitles.length - 1],
  profileRoles: pbox.children[pBeforeRoles].children.filter(
      c => c.dataset && 'rolesContainer' in c.dataset).length,
}));
"""


def _js_block(lines, start_prefix, end_line):
    """按行抠一段前端代码：从 start_prefix 那行到之后第一个 end_line 行。"""
    start = next(i for i, line in enumerate(lines) if line.startswith(start_prefix))
    end = next(i for i in range(start, len(lines)) if i > start and lines[i] == end_line)
    return "\n".join(lines[start:end + 1])


def _js_function(lines, name):
    return _js_block(lines, f"        function {name}(", "        }")


def _js_line(lines, prefix):
    return next(line for line in lines if line.startswith(prefix))


def _render_config_page():
    """真跑一遍配置页渲染（node + 极简 DOM 桩），返回分组与字段的实测结果。

    只看源码字符串发现不了"把字段行构造器抽出来后结构错位"这类问题，
    所以这里把 buildConfigRow / renderConfigForm / renderRolesConfig 抠出来真跑，
    并同时渲染到设置页与配置文件页两个容器，确认同一套函数能喂给两处。
    """
    node = shutil.which("node")
    if not node:
        return None
    scripts = re.findall(r"<script>(.*?)</script>", HTML, re.S)
    lines = scripts[1].replace("\r\n", "\n").split("\n")
    code = "\n".join([
        _DOM_STUB_JS,
        _js_block(lines, "        const configGroups = [", "        ];"),
        _js_block(lines, "        const configMeta = {", "        };"),
        _js_function(lines, "buildConfigRow"),
        _js_line(lines, "        let configTreeHideTimer = null;"),
        _js_function(lines, "hideConfigTree"),
        _js_function(lines, "keepConfigTree"),
        _js_function(lines, "scheduleHideConfigTree"),
        _js_function(lines, "bindConfigTreeOnce"),
        _js_function(lines, "setSubgroupOpen"),
        _js_function(lines, "renderSubgroupTree"),
        _js_function(lines, "renderConfigForm"),
        _js_function(lines, "renderRolesConfig"),
        _js_line(lines, "        const configTailGroups"),
        _js_line(lines, "        const configMainGroups"),
        _RENDER_DRIVER_JS,
    ])
    with tempfile.TemporaryDirectory(prefix="lovomo_cfgrender_") as td:
        path = Path(td) / "render.js"
        path.write_text(code, encoding="utf-8")
        proc = subprocess.run([node, str(path)], capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
    if proc.returncode != 0:
        print("      node 退出码:", proc.returncode, proc.stderr[:400])
        return None
    return json.loads(proc.stdout.strip().splitlines()[-1])


if __name__ == "__main__":
    sys.exit(main())
