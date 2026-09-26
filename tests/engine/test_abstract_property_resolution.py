# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for abstract DatatypeProperty → class-scoped subproperty resolution.

Verifies that _resolve_to_specific_property() correctly follows rdfs:subPropertyOf
to return the class-scoped subproperty when an abstract property (no rdfs:domain)
has exactly one subproperty whose rdfs:domain matches the current table's class.
"""
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, Namespace, URIRef
from rdflib.namespace import XSD
from rdflib.namespace import SKOS as _SKOS

from semantic_mapper.engine.mapper import MapperOrchestrator

ONT = Namespace("http://test.org/ont#")
SKOS_ALT = URIRef("http://www.w3.org/2004/02/skos/core#altLabel")


def _make_graph() -> Graph:
    """
    Graph with:
      - hasSubjectId: abstract DatatypeProperty (no rdfs:domain)
      - hasAdmissionSubjectId: subPropertyOf hasSubjectId, domain=Admission
      - hasPatientSubjectId:   subPropertyOf hasSubjectId, domain=Patient
    """
    g = Graph()
    g.add((ONT.Admission, RDF.type, OWL.Class))
    g.add((ONT.Patient, RDF.type, OWL.Class))
    g.add((ONT.OtherClass, RDF.type, OWL.Class))

    # Abstract parent — no rdfs:domain
    g.add((ONT.hasSubjectId, RDF.type, OWL.DatatypeProperty))
    g.add((ONT.hasSubjectId, RDFS.range, XSD.integer))
    g.add((ONT.hasSubjectId, SKOS_ALT, Literal("subject_id", lang="en")))

    # Class-scoped subproperty for Admission
    g.add((ONT.hasAdmissionSubjectId, RDF.type, OWL.DatatypeProperty))
    g.add((ONT.hasAdmissionSubjectId, RDFS.domain, ONT.Admission))
    g.add((ONT.hasAdmissionSubjectId, RDFS.range, XSD.integer))
    g.add((ONT.hasAdmissionSubjectId, RDFS.subPropertyOf, ONT.hasSubjectId))

    # Class-scoped subproperty for Patient
    g.add((ONT.hasPatientSubjectId, RDF.type, OWL.DatatypeProperty))
    g.add((ONT.hasPatientSubjectId, RDFS.domain, ONT.Patient))
    g.add((ONT.hasPatientSubjectId, RDFS.range, XSD.integer))
    g.add((ONT.hasPatientSubjectId, RDFS.subPropertyOf, ONT.hasSubjectId))

    return g


def _make_orch(graph: Graph) -> MapperOrchestrator:
    """Create a MapperOrchestrator with only `graph` set (bypasses __init__)."""
    orch = object.__new__(MapperOrchestrator)
    orch.graph = graph
    return orch


class TestResolveToSpecificProperty:
    """_resolve_to_specific_property — abstract DatatypeProperty resolution."""

    def test_subject_id_in_admissions_context_resolves_to_class_scoped_subproperty(self):
        """
        Abstract hasSubjectId (skos:altLabel 'subject_id') with no rdfs:domain →
        resolves to hasAdmissionSubjectId when the table class is Admission.
        Verifies Bug 1 fix: AnnotationMatcher short-circuit on an abstract property
        is subsequently resolved to the correct class-scoped subproperty.
        """
        g = _make_graph()
        orch = _make_orch(g)
        result = orch._resolve_to_specific_property(
            str(ONT.hasSubjectId), str(ONT.Admission), "subject_id"
        )
        assert result == str(ONT.hasAdmissionSubjectId)

    def test_subject_id_in_patients_context_resolves_to_class_scoped_subproperty(self):
        """Abstract hasSubjectId → hasPatientSubjectId when the table class is Patient."""
        g = _make_graph()
        orch = _make_orch(g)
        result = orch._resolve_to_specific_property(
            str(ONT.hasSubjectId), str(ONT.Patient), "subject_id"
        )
        assert result == str(ONT.hasPatientSubjectId)

    def test_subject_id_with_no_matching_subproperty_returns_abstract(self):
        """
        When no class-scoped subproperty exists for the given domain (OtherClass),
        the abstract property is returned unchanged — correct fallback behaviour.
        """
        g = _make_graph()
        orch = _make_orch(g)
        result = orch._resolve_to_specific_property(
            str(ONT.hasSubjectId), str(ONT.OtherClass), "subject_id"
        )
        assert result == str(ONT.hasSubjectId)

    def test_prop_with_mismatched_domain_resolves_via_cross_type_fallback(self):
        """
        hasSubjectId with domain=Patient in an Admissions context (domain mismatch).
        The domain-mismatch branch searches for a property with domain=Admission
        and concept overlap → finds hasAdmissionSubjectId via the any_match fallback.
        """
        g = _make_graph()
        g.add((ONT.hasSubjectId, RDFS.domain, ONT.Patient))
        orch = _make_orch(g)
        result = orch._resolve_to_specific_property(
            str(ONT.hasSubjectId), str(ONT.Admission), "subject_id"
        )
        assert result == str(ONT.hasAdmissionSubjectId)

    def test_resolve_searches_both_datatype_and_object_subproperties(self):
        """
        _resolve_to_specific_property uses graph.subjects(RDFS.subPropertyOf, prop),
        which is type-agnostic.  Verify it finds an ObjectProperty subproperty too.
        """
        g = Graph()
        g.add((ONT.Admission, RDF.type, OWL.Class))
        g.add((ONT.Patient, RDF.type, OWL.Class))

        # Abstract ObjectProperty parent — no domain
        g.add((ONT.hasPatient, RDF.type, OWL.ObjectProperty))
        g.add((ONT.hasPatient, RDFS.range, ONT.Patient))

        # Two class-scoped ObjectProperty subproperties
        g.add((ONT.hasAdmissionPatient, RDF.type, OWL.ObjectProperty))
        g.add((ONT.hasAdmissionPatient, RDFS.domain, ONT.Admission))
        g.add((ONT.hasAdmissionPatient, RDFS.range, ONT.Patient))
        g.add((ONT.hasAdmissionPatient, RDFS.subPropertyOf, ONT.hasPatient))

        g.add((ONT.hasInputeventPatient, RDF.type, OWL.ObjectProperty))
        g.add((ONT.hasInputeventPatient, RDFS.domain, ONT.OtherClass))
        g.add((ONT.hasInputeventPatient, RDFS.range, ONT.Patient))
        g.add((ONT.hasInputeventPatient, RDFS.subPropertyOf, ONT.hasPatient))

        orch = _make_orch(g)
        result = orch._resolve_to_specific_property(
            str(ONT.hasPatient), str(ONT.Admission), "patient"
        )
        assert result == str(ONT.hasAdmissionPatient)
