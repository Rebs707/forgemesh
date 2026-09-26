# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for the 'descriptive column shadowing a coded FK' guard.

A low-cardinality text column whose sibling `{col}_code`/`{col}_id` is a foreign key
is the human-readable form of that entity reference (a `venue` name alongside a
`venue_code` FK to a Venue entity) — so it must NOT become its own enumeration class.
"""
from __future__ import annotations

from semantic_mapper.db.models import ColumnProfile, TableProfile
from semantic_mapper.ontology.iterative.context_builder import (
    build_table_context,
    _shadows_coded_fk,
)


def _col(name, distinct, pk=False, top=None):
    return ColumnProfile(
        name=name, inferred_type="VARCHAR(64)", is_nullable=True,
        is_primary_key=pk, null_rate=0.0, distinct_count=distinct,
        top_values=top or [],
    )


def _top(values):
    return [{"value": v, "count": 100} for v in values]


class TestShadowsCodedFk:
    def test_code_sibling_is_shadow(self):
        assert _shadows_coded_fk("venue", {"venue_code"}) is True

    def test_id_sibling_is_shadow(self):
        assert _shadows_coded_fk("venue", {"venue_id"}) is True

    def test_no_sibling_not_shadow(self):
        assert _shadows_coded_fk("venue", set()) is False

    def test_unrelated_fk_not_shadow(self):
        assert _shadows_coded_fk("discipline", {"venue_code"}) is False


class TestBuildContextDemotion:
    def test_name_demoted_when_code_sibling_is_fk(self):
        sched = TableProfile(
            table_name="raw_schedules", row_count=1884,
            columns={
                "row_id":     _col("row_id", 1884, pk=True),
                "venue":      _col("venue", 15, top=_top([f"Arena{i}" for i in range(15)])),
                "venue_code": _col("venue_code", 15),
            },
            foreign_keys=[{"column": "venue_code", "ref_table": "raw_venues"}],
        )
        ctx = build_table_context(sched, processing_index=1, total_tables=1)
        by_name = {c.col_name: c for c in ctx.columns}
        assert by_name["venue"].is_categorical is False  # shadows venue_code FK → demoted

    def test_categorical_kept_without_coded_fk_sibling(self):
        sched = TableProfile(
            table_name="raw_schedules", row_count=1884,
            columns={
                "row_id": _col("row_id", 1884, pk=True),
                "status": _col("status", 3, top=_top(["FINISHED", "SCHEDULED", "CANCELLED"])),
            },
            foreign_keys=[],
        )
        ctx = build_table_context(sched, processing_index=1, total_tables=1)
        by_name = {c.col_name: c for c in ctx.columns}
        assert by_name["status"].is_categorical is True  # genuine vocabulary, kept
