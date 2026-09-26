# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""prove_answer MCP tool.

Returns a structured proof for a previous query. Two proof layers:

  semantic_proof  — Feature 12 ontology-grounded ProofRecord v2.0.0.
                    Mechanically derived from SPARQL + ontology; no LLM.
                    Always present for SPARQL-mode queries.

  lineage_summary — Feature 02 v1 bronze-lineage ProofSummary.
                    Derived from the pipeline lineage walk via the web app.
                    May be null if the web app is unavailable or the query
                    mode does not support lineage (e.g. non-SPARQL modes).

All errors are caught and returned as an error dict — never raises.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

import requests

from semantic_mapper.config.settings import Settings
from semantic_mapper.mcp.query_store import read_query


def run_prove_answer(query_id: str, settings: Settings) -> Dict[str, Any]:
    """Return a combined proof for query_id, or an error dict on failure."""
    record = read_query(query_id, settings)
    if record is None:
        return {"error": "unknown_query_id", "query_id": query_id}

    # Layer 1: Feature 12 semantic proof (stored at query time).
    # Use getattr defensively: records stored before the proof_record_json column
    # existed (or written by an older MCPQueryRecord) will not have the attribute.
    semantic_proof: Optional[Dict[str, Any]] = None
    raw_proof_json = getattr(record, "proof_record_json", None)
    if raw_proof_json:
        try:
            semantic_proof = json.loads(raw_proof_json)
        except Exception:
            pass

    # Layer 2: Feature 02 v1 lineage summary (live call to web app)
    lineage_summary: Optional[Dict[str, Any]] = None
    lineage_error: Optional[str] = None
    lineage_error_result: Optional[Dict[str, Any]] = None
    _v1_proof_dict: Optional[Dict[str, Any]] = None  # kept for presentation builder
    try:
        proof_dict = _call_proof_endpoint(record, settings)
        if "error" in proof_dict:
            lineage_error = proof_dict.get("error")
            lineage_error_result = proof_dict
        else:
            _v1_proof_dict = proof_dict
            from semantic_mapper.prove.models import ProofRecord as V1ProofRecord
            from semantic_mapper.prove.summary import summarise_proof
            v1_record = V1ProofRecord.model_validate(proof_dict)
            summary = summarise_proof(v1_record, query_id, settings)
            lineage_summary = summary.to_dict()
    except Exception as exc:
        lineage_error = str(exc)

    if semantic_proof is None and lineage_summary is None:
        # No proof available from either layer. Distinguish between a connectivity
        # failure (fixable config), the web app reporting its bronze DB down, and a
        # genuinely unsupported query.
        if lineage_error == "bronze_unavailable" and lineage_error_result is not None:
            return lineage_error_result
        _network_errors = {"webapp_unreachable", "proof_timeout", "proof_failed"}
        if lineage_error in _network_errors:
            webapp_url = getattr(settings, "FORGEMESH_WEB_APP_URL", "http://localhost:5173")
            return {
                "error": "webapp_unreachable",
                "detail": (
                    f"Could not reach the ForgeMesh webapp at {webapp_url}. "
                    "The webapp must be running (./start-forgemesh.sh) for lineage proofs. "
                    "If the URL is wrong, set FORGEMESH_WEB_APP_URL in your .env file. "
                    "No cached semantic proof was found for this query_id — "
                    "re-run the query to generate one."
                ),
                "query_id": query_id,
            }
        if lineage_error == "unsupported_pattern":
            return _call_proof_endpoint(record, settings)  # get the pattern detail
        return {
            "error": "proof_unavailable",
            "detail": (
                "No semantic proof stored for this query_id "
                "(only SPARQL-mode queries generate semantic proofs). "
                f"Lineage error: {lineage_error}"
            ),
            "query_id": query_id,
        }

    result: Dict[str, Any] = {"query_id": query_id}
    if semantic_proof is not None:
        result["semantic_proof"] = semantic_proof
    if lineage_summary is not None:
        result["lineage_summary"] = lineage_summary
    elif lineage_error:
        result["lineage_note"] = f"Lineage unavailable: {lineage_error}"

    # Presentation layer — ordered contract for agent rendering
    try:
        from semantic_mapper.prove.presentation import build_presentation
        # ISSUE-06: derive actual query row count from the stored answer so that
        # result_confidence.row_count shows the number of result rows, not
        # subject_count (which reflects the full scan, not the filtered result).
        _answer = record.answer or {}
        _qrc: Optional[int] = None
        if isinstance(_answer.get("row_count"), int):
            _qrc = _answer["row_count"]
        elif isinstance(_answer.get("rows"), list):
            _qrc = len(_answer["rows"])
        elif isinstance(_answer.get("value"), int) and record.mode != "sparql":
            _qrc = _answer["value"]
        presentation = build_presentation(
            v2_proof=semantic_proof,
            v1_proof_dict=_v1_proof_dict,
            executed_query=record.executed_query,
            nl_question=record.question,
            query_row_count=_qrc,
        )
        result["proof_presentation"] = presentation.model_dump()
    except Exception:
        pass

    return result


def _call_proof_endpoint(record: Any, settings: Settings) -> Dict[str, Any]:
    """POST MCPQueryRecord fields to /api/query/proof and return the JSON response."""
    url = f"{settings.FORGEMESH_WEB_APP_URL}/api/query/proof"
    import os as _os
    payload = {
        "question": record.question,
        "query": record.executed_query,
        "mode": record.mode,
        "answer": record.answer,
        "run_id": record.run_id,
        # Include project so the proof endpoint uses the correct databases.
        # FORGEMESH_PROJECT is set by apply_project_to_env (--project flag) or
        # the docker-compose environment block.
        "project": _os.environ.get("FORGEMESH_PROJECT") or None,
    }
    timeout = getattr(settings, "FORGEMESH_PROOF_TIMEOUT", 120)
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
    except requests.exceptions.ConnectionError:
        return {
            "error": "webapp_unreachable",
            "detail": f"Could not connect to ForgeMesh web app at {url}.",
            "query_id": record.query_id,
        }
    except requests.exceptions.Timeout:
        return {
            "error": "proof_timeout",
            "detail": f"Proof request timed out after {timeout}s.",
            "query_id": record.query_id,
        }
    except Exception as exc:
        return {"error": "proof_failed", "detail": str(exc), "query_id": record.query_id}

    if resp.status_code == 503:
        detail = resp.json().get("detail", {})
        return {"error": "bronze_unavailable", "detail": str(detail), "query_id": record.query_id}
    if resp.status_code == 422:
        detail = resp.json().get("detail", {})
        return {"error": "unsupported_pattern", "pattern": str(detail), "query_id": record.query_id}
    if not resp.ok:
        return {"error": "proof_failed", "detail": f"HTTP {resp.status_code}: {resp.text[:400]}", "query_id": record.query_id}
    return resp.json()
