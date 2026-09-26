# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Feature 06 Phase 1 — SPARQL funnel.

Decomposes a SPARQL WHERE clause into ordered triple patterns and executes
a progressive COUNT(DISTINCT ?subject) for each prefix, producing the
narrowing funnel shown in the proof view.

Unsupported constructs (UNION, subqueries) cause an early return with a
reason string rather than raising.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

from semantic_mapper.prove.query_rewriter import (
    ProofRewriteError,
    _check_unsupported_sparql,
    _extract_where_parts,
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FunnelStep:
    pattern_text: str
    pattern_type: str           # "triple" | "filter" | "optional"
    cumulative_subject_count: int
    sample_subjects: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_funnel(
    sparql: str,
    sparql_engine: Any,
    primary_subject_var: str = "",
    sample_limit: int = 50,
    row_binding: str = "",
    row_grouping_values: Optional[dict] = None,
) -> Tuple[List[FunnelStep], Optional[str]]:
    """Return (funnel_steps, unsupported_reason).

    If the query uses UNION, subqueries, or other unsupported constructs,
    returns ([], reason_string).  Never raises.

    row_grouping_values: dict of {sparql_var_name: value} for the clicked result
    row.  For each step, any grouping variable that appears in the cumulative
    pattern prefix gets a FILTER(STR(?var) = "value") clause injected into the
    count query, so counts narrow as row-specific constraints come into scope.
    """
    try:
        _check_unsupported_sparql(sparql)
    except ProofRewriteError as exc:
        return [], exc.unsupported_pattern

    try:
        _, where_body, _ = _extract_where_parts(sparql)
    except ProofRewriteError as exc:
        return [], str(exc)

    # If the WHERE body is a single GRAPH block, strip the wrapper and re-apply
    # it when building count/sample queries.  This is the common pattern when
    # queries target a named graph (GRAPH <...> { triple patterns }).
    graph_decl, inner_body = _strip_graph_block(where_body)
    body_to_parse = inner_body if inner_body else where_body

    try:
        patterns = _decompose(body_to_parse)
    except ValueError as exc:
        return [], str(exc)

    if not patterns:
        return [], "no_patterns"

    if not primary_subject_var:
        primary_subject_var = _detect_primary_var(patterns)
    if not primary_subject_var:
        return [], "no_subject_var"

    logger.info(
        "funnel: primary_var=%s graph=%s patterns=%d first_pattern=%r",
        primary_subject_var, graph_decl[:60] if graph_decl else "none",
        len(patterns), patterns[0][1][:80] if patterns else ""
    )
    prefix_block = _prefix_block(sparql)

    rgv = {k: v for k, v in (row_grouping_values or {}).items() if not str(k).startswith("_")}

    steps: List[FunnelStep] = []
    n = len(patterns)
    for idx in range(n):
        prefix = patterns[: idx + 1]
        where_clause = _build_where(prefix)
        # Inject row-specific FILTER clauses for grouping variables that are
        # now in scope (first appear in this cumulative prefix).  This makes
        # the count narrow at the step where each row-specific constraint is
        # introduced, so the funnel shows genuine per-row narrowing.
        step_filters = _row_filters_for_prefix(prefix, rgv)
        if graph_decl:
            where_clause = f"{graph_decl} {{ {where_clause}{step_filters} }}"
        else:
            where_clause = f"{where_clause}{step_filters}"
        count = _run_count(sparql_engine, prefix_block, primary_subject_var, where_clause, row_binding)
        sample: List[str] = []
        if idx == n - 1:
            sample = _run_sample(
                sparql_engine, prefix_block, primary_subject_var, where_clause, sample_limit, row_binding
            )
        steps.append(
            FunnelStep(
                pattern_text=prefix[-1][1],
                pattern_type=prefix[-1][0],
                cumulative_subject_count=count,
                sample_subjects=sample,
            )
        )

    return steps, None


def _strip_graph_block(where_body: str) -> Tuple[str, str]:
    """If WHERE body is a single top-level GRAPH block, return (graph_decl, inner_body).

    E.g. 'GRAPH <http://x/g> { ?s ?p ?o . }' →
         ('GRAPH <http://x/g>', '?s ?p ?o .')

    Returns ("", "") if not a single GRAPH block.
    """
    stripped = where_body.strip()
    m = re.match(r"^(GRAPH\s+<[^>]+>)\s*\{", stripped, re.IGNORECASE)
    if not m:
        return "", ""
    graph_decl = m.group(1).strip()
    brace_pos = stripped.index("{", m.start())
    try:
        end = _match_brace(stripped, brace_pos)
    except (ValueError, IndexError):
        return "", ""
    after = stripped[end + 1:].strip()
    if after:
        return "", ""
    inner = stripped[brace_pos + 1 : end].strip()
    return graph_decl, inner


# ---------------------------------------------------------------------------
# WHERE clause decomposition
# ---------------------------------------------------------------------------


def _decompose(where_body: str) -> List[Tuple[str, str]]:
    """Return [(pattern_type, pattern_text), ...] from a WHERE body string."""
    if re.search(r"\bUNION\b", where_body, re.IGNORECASE):
        raise ValueError("UNION")
    if re.search(r"\{\s*SELECT\b", where_body, re.IGNORECASE):
        raise ValueError("subquery")

    result: List[Tuple[str, str]] = []
    for tok_type, tok_text in _tokenize(where_body):
        if tok_type in ("optional", "filter"):
            if tok_text.strip():
                result.append((tok_type, tok_text.strip()))
        else:
            result.extend(_expand_triple_group(tok_text))

    return result


def _tokenize(where_body: str) -> List[Tuple[str, str]]:
    """Split WHERE body into ("optional"|"filter"|"triples", text) tokens."""
    chunks: List[Tuple[str, str]] = []
    i = 0
    s = where_body.strip()
    buf: List[str] = []

    def flush() -> None:
        t = "".join(buf).strip()
        if t:
            chunks.append(("triples", t))
        buf.clear()

    while i < len(s):
        # OPTIONAL { ... }
        if re.match(r"OPTIONAL\s*\{", s[i:], re.IGNORECASE):
            flush()
            brace = i + s[i:].index("{")
            end = _match_brace(s, brace)
            inner = s[brace + 1 : end].strip()
            chunks.append(("optional", f"OPTIONAL {{ {inner} }}"))
            i = end + 1
            continue

        # FILTER(...)
        if re.match(r"FILTER\s*\(", s[i:], re.IGNORECASE):
            flush()
            paren = i + s[i:].index("(")
            end = _match_paren(s, paren)
            inner = s[paren + 1 : end]
            chunks.append(("filter", f"FILTER({inner})"))
            i = end + 1
            continue

        buf.append(s[i])
        i += 1

    flush()
    return chunks


def _match_brace(s: str, open_pos: int) -> int:
    depth = 1
    i = open_pos + 1
    while i < len(s) and depth:
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
        i += 1
    return i - 1


def _match_paren(s: str, open_pos: int) -> int:
    depth = 1
    i = open_pos + 1
    in_str = False
    str_ch = ""
    while i < len(s) and depth:
        c = s[i]
        if in_str:
            if c == str_ch:
                in_str = False
        elif c in ('"', "'"):
            in_str, str_ch = True, c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    return i - 1


def _expand_triple_group(text: str) -> List[Tuple[str, str]]:
    """Expand a raw triples chunk (may use ; shorthand) into individual triples."""
    result: List[Tuple[str, str]] = []
    current_subject: str = ""

    for dot_part in _split_unquoted(text, "."):
        dot_part = dot_part.strip()
        if not dot_part:
            continue
        semi_parts = _split_unquoted(dot_part, ";")
        for j, sp in enumerate(semi_parts):
            sp = sp.strip()
            if not sp:
                continue
            if j == 0:
                tokens = sp.split(None, 1)
                if tokens:
                    current_subject = tokens[0]
                result.append(("triple", sp))
            elif current_subject:
                result.append(("triple", f"{current_subject} {sp}"))

    return result


def _split_unquoted(text: str, delimiter: str) -> List[str]:
    """Split *text* on *delimiter* outside quoted strings and angle-bracket URIs."""
    parts: List[str] = []
    buf: List[str] = []
    in_str = False
    in_uri = False  # inside <...>
    str_ch = ""
    for c in text:
        if in_uri:
            buf.append(c)
            if c == ">":
                in_uri = False
        elif in_str:
            buf.append(c)
            if c == str_ch:
                in_str = False
        elif c == "<":
            in_uri = True
            buf.append(c)
        elif c in ('"', "'"):
            in_str, str_ch = True, c
            buf.append(c)
        elif c == delimiter:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(c)
    parts.append("".join(buf))
    return parts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _detect_primary_var(patterns: List[Tuple[str, str]]) -> str:
    """Return the first ?variable that appears as a subject in a triple pattern."""
    for ptype, ptext in patterns:
        if ptype == "triple":
            m = re.match(r"\s*(\?[\w]+)", ptext)
            if m:
                return m.group(1)
    return ""


def _prefix_block(sparql: str) -> str:
    lines = [l for l in sparql.strip().splitlines() if l.strip().upper().startswith("PREFIX")]
    return "\n".join(lines) + "\n" if lines else ""


def _build_where(patterns: List[Tuple[str, str]]) -> str:
    parts: List[str] = []
    for ptype, ptext in patterns:
        parts.append(ptext + " ." if ptype == "triple" else ptext)
    return " ".join(parts)


def _row_filters_for_prefix(prefix: List[Tuple[str, str]], rgv: dict) -> str:
    """Return a SPARQL FILTER string for grouping values whose variables are in scope.

    Scans the variable names in the cumulative prefix patterns and builds
    FILTER(STR(?var) = "value") for each matching grouping key.
    """
    if not rgv:
        return ""
    in_scope = set()
    for _, ptext in prefix:
        for m in re.finditer(r"\?(\w+)", ptext):
            in_scope.add(m.group(1))
    parts = []
    for k, v in rgv.items():
        if k in in_scope:
            escaped = str(v).replace("\\", "\\\\").replace('"', '\\"')
            parts.append(f'FILTER(STR(?{k}) = "{escaped}")')
    return (" " + " ".join(parts)) if parts else ""


def _run_count(engine: Any, prefix_block: str, subject_var: str, where_clause: str, row_binding: str = "") -> int:
    binding = f"{row_binding} " if row_binding else ""
    q = f"{prefix_block}SELECT (COUNT(DISTINCT {subject_var}) AS ?n) WHERE {{ {binding}{where_clause} }}"
    try:
        rows = engine._execute_sparql(q)
        val = rows[0].get("n") if rows else None
        result = int(val) if val is not None else 0
        logger.info("funnel count [%s] → %s", where_clause[:100], result)
        return result
    except Exception as exc:
        logger.warning("funnel count query failed: %s | query: %s", exc, q[:300])
        return -1


def _run_sample(
    engine: Any, prefix_block: str, subject_var: str, where_clause: str, limit: int, row_binding: str = ""
) -> List[str]:
    binding = f"{row_binding} " if row_binding else ""
    q = f"{prefix_block}SELECT DISTINCT {subject_var} WHERE {{ {binding}{where_clause} }} LIMIT {limit}"
    var = subject_var.lstrip("?")
    try:
        rows = engine._execute_sparql(q)
        return [str(r[var]) for r in rows if r.get(var)]
    except Exception:
        return []
