"""用户自定义定时任务：interval / daily / weekly 触发，向指定会话发送模板或 LLM 生成消息。

配置存于 data/scheduled_jobs.json（WebUI「定时任务」页可视化编辑）：
  {
    "id": "morning_greet",
    "name": "每日早安",
    "enabled": true,
    "trigger": {"type": "daily", "time": "08:30"},        # 或 {"type":"interval","seconds":3600}
    "weekdays": [0,1,2,3,4],                              # daily 可选，0=周一
    "target": {"session_type": "private", "session_id": "10001"},
    "action": {"mode": "template", "template": "主人早上好呀～今天是 {date} {weekday}",
               "use_voice": true}
  }
mode=llm 时使用 action.llm_prompt 生成开场白（可使用 {character_name} 等占位符）。

每个任务当天是否已经跑过记在 data/scheduled_jobs_state.json：
程序在任务时刻之后才启动时，catch_up_missed_daily 会把当天漏掉的每日任务补跑一次。
"""
import json
import time
from pathlib import Path
from typing import Callable, Optional

from .scheduler import SchedulerManager
from .llm_helpers import (RoleContext, generate_text_reply, strip_thinking,
                          looks_like_thinking, no_think_suffix)

JOB_PREFIX = "sched_"


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
    """让角色以其人设说一句话（用于欢迎语 / 提醒话术等辅助生成）。

    instruction 形如「把『吃药』这件事提醒主人」；history_block 是对话历史
    （见主程序 dialog_history_block），用于让主动消息/问候承接之前的聊天，
    避免"凭空开场、话题对不上"。返回纯文本，失败返回空串。

    安全约束（血泪教训）：推理型模型可能把思维链写进输出，
    一旦被当成台词就会合成出一分多钟的"思考内容语音"。因此：
      1. 统一剥离 thinking（strip_thinking / chat_once 双重清洗）；
      2. 输出超长或仍带思考特征时，判定为失败返回空串（调用方走预设模板或跳过），
         绝不把一大段文字丢给 TTS。
    """
    raw_limit = max(40, int(ctx.get("proactive_text_max_chars", 120) or 120))
    system = (
        f"{ctx.get('personality_prompt', '')}\n"
        "【输出要求】直接输出你要说的那句话本身，口语化、简短自然（1~3句，不超过80字）。"
        "禁止输出JSON、解释、思考过程、动作描写或任何多余格式。"
    )
    if str(history_block or "").strip():
        system += (
            "\n【承接上文】下面会给出你和主人的真实对话历史："
            "你的这句话必须自然承接历史里的内容（延续同一话题、呼应主人的状态与称呼），"
            "禁止假装之前什么都没发生过，禁止问历史里刚刚已经聊过、已经回答过的问题。"
        )
    # 推理型模型补一道强关思考的模板开关，减少"思考被当成台词"的概率
    prompt_parts = [str(history_block or "").strip(), instruction]
    user_prompt = "\n\n".join(p for p in prompt_parts if p) + no_think_suffix(ctx)
    try:
        text = await generate_text_reply(ctx, system, user_prompt, max_tokens=max_tokens)
    except Exception as e:
        print(f"角色话术生成失败: {type(e).__name__}: {e}")
        return ""
    # 原始输出只要带思考特征就整段丢弃：主动消息会直接变成语音，
    # 宁可这次不发，也不能把思考过程念给主人听。
    if thinks := looks_like_thinking(text):
        print(f"角色话术疑似思考内容，已整段丢弃：{str(text)[:80]!r}")
        return ""
    text = strip_thinking(str(text or "")).strip().strip('"“”')
    if not text:
        return ""
    if len(text) > raw_limit:
        print(f"角色话术过长（{len(text)} 字，上限 {raw_limit}），已丢弃：{text[:80]!r}")
        return ""
    return text


async def generate_proactive_text(ctx: RoleContext, instruction: str,
                                  history_block: str = "") -> str:
    """让角色以其人设生成一段主动开口的话（纯文本，无 JSON）。

    history_block 非空时随请求一起发给模型，用于主动消息/节日问候承接上下文。
    """
    return await generate_in_character_text(ctx, instruction, max_tokens=200,
                                            history_block=history_block)


