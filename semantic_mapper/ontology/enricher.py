# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import logging
import re
from typing import Dict, List, Tuple

from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, URIRef

from semantic_mapper.schema.mapping_output import SemanticMappingReport

logger = logging.getLogger(__name__)


def _uri_fragment(uri: str) -> str:
    return uri.split("#")[-1] if "#" in uri else uri.split("/")[-1]


def _to_label(fragment: str) -> str:
    """CamelCase fragment → 'Camel Case' readable label."""
    return re.sub(r"([a-z])([A-Z])", r"\1 \2", fragment)


def _update_one_of(graph: Graph, parent_ref: URIRef, new_individuals: List[URIRef]) -> None:
    """Add new_individuals to the owl:oneOf enumeration on parent_ref's equivalentClass.

    If an enumeration already exists, appends to it.
    If none exists, creates owl:equivalentClass [ owl:oneOf ( ... ) ] from scratch.
    """
    for eq_class in graph.objects(parent_ref, OWL.equivalentClass):
        one_of_head = graph.value(eq_class, OWL.oneOf)
        if one_of_head is None:
            continue
        # Walk to the end of the existing RDF list
        current = one_of_head
        while True:
            rest = graph.value(current, RDF.rest)
            if rest is None or rest == RDF.nil:
                break
            current = rest
        for ind_ref in new_individuals:
            new_node = BNode()
            graph.remove((current, RDF.rest, RDF.nil))
            graph.add((current, RDF.rest, new_node))
            graph.add((new_node, RDF.first, ind_ref))
            graph.add((new_node, RDF.rest, RDF.nil))
            current = new_node
            logger.info("Ontology: appended %s to owl:oneOf on %s", ind_ref, parent_ref)
        return

    # No existing enumeration — create one
    eq_bnode = BNode()
    graph.add((parent_ref, OWL.equivalentClass, eq_bnode))
    # Build RDF list
    head = BNode()
    graph.add((eq_bnode, OWL.oneOf, head))
    current = head
    for i, ind_ref in enumerate(new_individuals):
        graph.add((current, RDF.first, ind_ref))
        if i < len(new_individuals) - 1:
            next_node = BNode()
            graph.add((current, RDF.rest, next_node))
            current = next_node
        else:
            graph.add((current, RDF.rest, RDF.nil))
    logger.info(
        "Ontology: created owl:oneOf on %s with %d individuals",
        parent_ref, len(new_individuals),
    )


def _individual_in_oneof(graph: Graph, cls_ref: URIRef, ind_ref: URIRef) -> bool:
    """Return True if ind_ref appears in any owl:oneOf enumeration on cls_ref."""
    for eq_class in graph.objects(cls_ref, OWL.equivalentClass):
        one_of_head = graph.value(eq_class, OWL.oneOf)
        if one_of_head is None:
            continue
        current = one_of_head
        while current and current != RDF.nil:
            if graph.value(current, RDF.first) == ind_ref:
                return True
            current = graph.value(current, RDF.rest)
    return False


def enrich_ontology_from_value_mappings(
    sidecar: Dict[str, list],
    report: SemanticMappingReport,
    graph: Graph,
    ontology_path: str,
) -> Tuple[int, List[str]]:
    """Add NamedIndividual declarations for derived value_mapping URIs not yet in the ontology.

    For each URI in the sidecar that is absent from the graph, adds:
      - rdf:type owl:NamedIndividual
      - rdf:type <parent>  where parent = rdfs:range of the column's property
      - rdfs:label derived from the URI fragment
      - appends to owl:oneOf enumeration on <parent> if one exists

    For URIs that already exist in the graph (warm run), ensures they are present
    in the parent's owl:oneOf enumeration; appends if absent.

    Writes the updated graph back to ontology_path if any individuals were added
    or if any warm-run appends to owl:oneOf occurred.
    Returns (num_added, list_of_new_uris).
    """
    # (table, col) → mapped_property_uri
    prop_by_col: Dict[Tuple[str, str], str] = {
        (tm.source_table, col.source_column): col.mapped_property_uri
        for tm in report.tables
        for col in tm.column_mappings
        if col.mapped_property_uri
    }

    # property_uri → rdfs:range class URI (object properties only)
    prop_range: Dict[str, str] = {}
    for prop in graph.subjects(RDF.type, OWL.ObjectProperty):
        range_ = graph.value(prop, RDFS.range)
        if range_:
            prop_range[str(prop)] = str(range_)

    existing = {str(s) for s in graph.subjects(RDF.type, OWL.NamedIndividual)}
    added: List[str] = []
    graph_modified = False

    # Group new individuals by parent so we extend owl:oneOf once per parent
    new_by_parent: Dict[str, List[URIRef]] = {}

    for key, rules in sidecar.items():
        table, col = key.split(".", 1)
        prop_uri = prop_by_col.get((table, col))
        if not prop_uri:
            continue
        parent_uri = prop_range.get(prop_uri)

        for rule in rules:
            uri = rule.get("mapped_uri")
            if not uri:
                continue

            ind_ref = URIRef(uri)

            if uri in existing:
                logger.info("Ontology: individual %s already exists (warm run)", uri)
                if parent_uri:
                    parent_ref = URIRef(parent_uri)
                    if not _individual_in_oneof(graph, parent_ref, ind_ref):
                        _update_one_of(graph, parent_ref, [ind_ref])
                        graph_modified = True
                continue

            if not parent_uri:
                logger.error(
                    "Ontology: skipping individual %s — no rdfs:range on property %s "
                    "(untyped individual would be created)",
                    uri, prop_uri,
                )
                continue

            graph.add((ind_ref, RDF.type, OWL.NamedIndividual))
            graph.add((ind_ref, RDF.type, URIRef(parent_uri)))
            new_by_parent.setdefault(parent_uri, []).append(ind_ref)
            graph.add((ind_ref, RDFS.label, Literal(_to_label(_uri_fragment(uri)))))

            existing.add(uri)
            added.append(uri)
            logger.info(
                "Ontology: added individual %s (type %s)",
                uri, parent_uri,
            )

    # Extend owl:oneOf enumerations for any parent that has one
    for parent_uri, ind_refs in new_by_parent.items():
        _update_one_of(graph, URIRef(parent_uri), ind_refs)

    if added or graph_modified:
        fmt = "xml" if ontology_path.lower().endswith((".owl", ".rdf")) else "turtle"
        graph.serialize(destination=ontology_path, format=fmt)
        logger.info("Ontology saved to %s (%d new individuals added)", ontology_path, len(added))

    return len(added), added
