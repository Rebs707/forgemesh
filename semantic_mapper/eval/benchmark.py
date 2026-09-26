# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
BenchmarkRunner — issue #51 (Benchmark Query Set).

Loads a YAML benchmark suite and executes each SPARQL query against a
Fuseki endpoint, comparing actual result counts to expected counts.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import requests

from semantic_mapper.eval.models import (
    BenchmarkQuery,
    BenchmarkQueryResult,
    BenchmarkRunResult,
    BenchmarkSuite,
)

logger = logging.getLogger(__name__)


class BenchmarkRunner:
    """Execute a BenchmarkSuite against a SPARQL endpoint."""

    def __init__(self, fuseki_url: str, dataset_name: str, timeout: int = 30):
        self._endpoint = f"{fuseki_url.rstrip('/')}/{dataset_name}/query"
        self._timeout = timeout

    @staticmethod
    def _inject_graph(sparql: str, named_graph: str) -> str:
        """Insert FROM <named_graph> before the WHERE clause if not already present."""
        if "FROM" in sparql.upper() or not named_graph:
            return sparql
        import re
        return re.sub(
            r"(?i)(WHERE\s*\{)",
            f"FROM <{named_graph}>\nWHERE {{",
            sparql,
            count=1,
        )

    def run_suite(
        self, suite: BenchmarkSuite, suite_path: str = ""
    ) -> BenchmarkRunResult:
        results = [
            self._run_one(q, named_graph=suite.named_graph or "")
            for q in suite.queries
        ]
        passed = sum(1 for r in results if r.passed and r.error is None)
        errored = sum(1 for r in results if r.error is not None)
        failed = len(results) - passed - errored
        return BenchmarkRunResult(
            suite_path=suite_path,
            run_at=datetime.now(timezone.utc).isoformat(),
            total=len(results),
            passed=passed,
            failed=failed,
            errored=errored,
            results=results,
        )

    def _run_one(self, q: BenchmarkQuery, named_graph: str = "") -> BenchmarkQueryResult:
        try:
            sparql = self._inject_graph(q.sparql, named_graph)
            resp = requests.post(
                self._endpoint,
                data={"query": sparql},
                headers={"Accept": "application/sparql-results+json"},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            bindings = data.get("results", {}).get("bindings", [])

            # Try to extract a scalar COUNT from the first binding; else use len.
            actual: Optional[int] = None
            if bindings:
                for val in bindings[0].values():
                    try:
                        actual = int(val["value"])
                        break
                    except (ValueError, KeyError, TypeError):
                        pass
            if actual is None:
                actual = len(bindings)

            if q.tolerance > 0:
                passed = abs(actual - q.expected_count) / max(q.expected_count, 1) <= q.tolerance
            else:
                passed = actual == q.expected_count

            return BenchmarkQueryResult(
                query_id=q.id,
                description=q.description,
                expected_count=q.expected_count,
                actual_count=actual,
                passed=passed,
            )
        except Exception as exc:
            logger.warning("Benchmark query %s failed: %s", q.id, exc)
            return BenchmarkQueryResult(
                query_id=q.id,
                description=q.description,
                expected_count=q.expected_count,
                actual_count=None,
                passed=False,
                error=str(exc),
            )

    @staticmethod
    def load_suite(path: str) -> BenchmarkSuite:
        import yaml  # optional dep; only needed when loading suites

        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return BenchmarkSuite.model_validate(data)
