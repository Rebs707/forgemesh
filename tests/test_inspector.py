# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for MySQLInspector — issue #44 (smarter categorical column detection)
                          — issue #70 (heuristic PK/FK inference).
"""
from __future__ import annotations

import pytest

from semantic_mapper.config.settings import Settings
from semantic_mapper.db.inspector import (
    MySQLInspector,
    _is_low_cardinality, _infer_pk, _infer_fks,
    _build_pk_name_map, _ambiguous_pk_fks,
    _build_natural_key_map, _looks_free_text, _prune_redundant_descriptive_fks,
    _is_text_type, _value_overlap_targets,
)
from semantic_mapper.db.models import ColumnProfile, TableProfile


@pytest.fixture()
def settings():
    return Settings(
        DATABASE_URL="mysql+pymysql://user:pass@localhost/test_db",
        OPENAI_API_KEY="test-key",
        LOW_CARDINALITY_THRESHOLD=20,
        LOW_CARDINALITY_RATIO=0.05,
    )


class TestIsLowCardinality:
    # ── Positive cases — should be low-cardinality ────────────────────────────

    def test_clearly_categorical(self, settings):
        # 5 distinct values in 1000 rows (0.5% ratio) → categorical
        assert _is_low_cardinality("status", 5, 1000, False, settings) is True

    def test_boundary_threshold(self, settings):
        # Exactly at threshold and ratio
        assert _is_low_cardinality("category", 20, 1000, False, settings) is True

    def test_zero_row_count_no_crash(self, settings):
        # Edge case: 0 rows → ratio check skipped
        assert _is_low_cardinality("status", 0, 0, False, settings) is True

    def test_high_row_count_low_distinct(self, settings):
        # 10 distinct in 1,000,000 rows → definitely categorical
        assert _is_low_cardinality("party_type", 10, 1_000_000, False, settings) is True

    # ── Negative cases — should NOT be low-cardinality ───────────────────────

    def test_primary_key_excluded(self, settings):
        assert _is_low_cardinality("id", 5, 1000, True, settings) is False

    def test_id_suffix_excluded(self, settings):
        assert _is_low_cardinality("customer_id", 5, 1000, False, settings) is False

    def test_key_suffix_excluded(self, settings):
        assert _is_low_cardinality("account_key", 5, 1000, False, settings) is False

    def test_ref_suffix_excluded(self, settings):
        assert _is_low_cardinality("policy_ref", 5, 1000, False, settings) is False

    def test_above_absolute_threshold(self, settings):
        # 25 distinct > LOW_CARDINALITY_THRESHOLD=20 → not categorical
        assert _is_low_cardinality("type", 25, 10_000, False, settings) is False

    def test_high_ratio_small_table(self, settings):
        # Small table (<500 rows) — ratio check skipped, so 18/100 is still categorical
        assert _is_low_cardinality("some_col", 18, 100, False, settings) is True

    def test_ratio_just_over_threshold(self, settings):
        # Large table (>=500 rows): 11/500 = 0.022 → categorical (under ratio)
        assert _is_low_cardinality("region", 11, 500, False, settings) is True

    def test_ratio_over_threshold_large_table(self, settings):
        # Large table: 55/500 = 0.11 > 0.05 → not categorical
        assert _is_low_cardinality("region", 55, 500, False, settings) is False

    def test_ratio_just_under_threshold(self, settings):
        # Large table: 10/500 = 0.02 < 0.05 ratio, and 10 <= 20 threshold → categorical
        assert _is_low_cardinality("region", 10, 500, False, settings) is True


# ── Issue #70: heuristic PK inference ─────────────────────────────────────────

class TestInferPk:
    def test_id_column_fully_unique(self):
        assert _infer_pk("id", distinct_count=1000, row_count=1000, null_rate=0.0) is True

    def test_uuid_column_fully_unique(self):
        assert _infer_pk("uuid", distinct_count=500, row_count=500, null_rate=0.0) is True

    def test_guid_column_fully_unique(self):
        assert _infer_pk("guid", distinct_count=200, row_count=200, null_rate=0.0) is True

    def test_case_insensitive_id(self):
        assert _infer_pk("Id", distinct_count=100, row_count=100, null_rate=0.0) is True
        assert _infer_pk("ID", distinct_count=100, row_count=100, null_rate=0.0) is True

    def test_not_unique_not_pk(self):
        # 900 distinct in 1000 rows → not 100% unique
        assert _infer_pk("id", distinct_count=900, row_count=1000, null_rate=0.0) is False

    def test_has_nulls_not_pk(self):
        assert _infer_pk("id", distinct_count=1000, row_count=1000, null_rate=0.01) is False

    def test_non_id_name_not_pk(self):
        assert _infer_pk("patient", distinct_count=1000, row_count=1000, null_rate=0.0) is False
        assert _infer_pk("encounter_id", distinct_count=500, row_count=500, null_rate=0.0) is False

    def test_zero_rows_not_pk(self):
        assert _infer_pk("id", distinct_count=0, row_count=0, null_rate=0.0) is False


# ── Issue #70: heuristic FK inference ─────────────────────────────────────────

def _col(name, pk=False):
    return ColumnProfile(
        name=name, inferred_type="VARCHAR(36)", is_nullable=False,
        is_primary_key=pk, null_rate=0.0, distinct_count=500,
    )


def _table(name, col_names, pk_col=None, foreign_keys=None):
    return TableProfile(
        table_name=name,
        row_count=1000,
        columns={c: _col(c, pk=(c == pk_col)) for c in col_names},
        foreign_keys=foreign_keys or [],
    )


class TestInferFks:
    def test_patient_col_links_to_patients_table(self):
        profiles = [
            _table("raw_patients",  ["id"], pk_col="id"),
            _table("raw_encounters", ["id", "patient", "start"], pk_col="id"),
        ]
        result = _infer_fks(profiles)
        enc_fks = result.get("raw_encounters", [])
        assert any(f["column"] == "patient" and f["ref_table"] == "raw_patients" for f in enc_fks)

    def test_patient_id_suffix_links_to_patients(self):
        profiles = [
            _table("raw_patients",  ["id"], pk_col="id"),
            _table("raw_encounters", ["id", "patient_id"], pk_col="id"),
        ]
        result = _infer_fks(profiles)
        enc_fks = result.get("raw_encounters", [])
        assert any(f["column"] == "patient_id" for f in enc_fks)

    def test_no_self_reference(self):
        """A table's own base name should not generate a self-referencing FK."""
        profiles = [_table("raw_patients", ["id", "patient"], pk_col="id")]
        result = _infer_fks(profiles)
        pat_fks = result.get("raw_patients", [])
        assert not any(f["ref_table"] == "raw_patients" for f in pat_fks)

    def test_explicit_fk_not_duplicated(self):
        """A column already listed as an explicit FK is not added again by _infer_fks."""
        profiles = [
            _table("raw_patients", ["id"], pk_col="id"),
            _table("raw_encounters", ["id", "patient"], pk_col="id",
                   foreign_keys=[{"column": "patient", "ref_table": "raw_patients"}]),
        ]
        result = _infer_fks(profiles)
        # _infer_fks skips columns already in explicit_fk_cols, so no duplication
        enc_fks = result.get("raw_encounters", [])
        assert not any(f["column"] == "patient" for f in enc_fks)

    def test_pk_cols_not_inferred_as_fk(self):
        """PK columns are never inferred as FKs."""
        profiles = [
            _table("raw_patients",  ["id"], pk_col="id"),
            _table("raw_encounters", ["id", "patient"], pk_col="patient"),
        ]
        result = _infer_fks(profiles)
        enc_fks = result.get("raw_encounters", [])
        assert not any(f["column"] == "patient" for f in enc_fks)

    def test_inferred_flag_set(self):
        profiles = [
            _table("raw_patients",  ["id"], pk_col="id"),
            _table("raw_encounters", ["id", "patient"], pk_col="id"),
        ]
        result = _infer_fks(profiles)
        enc_fks = result.get("raw_encounters", [])
        patient_fk = next((f for f in enc_fks if f["column"] == "patient"), None)
        assert patient_fk is not None
        assert patient_fk.get("inferred") is True

    def test_no_match_returns_empty(self):
        profiles = [
            _table("raw_patients",  ["id", "name"], pk_col="id"),
        ]
        result = _infer_fks(profiles)
        assert result.get("raw_patients", []) == []


