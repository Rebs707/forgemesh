# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Phase 3 — Proof query rewriter.

Given an executed query and its mode, produces a ProofQuery that returns the
underlying subjects or contributing rows rather than the aggregated answer.

SPARQL decomposition rules:
  COUNT(?x)           → SELECT DISTINCT ?x WHERE { ... }
  SUM/AVG(?v)         → SELECT ?subject ?v WHERE { ... }
  GROUP BY ?g + agg   → SELECT ?g + inner vars WHERE { ... }
  No aggregate        → pass through (with sampling limit applied)
  HAVING / MINUS / subquery → ProofRewriteError

SQL rules (silver / gold modes):
  Strip SELECT projection.  Replace with SELECT *.
  Drop ORDER BY and existing LIMIT.
  Add LIMIT PROVE_MAX_SUBJECTS (or PROVE_SAMPLE_SIZE when sampled).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MAX_SUBJECTS = 1000
_DEFAULT_SAMPLE_SIZE = 200

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ProofRewriteError(Exception):
    def __init__(self, message: str, unsupported_pattern: str) -> None:
        super().__init__(message)
        self.unsupported_pattern = unsupported_pattern


@dataclass(frozen=True)
class ProofQuery:
    proof_query: str
    mode: str                    # "silver" | "gold" | "sparql"
    sampled: bool
    original_count: Optional[int]
    sample_limit: int            # the LIMIT value actually applied in proof_query
    grouping_fields: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class RowProofQuery:
    proof_query: str
    mode: str
    row_grouping_values: Dict[str, Any]
    sampled: bool


# ---------------------------------------------------------------------------
# Internal regex
# ---------------------------------------------------------------------------

# Matches (COUNT|SUM|AVG|MIN|MAX)(?inner) AS ?alias  inside a SELECT clause.
_AGG_RE = re.compile(
    r"""\(\s*
        (COUNT|SUM|AVG|MIN|MAX)        # aggregate function
        \s*\(\s*
        (?:DISTINCT\s+)?               # optional DISTINCT inside agg
        (\?[\w]+|\*)                   # inner variable or *
        \s*\)\s*
        AS\s+(\?[\w]+)                 # AS ?alias
        \s*\)""",
    re.IGNORECASE | re.VERBOSE,
)

_GROUP_BY_RE = re.compile(
    r"""\bGROUP\s+BY\b\s+
        ((?:\?[\w]+\s*)+)             # one or more ?vars
    """,
    re.IGNORECASE | re.VERBOSE,
)

_ORDER_BY_RE = re.compile(r"\bORDER\s+BY\b.*?(?=\bLIMIT\b|\bOFFSET\b|$)", re.IGNORECASE | re.DOTALL)
_LIMIT_RE = re.compile(r"\bLIMIT\s+\d+", re.IGNORECASE)
_OFFSET_RE = re.compile(r"\bOFFSET\s+\d+", re.IGNORECASE)

# Unsupported SPARQL patterns
_UNSUPPORTED: dict[str, re.Pattern] = {
    "HAVING": re.compile(r"\bHAVING\b", re.IGNORECASE),
    "MINUS": re.compile(r"\bMINUS\b", re.IGNORECASE),
    "subquery": re.compile(r"\{\s*SELECT\b", re.IGNORECASE),
}

# SQL: everything between SELECT and FROM (the projection)
_SQL_PROJECTION_RE = re.compile(
    r"^\s*SELECT\s+(?:DISTINCT\s+)?.+?\bFROM\b",
    re.IGNORECASE | re.DOTALL,
)

# Matches `table`.`col` or table.col in a SELECT clause
_SQL_TABLE_COL_RE = re.compile(
    r"[`\"]?(\w+)[`\"]?\s*\.\s*[`\"]?(\w+)[`\"]?",
    re.IGNORECASE,
)


def _col_table_map(query: str) -> Dict[str, str]:
    """Return {col_lower: table} extracted from the SELECT projection."""
    m = re.match(r"^\s*SELECT\s+(.*?)\s+FROM\b", query, re.IGNORECASE | re.DOTALL)
    if not m:
        return {}
    result: Dict[str, str] = {}
    for t_match in _SQL_TABLE_COL_RE.finditer(m.group(1)):
        result[t_match.group(2).lower()] = t_match.group(1)
    return result

