# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for Task 8: cardinality metadata threading into the iterative ontology generator.

Covers:
  - _compute_cardinality_hint() classification logic
  - build_table_context() sets cardinality_hint on each ColumnContext
  - gen_prompt shows correct flag text per hint value
  - judge_prompt shows cardinality_hint in coverage checklist and rule 12
"""
import pytest

from semantic_mapper.db.models import ColumnProfile, TableProfile
from semantic_mapper.ontology.iterative.context_builder import (
    _compute_cardinality_hint,
    build_table_context,
)
from semantic_mapper.ontology.iterative.gen_prompt import build_gen_prompt
from semantic_mapper.ontology.iterative.judge_prompt import build_judge_prompt
from semantic_mapper.ontology.iterative.models import (
    ColumnContext,
    OntologyFragment,
    TableContext,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _col_profile(
    name: str,
    inferred_type: str,
    distinct_count: int,
    is_pk: bool = False,
    is_nullable: bool = False,
    top_values=None,
) -> ColumnProfile:
    top_values = top_values or []
    return ColumnProfile(
        name=name,
        inferred_type=inferred_type,
        is_primary_key=is_pk,
        is_nullable=is_nullable,
        null_rate=0.0,
        distinct_count=distinct_count,
        top_values=top_values,
        detected_formats=[],
    )


def _table_profile(cols: list, row_count: int = 100) -> TableProfile:
    return TableProfile(
        table_name="test_table",
        row_count=row_count,
        columns={c.name: c for c in cols},
        foreign_keys=[],
        primary_keys=[c.name for c in cols if c.is_primary_key],
    )


def _minimal_col_context(
    col_name: str,
    inferred_type: str,
    is_pk: bool = False,
    is_fk: bool = False,
    distinct_count: int = 5,
    top_values=None,
    cardinality_hint: str = "",
    categorical_values=None,
    is_categorical: bool = False,
) -> ColumnContext:
    return ColumnContext(
        table_name="test_table",
        col_name=col_name,
        inferred_type=inferred_type,
        is_pk=is_pk,
        is_fk=is_fk,
        null_rate=0.0,
        distinct_count=distinct_count,
        top_values=top_values or [],
        is_categorical=is_categorical,
        categorical_values=categorical_values or [],
        cardinality_hint=cardinality_hint,
    )


def _minimal_table_ctx(cols: list) -> TableContext:
    return TableContext(
        table_name="test_table",
        row_count=100,
        columns=cols,
        explicit_fks=[],
        processing_index=1,
        total_tables=1,
    )


# ---------------------------------------------------------------------------
# _compute_cardinality_hint unit tests
# ---------------------------------------------------------------------------

class TestComputeCardinalityHint:
    MAX_ENUM = 20
    MAX_OPEN = 100

    def _hint(self, col_profile, is_fk=False, top_vals=None):
        return _compute_cardinality_hint(
            col_profile, is_fk, top_vals or [], self.MAX_ENUM, self.MAX_OPEN
        )

    def test_pk_returns_empty(self):
        col = _col_profile("id", "INTEGER", 500, is_pk=True)
        assert self._hint(col) == ""

    def test_fk_returns_empty(self):
        col = _col_profile("patient_id", "INTEGER", 500)
        assert self._hint(col, is_fk=True) == ""

    def test_categorical_closed_varchar(self):
        col = _col_profile("status", "VARCHAR", 3)
        assert self._hint(col, top_vals=["A", "B", "C"]) == "CATEGORICAL_CLOSED"

    def test_categorical_closed_char(self):
        col = _col_profile("gender", "CHAR(1)", 2)
        assert self._hint(col, top_vals=["M", "F"]) == "CATEGORICAL_CLOSED"

    def test_categorical_closed_text(self):
        col = _col_profile("category", "TEXT", 5)
        assert self._hint(col, top_vals=["a", "b", "c", "d", "e"]) == "CATEGORICAL_CLOSED"

    def test_categorical_closed_requires_top_values(self):
        col = _col_profile("status", "VARCHAR", 3)
        assert self._hint(col, top_vals=[]) == ""

    def test_flag_integer(self):
        col = _col_profile("flag", "INTEGER", 2)
        assert self._hint(col) == "FLAG"

    def test_flag_tinyint(self):
        col = _col_profile("active", "TINYINT", 2)
        assert self._hint(col) == "FLAG"

    def test_flag_boolean(self):
        col = _col_profile("is_active", "BOOLEAN", 2)
        assert self._hint(col) == "FLAG"

    def test_flag_bit(self):
        col = _col_profile("deleted", "BIT", 2)
        assert self._hint(col) == "FLAG"

    def test_categorical_open_mid_cardinality_varchar(self):
        col = _col_profile("drug_type", "VARCHAR", 45)
        assert self._hint(col) == "CATEGORICAL_OPEN"

    def test_categorical_open_at_boundary(self):
        col = _col_profile("code", "VARCHAR", self.MAX_OPEN)
        assert self._hint(col) == "CATEGORICAL_OPEN"

    def test_free_text_high_cardinality_varchar(self):
        col = _col_profile("notes", "VARCHAR", 5000)
        assert self._hint(col) == "FREE_TEXT"

    def test_identifier_high_cardinality_integer(self):
        col = _col_profile("seq_num", "INTEGER", 5000)
        assert self._hint(col) == "IDENTIFIER"

    def test_identifier_decimal(self):
        col = _col_profile("amount", "DECIMAL", 500)
        assert self._hint(col) == "IDENTIFIER"

    def test_identifier_at_boundary(self):
        col = _col_profile("count", "INTEGER", self.MAX_OPEN + 1)
        assert self._hint(col) == "IDENTIFIER"

    def test_datetime_no_hint(self):
        col = _col_profile("created_at", "DATETIME", 500)
        assert self._hint(col) == ""

    def test_date_no_hint(self):
        col = _col_profile("admit_date", "DATE", 200)
        assert self._hint(col) == ""


# ---------------------------------------------------------------------------
# build_table_context sets cardinality_hint correctly
# ---------------------------------------------------------------------------

class TestBuildTableContextCardinalityHint:
    def test_categorical_closed_column(self):
        profile = _table_profile([
            _col_profile("id", "INTEGER", 100, is_pk=True),
            _col_profile("status", "VARCHAR", 3,
                         top_values=[{"value": "A", "count": 10},
                                     {"value": "B", "count": 5},
                                     {"value": "C", "count": 2}]),
        ])
        ctx = build_table_context(profile, 1, 1,
                                  max_distinct_for_enumeration=20,
                                  max_distinct_for_open_class=100)
        status_col = next(c for c in ctx.columns if c.col_name == "status")
        assert status_col.cardinality_hint == "CATEGORICAL_CLOSED"
        assert status_col.is_categorical is True
        assert status_col.categorical_values == ["A", "B", "C"]

    def test_flag_column(self):
        profile = _table_profile([
            _col_profile("id", "INTEGER", 100, is_pk=True),
            _col_profile("active", "TINYINT", 2),
        ])
        ctx = build_table_context(profile, 1, 1)
        active_col = next(c for c in ctx.columns if c.col_name == "active")
        assert active_col.cardinality_hint == "FLAG"
        assert active_col.is_categorical is False

    def test_categorical_open_column(self):
        profile = _table_profile([
            _col_profile("id", "INTEGER", 100, is_pk=True),
            _col_profile("drug_type", "VARCHAR", 45),
        ])
        ctx = build_table_context(profile, 1, 1)
        col = next(c for c in ctx.columns if c.col_name == "drug_type")
        assert col.cardinality_hint == "CATEGORICAL_OPEN"
        assert col.is_categorical is False

    def test_free_text_column(self):
        profile = _table_profile([
            _col_profile("id", "INTEGER", 100, is_pk=True),
            _col_profile("notes", "VARCHAR", 5000),
        ])
        ctx = build_table_context(profile, 1, 1)
        col = next(c for c in ctx.columns if c.col_name == "notes")
        assert col.cardinality_hint == "FREE_TEXT"

    def test_identifier_column(self):
        profile = _table_profile([
            _col_profile("id", "INTEGER", 100, is_pk=True),
            _col_profile("seq_num", "INTEGER", 5000),
        ])
        ctx = build_table_context(profile, 1, 1)
        col = next(c for c in ctx.columns if c.col_name == "seq_num")
        assert col.cardinality_hint == "IDENTIFIER"

    def test_pk_has_no_hint(self):
        profile = _table_profile([
            _col_profile("id", "INTEGER", 100, is_pk=True),
        ])
        ctx = build_table_context(profile, 1, 1)
        pk_col = ctx.columns[0]
        assert pk_col.cardinality_hint == ""

    def test_custom_thresholds(self):
        profile = _table_profile([
            _col_profile("id", "INTEGER", 100, is_pk=True),
            _col_profile("code", "VARCHAR", 30,
                         top_values=[{"value": str(i), "count": 1} for i in range(30)]),
        ])
        # With default thresholds (enum=20), distinct_count=30 → CATEGORICAL_OPEN
        ctx_default = build_table_context(profile, 1, 1)
        col = next(c for c in ctx_default.columns if c.col_name == "code")
        assert col.cardinality_hint == "CATEGORICAL_OPEN"

        # With raised threshold (enum=50), distinct_count=30 → CATEGORICAL_CLOSED
        ctx_raised = build_table_context(profile, 1, 1,
                                         max_distinct_for_enumeration=50,
                                         max_distinct_for_open_class=200)
        col2 = next(c for c in ctx_raised.columns if c.col_name == "code")
        assert col2.cardinality_hint == "CATEGORICAL_CLOSED"


# ---------------------------------------------------------------------------
# gen_prompt shows cardinality_hint in column table
# ---------------------------------------------------------------------------

class TestGenPromptCardinalityHint:
    BASE_IRI = "http://mesh.local/ontology/test#"

    def _build(self, cols):
        ctx = _minimal_table_ctx(cols)
        return build_gen_prompt(
            table_ctx=ctx,
            retrieved_classes=[],
            deterministic_uri=f"{self.BASE_IRI}TestTable",
            base_iri=self.BASE_IRI,
        )

    def test_categorical_closed_shown_in_prompt(self):
        col = _minimal_col_context(
            "status", "VARCHAR",
            cardinality_hint="CATEGORICAL_CLOSED",
            is_categorical=True,
            categorical_values=["A", "B", "C"],
        )
        prompt = self._build([col])
        assert "CATEGORICAL_CLOSED" in prompt
        assert "A, B, C" in prompt

    def test_flag_shown_in_prompt(self):
        col = _minimal_col_context("active", "TINYINT", cardinality_hint="FLAG")
        prompt = self._build([col])
        assert "FLAG" in prompt

    def test_categorical_open_shown_in_prompt(self):
        col = _minimal_col_context(
            "drug_type", "VARCHAR",
            cardinality_hint="CATEGORICAL_OPEN",
            top_values=["TypeA", "TypeB"],
        )
        prompt = self._build([col])
        assert "CATEGORICAL_OPEN" in prompt

    def test_free_text_shown_in_prompt(self):
        col = _minimal_col_context("notes", "VARCHAR", cardinality_hint="FREE_TEXT")
        prompt = self._build([col])
        assert "FREE_TEXT" in prompt

    def test_identifier_shown_in_prompt(self):
        col = _minimal_col_context("seq_num", "INTEGER", cardinality_hint="IDENTIFIER")
        prompt = self._build([col])
        assert "IDENTIFIER" in prompt

    def test_rule_14_in_prompt(self):
        col = _minimal_col_context("status", "VARCHAR", cardinality_hint="CATEGORICAL_CLOSED",
                                   is_categorical=True, categorical_values=["A"])
        prompt = self._build([col])
        assert "CARDINALITY HINT rules" in prompt
        assert "owl:oneOf" in prompt
        assert "HARD RULE" in prompt


# ---------------------------------------------------------------------------
# judge_prompt shows cardinality_hint in coverage checklist and rule 12
# ---------------------------------------------------------------------------

class TestJudgePromptCardinalityHint:
    BASE_IRI = "http://mesh.local/ontology/test#"

    def _build(self, cols):
        ctx = _minimal_table_ctx(cols)
        fragment = OntologyFragment(
            table_name="test_table",
            manchester_syntax="Class: :TestTable",
        )
        _, user_prompt = build_judge_prompt(
            table_ctx=ctx,
            fragment=fragment,
            accumulated_classes=[],
            base_iri=self.BASE_IRI,
        )
        return user_prompt

    def test_categorical_closed_in_coverage(self):
        col = _minimal_col_context(
            "status", "VARCHAR",
            cardinality_hint="CATEGORICAL_CLOSED",
            is_categorical=True,
            categorical_values=["A", "B", "C"],
        )
        prompt = self._build([col])
        assert "CATEGORICAL_CLOSED" in prompt

    def test_flag_in_coverage(self):
        col = _minimal_col_context("active", "TINYINT", cardinality_hint="FLAG")
        prompt = self._build([col])
        assert "FLAG" in prompt

    def test_categorical_open_in_coverage(self):
        col = _minimal_col_context("drug_type", "VARCHAR", cardinality_hint="CATEGORICAL_OPEN")
        prompt = self._build([col])
        assert "CATEGORICAL_OPEN" in prompt

    def test_free_text_in_coverage(self):
        col = _minimal_col_context("notes", "VARCHAR", cardinality_hint="FREE_TEXT")
        prompt = self._build([col])
        assert "FREE_TEXT" in prompt

    def test_identifier_in_coverage(self):
        col = _minimal_col_context("seq_num", "INTEGER", cardinality_hint="IDENTIFIER")
        prompt = self._build([col])
        assert "IDENTIFIER" in prompt

    def test_rule_12_cardinality_present(self):
        col = _minimal_col_context("status", "VARCHAR", cardinality_hint="CATEGORICAL_CLOSED",
                                   is_categorical=True, categorical_values=["A"])
        prompt = self._build([col])
        assert "12. CARDINALITY" in prompt
        assert "CATEGORICAL_CLOSED" in prompt

    def test_rule_12_references_flag_and_open(self):
        col = _minimal_col_context("status", "VARCHAR")
        prompt = self._build([col])
        assert "FLAG" in prompt
        assert "CATEGORICAL_OPEN" in prompt
