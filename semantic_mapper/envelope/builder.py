# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""AnswerEnvelope builder (FP-1).

Assembles an :class:`AnswerEnvelope` from signals that already exist:

  * the executed query + tabular answer (always available at query time)
  * the v2 semantic proof (``proof_record_json``, stored at query time — cheap)
  * the full prove_answer result (v1 lineage + presentation — only on deep reads)

Two depths:

  * ``inline`` — built at query time from the answer + v2 proof + general
    detectors.  No bronze walk, no web-app round-trip.  This is what every
    ``/query`` and ``execute_*`` call attaches.
  * ``full``   — additionally folds in the prove_answer result (bronze lineage,
    mapping confirmations, broken-chain counts).  Used by the ``/runs/{id}``
    permalink and the MCP receipt resource.

Never raises: any assembly error degrades to a minimal-but-valid envelope so a
query never fails because its receipt could not be built.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from semantic_mapper.config.settings import Settings
from semantic_mapper.envelope.detectors import DetectorContext, run_detectors
from semantic_mapper.envelope.models import (
    ENVELOPE_VERSION,
    Answer,
    AnswerEnvelope,
    EvidenceBlock,
    FunnelStep,
    Receipt,
    SourceRef,
    TermReading,
)

DEFAULT_SAMPLE_ROWS = 3


# ── URL / handle helpers ──────────────────────────────────────────────────────

def _receipt_base(settings: Settings) -> str:
    base = getattr(settings, "RECEIPT_BASE_URL", "") or getattr(
        settings, "FORGEMESH_WEB_APP_URL", "http://127.0.0.1:5173"
    )
    return base.rstrip("/")


def receipt_url_for(run_id: str, settings: Settings) -> str:
    """Stable permalink to the FP-2 receipt for this run."""
    return f"{_receipt_base(settings)}/runs/{run_id}"


def row_set_handle_for(run_id: str) -> str:
    """Opaque handle resolving to the exact rows behind the answer (paged)."""
    return f"fm://runs/{run_id}/rows"


# ── Query parsing (general, dataset-agnostic) ─────────────────────────────────

# Words that may follow a table and must NOT be mistaken for its alias.
_ALIAS_STOP = (
    "ON|WHERE|GROUP|ORDER|LIMIT|JOIN|LEFT|RIGHT|INNER|OUTER|CROSS|FULL|"
    "USING|HAVING|UNION|NATURAL|STRAIGHT_JOIN"
)
# A FROM/JOIN source: the table, then an optional alias (never a reserved word).
_SQL_SRC_RE = re.compile(
    r"\b(?:FROM|JOIN)\s+[`\"']?([\w.]+)[`\"']?"
    r"(?:\s+(?:AS\s+)?(?!(?:" + _ALIAS_STOP + r")\b)[`\"']?([A-Za-z_]\w*)[`\"']?)?",
    re.IGNORECASE,
)
# An ON clause's expression, up to the next join/clause keyword.
_SQL_ON_COND_RE = re.compile(
    r"\bON\s+(.+?)(?=\b(?:JOIN|LEFT|RIGHT|INNER|OUTER|CROSS|FULL|WHERE|GROUP|ORDER|"
    r"LIMIT|HAVING|UNION)\b|$)",
    re.IGNORECASE | re.DOTALL,
)
# A column reference: dotted identifiers, each optionally back-tick/quote wrapped
# per segment — so `t`.`c`, "t"."c", t.c and c all match.
_IDENT = r"[`\"']?\w+[`\"']?"
_QUALREF = _IDENT + r"(?:\." + _IDENT + r")*"
# A single equality inside an ON expression.
_SQL_EQ_RE = re.compile(r"(" + _QUALREF + r")\s*=\s*(" + _QUALREF + r")")


def _local_name(uri: str) -> str:
    return uri.split("#")[-1].split("/")[-1] if uri else uri


def _entity_from_gold_table(table_name: str) -> str:
    """``gold_icustay`` → ``Icustay`` — the graph-layer entity label for a gold table."""
    base = table_name[len("gold_"):] if table_name.startswith("gold_") else table_name
    return (base[:1].upper() + base[1:]) if base else table_name


