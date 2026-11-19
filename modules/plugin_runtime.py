
import sys
import threading
import traceback
import copy
import re
from functools import wraps
from types import MappingProxyType
from pathlib import Path

# Keep system/runtime paths available without passing inherited service credentials.
_WORKER_ENV_KEYS = frozenset({"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE",
                              "COMSPEC", "TEMP", "TMP", "TMPDIR", "LOCALAPPDATA", "APPDATA",
                              "USERPROFILE", "HOME", "LANG", "LC_ALL", "TZ"})


def worker_environment(environment):
    return {key: value for key, value in environment.items() if key.upper() in _WORKER_ENV_KEYS}


PLUGIN_MODULE_PREFIX = "lovomo_plugin_"
HOOK_NAMES = ("on_load", "on_unload", "on_message", "on_command", "on_reply_done")


def _serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return call


def config_projection(value):
    """Detach plugin-visible state and omit credentials recursively."""
    from .security import _SECRET_FIELD_KEYS, _CONNECTION_SECRET_KEYS
    secrets = set(_SECRET_FIELD_KEYS) | set(_CONNECTION_SECRET_KEYS)
    def clean(node):
        if isinstance(node, dict):
            return {key: clean(item) for key, item in node.items()
                    if key not in secrets and not re.search(
                        r"password|secret|api_key|(?:^|_)token(?:$|_)|(?:^|_)access_key(?:$|_)",
                        str(key).lower())}
        if isinstance(node, (list, tuple)):
            return [clean(item) for item in node]
        return copy.deepcopy(node)
    return clean(value) if isinstance(value, dict) else {}


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


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
                return _freeze(config_projection(cfg))
            except Exception:
                return {}
        return {}

    @property
    def emotions(self) -> dict:
        if callable(self._emotions_getter):
            try:
                table = self._emotions_getter()
                return _freeze(copy.deepcopy(table)) if isinstance(table, dict) else {}
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


class WorkerConnection:
    def __init__(self, proc):
        import queue
        import json
        self.proc, self.queue, self.pending = proc, queue.Queue(maxsize=2), None
        self.json = json
        def read():
            try:
                while True:
                    line = proc.stdout.readline(2 * 1024 * 1024 + 1)
                    if not line:
                        self.queue.put(EOFError())
                        return
                    if len(line) > 2 * 1024 * 1024 or not line.endswith(b"\n"):
                        self.queue.put(ValueError("插件响应过大或格式非法"))
                        return
                    self.queue.put(json.loads(line))
            except Exception as e:
                self.queue.put(e)
        threading.Thread(target=read, daemon=True).start()

    def send(self, value, timeout=5):
        data = self.json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("插件请求过大")
        complete = threading.Event()
        failures = []
        def write():
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except Exception as e:
                failures.append(e)
            finally:
                complete.set()
        threading.Thread(target=write, daemon=True).start()
        if not complete.wait(timeout):
            raise TimeoutError("插件读取请求超时")
        if failures:
            raise failures[0]

    def poll(self, timeout):
        import queue
        try:
            self.pending = self.queue.get(timeout=timeout)
            return True
        except queue.Empty:
            return False

    def recv(self):
        value, self.pending = self.pending, None
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self):
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()
            except OSError:
                pass


