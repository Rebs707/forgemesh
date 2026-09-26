# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for _silver_refs and _mapping_run_id lineage columns on gold tables.

Covers:
- Every gold row has non-null _silver_refs and _mapping_run_id after a pipeline run.
- _silver_refs is a valid JSON object mapping silver table name to list of silver IDs.
- _mapping_run_id matches the run_id passed to GoldPipeline.run().
- End-to-end lineage walk: gold._silver_refs → silver._silver_id → silver._bronze_source_ref
  (all references resolve; _bronze_source_ref is non-null).
- Correct behaviour when multiple silver tables feed a single gold table.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text

from semantic_mapper.gold.models import GoldColumnSpec, GoldTableDefinition
from semantic_mapper.gold.pipeline import GoldPipeline
from semantic_mapper.gold.writer.mysql import MySQLGoldWriter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pipeline(definitions, silver_engine, gold_engine, settings) -> GoldPipeline:
    writer = MySQLGoldWriter(engine=gold_engine, settings=settings)
    return GoldPipeline(
        definitions=definitions,
        silver_engine=silver_engine,
        writer=writer,
        settings=settings,
    )


def _read_gold(gold_engine, table: str) -> list:
    with gold_engine.connect() as conn:
        return conn.execute(text(f'SELECT * FROM "{table}"')).mappings().fetchall()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

RUN_ID = "test-lineage-run-001"


@pytest.fixture
def silver_engine_two_rows():
    """In-memory silver with two rows — mirrors the existing conftest fixture."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE silver_raw_party (
                _silver_id                INTEGER PRIMARY KEY AUTOINCREMENT,
                _bronze_source_ref        TEXT NOT NULL,
                _run_id                   TEXT NOT NULL,
                _mapping_confidence_score REAL NOT NULL,
                _ontology_class_uri       TEXT NOT NULL,
                party_id                  TEXT,
                dob                       TEXT
            )
        """))
        conn.execute(text("""
            INSERT INTO silver_raw_party
                (_bronze_source_ref, _run_id, _mapping_confidence_score,
                 _ontology_class_uri, party_id, dob)
            VALUES
                ('raw_party.party_id=P1', 'silver-run-001', 0.95,
                 'http://ex.org/ontology#Party', 'P1', '1978-03-14'),
                ('raw_party.party_id=P2', 'silver-run-001', 0.95,
                 'http://ex.org/ontology#Party', 'P2', '1982-03-14')
        """))
    return engine


@pytest.fixture
def gold_engine():
    return create_engine("sqlite:///:memory:")


@pytest.fixture
def gold_settings(gold_settings):
    return gold_settings


