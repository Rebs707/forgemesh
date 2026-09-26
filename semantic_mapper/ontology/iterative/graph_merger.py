# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Graph merger for the iterative ontology generator.

Merges a validated OntologyFragment's rdflib.Graph into the accumulating
ontology graph, handling duplicate URI and conflicting type assertions.
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import rdflib as _rdflib
import rdflib
from rdflib import OWL, RDF, RDFS, BNode, Graph, URIRef
from rdflib.collection import Collection
from rdflib.namespace import SKOS

from semantic_mapper.ontology.iterative.context_builder import (
    derive_class_local_name,
    is_closed_vocabulary,
    DEFAULT_MAX_NULL_RATE_FOR_CLASS,
    DEFAULT_MIN_SUPPORT_FOR_CLASS,
    DEFAULT_MAX_UNIQUENESS_FOR_CLASS,
)
from semantic_mapper.ontology.iterative.models import AccumulatedClassSummary
from semantic_mapper.ontology.owl_utils import domain_class_uris

logger = logging.getLogger(__name__)

_SKOS_ALT_LABEL = URIRef("http://www.w3.org/2004/02/skos/core#altLabel")

_FM_BASE = "http://forgemesh.ai/ontology#"
FM_SOURCE_TABLE = URIRef(_FM_BASE + "sourceTable")
FM_SOURCE_COLUMN = URIRef(_FM_BASE + "sourceColumn")
FM_SOURCE_FK = URIRef(_FM_BASE + "sourceForeignKey")
FM_SQL_TYPE = URIRef(_FM_BASE + "sqlType")
FM_NULL_RATE = URIRef(_FM_BASE + "nullRate")


@dataclass
class MergeResult:
    new_classes: List[AccumulatedClassSummary] = field(default_factory=list)
    new_props: List[str] = field(default_factory=list)       # URIs of newly added properties
    skipped_duplicates: List[str] = field(default_factory=list)
    conflict_warnings: List[str] = field(default_factory=list)
    domain_range_warnings: List[str] = field(default_factory=list)
    enumeration_warnings: List[str] = field(default_factory=list)
    auto_wired: List[str] = field(default_factory=list)  # "<prop> → <abstract>" strings


def merge(
    accumulating: Graph,
    fragment_graph: Graph,
    table_name: str,
    base_iri: str,
    table_row_count: int = 0,
    max_rows_for_enumeration: int = 1000,
    fk_info: Optional[List[Dict]] = None,
    skipped_tables: Optional[Set[str]] = None,
) -> MergeResult:
    """
    Merge *fragment_graph* into *accumulating*.

    Rules:
    - If a subject URI already exists in *accumulating* with the same rdf:type,
      the duplicate is skipped (no redeclaration).
    - If the same URI already exists with a DIFFERENT rdf:type (e.g. declared as
      DatatypeProperty but fragment claims ObjectProperty), a warning is logged
      and the fragment's triple is skipped.
    - Annotation triples: rdfs:comment and rdfs:label use keep-first (the first table
      to declare the class is authoritative); skos:altLabel is always merged (multi-valued).
    - Returns a MergeResult listing newly added classes (for the embedding index).
    """
    _strip_self_referential_subprops(fragment_graph)
    _split_multi_domain_properties(fragment_graph, base_iri)
    _strip_dual_annotated_object_properties(fragment_graph)
    if fk_info:
        _fix_datatype_fk_properties(fragment_graph, base_iri, fk_info, skipped_tables or set())

    result = MergeResult()
    new_class_uris: List[str] = []

    # Collect existing typed subjects in the accumulating graph
    existing_types: dict = {}
    for s, _, o in accumulating.triples((None, RDF.type, None)):
        existing_types.setdefault(str(s), set()).add(str(o))

    # Collect what the fragment declares
    fragment_types: dict = {}
    for s, _, o in fragment_graph.triples((None, RDF.type, None)):
        fragment_types.setdefault(str(s), set()).add(str(o))

    OWL_STRUCTURAL_TYPES = {
        str(OWL.Class),
        str(OWL.DatatypeProperty),
        str(OWL.ObjectProperty),
        str(OWL.AnnotationProperty),
    }

    for s_str, frag_type_set in fragment_types.items():
        if s_str not in existing_types:
            # New entity — add all triples for this subject
            s_ref = URIRef(s_str)
            for triple in fragment_graph.triples((s_ref, None, None)):
                _add_triple(accumulating, triple)
            # Track new classes for index
            if str(OWL.Class) in frag_type_set:
                new_class_uris.append(s_str)
        else:
            existing_structural = existing_types[s_str] & OWL_STRUCTURAL_TYPES
            fragment_structural = frag_type_set & OWL_STRUCTURAL_TYPES
            if existing_structural and fragment_structural and existing_structural != fragment_structural:
                result.conflict_warnings.append(
                    f"URI {s_str}: already declared as {existing_structural}, "
                    f"fragment claims {fragment_structural} — skipping redeclaration"
                )
                logger.warning(
                    "graph_merger: type conflict for <%s>: existing=%s fragment=%s — skipped",
                    s_str, existing_structural, fragment_structural,
                )
            else:
                # Same structural type — skip rdf:type re-add but merge annotations
                result.skipped_duplicates.append(s_str)
            # Merge annotations: keep-first for label/comment; always-merge for altLabel.
            s_ref = URIRef(s_str)
            for ann_pred in (RDFS.label, _SKOS_ALT_LABEL, RDFS.comment):
                if ann_pred in (RDFS.label, RDFS.comment):
                    if list(accumulating.objects(s_ref, ann_pred)):
                        continue  # keep-first: first declaration is authoritative
                for triple in fragment_graph.triples((s_ref, ann_pred, None)):
                    _add_triple(accumulating, triple)

    # Merge all non-rdf:type triples for NEW subjects
    for s_str in new_class_uris:
        pass  # already added above in the new-entity branch

    # Also merge triples whose subjects are BNodes (OWL restrictions, oneOf lists, etc.)
    for s in fragment_graph.subjects():
        if isinstance(s, rdflib.BNode):
            for triple in fragment_graph.triples((s, None, None)):
                _add_triple(accumulating, triple)

    # Also merge non-rdf:type structural triples for new entities
    # (domain, range, subClassOf, subPropertyOf, inverseOf, etc.)
    for s_str in new_class_uris:
        s_ref = URIRef(s_str)
        for triple in fragment_graph.triples((s_ref, None, None)):
            _add_triple(accumulating, triple)

    # Warn about new properties missing domain or range
    new_prop_uris = [
        s_str for s_str, frag_type_set in fragment_types.items()
        if s_str not in existing_types
        and (str(OWL.DatatypeProperty) in frag_type_set or str(OWL.ObjectProperty) in frag_type_set)
    ]
    result.new_props = new_prop_uris
    new_obj_prop_uris = [
        s_str for s_str, frag_type_set in fragment_types.items()
        if s_str not in existing_types
        and str(OWL.ObjectProperty) in frag_type_set
    ]
    for prop_str in new_prop_uris:
        prop_ref = URIRef(prop_str)
        if not list(fragment_graph.objects(prop_ref, RDFS.domain)):
            msg = f"Property <{prop_str}> has no rdfs:domain"
            result.domain_range_warnings.append(msg)
            logger.warning("graph_merger: '%s' — %s", table_name, msg)
        if not list(fragment_graph.objects(prop_ref, RDFS.range)):
            msg = f"Property <{prop_str}> has no rdfs:range"
            result.domain_range_warnings.append(msg)
            logger.warning("graph_merger: '%s' — %s", table_name, msg)

    # Build AccumulatedClassSummary for each new class
    for cls_uri in new_class_uris:
        cls_ref = URIRef(cls_uri)
        labels = list(accumulating.objects(cls_ref, RDFS.label))
        label = str(labels[0]) if labels else cls_uri.split("#")[-1].split("/")[-1]
        local_name = cls_uri.split("#")[-1].split("/")[-1]
        result.new_classes.append(AccumulatedClassSummary(
            local_name=local_name,
            label=label,
            uri=cls_uri,
            source_tables=[table_name],
        ))

    # Issue 1: warn if a high-row-count table's primary class was enumerated
    if table_row_count > max_rows_for_enumeration:
        primary_uri = URIRef(base_iri + derive_class_local_name(table_name))
        if list(accumulating.objects(primary_uri, OWL.equivalentClass)):
            msg = (
                f"CRITICAL: Class <{primary_uri}> from table '{table_name}' "
                f"({table_row_count:,} rows > threshold {max_rows_for_enumeration}) "
                f"was generated as an enumerated type (owl:equivalentClass / owl:oneOf). "
                f"This is wrong — it is an ENTITY class, not a controlled vocabulary."
            )
            result.enumeration_warnings.append(msg)
            logger.critical("graph_merger: '%s' — %s", table_name, msg)

    if result.skipped_duplicates:
        logger.debug(
            "graph_merger: '%s' — skipped %d duplicate URI(s)",
            table_name, len(result.skipped_duplicates),
        )
    if result.new_classes:
        logger.debug(
            "graph_merger: '%s' — added %d new class(es): %s",
            table_name, len(result.new_classes),
            [c.local_name for c in result.new_classes],
        )

    # Safety net: auto-wire ObjectProperty → abstract parent by range match, and fix
    # any XSD range on a property whose abstract parent has a named-class range.
    _fix_xsd_range_on_subproperties(accumulating, new_obj_prop_uris)
    _autowire_subproperty_by_range(accumulating, new_obj_prop_uris, result)

    # Strip rdfs:domain from properties used as shared abstract parents (≥2 subproperties).
    _strip_domain_from_abstract_parents(accumulating)

    # Post-merge scan: strip any self-referential subPropertyOf.
    _scan_and_strip_self_ref_subprops(accumulating)

    return result


