# -*- coding: utf-8 -*-
"""自主学习（黑话词典）回归测试。

覆盖：学习提示词契约、配置默认值与默认开启、置信度分流、待确认裁决、
含义更新与删除、落盘重载、词条上限淘汰、注入开关与长度限制、触发条件、
历史到词典的学习链路（模型调用与提示词组装用替身）。

运行: python tests/test_lexicon.py     （全通过退出码 0）
"""
import io
import json
import os
import shutil
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

from modules.lexicon import (CATEGORIES, DEFAULT_INJECT_TEMPLATE, DEFAULT_LEARN_PROMPT,
                             LEGACY_LEARN_TAIL_PHRASES, LexiconManager,
                             QUARANTINE_REASON, migrate_learn_prompt)

PASS, FAIL = [], []
TMP_ROOT = Path(tempfile.mkdtemp(prefix="lexicon_test_"))


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


def _mgr(name, **overrides):
    cfg = {
        "learning_enabled": True,
        "learning_min_confidence": 0.6,
        "learning_max_terms": 200,
        "learning_max_chars": 400,
        "learning_trigger_messages": 20,
    }
    cfg.update(overrides)
    return LexiconManager(cfg, TMP_ROOT / name)


def _item(term, meaning, confidence, action="add", category="网络黑话", **extra):
    data = {"term": term, "meaning": meaning, "category": category,
            "confidence": confidence, "action": action}
    data.update(extra)
    return data


def t1():
    section("1 学习提示词覆盖四类要求")
    prompt = DEFAULT_LEARN_PROMPT
    for token in ("学习触发条件", "识别范围", "排除项", "含义推断判定规则",
                  "存储与更新", "confidence", "uncertain_reason", "action",
                  "网络黑话", "俚语", "专有表达", "指代称呼"):
        check(f"提示词含「{token}」", lambda t=token: t in prompt)
    check("提示词要求只输出 JSON", lambda: "只输出JSON" in prompt.replace(" ", ""))
    check("提示词声明了低分条目的去向",
          lambda: "待确认" in prompt and "不确定" in prompt)
    check("注入模板带 {terms} 占位", lambda: "{terms}" in DEFAULT_INJECT_TEMPLATE)
    # 含义必须是具体所指：曾把有本义的词学成「一种暧昧状态」，抽象化与网络流行义都要挡住
    check("提示词要求先按字面本义理解",
          lambda: "先看字面" in prompt and "网络流行义" in prompt)
    check("提示词禁止把具体现象概括成抽象状态",
          lambda: "一种状态" in prompt and "一种氛围" in prompt)
    # 词典是所有角色共用的：含义里带角色名或只对某次对话成立的说法，换个角色就读不通
    check("提示词要求含义通用、不绑角色",
          lambda: "含义必须通用" in prompt and "所有角色" in prompt
          and "禁止出现角色名" in prompt and "专属称呼" in prompt)
    check("提示词排除角色名与临时改名",
          lambda: "临时给角色起的名字或称呼" in prompt)
    check("提示词要求已有词典里的绑角色条目改写成通用含义",
          lambda: "按 update 用通用含义重写" in prompt)
    old_tail, new_tail = LEGACY_LEARN_TAIL_PHRASES[0]
    legacy = {"learning_prompt": DEFAULT_LEARN_PROMPT.replace(new_tail, old_tail)}
    check("旧默认提示词被补上字面规则",
          lambda: legacy["learning_prompt"] != DEFAULT_LEARN_PROMPT
          and migrate_learn_prompt(legacy)
          and legacy["learning_prompt"] == DEFAULT_LEARN_PROMPT)
    check("迁移是幂等的", lambda: migrate_learn_prompt(legacy) is False)
    check("自定义过的提示词不被改写",
          lambda: migrate_learn_prompt({"learning_prompt": "只学黑话"}) is False
          and migrate_learn_prompt({"learning_prompt": ""}) is False)


