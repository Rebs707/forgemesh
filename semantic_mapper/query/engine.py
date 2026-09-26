# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List

from sqlalchemy import Engine, text

from semantic_mapper.config.settings import Settings
from semantic_mapper.core.exceptions import QueryLLMError, QuerySafetyError
from semantic_mapper.query.context_builder import build_schema_context
from semantic_mapper.query.gold_context_builder import build_gold_schema_context
from semantic_mapper.query.models import QueryResult
from semantic_mapper.query.safety import validate_select_only
from semantic_mapper.schema.mapping_output import SemanticMappingReport

logger = logging.getLogger(__name__)

_RULES_SILVER = """\
3. To filter by entity class ALWAYS use `_ontology_class_uri LIKE '%ClassName'`
   where ClassName is one of the classes listed in the TABLE header.
   Never filter by any other column for class.
4. Only add a WHERE filter on `_ontology_class_uri` when you need to restrict to a
   specific class. If the question asks about all rows in a table, omit the filter.
5. When grouping or counting by entity class, always GROUP BY `_ontology_class_uri`
   and alias it: SUBSTRING_INDEX(`_ontology_class_uri`, '#', -1) AS class_name.\
"""

_RULES_GOLD = """\
3. Each gold table contains exactly one ontology class — the table name encodes it
   (e.g. gold_patient = Patient). Never filter or GROUP BY `_ontology_class_uri`.
4. For ranking queries ("top N most expensive", "highest X") use ORDER BY ... DESC
   LIMIT N on individual rows. Do NOT use MAX/GROUP BY unless you are genuinely
   aggregating across a business dimension (e.g. gender, state, encounterclass).
5. Group only by real business columns (e.g. GROUP BY gender). Never GROUP BY
   `_ontology_class_uri` in gold mode — it is the same value on every row.
5a. JOINS: the SCHEMA section contains a JOINS block listing the exact FK column
    pairs to use when joining gold tables (e.g. gold_procedure.patient = gold_patient.id).
    ALWAYS use those pairs. Never guess join columns from names like fips, zip, or lat.\
"""

_SYSTEM_PROMPT_TEMPLATE = """\
You are a SQL expert for a semantic data lake.
You translate natural language questions into MySQL SELECT statements
against {layer} tables produced by a semantic mapping pipeline.

RULES:
1. Only generate SELECT statements. Never INSERT, UPDATE, DELETE, DROP, or DDL.
2. Always add LIMIT {max_rows} unless the user explicitly asks for all rows.
{class_rules}
6. Columns marked "contains full URI" store values like '...#ClassName'. To display
   them readably use SUBSTRING_INDEX(`col`, '#', -1) AS label. To filter, use LIKE.
   NEVER apply SUBSTRING_INDEX to numeric columns (INTEGER, DECIMAL, DOUBLE, BIGINT)
   or to any column not explicitly marked as storing full URIs in the SCHEMA.
7. Use backtick-quoted identifiers. For qualified references always backtick table
   and column separately: `gold_procedure`.`base_cost`, NEVER `gold_procedure.base_cost`.
8. For columns with listed actual values, use ONLY those exact values when filtering.
9. CRITICAL: Only reference column names that are explicitly listed in the SCHEMA
   section above. Never invent, assume, or guess column names. If the data needed
   to answer the question does not exist in the schema, return null SQL and explain
   which column is missing. Do NOT substitute a different column as a workaround.
10. Always attempt COUNT, SUM, AVG, GROUP BY when columns exist in the schema.
    Only return null SQL if required columns or tables genuinely do not exist.
11. For columns shown in ACTUAL COLUMN VALUES as "stores URIs — actual fragments: ...",
    treat the listed fragments as the groupable categories for that column.
    Use SUBSTRING_INDEX(`col`, '#', -1) AS label for display and GROUP BY the raw
    column when aggregating (e.g. SUM, COUNT, AVG) across those categories.

SCHEMA:
{schema_context}

Respond ONLY with valid JSON:
{{"sql": "SELECT ...", "explanation": "one sentence", "tables_used": ["silver_raw_party"]}}\
"""

# Regex to find an existing LIMIT clause (captures the numeric value)
_LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+)\b", re.IGNORECASE)

# Regex to extract table names after FROM / JOIN keywords
_TABLE_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+`?(\w+)`?",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Categorical column filter
# ---------------------------------------------------------------------------
# Column name patterns that indicate PII or identifier columns — not useful
# as enumerable categories for SQL generation.
_SKIP_COL_RE = re.compile(
    r"(?:^|_)(?:email|addr|dob|birth|nm|name)(?:_|$)"
    r"|(?:^|_)id(?:_|$)",
    re.IGNORECASE,
)
# Value patterns that indicate non-categorical data
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")          # dates and timestamps
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$") # email addresses
_REF_CODE_RE = re.compile(r"^[A-Za-z]+-\d+$")         # e.g. AGR-001, P-001


