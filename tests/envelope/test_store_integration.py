# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Integration: receipt store round-trip + envelope assembly + row paging.

Exercises the real persistence path used by GET /runs/{id}, GET /runs/{id}/rows,
get_receipt and get_rows — without needing the LLM or the database stack. Uses a
temporary pipeline_runs.db (SQLite), consistent with the project's test rules.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from semantic_mapper.config.settings import Settings
from semantic_mapper.envelope import build_envelope
from semantic_mapper.mcp import query_store as qs


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings()
    s.CORRECTIONS_DB_PATH = str(tmp_path / "corrections.db")
    return s


def _make_record(query_id: str) -> qs.MCPQueryRecord:
    return qs.MCPQueryRecord(
        query_id=query_id,
        session_id="mcp_default",
        question="admissions by type",
        executed_query="SELECT admission_type, COUNT(*) AS n FROM gold_admission GROUP BY admission_type",
        mode="gold",
        answer={
            "columns": ["admission_type", "n"],
            "rows": [{"admission_type": f"T{i}", "n": i} for i in range(250)],
            "row_count": 250,
        },
        run_id="run_x",
        created_at=datetime.now(timezone.utc).isoformat(),
        proof_record_json=None,
    )


def test_store_roundtrip_and_envelope(settings):
    qs.ensure_table(settings)
    qid = qs.new_query_id()
    qs.write_query(_make_record(qid), settings)

    record = qs.read_query(qid, settings)
    assert record is not None
    assert record.question == "admissions by type"

    env = build_envelope(
        run_id=qid, question=record.question, executed_query=record.executed_query,
        mode=record.mode, answer=record.answer, settings=settings)
    assert env.run_id == qid
    assert env.receipt.evidence.row_set == f"fm://runs/{qid}/rows"
    assert env.receipt_url.endswith(f"/runs/{qid}")
    # small_group fires: groups T0..T19 have n < 20 → 20 small groups
    sg = [c for c in env.receipt.caveats if c.kind == "small_group"]
    assert sg and sg[0].count == 20


def test_recent_runs_listing(settings):
    qs.ensure_table(settings)
    ids = [qs.new_query_id() for _ in range(3)]
    for qid in ids:
        qs.write_query(_make_record(qid), settings)
    # The /api/runs listing reads mcp_queries directly; assert the rows are present.
    import sqlite3
    con = sqlite3.connect(str(qs.get_pipeline_runs_db_path(settings)))
    rows = con.execute("SELECT query_id FROM mcp_queries ORDER BY created_at DESC").fetchall()
    con.close()
    stored = {r[0] for r in rows}
    assert set(ids) <= stored


def _page(record, page, size):
    """Mirror the paging maths used by GET /runs/{id}/rows and get_rows."""
    all_rows = list((record.answer or {}).get("rows") or [])
    start = (page - 1) * size
    return all_rows[start:start + size], len(all_rows)


def test_row_paging(settings):
    qs.ensure_table(settings)
    qid = qs.new_query_id()
    qs.write_query(_make_record(qid), settings)
    record = qs.read_query(qid, settings)

    page1, total = _page(record, 1, 100)
    page3, _ = _page(record, 3, 100)
    assert total == 250
    assert len(page1) == 100
    assert len(page3) == 50           # last partial page
    assert page1[0]["admission_type"] == "T0"
    assert page3[0]["admission_type"] == "T200"
