# -*- coding: utf-8 -*-
"""导出配置修复回归测试。

覆盖：
  A 导出接口可用：/api/config/export 返回 200 + 可解析 JSON
  B 禁止明文密钥导出：enc:... 占位符原样保留，明文 sk- 等一律加密/打码
  C 导出链路健壮：异常时返回 error 状态而不是静默失败

运行: python tests/test_export_fix.py      （全通过退出码 0）
"""
import asyncio
import io
import json
import os
import shutil
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
sys.stdin = io.StringIO()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import httpx  # noqa: E402
import main as M  # noqa: E402

PASS, FAIL = [], []

PLAIN_LLM = "sk-live-plaintext-abcdef1234567890"
PLAIN_TOKEN = "token-plain-text-value"
PLAIN_SEARCH = "sk-search-plain-0001"
PLAIN_PASSWORD = "SuperSecretWebUIPass123"
EXISTING_ENC = "enc2:AAECAwQFBgcICQoLDA0ODw=="


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


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# 真实密钥残留检测：与产品里的 _EMBEDDED_SECRET_RE 一致的判别口径。
# 注意不能直接判 "sk-" in body —— 打码预览 sk-a****z（51位）里同样含 sk-，
# 但它只是掩码，不是密钥；所以要求 sk- 后面跟足量的真实密钥字符。
import re  # noqa: E402

_PLAINTEXT_KEY_RE = re.compile(
    r"(?:sk-|sk_|ak-|ak_|ghp_|gho_|xoxb-|AIza)[A-Za-z0-9_\-]{8,}"
    r"|Bearer\s+[A-Za-z0-9_\-\.]{12,}"
    r"|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")


def _no_plaintext_secret(text: str) -> bool:
    """导出内容里不得出现任何真实明文密钥。"""
    return _PLAINTEXT_KEY_RE.search(str(text or "")) is None


TMP = ROOT / ".tmp_test" / "export_fix"
if TMP.exists():
    shutil.rmtree(TMP, ignore_errors=True)
(TMP / "ref").mkdir(parents=True, exist_ok=True)


def build_server(path=None, password=PLAIN_PASSWORD):
    """构造一个带 WebUI 服务的隔离实例。

    path / password 可覆盖，便于模拟「另一台机器」「另一个密码」的场景。
    """
    cfg_path = Path(path) if path else (TMP / "config.json")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data_dir = cfg_path.parent / f"mdata_{cfg_path.stem}"
    cfg = M.ConfigLoader(str(cfg_path))
    port = free_port()
    cfg.config.update({
        "memory_data_path": str(data_dir),
        "ref_audio_root": str(TMP / "ref"),
        "webui_host": "127.0.0.1", "webui_port": port,
        "webui_enabled": True, "webui_password": password,
        "auto_start_tts": False, "tools_enabled": False, "scheduler_enabled": False,
        "proactive_enabled": False, "greeting_events_enabled": False,
        "todo_enabled": False, "stickers_enabled": False,
        "llm_api_key": PLAIN_LLM,
        "napcat_token": PLAIN_TOKEN,
        "web_search_api_keys": {"bing_key": PLAIN_SEARCH, "keep_me": EXISTING_ENC},
    })
    M.global_config = cfg
    M.app_context.global_config = M.global_config
    M.memory_manager = M.MemoryManager(cfg)
    M.app_context.memory_manager = M.memory_manager
    M.db = M.DatabaseManager(M.memory_manager.data_path)
    M.stats_mgr = M.StatsManager(M.db)
    return M.WebUIServer(cfg, M.memory_manager), port, cfg


