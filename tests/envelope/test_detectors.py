# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the general (dataset-agnostic) caveat detectors.

Every test uses generic table/column names — no detector may depend on a specific
dataset's schema (CONTRIBUTING.md).
"""
from __future__ import annotations

from semantic_mapper.envelope.detectors import (
    DetectorContext,
    caveats_from_proof,
    detect_small_groups,
    run_detectors,
)


def _ctx(**kw) -> DetectorContext:
    base = dict(executed_query="", mode="gold", columns=[], rows=[])
    base.update(kw)
    return DetectorContext(**base)


# ── small_group ───────────────────────────────────────────────────────────────

def test_small_group_flags_groups_below_threshold():
    ctx = _ctx(
        executed_query="SELECT category, COUNT(*) AS n FROM t GROUP BY category",
        columns=["category", "n"],
        rows=[{"category": "A", "n": 300}, {"category": "B", "n": 12}, {"category": "C", "n": 4}],
        small_group_threshold=20,
    )
    caveats = detect_small_groups(ctx)
    assert len(caveats) == 1
    assert caveats[0].kind == "small_group"
    assert caveats[0].count == 2  # 12 and 4 are below 20


def test_small_group_requires_group_by():
    # Same shape but no GROUP BY → not an aggregate, so never fires.
    ctx = _ctx(
        executed_query="SELECT id, score FROM t",
        columns=["id", "score"],
        rows=[{"id": 1, "score": 3}, {"id": 2, "score": 5}],
    )
    assert detect_small_groups(ctx) == []


def test_small_group_requires_aggregate_alias():
    # GROUP BY present but no COUNT/SUM/AVG alias in the SELECT → no measure column.
    ctx = _ctx(
        executed_query="SELECT year FROM t GROUP BY year",
        columns=["year"],
        rows=[{"year": 3}, {"year": 5}],  # small ints, but not a measure
    )
    assert detect_small_groups(ctx) == []


def test_small_group_sparql_alias():
    ctx = _ctx(
        executed_query="SELECT ?g (COUNT(?x) AS ?n) WHERE { ?x :g ?g } GROUP BY ?g",
        mode="sparql",
        columns=["g", "n"],
        rows=[{"g": "x", "n": 2}, {"g": "y", "n": 99}],
        small_group_threshold=20,
    )
    caveats = detect_small_groups(ctx)
    assert caveats and caveats[0].count == 1


# ── caveats_from_proof ────────────────────────────────────────────────────────

def test_proof_caveats_map_to_kinds():
    ctx = _ctx(proof_caveats=[
        {"type": "QUARANTINED", "count": 4},
        {"type": "NULL_EXCLUDED", "count": 2},
    ])
    kinds = {c.kind: c.count for c in caveats_from_proof(ctx)}
    assert kinds == {"quarantined": 4, "null_excluded": 2}


def test_broken_lineage_becomes_reference_gap():
    ctx = _ctx(broken_lineage_count=3)
    caveats = caveats_from_proof(ctx)
    assert any(c.kind == "reference_gap" and c.count == 3 for c in caveats)


def test_null_exclusion_refined_to_open_records_by_reason():
    ctx = _ctx(proof_caveats=[
        {"type": "NULL_EXCLUDED", "count": 23, "reason": "23 stays have no discharge date (still open)"},
    ])
    caveats = caveats_from_proof(ctx)
    assert any(c.kind == "open_records" and c.count == 23 for c in caveats)


def test_null_exclusion_refined_to_late_arrival_by_reason():
    ctx = _ctx(proof_caveats=[
        {"type": "NULL_EXCLUDED", "count": 4, "reason": "4 records arrived after the period boundary"},
    ])
    caveats = caveats_from_proof(ctx)
    assert any(c.kind == "late_arrival" and c.count == 4 for c in caveats)


# ── run_detectors composition ─────────────────────────────────────────────────

def test_run_detectors_merges_and_sorts():
    ctx = _ctx(
        executed_query="SELECT category, COUNT(*) AS n FROM t GROUP BY category",
        columns=["category", "n"],
        rows=[{"category": "A", "n": 2}],
        proof_caveats=[{"type": "QUARANTINED", "count": 1}],
        broken_lineage_count=1,
        small_group_threshold=20,
    )
    caveats = run_detectors(ctx)
    kinds = [c.kind for c in caveats]
    assert kinds == sorted(kinds)  # deterministic order
    assert set(kinds) == {"quarantined", "reference_gap", "small_group"}
    # one caveat per kind
    assert len(kinds) == len(set(kinds))
