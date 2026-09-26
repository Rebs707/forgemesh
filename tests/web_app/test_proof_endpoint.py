# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for Phase 9 — POST /api/query/proof and GET /api/query/proof/supported-patterns.

All phase internals (rewrite, lineage, decisions, narrator, composer) are mocked.
No live DB, Fuseki, or OpenAI key required.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Patch out the expensive bootstrap before importing web_app
# ---------------------------------------------------------------------------

_MOCK_REPORT_JSON = json.dumps({
    "schema_version": "1.3.0",
    "ontology_uri": "http://mesh.local/ontology",
    "database_name": "bronze_db",
    "settings_used": {},
    "tables": [],
    "total_pending_review": 0,
})

_MOCK_GOLD_DEFINITIONS = []


def _make_proof_record_dict() -> dict:
    from semantic_mapper.prove.models import REQUIRED_LIMITATIONS
    return {
        "proof_schema_version": "1.0.0",
        "query_mode": "sparql",
        "question": "How many admissions?",
        "answer": {"value": 51, "type": "count"},
        "interpretation": {
            "interpreted_as": "Admission events.",
            "scope_assumptions": [],
            "not_included": [],
            "interpretation_confidence": "HIGH",
            "interpretation_confidence_reason": "Clear.",
            "narrated": True,
            "ontology_subgraph_attached": False,
        },
        "ontology_subgraph": {"classes": {}, "properties": {}, "adjacency_notes": []},
        "executed_query": "SELECT (COUNT(?a) AS ?n) WHERE { ?a a :Admission . }",
        "proof_query": "SELECT DISTINCT ?a WHERE { ?a a :Admission . } LIMIT 1000",
        "subject_count": 1,
        "subjects": ["http://mesh.local/data/admission/1"],
        "lineage": [],
        "mapping_decisions": [],
        "caveats": [],
        "limitations": list(REQUIRED_LIMITATIONS),
        "mapping_report_id": "run_001",
        "generated_at": "2026-05-05T12:00:00+00:00",
    }


@pytest.fixture(scope="module")
def client():
    import builtins
    original_open = builtins.open

    def _patched_open(file, *args, **kwargs):
        if "mapping_report" in str(file) or "semantic_mapping_report" in str(file):
            from io import StringIO
            return StringIO(_MOCK_REPORT_JSON)
        return original_open(file, *args, **kwargs)

    patches = [
        patch("builtins.open", side_effect=_patched_open),
        patch("semantic_mapper.gold.builder.GoldDefinitionBuilder.build", return_value=_MOCK_GOLD_DEFINITIONS),
        patch("semantic_mapper.rdf.query.engine.SparqlQueryEngine.__init__", return_value=None),
        patch("semantic_mapper.rdf.query.engine.SparqlQueryEngine._execute_sparql", return_value=[]),
        patch("semantic_mapper.rdf.query.engine.SparqlQueryEngine._schema_context", "", create=True),
    ]

    for p in patches:
        p.start()

    try:
        import web_app
        from web_app import app
        yield TestClient(app)
    finally:
        for p in patches:
            p.stop()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

_PROOF_URL = "/api/query/proof"
_VALID_BODY = {
    "question": "How many admissions?",
    "query": "SELECT (COUNT(?a) AS ?n) WHERE { ?a a :Admission . }",
    "mode": "sparql",
    "answer": {"value": 51, "type": "count"},
    "run_id": "run_001",
}


