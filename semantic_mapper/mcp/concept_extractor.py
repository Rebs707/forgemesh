# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 1 — extract_anchor_concepts.

Identifies ontology class URIs relevant to a natural-language question using
label matching (exact then fuzzy) against GoldTableDefinitions and the OWL
graph's rdfs:label annotations.  No LLM call; runs in milliseconds.

Fallback: if no anchors are found, returns all class URIs so the caller
always gets a usable (if unfiltered) result.
"""
from __future__ import annotations

import difflib
import re
from typing import Any, Dict, List, Optional, Set


def _tokenise(text: str) -> List[str]:
    """Lowercase, strip punctuation, split on whitespace."""
    return re.sub(r"[^\w\s]", " ", text.lower()).split()


def _ngrams(tokens: List[str], n: int) -> List[str]:
    return [" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]


def _label_variants(label: str) -> Set[str]:
    """Return lowercase matching forms for a class or property label."""
    variants = {label.lower()}
    # snake_case → space-separated
    variants.add(label.replace("_", " ").lower())
    # camelCase → space-separated
    variants.add(re.sub(r"([A-Z])", r" \1", label).strip().lower())
    # strip "has" / "is" prefix
    stripped = re.sub(r"^(has|is)([A-Z])", r"\2", label)
    if stripped != label:
        variants.update(_label_variants(stripped))
    return {v.strip() for v in variants if v.strip()}


def _rdfs_labels(ontology_graph: Any) -> Dict[str, List[str]]:
    """Return {class_uri: [lowercase label variants]} from rdfs:label annotations."""
    if ontology_graph is None:
        return {}
    try:
        from rdflib import RDFS
        result: Dict[str, List[str]] = {}
        for subj, _, obj in ontology_graph.triples((None, RDFS.label, None)):
            uri = str(subj)
            lbl = str(obj).lower().strip()
            result.setdefault(uri, []).append(lbl)
        return result
    except Exception:
        return {}


def extract_anchor_concepts(
    question: str,
    definitions: List[Any],
    ontology_graph: Any = None,
    fuzzy_cutoff: float = 0.75,
) -> List[str]:
    """Return class URIs relevant to *question* by label matching.

    Matching strategy (in order):
    1. Exact match on class entity_short name (``gold_admission`` → ``admission``)
    2. Exact match on ngrams (1-3 tokens) against label variants
    3. Exact match on rdfs:label from ontology graph
    4. Fuzzy match (difflib, cutoff=fuzzy_cutoff) on all labels
    5. Fallback: return all class URIs if nothing matched

    Parameters
    ----------
    question:
        The user's natural-language question.
    definitions:
        List of GoldTableDefinition — provides class URIs and entity labels.
    ontology_graph:
        Optional rdflib.Graph for rdfs:label lookup.
    fuzzy_cutoff:
        difflib similarity threshold for fuzzy matching (0–1).

    Returns
    -------
    Deduplicated list of primary_class_uri strings, preserving match order.
    """
    tokens = _tokenise(question)
    question_ngrams: Set[str] = set(tokens)
    for n in (2, 3):
        question_ngrams.update(_ngrams(tokens, n))

    # Build label index: {lowercase_label_variant: class_uri}
    label_index: Dict[str, str] = {}
    all_uris: List[str] = []

    for defn in definitions:
        uri = defn.primary_class_uri
        if not uri:
            continue
        all_uris.append(uri)

        entity_short = (
            defn.gold_table_name[len("gold_"):]
            if defn.gold_table_name.startswith("gold_")
            else defn.gold_table_name
        )
        for variant in _label_variants(entity_short):
            label_index[variant] = uri
        # Also index the class_label field directly
        for variant in _label_variants(defn.class_label):
            label_index[variant] = uri

    # Add rdfs:labels from ontology
    uri_to_rdfs: Dict[str, List[str]] = _rdfs_labels(ontology_graph)
    for uri, labels in uri_to_rdfs.items():
        for lbl in labels:
            label_index[lbl] = uri

    # --- Pass 1: exact match ---
    matched: List[str] = []
    seen: Set[str] = set()

    for ngram in sorted(question_ngrams, key=len, reverse=True):
        if ngram in label_index:
            uri = label_index[ngram]
            if uri not in seen:
                seen.add(uri)
                matched.append(uri)

    # --- Pass 2: fuzzy match (on unmatched classes) ---
    unmatched_uris = [u for u in all_uris if u not in seen]
    if unmatched_uris and tokens:
        all_labels = list(label_index.keys())
        for token_window in sorted(question_ngrams, key=len, reverse=True):
            close = difflib.get_close_matches(
                token_window, all_labels, n=3, cutoff=fuzzy_cutoff
            )
            for match in close:
                uri = label_index[match]
                if uri in unmatched_uris and uri not in seen:
                    seen.add(uri)
                    matched.append(uri)

    # --- Fallback: no anchors found → return all ---
    if not matched:
        return list(all_uris)

    return matched
