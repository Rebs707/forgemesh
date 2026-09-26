# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import logging
import pytest
from sqlalchemy import create_engine, text

from semantic_mapper.gold.models import GoldColumnSpec, GoldTableDefinition
from semantic_mapper.rdf.models import RdfPipelineStats
from semantic_mapper.rdf.pipeline import RdfPipeline
from semantic_mapper.rdf.writer.rdflib_writer import RdflibWriter


def _make_pipeline(definitions, gold_engine, settings):
    writer = RdflibWriter()
    return RdfPipeline(
        definitions=definitions,
        gold_engine=gold_engine,
        writer=writer,
        settings=settings,
    ), writer


class TestPipelineTriplesWritten:
    def test_triple_count_matches_expected(
        self, simple_definitions, gold_engine, rdf_settings
    ):
        # gold_patient: 2 rows × (1 rdf:type + 2 props + 2 provenance) = 10 triples
        # gold_encounter: 1 row × (1 rdf:type + 2 props + 2 provenance) = 5 triples
        # total = 15
        pipeline, writer = _make_pipeline(simple_definitions, gold_engine, rdf_settings)
        stats = pipeline.run(run_id="test-run-1")

        assert stats.triples_written == 15
        assert len(list(writer.graph)) == 15

    def test_tables_processed_populated(
        self, simple_definitions, gold_engine, rdf_settings
    ):
        pipeline, _ = _make_pipeline(simple_definitions, gold_engine, rdf_settings)
        stats = pipeline.run(run_id="test-run-tables")

        assert "gold_patient" in stats.tables_processed
        assert "gold_encounter" in stats.tables_processed


class TestPipelineStats:
    def test_stats_fields_populated(
        self, simple_definitions, gold_engine, rdf_settings
    ):
        pipeline, _ = _make_pipeline(simple_definitions, gold_engine, rdf_settings)
        stats = pipeline.run(run_id="stats-run")

        assert isinstance(stats, RdfPipelineStats)
        assert stats.run_id == "stats-run"
        assert stats.triples_written == 15
        assert stats.finished_at is not None
        assert stats.duration_seconds is not None
        assert stats.duration_seconds >= 0.0
        assert stats.named_graph == rdf_settings.RDF_NAMED_GRAPH

    def test_run_id_auto_generated_when_none(
        self, simple_definitions, gold_engine, rdf_settings
    ):
        pipeline, _ = _make_pipeline(simple_definitions, gold_engine, rdf_settings)
        stats = pipeline.run()

        assert stats.run_id is not None
        assert len(stats.run_id) > 0


class TestPipelineEmptyGold:
    def test_empty_gold_table_no_crash(
        self, simple_definitions, rdf_settings
    ):
        empty_gold = create_engine("sqlite:///:memory:")
        with empty_gold.begin() as conn:
            conn.execute(text("""
                CREATE TABLE gold_patient (
                    _gold_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    _ontology_class_uri TEXT NOT NULL,
                    _bronze_source_ref TEXT NOT NULL,
                    _silver_run_id TEXT NOT NULL,
                    _gold_run_id TEXT NOT NULL,
                    _mapping_confidence_score REAL NOT NULL,
                    id TEXT,
                    date_of_birth TEXT
                )
            """))
            conn.execute(text("""
                CREATE TABLE gold_encounter (
                    _gold_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    _ontology_class_uri TEXT NOT NULL,
                    _bronze_source_ref TEXT NOT NULL,
                    _silver_run_id TEXT NOT NULL,
                    _gold_run_id TEXT NOT NULL,
                    _mapping_confidence_score REAL NOT NULL,
                    patient TEXT,
                    encounter_date TEXT
                )
            """))

        pipeline, _ = _make_pipeline(simple_definitions, empty_gold, rdf_settings)
        stats = pipeline.run(run_id="test-empty")

        assert stats.triples_written == 0


class TestPipelineOverwriteSemantics:
    def test_second_run_replaces_first(
        self, simple_definitions, gold_engine, rdf_settings
    ):
        writer = RdflibWriter()
        pipeline1 = RdfPipeline(
            definitions=simple_definitions,
            gold_engine=gold_engine,
            writer=writer,
            settings=rdf_settings,
        )
        stats1 = pipeline1.run(run_id="run-1")
        assert stats1.triples_written == 15

        # Second run reuses same writer (same in-memory store) but re-initialises
        pipeline2 = RdfPipeline(
            definitions=simple_definitions,
            gold_engine=gold_engine,
            writer=writer,
            settings=rdf_settings,
        )
        stats2 = pipeline2.run(run_id="run-2")

        # initialise() clears the graph — only run-2 triples present
        assert stats2.triples_written == 15
        assert len(list(writer.graph)) == 15


# ---------------------------------------------------------------------------
# BUG-05 — Duplicate subject detection and logging
# ---------------------------------------------------------------------------

