# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for Phase 5 — Mapping evidence retriever.

get_mapping_report is mocked throughout; the run_store is tested separately.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from semantic_mapper.prove.decision_lookup import (
    MappingDecisionRef,
    ProofEvidenceMissing,
    get_mapping_decisions,
)
from semantic_mapper.schema.mapping_output import (
    ClassCandidate,
    MappingEvidence,
    MappingStatus,
    PropertyMapping,
    SemanticMappingReport,
    TableMapping,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CLASS_URI = "http://mesh.local/ontology#Admission"
_CLASS_URI_2 = "http://mesh.local/ontology#Patient"
_PROP_URI = "http://mesh.local/ontology#admittime"
_PROP_URI_2 = "http://mesh.local/ontology#dischargeTime"
_ADJ_PROP_URI = "http://mesh.local/ontology#admissionType"

_EVIDENCE_NAME = MappingEvidence(strategy="NameMatcher", score=0.85, rationale="exact name")
_EVIDENCE_SEMANTIC = MappingEvidence(strategy="SemanticMatcher", score=0.72, rationale="embedding")
_EVIDENCE_HISTORY = MappingEvidence(strategy="HistoryMatcher", score=1.0, rationale="human correction")


def _make_report(
    *,
    include_adjacent: bool = False,
    adjacent_confirmed: bool = True,
    prop_evidence: list[MappingEvidence] | None = None,
) -> SemanticMappingReport:
    if prop_evidence is None:
        prop_evidence = [_EVIDENCE_NAME, _EVIDENCE_SEMANTIC]

    col_mappings = [
        PropertyMapping(
            source_column="admittime",
            source_data_type="DATETIME",
            is_nullable=False,
            mapped_property_uri=_PROP_URI,
            mapped_property_label="admittime",
            status=MappingStatus.CONFIRMED,
            confidence_score=0.85,
            rationale="name match",
            evidence_trail=prop_evidence,
        ),
    ]

    if include_adjacent:
        col_mappings.append(
            PropertyMapping(
                source_column="admission_type",
                source_data_type="VARCHAR",
                is_nullable=False,
                mapped_property_uri=_ADJ_PROP_URI,
                mapped_property_label="admissionType",
                status=MappingStatus.CONFIRMED if adjacent_confirmed else MappingStatus.PENDING_REVIEW,
                confidence_score=0.80,
                rationale="name match",
                evidence_trail=[_EVIDENCE_NAME],
            )
        )

    table = TableMapping(
        source_table="admissions",
        class_candidate_set=[
            ClassCandidate(
                uri=_CLASS_URI,
                label="Admission",
                rationale="best match",
                confidence_score=0.92,
                is_primary_discriminator=True,
            )
        ],
        column_mappings=col_mappings,
    )
    return SemanticMappingReport(
        ontology_uri="http://mesh.local/ontology",
        database_name="bronze_db",
        settings_used={},
        tables=[table],
        total_pending_review=0,
    )


def _settings():
    return MagicMock()


def _call(report, class_uris=None, property_uris=None, include_adjacent=True):
    with patch(
        "semantic_mapper.prove.decision_lookup.get_mapping_report",
        return_value=report,
    ):
        return get_mapping_decisions(
            run_id="run_001",
            class_uris=class_uris or set(),
            property_uris=property_uris or set(),
            settings=_settings(),
            include_adjacent=include_adjacent,
        )


# ---------------------------------------------------------------------------
# ProofEvidenceMissing
# ---------------------------------------------------------------------------

class TestProofEvidenceMissing:
    def test_raised_for_unknown_run_id(self):
        with patch(
            "semantic_mapper.prove.decision_lookup.get_mapping_report",
            return_value=None,
        ):
            with pytest.raises(ProofEvidenceMissing) as exc_info:
                get_mapping_decisions("bad_run", set(), set(), _settings())
            assert exc_info.value.run_id == "bad_run"

    def test_exception_has_run_id_attr(self):
        with patch(
            "semantic_mapper.prove.decision_lookup.get_mapping_report",
            return_value=None,
        ):
            with pytest.raises(ProofEvidenceMissing) as exc_info:
                get_mapping_decisions("run_xyz", set(), set(), _settings())
            assert "run_xyz" in str(exc_info.value)

    def test_not_raised_for_valid_run_id(self):
        report = _make_report()
        result = _call(report)  # should not raise
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Class URI lookup
# ---------------------------------------------------------------------------

class TestClassLookup:
    def test_class_ref_returned(self):
        report = _make_report()
        refs = _call(report, class_uris={_CLASS_URI}, include_adjacent=False)
        uris = [r.uri for r in refs]
        assert _CLASS_URI in uris

    def test_kind_is_class(self):
        report = _make_report()
        refs = _call(report, class_uris={_CLASS_URI}, include_adjacent=False)
        ref = next(r for r in refs if r.uri == _CLASS_URI)
        assert ref.kind == "Class"

    def test_source_is_table_name(self):
        report = _make_report()
        refs = _call(report, class_uris={_CLASS_URI}, include_adjacent=False)
        ref = next(r for r in refs if r.uri == _CLASS_URI)
        assert ref.source == "admissions"

    def test_score_from_candidate(self):
        report = _make_report()
        refs = _call(report, class_uris={_CLASS_URI}, include_adjacent=False)
        ref = next(r for r in refs if r.uri == _CLASS_URI)
        assert ref.score == pytest.approx(0.92)

    def test_unknown_class_uri_silently_skipped(self):
        report = _make_report()
        refs = _call(report, class_uris={"http://mesh.local/ontology#Unknown"}, include_adjacent=False)
        assert refs == []

    def test_class_evidence_trail_empty(self):
        report = _make_report()
        refs = _call(report, class_uris={_CLASS_URI}, include_adjacent=False)
        ref = next(r for r in refs if r.uri == _CLASS_URI)
        assert ref.evidence_trail == []

    def test_class_confirmation_auto(self):
        report = _make_report()
        refs = _call(report, class_uris={_CLASS_URI}, include_adjacent=False)
        ref = next(r for r in refs if r.uri == _CLASS_URI)
        assert ref.confirmation == "AUTO"


# ---------------------------------------------------------------------------
# Property URI lookup
# ---------------------------------------------------------------------------

class TestPropertyLookup:
    def test_property_ref_returned(self):
        report = _make_report()
        refs = _call(report, property_uris={_PROP_URI}, include_adjacent=False)
        uris = [r.uri for r in refs]
        assert _PROP_URI in uris

    def test_kind_is_datatype_property(self):
        report = _make_report()
        refs = _call(report, property_uris={_PROP_URI}, include_adjacent=False)
        ref = next(r for r in refs if r.uri == _PROP_URI)
        assert ref.kind == "DatatypeProperty"

    def test_source_format_table_dot_column(self):
        report = _make_report()
        refs = _call(report, property_uris={_PROP_URI}, include_adjacent=False)
        ref = next(r for r in refs if r.uri == _PROP_URI)
        assert ref.source == "admissions.admittime"

    def test_evidence_trail_preserved(self):
        report = _make_report()
        refs = _call(report, property_uris={_PROP_URI}, include_adjacent=False)
        ref = next(r for r in refs if r.uri == _PROP_URI)
        strategies = {e.strategy for e in ref.evidence_trail}
        assert "NameMatcher" in strategies
        assert "SemanticMatcher" in strategies

    def test_winning_strategy_highest_score(self):
        report = _make_report()
        refs = _call(report, property_uris={_PROP_URI}, include_adjacent=False)
        ref = next(r for r in refs if r.uri == _PROP_URI)
        assert ref.winning_strategy == "NameMatcher"
        assert ref.score == pytest.approx(0.85)

    def test_unknown_property_uri_silently_skipped(self):
        report = _make_report()
        refs = _call(report, property_uris={"http://mesh.local/ontology#Ghost"}, include_adjacent=False)
        assert refs == []

    def test_confirmed_at_is_none(self):
        report = _make_report()
        refs = _call(report, property_uris={_PROP_URI}, include_adjacent=False)
        ref = next(r for r in refs if r.uri == _PROP_URI)
        assert ref.confirmed_at is None


# ---------------------------------------------------------------------------
# HUMAN vs AUTO confirmation
# ---------------------------------------------------------------------------

class TestConfirmation:
    def test_history_matcher_gives_human(self):
        report = _make_report(prop_evidence=[_EVIDENCE_HISTORY])
        refs = _call(report, property_uris={_PROP_URI}, include_adjacent=False)
        ref = next(r for r in refs if r.uri == _PROP_URI)
        assert ref.confirmation == "HUMAN"

    def test_non_history_gives_auto(self):
        report = _make_report(prop_evidence=[_EVIDENCE_NAME, _EVIDENCE_SEMANTIC])
        refs = _call(report, property_uris={_PROP_URI}, include_adjacent=False)
        ref = next(r for r in refs if r.uri == _PROP_URI)
        assert ref.confirmation == "AUTO"

    def test_mixed_trail_with_history_gives_human(self):
        report = _make_report(prop_evidence=[_EVIDENCE_NAME, _EVIDENCE_HISTORY])
        refs = _call(report, property_uris={_PROP_URI}, include_adjacent=False)
        ref = next(r for r in refs if r.uri == _PROP_URI)
        assert ref.confirmation == "HUMAN"

    def test_empty_evidence_gives_auto(self):
        report = _make_report(prop_evidence=[])
        refs = _call(report, property_uris={_PROP_URI}, include_adjacent=False)
        ref = next(r for r in refs if r.uri == _PROP_URI)
        assert ref.confirmation == "AUTO"

    def test_history_matcher_winning_strategy(self):
        report = _make_report(prop_evidence=[_EVIDENCE_NAME, _EVIDENCE_HISTORY])
        refs = _call(report, property_uris={_PROP_URI}, include_adjacent=False)
        ref = next(r for r in refs if r.uri == _PROP_URI)
        assert ref.winning_strategy == "HistoryMatcher"
        assert ref.score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Adjacent properties
# ---------------------------------------------------------------------------

class TestAdjacentProperties:
    def test_adjacent_included_when_flag_true(self):
        report = _make_report(include_adjacent=True)
        refs = _call(report, class_uris={_CLASS_URI}, property_uris=set(), include_adjacent=True)
        uris = [r.uri for r in refs]
        assert _ADJ_PROP_URI in uris

    def test_adjacent_excluded_when_flag_false(self):
        report = _make_report(include_adjacent=True)
        refs = _call(report, class_uris={_CLASS_URI}, property_uris=set(), include_adjacent=False)
        uris = [r.uri for r in refs]
        assert _ADJ_PROP_URI not in uris

    def test_adjacent_excluded_if_in_property_uris(self):
        report = _make_report(include_adjacent=True)
        refs = _call(
            report,
            class_uris={_CLASS_URI},
            property_uris={_ADJ_PROP_URI},
            include_adjacent=True,
        )
        adj_refs = [r for r in refs if r.uri == _ADJ_PROP_URI]
        assert len(adj_refs) == 1  # appears once, not twice

    def test_non_confirmed_adjacent_skipped(self):
        report = _make_report(include_adjacent=True, adjacent_confirmed=False)
        refs = _call(report, class_uris={_CLASS_URI}, include_adjacent=True)
        uris = [r.uri for r in refs]
        assert _ADJ_PROP_URI not in uris

    def test_adjacent_queried_prop_not_duplicated(self):
        report = _make_report()
        # _PROP_URI is both queried and on the same table
        refs = _call(
            report,
            class_uris={_CLASS_URI},
            property_uris={_PROP_URI},
            include_adjacent=True,
        )
        prop_refs = [r for r in refs if r.uri == _PROP_URI]
        assert len(prop_refs) == 1


# ---------------------------------------------------------------------------
# Deduplication and edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_uris_returns_empty(self):
        report = _make_report()
        refs = _call(report, class_uris=set(), property_uris=set(), include_adjacent=False)
        assert refs == []

    def test_empty_uris_adjacent_still_empty_without_class(self):
        report = _make_report(include_adjacent=True)
        refs = _call(report, class_uris=set(), property_uris=set(), include_adjacent=True)
        # No matched tables → no adjacent properties
        assert refs == []

    def test_no_duplicates_across_class_and_property(self):
        report = _make_report()
        refs = _call(report, class_uris={_CLASS_URI}, property_uris={_PROP_URI}, include_adjacent=False)
        uris = [r.uri for r in refs]
        assert len(uris) == len(set(uris))

    def test_returns_list_of_mapping_decision_refs(self):
        report = _make_report()
        refs = _call(report, class_uris={_CLASS_URI})
        assert all(isinstance(r, MappingDecisionRef) for r in refs)
