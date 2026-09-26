# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for C3 — join integrity."""
from __future__ import annotations

from rdflib import RDF, Dataset, Literal, Namespace, URIRef

from semantic_mapper.validation.checks.join_integrity import JoinIntegrityCheck
from semantic_mapper.validation.context import ValidationContext
from semantic_mapper.validation.sparql_backend import RdflibBackend

from .conftest import BASE, FM, GRAPH_URI, ONT, BASELINE_RUN_ID, PROJECT_NAME


class TestC3JoinIntegrityPass:
    def test_passes_on_known_good_graph(self, good_ctx: ValidationContext) -> None:
        result = JoinIntegrityCheck().run(good_ctx)
        assert result.passed, result.message
        assert result.check_id == "C3"

    def test_all_joins_in_details(self, good_ctx: ValidationContext) -> None:
        result = JoinIntegrityCheck().run(good_ctx)
        # 3 FK object properties — but hasSourceAirport and hasDestAirport share same range
        all_joins = result.details["all_joins"]
        props = {j["property"] for j in all_joins}
        assert str(ONT.hasSourceAirport) in props
        assert str(ONT.hasDestAirport) in props
        assert str(ONT.hasAirline) in props

    def test_all_join_counts_positive(self, good_ctx: ValidationContext) -> None:
        result = JoinIntegrityCheck().run(good_ctx)
        for j in result.details["all_joins"]:
            assert j["join_count"] > 0, f"{j['property']} resolved zero joins"

    def test_skips_check_with_no_ontology(self, good_ctx: ValidationContext) -> None:
        ctx = ValidationContext(
            project_name=good_ctx.project_name,
            graph_uri=good_ctx.graph_uri,
            sparql=good_ctx.sparql,
            gold_engine=good_ctx.gold_engine,
            mapping_report=good_ctx.mapping_report,
            ontology=None,
            baseline_run_id=good_ctx.baseline_run_id,
        )
        result = JoinIntegrityCheck().run(ctx)
        assert not result.passed
        assert "no ontology" in result.message


class TestC3JoinIntegrityFail:
    """Fault injection: mint airport URIs under a different scheme for routes."""

    def _mismatched_ctx(self, good_ctx: ValidationContext) -> ValidationContext:
        """Routes link to airports under 'broken/' prefix — typed airports use 'airport/'."""
        ds = Dataset()
        g = ds.graph(URIRef(GRAPH_URI))

        # Correctly typed airports
        for aid in (1, 2, 3):
            g.add((BASE[f"airport/{aid}"], RDF.type, ONT.Airport))

        # Routes pointing to broken URIs
        BROKEN = Namespace("http://mesh.local/resource/broken/")
        for rid in (1, 2, 3):
            s = BASE[f"route/{rid}"]
            g.add((s, RDF.type,             ONT.Route))
            g.add((s, ONT.hasSourceAirport, BROKEN[f"airport/{rid}"]))
            g.add((s, ONT.hasDestAirport,   BROKEN[f"airport/{rid}"]))
            # Airline joins still work (same scheme)
            code = "AA" if rid != 2 else "BB"
            g.add((BASE[f"airline/{code}"], RDF.type, ONT.Airline))
            g.add((s, ONT.hasAirline, BASE[f"airline/{code}"]))

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

    def test_fails_on_uri_scheme_mismatch(self, good_ctx: ValidationContext) -> None:
        ctx = self._mismatched_ctx(good_ctx)
        result = JoinIntegrityCheck().run(ctx)
        assert not result.passed

    def test_broken_joins_listed(self, good_ctx: ValidationContext) -> None:
        ctx = self._mismatched_ctx(good_ctx)
        result = JoinIntegrityCheck().run(ctx)
        broken_props = {b["property"] for b in result.details["broken"]}
        assert str(ONT.hasSourceAirport) in broken_props
        assert str(ONT.hasDestAirport) in broken_props

    def test_working_join_not_flagged(self, good_ctx: ValidationContext) -> None:
        ctx = self._mismatched_ctx(good_ctx)
        result = JoinIntegrityCheck().run(ctx)
        broken_props = {b["property"] for b in result.details["broken"]}
        assert str(ONT.hasAirline) not in broken_props
