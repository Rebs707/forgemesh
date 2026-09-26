# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Phase 8 — Deterministic markdown renderer for ProofRecord.

render_proof(record) called twice on the same record produces byte-identical
output.  Rules:
  - All lists sorted before rendering (subjects alphabetically, decisions by URI)
  - Timestamps formatted from record.generated_at (never datetime.now())
  - No randomness

Section order (fixed):
  1. Header
  2. What we understood
  3. The query that was executed
  4. Source data audit
  5. Mapping confidence
  6. Limitations of this proof  ← always last
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from semantic_mapper.prove.models import ProofRecord
from semantic_mapper.prove.presentation import ProofPresentation

_TABLE_DISPLAY_LIMIT = 200   # max chains/subjects in source data audit
_VALUE_DISPLAY_LIMIT = 20    # max field rows per layer table


def render_proof(record: ProofRecord) -> str:
    """Render *record* to a deterministic markdown string."""
    sections = [
        _header(record),
        _interpretation(record),
        _query_section(record),
        _source_audit(record),
        _mapping_confidence(record),
        _limitations(record),
    ]
    return "\n\n---\n\n".join(s.strip() for s in sections) + "\n"


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _header(record: ProofRecord) -> str:
    answer_val = record.answer.get("value", "")
    answer_type = record.answer.get("type", "")
    answer_str = f"{answer_val} ({answer_type})" if answer_type else str(answer_val)
    ts = _fmt_timestamp(record.generated_at)
    lines = [
        f"# {record.question}",
        "",
        f"**Answer:** {answer_str} ✓ VERIFIED  ",
        f"**Mode:** {record.query_mode} | **Generated:** {ts}",
    ]
    if record.interpretation.get("ontology_subgraph_attached"):
        lines.append("**Ontology subgraph:** attached")
    return "\n".join(lines)


def _interpretation(record: ProofRecord) -> str:
    interp = record.interpretation
    lines = ["## What we understood", ""]

    if not interp.get("narrated"):
        lines.append("*Narration was disabled.*")
    else:
        interpreted_as = interp.get("interpreted_as", "")
        if interpreted_as:
            lines.append(interpreted_as)
            lines.append("")

        assumptions = sorted(interp.get("scope_assumptions") or [])
        if assumptions:
            lines.append("**Scope assumptions:**")
            for a in assumptions:
                lines.append(f"- {a}")
            lines.append("")

        not_included = sorted(interp.get("not_included") or [])
        if not_included:
            lines.append("**Not included:**")
            for n in not_included:
                lines.append(f"- {n}")
            lines.append("")

        confidence = interp.get("interpretation_confidence", "")
        reason = interp.get("interpretation_confidence_reason", "")
        if confidence or reason:
            lines.append(f"*Confidence: {confidence} — {reason}*")
            lines.append("")

    caveats = sorted(record.caveats, key=lambda c: c.get("type", ""))
    if caveats:
        lines.append("**Caveats:**")
        for c in caveats:
            parts = [c.get("type", "")]
            if c.get("count") is not None:
                parts.append(f"count={c['count']}")
            if c.get("table"):
                parts.append(f"table={c['table']}")
            if c.get("reason"):
                parts.append(c["reason"])
            lines.append("- " + " — ".join(p for p in parts if p))

    return "\n".join(lines)


def _query_section(record: ProofRecord) -> str:
    mode_hint = "sparql" if record.query_mode == "sparql" else "sql"
    lines = [
        "## The query that was executed",
        "",
        f"```{mode_hint}",
        record.executed_query.strip(),
        "```",
        "",
        "**Proof query:**",
        "",
        f"```{mode_hint}",
        record.proof_query.strip(),
        "```",
    ]
    return "\n".join(lines)


