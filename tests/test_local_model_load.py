# -*- coding: utf-8 -*-
"""启动时预加载本地模型的测试。

覆盖三件事：
1. 开关默认开启，且落在配置页的「更多 → 本地模型」里；
2. 预加载的触发条件：开关关闭、非 openai 接口、服务地址不在本机时都不加载；
   满足条件时对话与识图模型（含角色级覆盖）各加载一次、不重复；
3. 主程序启动流程里真的调了预加载。

运行: python tests/test_local_model_load.py     （全通过退出码 0）
"""
import io
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
sys.stdin = io.StringIO()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import main as M
import modules.llm_helpers as LH

PASS, FAIL = [], []
HTML = (ROOT / "webui" / "start.html").read_text(encoding="utf-8")


def check(name, fn):
    try:
        result = fn()
        if not result:
            raise AssertionError(f"断言为假: {result!r}")
        PASS.append(name)
        print(f"  [PASS] {name}")
    except Exception as e:
        FAIL.append((name, f"{type(e).__name__}: {e}"))
        print(f"  [FAIL] {name}: {type(e).__name__}: {e}")


def section(title):
    print(f"\n{title}")
    print("-" * 70)


class _SyncThread:
    """把预加载的后台线程改成同步执行，好在测试里直接断言加载了哪些模型。"""

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


def _run_auto_load(ctx, lms_ok=True):
    """跑一次预加载，返回 (走 lms 命令行的模型, 走预热请求的 (模型, 用途))。"""
    loaded, warmed = [], []
    saved = (LH.threading, LH._lmstudio_load, LH._warmup_local_model)
    LH.threading = types.SimpleNamespace(Thread=_SyncThread)
    LH._lmstudio_load = lambda model_key: loaded.append(model_key) or lms_ok
    LH._warmup_local_model = lambda backend, url, key, model, kind="chat": (
        warmed.append((model, kind)) or True)
    try:
        LH.auto_load_local_models(ctx if isinstance(ctx, list) else [ctx])
    finally:
        LH.threading, LH._lmstudio_load, LH._warmup_local_model = saved
    return loaded, warmed


def _warmed_models(warmed):
    return [name for name, _ in warmed]


def _local_ctx(**overrides):
    ctx = {
        "llm_auto_load_local": True,
        "llm_backend": "openai",
        "llm_base_url": "http://127.0.0.1:1234/v1",
        "llm_model_name": "chat-model",
        "image_caption_model_name": "vision-model",
    }
    ctx.update(overrides)
    return ctx


def t1_default_on():
    return M.ConfigLoader.default_config().get("llm_auto_load_local") is True


def t2_in_more_group():
    block = HTML[HTML.index("const configGroups = ["):HTML.index("const configTailGroups")]
    return "{ title: '本地模型', keys: ['llm_auto_load_local'] }" in block


def t3_meta_row():
    meta = HTML[HTML.index("const configMeta = {"):]
    return "'llm_auto_load_local': {" in meta and "default: true" in meta.split(
        "'llm_auto_load_local': {")[1].split("},")[0]


def t4_as_bool():
    return (LH._as_bool(None) is True and LH._as_bool(None, False) is False
            and LH._as_bool("false") is False and LH._as_bool("0") is False
            and LH._as_bool(True) is True and LH._as_bool("true") is True)


def t5_disabled_skips():
    return _run_auto_load(_local_ctx(llm_auto_load_local=False)) == ([], [])


def t6_remote_service_skips():
    return _run_auto_load(_local_ctx(llm_base_url="https://api.deepseek.com")) == ([], [])


def t7_no_model_skips():
    return _run_auto_load(_local_ctx(llm_model_name="", image_caption_model_name="")) == ([], [])


def t8_lmstudio_first():
    loaded, warmed = _run_auto_load(_local_ctx(roles=[{"llm_model_name": "role-model"},
                                                      {"llm_model_name": "chat-model"}],
                                               llm_embedding_model="embed-model",
                                               rag_embedding_model="rag-embed"))
    return loaded == ["chat-model", "vision-model", "role-model", "embed-model"] \
        and warmed == []


def t9_ollama_warms_up():
    """Ollama 也预热；它的嵌入模型按 rag.py 的取法固定走本机默认地址。"""
    loaded, warmed = _run_auto_load(_local_ctx(llm_backend="ollama",
                                               llm_base_url="http://127.0.0.1:11434"),
                                    lms_ok=False)
    return loaded == [] and warmed == [("chat-model", "chat"), ("vision-model", "chat"),
                                       ("nomic-embed-text", "embedding")]


def t10_no_cli_warms_up():
    loaded, warmed = _run_auto_load(_local_ctx(), lms_ok=False)
    return warmed == [("chat-model", "chat"), ("vision-model", "chat")]


