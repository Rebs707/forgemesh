# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Count-of-groups rewrite: COUNT(DISTINCT ?x) … GROUP BY ?x returns 1 per group."""
from __future__ import annotations

import pytest
from rdflib import Graph, Literal, Namespace, RDF

from semantic_mapper.rdf.query.engine import _fix_count_of_groups

EX = Namespace("http://example.org/")

_DEGENERATE = (
    "PREFIX ont: <http://example.org/>\n"
    "SELECT (COUNT(DISTINCT ?ownerURI) AS ?ownerCount) WHERE { "
    "?itemURI ont:hasOwner ?ownerURI . ?itemURI a ont:Item . } "
    "GROUP BY ?ownerURI HAVING (COUNT(?itemURI) > 2) LIMIT 100"
)


def _graph() -> Graph:
    """Owner o1 has 3 items, o2 has 3, o3 has 1."""
    g = Graph()
    counts = {"o1": 3, "o2": 3, "o3": 1}
    n = 0
    for owner, k in counts.items():
        for _ in range(k):
            item = EX[f"i{n}"]
            g.add((item, RDF.type, EX.Item))
            g.add((item, EX.hasOwner, EX[owner]))
            n += 1
    return g


def test_degenerate_query_returns_one_per_group():
    """Documents the bug: the original shape answers 1, once per qualifying group."""
    rows = list(_graph().query(_DEGENERATE))
    assert [int(r[0]) for r in rows] == [1, 1]


def test_rewrite_counts_groups():
    rewritten, note = _fix_count_of_groups(_DEGENERATE)
    assert note is not None and "Original query" in note
    rows = list(_graph().query(rewritten))
    assert [int(r[0]) for r in rows] == [2]
    assert "LIMIT 100" in rewritten
    assert rewritten.startswith("PREFIX ont:")


def test_rewrite_without_having_counts_distinct_groups():
    q = (
        "PREFIX ont: <http://example.org/>\n"
        "SELECT (COUNT(DISTINCT ?ownerURI) AS ?n) WHERE { ?i ont:hasOwner ?ownerURI } "
        "GROUP BY ?ownerURI"
    )
    rewritten, note = _fix_count_of_groups(q)
    assert note is not None
    assert [int(r[0]) for r in _graph().query(rewritten)] == [3]


@pytest.mark.parametrize("query", [
    # Counting a different variable from the grouping one: a real per-group count.
    "SELECT ?o (COUNT(DISTINCT ?i) AS ?n) WHERE { ?i <http://example.org/hasOwner> ?o } GROUP BY ?o",
    "SELECT (COUNT(DISTINCT ?i) AS ?n) WHERE { ?i <http://example.org/hasOwner> ?o } GROUP BY ?o",
    # COUNT without DISTINCT over the group variable is the group size, not 1.
    "SELECT (COUNT(?o) AS ?n) WHERE { ?i <http://example.org/hasOwner> ?o } GROUP BY ?o",
    # No GROUP BY at all.
    "SELECT (COUNT(DISTINCT ?o) AS ?n) WHERE { ?i <http://example.org/hasOwner> ?o }",
    # Already uses a subquery.
    "SELECT (COUNT(*) AS ?n) WHERE { { SELECT ?o WHERE { ?i <http://example.org/hasOwner> ?o } "
    "GROUP BY ?o HAVING (COUNT(?i) > 2) } }",
])
def test_leaves_other_queries_unchanged(query):
    assert _fix_count_of_groups(query) == (query, None)
