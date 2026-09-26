# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Example questions for the query UI, derived from the schema's structure.

No dataset knowledge: questions come from three structural signals in the gold
definitions — which entity other entities point at most (a count), FK
relationships ("which B have the most A?"), and value-mapped categorical columns
("how many A by category?").
"""
from __future__ import annotations

import re
from typing import Dict, List, Mapping, Sequence, Tuple

import inflect

from semantic_mapper.gold.models import GoldTableDefinition

_inflect = inflect.engine()
MAX_SUGGESTIONS = 8


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _plural(phrase: str) -> str:
    *head, last = phrase.split(" ")
    return " ".join([*head, _inflect.plural_noun(last) or last])


def _entity_name(defn: GoldTableDefinition, class_labels: Mapping[str, str]) -> str:
    label = class_labels.get(defn.primary_class_uri) or defn.class_label
    # CamelCase / snake_case → lower-case words
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", label).replace("_", " ")
    return " ".join(words.lower().split())


def _column_name(source_column: str) -> str:
    return " ".join(source_column.lower().replace("_", " ").split())


def build_suggestions(
    definitions: Sequence[GoldTableDefinition],
    prop_ranges: Mapping[str, str],
    class_labels: Mapping[str, str] | None = None,
) -> List[str]:
    """Return up to MAX_SUGGESTIONS example questions for *definitions*.

    prop_ranges maps a property URI to its rdfs:range; a range that is another
    entity's class makes that property an FK relationship (ranges that aren't gold
    entities, such as categorical value classes, are ignored). class_labels
    optionally maps class URIs to display labels.
    """
    class_labels = class_labels or {}
    by_class = {d.primary_class_uri: d for d in definitions}
    if not definitions:
        return []

    names = {d.gold_table_name: _entity_name(d, class_labels) for d in definitions}

    # FK edges source → target, and each entity's in-degree.
    edges: List[Tuple[GoldTableDefinition, GoldTableDefinition]] = []
    in_degree: Dict[str, int] = {d.gold_table_name: 0 for d in definitions}
    for d in definitions:
        seen = set()
        for spec in d.column_specs:
            target = by_class.get(prop_ranges.get(spec.property_uri, ""))
            if target is None or target.gold_table_name == d.gold_table_name:
                continue
            if target.gold_table_name in seen:
                continue
            seen.add(target.gold_table_name)
            edges.append((d, target))
            in_degree[target.gold_table_name] += 1

    def centrality(d: GoldTableDefinition) -> Tuple[int, int]:
        return (in_degree[d.gold_table_name], len(d.column_specs))

    ranked = sorted(definitions, key=centrality, reverse=True)
    central = ranked[0]

    relationship_qs: List[str] = []
    per_target: Dict[str, int] = {}
    for src, tgt in sorted(edges, key=lambda e: centrality(e[1]), reverse=True):
        if per_target.get(tgt.gold_table_name, 0) >= 2:
            continue
        per_target[tgt.gold_table_name] = per_target.get(tgt.gold_table_name, 0) + 1
        relationship_qs.append(
            f"Which {_plural(names[tgt.gold_table_name])} have the most "
            f"{_plural(names[src.gold_table_name])}?"
        )
    categorical_qs: List[str] = []
    for d in ranked:
        cats = [
            spec for spec in d.column_specs
            if spec.value_map and not _norm(spec.source_column).endswith("id")
        ]
        categorical_qs += [
            f"How many {_plural(names[d.gold_table_name])} are there by "
            f"{_column_name(spec.source_column)}?"
            for spec in cats[:2]
        ]

    questions = [f"How many {_plural(names[central.gold_table_name])} are there in total?"]
    for pair in zip(relationship_qs, categorical_qs):
        questions.extend(pair)
    longer = relationship_qs if len(relationship_qs) > len(categorical_qs) else categorical_qs
    questions.extend(longer[min(len(relationship_qs), len(categorical_qs)):])
    if len(questions) == 1:
        questions.append(f"Show me a sample of {_plural(names[central.gold_table_name])}")

    deduped: List[str] = []
    for q in questions:
        if q not in deduped:
            deduped.append(q)
    return deduped[:MAX_SUGGESTIONS]
