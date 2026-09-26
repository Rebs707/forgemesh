# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for the cross-table canonical value registry (Issue 1, CANON-1)."""
from __future__ import annotations

from semantic_mapper.engine.mapper import (
    canonicalise_value_mappings,
    is_entity_reference_column,
    looks_like_packed_cell,
)
from semantic_mapper.schema.mapping_output import ValueMappingRule


class _Col:
    def __init__(self, top_values):
        self.top_values = top_values


def test_packed_cell_json_list_is_detected():
    col = _Col([{"value": "[{'discipline': 'Ice Hockey', 'event': \"Men's\"}]", "count": 3}])
    assert looks_like_packed_cell(col) is True


def test_packed_cell_json_object_is_detected():
    col = _Col([{"value": '{"a": 1}', "count": 3}])
    assert looks_like_packed_cell(col) is True


def test_plain_categorical_is_not_packed():
    col = _Col([{"value": "GOLD", "count": 9}, {"value": "SILVER", "count": 8}])
    assert looks_like_packed_cell(col) is False


def test_empty_top_values_not_packed():
    assert looks_like_packed_cell(_Col([])) is False
    assert looks_like_packed_cell(_Col(None)) is False


def _r(value, uri):
    return ValueMappingRule(source_value=value, mapped_uri=uri, rationale="t", confidence_score=0.9)


def test_first_declaration_wins_within_a_field():
    reg: dict = {}
    a = canonicalise_value_mappings(
        "gender", [_r("M", "http://x#MaleGender"), _r("F", "http://x#FemaleGender")], reg
    )
    b = canonicalise_value_mappings(  # a second table, same field, different URIs
        "gender", [_r("M", "http://x#MedallistGenderMale")], reg
    )
    assert a[0].mapped_uri == "http://x#MaleGender"          # first declaration stands
    assert b[0].mapped_uri == "http://x#MaleGender"          # later table reuses it


def test_new_value_registers_and_is_reused():
    reg: dict = {}
    canonicalise_value_mappings("code", [_r("ALP", "http://x#DiscALP")], reg)
    out = canonicalise_value_mappings("code", [_r("ALP", "http://x#DiscAlpineSkiing")], reg)
    assert out[0].mapped_uri == "http://x#DiscALP"           # the split is collapsed


def test_normalisation_of_value_and_field():
    reg: dict = {}
    canonicalise_value_mappings("Gender", [_r("M", "http://x#MaleGender")], reg)
    out = canonicalise_value_mappings("gender", [_r(" m ", "http://x#Other")], reg)
    assert out[0].mapped_uri == "http://x#MaleGender"        # case/space-insensitive match


def test_same_value_different_field_is_independent():
    reg: dict = {}
    canonicalise_value_mappings("gender", [_r("M", "http://x#MaleGender")], reg)
    out = canonicalise_value_mappings("medal", [_r("M", "http://x#MedalM")], reg)
    assert out[0].mapped_uri == "http://x#MedalM"            # homonym across fields kept apart


# ── Discriminate-vs-FK fork (Issue 2) — an entity reference is not a vocabulary ──


def test_fk_column_is_entity_reference():
    fks = [{"column": "venue_code", "ref_table": "raw_venues", "inferred": True}]
    assert is_entity_reference_column("venue_code", fks) is True


def test_descriptive_shadow_of_coded_fk_is_entity_reference():
    # `venue` name beside a `venue_code` FK → reference, not its own vocabulary.
    fks = [{"column": "venue_code", "ref_table": "raw_venues", "inferred": True}]
    assert is_entity_reference_column("venue", fks) is True


def test_plain_categorical_is_not_entity_reference():
    # `status` with no FK / coded sibling stays a discriminable vocabulary.
    fks = [{"column": "venue_code", "ref_table": "raw_venues", "inferred": True}]
    assert is_entity_reference_column("status", fks) is False


def test_handles_constrained_column_key_and_empty():
    assert is_entity_reference_column("x", [{"constrained_column": "x"}]) is True
    assert is_entity_reference_column("x", []) is False
    assert is_entity_reference_column("x", None) is False


# ---------------------------------------------------------------------------
# Ontology alignment: value-map targets snap onto the generator's declared
# deterministic individuals (so OWL individuals == value-map targets).
# ---------------------------------------------------------------------------

