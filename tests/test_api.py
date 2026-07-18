from conftest import chat


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_auth_required(client):
    r = client.post("/v1/chat/completions", json={"model": "auto", "messages": []})
    assert r.status_code == 401
    assert r.json()["error"]["stage"] == "auth"


def test_chat_completion_openai_schema(client):
    r = chat(client)
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert body["usage"]["total_tokens"] > 0
    # gateway metadata: provider chosen, cost, trace id
    assert body["agentgate"]["provider"] == "mock-primary"
    assert body["agentgate"]["cost_usd"] > 0
    assert body["agentgate"]["trace_id"]


def test_cheapest_route_selection(client):
    r = chat(client, model="cheap")
    assert r.status_code == 200
    assert r.json()["agentgate"]["model"] == "kimi-k2.6-mini"


def test_fallback_when_primary_fails(client):
    r = chat(client, model="flaky")
    assert r.status_code == 200
    meta = r.json()["agentgate"]
    assert meta["provider"] == "mock-backup"  # fell back
    assert len(meta["attempts"]) == 2
    assert meta["attempts"][0]["ok"] is False
    assert meta["attempts"][1]["ok"] is True


def test_usage_recorded_and_summarized(client, admin):
    chat(client, content="metering check")
    usage = client.get("/admin/usage", headers=admin).json()
    assert len(usage) == 1
    row = usage[0]
    assert row["key_name"] == "main"
    assert row["prompt_tokens"] > 0 and row["cost_usd"] > 0


def test_trace_has_spans(client, admin):
    trace_id = chat(client).json()["agentgate"]["trace_id"]
    t = client.get(f"/admin/traces/{trace_id}", headers=admin).json()
    assert t["status"] == "ok"
    names = [s["name"] for s in t["spans"]]
    assert names == ["gateway.receive", "route.decide", "provider.call", "gateway.respond"]
    assert t["route_reason"]


def test_daily_quota_enforced(client):
    assert chat(client, key="sk-test-tiny-quota").status_code == 200
    r = chat(client, key="sk-test-tiny-quota")
    assert r.status_code == 429
    assert r.json()["error"]["stage"] == "quota"


def test_rate_limit_enforced(client):
    assert chat(client, key="sk-test-slow", content="1").status_code == 200
    assert chat(client, key="sk-test-slow", content="2").status_code == 200
    r = chat(client, key="sk-test-slow", content="3")
    assert r.status_code == 429
    assert r.json()["error"]["stage"] == "ratelimit"


def test_failure_attribution_aggregation(client, admin):
    client.post("/v1/chat/completions", json={})  # auth failure
    chat(client, key="sk-test-tiny-quota")
    chat(client, key="sk-test-tiny-quota")  # quota failure
    attr = client.get("/admin/attribution", headers=admin).json()
    stages = {a["stage"]: a["n"] for a in attr}
    assert stages["auth"] == 1
    assert stages["quota"] == 1


def test_admin_requires_key(client):
    assert client.get("/admin/traces").status_code == 403
