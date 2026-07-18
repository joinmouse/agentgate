import pytest
from fastapi.testclient import TestClient

from agentgate.main import create_app

TEST_CONFIG = """
[gateway]
db_path = "{db}"
admin_key = "test-admin"

[[keys]]
key = "sk-test-main"
name = "main"
daily_quota_tokens = 1000000
rpm = 1000

[[keys]]
key = "sk-test-tiny-quota"
name = "tiny"
daily_quota_tokens = 1
rpm = 1000

[[keys]]
key = "sk-test-slow"
name = "slow"
daily_quota_tokens = 1000000
rpm = 2

[[providers]]
name = "mock-primary"
type = "mock"
models = ["kimi-k2.6", "kimi-k2.6-mini"]
latency_ms = 0
fail_rate = 0.0

[[providers]]
name = "mock-flaky"
type = "mock"
models = ["kimi-k2.6"]
latency_ms = 0
fail_rate = 1.0

[[providers]]
name = "mock-backup"
type = "mock"
models = ["kimi-k2.6"]
latency_ms = 0
fail_rate = 0.0

[pricing]
"kimi-k2.6" = [0.60, 2.50]
"kimi-k2.6-mini" = [0.10, 0.40]

[routes]
auto = ["mock-primary/kimi-k2.6", "mock-backup/kimi-k2.6"]
cheap = ["mock-primary/kimi-k2.6-mini"]
flaky = ["mock-flaky/kimi-k2.6", "mock-backup/kimi-k2.6"]
"""


@pytest.fixture()
def client(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(TEST_CONFIG.format(db=tmp_path / "test.db"), encoding="utf-8")
    app = create_app(str(cfg))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def admin():
    return {"X-Admin-Key": "test-admin"}


def chat(client, key="sk-test-main", model="auto", content="hello agentgate"):
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": model, "messages": [{"role": "user", "content": content}]},
    )
