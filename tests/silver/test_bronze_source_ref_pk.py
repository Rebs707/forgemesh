# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""bronze_source_ref must key on the inspector-detected PRIMARY KEY (name-agnostic,
composite-aware), not a column literally named `id`. A non-unique business code such
as schedules.id would otherwise collapse rows onto one provenance key / subject URI.
"""
from __future__ import annotations

from semantic_mapper.silver.pipeline import SilverPipeline

_ref = SilverPipeline._build_bronze_source_ref


def test_uses_declared_pk_over_id_column():
    # row has BOTH a non-unique business `id` and the real PK `row_id`
    row = {"row_id": 123, "id": "ALPMDH----TRNO000100--", "status": "FINISHED"}
    assert _ref("raw_schedules", row, 0, ["row_id"]) == "raw_schedules.row_id=123"


def test_natural_key_pk_is_used():
    row = {"country_code": "NOR", "gold": 18, "country": "Norway"}
    assert _ref("raw_medals", row, 0, ["country_code"]) == "raw_medals.country_code=NOR"


def test_composite_pk():
    row = {"a": "X", "b": "Y", "v": 1}
    assert _ref("raw_t", row, 0, ["a", "b"]) == "raw_t.a+b=X|Y"


def test_falls_back_to_name_heuristic_when_no_pk():
    row = {"id": "P1", "name": "x"}
    assert _ref("raw_patient", row, 0, []) == "raw_patient.id=P1"


def test_falls_back_to_positional_index_when_nothing_present():
    row = {"some_col": "v"}
    assert _ref("raw_t", row, 7, []) == "raw_t._idx=7"


def test_pk_missing_from_row_falls_through():
    # defensive: declared PK not present in the row → fall back to heuristic
    row = {"id": "P1"}
    assert _ref("raw_t", row, 0, ["row_id"]) == "raw_t.id=P1"
