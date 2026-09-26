# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for PK-denoted entity class naming (the country→Medal fix).

A dimension table named after a measure (a per-country medal-tally table `medals`,
PK country_code) should produce a class named after the entity its PK denotes
(Country), not the table (Medal) — but only when guarded signals agree, so identity
tables (patients, caregivers) keep their names. Dataset-agnostic.
"""
from __future__ import annotations

import pytest

from semantic_mapper.db.models import ColumnProfile, TableProfile
from semantic_mapper.ontology.iterative.context_builder import (
    compute_class_name_overrides,
    set_class_name_overrides,
    derive_class_local_name,
)


@pytest.fixture(autouse=True)
def _reset_overrides():
    """Keep the module-level override map isolated between tests."""
    set_class_name_overrides({})
    yield
    set_class_name_overrides({})


def _col(name, pk=False):
    return ColumnProfile(name=name, inferred_type="VARCHAR(32)", is_nullable=False,
                         is_primary_key=pk, null_rate=0.0, distinct_count=10)


def _tbl(name, cols, pk):
    return TableProfile(table_name=name, row_count=100,
                        columns={c: _col(c, pk=(c == pk)) for c in cols}, foreign_keys=[])


class TestComputeOverrides:
    def test_pk_with_sibling_column_renames(self):
        t = _tbl("raw_medals", ["country", "country_code", "gold"], "country_code")
        assert compute_class_name_overrides([t]) == {"raw_medals": "Country"}

    def test_surrogate_pk_does_not_rename(self):
        t = _tbl("raw_patients", ["row_id", "subject_id", "gender"], "row_id")
        assert compute_class_name_overrides([t]) == {}

    def test_opaque_pk_without_id_split_does_not_rename(self):
        # cgid has no `{X}_id` underscore split → caregivers keeps its name
        t = _tbl("raw_caregivers", ["cgid", "label"], "cgid")
        assert compute_class_name_overrides([t]) == {}

    def test_no_sibling_column_does_not_rename(self):
        t = _tbl("raw_widgets", ["widget_code", "price"], "widget_code")
        assert compute_class_name_overrides([t]) == {}

    def test_pk_entity_equal_to_table_name_does_not_rename(self):
        # venue_code + venue sibling → 'Venue' == table-derived 'Venue' → no override
        t = _tbl("raw_venues", ["venue", "venue_code", "url"], "venue_code")
        assert compute_class_name_overrides([t]) == {}

    def test_composite_pk_does_not_rename(self):
        t = TableProfile(
            table_name="raw_medals", row_count=100,
            columns={"country": _col("country", pk=True),
                     "country_code": _col("country_code", pk=True),
                     "gold": _col("gold")},
            foreign_keys=[],
        )
        assert compute_class_name_overrides([t]) == {}


class TestOverrideApplied:
    def test_override_changes_derived_name(self):
        assert derive_class_local_name("raw_medals") == "Medal"   # default
        set_class_name_overrides({"raw_medals": "Country"})
        assert derive_class_local_name("raw_medals") == "Country"

    def test_unoverridden_tables_unchanged(self):
        set_class_name_overrides({"raw_medals": "Country"})
        assert derive_class_local_name("raw_athletes") == "Athlete"

    def test_reset_restores_table_derivation(self):
        set_class_name_overrides({"raw_medals": "Country"})
        set_class_name_overrides({})
        assert derive_class_local_name("raw_medals") == "Medal"
