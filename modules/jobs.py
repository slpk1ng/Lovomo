import time
from pathlib import Path
from typing import Callable, Optional

from .scheduler import SchedulerManager
from .llm_helpers import (RoleContext, generate_text_reply, strip_thinking,
                          looks_like_thinking, no_think_suffix)
from .session_context import reply_ctx_of

JOB_PREFIX = "sched_"

_SENTENCE_END_MARKS = "。！？!?…"
_CLAUSE_MARKS = "，、；,;"


def clip_to_limit(text: str, limit: int) -> str:
    head = text[:limit]
    for marks in (_SENTENCE_END_MARKS, _CLAUSE_MARKS):
        cut = max(head.rfind(m) for m in marks)
        if cut >= 0:
            clipped = head[:cut + 1].strip()
            if len(clipped) * 2 >= limit:
                return clipped
    return ""


def render_template(template: str, character_name: str = "") -> str:
    lt = time.localtime()
    try:
        weekday = "周" + "一二三四五六日"[lt.tm_wday % 7]
    except Exception:
        weekday = ""
    return (str(template)
            .replace("{date}", time.strftime("%Y-%m-%d", lt))
            .replace("{time}", time.strftime("%H:%M", lt))
            .replace("{weekday}", weekday)
            .replace("{character_name}", str(character_name or "")))


async def generate_in_character_text(ctx: RoleContext, instruction: str,
                                     max_tokens: int = 200,
                                     history_block: str = "") -> str:
    raw_limit = max(40, int(ctx.get("proactive_text_max_chars", 120) or 120))
    system = (
        f"{ctx.get('personality_prompt', '')}\n"
        "【输出要求】直接输出你要说的那句话本身，口语化、简短自然（1~3句，不超过80字）。"
        "禁止输出JSON、解释、思考过程、动作描写或任何多余格式。"
    )
    if str(history_block or "").strip():
        system += (
            "\n【承接上文】下面会给出你和对方的真实对话历史："
            "你的这句话必须自然承接历史里的内容（延续同一话题、呼应对方的状态与称呼），"
            "禁止假装之前什么都没发生过，禁止问历史里刚刚已经聊过、已经回答过的问题。"
        )
    prompt_parts = [str(history_block or "").strip(), instruction]
    user_prompt = "\n\n".join(p for p in prompt_parts if p) + no_think_suffix(ctx)
    try:
        text = await generate_text_reply(ctx, system, user_prompt, max_tokens=max_tokens,
                                         label="主动消息")
    except Exception as e:
        print(f"角色话术生成失败: {type(e).__name__}: {e}")
        return ""
    if thinks := looks_like_thinking(text):
        print(f"角色话术疑似思考内容，已整段丢弃：{str(text)[:80]!r}")
        return ""
    text = strip_thinking(str(text or "")).strip().strip('"“”')
    if not text:
        print("角色话术生成为空（模型没有给出有效文本），本次跳过该条话术。")
        return ""
    if len(text) > raw_limit:
        clipped = clip_to_limit(text, raw_limit)
        if not clipped:
            print(f"角色话术过长（{len(text)} 字，上限 {raw_limit}）且截不出完整句子，已丢弃：{text[:80]!r}")
            return ""
        print(f"角色话术过长（{len(text)} 字，上限 {raw_limit}），已截断为 {len(clipped)} 字")
        text = clipped
    return text


async def generate_proactive_text(ctx: RoleContext, instruction: str,
                                  history_block: str = "") -> str:
    return await generate_in_character_text(ctx, instruction, max_tokens=200,
                                            history_block=history_block)


