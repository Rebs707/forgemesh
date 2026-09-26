# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Phase 6 — Proof narrator.

Single LLM call that produces a structured InterpretationNote grounded in
the ontology subgraph.  Every scope assumption is verified against the
subgraph after the LLM responds; ungrounded claims are stripped and each
one triggers a NARRATOR_UNGROUNDED caveat.

Never raises — all failures degrade gracefully to a NARRATION_FAILED caveat.

Fallback when PROVE_NARRATE=False: returns immediately with narrated=False.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from semantic_mapper.config.settings import Settings
from semantic_mapper.prove.decision_lookup import MappingDecisionRef
from semantic_mapper.prove.subgraph import OntologySubgraph

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InterpretationNote:
    interpreted_as: str
    scope_assumptions: List[str]
    not_included: List[str]
    interpretation_confidence: str          # "HIGH" | "MEDIUM" | "LOW"
    interpretation_confidence_reason: str
    narrated: bool
    narrator_model: Optional[str]


@dataclass(frozen=True)
class ProofCaveat:
    type: str       # QUARANTINED_ROWS | NULL_VALUES_EXCLUDED | SAMPLED |
                    # NARRATOR_UNGROUNDED | NARRATION_FAILED | LINEAGE_BROKEN
    count: Optional[int]
    table: Optional[str]
    property_uri: Optional[str]
    reason: Optional[str]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a proof narrator for a semantic data pipeline. Given a natural-language
question, the query executed to answer it, the answer, and ontology context,
produce a structured interpretation. Be precise and concise. Assemble — do not infer.

Return ONLY valid JSON with this exact structure:
{
  "interpreted_as": "<one sentence: what entity/time/scope the answer covers>",
  "scope_assumptions": [
    "<one assumption per property marked NOT FILTERED in the adjacency notes below>"
  ],
  "not_included": [
    "<explicit exclusions: NULL values, quarantined rows, filters applied, etc.>"
  ],
  "interpretation_confidence": "<HIGH|MEDIUM|LOW>",
  "interpretation_confidence_reason": "<one sentence>"
}

Rules:
- scope_assumptions: generate one entry per property in the adjacency notes that is
  marked "not filtered in this query". Use the property local name verbatim in the
  assumption text. If there are no such properties, return an empty list.
- not_included: derive from caveats and any explicit WHERE filters in the query.
- interpretation_confidence HIGH: query is unambiguous, time window and scope explicit.
  MEDIUM: some scope is implicit. LOW: significant ambiguity.
- interpretation_confidence_reason MUST be non-empty when confidence is MEDIUM or LOW.
  Explain specifically what makes the interpretation ambiguous or incomplete.
