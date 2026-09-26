# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef
from rdflib.namespace import SKOS

from semantic_mapper.ontology.iterative.graph_merger import merge

BASE = "http://mesh.local/ontology/test#"


def _class_graph(uri: str, label: str = "") -> Graph:
    g = Graph()
    g.add((URIRef(uri), RDF.type, OWL.Class))
    if label:
        g.add((URIRef(uri), RDFS.label, Literal(label, lang="en")))
    return g


def _prop_graph(uri: str, prop_type=OWL.DatatypeProperty) -> Graph:
    g = Graph()
    g.add((URIRef(uri), RDF.type, prop_type))
    g.add((URIRef(uri), RDFS.label, Literal("test prop", lang="en")))
    g.add((URIRef(uri), SKOS.altLabel, Literal("source_col", lang="en")))
    return g


class TestMerge:
    def test_new_class_added(self):
        acc = Graph()
        frag = _class_graph(f"{BASE}Patient", "Patient")
        result = merge(acc, frag, "raw_patients", BASE)
        assert (URIRef(f"{BASE}Patient"), RDF.type, OWL.Class) in acc
        assert len(result.new_classes) == 1
        assert result.new_classes[0].local_name == "Patient"

    def test_duplicate_class_skipped(self):
        acc = Graph()
        acc.add((URIRef(f"{BASE}Patient"), RDF.type, OWL.Class))
        frag = _class_graph(f"{BASE}Patient", "Patient")
        result = merge(acc, frag, "raw_patients", BASE)
        assert len(result.new_classes) == 0
        assert f"{BASE}Patient" in result.skipped_duplicates

    def test_annotation_merged_on_duplicate(self):
        """rdfs:label should be added even when the class already exists."""
        acc = Graph()
        acc.add((URIRef(f"{BASE}Patient"), RDF.type, OWL.Class))
        frag = _class_graph(f"{BASE}Patient", "Patient")
        merge(acc, frag, "raw_patients", BASE)
        labels = list(acc.objects(URIRef(f"{BASE}Patient"), RDFS.label))
        assert any("Patient" in str(l) for l in labels)

    def test_type_conflict_warning(self):
        acc = Graph()
        acc.add((URIRef(f"{BASE}MyProp"), RDF.type, OWL.DatatypeProperty))
        frag = Graph()
        frag.add((URIRef(f"{BASE}MyProp"), RDF.type, OWL.ObjectProperty))
        result = merge(acc, frag, "t", BASE)
        assert len(result.conflict_warnings) == 1

    def test_new_datatype_property_merged(self):
        acc = Graph()
        frag = _prop_graph(f"{BASE}hasValue")
        result = merge(acc, frag, "t", BASE)
        assert (URIRef(f"{BASE}hasValue"), RDF.type, OWL.DatatypeProperty) in acc
        # DatatypeProperties are not tracked as new_classes
        assert len(result.new_classes) == 0

    def test_skos_alt_label_preserved(self):
        acc = Graph()
        frag = _prop_graph(f"{BASE}hasValue")
        merge(acc, frag, "t", BASE)
        alt = list(acc.objects(URIRef(f"{BASE}hasValue"), SKOS.altLabel))
        assert any("source_col" in str(a) for a in alt)

    def test_multiple_classes(self):
        acc = Graph()
        frag = Graph()
        frag.add((URIRef(f"{BASE}A"), RDF.type, OWL.Class))
        frag.add((URIRef(f"{BASE}B"), RDF.type, OWL.Class))
        result = merge(acc, frag, "t", BASE)
        assert len(result.new_classes) == 2

    def test_accumulated_class_summary_fields(self):
        acc = Graph()
        frag = _class_graph(f"{BASE}Patient", "Patient")
        result = merge(acc, frag, "raw_patients", BASE)
        summary = result.new_classes[0]
        assert summary.uri == f"{BASE}Patient"
        assert summary.source_tables == ["raw_patients"]
        assert summary.label == "Patient"

    def test_comment_not_duplicated_on_shared_class(self):
        """When the same class is merged from two fragments with different rdfs:comment, only
        the first comment is kept (keep-first policy)."""
        cls = URIRef(f"{BASE}SharedClass")
        acc = Graph()
        frag_a = Graph()
        frag_a.add((cls, RDF.type, OWL.Class))
        frag_a.add((cls, RDFS.comment, Literal("First comment.", lang="en")))
        merge(acc, frag_a, "table_a", BASE)

        frag_b = Graph()
        frag_b.add((cls, RDF.type, OWL.Class))
        frag_b.add((cls, RDFS.comment, Literal("Second comment.", lang="en")))
        merge(acc, frag_b, "table_b", BASE)

        comments = list(acc.objects(cls, RDFS.comment))
        assert len(comments) == 1, f"Expected 1 comment, got {len(comments)}: {comments}"
        assert str(comments[0]) == "First comment."

    def test_label_not_duplicated_on_shared_class(self):
        """rdfs:label also uses keep-first when the class is already in the accumulating graph."""
        cls = URIRef(f"{BASE}SharedClass")
        acc = Graph()
        frag_a = Graph()
        frag_a.add((cls, RDF.type, OWL.Class))
        frag_a.add((cls, RDFS.label, Literal("First label", lang="en")))
        merge(acc, frag_a, "table_a", BASE)

        frag_b = Graph()
        frag_b.add((cls, RDF.type, OWL.Class))
        frag_b.add((cls, RDFS.label, Literal("Second label", lang="en")))
        merge(acc, frag_b, "table_b", BASE)

        labels = list(acc.objects(cls, RDFS.label))
        assert len(labels) == 1, f"Expected 1 label, got {len(labels)}: {labels}"
        assert str(labels[0]) == "First label"

    def test_alt_label_always_merged(self):
        """skos:altLabel is always merged — different values from different tables both survive."""
        cls = URIRef(f"{BASE}SharedClass")
        acc = Graph()
        frag_a = Graph()
        frag_a.add((cls, RDF.type, OWL.Class))
        frag_a.add((cls, SKOS.altLabel, Literal("alias_one", lang="en")))
        merge(acc, frag_a, "table_a", BASE)

        frag_b = Graph()
        frag_b.add((cls, RDF.type, OWL.Class))
        frag_b.add((cls, SKOS.altLabel, Literal("alias_two", lang="en")))
        merge(acc, frag_b, "table_b", BASE)

        alt_labels = {str(a) for a in acc.objects(cls, SKOS.altLabel)}
        assert "alias_one" in alt_labels
        assert "alias_two" in alt_labels