def synthesise_missing_provenance(
    accumulating: Graph,
    new_class_uris: List[str],
    new_prop_uris: List[str],
    table_name: str,
) -> List[str]:
    """
    Post-merge safety sweep: for each newly merged class or property still lacking
    the required fm: provenance annotation, synthesise a degraded fallback.

    Classes: adds fm:sourceTable when missing.
    DataProperties: adds fm:sourceColumn, fm:sqlType, fm:nullRate when missing.
    ObjectProperties: adds fm:sourceForeignKey "not recoverable" when missing.

    Returns the list of violation URIs.
    """
    violations: List[str] = []

    for cls_str in new_class_uris:
        cls_ref = URIRef(cls_str)
        if list(accumulating.objects(cls_ref, FM_SOURCE_TABLE)):
            continue
        local = cls_str.split("#")[-1].split("/")[-1] or "unknown"
        accumulating.add((cls_ref, FM_SOURCE_TABLE, _rdflib.Literal(local)))
        violations.append(cls_str)
        logger.warning(
            "graph_merger: synthesised fm:sourceTable for <%s> (table '%s')",
            cls_str, table_name,
        )

    for prop_str in new_prop_uris:
        prop_ref = URIRef(prop_str)
        prop_types = {str(o) for _, _, o in accumulating.triples((prop_ref, RDF.type, None))}

        if str(OWL.ObjectProperty) in prop_types:
            if not list(accumulating.objects(prop_ref, FM_SOURCE_FK)):
                accumulating.add((prop_ref, FM_SOURCE_FK, _rdflib.Literal("not recoverable")))
                violations.append(prop_str)
                logger.warning(
                    "graph_merger: synthesised fm:sourceForeignKey for <%s> (table '%s')",
                    prop_str, table_name,
                )
        else:
            # DatatypeProperty
            local = prop_str.split("#")[-1].split("/")[-1] or "unknown"
            changed = False
            if not list(accumulating.objects(prop_ref, FM_SOURCE_COLUMN)):
                accumulating.add((prop_ref, FM_SOURCE_COLUMN,
                                   _rdflib.Literal(f"{table_name}.{local}")))
                changed = True
            if not list(accumulating.objects(prop_ref, FM_SQL_TYPE)):
                accumulating.add((prop_ref, FM_SQL_TYPE, _rdflib.Literal("unknown")))
                changed = True
            if not list(accumulating.objects(prop_ref, FM_NULL_RATE)):
                accumulating.add((prop_ref, FM_NULL_RATE, _rdflib.Literal("unknown")))
                changed = True
            if changed:
                violations.append(prop_str)
                logger.warning(
                    "graph_merger: synthesised fm: data property annotations for <%s> (table '%s')",
                    prop_str, table_name,
                )

    return violations


