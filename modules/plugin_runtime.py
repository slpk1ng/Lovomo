"""插件运行时：把启用插件的 Python 代码 import 进来并调用约定的钩子。

约定钩子（都在插件 main.py 顶层定义，全部可选）
------------------------------------------------
    def on_load(ctx): ...      插件被加载时调用一次
    def on_unload(ctx): ...    插件被停用/卸载时调用一次
    def on_message(ctx, event): ...   收到 QQ 消息时调用（可返回字符串作为回复）
    def on_command(ctx, name, args, event): ...  自定义指令分发
    def on_reply_done(ctx, info): ...  一轮 LLM 回复发送完成后调用

`ctx` 是 LovomoContext，插件通过它拿配置、发消息、注册指令、记日志。
插件代码抛异常一律被吞掉并记录，绝不影响主程序运行。
"""

import importlib.util
import sys
import threading
import traceback
from pathlib import Path

PLUGIN_MODULE_PREFIX = "lovomo_plugin_"
HOOK_NAMES = ("on_load", "on_unload", "on_message", "on_command", "on_reply_done")


class LovomoContext:
    """暴露给插件的受限但她足够用的操作面板。

    插件作者只需要关心这里的方法，不需要（也不应该）去 import main。
    """

    def __init__(self, plugin_id: str, plugin_name: str, manager,
                 logger=None, config_getter=None, sender=None,
                 voice_sender=None, emotions_getter=None):
        self.plugin_id = plugin_id
        self.plugin_name = plugin_name
        self.manager = manager
        self._logger = logger
        self._config_getter = config_getter
        self._sender = sender
        self._voice_sender = voice_sender
        self._emotions_getter = emotions_getter
        self.commands = {}          # 插件可注册的指令：name -> callable

    # --- 日志 ---------------------------------------------------------
    def log(self, *parts) -> None:
        text = " ".join(str(p) for p in parts)
        line = f"[插件:{self.plugin_name}] {text}"
        if callable(self._logger):
            try:
                self._logger(line)
                return
            except Exception:
                pass
        print(line)

    # --- 配置 ---------------------------------------------------------
    @property
    def config(self) -> dict:
        if callable(self._config_getter):
            try:
                cfg = self._config_getter()
                return cfg if isinstance(cfg, dict) else {}
            except Exception:
                return {}
        return {}

    @property
    def emotions(self) -> dict:
        """当前角色的情绪表 {情绪名: {...}}，取不到返回空字典。"""
        if callable(self._emotions_getter):
            try:
                table = self._emotions_getter()
                return table if isinstance(table, dict) else {}
            except Exception:
                return {}
        return {}

    # --- 发消息 -------------------------------------------------------
    def send_message(self, group_id, text: str) -> bool:
        """向指定群发送一条文本消息（兼容早期只支持群聊的签名）。

        旧签名是 (group_id, text)，新桥接器是 (session_type, target_id, text)；
        这里按注入的可调用对象实际接受的参数个数自适应，避免旧签名插件失效。
        """
        if not callable(self._sender):
            self.log("send_text 不可用（当前上下文没接入发送器）")
            return False
        try:
            return bool(self._sender("group", group_id, str(text)))
        except TypeError:
            try:
                return bool(self._sender(group_id, str(text)))
            except Exception as e:
                self.log(f"发送文本失败: {e}")
                return False
        except Exception as e:
            self.log(f"发送文本失败: {e}")
            return False

    def send_text(self, session_type, target_id, text: str) -> bool:
        """向任意会话发一条文本消息。

        session_type 取 "group" / "private"，target_id 是群号或用户号。
        """
        if not callable(self._sender):
            self.log("send_text 不可用（当前上下文没接入发送器）")
            return False
        try:
            return bool(self._sender(session_type, target_id, str(text)))
        except Exception as e:
            self.log(f"发送文本失败: {e}")
            return False

    def send_voice(self, session_type, target_id, text: str,
                   emotion: str = "") -> bool:
        """把一段文字合成为语音并发出去。

        合成走主程序自己的 TTS 链路（含情绪音色选择与失败降级），
        插件不需要关心模型和音频格式。返回是否真的发出了语音。
        """
        if not callable(self._voice_sender):
            self.log("send_voice 不可用（当前上下文没接入语音发送器）")
            return False
        try:
            return bool(self._voice_sender(session_type, target_id,
                                           str(text), str(emotion or "")))
        except Exception as e:
            self.log(f"发送语音失败: {e}")
            return False

    # --- 注册指令 -----------------------------------------------------
    def register_command(self, name: str, handler) -> bool:
        name = str(name or "").strip().lstrip("/").lstrip("#")
        if not name or not callable(handler):
            return False
        self.commands[name] = handler
        return True

    # --- 数据目录 -----------------------------------------------------
    def data_dir(self) -> Path:
        """给插件一块属于它自己的可写目录（不会被卸载以外的事情清掉）。"""
        d = Path(self.manager.root) / self.plugin_id / "data"
        d.mkdir(parents=True, exist_ok=True)
        return d


