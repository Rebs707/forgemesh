# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Feature 06 Phase 1 — Tests for sparql_funnel.compute_funnel().

All tests use a mock engine; no live Fuseki required.
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from semantic_mapper.prove.sparql_funnel import (
    FunnelStep,
    _decompose,
    _detect_primary_var,
    _expand_triple_group,
    compute_funnel,
)


# ---------------------------------------------------------------------------
# Mock engine factory
# ---------------------------------------------------------------------------


def _engine(count_sequence: List[int], sample: List[str] | None = None) -> Any:
    """Build a mock engine that returns count_sequence values in order for COUNT
    queries, and *sample* for the final DISTINCT sample query."""
    engine = MagicMock()
    count_iter = iter(count_sequence)

    def _execute(sparql: str) -> List[Dict[str, Any]]:
        if "COUNT(" in sparql.upper():
            try:
                n = next(count_iter)
            except StopIteration:
                n = 0
            return [{"n": str(n)}]
        # DISTINCT sample query
        return [{sparql.split("DISTINCT ")[1].split()[0].lstrip("?"): uri} for uri in (sample or [])]

    engine._execute_sparql.side_effect = _execute
    return engine


# ---------------------------------------------------------------------------
# Decomposition unit tests
# ---------------------------------------------------------------------------


class TestDecompose:
    def test_simple_triple(self):
        where = "?patient a :Patient ."
        patterns = _decompose(where)
        assert len(patterns) == 1
        assert patterns[0][0] == "triple"
        assert "?patient" in patterns[0][1]

    def test_semicolon_expansion(self):
        where = "?adm a :Admission ; :hasFlag \"1\" ."
        patterns = _decompose(where)
        assert len(patterns) == 2
        assert all(p[0] == "triple" for p in patterns)
        # Second triple must have the subject prepended
        assert patterns[1][1].startswith("?adm")

    def test_multiple_triples(self):
        where = "?adm a :Admission . ?adm :hasPatient ?p ."
        patterns = _decompose(where)
        assert len(patterns) == 2

    def test_filter_kept_as_filter(self):
        where = "?adm a :Admission . FILTER(?adm != <x>) ."
        patterns = _decompose(where)
        types = [p[0] for p in patterns]
        assert "filter" in types
        assert "triple" in types

    def test_optional_kept_as_optional(self):
        where = "?adm a :Admission . OPTIONAL { ?adm :hasNote ?n . }"
        patterns = _decompose(where)
        types = [p[0] for p in patterns]
        assert "optional" in types

    def test_union_raises(self):
        where = "{ ?a :p ?b } UNION { ?c :p ?d }"
        with pytest.raises(ValueError, match="UNION"):
            _decompose(where)

    def test_subquery_raises(self):
        where = "{ SELECT ?x WHERE { ?x a :T } }"
        with pytest.raises(ValueError, match="subquery"):
            _decompose(where)


# ---------------------------------------------------------------------------
# compute_funnel integration tests
# ---------------------------------------------------------------------------


class TestComputeFunnel:
    _SIMPLE = """
        SELECT (COUNT(?adm) AS ?n)
        WHERE {
          ?adm a :Admission .
          ?adm :hasFlag "1" .
        }
    """

    def test_funnel_for_simple_count_query(self):
        eng = _engine([1000, 50])
        steps, reason = compute_funnel(self._SIMPLE, eng)
        assert reason is None
        assert len(steps) == 2

    def test_funnel_steps_are_funnelstep_instances(self):
        eng = _engine([1000, 50])
        steps, _ = compute_funnel(self._SIMPLE, eng)
        assert all(isinstance(s, FunnelStep) for s in steps)

    def test_funnel_counts_narrow_progressively(self):
        eng = _engine([5854, 1])
        steps, _ = compute_funnel(self._SIMPLE, eng)
        counts = [s.cumulative_subject_count for s in steps]
        assert counts == [5854, 1]

    def test_first_step_has_no_sample(self):
        eng = _engine([1000, 50], sample=["http://ex/adm/1"])
        steps, _ = compute_funnel(self._SIMPLE, eng)
        assert steps[0].sample_subjects == []

    def test_final_step_has_sample(self):
        eng = _engine([1000, 1], sample=["http://ex/adm/42"])
        steps, _ = compute_funnel(self._SIMPLE, eng)
        assert "http://ex/adm/42" in steps[-1].sample_subjects

    def test_funnel_handles_filter(self):
        sparql = """
            SELECT (COUNT(?adm) AS ?n)
            WHERE {
              ?adm a :Admission .
              FILTER(?adm != <http://ex/adm/0>)
            }
        """
        eng = _engine([1000, 999])
        steps, reason = compute_funnel(sparql, eng)
        assert reason is None
        types = [s.pattern_type for s in steps]
        assert "filter" in types

    def test_funnel_handles_optional(self):
        sparql = """
            SELECT (COUNT(?adm) AS ?n)
            WHERE {
              ?adm a :Admission .
              OPTIONAL { ?adm :hasNote ?n . }
            }
        """
        eng = _engine([1000, 1000])
        steps, reason = compute_funnel(sparql, eng)
        assert reason is None
        types = [s.pattern_type for s in steps]
        assert "optional" in types

    def test_funnel_unsupported_for_union(self):
        sparql = """
            SELECT (COUNT(?x) AS ?n)
            WHERE { { ?x a :A } UNION { ?x a :B } }
        """
        eng = _engine([])
        steps, reason = compute_funnel(sparql, eng)
        assert steps == []
        assert reason == "UNION"

    def test_funnel_unsupported_for_subquery(self):
        sparql = """
            SELECT (COUNT(?x) AS ?n)
            WHERE { { SELECT ?x WHERE { ?x a :T } } }
        """
        eng = _engine([])
        steps, reason = compute_funnel(sparql, eng)
        assert steps == []
        assert "subquery" in (reason or "")

    def test_final_step_subjects_match_sample(self):
        expected = ["http://ex/adm/1", "http://ex/adm/2"]
        eng = _engine([100, 2], sample=expected)
        steps, _ = compute_funnel(self._SIMPLE, eng)
        assert steps[-1].sample_subjects == expected

    def test_primary_var_autodetected(self):
        eng = _engine([10, 5])
        steps, reason = compute_funnel(self._SIMPLE, eng)
        assert reason is None
        # Engine was called — primary var detection worked
        assert eng._execute_sparql.called

    def test_engine_error_returns_negative_count(self):
        eng = MagicMock()
        eng._execute_sparql.side_effect = RuntimeError("Fuseki down")
        steps, reason = compute_funnel(self._SIMPLE, eng)
        assert reason is None
        assert all(s.cumulative_subject_count == -1 for s in steps)

    def test_having_clause_returns_unsupported(self):
        sparql = """
            SELECT ?g (COUNT(?x) AS ?n)
            WHERE { ?x a :T . }
            GROUP BY ?g HAVING (?n > 1)
        """
        eng = _engine([])
        steps, reason = compute_funnel(sparql, eng)
        assert steps == []
        assert reason == "HAVING"

    def test_semicolon_expansion_produces_correct_step_count(self):
        sparql = """
            SELECT (COUNT(?adm) AS ?n)
            WHERE {
              ?adm a :Admission ;
                   :hasFlag "1" ;
                   :hasDiag ?diag .
            }
        """
        eng = _engine([58976, 5854, 5854])
        steps, reason = compute_funnel(sparql, eng)
        assert reason is None
        assert len(steps) == 3
