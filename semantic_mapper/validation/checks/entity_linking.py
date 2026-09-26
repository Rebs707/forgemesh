# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""C9 — Entity-linking integrity (value-overlap FK resolution).

Issue 2: a relationship that is real in the data but never declared as a foreign key
must still resolve to an entity LINK — not be discriminated into per-value individuals,
and not leave the referenced entity an orphan.  A column whose distinct values are a
near-subset of another entity's identifying key is referencing that entity; the graph
must reflect that.  Two sub-assertions, both general:

- FK-2  no orphan referenced entity: an entity referenced by value overlap must be the
        ``rdfs:range`` of at least one ``owl:ObjectProperty`` — something links to it.
- FK-3  entity-referencing column not discriminated: a column that references an entity
        by value overlap must NOT carry a value-map.  It links to the entity; it is not
        its own controlled vocabulary.

Dataset-agnostic: entities, keys and overlaps are read from the gold tables and the
ontology; no table or column names are hardcoded.  Overlap is computed on textual
columns only (codes and names) — the same text↔text rule the inspector's value-overlap
FK inference uses, so a coincidental numeric overlap cannot fabricate a reference.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from rdflib import OWL, RDF, RDFS
from sqlalchemy import inspect as _sa_inspect, text as _sa_text

from semantic_mapper.db.inspector import _is_text_type
from semantic_mapper.gold.builder import GoldDefinitionBuilder

from ..context import ValidationContext
from ..models import CheckResult
from .base import BaseCheck

logger = logging.getLogger(__name__)

_OVERLAP_THRESHOLD = 0.80   # fraction of a column's distinct values found in a key
_NEAR_UNIQUE = 0.95         # distinct / row_count for a column to count as an identifying key
_SAMPLE = 200               # distinct values sampled per column for the overlap test


