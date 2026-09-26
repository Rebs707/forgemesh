# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for Phase 7 — Proof composer and JSON contract.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from semantic_mapper.prove.composer import compose_proof, compose_proof_tree
from semantic_mapper.prove.decision_lookup import MappingDecisionRef
from semantic_mapper.prove.lineage_walker import (
    BronzeRecord,
    GoldRecord,
    LineageChain,
    SilverRecord,
)
from semantic_mapper.prove.models import REQUIRED_LIMITATIONS, ProofRecord, ProofTree
from semantic_mapper.prove.narrator import InterpretationNote, ProofCaveat
from semantic_mapper.prove.query_rewriter import ProofQuery
from semantic_mapper.prove.subgraph import ClassNode, OntologySubgraph, PropertyNode
from semantic_mapper.schema.mapping_output import MappingEvidence

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_CLASS_URI = "http://mesh.local/ontology#Admission"
_PROP_URI = "http://mesh.local/ontology#admittime"
_SUBJECT_URI = "http://mesh.local/data/admission/42"


def _subgraph() -> OntologySubgraph:
    return OntologySubgraph(
        classes={
            _CLASS_URI: ClassNode(
                uri=_CLASS_URI, label="Admission",
                comment="An admission.", source_table="admissions",
            )
        },
        properties={
            _PROP_URI: PropertyNode(
                uri=_PROP_URI, label="admittime", comment="",
                source_column="admissions.admittime", source_fk="",
                sql_type="DATETIME", null_rate="0.02",
                domain_uri=_CLASS_URI, range_uri="", kind="DatatypeProperty",
            )
        },
        adjacency_notes=[
            "Admission has property admittime (from admissions.admittime) — filtered in this query"
        ],
    )


def _proof_query() -> ProofQuery:
    return ProofQuery(
        proof_query="SELECT DISTINCT ?a WHERE { ?a a :Admission . } LIMIT 1000",
        mode="sparql",
        sampled=False,
        original_count=51,
        sample_limit=1000,
    )


def _chain() -> LineageChain:
    return LineageChain(
        subject_uri=_SUBJECT_URI,
        row_id=None,
        gold=GoldRecord(table="gold_admission", id=1, fields_used=["subject_id"]),
        silver=[SilverRecord(table="silver_admissions", id=1, transformations=[])],
        bronze=[BronzeRecord(
            table="raw_admissions",
            pk={"row_id": "1"},
            raw_values={"subject_id": "42", "admission_type": "URGENT"},
        )],
        broken=False,
        broken_reason=None,
    )


def _note() -> InterpretationNote:
    return InterpretationNote(
        interpreted_as="Distinct admission events last month.",
        scope_assumptions=["All admissionType values included."],
        not_included=["2 rows with NULL admittime excluded."],
        interpretation_confidence="HIGH",
        interpretation_confidence_reason="Explicit time window.",
        narrated=True,
        narrator_model="gpt-4o-mini",
    )


def _decisions() -> list[MappingDecisionRef]:
    return [
        MappingDecisionRef(
            uri=_CLASS_URI,
            kind="Class",
            source="admissions",
            winning_strategy="",
            score=0.92,
            confirmation="AUTO",
            confirmed_at=None,
            evidence_trail=[
                MappingEvidence(strategy="NameMatcher", score=0.85, rationale="exact")
            ],
        )
    ]


def _caveats() -> list[ProofCaveat]:
    return [
        ProofCaveat(type="SAMPLED", count=200, table=None, property_uri=None, reason=None)
    ]


def _settings():
    return MagicMock()


def _compose(**overrides) -> ProofRecord:
    kwargs = dict(
        question="How many admissions?",
        query="SELECT (COUNT(?a) AS ?n) WHERE { ?a a :Admission . }",
        mode="sparql",
        answer={"value": 51, "type": "count"},
        run_id="run_001",
        proof_query=_proof_query(),
        subjects=[_SUBJECT_URI],
        chains=[_chain()],
        subgraph=_subgraph(),
        decisions=_decisions(),
        note=_note(),
        caveats=_caveats(),
        settings=_settings(),
    )
    kwargs.update(overrides)
    return compose_proof(**kwargs)


