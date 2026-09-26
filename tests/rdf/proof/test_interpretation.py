# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 5 tests: Interpretation submodule."""
from __future__ import annotations

from semantic_mapper.rdf.proof.interpretation import build_interpretation
from tests.rdf.proof.conftest import (
    FIRST_NAME_URI,
    HAS_ENCOUNTER_URI,
    PATIENT_URI,
    NS,
)


class TestBuildInterpretation:
    def test_concepts_mapped_from_annotations(self, ontology):
        # "patient" appears in fm:interpretation on PATIENT_URI
        interp = build_interpretation(
            nl_question="How many patients are there?",
            sparql_class_uris=[PATIENT_URI],
            sparql_property_uris=[],
            ontology_graph=ontology,
        )
        assert len(interp.interpreted_concepts) >= 1
        uris = {c.mapped_to_uri for c in interp.interpreted_concepts}
        assert PATIENT_URI in uris

    def test_direct_form_when_fm_interpretation_present(self, ontology):
        interp = build_interpretation(
            nl_question="patient encounter",
            sparql_class_uris=[PATIENT_URI],
            sparql_property_uris=[HAS_ENCOUNTER_URI],
            ontology_graph=ontology,
        )
        forms = {c.mapping_form for c in interp.interpreted_concepts}
        # fm:interpretation is present on PATIENT_URI → DIRECT
        assert "DIRECT" in forms

    def test_pattern_form_for_rdfs_comment_match(self, ontology):
        # firstName has only rdfs:comment (no fm:interpretation)
        # "given" appears in rdfs:comment "The given name of the patient."
        interp = build_interpretation(
            nl_question="Show patient given names",
            sparql_class_uris=[],
            sparql_property_uris=[FIRST_NAME_URI],
            ontology_graph=ontology,
        )
        first_name_concepts = [c for c in interp.interpreted_concepts
                               if c.mapped_to_uri == FIRST_NAME_URI]
        assert len(first_name_concepts) >= 1
        assert first_name_concepts[0].mapping_form == "PATTERN"

    def test_high_confidence_when_all_fm_annotated(self, ontology):
        # PATIENT_URI and HAS_ENCOUNTER_URI both have fm:interpretation
        interp = build_interpretation(
            nl_question="patient encounter",
            sparql_class_uris=[PATIENT_URI],
            sparql_property_uris=[HAS_ENCOUNTER_URI],
            ontology_graph=ontology,
        )
        assert interp.confidence == "HIGH"

    def test_medium_confidence_with_pattern_match(self, ontology):
        interp = build_interpretation(
            nl_question="Show patient given names",
            sparql_class_uris=[],
            sparql_property_uris=[FIRST_NAME_URI],
            ontology_graph=ontology,
        )
        assert interp.confidence in ("MEDIUM", "LOW")

    def test_low_confidence_no_ontology(self):
        interp = build_interpretation(
            nl_question="How many patients?",
            sparql_class_uris=[PATIENT_URI],
            sparql_property_uris=[],
            ontology_graph=None,
        )
        assert interp.confidence == "LOW"

    def test_low_confidence_uri_absent_from_ontology(self, ontology):
        interp = build_interpretation(
            nl_question="unknown concept",
            sparql_class_uris=["http://ont/UnknownClass"],
            sparql_property_uris=[],
            ontology_graph=ontology,
        )
        assert interp.confidence == "LOW"

    def test_nl_question_preserved(self, ontology):
        q = "How many patients had cardiac diagnoses?"
        interp = build_interpretation(
            nl_question=q,
            sparql_class_uris=[PATIENT_URI],
            sparql_property_uris=[],
            ontology_graph=ontology,
        )
        assert interp.nl_question == q