def _mock_all_phases(proof_record_dict: dict | None = None):
    """Context manager that mocks all phase internals."""
    from unittest.mock import patch as _patch

    from semantic_mapper.prove.query_rewriter import ProofQuery

    if proof_record_dict is None:
        proof_record_dict = _make_proof_record_dict()

    pq = ProofQuery(
        proof_query="SELECT DISTINCT ?a WHERE { ?a a :Admission . } LIMIT 1000",
        mode="sparql", sampled=False, original_count=51, sample_limit=1000,
    )

    from semantic_mapper.prove.models import ProofRecord
    proof_record = ProofRecord(**proof_record_dict)

    class _CM:
        def __enter__(self):
            self._patches = [
                _patch("web_app.rewrite", return_value=pq),
                _patch("web_app._engine._execute_sparql", return_value=[
                    {"a": "http://mesh.local/data/admission/1"}
                ]),
                _patch("web_app.LineageWalker"),
                _patch("web_app.get_mapping_decisions", return_value=[]),
                _patch("web_app.extract_subgraph"),
                _patch("web_app.narrate", return_value=(MagicMock(), [])),
                _patch("web_app.compose_proof", return_value=proof_record),
                _patch("web_app.render_proof", return_value="# proof markdown"),
                _patch("web_app._gold_def_for_subjects", return_value=None),
                _patch(
                    "sqlalchemy.create_engine",
                    return_value=MagicMock(__enter__=MagicMock(return_value=MagicMock()), __exit__=MagicMock(return_value=False)),
                ),
            ]
            # Make the engine mock work as a context manager
            mock_engine = MagicMock()
            mock_conn = MagicMock()
            mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
            for p in self._patches:
                p.start()
            return self

        def __exit__(self, *args):
            for p in self._patches:
                p.stop()

    return _CM()


# ---------------------------------------------------------------------------
# GET /api/query/proof/supported-patterns
# ---------------------------------------------------------------------------

class TestSupportedPatterns:
    def test_returns_200(self, client):
        resp = client.get("/api/query/proof/supported-patterns")
        assert resp.status_code == 200

    def test_sparql_key_present(self, client):
        resp = client.get("/api/query/proof/supported-patterns")
        assert "sparql" in resp.json()

    def test_sql_key_present(self, client):
        resp = client.get("/api/query/proof/supported-patterns")
        assert "sql" in resp.json()

    def test_unsupported_key_present(self, client):
        resp = client.get("/api/query/proof/supported-patterns")
        assert "unsupported" in resp.json()


# ---------------------------------------------------------------------------
# POST /api/query/proof — 422 unsupported pattern
# ---------------------------------------------------------------------------

class TestUnsupportedPattern:
    def test_having_returns_422(self, client):
        from semantic_mapper.prove.query_rewriter import ProofRewriteError
        body = {**_VALID_BODY, "query": "SELECT ?w (COUNT(?p) AS ?n) WHERE { ?p :inWard ?w . } GROUP BY ?w HAVING (?n > 5)"}
        with patch("web_app.rewrite", side_effect=ProofRewriteError("HAVING not supported", "HAVING")):
            resp = client.post(_PROOF_URL, json=body)
        assert resp.status_code == 422

    def test_422_detail_contains_pattern(self, client):
        from semantic_mapper.prove.query_rewriter import ProofRewriteError
        with patch("web_app.rewrite", side_effect=ProofRewriteError("MINUS not supported", "MINUS")):
            resp = client.post(_PROOF_URL, json=_VALID_BODY)
        detail = resp.json()["detail"]
        assert detail["pattern"] == "MINUS"

    def test_422_error_field(self, client):
        from semantic_mapper.prove.query_rewriter import ProofRewriteError
        with patch("web_app.rewrite", side_effect=ProofRewriteError("X", "HAVING")):
            resp = client.post(_PROOF_URL, json=_VALID_BODY)
        assert resp.json()["detail"]["error"] == "unsupported_pattern"

    def test_unknown_mode_returns_422(self, client):
        body = {**_VALID_BODY, "mode": "bronze"}
        resp = client.post(_PROOF_URL, json=body)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/query/proof — 503 bronze unavailable
# ---------------------------------------------------------------------------

