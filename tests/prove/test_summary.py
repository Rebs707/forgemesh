# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 3 tests — summarise_proof and ProofSummary."""
from __future__ import annotations

import json

import pytest

from semantic_mapper.prove.models import REQUIRED_LIMITATIONS, ProofRecord
from semantic_mapper.prove.summary import (
    PROOF_SUMMARY_VERSION,
    DataAuditSummary,
    MappingConfidenceSummary,
    ProofSummary,
    summarise_proof,
)


def _make_record(**overrides) -> ProofRecord:
    base = dict(
        query_mode="gold",
        question="How many admissions?",
        answer={"value": 58976, "type": "count"},
        interpretation={
            "interpreted_as": "Total admission count",
            "scope_assumptions": [],
            "not_included": [],
            "interpretation_confidence": "HIGH",
            "interpretation_confidence_reason": "",
            "narrated": True,
            "ontology_subgraph_attached": False,
        },
        ontology_subgraph={"classes": {}, "properties": {}, "adjacency_notes": []},
        executed_query="SELECT COUNT(*) FROM gold_admission",
        proof_query="SELECT _gold_id FROM gold_admission",
        subject_count=58976,
        subjects=["1", "2", "3"],
        lineage=[
            {"subject_uri": "u1", "broken": False},
            {"subject_uri": "u2", "broken": True, "broken_reason": "missing silver row"},
        ],
        mapping_decisions=[
            {"uri": "ont:hasAdmissionType", "confirmation": "HUMAN", "score": 0.99},
            {"uri": "ont:hasHadmId", "confirmation": "AUTO", "score": 0.84},
        ],
        caveats=[
            {"type": "QUARANTINED", "count": 3, "table": "silver_admissions",
             "property_uri": None, "reason": "null key"},
        ],
        limitations=list(REQUIRED_LIMITATIONS),
        mapping_report_id="run_abc",
        generated_at="2026-05-15T12:00:00+00:00",
    )
    base.update(overrides)
    return ProofRecord(**base)


def test_summary_version():
    record = _make_record()
    summary = summarise_proof(record, "q_test123")
    assert summary.proof_summary_version == PROOF_SUMMARY_VERSION


def test_query_id_preserved():
    record = _make_record()
    summary = summarise_proof(record, "q_abc456")
    assert summary.query_id == "q_abc456"


def test_question_preserved():
    record = _make_record()
    summary = summarise_proof(record, "q_x")
    assert summary.question == record.question


def test_answer_preserved():
    record = _make_record()
    summary = summarise_proof(record, "q_x")
    assert summary.answer == {"value": 58976, "type": "count"}


def test_subject_count_in_audit():
    record = _make_record()
    summary = summarise_proof(record, "q_x")
    assert summary.data_audit_summary.subject_count == 58976


def test_broken_lineage_count():
    record = _make_record()
    summary = summarise_proof(record, "q_x")
    assert summary.data_audit_summary.broken_lineage_count == 1


def test_quarantined_rows_from_caveats():
    record = _make_record()
    summary = summarise_proof(record, "q_x")
    assert summary.data_audit_summary.quarantined_rows == 3


def test_mapping_confidence_counts():
    record = _make_record()
    summary = summarise_proof(record, "q_x")
    conf = summary.mapping_confidence_summary
    assert conf.human_confirmed_count == 1
    assert conf.auto_confirmed_count == 1
    assert not conf.all_mappings_human_confirmed


def test_lowest_score():
    record = _make_record()
    summary = summarise_proof(record, "q_x")
    assert abs(summary.mapping_confidence_summary.lowest_score - 0.84) < 1e-6


def test_limitations_required_strings_present():
    record = _make_record()
    summary = summarise_proof(record, "q_x")
    for req in REQUIRED_LIMITATIONS:
        assert req in summary.limitations


def test_full_record_available_via():
    record = _make_record()
    summary = summarise_proof(record, "q_x")
    assert summary.full_record_available_via == "get_proof_detail"


def test_deterministic_same_output():
    record = _make_record()
    s1 = summarise_proof(record, "q_x")
    s2 = summarise_proof(record, "q_x")
    assert s1 == s2


def test_to_dict_is_json_serialisable():
    record = _make_record()
    summary = summarise_proof(record, "q_x")
    dumped = json.dumps(summary.to_dict())
    reloaded = json.loads(dumped)
    assert reloaded["query_id"] == "q_x"
    assert "data_audit_summary" in reloaded
    assert "mapping_confidence_summary" in reloaded


def test_no_decisions_gives_none_lowest_score():
    record = _make_record(mapping_decisions=[])
    summary = summarise_proof(record, "q_x")
    conf = summary.mapping_confidence_summary
    assert conf.lowest_score is None
    assert conf.human_confirmed_count == 0
    assert conf.auto_confirmed_count == 0
