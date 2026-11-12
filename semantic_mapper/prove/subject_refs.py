# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Resolve graph subjects to their gold rows via ``fm:bronzeSourceRef``.

Every materialised subject carries ``fm:bronzeSourceRef`` with the same value as
its gold row's ``_bronze_source_ref`` column (see triple_builder). That literal is
the provenance join key from the graph back to gold, independent of how the
subject URI itself was minted.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, List

logger = logging.getLogger(__name__)

BRONZE_SOURCE_REF = "http://forgemesh.ai/ontology#bronzeSourceRef"
_BATCH = 500


def bronze_source_refs(
    subjects: Iterable[str],
    execute_sparql: Callable[[str], List[Dict[str, Any]]],
    batch_size: int = _BATCH,
) -> Dict[str, str]:
    """Return {subject URI: fm:bronzeSourceRef value} for the given subjects.

    Looks in both the default graph and named graphs. Subjects with no ref are
    omitted. Never raises: a failed batch is logged and skipped, and the caller
    falls back to URI-based resolution for those subjects.
    """
    uris = [u for u in dict.fromkeys(subjects) if u and "<" not in u and ">" not in u]
    out: Dict[str, str] = {}
    for i in range(0, len(uris), batch_size):
        chunk = uris[i:i + batch_size]
        values = " ".join(f"<{u}>" for u in chunk)
        query = (
            f"SELECT ?s ?ref WHERE {{ VALUES ?s {{ {values} }} "
            f"{{ ?s <{BRONZE_SOURCE_REF}> ?ref }} UNION "
            f"{{ GRAPH ?g {{ ?s <{BRONZE_SOURCE_REF}> ?ref }} }} }}"
        )
        try:
            for row in execute_sparql(query):
                s, ref = row.get("s"), row.get("ref")
                if s and ref is not None:
                    out.setdefault(str(s), str(ref))
        except Exception as exc:
            logger.warning("bronzeSourceRef lookup failed for %d subjects: %s", len(chunk), exc)
    return out
