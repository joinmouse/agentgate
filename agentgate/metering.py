"""Cost metering, per-key daily token quota, and sliding-window rate limiting.

Reasoning models burn hidden reasoning tokens at several times the visible
output — without quotas, queues and degradation policies, cost explodes.
This module is the enforcement point.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from .config import Config


def cost_usd(config: Config, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    pp, cp = config.pricing.get(model, (0.0, 0.0))
    return (prompt_tokens * pp + completion_tokens * cp) / 1_000_000


class RateLimiter:
    """Per-key sliding window over `window_s` seconds."""

    def __init__(self, window_s: float = 60.0):
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int) -> bool:
        now = time.monotonic()
        with self._lock:
            dq = self._hits[key]
            while dq and now - dq[0] > self.window_s:
                dq.popleft()
            if len(dq) >= limit:
                return False
            dq.append(now)
            return True


class QuotaExceeded(Exception):
    def __init__(self, used: int, quota: int):
        self.used, self.quota = used, quota
        super().__init__(f"daily token quota exceeded: {used}/{quota}")


class RateLimited(Exception):
    pass
