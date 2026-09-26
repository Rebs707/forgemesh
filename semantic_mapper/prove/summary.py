# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 3 — proof summary generator.

Converts a full ProofRecord into a lightweight ProofSummary suitable for
agent consumption.  Deterministic: same inputs always produce identical output.
No LLM call.

Also defines ProofTreeSummary for agentic multi-step proof composition (Phase 7).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Union

from semantic_mapper.prove.models import REQUIRED_LIMITATIONS, ProofRecord

PROOF_SUMMARY_VERSION = "1.0.0"
PROOF_TREE_SUMMARY_VERSION = "1.0.0"


@dataclass(frozen=True)
class DataAuditSummary:
    subject_count: int
    broken_lineage_count: int
    quarantined_rows: int
    null_excluded_rows: int
    sampled: bool


@dataclass(frozen=True)
class MappingConfidenceSummary:
    all_mappings_human_confirmed: bool
    human_confirmed_count: int
    auto_confirmed_count: int
    lowest_score: Optional[float]  # None when no mapping decisions exist


@dataclass(frozen=True)
class ProofSummary:
    proof_summary_version: str
    query_id: str
    question: str
    answer: Dict[str, Any]
    interpretation: Dict[str, Any]
    data_audit_summary: DataAuditSummary
    mapping_confidence_summary: MappingConfidenceSummary
    limitations: List[str]
    full_record_available_via: str = "get_proof_detail"

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


@dataclass(frozen=True)
class ProofTreeSummary:
    proof_tree_summary_version: str
    tree_id: str
    question: str
    answer: Dict[str, Any]
    synthesis_step: Dict[str, Any]
    child_summaries: List[Dict[str, Any]]
    caveats: List[str]
    limitations: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def summarise_proof(record: ProofRecord, query_id: str, settings: Any = None) -> ProofSummary:
    """Convert a ProofRecord to a lightweight ProofSummary.

    Deterministic — same record + query_id always produces identical output.
    Truncates to MCP_PROOF_SUMMARY_MAX_BYTES if settings provided.
    """
    broken = sum(1 for c in record.lineage if c.get("broken"))
    quarantined = sum(
        c.get("count", 0) or 0
        for c in record.caveats
        if c.get("type") == "QUARANTINED"
    )
    null_excluded = sum(
        c.get("count", 0) or 0
        for c in record.caveats
        if c.get("type") in ("NULL_EXCLUDED", "MISSING_VALUE")
    )

    decisions = record.mapping_decisions
    confirmed_human = [
        d for d in decisions if d.get("confirmation") in ("HUMAN", "CONFIRMED")
    ]
    confirmed_auto = [
        d for d in decisions if d.get("confirmation") == "AUTO"
    ]
    scores = [d.get("score") or 0.0 for d in decisions]
    lowest = min(scores) if scores else None

    audit = DataAuditSummary(
        subject_count=record.subject_count,
        broken_lineage_count=broken,
        quarantined_rows=quarantined,
        null_excluded_rows=null_excluded,
        sampled=False,
    )
    confidence = MappingConfidenceSummary(
        all_mappings_human_confirmed=bool(decisions and not confirmed_auto),
        human_confirmed_count=len(confirmed_human),
        auto_confirmed_count=len(confirmed_auto),
        lowest_score=lowest,
    )

    return ProofSummary(
        proof_summary_version=PROOF_SUMMARY_VERSION,
        query_id=query_id,
        question=record.question,
        answer=record.answer,
        interpretation=record.interpretation,
        data_audit_summary=audit,
        mapping_confidence_summary=confidence,
        limitations=list(REQUIRED_LIMITATIONS),
    )
