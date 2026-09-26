# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Generic fixtures for terminology-binding tests.

Nothing here is dataset-specific: tables/columns/classes carry neutral names
(``raw_events``, ``status_code``, ``StatusCode``) chosen only to illustrate the
structural pattern, exactly as CONTRIBUTING.md permits.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Dict, List, Optional

from rdflib import Graph, Literal, OWL, RDF, RDFS, URIRef

from semantic_mapper.db.models import ColumnProfile, TableProfile

FM = "http://forgemesh.ai/ontology#"
BASE = "http://mesh.local/ontology/testds#"


def make_settings(
    *,
    enabled: bool = True,
    sources: Optional[Dict] = None,
    overlap: float = 0.8,
    floor: float = 0.5,
) -> SimpleNamespace:
    """A lightweight settings stand-in (bind_terminology reads via getattr)."""
    return SimpleNamespace(
        TERMINOLOGY_BINDING_ENABLED=enabled,
        TERMINOLOGY_SOURCES=sources or {},
        TERM_BIND_OVERLAP_THRESHOLD=overlap,
        TERM_BIND_CONFIDENCE_FLOOR=floor,
    )


def offline_source_config(
    source_id: str = "testsys",
    *,
    system_label: str = "TestSys",
    codes: Optional[Dict[str, str]] = None,
    iri_template: str = "http://example.org/concept/{code}",
    system_iri: Optional[str] = "http://example.org/system",
) -> Dict:
    """A TERMINOLOGY_SOURCES entry backed by an inline offline code set."""
    return {
        source_id: {
            "adapter": "offline_codeset",
            "system_label": system_label,
            "codes": codes or {},
            "iri_template": iri_template,
            "system_iri": system_iri,
        }
    }


def make_profile(table: str, col: str, values: List[str]) -> TableProfile:
    """A TableProfile carrying one low-cardinality coded column (top_values set)."""
    cp = ColumnProfile(
        name=col,
        inferred_type="VARCHAR",
        is_nullable=True,
        is_primary_key=False,
        null_rate=0.0,
        distinct_count=len(values),
        top_values=[{"value": v, "count": 1} for v in values],
    )
    return TableProfile(table_name=table, row_count=len(values), columns={col: cp})


def build_vocab_graph(
    table: str,
    col: str,
    class_local: str,
    values: List[str],
    *,
    base: str = BASE,
):
    """Build a graph mimicking ``materialise_categorical_individuals`` output: a
    vocab class, an ObjectProperty with FK provenance + range, and one individual
    per value (raw value as ``rdfs:label``). Returns ``(graph, class_uri, {value: ind_uri})``.
    """
    g = Graph()
    cls = URIRef(base + class_local)
    g.add((cls, RDF.type, OWL.Class))

    prop = URIRef(base + "has" + class_local)
    g.add((prop, RDF.type, OWL.ObjectProperty))
    g.add((prop, RDFS.range, cls))
    g.add((prop, URIRef(FM + "sourceForeignKey"), Literal(f"{table}.{col} -> {class_local}")))

    inds: Dict[str, URIRef] = {}
    for i, raw in enumerate(values):
        ind = URIRef(f"{base}ind_{class_local}_{i}")
        g.add((ind, RDF.type, OWL.NamedIndividual))
        g.add((ind, RDF.type, cls))
        g.add((ind, RDFS.label, Literal(raw)))
        inds[raw] = ind
    return g, cls, inds


def build_datatype_graph(table: str, col: str, prop_local: str, *, base: str = BASE):
    """A high-cardinality coded column modelled as a DatatypeProperty (no
    individuals) with ``fm:sourceColumn`` provenance. Returns ``(graph, prop_uri)``."""
    g = Graph()
    prop = URIRef(base + prop_local)
    g.add((prop, RDF.type, OWL.DatatypeProperty))
    g.add((prop, URIRef(FM + "sourceColumn"), Literal(f"{table}.{col}")))
    return g, prop
