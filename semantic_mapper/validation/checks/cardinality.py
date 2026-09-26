# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""C2 — Relationship cardinality.

For every ``owl:ObjectProperty`` in the ontology, compute the ratio of triples
to distinct subjects in the named graph.  A ratio substantially above 1.0 for
a property that should be single-valued is the *airline double-mint* signature
— one logical relationship fanned out onto two subjects because two different
URI-minting schemes are in use.

Default threshold: flag any property whose ratio exceeds ``ratio_threshold``
(default 1.5).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from rdflib import OWL, RDF, Graph, Namespace, URIRef

from ..context import ValidationContext
from ..models import CheckResult
from .base import BaseCheck

logger = logging.getLogger(__name__)

_OWL_OBJECT_PROPERTY = str(OWL.ObjectProperty)
_RDF_TYPE = str(RDF.type)
# fm:multiValued marks a property as genuinely 1-to-N by construction (e.g. an
# exploded packed column). Such fan-out is expected, not the double-mint signature.
_FM_MULTIVALUED = Namespace("http://forgemesh.ai/ontology#").multiValued


class CardinalityCheck(BaseCheck):
    """C2 — relationship cardinality."""

    CHECK_ID = "C2"
    CHECK_NAME = "relationship_cardinality"

    def __init__(self, ratio_threshold: float = 1.5) -> None:
        self._threshold = ratio_threshold

    def run(self, ctx: ValidationContext) -> CheckResult:  # noqa: D102
        try:
            return self._run(ctx)
        except Exception as exc:
            logger.exception("C2 error")
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
                message="no ontology provided — cannot determine object properties",
            )

        # Collect all owl:ObjectProperty URIs from the ontology, excluding those
        # explicitly marked fm:multiValued — genuinely 1-to-N relationships (e.g.
        # exploded packed columns) whose fan-out is expected, not a double-mint.
        multi_valued = {
            str(s)
            for s, _, o in ctx.ontology.triples((None, _FM_MULTIVALUED, None))
            if str(o).strip().lower() in ("true", "1")
        }
        obj_props: List[str] = [
            str(s)
            for s, _, _ in ctx.ontology.triples((None, RDF.type, OWL.ObjectProperty))
            if str(s) not in multi_valued
        ]

        if not obj_props:
            return CheckResult(
                check_id=self.CHECK_ID,
                check_name=self.CHECK_NAME,
                passed=True,
                message="no owl:ObjectProperty declarations found — check skipped",
            )

        flagged: List[Dict[str, Any]] = []
        all_ratios: List[Dict[str, Any]] = []

        for prop_uri in obj_props:
            rows = ctx.sparql.query(
                f"""
                SELECT
                    (COUNT(?o) AS ?n_triples)
                    (COUNT(DISTINCT ?s) AS ?n_subjects)
                FROM <{ctx.graph_uri}>
                WHERE {{
                    ?s <{prop_uri}> ?o .
                }}
                """
            )
            if not rows:
                continue
            n_triples = int(rows[0].get("n_triples", 0))
            n_subjects = int(rows[0].get("n_subjects", 0))

            if n_subjects == 0:
                # Property not used in this graph — skip
                continue

            ratio = n_triples / n_subjects
            entry: Dict[str, Any] = {
                "property": prop_uri,
                "n_triples": n_triples,
                "n_subjects": n_subjects,
                "ratio": round(ratio, 3),
            }
            all_ratios.append(entry)

            if ratio > self._threshold:
                flagged.append(entry)

        if flagged:
            lines = [
                f"{_local(r['property'])}: {r['ratio']:.2f} ({r['n_triples']} triples / {r['n_subjects']} subjects)"
                for r in flagged
            ]
            return CheckResult(
                check_id=self.CHECK_ID,
                check_name=self.CHECK_NAME,
                passed=False,
                message=(
                    f"{len(flagged)} object property/ies with ratio > {self._threshold}: "
                    + "; ".join(lines)
                ),
                details={"flagged": flagged, "all_ratios": all_ratios, "threshold": self._threshold},
            )

        return CheckResult(
            check_id=self.CHECK_ID,
            check_name=self.CHECK_NAME,
            passed=True,
            message=(
                f"all {len(all_ratios)} used object propert{'ies' if len(all_ratios) != 1 else 'y'} "
                f"within cardinality threshold ({self._threshold})"
            ),
            details={"all_ratios": all_ratios, "threshold": self._threshold},
        )


def _local(uri: str) -> str:
    return uri.split("#")[-1] if "#" in uri else uri.split("/")[-1]
