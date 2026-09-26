# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for enrich_ontology_from_value_mappings."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection

from semantic_mapper.ontology.enricher import enrich_ontology_from_value_mappings
from semantic_mapper.schema.mapping_output import (
    ClassCandidate,
    MappingStatus,
    PropertyMapping,
    SemanticMappingReport,
    TableMapping,
    ValueMappingRule,
)

ONT = Namespace("http://example.org/ont#")


def _base_graph() -> Graph:
    g = Graph()
    g.bind("ont", ONT)
    # Religion object property: domain=Admission, range=Religion
    admission = ONT.Admission
    religion_class = ONT.Religion
    religion_prop = ONT.hasReligion

    g.add((admission, RDF.type, OWL.Class))
    g.add((religion_class, RDF.type, OWL.Class))
    g.add((religion_prop, RDF.type, OWL.ObjectProperty))
    g.add((religion_prop, RDFS.domain, admission))
    g.add((religion_prop, RDFS.range, religion_class))
    return g


def _base_report(*, value_mappings: list | None = None) -> SemanticMappingReport:
    vm_rules = value_mappings or [
        ValueMappingRule(source_value="CATHOLIC", mapped_uri=str(ONT.CatholicReligion), rationale="derived", confidence_score=0.9),
        ValueMappingRule(source_value="PROTESTANT", mapped_uri=str(ONT.ProtestantReligion), rationale="derived", confidence_score=0.9),
    ]
    col = PropertyMapping(
        source_column="religion",
        source_data_type="VARCHAR",
        is_nullable=True,
        mapped_property_uri=str(ONT.hasReligion),
        status=MappingStatus.CONFIRMED,
        confidence_score=0.9,
        rationale="matched",
        value_mappings=vm_rules,
    )
    table = TableMapping(
        source_table="admissions",
        class_candidate_set=[ClassCandidate(uri=str(ONT.Admission), label="Admission", rationale="matched", confidence_score=0.9)],
        column_mappings=[col],
    )
    return SemanticMappingReport(
        ontology_uri=str(ONT),
        database_name="test_db",
        settings_used={},
        tables=[table],
        total_pending_review=0,
    )


def _base_sidecar() -> dict:
    return {
        "admissions.religion": [
            {"source_value": "CATHOLIC", "mapped_uri": str(ONT.CatholicReligion), "rationale": "derived"},
            {"source_value": "PROTESTANT", "mapped_uri": str(ONT.ProtestantReligion), "rationale": "derived"},
        ]
    }


