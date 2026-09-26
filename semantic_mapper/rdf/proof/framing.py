# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 2: Result framing submodule.

Extracts the primary class, joined classes, applied constraints, and row
cardinality from parsed SPARQL + result set + ontology graph.

Key invariants:
  - PREFIX declarations are expanded to full URIs before any extraction,
    so prefixed CURIEs (ont:hasProp) and angle-bracket URIs (<...>) are
    treated identically.
  - Cardinality on scalar aggregate queries (COUNT without GROUP BY) is the
    count value itself, not the number of result rows (which is always 1).
    This aligns the semantic cardinality with what the lineage layer traces.

Raises OntologyGroundingError if a FILTER references a property URI absent
from the ontology (when an ontology is supplied).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

from rdflib import Graph, OWL, RDF, RDFS, URIRef

from semantic_mapper.core.exceptions import OntologyGroundingError
from semantic_mapper.rdf.proof.models import AppliedConstraint, ResultFraming

# ── PREFIX expansion ──────────────────────────────────────────────────────────

_PREFIX_DECL_RE = re.compile(r"PREFIX\s+(\w+)\s*:\s*<([^>]+)>", re.IGNORECASE)


def expand_prefixes(sparql: str) -> str:
    """Replace prefixed CURIEs with full-URI angle-bracket form.

    PREFIX ont: <http://example.org/ont#>
    ?s ont:hasProp ?o  →  ?s <http://example.org/ont#hasProp> ?o

    Applied before all URI extraction so that prefixed and angle-bracket
    forms are treated identically by every downstream regex.
    Standard keywords (a, rdf:type, xsd:...) that appear in standard
    prefix declarations are expanded normally; the 'a' keyword alias for
    rdf:type is unaffected because it is not of the form 'prefix:local'.
    """
    prefixes: Dict[str, str] = {}
    for m in _PREFIX_DECL_RE.finditer(sparql):
        prefixes[m.group(1)] = m.group(2)

    if not prefixes:
        return sparql

    for prefix, base_uri in prefixes.items():
        # Match prefix:localName not already inside angle brackets or quoted strings.
        # Negative lookbehind for < prevents re-expanding already-expanded URIs.
        pattern = re.compile(r"(?<![<\"/])\b" + re.escape(prefix) + r":(\w+)")
        sparql = pattern.sub(lambda m: f"<{base_uri}{m.group(1)}>", sparql)

    return sparql


# ── SPARQL parse helpers ──────────────────────────────────────────────────────

# ?x a <URI> or ?x rdf:type <URI>  (angle-bracket form; prefix-expanded before use)
_TYPE_FULL_URI_RE = re.compile(
    r"\?\w+\s+(?:a|rdf:type|<[^>]+>)\s+<([^>]+)>", re.IGNORECASE
)
# Object/data property triples: ?s <URI> ?o  (angle-bracket form after expansion)
_PROP_TRIPLE_RE = re.compile(r"\?(\w+)\s+<([^>]+)>\s+\?(\w+)")
# FILTER(...) blocks
_FILTER_RE = re.compile(r"\bFILTER\s*\(", re.IGNORECASE)
# Literal binding: ?s <URI> "value"  or  ?s <URI> 123
_LITERAL_BINDING_RE = re.compile(r"\?(\w+)\s+<([^>]+)>\s+(?:\"[^\"]*\"|[-\d]+)")
# Aggregate detection
_AGGREGATE_RE = re.compile(r"\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\(", re.IGNORECASE)
_GROUP_BY_RE = re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE)


def _extract_type_uris(sparql: str) -> List[str]:
    """Return full URIs from ?x a <URI> / ?x rdf:type <URI> patterns."""
    return _TYPE_FULL_URI_RE.findall(sparql)


def _extract_filter_texts(sparql: str) -> List[str]:
    """Return each FILTER(…) block as a raw string."""
    blocks: List[str] = []
    for m in _FILTER_RE.finditer(sparql):
        depth = 0
        i = m.end() - 1
        while i < len(sparql):
            if sparql[i] == "(":
                depth += 1
            elif sparql[i] == ")":
                depth -= 1
                if depth == 0:
                    blocks.append(sparql[m.start() : i + 1])
                    break
            i += 1
    return blocks


def _extract_prop_uris(sparql: str) -> List[str]:
    """Return all property URIs from ?s <URI> ?o triples."""
    return [m.group(2) for m in _PROP_TRIPLE_RE.finditer(sparql)]


def _extract_literal_binding_uris(sparql: str) -> List[str]:
    """Return URIs from ?s <URI> "literal" / ?s <URI> 123 patterns."""
    return [m.group(2) for m in _LITERAL_BINDING_RE.finditer(sparql)]


def _semantic_cardinality(sparql: str, rows: List[Dict[str, Any]]) -> int:
    """Return the entity count that the user actually asked about.

    For non-aggregate queries: len(rows) — each row is one entity.

    For scalar aggregate queries (COUNT/SUM without GROUP BY returning a
    single row): the count value itself, not 1.  This aligns with what the
    lineage layer traces — the 60 admissions, not the 1-row result set.

    For grouped aggregates (GROUP BY present): len(rows) — the number of
    groups is the meaningful cardinality.
    """
    if not _AGGREGATE_RE.search(sparql):
        return len(rows)
    if _GROUP_BY_RE.search(sparql):
        return len(rows)
    if len(rows) != 1:
        return len(rows)
    # Scalar aggregate: look for the first non-negative integer column value
    row = rows[0]
    for val in row.values():
        if val is None:
            continue
        try:
            n = int(str(val))
            if n >= 0:
                return n
        except (ValueError, TypeError):
            continue
    return len(rows)


