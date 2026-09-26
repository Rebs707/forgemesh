# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 3 — execute_sql_silver and execute_sql_gold tool implementation.

Pure execution: validate SQL is SELECT-only, run against the target database,
return rows + a generated query_id.  No LLM involved.
"""
from __future__ import annotations

import datetime as _dt
import time
import uuid
from decimal import Decimal as _Decimal
from typing import Any, Dict, List


def _json_safe(value: Any) -> Any:
    """Coerce a DB cell into a JSON-serialisable value.

    MySQL returns ``SUM``/``AVG`` and computed columns as ``Decimal``, and
    date/time columns as ``datetime``/``date`` — none of which ``json.dumps``
    handles, so a routine aggregate (e.g. ``SUM(CASE WHEN … THEN 1 END)``) would
    otherwise crash the gold/silver SQL endpoints. Integral Decimals become int,
    fractional ones float; temporals become ISO strings; bytes are decoded.
    """
    if isinstance(value, _Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return value


def run_execute_sql(
    db_engine: Any,
    sql: str,
    question_hint: str = "",
    mode: str = "gold",
) -> Dict[str, Any]:
    """Validate and execute a SQL SELECT query.

    Parameters
    ----------
    db_engine:
        SQLAlchemy engine connected to the target (silver or gold) database.
    sql:
        The SQL SELECT string constructed by the agent.
    question_hint:
        The user's original question (optional).
    mode:
        ``"silver"`` or ``"gold"`` — recorded in the result for tracing.

    Returns
    -------
    dict with keys: query_id, rows, columns, row_count, duration_ms, mode.
    On error: adds an ``error`` key and returns empty rows.
    """
    from sqlalchemy import text as _text
    from semantic_mapper.query.safety import validate_select_only
    from semantic_mapper.core.exceptions import QuerySafetyError

    try:
        validate_select_only(sql)
    except QuerySafetyError as exc:
        return {
            "error": f"Safety error: {exc}",
            "query_id": None,
            "rows": [],
            "columns": [],
            "row_count": 0,
            "duration_ms": 0,
            "mode": mode,
        }

    query_id = "q_" + uuid.uuid4().hex[:12]
    t0 = time.monotonic()
    try:
        with db_engine.connect() as conn:
            rows = [
                {k: _json_safe(v) for k, v in r._mapping.items()}
                for r in conn.execute(_text(sql))
            ]
    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        return {
            "error": f"Execution error: {exc}",
            "query_id": query_id,
            "rows": [],
            "columns": [],
            "row_count": 0,
            "duration_ms": duration_ms,
            "mode": mode,
        }

    duration_ms = int((time.monotonic() - t0) * 1000)
    columns: List[str] = list(rows[0].keys()) if rows else []
    return {
        "query_id": query_id,
        "rows": rows,
        "columns": columns,
        "row_count": len(rows),
        "duration_ms": duration_ms,
        "mode": mode,
        "question_hint": question_hint or None,
    }
