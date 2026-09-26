# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for HistoryMatcher and updated PatternLookup scoring.

Key behaviours under test:
- AUTO-confirmed hits return best_confidence_score, not 1.0
- HUMAN-confirmed hits return 1.0
- Rationale strings clearly identify confirmation type
- HistoryMatcher emits strategy="HistoryMatcher" evidence
- Empty / no-match cases return empty evidence
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import rdflib

from semantic_mapper.config.settings import Settings
from semantic_mapper.db.corrections_store import CorrectionsStore, PatternLookup, _ConfirmedPattern
from semantic_mapper.db.models import ColumnProfile, TableProfile
from semantic_mapper.engine.strategies.history_matcher import HistoryMatcher


URI_A = "http://test.org/hasPartyType"
URI_B = "http://test.org/hasStatus"
ONTOLOGY = "test.owl"
DB_SRC = "test_db"


@pytest.fixture()
def store(tmp_path):
    return CorrectionsStore(tmp_path / "corrections.db")


@pytest.fixture()
def graph():
    g = rdflib.Graph()
    from rdflib.namespace import RDF, OWL
    g.add((rdflib.URIRef(URI_A), RDF.type, OWL.DatatypeProperty))
    g.add((rdflib.URIRef(URI_B), RDF.type, OWL.DatatypeProperty))
    return g


@pytest.fixture()
def settings_obj():
    return Settings(DATABASE_URL="mysql+pymysql://u:p@localhost/db", OPENAI_API_KEY="x")


def _col(name, sql_type="VARCHAR"):
    return ColumnProfile(
        name=name, inferred_type=sql_type,
        is_nullable=False, is_primary_key=False,
        null_rate=0.0, distinct_count=5, top_values=[],
    )


def _table(name="raw_party"):
    return TableProfile(table_name=name, row_count=100, columns={}, foreign_keys=[])


