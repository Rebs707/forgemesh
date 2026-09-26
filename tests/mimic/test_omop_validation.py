# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
OMOP-CDM Mapping Validation Tests — issue #57

Verifies that the OMOP ground truth file is well-formed and that the
MappingEvaluator correctly computes precision/recall/F1 for MIMIC→OMOP
column mappings.

Integration tests (test_*_live_*) are skipped unless the MIMIC mapping
report exists at output/mapping_report_mimic.json.

Unit tests build a synthetic SemanticMappingReport from a subset of the
ground truth to verify evaluator behaviour without a real pipeline run.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

import pytest
import yaml

from semantic_mapper.eval.evaluator import MappingEvaluator
from semantic_mapper.eval.models import GroundTruth, GroundTruthEntry
from semantic_mapper.schema.mapping_output import (
    MappingStatus,
    PropertyMapping,
    SemanticMappingReport,
    TableMapping,
)

ROOT = Path(__file__).resolve().parents[2]
GT_PATH = ROOT / "eval" / "omop_mimic_ground_truth.yaml"
REPORT_PATH = ROOT / "output" / "mapping_report_mimic.json"

# ── Shared fixture ────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def ground_truth() -> GroundTruth:
    assert GT_PATH.exists(), f"Ground truth not found: {GT_PATH}"
    with open(GT_PATH) as f:
        raw = yaml.safe_load(f)
    return GroundTruth.model_validate(raw)


# ─────────────────────────────────────────────────────────────────────────────
# Ground truth file structure
# ─────────────────────────────────────────────────────────────────────────────

class TestGroundTruthFile:
    """Validate that the YAML file is well-formed and contains expected entries."""

    def test_file_exists(self):
        assert GT_PATH.exists(), f"Ground truth file not found at {GT_PATH}"

    def test_parses_without_error(self, ground_truth):
        assert ground_truth is not None

    def test_dataset_field_is_mimic(self, ground_truth):
        assert "mimic" in ground_truth.dataset.lower(), (
            f"Expected 'mimic' in dataset name, got '{ground_truth.dataset}'"
        )

    def test_minimum_entry_count(self, ground_truth):
        """Ground truth must cover all major MIMIC tables — at least 40 entries."""
        assert len(ground_truth.mappings) >= 40, (
            f"Expected >= 40 ground truth entries, got {len(ground_truth.mappings)}"
        )

    def test_every_entry_states_one_meaning(self, ground_truth):
        bad = [(e.table, e.column) for e in ground_truth.mappings
               if bool(e.refers_to) == bool(e.value_type)]
        assert not bad, f"Entries needing exactly one of refers_to/value_type: {bad}"

    def test_value_types_are_known(self, ground_truth):
        known = {"temporal", "numeric", "boolean", "text", "categorical", "identifier"}
        for e in ground_truth.mappings:
            kinds = [e.value_type] if isinstance(e.value_type, str) else (e.value_type or [])
            assert set(kinds) <= known, f"{e.table}.{e.column}: unknown value_type {kinds}"

    def test_references_point_at_mimic_tables(self, ground_truth):
        schema = (ROOT / "datasets" / "mimic" / "setup" / "schema.sql").read_text()
        tables = set(re.findall(r"CREATE TABLE(?: IF NOT EXISTS)?\s+(\w+)", schema))
        bad = [(e.table, e.column, e.refers_to) for e in ground_truth.mappings
               if e.refers_to and e.refers_to not in tables]
        assert not bad, f"refers_to targets missing from the MIMIC schema: {bad}"

    def test_patients_table_covered(self, ground_truth):
        tables = {e.table for e in ground_truth.mappings}
        assert "raw_patients" in tables

    def test_admissions_table_covered(self, ground_truth):
        tables = {e.table for e in ground_truth.mappings}
        assert "raw_admissions" in tables

    def test_icustays_table_covered(self, ground_truth):
        tables = {e.table for e in ground_truth.mappings}
        assert "raw_icustays" in tables

    def test_labevents_table_covered(self, ground_truth):
        tables = {e.table for e in ground_truth.mappings}
        assert "raw_labevents" in tables

    def test_prescriptions_table_covered(self, ground_truth):
        tables = {e.table for e in ground_truth.mappings}
        assert "raw_prescriptions" in tables

    def test_no_duplicate_table_column_pairs(self, ground_truth):
        seen: set[tuple] = set()
        dupes = []
        for e in ground_truth.mappings:
            key = (e.table, e.column)
            if key in seen:
                dupes.append(key)
            seen.add(key)
        assert not dupes, f"Duplicate (table, column) pairs in ground truth: {dupes}"

    def test_patient_and_admission_keys_are_references(self, ground_truth):
        """Outside their own table, subject_id / hadm_id reference patients / admissions."""
        owners = {"subject_id": "raw_patients", "hadm_id": "raw_admissions"}
        for e in ground_truth.mappings:
            owner = owners.get(e.column)
            if owner and e.table != owner:
                assert e.refers_to == owner, f"{e.table}.{e.column} should refer to {owner}"


