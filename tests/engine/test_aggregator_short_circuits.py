# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for aggregator short-circuit logic.

Covers:
- HistoryMatcher short-circuit (pre-existing behaviour)
- AnnotationMatcher short-circuit: fires for score >= ANNOTATION_EXACT_CONFIRM_THRESHOLD
  (default 0.95), covering both verbatim (1.0) and case-folded (0.95) matches.
  Fuzzy scores (≤0.75) must NOT fire the short-circuit.
- NameMatcher exact-match short-circuit (score >= NAME_EXACT_CONFIRM_THRESHOLD)
- Weighted-sum path is not taken when a short-circuit fires
"""
from __future__ import annotations

import pytest

from semantic_mapper.config.settings import Settings
from semantic_mapper.engine.aggregator import aggregate
from semantic_mapper.schema.mapping_output import MappingEvidence


URI_A = "http://test.org/ontology#hasOutputeventNewbottle"
URI_B = "http://test.org/ontology#hasSomeOtherProp"


@pytest.fixture()
def settings():
    return Settings(DATABASE_URL="sqlite:///:memory:", OPENAI_API_KEY="x")


def _ev(strategy: str, score: float, uri: str = URI_A, rationale: str = "test") -> MappingEvidence:
    return MappingEvidence(strategy=strategy, score=score, mapped_uri=uri, rationale=rationale)


# ---------------------------------------------------------------------------
# AnnotationMatcher exact-match short-circuit
# ---------------------------------------------------------------------------

class TestAnnotationMatcherShortCircuit:
    def test_score_1_returns_uri_at_1_0(self, settings):
        """AnnotationMatcher score == 1.0 must short-circuit and return the URI at 1.0."""
        trail = [
            _ev("AnnotationMatcher", 1.0, URI_A, "verbatim skos:altLabel hit"),
            _ev("NameMatcher",       0.55, URI_B),
            _ev("TypeMatcher",       0.80, URI_A),
        ]
        uri, score, rationale = aggregate(trail, settings)
        assert uri == URI_A
        assert score == pytest.approx(1.0)

    def test_rationale_contains_annotation_exact_tag(self, settings):
        trail = [_ev("AnnotationMatcher", 1.0, URI_A, "verbatim label hit")]
        _, _, rationale = aggregate(trail, settings)
        assert "[annotation-exact]" in rationale

    def test_rationale_preserves_original_evidence_rationale(self, settings):
        trail = [_ev("AnnotationMatcher", 1.0, URI_A, "verbatim skos:altLabel hit for newbottle")]
        _, _, rationale = aggregate(trail, settings)
        assert "verbatim skos:altLabel hit for newbottle" in rationale

    def test_normalised_095_fires_short_circuit(self, settings):
        """AnnotationMatcher score == 0.95 (case-folded match) must also short-circuit."""
        trail = [
            _ev("AnnotationMatcher", 0.95, URI_A, "case-folded annotation match"),
            _ev("NameMatcher",       0.50, URI_B),
            _ev("TypeMatcher",       0.80, URI_A),
        ]
        uri, score, rationale = aggregate(trail, settings)
        assert uri == URI_A
        assert score == pytest.approx(0.95)
        assert "[annotation-exact]" in rationale

    def test_normalised_095_returns_evidence_score_not_hardcoded_1(self, settings):
        """The returned score is the evidence score (0.95), not a hardcoded 1.0."""
        trail = [_ev("AnnotationMatcher", 0.95, URI_A, "case-folded hit")]
        _, score, _ = aggregate(trail, settings)
        assert score == pytest.approx(0.95)

    def test_fuzzy_score_below_threshold_does_not_short_circuit(self, settings):
        """AnnotationMatcher score 0.75 (fuzzy cap) is below threshold → weighted sum."""
        trail = [
            _ev("AnnotationMatcher", 0.75, URI_A),
            _ev("NameMatcher",       0.50, URI_B),
        ]
        uri, score, rationale = aggregate(trail, settings)
        assert score < 0.95
        assert "[annotation-exact]" not in rationale

    def test_score_090_below_threshold_does_not_short_circuit(self, settings):
        """AnnotationMatcher score 0.90 is between fuzzy cap and normalised score — no fire."""
        trail = [
            _ev("AnnotationMatcher", 0.90, URI_A),
            _ev("NameMatcher",       0.50, URI_B),
        ]
        uri, score, rationale = aggregate(trail, settings)
        assert score < 0.95
        assert "[annotation-exact]" not in rationale

    def test_beats_higher_scoring_competing_evidence(self, settings):
        """Even when other strategies gave URI_B a higher individual score,
        the AnnotationMatcher 1.0 hit on URI_A must still win."""
        trail = [
            _ev("AnnotationMatcher", 1.0,  URI_A),
            _ev("NameMatcher",       0.97,  URI_B),
            _ev("SemanticMatcher",   0.95,  URI_B),
        ]
        uri, score, _ = aggregate(trail, settings)
        assert uri == URI_A
        assert score == pytest.approx(1.0)

    def test_fires_before_name_matcher_short_circuit(self, settings):
        """AnnotationMatcher short-circuit runs before NameMatcher short-circuit.
        When both could fire, AnnotationMatcher wins because it is checked first."""
        trail = [
            _ev("AnnotationMatcher", 1.0,  URI_A, "annotation hit"),
            _ev("NameMatcher",       0.97,  URI_B, "name exact hit"),
        ]
        uri, _, rationale = aggregate(trail, settings)
        assert uri == URI_A
        assert "[annotation-exact]" in rationale

    def test_no_uri_annotation_evidence_skipped(self, settings):
        """AnnotationMatcher evidence with mapped_uri=None must not short-circuit."""
        trail = [
            MappingEvidence(
                strategy="AnnotationMatcher", score=1.0, mapped_uri=None, rationale="no uri"
            ),
            _ev("NameMatcher", 0.60, URI_B),
        ]
        result = aggregate(trail, settings)
        # weighted-sum path runs; URI_B wins
        assert result is not None
        uri, score, rationale = result
        assert uri == URI_B
        assert "[annotation-exact]" not in rationale

    def test_newbottle_concrete_example_verbatim(self, settings):
        """Regression: newbottle column with verbatim AnnotationMatcher 1.0 must not stay PENDING.

        Before the short-circuit fix, weighted-sum floor denominator (1.25) dragged the
        final score to ~0.50, below the 0.70 confirm threshold.
        """
        trail = [
            _ev("AnnotationMatcher", 1.0,  URI_A, "verbatim altLabel match: newbottle"),
            _ev("NameMatcher",       0.42,  URI_A),
            _ev("TypeMatcher",       0.80,  URI_A),
            _ev("SemanticMatcher",   0.31,  URI_A),
        ]
        uri, score, rationale = aggregate(trail, settings)
        assert uri == URI_A
        assert score == pytest.approx(1.0)
        assert "[annotation-exact]" in rationale

    def test_originalrateuom_concrete_example_normalised(self, settings):
        """Regression: originalrateuom column with normalised AnnotationMatcher 0.95.

        Without the short-circuit, the floor denominator drags 0.95 well below 0.70.
        With the short-circuit (threshold=0.95), it short-circuits and returns 0.95,
        which is above the AUTO_CONFIRM_THRESHOLD (0.70) → CONFIRMED status.
        """
        trail = [
            _ev("AnnotationMatcher", 0.95, URI_A,
                "Column name 'originalrateuom' matched case-insensitively to "
                "'originalRateUom' in annotation: 'originalRateUom'"),
            _ev("NameMatcher",       0.42, URI_A),
            _ev("TypeMatcher",       0.80, URI_A),
            _ev("SemanticMatcher",   0.31, URI_A),
        ]
        uri, score, rationale = aggregate(trail, settings)
        assert uri == URI_A
        assert score == pytest.approx(0.95)
        assert "[annotation-exact]" in rationale


# ---------------------------------------------------------------------------
# HistoryMatcher short-circuit — regression guard
# ---------------------------------------------------------------------------

class TestHistoryMatcherShortCircuit:
    def test_history_matcher_wins_at_1_0(self, settings):
        trail = [
            _ev("HistoryMatcher",    1.0,  URI_A),
            _ev("AnnotationMatcher", 1.0,  URI_B),
            _ev("NameMatcher",       0.50, URI_B),
        ]
        uri, score, rationale = aggregate(trail, settings)
        assert uri == URI_A
        assert score == pytest.approx(1.0)
        assert "[human-override]" in rationale

    def test_history_matcher_fires_before_annotation_short_circuit(self, settings):
        """HistoryMatcher is checked first; AnnotationMatcher must not shadow it."""
        trail = [
            _ev("HistoryMatcher",    1.0, URI_A),
            _ev("AnnotationMatcher", 1.0, URI_B),
        ]
        uri, _, rationale = aggregate(trail, settings)
        assert uri == URI_A
        assert "[human-override]" in rationale


# ---------------------------------------------------------------------------
# NameMatcher short-circuit — regression guard
# ---------------------------------------------------------------------------

class TestNameMatcherShortCircuit:
    def test_name_exact_fires_when_no_annotation_match(self, settings):
        trail = [
            _ev("NameMatcher",     0.97, URI_A),
            _ev("TypeMatcher",     0.80, URI_A),
            _ev("SemanticMatcher", 0.40, URI_B),
        ]
        uri, score, rationale = aggregate(trail, settings)
        assert uri == URI_A
        assert "[name-exact]" in rationale