def _ontology_with_gender():
    from rdflib import Graph, Literal, OWL, RDF, RDFS, URIRef
    g = Graph()
    B = "http://x#"
    gender = URIRef(B + "Gender")
    g.add((gender, RDF.type, OWL.Class))
    for local, lbl in [("MGender", "M"), ("FGender", "F"), ("WGender", "W"), ("XGender", "X")]:
        i = URIRef(B + local)
        g.add((i, RDF.type, OWL.NamedIndividual))
        g.add((i, RDF.type, gender))
        g.add((i, RDFS.label, Literal(lbl)))
    prop = URIRef(B + "hasAthleteGender")
    g.add((prop, RDF.type, OWL.ObjectProperty))
    g.add((prop, RDFS.range, gender))
    return g, prop


class _Mapping:
    def __init__(self, source_column, mapped_property_uri):
        self.source_column = source_column
        self.mapped_property_uri = mapped_property_uri


def test_alignment_snaps_target_to_declared_individual():
    from semantic_mapper.engine.mapper import align_value_mappings_to_ontology
    g, prop = _ontology_with_gender()
    m = _Mapping("gender", str(prop))
    rules = [_r("M", "http://x#MaleGender"), _r("F", "http://x#FemaleGender")]
    out = align_value_mappings_to_ontology(m, rules, g)
    assert out[0].mapped_uri == "http://x#MGender"   # LLM synonym → declared individual
    assert out[1].mapped_uri == "http://x#FGender"


def test_alignment_matches_label_case_insensitively():
    from semantic_mapper.engine.mapper import align_value_mappings_to_ontology
    g, prop = _ontology_with_gender()
    out = align_value_mappings_to_ontology(_Mapping("gender", str(prop)),
                                           [_r("m", "http://x#Male")], g)
    assert out[0].mapped_uri == "http://x#MGender"


def test_alignment_leaves_undeclared_value_unchanged():
    from semantic_mapper.engine.mapper import align_value_mappings_to_ontology
    g, prop = _ontology_with_gender()
    out = align_value_mappings_to_ontology(_Mapping("gender", str(prop)),
                                           [_r("Z", "http://x#ZGender")], g)
    assert out[0].mapped_uri == "http://x#ZGender"   # no declared individual → unchanged


def test_alignment_noop_without_class_range():
    from rdflib import Graph, OWL, RDF, RDFS, URIRef
    from semantic_mapper.engine.mapper import align_value_mappings_to_ontology
    g = Graph()
    prop = URIRef("http://x#hasAge")
    g.add((prop, RDF.type, OWL.DatatypeProperty))
    g.add((prop, RDFS.range, URIRef("http://www.w3.org/2001/XMLSchema#integer")))
    out = align_value_mappings_to_ontology(_Mapping("age", str(prop)),
                                           [_r("M", "http://x#MaleGender")], g)
    assert out[0].mapped_uri == "http://x#MaleGender"   # datatype range → no-op


# ---------------------------------------------------------------------------
# maps_to_vocabulary_property: only value-map columns the generator modelled
# as a vocabulary (ObjectProperty → a class with declared individuals).
# ---------------------------------------------------------------------------

def test_vocab_property_true_for_objectproperty_with_declared_individuals():
    from semantic_mapper.engine.mapper import maps_to_vocabulary_property
    g, prop = _ontology_with_gender()        # hasAthleteGender → Gender (MGender, …)
    assert maps_to_vocabulary_property(str(prop), g) is True


def test_vocab_property_false_for_datatype_property():
    from rdflib import Graph, OWL, RDF, RDFS, URIRef
    from semantic_mapper.engine.mapper import maps_to_vocabulary_property
    g = Graph()
    p = URIRef("http://x#hasVenueVenue")     # the venue name as a plain string
    g.add((p, RDF.type, OWL.DatatypeProperty))
    g.add((p, RDFS.range, URIRef("http://www.w3.org/2001/XMLSchema#string")))
    assert maps_to_vocabulary_property(str(p), g) is False


def test_vocab_property_false_for_entity_range_without_individuals():
    from rdflib import Graph, OWL, RDF, RDFS, URIRef
    from semantic_mapper.engine.mapper import maps_to_vocabulary_property
    g = Graph()
    venue = URIRef("http://x#Venue")          # entity class — no declared individuals
    g.add((venue, RDF.type, OWL.Class))
    p = URIRef("http://x#hasScheduleVenue")
    g.add((p, RDF.type, OWL.ObjectProperty))
    g.add((p, RDFS.range, venue))
    assert maps_to_vocabulary_property(str(p), g) is False


def test_vocab_property_false_for_unmapped_column():
    from rdflib import Graph
    from semantic_mapper.engine.mapper import maps_to_vocabulary_property
    assert maps_to_vocabulary_property(None, Graph()) is False
