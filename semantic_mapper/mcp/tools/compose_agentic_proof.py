# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 7 — compose_agentic_proof MCP tool.

Assembles a ProofTreeSummary from constituent query_ids plus a synthesis step
described by the agent.  Recognised operations are mathematically verified;
free-form operations are flagged as LLM_SYNTHESIS_UNVERIFIED.
"""
from __future__ import annotations

from typing import Any, Dict, List
from uuid import uuid4

from semantic_mapper.config.settings import Settings
from semantic_mapper.prove.models import REQUIRED_LIMITATIONS
from semantic_mapper.prove.synthesis import (
    RECOGNISED_OPERATIONS,
    SynthesisMismatch,
    verify_synthesis,
)
from semantic_mapper.prove.summary import ProofTreeSummary
from semantic_mapper.mcp.tools.prove_answer import run_prove_answer


def run_compose_agentic_proof(
    question: str,
    answer: Dict[str, Any],
    constituent_query_ids: List[str],
    synthesis_step_input: Dict[str, Any],
    settings: Settings,
) -> Dict[str, Any]:
    """Compose a proof tree from child queries plus a synthesis step.

    Returns a ProofTreeSummary dict, or an error dict on failure.
    """
    # Retrieve ProofSummary for each child query_id
    child_summaries: List[Dict[str, Any]] = []
    for qid in constituent_query_ids:
        child = run_prove_answer(qid, settings)
        if "error" in child:
            return {
                "error": "child_proof_failed",
                "query_id": qid,
                "detail": child.get("error"),
            }
        child_summaries.append(child)

    # Verify synthesis step
    operation = synthesis_step_input.get("operation", "")
    inputs_used = synthesis_step_input.get("inputs_used", {})
    claimed_output = synthesis_step_input.get("output")
    description = synthesis_step_input.get("description", "")

    caveats: List[str] = []

    try:
        step = verify_synthesis(operation, inputs_used, claimed_output, description)
    except SynthesisMismatch as exc:
        return {"error": "synthesis_mismatch", "detail": str(exc)}

    if not step.operation_verified:
        caveats.append(
            f"LLM_SYNTHESIS_UNVERIFIED: operation '{operation}' is not in the "
            f"recognised set {sorted(RECOGNISED_OPERATIONS)}. "
            "The synthesis step could not be mathematically verified."
        )

    tree = ProofTreeSummary(
        proof_tree_summary_version="1.0.0",
        tree_id="t_" + uuid4().hex[:12],
        question=question,
        answer=answer,
        synthesis_step={
            "operation": step.operation,
            "operation_verified": step.operation_verified,
            "description": step.description,
            "inputs_used": step.inputs_used,
            "output": step.output,
        },
        child_summaries=child_summaries,
        caveats=caveats,
        limitations=list(REQUIRED_LIMITATIONS),
    )
    return tree.to_dict()