class TestEnrichOntologyFromValueMappings:
    def test_adds_new_classes_to_graph(self):
        g = _base_graph()
        report = _base_report()
        sidecar = _base_sidecar()

        with tempfile.NamedTemporaryFile(suffix=".owl", delete=False) as f:
            owl_path = f.name

        n_added, new_uris = enrich_ontology_from_value_mappings(sidecar, report, g, owl_path)

        assert n_added == 2
        assert str(ONT.CatholicReligion) in new_uris
        assert str(ONT.ProtestantReligion) in new_uris

    def test_new_individuals_have_correct_rdf_type(self):
        g = _base_graph()
        report = _base_report()
        sidecar = _base_sidecar()

        with tempfile.NamedTemporaryFile(suffix=".owl", delete=False) as f:
            owl_path = f.name

        enrich_ontology_from_value_mappings(sidecar, report, g, owl_path)

        assert (ONT.CatholicReligion, RDF.type, OWL.NamedIndividual) in g
        assert (ONT.ProtestantReligion, RDF.type, OWL.NamedIndividual) in g

    def test_new_individuals_typed_as_range_class(self):
        g = _base_graph()
        report = _base_report()
        sidecar = _base_sidecar()

        with tempfile.NamedTemporaryFile(suffix=".owl", delete=False) as f:
            owl_path = f.name

        enrich_ontology_from_value_mappings(sidecar, report, g, owl_path)

        assert (ONT.CatholicReligion, RDF.type, ONT.Religion) in g
        assert (ONT.ProtestantReligion, RDF.type, ONT.Religion) in g

    def test_new_classes_have_rdfs_label(self):
        g = _base_graph()
        report = _base_report()
        sidecar = _base_sidecar()

        with tempfile.NamedTemporaryFile(suffix=".owl", delete=False) as f:
            owl_path = f.name

        enrich_ontology_from_value_mappings(sidecar, report, g, owl_path)

        labels = {str(o) for o in g.objects(ONT.CatholicReligion, RDFS.label)}
        assert labels  # at least one label present

    def test_skips_uris_already_in_ontology(self):
        g = _base_graph()
        g.add((ONT.CatholicReligion, RDF.type, OWL.NamedIndividual))
        report = _base_report()
        sidecar = _base_sidecar()

        with tempfile.NamedTemporaryFile(suffix=".owl", delete=False) as f:
            owl_path = f.name

        n_added, new_uris = enrich_ontology_from_value_mappings(sidecar, report, g, owl_path)

        assert str(ONT.CatholicReligion) not in new_uris
        assert n_added == 1  # only ProtestantReligion is new

    def test_skips_sidecar_keys_with_no_mapped_property(self):
        g = _base_graph()
        # Column not present in report → no property_uri resolved
        sidecar = {"admissions.unknown_col": [{"source_value": "X", "mapped_uri": str(ONT.SomeClass)}]}
        report = _base_report()

        with tempfile.NamedTemporaryFile(suffix=".owl", delete=False) as f:
            owl_path = f.name

        n_added, new_uris = enrich_ontology_from_value_mappings(sidecar, report, g, owl_path)

        assert n_added == 0
        assert new_uris == []

    def test_serialises_graph_to_owl_file(self):
        g = _base_graph()
        report = _base_report()
        sidecar = _base_sidecar()

        with tempfile.NamedTemporaryFile(suffix=".owl", delete=False) as f:
            owl_path = f.name

        enrich_ontology_from_value_mappings(sidecar, report, g, owl_path)

        written = Path(owl_path).read_text()
        assert "CatholicReligion" in written

    def test_no_write_when_nothing_added(self):
        g = _base_graph()
        # Pre-populate both individuals AND put them in owl:oneOf so warm-run has nothing to do
        g.add((ONT.CatholicReligion, RDF.type, OWL.NamedIndividual))
        g.add((ONT.ProtestantReligion, RDF.type, OWL.NamedIndividual))
        eq_bn = BNode()
        n1, n2 = BNode(), BNode()
        g.add((ONT.Religion, OWL.equivalentClass, eq_bn))
        g.add((eq_bn, OWL.oneOf, n1))
        g.add((n1, RDF.first, ONT.CatholicReligion))
        g.add((n1, RDF.rest, n2))
        g.add((n2, RDF.first, ONT.ProtestantReligion))
        g.add((n2, RDF.rest, RDF.nil))
        report = _base_report()
        sidecar = _base_sidecar()

        with tempfile.NamedTemporaryFile(suffix=".owl", delete=False) as f:
            owl_path = f.name

        mtime_before = Path(owl_path).stat().st_mtime
        n_added, _ = enrich_ontology_from_value_mappings(sidecar, report, g, owl_path)

        assert n_added == 0
        assert Path(owl_path).stat().st_mtime == mtime_before

    def test_no_subclass_added_when_property_has_no_range(self):
        g = _base_graph()
        # Add another property with no rdfs:range
        no_range_prop = ONT.hasStatus
        status_class = ONT.Status
        g.add((status_class, RDF.type, OWL.Class))
        g.add((no_range_prop, RDF.type, OWL.ObjectProperty))
        # no RDFS.range triple

        col2 = PropertyMapping(
            source_column="status",
            source_data_type="VARCHAR",
            is_nullable=True,
            mapped_property_uri=str(no_range_prop),
            status=MappingStatus.CONFIRMED,
            confidence_score=0.9,
            rationale="matched",
            value_mappings=[
                ValueMappingRule(source_value="ACTIVE", mapped_uri=str(ONT.ActiveStatus), rationale="d", confidence_score=0.9)
            ],
        )
        table2 = TableMapping(
            source_table="patients",
            class_candidate_set=[ClassCandidate(uri=str(ONT.Status), label="Status", rationale="m", confidence_score=0.9)],
            column_mappings=[col2],
        )
        report2 = SemanticMappingReport(
            ontology_uri=str(ONT), database_name="test_db", settings_used={},
            tables=[table2], total_pending_review=0,
        )
        sidecar2 = {"patients.status": [{"source_value": "ACTIVE", "mapped_uri": str(ONT.ActiveStatus)}]}

        with tempfile.NamedTemporaryFile(suffix=".owl", delete=False) as f:
            owl_path = f.name

        n_added, _ = enrich_ontology_from_value_mappings(sidecar2, report2, g, owl_path)

        # Individual must NOT be created when range is unknown (untyped guard)
        assert n_added == 0
        assert (ONT.ActiveStatus, RDF.type, OWL.NamedIndividual) not in g

    def test_empty_sidecar_returns_zero(self):
        g = _base_graph()
        report = _base_report()

        with tempfile.NamedTemporaryFile(suffix=".owl", delete=False) as f:
            owl_path = f.name

        n_added, new_uris = enrich_ontology_from_value_mappings({}, report, g, owl_path)

        assert n_added == 0
        assert new_uris == []

    def test_appends_to_existing_one_of_enumeration(self):
        g = _base_graph()
        # Add an existing individual in an owl:oneOf on Religion
        existing_ind = ONT.OtherReligion
        g.add((existing_ind, RDF.type, OWL.NamedIndividual))
        g.add((existing_ind, RDF.type, ONT.Religion))

        eq_bnode = BNode()
        list_node = BNode()
        g.add((ONT.Religion, OWL.equivalentClass, eq_bnode))
        g.add((eq_bnode, OWL.oneOf, list_node))
        g.add((list_node, RDF.first, existing_ind))
        g.add((list_node, RDF.rest, RDF.nil))

        report = _base_report()
        sidecar = _base_sidecar()

        with tempfile.NamedTemporaryFile(suffix=".owl", delete=False) as f:
            owl_path = f.name

        n_added, new_uris = enrich_ontology_from_value_mappings(sidecar, report, g, owl_path)

        assert n_added == 2
        # Collect all members of the owl:oneOf list
        members = set(Collection(g, list_node))
        assert existing_ind in members
        assert ONT.CatholicReligion in members
        assert ONT.ProtestantReligion in members

    def test_creates_one_of_when_none_exists(self):
        g = _base_graph()
        # Religion has no equivalentClass / oneOf — enricher should create one
        report = _base_report()
        sidecar = _base_sidecar()

        with tempfile.NamedTemporaryFile(suffix=".owl", delete=False) as f:
            owl_path = f.name

        n_added, _ = enrich_ontology_from_value_mappings(sidecar, report, g, owl_path)

        assert n_added == 2
        assert (ONT.CatholicReligion, RDF.type, OWL.NamedIndividual) in g

        # An owl:equivalentClass with owl:oneOf should now exist on Religion
        eq_classes = list(g.objects(ONT.Religion, OWL.equivalentClass))
        assert eq_classes, "equivalentClass axiom should have been created"
        one_of_head = g.value(eq_classes[0], OWL.oneOf)
        assert one_of_head is not None
        members = set(Collection(g, one_of_head))
        assert ONT.CatholicReligion in members
        assert ONT.ProtestantReligion in members
