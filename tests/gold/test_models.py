# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest
from datetime import datetime, timezone

from semantic_mapper.gold.models import (
    GoldColumnSpec,
    GoldPipelineStats,
    GoldTableDefinition,
    GoldWriteResult,
)


class TestGoldColumnSpec:
    def test_frozen(self):
        spec = GoldColumnSpec(
            silver_table="silver_raw_party",
            source_column="party_id",
            gold_column_name="party_id",
            source_data_type="VARCHAR(50)",
            property_uri="http://ex.org#id",
        )
        with pytest.raises(Exception):
            spec.gold_column_name = "new_name"  # type: ignore[misc]


class TestGoldTableDefinition:
    def test_frozen(self, simple_definition):
        with pytest.raises(Exception):
            simple_definition.gold_table_name = "changed"  # type: ignore[misc]

    def test_fields(self, simple_definition):
        assert simple_definition.gold_table_name == "gold_party"
        assert simple_definition.primary_class_uri == "http://ex.org/ontology#Party"
        assert simple_definition.class_label == "party"
        assert simple_definition.source_silver_tables == ["silver_raw_party"]
        assert len(simple_definition.column_specs) == 2


class TestGoldWriteResult:
    def test_frozen(self):
        result = GoldWriteResult(
            rows_written=5,
            rows_failed=0,
            target_table="gold_party",
            run_id="r1",
            written_at=datetime.now(timezone.utc),
        )
        with pytest.raises(Exception):
            result.rows_written = 0  # type: ignore[misc]

    def test_defaults(self):
        result = GoldWriteResult(
            rows_written=10,
            target_table="gold_party",
            run_id="r1",
            written_at=datetime.now(timezone.utc),
        )
        assert result.rows_failed == 0


class TestGoldPipelineStats:
    def test_frozen(self):
        stats = GoldPipelineStats(
            run_id="r1",
            tables_written=["gold_party"],
            rows_per_table={"gold_party": 5},
            total_rows_written=5,
            started_at=datetime.now(timezone.utc),
        )
        with pytest.raises(Exception):
            stats.total_rows_written = 0  # type: ignore[misc]

    def test_optional_fields_default_to_none(self):
        stats = GoldPipelineStats(
            run_id="r1",
            tables_written=[],
            rows_per_table={},
            total_rows_written=0,
            started_at=datetime.now(timezone.utc),
        )
        assert stats.finished_at is None
        assert stats.duration_seconds is None
