# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect, text

from semantic_mapper.core.exceptions import SchemaInitialisationError
from semantic_mapper.schema.mapping_output import (
    ClassCandidate,
    MappingStatus,
    PropertyMapping,
    SemanticMappingReport,
    TableMapping,
    TransformationSpec,
)
from semantic_mapper.silver.models import (
    BatchWriteResult,
    PropertyKind,
    QuarantineRecord,
    RowAudit,
    SilverRow,
    TransformOutcome,
    TransformResult,
)
from semantic_mapper.silver.writer.mysql import MySQLSilverWriter


def _make_engine():
    return create_engine("sqlite:///:memory:")


def _make_settings():
    from semantic_mapper.config.settings import Settings
    return Settings(
        DATABASE_URL="sqlite:///:memory:",
        SILVER_DATABASE_URL="sqlite:///:memory:",
    )


def _audit(run_id="test-run"):
    return RowAudit(
        bronze_source_ref="raw_party.party_id=P1",
        run_id=run_id,
        transformation_timestamp=datetime.now(timezone.utc),
        mapping_confidence_score=0.95,
        transformation_strategy="NullHandler",
    )


def _passthrough_result(value="P1"):
    return TransformResult(
        outcome=TransformOutcome.PASSTHROUGH,
        transformed_value=value,
        original_value=value,
        transformer_name="NullHandler",
        rationale="passthrough",
    )


def _simple_report(confirmed=True):
    status = MappingStatus.CONFIRMED if confirmed else MappingStatus.PENDING_REVIEW
    return SemanticMappingReport(
        ontology_uri="http://ex.org/onto",
        database_name="bronze_db",
        settings_used={},
        tables=[
            TableMapping(
                source_table="raw_party",
                class_candidate_set=[
                    ClassCandidate(
                        uri="http://ex.org#Party",
                        label="Party",
                        rationale="test",
                        confidence_score=0.99,
                    )
                ],
                column_mappings=[
                    PropertyMapping(
                        source_column="party_id",
                        source_data_type="VARCHAR(50)",
                        is_nullable=False,
                        status=status,
                        confidence_score=0.99,
                        rationale="test",
                        mapped_property_uri="http://ex.org#id",
                    )
                ],
            )
        ],
        total_pending_review=0,
    )


# ---------------------------------------------------------------------------
# Table creation
# ---------------------------------------------------------------------------

class TestTableCreation:
    def test_silver_table_created(self):
        engine = _make_engine()
        writer = MySQLSilverWriter(engine=engine, settings=_make_settings())
        writer.initialise(_simple_report())
        insp = inspect(engine)
        assert "silver_raw_party" in insp.get_table_names()

    def test_quarantine_table_created(self):
        engine = _make_engine()
        writer = MySQLSilverWriter(engine=engine, settings=_make_settings())
        writer.initialise(_simple_report())
        insp = inspect(engine)
        assert "quarantine_raw_party" in insp.get_table_names()

    def test_silver_table_has_mapped_column(self):
        engine = _make_engine()
        writer = MySQLSilverWriter(engine=engine, settings=_make_settings())
        writer.initialise(_simple_report())
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("silver_raw_party")}
        assert "party_id" in cols

    def test_pending_mapping_column_not_added(self):
        """PENDING mappings should not add columns to the silver table."""
        engine = _make_engine()
        writer = MySQLSilverWriter(engine=engine, settings=_make_settings())
        writer.initialise(_simple_report(confirmed=False))
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("silver_raw_party")}
        assert "party_id" not in cols


# ---------------------------------------------------------------------------
# Idempotent init (schema migration)
# ---------------------------------------------------------------------------