class TestBronzeUnavailable:
    def test_returns_503(self, client):
        from semantic_mapper.prove.query_rewriter import ProofQuery
        pq = ProofQuery("SELECT DISTINCT ?a WHERE {...} LIMIT 1000", "sparql", False, None, 1000)

        mock_engine = MagicMock()
        mock_engine.connect.side_effect = Exception("Connection refused")

        with patch("web_app.rewrite", return_value=pq), \
             patch("sqlalchemy.create_engine", return_value=mock_engine):
            resp = client.post(_PROOF_URL, json=_VALID_BODY)
        assert resp.status_code == 503

    def test_503_detail_error_field(self, client):
        from semantic_mapper.prove.query_rewriter import ProofQuery
        pq = ProofQuery("SELECT DISTINCT ?a WHERE {...} LIMIT 1000", "sparql", False, None, 1000)

        mock_engine = MagicMock()
        mock_engine.connect.side_effect = Exception("timeout")

        with patch("web_app.rewrite", return_value=pq), \
             patch("sqlalchemy.create_engine", return_value=mock_engine):
            resp = client.post(_PROOF_URL, json=_VALID_BODY)
        assert resp.json()["detail"]["error"] == "bronze_unavailable"


# ---------------------------------------------------------------------------
# POST /api/query/proof — successful JSON response
# ---------------------------------------------------------------------------

class TestSuccessfulJson:
    def test_returns_200(self, client):
        with _mock_all_phases():
            resp = client.post(_PROOF_URL, json=_VALID_BODY)
        assert resp.status_code == 200

    def test_response_has_proof_schema_version(self, client):
        with _mock_all_phases():
            resp = client.post(_PROOF_URL, json=_VALID_BODY)
        assert resp.json()["proof_schema_version"] == "1.0.0"

    def test_response_has_limitations(self, client):
        with _mock_all_phases():
            resp = client.post(_PROOF_URL, json=_VALID_BODY)
        from semantic_mapper.prove.models import REQUIRED_LIMITATIONS
        for lim in REQUIRED_LIMITATIONS:
            assert lim in resp.json()["limitations"]

    def test_response_is_json_by_default(self, client):
        with _mock_all_phases():
            resp = client.post(_PROOF_URL, json=_VALID_BODY)
        assert resp.headers["content-type"].startswith("application/json")

    def test_question_preserved_in_response(self, client):
        with _mock_all_phases():
            resp = client.post(_PROOF_URL, json=_VALID_BODY)
        assert resp.json()["question"] == "How many admissions?"


# ---------------------------------------------------------------------------
# POST /api/query/proof — Accept: text/markdown
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# queried_uris — Phase 13
# ---------------------------------------------------------------------------

