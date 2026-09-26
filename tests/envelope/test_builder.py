# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the AnswerEnvelope builder and models."""
from __future__ import annotations

import pytest

from semantic_mapper.config.settings import Settings
from semantic_mapper.envelope import build_envelope, receipt_url_for, row_set_handle_for
from semantic_mapper.envelope.models import Answer, AnswerEnvelope, caveat_label


@pytest.fixture
def settings() -> Settings:
    return Settings()


# ── Answer.from_result ────────────────────────────────────────────────────────

def test_answer_from_dict_rows():
    ans = Answer.from_result({"rows": [{"a": 1, "b": 2}, {"a": 3, "b": 4}], "row_count": 2})
    assert ans.columns == ["a", "b"]
    assert ans.rows == [[1, 2], [3, 4]]
    assert ans.row_count == 2


def test_answer_from_empty():
    ans = Answer.from_result({})
    assert ans.columns == [] and ans.rows == [] and ans.row_count == 0


def test_caveat_label_fallback():
    assert caveat_label("small_group") == "SMALL GROUP"
    assert caveat_label("weird_kind") == "WEIRD KIND"


# ── URL / handle helpers ──────────────────────────────────────────────────────

def test_receipt_url_and_handle(settings):
    assert receipt_url_for("q_x", settings).endswith("/runs/q_x")
    assert row_set_handle_for("q_x") == "fm://runs/q_x/rows"


def test_receipt_base_url_override():
    s = Settings()
    s.RECEIPT_BASE_URL = "https://mesh.example.com/"
    assert receipt_url_for("q_1", s) == "https://mesh.example.com/runs/q_1"


# ── Inline envelope (v2 proof only) ───────────────────────────────────────────

def _v2():
    return {
        "interpretation": {
            "nl_question": "admissions by type",
            "interpreted_concepts": [
                {"nl_phrase": "admissions", "mapped_to_uri": "http://x#Admission",
                 "mapping_form": "DIRECT", "grounding_annotation": "one inpatient stay",
                 "grounding_node_uri": "http://x#Admission"}],
            "confidence": "HIGH", "confidence_reason": ""},
        "framing": {"cardinality": 5, "primary_class_uri": "http://x#Admission",
                    "joined_class_uris": [],
                    "constraints_applied": [
                        {"property_uri": "http://x#admitTime", "filter_form": "June 2026",
                         "property_definition": "admission timestamp"}]},
        "ontology_subgraph_hash": "abc123",
    }


def _answer():
    return {"columns": ["admission_type", "n"],
            "rows": [{"admission_type": "EMERGENCY", "n": 300},
                     {"admission_type": "ELECTIVE", "n": 18},
                     {"admission_type": "URGENT", "n": 3}],
            "row_count": 3}


def test_inline_envelope_four_blocks(settings):
    env = build_envelope(
        run_id="q_abc", question="admissions by type",
        executed_query="SELECT admission_type, COUNT(*) AS n FROM gold_admission GROUP BY admission_type",
        mode="gold", answer=_answer(), settings=settings, v2_proof=_v2())
    assert isinstance(env, AnswerEnvelope)
    r = env.receipt
    # Block 1: understood (concept + constraint)
    terms = {t.term: t.reading for t in r.understood}
    assert terms["admissions"] == "one inpatient stay"
    assert "admitTime" in terms
    # Block 2: sources — SPARQL framing falls back to class label
    assert any(s.table == "Admission" for s in r.sources)
    # Block 3: evidence — cardinality wins, handle + sample present
    assert r.evidence.rows_matched == 5
    assert r.evidence.row_set == "fm://runs/q_abc/rows"
    assert r.evidence.sample_shown == 3
    # Block 4: caveats — small_group fires (18 and 3 < 20)
    sg = [c for c in r.caveats if c.kind == "small_group"]
    assert sg and sg[0].count == 2
    # envelope framing
    assert env.receipt_url.endswith("/runs/q_abc")
    assert r.depth == "inline"
    assert r.ontology_version == "abc123"


# ── Full-depth envelope (prove_result folds in lineage + tables) ──────────────

def test_full_envelope_uses_lineage(settings):
    prove_result = {
        "semantic_proof": _v2(),
        "lineage_summary": {
            "data_audit_summary": {"quarantined_rows": 4, "null_excluded_rows": 2,
                                   "broken_lineage_count": 1, "subject_count": 321},
            "mapping_confidence_summary": {"all_mappings_human_confirmed": True,
                                           "human_confirmed_count": 3, "auto_confirmed_count": 0}},
        "proof_presentation": {
            "question_interpretation": "counted Admission instances",
            "data_layer_trace": [
                {"layer": "gold", "table_name": "gold_admission", "column_names": ["admission_type"]},
                {"layer": "bronze", "table_name": "admissions", "column_names": ["admittime"]}],
            "result_confidence": {"row_count": 3}},
    }
    env = build_envelope(
        run_id="q_full", question="admissions by type",
        executed_query="SELECT admission_type, COUNT(*) AS n FROM gold_admission GROUP BY admission_type",
        mode="gold", answer=_answer(), settings=settings, prove_result=prove_result)
    r = env.receipt
    assert r.depth == "full"
    # Real pipeline tables replace the inline class labels.
    tables = {s.table for s in r.sources}
    assert {"gold_admission", "admissions"} <= tables
    assert r.sources[0].mapping_confirmed == "confirmed by operator"
    # Lineage-derived caveats present alongside the computed small_group.
    kinds = {c.kind for c in r.caveats}
    assert {"quarantined", "null_excluded", "reference_gap", "small_group"} <= kinds