class TestIdempotentInit:
    def test_double_init_does_not_raise(self):
        engine = _make_engine()
        writer = MySQLSilverWriter(engine=engine, settings=_make_settings())
        report = _simple_report()
        writer.initialise(report)
        # Second call should be a no-op, not raise
        writer.initialise(report)

    def test_schema_migration_adds_new_column(self):
        """Adding a new confirmed mapping on re-init adds the column."""
        engine = _make_engine()
        writer = MySQLSilverWriter(engine=engine, settings=_make_settings())
        writer.initialise(_simple_report())

        # Build a report with an additional column
        report2 = SemanticMappingReport(
            ontology_uri="http://ex.org/onto",
            database_name="bronze_db",
            settings_used={},
            tables=[
                TableMapping(
                    source_table="raw_party",
                    class_candidate_set=[
                        ClassCandidate(
                            uri="http://ex.org#Party",
                            label="Party",
                            rationale="test",
                            confidence_score=0.99,
                        )
                    ],
                    column_mappings=[
                        PropertyMapping(
                            source_column="party_id",
                            source_data_type="VARCHAR(50)",
                            is_nullable=False,
                            status=MappingStatus.CONFIRMED,
                            confidence_score=0.99,
                            rationale="test",
                            mapped_property_uri="http://ex.org#id",
                        ),
                        PropertyMapping(
                            source_column="dob",
                            source_data_type="VARCHAR(20)",
                            is_nullable=True,
                            status=MappingStatus.CONFIRMED,
                            confidence_score=0.90,
                            rationale="test",
                            mapped_property_uri="http://ex.org#dob",
                        ),
                    ],
                )
            ],
            total_pending_review=0,
        )
        writer.initialise(report2)
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("silver_raw_party")}
        assert "dob" in cols


# ---------------------------------------------------------------------------
# write_batch
# ---------------------------------------------------------------------------

class TestWriteBatch:
    def test_write_batch_returns_result(self):
        engine = _make_engine()
        writer = MySQLSilverWriter(engine=engine, settings=_make_settings())
        writer._set_run_context("r1", "raw_party")
        writer.initialise(_simple_report())

        rows = [
            SilverRow(
                source_table="raw_party",
                source_column="party_id",
                ontology_class_uri="http://ex.org#Party",
                property_uri="http://ex.org#id",
                property_local_name="party_id",
                property_kind=PropertyKind.DATATYPE,
                value="P1",
                transform_result=_passthrough_result("P1"),
                audit=_audit("r1"),
            )
        ]
        result = writer.write_batch(rows)
        assert result.rows_written == 1
        assert result.rows_failed == 0

    def test_write_batch_empty_returns_zero(self):
        engine = _make_engine()
        writer = MySQLSilverWriter(engine=engine, settings=_make_settings())
        writer._set_run_context("r1", "raw_party")
        result = writer.write_batch([])
        assert result.rows_written == 0


# ---------------------------------------------------------------------------
# write_quarantine never raises
# ---------------------------------------------------------------------------

class TestWriteQuarantine:
    def test_write_quarantine_never_raises_on_bad_table(self):
        """write_quarantine must swallow errors and return rows_failed count."""
        engine = _make_engine()
        writer = MySQLSilverWriter(engine=engine, settings=_make_settings())
        writer._set_run_context("r1", "raw_party")
        # quarantine_raw_party table doesn't exist — should not raise
        records = [
            QuarantineRecord(
                source_table="raw_party",
                source_column="dob",
                original_value="bad",
                property_uri="http://ex.org#dob",
                transformer_name="DateNormaliser",
                error_detail="unparseable",
                audit=_audit("r1"),
            )
        ]
        result = writer.write_quarantine(records)
        # Should fail gracefully, not raise
        assert isinstance(result, BatchWriteResult)
        assert result.rows_failed == 1

    def test_write_quarantine_succeeds_after_init(self):
        engine = _make_engine()
        writer = MySQLSilverWriter(engine=engine, settings=_make_settings())
        writer._set_run_context("r1", "raw_party")
        writer.initialise(_simple_report())
        records = [
            QuarantineRecord(
                source_table="raw_party",
                source_column="dob",
                original_value="bad",
                property_uri="http://ex.org#dob",
                transformer_name="DateNormaliser",
                error_detail="unparseable date",
                audit=_audit("r1"),
            )
        ]
        result = writer.write_quarantine(records)
        assert result.rows_written == 1
        assert result.rows_failed == 0

    def test_write_quarantine_empty_returns_zero(self):
        engine = _make_engine()
        writer = MySQLSilverWriter(engine=engine, settings=_make_settings())
        writer._set_run_context("r1", "raw_party")
        result = writer.write_quarantine([])
        assert result.rows_written == 0


# ---------------------------------------------------------------------------
# finalise
# ---------------------------------------------------------------------------

class TestFinalise:
    def test_finalise_returns_run_stats(self):
        engine = _make_engine()
        writer = MySQLSilverWriter(engine=engine, settings=_make_settings())
        writer._set_run_context("r1", "raw_party")
        stats = writer.finalise()
        assert stats.run_id == "r1"
        assert stats.duration_seconds is not None
        assert stats.finished_at is not None