def t2():
    section("2 配置默认值（默认开启）")
    import main as M

    d = M.ConfigLoader.default_config()
    check("learning_enabled 默认开启", lambda: d.get("learning_enabled") is True)
    for key in ("learning_trigger_messages", "learning_min_confidence",
                "learning_history_lines", "learning_max_terms",
                "learning_max_chars", "learning_prompt", "learning_inject_template"):
        check(f"默认配置含 {key}", lambda k=key: k in d)
    check("学习提示词与模块常量一致",
          lambda: d.get("learning_prompt") == DEFAULT_LEARN_PROMPT)
    check("注入模板与模块常量一致",
          lambda: d.get("learning_inject_template") == DEFAULT_INJECT_TEMPLATE)


def t3():
    section("3 置信度分流：达标入库、不达标进待确认")
    m = _mgr("t3")
    result = m.apply_items([
        _item("yyds", "永远的神，表示极度赞赏", 0.9, evidence="这波操作yyds"),
        _item("拴Q", "谢谢你", 0.3, evidence="我真的拴Q", uncertain_reason="缺少上下文"),
    ])
    check("达标词条入库", lambda: result["added"] == 1 and "yyds" in m.terms)
    check("低置信度词条入待确认", lambda: result["held"] == 1 and len(m.pending) == 1)
    check("低置信度词条不进词典", lambda: "拴Q" not in m.terms)
    check("待确认保留不确定原因", lambda: m.pending[0]["reason"] == "缺少上下文")
    check("待确认词条不参与注入", lambda: "拴Q" not in m.build_injection())


def t4():
    section("4 待确认词条：通过 / 改后通过 / 丢弃")
    m = _mgr("t4")
    m.apply_items([_item("乐", "嘲讽对方可笑", 0.5, category="俚语")])
    pid = m.pending[0]["id"]
    check("采纳前词典为空", lambda: not m.terms)
    check("丢弃不存在的 id 返回假", lambda: m.reject("nope") is False)
    check("按 id 采纳并改写含义", lambda: m.confirm(pid, meaning="嘲讽对方很可笑"))
    check("采纳后进词典", lambda: m.terms["乐"]["meaning"] == "嘲讽对方很可笑")
    check("采纳后待确认清空", lambda: not m.pending)
    check("重复采纳返回假", lambda: m.confirm(pid) is False)
    m.apply_items([_item("乐", "另一种猜测", 0.5, category="俚语")])
    check("同词新猜测再次入队", lambda: len(m.pending) == 1)
    check("丢弃成功", lambda: m.reject(m.pending[0]["id"]) is True)
    check("丢弃后待确认清空", lambda: not m.pending)


def t5():
    section("5 含义更新与词条删除")
    m = _mgr("t5")
    m.apply_items([_item("绷不住了", "忍不住笑了", 0.9)])
    first = m.terms["绷不住了"]["count"]
    m.apply_items([_item("绷不住了", "忍不住笑了", 0.9)])
    check("重复学习累计出现次数", lambda: m.terms["绷不住了"]["count"] == first + 1)
    m.apply_items([_item("绷不住了", "绷不住笑出声", 0.9, action="update")])
    check("update 覆盖旧含义",
          lambda: m.terms["绷不住了"]["meaning"] == "绷不住笑出声")
    m.apply_items([_item("绷不住了", "", 0.9, action="remove")])
    check("remove 删除词条", lambda: "绷不住了" not in m.terms)
    check("手动新增词条", lambda: m.upsert_term("绝绝子", "非常棒", "网络黑话"))
    check("手动新增后可见", lambda: m.terms["绝绝子"]["meaning"] == "非常棒")
    check("手动删除词条", lambda: m.delete_term("绝绝子") and "绝绝子" not in m.terms)
    check("删除不存在的词条返回假", lambda: m.delete_term("不存在") is False)


