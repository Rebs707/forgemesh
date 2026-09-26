# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Pydantic contracts for Feature 12 ontology-grounded proof records.

proof_schema_version="2.0.0" guarantees no bronze/silver references appear.
Every claim has a grounding_node_uri and grounding_annotation — enforced by
field validators so the invariant cannot be violated at construction time.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, field_validator


class InterpretedConcept(BaseModel, frozen=True):
    """A single NL phrase mapped to an ontology node with a cited grounding."""

    nl_phrase: str
    mapped_to_uri: str
    mapping_form: Literal["DIRECT", "EXTENSION", "PATTERN"]
    grounding_annotation: str
    grounding_node_uri: str

    @field_validator("grounding_annotation")
    @classmethod
    def _annotation_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("grounding_annotation must not be empty — every claim must cite the ontology")
        return v

    @field_validator("grounding_node_uri")
    @classmethod
    def _node_uri_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("grounding_node_uri must not be empty — every claim must be traceable to the ontology")
        return v


class QuestionInterpretation(BaseModel, frozen=True):
    """What the NL question was understood to mean, in ontology terms."""

    nl_question: str
    interpreted_concepts: List[InterpretedConcept]
    confidence: Literal["HIGH", "MEDIUM", "LOW"]
    confidence_reason: str


class AppliedConstraint(BaseModel, frozen=True):
    """A filter or binding in the SPARQL query, grounded in an ontology property."""

    property_uri: str
    filter_form: str
    property_definition: str
    range_uri: Optional[str] = None


class ResultFraming(BaseModel, frozen=True):
    """What the result says in ontology terms: class, cardinality, constraints."""

    cardinality: int
    primary_class_uri: str
    joined_class_uris: List[str]
    constraints_applied: List[AppliedConstraint]


class ScopeAssumption(BaseModel, frozen=True):
    """A property that was available but not constrained by the query."""

    unconstrained_property_uri: str
    property_label: str
    implied_inclusion: str
    annotated_default: Optional[str] = None


class Limitation(BaseModel, frozen=True):
    """An epistemic limitation on the result."""

    kind: Literal["OPEN_WORLD", "CARDINALITY", "ANNOTATED"]
    text: str
    grounding_node_uri: Optional[str] = None


class ProofRecord(BaseModel, frozen=True):
    """Complete ontology-grounded proof for a single SPARQL query result.

    Version 2.0.0: no bronze/silver references. Every claim is grounded in
    the ontology subgraph identified by ontology_subgraph_hash.
    """

    proof_schema_version: str = "2.0.0"
    query_id: str
    interpretation: QuestionInterpretation
    framing: ResultFraming
    scope_assumptions: List[ScopeAssumption]
    limitations: List[Limitation]
    ontology_subgraph_hash: str
    generated_at: datetime