def _write_auto(store, col_name, uri, confidence, count=1):
    """Write `count` AUTO corrections rows for col_name → uri."""
    import uuid, json
    from datetime import datetime, timezone
    rows = [
        (
            "raw_party", col_name, "VARCHAR", uri, json.dumps([]),
            "AUTO", "NameMatcher", confidence, None, ONTOLOGY, DB_SRC,
            datetime.now(timezone.utc).isoformat(), str(uuid.uuid4()),
        )
        for _ in range(count)
    ]
    with sqlite3.connect(store.db_path) as conn:
        conn.executemany(
            """INSERT INTO corrections (
                source_table, source_column, source_data_type,
                confirmed_uri, rejected_uris, confirmation_type, confirmed_by,
                confidence_score, evidence_json, ontology_source, db_source,
                created_at, run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )


def _write_human(store, col_name, uri):
    store.overwrite_human_correction(
        "raw_party", col_name, uri, "VARCHAR", ONTOLOGY, DB_SRC
    )


# ---------------------------------------------------------------------------
# PatternLookup — AUTO scoring
# ---------------------------------------------------------------------------

class TestPatternLookupAutoScoring:
    def test_auto_exact_returns_stored_confidence(self, store):
        _write_auto(store, "party_type", URI_A, confidence=0.84, count=3)
        lookup = store.load_patterns(ONTOLOGY, DB_SRC)
        uri, score, rationale = lookup.match("party_type")
        assert uri == URI_A
        assert score == pytest.approx(0.84)

    def test_auto_rationale_says_auto_confirmed(self, store):
        _write_auto(store, "party_type", URI_A, confidence=0.84, count=3)
        lookup = store.load_patterns(ONTOLOGY, DB_SRC)
        _, _, rationale = lookup.match("party_type")
        assert "AUTO-confirmed" in rationale
        assert "pipeline run" in rationale

    def test_auto_multiple_runs_uses_max_confidence(self, store):
        """If confidence varied across runs, best_confidence_score is the max."""
        import uuid, json
        from datetime import datetime, timezone
        rows = [
            ("raw_party", "party_type", "VARCHAR", URI_A, json.dumps([]),
             "AUTO", "NameMatcher", conf, None, ONTOLOGY, DB_SRC,
             datetime.now(timezone.utc).isoformat(), str(uuid.uuid4()))
            for conf in (0.80, 0.84, 0.82)
        ]
        with sqlite3.connect(store.db_path) as conn:
            conn.executemany(
                """INSERT INTO corrections (
                    source_table, source_column, source_data_type,
                    confirmed_uri, rejected_uris, confirmation_type, confirmed_by,
                    confidence_score, evidence_json, ontology_source, db_source,
                    created_at, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        lookup = store.load_patterns(ONTOLOGY, DB_SRC)
        _, score, _ = lookup.match("party_type")
        assert score == pytest.approx(0.84)


# ---------------------------------------------------------------------------
# PatternLookup — HUMAN scoring
# ---------------------------------------------------------------------------

class TestPatternLookupHumanScoring:
    def test_human_exact_returns_1_0(self, store):
        _write_human(store, "party_type", URI_A)
        lookup = store.load_patterns(ONTOLOGY, DB_SRC)
        uri, score, rationale = lookup.match("party_type")
        assert uri == URI_A
        assert score == pytest.approx(1.0)

    def test_human_rationale_says_human_confirmed(self, store):
        _write_human(store, "party_type", URI_A)
        lookup = store.load_patterns(ONTOLOGY, DB_SRC)
        _, _, rationale = lookup.match("party_type")
        assert "Human-confirmed" in rationale

    def test_human_overrides_previous_auto_entries(self, store):
        """After human override, score must be 1.0 even if AUTO entries existed."""
        _write_auto(store, "party_type", URI_A, confidence=0.84, count=5)
        _write_human(store, "party_type", URI_A)
        lookup = store.load_patterns(ONTOLOGY, DB_SRC)
        _, score, rationale = lookup.match("party_type")
        assert score == pytest.approx(1.0)
        assert "Human-confirmed" in rationale


# ---------------------------------------------------------------------------
# PatternLookup — no match
# ---------------------------------------------------------------------------

class TestPatternLookupNoMatch:
    def test_unknown_column_returns_none(self, store):
        _write_auto(store, "party_type", URI_A, confidence=0.84)
        lookup = store.load_patterns(ONTOLOGY, DB_SRC)
        assert lookup.match("completely_unrelated_column") is None

    def test_empty_store_returns_none(self, store):
        lookup = store.load_patterns(ONTOLOGY, DB_SRC)
        assert lookup.match("party_type") is None


# ---------------------------------------------------------------------------
# HistoryMatcher — evidence emission
# ---------------------------------------------------------------------------

class TestHistoryMatcher:
    def test_emits_history_matcher_strategy_for_human(self, store, graph, settings_obj):
        """HistoryMatcher emits evidence only for HUMAN-confirmed corrections."""
        _write_human(store, "party_type", URI_A)
        lookup = store.load_patterns(ONTOLOGY, DB_SRC)
        matcher = HistoryMatcher(graph, settings_obj, lookup)
        evidence = matcher.evaluate_column(_col("party_type"), _table())
        assert len(evidence) == 1
        assert evidence[0].strategy == "HistoryMatcher"

    def test_auto_confirmation_produces_no_evidence(self, store, graph, settings_obj):
        """AUTO-confirmed corrections are intentionally ignored to prevent poisoning."""
        _write_auto(store, "party_type", URI_A, confidence=0.84)
        lookup = store.load_patterns(ONTOLOGY, DB_SRC)
        matcher = HistoryMatcher(graph, settings_obj, lookup)
        evidence = matcher.evaluate_column(_col("party_type"), _table())
        assert evidence == []

    def test_human_evidence_scores_1_0(self, store, graph, settings_obj):
        _write_human(store, "party_type", URI_A)
        lookup = store.load_patterns(ONTOLOGY, DB_SRC)
        matcher = HistoryMatcher(graph, settings_obj, lookup)
        evidence = matcher.evaluate_column(_col("party_type"), _table())
        assert evidence[0].score == pytest.approx(1.0)

    def test_no_match_returns_empty(self, store, graph, settings_obj):
        lookup = store.load_patterns(ONTOLOGY, DB_SRC)
        matcher = HistoryMatcher(graph, settings_obj, lookup)
        evidence = matcher.evaluate_column(_col("unknown_col"), _table())
        assert evidence == []

    def test_none_pattern_lookup_returns_empty(self, graph, settings_obj):
        matcher = HistoryMatcher(graph, settings_obj, pattern_lookup=None)
        evidence = matcher.evaluate_column(_col("party_type"), _table())
        assert evidence == []

    def test_evaluate_table_always_empty(self, graph, settings_obj):
        matcher = HistoryMatcher(graph, settings_obj, pattern_lookup=None)
        assert matcher.evaluate_table(_table()) == []


# ---------------------------------------------------------------------------
# PatternLookup — Priority 4 cross-column bleed prevention
# ---------------------------------------------------------------------------

class TestPatternLookupBleedPrevention:
    """A HUMAN correction on one column must not bleed onto an unrelated column
    that shares only a single token.  Priority 4 requires ≥2 token overlap;
    a single shared token (e.g. "event") does not qualify.
    """

    def _write_human_raw(self, store, col_name, uri, sql_type="VARCHAR"):
        """Write a HUMAN correction directly, bypassing overwrite_human_correction
        table-name scope so we can set an arbitrary table."""
        import uuid, json
        from datetime import datetime, timezone
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                """INSERT INTO corrections (
                    source_table, source_column, source_data_type,
                    confirmed_uri, rejected_uris, confirmation_type, confirmed_by,
                    confidence_score, evidence_json, ontology_source, db_source,
                    created_at, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "raw_events", col_name, sql_type, uri, json.dumps([]),
                    "HUMAN", "web_ui", 1.0, None, ONTOLOGY, DB_SRC,
                    datetime.now(timezone.utc).isoformat(), str(uuid.uuid4()),
                ),
            )

    def test_single_token_human_does_not_bleed_to_other_column(self, store):
        """HUMAN correction on 'event_class' (tokens: event, class) must NOT
        bleed onto 'event_cost' (tokens: event, cost): only 1 shared token."""
        self._write_human_raw(store, "event_class", URI_A, sql_type="VARCHAR")
        lookup = store.load_patterns(ONTOLOGY, DB_SRC)
        result = lookup.match("event_cost", sql_type="VARCHAR", human_only=True)
        assert result is None, (
            f"Priority 4 single-token bleed: 'event_cost' matched via 'event_class' "
            f"with shared token 'event' alone — got {result}"
        )

    def test_two_token_human_matches_related_column(self, store):
        """HUMAN correction on 'event_class_code' (tokens: event, class, code) DOES
        carry onto 'event_class_type' (tokens: event, class, type): 2 shared tokens."""
        self._write_human_raw(store, "event_class_code", URI_A, sql_type="VARCHAR")
        lookup = store.load_patterns(ONTOLOGY, DB_SRC)
        result = lookup.match("event_class_type", sql_type="VARCHAR", human_only=True)
        assert result is not None, (
            "Priority 4 two-token match failed: 'event_class_type' should match "
            "'event_class_code' via shared tokens 'event' and 'class'"
        )
        assert result[0] == URI_A

    def test_single_token_auto_does_not_bleed(self, store):
        """AUTO correction on 'event_category' (tokens: event, category) must NOT
        bleed onto 'event_cost' (tokens: event, cost): only 1 shared token."""
        _write_auto(store, "event_category", URI_A, confidence=0.88)
        lookup = store.load_patterns(ONTOLOGY, DB_SRC)
        result = lookup.match("event_cost", sql_type="VARCHAR")
        assert result is None, (
            f"Priority 4 single-token AUTO bleed: got {result}"
        )
