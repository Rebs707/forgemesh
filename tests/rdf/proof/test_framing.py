# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 2 tests: ResultFraming submodule."""
from __future__ import annotations

import pytest
from rdflib import Graph

from semantic_mapper.core.exceptions import OntologyGroundingError
from semantic_mapper.rdf.proof.framing import build_framing, expand_prefixes
from tests.rdf.proof.conftest import (
    ENCOUNTER_URI,
    HAS_ENCOUNTER_URI,
    FIRST_NAME_URI,
    PATIENT_URI,
    NS,
)

_ROWS = [{"name": "Alice"}, {"name": "Bob"}]

SPARQL_COUNT = f"""
    PREFIX ex: <{NS}>
    SELECT (COUNT(?s) AS ?count)
    WHERE {{
        GRAPH <http://mesh.local/graph/test> {{
            ?s a <{PATIENT_URI}> .
        }}
    }}
"""

SPARQL_WITH_FILTER = f"""
    PREFIX ex: <{NS}>
    SELECT ?name
    WHERE {{
        GRAPH <http://g> {{
            ?s a <{PATIENT_URI}> .
            ?s <{FIRST_NAME_URI}> ?name .
            FILTER(?name = "Alice")
        }}
    }}
"""

SPARQL_WITH_JOIN = f"""
    PREFIX ex: <{NS}>
    SELECT ?name
    WHERE {{
        GRAPH <http://g> {{
            ?s a <{PATIENT_URI}> .
            ?s <{HAS_ENCOUNTER_URI}> ?encURI .
            ?encURI a <{ENCOUNTER_URI}> .
        }}
    }}
"""

SPARQL_LITERAL_BINDING = f"""
    SELECT ?s WHERE {{
        GRAPH <http://g> {{
            ?s <{FIRST_NAME_URI}> "Alice" .
        }}
    }}
"""


class TestBuildFraming:
    def test_primary_class_extracted(self, ontology):
        framing = build_framing(SPARQL_COUNT, _ROWS, ontology)
        assert framing.primary_class_uri == PATIENT_URI

    def test_cardinality_from_rows(self, ontology):
        framing = build_framing(SPARQL_COUNT, _ROWS, ontology)
        assert framing.cardinality == 2

    def test_no_constraints_on_count_query(self, ontology):
        framing = build_framing(SPARQL_COUNT, _ROWS, ontology)
        assert framing.constraints_applied == []

    def test_filter_produces_applied_constraint(self, ontology):
        framing = build_framing(SPARQL_WITH_FILTER, _ROWS, ontology)
        assert len(framing.constraints_applied) == 1
        c = framing.constraints_applied[0]
        assert c.property_uri == FIRST_NAME_URI
        assert "Alice" in c.filter_form

    def test_constraint_property_definition_from_ontology(self, ontology):
        framing = build_framing(SPARQL_WITH_FILTER, _ROWS, ontology)
        c = framing.constraints_applied[0]
        # rdfs:comment says "The given name of the patient."
        assert "given name" in c.property_definition

    def test_joined_class_from_object_property(self, ontology):
        framing = build_framing(SPARQL_WITH_JOIN, _ROWS, ontology)
        assert ENCOUNTER_URI in framing.joined_class_uris

    def test_literal_binding_is_constraint(self, ontology):
        framing = build_framing(SPARQL_LITERAL_BINDING, _ROWS, ontology)
        assert any(c.property_uri == FIRST_NAME_URI for c in framing.constraints_applied)

    def test_no_ontology_still_produces_framing(self):
        framing = build_framing(SPARQL_COUNT, _ROWS, None)
        assert framing.primary_class_uri == PATIENT_URI
        assert framing.cardinality == 2

    def test_grounding_error_for_unknown_property(self, ontology):
        unknown_prop = "http://ont/unknownProperty"
        sparql = f"""
            SELECT ?s WHERE {{
                GRAPH <http://g> {{
                    ?s <{unknown_prop}> ?val .
                    FILTER(?val = "x")
                }}
            }}
        """
        with pytest.raises(OntologyGroundingError):
            build_framing(sparql, [], ontology)


