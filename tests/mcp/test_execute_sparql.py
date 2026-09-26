# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 2 tests — execute_sparql tool."""
from __future__ import annotations

import pytest

from semantic_mapper.mcp.tools.execute_sparql import run_execute_sparql


# ---------------------------------------------------------------------------
# Mock SPARQL engine
# ---------------------------------------------------------------------------

class _MockSparqlEngine:
    """Simulates SparqlQueryEngine._execute_sparql without Fuseki."""

    def __init__(self, rows=None, raise_exc=None):
        self._rows = rows or []
        self._raise = raise_exc

    def _execute_sparql(self, sparql: str):
        if self._raise:
            raise self._raise
        return self._rows


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_executes_valid_sparql_returns_rows():
    engine = _MockSparqlEngine(rows=[{"admType": "EMERGENCY", "n": "40"}])
    result = run_execute_sparql(engine, "SELECT ?admType (COUNT(?x) AS ?n) WHERE { ?x a <ont:Admission> } GROUP BY ?admType")
    assert result["rows"] == [{"admType": "EMERGENCY", "n": "40"}]
    assert result["row_count"] == 1
    assert "error" not in result


def test_rejects_non_select_query():
    engine = _MockSparqlEngine()
    result = run_execute_sparql(engine, "INSERT DATA { <http://ex.org/x> a <http://ex.org/Y> }")
    assert "error" in result
    assert "Safety" in result["error"] or "safety" in result["error"].lower()
    assert result["query_id"] is None
    assert result["rows"] == []


def test_returns_query_id():
    engine = _MockSparqlEngine(rows=[{"n": "5"}])
    result = run_execute_sparql(engine, "SELECT (COUNT(?x) AS ?n) WHERE { ?x a <ont:Patient> }")
    assert result["query_id"] is not None
    assert result["query_id"].startswith("q_")
    assert len(result["query_id"]) == 14  # "q_" + 12 hex chars


def test_records_duration_ms():
    engine = _MockSparqlEngine(rows=[])
    result = run_execute_sparql(engine, "SELECT ?x WHERE { ?x a <ont:Patient> }")
    assert "duration_ms" in result
    assert isinstance(result["duration_ms"], int)
    assert result["duration_ms"] >= 0


def test_returns_columns_list():
    engine = _MockSparqlEngine(rows=[{"gender": "M", "count": "10"}])
    result = run_execute_sparql(engine, "SELECT ?gender (COUNT(?x) AS ?count) WHERE { ?x a <ont:Patient> } GROUP BY ?gender")
    assert set(result["columns"]) == {"gender", "count"}


def test_returns_clear_error_on_execution_failure():
    engine = _MockSparqlEngine(raise_exc=RuntimeError("Fuseki 400: parse error"))
    result = run_execute_sparql(engine, "SELECT ?x WHERE { ?x a <ont:Patient> }")
    assert "error" in result
    assert "Execution error" in result["error"]
    assert result["rows"] == []
    assert result["row_count"] == 0
    # query_id is still generated (before execution attempt)
    assert result["query_id"] is not None


def test_question_hint_stored():
    engine = _MockSparqlEngine(rows=[{"n": "1"}])
    result = run_execute_sparql(
        engine,
        "SELECT (COUNT(?x) AS ?n) WHERE { ?x a <ont:Patient> }",
        question_hint="How many patients?",
    )
    assert result["question_hint"] == "How many patients?"


def test_empty_result_returns_empty_columns():
    engine = _MockSparqlEngine(rows=[])
    result = run_execute_sparql(engine, "SELECT ?x WHERE { ?x a <ont:Patient> } LIMIT 0")
    assert result["columns"] == []
    assert result["rows"] == []
    assert result["row_count"] == 0