class PluginRuntime:
    """Trusted plugins execute in bounded, independently reclaimable workers."""
    def __init__(self, manager, logger=None, config_getter=None, sender=None,
                 voice_sender=None, emotions_getter=None):
        self.manager, self._logger = manager, logger
        self._config_getter, self._sender = config_getter, sender
        self._voice_sender, self._emotions_getter = voice_sender, emotions_getter
        self.loaded = {}
        self._lock = threading.RLock()

    def _log(self, text):
        if callable(self._logger):
            self._logger(text)
        else:
            print(text)

    def _snapshot(self):
        config = self._config_getter() if callable(self._config_getter) else {}
        emotions = self._emotions_getter() if callable(self._emotions_getter) else {}
        return {"config": config_projection(config), "emotions": copy.deepcopy(emotions or {})}

    def _timeout(self):
        from .runtime_policy import limit
        config = self._config_getter() if callable(self._config_getter) else {}
        return limit(config, "plugin_callback_timeout_seconds")

    def _stop_record(self, record):
        from . import process_lifecycle
        proc = record.get("process")
        if proc is not None:
            process_lifecycle.stop_tree(proc, timeout=0.5)
            try:
                proc.wait(timeout=0.5)
            except Exception:
                pass
        connection = record.get("connection")
        if connection is not None:
            connection.close()

    @_serialized
    def _invoke(self, record, hook, *args):
        if not record.get("process"):
            return None
        with record["lock"]:
            pid = record["info"]["id"]
            if not self.manager.is_trusted(pid):
                self._stop_record(record)
                self.loaded.pop(pid, None)
                raise ValueError("插件内容发生变化，需要重新确认信任")
            connection = record["connection"]
            try:
                import time
                deadline = time.monotonic() + self._timeout()
                connection.send({"hook": hook, "args": args, "snapshot": self._snapshot()},
                                timeout=max(0, deadline - time.monotonic()))
                if not connection.poll(max(0, deadline - time.monotonic())):
                    raise TimeoutError("插件调用超时，已关闭该插件及其子进程")
                reply = connection.recv()
            except (EOFError, BrokenPipeError, OSError, TimeoutError, ValueError) as e:
                self._stop_record(record)
                self.loaded.pop(pid, None)
                raise RuntimeError(str(e)) from e
            previous = record["ctx"].commands
            for name in reply.get("commands", []):
                if name not in previous:
                    owner = self.command_owner(name, pid)
                    if owner:
                        self._log(f"[插件:{pid}] 指令 {name} 已由插件「{owner}」注册，本次注册不会生效。")
            record["hooks"] = {name: True for name in reply.get("hooks", [])}
            record["ctx"].commands = {name: True for name in reply.get("commands", [])}
            can_send = self.manager.message_granted(pid)
            for kind, values in reply.get("events", [])[:100]:
                if kind in ("send", "voice") and not can_send:
                    self._log(f"[插件:{pid}] 消息发送未授权，请在插件详情修改权限")
                    continue
                if kind == "log":
                    self._log(str(values[0]))
                elif kind == "send" and callable(self._sender):
                    try:
                        self._sender(*values)
                    except TypeError:
                        self._sender(*values[1:])
                elif kind == "voice" and callable(self._voice_sender):
                    self._voice_sender(*values)
            if reply.get("error"):
                if hook == "__load__":
                    raise RuntimeError(reply["error"])
                self._log(f"[插件:{pid}] {hook} 执行出错：{reply['error']}")
                return None
            return reply.get("result")

    @_serialized
    def load_all(self):
        errors = {}
        for info in self.manager.list_plugins():
            if not info.get("enabled"):
                continue
            try:
                self.load_one(info)
            except Exception as e:
                errors[info["id"]] = f"{type(e).__name__}: {e}"
                self._log(f"[插件:{info.get('name')}] 加载失败：{e}")
        return errors

    @_serialized
    def load_one(self, info):
        from . import process_lifecycle
        import subprocess
        import os
        pid = info["id"]
        if not self.manager.is_trusted(pid):
            raise ValueError("插件尚未获得当前内容版本的信任授权")
        self.unload_one(pid)
        ctx = LovomoContext(pid, info.get("name") or pid, self.manager,
                            logger=self._logger, config_getter=self._config_getter,
                            sender=self._sender, voice_sender=self._voice_sender,
                            emotions_getter=self._emotions_getter, runtime=self)
        record = {"info": dict(info), "ctx": ctx, "hooks": {}, "lock": threading.RLock(),
                  "module": None, "process": None, "connection": None, "sys_path": ""}
        folder = Path(info["dir"]).resolve()
        entry = (folder / (info.get("entry") or "main.py")).resolve()
        if folder not in entry.parents:
            raise ValueError("插件入口路径非法")
        self.loaded[pid] = record
        if not entry.is_file():
            return record
        try:
            env = worker_environment(os.environ)
            if getattr(sys, "frozen", False):
                command = [sys.executable, "--lovomo-plugin-worker"]
            else:
                command = [process_lifecycle.python_child_executable(env), "-m", "modules.plugin_worker"]
            if os.name == "nt":
                command.append("--report-worker-pid")
            proc = process_lifecycle.popen(command, cwd=str(Path(__file__).resolve().parent.parent),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=env, job_restrictions=(4, 768 * 1024**2),
                creationflags=0x08000000 if os.name == "nt" else 0)
            record["process"] = proc
            parent = WorkerConnection(proc)
            record["connection"] = parent
            if os.name == "nt":
                if not parent.poll(5):
                    raise TimeoutError("插件进程启动超时")
                ready = parent.recv()
                if not isinstance(ready, dict) or not process_lifecycle.command_job_contains(proc, ready.get("pid")):
                    raise RuntimeError("插件进程未纳入资源限制，已停止启动")
            parent.send({"info": dict(info), "root": str(self.manager.root.resolve())})
            self._invoke(record, "__load__")
        except BaseException:
            self._stop_record(record)
            self.loaded.pop(pid, None)
            raise
        self._log(f"[插件:{info.get('name')}] 已启用（独立进程 PID {proc.pid}）")
        return record

    @_serialized
    def unload_one(self, pid):
        record = self.loaded.pop(pid, None)
        if record is None:
            return False
        try:
            self._invoke(record, "on_unload")
        except Exception as e:
            self._log(f"[插件:{pid}] 停用清理：{e}")
        finally:
            self._stop_record(record)
        return True

    @_serialized
    def unload_all(self):
        for pid in list(self.loaded):
            self.unload_one(pid)

    @_serialized
    def reload_all(self):
        self.unload_all()
        return self.load_all()

    @_serialized
    def reload_one(self, pid):
        info = self.manager.get(pid)
        if info and info.get("enabled") and self.manager.is_trusted(pid):
            return self.load_one(info)
        self.unload_one(pid)
        return None

    def _call_record(self, record, hook, *args):
        try:
            return self._invoke(record, hook, *args)
        except Exception as e:
            self._log(f"[插件:{record['info']['id']}] {hook}：{e}")
            return None

    def _call(self, pid, hook, *args):
        record = self.loaded.get(pid)
        return self._call_record(record, hook, *args) if record else None

    @_serialized
    def dispatch_message(self, event):
        replies = []
        for record in list(self.loaded.values()):
            if "on_message" in record["hooks"]:
                result = self._call_record(record, "on_message", copy.deepcopy(event))
                if isinstance(result, str) and result.strip():
                    replies.append(result.strip())
        return replies

    @_serialized
    def dispatch_reply_done(self, info):
        done = 0
        for record in list(self.loaded.values()):
            if "on_reply_done" in record["hooks"]:
                self._call_record(record, "on_reply_done", copy.deepcopy(info))
                done += 1
        return done

    @_serialized
    def command_owner(self, name, exclude_pid=""):
        name = str(name or "").strip().lstrip("/#")
        for pid, record in list(self.loaded.items()):
            if pid != exclude_pid and name in record["ctx"].commands:
                return str(record["info"].get("name") or pid)
        return ""

    @_serialized
    def dispatch_command(self, name, args, event):
        name = str(name or "").strip().lstrip("/#")
        for record in list(self.loaded.values()):
            if name in record["ctx"].commands:
                return self._call_record(record, "__command__", name, args, copy.deepcopy(event))
        for record in list(self.loaded.values()):
            if "on_command" in record["hooks"]:
                out = self._call_record(record, "on_command", name, args, copy.deepcopy(event))
                if out is not None:
                    return out
        return None

    @_serialized
    def command_names(self):
        return sorted({name for record in self.loaded.values() for name in record["ctx"].commands})
