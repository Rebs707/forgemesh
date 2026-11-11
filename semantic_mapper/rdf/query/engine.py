# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from semantic_mapper.config.settings import Settings
from semantic_mapper.core.exceptions import QueryLLMError, QuerySafetyError
from semantic_mapper.gold.models import GoldTableDefinition
from semantic_mapper.query.models import QueryResult
from semantic_mapper.rdf.query.context_builder import build_sparql_schema_context, _detect_ontology_namespace
from semantic_mapper.rdf.query.safety import validate_sparql_select_only

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT_TEMPLATE = """\
You are a SPARQL expert for a semantic health data lake backed by an Apache Jena Fuseki triplestore.
You translate natural-language questions into SPARQL SELECT queries that return human-readable results.

RULES:
1. Only generate SELECT queries. Never INSERT, UPDATE, DELETE, CONSTRUCT, DESCRIBE, ASK, or any SPARQL update.
2. Always wrap triple patterns in GRAPH <{named_graph}> {{ … }}.
3. Always add LIMIT {max_rows} unless the user explicitly asks for all results.
4. Use the exact property URIs listed in the SCHEMA below — never invent URIs.
5. Use ONLY the PREFIX declarations listed in the SCHEMA PREFIXES block.
   NEVER create a new PREFIX or use a base URI as a shortcut for a namespace it does not cover.
   In particular: the data: prefix covers individual subject URIs only — never use it for class or property URIs.
6. For object properties (FK relationships), use the join pattern shown in the SCHEMA.
   The pattern is always: ?fromURI <objectProperty> ?toURI — the variable binds to a URI.
   CRITICAL — JOIN VARIABLE REUSE: In SPARQL, a join between two entities works because
   they share the same variable. If ?fooURI is already bound in the query, the join triple
   MUST reuse that exact name:
     ?barURI <ont:hasFoo> ?fooURI .         ← CORRECT: shared ?fooURI creates the join
     ?barURI <ont:hasFoo> ?fooRef .          ← WRONG: ?fooRef is a new independent variable;
                                                this produces a CARTESIAN PRODUCT and
                                                completely wrong counts / results.
   Never rename the join variable. Copy the variable name from the SCHEMA join pattern.
   CRITICAL — MULTI-HOP: After following an object property, the resulting variable IS the
   linked entity — access its data properties directly. Do NOT use a code or key as an
   intermediate step:
     ?aURI <ont:hasB> ?bURI .              ← ?bURI is now the B entity (a URI)
     ?bURI <ont:hasBText> ?bText .         ← read B's data properties directly from ?bURI
   WRONG — never do this:
     ?aURI <ont:hasB> ?bCode .             ← names it like a literal, but it IS a URI
     ?bURI <ont:hasCode> ?bCode .          ← URI ≠ string literal → always 0 rows
   Object properties store entity URIs; data properties store literal values (strings,
   integers, dates). These two kinds of values are NEVER interchangeable as join variables.
   Do NOT extract a literal from a data property and then match it to an object property —
   the types differ and the join will always return 0 rows.
   For VALUE-MATCHING JOINS (properties shared by two entities without an object property),
   use a shared literal variable: ?aURI <prop> ?sharedVal . ?bURI <prop> ?sharedVal .
   The VALUE-MATCHING JOINS section of the SCHEMA lists ALL available cross-entity value joins.
   Any cross-entity join NOT listed there must use a direct object property as the join.
7. Use FILTER for numeric or string comparisons; use OPTIONAL for nullable properties.
8. Only reference classes and properties that are explicitly listed in the SCHEMA.
   If the data needed to answer the question is not in the schema, return null SPARQL and explain.

ENTITY TYPE RULES — CRITICAL:
9. Some entity classes have many discriminated subtypes (e.g. an Order class with
   50+ subtypes for payment method, shipping method, sales channel, etc.).
   NEVER enumerate all subtypes in a UNION just to match "all instances" of an entity.
   Instead, bind the entity variable purely via its data properties — the rdf:type is not
   needed to locate instances when you are querying by property values.
   BAD:  {{ ?orderURI a <ChannelSubtype1> }} UNION {{ ?orderURI a <ChannelSubtype2> }} … (50 UNION branches)
   GOOD: ?orderURI <ont:hasOrderGiftFlag> 1 .   (entity bound by property, no type filter needed)
   Only add a ?uri a <SpecificType> triple when the question explicitly asks to FILTER
   to that specific subtype (e.g. "express orders only" → add a <ExpressShippingMethod> filter).

CATEGORICAL VALUE FORMATS:
10. Categorical property values are stored exactly as they appear in the source data.
   Do NOT assume standard codes or abbreviations — values vary by dataset.
   When filtering on a categorical, use LCASE(STR(?var)) = LCASE("user value")
   to handle case variation, or use CONTAINS/REGEX for partial matches.
   Example: FILTER(LCASE(STR(?paymentMethod)) = "credit card")
            FILTER(CONTAINS(LCASE(STR(?maritalStatus)), "married"))

READABILITY RULES (these are mandatory — violations produce useless output):
11. NEVER include raw URI variables (?xxxURI, ?s, ?o, etc.) or any variable bound via an
   object property join as SELECT output columns — those variables hold entity URIs, not
   readable values. URI variables exist only for WHERE clause pattern matching and joins.
12. For every entity type referenced in the query, SELECT its human-readable data properties
    using the DISPLAY FIELDS listed in the SCHEMA for that class.
    A result set full of UUIDs or raw row references is unacceptable.
13. Name every SELECT variable descriptively using camelCase that reflects the data:
    ?customerFirstName, ?customerLastName, ?orderDate, ?productDescription
    — NOT ?custURI, ?ordURI, ?prodURI, ?x, ?val.
14. SPARQL aggregate syntax — MANDATORY:
    a) Every aggregate in SELECT must be wrapped in parentheses with AS inside:
         CORRECT:   (COUNT(DISTINCT ?x) AS ?xCount)
         WRONG:     COUNT(DISTINCT ?x) AS ?xCount
       Arithmetic over aggregates must also be fully wrapped:
         CORRECT:   ((COUNT(DISTINCT ?x) + COUNT(DISTINCT ?y)) AS ?total)
    b) GROUP BY IS MANDATORY whenever SELECT contains any aggregate.
       List EVERY non-aggregate SELECT variable in GROUP BY — missing even one causes a parse error.
       Example: SELECT ?a ?b (COUNT(?x) AS ?n) … GROUP BY ?a ?b
    c) In HAVING and ORDER BY you may reference the alias defined in SELECT (?xCount, ?total).
    d) To count entities that meet a per-entity aggregate condition ("how many X have more
       than N Y"), group in a subquery and count its rows. Never COUNT the GROUP BY variable
       itself — that returns 1 for every group.
         CORRECT:   SELECT (COUNT(*) AS ?xCount) WHERE {{ {{ SELECT ?x WHERE {{ ?y ont:hasX ?x }}
                      GROUP BY ?x HAVING (COUNT(?y) > 3) }} }}
         WRONG:     SELECT (COUNT(DISTINCT ?x) AS ?xCount) WHERE {{ ?y ont:hasX ?x }}
                      GROUP BY ?x HAVING (COUNT(?y) > 3)
15. When a question asks "for each X, list Y", use OPTIONAL for Y so entities
    with no Y still appear. SELECT the display fields of X and Y, not their URIs.
16. Always use OPTIONAL for nullable properties such as hasMiddleName, hasShippedDate,
    hasCancellationReason — these are not present on every entity.

SCHEMA:
{schema_context}
{dataset_notes}
Respond ONLY with valid JSON:
{{"sparql": "SELECT …", "explanation": "one sentence", "classes_used": ["ClassName"]}}\
"""

