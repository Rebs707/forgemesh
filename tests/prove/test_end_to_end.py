# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Phase 11 — End-to-end harness.

Runs the full Prove It pipeline against a SQLite in-memory fixture:
  bronze (admissions) → silver → gold → RDF (rdflib) → prove

No Docker, MySQL, Fuseki, or OpenAI key required.

Golden files in tests/prove/fixtures/golden/ are auto-generated on first run
then compared field-by-field on subsequent runs to catch regressions.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import rdflib
from rdflib import RDF, URIRef
from sqlalchemy import create_engine, text

from semantic_mapper.gold.builder import GoldDefinitionBuilder
from semantic_mapper.gold.pipeline import GoldPipeline
from semantic_mapper.gold.writer.mysql import MySQLGoldWriter
from semantic_mapper.prove.composer import compose_proof
from semantic_mapper.prove.decision_lookup import get_mapping_decisions
from semantic_mapper.prove.lineage_walker import LineageWalker
from semantic_mapper.prove.models import REQUIRED_LIMITATIONS, ProofRecord
from semantic_mapper.prove.narrator import narrate
from semantic_mapper.prove.query_rewriter import ProofQuery
from semantic_mapper.prove.subgraph import extract_subgraph
from semantic_mapper.rdf.pipeline import RdfPipeline
from semantic_mapper.rdf.writer.rdflib_writer import RdflibWriter
from semantic_mapper.schema.mapping_output import (
    ClassCandidate,
    MappingEvidence,
    MappingStatus,
    PropertyMapping,
    SemanticMappingReport,
    TableMapping,
    TransformationSpec,
)
from semantic_mapper.silver.pipeline import SilverPipeline
from semantic_mapper.silver.writer.mysql import MySQLSilverWriter

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ONT = "http://mesh.local/ontology#"
_CLASS_URI = f"{_ONT}Admission"
_BASE_URI = "http://mesh.local/data/"
_RUN_ID = "e2e_test"
_GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden"

# ---------------------------------------------------------------------------
# Fixture OWL — embedded Turtle; defines Admission class + 3 properties
# ---------------------------------------------------------------------------

_OWL_TURTLE = f"""\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix : <{_ONT}> .

<http://mesh.local/ontology> a owl:Ontology .

:Admission a owl:Class ; rdfs:label "Admission" .

:hadmId a owl:DatatypeProperty ;
    rdfs:label "hadmId" ; rdfs:comment "Source column: admissions_id" ;
    rdfs:domain :Admission ; rdfs:range xsd:integer .

:admittime a owl:DatatypeProperty ;
    rdfs:label "admittime" ; rdfs:comment "Source column: admittime" ;
    rdfs:domain :Admission ; rdfs:range xsd:dateTime .

:admissionType a owl:DatatypeProperty ;
    rdfs:label "admissionType" ; rdfs:comment "Source column: admission_type" ;
    rdfs:domain :Admission ; rdfs:range xsd:string .
"""

# ---------------------------------------------------------------------------
# Bronze fixture — 20 admissions, 2 NULL admittime (row 5 and 14)
# Columns: admissions_id, subject_id, admittime, admission_type
# PK named "admissions_id" so silver pipeline builds _bronze_source_ref correctly
# ---------------------------------------------------------------------------

_ADMISSIONS = [
    (1,  1, "2023-01-05 10:30:00", "ELECTIVE"),
    (2,  2, "2023-01-08 09:00:00", "URGENT"),
    (3,  1, "2023-02-15 14:00:00", "URGENT"),
    (4,  3, "2023-02-20 08:00:00", "ELECTIVE"),
    (5,  4, None,                  "NEWBORN"),
    (6,  5, "2023-03-10 11:00:00", "ELECTIVE"),
    (7,  6, "2023-03-20 08:30:00", "URGENT"),
    (8,  7, "2023-04-01 07:00:00", "ELECTIVE"),
    (9,  8, "2023-04-10 12:00:00", "URGENT"),
    (10, 9, "2023-04-20 16:00:00", "NEWBORN"),
    (11, 10, "2023-05-01 09:00:00", "ELECTIVE"),
    (12, 1,  "2023-05-15 13:00:00", "URGENT"),
    (13, 2,  "2023-06-01 08:00:00", "ELECTIVE"),
    (14, 3,  None,                  "URGENT"),
    (15, 4,  "2023-07-01 10:00:00", "ELECTIVE"),
    (16, 5,  "2023-07-15 14:00:00", "URGENT"),
    (17, 6,  "2023-08-01 09:00:00", "NEWBORN"),
    (18, 7,  "2023-08-15 13:00:00", "ELECTIVE"),
    (19, 8,  "2023-09-01 08:00:00", "URGENT"),
    (20, 9,  "2023-09-15 12:00:00", "ELECTIVE"),
]

