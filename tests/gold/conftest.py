# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from semantic_mapper.config.settings import Settings
from semantic_mapper.gold.models import GoldColumnSpec, GoldTableDefinition
from semantic_mapper.schema.mapping_output import (
    ClassCandidate,
    MappingStatus,
    PropertyMapping,
    SemanticMappingReport,
    TableMapping,
)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@pytest.fixture
def gold_settings():
    return Settings(
        DATABASE_URL="sqlite:///:memory:",
        SILVER_DATABASE_URL="sqlite:///:memory:",
        GOLD_DATABASE_URL="sqlite:///:memory:",
        GOLD_WRITER="mysql",
        GOLD_BATCH_SIZE=5,
    )


# ---------------------------------------------------------------------------
# SQLite engines
# ---------------------------------------------------------------------------

@pytest.fixture
def silver_engine():
    """In-memory SQLite engine with a pre-populated silver_raw_party table."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE silver_raw_party (
                _silver_id                INTEGER PRIMARY KEY AUTOINCREMENT,
                _bronze_source_ref        TEXT NOT NULL,
                _run_id                   TEXT NOT NULL,
                _mapping_confidence_score REAL NOT NULL,
                _ontology_class_uri       TEXT NOT NULL,
                party_id                  TEXT,
                dob                       TEXT
            )
        """))
        conn.execute(text("""
            INSERT INTO silver_raw_party
                (_bronze_source_ref, _run_id, _mapping_confidence_score,
                 _ontology_class_uri, party_id, dob)
            VALUES
                ('raw_party.party_id=P1', 'silver-run-001', 0.95,
                 'http://ex.org/ontology#Party', 'P1', '1978-03-14'),
                ('raw_party.party_id=P2', 'silver-run-001', 0.95,
                 'http://ex.org/ontology#Party', 'P2', '1982-03-14')
        """))
    return engine


@pytest.fixture
def gold_engine():
    """In-memory SQLite engine for the gold target."""
    return create_engine("sqlite:///:memory:")


# ---------------------------------------------------------------------------
# Gold models
# ---------------------------------------------------------------------------

@pytest.fixture
def simple_definition():
    return GoldTableDefinition(
        gold_table_name="gold_party",
        primary_class_uri="http://ex.org/ontology#Party",
        class_label="party",
        source_silver_tables=["silver_raw_party"],
        column_specs=[
            GoldColumnSpec(
                silver_table="silver_raw_party",
                source_column="party_id",
                gold_column_name="party_id",
                source_data_type="VARCHAR(50)",
                property_uri="http://ex.org/ontology#partyId",
            ),
            GoldColumnSpec(
                silver_table="silver_raw_party",
                source_column="dob",
                gold_column_name="dob",
                source_data_type="VARCHAR(20)",
                property_uri="http://ex.org/ontology#dateOfBirth",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Mapping report (for builder tests)
# ---------------------------------------------------------------------------

@pytest.fixture
def simple_report():
    return SemanticMappingReport(
        ontology_uri="http://ex.org/ontology",
        database_name="bronze_db",
        settings_used={},
        tables=[
            TableMapping(
                source_table="raw_party",
                class_candidate_set=[
                    ClassCandidate(
                        uri="http://ex.org/ontology#Party",
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
                        mapped_property_uri="http://ex.org/ontology#partyId",
                        status=MappingStatus.CONFIRMED,
                        confidence_score=0.99,
                        rationale="test",
                    ),
                    PropertyMapping(
                        source_column="dob",
                        source_data_type="VARCHAR(20)",
                        is_nullable=True,
                        mapped_property_uri="http://ex.org/ontology#dateOfBirth",
                        status=MappingStatus.CONFIRMED,
                        confidence_score=0.95,
                        rationale="test",
                    ),
                ],
            )
        ],
        total_pending_review=0,
    )
