# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""C3 — Join integrity.

For each ``owl:ObjectProperty`` whose ``rdfs:range`` is a class that has a
corresponding gold table, execute the join in the named graph and assert it
returns at least one row.

A declared join that returns zero rows is the *airport-to-country URI-scheme
mismatch* signature — the property triple is present, but the object URIs were
minted under a different scheme from the typed subjects, so the join silently
resolves to nothing.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from rdflib import OWL, RDF, RDFS, URIRef

from semantic_mapper.gold.builder import GoldDefinitionBuilder

from ..context import ValidationContext
from ..models import CheckResult
from .base import BaseCheck

logger = logging.getLogger(__name__)

_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"


class JoinIntegrityCheck(BaseCheck):
    """C3 — join integrity."""

    CHECK_ID = "C3"
    CHECK_NAME = "join_integrity"

    def run(self, ctx: ValidationContext) -> CheckResult:  # noqa: D102
        try:
            return self._run(ctx)
        except Exception as exc:
            logger.exception("C3 error")
            return CheckResult(
                check_id=self.CHECK_ID,
                check_name=self.CHECK_NAME,
                passed=False,
                message=f"check errored: {exc}",
            )

    def _run(self, ctx: ValidationContext) -> CheckResult:
        if ctx.ontology is None:
            return CheckResult(
                check_id=self.CHECK_ID,
                check_name=self.CHECK_NAME,
                passed=False,
                message="no ontology provided — cannot determine join properties",
            )

        # Build gold class URI set from definitions
        definitions = GoldDefinitionBuilder().build(
            ctx.mapping_report,
            ontology_graph=ctx.ontology,
        )
        gold_class_uris: Set[str] = {d.primary_class_uri for d in definitions}

        # Find object properties whose range class has a gold table
        join_props: List[Dict[str, str]] = []
        for prop, _, range_cls in ctx.ontology.triples((None, RDFS.range, None)):
            # Only consider owl:ObjectProperty declarations
            if (prop, RDF.type, OWL.ObjectProperty) not in ctx.ontology:
                continue
            range_uri = str(range_cls)
            if range_uri in gold_class_uris:
                join_props.append({
                    "property": str(prop),
                    "range_class": range_uri,
                })

        if not join_props:
            return CheckResult(
                check_id=self.CHECK_ID,
                check_name=self.CHECK_NAME,
                passed=True,
                message="no FK object properties with known range classes found — check skipped",
            )

        broken: List[Dict[str, Any]] = []
        all_joins: List[Dict[str, Any]] = []

        for jp in join_props:
            prop_uri = jp["property"]
            range_uri = jp["range_class"]

            rows = ctx.sparql.query(
                f"""
                SELECT (COUNT(*) AS ?cnt)
                FROM <{ctx.graph_uri}>
                WHERE {{
                    ?s <{prop_uri}> ?o .
                    ?o <{_RDF_TYPE}> <{range_uri}> .
                }}
                """
            )
            cnt = int(rows[0].get("cnt", 0)) if rows else 0

            # Also count how many subjects *have* the property (to detect unused joins vs broken ones)
            usage_rows = ctx.sparql.query(
                f"""
                SELECT (COUNT(*) AS ?cnt)
                FROM <{ctx.graph_uri}>
                WHERE {{
                    ?s <{prop_uri}> ?o .
                }}
                """
            )
            usage_cnt = int(usage_rows[0].get("cnt", 0)) if usage_rows else 0

            entry: Dict[str, Any] = {
                "property": prop_uri,
                "range_class": range_uri,
                "join_count": cnt,
                "usage_count": usage_cnt,
            }
            all_joins.append(entry)

            # Only flag as broken if the property is actually used but the join returns nothing
            if usage_cnt > 0 and cnt == 0:
                broken.append(entry)

        if broken:
            lines = [
                f"{_local(b['property'])} → {_local(b['range_class'])}: "
                f"{b['usage_count']} triples, 0 resolved"
                for b in broken
            ]
            return CheckResult(
                check_id=self.CHECK_ID,
                check_name=self.CHECK_NAME,
                passed=False,
                message=f"{len(broken)} broken join(s): " + "; ".join(lines),
                details={"broken": broken, "all_joins": all_joins},
            )

        return CheckResult(
            check_id=self.CHECK_ID,
            check_name=self.CHECK_NAME,
            passed=True,
            message=f"all {len(all_joins)} FK join(s) resolve correctly",
            details={"all_joins": all_joins},
        )


def _local(uri: str) -> str:
    return uri.split("#")[-1] if "#" in uri else uri.split("/")[-1]