# ─────────────────────────────────────────────────────────────────────────────
# Evaluator unit tests — synthetic report, no pipeline needed
# ─────────────────────────────────────────────────────────────────────────────

def _make_report(confirmed_entries: List[tuple], pending_tables: List[tuple]) -> SemanticMappingReport:
    """
    Build a minimal SemanticMappingReport from explicit column lists.

    confirmed_entries: [(table, column, uri), ...]  — status=CONFIRMED, URI set
    pending_tables:    [(table, column), ...]        — status=PENDING_REVIEW
    """
    # Group by table
    table_cols: dict[str, list[PropertyMapping]] = {}

    for table, column, uri in confirmed_entries:
        pm = PropertyMapping(
            source_column=column,
            source_data_type="VARCHAR",
            is_nullable=False,
            mapped_property_uri=uri,
            confidence_score=0.95,
            status=MappingStatus.CONFIRMED,
            rationale="synthetic test entry",
        )
        table_cols.setdefault(table, []).append(pm)

    for table, column in pending_tables:
        pm = PropertyMapping(
            source_column=column,
            source_data_type="VARCHAR",
            is_nullable=True,
            mapped_property_uri=None,
            confidence_score=0.4,
            status=MappingStatus.PENDING_REVIEW,
            rationale="low confidence — pending review",
        )
        table_cols.setdefault(table, []).append(pm)

    tables = [
        TableMapping(
            source_table=tbl,
            class_candidate_set=[],
            column_mappings=cols,
        )
        for tbl, cols in table_cols.items()
    ]
    total_pending = sum(
        1 for cols in table_cols.values()
        for pm in cols
        if pm.status == MappingStatus.PENDING_REVIEW
    )
    return SemanticMappingReport(
        ontology_uri="http://mesh.local/ontology/mimic",
        database_name="mimic_bronze",
        settings_used={},
        tables=tables,
        total_pending_review=total_pending,
    )


class TestEvaluatorWithMimicGT:
    """Run MappingEvaluator against subsets of the real OMOP ground truth."""

    def test_all_pending_gives_zero_precision_full_recall_miss(self, ground_truth):
        """If nothing is confirmed, precision and recall are both 0."""
        subset = ground_truth.mappings[:5]
        pending = [(e.table, e.column) for e in subset]
        report = _make_report([], pending)

        sub_gt = GroundTruth(dataset=ground_truth.dataset, mappings=subset)
        result = MappingEvaluator().evaluate(report, sub_gt)
        assert result.precision == 0.0
        assert result.recall == 0.0
        assert result.true_positives == 0
        assert result.false_negatives == len(subset)

    def test_without_an_ontology_semantic_entries_are_misses(self, ground_truth):
        """Meaning can't be checked without the ontology, so nothing scores as correct."""
        entry = ground_truth.mappings[0]
        report = _make_report([(entry.table, entry.column, "http://x#anything")], [])
        result = MappingEvaluator().evaluate(
            report, GroundTruth(dataset=ground_truth.dataset, mappings=[entry]))
        assert result.true_positives == 0
        assert "no ontology" in result.per_column[0].reason


# ─────────────────────────────────────────────────────────────────────────────
# Integration test — only runs when mapping_report_mimic.json exists
# ─────────────────────────────────────────────────────────────────────────────

_LIVE_REASON = f"output/mapping_report_mimic.json not found — run the MIMIC pipeline first"
TARGET_PRECISION = 0.90


@pytest.mark.skipif(not REPORT_PATH.exists(), reason=_LIVE_REASON)
class TestLiveMimicMappingPrecision:
    """Validate that a real pipeline run achieves >= 90% mapping precision."""

    @pytest.fixture(scope="class")
    def live_report(self) -> SemanticMappingReport:
        import json
        with open(REPORT_PATH) as f:
            return SemanticMappingReport.model_validate(json.load(f))

    def test_precision_meets_target(self, live_report, ground_truth):
        result = MappingEvaluator().evaluate(live_report, ground_truth, str(GT_PATH))
        assert result.precision >= TARGET_PRECISION, (
            f"Mapping precision {result.precision:.1%} below target {TARGET_PRECISION:.0%}. "
            f"TP={result.true_positives}, FP={result.false_positives}, "
            f"FN={result.false_negatives}"
        )

    def test_f1_is_positive(self, live_report, ground_truth):
        result = MappingEvaluator().evaluate(live_report, ground_truth, str(GT_PATH))
        assert result.f1 > 0.0, "F1 score is 0 — no correct mappings found"

    def test_per_column_count_matches_ground_truth(self, live_report, ground_truth):
        result = MappingEvaluator().evaluate(live_report, ground_truth, str(GT_PATH))
        assert result.total_ground_truth_entries == len(ground_truth.mappings)
        assert len(result.per_column) == len(ground_truth.mappings)
