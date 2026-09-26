# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Persistence helpers for pipeline run records.

Centralises the path derivation and read/write logic for pipeline_runs.db
so that main.py (subprocess) and web_app.py (server) share a single
implementation.
"""
import logging
import sqlite3
from pathlib import Path
from typing import Optional

from semantic_mapper.config.settings import Settings
from semantic_mapper.schema.mapping_output import SemanticMappingReport

logger = logging.getLogger(__name__)


def get_pipeline_runs_db_path(settings: Settings) -> Path:
    """Derive the pipeline_runs.db path from settings (mirrors web_app.py logic)."""
    return Path(settings.CORRECTIONS_DB_PATH).parent / "pipeline_runs.db"


def write_mapping_report(
    run_id: str,
    report: SemanticMappingReport,
    settings: Settings,
) -> None:
    """Serialise *report* to JSON and UPDATE the mapping_report_json column for *run_id*.

    The row must already exist (web_app.py inserts it when the run finishes).
    Failures are logged as warnings and swallowed so a DB hiccup never kills
    the pipeline process.
    """
    db_path = get_pipeline_runs_db_path(settings)
    try:
        con = sqlite3.connect(str(db_path))
        con.execute(
            "UPDATE pipeline_runs SET mapping_report_json = ? WHERE run_id = ?",
            (report.model_dump_json(), run_id),
        )
        con.commit()
        con.close()
        logger.debug("Mapping report persisted to pipeline_runs (run_id=%s)", run_id)
    except Exception as exc:
        logger.warning(
            "Could not persist mapping report for run %s: %s", run_id, exc
        )


def get_mapping_report(
    run_id: str,
    settings: Settings,
) -> Optional[SemanticMappingReport]:
    """Deserialise and return the SemanticMappingReport stored for *run_id*.

    Returns None when the run_id is unknown, the column is NULL, or
    deserialisation fails.  Never raises.
    """
    db_path = get_pipeline_runs_db_path(settings)
    try:
        con = sqlite3.connect(str(db_path))
        row = con.execute(
            "SELECT mapping_report_json FROM pipeline_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        con.close()
        if row is None or row[0] is None:
            return None
        return SemanticMappingReport.model_validate_json(row[0])
    except Exception as exc:
        logger.warning(
            "Could not retrieve mapping report for run %s: %s", run_id, exc
        )
        return None
