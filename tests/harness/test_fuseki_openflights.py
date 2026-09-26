# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Live-Fuseki integration test for the openflights named graph.

Connects directly to Fuseki ($FUSEKI_URL, default localhost:3030) via HTTP (no MCP
layer). Requires the openflights pipeline to have been materialised into that
Fuseki, so it is opt-in (like the Synthea gate), and also skipped if Fuseki is
unreachable or the openflights graph is absent:

    OPENFLIGHTS_E2E=1 python -m pytest tests/harness/test_fuseki_openflights.py -v

Assertions:
  1. The graph contains at least one Airport individual (pipeline ran).
  2. Every Airport individual carries at least one non-rdf:type triple
     (TripleBuilder emitted property triples for every row).
"""
from __future__ import annotations

import os

import pytest
import requests

pytestmark = pytest.mark.skipif(
    not os.environ.get("OPENFLIGHTS_E2E"),
    reason="Set OPENFLIGHTS_E2E=1 to run against a materialised openflights graph in Fuseki",
)

FUSEKI_URL  = os.environ.get("FUSEKI_URL", "http://localhost:3030")
DATASET     = "openflights-mesh"
NAMED_GRAPH = "http://mesh.local/graph/openflights"
SPARQL_EP   = f"{FUSEKI_URL}/{DATASET}/sparql"

PREFIXES = """\
PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl:  <http://www.w3.org/2002/07/owl#>
PREFIX xsd:  <http://www.w3.org/2001/XMLSchema#>
PREFIX fm:   <http://forgemesh.ai/ontology#>
PREFIX of:   <http://mesh.local/ontology/openflights#>
PREFIX data: <http://mesh.local/data/>
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def fuseki(request):
    """requests.Session pointed at the local Fuseki instance.

    Skips the entire module if Fuseki is unreachable so the offline suite
    stays green in environments without a running triplestore.
    """
    s = requests.Session()
    s.headers.update({"Accept": "application/sparql-results+json"})
    try:
        s.get(f"{FUSEKI_URL}/$/ping", timeout=2).raise_for_status()
    except Exception:
        pytest.skip(f"Fuseki not reachable at {FUSEKI_URL}")
    try:
        r = s.post(SPARQL_EP, data={"query": f"ASK {{ GRAPH <{NAMED_GRAPH}> {{ ?s ?p ?o }} }}"}, timeout=10)
        r.raise_for_status()
        loaded = r.json().get("boolean", False)
    except Exception:
        loaded = False
    if not loaded:
        pytest.skip(f"openflights graph not materialised in {DATASET} (run: python main.py --project openflights)")
    return s


def _sparql(session: requests.Session, query: str) -> list[dict]:
    """POST a SPARQL SELECT and return rows as plain {var: value} dicts."""
    resp = session.post(
        SPARQL_EP,
        data={"query": PREFIXES + query},
        timeout=30,
    )
    resp.raise_for_status()
    bindings = resp.json()["results"]["bindings"]
    return [
        {k: v["value"] for k, v in row.items()}
        for row in bindings
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_airport_graph_is_populated(fuseki):
    """The openflights named graph must contain at least one Airport individual."""
    rows = _sparql(fuseki, f"""
        SELECT (COUNT(?airport) AS ?airport_count)
        FROM <{NAMED_GRAPH}>
        WHERE {{
            ?airport a of:Airport .
        }}
    """)
    count = int(rows[0]["airport_count"])
    assert count > 0, "No Airport individuals found — has the RDF pipeline been run?"


def test_every_airport_has_non_type_property(fuseki):
    """Every Airport individual must carry at least one triple beyond rdf:type.

    Finds airports that have ONLY type assertions and no other predicate.
    The pipeline is considered correct when this set is empty.
    """
    rows = _sparql(fuseki, f"""
        SELECT ?airport
        FROM <{NAMED_GRAPH}>
        WHERE {{
            ?airport a of:Airport .
            FILTER NOT EXISTS {{
                ?airport ?p ?o .
                FILTER(?p != rdf:type)
            }}
        }}
    """)
    bare = [r["airport"] for r in rows]
    assert bare == [], (
        f"{len(bare)} Airport individual(s) carry no non-type properties.\n"
        f"First 5: {bare[:5]}"
    )
