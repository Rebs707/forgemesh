# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the Manchester Syntax parser.
"""
import pytest
from rdflib import OWL, RDF, RDFS, URIRef
from rdflib.namespace import SKOS

from semantic_mapper.ontology.iterative.manchester_parser import (
    _parse,
    _strip_fences,
    parse_manchester,
)

BASE = "http://mesh.local/ontology/test#"

_SAMPLE_FRAGMENT = """
Class: <http://mesh.local/ontology/test#Patient>
    Annotations: rdfs:label "Patient"@en
    SubClassOf: owl:Thing

ObjectProperty: <http://mesh.local/ontology/test#hasAdmission>
    Annotations: rdfs:label "has admission"@en
    Domain: <http://mesh.local/ontology/test#Patient>
    Range: <http://mesh.local/ontology/test#Admission>

DatatypeProperty: <http://mesh.local/ontology/test#hasSubjectId>
    Annotations:
        rdfs:label "has subject id"@en,
        skos:altLabel "subject_id"@en
    Domain: <http://mesh.local/ontology/test#Patient>
    Range: xsd:integer
"""


class TestStripFences:
    def test_no_fences(self):
        assert _strip_fences("Class: <x>") == "Class: <x>"

    def test_strips_generic_fence(self):
        text = "```\nClass: <x>\n```"
        assert _strip_fences(text) == "Class: <x>"

    def test_strips_manchester_fence(self):
        text = "```manchester\nClass: <x>\n```"
        assert _strip_fences(text) == "Class: <x>"


class TestRegexParser:
    def test_class_declared(self):
        g, classes, props = _parse(_SAMPLE_FRAGMENT, BASE, "test")
        assert f"{BASE}Patient" in classes

    def test_object_property_declared(self):
        g, classes, props = _parse(_SAMPLE_FRAGMENT, BASE, "test")
        assert f"{BASE}hasAdmission" in props

    def test_datatype_property_declared(self):
        g, classes, props = _parse(_SAMPLE_FRAGMENT, BASE, "test")
        assert f"{BASE}hasSubjectId" in props

    def test_class_rdf_type_triple(self):
        g, _, _ = _parse(_SAMPLE_FRAGMENT, BASE, "test")
        patient = URIRef(f"{BASE}Patient")
        assert (patient, RDF.type, OWL.Class) in g

    def test_object_property_type_triple(self):
        g, _, _ = _parse(_SAMPLE_FRAGMENT, BASE, "test")
        prop = URIRef(f"{BASE}hasAdmission")
        assert (prop, RDF.type, OWL.ObjectProperty) in g

    def test_datatype_property_type_triple(self):
        g, _, _ = _parse(_SAMPLE_FRAGMENT, BASE, "test")
        prop = URIRef(f"{BASE}hasSubjectId")
        assert (prop, RDF.type, OWL.DatatypeProperty) in g

    def test_skos_alt_label(self):
        g, _, _ = _parse(_SAMPLE_FRAGMENT, BASE, "test")
        prop = URIRef(f"{BASE}hasSubjectId")
        alt_labels = list(g.objects(prop, SKOS.altLabel))
        assert any("subject_id" in str(lbl) for lbl in alt_labels)

    def test_domain_triple(self):
        g, _, _ = _parse(_SAMPLE_FRAGMENT, BASE, "test")
        prop = URIRef(f"{BASE}hasAdmission")
        domains = list(g.objects(prop, RDFS.domain))
        assert URIRef(f"{BASE}Patient") in domains

    def test_range_triple(self):
        g, _, _ = _parse(_SAMPLE_FRAGMENT, BASE, "test")
        prop = URIRef(f"{BASE}hasAdmission")
        ranges = list(g.objects(prop, RDFS.range))
        assert URIRef(f"{BASE}Admission") in ranges

    def test_empty_fragment_returns_empty_graph(self):
        g, classes, props = _parse("", BASE, "test")
        assert len(g) == 0
        assert classes == []
        assert props == []


class TestParseManchester:
    def test_never_raises(self):
        """parse_manchester must never raise even on garbage input."""
        g, classes, props = parse_manchester("GARBAGE @#$%", BASE, "test")
        # Should return something without raising
        assert isinstance(g, object)

    def test_parses_valid_fragment(self):
        g, classes, props = parse_manchester(_SAMPLE_FRAGMENT, BASE, "test")
        assert len(g) > 0


_INDIVIDUAL_FRAGMENT = """
Class: <http://mesh.local/ontology/test#AdmissionType>
    Annotations: rdfs:label "Admission Type"@en

