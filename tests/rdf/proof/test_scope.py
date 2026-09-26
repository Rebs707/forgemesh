# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 3 tests: Scope assumption submodule."""
from __future__ import annotations

from semantic_mapper.rdf.proof.scope import build_scope_assumptions
from tests.rdf.proof.conftest import (
    FIRST_NAME_URI,
    GENDER_URI,
    HAS_ENCOUNTER_URI,
    PATIENT_URI,
)


class TestBuildScopeAssumptions:
    def test_unconstrained_properties_included(self, ontology):
        # constrain only firstName; gender and patientHasEncounter should appear
        assumptions = build_scope_assumptions(
            queried_class_uris=[PATIENT_URI],
            constrained_property_uris={FIRST_NAME_URI},
            ontology_graph=ontology,
        )
        prop_uris = {a.unconstrained_property_uri for a in assumptions}
        assert GENDER_URI in prop_uris
        assert HAS_ENCOUNTER_URI in prop_uris

    def test_constrained_properties_excluded(self, ontology):
        assumptions = build_scope_assumptions(
            queried_class_uris=[PATIENT_URI],
            constrained_property_uris={FIRST_NAME_URI},
            ontology_graph=ontology,
        )
        prop_uris = {a.unconstrained_property_uri for a in assumptions}
        assert FIRST_NAME_URI not in prop_uris

    def test_implied_inclusion_text(self, ontology):
        assumptions = build_scope_assumptions(
            queried_class_uris=[PATIENT_URI],
            constrained_property_uris=set(),
            ontology_graph=ontology,
        )
        assert all("all values" in a.implied_inclusion for a in assumptions)

    def test_fm_scope_default_populated(self, ontology):
        # patientHasEncounter has fm:scope_default in conftest
        assumptions = build_scope_assumptions(
            queried_class_uris=[PATIENT_URI],
            constrained_property_uris=set(),
            ontology_graph=ontology,
        )
        enc_assumption = next(
            (a for a in assumptions if a.unconstrained_property_uri == HAS_ENCOUNTER_URI),
            None,
        )
        assert enc_assumption is not None
        assert enc_assumption.annotated_default is not None
        assert "All encounter types" in enc_assumption.annotated_default

    def test_no_ontology_returns_empty(self):
        result = build_scope_assumptions(
            queried_class_uris=[PATIENT_URI],
            constrained_property_uris=set(),
            ontology_graph=None,
        )
        assert result == []

    def test_no_class_returns_empty(self, ontology):
        result = build_scope_assumptions(
            queried_class_uris=[],
            constrained_property_uris=set(),
            ontology_graph=ontology,
        )
        assert result == []

    def test_all_constrained_returns_empty(self, ontology):
        result = build_scope_assumptions(
            queried_class_uris=[PATIENT_URI],
            constrained_property_uris={FIRST_NAME_URI, GENDER_URI, HAS_ENCOUNTER_URI},
            ontology_graph=ontology,
        )
        assert result == []

    def test_property_label_humanised(self, ontology):
        assumptions = build_scope_assumptions(
            queried_class_uris=[PATIENT_URI],
            constrained_property_uris=set(),
            ontology_graph=ontology,
        )
        # gender property has rdfs:label "gender"
        gender_a = next(
            (a for a in assumptions if a.unconstrained_property_uri == GENDER_URI), None
        )
        assert gender_a is not None
        assert "gender" in gender_a.property_label.lower()
