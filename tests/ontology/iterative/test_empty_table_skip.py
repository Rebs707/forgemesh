# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the empty/near-empty table skip feature.

Covers:
  - topology: skipped tables excluded from sort result, FK deps on them cleared
  - context_builder: fk_ref_skipped set when ref table is in skip set
  - generator: warning logged, only non-skipped tables generate LLM calls
  - gen_prompt: SKIPPED flag shown in column display, Rule 3 exception present
  - judge_prompt: SKIPPED shown in coverage, acceptance note present
  - ONTOLOGY_MIN_ROWS_FOR_GENERATION threshold respected
"""
import logging
from unittest.mock import MagicMock, patch

import pytest

from semantic_mapper.db.models import ColumnProfile, TableProfile
from semantic_mapper.ontology.iterative.context_builder import build_table_context
from semantic_mapper.ontology.iterative.gen_prompt import build_gen_prompt
from semantic_mapper.ontology.iterative.judge_prompt import build_judge_prompt
from semantic_mapper.ontology.iterative.generator import IterativeOntologyGenerator
from semantic_mapper.ontology.iterative.models import (
    ColumnContext,
    JudgeVerdict,
    OntologyFragment,
    TableContext,
)
from semantic_mapper.ontology.iterative.topology import fk_topological_sort


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _col(name, inferred_type="VARCHAR", distinct_count=10, is_pk=False,
         is_nullable=False, top_values=None):
    return ColumnProfile(
        name=name, inferred_type=inferred_type, is_primary_key=is_pk,
        is_nullable=is_nullable, null_rate=0.0, distinct_count=distinct_count,
        top_values=top_values or [], detected_formats=[],
    )


def _profile(table_name, row_count, cols, fks=None):
    return TableProfile(
        table_name=table_name,
        row_count=row_count,
        columns={c.name: c for c in cols},
        foreign_keys=fks or [],
        primary_keys=[c.name for c in cols if c.is_primary_key],
    )


def _make_settings(tmp_path, min_rows=0):
    # A real Settings (not a bare MagicMock) so every attribute the generator reads
    # has its typed default; only the knobs these tests depend on are overridden.
    from semantic_mapper.config.settings import Settings
    return Settings(
        OPENAI_API_KEY="test-key",
        LLM_REQUEST_TIMEOUT=30,
        LLM_STREAM=False,
        ONTOLOGY_GEN_MODEL="gpt-4o",
        ONTOLOGY_JUDGE_MODEL="gpt-4o-mini",
        ONTOLOGY_GEN_MAX_RETRIES=0,
        ONTOLOGY_GEN_TOP_K_CLASSES=10,
        ONTOLOGY_GEN_TEMPERATURE=0.1,
        ONTOLOGY_GEN_JUDGE_TEMPERATURE=0.0,
        ONTOLOGY_JUDGE_CONFIDENCE_FLOOR=0.6,
        ONTOLOGY_MAX_ROWS_FOR_ENUMERATION=1000,
        ONTOLOGY_MAX_DISTINCT_FOR_ENUMERATION=20,
        ONTOLOGY_MAX_DISTINCT_FOR_OPEN_CLASS=100,
        ONTOLOGY_MIN_ROWS_FOR_GENERATION=min_rows,
        ONTOLOGY_COL_WORD_SPLITS={},
        SEMANTIC_MATCHER_MODEL="all-MiniLM-L6-v2",
        DATABASE_URL="mysql://root@localhost/bronze_db",
        SEED_OUTPUT_PATH=str(tmp_path / "seed.owl"),
        OPENAI_MODEL="gpt-4o",
    )


_MINIMAL_FRAGMENT = """\
Prefix: owl: <http://www.w3.org/2002/07/owl#>
Prefix: rdfs: <http://www.w3.org/2000/01/rdf-schema#>
Prefix: skos: <http://www.w3.org/2004/02/skos/core#>
Prefix: xsd: <http://www.w3.org/2001/XMLSchema#>
Prefix: : <http://mesh.local/ontology/bronze_db#>

Class: :Patient
    Annotations: rdfs:label "Patient"@en

DatatypeProperty: :hasPatientName
    Annotations:
        rdfs:label "has patient name"@en,
        skos:altLabel "name"@en
    Domain: :Patient
    Range: xsd:string
