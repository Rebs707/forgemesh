# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for Phase 4 — Lineage walker.

Uses SQLite in-memory engines — no live MySQL or Fuseki required.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text

from semantic_mapper.prove.lineage_walker import (
    LineageWalker,
    _parse_bronze_ref,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BASE_URI = "http://mesh.local/data/"


def _make_bronze():
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE raw_admissions "
            "(row_id TEXT, subject_id TEXT, admission_type TEXT)"
        ))
        conn.execute(text(
            "INSERT INTO raw_admissions VALUES ('1', '42', 'URGENT')"
        ))
        conn.execute(text(
            "INSERT INTO raw_admissions VALUES ('2', '99', 'ELECTIVE')"
        ))
        conn.commit()
    return engine


def _make_silver(bronze_ref_1: str = "raw_admissions.row_id=1",
                 bronze_ref_2: str = "raw_admissions.row_id=2"):
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE silver_admissions "
            "(_silver_id INTEGER PRIMARY KEY, _bronze_source_ref TEXT, subject_id TEXT)"
        ))
        conn.execute(text(
            f"INSERT INTO silver_admissions VALUES (1, '{bronze_ref_1}', '42')"
        ))
        conn.execute(text(
            f"INSERT INTO silver_admissions VALUES (2, '{bronze_ref_2}', '99')"
        ))
        conn.commit()
    return engine


def _make_gold(silver_refs_1: str | None = None):
    if silver_refs_1 is None:
        silver_refs_1 = json.dumps({"silver_admissions": [1]})
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE gold_admission ("
            "_gold_id INTEGER PRIMARY KEY,"
            "_ontology_class_uri TEXT,"
            "_bronze_source_ref TEXT,"
            "_silver_run_id TEXT,"
            "_gold_run_id TEXT,"
            "_mapping_confidence_score TEXT,"
            "_silver_refs TEXT,"
            "_mapping_run_id TEXT,"
            "subject_id TEXT"
            ")"
        ))
        conn.execute(text(
            "INSERT INTO gold_admission VALUES "
            "(1, 'http://mesh.local/Patient', 'raw_admissions.row_id=1', "
            "NULL, NULL, NULL, :refs, NULL, '42')"
        ), {"refs": silver_refs_1})
        conn.commit()
    return engine


@pytest.fixture
def engines():
    return _make_bronze(), _make_silver(), _make_gold()


@pytest.fixture
def walker(engines):
    bronze, silver, gold = engines
    return LineageWalker(
        gold_engine=gold,
        silver_engine=silver,
        bronze_engine=bronze,
        base_uri=BASE_URI,
    )


# ---------------------------------------------------------------------------
# _parse_bronze_ref unit tests
# ---------------------------------------------------------------------------

class TestParseBronzeRef:
    def test_standard_format(self):
        assert _parse_bronze_ref("raw_admissions.row_id=12345") == (
            "raw_admissions", "row_id", "12345"
        )

    def test_col_with_underscore(self):
        assert _parse_bronze_ref("raw_lab.specimen_id=7") == (
            "raw_lab", "specimen_id", "7"
        )

    def test_value_with_equals(self):
        # value itself contains '=' — should still parse correctly
        result = _parse_bronze_ref("raw_t.col=a=b")
        assert result == ("raw_t", "col", "a=b")

    def test_missing_dot_returns_none(self):
        assert _parse_bronze_ref("no_dot_here") is None

    def test_missing_equals_returns_none(self):
        assert _parse_bronze_ref("table.col") is None

    def test_empty_string_returns_none(self):
        assert _parse_bronze_ref("") is None


# ---------------------------------------------------------------------------
# walk_gold — full chain
# ---------------------------------------------------------------------------

