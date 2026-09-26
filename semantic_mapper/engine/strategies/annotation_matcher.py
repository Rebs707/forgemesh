# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
AnnotationMatcher — Tier 1 strategy.

Scores ontology properties by scanning skos:altLabel, FM source annotations,
rdfs:label, and rdfs:comment for column name references.

Scoring tiers
-------------
1.0  — explicit mapping pattern ("Maps to X in source data", "source column: X", ...)
       OR verbatim word-boundary occurrence in skos:altLabel / FM FK annotation
0.95 — case-insensitive word-boundary match in skos:altLabel / FM FK annotation
≤0.75 — fuzzy / partial match against any annotation text (capped at 0.75)
0.0   — no match; property is omitted from the evidence trail entirely

Word-boundary matching is deliberately restricted to altLabel/FK annotations and
NOT applied to rdfs:comment. Comments are free-form prose that will contain common
English words (e.g. "DST type") which would produce false 1.0 matches for short
column names like "type".

Corrections store lookups are handled by HistoryMatcher (a separate strategy).
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

import rdflib
from rdflib import OWL, RDF, RDFS, Namespace
from rapidfuzz import fuzz

_FM = Namespace("http://forgemesh.ai/ontology#")

from semantic_mapper.config.settings import Settings
from semantic_mapper.db.models import ColumnProfile, TableProfile
from semantic_mapper.engine.strategies.base import BaseMappingStrategy
from semantic_mapper.schema.mapping_output import MappingEvidence

logger = logging.getLogger(__name__)

_SKOS_ALT_LABEL = rdflib.URIRef("http://www.w3.org/2004/02/skos/core#altLabel")

# Matches "table_name.col_name -> ClassName" — extracts the column part.
_FK_COL_RE = re.compile(r"[^.]+\.([^ ]+)\s*->")

# Ordered from most-specific to least-specific.  The first group extracts an
# explicit source-column token; the second checks for verbatim occurrence.
_EXPLICIT_PATTERNS: List[re.Pattern] = [
    re.compile(r"\bmaps?\s+to\s+(\S+?)\s+in\s+(?:the\s+)?source", re.IGNORECASE),
    re.compile(r"\bsource\s+column[:\s]+(\S+)", re.IGNORECASE),
    re.compile(r"\bsource\s+field[:\s]+(\S+)", re.IGNORECASE),
    re.compile(r"\bmapped?\s+from\s+(\S+)", re.IGNORECASE),
]

# Minimum fuzzy ratio to emit a non-zero evidence item (capped at 0.75).
_FUZZY_MIN = 0.60