def _prop_definition(prop_uri: str, ontology_graph: Optional[Graph]) -> str:
    if ontology_graph is None:
        return _local_name(prop_uri)
    from semantic_mapper.rdf.proof._fm import FM
    ref = URIRef(prop_uri)
    for pred in (FM.interpretation, RDFS.comment):
        val = next(ontology_graph.objects(ref, pred), None)
        if val:
            return str(val)
    return _local_name(prop_uri)


def _local_name(uri: str) -> str:
    return uri.split("#")[-1].split("/")[-1]


def _prop_range_uri(prop_uri: str, ontology_graph: Optional[Graph]) -> Optional[str]:
    if ontology_graph is None:
        return None
    val = next(ontology_graph.objects(URIRef(prop_uri), RDFS.range), None)
    return str(val) if val else None


def _is_object_property(prop_uri: str, ontology_graph: Optional[Graph]) -> bool:
    if ontology_graph is None:
        return False
    return (URIRef(prop_uri), RDF.type, OWL.ObjectProperty) in ontology_graph


def _ontology_has_property(prop_uri: str, ontology_graph: Optional[Graph]) -> bool:
    if ontology_graph is None:
        return True
    ref = URIRef(prop_uri)
    return any(
        (ref, RDF.type, t) in ontology_graph
        for t in (OWL.ObjectProperty, OWL.DatatypeProperty, OWL.AnnotationProperty)
    ) or any(ontology_graph.triples((ref, None, None)))


# ── Public API ────────────────────────────────────────────────────────────────

def build_framing(
    sparql: str,
    rows: List[Dict[str, Any]],
    ontology_graph: Optional[Graph],
) -> ResultFraming:
    """Derive a ResultFraming from the SPARQL query and its result set.

    Expands PREFIX declarations before extraction so that prefixed CURIEs
    and angle-bracket URIs are treated identically.

    Raises OntologyGroundingError if any FILTER or data property triple
    references a URI absent from the supplied ontology graph.
    """
    sparql = expand_prefixes(sparql)

    type_uris = _extract_type_uris(sparql)
    prop_uris = _extract_prop_uris(sparql)
    filter_texts = _extract_filter_texts(sparql)
    literal_binding_uris = _extract_literal_binding_uris(sparql)

    # Constrained property URIs
    filter_content = " ".join(filter_texts)
    constrained_prop_set: Set[str] = set()

    for uri in literal_binding_uris:
        constrained_prop_set.add(uri)

    for m in _PROP_TRIPLE_RE.finditer(sparql):
        uri = m.group(2)
        obj_var = m.group(3)
        if f"?{obj_var}" in filter_content:
            constrained_prop_set.add(uri)

    constrained_prop_uris: List[str] = list(constrained_prop_set)

    # Grounding check
    if ontology_graph is not None:
        for uri in constrained_prop_uris:
            if not _ontology_has_property(uri, ontology_graph):
                raise OntologyGroundingError(
                    f"FILTER/constraint references property <{uri}> which is absent from "
                    "the ontology subgraph. The narrator cannot produce a warranted "
                    "AppliedConstraint."
                )

    # Primary class
    primary_class_uri = type_uris[0] if type_uris else ""
    if not primary_class_uri and ontology_graph is not None:
        for uri in prop_uris:
            range_ = _prop_range_uri(uri, ontology_graph)
            if range_:
                primary_class_uri = range_
                break

    # Joined classes
    joined: List[str] = []
    seen_joined: Set[str] = set()
    for uri in prop_uris:
        if _is_object_property(uri, ontology_graph):
            range_ = _prop_range_uri(uri, ontology_graph) or ""
            if range_ and range_ != primary_class_uri and range_ not in seen_joined:
                joined.append(range_)
                seen_joined.add(range_)
    for t in type_uris[1:]:
        if t not in seen_joined and t != primary_class_uri:
            joined.append(t)
            seen_joined.add(t)

    # Applied constraints
    constraints: List[AppliedConstraint] = []
    seen_constraint_uris: Set[str] = set()
    for uri in constrained_prop_uris:
        if uri in seen_constraint_uris:
            continue
        seen_constraint_uris.add(uri)
        filter_form_parts = []
        for m in _PROP_TRIPLE_RE.finditer(sparql):
            if m.group(2) == uri:
                obj_var = m.group(3)
                for ft in filter_texts:
                    if f"?{obj_var}" in ft:
                        filter_form_parts.append(ft.strip())
        if not filter_form_parts:
            for m in _LITERAL_BINDING_RE.finditer(sparql):
                if m.group(2) == uri:
                    filter_form_parts.append(m.group(0).strip())
                    break
        filter_form = "; ".join(filter_form_parts) or uri
        constraints.append(AppliedConstraint(
            property_uri=uri,
            filter_form=filter_form,
            property_definition=_prop_definition(uri, ontology_graph),
            range_uri=_prop_range_uri(uri, ontology_graph),
        ))

    return ResultFraming(
        cardinality=_semantic_cardinality(sparql, rows),
        primary_class_uri=primary_class_uri,
        joined_class_uris=joined,
        constraints_applied=constraints,
    )