def remove_legacy_provenance_comments(graph: Graph) -> int:
    """
    Remove any rdfs:comment triple whose string value contains "Derived from".
    Returns the count of triples removed.
    """
    to_remove = [
        (s, RDFS.comment, o)
        for s, _, o in graph.triples((None, RDFS.comment, None))
        if "Derived from" in str(o)
    ]
    for triple in to_remove:
        graph.remove(triple)
        logger.info(
            "graph_merger: removed legacy provenance rdfs:comment from <%s>", triple[0]
        )
    return len(to_remove)


def check_duplicate_property_types(fragment_graph: Graph) -> List[str]:
    """
    Return local-name strings of URIs declared as both ObjectProperty and
    DatatypeProperty within the same fragment graph.  This is structurally
    invalid OWL and must be caught before the fragment is merged.
    """
    obj_props = {str(s) for s in fragment_graph.subjects(RDF.type, OWL.ObjectProperty)}
    data_props = {str(s) for s in fragment_graph.subjects(RDF.type, OWL.DatatypeProperty)}
    dual = sorted(obj_props & data_props)
    for uri in dual:
        logger.warning(
            "graph_merger: dual-type conflict — <%s> is both ObjectProperty and DatatypeProperty",
            uri,
        )
    return dual


# ---------------------------------------------------------------------------
# Pre-merge safety helpers
# ---------------------------------------------------------------------------

def _strip_self_referential_subprops(fragment_graph: Graph) -> int:
    """
    Remove any rdfs:subPropertyOf triple where subject == object.
    Returns the number of triples removed.
    """
    to_remove = [
        (s, RDFS.subPropertyOf, o)
        for s, _, o in fragment_graph.triples((None, RDFS.subPropertyOf, None))
        if s == o
    ]
    for triple in to_remove:
        fragment_graph.remove(triple)
        logger.warning(
            "Skipping self-referential subPropertyOf: %s subPropertyOf itself",
            str(triple[0]).split("#")[-1].split("/")[-1],
        )
    return len(to_remove)


def _add_triple(accumulating: Graph, triple: tuple) -> None:
    """
    Add *triple* to *accumulating*, skipping any self-referential
    rdfs:subPropertyOf (subject == object) with a WARNING.

    Belt-and-suspenders guard that fires if a self-referential triple somehow
    reaches the add site despite the pre-merge fragment strip.
    """
    s, p, o = triple
    if p == RDFS.subPropertyOf and s == o:
        prop_name = str(s).split("#")[-1].split("/")[-1]
        logger.warning(
            "Skipping self-referential subPropertyOf: %s subPropertyOf itself",
            prop_name,
        )
        return
    accumulating.add(triple)


def _remove_rdf_list(graph: Graph, head) -> None:
    """Remove an RDF collection (rdf:first/rdf:rest chain) rooted at *head*."""
    node = head
    while node is not None and node != RDF.nil:
        nxt = next(graph.objects(node, RDF.rest), None)
        graph.remove((node, RDF.first, None))
        graph.remove((node, RDF.rest, None))
        node = nxt


def _clear_property_domain(graph: Graph, prop: URIRef) -> None:
    """Remove every rdfs:domain triple on *prop*, including the subtree of any
    owl:unionOf restriction blank node (so re-running leaves no orphan triples)."""
    for dom in list(graph.objects(prop, RDFS.domain)):
        if isinstance(dom, BNode):
            for lst in list(graph.objects(dom, OWL.unionOf)):
                _remove_rdf_list(graph, lst)
                graph.remove((dom, OWL.unionOf, lst))
            graph.remove((dom, None, None))
        graph.remove((prop, RDFS.domain, dom))


def _set_union_domain(graph: Graph, prop: URIRef, class_uris: list) -> None:
    """Set *prop*'s rdfs:domain to *class_uris*: a single class URI when there is
    one, or one ``rdfs:domain [ owl:unionOf ( ... ) ]`` restriction when several.
    A union is used (never multiple rdfs:domain triples) because multiple domains
    are conjunction in OWL and would make a property over disjoint classes
    unsatisfiable."""
    if len(class_uris) == 1:
        graph.add((prop, RDFS.domain, URIRef(class_uris[0])))
        return
    restriction = BNode()
    list_head = BNode()
    Collection(graph, list_head, [URIRef(c) for c in class_uris])
    graph.add((restriction, RDF.type, OWL.Class))
    graph.add((restriction, OWL.unionOf, list_head))
    graph.add((prop, RDFS.domain, restriction))


def _strip_domain_from_abstract_parents(accumulating: Graph) -> int:
    """
    For any property that is the *target* of rdfs:subPropertyOf from ≥2 distinct
    subjects (i.e. a shared abstract parent), its own rdfs:domain triples are
    AND-ed by OWL (conjunction). Across disjoint table classes that intersection
    is empty, making the property unsatisfiable — which is why the domain cannot
    simply be left as-is.

    Instead of dropping the domain entirely (which loses the source anchor and
    leaves the property unrestricted), set it to the *union* of the child
    subproperties' domains, expressed as a single
    ``rdfs:domain [ owl:unionOf ( ClassA ClassB ... ) ]`` restriction (or a plain
    single class when the children share one domain). When no child domain can be
    determined, fall back to removing the domain (the previous behaviour) so the
    conjunction trap is still avoided.

    Idempotent: re-running with the same children is a no-op; adding a child
    rebuilds the union and cleans up the prior restriction blank node.

    If exactly 1 subproperty exists, the domain is retained unchanged (it may be
    a legitimate single-domain parent, not yet promoted to abstract).

    Returns the number of parents whose domain was replaced or stripped.
    """
    from collections import defaultdict

    children: defaultdict = defaultdict(set)
    for s, _, o in accumulating.triples((None, RDFS.subPropertyOf, None)):
        if s != o:
            children[o].add(s)

    changed = 0
    for parent, kids in children.items():
        own_domains = list(accumulating.objects(parent, RDFS.domain))
        prop_local = str(parent).split("#")[-1].split("/")[-1]

        if len(kids) < 2:
            if own_domains:
                logger.warning(
                    "graph_merger: <%s> has rdfs:domain AND 1 subproperty via "
                    "rdfs:subPropertyOf — domain retained (may be single-domain parent)",
                    prop_local,
                )
            continue

        # Union of the child subproperties' named-class domains.
        union_classes = sorted({
            str(d)
            for kid in kids
            for d in accumulating.objects(kid, RDFS.domain)
            if isinstance(d, URIRef)
        })
        current = domain_class_uris(accumulating, parent)
        existing_domain_objs = list(accumulating.objects(parent, RDFS.domain))

        if union_classes:
            # Idempotent ONLY when already in canonical single-object form (one class
            # URI, or one owl:unionOf restriction). Multiple plain rdfs:domain triples
            # that merely expand to the same class set are conjunction we must still
            # collapse into a union.
            if current == set(union_classes) and len(existing_domain_objs) == 1:
                continue
            _clear_property_domain(accumulating, parent)
            _set_union_domain(accumulating, parent, union_classes)
            changed += 1
            logger.warning(
                "graph_merger: set rdfs:domain of abstract <%s> to union of %d child "
                "domain(s): %s",
                prop_local, len(union_classes),
                [c.split("#")[-1] for c in union_classes],
            )
        elif own_domains:
            # No determinable child domains — fall back to removing the parent's own
            # domain so the OWL conjunction trap is still avoided.
            _clear_property_domain(accumulating, parent)
            changed += 1
            logger.warning(
                "graph_merger: stripped rdfs:domain from <%s> — abstract parent of "
                "%d subproperties with no determinable child domains",
                prop_local, len(kids),
            )
        # else: no own domain and no child domains — nothing to change.
    return changed


