# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for compute_coverage — issue #49."""
from __future__ import annotations

from semantic_mapper.schema.mapping_output import (
    MappingStatus, PropertyMapping, SemanticMappingReport,
    TableMapping, compute_coverage,
)


def _prop(col: str, status: MappingStatus) -> PropertyMapping:
    return PropertyMapping(
        source_column=col,
        source_data_type="VARCHAR",
        is_nullable=False,
        status=status,
        confidence_score=0.9 if status == MappingStatus.CONFIRMED else 0.3,
        rationale="test",
    )


def _report(tables) -> SemanticMappingReport:
    return SemanticMappingReport(
        ontology_uri="http://ont",
        database_name="db",
        settings_used={},
        tables=tables,
        total_pending_review=0,
    )


class TestComputeCoverage:
    def test_all_confirmed(self):
        tables = [TableMapping(
            source_table="t1",
            class_candidate_set=[],
            column_mappings=[
                _prop("a", MappingStatus.CONFIRMED),
                _prop("b", MappingStatus.CONFIRMED),
            ],
        )]
        cov = compute_coverage(_report(tables))
        assert cov.total_columns == 2
        assert cov.confirmed == 2
        assert cov.coverage_pct == 100.0
        assert cov.pending_pct == 0.0
        assert cov.unmapped_pct == 0.0

    def test_mixed_statuses(self):
        tables = [TableMapping(
            source_table="t1",
            class_candidate_set=[],
            column_mappings=[
                _prop("a", MappingStatus.CONFIRMED),
                _prop("b", MappingStatus.PENDING_REVIEW),
                _prop("c", MappingStatus.UNMAPPED),
                _prop("d", MappingStatus.REJECTED),
            ],
        )]
        cov = compute_coverage(_report(tables))
        assert cov.total_columns == 4
        assert cov.confirmed == 1
        assert cov.pending_review == 1
        assert cov.unmapped == 1
        assert cov.rejected == 1
        assert cov.coverage_pct == 25.0
        assert cov.pending_pct == 25.0
        assert cov.unmapped_pct == 25.0

    def test_per_table_breakdown(self):
        tables = [
            TableMapping(
                source_table="t1",
                class_candidate_set=[],
                column_mappings=[
                    _prop("a", MappingStatus.CONFIRMED),
                    _prop("b", MappingStatus.CONFIRMED),
                ],
            ),
            TableMapping(
                source_table="t2",
                class_candidate_set=[],
                column_mappings=[
                    _prop("x", MappingStatus.UNMAPPED),
                ],
            ),
        ]
        cov = compute_coverage(_report(tables))
        assert len(cov.per_table) == 2
        t1 = next(t for t in cov.per_table if t.source_table == "t1")
        t2 = next(t for t in cov.per_table if t.source_table == "t2")
        assert t1.coverage_pct == 100.0
        assert t2.coverage_pct == 0.0

    def test_accepts_table_list_directly(self):
        tables = [TableMapping(
            source_table="t",
            class_candidate_set=[],
            column_mappings=[_prop("a", MappingStatus.CONFIRMED)],
        )]
        cov_from_list   = compute_coverage(tables)
        cov_from_report = compute_coverage(_report(tables))
        assert cov_from_list.total_columns == cov_from_report.total_columns
        assert cov_from_list.coverage_pct  == cov_from_report.coverage_pct

    def test_empty_report(self):
        cov = compute_coverage(_report([]))
        assert cov.total_columns == 0
        assert cov.coverage_pct == 0.0

    def test_coverage_attached_to_report(self):
        """SemanticMappingReport.coverage field should be populated by mapper."""
        tables = [TableMapping(
            source_table="t",
            class_candidate_set=[],
            column_mappings=[_prop("a", MappingStatus.CONFIRMED)],
        )]
        r = SemanticMappingReport(
            ontology_uri="http://ont",
            database_name="db",
            settings_used={},
            tables=tables,
            total_pending_review=0,
            coverage=compute_coverage(tables),
        )
        assert r.coverage is not None
        assert r.coverage.confirmed == 1
