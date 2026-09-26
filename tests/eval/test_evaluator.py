# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for MappingEvaluator — issue #48."""
from __future__ import annotations

import pytest

from semantic_mapper.eval.evaluator import MappingEvaluator
from semantic_mapper.eval.models import GroundTruth, GroundTruthEntry
from semantic_mapper.schema.mapping_output import (
    MappingEvidence, MappingStatus, PropertyMapping,
    SemanticMappingReport, TableMapping,
)


URI_A = "http://ont/hasPatientId"
URI_B = "http://ont/hasBirthDate"
URI_C = "http://ont/hasGender"


def _prop(col: str, status: MappingStatus, uri: str | None = None) -> PropertyMapping:
    return PropertyMapping(
        source_column=col,
        source_data_type="VARCHAR",
        is_nullable=False,
        status=status,
        confidence_score=0.9 if status == MappingStatus.CONFIRMED else 0.4,
        mapped_property_uri=uri,
        rationale="test",
    )


def _report(tables: list[TableMapping]) -> SemanticMappingReport:
    return SemanticMappingReport(
        ontology_uri="http://ont",
        database_name="test_db",
        settings_used={},
        tables=tables,
        total_pending_review=0,
    )


def _gt(*entries: tuple[str, str, str]) -> GroundTruth:
    return GroundTruth(
        dataset="test",
        mappings=[
            GroundTruthEntry(table=t, column=c, expected_property_uri=u)
            for t, c, u in entries
        ],
    )


evaluator = MappingEvaluator()


class TestPerfectMapping:
    def test_precision_recall_both_one(self):
        tables = [TableMapping(
            source_table="raw_patients",
            class_candidate_set=[],
            column_mappings=[_prop("id", MappingStatus.CONFIRMED, URI_A)],
        )]
        report = _report(tables)
        gt = _gt(("raw_patients", "id", URI_A))
        result = evaluator.evaluate(report, gt)
        assert result.precision == 1.0
        assert result.recall == 1.0
        assert result.f1 == 1.0
        assert result.true_positives == 1
        assert result.false_positives == 0
        assert result.false_negatives == 0

    def test_multiple_correct(self):
        tables = [TableMapping(
            source_table="raw_patients",
            class_candidate_set=[],
            column_mappings=[
                _prop("id", MappingStatus.CONFIRMED, URI_A),
                _prop("birthdate", MappingStatus.CONFIRMED, URI_B),
            ],
        )]
        report = _report(tables)
        gt = _gt(
            ("raw_patients", "id", URI_A),
            ("raw_patients", "birthdate", URI_B),
        )
        result = evaluator.evaluate(report, gt)
        assert result.true_positives == 2
        assert result.precision == 1.0
        assert result.recall == 1.0


