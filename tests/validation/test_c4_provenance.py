# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for C4 — provenance completeness."""
from __future__ import annotations

from rdflib import RDF, Dataset, Literal, URIRef

from semantic_mapper.validation.checks.provenance import ProvenanceCheck
from semantic_mapper.validation.context import ValidationContext
from semantic_mapper.validation.sparql_backend import RdflibBackend

from .conftest import BASE, FM, GRAPH_URI, ONT, BASELINE_RUN_ID


class TestC4ProvenancePass:
    def test_passes_on_known_good_graph(self, good_ctx: ValidationContext) -> None:
        result = ProvenanceCheck().run(good_ctx)
        assert result.passed, result.message
        assert result.check_id == "C4"

    def test_message_mentions_complete(self, good_ctx: ValidationContext) -> None:
        result = ProvenanceCheck().run(good_ctx)
        assert "complete" in result.message

    def test_details_have_all_tables(self, good_ctx: ValidationContext) -> None:
        result = ProvenanceCheck().run(good_ctx)
        table_names = [t["gold_table"] for t in result.details["all_tables"]]
        assert "gold_airport" in table_names
        assert "gold_airline" in table_names
        assert "gold_route" in table_names

    def test_all_subjects_have_provenance(self, good_ctx: ValidationContext) -> None:
        result = ProvenanceCheck().run(good_ctx)
        for t in result.details["all_tables"]:
            assert t["total_subjects"] == t["with_provenance"], (
                f"{t['gold_table']}: {t['with_provenance']}/{t['total_subjects']}"
            )


class TestC4ProvenanceFail:
    """Fault injection: suppress fm:bronzeSourceRef for one class."""

    def _missing_prov_ctx(self, good_ctx: ValidationContext) -> ValidationContext:
        """Remove bronzeSourceRef from all airline subjects."""
        ds = Dataset()
        g = ds.graph(URIRef(GRAPH_URI))
        good_g = good_ctx.sparql._g.graph(URIRef(GRAPH_URI))
        for s, p, o in good_g:
            # Drop bronzeSourceRef triples for airlines
            if p == FM.bronzeSourceRef and "airline" in str(s):
                continue
            g.add((s, p, o))

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

    def test_fails_when_provenance_missing(self, good_ctx: ValidationContext) -> None:
        ctx = self._missing_prov_ctx(good_ctx)
        result = ProvenanceCheck().run(ctx)
        assert not result.passed

    def test_identifies_affected_class(self, good_ctx: ValidationContext) -> None:
        ctx = self._missing_prov_ctx(good_ctx)
        result = ProvenanceCheck().run(ctx)
        affected = [m["gold_table"] for m in result.details["mismatches"]]
        assert "gold_airline" in affected

    def test_does_not_flag_unaffected_classes(self, good_ctx: ValidationContext) -> None:
        ctx = self._missing_prov_ctx(good_ctx)
        result = ProvenanceCheck().run(ctx)
        affected = {m["gold_table"] for m in result.details["mismatches"]}
        assert "gold_airport" not in affected
