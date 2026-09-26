# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Small OWL graph helpers shared across the ontology and proof layers."""
from __future__ import annotations

from typing import Set

from rdflib import OWL, RDFS, BNode, Graph, URIRef
from rdflib.collection import Collection


def domain_class_uris(graph: Graph, prop) -> Set[str]:
    """Return the named class URIs in *prop*'s rdfs:domain, expanding any
    ``owl:unionOf`` restriction.

    A shared abstract property (e.g. ``hasPatient``) carries its domain as a
    single ``rdfs:domain [ owl:unionOf ( ClassA ClassB ... ) ]`` restriction so
    OWL does not AND the member classes together (which would make the property
    unsatisfiable across disjoint table classes). Consumers that previously read
    ``next(graph.objects(prop, RDFS.domain))`` and assumed a class URI must use
    this helper instead so they see the union members rather than a blank node.

    An empty set means the property has no declared domain (unrestricted — it
    applies to every class).
    """
    if not isinstance(prop, URIRef):
        prop = URIRef(str(prop))
    out: Set[str] = set()
    for dom in graph.objects(prop, RDFS.domain):
        if isinstance(dom, URIRef):
            out.add(str(dom))
        elif isinstance(dom, BNode):
            for lst in graph.objects(dom, OWL.unionOf):
                out.update(
                    str(c) for c in Collection(graph, lst) if isinstance(c, URIRef)
                )
    return out
