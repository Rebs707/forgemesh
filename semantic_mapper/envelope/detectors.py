# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""General caveat detectors (FP-1).

The design's "first set" of caveat detectors, every one dataset-agnostic:

    small_group     — groups under an aggregate whose size is below a threshold
    reference_gap   — join keys that failed to match a reference (broken lineage)
    null_excluded   — rows dropped because a required value was null
    open_records    — records still open (a null end/close value on a live entity)
    late_arrival    — rows that arrived after a period boundary
    quarantined     — rows quarantined by the pipeline

Two of these (``small_group``, ``reference_gap``) are computed here from generally
available signals — the aggregate result shape and the pipeline's broken-lineage
count.  The remainder are *mapped* from caveats the pipeline already emits for any
schema (QUARANTINED, NULL_EXCLUDED, LINEAGE_BROKEN, ...); we never probe a
dataset-specific column to synthesise them, which would violate the no
dataset-specific-knowledge rule (CONTRIBUTING.md).

Every detector is a pure function over a ``DetectorContext`` and returns a list of
``Caveat``.  ``run_detectors`` composes them and de-duplicates by kind.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from semantic_mapper.envelope.models import Caveat

DEFAULT_SMALL_GROUP_THRESHOLD = 20

# Aggregate function → its output is a measure/count column we can size-check.
_AGG_ALIAS_RE = re.compile(
    r"\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\([^)]*\)\s+AS\s+[`\"']?\??(\w+)",
    re.IGNORECASE,
)
_HAS_GROUP_BY_RE = re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE)


@dataclass(frozen=True)
class DetectorContext:
    """Everything the detectors are allowed to see — no live DB access."""

    executed_query: str
    mode: str
    columns: List[str]
    rows: List[Dict[str, Any]]              # matched rows (may be a sample)
    proof_caveats: List[Dict[str, Any]] = field(default_factory=list)  # v1 caveat dicts
    broken_lineage_count: int = 0
    small_group_threshold: int = DEFAULT_SMALL_GROUP_THRESHOLD
    query_rewrites: List[str] = field(default_factory=list)  # notes from the query engine


# ── Detector: small groups under an aggregate ─────────────────────────────────

def _aggregate_measure_columns(query: str, columns: List[str]) -> List[str]:
    """Column names produced by an aggregate function in the query's SELECT.

    Fully general: parses ``COUNT(...) AS alias`` style clauses out of the executed
    query (works for both SQL and SPARQL ``(COUNT(...) AS ?n)``).  Returns only the
    aliases that also appear as result columns.
    """
    aliases = {m.group(1) for m in _AGG_ALIAS_RE.finditer(query or "")}
    return [c for c in columns if c in aliases]


def detect_small_groups(ctx: DetectorContext) -> List[Caveat]:
    """Flag aggregate groups whose measure is below the threshold.

    Only fires when the query is a genuine grouped aggregate (``GROUP BY`` present)
    and an aggregate measure column can be identified — so plain SELECTs and id
    columns never trip it.
    """
    if not ctx.rows or not _HAS_GROUP_BY_RE.search(ctx.executed_query or ""):
        return []
    measure_cols = _aggregate_measure_columns(ctx.executed_query, ctx.columns)
    if not measure_cols:
        return []
    threshold = ctx.small_group_threshold
    smallest: Optional[int] = None
    small = 0
    for row in ctx.rows:
        for col in measure_cols:
            val = row.get(col)
            if isinstance(val, bool):
                continue
            if isinstance(val, int) or (isinstance(val, float) and val.is_integer()):
                iv = int(val)
                if 0 <= iv < threshold:
                    small += 1
                    smallest = iv if smallest is None else min(smallest, iv)
            break  # one measure column per row is enough
    if small == 0:
        return []
    note = (
        f"{small} group{'s' if small != 1 else ''} contain fewer than "
        f"{threshold} rows (smallest: {smallest}); treat those averages/rates gently."
    )
    return [Caveat(kind="small_group", count=small, note=note, severity="warn")]