@pytest.fixture
def party_definition():
    return GoldTableDefinition(
        gold_table_name="gold_party",
        primary_class_uri="http://ex.org/ontology#Party",
        class_label="party",
        source_silver_tables=["silver_raw_party"],
        column_specs=[
            GoldColumnSpec(
                silver_table="silver_raw_party",
                source_column="party_id",
                gold_column_name="party_id",
                source_data_type="VARCHAR(50)",
                property_uri="http://ex.org/ontology#partyId",
            ),
            GoldColumnSpec(
                silver_table="silver_raw_party",
                source_column="dob",
                gold_column_name="dob",
                source_data_type="VARCHAR(20)",
                property_uri="http://ex.org/ontology#dateOfBirth",
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Column presence
# ---------------------------------------------------------------------------

class TestNewColumnsExistInDDL:
    def test_silver_refs_column_present(self, party_definition, silver_engine_two_rows, gold_engine, gold_settings):
        pipeline = _make_pipeline([party_definition], silver_engine_two_rows, gold_engine, gold_settings)
        pipeline.run(run_id=RUN_ID)

        with gold_engine.connect() as conn:
            cols = {
                row[1]
                for row in conn.execute(text('PRAGMA table_info("gold_party")')).fetchall()
            }
        assert "_silver_refs" in cols

    def test_mapping_run_id_column_present(self, party_definition, silver_engine_two_rows, gold_engine, gold_settings):
        pipeline = _make_pipeline([party_definition], silver_engine_two_rows, gold_engine, gold_settings)
        pipeline.run(run_id=RUN_ID)

        with gold_engine.connect() as conn:
            cols = {
                row[1]
                for row in conn.execute(text('PRAGMA table_info("gold_party")')).fetchall()
            }
        assert "_mapping_run_id" in cols


# ---------------------------------------------------------------------------
# Non-null guarantee
# ---------------------------------------------------------------------------

class TestNonNull:
    def test_every_row_has_silver_refs(self, party_definition, silver_engine_two_rows, gold_engine, gold_settings):
        pipeline = _make_pipeline([party_definition], silver_engine_two_rows, gold_engine, gold_settings)
        pipeline.run(run_id=RUN_ID)

        rows = _read_gold(gold_engine, "gold_party")
        assert len(rows) == 2
        for row in rows:
            assert row["_silver_refs"] is not None

    def test_every_row_has_mapping_run_id(self, party_definition, silver_engine_two_rows, gold_engine, gold_settings):
        pipeline = _make_pipeline([party_definition], silver_engine_two_rows, gold_engine, gold_settings)
        pipeline.run(run_id=RUN_ID)

        rows = _read_gold(gold_engine, "gold_party")
        assert len(rows) == 2
        for row in rows:
            assert row["_mapping_run_id"] is not None


# ---------------------------------------------------------------------------
# _silver_refs format
# ---------------------------------------------------------------------------

class TestSilverRefsFormat:
    def test_silver_refs_is_valid_json(self, party_definition, silver_engine_two_rows, gold_engine, gold_settings):
        pipeline = _make_pipeline([party_definition], silver_engine_two_rows, gold_engine, gold_settings)
        pipeline.run(run_id=RUN_ID)

        rows = _read_gold(gold_engine, "gold_party")
        for row in rows:
            parsed = json.loads(row["_silver_refs"])
            assert isinstance(parsed, dict)

    def test_silver_refs_keyed_by_silver_table_name(self, party_definition, silver_engine_two_rows, gold_engine, gold_settings):
        pipeline = _make_pipeline([party_definition], silver_engine_two_rows, gold_engine, gold_settings)
        pipeline.run(run_id=RUN_ID)

        rows = _read_gold(gold_engine, "gold_party")
        for row in rows:
            refs = json.loads(row["_silver_refs"])
            assert "silver_raw_party" in refs

    def test_silver_refs_value_is_list_of_ids(self, party_definition, silver_engine_two_rows, gold_engine, gold_settings):
        pipeline = _make_pipeline([party_definition], silver_engine_two_rows, gold_engine, gold_settings)
        pipeline.run(run_id=RUN_ID)

        rows = _read_gold(gold_engine, "gold_party")
        for row in rows:
            refs = json.loads(row["_silver_refs"])
            ids = refs["silver_raw_party"]
            assert isinstance(ids, list)
            assert len(ids) == 1
            assert isinstance(ids[0], int)

    def test_silver_ids_are_distinct_across_rows(self, party_definition, silver_engine_two_rows, gold_engine, gold_settings):
        pipeline = _make_pipeline([party_definition], silver_engine_two_rows, gold_engine, gold_settings)
        pipeline.run(run_id=RUN_ID)

        rows = _read_gold(gold_engine, "gold_party")
        all_ids = [
            json.loads(row["_silver_refs"])["silver_raw_party"][0]
            for row in rows
        ]
        assert len(set(all_ids)) == len(all_ids), "Each gold row must reference a distinct silver row"

    def test_silver_id_not_written_as_raw_column(self, party_definition, silver_engine_two_rows, gold_engine, gold_settings):
        """_silver_id must not appear as a direct column in gold — only via _silver_refs."""
        pipeline = _make_pipeline([party_definition], silver_engine_two_rows, gold_engine, gold_settings)
        pipeline.run(run_id=RUN_ID)

        with gold_engine.connect() as conn:
            cols = {
                row[1]
                for row in conn.execute(text('PRAGMA table_info("gold_party")')).fetchall()
            }
        assert "_silver_id" not in cols


# ---------------------------------------------------------------------------
# _mapping_run_id value
# ---------------------------------------------------------------------------

class TestMappingRunId:
    def test_mapping_run_id_matches_pipeline_run_id(self, party_definition, silver_engine_two_rows, gold_engine, gold_settings):
        pipeline = _make_pipeline([party_definition], silver_engine_two_rows, gold_engine, gold_settings)
        pipeline.run(run_id=RUN_ID)

        rows = _read_gold(gold_engine, "gold_party")
        for row in rows:
            assert row["_mapping_run_id"] == RUN_ID

    def test_mapping_run_id_changes_between_runs(self, party_definition, silver_engine_two_rows, gold_settings):
        gold1 = create_engine("sqlite:///:memory:")
        gold2 = create_engine("sqlite:///:memory:")

        _make_pipeline([party_definition], silver_engine_two_rows, gold1, gold_settings).run(run_id="run-A")
        _make_pipeline([party_definition], silver_engine_two_rows, gold2, gold_settings).run(run_id="run-B")

        rows1 = _read_gold(gold1, "gold_party")
        rows2 = _read_gold(gold2, "gold_party")
        assert all(r["_mapping_run_id"] == "run-A" for r in rows1)
        assert all(r["_mapping_run_id"] == "run-B" for r in rows2)


# ---------------------------------------------------------------------------
# End-to-end lineage walk: gold → silver → bronze
# ---------------------------------------------------------------------------

class TestLineageWalk:
    def test_all_silver_ids_resolve_to_real_rows(self, party_definition, silver_engine_two_rows, gold_engine, gold_settings):
        """Each silver ID in _silver_refs must reference an existing silver row."""
        pipeline = _make_pipeline([party_definition], silver_engine_two_rows, gold_engine, gold_settings)
        pipeline.run(run_id=RUN_ID)

        gold_rows = _read_gold(gold_engine, "gold_party")
        with silver_engine_two_rows.connect() as s_conn:
            for gold_row in gold_rows:
                refs = json.loads(gold_row["_silver_refs"])
                for silver_table, silver_ids in refs.items():
                    for sid in silver_ids:
                        silver_row = s_conn.execute(
                            text(f'SELECT _silver_id FROM "{silver_table}" WHERE _silver_id = :id'),
                            {"id": sid},
                        ).fetchone()
                        assert silver_row is not None, (
                            f"silver ID {sid} not found in {silver_table}"
                        )

    def test_all_silver_rows_have_bronze_source_ref(self, party_definition, silver_engine_two_rows, gold_engine, gold_settings):
        """Every silver row referenced by a gold row has a non-null _bronze_source_ref."""
        pipeline = _make_pipeline([party_definition], silver_engine_two_rows, gold_engine, gold_settings)
        pipeline.run(run_id=RUN_ID)

        gold_rows = _read_gold(gold_engine, "gold_party")
        with silver_engine_two_rows.connect() as s_conn:
            for gold_row in gold_rows:
                refs = json.loads(gold_row["_silver_refs"])
                for silver_table, silver_ids in refs.items():
                    for sid in silver_ids:
                        silver_row = s_conn.execute(
                            text(
                                f'SELECT _bronze_source_ref FROM "{silver_table}" '
                                f'WHERE _silver_id = :id'
                            ),
                            {"id": sid},
                        ).fetchone()
                        assert silver_row is not None
                        assert silver_row[0] is not None, (
                            f"_bronze_source_ref is NULL for silver ID {sid} in {silver_table}"
                        )

    def test_bronze_ref_format_is_non_empty(self, party_definition, silver_engine_two_rows, gold_engine, gold_settings):
        pipeline = _make_pipeline([party_definition], silver_engine_two_rows, gold_engine, gold_settings)
        pipeline.run(run_id=RUN_ID)

        gold_rows = _read_gold(gold_engine, "gold_party")
        with silver_engine_two_rows.connect() as s_conn:
            for gold_row in gold_rows:
                refs = json.loads(gold_row["_silver_refs"])
                for silver_table, silver_ids in refs.items():
                    for sid in silver_ids:
                        bronze_ref = s_conn.execute(
                            text(
                                f'SELECT _bronze_source_ref FROM "{silver_table}" '
                                f'WHERE _silver_id = :id'
                            ),
                            {"id": sid},
                        ).scalar()
                        assert isinstance(bronze_ref, str) and len(bronze_ref) > 0


# ---------------------------------------------------------------------------
# Multi-silver-table gold table
# ---------------------------------------------------------------------------

class TestMultiSilverTable:
    def test_refs_contain_correct_silver_table(self):
        """When two silver tables feed one gold table, _silver_refs uses the source table name."""
        silver = create_engine("sqlite:///:memory:")
        with silver.begin() as conn:
            conn.execute(text("""
                CREATE TABLE silver_admissions (
                    _silver_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    _bronze_source_ref TEXT NOT NULL,
                    _run_id TEXT NOT NULL,
                    _mapping_confidence_score REAL NOT NULL,
                    _ontology_class_uri TEXT NOT NULL,
                    hadm_id TEXT
                )
            """))
            conn.execute(text("""
                CREATE TABLE silver_transfers (
                    _silver_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    _bronze_source_ref TEXT NOT NULL,
                    _run_id TEXT NOT NULL,
                    _mapping_confidence_score REAL NOT NULL,
                    _ontology_class_uri TEXT NOT NULL,
                    transfer_id TEXT
                )
            """))
            conn.execute(text(
                "INSERT INTO silver_admissions (_bronze_source_ref, _run_id, "
                "_mapping_confidence_score, _ontology_class_uri, hadm_id) "
                "VALUES ('admissions.hadm_id=100', 'sr-1', 0.9, 'http://ex.org#Admission', 'A100')"
            ))
            conn.execute(text(
                "INSERT INTO silver_transfers (_bronze_source_ref, _run_id, "
                "_mapping_confidence_score, _ontology_class_uri, transfer_id) "
                "VALUES ('transfers.transfer_id=200', 'sr-1', 0.9, 'http://ex.org#Admission', 'T200')"
            ))

        gold = create_engine("sqlite:///:memory:")
        from semantic_mapper.config.settings import Settings
        settings = Settings(
            DATABASE_URL="sqlite:///:memory:",
            SILVER_DATABASE_URL="sqlite:///:memory:",
            GOLD_DATABASE_URL="sqlite:///:memory:",
            GOLD_WRITER="mysql",
            GOLD_BATCH_SIZE=5,
        )

        defn = GoldTableDefinition(
            gold_table_name="gold_admission",
            primary_class_uri="http://ex.org#Admission",
            class_label="admission",
            source_silver_tables=["silver_admissions", "silver_transfers"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_admissions",
                    source_column="hadm_id",
                    gold_column_name="hadm_id",
                    source_data_type="TEXT",
                    property_uri="http://ex.org#hadmId",
                ),
                GoldColumnSpec(
                    silver_table="silver_transfers",
                    source_column="transfer_id",
                    gold_column_name="transfer_id",
                    source_data_type="TEXT",
                    property_uri="http://ex.org#transferId",
                ),
            ],
        )

        pipeline = _make_pipeline([defn], silver, gold, settings)
        pipeline.run(run_id="multi-silver-run")

        gold_rows = _read_gold(gold, "gold_admission")

        # One gold row comes from silver_admissions, one from silver_transfers
        tables_seen = set()
        for row in gold_rows:
            refs = json.loads(row["_silver_refs"])
            tables_seen.update(refs.keys())

        assert "silver_admissions" in tables_seen
        assert "silver_transfers" in tables_seen

    def test_each_row_references_only_its_source_table(self):
        """A row sourced from silver_admissions must not claim silver_transfers in its refs."""
        silver = create_engine("sqlite:///:memory:")
        with silver.begin() as conn:
            conn.execute(text("""
                CREATE TABLE silver_admissions (
                    _silver_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    _bronze_source_ref TEXT NOT NULL,
                    _run_id TEXT NOT NULL,
                    _mapping_confidence_score REAL NOT NULL,
                    _ontology_class_uri TEXT NOT NULL,
                    hadm_id TEXT
                )
            """))
            conn.execute(text("""
                CREATE TABLE silver_transfers (
                    _silver_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    _bronze_source_ref TEXT NOT NULL,
                    _run_id TEXT NOT NULL,
                    _mapping_confidence_score REAL NOT NULL,
                    _ontology_class_uri TEXT NOT NULL,
                    transfer_id TEXT
                )
            """))
            conn.execute(text(
                "INSERT INTO silver_admissions (_bronze_source_ref, _run_id, "
                "_mapping_confidence_score, _ontology_class_uri, hadm_id) "
                "VALUES ('admissions.hadm_id=100', 'sr-1', 0.9, 'http://ex.org#Admission', 'A100')"
            ))
            conn.execute(text(
                "INSERT INTO silver_transfers (_bronze_source_ref, _run_id, "
                "_mapping_confidence_score, _ontology_class_uri, transfer_id) "
                "VALUES ('transfers.transfer_id=200', 'sr-1', 0.9, 'http://ex.org#Admission', 'T200')"
            ))

        gold = create_engine("sqlite:///:memory:")
        from semantic_mapper.config.settings import Settings
        settings = Settings(
            DATABASE_URL="sqlite:///:memory:",
            SILVER_DATABASE_URL="sqlite:///:memory:",
            GOLD_DATABASE_URL="sqlite:///:memory:",
            GOLD_WRITER="mysql",
            GOLD_BATCH_SIZE=5,
        )

        defn = GoldTableDefinition(
            gold_table_name="gold_admission",
            primary_class_uri="http://ex.org#Admission",
            class_label="admission",
            source_silver_tables=["silver_admissions", "silver_transfers"],
            column_specs=[
                GoldColumnSpec(
                    silver_table="silver_admissions",
                    source_column="hadm_id",
                    gold_column_name="hadm_id",
                    source_data_type="TEXT",
                    property_uri="http://ex.org#hadmId",
                ),
                GoldColumnSpec(
                    silver_table="silver_transfers",
                    source_column="transfer_id",
                    gold_column_name="transfer_id",
                    source_data_type="TEXT",
                    property_uri="http://ex.org#transferId",
                ),
            ],
        )

        pipeline = _make_pipeline([defn], silver, gold, settings)
        pipeline.run(run_id="multi-silver-run")

        gold_rows = _read_gold(gold, "gold_admission")
        for row in gold_rows:
            refs = json.loads(row["_silver_refs"])
            # Each row references exactly one silver table
            assert len(refs) == 1
