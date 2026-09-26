# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from semantic_mapper.gold.models import GoldPipelineStats
from semantic_mapper.gold.pipeline import GoldPipeline
from semantic_mapper.gold.writer.mysql import MySQLGoldWriter


def _make_pipeline(definitions, silver_engine, gold_engine, settings):
    writer = MySQLGoldWriter(engine=gold_engine, settings=settings)
    return GoldPipeline(
        definitions=definitions,
        silver_engine=silver_engine,
        writer=writer,
        settings=settings,
    )


class TestPipelineMaterialisesRows:
    def test_pipeline_materialises_rows(
        self, simple_definition, silver_engine, gold_engine, gold_settings
    ):
        pipeline = _make_pipeline(
            [simple_definition], silver_engine, gold_engine, gold_settings
        )
        stats = pipeline.run(run_id="test-run-1")

        with gold_engine.connect() as conn:
            rows = conn.execute(
                text('SELECT * FROM "gold_party"')
            ).fetchall()

        assert len(rows) == 2
        assert stats.rows_per_table["gold_party"] == 2

    def test_value_map_raw_in_gold_uri_in_semantic_view(self, gold_engine, gold_settings):
        """Gold table keeps raw values; semantic view converts them to URIs via CASE WHEN."""
        from sqlalchemy import create_engine
        silver = create_engine("sqlite:///:memory:")
        with silver.begin() as conn:
            conn.execute(text("""
                CREATE TABLE silver_raw_party (
                    _silver_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    _bronze_source_ref TEXT NOT NULL,
                    _run_id TEXT NOT NULL,
                    _mapping_confidence_score REAL NOT NULL,
                    _ontology_class_uri TEXT NOT NULL,
                    status TEXT
                )
            """))
            conn.execute(text(
                "INSERT INTO silver_raw_party "
                "(_bronze_source_ref, _run_id, _mapping_confidence_score, _ontology_class_uri, status) "
                "VALUES ('raw.row=1', 's1', 0.9, 'http://ex.org#Party', 'ACTIVE')"
            ))

        from semantic_mapper.gold.models import GoldColumnSpec, GoldTableDefinition
        defn = GoldTableDefinition(
            gold_table_name="gold_party",
            primary_class_uri="http://ex.org#Party",
            class_label="party",
            source_silver_tables=["silver_raw_party"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_party",
                    source_column="status",
                    gold_column_name="status",
                    source_data_type="VARCHAR(50)",
                    property_uri="http://ex.org#hasStatus",
                    value_map={"ACTIVE": "http://ex.org#Active", "CLOSED": "http://ex.org#Closed"},
                )
            ],
        )
        pipeline = _make_pipeline([defn], silver, gold_engine, gold_settings)
        pipeline.run(run_id="r1")

        with gold_engine.connect() as conn:
            # Gold table retains raw value
            gold_row = conn.execute(text('SELECT "status" FROM "gold_party"')).fetchone()
            assert gold_row[0] == "ACTIVE"
            # Semantic view converts to URI
            sem_row = conn.execute(text('SELECT "hasStatus" FROM "gold_semantic_party"')).fetchone()
            assert sem_row[0] == "http://ex.org#Active"


class TestPipelineEmptySilver:
    def test_empty_silver_no_crash(
        self, simple_definition, gold_engine, gold_settings
    ):
        # Create an empty silver engine
        empty_silver = create_engine("sqlite:///:memory:")
        with empty_silver.begin() as conn:
            conn.execute(text("""
                CREATE TABLE silver_raw_party (
                    _silver_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    _bronze_source_ref TEXT NOT NULL,
                    _run_id TEXT NOT NULL,
                    _mapping_confidence_score REAL NOT NULL,
                    _ontology_class_uri TEXT NOT NULL,
                    party_id TEXT,
                    dob TEXT
                )
            """))

        pipeline = _make_pipeline(
            [simple_definition], empty_silver, gold_engine, gold_settings
        )
        stats = pipeline.run(run_id="test-run-empty")

        assert stats.rows_per_table["gold_party"] == 0
        assert stats.total_rows_written == 0


class TestPipelineReturnsStats:
    def test_stats_fields_populated(
        self, simple_definition, silver_engine, gold_engine, gold_settings
    ):
        pipeline = _make_pipeline(
            [simple_definition], silver_engine, gold_engine, gold_settings
        )
        stats = pipeline.run(run_id="stats-run")

        assert isinstance(stats, GoldPipelineStats)
        assert stats.run_id == "stats-run"
        assert "gold_party" in stats.tables_written
        assert stats.total_rows_written == stats.rows_per_table["gold_party"]
        assert stats.finished_at is not None
        assert stats.duration_seconds is not None
        assert stats.duration_seconds >= 0.0


class TestPipelineOverwriteSemantics:
    def test_second_run_replaces_first_run(
        self, simple_definition, silver_engine, gold_engine, gold_settings
    ):
        writer = MySQLGoldWriter(engine=gold_engine, settings=gold_settings)
        pipeline = GoldPipeline(
            definitions=[simple_definition],
            silver_engine=silver_engine,
            writer=writer,
            settings=gold_settings,
        )

        stats1 = pipeline.run(run_id="run-1")
        assert stats1.rows_per_table["gold_party"] == 2

        # Re-run with the SAME writer (re-uses same gold_engine)
        pipeline2 = GoldPipeline(
            definitions=[simple_definition],
            silver_engine=silver_engine,
            writer=MySQLGoldWriter(engine=gold_engine, settings=gold_settings),
            settings=gold_settings,
        )
        stats2 = pipeline2.run(run_id="run-2")

        with gold_engine.connect() as conn:
            count = conn.execute(
                text('SELECT COUNT(*) FROM "gold_party"')
            ).scalar()

        # Overwrite semantics: only run-2 rows present
        assert count == 2
        assert stats2.rows_per_table["gold_party"] == 2
