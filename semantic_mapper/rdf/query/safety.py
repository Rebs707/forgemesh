# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import re

from semantic_mapper.core.exceptions import QuerySafetyError

# Keywords banned in SPARQL update/admin operations
_BANNED = re.compile(
    r"\b(INSERT|DELETE|CLEAR|DROP|CREATE|LOAD|COPY|MOVE)\b",
    re.IGNORECASE,
)


def _strip_comments(sparql: str) -> str:
    """Remove # line comments from SPARQL.

    Only strips '#' that appear as standalone comments — at line start (with
    optional leading whitespace) or preceded by whitespace — so that '#' inside
    URI angle brackets (e.g. XMLSchema#) is preserved.
    """
    return re.sub(r"(?m)^\s*#[^\n]*|(?<=\s)#[^\n]*", "", sparql)


def _strip_prefixes(sparql: str) -> str:
    """Remove PREFIX declarations so the first keyword check works correctly."""
    return re.sub(r"PREFIX\s+\S*\s*<[^>]*>\s*", "", sparql, flags=re.IGNORECASE)


def validate_sparql_select_only(sparql: str) -> None:
    """Raise QuerySafetyError if *sparql* is not a pure SELECT query.

    Checks:
    1. After stripping comments and PREFIX declarations the statement must
       begin with SELECT.
    2. No banned SPARQL update / admin keyword appears anywhere in the
       stripped text.
    """
    stripped = _strip_prefixes(_strip_comments(sparql)).strip()

    if not stripped.upper().startswith("SELECT"):
        raise QuerySafetyError(
            f"Generated SPARQL does not begin with SELECT: {sparql[:120]!r}"
        )

    match = _BANNED.search(stripped)
    if match:
        raise QuerySafetyError(
            f"Generated SPARQL contains banned keyword '{match.group()}': {sparql[:120]!r}"
        )