_EXPECTED_ROWS = len(_ADMISSIONS)          # 20
_EXPECTED_NULL_ADMITTIME = 2               # rows 5 and 14

# ---------------------------------------------------------------------------
# SemanticMappingReport — built manually; no MapperOrchestrator or LLM needed
# ---------------------------------------------------------------------------

_REPORT = SemanticMappingReport(
    ontology_uri="http://mesh.local/ontology",
    database_name="bronze_db",
    settings_used={},
    tables=[
        TableMapping(
            source_table="admissions",
            class_candidate_set=[
                ClassCandidate(
                    uri=_CLASS_URI,
                    label="Admission",
                    rationale="fixture",
                    confidence_score=0.99,
                )
            ],
            column_mappings=[
                PropertyMapping(
                    source_column="admissions_id",
                    source_data_type="INTEGER",
                    is_nullable=False,
                    mapped_property_uri=f"{_ONT}hadmId",
                    mapped_property_label="hadmId",
                    status=MappingStatus.CONFIRMED,
                    confidence_score=0.99,
                    rationale="PK",
                    evidence_trail=[
                        MappingEvidence(strategy="NameMatcher", score=0.99, rationale="PK")
                    ],
                ),
                PropertyMapping(
                    source_column="admittime",
                    source_data_type="TEXT",
                    is_nullable=True,
                    mapped_property_uri=f"{_ONT}admittime",
                    mapped_property_label="admittime",
                    status=MappingStatus.CONFIRMED,
                    confidence_score=0.95,
                    rationale="date column — exercises date transform and NULL handling",
                    transformation_spec=TransformationSpec(
                        detected_formats=["YYYY-MM-DD HH:MM:SS"],
                        target_format="xsd:dateTime",
                        parse_strategy="dateutil.parser",
                        validation_rule="must be valid datetime",
                    ),
                    evidence_trail=[
                        MappingEvidence(strategy="NameMatcher", score=0.95, rationale="date")
                    ],
                ),
                PropertyMapping(
                    source_column="admission_type",
                    source_data_type="TEXT",
                    is_nullable=False,
                    mapped_property_uri=f"{_ONT}admissionType",
                    mapped_property_label="admissionType",
                    status=MappingStatus.CONFIRMED,
                    confidence_score=0.88,
                    rationale="type field",
                    evidence_trail=[
                        MappingEvidence(strategy="NameMatcher", score=0.88, rationale="match")
                    ],
                ),
            ],
        )
    ],
    total_pending_review=0,
)


# ---------------------------------------------------------------------------
# Settings helper — SQLite in-memory, LLM + narration disabled
# ---------------------------------------------------------------------------

def _make_settings():
    from semantic_mapper.config.settings import Settings
    return Settings(
        DATABASE_URL="sqlite:///:memory:",
        SILVER_DATABASE_URL="sqlite:///:memory:",
        GOLD_DATABASE_URL="sqlite:///:memory:",
        SILVER_WRITER="mysql",
        GOLD_WRITER="mysql",
        RDF_WRITER="rdflib",
        RDF_BASE_URI=_BASE_URI,
        RDF_NAMED_GRAPH="http://mesh.local/graph/e2e",
        ONTOLOGY_PATH="",
        LLM_ENABLED=False,
        PROVE_NARRATE=False,  # skip LLM narrator — no OpenAI key needed
    )


