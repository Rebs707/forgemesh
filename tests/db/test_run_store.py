# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for semantic_mapper/db/run_store.py.

All tests use a temporary SQLite database so they are fully isolated from
any real pipeline_runs.db on the filesystem.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from semantic_mapper.config.settings import Settings
from semantic_mapper.db.run_store import (
    get_mapping_report,
    get_pipeline_runs_db_path,
    write_mapping_report,
)
from semantic_mapper.schema.mapping_output import SemanticMappingReport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_report(**kwargs) -> SemanticMappingReport:
    defaults = dict(
        ontology_uri="http://test.org/ont",
        database_name="test_db",
        settings_used={},
        tables=[],
        total_pending_review=0,
    )
    defaults.update(kwargs)
    return SemanticMappingReport(**defaults)


def _make_db(tmp_path: Path) -> Path:
    """Create a pipeline_runs.db with the mapping_report_json column."""
    db_path = tmp_path / "pipeline_runs.db"
    con = sqlite3.connect(str(db_path))
    con.execute("""
        CREATE TABLE pipeline_runs (
            run_id               TEXT PRIMARY KEY,
            started_at           TEXT NOT NULL,
            finished_at          TEXT,
            status               TEXT NOT NULL,
            return_code          INTEGER,
            phases               TEXT,
            threshold            REAL,
            log_content          TEXT,
            ontology_hash        TEXT,
            gold_mode            INTEGER,
            mapping_report_json  TEXT
        )
    """)
    con.commit()
    con.close()
    return db_path


def _insert_run(db_path: Path, run_id: str) -> None:
    con = sqlite3.connect(str(db_path))
    con.execute(
        "INSERT INTO pipeline_runs (run_id, started_at, status) VALUES (?, ?, ?)",
        (run_id, "2026-05-03T10:00:00Z", "done"),
    )
    con.commit()
    con.close()


def _settings_for(db_path: Path) -> Settings:
    """Return Settings whose CORRECTIONS_DB_PATH parent resolves to db_path's parent."""
    corrections_db = db_path.parent / "corrections.db"
    return Settings(
        DATABASE_URL="sqlite:///:memory:",
        OPENAI_API_KEY="x",
        CORRECTIONS_DB_PATH=str(corrections_db),
    )


# ---------------------------------------------------------------------------
# get_pipeline_runs_db_path
# ---------------------------------------------------------------------------

class TestGetPipelineRunsDbPath:
    def test_path_is_sibling_of_corrections_db(self, tmp_path):
        settings = _settings_for(tmp_path / "pipeline_runs.db")
        result = get_pipeline_runs_db_path(settings)
        assert result.name == "pipeline_runs.db"
        assert result.parent == Path(settings.CORRECTIONS_DB_PATH).parent


# ---------------------------------------------------------------------------
# write_mapping_report
# ---------------------------------------------------------------------------

class TestWriteMappingReport:
    def test_writes_json_to_column(self, tmp_path):
        db_path = _make_db(tmp_path)
        run_id = str(uuid.uuid4())
        _insert_run(db_path, run_id)
        settings = _settings_for(db_path)
        report = _minimal_report(ontology_uri="http://test.org/ont#v1")

        write_mapping_report(run_id, report, settings)

        con = sqlite3.connect(str(db_path))
        row = con.execute(
            "SELECT mapping_report_json FROM pipeline_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        con.close()
        assert row is not None
        assert row[0] is not None
        parsed = json.loads(row[0])
        assert parsed["ontology_uri"] == "http://test.org/ont#v1"

    def test_column_is_null_for_other_runs(self, tmp_path):
        db_path = _make_db(tmp_path)
        run_id_a = str(uuid.uuid4())
        run_id_b = str(uuid.uuid4())
        _insert_run(db_path, run_id_a)
        _insert_run(db_path, run_id_b)
        settings = _settings_for(db_path)
        write_mapping_report(run_id_a, _minimal_report(), settings)

        con = sqlite3.connect(str(db_path))
        row = con.execute(
            "SELECT mapping_report_json FROM pipeline_runs WHERE run_id = ?",
            (run_id_b,),
        ).fetchone()
        con.close()
        assert row[0] is None

    def test_does_not_raise_on_missing_db(self, tmp_path):
        # DB file does not exist — must swallow and log, not raise.
        settings = _settings_for(tmp_path / "pipeline_runs.db")
        write_mapping_report("nonexistent-run", _minimal_report(), settings)

    def test_overwrites_on_second_write(self, tmp_path):
        db_path = _make_db(tmp_path)
        run_id = str(uuid.uuid4())
        _insert_run(db_path, run_id)
        settings = _settings_for(db_path)

        write_mapping_report(run_id, _minimal_report(database_name="first"), settings)
        write_mapping_report(run_id, _minimal_report(database_name="second"), settings)

        con = sqlite3.connect(str(db_path))
        row = con.execute(
            "SELECT mapping_report_json FROM pipeline_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        con.close()
        assert json.loads(row[0])["database_name"] == "second"


# ---------------------------------------------------------------------------
# get_mapping_report
# ---------------------------------------------------------------------------

class TestGetMappingReport:
    def test_returns_deserialised_report(self, tmp_path):
        db_path = _make_db(tmp_path)
        run_id = str(uuid.uuid4())
        _insert_run(db_path, run_id)
        settings = _settings_for(db_path)
        original = _minimal_report(database_name="round_trip_db")
        write_mapping_report(run_id, original, settings)

        result = get_mapping_report(run_id, settings)

        assert result is not None
        assert isinstance(result, SemanticMappingReport)
        assert result.database_name == "round_trip_db"

    def test_returns_none_for_unknown_run_id(self, tmp_path):
        db_path = _make_db(tmp_path)
        settings = _settings_for(db_path)

        result = get_mapping_report("no-such-run", settings)

        assert result is None

    def test_returns_none_when_column_is_null(self, tmp_path):
        db_path = _make_db(tmp_path)
        run_id = str(uuid.uuid4())
        _insert_run(db_path, run_id)
        settings = _settings_for(db_path)

        result = get_mapping_report(run_id, settings)

        assert result is None

    def test_returns_none_on_missing_db(self, tmp_path):
        settings = _settings_for(tmp_path / "pipeline_runs.db")

        result = get_mapping_report("any-run", settings)

        assert result is None

    def test_report_fields_survive_round_trip(self, tmp_path):
        db_path = _make_db(tmp_path)
        run_id = str(uuid.uuid4())
        _insert_run(db_path, run_id)
        settings = _settings_for(db_path)
        original = _minimal_report(
            ontology_uri="http://example.org/ont#",
            database_name="mimic_bronze",
            total_pending_review=7,
        )
        write_mapping_report(run_id, original, settings)

        result = get_mapping_report(run_id, settings)

        assert result.ontology_uri == "http://example.org/ont#"
        assert result.database_name == "mimic_bronze"
        assert result.total_pending_review == 7

    def test_schema_version_preserved(self, tmp_path):
        db_path = _make_db(tmp_path)
        run_id = str(uuid.uuid4())
        _insert_run(db_path, run_id)
        settings = _settings_for(db_path)
        write_mapping_report(run_id, _minimal_report(), settings)

        result = get_mapping_report(run_id, settings)

        assert result.schema_version == "1.3.0"
