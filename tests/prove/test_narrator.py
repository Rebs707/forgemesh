# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for Phase 6 — Proof narrator.

All LLM calls are mocked; no network or OpenAI key required.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from semantic_mapper.prove.decision_lookup import MappingDecisionRef
from semantic_mapper.prove.narrator import (
    InterpretationNote,
    ProofCaveat,
    narrate,
)
from semantic_mapper.prove.subgraph import ClassNode, OntologySubgraph, PropertyNode

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_QUESTION = "How many admissions occurred last month?"
_QUERY = "SELECT (COUNT(?a) AS ?n) WHERE { ?a a :Admission . }"
_ANSWER = {"value": 51, "type": "count"}

_CLASS_URI = "http://mesh.local/ontology#Admission"
_PROP_URI = "http://mesh.local/ontology#admissionType"
_PROP_URI_2 = "http://mesh.local/ontology#admittime"


def _make_subgraph(
    adjacency_notes: list[str] | None = None,
) -> OntologySubgraph:
    if adjacency_notes is None:
        adjacency_notes = [
            "Admission has property admissionType (from admissions.admission_type) — not filtered in this query",
            "Admission has property admittime (from admissions.admittime) — filtered in this query",
        ]
    return OntologySubgraph(
        classes={
            _CLASS_URI: ClassNode(
                uri=_CLASS_URI,
                label="Admission",
                comment="An admission event.",
                source_table="admissions",
            )
        },
        properties={
            _PROP_URI: PropertyNode(
                uri=_PROP_URI,
                label="admissionType",
                comment="Type of admission.",
                source_column="admissions.admission_type",
                source_fk="",
                sql_type="VARCHAR",
                null_rate="0.00",
                domain_uri=_CLASS_URI,
                range_uri="",
                kind="DatatypeProperty",
            ),
            _PROP_URI_2: PropertyNode(
                uri=_PROP_URI_2,
                label="admittime",
                comment="Admission timestamp.",
                source_column="admissions.admittime",
                source_fk="",
                sql_type="DATETIME",
                null_rate="0.02",
                domain_uri=_CLASS_URI,
                range_uri="",
                kind="DatatypeProperty",
            ),
        },
        adjacency_notes=adjacency_notes,
    )


def _make_decisions() -> list[MappingDecisionRef]:
    return [
        MappingDecisionRef(
            uri=_CLASS_URI,
            kind="Class",
            source="admissions",
            winning_strategy="",
            score=0.92,
            confirmation="AUTO",
            confirmed_at=None,
            evidence_trail=[],
        )
    ]


def _make_settings(narrate: bool = True, model: str = "gpt-4o-mini") -> MagicMock:
    s = MagicMock()
    s.PROVE_NARRATE = narrate
    s.PROVE_NARRATOR_MODEL = model
    s.OPENAI_API_KEY = "test-key"
    s.LLM_REQUEST_TIMEOUT = 30
    s.QUERY_PROVIDER = "openai"
    s.LLM_BASE_URL = None
    s.LLM_STREAM = False  # mocks return a single (non-streamed) response
    return s


def _llm_response(
    interpreted_as: str = "Distinct admission events in the last month.",
    scope_assumptions: list[str] | None = None,
    not_included: list[str] | None = None,
    confidence: str = "HIGH",
    confidence_reason: str = "Explicit time window.",
) -> MagicMock:
    if scope_assumptions is None:
        scope_assumptions = [
            "All admissionType values included — the query did not filter on admissionType."
        ]
    if not_included is None:
        not_included = []
    content = json.dumps({
        "interpreted_as": interpreted_as,
        "scope_assumptions": scope_assumptions,
        "not_included": not_included,
        "interpretation_confidence": confidence,
        "interpretation_confidence_reason": confidence_reason,
    })
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = content
    return mock_resp


def _call(
    *,
    settings=None,
    subgraph=None,
    decisions=None,
    caveats=None,
    llm_response=None,
):
    if settings is None:
        settings = _make_settings()
    if subgraph is None:
        subgraph = _make_subgraph()
    if decisions is None:
        decisions = _make_decisions()
    if caveats is None:
        caveats = []
    if llm_response is None:
        llm_response = _llm_response()

    with patch("openai.OpenAI") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client
        mock_client.chat.completions.create.return_value = llm_response
        return narrate(
            _QUESTION, _QUERY, _ANSWER, subgraph, decisions, caveats, settings
        ), mock_client


# ---------------------------------------------------------------------------
# PROVE_NARRATE=False
# ---------------------------------------------------------------------------