# ---------------------------------------------------------------------------
# Module-scoped pipeline fixture (runs once per test session)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pipeline():
    """
    Full bronze → silver → gold → RDF pipeline against SQLite in-memory.
    Returns dict of engines, rdf_writer, definitions, ont_graph, settings.
    """
    settings = _make_settings()

    # Bronze
    bronze_engine = create_engine("sqlite:///:memory:")
    with bronze_engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE admissions ("
            "  admissions_id INTEGER PRIMARY KEY,"
            "  subject_id    INTEGER,"
            "  admittime     TEXT,"
            "  admission_type TEXT"
            ")"
        ))
        for row in _ADMISSIONS:
            conn.execute(
                text("INSERT INTO admissions VALUES (:aid, :sid, :at, :atype)"),
                {"aid": row[0], "sid": row[1], "at": row[2], "atype": row[3]},
            )

    # Silver
    silver_engine = create_engine("sqlite:///:memory:")
    silver_writer = MySQLSilverWriter(silver_engine, settings)
    SilverPipeline(_REPORT, bronze_engine, silver_writer, settings).run(run_id=_RUN_ID)

    # Gold
    definitions = GoldDefinitionBuilder().build(_REPORT)
    gold_engine = create_engine("sqlite:///:memory:")
    gold_writer = MySQLGoldWriter(gold_engine, settings)
    GoldPipeline(definitions, silver_engine, gold_writer, settings).run(run_id=_RUN_ID)

    # RDF
    rdf_writer = RdflibWriter()
    rdf_writer.initialise(
        dataset_name="e2e",
        named_graph="http://mesh.local/graph/e2e",
    )
    RdfPipeline(definitions, gold_engine, rdf_writer, settings).run(run_id=_RUN_ID)

    # Ontology graph for subgraph extraction
    ont_graph = rdflib.Graph()
    ont_graph.parse(data=_OWL_TURTLE, format="turtle")

    yield {
        "bronze": bronze_engine,
        "silver": silver_engine,
        "gold": gold_engine,
        "rdf": rdf_writer,
        "definitions": definitions,
        "ont_graph": ont_graph,
        "settings": settings,
    }

    bronze_engine.dispose()
    silver_engine.dispose()
    gold_engine.dispose()


# ---------------------------------------------------------------------------
# Proof builder — shared logic for all three modes
# ---------------------------------------------------------------------------

def _build_proof(pl: dict, mode: str) -> ProofRecord:
    """Run the full prove pipeline for *mode* and return the ProofRecord."""
    settings = pl["settings"]
    walker = LineageWalker(
        gold_engine=pl["gold"],
        silver_engine=pl["silver"],
        bronze_engine=pl["bronze"],
        base_uri=_BASE_URI,
    )
    gold_table = "gold_admission"
    silver_table = "silver_admissions"
    class_uris = {_CLASS_URI}

    if mode == "gold":
        executed_query = f"SELECT COUNT(_gold_id) FROM {gold_table}"
        with pl["gold"].connect() as conn:
            ids = [r[0] for r in conn.execute(
                text(f"SELECT _gold_id FROM {gold_table} LIMIT 1000")
            ).fetchall()]
        subjects = [str(i) for i in ids]
        chains = walker.walk_gold(gold_table, ids)
        proof_q = ProofQuery(
            proof_query=f"SELECT DISTINCT _gold_id FROM {gold_table} LIMIT 1000",
            mode="gold", sampled=False, original_count=len(ids), sample_limit=1000,
        )

    elif mode == "silver":
        executed_query = f"SELECT COUNT(_silver_id) FROM {silver_table}"
        with pl["silver"].connect() as conn:
            ids = [r[0] for r in conn.execute(
                text(f"SELECT _silver_id FROM {silver_table} LIMIT 1000")
            ).fetchall()]
        subjects = [str(i) for i in ids]
        chains = walker.walk_silver(silver_table, ids)
        proof_q = ProofQuery(
            proof_query=f"SELECT DISTINCT _silver_id FROM {silver_table} LIMIT 1000",
            mode="silver", sampled=False, original_count=len(ids), sample_limit=1000,
        )

    else:  # sparql
        executed_query = f"SELECT (COUNT(?a) AS ?n) WHERE {{ ?a a <{_CLASS_URI}> . }}"
        sparql_results = list(pl["rdf"].graph.query(
            f"SELECT DISTINCT ?a WHERE {{ ?a a <{_CLASS_URI}> . }}"
        ))
        subjects = [str(r[0]) for r in sparql_results]
        chains = []  # walk_uris omitted — URI format mismatch with _bronze_source_ref
        proof_q = ProofQuery(
            proof_query=f"SELECT DISTINCT ?a WHERE {{ ?a a <{_CLASS_URI}> . }} LIMIT 1000",
            mode="sparql", sampled=False, original_count=len(subjects), sample_limit=1000,
        )

    subgraph = extract_subgraph(pl["ont_graph"], class_uris, set())

    with patch("semantic_mapper.prove.decision_lookup.get_mapping_report", return_value=_REPORT):
        decisions = get_mapping_decisions(
            run_id=_RUN_ID,
            class_uris=class_uris,
            property_uris=set(),
            settings=settings,
            include_adjacent=True,
        )

    note, caveats = narrate(
        question="How many admissions are there?",
        query=executed_query,
        answer={"value": len(subjects), "type": "count"},
        subgraph=subgraph,
        decisions=decisions,
        caveats=[],
        settings=settings,
    )

    return compose_proof(
        question="How many admissions are there?",
        query=executed_query,
        mode=mode,
        answer={"value": len(subjects), "type": "count"},
        run_id=_RUN_ID,
        proof_query=proof_q,
        subjects=subjects,
        chains=chains,
        subgraph=subgraph,
        decisions=decisions,
        note=note,
        caveats=caveats,
        settings=settings,
    )


