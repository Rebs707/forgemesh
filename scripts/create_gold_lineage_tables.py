# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Migration script: create gold lineage tables and backfill from existing data.

Usage:
    python scripts/create_gold_lineage_tables.py --env .env

What this does:
    1. Creates gold_{entity}_lineage tables for all 21 MIMIC gold tables.
    2. Backfills each lineage table by joining gold_{entity} to its silver
       source on (_bronze_source_ref, _silver_run_id = _run_id).

Note on _transformation_strategy:
    Existing silver rows written before the Bug 1 fix will have
    _transformation_strategy = 'NullHandler' for all rows, including rows
    where ValueMapper was the actual transformer.  The backfill preserves
    these values as-is.  Run a full pipeline after this migration to get
    correct strategy values in new rows.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

parser = argparse.ArgumentParser(description="Create gold lineage tables")
parser.add_argument("--env", default=os.environ.get("FORGEMESH_ENV"), metavar="FILE")
args, _ = parser.parse_known_args()

if args.env:
    from dotenv import load_dotenv
    load_dotenv(args.env, override=True)

from sqlalchemy import create_engine, text
from semantic_mapper.config.settings import Settings

settings = Settings()

# ── Connection engines ─────────────────────────────────────────────────────────
silver_engine = create_engine(settings.SILVER_DATABASE_URL)
gold_engine   = create_engine(settings.GOLD_DATABASE_URL)

# ── Table map: gold_entity → silver_table ─────────────────────────────────────
GOLD_TO_SILVER = {
    "gold_admission":           "silver_raw_admissions",
    "gold_caregiver":           "silver_raw_caregivers",
    "gold_chartevent":          "silver_raw_chartevents",
    "gold_diagnosisicd":        "silver_raw_diagnoses_icd",
    "gold_drgcode":             "silver_raw_drgcodes",
    "gold_icddiagnosis":        "silver_raw_d_icd_diagnoses",
    "gold_icdprocedure":        "silver_raw_d_icd_procedures",
    "gold_icustay":             "silver_raw_icustays",
    "gold_inputeventcv":        "silver_raw_inputevents_cv",
    "gold_inputeventmv":        "silver_raw_inputevents_mv",
    "gold_item":                "silver_raw_d_items",
    "gold_labevent":            "silver_raw_labevents",
    "gold_labitem":             "silver_raw_d_labitems",
    "gold_microbiologyevent":   "silver_raw_microbiologyevents",
    "gold_noteevent":           "silver_raw_noteevents",
    "gold_outputevent":         "silver_raw_outputevents",
    "gold_patient":             "silver_raw_patients",
    "gold_prescription":        "silver_raw_prescriptions",
    "gold_procedureicd":        "silver_raw_procedures_icd",
    "gold_service":             "silver_raw_services",
    "gold_transfer":            "silver_raw_transfers",
}

CREATE_LINEAGE_DDL = """\
CREATE TABLE IF NOT EXISTS `{lineage_tbl}` (
    _gold_id                    BIGINT       NOT NULL PRIMARY KEY,
    _silver_run_id              CHAR(36)     NOT NULL,
    _transformation_timestamp   DATETIME(6),
    _transformation_strategy    VARCHAR(128),
    _expansion_index            TINYINT,
    _discriminator_value        TEXT,
    INDEX idx_lineage_silver_run (_silver_run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

BACKFILL_SQL = """\
INSERT IGNORE INTO `{gold_db}`.`{lineage_tbl}`
    (_gold_id, _silver_run_id,
     _transformation_timestamp, _transformation_strategy,
     _expansion_index, _discriminator_value)
SELECT
    g._gold_id,
    g._silver_run_id,
    s._transformation_timestamp,
    s._transformation_strategy,
    s._expansion_index,
    s._discriminator_value
FROM `{gold_db}`.`{gold_tbl}` g
JOIN `{silver_db}`.`{silver_tbl}` s
  ON g._bronze_source_ref = s._bronze_source_ref
 AND g._silver_run_id     = s._run_id
"""


def run() -> None:
    gold_db   = gold_engine.url.database
    silver_db = silver_engine.url.database

    print(f"Gold DB:   {gold_db}")
    print(f"Silver DB: {silver_db}")
    print()

    with gold_engine.begin() as conn:
        for gold_tbl, silver_tbl in GOLD_TO_SILVER.items():
            lineage_tbl = f"{gold_tbl}_lineage"

            # 1. Create lineage table
            conn.execute(text(CREATE_LINEAGE_DDL.format(lineage_tbl=lineage_tbl)))
            print(f"[CREATE] {lineage_tbl}")

            # 2. Backfill from gold JOIN silver
            sql = BACKFILL_SQL.format(
                gold_db=gold_db,
                lineage_tbl=lineage_tbl,
                gold_tbl=gold_tbl,
                silver_db=silver_db,
                silver_tbl=silver_tbl,
            )
            result = conn.execute(text(sql))
            print(f"[FILL]   {lineage_tbl}: {result.rowcount} rows inserted")

    print()
    print("Migration complete.")
    print()
    print("NOTE: existing silver rows have _transformation_strategy = 'NullHandler'")
    print("      for all rows due to Bug 1 (fixed in this release). Re-run the full")
    print("      pipeline (map → silver → gold) to get correct strategy values.")


if __name__ == "__main__":
    run()
