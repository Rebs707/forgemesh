# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, Graph, URIRef

from semantic_mapper.gold.models import GoldColumnSpec, GoldTableDefinition
from semantic_mapper.rdf.query.context_builder import (
    _source_to_xsd_label,
    build_sparql_schema_context,
)


def _ontology_with_range(prop_uri: str, range_uri: str) -> Graph:
    g = Graph()
    prop = URIRef(prop_uri)
    g.add((prop, RDF.type, OWL.ObjectProperty))
    g.add((prop, RDFS.range, URIRef(range_uri)))
    return g


NAMED_GRAPH = "http://mesh.local/graph/current"
BASE_URI = "http://mesh.local/data/"


class TestSourceToXsdLabel:
    def test_date(self):
        assert _source_to_xsd_label("DATE") == "xsd:date"

    def test_datetime(self):
        assert _source_to_xsd_label("DATETIME") == "xsd:dateTime"

    def test_timestamp(self):
        assert _source_to_xsd_label("TIMESTAMP") == "xsd:dateTime"

    def test_int(self):
        assert _source_to_xsd_label("INT") == "xsd:integer"

    def test_bigint(self):
        assert _source_to_xsd_label("BIGINT") == "xsd:integer"

    def test_tinyint(self):
        assert _source_to_xsd_label("TINYINT") == "xsd:integer"

    def test_decimal(self):
        assert _source_to_xsd_label("DECIMAL(10,2)") == "xsd:decimal"

    def test_float(self):
        assert _source_to_xsd_label("FLOAT") == "xsd:double"

    def test_double(self):
        assert _source_to_xsd_label("DOUBLE") == "xsd:double"

    def test_boolean(self):
        assert _source_to_xsd_label("BOOLEAN") == "xsd:boolean"

    def test_varchar(self):
        assert _source_to_xsd_label("VARCHAR(100)") == "xsd:string"

    def test_text(self):
        assert _source_to_xsd_label("TEXT") == "xsd:string"


class TestBuildSparqlSchemaContext:
    def test_named_graph_in_output(self, simple_definitions):
        ctx = build_sparql_schema_context(simple_definitions, NAMED_GRAPH, BASE_URI)
        assert f"NAMED GRAPH: <{NAMED_GRAPH}>" in ctx

    def test_base_uri_in_output(self, simple_definitions):
        ctx = build_sparql_schema_context(simple_definitions, NAMED_GRAPH, BASE_URI)
        assert BASE_URI in ctx

    def test_patient_class_uri_in_output(self, simple_definitions):
        ctx = build_sparql_schema_context(simple_definitions, NAMED_GRAPH, BASE_URI)
        assert "ont:Patient" in ctx

    def test_encounter_class_uri_in_output(self, simple_definitions):
        ctx = build_sparql_schema_context(simple_definitions, NAMED_GRAPH, BASE_URI)
        assert "ont:Encounter" in ctx

    def test_property_uri_in_output(self, simple_definitions):
        ctx = build_sparql_schema_context(simple_definitions, NAMED_GRAPH, BASE_URI)
        assert "ont:patientId" in ctx
        assert "ont:dateOfBirth" in ctx

    def test_datatype_label_for_date(self, simple_definitions):
        ctx = build_sparql_schema_context(simple_definitions, NAMED_GRAPH, BASE_URI)
        # date_of_birth is DATE → xsd:date
        assert "xsd:date" in ctx

    def test_datatype_label_for_varchar(self, simple_definitions):
        ctx = build_sparql_schema_context(simple_definitions, NAMED_GRAPH, BASE_URI)
        assert "xsd:string" in ctx

    def test_fk_column_marked_as_object_property(self, simple_definitions):
        ctx = build_sparql_schema_context(simple_definitions, NAMED_GRAPH, BASE_URI)
        # 'patient' column in gold_encounter is a FK to gold_patient
        assert "object property → patient" in ctx

    def test_fk_join_pattern_included(self, simple_definitions):
        ctx = build_sparql_schema_context(simple_definitions, NAMED_GRAPH, BASE_URI)
        # The join pattern should reference hasPatient in ont: short form
        assert "ont:hasPatient" in ctx
        assert "join:" in ctx

    def test_entity_short_name_in_class_header(self, simple_definitions):
        ctx = build_sparql_schema_context(simple_definitions, NAMED_GRAPH, BASE_URI)
        assert "(entity: patient)" in ctx
        assert "(entity: encounter)" in ctx

    def test_instance_selection_hint(self, simple_definitions):
        ctx = build_sparql_schema_context(simple_definitions, NAMED_GRAPH, BASE_URI)
        assert "Use ?patientURI a ont:Patient to select instances." in ctx

    def test_empty_definitions_returns_header_only(self):
        ctx = build_sparql_schema_context([], NAMED_GRAPH, BASE_URI)
        assert f"NAMED GRAPH: <{NAMED_GRAPH}>" in ctx
        assert "CLASS" not in ctx