class TestDuplicateSubjectLogging:
    """When two gold rows produce the same subject URI (duplicate key values),
    the pipeline must log a WARNING.

    Root cause: refine_id_columns may pick a name/code column as id_column when
    no *_id column is unique.  If two rows have the same value in that column,
    TripleBuilder mints the same subject URI for both rows.  SPARQL COUNT DISTINCT
    then returns N-1 subjects for N gold rows — the C1 entity count check fails.

    The fix (BUG-05): _process_definition tracks distinct subjects and logs a
    WARNING when rows_total > len(seen_subjects).
    """

    COUNTRY_CLASS = "http://ex.org/ontology#Country"

    def _make_country_def(self):
        return GoldTableDefinition(
            gold_table_name="gold_country",
            primary_class_uri=self.COUNTRY_CLASS,
            class_label="country",
            source_silver_tables=["silver_raw_countries"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_countries",
                    source_column="name",
                    gold_column_name="name",
                    source_data_type="VARCHAR(100)",
                    property_uri="http://ex.org/ontology#hasName",
                ),
            ],
            id_column="name",   # name column used as key — duplicates will merge
        )

    def _make_gold_engine_with_duplicate(self):
        """Three country rows: two share the same name — they will map to the same URI."""
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE gold_country (
                    _gold_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    _ontology_class_uri       TEXT NOT NULL,
                    _bronze_source_ref        TEXT NOT NULL,
                    _silver_run_id            TEXT NOT NULL,
                    _gold_run_id              TEXT NOT NULL,
                    _mapping_confidence_score REAL NOT NULL,
                    name                      TEXT
                )
            """))
            conn.execute(text("""
                INSERT INTO gold_country
                    (_ontology_class_uri, _bronze_source_ref, _silver_run_id,
                     _gold_run_id, _mapping_confidence_score, name)
                VALUES
                    ('http://ex.org/ontology#Country', 'ref=1', 'sr', 'gr', 0.9, 'France'),
                    ('http://ex.org/ontology#Country', 'ref=2', 'sr', 'gr', 0.9, 'Germany'),
                    ('http://ex.org/ontology#Country', 'ref=3', 'sr', 'gr', 0.9, 'France')
            """))
        return engine

    def _make_gold_engine_no_duplicate(self):
        """Three country rows with distinct names — no merge expected."""
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE gold_country (
                    _gold_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    _ontology_class_uri       TEXT NOT NULL,
                    _bronze_source_ref        TEXT NOT NULL,
                    _silver_run_id            TEXT NOT NULL,
                    _gold_run_id              TEXT NOT NULL,
                    _mapping_confidence_score REAL NOT NULL,
                    name                      TEXT
                )
            """))
            conn.execute(text("""
                INSERT INTO gold_country
                    (_ontology_class_uri, _bronze_source_ref, _silver_run_id,
                     _gold_run_id, _mapping_confidence_score, name)
                VALUES
                    ('http://ex.org/ontology#Country', 'ref=1', 'sr', 'gr', 0.9, 'France'),
                    ('http://ex.org/ontology#Country', 'ref=2', 'sr', 'gr', 0.9, 'Germany'),
                    ('http://ex.org/ontology#Country', 'ref=3', 'sr', 'gr', 0.9, 'Italy')
            """))
        return engine

    def test_duplicate_subject_logs_warning(self, rdf_settings, caplog):
        """Two gold rows with the same name → same subject URI → WARNING logged."""
        defn = self._make_country_def()
        engine = self._make_gold_engine_with_duplicate()

        pipeline, _ = _make_pipeline([defn], engine, rdf_settings)
        with caplog.at_level(logging.WARNING, logger="semantic_mapper.rdf.pipeline"):
            pipeline.run(run_id="dup-test")

        merge_warnings = [
            r for r in caplog.records
            if "merge" in r.message.lower() and "gold_country" in r.message
        ]
        assert len(merge_warnings) >= 1, "Expected at least one merge warning for gold_country"
        assert "3 gold rows" in merge_warnings[0].message
        assert "2 distinct subjects" in merge_warnings[0].message

    def test_no_duplicates_no_warning(self, rdf_settings, caplog):
        """Three rows with distinct names → no merge warning."""
        defn = self._make_country_def()
        engine = self._make_gold_engine_no_duplicate()

        pipeline, _ = _make_pipeline([defn], engine, rdf_settings)
        with caplog.at_level(logging.WARNING, logger="semantic_mapper.rdf.pipeline"):
            pipeline.run(run_id="nodup-test")

        merge_warnings = [
            r for r in caplog.records
            if "merge" in r.message.lower()
        ]
        assert len(merge_warnings) == 0

    def test_triples_still_written_on_duplicate(self, rdf_settings):
        """Even with duplicate subjects the pipeline must still write triples and not crash."""
        defn = self._make_country_def()
        engine = self._make_gold_engine_with_duplicate()

        pipeline, writer = _make_pipeline([defn], engine, rdf_settings)
        stats = pipeline.run(run_id="dup-triples")

        # 3 gold rows → but 2 of them merge: 2 distinct subjects × (1 type + 1 prop) = 4 unique triples
        # (the third 'France' row overwrites the first's triples in the graph)
        assert stats.triples_written > 0
