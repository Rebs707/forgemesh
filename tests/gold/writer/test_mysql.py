# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect, text

from semantic_mapper.config.settings import Settings
from semantic_mapper.core.exceptions import GoldSchemaInitialisationError
from semantic_mapper.gold.models import (
    GoldColumnSpec,
    GoldPipelineStats,
    GoldTableDefinition,
    GoldWriteResult,
)
from semantic_mapper.gold.writer.mysql import MySQLGoldWriter


def _make_engine():
    return create_engine("sqlite:///:memory:")


def _make_settings():
    return Settings(
        DATABASE_URL="sqlite:///:memory:",
        SILVER_DATABASE_URL="sqlite:///:memory:",
        GOLD_DATABASE_URL="sqlite:///:memory:",
    )


def _make_definition(table_name="gold_party", cols=None):
    if cols is None:
        cols = [
            GoldColumnSpec(
                silver_table="silver_raw_party",
                source_column="party_id",
                gold_column_name="party_id",
                source_data_type="VARCHAR(50)",
                property_uri="http://ex.org#id",
            )
        ]
    return GoldTableDefinition(
        gold_table_name=table_name,
        primary_class_uri="http://ex.org#Party",
        class_label="party",
        source_silver_tables=["silver_raw_party"],
        column_specs=cols,
    )


def _make_row(bronze_ref="raw_party.party_id=P1", run_id="r1"):
    """A gold row dict as produced by GoldPipeline — includes silver provenance
    columns that must be stripped before writing to the gold table."""
    return {
        "_ontology_class_uri":       "http://ex.org#Party",
        "_bronze_source_ref":        bronze_ref,
        "_silver_run_id":            "silver-run-1",
        "_gold_run_id":              run_id,
        "_mapping_confidence_score": 0.95,
        "party_id":                  "P1",
        # provenance columns from silver (must be stripped before gold write)
        "_transformation_timestamp": "2026-04-17 10:00:00.000000",
        "_transformation_strategy":  "ValueMapper",
        "_expansion_index":          0,
        "_discriminator_value":      "EMERGENCY",
    }


class TestInitialiseCreatesTable:
    def test_gold_table_created(self):
        engine = _make_engine()
        writer = MySQLGoldWriter(engine=engine, settings=_make_settings())
        writer.initialise([_make_definition()])
        insp = inspect(engine)
        assert "gold_party" in insp.get_table_names()

    def test_gold_table_has_audit_columns(self):
        engine = _make_engine()
        writer = MySQLGoldWriter(engine=engine, settings=_make_settings())
        writer.initialise([_make_definition()])
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("gold_party")}
        assert "_ontology_class_uri" in cols
        assert "_bronze_source_ref" in cols
        assert "_silver_run_id" in cols
        assert "_gold_run_id" in cols
        assert "_mapping_confidence_score" in cols

    def test_gold_table_has_data_columns(self):
        engine = _make_engine()
        writer = MySQLGoldWriter(engine=engine, settings=_make_settings())
        writer.initialise([_make_definition()])
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("gold_party")}
        assert "party_id" in cols

    def test_no_lineage_table_created(self):
        """Lineage is powered from the mapping report file — no companion table."""
        engine = _make_engine()
        writer = MySQLGoldWriter(engine=engine, settings=_make_settings())
        writer.initialise([_make_definition()])
        insp = inspect(engine)
        assert "gold_party_lineage" not in insp.get_table_names()


class TestIdempotentSchema:
    def test_double_initialise_does_not_raise(self):
        engine = _make_engine()
        writer = MySQLGoldWriter(engine=engine, settings=_make_settings())
        defn = _make_definition()
        writer.initialise([defn])
        writer.initialise([defn])

    def test_new_column_added_on_reinit(self):
        engine = _make_engine()
        writer = MySQLGoldWriter(engine=engine, settings=_make_settings())
        writer.initialise([_make_definition()])

        defn2 = _make_definition(
            cols=[
                GoldColumnSpec(
                    silver_table="silver_raw_party",
                    source_column="party_id",
                    gold_column_name="party_id",
                    source_data_type="VARCHAR(50)",
                    property_uri="http://ex.org#id",
                ),
                GoldColumnSpec(
                    silver_table="silver_raw_party",
                    source_column="dob",
                    gold_column_name="dob",
                    source_data_type="VARCHAR(20)",
                    property_uri="http://ex.org#dob",
                ),
            ]
        )
        writer.initialise([defn2])
        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("gold_party")}
        assert "dob" in cols


