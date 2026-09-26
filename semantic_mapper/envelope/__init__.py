# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Answer-envelope layer (FP-1).

Every query, from any surface (web app Query tab, MCP client), returns a single
``AnswerEnvelope``: the tabular answer, a plain-language ``Receipt`` describing how
it was produced, and a stable ``receipt_url``.  The envelope is *assembled from the
existing proof stack* (ProofRecord v1 lineage + v2 semantic + ProofPresentation) —
it does not re-derive provenance, it re-frames it for a non-technical reader.

Public API::

    from semantic_mapper.envelope import build_envelope, AnswerEnvelope

Design contract: the envelope is dataset-agnostic. Every field is populated from
signals the pipeline already emits for any schema; nothing here encodes knowledge
of a specific dataset's tables or columns (see CONTRIBUTING.md).
"""
from __future__ import annotations

from semantic_mapper.envelope.models import (
    ENVELOPE_VERSION,
    Answer,
    AnswerEnvelope,
    Caveat,
    EvidenceBlock,
    FunnelStep,
    Receipt,
    SourceRef,
    TermReading,
)
from semantic_mapper.envelope.builder import build_envelope, receipt_url_for, row_set_handle_for
from semantic_mapper.envelope.render import render_receipt

__all__ = [
    "ENVELOPE_VERSION",
    "Answer",
    "AnswerEnvelope",
    "Caveat",
    "EvidenceBlock",
    "FunnelStep",
    "Receipt",
    "SourceRef",
    "TermReading",
    "build_envelope",
    "receipt_url_for",
    "row_set_handle_for",
    "render_receipt",
]
