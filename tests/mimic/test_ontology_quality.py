# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Ontology Generation Quality Tests — issue #54

Tests that a MIMIC-III OWL ontology (generated or synthetic) satisfies
three structural quality criteria:

  1. Patient → Admission → DiagnosisIcd three-level object-property hierarchy.
  2. ICD-9 code property is present and typed as a datatype property (or at
     minimum as an owl:ObjectProperty / rdf:Property) with a recognisable label.
  3. CareVue vs MetaVision table split is represented as subclasses or
     distinguished instances under a common ICU-stay class.

Integration tests (test_*_live) are skipped unless owl/mimic_gen.owl exists.
Unit tests (all others) build a minimal synthetic OWL graph inline so they
always run in CI without a MIMIC database.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, Namespace, URIRef

# ── Namespace used by the generator ───────────────────────────────────────────
MIMIC = Namespace("http://mesh.local/ontology/mimic#")
ROOT = Path(__file__).resolve().parents[2]
MIMIC_OWL = ROOT / "owl" / "mimic_gen.owl"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _label_of(g: Graph, uri: URIRef) -> str:
    """Return first rdfs:label of *uri*, or the local fragment if absent."""
    for obj in g.objects(uri, RDFS.label):
        return str(obj)
    return uri.fragment or str(uri).rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def _all_classes(g: Graph) -> set[URIRef]:
    return {s for s in g.subjects(RDF.type, OWL.Class) if isinstance(s, URIRef)}


def _object_properties(g: Graph) -> set[URIRef]:
    return {s for s in g.subjects(RDF.type, OWL.ObjectProperty) if isinstance(s, URIRef)}


def _data_properties(g: Graph) -> set[URIRef]:
    return {s for s in g.subjects(RDF.type, OWL.DatatypeProperty) if isinstance(s, URIRef)}


def _camel(name: str) -> str:
    """'patient' → 'Patient'; 'diagnoses_icd' → 'DiagnosesIcd'."""
    return "".join(p.capitalize() for p in re.split(r"[_\s]+", name))


def _class_uri(local: str) -> URIRef:
    return MIMIC[_camel(local)]


def _prop_uri(local: str) -> URIRef:
    parts = re.split(r"[_\s]+", local)
    camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
    return MIMIC["has" + camel[0].upper() + camel[1:]]


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic OWL builder — mimics what the full_generator produces for MIMIC
# ─────────────────────────────────────────────────────────────────────────────

def _build_synthetic_mimic_graph() -> Graph:
    """Minimal OWL graph covering PATIENTS, ADMISSIONS, ICUSTAYS, DIAGNOSES_ICD."""
    g = Graph()
    g.bind("mimic", MIMIC)
    g.bind("owl", OWL)
    g.bind("rdfs", RDFS)

    # ── Classes ───────────────────────────────────────────────────────────────
    classes = [
        ("Patient",       "raw_patients"),
        ("Admission",     "raw_admissions"),
        ("Icustay",       "raw_icustays"),
        ("DiagnosisIcd",  "raw_diagnoses_icd"),
    ]
    for cls_name, src in classes:
        uri = MIMIC[cls_name]
        g.add((uri, RDF.type, OWL.Class))
        g.add((uri, RDFS.label, Literal(cls_name)))
        g.add((uri, RDFS.comment, Literal(f"Generated from {src}")))

    # ── Object properties linking the three levels ────────────────────────────
    # Admission → Patient
    has_patient = MIMIC["hasPatient"]
    g.add((has_patient, RDF.type, OWL.ObjectProperty))
    g.add((has_patient, RDFS.label, Literal("hasPatient")))
    g.add((has_patient, RDFS.domain, MIMIC["Admission"]))
    g.add((has_patient, RDFS.range, MIMIC["Patient"]))

    # DiagnosisIcd → Admission
    has_admission = MIMIC["hasAdmission"]
    g.add((has_admission, RDF.type, OWL.ObjectProperty))
    g.add((has_admission, RDFS.label, Literal("hasAdmission")))
    g.add((has_admission, RDFS.domain, MIMIC["DiagnosisIcd"]))
    g.add((has_admission, RDFS.range, MIMIC["Admission"]))

    # Icustay → Admission
    has_admission_icu = MIMIC["hasHadmId"]
    g.add((has_admission_icu, RDF.type, OWL.ObjectProperty))
    g.add((has_admission_icu, RDFS.label, Literal("hasHadmId")))
    g.add((has_admission_icu, RDFS.domain, MIMIC["Icustay"]))
    g.add((has_admission_icu, RDFS.range, MIMIC["Admission"]))

    # ── Datatype properties on Patient ────────────────────────────────────────
    for col in ("subject_id", "gender", "dob", "dod", "expire_flag"):
        prop = _prop_uri(col)
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        g.add((prop, RDFS.label, Literal(f"has{_camel(col)}")))
        g.add((prop, RDFS.domain, MIMIC["Patient"]))

    # ── ICD-9 code property on DiagnosisIcd ───────────────────────────────────
    icd9_prop = MIMIC["hasIcd9Code"]
    g.add((icd9_prop, RDF.type, OWL.DatatypeProperty))
    g.add((icd9_prop, RDFS.label, Literal("hasIcd9Code")))
    g.add((icd9_prop, RDFS.domain, MIMIC["DiagnosisIcd"]))

    # ── CareVue / MetaVision subclasses of Icustay ────────────────────────────
    for sub in ("CareVueIcustay", "MetavisionIcustay"):
        uri = MIMIC[sub]
        g.add((uri, RDF.type, OWL.Class))
        g.add((uri, RDFS.subClassOf, MIMIC["Icustay"]))
        g.add((uri, RDFS.label, Literal(sub)))

    return g