# ============================================================================
# A 导出接口可用
# ============================================================================
def a_export_endpoint_works():
    section("A 导出接口可用（按钮点击链路末端）")
    server, port, _ = build_server()
    base = f"http://127.0.0.1:{port}"

    async def flow():
        await server.start()
        try:
            async with httpx.AsyncClient(timeout=30) as hc:
                # 未带密码 Cookie：必须回 401，前端据此弹登录框后重试
                r_anon = await hc.get(base + "/api/config/export")
                check("未登录时导出接口返回 401（前端会弹密码框）",
                      lambda: r_anon.status_code == 401)

                # 登录后再导出
                r_login = await hc.post(base + "/api/auth/login",
                                        json={"password": PLAIN_PASSWORD,
                                              "remember_minutes": 30})
                check("使用正确密码可登录", lambda: r_login.status_code == 200)

                r = await hc.get(base + "/api/config/export")
                check("GET /api/config/export 返回 200", lambda: r.status_code == 200)
                check("导出响应声明为附件下载",
                      lambda: "attachment" in (r.headers.get("Content-Disposition") or ""))
                check("导出响应文件名正确",
                      lambda: "lovomo_config_export.json"
                      in (r.headers.get("Content-Disposition") or ""))

                def parses():
                    d = json.loads(r.text)
                    return isinstance(d, dict) and len(d) > 100
                check("导出内容是合法且完整的 JSON 对象", parses)

                # 导出结果可直接被导入接口消费（换机器迁移的主流程）
                r2 = await hc.post(base + "/api/config/import",
                                   files={"file": ("e.json", r.text.encode("utf-8"),
                                                   "application/json")})
                check("导出文件可被 /api/config/import 成功导入",
                      lambda: r2.status_code == 200 and r2.json().get("success") is True)

                # 导入后密钥仍要能解回明文，说明导出的是本机可解的密文
                check("导入后 llm_api_key 解回明文（密文绑本机可解）",
                      lambda: M._decrypt_value(
                          (server.config.config or {}).get("llm_api_key", "")) == PLAIN_LLM)

        finally:
            await server.shutdown()

    asyncio.run(flow())


