"""接入方式适配器：消息转换与收发载荷（用桩，不连真实服务）。"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import napcat  # noqa: E402
from modules.adapters import (  # noqa: E402
    ILinkClient, ILinkStaleContext, QQBotClient, build_client, segment_to_dict,
    segments_to_text)

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


# ---------------------------------------------------------------------------
# 消息段
# ---------------------------------------------------------------------------
def t_segments():
    check("文本段转成 OneBot 字典",
          lambda: segment_to_dict(napcat.Text(text="你好"))
          == {"type": "text", "data": {"text": "你好"}})
    check("图片段带上文件字段",
          lambda: segment_to_dict(napcat.Image(file="a.png"))["type"] == "image"
          and segment_to_dict(napcat.Image(file="a.png"))["data"]["file"] == "a.png")
    check("发送时只取文本、按顺序拼接",
          lambda: segments_to_text([napcat.Text(text="你"), napcat.At(qq="1"),
                                    napcat.Text(text="好")]) == "你好")
    check("非文本段不会变成乱码",
          lambda: segments_to_text([napcat.Image(file="a.png")]) == "")


# ---------------------------------------------------------------------------
# 微信 ClawBot（iLink）
# ---------------------------------------------------------------------------
def t_ilink():
    client = ILinkClient({"id": "wx1", "bot_id": "bot@im.bot", "bot_token": "tk"})

    inbound = {
        "from_user_id": "o9cq800kum@im.wechat",
        "to_user_id": "bot@im.bot",
        "message_type": 1,
        "context_token": "CTX-1",
        "msg_id": "m-1",
        "item_list": [{"type": 1, "text_item": {"text": "你好"}},
                      {"type": 1, "text_item": {"text": "呀"}}],
    }
    event = client.to_event(inbound)
    check("iLink 消息转成 NapCat 私聊事件（段是真 Text）",
          lambda: isinstance(event, napcat.PrivateMessageEvent)
          and event.user_id == "o9cq800kum@im.wechat"
          and isinstance(event.message[0], napcat.Text)
          and event.message[0].text == "你好呀"
          and event.message_id == "m-1" and event.group_id is None
          and event.message_type == "private")
    check("context_token 被记下来（回复要原样带回）",
          lambda: client._contexts.get("o9cq800kum@im.wechat") == "CTX-1")
    check("没有发送者的消息会被丢掉", lambda: client.to_event({"item_list": []}) is None)

    sent = {"typing": [], "calls": []}

    async def fake_post(path, payload, timeout=45, log_result=False):
        sent["calls"].append(path)
        # 官方客户端的收发顺序：getconfig 取票据 → sendtyping(1) → sendmessage → sendtyping(2)
        if path.endswith("getconfig"):
            return {"ret": 0, "typing_ticket": "TK-1"}
        if path.endswith("sendtyping"):
            sent["typing"].append(int(payload.get("status") or 0))
            return {"ret": 0}
        sent["path"] = path
        sent["payload"] = payload
        # 投递成功时服务端会回 message_id（失败时只有 ret）
        return {"ret": 0, "message_id": "M-1"}

    client._post = fake_post
    run(client.send_private_msg("o9cq800kum@im.wechat",
                               [napcat.Text(text="回复你")]))
    check("回复走 sendmessage 且带上 context_token",
          lambda: sent["path"] == "ilink/bot/sendmessage"
          and sent["payload"]["msg"]["context_token"] == "CTX-1"
          and sent["payload"]["msg"]["to_user_id"] == "o9cq800kum@im.wechat"
          and sent["payload"]["msg"]["item_list"][0]["text_item"]["text"] == "回复你")
    check("发送载荷带上 base_info / from_user_id / client_id",
          lambda: sent["payload"]["base_info"]["channel_version"]
          and sent["payload"]["msg"]["from_user_id"] == ""
          and len(sent["payload"]["msg"]["client_id"]) == 32)
    check("发送消息本身不带上「正在输入」握手（不占发送路径）",
          lambda: sent["typing"] == []
          and "ilink/bot/sendtyping" not in sent["calls"])

    # 「正在输入」按整轮回复开关一次，且不阻塞发送
    async def typing_flow():
        client.begin_typing("o9cq800kum@im.wechat")
        await asyncio.sleep(0.05)
        client.end_typing("o9cq800kum@im.wechat")
        await asyncio.sleep(0.05)

    run(typing_flow())
    check("整轮回复只开关一次「正在输入」",
          lambda: sent["typing"] == [1, 2]
          and "ilink/bot/getconfig" in sent["calls"]
          and sent["calls"][-1] == "ilink/bot/sendtyping")

    # 服务端会间歇性地不给 context_token：这时不带 token 直接发，
    # 不能静默返回——静默返回会被上层当成发送成功，这条回复就凭空消失了
    sent.pop("path", None)
    sent.pop("payload", None)
    run(client.send_private_msg("never-said-hi", [napcat.Text(text="喂")]))
    check("没有 context_token 时不带 token 发送",
          lambda: sent["path"] == "ilink/bot/sendmessage"
          and "context_token" not in sent["payload"]["msg"]
          and sent["payload"]["msg"]["to_user_id"] == "never-said-hi")

    # 全是图片这类没有文本的消息也不发空包
    sent.pop("path", None)
    sent.pop("payload", None)
    run(client.send_private_msg("o9cq800kum@im.wechat", [napcat.Image(file="a.png")]))
    check("没有文本时不发空消息", lambda: "path" not in sent)

    # 服务端投递成功会回 message_id：平时每句一行回执；
    # 只有"收下但没这个 id"（=没投递到微信）才刷出载荷与响应字段（并脱敏）
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run(client.send_private_msg("o9cq800kum@im.wechat", [napcat.Text(text="一")]))
        run(client.send_private_msg("o9cq800kum@im.wechat", [napcat.Text(text="二")]))
    out = buf.getvalue()
    check("投递成功时每句只打一行回执，带上服务端返回的 message_id",
          lambda: out.count("已发送（message_id=") == 2
          and "发送载荷字段" not in out
          and "重新扫码登录" not in out)

    async def no_id_post(path, payload, timeout=45, log_result=False):
        if path.endswith("getconfig") or path.endswith("sendtyping"):
            return {"ret": 0}
        return {"ret": 0}

    client._post = no_id_post
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run(client.send_private_msg("o9cq800kum@im.wechat", [napcat.Text(text="三")]))
    out = buf.getvalue()
    check("没有 message_id 时提示没投递，并打出两端字段且不泄露完整 ID",
          lambda: "没有 message_id" in out.replace("没返回 message_id", "没有 message_id")
          and "发送载荷字段" in out and "发送响应字段" in out
          and "o9cq800kum@im.wechat" not in out
          and "from_user_id=(空)" in out)
    client._post = fake_post

    check("snapshot 带上游标与 context_token",
          lambda: client.snapshot()["context_tokens"].get("o9cq800kum@im.wechat") == "CTX-1")

    # 投递自检要按"最近说过话的人"选目标：字典末尾始终是最后发来消息的那个
    from modules.adapters import recent_ilink_target
    order = ILinkClient({"id": "wx2", "bot_token": "tk"})
    order.to_event({"from_user_id": "A@im.wechat", "context_token": "TA",
                    "item_list": [{"type": 1, "text_item": {"text": "一"}}]})
    order.to_event({"from_user_id": "B@im.wechat", "context_token": "TB",
                    "item_list": [{"type": 1, "text_item": {"text": "二"}}]})
    order.to_event({"from_user_id": "A@im.wechat", "context_token": "TA2",
                    "item_list": [{"type": 1, "text_item": {"text": "三"}}]})
    check("最近说过话的人就是字典末尾那个",
          lambda: recent_ilink_target(order.snapshot()) == "A@im.wechat"
          and recent_ilink_target({"context_tokens": {}}) == "")

    # 服务端不认 context_token（ret=-2）：去掉 token 再发一次，并丢掉缓存
    tokens = []

    async def stale_post(path, payload, timeout=45, log_result=False):
        if path.endswith("getconfig") or path.endswith("sendtyping"):
            return {"ret": 0}
        tokens.append(str(payload["msg"].get("context_token", "")))
        if payload["msg"].get("context_token"):
            raise ILinkStaleContext("ret=-2")
        return {}

    client._post = stale_post
    run(client.send_private_msg("o9cq800kum@im.wechat", [napcat.Text(text="再来")]))
    check("context_token 被拒后去掉 token 重发",
          lambda: tokens == ["CTX-1", ""]
          and client._contexts.get("o9cq800kum@im.wechat") is None)

    check("微信群聊暂不支持发送",
          lambda: run(_expect_not_implemented(client)))


async def _expect_not_implemented(client):
    try:
        await client.send_group_msg("g1", [napcat.Text(text="x")])
        return False
    except NotImplementedError:
        return True


# ---------------------------------------------------------------------------
# QQ 官方机器人
# ---------------------------------------------------------------------------
def t_qqbot():
    client = QQBotClient({"id": "qq1", "app_id": "102000000", "app_secret": "s"})

    c2c = client.to_event("C2C_MESSAGE_CREATE",
                          {"id": "m1", "content": "在吗", "user_openid": "OPENID-A"})
    check("QQ 单聊消息转成 NapCat 私聊事件",
          lambda: isinstance(c2c, napcat.PrivateMessageEvent)
          and c2c.user_id == "OPENID-A" and c2c.group_id is None
          and c2c.message[0].text == "在吗" and c2c.message_id == "m1")

    group = client.to_event("GROUP_AT_MESSAGE_CREATE",
                            {"id": "m2", "content": "早上好", "group_openid": "G-1",
                             "author": {"member_openid": "OPENID-B"}})
    check("QQ 群 @ 消息转成群聊事件且带上 @ 段",
          lambda: isinstance(group, napcat.GroupMessageEvent)
          and group.group_id == "G-1" and group.user_id == "OPENID-B"
          and group.sender.user_id == "OPENID-B" and group.message_type == "group"
          and isinstance(group.message[0], napcat.At)
          and group.message[0].qq == "102000000")

    check("不关心的事件类型被忽略",
          lambda: client.to_event("GUILD_CREATE", {}) is None
          and client.to_event("C2C_MESSAGE_CREATE", {}) is None)

    calls = {}

    async def fake_ensure():
        return None

    async def fake_post_message(path, text, message_id=""):
        calls["path"] = path
        calls["text"] = text

    client._ensure_token = fake_ensure
    client._post_message = fake_post_message
    run(client.send_private_msg("OPENID-A", [napcat.Text(text="在的")]))
    check("QQ 单聊回复走 /v2/users/{openid}/messages",
          lambda: calls["path"] == "/v2/users/OPENID-A/messages"
          and calls["text"] == "在的")
    run(client.send_group_msg("G-1", [napcat.Text(text="早")]))
    check("QQ 群回复走 /v2/groups/{group_openid}/messages",
          lambda: calls["path"] == "/v2/groups/G-1/messages" and calls["text"] == "早")

    check("沙箱开关决定接口域名",
          lambda: "sandbox" in QQBotClient({"sandbox": True}).api_base
          and "sandbox" not in QQBotClient({"sandbox": False}).api_base)

    check("Access Token 请求体带上 AppID 与 AppSecret",
          lambda: run(_token_payload()) == {"appId": "102000000", "clientSecret": "s"})


async def _token_payload():
    client = QQBotClient({"app_id": "102000000", "app_secret": "s"})
    captured = {}

    class FakeResp:
        status = 200

        async def text(self):
            return json.dumps({"access_token": "AT", "expires_in": 7200})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class FakeSession:
        def post(self, url, json=None, timeout=None):
            captured.update(json or {})
            return FakeResp()

        async def close(self):
            return None

    client._session = FakeSession()
    await client._ensure_token()
    return captured


# ---------------------------------------------------------------------------
def t_labels_and_caps():
    from modules.adapters import friendly_user_label

    long_id = "o9cq803cY8qdjYVpVczwZ0gfBNwU@im.wechat"
    check("微信 openid 会变成可读的用户名",
          lambda: friendly_user_label("wechat_clawbot", long_id).startswith("微信用户 ")
          and "@im.wechat" not in friendly_user_label("wechat_clawbot", long_id))
    check("有昵称就用昵称", lambda: friendly_user_label("", long_id, "小明") == "小明")
    check("QQ 号原样返回",
          lambda: friendly_user_label("", "1905332561") == "1905332561")

    from main import client_supports
    check("微信/QQ 接入方式声明为只能发文字",
          lambda: not client_supports(ILinkClient({}), "voice")
          and not client_supports(QQBotClient({}), "voice")
          and not client_supports(QQBotClient({}), "sticker"))
    check("没有声明的客户端默认支持（NapCat 那种）",
          lambda: client_supports(object(), "voice"))


def t_poll_tolerance():
    """长轮询被断开要能自己接着轮，而不是当成连接失败（否则每分钟刷一条错误）。"""
    import aiohttp

    client = ILinkClient({"id": "wx1", "bot_token": "tk"})
    calls = {"n": 0}

    async def fake_post(path, payload, timeout=90):
        calls["n"] += 1
        if calls["n"] == 1:
            raise aiohttp.ClientError("boom")
        if calls["n"] >= 3:
            # 第三次卡住：让外层的超时来收尾（否则空转不让事件循环走）
            await asyncio.sleep(3600)
        return {"msgs": [], "get_updates_buf": "c1"}

    client._post = fake_post

    async def drive():
        agen = client.__aiter__()
        try:
            await asyncio.wait_for(agen.__anext__(), timeout=3)
        except asyncio.TimeoutError:
            pass
        return calls["n"]

    check("轮询断开后会自动接着轮（不抛出去）", lambda: run(drive()) >= 2)


def t_session_routing():
    """主动消息/提醒要按会话选连接：微信会话不能走默认的 NapCat。"""
    from modules.adapters import ILinkClient
    from modules.sender import MessageSender

    class Cfg(dict):
        def get(self, k, d=None):
            return super().get(k, d)

    snd = MessageSender(Cfg({}), None)
    default, wx = object(), ILinkClient({})
    snd.client = default
    snd.remember_session("private_o9cq@im.wechat", wx)

    with snd.for_session("private_o9cq@im.wechat"):
        inside = snd._active_client()
    outside = snd._active_client()
    with snd.for_session("private_1905332561"):
        unknown = snd._active_client()

    check("按会话切到对应连接、出块回默认",
          lambda: inside is wx and outside is default)
    check("没见过的会话回落到默认连接", lambda: unknown is default)

    # 映射表里没有的微信会话（老会话、换过接入方式）也要走微信连接，
    # 否则提醒/日记会落到默认的 NapCat 上，报「无法获取用户信息」
    snd2 = MessageSender(Cfg({}), None)
    snd2.client = default
    wx2 = ILinkClient({"id": "wechat_clawbot_2"})
    snd2.set_channel_client("wechat_clawbot_2", wx2)
    with snd2.for_session("private_someone@im.wechat"):
        picked_wx = snd2._active_client()
    with snd2.for_session("private_1905332561"):
        picked_qq = snd2._active_client()
    with snd2.for_session("group_12345@chatroom"):
        picked_room = snd2._active_client()
    check("没有映射记录的微信会话按 ID 走微信连接",
          lambda: picked_wx is wx2 and picked_room is wx2)
    check("QQ 号会话没有微信连接可分派，仍用默认连接",
          lambda: picked_qq is default)
    check("按 ID 判断平台只认微信目标格式",
          lambda: MessageSender.platform_of_session("private_a@im.wechat") == "wechat_clawbot"
          and MessageSender.platform_of_session("group_1@chatroom") == "wechat_clawbot"
          and MessageSender.platform_of_session("private_1905332561") == ""
          and MessageSender.platform_of_session("group_248315321") == "")

    sender_src = (ROOT / "modules" / "sender.py").read_text(encoding="utf-8")
    check("发送层统一按能力关掉语音（覆盖待办/主动/日记所有路径）",
          lambda: 'if use_voice and not client_supports(active, "voice")' in sender_src
          and 'if use_voice and not client_supports(self._active_client(), "voice")'
          in sender_src)


def t_session_timeout():
    """登录态失效（errcode -14）要抛专门的异常，让主循环停下来提示重新扫码。"""
    from modules.adapters import ILinkSessionExpired

    client = ILinkClient({"id": "wx1", "bot_token": "tk"})

    class FakeResp:
        status = 200

        async def text(self):
            return json.dumps({"errcode": -14, "errmsg": "session timeout"})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class FakeSession:
        def post(self, url, json=None, headers=None, timeout=None):
            return FakeResp()

    client._session = FakeSession()

    async def call():
        try:
            await client._post("ilink/bot/getupdates", {})
            return False
        except ILinkSessionExpired:
            return True

    check("登录态失效抛 ILinkSessionExpired（不再无限重试）",
          lambda: run(call()))


def t_pipeline_contract():
    """适配器造的事件必须能过消息管线的入口。

    管线是按 isinstance 分派的：不是 NapCat 的事件类会被**静默丢弃**
    （日志里一行都没有），消息段也必须是真正的 Text 对象。
    """
    import main as M
    from modules.adapters import make_event

    class FakeClient:
        self_id = "bot@im.bot"

    class Cfg(dict):
        def get(self, k, d=None):
            return super().get(k, d)

    M.global_config = Cfg({"isolated_session": False, "only_private": False,
                           "multi_role_enabled": False})
    M.app_context.global_config = M.global_config

    wx = make_event(private=True, user_id="o9cq@im.wechat", text="你好",
                    self_id="bot@im.bot")
    info = M._extract_event_info(wx, FakeClient())
    check("微信事件能进管线（事件类/消息段都得是真的 NapCat 类型）",
          lambda: info is not None and info["text"] == "你好"
          and info["session_id"] == "private_o9cq@im.wechat")

    qq = make_event(private=False, user_id="OPENID-A", text="早上好",
                    group_id="G-1", self_id="102000000")
    ginfo = M._extract_event_info(qq, FakeClient())
    check("QQ 群事件能进管线且会话按群分",
          lambda: ginfo is not None and ginfo["text"] == "早上好"
          and ginfo["session_id"] == "group_G-1")


def _FakePipelineClient():
    """管线只读 self_id，别的不用。"""
    class Client:
        self_id = "97f1258689b4@im.bot"
    return Client()


def t_wechat_inbound_image():
    """微信发来的图片要能进识图：下载 → 解密 → 落盘 → Image 段。

    以前只认 item type 1（文本），图片消息既没有文本也没有图片段，
    管线直接判成"空消息"丢掉，日志里连一行"收到消息"都没有。
    """
    import os as _os
    from pathlib import Path as _Path

    import main as M

    from modules.adapters import ILinkClient, _media_key, _pkcs7_unpad

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        check("微信图片：缺 cryptography 时跳过加解密用例", lambda: True)
        return

    def encrypt_ecb(data: bytes, key: bytes) -> bytes:
        pad = 16 - (len(data) % 16)
        padded = data + bytes([pad]) * pad
        enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        return enc.update(padded) + enc.finalize()

    def decrypt_ecb(data: bytes, key: bytes) -> bytes:
        dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        return dec.update(data) + dec.finalize()

    png = b"\x89PNG\r\n\x1a\n" + _os.urandom(24)
    key = _os.urandom(16)
    cipher_text = encrypt_ecb(png, key)

    client = ILinkClient({"id": "wx1", "bot_token": "tk", "cursor": "c"})

    async def fake_download(param):
        return cipher_text

    client._download_media = fake_download

    msg = {"from_user_id": "o9cq@im.wechat", "context_token": "CTX", "msg_id": "m-1",
           "item_list": [{"type": 1, "text_item": {"text": "看看这张"}},
                         {"type": 2, "image_item": {
                             "media": {"encrypt_query_param": "P1"},
                             "aeskey": key.hex()}}]}
    event = client.to_event(msg, run(client._inbound_media(msg)))
    images = [s for s in event.message if isinstance(s, napcat.Image)]
    check("微信图片消息转成 Image 段（文本段照常保留）",
          lambda: len(images) == 1
          and any(isinstance(s, napcat.Text) and s.text == "看看这张"
                  for s in event.message))
    check("图片下载后解密并落成本地文件（内容与原文一致）",
          lambda: bool(images)
          and _Path(images[0].file).exists()
          and _Path(images[0].file).read_bytes() == png
          and images[0].file.endswith(".png"))

    info = M._extract_event_info(event, _FakePipelineClient())
    check("管线认得出这是带图消息（识图会触发）",
          lambda: info is not None and info["has_image"] is True
          and info["text"] == "看看这张"
          and bool(info["image_urls"]))

    # 图片解密失败不能把整条消息吞掉：文本照常进管线
    async def broken_download(param):
        raise RuntimeError("boom")

    client._download_media = broken_download
    event2 = client.to_event(msg, run(client._inbound_media(msg)))
    check("图片拿不到时仍保留文本（不会整条丢掉）",
          lambda: [type(s).__name__ for s in event2.message] == ["Text"])

    check("图片密钥认十六进制与 base64 两种写法",
          lambda: len(_media_key(key.hex(), "")) == 16
          and len(_media_key("", __import__("base64").b64encode(
              key.hex().encode()).decode())) == 16
          and _media_key("not-hex", "") == b"")
    check("PKCS7 去填充不会把内容截坏",
          lambda: _pkcs7_unpad(decrypt_ecb(encrypt_ecb(b"abcd", key), key)) == b"abcd"
          and _pkcs7_unpad(b"") == b"")


def t_login_contract():
    """登录接口的字段名照官方客户端（AstrBot weixin_oc 适配器）对齐，别再改错。"""
    src = (ROOT / "modules" / "adapters.py").read_text(encoding="utf-8")
    # 登录接口的 WebUI 端已拆到 modules/webui_server.py，静态扫描需同时覆盖
    main_src = ((ROOT / "main.py").read_text(encoding="utf-8")
                + (ROOT / "modules" / "webui_server.py").read_text(encoding="utf-8"))
    check("取二维码必须带 bot_type（漏了会报 missing bot_type）",
          lambda: 'params={"bot_type"' in src and 'ILINK_BOT_TYPE = "3"' in src)
    check("二维码内容取 qrcode_img_content，票据另算",
          lambda: "qrcode_img_content" in src and "qr_content" in main_src)
    check("轮询状态读 status / bot_token / ilink_bot_id",
          lambda: '"status", "state"' in main_src and '"ilink_bot_id"' in main_src)
    check("请求头按官方客户端：UIN 是 base64(十进制串)、版本号只在轮询状态时带",
          lambda: 'str(random.getrandbits(32)).encode' in src
          and 'iLink-App-ClientVersion' in src
          and 'ilink_headers(client_version=' in src
          and src.count("iLink-App-ClientVersion") == 1)
    check("按 ret / errcode 判错（HTTP 200 也可能是失败）",
          lambda: 'int(data.get("ret") or 0)' in src and "err_msg" in src)


def t_text_only_pacing():
    """只能发文字的通道要拉开两条消息的间隔。

    一条回复按句发好几条（几百毫秒一条）时，微信这类通道会把整批当成刷屏；
    正常节奏是 1.5 秒上下一条，QQ 这种能发语音的通道不受影响。
    """
    import time as _time

    from modules import sender as sender_mod

    class TextOnly:
        capabilities = {"voice": False, "sticker": False, "image": False}

        def __init__(self):
            self.sent = []

        async def send_private_msg(self, user_id, message):
            self.sent.append((user_id, message))
            return {}

    class VoiceCapable:
        def __init__(self):
            self.sent = []

        async def send_private_msg(self, user_id, message):
            self.sent.append((user_id, message))
            return {}

    class Cfg(dict):
        def get(self, k, default=None):
            return dict.get(self, k, default)

    old_gap, old_jitter = sender_mod.TEXT_ONLY_SEND_MIN_GAP, sender_mod.TEXT_ONLY_SEND_GAP_JITTER
    sender_mod.TEXT_ONLY_SEND_MIN_GAP, sender_mod.TEXT_ONLY_SEND_GAP_JITTER = 0.05, 0.0
    try:
        text_only = sender_mod.MessageSender(Cfg({}), None)
        text_only.client = TextOnly()
        marks = []
        for line in ("一", "二", "三"):
            marks.append(_time.monotonic())
            run(text_only.send_text("private", "u1", line))

        voicey = sender_mod.MessageSender(Cfg({}), None)
        voicey.client = VoiceCapable()
        voice_marks = []
        for line in ("一", "二", "三"):
            voice_marks.append(_time.monotonic())
            run(voicey.send_text("private", "u1", line))
    finally:
        sender_mod.TEXT_ONLY_SEND_MIN_GAP, sender_mod.TEXT_ONLY_SEND_GAP_JITTER = old_gap, old_jitter

    check("只能发文字：第一条立刻发，第二条起先等够间隔",
          lambda: len(text_only.client.sent) == 3
          and marks[1] - marks[0] < 0.04
          and marks[2] - marks[1] >= 0.04)
    check("能发语音的通道不节流",
          lambda: len(voicey.client.sent) == 3
          and voice_marks[1] - voice_marks[0] < 0.04
          and voice_marks[2] - voice_marks[1] < 0.04)


def t_factory():
    check("按平台建对应客户端",
          lambda: isinstance(build_client("wechat_clawbot", {}), ILinkClient)
          and isinstance(build_client("qq_official", {}), QQBotClient)
          and build_client("napcat", {}) is None)


if __name__ == "__main__":
    t_segments()
    t_ilink()
    t_qqbot()
    t_labels_and_caps()
    t_session_timeout()
    t_poll_tolerance()
    t_session_routing()
    t_pipeline_contract()
    t_login_contract()
    t_wechat_inbound_image()
    t_text_only_pacing()
    t_factory()
    print("=" * 70)
    for name in PASS:
        print("  [PASS]", name)
    for name in FAIL:
        print("  [FAIL]", name)
    print("=" * 70)
    print(f"结果: {len(PASS)} PASS / {len(FAIL)} FAIL")
    print("=" * 70)
    sys.exit(1 if FAIL else 0)
