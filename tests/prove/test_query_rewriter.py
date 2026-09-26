# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for Phase 3 — Proof query rewriter.

String-based only; no live DB required.
"""
from __future__ import annotations

import pytest

from semantic_mapper.prove.query_rewriter import (
    ProofQuery,
    ProofRewriteError,
    rewrite,
)

MAX = 1000
SAMPLE = 200


# ---------------------------------------------------------------------------
# SPARQL — COUNT rewrite
# ---------------------------------------------------------------------------

class TestSparqlCount:
    def test_count_becomes_distinct(self):
        q = "SELECT (COUNT(?patient) AS ?n) WHERE { ?patient a :Patient . }"
        result = rewrite(q, "sparql")
        assert "DISTINCT ?patient" in result.proof_query
        assert "COUNT" not in result.proof_query.upper()

    def test_count_preserves_where_body(self):
        q = "SELECT (COUNT(?patient) AS ?n) WHERE { ?patient a :Patient . }"
        result = rewrite(q, "sparql")
        assert "?patient a :Patient" in result.proof_query

    def test_count_adds_limit(self):
        q = "SELECT (COUNT(?x) AS ?n) WHERE { ?x :p ?v . }"
        result = rewrite(q, "sparql")
        assert f"LIMIT {MAX}" in result.proof_query

    def test_count_star_falls_back_to_where_vars(self):
        q = "SELECT (COUNT(*) AS ?n) WHERE { ?patient :hasAdmission ?admission . }"
        result = rewrite(q, "sparql")
        assert "DISTINCT" in result.proof_query
        assert "?patient" in result.proof_query or "?admission" in result.proof_query

    def test_count_mode_is_sparql(self):
        q = "SELECT (COUNT(?x) AS ?n) WHERE { ?x :p ?v . }"
        result = rewrite(q, "sparql")
        assert result.mode == "sparql"

    def test_count_not_sampled_by_default(self):
        q = "SELECT (COUNT(?x) AS ?n) WHERE { ?x :p ?v . }"
        result = rewrite(q, "sparql")
        assert result.sampled is False

    def test_count_original_count_preserved(self):
        q = "SELECT (COUNT(?x) AS ?n) WHERE { ?x :p ?v . }"
        result = rewrite(q, "sparql", original_count=42)
        assert result.original_count == 42


# ---------------------------------------------------------------------------
# SPARQL — SUM / AVG rewrite
# ---------------------------------------------------------------------------

class TestSparqlSumAvg:
    def test_sum_exposes_inner_var(self):
        q = "SELECT (SUM(?amount) AS ?total) WHERE { ?admission :hasAmount ?amount . }"
        result = rewrite(q, "sparql")
        assert "?amount" in result.proof_query
        assert "SUM" not in result.proof_query.upper()

    def test_avg_exposes_inner_var(self):
        q = "SELECT (AVG(?los) AS ?avg_los) WHERE { ?stay :hasLos ?los . }"
        result = rewrite(q, "sparql")
        assert "?los" in result.proof_query
        assert "AVG" not in result.proof_query.upper()

    def test_sum_no_aggregate_alias_in_result(self):
        q = "SELECT (SUM(?amount) AS ?total) WHERE { ?x :hasAmount ?amount . }"
        result = rewrite(q, "sparql")
        assert "?total" not in result.proof_query

    def test_sum_where_body_preserved(self):
        q = "SELECT (SUM(?v) AS ?s) WHERE { ?x :prop ?v . FILTER(?v > 0) }"
        result = rewrite(q, "sparql")
        assert "?x :prop ?v" in result.proof_query
        assert "FILTER(?v > 0)" in result.proof_query


# ---------------------------------------------------------------------------
# SPARQL — GROUP BY rewrite
# ---------------------------------------------------------------------------

class TestSparqlGroupBy:
    def test_group_by_var_in_projection(self):
        q = (
            "SELECT ?ward (COUNT(?patient) AS ?n) "
            "WHERE { ?patient :inWard ?ward . } "
            "GROUP BY ?ward"
        )
        result = rewrite(q, "sparql")
        assert "?ward" in result.proof_query
        assert "GROUP BY" not in result.proof_query.upper()

    def test_group_by_inner_var_in_projection(self):
        q = (
            "SELECT ?ward (COUNT(?patient) AS ?n) "
            "WHERE { ?patient :inWard ?ward . } "
            "GROUP BY ?ward"
        )
        result = rewrite(q, "sparql")
        assert "?patient" in result.proof_query

    def test_group_by_no_aggregate_alias(self):
        q = (
            "SELECT ?dept (SUM(?salary) AS ?total) "
            "WHERE { ?emp :inDept ?dept ; :hasSalary ?salary . } "
            "GROUP BY ?dept"
        )
        result = rewrite(q, "sparql")
        assert "?total" not in result.proof_query

    def test_group_by_limit_applied(self):
        q = (
            "SELECT ?ward (COUNT(?patient) AS ?n) "
            "WHERE { ?patient :inWard ?ward . } "
            "GROUP BY ?ward"
        )
        result = rewrite(q, "sparql")
        assert f"LIMIT {MAX}" in result.proof_query


# ---------------------------------------------------------------------------
# SPARQL — no aggregate (pass-through)
# ---------------------------------------------------------------------------

class TestSparqlNoAggregate:
    def test_no_aggregate_where_body_preserved(self):
        q = "SELECT ?patient ?admission WHERE { ?patient :hasAdmission ?admission . }"
        result = rewrite(q, "sparql")
        assert "?patient :hasAdmission ?admission" in result.proof_query

    def test_no_aggregate_limit_added(self):
        q = "SELECT ?x ?y WHERE { ?x :p ?y . }"
        result = rewrite(q, "sparql")
        assert f"LIMIT {MAX}" in result.proof_query

    def test_no_aggregate_existing_limit_replaced(self):
        q = "SELECT ?x WHERE { ?x :p ?v . } LIMIT 7"
        result = rewrite(q, "sparql")
        assert "LIMIT 7" not in result.proof_query
        assert f"LIMIT {MAX}" in result.proof_query

    def test_no_aggregate_order_by_stripped(self):
        q = "SELECT ?x WHERE { ?x :p ?v . } ORDER BY ?x LIMIT 10"
        result = rewrite(q, "sparql")
        assert "ORDER BY" not in result.proof_query.upper()


# ---------------------------------------------------------------------------
# SPARQL — unsupported patterns
# ---------------------------------------------------------------------------

class TestSparqlUnsupported:
    def test_having_raises(self):
        q = "SELECT ?ward (COUNT(?p) AS ?n) WHERE { ?p :inWard ?ward . } GROUP BY ?ward HAVING (?n > 5)"
        with pytest.raises(ProofRewriteError) as exc_info:
            rewrite(q, "sparql")
        assert exc_info.value.unsupported_pattern == "HAVING"

    def test_minus_raises(self):
        q = "SELECT ?x WHERE { ?x a :Patient . MINUS { ?x :discharged true . } }"
        with pytest.raises(ProofRewriteError) as exc_info:
            rewrite(q, "sparql")
        assert exc_info.value.unsupported_pattern == "MINUS"

    def test_subquery_raises(self):
        q = "SELECT ?x WHERE { { SELECT ?x WHERE { ?x a :Patient . } } ?x :p ?v . }"
        with pytest.raises(ProofRewriteError) as exc_info:
            rewrite(q, "sparql")
        assert exc_info.value.unsupported_pattern == "subquery"

    def test_error_message_names_pattern(self):
        q = "SELECT ?x WHERE { ?x a :Patient . MINUS { ?x :flag true . } }"
        with pytest.raises(ProofRewriteError, match="MINUS"):
            rewrite(q, "sparql")


# ---------------------------------------------------------------------------
# SPARQL — sampling
# ---------------------------------------------------------------------------

class TestSparqlSampling:
    def test_sampled_false_when_count_below_threshold(self):
        q = "SELECT (COUNT(?x) AS ?n) WHERE { ?x a :Patient . }"
        result = rewrite(q, "sparql", original_count=500)
        assert result.sampled is False
        assert result.sample_limit == MAX

    def test_sampled_true_when_count_exceeds_threshold(self):
        q = "SELECT (COUNT(?x) AS ?n) WHERE { ?x a :Patient . }"
        result = rewrite(q, "sparql", original_count=5000)
        assert result.sampled is True
        assert result.sample_limit == SAMPLE
        assert f"LIMIT {SAMPLE}" in result.proof_query

    def test_sampled_false_when_no_original_count(self):
        q = "SELECT (COUNT(?x) AS ?n) WHERE { ?x a :Patient . }"
        result = rewrite(q, "sparql", original_count=None)
        assert result.sampled is False

    def test_custom_max_subjects(self):
        q = "SELECT ?x WHERE { ?x a :Patient . }"
        result = rewrite(q, "sparql", original_count=50, max_subjects=100, sample_size=10)
        assert result.sampled is False
        assert "LIMIT 100" in result.proof_query

    def test_custom_sample_size_applied_when_over_limit(self):
        q = "SELECT ?x WHERE { ?x a :Patient . }"
        result = rewrite(q, "sparql", original_count=200, max_subjects=100, sample_size=25)
        assert result.sampled is True
        assert "LIMIT 25" in result.proof_query


# ---------------------------------------------------------------------------
# SQL — silver / gold modes
# ---------------------------------------------------------------------------

class TestSqlRewrite:
    def test_projection_replaced_with_star(self):
        q = "SELECT patient_id, admission_type FROM silver_admissions WHERE subject_id = 1"
        result = rewrite(q, "silver")
        assert result.proof_query.startswith("SELECT * FROM")

    def test_where_clause_preserved(self):
        q = "SELECT patient_id FROM silver_admissions WHERE subject_id = 1"
        result = rewrite(q, "silver")
        assert "WHERE subject_id = 1" in result.proof_query

    def test_gold_mode_label(self):
        q = "SELECT id, name FROM gold_patient WHERE id = 42"
        result = rewrite(q, "gold")
        assert result.mode == "gold"

    def test_existing_limit_replaced(self):
        q = "SELECT count(*) FROM silver_admissions WHERE subject_id = 1 LIMIT 99"
        result = rewrite(q, "silver")
        assert "LIMIT 99" not in result.proof_query
        assert f"LIMIT {MAX}" in result.proof_query

    def test_order_by_stripped(self):
        q = "SELECT patient_id FROM silver_admissions ORDER BY patient_id LIMIT 50"
        result = rewrite(q, "silver")
        assert "ORDER BY" not in result.proof_query.upper()

    def test_max_subjects_limit_added(self):
        q = "SELECT id FROM gold_patient WHERE id > 0"
        result = rewrite(q, "gold")
        assert f"LIMIT {MAX}" in result.proof_query

    def test_aggregate_stripped(self):
        q = "SELECT COUNT(*) FROM silver_admissions WHERE subject_id = 1"
        result = rewrite(q, "silver")
        assert result.proof_query.startswith("SELECT * FROM")
        assert "COUNT" not in result.proof_query.upper()

    def test_sampled_false_below_threshold(self):
        q = "SELECT id FROM gold_patient"
        result = rewrite(q, "gold", original_count=50)
        assert result.sampled is False
        assert result.sample_limit == MAX

    def test_sampled_true_above_threshold(self):
        q = "SELECT id FROM gold_patient"
        result = rewrite(q, "gold", original_count=2000)
        assert result.sampled is True
        assert result.sample_limit == SAMPLE
        assert f"LIMIT {SAMPLE}" in result.proof_query

    def test_distinct_stripped(self):
        q = "SELECT DISTINCT patient_id FROM silver_admissions WHERE ward = 'ICU'"
        result = rewrite(q, "silver")
        assert result.proof_query.startswith("SELECT * FROM")


# ---------------------------------------------------------------------------
# Unknown mode
# ---------------------------------------------------------------------------

class TestUnknownMode:
    def test_unknown_mode_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown mode"):
            rewrite("SELECT ?x WHERE { ?x a :C . }", "bronze")
