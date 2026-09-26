# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 3 tests — execute_sql_silver tool."""
from __future__ import annotations

import pytest

from semantic_mapper.mcp.tools.execute_sql import run_execute_sql


def test_executes_valid_sql_returns_rows(silver_sqlite_engine):
    result = run_execute_sql(silver_sqlite_engine, "SELECT * FROM silver_raw_admissions", mode="silver")
    assert result["row_count"] == 2
    assert result["mode"] == "silver"
    assert "error" not in result


def test_rejects_non_select_query(silver_sqlite_engine):
    result = run_execute_sql(silver_sqlite_engine, "DELETE FROM silver_raw_admissions", mode="silver")
    assert "error" in result
    assert result["query_id"] is None
    assert result["rows"] == []


def test_returns_query_id(silver_sqlite_engine):
    result = run_execute_sql(silver_sqlite_engine, "SELECT COUNT(*) AS n FROM silver_raw_admissions", mode="silver")
    assert result["query_id"] is not None
    assert result["query_id"].startswith("q_")


def test_records_duration_ms(silver_sqlite_engine):
    result = run_execute_sql(silver_sqlite_engine, "SELECT * FROM silver_raw_admissions", mode="silver")
    assert isinstance(result["duration_ms"], int)
    assert result["duration_ms"] >= 0


def test_returns_columns(silver_sqlite_engine):
    result = run_execute_sql(silver_sqlite_engine, "SELECT hadm_id, admission_type FROM silver_raw_admissions", mode="silver")
    assert set(result["columns"]) == {"hadm_id", "admission_type"}


def test_returns_clear_error_on_invalid_sql(silver_sqlite_engine):
    result = run_execute_sql(silver_sqlite_engine, "SELECT * FROM nonexistent_table_xyz", mode="silver")
    assert "error" in result
    assert result["rows"] == []


def test_class_uri_filter(silver_sqlite_engine):
    result = run_execute_sql(
        silver_sqlite_engine,
        "SELECT * FROM silver_raw_admissions WHERE _ontology_class_uri LIKE '%EmergencyAdmission'",
        mode="silver",
    )
    assert result["row_count"] == 1
    assert result["rows"][0]["admission_type"] == "EMERGENCY"
