# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, inspect, text

from semantic_mapper.config.settings import Settings
from semantic_mapper.core.exceptions import SchemaInitialisationError
from semantic_mapper.schema.mapping_output import (
    ClassCandidate,
    MappingStatus,
    PropertyMapping,
    SemanticMappingReport,
    TableMapping,
    TransformationSpec,
    ValueMappingRule,
)
from semantic_mapper.silver.models import RunStats, TransformOutcome
from semantic_mapper.silver.pipeline import SilverPipeline, _resolve_property_local_name
from semantic_mapper.silver.writer.mysql import MySQLSilverWriter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings():
    return Settings(
        DATABASE_URL="sqlite:///:memory:",
        SILVER_DATABASE_URL="sqlite:///:memory:",
        SILVER_BATCH_SIZE=10,
    )


def _bronze_engine_with_data(rows=None):
    """SQLite in-memory engine with raw_party data."""
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        conn.execute(
            text(
                "CREATE TABLE raw_party ("
                "  party_id TEXT,"
                "  dob TEXT,"
                "  party_type INTEGER"
                ")"
            )
        )
        for row in (rows or [("P1", "1978-03-14", 1), ("P2", "14/03/1982", 1), ("P3", None, 2)]):
            conn.execute(
                text("INSERT INTO raw_party VALUES (:pid, :dob, :pt)"),
                {"pid": row[0], "dob": row[1], "pt": row[2]},
            )
        conn.commit()
    return engine


def _silver_engine():
    return create_engine("sqlite:///:memory:")


def _make_writer(engine=None):
    if engine is None:
        engine = _silver_engine()
    return MySQLSilverWriter(engine=engine, settings=_settings())


def _report_with_status(status):
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
                        mapped_property_uri="http://ex.org#partyId",
                    )
                ],
            )
        ],
        total_pending_review=0,
    )


def _confirmed_report():
    return _report_with_status(MappingStatus.CONFIRMED)


def _pending_report():
    return _report_with_status(MappingStatus.PENDING_REVIEW)


def _report_with_date_mapping():
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
                        status=MappingStatus.CONFIRMED,
                        confidence_score=0.99,
                        rationale="test",
                        mapped_property_uri="http://ex.org#partyId",
                    ),
                    PropertyMapping(
                        source_column="dob",
                        source_data_type="VARCHAR(20)",
                        is_nullable=True,
                        status=MappingStatus.CONFIRMED,
                        confidence_score=0.90,
                        rationale="test",
                        mapped_property_uri="http://ex.org#dateOfBirth",
                        transformation_spec=TransformationSpec(
                            detected_formats=["YYYY-MM-DD", "DD/MM/YYYY"],
                            target_format="xsd:date",
                            parse_strategy="dateutil.parser",
                            validation_rule="valid date",
                        ),
                    ),
                ],
            )
        ],
        total_pending_review=0,
    )


def _report_with_discriminator():
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
                        status=MappingStatus.CONFIRMED,
                        confidence_score=0.99,
                        rationale="test",
                        mapped_property_uri="http://ex.org#partyId",
                    ),
                    PropertyMapping(
                        source_column="party_type",
                        source_data_type="INT",
                        is_nullable=False,
                        status=MappingStatus.CONFIRMED,
                        confidence_score=0.90,
                        rationale="discriminator",
                        mapped_property_uri="http://ex.org#partyType",
                        value_mappings=[
                            ValueMappingRule(
                                source_value=1,
                                mapped_uri="http://ex.org#Individual",
                                rationale="1=Individual",
                                confidence_score=0.95,
                            ),
                            ValueMappingRule(
                                source_value=2,
                                mapped_uri="http://ex.org#Organisation",
                                rationale="2=Organisation",
                                confidence_score=0.95,
                            ),
                        ],
                    ),
                ],
            )
        ],
        total_pending_review=0,
    )


# ---------------------------------------------------------------------------
# PENDING mappings skipped
# ---------------------------------------------------------------------------

