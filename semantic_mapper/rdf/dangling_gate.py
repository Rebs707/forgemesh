# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Post-materialisation gate: every object-property link must resolve to a subject.

An object triple ``?s ?p ?o`` whose object is an IRI must point at an individual
that exists and is typed *somewhere queryable* — either the data graph or the
ontology graph (categorical value individuals live in the latter). A link whose
target is typed in no graph is a dangling reference: it silently breaks every
multi-hop SPARQL join. This gate counts such links and fails the rdf phase when any
are found, so a broken knowledge graph can never ship as a green run.

Union-scoped by design (``GRAPH ?g { ?o a ?t }`` spans all graphs): a value
individual typed only in the ontology graph is resolvable, not dangling; only a
target that exists nowhere is a true dangle.

Cross-dataset reference predicates (SKOS mapping relations, ``owl:sameAs``,
``rdfs:seeAlso``) are exempt: their objects are external IRIs (e.g. terminology
concepts bound via ``skos:exactMatch``) that are deliberately *not* resolvable in
this store, so counting them would fail every dataset with terminology binding.
"""
from __future__ import annotations

import logging
from typing import List, Tuple

logger = logging.getLogger(__name__)

_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

_SKOS = "http://www.w3.org/2004/02/skos/core#"
# Predicates whose objects point outside the dataset by design.
EXTERNAL_REFERENCE_PREDICATES = (
    _SKOS + "exactMatch",
    _SKOS + "closeMatch",
    _SKOS + "broadMatch",
    _SKOS + "narrowMatch",
    _SKOS + "relatedMatch",
    "http://www.w3.org/2002/07/owl#sameAs",
    "http://www.w3.org/2000/01/rdf-schema#seeAlso",
)
_EXCLUDED = ", ".join(f"<{p}>" for p in (_RDF_TYPE, *EXTERNAL_REFERENCE_PREDICATES))


def _count_query(named_graph: str) -> str:
    return f"""
SELECT (COUNT(*) AS ?n) WHERE {{
  GRAPH <{named_graph}> {{
    ?s ?p ?o .
    FILTER(isIRI(?o) && ?p NOT IN ({_EXCLUDED}))
  }}
  FILTER NOT EXISTS {{ GRAPH ?g {{ ?o a ?t }} }}
}}"""


def _by_predicate_query(named_graph: str, limit: int = 10) -> str:
    return f"""
SELECT ?p (COUNT(*) AS ?n) WHERE {{
  GRAPH <{named_graph}> {{
    ?s ?p ?o .
    FILTER(isIRI(?o) && ?p NOT IN ({_EXCLUDED}))
  }}
  FILTER NOT EXISTS {{ GRAPH ?g {{ ?o a ?t }} }}
}} GROUP BY ?p ORDER BY DESC(?n) LIMIT {limit}"""


def _post(settings, sparql: str) -> List[dict]:
    import requests  # lazy import

    resp = requests.post(
        f"{settings.FUSEKI_URL}/{settings.RDF_DATASET_NAME}/sparql",
        data={"query": sparql},
        headers={"Accept": "application/sparql-results+json"},
        auth=(settings.FUSEKI_ADMIN_USER, settings.FUSEKI_ADMIN_PASSWORD),
        timeout=120,
    )
    if not resp.ok:
        raise RuntimeError(f"Fuseki {resp.status_code}: {resp.text.strip()[:400]}")
    data = resp.json()
    vars_ = data["head"]["vars"]
    return [
        {v: b[v]["value"] if v in b else None for v in vars_}
        for b in data["results"]["bindings"]
    ]


def measure_dangling(settings, named_graph: str) -> Tuple[int, List[Tuple[str, int]]]:
    """Return (total_dangling, [(predicate, count), ...]) — union-scoped over all graphs."""
    total = int(_post(settings, _count_query(named_graph))[0]["n"])
    top: List[Tuple[str, int]] = []
    if total:
        top = [
            (r["p"], int(r["n"]))
            for r in _post(settings, _by_predicate_query(named_graph))
        ]
    return total, top


class DanglingLinkError(RuntimeError):
    """Raised when the rdf phase materialised object links with no resolvable target."""


def run_dangling_gate(settings, named_graph: str) -> int:
    """Run the gate after materialisation. Logs PASS/FAIL and, per RDF_DANGLING_GATE,
    raises on failure (``fail``, default) or logs a warning (``warn``); ``off`` skips.
    Returns the dangling count."""
    mode = str(getattr(settings, "RDF_DANGLING_GATE", "fail")).lower()
    if mode == "off":
        logger.info("dangling gate: skipped (RDF_DANGLING_GATE=off)")
        return -1
    total, top = measure_dangling(settings, named_graph)
    if total == 0:
        logger.info("dangling gate: PASS — 0 unresolved object links in <%s>", named_graph)
        return 0
    detail = ", ".join(f"{p.rsplit('#', 1)[-1]}={n}" for p, n in top)
    msg = (
        f"dangling gate: FAIL — {total} object link(s) in <{named_graph}> point at a "
        f"target typed in no graph. Top predicates: {detail}"
    )
    if mode == "warn":
        logger.warning(msg)
        return total
    raise DanglingLinkError(msg)
