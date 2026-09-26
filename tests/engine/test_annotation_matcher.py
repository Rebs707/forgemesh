# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for AnnotationMatcher scoring tiers.

Tier 2a (verbatim, score=1.0): col_name appears byte-for-byte in the annotation.
Tier 2b (case-folded, score=0.95): col_name.lower() matches case-insensitively.
Tier 3 (fuzzy, score≤0.75): token-set ratio is above _FUZZY_MIN.

Key regression: column "originalrateuom" must NOT score 1.0 against the annotation
"originalRateUom" — that is a case-folded match (0.95) with a rationale that names
the matched token, not a verbatim (1.0) match.
"""
from __future__ import annotations

import pytest
import rdflib
from rdflib import OWL, RDF, RDFS, Namespace

from semantic_mapper.config.settings import Settings
from semantic_mapper.db.models import ColumnProfile, TableProfile
from semantic_mapper.engine.strategies.annotation_matcher import AnnotationMatcher

_SKOS_ALT = rdflib.URIRef("http://www.w3.org/2004/02/skos/core#altLabel")
_FM = Namespace("http://forgemesh.ai/ontology#")

PROP_URI = "http://test.org/ontology#hasOriginalrateuom"


@pytest.fixture()
def settings():
    return Settings(DATABASE_URL="sqlite:///:memory:", OPENAI_API_KEY="x")


def _col(name: str) -> ColumnProfile:
    return ColumnProfile(
        name=name, inferred_type="VARCHAR",
        is_nullable=False, is_primary_key=False,
        null_rate=0.0, distinct_count=5, top_values=[],
    )


def _table() -> TableProfile:
    return TableProfile(table_name="raw_icu", row_count=100, columns={}, foreign_keys=[])


def _matcher_with_annotation(annotation: str, predicate=None, settings=None) -> AnnotationMatcher:
    """Build a graph with a single property + one annotation and return the matcher."""
    if predicate is None:
        predicate = _SKOS_ALT
    g = rdflib.Graph()
    prop = rdflib.URIRef(PROP_URI)
    g.add((prop, RDF.type, OWL.DatatypeProperty))
    g.add((prop, predicate, rdflib.Literal(annotation)))
    if settings is None:
        settings = Settings(DATABASE_URL="sqlite:///:memory:", OPENAI_API_KEY="x")
    return AnnotationMatcher(g, settings)


# ---------------------------------------------------------------------------
# Tier 1: explicit source-column mapping patterns
# ---------------------------------------------------------------------------

class TestExplicitPattern:
    def test_source_column_pattern_scores_1_0(self, settings):
        matcher = _matcher_with_annotation("source column: originalrateuom", settings=settings)
        evs = matcher.evaluate_column(_col("originalrateuom"), _table())
        assert len(evs) == 1
        assert evs[0].score == pytest.approx(1.0)

    def test_explicit_rationale_mentions_snippet(self, settings):
        matcher = _matcher_with_annotation("source column: originalrateuom", settings=settings)
        evs = matcher.evaluate_column(_col("originalrateuom"), _table())
        assert "Explicit source mapping" in evs[0].rationale

    def test_maps_to_pattern_scores_1_0(self, settings):
        matcher = _matcher_with_annotation("Maps to originalrateuom in the source", settings=settings)
        evs = matcher.evaluate_column(_col("originalrateuom"), _table())
        assert len(evs) == 1
        assert evs[0].score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Tier 2a: true verbatim (case-sensitive) → score 1.0
# ---------------------------------------------------------------------------

class TestTrueVerbatimMatch:
    def test_exact_lowercase_annotation_scores_1_0(self, settings):
        """Annotation contains col_name exactly (same case) → verbatim tier → 1.0."""
        matcher = _matcher_with_annotation("originalrateuom", settings=settings)
        evs = matcher.evaluate_column(_col("originalrateuom"), _table())
        assert len(evs) == 1
        assert evs[0].score == pytest.approx(1.0)

    def test_verbatim_rationale_says_verbatim(self, settings):
        matcher = _matcher_with_annotation("originalrateuom", settings=settings)
        evs = matcher.evaluate_column(_col("originalrateuom"), _table())
        assert "verbatim" in evs[0].rationale.lower()

    def test_verbatim_rationale_includes_column_name(self, settings):
        matcher = _matcher_with_annotation("originalrateuom", settings=settings)
        evs = matcher.evaluate_column(_col("originalrateuom"), _table())
        assert "originalrateuom" in evs[0].rationale

    def test_verbatim_annotation_in_sentence_still_scores_1_0(self, settings):
        """Word-boundary match: annotation is a sentence containing the column name."""
        matcher = _matcher_with_annotation(
            "Rate uom for originalrateuom field", settings=settings
        )
        evs = matcher.evaluate_column(_col("originalrateuom"), _table())
        assert evs[0].score == pytest.approx(1.0)

    def test_rdfs_label_only_scores_fuzzy(self, settings):
        """rdfs:label is prose — word-boundary matching is NOT applied, only fuzzy (max 0.75).
        Use skos:altLabel for structured column references that warrant a 1.0 hit."""
        matcher = _matcher_with_annotation("originalrateuom", predicate=RDFS.label, settings=settings)
        evs = matcher.evaluate_column(_col("originalrateuom"), _table())
        assert len(evs) == 1
        assert evs[0].score <= 0.75


# ---------------------------------------------------------------------------
# Tier 2b: case-folded match → score 0.95 (ANNOTATION_NORMALISED_MATCH_SCORE)
# ---------------------------------------------------------------------------

class TestCaseFoldedMatch:
    def test_camelcase_annotation_scores_095(self, settings):
        """Annotation 'originalRateUom' vs column 'originalrateuom' → case-folded, 0.95."""
        matcher = _matcher_with_annotation("originalRateUom", settings=settings)
        evs = matcher.evaluate_column(_col("originalrateuom"), _table())
        assert len(evs) == 1
        assert evs[0].score == pytest.approx(0.95)

    def test_casefold_rationale_does_not_say_verbatim(self, settings):
        """Rationale must NOT claim the match was verbatim when it was case-folded."""
        matcher = _matcher_with_annotation("originalRateUom", settings=settings)
        evs = matcher.evaluate_column(_col("originalrateuom"), _table())
        assert "verbatim" not in evs[0].rationale.lower()

    def test_casefold_rationale_names_matched_token(self, settings):
        """Rationale must report the actual annotation token that was matched."""
        matcher = _matcher_with_annotation("originalRateUom", settings=settings)
        evs = matcher.evaluate_column(_col("originalrateuom"), _table())
        assert "originalRateUom" in evs[0].rationale

    def test_casefold_rationale_names_column(self, settings):
        matcher = _matcher_with_annotation("originalRateUom", settings=settings)
        evs = matcher.evaluate_column(_col("originalrateuom"), _table())
        assert "originalrateuom" in evs[0].rationale

    def test_casefold_rationale_says_case_insensitively(self, settings):
        matcher = _matcher_with_annotation("originalRateUom", settings=settings)
        evs = matcher.evaluate_column(_col("originalrateuom"), _table())
        assert "case-insensitively" in evs[0].rationale

    def test_uppercase_annotation_casefolds(self, settings):
        """Annotation 'ORIGINALRATEUOM' vs column 'originalrateuom' → case-folded, 0.95."""
        matcher = _matcher_with_annotation("ORIGINALRATEUOM", settings=settings)
        evs = matcher.evaluate_column(_col("originalrateuom"), _table())
        assert evs[0].score == pytest.approx(0.95)

    def test_concrete_bug_regression(self, settings):
        """Regression: hasOriginalrateuom annotated with 'originalRateUom' (camelCase).

        Before the fix: score was reported as 1.0 with 'found verbatim' — factually wrong.
        After the fix: score is 0.95, rationale accurately names 'originalRateUom'.
        """
        matcher = _matcher_with_annotation("originalRateUom", settings=settings)
        evs = matcher.evaluate_column(_col("originalrateuom"), _table())
        assert evs[0].score == pytest.approx(0.95)
        assert "originalRateUom" in evs[0].rationale
        assert "verbatim" not in evs[0].rationale.lower()

    def test_normalised_score_respects_setting(self):
        """ANNOTATION_NORMALISED_MATCH_SCORE setting is used for the case-folded tier."""
        custom_settings = Settings(
            DATABASE_URL="sqlite:///:memory:",
            OPENAI_API_KEY="x",
            ANNOTATION_NORMALISED_MATCH_SCORE=0.90,
        )
        matcher = _matcher_with_annotation("originalRateUom", settings=custom_settings)
        evs = matcher.evaluate_column(_col("originalrateuom"), _table())
        assert evs[0].score == pytest.approx(0.90)

    def test_verbatim_takes_priority_over_casefold(self, settings):
        """When the annotation contains col_name verbatim, Tier 2a fires (score=1.0),
        never reaching Tier 2b."""
        # annotation 'originalrateuom' IS verbatim (same case as col_name)
        matcher = _matcher_with_annotation("originalrateuom", settings=settings)
        evs = matcher.evaluate_column(_col("originalrateuom"), _table())
        assert evs[0].score == pytest.approx(1.0)
        assert "verbatim" in evs[0].rationale.lower()


# ---------------------------------------------------------------------------
# Tier 3: fuzzy match → ≤ 0.75
# ---------------------------------------------------------------------------

class TestFuzzyMatch:
    def test_fuzzy_score_capped_at_075(self, settings):
        """A partial match that doesn't hit Tier 1 or 2 must be capped at 0.75."""
        # 'original rate' is close to 'originalrateuom' but not a word-boundary match
        matcher = _matcher_with_annotation("original rate measurement", settings=settings)
        evs = matcher.evaluate_column(_col("originalrateuom"), _table())
        # May or may not match depending on fuzz ratio; if it does, cap must hold
        for ev in evs:
            assert ev.score <= 0.75

    def test_no_match_omitted(self, settings):
        """Annotations completely unrelated to the column produce no evidence."""
        matcher = _matcher_with_annotation("completely unrelated annotation", settings=settings)
        evs = matcher.evaluate_column(_col("originalrateuom"), _table())
        assert evs == []


