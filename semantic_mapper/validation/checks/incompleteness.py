# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""C5 — Incompleteness reconciliation.

The materialiser emits an ``fm:incompleteReference`` quality triple for every
null FK value it encounters AND for every non-null value_map entry that resolves
to an OWL CLASS URI instead of an individual URI (FK-to-missing-class fallback).
See ``triple_builder.py`` for both emission paths.

This check asserts that the count of such triples in the named graph equals the
count of incomplete references across all FK-like columns in the gold tables.

A mismatch in either direction is a bug:

* More in graph than gold  — the materialiser is emitting spurious quality
  triples.
* More in gold than graph  — the materialiser silently dropped some null FK
  rows without recording the incompleteness.

Two sources of incompleteness are counted on the SQL side:

1. Null values in FK columns (ObjectProperty with gold-backed range class).
2. Non-null values in value_map columns where the mapped URI is an OWL class,
   not an individual (FK-to-missing-class: target entity has no gold table).
   For each such column, the SQL counts rows whose gold value appears as a key
   in the subset of value_map entries that map to class URIs.

**Null datatype literals are intentionally excluded from C5.**

A null value in a DatatypeProperty column (e.g. ``reasoncode``, ``description``)
produces no triple at all — the absence of the triple is the correct semantic
representation.  This differs from the FK / ObjectProperty case, where the
*predicate* exists but the *target individual* is missing; the structural gap
warrants an explicit quality triple.

Consumers that need to distinguish "no reason code recorded" from "reason code
not applicable" should use ``FILTER NOT EXISTS { ?s :hasReasonCode ?v }`` in
their SPARQL queries — the standard RDF open-world pattern.  Emitting
``fm:incompleteReference`` for every nullable column would add O(N × nullable
columns) quality triples to the graph without enabling queries that are not
already expressible with ``FILTER NOT EXISTS``.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

from rdflib import OWL, RDF, RDFS, URIRef
from sqlalchemy import inspect, text

from semantic_mapper.gold.builder import GoldDefinitionBuilder
from semantic_mapper.gold.models import GoldColumnSpec

from ..context import ValidationContext
from ..models import CheckResult
from .base import BaseCheck

logger = logging.getLogger(__name__)

_FM_INCOMPLETE   = "http://forgemesh.ai/ontology#incompleteReference"
_FM_INVALID_MAPPING = "http://forgemesh.ai/ontology#invalidMapping"


