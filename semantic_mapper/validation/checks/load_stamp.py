# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""C6 — Load-stamp freshness and project identity.

Asserts three things about the named graph's metadata node:

1. ``fm:loadRunId`` equals ``ctx.baseline_run_id`` — the graph was loaded from
   the most recent gold run, not an older one.

2. ``fm:projectName`` equals ``ctx.project_name`` — the graph belongs to the
   expected project, not a stale graph from a different dataset that happens to
   share the same named-graph URI.

3. ``fm:ontologySource`` equals ``ctx.ontology_source`` (when set) — the graph
   was materialised with the same OWL file currently loaded for the project.
   A mismatch means the ontology changed between runs without re-materialising.

All three halves can fail independently and are reported together.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..context import ValidationContext
from ..models import CheckResult
from .base import BaseCheck

logger = logging.getLogger(__name__)

_FM_LOAD_RUN_ID       = "http://forgemesh.ai/ontology#loadRunId"
_FM_PROJECT_NAME      = "http://forgemesh.ai/ontology#projectName"
_FM_ONTOLOGY_SOURCE   = "http://forgemesh.ai/ontology#ontologySource"


class LoadStampCheck(BaseCheck):
    """C6 — load-stamp freshness and project identity."""

    CHECK_ID = "C6"
    CHECK_NAME = "load_stamp_freshness"

    def run(self, ctx: ValidationContext) -> CheckResult:  # noqa: D102
        try:
            return self._run(ctx)
        except Exception as exc:
            logger.exception("C6 error")
            return CheckResult(
                check_id=self.CHECK_ID,
                check_name=self.CHECK_NAME,
                passed=False,
                message=f"check errored: {exc}",
            )

    def _run(self, ctx: ValidationContext) -> CheckResult:
        rows = ctx.sparql.query(
            f"""
            SELECT ?runId ?projectName ?ontologySource
            FROM <{ctx.graph_uri}>
            WHERE {{
                <{ctx.graph_uri}> <{_FM_LOAD_RUN_ID}> ?runId .
                OPTIONAL {{ <{ctx.graph_uri}> <{_FM_PROJECT_NAME}>    ?projectName . }}
                OPTIONAL {{ <{ctx.graph_uri}> <{_FM_ONTOLOGY_SOURCE}> ?ontologySource . }}
            }}
            """
        )

        details: Dict[str, Any] = {
            "expected_run_id":       ctx.baseline_run_id,
            "expected_project":      ctx.project_name,
            "expected_ontology":     ctx.ontology_source or "(not checked)",
        }

        if not rows:
            return CheckResult(
                check_id=self.CHECK_ID,
                check_name=self.CHECK_NAME,
                passed=False,
                message="no load-stamp metadata found on graph node",
                details=details,
            )

        actual_run_id:        Optional[str] = rows[0].get("runId")
        actual_project:       Optional[str] = rows[0].get("projectName")
        actual_ontology:      Optional[str] = rows[0].get("ontologySource")

        details["actual_run_id"]   = actual_run_id
        details["actual_project"]  = actual_project
        details["actual_ontology"] = actual_ontology

        failures = []
        if actual_run_id != ctx.baseline_run_id:
            failures.append(
                f"runId mismatch: expected={ctx.baseline_run_id!r} actual={actual_run_id!r}"
            )
        if actual_project is not None and actual_project != ctx.project_name:
            failures.append(
                f"projectName mismatch: expected={ctx.project_name!r} actual={actual_project!r}"
            )
        if ctx.ontology_source and actual_ontology != ctx.ontology_source:
            failures.append(
                f"ontologySource mismatch: expected={ctx.ontology_source!r} actual={actual_ontology!r} — re-materialise after ontology change"
            )

        if failures:
            return CheckResult(
                check_id=self.CHECK_ID,
                check_name=self.CHECK_NAME,
                passed=False,
                message="; ".join(failures),
                details=details,
            )

        return CheckResult(
            check_id=self.CHECK_ID,
            check_name=self.CHECK_NAME,
            passed=True,
            message=(
                f"load stamp ok — runId={actual_run_id!r}  "
                f"project={actual_project!r}  "
                f"ontology={actual_ontology!r}"
            ),
            details=details,
        )