# Regex to find an existing LIMIT clause (captures the numeric value)
_LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+)\b", re.IGNORECASE)

# Standard prefixes to inject when used but not declared
_STANDARD_PREFIXES: Dict[str, str] = {
    "xsd":  "http://www.w3.org/2001/XMLSchema#",
    "rdf":  "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "owl":  "http://www.w3.org/2002/07/owl#",
}
_DECLARED_PREFIX_RE  = re.compile(r"PREFIX\s+(\w+)\s*:", re.IGNORECASE)
_USED_PREFIX_RE      = re.compile(r"\b(\w+):[A-Za-z_]")
_URI_RE              = re.compile(r"<[^>]*>")


_AGGREGATE_RE  = re.compile(r"\b(COUNT|SUM|AVG|MIN|MAX|GROUP_CONCAT)\s*\(", re.IGNORECASE)
_GROUP_BY_RE   = re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE)
# Matches bare ?var in SELECT (not inside an aggregate expression or parenthesised alias)
_SELECT_VAR_RE = re.compile(r"(?<!\w)\?(\w+)(?!\s*\))")


def _extract_filter_blocks(sparql: str) -> List[Tuple[int, int]]:
    """Return (start, end) char positions for every FILTER(…) block.

    Counts parentheses so nested calls like FILTER(CONTAINS(LCASE(?x), "y"))
    are captured in full.
    """
    blocks: List[Tuple[int, int]] = []
    for m in re.finditer(r"\bFILTER\s*\(", sparql, re.IGNORECASE):
        depth = 0
        i = m.end() - 1  # position of the opening '('
        while i < len(sparql):
            if sparql[i] == "(":
                depth += 1
            elif sparql[i] == ")":
                depth -= 1
                if depth == 0:
                    blocks.append((m.start(), i + 1))
                    break
            i += 1
    return blocks