def _scan_and_strip_self_ref_subprops(accumulating: Graph) -> int:
    """
    Post-merge scan: remove any rdfs:subPropertyOf triple where subject == object
    from *accumulating* and log a WARNING for each one found.

    Catches triples that entered via a previous run, a loaded OWL file, or any
    code path that bypassed the pre-merge fragment strip.
    """
    to_strip = [
        (s, RDFS.subPropertyOf, o)
        for s, _, o in accumulating.triples((None, RDFS.subPropertyOf, None))
        if s == o
    ]
    for triple in to_strip:
        accumulating.remove(triple)
        prop_name = str(triple[0]).split("#")[-1].split("/")[-1]
        logger.warning(
            "Skipping self-referential subPropertyOf: %s subPropertyOf itself",
            prop_name,
        )
    return len(to_strip)


_XSD_BASE = "http://www.w3.org/2001/XMLSchema#"


def _autowire_subproperty_by_range(
    accumulating: Graph,
    new_obj_prop_uris: List[str],
    result: "MergeResult",
) -> None:
    """
    For each newly added ObjectProperty that has rdfs:domain but no rdfs:subPropertyOf,
    find abstract ObjectProperties in *accumulating* (those with no rdfs:domain) whose
    rdfs:range matches.

    - Exactly one match → auto-wire rdfs:subPropertyOf and log INFO.
    - Multiple matches → log WARNING, do not auto-wire (ambiguous).
    """
    # Build: range_uri → [abstract_prop_uri, ...]
    abstract_by_range: dict = {}
    for prop in accumulating.subjects(RDF.type, OWL.ObjectProperty):
        if list(accumulating.objects(prop, RDFS.domain)):
            continue  # has domain → not abstract
        for rng in accumulating.objects(prop, RDFS.range):
            rng_str = str(rng)
            abstract_by_range.setdefault(rng_str, []).append(prop)

    for prop_str in new_obj_prop_uris:
        prop = URIRef(prop_str)
        # Skip if already has subPropertyOf or has no domain (already abstract)
        if list(accumulating.objects(prop, RDFS.subPropertyOf)):
            continue
        if not list(accumulating.objects(prop, RDFS.domain)):
            continue
        ranges = list(accumulating.objects(prop, RDFS.range))
        if not ranges:
            continue
        rng_str = str(ranges[0])
        candidates = abstract_by_range.get(rng_str, [])
        if len(candidates) == 1:
            parent = candidates[0]
            accumulating.add((prop, RDFS.subPropertyOf, parent))
            prop_local = prop_str.split("#")[-1].split("/")[-1]
            parent_local = str(parent).split("#")[-1].split("/")[-1]
            msg = f"{prop_local} → {parent_local}"
            result.auto_wired.append(msg)
            logger.info(
                "Auto-wired <%s> as subPropertyOf <%s> by range match",
                prop, parent,
            )
        elif len(candidates) > 1:
            prop_local = prop_str.split("#")[-1].split("/")[-1]
            logger.warning(
                "graph_merger: <%s> has range <%s> matching %d abstract ObjectProperties — "
                "ambiguous, not auto-wired: %s",
                prop, rng_str, len(candidates),
                [str(c).split("#")[-1] for c in candidates],
            )


def _fix_xsd_range_on_subproperties(
    accumulating: Graph,
    new_obj_prop_uris: List[str],
) -> None:
    """
    If a newly merged ObjectProperty has rdfs:subPropertyOf pointing to an abstract
    parent, but its own rdfs:range is an XSD datatype (not a named class), correct the
    range to match the abstract parent's range and log a WARNING.

    This catches the case where the gen-LLM wired SubPropertyOf correctly but still
    declared Range: xsd:integer instead of the actual class URI.
    """
    for prop_str in new_obj_prop_uris:
        prop = URIRef(prop_str)
        parents = list(accumulating.objects(prop, RDFS.subPropertyOf))
        if not parents:
            continue
        current_ranges = list(accumulating.objects(prop, RDFS.range))
        xsd_ranges = [r for r in current_ranges if str(r).startswith(_XSD_BASE)]
        if not xsd_ranges:
            continue
        for parent in parents:
            parent_ranges = list(accumulating.objects(parent, RDFS.range))
            if not parent_ranges:
                continue
            parent_range = parent_ranges[0]
            if str(parent_range).startswith(_XSD_BASE):
                continue  # parent also has XSD range — correct type, leave it
            prop_local = prop_str.split("#")[-1].split("/")[-1]
            parent_local = str(parent).split("#")[-1].split("/")[-1]
            for xsd_r in xsd_ranges:
                accumulating.remove((prop, RDFS.range, xsd_r))
            accumulating.add((prop, RDFS.range, parent_range))
            logger.warning(
                "graph_merger: corrected XSD range on <%s> to <%s> to match abstract parent <%s>",
                prop, parent_range, parent_local,
            )
            break  # corrected — one pass sufficient


