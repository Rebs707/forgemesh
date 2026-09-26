# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for C7 — NL-to-SQL join correctness.

The SemanticQueryEngine calls an external LLM; we mock the openai module so
these tests run in CI without a live API key.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from semantic_mapper.validation.checks.nl_query import NLQueryCheck
from semantic_mapper.validation.context import NLQuestion, ValidationContext
from semantic_mapper.validation.models import CheckResult


# ── Mock helpers ──────────────────────────────────────────────────────────────

def _mock_openai_response(sql: str) -> MagicMock:
    """Return a mock ChatCompletion response whose content is a JSON SQL payload."""
    payload = json.dumps({"sql": sql, "explanation": "test", "tables_used": []})
    mock_choice = MagicMock()
    mock_choice.message.content = payload
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage.prompt_tokens = 10
    mock_response.usage.completion_tokens = 5
    return mock_response


def _make_settings_mock() -> MagicMock:
    s = MagicMock()
    s.QUERY_MODEL = "gpt-4o-mini"
    s.QUERY_MAX_ROWS = 100
    s.QUERY_DATASET_NOTES = ""
    s.OPENAI_API_KEY = "test-key"
    s.LLM_REQUEST_TIMEOUT = 30
    return s


# ── Pass tests ────────────────────────────────────────────────────────────────

class TestC7NLQueryPass:
    def test_skips_when_no_questions(self, good_ctx: ValidationContext) -> None:
        """No NL questions → check skipped (passes)."""
        result = NLQueryCheck().run(good_ctx)
        assert result.passed
        assert "skipped" in result.message

    def test_fails_when_no_settings(self, good_ctx: ValidationContext) -> None:
        """NL questions present but no settings → fails with informative message."""
        ctx = ValidationContext(
            project_name=good_ctx.project_name,
            graph_uri=good_ctx.graph_uri,
            sparql=good_ctx.sparql,
            gold_engine=good_ctx.gold_engine,
            mapping_report=good_ctx.mapping_report,
            ontology=good_ctx.ontology,
            baseline_run_id=good_ctx.baseline_run_id,
            nl_questions=[NLQuestion("How many airports?")],
            settings=None,
        )
        result = NLQueryCheck().run(ctx)
        assert not result.passed
        assert "settings" in result.message.lower()

    @patch("openai.OpenAI")
    def test_passes_with_non_empty_result(
        self, mock_openai_cls: MagicMock, good_ctx: ValidationContext
    ) -> None:
        """LLM emits valid SQL → check passes."""
        sql = "SELECT * FROM gold_airport LIMIT 10"
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _mock_openai_response(sql)

        ctx = ValidationContext(
            project_name=good_ctx.project_name,
            graph_uri=good_ctx.graph_uri,
            sparql=good_ctx.sparql,
            gold_engine=good_ctx.gold_engine,
            mapping_report=good_ctx.mapping_report,
            ontology=good_ctx.ontology,
            baseline_run_id=good_ctx.baseline_run_id,
            nl_questions=[NLQuestion("List all airports", expect_non_empty=True)],
            settings=_make_settings_mock(),
        )
        result = NLQueryCheck().run(ctx)
        assert result.passed, result.message


# ── Fail tests ────────────────────────────────────────────────────────────────

class TestC7NLQueryFail:
    @patch("openai.OpenAI")
    def test_fails_when_result_empty(
        self, mock_openai_cls: MagicMock, good_ctx: ValidationContext
    ) -> None:
        """LLM emits a broken join that returns zero rows → check fails."""
        sql = "SELECT * FROM gold_airport WHERE 1=0"
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = _mock_openai_response(sql)

        ctx = ValidationContext(
            project_name=good_ctx.project_name,
            graph_uri=good_ctx.graph_uri,
            sparql=good_ctx.sparql,
            gold_engine=good_ctx.gold_engine,
            mapping_report=good_ctx.mapping_report,
            ontology=good_ctx.ontology,
            baseline_run_id=good_ctx.baseline_run_id,
            nl_questions=[NLQuestion("List all airports", expect_non_empty=True)],
            settings=_make_settings_mock(),
        )
        result = NLQueryCheck().run(ctx)
        assert not result.passed
        assert "failed" in result.message.lower()

    @patch("openai.OpenAI")
    def test_multiple_questions_partial_failure(
        self, mock_openai_cls: MagicMock, good_ctx: ValidationContext
    ) -> None:
        """First question passes, second returns zero rows → 1 failure reported."""
        responses = [
            _mock_openai_response("SELECT * FROM gold_airport LIMIT 10"),
            _mock_openai_response("SELECT * FROM gold_airport WHERE 1=0"),
        ]
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_client.chat.completions.create.side_effect = responses

        ctx = ValidationContext(
            project_name=good_ctx.project_name,
            graph_uri=good_ctx.graph_uri,
            sparql=good_ctx.sparql,
            gold_engine=good_ctx.gold_engine,
            mapping_report=good_ctx.mapping_report,
            ontology=good_ctx.ontology,
            baseline_run_id=good_ctx.baseline_run_id,
            nl_questions=[
                NLQuestion("List all airports"),
                NLQuestion("Broken question"),
            ],
            settings=_make_settings_mock(),
        )
        result = NLQueryCheck().run(ctx)
        assert not result.passed
        assert len(result.details["failures"]) == 1