def t6():
    section("6 落盘与重载")
    m = _mgr("t6")
    m.apply_items([_item("抽象", "指言行离谱好笑", 0.9, category="俚语")])
    m.apply_items([_item("谜语人", "说话绕弯不直说", 0.2, category="俚语")])
    reloaded = LexiconManager(m.config, TMP_ROOT / "t6")
    check("重载后词条仍在", lambda: "抽象" in reloaded.terms)
    check("重载后待确认仍在", lambda: len(reloaded.pending) == 1)
    check("重载后含义不变",
          lambda: reloaded.terms["抽象"]["meaning"] == "指言行离谱好笑")


def t7():
    section("7 词条上限淘汰")
    m = _mgr("t7", learning_max_terms=10)
    m.apply_items([_item(f"词{i}", f"含义{i}", 0.9, category="俚语") for i in range(15)])
    check("不超过上限", lambda: len(m.terms) == 10)
    check("淘汰最久未更新的低频词", lambda: "词0" not in m.terms and "词14" in m.terms)
    m.apply_items([_item("词14", "含义14", 0.9, category="俚语") for _ in range(3)])
    check("高频词不会被先淘汰", lambda: m.terms["词14"]["count"] == 4)


def t8():
    section("8 注入开关与长度限制")
    m = _mgr("t8")
    check("词典为空时不注入", lambda: m.build_injection() == "")
    m.apply_items([_item("A", "含义A", 0.9, category="俚语")])
    check("注入含词条与含义", lambda: "A：含义A" in m.build_injection())
    m.config["learning_enabled"] = False
    check("关闭总开关后不注入", lambda: m.build_injection() == "")
    m.config["learning_enabled"] = True
    m.config["learning_max_chars"] = 80
    m.apply_items([_item(f"长词{i}", "很长的含义" * 5, 0.9, category="俚语")
                   for i in range(10)])
    check("注入受长度上限约束", lambda: len(m.build_injection()) < 400)


def t9():
    section("9 学习触发条件")
    m = _mgr("t9", learning_trigger_messages=5)
    check("未到间隔不触发", lambda: not m.should_learn(4))
    check("到达间隔触发", lambda: m.should_learn(5))
    check("计数为 0 不触发", lambda: not m.should_learn(0))
    check("非数字计数不触发", lambda: not m.should_learn("abc"))
    m.config["learning_enabled"] = False
    check("总开关关闭不触发", lambda: not m.should_learn(5))


def t10():
    section("10 词条字段清洗")
    m = _mgr("t10")
    m.apply_items([_item("  多余   空格  ", "含\n换行 的含义", 1.5,
                         category="不存在的分类")])
    term = m.list_terms()[0]
    check("表达归一空白", lambda: term["term"] == "多余 空格")
    check("含义归一空白", lambda: term["meaning"] == "含 换行 的含义")
    check("非法分类回落到合法分类", lambda: term["category"] in CATEGORIES)
    check("置信度截断到 1", lambda: term["confidence"] == 1.0)
    check("缺含义的条目被忽略",
          lambda: m.apply_items([_item("无含义", "", 0.9)])["added"] == 0
          and "无含义" not in m.terms)
    check("缺表达的条目被忽略",
          lambda: m.apply_items([_item("", "有含义", 0.9)])["added"] == 0)


