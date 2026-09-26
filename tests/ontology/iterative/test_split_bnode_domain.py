# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Regression: _split_multi_domain_dt_properties must not leak a blank-node domain
id into a scoped subproperty name (e.g. `hasN4762a4…Country`).

A datatype property can end up with both a concrete URIRef domain and a blank-node
(union/restriction) domain. Splitting per-domain must scope only the concrete
domains; the blank node cannot name a class-scoped subproperty.
"""
from __future__ import annotations

from rdflib import Graph, URIRef, BNode, OWL, RDF, RDFS, Literal, XSD
from rdflib.namespace import SKOS

from semantic_mapper.ontology.iterative.generator import _split_multi_domain_dt_properties

EX = "http://mesh.local/ontology/test#"


def _local(u):
    return str(u).split("#")[-1]


def test_blank_node_domain_not_leaked_into_property_name():
    g = Graph()
    prop = URIRef(EX + "hasMedallistCountry")
    g.add((prop, RDF.type, OWL.DatatypeProperty))
    g.add((prop, RDFS.range, XSD.string))
    g.add((prop, SKOS.altLabel, Literal("country")))
    g.add((prop, RDFS.domain, URIRef(EX + "Medallist")))   # concrete domain
    bn = BNode()
    g.add((bn, RDF.type, OWL.Class))                        # a union/restriction class
    g.add((prop, RDFS.domain, bn))                          # blank-node domain

    _split_multi_domain_dt_properties(g, EX)

    names = [_local(s) for s in g.subjects(RDF.type, OWL.DatatypeProperty)]
    # No property name may contain the blank-node id.
    leaked = [nm for nm in names if str(bn) in nm]
    assert leaked == [], f"blank-node id leaked into property names: {leaked}"
    # The concrete domain still yields a proper scoped property under the abstract parent.
    assert "hasMedallistCountry" in names
    assert "hasCountry" in names
    assert (URIRef(EX + "hasMedallistCountry"), RDFS.subPropertyOf, URIRef(EX + "hasCountry")) in g


def test_only_concrete_domains_produce_scoped_subproperties():
    g = Graph()
    prop = URIRef(EX + "hasStatus")
    g.add((prop, RDF.type, OWL.DatatypeProperty))
    g.add((prop, RDFS.range, XSD.string))
    g.add((prop, SKOS.altLabel, Literal("status")))
    g.add((prop, RDFS.domain, URIRef(EX + "Schedule")))
    g.add((prop, RDFS.domain, URIRef(EX + "Event")))
    bn = BNode()
    g.add((prop, RDFS.domain, bn))

    _split_multi_domain_dt_properties(g, EX)
    names = {_local(s) for s in g.subjects(RDF.type, OWL.DatatypeProperty)}
    assert "hasScheduleStatus" in names
    assert "hasEventStatus" in names
    assert not any(str(bn) in nm for nm in names)
