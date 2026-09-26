# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Pydantic models for Phase-2 quality evaluation.

Issues:
  #48 — Mapping Quality Evaluation (GroundTruth, MappingQualityReport)
  #50 — Triple Completeness (PipelineCompletenessReport)
  #51 — Benchmark Query Set (BenchmarkSuite, BenchmarkRunResult)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


# ── 2.1 Mapping Quality ────────────────────────────────────────────────────────

class GroundTruthEntry(BaseModel):
    """What a source column means, stated independently of any ontology's naming.

    Semantic expectations (scored against the ontology):
      refers_to  — the column is a reference to rows of this table, so it must be
                   an ObjectProperty whose range is that table's class.
      value_type — the column holds this kind of value (or any of a list):
                   temporal | numeric | boolean | text | categorical | identifier.
    expected_property_uri is the legacy exact-name expectation, used only for
    entries without a semantic expectation.
    """
    table: str
    column: str
    refers_to: Optional[str] = None
    value_type: Optional[Union[str, List[str]]] = None
    expected_property_uri: Optional[str] = None
    note: Optional[str] = None

    def expectation(self) -> str:
        if self.refers_to:
            return f"refers to {self.refers_to}"
        if self.value_type:
            kinds = [self.value_type] if isinstance(self.value_type, str) else self.value_type
            return " or ".join(kinds) + " value"
        return self.expected_property_uri or ""


class GroundTruth(BaseModel):
    version: str = "1.0"
    dataset: str
    description: Optional[str] = None
    mappings: List[GroundTruthEntry]


class ColumnEvalResult(BaseModel):
    table: str
    column: str
    expected_uri: str
    actual_uri: Optional[str]
    actual_status: str  # "confirmed" | "pending" | "unmapped" | "rejected"
    correct: bool       # confirmed AND the mapped property meets the expectation
    reason: Optional[str] = None  # why an entry was scored incorrect


class MappingQualityReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    ground_truth_path: str
    report_generated_at: str
    dataset: str
    total_ground_truth_entries: int
    true_positives: int   # confirmed AND correct URI
    false_positives: int  # confirmed but wrong URI
    false_negatives: int  # expected but not confirmed
    precision: float      # tp / (tp + fp)
    recall: float         # tp / total_gt
    f1: float
    pending_rate: float   # pending columns / total_gt
    unmapped_rate: float  # unmapped columns / total_gt
    llm_cost_usd: Optional[float] = None
    llm_cost_per_mapping: Optional[float] = None
    per_column: List[ColumnEvalResult]


# ── 2.3 Triple Completeness ───────────────────────────────────────────────────

class SilverCompletenessStats(BaseModel):
    bronze_rows_read: int = 0
    silver_rows_written: int = 0
    quarantine_rows_written: int = 0
    null_preserved_count: int = 0
    transformation_errors: int = 0
    duration_seconds: Optional[float] = None
    run_id: Optional[str] = None
    finished_at: Optional[str] = None

    @property
    def completeness_pct(self) -> Optional[float]:
        if self.bronze_rows_read == 0:
            return None
        return round(self.silver_rows_written / self.bronze_rows_read * 100, 2)

    @property
    def quarantine_pct(self) -> Optional[float]:
        if self.bronze_rows_read == 0:
            return None
        return round(self.quarantine_rows_written / self.bronze_rows_read * 100, 2)


class GoldCompletenessStats(BaseModel):
    total_rows_written: int = 0
    tables_written: List[str] = Field(default_factory=list)
    rows_per_table: Dict[str, int] = Field(default_factory=dict)
    duration_seconds: Optional[float] = None
    run_id: Optional[str] = None
    finished_at: Optional[str] = None


class RdfCompletenessStats(BaseModel):
    triples_written: int = 0
    tables_processed: List[str] = Field(default_factory=list)
    named_graph: str = ""
    duration_seconds: Optional[float] = None
    run_id: Optional[str] = None
    finished_at: Optional[str] = None


class PipelineCompletenessReport(BaseModel):
    available: bool
    silver: Optional[SilverCompletenessStats] = None
    gold: Optional[GoldCompletenessStats] = None
    rdf: Optional[RdfCompletenessStats] = None


# ── 2.4 Benchmark Query Set ───────────────────────────────────────────────────

class BenchmarkQuery(BaseModel):
    id: str
    description: str
    sparql: str
    expected_count: int
    tolerance: float = 0.0   # fractional: 0.05 = ±5%; 0.0 = exact


class BenchmarkSuite(BaseModel):
    version: str = "1.0"
    dataset: str
    description: Optional[str] = None
    named_graph: Optional[str] = None   # if set, queries run FROM this named graph
    queries: List[BenchmarkQuery]


class BenchmarkQueryResult(BaseModel):
    query_id: str
    description: str
    expected_count: int
    actual_count: Optional[int]
    passed: bool
    error: Optional[str] = None


class BenchmarkRunResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    suite_path: str
    run_at: str
    total: int
    passed: int
    failed: int
    errored: int
    results: List[BenchmarkQueryResult]