def _unique_col(name, pk=False):
    """Column that qualifies as a natural key (≥95% unique, 0% nulls)."""
    return ColumnProfile(
        name=name, inferred_type="INT", is_nullable=False,
        is_primary_key=pk, null_rate=0.0, distinct_count=1000,
    )


def _nonunique_col(name):
    """Column that is clearly non-unique (FK side)."""
    return ColumnProfile(
        name=name, inferred_type="INT", is_nullable=False,
        is_primary_key=False, null_rate=0.0, distinct_count=50,
    )


def _table_custom(name, cols: dict, foreign_keys=None):
    return TableProfile(
        table_name=name,
        row_count=1000,
        columns=cols,
        foreign_keys=foreign_keys or [],
    )


class TestNaturalKeyInference:
    """Natural-key strategy: column near-unique in table B → FK in table A (issue #70)."""

    def test_mimic_subject_id(self):
        """subject_id near-unique in patients → FK in admissions (MIMIC pattern)."""
        profiles = [
            _table_custom("raw_patients", {
                "row_id":     _unique_col("row_id", pk=True),
                "subject_id": _unique_col("subject_id"),
            }),
            _table_custom("raw_admissions", {
                "row_id":     _unique_col("row_id", pk=True),
                "hadm_id":    _unique_col("hadm_id"),
                "subject_id": _nonunique_col("subject_id"),
            }),
        ]
        result = _infer_fks(profiles)
        adm_fks = result.get("raw_admissions", [])
        assert any(f["column"] == "subject_id" and f["ref_table"] == "raw_patients"
                   for f in adm_fks)

    def test_mimic_hadm_id(self):
        """hadm_id near-unique in admissions → FK in chartevents."""
        profiles = [
            _table_custom("raw_admissions", {
                "row_id":  _unique_col("row_id", pk=True),
                "hadm_id": _unique_col("hadm_id"),
            }),
            _table_custom("raw_chartevents", {
                "row_id":  _unique_col("row_id", pk=True),
                "hadm_id": _nonunique_col("hadm_id"),
            }),
        ]
        result = _infer_fks(profiles)
        fks = result.get("raw_chartevents", [])
        assert any(f["column"] == "hadm_id" and f["ref_table"] == "raw_admissions"
                   for f in fks)

    def test_near_unique_both_sides_not_inferred(self):
        """If a column is near-unique in BOTH tables it is not inferred as a FK."""
        profiles = [
            _table_custom("raw_a", {"id": _unique_col("id", pk=True), "code": _unique_col("code")}),
            _table_custom("raw_b", {"id": _unique_col("id", pk=True), "code": _unique_col("code")}),
        ]
        result = _infer_fks(profiles)
        # Neither side should point to the other via natural key
        a_fks = [f for f in result.get("raw_a", []) if f["column"] == "code"]
        b_fks = [f for f in result.get("raw_b", []) if f["column"] == "code"]
        assert not a_fks
        assert not b_fks

    def test_natural_key_inferred_flag(self):
        """Natural-key inferred FKs carry inferred=True."""
        profiles = [
            _table_custom("raw_patients", {
                "row_id":     _unique_col("row_id", pk=True),
                "subject_id": _unique_col("subject_id"),
            }),
            _table_custom("raw_admissions", {
                "row_id":     _unique_col("row_id", pk=True),
                "subject_id": _nonunique_col("subject_id"),
            }),
        ]
        result = _infer_fks(profiles)
        fk = next((f for f in result.get("raw_admissions", [])
                   if f["column"] == "subject_id"), None)
        assert fk is not None
        assert fk.get("inferred") is True


