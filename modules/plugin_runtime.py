
import importlib.util
import sys
import threading
import traceback
from pathlib import Path

PLUGIN_MODULE_PREFIX = "lovomo_plugin_"
HOOK_NAMES = ("on_load", "on_unload", "on_message", "on_command", "on_reply_done")


class LovomoContext:

    def __init__(self, plugin_id: str, plugin_name: str, manager,
                 logger=None, config_getter=None, sender=None,
                 voice_sender=None, emotions_getter=None, runtime=None):
        self.plugin_id = plugin_id
        self.plugin_name = plugin_name
        self.manager = manager
        self._logger = logger
        self._config_getter = config_getter
        self._sender = sender
        self._voice_sender = voice_sender
        self._emotions_getter = emotions_getter
        self._runtime = runtime
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
        if callable(self._emotions_getter):
            try:
                table = self._emotions_getter()
                return table if isinstance(table, dict) else {}
            except Exception:
                return {}
        return {}

    # --- 发消息 -------------------------------------------------------
    def send_message(self, group_id, text: str) -> bool:
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
        owner = self._command_owner(name)
        if owner:
            self.log(f"指令 {name} 已由插件「{owner}」注册，本次注册不会生效。")
        self.commands[name] = handler
        return True

    def _command_owner(self, name: str) -> str:
        if self._runtime is None:
            return ""
        try:
            return self._runtime.command_owner(name, self.plugin_id)
        except Exception:
            return ""

    # --- 数据目录 -----------------------------------------------------
    def data_dir(self) -> Path:
        d = Path(self.manager.root) / self.plugin_id / "data"
        d.mkdir(parents=True, exist_ok=True)
        return d


class PluginRuntime:

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
                                emotions_getter=self._emotions_getter,
                                runtime=self)
            record = {"info": info, "module": None, "ctx": ctx, "hooks": {},
                      "sys_path": ""}
            if not entry.is_file():
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
        done = 0
        for pid, record in list(self.loaded.items()):
            if "on_reply_done" not in (record.get("hooks") or {}):
                continue
            self._call_record(record, "on_reply_done", info)
            done += 1
        return done

    def command_owner(self, name: str, exclude_pid: str = "") -> str:
        name = str(name or "").strip().lstrip("/").lstrip("#")
        if not name:
            return ""
        for pid, record in list(self.loaded.items()):
            if pid == exclude_pid:
                continue
            ctx = record.get("ctx")
            if ctx is not None and callable(getattr(ctx, "commands", {}).get(name)):
                return str((record.get("info") or {}).get("name") or pid)
        return ""

    def dispatch_command(self, name: str, args, event):
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
