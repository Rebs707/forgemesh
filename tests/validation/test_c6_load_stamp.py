# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for C6 — load-stamp freshness and project identity."""
from __future__ import annotations

from rdflib import Dataset, Literal, URIRef

from semantic_mapper.validation.checks.load_stamp import LoadStampCheck
from semantic_mapper.validation.context import ValidationContext
from semantic_mapper.validation.sparql_backend import RdflibBackend

from .conftest import BASE, FM, GRAPH_URI, ONT, BASELINE_RUN_ID, PROJECT_NAME, ONTOLOGY_SOURCE


class TestC6LoadStampPass:
    def test_passes_on_known_good_graph(self, good_ctx: ValidationContext) -> None:
        result = LoadStampCheck().run(good_ctx)
        assert result.passed, result.message
        assert result.check_id == "C6"

    def test_details_show_expected_values(self, good_ctx: ValidationContext) -> None:
        result = LoadStampCheck().run(good_ctx)
        assert result.details["actual_run_id"] == BASELINE_RUN_ID
        assert result.details["actual_project"] == PROJECT_NAME
        assert result.details["actual_ontology"] == ONTOLOGY_SOURCE

    def test_message_mentions_run_id(self, good_ctx: ValidationContext) -> None:
        result = LoadStampCheck().run(good_ctx)
        assert BASELINE_RUN_ID in result.message


class TestC6LoadStampFailStaleRunId:
    """Fault injection: graph has an old run ID."""

    def _stale_ctx(self, good_ctx: ValidationContext) -> ValidationContext:
        ds = Dataset()
        g = ds.graph(URIRef(GRAPH_URI))
        good_g = good_ctx.sparql._g.graph(URIRef(GRAPH_URI))
        for s, p, o in good_g:
            if p == FM.loadRunId:
                g.add((s, p, Literal("old_run_999")))
            else:
                g.add((s, p, o))

        return ValidationContext(
            project_name=good_ctx.project_name,
            graph_uri=good_ctx.graph_uri,
            sparql=RdflibBackend(ds),
            gold_engine=good_ctx.gold_engine,
            mapping_report=good_ctx.mapping_report,
            ontology=good_ctx.ontology,
            baseline_run_id=BASELINE_RUN_ID,
        )

    def test_fails_with_stale_run_id(self, good_ctx: ValidationContext) -> None:
        ctx = self._stale_ctx(good_ctx)
        result = LoadStampCheck().run(ctx)
        assert not result.passed

    def test_message_shows_mismatch(self, good_ctx: ValidationContext) -> None:
        ctx = self._stale_ctx(good_ctx)
        result = LoadStampCheck().run(ctx)
        assert "runId mismatch" in result.message

    def test_details_show_both_ids(self, good_ctx: ValidationContext) -> None:
        ctx = self._stale_ctx(good_ctx)
        result = LoadStampCheck().run(ctx)
        assert result.details["expected_run_id"] == BASELINE_RUN_ID
        assert result.details["actual_run_id"] == "old_run_999"


class TestC6LoadStampFailWrongProject:
    """Fault injection: graph carries the wrong project name."""

    def _wrong_project_ctx(self, good_ctx: ValidationContext) -> ValidationContext:
        ds = Dataset()
        g = ds.graph(URIRef(GRAPH_URI))
        good_g = good_ctx.sparql._g.graph(URIRef(GRAPH_URI))
        for s, p, o in good_g:
            if p == FM.projectName:
                g.add((s, p, Literal("wrong_project")))
            else:
                g.add((s, p, o))

        return ValidationContext(
            project_name=PROJECT_NAME,
            graph_uri=good_ctx.graph_uri,
            sparql=RdflibBackend(ds),
            gold_engine=good_ctx.gold_engine,
            mapping_report=good_ctx.mapping_report,
            ontology=good_ctx.ontology,
            baseline_run_id=BASELINE_RUN_ID,
        )

    def test_fails_with_wrong_project(self, good_ctx: ValidationContext) -> None:
        ctx = self._wrong_project_ctx(good_ctx)
        result = LoadStampCheck().run(ctx)
        assert not result.passed

    def test_message_shows_project_mismatch(self, good_ctx: ValidationContext) -> None:
        ctx = self._wrong_project_ctx(good_ctx)
        result = LoadStampCheck().run(ctx)
        assert "projectName mismatch" in result.message