# ── Strategy 3: PK-name match ─────────────────────────────────────────────────

def _pk_col(name):
    """Column declared as PK (unique, no nulls)."""
    return ColumnProfile(
        name=name, inferred_type="INT", is_nullable=False,
        is_primary_key=True, null_rate=0.0, distinct_count=100,
    )


class TestPkNameMatch:
    """Strategy 3: bare identifier whose name matches a PK in exactly one other table."""

    def test_cgid_inferred_to_caregivers(self):
        """cgid is PK of caregivers → FK in chartevents (MIMIC pattern)."""
        profiles = [
            TableProfile(
                table_name="raw_caregivers", row_count=100,
                columns={"cgid": _pk_col("cgid"), "label": _nonunique_col("label")},
                foreign_keys=[],
            ),
            TableProfile(
                table_name="raw_chartevents", row_count=5000,
                columns={
                    "row_id": _pk_col("row_id"),
                    "cgid":   _nonunique_col("cgid"),
                },
                foreign_keys=[],
            ),
        ]
        result = _infer_fks(profiles)
        fks = result.get("raw_chartevents", [])
        assert any(f["column"] == "cgid" and f["ref_table"] == "raw_caregivers"
                   for f in fks), f"Expected cgid→raw_caregivers, got {fks}"

    def test_inferred_flag_set_for_strategy3(self):
        profiles = [
            TableProfile(
                table_name="raw_caregivers", row_count=100,
                columns={"cgid": _pk_col("cgid")}, foreign_keys=[],
            ),
            TableProfile(
                table_name="raw_outputevents", row_count=2000,
                columns={"row_id": _pk_col("row_id"), "cgid": _nonunique_col("cgid")},
                foreign_keys=[],
            ),
        ]
        result = _infer_fks(profiles)
        fk = next((f for f in result.get("raw_outputevents", [])
                   if f["column"] == "cgid"), None)
        assert fk is not None
        assert fk.get("inferred") is True

    def test_pk_col_itself_not_inferred_as_fk(self):
        """The PK column of the source table is never inferred as a FK."""
        profiles = [
            TableProfile(
                table_name="raw_caregivers", row_count=100,
                columns={"cgid": _pk_col("cgid")}, foreign_keys=[],
            ),
        ]
        result = _infer_fks(profiles)
        assert result.get("raw_caregivers", []) == []

    def test_strategy3_does_not_fire_when_strategy1_wins(self):
        """If Strategy 1 already resolved the column, Strategy 3 should not add a duplicate."""
        profiles = [
            TableProfile(
                table_name="raw_patients", row_count=1000,
                columns={
                    "row_id":     _unique_col("row_id", pk=True),
                    "subject_id": _unique_col("subject_id"),
                },
                foreign_keys=[],
            ),
            TableProfile(
                table_name="raw_admissions", row_count=1500,
                columns={
                    "row_id":     _unique_col("row_id", pk=True),
                    "subject_id": _nonunique_col("subject_id"),
                },
                foreign_keys=[],
            ),
        ]
        result = _infer_fks(profiles)
        adm_fks = [f for f in result.get("raw_admissions", []) if f["column"] == "subject_id"]
        assert len(adm_fks) == 1, "Strategy 3 must not duplicate a Strategy-1 result"


class TestAmbiguousPkFks:
    """_ambiguous_pk_fks returns columns whose PK name matches multiple tables."""

    def test_itemid_flagged_when_two_pk_tables(self):
        """itemid is PK in both d_items and d_labitems → ambiguous for labevents."""
        profiles = [
            TableProfile(
                table_name="raw_d_items", row_count=200,
                columns={"itemid": _pk_col("itemid"), "label": _nonunique_col("label")},
                foreign_keys=[],
            ),
            TableProfile(
                table_name="raw_d_labitems", row_count=50,
                columns={"itemid": _pk_col("itemid"), "fluid": _nonunique_col("fluid")},
                foreign_keys=[],
            ),
            TableProfile(
                table_name="raw_labevents", row_count=5000,
                columns={
                    "row_id": _pk_col("row_id"),
                    "itemid": _nonunique_col("itemid"),
                },
                foreign_keys=[],
            ),
        ]
        ambiguous = _ambiguous_pk_fks(profiles)
        lab_cases = ambiguous.get("raw_labevents", [])
        itemid_case = next((c for c in lab_cases if c["column"] == "itemid"), None)
        assert itemid_case is not None
        assert set(itemid_case["candidates"]) == {"raw_d_items", "raw_d_labitems"}

    def test_unambiguous_column_not_in_ambiguous_map(self):
        """cgid appears as PK in only one table → not in the ambiguous map."""
        profiles = [
            TableProfile(
                table_name="raw_caregivers", row_count=100,
                columns={"cgid": _pk_col("cgid")}, foreign_keys=[],
            ),
            TableProfile(
                table_name="raw_chartevents", row_count=5000,
                columns={"row_id": _pk_col("row_id"), "cgid": _nonunique_col("cgid")},
                foreign_keys=[],
            ),
        ]
        ambiguous = _ambiguous_pk_fks(profiles)
        assert "raw_chartevents" not in ambiguous