def _split_qualified(token: str) -> tuple[Optional[str], str]:
    """Split ``alias.column`` into (qualifier, column); (None, column) when bare.

    Tolerates per-segment quoting (`` `t`.`c` ``) as emitted by MySQL/the LLM.
    """
    parts = [p.strip().strip("`\"'") for p in token.strip().split(".")]
    parts = [p for p in parts if p]
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    if parts:
        return None, parts[0]
    return None, ""


def _sql_source_join_keys(query: str) -> List[tuple[str, List[str]]]:
    """Return ordered ``(table, [join_keys])`` derived from the query's ON clauses.

    Each table's keys come from the equality conditions *that table participates
    in* — resolved by mapping the ON-clause qualifier (alias or table name) back to
    its table. A table joined on more than one key lists them all; a table with no
    resolvable key gets an empty list. Never assigns keys by table position.
    """
    query = query or ""

    # 1. Tables + aliases, in FROM/JOIN order.
    tables: List[str] = []
    alias_to_table: Dict[str, str] = {}
    for m in _SQL_SRC_RE.finditer(query):
        table, alias = m.group(1), m.group(2)
        if table not in tables:
            tables.append(table)
        alias_to_table[table.lower()] = table          # table may be used unaliased
        if alias:
            alias_to_table[alias.lower()] = table

    # 2. Attribute each equality's columns to the table its qualifier names.
    keys: Dict[str, List[str]] = {t: [] for t in tables}
    for on_match in _SQL_ON_COND_RE.finditer(query):
        for eq in _SQL_EQ_RE.finditer(on_match.group(1)):
            for side in (eq.group(1), eq.group(2)):
                qualifier, column = _split_qualified(side)
                if qualifier is None or not column:
                    continue                            # bare column → unattributable
                table = alias_to_table.get(qualifier.lower())
                if table and column not in keys[table]:
                    keys[table].append(column)

    return [(t, keys[t]) for t in tables]


# ── Block builders ────────────────────────────────────────────────────────────

def _build_understood(
    v2_proof: Optional[Dict[str, Any]],
    presentation: Optional[Dict[str, Any]],
    question: str,
) -> List[TermReading]:
    readings: List[TermReading] = []

    if v2_proof:
        interp = v2_proof.get("interpretation") or {}
        for concept in interp.get("interpreted_concepts") or []:
            term = concept.get("nl_phrase") or ""
            reading = concept.get("grounding_annotation") or _local_name(
                concept.get("mapped_to_uri", "")
            )
            if term and reading:
                readings.append(TermReading(
                    term=term, reading=reading,
                    grounded_uri=concept.get("mapped_to_uri") or None,
                ))
        # Constraints applied (e.g. a time filter) read as term→reading too.
        framing = v2_proof.get("framing") or {}
        for con in framing.get("constraints_applied") or []:
            prop = _local_name(con.get("property_uri", ""))
            form = con.get("filter_form") or con.get("property_definition") or ""
            if prop and form and not any(r.term == prop for r in readings):
                readings.append(TermReading(term=prop, reading=form))

    if not readings and presentation:
        qi = presentation.get("question_interpretation")
        if isinstance(qi, str) and qi.strip():
            readings.append(TermReading(term=question or "the question", reading=qi.strip()))

    if not readings:
        readings.append(TermReading(
            term=question or "the question",
            reading="interpreted as asked (no grounded term resolutions captured)",
        ))
    return readings


