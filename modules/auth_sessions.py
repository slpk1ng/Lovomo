"""Independent browser sessions and session-bound secondary authorization."""
import hashlib
import json
import secrets
import threading
import time
from collections import deque
from pathlib import Path

from .jsonio import load_json_ex, save_json
from .security import _decrypt_value, _encrypt_value


class AuthRateLimiter:
    """Bound password guesses per remote address and endpoint."""
    def __init__(self, limit=5, window_seconds=900, lock_seconds=900, max_buckets=2048):
        self.limit = max(1, int(limit))
        self.window_seconds = max(1, int(window_seconds))
        self.lock_seconds = max(1, int(lock_seconds))
        self.max_buckets = max(32, int(max_buckets))
        self.lock = threading.Lock()
        self.buckets = {}

    def _prune(self, now):
        stale = [key for key, item in self.buckets.items()
                 if item["blocked_until"] <= now
                 and (not item["failures"] or item["failures"][-1] <= now - self.window_seconds)]
        for key in stale:
            self.buckets.pop(key, None)
        while len(self.buckets) > self.max_buckets:
            self.buckets.pop(next(iter(self.buckets)))

    def retry_after(self, key, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            item = self.buckets.get(str(key))
            if not item:
                return 0
            self._prune(now)
            item = self.buckets.get(str(key))
            if not item:
                return 0
            if item["blocked_until"] > now:
                return max(1, int(item["blocked_until"] - now + 0.999))
            while item["failures"] and item["failures"][0] <= now - self.window_seconds:
                item["failures"].popleft()
            return 0

    def failure(self, key, now=None):
        now = time.monotonic() if now is None else now
        key = str(key)
        with self.lock:
            self._prune(now)
            item = self.buckets.setdefault(key, {"failures": deque(), "blocked_until": 0.0})
            while item["failures"] and item["failures"][0] <= now - self.window_seconds:
                item["failures"].popleft()
            item["failures"].append(now)
            if len(item["failures"]) >= self.limit:
                item["blocked_until"] = now + self.lock_seconds
            self._prune(now)
            return max(1, self.lock_seconds) if item["blocked_until"] > now else 0

    def success(self, key):
        with self.lock:
            self.buckets.pop(str(key), None)


class AuthSessions:
    def __init__(self, path, password="", second_password=""):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.fingerprint = self._fingerprint(password, second_password)
        self.sessions = {}
        raw, _ = load_json_ex(self.path, {})
        # The old shared-token format is deliberately invalidated on upgrade.
        if isinstance(raw, dict) and raw.get("version") == 2:
            try:
                plain = _decrypt_value(raw.get("state", ""))
                state = json.loads(plain) if plain else {}
                if state.get("fingerprint") == self.fingerprint:
                    now = time.time()
                    self.sessions = {key: value for key, value in state.get("sessions", {}).items()
                                     if isinstance(value, dict) and value.get("expires", 0) > now}
            except (ValueError, TypeError, AttributeError):
                pass

    @staticmethod
    def _fingerprint(password, second):
        return hashlib.sha256(json.dumps([password, second]).encode()).hexdigest()

    @staticmethod
    def _key(token):
        return hashlib.sha256(str(token or "").encode()).hexdigest()

    def _save(self):
        # Do not persist secondary grants, including single-operation grants.
        state = {"fingerprint": self.fingerprint, "sessions": {
            key: {"expires": item["expires"], "primary": item["primary"]}
            for key, item in self.sessions.items() if item.get("remember")}}
        # Loaded remembered sessions keep their persistence across future saves.
        for item in state["sessions"].values():
            item["remember"] = True
        save_json(self.path, {"version": 2, "state": _encrypt_value(json.dumps(state))})

    def refresh(self, password, second):
        fingerprint = self._fingerprint(password, second)
        with self.lock:
            if fingerprint != self.fingerprint:
                self.fingerprint = fingerprint
                self.sessions.clear()
                self._save()

    def create(self, minutes=0, primary=True, replaces=None):
        with self.lock:
            now = time.time()
            previous = self.sessions
            updated = {k: v for k, v in previous.items() if v["expires"] > now}
            if replaces:
                updated.pop(self._key(replaces), None)
            if len(updated) >= 256:
                raise ValueError("登录会话过多，请稍后再试")
            token = secrets.token_urlsafe(32)
            updated[self._key(token)] = {
                "expires": now + (minutes * 60 if minutes > 0 else 12 * 3600),
                "primary": primary, "remember": minutes > 0}
            self.sessions = updated
            try:
                self._save()
            except Exception:
                self.sessions = previous
                raise
            return token

    def status(self, token):
        with self.lock:
            item = self.sessions.get(self._key(token))
            return dict(item) if item and item["expires"] > time.time() else None

    def grant(self, token, minutes):
        with self.lock:
            item = self.sessions.get(self._key(token))
            if not item or item["expires"] <= time.time():
                return False
            item["second_until"] = time.time() + minutes * 60 if minutes else 0
            item["second_once"] = minutes == 0
            return True

    def consume_second(self, token):
        with self.lock:
            item = self.sessions.get(self._key(token))
            if not item or item["expires"] <= time.time():
                return False
            if item.get("second_until", 0) > time.time():
                return True
            if item.get("second_once"):
                item["second_once"] = False
                return True
            return False
