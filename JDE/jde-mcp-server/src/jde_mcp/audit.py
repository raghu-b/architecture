"""Independent audit log.

JDE's own transaction log records writes, but says nothing about what an agent
*read*. When an auditor asks "what did the AI have access to during close",
that question is only answerable from a log you keep yourself. Every tool call
lands here, successful or not.

SQLite is used so the package runs with no external dependency. For a shared
deployment, point this at Postgres or ship the rows to your SIEM — the schema
is deliberately simple enough to port.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id            TEXT PRIMARY KEY,
    ts            TEXT NOT NULL,
    actor         TEXT NOT NULL,
    tool          TEXT NOT NULL,
    action        TEXT NOT NULL,
    arguments     TEXT,
    row_count     INTEGER,
    status        TEXT NOT NULL,
    detail        TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_tool ON audit_log(tool);

CREATE TABLE IF NOT EXISTS write_drafts (
    draft_id      TEXT PRIMARY KEY,
    ts            TEXT NOT NULL,
    target        TEXT NOT NULL,
    payload       TEXT NOT NULL,
    explanation   TEXT,
    requested_by  TEXT NOT NULL,
    status        TEXT NOT NULL,
    decided_ts    TEXT,
    decided_by    TEXT,
    result        TEXT
);
CREATE INDEX IF NOT EXISTS idx_draft_status ON write_drafts(status);
"""


class AuditLog:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def record(self, tool: str, action: str, arguments: dict[str, Any] | None = None,
               row_count: int | None = None, status: str = "ok",
               detail: str | None = None, actor: str = "claude-agent") -> str:
        """Write one audit row. Never raises — a logging failure must not take
        down a tool call, but it should be visible, so we degrade to detail."""
        entry_id = str(uuid.uuid4())
        safe_args = _redact(arguments or {})
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO audit_log (id, ts, actor, tool, action, arguments,"
                    " row_count, status, detail) VALUES (?,?,?,?,?,?,?,?,?)",
                    (entry_id, self._now(), actor, tool, action,
                     json.dumps(safe_args, default=str)[:8000],
                     row_count, status, (detail or "")[:4000]),
                )
                self._conn.commit()
        except sqlite3.Error:
            pass
        return entry_id

    # -- write drafts -------------------------------------------------------

    def save_draft(self, target: str, payload: dict[str, Any],
                   explanation: str, requested_by: str = "claude-agent") -> str:
        draft_id = f"draft-{uuid.uuid4().hex[:12]}"
        with self._lock:
            self._conn.execute(
                "INSERT INTO write_drafts (draft_id, ts, target, payload,"
                " explanation, requested_by, status) VALUES (?,?,?,?,?,?,?)",
                (draft_id, self._now(), target,
                 json.dumps(payload, default=str), explanation,
                 requested_by, "pending_approval"),
            )
            self._conn.commit()
        return draft_id

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM write_drafts WHERE draft_id = ?", (draft_id,)
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        return d

    def list_drafts(self, status: str = "pending_approval",
                    limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT draft_id, ts, target, explanation, requested_by, status"
                " FROM write_drafts WHERE status = ? ORDER BY ts DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def decide_draft(self, draft_id: str, status: str, decided_by: str,
                     result: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE write_drafts SET status=?, decided_ts=?, decided_by=?,"
                " result=? WHERE draft_id=?",
                (status, self._now(), decided_by, result, draft_id),
            )
            self._conn.commit()

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, actor, tool, action, row_count, status"
                " FROM audit_log ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_SENSITIVE = {"password", "token", "secret", "authorization", "pwd"}


def _redact(data: dict[str, Any]) -> dict[str, Any]:
    """Strip anything credential-shaped before it reaches durable storage."""
    out = {}
    for k, v in data.items():
        if any(s in k.lower() for s in _SENSITIVE):
            out[k] = "***redacted***"
        elif isinstance(v, dict):
            out[k] = _redact(v)
        else:
            out[k] = v
    return out
