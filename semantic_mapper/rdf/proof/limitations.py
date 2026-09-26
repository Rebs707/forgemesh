# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 4: Limitations submodule.

Three mechanical limitation kinds:
  OPEN_WORLD  — absence of a triple doesn't mean the fact is false.
  CARDINALITY — owl:minCardinality / owl:maxCardinality declares a constraint
                that may not hold for all instances in the graph.
  ANNOTATED   — fm:limitation annotation on a class or property.
"""
from __future__ import annotations

from typing import List, Optional

from rdflib import BNode, Graph, OWL, RDF, RDFS, URIRef
from rdflib.namespace import XSD

from semantic_mapper.ontology.owl_utils import domain_class_uris
from semantic_mapper.rdf.proof._fm import FM
from semantic_mapper.rdf.proof.models import Limitation


def _local_name(uri: str) -> str:
    return uri.split("#")[-1].split("/")[-1]


def _class_label(class_uri: str, ontology_graph: Graph) -> str:
    ref = URIRef(class_uri)
    label = next(ontology_graph.objects(ref, RDFS.label), None)
    return str(label) if label else _local_name(class_uri)


def _prop_label(prop_uri: str, ontology_graph: Graph) -> str:
    ref = URIRef(prop_uri)
    label = next(ontology_graph.objects(ref, RDFS.label), None)
    if label:
        return str(label)
    import re
    name = _local_name(prop_uri)
    name = re.sub(r"^has", "", name)
    name = re.sub(r"([A-Z])", r" \1", name).strip().lower()
    return name or _local_name(prop_uri)


def _object_properties_on_class(class_uri: str, ontology_graph: Graph) -> List[str]:
    result: List[str] = []
    for prop in ontology_graph.subjects(RDF.type, OWL.ObjectProperty):
        # Expand owl:unionOf so a shared abstract (e.g. hasPatient) is reported on
        # each union-member class. Empty set = no declared domain → applies to all.
        domains = domain_class_uris(ontology_graph, prop)
        if not domains or class_uri in domains:
            result.append(str(prop))
    return result


def _cardinality_limitations(
    class_uri: str, ontology_graph: Graph
) -> List[Limitation]:
    """Emit CARDINALITY limitations for properties with declared min/max cardinality."""
    limitations: List[Limitation] = []
    class_ref = URIRef(class_uri)

    # Walk subClassOf restrictions on the class
    for restriction in ontology_graph.objects(class_ref, RDFS.subClassOf):
        if not isinstance(restriction, BNode):
            continue
        if (restriction, RDF.type, OWL.Restriction) not in ontology_graph:
            continue
        on_prop = next(ontology_graph.objects(restriction, OWL.onProperty), None)
        if on_prop is None:
            continue
        prop_label = _prop_label(str(on_prop), ontology_graph)
        class_label = _class_label(class_uri, ontology_graph)

        min_card = next(ontology_graph.objects(restriction, OWL.minCardinality), None)
        max_card = next(ontology_graph.objects(restriction, OWL.maxCardinality), None)

        if min_card is not None:
            limitations.append(Limitation(
                kind="CARDINALITY",
                text=(
                    f"The ontology declares a minimum cardinality of {min_card} for "
                    f"'{prop_label}' on {class_label}. Instances missing this property "
                    "in the graph may cause the result to undercount."
                ),
                grounding_node_uri=str(restriction),
            ))
        if max_card is not None:
            limitations.append(Limitation(
                kind="CARDINALITY",
                text=(
                    f"The ontology declares a maximum cardinality of {max_card} for "
                    f"'{prop_label}' on {class_label}. Results assume at most "
                    f"{max_card} value(s) per instance."
                ),
                grounding_node_uri=str(restriction),
            ))
    return limitations


def _annotated_limitations(
    class_uris: List[str], prop_uris: List[str], ontology_graph: Graph
) -> List[Limitation]:
    """Read fm:limitation annotations from classes and properties."""
    limitations: List[Limitation] = []
    seen: set = set()
    nodes = [URIRef(u) for u in class_uris + prop_uris]
    for ref in nodes:
        for val in ontology_graph.objects(ref, FM.limitation):
            text = str(val)
            if text not in seen:
                seen.add(text)
                limitations.append(Limitation(
                    kind="ANNOTATED",
                    text=text,
                    grounding_node_uri=str(ref),
                ))
    return limitations


def build_limitations(
    queried_class_uris: List[str],
    constrained_property_uris: List[str],
    ontology_graph: Optional[Graph],
) -> List[Limitation]:
    """Build the complete limitations list for the proof record."""
    if ontology_graph is None:
        return [Limitation(
            kind="OPEN_WORLD",
            text=(
                "Results are limited to facts explicitly present in the graph. "
                "The absence of a triple does not mean the corresponding fact is false — "
                "it may simply not have been recorded."
            ),
        )]

    limitations: List[Limitation] = []

    # OPEN_WORLD: one per object property on queried classes
    for class_uri in queried_class_uris:
        class_label = _class_label(class_uri, ontology_graph)
        for prop_uri in _object_properties_on_class(class_uri, ontology_graph):
            prop_label = _prop_label(prop_uri, ontology_graph)
            range_ = next(ontology_graph.objects(URIRef(prop_uri), RDFS.range), None)
            range_label = _class_label(str(range_), ontology_graph) if range_ else "a related entity"
            limitations.append(Limitation(
                kind="OPEN_WORLD",
                text=(
                    f"The absence of a '{prop_label}' triple on a {class_label} instance "
                    f"does not mean it has no associated {range_label} — "
                    "it may simply not be recorded in this graph."
                ),
                grounding_node_uri=str(URIRef(prop_uri)),
            ))

    # CARDINALITY
    for class_uri in queried_class_uris:
        limitations.extend(_cardinality_limitations(class_uri, ontology_graph))

    # ANNOTATED: fm:limitation on classes and constrained properties
    limitations.extend(_annotated_limitations(
        queried_class_uris, constrained_property_uris, ontology_graph
    ))

    # Deduplicate by text
    seen_texts: set = set()
    deduped: List[Limitation] = []
    for lim in limitations:
        if lim.text not in seen_texts:
            seen_texts.add(lim.text)
            deduped.append(lim)

    return deduped
