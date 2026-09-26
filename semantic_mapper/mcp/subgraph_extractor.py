# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 2 — extract_subgraph.

BFS traversal of the OWL class graph starting from anchor class URIs.
Returns a filtered list of GoldTableDefinitions containing only the classes
and properties provably reachable within max_hops.

The returned definitions can be passed directly to build_agent_schema — they
are structurally identical to the full definitions list, just a subset.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Dict, List, Optional, Set


def _build_adjacency(
    definitions: List[Any],
    ontology_graph: Any = None,
) -> Dict[str, Set[str]]:
    """Return {class_uri: {reachable_class_uri, ...}} via object property links.

    Detection uses the same two-path logic as build_agent_schema:
    - Path 1: column name matches another entity's short name (FK by column)
    - Path 2: property rdfs:range identifies another class (FK by range)
    Also follows rdfs:subClassOf edges from the ontology graph.
    """
    entity_map: Dict[str, str] = {
        d.gold_table_name[len("gold_"):]: d.primary_class_uri
        for d in definitions
        if d.gold_table_name.startswith("gold_")
    }
    class_uri_to_def: Dict[str, Any] = {d.primary_class_uri: d for d in definitions}

    # Build prop_range from ontology if available
    prop_range: Dict[str, str] = {}
    if ontology_graph is not None:
        try:
            from rdflib import OWL, RDF, RDFS
            for prop in ontology_graph.subjects(RDF.type, OWL.ObjectProperty):
                range_ = next(ontology_graph.objects(prop, RDFS.range), None)
                if range_:
                    prop_range[str(prop)] = str(range_)
        except Exception:
            pass

    adjacency: Dict[str, Set[str]] = {d.primary_class_uri: set() for d in definitions}

    for defn in definitions:
        entity_short = (
            defn.gold_table_name[len("gold_"):]
            if defn.gold_table_name.startswith("gold_")
            else defn.gold_table_name
        )
        for spec in defn.column_specs:
            # Path 1: column name matches another entity's short name
            linked_uri = entity_map.get(spec.gold_column_name)
            if linked_uri and linked_uri != defn.primary_class_uri:
                adjacency[defn.primary_class_uri].add(linked_uri)
                # bidirectional — object properties are navigable both ways
                adjacency.setdefault(linked_uri, set()).add(defn.primary_class_uri)
                continue

            # Path 2: property rdfs:range identifies another class
            range_class = prop_range.get(spec.property_uri)
            if range_class and range_class in class_uri_to_def and range_class != defn.primary_class_uri:
                adjacency[defn.primary_class_uri].add(range_class)
                adjacency.setdefault(range_class, set()).add(defn.primary_class_uri)

    # rdfs:subClassOf edges from ontology
    if ontology_graph is not None:
        try:
            from rdflib import RDFS
            for child, _, parent in ontology_graph.triples((None, RDFS.subClassOf, None)):
                child_uri, parent_uri = str(child), str(parent)
                if child_uri in adjacency and parent_uri in adjacency:
                    adjacency[child_uri].add(parent_uri)
                    adjacency[parent_uri].add(child_uri)
        except Exception:
            pass

    return adjacency


def extract_subgraph(
    anchor_uris: List[str],
    definitions: List[Any],
    ontology_graph: Any = None,
    max_hops: int = 3,
) -> List[Any]:
    """Return GoldTableDefinitions reachable from anchor_uris within max_hops.

    Parameters
    ----------
    anchor_uris:
        Starting class URIs — typically from extract_anchor_concepts.
    definitions:
        Full list of GoldTableDefinition objects.
    ontology_graph:
        Optional rdflib.Graph for range/subClassOf traversal.
    max_hops:
        BFS depth limit.  3 is the recommended default.

    Returns
    -------
    Subset of *definitions* in original order, containing only reachable classes.
    Fallback: empty anchor_uris → all definitions returned unchanged.
    """
    if not anchor_uris:
        return list(definitions)

    class_uri_to_def: Dict[str, Any] = {d.primary_class_uri: d for d in definitions}
    adjacency = _build_adjacency(definitions, ontology_graph)

    # BFS
    visited: Set[str] = set()
    queue: deque = deque()

    for uri in anchor_uris:
        if uri in class_uri_to_def:
            queue.append((uri, 0))
            visited.add(uri)

    while queue:
        current_uri, depth = queue.popleft()
        if depth >= max_hops:
            continue
        for neighbour in adjacency.get(current_uri, set()):
            if neighbour not in visited and neighbour in class_uri_to_def:
                visited.add(neighbour)
                queue.append((neighbour, depth + 1))

    # Return in original definition order
    return [d for d in definitions if d.primary_class_uri in visited]