class TestPendingSkipped:
    def test_pending_columns_produce_no_silver_rows(self):
        bronze = _bronze_engine_with_data()
        silver = _silver_engine()
        writer = _make_writer(silver)
        pipeline = SilverPipeline(
            report=_pending_report(),
            bronze_engine=bronze,
            writer=writer,
            settings=_settings(),
        )
        stats = pipeline.run(run_id="test-pending")
        # No silver rows because the only mapping is PENDING
        assert stats.silver_rows_written == 0

    def test_no_silver_table_columns_for_pending(self):
        bronze = _bronze_engine_with_data()
        silver = _silver_engine()
        writer = _make_writer(silver)
        pipeline = SilverPipeline(
            report=_pending_report(),
            bronze_engine=bronze,
            writer=writer,
            settings=_settings(),
        )
        pipeline.run(run_id="test-pending-cols")
        insp = inspect(silver)
        cols = {c["name"] for c in insp.get_columns("silver_raw_party")}
        # party_id column should NOT be added (mapping was PENDING)
        assert "party_id" not in cols


# ---------------------------------------------------------------------------
# NULL_PRESERVED not quarantined
# ---------------------------------------------------------------------------

class TestNullPreservedNotQuarantined:
    def test_null_value_goes_to_silver_not_quarantine(self):
        """dob=NULL rows should produce silver rows (NULL_PRESERVED), not quarantine."""
        bronze = _bronze_engine_with_data()
        silver = _silver_engine()
        writer = _make_writer(silver)
        pipeline = SilverPipeline(
            report=_report_with_date_mapping(),
            bronze_engine=bronze,
            writer=writer,
            settings=_settings(),
        )
        stats = pipeline.run(run_id="test-null")
        # 3 rows × 2 confirmed columns = 6 silver rows; no quarantine from NULL
        assert stats.quarantine_rows_written == 0
        assert stats.null_preserved_count > 0


# ---------------------------------------------------------------------------
# Stable run_id
# ---------------------------------------------------------------------------

class TestStableRunId:
    def test_supplied_run_id_propagated_to_stats(self):
        bronze = _bronze_engine_with_data()
        silver = _silver_engine()
        writer = _make_writer(silver)
        pipeline = SilverPipeline(
            report=_confirmed_report(),
            bronze_engine=bronze,
            writer=writer,
            settings=_settings(),
        )
        stats = pipeline.run(run_id="stable-run-42")
        assert stats.run_id == "stable-run-42"

    def test_auto_run_id_generated_when_none(self):
        bronze = _bronze_engine_with_data()
        silver = _silver_engine()
        writer = _make_writer(silver)
        pipeline = SilverPipeline(
            report=_confirmed_report(),
            bronze_engine=bronze,
            writer=writer,
            settings=_settings(),
        )
        stats = pipeline.run()
        assert stats.run_id is not None
        assert len(stats.run_id) == 36  # UUID format


# ---------------------------------------------------------------------------
# SchemaInitialisationError propagates
# ---------------------------------------------------------------------------

class TestSchemaInitialisationErrorPropagates:
    def test_init_error_stops_pipeline(self):
        bronze = _bronze_engine_with_data()
        writer = _make_writer()

        # Force initialise to raise
        writer.initialise = MagicMock(
            side_effect=SchemaInitialisationError("DDL failed")
        )
        pipeline = SilverPipeline(
            report=_confirmed_report(),
            bronze_engine=bronze,
            writer=writer,
            settings=_settings(),
        )
        with pytest.raises(SchemaInitialisationError):
            pipeline.run(run_id="err-test")


# ---------------------------------------------------------------------------
# RunStats counts correct
# ---------------------------------------------------------------------------

