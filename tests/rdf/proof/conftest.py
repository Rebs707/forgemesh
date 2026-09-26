# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for Feature 12 proof narrator tests.

Builds a minimal in-memory ontology with:
  - Classes: Patient, Encounter
  - Properties: patientHasEncounter (ObjectProperty), firstName (DatatypeProperty)
  - fm: annotations on Patient and patientHasEncounter
  - rdfs:subClassOf: Encounter rdfs:subClassOf Entity
"""
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef
from rdflib.namespace import XSD

from semantic_mapper.rdf.proof._fm import FM

NS = "http://mesh.local/ontology/"
DATA = "http://mesh.local/data/"

PATIENT_URI = NS + "Patient"
ENCOUNTER_URI = NS + "Encounter"
ENTITY_URI = NS + "Entity"
HAS_ENCOUNTER_URI = NS + "patientHasEncounter"
FIRST_NAME_URI = NS + "firstName"
GENDER_URI = NS + "gender"
ADMIT_TIME_URI = NS + "admitTime"


@pytest.fixture
def ontology() -> Graph:
    """Minimal OWL ontology for proof tests."""
    g = Graph()

    # Classes
    for cls in (PATIENT_URI, ENCOUNTER_URI, ENTITY_URI):
        g.add((URIRef(cls), RDF.type, OWL.Class))

    # Encounter subClassOf Entity (for superclass map tests)
    g.add((URIRef(ENCOUNTER_URI), RDFS.subClassOf, URIRef(ENTITY_URI)))

    # Labels
    g.add((URIRef(PATIENT_URI), RDFS.label, Literal("Patient")))
    g.add((URIRef(ENCOUNTER_URI), RDFS.label, Literal("Encounter")))

    # fm:interpretation on Patient
    g.add((URIRef(PATIENT_URI), FM.interpretation,
           Literal("A person receiving medical care, identified by patient ID.")))
    # fm:limitation on Patient
    g.add((URIRef(PATIENT_URI), FM.limitation,
           Literal("Patient records are limited to those admitted to this facility.")))

    # ObjectProperty: patientHasEncounter  (Patient → Encounter)
    g.add((URIRef(HAS_ENCOUNTER_URI), RDF.type, OWL.ObjectProperty))
    g.add((URIRef(HAS_ENCOUNTER_URI), RDFS.domain, URIRef(PATIENT_URI)))
    g.add((URIRef(HAS_ENCOUNTER_URI), RDFS.range, URIRef(ENCOUNTER_URI)))
    g.add((URIRef(HAS_ENCOUNTER_URI), RDFS.label, Literal("patient has encounter")))
    # fm:interpretation on the property
    g.add((URIRef(HAS_ENCOUNTER_URI), FM.interpretation,
           Literal("Links a patient to their hospital encounter records.")))
    # fm:scope_default on the property
    g.add((URIRef(HAS_ENCOUNTER_URI), FM.scope_default,
           Literal("All encounter types are included unless filtered.")))

    # DatatypeProperty: firstName
    g.add((URIRef(FIRST_NAME_URI), RDF.type, OWL.DatatypeProperty))
    g.add((URIRef(FIRST_NAME_URI), RDFS.domain, URIRef(PATIENT_URI)))
    g.add((URIRef(FIRST_NAME_URI), RDFS.range, XSD.string))
    g.add((URIRef(FIRST_NAME_URI), RDFS.label, Literal("first name")))
    g.add((URIRef(FIRST_NAME_URI), RDFS.comment,
           Literal("The given name of the patient.")))

    # DatatypeProperty: gender
    g.add((URIRef(GENDER_URI), RDF.type, OWL.DatatypeProperty))
    g.add((URIRef(GENDER_URI), RDFS.domain, URIRef(PATIENT_URI)))
    g.add((URIRef(GENDER_URI), RDFS.range, XSD.string))
    g.add((URIRef(GENDER_URI), RDFS.label, Literal("gender")))

    # DatatypeProperty: admitTime (on Encounter)
    g.add((URIRef(ADMIT_TIME_URI), RDF.type, OWL.DatatypeProperty))
    g.add((URIRef(ADMIT_TIME_URI), RDFS.domain, URIRef(ENCOUNTER_URI)))
    g.add((URIRef(ADMIT_TIME_URI), RDFS.range, XSD.dateTime))
    g.add((URIRef(ADMIT_TIME_URI), RDFS.label, Literal("admission time")))

    return g
