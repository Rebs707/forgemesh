# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for C5 — incompleteness reconciliation."""
from __future__ import annotations

from rdflib import RDF, Dataset, Literal, URIRef
from sqlalchemy import create_engine, text

from semantic_mapper.validation.checks.incompleteness import IncompletenessCheck
from semantic_mapper.validation.context import ValidationContext
from semantic_mapper.validation.sparql_backend import RdflibBackend

from .conftest import BASE, FM, GRAPH_URI, ONT, BASELINE_RUN_ID, PROJECT_NAME


class TestC5IncompletenessPass:
    """Known-good graph has no null FKs and no incompleteReference triples."""

    def test_passes_on_known_good_graph(self, good_ctx: ValidationContext) -> None:
        result = IncompletenessCheck().run(good_ctx)
        assert result.passed, result.message
        assert result.check_id == "C5"

    def test_both_counts_zero(self, good_ctx: ValidationContext) -> None:
        result = IncompletenessCheck().run(good_ctx)
        assert result.details["graph_incomplete_count"] == 0
        assert result.details["gold_null_fk_count"] == 0


class TestC5IncompletenessMatchingNulls:
    """One route with a null airline FK + matching incompleteReference triple."""

    def _ctx_with_one_null(self, good_ctx: ValidationContext) -> ValidationContext:
        # Add an incompleteReference triple for the null airline FK
        ds = Dataset()
        g = ds.graph(URIRef(GRAPH_URI))
        good_g = good_ctx.sparql._g.graph(URIRef(GRAPH_URI))
        for triple in good_g:
            g.add(triple)
        # Emit the quality triple (null FK)
        g.add((BASE["route/4"], FM.incompleteReference, Literal(str(ONT.hasAirline))))

        # Gold DB: add a route row with null airline
        from sqlalchemy import create_engine, text
        engine = create_engine("sqlite:///:memory:", future=True)
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE gold_airport (id INTEGER PRIMARY KEY,
                    name TEXT, iata TEXT,
                    _bronze_source_ref TEXT, _gold_run_id TEXT)
            """))
            conn.execute(text(
                "INSERT INTO gold_airport VALUES (1,'A One','A01','airports/1','run_test_001')"
            ))
            conn.execute(text("""
                CREATE TABLE gold_airline (id TEXT PRIMARY KEY,
                    alias TEXT, _bronze_source_ref TEXT, _gold_run_id TEXT)
            """))
            conn.execute(text(
                "INSERT INTO gold_airline VALUES ('AA','Airline AA','airlines/AA','run_test_001')"
            ))
            conn.execute(text("""
                CREATE TABLE gold_route (
                    id INTEGER PRIMARY KEY,
                    source_airport_id INTEGER,
                    dest_airport_id   INTEGER,
                    airline_id        TEXT,
                    _bronze_source_ref TEXT, _gold_run_id TEXT)
            """))
            conn.execute(text("""
                INSERT INTO gold_route VALUES
                    (1, 1, 1, 'AA', 'routes/1', 'run_test_001'),
                    (4, 1, 1, NULL, 'routes/4', 'run_test_001')
            """))

        backend = RdflibBackend(ds)
        return ValidationContext(
            project_name=good_ctx.project_name,
            graph_uri=good_ctx.graph_uri,
            sparql=backend,
            gold_engine=engine,
            mapping_report=good_ctx.mapping_report,
            ontology=good_ctx.ontology,
            baseline_run_id=good_ctx.baseline_run_id,
        )

    def test_passes_when_counts_match(self, good_ctx: ValidationContext) -> None:
        ctx = self._ctx_with_one_null(good_ctx)
        result = IncompletenessCheck().run(ctx)
        assert result.passed, result.message
        assert result.details["graph_incomplete_count"] == 1
        assert result.details["gold_null_fk_count"] == 1


class TestC5IncompletnessFail:
    """Fault injection: null FK in gold with no matching incompleteReference triple."""

    def _ctx_missing_triple(self, good_ctx: ValidationContext) -> ValidationContext:
        """Gold has 1 null FK; graph has 0 incompleteReference triples."""
        engine = create_engine("sqlite:///:memory:", future=True)
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE gold_airport (id INTEGER PRIMARY KEY,
                    name TEXT, iata TEXT,
                    _bronze_source_ref TEXT, _gold_run_id TEXT)
            """))
            conn.execute(text(
                "INSERT INTO gold_airport VALUES (1,'A One','A01','airports/1','run_test_001')"
            ))
            conn.execute(text("""
                CREATE TABLE gold_airline (id TEXT PRIMARY KEY,
                    alias TEXT, _bronze_source_ref TEXT, _gold_run_id TEXT)
            """))
            conn.execute(text(
                "INSERT INTO gold_airline VALUES ('AA','Airline AA','airlines/AA','run_test_001')"
            ))
            conn.execute(text("""
                CREATE TABLE gold_route (
                    id INTEGER PRIMARY KEY,
                    source_airport_id INTEGER,
                    dest_airport_id   INTEGER,
                    airline_id        TEXT,
                    _bronze_source_ref TEXT, _gold_run_id TEXT)
            """))
            # Route 4 has null airline — but NO quality triple will be in the graph
            conn.execute(text("""
                INSERT INTO gold_route VALUES
                    (1, 1, 1, 'AA', 'routes/1', 'run_test_001'),
                    (4, 1, 1, NULL, 'routes/4', 'run_test_001')
            """))

        # Graph has NO incompleteReference triples
        ds = Dataset()
        g = ds.graph(URIRef(GRAPH_URI))
        good_g = good_ctx.sparql._g.graph(URIRef(GRAPH_URI))
        for triple in good_g:
            g.add(triple)

        backend = RdflibBackend(ds)
        return ValidationContext(
            project_name=good_ctx.project_name,
            graph_uri=good_ctx.graph_uri,
            sparql=backend,
            gold_engine=engine,
            mapping_report=good_ctx.mapping_report,
            ontology=good_ctx.ontology,
            baseline_run_id=good_ctx.baseline_run_id,
        )

    def test_fails_when_null_not_recorded(self, good_ctx: ValidationContext) -> None:
        ctx = self._ctx_missing_triple(good_ctx)
        result = IncompletenessCheck().run(ctx)
        assert not result.passed

    def test_message_shows_delta(self, good_ctx: ValidationContext) -> None:
        ctx = self._ctx_missing_triple(good_ctx)
        result = IncompletenessCheck().run(ctx)
        assert "delta" in result.message.lower() or "1" in result.message
