# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""The dangling-link gate's SPARQL, run against an in-memory rdflib Dataset."""
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, Dataset, Literal, Namespace, URIRef
from rdflib.namespace import SKOS

from semantic_mapper.rdf.dangling_gate import (
    EXTERNAL_REFERENCE_PREDICATES,
    _by_predicate_query,
    _count_query,
)

ONT = Namespace("http://ex.org/ontology#")
DATA = Namespace("http://ex.org/data/")
EXT = Namespace("http://external.example/concept/")
DATA_GRAPH = URIRef("http://ex.org/graph/data")
ONT_GRAPH = URIRef("http://ex.org/graph/ontology")


def _count(ds: Dataset) -> int:
    return int(next(iter(ds.query(_count_query(str(DATA_GRAPH)))))[0])


@pytest.fixture
def ds() -> Dataset:
    ds = Dataset()
    data = ds.graph(DATA_GRAPH)
    ont = ds.graph(ONT_GRAPH)
    # Resolvable in the data graph.
    data.add((DATA.event1, RDF.type, ONT.Event))
    data.add((DATA.person1, RDF.type, ONT.Person))
    data.add((DATA.event1, ONT.hasPerson, DATA.person1))
    # Resolvable via a value individual typed only in the ontology graph.
    ont.add((ONT.HighStatus, RDF.type, ONT.Status))
    data.add((DATA.event1, ONT.hasStatus, ONT.HighStatus))
    data.add((DATA.event1, RDFS.label, Literal("not an IRI")))
    return ds


def test_resolvable_links_pass(ds):
    assert _count(ds) == 0


def test_link_to_untyped_local_target_is_dangling(ds):
    ds.graph(DATA_GRAPH).add((DATA.event1, ONT.hasPerson, DATA.ghost))
    assert _count(ds) == 1
    rows = list(ds.query(_by_predicate_query(str(DATA_GRAPH))))
    assert [(str(p), int(n)) for p, n in rows] == [(str(ONT.hasPerson), 1)]


@pytest.mark.parametrize("pred", EXTERNAL_REFERENCE_PREDICATES)
def test_external_reference_predicates_are_exempt(ds, pred):
    # e.g. a code individual bound to an external terminology concept: the target
    # is deliberately not typed anywhere in this store.
    data = ds.graph(DATA_GRAPH)
    data.add((ONT.code123Concept, RDF.type, ONT.CodeConcept))
    data.add((ONT.code123Concept, URIRef(pred), EXT["123"]))
    assert _count(ds) == 0


def test_exemption_list_covers_standard_mapping_predicates():
    assert str(SKOS.exactMatch) in EXTERNAL_REFERENCE_PREDICATES
    assert str(OWL.sameAs) in EXTERNAL_REFERENCE_PREDICATES
