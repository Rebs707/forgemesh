# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for Phase 12 — rewrite_for_row_proof() in query_rewriter.py.
"""
from __future__ import annotations

import pytest

from semantic_mapper.prove.query_rewriter import (
    ProofRewriteError,
    RowProofQuery,
    rewrite_for_row_proof,
)


def test_row_rewrite_groups_by_single_variable():
    q = "SELECT ?diag (COUNT(?adm) AS ?n) WHERE { ?adm a :Admission ; :hasDiag ?diag . } GROUP BY ?diag"
    result = rewrite_for_row_proof(q, "sparql", {"diag": "Sepsis"})
    assert 'FILTER(STR(?diag) = "Sepsis")' in result.proof_query
    assert "DISTINCT" in result.proof_query
    assert "GROUP BY" not in result.proof_query.upper()


def test_row_rewrite_groups_by_multiple_variables():
    q = 'SELECT ?a ?b (COUNT(?x) AS ?n) WHERE { ?x :propA ?a ; :propB ?b . } GROUP BY ?a ?b'
    result = rewrite_for_row_proof(q, "sparql", {"a": "foo", "b": "bar"})
    assert 'FILTER(STR(?a) = "foo")' in result.proof_query
    assert 'FILTER(STR(?b) = "bar")' in result.proof_query


def test_row_rewrite_no_group_by_falls_through():
    q = "SELECT (COUNT(?p) AS ?n) WHERE { ?p a :Patient . }"
    result = rewrite_for_row_proof(q, "sparql", {})
    assert isinstance(result, RowProofQuery)
    assert "LIMIT" in result.proof_query


def test_row_rewrite_unsupported_pattern_raises():
    q = "SELECT ?w (COUNT(?p) AS ?n) WHERE { ?p :ward ?w . } GROUP BY ?w HAVING(?n > 5)"
    with pytest.raises(ProofRewriteError) as exc:
        rewrite_for_row_proof(q, "sparql", {"w": "ICU"})
    assert exc.value.unsupported_pattern == "HAVING"


def test_row_rewrite_numeric_filter_unquoted():
    q = "SELECT ?ward (COUNT(?p) AS ?n) WHERE { ?p :ward ?ward . } GROUP BY ?ward"
    result = rewrite_for_row_proof(q, "sparql", {"ward": 42})
    assert "FILTER(?ward = 42)" in result.proof_query
    assert '"42"' not in result.proof_query


def test_row_rewrite_sql_gold_mode():
    q = "SELECT `type`, COUNT(*) AS n FROM gold_admissions GROUP BY `type` ORDER BY n DESC LIMIT 100"
    result = rewrite_for_row_proof(q, "gold", {"type": "ELECTIVE"})
    assert result.mode == "gold"
    assert "SELECT * FROM" in result.proof_query
    assert "GROUP BY" not in result.proof_query.upper()
    assert "ORDER BY" not in result.proof_query.upper()
    assert "`type` = 'ELECTIVE'" in result.proof_query
    assert "LIMIT" in result.proof_query


def test_row_rewrite_sql_silver_mode_no_grouping():
    q = "SELECT patient_id, COUNT(*) AS c FROM silver_patients"
    result = rewrite_for_row_proof(q, "silver", {})
    assert result.mode == "silver"
    assert "SELECT * FROM" in result.proof_query
    assert "LIMIT" in result.proof_query


def test_row_rewrite_unknown_mode_raises():
    with pytest.raises(ValueError, match="Unknown mode"):
        rewrite_for_row_proof("SELECT * FROM t", "bronze", {})


def test_row_rewrite_returns_correct_type():
    q = "SELECT ?x (COUNT(?y) AS ?n) WHERE { ?x :p ?y . } GROUP BY ?x"
    result = rewrite_for_row_proof(q, "sparql", {"x": "val"})
    assert isinstance(result, RowProofQuery)
    assert result.mode == "sparql"
    assert result.row_grouping_values == {"x": "val"}
    assert not result.sampled
