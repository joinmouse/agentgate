from agentgate.config import load_config
from agentgate.routing import CircuitBreaker, Router


def make_router(tmp_path, toml: str) -> Router:
    p = tmp_path / "c.toml"
    p.write_text(toml, encoding="utf-8")
    return Router(load_config(p))

BASE = """
[gateway]
[[keys]]
key = "k"
name = "k"
[[providers]]
name = "p1"
type = "mock"
models = ["m-expensive", "m-cheap"]
[[providers]]
name = "p2"
type = "mock"
models = ["m-expensive"]
[pricing]
"m-expensive" = [2.0, 8.0]
"m-cheap" = [0.1, 0.4]
[routes]
auto = ["p1/m-expensive", "p1/m-cheap", "p2/m-expensive"]
"""


def test_resolve_alias_returns_candidates(tmp_path):
    r = make_router(tmp_path, BASE)
    d = r.resolve("auto")
    assert len(d.candidates) == 3
    assert d.candidates[0].provider == "p1"


def test_order_is_cheapest_first(tmp_path):
    r = make_router(tmp_path, BASE)
    ordered = r.order(r.resolve("auto"))
    assert ordered[0].model == "m-cheap"  # 0.1/1M prompt price wins


def test_unknown_alias_falls_back_to_model_name(tmp_path):
    r = make_router(tmp_path, BASE)
    d = r.resolve("m-expensive")  # not an alias, but a known model name
    assert {c.provider for c in d.candidates} == {"p1", "p2"}


def test_unknown_model_raises(tmp_path):
    r = make_router(tmp_path, BASE)
    try:
        r.resolve("nonexistent-model")
        assert False, "should raise"
    except KeyError:
        pass


def test_circuit_breaker_deprioritizes_failing_provider(tmp_path):
    r = make_router(tmp_path, BASE)
    for _ in range(3):
        r.breaker.record_failure("p1")
    assert r.breaker.is_open("p1")
    ordered = r.order(r.resolve("auto"))
    assert ordered[0].provider == "p2"  # p1 circuit-broken, sinks to bottom
    assert ordered[-1].provider == "p1"
    r.breaker.record_success("p1")
    assert not r.breaker.is_open("p1")


def test_circuit_breaker_cooldown():
    cb = CircuitBreaker(threshold=1, cooldown_s=0.05)
    cb.record_failure("p")
    assert cb.is_open("p")
    import time
    time.sleep(0.06)
    assert not cb.is_open("p")  # cooled down and auto-reset
