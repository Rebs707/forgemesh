# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the self-referential rdfs:subPropertyOf guard in graph_merger.

Covers:
  - Fragment containing a self-referential subPropertyOf → triple absent from
    accumulating graph and a WARNING was logged.
  - Self-referential triple already in the accumulating graph → post-merge scan
    strips it and logs a WARNING.
  - Per-triple _add_triple guard rejects self-refs and logs WARNING.
  - Valid subPropertyOf (subject != object) is preserved unchanged.
"""
import logging

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef

from semantic_mapper.ontology.iterative.graph_merger import (
    _add_triple,
    _scan_and_strip_self_ref_subprops,
    _strip_self_referential_subprops,
    merge,
)

BASE = "http://mesh.local/ontology/test#"


def _make_fragment_with_class_and_self_ref_prop(prop_uri: str) -> Graph:
    """A minimal fragment: one class, one property self-referencing itself via subPropertyOf."""
    g = Graph()
    cls = URIRef(f"{BASE}MyClass")
    prop = URIRef(prop_uri)
    g.add((cls, RDF.type, OWL.Class))
    g.add((cls, RDFS.label, Literal("MyClass", lang="en")))
    g.add((prop, RDF.type, OWL.DatatypeProperty))
    g.add((prop, RDFS.domain, cls))
    g.add((prop, RDFS.range, URIRef("http://www.w3.org/2001/XMLSchema#string")))
    g.add((prop, RDFS.label, Literal("has x", lang="en")))
    g.add((prop, RDFS.subPropertyOf, prop))   # self-referential — invalid OWL
    return g


# ---------------------------------------------------------------------------
# Fragment-level: self-ref stripped before entering accumulating, WARNING logged
# ---------------------------------------------------------------------------

class TestFragmentSelfRefStripped:
    def test_self_ref_triple_absent_after_merge(self):
        prop = URIRef(f"{BASE}hasDbSource")
        frag = _make_fragment_with_class_and_self_ref_prop(str(prop))
        acc = Graph()

        merge(acc, frag, "my_table", BASE)

        assert (prop, RDFS.subPropertyOf, prop) not in acc

    def test_warning_logged_for_fragment_self_ref(self, caplog):
        prop = URIRef(f"{BASE}hasInsurance")
        frag = _make_fragment_with_class_and_self_ref_prop(str(prop))
        acc = Graph()

        with caplog.at_level(logging.WARNING, logger="semantic_mapper.ontology.iterative.graph_merger"):
            merge(acc, frag, "my_table", BASE)

        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("hasInsurance" in m and "subPropertyOf" in m for m in warning_messages)

    def test_warning_message_contains_property_name(self, caplog):
        prop = URIRef(f"{BASE}hasDbSource")
        frag = _make_fragment_with_class_and_self_ref_prop(str(prop))
        acc = Graph()

        with caplog.at_level(logging.WARNING, logger="semantic_mapper.ontology.iterative.graph_merger"):
            merge(acc, frag, "my_table", BASE)

        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("hasDbSource" in m for m in warning_messages)

    def test_valid_subpropertyof_preserved(self):
        """A child → parent subPropertyOf where subject != object must survive."""
        g = Graph()
        parent = URIRef(f"{BASE}hasValue")
        child = URIRef(f"{BASE}hasLabeventValue")
        cls = URIRef(f"{BASE}Labevent")
        g.add((cls, RDF.type, OWL.Class))
        g.add((parent, RDF.type, OWL.DatatypeProperty))
        g.add((parent, RDFS.range, URIRef("http://www.w3.org/2001/XMLSchema#string")))
        g.add((child, RDF.type, OWL.DatatypeProperty))
        g.add((child, RDFS.domain, cls))
        g.add((child, RDFS.range, URIRef("http://www.w3.org/2001/XMLSchema#string")))
        g.add((child, RDFS.subPropertyOf, parent))   # valid — different subject and object

        acc = Graph()
        merge(acc, g, "labevent", BASE)

        assert (child, RDFS.subPropertyOf, parent) in acc


# ---------------------------------------------------------------------------
# Post-merge scan: self-ref already in accumulating → stripped with WARNING
# ---------------------------------------------------------------------------

class TestPostMergeScan:
    def test_self_ref_in_accumulating_stripped(self):
        prop = URIRef(f"{BASE}hasDbSource")
        acc = Graph()
        # Insert self-referential triple directly into accumulating (simulates prior run)
        acc.add((prop, RDF.type, OWL.DatatypeProperty))
        acc.add((prop, RDFS.subPropertyOf, prop))

        # Merge any valid fragment to trigger the post-merge scan
        frag = Graph()
        cls = URIRef(f"{BASE}AnotherClass")
        frag.add((cls, RDF.type, OWL.Class))
        frag.add((cls, RDFS.label, Literal("AnotherClass", lang="en")))

        merge(acc, frag, "another_table", BASE)

        assert (prop, RDFS.subPropertyOf, prop) not in acc

    def test_warning_logged_for_accumulating_self_ref(self, caplog):
        prop = URIRef(f"{BASE}hasInsurance")
        acc = Graph()
        acc.add((prop, RDF.type, OWL.DatatypeProperty))
        acc.add((prop, RDFS.subPropertyOf, prop))

        frag = Graph()
        cls = URIRef(f"{BASE}AnotherClass")
        frag.add((cls, RDF.type, OWL.Class))
        frag.add((cls, RDFS.label, Literal("AnotherClass", lang="en")))

        with caplog.at_level(logging.WARNING, logger="semantic_mapper.ontology.iterative.graph_merger"):
            merge(acc, frag, "another_table", BASE)

        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("hasInsurance" in m and "subPropertyOf" in m for m in warning_messages)

    def test_scan_and_strip_directly(self):
        """Unit test _scan_and_strip_self_ref_subprops in isolation."""
        prop = URIRef(f"{BASE}hasSelf")
        g = Graph()
        g.add((prop, RDFS.subPropertyOf, prop))

        count = _scan_and_strip_self_ref_subprops(g)

        assert count == 1
        assert (prop, RDFS.subPropertyOf, prop) not in g

    def test_scan_preserves_valid_subpropertyof(self):
        parent = URIRef(f"{BASE}hasValue")
        child = URIRef(f"{BASE}hasLabeventValue")
        g = Graph()
        g.add((child, RDFS.subPropertyOf, parent))

        count = _scan_and_strip_self_ref_subprops(g)

        assert count == 0
        assert (child, RDFS.subPropertyOf, parent) in g


# ---------------------------------------------------------------------------
# Per-triple guard: _add_triple rejects self-refs, accepts valid triples
# ---------------------------------------------------------------------------

class TestAddTripleGuard:
    def test_self_ref_not_added(self):
        prop = URIRef(f"{BASE}hasX")
        g = Graph()

        _add_triple(g, (prop, RDFS.subPropertyOf, prop))

        assert (prop, RDFS.subPropertyOf, prop) not in g

    def test_self_ref_logs_warning(self, caplog):
        prop = URIRef(f"{BASE}hasX")
        g = Graph()

        with caplog.at_level(logging.WARNING, logger="semantic_mapper.ontology.iterative.graph_merger"):
            _add_triple(g, (prop, RDFS.subPropertyOf, prop))

        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("hasX" in m and "subPropertyOf" in m for m in warning_messages)

    def test_valid_triple_is_added(self):
        prop = URIRef(f"{BASE}hasX")
        g = Graph()

        _add_triple(g, (prop, RDF.type, OWL.DatatypeProperty))

        assert (prop, RDF.type, OWL.DatatypeProperty) in g

    def test_valid_subpropertyof_is_added(self):
        parent = URIRef(f"{BASE}hasValue")
        child = URIRef(f"{BASE}hasMyValue")
        g = Graph()

        _add_triple(g, (child, RDFS.subPropertyOf, parent))

        assert (child, RDFS.subPropertyOf, parent) in g