def _is_categorical(col: str, values: list) -> bool:
    """Return True only when *values* represent useful enumerable categories.

    Filters out PII (emails, names, dates of birth), identifier-like columns
    (IDs, sequential reference codes, timestamps) — none of which help the
    LLM write better SQL than the schema definition alone already provides.
    """
    if _SKIP_COL_RE.search(col):
        return False
    if all(_DATE_RE.match(v) for v in values):
        return False
    if any(_EMAIL_RE.match(v) for v in values):
        return False
    if all(_REF_CODE_RE.match(v) for v in values):
        return False
    if all(v.isdigit() for v in values):
        return False
    return True


class SemanticQueryEngine:
    """Translate natural-language questions into SQL against silver or gold tables."""

    def __init__(
        self,
        report: SemanticMappingReport,
        silver_engine: Engine,
        settings: Settings,
        *,
        gold_engine: Engine | None = None,
        gold_definitions: list | None = None,
    ) -> None:
        self._silver_engine = silver_engine
        self._settings = settings
        # gold_engine takes precedence for SQL execution when provided
        self._exec_engine: Engine = gold_engine if gold_engine is not None else silver_engine

        if gold_engine is not None and gold_definitions is not None:
            actual_classes = self._fetch_actual_class_uris_gold(
                gold_definitions, gold_engine
            )
            self._schema_context = build_gold_schema_context(
                gold_definitions, actual_class_uris=actual_classes
            )
            self._schema_context += self._actual_value_hints_gold(
                gold_definitions, gold_engine, actual_classes
            )
            self._system_prompt = self._build_system_prompt(gold_mode=True)
            # Build column index for pre-execution validation
            self._gold_columns: Dict[str, set] = self._build_gold_column_index(
                gold_definitions, gold_engine
            )
        else:
            actual_classes = self._fetch_actual_class_uris(report)
            self._schema_context = build_schema_context(
                report, actual_class_uris=actual_classes
            )
            self._schema_context += self._actual_value_hints(report, actual_classes)
            self._system_prompt = self._build_system_prompt()
            self._gold_columns: Dict[str, set] = {}

        # Conversation history — list of {role, content} dicts for multi-turn queries.
        # Grows with each query(); cleared by reset_conversation().
        self._conversation_history: List[Dict[str, str]] = []

        # Telemetry (accumulated across all queries)
        self.api_calls: int = 0
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def query(self, question: str) -> QueryResult:
        """Translate *question* to SQL, execute it, and return a QueryResult.

        Never raises — all failures are captured in QueryResult.error.
        """
        # Step 1: LLM call
        try:
            llm_response = self._call_llm(question)
        except QueryLLMError as exc:
            logger.error("LLM call failed: %s", exc)
            return QueryResult(
                question=question,
                generated_sql="",
                explanation="",
                rows=[],
                row_count=0,
                tables_used=[],
                error=str(exc),
            )

        sql: str | None = llm_response.get("sql")
        explanation: str = llm_response.get("explanation", "")
        tables_used: List[str] = llm_response.get("tables_used") or []
        prompt_tokens: int = llm_response.get("_prompt_tokens", 0)
        completion_tokens: int = llm_response.get("_completion_tokens", 0)

        # Step 2: Null SQL means the question cannot be answered
        if not sql:
            result = QueryResult(
                question=question,
                generated_sql="",
                explanation=explanation,
                rows=[],
                row_count=0,
                tables_used=tables_used,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                error=f"Cannot answer: {explanation}",
            )
            self._record_turn(question, result)
            return result

        # Step 3: Safety check
        try:
            validate_select_only(sql)
        except QuerySafetyError as exc:
            logger.warning("Safety violation in generated SQL: %s", exc)
            result = QueryResult(
                question=question,
                generated_sql=sql,
                explanation=explanation,
                rows=[],
                row_count=0,
                tables_used=tables_used,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                error=str(exc),
            )
            self._record_turn(question, result)
            return result

        # Step 4: Enforce row cap
        sql = self._enforce_limit(sql)

        # Step 4a: Validate referenced columns exist (gold mode only)
        col_error = self._validate_gold_columns(sql)
        if col_error:
            result = QueryResult(
                question=question,
                generated_sql=sql,
                explanation=explanation,
                rows=[],
                row_count=0,
                tables_used=tables_used,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                error=col_error,
            )
            self._record_turn(question, result)
            return result

        # Step 5: Execute
        try:
            rows = self._execute_sql(sql)
        except Exception as exc:
            logger.error("SQL execution failed: %s", exc)
            result = QueryResult(
                question=question,
                generated_sql=sql,
                explanation=explanation,
                rows=[],
                row_count=0,
                tables_used=tables_used,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                error=str(exc),
            )
            self._record_turn(question, result)
            return result

        # Step 6: Tables used (prefer LLM value, fall back to regex)
        if not tables_used:
            tables_used = self._extract_tables_used(sql)

        result = QueryResult(
            question=question,
            generated_sql=sql,
            explanation=explanation,
            rows=rows,
            row_count=len(rows),
            tables_used=tables_used,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            error=None,
        )
        self._record_turn(question, result)
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_actual_class_uris(self, report: SemanticMappingReport) -> Dict[str, List[str]]:
        """Query each silver table for distinct ``_ontology_class_uri`` fragment values.

        Returns a mapping of silver_table_name → sorted list of class name fragments.
        Silently skips tables that don't exist yet.
        """
        result: Dict[str, List[str]] = {}
        for table in report.tables:
            silver_table = f"silver_{table.source_table}"
            try:
                with self._silver_engine.connect() as conn:
                    rows = conn.execute(
                        text(
                            f"SELECT DISTINCT `_ontology_class_uri`"
                            f" FROM `{silver_table}`"
                            f" WHERE `_ontology_class_uri` IS NOT NULL LIMIT 20"
                        )
                    ).fetchall()
                    if rows:
                        result[silver_table] = sorted(
                            str(r[0]).split("#")[-1].split("/")[-1]
                            for r in rows
                        )
            except Exception:
                pass
        return result

    def _actual_value_hints(
        self,
        report: SemanticMappingReport,
        actual_classes: Dict[str, List[str]],
    ) -> str:
        """Return a schema appendix with actual distinct values for each silver table column.

        - Non-URI low-cardinality columns: list exact values.
        - URI-valued columns: list URI fragments so the LLM knows what categories exist
          for GROUP BY and LIKE filtering (e.g. agr_type, rel_role).
        - Columns whose schema entry claims URI storage but whose actual values are not
          URIs get a TRANSFORMATION DID NOT FIRE warning.

        Silently skips tables that don't exist yet.
        """
        _MAX_DISTINCT = 20
        col_marker = "contains full URI after transformation"
        lines: list[str] = ["\n\nACTUAL COLUMN VALUES (sampled from silver tables):"]
        found_any = False

        for table in report.tables:
            silver_table = f"silver_{table.source_table}"
            try:
                with self._silver_engine.connect() as conn:
                    result = conn.execute(text(f"SELECT * FROM `{silver_table}` LIMIT 0"))
                    all_cols = list(result.keys())

                    for col in all_cols:
                        if col.startswith("_"):
                            continue  # skip metadata columns
                        try:
                            rows = conn.execute(
                                text(
                                    f"SELECT DISTINCT `{col}` FROM `{silver_table}` "
                                    f"WHERE `{col}` IS NOT NULL LIMIT {_MAX_DISTINCT + 1}"
                                )
                            ).fetchall()
                        except Exception:
                            continue

                        values = [str(r[0]) for r in rows]
                        if not values or len(values) > _MAX_DISTINCT:
                            continue

                        if any(v.startswith("http") for v in values):
                            # URI-valued column — expose fragments so the LLM can use them
                            # for GROUP BY, LIKE filtering, and SUBSTRING_INDEX display.
                            frags = sorted(
                                v.split("#")[-1].split("/")[-1] for v in values
                            )
                            if not _is_categorical(col, frags):
                                continue
                            found_any = True
                            frags_str = ", ".join(f"'{f}'" for f in frags)
                            lines.append(
                                f"  {silver_table}.{col} stores URIs"
                                f" — use LIKE '%Fragment' to filter"
                                f" or SUBSTRING_INDEX(`{col}`, '#', -1) for display."
                                f" Actual fragments: {frags_str}"
                            )
                            continue

                        if not _is_categorical(col, values):
                            continue

                        found_any = True
                        vals_str = ", ".join(f"'{v}'" for v in sorted(values))

                        # If the schema context claims this column stores full URIs
                        # but actual values are not URIs, flag it so the LLM does
                        # not try to use it for class filtering.
                        col_line_is_uri_claim = any(
                            col_marker in line and f"  {col} " in line
                            for line in self._schema_context.splitlines()
                        )
                        if col_line_is_uri_claim:
                            lines.append(
                                f"  {silver_table}.{col}: TRANSFORMATION DID NOT FIRE"
                                f" — actual values are {vals_str}."
                                f" Do NOT use this column for class filtering;"
                                f" use _ontology_class_uri instead."
                            )
                        else:
                            lines.append(f"  {silver_table}.{col}: {vals_str}")
            except Exception:
                continue  # table not yet created

        return "\n".join(lines) if found_any else ""

    def _build_system_prompt(self, gold_mode: bool = False) -> str:
        return _SYSTEM_PROMPT_TEMPLATE.format(
            layer="gold" if gold_mode else "silver",
            max_rows=self._settings.QUERY_MAX_ROWS,
            schema_context=self._schema_context,
            class_rules=_RULES_GOLD if gold_mode else _RULES_SILVER,
        )

    def _record_turn(self, question: str, result: "QueryResult") -> None:  # type: ignore[name-defined]
        """Append a user/assistant turn to the conversation history."""
        if result.error:
            assistant_content = json.dumps({
                "sql": result.generated_sql or None,
                "explanation": result.explanation,
                "error": result.error,
            })
        else:
            assistant_content = json.dumps({
                "sql": result.generated_sql,
                "explanation": result.explanation,
                "rows_returned": result.row_count,
            })
        self._conversation_history.append({"role": "user", "content": question})
        self._conversation_history.append({"role": "assistant", "content": assistant_content})

    def reset_conversation(self) -> None:
        """Clear conversation history, starting a fresh context."""
        self._conversation_history.clear()

    @property
    def turn_count(self) -> int:
        """Number of completed question/answer turns in the current conversation."""
        return len(self._conversation_history) // 2

    def _call_llm(self, question: str) -> Dict[str, Any]:
        """Call the OpenAI API and return the parsed JSON dict.

        Raises QueryLLMError on API failure or unparseable response.
        Injects telemetry keys _prompt_tokens / _completion_tokens.
        """
        try:
            from semantic_mapper.core.llm import make_openai_client
            client = make_openai_client(self._settings)
            response = client.chat.completions.create(
                model=self._settings.QUERY_MODEL,
                messages=[
                    {"role": "system", "content": self._system_prompt},
                    *self._conversation_history,
                    {"role": "user", "content": question},
                ],
                response_format={"type": "json_object"},
                temperature=0,
                timeout=self._settings.LLM_REQUEST_TIMEOUT,
            )
        except Exception as exc:
            raise QueryLLMError(f"OpenAI API call failed: {exc}") from exc

        self.api_calls += 1
        prompt_tokens = 0
        completion_tokens = 0
        if response.usage:
            prompt_tokens = response.usage.prompt_tokens
            completion_tokens = response.usage.completion_tokens
            self.prompt_tokens += prompt_tokens
            self.completion_tokens += completion_tokens

        raw_content = response.choices[0].message.content
        try:
            parsed: Dict[str, Any] = json.loads(raw_content)
        except (json.JSONDecodeError, TypeError) as exc:
            raise QueryLLMError(
                f"LLM returned unparseable JSON: {raw_content[:200]!r}"
            ) from exc

        parsed["_prompt_tokens"] = prompt_tokens
        parsed["_completion_tokens"] = completion_tokens
        return parsed

    def _execute_sql(self, sql: str) -> List[Dict[str, Any]]:
        """Execute *sql* and return rows as a list of dicts."""
        with self._exec_engine.connect() as conn:
            result = conn.execute(text(sql))
            columns = list(result.keys())
            return [dict(zip(columns, row)) for row in result.fetchall()]

    # ------------------------------------------------------------------
    # Gold mode helpers
    # ------------------------------------------------------------------

    def _fetch_actual_class_uris_gold(
        self, definitions: list, engine: Engine
    ) -> Dict[str, List[str]]:
        """Query each gold table for distinct _ontology_class_uri values."""
        result: Dict[str, List[str]] = {}
        for definition in definitions:
            tbl = definition.gold_table_name
            try:
                with engine.connect() as conn:
                    rows = conn.execute(
                        text(
                            f"SELECT DISTINCT `_ontology_class_uri`"
                            f" FROM `{tbl}`"
                            f" WHERE `_ontology_class_uri` IS NOT NULL LIMIT 20"
                        )
                    ).fetchall()
                    if rows:
                        result[tbl] = sorted(
                            str(r[0]).split("#")[-1].split("/")[-1]
                            for r in rows
                        )
            except Exception:
                pass
        return result

    def _actual_value_hints_gold(
        self,
        definitions: list,
        engine: Engine,
        actual_classes: Dict[str, List[str]],
    ) -> str:
        """Return a schema appendix with actual distinct values from gold table columns."""
        _MAX_DISTINCT = 20
        lines: list[str] = ["\n\nACTUAL COLUMN VALUES (sampled from gold tables):"]
        found_any = False

        for definition in definitions:
            tbl = definition.gold_table_name
            try:
                with engine.connect() as conn:
                    result = conn.execute(text(f"SELECT * FROM `{tbl}` LIMIT 0"))
                    all_cols = list(result.keys())

                    for col in all_cols:
                        if col.startswith("_"):
                            continue
                        try:
                            rows = conn.execute(
                                text(
                                    f"SELECT DISTINCT `{col}` FROM `{tbl}` "
                                    f"WHERE `{col}` IS NOT NULL LIMIT {_MAX_DISTINCT + 1}"
                                )
                            ).fetchall()
                        except Exception:
                            continue

                        values = [str(r[0]) for r in rows]
                        if not values or len(values) > _MAX_DISTINCT:
                            continue

                        if any(v.startswith("http") for v in values):
                            frags = sorted(
                                v.split("#")[-1].split("/")[-1] for v in values
                            )
                            if not _is_categorical(col, frags):
                                continue
                            found_any = True
                            frags_str = ", ".join(f"'{f}'" for f in frags)
                            lines.append(
                                f"  {tbl}.{col} stores URIs"
                                f" — use LIKE '%Fragment' to filter"
                                f" or SUBSTRING_INDEX(`{col}`, '#', -1) for display."
                                f" Actual fragments: {frags_str}"
                            )
                        else:
                            if not _is_categorical(col, values):
                                continue
                            found_any = True
                            vals_str = ", ".join(f"'{v}'" for v in sorted(values))
                            lines.append(f"  {tbl}.{col}: {vals_str}")
            except Exception:
                continue

        return "\n".join(lines) if found_any else ""

    def _build_gold_column_index(self, definitions: list, engine: Engine) -> Dict[str, set]:
        """Return {gold_table_name: set_of_actual_column_names} by querying the DB."""
        index: Dict[str, set] = {}
        for definition in definitions:
            tbl = definition.gold_table_name
            try:
                with engine.connect() as conn:
                    result = conn.execute(text(f"SELECT * FROM `{tbl}` LIMIT 0"))
                    index[tbl] = set(result.keys())
            except Exception:
                pass
        return index

    # Matches `table`.`column` or table.column (unquoted) in generated SQL.
    _QUALIFIED_COL_RE = re.compile(
        r"`(\w+)`\.`(\w+)`"        # `table`.`column`
        r"|`(\w+)`\.\s*(\w+)"      # `table`.column
        r"|(\w+)\.\s*`(\w+)`"      # table.`column`
    )

    def _validate_gold_columns(self, sql: str) -> str | None:
        """Return an error string if the SQL references columns not present in gold tables.

        Only runs in gold mode (when _gold_columns is populated). Returns None when
        all referenced columns are valid.
        """
        if not self._gold_columns:
            return None

        unknown: List[str] = []
        for match in self._QUALIFIED_COL_RE.finditer(sql):
            # Extract (table, column) from whichever group pair matched
            table = match.group(1) or match.group(3) or match.group(5)
            col   = match.group(2) or match.group(4) or match.group(6)
            if not table or not col:
                continue
            if table not in self._gold_columns:
                continue  # table unknown to us — let DB handle it
            if col not in self._gold_columns[table]:
                available = ", ".join(sorted(self._gold_columns[table]))
                unknown.append(
                    f"Column `{col}` does not exist in `{table}`. "
                    f"Available columns: {available}"
                )

        return "; ".join(unknown) if unknown else None

    def _enforce_limit(self, sql: str) -> str:
        """Ensure LIMIT is present and does not exceed QUERY_MAX_ROWS."""
        max_rows = self._settings.QUERY_MAX_ROWS
        match = _LIMIT_RE.search(sql)
        if match:
            existing = int(match.group(1))
            if existing > max_rows:
                sql = _LIMIT_RE.sub(f"LIMIT {max_rows}", sql)
        else:
            sql = sql.rstrip().rstrip(";") + f" LIMIT {max_rows}"
        return sql

    @staticmethod
    def _extract_tables_used(sql: str) -> List[str]:
        """Extract table names following FROM / JOIN keywords."""
        return list(dict.fromkeys(_TABLE_RE.findall(sql)))
