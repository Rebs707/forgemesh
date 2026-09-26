# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for C9 — entity-linking integrity (value-overlap FK resolution).

Uses the airport/airline/route fixtures: gold_route.airline_id (text, {AA, BB})
is a value-overlap reference to gold_airline.id (text key {AA, BB}).
"""
from __future__ import annotations

from rdflib import OWL, RDF, RDFS

from semantic_mapper.schema.mapping_output import (
    MappingStatus, PropertyMapping, TableMapping, ValueMappingRule,
)
from semantic_mapper.validation.checks.entity_linking import EntityLinkingCheck
from semantic_mapper.validation.context import ValidationContext

from .conftest import ONT


def _ctx_with(good_ctx, *, report=None, ontology=None) -> ValidationContext:
    return ValidationContext(
        project_name=good_ctx.project_name,
        graph_uri=good_ctx.graph_uri,
        sparql=good_ctx.sparql,
        gold_engine=good_ctx.gold_engine,
        mapping_report=report or good_ctx.mapping_report,
        ontology=ontology or good_ctx.ontology,
        baseline_run_id=good_ctx.baseline_run_id,
    )


class TestC9Pass:
    def test_passes_on_known_good_graph(self, good_ctx) -> None:
        result = EntityLinkingCheck().run(good_ctx)
        assert result.passed, result.message
        assert result.check_id == "C9"

    def test_detects_the_airline_reference(self, good_ctx) -> None:
        # route.airline_id ⊆ airline.id (text key) → at least one reference resolved.
        result = EntityLinkingCheck().run(good_ctx)
        assert result.details["references_detected"] >= 1

    def test_skips_without_ontology(self, good_ctx) -> None:
        ctx = ValidationContext(
            project_name=good_ctx.project_name, graph_uri=good_ctx.graph_uri,
            sparql=good_ctx.sparql, gold_engine=good_ctx.gold_engine,
            mapping_report=good_ctx.mapping_report, ontology=None,
            baseline_run_id=good_ctx.baseline_run_id,
        )
        result = EntityLinkingCheck().run(ctx)
        assert result.passed and "skipped" in result.message


def _report_with_airline_value_map(report):
    """Rebuild the report so raw_route.airline_id carries a value-map (discriminated)."""
    new_tables = []
    for tm in report.tables:
        if tm.source_table != "raw_route":
            new_tables.append(tm)
            continue
        new_cols = []
        for pm in tm.column_mappings:
            if pm.source_column == "airline_id":
                pm = pm.model_copy(update={"value_mappings": [
                    ValueMappingRule(source_value="AA", mapped_uri=str(ONT.AirlineAA),
                                     rationale="t", confidence_score=0.9),
                    ValueMappingRule(source_value="BB", mapped_uri=str(ONT.AirlineBB),
                                     rationale="t", confidence_score=0.9),
                ]})
            new_cols.append(pm)
        new_tables.append(tm.model_copy(update={"column_mappings": new_cols}))
    return report.model_copy(update={"tables": new_tables})


class TestC9FK3Discrimination:
    def test_fails_when_reference_column_is_discriminated(self, good_ctx) -> None:
        report = _report_with_airline_value_map(good_ctx.mapping_report)
        result = EntityLinkingCheck().run(_ctx_with(good_ctx, report=report))
        assert not result.passed
        assert "FK-3" in result.message
        assert any("airline_id" in d for d in result.details["fk3_discriminated"])


class TestC9FK2Orphan:
    def test_fails_when_referenced_entity_is_orphan(self, good_ctx) -> None:
        # Strip every object-property range pointing at Airline → it becomes an orphan
        # that route.airline_id still references by value.
        g = good_ctx.ontology
        ont = type(g)()
        for t in g:
            ont.add(t)
        for prop in list(ont.subjects(RDF.type, OWL.ObjectProperty)):
            for rng in list(ont.objects(prop, RDFS.range)):
                if rng == ONT.Airline:
                    ont.remove((prop, RDFS.range, rng))
        result = EntityLinkingCheck().run(_ctx_with(good_ctx, ontology=ont))
        assert not result.passed
        assert "FK-2" in result.message
        assert "Airline" in result.message
