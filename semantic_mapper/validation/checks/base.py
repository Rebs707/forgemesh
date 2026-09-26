# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Abstract base class for all validation checks."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..context import ValidationContext
from ..models import CheckResult


class BaseCheck(ABC):
    """A single parametric validation check.

    Subclasses implement ``run()`` and return a ``CheckResult``.  Checks must
    never raise — all failures should be captured in ``CheckResult.passed=False``
    with a descriptive ``message``.  Exceptions that escape the caller indicate a
    programming error, not a data error.
    """

    @abstractmethod
    def run(self, ctx: ValidationContext) -> CheckResult:
        """Execute the check against *ctx* and return the result."""
        ...