def _fix_datatype_fk_properties(
    fragment_graph: Graph,
    base_iri: str,
    fk_info: List[Dict],
    skipped_tables: Set[str],
) -> int:
    """
    Safety net: for each FK column in *fk_info* (excluding skipped tables), find any
    DatatypeProperty in *fragment_graph* whose skos:altLabel matches the FK column name
    and convert it to an ObjectProperty with the correct rdfs:range (the referenced class URI).

    Returns the number of properties converted.

    The gen-LLM is instructed to model FK columns as ObjectProperty but may fall back to
    DatatypeProperty xsd:integer. This function corrects that mistake before the fragment
    is merged into the accumulating graph.

    NOTE: gold/builder.py._resolve_fk_properties() and rdf/triple_builder.TripleBuilder both
    detect FK relationships by column name (entity_map) rather than property type, so they
    are compatible regardless of whether FK properties are ObjectProperty or DatatypeProperty.
    With this fix, FK properties are always ObjectProperty in the ontology, making the semantic
    model correct from the start.
    """
    from rdflib.namespace import SKOS as _SKOS

    # Build: lower(col_name) → target class URI for non-skipped FKs
    fk_col_to_range: Dict[str, URIRef] = {}
    for fk in fk_info:
        col_name = fk.get("constrained_column") or fk.get("column")
        ref_table = fk.get("referred_table") or fk.get("ref_table")
        if not col_name or not ref_table:
            continue
        if ref_table in skipped_tables:
            continue  # SKIPPED FK — intentionally modelled as DatatypeProperty xsd:integer
        ref_class_local = derive_class_local_name(ref_table)
        fk_col_to_range[col_name.lower()] = URIRef(f"{base_iri}{ref_class_local}")

    if not fk_col_to_range:
        return 0

    _XSD = "http://www.w3.org/2001/XMLSchema#"
    converted = 0

    for prop in list(fragment_graph.subjects(RDF.type, OWL.DatatypeProperty)):
        alt_labels = [str(lbl).lower() for lbl in fragment_graph.objects(prop, _SKOS.altLabel)]
        matched_col = next((al for al in alt_labels if al in fk_col_to_range), None)
        if matched_col is None:
            continue

        range_uri = fk_col_to_range[matched_col]
        prop_local = str(prop).split("#")[-1].split("/")[-1]

        # Swap DatatypeProperty → ObjectProperty
        fragment_graph.remove((prop, RDF.type, OWL.DatatypeProperty))
        fragment_graph.add((prop, RDF.type, OWL.ObjectProperty))

        # Replace any XSD range with the named class URI
        for r in list(fragment_graph.objects(prop, RDFS.range)):
            if str(r).startswith(_XSD):
                fragment_graph.remove((prop, RDFS.range, r))
        if not list(fragment_graph.objects(prop, RDFS.range)):
            fragment_graph.add((prop, RDFS.range, range_uri))
        else:
            # Non-XSD range already present — overwrite only if it differs
            existing_ranges = list(fragment_graph.objects(prop, RDFS.range))
            if range_uri not in existing_ranges:
                for r in existing_ranges:
                    fragment_graph.remove((prop, RDFS.range, r))
                fragment_graph.add((prop, RDFS.range, range_uri))

        converted += 1
        logger.error(
            "graph_merger: FK column '%s' generated as DatatypeProperty <%s> — "
            "converted to ObjectProperty with Range: <%s>",
            matched_col, prop_local, range_uri,
        )

    return converted


def _strip_dual_annotated_object_properties(fragment_graph: Graph) -> int:
    """
    Remove ObjectProperty declarations whose fm:sourceForeignKey "table.col → ..."
    duplicates the same source column already annotated as fm:sourceColumn on a
    DatatypeProperty within the same fragment.

    This is the structural signature of a fabricated-class hallucination: the LLM
    emits both a scalar (DatatypeProperty + fm:sourceColumn) and an FK
    (ObjectProperty + fm:sourceForeignKey → invented class) for a column that the
    inspector never flagged as a FK.  The scalar side is authoritative; the
    ObjectProperty side is stripped.  If the ObjectProperty's range class was also
    declared in this fragment and is no longer referenced by any other property,
    that fabricated class is stripped too.

    Returns the number of ObjectProperty subjects removed.
    """
    # Collect (table, col) → prop for DatatypeProperty fm:sourceColumn annotations
    dt_col_keys: dict[tuple, URIRef] = {}
    for prop in fragment_graph.subjects(RDF.type, OWL.DatatypeProperty):
        for val in fragment_graph.objects(prop, FM_SOURCE_COLUMN):
            s = str(val)
            if "." in s:
                tbl, col = s.split(".", 1)
                dt_col_keys[(tbl.strip(), col.strip())] = prop

    if not dt_col_keys:
        return 0

    # Collect (table, col) → prop for ObjectProperty fm:sourceForeignKey annotations
    obj_col_keys: dict[tuple, URIRef] = {}
    for prop in fragment_graph.subjects(RDF.type, OWL.ObjectProperty):
        for val in fragment_graph.objects(prop, FM_SOURCE_FK):
            s = str(val)
            if "." in s and "->" in s:
                left = s.split("->", 1)[0].strip()
                if "." in left:
                    tbl, col = left.split(".", 1)
                    obj_col_keys[(tbl.strip(), col.strip())] = prop

    removed = 0
    for key in sorted(set(dt_col_keys) & set(obj_col_keys)):
        tbl, col = key
        obj_prop = obj_col_keys[key]
        obj_prop_local = str(obj_prop).split("#")[-1].split("/")[-1]

        # Collect the range class before removing the property
        range_classes = list(fragment_graph.objects(obj_prop, RDFS.range))

        # Remove all triples for this ObjectProperty
        for triple in list(fragment_graph.triples((obj_prop, None, None))):
            fragment_graph.remove(triple)
        for triple in list(fragment_graph.triples((None, None, obj_prop))):
            fragment_graph.remove(triple)

        removed += 1
        logger.warning(
            "graph_merger: stripped fabricated ObjectProperty <%s> "
            "(fm:sourceForeignKey '%s.%s → ...') — same column has "
            "fm:sourceColumn on a DatatypeProperty in this fragment",
            obj_prop_local, tbl, col,
        )

        # Strip the fabricated range class if it was declared in this fragment
        # and is no longer pointed at by any other ObjectProperty range.
        for range_cls in range_classes:
            if not isinstance(range_cls, URIRef):
                continue
            if not list(fragment_graph.triples((range_cls, RDF.type, OWL.Class))):
                continue
            still_referenced = any(
                True for _ in fragment_graph.triples((None, RDFS.range, range_cls))
            )
            if not still_referenced:
                for triple in list(fragment_graph.triples((range_cls, None, None))):
                    fragment_graph.remove(triple)
                cls_local = str(range_cls).split("#")[-1].split("/")[-1]
                logger.warning(
                    "graph_merger: stripped fabricated class <%s> — "
                    "no longer referenced after removing dual-annotated ObjectProperty",
                    cls_local,
                )

    return removed