class IncompletenessCheck(BaseCheck):
    """C5 — incompleteness reconciliation."""

    CHECK_ID = "C5"
    CHECK_NAME = "incompleteness_reconciliation"

    def run(self, ctx: ValidationContext) -> CheckResult:  # noqa: D102
        try:
            return self._run(ctx)
        except Exception as exc:
            logger.exception("C5 error")
            return CheckResult(
                check_id=self.CHECK_ID,
                check_name=self.CHECK_NAME,
                passed=False,
                message=f"check errored: {exc}",
            )

    def _run(self, ctx: ValidationContext) -> CheckResult:
        # ── 1. SPARQL: count fm:incompleteReference triples ──────────────────
        sparql_rows = ctx.sparql.query(
            f"""
            SELECT (COUNT(*) AS ?cnt)
            FROM <{ctx.graph_uri}>
            WHERE {{
                ?s <{_FM_INCOMPLETE}> ?v .
            }}
            """
        )
        graph_count = int(sparql_rows[0].get("cnt", 0)) if sparql_rows else 0

        # ── 2. SQL: count null FK values across gold tables ───────────────────
        if ctx.ontology is None:
            # Without an ontology we cannot determine which columns are FKs
            return CheckResult(
                check_id=self.CHECK_ID,
                check_name=self.CHECK_NAME,
                passed=False,
                message="no ontology provided — cannot determine FK columns",
            )

        definitions = GoldDefinitionBuilder().build(
            ctx.mapping_report,
            ontology_graph=ctx.ontology,
        )

        # Object properties whose range is a gold-backed class (source 1: null FK)
        gold_class_uris: Set[str] = {d.primary_class_uri for d in definitions}
        fk_property_uris: Set[str] = set()
        for prop, _, range_cls in ctx.ontology.triples((None, RDFS.range, None)):
            if (prop, RDF.type, OWL.ObjectProperty) not in ctx.ontology:
                continue
            if str(range_cls) in gold_class_uris:
                fk_property_uris.add(str(prop))

        # All OWL class URIs — used to detect value_map→class URI entries (source 2).
        all_class_uris: Set[str] = {
            str(cls)
            for cls in ctx.ontology.subjects(RDF.type, OWL.Class)
            if isinstance(cls, URIRef)
        }
        # Cross-namespace extension: value_map entries may reference class URIs from a
        # base domain ontology (e.g. OrgHealth.owl#Payer) whose namespace differs from the
        # rigor OWL (e.g. synthea#Payer).  Apply the same fragment-matching heuristic used
        # by TripleBuilder so the SQL-side count and the graph-side count use identical
        # detection logic.
        _class_fragments_c5: Set[str] = {
            uri.split("#")[-1] for uri in all_class_uris if "#" in uri
        }
        for _defn_c5 in definitions:
            for _spec_c5 in _defn_c5.column_specs:
                for _v_c5 in (_spec_c5.value_map or {}).values():
                    if _v_c5 and "#" in _v_c5 and _v_c5 not in all_class_uris:
                        _frag_c5 = _v_c5.split("#")[-1]
                        if _frag_c5 in _class_fragments_c5:
                            all_class_uris.add(_v_c5)
        # Property range map for OWL ObjectProperties — used to exclude BUG-02 cases
        # (where mapped_uri == range class → emits fm:invalidMapping, not fm:incompleteReference).
        obj_prop_range: Dict[str, str] = {}
        for prop, _, range_cls in ctx.ontology.triples((None, RDFS.range, None)):
            if (prop, RDF.type, OWL.ObjectProperty) in ctx.ontology:
                obj_prop_range[str(prop)] = str(range_cls)

        gold_null_count = 0
        null_detail: List[Dict[str, Any]] = []

        # Get actual columns in gold tables (for dialect compatibility)
        insp = inspect(ctx.gold_engine)
        existing_tables: Set[str] = set(insp.get_table_names())

        for defn in definitions:
            table = defn.gold_table_name
            if table not in existing_tables:
                logger.debug("C5: gold table %s not found — skipping", table)
                continue

            # Source 1: FK column specs (OWL ObjectProperty + gold-backed range) → null values.
            fk_specs: List[GoldColumnSpec] = [
                s for s in defn.column_specs
                if s.property_uri in fk_property_uris
            ]
            for spec in fk_specs:
                col = spec.gold_column_name
                try:
                    with ctx.gold_engine.connect() as conn:
                        result = conn.execute(
                            text(  # noqa: S608
                                f"SELECT COUNT(*) AS cnt FROM `{table}`"
                                f" WHERE `{col}` IS NULL"
                            )
                        )
                        null_cnt = result.scalar() or 0
                except Exception as sql_exc:
                    logger.warning("C5: could not query %s.%s: %s", table, col, sql_exc)
                    continue

                if null_cnt > 0:
                    null_detail.append(
                        {"table": table, "column": col, "null_count": null_cnt}
                    )
                    gold_null_count += null_cnt

            # Source 2: value_map columns where some entries map to OWL class URIs
            # (FK-to-missing-class fallback).  The TripleBuilder emits
            # fm:incompleteReference for these non-null values, so the SQL side must
            # count them too.  Only entries where mapped_uri is a class URI BUT is NOT
            # the property's declared range class (which would be BUG-02 →
            # fm:invalidMapping, not fm:incompleteReference) are included.
            for spec in defn.column_specs:
                if spec.property_uri in fk_property_uris:
                    continue  # already counted above
                if not spec.value_map:
                    continue
                range_cls_str = obj_prop_range.get(spec.property_uri)
                # Keys whose value_map entry maps to a class URI (excluding BUG-02)
                class_mapped_keys = [
                    k for k, v in spec.value_map.items()
                    if v in all_class_uris and v != range_cls_str
                ]
                if not class_mapped_keys:
                    continue

                col = spec.gold_column_name
                # SQL: count rows whose gold column value is one of the class-mapped keys.
                # Parameterise carefully to avoid SQL injection (keys are source data values).
                placeholders = ", ".join(":k" + str(i) for i in range(len(class_mapped_keys)))
                params = {"k" + str(i): k for i, k in enumerate(class_mapped_keys)}
                try:
                    with ctx.gold_engine.connect() as conn:
                        result = conn.execute(
                            text(  # noqa: S608
                                f"SELECT COUNT(*) AS cnt FROM `{table}`"
                                f" WHERE `{col}` IN ({placeholders})"
                            ),
                            params,
                        )
                        cls_mapped_cnt = result.scalar() or 0
                except Exception as sql_exc:
                    logger.warning(
                        "C5: could not query class-URI-mapped values %s.%s: %s",
                        table, col, sql_exc,
                    )
                    continue

                if cls_mapped_cnt > 0:
                    null_detail.append({
                        "table": table,
                        "column": col,
                        "null_count": cls_mapped_cnt,
                        "note": "value_map→class URI (FK-to-missing-class fallback)",
                    })
                    gold_null_count += cls_mapped_cnt

        # ── 3. Compare ────────────────────────────────────────────────────────
        passed = graph_count == gold_null_count
        return CheckResult(
            check_id=self.CHECK_ID,
            check_name=self.CHECK_NAME,
            passed=passed,
            message=(
                f"graph incompleteReference={graph_count}, gold null FKs={gold_null_count}"
                + ("" if passed else f" — delta={abs(graph_count - gold_null_count)}")
            ),
            details={
                "graph_incomplete_count": graph_count,
                "gold_null_fk_count": gold_null_count,
                "null_fk_detail": null_detail,
            },
        )
