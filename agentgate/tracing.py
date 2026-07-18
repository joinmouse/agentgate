"""OTel-style tracing: one trace per request, spans for each phase
(gateway.receive -> route.decide -> provider.call -> gateway.respond).

Failure attribution — which phase actually failed — is recorded on the trace.
Traces tell you *what* happened; attribution tells you *why* it broke, and
aggregating it across traces is what most observability platforms still miss.
"""
from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from .storage import Storage

STAGES = ("auth", "quota", "ratelimit", "route", "provider", "timeout")


class Span:
    def __init__(self, recorder: "Recorder", trace_id: str, name: str):
        self.recorder, self.trace_id, self.name = recorder, trace_id, name
        self.start_ts = time.time()
        self.status = "ok"
        self.attrs: dict[str, Any] = {}

    def finish(self) -> None:
        self.recorder.storage.insert_span(
            self.trace_id, self.name, self.start_ts, time.time(), self.status, self.attrs
        )


class Recorder:
    def __init__(self, storage: Storage):
        self.storage = storage

    def new_trace_id(self) -> str:
        return uuid.uuid4().hex

    @contextmanager
    def span(self, trace_id: str, name: str) -> Iterator[Span]:
        sp = Span(self, trace_id, name)
        try:
            yield sp
        except Exception:
            sp.status = "error"
            raise
        finally:
            sp.finish()

    def finish_trace(self, trace_id: str, *, ts: float, key_name: str | None,
                     alias: str | None, status: str, latency_ms: float,
                     failure_stage: str | None = None, provider: str | None = None,
                     model: str | None = None, route_reason: str | None = None) -> None:
        if failure_stage is not None and failure_stage not in STAGES:
            raise ValueError(f"invalid failure stage: {failure_stage}")
        self.storage.insert_trace(
            {
                "trace_id": trace_id, "ts": ts, "key_name": key_name, "alias": alias,
                "status": status, "failure_stage": failure_stage,
                "latency_ms": round(latency_ms, 2), "provider": provider,
                "model": model, "route_reason": route_reason,
            }
        )
