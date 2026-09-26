# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

from pydantic import BaseModel

if TYPE_CHECKING:
    from semantic_mapper.rdf.proof.models import ProofRecord


class QueryResult(BaseModel, frozen=True):
    question: str
    generated_sql: str
    explanation: str
    rows: List[Dict[str, Any]]
    row_count: int
    tables_used: List[str]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: Optional[str] = None  # non-None on any failure; rows=[] when set
    proof_record: Optional[Any] = None  # ProofRecord when Feature 12 narrator is active
    # Plain-language notes on any automatic rewrite of the first generated query
    # (e.g. regenerated after it returned no rows). Surfaced as a receipt caveat.
    query_rewrites: List[str] = []
