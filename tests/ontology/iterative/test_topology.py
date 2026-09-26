# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
import pytest
from semantic_mapper.db.models import ColumnProfile, TableProfile
from semantic_mapper.ontology.iterative.topology import fk_topological_sort


def _profile(name: str, row_count: int = 100, fks: list = None) -> TableProfile:
    return TableProfile(
        table_name=name,
        row_count=row_count,
        columns={},
        foreign_keys=fks or [],
    )


def _fk(col: str, ref_table: str) -> dict:
    return {"constrained_column": col, "referred_table": ref_table}


class TestFkTopologicalSort:
    def test_no_dependencies_stable_order(self):
        profiles = [_profile("c"), _profile("a"), _profile("b")]
        result = fk_topological_sort(profiles)
        names = [p.table_name for p in result]
        assert set(names) == {"a", "b", "c"}

    def test_simple_chain(self):
        """B → A means A must come before B."""
        profiles = [
            _profile("b", fks=[_fk("a_id", "a")]),
            _profile("a"),
        ]
        result = fk_topological_sort(profiles)
        names = [p.table_name for p in result]
        assert names.index("a") < names.index("b")

    def test_diamond_dependency(self):
        """c depends on a and b; b depends on a — order: a, b, c."""
        profiles = [
            _profile("c", fks=[_fk("a_id", "a"), _fk("b_id", "b")]),
            _profile("b", fks=[_fk("a_id", "a")]),
            _profile("a"),
        ]
        result = fk_topological_sort(profiles)
        names = [p.table_name for p in result]
        assert names.index("a") < names.index("b")
        assert names.index("a") < names.index("c")
        assert names.index("b") < names.index("c")

    def test_self_reference_ignored(self):
        """A FK pointing to the same table is silently skipped."""
        profiles = [_profile("a", fks=[_fk("parent_id", "a")])]
        result = fk_topological_sort(profiles)
        assert [p.table_name for p in result] == ["a"]

    def test_fk_to_outside_set_ignored(self):
        """FK pointing to a table not in profiles is ignored."""
        profiles = [_profile("a", fks=[_fk("x_id", "external_table")])]
        result = fk_topological_sort(profiles)
        assert [p.table_name for p in result] == ["a"]

    def test_all_tables_present_after_sort(self):
        profiles = [
            _profile("a"),
            _profile("b", fks=[_fk("a_id", "a")]),
            _profile("c", fks=[_fk("b_id", "b")]),
            _profile("d"),
        ]
        result = fk_topological_sort(profiles)
        assert len(result) == 4
        assert {p.table_name for p in result} == {"a", "b", "c", "d"}

    def test_cycle_detection_does_not_lose_tables(self):
        """Circular FK — both tables must appear in output."""
        profiles = [
            _profile("x", row_count=10, fks=[_fk("y_id", "y")]),
            _profile("y", row_count=1000, fks=[_fk("x_id", "x")]),
        ]
        result = fk_topological_sort(profiles)
        assert len(result) == 2
        assert {p.table_name for p in result} == {"x", "y"}