def _split_multi_domain_properties(fragment_graph: Graph, base_iri: str) -> int:
    """
    Scan *fragment_graph* for DatatypeProperty/ObjectProperty nodes with
    more than one distinct rdfs:domain.  For each such property:
      - Strip all Domain: triples (making the original an abstract parent).
      - Create one class-scoped subproperty per domain, each a subPropertyOf
        the original abstract parent.
    Returns the number of properties rewritten.
    """
    from rdflib import Literal
    rewritten = 0
    for ptype in (OWL.DatatypeProperty, OWL.ObjectProperty):
        for prop in list(fragment_graph.subjects(RDF.type, ptype)):
            domains = list(fragment_graph.objects(prop, RDFS.domain))
            if len(domains) <= 1:
                continue

            prop_local = str(prop).split("#")[-1].split("/")[-1]
            ranges = list(fragment_graph.objects(prop, RDFS.range))
            range_uri = ranges[0] if ranges else None
            labels = list(fragment_graph.objects(prop, RDFS.label))

            # Make original abstract (no domain)
            for dom in domains:
                fragment_graph.remove((prop, RDFS.domain, dom))

            # Create a class-scoped subproperty for each domain
            for dom in domains:
                class_local = str(dom).split("#")[-1].split("/")[-1]
                if prop_local.startswith("has") and len(prop_local) > 3:
                    concept = prop_local[3:]
                    scoped_local = f"has{class_local}{concept}"
                else:
                    scoped_local = f"has{class_local}{prop_local.capitalize()}"
                scoped_uri = URIRef(f"{base_iri}{scoped_local}")
                fragment_graph.add((scoped_uri, RDF.type, ptype))
                fragment_graph.add((scoped_uri, RDFS.domain, dom))
                if range_uri:
                    fragment_graph.add((scoped_uri, RDFS.range, range_uri))
                fragment_graph.add((scoped_uri, RDFS.subPropertyOf, prop))
                for lbl in labels:
                    fragment_graph.add((scoped_uri, RDFS.label, lbl))

            rewritten += 1
            logger.warning(
                "graph_merger: split <%s> with %d domains into class-scoped subproperties",
                prop, len(domains),
            )

    return rewritten


# ---------------------------------------------------------------------------
# MIXED_RANGE resolver
# ---------------------------------------------------------------------------
#
# A property carrying BOTH an ontology-namespace class range AND an XSD literal
# range is unsatisfiable OWL (range_coherence_check flags it as MIXED_RANGE).
# This resolver is the deterministic repair for the subset that has a trustworthy
# anchor, and an explicit router for the subset that does not.
#
# CARDINAL RULE: key on the inspector's real per-table fk_info and column types,
# NEVER on the LLM's fm:sourceForeignKey (the generator fabricates those, and the
# inspector's own value-overlap inference can mislabel a scalar like
# raw_transfers.los as an FK — only the property's TRUE source table disambiguates).
#
# Tiers:
#   R1  source column IS an FK of its source table   -> ObjectProperty + class range, drop XSD
#   R2  not an FK AND (temporal / non-integer-numeric type, OR range-class is not a
#       well-formed controlled vocabulary)           -> DatatypeProperty + XSD range, drop class
#   R3  range-class is a well-formed owl:oneOf vocab  -> ROUTE to the coherence queue
#                                                        (left unchanged; a human picks
#                                                         vocab-class vs xsd:string)
#   unkeyable  source (table, col) could not be determined -> route, do not guess

from dataclasses import dataclass as _dataclass

_XSD = "http://www.w3.org/2001/XMLSchema#"
_TEMPORAL_SQL = {"DATE", "DATETIME", "TIME", "TIMESTAMP"}
_NON_INT_NUMERIC_SQL = {"DOUBLE", "FLOAT", "DECIMAL", "NUMERIC", "REAL"}
_SQL_TO_XSD = {
    "DATETIME": "dateTime", "TIMESTAMP": "dateTime", "DATE": "date", "TIME": "time",
    "DOUBLE": "double", "FLOAT": "double", "REAL": "double",
    "DECIMAL": "decimal", "NUMERIC": "decimal",
    "INTEGER": "integer", "INT": "integer", "BIGINT": "integer",
    "SMALLINT": "integer", "TINYINT": "integer",
    "VARCHAR": "string", "CHAR": "string", "TEXT": "string", "BOOLEAN": "boolean",
}


@_dataclass
class MixedRangeResolution:
    resolved_fk: list = field(default_factory=list)            # R1: (prop, "table.col", class)
    resolved_scalar: list = field(default_factory=list)        # R2: (prop, "table.col", sql_type)
    resolved_vocab_class: list = field(default_factory=list)   # R3->CLASS: keep vocab class
    resolved_vocab_literal: list = field(default_factory=list) # R3->LITERAL: demote to xsd
    routed_vocab: list = field(default_factory=list)           # R3->queue: no decider/borderline
    unkeyable: list = field(default_factory=list)              # (prop,) source not determinable

    def summary(self) -> str:
        return (f"R1(FK)={len(self.resolved_fk)} "
                f"R2(scalar)={len(self.resolved_scalar)} "
                f"R3-class={len(self.resolved_vocab_class)} "
                f"R3-literal={len(self.resolved_vocab_literal)} "
                f"R3-routed={len(self.routed_vocab)} "
                f"unkeyable={len(self.unkeyable)}")


