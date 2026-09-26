# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Dict, List, Sequence, Set

from sqlalchemy import Engine, text

from semantic_mapper.config.settings import Settings
from semantic_mapper.core.exceptions import SchemaInitialisationError, WriterError
from semantic_mapper.schema.mapping_output import MappingStatus, SemanticMappingReport
from semantic_mapper.silver.models import (
    BatchWriteResult,
    QuarantineRecord,
    RunStats,
    SilverRow,
)
from semantic_mapper.silver.writer.base import SilverWriter

logger = logging.getLogger(__name__)

# Map source_data_type fragment → silver MySQL column type.
# String types use TEXT (stored off-row in InnoDB DYNAMIC format) to avoid
# the 65535-byte inline row size limit when many columns are present.
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

# SQLite-compatible equivalents (SQLite ignores precision/scale but accepts the
# type strings; this avoids MySQL-specific syntax that SQLite rejects)
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


def _map_sql_type(source_data_type: str, sqlite: bool = False) -> str:
    upper = source_data_type.upper()
    mapping = _TYPE_MAP_SQLITE if sqlite else _TYPE_MAP
    for key, silver_type in mapping.items():
        if upper.startswith(key):
            return silver_type
    return "TEXT"


def _ensure_database_exists(engine: Engine) -> None:
    """Create the target database if it does not exist (MySQL only; no-op for SQLite)."""
    if engine.dialect.name == "sqlite":
        return
    url = engine.url
    db_name = url.database
    if not db_name:
        return
    # Connect to the server root (mysql system DB) to run CREATE DATABASE
    root_url = url.set(database="mysql")
    from sqlalchemy import create_engine as _ce
    root_engine = _ce(root_url)
    try:
        with root_engine.connect() as conn:
            conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4"))
            conn.commit()
        logger.info("Ensured database exists: %s", db_name)
    finally:
        root_engine.dispose()


