# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Frozen result models for the terminology-binding pass."""
from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class DetectedColumn(BaseModel, frozen=True):
    """A coded column detected as bindable to a terminology source by value overlap."""

    table: str
    column: str
    source_id: str
    system_label: str
    overlap: float            # fraction of distinct values valid in the source
    n_distinct: int           # distinct non-null values considered
    matched_values: List[str] = Field(default_factory=list)
    unmatched_values: List[str] = Field(default_factory=list)


class ProposedBinding(BaseModel, frozen=True):
    """An individual-level external-concept binding (``skos:exactMatch``)."""

    individual_uri: str
    raw_value: str
    source_id: str
    system_label: str
    concept_iri: str
    confidence: float
    method: str = "value-set-overlap"      # | "human-confirmed"
    status: str = "PENDING_REVIEW"          # | "CONFIRMED"
    # >1 candidate → an ambiguous one-to-many concept map; surfaced for review,
    # never auto-resolved (plan KNOWN-PENDING).
    candidate_iris: List[str] = Field(default_factory=list)


class SystemBinding(BaseModel, frozen=True):
    """A system-level alignment of a class or property to a code system
    (best-effort ``skos:closeMatch`` / ``rdfs:seeAlso``)."""

    subject_uri: str
    subject_kind: str        # "class" | "property"
    source_id: str
    system_label: str
    system_iri: str
    predicate: str           # "skos:closeMatch" | "rdfs:seeAlso"


class CodedProperty(BaseModel, frozen=True):
    """A high-cardinality coded column re-typed to a terminology-coded object
    property at generation. Its code individuals (+ ``skos:exactMatch``) are minted
    lazily at materialisation, one per distinct code, typed as ``system_class_uri``."""

    property_uri: str
    system_id: str
    system_label: str
    system_class_uri: str       # conformed per-system code class (e.g. SnomedctConcept)
    overlap: float              # column-level value-overlap that triggered the re-typing


class UnmappedValue(BaseModel, frozen=True):
    """A distinct value in a detected coded column that was NOT bound — surfaced
    so no coded value silently passes through unbound and unreported (TERM-4)."""

    table: str
    column: str
    value: str
    reason: str              # "not-in-source" | "no-individual" | "sub-floor"


class BindingResult(BaseModel, frozen=True):
    """Everything the binder pass produced — the sidecar contents and a coverage
    audit, in addition to what it asserted into the graph."""

    detected_columns: List[DetectedColumn] = Field(default_factory=list)
    asserted: List[ProposedBinding] = Field(default_factory=list)    # emitted (≥ floor)
    sidecar: List[ProposedBinding] = Field(default_factory=list)     # sub-floor / ambiguous
    system_bindings: List[SystemBinding] = Field(default_factory=list)
    coded_properties: List[CodedProperty] = Field(default_factory=list)  # high-card → coded object props
    unmapped: List[UnmappedValue] = Field(default_factory=list)

    @property
    def n_asserted(self) -> int:
        return len(self.asserted)

    @property
    def n_sidecar(self) -> int:
        return len(self.sidecar)

    @property
    def n_detected_columns(self) -> int:
        return len(self.detected_columns)
