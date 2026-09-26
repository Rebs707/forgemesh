# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Pipeline state machine (issue #47).

Tracks the lifecycle of a pipeline run and derives the current state
from a set of timestamps persisted in ``app_state.json``.

States
------
CLEAN
    No mapping report, empty corrections store.  Fresh install or after
    corrections are cleared.

MAPPED
    Mapping report exists and corrections are populated, but no
    materialisation phase (silver/gold/RDF) has been run yet.

STALE
    A correction was added or deleted *after* the last successful map run,
    so the mapping report on disk does not reflect the current corrections.
    Re-running the map phase will resolve this.

PARTIALLY_MATERIALISED
    The map phase has run and at least one materialisation phase has
    completed, but not all three (silver, gold, rdf).

DIVERGED
    The mapping report was re-generated *after* the last materialisation run
    (i.e. the map was re-run but materialisation was not).  Data in
    silver/gold/RDF is stale relative to the new mapping.

MATERIALISED
    All three materialisation phases have been run, all completed *after*
    the last map run, and no corrections have been added since.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Optional


class PipelineState(str, Enum):
    CLEAN                 = "CLEAN"
    MAPPED                = "MAPPED"
    STALE                 = "STALE"
    PARTIALLY_MATERIALISED = "PARTIALLY_MATERIALISED"
    DIVERGED              = "DIVERGED"
    MATERIALISED          = "MATERIALISED"


# Human-readable label and CSS-like severity for each state
_STATE_META: Dict[str, Dict[str, str]] = {
    PipelineState.CLEAN:                  {"label": "Clean",                 "level": "info"},
    PipelineState.MAPPED:                 {"label": "Mapped",                "level": "ok"},
    PipelineState.STALE:                  {"label": "Stale — re-map needed", "level": "warn"},
    PipelineState.PARTIALLY_MATERIALISED: {"label": "Partially materialised","level": "warn"},
    PipelineState.DIVERGED:               {"label": "Diverged — re-materialise needed", "level": "warn"},
    PipelineState.MATERIALISED:           {"label": "Materialised",          "level": "ok"},
}

# Materialisation phases in required order
_MAT_PHASES = ("silver", "gold", "rdf")


def _ts(iso: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 string to an aware datetime, or return None."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def derive_state(
    *,
    mapping_report_exists: bool,
    corrections_count: int,
    last_map_at: Optional[str] = None,
    last_correction_at: Optional[str] = None,
    last_silver_at: Optional[str] = None,
    last_gold_at: Optional[str] = None,
    last_rdf_at: Optional[str] = None,
) -> PipelineState:
    """Derive the current ``PipelineState`` from pipeline timestamps.

    All ``*_at`` parameters are ISO-8601 strings (or None if the event has
    never occurred).

    Parameters
    ----------
    mapping_report_exists:
        True when the mapping report JSON file is present on disk.
    corrections_count:
        Number of rows currently in the corrections store.
    last_map_at:
        Timestamp of the last successful mapping (map) phase.
    last_correction_at:
        Timestamp of the most recent correction create/update/delete.
    last_silver_at, last_gold_at, last_rdf_at:
        Timestamps of the last successful materialisation runs per phase.
    """
    ts_map        = _ts(last_map_at)
    ts_correction = _ts(last_correction_at)
    ts_silver     = _ts(last_silver_at)
    ts_gold       = _ts(last_gold_at)
    ts_rdf        = _ts(last_rdf_at)

    # ── CLEAN ────────────────────────────────────────────────────────────────
    if not mapping_report_exists and corrections_count == 0 and ts_map is None:
        return PipelineState.CLEAN

    # ── STALE: corrections newer than last map run ────────────────────────────
    if ts_map is not None and ts_correction is not None and ts_correction > ts_map:
        return PipelineState.STALE

    # ── No materialisation at all ─────────────────────────────────────────────
    mat_timestamps = [t for t in (ts_silver, ts_gold, ts_rdf) if t is not None]
    if not mat_timestamps:
        if mapping_report_exists or ts_map is not None:
            return PipelineState.MAPPED
        return PipelineState.CLEAN

    # ── DIVERGED: map was re-run after last materialisation ───────────────────
    last_mat = max(mat_timestamps)
    if ts_map is not None and ts_map > last_mat:
        return PipelineState.DIVERGED

    # ── PARTIALLY_MATERIALISED vs MATERIALISED ────────────────────────────────
    if len(mat_timestamps) < len(_MAT_PHASES):
        return PipelineState.PARTIALLY_MATERIALISED

    return PipelineState.MATERIALISED


def state_meta(state: PipelineState) -> Dict[str, str]:
    """Return ``{"label": ..., "level": ...}`` for the given state."""
    return _STATE_META.get(state, {"label": str(state), "level": "info"})