def t11_cloud_vision_skipped():
    ctx = _local_ctx(image_caption_backend="openai",
                     image_caption_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1")
    loaded, warmed = _run_auto_load(ctx, lms_ok=False)
    return _warmed_models(warmed) == ["chat-model"]


def t12_merges_configs():
    cloud = _local_ctx(llm_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                       llm_model_name="cloud-model", image_caption_model_name="")
    profile = _local_ctx(llm_model_name="chat-model", image_caption_model_name="chat-model")
    loaded, _ = _run_auto_load([cloud, profile])
    return loaded == ["chat-model"]


def t13_embedding_warms_up():
    loaded, warmed = _run_auto_load(_local_ctx(llm_embedding_model="embed-model"), lms_ok=False)
    return warmed == [("chat-model", "chat"), ("vision-model", "chat"),
                      ("embed-model", "embedding")]


def t14_embedding_same_as_chat():
    """嵌入模型与对话模型同名时不重复预热：对话那次已经把它带起来了。"""
    _, warmed = _run_auto_load(_local_ctx(rag_embedding_model="chat-model"), lms_ok=False)
    return warmed == [("chat-model", "chat"), ("vision-model", "chat")]


def t15_cloud_embedding_skipped():
    ctx = _local_ctx(
        llm_embedding_model="embed-model",
        llm_embedding_url="https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings")
    _, warmed = _run_auto_load(ctx, lms_ok=False)
    return _warmed_models(warmed) == ["chat-model", "vision-model"]


def t16_wired_in_main():
    import inspect
    src = inspect.getsource(M.main)
    return "auto_load_local_models(preload_configs)" in src \
        and "config_for_connection(" in src


def t17_already_loaded_skips_cli():
    """已加载的模型不再执行 lms load：再 load 一次会另起实例，显存翻倍。"""
    calls = []
    saved = (LH._lmstudio_cli, LH._lmstudio_loaded, LH._lms_command)
    LH._lmstudio_cli = lambda: "lms"
    LH._lmstudio_loaded = lambda exe: {"chat-model"}
    LH._lms_command = lambda exe, args, timeout: (
        calls.append(args) or types.SimpleNamespace(returncode=0))
    try:
        already = LH._lmstudio_load("chat-model")
        fresh = LH._lmstudio_load("other-model")
    finally:
        LH._lmstudio_cli, LH._lmstudio_loaded, LH._lms_command = saved
    return already is True and fresh is True and calls == [["load", "other-model"]]


def t18_no_cli_no_load():
    saved = LH._lmstudio_cli
    LH._lmstudio_cli = lambda: ""
    try:
        return LH._lmstudio_load("chat-model") is False
    finally:
        LH._lmstudio_cli = saved


def t19_lms_loaded_parses_json():
    saved = LH._lms_command
    LH._lms_command = lambda exe, args, timeout: types.SimpleNamespace(
        stdout=b'[{"modelKey": "a"}, {"modelKey": "b"}]')
    try:
        return LH._lmstudio_loaded("lms") == {"a", "b"}
    finally:
        LH._lms_command = saved


def main():
    section("A 开关与配置页")
    check("启动预加载默认开启", t1_default_on)
    check("开关落在「更多 → 本地模型」里", t2_in_more_group)
    check("配置页有这一行的说明与默认值", t3_meta_row)
    check("开关字符串 false / 0 不当成真", t4_as_bool)

    section("B 触发条件")
    check("关掉开关就不加载", t5_disabled_skips)
    check("服务地址在云端就不加载", t6_remote_service_skips)
    check("没配模型就不加载", t7_no_model_skips)
    check("本机 openai 服务优先走 lms 命令行（含角色级、不重复）", t8_lmstudio_first)
    check("本机 Ollama 也预加载（走预热请求）", t9_ollama_warms_up)
    check("没有 lms 命令行时改用预热请求", t10_no_cli_warms_up)
    check("识图模型在云端时不发本机预热请求", t11_cloud_vision_skipped)
    check("多份配置取并集去重，云端那份不参与", t12_merges_configs)

    section("C 嵌入模型")
    check("本机嵌入模型一起预热", t13_embedding_warms_up)
    check("嵌入模型与对话模型同名时不重复预热", t14_embedding_same_as_chat)
    check("嵌入端点在云端时不预热", t15_cloud_embedding_skipped)

    section("D 主程序接线")
    check("启动流程里按主配置与接入方式绑定的配置调了预加载", t16_wired_in_main)

    section("E LM Studio 命令行")
    check("已加载的模型不再重复 load（避免显存翻倍）", t17_already_loaded_skips_cli)
    check("没装 lms 命令行时不去执行它", t18_no_cli_no_load)
    check("已加载清单能解析 lms ps 的 JSON", t19_lms_loaded_parses_json)

    print(f"\n结果：通过 {len(PASS)} | 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：")
        for name, reason in FAIL:
            print(f"  - {name}: {reason}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
