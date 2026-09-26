#!/usr/bin/env python3
# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Load Synthea CSV output into the bronze_synthea MySQL database.

Usage:
    python scripts/load_synthea.py                      # bundled sample
    python scripts/load_synthea.py --csv-dir PATH       # your own Synthea output

CSV source, in order of preference: --csv-dir, datasets/synthea-output/csv/
(your own Synthea run, gitignored), then the bundled datasets/synthea/sample/
(*.csv.gz). Loads the five core tables:
    raw_patients, raw_encounters, raw_conditions, raw_medications, raw_procedures

Connects via DATABASE_URL (default: root:password@localhost:3307/bronze_synthea).

Creates bronze_synthea (and silver_synthea / gold_synthea) databases if they
don't exist.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DATASETS = Path(__file__).parent.parent / "datasets"
USER_CSV_DIR = _DATASETS / "synthea-output" / "csv"
SAMPLE_CSV_DIR = _DATASETS / "synthea" / "sample"

TABLES: dict[str, str] = {
    "raw_patients":    "patients.csv",
    "raw_encounters":  "encounters.csv",
    "raw_conditions":  "conditions.csv",
    "raw_medications": "medications.csv",
    "raw_procedures":  "procedures.csv",
}

BATCH_SIZE = 500


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_col(name: str) -> str:
    """Lowercase and replace non-identifier characters with underscores."""
    return name.lower().replace(" ", "_").replace("-", "_").replace("/", "_")


def _create_databases(db_url: str) -> None:
    """Create bronze_synthea, silver_synthea, and gold_synthea if absent."""
    # Connect to the server root (use 'mysql' system DB)
    base = db_url.rsplit("/", 1)[0] + "/mysql"
    engine = create_engine(base)
    with engine.connect() as conn:
        for db in ("bronze_synthea", "silver_synthea", "gold_synthea"):
            conn.execute(
                text(f"CREATE DATABASE IF NOT EXISTS `{db}` CHARACTER SET utf8mb4")
            )
            conn.commit()
    engine.dispose()
    print("  Databases ready: bronze_synthea, silver_synthea, gold_synthea")


def _load_table(engine, table_name: str, csv_path: Path) -> int:
    """Create *table_name* and bulk-insert rows from *csv_path*.

    Returns the number of rows written.
    """
    opener = gzip.open if csv_path.suffix == ".gz" else open
    with opener(csv_path, "rt", newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        headers = list(reader.fieldnames or [])
        safe_headers = [_safe_col(h) for h in headers]

        # Build INSERT template once
        cols_sql = ", ".join(f"`{h}`" for h in safe_headers)
        params_sql = ", ".join(f":{h}" for h in safe_headers)
        insert_sql = text(
            f"INSERT INTO `{table_name}` ({cols_sql}) VALUES ({params_sql})"
        )

        # (Re-)create table — TEXT for all columns; mapping engine infers types
        col_defs = ", ".join(f"`{h}` TEXT" for h in safe_headers)
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS `{table_name}`"))
            conn.execute(text(f"CREATE TABLE `{table_name}` ({col_defs})"))

        total = 0
        batch: list[dict] = []

        def _flush(conn) -> None:
            nonlocal total
            conn.execute(insert_sql, batch)
            total += len(batch)
            batch.clear()

        with engine.begin() as conn:
            for row in reader:
                safe_row = {
                    _safe_col(k): (v if v != "" else None)
                    for k, v in row.items()
                }
                batch.append(safe_row)
                if len(batch) >= BATCH_SIZE:
                    _flush(conn)
                    print(f"    {total:>7,} rows...", end="\r", flush=True)
            if batch:
                _flush(conn)

    return total


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _find_csv(csv_dir: Path, csv_file: str) -> Path | None:
    for candidate in (csv_dir / csv_file, csv_dir / f"{csv_file}.gz"):
        if candidate.exists():
            return candidate
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--csv-dir", type=Path, default=None,
                        help="Directory of Synthea CSVs (.csv or .csv.gz)")
    args = parser.parse_args()

    if args.csv_dir:
        csv_dir = args.csv_dir
    elif (USER_CSV_DIR / "patients.csv").exists():
        csv_dir = USER_CSV_DIR
    else:
        csv_dir = SAMPLE_CSV_DIR

    db_url = (
        os.environ.get("DATABASE_URL")
        or "mysql+pymysql://root:password@localhost:3307/bronze_synthea"
    )

    print("Synthea → bronze_synthea loader")
    print("=" * 40)
    print(f"  Source: {csv_dir}")

    _create_databases(db_url)
    print()

    engine = create_engine(db_url)

    for table_name, csv_file in TABLES.items():
        csv_path = _find_csv(csv_dir, csv_file)
        if csv_path is None:
            print(f"  SKIP  {csv_file} — not found in {csv_dir}")
            continue

        print(f"  Loading {csv_path.name}  →  {table_name} ...")
        rows = _load_table(engine, table_name, csv_path)
        print(f"  OK    {table_name}: {rows:,} rows{' ' * 20}")

    engine.dispose()

    print()
    print("=" * 40)
    print("Done. Run the pipeline with:")
    print("  python main.py --project synthea")


if __name__ == "__main__":
    main()