class TestBuildPkNameMap:
    def test_maps_pk_col_to_table(self):
        profiles = [
            TableProfile(
                table_name="raw_caregivers", row_count=100,
                columns={"cgid": _pk_col("cgid"), "label": _nonunique_col("label")},
                foreign_keys=[],
            ),
        ]
        pk_map = _build_pk_name_map(profiles)
        assert pk_map.get("cgid") == ["raw_caregivers"]

    def test_multiple_tables_same_pk_name(self):
        profiles = [
            TableProfile(table_name="raw_d_items",    row_count=10, columns={"itemid": _pk_col("itemid")}, foreign_keys=[]),
            TableProfile(table_name="raw_d_labitems", row_count=10, columns={"itemid": _pk_col("itemid")}, foreign_keys=[]),
        ]
        pk_map = _build_pk_name_map(profiles)
        assert set(pk_map.get("itemid", [])) == {"raw_d_items", "raw_d_labitems"}


class TestRolePrefixedFK:
    """Strategy 4: {role}_{entity_col} columns (e.g. dest_airport_id, source_airport_id)."""

    def _profiles(self):
        return [
            TableProfile(
                table_name="raw_airports",
                row_count=1000,
                columns={"airport_id": _unique_col("airport_id"), "name": _nonunique_col("name")},
                foreign_keys=[],
            ),
            TableProfile(
                table_name="raw_routes",
                row_count=5000,
                columns={
                    "dest_airport_id":   _nonunique_col("dest_airport_id"),
                    "source_airport_id": _nonunique_col("source_airport_id"),
                    "airline_id":        _nonunique_col("airline_id"),
                },
                foreign_keys=[],
            ),
        ]

    def test_dest_airport_id_inferred(self):
        result = _infer_fks(self._profiles())
        fks = {fk["column"]: fk["ref_table"] for fk in result.get("raw_routes", [])}
        assert fks.get("dest_airport_id") == "raw_airports"

    def test_source_airport_id_inferred(self):
        result = _infer_fks(self._profiles())
        fks = {fk["column"]: fk["ref_table"] for fk in result.get("raw_routes", [])}
        assert fks.get("source_airport_id") == "raw_airports"

    def test_both_role_fks_flagged_inferred(self):
        result = _infer_fks(self._profiles())
        dest = next((fk for fk in result.get("raw_routes", []) if fk["column"] == "dest_airport_id"), None)
        assert dest is not None
        assert dest.get("inferred") is True

    def test_short_suffix_ignored(self):
        """Suffix 'id' alone is too generic and must not be matched."""
        profiles = [
            TableProfile(
                table_name="raw_things",
                row_count=1000,
                columns={"id": _unique_col("id")},
                foreign_keys=[],
            ),
            TableProfile(
                table_name="raw_events",
                row_count=5000,
                columns={"dest_id": _nonunique_col("dest_id")},
                foreign_keys=[],
            ),
        ]
        result = _infer_fks(profiles)
        fks = {fk["column"] for fk in result.get("raw_events", [])}
        assert "dest_id" not in fks


