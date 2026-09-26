# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""C7 — NL-to-SQL join correctness.

Runs a fixed set of natural-language questions (with known-correct answers)
through the ``SemanticQueryEngine`` and asserts each returns a non-empty
result set (or an empty one, for questions where the correct answer is
genuinely empty).

This check catches the *dual-identifier join bug* — the query generator joined
an IATA code column against a numeric identifier column and returned zero rows
— which is completely silent at the SQL level: no error, just an empty result.

Requires ``ctx.settings`` (a ``Settings`` instance) to construct the query
engine.  When ``ctx.nl_questions`` is empty, the check is skipped with a pass.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from ..context import NLQuestion, ValidationContext
from ..models import CheckResult
from .base import BaseCheck

logger = logging.getLogger(__name__)


class NLQueryCheck(BaseCheck):
    """C7 — NL-to-SQL join correctness."""

    CHECK_ID = "C7"
    CHECK_NAME = "nl_query_join_correctness"

    def run(self, ctx: ValidationContext) -> CheckResult:  # noqa: D102
        try:
            return self._run(ctx)
        except Exception as exc:
            logger.exception("C7 error")
            return CheckResult(
                check_id=self.CHECK_ID,
                check_name=self.CHECK_NAME,
                passed=False,
                message=f"check errored: {exc}",
            )

    def _run(self, ctx: ValidationContext) -> CheckResult:
        if not ctx.nl_questions:
            return CheckResult(
                check_id=self.CHECK_ID,
                check_name=self.CHECK_NAME,
                passed=True,
                message="no NL questions provided — check skipped",
            )

        if ctx.settings is None:
            return CheckResult(
                check_id=self.CHECK_ID,
                check_name=self.CHECK_NAME,
                passed=False,
                message="ctx.settings is required for C7 but was not provided",
            )

        # Import lazily so that SemanticQueryEngine / OpenAI dependency is
        # only required when this check actually runs.
        from semantic_mapper.gold.builder import GoldDefinitionBuilder
        from semantic_mapper.query.engine import SemanticQueryEngine

        definitions = GoldDefinitionBuilder().build(
            ctx.mapping_report,
            ontology_graph=ctx.ontology,
        )

        engine = SemanticQueryEngine(
            report=ctx.mapping_report,
            silver_engine=ctx.gold_engine,   # C7 queries gold directly
            settings=ctx.settings,
            gold_engine=ctx.gold_engine,
            gold_definitions=definitions,
        )

        failures: List[Dict[str, Any]] = []
        results: List[Dict[str, Any]] = []

        for q in ctx.nl_questions:
            qr = engine.query(q.question)
            is_empty = (qr.error is not None) or (not qr.rows)
            failed = (q.expect_non_empty and is_empty) or (
                not q.expect_non_empty and not is_empty
            )
            entry: Dict[str, Any] = {
                "question": q.question,
                "expect_non_empty": q.expect_non_empty,
                "rows_returned": len(qr.rows) if qr.rows else 0,
                "error": qr.error,
                "passed": not failed,
            }
            results.append(entry)
            if failed:
                failures.append(entry)

        if failures:
            lines = [
                f"Q={f['question']!r}: got {f['rows_returned']} rows"
                + (f" (error: {f['error']})" if f["error"] else "")
                for f in failures
            ]
            return CheckResult(
                check_id=self.CHECK_ID,
                check_name=self.CHECK_NAME,
                passed=False,
                message=f"{len(failures)} NL question(s) failed: " + "; ".join(lines),
                details={"failures": failures, "all_results": results},
            )

        return CheckResult(
            check_id=self.CHECK_ID,
            check_name=self.CHECK_NAME,
            passed=True,
            message=f"all {len(results)} NL question(s) returned expected results",
            details={"all_results": results},
        )
