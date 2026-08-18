"""
GitPulse - lightweight local persistence.

The project ships without a database, so this module provides a small
SQLite-backed store (stdlib `sqlite3`) for durable records that matter:

    * AI code analyses
    * AI fix attempts + their validation results
    * Incoming GitHub webhook events

It degrades gracefully: if the database cannot be written (read-only
filesystem, locked file, missing permissions) the app keeps running and
logs the problem instead of crashing.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from config.logging_setup import get_logger

logger = get_logger("app")

_DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "gitpulse.db")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """Thread-safe SQLite store with a tiny API surface."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or os.getenv("GITPULSE_DB_PATH", _DEFAULT_DB_PATH)
        self._lock = threading.Lock()
        self._enabled = False
        try:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            self._connection = sqlite3.connect(self.db_path, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._init_schema()
            self._enabled = True
        except Exception as exc:  # noqa: BLE001 - store must never crash the app
            logger.warning("SQLite store disabled (%s); using in-memory fallback.", exc)
            self._enabled = False

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def _init_schema(self) -> None:
        with self._lock:
            cur = self._connection.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS ai_analysis (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    kind TEXT NOT NULL,            -- code | pr | issue
                    target TEXT NOT NULL,          -- repo path / pr number / issue number
                    author TEXT,
                    result_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fix_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    branch TEXT,
                    pr_url TEXT,
                    path TEXT,
                    status TEXT,                   -- created | validation_failed | skipped | error
                    validation TEXT,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS webhook_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    event TEXT NOT NULL,
                    action TEXT,
                    sender TEXT,
                    repo TEXT,
                    payload_json TEXT NOT NULL
                );
                """
            )
            self._connection.commit()

    # ------------------------------------------------------------------
    # AI analysis records
    # ------------------------------------------------------------------
    def save_analysis(
        self,
        kind: str,
        target: str,
        result: dict[str, Any],
        author: Optional[str] = None,
    ) -> None:
        self._insert(
            "ai_analysis",
            created_at=_utcnow(),
            kind=kind,
            target=target,
            author=author,
            result_json=json.dumps(result, default=str),
        )

    def list_analyses(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._query("ai_analysis", "ORDER BY id DESC", limit=limit)
        return [{**dict(r), "result": json.loads(r["result_json"])} for r in rows]

    def find_analysis(
        self, kind: str, target: str
    ) -> Optional[dict[str, Any]]:
        """
        Return the most recent saved analysis matching `kind` + `target`
        (for example kind="issue", target="#12"), or None when no analysis
        has been saved yet. Used to show real AI status on the Issues page.
        """
        if not self._enabled:
            return None
        try:
            with self._lock:
                cur = self._connection.execute(
                    "SELECT * FROM ai_analysis "
                    "WHERE kind = ? AND target = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (kind, target),
                )
                row = cur.fetchone()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read analysis %s/%s: %s", kind, target, exc)
            return None
        if row is None:
            return None
        return {**dict(row), "result": json.loads(row["result_json"])}

    # ------------------------------------------------------------------
    # Fix attempts
    # ------------------------------------------------------------------
    def save_fix_attempt(
        self,
        status: str,
        branch: Optional[str] = None,
        pr_url: Optional[str] = None,
        path: Optional[str] = None,
        validation: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        self._insert(
            "fix_attempts",
            created_at=_utcnow(),
            branch=branch,
            pr_url=pr_url,
            path=path,
            status=status,
            validation=validation,
            error=error,
        )

    def list_fix_attempts(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query("fix_attempts", "ORDER BY id DESC", limit=limit)]

    # ------------------------------------------------------------------
    # Webhook events
    # ------------------------------------------------------------------
    def save_webhook_event(
        self,
        event: str,
        action: Optional[str],
        sender: Optional[str],
        repo: Optional[str],
        payload: dict[str, Any],
    ) -> None:
        self._insert(
            "webhook_events",
            created_at=_utcnow(),
            event=event,
            action=action,
            sender=sender,
            repo=repo,
            payload_json=json.dumps(payload, default=str),
        )

    def list_webhook_events(self, limit: int = 20) -> list[dict[str, Any]]:
        return [dict(r) for r in self._query("webhook_events", "ORDER BY id DESC", limit=limit)]

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------
    def _insert(self, table: str, **values: Any) -> None:
        if not self._enabled:
            return
        columns = ", ".join(values.keys())
        placeholders = ", ".join("?" for _ in values)
        try:
            with self._lock:
                self._connection.execute(
                    f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                    tuple(values.values()),
                )
                self._connection.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not write %s record: %s", table, exc)

    def _query(self, table: str, order: str = "", limit: int = 50) -> list[sqlite3.Row]:
        if not self._enabled:
            return []
        try:
            with self._lock:
                cur = self._connection.execute(
                    f"SELECT * FROM {table} {order} LIMIT ?", (limit,)
                )
                return cur.fetchall()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not read %s records: %s", table, exc)
            return []


# A process-wide singleton, created lazily.
_store: Optional[Store] = None


def get_store() -> Store:
    """Return the shared Store instance."""
    global _store
    if _store is None:
        _store = Store()
    return _store