# ── Detector: the executed query is not the first one generated ───────────────

def detect_query_rewrites(ctx: DetectorContext) -> List[Caveat]:
    """Disclose any automatic rewrite of the generated query (e.g. regenerated after
    returning no rows), so the answer is never presented as the first attempt's."""
    if not ctx.query_rewrites:
        return []
    return [Caveat(
        kind="query_rewritten",
        count=len(ctx.query_rewrites),
        note=ctx.query_rewrites[0],
        severity="warn",
    )]


# ── Adapter: pipeline caveats → envelope caveats ──────────────────────────────

# Pipeline caveat type → (envelope kind, matcher on the reason text for refinement).
_PROOF_TYPE_MAP = {
    "QUARANTINED": "quarantined",
    "NULL_EXCLUDED": "null_excluded",
    "MISSING_VALUE": "null_excluded",
    "LINEAGE_BROKEN": "broken_lineage",
}

# Words in a caveat reason that mark a null-exclusion as an "open record"
# (a live entity missing an end/close value) rather than a generic null drop.
_OPEN_RECORD_HINTS = re.compile(
    r"\b(open|end|close|closed|discharge|finish|complete|null\s+end)\b", re.IGNORECASE
)
# Words that mark a caveat as late arrival relative to a period boundary.
_LATE_ARRIVAL_HINTS = re.compile(
    r"(\blate\b|back-?dated|arriv|after\b[\w\s]*\b(?:month|period|window|boundary)\b)",
    re.IGNORECASE,
)


def caveats_from_proof(ctx: DetectorContext) -> List[Caveat]:
    """Map the pipeline's own caveats (already dataset-agnostic) to envelope kinds."""
    out: List[Caveat] = []
    for c in ctx.proof_caveats or []:
        ctype = (c.get("type") or c.get("kind") or "").upper()
        count = c.get("count")
        count = int(count) if isinstance(count, int) else 0
        reason = c.get("reason") or c.get("note") or None
        kind = _PROOF_TYPE_MAP.get(ctype)
        if kind is None:
            continue
        # Refine null-exclusions into the more specific open_records / late_arrival
        # kinds when the pipeline's own reason text signals it — no column probing.
        if kind == "null_excluded" and reason:
            if _LATE_ARRIVAL_HINTS.search(reason):
                kind = "late_arrival"
            elif _OPEN_RECORD_HINTS.search(reason):
                kind = "open_records"
        out.append(Caveat(kind=kind, count=count, note=reason, severity="warn"))

    if ctx.broken_lineage_count > 0 and not any(
        cav.kind in ("broken_lineage", "reference_gap") for cav in out
    ):
        out.append(Caveat(
            kind="reference_gap",
            count=ctx.broken_lineage_count,
            note=(
                f"{ctx.broken_lineage_count} row"
                f"{'s' if ctx.broken_lineage_count != 1 else ''} could not be traced "
                "back to a source row (join key missing from the reference table)."
            ),
            severity="warn",
        ))
    return out


# ── Composition ───────────────────────────────────────────────────────────────

def run_detectors(ctx: DetectorContext) -> List[Caveat]:
    """Run all detectors and return the merged caveat list.

    Merges by kind: counts of the same kind are summed and the first note wins,
    so a UI can rely on at most one caveat per kind.
    """
    raw: List[Caveat] = []
    raw.extend(caveats_from_proof(ctx))
    raw.extend(detect_small_groups(ctx))
    raw.extend(detect_query_rewrites(ctx))

    merged: Dict[str, Caveat] = {}
    for cav in raw:
        existing = merged.get(cav.kind)
        if existing is None:
            merged[cav.kind] = cav
        else:
            merged[cav.kind] = Caveat(
                kind=cav.kind,
                count=existing.count + cav.count,
                note=existing.note or cav.note,
                severity=existing.severity,
            )
    # Stable, deterministic order for rendering.
    return sorted(merged.values(), key=lambda c: c.kind)
