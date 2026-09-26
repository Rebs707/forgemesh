# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""TERM-1 — coded-column detection generalises by value overlap.

Assertions key on the detection RESULT (which source, what overlap), never on
table/column names, so the rule is the general one.
"""
from __future__ import annotations

from semantic_mapper.ontology.terminology.detection import column_values, detect_bindings
from semantic_mapper.ontology.terminology.sources import build_sources

from .builders import make_profile, offline_source_config


def _sources():
    # A code set of three valid codes.
    return build_sources(offline_source_config(codes={"A1": "", "B2": "", "C3": ""}))


class TestDetection:
    def test_overlapping_column_is_detected(self) -> None:
        # 3/3 distinct values are valid codes → overlap 1.0 ≥ threshold.
        profiles = [make_profile("raw_events", "status_code", ["A1", "B2", "C3"])]
        detected = detect_bindings(profiles, _sources(), overlap_threshold=0.8)
        assert len(detected) == 1
        assert detected[0].source_id == "testsys"
        assert detected[0].overlap == 1.0

    def test_partial_overlap_above_threshold_detected(self) -> None:
        # 3/4 valid → overlap 0.75; threshold 0.7 → detected, "Z9" is unmatched.
        profiles = [make_profile("raw_events", "status_code", ["A1", "B2", "C3", "Z9"])]
        detected = detect_bindings(profiles, _sources(), overlap_threshold=0.7)
        assert len(detected) == 1
        assert abs(detected[0].overlap - 0.75) < 1e-9
        assert detected[0].unmatched_values == ["Z9"]

    def test_partial_overlap_below_threshold_not_detected(self) -> None:
        profiles = [make_profile("raw_events", "status_code", ["A1", "B2", "C3", "Z9"])]
        detected = detect_bindings(profiles, _sources(), overlap_threshold=0.8)
        assert detected == []

    def test_column_matching_no_source_is_not_detected(self) -> None:
        # Values overlap nothing → binds to nothing.
        profiles = [make_profile("raw_events", "label", ["red", "green", "blue"])]
        detected = detect_bindings(profiles, _sources(), overlap_threshold=0.8)
        assert detected == []

    def test_single_value_column_ignored(self) -> None:
        # One distinct value coincidentally matching a code is not evidence.
        profiles = [make_profile("raw_events", "only", ["A1"])]
        detected = detect_bindings(profiles, _sources(), overlap_threshold=0.8)
        assert detected == []

    def test_no_sources_no_detection(self) -> None:
        profiles = [make_profile("raw_events", "status_code", ["A1", "B2", "C3"])]
        assert detect_bindings(profiles, [], overlap_threshold=0.8) == []

    def test_best_source_wins_on_multi_match(self) -> None:
        # Two sources both above threshold; the higher-overlap one is chosen.
        sources = build_sources({
            **offline_source_config("narrow", codes={"A1": "", "B2": ""}),
            **offline_source_config("wide", codes={"A1": "", "B2": "", "C3": ""}),
        })
        profiles = [make_profile("raw_events", "status_code", ["A1", "B2", "C3"])]
        detected = detect_bindings(profiles, sources, overlap_threshold=0.5)
        assert len(detected) == 1
        assert detected[0].source_id == "wide"     # 1.0 beats 0.667


class TestColumnValues:
    def test_prefers_top_values_then_dedups(self) -> None:
        from semantic_mapper.db.models import ColumnProfile
        cp = ColumnProfile(
            name="c", inferred_type="VARCHAR", is_nullable=True, is_primary_key=False,
            null_rate=0.0, distinct_count=2,
            top_values=[{"value": "A1", "count": 3}, {"value": "A1", "count": 1},
                        {"value": "B2", "count": 1}],
        )
        assert column_values(cp) == ["A1", "B2"]

    def test_falls_back_to_sample_data(self) -> None:
        from semantic_mapper.db.models import ColumnProfile
        cp = ColumnProfile(
            name="c", inferred_type="VARCHAR", is_nullable=True, is_primary_key=False,
            null_rate=0.0, distinct_count=2, top_values=[],
            sample_data=["X1", "X2", None, "X1"],
        )
        assert column_values(cp) == ["X1", "X2"]
