# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the query harness.

Provides:
  - gold_engine   : SQLite in-memory gold DB (gold_patient + gold_encounter)
  - rdf_dataset   : rdflib Dataset with matching triples in a named graph
  - harness_defs  : GoldTableDefinitions matching the test schema
  - harness_report: minimal SemanticMappingReport (required by SemanticQueryEngine)
  - harness_settings: Settings wired for SQLite / local Fuseki
"""
from __future__ import annotations

import pytest
from rdflib import RDF, Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import XSD
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
# Ontology constants shared by SQL and SPARQL harnesses
# ---------------------------------------------------------------------------

NS = Namespace("http://ex.org/health#")
BASE = "http://mesh.local/data/"
NAMED_GRAPH = "http://mesh.local/graph/current"

PATIENT_CLASS   = str(NS.Patient)
ENCOUNTER_CLASS = str(NS.Encounter)

PROP_PATIENT_ID  = str(NS.patientId)
PROP_FIRST_NAME  = str(NS.firstName)
PROP_DOB         = str(NS.dateOfBirth)
PROP_GENDER      = str(NS.gender)

# patientHasEncounter: domain=Patient, range=Encounter
# TripleBuilder FLIPS direction → triple stored as <patient> patientHasEncounter <encounter>
PROP_PAT_HAS_ENC = str(NS.patientHasEncounter)

PROP_ENC_ID      = str(NS.encounterId)
PROP_ENC_DATE    = str(NS.encounterDate)
PROP_TOTAL_COST  = str(NS.totalCost)
PROP_ADMIT_TIME  = str(NS.admitTime)

# Test rows ---------------------------------------------------------------
PATIENTS = [
    {"id": "P001", "first_name": "Alice", "date_of_birth": "1990-01-15", "gender": "Female"},
    {"id": "P002", "first_name": "Bob",   "date_of_birth": "1975-06-20", "gender": "Male"},
    {"id": "P003", "first_name": "Carol", "date_of_birth": "1985-03-08", "gender": "Female"},
]
# admit_time uses MySQL space format ("YYYY-MM-DD HH:MM:SS") — as returned by a
# DATETIME column SELECT.  The RDF fixture normalises these to T-separated ISO 8601
# before storing as xsd:dateTime literals, mirroring what _normalise_literal does.
ENCOUNTERS = [
    {"id": "E001", "patient": "P001", "encounter_date": "2023-01-10", "total_cost": 150.00,
     "admit_time": "2023-01-10 09:00:00"},
    {"id": "E002", "patient": "P001", "encounter_date": "2023-06-15", "total_cost": 200.00,
     "admit_time": "2023-06-15 14:30:00"},
    {"id": "E003", "patient": "P002", "encounter_date": "2023-09-20", "total_cost": 75.00,
     "admit_time": "2023-09-20 08:15:00"},
]


# ---------------------------------------------------------------------------
# Gold SQLite fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def gold_engine():
    """In-memory SQLite gold DB with gold_patient and gold_encounter tables."""
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE TABLE gold_patient (
                id TEXT,
                first_name TEXT,
                date_of_birth TEXT,
                gender TEXT,
                _ontology_class_uri TEXT,
                _bronze_source_ref TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE gold_encounter (
                id TEXT,
                patient TEXT,
                encounter_date TEXT,
                total_cost REAL,
                admit_time TEXT,
                _ontology_class_uri TEXT,
                _bronze_source_ref TEXT
            )
        """))
        for p in PATIENTS:
            conn.execute(text(
                "INSERT INTO gold_patient VALUES (:id, :first_name, :date_of_birth, :gender,"
                " :class_uri, :ref)"
            ), {
                **p,
                "class_uri": PATIENT_CLASS,
                "ref": f"raw_patient.id={p['id']}",
            })
        for e in ENCOUNTERS:
            conn.execute(text(
                "INSERT INTO gold_encounter VALUES (:id, :patient, :encounter_date,"
                " :total_cost, :admit_time, :class_uri, :ref)"
            ), {
                **e,
                "class_uri": ENCOUNTER_CLASS,
                "ref": f"raw_encounter.id={e['id']}",
            })
        conn.commit()
    return engine


