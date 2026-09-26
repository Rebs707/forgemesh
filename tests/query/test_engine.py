# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from semantic_mapper.query.engine import SemanticQueryEngine


# ---------------------------------------------------------------------------
# Helper to build a fake OpenAI response
# ---------------------------------------------------------------------------

def _make_llm_response(
    sql: str | None,
    explanation: str = "test explanation",
    tables_used: list | None = None,
    prompt_tokens: int = 50,
    completion_tokens: int = 30,
) -> MagicMock:
    """Return a mock that quacks like an openai ChatCompletion response."""
    content = json.dumps({
        "sql": sql,
        "explanation": explanation,
        "tables_used": tables_used or (["silver_raw_party"] if sql else []),
    })
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = content
    mock_response.usage.prompt_tokens = prompt_tokens
    mock_response.usage.completion_tokens = completion_tokens
    return mock_response


def _make_engine(simple_report, silver_engine, query_settings) -> SemanticQueryEngine:
    return SemanticQueryEngine(
        report=simple_report,
        silver_engine=silver_engine,
        settings=query_settings,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSemanticQueryEngineSuccessPath:
    def test_successful_query_returns_rows(self, simple_report, silver_engine, query_settings):
        engine = _make_engine(simple_report, silver_engine, query_settings)
        mock_response = _make_llm_response(
            sql="SELECT _ontology_class_uri FROM silver_raw_party LIMIT 10",
            tables_used=["silver_raw_party"],
        )
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_response

            result = engine.query("Show all class URIs")

        assert result.error is None
        assert result.row_count == 2
        assert len(result.rows) == 2
        assert result.tables_used == ["silver_raw_party"]

    def test_telemetry_accumulated(self, simple_report, silver_engine, query_settings):
        engine = _make_engine(simple_report, silver_engine, query_settings)
        mock_response = _make_llm_response(
            sql="SELECT * FROM silver_raw_party LIMIT 10",
            prompt_tokens=100,
            completion_tokens=40,
        )
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_response

            engine.query("First query")
            engine.query("Second query")

        assert engine.api_calls == 2
        assert engine.prompt_tokens == 200
        assert engine.completion_tokens == 80


class TestSemanticQueryEngineSafetyGuard:
    def test_dml_returns_error_not_raise(self, simple_report, silver_engine, query_settings):
        engine = _make_engine(simple_report, silver_engine, query_settings)
        mock_response = _make_llm_response(sql="DROP TABLE silver_raw_party")
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_response

            result = engine.query("Destroy everything")

        assert result.error is not None
        assert result.rows == []
        # Should not raise — error captured in QueryResult


class TestSemanticQueryEngineLimitEnforcement:
    def test_limit_injected_when_absent(self, simple_report, silver_engine, query_settings):
        engine = _make_engine(simple_report, silver_engine, query_settings)
        # SQL without LIMIT
        mock_response = _make_llm_response(
            sql="SELECT * FROM silver_raw_party"
        )
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_response

            result = engine.query("Show everything")

        assert "LIMIT" in result.generated_sql.upper()
        assert result.error is None

    def test_oversized_limit_capped(self, simple_report, silver_engine, query_settings):
        engine = _make_engine(simple_report, silver_engine, query_settings)
        # QUERY_MAX_ROWS=10 but SQL has LIMIT 9999
        mock_response = _make_llm_response(
            sql="SELECT * FROM silver_raw_party LIMIT 9999"
        )
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_response

            result = engine.query("Show everything")

        assert "LIMIT 10" in result.generated_sql
        assert "9999" not in result.generated_sql


class TestSemanticQueryEngineFailurePaths:
    def test_llm_api_failure_returns_error(self, simple_report, silver_engine, query_settings):
        engine = _make_engine(simple_report, silver_engine, query_settings)
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.side_effect = RuntimeError("network error")

            result = engine.query("Some question")

        assert result.error is not None
        assert "network error" in result.error
        assert result.rows == []

    def test_null_sql_returns_cannot_answer(self, simple_report, silver_engine, query_settings):
        engine = _make_engine(simple_report, silver_engine, query_settings)
        mock_response = _make_llm_response(
            sql=None,
            explanation="No matching table",
            tables_used=[],
        )
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_response

            result = engine.query("Something unanswerable")

        assert result.error is not None
        assert "Cannot answer" in result.error
        assert result.rows == []

    def test_db_execution_error_returns_error(self, simple_report, silver_engine, query_settings):
        engine = _make_engine(simple_report, silver_engine, query_settings)
        mock_response = _make_llm_response(
            sql="SELECT * FROM nonexistent_table LIMIT 10",
            tables_used=["nonexistent_table"],
        )
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_response

            result = engine.query("Bad table query")

        assert result.error is not None
        assert result.rows == []


class TestExtractTablesUsed:
    def test_from_clause(self):
        tables = SemanticQueryEngine._extract_tables_used(
            "SELECT * FROM silver_raw_party LIMIT 10"
        )
        assert tables == ["silver_raw_party"]

    def test_join_clause(self):
        tables = SemanticQueryEngine._extract_tables_used(
            "SELECT * FROM silver_raw_party JOIN silver_agreements ON id=id LIMIT 10"
        )
        assert "silver_raw_party" in tables
        assert "silver_agreements" in tables

    def test_backtick_identifiers(self):
        tables = SemanticQueryEngine._extract_tables_used(
            "SELECT * FROM `silver_raw_party` LIMIT 10"
        )
        assert "silver_raw_party" in tables

    def test_no_duplicates(self):
        tables = SemanticQueryEngine._extract_tables_used(
            "SELECT * FROM silver_raw_party a JOIN silver_raw_party b ON a.id=b.id"
        )
        assert tables.count("silver_raw_party") == 1
