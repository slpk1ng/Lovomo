# -*- coding: utf-8 -*-
"""喵喵皮肤・回复追加 —— 「皮肤 + 功能」混合插件示例。

两块能力：
1. 深度换肤：由 theme.css 完成，并通过主程序注入的 --skin-* 变量
   读取用户在「换肤」页设置的背景图、铺满方式、不透明度与主色。
2. LLM 回复追加：开启开关后，每轮 LLM 回复发送完成时，再单独发一条
   「喵～」和一条喵叫语音。

状态持久化在插件自己的数据目录（settings.json），
停用/卸载时 on_unload 会把开关关掉，行为立即回到主程序原样。

可用钩子：
    on_load(ctx)               加载时调用
    on_unload(ctx)             停用/卸载时调用
    on_reply_done(ctx, info)   一轮 LLM 回复发送完成后调用

on_reply_done 的 info 字段：
    session_type / target_id / session_id
    sentences / emotions / role / reply / sender_id / user_text
"""

import json

SETTINGS_FILE = "settings.json"
DEFAULT_SETTINGS = {
    "meow_reply_enabled": False,
    "meow_reply_times": 1,
    "appearance_enabled": False,
    "meow_text": "喵～",
    "meow_voice_text": "喵～",
    "meow_voice_emotion": "",
    "accent": "#7c4dff",
}

# 运行期状态：settings 缓存 + 该缓存对应的文件修改时间，避免每条回复都读文件
_state = {"settings": None, "mtime": object(), "busy": set()}


def _settings_path(ctx):
    return ctx.data_dir() / SETTINGS_FILE


def _load_settings(ctx):
    """读插件设置。文件不存在/损坏时返回默认值，不抛异常。

    设置也可能被 WebUI 的「换肤」页直接改掉（那条路径不经过本模块），
    所以这里按文件的修改时间判断缓存是否还新鲜，而不是只读一次。
    """
    f = _settings_path(ctx)
    try:
        mtime = f.stat().st_mtime
    except OSError:
        mtime = None
    cached = _state.get("settings")
    if isinstance(cached, dict) and _state.get("mtime") == mtime:
        return cached
    data = dict(DEFAULT_SETTINGS)
    try:
        if mtime is not None:
            raw = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update({k: v for k, v in raw.items() if v is not None})
    except Exception as e:
        ctx.log("读取设置失败，使用默认值:", e)
    _state["settings"] = data
    _state["mtime"] = mtime
    return data


def _save_settings(ctx, data):
    _state["settings"] = data
    try:
        f = _settings_path(ctx)
        f.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        _state["mtime"] = f.stat().st_mtime
        return True
    except Exception as e:
        ctx.log("保存设置失败:", e)
        return False


def _is_enabled(ctx):
    return bool(_load_settings(ctx).get("meow_reply_enabled"))


def _toggle(ctx, name_or_args=None, args=None, event=None):
    """#喵开关 —— 不带参数时翻转，带 on/off 时显式设置。

    签名兼容两种调用约定：
      · 运行时以 `handler(ctx, name, args, event)` 调用（4 个位置参数）；
      · 早期/其他集成可能只传 `(ctx, args)`。
    统一把「真正的参数」挑出来，避免因为多传了 name/event 而 TypeError。
    """
    if args is None and event is None:
        # 只有两个位置参数：第二个就是 args
        real_args = name_or_args
    else:
        real_args = args
    data = _load_settings(ctx)
    arg = str(real_args or "").strip().lower()
    if arg in ("on", "1", "true", "开", "开启"):
        new = True
    elif arg in ("off", "0", "false", "关", "关闭"):
        new = False
    else:
        new = not bool(data.get("meow_reply_enabled"))
    data["meow_reply_enabled"] = new
    _save_settings(ctx, data)
    if new:
        return (f"喵喵追加已开启：以后每轮回复完，我会再发"
                f"{data.get('meow_reply_times', 1)} 条"
                f"「{data.get('meow_text') or DEFAULT_SETTINGS['meow_text']}」"
                f"和一条喵叫语音。")
    return "喵喵追加已关闭，回复恢复原样。"


def _status(ctx, name_or_args=None, args=None, event=None):
    data = _load_settings(ctx)
    return ("喵喵插件状态：" +
            ("追加已开启" if data.get("meow_reply_enabled") else "追加已关闭") +
            f"；条数 {data.get('meow_reply_times', 1)}；"
            f"文本「{data.get('meow_text')}」；"
            f"主色 {data.get('accent')}")


def on_load(ctx):
    """注册指令，并把皮肤设置里的主色同步成插件默认值。"""
    ctx.register_command("喵开关", _toggle)
    ctx.register_command("喵状态", _status)
    ctx.log("喵喵插件已加载")


def on_unload(ctx):
    """停用/卸载时确保开关处于关闭状态，行为立刻回到主程序原样。

    皮肤部分无需在这里处理：theme.css 只在插件启用时才会被主程序拼接，
    禁用或卸载后自然不再生效。
    """
    data = _load_settings(ctx)
    if data.get("meow_reply_enabled"):
        data["meow_reply_enabled"] = False
        _save_settings(ctx, data)
        ctx.log("插件已停用，喵喵追加已关闭")
    _state["settings"] = None
    _state["mtime"] = object()
    _state["busy"].clear()


def on_reply_done(ctx, info):
    """一轮 LLM 回复发送完成后触发：追加一条文字 + 一条语音。

    语音通过 ctx.send_voice 交给主程序自己的 TTS 链路合成，
    合成失败时降级为只发文字，不影响别的东西。
    """
    if not _is_enabled(ctx):
        return
    data = _load_settings(ctx)
    session_type = str((info or {}).get("session_type") or "group")
    target_id = (info or {}).get("target_id")
    if target_id is None:
        return
    # 同一会话上一轮还在发就跳过，防止连发消息把追加内容叠成一串
    key = f"{session_type}_{target_id}"
    if key in _state["busy"]:
        ctx.log("上一轮追加还没结束，本轮跳过:", key)
        return
    _state["busy"].add(key)
    try:
        text = str(data.get("meow_text") or DEFAULT_SETTINGS["meow_text"])
        voice_text = str(data.get("meow_voice_text")
                         or DEFAULT_SETTINGS["meow_voice_text"])
        try:
            times = int(data.get("meow_reply_times", 1))
        except (TypeError, ValueError):
            times = 1
        times = max(1, min(5, times))
        for i in range(times):
            if text:
                ctx.send_text(session_type, target_id, text)
            if voice_text:
                # 语音只在最后一轮发，免得叠出一串喵叫
                if i == times - 1:
                    ok = ctx.send_voice(session_type, target_id, voice_text,
                                        str(data.get("meow_voice_emotion") or ""))
                    if not ok:
                        ctx.log("喵叫语音发送失败，本轮只发了文字")
    finally:
        _state["busy"].discard(key)
