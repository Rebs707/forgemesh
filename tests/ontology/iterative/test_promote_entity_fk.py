# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for _promote_entity_fk_properties — the deterministic entity-FK promotion pass.

The coded+name entanglement (a `venue` name column and a `venue_code` FK to a Venue
table collapsed by the gen-LLM into one DatatypeProperty sourced from the name) is
re-typed to ObjectProperty(range=Venue) and re-sourced to the coded key column so it
resolves to the entity.  Generic — no dataset names in the logic under test.
"""
from __future__ import annotations

from rdflib import Graph, Literal, Namespace, OWL, RDF, RDFS, URIRef
from rdflib.namespace import SKOS, XSD

from semantic_mapper.db.models import ColumnProfile, TableProfile
from semantic_mapper.ontology.iterative.context_builder import derive_class_uri
from semantic_mapper.ontology.iterative.generator import _promote_entity_fk_properties

BASE = "http://mesh.local/ontology/test#"
FM = "http://forgemesh.ai/ontology#"
_SRC = URIRef(FM + "sourceColumn")
_FK = URIRef(FM + "sourceForeignKey")


def _col(name, pk=False):
    return ColumnProfile(name=name, inferred_type="VARCHAR(8)", is_nullable=False,
                         is_primary_key=pk, null_rate=0.0, distinct_count=15)


def _profiles():
    sched = TableProfile(
        table_name="raw_schedules", row_count=1884,
        columns={"venue": _col("venue"), "venue_code": _col("venue_code")},
        foreign_keys=[{"column": "venue_code", "ref_table": "raw_venues"}],
    )
    venues = TableProfile(
        table_name="raw_venues", row_count=15,
        columns={"venue_code": _col("venue_code", pk=True), "venue": _col("venue")},
        foreign_keys=[],
    )
    return [sched, venues]


def _base_graph():
    g = Graph()
    g.add((URIRef(derive_class_uri("raw_schedules", BASE)), RDF.type, OWL.Class))
    g.add((URIRef(derive_class_uri("raw_venues", BASE)), RDF.type, OWL.Class))
    return g


def _entangled_property(g):
    """hasScheduleVenue: DatatypeProperty sourced from the NAME, carrying the venue_code FK."""
    p = URIRef(BASE + "hasScheduleVenue")
    g.add((p, RDF.type, OWL.DatatypeProperty))
    g.add((p, RDFS.domain, URIRef(derive_class_uri("raw_schedules", BASE))))
    g.add((p, RDFS.range, XSD.string))
    g.add((p, _FK, Literal("raw_schedules.venue_code -> raw_venues")))
    g.add((p, _SRC, Literal("raw_schedules.venue")))
    g.add((p, SKOS.altLabel, Literal("venue", lang="en")))
    return p


class TestPromoteEntityFk:
    def test_promotes_and_resources_entangled_fk(self):
        g = _base_graph()
        p = _entangled_property(g)
        venue_cls = URIRef(derive_class_uri("raw_venues", BASE))

        fixed = _promote_entity_fk_properties(g, _profiles(), BASE)

        assert fixed == 1
        assert (p, RDF.type, OWL.ObjectProperty) in g
        assert (p, RDF.type, OWL.DatatypeProperty) not in g
        assert list(g.objects(p, RDFS.range)) == [venue_cls]
        # fm:sourceColumn is stripped — an FK ObjectProperty is bound via fm:sourceForeignKey
        # only; keeping it triggers the FK/literal coherence collision (→ PENDING).
        assert list(g.objects(p, _SRC)) == []
        assert str(next(g.objects(p, _FK))) == "raw_schedules.venue_code -> raw_venues"
        assert str(next(g.objects(p, SKOS.altLabel))) == "venue_code"
        # Domain preserved.
        assert URIRef(derive_class_uri("raw_schedules", BASE)) in list(g.objects(p, RDFS.domain))

    def test_strips_datatype_parent_subproperty(self):
        g = _base_graph()
        p = _entangled_property(g)
        parent = URIRef(BASE + "hasVenue")
        g.add((parent, RDF.type, OWL.DatatypeProperty))
        g.add((p, RDFS.subPropertyOf, parent))

        _promote_entity_fk_properties(g, _profiles(), BASE)
        # ObjectProperty must not remain subPropertyOf a DatatypeProperty parent.
        assert (p, RDFS.subPropertyOf, parent) not in g

    def test_idempotent_on_correct_object_property(self):
        g = _base_graph()
        p = URIRef(BASE + "hasScheduleVenue")
        venue_cls = URIRef(derive_class_uri("raw_venues", BASE))
        g.add((p, RDF.type, OWL.ObjectProperty))
        g.add((p, RDFS.domain, URIRef(derive_class_uri("raw_schedules", BASE))))
        g.add((p, RDFS.range, venue_cls))
        g.add((p, _FK, Literal("raw_schedules.venue_code -> raw_venues")))
        g.add((p, _SRC, Literal("raw_schedules.venue_code")))
        g.add((p, SKOS.altLabel, Literal("venue_code", lang="en")))

        fixed = _promote_entity_fk_properties(g, _profiles(), BASE)
        assert fixed == 0
        assert list(g.objects(p, RDFS.range)) == [venue_cls]

    def test_skips_fk_whose_ref_is_not_a_class(self):
        # No class declared for the referenced table → the FK is a value-mapped categorical
        # the pass must leave untouched.
        g = Graph()
        g.add((URIRef(derive_class_uri("raw_schedules", BASE)), RDF.type, OWL.Class))
        p = _entangled_property(g)  # venue_code -> raw_venues, but no Venue class declared
        fixed = _promote_entity_fk_properties(g, _profiles(), BASE)
        assert fixed == 0
        assert (p, RDF.type, OWL.DatatypeProperty) in g