# ---------------------------------------------------------------------------
# Scoring tier ordering (Tier 1 > Tier 2a > Tier 2b > Tier 3)
# ---------------------------------------------------------------------------

class TestScoringTierOrdering:
    def test_explicit_outranks_verbatim(self, settings):
        """Tier 1 (explicit pattern, 1.0) fires before Tier 2a even when 2a also matches."""
        g = rdflib.Graph()
        prop = rdflib.URIRef(PROP_URI)
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        # Annotation satisfies both Tier 1 (explicit pattern) and Tier 2a (verbatim)
        g.add((prop, _SKOS_ALT, rdflib.Literal("source column: mycolumn (mycolumn value)")))
        matcher = AnnotationMatcher(g, settings)
        evs = matcher.evaluate_column(_col("mycolumn"), _table())
        assert evs[0].score == pytest.approx(1.0)
        assert "Explicit source mapping" in evs[0].rationale

    def test_verbatim_outranks_casefold(self, settings):
        """When annotation is exactly col_name (same case), verbatim (1.0) fires,
        not case-fold (0.95)."""
        # col_name = "newbottle", annotation = "newbottle" (exact)
        matcher = _matcher_with_annotation("newbottle", settings=settings)
        evs = matcher.evaluate_column(_col("newbottle"), _table())
        assert evs[0].score == pytest.approx(1.0)
        assert "verbatim" in evs[0].rationale.lower()


