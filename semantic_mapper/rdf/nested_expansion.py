# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Detect and parse nested/packed cells — a column whose values hold a stringified
list of flat ``{key: value}`` objects (a denormalised 1-to-N group), e.g.::

    [{'discipline': 'Alpine Skiing', 'event': "Men's Slalom"}, ...]

The RDF layer uses this to "open up" such a column at the graph boundary — exploding
one gold row into N edges to first-class entities — while gold keeps the opaque text
column. Dataset-agnostic: keys are discovered from the data, never hardcoded.
"""
from __future__ import annotations

import ast
import json
import re
from typing import Any, Dict, List, Optional, Sequence

from rdflib import URIRef, Literal, Namespace, RDF, RDFS, OWL, XSD

_FM = Namespace("http://forgemesh.ai/ontology#")
# fm:multiValued — marks an object property as genuinely 1-to-N by construction
# (it explodes a packed multi-valued cell). The C2 cardinality check honours this
# and exempts the property from its single-valued "double-mint" ratio heuristic.
_FM_MULTIVALUED = _FM.multiValued


def _singular(word: str) -> str:
    w = word.strip()
    if len(w) > 3 and w.lower().endswith("ies"):
        return w[:-3] + "y"
    if w.lower().endswith("s") and not w.lower().endswith("ss"):
        return w[:-1]
    return w


def _pascal(word: str) -> str:
    return "".join(p.capitalize() for p in re.split(r"[_\s]+", word.strip()) if p)


def nested_term_names(column: str, keys: Sequence[str]) -> Dict[str, Any]:
    """Derive ontology term names for an exploded nested column, from the column and
    its discovered keys. e.g. column='events', keys=['discipline','event'] →
    child class 'Event', parent property 'hasEvent', key properties
    {discipline: 'eventDiscipline', event: 'eventName'} (a key matching the entity name
    becomes the primary <child>Name label).
    """
    child = _pascal(_singular(column))                  # events -> Event
    cl = child[:1].lower() + child[1:]
    key_props: Dict[str, str] = {}
    for k in keys:
        if _pascal(_singular(k)) == child:              # 'event' key on an Event entity
            key_props[k] = f"{cl}Name"                  # -> eventName
        else:
            key_props[k] = f"{cl}{_pascal(k)}"          # discipline -> eventDiscipline
    return {"child": child, "parent_prop": f"has{child}", "key_props": key_props}


def _norm(v) -> str:
    return str(v).strip()


def reconcile_nested_keys(
    term_names: dict,
    key_samples: Dict[str, Sequence[Any]],
    reconcile_by_key: Dict[str, Dict[str, str]],
    individual_class: Dict[str, str],
    obj_prop_by_class: Dict[str, str],
    min_fraction: float = 0.95,
) -> dict:
    """Promote a child-key from a literal to an object link to an EXISTING value-mapped
    vocabulary, when its sampled values resolve through that vocabulary's map.

    General, data-driven — no field names are hardcoded. A key only reconciles against
    value-maps drawn from columns of the SAME field name (``reconcile_by_key[key]``), so
    homonyms across columns (event "Men" vs gender "Men") cannot cross-link. A key is
    promoted only when ≥ ``min_fraction`` of its non-null sampled values resolve to
    individuals of a SINGLE class — otherwise it stays a literal (mixed/ambiguous keys
    are left alone).

    Returns a NEW term_names with a ``key_links`` map
    ``{key: {property, class_uri, reused}}`` and those keys removed from ``key_props``.
    ``property`` reuses an existing ``has<Class>`` object property when one exists
    (``reused=True``), else the key's own derived property is repurposed.
    """
    out = dict(term_names)
    key_props = dict(out.get("key_props", {}))
    key_links = dict(out.get("key_links", {}))
    for k in list(key_props):
        index = reconcile_by_key.get(k)
        if not index:
            continue
        vals = [v for v in key_samples.get(k, []) if v is not None and _norm(v)]
        if not vals:
            continue
        hits = [index.get(_norm(v)) for v in vals]
        resolved = [u for u in hits if u]
        if len(resolved) / len(vals) < min_fraction:
            continue
        classes = {individual_class.get(u) for u in resolved if individual_class.get(u)}
        if len(classes) != 1:
            continue                                 # ambiguous target class → keep literal
        cls = next(iter(classes))
        reused_prop = obj_prop_by_class.get(cls)
        key_links[k] = {
            "property": reused_prop or key_props[k],  # reuse has<Class>, else repurpose derived
            "class_uri": cls,
            "reused": bool(reused_prop),
        }
        del key_props[k]
    out["key_props"] = key_props
    out["key_links"] = key_links
    return out


def explode_nested_cell(subject, value, term_names: dict, uri_factory, onto: str,
                        parent_prop=None, reconcile_by_key=None) -> list:
    """Return the triples for one row's nested column: subject -> <parent_prop> -> <child>
    (a shared entity keyed by its values), <child> a <Child>, and each key as a literal.

    ``parent_prop`` is the edge property — normally the packed column's OWN mapped
    property URI (so unpacking repurposes that property from an opaque literal into an
    object link), falling back to ``onto + term_names['parent_prop']`` when not given.
    """
    objs = parse_nested_cell(value)
    if not objs:
        return []
    child_cls = URIRef(onto + term_names["child"])
    parent_prop = (
        URIRef(parent_prop) if parent_prop is not None
        else URIRef(onto + term_names["parent_prop"])
    )
    key_links = term_names.get("key_links", {})
    reconcile_by_key = reconcile_by_key or {}
    triples = []
    for obj in objs:
        # Shared identity: the same {discipline, event} object is ONE entity that every
        # parent referencing it points at (so 96 athletes -> 1 'Men's Slalom' Event).
        ident = "|".join(f"{k}={obj[k]}" for k in sorted(obj) if obj[k] is not None)
        child_uri = uri_factory.individual(term_names["child"].lower(), ident)
        triples.append((subject, parent_prop, child_uri))
        triples.append((child_uri, RDF.type, child_cls))
        for k, v in obj.items():
            if v is None:
                continue
            if k in key_links:
                # Reconciled key: link to the EXISTING value-mapped individual rather
                # than emit a duplicate literal. Unresolved value → no triple (leave the
                # child incomplete on this key) so the object property never carries a
                # literal (avoids a mixed range).
                ind = reconcile_by_key.get(k, {}).get(_norm(v))
                if ind:
                    triples.append((child_uri, URIRef(onto + key_links[k]["property"]), URIRef(ind)))
                continue
            prop = term_names["key_props"].get(k)
            if prop:
                triples.append((child_uri, URIRef(onto + prop), Literal(str(v))))
    return triples


def nested_ontology_triples(parent_class_uri, term_names: dict, onto: str,
                            parent_prop=None) -> list:
    """Declare the schema discovered inside the cell: the child Class, the parent->child
    ObjectProperty, and a DatatypeProperty per key.

    ``parent_prop`` is the edge property URI (normally the packed column's own mapped
    property), defaulting to ``onto + term_names['parent_prop']``. ``parent_class_uri``
    is the single subject domain, or None to omit ``rdfs:domain`` (used when the property
    is shared across several subject classes, where multiple plain domains would wrongly
    intersect)."""
    child = URIRef(onto + term_names["child"])
    pp = (
        URIRef(parent_prop) if parent_prop is not None
        else URIRef(onto + term_names["parent_prop"])
    )
    triples = [
        (child, RDF.type, OWL.Class),
        (child, RDFS.label, Literal(term_names["child"])),
        (pp, RDF.type, OWL.ObjectProperty),
        (pp, RDFS.range, child),
        # An exploded packed column is 1-to-N by construction — mark it so the C2
        # cardinality check does not flag the expected fan-out as a double-mint.
        (_FM_MULTIVALUED, RDF.type, OWL.AnnotationProperty),
        (pp, _FM_MULTIVALUED, Literal(True)),
    ]
    if parent_class_uri:
        triples.append((pp, RDFS.domain, URIRef(parent_class_uri)))
    for prop in term_names["key_props"].values():
        p = URIRef(onto + prop)
        triples += [
            (p, RDF.type, OWL.DatatypeProperty),
            (p, RDFS.domain, child),
            (p, RDFS.range, XSD.string),
        ]
    # Reconciled keys: each links the child to an existing value-mapped vocabulary.
    # Only declare a NEW (repurposed) property; a reused has<Class> property keeps its
    # existing declaration (re-adding domain would intersect it onto the child class).
    for link in term_names.get("key_links", {}).values():
        if link.get("reused"):
            continue
        p = URIRef(onto + link["property"])
        triples += [
            (p, RDF.type, OWL.ObjectProperty),
            (p, RDFS.domain, child),
            (p, RDFS.range, URIRef(link["class_uri"])),
        ]
    return triples


def parse_nested_cell(value: Any) -> Optional[List[dict]]:
    """Return the list of flat dicts a cell encodes, or None if it isn't one.

    Accepts JSON (``[{"a": 1}]``) and Python-repr (``[{'a': 1}]``) list-of-object
    cells. Returns None for scalars, list-of-scalars, or unparseable values.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not (s.startswith("[") and s.endswith("]")):
        return None
    parsed = None
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(s)
            break
        except Exception:
            continue
    if not isinstance(parsed, list) or not parsed:
        return None
    if not all(isinstance(x, dict) for x in parsed):
        return None
    return [{str(k): v for k, v in obj.items()} for obj in parsed]


def detect_nested_columns(
    rows: Sequence[dict],
    columns: Sequence[str],
    *,
    min_fraction: float = 0.5,
    sample: int = 200,
) -> Dict[str, List[str]]:
    """Return ``{column: sorted(discovered keys)}`` for columns whose non-null cells
    are predominantly lists of homogeneous ``{k: v}`` objects.

    A column qualifies when ≥ ``min_fraction`` of its sampled non-null cells parse as
    list-of-objects. The discovered keys are the union of object keys observed — these
    name the per-element properties (e.g. ``discipline``, ``event``).
    """
    result: Dict[str, List[str]] = {}
    for col in columns:
        if col.startswith("_"):  # audit columns never carry domain nesting
            continue
        nonnull = [r.get(col) for r in list(rows)[:sample]]
        nonnull = [v for v in nonnull if v is not None and str(v).strip()]
        if not nonnull:
            continue
        parsed_ok = 0
        keys: set = set()
        for v in nonnull:
            objs = parse_nested_cell(v)
            if objs is not None:
                parsed_ok += 1
                for obj in objs:
                    keys.update(obj.keys())
        if keys and parsed_ok / len(nonnull) >= min_fraction:
            result[col] = sorted(keys)
    return result
