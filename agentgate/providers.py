"""Provider adapters. A provider translates our canonical call into a
vendor-specific API and normalizes the response back to OpenAI schema."""
from __future__ import annotations

import hashlib
import random
import time
from typing import Any, Protocol

import httpx

from .config import ProviderConfig


class ProviderError(Exception):
    """Raised when a provider call fails (network, 5xx, timeout)."""


class Provider(Protocol):
    name: str

    def chat(self, model: str, messages: list[dict], **params: Any) -> dict: ...


def estimate_tokens(text: str) -> int:
    """Rough heuristic: ~4 chars per token. Good enough for metering demo;
    real deployments should use the provider's usage field or a tokenizer."""
    return max(1, len(text) // 4)


class MockProvider:
    """Deterministic in-process provider for dev, tests and demos.

    Simulates latency, failures and token usage without any network access —
    this is what makes the whole gateway runnable with zero external deps.
    """

    def __init__(self, cfg: ProviderConfig, seed: int | None = None):
        self.name = cfg.name
        self.cfg = cfg
        self._rng = random.Random(seed)

    def chat(self, model: str, messages: list[dict], **params: Any) -> dict:
        if self._rng.random() < self.cfg.fail_rate:
            raise ProviderError(f"{self.name}: simulated provider failure")
        # simulated latency (skipped in tests via latency_ms=0)
        if self.cfg.latency_ms:
            time.sleep(self.cfg.latency_ms / 1000.0)
        prompt_text = "\n".join(str(m.get("content", "")) for m in messages)
        digest = hashlib.sha256(prompt_text.encode()).hexdigest()[:8]
        last_user = next(
            (str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        reply = f"[{self.name}/{model}] reply #{digest}: {last_user[:120]!r} received."
        pt = estimate_tokens(prompt_text)
        ct = estimate_tokens(reply)
        return {
            "id": f"chatcmpl-mock-{digest}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": reply},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "total_tokens": pt + ct,
            },
        }


class OpenAICompatibleProvider:
    """Any vendor exposing POST {base_url}/chat/completions in OpenAI schema
    (Moonshot/Kimi, OpenAI, DeepSeek, Qwen, vLLM, ...). Protocol translation
    for non-conforming vendors would live in a sibling adapter class."""

    def __init__(self, cfg: ProviderConfig):
        self.name = cfg.name
        self.cfg = cfg
        self._client = httpx.Client(timeout=cfg.timeout_s)

    def chat(self, model: str, messages: list[dict], **params: Any) -> dict:
        body = {"model": model, "messages": messages, **params}
        try:
            resp = self._client.post(
                f"{self.cfg.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.cfg.api_key}"},
                json=body,
            )
        except httpx.TimeoutException as e:
            raise ProviderError(f"{self.name}: timeout after {self.cfg.timeout_s}s") from e
        except httpx.HTTPError as e:
            raise ProviderError(f"{self.name}: network error: {e}") from e
        if resp.status_code >= 400:
            raise ProviderError(f"{self.name}: HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()


def build_provider(cfg: ProviderConfig, seed: int | None = None) -> Provider:
    if cfg.type == "mock":
        return MockProvider(cfg, seed=seed)
    if cfg.type == "openai":
        return OpenAICompatibleProvider(cfg)
    raise ValueError(f"unknown provider type: {cfg.type}")