class TestC6LoadStampFailNoMetadata:
    """Fault injection: graph has no load-stamp metadata at all."""

    def _no_metadata_ctx(self, good_ctx: ValidationContext) -> ValidationContext:
        ds = Dataset()
        g = ds.graph(URIRef(GRAPH_URI))
        good_g = good_ctx.sparql._g.graph(URIRef(GRAPH_URI))
        for s, p, o in good_g:
            if p in (FM.loadRunId, FM.projectName, FM.loadTimestamp, FM.ontologySource):
                continue
            g.add((s, p, o))

        return ValidationContext(
            project_name=good_ctx.project_name,
            graph_uri=good_ctx.graph_uri,
            sparql=RdflibBackend(ds),
            gold_engine=good_ctx.gold_engine,
            mapping_report=good_ctx.mapping_report,
            ontology=good_ctx.ontology,
            baseline_run_id=BASELINE_RUN_ID,
            ontology_source=ONTOLOGY_SOURCE,
        )

    def test_fails_when_no_metadata(self, good_ctx: ValidationContext) -> None:
        ctx = self._no_metadata_ctx(good_ctx)
        result = LoadStampCheck().run(ctx)
        assert not result.passed

    def test_message_mentions_missing_stamp(self, good_ctx: ValidationContext) -> None:
        ctx = self._no_metadata_ctx(good_ctx)
        result = LoadStampCheck().run(ctx)
        assert "no load-stamp" in result.message


class TestC6OntologyDrift:
    """Fault injection: graph was materialised with a different ontology version."""

    def _drift_ctx(self, good_ctx: ValidationContext, stamped: str, expected: str) -> ValidationContext:
        ds = Dataset()
        g = ds.graph(URIRef(GRAPH_URI))
        good_g = good_ctx.sparql._g.graph(URIRef(GRAPH_URI))
        for s, p, o in good_g:
            if p == FM.ontologySource:
                g.add((s, p, Literal(stamped)))
            else:
                g.add((s, p, o))
        return ValidationContext(
            project_name=good_ctx.project_name,
            graph_uri=good_ctx.graph_uri,
            sparql=RdflibBackend(ds),
            gold_engine=good_ctx.gold_engine,
            mapping_report=good_ctx.mapping_report,
            ontology=good_ctx.ontology,
            baseline_run_id=BASELINE_RUN_ID,
            ontology_source=expected,
        )

    def test_fails_when_ontology_changed(self, good_ctx: ValidationContext) -> None:
        ctx = self._drift_ctx(good_ctx, stamped="old_ontology_v0.owl", expected=ONTOLOGY_SOURCE)
        result = LoadStampCheck().run(ctx)
        assert not result.passed

    def test_message_mentions_ontology_mismatch(self, good_ctx: ValidationContext) -> None:
        ctx = self._drift_ctx(good_ctx, stamped="old_ontology_v0.owl", expected=ONTOLOGY_SOURCE)
        result = LoadStampCheck().run(ctx)
        assert "ontologySource mismatch" in result.message

    def test_details_show_both_ontologies(self, good_ctx: ValidationContext) -> None:
        ctx = self._drift_ctx(good_ctx, stamped="old_ontology_v0.owl", expected=ONTOLOGY_SOURCE)
        result = LoadStampCheck().run(ctx)
        assert result.details["actual_ontology"] == "old_ontology_v0.owl"
        assert result.details["expected_ontology"] == ONTOLOGY_SOURCE

    def test_passes_when_ontology_source_not_set_in_ctx(self, good_ctx: ValidationContext) -> None:
        """If ctx.ontology_source is empty the check is skipped (backwards compat)."""
        import dataclasses
        ctx = dataclasses.replace(good_ctx, ontology_source="")
        result = LoadStampCheck().run(ctx)
        assert result.passed