class TestQueriedUris:
    """Tests for _queried_uris helper in composer.py."""

    def test_sparql_extracts_class_and_property_uris(self):
        from semantic_mapper.prove.composer import _queried_uris
        sparql = (
            "SELECT DISTINCT ?adm WHERE { "
            "?adm a <http://example.org/ont#Admission> . "
            "?adm <http://example.org/ont#hasFlag> 1 . "
            "}"
        )
        result = _queried_uris(sparql, "sparql", [])
        assert "http://example.org/ont#Admission" in result
        assert "http://example.org/ont#hasFlag" in result

    def test_sparql_deduplicates(self):
        from semantic_mapper.prove.composer import _queried_uris
        sparql = (
            "SELECT ?a WHERE { "
            "?a a <http://example.org/ont#Admission> . "
            "?b a <http://example.org/ont#Admission> . "
            "}"
        )
        result = _queried_uris(sparql, "sparql", [])
        assert result.count("http://example.org/ont#Admission") == 1

    def test_sparql_empty_query_returns_empty(self):
        from semantic_mapper.prove.composer import _queried_uris
        result = _queried_uris("SELECT * WHERE { }", "sparql", [])
        assert result == []

    def test_sql_matches_decision_by_source_column(self):
        from semantic_mapper.prove.composer import _queried_uris
        from semantic_mapper.prove.decision_lookup import MappingDecisionRef

        d = MappingDecisionRef(
            uri="http://example.org/ont#hasExpireFlag",
            kind="data_property",
            source="raw_admissions.hospital_expire_flag",
            winning_strategy="HistoryMatcher",
            score=0.95,
            confirmation="HUMAN",
            confirmed_at="2026-01-01",
            evidence_trail=[],
        )
        sql = "SELECT hospital_expire_flag, COUNT(*) FROM gold_admission WHERE hospital_expire_flag = 1"
        result = _queried_uris(sql, "gold", [d])
        assert "http://example.org/ont#hasExpireFlag" in result

    def test_sql_ignores_unrelated_columns(self):
        from semantic_mapper.prove.composer import _queried_uris
        from semantic_mapper.prove.decision_lookup import MappingDecisionRef

        d = MappingDecisionRef(
            uri="http://example.org/ont#hasName",
            kind="data_property",
            source="raw_patients.patient_name",
            winning_strategy="AnnotationMatcher",
            score=0.80,
            confirmation="AUTO",
            confirmed_at=None,
            evidence_trail=[],
        )
        sql = "SELECT hospital_expire_flag FROM gold_admission WHERE hospital_expire_flag = 1"
        result = _queried_uris(sql, "gold", [d])
        assert "http://example.org/ont#hasName" not in result

    def test_proof_record_has_queried_uris_field(self):
        from semantic_mapper.prove.models import ProofRecord, REQUIRED_LIMITATIONS
        record = ProofRecord(
            query_mode="sparql",
            question="How many?",
            answer={"value": 1, "type": "count"},
            interpretation={
                "interpreted_as": "", "scope_assumptions": [], "not_included": [],
                "interpretation_confidence": "LOW", "interpretation_confidence_reason": "",
                "narrated": False, "ontology_subgraph_attached": False,
            },
            ontology_subgraph={"classes": {}, "properties": {}, "adjacency_notes": []},
            executed_query="SELECT * WHERE {}",
            proof_query="SELECT * WHERE {} LIMIT 1000",
            subject_count=0,
            subjects=[],
            lineage=[],
            mapping_decisions=[],
            caveats=[],
            limitations=list(REQUIRED_LIMITATIONS),
            mapping_report_id="run_001",
            generated_at="2026-01-01T00:00:00+00:00",
        )
        assert record.queried_uris == []

    def test_proof_record_accepts_queried_uris(self):
        from semantic_mapper.prove.models import ProofRecord, REQUIRED_LIMITATIONS
        record = ProofRecord(
            query_mode="sparql",
            question="How many?",
            answer={"value": 1, "type": "count"},
            interpretation={
                "interpreted_as": "", "scope_assumptions": [], "not_included": [],
                "interpretation_confidence": "LOW", "interpretation_confidence_reason": "",
                "narrated": False, "ontology_subgraph_attached": False,
            },
            ontology_subgraph={"classes": {}, "properties": {}, "adjacency_notes": []},
            executed_query="SELECT * WHERE {}",
            proof_query="SELECT * WHERE {} LIMIT 1000",
            subject_count=0,
            subjects=[],
            lineage=[],
            mapping_decisions=[],
            caveats=[],
            limitations=list(REQUIRED_LIMITATIONS),
            mapping_report_id="run_001",
            generated_at="2026-01-01T00:00:00+00:00",
            queried_uris=["http://example.org/ont#Admission"],
        )
        assert "http://example.org/ont#Admission" in record.queried_uris


class TestMarkdownAccept:
    def test_returns_markdown_when_accepted(self, client):
        with _mock_all_phases():
            resp = client.post(
                _PROOF_URL, json=_VALID_BODY,
                headers={"accept": "text/markdown"},
            )
        assert resp.status_code == 200
        assert "text/markdown" in resp.headers["content-type"]

    def test_markdown_body_is_string(self, client):
        with _mock_all_phases():
            resp = client.post(
                _PROOF_URL, json=_VALID_BODY,
                headers={"accept": "text/markdown"},
            )
        assert isinstance(resp.text, str)
        assert "proof markdown" in resp.text