"""

_APPROVED = JudgeVerdict(approved=True, confidence=0.9)


# ---------------------------------------------------------------------------
# Topology: skip_tables
# ---------------------------------------------------------------------------

class TestTopologySkipTables:
    def test_skipped_table_not_in_result(self):
        profiles = [
            _profile("patients", 500, [_col("id", is_pk=True)]),
            _profile("noteevents", 0, [_col("id", is_pk=True)]),
            _profile("admissions", 300, [_col("id", is_pk=True)]),
        ]
        result = fk_topological_sort(profiles, skip_tables={"noteevents"})
        names = [p.table_name for p in result]
        assert "noteevents" not in names
        assert "patients" in names
        assert "admissions" in names

    def test_empty_skip_set_returns_all(self):
        profiles = [
            _profile("patients", 500, [_col("id", is_pk=True)]),
            _profile("admissions", 300, [_col("id", is_pk=True)]),
        ]
        result = fk_topological_sort(profiles, skip_tables=set())
        assert len(result) == 2

    def test_none_skip_set_returns_all(self):
        profiles = [
            _profile("patients", 500, [_col("id", is_pk=True)]),
        ]
        result = fk_topological_sort(profiles, skip_tables=None)
        assert len(result) == 1

    def test_downstream_not_blocked_by_skipped_dep(self):
        """A table with FK to a skipped table must still appear in the result."""
        profiles = [
            _profile("noteevents", 0, [_col("id", is_pk=True)]),
            _profile("admissions", 300, [_col("id", is_pk=True)],
                     fks=[{"constrained_column": "id", "referred_table": "noteevents"}]),
        ]
        result = fk_topological_sort(profiles, skip_tables={"noteevents"})
        names = [p.table_name for p in result]
        assert "admissions" in names
        assert "noteevents" not in names

    def test_topo_order_preserved_for_non_skipped(self):
        """Referenced tables still come before their dependents."""
        profiles = [
            _profile("patients", 500, [_col("id", is_pk=True)]),
            _profile("noteevents", 0, [_col("id", is_pk=True)]),
            _profile("admissions", 300, [_col("id", is_pk=True)],
                     fks=[{"constrained_column": "patient_id", "referred_table": "patients"}]),
        ]
        result = fk_topological_sort(profiles, skip_tables={"noteevents"})
        names = [p.table_name for p in result]
        assert names.index("patients") < names.index("admissions")


# ---------------------------------------------------------------------------
# context_builder: fk_ref_skipped
# ---------------------------------------------------------------------------

class TestContextBuilderFkRefSkipped:
    def test_fk_ref_skipped_set_when_ref_in_skip_set(self):
        profile = _profile(
            "admissions", 300,
            [_col("id", "INTEGER", is_pk=True), _col("noteid", "INTEGER")],
            fks=[{"constrained_column": "noteid", "referred_table": "noteevents"}],
        )
        ctx = build_table_context(profile, 1, 1, skipped_tables={"noteevents"})
        noteid_col = next(c for c in ctx.columns if c.col_name == "noteid")
        assert noteid_col.is_fk is True
        assert noteid_col.fk_ref_table == "noteevents"
        assert noteid_col.fk_ref_skipped is True

    def test_fk_ref_not_skipped_when_ref_not_in_skip_set(self):
        profile = _profile(
            "admissions", 300,
            [_col("id", "INTEGER", is_pk=True), _col("patient_id", "INTEGER")],
            fks=[{"constrained_column": "patient_id", "referred_table": "patients"}],
        )
        ctx = build_table_context(profile, 1, 1, skipped_tables={"noteevents"})
        pid_col = next(c for c in ctx.columns if c.col_name == "patient_id")
        assert pid_col.fk_ref_skipped is False

    def test_fk_ref_not_skipped_when_no_skip_set(self):
        profile = _profile(
            "admissions", 300,
            [_col("id", "INTEGER", is_pk=True), _col("patient_id", "INTEGER")],
            fks=[{"constrained_column": "patient_id", "referred_table": "patients"}],
        )
        ctx = build_table_context(profile, 1, 1)
        pid_col = next(c for c in ctx.columns if c.col_name == "patient_id")
        assert pid_col.fk_ref_skipped is False

    def test_non_fk_col_never_skipped(self):
        profile = _profile("admissions", 300, [_col("name", "VARCHAR")])
        ctx = build_table_context(profile, 1, 1, skipped_tables={"name"})
        col = ctx.columns[0]
        assert col.fk_ref_skipped is False


# ---------------------------------------------------------------------------
# gen_prompt: SKIPPED display and Rule 3 exception
# ---------------------------------------------------------------------------

BASE_IRI = "http://mesh.local/ontology/test#"


def _col_ctx(col_name, inferred_type="VARCHAR", is_fk=False, fk_ref_table=None,
             fk_ref_skipped=False):
    return ColumnContext(
        table_name="admissions",
        col_name=col_name,
        inferred_type=inferred_type,
        is_pk=False,
        is_fk=is_fk,
        fk_ref_table=fk_ref_table,
        null_rate=0.0,
        distinct_count=10,
        fk_ref_skipped=fk_ref_skipped,
    )


def _table_ctx(cols):
    return TableContext(
        table_name="admissions",
        row_count=300,
        columns=cols,
        explicit_fks=[],
        processing_index=1,
        total_tables=1,
    )


class TestGenPromptSkippedFk:
    def test_skipped_fk_shown_in_column_display(self):
        col = _col_ctx("noteid", "INTEGER", is_fk=True, fk_ref_table="noteevents",
                       fk_ref_skipped=True)
        prompt = build_gen_prompt(
            table_ctx=_table_ctx([col]),
            retrieved_classes=[],
            deterministic_uri=f"{BASE_IRI}Admission",
            base_iri=BASE_IRI,
        )
        assert "SKIPPED" in prompt
        assert "DatatypeProperty xsd:integer" in prompt

    def test_non_skipped_fk_shows_normal_flag(self):
        col = _col_ctx("patient_id", "INTEGER", is_fk=True, fk_ref_table="patients",
                       fk_ref_skipped=False)
        prompt = build_gen_prompt(
            table_ctx=_table_ctx([col]),
            retrieved_classes=[],
            deterministic_uri=f"{BASE_IRI}Admission",
            base_iri=BASE_IRI,
        )
        assert "FK→patients" in prompt
        # The SKIPPED bracket tag only appears when fk_ref_skipped=True; the rules
        # do mention the word SKIPPED in their exception text so we check the tag form.
        assert "FK→patients [SKIPPED" not in prompt

    def test_rule_3_exception_present(self):
        col = _col_ctx("noteid", "INTEGER", is_fk=True, fk_ref_table="noteevents",
                       fk_ref_skipped=True)
        prompt = build_gen_prompt(
            table_ctx=_table_ctx([col]),
            retrieved_classes=[],
            deterministic_uri=f"{BASE_IRI}Admission",
            base_iri=BASE_IRI,
        )
        assert "EXCEPTION" in prompt
        assert "empty" in prompt


# ---------------------------------------------------------------------------
# judge_prompt: SKIPPED in coverage and acceptance note
# ---------------------------------------------------------------------------

class TestJudgePromptSkippedFk:
    def _build(self, cols):
        ctx = _table_ctx(cols)
        fragment = OntologyFragment(table_name="admissions", manchester_syntax="Class: :Admission")
        _, user_prompt = build_judge_prompt(
            table_ctx=ctx,
            fragment=fragment,
            accumulated_classes=[],
            base_iri=BASE_IRI,
        )
        return user_prompt

    def test_skipped_fk_shown_in_coverage(self):
        col = _col_ctx("noteid", "INTEGER", is_fk=True, fk_ref_table="noteevents",
                       fk_ref_skipped=True)
        prompt = self._build([col])
        assert "[SKIPPED]" in prompt

    def test_acceptance_note_present(self):
        col = _col_ctx("noteid", "INTEGER", is_fk=True, fk_ref_table="noteevents",
                       fk_ref_skipped=True)
        prompt = self._build([col])
        assert "SKIPPED" in prompt
        assert "DatatypeProperty xsd:integer" in prompt

    def test_non_skipped_fk_no_skipped_tag(self):
        col = _col_ctx("patient_id", "INTEGER", is_fk=True, fk_ref_table="patients")
        prompt = self._build([col])
        # The acceptance note mentions [SKIPPED] in its prose regardless; the column-line
        # tag form "FK→… [SKIPPED]" only appears when fk_ref_skipped=True.
        assert "FK→patients [SKIPPED]" not in prompt


# ---------------------------------------------------------------------------
# generator: warning logged, only non-skipped tables produce LLM calls
# ---------------------------------------------------------------------------

class TestGeneratorEmptyTableSkip:
    def _three_profiles(self):
        return [
            _profile("patients", 500, [_col("id", "INTEGER", is_pk=True), _col("name")]),
            _profile("noteevents", 0,  [_col("id", "INTEGER", is_pk=True), _col("text")]),
            _profile("admissions", 300, [_col("id", "INTEGER", is_pk=True), _col("type")]),
        ]

    def test_empty_table_excluded_from_llm_calls(self, tmp_path):
        settings = _make_settings(tmp_path, min_rows=0)
        gen = IterativeOntologyGenerator(settings)
        gen_call_tables = []

        def fake_gen_llm(prompt, model, temperature):
            # Record which table the call is for by inspecting the prompt
            gen_call_tables.append(prompt)
            return _MINIMAL_FRAGMENT

        with patch("openai.OpenAI"):
            with patch.object(gen, "_call_gen_llm", side_effect=fake_gen_llm):
                with patch.object(gen, "_call_judge_llm", return_value=_APPROVED):
                    gen.generate(self._three_profiles())

        # Only patients and admissions should have triggered a gen call
        assert len(gen_call_tables) == 2
        for prompt in gen_call_tables:
            assert "noteevents" not in prompt.split("Table")[1].split("\n")[0]

    def test_warning_logged_for_empty_table(self, tmp_path, caplog):
        settings = _make_settings(tmp_path, min_rows=0)
        gen = IterativeOntologyGenerator(settings)

        with caplog.at_level(logging.WARNING, logger="semantic_mapper.ontology.iterative.generator"):
            with patch("openai.OpenAI"):
                with patch.object(gen, "_call_gen_llm", return_value=_MINIMAL_FRAGMENT):
                    with patch.object(gen, "_call_judge_llm", return_value=_APPROVED):
                        gen.generate(self._three_profiles())

        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        skip_warnings = [m for m in warning_messages if "noteevents" in m and "ONTOLOGY_MIN_ROWS" in m]
        assert len(skip_warnings) == 1

    def test_generated_ontology_has_no_class_for_skipped_table(self, tmp_path):
        settings = _make_settings(tmp_path, min_rows=0)
        gen = IterativeOntologyGenerator(settings)

        with patch("openai.OpenAI"):
            with patch.object(gen, "_call_gen_llm", return_value=_MINIMAL_FRAGMENT):
                with patch.object(gen, "_call_judge_llm", return_value=_APPROVED):
                    out_path = gen.generate(self._three_profiles())

        from rdflib import OWL, RDF, Graph
        g = Graph()
        g.parse(str(out_path), format="xml")
        class_uris = {str(s) for s in g.subjects(RDF.type, OWL.Class)}
        assert not any("Noteevent" in uri for uri in class_uris)


# ---------------------------------------------------------------------------
# generator: min_rows threshold
# ---------------------------------------------------------------------------

class TestMinRowsThreshold:
    def _profiles(self):
        return [
            _profile("small_table", 50,  [_col("id", "INTEGER", is_pk=True), _col("name")]),
            _profile("large_table", 150, [_col("id", "INTEGER", is_pk=True), _col("name")]),
        ]

    def test_threshold_100_skips_50_row_table(self, tmp_path):
        settings = _make_settings(tmp_path, min_rows=100)
        gen = IterativeOntologyGenerator(settings)
        gen_call_count = {"n": 0}

        def fake_gen(prompt, model, temp):
            gen_call_count["n"] += 1
            return _MINIMAL_FRAGMENT

        with patch("openai.OpenAI"):
            with patch.object(gen, "_call_gen_llm", side_effect=fake_gen):
                with patch.object(gen, "_call_judge_llm", return_value=_APPROVED):
                    gen.generate(self._profiles())

        # Only large_table (150 rows) should generate
        assert gen_call_count["n"] == 1

    def test_threshold_100_does_not_skip_150_row_table(self, tmp_path, caplog):
        settings = _make_settings(tmp_path, min_rows=100)
        gen = IterativeOntologyGenerator(settings)

        with caplog.at_level(logging.WARNING, logger="semantic_mapper.ontology.iterative.generator"):
            with patch("openai.OpenAI"):
                with patch.object(gen, "_call_gen_llm", return_value=_MINIMAL_FRAGMENT):
                    with patch.object(gen, "_call_judge_llm", return_value=_APPROVED):
                        gen.generate(self._profiles())

        skip_warnings = [
            r.message for r in caplog.records
            if r.levelno == logging.WARNING and "ONTOLOGY_MIN_ROWS" in r.message
        ]
        # small_table skipped, large_table not
        assert any("small_table" in m for m in skip_warnings)
        assert not any("large_table" in m for m in skip_warnings)

    def test_threshold_0_only_skips_zero_row_table(self, tmp_path, caplog):
        profiles = [
            _profile("empty",    0,   [_col("id", "INTEGER", is_pk=True)]),
            _profile("nonempty", 1,   [_col("id", "INTEGER", is_pk=True), _col("v")]),
        ]
        settings = _make_settings(tmp_path, min_rows=0)
        gen = IterativeOntologyGenerator(settings)
        gen_calls = {"n": 0}

        def fake_gen(prompt, model, temp):
            gen_calls["n"] += 1
            return _MINIMAL_FRAGMENT

        with caplog.at_level(logging.WARNING, logger="semantic_mapper.ontology.iterative.generator"):
            with patch("openai.OpenAI"):
                with patch.object(gen, "_call_gen_llm", side_effect=fake_gen):
                    with patch.object(gen, "_call_judge_llm", return_value=_APPROVED):
                        gen.generate(profiles)

        assert gen_calls["n"] == 1
        skip_warnings = [
            r.message for r in caplog.records
            if r.levelno == logging.WARNING and "ONTOLOGY_MIN_ROWS" in r.message
        ]
        assert any("empty" in m for m in skip_warnings)
        assert not any("nonempty" in m for m in skip_warnings)