class ScheduledJobManager:
    def __init__(self, config, data_path: Path, scheduler: SchedulerManager,
                 sender=None, ctx_provider: Optional[Callable[[], RoleContext]] = None,
                 emotions_provider: Optional[Callable[[], dict]] = None,
                 sessions_provider: Optional[Callable[[], list]] = None,
                 history_provider: Optional[Callable] = None):
        self.config = config
        self.file = Path(data_path) / "scheduled_jobs.json"
        self.state_file = Path(data_path) / "scheduled_jobs_state.json"
        self.scheduler = scheduler
        self.sender = sender
        self.ctx_provider = ctx_provider
        self.emotions_provider = emotions_provider
        self.sessions_provider = sessions_provider
        self.history_provider = history_provider
        self.jobs: list = []
        self.run_state: dict = {}
        self.load()
        self.load_state()

    # ---------------- 持久化 ----------------
    def load_state(self):
        from .jsonio import load_json_ex
        if not self.state_file.exists():
            self._state_load_failed = False
            return
        data, readable = load_json_ex(self.state_file, {})
        self._state_load_failed = not readable
        self.run_state = data if isinstance(data, dict) else {}

    def save_state(self):
        if getattr(self, "_state_load_failed", False):
            print("定时任务运行状态本次未能读取，已跳过保存以免覆盖磁盘上的原有内容。")
            return
        try:
            from .jsonio import save_json
            cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - 14 * 86400))
            # 又会永久留在文件里
            self.run_state = {k: v for k, v in self.run_state.items()
                              if isinstance(v, str) and v >= cutoff}
            save_json(self.state_file, self.run_state)
        except Exception as e:
            print(f"保存定时任务运行状态失败: {e}")

    def _mark_ran(self, job_id: str, when: float = None):
        self.run_state[str(job_id)] = time.strftime(
            "%Y-%m-%d", time.localtime(when if when is not None else time.time()))
        self.save_state()

    def _clear_ran(self, job_id: str):
        if self.run_state.pop(str(job_id), None) is not None:
            self.save_state()

    def _ran_on(self, job_id: str, day: str) -> bool:
        return str(self.run_state.get(str(job_id), "")) == day

    def load(self):
        from .jsonio import load_json_ex
        if not self.file.exists():
            self._load_failed = False
            return
        data, readable = load_json_ex(self.file, [])
        self._load_failed = not readable
        self.jobs = data if isinstance(data, list) else []

    def save(self):
        if getattr(self, "_load_failed", False):
            print("定时任务本次未能读取，已跳过保存以免覆盖磁盘上的原有配置。")
            return
        try:
            from .jsonio import save_json
            save_json(self.file, self.jobs)
        except Exception as e:
            print(f"保存定时任务失败: {e}")

    # ---------------- 调度 ----------------
    def register_all(self):
        if not self.config.get("scheduler_enabled", True):
            print("定时任务功能未开启（scheduler_enabled=false），跳过注册。")
            return
        count = 0
        for job in self.jobs:
            if self._register(job):
                count += 1
        print(f"已注册 {count}/{len(self.jobs)} 个自定义定时任务。")

    def _register(self, job: dict) -> bool:
        if not job.get("enabled"):
            return False
        trigger = job.get("trigger") or {}
        ttype = trigger.get("type")
        if ttype == "weekly":
            trigger = {"type": "daily", "time": trigger.get("time", "08:00"),
                       "weekdays": trigger.get("weekdays")}
            ttype = "daily"
        if ttype not in ("interval", "daily"):
            print(f"定时任务 {job.get('name')} 触发器类型无效: {trigger.get('type')}")
            return False
        job_id = JOB_PREFIX + str(job.get("id", ""))
        self.scheduler.add_job(job_id, job.get("name", job_id), trigger,
                               self._run_job, args=(job,))
        return True

    def reload(self):
        self.load()
        for job_id in [jid for jid in list(self.scheduler.jobs) if jid.startswith(JOB_PREFIX)]:
            self.scheduler.remove_job(job_id)
        self.register_all()

    def describe(self):
        infos = []
        for job in self.jobs:
            info = dict(job)
            live = self.scheduler.jobs.get(JOB_PREFIX + str(job.get("id", "")))
            info["runtime"] = live.describe() if live else None
            targets = self.resolve_targets(job.get("target") or {})
            info["resolved"] = {
                "targets": [{"session_type": t, "session_id": s} for t, s in targets],
                "broadcast": not str((job.get("target") or {}).get("session_id") or "").strip(),
            }
            infos.append(info)
        return infos

    # ---------------- 发送目标 ----------------
    def known_sessions(self) -> list:
        if self.sessions_provider is None:
            return []
        try:
            items = self.sessions_provider() or []
        except Exception as e:
            print(f"读取会话列表失败: {type(e).__name__}: {e}")
            return []
        return [(str(s.get("session_type", "private")), str(s.get("session_id", "")).strip())
                for s in items if str(s.get("session_id", "")).strip()]

    def resolve_targets(self, target: dict) -> list:
        session_type = str(target.get("session_type") or "private")
        session_id = str(target.get("session_id") or "").strip()
        known = self.known_sessions()
        if not session_id:
            return list(known)
        for prefix, stype in (("private_", "private"), ("group_", "group")):
            if session_id.startswith(prefix):
                session_type = stype
                session_id = session_id[len(prefix):]
                break
        if session_type == "group":
            session_id = session_id.split("_")[0]
        for stype, sid in known:
            if sid == session_id and stype != session_type:
                print(f"[定时任务] 目标类型已纠正：{session_id} 实际是{stype}会话"
                      f"（任务里填的是{session_type}）")
                session_type = stype
                break
        return [(session_type, session_id)]

    def _history_block(self, session_key: str, ctx) -> str:
        if self.history_provider is None:
            return ""
        try:
            return str(self.history_provider(session_key, ctx) or "")
        except Exception as e:
            print(f"[定时任务] 读取会话历史失败（忽略）: {type(e).__name__}: {e}")
            return ""

    # ---------------- 执行 ----------------
    async def _run_job(self, job: dict) -> bool:
        target = job.get("target") or {}
        action = job.get("action") or {}
        if self.sender is None or self.sender.client is None:
            return False
        targets = self.resolve_targets(target)
        if not targets:
            print(f"[定时任务] {job.get('name') or job.get('id', '')} 没有可用的发送目标，跳过。")
            return False
        ctx = (self.ctx_provider() if self.ctx_provider else None) or RoleContext(self.config)
        emotions = (self.emotions_provider() if self.emotions_provider else None) or {}
        mode = action.get("mode", "template")
        template_text = ""
        if mode != "llm":
            template_text = render_template(str(action.get("template", "")),
                                            character_name=ctx.character_name)
            if not template_text:
                return False
        sent_any = False
        composed = 0
        for session_type, session_id in targets:
            session_key = f"{session_type}_{session_id}"
            session_ctx = reply_ctx_of(ctx.role, session_id=session_key)
            if mode == "llm":
                instruction = render_template(
                    str(action.get("llm_prompt", "") or "主动打个招呼"),
                    character_name=session_ctx.character_name)
                block = self._history_block(session_key, session_ctx)
                if block:
                    print(f"[定时任务] 已带上会话 {session_key} 的聊天历史"
                          f"（{len(block)} 字），话术会承接上次话题。")
                try:
                    text = await generate_proactive_text(session_ctx, instruction,
                                                         history_block=block)
                except Exception as e:
                    print(f"定时任务 LLM 生成失败: {e}")
                    text = ""
            else:
                text = template_text
            if not text:
                print(f"[定时任务] {job.get('name') or job.get('id', '')} 向 "
                      f"{session_type} {session_id} 生成的话术为空，本次跳过"
                      f"（检查 LLM 是否返回了空内容，或模板是否为空）。")
                continue
            composed += 1
            try:
                with self.sender.for_session(session_key):
                    ok = await self.sender.speak_and_send(
                        session_type, session_id, text, emotions, session_ctx,
                        use_voice=bool(action.get("use_voice", False)),
                        sticker=bool(self.config.get("proactive_sticker", False)),
                        session_id=session_key)
            except Exception as e:
                print(f"[定时任务] {job.get('name') or job.get('id', '')} 向 "
                      f"{session_type} {session_id} 发送失败: {type(e).__name__}: {e}")
                ok = False
            sent_any = sent_any or bool(ok)
        if not composed:
            return False
        if not sent_any:
            print(f"定时任务 {job.get('id', '')} 发送未成功，本次不标记已执行。")
            return False
        self._mark_ran(job.get("id", ""))
        return True

    def _daily_time_passed_today(self, job: dict) -> Optional[float]:
        trigger = job.get("trigger") or {}
        if trigger.get("type") not in ("daily", "weekly"):
            return None
        weekdays = trigger.get("weekdays")
        if trigger.get("type") == "weekly" and not weekdays:
            weekdays = None
        now = time.time()
        lt = time.localtime(now)
        if weekdays:
            try:
                if lt.tm_wday not in [int(w) for w in weekdays]:
                    return None
            except (TypeError, ValueError):
                pass
        try:
            hour, minute = [int(x) for x in str(trigger.get("time", "08:00")).split(":")[:2]]
        except Exception:
            hour, minute = 8, 0
        try:
            due = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1))
        except (OverflowError, ValueError):
            return None
        return due if due <= now else None

    async def catch_up_missed_daily(self) -> int:
        if not self.config.get("scheduler_enabled", True):
            return 0
        if self.sender is None or self.sender.client is None:
            return 0
        today = time.strftime("%Y-%m-%d")
        now = time.time()
        done = 0
        for job in list(self.jobs):
            if not job.get("enabled"):
                continue
            job_id = str(job.get("id", ""))
            due = self._daily_time_passed_today(job)
            if due is None:
                continue
            if self._ran_on(job_id, today):
                continue
            if now - due > 12 * 3600:
                self._mark_ran(job_id, now)
                print(f"[定时任务] {job.get('name', job_id)} 今天的执行时刻已过去 12 小时以上，"
                      "不再补发（避免半夜补上一条早上的问候）。")
                continue
            print(f"[定时任务] 补发今天漏掉的每日任务：{job.get('name', job_id)}"
                  f"（应执行于 {time.strftime('%H:%M', time.localtime(due))}）")
            self._mark_ran(job_id)
            sent = False
            try:
                sent = await self._run_job(job)
            except Exception as e:
                print(f"[定时任务] 补发 {job.get('name', job_id)} 失败: "
                      f"{type(e).__name__}: {e}")
            if sent:
                done += 1
            else:
                self._clear_ran(job_id)
        return done