Individual: <http://mesh.local/ontology/test#Emergency>
    Types: <http://mesh.local/ontology/test#AdmissionType>
    Annotations: rdfs:label "Emergency"@en

Individual: <http://mesh.local/ontology/test#Elective>
    Types: <http://mesh.local/ontology/test#AdmissionType>
    Annotations: rdfs:label "Elective"@en
"""


class TestBareXsdTypes:
    """LLM sometimes writes 'Range: integer' without xsd: prefix — parser must resolve it."""

    def test_bare_integer_resolved(self):
        frag = f"""
DatatypeProperty: <{BASE}hasFoo>
    Domain: <{BASE}Patient>
    Range: integer
"""
        g, _, _ = _parse(frag, BASE, "test")
        prop = URIRef(f"{BASE}hasFoo")
        ranges = list(g.objects(prop, RDFS.range))
        assert URIRef("http://www.w3.org/2001/XMLSchema#integer") in ranges

    def test_bare_datetime_resolved(self):
        frag = f"""
DatatypeProperty: <{BASE}hasTime>
    Domain: <{BASE}Patient>
    Range: dateTime
"""
        g, _, _ = _parse(frag, BASE, "test")
        prop = URIRef(f"{BASE}hasTime")
        ranges = list(g.objects(prop, RDFS.range))
        assert URIRef("http://www.w3.org/2001/XMLSchema#dateTime") in ranges

    def test_prefixed_xsd_still_works(self):
        frag = f"""
DatatypeProperty: <{BASE}hasAge>
    Domain: <{BASE}Patient>
    Range: xsd:integer
