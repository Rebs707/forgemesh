# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Pydantic models for the RIGOR-inspired iterative ontology generator.
"""
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class ColumnContext(BaseModel):
    table_name: str
    col_name: str
    inferred_type: str
    is_pk: bool
    is_fk: bool
    fk_ref_table: Optional[str] = None
    fk_ref_skipped: bool = False
    is_nullable: bool = False
    null_rate: float
    distinct_count: int
    top_values: List[str] = Field(default_factory=list)   # max 10, as strings
    detected_formats: List[str] = Field(default_factory=list)
    is_categorical: bool = False
    categorical_values: List[str] = Field(default_factory=list)
    cardinality_hint: Optional[str] = None


class TableContext(BaseModel):
    table_name: str
    row_count: int
    columns: List[ColumnContext]
    explicit_fks: List[Dict[str, str]] = Field(default_factory=list)  # [{column, ref_table}]
    processing_index: int   # 1-based position in topo order
    total_tables: int


class AccumulatedClassSummary(BaseModel):
    local_name: str
    label: str
    uri: str
    source_tables: List[str] = Field(default_factory=list)


class AccumulatedAbstractPropertySummary(BaseModel):
    """Summary of an abstract (domain-less) property already in the accumulating graph."""
    local_name: str
    label: str
    uri: str
    range_uri: str = ""
    prop_type: str = ""  # "DatatypeProperty" | "ObjectProperty"


class OntologyFragment(BaseModel):
    table_name: str
    manchester_syntax: str
    classes_declared: List[str] = Field(default_factory=list)
    properties_declared: List[str] = Field(default_factory=list)
    retry_count: int = 0
    provenance_violations: List[str] = Field(default_factory=list)


class JudgeVerdict(BaseModel):
    approved: bool
    coverage_issues: List[str] = Field(default_factory=list)    # ["col_name: reason", ...]
    coherence_issues: List[str] = Field(default_factory=list)
    revision_instructions: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
