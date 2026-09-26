# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for CorrectionsStore — value_mappings cache methods.

All tests use a temporary SQLite file (tmp_path fixture) so there is no
interference with the real corrections.db on disk.
"""
import json
import pytest

from semantic_mapper.db.corrections_store import CorrectionsStore
from semantic_mapper.schema.mapping_output import ValueMappingRule


ONTOLOGY = "test_ontology.owl"
DB_SRC   = "test_db"
TABLE    = "raw_party"
COL      = "party_type"

RULES = [
    ValueMappingRule(
        source_value="I",
        mapped_uri="http://example.org/ont#Individual",
        rationale="Individual customer",
        confidence_score=0.95,
    ),
    ValueMappingRule(
        source_value="O",
        mapped_uri="http://example.org/ont#Organisation",
        rationale="Organisation customer",
        confidence_score=0.93,
    ),
]


@pytest.fixture
def store(tmp_path):
    return CorrectionsStore(tmp_path / "corrections.db")


# ---------------------------------------------------------------------------
# load_value_mappings — cache miss
# ---------------------------------------------------------------------------

def test_load_returns_none_when_empty(store):
    result = store.load_value_mappings(TABLE, COL, ONTOLOGY, DB_SRC)
    assert result is None


# ---------------------------------------------------------------------------
# write_value_mappings + load_value_mappings round-trip
# ---------------------------------------------------------------------------

def test_round_trip_stores_and_retrieves_rules(store):
    store.write_value_mappings(TABLE, COL, RULES, ONTOLOGY, DB_SRC)
    result = store.load_value_mappings(TABLE, COL, ONTOLOGY, DB_SRC)

    assert result is not None
    assert len(result) == 2
    assert result[0].source_value == "I"
    assert result[0].mapped_uri == "http://example.org/ont#Individual"
    assert result[1].source_value == "O"
    assert result[1].confidence_score == pytest.approx(0.93)


def test_load_returns_most_recent_entry(store):
    """Second write supersedes the first — load returns latest."""
    updated = [
        ValueMappingRule(
            source_value="I",
            mapped_uri="http://example.org/ont#Person",
            rationale="Updated mapping",
            confidence_score=0.99,
        ),
    ]
    store.write_value_mappings(TABLE, COL, RULES, ONTOLOGY, DB_SRC)
    store.write_value_mappings(TABLE, COL, updated, ONTOLOGY, DB_SRC)

    result = store.load_value_mappings(TABLE, COL, ONTOLOGY, DB_SRC)
    assert result is not None
    assert len(result) == 1
    assert result[0].mapped_uri == "http://example.org/ont#Person"


# ---------------------------------------------------------------------------
# Isolation — different (table, column, ontology, db) combinations
# ---------------------------------------------------------------------------

def test_different_column_is_isolated(store):
    store.write_value_mappings(TABLE, COL, RULES, ONTOLOGY, DB_SRC)
    result = store.load_value_mappings(TABLE, "other_col", ONTOLOGY, DB_SRC)
    assert result is None


def test_different_table_is_isolated(store):
    store.write_value_mappings(TABLE, COL, RULES, ONTOLOGY, DB_SRC)
    result = store.load_value_mappings("other_table", COL, ONTOLOGY, DB_SRC)
    assert result is None


def test_different_ontology_is_isolated(store):
    store.write_value_mappings(TABLE, COL, RULES, ONTOLOGY, DB_SRC)
    result = store.load_value_mappings(TABLE, COL, "other_ontology.owl", DB_SRC)
    assert result is None


def test_different_db_source_is_isolated(store):
    store.write_value_mappings(TABLE, COL, RULES, ONTOLOGY, DB_SRC)
    result = store.load_value_mappings(TABLE, COL, ONTOLOGY, "other_db")
    assert result is None


# ---------------------------------------------------------------------------
# clear() wipes value_mappings too
# ---------------------------------------------------------------------------

def test_clear_removes_value_mappings(store):
    store.write_value_mappings(TABLE, COL, RULES, ONTOLOGY, DB_SRC)
    store.clear()
    assert store.load_value_mappings(TABLE, COL, ONTOLOGY, DB_SRC) is None


# ---------------------------------------------------------------------------
# Empty rules list
# ---------------------------------------------------------------------------

def test_write_empty_rules_and_retrieve(store):
    store.write_value_mappings(TABLE, COL, [], ONTOLOGY, DB_SRC)
    result = store.load_value_mappings(TABLE, COL, ONTOLOGY, DB_SRC)
    assert result == []


# ---------------------------------------------------------------------------
# Schema is idempotent (double init does not raise)
# ---------------------------------------------------------------------------

def test_double_init_is_idempotent(tmp_path):
    path = tmp_path / "corrections.db"
    CorrectionsStore(path)
    CorrectionsStore(path)  # should not raise