_SQL_ORDER_BY_RE  = re.compile(r"\bORDER\s+BY\b.*?(?=\bLIMIT\b|\bHAVING\b|;|\s*$)", re.IGNORECASE | re.DOTALL)
_SQL_GROUP_BY_RE  = re.compile(r"\bGROUP\s+BY\b.*?(?=\bHAVING\b|\bORDER\b|\bLIMIT\b|;|\s*$)", re.IGNORECASE | re.DOTALL)
_SQL_HAVING_RE    = re.compile(r"\bHAVING\b.*?(?=\bORDER\b|\bLIMIT\b|;|\s*$)", re.IGNORECASE | re.DOTALL)
_SQL_LIMIT_RE     = re.compile(r"\bLIMIT\s+\d+", re.IGNORECASE)
_SQL_FROM_TABLE_RE = re.compile(r"\bFROM\s+[`\"]?(\w+)[`\"]?", re.IGNORECASE)


def _first_table_from_sql(query: str) -> str:
    """Return the first table name after FROM (without backticks)."""
    m = _SQL_FROM_TABLE_RE.search(query)
    return m.group(1) if m else ""


def _select_star_clause(query: str) -> str:
    """Return 'SELECT `table`.* FROM' for JOINs, 'SELECT * FROM' otherwise.

    Qualifying SELECT to the first (fact) table avoids duplicate audit columns
    (_gold_id, _bronze_source_ref) when multiple gold tables are joined.
    """
    if re.search(r"\bJOIN\b", query, re.IGNORECASE):
        first = _first_table_from_sql(query)
        if first:
            return f"SELECT `{first}`.* FROM"
    return "SELECT * FROM"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def rewrite(
    query: str,
    mode: str,
    original_count: Optional[int] = None,
    max_subjects: int = _DEFAULT_MAX_SUBJECTS,
    sample_size: int = _DEFAULT_SAMPLE_SIZE,
) -> ProofQuery:
    """Rewrite *query* into a proof query for the given *mode*.

    Args:
        query:          The original executed query string.
        mode:           "sparql", "silver", or "gold".
        original_count: The scalar result from the original query (e.g. the
                        COUNT value), used to decide whether to sample.
        max_subjects:   Return at most this many proof rows (default 1000).
        sample_size:    LIMIT used when original_count exceeds max_subjects.

    Returns:
        ProofQuery with proof_query, sampled flag, and sample_limit.

    Raises:
        ProofRewriteError: for unsupported SPARQL patterns.
        ValueError:       for unknown mode.
    """
    if mode == "sparql":
        return _rewrite_sparql(query, max_subjects, sample_size, original_count)
    if mode in ("silver", "gold"):
        return _rewrite_sql(query, mode, max_subjects, sample_size, original_count)
    raise ValueError(f"Unknown mode {mode!r}; expected 'sparql', 'silver', or 'gold'")


# ---------------------------------------------------------------------------
# SPARQL rewriter
# ---------------------------------------------------------------------------


def _check_unsupported_sparql(query: str) -> None:
    for pattern_name, pattern_re in _UNSUPPORTED.items():
        if pattern_re.search(query):
            raise ProofRewriteError(
                f"Unsupported SPARQL pattern '{pattern_name}' cannot be rewritten into a proof query",
                unsupported_pattern=pattern_name,
            )


def _extract_where_parts(query: str) -> tuple[str, str, str]:
    """Return (select_clause, where_body, tail).

    *select_clause*  everything from the start up to (not including) WHERE.
    *where_body*     contents inside the outermost WHERE { ... }.
    *tail*           everything after the closing } (GROUP BY, LIMIT, …).
    """
    where_match = re.search(r"\bWHERE\s*\{", query, re.IGNORECASE)
    if not where_match:
        raise ProofRewriteError("No WHERE clause found", "missing_where")

    select_clause = query[: where_match.start()]

    # Walk the string to find the matching closing brace.
    start = where_match.end()
    depth = 1
    i = start
    while i < len(query) and depth > 0:
        if query[i] == "{":
            depth += 1
        elif query[i] == "}":
            depth -= 1
        i += 1

    where_body = query[start : i - 1]
    tail = query[i:].strip()
    return select_clause, where_body, tail


def _unique_vars(text: str) -> list[str]:
    """Return unique ?variable names in order of first appearance."""
    seen: set[str] = set()
    result = []
    for v in re.findall(r"\?[\w]+", text):
        if v not in seen:
            seen.add(v)
            result.append(v)
    return result


