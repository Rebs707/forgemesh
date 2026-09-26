# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""ForgeMesh MCP server.

Exposes the query engine as MCP tools so Claude Desktop (or any MCP client)
can query ForgeMesh data directly via natural language or raw SQL/SPARQL.

Modes (--mode):
  sparql  (default) — NL → SPARQL → Fuseki triplestore  (requires full pipeline)
  silver            — NL → SQL   → silver MySQL tables   (requires silver phase)
  gold              — NL → SQL   → gold MySQL tables     (requires gold phase)
  bronze            — NL → SQL   → raw bronze tables     (no pipeline needed)

Usage:
    python mcp_server.py --project synthea --mode sparql

Registration in claude_desktop_config.json:
    {
      "mcpServers": {
        "forgemesh": {
          "command": "/path/to/.venv/bin/python",
          "args": ["/path/to/forgemesh/mcp_server.py",
                   "--project", "synthea"]
        }
      }
    }
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Resolve paths relative to this file so the server works regardless of cwd.
_HERE = Path(__file__).parent.resolve()
os.chdir(_HERE)

from mcp.server.fastmcp import FastMCP

# ── Argument parsing ──────────────────────────────────────────────────────────
_parser = argparse.ArgumentParser(description="ForgeMesh MCP server")
_parser.add_argument("--project",   default=None, metavar="NAME",
                     help="Project name (e.g. mimic, openflights, synthea). "
                          "Reads projects/<name>.yaml; wins over --env. "
                          "Default: DEFAULT_PROJECT (synthea) unless --env is given.")
_parser.add_argument("--env",       default=None, metavar="FILE")
_parser.add_argument("--model",     default=None, metavar="MODEL",  help="Override QUERY_MODEL")
_parser.add_argument("--max-rows",  type=int, default=None, metavar="N", help="Override QUERY_MAX_ROWS")
_parser.add_argument("--mode",      default="sparql",
                     choices=["sparql", "silver", "gold", "bronze"],
                     help="Query backend (default: sparql)")
_parser.add_argument("--transport", default="stdio", choices=["stdio", "sse", "streamable-http"],
                     help="MCP transport: stdio (default), sse, or streamable-http (Cursor)")
_parser.add_argument("--host",      default="0.0.0.0", metavar="HOST",
                     help="SSE listen host (default: 0.0.0.0, sse transport only)")
_parser.add_argument("--port",      type=int, default=8000, metavar="PORT",
                     help="SSE listen port (default: 8000, sse transport only)")
_args, _ = _parser.parse_known_args()

# ── Settings ──────────────────────────────────────────────────────────────────
from semantic_mapper.config.settings import Settings

# With neither --project nor --env, fall back to DEFAULT_PROJECT (synthea unless .env
# overrides it; empty = use .env's dataset settings as-is).
_startup_project = _args.project or (None if _args.env else (Settings().DEFAULT_PROJECT or None))
if _startup_project:
    from semantic_mapper.project.resolver import resolve_project, apply_project_to_env
    apply_project_to_env(resolve_project(_startup_project))
    settings = Settings()
elif _args.env:
    settings = Settings(_env_file=_args.env)
else:
    settings = Settings()
if _args.model:
    settings.QUERY_MODEL = _args.model
if _args.max_rows is not None:
    settings.QUERY_MAX_ROWS = _args.max_rows

from semantic_mapper.gold.builder import GoldDefinitionBuilder
from semantic_mapper.ontology.loader import OntologyLoader
from semantic_mapper.schema.mapping_output import MappingStatus, SemanticMappingReport


def _load_report() -> SemanticMappingReport:
    with open(settings.QUERY_MAPPING_REPORT_PATH) as f:
        return SemanticMappingReport.model_validate(json.load(f))


def _load_ontology_graph():
    if not settings.ONTOLOGY_PATH:
        return None
    try:
        return OntologyLoader.load(settings.ONTOLOGY_PATH)
    except Exception as exc:
        print(f"[warn] Could not load ontology: {exc}", file=sys.stderr)
        return None


# ── Bronze schema context ─────────────────────────────────────────────────────
# Built inline — the silver context builder references silver_ table names,
# so for bronze we build a lightweight context from source table/column names.

def _build_bronze_schema_context(report: SemanticMappingReport) -> str:
    lines = ["Raw bronze tables (MySQL). Use exact table and column names shown.\n"]
    for tm in report.tables:
        confirmed = [m for m in tm.column_mappings if m.status == MappingStatus.CONFIRMED]
        if not confirmed:
            continue
        lines.append(f"TABLE `{tm.source_table}`")
        for m in confirmed:
            col = m.source_column.replace(" ", "_").replace("-", "_").lower()
            lines.append(f"  `{col}`  {m.source_data_type}")
        lines.append("")
    return "\n".join(lines)


_BRONZE_SYSTEM_PROMPT = """\
You are a SQL expert. Translate natural-language questions into MySQL SELECT queries
against the raw bronze tables described in the SCHEMA below.

RULES:
1. Only generate SELECT statements. Never INSERT, UPDATE, DELETE, DROP, or DDL.
2. Always add LIMIT {max_rows} unless the user explicitly asks for all rows.
3. Use backtick-quoted identifiers.
4. Only reference tables and columns listed in the SCHEMA.
5. If the data needed does not exist in the schema, return null SQL and explain.

SCHEMA:
{schema_context}

Respond ONLY with valid JSON:
{{"sql": "SELECT ...", "explanation": "one sentence", "tables_used": ["table_name"]}}
"""

_LIMIT_RE = re.compile(r"\bLIMIT\s+(\d+)\b", re.IGNORECASE)