class ScheduledJobManager:
    def __init__(self, config, data_path: Path, scheduler: SchedulerManager,
                 sender=None, ctx_provider: Optional[Callable[[], RoleContext]] = None,
                 emotions_provider: Optional[Callable[[], dict]] = None):
        self.config = config
        self.file = Path(data_path) / "scheduled_jobs.json"
        self.state_file = Path(data_path) / "scheduled_jobs_state.json"
        self.scheduler = scheduler
        self.sender = sender
        self.ctx_provider = ctx_provider
        self.emotions_provider = emotions_provider
        self.jobs: list = []
        self.run_state: dict = {}
        self.load()
        self.load_state()

    # ---------------- 持久化 ----------------
    def load_state(self):
        try:
            if self.state_file.exists():
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                self.run_state = data if isinstance(data, dict) else {}
        except Exception as e:
            print(f"加载定时任务运行状态失败: {e}")
            self.run_state = {}

    def save_state(self):
        try:
            from .jsonio import save_json
            cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - 14 * 86400))
            self.run_state = {k: v for k, v in self.run_state.items()
                              if not isinstance(v, str) or v >= cutoff}
            save_json(self.state_file, self.run_state)
        except Exception as e:
            print(f"保存定时任务运行状态失败: {e}")

    def _mark_ran(self, job_id: str, when: float = None):
        self.run_state[str(job_id)] = time.strftime(
            "%Y-%m-%d", time.localtime(when if when is not None else time.time()))
        self.save_state()

    def _ran_on(self, job_id: str, day: str) -> bool:
        return str(self.run_state.get(str(job_id), "")) == day

    def load(self):
        try:
            if self.file.exists():
                self.jobs = json.loads(self.file.read_text(encoding="utf-8"))
                if not isinstance(self.jobs, list):
                    self.jobs = []
        except Exception as e:
            print(f"加载定时任务失败: {e}")
            self.jobs = []

    def save(self):
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
            # 调度器以 daily+weekdays 表示每周任务；兼容 weekly 类型避免退化为分钟级循环
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
        """WebUI 保存后重新注册全部任务。"""
        self.load()
        for job_id in [jid for jid in list(self.scheduler.jobs) if jid.startswith(JOB_PREFIX)]:
            self.scheduler.remove_job(job_id)
        self.register_all()

    def describe(self):
        """返回任务定义 + 实时运行状态。"""
        infos = []
        for job in self.jobs:
            info = dict(job)
            live = self.scheduler.jobs.get(JOB_PREFIX + str(job.get("id", "")))
            if live:
                info["runtime"] = live.describe()
            else:
                info["runtime"] = None
            infos.append(info)
        return infos

    # ---------------- 执行 ----------------
    async def _run_job(self, job: dict):
        target = job.get("target") or {}
        action = job.get("action") or {}
        session_type = target.get("session_type", "private")
        session_id = target.get("session_id", "")
        if not session_id or self.sender is None or self.sender.client is None:
            return
        ctx = (self.ctx_provider() if self.ctx_provider else None) or RoleContext(self.config)
        emotions = (self.emotions_provider() if self.emotions_provider else None) or {}
        mode = action.get("mode", "template")
        text = ""
        if mode == "llm":
            instruction = render_template(str(action.get("llm_prompt", "") or "主动打个招呼"),
                                          character_name=ctx.character_name)
            try:
                text = await generate_proactive_text(ctx, instruction)
            except Exception as e:
                print(f"定时任务 LLM 生成失败: {e}")
        else:
            text = render_template(str(action.get("template", "")),
                                   character_name=ctx.character_name)
        if not text:
            return
        await self.sender.speak_and_send(session_type, session_id, text, emotions, ctx,
                                         use_voice=bool(action.get("use_voice", False)),
                                         sticker=bool(self.config.get("proactive_sticker", False)))
        self._mark_ran(job.get("id", ""))

    def _daily_time_passed_today(self, job: dict) -> Optional[float]:
        """返回该每日任务今天应执行的时刻；今天没有该任务的时刻时返回 None。"""
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
        """补跑当天已到点但还没执行过的每日任务，返回补跑成功的条数。

        程序在问候/早安这类任务时刻之后才启动时，任务不会再被触发；
        这里按当天应执行时刻判断并补跑一次，每个任务每天最多补一次
        （已跑过的记在 scheduled_jobs_state.json）。
        """
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
            try:
                await self._run_job(job)
                done += 1
            except Exception as e:
                print(f"[定时任务] 补发 {job.get('name', job_id)} 失败: "
                      f"{type(e).__name__}: {e}")
        return done
