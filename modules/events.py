import time
from pathlib import Path
from typing import Callable, Optional

from .llm_helpers import RoleContext
from .jobs import generate_proactive_text, render_template
from .session_context import reply_ctx_of


def _default_event(eid: str, name: str, date: str, prompt: str) -> dict:
    return {"id": eid, "name": name, "type": "date", "date": date,
            "enabled": True, "mode": "llm", "llm_prompt": prompt,
            "use_voice": False, "targets": [], "default": True}


DEFAULT_EVENTS = [
    _default_event("default_yuandan", "元旦", "01-01",
                   "今天是元旦、新年的第一天，向对方送上新年祝福，可以聊聊新一年的期待"),
    _default_event("default_valentine", "情人节", "02-14",
                   "今天是情人节，用你的角色口吻向对方表达心意与陪伴的感谢"),
    _default_event("default_women", "妇女节", "03-08",
                   "今天是妇女节，向对方送上节日问候与感谢"),
    _default_event("default_labour", "劳动节", "05-01",
                   "今天是五一劳动节，向辛苦的对方送上慰问，提醒对方好好休息"),
    _default_event("default_children", "儿童节", "06-01",
                   "今天是儿童节，用轻松俏皮的方式祝对方节日快乐，保持童心"),
    _default_event("default_national", "国庆节", "10-01",
                   "今天是国庆节，向对方送上国庆假期的祝福"),
    _default_event("default_christmas", "圣诞节", "12-25",
                   "今天是圣诞节，向对方送上圣诞祝福，营造节日气氛"),
]