class TestExpandPrefixes:
    def test_prefixed_property_expanded(self):
        sparql = (
            f"PREFIX ex: <{NS}>\n"
            "SELECT ?s WHERE { ?s ex:firstName ?name . }"
        )
        expanded = expand_prefixes(sparql)
        assert f"<{NS}firstName>" in expanded
        assert "ex:firstName" not in expanded

    def test_prefixed_class_in_type_triple_expanded(self):
        sparql = (
            f"PREFIX ex: <{NS}>\n"
            "SELECT ?s WHERE { ?s a ex:Patient . }"
        )
        expanded = expand_prefixes(sparql)
        assert f"<{NS}Patient>" in expanded

    def test_angle_bracket_uris_unchanged(self):
        sparql = f"SELECT ?s WHERE {{ ?s <{NS}firstName> ?n . }}"
        assert expand_prefixes(sparql) == sparql

    def test_no_prefixes_unchanged(self):
        sparql = "SELECT ?s WHERE { ?s ?p ?o . }"
        assert expand_prefixes(sparql) == sparql

    def test_multiple_prefixes_expanded(self):
        sparql = (
            "PREFIX ont: <http://ont/>\n"
            "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n"
            "SELECT ?s WHERE { ?s ont:prop ?v . FILTER(?v > xsd:integer) }"
        )
        expanded = expand_prefixes(sparql)
        assert "<http://ont/prop>" in expanded
        assert "ont:prop" not in expanded


class TestSemanticCardinality:
    def test_non_aggregate_uses_row_count(self, ontology):
        framing = build_framing(SPARQL_WITH_FILTER, _ROWS, ontology)
        assert framing.cardinality == len(_ROWS)

    def test_scalar_count_uses_count_value(self, ontology):
        # SPARQL_COUNT returns 1 row {"count": "3"} — cardinality should be 3
        count_rows = [{"count": "3"}]
        framing = build_framing(SPARQL_COUNT, count_rows, ontology)
        assert framing.cardinality == 3

    def test_scalar_count_zero(self, ontology):
        count_rows = [{"count": "0"}]
        framing = build_framing(SPARQL_COUNT, count_rows, ontology)
        assert framing.cardinality == 0

    def test_grouped_aggregate_uses_row_count(self, ontology):
        # GROUP BY query: many rows, each with a count — len(rows) is correct
        sparql = f"""
            PREFIX ex: <{NS}>
            SELECT ?gender (COUNT(?s) AS ?n)
            WHERE {{ GRAPH <http://g> {{ ?s a <{PATIENT_URI}> . ?s <{FIRST_NAME_URI}> ?name . }} }}
            GROUP BY ?gender
        """
        rows = [{"gender": "M", "n": "40"}, {"gender": "F", "n": "20"}]
        framing = build_framing(sparql, rows, ontology)
        assert framing.cardinality == 2  # number of groups, not 40 or 20


class TestPrefixedSparqlFraming:
    """Integration: prefixed CURIEs produce same framing as angle-bracket URIs."""

    def test_prefixed_property_detected_as_prop(self, ontology):
        sparql = (
            f"PREFIX ex: <{NS}>\n"
            "SELECT ?name WHERE { GRAPH <http://g> { ?s a ex:Patient . ?s ex:firstName ?name . } }"
        )
        framing = build_framing(sparql, _ROWS, ontology)
        assert framing.primary_class_uri == PATIENT_URI

    def test_prefixed_filter_detected_as_constraint(self, ontology):
        sparql = (
            f"PREFIX ex: <{NS}>\n"
            "SELECT ?name WHERE { GRAPH <http://g> { ?s ex:firstName ?name . FILTER(?name = \"Alice\") } }"
        )
        framing = build_framing(sparql, _ROWS, ontology)
        assert any(c.property_uri == FIRST_NAME_URI for c in framing.constraints_applied)

    def test_prefixed_count_cardinality(self, ontology):
        sparql = (
            f"PREFIX ex: <{NS}>\n"
            "SELECT (COUNT(?s) AS ?n) WHERE { GRAPH <http://g> { ?s a ex:Patient . } }"
        )
        framing = build_framing(sparql, [{"n": "60"}], ontology)
        assert framing.cardinality == 60
        assert framing.primary_class_uri == PATIENT_URI
