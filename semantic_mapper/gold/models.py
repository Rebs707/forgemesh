# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from pydantic import BaseModel


class GoldColumnSpec(BaseModel, frozen=True):
    silver_table: str       # e.g. "silver_raw_party"
    source_column: str      # bronze column name (before safe-name transform)
    gold_column_name: str   # ontology property local name, e.g. "hasSubjectId"
    source_data_type: str   # original MySQL type, for DDL type mapping
    property_uri: str       # full ontology property URI (empty = collision loser, skip triple)
    # raw_value → ontology URI; non-empty for enumerable/discriminated columns.
    # TripleBuilder uses this to emit object property triples instead of literals.
    value_map: Dict[str, str] = {}
    # Confidence score from the mapping report — used for C2 collision tiebreaking:
    # when two columns share a property_uri, the higher-confidence one keeps it.
    confidence_score: float = 0.0


class GoldTableDefinition(BaseModel, frozen=True):
    gold_table_name: str                # "gold_{class_label}"
    primary_class_uri: str             # ontology class URI
    class_label: str                   # snake_case local name
    source_silver_tables: List[str]    # ["silver_raw_party", ...]
    column_specs: List[GoldColumnSpec] # CONFIRMED columns only; deduped by gold_column_name
    # Discriminated subtypes written to the triplestore via value_mappings.
    # Non-empty when a source column maps distinct values to distinct class URIs
    # (e.g. admission_type: ELECTIVE→ElectiveAdmission, EMERGENCY→EmergencyAdmission).
    # The SPARQL context uses these to generate correct UNION patterns.
    discriminated_class_uris: List[str] = []
    # Best _id column to use as the entity's subject URI.  None = fall back to
    # _bronze_source_ref (event/junction tables without a meaningful single PK).
    id_column: Optional[str] = None


class GoldWriteResult(BaseModel, frozen=True):
    rows_written: int
    rows_failed: int = 0
    target_table: str
    run_id: str
    written_at: datetime


class GoldPipelineStats(BaseModel, frozen=True):
    run_id: str
    tables_written: List[str]
    rows_per_table: Dict[str, int]
    total_rows_written: int
    started_at: datetime
    finished_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