class MySQLSilverWriter(SilverWriter):
    def __init__(self, engine: Engine, settings: Settings) -> None:
        self._engine = engine
        self._settings = settings
        self._batch_results: List[BatchWriteResult] = []
        self._quarantine_results: List[BatchWriteResult] = []
        self._started_at: datetime = datetime.now(timezone.utc)
        self._run_id: str = ""
        self._source_table: str = ""
        self._bronze_rows_read: int = 0
        self._silver_rows_written: int = 0
        self._quarantine_rows_written: int = 0
        self._null_preserved_count: int = 0
        self._transformation_errors: int = 0
        self._initialised_tables: Set[str] = set()
        self._stats_lock = threading.Lock()

    @property
    def _is_sqlite(self) -> bool:
        return self._engine.dialect.name == "sqlite"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def initialise(self, report: SemanticMappingReport) -> None:
        _ensure_database_exists(self._engine)
        try:
            with self._engine.begin() as conn:
                # Drop tables from previous runs that are no longer in this report.
                expected = set()
                for tm in report.tables:
                    expected.add(f"silver_{tm.source_table}")
                    expected.add(f"quarantine_{tm.source_table}")
                self._drop_stale_tables(conn, expected, ("silver_", "quarantine_"))

                for table_mapping in report.tables:
                    self._create_silver_table(conn, table_mapping)
                    self._create_quarantine_table(conn, table_mapping)
                    # Overwrite semantics: each run starts from a clean slate.
                    silver_tbl = f"silver_{table_mapping.source_table}"
                    quarantine_tbl = f"quarantine_{table_mapping.source_table}"
                    if self._is_sqlite:
                        conn.execute(text(f'DELETE FROM "{silver_tbl}"'))
                        conn.execute(text(f'DELETE FROM "{quarantine_tbl}"'))
                    else:
                        conn.execute(text(f"TRUNCATE TABLE `{silver_tbl}`"))
                        conn.execute(text(f"TRUNCATE TABLE `{quarantine_tbl}`"))
        except Exception as exc:
            raise SchemaInitialisationError(
                f"Failed to initialise silver schema: {exc}"
            ) from exc

    def write_batch(self, rows: Sequence[SilverRow]) -> BatchWriteResult:
        if not rows:
            return BatchWriteResult(
                rows_written=0,
                rows_failed=0,
                target_table="",
                run_id=self._run_id,
                written_at=datetime.now(timezone.utc),
            )

        table_name = f"silver_{rows[0].source_table}"

        # Group per-property SilverRows into wide rows (one dict per bronze source row).
        # Key: (bronze_source_ref, expansion_index)
        wide: Dict[tuple, dict] = {}
        for row in rows:
            key = (row.audit.bronze_source_ref, row.expansion_index)
            if key not in wide:
                wide[key] = {
                    "_bronze_source_ref": row.audit.bronze_source_ref,
                    "_run_id": row.audit.run_id,
                    "_transformation_timestamp": row.audit.transformation_timestamp.strftime(
                        "%Y-%m-%d %H:%M:%S.%f"
                    ),
                    "_mapping_confidence_score": row.audit.mapping_confidence_score,
                    "_ontology_class_uri": row.ontology_class_uri,
                    "_expansion_index": row.expansion_index,
                    "_discriminator_value": str(row.discriminator_value)
                    if row.discriminator_value is not None
                    else None,
                }
            # Merge this property's value into the wide row
            col_name = self._safe_col_name(row.source_column)
            wide[key][col_name] = row.transform_result.transformed_value

        # Group by column-set so executemany works (all rows in a group have same keys)
        from collections import defaultdict as _dd

        by_col_set: Dict[tuple, List[dict]] = _dd(list)
        for row_dict in wide.values():
            by_col_set[tuple(sorted(row_dict.keys()))].append(row_dict)

        total_written = 0
        try:
            with self._engine.begin() as conn:
                for col_key, row_dicts in by_col_set.items():
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

            result = BatchWriteResult(
                rows_written=total_written,
                rows_failed=0,
                target_table=table_name,
                run_id=self._run_id,
                written_at=datetime.now(timezone.utc),
            )
        except Exception as exc:
            logger.error(
                "write_batch failed",
                extra={"table": table_name, "error": str(exc)},
            )
            result = BatchWriteResult(
                rows_written=0,
                rows_failed=len(rows),
                target_table=table_name,
                run_id=self._run_id,
                written_at=datetime.now(timezone.utc),
            )
            raise WriterError(
                f"Catastrophic batch write failure for {table_name}: {exc}"
            ) from exc

        with self._stats_lock:
            self._batch_results.append(result)
            self._silver_rows_written += result.rows_written
        return result

    def write_quarantine(self, records: Sequence[QuarantineRecord]) -> BatchWriteResult:
        if not records:
            return BatchWriteResult(
                rows_written=0,
                rows_failed=0,
                target_table="",
                run_id=self._run_id,
                written_at=datetime.now(timezone.utc),
            )

        by_table: Dict[str, List[QuarantineRecord]] = {}
        for rec in records:
            by_table.setdefault(rec.source_table, []).append(rec)

        total_written = 0
        total_failed = 0

        for source_table, recs in by_table.items():
            qtable = f"quarantine_{source_table}"
            try:
                params = [self._quarantine_record_to_params(r) for r in recs]
                cols = list(params[0].keys())
                if self._is_sqlite:
                    col_list = ", ".join(f'"{c}"' for c in cols)
                else:
                    col_list = ", ".join(f"`{c}`" for c in cols)
                bind_list = ", ".join(f":{c}" for c in cols)
                sql = text(
                    f'INSERT INTO "{qtable}" ({col_list}) VALUES ({bind_list})'
                    if self._is_sqlite
                    else f"INSERT INTO `{qtable}` ({col_list}) VALUES ({bind_list})"
                )
                with self._engine.begin() as conn:
                    conn.execute(sql, params)
                total_written += len(recs)
            except Exception as exc:
                logger.error(
                    "write_quarantine failed",
                    extra={"table": qtable, "error": str(exc)},
                )
                total_failed += len(recs)

        result = BatchWriteResult(
            rows_written=total_written,
            rows_failed=total_failed,
            target_table="quarantine",
            run_id=self._run_id,
            written_at=datetime.now(timezone.utc),
        )
        with self._stats_lock:
            self._quarantine_results.append(result)
            self._quarantine_rows_written += total_written
        return result

    def finalise(self) -> RunStats:
        finished_at = datetime.now(timezone.utc)
        duration = (finished_at - self._started_at).total_seconds()
        return RunStats(
            run_id=self._run_id,
            source_table=self._source_table,
            bronze_rows_read=self._bronze_rows_read,
            silver_rows_written=self._silver_rows_written,
            quarantine_rows_written=self._quarantine_rows_written,
            null_preserved_count=self._null_preserved_count,
            transformation_errors=self._transformation_errors,
            batch_results=self._batch_results + self._quarantine_results,
            started_at=self._started_at,
            finished_at=finished_at,
            duration_seconds=duration,
        )

    # ------------------------------------------------------------------
    # Internal state setters (called by SilverPipeline)
    # ------------------------------------------------------------------

    def _set_run_context(self, run_id: str, source_table: str) -> None:
        self._run_id = run_id
        self._source_table = source_table

    def _record_bronze_rows(self, count: int) -> None:
        with self._stats_lock:
            self._bronze_rows_read += count

    def _record_null_preserved(self) -> None:
        with self._stats_lock:
            self._null_preserved_count += 1

    def _record_transformation_error(self) -> None:
        with self._stats_lock:
            self._transformation_errors += 1

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

    def _create_silver_table(self, conn, table_mapping) -> None:
        table_name = f"silver_{table_mapping.source_table}"

        if self._is_sqlite:
            base_ddl = f"""
            CREATE TABLE IF NOT EXISTS "{table_name}" (
                _silver_id INTEGER PRIMARY KEY AUTOINCREMENT,
                _bronze_source_ref TEXT NOT NULL,
                _run_id TEXT NOT NULL,
                _transformation_timestamp TEXT NOT NULL,
                _mapping_confidence_score REAL NOT NULL,
                _transformation_strategy TEXT,
                _ontology_class_uri TEXT NOT NULL,
                _expansion_index INTEGER NOT NULL DEFAULT 0,
                _discriminator_value TEXT
            )"""
        else:
            base_ddl = f"""
            CREATE TABLE IF NOT EXISTS `{table_name}` (
                _silver_id                BIGINT        AUTO_INCREMENT PRIMARY KEY,
                _bronze_source_ref        VARCHAR(512)  NOT NULL,
                _run_id                   CHAR(36)      NOT NULL,
                _transformation_timestamp DATETIME(6)   NOT NULL,
                _mapping_confidence_score DOUBLE        NOT NULL,
                _transformation_strategy  VARCHAR(128)  NULL,
                _ontology_class_uri       VARCHAR(1024) NOT NULL,
                _expansion_index          TINYINT       NOT NULL DEFAULT 0,
                _discriminator_value      TEXT,
                INDEX idx_run_id (_run_id),
                INDEX idx_bronze_ref (_bronze_source_ref(255))
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

        conn.execute(text(base_ddl))

        confirmed = [
            m for m in table_mapping.column_mappings
            if m.status == MappingStatus.CONFIRMED
        ]
        for mapping in confirmed:
            col_name = self._safe_col_name(mapping.source_column)
            sql_type = _map_sql_type(mapping.source_data_type, sqlite=self._is_sqlite)
            self._add_column_if_missing(conn, table_name, col_name, sql_type)

        self._initialised_tables.add(table_name)

    def _create_quarantine_table(self, conn, table_mapping) -> None:
        table_name = f"quarantine_{table_mapping.source_table}"
        if self._is_sqlite:
            ddl = f"""
            CREATE TABLE IF NOT EXISTS "{table_name}" (
                _quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
                _bronze_source_ref TEXT NOT NULL,
                _run_id TEXT NOT NULL,
                _transformation_timestamp TEXT NOT NULL,
                _mapping_confidence_score REAL NOT NULL,
                _transformation_strategy TEXT NOT NULL,
                _source_column TEXT NOT NULL,
                _property_uri TEXT,
                _transformer_name TEXT NOT NULL,
                _original_value TEXT,
                _error_detail TEXT NOT NULL
            )"""
        else:
            ddl = f"""
            CREATE TABLE IF NOT EXISTS `{table_name}` (
                _quarantine_id            BIGINT        AUTO_INCREMENT PRIMARY KEY,
                _bronze_source_ref        VARCHAR(512)  NOT NULL,
                _run_id                   CHAR(36)      NOT NULL,
                _transformation_timestamp DATETIME(6)   NOT NULL,
                _mapping_confidence_score DOUBLE        NOT NULL,
                _transformation_strategy  VARCHAR(128)  NOT NULL,
                _source_column            VARCHAR(256)  NOT NULL,
                _property_uri             VARCHAR(1024),
                _transformer_name         VARCHAR(128)  NOT NULL,
                _original_value           TEXT,
                _error_detail             TEXT          NOT NULL,
                INDEX idx_run_id (_run_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
        conn.execute(text(ddl))

    def _add_column_if_missing(
        self, conn, table_name: str, col_name: str, sql_type: str
    ) -> None:
        if self._is_sqlite:
            # SQLite: check pragma, add if absent
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
            # MySQL: check INFORMATION_SCHEMA before adding (idempotent)
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
                    text(f"ALTER TABLE `{table_name}` ADD COLUMN `{col_name}` {sql_type}")
                )

    # ------------------------------------------------------------------
    # Row serialisation
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_col_name(col: str) -> str:
        return col.replace(" ", "_").replace("-", "_").lower()

    @staticmethod
    def _silver_row_to_params(row: SilverRow) -> dict:
        params = {
            "_bronze_source_ref": row.audit.bronze_source_ref,
            "_run_id": row.audit.run_id,
            "_transformation_timestamp": row.audit.transformation_timestamp.strftime(
                "%Y-%m-%d %H:%M:%S.%f"
            ),
            "_mapping_confidence_score": row.audit.mapping_confidence_score,
            "_transformation_strategy": row.audit.transformation_strategy,
            "_ontology_class_uri": row.ontology_class_uri,
            "_expansion_index": row.expansion_index,
            "_discriminator_value": str(row.discriminator_value)
            if row.discriminator_value is not None
            else None,
        }
        col_name = MySQLSilverWriter._safe_col_name(row.source_column)
        params[col_name] = row.transform_result.transformed_value
        return params

    @staticmethod
    def _quarantine_record_to_params(rec: QuarantineRecord) -> dict:
        return {
            "_bronze_source_ref": rec.audit.bronze_source_ref,
            "_run_id": rec.audit.run_id,
            "_transformation_timestamp": rec.audit.transformation_timestamp.strftime(
                "%Y-%m-%d %H:%M:%S.%f"
            ),
            "_mapping_confidence_score": rec.audit.mapping_confidence_score,
            "_transformation_strategy": rec.audit.transformation_strategy,
            "_source_column": rec.source_column,
            "_property_uri": rec.property_uri,
            "_transformer_name": rec.transformer_name,
            "_original_value": str(rec.original_value)
            if rec.original_value is not None
            else None,
            "_error_detail": rec.error_detail,
        }