def _is_wellformed_vocab(graph: Graph, cls: URIRef) -> bool:
    """A class is a well-formed controlled vocabulary if it is enumerated
    (owl:oneOf / owl:equivalentClass) or carries NamedIndividuals and is not used
    as the domain of any property (i.e. it is a value type, not an entity)."""
    if list(graph.objects(cls, OWL.oneOf)) or list(graph.objects(cls, OWL.equivalentClass)):
        return True
    individuals = list(graph.subjects(RDF.type, cls))
    used_as_domain = list(graph.subjects(RDFS.domain, cls))
    return bool(individuals) and not used_as_domain


def _drop_crosskind_subproperty(graph: Graph, prop: URIRef, want_object: bool) -> None:
    """Remove rdfs:subPropertyOf links to a parent of the opposite property kind
    (e.g. an ObjectProperty pointing at a DatatypeProperty parent after R1/R2)."""
    for parent in list(graph.objects(prop, RDFS.subPropertyOf)):
        ptypes = set(graph.objects(parent, RDF.type))
        parent_obj = OWL.ObjectProperty in ptypes
        parent_dt = OWL.DatatypeProperty in ptypes
        if want_object and parent_dt and not parent_obj:
            graph.remove((prop, RDFS.subPropertyOf, parent))
        elif not want_object and parent_obj and not parent_dt:
            graph.remove((prop, RDFS.subPropertyOf, parent))


def _force_object_property(graph: Graph, prop: URIRef, class_uri: str) -> None:
    for r in list(graph.objects(prop, RDFS.range)):
        graph.remove((prop, RDFS.range, r))
    graph.remove((prop, RDF.type, OWL.DatatypeProperty))
    graph.add((prop, RDF.type, OWL.ObjectProperty))
    graph.add((prop, RDFS.range, URIRef(class_uri)))
    _drop_crosskind_subproperty(graph, prop, want_object=True)


def _force_datatype_property(graph: Graph, prop: URIRef, xsd_uri: str) -> None:
    for r in list(graph.objects(prop, RDFS.range)):
        graph.remove((prop, RDFS.range, r))
    graph.remove((prop, RDF.type, OWL.ObjectProperty))
    graph.add((prop, RDF.type, OWL.DatatypeProperty))
    graph.add((prop, RDFS.range, URIRef(xsd_uri)))
    _drop_crosskind_subproperty(graph, prop, want_object=False)


def resolve_mixed_ranges(
    graph: Graph,
    namespace: str,
    get_source,
    fk_lookup: Dict[tuple, str],
    type_lookup: Dict[tuple, str],
    vocab_decider=None,
) -> MixedRangeResolution:
    """
    Resolve every MIXED_RANGE property (one carrying both a class range and an XSD
    range) deterministically where an anchor exists, else route it.

    Parameters
    ----------
    namespace   : ontology class/property namespace (class ranges start with this).
    get_source  : callable(prop_uri:str) -> (table:str, col:str|None) | None.
                  At merge time this is exact (the current table + the column the
                  property was built from). Post-hoc it is a best-effort derivation.
    fk_lookup   : {(table, col): ref_class_local} for REAL (inspector) FK columns,
                  keyed per source table — NOT the LLM's fm:sourceForeignKey.
    type_lookup : {(table, col): SQL_TYPE} for the column's inspector type.

    The trigger is exactly range_coherence_check's MIXED_RANGE definition, so this
    is a provable no-op on any graph with zero MIXED_RANGE (e.g. the gold ontology).
    """
    report = MixedRangeResolution()
    all_props = (set(graph.subjects(RDF.type, OWL.ObjectProperty)) |
                 set(graph.subjects(RDF.type, OWL.DatatypeProperty)))

    for prop in sorted(all_props, key=lambda p: str(p)):
        ranges = [str(r) for r in graph.objects(prop, RDFS.range)]
        class_ranges = [r for r in ranges if r.startswith(namespace)]
        xsd_ranges = [r for r in ranges if r.startswith(_XSD)]
        if not (class_ranges and xsd_ranges):
            continue  # not MIXED_RANGE — leave untouched (this is the gold no-op guard)

        plocal = str(prop).split("#")[-1].split("/")[-1]
        src = get_source(str(prop))
        table, col = (src if src else (None, None))

        # R1 — real FK on the property's true source table.
        if col is not None and (table, col) in fk_lookup:
            ref_class = fk_lookup[(table, col)]
            _force_object_property(graph, prop, namespace + ref_class)
            report.resolved_fk.append((plocal, f"{table}.{col}", ref_class))
            logger.warning("resolve_mixed_ranges: R1 <%s> (%s.%s) -> ObjectProperty range <%s>",
                           plocal, table, col, ref_class)
            continue

        cls_uri = class_ranges[0]
        cls_local = cls_uri.split("#")[-1].split("/")[-1]
        sql = (type_lookup.get((table, col)) or "").upper().split("(")[0].strip()
        is_vocab = _is_wellformed_vocab(graph, URIRef(cls_uri))

        # R2 — scalar: temporal / non-integer-numeric type, or range-class is not a
        # well-formed vocabulary (a hallucinated relationship onto a plain attribute).
        if col is not None and (sql in _TEMPORAL_SQL or sql in _NON_INT_NUMERIC_SQL or not is_vocab):
            xsd_local = _SQL_TO_XSD.get(sql) or xsd_ranges[0].split("#")[-1]
            _force_datatype_property(graph, prop, _XSD + xsd_local)
            report.resolved_scalar.append((plocal, f"{table}.{col}", sql or "?"))
            logger.warning("resolve_mixed_ranges: R2 <%s> (%s.%s) -> DatatypeProperty range xsd:%s",
                           plocal, table, col, xsd_local)
            continue

        # R3 — well-formed controlled vocabulary. With a vocab_decider, decide
        # class-vs-literal deterministically from inspector stats (null-rate /
        # support / uniqueness); without one it is genuinely ambiguous → route.
        if is_vocab:
            src = f"{table}.{col}" if col else f"{table}.?"
            decision = vocab_decider(table, col) if vocab_decider else None
            if decision == "CLASS":
                _force_object_property(graph, prop, cls_uri)
                report.resolved_vocab_class.append((plocal, src, cls_local))
                logger.warning("resolve_mixed_ranges: R3->CLASS <%s> (%s) — keep vocab <%s>",
                               plocal, src, cls_local)
            elif decision == "LITERAL":
                xsd_local = _SQL_TO_XSD.get(sql) or "string"
                _force_datatype_property(graph, prop, _XSD + xsd_local)
                report.resolved_vocab_literal.append((plocal, src, cls_local))
                logger.warning("resolve_mixed_ranges: R3->LITERAL <%s> (%s) — demote vocab <%s> to xsd:%s",
                               plocal, src, cls_local, xsd_local)
            else:
                report.routed_vocab.append((plocal, src, cls_local))
                logger.info("resolve_mixed_ranges: R3 <%s> -> coherence queue (vocab class <%s>)",
                            plocal, cls_local)
            continue

        report.unkeyable.append((plocal,))
        logger.warning("resolve_mixed_ranges: unkeyable <%s> — source (table,col) not determinable, routed",
                       plocal)

    return report


