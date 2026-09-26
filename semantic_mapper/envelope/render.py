# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Inline receipt renderer.

``render_receipt(envelope) -> str`` turns an AnswerEnvelope into a compact,
numbered plain-text receipt so the provenance shows up in the SAME surface as the
answer — no need to open the receipt_url.

Pure function: envelope in, string out. No I/O, no network, deterministic.

Rendering rule (strict): only fields the envelope actually carries produce a line.
A null or empty field produces NO line — we never synthesise, infer, or paraphrase
a value that is not present. In particular:
  * an ``understood`` term with no ``grounded_uri`` is NOT rendered as a grounding
    (its reading may be a "interpreted as asked" placeholder, which must never read
    as a proven grounding);
  * when ``evidence.rows_scanned`` is null the scan->match funnel line is omitted
    entirely;
  * a source's confirmation is shown only when ``mapping_confirmed`` is set.

The failure mode this prevents is a line that reads as proven when the underlying
field is null.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from semantic_mapper.envelope.models import caveat_label


def _as_dict(envelope: Any) -> Dict[str, Any]:
    if hasattr(envelope, "model_dump"):
        return envelope.model_dump()
    return dict(envelope or {})


def _num(v: Any) -> str:
    return f"{v:,}" if isinstance(v, int) and not isinstance(v, bool) else str(v)


def _understood_line(receipt: Dict[str, Any]) -> str | None:
    # Only terms with a real grounding URI AND a reading. Never invent a reading.
    grounded = [
        t for t in (receipt.get("understood") or [])
        if t.get("grounded_uri") and t.get("term") and t.get("reading")
    ]
    if not grounded:
        return None
    parts = [f'"{t["term"]}" → {t["reading"]}' for t in grounded]
    return "Understood: " + "; ".join(parts)


def _sources_line(receipt: Dict[str, Any]) -> str | None:
    parts: List[str] = []
    for s in receipt.get("sources") or []:
        table = s.get("table")
        if not table:
            continue
        attrs: List[str] = []
        if s.get("layer"):
            attrs.append(str(s["layer"]))
        if s.get("join_key"):
            attrs.append(f"key {s['join_key']}")
        if s.get("mapping_confirmed"):            # confirmation only when present
            attrs.append(str(s["mapping_confirmed"]))
        parts.append(table + (f" ({', '.join(attrs)})" if attrs else ""))
    if not parts:
        return None
    return "Sources: " + "; ".join(parts)


def _evidence_line(receipt: Dict[str, Any]) -> str | None:
    ev = receipt.get("evidence") or {}
    scanned = ev.get("rows_scanned")
    if scanned is None:                            # no scan figure -> omit the funnel
        return None
    matched = ev.get("rows_matched")
    text = f"Evidence: {_num(scanned)} scanned → {_num(matched)} matched"
    funnel = ev.get("funnel") or []
    stages = [f"{_num(f.get('n'))} {f.get('label')}" for f in funnel if f.get("label")]
    # Only show the funnel detail when it adds intermediate stages beyond the
    # scanned->matched headline (a 2-step funnel just restates it).
    if len(stages) > 2:
        text += " (" + " → ".join(stages) + ")"
    return text


def _caveats_line(receipt: Dict[str, Any]) -> str | None:
    cav = receipt.get("caveats") or []
    if not cav:
        return None
    parts: List[str] = []
    for c in cav:
        label = c.get("label") or caveat_label(c.get("kind", ""))
        count = c.get("count")
        seg = f"{label} ×{_num(count)}" if isinstance(count, int) and count else label
        if c.get("note"):
            seg += f" — {c['note']}"
        parts.append(seg)
    return "Caveats: " + "; ".join(parts)


def render_receipt(envelope: Any) -> str:
    """Render an AnswerEnvelope (model or dict) to a compact numbered receipt."""
    env = _as_dict(envelope)
    receipt = env.get("receipt") or {}
    run_id = env.get("run_id") or receipt.get("run_id") or ""
    url = env.get("receipt_url") or ""

    header = f"Receipt — run {run_id}" if run_id else "Receipt"
    body: List[str] = [
        line for line in (
            _understood_line(receipt),
            _sources_line(receipt),
            _evidence_line(receipt),
            _caveats_line(receipt),
        ) if line
    ]

    out: List[str] = [header]
    for i, line in enumerate(body, 1):
        out.append(f"{i:02d}. {line}")
    if url:
        out.append(f"Full receipt: {url}")
    return "\n".join(out)