class AnnotationMatcher(BaseMappingStrategy):
    """
    Parameters
    ----------
    graph:
        Loaded rdflib.Graph for the ontology.
    settings:
        Pipeline settings instance.
    """

    def __init__(
        self,
        graph: rdflib.Graph,
        settings: Settings,
    ) -> None:
        super().__init__(graph, settings)
        # uri → (precise_texts, fuzzy_texts)
        # precise_texts: skos:altLabel + FK col name — used for word-boundary matching
        # fuzzy_texts:   rdfs:label + rdfs:comment — used for fuzzy matching only
        # Keeping them separate prevents free-form comment prose (e.g. "DST type")
        # from producing false 1.0 verbatim matches against short column names like "type".
        self._prop_annotations: Dict[str, Tuple[List[str], List[str]]] = {}
        self._build_annotation_cache()
        # table_name → primary-entity class URI, derived from fm:sourceTable provenance.
        self._table_entity_class: Dict[str, str] = {}
        self._build_source_table_entity_map()

    # ------------------------------------------------------------------
    # Cache building
    # ------------------------------------------------------------------

    def _build_annotation_cache(self) -> None:
        props = list(self.graph.subjects(RDF.type, OWL.DatatypeProperty)) + list(
            self.graph.subjects(RDF.type, OWL.ObjectProperty)
        )
        for prop in props:
            precise: List[str] = []  # altLabel + FK col — word-boundary safe
            fuzzy: List[str] = []    # label + comment — free-form prose
            for obj in self.graph.objects(prop, _SKOS_ALT_LABEL):
                t = str(obj).strip()
                if t:
                    precise.append(t)
            for predicate in (RDFS.label, RDFS.comment):
                for obj in self.graph.objects(prop, predicate):
                    t = str(obj).strip()
                    if t:
                        fuzzy.append(t)
            # For ObjectProperties: extract the source column name from
            # fm:sourceForeignKey "table.col -> ClassName" so FK columns
            # get annotation credit against the correct ObjectProperty.
            for fk_val in self.graph.objects(prop, _FM.sourceForeignKey):
                m = _FK_COL_RE.match(str(fk_val))
                if m:
                    precise.append(m.group(1))
            if precise or fuzzy:
                self._prop_annotations[str(prop)] = (precise, fuzzy)
        logger.debug(
            "AnnotationMatcher: cached annotations for %d properties",
            len(self._prop_annotations),
        )

    # ------------------------------------------------------------------
    # Strategy interface
    # ------------------------------------------------------------------

    def evaluate_column(
        self,
        profile: ColumnProfile,
        table_profile: TableProfile,
    ) -> List[MappingEvidence]:
        evidence: List[MappingEvidence] = []
        col_name = profile.name

        for prop_uri, (precise, fuzzy) in self._prop_annotations.items():
            ann_score, ann_rationale = self._score(col_name, precise, fuzzy)
            if ann_score > 0.0:
                evidence.append(
                    MappingEvidence(
                        strategy="AnnotationMatcher",
                        score=ann_score,
                        rationale=ann_rationale,
                        mapped_uri=prop_uri,
                    )
                )

        logger.debug(
            "AnnotationMatcher: %s → %d evidence item(s)",
            col_name,
            len(evidence),
        )
        return evidence

    def _property_source_table(self, prop) -> Optional[str]:
        """Return the source table a property was derived from, read from its
        fm:sourceColumn ("table.col") or fm:sourceForeignKey ("table.col -> Class")
        annotation.  Returns None for abstract / unannotated properties."""
        for sc in self.graph.objects(prop, _FM.sourceColumn):
            s = str(sc)
            if "." in s:
                return s.split(".", 1)[0].strip()
        for fk in self.graph.objects(prop, _FM.sourceForeignKey):
            left = str(fk).split("->", 1)[0]
            if "." in left:
                return left.split(".", 1)[0].strip()
        return None

    def _build_source_table_entity_map(self) -> None:
        """Map each source table to its primary ENTITY class via fm:sourceTable provenance.

        The generator stamps fm:sourceTable on EVERY class it derives from a table —
        both the entity class AND any value-mapped vocabulary classes minted from that
        table's categorical columns (Gender, Discipline, Status, …).  The ENTITY is the
        one that OWNS the table's columns: it is the rdfs:domain of the properties
        sourced from the table.  Value-mapped vocabulary classes are the RANGE of the
        table's object properties and are the domain of none of its columns.  So the
        entity = the fm:sourceTable class that is the rdfs:domain of the most properties
        sourced from that table.

        This is the only signal that links a table to its entity class when the table is
        NAMED after a measure but KEYED by another entity — e.g. a per-country
        medal-tally table `medals` (PK country_code) whose rows ARE countries.  Name
        matching can never connect `medals` → `Country`; this provenance can.  Wholly
        dataset-agnostic: no table or column names are referenced.
        """
        # class URI → source table (lower-cased for robust comparison)
        cls_table: Dict[str, str] = {}
        for cls in self.graph.subjects(RDF.type, OWL.Class):
            for st in self.graph.objects(cls, _FM.sourceTable):
                cls_table[str(cls)] = str(st).strip().lower()
                break
        if not cls_table:
            return

        # For each source table, count how many of its OWN columns each candidate class
        # is the rdfs:domain of.  The entity owns its columns; vocab classes own none.
        counts: Dict[str, Dict[str, int]] = {}
        props = list(self.graph.subjects(RDF.type, OWL.DatatypeProperty)) + list(
            self.graph.subjects(RDF.type, OWL.ObjectProperty)
        )
        for prop in props:
            src_table = self._property_source_table(prop)
            if not src_table:
                continue
            src_table = src_table.lower()
            for dom in self.graph.objects(prop, RDFS.domain):
                d = str(dom)
                if cls_table.get(d) == src_table:
                    counts.setdefault(src_table, {})[d] = (
                        counts.setdefault(src_table, {}).get(d, 0) + 1
                    )

        # Winner per table = the class owning the most of that table's columns.
        for table, cmap in counts.items():
            best_uri, best_n = max(cmap.items(), key=lambda kv: kv[1])
            if best_n > 0:
                self._table_entity_class[table] = best_uri

    def evaluate_table(self, profile: TableProfile) -> List[MappingEvidence]:
        """Authoritative table→entity match from fm:sourceTable provenance.

        Emitted at score 1.0 so it wins over NameMatcher's surface-name guesses: when a
        dimension table is named after a measure but keyed by an entity, the table name
        matches the wrong class (e.g. `medals` → Medal / Medallist), and only the
        generator's fm:sourceTable annotation identifies the true entity (Country).
        Returns nothing when the ontology carries no such provenance, leaving the
        existing NameMatcher behaviour untouched.
        """
        entity = self._table_entity_class.get(profile.table_name.strip().lower())
        if not entity:
            return []
        return [
            MappingEvidence(
                strategy="AnnotationMatcher",
                score=1.0,
                rationale=(
                    f"Source-table entity: ontology class is annotated "
                    f"fm:sourceTable='{profile.table_name}' and is the rdfs:domain of "
                    f"that table's columns (authoritative table→entity provenance)"
                ),
                mapped_uri=entity,
            )
        ]

    # ------------------------------------------------------------------
    # Scoring helpers
    # ------------------------------------------------------------------

    def _score(
        self,
        col_name: str,
        precise: List[str],
        fuzzy: List[str],
    ) -> Tuple[float, str]:
        """
        Return (score, rationale) for col_name against two text buckets.

        precise — skos:altLabel + FK col name: safe for verbatim word-boundary matching.
        fuzzy   — rdfs:label + rdfs:comment: free-form prose, fuzzy matching only.

        Returns (0.0, "") when no match is found.
        """
        col_lower = col_name.lower()
        all_texts = precise + fuzzy
        precise_combined = " ".join(precise)
        all_combined = " ".join(all_texts)

        # Tier 1: explicit source-column mapping pattern → 1.0 (searched in all texts)
        for pat in _EXPLICIT_PATTERNS:
            for m in pat.finditer(all_combined):
                extracted = m.group(1).rstrip(".,;)\"'").lower()
                if extracted == col_lower:
                    start = max(0, m.start() - 8)
                    snippet = all_combined[start : m.end() + 8].strip()
                    return (
                        1.0,
                        f"Explicit source mapping in annotation: '...{snippet}...'",
                    )

        # Tier 2a: verbatim word-boundary occurrence (case-sensitive) → 1.0
        # Only searched in precise texts (altLabel + FK col) to avoid false matches
        # from comment prose (e.g. "DST type" matching column "type").
        word_pat_cs = r"\b" + re.escape(col_name) + r"\b"
        if re.search(word_pat_cs, precise_combined):
            return (
                1.0,
                f"Column name '{col_name}' found verbatim in ontology altLabel/FK annotation",
            )

        # Tier 2b: case-insensitive word-boundary match on precise texts → 0.95
        word_pat_ci = r"\b" + re.escape(col_lower) + r"\b"
        m_ci = re.search(word_pat_ci, precise_combined.lower())
        if m_ci:
            matched_token = precise_combined[m_ci.start() : m_ci.end()]
            normalised_score = self.settings.ANNOTATION_NORMALISED_MATCH_SCORE
            return (
                normalised_score,
                (
                    f"Column name '{col_name}' matched '{matched_token}' "
                    f"case-insensitively in ontology altLabel/FK annotation"
                ),
            )

        # Tier 3: fuzzy token-set match on all annotation texts → ≤0.75
        clean_col = re.sub(r"[_\-]", " ", col_lower)
        for text in all_texts:
            if len(text) > 200:
                continue
            ratio = fuzz.token_set_ratio(clean_col, text.lower()) / 100.0
            if ratio >= _FUZZY_MIN:
                score = min(0.75, ratio)
                return (
                    score,
                    (
                        f"Fuzzy annotation match for '{col_name}' "
                        f"(similarity={ratio:.0%}): '{text[:60]}'"
                    ),
                )

        return 0.0, ""