def _source_audit(record: ProofRecord) -> str:
    lines = ["## Source data audit", ""]

    if record.subject_count == 0:
        lines.append("No subjects.")
        return "\n".join(lines)

    count_note = f"{record.subject_count} subject(s) confirmed."
    if record.subject_count > _TABLE_DISPLAY_LIMIT:
        count_note += f" Showing first {_TABLE_DISPLAY_LIMIT}."
    lines.append(count_note)

    if not record.lineage:
        lines.append("")
        for subj in sorted(record.subjects)[:_TABLE_DISPLAY_LIMIT]:
            lines.append(f"- `{subj}`")
        return "\n".join(lines)

    for chain in record.lineage[:_TABLE_DISPLAY_LIMIT]:
        lines.append("")
        subject_label = chain.get("subject_uri") or f"row {chain.get('row_id', '?')}"
        lines.append(f"### {subject_label}")
        lines.append("")

        gold = chain.get("gold", {})
        if gold.get("table"):
            gold_vals = gold.get("row_values") or {}
            lines.append(f"**Gold** (`{gold['table']}`, row {gold.get('id', '?')})")
            if gold_vals:
                lines.append("")
                lines.append("| Field | Value |")
                lines.append("|---|---|")
                for k, v in list(gold_vals.items())[:_VALUE_DISPLAY_LIMIT]:
                    lines.append(f"| {k} | {_safe_val(v)} |")
            lines.append("")

        for s in chain.get("silver", []):
            if s.get("table"):
                silver_vals = s.get("row_values") or {}
                lines.append(f"**Silver** (`{s['table']}`, row {s.get('id', '?')})")
                if silver_vals:
                    lines.append("")
                    lines.append("| Field | Value |")
                    lines.append("|---|---|")
                    for k, v in list(silver_vals.items())[:_VALUE_DISPLAY_LIMIT]:
                        lines.append(f"| {k} | {_safe_val(v)} |")
                lines.append("")

        for b in chain.get("bronze", []):
            if b.get("table"):
                pk_str = ", ".join(
                    f"{k}={v}" for k, v in sorted((b.get("pk") or {}).items())
                )
                raw_vals = b.get("raw_values") or {}
                lines.append(
                    f"**Bronze** (`{b['table']}`{(', ' + pk_str) if pk_str else ''})"
                )
                if raw_vals:
                    lines.append("")
                    lines.append("| Field | Value |")
                    lines.append("|---|---|")
                    for k, v in list(raw_vals.items())[:_VALUE_DISPLAY_LIMIT]:
                        lines.append(f"| {k} | {_safe_val(v)} |")
                lines.append("")

        if chain.get("broken"):
            reason = chain.get("broken_reason") or "unknown"
            lines.append(f"*Lineage incomplete: {reason}*")
            lines.append("")

    return "\n".join(lines)


def _mapping_confidence(record: ProofRecord) -> str:
    lines = ["## Mapping confidence", ""]
    decisions = sorted(record.mapping_decisions, key=lambda d: d.get("uri", ""))
    if not decisions:
        lines.append("No mapping decisions recorded.")
        return "\n".join(lines)

    lines.append("| URI | Kind | Source | Strategy | Score | Confirmation |")
    lines.append("|---|---|---|---|---|---|")
    for d in decisions:
        uri_local = _local_name(d.get("uri", ""))
        kind = d.get("kind", "")
        source = d.get("source", "")
        strategy = d.get("winning_strategy", "")
        score_raw = d.get("score", 0.0)
        score = f"{score_raw:.2f}"
        confirmation = d.get("confirmation", "")
        lines.append(f"| {uri_local} | {kind} | {source} | {strategy} | {score} | {confirmation} |")

    return "\n".join(lines)


