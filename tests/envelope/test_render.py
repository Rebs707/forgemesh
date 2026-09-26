# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the pure inline receipt renderer.

The failure mode under test: a line that reads as *proven* when the underlying
field is null. Fixture A asserts those lines are ABSENT, not merely that others
are present.
"""
from __future__ import annotations

from semantic_mapper.config.settings import Settings
from semantic_mapper.envelope import build_envelope, render_receipt


# ── Fixture A — today's real envelope shape: only caveats carry information ────
# understood term is the ungrounded "interpreted as asked" placeholder (grounded_uri
# is None); the source has no mapping_confirmed; evidence has no rows_scanned.

FIXTURE_A = {
    "envelope_version": "1.0.0",
    "run_id": "q_real",
    "answer": {"columns": ["admission_type", "n"], "rows": [["EMERGENCY", 300]], "row_count": 1},
    "receipt": {
        "understood": [
            {"term": "how many admissions by type?",
             "reading": "interpreted as asked (no grounded term resolutions captured)",
             "grounded_uri": None},
        ],
        "sources": [
            {"table": "gold_admission", "layer": "gold", "rows_scanned": None,
             "join_key": None, "date_column": None, "mapping_confirmed": None},
        ],
        "evidence": {"rows_matched": 3, "rows_scanned": None, "sample": [],
                     "sample_shown": 0, "row_set": "fm://runs/q_real/rows", "funnel": []},
        "caveats": [
            {"kind": "small_group", "count": 2,
             "note": "2 groups contain fewer than 20 rows (smallest: 3).", "severity": "warn"},
        ],
        "run_id": "q_real", "query_text": "SELECT ...", "ontology_version": None, "depth": "inline",
    },
    "receipt_url": "http://mesh.local/runs/q_real",
}


def test_fixture_a_renders_caveats_and_url():
    out = render_receipt(FIXTURE_A)
    # Present:
    assert "SMALL GROUP ×2" in out
    assert "2 groups contain fewer than 20 rows" in out
    assert "http://mesh.local/runs/q_real" in out


def test_fixture_a_omits_unproven_lines():
    """Assert on ABSENCE — nothing null may render as if it were proven."""
    out = render_receipt(FIXTURE_A)
    # No grounding line — the ungrounded placeholder reading must not appear...
    assert "Understood" not in out
    assert "interpreted as asked" not in out
    # ...and the grounding arrow (used only by grounding + funnel) must be gone.
    assert "→" not in out
    # No funnel / scan line — rows_scanned is null.
    assert "Evidence" not in out
    assert "scanned" not in out
    # No mapping-confirmed claim — mapping_confirmed is null.
    assert "confirmed" not in out.lower()


# ── Fixture B — fully populated: all four line types must render ──────────────

FIXTURE_B = {
    "envelope_version": "1.0.0",
    "run_id": "q_full",
    "answer": {"columns": ["c"], "rows": [[1]], "row_count": 1},
    "receipt": {
        "understood": [
            {"term": "admissions", "reading": "one inpatient stay",
             "grounded_uri": "http://x#Admission"},
            {"term": "last month", "reading": "2026-06-01..2026-06-30",
             "grounded_uri": "http://x#admitTime"},
        ],
        "sources": [
            {"table": "gold_admission", "layer": "gold", "rows_scanned": 48112,
             "join_key": "hadm_id", "mapping_confirmed": "confirmed by operator"},
            {"table": "gold_icddiagnosis", "layer": "gold", "join_key": "icd9_code",
             "mapping_confirmed": "confirmed by operator"},
        ],
        "evidence": {"rows_matched": 1284, "rows_scanned": 48112,
                     "sample": [{"c": 1}], "sample_shown": 1,
                     "row_set": "fm://runs/q_full/rows",
                     "funnel": [{"n": 48112, "label": "all stays"},
                                {"n": 3200, "label": "in June"},
                                {"n": 1284, "label": "emergency, discharged"}]},
        "caveats": [
            {"kind": "open_records", "count": 23, "note": "23 stays still open.", "severity": "warn"},
        ],
        "run_id": "q_full", "query_text": "SELECT ...", "ontology_version": "hash", "depth": "full",
    },
    "receipt_url": "http://mesh.local/runs/q_full",
}


def test_fixture_b_renders_all_four_line_types():
    out = render_receipt(FIXTURE_B)
    # 1. grounded terms
    assert "Understood:" in out
    assert '"admissions" → one inpatient stay' in out
    assert '"last month" → 2026-06-01..2026-06-30' in out
    # 2. sources with confirmation state
    assert "Sources:" in out
    assert "gold_admission (gold, key hadm_id, confirmed by operator)" in out
    # 3. scan -> match funnel, with intermediate stages
    assert "Evidence: 48,112 scanned → 1,284 matched" in out
    assert "3,200 in June" in out
    # 4. caveats
    assert "OPEN RECORDS ×23" in out
    # footer link
    assert out.rstrip().endswith("http://mesh.local/runs/q_full")


def test_fixture_b_line_numbering_sequential():
    out = render_receipt(FIXTURE_B)
    assert "01. Understood:" in out
    assert "02. Sources:" in out
    assert "03. Evidence:" in out
    assert "04. Caveats:" in out


# ── Purity ────────────────────────────────────────────────────────────────────

def test_render_receipt_is_pure_and_deterministic():
    a = render_receipt(FIXTURE_A)
    b = render_receipt(FIXTURE_A)
    assert a == b                      # deterministic
    assert isinstance(a, str)
    # input is not mutated
    assert FIXTURE_A["receipt"]["caveats"][0]["count"] == 2


def test_render_accepts_model_and_dict_alike():
    """A real AnswerEnvelope model renders the same as its dict form."""
    env = build_envelope(
        run_id="q_model", question="admissions by type",
        executed_query="SELECT admission_type, COUNT(*) AS n FROM gold_admission GROUP BY admission_type",
        mode="gold",
        answer={"rows": [{"admission_type": "A", "n": 3}], "row_count": 1},
        settings=Settings())
    from_model = render_receipt(env)
    from_dict = render_receipt(env.model_dump())
    assert from_model == from_dict
    assert "Full receipt:" in from_model
