# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""C10 TerminologyBindingCheck — provenance + floor integrity over the graph."""
from __future__ import annotations

from types import SimpleNamespace

from rdflib import Graph, Literal, URIRef, XSD
from rdflib.namespace import SKOS

from semantic_mapper.validation.checks import TerminologyBindingCheck
from semantic_mapper.validation.context import ValidationContext

from .builders import FM

_SYS = URIRef(FM + "terminologySystem")
_CONF = URIRef(FM + "terminologyBindingConfidence")
_METHOD = URIRef(FM + "terminologyBindingMethod")
_IND = URIRef("http://mesh.local/ontology/testds#ind1")
_CONCEPT = URIRef("http://example.org/concept/A1")


def _ctx(graph: Graph, floor: float = 0.5) -> ValidationContext:
    return ValidationContext(
        project_name="testds",
        graph_uri="http://mesh.local/graph/testds",
        sparql=None,
        gold_engine=None,
        mapping_report=None,
        ontology=graph,
        baseline_run_id="run-1",
        settings=SimpleNamespace(TERM_BIND_CONFIDENCE_FLOOR=floor),
    )


def _good_binding(g: Graph, conf: float = 0.9) -> None:
    g.add((_IND, SKOS.exactMatch, _CONCEPT))
    g.add((_IND, _SYS, Literal("TestSys")))
    g.add((_IND, _CONF, Literal(conf, datatype=XSD.decimal)))
    g.add((_IND, _METHOD, Literal("value-set-overlap")))


class TestTerminologyBindingCheck:
    def test_no_bindings_is_skipped_pass(self) -> None:
        result = TerminologyBindingCheck().run(_ctx(Graph()))
        assert result.passed
        assert "no external terminology bindings" in result.message

    def test_well_formed_binding_passes(self) -> None:
        g = Graph()
        _good_binding(g)
        result = TerminologyBindingCheck().run(_ctx(g))
        assert result.passed, result.message
        assert result.details["bound_subjects"] == 1

    def test_missing_provenance_fails_tb1(self) -> None:
        g = Graph()
        g.add((_IND, SKOS.exactMatch, _CONCEPT))   # no system/confidence
        result = TerminologyBindingCheck().run(_ctx(g))
        assert not result.passed
        assert "TB-1" in result.message

    def test_duplicate_provenance_fails_tb1_cardinality(self) -> None:
        g = Graph()
        _good_binding(g)
        g.add((_IND, _SYS, Literal("OtherSys")))   # two systems
        result = TerminologyBindingCheck().run(_ctx(g))
        assert not result.passed
        assert "cardinality" in result.message

    def test_sub_floor_confidence_fails_tb2(self) -> None:
        g = Graph()
        _good_binding(g, conf=0.30)
        result = TerminologyBindingCheck().run(_ctx(g, floor=0.5))
        assert not result.passed
        assert "TB-2" in result.message

    def test_at_floor_passes(self) -> None:
        g = Graph()
        _good_binding(g, conf=0.50)
        result = TerminologyBindingCheck().run(_ctx(g, floor=0.5))
        assert result.passed, result.message

    def test_no_ontology_is_skipped(self) -> None:
        ctx = _ctx(Graph())
        ctx.ontology = None
        result = TerminologyBindingCheck().run(ctx)
        assert result.passed
        assert "no ontology" in result.message
