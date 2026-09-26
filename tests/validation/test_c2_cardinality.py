# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for C2 — relationship cardinality."""
from __future__ import annotations

from rdflib import RDF, Dataset, Literal, Namespace, URIRef

from semantic_mapper.validation.checks.cardinality import CardinalityCheck
from semantic_mapper.validation.context import ValidationContext
from semantic_mapper.validation.sparql_backend import RdflibBackend

from .conftest import BASE, FM, GRAPH_URI, ONT


class TestC2CardinalityPass:
    def test_passes_on_known_good_graph(self, good_ctx: ValidationContext) -> None:
        result = CardinalityCheck().run(good_ctx)
        assert result.passed, result.message
        assert result.check_id == "C2"

    def test_all_ratios_in_details(self, good_ctx: ValidationContext) -> None:
        result = CardinalityCheck().run(good_ctx)
        assert "all_ratios" in result.details
        # Should have 3 object properties (hasSourceAirport, hasDestAirport, hasAirline)
        assert len(result.details["all_ratios"]) == 3

    def test_ratios_are_one(self, good_ctx: ValidationContext) -> None:
        result = CardinalityCheck().run(good_ctx)
        for row in result.details["all_ratios"]:
            assert row["ratio"] == pytest.approx(1.0, abs=0.01), (
                f"{row['property']} has ratio {row['ratio']}"
            )

    def test_skips_check_with_no_ontology(
        self, good_ctx: ValidationContext
    ) -> None:
        ctx = ValidationContext(
            project_name=good_ctx.project_name,
            graph_uri=good_ctx.graph_uri,
            sparql=good_ctx.sparql,
            gold_engine=good_ctx.gold_engine,
            mapping_report=good_ctx.mapping_report,
            ontology=None,
            baseline_run_id=good_ctx.baseline_run_id,
        )
        result = CardinalityCheck().run(ctx)
        assert not result.passed
        assert "no ontology" in result.message


class TestC2CardinalityFail:
    """Fault injection: emit two hasAirline triples per route (double-mint)."""

    def _double_mint_ctx(self, good_ctx: ValidationContext) -> ValidationContext:
        # Take the good dataset and add a second hasAirline triple for route/1
        import copy
        from rdflib import ConjunctiveGraph

        ds = Dataset()
        g = ds.graph(URIRef(GRAPH_URI))
        good_g = good_ctx.sparql._g.graph(URIRef(GRAPH_URI))
        for triple in good_g:
            g.add(triple)

        # Add a spurious second airline for route/1
        g.add((BASE["route/1"], ONT.hasAirline, BASE["airline/BB"]))

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

    def test_fails_on_double_mint(self, good_ctx: ValidationContext) -> None:
        ctx = self._double_mint_ctx(good_ctx)
        result = CardinalityCheck(ratio_threshold=1.2).run(ctx)
        assert not result.passed

    def test_flagged_in_details(self, good_ctx: ValidationContext) -> None:
        ctx = self._double_mint_ctx(good_ctx)
        result = CardinalityCheck(ratio_threshold=1.2).run(ctx)
        flagged_props = [r["property"] for r in result.details["flagged"]]
        assert str(ONT.hasAirline) in flagged_props

    def test_multivalued_property_is_exempt(self, good_ctx: ValidationContext) -> None:
        # A high ratio on a property explicitly marked fm:multiValued (e.g. an
        # exploded packed column) is expected fan-out, not a double-mint — exempt it.
        ctx = self._double_mint_ctx(good_ctx)
        ctx.ontology.add((ONT.hasAirline, FM.multiValued, Literal(True)))
        result = CardinalityCheck(ratio_threshold=1.2).run(ctx)
        assert result.passed, result.message
        rated = [r["property"] for r in result.details["all_ratios"]]
        assert str(ONT.hasAirline) not in rated  # not even measured


import pytest
