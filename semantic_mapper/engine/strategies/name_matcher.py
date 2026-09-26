# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
import re
import logging
from typing import Dict, List, Optional, Set, Tuple
from rapidfuzz import fuzz
from rdflib import RDF, RDFS, OWL
from semantic_mapper.engine.strategies.base import BaseMappingStrategy
from semantic_mapper.db.models import TableProfile, ColumnProfile
from semantic_mapper.schema.mapping_output import MappingEvidence

logger = logging.getLogger(__name__)


class NameMatcher(BaseMappingStrategy):
    """
    Scores each (column, property) pair using fuzzy lexical matching.

    For ordinary columns the score is simply:
        fuzz.token_set_ratio(clean_column_name, property_local_name) / 100

    FK-aware domain boost
    ─────────────────────
    Many ontologies have table-qualified subproperties such as:
        hasICUStayForChartEvent   (rdfs:domain ChartEvent)
        hasICUStayForTransfer     (rdfs:domain Transfer)

    The column name `icustay_id` fuzzy-matches `hasICUStay` well (~83%) but
    matches `hasICUStayForChartEvent` poorly (~43%) because `ForChartEvent`
    adds noise.  Without correction the generic base property always wins.

    For FK-like columns (name ends with _id, or listed in table_profile.foreign_keys):
      1. Strip any `For<TableClass>` suffix from the property local name before scoring.
         e.g. "hasicustayforchartevent" → "hasicustay" when processing chartevents.
      2. Add DOMAIN_MATCH_BOOST (default 0.15) to the resulting score.

    Conditions that must both hold for the boost to apply:
      - The FK base name (icustay_id → icustay) appears in the property local name.
      - The property's rdfs:domain overlaps with the current table's class
        (matched by prefix: chartevent ⊆ chartevents).

    This pushes the table-specific subproperty above the generic base while leaving
    all other candidates unaffected.
    """

    def __init__(self, graph, settings):
        super().__init__(graph, settings)
        # prop_uri → frozenset of domain local names (lower-cased, underscore-stripped)
        self._domain_map: Dict[str, frozenset] = {}
        self._build_domain_map()

    def _build_domain_map(self) -> None:
        for prop in list(self.graph.subjects(RDF.type, OWL.DatatypeProperty)) + \
                    list(self.graph.subjects(RDF.type, OWL.ObjectProperty)):
            domains = frozenset(
                re.sub(r"_", "", str(d).split("#")[-1].split("/")[-1]).lower()
                for d in self.graph.objects(prop, RDFS.domain)
            )
            self._domain_map[str(prop)] = domains
        logger.debug("NameMatcher: domain map built for %d properties", len(self._domain_map))

    # ── Table class normalisation ────────────────────────────────────────────

    @staticmethod
    def _table_forms(table_name: str) -> Set[str]:
        """Return normalised forms of the ontology class expected for this table.

        e.g. raw_chartevents → {'chartevents', 'chartevent', 'chartevents', 'chartevent'}
        The forms are lowercased and underscore-stripped so they can be compared
        against domain local names that went through the same normalisation.
        """
        stripped = re.sub(r"^raw_", "", table_name, flags=re.IGNORECASE).lower()
        no_under = re.sub(r"_", "", stripped)
        return {stripped, stripped.rstrip("s"), no_under, no_under.rstrip("s")}

    # ── Domain-match helpers ─────────────────────────────────────────────────

    def _domain_matches_table(self, prop_uri: str, table_forms: Set[str]) -> bool:
        """True if any rdfs:domain of this property overlaps with the table's class forms."""
        for d in self._domain_map.get(prop_uri, frozenset()):
            for form in table_forms:
                if len(form) < 4 or len(d) < 4:
                    continue
                # Prefix match in either direction catches plural/singular pairs
                if d.startswith(form) or form.startswith(d):
                    return True
        return False

    def _strip_table_suffix(self, local_lower: str, table_forms: Set[str]) -> str:
        """Remove a For<TableClass> qualifier suffix if present.

        e.g. "hasicustayforchartevent" with table_form "chartevent"
             → "hasicustay"
        """
        for form in sorted(table_forms, key=len, reverse=True):  # longest first
            suffix = f"for{form}"
            if local_lower.endswith(suffix) and len(local_lower) > len(suffix):
                return local_lower[: -len(suffix)]
        return local_lower

    # ── FK-aware score ───────────────────────────────────────────────────────

    def _fk_domain_score(
        self,
        col_name: str,
        prop_uri: str,
        table_forms: Set[str],
    ) -> Optional[Tuple[float, str]]:
        """Return (boosted_score, rationale) for FK columns with a domain match, or None.

        Conditions:
          1. FK base name (strip _id) appears in the property local name.
          2. Property's rdfs:domain matches the current table's class.
        If either condition fails, returns None and the caller uses the base score.
        """
        base_name = re.sub(r"_id$", "", col_name.lower())
        local = prop_uri.split("#")[-1].split("/")[-1].lower()

        if base_name not in local:
            return None

        if not self._domain_matches_table(prop_uri, table_forms):
            return None

        # Strip the table-qualifier suffix before re-scoring so that
        # hasICUStayForChartEvent competes on the same footing as hasICUStay.
        local_stripped = self._strip_table_suffix(local, table_forms)
        clean_source = re.sub(r"^(raw_|has)", "", col_name.lower())
        stripped_score = fuzz.token_set_ratio(clean_source, local_stripped) / 100.0
        boosted = min(1.0, stripped_score + self.settings.DOMAIN_MATCH_BOOST)

        matched_domain = next(
            (d for d in self._domain_map.get(prop_uri, frozenset())
             for f in table_forms if len(f) >= 4 and (d.startswith(f) or f.startswith(d))),
            "unknown",
        )
        rationale = (
            f"Lexical match for {col_name} [domain={matched_domain} matches table; "
            f"stripped '{local}' → '{local_stripped}'; "
            f"+{self.settings.DOMAIN_MATCH_BOOST} domain boost]"
        )
        return boosted, rationale

    # ── Base score ───────────────────────────────────────────────────────────

    def _score(self, source_name: str, uri) -> float:
        raw_local = str(uri).split("#")[-1].split("/")[-1].lower()
        # Strip semantic verb prefix from property local name so that
        # "hasAmountUom" → "amountuom" matches column "amountuom" at 100%
        # rather than being penalised for the leading "has".
        local_name = re.sub(r"^(?:has|is|of)(?=[a-z])", "", raw_local)
        clean_source = re.sub(r"^(raw_|has)", "", source_name.lower())
        return fuzz.token_set_ratio(clean_source, local_name) / 100.0

    # ── Strategy interface ───────────────────────────────────────────────────

    def evaluate_column(
        self, profile: ColumnProfile, table_profile: TableProfile
    ) -> List[MappingEvidence]:
        is_fk = profile.name.lower().endswith("_id") or any(
            fk.get("column") == profile.name for fk in table_profile.foreign_keys
        )
        table_forms = self._table_forms(table_profile.table_name) if is_fk else set()

        props = list(self.graph.subjects(RDF.type, OWL.DatatypeProperty)) + list(
            self.graph.subjects(RDF.type, OWL.ObjectProperty)
        )
        evidence = []
        for prop in props:
            prop_uri = str(prop)
            fk_result = (
                self._fk_domain_score(profile.name, prop_uri, table_forms)
                if is_fk and table_forms
                else None
            )
            if fk_result is not None:
                score, rationale = fk_result
            else:
                score = self._score(profile.name, prop)
                rationale = f"Lexical match for {profile.name}"
            evidence.append(MappingEvidence(
                strategy="NameMatcher", score=score, rationale=rationale, mapped_uri=prop_uri
            ))
        return evidence

    def evaluate_table(self, profile: TableProfile) -> List[MappingEvidence]:
        evidence = []
        for cls in self.graph.subjects(RDF.type, OWL.Class):
            score = self._score(profile.table_name, cls)
            evidence.append(MappingEvidence(
                strategy="NameMatcher",
                score=score,
                rationale="Table name match",
                mapped_uri=str(cls),
            ))
        return evidence
