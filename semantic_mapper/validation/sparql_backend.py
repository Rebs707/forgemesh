# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""SPARQL backend abstraction — rdflib for tests, Fuseki for integration."""
from __future__ import annotations

from typing import Any, Dict, List, Protocol, runtime_checkable


@runtime_checkable
class SparqlBackend(Protocol):
    """Minimal SPARQL SELECT interface shared by rdflib and Fuseki backends."""

    def query(self, sparql: str) -> List[Dict[str, Any]]: ...


# ── rdflib backend (unit tests) ───────────────────────────────────────────────

class RdflibBackend:
    """Runs SPARQL SELECT against an in-memory rdflib graph."""

    def __init__(self, graph: Any) -> None:
        # Accepts ConjunctiveGraph or Dataset
        self._g = graph

    def query(self, sparql: str) -> List[Dict[str, Any]]:
        results = self._g.query(sparql)
        rows: List[Dict[str, Any]] = []
        for row in results:
            record: Dict[str, Any] = {}
            for var in results.vars:
                val = row[var]
                if val is None:
                    continue
                from rdflib import Literal, URIRef
                if isinstance(val, Literal):
                    record[str(var)] = val.toPython()
                elif isinstance(val, URIRef):
                    record[str(var)] = str(val)
                else:
                    record[str(var)] = str(val)
            rows.append(record)
        return rows


# ── Fuseki backend (integration) ─────────────────────────────────────────────

class FusekiBackend:
    """Runs SPARQL SELECT against a live Fuseki dataset."""

    def __init__(self, fuseki_url: str, dataset_name: str, timeout: int = 300) -> None:
        self._endpoint = f"{fuseki_url.rstrip('/')}/{dataset_name}/sparql"
        self._timeout = timeout
        # 300 s default — COUNT(DISTINCT) queries over MIMIC-scale named graphs
        # (millions of triples) can take several minutes on a local Fuseki instance.
        # 30 s was too short and caused C4 provenance check to error on large datasets.

    def query(self, sparql: str) -> List[Dict[str, Any]]:
        import requests
        resp = requests.post(
            self._endpoint,
            data={"query": sparql},
            headers={"Accept": "application/sparql-results+json"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        # SPARQL 1.1 JSON format: vars live under "head", bindings under "results"
        # https://www.w3.org/TR/sparql11-results-json/
        vars_ = data["head"]["vars"]
        return [
            {v: b[v]["value"] for v in vars_ if v in b}
            for b in data["results"]["bindings"]
        ]