def t11():
    """learn_from_history 端到端：历史 → 提示词 → 模型输出 → 分流 → 落盘。"""
    import asyncio

    from modules import lexicon as lex_mod

    mgr = _mgr("t11")
    seen = {}

    async def fake_generate(ctx, system, user_prompt, **kwargs):
        seen["system"] = system
        seen["user"] = user_prompt
        return {"learned": [_item("蒸鹅心", "真恶心的谐音", 0.9),
                            _item("何意味", "什么意思", 0.5)]}

    real = lex_mod.generate_json_reply
    lex_mod.generate_json_reply = fake_generate
    try:
        history = [
            {"role": "user", "content": "这波操作蒸鹅心", "sender_id": "1001",
             "sender_name": "甲"},
            {"role": "assistant", "content": "哈哈确实", "speaker": "bot"},
            {"role": "user", "content": "何意味", "sender_id": "1002",
             "sender_name": "乙"},
        ]
        out = asyncio.run(mgr.learn_from_history({"character_key": "murasame"},
                                                 history, "group_1"))
    finally:
        lex_mod.generate_json_reply = real

    check("学习调用模型并返回分流结果",
          lambda: out == {"added": 1, "updated": 0, "removed": 0, "held": 1, "skipped": 0})
    check("达标词条写入词典",
          lambda: [t["term"] for t in mgr.list_terms()] == ["蒸鹅心"])
    check("低分词条进待确认",
          lambda: [p["term"] for p in mgr.list_pending()] == ["何意味"])
    check("提示词带上历史对话",
          lambda: "蒸鹅心" in seen["user"] and "何意味" in seen["user"])
    check("提示词带上已有词典", lambda: "已有词典" in seen["system"])
    check("学习结果落盘",
          lambda: (TMP_ROOT / "t11" / "learned_terms.json").exists())
    check("空历史不调用模型",
          lambda: asyncio.run(mgr.learn_from_history({}, [], "group_1")) == {})
    check("总开关关闭不学习",
          lambda: asyncio.run(_mgr("t11_off", learning_enabled=False)
                              .learn_from_history({}, history, "group_1")) == {})