# ---------------------------------------------------------------------------
# GoldTableDefinitions
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def harness_defs():
    """GoldTableDefinitions matching the SQLite harness schema."""
    patient_def = GoldTableDefinition(
        gold_table_name="gold_patient",
        primary_class_uri=PATIENT_CLASS,
        class_label="patient",
        source_silver_tables=["silver_raw_patient"],
        column_specs=[
            GoldColumnSpec(silver_table="silver_raw_patient", source_column="id",
                           gold_column_name="id", source_data_type="VARCHAR(50)",
                           property_uri=PROP_PATIENT_ID),
            GoldColumnSpec(silver_table="silver_raw_patient", source_column="first_name",
                           gold_column_name="first_name", source_data_type="VARCHAR(100)",
                           property_uri=PROP_FIRST_NAME),
            GoldColumnSpec(silver_table="silver_raw_patient", source_column="date_of_birth",
                           gold_column_name="date_of_birth", source_data_type="DATE",
                           property_uri=PROP_DOB),
            GoldColumnSpec(silver_table="silver_raw_patient", source_column="gender",
                           gold_column_name="gender", source_data_type="VARCHAR(20)",
                           property_uri=PROP_GENDER),
        ],
    )
    encounter_def = GoldTableDefinition(
        gold_table_name="gold_encounter",
        primary_class_uri=ENCOUNTER_CLASS,
        class_label="encounter",
        source_silver_tables=["silver_raw_encounter"],
        column_specs=[
            GoldColumnSpec(silver_table="silver_raw_encounter", source_column="id",
                           gold_column_name="id", source_data_type="VARCHAR(50)",
                           property_uri=PROP_ENC_ID),
            # FK column: patient → gold_patient (object property, direction-flipped)
            GoldColumnSpec(silver_table="silver_raw_encounter", source_column="patient",
                           gold_column_name="patient", source_data_type="VARCHAR(50)",
                           property_uri=PROP_PAT_HAS_ENC),
            GoldColumnSpec(silver_table="silver_raw_encounter", source_column="encounter_date",
                           gold_column_name="encounter_date", source_data_type="DATE",
                           property_uri=PROP_ENC_DATE),
            GoldColumnSpec(silver_table="silver_raw_encounter", source_column="total_cost",
                           gold_column_name="total_cost", source_data_type="DECIMAL(10,2)",
                           property_uri=PROP_TOTAL_COST),
            GoldColumnSpec(silver_table="silver_raw_encounter", source_column="admit_time",
                           gold_column_name="admit_time", source_data_type="DATETIME",
                           property_uri=PROP_ADMIT_TIME),
        ],
    )
    return [patient_def, encounter_def]