"""
        g, _, _ = _parse(frag, BASE, "test")
        prop = URIRef(f"{BASE}hasAge")
        ranges = list(g.objects(prop, RDFS.range))
        assert URIRef("http://www.w3.org/2001/XMLSchema#integer") in ranges


class TestIndividualParsing:
    def test_individual_named_individual_triple(self):
        g, _, _ = _parse(_INDIVIDUAL_FRAGMENT, BASE, "test")
        ind = URIRef(f"{BASE}Emergency")
        assert (ind, RDF.type, OWL.NamedIndividual) in g

    def test_individual_types_triple(self):
        g, _, _ = _parse(_INDIVIDUAL_FRAGMENT, BASE, "test")
        ind = URIRef(f"{BASE}Emergency")
        cls = URIRef(f"{BASE}AdmissionType")
        assert (ind, RDF.type, cls) in g

    def test_individual_label(self):
        g, _, _ = _parse(_INDIVIDUAL_FRAGMENT, BASE, "test")
        ind = URIRef(f"{BASE}Elective")
        labels = [str(lbl) for lbl in g.objects(ind, RDFS.label)]
        assert "Elective" in labels

    def test_multiple_individuals_parsed(self):
        g, _, _ = _parse(_INDIVIDUAL_FRAGMENT, BASE, "test")
        individuals = list(g.subjects(RDF.type, OWL.NamedIndividual))
        assert len(individuals) == 2

    def test_class_still_parsed_alongside_individuals(self):
        g, classes, _ = _parse(_INDIVIDUAL_FRAGMENT, BASE, "test")
        assert f"{BASE}AdmissionType" in classes


# ---------------------------------------------------------------------------
# Prefix-leak normalisation (qwen3 "Ontology:" prefix bug)
# ---------------------------------------------------------------------------

class TestPrefixLeakNormalisation:
    """Parser must strip invented namespace prefixes before URIRef construction."""

    def test_invented_prefix_on_class_stripped(self):
        """'Ontology:Patient' in a Class: block resolves to base#Patient."""
        fragment = """
Ontology: <http://mesh.local/ontology/test#>
Prefix: fm: <http://forgemesh.ai/ontology#>

Class: Ontology:Patient
  Annotations: rdfs:label "Patient"@en
  Annotations: fm:sourceTable "raw_patients"
"""
        g, classes, _ = _parse(fragment, BASE, "raw_patients")
        assert f"{BASE}Patient" in classes, f"Expected {BASE}Patient in classes; got {classes}"
        # The malformed URI must NOT appear
        assert not any("Ontology:" in c for c in classes)

    def test_invented_prefix_on_object_property_range_stripped(self):
        """Range: Ontology:Admission resolves to base#Admission, not base#Ontology:Admission."""
        fragment = """
Ontology: <http://mesh.local/ontology/test#>
Prefix: fm: <http://forgemesh.ai/ontology#>

ObjectProperty: hasAdmission
  Domain: Event
  Range: Ontology:Admission
  Annotations: rdfs:label "has admission"@en, fm:sourceForeignKey "raw_events.hadm_id -> raw_admissions"
"""
        g, _, props = _parse(fragment, BASE, "raw_events")
        prop = URIRef(f"{BASE}hasAdmission")
        ranges = list(g.objects(prop, RDFS.range))
        assert len(ranges) == 1
        assert str(ranges[0]) == f"{BASE}Admission", f"Unexpected range: {ranges}"

    def test_double_mangled_property_uri_stripped(self):
        """'hasOntology:Caregiver' in an ObjectProperty: block resolves to base#hasCaregiver."""
        fragment = """
Ontology: <http://mesh.local/ontology/test#>
Prefix: fm: <http://forgemesh.ai/ontology#>

ObjectProperty: hasOntology:Caregiver
  Domain: Event
  Range: Caregiver
  Annotations: rdfs:label "has caregiver"@en, fm:sourceForeignKey "raw_events.cgid -> raw_caregivers"
"""
        g, _, props = _parse(fragment, BASE, "raw_events")
        assert f"{BASE}hasCaregiver" in props, f"Expected hasCaregiver in props; got {props}"
        assert not any("Ontology" in p for p in props)

    def test_known_prefixes_not_stripped(self):
        """skos:altLabel and fm:sourceTable must survive normalisation untouched."""
        fragment = """
Ontology: <http://mesh.local/ontology/test#>
Prefix: fm: <http://forgemesh.ai/ontology#>
Prefix: skos: <http://www.w3.org/2004/02/skos/core#>

DatatypeProperty: hasEventStatus
  Domain: Event
  Range: xsd:string
  Annotations:
    rdfs:label "has event status"@en,
    skos:altLabel "status"@en,
    fm:sourceColumn "raw_events.status",
    fm:sqlType "VARCHAR",
    fm:nullRate "0.10"
"""
        g, _, props = _parse(fragment, BASE, "raw_events")
        prop = URIRef(f"{BASE}hasEventStatus")
        alt_labels = list(g.objects(prop, SKOS.altLabel))
        assert any("status" in str(a) for a in alt_labels), "skos:altLabel must survive"

    def test_detect_prefix_leak_clean_after_parse(self):
        """After parsing a fragment with invented prefixes, detect_prefix_leak returns empty."""
        from semantic_mapper.ontology.iterative.prefix_leak_fix import detect_prefix_leak
        fragment = """
Ontology: <http://mesh.local/ontology/test#>
Prefix: fm: <http://forgemesh.ai/ontology#>

Class: Ontology:Patient
  Annotations: rdfs:label "Patient"@en, fm:sourceTable "raw_patients"

ObjectProperty: hasOntology:Caregiver
  Domain: Ontology:Patient
  Range: Caregiver
  Annotations: rdfs:label "has caregiver"@en, fm:sourceForeignKey "raw_patients.cgid -> raw_caregivers"
"""
        g, classes, props = _parse(fragment, BASE, "raw_patients")
        all_local_names = [u.split("#")[-1] for u in classes + props]
        leaks = detect_prefix_leak(all_local_names)
        assert leaks == {}, f"Prefix leak detected after parse: {leaks}"