def _rewrite_sparql(
    query: str,
    max_subjects: int,
    sample_size: int,
    original_count: Optional[int],
) -> ProofQuery:
    _check_unsupported_sparql(query)

    select_clause, where_body, tail = _extract_where_parts(query)

    aggregates = _AGG_RE.findall(select_clause)  # [(func, inner_var, alias_var), ...]

    # Extract GROUP BY vars for grouping_fields
    group_by_match_full = _GROUP_BY_RE.search(tail + " " + select_clause)
    group_by_vars_full: list[str] = []
    if group_by_match_full:
        group_by_vars_full = re.findall(r"\?[\w]+", group_by_match_full.group(1))
    grouping_fields = [v.lstrip("?") for v in group_by_vars_full]

    if not aggregates:
        # No rewrite needed — pass through WHERE body, drop ORDER BY / existing LIMIT.
        sampled = original_count is not None and original_count > max_subjects
        limit = sample_size if sampled else max_subjects
        tail_clean = _strip_tail_clauses(tail)
        proof = f"SELECT * WHERE {{{where_body}}}"
        proof = f"{proof} LIMIT {limit}"
        return ProofQuery(proof.strip(), "sparql", sampled, original_count, limit, grouping_fields)

    # ---- There are aggregates: build a de-aggregated projection. ----

    # GROUP BY variables (in order).
    group_by_match = _GROUP_BY_RE.search(tail + " " + select_clause)
    group_by_vars: list[str] = []
    if group_by_match:
        group_by_vars = re.findall(r"\?[\w]+", group_by_match.group(1))

    # Non-aggregate plain variables in the SELECT clause.
    select_no_agg = _AGG_RE.sub("", select_clause)
    plain_select_vars = _unique_vars(select_no_agg)
    # Exclude DISTINCT keyword residue.
    plain_select_vars = [v for v in plain_select_vars if v.lower() != "?distinct"]

    # Inner variables from aggregate expressions (skip COUNT(*)).
    inner_vars = [agg[1] for agg in aggregates if agg[1] != "*"]

    # Alias variables introduced by aggregates — we must NOT include these.
    alias_vars = {agg[2].lower() for agg in aggregates}

    all_count = all(agg[0].upper() == "COUNT" for agg in aggregates)

    # Build the proof projection.
    if all_count and not group_by_vars and not plain_select_vars:
        # Pure COUNT: SELECT (COUNT(?x) AS ?n) → SELECT DISTINCT ?x
        if inner_vars:
            projection = f"DISTINCT {inner_vars[0]}"
        else:
            # COUNT(*) — expose all WHERE variables.
            where_vars = [v for v in _unique_vars(where_body) if v.lower() not in alias_vars]
            projection = "DISTINCT " + " ".join(where_vars) if where_vars else "*"
    else:
        # SUM/AVG, GROUP BY, or mixed: collect vars in priority order.
        seen: set[str] = set()
        proof_vars: list[str] = []

        for v in group_by_vars + plain_select_vars + inner_vars:
            lv = v.lower()
            if lv not in seen and lv not in alias_vars:
                seen.add(lv)
                proof_vars.append(v)

        if not proof_vars:
            # Fallback: all vars in WHERE body that aren't aggregate aliases.
            proof_vars = [v for v in _unique_vars(where_body) if v.lower() not in alias_vars]

        projection = " ".join(proof_vars) if proof_vars else "*"

    sampled = original_count is not None and original_count > max_subjects
    limit = sample_size if sampled else max_subjects

    proof = f"SELECT {projection} WHERE {{{where_body}}} LIMIT {limit}"
    return ProofQuery(proof.strip(), "sparql", sampled, original_count, limit, grouping_fields)


def _strip_tail_clauses(tail: str) -> str:
    """Remove GROUP BY, ORDER BY, LIMIT, OFFSET from the tail of a SPARQL query."""
    tail = re.sub(r"\bGROUP\s+BY\b.*?(?=\bORDER\b|\bLIMIT\b|\bOFFSET\b|$)", "", tail, flags=re.IGNORECASE | re.DOTALL)
    tail = _ORDER_BY_RE.sub("", tail)
    tail = _LIMIT_RE.sub("", tail)
    tail = _OFFSET_RE.sub("", tail)
    return tail.strip()


# ---------------------------------------------------------------------------
# SQL rewriter (silver / gold)
# ---------------------------------------------------------------------------


