# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 4 tests: Limitations submodule."""
from __future__ import annotations

from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, URIRef
from rdflib.namespace import XSD

from semantic_mapper.rdf.proof._fm import FM
from semantic_mapper.rdf.proof.limitations import build_limitations
from tests.rdf.proof.conftest import (
    HAS_ENCOUNTER_URI,
    PATIENT_URI,
    ENCOUNTER_URI,
)


class TestBuildLimitations:
    def test_open_world_emitted_for_object_property(self, ontology):
        lims = build_limitations(
            queried_class_uris=[PATIENT_URI],
            constrained_property_uris=[],
            ontology_graph=ontology,
        )
        kinds = {l.kind for l in lims}
        assert "OPEN_WORLD" in kinds
        open_world = [l for l in lims if l.kind == "OPEN_WORLD"]
        # patientHasEncounter is an object property on Patient
        assert any(HAS_ENCOUNTER_URI in (l.grounding_node_uri or "") for l in open_world)

    def test_open_world_text_mentions_absence(self, ontology):
        lims = build_limitations(
            queried_class_uris=[PATIENT_URI],
            constrained_property_uris=[],
            ontology_graph=ontology,
        )
        texts = " ".join(l.text for l in lims if l.kind == "OPEN_WORLD")
        assert "absence" in texts.lower() or "not" in texts.lower()

    def test_annotated_from_fm_limitation(self, ontology):
        lims = build_limitations(
            queried_class_uris=[PATIENT_URI],
            constrained_property_uris=[],
            ontology_graph=ontology,
        )
        annotated = [l for l in lims if l.kind == "ANNOTATED"]
        assert len(annotated) >= 1
        # conftest adds "Patient records are limited to those admitted to this facility."
        assert any("admitted" in l.text for l in annotated)

    def test_cardinality_limitation(self):
        # Build a custom ontology with an owl:minCardinality restriction
        g = Graph()
        cls = URIRef("http://ont/Thing")
        prop = URIRef("http://ont/hasPart")
        g.add((cls, RDF.type, OWL.Class))
        g.add((prop, RDF.type, OWL.ObjectProperty))
        restriction = BNode()
        g.add((restriction, RDF.type, OWL.Restriction))
        g.add((restriction, OWL.onProperty, prop))
        g.add((restriction, OWL.minCardinality, Literal(1, datatype=XSD.nonNegativeInteger)))
        g.add((cls, RDFS.subClassOf, restriction))

        lims = build_limitations(
            queried_class_uris=["http://ont/Thing"],
            constrained_property_uris=[],
            ontology_graph=g,
        )
        cardinality_lims = [l for l in lims if l.kind == "CARDINALITY"]
        assert len(cardinality_lims) >= 1
        assert any("minimum cardinality" in l.text.lower() for l in cardinality_lims)

    def test_no_ontology_returns_single_open_world(self):
        lims = build_limitations(
            queried_class_uris=[PATIENT_URI],
            constrained_property_uris=[],
            ontology_graph=None,
        )
        assert len(lims) == 1
        assert lims[0].kind == "OPEN_WORLD"

    def test_deduplication(self, ontology):
        # Calling with the same class twice should not duplicate
        lims = build_limitations(
            queried_class_uris=[PATIENT_URI, PATIENT_URI],
            constrained_property_uris=[],
            ontology_graph=ontology,
        )
        texts = [l.text for l in lims]
        assert len(texts) == len(set(texts))
