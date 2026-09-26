# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""C4 — Provenance completeness.

For every class that has typed subjects in the named graph, count how many of
those subjects also carry the ``fm:bronzeSourceRef`` provenance predicate.  The
two counts must be equal.

A mismatch means the materialiser dropped the audit columns on some triples —
those subjects are queryable but untraceable, breaking ``prove_answer``
lineage.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from semantic_mapper.gold.builder import GoldDefinitionBuilder

from ..context import ValidationContext
from ..models import CheckResult
from .base import BaseCheck

logger = logging.getLogger(__name__)

_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_FM_BRONZE_SOURCE_REF = "http://forgemesh.ai/ontology#bronzeSourceRef"


class ProvenanceCheck(BaseCheck):
    """C4 — provenance completeness."""

    CHECK_ID = "C4"
    CHECK_NAME = "provenance_completeness"

    def run(self, ctx: ValidationContext) -> CheckResult:  # noqa: D102
        try:
            return self._run(ctx)
        except Exception as exc:
            logger.exception("C4 error")
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
        all_tables: List[Dict[str, Any]] = []

        for defn in definitions:
            class_uri = defn.primary_class_uri

            # Count all typed subjects
            total_rows = ctx.sparql.query(
                f"""
                SELECT (COUNT(DISTINCT ?s) AS ?cnt)
                FROM <{ctx.graph_uri}>
                WHERE {{
                    ?s <{_RDF_TYPE}> <{class_uri}> .
                }}
                """
            )
            total = int(total_rows[0].get("cnt", 0)) if total_rows else 0

            if total == 0:
                # No triples for this class — nothing to check
                continue

            # Count subjects that carry the provenance predicate
            prov_rows = ctx.sparql.query(
                f"""
                SELECT (COUNT(DISTINCT ?s) AS ?cnt)
                FROM <{ctx.graph_uri}>
                WHERE {{
                    ?s <{_RDF_TYPE}> <{class_uri}> .
                    ?s <{_FM_BRONZE_SOURCE_REF}> ?ref .
                }}
                """
            )
            with_prov = int(prov_rows[0].get("cnt", 0)) if prov_rows else 0

            entry: Dict[str, Any] = {
                "class": class_uri,
                "gold_table": defn.gold_table_name,
                "total_subjects": total,
                "with_provenance": with_prov,
            }
            all_tables.append(entry)

            if with_prov != total:
                mismatches.append(entry)

        if mismatches:
            lines = [
                f"{m['gold_table']}: {m['with_provenance']}/{m['total_subjects']} have provenance"
                for m in mismatches
            ]
            return CheckResult(
                check_id=self.CHECK_ID,
                check_name=self.CHECK_NAME,
                passed=False,
                message=f"{len(mismatches)} class(es) with missing provenance: " + "; ".join(lines),
                details={"mismatches": mismatches, "all_tables": all_tables},
            )

        return CheckResult(
            check_id=self.CHECK_ID,
            check_name=self.CHECK_NAME,
            passed=True,
            message=f"provenance complete for all {len(all_tables)} class(es)",
            details={"all_tables": all_tables},
        )
