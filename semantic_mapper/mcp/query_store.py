# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 1 — MCP query session storage.

Persists every execute_sparql / execute_sql query to pipeline_runs.db so that
prove_answer can look up the executed query and answer by query_id later.

Table is created idempotently via ensure_table(); call this on MCP server
startup and from _init_pipeline_runs_db() in web_app.py.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional
from uuid import uuid4

from semantic_mapper.config.settings import Settings
from semantic_mapper.db.run_store import get_pipeline_runs_db_path

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS mcp_queries (
        query_id          TEXT PRIMARY KEY,
        session_id        TEXT NOT NULL,
        question          TEXT NOT NULL,
        executed_query    TEXT NOT NULL,
        mode              TEXT NOT NULL,
        answer_json       TEXT NOT NULL,
        run_id            TEXT NOT NULL,
        created_at        TEXT NOT NULL,
        proof_record_json TEXT
    )
"""
_CREATE_IDX_SESSION = (
    "CREATE INDEX IF NOT EXISTS idx_mcp_queries_session ON mcp_queries(session_id)"
)
_CREATE_IDX_CREATED = (
    "CREATE INDEX IF NOT EXISTS idx_mcp_queries_created ON mcp_queries(created_at)"
)


@dataclass(frozen=True)
class MCPQueryRecord:
    query_id: str
    session_id: str
    question: str
    executed_query: str
    mode: str
    answer: Dict[str, Any]
    run_id: str
    created_at: str  # ISO 8601 UTC
    proof_record_json: Optional[str] = None  # Feature 12 ProofRecord v2.0.0 serialised


def new_query_id() -> str:
    """Return a fresh query_id in the same format used by execute_sparql/sql."""
    return "q_" + uuid4().hex[:12]


def _db_path(settings: Settings) -> Path:
    return get_pipeline_runs_db_path(settings)


def ensure_table(settings: Settings) -> None:
    """Create mcp_queries table and indexes idempotently. Safe to call repeatedly."""
    path = _db_path(settings)
    con = sqlite3.connect(str(path))
    con.execute(_CREATE_TABLE)
    con.execute(_CREATE_IDX_SESSION)
    con.execute(_CREATE_IDX_CREATED)
    # Add proof_record_json column to existing tables (migration)
    try:
        con.execute("ALTER TABLE mcp_queries ADD COLUMN proof_record_json TEXT")
    except Exception:
        pass  # column already exists
    con.commit()
    con.close()


def write_query(record: MCPQueryRecord, settings: Settings) -> None:
    """Persist a query record. Logs a warning and returns silently on failure."""
    try:
        con = sqlite3.connect(str(_db_path(settings)))
        con.execute(
            """
            INSERT OR REPLACE INTO mcp_queries
                (query_id, session_id, question, executed_query,
                 mode, answer_json, run_id, created_at, proof_record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.query_id,
                record.session_id,
                record.question,
                record.executed_query,
                record.mode,
                json.dumps(record.answer),
                record.run_id,
                record.created_at,
                record.proof_record_json,
            ),
        )
        con.commit()
        con.close()
    except Exception as exc:
        logger.warning("Could not write MCP query record %s: %s", record.query_id, exc)


def read_query(query_id: str, settings: Settings) -> Optional[MCPQueryRecord]:
    """Return the record for query_id, or None if not found. Never raises."""
    try:
        con = sqlite3.connect(str(_db_path(settings)))
        row = con.execute(
            "SELECT query_id, session_id, question, executed_query, "
            "mode, answer_json, run_id, created_at, proof_record_json "
            "FROM mcp_queries WHERE query_id = ?",
            (query_id,),
        ).fetchone()
        con.close()
        if row is None:
            return None
        return MCPQueryRecord(
            query_id=row[0],
            session_id=row[1],
            question=row[2],
            executed_query=row[3],
            mode=row[4],
            answer=json.loads(row[5]),
            run_id=row[6],
            created_at=row[7],
            proof_record_json=row[8],
        )
    except Exception as exc:
        logger.warning("Could not read MCP query record %s: %s", query_id, exc)
        return None


def get_latest_run_id(settings: Settings) -> Optional[str]:
    """Return the run_id of the most-recently completed pipeline run, or None."""
    try:
        con = sqlite3.connect(str(_db_path(settings)))
        row = con.execute(
            "SELECT run_id FROM pipeline_runs WHERE status='done' "
            "ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None


def purge_old_queries(retention_days: int, settings: Settings) -> int:
    """Delete records older than retention_days. Returns count deleted.

    retention_days=0 deletes all records (cutoff = now).
    """
    try:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=retention_days)
        ).isoformat()
        con = sqlite3.connect(str(_db_path(settings)))
        cursor = con.execute(
            "DELETE FROM mcp_queries WHERE created_at < ?", (cutoff,)
        )
        deleted = cursor.rowcount
        con.commit()
        con.close()
        return deleted
    except Exception as exc:
        logger.warning("Could not purge old MCP query records: %s", exc)
        return 0