class TestDateFormatsExcludedFromFkInference:
    """Date/time-formatted columns must never be treated as FK candidates (Synthea regression).

    Synthea's raw_conditions and raw_encounters both have 'start' / 'stop' columns
    with YYYY-MM-DD values.  Because encounter.start is near-unique it entered
    the natural-key map and raw_conditions.start was falsely inferred as a FK.
    The fix: columns whose detected_formats list is non-empty are excluded from
    both sides of FK inference.
    """

    def _date_col(self, name: str, distinct: int = 1000, rows: int = 1000,
                  fmt: str = "YYYY-MM-DD") -> ColumnProfile:
        """A near-unique column whose values are detected as dates."""
        return ColumnProfile(
            name=name, inferred_type="TEXT", is_nullable=False,
            is_primary_key=False, null_rate=0.0,
            distinct_count=distinct,
            detected_formats=[fmt],
        )

    def _nonunique_date_col(self, name: str, fmt: str = "YYYY-MM-DD") -> ColumnProfile:
        """A non-unique date column (the FK-source side in the false-positive)."""
        return ColumnProfile(
            name=name, inferred_type="TEXT", is_nullable=False,
            is_primary_key=False, null_rate=0.0,
            distinct_count=50,
            detected_formats=[fmt],
        )

    def test_date_column_not_fk_via_natural_key_match(self):
        """Near-unique date col in raw_encounters must not pull raw_conditions.start as FK."""
        profiles = [
            TableProfile(
                table_name="raw_encounters",
                row_count=1000,
                columns={
                    "id":    _unique_col("id", pk=True),
                    "start": self._date_col("start", distinct=1000, rows=1000),
                },
                foreign_keys=[],
            ),
            TableProfile(
                table_name="raw_conditions",
                row_count=5000,
                columns={
                    "id":      _unique_col("id", pk=True),
                    "patient": _nonunique_col("patient"),
                    "start":   self._nonunique_date_col("start"),
                    "stop":    self._nonunique_date_col("stop"),
                },
                foreign_keys=[],
            ),
        ]
        result = _infer_fks(profiles)
        fks = {fk["column"] for fk in result.get("raw_conditions", [])}
        assert "start" not in fks, "date-valued 'start' must not be inferred as FK"
        assert "stop" not in fks, "date-valued 'stop' must not be inferred as FK"

    def test_date_column_not_entered_in_natural_key_map(self):
        """A near-unique date column must not appear in the natural-key map."""
        from semantic_mapper.db.inspector import _build_natural_key_map
        profiles = [
            TableProfile(
                table_name="raw_encounters",
                row_count=1000,
                columns={
                    "id":    _unique_col("id", pk=True),
                    "start": self._date_col("start", distinct=1000, rows=1000),
                },
                foreign_keys=[],
            ),
        ]
        nk_map = _build_natural_key_map(profiles)
        assert "start" not in nk_map, "date column must be excluded from natural-key map"

    def test_iso8601_datetime_not_fk(self):
        """ISO 8601 datetimes (e.g. Synthea medications start/stop) must also be excluded."""
        profiles = [
            TableProfile(
                table_name="raw_encounters",
                row_count=1000,
                columns={
                    "id":    _unique_col("id", pk=True),
                    "start": self._date_col("start", fmt="ISO8601-UTC"),
                },
                foreign_keys=[],
            ),
            TableProfile(
                table_name="raw_medications",
                row_count=5000,
                columns={
                    "id":    _unique_col("id", pk=True),
                    "start": self._nonunique_date_col("start", fmt="ISO8601-UTC"),
                    "stop":  self._nonunique_date_col("stop",  fmt="ISO8601-UTC"),
                },
                foreign_keys=[],
            ),
        ]
        result = _infer_fks(profiles)
        fks = {fk["column"] for fk in result.get("raw_medications", [])}
        assert "start" not in fks, "ISO datetime 'start' must not be inferred as FK"
        assert "stop"  not in fks, "ISO datetime 'stop' must not be inferred as FK"

    def test_non_date_column_still_inferred_normally(self):
        """Removing date columns must not block legitimate FK inference."""
        profiles = [
            TableProfile(
                table_name="raw_patients",
                row_count=1000,
                columns={
                    "id":      _unique_col("id", pk=True),
                    "patient": _unique_col("patient"),
                },
                foreign_keys=[],
            ),
            TableProfile(
                table_name="raw_conditions",
                row_count=5000,
                columns={
                    "id":      _unique_col("id", pk=True),
                    "patient": _nonunique_col("patient"),
                    "start":   self._nonunique_date_col("start"),
                },
                foreign_keys=[],
            ),
        ]
        result = _infer_fks(profiles)
        fks = {fk["column"]: fk["ref_table"] for fk in result.get("raw_conditions", [])}
        assert fks.get("patient") == "raw_patients", "legitimate FK still resolved"
        assert "start" not in fks, "date column not added alongside the legitimate FK"


# ── Descriptive-column FK over-firing guards ─────────────────────────────────
# A fresh dataset surfaced three general FK over-firing patterns that the
# identifier-based datasets (MIMIC/Synthea/OpenFlights) did not:
#   1. a near-unique free-text column (name/title/place) seeds the natural-key
#      map, so any same-named column elsewhere inherits a spurious FK;
#   2. a low-cardinality categorical whose name matches a table's singular form
#      (e.g. a 'medal' value vs a 'medals' table) is linked by Strategy 2;
#   3. a source table references the same target via both a key-aligned column
#      and a redundant descriptive one.
# The fixes are general; these tests use generic names per the
# "no dataset-specific knowledge" principle.

def _free_text_col(name, distinct=1000, pk=False):
    """Near-unique column whose sample values are multi-word free text."""
    return ColumnProfile(
        name=name, inferred_type="VARCHAR(255)", is_nullable=False,
        is_primary_key=pk, null_rate=0.0, distinct_count=distinct,
        sample_data=["Jane Doe", "John Smith", "Ada Lovelace", "Alan Turing"],
    )


def _categorical_col(name, distinct=3):
    """Low-cardinality categorical column (top_values populated, as the profiler does)."""
    return ColumnProfile(
        name=name, inferred_type="VARCHAR(16)", is_nullable=False,
        is_primary_key=False, null_rate=0.0, distinct_count=distinct,
        top_values=[{"value": "A", "count": 30}, {"value": "B", "count": 20},
                    {"value": "C", "count": 10}],
    )


class TestLooksFreeText:
    def test_multiword_values_are_free_text(self):
        assert _looks_free_text(_free_text_col("full_name")) is True

    def test_single_token_values_are_not_free_text(self):
        col = ColumnProfile(
            name="badge", inferred_type="VARCHAR(8)", is_nullable=False,
            is_primary_key=False, null_rate=0.0, distinct_count=1000,
            sample_data=["AB12", "CD34", "EF56"],
        )
        assert _looks_free_text(col) is False

    def test_no_samples_is_not_free_text(self):
        col = ColumnProfile(
            name="x", inferred_type="INT", is_nullable=False,
            is_primary_key=False, null_rate=0.0, distinct_count=100,
        )
        assert _looks_free_text(col) is False


