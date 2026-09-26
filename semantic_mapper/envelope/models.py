# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""AnswerEnvelope contract (FP-1).

One JSON envelope produced per query and persisted under a run id.  Rendered by
both surfaces: the web app Query tab (FP-2) and any MCP client (FP-3).

Versioned from day one via ``ENVELOPE_VERSION`` so agents and UIs can detect a
breaking change.  Every model is frozen — an envelope is an immutable receipt.

The four receipt blocks mirror the design's plain-language steps:

    understood  — each NL term and the reading it was given
    sources     — tables, join keys, mapping confirmation dates
    evidence    — rows scanned, rows matched, a small sample, a row-set handle
    caveats     — typed, with counts
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel

ENVELOPE_VERSION = "1.0.0"


# ── Caveat kinds ──────────────────────────────────────────────────────────────
# Canonical, dataset-agnostic caveat vocabulary.  Detectors (see detectors.py) and
# the proof-caveat adapter both emit these kinds.  Each kind carries a stable
# display label for the receipt UI and a severity that drives colour emphasis.

CAVEAT_LABELS: Dict[str, str] = {
    "open_records": "OPEN RECORDS",
    "late_arrival": "LATE ARRIVAL",
    "reference_gap": "REFERENCE GAP",
    "small_group": "SMALL GROUP",
    "null_excluded": "NULL EXCLUDED",
    "quarantined": "QUARANTINED",
    "broken_lineage": "BROKEN LINEAGE",
    "sampled": "SAMPLED",
    "query_rewritten": "QUERY REWRITTEN",
}

# Kinds that warrant the warning colour and stay visible in the collapsed receipt.
WARN_CAVEAT_KINDS = frozenset(CAVEAT_LABELS) - {"sampled"}


def caveat_label(kind: str) -> str:
    """Display label for a caveat kind. Falls back to an upper-cased kind."""
    return CAVEAT_LABELS.get(kind, kind.replace("_", " ").upper())


# ── Receipt blocks ────────────────────────────────────────────────────────────

class TermReading(BaseModel, frozen=True):
    """Block 1 — one NL term and the concrete reading it was given."""

    term: str
    reading: str
    # Optional ontology URI backing the reading (present for grounded concepts).
    grounded_uri: Optional[str] = None


class SourceRef(BaseModel, frozen=True):
    """Block 2 — one table that fed the answer, with join/mapping metadata.

    All numeric/date fields are optional: the envelope populates whatever the
    proof stack surfaced for this dataset and degrades gracefully otherwise.
    """

    table: str
    layer: Optional[str] = None            # gold | silver | bronze
    rows_scanned: Optional[int] = None
    join_key: Optional[str] = None         # FK / shared key onto the *next* source
    date_column: Optional[str] = None
    mapping_confirmed: Optional[str] = None  # ISO date or confirmation state
    description: Optional[str] = None


class FunnelStep(BaseModel, frozen=True):
    """One stage of the row funnel (multi-table 'what was counted')."""

    n: int
    label: str


class EvidenceBlock(BaseModel, frozen=True):
    """Block 3 — what was counted, with a sample and a handle to the full set."""

    rows_matched: int
    rows_scanned: Optional[int] = None
    sample: List[Dict[str, Any]] = []       # up to N rows, as column→value dicts
    sample_shown: int = 0
    row_set: str = ""                        # fm://runs/{id}/rows handle
    funnel: List[FunnelStep] = []            # non-empty only for multi-stage counts


class Caveat(BaseModel, frozen=True):
    """Block 4 — one typed caveat with a count and optional note."""

    kind: str
    count: int = 0
    note: Optional[str] = None
    severity: str = "warn"                   # "warn" | "info"

    @property
    def label(self) -> str:
        return caveat_label(self.kind)


class Receipt(BaseModel, frozen=True):
    """The four-block receipt: how the answer was produced."""

    understood: List[TermReading] = []
    sources: List[SourceRef] = []
    evidence: EvidenceBlock
    caveats: List[Caveat] = []
    run_id: str
    question: str = ""                       # the natural-language question asked
    query_text: str = ""                     # the executed SPARQL/SQL
    ontology_version: Optional[str] = None
    # Provenance depth: "inline" (assembled from locally-captured signals at query
    # time) or "full" (also walked bronze lineage via prove_answer).
    depth: str = "inline"


# ── Answer + envelope ─────────────────────────────────────────────────────────

class Answer(BaseModel, frozen=True):
    """The tabular answer in columns + row-arrays form (matches the contract mock)."""

    columns: List[str] = []
    rows: List[List[Any]] = []
    row_count: int = 0

    @classmethod
    def from_result(cls, answer: Dict[str, Any]) -> "Answer":
        """Build from the stored answer dict ({rows:[dict], row_count, columns})."""
        rows_in = answer.get("rows") or []
        columns = list(answer.get("columns") or [])
        if not columns and rows_in and isinstance(rows_in[0], dict):
            columns = list(rows_in[0].keys())
        row_arrays: List[List[Any]] = []
        for r in rows_in:
            if isinstance(r, dict):
                row_arrays.append([r.get(c) for c in columns])
            elif isinstance(r, (list, tuple)):
                row_arrays.append(list(r))
            else:
                row_arrays.append([r])
        row_count = answer.get("row_count")
        if not isinstance(row_count, int):
            row_count = len(row_arrays)
        return cls(columns=columns, rows=row_arrays, row_count=row_count)


class AnswerEnvelope(BaseModel, frozen=True):
    """The single object every query returns, from every surface."""

    envelope_version: str = ENVELOPE_VERSION
    run_id: str
    answer: Answer
    receipt: Receipt
    receipt_url: str

    def model_dump_public(self) -> Dict[str, Any]:
        """Serialisation used by both HTTP and MCP surfaces."""
        return self.model_dump()