def _build_sources(
    v2_proof: Optional[Dict[str, Any]],
    presentation: Optional[Dict[str, Any]],
    executed_query: str,
    mode: str,
    mapping_confirmed: Optional[str],
) -> List[SourceRef]:
    sources: List[SourceRef] = []

    # Triplestore query: the graph is the top of the lineage — the entity was
    # queried in the knowledge graph, which is materialised from gold. A semantic
    # v2_proof is only produced for graph queries, so its presence is the signal.
    trace = (presentation or {}).get("data_layer_trace") or []

    if trace and v2_proof:
        # Lineage resolved: label the GRAPH top from the traced gold entity so it
        # matches the gold→silver→bronze descent below. The framing's primary class
        # may be a grouping dimension (e.g. Careunit), not the walked fact entity.
        top_gold = next(
            (e.get("table_name") for e in trace
             if e.get("layer") == "gold" and e.get("table_name")),
            None,
        )
        graph_label = _entity_from_gold_table(top_gold) if top_gold else None
        if not graph_label:
            primary = (v2_proof.get("framing") or {}).get("primary_class_uri")
            graph_label = _local_name(primary) if primary else None
        if graph_label:
            sources.append(SourceRef(
                table=graph_label, layer="graph",
                description="knowledge graph", mapping_confirmed=mapping_confirmed,
            ))

    if trace:
        # Group by table, keep the deepest layer label seen.
        for entry in trace:
            table = entry.get("table_name")
            if not table:
                continue
            if any(s.table == table for s in sources):
                continue
            sources.append(SourceRef(
                table=table,
                layer=entry.get("layer"),
                mapping_confirmed=mapping_confirmed,
            ))
    elif v2_proof:
        # No pipeline trace (e.g. a grouped query with no single walkable subject):
        # list the queried classes as graph-layer sources.
        framing = v2_proof.get("framing") or {}
        primary = framing.get("primary_class_uri")
        for uri in [primary] + list(framing.get("joined_class_uris") or []):
            if uri and not any(s.table == _local_name(uri) for s in sources):
                sources.append(SourceRef(
                    table=_local_name(uri), layer="graph",
                    description="knowledge graph" if uri == primary else "ontology class",
                    mapping_confirmed=mapping_confirmed,
                ))

    # Inline SQL / bronze: parse table + join keys straight from the query. Each
    # table's join_key is derived from its OWN ON clause (see _sql_source_join_keys),
    # never assigned by position. Tables with no resolvable key emit None.
    if not sources and mode in ("gold", "silver", "bronze"):
        for table, join_keys in _sql_source_join_keys(executed_query):
            sources.append(SourceRef(
                table=table, layer=mode,
                join_key=", ".join(join_keys) if join_keys else None,
                mapping_confirmed=mapping_confirmed,
            ))

    return sources


def _build_evidence(
    answer: Answer,
    run_id: str,
    v2_proof: Optional[Dict[str, Any]],
    presentation: Optional[Dict[str, Any]],
    sample_rows: int,
) -> EvidenceBlock:
    # rows_matched: prefer the semantic cardinality, else the answer row count.
    rows_matched = answer.row_count
    if v2_proof:
        card = (v2_proof.get("framing") or {}).get("cardinality")
        if isinstance(card, int):
            rows_matched = card

    rows_scanned: Optional[int] = None
    if presentation:
        rc = (presentation.get("result_confidence") or {})
        # subject scan count is the closest generally-available "scanned" figure.
        scanned = rc.get("subject_count") or rc.get("rows_scanned")
        if isinstance(scanned, int):
            rows_scanned = scanned

    # Sample: first N answer rows as column→value dicts.
    sample: List[Dict[str, Any]] = []
    for row in answer.rows[:sample_rows]:
        sample.append({col: row[i] if i < len(row) else None
                       for i, col in enumerate(answer.columns)})

    # Funnel: only meaningful for a multi-stage narrowing we can quantify.
    funnel: List[FunnelStep] = []
    if rows_scanned is not None and rows_scanned != rows_matched:
        funnel = [
            FunnelStep(n=rows_scanned, label="rows scanned"),
            FunnelStep(n=rows_matched, label="rows matched the query"),
        ]

    return EvidenceBlock(
        rows_matched=rows_matched,
        rows_scanned=rows_scanned,
        sample=sample,
        sample_shown=len(sample),
        row_set=row_set_handle_for(run_id),
        funnel=funnel,
    )


def _proof_caveat_dicts(prove_result: Optional[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], int]:
    """Reconstruct pipeline caveat dicts + broken count from a prove_answer result."""
    if not prove_result:
        return [], 0
    summary = prove_result.get("lineage_summary") or {}
    audit = summary.get("data_audit_summary") or {}
    caveats: List[Dict[str, Any]] = []
    q = audit.get("quarantined_rows") or 0
    n = audit.get("null_excluded_rows") or 0
    if q:
        caveats.append({"type": "QUARANTINED", "count": int(q)})
    if n:
        caveats.append({"type": "NULL_EXCLUDED", "count": int(n)})
    broken = int(audit.get("broken_lineage_count") or 0)
    return caveats, broken


