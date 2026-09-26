#!/usr/bin/env python3
# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Load OpenFlights .dat files into the bronze_openflights MySQL database.

Usage:
    python scripts/load_openflights.py [--data-dir ~/Downloads/openflights]

Connects via DATABASE_URL (default: root:password@localhost:3307/bronze_openflights).

OpenFlights data: https://github.com/jpatokal/openflights/tree/master/data
Files have no header row. Null values are encoded as \\N.

Tables loaded:
    raw_airlines             airlines.dat              (6,048 rows)
    raw_airports             airports.dat              (7,698 rows)
    raw_airports_extended    airports-extended.dat    (12,668 rows)
    raw_countries            countries.dat               (261 rows)
    raw_planes               planes.dat                  (246 rows)
    raw_routes               routes.dat               (67,663 rows)
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

# ---------------------------------------------------------------------------
# Column definitions (OpenFlights has no header row)
# Docs: https://openflights.org/data
# ---------------------------------------------------------------------------

COLUMNS: dict[str, list[str]] = {
    "raw_airlines": [
        "airline_id",       # Unique OpenFlights identifier
        "name",             # Full name of the airline
        "alias",            # Alias / also known as
        "iata",             # 2-letter IATA code
        "icao",             # 3-letter ICAO code
        "callsign",         # Airline callsign
        "country",          # Country or territory
        "active",           # Y = currently active
    ],
    "raw_airports": [
        "airport_id",       # Unique OpenFlights identifier
        "name",             # Full name of the airport
        "city",             # Main city served
        "country",          # Country or territory
        "iata",             # 3-letter IATA code
        "icao",             # 4-letter ICAO code
        "latitude",         # Decimal degrees, −90 to +90
        "longitude",        # Decimal degrees, −180 to +180
        "altitude",         # In feet
        "timezone",         # Hours offset from UTC (float)
        "dst",              # Daylight savings: E/A/S/O/Z/N/U
        "tz_database",      # tz database time zone (Olson format)
        "type",             # Type of point: airport / heliport etc.
        "source",           # Source of data
    ],
    "raw_countries": [
        "name",             # Full name of the country
        "iso_code",         # 2-letter ISO 3166-1 alpha-2 code
        "dafif_code",       # DAFIF code (may be blank)
    ],
    "raw_planes": [
        "name",             # Full name of the aircraft
        "iata_code",        # Unique 3-letter IATA identifier
        "icao_code",        # Unique 3-4 letter ICAO identifier
    ],
    "raw_routes": [
        "airline",          # 2-letter IATA or 3-letter ICAO airline code
        "airline_id",       # Unique OpenFlights identifier (\\N if unknown)
        "source_airport",   # 3-letter IATA or 4-letter ICAO source airport
        "source_airport_id",# Unique OpenFlights source airport ID
        "dest_airport",     # 3-letter IATA or 4-letter ICAO destination
        "dest_airport_id",  # Unique OpenFlights destination airport ID
        "codeshare",        # Y if operated by different carrier
        "stops",            # Number of stops (0 = non-stop)
        "equipment",        # 3-letter IATA codes for plane type(s)
    ],
}

# airports-extended uses the same columns as raw_airports
COLUMNS["raw_airports_extended"] = COLUMNS["raw_airports"]

TABLES: dict[str, str] = {
    "raw_airlines":          "airlines.dat",
    "raw_airports":          "airports.dat",
    "raw_airports_extended": "airports-extended.dat",
    "raw_countries":         "countries.dat",
    "raw_planes":            "planes.dat",
    "raw_routes":            "routes.dat",
}

BATCH_SIZE = 1000
_NULL_SENTINEL = r"\N"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_databases(db_url: str) -> None:
    base = db_url.rsplit("/", 1)[0] + "/mysql"
    engine = create_engine(base)
    with engine.connect() as conn:
        for db in ("bronze_openflights", "silver_openflights", "gold_openflights"):
            conn.execute(
                text(f"CREATE DATABASE IF NOT EXISTS `{db}` CHARACTER SET utf8mb4")
            )
            conn.commit()
    engine.dispose()
    print("  Databases ready: bronze_openflights, silver_openflights, gold_openflights")


def _load_table(engine, table_name: str, dat_path: Path) -> int:
    col_names = COLUMNS[table_name]
    cols_sql = ", ".join(f"`{c}`" for c in col_names)
    params_sql = ", ".join(f":{c}" for c in col_names)
    insert_sql = text(
        f"INSERT INTO `{table_name}` ({cols_sql}) VALUES ({params_sql})"
    )

    col_defs = ", ".join(f"`{c}` TEXT" for c in col_names)
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

    with open(dat_path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        with engine.begin() as conn:
            for row in reader:
                if not row:
                    continue
                # Pad/truncate to expected column count
                row = (row + [None] * len(col_names))[:len(col_names)]
                record = {
                    col: (None if v is None or v == _NULL_SENTINEL or v == "" else v)
                    for col, v in zip(col_names, row)
                }
                batch.append(record)
                if len(batch) >= BATCH_SIZE:
                    _flush(conn)
                    print(f"    {total:>7,} rows...", end="\r", flush=True)
            if batch:
                _flush(conn)

    return total


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Load OpenFlights .dat files into MySQL")
    parser.add_argument(
        "--data-dir",
        default=str(Path.home() / "Downloads" / "openflights"),
        help="Directory containing the .dat files (default: ~/Downloads/openflights)",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir).expanduser()
    if not data_dir.exists():
        print(f"ERROR: data directory not found: {data_dir}", file=sys.stderr)
        sys.exit(1)

    db_url = os.environ.get(
        "DATABASE_URL",
        "mysql+pymysql://root:password@localhost:3307/bronze_openflights",
    )

    print("OpenFlights → bronze_openflights loader")
    print("=" * 45)
    print(f"  Data dir : {data_dir}")
    print(f"  DB URL   : {db_url}")
    print()

    _create_databases(db_url)
    print()

    engine = create_engine(db_url)

    for table_name, dat_file in TABLES.items():
        dat_path = data_dir / dat_file
        if not dat_path.exists():
            print(f"  SKIP  {dat_file} — not found at {dat_path}")
            continue
        print(f"  Loading {dat_file}  →  {table_name} ...")
        rows = _load_table(engine, table_name, dat_path)
        print(f"  OK    {table_name}: {rows:,} rows{' ' * 20}")

    engine.dispose()

    print()
    print("=" * 45)
    print("Done. Run the pipeline with:")
    print("  python main.py --project openflights")


if __name__ == "__main__":
    main()
