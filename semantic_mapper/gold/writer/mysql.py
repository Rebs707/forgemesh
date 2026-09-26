# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone
from typing import Dict, List, Sequence

from sqlalchemy import Engine, text

from semantic_mapper.config.settings import Settings
from semantic_mapper.core.exceptions import GoldSchemaInitialisationError, GoldWriterError
from semantic_mapper.gold.models import (
    GoldPipelineStats,
    GoldTableDefinition,
    GoldWriteResult,
)
from semantic_mapper.gold.writer.base import GoldWriter

logger = logging.getLogger(__name__)

# Map source_data_type fragment → gold MySQL column type.
# Mirrors MySQLSilverWriter._TYPE_MAP exactly.
_TYPE_MAP: Dict[str, str] = {
    "VARCHAR": "TEXT",
    "CHAR": "TEXT",
    "TEXT": "TEXT",
    "INT": "BIGINT",
    "BIGINT": "BIGINT",
    "SMALLINT": "BIGINT",
    "TINYINT": "BIGINT",
    "DECIMAL": "DECIMAL(20,6)",
    "NUMERIC": "DECIMAL(20,6)",
    "FLOAT": "DOUBLE",
    "DOUBLE": "DOUBLE",
    "DATE": "TEXT",
    "DATETIME": "TEXT",
    "TIMESTAMP": "TEXT",
    "BOOLEAN": "TINYINT(1)",
    "BIT": "TINYINT(1)",
}

_TYPE_MAP_SQLITE: Dict[str, str] = {
    "VARCHAR": "TEXT",
    "CHAR": "TEXT",
    "TEXT": "TEXT",
    "INT": "INTEGER",
    "BIGINT": "INTEGER",
    "SMALLINT": "INTEGER",
    "TINYINT": "INTEGER",
    "DECIMAL": "REAL",
    "NUMERIC": "REAL",
    "FLOAT": "REAL",
    "DOUBLE": "REAL",
    "DATE": "TEXT",
    "DATETIME": "TEXT",
    "TIMESTAMP": "TEXT",
    "BOOLEAN": "INTEGER",
    "BIT": "INTEGER",
}


def _camel_to_snake(name: str) -> str:
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower().replace("-", "_").replace(" ", "_")


def _case_expr(col_name: str, value_map: dict, sqlite: bool) -> str:
    """Build a SQL CASE expression that maps raw values to URIs."""
    q = '"' if sqlite else '`'
    src = f"{q}{col_name}{q}"
    whens = "\n    ".join(
        f"WHEN '{raw.replace(chr(39), chr(39)+chr(39))}' THEN '{uri}'"
        for raw, uri in value_map.items()
    )
    return f"CASE {src}\n    {whens}\n    ELSE {src}\n  END"


def _uri_fragment(uri: str) -> str:
    return uri.split("#")[-1] if "#" in uri else uri.split("/")[-1]


def _map_sql_type(source_data_type: str, sqlite: bool = False) -> str:
    upper = source_data_type.upper()
    mapping = _TYPE_MAP_SQLITE if sqlite else _TYPE_MAP
    for key, gold_type in mapping.items():
        if upper.startswith(key):
            return gold_type
    return "TEXT"


