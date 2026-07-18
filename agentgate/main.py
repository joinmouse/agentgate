"""FastAPI assembly: OpenAI-compatible endpoint, admin APIs, dashboard."""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import Config, KeyConfig, load_config
from .metering import QuotaExceeded, RateLimited, RateLimiter, cost_usd
from .providers import ProviderError, build_provider
from .routing import Router
from .storage import Storage, utc_day
from .tracing import Recorder

DASHBOARD = Path(__file__).parent / "dashboard" / "index.html"


def create_app(config_path: str | None = None) -> FastAPI:
    config_path = config_path or os.environ.get("AGENTGATE_CONFIG", "config.example.toml")
    config: Config = load_config(config_path)
    storage = Storage(config.gateway.db_path)
    recorder = Recorder(storage)
    router = Router(config)
    limiter = RateLimiter()
    providers = {p.name: build_provider(p) for p in config.providers}

    app = FastAPI(title="AgentGate", version="0.1.0")
    app.state.config = config
    app.state.storage = storage

    # ---------- helpers ----------
    def admin_guard(request: Request) -> None:
        if request.headers.get("X-Admin-Key") != config.gateway.admin_key:
            raise HTTPException(status_code=403, detail="invalid admin key")

    def fail(trace_id: str, *, ts: float, key: KeyConfig | None, alias: str | None,
             stage: str, status_code: int, detail: str,
             route_reason: str | None = None) -> JSONResponse:
        recorder.finish_trace(
            trace_id, ts=ts, key_name=key.name if key else None, alias=alias,
            status="error", latency_ms=(time.time() - ts) * 1000,
            failure_stage=stage, route_reason=route_reason,
        )
        return JSONResponse(
            status_code=status_code,
            content={"error": {"message": detail, "stage": stage,
                               "trace_id": trace_id}},
        )

    # ---------- public API ----------
    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "version": "0.1.0"}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Any:
        ts = time.time()
        trace_id = recorder.new_trace_id()
        key_cfg: KeyConfig | None = None
        alias: str | None = None
        try:
            body = await request.json()
        except Exception:
            body = {}

        with recorder.span(trace_id, "gateway.receive") as sp:
            sp.attrs["path"] = "/v1/chat/completions"

        # 1. auth
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        key_cfg = config.key_map().get(token)
        if key_cfg is None:
            return fail(trace_id, ts=ts, key=None, alias=None, stage="auth",
                        status_code=401, detail="invalid or missing API key")

        # 2. daily token quota
        day = utc_day(ts)
        used = storage.tokens_today(key_cfg.name, day)
        if used >= key_cfg.daily_quota_tokens:
            return fail(trace_id, ts=ts, key=key_cfg, alias=None, stage="quota",
                        status_code=429,
                        detail=f"daily quota exceeded: {used}/{key_cfg.daily_quota_tokens}")

        # 3. rate limit
        if not limiter.allow(key_cfg.name, key_cfg.rpm):
            return fail(trace_id, ts=ts, key=key_cfg, alias=None, stage="ratelimit",
                        status_code=429, detail="rate limit exceeded")

        # 4. routing decision
        alias = str(body.get("model", "auto"))
        with recorder.span(trace_id, "route.decide") as sp:
            try:
                decision = router.resolve(alias)
                ordered = router.order(decision)
            except KeyError as e:
                sp.status = "error"
                return fail(trace_id, ts=ts, key=key_cfg, alias=alias, stage="route",
                            status_code=404, detail=str(e))
            sp.attrs["reason"] = decision.reason
            sp.attrs["candidates"] = [f"{c.provider}/{c.model}" for c in ordered]
        if not ordered:
            return fail(trace_id, ts=ts, key=key_cfg, alias=alias, stage="route",
                        status_code=404, detail="no healthy provider for alias",
                        route_reason=decision.reason)

        # 5. provider call with fallback
        messages = body.get("messages", [])
        params = {k: v for k, v in body.items() if k not in ("model", "messages")}
        last_err: str = ""
        with recorder.span(trace_id, "provider.call") as sp:
            for cand in ordered:
                provider = providers[cand.provider]
                attempt = {"provider": cand.provider, "model": cand.model}
                try:
                    t0 = time.time()
                    resp = provider.chat(cand.model, messages, **params)
                    attempt["latency_ms"] = round((time.time() - t0) * 1000, 2)
                    attempt["ok"] = True
                    decision.attempts.append(attempt)
                    router.breaker.record_success(cand.provider)
                    sp.attrs["provider"] = cand.provider
                    sp.attrs["model"] = cand.model
                    sp.attrs["latency_ms"] = attempt["latency_ms"]
                    # record full attempt history so successful fallbacks are
                    # auditable too (not only total failures)
                    sp.attrs["attempts"] = decision.attempts

                    usage = resp.get("usage", {})
                    pt = int(usage.get("prompt_tokens", 0))
                    ct = int(usage.get("completion_tokens", 0))
                    cost = cost_usd(config, cand.model, pt, ct)
                    storage.insert_usage({
                        "ts": ts, "day": day, "key_name": key_cfg.name,
                        "provider": cand.provider, "model": cand.model,
                        "prompt_tokens": pt, "completion_tokens": ct,
                        "cost_usd": cost, "trace_id": trace_id,
                    })
                    with recorder.span(trace_id, "gateway.respond") as sp2:
                        sp2.attrs["cost_usd"] = round(cost, 6)
                        sp2.attrs["total_tokens"] = pt + ct
                    recorder.finish_trace(
                        trace_id, ts=ts, key_name=key_cfg.name, alias=alias,
                        status="ok", latency_ms=(time.time() - ts) * 1000,
                        provider=cand.provider, model=cand.model,
                        route_reason=decision.reason,
                    )
                    resp.setdefault("agentgate", {})
                    resp["agentgate"].update({
                        "trace_id": trace_id, "provider": cand.provider,
                        "model": cand.model, "cost_usd": round(cost, 6),
                        "attempts": decision.attempts,
                    })
                    return resp
                except ProviderError as e:
                    last_err = str(e)
                    attempt["ok"] = False
                    attempt["error"] = last_err
                    decision.attempts.append(attempt)
                    router.breaker.record_failure(cand.provider)
            sp.status = "error"
            sp.attrs["attempts"] = decision.attempts

        stage = "timeout" if "timeout" in last_err else "provider"
        return fail(trace_id, ts=ts, key=key_cfg, alias=alias, stage=stage,
                    status_code=502, detail=f"all providers failed: {last_err}",
                    route_reason=decision.reason)

    # ---------- admin API ----------
    @app.get("/admin/traces")
    def admin_traces(limit: int = 50, status: str | None = None,
                     _: None = Depends(admin_guard)) -> list[dict]:
        return storage.list_traces(limit=limit, status=status)

    @app.get("/admin/traces/{trace_id}")
    def admin_trace_detail(trace_id: str, _: None = Depends(admin_guard)) -> dict:
        t = storage.get_trace(trace_id)
        if t is None:
            raise HTTPException(status_code=404, detail="trace not found")
        return t

    @app.get("/admin/usage")
    def admin_usage(day: str | None = None, _: None = Depends(admin_guard)) -> list[dict]:
        return storage.usage_summary(day=day)

    @app.get("/admin/attribution")
    def admin_attribution(_: None = Depends(admin_guard)) -> list[dict]:
        return storage.attribution()

    # ---------- dashboard ----------
    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return DASHBOARD.read_text(encoding="utf-8")

    return app


# Module-level app for `uvicorn agentgate.main:app` (uses AGENTGATE_CONFIG or
# falls back to config.example.toml in the working directory).
app = create_app()
