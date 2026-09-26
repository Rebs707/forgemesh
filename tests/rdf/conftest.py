# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from semantic_mapper.config.settings import Settings
from semantic_mapper.gold.models import GoldColumnSpec, GoldTableDefinition


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@pytest.fixture
def rdf_settings():
    return Settings(
        DATABASE_URL="sqlite:///:memory:",
        SILVER_DATABASE_URL="sqlite:///:memory:",
        GOLD_DATABASE_URL="sqlite:///:memory:",
        RDF_WRITER="rdflib",
        RDF_BATCH_SIZE=5,
        RDF_BASE_URI="http://mesh.local/data/",
        RDF_NAMED_GRAPH="http://mesh.local/graph/current",
        ONTOLOGY_PATH="",  # skip ontology loading in tests
    )


# ---------------------------------------------------------------------------
# SQLite gold engine with pre-populated tables
# ---------------------------------------------------------------------------

@pytest.fixture
def gold_engine():
    """In-memory SQLite engine with gold_patient and gold_encounter tables."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE gold_patient (
                _gold_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                _ontology_class_uri       TEXT NOT NULL,
                _bronze_source_ref        TEXT NOT NULL,
                _silver_run_id            TEXT NOT NULL,
                _gold_run_id              TEXT NOT NULL,
                _mapping_confidence_score REAL NOT NULL,
                id                        TEXT,
                date_of_birth             TEXT
            )
        """))
        conn.execute(text("""
            INSERT INTO gold_patient
                (_ontology_class_uri, _bronze_source_ref, _silver_run_id, _gold_run_id,
                 _mapping_confidence_score, id, date_of_birth)
            VALUES
                ('http://ex.org/ontology#Patient', 'raw_patient.id=P1',
                 'silver-run-001', 'gold-run-001', 0.95, 'P1', '1978-03-14'),
                ('http://ex.org/ontology#Patient', 'raw_patient.id=P2',
                 'silver-run-001', 'gold-run-001', 0.92, 'P2', '1985-07-22')
        """))

        conn.execute(text("""
            CREATE TABLE gold_encounter (
                _gold_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                _ontology_class_uri       TEXT NOT NULL,
                _bronze_source_ref        TEXT NOT NULL,
                _silver_run_id            TEXT NOT NULL,
                _gold_run_id              TEXT NOT NULL,
                _mapping_confidence_score REAL NOT NULL,
                patient                   TEXT,
                encounter_date            TEXT
            )
        """))
        conn.execute(text("""
            INSERT INTO gold_encounter
                (_ontology_class_uri, _bronze_source_ref, _silver_run_id, _gold_run_id,
                 _mapping_confidence_score, patient, encounter_date)
            VALUES
                ('http://ex.org/ontology#Encounter', 'raw_encounter.id=E1',
                 'silver-run-001', 'gold-run-001', 0.90, 'P1', '2023-01-15')
        """))
    return engine


# ---------------------------------------------------------------------------
# GoldTableDefinitions with FK column
# ---------------------------------------------------------------------------

@pytest.fixture
def simple_definitions():
    return [
        GoldTableDefinition(
            gold_table_name="gold_patient",
            primary_class_uri="http://ex.org/ontology#Patient",
            class_label="patient",
            source_silver_tables=["silver_raw_patient"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_patient",
                    source_column="id",
                    gold_column_name="id",
                    source_data_type="VARCHAR(50)",
                    property_uri="http://ex.org/ontology#patientId",
                ),
                GoldColumnSpec(
                    silver_table="silver_raw_patient",
                    source_column="date_of_birth",
                    gold_column_name="date_of_birth",
                    source_data_type="DATE",
                    property_uri="http://ex.org/ontology#dateOfBirth",
                ),
            ],
        ),
        GoldTableDefinition(
            gold_table_name="gold_encounter",
            primary_class_uri="http://ex.org/ontology#Encounter",
            class_label="encounter",
            source_silver_tables=["silver_raw_encounter"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_raw_encounter",
                    source_column="patient",
                    gold_column_name="patient",
                    source_data_type="VARCHAR(50)",
                    property_uri="http://ex.org/ontology#hasPatient",
                ),
                GoldColumnSpec(
                    silver_table="silver_raw_encounter",
                    source_column="encounter_date",
                    gold_column_name="encounter_date",
                    source_data_type="DATE",
                    property_uri="http://ex.org/ontology#encounterDate",
                ),
            ],
        ),
    ]
