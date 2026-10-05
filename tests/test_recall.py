"""会话回忆：话题分段、两种检索方式、注入文案与未了话题（全用桩，不连服务）。"""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from modules import recall  # noqa: E402
import numpy as np  # noqa: E402

PASS = []
FAIL = []


def check(name, fn):
    try:
        ok = fn()
    except Exception as e:
        FAIL.append(f"{name}: {type(e).__name__}: {e}")
        return
    (PASS if ok else FAIL).append(name if ok else f"{name}: 断言为假")


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class Cfg(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class FakeEmbedder:
    """按关键词计数造向量：同词的两个文本余弦相似度高，好断言。

    返回 numpy float32 数组，与 RAGManager.embed_texts 的真实返回一致 ——
    用 Python list 做桩会漏掉「向量元素不是 JSON 可序列化的 float」这类问题。
    """

    WORDS = ("海边", "调情", "工作", "吃饭")

    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def embed_texts(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("嵌入服务不可用")
        return np.array([[float(str(t).count(w)) for w in self.WORDS] for t in texts],
                        dtype=np.float32)


def msg(role, content, ts, **extra):
    data = {"role": role, "content": content, "timestamp": ts}
    data.update(extra)
    return data


BASE = 1_700_000_000.0
# 三段话题：海边（已结束）→ 调情（已结束）→ 工作（进行中）
HISTORY = [
    msg("user", "周末想去海边玩", BASE, sender_name="主人"),
    msg("assistant", "那本座陪你去，说好了", BASE + 10, speaker="丛雨"),
    msg("user", "什么时候出发呀", BASE + 20, sender_name="主人"),
    msg("user", "你今天真好看", BASE + 3600, sender_name="主人"),
    msg("assistant", "讨厌啦", BASE + 3610, speaker="丛雨"),
    msg("user", "这周工作好多", BASE + 7200, sender_name="主人"),
    msg("assistant", "辛苦主人了", BASE + 7210, speaker="丛雨"),
]


def t_segments():
    print("\n--- 话题分段 ---")
    groups = recall.split_segments(HISTORY)
    check("按间隔切成三段", lambda: [len(g) for g in groups] == [3, 2, 2])
    check("按条数封顶", lambda: len(recall.split_segments(HISTORY, max_messages=2)) == 4)
    check("跳过非对话条目", lambda: len(recall.split_segments(
        [{"role": "system", "content": "x", "timestamp": 0}] + HISTORY)) == 3)
    text = recall.render_segment(groups[0])
    check("段首标时间、后续行不重复标",
          lambda: text.count("[") == 1 and "海边" in text and "丛雨" in text)
    check("用户行用昵称、角色行用角色名",
          lambda: "主人：周末想去海边玩" in text and "丛雨：那本座陪你去，说好了" in text)


def t_lexical():
    print("\n--- 字符重合打分 ---")
    seg = recall.render_segment(recall.split_segments(HISTORY)[0])
    check("关键词重合得分 > 0", lambda: recall.lexical_score("上次说的海边那家店", seg) > 0)
    check("无关内容得 0 分", lambda: recall.lexical_score("今天天气怎么样", seg) == 0.0)
    check("空查询得 0 分", lambda: recall.lexical_score("", seg) == 0.0)


def t_index(tmp: Path):
    print("\n--- 索引与检索 ---")
    embedder = FakeEmbedder()
    mgr = recall.HistoryRecallManager(Cfg({"history_recall_enabled": True}), tmp, embedder)
    run(mgr.refresh("s1", HISTORY))
    index = mgr._load_index("s1")
    check("只索引已结束的话题段（进行中的最后一段不索引）",
          lambda: len(index["segments"]) == 2)
    check("每段都带时间戳", lambda: all(e.get("ts") for e in index["segments"]))
    check("每段都拿到向量", lambda: all(v for v in index["vectors"]))
    index_path = mgr.dir / f"{mgr._key('s1')}.json"
    check("索引真的写进了磁盘，且向量是可序列化的 float",
          lambda: all(isinstance(x, float)
                      for vec in json.loads(index_path.read_text(encoding="utf-8"))["vectors"]
                      if vec for x in vec))

    embedder.calls.clear()
    run(mgr.refresh("s1", HISTORY))
    check("重复刷新不重复向量化", lambda: embedder.calls == [])

    run(mgr.refresh("s1", HISTORY + [msg("user", "明天还要开会", BASE + 12000,
                                       sender_name="主人")]))
    check("新起一段后，上一段才被索引",
          lambda: len(embedder.calls) == 1 and len(embedder.calls[0]) == 1
          and "辛苦主人了" in embedder.calls[0][0])


def t_recall(tmp: Path):
    print("\n--- 检索与注入 ---")
    embedder = FakeEmbedder()
    mgr = recall.HistoryRecallManager(Cfg({"history_recall_enabled": True}), tmp, embedder)
    run(mgr.refresh("s2", HISTORY))

    hits = run(mgr.recall("s2", "海边那家店还去不去"))
    check("检索命中海边那段", lambda: len(hits) == 1 and "海边" in hits[0]["text"])
    check("无关查询不命中", lambda: run(mgr.recall("s2", "今天心情不错")) == [])
    check("top_k 生效",
          lambda: len(run(mgr.recall("s2", "海边 调情 工作", top_k=2))) <= 2)

    block = mgr.build_context(hits)
    check("注入块标注「不是刚刚发生的事」",
          lambda: "【相关往事】" in block and "不是主人现在说的话" in block)
    check("注入块受字数上限约束",
          lambda: len(mgr.build_context(hits, max_chars=5)) == 0)

    # 嵌入服务挂了要自动退回字符重合，不能整条回忆失效
    broken = recall.HistoryRecallManager(Cfg({"history_recall_enabled": True}), tmp,
                                        FakeEmbedder(fail=True))
    broken._cache["s2"] = mgr._load_index("s2")
    fallback = run(broken.recall("s2", "海边那家店还去不去"))
    check("嵌入失败时退回字符重合", lambda: len(fallback) == 1 and "海边" in fallback[0]["text"])

    # 词面模式：不走嵌入
    lexical_only = recall.HistoryRecallManager(
        Cfg({"history_recall_enabled": True, "history_recall_mode": "lexical"}), tmp,
        FakeEmbedder())
    lexical_only._cache["s2"] = mgr._load_index("s2")
    check("词面模式不调用嵌入",
          lambda: run(lexical_only.recall("s2", "海边那家店")) != []
          and lexical_only.embedder.calls == [])

    # 已在最近窗口里的往事不再注入
    check("最近窗口内的段被排除",
          lambda: run(mgr.recall("s2", "海边那家店", exclude_after_ts=BASE)) == [])

    off = recall.HistoryRecallManager(Cfg({"history_recall_enabled": False}), tmp, embedder)
    off._cache["s2"] = mgr._load_index("s2")
    check("总开关关闭时不检索", lambda: run(off.recall("s2", "海边那家店")) == [])


def t_open_topics(tmp: Path):
    print("\n--- 未了话题 ---")
    mgr = recall.HistoryRecallManager(Cfg({"history_recall_enabled": True}), tmp)
    check("没有清单时不注入", lambda: mgr.open_topics_block("s3") == "")

    calls = {"n": 0}

    async def fake_chat_once(ctx, messages, **kwargs):
        calls["n"] += 1
        return {"content": json.dumps({"topics": ["说好要去海边玩，还没定哪天出发"]},
                                      ensure_ascii=False)}

    original = recall.chat_once
    recall.chat_once = fake_chat_once
    try:
        for _ in range(recall.OPEN_TOPICS_EVERY_N - 1):
            run(mgr.update_open_topics(None, "s3", HISTORY))
        check("未到间隔不触发提取", lambda: calls["n"] == 0)
        run(mgr.update_open_topics(None, "s3", HISTORY))
        check("到间隔才跑一次提取", lambda: calls["n"] == 1)

        block = mgr.open_topics_block("s3")
        check("未了话题注入块带说明",
              lambda: "【还没结清的事】" in block and "还没定哪天出发" in block)

        async def fake_empty(ctx, messages, **kwargs):
            return {"content": json.dumps({"topics": []})}

        recall.chat_once = fake_empty
        for _ in range(recall.OPEN_TOPICS_EVERY_N):
            run(mgr.update_open_topics(None, "s3", HISTORY))
        check("结清后清单被清空", lambda: mgr.open_topics_block("s3") == "")

        async def fake_boom(ctx, messages, **kwargs):
            raise RuntimeError("模型挂了")

        recall.chat_once = fake_boom
        mgr._open["s3"] = [{"text": "旧清单", "updated": time.time()}]
        for _ in range(recall.OPEN_TOPICS_EVERY_N):
            run(mgr.update_open_topics(None, "s3", HISTORY))
        check("提取失败时沿用旧清单", lambda: "旧清单" in mgr.open_topics_block("s3"))
    finally:
        recall.chat_once = original

    check("drop_session 清掉清单", lambda: mgr.drop_session("s3") == 1
          and mgr.open_topics_block("s3") == "")
    check("磁盘上留了一份清单文件", lambda: (tmp / "open_topics.json").exists())


def t_time_spread(tmp: Path):
    print("\n--- 同一话题时段只取一段 / 按时间排列 ---")
    # 单段条数封顶会把一段连续对话切成多段，这些段的起始时间几乎相同；
    # 相似度检索会把它们整串捞上来（都在聊同一件事），按时间拉开只该取一段
    dense = [msg("user", f"海边那家店第{i}次去真不错", BASE + i * 5, sender_name="主人")
             for i in range(24)]
    groups = recall.split_segments(dense)
    mgr = recall.HistoryRecallManager(Cfg({"history_recall_enabled": True}), tmp,
                                      FakeEmbedder())
    run(mgr.refresh("s5", dense))
    index = mgr._load_index("s5")
    hits = run(mgr.recall("s5", "海边那家店"))

    check("条数封顶确实把连续对话切成了多段", lambda: len(groups) > 1)
    check("这些段都进了索引", lambda: len(index["segments"]) == len(groups) - 1)
    check("同一话题时段只取一段", lambda: len(hits) == 1)
    check("取的是其中最早的那段",
          lambda: hits and hits[0]["ts"] == index["segments"][0]["ts"])

    # 两段相隔一小时：都该被取到，且按时间从早到晚排列（更相关的那段在后面）
    spread = [
        msg("user", "周末想去海边玩", BASE, sender_name="主人"),
        msg("assistant", "好呀", BASE + 10, speaker="丛雨"),
        msg("user", "这周海边的工作好多", BASE + 3600, sender_name="主人"),
        msg("assistant", "辛苦了", BASE + 3610, speaker="丛雨"),
        msg("user", "现在聊点别的", BASE + 7200, sender_name="主人"),
    ]
    mgr2 = recall.HistoryRecallManager(Cfg({"history_recall_enabled": True}), tmp,
                                       FakeEmbedder())
    run(mgr2.refresh("s6", spread))
    ordered = run(mgr2.recall("s6", "海边 工作"))
    check("相隔较远的段都保留", lambda: len(ordered) == 2)
    check("更相关的那段不是排在最前（说明是按时间排的）",
          lambda: ordered[0]["sim"] < ordered[-1]["sim"])
    check("结果按时间从早到晚排列",
          lambda: [h["ts"] for h in ordered] == sorted(h["ts"] for h in ordered))


def t_load_guard(tmp: Path):
    print("\n--- 读失败守卫 ---")
    mgr = recall.HistoryRecallManager(Cfg({"history_recall_enabled": True}), tmp,
                                      FakeEmbedder())
    run(mgr.refresh("s4", HISTORY))
    index_path = mgr.dir / f"{mgr._key('s4')}.json"
    index_path.write_text("{ 坏掉的 JSON", encoding="utf-8")
    broken = recall.HistoryRecallManager(Cfg({"history_recall_enabled": True}), tmp,
                                         FakeEmbedder())
    run(broken.refresh("s4", HISTORY))
    check("索引损坏时先备份再重建",
          lambda: index_path.exists()
          and Path(str(index_path) + ".corrupt").exists())

    open_path = tmp / "open_topics.json"
    open_path.write_text("{ 也是坏的", encoding="utf-8")
    guarded = recall.HistoryRecallManager(Cfg({"history_recall_enabled": True}), tmp)
    guarded._open["s4"] = [{"text": "新清单", "updated": time.time()}]
    guarded._save_open()
    check("未了话题损坏时先备份再重建",
          lambda: open_path.exists()
          and Path(str(open_path) + ".corrupt").exists())


def main():
    print("=" * 70)
    print("会话回忆自测（桩，不连真实服务）")
    print("=" * 70)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        t_segments()
        t_lexical()
        t_index(tmp)
        t_recall(tmp)
        t_open_topics(tmp)
        t_time_spread(tmp)
        t_load_guard(tmp)
    print("\n" + "=" * 70)
    print(f"结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
    for item in FAIL:
        print(f"  FAIL: {item}")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
