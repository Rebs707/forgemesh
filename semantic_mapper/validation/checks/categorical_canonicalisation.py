# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""C8 — Categorical subtype canonicalisation.

A categorical (value-mapped) source value must resolve to ONE ontology individual,
reused across every table that carries the same field.  Three sub-assertions:

- CANON-1  one entity per value: within a field (grouped by source column name), no
           normalised source value maps to more than one individual URI.  Catches the
           ``MaleGender``/``MedallistGenderMale`` fragmentation and ``DisciplineCodeALP``
           vs ``DisciplineCodeAlpineSkiing`` split.
- CANON-2  no raw-code passthrough: every categorical individual present in the graph is
           the target of a value-map rule — none bypassed mapping.
- CANON-3  cross-table join holds: for a field value-mapped in two or more tables whose
           maps share a value, at least one individual is referenced from both tables.

Dataset-agnostic: fields and values are read from the mapping report and the graph; no
table or column names are hardcoded.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Set, Tuple

from ..context import ValidationContext
from ..models import CheckResult
from .base import BaseCheck

logger = logging.getLogger(__name__)


def _norm(v: Any) -> str:
    return str(v).strip().lower()


class CategoricalCanonicalisationCheck(BaseCheck):
    CHECK_ID = "C8"
    CHECK_NAME = "categorical_canonicalisation"

    def run(self, ctx: ValidationContext) -> CheckResult:
        try:
            return self._run(ctx)
        except Exception as exc:  # never raise
            logger.exception("C8 error")
            return CheckResult(
                check_id=self.CHECK_ID, check_name=self.CHECK_NAME,
                passed=False, message=f"check errored: {exc}",
            )

    def _run(self, ctx: ValidationContext) -> CheckResult:
        report = ctx.mapping_report
        if report is None:
            return CheckResult(
                check_id=self.CHECK_ID, check_name=self.CHECK_NAME,
                passed=True, message="no mapping report — check skipped",
            )

        # Index value mappings by field (source column), collecting value→{uris} and
        # the categorical property URIs that carry the field across tables.
        field_value_uris: Dict[str, Dict[str, Set[str]]] = {}
        field_props: Dict[str, Set[str]] = {}
        all_targets: Set[str] = set()
        for tm in report.tables:
            for cm in tm.column_mappings:
                if not cm.value_mappings:
                    continue
                field = _norm(cm.source_column)
                vmap = field_value_uris.setdefault(field, {})
                for vm in cm.value_mappings:
                    vmap.setdefault(_norm(vm.source_value), set()).add(str(vm.mapped_uri))
                    all_targets.add(str(vm.mapped_uri))
                if cm.mapped_property_uri:
                    field_props.setdefault(field, set()).add(str(cm.mapped_property_uri))

        # ── CANON-1 — one entity per value (within a field) ──────────────────
        canon1: List[str] = []
        for field, vmap in field_value_uris.items():
            for value, uris in vmap.items():
                if len(uris) > 1:
                    canon1.append(
                        f"{field}={value!r} → {sorted(_frag(u) for u in uris)}"
                    )

        # ── CANON-2 — no raw-code passthrough ────────────────────────────────
        # Every categorical individual present as an object in the graph must be a
        # value-map target.  Query objects of the categorical properties.
        canon2: List[str] = []
        props = sorted({p for ps in field_props.values() for p in ps})
        graph_individuals: Set[str] = set()
        if props:
            values_clause = " ".join(f"<{p}>" for p in props)
            rows = ctx.sparql.query(
                f"""
                SELECT DISTINCT ?o
                FROM <{ctx.graph_uri}>
                WHERE {{ ?s ?p ?o . VALUES ?p {{ {values_clause} }} FILTER(isIRI(?o)) }}
                """
            )
            graph_individuals = {str(r.get("o")) for r in rows if r.get("o")}
            canon2 = sorted(_frag(o) for o in graph_individuals if o not in all_targets)

        # ── CANON-3 — cross-table join holds for shared fields ───────────────
        canon3: List[str] = []
        for field, props_set in field_props.items():
            if len(props_set) < 2:
                continue  # field only in one table — no cross-table join to test
            # Only require a shared individual when the maps actually share a value.
            vmap = field_value_uris.get(field, {})
            shares_value = any(len(uris) >= 1 for uris in vmap.values()) and len(vmap) > 0
            if not shares_value:
                continue
            vclause = " ".join(f"<{p}>" for p in sorted(props_set))
            # FROM must sit on the OUTER query — a sub-SELECT inherits the dataset and
            # may not carry its own FROM clause.
            rows = ctx.sparql.query(
                f"""
                SELECT (COUNT(*) AS ?n) FROM <{ctx.graph_uri}> WHERE {{
                  SELECT ?o (COUNT(DISTINCT ?p) AS ?np)
                  WHERE {{ ?s ?p ?o . VALUES ?p {{ {vclause} }} FILTER(isIRI(?o)) }}
                  GROUP BY ?o HAVING(COUNT(DISTINCT ?p) >= 2)
                }}
                """
            )
            n_shared = int(rows[0].get("n", 0)) if rows else 0
            if n_shared == 0:
                canon3.append(field)

        # ── CANON-4 — data uses the generator's deterministic vocabulary ─────
        # Every categorical individual the data references must BE the generator's canonical
        # {Value}{Class} individual for its value — not an LLM-invented synonym the ontology
        # enricher added (MaleGender for "M" instead of the declared MGender, leaving the
        # generated vocabulary orphaned). Verified structurally against the ontology: an
        # individual's local name must equal value_slug(its rdfs:label) + its vocab-class
        # local name. Dataset-agnostic — keyed on the deterministic naming rule, not values.
        from rdflib import OWL as _OWL, RDF as _RDF, RDFS as _RDFS, URIRef as _URIRef
        from semantic_mapper.ontology.iterative.categorical_individuals import _value_slug
        canon4: List[str] = []
        if ctx.ontology is not None:
            for o in sorted(graph_individuals):
                ind = _URIRef(o)
                vocab_classes = [
                    c for c in ctx.ontology.objects(ind, _RDF.type)
                    if c != _OWL.NamedIndividual
                    and (c, _RDF.type, _OWL.Class) in ctx.ontology
                ]
                labels = [str(lbl) for lbl in ctx.ontology.objects(ind, _RDFS.label)]
                if not vocab_classes or not labels:
                    canon4.append(f"{_frag(o)} (not a declared vocabulary individual)")
                    continue
                cls_local = _frag(str(sorted(vocab_classes, key=str)[0]))
                expected = _value_slug(labels[0]) + cls_local
                if _frag(o) != expected:
                    canon4.append(f"{_frag(o)} (label {labels[0]!r} → expected {expected})")

        failures: List[str] = []
        if canon1:
            failures.append(f"CANON-1 ({len(canon1)} value(s) → multiple individuals): "
                            + "; ".join(canon1[:5]) + (" …" if len(canon1) > 5 else ""))
        if canon2:
            failures.append(f"CANON-2 ({len(canon2)} raw-code passthrough individual(s)): "
                            + ", ".join(canon2[:8]) + (" …" if len(canon2) > 8 else ""))
        if canon3:
            failures.append(f"CANON-3 (no shared individual for field(s)): {', '.join(canon3)}")
        if canon4:
            failures.append(f"CANON-4 ({len(canon4)} non-canonical individual(s) in data — "
                            f"map drifted from generated vocabulary): "
                            + ", ".join(canon4[:8]) + (" …" if len(canon4) > 8 else ""))

        details = {"canon1_violations": canon1, "canon2_passthrough": canon2,
                   "canon3_unjoined_fields": canon3, "canon4_noncanonical": canon4}
        if failures:
            return CheckResult(
                check_id=self.CHECK_ID, check_name=self.CHECK_NAME,
                passed=False, message="; ".join(failures), details=details,
            )
        n_fields = len(field_value_uris)
        return CheckResult(
            check_id=self.CHECK_ID, check_name=self.CHECK_NAME, passed=True,
            message=f"categorical values canonical across {n_fields} field(s) "
                    f"(one entity per value, no passthrough, cross-table joins hold)",
            details=details,
        )


def _frag(uri: str) -> str:
    return uri.split("#")[-1] if "#" in uri else uri.rsplit("/", 1)[-1]
