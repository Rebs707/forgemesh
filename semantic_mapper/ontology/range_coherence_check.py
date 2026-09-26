# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Range coherence checks for generated ontologies.

Splits the overloaded DANGLING_RANGE error into three distinct rules:

  MIXED_RANGE      a single property declares both a class range (ontology
                   namespace) and an XSD literal range. Semantically the value
                   must satisfy the intersection of all ranges, so a
                   class-and-scalar property is unsatisfiable. This is the
                   ObjectProperty-level sibling of the SOURCE_COLUMN_COLLISION
                   (FK + scalar) defect.

  MULTI_RANGE      a property declares more than one range of the same kind
                   (e.g. two XSD ranges, or two class ranges). Multiple
                   rdfs:range is conjunction in OWL, so two classes means the
                   value must be an instance of both. Usually a generation
                   slip, not intent.

  DANGLING_RANGE   a class-namespace range that points at a class never
                   declared in the ontology. The original, genuine dangling
                   case.

  WRONG_RANGE_TYPE a property typed owl:ObjectProperty whose only range(s) are
                   XSD literals (no class range at all). Nothing is "mixed"
                   because there is no class side, but the property type is
                   wrong: an ObjectProperty should range over a class. This is
                   the XSD-only-on-ObjectProperty case that would otherwise
                   fall through every other bucket silently.

Run against an rdflib graph. No network, no reasoner required.
"""

from __future__ import annotations

from dataclasses import dataclass
from rdflib import Graph, RDF, RDFS, OWL, URIRef


XSD_NS = "http://www.w3.org/2001/XMLSchema#"


@dataclass(frozen=True)
class RangeDefect:
    rule: str               # MIXED_RANGE | MULTI_RANGE | DANGLING_RANGE
    property_uri: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.property_uri} :: {self.detail}"


def _local(uri: str) -> str:
    return uri.split("#")[-1].split("/")[-1]


def check_range_coherence(
    graph: Graph,
    ontology_namespace: str,
) -> list[RangeDefect]:
    """
    ontology_namespace: the class/property namespace for this ontology,
    e.g. "http://mesh.local/ontology/mimic_bronze#".
    Ranges in this namespace are treated as class references; ranges in the
    XSD namespace are treated as literals.
    """
    declared_classes = {
        str(s) for s in graph.subjects(RDF.type, OWL.Class)
        if str(s).startswith(ontology_namespace)
    }

    object_props = set(graph.subjects(RDF.type, OWL.ObjectProperty))
    data_props = set(graph.subjects(RDF.type, OWL.DatatypeProperty))
    all_props = object_props | data_props

    owl_thing = "http://www.w3.org/2002/07/owl#Thing"

    defects: list[RangeDefect] = []

    for prop in all_props:
        ranges = [str(r) for r in graph.objects(prop, RDFS.range)]
        if not ranges:
            continue

        # owl:Thing is always a valid range; exclude it from defect logic
        effective = [r for r in ranges if r != owl_thing]
        if not effective:
            continue

        class_ranges = [r for r in effective if r.startswith(ontology_namespace)]
        xsd_ranges = [r for r in effective if r.startswith(XSD_NS)]
        dangling = [
            r for r in class_ranges if r not in declared_classes
        ]
        is_object = prop in object_props
        pretty = [_local(r) for r in ranges]

        # Priority order. At most one of MIXED / WRONG_TYPE / MULTI fires per
        # property; DANGLING is reported independently because an undeclared
        # class target is a distinct, additive fault.

        if class_ranges and xsd_ranges:
            # both a class and a literal range: unsatisfiable
            defects.append(
                RangeDefect(
                    rule="MIXED_RANGE",
                    property_uri=str(prop),
                    detail=f"class and literal ranges on one property: {pretty}",
                )
            )
        elif is_object and xsd_ranges and not class_ranges:
            # ObjectProperty whose only range is a literal: wrong property type.
            # This is the XSD-only case that fell through every other bucket.
            defects.append(
                RangeDefect(
                    rule="WRONG_RANGE_TYPE",
                    property_uri=str(prop),
                    detail=f"ObjectProperty with only XSD range(s): {pretty}",
                )
            )
        elif len(class_ranges) > 1 or len(xsd_ranges) > 1:
            defects.append(
                RangeDefect(
                    rule="MULTI_RANGE",
                    property_uri=str(prop),
                    detail=f"multiple ranges of one kind (conjunction): {pretty}",
                )
            )

        # DANGLING_RANGE: reported once per undeclared class target, in
        # addition to any of the above. A property can be both MIXED and point
        # at an undeclared class.
        for r in dangling:
            defects.append(
                RangeDefect(
                    rule="DANGLING_RANGE",
                    property_uri=str(prop),
                    detail=f"range {_local(r)} is not a declared class",
                )
            )

    return defects


def summarise(defects: list[RangeDefect]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for d in defects:
        counts[d.rule] = counts.get(d.rule, 0) + 1
    return counts


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        print("usage: range_coherence_check.py <ontology.owl> <namespace>")
        sys.exit(2)

    g = Graph()
    g.parse(sys.argv[1])
    found = check_range_coherence(g, sys.argv[2])

    counts = summarise(found)
    print("Range coherence summary:")
    for rule in ("MIXED_RANGE", "WRONG_RANGE_TYPE", "MULTI_RANGE", "DANGLING_RANGE"):
        print(f"  {rule}: {counts.get(rule, 0)}")
    print()
    for d in found:
        print(d)
