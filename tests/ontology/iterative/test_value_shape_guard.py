# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for the value-shape guard that keeps descriptive string columns out of
enumeration-class modelling (Fix A + C of the generator refinement).

A genuine controlled vocabulary is a small set of short labels that RECUR across
many rows. Descriptive attributes that happen to be low-distinct (because the table
is a small dimension) must become DatatypeProperties, not owl:Class enumerations:
  - URL columns (venue/sport URLs)
  - JSON-array / list cells (multi-valued discipline columns)
  - near-unique multi-word free text (venue names)
Recurring multi-word labels (e.g. "Head Coach") are still vocabularies.
"""
from __future__ import annotations

import types

from semantic_mapper.ontology.iterative.context_builder import (
    _values_look_descriptive,
    _compute_cardinality_hint,
)


def _col(inferred_type="VARCHAR(200)", distinct_count=6, null_rate=0.0, is_primary_key=False):
    return types.SimpleNamespace(
        inferred_type=inferred_type,
        distinct_count=distinct_count,
        null_rate=null_rate,
        is_primary_key=is_primary_key,
    )


def _hint(col, top_vals, row_count):
    return _compute_cardinality_hint(
        col, is_fk=False, top_vals=top_vals,
        max_distinct_for_enumeration=20, max_distinct_for_open_class=100,
        row_count=row_count,
    )


class TestValuesLookDescriptive:
    def test_urls_are_descriptive(self):
        assert _values_look_descriptive(
            ["https://x/a", "https://x/b", "http://x/c"], 3, 3) is True

    def test_json_array_cells_are_descriptive(self):
        assert _values_look_descriptive(
            ['discipline-biathlon', '["a","b","c"]', 'discipline-curling'], 12, 15) is True

    def test_near_unique_multiword_is_descriptive(self):
        # venue names: one descriptive value per row
        assert _values_look_descriptive(
            ["Cortina Curling Stadium", "Livigno Snow Park", "Tofane Centre"], 15, 15) is True

    def test_recurring_multiword_label_is_not_descriptive(self):
        # "Head Coach" recurs (2 distinct over 50 rows) → a real vocabulary
        assert _values_look_descriptive(["Coach", "Head Coach"], 2, 50) is False

    def test_short_single_token_vocab_is_not_descriptive(self):
        assert _values_look_descriptive(["GOLD", "SILVER", "BRONZE"], 3, 709) is False

    def test_empty_values_not_descriptive(self):
        assert _values_look_descriptive([], 0, 0) is False


class TestCardinalityHintGuard:
    def test_url_column_not_categorical(self):
        col = _col(distinct_count=6, null_rate=0.0)
        assert _hint(col, ["https://o/a", "https://o/b"], row_count=15) != "CATEGORICAL_CLOSED"

    def test_json_list_column_not_categorical(self):
        col = _col(distinct_count=12, null_rate=0.0)
        assert _hint(col, ['discipline-x', '["a","b"]'], row_count=15) != "CATEGORICAL_CLOSED"

    def test_near_unique_name_not_categorical(self):
        col = _col(distinct_count=15, null_rate=0.0)
        vals = ["Cortina Curling Stadium", "Livigno Snow Park", "Tofane Centre"]
        assert _hint(col, vals, row_count=15) != "CATEGORICAL_CLOSED"

    def test_recurring_multiword_still_categorical(self):
        col = _col(distinct_count=2, null_rate=0.0)
        assert _hint(col, ["Coach", "Head Coach"], row_count=50) == "CATEGORICAL_CLOSED"

    def test_short_vocab_still_categorical(self):
        col = _col(distinct_count=3, null_rate=0.0)
        assert _hint(col, ["GOLD", "SILVER", "BRONZE"], row_count=709) == "CATEGORICAL_CLOSED"