def _rewrite_sql(
    query: str,
    mode: str,
    max_subjects: int,
    sample_size: int,
    original_count: Optional[int],
) -> ProofQuery:
    # Extract GROUP BY columns BEFORE stripping them — these become grouping_fields
    # so the frontend knows which columns are real group keys (not aggregate results).
    group_by_match = _SQL_GROUP_BY_RE.search(query)
    grouping_fields: List[str] = []
    if group_by_match:
        raw = group_by_match.group(0)
        # Strip "GROUP BY" keyword, split on comma, extract bare identifiers
        cols_str = re.sub(r"^GROUP\s+BY\s*", "", raw, flags=re.IGNORECASE).strip()
        for part in cols_str.split(","):
            col = re.sub(r"[`'\"\s]", "", part.split(".")[-1]).strip()
            if col:
                grouping_fields.append(col)

    # Replace SELECT ... FROM with SELECT <table>.* FROM (JOIN) or SELECT * FROM.
    proof = _SQL_PROJECTION_RE.sub(_select_star_clause(query), query, count=1)

    # Drop GROUP BY / HAVING / ORDER BY so SELECT * works without
    # ONLY_FULL_GROUP_BY errors and returns individual rows for lineage.
    proof = _SQL_HAVING_RE.sub("", proof)
    proof = _SQL_GROUP_BY_RE.sub("", proof)
    proof = _SQL_ORDER_BY_RE.sub("", proof)

    # Drop existing LIMIT.
    proof = _SQL_LIMIT_RE.sub("", proof)

    proof = proof.rstrip().rstrip(";").strip()

    sampled = original_count is not None and original_count > max_subjects
    limit = sample_size if sampled else max_subjects

    proof = f"{proof} LIMIT {limit}"
    return ProofQuery(proof, mode, sampled, original_count, limit, grouping_fields)


# ---------------------------------------------------------------------------
# Row-level proof rewriter
# ---------------------------------------------------------------------------