class TestWalkGold:
    def test_returns_one_chain_per_id(self, walker):
        chains = walker.walk_gold("gold_admission", [1])
        assert len(chains) == 1

    def test_gold_record_table(self, walker):
        chain = walker.walk_gold("gold_admission", [1])[0]
        assert chain.gold.table == "gold_admission"

    def test_gold_record_id(self, walker):
        chain = walker.walk_gold("gold_admission", [1])[0]
        assert chain.gold.id == 1

    def test_gold_fields_used_excludes_audit_cols(self, walker):
        chain = walker.walk_gold("gold_admission", [1])[0]
        audit = {
            "_gold_id", "_ontology_class_uri", "_bronze_source_ref",
            "_silver_run_id", "_gold_run_id", "_mapping_confidence_score",
            "_silver_refs", "_mapping_run_id",
        }
        assert not (set(chain.gold.fields_used) & audit)

    def test_gold_fields_used_includes_data_cols(self, walker):
        chain = walker.walk_gold("gold_admission", [1])[0]
        assert "subject_id" in chain.gold.fields_used

    def test_silver_record_present(self, walker):
        chain = walker.walk_gold("gold_admission", [1])[0]
        assert len(chain.silver) == 1
        assert chain.silver[0].table == "silver_admissions"
        assert chain.silver[0].id == 1

    def test_silver_transformations_stubbed_empty(self, walker):
        chain = walker.walk_gold("gold_admission", [1])[0]
        assert chain.silver[0].transformations == []

    def test_bronze_record_present(self, walker):
        chain = walker.walk_gold("gold_admission", [1])[0]
        assert len(chain.bronze) == 1

    def test_bronze_record_table(self, walker):
        chain = walker.walk_gold("gold_admission", [1])[0]
        assert chain.bronze[0].table == "raw_admissions"

    def test_bronze_record_pk(self, walker):
        chain = walker.walk_gold("gold_admission", [1])[0]
        assert chain.bronze[0].pk == {"row_id": "1"}

    def test_bronze_raw_values_contain_data(self, walker):
        chain = walker.walk_gold("gold_admission", [1])[0]
        assert chain.bronze[0].raw_values.get("admission_type") == "URGENT"

    def test_bronze_pk_excluded_from_raw_values(self, walker):
        chain = walker.walk_gold("gold_admission", [1])[0]
        assert "row_id" not in chain.bronze[0].raw_values

    def test_not_broken_on_valid_chain(self, walker):
        chain = walker.walk_gold("gold_admission", [1])[0]
        assert chain.broken is False
        assert chain.broken_reason is None

    def test_empty_ids_returns_empty(self, walker):
        assert walker.walk_gold("gold_admission", []) == []

    def test_missing_gold_id_returns_broken(self, walker):
        chain = walker.walk_gold("gold_admission", [999])[0]
        assert chain.broken is True
        assert chain.broken_reason is not None

    def test_missing_gold_does_not_raise(self, walker):
        chains = walker.walk_gold("gold_admission", [999])
        assert len(chains) == 1


# ---------------------------------------------------------------------------
# walk_gold — merged silver refs (multiple silver rows)
# ---------------------------------------------------------------------------

class TestWalkGoldMergedSilver:
    @pytest.fixture
    def walker_multi(self):
        bronze = _make_bronze()
        silver = _make_silver()
        # Gold row references both silver rows
        refs = json.dumps({"silver_admissions": [1, 2]})
        gold = _make_gold(silver_refs_1=refs)
        return LineageWalker(
            gold_engine=gold, silver_engine=silver, bronze_engine=bronze,
            base_uri=BASE_URI,
        )

    def test_two_silver_records(self, walker_multi):
        chain = walker_multi.walk_gold("gold_admission", [1])[0]
        assert len(chain.silver) == 2

    def test_two_bronze_records(self, walker_multi):
        chain = walker_multi.walk_gold("gold_admission", [1])[0]
        assert len(chain.bronze) == 2

    def test_both_bronze_tables_raw_admissions(self, walker_multi):
        chain = walker_multi.walk_gold("gold_admission", [1])[0]
        tables = {b.table for b in chain.bronze}
        assert tables == {"raw_admissions"}

    def test_both_bronze_pks_present(self, walker_multi):
        chain = walker_multi.walk_gold("gold_admission", [1])[0]
        pks = {list(b.pk.values())[0] for b in chain.bronze}
        assert "1" in pks
        assert "2" in pks


# ---------------------------------------------------------------------------
# walk_gold — broken chains
# ---------------------------------------------------------------------------

