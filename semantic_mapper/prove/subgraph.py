# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Bounded subgraph extraction from an rdflib OWL graph.

Extracts a neighbourhood of classes and properties centred on a caller-supplied
set of class URIs.  Used as grounding context for the Phase 6 proof narrator.

Expansion rules (no inference, no transitivity):
  - Properties whose rdfs:domain touches a queried class
  - Classes one hop via rdfs:range of those properties
  - Domain classes of any explicitly requested property_uris
  - One hop up the subclass hierarchy from each queried class
  - One hop down the subclass hierarchy from each queried class

Provenance is read from fm: annotation predicates (Phase 0b).
Gracefully degrades to empty strings when fm: annotations are absent.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Set

import rdflib
from rdflib import OWL, RDF, RDFS, Namespace, URIRef

FM = Namespace("http://forgemesh.ai/ontology#")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClassNode:
    uri: str
    label: str
    comment: str
    source_table: str       # from fm:sourceTable


@dataclass(frozen=True)
class PropertyNode:
    uri: str
    label: str
    comment: str
    source_column: str      # from fm:sourceColumn (DatatypeProperties)
    source_fk: str          # from fm:sourceForeignKey (ObjectProperties)
    sql_type: str           # from fm:sqlType
    null_rate: str          # from fm:nullRate
    domain_uri: str
    range_uri: str
    kind: str               # "DatatypeProperty" | "ObjectProperty"


@dataclass(frozen=True)
class OntologySubgraph:
    classes: Dict[str, ClassNode]
    properties: Dict[str, PropertyNode]
    adjacency_notes: List[str]   # one per property that has a domain class


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_subgraph(
    graph: rdflib.Graph,
    class_uris: Set[str],
    property_uris: Set[str],
    filtered_property_uris: Set[str] = frozenset(),
) -> OntologySubgraph:
    """Extract a bounded neighbourhood from *graph* centred on *class_uris*.

    Args:
        graph:                  Loaded rdflib.Graph (not mutated).
        class_uris:             Seed OWL Class URIs to centre the subgraph on.
        property_uris:          Additional property URIs to include explicitly.
        filtered_property_uris: Subset of included properties that are filtered
                                in the calling query (reflected in adjacency_notes).

    Returns:
        OntologySubgraph with classes, properties, and adjacency_notes.
    """
    # --- Step 1: collect all property URIs -----------------------------------
    all_prop_uris: Set[str] = set(property_uris)

    for cls_uri in class_uris:
        for prop in graph.subjects(RDFS.domain, URIRef(cls_uri)):
            if not isinstance(prop, URIRef):
                continue
            # ISSUE-05: only include a property when its SOLE rdfs:domain is the
            # queried class.  Properties with multiple rdfs:domain values (multi-
            # domain bleed from the iterative generator) would appear as valid
            # assumptions for classes they don't actually apply to.  Checking
            # the full domain set prevents hasAmountUom from appearing in an
            # Admission proof simply because Admission is one of its domains.
            all_domains = {str(d) for d in graph.objects(prop, RDFS.domain) if isinstance(d, URIRef)}
            if all_domains <= set(class_uris):
                all_prop_uris.add(str(prop))

    # --- Step 2: expand class set -------------------------------------------
    all_class_uris: Set[str] = set(class_uris)

    for prop_uri in all_prop_uris:
        prop_ref = URIRef(prop_uri)
        for obj in graph.objects(prop_ref, RDFS.range):
            if isinstance(obj, URIRef):
                all_class_uris.add(str(obj))
        for obj in graph.objects(prop_ref, RDFS.domain):
            if isinstance(obj, URIRef):
                all_class_uris.add(str(obj))

    for cls_uri in class_uris:
        for parent in graph.objects(URIRef(cls_uri), RDFS.subClassOf):
            if isinstance(parent, URIRef):
                all_class_uris.add(str(parent))

    for cls_uri in class_uris:
        for child in graph.subjects(RDFS.subClassOf, URIRef(cls_uri)):
            if isinstance(child, URIRef):
                all_class_uris.add(str(child))

    # --- Step 3: build ClassNode objects ------------------------------------
    classes: Dict[str, ClassNode] = {}
    for uri_str in all_class_uris:
        ref = URIRef(uri_str)
        classes[uri_str] = ClassNode(
            uri=uri_str,
            label=_first_literal(graph, ref, RDFS.label),
            comment=_first_literal(graph, ref, RDFS.comment),
            source_table=_first_literal(graph, ref, FM.sourceTable),
        )

    # --- Step 4: build PropertyNode objects ---------------------------------
    properties: Dict[str, PropertyNode] = {}
    for uri_str in all_prop_uris:
        ref = URIRef(uri_str)
        is_object = (ref, RDF.type, OWL.ObjectProperty) in graph
        properties[uri_str] = PropertyNode(
            uri=uri_str,
            label=_first_literal(graph, ref, RDFS.label),
            comment=_first_literal(graph, ref, RDFS.comment),
            source_column=_first_literal(graph, ref, FM.sourceColumn),
            source_fk=_first_literal(graph, ref, FM.sourceForeignKey),
            sql_type=_first_literal(graph, ref, FM.sqlType),
            null_rate=_first_literal(graph, ref, FM.nullRate),
            domain_uri=_first_uri(graph, ref, RDFS.domain),
            range_uri=_first_uri(graph, ref, RDFS.range),
            kind="ObjectProperty" if is_object else "DatatypeProperty",
        )

    # --- Step 5: build adjacency notes (sorted for determinism) -------------
    adjacency_notes = _build_adjacency_notes(properties, filtered_property_uris)

    return OntologySubgraph(
        classes=classes,
        properties=properties,
        adjacency_notes=adjacency_notes,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _first_literal(graph: rdflib.Graph, subject: URIRef, predicate) -> str:
    for obj in graph.objects(subject, predicate):
        if isinstance(obj, rdflib.Literal):
            return str(obj)
    return ""


def _first_uri(graph: rdflib.Graph, subject: URIRef, predicate) -> str:
    for obj in graph.objects(subject, predicate):
        if isinstance(obj, URIRef):
            return str(obj)
    return ""


def _local(uri: str) -> str:
    if "#" in uri:
        return uri.rsplit("#", 1)[-1]
    return uri.rsplit("/", 1)[-1]


def _build_adjacency_notes(
    properties: Dict[str, PropertyNode],
    filtered_property_uris: Set[str],
) -> List[str]:
    notes = []
    for prop_uri in sorted(properties):
        pnode = properties[prop_uri]
        if not pnode.domain_uri:
            continue  # abstract properties have no domain to anchor the note

        domain_label = _local(pnode.domain_uri)
        prop_label = _local(pnode.uri)
        flag = (
            "filtered in this query"
            if prop_uri in filtered_property_uris
            else "not filtered in this query"
        )

        if pnode.kind == "ObjectProperty":
            provenance = f" (FK: {pnode.source_fk})" if pnode.source_fk else ""
            notes.append(
                f"{domain_label} has object property {prop_label}{provenance} — {flag}"
            )
        else:
            provenance = f" (from {pnode.source_column})" if pnode.source_column else ""
            notes.append(
                f"{domain_label} has property {prop_label}{provenance} — {flag}"
            )

    return notes
