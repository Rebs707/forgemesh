# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for Phase 12 — compose_row_proof() in composer.py.
"""
from __future__ import annotations

from semantic_mapper.prove.composer import compose_row_proof
from semantic_mapper.prove.models import REQUIRED_LIMITATIONS, RowProofRecord


def test_compose_row_proof_returns_contributing_subjects():
    rec = compose_row_proof(
        question="How many admissions by diagnosis?",
        query="SELECT ?diag (COUNT(?adm) AS ?n) WHERE { ?adm :hasDiag ?diag . } GROUP BY ?diag",
        mode="sparql",
        row_grouping_values={"diag": "Sepsis"},
        run_id="run1",
        subjects=["http://mesh.local/data/adm/1"],
        chains=[],
        decisions=[],
        caveats=[],
    )
    assert "http://mesh.local/data/adm/1" in rec.contributing_subjects


def test_compose_row_proof_includes_relevant_mapping_decisions():
    from dataclasses import dataclass, field
    from typing import List, Optional

    from semantic_mapper.prove.decision_lookup import MappingDecisionRef

    d = MappingDecisionRef(
        uri="http://ont#hasDiag",
        kind="DatatypeProperty",
        source="admissions.diag",
        winning_strategy="",
        score=0.9,
        confirmation="AUTO",
        confirmed_at=None,
    )
    rec = compose_row_proof(
        question="q?",
        query="q",
        mode="sparql",
        row_grouping_values={"diag": "Sepsis"},
        run_id="run1",
        subjects=[],
        chains=[],
        decisions=[d],
        caveats=[],
    )
    assert any(x["uri"] == "http://ont#hasDiag" for x in rec.mapping_decisions_for_grouping)


def test_compose_row_proof_limitations_present():
    rec = compose_row_proof(
        question="q?",
        query="q",
        mode="sparql",
        row_grouping_values={},
        run_id="run1",
        subjects=[],
        chains=[],
        decisions=[],
        caveats=[],
    )
    for lim in REQUIRED_LIMITATIONS:
        assert lim in rec.limitations


def test_compose_row_proof_returns_frozen_model():
    rec = compose_row_proof(
        question="q?",
        query="q",
        mode="gold",
        row_grouping_values={"type": "ELECTIVE"},
        run_id="run2",
        subjects=[],
        chains=[],
        decisions=[],
        caveats=[],
    )
    assert isinstance(rec, RowProofRecord)
    assert rec.query_mode == "gold"
    assert rec.row_grouping_values == {"type": "ELECTIVE"}
    assert rec.mapping_report_id == "run2"


def test_compose_row_proof_generated_at_is_utc_iso():
    rec = compose_row_proof(
        question="q?",
        query="q",
        mode="sparql",
        row_grouping_values={},
        run_id="run1",
        subjects=[],
        chains=[],
        decisions=[],
        caveats=[],
    )
    assert "+" in rec.generated_at or rec.generated_at.endswith("Z")


def test_compose_row_proof_empty_subjects_valid():
    rec = compose_row_proof(
        question="q?",
        query="q",
        mode="sparql",
        row_grouping_values={},
        run_id="run1",
        subjects=[],
        chains=[],
        decisions=[],
        caveats=[],
    )
    assert rec.contributing_subjects == []
    assert rec.lineage == []
