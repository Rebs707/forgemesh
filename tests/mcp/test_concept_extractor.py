# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 1 tests — extract_anchor_concepts."""
from __future__ import annotations

import pytest

from semantic_mapper.mcp.concept_extractor import extract_anchor_concepts
from .conftest import ONT, PATIENT_DEF, ADMISSION_DEF

DEFS = [PATIENT_DEF, ADMISSION_DEF]
PATIENT_URI = f"{ONT}Patient"
ADMISSION_URI = f"{ONT}Admission"


def test_exact_match_entity_short_name():
    anchors = extract_anchor_concepts("count patients by gender", DEFS)
    assert PATIENT_URI in anchors


def test_exact_match_admission():
    anchors = extract_anchor_concepts("emergency admissions this month", DEFS)
    assert ADMISSION_URI in anchors


def test_both_classes_for_join_question():
    anchors = extract_anchor_concepts("list admissions for each patient", DEFS)
    assert PATIENT_URI in anchors
    assert ADMISSION_URI in anchors


def test_fuzzy_match_plural():
    anchors = extract_anchor_concepts("how many admissions were there", DEFS)
    assert ADMISSION_URI in anchors


def test_fuzzy_match_partial_word():
    anchors = extract_anchor_concepts("patient demographics", DEFS)
    assert PATIENT_URI in anchors


def test_fallback_unknown_question_returns_all():
    anchors = extract_anchor_concepts("what is the weather today", DEFS)
    assert PATIENT_URI in anchors
    assert ADMISSION_URI in anchors
    assert len(anchors) == len(DEFS)


def test_empty_question_returns_all():
    anchors = extract_anchor_concepts("", DEFS)
    assert len(anchors) == len(DEFS)


def test_no_duplicates():
    anchors = extract_anchor_concepts("patient and admission and patient", DEFS)
    assert len(anchors) == len(set(anchors))


def test_returns_list_of_uris():
    anchors = extract_anchor_concepts("count patients", DEFS)
    assert isinstance(anchors, list)
    assert all(isinstance(u, str) for u in anchors)
    assert all(u.startswith("http") for u in anchors)


def test_no_ontology_graph_still_works():
    anchors = extract_anchor_concepts("patient data", DEFS, ontology_graph=None)
    assert PATIENT_URI in anchors
