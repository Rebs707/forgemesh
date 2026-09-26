# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""ProofNarrator — main entry point for Feature 12 ontology-grounded proof generation.

Usage::

    narrator = ProofNarrator(ontology_graph)
    record = narrator.narrate(
        sparql=sparql_string,
        nl_question=question,
        rows=result_rows,
        query_id=query_id,
    )

The record is a ProofRecord (frozen Pydantic model, JSON-serialisable).
Generation is purely mechanical — no LLM call. Typical latency < 10ms.

If the ontology is None the narrator still produces a record, with lower
confidence interpretation and sparser scope/limitations.

If a FILTER references a property absent from the supplied ontology
(OntologyGroundingError from framing.py), the narrator logs it and falls back
to an ungrounded framing rather than raising.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from rdflib import Graph, OWL, RDF

from semantic_mapper.rdf.proof.framing import (
    _extract_prop_uris,
    _extract_type_uris,
    build_framing,
    expand_prefixes,
)
from semantic_mapper.rdf.proof.interpretation import build_interpretation
from semantic_mapper.rdf.proof.limitations import build_limitations
from semantic_mapper.rdf.proof.models import ProofRecord
from semantic_mapper.rdf.proof.scope import build_scope_assumptions


def _derive_ontology_namespace(graph: Optional[Graph]) -> Optional[str]:
    """Best-effort ontology namespace for expanding bare ``ont:`` CURIEs.

    The NL→SPARQL engine binds the ``ont`` prefix to the ontology namespace, but
    the stored/executed query usually omits the PREFIX header (prefixes are
    injected only at Fuseki-execution time). Without a declaration, prefix
    expansion — and therefore Phase-5 concept interpretation — sees no URIs and
    every receipt reads "no ontology concept resolved". We recover the namespace
    from the ontology graph itself: the most common namespace across declared
    classes and properties (which is exactly what the SPARQL predicates use).
    """
    if graph is None:
        return None
    counts: Counter = Counter()
    for rdf_type in (OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty):
        for s in graph.subjects(RDF.type, rdf_type):
            u = str(s)
            i = max(u.rfind("#"), u.rfind("/"))
            if i != -1:
                counts[u[: i + 1]] += 1
    if counts:
        return counts.most_common(1)[0][0]
    for s in graph.subjects(RDF.type, OWL.Ontology):  # fallback: owl:Ontology IRI
        base = str(s)
        return base if base.endswith(("#", "/")) else base + "#"
    return None


def _ensure_prefix_declared(sparql: str, prefix: str, namespace: str) -> str:
    """Prepend ``PREFIX prefix: <namespace>`` when the CURIE is used but undeclared.

    No-op when the namespace is unknown, the prefix is already declared, or the
    prefix never appears as a CURIE. The used/declared checks mirror
    ``expand_prefixes`` so anything injected here is expanded there.
    """
    if not namespace:
        return sparql
    if re.search(r"PREFIX\s+" + re.escape(prefix) + r"\s*:", sparql, re.IGNORECASE):
        return sparql
    if not re.search(r"(?<![<\"/])\b" + re.escape(prefix) + r":\w", sparql):
        return sparql
    return "PREFIX " + prefix + ": <" + namespace + ">\n" + sparql


def _compute_subgraph_hash(
    class_uris: List[str],
    prop_uris: List[str],
    ontology_graph: Optional[Graph],
) -> str:
    """SHA-256 of canonical N-Triples for the ontology subgraph relevant to the query.

    If no ontology, returns the SHA-256 of an empty string (predictable sentinel).
    Canonical N-Triples (sorted) ensure the hash is stable across rdflib ordering.
    """
    if ontology_graph is None:
        return hashlib.sha256(b"").hexdigest()

    from rdflib import URIRef

    relevant_nodes = set(class_uris) | set(prop_uris)
    triples: List[str] = []
    for uri in relevant_nodes:
        ref = URIRef(uri)
        for s, p, o in ontology_graph.triples((ref, None, None)):
            triples.append(f"<{s}> <{p}> {o.n3()} .")
        for s, p, o in ontology_graph.triples((None, None, ref)):
            triples.append(f"<{s}> <{p}> {o.n3()} .")

    canonical = "\n".join(sorted(set(triples)))
    return hashlib.sha256(canonical.encode()).hexdigest()


