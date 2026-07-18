"""SQLite persistence: traces, spans, usage events. Single-writer lock keeps
it safe under uvicorn's threadpool; swap for Postgres in production."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    key_name TEXT,
    alias TEXT,
    status TEXT NOT NULL,            -- ok | error
    failure_stage TEXT,              -- auth|quota|ratelimit|route|provider|timeout
    latency_ms REAL,
    provider TEXT,
    model TEXT,
    route_reason TEXT
);
CREATE TABLE IF NOT EXISTS spans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL,
    status TEXT NOT NULL DEFAULT 'ok',
    attrs TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    day TEXT NOT NULL,               -- YYYY-MM-DD (UTC)
    key_name TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    trace_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_day ON usage_events(day, key_name);
"""


class Storage:
    def __init__(self, db_path: str):
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- writes ----
    def insert_trace(self, t: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO traces (trace_id, ts, key_name, alias, status,"
                " failure_stage, latency_ms, provider, model, route_reason)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    t["trace_id"], t["ts"], t.get("key_name"), t.get("alias"),
                    t["status"], t.get("failure_stage"), t.get("latency_ms"),
                    t.get("provider"), t.get("model"), t.get("route_reason"),
                ),
            )
            self._conn.commit()

    def insert_span(self, trace_id: str, name: str, start_ts: float,
                    end_ts: float | None, status: str, attrs: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO spans (trace_id, name, start_ts, end_ts, status, attrs)"
                " VALUES (?,?,?,?,?,?)",
                (trace_id, name, start_ts, end_ts, status, json.dumps(attrs, ensure_ascii=False)),
            )
            self._conn.commit()

    def insert_usage(self, u: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO usage_events (ts, day, key_name, provider, model,"
                " prompt_tokens, completion_tokens, cost_usd, trace_id)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    u["ts"], u["day"], u["key_name"], u["provider"], u["model"],
                    u["prompt_tokens"], u["completion_tokens"], u["cost_usd"],
                    u.get("trace_id"),
                ),
            )
            self._conn.commit()

    # ---- reads ----
    def tokens_today(self, key_name: str, day: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS t"
                " FROM usage_events WHERE key_name=? AND day=?",
                (key_name, day),
            ).fetchone()
        return int(row["t"])

    def list_traces(self, limit: int = 50, status: str | None = None) -> list[dict]:
        q = "SELECT * FROM traces"
        args: list[Any] = []
        if status:
            q += " WHERE status=?"
            args.append(status)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def get_trace(self, trace_id: str) -> dict | None:
        with self._lock:
            t = self._conn.execute(
                "SELECT * FROM traces WHERE trace_id=?", (trace_id,)
            ).fetchone()
            spans = self._conn.execute(
                "SELECT * FROM spans WHERE trace_id=? ORDER BY start_ts", (trace_id,)
            ).fetchall()
        if t is None:
            return None
        out = dict(t)
        out["spans"] = [
            {**dict(s), "attrs": json.loads(s["attrs"] or "{}")} for s in spans
        ]
        return out

    def usage_summary(self, day: str | None = None) -> list[dict]:
        q = (
            "SELECT day, key_name, model, provider,"
            " SUM(prompt_tokens) AS prompt_tokens,"
            " SUM(completion_tokens) AS completion_tokens,"
            " ROUND(SUM(cost_usd), 6) AS cost_usd, COUNT(*) AS calls"
            " FROM usage_events"
        )
        args: list[Any] = []
        if day:
            q += " WHERE day=?"
            args.append(day)
        q += " GROUP BY day, key_name, model, provider ORDER BY day DESC, cost_usd DESC"
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [dict(r) for r in rows]

    def attribution(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT COALESCE(failure_stage, 'unknown') AS stage, COUNT(*) AS n"
                " FROM traces WHERE status='error' GROUP BY failure_stage"
                " ORDER BY n DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def utc_day(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts or time.time()))
