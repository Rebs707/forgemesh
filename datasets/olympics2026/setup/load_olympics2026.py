# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Milano-Cortina 2026 Winter Olympics — bronze loader.

Loads the Kaggle piterfm CSVs into MySQL as raw_-prefixed bronze tables. Run this
after creating the olympics2026_bronze database and tables via
datasets/olympics2026/setup/schema.sql.

Usage:
    python datasets/olympics2026/setup/load_olympics2026.py \
        --data-dir datasets/olympics2026/data

Mirrors datasets/mimic/setup/load_mimic.py:
  - Lowercases all column names
  - Converts empty strings to NULL
  - Inserts only columns that exist in the target table (surrogate row_id PKs on the
    key-less fact tables auto-populate)
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

# Default bronze DB (same host/port/credentials pattern as projects/*.yaml).
_DEFAULT_DB_URL = "mysql+pymysql://root:password@localhost:3307/olympics2026_bronze"

# Maps CSV filename stem (lowercase, Kaggle convention) → target table name.
_TABLE_MAP: dict[str, str] = {
    "athletes":   "raw_athletes",
    "coaches":    "raw_coaches",
    "medallists": "raw_medallists",
    "medals":     "raw_medals",
    "schedules":  "raw_schedules",
    "teams":      "raw_teams",
    "venues":     "raw_venues",
}


def _table_columns(table: str, engine) -> set[str]:
    """Return the set of column names that actually exist in the target table."""
    from sqlalchemy import text
    with engine.connect() as conn:
        rows = conn.execute(text(f"DESCRIBE `{table}`")).fetchall()
    return {row[0].lower() for row in rows}


def _load_table(csv_path: Path, table: str, engine, chunksize: int = 10_000) -> int:
    """Load one CSV into MySQL. Returns total rows written.

    Only columns present in BOTH the CSV and the target table are inserted — extra
    CSV columns are dropped, and table-only columns (e.g. AUTO_INCREMENT row_id) are
    left for the database to populate.
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

    parser = argparse.ArgumentParser(description="Load Milano-Cortina 2026 CSVs into MySQL")
    parser.add_argument(
        "--data-dir",
        default=str(Path(__file__).resolve().parent.parent / "data"),
        help="Directory containing the Kaggle CSVs (default: datasets/olympics2026/data)",
    )
    parser.add_argument(
        "--database-url",
        default=_DEFAULT_DB_URL,
        help=f"SQLAlchemy URL for the bronze DB (default: {_DEFAULT_DB_URL})",
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

    from sqlalchemy import create_engine

    engine = create_engine(args.database_url)
    data_dir = Path(args.data_dir)

    if not data_dir.is_dir():
        logger.error("Data directory does not exist: %s", data_dir)
        sys.exit(1)

    stems_to_load = (
        {s.strip().lower() for s in args.tables.split(",")}
        if args.tables
        else set(_TABLE_MAP)
    )

    sep = "=" * 60
    print(f"\n{sep}\nMILANO-CORTINA 2026 LOADER\n{sep}")
    print(f"  Data dir : {data_dir}")
    print(f"  Database : {args.database_url.split('@')[-1]}")
    print(f"  Tables   : {len(stems_to_load)}")
    print(sep)

    total_rows = 0
    missing = []

    for stem, table in _TABLE_MAP.items():
        if stem not in stems_to_load:
            continue

        csv_path = data_dir / f"{stem}.csv"
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
