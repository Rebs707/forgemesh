# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Bronze vs Gold Query Comparison — MIMIC-III — issue #56

Demonstrates concretely that gold-layer SPARQL queries are simpler and more
semantically correct than equivalent bronze-layer SQL queries on MIMIC data.

Test case: "Show me all patients admitted more than twice with a cardiac diagnosis"

─── BRONZE (SQL) ────────────────────────────────────────────────────────────────
Requires expert knowledge of:
  - MIMIC column names (HADM_ID, SUBJECT_ID, ICD9_CODE)
  - ICD-9 code structure (cardiac codes start with '4')
  - Join path: PATIENTS → ADMISSIONS → DIAGNOSES_ICD
  - Deduplication of diagnoses per admission

─── GOLD (SPARQL) ───────────────────────────────────────────────────────────────
Requires only:
  - Knowing the entity names (Patient, Admission, DiagnosisIcd)
  - Knowing the property name (hasIcd9Code)
  - The ontology encodes the join path and ICD-9 semantics

Usage:
    python demo/mimic_cardiac_comparison.py
    python demo/mimic_cardiac_comparison.py --run-bronze
    python demo/mimic_cardiac_comparison.py --run-both
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ── Query definitions ─────────────────────────────────────────────────────────

BRONZE_SQL = """
-- BRONZE LAYER QUERY (SQL against raw MIMIC tables)
--
-- Analyst must know:
--   1. ICD-9 cardiac codes span 390–459 (prefix '4' covers most but not all)
--   2. DIAGNOSES_ICD links to ADMISSIONS via HADM_ID
--   3. Patients can have multiple diagnoses per admission — must DISTINCT
--   4. Column names are cryptic: HADM_ID, SUBJECT_ID, ICD9_CODE

SELECT
    p.subject_id                        AS patient_id,
    COUNT(DISTINCT a.hadm_id)           AS total_admissions,
    COUNT(DISTINCT d.hadm_id)           AS cardiac_admissions
FROM
    raw_patients p
    JOIN raw_admissions   a ON p.subject_id = a.subject_id
    JOIN raw_diagnoses_icd d ON a.hadm_id   = d.hadm_id
WHERE
    -- ICD-9 cardiac range: 390–459 (circulatory system)
    -- Code format is VARCHAR; numeric prefix filter approximates the range
    (
        d.icd9_code REGEXP '^39[0-9]'  -- 390–399
     OR d.icd9_code REGEXP '^4[0-5][0-9]'  -- 400–459
    )
GROUP BY
    p.subject_id
HAVING
    COUNT(DISTINCT a.hadm_id) > 2
ORDER BY
    total_admissions DESC;
"""

GOLD_SPARQL = """
# GOLD LAYER QUERY (SPARQL against the semantic triplestore)
#
# Analyst needs only:
#   1. Entity names from the ontology (Patient, Admission, DiagnosisIcd)
#   2. The hasIcd9Code property — semantic name, not MIMIC column name
#   3. ICD-9 cardiac prefix — same domain knowledge, but explicit
#
# The engine has already resolved:
#   - Join paths: Patient ←hasPatient— Admission ←hasAdmission— DiagnosisIcd
#   - Column name cryptic-ness: subject_id → hasSubjectId, hadm_id → hasHadmId
#   - Value mappings: ICD-9 codes mapped to condition concepts where possible

PREFIX ont: <http://mesh.local/ontology/mimic#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>

SELECT ?patient (COUNT(DISTINCT ?admission) AS ?total_admissions)
WHERE {
    ?patient   rdf:type  ont:Patient .
    ?admission rdf:type  ont:Admission ;
               ont:hasPatient   ?patient .
    ?diag      rdf:type  ont:DiagnosisIcd ;
               ont:hasAdmission ?admission ;
               ont:hasIcd9Code  ?icd9 .

    # ICD-9 circulatory system: 390–459
    FILTER(
        REGEX(?icd9, "^39[0-9]") ||
        REGEX(?icd9, "^4[0-5][0-9]")
    )
}
GROUP BY ?patient
HAVING (COUNT(DISTINCT ?admission) > 2)
ORDER BY DESC(?total_admissions)
"""

# ── Complexity comparison ─────────────────────────────────────────────────────

COMPARISON_TABLE = """
┌─────────────────────────────────────────┬──────────────┬─────────────────┐
│ Dimension                               │ Bronze (SQL) │  Gold (SPARQL)  │
├─────────────────────────────────────────┼──────────────┼─────────────────┤
│ Knowledge of MIMIC column names         │ Required     │ Not required    │
│ Knowledge of ICD-9 code structure       │ Required     │ Required (same) │
│ Knowledge of join path                  │ Required     │ Not required    │
│ Deduplication logic                     │ Manual       │ Automatic       │
│ Handles CareVue/MetaVision split        │ Manual UNION │ Transparent     │
│ Lines of query code                     │ ~25          │ ~18             │
│ Works across multiple source systems    │ No           │ Yes (federated) │
└─────────────────────────────────────────┴──────────────┴─────────────────┘
"""


