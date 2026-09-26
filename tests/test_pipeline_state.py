# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for the pipeline state machine — issue #47."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from semantic_mapper.pipeline_state import PipelineState, derive_state


def _ts(offset_seconds: int = 0) -> str:
    """Return an ISO-8601 UTC timestamp offset by *offset_seconds* from now."""
    dt = datetime(2026, 4, 3, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=offset_seconds)
    return dt.isoformat()


# Convenience aliases
T0  = _ts(0)    # base time
T1  = _ts(10)   # slightly later
T2  = _ts(20)
T3  = _ts(30)
T4  = _ts(40)
T5  = _ts(50)


# ── CLEAN ─────────────────────────────────────────────────────────────────────

class TestCleanState:
    def test_nothing_exists(self):
        assert derive_state(
            mapping_report_exists=False,
            corrections_count=0,
        ) == PipelineState.CLEAN

    def test_no_map_run_no_report_no_corrections(self):
        assert derive_state(
            mapping_report_exists=False,
            corrections_count=0,
            last_map_at=None,
        ) == PipelineState.CLEAN

    def test_report_present_overrides_clean(self):
        # If a report exists, we're not CLEAN even without corrections
        state = derive_state(
            mapping_report_exists=True,
            corrections_count=0,
            last_map_at=T0,
        )
        assert state != PipelineState.CLEAN


# ── MAPPED ────────────────────────────────────────────────────────────────────

class TestMappedState:
    def test_map_run_no_materialisation(self):
        assert derive_state(
            mapping_report_exists=True,
            corrections_count=5,
            last_map_at=T0,
        ) == PipelineState.MAPPED

    def test_corrections_exist_no_materialisation(self):
        assert derive_state(
            mapping_report_exists=True,
            corrections_count=10,
            last_map_at=T0,
            last_correction_at=T0,   # same time as map — not stale
        ) == PipelineState.MAPPED


# ── STALE ─────────────────────────────────────────────────────────────────────

class TestStaleState:
    def test_correction_after_map(self):
        assert derive_state(
            mapping_report_exists=True,
            corrections_count=3,
            last_map_at=T0,
            last_correction_at=T1,   # T1 > T0 → STALE
        ) == PipelineState.STALE

    def test_stale_even_with_materialisation(self):
        """Corrections after the last map make the state STALE regardless of materialisation."""
        assert derive_state(
            mapping_report_exists=True,
            corrections_count=3,
            last_map_at=T0,
            last_correction_at=T1,
            last_silver_at=T2,
            last_gold_at=T3,
            last_rdf_at=T4,
        ) == PipelineState.STALE

    def test_correction_equal_to_map_not_stale(self):
        """Exactly equal timestamps are NOT stale (correction > map required)."""
        state = derive_state(
            mapping_report_exists=True,
            corrections_count=3,
            last_map_at=T0,
            last_correction_at=T0,
        )
        assert state != PipelineState.STALE

    def test_correction_before_map_not_stale(self):
        """Correction before the map run — report is up-to-date."""
        state = derive_state(
            mapping_report_exists=True,
            corrections_count=3,
            last_map_at=T1,
            last_correction_at=T0,
        )
        assert state != PipelineState.STALE


# ── PARTIALLY_MATERIALISED ────────────────────────────────────────────────────

class TestPartiallyMaterialised:
    def test_only_silver_run(self):
        assert derive_state(
            mapping_report_exists=True,
            corrections_count=3,
            last_map_at=T0,
            last_silver_at=T1,
        ) == PipelineState.PARTIALLY_MATERIALISED

    def test_silver_and_gold_but_not_rdf(self):
        assert derive_state(
            mapping_report_exists=True,
            corrections_count=3,
            last_map_at=T0,
            last_silver_at=T1,
            last_gold_at=T2,
        ) == PipelineState.PARTIALLY_MATERIALISED

    def test_only_rdf_run_partial(self):
        assert derive_state(
            mapping_report_exists=True,
            corrections_count=3,
            last_map_at=T0,
            last_rdf_at=T1,
        ) == PipelineState.PARTIALLY_MATERIALISED


# ── DIVERGED ──────────────────────────────────────────────────────────────────

