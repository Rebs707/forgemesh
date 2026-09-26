# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Phase 4 — Lineage walker.

Walks gold → silver → bronze using batched SQL lookups.
Never raises on broken references; sets broken=True with a reason.

Three entry points:

  walk_uris(subject_uris, gold_table, id_column)
      SPARQL mode.  Parses subject URIs to extract the id value, then looks up
      the matching gold row via id_column.

  walk_gold(gold_table, gold_ids)
      Gold SQL mode.  gold_ids are _gold_id (autoincrement) values.

  walk_silver(silver_table, silver_ids)
      Silver SQL mode.  silver_ids are _silver_id (autoincrement) values.
      One hop only: silver → bronze.

All SQL lookups are batched with WHERE … IN (…).  No row-by-row iteration.
TransformationRecord is intentionally empty (Phase 4 stub; option 3 is the
long-term fix: capture transformer provenance at silver write time).
"""
from __future__ import annotations

import json
import logging
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine

from semantic_mapper.rdf.uri_factory import hash_identifier

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransformationRecord:
    field: str
    transformer_type: str
    value_before: Any
    value_after: Any


@dataclass(frozen=True)
class BronzeRecord:
    table: str
    pk: Dict[str, Any]
    raw_values: Dict[str, Any]


@dataclass(frozen=True)
class SilverRecord:
    table: str
    id: int
    transformations: List[TransformationRecord]
    row_values: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GoldRecord:
    table: str
    id: int
    fields_used: List[str]
    row_values: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LineageChain:
    subject_uri: Optional[str]     # SPARQL mode
    row_id: Optional[int]          # SQL modes
    gold: GoldRecord
    silver: List[SilverRecord]
    bronze: List[BronzeRecord]
    broken: bool
    broken_reason: Optional[str]


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------

_AUDIT_COLS = frozenset({
    "_gold_id", "_ontology_class_uri", "_bronze_source_ref",
    "_silver_run_id", "_gold_run_id", "_mapping_confidence_score",
    "_silver_refs", "_mapping_run_id",
})

_SILVER_AUDIT_COLS = frozenset({
    "_silver_id", "_bronze_source_ref", "_run_id",
    "_transformation_timestamp", "_mapping_confidence_score", "_transformation_strategy",
})


class LineageWalker:
    """Walks lineage chains from gold/silver/SPARQL subjects back to bronze."""

    def __init__(
        self,
        gold_engine: Engine,
        silver_engine: Engine,
        bronze_engine: Engine,
        base_uri: str = "http://mesh.local/data/",
        source_name: str = "",
    ) -> None:
        self._gold = gold_engine
        self._silver = silver_engine
        self._bronze = bronze_engine
        self._base = base_uri.rstrip("/") + "/"
        self._source_prefix = (
            urllib.parse.quote(source_name.strip(), safe="") + "/"
            if source_name.strip()
            else ""
        )

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def walk_uris(
        self,
        subject_uris: List[str],
        gold_table: str,
        id_column: str,
    ) -> List[LineageChain]:
        """Walk from SPARQL subject URIs → gold → silver → bronze.

        Parses each URI to extract the id value, then batch-fetches gold rows
        WHERE {id_column} IN (...).  Falls through to the gold → silver →
        bronze walk from there.

        Args:
            subject_uris: e.g. ["http://mesh.local/data/patient/12345"]
            gold_table:   e.g. "gold_patient"
            id_column:    the gold column that holds the URI id value,
                          e.g. "subject_id" or "_bronze_source_ref"
        """
        # Parse URIs → (uri, id_value)
        parsed: list[tuple[str, str]] = []
        for uri in subject_uris:
            id_value = self._parse_uri_id(uri)
            parsed.append((uri, id_value))

        if not parsed:
            return []

        id_values = [p[1] for p in parsed]

        # Batch-fetch gold rows. UriFactory.individual() hashes the id value
        # (sha256(str(value))[:16]), so a subject URI's last segment is that hash,
        # NOT the raw id_column value. We resolve by BOTH: the fast indexed path
        # for any raw-id URIs, then a hash-aware fetch for the (production) hashed
        # remainder. val_to_row is keyed by raw value AND hash so either shape joins.
        gold_rows = self._fetch_gold_by_id_or_hash(gold_table, id_column, id_values)

        # Build URI → gold_row mapping
        val_to_row: dict[str, dict] = {}
        for row in gold_rows:
            raw = row.get(id_column)
            val_to_row[str(raw)] = row
            val_to_row[hash_identifier(raw)] = row

        # Collect silver refs from all matched gold rows
        all_silver_refs = self._collect_silver_refs(list(gold_rows))
        all_silver_rows = self._fetch_all_silver(all_silver_refs)
        all_bronze = self._fetch_all_bronze(all_silver_rows)

        chains: list[LineageChain] = []
        for uri, id_value in parsed:
            gold_row = val_to_row.get(id_value)
            if gold_row is None:
                chains.append(self._broken_chain(
                    subject_uri=uri,
                    row_id=None,
                    gold_table=gold_table,
                    reason=f"no gold row in {gold_table} where {id_column}={id_value!r}",
                ))
                continue
            chains.append(self._build_chain(
                gold_row, gold_table, all_silver_rows, all_bronze,
                subject_uri=uri,
            ))

        return chains

    def walk_gold(
        self,
        gold_table: str,
        gold_ids: List[int],
    ) -> List[LineageChain]:
        """Walk from gold row IDs (_gold_id) → silver → bronze."""
        if not gold_ids:
            return []

        gold_rows = self._fetch_by_column(self._gold, gold_table, "_gold_id", [str(i) for i in gold_ids])
        gold_by_id: dict[int, dict] = {int(r["_gold_id"]): r for r in gold_rows}

        all_silver_refs = self._collect_silver_refs(list(gold_rows))
        all_silver_rows = self._fetch_all_silver(all_silver_refs)
        all_bronze = self._fetch_all_bronze(all_silver_rows)

        chains: list[LineageChain] = []
        for gid in gold_ids:
            gold_row = gold_by_id.get(gid)
            if gold_row is None:
                chains.append(self._broken_chain(
                    subject_uri=None,
                    row_id=gid,
                    gold_table=gold_table,
                    reason=f"gold row _gold_id={gid} not found in {gold_table}",
                ))
                continue
            chains.append(self._build_chain(
                gold_row, gold_table, all_silver_rows, all_bronze,
                row_id=gid,
            ))

        return chains

    def walk_silver(
        self,
        silver_table: str,
        silver_ids: List[int],
    ) -> List[LineageChain]:
        """Walk from silver row IDs (_silver_id) → bronze.  One hop only."""
        if not silver_ids:
            return []

        silver_rows = self._fetch_by_column(
            self._silver, silver_table, "_silver_id", [str(i) for i in silver_ids]
        )
        silver_by_id: dict[int, dict] = {int(r["_silver_id"]): r for r in silver_rows}

        all_silver_rows: dict[str, dict[int, dict]] = {
            silver_table: silver_by_id
        }
        all_bronze = self._fetch_all_bronze(all_silver_rows)

        chains: list[LineageChain] = []
        for sid in silver_ids:
            s_row = silver_by_id.get(sid)
            if s_row is None:
                chains.append(LineageChain(
                    subject_uri=None, row_id=sid,
                    gold=GoldRecord(table="", id=0, fields_used=[]),
                    silver=[SilverRecord(silver_table, sid, [])],
                    bronze=[],
                    broken=True,
                    broken_reason=f"silver row _silver_id={sid} not found in {silver_table}",
                ))
                continue

            s_vals = {k: v for k, v in s_row.items() if k not in _SILVER_AUDIT_COLS and v is not None}
            silver_rec = SilverRecord(silver_table, sid, [], s_vals)
            bronze_recs, broken, reason = self._resolve_bronze(
                s_row, all_bronze
            )
            chains.append(LineageChain(
                subject_uri=None, row_id=sid,
                gold=GoldRecord(table="", id=0, fields_used=[]),
                silver=[silver_rec],
                bronze=bronze_recs,
                broken=broken,
                broken_reason=reason,
            ))

        return chains

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_uri_id(self, uri: str) -> str:
        """Extract the id_value from a subject URI."""
        path = uri
        if path.startswith(self._base):
            path = path[len(self._base):]
        if self._source_prefix and path.startswith(self._source_prefix):
            path = path[len(self._source_prefix):]
        # path = "{entity_name}/{id_value}"
        parts = path.split("/", 1)
        return urllib.parse.unquote(parts[1]) if len(parts) > 1 else urllib.parse.unquote(parts[0])

    @staticmethod
    def _q(name: str, dialect: str) -> str:
        """Quote an identifier for the given dialect."""
        if dialect == "mysql":
            return f"`{name}`"
        return f'"{name}"'

    def _fetch_by_column(
        self,
        engine: Engine,
        table: str,
        column: str,
        values: list[str],
    ) -> list[dict]:
        """SELECT * FROM table WHERE column IN (values) — batched."""
        if not values:
            return []
        dialect = engine.dialect.name
        qt = self._q(table, dialect)
        qc = self._q(column, dialect)
        # Use named params to avoid injection; SQLAlchemy handles the list.
        params = {f"v{i}": v for i, v in enumerate(values)}
        placeholders = ", ".join(f":v{i}" for i in range(len(values)))
        stmt = text(f"SELECT * FROM {qt} WHERE {qc} IN ({placeholders})")
        try:
            with engine.connect() as conn:
                rows = conn.execute(stmt, params).fetchall()
                return [dict(r._mapping) for r in rows]
        except Exception as exc:
            logger.warning("LineageWalker: query failed for %s: %s", table, exc)
            return []

    def _fetch_gold_by_id_or_hash(
        self,
        table: str,
        id_column: str,
        id_values: list[str],
    ) -> list[dict]:
        """Fetch gold rows whose id_column matches id_values as a raw value OR as
        hash_identifier(raw value).

        Subject URIs encode sha256(str(raw))[:16], so the parsed id is usually a
        hash. We try the direct (indexed) match first — this covers un-hashed URIs
        and is a cheap no-op otherwise — then resolve any unmatched ids by hash.
        """
        if not id_values:
            return []
        rows = self._fetch_by_column(self._gold, table, id_column, id_values)
        matched = {str(r.get(id_column)) for r in rows}
        unresolved = [v for v in id_values if v not in matched]
        if unresolved:
            rows = rows + self._fetch_by_hashed_column(
                self._gold, table, id_column, unresolved
            )
        return rows

    def _fetch_by_hashed_column(
        self,
        engine: Engine,
        table: str,
        column: str,
        hashes: list[str],
    ) -> list[dict]:
        """Fetch rows where hash_identifier(column) is in `hashes`.

        Mirrors hash_identifier (sha256(str(v).strip())[:16]) in SQL on MySQL for
        an efficient filter; falls back to a portable scan-and-hash for other
        dialects (e.g. SQLite in tests). Correctness is guaranteed regardless
        because the caller re-keys results by the Python hash_identifier.
        """
        wanted = list(dict.fromkeys(hashes))
        if not wanted:
            return []
        dialect = engine.dialect.name
        qt = self._q(table, dialect)
        qc = self._q(column, dialect)
        if dialect == "mysql":
            params = {f"h{i}": h for i, h in enumerate(wanted)}
            placeholders = ", ".join(f":h{i}" for i in range(len(wanted)))
            stmt = text(
                f"SELECT * FROM {qt} "
                f"WHERE LEFT(SHA2(TRIM(CAST({qc} AS CHAR)), 256), 16) IN ({placeholders})"
            )
            try:
                with engine.connect() as conn:
                    return [dict(r._mapping) for r in conn.execute(stmt, params).fetchall()]
            except Exception as exc:
                logger.warning("LineageWalker: hashed query failed for %s: %s", table, exc)
                # fall through to the portable scan
        # Portable fallback: scan the table and hash each candidate in Python.
        wanted_set = set(wanted)
        try:
            with engine.connect() as conn:
                out: list[dict] = []
                for r in conn.execute(text(f"SELECT * FROM {qt}")):
                    row = dict(r._mapping)
                    if hash_identifier(row.get(column)) in wanted_set:
                        out.append(row)
                return out
        except Exception as exc:
            logger.warning("LineageWalker: hashed scan failed for %s: %s", table, exc)
            return []

    @staticmethod
    def _collect_silver_refs(gold_rows: list[dict]) -> dict[str, list[int]]:
        """Merge _silver_refs from multiple gold rows → {silver_table: [ids]}."""
        merged: dict[str, list[int]] = {}
        for row in gold_rows:
            refs_raw = row.get("_silver_refs") or "{}"
            try:
                refs: dict = json.loads(refs_raw)
            except (json.JSONDecodeError, TypeError):
                continue
            for s_table, s_ids in refs.items():
                merged.setdefault(s_table, []).extend(int(i) for i in s_ids if i is not None)
        return merged

    def _fetch_all_silver(
        self, silver_refs: dict[str, list[int]]
    ) -> dict[str, dict[int, dict]]:
        """Batch-fetch silver rows. Returns {silver_table: {_silver_id: row}}."""
        result: dict[str, dict[int, dict]] = {}
        for s_table, s_ids in silver_refs.items():
            if not s_ids:
                continue
            rows = self._fetch_by_column(
                self._silver, s_table, "_silver_id", [str(i) for i in s_ids]
            )
            result[s_table] = {int(r["_silver_id"]): r for r in rows}
        return result

    def _fetch_all_bronze(
        self, silver_rows: dict[str, dict[int, dict]]
    ) -> dict[str, dict[tuple, dict]]:
        """Batch-fetch bronze rows. Returns {bronze_table: {(pk_col, pk_val): row}}."""
        # Separate real-column refs from positional _idx fallbacks
        groups: dict[tuple[str, str], list[str]] = {}
        idx_groups: dict[str, list[int]] = {}   # bronze_table → [row offsets]
        for s_table_rows in silver_rows.values():
            for s_row in s_table_rows.values():
                ref = s_row.get("_bronze_source_ref", "")
                if not ref:
                    continue
                parsed = _parse_bronze_ref(ref)
                if parsed is None:
                    continue
                b_table, b_col, b_val = parsed
                if b_col == "_idx":
                    try:
                        idx_groups.setdefault(b_table, []).append(int(b_val))
                    except ValueError:
                        pass
                else:
                    groups.setdefault((b_table, b_col), []).append(b_val)

        result: dict[str, dict[tuple, dict]] = {}

        # Real-column batch fetch
        for (b_table, b_col), b_vals in groups.items():
            rows = self._fetch_by_column(self._bronze, b_table, b_col, b_vals)
            table_map = result.setdefault(b_table, {})
            for row in rows:
                key = (b_col, str(row.get(b_col, "")))
                table_map[key] = row

        # Positional _idx fetch: SELECT * … LIMIT 1 OFFSET N for each index.
        # This works for any table regardless of PK column naming conventions.
        # Safe only when the source table is stable (append-only bronze tables).
        for b_table, offsets in idx_groups.items():
            dialect = self._bronze.dialect.name
            qt = self._q(b_table, dialect)
            table_map = result.setdefault(b_table, {})
            for offset in offsets:
                try:
                    with self._bronze.connect() as conn:
                        rows = conn.execute(
                            text(f"SELECT * FROM {qt} LIMIT 1 OFFSET :off"),
                            {"off": offset},
                        ).fetchall()
                    if rows:
                        row = dict(rows[0]._mapping)
                        table_map[("_idx", str(offset))] = row
                except Exception as exc:
                    logger.warning(
                        "LineageWalker: positional _idx fetch failed for %s[%s]: %s",
                        b_table, offset, exc,
                    )

        return result

    def _build_chain(
        self,
        gold_row: dict,
        gold_table: str,
        all_silver_rows: dict[str, dict[int, dict]],
        all_bronze: dict[str, dict[tuple, dict]],
        subject_uri: Optional[str] = None,
        row_id: Optional[int] = None,
    ) -> LineageChain:
        gold_id = int(gold_row.get("_gold_id", 0))
        fields_used = [k for k in gold_row if k not in _AUDIT_COLS]
        gold_vals = {k: v for k, v in gold_row.items() if k not in _AUDIT_COLS and v is not None}
        gold_rec = GoldRecord(gold_table, gold_id, fields_used, gold_vals)

        refs_raw = gold_row.get("_silver_refs") or "{}"
        try:
            refs: dict = json.loads(refs_raw)
        except (json.JSONDecodeError, TypeError):
            refs = {}

        silver_recs: list[SilverRecord] = []
        bronze_recs: list[BronzeRecord] = []
        broken = False
        broken_reason: Optional[str] = None

        for s_table, s_ids in refs.items():
            for s_id in s_ids:
                s_id = int(s_id)
                s_row = (all_silver_rows.get(s_table) or {}).get(s_id)
                if s_row is None:
                    broken = True
                    broken_reason = f"silver row {s_table}._silver_id={s_id} not found"
                    silver_recs.append(SilverRecord(s_table, s_id, []))
                    continue

                s_vals = {k: v for k, v in s_row.items() if k not in _SILVER_AUDIT_COLS and v is not None}
                silver_recs.append(SilverRecord(s_table, s_id, [], s_vals))

                b_recs, b_broken, b_reason = self._resolve_bronze(s_row, all_bronze)
                bronze_recs.extend(b_recs)
                if b_broken and not broken:
                    broken = True
                    broken_reason = b_reason

        return LineageChain(
            subject_uri=subject_uri,
            row_id=row_id,
            gold=gold_rec,
            silver=silver_recs,
            bronze=bronze_recs,
            broken=broken,
            broken_reason=broken_reason,
        )

    @staticmethod
    def _resolve_bronze(
        s_row: dict,
        all_bronze: dict[str, dict[tuple, dict]],
    ) -> tuple[list[BronzeRecord], bool, Optional[str]]:
        """Resolve one silver row's _bronze_source_ref to BronzeRecord(s)."""
        ref = s_row.get("_bronze_source_ref", "")
        if not ref:
            return [], True, "silver row has no _bronze_source_ref"

        parsed = _parse_bronze_ref(ref)
        if parsed is None:
            return [], True, f"malformed _bronze_source_ref: {ref!r}"

        b_table, b_col, b_val = parsed
        key = (b_col, b_val)
        b_row = (all_bronze.get(b_table) or {}).get(key)
        if b_row is None:
            return [], True, f"bronze row {ref!r} not found"

        raw_values = {k: v for k, v in b_row.items() if k != b_col}
        return [BronzeRecord(table=b_table, pk={b_col: b_val}, raw_values=raw_values)], False, None

    @staticmethod
    def _broken_chain(
        subject_uri: Optional[str],
        row_id: Optional[int],
        gold_table: str,
        reason: str,
    ) -> LineageChain:
        return LineageChain(
            subject_uri=subject_uri,
            row_id=row_id,
            gold=GoldRecord(gold_table, 0, []),
            silver=[],
            bronze=[],
            broken=True,
            broken_reason=reason,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_bronze_ref(ref: str) -> Optional[tuple[str, str, str]]:
    """Parse 'raw_admissions.row_id=12345' → ('raw_admissions', 'row_id', '12345').

    Returns None for malformed refs.
    """
    try:
        dot = ref.index(".")
        eq = ref.index("=", dot)
        return ref[:dot], ref[dot + 1: eq], ref[eq + 1:]
    except ValueError:
        return None