class TestNarrateDisabled:
    def test_returns_immediately_no_llm_call(self):
        settings = _make_settings(narrate=False)
        with patch("openai.OpenAI") as mock_cls:
            note, caveats = narrate(
                _QUESTION, _QUERY, _ANSWER, _make_subgraph(), [], [], settings
            )
            mock_cls.assert_not_called()

    def test_narrated_is_false(self):
        settings = _make_settings(narrate=False)
        note, _ = narrate(
            _QUESTION, _QUERY, _ANSWER, _make_subgraph(), [], [], settings
        )
        assert note.narrated is False

    def test_narrator_model_is_none(self):
        settings = _make_settings(narrate=False)
        note, _ = narrate(
            _QUESTION, _QUERY, _ANSWER, _make_subgraph(), [], [], settings
        )
        assert note.narrator_model is None

    def test_caveats_passed_through_unchanged(self):
        settings = _make_settings(narrate=False)
        existing = [ProofCaveat(type="SAMPLED", count=200, table=None, property_uri=None, reason=None)]
        _, caveats = narrate(
            _QUESTION, _QUERY, _ANSWER, _make_subgraph(), [], existing, settings
        )
        assert caveats == existing


# ---------------------------------------------------------------------------
# Valid LLM response
# ---------------------------------------------------------------------------

class TestValidResponse:
    def test_returns_interpretation_note(self):
        (note, _), _ = _call()
        assert isinstance(note, InterpretationNote)

    def test_narrated_true(self):
        (note, _), _ = _call()
        assert note.narrated is True

    def test_interpreted_as_populated(self):
        (note, _), _ = _call()
        assert "admission" in note.interpreted_as.lower()

    def test_scope_assumptions_present(self):
        (note, _), _ = _call()
        assert len(note.scope_assumptions) >= 1

    def test_interpretation_confidence_valid(self):
        (note, _), _ = _call()
        assert note.interpretation_confidence in {"HIGH", "MEDIUM", "LOW"}

    def test_interpretation_confidence_reason(self):
        (note, _), _ = _call()
        assert note.interpretation_confidence_reason != ""

    def test_narrator_model_set(self):
        (note, _), _ = _call(settings=_make_settings(model="gpt-4o-mini"))
        assert note.narrator_model == "gpt-4o-mini"

    def test_no_extra_caveats_on_clean_response(self):
        (_, caveats), _ = _call(caveats=[])
        # Only NARRATOR_UNGROUNDED caveats could be added; clean response adds none
        assert not any(c.type == "NARRATION_FAILED" for c in caveats)

    def test_existing_caveats_preserved(self):
        existing = [ProofCaveat(type="SAMPLED", count=200, table=None, property_uri=None, reason=None)]
        (_, caveats), _ = _call(caveats=existing)
        assert any(c.type == "SAMPLED" for c in caveats)

    def test_invalid_confidence_normalised_to_low(self):
        resp = _llm_response(confidence="VERY HIGH")
        (note, _), _ = _call(llm_response=resp)
        assert note.interpretation_confidence == "LOW"

    def test_not_included_list_preserved(self):
        resp = _llm_response(not_included=["2 rows with NULL admittime excluded."])
        (note, _), _ = _call(llm_response=resp)
        assert len(note.not_included) == 1


# ---------------------------------------------------------------------------
# Grounding guard
# ---------------------------------------------------------------------------

class TestGroundingGuard:
    def test_grounded_assumption_kept(self):
        # "admissionType" is in the subgraph → should survive the guard
        resp = _llm_response(
            scope_assumptions=["All admissionType values included."]
        )
        (note, _), _ = _call(llm_response=resp)
        assert any("admissionType" in a for a in note.scope_assumptions)

    def test_ungrounded_assumption_removed(self):
        resp = _llm_response(
            scope_assumptions=["All xyz_phantom_property values included."]
        )
        (note, _), _ = _call(llm_response=resp)
        assert note.scope_assumptions == []

    def test_ungrounded_adds_narrator_ungrounded_caveat(self):
        resp = _llm_response(
            scope_assumptions=["All xyz_phantom_property values included."]
        )
        (_, caveats), _ = _call(llm_response=resp)
        assert any(c.type == "NARRATOR_UNGROUNDED" for c in caveats)

    def test_narrator_ungrounded_caveat_contains_reason(self):
        bad_assumption = "All xyz_phantom_property values included."
        resp = _llm_response(scope_assumptions=[bad_assumption])
        (_, caveats), _ = _call(llm_response=resp)
        ungrounded = [c for c in caveats if c.type == "NARRATOR_UNGROUNDED"]
        assert ungrounded[0].reason == bad_assumption

    def test_mixed_assumptions_partial_removal(self):
        resp = _llm_response(
            scope_assumptions=[
                "All admissionType values included.",       # grounded
                "All xyz_phantom_property values included.", # ungrounded
            ]
        )
        (note, caveats), _ = _call(llm_response=resp)
        assert len(note.scope_assumptions) == 1
        assert any(c.type == "NARRATOR_UNGROUNDED" for c in caveats)

    def test_empty_subgraph_all_assumptions_stripped(self):
        empty_subgraph = OntologySubgraph(classes={}, properties={}, adjacency_notes=[])
        resp = _llm_response(scope_assumptions=["All admissionType values included."])
        (note, caveats), _ = _call(subgraph=empty_subgraph, llm_response=resp)
        assert note.scope_assumptions == []
        assert any(c.type == "NARRATOR_UNGROUNDED" for c in caveats)

    def test_no_assumptions_no_guard_caveats(self):
        resp = _llm_response(scope_assumptions=[])
        (note, caveats), _ = _call(llm_response=resp)
        assert note.scope_assumptions == []
        assert not any(c.type == "NARRATOR_UNGROUNDED" for c in caveats)