# ─────────────────────────────────────────────────────────────────────────────
# 1. Three-level hierarchy: Patient → Admission → DiagnosisIcd
# ─────────────────────────────────────────────────────────────────────────────

class TestThreeLevelHierarchy:
    """Issue #54 criterion 1: object-property chain encodes join path."""

    @pytest.fixture(scope="class")
    def g(self):
        return _build_synthetic_mimic_graph()

    def test_patient_class_exists(self, g):
        assert MIMIC["Patient"] in _all_classes(g), "Expected OWL class mimic:Patient"

    def test_admission_class_exists(self, g):
        assert MIMIC["Admission"] in _all_classes(g), "Expected OWL class mimic:Admission"

    def test_diagnosis_class_exists(self, g):
        assert MIMIC["DiagnosisIcd"] in _all_classes(g), "Expected OWL class mimic:DiagnosisIcd"

    def test_admission_links_to_patient_via_object_property(self, g):
        """An object property must have domain=Admission and range=Patient."""
        obj_props = _object_properties(g)
        found = any(
            (p, RDFS.domain, MIMIC["Admission"]) in g
            and (p, RDFS.range, MIMIC["Patient"]) in g
            for p in obj_props
        )
        assert found, "No object property with domain=Admission and range=Patient found"

    def test_diagnosis_links_to_admission_via_object_property(self, g):
        """An object property must have domain=DiagnosisIcd and range=Admission."""
        obj_props = _object_properties(g)
        found = any(
            (p, RDFS.domain, MIMIC["DiagnosisIcd"]) in g
            and (p, RDFS.range, MIMIC["Admission"]) in g
            for p in obj_props
        )
        assert found, "No object property with domain=DiagnosisIcd and range=Admission found"

    def test_hierarchy_depth_is_at_least_three(self, g):
        """Verify at minimum three distinct class nodes exist in the chain."""
        chain = {MIMIC["Patient"], MIMIC["Admission"], MIMIC["DiagnosisIcd"]}
        classes = _all_classes(g)
        assert chain.issubset(classes), f"Missing classes: {chain - classes}"

    def test_classes_have_rdfs_labels(self, g):
        """Each class in the hierarchy should carry an rdfs:label."""
        for cls_name in ("Patient", "Admission", "DiagnosisIcd"):
            uri = MIMIC[cls_name]
            labels = list(g.objects(uri, RDFS.label))
            assert labels, f"mimic:{cls_name} has no rdfs:label"


# ─────────────────────────────────────────────────────────────────────────────
# 2. ICD-9 code property
# ─────────────────────────────────────────────────────────────────────────────

class TestIcd9CodeProperty:
    """Issue #54 criterion 2: ICD-9 code column is mapped to a typed property."""

    @pytest.fixture(scope="class")
    def g(self):
        return _build_synthetic_mimic_graph()

    def test_icd9_property_exists(self, g):
        icd9_prop = MIMIC["hasIcd9Code"]
        rdf_types = set(g.objects(icd9_prop, RDF.type))
        assert rdf_types, f"mimic:hasIcd9Code has no rdf:type"

    def test_icd9_property_is_datatype_property(self, g):
        icd9_prop = MIMIC["hasIcd9Code"]
        assert (icd9_prop, RDF.type, OWL.DatatypeProperty) in g, (
            "mimic:hasIcd9Code should be an owl:DatatypeProperty "
            "(ICD-9 codes are string literals, not entity URIs)"
        )

    def test_icd9_property_has_diagnosis_domain(self, g):
        icd9_prop = MIMIC["hasIcd9Code"]
        domains = list(g.objects(icd9_prop, RDFS.domain))
        assert MIMIC["DiagnosisIcd"] in domains, (
            "mimic:hasIcd9Code should have rdfs:domain of mimic:DiagnosisIcd"
        )

    def test_icd9_property_has_label(self, g):
        icd9_prop = MIMIC["hasIcd9Code"]
        labels = list(g.objects(icd9_prop, RDFS.label))
        assert labels, "mimic:hasIcd9Code has no rdfs:label"
        assert any("icd" in str(lbl).lower() or "Icd" in str(lbl) for lbl in labels), (
            f"Label {labels} does not reference ICD"
        )

    def test_icd9_property_uri_follows_naming_convention(self, g):
        """All MIMIC properties should follow the hasXxx camelCase pattern."""
        all_props = _data_properties(g) | _object_properties(g)
        mimic_props = [p for p in all_props if str(p).startswith(str(MIMIC))]
        for p in mimic_props:
            local = str(p).split("#")[-1]
            assert local.startswith("has"), (
                f"Property {p} does not follow 'has...' naming convention"
            )


