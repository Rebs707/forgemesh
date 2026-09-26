# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class TransformOutcome(str, Enum):
    TRANSFORMED = "transformed"
    PASSTHROUGH = "passthrough"
    NULL_PRESERVED = "null_preserved"
    QUARANTINED = "quarantined"


class PropertyKind(str, Enum):
    DATATYPE = "datatype"   # → Satellite column in DV
    OBJECT = "object"       # → Link table in DV


class RowAudit(BaseModel, frozen=True):
    bronze_source_ref: str          # "{table}.{pk_col}={pk_value}"
    run_id: str                     # UUID; stable across all rows in one run
    transformation_timestamp: datetime
    mapping_confidence_score: float = Field(..., ge=0.0, le=1.0)
    transformation_strategy: str


class TransformResult(BaseModel, frozen=True):
    outcome: TransformOutcome
    transformed_value: Any          # None when QUARANTINED or NULL_PRESERVED
    original_value: Any             # always populated for audit
    transformer_name: str
    rationale: str
    error_detail: Optional[str] = None     # non-None when QUARANTINED
    null_metadata: Optional[Dict[str, Any]] = None  # non-None when NULL_PRESERVED


class SilverRow(BaseModel, frozen=True):
    source_table: str
    source_column: str
    ontology_class_uri: str         # DV: identifies Hub
    property_uri: str
    property_local_name: str        # snake_case; DV: Satellite column name
    property_kind: PropertyKind     # DV routing signal
    value: Any
    transform_result: TransformResult
    expansion_index: int = 0        # multi-class row expansion index
    discriminator_value: Optional[Any] = None
    audit: RowAudit


class QuarantineRecord(BaseModel, frozen=True):
    source_table: str
    source_column: str
    original_value: Any
    property_uri: Optional[str]
    transformer_name: str
    error_detail: str
    audit: RowAudit


class BatchWriteResult(BaseModel, frozen=True):
    rows_written: int
    rows_failed: int = 0
    target_table: str
    run_id: str
    written_at: datetime


class RunStats(BaseModel, frozen=True):
    run_id: str
    source_table: str
    bronze_rows_read: int
    silver_rows_written: int = 0
    quarantine_rows_written: int = 0
    null_preserved_count: int = 0
    transformation_errors: int = 0
    batch_results: List[BatchWriteResult]
    started_at: datetime
    finished_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
