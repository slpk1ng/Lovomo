# -*- coding: utf-8 -*-
"""聊天记录完整性回归测试。

覆盖：
  A 会话文件保留完整聊天记录：不再按条数截断，跨天消息全量落盘与读取
  B 聊天列表「按角色」排序：组间按各角色最新对话倒序，组内按时间倒序
  C 消息渲染遍历全部记录，不二次截断

运行: python tests/test_chat_history.py     （全通过退出码 0）
"""
import io
import json
import os
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

PASS, FAIL = [], []


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


def _html() -> str:
    return (ROOT / "webui" / "start.html").read_text(encoding="utf-8")


def _source() -> str:
    # 发送拆条逻辑已拆到 modules/message_pipeline.py 与 modules/reply_pipeline.py
    # （SentenceSink），拼接多个源文件以保持原有断言不变。
    return "\n".join((ROOT / name).read_text(encoding="utf-8")
                     for name in ("main.py",
                                  "modules/message_pipeline.py",
                                  "modules/reply_pipeline.py"))


def _sender_source() -> str:
    return (ROOT / "modules" / "sender.py").read_text(encoding="utf-8")


SENDER_SRC = _sender_source()


def _make_memory(tmp: Path):
    cfg = M.ConfigLoader(str(tmp / "config.json"))
    cfg.config["memory_data_path"] = str(tmp / "mdata")
    return M.MemoryManager(cfg)


def a_full_history(tmp: Path):
    section("A 会话文件保留完整聊天记录")
    mm = _make_memory(tmp)
    sid = "private_10001"
    day = 86400
    now = time.time()

    data = mm.load_session_data(sid)
    data["history"] = [{"role": "user", "content": f"m{i}", "timestamp": now - (200 - i) * 60}
                       for i in range(200)]
    mm.save_session_data(sid, data)
    check("保存 200 条后读回仍是 200 条（不再按条数截断）",
          lambda: len(mm.load_session_data(sid)["history"]) == 200)

    across = mm.load_session_data(sid)
    across["history"] = [{"role": "user", "content": f"d{i}",
                          "timestamp": now - (6 - i) * day} for i in range(7)]
    mm.save_session_data(sid, across)
    back = mm.load_session_data(sid)["history"]
    check("跨 7 天的记录全量保留，首尾时间戳不变",
          lambda: len(back) == 7 and back[0]["timestamp"] == now - 6 * day
          and back[-1]["timestamp"] == now)

    more = mm.load_session_data(sid)
    more["history"].append({"role": "assistant", "content": "新回复", "timestamp": now + 60})
    mm.save_session_data(sid, more)
    check("再次保存追加新消息不会丢掉旧记录",
          lambda: len(mm.load_session_data(sid)["history"]) == 8)

    # save_session_data 已搬至 modules/memory_store.py，源码检查跟着换源文件
    check("保存会话数据不再出现按条数截断的写法",
          lambda: "[-60:]" not in (ROOT / "modules" / "memory_store.py")
          .read_text(encoding="utf-8").split("def save_session_data")[1].split("def ")[0])

    filename = "murasame_private_10001.json"
    bulk = {"character_name": "丛雨",
            "history": [{"role": "assistant" if i % 2 else "user", "content": f"n{i}",
                         "timestamp": now - i} for i in range(120)]}
    from modules.jsonio import save_json
    save_json(mm.data_path / filename, bulk)
    got = mm.get_history(filename)
    check("get_history 返回文件里的全部 120 条记录",
          lambda: got["success"] and len(got["history"]) == 120)

    listed = [m for m in mm.list_memories() if m["filename"] == filename]
    check("list_memories 仍能列出该会话并取到最后一条角色台词",
          lambda: len(listed) == 1 and listed[0]["last_sentence"] == "n119")


def b_role_sort_order():
    section("B 聊天列表「按角色」排序")
    html = _html()
    start = html.index("function sortAndRender()")
    body = html[start:html.index("function renderFileList()")]

    check("组间按各角色的最新对话时间倒序",
          lambda: "newestOfRole" in body and "newestOfRole[rb] - newestOfRole[ra]" in body)
    check("组内仍按时间倒序（同一角色比较 modified_time）",
          lambda: "(b.modified_time||0) - (a.modified_time||0)" in body)
    check("不再只按角色名排序",
          lambda: "a.role_name !== b.role_name" not in body)


