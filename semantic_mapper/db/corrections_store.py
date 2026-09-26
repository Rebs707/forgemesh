# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
CorrectionsStore — SQLite-backed learning loop for confirmed mappings.

After each successful pipeline run, all CONFIRMED column mappings are
persisted to corrections.db.  On the next run, AnnotationMatcher queries
the store on startup; columns with prior confirmed mappings receive high
scores immediately, skipping OWL annotation scanning and LLM entirely.

Pattern-based matching (table-name-agnostic)
--------------------------------------------
Lookups are NOT keyed on (table, column) pairs.  Instead, the store builds
a PatternLookup from every confirmed column name across all tables, enabling
cross-table reuse.  Priority order:

  1  Exact column name (case-insensitive)         → score 1.00
  2  Normalised name (abbreviation-expanded)       → score 0.97
  4* Type + ≥2 meaningful token overlap            → score 0.90
  3  ≥2 meaningful token overlap                   → score 0.85–0.95

  * Priority 4 is evaluated before Priority 3 because its fixed score
    (0.90) exceeds Priority 3's minimum (0.85).  All type-matching
    candidates are collected before picking the best (score-and-compare),
    so a single shared high-frequency token cannot carry a HUMAN correction
    from one column onto an unrelated column.

Abbreviation expansion
----------------------
Abbreviations are stored in the ``abbreviations`` table (same DB) rather than
hardcoded in source.  The table is seeded with universal tokens on first run.
Domain-specific abbreviations (e.g. clinical, financial) can be added via
``CorrectionsStore.add_abbreviation()`` or directly in the database.

Schema
------
corrections
  id               INTEGER PK AUTOINCREMENT
  source_table     TEXT    source table name (e.g. "raw_party")
  source_column    TEXT    source column name (e.g. "party_id")
  source_data_type TEXT    inferred SQL type (e.g. "VARCHAR")   ← added v2
  confirmed_uri    TEXT    winning ontology property URI
  rejected_uris    TEXT    JSON array of other candidate URIs from that run
  confirmation_type TEXT   "AUTO" | "HUMAN"
  confirmed_by     TEXT    comma-separated strategy names that agreed
  confidence_score REAL    final combined score at confirmation
  evidence_json    TEXT    JSON array of full MappingEvidence dicts
  ontology_source  TEXT    path/name of the OWL file used
  db_source        TEXT    database name / connection identifier
  created_at       TEXT    ISO-8601 UTC timestamp
  run_id           TEXT    UUID identifying the pipeline run

abbreviations
  token      TEXT    PK   short token to expand (e.g. "dob")
  expansion  TEXT         full phrase (e.g. "date of birth")
  source     TEXT         "seed" | "user" | "llm"
  created_at TEXT         ISO-8601 UTC timestamp
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from semantic_mapper.schema.mapping_output import MappingStatus, SemanticMappingReport

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS corrections (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    source_table      TEXT    NOT NULL,
    source_column     TEXT    NOT NULL,
    source_data_type  TEXT,
    confirmed_uri     TEXT    NOT NULL,
    rejected_uris     TEXT,
    confirmation_type TEXT    NOT NULL DEFAULT 'AUTO',
    confirmed_by      TEXT,
    confidence_score  REAL,
    evidence_json     TEXT,
    ontology_source   TEXT,
    db_source         TEXT,
    created_at        TEXT,
    run_id            TEXT
);
CREATE INDEX IF NOT EXISTS idx_corrections_lookup
    ON corrections (source_column, ontology_source, db_source);

CREATE TABLE IF NOT EXISTS value_mappings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_table    TEXT    NOT NULL,
    source_column   TEXT    NOT NULL,
    rules_json      TEXT    NOT NULL,
    ontology_source TEXT    NOT NULL,
    db_source       TEXT    NOT NULL,
    created_at      TEXT,
    run_id          TEXT
);
CREATE INDEX IF NOT EXISTS idx_value_mappings_lookup
    ON value_mappings (source_table, source_column, ontology_source, db_source);