def t12():
    """跨角色通用性：含义绑定具体角色的词条不能进词典，已有的旧条目移入待确认。"""
    section("12 含义必须跨角色通用")

    cfg = {"learning_enabled": True, "character_name": "丛雨", "character_key": "murasame",
           "roles": [{"character_name": "丛雨", "character_key": "murasame"},
                     {"character_name": "小町", "character_key": "komachi"}]}
    mgr = LexiconManager(cfg, TMP_ROOT / "t12")
    out = mgr.apply_items([
        _item("老婆", "用户对虚拟角色丛雨的专属配偶称呼", 0.95),
        _item("丛雨", "指代该虚拟角色自身", 0.95),
        _item("宝宝", "本次对话里被角色拒绝的称呼，用户不能再这么叫", 0.9),
        _item("老婆", "对妻子或伴侣的口语称呼，亲密关系里也用来称呼恋人", 0.9),
        _item("yyds", "永远的神，用来夸某人某物非常好", 0.9),
        _item("杂鱼", "指代地位低下、能力弱小或被轻视的对象，此处用于在暧昧时辱骂用户。", 0.9),
    ])
    check("含义绑角色或编号的条目不入库",
          lambda: out["skipped"] == 3 and out["added"] == 3)
    check("通用含义照常入库",
          lambda: mgr.terms["老婆"]["meaning"].startswith("对妻子或伴侣")
          and "yyds" in mgr.terms and "杂鱼" in mgr.terms)
    check("角色名不会被学成词条",
          lambda: "丛雨" not in mgr.terms and "宝宝" not in mgr.terms)
    # 「此处用于…」只是说明这个说法怎么用，换个角色照样成立，不算绑定某次对话
    check("描述一般使用场景的含义照常入库",
          lambda: mgr.terms["杂鱼"]["meaning"].startswith("指代地位低下")
          and "杂鱼" in mgr.build_injection())

    # 旧数据：含义里写了角色名的条目在加载时移入待确认（不再参与回复，也不丢内容）
    legacy = TMP_ROOT / "t12_legacy"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / "learned_terms.json").write_text(json.dumps({
        "terms": {
            "老婆": {"term": "老婆", "meaning": "用户对虚拟角色丛雨的专属配偶称呼",
                     "category": "指代称呼", "confidence": 0.95, "evidence": "", "count": 1},
            "小雨": {"term": "小雨", "meaning": "对话中对角色'丛雨'的昵称",
                     "category": "指代称呼", "confidence": 1.0, "evidence": "", "count": 1},
            "yyds": {"term": "yyds", "meaning": "永远的神，用来夸某人某物非常好",
                     "category": "网络黑话", "confidence": 0.9, "evidence": "", "count": 1},
        }, "pending": []}, ensure_ascii=False), encoding="utf-8")
    reloaded = LexiconManager(cfg, legacy)
    check("旧条目按角色名隔离到待确认",
          lambda: "老婆" not in reloaded.terms and "小雨" not in reloaded.terms
          and "yyds" in reloaded.terms
          and {p["term"] for p in reloaded.pending} == {"老婆", "小雨"})
    check("待确认条目给出可读原因",
          lambda: all("换个角色不适用" in p["reason"] for p in reloaded.pending))
    check("隔离后不再注入给模型",
          lambda: "老婆" not in reloaded.build_injection()
          and "yyds" in reloaded.build_injection())
    check("重新学到通用含义时把待确认项顶掉",
          lambda: reimport_and_relearn(legacy, cfg))

    confirmed = TMP_ROOT / "t12_confirmed"
    confirmed.mkdir(parents=True, exist_ok=True)
    (confirmed / "learned_terms.json").write_text(json.dumps({
        "terms": {},
        "pending": [{"id": "p1", "term": "小雨",
                     "meaning": "对话中对角色'丛雨'的昵称", "category": "指代称呼",
                     "confidence": 1.0, "evidence": "", "reason": "把握不足",
                     "created_at": 0}]}, ensure_ascii=False), encoding="utf-8")
    first = LexiconManager(cfg, confirmed)
    check("页面上采纳待确认词条", lambda: first.confirm("p1") and "小雨" in first.terms)
    reloaded2 = LexiconManager(cfg, confirmed)
    check("采纳过的词条重启后不再回到待确认",
          lambda: "小雨" in reloaded2.terms and not reloaded2.pending
          and reloaded2.terms["小雨"].get("confirmed") is True)

    # 判定放宽后，早先被隔离、其实通用的条目自动放回词典（理由已经不成立）
    stale = TMP_ROOT / "t12_stale"
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "learned_terms.json").write_text(json.dumps({
        "terms": {},
        "pending": [
            {"id": "q1", "term": "杂鱼",
             "meaning": "指代地位低下、能力弱小或被轻视的对象，此处用于在暧昧时辱骂用户。",
             "category": "俚语", "confidence": 0.9, "evidence": "", "count": 1,
             "reason": QUARANTINE_REASON, "created_at": 0},
            {"id": "q2", "term": "小雨", "meaning": "对话中对角色'丛雨'的昵称",
             "category": "指代称呼", "confidence": 1.0, "evidence": "",
             "reason": QUARANTINE_REASON, "created_at": 0},
            {"id": "q3", "term": "某个梗", "meaning": "把握不足的候选",
             "category": "网络黑话", "confidence": 0.3, "evidence": "",
             "reason": "对话里没有足够依据判断含义", "created_at": 0}]},
        ensure_ascii=False), encoding="utf-8")
    restored = LexiconManager(cfg, stale)
    check("放宽判定后通用的隔离条目自动放回",
          lambda: "杂鱼" in restored.terms and "杂鱼" in restored.build_injection())
    check("仍然绑角色的条目留在待确认",
          lambda: {p["term"] for p in restored.pending} == {"小雨", "某个梗"})


def reimport_and_relearn(legacy: Path, cfg: dict) -> bool:
    mgr = LexiconManager(cfg, legacy)
    mgr.apply_items([_item("老婆", "对妻子或伴侣的口语称呼", 0.9, category="指代称呼")])
    return (mgr.terms["老婆"]["meaning"] == "对妻子或伴侣的口语称呼"
            and not any(p["term"] == "老婆" for p in mgr.pending))


def main():
    for fn in (t1, t2, t3, t4, t5, t6, t7, t8, t9, t10, t11, t12):
        fn()
    shutil.rmtree(TMP_ROOT, ignore_errors=True)
    print(f"\n{'=' * 70}")
    print(f"结果：通过 {len(PASS)} | 失败 {len(FAIL)}")
    for name, err in FAIL:
        print(f"  - {name}: {err}")
    print(f"{'=' * 70}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