def c_render_all_messages():
    section("C 消息渲染遍历全部记录")
    html = _html()
    start = html.index("function renderChatMessages()")
    body = html[start:html.index("function updateMsgToolbar()")]

    check("渲染遍历整份历史，不做条数截断",
          lambda: "currentChatHistory.forEach" in body and "slice(-" not in body)
    check("保留按间隔插入时间分隔条的逻辑",
          lambda: "ts - lastTs >= 300" in body)


def d_split_records():
    section("D 分开发送时聊天记录也跟着拆条")
    src = _source()
    check("分开发送时按实际发出去的每条文本各记一条",
          lambda: "sent_texts = (sink.sent_texts if sink is not None and sink.sent" in src
          and 'send_result.get("sent_texts")' in src)
    check("合并发送（只有一条文本）仍记成一条",
          lambda: "if len(parts) <= 1:" in src and "parts = [zh_text]" in src)
    check("发送器把逐条发出去的文本记进结果",
          lambda: all(s in SENDER_SRC for s in
                      ("sent_texts: List[str] = []", 'result["sent_texts"] = sent_texts',
                       "sent_texts.append(")))
    check("流式路径也按逐句发出去的文本记",
          lambda: "self.sent_texts: List[str] = []" in src
          and 'self.sent_texts.append(str(sentence.get("zh") or shown))' in src)
    check("备注跟着最后一条走",
          lambda: "if tool_notes and index == len(parts) - 1" in src)


def e_bubble_colors():
    section("E 气泡配色：角色固定，用户按人区分")
    html = _html()
    check("角色气泡仍是原来的颜色",
          lambda: ".msg-row.bot .msg-bubble { background: #fff;" in html)
    check("不同用户有几档底色",
          lambda: all(f".msg-row.user.u{i} .msg-bubble" in html for i in (2, 3, 4, 5, 6)))
    check("按用户 ID 分配，同一份记录里颜色稳定",
          lambda: "function chatUserColorClass" in html
          and "msg.sender_id || msg.sender_name" in html
          and "if (!colors.has(key))" in html)
    start = html.index("function renderChatMessages()")
    body = html[start:html.index("function updateMsgToolbar()")]
    check("渲染时把用户的颜色类挂到行上",
          lambda: "chatUserColorClass(msg, userColors)" in body
          and "row.classList.add(cls)" in body)


