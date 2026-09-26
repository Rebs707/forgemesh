# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for _resolve_to_specific_property() Phase 2 (compound column fallback).

Phase 2 fires when:
  - Phase 1 yields 0 or >1 candidates (subproperty walk did not resolve uniquely)
  - A common suffix was stripped from the column name
  - The stripped suffix aligns with the abstract property's concept

Real failing cases that motivated the fix:
  - "ordercategorydescription" → hasDescription  →  hasInputeventsMvOrderCategoryDescription
  - "statusdescription"        → hasDescription  →  hasInputeventsMvStatusDescription
  - "totalamountuom"           → hasAmountUom    →  hasInputeventsMvTotalAmountUom
"""
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Namespace, URIRef
from rdflib.namespace import XSD

from semantic_mapper.engine.mapper import MapperOrchestrator

ONT = Namespace("http://test.org/ont#")


def _make_orch(graph: Graph) -> MapperOrchestrator:
    orch = object.__new__(MapperOrchestrator)
    orch.graph = graph
    return orch


# ---------------------------------------------------------------------------
# Shared graph fixture builders
# ---------------------------------------------------------------------------

def _make_description_graph() -> Graph:
    """
    Graph for 'ordercategorydescription' / 'statusdescription' tests.

      hasDescription              — abstract DatatypeProperty (no domain)
      hasInputeventsMvDescription — subPropertyOf hasDescription, domain=InputeventsMv
                                    (generic; NOT what we want for the compound columns)
      hasInputeventsMvOrderCategoryDescription — domain=InputeventsMv, NOT subPropertyOf
      hasInputeventsMvStatusDescription        — domain=InputeventsMv, NOT subPropertyOf
    """
    g = Graph()
    g.add((ONT.InputeventsMv, RDF.type, OWL.Class))

    # Abstract parent
    g.add((ONT.hasDescription, RDF.type, OWL.DatatypeProperty))
    g.add((ONT.hasDescription, RDFS.range, XSD.string))

    # Generic subproperty (the one Phase 1 would find — wrong target)
    g.add((ONT.hasInputeventsMvDescription, RDF.type, OWL.DatatypeProperty))
    g.add((ONT.hasInputeventsMvDescription, RDFS.domain, ONT.InputeventsMv))
    g.add((ONT.hasInputeventsMvDescription, RDFS.range, XSD.string))
    g.add((ONT.hasInputeventsMvDescription, RDFS.subPropertyOf, ONT.hasDescription))

    # Compound-specific properties (NOT subPropertyOf hasDescription)
    g.add((ONT.hasInputeventsMvOrderCategoryDescription, RDF.type, OWL.DatatypeProperty))
    g.add((ONT.hasInputeventsMvOrderCategoryDescription, RDFS.domain, ONT.InputeventsMv))
    g.add((ONT.hasInputeventsMvOrderCategoryDescription, RDFS.range, XSD.string))

    g.add((ONT.hasInputeventsMvStatusDescription, RDF.type, OWL.DatatypeProperty))
    g.add((ONT.hasInputeventsMvStatusDescription, RDFS.domain, ONT.InputeventsMv))
    g.add((ONT.hasInputeventsMvStatusDescription, RDFS.range, XSD.string))

    return g


def _make_uom_graph() -> Graph:
    """
    Graph for 'totalamountuom' test.

      hasAmountUom                  — abstract DatatypeProperty (no domain)
      hasInputeventsMvAmountUom     — subPropertyOf hasAmountUom, domain=InputeventsMv
      hasInputeventsMvTotalAmountUom — domain=InputeventsMv, NOT subPropertyOf
    """
    g = Graph()
    g.add((ONT.InputeventsMv, RDF.type, OWL.Class))

    g.add((ONT.hasAmountUom, RDF.type, OWL.DatatypeProperty))
    g.add((ONT.hasAmountUom, RDFS.range, XSD.string))

    g.add((ONT.hasInputeventsMvAmountUom, RDF.type, OWL.DatatypeProperty))
    g.add((ONT.hasInputeventsMvAmountUom, RDFS.domain, ONT.InputeventsMv))
    g.add((ONT.hasInputeventsMvAmountUom, RDFS.range, XSD.string))
    g.add((ONT.hasInputeventsMvAmountUom, RDFS.subPropertyOf, ONT.hasAmountUom))

    g.add((ONT.hasInputeventsMvTotalAmountUom, RDF.type, OWL.DatatypeProperty))
    g.add((ONT.hasInputeventsMvTotalAmountUom, RDFS.domain, ONT.InputeventsMv))
    g.add((ONT.hasInputeventsMvTotalAmountUom, RDFS.range, XSD.string))

    return g


# ---------------------------------------------------------------------------
# Compound column tests (Phase 2 scenarios)
# ---------------------------------------------------------------------------

class TestCompoundColumnResolution:
    def test_ordercategorydescription_resolves_to_compound_property(self):
        """
        'ordercategorydescription' → abstract hasDescription
        Phase 1: finds hasInputeventsMvDescription but concept "inputeventsmvdescription"
                 does not overlap tightly — col_base "ordercategory" not in concept.
        Phase 2: col_norm ends with "description" == abstract concept; search all
                 InputeventsMv DatatypeProperties for col_base "ordercategory" →
                 finds hasInputeventsMvOrderCategoryDescription uniquely.
        """
        g = _make_description_graph()
        orch = _make_orch(g)
        result = orch._resolve_to_specific_property(
            str(ONT.hasDescription),
            str(ONT.InputeventsMv),
            "ordercategorydescription",
        )
        assert result == str(ONT.hasInputeventsMvOrderCategoryDescription)

    def test_statusdescription_resolves_to_compound_property(self):
        """
        'statusdescription' → abstract hasDescription → hasInputeventsMvStatusDescription.
        col_base = "status"; "status" in "inputeventsmvstatusdescription" = True.
        """
        g = _make_description_graph()
        orch = _make_orch(g)
        result = orch._resolve_to_specific_property(
            str(ONT.hasDescription),
            str(ONT.InputeventsMv),
            "statusdescription",
        )
        assert result == str(ONT.hasInputeventsMvStatusDescription)

    def test_totalamountuom_resolves_to_compound_property(self):
        """
        'totalamountuom' → abstract hasAmountUom → hasInputeventsMvTotalAmountUom.
        col_base = "totalamount" after stripping "uom";
        "totalamount" in "inputeventsmvtotalamountuom" = True.
        """
        g = _make_uom_graph()
        orch = _make_orch(g)
        result = orch._resolve_to_specific_property(
            str(ONT.hasAmountUom),
            str(ONT.InputeventsMv),
            "totalamountuom",
        )
        assert result == str(ONT.hasInputeventsMvTotalAmountUom)

    def test_plain_description_column_resolves_via_phase1(self):
        """
        A plain 'description' column (no compound prefix) should resolve via
        Phase 1 to the generic hasInputeventsMvDescription — Phase 2 must not
        disturb valid Phase 1 resolution.
        """
        g = _make_description_graph()
        # Remove the compound props so Phase 1 resolves uniquely
        g.remove((ONT.hasInputeventsMvOrderCategoryDescription, None, None))
        g.remove((ONT.hasInputeventsMvStatusDescription, None, None))
        orch = _make_orch(g)
        result = orch._resolve_to_specific_property(
            str(ONT.hasDescription),
            str(ONT.InputeventsMv),
            "description",
        )
        assert result == str(ONT.hasInputeventsMvDescription)

    def test_ambiguous_phase2_returns_abstract(self):
        """
        When Phase 2 finds >1 candidate, return the abstract URI (no guess).
        Add a second property whose concept also contains col_base.
        """
        g = _make_description_graph()
        # Add a second compound property also matching col_base "ordercategory"
        g.add((ONT.hasInputeventsMvOrderCategoryShortDescription, RDF.type, OWL.DatatypeProperty))
        g.add((ONT.hasInputeventsMvOrderCategoryShortDescription, RDFS.domain, ONT.InputeventsMv))
        g.add((ONT.hasInputeventsMvOrderCategoryShortDescription, RDFS.range, XSD.string))
        orch = _make_orch(g)
        result = orch._resolve_to_specific_property(
            str(ONT.hasDescription),
            str(ONT.InputeventsMv),
            "ordercategorydescription",
        )
        # Two candidates → no guess → return abstract
        assert result == str(ONT.hasDescription)

    def test_no_suffix_column_skips_phase2(self):
        """
        Column with no strippable suffix does not trigger Phase 2 at all.
        col_base == col_norm → Phase 2 condition is False.
        """
        g = _make_description_graph()
        orch = _make_orch(g)
        # "ordercategory" has no strippable suffix; Phase 1 finds nothing overlapping
        result = orch._resolve_to_specific_property(
            str(ONT.hasDescription),
            str(ONT.InputeventsMv),
            "ordercategory",
        )
        assert result == str(ONT.hasDescription)

    def test_phase2_does_not_cross_property_types(self):
        """
        Phase 2 must not return an ObjectProperty when the abstract is a DatatypeProperty.
        """
        g = _make_description_graph()
        # Add an ObjectProperty whose concept contains "ordercategory"
        g.add((ONT.hasInputeventsMvOrderCategoryObj, RDF.type, OWL.ObjectProperty))
        g.add((ONT.hasInputeventsMvOrderCategoryObj, RDFS.domain, ONT.InputeventsMv))
        g.add((ONT.hasInputeventsMvOrderCategoryObj, RDFS.range, ONT.InputeventsMv))
        orch = _make_orch(g)
        result = orch._resolve_to_specific_property(
            str(ONT.hasDescription),
            str(ONT.InputeventsMv),
            "ordercategorydescription",
        )
        # ObjectProperty excluded by type guard; only DatatypeProperty candidate remains
        assert result == str(ONT.hasInputeventsMvOrderCategoryDescription)

    def test_underscore_column_name_normalised(self):
        """
        Column names with underscores are normalised before matching.
        'order_category_description' should behave identically to 'ordercategorydescription'.
        """
        g = _make_description_graph()
        orch = _make_orch(g)
        result = orch._resolve_to_specific_property(
            str(ONT.hasDescription),
            str(ONT.InputeventsMv),
            "order_category_description",
        )
        assert result == str(ONT.hasInputeventsMvOrderCategoryDescription)


# ---------------------------------------------------------------------------
# Regression: existing Phase 1 behaviour unchanged
# ---------------------------------------------------------------------------

class TestPhase1Regression:
    def test_subject_id_in_admissions_context(self):
        """subject_id → abstract hasSubjectId → hasAdmissionSubjectId (Phase 1)."""
        g = Graph()
        g.add((ONT.Admission, RDF.type, OWL.Class))
        g.add((ONT.Patient, RDF.type, OWL.Class))
        g.add((ONT.hasSubjectId, RDF.type, OWL.DatatypeProperty))
        g.add((ONT.hasSubjectId, RDFS.range, XSD.integer))
        g.add((ONT.hasAdmissionSubjectId, RDF.type, OWL.DatatypeProperty))
        g.add((ONT.hasAdmissionSubjectId, RDFS.domain, ONT.Admission))
        g.add((ONT.hasAdmissionSubjectId, RDFS.range, XSD.integer))
        g.add((ONT.hasAdmissionSubjectId, RDFS.subPropertyOf, ONT.hasSubjectId))
        g.add((ONT.hasPatientSubjectId, RDF.type, OWL.DatatypeProperty))
        g.add((ONT.hasPatientSubjectId, RDFS.domain, ONT.Patient))
        g.add((ONT.hasPatientSubjectId, RDFS.range, XSD.integer))
        g.add((ONT.hasPatientSubjectId, RDFS.subPropertyOf, ONT.hasSubjectId))

        orch = _make_orch(g)
        result = orch._resolve_to_specific_property(
            str(ONT.hasSubjectId), str(ONT.Admission), "subject_id"
        )
        assert result == str(ONT.hasAdmissionSubjectId)

    def test_no_subprops_returns_abstract(self):
        """Abstract prop with no subproperties → return unchanged."""
        g = Graph()
        g.add((ONT.Admission, RDF.type, OWL.Class))
        g.add((ONT.hasDescription, RDF.type, OWL.DatatypeProperty))
        g.add((ONT.hasDescription, RDFS.range, XSD.string))
        orch = _make_orch(g)
        result = orch._resolve_to_specific_property(
            str(ONT.hasDescription), str(ONT.Admission), "description"
        )
        assert result == str(ONT.hasDescription)

    def test_property_with_domain_returned_unchanged(self):
        """If the property already has rdfs:domain, return it immediately."""
        g = Graph()
        g.add((ONT.Admission, RDF.type, OWL.Class))
        g.add((ONT.hasAdmissionSubjectId, RDF.type, OWL.DatatypeProperty))
        g.add((ONT.hasAdmissionSubjectId, RDFS.domain, ONT.Admission))
        orch = _make_orch(g)
        result = orch._resolve_to_specific_property(
            str(ONT.hasAdmissionSubjectId), str(ONT.Admission), "subject_id"
        )
        assert result == str(ONT.hasAdmissionSubjectId)
