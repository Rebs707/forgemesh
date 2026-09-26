# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for LLMStrategy value mapping robustness — issue #45."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from semantic_mapper.engine.strategies.llm_strategy import (
    _preprocess_top_values,
    _is_numeric_only_column,
    _is_null_like,
    LLMStrategy,
)
from semantic_mapper.config.settings import Settings
from semantic_mapper.db.models import ColumnProfile, TableProfile


# ── _is_null_like ─────────────────────────────────────────────────────────────

class TestIsNullLike:
    def test_empty_string(self):
        assert _is_null_like("") is True

    def test_null_string(self):
        assert _is_null_like("null") is True
        assert _is_null_like("NULL") is True
        assert _is_null_like("Null") is True

    def test_na_variants(self):
        assert _is_null_like("N/A") is True
        assert _is_null_like("NA") is True
        assert _is_null_like("n/a") is True

    def test_unknown(self):
        assert _is_null_like("unknown") is True
        assert _is_null_like("UNKNOWN") is True

    def test_zero_string(self):
        assert _is_null_like("0") is True

    def test_real_values_not_null_like(self):
        assert _is_null_like("Person") is False
        assert _is_null_like("ACTIVE") is False
        assert _is_null_like("1") is False   # "1" is not in the null-like list
        assert _is_null_like("POL") is False


# ── _preprocess_top_values ────────────────────────────────────────────────────

class TestPreprocessTopValues:
    def test_filters_null_like(self):
        top_values = [
            {"value": "Person", "count": 200},
            {"value": "NULL", "count": 5},
            {"value": "", "count": 3},
            {"value": "N/A", "count": 1},
        ]
        result = _preprocess_top_values(top_values)
        values = [r["value"] for r in result]
        assert "Person" in values
        assert len([v for v in values if v in ("", "NULL", "N/A")]) == 0

    def test_deduplicates_case_variants(self):
        top_values = [
            {"value": "pol",  "count": 50},
            {"value": "POL",  "count": 30},
            {"value": "Pol",  "count": 20},
        ]
        result = _preprocess_top_values(top_values)
        # All three normalise to "Pol" (title case) — should collapse to one entry
        assert len(result) == 1

    def test_keeps_highest_count_representative(self):
        top_values = [
            {"value": "pol",  "count": 50},  # highest count
            {"value": "POL",  "count": 30},
            {"value": "Pol",  "count": 20},
        ]
        result = _preprocess_top_values(top_values)
        # Representative should have the original value from the highest-count entry
        assert result[0]["value"] == "pol"
        assert result[0]["count"] == 50

    def test_multiple_distinct_normalised_values_preserved(self):
        top_values = [
            {"value": "Person",       "count": 200},
            {"value": "ORGANISATION", "count": 100},
            {"value": "NULL",         "count": 5},
        ]
        result = _preprocess_top_values(top_values)
        assert len(result) == 2

    def test_empty_input(self):
        assert _preprocess_top_values([]) == []

    def test_all_null_like_returns_empty(self):
        top_values = [
            {"value": "NULL", "count": 10},
            {"value": "",     "count": 5},
        ]
        result = _preprocess_top_values(top_values)
        assert result == []


# ── _is_numeric_only_column ───────────────────────────────────────────────────

class TestIsNumericOnlyColumn:
    def test_all_numeric_returns_true(self):
        top_values = [
            {"value": "1", "count": 100},
            {"value": "2", "count": 80},
            {"value": "3", "count": 60},
        ]
        assert _is_numeric_only_column(top_values) is True

    def test_mixed_returns_false(self):
        top_values = [
            {"value": "Person", "count": 200},
            {"value": "1",      "count": 50},
        ]
        assert _is_numeric_only_column(top_values) is False

    def test_all_string_returns_false(self):
        top_values = [
            {"value": "Active",   "count": 300},
            {"value": "Inactive", "count": 50},
        ]
        assert _is_numeric_only_column(top_values) is False

    def test_null_like_values_ignored(self):
        top_values = [
            {"value": "NULL", "count": 5},
            {"value": "1",    "count": 100},
            {"value": "2",    "count": 80},
        ]
        # After filtering null-likes, only "1" and "2" remain — numeric-only
        assert _is_numeric_only_column(top_values) is True

    def test_empty_input_returns_false(self):
        assert _is_numeric_only_column([]) is False

    def test_decimal_values(self):
        top_values = [{"value": "1.5", "count": 10}, {"value": "2.3", "count": 5}]
        assert _is_numeric_only_column(top_values) is True

    def test_negative_numbers(self):
        top_values = [{"value": "-1", "count": 10}, {"value": "2", "count": 5}]
        assert _is_numeric_only_column(top_values) is True