# ── Robustness ────────────────────────────────────────────────────────────────

def test_builder_never_raises_on_garbage(settings):
    env = build_envelope(
        run_id="q_bad", question="q", executed_query="", mode="gold",
        answer={"rows": "not-a-list"}, settings=settings, v2_proof={"framing": "broken"})
    assert isinstance(env, AnswerEnvelope)
    assert env.run_id == "q_bad"
    assert env.receipt.evidence.row_set == "fm://runs/q_bad/rows"


def test_multi_source_sql_join_keys(settings):
    env = build_envelope(
        run_id="q_join", question="stays by ward",
        executed_query=("SELECT w.name, COUNT(*) AS n FROM gold_admission a "
                        "JOIN ref_wards w ON a.ward_code = w.ward_code GROUP BY w.name"),
        mode="gold", answer={"rows": [{"name": "AMU", "n": 5}], "row_count": 1},
        settings=settings)
    tables = [s.table for s in env.receipt.sources]
    assert "gold_admission" in tables and "ref_wards" in tables
    # join key parsed generically from ON a.ward_code = w.ward_code
    assert any(s.join_key == "ward_code" for s in env.receipt.sources)


# ── Join keys must come from each table's own ON clause, not positional order ──

def _source(env, table):
    return next(s for s in env.receipt.sources if s.table == table)


def test_two_table_join_key_control(settings):
    """Control: a single 2-table join where positional and parsed agree.

    Both tables link on the same column, so a positional bug and a correct parser
    happen to produce the same answer here — this stays green before and after the
    fix, isolating the 3-table failure to the mechanism, not the parser overall.
    """
    env = build_envelope(
        run_id="q_2t", question="counts by admission",
        executed_query=(
            "SELECT a.hadm_id, COUNT(*) AS n "
            "FROM gold_diagnosisicd d "
            "JOIN gold_admission a ON a.hadm_id = d.hadm_id "
            "GROUP BY a.hadm_id"),
        mode="gold", answer={"rows": [{"hadm_id": 1, "n": 2}], "row_count": 1},
        settings=settings)
    assert _source(env, "gold_admission").join_key == "hadm_id"
    # The hub table also links on hadm_id here.
    assert "hadm_id" in (_source(env, "gold_diagnosisicd").join_key or "")


def test_three_table_join_keys_from_own_on_clause(settings):
    """Regression for q_573ad7d776dc: keys must not slip by one source.

    gold_admission links via a.hadm_id = d.hadm_id  -> hadm_id
    gold_icddiagnosis links via dict.icd9_code = d.icd9_code -> icd9_code
    gold_diagnosisicd (the hub, alias d) participates in BOTH -> both keys
    """
    env = build_envelope(
        run_id="q_3t", question="deaths by diagnosis",
        executed_query=(
            "SELECT d.long_title, COUNT(DISTINCT a.hadm_id) AS death_count "
            "FROM gold_diagnosisicd d "
            "JOIN gold_admission a ON a.hadm_id = d.hadm_id "
            "LEFT JOIN gold_icddiagnosis dict ON dict.icd9_code = d.icd9_code "
            "WHERE a.hospital_expire_flag = 1 "
            "GROUP BY d.long_title ORDER BY death_count DESC LIMIT 100"),
        mode="gold",
        answer={"rows": [{"long_title": "Unspecified septicemia", "death_count": 5}], "row_count": 1},
        settings=settings)

    # Each leaf table's key comes from its OWN ON clause — not the next clause.
    assert _source(env, "gold_admission").join_key == "hadm_id"
    assert _source(env, "gold_icddiagnosis").join_key == "icd9_code"
    # The hub participates in both joins and must list both linking keys.
    hub_keys = {k.strip() for k in (_source(env, "gold_diagnosisicd").join_key or "").split(",")}
    assert hub_keys == {"hadm_id", "icd9_code"}


def test_join_key_backtick_table_qualified_no_alias(settings):
    """The exact shape the web LLM emits: per-identifier backticks, table-qualified
    ON columns, no aliases. `t`.`c` must parse the same as t.c."""
    env = build_envelope(
        run_id="q_bt", question="admissions per gender",
        executed_query=(
            "SELECT `gold_patient`.`gender`, COUNT(*) AS `admission_count` "
            "FROM `gold_admission` JOIN `gold_patient` "
            "ON `gold_admission`.`subject_id` = `gold_patient`.`subject_id` "
            "GROUP BY `gold_patient`.`gender` LIMIT 100"),
        mode="gold", answer={"rows": [{"gender": "F", "admission_count": 5}], "row_count": 1},
        settings=settings)
    assert _source(env, "gold_admission").join_key == "subject_id"
    assert _source(env, "gold_patient").join_key == "subject_id"