# ---------------------------------------------------------------------------
# fm:sourceForeignKey extraction — ObjectProperties get annotation credit
# via the column name embedded in "table.col -> ClassName" values.
# ---------------------------------------------------------------------------

class TestFkAnnotationExtraction:
    def _object_prop_graph(self, fk_value: str) -> rdflib.Graph:
        g = rdflib.Graph()
        prop = rdflib.URIRef("http://test.org/ontology#hasSubject")
        g.add((prop, RDF.type, OWL.ObjectProperty))
        g.add((prop, _FM.sourceForeignKey, rdflib.Literal(fk_value)))
        return g

    def test_fk_col_verbatim_match_scores_1_0(self, settings):
        """ObjectProperty with fm:sourceForeignKey 'tbl.subject_id -> Subject'
        must score 1.0 for column 'subject_id' (verbatim after extraction)."""
        g = self._object_prop_graph("raw_admissions.subject_id -> Subject")
        m = AnnotationMatcher(g, settings)
        evs = m.evaluate_column(_col("subject_id"), _table())
        assert len(evs) == 1
        assert evs[0].score == pytest.approx(1.0)

    def test_fk_col_does_not_match_different_column(self, settings):
        """ObjectProperty annotated for 'subject_id' must not score for 'hadm_id'."""
        g = self._object_prop_graph("raw_admissions.subject_id -> Subject")
        m = AnnotationMatcher(g, settings)
        evs = m.evaluate_column(_col("hadm_id"), _table())
        assert evs == []

    def test_fk_value_without_table_prefix_still_extracted(self, settings):
        """Format 'table.col -> Class': col is extracted regardless of table name length."""
        g = self._object_prop_graph("raw_icustays.icustay_id -> IcuStay")
        m = AnnotationMatcher(g, settings)
        evs = m.evaluate_column(_col("icustay_id"), _table())
        assert len(evs) == 1
        assert evs[0].score == pytest.approx(1.0)

    def test_fk_annotation_coexists_with_label_annotations(self, settings):
        """A property with both rdfs:label and fm:sourceForeignKey is cached correctly;
        the FK column must still match at 1.0."""
        g = rdflib.Graph()
        prop = rdflib.URIRef("http://test.org/ontology#hasSubject")
        g.add((prop, RDF.type, OWL.ObjectProperty))
        g.add((prop, RDFS.label, rdflib.Literal("Has Subject")))
        g.add((prop, _FM.sourceForeignKey, rdflib.Literal("raw_admissions.subject_id -> Subject")))
        m = AnnotationMatcher(g, settings)
        evs = m.evaluate_column(_col("subject_id"), _table())
        assert len(evs) == 1
        assert evs[0].score == pytest.approx(1.0)

    def test_datatype_property_without_fk_annotation_unaffected(self, settings):
        """DatatypeProperty with skos:altLabel 'subject_id' still scores correctly.
        The fm:sourceForeignKey logic must not break DatatypeProperty matching."""
        g = rdflib.Graph()
        prop = rdflib.URIRef("http://test.org/ontology#hasSubjectId")
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        g.add((prop, _SKOS_ALT, rdflib.Literal("subject_id")))
        m = AnnotationMatcher(g, settings)
        evs = m.evaluate_column(_col("subject_id"), _table())
        assert len(evs) == 1
        assert evs[0].score == pytest.approx(1.0)
