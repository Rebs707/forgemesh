# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, List, Optional, Dict, Any, Union
from pydantic import BaseModel, Field, ConfigDict
from datetime import datetime

class MappingStatus(str, Enum):
    CONFIRMED = "confirmed"
    PENDING_REVIEW = "pending"
    REJECTED = "rejected"
    UNMAPPED = "unmapped"

class ConfidenceLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

class TransformationSpec(BaseModel):
    detected_formats: List[str] = Field(..., description="Detected patterns, e.g. ['YYYY-MM-DD', 'DD/MM/YYYY']")
    target_format: str = Field(..., description="Target XSD or ISO format")
    parse_strategy: str = Field(..., description="Python-specific approach, e.g. 'dateutil.parser'")
    edge_cases: List[str] = Field(default_factory=list)
    validation_rule: str

class ValueMappingRule(BaseModel):
    source_value: Union[str, int, float, None]
    mapped_uri: str
    rationale: str
    confidence_score: float = Field(..., ge=0.0, le=1.0)

class ClassCandidate(BaseModel):
    uri: str
    label: str
    rationale: str
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    is_primary_discriminator: bool = False

class DerivationDependency(BaseModel):
    target_derived_class: str
    required_tables: List[str]
    required_columns: List[str]
    join_logic_hint: str
    reasoning_premise: str

class MappingEvidence(BaseModel):
    strategy: str
    score: float
    rationale: str
    mapped_uri: Optional[str] = None

class PropertyMapping(BaseModel):
    source_column: str
    source_data_type: str
    is_nullable: bool
    mapped_property_uri: Optional[str] = None
    mapped_property_label: Optional[str] = None
    status: MappingStatus = MappingStatus.PENDING_REVIEW
    confidence_score: float = Field(..., ge=0.0, le=1.0)
    rationale: str
    transformation_spec: Optional[TransformationSpec] = None
    value_mappings: List[ValueMappingRule] = Field(default_factory=list)
    derivation_dependencies: List[DerivationDependency] = Field(default_factory=list)
    evidence_trail: List[MappingEvidence] = Field(default_factory=list)

class TableMapping(BaseModel):
    source_table: str
    class_candidate_set: List[ClassCandidate]
    column_mappings: List[PropertyMapping]
    mapping_notes: Optional[str] = None
    # Inspector-detected primary key columns for this table (declared PK preferred,
    # else inferred). The canonical, name-agnostic row key used downstream for
    # bronze_source_ref (provenance) and the RDF subject URI. Empty when no PK was
    # detected — downstream falls back to name heuristics / a surrogate.
    primary_key_columns: List[str] = Field(default_factory=list)

# ── Issue #49: Ontology Coverage Metrics ──────────────────────────────────────

class TableCoverage(BaseModel):
    source_table: str
    total_columns: int
    confirmed: int
    pending_review: int
    unmapped: int
    rejected: int
    coverage_pct: float   # confirmed / total * 100


class CoverageReport(BaseModel):
    total_columns: int
    confirmed: int
    pending_review: int
    unmapped: int
    rejected: int
    coverage_pct: float   # confirmed / total * 100
    pending_pct: float
    unmapped_pct: float
    per_table: List[TableCoverage]


class SemanticMappingReport(BaseModel):
    model_config = ConfigDict(frozen=True)
    schema_version: str = "1.3.0"
    generated_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    generated_by: str = "SemanticMapper-v1"
    ontology_uri: str
    database_name: str
    settings_used: Dict[str, Any]
    tables: List[TableMapping]
    total_pending_review: int
    coverage: Optional[CoverageReport] = None


def compute_coverage(
    report_or_tables: SemanticMappingReport | List[TableMapping],
) -> CoverageReport:
    """Derive per-table and aggregate coverage metrics.

    Accepts either a full SemanticMappingReport or a bare list of TableMapping
    objects so callers can compute coverage before the report object exists.
    """
    tables = (
        report_or_tables.tables
        if isinstance(report_or_tables, SemanticMappingReport)
        else report_or_tables
    )
    per_table: List[TableCoverage] = []
    total = confirmed = pending = unmapped = rejected = 0

    for t in tables:
        tc = ti = tu = tr = 0
        for c in t.column_mappings:
            if c.status == MappingStatus.CONFIRMED:
                tc += 1
            elif c.status == MappingStatus.PENDING_REVIEW:
                ti += 1
            elif c.status == MappingStatus.UNMAPPED:
                tu += 1
            elif c.status == MappingStatus.REJECTED:
                tr += 1
        n = tc + ti + tu + tr
        total += n
        confirmed += tc
        pending += ti
        unmapped += tu
        rejected += tr
        per_table.append(TableCoverage(
            source_table=t.source_table,
            total_columns=n,
            confirmed=tc,
            pending_review=ti,
            unmapped=tu,
            rejected=tr,
            coverage_pct=round(tc / n * 100, 1) if n else 0.0,
        ))

    return CoverageReport(
        total_columns=total,
        confirmed=confirmed,
        pending_review=pending,
        unmapped=unmapped,
        rejected=rejected,
        coverage_pct=round(confirmed / total * 100, 1) if total else 0.0,
        pending_pct=round(pending / total * 100, 1) if total else 0.0,
        unmapped_pct=round(unmapped / total * 100, 1) if total else 0.0,
        per_table=per_table,
    )