# ---------------------------------------------------------------------------
# ProofRecord model — limitations validator
# ---------------------------------------------------------------------------

class TestLimitationsValidator:
    def test_all_required_limitations_present(self):
        record = _compose()
        for s in REQUIRED_LIMITATIONS:
            assert s in record.limitations

    def test_missing_one_limitation_raises(self):
        with pytest.raises(ValueError, match="missing required"):
            ProofRecord(
                query_mode="sparql",
                question="Q",
                answer={},
                interpretation={},
                ontology_subgraph={},
                executed_query="",
                proof_query="",
                subject_count=0,
                subjects=[],
                lineage=[],
                mapping_decisions=[],
                caveats=[],
                limitations=[REQUIRED_LIMITATIONS[0], REQUIRED_LIMITATIONS[1]],  # missing [2]
                mapping_report_id="run",
                generated_at="2026-01-01T00:00:00+00:00",
            )

    def test_missing_all_limitations_raises(self):
        with pytest.raises(ValueError):
            ProofRecord(
                query_mode="sparql",
                question="Q",
                answer={},
                interpretation={},
                ontology_subgraph={},
                executed_query="",
                proof_query="",
                subject_count=0,
                subjects=[],
                lineage=[],
                mapping_decisions=[],
                caveats=[],
                limitations=[],
                mapping_report_id="run",
                generated_at="2026-01-01T00:00:00+00:00",
            )

    def test_extra_limitations_allowed(self):
        record = ProofRecord(
            query_mode="sparql",
            question="Q",
            answer={},
            interpretation={},
            ontology_subgraph={},
            executed_query="",
            proof_query="",
            subject_count=0,
            subjects=[],
            lineage=[],
            mapping_decisions=[],
            caveats=[],
            limitations=list(REQUIRED_LIMITATIONS) + ["An extra note."],
            mapping_report_id="run",
            generated_at="2026-01-01T00:00:00+00:00",
        )
        assert len(record.limitations) == 4


# ---------------------------------------------------------------------------
# ProofRecord model — frozen
# ---------------------------------------------------------------------------

class TestProofRecordFrozen:
    def test_cannot_set_attribute(self):
        record = _compose()
        with pytest.raises(Exception):
            record.question = "changed"  # type: ignore[misc]

    def test_cannot_set_subjects(self):
        record = _compose()
        with pytest.raises(Exception):
            record.subjects = []  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ProofRecord model — JSON round-trip
# ---------------------------------------------------------------------------

class TestJsonRoundTrip:
    def test_model_dump_json_parseable(self):
        record = _compose()
        raw = record.model_dump_json()
        data = json.loads(raw)
        assert data["proof_schema_version"] == "1.0.0"

    def test_round_trip_preserves_question(self):
        record = _compose()
        restored = ProofRecord.model_validate_json(record.model_dump_json())
        assert restored.question == record.question

    def test_round_trip_preserves_subject_count(self):
        record = _compose()
        restored = ProofRecord.model_validate_json(record.model_dump_json())
        assert restored.subject_count == record.subject_count

    def test_round_trip_preserves_limitations(self):
        record = _compose()
        restored = ProofRecord.model_validate_json(record.model_dump_json())
        assert set(restored.limitations) == set(record.limitations)

    def test_round_trip_preserves_caveats(self):
        record = _compose()
        restored = ProofRecord.model_validate_json(record.model_dump_json())
        assert restored.caveats == record.caveats


# ---------------------------------------------------------------------------
# compose_proof — field assembly
# ---------------------------------------------------------------------------