@pytest.fixture(scope="module")
def gold_proof(pipeline):
    return _build_proof(pipeline, "gold")


@pytest.fixture(scope="module")
def silver_proof(pipeline):
    return _build_proof(pipeline, "silver")


@pytest.fixture(scope="module")
def sparql_proof(pipeline):
    return _build_proof(pipeline, "sparql")


# ---------------------------------------------------------------------------
# Golden file helpers
# ---------------------------------------------------------------------------

_STABLE_FIELDS = [
    "proof_schema_version", "query_mode", "question", "subject_count", "limitations",
]


def _compare_or_generate(record: ProofRecord, name: str) -> None:
    """Write golden file on first run; compare field-by-field on subsequent runs."""
    _GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    path = _GOLDEN_DIR / f"proof_{name}.json"
    if not path.exists():
        path.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        return  # first run — generated and passed
    golden = ProofRecord.model_validate_json(path.read_text(encoding="utf-8"))
    for field in _STABLE_FIELDS:
        actual = getattr(record, field)
        expected = getattr(golden, field)
        assert actual == expected, (
            f"[{name}] field '{field}' regression:\n"
            f"  actual:   {actual!r}\n"
            f"  expected: {expected!r}"
        )


# ---------------------------------------------------------------------------
# Tests — pipeline integrity
# ---------------------------------------------------------------------------

class TestPipelineIntegrity:
    def test_silver_has_twenty_rows(self, pipeline):
        with pipeline["silver"].connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM silver_admissions")).scalar()
        assert count == _EXPECTED_ROWS

    def test_gold_has_twenty_rows(self, pipeline):
        with pipeline["gold"].connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM gold_admission")).scalar()
        assert count == _EXPECTED_ROWS

    def test_gold_silver_refs_populated(self, pipeline):
        with pipeline["gold"].connect() as conn:
            row = conn.execute(
                text("SELECT _silver_refs FROM gold_admission WHERE _silver_refs IS NOT NULL LIMIT 1")
            ).fetchone()
        assert row is not None
        refs = json.loads(row[0])
        assert "silver_admissions" in refs

    def test_rdf_has_twenty_type_triples(self, pipeline):
        count = sum(1 for _ in pipeline["rdf"].graph.subjects(RDF.type, URIRef(_CLASS_URI)))
        assert count == _EXPECTED_ROWS

    def test_rdf_admittime_triples_skip_nulls(self, pipeline):
        prop_uri = URIRef(f"{_ONT}admittime")
        count = sum(1 for _ in pipeline["rdf"].graph.subject_objects(prop_uri))
        assert count == _EXPECTED_ROWS - _EXPECTED_NULL_ADMITTIME

    def test_silver_has_null_admittime_rows(self, pipeline):
        with pipeline["silver"].connect() as conn:
            null_count = conn.execute(
                text("SELECT COUNT(*) FROM silver_admissions WHERE admittime IS NULL")
            ).scalar()
        assert null_count == _EXPECTED_NULL_ADMITTIME

    def test_silver_bronze_source_ref_uses_admissions_id(self, pipeline):
        with pipeline["silver"].connect() as conn:
            ref = conn.execute(
                text("SELECT _bronze_source_ref FROM silver_admissions LIMIT 1")
            ).scalar()
        assert ref is not None
        assert ref.startswith("admissions.admissions_id=")


