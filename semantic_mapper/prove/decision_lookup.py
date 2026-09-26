# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Phase 5 — Mapping evidence retriever.

Given a run_id and sets of class/property URIs, retrieves the full mapping
decision records from the persisted SemanticMappingReport.

Adjacent properties — all confirmed properties on the queried classes that
were NOT included in the query — are returned when include_adjacent=True.
These become the scope assumptions fed to the Phase 6 narrator.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Set

from semantic_mapper.config.settings import Settings
from semantic_mapper.db.run_store import get_mapping_report
from semantic_mapper.schema.mapping_output import (
    MappingEvidence,
    MappingStatus,
    PropertyMapping,
    SemanticMappingReport,
    TableMapping,
)

logger = logging.getLogger(__name__)

_HUMAN_STRATEGY = "HistoryMatcher"

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ProofEvidenceMissing(Exception):
    def __init__(self, run_id: str, uri: str) -> None:
        super().__init__(
            f"No mapping evidence available for run_id={run_id!r} uri={uri!r}"
        )
        self.run_id = run_id
        self.uri = uri


@dataclass(frozen=True)
class MappingDecisionRef:
    uri: str
    kind: str                       # "Class" | "DatatypeProperty" | "ObjectProperty"
    source: str                     # "admissions" or "admissions.admittime"
    winning_strategy: str
    score: float
    confirmation: str               # "HUMAN" | "AUTO"
    confirmed_at: Optional[str]
    evidence_trail: List[MappingEvidence] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_mapping_decisions(
    run_id: str,
    class_uris: Set[str],
    property_uris: Set[str],
    settings: Settings,
    include_adjacent: bool = True,
) -> List[MappingDecisionRef]:
    """Return MappingDecisionRefs for the given class and property URIs.

    Args:
        run_id:           The pipeline run that produced the mapping.
        class_uris:       OWL class URIs present in the query.
        property_uris:    OWL property URIs present in the query.
        settings:         Settings (needed to locate pipeline_runs.db).
        include_adjacent: When True, all confirmed properties on the queried
                          classes that were not in property_uris are appended.

    Raises:
        ProofEvidenceMissing: when run_id is unknown or the stored report is
                              NULL / undeserializable.
    """
    report = get_mapping_report(run_id, settings)
    if report is None:
        raise ProofEvidenceMissing(run_id, "")

    class_idx, prop_idx = _build_indexes(report)

    seen: set[str] = set()
    results: list[MappingDecisionRef] = []
    matched_tables: list[TableMapping] = []

    for uri in sorted(class_uris):
        table = class_idx.get(uri)
        if table is None:
            logger.debug("decision_lookup: class %s not found in report", uri)
            continue
        if table not in matched_tables:
            matched_tables.append(table)
        if uri not in seen:
            seen.add(uri)
            results.append(_class_ref(uri, table))

    for uri in sorted(property_uris):
        entry = prop_idx.get(uri)
        if entry is None:
            logger.debug("decision_lookup: property %s not found in report", uri)
            continue
        table, pm = entry
        if table not in matched_tables:
            matched_tables.append(table)
        if uri not in seen:
            seen.add(uri)
            results.append(_property_ref(uri, table.source_table, pm))

    if include_adjacent:
        for table in matched_tables:
            for pm in table.column_mappings:
                uri = pm.mapped_property_uri
                if not uri or uri in seen:
                    continue
                if pm.status != MappingStatus.CONFIRMED:
                    continue
                seen.add(uri)
                results.append(_property_ref(uri, table.source_table, pm))

    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_indexes(
    report: SemanticMappingReport,
) -> tuple[dict[str, TableMapping], dict[str, tuple[TableMapping, PropertyMapping]]]:
    class_idx: dict[str, TableMapping] = {}
    prop_idx: dict[str, tuple[TableMapping, PropertyMapping]] = {}
    for table in report.tables:
        for candidate in table.class_candidate_set:
            class_idx.setdefault(candidate.uri, table)
        for pm in table.column_mappings:
            if pm.mapped_property_uri:
                prop_idx.setdefault(pm.mapped_property_uri, (table, pm))
    return class_idx, prop_idx


def _winning(trail: list[MappingEvidence]) -> tuple[str, float]:
    if not trail:
        return "", 0.0
    best = max(trail, key=lambda e: e.score)
    return best.strategy, best.score


def _confirmation(trail: list[MappingEvidence]) -> str:
    return "HUMAN" if any(e.strategy == _HUMAN_STRATEGY for e in trail) else "AUTO"


def _class_ref(uri: str, table: TableMapping) -> MappingDecisionRef:
    candidates = [c for c in table.class_candidate_set if c.uri == uri]
    score = candidates[0].confidence_score if candidates else 0.0
    return MappingDecisionRef(
        uri=uri,
        kind="Class",
        source=table.source_table,
        winning_strategy="",
        score=score,
        confirmation="AUTO",
        confirmed_at=None,
        evidence_trail=[],
    )


def _property_ref(
    uri: str, source_table: str, pm: PropertyMapping
) -> MappingDecisionRef:
    strategy, score = _winning(list(pm.evidence_trail))
    return MappingDecisionRef(
        uri=uri,
        kind="DatatypeProperty",
        source=f"{source_table}.{pm.source_column}",
        winning_strategy=strategy,
        score=score,
        confirmation=_confirmation(list(pm.evidence_trail)),
        confirmed_at=None,
        evidence_trail=list(pm.evidence_trail),
    )
