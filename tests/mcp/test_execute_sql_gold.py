# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 3 tests — execute_sql_gold tool."""
from __future__ import annotations

import pytest

from semantic_mapper.mcp.tools.execute_sql import run_execute_sql


def test_executes_valid_sql_returns_rows(gold_sqlite_engine):
    result = run_execute_sql(gold_sqlite_engine, "SELECT * FROM gold_patient", mode="gold")
    assert result["row_count"] == 2
    assert result["mode"] == "gold"
    assert "error" not in result
    assert len(result["rows"]) == 2


def test_rejects_non_select_query(gold_sqlite_engine):
    result = run_execute_sql(gold_sqlite_engine, "DROP TABLE gold_patient", mode="gold")
    assert "error" in result
    assert "Safety" in result["error"] or "safety" in result["error"].lower()
    assert result["query_id"] is None
    assert result["rows"] == []


def test_returns_query_id(gold_sqlite_engine):
    result = run_execute_sql(gold_sqlite_engine, "SELECT COUNT(*) AS n FROM gold_patient", mode="gold")
    assert result["query_id"] is not None
    assert result["query_id"].startswith("q_")


def test_records_duration_ms(gold_sqlite_engine):
    result = run_execute_sql(gold_sqlite_engine, "SELECT * FROM gold_patient", mode="gold")
    assert "duration_ms" in result
    assert isinstance(result["duration_ms"], int)
    assert result["duration_ms"] >= 0


def test_returns_columns(gold_sqlite_engine):
    result = run_execute_sql(gold_sqlite_engine, "SELECT subject_id, gender FROM gold_patient", mode="gold")
    assert set(result["columns"]) == {"subject_id", "gender"}


def test_returns_clear_error_on_invalid_sql(gold_sqlite_engine):
    result = run_execute_sql(gold_sqlite_engine, "SELECT * FROM nonexistent_table_xyz", mode="gold")
    assert "error" in result
    assert "Execution error" in result["error"]
    assert result["rows"] == []


def test_mode_field_set_correctly(gold_sqlite_engine):
    result = run_execute_sql(gold_sqlite_engine, "SELECT * FROM gold_patient LIMIT 1", mode="gold")
    assert result["mode"] == "gold"


def test_group_by_query(gold_sqlite_engine):
    result = run_execute_sql(
        gold_sqlite_engine,
        "SELECT admission_type, COUNT(*) AS n FROM gold_admission GROUP BY admission_type ORDER BY n DESC",
        mode="gold",
    )
    assert result["row_count"] == 2  # EMERGENCY and ELECTIVE
    rows_by_type = {r["admission_type"]: r["n"] for r in result["rows"]}
    assert rows_by_type["EMERGENCY"] == 2
    assert rows_by_type["ELECTIVE"] == 1


def test_decimal_and_temporal_values_are_json_serialisable(gold_sqlite_engine):
    # Regression: MySQL returns SUM/AVG as Decimal and dates as datetime, which
    # crashed json.dumps on the gold/silver endpoints. run_execute_sql must coerce
    # cells to JSON-safe types so the result always serialises.
    import json
    from decimal import Decimal
    from semantic_mapper.mcp.tools.execute_sql import _json_safe

    assert _json_safe(Decimal("7")) == 7 and isinstance(_json_safe(Decimal("7")), int)
    assert _json_safe(Decimal("2.5")) == 2.5 and isinstance(_json_safe(Decimal("2.5")), float)

    result = run_execute_sql(
        gold_sqlite_engine,
        "SELECT SUM(CASE WHEN 1=1 THEN 1 ELSE 0 END) AS n FROM gold_patient",
        mode="gold",
    )
    assert "error" not in result
    json.dumps(result)  # must not raise
