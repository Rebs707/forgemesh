# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Binder emission — TERM-2 (provenance), TERM-3 (floor), TERM-4 (coverage),
system-level best-effort, and the disabled no-op."""
from __future__ import annotations

from rdflib import Graph, OWL, RDF, RDFS, URIRef
from rdflib.namespace import SKOS

from semantic_mapper.ontology.terminology import bind_terminology
from semantic_mapper.ontology.terminology.binder import _system_class_uri

from .builders import (
    BASE,
    FM,
    build_datatype_graph,
    build_vocab_graph,
    make_profile,
    make_settings,
    offline_source_config,
)

_SYS = URIRef(FM + "terminologySystem")
_CONF = URIRef(FM + "terminologyBindingConfidence")
_METHOD = URIRef(FM + "terminologyBindingMethod")

_CODES = {"A1": "Alpha", "B2": "Beta", "C3": "Gamma"}


def _settings(overlap=0.7, floor=0.5):
    return make_settings(
        sources=offline_source_config(codes=_CODES, system_iri="http://example.org/system"),
        overlap=overlap, floor=floor,
    )


class TestProvenanceTerm2:
    def test_every_exactmatch_carries_exactly_one_system_and_confidence(self) -> None:
        g, cls, inds = build_vocab_graph("raw_events", "status_code", "StatusCode",
                                         ["A1", "B2", "C3"])
        result = bind_terminology(g, [make_profile("raw_events", "status_code",
                                                   ["A1", "B2", "C3"])],
                                  BASE, _settings(floor=0.5))
        assert result.n_asserted == 3
        bound = list(g.subjects(SKOS.exactMatch, None))
        assert len(bound) == 3
        for s in bound:
            assert len(list(g.objects(s, _SYS))) == 1
            assert len(list(g.objects(s, _CONF))) == 1
            assert len(list(g.objects(s, _METHOD))) == 1
            assert str(next(g.objects(s, _SYS))) == "TestSys"

    def test_concept_iri_is_minted(self) -> None:
        g, cls, inds = build_vocab_graph("raw_events", "status_code", "StatusCode", ["A1"])
        # need ≥2 distinct for detection
        g2, _, inds2 = build_vocab_graph("raw_events", "status_code", "StatusCode",
                                         ["A1", "B2"])
        bind_terminology(g2, [make_profile("raw_events", "status_code", ["A1", "B2"])],
                         BASE, _settings(floor=0.5))
        objs = {str(o) for o in g2.objects(inds2["A1"], SKOS.exactMatch)}
        assert objs == {"http://example.org/concept/A1"}

    def test_no_exactmatch_without_provenance(self) -> None:
        g, cls, inds = build_vocab_graph("raw_events", "status_code", "StatusCode",
                                         ["A1", "B2", "C3"])
        bind_terminology(g, [make_profile("raw_events", "status_code", ["A1", "B2", "C3"])],
                         BASE, _settings(floor=0.5))
        for s in g.subjects(SKOS.exactMatch, None):
            assert list(g.objects(s, _SYS)) and list(g.objects(s, _CONF))


class TestFloorTerm3:
    def test_sub_floor_goes_to_sidecar_not_graph(self) -> None:
        # overlap 0.75 (3/4), floor 0.9 → nothing asserted; candidates in sidecar.
        g, cls, inds = build_vocab_graph("raw_events", "status_code", "StatusCode",
                                         ["A1", "B2", "C3", "Z9"])
        result = bind_terminology(g, [make_profile("raw_events", "status_code",
                                                   ["A1", "B2", "C3", "Z9"])],
                                  BASE, _settings(overlap=0.7, floor=0.9))
        assert result.n_asserted == 0
        assert result.n_sidecar == 3
        assert list(g.subjects(SKOS.exactMatch, None)) == []   # nothing in asserted graph

    def test_above_floor_is_asserted(self) -> None:
        g, cls, inds = build_vocab_graph("raw_events", "status_code", "StatusCode",
                                         ["A1", "B2", "C3", "Z9"])
        result = bind_terminology(g, [make_profile("raw_events", "status_code",
                                                   ["A1", "B2", "C3", "Z9"])],
                                  BASE, _settings(overlap=0.7, floor=0.5))
        assert result.n_asserted == 3
        assert result.n_sidecar == 0


class TestCoverageTerm4:
    def test_every_value_bound_or_reported(self) -> None:
        values = ["A1", "B2", "C3", "Z9"]
        g, cls, inds = build_vocab_graph("raw_events", "status_code", "StatusCode", values)
        result = bind_terminology(g, [make_profile("raw_events", "status_code", values)],
                                  BASE, _settings(overlap=0.7, floor=0.5))
        accounted = (
            {b.raw_value for b in result.asserted}
            | {b.raw_value for b in result.sidecar}
            | {u.value for u in result.unmapped}
        )
        assert set(values) <= accounted
        # Z9 is the unmatched value, reported not dropped.
        assert any(u.value == "Z9" and u.reason == "not-in-source" for u in result.unmapped)

    def test_value_without_individual_reported(self) -> None:
        # Graph minted individuals for A1,B2 only; profile also has C3.
        g, cls, inds = build_vocab_graph("raw_events", "status_code", "StatusCode",
                                         ["A1", "B2"])
        result = bind_terminology(g, [make_profile("raw_events", "status_code",
                                                   ["A1", "B2", "C3"])],
                                  BASE, _settings(overlap=0.6, floor=0.5))
        assert any(u.value == "C3" and u.reason == "no-individual" for u in result.unmapped)


class TestCodedObjectProperty:
    """Option (b): a detected high-cardinality column is re-typed to a terminology-
    coded object property; its code individuals are minted at materialisation, not here."""

    def test_high_card_property_retyped_to_coded_object_property(self) -> None:
        g, prop = build_datatype_graph("raw_obs", "code", "hasCode")
        result = bind_terminology(g, [make_profile("raw_obs", "code", ["A1", "B2", "C3"])],
                                  BASE, _settings(overlap=0.8, floor=0.5))
        # No individual-level bindings here — codes are minted at materialisation.
        assert result.n_asserted == 0
        # DatatypeProperty → ObjectProperty, range = the conformed code class.
        assert (prop, RDF.type, OWL.ObjectProperty) in g
        assert (prop, RDF.type, OWL.DatatypeProperty) not in g
        cls = _system_class_uri(BASE, "testsys")
        assert (prop, RDFS.range, cls) in g
        assert (cls, RDF.type, OWL.Class) in g
        # Property flagged with the recipe the materialiser reads.
        assert str(next(g.objects(prop, URIRef(FM + "terminologySystem")))) == "TestSys"
        # Recorded as a coded property.
        assert len(result.coded_properties) == 1
        cp = result.coded_properties[0]
        assert cp.property_uri == str(prop) and cp.system_id == "testsys"

    def test_code_class_gets_system_close_match(self) -> None:
        g, prop = build_datatype_graph("raw_obs", "code", "hasCode")
        bind_terminology(g, [make_profile("raw_obs", "code", ["A1", "B2", "C3"])],
                         BASE, _settings(overlap=0.8, floor=0.5))
        cls = _system_class_uri(BASE, "testsys")
        assert (cls, SKOS.closeMatch, URIRef("http://example.org/system")) in g

    def test_code_class_gets_rdfs_comment(self) -> None:
        g, prop = build_datatype_graph("raw_obs", "code", "hasCode")
        bind_terminology(g, [make_profile("raw_obs", "code", ["A1", "B2", "C3"])],
                         BASE, _settings(overlap=0.8, floor=0.5))
        cls = _system_class_uri(BASE, "testsys")
        comments = [str(c) for c in g.objects(cls, RDFS.comment)]
        assert len(comments) == 1 and "TestSys" in comments[0]

    def test_detected_column_with_no_property_reported(self) -> None:
        # Detection fires but the ontology has no property for the column → coverage gap.
        g = Graph()
        result = bind_terminology(g, [make_profile("raw_obs", "code", ["A1", "B2", "C3"])],
                                  BASE, _settings(overlap=0.8, floor=0.5))
        assert result.coded_properties == []
        assert {u.reason for u in result.unmapped} == {"no-property"}

    def test_bound_vocab_class_gets_system_close_match(self) -> None:
        g, cls, inds = build_vocab_graph("raw_events", "status_code", "StatusCode",
                                         ["A1", "B2", "C3"])
        bind_terminology(g, [make_profile("raw_events", "status_code", ["A1", "B2", "C3"])],
                         BASE, _settings(floor=0.5))
        assert (cls, SKOS.closeMatch, URIRef("http://example.org/system")) in g


class TestDisabled:
    def test_disabled_is_noop(self) -> None:
        g, cls, inds = build_vocab_graph("raw_events", "status_code", "StatusCode",
                                         ["A1", "B2", "C3"])
        before = len(g)
        result = bind_terminology(g, [make_profile("raw_events", "status_code",
                                                   ["A1", "B2", "C3"])],
                                  BASE, make_settings(enabled=False))
        assert result.n_asserted == 0
        assert len(g) == before
        assert list(g.subjects(SKOS.exactMatch, None)) == []
