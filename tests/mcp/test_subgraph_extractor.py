# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 2 tests — extract_subgraph."""
from __future__ import annotations

import pytest

from semantic_mapper.mcp.subgraph_extractor import extract_subgraph
from .conftest import ONT, PATIENT_DEF, ADMISSION_DEF

DEFS = [PATIENT_DEF, ADMISSION_DEF]
PATIENT_URI = f"{ONT}Patient"
ADMISSION_URI = f"{ONT}Admission"


def test_anchor_only_returns_anchor_at_zero_hops():
    result = extract_subgraph([PATIENT_URI], DEFS, max_hops=0)
    uris = {d.primary_class_uri for d in result}
    assert PATIENT_URI in uris


def test_one_hop_from_admission_includes_patient():
    # admission has a "patient" column → object property link to Patient
    result = extract_subgraph([ADMISSION_URI], DEFS, max_hops=1)
    uris = {d.primary_class_uri for d in result}
    assert ADMISSION_URI in uris
    assert PATIENT_URI in uris


def test_one_hop_from_patient_includes_admission():
    # adjacency is bidirectional
    result = extract_subgraph([PATIENT_URI], DEFS, max_hops=1)
    uris = {d.primary_class_uri for d in result}
    assert PATIENT_URI in uris
    assert ADMISSION_URI in uris


def test_empty_anchors_returns_all():
    result = extract_subgraph([], DEFS)
    assert len(result) == len(DEFS)


def test_unknown_uri_anchor_returns_empty_reachable():
    result = extract_subgraph(["http://unknown/class"], DEFS)
    # unknown URI has no definition, BFS finds nothing — fallback not triggered
    # (fallback is only for empty anchor list)
    uris = {d.primary_class_uri for d in result}
    assert len(uris) == 0


def test_returns_definitions_in_original_order():
    result = extract_subgraph([PATIENT_URI, ADMISSION_URI], DEFS, max_hops=1)
    gold_tables = [d.gold_table_name for d in result]
    original_order = [d.gold_table_name for d in DEFS if d.primary_class_uri in {PATIENT_URI, ADMISSION_URI}]
    assert gold_tables == original_order


def test_no_duplicates_in_result():
    result = extract_subgraph([PATIENT_URI, ADMISSION_URI], DEFS, max_hops=2)
    uris = [d.primary_class_uri for d in result]
    assert len(uris) == len(set(uris))


def test_max_hops_zero_returns_only_anchors():
    result = extract_subgraph([ADMISSION_URI], DEFS, max_hops=0)
    uris = {d.primary_class_uri for d in result}
    # With max_hops=0, no neighbours are explored
    assert ADMISSION_URI in uris
    assert PATIENT_URI not in uris
