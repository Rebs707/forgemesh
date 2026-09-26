# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for C1 — entity count reconciliation."""
from __future__ import annotations

from rdflib import RDF, Dataset, Namespace, URIRef
from sqlalchemy import create_engine, text

from semantic_mapper.validation.checks.entity_count import EntityCountCheck
from semantic_mapper.validation.context import ValidationContext
from semantic_mapper.validation.sparql_backend import RdflibBackend

from .conftest import (
    BASELINE_RUN_ID,
    GRAPH_URI,
    ONT,
    PROJECT_NAME,
    BASE,
    FM,
)

BASE_NS = Namespace(str(BASE))


class TestC1EntityCountPass:
    def test_passes_on_known_good_graph(self, good_ctx: ValidationContext) -> None:
        result = EntityCountCheck().run(good_ctx)
        assert result.passed, result.message
        assert result.check_id == "C1"

    def test_message_mentions_count(self, good_ctx: ValidationContext) -> None:
        result = EntityCountCheck().run(good_ctx)
        assert "reconciled" in result.message

    def test_details_have_all_tables(self, good_ctx: ValidationContext) -> None:
        result = EntityCountCheck().run(good_ctx)
        tables = result.details["all_tables"]
        table_names = [t["gold_table"] for t in tables]
        assert "gold_airport" in table_names
        assert "gold_airline" in table_names
        assert "gold_route" in table_names


class TestC1EntityCountFail:
    """Fault injection: collapse 3 airports onto 1 URI → entity count mismatch."""

    def _collapsed_ctx(self, good_ctx: ValidationContext) -> ValidationContext:
        # Build a dataset where all 3 airports share the same URI
        ds = Dataset()
        g = ds.graph(URIRef(GRAPH_URI))
        collapsed_uri = BASE["airport/1"]
        for _aid in range(1, 4):
            g.add((collapsed_uri, RDF.type, ONT.Airport))

        backend = RdflibBackend(ds)
        return ValidationContext(
            project_name=good_ctx.project_name,
            graph_uri=good_ctx.graph_uri,
            sparql=backend,
            gold_engine=good_ctx.gold_engine,
            mapping_report=good_ctx.mapping_report,
            ontology=good_ctx.ontology,
            baseline_run_id=good_ctx.baseline_run_id,
        )

    def test_fails_when_rows_collapsed(self, good_ctx: ValidationContext) -> None:
        ctx = self._collapsed_ctx(good_ctx)
        result = EntityCountCheck().run(ctx)
        assert not result.passed

    def test_message_shows_mismatch(self, good_ctx: ValidationContext) -> None:
        ctx = self._collapsed_ctx(good_ctx)
        result = EntityCountCheck().run(ctx)
        assert "mismatch" in result.message.lower()

    def test_details_list_mismatches(self, good_ctx: ValidationContext) -> None:
        ctx = self._collapsed_ctx(good_ctx)
        result = EntityCountCheck().run(ctx)
        assert len(result.details["mismatches"]) >= 1
        airport_mismatch = next(
            m for m in result.details["mismatches"]
            if m["gold_table"] == "gold_airport"
        )
        assert airport_mismatch["graph_count"] == 1
        assert airport_mismatch["gold_count"] == 3
