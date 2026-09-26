# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Feature 15 — Dataset Validation Test Suite.

Public API::

    from semantic_mapper.validation import (
        ValidationContext,
        ValidationRunner,
        ValidationReport,
        CheckResult,
        NLQuestion,
        RdflibBackend,
        FusekiBackend,
    )
"""
from .context import NLQuestion, ValidationContext
from .models import CheckResult, ValidationReport
from .runner import ValidationRunner
from .sparql_backend import FusekiBackend, RdflibBackend, SparqlBackend

__all__ = [
    "ValidationContext",
    "ValidationRunner",
    "ValidationReport",
    "CheckResult",
    "NLQuestion",
    "SparqlBackend",
    "RdflibBackend",
    "FusekiBackend",
]
