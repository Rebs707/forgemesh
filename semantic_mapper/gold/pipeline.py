# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy import Engine, text

from semantic_mapper.config.settings import Settings
from semantic_mapper.gold.models import GoldColumnSpec, GoldPipelineStats, GoldTableDefinition
from semantic_mapper.gold.writer.base import GoldWriter

logger = logging.getLogger(__name__)


class GoldPipeline:
    def __init__(
        self,
        definitions: List[GoldTableDefinition],
        silver_engine: Engine,
        writer: GoldWriter,
        settings: Settings,
    ) -> None:
        self._definitions = definitions
        self._silver_engine = silver_engine
        self._writer = writer
        self._settings = settings

    def run(self, run_id: Optional[str] = None) -> GoldPipelineStats:
        run_id = run_id or str(uuid.uuid4())
        logger.info("GoldPipeline starting", extra={"run_id": run_id})

        # 1. Initialise writer (DDL + TRUNCATE) — serial, single connection
        self._writer.initialise(self._definitions)
        self._writer._run_id = run_id  # type: ignore[attr-defined]

        # 2. Process definitions in parallel — each writes to a distinct gold table
        rows_per_table: Dict[str, int] = {}
        lock = threading.Lock()

        is_sqlite = self._silver_engine.dialect.name == "sqlite"
        if is_sqlite:
            for definition in self._definitions:
                rows_per_table[definition.gold_table_name] = self._process_definition(
                    definition, run_id
                )
        else:
            with ThreadPoolExecutor(max_workers=self._settings.PIPELINE_WORKERS) as pool:
                futures = {
                    pool.submit(self._process_definition, definition, run_id): definition.gold_table_name
                    for definition in self._definitions
                }
                for future in as_completed(futures):
                    gold_table = futures[future]
                    try:
                        rows_written = future.result()
                        with lock:
                            rows_per_table[gold_table] = rows_written
                    except Exception as exc:
                        logger.error(
                            "Gold table processing failed",
                            extra={"table": gold_table, "error": str(exc)},
                        )
                        raise

        # 3. Finalise
        stats = self._writer.finalise(run_id, rows_per_table)
        logger.info("GoldPipeline finished", extra={"run_id": run_id})
        return stats

    # ------------------------------------------------------------------
    # Per-definition worker (runs in thread pool)
    # ------------------------------------------------------------------

    def _process_definition(self, definition: GoldTableDefinition, run_id: str) -> int:
        gold_table = definition.gold_table_name
        rows_written = 0

        for silver_table in definition.source_silver_tables:
            actual_cols = self._get_silver_columns(silver_table)
            cols_for_table = [
                spec for spec in definition.column_specs
                if spec.gold_column_name in actual_cols
            ]

            offset = 0
            batch_size = self._settings.GOLD_BATCH_SIZE

            while True:
                rows = self._read_silver_batch(
                    silver_table, cols_for_table, batch_size, offset, actual_cols
                )
                if not rows:
                    break

                batch = []
                for r in rows:
                    row = dict(r)
                    silver_id = row.pop("_silver_id", None)
                    row["_silver_refs"] = json.dumps(
                        {silver_table: [silver_id]} if silver_id is not None else {}
                    )
                    row["_mapping_run_id"] = run_id
                    row["_gold_run_id"] = run_id
                    batch.append(row)
                result = self._writer.write_batch(gold_table, batch)
                rows_written += result.rows_written
                offset += batch_size

        logger.info(
            "Gold table written",
            extra={"table": gold_table, "rows": rows_written, "run_id": run_id},
        )
        return rows_written

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_silver_columns(self, silver_table: str) -> set:
        """Return the set of column names present in *silver_table*."""
        is_sqlite = self._silver_engine.dialect.name == "sqlite"
        with self._silver_engine.connect() as conn:
            if is_sqlite:
                rows = conn.execute(
                    text(f"PRAGMA table_info(\"{silver_table}\")")
                ).fetchall()
                return {r[1] for r in rows}
            else:
                result = conn.execute(
                    text(f"SELECT * FROM `{silver_table}` LIMIT 0")
                )
                return set(result.keys())

    # Silver provenance columns fetched from silver but stripped before writing to gold.
    _PROVENANCE_COLS = (
        "_transformation_timestamp",
        "_transformation_strategy",
        "_expansion_index",
        "_discriminator_value",
    )

    def _read_silver_batch(
        self,
        silver_table: str,
        cols: List[GoldColumnSpec],
        batch_size: int,
        offset: int,
        available_cols: Optional[set] = None,
    ) -> list:
        is_sqlite = self._silver_engine.dialect.name == "sqlite"

        if is_sqlite:
            tbl = f'"{silver_table}"'
            data_col_parts = [f'"{spec.gold_column_name}"' for spec in cols]
        else:
            tbl = f"`{silver_table}`"
            data_col_parts = [f"`{spec.gold_column_name}`" for spec in cols]

        audit_parts = [
            "_silver_id",
            "_ontology_class_uri",
            "_bronze_source_ref",
            "_run_id AS _silver_run_id",
            "_mapping_confidence_score",
        ]

        # Only add provenance columns that actually exist in the silver table
        # so that older silver schemas (e.g. in tests) do not break.
        if available_cols:
            for col in self._PROVENANCE_COLS:
                if col in available_cols:
                    audit_parts.append(col)

        all_parts = audit_parts + data_col_parts
        select_cols = ", ".join(all_parts)

        sql = text(
            f"SELECT {select_cols} FROM {tbl} "
            f"LIMIT {batch_size} OFFSET {offset}"
        )

        with self._silver_engine.connect() as conn:
            return conn.execute(sql).mappings().fetchall()