class TestWalkGoldBroken:
    def test_missing_silver_row_sets_broken(self):
        bronze = _make_bronze()
        # Silver is empty
        silver = create_engine("sqlite:///:memory:")
        with silver.connect() as conn:
            conn.execute(text(
                "CREATE TABLE silver_admissions "
                "(_silver_id INTEGER PRIMARY KEY, _bronze_source_ref TEXT, subject_id TEXT)"
            ))
            conn.commit()
        gold = _make_gold()
        w = LineageWalker(gold_engine=gold, silver_engine=silver, bronze_engine=bronze, base_uri=BASE_URI)
        chain = w.walk_gold("gold_admission", [1])[0]
        assert chain.broken is True

    def test_missing_bronze_row_sets_broken(self):
        # Bronze is empty
        bronze = create_engine("sqlite:///:memory:")
        with bronze.connect() as conn:
            conn.execute(text(
                "CREATE TABLE raw_admissions (row_id TEXT, subject_id TEXT, admission_type TEXT)"
            ))
            conn.commit()
        silver = _make_silver()
        gold = _make_gold()
        w = LineageWalker(gold_engine=gold, silver_engine=silver, bronze_engine=bronze, base_uri=BASE_URI)
        chain = w.walk_gold("gold_admission", [1])[0]
        assert chain.broken is True

    def test_malformed_bronze_ref_sets_broken(self):
        bronze = _make_bronze()
        # Silver row has malformed _bronze_source_ref
        silver = create_engine("sqlite:///:memory:")
        with silver.connect() as conn:
            conn.execute(text(
                "CREATE TABLE silver_admissions "
                "(_silver_id INTEGER PRIMARY KEY, _bronze_source_ref TEXT, subject_id TEXT)"
            ))
            conn.execute(text(
                "INSERT INTO silver_admissions VALUES (1, 'BADREF', '42')"
            ))
            conn.commit()
        gold = _make_gold()
        w = LineageWalker(gold_engine=gold, silver_engine=silver, bronze_engine=bronze, base_uri=BASE_URI)
        chain = w.walk_gold("gold_admission", [1])[0]
        assert chain.broken is True

    def test_empty_bronze_ref_sets_broken(self):
        bronze = _make_bronze()
        silver = create_engine("sqlite:///:memory:")
        with silver.connect() as conn:
            conn.execute(text(
                "CREATE TABLE silver_admissions "
                "(_silver_id INTEGER PRIMARY KEY, _bronze_source_ref TEXT, subject_id TEXT)"
            ))
            conn.execute(text(
                "INSERT INTO silver_admissions VALUES (1, '', '42')"
            ))
            conn.commit()
        gold = _make_gold()
        w = LineageWalker(gold_engine=gold, silver_engine=silver, bronze_engine=bronze, base_uri=BASE_URI)
        chain = w.walk_gold("gold_admission", [1])[0]
        assert chain.broken is True
        assert chain.bronze == []

    def test_broken_chain_does_not_raise(self):
        bronze = _make_bronze()
        silver = _make_silver()
        gold = _make_gold()
        w = LineageWalker(gold_engine=gold, silver_engine=silver, bronze_engine=bronze, base_uri=BASE_URI)
        # Nonexistent ID — must not raise
        chains = w.walk_gold("gold_admission", [999])
        assert chains[0].broken is True


# ---------------------------------------------------------------------------
# walk_silver — one hop only
# ---------------------------------------------------------------------------

class TestWalkSilver:
    def test_returns_one_chain(self, walker):
        chains = walker.walk_silver("silver_admissions", [1])
        assert len(chains) == 1

    def test_gold_record_is_empty_stub(self, walker):
        chain = walker.walk_silver("silver_admissions", [1])[0]
        assert chain.gold.table == ""
        assert chain.gold.id == 0
        assert chain.gold.fields_used == []

    def test_silver_record_present(self, walker):
        chain = walker.walk_silver("silver_admissions", [1])[0]
        assert len(chain.silver) == 1
        assert chain.silver[0].id == 1

    def test_bronze_record_resolved(self, walker):
        chain = walker.walk_silver("silver_admissions", [1])[0]
        assert len(chain.bronze) == 1
        assert chain.bronze[0].table == "raw_admissions"

    def test_not_broken(self, walker):
        chain = walker.walk_silver("silver_admissions", [1])[0]
        assert chain.broken is False

    def test_missing_silver_id_broken(self, walker):
        chain = walker.walk_silver("silver_admissions", [999])[0]
        assert chain.broken is True

    def test_empty_ids_returns_empty(self, walker):
        assert walker.walk_silver("silver_admissions", []) == []

    def test_subject_uri_is_none(self, walker):
        chain = walker.walk_silver("silver_admissions", [1])[0]
        assert chain.subject_uri is None

    def test_row_id_is_silver_id(self, walker):
        chain = walker.walk_silver("silver_admissions", [1])[0]
        assert chain.row_id == 1


# ---------------------------------------------------------------------------
# walk_uris — SPARQL mode
# ---------------------------------------------------------------------------

