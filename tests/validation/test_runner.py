# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for ValidationRunner — the orchestrator."""
from __future__ import annotations

import pytest

from semantic_mapper.validation.context import ValidationContext
from semantic_mapper.validation.models import ValidationReport
from semantic_mapper.validation.runner import ValidationRunner


class TestValidationRunner:
    def test_returns_validation_report(self, good_ctx: ValidationContext) -> None:
        report = ValidationRunner().run(good_ctx)
        assert isinstance(report, ValidationReport)

    def test_has_ten_checks(self, good_ctx: ValidationContext) -> None:
        report = ValidationRunner().run(good_ctx)
        assert len(report.checks) == 10

    def test_check_ids_sequential(self, good_ctx: ValidationContext) -> None:
        report = ValidationRunner().run(good_ctx)
        ids = [c.check_id for c in report.checks]
        assert ids == ["C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8", "C9", "C10"]

    def test_passes_on_known_good_graph(self, good_ctx: ValidationContext) -> None:
        report = ValidationRunner().run(good_ctx)
        failed = [c for c in report.checks if not c.passed]
        # C7 is always skipped (no NL questions) → passes.
        # All others should pass on the known-good graph.
        assert report.all_passed, (
            f"Unexpected failures:\n"
            + "\n".join(f"  [{c.check_id}] {c.message}" for c in failed)
        )

    def test_report_metadata(self, good_ctx: ValidationContext) -> None:
        from .conftest import BASELINE_RUN_ID, GRAPH_URI, PROJECT_NAME
        report = ValidationRunner().run(good_ctx)
        assert report.project_name == PROJECT_NAME
        assert report.graph_uri == GRAPH_URI
        assert report.baseline_run_id == BASELINE_RUN_ID

    def test_summary_contains_all_check_ids(self, good_ctx: ValidationContext) -> None:
        report = ValidationRunner().run(good_ctx)
        summary = report.summary()
        for cid in ["C1", "C2", "C3", "C4", "C5", "C6", "C7"]:
            assert cid in summary, f"{cid} missing from summary"

    def test_n_passed_and_n_failed_sum_to_total(self, good_ctx: ValidationContext) -> None:
        report = ValidationRunner().run(good_ctx)
        assert report.n_passed + report.n_failed == len(report.checks)

    def test_extra_checks_appended(self, good_ctx: ValidationContext) -> None:
        from semantic_mapper.validation.checks.base import BaseCheck
        from semantic_mapper.validation.models import CheckResult

        class AlwaysPassCheck(BaseCheck):
            CHECK_ID = "CX"
            CHECK_NAME = "always_pass"

            def run(self, ctx):
                return CheckResult(
                    check_id="CX",
                    check_name="always_pass",
                    passed=True,
                    message="extra check ok",
                )

        report = ValidationRunner(extra_checks=[AlwaysPassCheck()]).run(good_ctx)
        assert len(report.checks) == 11
        assert report.checks[-1].check_id == "CX"