class TestJoinDirection:
    """Join patterns reflect ontology rdfs:range when graph is supplied."""

    def test_normal_direction_when_range_does_not_match_entity(
        self, simple_definitions
    ):
        """hasPatient range=Patient, processing encounter → no flip.
        Pattern: ?enc <hasPatient> ?patURI
        """
        ontology = _ontology_with_range(
            "http://ex.org/ontology#hasPatient",
            "http://ex.org/ontology#Patient",  # range ≠ Encounter → no flip
        )
        ctx = build_sparql_schema_context(
            simple_definitions, NAMED_GRAPH, BASE_URI, ontology_graph=ontology
        )
        # Normal direction: encounter is subject of the join
        assert "?encounterURI ont:hasPatient ?patientURI" in ctx

    def test_flipped_direction_when_range_matches_entity(self, simple_definitions):
        """patientHasEncounter range=Encounter, processing encounter → flip.
        Pattern: ?patURI <patientHasEncounter> ?enc
        """
        PROP = "http://ex.org/ontology#patientHasEncounter"
        flipped_definitions = [
            simple_definitions[0],  # gold_patient unchanged
            GoldTableDefinition(
                gold_table_name="gold_encounter",
                primary_class_uri="http://ex.org/ontology#Encounter",
                class_label="encounter",
                source_silver_tables=["silver_raw_encounter"],
                column_specs=[
                    GoldColumnSpec(
                        silver_table="silver_raw_encounter",
                        source_column="patient",
                        gold_column_name="patient",
                        source_data_type="VARCHAR(50)",
                        property_uri=PROP,
                    ),
                ],
            ),
        ]
        ontology = _ontology_with_range(
            PROP,
            "http://ex.org/ontology#Encounter",  # range = Encounter → flip
        )
        ctx = build_sparql_schema_context(
            flipped_definitions, NAMED_GRAPH, BASE_URI, ontology_graph=ontology
        )
        # Flipped: patient URI is subject of the join
        # PROP is in the ont: namespace so it's shortened to ont:patientHasEncounter
        assert "?patientURI ont:patientHasEncounter ?encounterURI" in ctx

    def test_no_ontology_graph_uses_normal_direction(self, simple_definitions):
        """Without ontology_graph, fall back to normal (entity-as-subject) direction."""
        ctx = build_sparql_schema_context(simple_definitions, NAMED_GRAPH, BASE_URI)
        assert "?encounterURI ont:hasPatient ?patientURI" in ctx


class TestTerminalObjectProperties:
    """Properties declared as owl:ObjectProperty but with no gold table for their range
    are value-mapped URI columns (e.g. marital status, language, insurance type).
    The context builder must not show them as xsd:string — the LLM must treat them as URIs.
    """

    _PROP = "http://ex.org/ontology#hasMaritalStatus"
    _RANGE = "http://ex.org/ontology#MaritalStatus"

    def _definitions(self):
        return [
            GoldTableDefinition(
                gold_table_name="gold_patient",
                primary_class_uri="http://ex.org/ontology#Patient",
                class_label="patient",
                source_silver_tables=["silver_raw_patient"],
                column_specs=[
                    GoldColumnSpec(
                        silver_table="silver_raw_patient",
                        source_column="marital_status",
                        gold_column_name="marital_status",
                        source_data_type="VARCHAR(50)",
                        property_uri=self._PROP,
                    ),
                ],
            )
        ]

    def _ontology(self):
        g = Graph()
        prop = URIRef(self._PROP)
        range_cls = URIRef(self._RANGE)
        g.add((prop, RDF.type, OWL.ObjectProperty))
        g.add((prop, RDFS.range, range_cls))
        g.add((range_cls, RDF.type, OWL.Class))
        return g

    def test_not_shown_as_xsd_string(self):
        ctx = build_sparql_schema_context(
            self._definitions(), NAMED_GRAPH, BASE_URI, ontology_graph=self._ontology()
        )
        assert "xsd:string" not in ctx

    def test_shown_as_object_property(self):
        ctx = build_sparql_schema_context(
            self._definitions(), NAMED_GRAPH, BASE_URI, ontology_graph=self._ontology()
        )
        assert "object property → MaritalStatus" in ctx

    def test_uri_filter_hint_present(self):
        ctx = build_sparql_schema_context(
            self._definitions(), NAMED_GRAPH, BASE_URI, ontology_graph=self._ontology()
        )
        assert "stores URI individual" in ctx

    def test_no_join_pattern_emitted(self):
        """No gold table exists for the range class so no (join: ...) is emitted."""
        ctx = build_sparql_schema_context(
            self._definitions(), NAMED_GRAPH, BASE_URI, ontology_graph=self._ontology()
        )
        assert "join:" not in ctx

    def test_without_ontology_falls_back_to_xsd(self):
        """Without an ontology graph, unknown properties still default to xsd:string."""
        ctx = build_sparql_schema_context(self._definitions(), NAMED_GRAPH, BASE_URI)
        assert "xsd:string" in ctx

    def test_range_fragment_in_output(self):
        """The range class local name appears in the output for disambiguation."""
        ctx = build_sparql_schema_context(
            self._definitions(), NAMED_GRAPH, BASE_URI, ontology_graph=self._ontology()
        )
        assert "MaritalStatus" in ctx
