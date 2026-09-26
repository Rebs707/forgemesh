# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 1 tests — MCPQueryRecord store (query_store.py)."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from semantic_mapper.config.settings import Settings
from semantic_mapper.mcp.query_store import (
    MCPQueryRecord,
    ensure_table,
    new_query_id,
    purge_old_queries,
    read_query,
    write_query,
)


@pytest.fixture
def store_settings(tmp_path):
    return Settings(
        DATABASE_URL="sqlite:///:memory:",
        SILVER_DATABASE_URL="sqlite:///:memory:",
        GOLD_DATABASE_URL="sqlite:///:memory:",
        CORRECTIONS_DB_PATH=str(tmp_path / "corrections.db"),
    )


def _make_record(query_id: str = None, created_at: str = None) -> MCPQueryRecord:
    return MCPQueryRecord(
        query_id=query_id or new_query_id(),
        session_id="sess_test",
        question="how many admissions",
        executed_query="SELECT COUNT(*) FROM gold_admission",
        mode="gold",
        answer={"row_count": 1, "rows": [{"count": 58976}]},
        run_id="run_abc123",
        created_at=created_at or datetime.now(timezone.utc).isoformat(),
    )


def test_ensure_table_is_idempotent(store_settings):
    ensure_table(store_settings)
    ensure_table(store_settings)  # must not raise


def test_write_and_read_round_trip(store_settings):
    ensure_table(store_settings)
    record = _make_record()
    write_query(record, store_settings)
    retrieved = read_query(record.query_id, store_settings)
    assert retrieved is not None
    assert retrieved.query_id == record.query_id
    assert retrieved.question == record.question
    assert retrieved.executed_query == record.executed_query
    assert retrieved.mode == record.mode
    assert retrieved.answer == record.answer
    assert retrieved.run_id == record.run_id
    assert retrieved.session_id == record.session_id


def test_read_unknown_query_id_returns_none(store_settings):
    ensure_table(store_settings)
    result = read_query("q_doesnotexist", store_settings)
    assert result is None


def test_write_overwrites_on_duplicate_query_id(store_settings):
    ensure_table(store_settings)
    qid = new_query_id()
    r1 = _make_record(query_id=qid)
    r2 = MCPQueryRecord(
        query_id=qid,
        session_id="sess_updated",
        question="updated question",
        executed_query="SELECT 1",
        mode="sparql",
        answer={"row_count": 0, "rows": []},
        run_id="run_xyz",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    write_query(r1, store_settings)
    write_query(r2, store_settings)
    retrieved = read_query(qid, store_settings)
    assert retrieved.session_id == "sess_updated"
    assert retrieved.mode == "sparql"


def test_purge_retention_zero_deletes_all(store_settings):
    ensure_table(store_settings)
    for _ in range(3):
        write_query(_make_record(), store_settings)
    deleted = purge_old_queries(0, store_settings)
    assert deleted == 3
    assert read_query("anything", store_settings) is None


def test_purge_retention_keeps_recent_records(store_settings):
    ensure_table(store_settings)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    new_ts = datetime.now(timezone.utc).isoformat()
    old_id = new_query_id()
    new_id = new_query_id()
    write_query(_make_record(query_id=old_id, created_at=old_ts), store_settings)
    write_query(_make_record(query_id=new_id, created_at=new_ts), store_settings)
    deleted = purge_old_queries(7, store_settings)
    assert deleted == 1
    assert read_query(old_id, store_settings) is None
    assert read_query(new_id, store_settings) is not None


def test_new_query_id_format():
    qid = new_query_id()
    assert qid.startswith("q_")
    assert len(qid) == 14  # "q_" + 12 hex chars


def test_new_query_id_is_unique():
    ids = {new_query_id() for _ in range(100)}
    assert len(ids) == 100


def test_answer_json_survives_round_trip(store_settings):
    ensure_table(store_settings)
    record = _make_record()
    complex_answer = {
        "row_count": 2,
        "rows": [{"patient": "P1", "count": 10}, {"patient": "P2", "count": 5}],
        "columns": ["patient", "count"],
        "duration_ms": 42,
    }
    record = MCPQueryRecord(
        query_id=new_query_id(),
        session_id="s",
        question="q",
        executed_query="SELECT 1",
        mode="gold",
        answer=complex_answer,
        run_id="r",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    write_query(record, store_settings)
    retrieved = read_query(record.query_id, store_settings)
    assert retrieved.answer == complex_answer