# ============================================================================
# B 禁止明文密钥导出
# ============================================================================
def b_no_plaintext_secrets():
    section("B 禁止明文密钥导出（enc: 占位符原样保留）")
    server, _, cfg = build_server()

    payload = server._config_export_payload()
    body = json.dumps(payload, ensure_ascii=False)

    check("导出整体不含任何真实明文密钥（sk-/Bearer/JWT）",
          lambda: _no_plaintext_secret(body))
    check("导出不含明文 napcat_token", lambda: PLAIN_TOKEN not in body)
    check("导出不含明文 WebUI 访问密码", lambda: PLAIN_PASSWORD not in body)
    check("导出不含明文搜索密钥", lambda: PLAIN_SEARCH not in body)

    check("llm_api_key 导出为 enc2: 密文",
          lambda: str(payload.get("llm_api_key", "")).startswith("enc2:"))
    check("napcat_token 导出为 enc2: 密文",
          lambda: str(payload.get("napcat_token", "")).startswith("enc2:"))
    check("webui_password 导出为 enc2: 密文",
          lambda: str(payload.get("webui_password", "")).startswith("enc2:"))

    check("web_search_api_keys 全部为密文",
          lambda: all(str(v).startswith("enc2:")
                      for v in payload.get("web_search_api_keys", {}).values()))

    check("原有 enc2: 密文占位符原样保留（不被二次加密）",
          lambda: payload["web_search_api_keys"]["keep_me"] == EXISTING_ENC)

    # 兼容历史 enc:（无 2）前缀：同样必须原样保留
    cfg.config["napcat_token"] = "enc:QUJDREVGR0g="
    p2 = server._config_export_payload()
    check("历史 enc: 前缀密文原样保留（不被二次加密）",
          lambda: p2["napcat_token"] == "enc:QUJDREVGR0g=")
    cfg.config["napcat_token"] = PLAIN_TOKEN

    # 打码预览（****）不含真实密钥，不应被当成明文密钥加密，也不该被判成泄漏
    MASKED = "sk-a****z（51位）"
    cfg.config["llm_api_key"] = MASKED
    p3 = server._config_export_payload()
    check("打码预览值原样保留（不二次加密）",
          lambda: p3["llm_api_key"] == MASKED)

    # 打码值里仍含 "sk-" 字样，但它是掩码不是真密钥：
    # 校验规则要看「是否有真实密钥残留」，不能只做朴素子串匹配
    check("打码预览值不算明文密钥泄漏",
          lambda: _no_plaintext_secret(json.dumps(p3, ensure_ascii=False)))
    cfg.config["llm_api_key"] = PLAIN_LLM

    # 空密钥（从未填写）必须保持空串，不能被凭空写成密文/打码值
    cfg.config["llm_api_key"] = ""
    cfg.config["webui_password"] = ""
    cfg.config["web_search_api_keys"] = {}
    p_empty = server._config_export_payload()
    check("空密钥字段导出仍为空串（不产生假密钥）",
          lambda: p_empty["llm_api_key"] == "" and p_empty["webui_password"] == "")
    check("空字典密钥字段导出仍为空字典",
          lambda: p_empty["web_search_api_keys"] == {})
    check("空密钥不会被当成明文而打码",
          lambda: M._SECRET_REDACTION not in json.dumps(p_empty, ensure_ascii=False))
    cfg.config["llm_api_key"] = PLAIN_LLM
    cfg.config["webui_password"] = PLAIN_PASSWORD
    cfg.config["web_search_api_keys"] = {"bing_key": PLAIN_SEARCH, "keep_me": EXISTING_ENC}

    # _encrypt_value 也不能把打码预览加密成"真密钥"
    check("_encrypt_value 对打码预览原样返回",
          lambda: M._encrypt_value(MASKED) == MASKED)
    check("_encrypt_value 对空串原样返回", lambda: M._encrypt_value("") == "")

    # 导出的是本机密文：必须能解回原文，否则等于丢密钥
    check("导出密文可解回原文（密钥未丢失）",
          lambda: M._decrypt_value(payload["llm_api_key"]) == PLAIN_LLM)

    # 深层嵌套结构里的明文密钥也不能漏
    cfg.config["web_search_api_keys"] = {
        "bing_key": PLAIN_SEARCH,
        "nested": {"deep": [{"inner_key": PLAIN_LLM}]},
        "keep_me": EXISTING_ENC,
    }
    p4 = server._config_export_payload()
    nested_json = json.dumps(p4, ensure_ascii=False)
    check("嵌套结构中的明文密钥同样被加密", lambda: "sk-" not in nested_json)
    check("嵌套结构中的原有密文仍原样保留",
          lambda: p4["web_search_api_keys"]["keep_me"] == EXISTING_ENC)
    cfg.config["web_search_api_keys"] = {"bing_key": PLAIN_SEARCH, "keep_me": EXISTING_ENC}

    # 敏感字段之外、但长得像密钥的残留明文也不能漏（按前缀识别）
    cfg.config["llm_extra_body"] = '{"k": "sk-sneaky-0001"}'
    p5 = server._config_export_payload()
    check("敏感字段之外的 sk- 明文同样被处理",
          lambda: "sk-sneaky-0001" not in json.dumps(p5, ensure_ascii=False))
    cfg.config["llm_extra_body"] = ""

    # _looks_like_plaintext_secret 单元边界
    looks = M._looks_like_plaintext_secret
    check("_looks_like_plaintext_secret 识别明文密钥",
          lambda: looks("sk-abc") and looks("ghp_xyz") and looks("AIzaSy123"))
    check("_looks_like_plaintext_secret 放行密文与打码值",
          lambda: not looks(EXISTING_ENC) and not looks("enc:abc")
          and not looks("sk-a****z（51位）") and not looks(""))


# ============================================================================
# C 导出链路健壮性
# ============================================================================
def c_export_error_handling():
    section("C 导出链路健壮性（异常不静默失败）")
    server, _, cfg = build_server()

    orig = server._config_export_payload

    def boom():
        raise RuntimeError("模拟导出异常")

    server._config_export_payload = boom

    async def flow():
        req = type("R", (), {})()
        resp = await server.handle_export_config(req)
        data = json.loads(resp.body.decode("utf-8"))
        check("导出内部异常时返回 mode=error 而非静默成功",
              lambda: data.get("mode") == "error" and "模拟导出异常" in data.get("path", ""))

    asyncio.run(flow())
    server._config_export_payload = orig

    # 取消保存框：返回 cancelled，不抛异常
    async def flow2():
        orig_dialog = server._save_export_via_dialog

        async def cancelled(name, content):
            return "cancelled"

        server._save_export_via_dialog = cancelled
        resp = await server.handle_export_config(type("R", (), {})())
        data = json.loads(resp.body.decode("utf-8"))
        check("用户取消保存时返回 mode=cancelled",
              lambda: data.get("mode") == "cancelled")
        server._save_export_via_dialog = orig_dialog

    asyncio.run(flow2())


