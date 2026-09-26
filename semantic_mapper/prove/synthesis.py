# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 6 — synthesis step model and verification.

verify_synthesis() recomputes the claimed output from inputs_used for every
recognised operation.  Free-form operations set operation_verified=False without
raising.  Mismatches on recognised operations raise SynthesisMismatch.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Set

RECOGNISED_OPERATIONS: Set[str] = {
    "subtraction",
    "addition",
    "ratio",
    "percentage_change",
    "comparison",
    "ranking",
    "top_n_selection",
    "aggregation_over_subqueries",
    "filter_and_count",
}

_FLOAT_TOL = 1e-6


class SynthesisMismatch(Exception):
    pass


@dataclass(frozen=True)
class SynthesisStep:
    operation: str
    operation_verified: bool
    description: str
    inputs_used: Dict[str, Any]
    output: Any


def verify_synthesis(
    operation: str,
    inputs_used: Dict[str, Any],
    claimed_output: Any,
    description: str = "",
) -> SynthesisStep:
    """Verify claimed_output for operation applied to inputs_used.

    Recognised operations are recomputed and verified.  Free-form operations
    return operation_verified=False without raising.

    Raises SynthesisMismatch when a recognised operation's recomputed value
    differs from claimed_output beyond float tolerance.
    """
    if operation not in RECOGNISED_OPERATIONS:
        return SynthesisStep(
            operation=operation,
            operation_verified=False,
            description=description,
            inputs_used=inputs_used,
            output=claimed_output,
        )

    # filter_and_count uses a free-form predicate that cannot be recomputed.
    if operation == "filter_and_count":
        return SynthesisStep(
            operation=operation,
            operation_verified=False,
            description=description,
            inputs_used=inputs_used,
            output=claimed_output,
        )

    recomputed = _recompute(operation, inputs_used, claimed_output)

    return SynthesisStep(
        operation=operation,
        operation_verified=True,
        description=description,
        inputs_used=inputs_used,
        output=recomputed,
    )


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= _FLOAT_TOL


def _recompute(operation: str, inputs: Dict[str, Any], claimed: Any) -> Any:
    if operation == "subtraction":
        result = float(inputs["a"]) - float(inputs["b"])
        if not _close(result, float(claimed)):
            raise SynthesisMismatch(
                f"subtraction: {inputs['a']} - {inputs['b']} = {result}, "
                f"claimed {claimed}"
            )
        return result

    if operation == "addition":
        result = float(inputs["a"]) + float(inputs["b"])
        if not _close(result, float(claimed)):
            raise SynthesisMismatch(
                f"addition: {inputs['a']} + {inputs['b']} = {result}, "
                f"claimed {claimed}"
            )
        return result

    if operation == "ratio":
        d = float(inputs["denominator"])
        if d == 0:
            raise SynthesisMismatch("ratio: denominator is zero")
        result = float(inputs["numerator"]) / d
        if not _close(result, float(claimed)):
            raise SynthesisMismatch(
                f"ratio: {inputs['numerator']} / {inputs['denominator']} = {result}, "
                f"claimed {claimed}"
            )
        return result

    if operation == "percentage_change":
        prior = float(inputs["prior"])
        if prior == 0:
            raise SynthesisMismatch("percentage_change: prior is zero")
        result = (float(inputs["current"]) - prior) / prior * 100
        if not _close(result, float(claimed)):
            raise SynthesisMismatch(
                f"percentage_change: ({inputs['current']} - {inputs['prior']}) "
                f"/ {inputs['prior']} * 100 = {result}, claimed {claimed}"
            )
        return result

    if operation == "comparison":
        a, b = float(inputs["a"]), float(inputs["b"])
        if a > b:
            expected = "a > b"
        elif a < b:
            expected = "a < b"
        else:
            expected = "a == b"
        if str(claimed) != expected:
            raise SynthesisMismatch(
                f"comparison: {a} vs {b} → {expected}, claimed {claimed!r}"
            )
        return expected

    if operation == "ranking":
        items = list(inputs["items"])
        result = sorted(items, reverse=True)
        if list(claimed) != result:
            raise SynthesisMismatch(
                f"ranking: expected descending {result}, claimed {claimed}"
            )
        return result

    if operation == "top_n_selection":
        items = list(inputs["items"])
        n = int(inputs["n"])
        result = items[:n]
        if list(claimed) != result:
            raise SynthesisMismatch(
                f"top_n_selection: first {n} of {items} = {result}, claimed {claimed}"
            )
        return result

    if operation == "aggregation_over_subqueries":
        values = list(inputs["subqueries"].values())
        agg = inputs.get("agg", "sum").lower()
        if agg == "sum":
            result = sum(float(v) for v in values)
        elif agg == "avg":
            result = sum(float(v) for v in values) / len(values) if values else 0.0
        elif agg == "max":
            result = max(float(v) for v in values) if values else 0.0
        elif agg == "min":
            result = min(float(v) for v in values) if values else 0.0
        else:
            raise SynthesisMismatch(f"aggregation_over_subqueries: unknown agg={agg!r}")
        if not _close(result, float(claimed)):
            raise SynthesisMismatch(
                f"aggregation_over_subqueries({agg}): {values} → {result}, "
                f"claimed {claimed}"
            )
        return result

    raise SynthesisMismatch(f"Unhandled operation: {operation!r}")
