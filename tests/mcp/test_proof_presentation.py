# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for ProofPresentation contract (Feature 13).

Coverage:
- Model is frozen (immutable)
- All seven sections present in correct order
- build_presentation with v2-only data
- build_presentation with v1-only data
- build_presentation with both v2 and v1 data
- build_presentation with neither source (graceful fallback)
- Limitations merged from both layers, deduplicated
- render_presentation produces ordered markdown
- prove_answer result includes proof_presentation key
- Version field presence and value
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from semantic_mapper.prove.presentation import (
    ProofPresentation,
    build_presentation,
)
from semantic_mapper.prove.render.markdown import render_presentation


# ---------------------------------------------------------------------------
# Fixtures: minimal v2 and v1 proof dicts
# ---------------------------------------------------------------------------

NS = "http://test.local/ont#"
PATIENT_URI = f"{NS}Patient"
ADMISSION_URI = f"{NS}Admission"
FIRST_NAME_URI = f"{NS}firstName"
HAS_ENCOUNTER_URI = f"{NS}hasEncounter"

_SPARQL = f"SELECT (COUNT(?s) AS ?n) WHERE {{ ?s a <{ADMISSION_URI}> . }}"
_NL = "How many admissions were there?"


def _v2_proof(cardinality: int = 60) -> Dict[str, Any]:
    return {
        "proof_schema_version": "2.0.0",
        "query_id": "q_test",
        "interpretation": {
            "nl_question": _NL,
            "confidence": "HIGH",
            "confidence_reason": "all concepts directly mapped via fm:interpretation",
            "interpreted_concepts": [
                {
                    "nl_phrase": "admissions",
                    "mapped_to_uri": ADMISSION_URI,
                    "mapping_form": "DIRECT",
                    "grounding_annotation": "a hospital admission event",
                    "grounding_node_uri": ADMISSION_URI,
                }
            ],
        },
        "framing": {
            "cardinality": cardinality,
            "primary_class_uri": ADMISSION_URI,
            "joined_class_uris": [],
            "constraints_applied": [],
        },
        "scope_assumptions": [
            {
                "unconstrained_property_uri": FIRST_NAME_URI,
                "property_label": "firstName",
                "implied_inclusion": "All first names included",
            }
        ],
        "limitations": [
            {"kind": "OPEN_WORLD", "text": "Absence of a triple does not mean absence in reality."},
        ],
        "ontology_subgraph_hash": "abc123",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _v1_proof(subject_count: int = 60) -> Dict[str, Any]:
    from semantic_mapper.prove.models import REQUIRED_LIMITATIONS
    return {
        "proof_schema_version": "1.0.0",
        "query_mode": "sparql",
        "question": _NL,
        "answer": {"value": subject_count, "type": "count"},
        "interpretation": {
            "interpreted_as": "Counting hospital admission events",
            "scope_assumptions": [],
            "not_included": [],
            "interpretation_confidence": "HIGH",
            "interpretation_confidence_reason": "",
            "narrated": True,
            "ontology_subgraph_attached": False,
        },
        "ontology_subgraph": {"classes": {}, "properties": {}, "adjacency_notes": []},
        "executed_query": _SPARQL,
        "proof_query": "SELECT _gold_id FROM gold_admission",
        "subject_count": subject_count,
        "subjects": [],
        "lineage": [
            {
                "gold": {"table": "gold_admission", "id": 1, "row_values": {"_gold_id": 1, "admission_type": "EMERGENCY"}},
                "silver": [{"table": "silver_admission", "id": 1, "row_values": {"admission_type": "EMERGENCY"}}],
                "bronze": [{"table": "admissions", "pk": {"hadm_id": 100}, "raw_values": {"hadm_id": 100, "admittime": "2023-01-01"}}],
                "broken": False,
            }
        ],
        "mapping_decisions": [
            {"uri": f"{NS}admissionType", "confirmation": "HUMAN", "score": 0.98, "winning_strategy": "AnnotationMatcher"},
        ],
        "caveats": [
            {"type": "QUARANTINED", "count": 3, "table": "gold_admission", "property_uri": None, "reason": None},
        ],
        "limitations": list(REQUIRED_LIMITATIONS),
        "mapping_report_id": "run_test",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# TestProofPresentationModel
# ---------------------------------------------------------------------------

class TestProofPresentationModel:
    def test_frozen(self):
        pres = build_presentation(
            v2_proof=_v2_proof(),
            v1_proof_dict=_v1_proof(),
            executed_query=_SPARQL,
            nl_question=_NL,
        )
        with pytest.raises((TypeError, AttributeError, Exception)):
            pres.limitations = []

    def test_version_field_present(self):
        pres = build_presentation(None, None, _SPARQL, _NL)
        assert pres.proof_presentation_version == "1.0.0"

    def test_all_seven_sections_present(self):
        pres = build_presentation(
            v2_proof=_v2_proof(),
            v1_proof_dict=_v1_proof(),
            executed_query=_SPARQL,
            nl_question=_NL,
        )
        assert pres.question_interpretation
        assert pres.query_display is not None
        assert pres.ontology_grounding is not None
        assert pres.data_layer_trace is not None
        assert pres.transformation_provenance is not None
        assert pres.result_confidence is not None
        assert pres.limitations is not None

    def test_section_order_matches_contract(self):
        # Field order in Pydantic model mirrors contract section order.
        fields = list(ProofPresentation.model_fields.keys())
        expected_order = [
            "proof_presentation_version",
            "question_interpretation",
            "query_display",
            "ontology_grounding",
            "data_layer_trace",
            "transformation_provenance",
            "result_confidence",
            "limitations",
        ]
        assert fields == expected_order


# ---------------------------------------------------------------------------
# TestBuildPresentation — data extraction
# ---------------------------------------------------------------------------

class TestBuildPresentationV2Only:
    def test_question_interpretation_cites_confidence(self):
        pres = build_presentation(_v2_proof(), None, _SPARQL, _NL)
        assert "HIGH" in pres.question_interpretation

    def test_question_interpretation_cites_grounding_annotation(self):
        pres = build_presentation(_v2_proof(), None, _SPARQL, _NL)
        assert "hospital admission event" in pres.question_interpretation

    def test_ontology_grounding_has_class(self):
        pres = build_presentation(_v2_proof(), None, _SPARQL, _NL)
        assert ADMISSION_URI in pres.ontology_grounding.classes_used

    def test_ontology_grounding_scope_properties_not_reported(self):
        # scope_assumptions are no longer included — only actual BGP predicates.
        # _SPARQL has no ?s <URI> ?o triples, so properties_used is empty.
        pres = build_presentation(_v2_proof(), None, _SPARQL, _NL)
        prop_uris = [p.uri for p in pres.ontology_grounding.properties_used]
        assert FIRST_NAME_URI not in prop_uris

    def test_ontology_grounding_bgp_predicates_reported(self):
        # A SPARQL with a real BGP predicate triple must appear in properties_used.
        sparql_with_prop = (
            f"SELECT ?n WHERE {{ ?s a <{ADMISSION_URI}> . "
            f"?s <{HAS_ENCOUNTER_URI}> ?enc . }}"
        )
        pres = build_presentation(_v2_proof(), None, sparql_with_prop, _NL)
        prop_uris = [p.uri for p in pres.ontology_grounding.properties_used]
        assert HAS_ENCOUNTER_URI in prop_uris
        assert FIRST_NAME_URI not in prop_uris

    def test_result_confidence_cardinality(self):
        pres = build_presentation(_v2_proof(cardinality=60), None, _SPARQL, _NL)
        assert pres.result_confidence.row_count == 60

    def test_v2_limitations_included(self):
        pres = build_presentation(_v2_proof(), None, _SPARQL, _NL)
        assert any("triple" in lim for lim in pres.limitations)

    def test_layer_trace_empty_without_v1(self):
        pres = build_presentation(_v2_proof(), None, _SPARQL, _NL)
        assert pres.data_layer_trace == []

    def test_transformation_provenance_empty_without_v1(self):
        pres = build_presentation(_v2_proof(), None, _SPARQL, _NL)
        assert pres.transformation_provenance == []


class TestBuildPresentationV1Only:
    def test_question_interpretation_falls_back_to_v1(self):
        pres = build_presentation(None, _v1_proof(), _SPARQL, _NL)
        assert "Counting hospital admission events" in pres.question_interpretation

    def test_result_confidence_uses_subject_count(self):
        pres = build_presentation(None, _v1_proof(subject_count=42), _SPARQL, _NL)
        assert pres.result_confidence.row_count == 42

    def test_quarantined_rows_extracted(self):
        pres = build_presentation(None, _v1_proof(), _SPARQL, _NL)
        assert pres.result_confidence.quarantined_row_count == 3

    def test_layer_trace_gold_silver_bronze(self):
        pres = build_presentation(None, _v1_proof(), _SPARQL, _NL)
        layers = [lt.layer for lt in pres.data_layer_trace]
        assert "gold" in layers
        assert "silver" in layers
        assert "bronze" in layers

    def test_layer_trace_gold_table_name(self):
        pres = build_presentation(None, _v1_proof(), _SPARQL, _NL)
        gold_traces = [lt for lt in pres.data_layer_trace if lt.layer == "gold"]
        assert any(lt.table_name == "gold_admission" for lt in gold_traces)

    def test_layer_trace_bronze_columns(self):
        pres = build_presentation(None, _v1_proof(), _SPARQL, _NL)
        bronze = [lt for lt in pres.data_layer_trace if lt.layer == "bronze"][0]
        assert "hadm_id" in bronze.column_names

    def test_transformation_provenance_has_record(self):
        pres = build_presentation(None, _v1_proof(), _SPARQL, _NL)
        assert len(pres.transformation_provenance) == 1
        tr = pres.transformation_provenance[0]
        assert tr.confirmation == "HUMAN"
        assert tr.score == pytest.approx(0.98)
        assert tr.rule_reference == "AnnotationMatcher"

    def test_v1_limitations_included(self):
        from semantic_mapper.prove.models import REQUIRED_LIMITATIONS
        pres = build_presentation(None, _v1_proof(), _SPARQL, _NL)
        for req_lim in REQUIRED_LIMITATIONS:
            assert any(req_lim in lim for lim in pres.limitations)


class TestBuildPresentationBoth:
    def test_v2_preferred_for_cardinality(self):
        pres = build_presentation(_v2_proof(cardinality=60), _v1_proof(subject_count=99), _SPARQL, _NL)
        assert pres.result_confidence.row_count == 60

    def test_limitations_deduplicated(self):
        v2 = _v2_proof()
        # v1 has REQUIRED_LIMITATIONS; v2 has one limitation text
        pres = build_presentation(v2, _v1_proof(), _SPARQL, _NL)
        # No duplicates
        assert len(pres.limitations) == len(set(pres.limitations))

    def test_limitations_last_section_non_empty(self):
        pres = build_presentation(_v2_proof(), _v1_proof(), _SPARQL, _NL)
        assert len(pres.limitations) > 0

    def test_query_display_sparql(self):
        pres = build_presentation(_v2_proof(), _v1_proof(), _SPARQL, _NL)
        assert pres.query_display.sparql == _SPARQL
        assert pres.query_display.plain_english_summary == _NL

    def test_cache_status_fresh(self):
        pres = build_presentation(_v2_proof(), _v1_proof(), _SPARQL, _NL)
        assert pres.query_display.cache_status == "fresh"


class TestBuildPresentationFallback:
    def test_neither_source_produces_valid_presentation(self):
        pres = build_presentation(None, None, _SPARQL, _NL)
        assert isinstance(pres, ProofPresentation)
        assert pres.question_interpretation == _NL  # falls back to nl_question
        assert pres.data_layer_trace == []
        assert pres.transformation_provenance == []
        assert pres.limitations == []


# ---------------------------------------------------------------------------
# TestRenderPresentation — markdown rendering
# ---------------------------------------------------------------------------

class TestRenderPresentation:
    def _build(self) -> ProofPresentation:
        return build_presentation(_v2_proof(), _v1_proof(), _SPARQL, _NL)

    def test_render_is_deterministic(self):
        pres = self._build()
        assert render_presentation(pres) == render_presentation(pres)

    def test_render_contains_all_section_headers(self):
        md = render_presentation(self._build())
        for header in [
            "## 1. Question interpretation",
            "## 2. Query executed",
            "## 3. Ontology grounding",
            "## 4. Data layer trace",
            "## 5. Transformation provenance",
            "## 6. Result confidence",
            "## 7. Limitations",
        ]:
            assert header in md, f"Missing section: {header}"

    def test_render_section_order(self):
        md = render_presentation(self._build())
        positions = [md.index(f"## {i}.") for i in range(1, 8)]
        assert positions == sorted(positions)

    def test_render_sparql_in_code_block(self):
        md = render_presentation(self._build())
        assert "```sparql" in md or "```sql" in md

    def test_render_limitations_non_empty(self):
        md = render_presentation(self._build())
        lim_idx = md.index("## 7. Limitations")
        lim_section = md[lim_idx:]
        assert "-" in lim_section  # at least one bullet point


# ---------------------------------------------------------------------------
# TestProveAnswerIncludesPresentation — integration
# ---------------------------------------------------------------------------

class TestProveAnswerIncludesPresentation:
    """prove_answer result must include proof_presentation when data is available."""

    @pytest.fixture
    def store_settings(self, tmp_path):
        from semantic_mapper.config.settings import Settings
        return Settings(
            DATABASE_URL="sqlite:///:memory:",
            SILVER_DATABASE_URL="sqlite:///:memory:",
            GOLD_DATABASE_URL="sqlite:///:memory:",
            CORRECTIONS_DB_PATH=str(tmp_path / "corrections.db"),
            FORGEMESH_WEB_APP_URL="http://localhost:8000",
        )

    def _make_and_write(self, query_id, store_settings):
        from datetime import datetime, timezone
        from semantic_mapper.mcp.query_store import MCPQueryRecord, ensure_table, write_query
        ensure_table(store_settings)
        record = MCPQueryRecord(
            query_id=query_id,
            session_id="sess_pres_test",
            question=_NL,
            executed_query=_SPARQL,
            mode="sparql",
            answer={"rows": [{"n": 60}], "row_count": 1},
            run_id="run_pres_test",
            created_at=datetime.now(timezone.utc).isoformat(),
            proof_record_json=json.dumps(_v2_proof()),
        )
        write_query(record, store_settings)

    def _mock_resp(self, data, status_code=200):
        m = MagicMock()
        m.ok = (status_code < 400)
        m.status_code = status_code
        m.json.return_value = data
        m.text = json.dumps(data)
        return m

    def test_proof_presentation_key_present(self, store_settings):
        from semantic_mapper.mcp.tools.prove_answer import run_prove_answer
        self._make_and_write("q_pres001", store_settings)
        with patch("semantic_mapper.mcp.tools.prove_answer.requests.post") as mock_post:
            mock_post.return_value = self._mock_resp(_v1_proof())
            result = run_prove_answer("q_pres001", store_settings)
        assert "proof_presentation" in result

    def test_proof_presentation_version(self, store_settings):
        from semantic_mapper.mcp.tools.prove_answer import run_prove_answer
        self._make_and_write("q_pres002", store_settings)
        with patch("semantic_mapper.mcp.tools.prove_answer.requests.post") as mock_post:
            mock_post.return_value = self._mock_resp(_v1_proof())
            result = run_prove_answer("q_pres002", store_settings)
        assert result["proof_presentation"]["proof_presentation_version"] == "1.0.0"

    def test_proof_presentation_has_all_sections(self, store_settings):
        from semantic_mapper.mcp.tools.prove_answer import run_prove_answer
        self._make_and_write("q_pres003", store_settings)
        with patch("semantic_mapper.mcp.tools.prove_answer.requests.post") as mock_post:
            mock_post.return_value = self._mock_resp(_v1_proof())
            result = run_prove_answer("q_pres003", store_settings)
        pp = result["proof_presentation"]
        for section in [
            "question_interpretation", "query_display", "ontology_grounding",
            "data_layer_trace", "transformation_provenance",
            "result_confidence", "limitations",
        ]:
            assert section in pp, f"Missing section: {section}"

    def test_proof_presentation_limitations_last_key(self, store_settings):
        from semantic_mapper.mcp.tools.prove_answer import run_prove_answer
        self._make_and_write("q_pres004", store_settings)
        with patch("semantic_mapper.mcp.tools.prove_answer.requests.post") as mock_post:
            mock_post.return_value = self._mock_resp(_v1_proof())
            result = run_prove_answer("q_pres004", store_settings)
        pp_keys = list(result["proof_presentation"].keys())
        assert pp_keys[-1] == "limitations"

    def test_proof_presentation_present_even_without_lineage(self, store_settings):
        from semantic_mapper.mcp.tools.prove_answer import run_prove_answer
        self._make_and_write("q_pres005", store_settings)
        with patch("semantic_mapper.mcp.tools.prove_answer.requests.post") as mock_post:
            mock_post.return_value = self._mock_resp(
                {"detail": {"error": "bronze_unavailable"}}, status_code=503
            )
            result = run_prove_answer("q_pres005", store_settings)
        # semantic_proof is present (stored), lineage unavailable — presentation still built
        assert "proof_presentation" in result