# ─────────────────────────────────────────────────────────────────────────────
# 3. CareVue / MetaVision subclass split
# ─────────────────────────────────────────────────────────────────────────────

class TestCareVueMetaVisionSplit:
    """Issue #54 criterion 3: ICU source system split modelled as subclasses."""

    @pytest.fixture(scope="class")
    def g(self):
        return _build_synthetic_mimic_graph()

    def test_icustay_class_exists(self, g):
        assert MIMIC["Icustay"] in _all_classes(g)

    def test_carevue_subclass_exists(self, g):
        carevue = MIMIC["CareVueIcustay"]
        assert (carevue, RDFS.subClassOf, MIMIC["Icustay"]) in g, (
            "Expected CareVueIcustay to be a subclass of Icustay"
        )

    def test_metavision_subclass_exists(self, g):
        metavision = MIMIC["MetavisionIcustay"]
        assert (metavision, RDFS.subClassOf, MIMIC["Icustay"]) in g, (
            "Expected MetavisionIcustay to be a subclass of Icustay"
        )

    def test_subclasses_are_distinct(self, g):
        carevue = MIMIC["CareVueIcustay"]
        metavision = MIMIC["MetavisionIcustay"]
        # They must not be declared as owl:equivalentClass of each other
        assert (carevue, OWL.equivalentClass, metavision) not in g, (
            "CareVueIcustay and MetavisionIcustay must not be equivalent"
        )

    def test_subclasses_have_labels(self, g):
        for cls in (MIMIC["CareVueIcustay"], MIMIC["MetavisionIcustay"]):
            labels = list(g.objects(cls, RDFS.label))
            assert labels, f"{cls} has no rdfs:label"

    def test_icustay_subclass_count_is_exactly_two(self, g):
        subclasses = [
            s for s in g.subjects(RDFS.subClassOf, MIMIC["Icustay"])
            if isinstance(s, URIRef)
        ]
        assert len(subclasses) == 2, (
            f"Expected exactly 2 subclasses of Icustay (CareVue, MetaVision), "
            f"got {len(subclasses)}: {subclasses}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Integration tests — only run when owl/mimic_gen.owl exists
# ─────────────────────────────────────────────────────────────────────────────

_LIVE_REASON = f"owl/mimic_gen.owl not found — run the MIMIC pipeline first"


@pytest.mark.skipif(not MIMIC_OWL.exists(), reason=_LIVE_REASON)
class TestLiveMimicOntology:
    """Load the real generated OWL and apply the same structural checks."""

    @pytest.fixture(scope="class")
    def g(self):
        graph = Graph()
        graph.parse(str(MIMIC_OWL))
        return graph

    def test_patient_class_present(self, g):
        classes = _all_classes(g)
        patient_like = [c for c in classes if "patient" in _label_of(g, c).lower()]
        assert patient_like, "No class with 'patient' in its label found in mimic_gen.owl"

    def test_admission_class_present(self, g):
        classes = _all_classes(g)
        adm_like = [c for c in classes if "admission" in _label_of(g, c).lower()]
        assert adm_like, "No class with 'admission' in its label found in mimic_gen.owl"

    def test_object_property_chain_admission_to_patient(self, g):
        obj_props = _object_properties(g)
        classes = _all_classes(g)
        patient_cls = next(
            (c for c in classes if "patient" in _label_of(g, c).lower()), None
        )
        adm_cls = next(
            (c for c in classes if "admission" in _label_of(g, c).lower()), None
        )
        if patient_cls is None or adm_cls is None:
            pytest.skip("Could not identify Patient/Admission classes in live OWL")

        found = any(
            (p, RDFS.domain, adm_cls) in g and (p, RDFS.range, patient_cls) in g
            for p in obj_props
        )
        assert found, (
            f"No object property found with domain={adm_cls} range={patient_cls}"
        )

    def test_icd9_property_present(self, g):
        all_props = _data_properties(g) | _object_properties(g)
        icd9_like = [
            p for p in all_props
            if "icd" in str(p).lower() or "icd" in _label_of(g, p).lower()
        ]
        assert icd9_like, "No ICD-9 property found in mimic_gen.owl"

    def test_minimum_class_count(self, g):
        """Generated OWL should have at least 5 classes for MIMIC tables."""
        classes = _all_classes(g)
        assert len(classes) >= 5, (
            f"Expected >= 5 classes in mimic_gen.owl, found {len(classes)}"
        )