# ── LLMStrategy.generate_value_mappings integration ──────────────────────────

@pytest.fixture()
def settings():
    return Settings(
        DATABASE_URL="mysql+pymysql://user:pass@localhost/test_db",
        OPENAI_API_KEY="test-key",
        LLM_STREAM=False,  # mocks return a single (non-streamed) response
    )


def _make_graph():
    """Minimal rdflib graph with one class URI."""
    import rdflib
    from rdflib.namespace import RDF, OWL
    g = rdflib.Graph()
    cls_uri = rdflib.URIRef("http://test.org/Person")
    g.add((cls_uri, RDF.type, OWL.Class))
    return g


def _col(name, top_values=None):
    return ColumnProfile(
        name=name, inferred_type="VARCHAR(20)",
        is_nullable=False, is_primary_key=False,
        null_rate=0.0, distinct_count=len(top_values or []),
        top_values=top_values or [],
    )


def _table(cols):
    return TableProfile(
        table_name="test_table",
        row_count=1000,
        columns={c.name: c for c in cols},
        foreign_keys=[],
    )


class TestGenerateValueMappingsRobustness:
    def test_numeric_only_column_skipped(self, settings):
        """Numeric-only columns must not be sent to the LLM."""
        strat = LLMStrategy(graph=_make_graph(), settings=settings)
        col = _col("type_code", top_values=[
            {"value": "1", "count": 100},
            {"value": "2", "count": 50},
        ])
        # Inject a mock client — it should NOT be called
        mock_client = MagicMock()
        strat._valid_class_uris = {"http://test.org/Person"}
        import openai
        original = openai.OpenAI

        called = []
        def patched_openai(**kwargs):
            called.append(True)
            return mock_client
        openai.OpenAI = patched_openai

        try:
            result = strat.generate_value_mappings(
                [("type_code", col)], _table([col])
            )
        finally:
            openai.OpenAI = original

        assert result == {}
        assert called == [], "LLM should NOT be called for numeric-only columns"

    def test_null_like_values_not_sent_to_llm(self, settings):
        """Null-like values should be stripped before the LLM sees them."""
        strat = LLMStrategy(graph=_make_graph(), settings=settings)
        col = _col("party_type", top_values=[
            {"value": "Person",       "count": 200},
            {"value": "Organisation", "count": 100},
            {"value": "NULL",         "count": 5},
            {"value": "N/A",          "count": 3},
        ])
        strat._valid_class_uris = {"http://test.org/Person"}

        captured_prompt = []

        import openai as _openai
        original = _openai.OpenAI

        def patched_openai(**kwargs):
            mock = MagicMock()
            resp = MagicMock()
            resp.choices[0].message.content = json.dumps({"columns": {}})
            resp.usage.prompt_tokens = 5
            resp.usage.completion_tokens = 5
            mock.chat.completions.create.side_effect = lambda **kw: (
                captured_prompt.append(kw["messages"][-1]["content"]) or resp
            )
            return mock

        _openai.OpenAI = patched_openai
        try:
            strat.generate_value_mappings([("party_type", col)], _table([col]))
        finally:
            _openai.OpenAI = original

        assert captured_prompt, "LLM should have been called"
        prompt_text = captured_prompt[0]
        assert "NULL" not in prompt_text
        assert "N/A" not in prompt_text

    def test_case_variants_collapsed_in_prompt(self, settings):
        """Mixed-case variants of the same value should appear once in the prompt."""
        strat = LLMStrategy(graph=_make_graph(), settings=settings)
        col = _col("status", top_values=[
            {"value": "pol",  "count": 50},
            {"value": "POL",  "count": 30},
            {"value": "Pol",  "count": 20},
        ])
        strat._valid_class_uris = {"http://test.org/Person"}

        captured_prompt = []

        import openai as _openai
        original = _openai.OpenAI

        def patched_openai(**kwargs):
            mock = MagicMock()
            resp = MagicMock()
            resp.choices[0].message.content = json.dumps({"columns": {}})
            resp.usage.prompt_tokens = 5
            resp.usage.completion_tokens = 5
            mock.chat.completions.create.side_effect = lambda **kw: (
                captured_prompt.append(kw["messages"][-1]["content"]) or resp
            )
            return mock

        _openai.OpenAI = patched_openai
        try:
            strat.generate_value_mappings([("status", col)], _table([col]))
        finally:
            _openai.OpenAI = original

        assert captured_prompt
        # "pol" should appear only once in the prompt (deduplicated)
        prompt_text = captured_prompt[0].lower()
        assert prompt_text.count("pol") == 1, (
            f"Expected 'pol' once after dedup, but prompt was:\n{prompt_text}"
        )

    def _make_llm_response(self, columns_data: dict):
        """Return a mock OpenAI client whose response contains the given columns_data."""
        import openai as _openai

        original = _openai.OpenAI

        def patched_openai(**kwargs):
            mock = MagicMock()
            resp = MagicMock()
            resp.choices[0].message.content = json.dumps({"columns": columns_data})
            resp.usage.prompt_tokens = 5
            resp.usage.completion_tokens = 5
            mock.chat.completions.create.return_value = resp
            return mock

        return original, patched_openai

    def test_all_same_uri_rules_dropped(self, settings):
        """When all rules for a column map to the same URI, the column must be
        excluded from the result so values pass through as string literals (issue #80)."""
        import openai as _openai

        strat = LLMStrategy(graph=_make_graph(), settings=settings)
        strat._valid_class_uris = {"http://test.org/Insurance"}

        col = _col("insurance", top_values=[
            {"value": "Medicare",  "count": 60},
            {"value": "Medicaid",  "count": 40},
            {"value": "Private",   "count": 20},
        ])

        # LLM returns all three values mapped to the same parent class URI
        columns_data = {
            "insurance": [
                {"source_value": "Medicare", "mapped_uri": "http://test.org/Insurance",
                 "confidence": 0.70, "rationale": "parent class"},
                {"source_value": "Medicaid", "mapped_uri": "http://test.org/Insurance",
                 "confidence": 0.70, "rationale": "parent class"},
                {"source_value": "Private",  "mapped_uri": "http://test.org/Insurance",
                 "confidence": 0.70, "rationale": "parent class"},
            ]
        }

        original, patched = self._make_llm_response(columns_data)
        _openai.OpenAI = patched
        try:
            result = strat.generate_value_mappings([("insurance", col)], _table([col]))
        finally:
            _openai.OpenAI = original

        assert "insurance" not in result, (
            "Column with all-same-URI rules should be excluded so values pass through as strings"
        )

    def test_distinct_uris_kept(self, settings):
        """When rules resolve to different URIs, the column must be retained."""
        import openai as _openai

        import rdflib
        from rdflib.namespace import RDF, OWL
        g = rdflib.Graph()
        for uri in ("http://test.org/Emergency", "http://test.org/Elective",
                    "http://test.org/Urgent"):
            g.add((rdflib.URIRef(uri), RDF.type, OWL.Class))

        strat = LLMStrategy(graph=g, settings=settings)
        strat._valid_class_uris = {
            "http://test.org/Emergency",
            "http://test.org/Elective",
            "http://test.org/Urgent",
        }

        col = _col("admission_type", top_values=[
            {"value": "EMERGENCY", "count": 80},
            {"value": "ELECTIVE",  "count": 30},
            {"value": "URGENT",    "count": 10},
        ])

        columns_data = {
            "admission_type": [
                {"source_value": "EMERGENCY", "mapped_uri": "http://test.org/Emergency",
                 "confidence": 0.95, "rationale": "exact match"},
                {"source_value": "ELECTIVE",  "mapped_uri": "http://test.org/Elective",
                 "confidence": 0.95, "rationale": "exact match"},
                {"source_value": "URGENT",    "mapped_uri": "http://test.org/Urgent",
                 "confidence": 0.95, "rationale": "exact match"},
            ]
        }

        original, patched = self._make_llm_response(columns_data)
        _openai.OpenAI = patched
        try:
            result = strat.generate_value_mappings([("admission_type", col)], _table([col]))
        finally:
            _openai.OpenAI = original

        assert "admission_type" in result
        assert len(result["admission_type"]) == 3
