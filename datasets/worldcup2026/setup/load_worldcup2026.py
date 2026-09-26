# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
2026 FIFA World Cup — bronze loader.

Loads the CSVs assembled by build_csvs.py (from revision-pinned Wikipedia snapshots)
into MySQL as raw_-prefixed bronze tables. Run after creating the worldcup2026_bronze
database and tables via datasets/worldcup2026/setup/schema.sql.

Usage:
    python datasets/worldcup2026/setup/load_worldcup2026.py \
        --data-dir datasets/worldcup2026/data --truncate

Mirrors datasets/olympics2026/setup/load_olympics2026.py:
  - Lowercases all column names; empty strings -> NULL
  - Inserts only columns present in BOTH the CSV and the target table
  - Skips tables whose CSV is not found (warns only); reports rows per table
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DB_URL = "mysql+pymysql://root:password@localhost:3307/worldcup2026_bronze"

# CSV filename stem -> target table name.
_TABLE_MAP: dict[str, str] = {
    "teams":   "raw_teams",
    "venues":  "raw_venues",
    "players": "raw_players",
    "matches": "raw_matches",
    "goals":   "raw_goals",
    "cards":   "raw_cards",
    "appearances": "raw_appearances",
}


def _table_columns(table: str, engine) -> set[str]:
    from sqlalchemy import text
    with engine.connect() as conn:
        rows = conn.execute(text(f"DESCRIBE `{table}`")).fetchall()
    return {row[0].lower() for row in rows}


def _load_table(csv_path: Path, table: str, engine, chunksize: int = 10_000) -> int:
    from sqlalchemy import text

    db_cols = _table_columns(table, engine)
    total = 0
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
            if batch:
                _flush(conn)
    return total


def _truncate_table(table: str, engine) -> None:
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE `{table}`"))


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-8s  %(message)s",
                        datefmt="%H:%M:%S")
    parser = argparse.ArgumentParser(description="Load 2026 World Cup CSVs into MySQL")
    parser.add_argument("--data-dir",
                        default=str(Path(__file__).resolve().parent.parent / "data"))
    parser.add_argument("--database-url", default=_DEFAULT_DB_URL)
    parser.add_argument("--truncate", action="store_true",
                        help="Truncate each target table before loading")
    parser.add_argument("--tables", default=None,
                        help="Comma-separated CSV stems to load (default: all)")
    args = parser.parse_args()

    from sqlalchemy import create_engine

    engine = create_engine(args.database_url)
    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        logger.error("Data directory does not exist: %s", data_dir)
        sys.exit(1)

    stems = ({s.strip().lower() for s in args.tables.split(",")}
             if args.tables else set(_TABLE_MAP))

    sep = "=" * 60
    print(f"\n{sep}\n2026 WORLD CUP LOADER\n{sep}")
    print(f"  Data dir : {data_dir}")
    print(f"  Database : {args.database_url.split('@')[-1]}")
    print(sep)

    total_rows = 0
    missing = []
    for stem, table in _TABLE_MAP.items():
        if stem not in stems:
            continue
        csv_path = data_dir / f"{stem}.csv"
        if not csv_path.exists():
            logger.warning("CSV not found, skipping: %s", stem)
            missing.append(stem)
            continue
        if args.truncate:
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
