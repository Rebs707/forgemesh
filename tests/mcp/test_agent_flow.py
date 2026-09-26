# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 7 — scripted agent flow tests.

The "agent" is a script, not a real LLM.  These tests prove that the tool
surface works end-to-end: get_schema → construct query → execute → verify.
No Docker, MySQL, Fuseki, or LLM calls.
"""
from __future__ import annotations

import pytest

from semantic_mapper.mcp.schema_builder import build_agent_schema
from semantic_mapper.mcp.tools.execute_sparql import run_execute_sparql
from semantic_mapper.mcp.tools.execute_sql import run_execute_sql
from .conftest import ONT


# ---------------------------------------------------------------------------
# Mock SPARQL engine — returns fixture data based on query content
# ---------------------------------------------------------------------------

class _ScriptedSparqlEngine:
    """Returns pre-scripted rows; simulates RdflibWriter results for agent tests."""

    def __init__(self, response_map=None):
        self._map = response_map or {}
        self.calls = []

    def _execute_sparql(self, sparql: str):
        self.calls.append(sparql)
        for keyword, rows in self._map.items():
            if keyword in sparql:
                return rows
        return []


# ---------------------------------------------------------------------------
# Phase 7 tests
# ---------------------------------------------------------------------------

def test_agent_constructs_simple_count_query(definitions, mapping_report):
    """Agent calls get_schema, constructs a SPARQL count query, executes it."""
    # Step 1: agent calls get_schema
    schema = build_agent_schema(
        definitions=definitions,
        mapping_report=mapping_report,
        named_graph="http://mesh.local/graph/current",
        base_uri="http://mesh.local/data/",
    )

    # Agent reads construction_rules and class URIs from schema
    patient_class = next(c for c in schema["classes"] if c["label"] == "patient")
    assert patient_class["uri"] == f"{ONT}Patient"

    # Step 2: agent constructs SPARQL from schema (hardcoded for test)
    sparql = (
        f"SELECT (COUNT(DISTINCT ?patURI) AS ?n) "
        f"WHERE {{ GRAPH <http://mesh.local/graph/current> {{ "
        f"?patURI a <{ONT}Patient> . }} }} LIMIT 100"
    )

    # Step 3: agent calls execute_sparql
    engine = _ScriptedSparqlEngine({"Patient": [{"n": "2"}]})
    result = run_execute_sparql(engine, sparql, question_hint="How many patients?")

    assert "error" not in result
    assert result["query_id"] is not None
    assert result["rows"] == [{"n": "2"}]
    assert result["question_hint"] == "How many patients?"
    assert result["duration_ms"] >= 0


def test_agent_iterates_on_zero_result_query(definitions, mapping_report):
    """Agent constructs a wrong query (0 rows), then corrects it — both get distinct query_ids."""
    schema = build_agent_schema(
        definitions=definitions,
        mapping_report=mapping_report,
        named_graph="http://mesh.local/graph/current",
        base_uri="http://mesh.local/data/",
    )

    # First attempt: wrong property URI → 0 rows
    bad_sparql = (
        "SELECT ?x WHERE { GRAPH <http://mesh.local/graph/current> { "
        "?x <ont:hasWrongProperty> ?y . } } LIMIT 100"
    )
    engine = _ScriptedSparqlEngine({"hasAdmissionType": [{"admType": "EMERGENCY", "n": "40"}]})
    result_bad = run_execute_sparql(engine, bad_sparql, question_hint="What admission types?")

    assert result_bad["rows"] == []
    assert result_bad["query_id"] is not None
    bad_query_id = result_bad["query_id"]

    # Agent sees 0 rows, corrects the query
    good_sparql = (
        f"SELECT ?admType (COUNT(DISTINCT ?admURI) AS ?n) "
        f"WHERE {{ GRAPH <http://mesh.local/graph/current> {{ "
        f"?admURI <{ONT}hasAdmissionType> ?admType . }} }} "
        f"GROUP BY ?admType LIMIT 100"
    )
    result_good = run_execute_sparql(engine, good_sparql, question_hint="What admission types?")

    assert result_good["rows"] == [{"admType": "EMERGENCY", "n": "40"}]
    assert result_good["query_id"] is not None

    # Distinct query_ids for distinct executions
    assert result_good["query_id"] != bad_query_id


def test_agent_uses_schema_join_patterns(definitions, mapping_report):
    """Agent reads a join_pattern from the schema and constructs a valid join query."""
    schema = build_agent_schema(
        definitions=definitions,
        mapping_report=mapping_report,
        named_graph="http://mesh.local/graph/current",
        base_uri="http://mesh.local/data/",
    )

    # Find the join_pattern for admission → patient
    admission_class = next(c for c in schema["classes"] if c["label"] == "admission")
    join = next(jp for jp in admission_class["join_patterns"] if jp["to_entity"] == "patient")

    # sparql_pattern should be usable in a query
    assert "admissionURI" in join["sparql_pattern"] or "patient" in join["sparql_pattern"].lower()
    assert join["via_property"] == f"{ONT}hasAdmissionPatient"

    # Agent constructs a join query using the pattern
    sparql = (
        f"SELECT ?gender (COUNT(DISTINCT ?admURI) AS ?n) "
        f"WHERE {{ GRAPH <http://mesh.local/graph/current> {{ "
        f"{join['sparql_pattern']} "
        f"?patientURI <{ONT}hasGender> ?gender . }} }} "
        f"GROUP BY ?gender LIMIT 100"
    )

    engine = _ScriptedSparqlEngine({"hasAdmissionPatient": [{"gender": "M", "n": "1"}]})
    result = run_execute_sparql(engine, sparql)

    assert "error" not in result
    assert result["rows"] == [{"gender": "M", "n": "1"}]


def test_agent_uses_sql_gold_tool(gold_sqlite_engine, definitions, mapping_report):
    """Agent can use execute_sql_gold for gold-layer queries without SPARQL."""
    schema = build_agent_schema(
        definitions=definitions,
        mapping_report=mapping_report,
        named_graph="http://mesh.local/graph/current",
        base_uri="http://mesh.local/data/",
    )

    # Agent reads gold_table from schema
    admission_class = next(c for c in schema["classes"] if c["label"] == "admission")
    gold_table = admission_class["gold_table"]
    assert gold_table == "gold_admission"

    # Agent constructs SQL using the gold_table name
    sql = f"SELECT admission_type, COUNT(*) AS n FROM {gold_table} GROUP BY admission_type ORDER BY n DESC"
    result = run_execute_sql(gold_sqlite_engine, sql, mode="gold", question_hint="Count by admission type")

    assert "error" not in result
    assert result["row_count"] == 2
    rows_by_type = {r["admission_type"]: r["n"] for r in result["rows"]}
    assert rows_by_type["EMERGENCY"] == 2
    assert rows_by_type["ELECTIVE"] == 1


def test_agent_value_matching_guidance_used_in_filter(definitions, mapping_report):
    """Agent reads value_matching_guidance and uses a value in a FILTER clause."""
    schema = build_agent_schema(
        definitions=definitions,
        mapping_report=mapping_report,
        named_graph="http://mesh.local/graph/current",
        base_uri="http://mesh.local/data/",
    )

    guidance = schema["value_matching_guidance"]
    assert any("EMERGENCY" in g for g in guidance)

    # Agent sees "EMERGENCY" in guidance and uses it in a FILTER
    sparql = (
        f"SELECT ?admURI WHERE {{ GRAPH <http://mesh.local/graph/current> {{ "
        f"?admURI <{ONT}hasAdmissionType> 'EMERGENCY' . }} }} LIMIT 100"
    )
    engine = _ScriptedSparqlEngine({"EMERGENCY": [{"admURI": "http://mesh.local/data/A1"}]})
    result = run_execute_sparql(engine, sparql)
    assert result["rows"] == [{"admURI": "http://mesh.local/data/A1"}]
