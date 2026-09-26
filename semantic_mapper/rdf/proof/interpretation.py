# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 5: Interpretation submodule (v1 — pattern matching).

Maps NL question phrases to ontology concepts by matching question tokens
against rdfs:label, rdfs:comment, and fm:interpretation/fm:extension
annotations on classes and properties referenced in the SPARQL.

The open question from the plan — whether to use a translator side-channel
(option b) or pattern matching (option a) — is resolved as option (a) for
v1. Option (b) requires a structured side-channel from the NL→SPARQL
translator that doesn't yet exist. Pattern matching is used here, which
yields MEDIUM confidence at best unless fm:interpretation annotations are
present.

Confidence rubric (mechanical):
  HIGH   — every SPARQL URI has an fm:interpretation or fm:extension
            annotation and every NL noun phrase maps to one URI (DIRECT).
  MEDIUM — all URIs found in the ontology but at least one maps via
            rdfs:comment substring matching (PATTERN).
  LOW    — at least one SPARQL URI is absent from the ontology, or no
            concept mapping at all could be made.
"""
from __future__ import annotations

import re
from typing import List, Optional, Set, Tuple

from rdflib import Graph, OWL, RDF, RDFS, URIRef

from semantic_mapper.rdf.proof._fm import FM
from semantic_mapper.rdf.proof.models import (
    InterpretedConcept,
    QuestionInterpretation,
)

_STOP_WORDS: Set[str] = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "shall",
    "should", "may", "might", "can", "could", "of", "in", "on", "at",
    "to", "for", "from", "by", "with", "about", "against", "between",
    "into", "through", "during", "how", "many", "much", "what", "which",
    "who", "when", "where", "why", "all", "each", "every", "any", "both",
    "and", "or", "but", "if", "than", "that", "this", "these", "those",
    "its", "their", "our", "your", "my", "his", "her", "list", "show",
    "give", "get", "find", "count", "number", "per", "total", "average",
}


def _tokenise(text: str) -> List[str]:
    """Return meaningful lowercased tokens from a natural-language string."""
    tokens = re.findall(r"[a-zA-Z]+", text.lower())
    return [t for t in tokens if t not in _STOP_WORDS and len(t) > 2]


def _local_name(uri: str) -> str:
    return uri.split("#")[-1].split("/")[-1]


def _annotation_texts(
    ref: URIRef, ontology_graph: Graph
) -> List[Tuple[str, str]]:
    """Return [(annotation_text, source_predicate_local)] for a given node."""
    texts: List[Tuple[str, str]] = []
    for pred, source in [
        (FM.interpretation, "fm:interpretation"),
        (FM.extension, "fm:extension"),
        (RDFS.label, "rdfs:label"),
        (RDFS.comment, "rdfs:comment"),
    ]:
        for val in ontology_graph.objects(ref, pred):
            if str(val).strip():
                texts.append((str(val).lower(), source))
    return texts


def _map_uri_to_concept(
    uri: str,
    nl_tokens: List[str],
    ontology_graph: Optional[Graph],
) -> Optional[InterpretedConcept]:
    """Attempt to map a single ontology URI to an NL concept via annotation matching."""
    if ontology_graph is None:
        return None

    ref = URIRef(uri)
    annotation_entries = _annotation_texts(ref, ontology_graph)

    if not annotation_entries:
        return None

    # Find the best matching NL phrase
    best_phrase: Optional[str] = None
    best_annotation: Optional[str] = None
    best_source: Optional[str] = None
    best_score = 0

    for annotation_text, source in annotation_entries:
        for token in nl_tokens:
            if token in annotation_text:
                score = len(token)  # prefer longer matching tokens
                if score > best_score:
                    best_score = score
                    best_phrase = token
                    best_annotation = annotation_text
                    best_source = source

    if best_phrase is None or best_annotation is None:
        # No NL token matches any annotation — use local name as fallback
        local = _local_name(uri)
        if annotation_entries:
            annotation_text, source = annotation_entries[0]
            return InterpretedConcept(
                nl_phrase=local,
                mapped_to_uri=uri,
                mapping_form="PATTERN",
                grounding_annotation=annotation_text,
                grounding_node_uri=uri,
            )
        return None

    # Determine mapping form: DIRECT if fm:interpretation/extension cited,
    # PATTERN if matched via rdfs:comment substring
    if best_source in ("fm:interpretation", "fm:extension"):
        form = "DIRECT" if best_source == "fm:interpretation" else "EXTENSION"
    else:
        form = "PATTERN"

    return InterpretedConcept(
        nl_phrase=best_phrase,
        mapped_to_uri=uri,
        mapping_form=form,
        grounding_annotation=best_annotation,
        grounding_node_uri=uri,
    )


def _confidence(
    concepts: List[InterpretedConcept],
    sparql_uris: List[str],
    ontology_graph: Optional[Graph],
) -> Tuple[str, str]:
    """Return (confidence_level, confidence_reason) using the mechanical rubric."""
    if not sparql_uris:
        return "LOW", "No ontology URIs detected in the SPARQL query."

    if ontology_graph is None:
        return "LOW", "No ontology supplied; interpretation is based on URI local names only."

    if not concepts:
        return "LOW", "No NL concepts could be mapped to ontology nodes."

    # Check whether any URI is absent from the ontology
    missing: List[str] = []
    for uri in sparql_uris:
        ref = URIRef(uri)
        if not any(ontology_graph.triples((ref, None, None))) and not any(
            ontology_graph.triples((None, None, ref))
        ):
            missing.append(uri)

    if missing:
        return (
            "LOW",
            f"{len(missing)} URI(s) referenced in SPARQL are absent from the ontology: "
            + ", ".join(_local_name(u) for u in missing[:3])
            + ("…" if len(missing) > 3 else ""),
        )

    forms = {c.mapping_form for c in concepts}
    if "PATTERN" in forms:
        return (
            "MEDIUM",
            "All queried classes/properties found in the ontology, but at least one "
            "concept was matched via rdfs:comment substring rather than an explicit "
            "fm:interpretation or fm:extension annotation.",
        )

    return (
        "HIGH",
        "Every interpreted concept maps directly to an ontology node with an "
        "fm:interpretation or fm:extension annotation.",
    )


def build_interpretation(
    nl_question: str,
    sparql_class_uris: List[str],
    sparql_property_uris: List[str],
    ontology_graph: Optional[Graph],
) -> QuestionInterpretation:
    """Produce a QuestionInterpretation by pattern-matching the NL question against the ontology."""
    nl_tokens = _tokenise(nl_question)
    all_uris = sparql_class_uris + sparql_property_uris

    concepts: List[InterpretedConcept] = []
    seen_uris: Set[str] = set()
    seen_nl_phrases: Set[str] = set()  # PROVE_IT-5: prevent same phrase appearing twice

    for uri in all_uris:
        if uri in seen_uris:
            continue
        seen_uris.add(uri)
        concept = _map_uri_to_concept(uri, nl_tokens, ontology_graph)
        if concept is None:
            continue
        # Deduplicate by (nl_phrase, mapped_to_uri) — same phrase from two URIs
        # (e.g. "Admission" class and "hasAdmission*" property) would otherwise
        # produce two undistinguished entries with the same NL text.
        dedup_key = f"{concept.nl_phrase}|{concept.mapped_to_uri}"
        if dedup_key in seen_nl_phrases:
            continue
        seen_nl_phrases.add(dedup_key)
        concepts.append(concept)

    confidence, reason = _confidence(concepts, all_uris, ontology_graph)

    return QuestionInterpretation(
        nl_question=nl_question,
        interpreted_concepts=concepts,
        confidence=confidence,
        confidence_reason=reason,
    )
