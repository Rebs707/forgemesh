#!/usr/bin/env python3
# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""User Acceptance Test — SPARQL join queries.

Runs all 13 natural-language queries against the SparqlQueryEngine and prints
a structured pass/fail report.  A query PASSES when it returns at least one row
and no error.  Zero rows is flagged as WARN (query ran but matched nothing in
the current dataset).

Usage:
    python scripts/uat_sparql.py                    # uses .env
    python scripts/uat_sparql.py --env .env.local
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Make sure the project root is on sys.path when run from any directory.
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from semantic_mapper.config.settings import Settings  # noqa: E402
from semantic_mapper.gold.builder import GoldDefinitionBuilder  # noqa: E402
from semantic_mapper.ontology.loader import OntologyLoader  # noqa: E402
from semantic_mapper.rdf.query.engine import SparqlQueryEngine  # noqa: E402
from semantic_mapper.schema.mapping_output import SemanticMappingReport  # noqa: E402

# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

QUERIES: list[tuple[str, str]] = [
    # (group, natural-language question)
    ("Patient → Conditions (1-hop)",        "List all conditions for patients born after 1980"),
    ("Patient → Conditions (1-hop)",        "How many distinct conditions does each patient have?"),
    ("Patient → Conditions (1-hop)",        "Which patients have a condition that started in 2020?"),
    ("Patient → Encounters (1-hop)",        "Show me all encounters for patients in Massachusetts"),
    ("Patient → Encounters (1-hop)",        "Which patients have had more than 5 encounters?"),
    ("Patient → Encounters (1-hop)",        "What is the average total claim cost per patient?"),
    ("Patient → Medications (1-hop)",       "Which medications are prescribed to female patients?"),
    ("Patient → Medications (1-hop)",       "List all medications for patients with income over 50000"),
    ("Encounter → Conditions (1-hop)",      "What conditions were recorded during inpatient encounters?"),
    ("Encounter → Conditions (1-hop)",      "Show encounters that have more than one condition attached"),
    ("Patient → Encounters → Conditions (2-hop)",
                                            "For each patient, list their encounters and any conditions recorded during those encounters"),
    ("Patient → Encounters → Conditions (2-hop)",
                                            "Which patients had a condition diagnosed during an encounter that cost more than $500?"),
    ("Patient → Encounters → Medications (2-hop)",
                                            "Show medications prescribed during encounters for patients over 60"),
    ("Patient → Encounters → Procedures (2-hop)",
                                            "List all procedures performed on patients who also have diabetes as a condition"),
]

# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

@dataclass
class CaseResult:
    group: str
    question: str
    status: str          # PASS | WARN | FAIL
    row_count: int
    sparql: str
    explanation: str
    error: str | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BOLD  = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED   = "\033[31m"
CYAN  = "\033[36m"
RESET = "\033[0m"

W = 100


def _colour(status: str) -> str:
    return {
        "PASS": f"{GREEN}PASS{RESET}",
        "WARN": f"{YELLOW}WARN{RESET}",
        "FAIL": f"{RED}FAIL{RESET}",
    }.get(status, status)


def _wrap(text: str, indent: int = 4) -> str:
    prefix = " " * indent
    return textwrap.fill(text, width=W, initial_indent=prefix, subsequent_indent=prefix)


def _load_report(path: str) -> SemanticMappingReport:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return SemanticMappingReport.model_validate(data)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="UAT — SPARQL join queries")
    parser.add_argument("--env", default=None, metavar="FILE",
                        help="Path to .env file (e.g. .env.local)")
    parser.add_argument("--json", dest="json_out", metavar="FILE",
                        help="Also write results as JSON to this path")
    args = parser.parse_args()

    settings = Settings(_env_file=args.env) if args.env else Settings()
    report = _load_report(settings.QUERY_MAPPING_REPORT_PATH)

    ontology_graph = None
    if settings.ONTOLOGY_PATH:
        try:
            ontology_graph = OntologyLoader.load(settings.ONTOLOGY_PATH)
        except Exception as exc:
            print(f"[warn] Could not load ontology: {exc}", file=sys.stderr)

    gold_definitions = GoldDefinitionBuilder().build(report, ontology_graph=ontology_graph)
    engine = SparqlQueryEngine(definitions=gold_definitions, settings=settings)

    print()
    print(f"{BOLD}{'═' * W}{RESET}")
    print(f"{BOLD}  ForgeMesh — SPARQL UAT  ({len(QUERIES)} queries){RESET}")
    print(f"  Fuseki : {settings.FUSEKI_URL}/{settings.RDF_DATASET_NAME}")
    print(f"  Graph  : {settings.RDF_NAMED_GRAPH}")
    print(f"  Model  : {settings.QUERY_MODEL}")
    print(f"{'═' * W}")
    print()

    results: list[CaseResult] = []
    current_group = None

    for i, (group, question) in enumerate(QUERIES, 1):
        if group != current_group:
            current_group = group
            print(f"{BOLD}{CYAN}  {group}{RESET}")
            print(f"  {'─' * (W - 2)}")

        print(f"  [{i:02d}] {question}")
        sys.stdout.flush()

        qr = engine.query(question)
        engine.reset_conversation()  # fresh context per query

        if qr.error:
            status = "FAIL"
        elif qr.row_count == 0:
            status = "WARN"
        else:
            status = "PASS"

        results.append(CaseResult(
            group=group,
            question=question,
            status=status,
            row_count=qr.row_count,
            sparql=qr.generated_sql,
            explanation=qr.explanation,
            error=qr.error,
        ))

        label = _colour(status)
        print(f"       {label}  {qr.row_count} rows")

        if status == "FAIL":
            print(_wrap(f"ERROR: {qr.error}", indent=8))
        else:
            # Show the SPARQL on one wrapped block
            sparql_short = " ".join(qr.generated_sql.split())
            print(_wrap(f"SPARQL: {sparql_short}", indent=8))
            if qr.explanation:
                print(_wrap(f"WHY:    {qr.explanation}", indent=8))

        print()

    # ── Summary ─────────────────────────────────────────────────────────────
    passed = sum(1 for r in results if r.status == "PASS")
    warned = sum(1 for r in results if r.status == "WARN")
    failed = sum(1 for r in results if r.status == "FAIL")

    print(f"{'═' * W}")
    print(f"{BOLD}  Summary{RESET}")
    print(f"  {GREEN}PASS{RESET} {passed}   {YELLOW}WARN{RESET} {warned}   {RED}FAIL{RESET} {failed}   total {len(results)}")
    print()

    if warned:
        print(f"  {YELLOW}WARN = query ran without error but returned 0 rows.{RESET}")
        print(f"  This usually means the SPARQL pattern doesn't match how the data was loaded,")
        print(f"  or the FK property name differs from what the LLM generated.")
        print()
        for r in results:
            if r.status == "WARN":
                print(f"    [{YELLOW}WARN{RESET}] {r.question}")
        print()

    if failed:
        print(f"  {RED}FAIL = LLM or Fuseki error.{RESET}")
        for r in results:
            if r.status == "FAIL":
                print(f"    [{RED}FAIL{RESET}] {r.question}")
                print(f"            {r.error}")
        print()

    print(f"{'═' * W}")
    print()

    # ── Optional JSON output ─────────────────────────────────────────────────
    if args.json_out:
        payload = [
            {
                "group": r.group,
                "question": r.question,
                "status": r.status,
                "row_count": r.row_count,
                "sparql": r.sparql,
                "explanation": r.explanation,
                "error": r.error,
            }
            for r in results
        ]
        Path(args.json_out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"  Results written to {args.json_out}")
        print()

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
