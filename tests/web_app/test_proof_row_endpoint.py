# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for Phase 12 — POST /api/query/proof/row endpoint.

All phase internals are mocked. No live DB, Fuseki, or OpenAI key required.
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


@pytest.fixture(scope="module")
def client():
    import builtins
    orig = builtins.open

    def _patched(file, *a, **k):
        if "mapping_report" in str(file) or "semantic_mapping_report" in str(file):
            from io import StringIO
            return StringIO(_MOCK_REPORT_JSON)
        return orig(file, *a, **k)

    patches = [
        patch("builtins.open", side_effect=_patched),
        patch("semantic_mapper.gold.builder.GoldDefinitionBuilder.build", return_value=[]),
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


_VALID_ROW_BODY = {
    "question": "How many admissions by type?",
    "query": "SELECT ?t (COUNT(?a) AS ?n) WHERE { ?a a :Admission ; :hasType ?t . } GROUP BY ?t",
    "mode": "sparql",
    "answer": {"value": 10, "type": "count"},
    "row_grouping_values": {"t": "ELECTIVE"},
}


def _make_mock_engine():
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__enter__ = MagicMock(return_value=MagicMock())
    mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
    return mock_engine


def test_row_endpoint_happy_path(client):
    from semantic_mapper.prove.query_rewriter import RowProofQuery
    rq = RowProofQuery(
        'SELECT DISTINCT ?a WHERE { ?a a :Admission . FILTER(?t = "ELECTIVE") } LIMIT 1000',
        "sparql",
        {"t": "ELECTIVE"},
        False,
    )
    mock_engine = _make_mock_engine()
    with patch("web_app.rewrite_for_row_proof", return_value=rq), \
         patch("sqlalchemy.create_engine", return_value=mock_engine), \
         patch("web_app._engine._execute_sparql", return_value=[]), \
         patch("web_app.get_mapping_decisions", return_value=[]):
        resp = client.post("/api/query/proof/row", json=_VALID_ROW_BODY)
    assert resp.status_code == 200
    data = resp.json()
    assert "contributing_subjects" in data
    assert "limitations" in data
    assert "mapping_decisions_for_grouping" in data


def test_row_endpoint_422_for_underivable_row_proof(client):
    from semantic_mapper.prove.query_rewriter import ProofRewriteError
    with patch("web_app.rewrite_for_row_proof", side_effect=ProofRewriteError("HAVING", "HAVING")):
        resp = client.post("/api/query/proof/row", json=_VALID_ROW_BODY)
    assert resp.status_code == 422
    assert resp.json()["detail"]["pattern"] == "HAVING"
    assert resp.json()["detail"]["error"] == "unsupported_pattern"


def test_row_endpoint_503_for_bronze_unavailable(client):
    from semantic_mapper.prove.query_rewriter import RowProofQuery
    rq = RowProofQuery("SELECT DISTINCT ?a WHERE {...} LIMIT 1000", "sparql", {}, False)
    mock_engine = MagicMock()
    mock_engine.connect.side_effect = Exception("Connection refused")
    with patch("web_app.rewrite_for_row_proof", return_value=rq), \
         patch("sqlalchemy.create_engine", return_value=mock_engine):
        resp = client.post("/api/query/proof/row", json=_VALID_ROW_BODY)
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["error"] == "bronze_unavailable"


def test_row_endpoint_returns_required_limitations(client):
    from semantic_mapper.prove.models import REQUIRED_LIMITATIONS
    from semantic_mapper.prove.query_rewriter import RowProofQuery
    rq = RowProofQuery(
        'SELECT DISTINCT ?a WHERE {...} LIMIT 1000',
        "sparql",
        {"t": "ELECTIVE"},
        False,
    )
    mock_engine = _make_mock_engine()
    with patch("web_app.rewrite_for_row_proof", return_value=rq), \
         patch("sqlalchemy.create_engine", return_value=mock_engine), \
         patch("web_app._engine._execute_sparql", return_value=[]), \
         patch("web_app.get_mapping_decisions", return_value=[]):
        resp = client.post("/api/query/proof/row", json=_VALID_ROW_BODY)
    assert resp.status_code == 200
    limitations = resp.json()["limitations"]
    for lim in REQUIRED_LIMITATIONS:
        assert lim in limitations


def test_row_endpoint_mode_validation(client):
    """Invalid mode should produce 422 from the ValueError raised by rewrite_for_row_proof."""
    body = dict(_VALID_ROW_BODY, mode="bronze")
    resp = client.post("/api/query/proof/row", json=body)
    assert resp.status_code == 422


def test_row_endpoint_grouping_values_filter(client):
    """Mapping decisions should be filtered to grouping keys."""
    from semantic_mapper.prove.decision_lookup import MappingDecisionRef
    from semantic_mapper.prove.query_rewriter import RowProofQuery

    # Use a body with a clearly distinct grouping key
    body = dict(_VALID_ROW_BODY, row_grouping_values={"admission_type": "ELECTIVE"})
    rq = RowProofQuery(
        'SELECT DISTINCT ?a WHERE { FILTER(?admission_type = "ELECTIVE") } LIMIT 1000',
        "sparql",
        {"admission_type": "ELECTIVE"},
        False,
    )
    mock_engine = _make_mock_engine()

    d_relevant = MappingDecisionRef(
        uri="http://ont#hasAdmissionType",
        kind="DatatypeProperty",
        source="admissions.admission_type",
        winning_strategy="",
        score=0.9,
        confirmation="AUTO",
        confirmed_at=None,
    )
    d_irrelevant = MappingDecisionRef(
        uri="http://ont#hasDiagnosis",
        kind="DatatypeProperty",
        source="diagnoses.icd_code",
        winning_strategy="",
        score=0.8,
        confirmation="AUTO",
        confirmed_at=None,
    )

    with patch("web_app.rewrite_for_row_proof", return_value=rq), \
         patch("sqlalchemy.create_engine", return_value=mock_engine), \
         patch("web_app._engine._execute_sparql", return_value=[]), \
         patch("web_app.get_mapping_decisions", return_value=[d_relevant, d_irrelevant]):
        resp = client.post("/api/query/proof/row", json=body)
    assert resp.status_code == 200
    # Only decisions whose source contains 'admission_type' (grouping key) should be returned
    decisions = resp.json()["mapping_decisions_for_grouping"]
    uris = [d["uri"] for d in decisions]
    assert "http://ont#hasAdmissionType" in uris
    # The irrelevant one has source "diagnoses.icd_code" — no 'admission_type' key match
    assert "http://ont#hasDiagnosis" not in uris