# ---------------------------------------------------------------------------
# LLM failure
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Confidence reason fallback (Phase 13 follow-up)
# ---------------------------------------------------------------------------

class TestConfidenceReasonFallback:
    def test_medium_with_empty_reason_gets_fallback(self):
        resp = _llm_response(confidence="MEDIUM", confidence_reason="")
        (note, _), _ = _call(llm_response=resp)
        assert note.interpretation_confidence == "MEDIUM"
        assert "MEDIUM" in note.interpretation_confidence_reason
        assert note.interpretation_confidence_reason != ""

    def test_low_with_empty_reason_gets_fallback(self):
        resp = _llm_response(confidence="LOW", confidence_reason="")
        (note, _), _ = _call(llm_response=resp)
        assert note.interpretation_confidence == "LOW"
        assert "LOW" in note.interpretation_confidence_reason
        assert note.interpretation_confidence_reason != ""

    def test_high_with_empty_reason_stays_empty(self):
        resp = _llm_response(confidence="HIGH", confidence_reason="")
        (note, _), _ = _call(llm_response=resp)
        assert note.interpretation_confidence == "HIGH"
        assert note.interpretation_confidence_reason == ""

    def test_medium_with_nonempty_reason_preserved(self):
        resp = _llm_response(confidence="MEDIUM", confidence_reason="Scope is implicit.")
        (note, _), _ = _call(llm_response=resp)
        assert note.interpretation_confidence_reason == "Scope is implicit."

    def test_fallback_text_mentions_cannot_identify(self):
        resp = _llm_response(confidence="MEDIUM", confidence_reason="")
        (note, _), _ = _call(llm_response=resp)
        assert "could not identify" in note.interpretation_confidence_reason


class TestLlmFailure:
    def test_does_not_raise_on_api_error(self):
        settings = _make_settings()
        with patch("openai.OpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.chat.completions.create.side_effect = RuntimeError("API down")
            note, caveats = narrate(
                _QUESTION, _QUERY, _ANSWER, _make_subgraph(), [], [], settings
            )
        assert isinstance(note, InterpretationNote)

    def test_narration_failed_caveat_added(self):
        settings = _make_settings()
        with patch("openai.OpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.chat.completions.create.side_effect = RuntimeError("API down")
            _, caveats = narrate(
                _QUESTION, _QUERY, _ANSWER, _make_subgraph(), [], [], settings
            )
        assert any(c.type == "NARRATION_FAILED" for c in caveats)

    def test_narration_failed_reason_contains_error(self):
        settings = _make_settings()
        with patch("openai.OpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.chat.completions.create.side_effect = RuntimeError("timeout")
            _, caveats = narrate(
                _QUESTION, _QUERY, _ANSWER, _make_subgraph(), [], [], settings
            )
        failed = next(c for c in caveats if c.type == "NARRATION_FAILED")
        assert "timeout" in failed.reason

    def test_narrated_false_on_failure(self):
        settings = _make_settings()
        with patch("openai.OpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.chat.completions.create.side_effect = ValueError("bad json")
            note, _ = narrate(
                _QUESTION, _QUERY, _ANSWER, _make_subgraph(), [], [], settings
            )
        assert note.narrated is False

    def test_existing_caveats_preserved_on_failure(self):
        existing = [ProofCaveat(type="SAMPLED", count=200, table=None, property_uri=None, reason=None)]
        settings = _make_settings()
        with patch("openai.OpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.chat.completions.create.side_effect = RuntimeError("down")
            _, caveats = narrate(
                _QUESTION, _QUERY, _ANSWER, _make_subgraph(), [], existing, settings
            )
        assert any(c.type == "SAMPLED" for c in caveats)

    def test_does_not_raise_on_bad_json(self):
        settings = _make_settings()
        bad_resp = MagicMock()
        bad_resp.choices = [MagicMock()]
        bad_resp.choices[0].message.content = "not valid json {{{"
        with patch("openai.OpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = bad_resp
            note, caveats = narrate(
                _QUESTION, _QUERY, _ANSWER, _make_subgraph(), [], [], settings
            )
        assert isinstance(note, InterpretationNote)
        assert any(c.type == "NARRATION_FAILED" for c in caveats)
