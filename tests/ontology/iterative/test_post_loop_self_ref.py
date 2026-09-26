# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the post-loop self-referential subPropertyOf coverage gap.

The guards inside merge() only protect triples added during the per-table loop.
Post-loop safety passes (_qualify_obj_props_safety, etc.) add triples directly
to accumulating_graph afterward — these are covered by:

  1. A self-ref guard inside _qualify_obj_props_safety itself.
  2. A final _scan_and_strip_self_ref_subprops() call in generate() after all
     safety passes complete.

Covered scenarios:
  - _qualify_obj_props_safety skips self-referential subPropertyOf and logs WARNING.
  - _qualify_obj_props_safety wires valid (non-self-referential) subPropertyOf correctly.
  - When prop URI == abstract_uri, the OTHER prop in the group still gets wired.
  - The final post-loop scan (called in generate()) strips self-refs injected by
    any safety pass that bypasses the _add_triple guard.
"""
import logging

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef

from semantic_mapper.ontology.iterative.generator import _qualify_obj_props_safety
from semantic_mapper.ontology.iterative.graph_merger import _scan_and_strip_self_ref_subprops

BASE = "http://mesh.local/ontology/test#"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uri(local: str) -> URIRef:
    return URIRef(BASE + local)


def _make_graph() -> Graph:
    g = Graph()
    g.add((_uri("Admission"), RDF.type, OWL.Class))
    g.add((_uri("Patient"), RDF.type, OWL.Class))
    return g


def _add_obj_prop(g: Graph, local: str, domain_local: str, range_local: str) -> None:
    prop = _uri(local)
    g.add((prop, RDF.type, OWL.ObjectProperty))
    g.add((prop, RDFS.domain, _uri(domain_local)))
    g.add((prop, RDFS.range, _uri(range_local)))


# ---------------------------------------------------------------------------
# _qualify_obj_props_safety: self-referential guard
# ---------------------------------------------------------------------------

class TestQualifyObjPropsSafetyGuard:
    def test_self_ref_skipped_when_prop_uri_equals_abstract_uri(self, caplog):
        """If an existing ObjectProperty has URI == would-be abstract_uri, skip subPropertyOf."""
        g = _make_graph()
        # hasAdmission exists with a domain — it would also be the abstract_uri target
        _add_obj_prop(g, "hasAdmission", "Inputevent", "Admission")
        # A second prop with a different domain forces qualification
        _add_obj_prop(g, "hasAdmissionRef", "Careunit", "Admission")

        with caplog.at_level(logging.WARNING, logger="semantic_mapper.ontology.iterative.generator"):
            _qualify_obj_props_safety(g, BASE)

        # hasAdmission must NOT be subPropertyOf itself
        self_ref = (
            _uri("hasAdmission"),
            RDFS.subPropertyOf,
            _uri("hasAdmission"),
        )
        assert self_ref not in g, "Self-referential subPropertyOf was incorrectly added"

    def test_warning_logged_for_self_ref_skip(self, caplog):
        """A WARNING is logged when the self-ref guard fires."""
        g = _make_graph()
        _add_obj_prop(g, "hasAdmission", "Inputevent", "Admission")
        _add_obj_prop(g, "hasAdmissionRef", "Careunit", "Admission")

        with caplog.at_level(logging.WARNING, logger="semantic_mapper.ontology.iterative.generator"):
            _qualify_obj_props_safety(g, BASE)

        assert any(
            "self-referential" in rec.message.lower() or "skipping" in rec.message.lower()
            for rec in caplog.records
            if rec.levelname == "WARNING"
        ), "Expected a WARNING about self-referential subPropertyOf"

    def test_other_prop_in_group_still_wired(self):
        """When one prop in a group triggers the guard, the other prop is still wired."""
        g = _make_graph()
        # hasAdmission (would become its own abstract) + hasAdmissionRef (should be wired)
        _add_obj_prop(g, "hasAdmission", "Inputevent", "Admission")
        _add_obj_prop(g, "hasAdmissionRef", "Careunit", "Admission")

        _qualify_obj_props_safety(g, BASE)

        abstract_uri = _uri("hasAdmission")
        # Abstract parent must exist
        assert (abstract_uri, RDF.type, OWL.ObjectProperty) in g

        # hasAdmissionRef should be subPropertyOf hasAdmission
        ref_prop = _uri("hasAdmissionRef")
        parents = list(g.objects(ref_prop, RDFS.subPropertyOf))
        assert abstract_uri in parents, (
            f"hasAdmissionRef should be subPropertyOf hasAdmission; got {parents}"
        )

    def test_no_self_ref_for_normal_props(self):
        """Normal props (URI != abstract_uri) receive subPropertyOf without guard firing."""
        g = _make_graph()
        # Two props with different domains, neither named hasAdmission
        _add_obj_prop(g, "admittedVia", "Inputevent", "Admission")
        _add_obj_prop(g, "transferredTo", "Careunit", "Admission")

        _qualify_obj_props_safety(g, BASE)

        abstract_uri = _uri("hasAdmission")
        for prop_local in ("admittedVia", "transferredTo"):
            prop = _uri(prop_local)
            parents = list(g.objects(prop, RDFS.subPropertyOf))
            assert abstract_uri in parents, (
                f"{prop_local} should be subPropertyOf hasAdmission; got {parents}"
            )
        # No self-referential triple
        assert (abstract_uri, RDFS.subPropertyOf, abstract_uri) not in g

    def test_single_prop_per_range_no_qualification(self):
        """A range with only one ObjectProperty is NOT qualified (no abstract parent added)."""
        g = _make_graph()
        _add_obj_prop(g, "hasPatient", "Admission", "Patient")

        _qualify_obj_props_safety(g, BASE)

        # No abstract parent should be created for Patient when there's only one prop
        abstract_uri = _uri("hasPatient")
        # If it's not already an abstract (has domain), no subPropertyOf should appear
        parents = list(g.objects(_uri("hasPatient"), RDFS.subPropertyOf))
        assert parents == [], f"Unexpected subPropertyOf on single-prop range: {parents}"

    def test_abstract_parent_range_set_correctly(self):
        """Abstract parent created by _qualify_obj_props_safety has correct rdfs:range."""
        g = _make_graph()
        _add_obj_prop(g, "admittedVia", "Inputevent", "Admission")
        _add_obj_prop(g, "transferredTo", "Careunit", "Admission")

        _qualify_obj_props_safety(g, BASE)

        abstract_uri = _uri("hasAdmission")
        ranges = list(g.objects(abstract_uri, RDFS.range))
        assert _uri("Admission") in ranges, (
            f"Abstract parent range should be Admission; got {ranges}"
        )

    def test_abstract_parent_has_no_domain(self):
        """Abstract parent created by _qualify_obj_props_safety must have no rdfs:domain."""
        g = _make_graph()
        _add_obj_prop(g, "admittedVia", "Inputevent", "Admission")
        _add_obj_prop(g, "transferredTo", "Careunit", "Admission")

        _qualify_obj_props_safety(g, BASE)

        abstract_uri = _uri("hasAdmission")
        domains = list(g.objects(abstract_uri, RDFS.domain))
        assert domains == [], f"Abstract parent should have no domain; got {domains}"

    def test_idempotent_no_duplicate_subprops(self):
        """Calling _qualify_obj_props_safety twice does not duplicate subPropertyOf triples."""
        g = _make_graph()
        _add_obj_prop(g, "admittedVia", "Inputevent", "Admission")
        _add_obj_prop(g, "transferredTo", "Careunit", "Admission")

        _qualify_obj_props_safety(g, BASE)
        _qualify_obj_props_safety(g, BASE)

        abstract_uri = _uri("hasAdmission")
        prop = _uri("admittedVia")
        parents = list(g.objects(prop, RDFS.subPropertyOf))
        assert parents.count(abstract_uri) == 1, (
            f"Expected exactly one subPropertyOf triple; got {len(parents)}"
        )

    def test_self_ref_prop_promoted_to_abstract_parent_loses_domain(self):
        """
        When a property coincidentally matches the abstract parent URI (has{Range}),
        it is promoted to the abstract parent role: domain is stripped (Bug 2 fix)
        and self-referential subPropertyOf is prevented. Range is preserved.
        """
        g = _make_graph()
        _add_obj_prop(g, "hasAdmission", "Inputevent", "Admission")
        _add_obj_prop(g, "hasAdmissionRef", "Careunit", "Admission")

        _qualify_obj_props_safety(g, BASE)

        prop = _uri("hasAdmission")
        domains = list(g.objects(prop, RDFS.domain))
        ranges = list(g.objects(prop, RDFS.range))
        # Domain stripped — hasAdmission is now the abstract parent
        assert domains == [], f"Abstract parent must have no domain; got {domains}"
        # Range retained
        assert _uri("Admission") in ranges


# ---------------------------------------------------------------------------
# Final post-loop scan (simulating what generate() does)
# ---------------------------------------------------------------------------

class TestFinalPostLoopScan:
    def test_scan_removes_self_ref_added_after_merge(self):
        """
        Simulate a self-referential triple added by a safety pass (bypassing _add_triple).
        The final _scan_and_strip_self_ref_subprops call must remove it.
        """
        g = Graph()
        prop = _uri("hasValue")
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        # Directly inject self-referential triple (as a safety pass might do via graph.add)
        g.add((prop, RDFS.subPropertyOf, prop))

        removed = _scan_and_strip_self_ref_subprops(g)

        assert removed == 1
        assert (prop, RDFS.subPropertyOf, prop) not in g

    def test_scan_preserves_valid_subprops(self):
        """Final scan must not remove valid (non-self-referential) subPropertyOf triples."""
        g = Graph()
        child = _uri("hasAdmissionValue")
        parent = _uri("hasValue")
        g.add((child, RDF.type, OWL.DatatypeProperty))
        g.add((parent, RDF.type, OWL.DatatypeProperty))
        g.add((child, RDFS.subPropertyOf, parent))

        removed = _scan_and_strip_self_ref_subprops(g)

        assert removed == 0
        assert (child, RDFS.subPropertyOf, parent) in g

    def test_scan_removes_multiple_self_refs(self):
        """Final scan strips all self-referential triples in one pass."""
        g = Graph()
        for local in ("hasFoo", "hasBar", "hasBaz"):
            prop = _uri(local)
            g.add((prop, RDF.type, OWL.DatatypeProperty))
            g.add((prop, RDFS.subPropertyOf, prop))

        removed = _scan_and_strip_self_ref_subprops(g)

        assert removed == 3
        for local in ("hasFoo", "hasBar", "hasBaz"):
            prop = _uri(local)
            assert (prop, RDFS.subPropertyOf, prop) not in g

    def test_scan_warns_for_each_self_ref(self, caplog):
        """A WARNING is logged for every self-referential triple the scan removes."""
        g = Graph()
        for local in ("hasFoo", "hasBar"):
            prop = _uri(local)
            g.add((prop, RDF.type, OWL.DatatypeProperty))
            g.add((prop, RDFS.subPropertyOf, prop))

        with caplog.at_level(logging.WARNING):
            _scan_and_strip_self_ref_subprops(g)

        warning_msgs = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert len(warning_msgs) >= 2, (
            f"Expected ≥2 warnings, got {len(warning_msgs)}: {warning_msgs}"
        )

    def test_qualify_then_scan_leaves_no_self_refs(self):
        """
        End-to-end: run _qualify_obj_props_safety followed by the final scan.
        Even if _qualify_obj_props_safety somehow added a self-ref,
        the final scan removes it.
        """
        g = _make_graph()
        # hasAdmission prop with domain — triggers self-ref guard in qualify
        _add_obj_prop(g, "hasAdmission", "Inputevent", "Admission")
        _add_obj_prop(g, "hasAdmissionRef", "Careunit", "Admission")

        # Simulate safety pass
        _qualify_obj_props_safety(g, BASE)

        # Simulate final scan (called in generate() after all safety passes)
        _scan_and_strip_self_ref_subprops(g)

        # Verify no self-referential triples remain anywhere in the graph
        self_refs = [
            (s, p, o)
            for s, p, o in g.triples((None, RDFS.subPropertyOf, None))
            if s == o
        ]
        assert self_refs == [], f"Self-referential subPropertyOf triples remain: {self_refs}"

    def test_empty_graph_scan_is_noop(self):
        """Scanning an empty graph returns 0 and does not raise."""
        g = Graph()
        removed = _scan_and_strip_self_ref_subprops(g)
        assert removed == 0

    def test_scan_after_qualify_with_normal_props_unchanged(self):
        """Final scan does not disturb valid subPropertyOf triples created by qualify."""
        g = _make_graph()
        _add_obj_prop(g, "admittedVia", "Inputevent", "Admission")
        _add_obj_prop(g, "transferredTo", "Careunit", "Admission")

        _qualify_obj_props_safety(g, BASE)
        _scan_and_strip_self_ref_subprops(g)

        abstract_uri = _uri("hasAdmission")
        for prop_local in ("admittedVia", "transferredTo"):
            prop = _uri(prop_local)
            parents = list(g.objects(prop, RDFS.subPropertyOf))
            assert abstract_uri in parents, (
                f"{prop_local} lost its subPropertyOf after final scan"
            )
