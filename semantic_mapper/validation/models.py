# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Frozen Pydantic models for the validation check suite (Feature 15)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from pydantic import BaseModel, Field


class CheckResult(BaseModel, frozen=True):
    """Result of one validation check."""

    check_id: str          # e.g. "C1"
    check_name: str        # e.g. "entity_count_reconciliation"
    passed: bool
    message: str
    details: Dict[str, Any] = Field(default_factory=dict)


class ValidationReport(BaseModel, frozen=True):
    """Full suite result for one dataset run."""

    project_name: str
    graph_uri: str
    baseline_run_id: str
    run_at: datetime
    checks: List[CheckResult]

    @property
    def n_passed(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def n_failed(self) -> int:
        return sum(1 for c in self.checks if not c.passed)

    @property
    def all_passed(self) -> bool:
        return self.n_failed == 0

    def summary(self) -> str:
        lines = [
            f"Validation report — project={self.project_name}  graph={self.graph_uri}",
            f"  run_at   : {self.run_at.isoformat()}",
            f"  passed   : {self.n_passed}/{len(self.checks)}",
        ]
        for c in self.checks:
            icon = "✓" if c.passed else "✗"
            lines.append(f"  {icon} [{c.check_id}] {c.check_name}: {c.message}")
        return "\n".join(lines)
