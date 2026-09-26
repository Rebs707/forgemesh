# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import re

from semantic_mapper.core.exceptions import QuerySafetyError

# Keywords that must never appear anywhere in a generated query.
_BANNED = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|REPLACE|MERGE|CALL|EXEC)\b",
    re.IGNORECASE,
)


def _strip_comments(sql: str) -> str:
    """Remove -- line comments and /* */ block comments."""
    # Block comments
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    # Line comments
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def validate_select_only(sql: str) -> None:
    """Raise QuerySafetyError if *sql* is not a pure SELECT statement.

    Checks:
    1. After stripping comments the statement must begin with SELECT.
    2. No banned DML/DDL keyword appears anywhere in the stripped text.
    """
    stripped = _strip_comments(sql).strip()

    if not stripped.upper().startswith("SELECT"):
        raise QuerySafetyError(
            f"Generated SQL does not begin with SELECT: {sql[:120]!r}"
        )

    match = _BANNED.search(stripped)
    if match:
        raise QuerySafetyError(
            f"Generated SQL contains banned keyword '{match.group()}': {sql[:120]!r}"
        )
