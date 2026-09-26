# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for Phase 8 — Markdown renderer.

All tests use a fixed ProofRecord so output is deterministic.
"""
from __future__ import annotations

import pytest

from semantic_mapper.prove.composer import compose_proof
from semantic_mapper.prove.decision_lookup import MappingDecisionRef
from semantic_mapper.prove.lineage_walker import (
    BronzeRecord,
    GoldRecord,
    LineageChain,
    SilverRecord,
)
from semantic_mapper.prove.models import REQUIRED_LIMITATIONS, ProofRecord
from semantic_mapper.prove.narrator import InterpretationNote, ProofCaveat
from semantic_mapper.prove.query_rewriter import ProofQuery
from semantic_mapper.prove.render.markdown import render_proof
from semantic_mapper.prove.subgraph import ClassNode, OntologySubgraph, PropertyNode
from semantic_mapper.schema.mapping_output import MappingEvidence
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_CLASS_URI = "http://mesh.local/ontology#Admission"
_PROP_URI = "http://mesh.local/ontology#admittime"
_ADJ_PROP_URI = "http://mesh.local/ontology#admissionType"
_SUBJ_A = "http://mesh.local/data/admission/100"
_SUBJ_B = "http://mesh.local/data/admission/200"


def _make_record(
    narrated: bool = True,
    subjects: list[str] | None = None,
    caveats: list[ProofCaveat] | None = None,
    decisions: list[MappingDecisionRef] | None = None,
    chains: list[LineageChain] | None = None,
    scope_assumptions: list[str] | None = None,
    not_included: list[str] | None = None,
) -> ProofRecord:
    if subjects is None:
        subjects = [_SUBJ_A]
    if caveats is None:
        caveats = []
    if scope_assumptions is None:
        scope_assumptions = ["All admissionType values included."] if narrated else []
    if not_included is None:
        not_included = ["2 rows with NULL admittime excluded."] if narrated else []

    if decisions is None:
        decisions = [
            MappingDecisionRef(
                uri=_CLASS_URI, kind="Class", source="admissions",
                winning_strategy="", score=0.92, confirmation="AUTO",
                confirmed_at=None, evidence_trail=[],
            ),
            MappingDecisionRef(
                uri=_ADJ_PROP_URI, kind="DatatypeProperty",
                source="admissions.admission_type",
                winning_strategy="NameMatcher", score=0.85,
                confirmation="AUTO", confirmed_at=None,
                evidence_trail=[
                    MappingEvidence(strategy="NameMatcher", score=0.85, rationale="exact")
                ],
            ),
        ]

    if chains is None and subjects:
        chains = [
            LineageChain(
                subject_uri=subjects[0],
                row_id=None,
                gold=GoldRecord("gold_admission", 1, ["subject_id"]),
                silver=[SilverRecord("silver_admissions", 1, [])],
                bronze=[BronzeRecord(
                    "raw_admissions", {"row_id": "1"},
                    {"subject_id": "42", "admission_type": "URGENT"},
                )],
                broken=False, broken_reason=None,
            )
        ]
    elif chains is None:
        chains = []

    subgraph = OntologySubgraph(
        classes={_CLASS_URI: ClassNode(_CLASS_URI, "Admission", "", "admissions")},
        properties={
            _PROP_URI: PropertyNode(
                _PROP_URI, "admittime", "", "admissions.admittime", "",
                "DATETIME", "0.02", _CLASS_URI, "", "DatatypeProperty",
            ),
            _ADJ_PROP_URI: PropertyNode(
                _ADJ_PROP_URI, "admissionType", "", "admissions.admission_type", "",
                "VARCHAR", "0.00", _CLASS_URI, "", "DatatypeProperty",
            ),
        },
        adjacency_notes=[
            "Admission has property admittime — filtered in this query",
            "Admission has property admissionType — not filtered in this query",
        ],
    )

    note = InterpretationNote(
        interpreted_as="Distinct admission events last month." if narrated else "",
        scope_assumptions=scope_assumptions,
        not_included=not_included,
        interpretation_confidence="HIGH",
        interpretation_confidence_reason="Explicit time window.",
        narrated=narrated,
        narrator_model="gpt-4o-mini" if narrated else None,
    )

    return compose_proof(
        question="How many admissions occurred last month?",
        query="SELECT (COUNT(?a) AS ?n) WHERE { ?a a :Admission . }",
        mode="sparql",
        answer={"value": 51, "type": "count"},
        run_id="run_001",
        proof_query=ProofQuery(
            proof_query="SELECT DISTINCT ?a WHERE { ?a a :Admission . } LIMIT 1000",
            mode="sparql", sampled=False, original_count=51, sample_limit=1000,
        ),
        subjects=subjects,
        chains=chains,
        subgraph=subgraph,
        decisions=decisions,
        note=note,
        caveats=caveats or [],
        settings=MagicMock(),
    )


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_identical_output_on_same_record(self):
        record = _make_record()
        assert render_proof(record) == render_proof(record)

    def test_identical_output_called_twice(self):
        record = _make_record()
        first = render_proof(record)
        second = render_proof(record)
        assert first == second

    def test_subjects_sorted_alphabetically(self):
        record = _make_record(subjects=[_SUBJ_B, _SUBJ_A], chains=[])
        md = render_proof(record)
        pos_a = md.index(_SUBJ_A)
        pos_b = md.index(_SUBJ_B)
        assert pos_a < pos_b

    def test_decisions_sorted_by_uri(self):
        record = _make_record()
        md = render_proof(record)
        pos_admission = md.index("Admission")
        pos_admissiontype = md.rindex("admissionType")
        assert pos_admission < pos_admissiontype


# ---------------------------------------------------------------------------
# Section order
# ---------------------------------------------------------------------------

class TestSectionOrder:
    def test_header_before_interpretation(self):
        md = render_proof(_make_record())
        assert md.index("# How many") < md.index("## What we understood")

    def test_interpretation_before_query(self):
        md = render_proof(_make_record())
        assert md.index("## What we understood") < md.index("## The query")

    def test_query_before_audit(self):
        md = render_proof(_make_record())
        assert md.index("## The query") < md.index("## Source data audit")

    def test_audit_before_mapping_confidence(self):
        md = render_proof(_make_record())
        assert md.index("## Source data audit") < md.index("## Mapping confidence")

    def test_limitations_is_last_section(self):
        md = render_proof(_make_record())
        assert md.rindex("## Limitations") > md.rindex("## Mapping confidence")

    def test_limitations_ends_the_document(self):
        md = render_proof(_make_record())
        lim_pos = md.rindex("## Limitations of this proof")
        after = md[lim_pos:]
        # No further ## headings after limitations
        assert "## " not in after.split("\n", 1)[1]


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

class TestHeader:
    def test_question_in_header(self):
        md = render_proof(_make_record())
        assert "How many admissions occurred last month?" in md

    def test_answer_value_in_header(self):
        md = render_proof(_make_record())
        assert "51" in md

    def test_verified_badge(self):
        md = render_proof(_make_record())
        assert "VERIFIED" in md

    def test_mode_in_header(self):
        md = render_proof(_make_record())
        assert "sparql" in md

    def test_timestamp_in_header(self):
        md = render_proof(_make_record())
        assert "Generated" in md


# ---------------------------------------------------------------------------
# Interpretation section
# ---------------------------------------------------------------------------

class TestInterpretation:
    def test_interpreted_as_present(self):
        md = render_proof(_make_record())
        assert "Distinct admission events" in md

    def test_scope_assumptions_present(self):
        md = render_proof(_make_record())
        assert "admissionType" in md

    def test_not_included_present(self):
        md = render_proof(_make_record())
        assert "NULL admittime" in md

    def test_confidence_present(self):
        md = render_proof(_make_record())
        assert "HIGH" in md

    def test_narrated_false_shows_disabled_note(self):
        md = render_proof(_make_record(narrated=False))
        assert "disabled" in md.lower()

    def test_narrated_false_no_interpreted_as(self):
        md = render_proof(_make_record(narrated=False))
        assert "Distinct admission events" not in md

    def test_caveats_shown(self):
        caveats = [ProofCaveat(type="SAMPLED", count=200, table=None, property_uri=None, reason=None)]
        md = render_proof(_make_record(caveats=caveats))
        assert "SAMPLED" in md

    def test_empty_scope_assumptions_no_scope_header(self):
        md = render_proof(_make_record(scope_assumptions=[]))
        assert "Scope assumptions" not in md

    def test_empty_not_included_no_not_included_header(self):
        md = render_proof(_make_record(not_included=[]))
        assert "Not included" not in md


# ---------------------------------------------------------------------------
# Query section
# ---------------------------------------------------------------------------

class TestQuerySection:
    def test_executed_query_present(self):
        md = render_proof(_make_record())
        assert "COUNT(?a)" in md

    def test_proof_query_present(self):
        md = render_proof(_make_record())
        assert "DISTINCT ?a" in md

    def test_sparql_fenced_code(self):
        md = render_proof(_make_record())
        assert "```sparql" in md

    def test_sql_fenced_code_for_gold_mode(self):
        record = _make_record()
        # Patch mode to gold
        patched = ProofRecord(
            **{**record.model_dump(), "query_mode": "gold"}
        )
        md = render_proof(patched)
        assert "```sql" in md


# ---------------------------------------------------------------------------
# Source data audit
# ---------------------------------------------------------------------------

class TestSourceAudit:
    def test_subject_uri_in_table(self):
        md = render_proof(_make_record())
        assert _SUBJ_A in md

    def test_bronze_table_in_table(self):
        md = render_proof(_make_record())
        assert "raw_admissions" in md

    def test_pk_in_table(self):
        md = render_proof(_make_record())
        assert "row_id=1" in md

    def test_subject_count_shown(self):
        md = render_proof(_make_record())
        assert "1 subject" in md

    def test_no_subjects_shows_none(self):
        md = render_proof(_make_record(subjects=[], chains=[]))
        assert "No subjects" in md

    def test_multiple_subjects_sorted(self):
        record = _make_record(subjects=[_SUBJ_B, _SUBJ_A], chains=[])
        md = render_proof(record)
        pos_a = md.index(_SUBJ_A)
        pos_b = md.index(_SUBJ_B)
        assert pos_a < pos_b


# ---------------------------------------------------------------------------
# Mapping confidence
# ---------------------------------------------------------------------------

class TestMappingConfidence:
    def test_class_in_table(self):
        md = render_proof(_make_record())
        assert "Admission" in md

    def test_adjacent_property_in_table(self):
        # admissionType is adjacent (not in the query) — must appear
        md = render_proof(_make_record())
        assert "admissionType" in md

    def test_confirmation_shown(self):
        md = render_proof(_make_record())
        assert "AUTO" in md

    def test_score_shown(self):
        md = render_proof(_make_record())
        assert "0.92" in md or "0.85" in md

    def test_empty_decisions_shows_none(self):
        md = render_proof(_make_record(decisions=[]))
        assert "No mapping decisions" in md


# ---------------------------------------------------------------------------
# Limitations
# ---------------------------------------------------------------------------

class TestLimitations:
    def test_all_three_required_strings_present(self):
        md = render_proof(_make_record())
        for lim in REQUIRED_LIMITATIONS:
            assert lim in md

    def test_limitations_section_heading(self):
        md = render_proof(_make_record())
        assert "## Limitations of this proof" in md
