#!/usr/bin/env python3
# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Semantic Query Interface REPL.

Usage:
    python query.py                        # uses .env
    python query.py --env .env.local     # uses Synthea environment
    python query.py --env .env.local --gold
    python query.py --env .env.local --sparql
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import textwrap
import urllib.parse

from sqlalchemy import create_engine

from semantic_mapper.config.settings import Settings
from semantic_mapper.gold.builder import GoldDefinitionBuilder
from semantic_mapper.ontology.loader import OntologyLoader
from semantic_mapper.query.engine import SemanticQueryEngine
from semantic_mapper.schema.mapping_output import SemanticMappingReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_report(path: str) -> SemanticMappingReport:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return SemanticMappingReport.model_validate(data)
    except FileNotFoundError:
        print(f"[error] Mapping report not found: {path}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"[error] Failed to load mapping report: {exc}", file=sys.stderr)
        sys.exit(1)


def _db_display(url: str) -> str:
    """Return a safe display string (strip password)."""
    try:
        parsed = urllib.parse.urlparse(url)
        return f"{parsed.hostname}:{parsed.port}{parsed.path}"
    except Exception:
        return url


def _term_width() -> int:
    return min(shutil.get_terminal_size(fallback=(80, 24)).columns, 120)


def _print_banner(settings: Settings, gold_mode: bool = False, sparql_mode: bool = False, dry_run: bool = False) -> None:
    w = _term_width()
    print("═" * w)
    title = "Semantic Query Interface  (type 'exit' to quit)"
    print(f"  {title}")
    print("═" * w)
    if sparql_mode:
        if dry_run:
            print("  Mode      : DRY RUN (no Fuseki execution)")
        else:
            print(f"  Fuseki    : {settings.FUSEKI_URL}/{settings.RDF_DATASET_NAME}")
        print(f"  Graph     : {settings.RDF_NAMED_GRAPH}")
    elif gold_mode:
        print(f"  Gold DB   : {_db_display(settings.GOLD_DATABASE_URL)}")
    else:
        print(f"  Silver DB : {_db_display(settings.SILVER_DATABASE_URL)}")
    print(f"  Report    : {settings.QUERY_MAPPING_REPORT_PATH}")
    print(f"  Row limit : {settings.QUERY_MAX_ROWS}")
    provider = (settings.QUERY_PROVIDER or "openai").lower()
    if provider == "anthropic":
        print(f"  Provider  : anthropic ({settings.QUERY_ANTHROPIC_MODEL})")
    elif provider == "claude_code":
        print(f"  Provider  : claude_code ({settings.QUERY_ANTHROPIC_MODEL}) — Max plan")
    else:
        print(f"  Provider  : openai ({settings.QUERY_MODEL})")
    print()


def _print_help() -> None:
    print(
        textwrap.dedent("""\
        Commands:
          .help    — show this help
          .reset   — clear conversation history (start fresh context)
          .tokens  — show accumulated API usage
          .schema  — print the schema context sent to the LLM
          exit     — quit (also: quit, Ctrl-D)
        """)
    )


def _uri_to_label(value: object) -> str:
    """Shorten full ontology URIs to their local fragment for display."""
    s = str(value)
    if s.startswith("http") and ("#" in s or s.count("/") > 4):
        fragment = s.split("#")[-1] if "#" in s else s.split("/")[-1]
        return fragment if fragment else s
    return s


def _print_table(rows: list) -> None:
    """Print *rows* as a column-aligned table with a header separator."""
    headers = list(rows[0].keys())
    str_rows = [
        [_uri_to_label(v) if v is not None else "" for v in row.values()]
        for row in rows
    ]
    widths = [len(h) for h in headers]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(cells: list) -> str:
        return "  " + "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells))

    print(fmt(headers).rstrip())
    print("  " + "  ".join("─" * w for w in widths))
    for row in str_rows:
        print(fmt(row).rstrip())


def _print_result(result, query_label: str = "SQL") -> None:  # type: ignore[no-untyped-def]
    w = _term_width()
    print("─" * w)
    if result.error:
        if result.generated_sql:
            sql_prefix = f"  {query_label}  "
            sql_cont   = " " * len(sql_prefix)
            sql_lines  = textwrap.wrap(
                result.generated_sql.strip().rstrip(";"),
                width=w - len(sql_prefix),
            )
            print()
            print(f"{sql_prefix}{sql_lines[0]}")
            for line in sql_lines[1:]:
                print(f"{sql_cont}{line}")
        print()
        for line in textwrap.wrap(result.error, width=w - 4):
            print(f"  {line}")
        print()
    else:
        # Query block — continuation lines align under the first token
        sql_prefix = f"  {query_label}  "
        sql_cont   = " " * len(sql_prefix)
        sql_lines = textwrap.wrap(
            result.generated_sql.strip().rstrip(";"),
            width=w - len(sql_prefix),
        )
        print()
        print(f"{sql_prefix}{sql_lines[0]}")
        for line in sql_lines[1:]:
            print(f"{sql_cont}{line}")

        # Explanation
        why_prefix = "  WHY  "
        why_cont   = " " * len(why_prefix)
        why_lines = textwrap.wrap(result.explanation, width=w - len(why_prefix))
        if why_lines:
            print(f"{why_prefix}{why_lines[0]}")
            for line in why_lines[1:]:
                print(f"{why_cont}{line}")

        # Result table
        if result.rows:
            print()
            _print_table(result.rows)

        # Footer
        row_word   = "row" if result.row_count == 1 else "rows"
        tables_str = ", ".join(result.tables_used) if result.tables_used else "—"
        print()
        print(f"  {result.row_count} {row_word}  ·  {tables_str}")

    print("─" * w)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Semantic Query Interface REPL")
    parser.add_argument(
        "--env",
        default=None,
        metavar="FILE",
        help="Path to .env file (e.g. .env.local). Overrides the default .env.",
    )
    parser.add_argument(
        "--gold",
        action="store_true",
        help="Query the gold database instead of silver.",
    )
    parser.add_argument(
        "--sparql",
        action="store_true",
        help="Query the Fuseki triplestore via SPARQL instead of SQL.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate SPARQL/SQL but skip execution — useful for inspecting query quality.",
    )
    args = parser.parse_args()

    settings = Settings(_env_file=args.env) if args.env else Settings()
    gold_mode = args.gold
    sparql_mode = args.sparql
    dry_run = args.dry_run

    report = _load_report(settings.QUERY_MAPPING_REPORT_PATH)

    ontology_graph = None
    if settings.ONTOLOGY_PATH:
        try:
            ontology_graph = OntologyLoader.load(settings.ONTOLOGY_PATH)
        except Exception as exc:
            print(f"[warn] Could not load ontology for FK resolution: {exc}", file=sys.stderr)

    if sparql_mode:
        from semantic_mapper.rdf.query.engine import SparqlQueryEngine
        gold_definitions = GoldDefinitionBuilder().build(report, ontology_graph=ontology_graph)
        engine = SparqlQueryEngine(definitions=gold_definitions, settings=settings)
    elif gold_mode:
        gold_definitions = GoldDefinitionBuilder().build(report, ontology_graph=ontology_graph)
        gold_engine = create_engine(settings.GOLD_DATABASE_URL)
        engine = SemanticQueryEngine(
            report=report,
            silver_engine=create_engine(settings.SILVER_DATABASE_URL),
            settings=settings,
            gold_engine=gold_engine,
            gold_definitions=gold_definitions,
        )
    else:
        silver_engine = create_engine(settings.SILVER_DATABASE_URL)
        engine = SemanticQueryEngine(
            report=report,
            silver_engine=silver_engine,
            settings=settings,
        )

    _print_banner(settings, gold_mode=gold_mode, sparql_mode=sparql_mode, dry_run=dry_run)

    while True:
        try:
            prompt = f"[{engine.turn_count}] >> " if engine.turn_count else ">> "
            user_input = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not user_input:
            continue

        if user_input.lower() in {"exit", "quit"}:
            print("Bye.")
            break

        if user_input == ".help":
            _print_help()
            continue

        if user_input == ".reset":
            engine.reset_conversation()
            print("  Conversation history cleared.")
            continue

        if user_input == ".tokens":
            print(
                f"  api_calls        : {engine.api_calls}\n"
                f"  prompt_tokens    : {engine.prompt_tokens}\n"
                f"  completion_tokens: {engine.completion_tokens}"
            )
            continue

        if user_input == ".schema":
            print(engine._schema_context)
            continue

        result = engine.query(user_input, dry_run=dry_run) if sparql_mode else engine.query(user_input)
        label = "SPARQL" if sparql_mode else "SQL"
        if sparql_mode and dry_run and not result.error:
            print(f"  (dry run — SPARQL generated, Fuseki not called)")
        _print_result(result, query_label=label)


if __name__ == "__main__":
    main()
