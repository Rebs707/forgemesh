# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""A zero-row answer is only replaced when a regenerated query finds rows, and the
replacement is always disclosed; a FILTER is never silently dropped."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from semantic_mapper.envelope.detectors import DetectorContext, run_detectors
from tests.rdf.query.test_engine import (
    _make_engine, _make_fuseki_response, _make_llm_response,
)

Q1 = ("SELECT ?s WHERE { GRAPH <http://mesh.local/graph/current> "
      "{ ?s <http://ex.org/ontology#hasTag> ?t } } LIMIT 10")
Q2 = ("SELECT ?s WHERE { GRAPH <http://mesh.local/graph/current> "
      "{ ?s a <http://ex.org/ontology#Patient> } } LIMIT 10")
EMPTY = _make_fuseki_response(vars_=["s"], bindings=[])
ONE_ROW = _make_fuseki_response(vars_=["s"], bindings=[{"s": {"type": "uri", "value": "http://ex.org/p1"}}])


def _ask(engine, llm_sparqls, fuseki_responses):
    with patch("openai.OpenAI") as mock_openai_cls:
        client = MagicMock()
        mock_openai_cls.return_value = client
        client.chat.completions.create.side_effect = [_make_llm_response(sparql=s) for s in llm_sparqls]
        with patch("requests.post") as mock_post:
            mock_post.side_effect = fuseki_responses
            return engine.query("Which items carry the tag?")


def test_empty_answer_kept_when_model_returns_the_same_query(simple_definitions, sparql_settings):
    result = _ask(_make_engine(simple_definitions, sparql_settings), [Q1, Q1], [EMPTY])
    assert result.row_count == 0
    assert result.query_rewrites == []


def test_empty_answer_kept_when_rewrite_also_finds_nothing(simple_definitions, sparql_settings):
    result = _ask(_make_engine(simple_definitions, sparql_settings), [Q1, Q2], [EMPTY, EMPTY])
    assert result.row_count == 0
    assert result.query_rewrites == []
    assert "hasTag" in result.generated_sql  # the original query is the one reported


def test_rewrite_that_finds_rows_is_disclosed(simple_definitions, sparql_settings):
    result = _ask(_make_engine(simple_definitions, sparql_settings), [Q1, Q2], [EMPTY, ONE_ROW])
    assert result.row_count == 1
    assert len(result.query_rewrites) == 1
    assert "returned no rows" in result.query_rewrites[0]
    assert "hasTag" in result.query_rewrites[0]  # original query is recorded


def test_rewrite_becomes_a_receipt_caveat():
    ctx = DetectorContext(executed_query=Q2, mode="sparql", columns=["s"], rows=[{"s": "x"}],
                          query_rewrites=["first query returned no rows; regenerated"])
    kinds = {c.kind: c for c in run_detectors(ctx)}
    assert "query_rewritten" in kinds
    assert kinds["query_rewritten"].severity == "warn"


def test_filter_on_uri_kept_when_no_text_property(simple_definitions, sparql_settings):
    engine = _make_engine(simple_definitions, sparql_settings)
    prop = "http://ex.org/ontology#hasItemGroup"
    engine._obj_prop_targets = {prop: "group"}
    engine._entity_text_props = {}  # nothing to redirect the filter to
    sparql = (
        "SELECT ?name WHERE {\n"
        f"  ?itemURI <{prop}> ?groupCode .\n"
        "  FILTER(CONTAINS(LCASE(?groupCode), 'red'))\n"
        "}"
    )
    fixed = engine._fix_object_property_variables(sparql)
    assert "FILTER" in fixed  # narrowing the question is never undone
