# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 1 tests: Pydantic contracts and the ontology-grounded invariant."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from semantic_mapper.rdf.proof.models import (
    AppliedConstraint,
    InterpretedConcept,
    Limitation,
    ProofRecord,
    QuestionInterpretation,
    ResultFraming,
    ScopeAssumption,
)


class TestInterpretedConcept:
    def test_valid_concept(self):
        c = InterpretedConcept(
            nl_phrase="cardiac",
            mapped_to_uri="http://ont/Cardiac",
            mapping_form="DIRECT",
            grounding_annotation="cardiac disease",
            grounding_node_uri="http://ont/Cardiac",
        )
        assert c.nl_phrase == "cardiac"
        assert c.mapping_form == "DIRECT"

    def test_empty_grounding_annotation_raises(self):
        with pytest.raises(ValidationError, match="grounding_annotation"):
            InterpretedConcept(
                nl_phrase="cardiac",
                mapped_to_uri="http://ont/Cardiac",
                mapping_form="DIRECT",
                grounding_annotation="",
                grounding_node_uri="http://ont/Cardiac",
            )

    def test_whitespace_only_annotation_raises(self):
        with pytest.raises(ValidationError, match="grounding_annotation"):
            InterpretedConcept(
                nl_phrase="cardiac",
                mapped_to_uri="http://ont/Cardiac",
                mapping_form="DIRECT",
                grounding_annotation="   ",
                grounding_node_uri="http://ont/Cardiac",
            )

    def test_empty_grounding_node_uri_raises(self):
        with pytest.raises(ValidationError, match="grounding_node_uri"):
            InterpretedConcept(
                nl_phrase="cardiac",
                mapped_to_uri="http://ont/Cardiac",
                mapping_form="DIRECT",
                grounding_annotation="cardiac disease",
                grounding_node_uri="",
            )

    def test_invalid_mapping_form_raises(self):
        with pytest.raises(ValidationError):
            InterpretedConcept(
                nl_phrase="cardiac",
                mapped_to_uri="http://ont/Cardiac",
                mapping_form="FUZZY",  # invalid
                grounding_annotation="cardiac disease",
                grounding_node_uri="http://ont/Cardiac",
            )

    def test_frozen(self):
        c = InterpretedConcept(
            nl_phrase="cardiac",
            mapped_to_uri="http://ont/Cardiac",
            mapping_form="PATTERN",
            grounding_annotation="cardiac",
            grounding_node_uri="http://ont/Cardiac",
        )
        with pytest.raises(Exception):
            c.nl_phrase = "changed"  # type: ignore[misc]


class TestLimitation:
    def test_open_world(self):
        lim = Limitation(kind="OPEN_WORLD", text="Absence is not negation.")
        assert lim.grounding_node_uri is None

    def test_cardinality_with_node(self):
        lim = Limitation(kind="CARDINALITY", text="Min 1.", grounding_node_uri="http://ont/R1")
        assert lim.grounding_node_uri == "http://ont/R1"

    def test_invalid_kind_raises(self):
        with pytest.raises(ValidationError):
            Limitation(kind="INVENTED", text="bad")


class TestProofRecord:
    def test_schema_version(self):
        # Build a minimal valid ProofRecord and check version
        from datetime import datetime, timezone
        record = ProofRecord(
            query_id="q1",
            interpretation=QuestionInterpretation(
                nl_question="How many patients?",
                interpreted_concepts=[],
                confidence="LOW",
                confidence_reason="no ontology",
            ),
            framing=ResultFraming(
                cardinality=5,
                primary_class_uri="http://ont/Patient",
                joined_class_uris=[],
                constraints_applied=[],
            ),
            scope_assumptions=[],
            limitations=[Limitation(kind="OPEN_WORLD", text="open world")],
            ontology_subgraph_hash="abc123",
            generated_at=datetime.now(timezone.utc),
        )
        assert record.proof_schema_version == "2.0.0"
        assert record.framing.cardinality == 5

    def test_json_serialisable(self):
        import json
        from datetime import datetime, timezone
        record = ProofRecord(
            query_id="q1",
            interpretation=QuestionInterpretation(
                nl_question="Q",
                interpreted_concepts=[],
                confidence="LOW",
                confidence_reason="r",
            ),
            framing=ResultFraming(
                cardinality=0,
                primary_class_uri="",
                joined_class_uris=[],
                constraints_applied=[],
            ),
            scope_assumptions=[],
            limitations=[],
            ontology_subgraph_hash="",
            generated_at=datetime.now(timezone.utc),
        )
        # Must not raise
        json_str = record.model_dump_json()
        parsed = json.loads(json_str)
        assert parsed["proof_schema_version"] == "2.0.0"
