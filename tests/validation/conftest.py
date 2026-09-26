# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the validation check suite tests.

The "known-good" fixture uses a tiny three-table graph
(Airport / Airline / Route) that mirrors the structural patterns from the
OpenFlights baseline:

* 3 airports  → gold_airport (3 rows)
* 2 airlines  → gold_airline (2 rows)
* 3 routes    → gold_route   (3 rows)

All triples are minted under the same URI scheme so every join resolves, all
provenance predicates are present, and the load stamp matches the baseline run
id "run_test_001".

Gold column names follow the GoldDefinitionBuilder rule:
    gold_column_name = _safe_col_name(source_column)  # = source_column.lower()
So source_column "name" → gold column "name"; "source_airport_id" → "source_airport_id".
"""
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, Dataset, Graph, Literal, Namespace, URIRef
from sqlalchemy import create_engine, text

from semantic_mapper.schema.mapping_output import (
    ClassCandidate,
    MappingStatus,
    PropertyMapping,
    SemanticMappingReport,
    TableMapping,
)
from semantic_mapper.validation.context import NLQuestion, ValidationContext
from semantic_mapper.validation.sparql_backend import RdflibBackend

# ── Namespaces ────────────────────────────────────────────────────────────────

BASE      = Namespace("http://mesh.local/resource/")
ONT       = Namespace("http://mesh.local/ontology/test#")
FM        = Namespace("http://forgemesh.ai/ontology#")
GRAPH_URI = "http://mesh.local/graph/test"

# ── Constants ─────────────────────────────────────────────────────────────────

BASELINE_RUN_ID  = "run_test_001"
PROJECT_NAME     = "test_project"
ONTOLOGY_SOURCE  = "test_ontology_v1.owl"


# ── Ontology ──────────────────────────────────────────────────────────────────

def _make_ontology() -> Graph:
    g = Graph()
    # Classes
    g.add((ONT.Airport, RDF.type, OWL.Class))
    g.add((ONT.Airline, RDF.type, OWL.Class))
    g.add((ONT.Route,   RDF.type, OWL.Class))

    # Datatype properties
    for prop, domain in [
        (ONT.hasName,  ONT.Airport),
        (ONT.hasIata,  ONT.Airport),
        (ONT.hasAlias, ONT.Airline),
    ]:
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        g.add((prop, RDFS.domain, domain))

    # Object (FK) properties
    g.add((ONT.hasSourceAirport, RDF.type, OWL.ObjectProperty))
    g.add((ONT.hasSourceAirport, RDFS.domain, ONT.Route))
    g.add((ONT.hasSourceAirport, RDFS.range,  ONT.Airport))

    g.add((ONT.hasDestAirport, RDF.type, OWL.ObjectProperty))
    g.add((ONT.hasDestAirport, RDFS.domain, ONT.Route))
    g.add((ONT.hasDestAirport, RDFS.range,  ONT.Airport))

    g.add((ONT.hasAirline, RDF.type, OWL.ObjectProperty))
    g.add((ONT.hasAirline, RDFS.domain, ONT.Route))
    g.add((ONT.hasAirline, RDFS.range,  ONT.Airline))

    return g


@pytest.fixture(scope="session")
def ontology() -> Graph:
    return _make_ontology()


# ── Mapping report ────────────────────────────────────────────────────────────

def _pm(
    col: str,
    prop_uri: str,
    status: MappingStatus = MappingStatus.CONFIRMED,
    data_type: str = "VARCHAR",
    nullable: bool = False,
) -> PropertyMapping:
    return PropertyMapping(
        source_column=col,
        source_data_type=data_type,
        is_nullable=nullable,
        mapped_property_uri=prop_uri,
        mapped_property_label=None,
        status=status,
        confidence_score=1.0,
        rationale="test fixture",
    )


def _make_mapping_report() -> SemanticMappingReport:
    tables = [
        TableMapping(
            source_table="raw_airport",
            class_candidate_set=[
                ClassCandidate(
                    uri=str(ONT.Airport),
                    label="Airport",
                    rationale="test fixture",
                    confidence_score=1.0,
                )
            ],
            column_mappings=[
                _pm("name", str(ONT.hasName)),
                _pm("iata", str(ONT.hasIata)),
            ],
        ),
        TableMapping(
            source_table="raw_airline",
            class_candidate_set=[
                ClassCandidate(
                    uri=str(ONT.Airline),
                    label="Airline",
                    rationale="test fixture",
                    confidence_score=1.0,
                )
            ],
            column_mappings=[
                _pm("alias", str(ONT.hasAlias)),
            ],
        ),
        TableMapping(
            source_table="raw_route",
            class_candidate_set=[
                ClassCandidate(
                    uri=str(ONT.Route),
                    label="Route",
                    rationale="test fixture",
                    confidence_score=1.0,
                )
            ],
            column_mappings=[
                _pm("source_airport_id", str(ONT.hasSourceAirport), nullable=True),
                _pm("dest_airport_id",   str(ONT.hasDestAirport),   nullable=True),
                _pm("airline_id",        str(ONT.hasAirline),        nullable=True),
            ],
        ),
    ]
    return SemanticMappingReport(
        ontology_uri=str(ONT),
        database_name="test_db",
        settings_used={},
        tables=tables,
        total_pending_review=0,
    )


@pytest.fixture(scope="session")
def mapping_report() -> SemanticMappingReport:
    return _make_mapping_report()


# ── rdflib Dataset (known-good graph) ─────────────────────────────────────────

def _make_dataset() -> Dataset:
    ds = Dataset()
    g = ds.graph(URIRef(GRAPH_URI))

    # Airports (gold columns: id, name, iata)
    for aid, name, iata in [(1, "Airport One", "A01"), (2, "Airport Two", "A02"), (3, "Airport Three", "A03")]:
        s = BASE[f"airport/{aid}"]
        g.add((s, RDF.type,           ONT.Airport))
        g.add((s, ONT.hasName,        Literal(name)))
        g.add((s, ONT.hasIata,        Literal(iata)))
        g.add((s, FM.bronzeSourceRef, Literal(f"airports/{aid}")))
        g.add((s, FM.goldRunId,       Literal(BASELINE_RUN_ID)))

    # Airlines (gold columns: id, alias)
    for code, alias in [("AA", "Airline AA"), ("BB", "Airline BB")]:
        s = BASE[f"airline/{code}"]
        g.add((s, RDF.type,           ONT.Airline))
        g.add((s, ONT.hasAlias,       Literal(alias)))
        g.add((s, FM.bronzeSourceRef, Literal(f"airlines/{code}")))
        g.add((s, FM.goldRunId,       Literal(BASELINE_RUN_ID)))

    # Routes (gold columns: id, source_airport_id, dest_airport_id, airline_id)
    route_data = [
        (1, 1, 2, "AA"),
        (2, 2, 3, "BB"),
        (3, 1, 3, "AA"),
    ]
    for rid, src, dst, acode in route_data:
        s = BASE[f"route/{rid}"]
        g.add((s, RDF.type,              ONT.Route))
        g.add((s, ONT.hasSourceAirport,  BASE[f"airport/{src}"]))
        g.add((s, ONT.hasDestAirport,    BASE[f"airport/{dst}"]))
        g.add((s, ONT.hasAirline,        BASE[f"airline/{acode}"]))
        g.add((s, FM.bronzeSourceRef,    Literal(f"routes/{rid}")))
        g.add((s, FM.goldRunId,          Literal(BASELINE_RUN_ID)))

    # Graph metadata node
    graph_node = URIRef(GRAPH_URI)
    g.add((graph_node, FM.loadRunId,       Literal(BASELINE_RUN_ID)))
    g.add((graph_node, FM.projectName,     Literal(PROJECT_NAME)))
    g.add((graph_node, FM.loadTimestamp,   Literal("2026-05-22T00:00:00")))
    g.add((graph_node, FM.ontologySource,  Literal(ONTOLOGY_SOURCE)))

    return ds


@pytest.fixture
def good_dataset() -> Dataset:
    """Fresh known-good rdflib Dataset (function-scoped so mutations don't bleed)."""
    return _make_dataset()


@pytest.fixture
def good_sparql(good_dataset: Dataset) -> RdflibBackend:
    return RdflibBackend(good_dataset)


# ── SQLite gold DB ────────────────────────────────────────────────────────────
#
# Column names match GoldDefinitionBuilder output:
#   gold_column_name = _safe_col_name(source_column) = source_column.lower()
# So:  source_column "name" → gold column "name"
#      source_column "source_airport_id" → gold column "source_airport_id"

def _make_gold_engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE gold_airport (
                id                   INTEGER PRIMARY KEY,
                name                 TEXT,
                iata                 TEXT,
                _bronze_source_ref   TEXT,
                _gold_run_id         TEXT
            )
        """))
        conn.execute(text("""
            INSERT INTO gold_airport VALUES
                (1, 'Airport One',   'A01', 'airports/1', 'run_test_001'),
                (2, 'Airport Two',   'A02', 'airports/2', 'run_test_001'),
                (3, 'Airport Three', 'A03', 'airports/3', 'run_test_001')
        """))

        conn.execute(text("""
            CREATE TABLE gold_airline (
                id                   TEXT PRIMARY KEY,
                alias                TEXT,
                _bronze_source_ref   TEXT,
                _gold_run_id         TEXT
            )
        """))
        conn.execute(text("""
            INSERT INTO gold_airline VALUES
                ('AA', 'Airline AA', 'airlines/AA', 'run_test_001'),
                ('BB', 'Airline BB', 'airlines/BB', 'run_test_001')
        """))

        conn.execute(text("""
            CREATE TABLE gold_route (
                id                   INTEGER PRIMARY KEY,
                source_airport_id    INTEGER,
                dest_airport_id      INTEGER,
                airline_id           TEXT,
                _bronze_source_ref   TEXT,
                _gold_run_id         TEXT
            )
        """))
        conn.execute(text("""
            INSERT INTO gold_route VALUES
                (1, 1, 2, 'AA', 'routes/1', 'run_test_001'),
                (2, 2, 3, 'BB', 'routes/2', 'run_test_001'),
                (3, 1, 3, 'AA', 'routes/3', 'run_test_001')
        """))
    return engine


@pytest.fixture
def gold_engine():
    return _make_gold_engine()


# ── ValidationContext ──────────────────────────────────────────────────────────

@pytest.fixture
def good_ctx(
    good_sparql: RdflibBackend,
    gold_engine,
    mapping_report: SemanticMappingReport,
    ontology: Graph,
) -> ValidationContext:
    return ValidationContext(
        project_name=PROJECT_NAME,
        graph_uri=GRAPH_URI,
        sparql=good_sparql,
        gold_engine=gold_engine,
        mapping_report=mapping_report,
        ontology=ontology,
        baseline_run_id=BASELINE_RUN_ID,
        ontology_source=ONTOLOGY_SOURCE,
    )