def resolve_mixed_ranges_in_graph(
    graph: Graph,
    namespace: str,
    profiles,
    max_distinct_for_enumeration: int = 20,
    max_null_rate_for_class: float = DEFAULT_MAX_NULL_RATE_FOR_CLASS,
    min_support_for_class: int = DEFAULT_MIN_SUPPORT_FOR_CLASS,
    max_uniqueness_for_class: float = DEFAULT_MAX_UNIQUENESS_FOR_CLASS,
) -> MixedRangeResolution:
    """Post-loop convenience wrapper around :func:`resolve_mixed_ranges`.

    Builds the per-table fk/type/stats lookups and a ``(table, col)`` source
    resolver from the inspector *profiles*, then resolves every MIXED_RANGE
    property in *graph*. R3 (well-formed vocab) is decided deterministically by
    is_closed_vocabulary() on the column's null-rate / support / uniqueness:
    CLASS keeps the vocabulary, LITERAL demotes it to an xsd literal.

    Must run AFTER union domains are set (so shared abstracts such as
    ``hasPatient`` are keyable from their rdfs:domain) and after owl:oneOf
    vocabularies are built (so R3 vocab detection is accurate). Keys strictly on
    the inspector's fk_info / column types and on fm:sourceColumn + rdfs:domain —
    never on the LLM's (fabricated) fm:sourceForeignKey.
    """
    fk_lookup: Dict[tuple, str] = {}
    type_lookup: Dict[tuple, str] = {}
    stats_lookup: Dict[tuple, tuple] = {}  # (table, col) -> (null_rate, distinct, non_null)
    class_to_table: Dict[str, str] = {}
    for p in profiles:
        class_to_table[derive_class_local_name(p.table_name)] = p.table_name
        row_count = getattr(p, "row_count", 0) or 0
        for fk in (getattr(p, "foreign_keys", None) or []):
            col = fk.get("column") or fk.get("constrained_column")
            ref = fk.get("ref_table") or fk.get("referred_table")
            if col and ref:
                fk_lookup[(p.table_name, col)] = derive_class_local_name(ref)
        for col_name, col_profile in (getattr(p, "columns", None) or {}).items():
            type_lookup[(p.table_name, col_name)] = getattr(col_profile, "inferred_type", "") or ""
            null_rate = getattr(col_profile, "null_rate", 0.0) or 0.0
            distinct = getattr(col_profile, "distinct_count", 0) or 0
            stats_lookup[(p.table_name, col_name)] = (
                null_rate, distinct, row_count * (1.0 - null_rate)
            )

    def vocab_decider(table, col):
        st = stats_lookup.get((table, col))
        if st is None:
            return None  # no inspector stats → genuinely ambiguous, route to queue
        null_rate, distinct, non_null = st
        is_class = is_closed_vocabulary(
            distinct, null_rate, non_null,
            max_distinct=max_distinct_for_enumeration,
            max_null_rate=max_null_rate_for_class,
            min_support=min_support_for_class,
            max_uniqueness=max_uniqueness_for_class,
        )
        return "CLASS" if is_class else "LITERAL"

    def get_source(prop_uri: str):
        prop = URIRef(prop_uri)
        # 1. fm:sourceColumn "table.col" — authoritative when present. Ordered first
        #    so it pre-empts the one case where the FK annotation's source table is
        #    wrong (los/outtime cite raw_transfers but really come from raw_icustays).
        for v in graph.objects(prop, FM_SOURCE_COLUMN):
            s = str(v)
            if "." in s:
                t, c = s.split(".", 1)
                return (t.strip(), c.strip())
        dom_tables = {class_to_table.get(u.split("#")[-1]) for u in domain_class_uris(graph, prop)}
        dom_tables.discard(None)
        # 2. fm:sourceForeignKey "table.col -> X": trust ONLY the left/source side,
        #    and only when it names a real column consistent with the property's
        #    domain. The right/target side is the part the LLM fabricates — never
        #    used. This recovers the column for categorical/vocab properties that
        #    carry no fm:sourceColumn (e.g. ethnicity, dbsource, category).
        for v in graph.objects(prop, FM_SOURCE_FK):
            s = str(v)
            if "." in s and "->" in s:
                left = s.split("->", 1)[0].strip()
                if "." in left:
                    t, c = (x.strip() for x in left.split(".", 1))
                    if (t, c) in type_lookup and (not dom_tables or t in dom_tables):
                        return (t, c)
        # 3. rdfs:domain (union-expanded) + range/FK match to recover the column.
        tables = sorted(dom_tables)
        if not tables:
            return None
        class_ranges = {
            str(r).split("#")[-1]
            for r in graph.objects(prop, RDFS.range)
            if str(r).startswith(namespace)
        }
        for table in tables:
            for (t, col), ref_class in fk_lookup.items():
                if t == table and ref_class in class_ranges:
                    return (table, col)
        return (tables[0], None)  # table known, column not — enough to route a vocab

    return resolve_mixed_ranges(
        graph, namespace, get_source, fk_lookup, type_lookup, vocab_decider=vocab_decider
    )
