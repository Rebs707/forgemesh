# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Registry mapping adapter names → TerminologySource builders.

``build_sources(TERMINOLOGY_SOURCES)`` turns the generic config dict into live
source adapters.  Adding a code system is adding a config entry; adding a *kind*
of code system is registering one builder here (or via ``register_adapter`` from
an extension).  No dataset names, no per-dataset branches.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, List, Mapping

from .base import TerminologySource
from .offline_codeset import OfflineCodesetSource

logger = logging.getLogger(__name__)

# adapter name → builder(source_id, cfg) -> TerminologySource
SourceBuilder = Callable[[str, Mapping], TerminologySource]
_REGISTRY: Dict[str, SourceBuilder] = {}


def register_adapter(name: str, builder: SourceBuilder) -> None:
    """Register a terminology-source builder under *name* (the ``adapter`` key)."""
    _REGISTRY[name] = builder


def _build_offline_codeset(source_id: str, cfg: Mapping) -> TerminologySource:
    return OfflineCodesetSource(
        source_id=source_id,
        system_label=str(cfg.get("system_label") or source_id),
        iri_template=cfg.get("iri_template"),
        codes=cfg.get("codes"),
        path=cfg.get("path"),
        system_iri=cfg.get("system_iri"),
    )


# A live SNOMED/LOINC service is a later adapter behind the same contract; the
# first cut treats an inline/file code set as the stand-in for that service, so
# "stub_service" is an alias for the offline adapter — registering a real
# ServiceSource here later is a drop-in replacement.
register_adapter("offline_codeset", _build_offline_codeset)
register_adapter("stub_service", _build_offline_codeset)


def build_sources(config: Mapping) -> List[TerminologySource]:
    """Build all configured terminology sources from *config*.

    *config* is ``settings.TERMINOLOGY_SOURCES`` — ``{source_id: {adapter, ...}}``.
    Never raises: an unknown adapter or a malformed entry is logged and skipped,
    so a bad config degrades binding coverage rather than crashing generation.
    """
    sources: List[TerminologySource] = []
    if not config:
        return sources
    for source_id, cfg in config.items():
        if not isinstance(cfg, Mapping):
            logger.warning(
                "TERMINOLOGY_SOURCES[%r] is not a mapping — skipping", source_id
            )
            continue
        adapter = cfg.get("adapter", "offline_codeset")
        builder = _REGISTRY.get(adapter)
        if builder is None:
            logger.warning(
                "TERMINOLOGY_SOURCES[%r]: unknown adapter %r (known: %s) — skipping",
                source_id, adapter, sorted(_REGISTRY),
            )
            continue
        try:
            sources.append(builder(str(source_id), cfg))
        except Exception as exc:  # never raise — a bad source must not crash generation
            logger.warning(
                "TERMINOLOGY_SOURCES[%r]: adapter %r failed to build: %s — skipping",
                source_id, adapter, exc,
            )
    logger.info("Terminology: built %d source(s) from config", len(sources))
    return sources
