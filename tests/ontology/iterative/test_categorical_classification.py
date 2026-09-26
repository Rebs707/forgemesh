# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the proportional class-vs-literal decision (is_closed_vocabulary) and
its use in _compute_cardinality_hint.

A low-distinct text column is only a controlled vocabulary (-> owl:Class) when its
values recur across enough non-null rows; sparse/mostly-null fields fall through to
a plain xsd literal even though their distinct_count is small.
"""
from __future__ import annotations

import types

from semantic_mapper.ontology.iterative.context_builder import (
    is_closed_vocabulary,
    _compute_cardinality_hint,
)


def _col(inferred_type="VARCHAR(200)", distinct_count=9, null_rate=0.0, is_primary_key=False):
    return types.SimpleNamespace(
        inferred_type=inferred_type,
        distinct_count=distinct_count,
        null_rate=null_rate,
        is_primary_key=is_primary_key,
    )


class TestIsClosedVocabulary:
    def test_real_vocabulary_is_class(self):
        # ethnicity: 9 distinct, 0% null, 129 rows
        assert is_closed_vocabulary(9, 0.0, 129, max_distinct=20) is True

    def test_partial_null_with_strong_support_is_class(self):
        # param_type: 76% null but 2992 supporting rows, 7 distinct
        assert is_closed_vocabulary(7, 0.76, 2992, max_distinct=20) is True

    def test_high_null_is_literal(self):
        # originalsite: distinct=1 but ~100% null over a big table
        assert is_closed_vocabulary(1, 1.0, 6, max_distinct=20) is False

    def test_mostly_null_high_support_is_literal(self):
        # comments_canceledby: 96.4% null, 479 non-null, 2 distinct — null dominates
        assert is_closed_vocabulary(2, 0.964, 479, max_distinct=20) is False

    def test_small_dense_dimension_is_class_by_default(self):
        # 10 distinct over 10 non-null rows, 0% null — a lookup/dimension table,
        # NOT noise. Defaults must keep it a class (support/uniqueness off by default).
        assert is_closed_vocabulary(10, 0.0, 10, max_distinct=20) is True

    def test_distinct_over_max_is_literal(self):
        assert is_closed_vocabulary(25, 0.0, 1000, max_distinct=20) is False

    def test_null_cutoff_is_tunable(self):
        # lifting the null cutoff to 0.99 flips comments_canceledby back to class
        assert is_closed_vocabulary(2, 0.964, 479, max_distinct=20, max_null_rate=0.99) is True

    def test_optional_support_floor_when_enabled(self):
        # min_support is OFF by default; enabling it demotes a tiny column
        assert is_closed_vocabulary(3, 0.0, 10, max_distinct=20) is True            # default: off
        assert is_closed_vocabulary(3, 0.0, 10, max_distinct=20, min_support=20) is False

    def test_optional_uniqueness_cap_when_enabled(self):
        # max_uniqueness is OFF by default; enabling it demotes a barely-recurring column
        assert is_closed_vocabulary(18, 0.0, 20, max_distinct=20) is True           # default: off
        assert is_closed_vocabulary(18, 0.0, 20, max_distinct=20, max_uniqueness=0.5) is False


class TestCardinalityHintNullAware:
    def test_real_vocab_text_is_categorical_closed(self):
        hint = _compute_cardinality_hint(
            _col(distinct_count=9, null_rate=0.0), is_fk=False,
            top_vals=["WHITE", "BLACK/AFRICAN AMERICAN"],
            max_distinct_for_enumeration=20, max_distinct_for_open_class=100,
            row_count=129,
        )
        assert hint == "CATEGORICAL_CLOSED"

    def test_sparse_text_not_categorical_closed(self):
        # originalsite-like: distinct=1 but ~100% null → must NOT be a class
        hint = _compute_cardinality_hint(
            _col(distinct_count=1, null_rate=1.0), is_fk=False,
            top_vals=["Right Femoral"],
            max_distinct_for_enumeration=20, max_distinct_for_open_class=100,
            row_count=34799,
        )
        assert hint != "CATEGORICAL_CLOSED"

    def test_mostly_null_status_field_not_categorical_closed(self):
        # resultstatus-like: 2 distinct, 97% null
        hint = _compute_cardinality_hint(
            _col(distinct_count=2, null_rate=0.971), is_fk=False,
            top_vals=["A", "B"],
            max_distinct_for_enumeration=20, max_distinct_for_open_class=100,
            row_count=21674 / (1 - 0.971),
        )
        assert hint != "CATEGORICAL_CLOSED"