class PluginRuntime:
    """负责 import 插件、调钩子、隔离异常。"""

    def __init__(self, manager, logger=None, config_getter=None, sender=None,
                 voice_sender=None, emotions_getter=None):
        self.manager = manager
        self._logger = logger
        self._config_getter = config_getter
        self._sender = sender
        self._voice_sender = voice_sender
        self._emotions_getter = emotions_getter
        self.loaded = {}         # pid -> {"module": m, "ctx": ctx, "hooks": {...}}
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- 日志
    def _log(self, text: str) -> None:
        if callable(self._logger):
            try:
                self._logger(text)
                return
            except Exception:
                pass
        print(text)

    # ---------------------------------------------------------------- 加载
    def load_all(self) -> dict:
        """加载所有启用的插件。

        返回 {pid: 错误信息}，只包含**加载阶段**就失败的插件（入口语法错误、
        缺依赖、导入即抛异常）。这些插件不会被注册进 loaded，等于没启用。

        注意区分：如果入口 import 成功、只是 on_load 里抛了异常，那插件仍然
        算加载成功（它可能已经注册好了部分指令），异常被记录在日志里由
        _call_record 处理。这样设计是故意的 —— 一个插件的小 bug 不该让它的
        全部功能凭空消失，用户能在日志里看到原因。
        """
        errors = {}
        for info in self.manager.list_plugins():
            if not info.get("enabled"):
                continue
            try:
                self.load_one(info)
            except Exception as e:
                errors[info["id"]] = f"{type(e).__name__}: {e}"
                self._log(f"[插件:{info.get('name')}] 加载失败: {type(e).__name__}: {e}")
        return errors

    def load_one(self, info: dict):
        """加载单个插件。已加载过则先卸载再加载（实现热更新）。"""
        pid = info["id"]
        with self._lock:
            if pid in self.loaded:
                self.unload_one(pid)
            d = Path(info["dir"])
            entry = d / (info.get("entry") or "main.py")
            ctx = LovomoContext(pid, info.get("name") or pid, self.manager,
                                logger=self._logger,
                                config_getter=self._config_getter,
                                sender=self._sender,
                                voice_sender=self._voice_sender,
                                emotions_getter=self._emotions_getter)
            record = {"info": info, "module": None, "ctx": ctx, "hooks": {},
                      "sys_path": ""}
            if not entry.is_file():
                # 纯皮肤插件没有 Python 入口，只提供 ctx 供别处使用
                self.loaded[pid] = record
                self._log(f"[插件:{info.get('name')}] 已启用（纯皮肤，无 Python 入口）")
                return record
            mod_name = f"{PLUGIN_MODULE_PREFIX}{pid}"
            plugin_path = str(d)
            try:
                spec = importlib.util.spec_from_file_location(mod_name, entry)
                if spec is None or spec.loader is None:
                    raise ImportError(f"无法为 {entry} 创建模块规格")
                module = importlib.util.module_from_spec(spec)
                # 插件目录常驻 sys.path：插件自带的库在钩子调用时也要 import 得到
                if plugin_path not in sys.path:
                    sys.path.insert(0, plugin_path)
                    record["sys_path"] = plugin_path
                sys.modules[mod_name] = module
                spec.loader.exec_module(module)
            except Exception:
                self._release_sys_path(record)
                self._log(f"[插件:{info.get('name')}] 入口执行失败:\n"
                          + traceback.format_exc())
                raise
            record["module"] = module
            for hook in HOOK_NAMES:
                fn = getattr(module, hook, None)
                if callable(fn):
                    record["hooks"][hook] = fn
            self.loaded[pid] = record
            self._call(pid, "on_load")
            self._log(f"[插件:{info.get('name')}] 已启用"
                      f"（v{info.get('version')}，"
                      f"{'含' if record['module'] else '无'} Python 逻辑）")
            return record

    def _release_sys_path(self, record) -> None:
        """把插件目录从 sys.path 摘掉（卸载/加载失败时调用）。"""
        added = (record or {}).get("sys_path")
        if not added:
            return
        try:
            sys.path.remove(added)
        except ValueError:
            pass
        record["sys_path"] = ""

    # ---------------------------------------------------------------- 卸载
    def unload_one(self, pid: str) -> bool:
        with self._lock:
            record = self.loaded.pop(pid, None)
        if not record:
            return False
        self._call_record(record, "on_unload")
        self._release_sys_path(record)
        mod_name = f"{PLUGIN_MODULE_PREFIX}{pid}"
        try:
            sys.modules.pop(mod_name, None)
        except Exception:
            pass
        return True

    def reload_all(self) -> dict:
        """停用后清理 + 重新加载。启用/禁用/安装/卸载后调用，实现热加载。"""
        with self._lock:
            for pid in list(self.loaded.keys()):
                try:
                    self.unload_one(pid)
                except Exception:
                    pass
        return self.load_all()

    # ---------------------------------------------------------------- 钩子
    def _call(self, pid: str, hook: str, *args):
        record = self.loaded.get(pid)
        if not record:
            return None
        return self._call_record(record, hook, *args)

    def _call_record(self, record, hook: str, *args):
        fn = (record.get("hooks") or {}).get(hook)
        if not callable(fn):
            return None
        name = (record.get("info") or {}).get("name") or "?"
        try:
            return fn(record["ctx"], *args) if hook != "on_load" else fn(record["ctx"])
        except Exception:
            self._log(f"[插件:{name}] {hook} 执行出错:\n" + traceback.format_exc())
            return None

    def dispatch_message(self, event) -> list:
        """把消息事件广播给所有插件的 on_message，收集非空返回值作为回复。"""
        replies = []
        for pid, record in list(self.loaded.items()):
            if "on_message" not in (record.get("hooks") or {}):
                continue
            try:
                out = self._call_record(record, "on_message", event)
            except Exception:
                continue
            if isinstance(out, str) and out.strip():
                replies.append(out.strip())
        return replies

    def dispatch_reply_done(self, info: dict) -> int:
        """通知所有插件「一轮 LLM 回复已经发完了」。

        info 是这一轮的结果快照，字段：
            session_type  "group" / "private"
            target_id     群号或用户号
            session_id    会话 ID
            sentences     已发出的句子列表（含 zh/display/emotion）
            emotions      当前角色情绪表
            role          当前角色配置
            reply         生成结果（含 llm_ms / tool_calls 等）

        插件可以借此追加消息、记录统计或做任何后处理；返回值被忽略，
        异常一律隔离。返回成功回调的插件数量，便于日志排查。
        """
        done = 0
        for pid, record in list(self.loaded.items()):
            if "on_reply_done" not in (record.get("hooks") or {}):
                continue
            self._call_record(record, "on_reply_done", info)
            done += 1
        return done

    def dispatch_command(self, name: str, args, event):
        """把一条指令交给注册了它的插件。返回第一个非 None 的结果。

        两条路径：
          1. 通过 ctx.register_command("名字", fn) 注册的具名处理器（推荐）；
          2. 插件定义了模块级 on_command(ctx, name, args, event) 作为兜底分发。
        具名处理器优先，避免一个插件的兜底钩子抢走别的插件的指令。
        """
        name = str(name or "").strip().lstrip("/").lstrip("#")
        for pid, record in list(self.loaded.items()):
            ctx = record.get("ctx")
            handler = getattr(ctx, "commands", {}).get(name) if ctx else None
            if not callable(handler):
                continue
            plugin_name = (record.get("info") or {}).get("name") or pid
            try:
                return handler(ctx, name, args, event)
            except Exception:
                self._log(f"[插件:{plugin_name}] 指令 {name} 执行出错:\n"
                          + traceback.format_exc())
                return None
        # 没有具名处理器，再看有没有兜底的 on_command 钩子
        for pid, record in list(self.loaded.items()):
            if "on_command" not in (record.get("hooks") or {}):
                continue
            out = self._call_record(record, "on_command", name, args, event)
            if out is not None:
                return out
        return None

    def command_names(self) -> list:
        names = []
        for record in self.loaded.values():
            ctx = record.get("ctx")
            if ctx:
                names.extend(getattr(ctx, "commands", {}).keys())
        return sorted(set(names))

    def unload_all(self) -> None:
        with self._lock:
            for pid in list(self.loaded.keys()):
                try:
                    self.unload_one(pid)
                except Exception:
                    pass
