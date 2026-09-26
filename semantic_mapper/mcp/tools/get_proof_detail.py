# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""get_proof_detail MCP tool.

Returns bronze-level lineage for a query. Subject list and lineage entries
are capped at max_subjects (default 10) to prevent >1MB responses that
overflow the MCP context limit.

For summary-level proof without row data, use prove_answer instead.
"""
from __future__ import annotations

from typing import Any, Dict

from semantic_mapper.config.settings import Settings
from semantic_mapper.mcp.query_store import read_query
from semantic_mapper.mcp.tools.prove_answer import _call_proof_endpoint

_DEFAULT_MAX_SUBJECTS = 10


def run_get_proof_detail(
    query_id: str,
    settings: Settings,
    max_subjects: int = _DEFAULT_MAX_SUBJECTS,
) -> Dict[str, Any]:
    """Return the full ProofRecord JSON for query_id with lineage capped at max_subjects."""
    record = read_query(query_id, settings)
    if record is None:
        return {"error": "unknown_query_id", "query_id": query_id}

    result = _call_proof_endpoint(record, settings)
    if "error" in result:
        return result

    # Cap subjects + lineage to prevent response overflow
    total_subjects = len(result.get("subjects", []))
    if total_subjects > max_subjects:
        result["subjects"] = result["subjects"][:max_subjects]
        if isinstance(result.get("lineage"), list):
            result["lineage"] = result["lineage"][:max_subjects]
        result["_truncated"] = True
        result["_total_subjects"] = total_subjects
        result["_shown_subjects"] = max_subjects
    else:
        result["_truncated"] = False
        result["_total_subjects"] = total_subjects

    result["query_id"] = query_id
    return result