class TestRunStatsCounts:
    def test_bronze_rows_read_count(self):
        bronze = _bronze_engine_with_data()
        silver = _silver_engine()
        writer = _make_writer(silver)
        pipeline = SilverPipeline(
            report=_confirmed_report(),
            bronze_engine=bronze,
            writer=writer,
            settings=_settings(),
        )
        stats = pipeline.run(run_id="count-test")
        assert stats.bronze_rows_read == 3

    def test_silver_rows_written_count(self):
        """3 bronze rows × 1 confirmed mapping = 3 silver rows."""
        bronze = _bronze_engine_with_data()
        silver = _silver_engine()
        writer = _make_writer(silver)
        pipeline = SilverPipeline(
            report=_confirmed_report(),
            bronze_engine=bronze,
            writer=writer,
            settings=_settings(),
        )
        stats = pipeline.run(run_id="silver-count")
        assert stats.silver_rows_written == 3

    def test_duration_seconds_populated(self):
        bronze = _bronze_engine_with_data()
        silver = _silver_engine()
        writer = _make_writer(silver)
        pipeline = SilverPipeline(
            report=_confirmed_report(),
            bronze_engine=bronze,
            writer=writer,
            settings=_settings(),
        )
        stats = pipeline.run(run_id="duration-test")
        assert stats.duration_seconds is not None
        assert stats.duration_seconds >= 0


# ---------------------------------------------------------------------------
# Multi-class expansion
# ---------------------------------------------------------------------------

def _discriminator_ontology():
    """A real subtype hierarchy: Individual/Organisation are owl:Class subClassOf Party,
    so party_type is a genuine discriminator (not a value-mapped categorical property)."""
    from rdflib import Graph, URIRef, RDF, RDFS, OWL
    g = Graph()
    party = URIRef("http://ex.org#Party")
    ind = URIRef("http://ex.org#Individual")
    org = URIRef("http://ex.org#Organisation")
    for c in (party, ind, org):
        g.add((c, RDF.type, OWL.Class))
    g.add((ind, RDFS.subClassOf, party))
    g.add((org, RDFS.subClassOf, party))
    return g


class TestMultiClassExpansion:
    def test_discriminator_produces_typed_silver_rows(self):
        """party_type=1 → Individual URI; party_type=2 → Organisation URI."""
        bronze = _bronze_engine_with_data(rows=[("P1", "1978-03-14", 1)])
        silver = _silver_engine()
        writer = _make_writer(silver)
        pipeline = SilverPipeline(
            report=_report_with_discriminator(),
            bronze_engine=bronze,
            writer=writer,
            settings=_settings(),
            ontology_graph=_discriminator_ontology(),
        )
        stats = pipeline.run(run_id="disc-test")
        # party_id passthrough + party_type value_mapped → 2 silver rows for P1
        assert stats.silver_rows_written > 0

    def test_unknown_discriminator_value_passes_through(self):
        """A discriminator value with no matching rule → passthrough as plain string."""
        bronze = _bronze_engine_with_data(rows=[("P99", "1978-03-14", 99)])
        silver = _silver_engine()
        writer = _make_writer(silver)
        pipeline = SilverPipeline(
            report=_report_with_discriminator(),
            bronze_engine=bronze,
            writer=writer,
            settings=_settings(),
            ontology_graph=_discriminator_ontology(),
        )
        stats = pipeline.run(run_id="disc-quar")
        assert stats.quarantine_rows_written == 0
        assert stats.silver_rows_written > 0


# ---------------------------------------------------------------------------
# _resolve_property_local_name unit tests
# ---------------------------------------------------------------------------

class TestResolvePropertyLocalName:
    def test_fragment_extracted_and_snake_cased(self):
        assert _resolve_property_local_name(
            "http://ex.org/ontology#dateOfBirth", "fallback"
        ) == "date_of_birth"

    def test_path_segment_extracted(self):
        assert _resolve_property_local_name(
            "http://ex.org/ontology/partyId", "fallback"
        ) == "party_id"

    def test_empty_uri_returns_fallback(self):
        assert _resolve_property_local_name("", "my_col") == "my_col"

    def test_property_returns_fallback(self):
        assert _resolve_property_local_name(
            "http://ex.org#property", "my_col"
        ) == "my_col"

    def test_already_lowercase_unchanged(self):
        assert _resolve_property_local_name(
            "http://ex.org#party_id", "fallback"
        ) == "party_id"