class TestPartialMapping:
    def test_confirmed_wrong_uri_is_false_positive(self):
        tables = [TableMapping(
            source_table="raw_patients",
            class_candidate_set=[],
            column_mappings=[_prop("id", MappingStatus.CONFIRMED, URI_B)],  # wrong URI
        )]
        report = _report(tables)
        gt = _gt(("raw_patients", "id", URI_A))
        result = evaluator.evaluate(report, gt)
        assert result.true_positives == 0
        assert result.false_positives == 1
        assert result.false_negatives == 1
        assert result.precision == 0.0
        assert result.recall == 0.0

    def test_pending_column_is_false_negative(self):
        tables = [TableMapping(
            source_table="raw_patients",
            class_candidate_set=[],
            column_mappings=[_prop("id", MappingStatus.PENDING_REVIEW, URI_A)],
        )]
        report = _report(tables)
        gt = _gt(("raw_patients", "id", URI_A))
        result = evaluator.evaluate(report, gt)
        assert result.false_negatives == 1
        assert result.recall == 0.0
        assert result.pending_rate == 1.0

    def test_unmapped_column_counted(self):
        tables = [TableMapping(
            source_table="raw_patients",
            class_candidate_set=[],
            column_mappings=[_prop("id", MappingStatus.UNMAPPED)],
        )]
        report = _report(tables)
        gt = _gt(("raw_patients", "id", URI_A))
        result = evaluator.evaluate(report, gt)
        assert result.unmapped_rate == 1.0

    def test_missing_column_in_report_counts_as_fn(self):
        """Column in GT but missing from report altogether → false negative."""
        tables = [TableMapping(
            source_table="raw_patients",
            class_candidate_set=[],
            column_mappings=[],
        )]
        report = _report(tables)
        gt = _gt(("raw_patients", "id", URI_A))
        result = evaluator.evaluate(report, gt)
        assert result.false_negatives == 1
        assert result.recall == 0.0

    def test_half_correct_precision(self):
        tables = [TableMapping(
            source_table="raw_patients",
            class_candidate_set=[],
            column_mappings=[
                _prop("id", MappingStatus.CONFIRMED, URI_A),         # correct
                _prop("birthdate", MappingStatus.CONFIRMED, URI_C),   # wrong
            ],
        )]
        report = _report(tables)
        gt = _gt(
            ("raw_patients", "id", URI_A),
            ("raw_patients", "birthdate", URI_B),
        )
        result = evaluator.evaluate(report, gt)
        assert result.true_positives == 1
        assert result.false_positives == 1
        assert result.precision == 0.5
        assert result.recall == 0.5


class TestEdgeCases:
    def test_empty_ground_truth(self):
        report = _report([])
        gt = GroundTruth(dataset="test", mappings=[])
        result = evaluator.evaluate(report, gt)
        assert result.total_ground_truth_entries == 0
        assert result.precision == 0.0
        assert result.recall == 0.0
        assert result.f1 == 0.0

    def test_f1_computed_correctly(self):
        # Two GT entries: 1 TP, 1 FN (pending) → precision=1.0, recall=0.5
        # f1 = 2*1.0*0.5 / (1.0+0.5) = 2/3
        tables = [TableMapping(
            source_table="t",
            class_candidate_set=[],
            column_mappings=[
                _prop("a", MappingStatus.CONFIRMED, URI_A),     # TP
                _prop("b", MappingStatus.PENDING_REVIEW, None),  # FN
            ],
        )]
        report = _report(tables)
        gt = _gt(("t", "a", URI_A), ("t", "b", URI_B))
        result = evaluator.evaluate(report, gt)
        assert result.true_positives == 1
        assert result.false_positives == 0
        assert result.false_negatives == 1
        assert result.precision == 1.0
        assert result.recall == 0.5
        assert abs(result.f1 - 2 / 3) < 0.001

    def test_llm_cost_per_mapping(self):
        tables = [TableMapping(
            source_table="t",
            class_candidate_set=[],
            column_mappings=[_prop("a", MappingStatus.CONFIRMED, URI_A)],
        )]
        report = _report(tables)
        gt = _gt(("t", "a", URI_A))
        result = evaluator.evaluate(report, gt, llm_cost_usd=0.003)
        assert result.llm_cost_per_mapping == pytest.approx(0.003, rel=1e-3)

    def test_per_column_details(self):
        tables = [TableMapping(
            source_table="raw_patients",
            class_candidate_set=[],
            column_mappings=[
                _prop("id", MappingStatus.CONFIRMED, URI_A),
                _prop("gender", MappingStatus.PENDING_REVIEW),
            ],
        )]
        report = _report(tables)
        gt = _gt(
            ("raw_patients", "id", URI_A),
            ("raw_patients", "gender", URI_C),
        )
        result = evaluator.evaluate(report, gt)
        id_row = next(r for r in result.per_column if r.column == "id")
        gender_row = next(r for r in result.per_column if r.column == "gender")
        assert id_row.correct is True
        assert gender_row.correct is False
        assert gender_row.actual_status == "pending"
