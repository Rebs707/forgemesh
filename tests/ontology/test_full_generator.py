# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for full_generator — issues #42 (property naming), #43 (subclass inference), #44 (obj prop hierarchy)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from semantic_mapper.config.settings import Settings
from semantic_mapper.db.models import ColumnProfile, TableProfile
from semantic_mapper.ontology.full_generator import (
    FullOntologyGenerator,
    _build_discriminator_section,
    _qualify_generic_props,
    _qualify_obj_props,
    _supplement_obj_props,
    _detect_categoricals,
    _DataProp,
    _StructObjectProp,
    _StructureSpec,
    _StructClass,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def settings(tmp_path):
    return Settings(
        DATABASE_URL="mysql+pymysql://user:pass@localhost/test_db",
        ONTOLOGY_PATH="",
        OPENAI_API_KEY="test-key",
        LLM_STREAM=False,  # mocks return a single (non-streamed) response
        SEED_OUTPUT_PATH=str(tmp_path / "seed.owl"),
    )


def _col(name, inferred_type="VARCHAR(36)", pk=False, nullable=False,
         distinct=50, top_values=None):
    return ColumnProfile(
        name=name, inferred_type=inferred_type,
        is_nullable=nullable, is_primary_key=pk,
        null_rate=0.0, distinct_count=distinct,
        top_values=top_values or [],
    )


@pytest.fixture()
def encounter_profile():
    return TableProfile(
        table_name="encounters",
        row_count=500,
        columns={
            "id":    _col("id", pk=True),
            "code":  _col("code", distinct=200),
            "patient_id": _col("patient_id"),
        },
        foreign_keys=[{"column": "patient_id", "ref_table": "patients"}],
    )


@pytest.fixture()
def observation_profile():
    return TableProfile(
        table_name="observations",
        row_count=2000,
        columns={
            "id":    _col("id", pk=True),
            "code":  _col("code", distinct=100),
            "value": _col("value"),
        },
        foreign_keys=[],
    )


@pytest.fixture()
def party_profile():
    """Table with discriminator column for subclass inference."""
    return TableProfile(
        table_name="parties",
        row_count=300,
        columns={
            "id":   _col("id", pk=True),
            "party_type": _col(
                "party_type", inferred_type="VARCHAR(20)",
                nullable=False, distinct=2,
                top_values=[
                    {"value": "Person", "count": 200},
                    {"value": "Organisation", "count": 100},
                ],
            ),
            "name": _col("name", distinct=300),
        },
        foreign_keys=[],
    )


# ── Issue #42: _qualify_generic_props ─────────────────────────────────────────

def _dp(local_name, label="", comment="", xsd_range="string", source_column="t.c"):
    return _DataProp(local_name=local_name, label=label, comment=comment,
                     xsd_range=xsd_range, source_column=source_column)


class TestQualifyGenericProps:
    def test_single_domain_generic_gets_prefixed(self):
        class_props = {
            "Encounter": [_dp("hasCode", source_column="encounters.code")],
        }
        result, abstracts = _qualify_generic_props(class_props)
        assert result["Encounter"][0].local_name == "hasEncounterCode"
        assert "hasCode" in abstracts

    def test_multi_domain_generic_each_prefixed_with_own_class(self):
        """hasCode in two classes must be qualified per-class to avoid URI collision.

        The old behaviour (leave both as hasCode) caused the deduplicator in _write_owl
        to silently drop all but the first occurrence, so only one class's hasCode was
        ever written to the OWL.  Both must be prefixed with their own class name.
        """
        class_props = {
            "Encounter":   [_dp("hasCode", source_column="encounters.code")],
            "Observation": [_dp("hasCode", source_column="observations.code")],
        }
        result, abstracts = _qualify_generic_props(class_props)
        assert result["Encounter"][0].local_name == "hasEncounterCode"
        assert result["Observation"][0].local_name == "hasObservationCode"
        # One abstract parent recorded (first class wins)
        assert "hasCode" in abstracts

    def test_non_generic_single_domain_unchanged(self):
        class_props = {
            "Encounter": [_dp("hasDiagnosisCode", source_column="encounters.diagnosis_code")],
        }
        result, abstracts = _qualify_generic_props(class_props)
        assert result["Encounter"][0].local_name == "hasDiagnosisCode"
        assert len(abstracts) == 0

    def test_all_generic_terms_get_prefixed(self):
        generic_terms = ["type", "status", "class", "name", "date", "id",
                         "description", "reason", "cost", "amount"]
        for term in generic_terms:
            class_props = {
                "Patient": [_dp(f"has{term.capitalize()}", source_column=f"patients.{term}")],
            }
            result, _ = _qualify_generic_props(class_props)
            local = result["Patient"][0].local_name
            assert local.startswith("hasPatient"), (
                f"Expected 'hasPatient...' for generic term '{term}', got '{local}'"
            )

    def test_multiple_props_mixed(self):
        class_props = {
            "Encounter": [
                _dp("hasCode"),           # generic → prefixed
                _dp("hasDescription"),    # generic → prefixed
                _dp("hasStartDate"),      # temporal multi-word → prefixed
                _dp("hasStartTime"),      # temporal multi-word → prefixed
                _dp("hasLabel"),          # non-generic → unchanged (not in fragments)
            ],
        }
        result, abstracts = _qualify_generic_props(class_props)
        locals_ = [p.local_name for p in result["Encounter"]]
        assert "hasEncounterCode" in locals_
        assert "hasEncounterDescription" in locals_
        assert "hasEncounterStartDate" in locals_
        assert "hasEncounterStartTime" in locals_
        assert "hasLabel" in locals_   # non-generic: unchanged
        # Abstract parents recorded for each qualified generic
        assert "hasCode" in abstracts
        assert "hasDescription" in abstracts
        assert "hasStartDate" in abstracts
        assert "hasStartTime" in abstracts
        assert "hasLabel" not in abstracts  # non-generic: no abstract parent


# ── Issue #43: _build_discriminator_section ───────────────────────────────────

class TestBuildDiscriminatorSection:
    def test_discriminator_column_included(self, party_profile):
        section = _build_discriminator_section([party_profile])
        assert "party_type" in section
        assert "Person" in section
        assert "Organisation" in section

    def test_no_discriminators_returns_empty(self, encounter_profile):
        # encounters.code has distinct=200 (> 10 threshold)
        section = _build_discriminator_section([encounter_profile])
        assert section == ""

    def test_pk_excluded(self):
        profile = TableProfile(
            table_name="t", row_count=10,
            columns={
                "id": _col("id", pk=True, distinct=10,
                            top_values=[{"value": "a", "count": 5}]),
            },
            foreign_keys=[],
        )
        section = _build_discriminator_section([profile])
        assert section == ""

    def test_nullable_excluded(self):
        profile = TableProfile(
            table_name="t", row_count=10,
            columns={
                "status": _col("status", nullable=True, distinct=3,
                               top_values=[{"value": "a", "count": 5},
                                           {"value": "b", "count": 3},
                                           {"value": "c", "count": 2}]),
            },
            foreign_keys=[],
        )
        section = _build_discriminator_section([profile])
        assert section == ""

    def test_high_cardinality_excluded(self, encounter_profile):
        """columns with distinct > 10 are not discriminators."""
        section = _build_discriminator_section([encounter_profile])
        assert section == ""


# ── Integration: subclass relationships written to OWL graph ──────────────────

def _make_llm_mock(structure_json: dict, props_json: dict):
    """Return a mock OpenAI client that returns the given JSON objects."""
    mock_client = MagicMock()

    def _completion(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        # Choose which response to return based on prompt content
        if "subclass_relationships" in prompt:
            content = json.dumps(structure_json)
        else:
            content = json.dumps(props_json)
        resp = MagicMock()
        resp.choices[0].message.content = content
        resp.usage.prompt_tokens = 10
        resp.usage.completion_tokens = 10
        return resp

    mock_client.chat.completions.create.side_effect = _completion
    return mock_client


class TestSubclassInOWL:
    def test_subclass_triples_written(self, settings, party_profile, tmp_path):
        structure = {
            "reasoning": "test",
            "base_iri": "http://test.org/",
            "classes": [
                {"local_name": "Party", "label": "Party", "comment": "", "source_tables": ["parties"]},
                {"local_name": "Person", "label": "Person", "comment": "", "source_tables": []},
                {"local_name": "Organisation", "label": "Organisation", "comment": "", "source_tables": []},
            ],
            "object_properties": [],
            "subclass_relationships": [
                {
                    "child_class": "Person",
                    "parent_class": "Party",
                    "discriminator_column": "parties.party_type",
                    "discriminator_value": "Person",
                },
                {
                    "child_class": "Organisation",
                    "parent_class": "Party",
                    "discriminator_column": "parties.party_type",
                    "discriminator_value": "Organisation",
                },
            ],
        }
        props_response = {
            "class_local_name": "Party",
            "datatype_properties": [
                {"local_name": "hasName", "label": "Name", "comment": "",
                 "xsd_range": "string", "source_column": "parties.name"},
            ],
        }

        gen = FullOntologyGenerator(settings)
        mock_client = _make_llm_mock(structure, props_response)

        with patch("openai.OpenAI", return_value=mock_client):
            out_path = gen.generate([party_profile])

        g = Graph()
        g.parse(str(out_path), format="xml")

        ns_base = "http://test.org/"
        person_uri  = URIRef(f"{ns_base}Person")
        org_uri     = URIRef(f"{ns_base}Organisation")
        party_uri   = URIRef(f"{ns_base}Party")

        assert (person_uri,  RDFS.subClassOf, party_uri) in g, "Person subClassOf Party missing"
        assert (org_uri,     RDFS.subClassOf, party_uri) in g, "Organisation subClassOf Party missing"

    def test_subclass_creates_missing_parent(self, settings, party_profile, tmp_path):
        """Parent class declared in subclass_relationships but absent from classes list is auto-created.

        _fix_subclass_parents will remove the subClassOf link when the discriminator column
        belongs to the child's own source table (Person discriminated from parties.party_type
        → parties is Person's table, so the parent link is corrected away). The parent class
        is still auto-created as an owl:Class; it just won't have Person as a subclass.
        """
        structure = {
            "reasoning": "test",
            "base_iri": "http://test.org/",
            "classes": [
                {"local_name": "Person", "label": "Person", "comment": "", "source_tables": ["parties"]},
            ],
            "object_properties": [],
            "subclass_relationships": [
                {
                    "child_class": "Person",
                    "parent_class": "LegalEntity",   # not in classes list — will be auto-created
                    # discriminator_column references a table not owned by any class,
                    # so _fix_subclass_parents leaves the parent unchanged
                    "discriminator_column": "type_lookup.party_type",
                    "discriminator_value": "Person",
                },
            ],
        }
        props_response = {
            "class_local_name": "Person",
            "datatype_properties": [],
        }
        gen = FullOntologyGenerator(settings)
        mock_client = _make_llm_mock(structure, props_response)

        with patch("openai.OpenAI", return_value=mock_client):
            out_path = gen.generate([party_profile])

        g = Graph()
        g.parse(str(out_path), format="xml")
        ns_base = "http://test.org/"
        person_uri       = URIRef(f"{ns_base}Person")
        legal_entity_uri = URIRef(f"{ns_base}LegalEntity")

        # Parent class is auto-created even if the subclass link is later corrected away
        assert (legal_entity_uri, RDF.type, OWL.Class) in g
        # Person exists as a class
        assert (person_uri, RDF.type, OWL.Class) in g


# ── Integration: generic property names qualified in OWL output ───────────────

class TestGenericPropQualificationInOWL:
    def test_hasCode_qualified_to_hasEncounterCode(self, settings, encounter_profile, tmp_path):
        structure = {
            "reasoning": "test",
            "base_iri": "http://test.org/",
            "classes": [
                {"local_name": "Encounter", "label": "Encounter", "comment": "",
                 "source_tables": ["encounters"]},
            ],
            "object_properties": [],
            "subclass_relationships": [],
        }
        props_response = {
            "class_local_name": "Encounter",
            "datatype_properties": [
                {"local_name": "hasCode", "label": "Code", "comment": "",
                 "xsd_range": "string", "source_column": "encounters.code"},
            ],
        }
        gen = FullOntologyGenerator(settings)
        mock_client = _make_llm_mock(structure, props_response)

        with patch("openai.OpenAI", return_value=mock_client):
            out_path = gen.generate([encounter_profile])

        g = Graph()
        g.parse(str(out_path), format="xml")
        ns_base = "http://test.org/"

        qualified_uri = URIRef(f"{ns_base}hasEncounterCode")
        abstract_uri  = URIRef(f"{ns_base}hasCode")
        encounter_uri = URIRef(f"{ns_base}Encounter")

        assert (qualified_uri, RDF.type, OWL.DatatypeProperty) in g, \
            "hasEncounterCode should be in graph"
        # Abstract parent is written (domain-free) for rdfs:subPropertyOf target
        assert (abstract_uri, RDF.type, OWL.DatatypeProperty) in g, \
            "hasCode abstract parent should be in graph"
        # Abstract parent must have NO domain assertion
        assert (abstract_uri, RDFS.domain, None) not in g, \
            "hasCode abstract parent must not have a domain"
        # Qualified property links to abstract parent via rdfs:subPropertyOf
        assert (qualified_uri, RDFS.subPropertyOf, abstract_uri) in g, \
            "hasEncounterCode rdfs:subPropertyOf hasCode missing"
        # Qualified property has the concrete domain
        assert (qualified_uri, RDFS.domain, encounter_uri) in g, \
            "hasEncounterCode should have Encounter as domain"


# ── Issue #44: _qualify_obj_props ─────────────────────────────────────────────

def _op(local_name, domain, range_, inverse="", inv_label="", source_fk=""):
    return _StructObjectProp(
        local_name=local_name, label=local_name, comment="",
        domain=domain, range=range_,
        source_fk=source_fk,
        inverse_local_name=inverse, inverse_label=inv_label,
    )


class TestQualifyObjProps:
    def test_single_domain_unchanged(self):
        """Only one source → simple name kept; no abstract parent created."""
        props = [_op("hasMedication", "Encounter", "Medication", inverse="isMedicationOf")]
        qualified, fwd_abstracts, inv_abstracts = _qualify_obj_props(props)
        assert len(qualified) == 1
        assert qualified[0].local_name == "hasMedication"
        assert len(fwd_abstracts) == 0
        assert len(inv_abstracts) == 0

    def test_multi_domain_qualified(self):
        """Two sources sharing Medication range → both renamed, abstract fwd+inv parents created."""
        props = [
            _op("hasMedication", "Encounter", "Medication", inverse="isMedicationOf"),
            _op("hasMedication", "Patient",   "Medication", inverse="isMedicationOf"),
        ]
        qualified, fwd_abstracts, inv_abstracts = _qualify_obj_props(props)

        names = {p.local_name for p in qualified}
        assert "hasMedicationForEncounter" in names
        assert "hasMedicationForPatient" in names
        assert "hasMedication" not in names  # no unqualified version

        assert len(fwd_abstracts) == 1
        assert fwd_abstracts[0].local_name == "hasMedication"
        assert fwd_abstracts[0].domain == ""         # no domain on abstract fwd
        assert fwd_abstracts[0].range == "Medication"
        assert fwd_abstracts[0].inverse_local_name == "isMedicationOf"

        assert len(inv_abstracts) == 1
        assert inv_abstracts[0].local_name == "isMedicationOf"
        assert inv_abstracts[0].domain == "Medication"  # domain constrained on abstract inv
        assert inv_abstracts[0].range == ""
        assert inv_abstracts[0].inverse_local_name == "hasMedication"

    def test_inverse_qualified_symmetrically(self):
        """Inverse is renamed is{Range}Of{Domain} when forward is qualified."""
        props = [
            _op("hasMedication", "Encounter", "Medication", inverse="isMedicationOf"),
            _op("hasMedication", "Patient",   "Medication", inverse="isMedicationOf"),
        ]
        qualified, _, _ = _qualify_obj_props(props)
        inv_names = {p.inverse_local_name for p in qualified}
        assert "isMedicationOfEncounter" in inv_names
        assert "isMedicationOfPatient" in inv_names

    def test_no_inverse_handled(self):
        """Props without an inverse do not get a spurious inverse_local_name."""
        props = [
            _op("hasPatient", "Encounter", "Patient"),
            _op("hasPatient", "Procedure", "Patient"),
        ]
        qualified, _, _ = _qualify_obj_props(props)
        for p in qualified:
            assert p.inverse_local_name == ""

    def test_different_ranges_independent(self):
        """Two properties with different ranges are handled independently."""
        props = [
            _op("hasMedication", "Encounter", "Medication", inverse="isMedicationOf"),
            _op("hasMedication", "Patient",   "Medication", inverse="isMedicationOf"),
            _op("hasProvider",   "Encounter", "Provider",   inverse="isProviderOf"),
        ]
        qualified, fwd_abstracts, inv_abstracts = _qualify_obj_props(props)

        names = {p.local_name for p in qualified}
        # Medication props qualified; Provider left alone (single domain)
        assert "hasMedicationForEncounter" in names
        assert "hasMedicationForPatient" in names
        assert "hasProvider" in names

        fwd_names = {a.local_name for a in fwd_abstracts}
        inv_names = {a.local_name for a in inv_abstracts}
        assert "hasMedication" in fwd_names
        assert "isMedicationOf" in inv_names
        assert "hasProvider" not in fwd_names   # single-domain, no abstract needed


# ── Integration: object property hierarchy written to OWL graph ───────────────

class TestObjPropHierarchyInOWL:
    def test_abstract_parent_and_subprops_written(self, settings, encounter_profile, tmp_path):
        """hasMedication abstract parent + hasMedicationForEncounter/Patient in OWL graph."""
        structure = {
            "reasoning": "test",
            "base_iri": "http://test.org/",
            "classes": [
                {"local_name": "Encounter",  "label": "Encounter",  "comment": "", "source_tables": ["encounters"]},
                {"local_name": "Patient",    "label": "Patient",    "comment": "", "source_tables": []},
                {"local_name": "Medication", "label": "Medication", "comment": "", "source_tables": []},
            ],
            "object_properties": [
                {"local_name": "hasMedication", "label": "Has Medication", "comment": "",
                 "domain": "Encounter", "range": "Medication",
                 "source_fk": "encounters.medication_id -> medications.id",
                 "inverse_local_name": "isMedicationOf", "inverse_label": ""},
                {"local_name": "hasMedication", "label": "Has Medication", "comment": "",
                 "domain": "Patient", "range": "Medication",
                 "source_fk": "patients.medication_id -> medications.id",
                 "inverse_local_name": "isMedicationOf", "inverse_label": ""},
            ],
            "subclass_relationships": [],
        }
        props_response = {
            "class_local_name": "Encounter",
            "datatype_properties": [],
        }
        gen = FullOntologyGenerator(settings)
        mock_client = _make_llm_mock(structure, props_response)

        with patch("openai.OpenAI", return_value=mock_client):
            out_path = gen.generate([encounter_profile])

        g = Graph()
        g.parse(str(out_path), format="xml")
        ns = "http://test.org/"

        abstract_uri   = URIRef(f"{ns}hasMedication")
        enc_prop_uri   = URIRef(f"{ns}hasMedicationForEncounter")
        pat_prop_uri   = URIRef(f"{ns}hasMedicationForPatient")
        medication_uri = URIRef(f"{ns}Medication")
        encounter_uri  = URIRef(f"{ns}Encounter")
        patient_uri    = URIRef(f"{ns}Patient")

        # Abstract parent exists as ObjectProperty with range but no domain
        assert (abstract_uri, RDF.type, OWL.ObjectProperty) in g
        assert (abstract_uri, RDFS.range, medication_uri) in g
        assert (abstract_uri, RDFS.domain, None) not in g

        # Qualified subproperties exist with correct domains
        assert (enc_prop_uri, RDF.type, OWL.ObjectProperty) in g
        assert (enc_prop_uri, RDFS.domain, encounter_uri) in g
        assert (pat_prop_uri, RDF.type, OWL.ObjectProperty) in g
        assert (pat_prop_uri, RDFS.domain, patient_uri) in g

        # Both declare subPropertyOf the abstract parent
        assert (enc_prop_uri, RDFS.subPropertyOf, abstract_uri) in g
        assert (pat_prop_uri, RDFS.subPropertyOf, abstract_uri) in g

        # Inverses are qualified symmetrically
        enc_inv_uri    = URIRef(f"{ns}isMedicationOfEncounter")
        pat_inv_uri    = URIRef(f"{ns}isMedicationOfPatient")
        abs_inv_uri    = URIRef(f"{ns}isMedicationOf")
        assert (enc_inv_uri, RDF.type, OWL.ObjectProperty) in g
        assert (pat_inv_uri, RDF.type, OWL.ObjectProperty) in g
        assert (enc_inv_uri, OWL.inverseOf, enc_prop_uri) in g
        assert (pat_inv_uri, OWL.inverseOf, pat_prop_uri) in g

        # Abstract inverse parent exists with domain=Medication, no range
        assert (abs_inv_uri, RDF.type, OWL.ObjectProperty) in g
        assert (abs_inv_uri, RDFS.domain, medication_uri) in g
        assert (abs_inv_uri, RDFS.range, None) not in g

        # Abstract fwd ↔ abstract inv linked via owl:inverseOf
        assert (abstract_uri, OWL.inverseOf, abs_inv_uri) in g
        assert (abs_inv_uri, OWL.inverseOf, abstract_uri) in g

        # Qualified inverses declare subPropertyOf abstract inverse
        assert (enc_inv_uri, RDFS.subPropertyOf, abs_inv_uri) in g
        assert (pat_inv_uri, RDFS.subPropertyOf, abs_inv_uri) in g


# ── _supplement_obj_props ─────────────────────────────────────────────────────

def _make_structure(classes, obj_props=None):
    return _StructureSpec(
        reasoning="test",
        base_iri="http://test.org/",
        classes=[_StructClass(local_name=c, label=c, comment="", source_tables=[t])
                 for c, t in classes.items()],
        object_properties=obj_props or [],
        subclass_relationships=[],
    )


class TestSupplementObjProps:
    def test_missing_pair_added(self):
        """LLM emits hasPatient only for Encounter; supplement adds it for Condition."""
        structure = _make_structure(
            {"Encounter": "encounters", "Condition": "conditions", "Patient": "patients"},
            obj_props=[_op("hasPatient", "Encounter", "Patient")],
        )
        # conditions table has patient_id FK
        condition_profile = TableProfile(
            table_name="conditions", row_count=100,
            columns={"id": _col("id", pk=True), "patient_id": _col("patient_id")},
            foreign_keys=[{"column": "patient_id", "ref_table": "patients"}],
        )
        encounter_profile = TableProfile(
            table_name="encounters", row_count=100,
            columns={"id": _col("id", pk=True), "patient_id": _col("patient_id")},
            foreign_keys=[{"column": "patient_id", "ref_table": "patients"}],
        )
        patient_profile = TableProfile(
            table_name="patients", row_count=50,
            columns={"id": _col("id", pk=True)},
            foreign_keys=[],
        )
        result = _supplement_obj_props(structure, [encounter_profile, condition_profile, patient_profile])

        pairs = {(p.domain, p.range) for p in result.object_properties}
        assert ("Encounter", "Patient") in pairs   # original
        assert ("Condition", "Patient") in pairs   # supplemented

    def test_existing_pair_not_duplicated(self):
        """Already-present (domain, range) pair is not added again."""
        structure = _make_structure(
            {"Encounter": "encounters", "Patient": "patients"},
            obj_props=[_op("hasPatient", "Encounter", "Patient")],
        )
        enc_profile = TableProfile(
            table_name="encounters", row_count=100,
            columns={"id": _col("id", pk=True), "patient_id": _col("patient_id")},
            foreign_keys=[{"column": "patient_id", "ref_table": "patients"}],
        )
        pat_profile = TableProfile(
            table_name="patients", row_count=50,
            columns={"id": _col("id", pk=True)},
            foreign_keys=[],
        )
        result = _supplement_obj_props(structure, [enc_profile, pat_profile])
        enc_patient = [p for p in result.object_properties
                       if p.domain == "Encounter" and p.range == "Patient"]
        assert len(enc_patient) == 1   # no duplicate

    def test_inferred_fk_supplemented(self):
        """Inferred FK (column name matches table name) also triggers supplement."""
        structure = _make_structure(
            {"Medication": "medications", "Patient": "patients"},
            obj_props=[],   # LLM emitted nothing
        )
        med_profile = TableProfile(
            table_name="medications", row_count=200,
            columns={
                "id": _col("id", pk=True),
                "patient_id": _col("patient_id"),  # inferred FK — no explicit FK entry
            },
            foreign_keys=[],
        )
        pat_profile = TableProfile(
            table_name="patients", row_count=50,
            columns={"id": _col("id", pk=True)},
            foreign_keys=[],
        )
        result = _supplement_obj_props(structure, [med_profile, pat_profile])
        pairs = {(p.domain, p.range) for p in result.object_properties}
        assert ("Medication", "Patient") in pairs

    def test_reverse_pair_replaces_llm_property(self):
        """LLM emits hasCondition (Patient→Condition); FK goes Condition→Patient.
        Supplement must REMOVE hasCondition and ADD (Condition→Patient) so the
        systematic is{Shared}Of naming hierarchy is complete."""
        structure = _make_structure(
            {"Patient": "patients", "Condition": "conditions"},
            obj_props=[_op("hasCondition", "Patient", "Condition")],
        )
        condition_profile = TableProfile(
            table_name="conditions", row_count=200,
            columns={"id": _col("id", pk=True), "patient_id": _col("patient_id")},
            foreign_keys=[{"column": "patient_id", "ref_table": "patients"}],
        )
        patient_profile = TableProfile(
            table_name="patients", row_count=50,
            columns={"id": _col("id", pk=True)},
            foreign_keys=[],
        )
        result = _supplement_obj_props(structure, [condition_profile, patient_profile])
        pairs = {(p.domain, p.range) for p in result.object_properties}
        # FK direction (Condition→Patient) must be present
        assert ("Condition", "Patient") in pairs
        # LLM's reverse (Patient→Condition) must be removed — replaced by the
        # inverse of hasPatientForCondition, i.e. isPatientOfCondition
        assert ("Patient", "Condition") not in pairs


# ── Issue #45: _detect_categoricals ──────────────────────────────────────────

class TestDetectCategoricals:
    def _make_profile(self, table, cols):
        return TableProfile(
            table_name=table, row_count=500,
            columns={n: c for n, c in cols},
            foreign_keys=[],
        )

    def test_low_cardinality_non_nullable_detected(self):
        profile = self._make_profile("patients", [
            ("id",     _col("id", pk=True)),
            ("gender", _col("gender", nullable=False, distinct=2,
                            top_values=[{"value": "M", "count": 300},
                                        {"value": "F", "count": 200}])),
        ])
        result = _detect_categoricals([profile], {}, threshold=10)
        assert "gender" in result.get("patients", {})

    def test_pk_excluded(self):
        profile = self._make_profile("t", [
            ("id", _col("id", pk=True, distinct=2,
                         top_values=[{"value": "a", "count": 1}])),
        ])
        result = _detect_categoricals([profile], {}, threshold=10)
        assert result == {}

    def test_nullable_included(self):
        """Nullable categoricals are valid — null means property simply not asserted."""
        profile = self._make_profile("t", [
            ("status", _col("status", nullable=True, distinct=3,
                            top_values=[{"value": "a", "count": 1},
                                        {"value": "b", "count": 1},
                                        {"value": "c", "count": 1}])),
        ])
        result = _detect_categoricals([profile], {}, threshold=10)
        assert "status" in result.get("t", {})

    def test_fk_col_excluded(self):
        profile = self._make_profile("t", [
            ("patient_id", _col("patient_id", distinct=2,
                                top_values=[{"value": "x", "count": 1}])),
        ])
        result = _detect_categoricals([profile], {"t": {"patient_id"}}, threshold=10)
        assert result == {}

    def test_high_cardinality_excluded(self):
        profile = self._make_profile("t", [
            ("code", _col("code", distinct=50,
                          top_values=[{"value": "a", "count": 1}])),
        ])
        result = _detect_categoricals([profile], {}, threshold=10)
        assert result == {}

    def test_no_top_values_excluded(self):
        profile = self._make_profile("t", [
            ("status", _col("status", distinct=3, top_values=[])),
        ])
        result = _detect_categoricals([profile], {}, threshold=10)
        assert result == {}


# ── Integration: categorical enumeration written to OWL graph ─────────────────

class TestCategoricalInOWL:
    def test_enumeration_class_and_individuals_written(self, settings, tmp_path):
        """gender column → Sex class + Male/Female individuals + hasBiologicalSex ObjectProperty."""
        structure = {
            "reasoning": "test",
            "base_iri": "http://test.org/",
            "classes": [
                {"local_name": "Patient", "label": "Patient", "comment": "",
                 "source_tables": ["patients"]},
            ],
            "object_properties": [],
            "subclass_relationships": [],
        }
        # Pass 2 returns no props (gender excluded as categorical)
        props_response = {"class_local_name": "Patient", "datatype_properties": []}
        # Pass 3 categorical response
        cat_response = {
            "categories": [{
                "class_local_name": "Patient",
                "property_local_name": "hasBiologicalSex",
                "property_label": "Biological Sex",
                "property_comment": "The biological sex of the patient.",
                "category_class_local_name": "Sex",
                "category_class_label": "Sex",
                "category_class_comment": "Enumeration of biological sex values.",
                "source_column": "patients.gender",
                "values": [
                    {"local_name": "Male",   "label": "Male",   "raw_value": "M"},
                    {"local_name": "Female", "label": "Female", "raw_value": "F"},
                ],
            }]
        }

        patient_profile = TableProfile(
            table_name="patients", row_count=500,
            columns={
                "id":     _col("id", pk=True),
                "gender": _col("gender", nullable=False, distinct=2,
                               top_values=[{"value": "M", "count": 300},
                                           {"value": "F", "count": 200}]),
            },
            foreign_keys=[],
        )

        gen = FullOntologyGenerator(settings)

        def _completion(**kwargs):
            prompt = kwargs["messages"][0]["content"]
            if "categories" in prompt or "enumeration" in prompt.lower():
                content = json.dumps(cat_response)
            elif "subclass_relationships" in prompt:
                content = json.dumps(structure)
            else:
                content = json.dumps(props_response)
            resp = MagicMock()
            resp.choices[0].message.content = content
            resp.usage.prompt_tokens = 5
            resp.usage.completion_tokens = 5
            return resp

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = _completion

        with patch("openai.OpenAI", return_value=mock_client):
            out_path = gen.generate([patient_profile])

        g = Graph()
        g.parse(str(out_path), format="xml")
        ns = "http://test.org/"

        sex_uri    = URIRef(f"{ns}Sex")
        male_uri   = URIRef(f"{ns}Male")
        female_uri = URIRef(f"{ns}Female")
        prop_uri   = URIRef(f"{ns}hasBiologicalSex")
        patient_uri = URIRef(f"{ns}Patient")

        # Enumeration class exists
        assert (sex_uri, RDF.type, OWL.Class) in g

        # Individuals exist and are typed to the class
        assert (male_uri,   RDF.type, OWL.NamedIndividual) in g
        assert (female_uri, RDF.type, OWL.NamedIndividual) in g
        assert (male_uri,   RDF.type, sex_uri) in g
        assert (female_uri, RDF.type, sex_uri) in g

        # ObjectProperty has correct domain and range
        assert (prop_uri, RDF.type, OWL.ObjectProperty) in g
        assert (prop_uri, RDFS.domain, patient_uri) in g
        assert (prop_uri, RDFS.range, sex_uri) in g

        # Property is NOT a DatatypeProperty
        assert (prop_uri, RDF.type, OWL.DatatypeProperty) not in g