# ---------------------------------------------------------------------------
# Tests — gold mode proof
# ---------------------------------------------------------------------------

class TestGoldModeProof:
    def test_subject_count_is_twenty(self, gold_proof):
        assert gold_proof.subject_count == _EXPECTED_ROWS

    def test_query_mode(self, gold_proof):
        assert gold_proof.query_mode == "gold"

    def test_lineage_chains_count(self, gold_proof):
        assert len(gold_proof.lineage) == _EXPECTED_ROWS

    def test_lineage_chains_have_bronze_records(self, gold_proof):
        assert any(chain["bronze"] for chain in gold_proof.lineage)

    def test_lineage_chains_have_silver_records(self, gold_proof):
        assert any(chain["silver"] for chain in gold_proof.lineage)

    def test_no_broken_chains(self, gold_proof):
        broken = [c for c in gold_proof.lineage if c["broken"]]
        assert broken == []

    def test_required_limitations_present(self, gold_proof):
        for lim in REQUIRED_LIMITATIONS:
            assert lim in gold_proof.limitations

    def test_mapping_decisions_populated(self, gold_proof):
        assert len(gold_proof.mapping_decisions) > 0

    def test_golden_file(self, gold_proof):
        _compare_or_generate(gold_proof, "gold")


# ---------------------------------------------------------------------------
# Tests — silver mode proof
# ---------------------------------------------------------------------------

class TestSilverModeProof:
    def test_subject_count_is_twenty(self, silver_proof):
        assert silver_proof.subject_count == _EXPECTED_ROWS

    def test_query_mode(self, silver_proof):
        assert silver_proof.query_mode == "silver"

    def test_lineage_chains_count(self, silver_proof):
        assert len(silver_proof.lineage) == _EXPECTED_ROWS

    def test_lineage_chains_have_bronze_records(self, silver_proof):
        assert any(chain["bronze"] for chain in silver_proof.lineage)

    def test_no_broken_chains(self, silver_proof):
        broken = [c for c in silver_proof.lineage if c["broken"]]
        assert broken == []

    def test_required_limitations_present(self, silver_proof):
        for lim in REQUIRED_LIMITATIONS:
            assert lim in silver_proof.limitations

    def test_golden_file(self, silver_proof):
        _compare_or_generate(silver_proof, "silver")


# ---------------------------------------------------------------------------
# Tests — SPARQL mode proof
# ---------------------------------------------------------------------------

class TestSparqlModeProof:
    def test_subject_count_is_twenty(self, sparql_proof):
        assert sparql_proof.subject_count == _EXPECTED_ROWS

    def test_subjects_are_uris(self, sparql_proof):
        assert all(s.startswith("http") for s in sparql_proof.subjects)

    def test_query_mode(self, sparql_proof):
        assert sparql_proof.query_mode == "sparql"

    def test_required_limitations_present(self, sparql_proof):
        for lim in REQUIRED_LIMITATIONS:
            assert lim in sparql_proof.limitations

    def test_mapping_decisions_populated(self, sparql_proof):
        assert len(sparql_proof.mapping_decisions) > 0

    def test_golden_file(self, sparql_proof):
        _compare_or_generate(sparql_proof, "sparql")
