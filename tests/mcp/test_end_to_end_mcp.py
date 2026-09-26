# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 8 — end-to-end MCP proof flow (prove_answer, get_proof_detail,
compose_agentic_proof).  No Docker, MySQL, or Fuseki required.
The /api/query/proof HTTP endpoint is mocked via unittest.mock.patch.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from semantic_mapper.config.settings import Settings
from semantic_mapper.mcp.query_store import (
    MCPQueryRecord,
    ensure_table,
    write_query,
)
from semantic_mapper.mcp.tools.prove_answer import run_prove_answer
from semantic_mapper.mcp.tools.get_proof_detail import run_get_proof_detail
from semantic_mapper.mcp.tools.compose_agentic_proof import run_compose_agentic_proof
from semantic_mapper.prove.models import REQUIRED_LIMITATIONS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store_settings(tmp_path):
    return Settings(
        DATABASE_URL="sqlite:///:memory:",
        SILVER_DATABASE_URL="sqlite:///:memory:",
        GOLD_DATABASE_URL="sqlite:///:memory:",
        CORRECTIONS_DB_PATH=str(tmp_path / "corrections.db"),
        FORGEMESH_WEB_APP_URL="http://localhost:8000",
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_record(query_id: str, question: str, query: str, mode: str = "gold") -> MCPQueryRecord:
    return MCPQueryRecord(
        query_id=query_id,
        session_id="sess_test",
        question=question,
        executed_query=query,
        mode=mode,
        answer={"rows": [{"count": 1247}], "row_count": 1},
        run_id="run_test",
        created_at=_now(),
    )


def _proof_record_json(question: str, subject_count: int = 1247) -> dict:
    """Realistic ProofRecord JSON matching the ProofRecord model."""
    return {
        "proof_schema_version": "1.0.0",
        "query_mode": "gold",
        "question": question,
        "answer": {"value": subject_count, "type": "count"},
        "interpretation": {
            "interpreted_as": question,
            "scope_assumptions": [],
            "not_included": [],
            "interpretation_confidence": "HIGH",
            "interpretation_confidence_reason": "",
            "narrated": True,
            "ontology_subgraph_attached": False,
        },
        "ontology_subgraph": {"classes": {}, "properties": {}, "adjacency_notes": []},
        "executed_query": "SELECT COUNT(*) FROM gold_admission",
        "proof_query": "SELECT _gold_id FROM gold_admission",
        "subject_count": subject_count,
        "subjects": [],
        "lineage": [],
        "mapping_decisions": [
            {"uri": "ont:hasAdmissionType", "confirmation": "HUMAN", "score": 0.99},
        ],
        "caveats": [],
        "limitations": list(REQUIRED_LIMITATIONS),
        "mapping_report_id": "run_test",
        "generated_at": _now(),
    }


def _mock_resp(json_data: dict, status_code: int = 200) -> MagicMock:
    m = MagicMock()
    m.ok = (status_code < 400)
    m.status_code = status_code
    m.json.return_value = json_data
    m.text = json.dumps(json_data)
    return m


# ---------------------------------------------------------------------------
# Test: prove_answer — verified path
# ---------------------------------------------------------------------------

def test_prove_answer_returns_proof_summary(store_settings):
    ensure_table(store_settings)
    record = _make_record("q_test001", "How many emergency admissions in Q2?",
                          "SELECT COUNT(*) FROM gold_admission WHERE admission_type='EMERGENCY'")
    write_query(record, store_settings)

    proof_json = _proof_record_json("How many emergency admissions in Q2?", subject_count=1247)

    with patch("semantic_mapper.mcp.tools.prove_answer.requests.post") as mock_post:
        mock_post.return_value = _mock_resp(proof_json)
        result = run_prove_answer("q_test001", store_settings)

    assert "error" not in result
    assert result["query_id"] == "q_test001"
    # v2: lineage summary is nested under lineage_summary
    summary = result["lineage_summary"]
    assert summary["proof_summary_version"] == "1.0.0"
    assert summary["data_audit_summary"]["subject_count"] == 1247
    assert all(lim in summary["limitations"] for lim in REQUIRED_LIMITATIONS)


def test_prove_answer_unknown_query_id(store_settings):
    ensure_table(store_settings)
    result = run_prove_answer("q_doesnotexist", store_settings)
    assert result["error"] == "unknown_query_id"
    assert result["query_id"] == "q_doesnotexist"


def test_prove_answer_bronze_unavailable(store_settings):
    """A genuine 503 bronze outage from the web app passes through as
    bronze_unavailable (a connection failure is webapp_unreachable — see below)."""
    ensure_table(store_settings)
    record = _make_record("q_test002", "count admissions", "SELECT COUNT(*) FROM gold_admission")
    write_query(record, store_settings)

    with patch("semantic_mapper.mcp.tools.prove_answer.requests.post") as mock_post:
        mock_post.return_value = _mock_resp(
            {"detail": {"error": "bronze_unavailable", "detail": "DB offline"}},
            status_code=503,
        )
        result = run_prove_answer("q_test002", store_settings)

    assert result["error"] == "bronze_unavailable"


def test_prove_answer_webapp_unreachable(store_settings):
    """A connection failure is a web-app problem, not a bronze outage."""
    import requests
    ensure_table(store_settings)
    record = _make_record("q_test004", "count admissions", "SELECT COUNT(*) FROM gold_admission")
    write_query(record, store_settings)

    with patch("semantic_mapper.mcp.tools.prove_answer.requests.post",
               side_effect=requests.exceptions.ConnectionError("refused")):
        result = run_prove_answer("q_test004", store_settings)

    assert result["error"] == "webapp_unreachable"


def test_prove_answer_unsupported_pattern(store_settings):
    ensure_table(store_settings)
    record = _make_record("q_test003", "complex HAVING query",
                          "SELECT x, COUNT(*) FROM gold_admission GROUP BY x HAVING COUNT(*) > 2")
    write_query(record, store_settings)

    with patch("semantic_mapper.mcp.tools.prove_answer.requests.post") as mock_post:
        mock_post.return_value = _mock_resp(
            {"detail": {"error": "unsupported_pattern", "pattern": "HAVING"}},
            status_code=422,
        )
        result = run_prove_answer("q_test003", store_settings)

    assert result["error"] == "unsupported_pattern"


# ---------------------------------------------------------------------------
# Test: get_proof_detail
# ---------------------------------------------------------------------------

def test_get_proof_detail_returns_full_record(store_settings):
    ensure_table(store_settings)
    record = _make_record("q_test010", "admission count", "SELECT COUNT(*) FROM gold_admission")
    write_query(record, store_settings)

    proof_json = _proof_record_json("admission count")

    with patch("semantic_mapper.mcp.tools.prove_answer.requests.post") as mock_post:
        mock_post.return_value = _mock_resp(proof_json)
        result = run_get_proof_detail("q_test010", store_settings)

    assert "error" not in result
    assert result["query_id"] == "q_test010"
    assert "lineage" in result
    assert "mapping_decisions" in result


def test_get_proof_detail_unknown_query_id(store_settings):
    ensure_table(store_settings)
    result = run_get_proof_detail("q_nope", store_settings)
    assert result["error"] == "unknown_query_id"


# ---------------------------------------------------------------------------
# Test: compose_agentic_proof — verified path
# ---------------------------------------------------------------------------

def test_agentic_multi_step_proof_verified_path(store_settings):
    ensure_table(store_settings)
    qid1 = "q_q2_admissions"
    qid2 = "q_q1_admissions"
    write_query(_make_record(qid1, "Q2 emergency admissions", "SELECT COUNT(*) FROM gold_admission"), store_settings)
    write_query(_make_record(qid2, "Q1 emergency admissions", "SELECT COUNT(*) FROM gold_admission"), store_settings)

    proof_q2 = _proof_record_json("Q2 emergency admissions", subject_count=1247)
    proof_q1 = _proof_record_json("Q1 emergency admissions", subject_count=1180)

    with patch("semantic_mapper.mcp.tools.prove_answer.requests.post") as mock_post:
        mock_post.side_effect = [_mock_resp(proof_q2), _mock_resp(proof_q1)]
        result = run_compose_agentic_proof(
            question="Are emergency admissions up vs Q1?",
            answer={"value": "Yes, 5.7% increase", "type": "comparison"},
            constituent_query_ids=[qid1, qid2],
            synthesis_step_input={
                "operation": "percentage_change",
                "description": "Computed (1247-1180)/1180*100",
                "inputs_used": {"current": 1247, "prior": 1180},
                "output": (1247 - 1180) / 1180 * 100,
            },
            settings=store_settings,
        )

    assert "error" not in result
    assert result["synthesis_step"]["operation_verified"] is True
    assert len(result["child_summaries"]) == 2
    assert not any("LLM_SYNTHESIS_UNVERIFIED" in c for c in result["caveats"])
    assert all(lim in result["limitations"] for lim in REQUIRED_LIMITATIONS)


def test_agentic_unverified_path(store_settings):
    ensure_table(store_settings)
    qid = "q_unverified"
    write_query(_make_record(qid, "some question", "SELECT 1"), store_settings)

    proof = _proof_record_json("some question")
    with patch("semantic_mapper.mcp.tools.prove_answer.requests.post") as mock_post:
        mock_post.return_value = _mock_resp(proof)
        result = run_compose_agentic_proof(
            question="Was this a good month?",
            answer={"value": "yes", "type": "judgment"},
            constituent_query_ids=[qid],
            synthesis_step_input={
                "operation": "contextual_judgment",
                "description": "Based on trend analysis",
                "inputs_used": {},
                "output": "yes",
            },
            settings=store_settings,
        )

    assert "error" not in result
    assert result["synthesis_step"]["operation_verified"] is False
    assert any("LLM_SYNTHESIS_UNVERIFIED" in c for c in result["caveats"])


def test_synthesis_mismatch_returns_error(store_settings):
    ensure_table(store_settings)
    qid1, qid2 = "q_mismatch1", "q_mismatch2"
    write_query(_make_record(qid1, "Q2", "SELECT COUNT(*) FROM gold_admission"), store_settings)
    write_query(_make_record(qid2, "Q1", "SELECT COUNT(*) FROM gold_admission"), store_settings)

    proof = _proof_record_json("Q2")
    with patch("semantic_mapper.mcp.tools.prove_answer.requests.post") as mock_post:
        mock_post.return_value = _mock_resp(proof)
        result = run_compose_agentic_proof(
            question="Change?",
            answer={"value": "50%"},
            constituent_query_ids=[qid1, qid2],
            synthesis_step_input={
                "operation": "percentage_change",
                "description": "Wrong arithmetic",
                "inputs_used": {"current": 1247, "prior": 1180},
                "output": 50.0,   # wrong — actual is ~5.68%
            },
            settings=store_settings,
        )

    assert result["error"] == "synthesis_mismatch"
