# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from semantic_mapper.silver.models import (
    BatchWriteResult,
    PropertyKind,
    QuarantineRecord,
    RowAudit,
    RunStats,
    SilverRow,
    TransformOutcome,
    TransformResult,
)


def _audit() -> RowAudit:
    return RowAudit(
        bronze_source_ref="raw_party.party_id=P1",
        run_id="test-run",
        transformation_timestamp=datetime.now(timezone.utc),
        mapping_confidence_score=0.95,
        transformation_strategy="NullHandler",
    )


def _passthrough_result() -> TransformResult:
    return TransformResult(
        outcome=TransformOutcome.PASSTHROUGH,
        transformed_value="hello",
        original_value="hello",
        transformer_name="NullHandler",
        rationale="passthrough",
    )


# ---------------------------------------------------------------------------
# Frozen model mutation tests
# ---------------------------------------------------------------------------

class TestFrozenModels:
    def test_row_audit_is_frozen(self):
        audit = _audit()
        with pytest.raises(ValidationError):
            audit.run_id = "mutated"  # type: ignore[misc]

    def test_transform_result_is_frozen(self):
        result = _passthrough_result()
        with pytest.raises(ValidationError):
            result.outcome = TransformOutcome.QUARANTINED  # type: ignore[misc]

    def test_silver_row_is_frozen(self):
        row = SilverRow(
            source_table="raw_party",
            source_column="party_id",
            ontology_class_uri="http://ex.org#Party",
            property_uri="http://ex.org#id",
            property_local_name="party_id",
            property_kind=PropertyKind.DATATYPE,
            value="P1",
            transform_result=_passthrough_result(),
            audit=_audit(),
        )
        with pytest.raises(ValidationError):
            row.value = "mutated"  # type: ignore[misc]

    def test_quarantine_record_is_frozen(self):
        rec = QuarantineRecord(
            source_table="raw_party",
            source_column="dob",
            original_value="bad",
            property_uri="http://ex.org#dob",
            transformer_name="DateNormaliser",
            error_detail="unparseable",
            audit=_audit(),
        )
        with pytest.raises(ValidationError):
            rec.error_detail = "changed"  # type: ignore[misc]

    def test_run_stats_is_frozen(self):
        stats = RunStats(
            run_id="r",
            source_table="t",
            bronze_rows_read=10,
            batch_results=[],
            started_at=datetime.now(timezone.utc),
        )
        with pytest.raises(ValidationError):
            stats.bronze_rows_read = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# QUARANTINED requires error_detail
# ---------------------------------------------------------------------------

class TestQuarantinedRequiresErrorDetail:
    """QUARANTINED outcome should carry a non-None error_detail."""

    def test_quarantined_result_has_error_detail(self):
        result = TransformResult(
            outcome=TransformOutcome.QUARANTINED,
            transformed_value=None,
            original_value="bad_value",
            transformer_name="DateNormaliser",
            rationale="Could not parse",
            error_detail="Unparseable date 'bad_value'. Tried formats: [YYYY-MM-DD]",
        )
        assert result.error_detail is not None
        assert "bad_value" in result.error_detail

    def test_quarantined_without_error_detail_is_allowed_by_model_but_flagged(self):
        # The model allows None error_detail (field is Optional), but callers
        # must supply it. This test documents the expectation.
        result = TransformResult(
            outcome=TransformOutcome.QUARANTINED,
            transformed_value=None,
            original_value="x",
            transformer_name="Test",
            rationale="test",
            error_detail=None,
        )
        # Model doesn't enforce it at construction time; pipeline logic handles this
        assert result.outcome == TransformOutcome.QUARANTINED


# ---------------------------------------------------------------------------
# NULL_PRESERVED requires null_metadata
# ---------------------------------------------------------------------------

class TestNullPreservedMetadata:
    def test_null_preserved_has_null_metadata(self):
        result = TransformResult(
            outcome=TransformOutcome.NULL_PRESERVED,
            transformed_value=None,
            original_value=None,
            transformer_name="NullHandler",
            rationale="NULL preserved",
            null_metadata={
                "is_nullable": True,
                "source_column": "dob",
                "null_semantic": "explicit_null",
            },
        )
        assert result.null_metadata is not None
        assert result.null_metadata["null_semantic"] == "explicit_null"


# ---------------------------------------------------------------------------
# BatchWriteResult defaults
# ---------------------------------------------------------------------------

class TestBatchWriteResult:
    def test_rows_failed_defaults_to_zero(self):
        result = BatchWriteResult(
            rows_written=5,
            target_table="silver_raw_party",
            run_id="r1",
            written_at=datetime.now(timezone.utc),
        )
        assert result.rows_failed == 0

    def test_is_frozen(self):
        result = BatchWriteResult(
            rows_written=5,
            target_table="silver_raw_party",
            run_id="r1",
            written_at=datetime.now(timezone.utc),
        )
        with pytest.raises(ValidationError):
            result.rows_written = 99  # type: ignore[misc]
