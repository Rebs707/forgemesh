# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Coded-column detection by value-set overlap.

For each column, the fraction of its distinct non-null values that are valid
codes/labels in a configured terminology source is computed.  Overlap ≥ the
threshold ⇒ the column is bindable to that source.  Detection keys ONLY on value
overlap — never on column names (``icd9``, ``code``) or dataset identity.  A
column whose values match no source produces no DetectedColumn.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Sequence

from semantic_mapper.ontology.iterative.categorical_individuals import (
    _is_null_like,
    _looks_packed,
)

from .models import DetectedColumn
from .sources.base import TerminologySource

logger = logging.getLogger(__name__)

# A column needs at least this many distinct values to be considered — a single
# value coincidentally matching a code is not evidence of a coded column.
_MIN_DISTINCT = 2


def column_values(col_profile) -> List[str]:
    """Distinct non-null, non-packed values for a column, as stripped strings.

    Prefers ``top_values`` (populated for low-cardinality columns — the exact set
    the gen-LLM saw), then ``distinct_sample`` (up to ~200 distinct values, for
    higher-cardinality coded columns), then ``sample_data`` (first-N rows, which may
    repeat). Order-preserving; deduped case-sensitively (codes are case-sensitive).
    """
    raw: List = []
    top_values = getattr(col_profile, "top_values", None) or []
    distinct_sample = getattr(col_profile, "distinct_sample", None) or []
    if top_values:
        for item in top_values:
            v = item.get("value") if isinstance(item, dict) else item
            if v is not None:
                raw.append(v)
    else:
        for v in (distinct_sample or getattr(col_profile, "sample_data", None) or []):
            if v is not None:
                raw.append(v)

    seen = set()
    out: List[str] = []
    for v in raw:
        s = str(v).strip()
        if not s or _is_null_like(v) or _looks_packed(s):
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def detect_bindings(
    profiles: Sequence,
    sources: Sequence[TerminologySource],
    overlap_threshold: float,
    *,
    min_distinct: int = _MIN_DISTINCT,
) -> List[DetectedColumn]:
    """Return one :class:`DetectedColumn` per column that binds to its best source.

    A column is tested against every source; the source with the highest overlap
    at or above *overlap_threshold* wins (ties broken by more matched values, then
    ``source_id`` for determinism).  Columns matching no source are omitted.
    """
    if not sources:
        return []

    detected: List[DetectedColumn] = []
    for table in profiles:
        table_name = getattr(table, "table_name", None)
        columns = getattr(table, "columns", {}) or {}
        for col_name, col_profile in columns.items():
            values = column_values(col_profile)
            if len(values) < min_distinct:
                continue

            best: Optional[DetectedColumn] = None
            for source in sources:
                matched = [v for v in values if source.contains(v)]
                if not matched:
                    continue
                overlap = len(matched) / len(values)
                if overlap < overlap_threshold:
                    continue
                unmatched = [v for v in values if v not in set(matched)]
                cand = DetectedColumn(
                    table=table_name,
                    column=col_name,
                    source_id=source.source_id,
                    system_label=source.system_label,
                    overlap=overlap,
                    n_distinct=len(values),
                    matched_values=matched,
                    unmatched_values=unmatched,
                )
                if best is None or (cand.overlap, len(cand.matched_values), source.source_id) > (
                    best.overlap, len(best.matched_values), best.source_id
                ):
                    best = cand
            if best is not None:
                detected.append(best)
                logger.info(
                    "Terminology: %s.%s → %s (overlap=%.2f, %d/%d values)",
                    best.table, best.column, best.system_label,
                    best.overlap, len(best.matched_values), best.n_distinct,
                )
    return detected
