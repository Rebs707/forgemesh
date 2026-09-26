# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from semantic_mapper.rdf.query.engine import SparqlQueryEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_llm_response(
    sparql: str | None,
    explanation: str = "test explanation",
    classes_used: list | None = None,
    prompt_tokens: int = 50,
    completion_tokens: int = 30,
) -> MagicMock:
    """Return a mock that quacks like an openai ChatCompletion response."""
    content = json.dumps({
        "sparql": sparql,
        "explanation": explanation,
        "classes_used": classes_used or (["Patient"] if sparql else []),
    })
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = content
    mock_response.usage.prompt_tokens = prompt_tokens
    mock_response.usage.completion_tokens = completion_tokens
    return mock_response


def _make_fuseki_response(vars_: list, bindings: list) -> MagicMock:
    """Return a mock that quacks like a requests.Response for SPARQL results JSON."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "head": {"vars": vars_},
        "results": {"bindings": bindings},
    }
    return mock_resp


def _make_engine(simple_definitions, sparql_settings) -> SparqlQueryEngine:
    return SparqlQueryEngine(definitions=simple_definitions, settings=sparql_settings)


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------

class TestSparqlQueryEngineSuccessPath:
    def test_successful_query_returns_rows(self, simple_definitions, sparql_settings):
        engine = _make_engine(simple_definitions, sparql_settings)
        mock_llm = _make_llm_response(
            sparql=(
                "SELECT ?s WHERE { GRAPH <http://mesh.local/graph/current> "
                "{ ?s a <http://ex.org/ontology#Patient> } } LIMIT 10"
            ),
            classes_used=["Patient"],
        )
        fuseki_mock = _make_fuseki_response(
            vars_=["s"],
            bindings=[
                {"s": {"value": "http://mesh.local/data/patient/P1", "type": "uri"}},
                {"s": {"value": "http://mesh.local/data/patient/P2", "type": "uri"}},
            ],
        )
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_llm

            with patch("requests.post") as mock_post:
                mock_post.return_value = fuseki_mock
                result = engine.query("How many patients?")

        assert result.error is None
        assert result.row_count == 2
        assert len(result.rows) == 2
        assert result.tables_used == ["Patient"]

    def test_sparql_stored_in_generated_sql(self, simple_definitions, sparql_settings):
        engine = _make_engine(simple_definitions, sparql_settings)
        sparql = "SELECT ?s WHERE { GRAPH <http://mesh.local/graph/current> { ?s a <http://ex.org/ontology#Patient> } } LIMIT 10"
        mock_llm = _make_llm_response(sparql=sparql)
        fuseki_mock = _make_fuseki_response(vars_=["s"], bindings=[])
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_llm
            with patch("requests.post") as mock_post:
                mock_post.return_value = fuseki_mock
                result = engine.query("List patients")

        assert sparql in result.generated_sql

    def test_telemetry_accumulated(self, simple_definitions, sparql_settings):
        engine = _make_engine(simple_definitions, sparql_settings)
        mock_llm = _make_llm_response(
            sparql="SELECT ?s WHERE { GRAPH <http://mesh.local/graph/current> { ?s a <http://ex.org/ontology#Patient> } } LIMIT 10",
            prompt_tokens=100,
            completion_tokens=40,
        )
        # One row, so the zero-rows retry (a second LLM call) does not fire.
        fuseki_mock = _make_fuseki_response(
            vars_=["s"], bindings=[{"s": {"type": "uri", "value": "http://ex.org/p1"}}],
        )
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_llm
            with patch("requests.post") as mock_post:
                mock_post.return_value = fuseki_mock
                engine.query("First query")
                engine.query("Second query")

        assert engine.api_calls == 2
        assert engine.prompt_tokens == 200
        assert engine.completion_tokens == 80

    def test_turn_count_increments(self, simple_definitions, sparql_settings):
        engine = _make_engine(simple_definitions, sparql_settings)
        assert engine.turn_count == 0
        mock_llm = _make_llm_response(
            sparql="SELECT ?s WHERE { GRAPH <http://mesh.local/graph/current> { ?s a <http://ex.org/ontology#Patient> } } LIMIT 10"
        )
        fuseki_mock = _make_fuseki_response(vars_=["s"], bindings=[])
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_llm
            with patch("requests.post") as mock_post:
                mock_post.return_value = fuseki_mock
                engine.query("Q1")
                engine.query("Q2")

        assert engine.turn_count == 2

    def test_reset_conversation_clears_history(self, simple_definitions, sparql_settings):
        engine = _make_engine(simple_definitions, sparql_settings)
        mock_llm = _make_llm_response(
            sparql="SELECT ?s WHERE { GRAPH <http://mesh.local/graph/current> { ?s a <http://ex.org/ontology#Patient> } } LIMIT 10"
        )
        fuseki_mock = _make_fuseki_response(vars_=["s"], bindings=[])
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_llm
            with patch("requests.post") as mock_post:
                mock_post.return_value = fuseki_mock
                engine.query("Q1")

        assert engine.turn_count == 1
        engine.reset_conversation()
        assert engine.turn_count == 0

    def test_null_binding_value_becomes_none(self, simple_definitions, sparql_settings):
        """A binding that omits a variable should produce None for that key."""
        engine = _make_engine(simple_definitions, sparql_settings)
        mock_llm = _make_llm_response(
            sparql="SELECT ?s ?d WHERE { GRAPH <http://mesh.local/graph/current> { ?s a <http://ex.org/ontology#Patient> } } LIMIT 10",
            classes_used=["Patient"],
        )
        # Binding for ?s present, ?d absent
        fuseki_mock = _make_fuseki_response(
            vars_=["s", "d"],
            bindings=[{"s": {"value": "http://mesh.local/data/patient/P1", "type": "uri"}}],
        )
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_llm
            with patch("requests.post") as mock_post:
                mock_post.return_value = fuseki_mock
                result = engine.query("Patients with optional DOB")

        assert result.error is None
        assert result.rows[0]["d"] is None


# ---------------------------------------------------------------------------
# Safety guard
# ---------------------------------------------------------------------------

class TestSparqlSafetyGuard:
    def test_construct_returns_error_not_raise(self, simple_definitions, sparql_settings):
        engine = _make_engine(simple_definitions, sparql_settings)
        mock_llm = _make_llm_response(sparql="CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }")
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_llm

            result = engine.query("Describe everything")

        assert result.error is not None
        assert result.rows == []

    def test_insert_returns_error(self, simple_definitions, sparql_settings):
        engine = _make_engine(simple_definitions, sparql_settings)
        mock_llm = _make_llm_response(
            sparql="SELECT ?s WHERE { ?s ?p ?o } INSERT DATA { <a> <b> <c> }"
        )
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_llm

            result = engine.query("Modify data")

        assert result.error is not None
        assert result.rows == []


# ---------------------------------------------------------------------------
# LIMIT enforcement
# ---------------------------------------------------------------------------

class TestLimitEnforcement:
    def test_limit_injected_when_absent(self, simple_definitions, sparql_settings):
        engine = _make_engine(simple_definitions, sparql_settings)
        mock_llm = _make_llm_response(
            sparql="SELECT ?s WHERE { GRAPH <http://mesh.local/graph/current> { ?s a <http://ex.org/ontology#Patient> } }"
        )
        fuseki_mock = _make_fuseki_response(vars_=["s"], bindings=[])
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_llm
            with patch("requests.post") as mock_post:
                mock_post.return_value = fuseki_mock
                result = engine.query("Show all patients")

        assert "LIMIT" in result.generated_sql.upper()
        assert result.error is None

    def test_oversized_limit_capped(self, simple_definitions, sparql_settings):
        engine = _make_engine(simple_definitions, sparql_settings)
        # QUERY_MAX_ROWS=10 but SPARQL has LIMIT 9999
        mock_llm = _make_llm_response(
            sparql="SELECT ?s WHERE { GRAPH <http://mesh.local/graph/current> { ?s a <http://ex.org/ontology#Patient> } } LIMIT 9999"
        )
        fuseki_mock = _make_fuseki_response(vars_=["s"], bindings=[])
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_llm
            with patch("requests.post") as mock_post:
                mock_post.return_value = fuseki_mock
                result = engine.query("Show all patients")

        assert "LIMIT 10" in result.generated_sql
        assert "9999" not in result.generated_sql


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------

class TestSparqlEngineFailurePaths:
    def test_llm_api_failure_returns_error(self, simple_definitions, sparql_settings):
        engine = _make_engine(simple_definitions, sparql_settings)
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.side_effect = RuntimeError("network error")

            result = engine.query("Some question")

        assert result.error is not None
        assert "network error" in result.error
        assert result.rows == []

    def test_null_sparql_returns_cannot_answer(self, simple_definitions, sparql_settings):
        engine = _make_engine(simple_definitions, sparql_settings)
        mock_llm = _make_llm_response(
            sparql=None,
            explanation="No matching class",
            classes_used=[],
        )
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_llm

            result = engine.query("Something unanswerable")

        assert result.error is not None
        assert "Cannot answer" in result.error
        assert result.rows == []

    def test_fuseki_http_error_returns_error(self, simple_definitions, sparql_settings):
        engine = _make_engine(simple_definitions, sparql_settings)
        mock_llm = _make_llm_response(
            sparql="SELECT ?s WHERE { GRAPH <http://mesh.local/graph/current> { ?s a <http://ex.org/ontology#Patient> } } LIMIT 10"
        )
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_llm

            with patch("requests.post") as mock_post:
                mock_post.side_effect = RuntimeError("connection refused")

                result = engine.query("Failing query")

        assert result.error is not None
        assert "connection refused" in result.error
        assert result.rows == []

    def test_fuseki_raise_for_status_returns_error(self, simple_definitions, sparql_settings):
        """raise_for_status() triggers on 4xx/5xx responses."""
        from requests.exceptions import HTTPError

        engine = _make_engine(simple_definitions, sparql_settings)
        mock_llm = _make_llm_response(
            sparql="SELECT ?s WHERE { GRAPH <http://mesh.local/graph/current> { ?s a <http://ex.org/ontology#Patient> } } LIMIT 10"
        )
        with patch("openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_openai_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = mock_llm

            with patch("requests.post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.ok = False
                mock_resp.status_code = 400
                mock_resp.text = "Parse error: unexpected token"
                mock_post.return_value = mock_resp

                result = engine.query("Bad SPARQL")

        assert result.error is not None
        assert result.rows == []
