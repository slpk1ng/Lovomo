"""Release model instances loaded by Lovomo on a shared local server."""
import threading
import time
from urllib.parse import urlsplit

import httpx

from .tls import verified_context


def _ollama_name(name):
    return name if ':' in name.rsplit('/', 1)[-1] else f'{name}:latest'


class LocalModelLifecycle:
    def __init__(self):
        self._condition = threading.Condition()
        self._closing = False
        self._loading = set()
        self._owned = {}
        self._closed = threading.Event()

    @staticmethod
    def _request(service, root, api_key, path, payload=None, timeout=1.5):
        headers = {"Authorization": f"Bearer {api_key}"} if api_key and service == "lmstudio" else {}
        with httpx.Client(timeout=timeout, trust_env=False, verify=verified_context(), headers=headers) as client:
            response = client.get(root + path) if payload is None else client.post(root + path, json=payload)
            response.raise_for_status()
            return response.json()

    def _instances(self, service, root, api_key, model):
        try:
            if service == "lmstudio":
                data = self._request(service, root, api_key, "/api/v1/models")
                instances = set()
                for entry in data["models"]:
                    for item in entry.get("loaded_instances", []):
                        instance = item.get("id")
                        if instance and (entry.get("key") == model or instance == model):
                            instances.add(instance)
                return instances
            data = self._request(service, root, api_key, "/api/ps")
            names = (entry.get("name") or entry.get("model") for entry in data["models"])
            return {name for name in names if name and _ollama_name(name) == _ollama_name(model)}
        except (httpx.HTTPError, OSError, TypeError, ValueError, AttributeError, KeyError):
            return None

    def load(self, service, base_url, api_key, model, load):
        parsed = urlsplit(base_url)
        if parsed.scheme not in ('http', 'https'):
            return load()
        root = f"{parsed.scheme}://{parsed.netloc}"
        name = _ollama_name(model) if service == "ollama" else model
        target = (service, root, name)
        with self._condition:
            while target in self._loading and not self._closing:
                self._condition.wait()
            if self._closing:
                return False
            self._loading.add(target)
        try:
            before = self._instances(service, root, api_key, name)
            if before:
                return True
            with self._condition:
                if self._closing:
                    return False
            try:
                return load()
            finally:
                # A failed or interrupted response can still leave a loaded instance.
                if before is not None:
                    for instance in self._instances(service, root, api_key, name) or ():
                        with self._condition:
                            self._owned[(service, root, instance)] = api_key
        finally:
            with self._condition:
                self._loading.discard(target)
                self._condition.notify_all()

    def shutdown(self, budget=8.0):
        deadline = time.monotonic() + budget
        with self._condition:
            already_closing = self._closing
            self._closing = True
            self._condition.notify_all()
        if already_closing:
            self._closed.wait(max(0.0, deadline - time.monotonic()))
            return
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    with self._condition:
                        pending = len(self._loading) + len(self._owned)
                    if pending:
                        print(f"[模型退出] 清理超时，仍有 {pending} 项未确认卸载。")
                    return
                with self._condition:
                    if self._owned:
                        (service, root, instance), api_key = self._owned.popitem()
                    elif self._loading:
                        self._condition.wait(remaining)
                        continue
                    else:
                        return
                path, payload = ("/api/v1/models/unload", {"instance_id": instance}) if service == "lmstudio" else (
                    "/api/generate", {"model": instance, "keep_alive": 0, "stream": False})
                try:
                    self._request(service, root, api_key, path, payload, timeout=min(1.5, remaining))
                    print(f"[模型退出] 已卸载 Lovomo 加载的本地模型：{instance}")
                except (httpx.HTTPError, OSError, ValueError) as error:
                    print(f"[模型退出] 无法确认模型 {instance} 已卸载：{type(error).__name__}")
        finally:
            self._closed.set()


local_models = LocalModelLifecycle()