def rewrite_for_row_proof(
    original_query: str,
    mode: str,
    row_grouping_values: Dict[str, Any],
    max_subjects: int = _DEFAULT_MAX_SUBJECTS,
) -> RowProofQuery:
    """Derive a row-level proof query that returns the contributing subjects
    for a specific grouping row.

    Args:
        original_query:      The original executed query string.
        mode:                "sparql", "silver", or "gold".
        row_grouping_values: The GROUP BY column values for the specific row,
                             e.g. {"short_title": "Acute resp failure"}.
        max_subjects:        Maximum subjects to return (default 1000).

    Returns:
        RowProofQuery with proof_query, mode, row_grouping_values, sampled.

    Raises:
        ProofRewriteError: for unsupported SPARQL patterns.
        ValueError:        for unknown mode.
    """
    if mode == "sparql":
        _check_unsupported_sparql(original_query)
        select_clause, where_body, tail = _extract_where_parts(original_query)

        # Find GROUP BY vars
        group_by_match = _GROUP_BY_RE.search(tail + " " + select_clause)
        group_by_vars: list[str] = []
        if group_by_match:
            group_by_vars = re.findall(r"\?[\w]+", group_by_match.group(1))

        if not group_by_vars or not row_grouping_values:
            # No GROUP BY — delegate to the main proof rewrite so that COUNT(*)
            # gets the same SELECT DISTINCT ?subjectVar treatment that correctly
            # surfaces subject URIs (SELECT * can return empty results in Fuseki
            # for named-graph patterns when no variable projection is specified).
            pq = _rewrite_sparql(original_query, max_subjects, max_subjects, None)
            return RowProofQuery(pq.proof_query, mode, row_grouping_values, pq.sampled)

        # Find the subject var: inner var of the first aggregate if not "*"
        aggregates = _AGG_RE.findall(select_clause)
        subject_var: str
        if aggregates and aggregates[0][1] != "*":
            subject_var = aggregates[0][1]  # e.g. "?adm"
        else:
            # Fallback: first WHERE var not in GROUP BY
            where_vars = _unique_vars(where_body)
            group_by_lower = {v.lower() for v in group_by_vars}
            non_group = [v for v in where_vars if v.lower() not in group_by_lower]
            subject_var = non_group[0] if non_group else (where_vars[0] if where_vars else "?s")

        # Build FILTER clauses for each grouping value.
        # Fuseki always returns binding values as strings in its JSON response,
        # regardless of the stored XSD type.  A string like "51881" that was
        # originally xsd:integer would fail a plain string FILTER.  Use
        # STR(?var) = "value" so the comparison is type-agnostic.
        filter_clauses: list[str] = []
        for var_name, val in row_grouping_values.items():
            sparql_var = f"?{var_name}"
            if isinstance(val, bool):
                filter_clauses.append(f"FILTER({sparql_var} = {'true' if val else 'false'})")
            elif isinstance(val, (int, float)):
                filter_clauses.append(f"FILTER({sparql_var} = {val})")
            else:
                escaped = str(val).replace('"', '\\"')
                filter_clauses.append(f'FILTER(STR({sparql_var}) = "{escaped}")')

        filter_str = " ".join(filter_clauses)
        new_body = where_body.rstrip() + "\n  " + filter_str if filter_str else where_body

        # Select all WHERE-clause variables so the caller can capture every
        # URI-typed binding (admission, diagnoses_icd, icd_diagnose, etc.),
        # not just the primary grouping subject.
        all_where_vars = _unique_vars(where_body)
        # Ensure subject_var is listed first; deduplicate preserving order.
        seen: set = set()
        ordered_vars: list[str] = []
        for v in [subject_var] + all_where_vars:
            if v not in seen:
                seen.add(v)
                ordered_vars.append(v)
        select_str = " ".join(ordered_vars)

        return RowProofQuery(
            f"SELECT DISTINCT {select_str} WHERE {{{new_body}}} LIMIT {max_subjects}",
            mode,
            row_grouping_values,
            False,
        )

    if mode in ("silver", "gold"):
        # Extract GROUP BY columns from the original query — only these are real
        # filterable columns; aggregate aliases (COUNT(*) AS count) must be skipped.
        group_by_m = re.search(
            r"\bGROUP\s+BY\b(.+?)(?=\bHAVING\b|\bORDER\b|\bLIMIT\b|;|\s*$)",
            original_query, re.IGNORECASE | re.DOTALL,
        )
        group_by_cols: set[str] = set()
        if group_by_m:
            for part in group_by_m.group(1).split(","):
                col = re.sub(r"[`'\"\s]", "", part.split(".")[-1]).strip()
                if col:
                    group_by_cols.add(col.lower())

        # Replace SELECT ... FROM with SELECT <table>.* FROM (JOIN) or SELECT * FROM
        proof = _SQL_PROJECTION_RE.sub(_select_star_clause(original_query), original_query, count=1)

        # Drop ORDER BY
        proof = _SQL_ORDER_BY_RE.sub("", proof)

        # Drop existing LIMIT
        proof = _SQL_LIMIT_RE.sub("", proof)

        # Strip GROUP BY
        proof = re.sub(
            r"\bGROUP\s+BY\b.*?(?=\bHAVING\b|\bORDER\b|\bLIMIT\b|;|\s*$)",
            "",
            proof,
            flags=re.IGNORECASE | re.DOTALL,
        )

        # Strip HAVING
        proof = re.sub(
            r"\bHAVING\b.*?(?=\bORDER\b|\bLIMIT\b|;|\s*$)",
            "",
            proof,
            flags=re.IGNORECASE | re.DOTALL,
        )

        proof = proof.rstrip().rstrip(";").strip()

        if row_grouping_values:
            col_map = _col_table_map(original_query)
            conditions: list[str] = []
            for col, val in row_grouping_values.items():
                safe_col = col.replace("`", "")
                # Skip aggregate expressions (COUNT, SUM, …) — can't appear in WHERE.
                if re.search(r"\b(COUNT|SUM|AVG|MIN|MAX|GROUP_CONCAT)\s*\(", safe_col, re.IGNORECASE):
                    continue
                # Skip columns not in GROUP BY when GROUP BY is present —
                # they are aggregate result aliases (e.g. "count") with no column in the table.
                if group_by_cols and safe_col.lower() not in group_by_cols:
                    continue
                table = col_map.get(safe_col.lower(), "")
                # For single-table queries with no JOIN, use unqualified col name.
                if not table and re.search(r"\bJOIN\b", proof, re.IGNORECASE):
                    continue
                col_expr = f"`{table}`.`{safe_col}`" if table else f"`{safe_col}`"
                if val is None:
                    conditions.append(f"{col_expr} IS NULL")
                elif isinstance(val, bool):
                    conditions.append(f"{col_expr} = {1 if val else 0}")
                elif isinstance(val, (int, float)):
                    conditions.append(f"{col_expr} = {val}")
                else:
                    escaped_val = str(val).replace("'", "''")
                    conditions.append(f"{col_expr} = '{escaped_val}'")

            if conditions:
                condition_str = " AND ".join(conditions)
                if re.search(r"\bWHERE\b", proof, re.IGNORECASE):
                    proof = proof + f" AND ({condition_str})"
                else:
                    proof = proof + f" WHERE {condition_str}"

        return RowProofQuery(proof + f" LIMIT {max_subjects}", mode, row_grouping_values, False)

    raise ValueError(f"Unknown mode {mode!r}; expected 'sparql', 'silver', or 'gold'")