class EventManager:
    def __init__(self, config, data_path: Path, profiles=None):
        self.config = config
        self.file = Path(data_path) / "events.json"
        self.log_file = Path(data_path) / "greeting_log.json"
        self.profiles = profiles
        self.events: list = []
        self._log_readable = True
        self.load_events()
        self.ensure_default_events()
        self.load_log()

    def ensure_default_events(self):
        if not self.config.get("default_events_enabled", False):
            return
        existing_ids = {str(e.get("id")) for e in self.events}
        added = []
        for ev in DEFAULT_EVENTS:
            if str(ev["id"]) not in existing_ids:
                self.events.append(dict(ev))
                added.append(ev["name"])
        if added:
            self.save_events()
            print(f"已注入 {len(added)} 个内置默认节日问候（可在「定时任务」页停用或修改）。")

    # ---------------- 持久化 ----------------
    def load_events(self):
        from .jsonio import load_json_ex
        if not self.file.exists():
            self._load_failed = False
            return
        data, readable = load_json_ex(self.file, [])
        self._load_failed = not readable
        self.events = data if isinstance(data, list) else []

    def save_events(self):
        if getattr(self, "_load_failed", False):
            print("事件配置本次未能读取，已跳过保存以免覆盖磁盘上的原有内容。")
            return
        try:
            from .jsonio import save_json
            save_json(self.file, self.events)
        except Exception as e:
            print(f"保存事件配置失败: {e}")

    def load_log(self):
        from .jsonio import load_json_ex
        data, readable = load_json_ex(self.log_file, {})
        self._log_readable = readable
        self._log = ({k: v for k, v in data.items() if isinstance(v, list)}
                     if isinstance(data, dict) else {})

    def _mark_sent(self, key: str):
        today = time.strftime("%Y-%m-%d")
        self._log.setdefault(today, [])
        if key not in self._log[today]:
            self._log[today].append(key)
        # 只保留最近 30 天
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))
        self._log = {k: v for k, v in self._log.items() if k >= cutoff}
        if not self._log_readable:
            print("问候记录本次未能读取，已跳过保存以免覆盖磁盘上的原有内容。")
            return
        try:
            from .jsonio import save_json
            save_json(self.log_file, self._log)
        except Exception as e:
            print(f"保存问候记录失败（下次检查可能重复发送）: {type(e).__name__}: {e}")

    def _sent(self, key: str) -> bool:
        return key in self._log.get(time.strftime("%Y-%m-%d"), [])

    # ---------------- 检查 ----------------
    async def check_and_greet(self, sender, ctx_provider: Callable[[], RoleContext],
                              emotions_provider: Callable[[], dict],
                              sessions_provider: Optional[Callable[[], list]] = None,
                              history_provider: Optional[Callable] = None) -> int:
        events_on = bool(self.config.get("greeting_events_enabled", False))
        birthday_on = bool(self.config.get("birthday_greeting_enabled", False))
        if not events_on and not birthday_on:
            print("[节日问候] 节日问候与生日祝福都未开启，本次不检查。")
            return 0
        today_md = time.strftime("%m-%d")
        ctx = ctx_provider()
        emotions = emotions_provider()
        checked = matched = sent_total = 0
        for event in (self.events if events_on else []):
            checked += 1
            if not event.get("enabled"):
                continue
            if str(event.get("date", "")).strip() != today_md:
                continue
            matched += 1
            key = f"event:{event.get('id')}"
            if self._sent(key):
                print(f"[节日问候] {event.get('name', event.get('id'))} 今天已经发过了，跳过。")
                continue
            targets = [t for t in (event.get("targets") or [])
                       if isinstance(t, dict) and str(t.get("session_id") or "").strip()]
            if not targets and event.get("default") \
                    and self.config.get("default_events_to_all", True) and sessions_provider:
                try:
                    targets = [{"session_type": st, "session_id": sid}
                               for st, sid in (sessions_provider() or [])]
                except Exception as e:
                    print(f"默认节日问候获取会话列表失败: {e}")
            if not targets:
                print(f"[节日问候] {event.get('name', event.get('id'))} 没有可发送的目标会话"
                      "（不是内置默认节日，或未勾选「默认节日发给全部会话」），跳过。")
                continue
            is_llm = str(event.get("mode", "llm")) != "template"
            if not is_llm:
                text = await self._render_event_text(event, ctx)
                if not text:
                    print(f"[节日问候] {event.get('name', event.get('id'))} 文案生成失败，"
                          "本次不标记已发送，下次检查时重试。")
                    continue
                sent_any = await self._send_to_targets(sender, targets, text, emotions, ctx, event)
            else:
                sent_any = False
                for target in targets:
                    session_key = (f"{target.get('session_type', 'private')}_"
                                   f"{target.get('session_id', '')}")
                    target_ctx = reply_ctx_of(ctx.role, session_id=session_key)
                    block = ""
                    if history_provider is not None:
                        try:
                            block = history_provider(session_key, target_ctx) or ""
                        except Exception as e:
                            print(f"[节日问候] 获取会话 {session_key} 的历史失败（忽略）: "
                                  f"{type(e).__name__}: {e}")
                    text = await self._render_event_text(event, target_ctx, history_block=block)
                    if not text:
                        print(f"[节日问候] {event.get('name', event.get('id'))} 对 {session_key} "
                              "的文案生成失败，下次检查时重试。")
                        continue
                    if await self._send_to_targets(sender, [target], text, emotions, target_ctx, event):
                        sent_any = True
            if sent_any:
                sent_total += 1
                print(f"[节日问候] {event.get('name', event.get('id'))} 已发送"
                      f"（模式：{event.get('mode', 'llm')}）")
                self._mark_sent(key)
            else:
                print(f"[节日问候] {event.get('name', event.get('id'))} 全部会话发送失败，不标记已发送。")
        if events_on:
            print(f"[节日问候] 检查完成：共 {checked} 个事件，其中 {matched} 个匹配今天的日期"
                  f"（{today_md}）。")
        # 用户画像生日问候
        if self.config.get("birthday_greeting_enabled", False) and self.profiles:
            template = str(self.config.get("birthday_greet_template", "") or
                           "今天是 {nickname} 的生日，送上最真挚的生日祝福！")
            for user_id, profile in list(self.profiles.profiles.items()):
                if str(profile.get("birthday", "")).strip() != today_md:
                    continue
                key = f"birthday:{user_id}"
                if self._sent(key):
                    continue
                text = template.replace("{nickname}", profile.get("nickname") or "你")
                session_key = f"private_{user_id}"
                user_ctx = reply_ctx_of(ctx.role, session_id=session_key)
                if self.config.get("birthday_greet_mode", "template") == "llm":
                    block = ""
                    if history_provider is not None:
                        try:
                            block = history_provider(session_key, user_ctx) or ""
                        except Exception as e:
                            print(f"[生日祝福] 获取会话历史失败（忽略）: {type(e).__name__}: {e}")
                    text = await generate_proactive_text(user_ctx, text, history_block=block) or text
                with sender.for_session(session_key):
                    ok = await sender.speak_and_send(
                        "private", user_id, text, emotions, user_ctx,
                        use_voice=bool(self.config.get("birthday_greet_voice", False)),
                        sticker=bool(self.config.get("proactive_sticker", False)),
                        session_id=session_key)
                if not ok:
                    print(f"[生日祝福] {user_id} 发送失败，不标记已发送，下次检查时重试。")
                    continue
                self._mark_sent(key)
                sent_total += 1
        return sent_total

    async def _send_to_targets(self, sender, targets, text, emotions, ctx, event) -> bool:
        sent_any = False
        seen = set()
        for target in targets:
            stype = target.get("session_type", "private")
            sid = target.get("session_id", "")
            if (stype, str(sid)) in seen:
                continue
            seen.add((stype, str(sid)))
            target_ctx = reply_ctx_of(ctx.role, session_id=f"{stype}_{sid}")
            try:
                with sender.for_session(f"{stype}_{sid}"):
                    ok = await sender.speak_and_send(
                        stype, sid, text, emotions, target_ctx,
                        use_voice=bool(event.get("use_voice", False)),
                        sticker=bool(self.config.get("proactive_sticker", False)),
                        session_id=f"{stype}_{sid}")
            except Exception as e:
                print(f"[节日问候] 发送失败 {target.get('session_id', '')}: {e}")
                continue
            if ok:
                sent_any = True
            else:
                print(f"[节日问候] 发送未成功 {target.get('session_id', '')}")
        return sent_any

    async def _render_event_text(self, event: dict, ctx: RoleContext,
                                 history_block: str = "") -> str:
        mode = event.get("mode", "llm")
        if mode != "template":
            instruction = render_template(str(event.get("llm_prompt", "") or
                                              f"今天是{event.get('name', '节日')}，向对方送上问候"),
                                          character_name=ctx.character_name)
            try:
                return await generate_proactive_text(ctx, instruction,
                                                     history_block=history_block)
            except Exception as e:
                print(f"节日问候 LLM 生成失败: {e}")
                return ""
        return render_template(str(event.get("template", "")), character_name=ctx.character_name)

    # ---------------- 手动触发（WebUI 测试） ----------------
    async def greet_event_now(self, event_id: str, sender, ctx_provider, emotions_provider):
        event = next((e for e in self.events if str(e.get("id")) == str(event_id)), None)
        if not event:
            return False
        base_ctx = ctx_provider()
        targets = event.get("targets", [])
        first = targets[0] if targets else {}
        first_key = (f"{first.get('session_type', 'private')}_"
                     f"{first.get('session_id', '')}") if targets else ""
        ctx = reply_ctx_of(base_ctx.role, session_id=first_key) if first_key else base_ctx
        text = await self._render_event_text(event, ctx)
        if not text:
            return False
        sent_any = False
        for target in targets:
            stype = target.get("session_type", "private")
            sid = target.get("session_id", "")
            session_key = f"{stype}_{sid}"
            target_ctx = reply_ctx_of(base_ctx.role, session_id=session_key)
            with sender.for_session(session_key):
                ok = await sender.speak_and_send(stype, sid, text,
                                                 emotions_provider(), target_ctx,
                                                 use_voice=bool(event.get("use_voice", False)),
                                                 session_id=session_key)
            sent_any = sent_any or bool(ok)
        return sent_any
