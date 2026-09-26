# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
HistoryMatcher — Human override strategy.

Consults the corrections store for HUMAN-confirmed mappings only.  When a
human has explicitly validated or corrected a mapping via the web UI, that
decision is treated as a hard override: the aggregator short-circuits all
other strategy evidence and returns the human-confirmed URI immediately.

AUTO-confirmed corrections (pipeline-only runs with no human input) are
intentionally ignored.  Caching AUTO pipeline output in the corrections
store caused circular confidence inflation and poisoning of future runs
when the aggregator produced an incorrect result.

Match priority (PatternLookup, human_only=True)
-----------------------------------------------
1  Exact column name (case-insensitive)
2  Normalised name (abbreviation-expanded)
4* Type + ≥1 token overlap
3  ≥2 meaningful token overlap

* Priority 4 is evaluated before Priority 3; see PatternLookup for details.
"""
from __future__ import annotations

import logging
from typing import List, Optional

import rdflib

from semantic_mapper.config.settings import Settings
from semantic_mapper.db.corrections_store import PatternLookup
from semantic_mapper.db.models import ColumnProfile, TableProfile
from semantic_mapper.engine.strategies.base import BaseMappingStrategy
from semantic_mapper.schema.mapping_output import MappingEvidence

logger = logging.getLogger(__name__)


class HistoryMatcher(BaseMappingStrategy):
    """
    Parameters
    ----------
    graph:
        Loaded rdflib.Graph for the ontology.
    settings:
        Pipeline settings instance.
    pattern_lookup:
        PatternLookup from CorrectionsStore.load_patterns().  When None,
        evaluate_column() always returns an empty list.
    """

    def __init__(
        self,
        graph: rdflib.Graph,
        settings: Settings,
        pattern_lookup: Optional[PatternLookup] = None,
    ) -> None:
        super().__init__(graph, settings)
        self._pattern_lookup = pattern_lookup

    def evaluate_column(
        self,
        profile: ColumnProfile,
        table_profile: TableProfile,
    ) -> List[MappingEvidence]:
        if self._pattern_lookup is None:
            return []

        result = self._pattern_lookup.match(profile.name, profile.inferred_type, human_only=True)
        if result is None:
            return []

        uri, score, rationale = result
        logger.debug(
            "HistoryMatcher: %s.%s → score=%.2f (%s)",
            table_profile.table_name,
            profile.name,
            score,
            uri.rsplit("/", 1)[-1],
        )
        return [
            MappingEvidence(
                strategy="HistoryMatcher",
                score=score,
                rationale=rationale,
                mapped_uri=uri,
            )
        ]

    def evaluate_table(self, table_profile: TableProfile) -> List[MappingEvidence]:
        return []