class TestDiverged:
    def test_map_after_full_materialisation(self):
        """Re-running map after all phases were materialised → DIVERGED."""
        assert derive_state(
            mapping_report_exists=True,
            corrections_count=5,
            last_map_at=T4,         # map re-run at T4
            last_silver_at=T1,
            last_gold_at=T2,
            last_rdf_at=T3,
        ) == PipelineState.DIVERGED

    def test_map_after_partial_materialisation(self):
        """Map newer than the most recent materialisation phase → DIVERGED."""
        assert derive_state(
            mapping_report_exists=True,
            corrections_count=5,
            last_map_at=T3,
            last_silver_at=T1,
            last_gold_at=T2,
        ) == PipelineState.DIVERGED

    def test_not_diverged_when_materialisation_after_map(self):
        state = derive_state(
            mapping_report_exists=True,
            corrections_count=5,
            last_map_at=T0,
            last_silver_at=T1,
            last_gold_at=T2,
            last_rdf_at=T3,
        )
        assert state != PipelineState.DIVERGED


# ── MATERIALISED ──────────────────────────────────────────────────────────────

class TestMaterialised:
    def test_all_phases_after_map(self):
        assert derive_state(
            mapping_report_exists=True,
            corrections_count=5,
            last_map_at=T0,
            last_silver_at=T1,
            last_gold_at=T2,
            last_rdf_at=T3,
        ) == PipelineState.MATERIALISED

    def test_all_phases_at_same_time_as_map(self):
        assert derive_state(
            mapping_report_exists=True,
            corrections_count=5,
            last_map_at=T0,
            last_silver_at=T0,
            last_gold_at=T0,
            last_rdf_at=T0,
        ) == PipelineState.MATERIALISED

    def test_corrections_before_map_stays_materialised(self):
        """Corrections made before the last map run don't stale the state."""
        assert derive_state(
            mapping_report_exists=True,
            corrections_count=5,
            last_correction_at=T0,  # before T1 map
            last_map_at=T1,
            last_silver_at=T2,
            last_gold_at=T3,
            last_rdf_at=T4,
        ) == PipelineState.MATERIALISED


# ── State transitions — happy-path sequence ───────────────────────────────────

class TestStateTransitionSequence:
    """Walk through a realistic pipeline lifecycle."""

    def test_full_lifecycle(self):
        # 1. Fresh install → CLEAN
        assert derive_state(
            mapping_report_exists=False, corrections_count=0
        ) == PipelineState.CLEAN

        # 2. Map phase runs → MAPPED
        assert derive_state(
            mapping_report_exists=True, corrections_count=5,
            last_map_at=T0,
        ) == PipelineState.MAPPED

        # 3. Silver runs → PARTIALLY_MATERIALISED
        assert derive_state(
            mapping_report_exists=True, corrections_count=5,
            last_map_at=T0, last_silver_at=T1,
        ) == PipelineState.PARTIALLY_MATERIALISED

        # 4. Gold + RDF run → MATERIALISED
        assert derive_state(
            mapping_report_exists=True, corrections_count=5,
            last_map_at=T0, last_silver_at=T1, last_gold_at=T2, last_rdf_at=T3,
        ) == PipelineState.MATERIALISED

        # 5. User adds a correction → STALE
        assert derive_state(
            mapping_report_exists=True, corrections_count=6,
            last_map_at=T0, last_correction_at=T4,
            last_silver_at=T1, last_gold_at=T2, last_rdf_at=T3,
        ) == PipelineState.STALE

        # 6. Re-run map (clears stale) → DIVERGED
        assert derive_state(
            mapping_report_exists=True, corrections_count=6,
            last_map_at=T5, last_correction_at=T4,
            last_silver_at=T1, last_gold_at=T2, last_rdf_at=T3,
        ) == PipelineState.DIVERGED

        # 7. Re-materialise → MATERIALISED again
        assert derive_state(
            mapping_report_exists=True, corrections_count=6,
            last_map_at=T5, last_correction_at=T4,
            last_silver_at=_ts(60), last_gold_at=_ts(70), last_rdf_at=_ts(80),
        ) == PipelineState.MATERIALISED