class EntityLinkingCheck(BaseCheck):
    """C9 — value-overlap entity-linking integrity (FK-2, FK-3)."""

    CHECK_ID = "C9"
    CHECK_NAME = "entity_linking"

    def run(self, ctx: ValidationContext) -> CheckResult:
        try:
            return self._run(ctx)
        except Exception as exc:  # never raise
            logger.exception("C9 error")
            return CheckResult(
                check_id=self.CHECK_ID, check_name=self.CHECK_NAME,
                passed=False, message=f"check errored: {exc}",
            )

    def _entity_is_linked(self, ctx: ValidationContext, e_class: str) -> bool:
        """True when ≥1 instance of *e_class* is the object of an object-property edge in
        the graph (an actually-materialised inbound link, not just an ontology promise)."""
        try:
            rows = ctx.sparql.query(
                f"""
                SELECT (COUNT(*) AS ?n) FROM <{ctx.graph_uri}> WHERE {{
                  ?o a <{e_class}> . ?s ?p ?o . FILTER(?s != ?o)
                }}
                """
            )
            return bool(rows) and int(rows[0].get("n", 0)) > 0
        except Exception:
            return False

    def _run(self, ctx: ValidationContext) -> CheckResult:
        if ctx.ontology is None or ctx.gold_engine is None or ctx.mapping_report is None:
            return CheckResult(
                check_id=self.CHECK_ID, check_name=self.CHECK_NAME,
                passed=True, message="no ontology/gold/report — check skipped",
            )

        definitions = GoldDefinitionBuilder().build(
            ctx.mapping_report, ontology_graph=ctx.ontology
        )
        eng = ctx.gold_engine
        insp = _sa_inspect(eng)
        gold_tables = set(insp.get_table_names())
        defs = [d for d in definitions if d.gold_table_name in gold_tables]
        if not defs:
            return CheckResult(
                check_id=self.CHECK_ID, check_name=self.CHECK_NAME,
                passed=True, message="no gold entity tables found — check skipped",
            )

        q = '"' if eng.dialect.name == "sqlite" else "`"

        # ── Sample each gold table's textual columns ─────────────────────────
        # col_values[(table, col)] = set of distinct string values (≤ _SAMPLE)
        # keys[table] = list of identifying (near-unique) text columns
        # spec[(table, col)] = GoldColumnSpec (carries value_map + source_column)
        col_values: Dict[Tuple[str, str], Set[str]] = {}
        keys: Dict[str, List[str]] = {}
        spec: Dict[Tuple[str, str], Any] = {}
        for d in defs:
            t = d.gold_table_name
            for s in d.column_specs:
                spec[(t, s.gold_column_name)] = s
            col_types = {c["name"]: str(c["type"]) for c in insp.get_columns(t)}
            with eng.connect() as conn:
                total = conn.execute(_sa_text(f"SELECT COUNT(*) FROM {q}{t}{q}")).scalar() or 0
                if total == 0:
                    continue
                for col, typ in col_types.items():
                    if col.startswith("_") or not _is_text_type(typ):
                        continue  # audit columns and non-text columns are not keys/refs
                    rows = conn.execute(_sa_text(
                        f"SELECT DISTINCT {q}{col}{q} AS v FROM {q}{t}{q} "
                        f"WHERE {q}{col}{q} IS NOT NULL LIMIT {_SAMPLE}"
                    )).fetchall()
                    vals = {str(r[0]) for r in rows}
                    if not vals:
                        continue
                    col_values[(t, col)] = vals
                    distinct = conn.execute(_sa_text(
                        f"SELECT COUNT(DISTINCT {q}{col}{q}) FROM {q}{t}{q}"
                    )).scalar() or 0
                    if distinct / total >= _NEAR_UNIQUE:
                        keys.setdefault(t, []).append(col)

        # ── Object-property range classes in the ontology (for FK-2) ─────────
        object_property_ranges: Set[str] = set()
        for prop in ctx.ontology.subjects(RDF.type, OWL.ObjectProperty):
            for rng in ctx.ontology.objects(prop, RDFS.range):
                object_property_ranges.add(str(rng))
        class_uri_by_table = {d.gold_table_name: d.primary_class_uri for d in defs}

        # ── Detect references by value overlap, then assert FK-2 / FK-3 ──────
        fk3_discriminated: List[str] = []   # referencing column carries a value-map
        fk2_orphans: List[str] = []         # referenced entity is not an object-property range
        references = 0
        seen_orphan: Set[str] = set()
        for (s_table, s_col), s_vals in col_values.items():
            for e_table, e_keys in keys.items():
                if e_table == s_table:
                    continue
                best = 0.0
                for key_col in e_keys:
                    if (e_table, key_col) == (s_table, s_col):
                        continue
                    k_vals = col_values.get((e_table, key_col), set())
                    if not k_vals:
                        continue
                    overlap = len(s_vals & k_vals) / len(s_vals)
                    best = max(best, overlap)
                if best < _OVERLAP_THRESHOLD:
                    continue
                references += 1

                # FK-3: a column that references an entity must not be discriminated.
                s_spec = spec.get((s_table, s_col))
                if s_spec is not None and getattr(s_spec, "value_map", None):
                    fk3_discriminated.append(
                        f"{s_table}.{s_col} → {_local(class_uri_by_table.get(e_table, e_table))} "
                        f"({len(s_spec.value_map)} value-map individuals)"
                    )

                # FK-2: the referenced entity must actually be LINKED — declared as an
                # object-property range AND, when a graph is available, the target of ≥1
                # object-property edge in the graph.  The graph check is what stops a
                # vacuous pass: a declared link whose mapping fell to PENDING_REVIEW emits
                # no triples, so the entity is an orphan in the data even though the ontology
                # promises the property.
                e_class = class_uri_by_table.get(e_table)
                if e_class and e_table not in seen_orphan:
                    declared = e_class in object_property_ranges
                    linked = self._entity_is_linked(ctx, e_class) if ctx.sparql is not None else declared
                    if not (declared and linked):
                        seen_orphan.add(e_table)
                        reason = ("no object property in ontology" if not declared
                                  else "declared object-property range but 0 links in graph")
                        fk2_orphans.append(f"{_local(e_class)} ({reason})")

        failures: List[str] = []
        if fk2_orphans:
            failures.append(
                f"FK-2 ({len(fk2_orphans)} orphan entit(y/ies) referenced but not linked): "
                + ", ".join(sorted(fk2_orphans))
            )
        if fk3_discriminated:
            failures.append(
                f"FK-3 ({len(fk3_discriminated)} entity-referencing column(s) discriminated): "
                + "; ".join(fk3_discriminated[:5])
                + (" …" if len(fk3_discriminated) > 5 else "")
            )

        details = {
            "references_detected": references,
            "fk2_orphans": sorted(fk2_orphans),
            "fk3_discriminated": fk3_discriminated,
        }
        if failures:
            return CheckResult(
                check_id=self.CHECK_ID, check_name=self.CHECK_NAME,
                passed=False, message="; ".join(failures), details=details,
            )
        return CheckResult(
            check_id=self.CHECK_ID, check_name=self.CHECK_NAME, passed=True,
            message=f"entity links resolve — {references} value-overlap reference(s): "
                    f"none discriminated, no orphan entities",
            details=details,
        )


def _local(uri: str) -> str:
    return uri.split("#")[-1] if "#" in uri else uri.rsplit("/", 1)[-1]