def _fix_missing_group_by(sparql: str) -> str:
    """If the query uses aggregates but has no GROUP BY, inject one.

    Extracts non-aggregate SELECT variables by stripping aggregate sub-expressions
    from the SELECT clause, then collecting remaining ?var tokens.
    """
    if not _AGGREGATE_RE.search(sparql):
        return sparql
    if _GROUP_BY_RE.search(sparql):
        return sparql

    # Isolate the SELECT clause (between SELECT and WHERE)
    select_match = re.search(r"\bSELECT\b(.*?)\bWHERE\b", sparql, re.IGNORECASE | re.DOTALL)
    if not select_match:
        return sparql

    select_clause = select_match.group(1)
    # Remove aggregate sub-expressions: (AGG(...) AS ?alias) or AGG(...)
    stripped = re.sub(
        r"\(\s*(?:COUNT|SUM|AVG|MIN|MAX|GROUP_CONCAT)\s*\(.*?\)\s*(?:AS\s*\?\w+)?\s*\)",
        "",
        select_clause,
        flags=re.IGNORECASE | re.DOTALL,
    )
    group_vars = list(dict.fromkeys(
        f"?{m}" for m in _SELECT_VAR_RE.findall(stripped)
    ))

    if not group_vars:
        return sparql

    group_clause = "GROUP BY " + " ".join(group_vars)

    # Insert GROUP BY before HAVING / ORDER BY / LIMIT, or before the final closing brace
    for keyword in (r"\bHAVING\b", r"\bORDER\s+BY\b", r"\bLIMIT\b"):
        m = re.search(keyword, sparql, re.IGNORECASE)
        if m:
            return sparql[:m.start()] + group_clause + "\n" + sparql[m.start():]

    return sparql.rstrip() + "\n" + group_clause


_SELECT_KW_RE = re.compile(r"\bSELECT\b", re.IGNORECASE)
_COUNT_DISTINCT_PROJ_RE = re.compile(
    r"^\s*\(\s*COUNT\s*\(\s*DISTINCT\s+\?(\w+)\s*\)\s+AS\s+\?(\w+)\s*\)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_GROUP_TAIL_RE = re.compile(
    r"^\s*GROUP\s+BY\s+(?P<vars>(?:\?\w+\s*)+)"
    r"(?P<having>HAVING\s*\(.*?\))?\s*"
    r"(?P<rest>(?:ORDER\s+BY\b.*?)?(?:LIMIT\s+\d+\s*)?(?:OFFSET\s+\d+\s*)?)$",
    re.IGNORECASE | re.DOTALL,
)


