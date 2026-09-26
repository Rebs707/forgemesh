# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 6 tests — SynthesisStep and verify_synthesis."""
from __future__ import annotations

import pytest

from semantic_mapper.prove.synthesis import (
    RECOGNISED_OPERATIONS,
    SynthesisMismatch,
    SynthesisStep,
    verify_synthesis,
)


# ---------------------------------------------------------------------------
# Recognised operations — verified path
# ---------------------------------------------------------------------------

def test_subtraction_verified():
    step = verify_synthesis("subtraction", {"a": 100, "b": 37}, 63)
    assert step.operation_verified
    assert step.operation == "subtraction"


def test_subtraction_mismatch_raises():
    with pytest.raises(SynthesisMismatch):
        verify_synthesis("subtraction", {"a": 100, "b": 37}, 999)


def test_addition_verified():
    step = verify_synthesis("addition", {"a": 50, "b": 25}, 75)
    assert step.operation_verified


def test_addition_mismatch_raises():
    with pytest.raises(SynthesisMismatch):
        verify_synthesis("addition", {"a": 50, "b": 25}, 70)


def test_ratio_verified():
    step = verify_synthesis("ratio", {"numerator": 1, "denominator": 4}, 0.25)
    assert step.operation_verified


def test_ratio_mismatch_raises():
    with pytest.raises(SynthesisMismatch):
        verify_synthesis("ratio", {"numerator": 1, "denominator": 4}, 0.5)


def test_ratio_zero_denominator_raises():
    with pytest.raises(SynthesisMismatch, match="zero"):
        verify_synthesis("ratio", {"numerator": 1, "denominator": 0}, 0)


def test_percentage_change_verified():
    step = verify_synthesis(
        "percentage_change",
        {"current": 1247, "prior": 1180},
        (1247 - 1180) / 1180 * 100,
    )
    assert step.operation_verified


def test_percentage_change_mismatch_raises():
    with pytest.raises(SynthesisMismatch):
        verify_synthesis("percentage_change", {"current": 1247, "prior": 1180}, 50.0)


def test_percentage_change_zero_prior_raises():
    with pytest.raises(SynthesisMismatch, match="zero"):
        verify_synthesis("percentage_change", {"current": 10, "prior": 0}, 0)


def test_comparison_greater():
    step = verify_synthesis("comparison", {"a": 10, "b": 5}, "a > b")
    assert step.operation_verified
    assert step.output == "a > b"


def test_comparison_less():
    step = verify_synthesis("comparison", {"a": 3, "b": 7}, "a < b")
    assert step.operation_verified


def test_comparison_equal():
    step = verify_synthesis("comparison", {"a": 5, "b": 5}, "a == b")
    assert step.operation_verified


def test_comparison_mismatch_raises():
    with pytest.raises(SynthesisMismatch):
        verify_synthesis("comparison", {"a": 10, "b": 5}, "a < b")


def test_ranking_verified():
    step = verify_synthesis("ranking", {"items": [3, 1, 4, 1, 5]}, [5, 4, 3, 1, 1])
    assert step.operation_verified


def test_ranking_mismatch_raises():
    with pytest.raises(SynthesisMismatch):
        verify_synthesis("ranking", {"items": [3, 1, 4]}, [1, 3, 4])


def test_top_n_selection_verified():
    step = verify_synthesis("top_n_selection", {"items": [10, 20, 30, 40], "n": 2}, [10, 20])
    assert step.operation_verified


def test_top_n_selection_mismatch_raises():
    with pytest.raises(SynthesisMismatch):
        verify_synthesis("top_n_selection", {"items": [10, 20, 30], "n": 2}, [20, 30])


def test_aggregation_sum_verified():
    step = verify_synthesis(
        "aggregation_over_subqueries",
        {"subqueries": {"q1": 100, "q2": 200}, "agg": "sum"},
        300,
    )
    assert step.operation_verified


def test_aggregation_avg_verified():
    step = verify_synthesis(
        "aggregation_over_subqueries",
        {"subqueries": {"q1": 100, "q2": 200}, "agg": "avg"},
        150,
    )
    assert step.operation_verified


def test_aggregation_mismatch_raises():
    with pytest.raises(SynthesisMismatch):
        verify_synthesis(
            "aggregation_over_subqueries",
            {"subqueries": {"q1": 100, "q2": 200}, "agg": "sum"},
            999,
        )


def test_filter_and_count_returns_unverified():
    step = verify_synthesis(
        "filter_and_count",
        {"items": [1, 2, 3], "predicate_description": "age > 18"},
        2,
    )
    assert not step.operation_verified


# ---------------------------------------------------------------------------
# Free-form operation — unverified, no raise
# ---------------------------------------------------------------------------

def test_free_form_operation_returns_unverified():
    step = verify_synthesis("contextual_judgment", {"a": 1}, "yes", "agent said so")
    assert not step.operation_verified
    assert step.operation == "contextual_judgment"
    assert step.output == "yes"


def test_free_form_does_not_raise():
    step = verify_synthesis("completely_unknown_op", {}, None)
    assert isinstance(step, SynthesisStep)
    assert not step.operation_verified


# ---------------------------------------------------------------------------
# SynthesisStep dataclass
# ---------------------------------------------------------------------------

def test_synthesis_step_is_frozen():
    step = SynthesisStep(
        operation="addition",
        operation_verified=True,
        description="test",
        inputs_used={"a": 1, "b": 2},
        output=3,
    )
    with pytest.raises(Exception):
        step.operation = "subtraction"  # type: ignore[misc]


def test_recognised_operations_set():
    assert "percentage_change" in RECOGNISED_OPERATIONS
    assert "subtraction" in RECOGNISED_OPERATIONS
    assert len(RECOGNISED_OPERATIONS) == 9