class TestWalkUris:
    def test_returns_one_chain(self, walker):
        uri = f"{BASE_URI}admission/42"
        chains = walker.walk_uris([uri], "gold_admission", "subject_id")
        assert len(chains) == 1

    def test_subject_uri_preserved(self, walker):
        uri = f"{BASE_URI}admission/42"
        chain = walker.walk_uris([uri], "gold_admission", "subject_id")[0]
        assert chain.subject_uri == uri

    def test_bronze_resolved(self, walker):
        uri = f"{BASE_URI}admission/42"
        chain = walker.walk_uris([uri], "gold_admission", "subject_id")[0]
        assert len(chain.bronze) == 1
        assert chain.bronze[0].raw_values.get("admission_type") == "URGENT"

    def test_not_broken(self, walker):
        uri = f"{BASE_URI}admission/42"
        chain = walker.walk_uris([uri], "gold_admission", "subject_id")[0]
        assert chain.broken is False

    def test_unknown_uri_broken(self, walker):
        uri = f"{BASE_URI}admission/0"
        chain = walker.walk_uris([uri], "gold_admission", "subject_id")[0]
        assert chain.broken is True

    def test_empty_list_returns_empty(self, walker):
        assert walker.walk_uris([], "gold_admission", "subject_id") == []

    def test_uri_with_source_name(self):
        bronze = _make_bronze()
        silver = _make_silver()
        gold = _make_gold()
        w = LineageWalker(
            gold_engine=gold, silver_engine=silver, bronze_engine=bronze,
            base_uri=BASE_URI, source_name="mimic",
        )
        uri = f"{BASE_URI}mimic/admission/42"
        chain = w.walk_uris([uri], "gold_admission", "subject_id")[0]
        assert chain.broken is False
        assert len(chain.bronze) == 1

    def test_hashed_uri_resolves(self, walker):
        # UriFactory.individual() hashes the id value, so production subject URIs
        # carry hash_identifier(subject_id), not the raw value. The walk must still
        # resolve the gold row (regression: previously matched the raw column only
        # → 100% broken chains for real triplestore data).
        from semantic_mapper.rdf.uri_factory import hash_identifier
        uri = f"{BASE_URI}admission/{hash_identifier('42')}"
        chain = walker.walk_uris([uri], "gold_admission", "subject_id")[0]
        assert chain.broken is False
        assert chain.subject_uri == uri
        assert len(chain.bronze) == 1
        assert chain.bronze[0].raw_values.get("admission_type") == "URGENT"

    def test_hashed_uri_unknown_is_broken(self, walker):
        from semantic_mapper.rdf.uri_factory import hash_identifier
        uri = f"{BASE_URI}admission/{hash_identifier('does-not-exist')}"
        chain = walker.walk_uris([uri], "gold_admission", "subject_id")[0]
        assert chain.broken is True


# ---------------------------------------------------------------------------
# Batching — 1000 subjects in one shot (performance smoke test)
# ---------------------------------------------------------------------------

class TestBatching:
    def test_walk_gold_batch_size(self):
        """1000 gold IDs with matching rows should all resolve without error."""
        N = 1000
        bronze = create_engine("sqlite:///:memory:")
        silver = create_engine("sqlite:///:memory:")
        gold = create_engine("sqlite:///:memory:")

        with bronze.connect() as conn:
            conn.execute(text("CREATE TABLE raw_t (pk TEXT, val TEXT)"))
            for i in range(1, N + 1):
                conn.execute(text("INSERT INTO raw_t VALUES (:pk, :val)"), {"pk": str(i), "val": f"v{i}"})
            conn.commit()

        with silver.connect() as conn:
            conn.execute(text(
                "CREATE TABLE silver_t (_silver_id INTEGER PRIMARY KEY, _bronze_source_ref TEXT)"
            ))
            for i in range(1, N + 1):
                conn.execute(
                    text("INSERT INTO silver_t VALUES (:id, :ref)"),
                    {"id": i, "ref": f"raw_t.pk={i}"},
                )
            conn.commit()

        with gold.connect() as conn:
            conn.execute(text(
                "CREATE TABLE gold_t (_gold_id INTEGER PRIMARY KEY, _silver_refs TEXT, data_col TEXT)"
            ))
            for i in range(1, N + 1):
                refs = json.dumps({"silver_t": [i]})
                conn.execute(
                    text("INSERT INTO gold_t VALUES (:gid, :refs, :d)"),
                    {"gid": i, "refs": refs, "d": f"d{i}"},
                )
            conn.commit()

        w = LineageWalker(
            gold_engine=gold, silver_engine=silver, bronze_engine=bronze,
            base_uri=BASE_URI,
        )
        chains = w.walk_gold("gold_t", list(range(1, N + 1)))
        assert len(chains) == N
        broken = [c for c in chains if c.broken]
        assert broken == []