class TestComposeProof:
    def test_schema_version_is_1_0_0(self):
        assert _compose().proof_schema_version == "1.0.0"

    def test_query_mode(self):
        assert _compose().query_mode == "sparql"

    def test_question_preserved(self):
        assert _compose().question == "How many admissions?"

    def test_answer_preserved(self):
        assert _compose().answer == {"value": 51, "type": "count"}

    def test_executed_query_preserved(self):
        record = _compose()
        assert "COUNT" in record.executed_query

    def test_proof_query_from_rewriter(self):
        record = _compose()
        assert "DISTINCT" in record.proof_query

    def test_subject_count_matches_subjects(self):
        record = _compose(subjects=[_SUBJECT_URI, "http://mesh.local/data/admission/43"])
        assert record.subject_count == 2

    def test_subjects_list(self):
        record = _compose()
        assert _SUBJECT_URI in record.subjects

    def test_mapping_report_id(self):
        assert _compose().mapping_report_id == "run_001"

    def test_generated_at_is_iso(self):
        record = _compose()
        # Must parse without error
        from datetime import datetime
        datetime.fromisoformat(record.generated_at)

    def test_lineage_serialised(self):
        record = _compose()
        assert len(record.lineage) == 1
        chain_dict = record.lineage[0]
        assert chain_dict["gold"]["table"] == "gold_admission"
        assert chain_dict["broken"] is False

    def test_lineage_bronze_raw_values(self):
        record = _compose()
        bronze = record.lineage[0]["bronze"][0]
        assert bronze["raw_values"]["admission_type"] == "URGENT"

    def test_mapping_decisions_serialised(self):
        record = _compose()
        assert len(record.mapping_decisions) == 1
        assert record.mapping_decisions[0]["uri"] == _CLASS_URI
        assert record.mapping_decisions[0]["confirmation"] == "AUTO"

    def test_mapping_decisions_evidence_trail(self):
        record = _compose()
        trail = record.mapping_decisions[0]["evidence_trail"]
        assert trail[0]["strategy"] == "NameMatcher"

    def test_caveats_serialised(self):
        record = _compose()
        assert len(record.caveats) == 1
        assert record.caveats[0]["type"] == "SAMPLED"
        assert record.caveats[0]["count"] == 200

    def test_interpretation_interpreted_as(self):
        record = _compose()
        assert "admission" in record.interpretation["interpreted_as"].lower()

    def test_interpretation_narrated(self):
        assert _compose().interpretation["narrated"] is True

    def test_interpretation_subgraph_attached(self):
        assert _compose().interpretation["ontology_subgraph_attached"] is True

    def test_ontology_subgraph_classes(self):
        record = _compose()
        assert _CLASS_URI in record.ontology_subgraph["classes"]

    def test_ontology_subgraph_properties(self):
        record = _compose()
        assert _PROP_URI in record.ontology_subgraph["properties"]

    def test_ontology_subgraph_adjacency_notes(self):
        record = _compose()
        assert len(record.ontology_subgraph["adjacency_notes"]) == 1

    def test_empty_subjects(self):
        record = _compose(subjects=[])
        assert record.subject_count == 0
        assert record.subjects == []

    def test_empty_chains(self):
        record = _compose(chains=[])
        assert record.lineage == []

    def test_empty_caveats(self):
        record = _compose(caveats=[])
        assert record.caveats == []


# ---------------------------------------------------------------------------
# ProofTree
# ---------------------------------------------------------------------------

class TestProofTree:
    def test_proof_tree_contains_records(self):
        r1 = _compose(question="Q1")
        r2 = _compose(question="Q2")
        tree = compose_proof_tree([r1, r2])
        assert isinstance(tree, ProofTree)
        assert len(tree.proofs) == 2

    def test_proof_tree_single_record(self):
        tree = compose_proof_tree([_compose()])
        assert len(tree.proofs) == 1

    def test_proof_tree_empty(self):
        tree = compose_proof_tree([])
        assert tree.proofs == []

    def test_proof_tree_frozen(self):
        tree = compose_proof_tree([_compose()])
        with pytest.raises(Exception):
            tree.proofs = []  # type: ignore[misc]

    def test_proof_tree_questions_preserved(self):
        r1 = _compose(question="Q1")
        r2 = _compose(question="Q2")
        tree = compose_proof_tree([r1, r2])
        questions = {p.question for p in tree.proofs}
        assert questions == {"Q1", "Q2"}
