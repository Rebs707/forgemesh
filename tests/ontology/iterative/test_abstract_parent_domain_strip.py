# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for _strip_domain_from_abstract_parents in graph_merger.

A property used as a shared parent by ≥2 subproperties cannot keep multiple plain
rdfs:domain triples (OWL reads them as conjunction → unsatisfiable across disjoint
classes). The function instead sets the parent's domain to the owl:unionOf of its
child subproperties' domains. When no child domain is determinable it falls back to
removing the parent's own domain. Properties with exactly 1 subproperty are untouched.
"""
from __future__ import annotations

import logging

import pytest
from rdflib import OWL, RDF, RDFS, Graph, URIRef
from rdflib.namespace import XSD

from semantic_mapper.ontology.iterative.graph_merger import _strip_domain_from_abstract_parents
from semantic_mapper.ontology.owl_utils import domain_class_uris

ONT = "http://test.org/ont#"


def _u(local: str) -> URIRef:
    return URIRef(ONT + local)


class TestStripDomainFromAbstractParents:
    def test_strips_domain_when_two_or_more_subproperties(self, caplog):
        """
        DatatypeProperty with rdfs:domain AND ≥2 subproperties (via rdfs:subPropertyOf):
        domain triple must be removed and a WARNING logged.
        """
        g = Graph()
        parent = _u("hasSubjectId")
        sub1 = _u("hasAdmissionSubjectId")
        sub2 = _u("hasPatientSubjectId")
        patient_cls = _u("Patient")

        g.add((parent, RDF.type, OWL.DatatypeProperty))
        g.add((parent, RDFS.domain, patient_cls))
        g.add((parent, RDFS.range, XSD.integer))
        g.add((sub1, RDFS.subPropertyOf, parent))
        g.add((sub2, RDFS.subPropertyOf, parent))

        with caplog.at_level(logging.WARNING, logger="semantic_mapper"):
            removed = _strip_domain_from_abstract_parents(g)

        assert removed == 1
        assert (parent, RDFS.domain, patient_cls) not in g
        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("hasSubjectId" in m for m in warning_messages)

    def test_retains_domain_when_exactly_one_subproperty(self, caplog):
        """
        DatatypeProperty with rdfs:domain AND exactly 1 subproperty:
        domain must be retained and a WARNING (not strip) logged.
        """
        g = Graph()
        parent = _u("hasSubjectId")
        sub1 = _u("hasAdmissionSubjectId")
        patient_cls = _u("Patient")

        g.add((parent, RDF.type, OWL.DatatypeProperty))
        g.add((parent, RDFS.domain, patient_cls))
        g.add((parent, RDFS.range, XSD.integer))
        g.add((sub1, RDFS.subPropertyOf, parent))

        with caplog.at_level(logging.WARNING, logger="semantic_mapper"):
            removed = _strip_domain_from_abstract_parents(g)

        assert removed == 0
        assert (parent, RDFS.domain, patient_cls) in g
        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("1 subproperty" in m for m in warning_messages)

    def test_no_change_when_property_has_no_subproperties(self):
        """Property with domain but no subproperties is untouched (non-abstract)."""
        g = Graph()
        prop = _u("hasFoo")
        cls = _u("Foo")
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        g.add((prop, RDFS.domain, cls))

        removed = _strip_domain_from_abstract_parents(g)

        assert removed == 0
        assert (prop, RDFS.domain, cls) in g

    def test_no_change_when_abstract_parent_already_has_no_domain(self):
        """Abstract parent with no domain — nothing to strip, returns 0."""
        g = Graph()
        parent = _u("hasSubjectId")
        sub1 = _u("hasAdmissionSubjectId")
        sub2 = _u("hasPatientSubjectId")

        g.add((parent, RDF.type, OWL.DatatypeProperty))
        # No domain triple — correct state
        g.add((sub1, RDFS.subPropertyOf, parent))
        g.add((sub2, RDFS.subPropertyOf, parent))

        removed = _strip_domain_from_abstract_parents(g)

        assert removed == 0

    def test_sets_union_domain_from_child_domains(self, caplog):
        """When the children carry domains, the abstract parent's erroneous plain
        domains are replaced by a single owl:unionOf of the child domains."""
        g = Graph()
        parent = _u("hasSubjectId")
        sub1 = _u("hasAdmissionSubjectId")
        sub2 = _u("hasPatientSubjectId")
        cls1 = _u("Patient")
        cls2 = _u("Admission")

        g.add((parent, RDF.type, OWL.DatatypeProperty))
        g.add((parent, RDFS.domain, cls1))  # erroneous direct domains (conjunction)
        g.add((parent, RDFS.domain, cls2))
        g.add((sub1, RDF.type, OWL.DatatypeProperty))
        g.add((sub1, RDFS.domain, cls2))
        g.add((sub1, RDFS.subPropertyOf, parent))
        g.add((sub2, RDF.type, OWL.DatatypeProperty))
        g.add((sub2, RDFS.domain, cls1))
        g.add((sub2, RDFS.subPropertyOf, parent))

        with caplog.at_level(logging.WARNING, logger="semantic_mapper"):
            changed = _strip_domain_from_abstract_parents(g)

        assert changed == 1
        # exactly ONE rdfs:domain triple now (a unionOf restriction), not two classes
        domains = list(g.objects(parent, RDFS.domain))
        assert len(domains) == 1
        assert domain_class_uris(g, parent) == {str(cls1), str(cls2)}
        # the original conjunction (two plain class domains) is gone
        assert (parent, RDFS.domain, cls1) not in g
        assert (parent, RDFS.domain, cls2) not in g

    def test_union_domain_rewrite_is_idempotent(self):
        """Re-running with the same children leaves the union domain unchanged."""
        g = Graph()
        parent = _u("hasSubjectId")
        sub1 = _u("hasAdmissionSubjectId")
        sub2 = _u("hasPatientSubjectId")
        cls1, cls2 = _u("Patient"), _u("Admission")
        g.add((parent, RDF.type, OWL.DatatypeProperty))
        g.add((sub1, RDFS.domain, cls2)); g.add((sub1, RDFS.subPropertyOf, parent))
        g.add((sub2, RDFS.domain, cls1)); g.add((sub2, RDFS.subPropertyOf, parent))

        first = _strip_domain_from_abstract_parents(g)
        triples_after_first = set(g)
        second = _strip_domain_from_abstract_parents(g)

        assert first == 1
        assert second == 0  # no change on the second pass
        assert set(g) == triples_after_first  # graph identical (no orphan bnodes)
        assert domain_class_uris(g, parent) == {str(cls1), str(cls2)}

    def test_self_referential_subprops_not_counted(self, caplog):
        """Self-referential rdfs:subPropertyOf (prop subPropertyOf itself) is ignored."""
        g = Graph()
        parent = _u("hasSubjectId")
        cls = _u("Patient")

        g.add((parent, RDF.type, OWL.DatatypeProperty))
        g.add((parent, RDFS.domain, cls))
        # Only a self-referential subPropertyOf — should NOT count towards the threshold
        g.add((parent, RDFS.subPropertyOf, parent))

        removed = _strip_domain_from_abstract_parents(g)

        assert removed == 0
        assert (parent, RDFS.domain, cls) in g

    def test_works_for_object_properties_too(self, caplog):
        """The same logic applies to ObjectProperty abstract parents."""
        g = Graph()
        parent = _u("hasPatient")
        sub1 = _u("hasAdmissionPatient")
        sub2 = _u("hasInputeventPatient")
        cls = _u("Admission")
        rng = _u("Patient")

        g.add((parent, RDF.type, OWL.ObjectProperty))
        g.add((parent, RDFS.domain, cls))  # wrong
        g.add((parent, RDFS.range, rng))
        g.add((sub1, RDFS.subPropertyOf, parent))
        g.add((sub2, RDFS.subPropertyOf, parent))

        with caplog.at_level(logging.WARNING, logger="semantic_mapper"):
            removed = _strip_domain_from_abstract_parents(g)

        assert removed == 1
        assert (parent, RDFS.domain, cls) not in g
