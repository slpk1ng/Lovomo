"""会话ID两种写法的归一：净化过的（…_im_wechat）必须还原成运行时写法（…@im.wechat）。

混用会把同一个会话当成两个 —— 主动消息会多发一条，而且反推出来的那个认不出平台、
解析发送目标时还会把 openid 截断，最后落到默认的 NapCat 上报「无法获取用户信息」。
"""
import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main as M  # noqa: E402
from modules.sender import MessageSender  # noqa: E402

PASS = []
FAIL = []


def check(name, fn):
    try:
        ok = fn()
    except Exception as e:
        FAIL.append(f"{name}: {type(e).__name__}: {e}")
        return
    (PASS if ok else FAIL).append(name if ok else f"{name}: 断言为假")


def sanitize(session_id: str) -> str:
    """复刻记忆文件名的净化规则（非 [A-Za-z0-9_-] 一律变成 _，@ 和 . 都会没）。"""
    return re.sub(r"[^A-Za-z0-9_\-]", "_", session_id)


class Cfg(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class FakeWeChat:
    platform = "wechat_clawbot"


class FakeNapCat:
    pass


def t_restore():
    print("\n--- 会话ID还原 ---")
    check("净化过的微信ID还原成 @ 写法",
          lambda: M.restore_session_id(sanitize("private_o9cq80@im.wechat"))
          == "private_o9cq80@im.wechat")
    check("群聊 _chatroom 也还原",
          lambda: M.restore_session_id(sanitize("group_abc@chatroom"))
          == "group_abc@chatroom")
    check("本来正常的ID原样返回",
          lambda: M.restore_session_id("private_1905332561") == "private_1905332561"
          and M.restore_session_id("group_248315321") == "group_248315321")
    check("空值不炸", lambda: M.restore_session_id(None) == "")


def t_memory_id():
    print("\n--- 从记忆文件名反推 ---")
    check("反推出来的是运行时写法",
          lambda: M._memory_session_id(
              "murasame_private_o9cq803cY8qdjYVpVczwZ0gfBNwU_im_wechat.json")
          == "private_o9cq803cY8qdjYVpVczwZ0gfBNwU@im.wechat")
    check("普通QQ会话不受影响",
          lambda: M._memory_session_id("murasame_private_1905332561.json")
          == "private_1905332561")
    check("群聊不受影响",
          lambda: M._memory_session_id("murasame_group_248315321.json")
          == "group_248315321")


def t_target():
    print("\n--- 发送目标解析 ---")
    full = "private_o9cq803cY8qdjYVpVczwZ0gfBNwU@im.wechat"
    safe = sanitize(full)
    check("微信私聊：两种写法都解析出完整 openid",
          lambda: M.parse_session_target(full) == ("private", full[len("private_"):])
          and M.parse_session_target(safe) == ("private", full[len("private_"):]))
    check("微信私聊不会被 _ 截断",
          lambda: M.parse_session_target(safe)[1].endswith("@im.wechat"))
    check("QQ 私聊照旧", lambda: M.parse_session_target("private_1905332561")
          == ("private", "1905332561"))
    check("群聊照旧", lambda: M.parse_session_target("group_248315321")
          == ("group", "248315321"))
    check("隔离群聊只取群号", lambda: M.parse_session_target("group_248315321_10001")
          == ("group", "248315321"))


def t_merge():
    print("\n--- 历史状态合并 ---")
    full = "private_o9cq80@im.wechat"
    merged = M.merge_restored_keys({full: 100.0, sanitize(full): 200.0,
                                    "private_1": 5.0})
    check("两种写法并成一条且取较大值",
          lambda: merged == {full: 200.0, "private_1": 5.0})
    check("日期前缀的计数键也能并",
          lambda: M.merge_restored_keys({"2026-10-04|" + full: 1,
                                         "2026-10-04|" + sanitize(full): 2})
          == {"2026-10-04|" + full: 2})


def t_proactive_state(tmp: Path):
    print("\n--- 主动消息状态载入时归一 ---")
    full = "private_o9cq803cY8qdjYVpVczwZ0gfBNwU@im.wechat"
    safe = sanitize(full)

    class FakeMem:
        data_path = tmp

    old_mem = M.memory_manager
    M.memory_manager = FakeMem()
    M.app_context.memory_manager = M.memory_manager
    try:
        (tmp / "proactive_state.json").write_text(json.dumps({
            "date": M.time.strftime("%Y-%m-%d"),
            "counts": {f"{M.time.strftime('%Y-%m-%d')}|{safe}": 2},
            "user_activity": {full: 111.0, safe: 222.0},
            "awaiting": [safe],
            "pending": {},
        }, ensure_ascii=False), encoding="utf-8")
        M.proactive_counts.clear()
        M.last_user_activity.clear()
        M.proactive_awaiting.clear()
        M.load_proactive_state()
        check("user_activity 两个键并成一条",
              lambda: M.last_user_activity == {full: 222.0})
        check("awaiting 归一",
              lambda: M.proactive_awaiting == {full})
        check("counts 归一",
              lambda: M.proactive_counts == {f"{M.time.strftime('%Y-%m-%d')}|{full}": 2})
    finally:
        M.memory_manager = old_mem
        M.app_context.memory_manager = M.memory_manager
        M.proactive_counts.clear()
        M.last_user_activity.clear()
        M.proactive_awaiting.clear()


def t_for_session():
    print("\n--- 按会话挑连接 ---")
    full = "private_o9cq803cY8qdjYVpVczwZ0gfBNwU@im.wechat"
    safe = sanitize(full)
    napcat = FakeNapCat()
    wechat = FakeWeChat()
    snd = MessageSender(Cfg({}), None)
    snd.client = napcat
    snd.set_channel_client("wechat_clawbot_1", wechat)
    with snd.for_session(safe):
        check("净化过的微信会话按平台挑到微信连接",
              lambda: snd._active_client() is wechat)
    with snd.for_session(full):
        check("运行时写法的微信会话同样挑到微信连接",
              lambda: snd._active_client() is wechat)
    with snd.for_session("private_1905332561"):
        check("QQ 会话仍回落默认连接",
              lambda: snd._active_client() is napcat)
    snd.remember_session(full, wechat)
    snd.set_channel_client("wechat_clawbot_1", None)
    with snd.for_session(full):
        check("记过的会话即使连接注销也用它记住的那条",
              lambda: snd._active_client() is wechat)


def main():
    print("=" * 70)
    print("会话ID归一自测（桩，不连真实服务）")
    print("=" * 70)
    t_restore()
    t_memory_id()
    t_target()
    t_merge()
    with tempfile.TemporaryDirectory() as d:
        t_proactive_state(Path(d))
    t_for_session()
    print("\n" + "=" * 70)
    print(f"结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
    for item in FAIL:
        print(f"  FAIL: {item}")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