def _ensure_database_exists(engine: Engine) -> None:
    """Create the target database if it does not exist (MySQL only; no-op for SQLite)."""
    if engine.dialect.name == "sqlite":
        return
    url = engine.url
    db_name = url.database
    if not db_name:
        return
    root_url = url.set(database="mysql")
    from sqlalchemy import create_engine as _ce, text as _text
    root_engine = _ce(root_url)
    try:
        with root_engine.connect() as conn:
            conn.execute(_text(f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4"))
            conn.commit()
        logger.info("Ensured database exists: %s", db_name)
    finally:
        root_engine.dispose()



# Silver provenance columns fetched by GoldPipeline that must NOT be written
# to the gold table (lineage is powered from the mapping report file instead).
_SILVER_PROVENANCE_COLS: frozenset = frozenset({
    "_transformation_timestamp",
    "_transformation_strategy",
    "_expansion_index",
    "_discriminator_value",
})


class MySQLGoldWriter(GoldWriter):
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self._engine = engine
        self._settings = settings
        self._started_at: datetime = datetime.now(timezone.utc)
        self._run_id: str = ""
        self._write_results: List[GoldWriteResult] = []
        self._results_lock = threading.Lock()

    @property
    def _is_sqlite(self) -> bool:
        return self._engine.dialect.name == "sqlite"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def initialise(self, definitions: List[GoldTableDefinition]) -> None:
        _ensure_database_exists(self._engine)
        gold_mode = getattr(self._settings, "GOLD_MODE", 1)
        try:
            with self._engine.begin() as conn:
                # Drop tables from previous runs that are no longer in this run.
                # Include lineage and semantic view names in expected so they are not dropped.
                expected = {d.gold_table_name for d in definitions}
                expected |= {f"gold_semantic_{d.gold_table_name[len('gold_'):]}" for d in definitions}
                self._drop_stale_tables(conn, expected, ("gold_",))

                for definition in definitions:
                    self._create_gold_table(conn, definition)
                    self._create_semantic_view(conn, definition)
        except Exception as exc:
            raise GoldSchemaInitialisationError(
                f"Failed to initialise gold schema: {exc}"
            ) from exc

    def write_batch(self, table_name: str, rows: Sequence[dict]) -> GoldWriteResult:
        if not rows:
            return GoldWriteResult(
                rows_written=0,
                rows_failed=0,
                target_table=table_name,
                run_id=self._run_id,
                written_at=datetime.now(timezone.utc),
            )

        gold_rows: List[dict] = [
            {k: v for k, v in row.items() if k not in _SILVER_PROVENANCE_COLS}
            for row in rows
        ]

        # Group gold rows by column-set so executemany works (rows from different
        # silver tables may have different column subsets)
        by_col_set: Dict[tuple, List[dict]] = {}
        for gold_row in gold_rows:
            key = tuple(sorted(gold_row.keys()))
            by_col_set.setdefault(key, []).append(gold_row)

        total_written = 0
        try:
            with self._engine.begin() as conn:
                for _col_key, row_dicts in by_col_set.items():
                    cols = list(row_dicts[0].keys())
                    if self._is_sqlite:
                        col_list = ", ".join(f'"{c}"' for c in cols)
                        bind_list = ", ".join(f":{c}" for c in cols)
                        sql = text(
                            f'INSERT OR IGNORE INTO "{table_name}" ({col_list}) '
                            f"VALUES ({bind_list})"
                        )
                    else:
                        col_list = ", ".join(f"`{c}`" for c in cols)
                        bind_list = ", ".join(f":{c}" for c in cols)
                        sql = text(
                            f"INSERT IGNORE INTO `{table_name}` ({col_list}) "
                            f"VALUES ({bind_list})"
                        )
                    conn.execute(sql, row_dicts)
                    total_written += len(row_dicts)
        except Exception as exc:
            logger.error(
                "write_batch failed",
                extra={"table": table_name, "error": str(exc)},
            )
            raise GoldWriterError(
                f"Catastrophic batch write failure for {table_name}: {exc}"
            ) from exc

        result = GoldWriteResult(
            rows_written=total_written,
            rows_failed=0,
            target_table=table_name,
            run_id=self._run_id,
            written_at=datetime.now(timezone.utc),
        )
        with self._results_lock:
            self._write_results.append(result)
        return result

    def finalise(self, run_id: str, rows_per_table: Dict[str, int]) -> GoldPipelineStats:
        finished_at = datetime.now(timezone.utc)
        duration = (finished_at - self._started_at).total_seconds()
        return GoldPipelineStats(
            run_id=run_id,
            tables_written=list(rows_per_table.keys()),
            rows_per_table=rows_per_table,
            total_rows_written=sum(rows_per_table.values()),
            started_at=self._started_at,
            finished_at=finished_at,
            duration_seconds=duration,
        )

    # ------------------------------------------------------------------
    # DDL helpers
    # ------------------------------------------------------------------

    def _drop_stale_tables(self, conn, expected: set, prefixes: tuple) -> None:
        """Drop any tables whose name starts with a watched prefix but are not in expected."""
        if self._is_sqlite:
            rows = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
            existing = {row[0] for row in rows}
        else:
            db_name = conn.engine.url.database
            rows = conn.execute(
                text(
                    "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
                    "WHERE TABLE_SCHEMA = :db"
                ),
                {"db": db_name},
            ).fetchall()
            existing = {row[0] for row in rows}

        stale = {
            tbl for tbl in existing
            if any(tbl.startswith(p) for p in prefixes) and tbl not in expected
        }
        for tbl in stale:
            logger.info("Dropping stale table", extra={"table": tbl})
            if self._is_sqlite:
                conn.execute(text(f'DROP TABLE IF EXISTS "{tbl}"'))
            else:
                conn.execute(text(f"DROP TABLE IF EXISTS `{tbl}`"))

    def _create_gold_table(self, conn, definition: GoldTableDefinition) -> None:
        tbl = definition.gold_table_name

        # Drop unconditionally so stale columns (from previous schema versions) don't persist.
        if self._is_sqlite:
            conn.execute(text(f'DROP TABLE IF EXISTS "{tbl}"'))
        else:
            conn.execute(text(f"DROP TABLE IF EXISTS `{tbl}`"))

        if self._is_sqlite:
            base_ddl = f"""
            CREATE TABLE "{tbl}" (
                _gold_id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                _ontology_class_uri       TEXT NOT NULL,
                _bronze_source_ref        TEXT NOT NULL,
                _silver_run_id            TEXT NOT NULL,
                _gold_run_id              TEXT NOT NULL,
                _mapping_confidence_score REAL NOT NULL,
                _silver_refs              TEXT,
                _mapping_run_id           TEXT
            )"""
        else:
            base_ddl = f"""
            CREATE TABLE `{tbl}` (
                _gold_id                  BIGINT        AUTO_INCREMENT PRIMARY KEY,
                _ontology_class_uri       VARCHAR(1024) NOT NULL,
                _bronze_source_ref        VARCHAR(512)  NOT NULL,
                _silver_run_id            CHAR(36)      NOT NULL,
                _gold_run_id              CHAR(36)      NOT NULL,
                _mapping_confidence_score DOUBLE        NOT NULL,
                _silver_refs              TEXT          NULL,
                _mapping_run_id           CHAR(36)      NULL,
                INDEX idx_gold_run_id (_gold_run_id),
                INDEX idx_bronze_ref  (_bronze_source_ref(255))
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

        conn.execute(text(base_ddl))

        for spec in definition.column_specs:
            sql_type = _map_sql_type(spec.source_data_type, sqlite=self._is_sqlite)
            self._add_column_if_missing(conn, tbl, spec.gold_column_name, sql_type)

    def _create_semantic_view(self, conn, definition: GoldTableDefinition) -> None:  # noqa: C901
        """Create (or replace) a gold_semantic_{entity} view that aliases raw source column
        names to their OWL property local names (camelCase URI fragment).

        When multiple source columns resolve to the same semantic alias (they share a
        property URI due to a mapping collision), the first column gets the camelCase alias
        and subsequent ones keep their original source column name.
        """
        tbl = definition.gold_table_name
        view_name = f"gold_semantic_{tbl[len('gold_'):]}"

        # Audit columns pass through unchanged
        audit_cols = [
            "_gold_id", "_ontology_class_uri", "_bronze_source_ref",
            "_silver_run_id", "_gold_run_id", "_mapping_confidence_score",
            "_silver_refs", "_mapping_run_id",
        ]

        # Build output column name for each spec — camelCase URI fragment.
        # Dedup: if alias already taken, fall back to the raw gold column name.
        seen_output_names: set = set(audit_cols)

        def _output_name(spec) -> str:
            semantic = _uri_fragment(spec.property_uri) if spec.property_uri else spec.gold_column_name
            if semantic not in seen_output_names:
                seen_output_names.add(semantic)
                return semantic
            # Collision — keep original gold column name (guaranteed unique in gold table)
            seen_output_names.add(spec.gold_column_name)
            return spec.gold_column_name

        if self._is_sqlite:
            audit_parts = [f'"{c}"' for c in audit_cols]
            data_parts = []
            for spec in definition.column_specs:
                out = _output_name(spec)
                if spec.value_map:
                    expr = _case_expr(spec.gold_column_name, spec.value_map, sqlite=True)
                    data_parts.append(f'{expr} AS "{out}"')
                else:
                    src = f'"{spec.gold_column_name}"'
                    data_parts.append(src if out == spec.gold_column_name else f'{src} AS "{out}"')
            col_expr = ", ".join(audit_parts + data_parts)
            ddl = f'CREATE VIEW IF NOT EXISTS "{view_name}" AS SELECT {col_expr} FROM "{tbl}"'
        else:
            audit_parts = [f"`{c}`" for c in audit_cols]
            data_parts = []
            for spec in definition.column_specs:
                out = _output_name(spec)
                if spec.value_map:
                    expr = _case_expr(spec.gold_column_name, spec.value_map, sqlite=False)
                    data_parts.append(f'{expr} AS `{out}`')
                else:
                    src = f"`{spec.gold_column_name}`"
                    data_parts.append(src if out == spec.gold_column_name else f'{src} AS `{out}`')
            col_expr = ", ".join(audit_parts + data_parts)
            ddl = f"CREATE OR REPLACE VIEW `{view_name}` AS SELECT {col_expr} FROM `{tbl}`"

        conn.execute(text(ddl))
        logger.info("Created semantic view", extra={"view": view_name})

    def _add_column_if_missing(
        self, conn, table_name: str, col_name: str, sql_type: str
    ) -> None:
        if self._is_sqlite:
            existing = {
                row[1]
                for row in conn.execute(
                    text(f'PRAGMA table_info("{table_name}")')
                ).fetchall()
            }
            if col_name not in existing:
                conn.execute(
                    text(f'ALTER TABLE "{table_name}" ADD COLUMN "{col_name}" {sql_type}')
                )
        else:
            db_name = conn.engine.url.database
            exists = conn.execute(
                text(
                    "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :tbl AND COLUMN_NAME = :col"
                ),
                {"db": db_name, "tbl": table_name, "col": col_name},
            ).scalar()
            if not exists:
                conn.execute(
                    text(
                        f"ALTER TABLE `{table_name}` ADD COLUMN `{col_name}` {sql_type}"
                    )
                )