"""


def narrate(
    question: str,
    query: str,
    answer: Dict[str, Any],
    subgraph: OntologySubgraph,
    decisions: List[MappingDecisionRef],
    caveats: List[ProofCaveat],
    settings: Settings,
) -> Tuple[InterpretationNote, List[ProofCaveat]]:
    """Produce an InterpretationNote and updated caveats list.

    Never raises.  Returns a degraded note with NARRATION_FAILED caveat on
    any LLM or parsing failure.
    """
    if not settings.PROVE_NARRATE:
        return _no_narration(), list(caveats)

    try:
        raw = _call_llm(question, query, answer, subgraph, decisions, caveats, settings)
        note = _parse_llm_response(raw, settings.PROVE_NARRATOR_MODEL)
    except Exception as exc:
        logger.warning("narrator: LLM call or parse failed: %s", exc)
        fail_caveat = ProofCaveat(
            type="NARRATION_FAILED",
            count=None, table=None, property_uri=None,
            reason=str(exc),
        )
        return _degraded_note(), list(caveats) + [fail_caveat]

    note, updated_caveats = _apply_grounding_guard(note, subgraph, list(caveats))
    return note, updated_caveats


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

def _call_llm(
    question: str,
    query: str,
    answer: Dict[str, Any],
    subgraph: OntologySubgraph,
    decisions: List[MappingDecisionRef],
    caveats: List[ProofCaveat],
    settings: Settings,
) -> str:
    user_content = _build_user_message(question, query, answer, subgraph, decisions, caveats)

    from semantic_mapper.core.llm import call_llm
    result = call_llm(user_content, settings, system_prompt=_SYSTEM_PROMPT, json_mode=True)
    return result.content


def _build_user_message(
    question: str,
    query: str,
    answer: Dict[str, Any],
    subgraph: OntologySubgraph,
    decisions: List[MappingDecisionRef],
    caveats: List[ProofCaveat],
) -> str:
    parts: list[str] = []

    parts.append(f"QUESTION: {question}")
    parts.append(f"ANSWER: {json.dumps(answer)}")
    parts.append(f"QUERY:\n{query}")

    if subgraph.adjacency_notes:
        parts.append("ONTOLOGY ADJACENCY NOTES (one per property):")
        for note in subgraph.adjacency_notes:
            parts.append(f"  - {note}")

    if decisions:
        parts.append("MAPPING DECISIONS:")
        for d in decisions:
            conf = f"[{d.confirmation}]"
            parts.append(f"  - {d.uri} ({d.kind}) source={d.source} {conf} score={d.score:.2f}")

    if caveats:
        parts.append("EXISTING CAVEATS:")
        for c in caveats:
            reason = f" — {c.reason}" if c.reason else ""
            parts.append(f"  - {c.type}{reason}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

_VALID_CONFIDENCE = {"HIGH", "MEDIUM", "LOW"}


def _parse_llm_response(content: str, model: str) -> InterpretationNote:
    data = json.loads(content)
    confidence = str(data.get("interpretation_confidence", "LOW")).upper()
    if confidence not in _VALID_CONFIDENCE:
        confidence = "LOW"

    confidence_reason = str(data.get("interpretation_confidence_reason", ""))
    if not confidence_reason and confidence in {"MEDIUM", "LOW"}:
        confidence_reason = (
            f"Confidence is {confidence} but the system could not identify a specific reason."
        )

    return InterpretationNote(
        interpreted_as=str(data.get("interpreted_as", "")),
        scope_assumptions=list(data.get("scope_assumptions") or []),
        not_included=list(data.get("not_included") or []),
        interpretation_confidence=confidence,
        interpretation_confidence_reason=confidence_reason,
        narrated=True,
        narrator_model=model,
    )


# ---------------------------------------------------------------------------
# Grounding guard
# ---------------------------------------------------------------------------

def _grounding_tokens(subgraph: OntologySubgraph) -> frozenset[str]:
    """Return lowercase tokens (local names + labels) for all subgraph properties."""
    tokens: set[str] = set()
    for pnode in subgraph.properties.values():
        local = re.split(r"[#/]", pnode.uri)[-1]
        if local:
            tokens.add(local.lower())
        if pnode.label:
            tokens.add(pnode.label.lower())
    return frozenset(t for t in tokens if len(t) > 2)


def _is_grounded(assumption: str, tokens: frozenset[str]) -> bool:
    lower = assumption.lower()
    return any(t in lower for t in tokens)


def _apply_grounding_guard(
    note: InterpretationNote,
    subgraph: OntologySubgraph,
    caveats: list[ProofCaveat],
) -> Tuple[InterpretationNote, list[ProofCaveat]]:
    if not note.scope_assumptions:
        return note, caveats

    tokens = _grounding_tokens(subgraph)
    good: list[str] = []
    for assumption in note.scope_assumptions:
        if _is_grounded(assumption, tokens):
            good.append(assumption)
        else:
            logger.warning("narrator: ungrounded scope assumption removed: %r", assumption)
            caveats = caveats + [ProofCaveat(
                type="NARRATOR_UNGROUNDED",
                count=None, table=None, property_uri=None,
                reason=assumption,
            )]

    if good == note.scope_assumptions:
        return note, caveats

    updated = InterpretationNote(
        interpreted_as=note.interpreted_as,
        scope_assumptions=good,
        not_included=note.not_included,
        interpretation_confidence=note.interpretation_confidence,
        interpretation_confidence_reason=note.interpretation_confidence_reason,
        narrated=note.narrated,
        narrator_model=note.narrator_model,
    )
    return updated, caveats


# ---------------------------------------------------------------------------
# Fallbacks
# ---------------------------------------------------------------------------

def _no_narration() -> InterpretationNote:
    return InterpretationNote(
        interpreted_as="",
        scope_assumptions=[],
        not_included=[],
        interpretation_confidence="LOW",
        interpretation_confidence_reason="",
        narrated=False,
        narrator_model=None,
    )


def _degraded_note() -> InterpretationNote:
    return InterpretationNote(
        interpreted_as="",
        scope_assumptions=[],
        not_included=[],
        interpretation_confidence="LOW",
        interpretation_confidence_reason="Narration failed.",
        narrated=False,
        narrator_model=None,
    )
