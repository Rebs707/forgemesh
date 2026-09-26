# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Phase 7 — Proof composer.

Assembles the outputs of Phases 3–6 into an immutable ProofRecord.
The composer does not call any LLMs, databases, or external services —
all inputs arrive pre-computed from the caller.
"""
from __future__ import annotations

import re as _re
from datetime import datetime, timezone
from typing import Any, Dict, List

from semantic_mapper.config.settings import Settings
from semantic_mapper.prove.decision_lookup import MappingDecisionRef
from semantic_mapper.prove.lineage_walker import LineageChain
from semantic_mapper.prove.models import REQUIRED_LIMITATIONS, ProofRecord, ProofTree, RowProofRecord
from semantic_mapper.prove.narrator import InterpretationNote, ProofCaveat
from semantic_mapper.prove.query_rewriter import ProofQuery
from semantic_mapper.prove.subgraph import OntologySubgraph


def compose_proof(
    question: str,
    query: str,
    mode: str,
    answer: Dict[str, Any],
    run_id: str,
    proof_query: ProofQuery,
    subjects: List[str],
    chains: List[LineageChain],
    subgraph: OntologySubgraph,
    decisions: List[MappingDecisionRef],
    note: InterpretationNote,
    caveats: List[ProofCaveat],
    settings: Settings,
) -> ProofRecord:
    """Assemble a ProofRecord from pre-computed phase outputs.

    Args:
        question:    The original natural-language question.
        query:       The query string that was executed to produce answer.
        mode:        "sparql", "gold", or "silver".
        answer:      The scalar result, e.g. {"value": 51, "type": "count"}.
        run_id:      The mapping pipeline run ID (_mapping_run_id on gold rows).
        proof_query: Output of Phase 3 (query_rewriter.rewrite()).
        subjects:    The proof subject URIs / IDs returned by executing proof_query.
        chains:      Lineage chains from Phase 4 (lineage_walker).
        subgraph:    Ontology subgraph from Phase 1 (subgraph.extract_subgraph()).
        decisions:   Mapping decisions from Phase 5 (decision_lookup).
        note:        Interpretation note from Phase 6 (narrator.narrate()).
        caveats:     Accumulated ProofCaveat list (passed through narrator).
        settings:    Settings (unused at assembly time; reserved for future use).
    """
    return ProofRecord(
        query_mode=mode,
        question=question,
        answer=answer,
        interpretation=_serialise_interpretation(note, subgraph),
        ontology_subgraph=_serialise_subgraph(subgraph),
        executed_query=query,
        proof_query=proof_query.proof_query,
        subject_count=len(subjects),
        subjects=list(subjects),
        lineage=[_serialise_chain(c) for c in chains],
        mapping_decisions=[_serialise_decision(d) for d in decisions],
        caveats=[_serialise_caveat(c) for c in caveats],
        limitations=list(REQUIRED_LIMITATIONS),
        mapping_report_id=run_id,
        generated_at=datetime.now(timezone.utc).isoformat(),
        grouping_fields=list(proof_query.grouping_fields),
        queried_uris=_queried_uris(query, mode, decisions),
    )


def compose_proof_tree(proofs: List[ProofRecord]) -> ProofTree:
    """Wrap N ProofRecords into a ProofTree for agentic multi-step queries."""
    return ProofTree(proofs=list(proofs))


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _serialise_interpretation(
    note: InterpretationNote, subgraph: OntologySubgraph
) -> Dict[str, Any]:
    return {
        "interpreted_as": note.interpreted_as,
        "scope_assumptions": list(note.scope_assumptions),
        "not_included": list(note.not_included),
        "interpretation_confidence": note.interpretation_confidence,
        "interpretation_confidence_reason": note.interpretation_confidence_reason,
        "narrated": note.narrated,
        "ontology_subgraph_attached": bool(subgraph.classes or subgraph.properties),
    }


def _serialise_subgraph(subgraph: OntologySubgraph) -> Dict[str, Any]:
    return {
        "classes": {
            uri: {
                "uri": n.uri,
                "label": n.label,
                "comment": n.comment,
                "source_table": n.source_table,
            }
            for uri, n in subgraph.classes.items()
        },
        "properties": {
            uri: {
                "uri": n.uri,
                "label": n.label,
                "kind": n.kind,
                "source_column": n.source_column,
                "source_fk": n.source_fk,
                "domain_uri": n.domain_uri,
                "range_uri": n.range_uri,
            }
            for uri, n in subgraph.properties.items()
        },
        "adjacency_notes": list(subgraph.adjacency_notes),
    }


def _serialise_chain(chain: LineageChain) -> Dict[str, Any]:
    return {
        "subject_uri": chain.subject_uri,
        "row_id": chain.row_id,
        "gold": {
            "table": chain.gold.table,
            "id": chain.gold.id,
            "fields_used": list(chain.gold.fields_used),
            "row_values": dict(chain.gold.row_values),
        },
        "silver": [
            {"table": s.table, "id": s.id, "row_values": dict(s.row_values)}
            for s in chain.silver
        ],
        "bronze": [
            {"table": b.table, "pk": dict(b.pk), "raw_values": dict(b.raw_values)}
            for b in chain.bronze
        ],
        "broken": chain.broken,
        "broken_reason": chain.broken_reason,
    }


def _serialise_decision(d: MappingDecisionRef) -> Dict[str, Any]:
    return {
        "uri": d.uri,
        "kind": d.kind,
        "source": d.source,
        "winning_strategy": d.winning_strategy,
        "score": d.score,
        "confirmation": d.confirmation,
        "confirmed_at": d.confirmed_at,
        "evidence_trail": [
            {
                "strategy": e.strategy,
                "score": e.score,
                "rationale": e.rationale,
            }
            for e in d.evidence_trail
        ],
    }


def _serialise_caveat(c: ProofCaveat) -> Dict[str, Any]:
    return {
        "type": c.type,
        "count": c.count,
        "table": c.table,
        "property_uri": c.property_uri,
        "reason": c.reason,
    }


def _queried_uris(query: str, mode: str, decisions: List) -> List[str]:
    """Return URIs referenced in the executed query for UI drawer filtering.

    SPARQL: extract angle-bracket URIs directly from the query text.
    SQL:    match decision source column names against SQL identifiers.
           (URIs don't appear in SQL; column-name intersection is the best proxy.)
    """
    if mode == "sparql":
        return list(dict.fromkeys(_re.findall(r"<(https?://[^>]+)>", query)))
    # SQL: build a set of lowercase identifiers present in the query text,
    # then match each decision's source column name against that set.
    sql_tokens = {t.lower() for t in _re.findall(r"\b([a-zA-Z_]\w*)\b", query)}
    result: List[str] = []
    seen: set = set()
    for d in decisions:
        src_col = (d.source or "").split(".")[-1].lower()
        if src_col and src_col in sql_tokens and d.uri not in seen:
            seen.add(d.uri)
            result.append(d.uri)
    return result


def compose_row_proof(
    question: str,
    query: str,
    mode: str,
    row_grouping_values: Dict[str, Any],
    run_id: str,
    subjects: List[str],
    chains: List,
    decisions: List,
    caveats: List,
    primary_table: str = "",
    rdf_neighbourhood: Dict[str, List[Dict[str, str]]] | None = None,
    sparql_funnel: List[Dict[str, Any]] | None = None,
    funnel_unsupported_reason: str | None = None,
    sparql_matches: List[Dict[str, Any]] | None = None,
) -> RowProofRecord:
    """Assemble a RowProofRecord for a specific result row."""
    from datetime import datetime, timezone
    return RowProofRecord(
        query_mode=mode,
        row_grouping_values=row_grouping_values,
        contributing_subjects=list(subjects),
        primary_table=primary_table,
        lineage=[_serialise_chain(c) for c in chains],
        mapping_decisions_for_grouping=[_serialise_decision(d) for d in decisions],
        caveats=[_serialise_caveat(c) for c in caveats],
        limitations=list(REQUIRED_LIMITATIONS),
        mapping_report_id=run_id,
        generated_at=datetime.now(timezone.utc).isoformat(),
        rdf_neighbourhood=rdf_neighbourhood or {},
        sparql_funnel=sparql_funnel,
        funnel_unsupported_reason=funnel_unsupported_reason,
        sparql_matches=sparql_matches,
    )


# ---------------------------------------------------------------------------
# Match record helpers (Phase 7)
# ---------------------------------------------------------------------------


def infer_match_links(where_body: str, bound_vars: set) -> List[Dict[str, Any]]:
    """Return [{from_role, to_role, via_property, via_property_uri}] from WHERE clause triples.

    Finds triple patterns where both subject and object are bound URI variables.
    """
    from semantic_mapper.prove.sparql_funnel import _decompose

    links: List[Dict[str, Any]] = []
    seen: set = set()
    try:
        patterns = _decompose(where_body)
    except Exception:
        return []

    for ptype, ptext in patterns:
        if ptype != "triple":
            continue
        tokens = ptext.strip().split()
        if len(tokens) < 3:
            continue
        subj, pred = tokens[0], tokens[1]
        obj = tokens[2].rstrip(".;,")
        if not (subj.startswith("?") and obj.startswith("?")):
            continue
        from_var = subj[1:]
        to_var = obj[1:]
        if from_var not in bound_vars or to_var not in bound_vars:
            continue
        key = (from_var, to_var)
        if key in seen:
            continue
        seen.add(key)
        links.append({
            "from_role": from_var,
            "to_role": to_var,
            "via_property": _pred_local_name(pred),
            "via_property_uri": pred,
        })
    return links


def _pred_local_name(pred: str) -> str:
    pred = pred.strip()
    if pred.startswith("<") and pred.endswith(">"):
        inner = pred[1:-1]
        return inner.split("#")[-1].split("/")[-1]
    if "#" in pred:
        return pred.split("#")[-1]
    if ":" in pred:
        return pred.split(":")[-1]
    return pred