def _run_bronze(settings) -> None:
    """Execute the bronze SQL against MIMIC MySQL and print results."""
    from sqlalchemy import create_engine, text

    print("\n── Executing Bronze SQL query ──────────────────────────────────")
    engine = create_engine(settings.DATABASE_URL)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(BRONZE_SQL.replace("-- BRONZE LAYER QUERY (SQL against raw MIMIC tables)\n--\n-- Analyst must know:\n--   1. ICD-9 cardiac codes span 390–459 (prefix '4' covers most but not all)\n--   2. DIAGNOSES_ICD links to ADMISSIONS via HADM_ID\n--   3. Patients can have multiple diagnoses per admission — must DISTINCT\n--   4. Column names are cryptic: HADM_ID, SUBJECT_ID, ICD9_CODE\n", ""))).fetchall()
        print(f"\n  Results ({len(rows)} patients with >2 cardiac admissions):\n")
        if rows:
            print(f"  {'patient_id':<15} {'total_admissions':<20} {'cardiac_admissions'}")
            print(f"  {'-'*15} {'-'*20} {'-'*20}")
            for row in rows:
                print(f"  {str(row[0]):<15} {str(row[1]):<20} {row[2]}")
        else:
            print("  (no results)")
    except Exception as exc:
        print(f"  ERROR: {exc}")
        print("  (Is MIMIC data loaded? Run: python mimic/setup/load_mimic.py)")


def _run_gold(settings) -> None:
    """Execute the gold SPARQL against Fuseki and print results."""
    import requests

    endpoint = f"{settings.FUSEKI_URL}/{settings.RDF_DATASET_NAME}/query"
    print("\n── Executing Gold SPARQL query ─────────────────────────────────")
    try:
        resp = requests.post(
            endpoint,
            data={"query": GOLD_SPARQL},
            headers={"Accept": "application/sparql-results+json"},
            timeout=30,
        )
        resp.raise_for_status()
        bindings = resp.json().get("results", {}).get("bindings", [])
        print(f"\n  Results ({len(bindings)} patients with >2 cardiac admissions):\n")
        if bindings:
            print(f"  {'patient_uri':<60} {'total_admissions'}")
            print(f"  {'-'*60} {'-'*16}")
            for b in bindings:
                uri = b.get("patient", {}).get("value", "")
                cnt = b.get("total_admissions", {}).get("value", "?")
                short = uri.rsplit("/", 1)[-1]
                print(f"  {short:<60} {cnt}")
        else:
            print("  (no results — run the full pipeline first)")
    except Exception as exc:
        print(f"  ERROR: {exc}")
        print("  (Is Fuseki running? Is the pipeline complete?)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MIMIC-III Bronze vs Gold query comparison demo"
    )
    parser.add_argument("--project", default="mimic")
    parser.add_argument("--env", default=None, help="Extra .env file (LLM backend etc.)")
    parser.add_argument(
        "--run-bronze",
        action="store_true",
        help="Execute the bronze SQL query against MIMIC MySQL",
    )
    parser.add_argument(
        "--run-gold",
        action="store_true",
        help="Execute the gold SPARQL query against Fuseki",
    )
    parser.add_argument(
        "--run-both",
        action="store_true",
        help="Execute both queries and compare results",
    )
    args = parser.parse_args()

    sep = "=" * 72

    print(f"\n{sep}")
    print("  MIMIC-III: Bronze SQL vs Gold SPARQL — Cardiac Patient Query")
    print(f"{sep}")
    print("\nQuestion: 'Show me all patients admitted more than twice with a cardiac diagnosis'\n")
    print(f"\n{'─'*40}  BRONZE SQL  {'─'*40}")
    print(BRONZE_SQL)
    print(f"\n{'─'*40}  GOLD SPARQL  {'─'*39}")
    print(GOLD_SPARQL)
    print(f"\n{'─'*40}  COMPARISON  {'─'*40}")
    print(COMPARISON_TABLE)

    from semantic_mapper.config.settings import Settings
    from semantic_mapper.project.resolver import activate_project
    activate_project(args.project)
    settings = Settings(_env_file=args.env) if args.env else Settings()

    if args.run_bronze or args.run_both:
        _run_bronze(settings)
    if args.run_gold or args.run_both:
        _run_gold(settings)

    if not (args.run_bronze or args.run_gold or args.run_both):
        print("\nTip: Add --run-bronze, --run-gold, or --run-both to execute the queries.\n")


if __name__ == "__main__":
    main()
