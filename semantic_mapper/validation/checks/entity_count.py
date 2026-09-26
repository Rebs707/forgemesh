# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""C1 — Entity-count reconciliation.

For each gold table, compare the number of distinct typed subjects in the named
graph against the number of rows in the gold table.  A mismatch indicates a
poor identifier choice (row collapse), a missing materialisation batch, or an
out-of-sync gold layer.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from sqlalchemy import text

from semantic_mapper.gold.builder import GoldDefinitionBuilder

from ..context import ValidationContext
from ..models import CheckResult
from .base import BaseCheck

logger = logging.getLogger(__name__)

_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"


class EntityCountCheck(BaseCheck):
    """C1 — entity count reconciliation."""

    CHECK_ID = "C1"
    CHECK_NAME = "entity_count_reconciliation"

    def run(self, ctx: ValidationContext) -> CheckResult:  # noqa: D102
        try:
            return self._run(ctx)
        except Exception as exc:
            logger.exception("C1 error")
            return CheckResult(
                check_id=self.CHECK_ID,
                check_name=self.CHECK_NAME,
                passed=False,
                message=f"check errored: {exc}",
            )

    def _run(self, ctx: ValidationContext) -> CheckResult:
        definitions = GoldDefinitionBuilder().build(
            ctx.mapping_report,
            ontology_graph=ctx.ontology,
        )

        mismatches: List[Dict[str, Any]] = []
        table_results: List[Dict[str, Any]] = []

        for defn in definitions:
            class_uri = defn.primary_class_uri
            gold_table = defn.gold_table_name

            # SPARQL — distinct typed subjects
            sparql_rows = ctx.sparql.query(
                f"""
                SELECT (COUNT(DISTINCT ?s) AS ?cnt)
                FROM <{ctx.graph_uri}>
                WHERE {{
                    ?s <{_RDF_TYPE}> <{class_uri}> .
                }}
                """
            )
            graph_count = int(sparql_rows[0].get("cnt", 0)) if sparql_rows else 0

            # SQL — gold table row count
            try:
                with ctx.gold_engine.connect() as conn:
                    sql_result = conn.execute(
                        text(f"SELECT COUNT(*) AS cnt FROM {gold_table}")  # noqa: S608
                    )
                    gold_count = sql_result.scalar() or 0
            except Exception as sql_exc:
                logger.warning("C1: could not query %s: %s", gold_table, sql_exc)
                gold_count = None

            row: Dict[str, Any] = {
                "class": class_uri,
                "gold_table": gold_table,
                "graph_count": graph_count,
                "gold_count": gold_count,
            }
            table_results.append(row)

            if gold_count is not None and graph_count != gold_count:
                mismatches.append(row)

        if mismatches:
            lines = [
                f"{m['gold_table']}: graph={m['graph_count']} gold={m['gold_count']}"
                for m in mismatches
            ]
            return CheckResult(
                check_id=self.CHECK_ID,
                check_name=self.CHECK_NAME,
                passed=False,
                message=f"{len(mismatches)} entity count mismatch(es): " + "; ".join(lines),
                details={"mismatches": mismatches, "all_tables": table_results},
            )

        return CheckResult(
            check_id=self.CHECK_ID,
            check_name=self.CHECK_NAME,
            passed=True,
            message=f"all {len(table_results)} entity count(s) reconciled",
            details={"all_tables": table_results},
        )
