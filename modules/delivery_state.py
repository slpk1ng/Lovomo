"""Bounded metadata for recent sends, without targets or content."""
from collections import OrderedDict
from contextvars import ContextVar
from threading import Lock
from time import monotonic, time
from uuid import uuid4

active_trace = ContextVar("lovomo_message_trace", default=None)


def record_stage(stage, status, **fields):
    trace = active_trace.get()
    if trace is not None:
        trace[0].record(trace[1], stage, status, **fields)


async def trace_voice(operation):
    started = monotonic()
    record_stage("voice", "started")
    try:
        result = await operation
    except BaseException:
        record_stage("voice", "interrupted", duration_ms=round((monotonic() - started) * 1000))
        raise
    record_stage("voice", "ready" if result else "no_audio", duration_ms=round((monotonic() - started) * 1000))
    return result


class DeliveryState:
    def __init__(self, capacity=200):
        self.capacity = max(1, capacity)
        self.items = OrderedDict()
        self.traces = OrderedDict()
        self.lock = Lock()

    def receive(self, platform):
        identifier = uuid4().hex[:12]
        with self.lock:
            self.traces[identifier] = {"id": identifier, "platform": platform, "received_at": time(),
                                       "started": monotonic(), "steps": []}
            self._record(identifier, "message", "received")
            if len(self.traces) > self.capacity:
                self.traces.popitem(last=False)
        return identifier

    def _record(self, identifier, stage, status, **fields):
        trace = self.traces.get(identifier)
        if trace is None:
            return
        elapsed = round((monotonic() - trace["started"]) * 1000)
        step = {"stage": stage, "status": status, "elapsed_ms": elapsed, **fields}
        if stage == "message" and status == "processing":
            trace.setdefault("queue_ms", elapsed)
        if stage == "model" and status == "started":
            trace["model_started_ms"] = elapsed
        if stage == "model" and status in {"first_sentence", "ready"} and "model_started_ms" in trace:
            trace.setdefault("inference_ms", elapsed - trace["model_started_ms"])
        if stage == "voice" and status == "ready" and "duration_ms" in fields:
            trace.setdefault("synthesis_ms", fields["duration_ms"])
        if stage == "delivery" and status in {"sending", "retrying"}:
            for kind in (fields.get("kind") or "").split("+"):
                if kind in {"text", "voice"}:
                    trace.setdefault(f"first_{kind}_ms", elapsed)
        if stage == "delivery" and status == "acknowledged" and "duration_ms" in fields:
            for kind in (fields.get("kind") or "").split("+"):
                if kind in {"text", "voice"}:
                    trace.setdefault(f"send_{kind}_ms", fields["duration_ms"])
        steps = trace["steps"]
        if steps and all(steps[-1].get(key) == value for key, value in step.items() if key != "elapsed_ms"):
            return
        steps.append(step)
        if len(steps) > 48:
            # Keep the reception and the latest progress on long streamed replies.
            del steps[1]
            trace["truncated"] = True

    def record(self, identifier, stage, status, **fields):
        with self.lock:
            self._record(identifier, stage, status, **fields)

    def finish(self, identifier):
        with self.lock:
            trace = self.traces.get(identifier)
            if trace and not any(step["stage"] in {"model", "delivery"} for step in trace["steps"]):
                self._record(identifier, "message", "no_reply")

    def start(self, platform, kind=None, *, dispatch=True):
        identifier = uuid4().hex[:12]
        context = active_trace.get()
        parent = context[1] if context is not None and context[0] is self else None
        with self.lock:
            self.items[identifier] = {"id": identifier, "platform": platform, "status": "sending",
                                      "attempt": 0, "checked_at": time(), "attempt_started": monotonic()}
            if parent:
                self.items[identifier].update(trace_id=parent, kind=kind)
                if dispatch:
                    self._record(parent, "delivery", "sending", kind=kind, delivery_id=identifier)
            if len(self.items) > self.capacity:
                self.items.popitem(last=False)
        return identifier

    def mark(self, identifier, status, *, attempt=None, reason=None):
        with self.lock:
            item = self.items.get(identifier)
            if item is None:
                return
            if status in {"sending", "retrying"}:
                item["attempt_started"] = monotonic()
            item.update(status=status, checked_at=time())
            if attempt is not None:
                item["attempt"] = attempt
            if reason is None:
                item.pop("reason", None)
            else:
                item["reason"] = reason
            if item.get("trace_id"):
                details = {"kind": item.get("kind"), "delivery_id": identifier}
                if status in {"acknowledged", "unconfirmed", "failed", "waiting"}:
                    details["duration_ms"] = round((monotonic() - item["attempt_started"]) * 1000)
                if reason:
                    details["reason"] = reason
                self._record(item["trace_id"], "delivery", status, **details)

    def performance(self):
        keys = ("first_text_ms", "first_voice_ms", "queue_ms", "inference_ms", "synthesis_ms",
                "send_text_ms", "send_voice_ms")
        with self.lock:
            samples = {key: [row[key] for row in self.traces.values() if key in row] for key in keys}
        return {key: {"avg": round(sum(values) / len(values)), "count": len(values)}
                for key, values in samples.items() if values}

    def snapshot(self):
        with self.lock:
            recent = list(reversed(self.items.values()))
            counts = {}
            for item in recent:
                status = item["status"]
                counts[status] = counts.get(status, 0) + 1
            activity = []
            for trace in list(reversed(self.traces.values()))[:20]:
                activity.append({key: [step.copy() for step in value] if key == "steps" else value
                                 for key, value in trace.items() if key not in {"started", "model_started_ms"}})
            return {"counts": counts, "recent": [{k: v for k, v in item.items() if k != "attempt_started"}
                                                 for item in recent[:20]], "activity": activity}
