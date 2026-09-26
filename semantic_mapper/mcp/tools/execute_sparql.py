# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 2 — execute_sparql tool implementation.

Pure execution: validate SPARQL is SELECT-only, run against the engine,
return rows + a generated query_id.  No LLM involved.

Note: MCPQueryRecord persistence (for prove_answer integration) is planned
for Feature 02.  This module returns a query_id that Feature 02 can hook
into when that feature is implemented.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional


def run_execute_sparql(
    engine: Any,
    sparql: str,
    question_hint: str = "",
) -> Dict[str, Any]:
    """Validate and execute a SPARQL SELECT query.

    Parameters
    ----------
    engine:
        Any object with an ``_execute_sparql(sparql: str) -> list`` method.
        In production this is ``SparqlQueryEngine``; in tests it is a mock.
    sparql:
        The SPARQL SELECT string constructed by the agent.
    question_hint:
        The user's original question (optional).  Stored for future proof
        integration but not required for execution.

    Returns
    -------
    dict with keys: query_id, rows, columns, row_count, duration_ms.
    On error: adds an ``error`` key and returns empty rows.
    """
    from semantic_mapper.rdf.query.safety import validate_sparql_select_only
    from semantic_mapper.core.exceptions import QuerySafetyError

    try:
        validate_sparql_select_only(sparql)
    except QuerySafetyError as exc:
        return {
            "error": f"Safety error: {exc}",
            "query_id": None,
            "rows": [],
            "columns": [],
            "row_count": 0,
            "duration_ms": 0,
        }

    query_id = "q_" + uuid.uuid4().hex[:12]
    t0 = time.monotonic()
    try:
        rows = engine._execute_sparql(sparql)
    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        return {
            "error": f"Execution error: {exc}",
            "query_id": query_id,
            "rows": [],
            "columns": [],
            "row_count": 0,
            "duration_ms": duration_ms,
        }

    duration_ms = int((time.monotonic() - t0) * 1000)
    columns: List[str] = list(rows[0].keys()) if rows else []

    # Feature 12: generate v2 ontology-grounded proof if narrator is available
    proof_record_json: Optional[str] = None
    narrator = getattr(engine, "_narrator", None)
    if narrator is not None:
        try:
            proof = narrator.narrate(
                sparql=sparql,
                nl_question=question_hint or "",
                rows=rows,
                query_id=query_id,
            )
            proof_record_json = proof.model_dump_json()
        except Exception as _narrator_exc:
            import logging as _log
            _log.getLogger(__name__).warning(
                "proof narrator failed for query %s: %s", query_id, _narrator_exc
            )

    return {
        "query_id": query_id,
        "rows": rows,
        "columns": columns,
        "row_count": len(rows),
        "duration_ms": duration_ms,
        "question_hint": question_hint or None,
        "_proof_record_json": proof_record_json,  # internal; stripped before MCP response
    }