class TestFreeTextExcludedFromFkInference:
    """A near-unique free-text column must not seed a natural-key FK (Strategy 1)."""

    def test_free_text_not_in_natural_key_map(self):
        profiles = [
            TableProfile(
                table_name="raw_people", row_count=1000,
                columns={
                    "person_id": _unique_col("person_id", pk=True),
                    "full_name": _free_text_col("full_name"),
                },
                foreign_keys=[],
            ),
        ]
        assert "full_name" not in _build_natural_key_map(profiles)

    def test_free_text_does_not_pull_spurious_fk(self):
        """A 'full_name' column elsewhere must not be inferred as a FK to raw_people."""
        profiles = [
            TableProfile(
                table_name="raw_people", row_count=1000,
                columns={
                    "person_id": _unique_col("person_id", pk=True),
                    "full_name": _free_text_col("full_name"),
                },
                foreign_keys=[],
            ),
            TableProfile(
                table_name="raw_teams", row_count=500,
                columns={
                    "team_id":   _unique_col("team_id", pk=True),
                    "full_name": _free_text_col("full_name", distinct=450),
                },
                foreign_keys=[],
            ),
        ]
        result = _infer_fks(profiles)
        cols = {fk["column"] for fk in result.get("raw_teams", [])}
        assert "full_name" not in cols

    def test_identifier_natural_key_still_inferred(self):
        """The guard is specific to free text: a single-token natural key still resolves."""
        badge_b = ColumnProfile(
            name="badge", inferred_type="VARCHAR(8)", is_nullable=False,
            is_primary_key=False, null_rate=0.0, distinct_count=1000,
            sample_data=["AB12", "CD34", "EF56"],
        )
        badge_a = ColumnProfile(
            name="badge", inferred_type="VARCHAR(8)", is_nullable=False,
            is_primary_key=False, null_rate=0.0, distinct_count=50,
            sample_data=["AB12", "AB12", "CD34"],
        )
        profiles = [
            TableProfile(table_name="raw_people", row_count=1000,
                         columns={"person_id": _unique_col("person_id", pk=True), "badge": badge_b},
                         foreign_keys=[]),
            TableProfile(table_name="raw_visits", row_count=1000,
                         columns={"visit_id": _unique_col("visit_id", pk=True), "badge": badge_a},
                         foreign_keys=[]),
        ]
        result = _infer_fks(profiles)
        cols = {fk["column"]: fk["ref_table"] for fk in result.get("raw_visits", [])}
        assert cols.get("badge") == "raw_people"


class TestTableNameMatchCategoricalGuard:
    """Strategy 2 (table-name match) must not fire on a low-cardinality categorical."""

    def test_categorical_not_linked_to_similar_table(self):
        """A 'grade' categorical must not link to a 'grades' table by name coincidence."""
        profiles = [
            TableProfile(table_name="raw_grades", row_count=5,
                         columns={"grade_id": _pk_col("grade_id")}, foreign_keys=[]),
            TableProfile(table_name="raw_students", row_count=1000,
                         columns={"student_id": _unique_col("student_id", pk=True),
                                  "grade": _categorical_col("grade")},
                         foreign_keys=[]),
        ]
        result = _infer_fks(profiles)
        cols = {fk["column"] for fk in result.get("raw_students", [])}
        assert "grade" not in cols

    def test_high_cardinality_table_name_match_still_fires(self):
        """The guard is gated on top_values: a non-categorical name match still resolves."""
        grade_hi = ColumnProfile(
            name="grade", inferred_type="VARCHAR(16)", is_nullable=False,
            is_primary_key=False, null_rate=0.0, distinct_count=400,  # no top_values
        )
        profiles = [
            TableProfile(table_name="raw_grades", row_count=500,
                         columns={"grade_id": _pk_col("grade_id")}, foreign_keys=[]),
            TableProfile(table_name="raw_students", row_count=1000,
                         columns={"student_id": _unique_col("student_id", pk=True),
                                  "grade": grade_hi},
                         foreign_keys=[]),
        ]
        result = _infer_fks(profiles)
        cols = {fk["column"]: fk["ref_table"] for fk in result.get("raw_students", [])}
        assert cols.get("grade") == "raw_grades"


def _t_with_fks(name, pk_col, fks):
    """A table whose foreign_keys are pre-populated (simulating post-merge state)."""
    return TableProfile(
        table_name=name, row_count=1000,
        columns={pk_col: _pk_col(pk_col)},
        foreign_keys=fks,
    )


