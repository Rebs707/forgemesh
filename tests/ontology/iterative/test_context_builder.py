# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
import pytest
from semantic_mapper.ontology.iterative.context_builder import (
    derive_class_local_name,
    derive_class_uri,
    build_table_context,
)
from semantic_mapper.db.models import ColumnProfile, TableProfile


class TestDeriveClassLocalName:
    def test_simple_plural(self):
        assert derive_class_local_name("raw_labevents") == "Labevent"

    def test_dict_table(self):
        assert derive_class_local_name("raw_d_labitems") == "Labitem"

    def test_mv_suffix(self):
        # "inputevents_mv" → singularize each part → "inputevent" + "mv" → "InputeventMv"
        assert derive_class_local_name("raw_inputevents_mv") == "InputeventMv"

    def test_no_prefix(self):
        assert derive_class_local_name("patients") == "Patient"

    def test_icd_diagnoses(self):
        # "icd_diagnoses" → singularize each part → "icd" + "diagnosis" → "IcdDiagnosis"
        result = derive_class_local_name("raw_d_icd_diagnoses")
        assert result == "IcdDiagnosis"

    def test_admissions(self):
        assert derive_class_local_name("raw_admissions") == "Admission"

    def test_short_name_no_strip(self):
        # "Ias" has len 3, condition is len > 3 → False → no strip
        name = derive_class_local_name("ias")
        assert name == "Ias"

    def test_single_word(self):
        assert derive_class_local_name("procedures") == "Procedure"

    def test_ies_plural(self):
        # "countries" → inflect singularizes ies→y → "Country", not "Countrie"
        assert derive_class_local_name("raw_countries") == "Country"

    def test_irregular_plural(self):
        # "diagnoses" → inflect → "diagnosis"
        assert derive_class_local_name("diagnoses") == "Diagnosis"


class TestDeriveClassUri:
    def test_full_uri(self):
        uri = derive_class_uri("raw_labevents", "http://x#")
        assert uri == "http://x#Labevent"


class TestBuildTableContext:
    def _make_profile(self):
        return TableProfile(
            table_name="raw_labevents",
            row_count=5000,
            columns={
                "row_id": ColumnProfile(
                    name="row_id", inferred_type="INTEGER", is_nullable=False,
                    is_primary_key=True, null_rate=0.0, distinct_count=5000,
                ),
                "subject_id": ColumnProfile(
                    name="subject_id", inferred_type="INTEGER", is_nullable=False,
                    is_primary_key=False, null_rate=0.0, distinct_count=400,
                ),
                "flag": ColumnProfile(
                    name="flag", inferred_type="VARCHAR", is_nullable=True,
                    is_primary_key=False, null_rate=0.3, distinct_count=3,
                    top_values=[{"value": "normal"}, {"value": "abnormal"}, {"value": "delta"}],
                ),
            },
            foreign_keys=[
                {"constrained_column": "subject_id", "referred_table": "patients"},
            ],
        )

    def test_pk_marked_correctly(self):
        ctx = build_table_context(self._make_profile(), 1, 5)
        pk_cols = [c for c in ctx.columns if c.is_pk]
        assert len(pk_cols) == 1
        assert pk_cols[0].col_name == "row_id"

    def test_fk_detected(self):
        ctx = build_table_context(self._make_profile(), 1, 5)
        subj = next(c for c in ctx.columns if c.col_name == "subject_id")
        assert subj.is_fk
        assert subj.fk_ref_table == "patients"

    def test_categorical_detected(self):
        ctx = build_table_context(self._make_profile(), 1, 5)
        flag_col = next(c for c in ctx.columns if c.col_name == "flag")
        assert flag_col.is_categorical
        assert "normal" in flag_col.categorical_values

    def test_processing_index(self):
        ctx = build_table_context(self._make_profile(), 3, 10)
        assert ctx.processing_index == 3
        assert ctx.total_tables == 10

    def test_inspector_inferred_fk_visible_in_context(self):
        """FKs added by the inspector (Strategy 1-3) appear in the context without
        any dataset-specific override map.  The profile.foreign_keys list is the
        single source of truth — the context builder reads it directly."""
        profile = TableProfile(
            table_name="raw_chartevents",
            row_count=1000,
            columns={
                "row_id": ColumnProfile(
                    name="row_id", inferred_type="INTEGER", is_nullable=False,
                    is_primary_key=True, null_rate=0.0, distinct_count=1000,
                ),
                "cgid": ColumnProfile(
                    name="cgid", inferred_type="INTEGER", is_nullable=True,
                    is_primary_key=False, null_rate=0.1, distinct_count=50,
                ),
            },
            # The inspector (Strategy 3) would have added this FK by the time
            # build_table_context is called in production.
            foreign_keys=[{"column": "cgid", "ref_table": "raw_caregivers", "inferred": True}],
        )
        ctx = build_table_context(profile, 1, 3)
        cgid_col = next(c for c in ctx.columns if c.col_name == "cgid")
        assert cgid_col.is_fk
        assert cgid_col.fk_ref_table == "raw_caregivers"

    def test_non_fk_column_unaffected(self):
        """Plain data columns are not misclassified as FKs."""
        profile = TableProfile(
            table_name="raw_chartevents",
            row_count=1000,
            columns={
                "row_id": ColumnProfile(
                    name="row_id", inferred_type="INTEGER", is_nullable=False,
                    is_primary_key=True, null_rate=0.0, distinct_count=1000,
                ),
                "value": ColumnProfile(
                    name="value", inferred_type="VARCHAR", is_nullable=True,
                    is_primary_key=False, null_rate=0.2, distinct_count=500,
                ),
            },
            foreign_keys=[],
        )
        ctx = build_table_context(profile, 1, 3)
        val_col = next(c for c in ctx.columns if c.col_name == "value")
        assert not val_col.is_fk