class ProofNarrator:
    """Generates a ProofRecord for each SPARQL query result.

    Instantiate once per engine run; reuse across queries.
    Pre-computes ontology indexes at init time so per-call overhead is minimal.
    """

    def __init__(self, ontology_graph: Optional[Graph] = None) -> None:
        self._ontology = ontology_graph
        self._ont_ns = _derive_ontology_namespace(ontology_graph)

    def narrate(
        self,
        sparql: str,
        nl_question: str,
        rows: List[Dict[str, Any]],
        query_id: str,
    ) -> ProofRecord:
        """Generate a ProofRecord for the given SPARQL query + result set.

        Framing errors — including OntologyGroundingError, when a FILTER
        references a URI absent from the ontology — are caught and produce a
        degraded (ungrounded) but valid record.
        """
        # Ensure the ontology 'ont:' prefix is declared so CURIEs expand to full
        # URIs even when the stored query lacks a PREFIX header (they are injected
        # only at execution time). Without this, URI extraction below finds nothing
        # and Phase-5 interpretation never grounds any concept.
        sparql = _ensure_prefix_declared(sparql, "ont", self._ont_ns)
        # Expand PREFIX declarations once; all submodules see full URIs.
        sparql = expand_prefixes(sparql)

        class_uris = _extract_type_uris(sparql)
        prop_uris = list(dict.fromkeys(_extract_prop_uris(sparql)))  # preserve order, deduplicate

        # Phase 2: Framing (receives pre-expanded SPARQL).
        # OntologyGroundingError means a FILTER/constraint references a property
        # absent from the OWL. Rather than aborting proof generation, produce a
        # degraded framing with no constraints and add a caveat so the caller still
        # gets a valid (lower-confidence) ProofRecord.
        try:
            framing = build_framing(sparql, rows, self._ontology)
        except Exception as _fe:
            from semantic_mapper.core.exceptions import OntologyGroundingError
            import logging as _log
            if isinstance(_fe, OntologyGroundingError):
                _log.getLogger(__name__).warning(
                    "narrator: OntologyGroundingError for query %s — "
                    "falling back to ungrounded framing: %s", query_id, _fe
                )
            else:
                _log.getLogger(__name__).warning(
                    "narrator: framing failed for query %s — "
                    "falling back to ungrounded framing: %s", query_id, _fe
                )
            framing = build_framing(sparql, rows, None)  # retry without grounding check

        # Phase 3: Scope assumptions
        constrained_prop_set = {c.property_uri for c in framing.constraints_applied}
        all_queried_classes = (
            [framing.primary_class_uri] + framing.joined_class_uris
            if framing.primary_class_uri
            else framing.joined_class_uris
        )
        scope = build_scope_assumptions(
            queried_class_uris=all_queried_classes,
            constrained_property_uris=constrained_prop_set,
            ontology_graph=self._ontology,
        )

        # Phase 4: Limitations
        limitations = build_limitations(
            queried_class_uris=all_queried_classes,
            constrained_property_uris=list(constrained_prop_set),
            ontology_graph=self._ontology,
        )

        # Phase 5: Interpretation
        interpretation = build_interpretation(
            nl_question=nl_question,
            sparql_class_uris=class_uris,
            sparql_property_uris=prop_uris,
            ontology_graph=self._ontology,
        )

        # Ontology subgraph hash
        subgraph_hash = _compute_subgraph_hash(class_uris, prop_uris, self._ontology)

        return ProofRecord(
            query_id=query_id,
            interpretation=interpretation,
            framing=framing,
            scope_assumptions=scope,
            limitations=limitations,
            ontology_subgraph_hash=subgraph_hash,
            generated_at=datetime.now(timezone.utc),
        )