class TestPruneRedundantDescriptiveFks:
    """Drop a redundant descriptive FK when a key-aligned sibling refs the same target."""

    def test_descriptive_dropped_when_key_aligned_sibling(self):
        profiles = [
            TableProfile(table_name="raw_countries", row_count=30,
                         columns={"country_code": _pk_col("country_code")}, foreign_keys=[]),
            _t_with_fks("raw_results", "row_id", [
                {"column": "country_code", "ref_table": "raw_countries", "inferred": True},
                {"column": "country",      "ref_table": "raw_countries", "inferred": True},
            ]),
        ]
        _prune_redundant_descriptive_fks(profiles)
        results = next(p for p in profiles if p.table_name == "raw_results")
        assert {fk["column"] for fk in results.foreign_keys} == {"country_code"}

    def test_role_prefix_pair_both_kept(self):
        """Neither column matches the target PK name → both legitimate FKs survive."""
        profiles = [
            TableProfile(table_name="raw_airports", row_count=100,
                         columns={"airport_id": _pk_col("airport_id")}, foreign_keys=[]),
            _t_with_fks("raw_routes", "route_id", [
                {"column": "source_airport_id", "ref_table": "raw_airports", "inferred": True},
                {"column": "dest_airport_id",   "ref_table": "raw_airports", "inferred": True},
            ]),
        ]
        _prune_redundant_descriptive_fks(profiles)
        routes = next(p for p in profiles if p.table_name == "raw_routes")
        assert {fk["column"] for fk in routes.foreign_keys} == {"source_airport_id", "dest_airport_id"}

    def test_single_fk_untouched(self):
        profiles = [
            TableProfile(table_name="raw_countries", row_count=30,
                         columns={"country_code": _pk_col("country_code")}, foreign_keys=[]),
            _t_with_fks("raw_results", "row_id", [
                {"column": "country", "ref_table": "raw_countries", "inferred": True},
            ]),
        ]
        _prune_redundant_descriptive_fks(profiles)
        results = next(p for p in profiles if p.table_name == "raw_results")
        assert {fk["column"] for fk in results.foreign_keys} == {"country"}

    def test_explicit_fk_never_pruned(self):
        """Explicit DB FKs (no 'inferred' flag) are never dropped, even if non-aligned."""
        profiles = [
            TableProfile(table_name="raw_t", row_count=10,
                         columns={"id_code": _pk_col("id_code")}, foreign_keys=[]),
            _t_with_fks("raw_src", "row_id", [
                {"column": "id_code", "ref_table": "raw_t", "inferred": True},   # aligned
                {"column": "label",   "ref_table": "raw_t", "inferred": True},   # non-aligned → drop
                {"column": "name",    "ref_table": "raw_t"},                     # explicit → keep
            ]),
        ]
        _prune_redundant_descriptive_fks(profiles)
        src = next(p for p in profiles if p.table_name == "raw_src")
        assert {fk["column"] for fk in src.foreign_keys} == {"id_code", "name"}


# ── FK-4 — value-overlap entity-link inference (Strategy 5) ────────────────────


class TestIsTextType:
    @pytest.mark.parametrize("t", ["VARCHAR(8)", 'VARCHAR(8) COLLATE "utf8mb4"',
                                   "TEXT", "CHAR(2)", "varchar", "ENUM('a','b')"])
    def test_text_types(self, t):
        assert _is_text_type(t) is True

    @pytest.mark.parametrize("t", ["INTEGER", "TINYINT", "BIGINT", "DECIMAL(10,2)",
                                   "FLOAT", "DATE", "DATETIME", ""])
    def test_non_text_types(self, t):
        assert _is_text_type(t) is False


def _sqlite_inspector(tmp_path, statements):
    """Build a MySQLInspector over a throwaway SQLite file loaded with *statements*.

    A file (not :memory:) is required — the inspector opens several connections and
    :memory: gives each a fresh, empty database.
    """
    from sqlalchemy import create_engine, text
    db = tmp_path / "vo.db"
    eng = create_engine(f"sqlite:///{db}")
    with eng.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))
    s = Settings(
        DATABASE_URL=f"sqlite:///{db}", OPENAI_API_KEY="x",
        LOW_CARDINALITY_THRESHOLD=20, LOW_CARDINALITY_RATIO=0.5,
    )
    return MySQLInspector(s)


class TestValueOverlapFKInference:
    """Strategy 5 — a text categorical whose values are a near-subset of another
    table's text primary key is an undeclared foreign key, not a discrimination
    candidate.  All fixtures use generic names — the rule is structural, not dataset
    specific.
    """

    def test_fires_on_text_categorical_subset_of_key(self, tmp_path):
        ins = _sqlite_inspector(tmp_path, [
            "CREATE TABLE dimension (code TEXT PRIMARY KEY, label TEXT)",
            "CREATE TABLE fact (row_id INTEGER PRIMARY KEY, category TEXT, qty INTEGER)",
            *[f"INSERT INTO dimension VALUES ('K{i}', 'Label {i}')" for i in range(6)],
            *[f"INSERT INTO fact (category, qty) VALUES ('K{i % 6}', {i})" for i in range(30)],
        ])
        fact = next(p for p in ins.profile_all() if p.table_name == "fact")
        fks = {(fk["column"], fk["ref_table"]) for fk in fact.foreign_keys}
        assert ("category", "dimension") in fks

    def test_numeric_measure_overlapping_surrogate_key_not_inferred(self, tmp_path):
        # qty values (0..N counts) fall inside the surrogate integer PK range, but a
        # numeric measure must never be read as an entity reference.
        ins = _sqlite_inspector(tmp_path, [
            "CREATE TABLE dimension (code TEXT PRIMARY KEY, label TEXT)",
            "CREATE TABLE fact (row_id INTEGER PRIMARY KEY, category TEXT, qty INTEGER)",
            *[f"INSERT INTO dimension VALUES ('K{i}', 'Label {i}')" for i in range(6)],
            *[f"INSERT INTO fact (category, qty) VALUES ('K{i % 6}', {i % 4})" for i in range(30)],
        ])
        fact = next(p for p in ins.profile_all() if p.table_name == "fact")
        assert "qty" not in {fk["column"] for fk in fact.foreign_keys}

    def test_no_fk_when_overlap_below_threshold(self, tmp_path):
        # Only 1 of 6 category values exists in the dimension key → far below threshold.
        ins = _sqlite_inspector(tmp_path, [
            "CREATE TABLE dimension (code TEXT PRIMARY KEY, label TEXT)",
            "INSERT INTO dimension VALUES ('K0', 'only one')",
            "CREATE TABLE fact (row_id INTEGER PRIMARY KEY, category TEXT)",
            *[f"INSERT INTO fact (category) VALUES ('K{i % 6}')" for i in range(30)],
        ])
        fact = next(p for p in ins.profile_all() if p.table_name == "fact")
        assert "category" not in {fk["column"] for fk in fact.foreign_keys}

    def test_descriptive_shadow_of_coded_fk_not_reinferred(self, tmp_path):
        # fact has both a `place_code` (matches dimension PK by name → Strategy 3) and a
        # descriptive `place` name that overlaps the dimension's own name key.  The link
        # is carried by place_code; `place` must NOT become a second inferred FK.
        ins = _sqlite_inspector(tmp_path, [
            "CREATE TABLE place (place_code TEXT PRIMARY KEY, place TEXT)",
            "CREATE TABLE fact (row_id INTEGER PRIMARY KEY, place TEXT, place_code TEXT)",
            *[f"INSERT INTO place VALUES ('P{i}', 'Place {i}')" for i in range(6)],
            *[f"INSERT INTO fact (place, place_code) VALUES ('Place {i % 6}', 'P{i % 6}')"
              for i in range(30)],
        ])
        fact = next(p for p in ins.profile_all() if p.table_name == "fact")
        fks = {(fk["column"], fk["ref_table"]) for fk in fact.foreign_keys}
        assert ("place_code", "place") in fks
        assert "place" not in {c for c, _ in fks}

    def test_targets_are_text_typed_keys_only(self):
        def _pk(name, sql_type):
            return ColumnProfile(name=name, inferred_type=sql_type, is_nullable=False,
                                 is_primary_key=True, null_rate=0.0, distinct_count=5)
        profiles = [
            TableProfile(table_name="t_text", row_count=5,
                         columns={"code": _pk("code", "VARCHAR(8)")}, foreign_keys=[]),
            TableProfile(table_name="t_int", row_count=5,
                         columns={"row_id": _pk("row_id", "INTEGER")}, foreign_keys=[]),
        ]
        targets = {(t, c) for t, c, _ in _value_overlap_targets(profiles)}
        assert ("t_text", "code") in targets
        assert ("t_int", "row_id") not in targets   # integer surrogate key excluded


