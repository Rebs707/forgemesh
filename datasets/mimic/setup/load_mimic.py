# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
MIMIC-III Demo Dataset Loader — issue #53

Loads MIMIC-III demo CSV files into MySQL as raw_ prefixed bronze tables.
Run this after downloading the demo CSVs from PhysioNet and creating the
mimic_bronze database via datasets/mimic/setup/schema.sql.

Usage:
    python datasets/mimic/setup/load_mimic.py --data-dir /path/to/mimic-iii-demo

Loads into projects/mimic.yaml's bronze_database_url (override with DATABASE_URL
or --env FILE).

The loader:
  - Lowercases all column names (SUBJECT_ID → subject_id)
  - Converts empty strings to NULL
  - Skips tables whose CSV is not found (warns only)
  - Reports rows loaded per table
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


# Maps CSV filename stem (uppercase, MIMIC convention) → target table name
_TABLE_MAP: dict[str, str] = {
    "PATIENTS":             "raw_patients",
    "ADMISSIONS":           "raw_admissions",
    "ICUSTAYS":             "raw_icustays",
    "DIAGNOSES_ICD":        "raw_diagnoses_icd",
    "D_ICD_DIAGNOSES":      "raw_d_icd_diagnoses",
    "PROCEDURES_ICD":       "raw_procedures_icd",
    "D_ICD_PROCEDURES":     "raw_d_icd_procedures",
    "LABEVENTS":            "raw_labevents",
    "D_LABITEMS":           "raw_d_labitems",
    "CHARTEVENTS":          "raw_chartevents",
    "D_ITEMS":              "raw_d_items",
    "INPUTEVENTS_CV":       "raw_inputevents_cv",
    "INPUTEVENTS_MV":       "raw_inputevents_mv",
    "OUTPUTEVENTS":         "raw_outputevents",
    "PRESCRIPTIONS":        "raw_prescriptions",
    "NOTEEVENTS":           "raw_noteevents",
    "MICROBIOLOGYEVENTS":   "raw_microbiologyevents",
    "TRANSFERS":            "raw_transfers",
    "CAREGIVERS":           "raw_caregivers",
    "SERVICES":             "raw_services",
    "DRGCODES":             "raw_drgcodes",
}


def _table_columns(table: str, engine) -> set[str]:
    """Return the set of column names that actually exist in the target table."""
    from sqlalchemy import text
    with engine.connect() as conn:
        rows = conn.execute(text(f"DESCRIBE `{table}`")).fetchall()
    return {row[0].lower() for row in rows}


def _load_table(
    csv_path: Path,
    table: str,
    engine,
    chunksize: int = 10_000,
) -> int:
    """Load one CSV into MySQL.  Returns total rows written.

    Only columns that exist in the target table are inserted — extra CSV
    columns (e.g. row_id on tables that don't define it) are silently dropped.
    """
    from sqlalchemy import text

    db_cols = _table_columns(table, engine)
    total = 0
    try:
        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            csv_cols = [c.lower() for c in (reader.fieldnames or [])]
            keep = [c for c in csv_cols if c in db_cols]

            cols_sql = ", ".join(f"`{c}`" for c in keep)
            params_sql = ", ".join(f":{c}" for c in keep)
            insert_sql = text(f"INSERT INTO `{table}` ({cols_sql}) VALUES ({params_sql})")

            batch: list[dict] = []

            def _flush(conn) -> None:
                nonlocal total
                conn.execute(insert_sql, batch)
                total += len(batch)
                batch.clear()

            with engine.begin() as conn:
                for row in reader:
                    safe = {c.lower(): (v if v != "" else None) for c, v in row.items()}
                    batch.append({c: safe[c] for c in keep})
                    if len(batch) >= chunksize:
                        _flush(conn)
                        print(f"    {total:>8,} rows…", end="\r", flush=True)
                if batch:
                    _flush(conn)

        return total
    except Exception as exc:
        logger.error("Failed to load %s → %s: %s", csv_path.name, table, exc)
        raise


def _truncate_table(table: str, engine) -> None:
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE `{table}`"))


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Load MIMIC-III demo CSVs into MySQL")
    parser.add_argument(
        "--data-dir",
        required=True,
        help="Directory containing MIMIC-III CSV files (e.g. PATIENTS.csv)",
    )
    parser.add_argument(
        "--project",
        default="mimic",
        help="Project whose bronze_database_url to load into (default: mimic)",
    )
    parser.add_argument(
        "--env",
        default=None,
        help="Take DATABASE_URL from this .env file instead of the project",
    )
    parser.add_argument(
        "--truncate",
        action="store_true",
        help="Truncate each target table before loading",
    )
    parser.add_argument(
        "--tables",
        default=None,
        help="Comma-separated list of CSV stems to load (default: all)",
    )
    args = parser.parse_args()

    # Bootstrap settings
    ROOT = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(ROOT))
    import os

    from sqlalchemy import create_engine

    if args.env:
        from semantic_mapper.config.settings import Settings
        db_url = Settings(_env_file=args.env).DATABASE_URL
    elif os.environ.get("DATABASE_URL"):
        db_url = os.environ["DATABASE_URL"]
    else:
        from semantic_mapper.project.resolver import activate_project
        db_url = activate_project(args.project).bronze_database_url

    engine = create_engine(db_url)
    data_dir = Path(args.data_dir)

    if not data_dir.is_dir():
        logger.error("Data directory does not exist: %s", data_dir)
        sys.exit(1)

    stems_to_load = (
        {s.strip().upper() for s in args.tables.split(",")}
        if args.tables
        else set(_TABLE_MAP)
    )

    sep = "=" * 60
    print(f"\n{sep}\nMIMIC-III LOADER\n{sep}")
    print(f"  Data dir : {data_dir}")
    print(f"  Database : {db_url.split('@')[-1]}")
    print(f"  Tables   : {len(stems_to_load)}")
    print(sep)

    total_rows = 0
    missing = []

    for stem, table in _TABLE_MAP.items():
        if stem not in stems_to_load:
            continue

        # MIMIC demo ships CSVs in uppercase; also try lowercase as fallback.
        csv_path = data_dir / f"{stem}.csv"
        if not csv_path.exists():
            csv_path = data_dir / f"{stem.lower()}.csv"
        if not csv_path.exists():
            logger.warning("CSV not found, skipping: %s", stem)
            missing.append(stem)
            continue

        if args.truncate:
            logger.info("Truncating %s…", table)
            try:
                _truncate_table(table, engine)
            except Exception as exc:
                logger.warning("Could not truncate %s: %s", table, exc)

        logger.info("Loading %s → %s …", stem, table)
        rows = _load_table(csv_path, table, engine)
        logger.info("  %s rows loaded", f"{rows:,}")
        total_rows += rows

    print(f"\n{sep}")
    print(f"  Total rows loaded : {total_rows:,}")
    if missing:
        print(f"  Missing CSVs      : {', '.join(missing)}")
    print(f"{sep}\n")


if __name__ == "__main__":
    main()
