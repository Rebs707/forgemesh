# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""C10 — External terminology-binding integrity.

Every external concept binding must carry its provenance, and none may be
asserted below the confidence floor.  Two general, dataset-agnostic assertions
over the ontology graph (the binder emits bindings into the ontology):

- TB-1  provenance complete: every individual with a ``skos:exactMatch`` to an
        external concept carries exactly one ``fm:terminologySystem`` and exactly
        one ``fm:terminologyBindingConfidence``.  No exactMatch without provenance.
- TB-2  floor honoured: no asserted ``skos:exactMatch`` has a binding confidence
        below ``TERM_BIND_CONFIDENCE_FLOOR`` — sub-floor candidates belong in the
        review sidecar, never the asserted graph.

Passes (skipped) when the ontology carries no external bindings, so datasets with
terminology binding disabled stay green.  Keys on nothing dataset-specific.
"""
from __future__ import annotations

import logging
from typing import List

from rdflib import URIRef
from rdflib.namespace import SKOS

from ..context import ValidationContext
from ..models import CheckResult
from .base import BaseCheck

logger = logging.getLogger(__name__)

_FM = "http://forgemesh.ai/ontology#"
_FM_TERM_SYSTEM = URIRef(_FM + "terminologySystem")
_FM_TERM_CONFIDENCE = URIRef(_FM + "terminologyBindingConfidence")


class TerminologyBindingCheck(BaseCheck):
    """C10 — terminology-binding provenance + confidence-floor integrity."""

    CHECK_ID = "C10"
    CHECK_NAME = "terminology_binding"

    def run(self, ctx: ValidationContext) -> CheckResult:
        try:
            return self._run(ctx)
        except Exception as exc:  # never raise
            logger.exception("C10 error")
            return CheckResult(
                check_id=self.CHECK_ID, check_name=self.CHECK_NAME,
                passed=False, message=f"check errored: {exc}",
            )

    def _run(self, ctx: ValidationContext) -> CheckResult:
        g = ctx.ontology
        if g is None:
            return CheckResult(
                check_id=self.CHECK_ID, check_name=self.CHECK_NAME,
                passed=True, message="no ontology — check skipped",
            )

        floor = 0.5
        if ctx.settings is not None:
            floor = float(getattr(ctx.settings, "TERM_BIND_CONFIDENCE_FLOOR", floor))

        bound_subjects = {s for s, _, _ in g.triples((None, SKOS.exactMatch, None))}
        if not bound_subjects:
            return CheckResult(
                check_id=self.CHECK_ID, check_name=self.CHECK_NAME,
                passed=True, message="no external terminology bindings — check skipped",
            )

        missing_provenance: List[str] = []
        bad_cardinality: List[str] = []
        sub_floor: List[str] = []

        for s in bound_subjects:
            systems = list(g.objects(s, _FM_TERM_SYSTEM))
            confs = list(g.objects(s, _FM_TERM_CONFIDENCE))
            local = _local(str(s))
            if not systems or not confs:
                missing_provenance.append(local)
                continue
            if len(systems) != 1 or len(confs) != 1:
                bad_cardinality.append(
                    f"{local} (system×{len(systems)}, confidence×{len(confs)})"
                )
            for c in confs:
                try:
                    if float(c) < floor:
                        sub_floor.append(f"{local}={float(c):.3f}")
                except (TypeError, ValueError):
                    bad_cardinality.append(f"{local} (non-numeric confidence {c!r})")

        failures: List[str] = []
        if missing_provenance:
            failures.append(
                f"TB-1 ({len(missing_provenance)} exactMatch without provenance): "
                + ", ".join(sorted(missing_provenance)[:5])
                + (" …" if len(missing_provenance) > 5 else "")
            )
        if bad_cardinality:
            failures.append(
                f"TB-1 ({len(bad_cardinality)} with wrong provenance cardinality): "
                + "; ".join(bad_cardinality[:5])
            )
        if sub_floor:
            failures.append(
                f"TB-2 ({len(sub_floor)} binding(s) below floor {floor}): "
                + ", ".join(sorted(sub_floor)[:5])
            )

        details = {
            "bound_subjects": len(bound_subjects),
            "confidence_floor": floor,
            "missing_provenance": sorted(missing_provenance),
            "bad_cardinality": bad_cardinality,
            "sub_floor": sorted(sub_floor),
        }
        if failures:
            return CheckResult(
                check_id=self.CHECK_ID, check_name=self.CHECK_NAME,
                passed=False, message="; ".join(failures), details=details,
            )
        return CheckResult(
            check_id=self.CHECK_ID, check_name=self.CHECK_NAME, passed=True,
            message=f"{len(bound_subjects)} external binding(s): all carry provenance, "
                    f"none below floor {floor}",
            details=details,
        )


def _local(uri: str) -> str:
    return uri.split("#")[-1] if "#" in uri else uri.rsplit("/", 1)[-1]
