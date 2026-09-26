# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for semantic_mapper/prove/subgraph.py.

Uses in-memory rdflib graphs with fm: annotations (Phase 0b format).
"""
from __future__ import annotations

import time

import pytest
import rdflib
from rdflib import OWL, RDF, RDFS, Literal, Namespace, URIRef
from rdflib.namespace import XSD

from semantic_mapper.prove.subgraph import (
    ClassNode,
    FM,
    OntologySubgraph,
    PropertyNode,
    extract_subgraph,
)

BASE = "http://example.org/mimic#"
FM_NS = Namespace("http://forgemesh.ai/ontology#")


def u(local: str) -> URIRef:
    return URIRef(f"{BASE}{local}")


def s(local: str) -> str:
    return f"{BASE}{local}"


# ---------------------------------------------------------------------------
# Fixture graph (Phase 0b format: fm: annotations, clean rdfs:comment)
# ---------------------------------------------------------------------------

def _build_graph() -> rdflib.Graph:
    """Small MIMIC-like graph with fm: annotations and clean rdfs:comment."""
    g = rdflib.Graph()

    # --- Classes ---
    g.add((u("Admission"), RDF.type, OWL.Class))
    g.add((u("Admission"), RDFS.label, Literal("Admission", lang="en")))
    g.add((u("Admission"), RDFS.comment, Literal("An admission event.", lang="en")))
    g.add((u("Admission"), FM_NS.sourceTable, Literal("admissions")))

    g.add((u("Patient"), RDF.type, OWL.Class))
    g.add((u("Patient"), RDFS.label, Literal("Patient", lang="en")))
    g.add((u("Patient"), RDFS.comment, Literal("A hospital patient.", lang="en")))
    g.add((u("Patient"), FM_NS.sourceTable, Literal("patients")))

    # One hop up from Admission
    g.add((u("ClinicalEvent"), RDF.type, OWL.Class))
    g.add((u("ClinicalEvent"), RDFS.label, Literal("ClinicalEvent", lang="en")))
    g.add((u("Admission"), RDFS.subClassOf, u("ClinicalEvent")))

    # One hop down from Patient
    g.add((u("InPatient"), RDF.type, OWL.Class))
    g.add((u("InPatient"), RDFS.label, Literal("InPatient", lang="en")))
    g.add((u("InPatient"), FM_NS.sourceTable, Literal("in_patients")))
    g.add((u("InPatient"), RDFS.subClassOf, u("Patient")))

    # Unannotated class (graceful degradation)
    g.add((u("Bare"), RDF.type, OWL.Class))

    # --- DatatypeProperty on Admission ---
    g.add((u("hasAdmittime"), RDF.type, OWL.DatatypeProperty))
    g.add((u("hasAdmittime"), RDFS.label, Literal("hasAdmittime", lang="en")))
    g.add((u("hasAdmittime"), RDFS.comment, Literal("The admission datetime.", lang="en")))
    g.add((u("hasAdmittime"), RDFS.domain, u("Admission")))
    g.add((u("hasAdmittime"), RDFS.range, XSD.dateTime))
    g.add((u("hasAdmittime"), FM_NS.sourceColumn, Literal("admissions.admittime")))
    g.add((u("hasAdmittime"), FM_NS.sqlType, Literal("DATETIME")))
    g.add((u("hasAdmittime"), FM_NS.nullRate, Literal("0.05")))

    # --- ObjectProperty: Admission → Patient ---
    g.add((u("hasAdmissionSubject"), RDF.type, OWL.ObjectProperty))
    g.add((u("hasAdmissionSubject"), RDFS.label, Literal("hasAdmissionSubject", lang="en")))
    g.add((u("hasAdmissionSubject"), RDFS.comment, Literal("Links admission to patient.", lang="en")))
    g.add((u("hasAdmissionSubject"), RDFS.domain, u("Admission")))
    g.add((u("hasAdmissionSubject"), RDFS.range, u("Patient")))
    g.add((u("hasAdmissionSubject"), FM_NS.sourceForeignKey,
           Literal("admissions.subject_id -> patients")))

    # --- DatatypeProperty on Admission (second) ---
    g.add((u("hasHadmId"), RDF.type, OWL.DatatypeProperty))
    g.add((u("hasHadmId"), RDFS.label, Literal("hasHadmId", lang="en")))
    g.add((u("hasHadmId"), RDFS.domain, u("Admission")))
    g.add((u("hasHadmId"), RDFS.range, XSD.integer))
    g.add((u("hasHadmId"), FM_NS.sourceColumn, Literal("admissions.hadm_id")))
    g.add((u("hasHadmId"), FM_NS.sqlType, Literal("INT")))
    g.add((u("hasHadmId"), FM_NS.nullRate, Literal("0.00")))

    # --- DatatypeProperty on Patient ---
    g.add((u("hasSubjectId"), RDF.type, OWL.DatatypeProperty))
    g.add((u("hasSubjectId"), RDFS.label, Literal("hasSubjectId", lang="en")))
    g.add((u("hasSubjectId"), RDFS.domain, u("Patient")))
    g.add((u("hasSubjectId"), RDFS.range, XSD.integer))
    g.add((u("hasSubjectId"), FM_NS.sourceColumn, Literal("patients.subject_id")))
    g.add((u("hasSubjectId"), FM_NS.sqlType, Literal("INT")))
    g.add((u("hasSubjectId"), FM_NS.nullRate, Literal("0.00")))

    return g


@pytest.fixture
def graph():
    return _build_graph()


# ---------------------------------------------------------------------------
# Dataclass shape
# ---------------------------------------------------------------------------

class TestDataclassShape:
    def test_class_node_is_frozen(self):
        node = ClassNode(uri="u", label="L", comment="C", source_table="t")
        with pytest.raises((AttributeError, TypeError)):
            node.label = "changed"  # type: ignore[misc]

    def test_property_node_is_frozen(self):
        node = PropertyNode(
            uri="u", label="L", comment="C",
            source_column="t.c", source_fk="", sql_type="INT", null_rate="0.00",
            domain_uri="d", range_uri="r", kind="DatatypeProperty",
        )
        with pytest.raises((AttributeError, TypeError)):
            node.kind = "changed"  # type: ignore[misc]

    def test_ontology_subgraph_is_frozen(self):
        sg = OntologySubgraph(classes={}, properties={}, adjacency_notes=[])
        with pytest.raises((AttributeError, TypeError)):
            sg.classes = {}  # type: ignore[misc]

    def test_ontology_subgraph_fields(self):
        sg = OntologySubgraph(
            classes={"a": ClassNode("a", "A", "", "")},
            properties={},
            adjacency_notes=["note"],
        )
        assert "a" in sg.classes
        assert sg.adjacency_notes == ["note"]

    def test_property_node_has_fm_fields(self):
        node = PropertyNode(
            uri="u", label="L", comment="C",
            source_column="t.col", source_fk="t.fk_col -> other",
            sql_type="VARCHAR(50)", null_rate="0.10",
            domain_uri="d", range_uri="r", kind="DatatypeProperty",
        )
        assert node.source_fk == "t.fk_col -> other"
        assert node.sql_type == "VARCHAR(50)"
        assert node.null_rate == "0.10"

    def test_fm_namespace_exported(self):
        assert str(FM) == "http://forgemesh.ai/ontology#"


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------

class TestEmptyInput:
    def test_empty_class_uris_returns_empty(self, graph):
        sg = extract_subgraph(graph, set(), set())
        assert sg.classes == {}
        assert sg.properties == {}
        assert sg.adjacency_notes == []

    def test_empty_graph_returns_empty(self):
        sg = extract_subgraph(rdflib.Graph(), {s("Admission")}, set())
        node = sg.classes[s("Admission")]
        assert node.label == ""
        assert node.comment == ""
        assert node.source_table == ""
        assert sg.properties == {}


# ---------------------------------------------------------------------------
# Class extraction — fm:sourceTable
# ---------------------------------------------------------------------------

class TestClassExtraction:
    def test_queried_classes_present(self, graph):
        sg = extract_subgraph(graph, {s("Admission"), s("Patient")}, set())
        assert s("Admission") in sg.classes
        assert s("Patient") in sg.classes

    def test_class_label_extracted(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert sg.classes[s("Admission")].label == "Admission"

    def test_class_comment_extracted(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert sg.classes[s("Admission")].comment == "An admission event."

    def test_source_table_from_fm_annotation(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert sg.classes[s("Admission")].source_table == "admissions"

    def test_source_table_patient_from_fm(self, graph):
        sg = extract_subgraph(graph, {s("Patient")}, set())
        assert sg.classes[s("Patient")].source_table == "patients"

    def test_unannotated_class_empty_source_table(self, graph):
        sg = extract_subgraph(graph, {s("Bare")}, set())
        node = sg.classes[s("Bare")]
        assert node.label == ""
        assert node.comment == ""
        assert node.source_table == ""

    def test_nonexistent_uri_empty_fields_no_error(self, graph):
        sg = extract_subgraph(graph, {"http://nowhere.org#Ghost"}, set())
        node = sg.classes["http://nowhere.org#Ghost"]
        assert node.label == ""
        assert node.source_table == ""


# ---------------------------------------------------------------------------
# Subclass expansion
# ---------------------------------------------------------------------------

class TestSubclassExpansion:
    def test_one_hop_up_included(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert s("ClinicalEvent") in sg.classes

    def test_one_hop_down_included(self, graph):
        sg = extract_subgraph(graph, {s("Patient")}, set())
        assert s("InPatient") in sg.classes

    def test_inpatient_not_included_when_querying_admission_only(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert s("InPatient") not in sg.classes

    def test_subclass_source_table_populated(self, graph):
        sg = extract_subgraph(graph, {s("Patient")}, set())
        assert sg.classes[s("InPatient")].source_table == "in_patients"


# ---------------------------------------------------------------------------
# Property extraction — fm: predicates
# ---------------------------------------------------------------------------

class TestPropertyExtraction:
    def test_domain_properties_auto_discovered(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert s("hasAdmittime") in sg.properties
        assert s("hasAdmissionSubject") in sg.properties
        assert s("hasHadmId") in sg.properties

    def test_other_class_properties_not_included(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert s("hasSubjectId") not in sg.properties

    def test_patient_properties_when_patient_queried(self, graph):
        sg = extract_subgraph(graph, {s("Patient")}, set())
        assert s("hasSubjectId") in sg.properties

    def test_explicitly_passed_property_included(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, {s("hasSubjectId")})
        assert s("hasSubjectId") in sg.properties

    def test_property_kind_datatype(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert sg.properties[s("hasAdmittime")].kind == "DatatypeProperty"

    def test_property_kind_object(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert sg.properties[s("hasAdmissionSubject")].kind == "ObjectProperty"

    def test_source_column_from_fm(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert sg.properties[s("hasAdmittime")].source_column == "admissions.admittime"

    def test_source_column_second_property(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert sg.properties[s("hasHadmId")].source_column == "admissions.hadm_id"

    def test_source_fk_from_fm(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert sg.properties[s("hasAdmissionSubject")].source_fk == \
            "admissions.subject_id -> patients"

    def test_source_fk_empty_for_datatype_property(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert sg.properties[s("hasAdmittime")].source_fk == ""

    def test_sql_type_from_fm(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert sg.properties[s("hasAdmittime")].sql_type == "DATETIME"

    def test_null_rate_from_fm(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert sg.properties[s("hasAdmittime")].null_rate == "0.05"

    def test_null_rate_zero(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert sg.properties[s("hasHadmId")].null_rate == "0.00"

    def test_domain_uri_populated(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert sg.properties[s("hasAdmittime")].domain_uri == s("Admission")

    def test_range_uri_xsd_datatype(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert "dateTime" in sg.properties[s("hasAdmittime")].range_uri

    def test_range_uri_object_property(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert sg.properties[s("hasAdmissionSubject")].range_uri == s("Patient")

    def test_unannotated_property_empty_fm_fields(self, graph):
        g2 = rdflib.Graph()
        for triple in graph:
            g2.add(triple)
        bare = URIRef(f"{BASE}hasBareProperty")
        g2.add((bare, RDF.type, OWL.DatatypeProperty))
        g2.add((bare, RDFS.domain, u("Admission")))
        sg = extract_subgraph(g2, {s("Admission")}, set())
        node = sg.properties[str(bare)]
        assert node.comment == ""
        assert node.source_column == ""
        assert node.source_fk == ""
        assert node.sql_type == ""
        assert node.null_rate == ""
        assert node.label == ""

    def test_property_label_extracted(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert sg.properties[s("hasAdmittime")].label == "hasAdmittime"

    def test_property_comment_is_clean(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert sg.properties[s("hasAdmittime")].comment == "The admission datetime."
        assert "Derived from" not in sg.properties[s("hasAdmittime")].comment


# ---------------------------------------------------------------------------
# Adjacent class via range
# ---------------------------------------------------------------------------

class TestRangeExpansion:
    def test_range_class_added_to_subgraph(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert s("Patient") in sg.classes

    def test_range_class_annotations_populated(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        patient = sg.classes[s("Patient")]
        assert patient.label == "Patient"
        assert patient.source_table == "patients"

    def test_range_class_properties_not_auto_included(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert s("hasSubjectId") not in sg.properties

    def test_explicit_prop_domain_class_added(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, {s("hasSubjectId")})
        assert s("Patient") in sg.classes


# ---------------------------------------------------------------------------
# Adjacency notes
# ---------------------------------------------------------------------------

class TestAdjacencyNotes:
    def test_one_note_per_property_with_domain(self, graph):
        # Admission has 3 properties; Patient has 1
        sg = extract_subgraph(graph, {s("Admission"), s("Patient")}, set())
        assert len(sg.adjacency_notes) == 4

    def test_datatype_property_note_format(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert any("Admission has property hasAdmittime" in n for n in sg.adjacency_notes)

    def test_object_property_note_format(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert any("Admission has object property hasAdmissionSubject" in n
                   for n in sg.adjacency_notes)

    def test_datatype_note_provenance_from_source_column(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        note = next(n for n in sg.adjacency_notes if "hasAdmittime" in n)
        assert "(from admissions.admittime)" in note

    def test_object_property_note_provenance_fk(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        note = next(n for n in sg.adjacency_notes if "hasAdmissionSubject" in n)
        assert "(FK: admissions.subject_id -> patients)" in note

    def test_not_filtered_default(self, graph):
        sg = extract_subgraph(graph, {s("Admission")}, set())
        assert all("not filtered in this query" in n for n in sg.adjacency_notes)

    def test_filtered_flag_for_member(self, graph):
        filtered = {s("hasAdmittime")}
        sg = extract_subgraph(graph, {s("Admission")}, set(), filtered_property_uris=filtered)
        admittime_note = next(n for n in sg.adjacency_notes if "hasAdmittime" in n)
        assert "filtered in this query" in admittime_note
        assert "not filtered" not in admittime_note

    def test_non_filtered_unchanged_when_one_filtered(self, graph):
        filtered = {s("hasAdmittime")}
        sg = extract_subgraph(graph, {s("Admission")}, set(), filtered_property_uris=filtered)
        subject_note = next(n for n in sg.adjacency_notes if "hasAdmissionSubject" in n)
        assert "not filtered in this query" in subject_note

    def test_notes_are_deterministic(self, graph):
        sg1 = extract_subgraph(graph, {s("Admission"), s("Patient")}, set())
        sg2 = extract_subgraph(graph, {s("Patient"), s("Admission")}, set())
        assert sg1.adjacency_notes == sg2.adjacency_notes

    def test_abstract_property_excluded_from_notes(self, graph):
        g2 = rdflib.Graph()
        for triple in graph:
            g2.add(triple)
        abstract = URIRef(f"{BASE}hasAbstractValue")
        g2.add((abstract, RDF.type, OWL.DatatypeProperty))
        # No rdfs:domain → abstract, excluded from notes
        sg = extract_subgraph(g2, {s("Admission")}, {str(abstract)})
        assert not any("hasAbstractValue" in n for n in sg.adjacency_notes)

    def test_no_provenance_note_omits_from_clause(self, graph):
        g2 = rdflib.Graph()
        for triple in graph:
            g2.add(triple)
        bare = URIRef(f"{BASE}hasBareProperty")
        g2.add((bare, RDF.type, OWL.DatatypeProperty))
        g2.add((bare, RDFS.domain, u("Admission")))
        sg = extract_subgraph(g2, {s("Admission")}, set())
        note = next(n for n in sg.adjacency_notes if "hasBareProperty" in n)
        assert "(from" not in note
        assert "(FK:" not in note
        assert "—" in note

    def test_object_property_without_fk_omits_fk_clause(self, graph):
        g2 = rdflib.Graph()
        for triple in graph:
            g2.add(triple)
        bare_op = URIRef(f"{BASE}hasBareObjectProp")
        g2.add((bare_op, RDF.type, OWL.ObjectProperty))
        g2.add((bare_op, RDFS.domain, u("Admission")))
        sg = extract_subgraph(g2, {s("Admission")}, set())
        note = next(n for n in sg.adjacency_notes if "hasBareObjectProp" in n)
        assert "has object property" in note
        assert "(FK:" not in note


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------

class TestPerformance:
    def test_extraction_under_100ms(self):
        """Extraction from a MIMIC-sized ontology completes in under 100ms."""
        ns = "http://example.org/large#"
        FM_L = Namespace("http://forgemesh.ai/ontology#")
        g = rdflib.Graph()

        for i in range(150):
            cls = URIRef(f"{ns}Class{i}")
            g.add((cls, RDF.type, OWL.Class))
            g.add((cls, RDFS.label, Literal(f"Class{i}", lang="en")))
            g.add((cls, FM_L.sourceTable, Literal(f"table{i}")))
            if i > 0:
                g.add((cls, RDFS.subClassOf, URIRef(f"{ns}Class{i - 1}")))

        for i in range(600):
            cls_idx = i % 150
            prop = URIRef(f"{ns}hasProp{i}")
            g.add((prop, RDF.type, OWL.DatatypeProperty))
            g.add((prop, RDFS.domain, URIRef(f"{ns}Class{cls_idx}")))
            g.add((prop, FM_L.sourceColumn, Literal(f"table{cls_idx}.col{i}")))
            g.add((prop, FM_L.sqlType, Literal("INT")))
            g.add((prop, FM_L.nullRate, Literal("0.00")))
            g.add((prop, RDFS.range, XSD.integer))

        seed = {f"{ns}Class0", f"{ns}Class1", f"{ns}Class5"}

        start = time.monotonic()
        sg = extract_subgraph(g, seed, set())
        elapsed_ms = (time.monotonic() - start) * 1000

        assert elapsed_ms < 100, f"Extraction took {elapsed_ms:.1f}ms (limit: 100ms)"
        assert len(sg.classes) > 0
        assert len(sg.properties) > 0