# ============================================================================
# E WebUI 密码加密往返（导出导入后原密码仍可用）
# ============================================================================
def e_webui_password_roundtrip():
    section("E WebUI 密码加密往返（导出→导入→重启后原密码仍可用）")

    work = TMP / "pw_rt"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    (work / "ref").mkdir(parents=True, exist_ok=True)
    ref = work / "ref"

    # ---- 磁盘落盘必须是密文（回归：曾经直接写明文） ----
    cfg = M.ConfigLoader(str(work / "disk.json"))
    cfg.config["ref_audio_root"] = str(ref)
    cfg.config["webui_password"] = PLAIN_PASSWORD
    cfg._atomic_save(cfg.config)
    disk_pw = json.loads((work / "disk.json").read_text(encoding="utf-8"))["webui_password"]
    check("WebUI 密码落盘为 enc2: 密文（不写明文）",
          lambda: disk_pw.startswith("enc2:") and PLAIN_PASSWORD not in disk_pw)
    check("落盘密文可解回原文", lambda: M._decrypt_value(disk_pw) == PLAIN_PASSWORD)

    # ---- 重新加载后内存是明文（登录校验用的值） ----
    cfg2 = M.ConfigLoader(str(work / "disk.json"))
    check("读盘后内存密码为明文原文",
          lambda: cfg2.config.get("webui_password") == PLAIN_PASSWORD)
    check("读盘后内存不是未解密的密文",
          lambda: not str(cfg2.config.get("webui_password")).startswith("enc2:"))

    # ---- 旧配置（明文密码）能平滑升级，不丢密码 ----
    legacy = M.ConfigLoader(str(work / "legacy.json")).default_config()
    legacy.update({"ref_audio_root": str(ref), "webui_password": "LegacyPlainPass-1"})
    (work / "legacy.json").write_text(
        json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8")
    lc = M.ConfigLoader(str(work / "legacy.json"))
    check("旧明文密码配置加载后仍可用",
          lambda: lc.config.get("webui_password") == "LegacyPlainPass-1")
    lc._atomic_save(lc.config)
    lc_disk = json.loads((work / "legacy.json").read_text(encoding="utf-8"))
    check("旧明文密码保存一次后自动升级为密文",
          lambda: str(lc_disk["webui_password"]).startswith("enc2:"))
    check("升级后仍能解回原密码",
          lambda: M._decrypt_value(lc_disk["webui_password"]) == "LegacyPlainPass-1")

    # ---- 空密码（不启用保护）保持空 ----
    cfg3 = M.ConfigLoader(str(work / "empty.json"))
    cfg3.config["ref_audio_root"] = str(ref)
    cfg3.config["webui_password"] = ""
    cfg3._atomic_save(cfg3.config)
    check("空密码落盘仍为空串（不产生假密钥）",
          lambda: json.loads((work / "empty.json").read_text(encoding="utf-8"))
          ["webui_password"] == "")

    # ---- 特殊字符密码往返 ----
    check("中文/emoji 密码加解密往返一致",
          lambda: M._decrypt_value(M._encrypt_value("中文密码测试🀄")) == "中文密码测试🀄")
    check("含引号反斜杠的密码往返一致",
          lambda: M._decrypt_value(M._encrypt_value('p@$"\\w/ord<>#'))
          == 'p@$"\\w/ord<>#')
    check("含首尾空格的密码往返一致",
          lambda: M._decrypt_value(M._encrypt_value("  含空格  ")) == "  含空格  ")

    # ---- 外来密文（别的机器导出、本机解不开）不置空 ----
    foreign = "enc2:" + __import__("base64").b64encode(
        b"\x00" * 16 + b"\x00" * 8 + b"garbage-cipher").decode()
    fu = M._decrypt_webui_password({"webui_password": foreign})
    check("解不开的外来密文原样保留（不静默清空密码）",
          lambda: fu["webui_password"] == foreign)


def e2_import_decrypts_password():
    section("E2 导入接口解密 webui_password（回归：导入后原密码登不进）")
    work = TMP / "pw_imp"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    (work / "ref").mkdir(parents=True, exist_ok=True)

    # A 侧：构造导出内容
    srv_a, _, _ = build_server(path=work / "a.json", password=PLAIN_PASSWORD)
    exported = srv_a._config_export_payload()
    exported_text = json.dumps(exported, ensure_ascii=False)
    check("导出内容里密码是密文",
          lambda: str(exported.get("webui_password", "")).startswith("enc2:"))
    check("导出内容不含明文密码", lambda: PLAIN_PASSWORD not in exported_text)

    # B 侧：全新实例导入
    srv_b, port_b, cfg_b = build_server(path=work / "b.json", password="OtherPass-999")
    base_b = f"http://127.0.0.1:{port_b}"

    async def flow():
        await srv_b.start()
        try:
            async with httpx.AsyncClient(timeout=30) as hc:
                await hc.post(base_b + "/api/auth/login",
                              json={"password": "OtherPass-999", "remember_minutes": 30})
                r = await hc.post(base_b + "/api/config/import",
                                  files={"file": ("a.json", exported_text.encode("utf-8"),
                                                  "application/json")})
                check("导入返回成功",
                      lambda: r.status_code == 200 and r.json().get("success") is True)

                pw_mem = srv_b.config.config.get("webui_password")
                check("导入后内存密码为明文原文（不是密文）",
                      lambda: pw_mem == PLAIN_PASSWORD)
                check("导入后内存密码不是 enc2: 密文",
                      lambda: not str(pw_mem).startswith("enc2:"))

                disk = json.loads((work / "b.json").read_text(encoding="utf-8"))
                check("导入后磁盘密码是密文",
                      lambda: str(disk.get("webui_password")).startswith("enc2:"))

                r = await hc.post(base_b + "/api/auth/login",
                                  json={"password": PLAIN_PASSWORD,
                                        "remember_minutes": 30})
                check("导入后用原密码能登录（核心诉求）",
                      lambda: r.status_code == 200)
                r = await hc.post(base_b + "/api/auth/login",
                                  json={"password": "OtherPass-999",
                                        "remember_minutes": 30})
                check("导入后旧密码失效（密码确实被换掉了）",
                      lambda: r.status_code == 401)
        finally:
            await srv_b.shutdown()

    asyncio.run(flow())

    # 等价于重启：用磁盘配置重新构造实例
    cfg_r = M.ConfigLoader(str(work / "b.json"))
    check("重启后内存密码仍为原文",
          lambda: cfg_r.config.get("webui_password") == PLAIN_PASSWORD)


# ============================================================================
# D 前端绑定静态校验
# ============================================================================
def d_frontend_binding():
    section("D 前端导出链路静态校验")
    html = (ROOT / "webui" / "start.html").read_text(encoding="utf-8")
    check("存在切换预设按钮元素 config-preset-btn",
          lambda: 'id="config-preset-btn"' in html)
    check("存在切换预设按钮点击绑定",
          lambda: "getElementById('config-preset-btn').addEventListener('click'" in html)
    check("导出当前配置走预设接口",
          lambda: "apiPost('api/config/presets/save'" in html)
    check("downloadFile 对无 Content-Disposition 的响应有兜底",
          lambda: "return payload || {mode: 'saved'}" in html)
    check("导出请求带超时保护",
          lambda: "withTimeout(fetchGuarded(url, {}), 120000" in html)
    check("按钮点击期间禁用防连点", lambda: "btn.disabled = true" in html)


def main():
    a_export_endpoint_works()
    b_no_plaintext_secrets()
    c_export_error_handling()
    e_webui_password_roundtrip()
    e2_import_decrypts_password()
    d_frontend_binding()

    print(f"\n{'=' * 70}")
    print(f"PASS {len(PASS)}  FAIL {len(FAIL)}")
    if FAIL:
        for name, err in FAIL:
            print(f"  - {name}: {err}")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