# ── Public entry point ────────────────────────────────────────────────────────

def build_envelope(
    *,
    run_id: str,
    question: str,
    executed_query: str,
    mode: str,
    answer: Dict[str, Any],
    settings: Settings,
    v2_proof: Optional[Dict[str, Any]] = None,
    prove_result: Optional[Dict[str, Any]] = None,
    ontology_version: Optional[str] = None,
    sample_rows: Optional[int] = None,
) -> AnswerEnvelope:
    """Assemble an AnswerEnvelope. Depth is ``full`` when ``prove_result`` is given."""
    try:
        return _build(
            run_id=run_id, question=question, executed_query=executed_query,
            mode=mode, answer=answer, settings=settings, v2_proof=v2_proof,
            prove_result=prove_result, ontology_version=ontology_version,
            sample_rows=sample_rows,
        )
    except Exception:
        # Minimal-but-valid fallback — a query never fails on receipt assembly.
        ans = Answer.from_result(answer or {})
        return AnswerEnvelope(
            run_id=run_id,
            answer=ans,
            receipt=Receipt(
                understood=[TermReading(
                    term=question or "the question",
                    reading="receipt could not be fully assembled",
                )],
                sources=[],
                evidence=EvidenceBlock(
                    rows_matched=ans.row_count,
                    row_set=row_set_handle_for(run_id),
                ),
                caveats=[],
                run_id=run_id,
                question=question or "",
                query_text=executed_query or "",
                ontology_version=ontology_version,
                depth="inline",
            ),
            receipt_url=receipt_url_for(run_id, settings),
        )


def _build(
    *, run_id, question, executed_query, mode, answer, settings,
    v2_proof, prove_result, ontology_version, sample_rows,
) -> AnswerEnvelope:
    if sample_rows is None:
        sample_rows = getattr(settings, "ENVELOPE_SAMPLE_ROWS", DEFAULT_SAMPLE_ROWS)

    # v2 proof may arrive via prove_result too (deep read).
    if v2_proof is None and prove_result:
        v2_proof = prove_result.get("semantic_proof")
    presentation = (prove_result or {}).get("proof_presentation")
    depth = "full" if prove_result else "inline"

    ans = Answer.from_result(answer or {})

    # Mapping confirmation summary (full depth only).
    mapping_confirmed: Optional[str] = None
    if prove_result:
        mc = ((prove_result.get("lineage_summary") or {}).get("mapping_confidence_summary") or {})
        if mc.get("all_mappings_human_confirmed"):
            mapping_confirmed = "confirmed by operator"
        elif mc.get("human_confirmed_count") or mc.get("auto_confirmed_count"):
            mapping_confirmed = "mapping confirmed"

    understood = _build_understood(v2_proof, presentation, question)
    sources = _build_sources(v2_proof, presentation, executed_query, mode, mapping_confirmed)
    evidence = _build_evidence(ans, run_id, v2_proof, presentation, sample_rows)

    proof_caveats, broken = _proof_caveat_dicts(prove_result)
    ctx = DetectorContext(
        executed_query=executed_query or "",
        mode=mode,
        columns=ans.columns,
        rows=[dict(zip(ans.columns, r)) for r in ans.rows],
        proof_caveats=proof_caveats,
        broken_lineage_count=broken,
        small_group_threshold=getattr(
            settings, "ENVELOPE_SMALL_GROUP_THRESHOLD", 20
        ),
        query_rewrites=list((answer or {}).get("query_rewrites") or []),
    )
    caveats = run_detectors(ctx)

    if ontology_version is None and v2_proof:
        ontology_version = v2_proof.get("ontology_subgraph_hash") or None

    receipt = Receipt(
        understood=understood,
        sources=sources,
        evidence=evidence,
        caveats=caveats,
        run_id=run_id,
        question=question or "",
        query_text=executed_query or "",
        ontology_version=ontology_version,
        depth=depth,
    )
    return AnswerEnvelope(
        envelope_version=ENVELOPE_VERSION,
        run_id=run_id,
        answer=ans,
        receipt=receipt,
        receipt_url=receipt_url_for(run_id, settings),
    )