# ---------------------------------------------------------------------------
# SemanticMappingReport (minimal — only used by SemanticQueryEngine constructor)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def harness_report():
    return SemanticMappingReport(
        ontology_uri="http://ex.org/health",
        database_name="bronze_db",
        settings_used={},
        tables=[
            TableMapping(
                source_table="raw_patient",
                class_candidate_set=[
                    ClassCandidate(uri=PATIENT_CLASS, label="Patient",
                                   rationale="test", confidence_score=0.99)
                ],
                column_mappings=[
                    PropertyMapping(source_column="id", source_data_type="VARCHAR(50)",
                                    is_nullable=False, mapped_property_uri=PROP_PATIENT_ID,
                                    status=MappingStatus.CONFIRMED,
                                    confidence_score=0.99, rationale="test"),
                    PropertyMapping(source_column="first_name", source_data_type="VARCHAR(100)",
                                    is_nullable=True, mapped_property_uri=PROP_FIRST_NAME,
                                    status=MappingStatus.CONFIRMED,
                                    confidence_score=0.99, rationale="test"),
                    PropertyMapping(source_column="date_of_birth", source_data_type="DATE",
                                    is_nullable=True, mapped_property_uri=PROP_DOB,
                                    status=MappingStatus.CONFIRMED,
                                    confidence_score=0.99, rationale="test"),
                    PropertyMapping(source_column="gender", source_data_type="VARCHAR(20)",
                                    is_nullable=True, mapped_property_uri=PROP_GENDER,
                                    status=MappingStatus.CONFIRMED,
                                    confidence_score=0.99, rationale="test"),
                ],
            ),
            TableMapping(
                source_table="raw_encounter",
                class_candidate_set=[
                    ClassCandidate(uri=ENCOUNTER_CLASS, label="Encounter",
                                   rationale="test", confidence_score=0.99)
                ],
                column_mappings=[
                    PropertyMapping(source_column="id", source_data_type="VARCHAR(50)",
                                    is_nullable=False, mapped_property_uri=PROP_ENC_ID,
                                    status=MappingStatus.CONFIRMED,
                                    confidence_score=0.99, rationale="test"),
                    PropertyMapping(source_column="patient", source_data_type="VARCHAR(50)",
                                    is_nullable=True, mapped_property_uri=PROP_PAT_HAS_ENC,
                                    status=MappingStatus.CONFIRMED,
                                    confidence_score=0.99, rationale="test"),
                    PropertyMapping(source_column="encounter_date", source_data_type="DATE",
                                    is_nullable=True, mapped_property_uri=PROP_ENC_DATE,
                                    status=MappingStatus.CONFIRMED,
                                    confidence_score=0.99, rationale="test"),
                    PropertyMapping(source_column="total_cost", source_data_type="DECIMAL(10,2)",
                                    is_nullable=True, mapped_property_uri=PROP_TOTAL_COST,
                                    status=MappingStatus.CONFIRMED,
                                    confidence_score=0.99, rationale="test"),
                    PropertyMapping(source_column="admit_time", source_data_type="DATETIME",
                                    is_nullable=True, mapped_property_uri=PROP_ADMIT_TIME,
                                    status=MappingStatus.CONFIRMED,
                                    confidence_score=0.99, rationale="test"),
                ],
            ),
        ],
        total_pending_review=0,
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def harness_settings():
    return Settings(
        DATABASE_URL="sqlite:///:memory:",
        SILVER_DATABASE_URL="sqlite:///:memory:",
        GOLD_DATABASE_URL="sqlite:///:memory:",
        FUSEKI_URL="http://localhost:3030",
        FUSEKI_ADMIN_USER="admin",
        FUSEKI_ADMIN_PASSWORD="admin",
        RDF_DATASET_NAME="test-mesh",
        RDF_NAMED_GRAPH=NAMED_GRAPH,
        RDF_BASE_URI=BASE,
        QUERY_MAX_ROWS=100,
        QUERY_MODEL="gpt-4o-mini",
        OPENAI_API_KEY="test-key",
        ONTOLOGY_PATH="",
    )


# ---------------------------------------------------------------------------
# RDF Dataset (rdflib) with matching triples
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def rdf_dataset():
    """rdflib Dataset containing test triples in NAMED_GRAPH.

    Mirrors the gold SQL data so SPARQL and SQL harness tests use the same
    logical dataset.

    patientHasEncounter triples are stored in the FLIPPED direction:
        <patient/Pxxx> patientHasEncounter <encounter/Exxx>
    because the property's rdfs:range is Encounter (the current entity),
    so TripleBuilder flips subject/object — the patient URI is subject.
    """
    ds = Dataset()
    g: Graph = ds.graph(URIRef(NAMED_GRAPH))

    pat_ns  = Namespace(f"{BASE}patient/")
    enc_ns  = Namespace(f"{BASE}encounter/")

    for p in PATIENTS:
        subj = pat_ns[p["id"]]
        g.add((subj, RDF.type,        URIRef(PATIENT_CLASS)))
        g.add((subj, NS.patientId,    Literal(p["id"])))
        g.add((subj, NS.firstName,    Literal(p["first_name"])))
        g.add((subj, NS.dateOfBirth,  Literal(p["date_of_birth"], datatype=XSD.date)))
        g.add((subj, NS.gender,       Literal(p["gender"])))

    for e in ENCOUNTERS:
        enc_subj = enc_ns[e["id"]]
        pat_subj = pat_ns[e["patient"]]
        g.add((enc_subj, RDF.type,        URIRef(ENCOUNTER_CLASS)))
        g.add((enc_subj, NS.encounterId,  Literal(e["id"])))
        g.add((enc_subj, NS.encounterDate,Literal(e["encounter_date"], datatype=XSD.date)))
        g.add((enc_subj, NS.totalCost,    Literal(str(e["total_cost"]), datatype=XSD.decimal)))
        # admit_time: MySQL space format normalised to T-separated ISO 8601 for xsd:dateTime
        g.add((enc_subj, NS.admitTime,
               Literal(e["admit_time"].replace(" ", "T"), datatype=XSD.dateTime)))
        # Flipped: patient is subject of patientHasEncounter
        g.add((pat_subj, NS.patientHasEncounter, enc_subj))

    return ds


# ---------------------------------------------------------------------------
# SPARQL executor against rdflib Dataset
# ---------------------------------------------------------------------------

def sparql_against_dataset(ds: Dataset, sparql: str) -> list[dict]:
    """Execute a SPARQL SELECT against a rdflib Dataset and return rows as dicts.

    Used by the SPARQL harness instead of Fuseki so tests run fully offline.
    Values are returned as their string representation (URI or literal value).
    """
    from rdflib.term import Variable

    result = ds.query(sparql)
    vars_ = list(result.vars)  # list of Variable objects
    rows = []
    for row in result:
        r: dict = {}
        for var in vars_:
            # Use Variable key access to avoid collisions with Python builtins
            # (e.g. ResultRow.count is a built-in method, not the SPARQL ?count)
            val = row[var]
            r[str(var)] = str(val) if val is not None else None
        rows.append(r)
    return rows
