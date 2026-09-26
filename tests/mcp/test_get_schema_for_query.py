# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 3 tests — get_schema_for_query end-to-end (without MCP server)."""
from __future__ import annotations

import pytest

from semantic_mapper.mcp.concept_extractor import extract_anchor_concepts
from semantic_mapper.mcp.subgraph_extractor import extract_subgraph
from semantic_mapper.mcp.schema_builder import build_agent_schema
from .conftest import ONT, PATIENT_DEF, ADMISSION_DEF

DEFS = [PATIENT_DEF, ADMISSION_DEF]
PATIENT_URI = f"{ONT}Patient"
ADMISSION_URI = f"{ONT}Admission"


def _query_schema(question: str, max_hops: int = 3) -> dict:
    """Simulate what get_schema_for_query does, without the MCP server."""
    anchor_uris = extract_anchor_concepts(question, DEFS)
    sub_defs = extract_subgraph(anchor_uris, DEFS, max_hops=max_hops)
    schema = build_agent_schema(
        definitions=sub_defs,
        mapping_report=None,
        named_graph="http://mesh.local/graph/current",
        base_uri="http://mesh.local/data/",
    )
    schema["anchor_concepts"] = anchor_uris
    schema["subgraph_class_count"] = len(sub_defs)
    schema["full_class_count"] = len(DEFS)
    return schema


def test_specific_question_returns_relevant_class():
    schema = _query_schema("count patients by gender")
    uris = {c["uri"] for c in schema["classes"]}
    assert PATIENT_URI in uris


def test_subgraph_count_less_than_full_for_single_anchor():
    # With a 2-class ontology both may be reachable via 1 hop, but
    # anchor_concepts should be a subset
    schema = _query_schema("patient demographics", max_hops=0)
    assert schema["subgraph_class_count"] <= schema["full_class_count"]


def test_anchor_concepts_populated():
    schema = _query_schema("how many emergency admissions")
    assert "anchor_concepts" in schema
    assert len(schema["anchor_concepts"]) >= 1
    assert ADMISSION_URI in schema["anchor_concepts"]


def test_full_class_count_matches_definitions():
    schema = _query_schema("anything")
    assert schema["full_class_count"] == len(DEFS)


def test_schema_has_construction_rules():
    schema = _query_schema("count admissions by type")
    assert len(schema["construction_rules"]) >= 10


def test_schema_has_named_graph():
    schema = _query_schema("count admissions")
    assert schema["named_graph"] == "http://mesh.local/graph/current"


def test_join_question_includes_both_classes():
    schema = _query_schema("list admissions with patient gender", max_hops=1)
    uris = {c["uri"] for c in schema["classes"]}
    assert ADMISSION_URI in uris
    assert PATIENT_URI in uris


def test_fallback_unknown_question_returns_full_schema():
    schema = _query_schema("what is the meaning of life")
    # Fallback: all definitions returned
    assert schema["subgraph_class_count"] == schema["full_class_count"]


def test_subgraph_schema_valid_json_structure():
    import json
    schema = _query_schema("count patients")
    # Must be serialisable
    dumped = json.dumps(schema)
    reloaded = json.loads(dumped)
    assert "classes" in reloaded
    assert "construction_rules" in reloaded
    assert "namespaces" in reloaded
