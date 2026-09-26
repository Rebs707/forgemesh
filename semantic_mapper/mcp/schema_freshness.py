# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Keep the MCP schema export in sync with the live artifacts and graph (Bug 2 hardening).

The MCP server loads the mapping report / gold definitions / ontology once at startup
and caches the built schema. If the pipeline is re-run (re-map, re-materialise) the
server keeps serving the OLD schema until it is manually restarted — so `get_schema`
can describe a different dataset than the one `execute_sparql` queries, and every join
silently returns zero rows.

Two pure helpers, decoupled from the server's live global state so they are unit
testable:

- ``artifact_fingerprint`` — a cheap on-disk staleness signal (file mtimes). When it
  changes, the server rebuilds its cached report/definitions/schema, so a
  re-materialise + re-map is served without a restart.
- ``schema_graph_disagreements`` — compares the SERVED schema (namespace + class URIs)
  against the LIVE graph (the class URIs that actually have instances) and returns
  human-readable mismatches, so a schema describing the wrong dataset is caught loudly
  instead of silently returning nothing.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


def artifact_fingerprint(paths: Iterable[str], base_dir: Optional[str] = None) -> Tuple:
    """Fingerprint files by ``(resolved_path, mtime)``.

    Missing files contribute ``mtime=None`` (so appearance/deletion is a change).
    Blank paths are ignored. Relative paths resolve against ``base_dir``. The result
    is order-independent (sorted), so callers compare fingerprints for equality to
    decide whether the underlying artifacts changed.
    """
    out: List[Tuple[str, Optional[float]]] = []
    for p in paths:
        if not p:
            continue
        pp = Path(p)
        if not pp.is_absolute() and base_dir:
            pp = Path(base_dir) / pp
        try:
            mtime: Optional[float] = os.path.getmtime(pp)
        except OSError:
            mtime = None
        out.append((str(pp), mtime))
    return tuple(sorted(out))


def namespace_of(uri: str) -> str:
    """The namespace of a class URI — up to and including ``#``, else the final ``/``."""
    if "#" in uri:
        return uri.rsplit("#", 1)[0] + "#"
    if "/" in uri:
        return uri.rsplit("/", 1)[0] + "/"
    return uri


def schema_graph_disagreements(
    served_namespace: Optional[str],
    served_class_uris: Iterable[str],
    graph_class_uris: Iterable[str],
) -> List[str]:
    """Return the reasons a served schema disagrees with the live graph.

    - The served namespace is absent from the graph's instantiated-class namespaces —
      the export describes a different ontology than what was materialised (the classic
      ``mimic_bronze#`` export vs ``mimic#`` graph stale-server bug).
    - None of the served classes has any instance in the graph.

    An empty ``graph_class_uris`` (graph unreachable or not yet materialised) yields no
    disagreement — staleness cannot be judged, and the caller decides how to treat an
    empty graph. Pure and side-effect free; keyed on URIs only.
    """
    graph_uris = {u for u in graph_class_uris if u}
    if not graph_uris:
        return []
    graph_namespaces = {namespace_of(u) for u in graph_uris}
    reasons: List[str] = []
    if served_namespace and served_namespace not in graph_namespaces:
        reasons.append(
            f"served schema namespace {served_namespace!r} is not among the graph's "
            f"class namespaces {sorted(graph_namespaces)} — the export describes a "
            f"different dataset than the one materialised"
        )
    served = {u for u in served_class_uris if u}
    if served and not (served & graph_uris):
        reasons.append(
            "none of the served schema's classes has any instance in the live graph"
        )
    return reasons
