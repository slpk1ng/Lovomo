# -*- coding: utf-8 -*-
"""配置预设（导出成 data 下的 JSON、随时切回）回归测试。

覆盖：
  A 存储层：保存后能列出、备注归一化与截断、同一秒内连续导出不覆盖、
    列表按时间倒序、读回内容一致、改备注不动配置、删除、非法编号一律拒绝、
    不存在的编号报错、损坏的预设被跳过、没有目录时返回空
  B HTTP 处理函数：导出走密钥密文化、列表、改备注、切换预设真的改到配置并落盘、
    非法编号与不存在的编号回 400
  C 链路接入：路由注册、写操作在敏感接口清单里（列表不拦）、导入与切换共用同一条
    应用路径、前端入口替换齐全
  D 配置页分组结构：分组顺序与「更多」位置、隐藏项不渲染、字段键不重复
    （D 段用 node + DOM 桩把配置页真渲染一遍，没有 node 时跳过）

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
import modules.config_presets as P  # noqa: E402

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
    """只喂 JSON 的假请求，够 handle_config_presets_* 用。"""

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


def main() -> int:
    print("=" * 70)
    print("配置预设：导出到 data、一键切回")
    print("=" * 70)

    tmp = Path(tempfile.mkdtemp(prefix="lovomo_presets_"))
    data = tmp / "data"
    try:
        section("A 存储层")
        first = P.save_preset(data, {"llm_base_url": "https://a.example"}, "换模型前")
        second = P.save_preset(data, {"llm_base_url": "https://b.example"}, "")
        check("保存后返回编号/备注/时间/大小",
              lambda: bool(first["id"]) and first["note"] == "换模型前"
              and first["created_at"] and first["size"] > 0)
        check("预设文件落在 data/config_presets 下",
              lambda: (data / "config_presets" / f"{first['id']}.json").is_file())
        check("同一秒内连续导出不会互相覆盖",
              lambda: first["id"] != second["id"] and second["id"].startswith(first["id"]))
        check("列表按时间倒序（最新在前）",
              lambda: [p["id"] for p in P.list_presets(data)] == [second["id"], first["id"]])
        check("读回的内容与存进去的一致",
              lambda: P.load_preset(data, first["id"]) == {"llm_base_url": "https://a.example"})
        check("备注里的换行与多余空白被折叠",
              lambda: P.save_preset(data, {}, "  换\n声线   之前  ")["note"] == "换 声线 之前")
        check("超长备注被截断",
              lambda: len(P.save_preset(data, {}, "备" * 500)["note"]) == P.MAX_NOTE_CHARS)
        check("没有备注时是空串",
              lambda: P.save_preset(data, {}, None)["note"] == "")

        note_id = P.save_preset(data, {"llm_base_url": "https://c.example"})["id"]
        P.update_note(data, note_id, "改过的备注")
        check("改备注只动备注、不动配置",
              lambda: [p["note"] for p in P.list_presets(data) if p["id"] == note_id]
              == ["改过的备注"]
              and P.load_preset(data, note_id) == {"llm_base_url": "https://c.example"})

        check("非法编号一律拒绝（防拼出目录外的路径）",
              lambda: all(_raises(lambda v=v: P.load_preset(data, v))
                          for v in ("", None, "../../evil", "a/b", "2026.json",
                                    "20260926-215012/../../x", "../config_presets")))
        check("不存在的编号报错",
              lambda: _raises(lambda: P.load_preset(data, "20200101-000000")))

        P.delete_preset(data, note_id)
        check("删除后列表里消失且文件没了",
              lambda: all(p["id"] != note_id for p in P.list_presets(data))
              and not (data / "config_presets" / f"{note_id}.json").exists())
        check("删除不存在的编号报错",
              lambda: _raises(lambda: P.delete_preset(data, note_id)))

        expected = sorted(p["id"] for p in P.list_presets(data))
        work = P.preset_dir(data)
        (work / "notes.txt").write_text("x", encoding="utf-8")
        (work / "随便写的.json").write_text("{}", encoding="utf-8")
        (work / "20260101-010101.json").write_text("{ 坏掉的", encoding="utf-8")
        check("非预设文件与损坏的预设都不会带崩列表",
              lambda: sorted(p["id"] for p in P.list_presets(data)) == expected)
        check("没有预设目录时列表是空的",
              lambda: P.list_presets(tmp / "nothing") == [])

        section("B HTTP 处理函数")
        server = _work_server(data)
        server.config.config["llm_api_key"] = "sk-plain-secret"
        server.config.config["llm_base_url"] = "https://saved.example"
        saved = _resp_json(asyncio.run(server.handle_config_presets_save(
            _FakeReq({"note": "接口存的"}))))
        check("导出接口回 success 并带上预设信息",
              lambda: saved["success"] and saved["preset"]["note"] == "接口存的")
        check("导出到预设的密钥是密文，不是明文",
              lambda: P.load_preset(data, saved["preset"]["id"])["llm_api_key"]
              != "sk-plain-secret"
              and str(P.load_preset(data, saved["preset"]["id"])["llm_api_key"]).startswith("enc2:"))

        listed = _resp_json(asyncio.run(server.handle_config_presets_list(_FakeReq({}))))
        check("列表接口能列出刚存的预设",
              lambda: listed["success"]
              and any(p["id"] == saved["preset"]["id"] for p in listed["presets"]))

        noted = _resp_json(asyncio.run(server.handle_config_presets_note(
            _FakeReq({"id": saved["preset"]["id"], "note": "接口改的"}))))
        check("改备注接口生效",
              lambda: noted["success"] and noted["preset"]["note"] == "接口改的")

        server.config.config["llm_base_url"] = "https://changed.example"
        applied = _resp_json(asyncio.run(server.handle_config_presets_apply(
            _FakeReq({"id": saved["preset"]["id"]}))))
        check("切换预设真的把配置改回存下来的那份",
              lambda: applied["success"] and server.config.config["llm_base_url"]
              == "https://saved.example")
        on_disk = json.loads((tmp / "config.json").read_text(encoding="utf-8"))
        check("切换预设写回了磁盘",
              lambda: on_disk.get("llm_base_url") == "https://saved.example")

        server.config.config["webui_password"] = "PresetPass-1"
        pw_preset = _resp_json(asyncio.run(server.handle_config_presets_save(
            _FakeReq({}))))["preset"]["id"]
        server.config.config["webui_password"] = "OtherPass-2"
        asyncio.run(server.handle_config_presets_apply(_FakeReq({"id": pw_preset})))
        check("切回预设后内存里的 WebUI 密码是明文（不是密文）",
              lambda: server.config.config.get("webui_password") == "PresetPass-1")

        bad = asyncio.run(server.handle_config_presets_apply(_FakeReq({"id": "../evil"})))
        check("非法编号回 400",
              lambda: bad.status == 400 and not _resp_json(bad)["success"])
        missing = asyncio.run(server.handle_config_presets_delete(
            _FakeReq({"id": "20200101-000000"})))
        check("删除不存在的预设回 400",
              lambda: missing.status == 400 and not _resp_json(missing)["success"])
        deleted = _resp_json(asyncio.run(server.handle_config_presets_delete(
            _FakeReq({"id": saved["preset"]["id"]}))))
        check("删除接口生效",
              lambda: deleted["success"] and deleted["id"] == saved["preset"]["id"])

        section("C 链路接入")
        check("五个接口都注册了路由",
              lambda: all(hasattr(M.WebUIServer, n) for n in
                          ("handle_config_presets_list", "handle_config_presets_save",
                           "handle_config_presets_note", "handle_config_presets_apply",
                           "handle_config_presets_delete")))
        check("写操作在敏感接口清单里（列表只读，不拦）",
              lambda: all(M._needs_second_password(f"/api/config/presets/{a}") for a in
                          ("save", "note", "apply", "delete"))
              and not M._needs_second_password("/api/config/presets"))
        check("导入与切换预设共用同一条应用路径",
              lambda: _shared_apply_path(data))

        check("右上角的两个旧按钮已换成「切换预设」",
              lambda: 'id="config-preset-btn"' in HTML
              and 'id="export-config-btn"' not in HTML
              and 'id="import-config-btn"' not in HTML
              and 'id="import-config-file"' not in HTML)
        check("弹窗里的 DOM id 都能在 HTML 里找到",
              lambda: all(f'id="{i}"' in HTML for i in
                          ("config-preset-modal", "config-preset-close", "preset-tab-import",
                           "preset-tab-export", "preset-pane-import", "preset-pane-export",
                           "preset-note", "preset-save", "preset-import-list",
                           "preset-export-list", "preset-msg")))
        check("前端调的是五个预设接口",
              lambda: all(f"api/config/presets{a}" in HTML for a in
                          ("'", "/save", "/note", "/apply", "/delete")))
        check("教程文案已改成切换预设",
              lambda: "右上角可切换预设" in HTML and "右上角可导出/导入配置" not in HTML)

        section("D 配置页分组结构")
        groups_block = HTML[HTML.index("const configGroups = ["):
                            HTML.index("const configTailGroups")]
        check("「更多」排在最后，紧跟在多角色配置之后",
              lambda: HTML.index("const configTailGroups") > 0
              and "renderRolesConfig(config);" in HTML
              and "renderConfigForm(config, defaults, { groups: configTailGroups, append: true });" in HTML)
        check("防刷屏与隐藏高级 GSV 参数都收进「更多」",
              lambda: "{ title: '防刷屏', keys: ['anti_spam_enabled', 'anti_spam_window_seconds', 'anti_spam_max_in_window'] }" in groups_block
              and "{ title: '其他', keys: ['hide_gsv_options'" in groups_block)
        check("插件系统的四个内部选项不再露在配置页上",
              lambda: all(f"'{k}'" not in groups_block for k in
                          ("plugin_market_repo", "plugin_market_branch_prefix",
                           "plugin_market_sources_path", "plugin_market_path")))
        check("当前激活角色挪进多角色配置块",
              lambda: "'active_character'" not in groups_block
              and "buildConfigRow('active_character', config, config.defaults || {})" in HTML
              and "container.insertBefore(activeRow" in HTML)
        check("「其他」分组已取消",
              lambda: "{ title: '其他', keys: ['active_character'" not in HTML)

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
    for (const m of this._html.matchAll(/id="([A-Za-z0-9_-]+)"/g)) {
      const child = new El('div');
      child.id = m[1];
      ids[m[1]] = child;
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
  querySelector() { return null; }
  querySelectorAll() { return []; }
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
function updateConditionalVisibility(){}
function alert(){}
function chooseModelFromService(){}
function syncPreset(){}
function testGithubMirrors(){}
function bindDeleteRole(){}
function showConfigMessage(){}
function makeBrowseButton(){ return document.createElement('button'); }
function makeVoiceDesignButton(){ return document.createElement('button'); }
function makeVoiceEnrollButton(){ return document.createElement('button'); }
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
                plugins_enabled: true, hide_gsv_options: false, asr_engine: 'local'};
const defaults = {active_character: 'congyu', history_length: 8};
renderConfigForm(config, defaults, {groups: configMainGroups});
const beforeRoles = document.getElementById('config-groups').children.length;
renderRolesConfig(config);
const rolesBlock = document.getElementById('config-groups').children[beforeRoles];
renderConfigForm(config, defaults, {groups: configTailGroups, append: true});
const container = document.getElementById('config-groups');
const groupTitles = container.children.map(c => c.children.filter(x => x.tagName === 'h3')
                                              .map(x => x.textContent).join(''));
const fieldKeys = [];
function walk(el) {
  (el.children || []).forEach(c => {
    if (c.dataset && c.dataset.fieldKey) fieldKeys.push(c.dataset.fieldKey);
    walk(c);
  });
}
walk(container);
const duplicateKeys = fieldKeys.filter((k, i) => fieldKeys.indexOf(k) !== i);
console.log(JSON.stringify({
  groupTitles,
  lastGroup: groupTitles[groupTitles.length - 1],
  hasSpam: fieldKeys.includes('anti_spam_enabled'),
  hasVoiceAsr: fieldKeys.includes('asr_engine'),
  hasHideGsv: fieldKeys.includes('hide_gsv_options'),
  hiddenPluginKeys: ['plugin_market_repo', 'plugin_market_branch_prefix',
                     'plugin_market_sources_path', 'plugin_market_path']
                    .filter(k => fieldKeys.includes(k)),
  duplicateKeys: [...new Set(duplicateKeys)],
  rolesKeys: rolesBlock.children.filter(r => r.dataset && r.dataset.fieldKey)
                                .map(r => r.dataset.fieldKey),
  rolesContainerIndex: rolesBlock.children.indexOf(ids['roles-container']),
  activeRowIndex: rolesBlock.children.findIndex(r => r.dataset
    && r.dataset.fieldKey === 'active_character'),
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
    所以这里把 buildConfigRow / renderConfigForm / renderRolesConfig 抠出来真跑。
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


def _shared_apply_path(data):
    """两个 handler 都必须走 _apply_imported_config，不能各写一份。"""
    tmp = Path(tempfile.mkdtemp(prefix="lovomo_presets_shared_"))
    try:
        server = _work_server(tmp / "data")
        seen = []
        server._apply_imported_config = lambda payload: seen.append(payload)
        preset = P.save_preset(tmp / "data", {"llm_base_url": "https://x.example"}, "")
        asyncio.run(server.handle_import_config(_FakeReq({"llm_base_url": "https://y.example"})))
        asyncio.run(server.handle_config_presets_apply(_FakeReq({"id": preset["id"]})))
        return [p.get("llm_base_url") for p in seen] == ["https://y.example", "https://x.example"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
