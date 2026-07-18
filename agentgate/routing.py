"""Cost/latency-aware routing with fallback chains and a simple circuit breaker.

Not every task deserves the strongest (and priciest) model: aliases like
`cheap`/`smart`/`auto` map to ordered candidate chains of "provider/model".
Candidates are tried in ascending prompt-cost order, skipping providers whose
circuit breaker is open.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .config import Config


@dataclass
class Candidate:
    provider: str
    model: str
    prompt_price: float  # usd per 1M tokens (unknown model -> +inf, deprioritized)
    completion_price: float


@dataclass
class RouteDecision:
    alias: str
    candidates: list[Candidate]
    reason: str = ""
    attempts: list[dict] = field(default_factory=list)


class CircuitBreaker:
    """Opens a provider for `cooldown_s` after `threshold` consecutive failures."""

    def __init__(self, threshold: int = 3, cooldown_s: float = 60.0):
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._fails: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}

    def is_open(self, provider: str) -> bool:
        opened = self._opened_at.get(provider)
        if opened is None:
            return False
        if time.monotonic() - opened > self.cooldown_s:
            self.reset(provider)
            return False
        return True

    def record_success(self, provider: str) -> None:
        self.reset(provider)

    def record_failure(self, provider: str) -> None:
        n = self._fails.get(provider, 0) + 1
        self._fails[provider] = n
        if n >= self.threshold:
            self._opened_at[provider] = time.monotonic()

    def reset(self, provider: str) -> None:
        self._fails.pop(provider, None)
        self._opened_at.pop(provider, None)


class Router:
    def __init__(self, config: Config, breaker: CircuitBreaker | None = None):
        self.config = config
        self.breaker = breaker or CircuitBreaker()

    def resolve(self, alias: str) -> RouteDecision:
        chain = self.config.routes.get(alias)
        if not chain:
            # allow direct "provider/model" or bare model name pass-through
            chain = [alias] if "/" in alias else self._by_model_name(alias)
        if not chain:
            raise KeyError(f"no route for model alias: {alias}")
        cands = []
        for item in chain:
            provider, model = item.split("/", 1)
            prices = self.config.pricing.get(model, (float("inf"), float("inf")))
            cands.append(Candidate(provider, model, prices[0], prices[1]))
        return RouteDecision(alias=alias, candidates=cands)

    def _by_model_name(self, model: str) -> list[str]:
        out = []
        for p in self.config.providers:
            if model in p.models:
                out.append(f"{p.name}/{model}")
        return out

    def order(self, decision: RouteDecision) -> list[Candidate]:
        """Cheapest-first among available; broken providers go last."""
        available, broken = [], []
        for c in decision.candidates:
            (broken if self.breaker.is_open(c.provider) else available).append(c)
        available.sort(key=lambda c: (c.prompt_price, c.completion_price))
        broken.sort(key=lambda c: (c.prompt_price, c.completion_price))
        decision.reason = (
            f"alias={decision.alias}; cheapest-first among {len(available)} healthy "
            f"provider(s), {len(broken)} circuit-broken"
        )
        return available + broken
