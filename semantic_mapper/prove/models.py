# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Phase 7 — Proof record models.

ProofRecord is the immutable JSON contract for a single proof.
ProofTree wraps N ProofRecords for agentic multi-step query mode.

The three REQUIRED_LIMITATIONS strings are enforced by a model_validator;
construction raises ValueError if any are absent.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, model_validator

# Non-removable limitation strings.  All three must appear in ProofRecord.limitations.
REQUIRED_LIMITATIONS: tuple[str, ...] = (
    "This proof verifies the answer given the query that was executed.",
    "It does not verify that the natural-language question was correctly translated to that query.",
    "It does not verify the semantic correctness of the underlying ontology.",
)


class ProofRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    proof_schema_version: str = "1.0.0"
    query_mode: str                         # "sparql" | "gold" | "silver"
    question: str
    answer: Dict[str, Any]
    interpretation: Dict[str, Any]
    ontology_subgraph: Dict[str, Any]
    executed_query: str
    proof_query: str
    subject_count: int
    subjects: List[str]
    lineage: List[Dict[str, Any]]
    mapping_decisions: List[Dict[str, Any]]
    caveats: List[Dict[str, Any]]
    limitations: List[str]
    mapping_report_id: str
    generated_at: str                       # ISO-8601 UTC
    grouping_fields: List[str] = []
    queried_uris: List[str] = []            # URIs referenced in executed_query, for UI filtering

    @model_validator(mode="after")
    def _require_limitations(self) -> "ProofRecord":
        present = set(self.limitations)
        missing = [s for s in REQUIRED_LIMITATIONS if s not in present]
        if missing:
            raise ValueError(
                "ProofRecord.limitations is missing required strings: "
                + "; ".join(repr(s) for s in missing)
            )
        return self


class RowProofRecord(BaseModel, frozen=True):
    proof_schema_version: str = "1.0.0"
    query_mode: str
    row_grouping_values: Dict[str, Any]
    contributing_subjects: List[str]
    primary_table: str = ""
    lineage: List[Dict[str, Any]]
    mapping_decisions_for_grouping: List[Dict[str, Any]]
    caveats: List[Dict[str, Any]]
    limitations: List[str]
    mapping_report_id: str
    generated_at: str
    rdf_neighbourhood: Dict[str, List[Dict[str, str]]] = {}  # uri → [{p, o}]
    sparql_funnel: Optional[List[Dict[str, Any]]] = None    # serialised FunnelStep list
    funnel_unsupported_reason: Optional[str] = None
    sparql_matches: Optional[List[Dict[str, Any]]] = None   # serialised MatchRecord list

    @model_validator(mode="after")
    def _require_limitations(self) -> "RowProofRecord":
        present = set(self.limitations)
        missing = [s for s in REQUIRED_LIMITATIONS if s not in present]
        if missing:
            raise ValueError(
                "RowProofRecord.limitations is missing required strings: "
                + "; ".join(repr(s) for s in missing)
            )
        return self


class ProofTree(BaseModel):
    model_config = ConfigDict(frozen=True)

    proofs: List[ProofRecord]
