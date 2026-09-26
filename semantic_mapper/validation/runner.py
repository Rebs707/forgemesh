# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""ValidationRunner — orchestrates the full C1–C7 check suite."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

from .checks import (
    CardinalityCheck,
    CategoricalCanonicalisationCheck,
    EntityCountCheck,
    EntityLinkingCheck,
    IncompletenessCheck,
    JoinIntegrityCheck,
    LoadStampCheck,
    NLQueryCheck,
    ProvenanceCheck,
    TerminologyBindingCheck,
)
from .checks.base import BaseCheck
from .context import ValidationContext
from .models import CheckResult, ValidationReport

logger = logging.getLogger(__name__)


class ValidationRunner:
    """Run the full validation check suite against a single dataset.

    Parameters
    ----------
    extra_checks:
        Optional additional ``BaseCheck`` instances to append after the
        standard C1–C7 suite.  Useful for dataset-specific checks that
        cannot be expressed generically.
    cardinality_threshold:
        Ratio threshold forwarded to :class:`CardinalityCheck` (default 1.5).
    """

    def __init__(
        self,
        *,
        extra_checks: Optional[List[BaseCheck]] = None,
        cardinality_threshold: float = 1.5,
    ) -> None:
        self._checks: List[BaseCheck] = [
            EntityCountCheck(),
            CardinalityCheck(ratio_threshold=cardinality_threshold),
            JoinIntegrityCheck(),
            ProvenanceCheck(),
            IncompletenessCheck(),
            LoadStampCheck(),
            NLQueryCheck(),
            CategoricalCanonicalisationCheck(),
            EntityLinkingCheck(),
            TerminologyBindingCheck(),
        ]
        if extra_checks:
            self._checks.extend(extra_checks)

    def run(self, ctx: ValidationContext) -> ValidationReport:
        """Execute all checks and return a :class:`ValidationReport`.

        Never raises — individual check exceptions are captured as failed
        ``CheckResult`` entries.
        """
        results: List[CheckResult] = []
        for check in self._checks:
            logger.info(
                "Running %s [%s]",
                check.__class__.__name__,
                getattr(check, "CHECK_ID", "?"),
            )
            result = check.run(ctx)
            results.append(result)
            icon = "✓" if result.passed else "✗"
            logger.info(
                "  %s [%s] %s: %s",
                icon,
                result.check_id,
                result.check_name,
                result.message,
            )

        report = ValidationReport(
            project_name=ctx.project_name,
            graph_uri=ctx.graph_uri,
            baseline_run_id=ctx.baseline_run_id,
            run_at=datetime.now(tz=timezone.utc),
            checks=results,
        )
        logger.info(
            "Validation complete — %d/%d passed",
            report.n_passed,
            len(report.checks),
        )
        return report
