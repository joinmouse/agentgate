from agentgate.config import load_config
from agentgate.metering import RateLimiter, cost_usd

BASE = """
[gateway]
[[keys]]
key = "k"
name = "k"
[[providers]]
name = "p1"
type = "mock"
models = ["kimi-k2.6"]
[pricing]
"kimi-k2.6" = [0.60, 2.50]
[routes]
auto = ["p1/kimi-k2.6"]
"""


def test_cost_calculation(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(BASE, encoding="utf-8")
    cfg = load_config(p)
    # 1M prompt + 1M completion = 0.60 + 2.50
    assert abs(cost_usd(cfg, "kimi-k2.6", 1_000_000, 1_000_000) - 3.10) < 1e-9
    # unknown model -> zero cost, never crash
    assert cost_usd(cfg, "unknown", 100, 100) == 0.0


def test_rate_limiter_window():
    rl = RateLimiter(window_s=60.0)
    assert rl.allow("k", 2) is True
    assert rl.allow("k", 2) is True
    assert rl.allow("k", 2) is False  # third hit within window blocked
    assert rl.allow("other", 2) is True  # per-key isolation