def f_delete_clears_state(tmp: Path):
    """删除会话记录时，跟这段对话绑定的状态一起清掉（私聊连画像与关系进度）。"""
    import asyncio

    section("F 删除记录：状态一起清空")

    cfg = M.ConfigLoader(str(tmp / "del_cfg.json"))
    cfg.config.update({
        "memory_data_path": str(tmp / "del_data"),
        "active_character": "murasame",
        "roles": [{"character_key": "murasame", "character_name": "丛雨"}],
    })
    cfg.roles = cfg._parse_roles()
    mem = M.MemoryManager(cfg)
    mem.data_path.mkdir(parents=True, exist_ok=True)
    data_dir = tmp / "del_state"
    mood = M.MoodManager(data_dir)
    aff = M.AffectionManager(cfg, data_dir)
    prom = M.PromiseManager(data_dir)
    prof = M.UserProfileManager(cfg, data_dir)

    saved = (M.memory_manager, M.mood_mgr, M.affection_mgr, M.promise_mgr, M.profile_mgr)
    M.memory_manager, M.mood_mgr, M.affection_mgr, M.promise_mgr, M.profile_mgr = \
        mem, mood, aff, prom, prof
    # handle_delete 已迁入 modules/webui_server，状态槽位从 modules.app_context 读取，
    # 所以 M.<槽位> 的桩要镜像到 M.app_context.<槽位> 才能被看到
    M.app_context.memory_manager = M.memory_manager
    M.app_context.mood_mgr = M.mood_mgr
    M.app_context.affection_mgr = M.affection_mgr
    M.app_context.promise_mgr = M.promise_mgr
    M.app_context.profile_mgr = M.profile_mgr

    class FakeRequest:
        def __init__(self, payload):
            self.payload = payload

        async def json(self):
            return self.payload

    class FakeServer:
        memory_manager = mem

    FakeServer.handle_delete = M.WebUIServer.handle_delete
    FakeServer._deleted_session_scopes = M.WebUIServer._deleted_session_scopes
    FakeServer._session_user_id = M.WebUIServer._session_user_id
    server = FakeServer()

    def prepare():
        for sid, uid in (("private_10001", "10001"), ("group_20002", "10001")):
            mem.save_session_data(sid, {"history": [
                {"role": "user", "content": "在吗", "sender_id": uid,
                 "timestamp": time.time()}]})
            mood.set_mood(sid, "murasame", 80, user_id=uid)
            mood.add_diary("murasame", "2026-10-01", "写了一点", session_id=sid)
            prom.add("murasame", sid, uid, "我明天来")
        aff.apply_delta("murasame", "10001", 30, session_id="private_10001")
        aff.apply_delta("murasame", "10002", 20, session_id="group_20002")
        prof.update("10001", {"nickname": "小明"})
        prof.update("10002", {"nickname": "小红"})

    prepare()
    priv_file = mem.get_memory_file("private_10001").name
    group_file = mem.get_memory_file("group_20002").name
    before = {"aff": aff.get("murasame", "10001", "private_10001")["score"]}

    asyncio.run(server.handle_delete(FakeRequest({"files": [priv_file]})))
    check("私聊记录文件已删除", lambda: not mem.get_memory_file("private_10001").exists())
    check("私聊的心情值已清空",
          lambda: mood.get_mood("private_10001", "murasame", 60) == 60)
    check("私聊的心情日记已清空",
          lambda: not mood.has_diary("murasame", "2026-10-01", "private_10001"))
    check("私聊的承诺已清空",
          lambda: not any(p.get("session_id") == "private_10001" for p in prom.items))
    check("私聊的关系进度回到「陌生」",
          lambda: before["aff"] > 0
          and aff.get("murasame", "10001", "private_10001")["score"] == 0
          and aff.stage("murasame", "10001", "private_10001") == "陌生")
    check("私聊的用户画像已删除", lambda: not prof.get("10001"))
    check("另一个人的关系与画像不受影响",
          lambda: aff.get("murasame", "10002", "group_20002")["score"] > 0
          and mem.get_memory_file("group_20002").exists())

    asyncio.run(server.handle_delete(FakeRequest({"files": [group_file]})))
    check("群聊记录删除后群会话的心情与日记也清掉",
          lambda: mood.get_mood("group_20002", "murasame", 60) == 60
          and not mood.has_diary("murasame", "2026-10-01", "group_20002"))
    check("群聊删除不牵连成员的关系与画像",
          lambda: aff.get("murasame", "10002", "group_20002")["score"] > 0
          and bool(prof.get("10002")))

    M.memory_manager, M.mood_mgr, M.affection_mgr, M.promise_mgr, M.profile_mgr = saved
    M.app_context.memory_manager = M.memory_manager
    M.app_context.mood_mgr = M.mood_mgr
    M.app_context.affection_mgr = M.affection_mgr
    M.app_context.promise_mgr = M.promise_mgr
    M.app_context.profile_mgr = M.profile_mgr


def main():
    print("聊天记录完整性回归测试开始")
    with tempfile.TemporaryDirectory(prefix="lovomo_chat_", ignore_cleanup_errors=True) as td:
        tmp = Path(td)
        try:
            a_full_history(tmp)
        except Exception as e:
            FAIL.append(("A-init", repr(e)))
            print(f"  [FAIL] A 段初始化失败: {e!r}")
        for fn in (b_role_sort_order, c_render_all_messages, d_split_records,
                   e_bubble_colors):
            try:
                fn()
            except Exception as e:
                FAIL.append((fn.__name__, repr(e)))
                print(f"  [FAIL] {fn.__name__}: {e!r}")
        try:
            f_delete_clears_state(tmp)
        except Exception as e:
            FAIL.append(("F-init", repr(e)))
            print(f"  [FAIL] F 段初始化失败: {e!r}")

    print(f"\n{'=' * 70}")
    print(f"结果：通过 {len(PASS)} | 失败 {len(FAIL)}")
    for name, err in FAIL:
        print(f"  - {name}: {err}")
    print(f"{'=' * 70}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
