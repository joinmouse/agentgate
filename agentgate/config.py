"""Configuration loading (TOML, stdlib tomllib — zero extra deps)."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GatewayConfig:
    host: str = "127.0.0.1"
    port: int = 8090
    db_path: str = "agentgate.db"
    admin_key: str = "admin-dev-key"


@dataclass
class KeyConfig:
    key: str
    name: str
    daily_quota_tokens: int = 100_000
    rpm: int = 60


@dataclass
class ProviderConfig:
    name: str
    type: str  # "mock" | "openai"
    base_url: str = ""
    api_key: str = ""
    models: list[str] = field(default_factory=list)
    latency_ms: int = 100  # mock only: simulated latency
    fail_rate: float = 0.0  # mock only: simulated failure rate
    timeout_s: float = 30.0


@dataclass
class Config:
    gateway: GatewayConfig
    keys: list[KeyConfig]
    providers: list[ProviderConfig]
    # pricing: model -> (prompt_usd_per_1m, completion_usd_per_1m)
    pricing: dict[str, tuple[float, float]]
    # routes: alias -> ordered candidate list of "provider/model"
    routes: dict[str, list[str]]

    def key_map(self) -> dict[str, KeyConfig]:
        return {k.key: k for k in self.keys}

    def provider_map(self) -> dict[str, ProviderConfig]:
        return {p.name: p for p in self.providers}


def load_config(path: str | Path) -> Config:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    gw = raw.get("gateway", {})
    pricing_raw = raw.get("pricing", {})
    return Config(
        gateway=GatewayConfig(
            host=gw.get("host", "127.0.0.1"),
            port=int(gw.get("port", 8090)),
            db_path=gw.get("db_path", "agentgate.db"),
            admin_key=gw.get("admin_key", "admin-dev-key"),
        ),
        keys=[KeyConfig(**k) for k in raw.get("keys", [])],
        providers=[ProviderConfig(**p) for p in raw.get("providers", [])],
        pricing={m: (float(v[0]), float(v[1])) for m, v in pricing_raw.items()},
        routes={alias: list(chain) for alias, chain in raw.get("routes", {}).items()},
    )