def _fix_count_of_groups(sparql: str) -> Tuple[str, Optional[str]]:
    """Rewrite a query that counts its own grouping variable into a count of groups.

    ``SELECT (COUNT(DISTINCT ?x) AS ?n) WHERE {…} GROUP BY ?x [HAVING …]``
    returns one row per group, each with ?n = 1, because counting the grouping
    variable inside its own group is always 1. Whatever the question, that answer
    is degenerate; the intended answer is the number of groups that survive the
    HAVING filter. The rewrite moves the grouping into a subquery and counts its
    rows:

    ``SELECT (COUNT(*) AS ?n) WHERE { { SELECT ?x WHERE {…} GROUP BY ?x [HAVING …] } }``

    Only fires when the query has a single SELECT whose sole projection is
    ``COUNT(DISTINCT ?v)`` and ``?v`` is one of the GROUP BY variables. Returns the
    (possibly rewritten) query and a human-readable note, or None when unchanged.
    """
    selects = list(_SELECT_KW_RE.finditer(sparql))
    if len(selects) != 1:
        return sparql, None
    sel = selects[0]

    where_m = re.search(r"\bWHERE\s*\{", sparql[sel.end():], re.IGNORECASE)
    if not where_m:
        return sparql, None
    proj_end = sel.end() + where_m.start()
    projection = sparql[sel.end():proj_end]
    proj_m = _COUNT_DISTINCT_PROJ_RE.match(projection)
    if not proj_m:
        return sparql, None
    counted_var, alias = proj_m.group(1), proj_m.group(2)

    # Find the closing brace of the WHERE group pattern.
    body_start = sel.end() + where_m.end() - 1  # index of "{"
    depth = 0
    body_end = -1
    for i in range(body_start, len(sparql)):
        ch = sparql[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                body_end = i + 1
                break
    if body_end < 0:
        return sparql, None

    tail_m = _GROUP_TAIL_RE.match(sparql[body_end:])
    if not tail_m:
        return sparql, None
    group_vars = re.findall(r"\?(\w+)", tail_m.group("vars"))
    if counted_var not in group_vars:
        return sparql, None

    prologue = sparql[:sel.start()]
    body = sparql[body_start:body_end]
    having = (tail_m.group("having") or "").strip()
    limit_m = re.search(r"LIMIT\s+\d+", tail_m.group("rest") or "", re.IGNORECASE)
    group_clause = "GROUP BY " + " ".join(f"?{v}" for v in group_vars)
    inner = (
        f"SELECT {' '.join('?' + v for v in group_vars)} WHERE {body} "
        f"{group_clause}" + (f" {having}" if having else "")
    )
    rewritten = (
        f"{prologue}SELECT (COUNT(*) AS ?{alias}) WHERE {{ {{ {inner} }} }}"
        + (f" {limit_m.group(0)}" if limit_m else "")
    )
    note = (
        f"The generated query counted ?{counted_var} inside groups of ?{counted_var}, "
        "which gives 1 for every group. It was rewritten to count the groups instead. "
        "Original query: " + " ".join(sparql.split())
    )
    return rewritten, note


def _inject_missing_prefixes(sparql: str, extra: Optional[Dict[str, str]] = None) -> str:
    """Prepend PREFIX declarations for standard and dataset-specific namespaces used but not declared."""
    declared = set(_DECLARED_PREFIX_RE.findall(sparql))
    # Strip URI literals (<...>) so we don't false-match e.g. <http://...>
    without_uris = _URI_RE.sub("", sparql)
    used = set(_USED_PREFIX_RE.findall(without_uris))
    all_prefixes = {**_STANDARD_PREFIXES, **(extra or {})}
    missing = [
        f"PREFIX {pfx}: <{uri}>"
        for pfx, uri in all_prefixes.items()
        if pfx in used and pfx not in declared
    ]
    return "\n".join(missing) + "\n" + sparql if missing else sparql


class SparqlQueryEngine:
    """Translate natural-language questions into SPARQL SELECT queries against Fuseki.

    Public API mirrors SemanticQueryEngine so the REPL can use either engine
    without branching.
    """

    def __init__(
        self,
        definitions: List[GoldTableDefinition],
        settings: Settings,
    ) -> None:
        self._settings = settings
        ontology_graph = None
        if settings.ONTOLOGY_PATH:
            try:
                from semantic_mapper.ontology.loader import OntologyLoader
                ontology_graph = OntologyLoader.load(settings.ONTOLOGY_PATH)
            except Exception:
                pass

        self._schema_context = build_sparql_schema_context(
            definitions,
            named_graph=settings.RDF_NAMED_GRAPH,
            base_uri=settings.RDF_BASE_URI,
            ontology_graph=ontology_graph,
        )

        # Dataset-specific prefixes to inject when the LLM uses them without declaring them.
        # These supplement _STANDARD_PREFIXES (xsd/rdf/rdfs/owl).
        self._schema_prefixes: Dict[str, str] = {}
        ont_ns = _detect_ontology_namespace(definitions)
        if ont_ns:
            self._schema_prefixes["ont"] = ont_ns
        if settings.RDF_BASE_URI:
            self._schema_prefixes["data"] = settings.RDF_BASE_URI
        dataset_notes = ""
        if settings.QUERY_DATASET_NOTES.strip():
            dataset_notes = "\nDATASET-SPECIFIC NOTES (follow these exactly):\n" + settings.QUERY_DATASET_NOTES.strip() + "\n"
        self._system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
            named_graph=settings.RDF_NAMED_GRAPH,
            max_rows=settings.QUERY_MAX_ROWS,
            schema_context=self._schema_context,
            dataset_notes=dataset_notes,
        )

        # Deterministic post-processor maps (built from schema context)
        self._obj_prop_targets: Dict[str, str]  # propUri → targetEntityShort
        self._entity_text_props: Dict[str, str]  # entityShort → best text propUri
        self._obj_prop_targets, self._entity_text_props = self._build_schema_fix_maps()

        # Feature 12: ontology-grounded proof narrator
        from semantic_mapper.rdf.proof.narrator import ProofNarrator
        self._narrator = ProofNarrator(ontology_graph)

        # Conversation history — list of {role, content} dicts for multi-turn queries.
        self._conversation_history: List[Dict[str, str]] = []

        # Telemetry (accumulated across all queries)
        self.api_calls: int = 0
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def query(self, question: str, limit: Optional[int] = None, dry_run: bool = False) -> QueryResult:
        """Translate *question* to SPARQL, execute it, and return a QueryResult.

        *limit* overrides QUERY_MAX_ROWS for this call only.  When None,
        QUERY_MAX_ROWS is used as the default.  When *dry_run* is True, the
        SPARQL is generated and validated but not sent to Fuseki.
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

        sparql: str | None = llm_response.get("sparql")
        explanation: str = llm_response.get("explanation", "")
        classes_used: List[str] = llm_response.get("classes_used") or []
        prompt_tokens: int = llm_response.get("_prompt_tokens", 0)
        completion_tokens: int = llm_response.get("_completion_tokens", 0)

        # Step 2: Null SPARQL means the question cannot be answered
        if not sparql:
            result = QueryResult(
                question=question,
                generated_sql="",
                explanation=explanation,
                rows=[],
                row_count=0,
                tables_used=classes_used,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                error=f"Cannot answer: {explanation}",
            )
            self._record_turn(question, result)
            return result

        # Step 3: Safety check
        try:
            validate_sparql_select_only(sparql)
        except QuerySafetyError as exc:
            logger.warning("Safety violation in generated SPARQL: %s", exc)
            result = QueryResult(
                question=question,
                generated_sql=sparql,
                explanation=explanation,
                rows=[],
                row_count=0,
                tables_used=classes_used,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                error=str(exc),
            )
            self._record_turn(question, result)
            return result

        # Step 4: Enforce row cap
        sparql = self._enforce_limit(sparql, limit=limit)

        # Step 4b: Deterministic post-processing — fix object property variable mistakes
        sparql = self._fix_object_property_variables(sparql)

        # Step 4c: Count-of-groups — COUNT(DISTINCT ?x) … GROUP BY ?x yields 1 per group
        sparql, count_note = _fix_count_of_groups(sparql)
        query_rewrites: List[str] = [count_note] if count_note else []
        if count_note:
            logger.info("SPARQL post-process: rewrote count-of-groups query")

        # Step 5: Execute against Fuseki (skipped in dry-run mode)
        if dry_run:
            result = QueryResult(
                question=question,
                generated_sql=sparql,
                explanation=explanation,
                rows=[],
                row_count=0,
                tables_used=classes_used,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                error=None,
            )
            self._record_turn(question, result)
            return result

        try:
            rows = self._execute_sparql(sparql)
        except Exception as exc:
            logger.error("SPARQL execution failed: %s", exc)
            result = QueryResult(
                question=question,
                generated_sql=sparql,
                explanation=explanation,
                rows=[],
                row_count=0,
                tables_used=classes_used,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                error=str(exc),
            )
            self._record_turn(question, result)
            return result

        # Step 6: Zero-rows retry — if the query ran cleanly but returned nothing,
        # pass the bad SPARQL back with a targeted object-property diagnosis.
        # _record_turn has NOT been called yet so the history is still clean.
        if not rows:
            retry = self._retry_zero_rows(question, sparql, limit=limit)
            if retry is not None:
                logger.info("zero-rows retry returned %d rows", retry.row_count)
                self._record_turn(question, retry)
                return retry

        # Feature 12: generate ontology-grounded proof record
        proof_record = None
        query_id = getattr(result, "query_id", None) if "result" in dir() else None
        try:
            proof_record = self._narrator.narrate(
                sparql=sparql,
                nl_question=question,
                rows=rows,
                query_id=str(id(question)),  # stable within this call; caller may override
            )
        except Exception as exc:
            logger.warning("Proof narrator failed (non-fatal): %s", exc)

        result = QueryResult(
            question=question,
            generated_sql=sparql,
            explanation=explanation,
            rows=rows,
            row_count=len(rows),
            tables_used=classes_used,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            error=None,
            proof_record=proof_record,
            query_rewrites=query_rewrites,
        )
        self._record_turn(question, result)
        return result

    def reset_conversation(self) -> None:
        """Clear conversation history, starting a fresh context."""
        self._conversation_history.clear()

    @property
    def turn_count(self) -> int:
        """Number of completed question/answer turns in the current conversation."""
        return len(self._conversation_history) // 2

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _record_turn(self, question: str, result: QueryResult) -> None:
        """Append a user/assistant turn to the conversation history."""
        if result.error:
            assistant_content = json.dumps({
                "sparql": result.generated_sql or None,
                "explanation": result.explanation,
                "error": result.error,
            })
        else:
            assistant_content = json.dumps({
                "sparql": result.generated_sql,
                "explanation": result.explanation,
                "rows_returned": result.row_count,
            })
        self._conversation_history.append({"role": "user", "content": question})
        self._conversation_history.append({"role": "assistant", "content": assistant_content})

    def _call_llm(self, question: str) -> Dict[str, Any]:
        """Call the configured LLM and return the parsed JSON dict.

        Raises QueryLLMError on API failure or unparseable response.
        Injects telemetry keys _prompt_tokens / _completion_tokens.
        """
        provider = (self._settings.QUERY_PROVIDER or "openai").lower()
        if provider == "anthropic":
            return self._call_anthropic(question)
        if provider == "claude_code":
            return self._call_claude_code(question)
        return self._call_openai(question)

    def _call_openai(self, question: str) -> Dict[str, Any]:
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
        prompt_tokens = completion_tokens = 0
        if response.usage:
            prompt_tokens = response.usage.prompt_tokens
            completion_tokens = response.usage.completion_tokens
            self.prompt_tokens += prompt_tokens
            self.completion_tokens += completion_tokens

        raw_content = response.choices[0].message.content
        try:
            parsed: Dict[str, Any] = json.loads(raw_content)
        except (json.JSONDecodeError, TypeError) as exc:
            raise QueryLLMError(f"LLM returned unparseable JSON: {raw_content[:200]!r}") from exc

        parsed["_prompt_tokens"] = prompt_tokens
        parsed["_completion_tokens"] = completion_tokens
        return parsed

    def _call_claude_code(self, question: str) -> Dict[str, Any]:
        """Shell out to the `claude` CLI (non-interactive) — uses Max plan, no API billing."""
        import subprocess

        # Embed conversation history as formatted turns so the subprocess gets full context.
        history_text = ""
        for msg in self._conversation_history:
            role = "User" if msg["role"] == "user" else "Assistant"
            history_text += f"\n{role}: {msg['content']}"
        if history_text:
            history_text = f"\nPrior conversation:{history_text}\n"

        user_prompt = f"{history_text}User: {question}"

        try:
            import os as _os
            from semantic_mapper.core.llm import _claude_exe
            _env = _os.environ.copy()
            _local_bin = _os.path.expanduser("~/.local/bin")
            if _local_bin not in _env.get("PATH", ""):
                _env["PATH"] = _local_bin + _os.pathsep + _env.get("PATH", "")
            proc = subprocess.run(
                [
                    _claude_exe(),
                    "-p",
                    "--system-prompt", self._system_prompt,
                    "--model", self._settings.QUERY_ANTHROPIC_MODEL,
                    "--output-format", "json",
                    user_prompt,
                ],
                capture_output=True,
                text=True,
                timeout=self._settings.LLM_REQUEST_TIMEOUT,
                env=_env,
            )
        except Exception as exc:
            raise QueryLLMError(f"Claude Code subprocess failed: {exc}") from exc

        if proc.returncode != 0:
            raise QueryLLMError(
                f"Claude Code exited {proc.returncode}: {proc.stderr.strip()[:400]}"
            )

        self.api_calls += 1

        # Outer envelope: {"result": "<text>", "is_error": false, ...}
        try:
            envelope = json.loads(proc.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise QueryLLMError(
                f"Claude Code returned unparseable envelope: {proc.stdout[:200]!r}"
            ) from exc

        if envelope.get("is_error"):
            raise QueryLLMError(f"Claude Code error: {envelope.get('result', '')[:400]}")

        raw_content = envelope.get("result", "").strip()
        if raw_content.startswith("```"):
            raw_content = re.sub(r"^```[a-z]*\n?", "", raw_content).rstrip("` \n")
        from semantic_mapper.core.llm import repair_json
        raw_content = repair_json(raw_content)
        try:
            parsed: Dict[str, Any] = json.loads(raw_content)
        except (json.JSONDecodeError, TypeError) as exc:
            raise QueryLLMError(
                f"Claude Code returned unparseable JSON: {raw_content[:200]!r}"
            ) from exc

        parsed["_prompt_tokens"] = 0
        parsed["_completion_tokens"] = 0
        return parsed

    def _call_anthropic(self, question: str) -> Dict[str, Any]:
        try:
            import anthropic  # lazy import
            client = anthropic.Anthropic(api_key=self._settings.ANTHROPIC_API_KEY)
            response = client.messages.create(
                model=self._settings.QUERY_ANTHROPIC_MODEL,
                system=self._system_prompt,
                messages=[
                    *self._conversation_history,
                    {"role": "user", "content": question},
                ],
                max_tokens=2048,
            )
        except Exception as exc:
            raise QueryLLMError(f"Anthropic API call failed: {exc}") from exc

        self.api_calls += 1
        prompt_tokens = response.usage.input_tokens
        completion_tokens = response.usage.output_tokens
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens

        raw_content = response.content[0].text
        # Strip markdown code fences if the model wraps the JSON
        raw_content = raw_content.strip()
        if raw_content.startswith("```"):
            raw_content = re.sub(r"^```[a-z]*\n?", "", raw_content).rstrip("` \n")
        try:
            parsed: Dict[str, Any] = json.loads(raw_content)
        except (json.JSONDecodeError, TypeError) as exc:
            raise QueryLLMError(f"LLM returned unparseable JSON: {raw_content[:200]!r}") from exc

        parsed["_prompt_tokens"] = prompt_tokens
        parsed["_completion_tokens"] = completion_tokens
        return parsed

    def _build_schema_fix_maps(self) -> Tuple[Dict[str, str], Dict[str, str]]:
        """Parse the pre-built schema context into two lookup dicts.

        Returns:
            obj_prop_targets  — {propUri: targetEntityShort}
            entity_text_props — {entityShort: best_text_propUri}
        """
        obj_props: Dict[str, str] = {}
        text_props: Dict[str, str] = {}

        for line in self._schema_context.split("\n"):
            # Object property lines: "    <propUri>  object property → entityName"
            m = re.search(r"<([^>]+)>\s+object property\s*→\s*(\w+)", line)
            if m:
                obj_props[m.group(1)] = m.group(2)
                continue

            # Display field lines: "    ?entity_var  ← ?entityURI <propUri> ?entity_var"
            m = re.match(r"\s+\?(\w+)_(\w+)\s+←\s+\?\w+URI\s+<([^>]+)>", line)
            if m:
                entity, var_local, prop_uri = m.group(1), m.group(2).lower(), m.group(3)
                if entity not in text_props:
                    if any(kw in var_local for kw in (
                        "title", "name", "short", "desc", "label", "text", "reason"
                    )):
                        text_props[entity] = prop_uri

        return obj_props, text_props

    def _fix_object_property_variables(self, sparql: str) -> str:
        """Deterministic three-phase repair for the most common zero-row causes.

        Phase 1 — Rename: any variable bound by an object property that has a
          literal-sounding name (doesn't end in URI/Uri) is renamed to
          ?<targetEntity>URI throughout the query.

        Phase 2 — Remove self-referential triples: after renaming, a triple like
          ?icddiagnoseURI <ont:hasIcdProcedureCode> ?icddiagnoseURI is impossible
          in RDF and will always return 0 rows.  Remove it.

        Phase 3 — Fix text FILTERs on URI variables: if the renamed URI variable
          appears inside a FILTER(CONTAINS/REGEX …) block, the filter will never
          match because URIs are not human-readable strings.  Insert a data
          property hop to a text variable first, then redirect the filter.
        """
        if not self._obj_prop_targets:
            return sparql

        # ── Phase 1: collect renames ──────────────────────────────────────────
        renames: Dict[str, str] = {}  # old_var → new_var
        for m in re.finditer(r"\?(\w+)\s+<([^>]+)>\s+\?(\w+)", sparql):
            prop_uri, obj_var = m.group(2), m.group(3)
            target = self._obj_prop_targets.get(prop_uri)
            if not target:
                continue
            expected = f"{target}URI"
            if obj_var == expected or obj_var.lower().endswith("uri"):
                continue  # already correctly named
            if obj_var not in renames:
                renames[obj_var] = expected

        if not renames:
            return sparql

        # Apply renames longest-first to prevent partial-name collisions
        for old, new in sorted(renames.items(), key=lambda kv: -len(kv[0])):
            sparql = re.sub(r"\?" + re.escape(old) + r"\b", "?" + new, sparql)
            logger.info("SPARQL post-process: renamed ?%s → ?%s", old, new)

        # ── Phase 2: remove self-referential triples ──────────────────────────
        before = sparql
        sparql = re.sub(
            r"\?(?P<v>\w+)\s+<[^>]+>\s+\?(?P=v)\s*\.[ \t]*\n?", "", sparql
        )
        if sparql != before:
            logger.info("SPARQL post-process: removed self-referential triple(s)")

        # ── Phase 3: fix text FILTERs on URI variables ────────────────────────
        for old, new in renames.items():
            entity = new[:-3] if new.endswith("URI") else new
            text_prop = self._entity_text_props.get(entity)
            label_var = f"{entity}_label"

            # Scan filter blocks in reverse order to preserve string positions
            filter_blocks = _extract_filter_blocks(sparql)
            for start, end in reversed(filter_blocks):
                block = sparql[start:end]
                if f"?{new}" not in block:
                    continue
                if text_prop:
                    # Insert text triple before the FILTER; redirect variable inside it
                    new_block = block.replace(f"?{new}", f"?{label_var}")
                    replacement = f"?{new} <{text_prop}> ?{label_var} .\n    {new_block}"
                    sparql = sparql[:start] + replacement + sparql[end:]
                    logger.info(
                        "SPARQL post-process: inserted text hop ?%s + fixed FILTER", new
                    )
                else:
                    # No text property known. Keep the FILTER: removing it would answer
                    # a broader question than the one asked. An empty result is honest.
                    logger.info(
                        "SPARQL post-process: FILTER on URI ?%s kept (no text property to "
                        "redirect it to)", new
                    )
                break  # one filter per variable per pass

        return sparql

    def _retry_zero_rows(
        self,
        question: str,
        bad_sparql: str,
        *,
        limit: Optional[int] = None,
    ) -> Optional["QueryResult"]:
        """Make one LLM retry after a zero-row result.

        Passes the failing SPARQL back with concrete before/after diagnostics for
        every known zero-row pattern.  Returns a QueryResult if the retry produces
        rows; None on any failure.  The conversation history is still clean at call
        time (no _record_turn yet) so the retry message is an isolated user turn.
        """
        # Extract object properties from the pre-built schema context so the retry
        # message can enumerate exactly which properties return URIs in this schema.
        obj_prop_lines: List[str] = []
        for line in self._schema_context.split("\n"):
            m = re.search(r"<([^>]+)>\s+object property\s*[→-]+\s*(\S+)", line)
            if m:
                prop_local = m.group(1).split("#")[-1].split("/")[-1]
                target = m.group(2).rstrip(")")
                obj_prop_lines.append(
                    f"  {prop_local}  →  bind to ?{target}URI  (a URI, NOT a literal)"
                )

        obj_props_block = ""
        if obj_prop_lines:
            obj_props_block = (
                "\nOBJECT PROPERTIES in this schema — each returns a URI, never a literal:\n"
                + "\n".join(obj_prop_lines[:20])
                + "\n"
            )

        retry_msg = (
            "The SPARQL query below executed without errors but returned 0 rows.\n"
            "An empty result can be the correct answer. Change the query ONLY if it contains\n"
            "one of the four mistakes below; if none applies, return the query unchanged.\n"
            "Never broaden the question (e.g. by dropping a filter) just to get rows.\n\n"
            "MISTAKE 1 — Object property result named as a literal:\n"
            "  WRONG:   ?orderURI <ont:hasOrderProduct> ?productCode\n"
            "           (?productCode sounds like a string, but this property returns a URI)\n"
            "  CORRECT: ?orderURI <ont:hasOrderProduct> ?productURI\n"
            "           (name it after the entity it points to, ending in URI)\n\n"
            "MISTAKE 2 — FILTER applied directly to a URI variable:\n"
            "  WRONG:   FILTER(CONTAINS(LCASE(?productCode), 'garden'))\n"
            "           (URI strings like http://mesh.local/data/... never contain 'garden')\n"
            "  CORRECT: ?productURI <ont:hasProductName> ?productName .\n"
            "           FILTER(CONTAINS(LCASE(?productName), 'garden'))\n\n"
            "MISTAKE 3 — Entity variable never bound:\n"
            "  WRONG:   ?customerURI <ont:hasCustomerCountry> ?country  (if ?customerURI appears nowhere else)\n"
            "  CORRECT: ?orderURI <ont:hasOrderCustomer> ?customerURI .\n"
            "           ?customerURI <ont:hasCustomerCountry> ?country\n"
            "           (every ?xxxURI must be introduced by a triple that binds it)\n\n"
            "MISTAKE 4 — Join variable renamed after binding:\n"
            "  WRONG:   ?orderURI <ont:hasOrderCustomer> ?orderCustomer .  (binds ?orderCustomer)\n"
            "           ?customerURI <ont:hasCustomerCountry> ?country     (?customerURI unbound)\n"
            "  CORRECT: ?orderURI <ont:hasOrderCustomer> ?customerURI .    (binds ?customerURI)\n"
            "           ?customerURI <ont:hasCustomerCountry> ?country     (reuses same variable)\n"
            f"{obj_props_block}\n"
            f"Failing query:\n{bad_sparql}\n\n"
            f"Generate a corrected SPARQL for: {question}"
        )
        logger.info("zero-rows retry: calling LLM with targeted diagnosis")
        try:
            llm_response = self._call_llm(retry_msg)
        except QueryLLMError as exc:
            logger.warning("zero-rows retry: LLM call failed: %s", exc)
            return None

        sparql = llm_response.get("sparql")
        if not sparql:
            return None
        try:
            validate_sparql_select_only(sparql)
        except QuerySafetyError:
            return None

        sparql = self._enforce_limit(sparql, limit=limit)
        if " ".join(sparql.split()) == " ".join(bad_sparql.split()):
            return None  # the model judged the original correct: keep its empty answer
        try:
            rows = self._execute_sparql(sparql)
        except Exception as exc:
            logger.warning("zero-rows retry: execution failed: %s", exc)
            return None
        if not rows:
            return None  # no better than the original — keep the original empty answer

        return QueryResult(
            question=question,
            generated_sql=sparql,
            explanation=llm_response.get("explanation", ""),
            rows=rows,
            row_count=len(rows),
            tables_used=llm_response.get("classes_used") or [],
            prompt_tokens=llm_response.get("_prompt_tokens", 0),
            completion_tokens=llm_response.get("_completion_tokens", 0),
            error=None,
            query_rewrites=[
                "The first generated query returned no rows, so it was checked for "
                "known query-construction mistakes and regenerated. This answer comes "
                "from the regenerated query. Original query: "
                + " ".join(bad_sparql.split())
            ],
        )

    def _execute_sparql(self, sparql: str) -> List[Dict[str, Any]]:
        """POST *sparql* to Fuseki and return rows as a list of dicts."""
        import requests  # lazy import

        sparql = _fix_missing_group_by(sparql)
        sparql = _inject_missing_prefixes(sparql, extra=self._schema_prefixes)
        resp = requests.post(
            f"{self._settings.FUSEKI_URL}/{self._settings.RDF_DATASET_NAME}/sparql",
            data={"query": sparql},
            headers={"Accept": "application/sparql-results+json"},
            auth=(self._settings.FUSEKI_ADMIN_USER, self._settings.FUSEKI_ADMIN_PASSWORD),
        )
        if not resp.ok:
            # Include Fuseki's error body — it contains the actual SPARQL parse error.
            raise RuntimeError(
                f"Fuseki {resp.status_code}: {resp.text.strip()[:400]}"
            )
        data = resp.json()
        vars_ = data["head"]["vars"]
        return [
            {v: b[v]["value"] if v in b else None for v in vars_}
            for b in data["results"]["bindings"]
        ]

    def _enforce_limit(self, sparql: str, limit: Optional[int] = None) -> str:
        """Ensure LIMIT is present and does not exceed the effective cap.

        *limit* overrides QUERY_MAX_ROWS for this call.  When None,
        QUERY_MAX_ROWS is used as the default.
        """
        max_rows = self._settings.QUERY_MAX_ROWS
        effective = limit if limit is not None else max_rows
        match = _LIMIT_RE.search(sparql)
        if match:
            existing = int(match.group(1))
            if existing != effective:
                sparql = _LIMIT_RE.sub(f"LIMIT {effective}", sparql)
        else:
            sparql = sparql.rstrip().rstrip(";") + f" LIMIT {effective}"
        return sparql
