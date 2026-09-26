# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""_annotate_missing_comments — the generator's final guarantee that every class and
property carries an rdfs:comment, including classes minted by deterministic passes
(value vocabularies, terminology concept classes) that the LLM never described.

Comments are derived structurally from the graph; no dataset names in the logic.
"""
from __future__ import annotations

from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, URIRef
from rdflib.collection import Collection

from semantic_mapper.ontology.iterative.generator import _annotate_missing_comments

BASE = "http://mesh.local/ontology/test#"
FM = "http://forgemesh.ai/ontology#"


def _cls(g, local, comment=None):
    c = URIRef(BASE + local)
    g.add((c, RDF.type, OWL.Class))
    if comment:
        g.add((c, RDFS.comment, Literal(comment, lang="en")))
    return c


def _comment(g, node):
    comments = [str(c) for c in g.objects(node, RDFS.comment)]
    assert len(comments) == 1, comments
    return comments[0]


def test_existing_class_comment_untouched():
    g = Graph()
    c = _cls(g, "Event", "An event.")
    _annotate_missing_comments(g)
    assert _comment(g, c) == "An event."


def test_vocabulary_class_described_from_source_fk():
    g = Graph()
    vocab = _cls(g, "Status")
    ind = URIRef(BASE + "OpenStatus")
    g.add((ind, RDF.type, OWL.NamedIndividual))
    g.add((ind, RDF.type, vocab))
    eq, head = BNode(), BNode()
    Collection(g, head, [ind])
    g.add((eq, OWL.oneOf, head))
    g.add((vocab, OWL.equivalentClass, eq))
    p = URIRef(BASE + "hasStatus")
    g.add((p, RDF.type, OWL.ObjectProperty))
    g.add((p, RDFS.range, vocab))
    g.add((p, URIRef(FM + "sourceForeignKey"), Literal("raw_events.status -> EventStatus")))

    _annotate_missing_comments(g)
    text = _comment(g, vocab)
    assert text.startswith("Controlled vocabulary") and "raw_events.status" in text


def test_terminology_class_described_from_coded_properties():
    g = Graph()
    cls = _cls(g, "TestsysConcept")
    for i, col in enumerate(["code", "reason_code"]):
        p = URIRef(BASE + f"hasCode{i}")
        g.add((p, RDF.type, OWL.ObjectProperty))
        g.add((p, RDFS.range, cls))
        g.add((p, URIRef(FM + "sourceColumn"), Literal(f"raw_obs.{col}")))
        g.add((p, URIRef(FM + "terminologySystem"), Literal("TestSys")))

    _annotate_missing_comments(g)
    text = _comment(g, cls)
    assert "TestSys" in text and "raw_obs.code" in text and "raw_obs.reason_code" in text


def test_orphan_class_still_gets_a_comment():
    g = Graph()
    c = _cls(g, "LooseThing")
    _annotate_missing_comments(g)
    assert _comment(g, c)