class _BronzeQueryEngine:
    """Minimal NL→SQL engine that queries raw bronze tables directly."""

    def __init__(self, report: SemanticMappingReport, settings: Settings) -> None:
        import sqlalchemy
        self._db = sqlalchemy.create_engine(
            settings.DATABASE_URL,
            execution_options={"isolation_level": "READ UNCOMMITTED"},
        )
        self._settings = settings
        schema = _build_bronze_schema_context(report)
        self._system_prompt = _BRONZE_SYSTEM_PROMPT.format(
            max_rows=settings.QUERY_MAX_ROWS,
            schema_context=schema,
        )
        self._schema_context = schema
        self._conversation_history: List[Dict[str, str]] = []

    def query(self, question: str):
        from semantic_mapper.query.models import QueryResult
        from semantic_mapper.core.llm import make_openai_client

        client = make_openai_client(self._settings)
        try:
            resp = client.chat.completions.create(
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
            return QueryResult(question=question, generated_sql="", explanation="",
                               rows=[], row_count=0, tables_used=[], error=str(exc))

        parsed = json.loads(resp.choices[0].message.content)
        sql: Optional[str] = parsed.get("sql")
        explanation: str = parsed.get("explanation", "")
        tables_used: List[str] = parsed.get("tables_used") or []

        if not sql:
            result = QueryResult(question=question, generated_sql="", explanation=explanation,
                                 rows=[], row_count=0, tables_used=tables_used,
                                 error=f"Cannot answer: {explanation}")
            self._record_turn(question, result)
            return result

        # Enforce row cap
        match = _LIMIT_RE.search(sql)
        if match:
            if int(match.group(1)) > self._settings.QUERY_MAX_ROWS:
                sql = _LIMIT_RE.sub(f"LIMIT {self._settings.QUERY_MAX_ROWS}", sql)
        else:
            sql = sql.rstrip().rstrip(";") + f" LIMIT {self._settings.QUERY_MAX_ROWS}"

        from sqlalchemy import text
        try:
            with self._db.connect() as conn:
                rows = [dict(r._mapping) for r in conn.execute(text(sql))]
        except Exception as exc:
            result = QueryResult(question=question, generated_sql=sql, explanation=explanation,
                                 rows=[], row_count=0, tables_used=tables_used, error=str(exc))
            self._record_turn(question, result)
            return result

        result = QueryResult(question=question, generated_sql=sql, explanation=explanation,
                             rows=rows, row_count=len(rows), tables_used=tables_used, error=None)
        self._record_turn(question, result)
        return result

    def _record_turn(self, question: str, result) -> None:
        self._conversation_history.append({"role": "user", "content": question})
        self._conversation_history.append({
            "role": "assistant",
            "content": json.dumps({"sql": result.generated_sql,
                                   "explanation": result.explanation}),
        })

    def reset_conversation(self) -> None:
        self._conversation_history.clear()


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def _bootstrap():
    report = _load_report()
    mode = _args.mode

    if mode == "sparql":
        from semantic_mapper.rdf.query.engine import SparqlQueryEngine
        # Load the specific ontology used during the map phase, not a potentially
        # mixed catalog graph — mismatched namespaces cause zero SPARQL results.
        ont_path = report.ontology_uri or settings.ONTOLOGY_PATH
        ont_graph = None
        if ont_path:
            from pathlib import Path as _Path
            p = _Path(ont_path) if _Path(ont_path).is_absolute() else _HERE / ont_path
            if p.exists():
                try:
                    from semantic_mapper.ontology.loader import OntologyLoader
                    ont_graph = OntologyLoader.load(str(p))
                except Exception as exc:
                    print(f"[warn] Could not load report ontology: {exc}", file=sys.stderr)
        definitions = GoldDefinitionBuilder().build(report, ontology_graph=ont_graph)
        engine = SparqlQueryEngine(definitions=definitions, settings=settings)
        return engine, definitions, mode, ont_graph

    if mode == "bronze":
        engine = _BronzeQueryEngine(report=report, settings=settings)
        return engine, [], mode, None

    # silver or gold — use SemanticQueryEngine
    from sqlalchemy import create_engine as _ce
    from semantic_mapper.query.engine import SemanticQueryEngine

    silver_engine = _ce(settings.SILVER_DATABASE_URL)

    if mode == "gold":
        ontology_graph = _load_ontology_graph()
        definitions = GoldDefinitionBuilder().build(report, ontology_graph=ontology_graph)
        gold_engine = _ce(settings.GOLD_DATABASE_URL)
        engine = SemanticQueryEngine(
            report=report,
            silver_engine=silver_engine,
            settings=settings,
            gold_engine=gold_engine,
            gold_definitions=definitions,
        )
        return engine, definitions, mode, ontology_graph

    # silver
    engine = SemanticQueryEngine(report=report, silver_engine=silver_engine, settings=settings)
    return engine, [], mode, None


_engine, _definitions, _mode, _bootstrap_ont_graph = _bootstrap()
print(f"[forgemesh] mode={_mode} model={settings.QUERY_MODEL} max_rows={settings.QUERY_MAX_ROWS}",
      file=sys.stderr)

# ── Persistent mapping report and ontology graph ──────────────────────────────
_report: Optional[Any] = None
try:
    _report = _load_report()
    # Startup consistency guard: emit a loud warning when the mapping report's
    # database name or ontology does not match the active graph/project.  A silent
    # mismatch (e.g. container started before the pipeline ran for this project)
    # causes get_schema to return a foreign dataset's classes while execute_sparql
    # hits the correct named graph — every query returns zero rows with no error.
    import os as _os_guard
    _project = _os_guard.environ.get("FORGEMESH_PROJECT", "")
    _expected_db = _project.replace("-", "_") if _project else None
    print(
        f"[forgemesh] report={settings.QUERY_MAPPING_REPORT_PATH} "
        f"db={_report.database_name} ont={_report.ontology_uri} "
        f"graph={settings.RDF_NAMED_GRAPH}",
        file=sys.stderr,
    )
    # Cross-check: project name should appear in either the report's database_name
    # OR its ontology_uri.  The database_name is sometimes generic ("MySQL_Bronze")
    # so we also check the ontology path which always contains the dataset slug.
    _report_fingerprint = (
        _report.database_name.lower() + " " + _report.ontology_uri.lower()
    )
    if _project and _project.lower() not in _report_fingerprint:
        print(
            f"[WARN] SCHEMA MISMATCH: mapping report (db='{_report.database_name}', "
            f"ont='{_report.ontology_uri}') does not appear to belong to "
            f"project='{_project}' — get_schema will describe the WRONG dataset. "
            f"Restart after running the pipeline for this project.",
            file=sys.stderr,
        )
except Exception:
    pass

# ── Schema self-refresh fingerprint (Bug 2 hardening) ─────────────────────────
# The report / definitions / schema are cached at startup. Track the on-disk mtimes
# of the mapping report + ontology so get_schema can rebuild automatically when the
# pipeline is re-run — no manual restart, no serving a stale dataset's schema.
from semantic_mapper.mcp.schema_freshness import (
    artifact_fingerprint as _artifact_fp_fn,
    schema_graph_disagreements as _schema_disagreements,
)


def _schema_artifact_paths() -> list:
    try:
        ont = (getattr(_report, "ontology_uri", "") or "") or (settings.ONTOLOGY_PATH or "")
    except Exception:
        ont = settings.ONTOLOGY_PATH or ""
    return [settings.QUERY_MAPPING_REPORT_PATH, ont]


_artifact_fp = _artifact_fp_fn(_schema_artifact_paths(), base_dir=str(_HERE))

_ont_graph: Optional[Any] = None
try:
    if settings.ONTOLOGY_PATH:
        from semantic_mapper.ontology.loader import OntologyLoader
        from pathlib import Path as _Path
        _ont_path = _Path(settings.ONTOLOGY_PATH)
        if not _ont_path.is_absolute():
            _ont_path = _HERE / _ont_path
        if _ont_path.exists():
            _ont_graph = OntologyLoader.load(str(_ont_path))
        # Also try the report's ontology_uri
        if _ont_graph is None and _report and getattr(_report, "ontology_uri", ""):
            _op2 = _Path(_report.ontology_uri)
            if not _op2.is_absolute():
                _op2 = _HERE / _op2
            if _op2.exists():
                _ont_graph = OntologyLoader.load(str(_op2))
except Exception as exc:
    print(f"[warn] Could not load ontology for subgraph: {exc}", file=sys.stderr)

# ── Subgraph cache (LRU, per-session) ─────────────────────────────────────────
from functools import lru_cache as _lru_cache

@_lru_cache(maxsize=getattr(settings, "SUBGRAPH_CACHE_SIZE", 50))
def _cached_subgraph(anchor_uris_frozen: frozenset, max_hops: int):
    from semantic_mapper.mcp.subgraph_extractor import extract_subgraph
    return extract_subgraph(
        list(anchor_uris_frozen), _definitions, _ont_graph, max_hops=max_hops
    )

# ── Query store startup (Feature 02) ─────────────────────────────────────────
try:
    from semantic_mapper.mcp.query_store import (
        ensure_table as _qs_ensure,
        purge_old_queries as _qs_purge,
        write_query as _qs_write,
        MCPQueryRecord as _MCPQueryRecord,
        new_query_id as _new_query_id,
        get_latest_run_id as _get_latest_run_id,
    )
    _qs_ensure(settings)
    _qs_purge(settings.MCP_QUERY_RETENTION_DAYS, settings)
except Exception as _qs_exc:
    print(f"[warn] query store init failed: {_qs_exc}", file=sys.stderr)
    _qs_write = _qs_ensure = _qs_purge = _MCPQueryRecord = _new_query_id = _get_latest_run_id = None  # type: ignore


def _persist_query(result: Dict[str, Any], question: str, executed_query: str, mode: str) -> None:
    """Write an MCPQueryRecord to the store after a successful execute call."""
    if _qs_write is None or result.get("error") or not result.get("query_id"):
        return
    try:
        from datetime import datetime, timezone
        record = _MCPQueryRecord(
            query_id=result["query_id"],
            session_id="mcp_default",
            question=question,
            executed_query=executed_query,
            mode=mode,
            answer={
                "rows": result.get("rows", []),
                "row_count": result.get("row_count", 0),
                "columns": result.get("columns", []),
            },
            run_id=_get_latest_run_id(settings) or "unknown",
            created_at=datetime.now(timezone.utc).isoformat(),
            proof_record_json=result.get("_proof_record_json"),
        )
        _qs_write(record, settings)
    except Exception as exc:
        print(f"[warn] could not persist query record: {exc}", file=sys.stderr)


# ── Answer envelope (FP-1 / FP-3) ─────────────────────────────────────────────
# Every execute_* result carries the same envelope the web app returns: the answer,
# a plain-language receipt, and a stable receipt_url. Assembled inline from the v2
# proof captured at query time + general caveat detectors — no web-app round-trip.

def _attach_envelope(result: Dict[str, Any], question: str, executed_query: str, mode: str) -> None:
    """Attach receipt + receipt_url + run_id to an execute result. Never raises."""
    if result.get("error") or not result.get("query_id"):
        return
    try:
        from semantic_mapper.envelope import build_envelope, render_receipt
        v2_proof = None
        raw = result.get("_proof_record_json")
        if raw:
            try:
                v2_proof = json.loads(raw)
            except Exception:
                v2_proof = None
        env = build_envelope(
            run_id=result["query_id"],
            question=question,
            executed_query=executed_query,
            mode=mode,
            answer={
                "rows": result.get("rows", []),
                "row_count": result.get("row_count", 0),
                "columns": result.get("columns", []),
            },
            settings=settings,
            v2_proof=v2_proof,
            ontology_version=getattr(_report, "ontology_uri", None),
        )
        result["run_id"] = env.run_id
        result["receipt"] = env.receipt.model_dump()
        result["receipt_url"] = env.receipt_url
        # Inline rendered receipt so the provenance shows in the agent surface
        # alongside the rows, without opening receipt_url. Additive — the
        # structured receipt above stays in the response.
        result["receipt_text"] = render_receipt(env)
    except Exception as exc:
        print(f"[warn] could not attach envelope: {exc}", file=sys.stderr)


def _full_envelope_dict(query_id: str) -> Dict[str, Any]:
    """Deep AnswerEnvelope for a stored run id (folds in bronze lineage). Never raises."""
    try:
        from semantic_mapper.mcp.query_store import read_query
        from semantic_mapper.envelope import build_envelope
        record = read_query(query_id, settings)
        if record is None:
            return {"error": "unknown_query_id", "query_id": query_id}
        v2_proof = None
        if getattr(record, "proof_record_json", None):
            try:
                v2_proof = json.loads(record.proof_record_json)
            except Exception:
                v2_proof = None
        prove_result = None
        try:
            from semantic_mapper.mcp.tools.prove_answer import run_prove_answer
            pr = run_prove_answer(query_id, settings)
            if isinstance(pr, dict) and (pr.get("semantic_proof") or pr.get("proof_presentation")):
                prove_result = pr
        except Exception:
            prove_result = None
        env = build_envelope(
            run_id=query_id, question=record.question, executed_query=record.executed_query,
            mode=record.mode, answer=record.answer, settings=settings,
            v2_proof=v2_proof, prove_result=prove_result,
            ontology_version=getattr(_report, "ontology_uri", None),
        )
        return env.model_dump()
    except Exception as exc:
        return {"error": "envelope_failed", "detail": str(exc), "query_id": query_id}


def _paged_rows(query_id: str, page: int = 1, page_size: int = 0) -> Dict[str, Any]:
    """Resolve the row-set handle to the exact rows behind an answer, paged."""
    try:
        from semantic_mapper.mcp.query_store import read_query
        record = read_query(query_id, settings)
        if record is None:
            return {"error": "unknown_query_id", "query_id": query_id}
        all_rows = list((record.answer or {}).get("rows") or [])
        columns = list((record.answer or {}).get("columns") or [])
        if not columns and all_rows and isinstance(all_rows[0], dict):
            columns = list(all_rows[0].keys())
        size = page_size or getattr(settings, "ENVELOPE_ROWS_PAGE_SIZE", 100)
        size = max(1, min(int(size), 1000))
        page = max(1, int(page))
        start = (page - 1) * size
        window = all_rows[start:start + size]
        total = len(all_rows)
        return {
            "query_id": query_id, "columns": columns, "rows": window,
            "page": page, "page_size": size, "total_rows": total,
            "total_pages": max(1, (total + size - 1) // size),
        }
    except Exception as exc:
        return {"error": "rows_failed", "detail": str(exc), "query_id": query_id}


# ── Execute-tool database engines ─────────────────────────────────────────────
# Created lazily on first use — guarded so the server starts even when the
# databases are offline.  SPARQL execution reuses _engine when in sparql mode,
# or falls back to a direct Fuseki POST for other modes.

def _make_silver_engine():
    import sqlalchemy
    return sqlalchemy.create_engine(settings.SILVER_DATABASE_URL)


def _make_gold_engine():
    import sqlalchemy
    return sqlalchemy.create_engine(settings.GOLD_DATABASE_URL)


class _DirectSparqlEngine:
    """Minimal Fuseki executor for execute_sparql when mode is not sparql."""

    def _execute_sparql(self, sparql: str) -> list:
        import requests
        resp = requests.post(
            f"{settings.FUSEKI_URL}/{settings.RDF_DATASET_NAME}/sparql",
            data={"query": sparql},
            headers={"Accept": "application/sparql-results+json"},
            auth=(settings.FUSEKI_ADMIN_USER, settings.FUSEKI_ADMIN_PASSWORD),
        )
        if not resp.ok:
            raise RuntimeError(f"Fuseki {resp.status_code}: {resp.text.strip()[:400]}")
        data = resp.json()
        vars_ = data["head"]["vars"]
        return [
            {v: b[v]["value"] if v in b else None for v in vars_}
            for b in data["results"]["bindings"]
        ]


def _sparql_engine_for_execute():
    """Return an engine with _execute_sparql() suitable for the execute_sparql tool."""
    if _mode == "sparql" and hasattr(_engine, "_execute_sparql"):
        return _engine
    return _DirectSparqlEngine()


def _switch_mode(new_mode: str):
    """Reinitialise the global engine for a new mode."""
    global _engine, _definitions, _mode, _bootstrap_ont_graph
    original = _args.mode
    _args.mode = new_mode
    try:
        _engine, _definitions, _mode, _bootstrap_ont_graph = _bootstrap()
        print(f"[forgemesh] switched to mode={_mode}", file=sys.stderr)
    except Exception as exc:
        _args.mode = original
        raise RuntimeError(f"Failed to switch to {new_mode} mode: {exc}") from exc

def _graph_class_uris() -> list:
    """Distinct class URIs that have instances in the live named graph (sparql mode).
    Empty on any failure — the caller treats that as 'cannot judge', not a mismatch."""
    try:
        eng = _sparql_engine_for_execute()
        rows = eng._execute_sparql(
            f"SELECT DISTINCT ?c WHERE {{ GRAPH <{settings.RDF_NAMED_GRAPH}> "
            f"{{ ?s a ?c }} }} LIMIT 1000"
        )
        return [r.get("c") for r in rows if r.get("c")]
    except Exception:
        return []


def _warn_on_schema_graph_mismatch() -> None:
    """Log a loud warning when the served schema describes a different dataset than the
    live graph — the divergence that otherwise silently returns zero rows. Never raises."""
    if _mode != "sparql" or not _definitions:
        return
    try:
        served = [getattr(d, "primary_class_uri", "") for d in _definitions]
        ns = next((u.rsplit("#", 1)[0] + "#" for u in served if u and "#" in u), None)
        for reason in _schema_disagreements(ns, served, _graph_class_uris()):
            print(f"[WARN] SCHEMA/GRAPH DIVERGENCE: {reason} — restart or re-run the "
                  f"pipeline for this project.", file=sys.stderr)
    except Exception:
        pass


def _refresh_schema_if_stale() -> None:
    """Rebuild the cached report / engine / definitions / ontology when the underlying
    artifacts changed on disk since load — so a re-materialise + re-map is served
    without a manual restart (Bug 2 hardening). Cheap (two stat() calls) on the common
    no-change path; only reloads on an actual change."""
    global _report, _engine, _definitions, _mode, _bootstrap_ont_graph, _artifact_fp
    current = _artifact_fp_fn(_schema_artifact_paths(), base_dir=str(_HERE))
    if current == _artifact_fp:
        return
    try:
        _report = _load_report()
        _engine, _definitions, _mode, _bootstrap_ont_graph = _bootstrap()
        _artifact_fp = current
        print(f"[forgemesh] schema artifacts changed — reloaded "
              f"(report={settings.QUERY_MAPPING_REPORT_PATH}, "
              f"ont={getattr(_report, 'ontology_uri', '')})", file=sys.stderr)
        _warn_on_schema_graph_mismatch()
    except Exception as exc:
        print(f"[warn] schema refresh failed; keeping cached state: {exc}", file=sys.stderr)


# One-time startup agreement check (graph-based, complements the name-based guard above).
_warn_on_schema_graph_mismatch()

# ── MCP server ────────────────────────────────────────────────────────────────
mcp = FastMCP("ForgeMesh", host=_args.host, port=_args.port, stateless_http=True)


@mcp.tool()
def get_schema() -> str:
    """Return the full ForgeMesh schema — all classes, properties, join patterns, and rules.

    Use this for broad exploration or when you are unsure which classes apply.
    For a specific question, prefer **get_schema_for_query(question)** which returns
    a smaller, more precise subgraph containing only the classes logically needed.

    The response contains:

    - **classes**: every ontology class with its URI, label, gold_table, display_fields,
      data properties (uri, label, range, source_column), and join_patterns.
      Properties with kind=categorical_uri store ontology subtype URIs not string literals
      — use FILTER(STRAFTER(STR(?var), '#') = 'LocalName'), not FILTER(?var = "string").
    - **value_matching_guidance**: categorical value → URI local name mappings.
    - **construction_rules**: SPARQL construction rules (join variable reuse, aggregate
      syntax, LIMIT, GRAPH wrapping, categorical URI filtering, etc.).
    - **namespaces**: PREFIX declarations (ont: for class/property URIs, data: for subjects).
    - **named_graph**: the Fuseki named graph to wrap all triple patterns in.

    After calling get_schema, construct SPARQL using execute_sparql or SQL using
    execute_sql_silver / execute_sql_gold.  Do NOT call query() — it is deprecated.
    """
    _refresh_schema_if_stale()
    if _definitions:
        try:
            from semantic_mapper.mcp.schema_builder import build_agent_schema
            # Use the ontology graph loaded during bootstrap (which prioritises
            # report.ontology_uri over settings.ONTOLOGY_PATH).  Re-loading from
            # settings.ONTOLOGY_PATH here would silently use a different OWL file
            # when the two paths diverge (e.g. patched vs un-patched ontology).
            schema = build_agent_schema(
                definitions=_definitions,
                mapping_report=_report,
                named_graph=settings.RDF_NAMED_GRAPH,
                base_uri=settings.RDF_BASE_URI,
                ontology_graph=_bootstrap_ont_graph,
                ontology_name=getattr(_report, "ontology_uri", "") or "",
            )
            return json.dumps(schema, indent=2)
        except Exception as exc:
            print(f"[warn] build_agent_schema failed, falling back to string: {exc}", file=sys.stderr)

    # Fallback: return the pre-built string schema context
    return getattr(_engine, "_schema_context", "Schema not available for current mode.")


@mcp.tool()
def get_schema_for_query(question: str) -> str:
    """Return a schema subgraph containing only the classes needed for this question.

    Preferred over get_schema when you have a specific question to answer.
    Uses concept extraction (label matching) + BFS traversal of the OWL graph to
    extract the provably complete relevant subgraph — classes, properties, and join
    patterns for concepts mentioned in the question and their neighbours.

    **Why use this instead of get_schema**:
    - Smaller context — only what the query needs, not the entire ontology.
    - More precise — the agent cannot hallucinate property URIs that aren't in
      the returned subgraph.
    - Bounded prompt size as the ontology grows.

    **When to use get_schema instead**:
    - Broad exploration ("what data is available?").
    - Question spans many entity types and you are unsure which classes apply.
    - Fallback if get_schema_for_query returns a suspiciously small subgraph.

    The response has the same JSON structure as get_schema, plus:
    - **anchor_concepts**: class URIs extracted from the question (transparency).
    - **subgraph_class_count** / **full_class_count**: how many classes are included vs total.

    After calling get_schema_for_query, construct SPARQL using execute_sparql.
    The construction_rules and categorical_uri guidance apply identically.
    """
    _refresh_schema_if_stale()
    if not _definitions:
        return get_schema()

    try:
        from semantic_mapper.mcp.concept_extractor import extract_anchor_concepts
        from semantic_mapper.mcp.schema_builder import build_agent_schema

        anchor_uris = extract_anchor_concepts(question, _definitions, _ont_graph)
        sub_defs = _cached_subgraph(frozenset(anchor_uris), max_hops=getattr(settings, "SUBGRAPH_MAX_HOPS", 3))

        schema = build_agent_schema(
            definitions=sub_defs,
            mapping_report=_report,
            named_graph=settings.RDF_NAMED_GRAPH,
            base_uri=settings.RDF_BASE_URI,
            ontology_graph=_ont_graph,
            ontology_name=getattr(_report, "ontology_uri", "") or "",
        )
        schema["anchor_concepts"] = anchor_uris
        schema["subgraph_class_count"] = len(sub_defs)
        schema["full_class_count"] = len(_definitions)
        print(
            f"[forgemesh] get_schema_for_query: anchors={len(anchor_uris)} "
            f"subgraph={len(sub_defs)}/{len(_definitions)} classes",
            file=sys.stderr,
        )
        return json.dumps(schema, indent=2)
    except Exception as exc:
        print(f"[warn] get_schema_for_query failed, falling back to get_schema: {exc}", file=sys.stderr)
        return get_schema()


@mcp.tool()
def execute_sparql(sparql: str, question_hint: str = "") -> str:
    """Execute a SPARQL SELECT query against the ForgeMesh knowledge graph.

    Use get_schema first to obtain class URIs, property URIs, join patterns,
    and construction rules.  This tool does no NL translation — construct the
    SPARQL yourself using the schema.

    **When to use**: for any query against the RDF triplestore (Fuseki).  Works
    regardless of the server's current mode.

    **Common patterns**::

        # Count by group
        SELECT ?paymentMethod (COUNT(DISTINCT ?orderURI) AS ?n)
        WHERE {
          GRAPH <named_graph> {
            ?orderURI <ont:hasPaymentMethod> ?paymentMethod .
          }
        }
        GROUP BY ?paymentMethod
        ORDER BY DESC(?n) LIMIT 20

        # Filter on time (use xsd:dateTime, not xsd:date, for DATETIME properties)
        FILTER(?orderedAt > "2024-01-01T00:00:00"^^xsd:dateTime)

        # Object property join (variable reuse is mandatory — see construction_rules)
        ?orderURI <ont:hasOrderCustomer> ?customerURI .
        ?customerURI <ont:hasCustomerCountry> ?country .

        # Value-matching join (shared literal variable across two entities)
        ?orderURI <ont:hasOrderCustomerRef> ?ref .
        ?customerURI <ont:hasCustomerRef> ?ref .

    **Debugging zero rows**:
    - Property URI misspelled? Copy exactly from schema.
    - Time literal xsd:dateTime vs xsd:date mismatch?
    - Join variable renamed after binding (creates cartesian product)?
    - Object property result treated as a literal (use ?entityURI, not ?entityCode)?

    **Returns**: JSON with query_id, rows, columns, row_count, duration_ms, and an
    answer **envelope**: ``run_id``, ``receipt`` (four plain-language blocks —
    understood / sources / evidence / caveats), and a stable ``receipt_url``.

    **Citing the receipt (do this without being asked)**: when you report a figure
    from this result, (1) quote the ``run_id`` alongside it, (2) name the source
    table(s) from ``receipt.sources``, (3) mention EVERY caveat in
    ``receipt.caveats`` that has a non-zero ``count`` (each has a plain ``note``),
    and (4) offer the ``receipt_url`` so the user can open the full receipt. Use
    ``get_receipt`` for the full receipt (with bronze lineage) and ``get_rows`` to
    page the evidence rows without dumping them all into context.

    On error: adds an ``error`` key.  The query_id can be used with prove_answer
    (Feature 02) when that tool is available.

    Only SELECT queries are accepted.  INSERT, UPDATE, DELETE, CONSTRUCT are rejected.
    """
    from semantic_mapper.mcp.tools.execute_sparql import run_execute_sparql
    engine = _sparql_engine_for_execute()
    result = run_execute_sparql(engine, sparql, question_hint=question_hint)
    _persist_query(result, question=question_hint or sparql[:200], executed_query=sparql, mode="sparql")
    _attach_envelope(result, question=question_hint or sparql[:200], executed_query=sparql, mode="sparql")
    # Strip internal key before returning to MCP caller
    result.pop("_proof_record_json", None)
    return json.dumps(result, indent=2)


@mcp.tool()
def execute_sql_silver(sql: str, question_hint: str = "") -> str:
    """Execute a SQL SELECT query against the ForgeMesh silver tables.

    Silver tables follow the bronze source schema with normalised column names.
    Each table is named ``silver_{source_table}`` (e.g. ``silver_raw_orders``).
    Use get_schema to see the source_column names; silver columns match those.

    **When to use**: for SQL queries against the normalised silver layer — useful
    when the RDF triplestore is not available or when you need direct SQL access.
    Prefer execute_sparql for semantic queries using the ontology.

    **Silver-specific rules**:
    - Tables are ``silver_{source_table}``.
    - To filter by entity class: ``_ontology_class_uri LIKE '%ClassName'``.
    - Use backtick-quoted identifiers.
    - When grouping by class: ``SUBSTRING_INDEX(_ontology_class_uri, '#', -1) AS class_name``.

    **Common patterns**::

        SELECT payment_method, COUNT(*) AS n
        FROM silver_raw_orders
        GROUP BY payment_method
        ORDER BY n DESC
        LIMIT 20

        SELECT s.customer_id, s.ordered_at
        FROM silver_raw_orders s
        WHERE s._ontology_class_uri LIKE '%ExpressOrder'
        LIMIT 50

    **Debugging**:
    - Column not found? Check get_schema source_column names.
    - Zero rows? Check the _ontology_class_uri LIKE pattern matches the class label.

    **Returns**: JSON with query_id, rows, columns, row_count, duration_ms, mode="silver".
    Only SELECT queries are accepted.
    """
    from semantic_mapper.mcp.tools.execute_sql import run_execute_sql
    try:
        db = _make_silver_engine()
        result = run_execute_sql(db, sql, question_hint=question_hint, mode="silver")
    except Exception as exc:
        result = {"error": str(exc), "query_id": None, "rows": [], "columns": [], "row_count": 0, "duration_ms": 0, "mode": "silver"}
    _persist_query(result, question=question_hint or sql[:200], executed_query=sql, mode="silver")
    _attach_envelope(result, question=question_hint or sql[:200], executed_query=sql, mode="silver")
    return json.dumps(result, indent=2)


@mcp.tool()
def execute_sql_gold(sql: str, question_hint: str = "") -> str:
    """Execute a SQL SELECT query against the ForgeMesh gold tables.

    Gold tables follow the ontology class structure — one table per class,
    named ``gold_{class_label}`` (e.g. ``gold_order``, ``gold_customer``).
    Use get_schema to see gold_table names and column names.

    **When to use**: for SQL queries against the gold layer — useful for
    direct SQL access when you want gold-layer column semantics without SPARQL.
    Prefer execute_sparql for semantic queries using the full ontology graph.

    **Gold-specific rules**:
    - Tables are ``gold_{class_label}``.
    - Each table contains exactly one ontology class — do NOT filter on _ontology_class_uri.
    - Join gold tables using the FK column shown in the schema (e.g. ``gold_orderitem.order_id = gold_order.order_id``).
    - Use backtick-quoted identifiers: ``\`gold_order\`.\`order_id\``
    - For ranking: ORDER BY ... DESC LIMIT N.  Only use GROUP BY for genuine
      business-dimension aggregations (e.g. GROUP BY payment_method), not for URIs.

    **Common patterns**::

        SELECT payment_method, COUNT(*) AS n
        FROM gold_order
        GROUP BY payment_method
        ORDER BY n DESC
        LIMIT 20

        SELECT o.order_id, o.ordered_at, i.product_id
        FROM gold_order o
        JOIN gold_orderitem i ON o.order_id = i.order_id
        WHERE o.is_gift = 1
        LIMIT 50

    **Debugging**:
    - Column not found? Copy exact name from get_schema.
    - Join producing too many rows? Check the FK column pair in the schema.

    **Returns**: JSON with query_id, rows, columns, row_count, duration_ms, mode="gold".
    Only SELECT queries are accepted.
    """
    from semantic_mapper.mcp.tools.execute_sql import run_execute_sql
    try:
        db = _make_gold_engine()
        result = run_execute_sql(db, sql, question_hint=question_hint, mode="gold")
    except Exception as exc:
        result = {"error": str(exc), "query_id": None, "rows": [], "columns": [], "row_count": 0, "duration_ms": 0, "mode": "gold"}
    _persist_query(result, question=question_hint or sql[:200], executed_query=sql, mode="gold")
    _attach_envelope(result, question=question_hint or sql[:200], executed_query=sql, mode="gold")
    return json.dumps(result, indent=2)


@mcp.tool()
def list_classes() -> str:
    """List ontology classes (sparql/gold/silver modes) or raw tables (bronze mode).

    Returns a JSON array describing available entities and their field counts.
    """
    if _mode == "sparql" and _definitions:
        return json.dumps([
            {
                "class_uri": d.primary_class_uri,
                "gold_table": d.gold_table_name,
                "property_count": len(d.column_specs),
                "discriminated_subtypes": d.discriminated_class_uris,
            }
            for d in _definitions
        ], indent=2)
    # bronze / silver — derive from schema context
    return _engine._schema_context


@mcp.tool()
def set_mode(mode: str) -> str:
    """Switch the query backend without restarting the server.

    mode must be one of: sparql, silver, gold, bronze.
      sparql  — NL → SPARQL → Fuseki triplestore  (requires full pipeline)
      silver  — NL → SQL   → silver MySQL tables   (requires silver phase)
      gold    — NL → SQL   → gold MySQL tables     (requires gold phase)
      bronze  — NL → SQL   → raw bronze tables     (no pipeline needed)

    Resets the conversation history. Returns confirmation of the active mode.
    """
    valid = {"sparql", "silver", "gold", "bronze"}
    if mode not in valid:
        return f"Invalid mode '{mode}'. Choose one of: {', '.join(sorted(valid))}"
    try:
        _switch_mode(mode)
        _engine.reset_conversation() if hasattr(_engine, "reset_conversation") else None
        return f"Switched to {mode} mode. Conversation history cleared."
    except RuntimeError as exc:
        return str(exc)


@mcp.tool()
def current_mode() -> str:
    """Return the currently active query backend mode and model."""
    return json.dumps({
        "mode": _mode,
        "model": settings.QUERY_MODEL,
        "max_rows": settings.QUERY_MAX_ROWS,
    }, indent=2)


@mcp.tool()
def run_sql(sql: str) -> str:
    """Execute a raw SQL SELECT against the active backend (bronze/silver/gold modes only).

    Not available in sparql mode — use run_sparql instead.
    Only SELECT statements are permitted.
    """
    if _mode == "sparql":
        return "run_sql is not available in sparql mode. Use run_sparql instead."

    if not re.match(r"^\s*SELECT\b", sql, re.IGNORECASE):
        return "Safety error: only SELECT statements are permitted."

    from sqlalchemy import text
    db = getattr(_engine, "_db", None) or getattr(_engine, "_exec_engine", None)
    if db is None:
        return "Error: no database engine available."
    try:
        with db.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(text(sql))]
        return json.dumps({"row_count": len(rows), "rows": rows}, indent=2)
    except Exception as exc:
        return f"Execution error: {exc}"


@mcp.tool()
def run_sparql(sparql: str) -> str:
    """Execute a raw SPARQL SELECT against Fuseki.

    **Mode gate (advisory):** intended for sparql mode; in other modes the Fuseki
    triplestore may not contain data for the current project, so results may be
    empty.  The gate is enforced by returning a clear informational message rather
    than raising — this keeps the MCP session alive so the agent can try
    execute_sql_silver / execute_sql_gold instead.

    Only SELECT queries are permitted — INSERT/UPDATE/DELETE/CONSTRUCT are rejected.
    Returns a JSON object with ``row_count`` and ``rows``, or an error message.
    """
    if _mode != "sparql":
        return (
            f"Note: run_sparql is intended for sparql mode (current mode: {_mode}). "
            "Fuseki may not hold data for this project in non-sparql modes. "
            "Consider execute_sql_gold or execute_sql_silver instead. "
            "Proceeding anyway — if Fuseki is populated, results will be returned."
        )

    from semantic_mapper.rdf.query.safety import validate_sparql_select_only
    from semantic_mapper.core.exceptions import QuerySafetyError
    try:
        validate_sparql_select_only(sparql)
    except QuerySafetyError as exc:
        return f"Safety error: {exc}"

    try:
        rows = _engine._execute_sparql(sparql)
        return json.dumps({"row_count": len(rows), "rows": rows}, indent=2)
    except Exception as exc:
        return f"Execution error: {exc}"


# ── Legacy NL-to-query tool (conditionally registered) ───────────────────────
# Controlled by MCP_LEGACY_QUERY_TOOL setting (default True).
# Set to False for air-gap deployments that don't want an OPENAI_API_KEY
# requirement on the MCP server.

if settings.MCP_LEGACY_QUERY_TOOL:
    @mcp.tool()
    def query(question: str) -> str:
        """DEPRECATED: This tool will be removed in a future major version.

        Use get_schema + execute_sparql instead. The new pattern allows the
        agent to construct queries itself, iterate on results, and removes the
        OpenAI dependency from the MCP server entirely.

        Migration::

            # Old pattern (deprecated)
            result = query("How many express orders last month?")

            # New pattern
            schema = get_schema()
            # Construct SPARQL from schema.classes / schema.construction_rules
            result = execute_sparql(sparql="SELECT ...", question_hint="How many...")

        To disable this tool and remove the OPENAI_API_KEY requirement, set
        MCP_LEGACY_QUERY_TOOL=False in your .env file.

        Answer a natural-language question against the ForgeMesh data backend.
        Returns result rows as JSON, or an error message with explanation.
        """
        result = _engine.query(question)
        if result.error:
            return f"Error: {result.error}\n\nExplanation: {result.explanation}"
        payload = {
            "explanation": result.explanation,
            "row_count": result.row_count,
            "rows": result.rows,
        }
        if result.query_rewrites:
            payload["query_rewrites"] = result.query_rewrites
        return json.dumps(payload, indent=2)
else:
    print("[forgemesh] MCP_LEGACY_QUERY_TOOL=False — query() tool not registered; OpenAI not required",
          file=sys.stderr)


# ── Prove-It MCP tools (Feature 02) ──────────────────────────────────────────

@mcp.tool()
def prove_answer(query_id: str) -> str:
    """Return a structured proof for a previous query.

    Use this when a user asks "prove it", "how do you know?", "verify that",
    or any similar request for justification of a number you reported.
    The query_id is always returned by execute_sparql / execute_sql_gold /
    execute_sql_silver — use the id from the most recent relevant call.

    **Returns three keys** (all may be present):

    proof_presentation — ORDERED presentation contract (proof_presentation_version):
      Render sections in this EXACT order. Do not reorder or omit sections.
      1. question_interpretation  — what the NL question meant in ontology terms
      2. query_display            — the SPARQL executed + plain English summary
      3. ontology_grounding       — classes and properties used (with URIs)
      4. data_layer_trace         — gold/silver/bronze tables that contributed
      5. transformation_provenance — mapping decisions (confirmation + score)
      6. result_confidence        — row count, quarantined rows, broken chains
      7. limitations              — epistemic limitations (always last)

    semantic_proof — raw ontology-grounded ProofRecord v2.0.0 (SPARQL queries only):
      - interpretation, framing, scope_assumptions, limitations, ontology_subgraph_hash.

    lineage_summary — bronze pipeline summary (present when web app available):
      - subject_count, broken_lineage_count, mapping_confidence, etc.
      - May be absent if the web app is offline; check lineage_note.

    **Rendering rules for proof_presentation** — respond in plain prose, not JSON:
    - Render all seven sections in order. Do not omit any section.
    - Do not add interpretation beyond what is in the data.
    - Prose framing around each section is fine; editorial reordering is not.
    - Section 1 (question_interpretation): quote the string verbatim as your opening.
    - Section 3 (ontology_grounding): list classes_used and the local names of
      properties_used. Mention abstract_parent when present.
    - Section 4 (data_layer_trace): state "gold → silver → bronze" chain, listing
      table names. If empty, say "lineage unavailable".
    - Section 6 (result_confidence): report row_count and any quarantined/broken counts.
    - Section 7 (limitations): list every item; do not paraphrase or condense.

    Example: "Verified. [question_interpretation text]. The SPARQL counted
    Order instances (cardinality: 129). Ontology: classes — Order;
    properties — paymentMethod, shippingMethod. Data path: gold_order →
    silver_order → orders. 129 rows, 0 quarantined, 0 broken chains.
    Limitations: [list verbatim]."

    **Error responses**: unknown_query_id, proof_unavailable, unsupported_pattern,
    webapp_unreachable (web app not reachable / timed out), bronze_unavailable
    (web app reached, bronze DB down) — all returned as JSON with an "error" key.
    """
    from semantic_mapper.mcp.tools.prove_answer import run_prove_answer
    result = run_prove_answer(query_id, settings)
    return json.dumps(result, indent=2)


@mcp.tool()
def get_proof_detail(query_id: str, max_subjects: int = 10) -> str:
    """Return bronze-level lineage for a query (capped to avoid overflow).

    Given a query_id, return the ProofRecord with source-row lineage for up to
    max_subjects subjects.  Use this only when you need to cite specific source
    rows or examine raw data provenance for a small sample.

    For most uses, prove_answer (which returns a structured summary) is sufficient
    and uses far less context window.

    **max_subjects** (default 10): cap on how many subjects' lineage entries are
    returned.  If the result was truncated, the response includes _truncated=true
    and _total_subjects so you can report "showing 10 of N subjects".

    **Returns**: ProofRecord JSON with subjects and lineage capped.
    **Error responses**: same as prove_answer.
    """
    from semantic_mapper.mcp.tools.get_proof_detail import run_get_proof_detail
    result = run_get_proof_detail(query_id, settings, max_subjects=max_subjects)
    return json.dumps(result, indent=2)


@mcp.tool()
def compose_agentic_proof(
    question: str,
    answer: str,
    constituent_query_ids: str,
    synthesis_step: str,
) -> str:
    """Compose a proof tree for a multi-step answer synthesised from multiple queries.

    Use this when the answer you gave was derived from multiple queries plus
    reasoning you performed yourself (e.g. "yes, express orders are up 5.7%"
    derived from two count queries plus arithmetic).

    **Parameters** (all JSON strings):

    - question: the composite question answered.
    - answer: JSON dict, e.g. {"value": "Yes, up 5.7%", "type": "comparison"}.
    - constituent_query_ids: JSON list of query_id strings from prior execute calls.
    - synthesis_step: JSON dict with keys:
        - operation: one of the recognised operations (or free-form string).
        - description: what you did, e.g. "Computed (1247-1180)/1180*100".
        - inputs_used: dict of named inputs, e.g. {"current": 1247, "prior": 1180}.
        - output: the result you computed, e.g. 5.68.

    **Recognised operations** (mathematically verified):
    subtraction, addition, ratio, percentage_change, comparison, ranking,
    top_n_selection, aggregation_over_subqueries, filter_and_count.

    Free-form operations are accepted but flagged LLM_SYNTHESIS_UNVERIFIED.

    **Returns**: ProofTreeSummary JSON with child_summaries, synthesis_step,
    and caveats.
    """
    import json as _json
    from semantic_mapper.mcp.tools.compose_agentic_proof import run_compose_agentic_proof
    try:
        answer_dict = _json.loads(answer) if isinstance(answer, str) else answer
        query_ids = _json.loads(constituent_query_ids) if isinstance(constituent_query_ids, str) else constituent_query_ids
        step_dict = _json.loads(synthesis_step) if isinstance(synthesis_step, str) else synthesis_step
    except Exception as exc:
        return _json.dumps({"error": "invalid_json_input", "detail": str(exc)})

    result = run_compose_agentic_proof(
        question=question,
        answer=answer_dict,
        constituent_query_ids=query_ids,
        synthesis_step_input=step_dict,
        settings=settings,
    )
    return json.dumps(result, indent=2)


# ── Answer-envelope MCP tools (FP-3) ──────────────────────────────────────────

@mcp.tool()
def get_receipt(query_id: str) -> str:
    """Return the full answer envelope (receipt) for a previous query.

    The envelope is the same object every execute_* call returns, deepened with
    bronze lineage: ``answer``, ``receipt`` (understood / sources / evidence /
    caveats), and ``receipt_url``.  Use this when a user asks to see the full
    provenance of a figure, or to re-attach a past receipt to the conversation.

    The ``query_id`` comes from any execute_sparql / execute_sql_* call. The same
    envelope is browsable at ``receipt_url`` and as the ``fm://runs/{id}`` resource.

    **Error responses**: unknown_query_id, envelope_failed — returned as JSON.
    """
    return json.dumps(_full_envelope_dict(query_id), indent=2, default=str)


@mcp.tool()
def get_rows(query_id: str, page: int = 1, page_size: int = 100) -> str:
    """Page through the exact rows behind an answer (resolves the row-set handle).

    An answer's ``receipt.evidence.row_set`` is a handle (``fm://runs/{id}/rows``);
    this tool resolves it to the underlying rows, one page at a time, so you can
    inspect evidence without dumping thousands of records into context.

    **Returns**: JSON with columns, rows, page, page_size, total_rows, total_pages.
    **Error responses**: unknown_query_id, rows_failed.
    """
    return json.dumps(_paged_rows(query_id, page=page, page_size=page_size), indent=2, default=str)


@mcp.resource("fm://runs/{run_id}")
def run_receipt_resource(run_id: str) -> str:
    """Expose a past receipt as an MCP resource so it can be attached to a chat."""
    return json.dumps(_full_envelope_dict(run_id), indent=2, default=str)


if __name__ == "__main__":
    if _args.transport in ("sse", "streamable-http"):
        mcp.run(transport=_args.transport)
    else:
        mcp.run(transport="stdio")