class TestWriteBatchInsertsRows:
    def test_write_batch_inserts_rows(self):
        engine = _make_engine()
        writer = MySQLGoldWriter(engine=engine, settings=_make_settings())
        writer._run_id = "r1"
        writer.initialise([_make_definition()])

        result = writer.write_batch("gold_party", [_make_row(run_id="r1")])
        assert result.rows_written == 1
        assert result.rows_failed == 0

        with engine.connect() as conn:
            count = conn.execute(text('SELECT COUNT(*) FROM "gold_party"')).scalar()
        assert count == 1

    def test_write_batch_empty_returns_zero(self):
        engine = _make_engine()
        writer = MySQLGoldWriter(engine=engine, settings=_make_settings())
        writer._run_id = "r1"
        result = writer.write_batch("gold_party", [])
        assert result.rows_written == 0
        assert result.rows_failed == 0

    def test_gold_table_does_not_have_provenance_columns(self):
        """The four silver provenance columns must be stripped from the gold table."""
        engine = _make_engine()
        writer = MySQLGoldWriter(engine=engine, settings=_make_settings())
        writer._run_id = "r1"
        writer.initialise([_make_definition()])

        writer.write_batch("gold_party", [_make_row(run_id="r1")])

        insp = inspect(engine)
        gold_cols = {c["name"] for c in insp.get_columns("gold_party")}
        assert "_transformation_strategy" not in gold_cols
        assert "_transformation_timestamp" not in gold_cols
        assert "_expansion_index" not in gold_cols
        assert "_discriminator_value" not in gold_cols

    def test_multiple_rows_inserted(self):
        engine = _make_engine()
        writer = MySQLGoldWriter(engine=engine, settings=_make_settings())
        writer._run_id = "r1"
        writer.initialise([_make_definition()])

        writer.write_batch("gold_party", [
            _make_row("raw_party.party_id=P1", run_id="r1"),
            _make_row("raw_party.party_id=P2", run_id="r1"),
        ])

        with engine.connect() as conn:
            count = conn.execute(text('SELECT COUNT(*) FROM "gold_party"')).scalar()
        assert count == 2


class TestFinaliseReturnsStats:
    def test_finalise_returns_gold_pipeline_stats(self):
        engine = _make_engine()
        writer = MySQLGoldWriter(engine=engine, settings=_make_settings())
        writer.initialise([_make_definition()])

        stats = writer.finalise("run-1", {"gold_party": 5})
        assert isinstance(stats, GoldPipelineStats)
        assert stats.run_id == "run-1"
        assert stats.total_rows_written == 5
        assert stats.finished_at is not None
        assert stats.duration_seconds is not None
        assert stats.duration_seconds >= 0.0


class TestGoldMode3SemanticView:
    def _make_settings_mode3(self):
        return Settings(
            DATABASE_URL="sqlite:///:memory:",
            SILVER_DATABASE_URL="sqlite:///:memory:",
            GOLD_DATABASE_URL="sqlite:///:memory:",
            GOLD_MODE=3,
        )

    def test_semantic_view_created_for_mode3(self):
        engine = _make_engine()
        writer = MySQLGoldWriter(engine=engine, settings=self._make_settings_mode3())
        writer.initialise([_make_definition()])
        with engine.connect() as conn:
            views = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='view'")
            ).fetchall()
        view_names = {r[0] for r in views}
        assert "gold_semantic_party" in view_names

    def test_semantic_view_always_created(self):
        """Semantic view is created regardless of gold_mode."""
        engine = _make_engine()
        writer = MySQLGoldWriter(engine=engine, settings=_make_settings())  # mode 1
        writer.initialise([_make_definition()])
        with engine.connect() as conn:
            views = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='view'")
            ).fetchall()
        view_names = {r[0] for r in views}
        assert "gold_semantic_party" in view_names

    def test_semantic_view_contains_data_via_alias(self):
        engine = _make_engine()
        writer = MySQLGoldWriter(engine=engine, settings=self._make_settings_mode3())
        writer._run_id = "r1"
        writer.initialise([_make_definition()])
        writer.write_batch("gold_party", [_make_row(run_id="r1")])

        with engine.connect() as conn:
            count = conn.execute(
                text('SELECT COUNT(*) FROM "gold_semantic_party"')
            ).scalar()
        assert count == 1

    def test_semantic_view_case_when_converts_raw_to_uri(self):
        """Semantic view uses CASE WHEN to convert raw values to URIs at query time."""
        status_col = GoldColumnSpec(
            silver_table="silver_raw_party",
            source_column="status",
            gold_column_name="status",
            source_data_type="VARCHAR(50)",
            property_uri="http://ex.org#hasStatus",
            value_map={"ACTIVE": "http://ex.org#Active", "CLOSED": "http://ex.org#Closed"},
        )
        defn = _make_definition(cols=[status_col])
        engine = _make_engine()
        writer = MySQLGoldWriter(engine=engine, settings=_make_settings())
        writer._run_id = "r1"
        writer.initialise([defn])

        # Gold table gets raw value (pipeline no longer substitutes)
        row = {
            "_ontology_class_uri": "http://ex.org#Party",
            "_bronze_source_ref": "raw.row=1",
            "_silver_run_id": "s1",
            "_gold_run_id": "r1",
            "_mapping_confidence_score": 0.9,
            "status": "ACTIVE",
        }
        writer.write_batch("gold_party", [row])

        with engine.connect() as conn:
            # Gold table has raw value
            gold_val = conn.execute(text('SELECT "status" FROM "gold_party"')).scalar()
            assert gold_val == "ACTIVE"
            # Semantic view converts to URI via CASE WHEN
            sem_val = conn.execute(
                text('SELECT "hasStatus" FROM "gold_semantic_party"')
            ).scalar()
        assert sem_val == "http://ex.org#Active"
