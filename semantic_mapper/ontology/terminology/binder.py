# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""bind_terminology — attach external concept bindings to the generated ontology.

Runs on the merged graph AFTER ``materialise_categorical_individuals`` (so the
value-mapped individuals exist) and BEFORE ``owl:oneOf`` enumeration.  For each
coded column detected by value overlap:

  * value-mapped individuals whose raw value is a valid code get
    ``skos:exactMatch`` to the external concept IRI, carrying
    ``fm:terminologySystem`` / ``fm:terminologyBindingConfidence`` /
    ``fm:terminologyBindingMethod`` (no binding without its provenance);
  * where the column binds confidently, the owning property/class gets a
    system-level ``skos:closeMatch`` / ``rdfs:seeAlso`` (best-effort);
  * bindings below ``TERM_BIND_CONFIDENCE_FLOOR`` and ambiguous one-to-many
    candidates go to the review sidecar, never the asserted graph;
  * confirmed bindings in the store are replayed at $0 (warm-run reuse).

Fully dataset-agnostic: detection keys on value overlap, terminology data is
pluggable reference data, and there are no table/column-name branches.
"""
from __future__ import annotations

import logging
import uuid
from typing import Dict, List, Optional, Sequence, Set, Tuple

from rdflib import Graph, Literal, OWL, RDF, RDFS, URIRef, XSD
from rdflib.namespace import SKOS

from semantic_mapper.ontology.iterative.categorical_individuals import (
    _parse_source_fk,
    _pascal,
)
from semantic_mapper.ontology.iterative.context_builder import derive_class_uri

from .detection import detect_bindings
from .models import (
    BindingResult,
    CodedProperty,
    DetectedColumn,
    ProposedBinding,
    SystemBinding,
    UnmappedValue,
)
from .sources.base import TerminologySource
from .sources.registry import build_sources

logger = logging.getLogger(__name__)

_FM = "http://forgemesh.ai/ontology#"
_FM_TERM_SYSTEM = URIRef(_FM + "terminologySystem")
_FM_TERM_CONFIDENCE = URIRef(_FM + "terminologyBindingConfidence")
_FM_TERM_METHOD = URIRef(_FM + "terminologyBindingMethod")
_FM_SOURCE_COLUMN = URIRef(_FM + "sourceColumn")

_METHOD_OVERLAP = "value-set-overlap"
_METHOD_HUMAN = "human-confirmed"
_STATUS_PENDING = "PENDING_REVIEW"
_STATUS_CONFIRMED = "CONFIRMED"


def _system_class_uri(base_iri: str, source_id: str) -> URIRef:
    """Conformed per-system code-concept class URI (e.g. ``…#SnomedctConcept``).

    One class per terminology system, shared across every column bound to it, so a
    given code resolves to one individual regardless of which column referenced it."""
    return URIRef(base_iri + _pascal(source_id) + "Concept")


# ---------------------------------------------------------------------------
# Graph index helpers
# ---------------------------------------------------------------------------

def _parse_source_column(graph: Graph, prop: URIRef) -> Optional[Tuple[str, str]]:
    """Recover ``(table, column)`` from a property's ``fm:sourceColumn`` annotation
    (``"raw_observations.code"``).  Used for datatype properties (no FK)."""
    for ann in graph.objects(prop, _FM_SOURCE_COLUMN):
        s = str(ann)
        if "." in s:
            table, col = s.split(".", 1)
            table, col = table.strip(), col.strip()
            if table and col:
                return (table, col)
    return None


def _index_columns(graph: Graph, entity_uris: Set[URIRef]):
    """Build column → (vocab class, properties) indexes from property provenance.

    Returns ``(vocab_class_by_col, props_by_col)`` keyed by ``(table_lower,
    col_lower)``.  ``vocab_class_by_col`` holds the object-property range that is a
    value-mapped vocabulary class (NOT a table-entity class).  ``props_by_col``
    holds every property (object or datatype) derived from that column, for
    system-level alignment.
    """
    vocab_class_by_col: Dict[Tuple[str, str], URIRef] = {}
    props_by_col: Dict[Tuple[str, str], List[URIRef]] = {}

    def _record_prop(table: str, col: str, prop: URIRef) -> None:
        props_by_col.setdefault((table.lower(), col.lower()), []).append(prop)

    for prop in graph.subjects(RDF.type, OWL.ObjectProperty):
        tc = _parse_source_fk(graph, prop)
        if tc is None:
            continue
        table, col = tc
        _record_prop(table, col, prop)
        for rng in graph.objects(prop, RDFS.range):
            if (
                isinstance(rng, URIRef)
                and (rng, RDF.type, OWL.Class) in graph
                and rng not in entity_uris
            ):
                vocab_class_by_col.setdefault((table.lower(), col.lower()), rng)

    for prop in graph.subjects(RDF.type, OWL.DatatypeProperty):
        tc = _parse_source_column(graph, prop)
        if tc is None:
            continue
        _record_prop(tc[0], tc[1], prop)

    return vocab_class_by_col, props_by_col


def _label_to_individual(graph: Graph, vocab_class: URIRef) -> Dict[str, URIRef]:
    """Map each individual's ``rdfs:label`` (raw source value) → its URI, for one
    vocabulary class.  Both verbatim and case-folded keys are recorded so a value
    matches its individual despite incidental case differences."""
    out: Dict[str, URIRef] = {}
    for ind in graph.subjects(RDF.type, vocab_class):
        for lbl in graph.objects(ind, RDFS.label):
            s = str(lbl)
            out.setdefault(s, ind)
            out.setdefault(s.casefold(), ind)
    return out


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------

def _emit_individual_binding(graph: Graph, b: ProposedBinding) -> None:
    """Assert one individual-level binding with its full provenance annotations.
    Exactly one system / confidence / method annotation per individual."""
    ind = URIRef(b.individual_uri)
    graph.add((ind, SKOS.exactMatch, URIRef(b.concept_iri)))
    graph.add((ind, _FM_TERM_SYSTEM, Literal(b.system_label)))
    graph.add((ind, _FM_TERM_CONFIDENCE, Literal(round(b.confidence, 4), datatype=XSD.decimal)))
    graph.add((ind, _FM_TERM_METHOD, Literal(b.method)))


def _emit_system_binding(graph: Graph, sb: SystemBinding) -> None:
    subj = URIRef(sb.subject_uri)
    pred = SKOS.closeMatch if sb.predicate == "skos:closeMatch" else RDFS.seeAlso
    graph.add((subj, pred, URIRef(sb.system_iri)))


# ---------------------------------------------------------------------------
# Main pass
# ---------------------------------------------------------------------------

def bind_terminology(
    graph: Graph,
    profiles: Sequence,
    base_iri: str,
    settings,
    *,
    store=None,
    ontology_source: str = "",
    db_source: str = "",
    run_id: Optional[str] = None,
) -> BindingResult:
    """Bind value-mapped individuals to external concepts. Returns a
    :class:`BindingResult` (the sidecar + coverage audit). No-op when the feature
    is disabled — the graph is left exactly as the categorical pass produced it.
    """
    if not getattr(settings, "TERMINOLOGY_BINDING_ENABLED", False):
        return BindingResult()

    floor = float(getattr(settings, "TERM_BIND_CONFIDENCE_FLOOR", 0.5))
    sources = build_sources(getattr(settings, "TERMINOLOGY_SOURCES", {}) or {})
    source_by_id: Dict[str, TerminologySource] = {s.source_id: s for s in sources}

    overlap_threshold = float(getattr(settings, "TERM_BIND_OVERLAP_THRESHOLD", 0.8))
    detected: List[DetectedColumn] = detect_bindings(profiles, sources, overlap_threshold)

    entity_uris: Set[URIRef] = {
        URIRef(derive_class_uri(p.table_name, base_iri)) for p in profiles
    }
    vocab_class_by_col, props_by_col = _index_columns(graph, entity_uris)

    # Confirmed bindings to replay (warm reuse), keyed by individual URI.
    confirmed_by_ind: Dict[str, dict] = {}
    if store is not None:
        for row in store.load_confirmed(ontology_source, db_source):
            confirmed_by_ind[row["individual_uri"]] = row

    bound: Dict[str, ProposedBinding] = {}       # ind_uri → winning binding (emitted)
    sidecar_inds: Dict[str, ProposedBinding] = {}  # ind_uri → sub-floor/ambiguous
    unmapped: List[UnmappedValue] = []
    system_bindings: List[SystemBinding] = []
    coded_properties: List[CodedProperty] = []   # high-card columns → coded object props
    classes_bound: Set[Tuple[URIRef, str]] = set()  # (vocab_class, source_id) with ≥1 binding

    for d in detected:
        source = source_by_id.get(d.source_id)
        if source is None:
            continue
        key = (d.table.lower(), d.column.lower())
        vocab_class = vocab_class_by_col.get(key)
        all_values = list(d.matched_values) + list(d.unmatched_values)

        if vocab_class is not None:
            label_to_ind = _label_to_individual(graph, vocab_class)
            for value in all_values:
                ind = label_to_ind.get(value) or label_to_ind.get(value.casefold())
                if ind is None:
                    unmapped.append(UnmappedValue(
                        table=d.table, column=d.column, value=value, reason="no-individual"))
                    continue
                iris = source.concept_iris(value)
                if not iris:
                    unmapped.append(UnmappedValue(
                        table=d.table, column=d.column, value=value, reason="not-in-source"))
                    continue
                ind_uri = str(ind)
                ambiguous = len(iris) > 1
                confidence = d.overlap
                confirmed = confirmed_by_ind.get(ind_uri)

                if confirmed and confirmed.get("concept_iri") == iris[0]:
                    bound[ind_uri] = ProposedBinding(
                        individual_uri=ind_uri, raw_value=value,
                        source_id=d.source_id, system_label=d.system_label,
                        concept_iri=iris[0], confidence=float(confirmed.get("confidence") or confidence),
                        method=_METHOD_HUMAN, status=_STATUS_CONFIRMED,
                    )
                    classes_bound.add((vocab_class, d.source_id))
                    continue

                proposal = ProposedBinding(
                    individual_uri=ind_uri, raw_value=value,
                    source_id=d.source_id, system_label=d.system_label,
                    concept_iri=iris[0], confidence=confidence,
                    method=_METHOD_OVERLAP, status=_STATUS_PENDING,
                    candidate_iris=iris if ambiguous else [],
                )
                if ambiguous or confidence < floor:
                    # sidecar — not asserted; do not overwrite an already-bound individual
                    if ind_uri not in bound:
                        sidecar_inds[ind_uri] = proposal
                else:
                    bound[ind_uri] = proposal
                    sidecar_inds.pop(ind_uri, None)
                    classes_bound.add((vocab_class, d.source_id))
        else:
            # High-cardinality coded column → re-type its property to a terminology-
            # coded OBJECT property whose range is a conformed per-system code class,
            # and flag it with fm:terminologySystem. The per-code individuals and their
            # skos:exactMatch are minted lazily at materialisation (one per distinct
            # code), so the full code set is never needed here. A detected column with
            # no property yet is a coverage gap — the generator must model it first.
            props = props_by_col.get(key, [])
            if not props:
                for value in all_values:
                    unmapped.append(UnmappedValue(
                        table=d.table, column=d.column, value=value, reason="no-property"))
                continue
            cls = _system_class_uri(base_iri, d.source_id)
            if (cls, RDF.type, OWL.Class) not in graph:
                graph.add((cls, RDF.type, OWL.Class))
                graph.add((cls, RDFS.label, Literal(f"{d.system_label} concept", lang="en")))
                graph.add((cls, RDFS.comment, Literal(
                    f"A concept from the {d.system_label} code system. Range of "
                    f"terminology-coded properties; one individual is minted per "
                    f"distinct source code at materialisation.", lang="en")))
                sys_iri = source.system_iri()
                if sys_iri:
                    graph.add((cls, SKOS.closeMatch, URIRef(sys_iri)))
                    system_bindings.append(SystemBinding(
                        subject_uri=str(cls), subject_kind="class",
                        source_id=d.source_id, system_label=d.system_label,
                        system_iri=sys_iri, predicate="skos:closeMatch"))
            for prop in props:
                graph.remove((prop, RDF.type, OWL.DatatypeProperty))
                graph.add((prop, RDF.type, OWL.ObjectProperty))
                for r in list(graph.objects(prop, RDFS.range)):
                    if str(r).startswith(str(XSD)):
                        graph.remove((prop, RDFS.range, r))
                graph.add((prop, RDFS.range, cls))
                graph.add((prop, _FM_TERM_SYSTEM, Literal(d.system_label)))
                graph.add((prop, _FM_TERM_CONFIDENCE, Literal(round(d.overlap, 4), datatype=XSD.decimal)))
                graph.add((prop, _FM_TERM_METHOD, Literal(_METHOD_OVERLAP)))
                coded_properties.append(CodedProperty(
                    property_uri=str(prop), system_id=d.source_id, system_label=d.system_label,
                    system_class_uri=str(cls), overlap=d.overlap))

    # Replay confirmed bindings detection did not reach (e.g. sources unconfigured
    # on a warm run): assert them from the store as long as the individual exists.
    for ind_uri, row in confirmed_by_ind.items():
        if ind_uri in bound:
            continue
        if not any(graph.triples((URIRef(ind_uri), None, None))):
            continue  # individual not in this graph — nothing to bind
        bound[ind_uri] = ProposedBinding(
            individual_uri=ind_uri, raw_value=str(row.get("raw_value") or ""),
            source_id=str(row.get("source_id") or ""),
            system_label=str(row.get("system_label") or ""),
            concept_iri=str(row["concept_iri"]),
            confidence=float(row.get("confidence") or 1.0),
            method=_METHOD_HUMAN, status=_STATUS_CONFIRMED,
        )
        sidecar_inds.pop(ind_uri, None)

    # System-level alignment for confidently-bound vocab classes (best-effort).
    for vocab_class, source_id in classes_bound:
        source = source_by_id.get(source_id)
        sys_iri = source.system_iri() if source else None
        if not sys_iri:
            continue
        system_bindings.append(SystemBinding(
            subject_uri=str(vocab_class), subject_kind="class",
            source_id=source_id, system_label=source.system_label,
            system_iri=sys_iri, predicate="skos:closeMatch"))

    # ── Emit into the graph ────────────────────────────────────────────────
    for b in bound.values():
        _emit_individual_binding(graph, b)
    for sb in system_bindings:
        _emit_system_binding(graph, sb)

    asserted = list(bound.values())
    sidecar = list(sidecar_inds.values())

    # ── Persist proposals for review (PENDING only; confirmed already stored) ─
    if store is not None:
        pending = [b for b in asserted if b.status == _STATUS_PENDING] + sidecar
        store.replace_proposals(
            pending, ontology_source=ontology_source, db_source=db_source,
            run_id=run_id or str(uuid.uuid4()),
        )

    result = BindingResult(
        detected_columns=detected,
        asserted=asserted,
        sidecar=sidecar,
        system_bindings=system_bindings,
        coded_properties=coded_properties,
        unmapped=unmapped,
    )
    logger.info(
        "Terminology binding: %d column(s) detected, %d individual binding(s) asserted "
        "(%d confirmed), %d coded object property(ies) (codes minted at materialisation), "
        "%d sidecar candidate(s), %d system-level alignment(s), %d unmapped value(s)",
        len(detected), len(asserted),
        sum(1 for b in asserted if b.status == _STATUS_CONFIRMED),
        len(coded_properties), len(sidecar), len(system_bindings), len(unmapped),
    )
    return result