class TestAmbiguousNaturalKeyFK:
    """A column that is a near-unique natural key in MULTIPLE tables (no declared PK,
    e.g. icd9_code in both ICD dimensions) must be disambiguated per referencing table
    by value-overlap — not collapsed onto the highest-uniqueness winner. Generic names;
    the rule is structural.
    """

    def _two_dim_one_fact(self, tmp_path, fact_codes_from="b"):
        from semantic_mapper.db.inspector import _natural_key_candidate_map, _ambiguous_natural_key_fks  # noqa: F401
        pick = "B" if fact_codes_from == "b" else "A"
        ins = _sqlite_inspector(tmp_path, [
            # Two dimensions keyed by `code`, NEITHER declaring a primary key.
            "CREATE TABLE dim_a (row_id INTEGER, code TEXT, title TEXT)",
            "CREATE TABLE dim_b (row_id INTEGER, code TEXT, title TEXT)",
            "CREATE TABLE fact (id INTEGER PRIMARY KEY, code TEXT, qty INTEGER)",
            *[f"INSERT INTO dim_a VALUES ({i}, 'A{i}', 't{i}')" for i in range(10)],
            *[f"INSERT INTO dim_b VALUES ({i}, 'B{i}', 't{i}')" for i in range(10)],
            *[f"INSERT INTO fact (code, qty) VALUES ('{pick}{i % 10}', {i})" for i in range(30)],
        ])
        return ins.profile_all()

    def test_ambiguous_natural_key_flagged(self):
        # Built from raw profiles (pre-FK-application): code is a near-unique natural
        # key in BOTH dims (neither declaring a PK) → ambiguous for the referencing fact.
        from semantic_mapper.db.inspector import _ambiguous_natural_key_fks
        profiles = [
            _table_custom("dim_a", {"code": _unique_col("code"), "title": _nonunique_col("title")}),
            _table_custom("dim_b", {"code": _unique_col("code"), "title": _nonunique_col("title")}),
            _table_custom("fact", {"id": _pk_col("id"), "code": _nonunique_col("code")}),
        ]
        cases = _ambiguous_natural_key_fks(profiles).get("fact", [])
        code_case = next((c for c in cases if c["column"] == "code"), None)
        assert code_case is not None
        assert set(code_case["candidates"]) == {"dim_a", "dim_b"}

    def test_resolved_to_the_overlapping_dimension(self, tmp_path):
        # fact.code values are all B* → must resolve to dim_b, not the winner dim_a.
        profiles = self._two_dim_one_fact(tmp_path, fact_codes_from="b")
        fact = next(p for p in profiles if p.table_name == "fact")
        fks = {(fk["column"], fk["ref_table"]) for fk in fact.foreign_keys}
        assert ("code", "dim_b") in fks
        assert ("code", "dim_a") not in fks

    def test_resolves_the_other_way_when_values_flip(self, tmp_path):
        # Same schema, fact.code values all A* → must resolve to dim_a (not a fixed winner).
        profiles = self._two_dim_one_fact(tmp_path, fact_codes_from="a")
        fact = next(p for p in profiles if p.table_name == "fact")
        fks = {(fk["column"], fk["ref_table"]) for fk in fact.foreign_keys}
        assert ("code", "dim_a") in fks
        assert ("code", "dim_b") not in fks
