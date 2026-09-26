# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for MCP tool tests."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from semantic_mapper.config.settings import Settings
from semantic_mapper.gold.models import GoldColumnSpec, GoldTableDefinition
from semantic_mapper.schema.mapping_output import (
    MappingStatus,
    PropertyMapping,
    SemanticMappingReport,
    TableMapping,
    ValueMappingRule,
    ClassCandidate,
)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@pytest.fixture
def mcp_settings():
    return Settings(
        DATABASE_URL="sqlite:///:memory:",
        SILVER_DATABASE_URL="sqlite:///:memory:",
        GOLD_DATABASE_URL="sqlite:///:memory:",
        RDF_WRITER="rdflib",
        RDF_BASE_URI="http://mesh.local/data/",
        RDF_NAMED_GRAPH="http://mesh.local/graph/current",
        ONTOLOGY_PATH="",
        MCP_LEGACY_QUERY_TOOL=True,
    )


# ---------------------------------------------------------------------------
# GoldTableDefinitions: patient + admission (admission has object property FK)
# ---------------------------------------------------------------------------

ONT = "http://ex.org/ontology#"
DATA = "http://mesh.local/data/"

PATIENT_DEF = GoldTableDefinition(
    gold_table_name="gold_patient",
    primary_class_uri=f"{ONT}Patient",
    class_label="patient",
    source_silver_tables=["silver_raw_patients"],
    id_column="subject_id",
    column_specs=[
        GoldColumnSpec(
            silver_table="silver_raw_patients",
            source_column="subject_id",
            gold_column_name="subject_id",
            source_data_type="INT",
            property_uri=f"{ONT}hasSubjectId",
        ),
        GoldColumnSpec(
            silver_table="silver_raw_patients",
            source_column="gender",
            gold_column_name="gender",
            source_data_type="VARCHAR(5)",
            property_uri=f"{ONT}hasGender",
        ),
        GoldColumnSpec(
            silver_table="silver_raw_patients",
            source_column="dob",
            gold_column_name="date_of_birth",
            source_data_type="DATETIME",
            property_uri=f"{ONT}hasDateOfBirth",
        ),
    ],
)

ADMISSION_DEF = GoldTableDefinition(
    gold_table_name="gold_admission",
    primary_class_uri=f"{ONT}Admission",
    class_label="admission",
    source_silver_tables=["silver_raw_admissions"],
    id_column="hadm_id",
    column_specs=[
        GoldColumnSpec(
            silver_table="silver_raw_admissions",
            source_column="hadm_id",
            gold_column_name="hadm_id",
            source_data_type="INT",
            property_uri=f"{ONT}hasHadmId",
        ),
        GoldColumnSpec(
            silver_table="silver_raw_admissions",
            source_column="subject_id",
            gold_column_name="subject_id",
            source_data_type="INT",
            property_uri=f"{ONT}hasSubjectId",
        ),
        GoldColumnSpec(
            silver_table="silver_raw_admissions",
            source_column="admission_type",
            gold_column_name="admission_type",
            source_data_type="VARCHAR(50)",
            property_uri=f"{ONT}hasAdmissionType",
            # The gold builder derives value_map from the report's value_mappings; the
            # value-matching guidance is reconciled against it (materialised reality).
            value_map={
                "EMERGENCY": f"{ONT}EmergencyAdmission",
                "ELECTIVE": f"{ONT}ElectiveAdmission",
                "URGENT": f"{ONT}UrgentAdmission",
            },
        ),
        GoldColumnSpec(
            silver_table="silver_raw_admissions",
            source_column="admittime",
            gold_column_name="admittime",
            source_data_type="DATETIME",
            property_uri=f"{ONT}hasAdmittime",
        ),
        # Object property: admission → patient (column name matches entity short name)
        GoldColumnSpec(
            silver_table="silver_raw_admissions",
            source_column="patient_fk",
            gold_column_name="patient",
            source_data_type="INT",
            property_uri=f"{ONT}hasAdmissionPatient",
        ),
    ],
)


@pytest.fixture
def definitions():
    return [PATIENT_DEF, ADMISSION_DEF]


# ---------------------------------------------------------------------------
# Mapping report with value_mappings for categorical guidance
# ---------------------------------------------------------------------------

@pytest.fixture
def mapping_report():
    return SemanticMappingReport(
        ontology_uri=f"{ONT}",
        database_name="test_db",
        settings_used={},
        total_pending_review=0,
        tables=[
            TableMapping(
                source_table="raw_admissions",
                class_candidate_set=[
                    ClassCandidate(
                        uri=f"{ONT}Admission",
                        label="Admission",
                        rationale="test",
                        confidence_score=0.9,
                    )
                ],
                column_mappings=[
                    PropertyMapping(
                        source_column="admission_type",
                        source_data_type="VARCHAR(50)",
                        is_nullable=False,
                        mapped_property_uri=f"{ONT}hasAdmissionType",
                        status=MappingStatus.CONFIRMED,
                        confidence_score=0.95,
                        rationale="test",
                        value_mappings=[
                            ValueMappingRule(
                                source_value="EMERGENCY",
                                mapped_uri=f"{ONT}EmergencyAdmission",
                                rationale="test",
                                confidence_score=0.9,
                            ),
                            ValueMappingRule(
                                source_value="ELECTIVE",
                                mapped_uri=f"{ONT}ElectiveAdmission",
                                rationale="test",
                                confidence_score=0.9,
                            ),
                            ValueMappingRule(
                                source_value="URGENT",
                                mapped_uri=f"{ONT}UrgentAdmission",
                                rationale="test",
                                confidence_score=0.9,
                            ),
                        ],
                    ),
                    PropertyMapping(
                        source_column="hadm_id",
                        source_data_type="INT",
                        is_nullable=False,
                        mapped_property_uri=f"{ONT}hasHadmId",
                        status=MappingStatus.CONFIRMED,
                        confidence_score=0.99,
                        rationale="test",
                    ),
                ],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# SQLite gold and silver engines
# ---------------------------------------------------------------------------

@pytest.fixture
def gold_sqlite_engine():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE gold_patient (
                subject_id TEXT,
                gender TEXT,
                date_of_birth TEXT
            )
        """))
        conn.execute(text("""
            INSERT INTO gold_patient (subject_id, gender, date_of_birth)
            VALUES ('P1', 'M', '1980-01-01'), ('P2', 'F', '1990-06-15')
        """))
        conn.execute(text("""
            CREATE TABLE gold_admission (
                hadm_id TEXT,
                subject_id TEXT,
                admission_type TEXT,
                admittime TEXT
            )
        """))
        conn.execute(text("""
            INSERT INTO gold_admission (hadm_id, subject_id, admission_type, admittime)
            VALUES
                ('A1', 'P1', 'EMERGENCY', '2150-01-01'),
                ('A2', 'P2', 'ELECTIVE',  '2150-02-15'),
                ('A3', 'P1', 'EMERGENCY', '2150-03-20')
        """))
    return engine


@pytest.fixture
def silver_sqlite_engine():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE silver_raw_admissions (
                _ontology_class_uri TEXT,
                hadm_id TEXT,
                subject_id TEXT,
                admission_type TEXT
            )
        """))
        conn.execute(text("""
            INSERT INTO silver_raw_admissions VALUES
                ('http://ex.org/ontology#EmergencyAdmission', 'A1', 'P1', 'EMERGENCY'),
                ('http://ex.org/ontology#ElectiveAdmission',  'A2', 'P2', 'ELECTIVE')
        """))
    return engine
