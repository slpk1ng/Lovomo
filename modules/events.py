"""事件监听问候：节假日/纪念日/用户生日自动问候。

配置存于 data/events.json（WebUI「定时任务」页编辑）：
  [{"id": "newyear", "name": "元旦", "type": "date", "date": "01-01", "enabled": true,
    "mode": "template|llm", "template": "新年快乐！", "llm_prompt": "今天是元旦，向主人送上祝福",
    "use_voice": false, "targets": [{"session_type": "private", "session_id": "10001"}]}]

生日问候（type=birthday）可绑定 user_id（从用户画像读取生日）；
另提供全局「画像生日问候」：所有画像中生日匹配今天的用户都会收到私信祝福。

内置默认节日（default_events_enabled 开启后自动注入 events.json，可在 WebUI 停用/修改）：
只包含公历固定日期的节日——春节、中秋等农历节日日期逐年不同，请按当年公历日期自行添加，
写错日期的节日问候比没有更糟，所以不做硬编码猜测。
"""
import time
from pathlib import Path
from typing import Callable, Optional

from .llm_helpers import RoleContext
from .jobs import generate_proactive_text, render_template


def _default_event(eid: str, name: str, date: str, prompt: str) -> dict:
    return {"id": eid, "name": name, "type": "date", "date": date,
            "enabled": True, "mode": "llm", "llm_prompt": prompt,
            "use_voice": False, "targets": [], "default": True}


DEFAULT_EVENTS = [
    _default_event("default_yuandan", "元旦", "01-01",
                   "今天是元旦、新年的第一天，向主人送上新年祝福，可以聊聊新一年的期待"),
    _default_event("default_valentine", "情人节", "02-14",
                   "今天是情人节，用你的角色口吻向主人表达心意与陪伴的感谢"),
    _default_event("default_women", "妇女节", "03-08",
                   "今天是妇女节，向主人送上节日问候与感谢"),
    _default_event("default_labour", "劳动节", "05-01",
                   "今天是五一劳动节，向辛苦的主人送上慰问，提醒主人好好休息"),
    _default_event("default_children", "儿童节", "06-01",
                   "今天是儿童节，用轻松俏皮的方式祝主人节日快乐，保持童心"),
    _default_event("default_national", "国庆节", "10-01",
                   "今天是国庆节，向主人送上国庆假期的祝福"),
    _default_event("default_christmas", "圣诞节", "12-25",
                   "今天是圣诞节，向主人送上圣诞祝福，营造节日气氛"),
]


