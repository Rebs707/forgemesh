# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Integration tests: ProofNarrator end-to-end."""
from __future__ import annotations

import pytest

from semantic_mapper.rdf.proof.models import ProofRecord
from semantic_mapper.rdf.proof.narrator import ProofNarrator
from tests.rdf.proof.conftest import (
    ENCOUNTER_URI,
    FIRST_NAME_URI,
    HAS_ENCOUNTER_URI,
    PATIENT_URI,
    NS,
)

_NAMED_GRAPH = "http://mesh.local/graph/test"

SPARQL_SIMPLE = f"""
    PREFIX ex: <{NS}>
    SELECT ?name
    WHERE {{
        GRAPH <{_NAMED_GRAPH}> {{
            ?s a <{PATIENT_URI}> .
            ?s <{FIRST_NAME_URI}> ?name .
            FILTER(?name = "Alice")
        }}
    }}
"""

SPARQL_JOIN = f"""
    PREFIX ex: <{NS}>
    SELECT ?name
    WHERE {{
        GRAPH <{_NAMED_GRAPH}> {{
            ?s a <{PATIENT_URI}> .
            ?s <{HAS_ENCOUNTER_URI}> ?encURI .
            ?encURI a <{ENCOUNTER_URI}> .
        }}
    }}
"""

_ROWS = [{"name": "Alice"}, {"name": "Bob"}]


class TestProofNarrator:
    def test_returns_proof_record(self, ontology):
        narrator = ProofNarrator(ontology)
        record = narrator.narrate(
            sparql=SPARQL_SIMPLE,
            nl_question="Show patients named Alice",
            rows=_ROWS,
            query_id="q-test-001",
        )
        assert isinstance(record, ProofRecord)

    def test_schema_version_is_v2(self, ontology):
        narrator = ProofNarrator(ontology)
        record = narrator.narrate(
            sparql=SPARQL_SIMPLE,
            nl_question="Show patients named Alice",
            rows=_ROWS,
            query_id="q1",
        )
        assert record.proof_schema_version == "2.0.0"

    def test_query_id_preserved(self, ontology):
        narrator = ProofNarrator(ontology)
        record = narrator.narrate(
            sparql=SPARQL_SIMPLE,
            nl_question="Q",
            rows=[],
            query_id="my-query-id",
        )
        assert record.query_id == "my-query-id"

    def test_framing_cardinality(self, ontology):
        narrator = ProofNarrator(ontology)
        record = narrator.narrate(SPARQL_SIMPLE, "Q", _ROWS, "q1")
        assert record.framing.cardinality == 2

    def test_framing_primary_class(self, ontology):
        narrator = ProofNarrator(ontology)
        record = narrator.narrate(SPARQL_SIMPLE, "Q", _ROWS, "q1")
        assert record.framing.primary_class_uri == PATIENT_URI

    def test_framing_joined_class_on_join_query(self, ontology):
        narrator = ProofNarrator(ontology)
        record = narrator.narrate(SPARQL_JOIN, "Patients and their encounters", _ROWS, "q2")
        assert ENCOUNTER_URI in record.framing.joined_class_uris

    def test_scope_assumptions_not_empty(self, ontology):
        narrator = ProofNarrator(ontology)
        record = narrator.narrate(SPARQL_SIMPLE, "Q", _ROWS, "q1")
        # firstName is constrained — gender and patientHasEncounter should appear
        assert len(record.scope_assumptions) >= 1

    def test_limitations_not_empty(self, ontology):
        narrator = ProofNarrator(ontology)
        record = narrator.narrate(SPARQL_SIMPLE, "Q", _ROWS, "q1")
        assert len(record.limitations) >= 1

    def test_ontology_subgraph_hash_present(self, ontology):
        narrator = ProofNarrator(ontology)
        record = narrator.narrate(SPARQL_SIMPLE, "Q", _ROWS, "q1")
        assert len(record.ontology_subgraph_hash) == 64  # SHA-256 hex

    def test_hash_is_stable_across_calls(self, ontology):
        narrator = ProofNarrator(ontology)
        r1 = narrator.narrate(SPARQL_SIMPLE, "Q", _ROWS, "q1")
        r2 = narrator.narrate(SPARQL_SIMPLE, "Q", _ROWS, "q1")
        assert r1.ontology_subgraph_hash == r2.ontology_subgraph_hash

    def test_no_ontology_produces_valid_record(self):
        narrator = ProofNarrator(None)
        record = narrator.narrate(SPARQL_SIMPLE, "Show patients named Alice", _ROWS, "q1")
        assert record.proof_schema_version == "2.0.0"
        assert record.framing.cardinality == 2
        assert len(record.limitations) >= 1
        assert record.interpretation.confidence == "LOW"

    def test_grounding_error_degrades_to_ungrounded_framing(self, ontology, caplog):
        """A FILTER on a property absent from the ontology no longer aborts proof
        generation: the narrator logs the grounding error and falls back to an
        ungrounded framing, still returning a valid record."""
        bad_sparql = """
            SELECT ?s WHERE {
                GRAPH <http://g> {
                    ?s <http://ont/unknownProperty> ?val .
                    FILTER(?val = "x")
                }
            }
        """
        narrator = ProofNarrator(ontology)
        with caplog.at_level("WARNING"):
            record = narrator.narrate(bad_sparql, "unknown", [], "q1")
        assert record.proof_schema_version == "2.0.0"
        assert "OntologyGroundingError" in caplog.text

    def test_generated_at_timezone_aware(self, ontology):
        import datetime
        narrator = ProofNarrator(ontology)
        record = narrator.narrate(SPARQL_SIMPLE, "Q", _ROWS, "q1")
        assert record.generated_at.tzinfo is not None

    def test_json_round_trip(self, ontology):
        import json
        narrator = ProofNarrator(ontology)
        record = narrator.narrate(SPARQL_SIMPLE, "Show patients named Alice", _ROWS, "q1")
        json_str = record.model_dump_json()
        parsed = json.loads(json_str)
        assert parsed["proof_schema_version"] == "2.0.0"
        assert parsed["framing"]["cardinality"] == 2