def _limitations(record: ProofRecord) -> str:
    lines = ["## Limitations of this proof", ""]
    for lim in record.limitations:
        lines.append(f"- {lim}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_timestamp(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return iso


def _local_name(uri: str) -> str:
    if "#" in uri:
        return uri.rsplit("#", 1)[-1]
    return uri.rsplit("/", 1)[-1]


def _safe_val(v: Any, max_len: int = 200) -> str:
    s = str(v) if v is not None else ""
    return s[:max_len] + "…" if len(s) > max_len else s


# ---------------------------------------------------------------------------
# ProofPresentation renderer (section order is contractually fixed)
# ---------------------------------------------------------------------------

def render_presentation(pres: ProofPresentation) -> str:
    """Render a ProofPresentation to deterministic markdown.

    Sections appear in the fixed contract order — do not reorder.
    """
    sections = [
        _pres_header(pres),
        _pres_query_display(pres),
        _pres_ontology_grounding(pres),
        _pres_layer_trace(pres),
        _pres_transformation_provenance(pres),
        _pres_result_confidence(pres),
        _pres_limitations(pres),
    ]
    return "\n\n---\n\n".join(s.strip() for s in sections) + "\n"


def _pres_header(pres: ProofPresentation) -> str:
    lines = [
        "# Proof",
        "",
        f"*contract v{pres.proof_presentation_version}*",
        "",
        f"## 1. Question interpretation",
        "",
        pres.question_interpretation,
    ]
    return "\n".join(lines)


def _pres_query_display(pres: ProofPresentation) -> str:
    qd = pres.query_display
    mode = "sparql" if qd.sparql.strip().upper().startswith("SELECT") else "sql"
    lines = [
        "## 2. Query executed",
        "",
        f"**Plain English:** {qd.plain_english_summary}  ",
        f"**Cache status:** {qd.cache_status}",
        "",
        f"```{mode}",
        qd.sparql.strip(),
        "```",
    ]
    return "\n".join(lines)


def _pres_ontology_grounding(pres: ProofPresentation) -> str:
    og = pres.ontology_grounding
    lines = ["## 3. Ontology grounding", ""]
    if og.classes_used:
        lines.append("**Classes:**")
        for c in og.classes_used:
            lines.append(f"- `{c}`")
        lines.append("")
    else:
        lines.append("*No classes identified.*")
        lines.append("")
    if og.properties_used:
        lines.append("**Properties:**")
        for p in og.properties_used:
            parent_note = f" ← `{p.abstract_parent}`" if p.abstract_parent else ""
            lines.append(f"- `{p.local_name}`{parent_note} (`{p.uri}`)")
    else:
        lines.append("*No properties identified.*")
    return "\n".join(lines)


def _pres_layer_trace(pres: ProofPresentation) -> str:
    lines = ["## 4. Data layer trace", ""]
    if not pres.data_layer_trace:
        lines.append("*Lineage unavailable.*")
        return "\n".join(lines)
    for lt in pres.data_layer_trace:
        cols = ", ".join(lt.column_names) if lt.column_names else "—"
        lines.append(f"**{lt.layer.upper()}** `{lt.table_name}`")
        lines.append(f"  columns: {cols}")
        lines.append("")
    return "\n".join(lines)


def _pres_transformation_provenance(pres: ProofPresentation) -> str:
    lines = ["## 5. Transformation provenance", ""]
    if not pres.transformation_provenance:
        lines.append("*No mapping decisions recorded.*")
        return "\n".join(lines)
    lines.append("| Property | Confirmation | Score | Strategy |")
    lines.append("|---|---|---|---|")
    for tr in pres.transformation_provenance:
        score = f"{tr.score:.2f}" if tr.score is not None else "—"
        strategy = tr.rule_reference or "—"
        lines.append(f"| {tr.local_name} | {tr.confirmation} | {score} | {strategy} |")
    return "\n".join(lines)


def _pres_result_confidence(pres: ProofPresentation) -> str:
    rc = pres.result_confidence
    lines = [
        "## 6. Result confidence",
        "",
        f"- **Row count:** {rc.row_count}",
        f"- **Quarantined rows:** {rc.quarantined_row_count}",
        f"- **Broken lineage chains:** {rc.broken_lineage_count}",
        f"- **Stale ontology warning:** {'yes' if rc.stale_ontology_warning else 'no'}",
    ]
    if rc.notes:
        lines.append("")
        lines.append("**Notes:**")
        for n in rc.notes:
            lines.append(f"- {n}")
    return "\n".join(lines)


def _pres_limitations(pres: ProofPresentation) -> str:
    lines = ["## 7. Limitations", ""]
    if not pres.limitations:
        lines.append("*No limitations recorded.*")
    else:
        for lim in pres.limitations:
            lines.append(f"- {lim}")
    return "\n".join(lines)