class EventManager:
    def __init__(self, config, data_path: Path, profiles=None):
        self.config = config
        self.file = Path(data_path) / "events.json"
        self.log_file = Path(data_path) / "greeting_log.json"
        self.profiles = profiles
        self.events: list = []
        self.load_events()
        self.ensure_default_events()
        self.load_log()

    def ensure_default_events(self):
        """把内置默认节日合并进 events.json（default_events_enabled 开启时）。

        合并策略：按 id 判断，列表里没有的默认节日才追加；已存在的（含被用户
        修改/停用的）绝不覆盖。想永久关掉某个默认节日，在 WebUI 里把它停用即可。
        """
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
        from .jsonio import load_json
        data = load_json(self.log_file, {})
        self._log = data if isinstance(data, dict) else {}

    def _mark_sent(self, key: str):
        today = time.strftime("%Y-%m-%d")
        self._log.setdefault(today, [])
        if key not in self._log[today]:
            self._log[today].append(key)
        # 只保留最近 30 天
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))
        self._log = {k: v for k, v in self._log.items() if k >= cutoff}
        try:
            # 必须原子写：写一半被强杀会让日志损坏，当天问候会被重复发送一遍
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
        """每日问候检查（由调度器在 greeting_check_hour 触发）。

        sessions_provider 返回 [(session_type, session_id)]：默认节日事件没有
        指定 targets 时，据此把问候发给全部已知会话（default_events_to_all）。
        history_provider(session_id, ctx) 返回对话历史提示块，让问候能承接之前的聊天。
        返回本次真正发出的问候条数（补发链据此判断"到底补发了没有"）。
        """
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
            targets = event.get("targets") or []
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
            is_llm = str(event.get("mode", "template")) == "llm"
            if not is_llm:
                # 模板模式：文案与目标无关，生成一次即可
                text = await self._render_event_text(event, ctx)
                if not text:
                    print(f"[节日问候] {event.get('name', event.get('id'))} 文案生成失败，"
                          "本次不标记已发送，下次检查时重试。")
                    continue
                sent_any = await self._send_to_targets(sender, targets, text, emotions, ctx, event)
            else:
                # LLM 模式：逐个目标生成，带上该会话的聊天历史，让问候承接得上前文
                sent_any = False
                for target in targets:
                    session_key = (f"{target.get('session_type', 'private')}_"
                                   f"{target.get('session_id', '')}")
                    block = ""
                    if history_provider is not None:
                        try:
                            block = history_provider(session_key, ctx) or ""
                        except Exception as e:
                            print(f"[节日问候] 获取会话 {session_key} 的历史失败（忽略）: "
                                  f"{type(e).__name__}: {e}")
                    text = await self._render_event_text(event, ctx, history_block=block)
                    if not text:
                        print(f"[节日问候] {event.get('name', event.get('id'))} 对 {session_key} "
                              "的文案生成失败，下次检查时重试。")
                        continue
                    if await self._send_to_targets(sender, [target], text, emotions, ctx, event):
                        sent_any = True
            if sent_any:
                sent_total += 1
                print(f"[节日问候] {event.get('name', event.get('id'))} 已发送"
                      f"（模式：{event.get('mode', 'template')}）")
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
                text = template.replace("{nickname}", profile.get("nickname") or "主人")
                if self.config.get("birthday_greet_mode", "template") == "llm":
                    block = ""
                    if history_provider is not None:
                        try:
                            block = history_provider(f"private_{user_id}", ctx) or ""
                        except Exception as e:
                            print(f"[生日祝福] 获取会话历史失败（忽略）: {type(e).__name__}: {e}")
                    text = await generate_proactive_text(ctx, text, history_block=block) or text
                ok = await sender.speak_and_send(
                    "private", user_id, text, emotions, ctx,
                    use_voice=bool(self.config.get("birthday_greet_voice", False)),
                    sticker=bool(self.config.get("proactive_sticker", False)))
                if not ok:
                    print(f"[生日祝福] {user_id} 发送失败，不标记已发送，下次检查时重试。")
                    continue
                self._mark_sent(key)
                sent_total += 1
        return sent_total

    async def _send_to_targets(self, sender, targets, text, emotions, ctx, event) -> bool:
        """把同一段话发给多个目标；返回是否至少成功发送了一个。"""
        sent_any = False
        for target in targets:
            try:
                ok = await sender.speak_and_send(
                    target.get("session_type", "private"), target.get("session_id", ""),
                    text, emotions, ctx,
                    use_voice=bool(event.get("use_voice", False)),
                    sticker=bool(self.config.get("proactive_sticker", False)))
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
        mode = event.get("mode", "template")
        if mode == "llm":
            instruction = render_template(str(event.get("llm_prompt", "") or
                                              f"今天是{event.get('name', '节日')}，向主人送上问候"),
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
        ctx = ctx_provider()
        text = await self._render_event_text(event, ctx)
        if not text:
            return False
        sent_any = False
        for target in event.get("targets", []):
            ok = await sender.speak_and_send(target.get("session_type", "private"),
                                             target.get("session_id", ""), text,
                                             emotions_provider(), ctx,
                                             use_voice=bool(event.get("use_voice", False)))
            sent_any = sent_any or bool(ok)
        return sent_any