CREATE TABLE IF NOT EXISTS abbreviations (
    token      TEXT    NOT NULL,
    expansion  TEXT    NOT NULL,
    source     TEXT    NOT NULL DEFAULT 'seed',
    created_at TEXT,
    PRIMARY KEY (token)
);
"""

# ---------------------------------------------------------------------------
# Abbreviation seed data
# ---------------------------------------------------------------------------
# Universal abbreviations seeded into the DB on first run.
# Domain-specific abbreviations belong in the database (added via
# add_abbreviation()), not here.  Keep this list generic.

_ABBREV_SEEDS: Dict[str, str] = {
    "nm":   "name",
    "dt":   "date",
    "amt":  "amount",
    "cd":   "code",
    "ref":  "reference",
    "id":   "identifier",
    "addr": "address",
    "stat": "status",
    "cat":  "category",
    "upd":  "updated",
    "src":  "source",
    "rel":  "relationship",
    "org":  "organisation",
    "cust": "customer",
    "agr":  "agreement",
    "prem": "premium",
    "curr": "currency",
    "lon":  "longitude",
    "lat":  "latitude",
    "num":  "number",
    "seq":  "sequence",
    "desc": "description",
    "val":  "value",
}

_STOP_TOKENS: FrozenSet[str] = frozenset(
    {"raw", "the", "a", "an", "of", "in", "by", "for", "and", "or"}
)


# ---------------------------------------------------------------------------
# Abbreviation expansion + tokenisation helpers
# ---------------------------------------------------------------------------

def _normalise(col_name: str, abbrev_map: Optional[Dict[str, str]] = None) -> str:
    """
    Lowercase, split on underscores/hyphens/spaces, expand abbreviations,
    then rejoin with spaces.

    Parameters
    ----------
    col_name:
        Raw column name, e.g. ``"full_nm"`` or ``"dob"``.
    abbrev_map:
        Abbreviation dictionary to use.  When omitted, falls back to
        ``_ABBREV_SEEDS`` (universal tokens only).  Pass the DB-loaded map
        from ``CorrectionsStore`` to include domain-specific expansions.

    Examples
    --------
    >>> _normalise("full_nm")
    'full name'
    >>> _normalise("email_addr")
    'email address'
    """
    effective = abbrev_map if abbrev_map is not None else _ABBREV_SEEDS
    parts: List[str] = []
    for tok in re.split(r"[_\-\s]+", col_name.lower()):
        if not tok:
            continue
        parts.append(effective.get(tok, tok))
    return " ".join(parts)


def _tokens(col_name: str, abbrev_map: Optional[Dict[str, str]] = None) -> FrozenSet[str]:
    """
    Return the set of meaningful tokens for overlap matching.

    Tokens are derived from the normalised name; stop-words and
    single-character tokens are filtered out.

    Examples
    --------
    >>> _tokens("raw_party_id")
    frozenset({'party', 'identifier'})
    >>> _tokens("email_addr")
    frozenset({'email', 'address'})
    """
    norm = _normalise(col_name, abbrev_map)
    return frozenset(
        tok
        for tok in norm.split()
        if tok not in _STOP_TOKENS and len(tok) > 1
    )


# ---------------------------------------------------------------------------
# Pattern dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _ConfirmedPattern:
    column_name: str            # raw column name, e.g. "party_id"
    normalised_name: str        # expanded form, e.g. "party identifier"
    tokens: FrozenSet[str]      # meaningful token set, e.g. frozenset({"party", "identifier"})
    source_data_type: Optional[str]   # SQL base type, e.g. "VARCHAR"
    confirmed_uri: str          # winning ontology property URI
    confirmation_count: int     # number of times confirmed across all tables
    confirmation_type: str      # "HUMAN" if any entry is human-confirmed, else "AUTO"
    best_confidence_score: float  # MAX(confidence_score) across all confirmed runs


# ---------------------------------------------------------------------------
# PatternLookup (exported — used by AnnotationMatcher)
# ---------------------------------------------------------------------------

class PatternLookup:
    """
    In-memory index of confirmed column → URI patterns built from the
    corrections store.  Supports four priority levels of fuzzy matching.

    Parameters
    ----------
    patterns:
        List of ``_ConfirmedPattern`` objects, typically loaded via
        ``CorrectionsStore.load_patterns()``.
    abbrev_map:
        Abbreviation dictionary used for normalising query column names in
        ``match()``.  Should be the same map used when building *patterns*
        so that normalised forms are consistent.
    """

    def __init__(
        self,
        patterns: List[_ConfirmedPattern],
        abbrev_map: Optional[Dict[str, str]] = None,
    ) -> None:
        self._patterns = patterns
        self._abbrev_map = abbrev_map or {}

        # Priority 1 index: lowercase column_name → list[pattern]
        self._exact: Dict[str, List[_ConfirmedPattern]] = {}
        # Priority 2 index: normalised_name → list[pattern]
        self._normalised: Dict[str, List[_ConfirmedPattern]] = {}

        for p in patterns:
            self._exact.setdefault(p.column_name.lower(), []).append(p)
            self._normalised.setdefault(p.normalised_name, []).append(p)

    def __len__(self) -> int:
        return len(self._patterns)

    def match(
        self,
        col_name: str,
        sql_type: Optional[str] = None,
        human_only: bool = False,
    ) -> Optional[Tuple[str, float, str]]:
        """
        Return ``(confirmed_uri, score, rationale)`` or ``None`` if no match.

        Parameters
        ----------
        col_name:
            Source column name to look up.
        sql_type:
            Optional SQL type string for type+token priority matching.
        human_only:
            When True, only return a result if the matched pattern is
            HUMAN-confirmed.  AUTO-confirmed patterns are silently skipped.
            Use this when the caller should act only on explicit human
            overrides and not cached pipeline AUTO confirmations.

        Priority order
        --------------
        1  Exact case-insensitive column name → 1.00
        2  Normalised (abbreviation-expanded) name → 0.97
        4* SQL type matches AND ≥2 token overlap → 0.90
        3  ≥2 meaningful token overlap → 0.85–0.95 (scaled by confirmation_count)

        * Priority 4 is tested before Priority 3 because its fixed score
          (0.90) exceeds Priority 3's minimum (0.85).  All candidates are
          collected before picking the best (score-and-compare) so that a
          single shared token cannot carry a correction across unrelated
          columns.
        """
        col_lower = col_name.lower()
        col_norm = _normalise(col_name, self._abbrev_map)
        col_tokens = _tokens(col_name, self._abbrev_map)
        sql_norm = (sql_type or "").upper().split("(")[0].strip()

        # ---- Priority 1: exact match ----------------------------------------
        if col_lower in self._exact:
            best = max(self._exact[col_lower], key=lambda p: p.confirmation_count)
            if human_only and best.confirmation_type != "HUMAN":
                pass  # skip — caller only wants human overrides
            elif best.confirmation_type == "HUMAN":
                return (best.confirmed_uri, 1.0, f"Human-confirmed exact match: '{col_name}'")
            else:
                return (
                    best.confirmed_uri,
                    best.best_confidence_score,
                    f"AUTO-confirmed exact match: '{col_name}' "
                    f"({best.confirmation_count}\u00d7 pipeline run(s))",
                )

        # ---- Priority 2: normalised name match ------------------------------
        if col_norm in self._normalised:
            best = max(
                self._normalised[col_norm], key=lambda p: p.confirmation_count
            )
            if human_only and best.confirmation_type != "HUMAN":
                pass  # skip — caller only wants human overrides
            elif best.confirmation_type == "HUMAN":
                return (
                    best.confirmed_uri,
                    1.0,
                    f"Human-confirmed normalised match: '{col_name}' \u2192 '{col_norm}'",
                )
            else:
                return (
                    best.confirmed_uri,
                    best.best_confidence_score,
                    f"AUTO-confirmed normalised match: "
                    f"'{col_name}' \u2192 '{col_norm}' "
                    f"({best.confirmation_count}\u00d7 pipeline run(s))",
                )

        # ---- Priorities 3 & 4: collect candidates, return best --------------
        candidates: List[Tuple[float, str, str]] = []  # (score, uri, rationale)

        # Priority 4: type + ≥2 token overlap (score-and-compare: all candidates collected)
        if sql_norm and col_tokens:
            for p in self._patterns:
                if human_only and p.confirmation_type != "HUMAN":
                    continue
                if not p.source_data_type:
                    continue
                p_sql = p.source_data_type.upper().split("(")[0].strip()
                if p_sql != sql_norm:
                    continue
                overlap = col_tokens & p.tokens
                if len(overlap) >= 2:
                    score = 1.0 if p.confirmation_type == "HUMAN" else p.best_confidence_score
                    prefix = "Human" if p.confirmation_type == "HUMAN" else "AUTO"
                    candidates.append((
                        score,
                        p.confirmed_uri,
                        (
                            f"{prefix}-confirmed type+token match: "
                            f"type={sql_norm}, tokens={sorted(overlap)} "
                            f"with '{p.column_name}' "
                            f"({p.confirmation_count}\u00d7 pipeline run(s))"
                        ),
                    ))

        # Priority 3: ≥2 token overlap
        if len(col_tokens) >= 2:
            for p in self._patterns:
                if human_only and p.confirmation_type != "HUMAN":
                    continue
                if len(p.tokens) < 2:
                    continue
                overlap = col_tokens & p.tokens
                if len(overlap) >= 2:
                    score = 1.0 if p.confirmation_type == "HUMAN" else p.best_confidence_score
                    prefix = "Human" if p.confirmation_type == "HUMAN" else "AUTO"
                    candidates.append((
                        score,
                        p.confirmed_uri,
                        (
                            f"{prefix}-confirmed token match: '{col_name}' "
                            f"overlaps {sorted(overlap)} with '{p.column_name}' "
                            f"({p.confirmation_count}\u00d7 pipeline run(s))"
                        ),
                    ))

        if candidates:
            score, uri, rationale = max(candidates, key=lambda c: c[0])
            return (uri, score, rationale)

        return None


# ---------------------------------------------------------------------------
# CorrectionsStore
# ---------------------------------------------------------------------------

class CorrectionsStore:
    """
    Thin wrapper around a local SQLite database for confirmed mappings
    and abbreviation expansions.

    Parameters
    ----------
    db_path:
        Path to the SQLite file.  Created automatically if it does not exist.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._init_db()
        self._abbrev_map: Dict[str, str] = self._load_abbreviations()

    # ------------------------------------------------------------------
    # Initialisation + schema migration
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(_SCHEMA)
            # v2 migration: add source_data_type column if absent
            existing_cols = {
                row[1] for row in conn.execute("PRAGMA table_info(corrections)")
            }
            if "source_data_type" not in existing_cols:
                conn.execute(
                    "ALTER TABLE corrections ADD COLUMN source_data_type TEXT"
                )
                logger.info(
                    "CorrectionsStore: schema migrated — added source_data_type column"
                )
            # Seed abbreviations if the table is empty
            count = conn.execute("SELECT COUNT(*) FROM abbreviations").fetchone()[0]
            if count == 0:
                created_at = datetime.now(timezone.utc).isoformat()
                conn.executemany(
                    "INSERT OR IGNORE INTO abbreviations (token, expansion, source, created_at) "
                    "VALUES (?, ?, 'seed', ?)",
                    [(token, expansion, created_at) for token, expansion in _ABBREV_SEEDS.items()],
                )
                logger.info(
                    "CorrectionsStore: seeded %d abbreviation(s)", len(_ABBREV_SEEDS)
                )
        logger.debug("CorrectionsStore: initialised at %s", self.db_path)

    # ------------------------------------------------------------------
    # Abbreviations
    # ------------------------------------------------------------------

    def _load_abbreviations(self) -> Dict[str, str]:
        """Load all abbreviation expansions from the DB into a dict."""
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT token, expansion FROM abbreviations"
            ).fetchall()
        abbrev_map = {token: expansion for token, expansion in rows}
        logger.debug(
            "CorrectionsStore: loaded %d abbreviation(s)", len(abbrev_map)
        )
        return abbrev_map

    def add_abbreviation(
        self,
        token: str,
        expansion: str,
        source: str = "user",
    ) -> None:
        """
        Add or replace an abbreviation expansion.

        Parameters
        ----------
        token:
            Short token to expand, e.g. ``"dob"``.
        expansion:
            Full phrase, e.g. ``"date of birth"``.
        source:
            Origin of the entry: ``"seed"`` | ``"user"`` | ``"llm"``.
        """
        created_at = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO abbreviations (token, expansion, source, created_at) "
                "VALUES (?, ?, ?, ?)",
                (token.lower(), expansion.lower(), source, created_at),
            )
        self._abbrev_map[token.lower()] = expansion.lower()
        logger.info(
            "CorrectionsStore: abbreviation added/updated: %s → %s (source=%s)",
            token, expansion, source,
        )

    def list_abbreviations(self) -> List[Dict]:
        """Return all abbreviations as a list of dicts."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT token, expansion, source, created_at FROM abbreviations ORDER BY token"
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_abbreviation(self, token: str) -> bool:
        """Delete an abbreviation by token. Returns True if a row was deleted."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM abbreviations WHERE token = ?", (token.lower(),)
            )
        deleted = cursor.rowcount > 0
        if deleted:
            self._abbrev_map.pop(token.lower(), None)
            logger.info("CorrectionsStore: abbreviation deleted: %s", token)
        return deleted

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def load_patterns(
        self,
        ontology_source: str,
        db_source: str,
    ) -> PatternLookup:
        """
        Build a ``PatternLookup`` from all confirmed mappings for the given
        ontology + database pair.

        Patterns are aggregated by (source_column, confirmed_uri, source_data_type)
        across ALL tables, so the lookup is table-name-agnostic.  The
        ``confirmation_count`` reflects how many times that column name was
        confirmed with that URI regardless of which table it appeared in.

        Abbreviation expansion uses the DB-loaded map (``self._abbrev_map``)
        so that domain-specific tokens added after the seed are applied.

        Parameters
        ----------
        ontology_source:
            Identifier for the ontology (e.g. the OWL file path).
        db_source:
            Identifier for the source database (e.g. "MySQL_Bronze").

        Returns
        -------
        PatternLookup
            Ready-to-query index.  ``len(lookup)`` gives the pattern count.
        """
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT   source_column,
                         confirmed_uri,
                         source_data_type,
                         COUNT(*) AS confirmation_count,
                         MAX(CASE WHEN confirmation_type = 'HUMAN' THEN 1 ELSE 0 END)
                             AS has_human,
                         MAX(COALESCE(confidence_score, 0.0)) AS best_confidence_score
                FROM     corrections
                WHERE    ontology_source = ? AND db_source = ?
                GROUP BY source_column, confirmed_uri, source_data_type
                ORDER BY confirmation_count DESC
                """,
                (ontology_source, db_source),
            ).fetchall()

        abbrev_map = self._abbrev_map
        patterns = [
            _ConfirmedPattern(
                column_name=col_name,
                normalised_name=_normalise(col_name, abbrev_map),
                tokens=_tokens(col_name, abbrev_map),
                source_data_type=dtype,
                confirmed_uri=uri,
                confirmation_count=count,
                confirmation_type="HUMAN" if has_human else "AUTO",
                best_confidence_score=float(best_score or 0.0),
            )
            for col_name, uri, dtype, count, has_human, best_score in rows
        ]

        logger.info(
            "CorrectionsStore: loaded %d pattern(s) "
            "(ontology='%s', db='%s')",
            len(patterns),
            ontology_source,
            db_source,
        )
        return PatternLookup(patterns, abbrev_map=abbrev_map)

    def count(self, ontology_source: str, db_source: str) -> int:
        """Return the number of distinct confirmed column patterns."""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT source_column || '|' || confirmed_uri)
                FROM   corrections
                WHERE  ontology_source = ? AND db_source = ?
                """,
                (ontology_source, db_source),
            ).fetchone()
        return row[0] if row else 0

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def overwrite_human_correction(
        self,
        source_table: str,
        source_column: str,
        confirmed_uri: str,
        source_data_type: Optional[str],
        ontology_source: str,
        db_source: str,
        run_id: Optional[str] = None,
    ) -> None:
        """
        Replace ALL existing corrections store entries for this column with a single
        HUMAN-confirmed entry.  This ensures a human override wins on the next pipeline
        run regardless of how many AUTO entries previously existed.
        """
        if run_id is None:
            run_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """DELETE FROM corrections
                   WHERE source_column = ? AND ontology_source = ? AND db_source = ?""",
                (source_column, ontology_source, db_source),
            )
            conn.execute(
                """INSERT INTO corrections (
                       source_table, source_column, source_data_type,
                       confirmed_uri, rejected_uris,
                       confirmation_type, confirmed_by,
                       confidence_score, evidence_json,
                       ontology_source, db_source, created_at, run_id
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source_table, source_column, source_data_type,
                    confirmed_uri, json.dumps([]),
                    "HUMAN", "web_ui",
                    1.0, None,
                    ontology_source, db_source, created_at, run_id,
                ),
            )
        logger.info(
            "CorrectionsStore: human override %s.%s → %s",
            source_table, source_column, confirmed_uri,
        )

    def write_confirmed(
        self,
        report: SemanticMappingReport,
        ontology_source: str,
        db_source: str,
        run_id: Optional[str] = None,
    ) -> int:
        """
        Persist all CONFIRMED column mappings from ``report``.

        Parameters
        ----------
        report:
            The SemanticMappingReport produced by MapperOrchestrator.run().
        ontology_source:
            Identifier for the ontology (e.g. the OWL file path).
        db_source:
            Identifier for the source database (e.g. "MySQL_Bronze").
        run_id:
            Optional UUID string.  A new UUID is generated if not supplied.

        Returns
        -------
        int
            Number of rows written.
        """
        if run_id is None:
            run_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()

        rows = []
        for table in report.tables:
            for col in table.column_mappings:
                if col.status != MappingStatus.CONFIRMED:
                    continue
                if not col.mapped_property_uri:
                    continue

                all_uris = {
                    ev.mapped_uri
                    for ev in col.evidence_trail
                    if ev.mapped_uri
                }
                rejected = sorted(all_uris - {col.mapped_property_uri})

                confirmed_by = "AUTO"
                if col.rationale and "agreeing=[" in col.rationale:
                    try:
                        raw = col.rationale.split("agreeing=[")[1].split("]")[0]
                        confirmed_by = raw.replace("'", "").replace(" ", "")
                    except Exception:
                        pass

                rows.append((
                    table.source_table,
                    col.source_column,
                    col.source_data_type,          # new in v2
                    col.mapped_property_uri,
                    json.dumps(rejected),
                    "AUTO",
                    confirmed_by,
                    col.confidence_score,
                    json.dumps([ev.model_dump() for ev in col.evidence_trail]),
                    ontology_source,
                    db_source,
                    created_at,
                    run_id,
                ))

        if not rows:
            logger.info("CorrectionsStore: no confirmed mappings to write")
            return 0

        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO corrections (
                    source_table, source_column, source_data_type,
                    confirmed_uri, rejected_uris,
                    confirmation_type, confirmed_by,
                    confidence_score, evidence_json,
                    ontology_source, db_source, created_at, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )

        logger.info(
            "CorrectionsStore: wrote %d confirmed mapping(s) (run_id=%s)",
            len(rows),
            run_id,
        )
        return len(rows)

    # ------------------------------------------------------------------
    # Value mappings cache
    # ------------------------------------------------------------------

    def load_value_mappings(
        self,
        source_table: str,
        source_column: str,
        ontology_source: str,
        db_source: str,
    ) -> "Optional[List]":
        """
        Return cached ``ValueMappingRule`` list for a column, or ``None``
        if no entry exists in the store.
        """
        from semantic_mapper.schema.mapping_output import ValueMappingRule

        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT rules_json FROM value_mappings
                WHERE  source_table = ? AND source_column = ?
                AND    ontology_source = ? AND db_source = ?
                ORDER  BY id DESC LIMIT 1
                """,
                (source_table, source_column, ontology_source, db_source),
            ).fetchone()

        if row is None:
            return None
        try:
            raw = json.loads(row[0])
            return [ValueMappingRule.model_validate(r) for r in raw]
        except Exception as exc:
            logger.warning(
                "CorrectionsStore: failed to deserialise value_mappings for "
                "%s.%s: %s",
                source_table,
                source_column,
                exc,
            )
            return None

    def write_value_mappings(
        self,
        source_table: str,
        source_column: str,
        rules: List,
        ontology_source: str,
        db_source: str,
        run_id: Optional[str] = None,
    ) -> None:
        """
        Persist ``ValueMappingRule`` list for a column.  Duplicate entries
        are intentionally allowed; ``load_value_mappings`` always returns the
        most recently written row.
        """
        if run_id is None:
            run_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        rules_json = json.dumps([r.model_dump() for r in rules])

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO value_mappings
                    (source_table, source_column, rules_json,
                     ontology_source, db_source, created_at, run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_table,
                    source_column,
                    rules_json,
                    ontology_source,
                    db_source,
                    created_at,
                    run_id,
                ),
            )
        logger.debug(
            "CorrectionsStore: wrote value_mappings for %s.%s (%d rules)",
            source_table,
            source_column,
            len(rules),
        )

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def fetch_all(self) -> List[dict]:
        """
        Return every row in the corrections table as a list of dicts, newest first.

        Each dict includes a ``winning`` flag: True when this is the highest-count
        AUTO entry for its (source_column, ontology_source, db_source) group, or
        when the entry is HUMAN-confirmed (HUMAN always wins after overwrite).
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, source_table, source_column, source_data_type,
                       confirmed_uri, confirmation_type, confirmed_by,
                       confidence_score, evidence_json, ontology_source, db_source,
                       created_at, run_id,
                       COUNT(*) OVER (
                           PARTITION BY source_column, confirmed_uri,
                                        ontology_source, db_source
                       ) AS vote_count
                FROM corrections
                ORDER BY id DESC
                """
            ).fetchall()

        # Determine winning entry per (source_column, ontology_source, db_source)
        # HUMAN entry wins unconditionally; among AUTO entries the highest vote_count wins.
        best: Dict[Tuple[str, str, str], dict] = {}
        result = [dict(r) for r in rows]
        for row in result:
            key = (row["source_column"], row["ontology_source"], row["db_source"])
            if key not in best:
                best[key] = row
            else:
                prev = best[key]
                # HUMAN beats AUTO; higher vote_count breaks ties
                if row["confirmation_type"] == "HUMAN" and prev["confirmation_type"] != "HUMAN":
                    best[key] = row
                elif row["vote_count"] > prev.get("vote_count", 0) and prev["confirmation_type"] != "HUMAN":
                    best[key] = row

        winning_ids = {r["id"] for r in best.values()}
        for row in result:
            row["winning"] = row["id"] in winning_ids

        return result

    def delete_by_id(self, correction_id: int) -> bool:
        """Delete a single corrections row by primary key. Returns True if a row was deleted."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM corrections WHERE id = ?", (correction_id,)
            )
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info("CorrectionsStore: deleted correction id=%d", correction_id)
        return deleted

    def prune_to_winning(self) -> int:
        """
        Delete all non-winning corrections rows, keeping only the single winning
        entry per (source_column, ontology_source, db_source) group.

        Winning priority: HUMAN entry beats all AUTO; among AUTO entries the row
        with the highest COUNT(*) (most pipeline-run confirmations) is kept; ties
        broken by highest id (most recent).

        Returns the number of rows deleted.
        """
        with sqlite3.connect(self.db_path) as conn:
            # Compute vote counts separately (SQLite does not support nested window functions)
            # then rank within each (column, ontology, db) group.
            winning_ids = {
                row[0]
                for row in conn.execute(
                    """
                    WITH vote_counts AS (
                        SELECT source_column, confirmed_uri, ontology_source, db_source,
                               COUNT(*) AS vote_count
                        FROM   corrections
                        GROUP  BY source_column, confirmed_uri, ontology_source, db_source
                    ),
                    ranked AS (
                        SELECT c.id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY c.source_column, c.ontology_source, c.db_source
                                   ORDER BY
                                       CASE c.confirmation_type WHEN 'HUMAN' THEN 1 ELSE 2 END,
                                       COALESCE(v.vote_count, 0) DESC,
                                       c.id DESC
                               ) AS rn
                        FROM   corrections c
                        LEFT JOIN vote_counts v
                               ON  c.source_column    = v.source_column
                               AND c.confirmed_uri    = v.confirmed_uri
                               AND c.ontology_source  = v.ontology_source
                               AND c.db_source        = v.db_source
                    )
                    SELECT id FROM ranked WHERE rn = 1
                    """
                )
            }
            if not winning_ids:
                return 0
            placeholders = ",".join("?" * len(winning_ids))
            cursor = conn.execute(
                f"DELETE FROM corrections WHERE id NOT IN ({placeholders})",
                list(winning_ids),
            )
        deleted = cursor.rowcount
        logger.info("CorrectionsStore: pruned %d non-winning row(s)", deleted)
        return deleted

    def delete_by_ontology(self, ontology_source: str, db_source: str) -> int:
        """Delete all corrections (and value_mappings) for a specific ontology+db pair.

        Returns the number of corrections rows deleted.
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM corrections WHERE ontology_source = ? AND db_source = ?",
                (ontology_source, db_source),
            )
            deleted = cursor.rowcount
            conn.execute(
                "DELETE FROM value_mappings WHERE ontology_source = ? AND db_source = ?",
                (ontology_source, db_source),
            )
        logger.info(
            "CorrectionsStore: deleted %d correction(s) for ontology='%s', db='%s'",
            deleted, ontology_source, db_source,
        )
        return deleted

    def clear(self) -> None:
        """Delete all rows.  Used for dry-run resets."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM corrections")
            conn.execute("DELETE FROM value_mappings")
        logger.info("CorrectionsStore: all entries cleared")
