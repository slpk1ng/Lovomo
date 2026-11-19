"""A plugin runs in a disposable process; trust approval is still required.

This is a timeout and crash boundary, not an operating-system sandbox.
"""
import importlib.util
import traceback
import sys
import json
import contextlib
import os
from pathlib import Path


def worker_main(connection, info, root):
    from .plugin_runtime import LovomoContext, HOOK_NAMES
    from .plugins import PluginManager
    events = []
    snapshot = {"config": {}, "emotions": {}}
    manager = PluginManager(Path(root), Path(root) / "state.json")

    def emit(kind, *args):
        if len(events) >= 100:
            raise RuntimeError("插件单次调用产生的操作过多")
        events.append([kind, list(args)])
        return True

    ctx = LovomoContext(info["id"], info.get("name") or info["id"], manager,
                        logger=lambda line: emit("log", line),
                        config_getter=lambda: snapshot["config"],
                        emotions_getter=lambda: snapshot["emotions"],
                        sender=lambda *args: emit("send", *args),
                        voice_sender=lambda *args: emit("voice", *args))
    hooks = {}
    try:
        while True:
            request = connection.recv()
            events.clear()
            snapshot.update(request.get("snapshot") or {})
            hook, args = request["hook"], request.get("args") or []
            error, result = "", None
            try:
                if hook == "__load__":
                    folder = Path(info["dir"]).resolve()
                    entry = (folder / (info.get("entry") or "main.py")).resolve()
                    if folder not in entry.parents or not entry.is_file():
                        raise ValueError("插件入口路径非法")
                    sys.path.insert(0, str(folder))
                    spec = importlib.util.spec_from_file_location("lovomo_plugin_" + info["id"], entry)
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[spec.name] = module
                    spec.loader.exec_module(module)
                    hooks = {name: getattr(module, name) for name in HOOK_NAMES
                             if callable(getattr(module, name, None))}
                    fn = hooks.get("on_load")
                    if fn:
                        try:
                            fn(ctx)
                        except Exception:
                            emit("log", "on_load 执行出错：" + traceback.format_exc())
                elif hook == "__command__":
                    fn = ctx.commands.get(args[0])
                    if fn:
                        result = fn(ctx, *args)
                else:
                    fn = hooks.get(hook)
                    if fn:
                        result = fn(ctx, *args)
            except Exception:
                error = traceback.format_exc()
            connection.send({"result": result, "error": error, "events": list(events),
                             "commands": list(ctx.commands), "hooks": list(hooks)})
            if hook == "on_unload":
                break
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        connection.close()


def _binary_stdio(name, standard_id, mode):
    stream = getattr(sys, name, None)
    if stream is not None:
        return stream.buffer, False
    if os.name != "nt":
        raise RuntimeError(f"插件通信缺少 {name}")
    # Windowed executables have no Python standard streams, but Popen still
    # supplies inherited pipe handles. Duplicate those handles before wrapping.
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE,
        wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
        wintypes.BOOL, wintypes.DWORD]
    kernel.DuplicateHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.GetStdHandle(standard_id)
    duplicate = wintypes.HANDLE()
    current = kernel.GetCurrentProcess()
    if not handle or handle == wintypes.HANDLE(-1).value or not kernel.DuplicateHandle(
            current, handle, current, ctypes.byref(duplicate), 0, False, 2):
        raise OSError(ctypes.get_last_error(), f"插件通信无法取得 {name} 管道")
    try:
        flags = os.O_BINARY | (os.O_RDONLY if mode == "rb" else os.O_WRONLY)
        fd = msvcrt.open_osfhandle(duplicate.value, flags)
    except BaseException:
        kernel.CloseHandle(duplicate)
        raise
    return os.fdopen(fd, mode, buffering=0), True


def run_stdio():
    with contextlib.ExitStack() as resources:
        incoming, own_in = _binary_stdio("stdin", -10, "rb")
        if own_in:
            resources.callback(incoming.close)
        outgoing, own_out = _binary_stdio("stdout", -11, "wb")
        if own_out:
            resources.callback(outgoing.close)
        errors = sys.stderr or resources.enter_context(open(os.devnull, "w", encoding="utf-8"))
        _run_connection(incoming, outgoing, errors)


def _run_connection(incoming, outgoing, errors):

    class Connection:
        def recv(self):
            line = incoming.readline(2 * 1024 * 1024 + 1)
            if not line:
                raise EOFError()
            if len(line) > 2 * 1024 * 1024 or not line.endswith(b"\n"):
                raise ValueError("插件通信消息过大")
            return json.loads(line)

        def send(self, value):
            data = json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n"
            if len(data) > 2 * 1024 * 1024:
                raise ValueError("插件响应过大")
            outgoing.write(data)
            outgoing.flush()

        def close(self):
            pass

    connection = Connection()
    if "--report-worker-pid" in sys.argv:
        connection.send({"pid": os.getpid()})
    initial = connection.recv()
    with contextlib.redirect_stdout(errors):
        worker_main(connection, initial["info"], initial["root"])


if __name__ == "__main__":
    run_stdio()
