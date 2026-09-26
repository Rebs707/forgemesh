# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""BindingStore — SQLite-backed persistence for external terminology bindings.

The binding analogue of :class:`CorrectionsStore`.  Proposed bindings land as
``PENDING_REVIEW``; a human confirmation promotes one to ``CONFIRMED``.  On the
next re-materialise the binder replays CONFIRMED bindings at $0 — the same
warm-run reuse property HistoryMatcher gives column mappings.

Schema
------
bindings
  id              INTEGER PK AUTOINCREMENT
  individual_uri  TEXT    the fm individual the binding is asserted on
  raw_value       TEXT    the source value (rdfs:label) that matched
  source_id       TEXT    TERMINOLOGY_SOURCES key (e.g. "snomedct")
  system_label    TEXT    code-system name written to fm:terminologySystem
  concept_iri     TEXT    the external concept IRI (skos:exactMatch object)
  confidence      REAL    binding confidence at proposal time
  method          TEXT    "value-set-overlap" | "human-confirmed"
  status          TEXT    "PENDING_REVIEW" | "CONFIRMED"
  ontology_source TEXT    OWL file path/name the binding was derived against
  db_source       TEXT    source database identifier
  created_at      TEXT    ISO-8601 UTC timestamp
  run_id          TEXT    UUID identifying the pipeline run
"""
from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

_STATUS_PENDING = "PENDING_REVIEW"
_STATUS_CONFIRMED = "CONFIRMED"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bindings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_uri  TEXT    NOT NULL,
    raw_value       TEXT,
    source_id       TEXT,
    system_label    TEXT,
    concept_iri     TEXT    NOT NULL,
    confidence      REAL,
    method          TEXT,
    status          TEXT    NOT NULL DEFAULT 'PENDING_REVIEW',
    ontology_source TEXT,
    db_source       TEXT,
    created_at      TEXT,
    run_id          TEXT
);
CREATE INDEX IF NOT EXISTS idx_bindings_lookup
    ON bindings (ontology_source, db_source, status);
"""


class BindingStore:
    """Thin wrapper around a local SQLite database for terminology bindings."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(_SCHEMA)
        logger.debug("BindingStore: initialised at %s", self.db_path)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def load_confirmed(self, ontology_source: str, db_source: str) -> List[Dict]:
        """Return CONFIRMED bindings for an ontology+db pair, one per individual
        (most recent wins)."""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT individual_uri, raw_value, source_id, system_label,
                       concept_iri, confidence, method, status
                FROM   bindings
                WHERE  status = ? AND ontology_source = ? AND db_source = ?
                ORDER  BY id DESC
                """,
                (_STATUS_CONFIRMED, ontology_source, db_source),
            ).fetchall()
        seen: set = set()
        out: List[Dict] = []
        for r in rows:
            d = dict(r)
            if d["individual_uri"] in seen:
                continue
            seen.add(d["individual_uri"])
            out.append(d)
        logger.info(
            "BindingStore: loaded %d confirmed binding(s) (ontology=%s, db=%s)",
            len(out), ontology_source, db_source,
        )
        return out

    def count(self, ontology_source: str = "", db_source: str = "") -> int:
        with sqlite3.connect(self.db_path) as conn:
            if ontology_source or db_source:
                row = conn.execute(
                    "SELECT COUNT(*) FROM bindings WHERE ontology_source = ? AND db_source = ?",
                    (ontology_source, db_source),
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM bindings").fetchone()
        return row[0] if row else 0

    def fetch_all(self) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM bindings ORDER BY id DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def replace_proposals(
        self,
        proposals: Sequence,
        ontology_source: str,
        db_source: str,
        run_id: Optional[str] = None,
    ) -> int:
        """Replace all PENDING proposals for an ontology+db pair with *proposals*.

        CONFIRMED rows are never touched — a re-run refreshes pending proposals
        without disturbing human confirmations.  Returns rows written.
        """
        if run_id is None:
            run_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                p.individual_uri, p.raw_value, p.source_id, p.system_label,
                p.concept_iri, p.confidence, p.method, _STATUS_PENDING,
                ontology_source, db_source, created_at, run_id,
            )
            for p in proposals
        ]
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM bindings WHERE status = ? AND ontology_source = ? AND db_source = ?",
                (_STATUS_PENDING, ontology_source, db_source),
            )
            if rows:
                conn.executemany(
                    """
                    INSERT INTO bindings (
                        individual_uri, raw_value, source_id, system_label,
                        concept_iri, confidence, method, status,
                        ontology_source, db_source, created_at, run_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
        logger.info("BindingStore: replaced pending proposals — wrote %d", len(rows))
        return len(rows)

    def confirm(
        self,
        individual_uri: str,
        concept_iri: str,
        ontology_source: str,
        db_source: str,
        *,
        raw_value: str = "",
        source_id: str = "",
        system_label: str = "",
        confidence: float = 1.0,
        run_id: Optional[str] = None,
    ) -> None:
        """Persist a human-confirmed binding. Replays at $0 on the next run.

        Removes any prior rows for the same individual (in this ontology+db) so the
        confirmation is the single authority, then inserts the CONFIRMED row.
        """
        if run_id is None:
            run_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM bindings WHERE individual_uri = ? "
                "AND ontology_source = ? AND db_source = ?",
                (individual_uri, ontology_source, db_source),
            )
            conn.execute(
                """
                INSERT INTO bindings (
                    individual_uri, raw_value, source_id, system_label,
                    concept_iri, confidence, method, status,
                    ontology_source, db_source, created_at, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    individual_uri, raw_value, source_id, system_label,
                    concept_iri, confidence, "human-confirmed", _STATUS_CONFIRMED,
                    ontology_source, db_source, created_at, run_id,
                ),
            )
        logger.info(
            "BindingStore: confirmed %s → %s", individual_uri, concept_iri,
        )

    def clear(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM bindings")
        logger.info("BindingStore: all bindings cleared")
