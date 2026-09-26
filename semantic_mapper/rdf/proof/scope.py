# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 3: Scope assumption submodule.

Mechanically derives what was NOT constrained by the query. For every
property declared on the queried class(es) in the ontology, any property
absent from the query's WHERE clause is an implicit "all values included"
assumption that must be surfaced.
"""
from __future__ import annotations

from typing import List, Optional, Set

from rdflib import Graph, OWL, RDF, RDFS, URIRef

from semantic_mapper.ontology.owl_utils import domain_class_uris
from semantic_mapper.rdf.proof._fm import FM
from semantic_mapper.rdf.proof.models import ScopeAssumption


def _local_name(uri: str) -> str:
    return uri.split("#")[-1].split("/")[-1]


def _prop_label(prop_uri: str, ontology_graph: Graph) -> str:
    """Return rdfs:label or a camelCase-split local name."""
    ref = URIRef(prop_uri)
    label = next(ontology_graph.objects(ref, RDFS.label), None)
    if label:
        return str(label)
    # Convert camelCase to words: hasAdmissionType → "admission type"
    import re
    name = _local_name(prop_uri)
    name = re.sub(r"^has", "", name)
    name = re.sub(r"([A-Z])", r" \1", name).strip().lower()
    return name or _local_name(prop_uri)


def _prop_range_label(prop_uri: str, ontology_graph: Graph) -> str:
    """Return a human-readable label for the range of a property."""
    ref = URIRef(prop_uri)
    range_ = next(ontology_graph.objects(ref, RDFS.range), None)
    if range_ is None:
        return "any value"
    label = next(ontology_graph.objects(range_, RDFS.label), None)
    if label:
        return str(label)
    return _local_name(str(range_))


def _fm_scope_default(prop_uri: str, ontology_graph: Graph) -> Optional[str]:
    val = next(ontology_graph.objects(URIRef(prop_uri), FM.scope_default), None)
    return str(val) if val else None


def _properties_on_class(class_uri: str, ontology_graph: Graph) -> List[str]:
    """Return URIs for all OWL properties whose rdfs:domain is *class_uri* (or a superclass)."""
    class_ref = URIRef(class_uri)

    # Collect this class plus its superclasses (direct only — no full inference)
    ancestors: Set[URIRef] = {class_ref}
    for _, _, parent in ontology_graph.triples((class_ref, RDFS.subClassOf, None)):
        if isinstance(parent, URIRef):
            ancestors.add(parent)

    ancestor_uris = {str(a) for a in ancestors}
    result: List[str] = []
    seen: Set[str] = set()
    for prop_type in (OWL.ObjectProperty, OWL.DatatypeProperty):
        for prop in ontology_graph.subjects(RDF.type, prop_type):
            # Expand owl:unionOf so a shared abstract (e.g. hasPatient) counts as a
            # property on each of its union-member classes. Empty set = no declared
            # domain → applies to all classes.
            domains = domain_class_uris(ontology_graph, prop)
            if not domains or (domains & ancestor_uris):
                uri = str(prop)
                if uri not in seen:
                    seen.add(uri)
                    result.append(uri)
    return result


def build_scope_assumptions(
    queried_class_uris: List[str],
    constrained_property_uris: Set[str],
    ontology_graph: Optional[Graph],
) -> List[ScopeAssumption]:
    """Return scope assumptions: ontology properties on queried classes not in the query.

    Returns an empty list when no ontology is available.
    """
    if ontology_graph is None or not queried_class_uris:
        return []

    available: List[str] = []
    seen: Set[str] = set()
    for class_uri in queried_class_uris:
        for p in _properties_on_class(class_uri, ontology_graph):
            if p not in seen:
                seen.add(p)
                available.append(p)

    assumptions: List[ScopeAssumption] = []
    for prop_uri in sorted(available):  # alphabetic for determinism
        if prop_uri in constrained_property_uris:
            continue
        label = _prop_label(prop_uri, ontology_graph)
        range_label = _prop_range_label(prop_uri, ontology_graph)
        assumptions.append(ScopeAssumption(
            unconstrained_property_uri=prop_uri,
            property_label=label,
            implied_inclusion=f"all values of {range_label} included",
            annotated_default=_fm_scope_default(prop_uri, ontology_graph),
        ))

    return assumptions
