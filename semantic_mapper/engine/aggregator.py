# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Evidence aggregator for the semantic mapping engine.

Combines per-strategy MappingEvidence into a single winning URI + combined score.

Supported aggregation strategies (controlled by Settings.AGGREGATION_STRATEGY):
  "weighted_sum"  — default; type-gated weighted average, no voting
  "max"           — legacy; takes the single highest individual score

weighted_sum algorithm
----------------------
1.  HistoryMatcher short-circuit: if HistoryMatcher returns any evidence (which
    only happens for HUMAN-confirmed corrections), that URI wins immediately at
    score 1.0.  No further computation needed.

1.5 AnnotationMatcher exact-match short-circuit: if any AnnotationMatcher evidence
    reaches ANNOTATION_EXACT_CONFIRM_THRESHOLD (default 0.95), that URI wins
    immediately.  This covers both true verbatim hits (score=1.0) and case-folded
    hits (score=ANNOTATION_NORMALISED_MATCH_SCORE, default 0.95).  The floor
    denominator in the weighted sum would otherwise drag both scores below the
    confirm threshold.  Fuzzy matches (≤0.75) do not reach the threshold.

1.7 NameMatcher exact-match short-circuit: if the top NameMatcher score for any
    URI reaches NAME_EXACT_CONFIRM_THRESHOLD (default 0.97), that URI wins
    immediately.  An exact concept-name match (property local name with "has/is/of"
    stripped == column name) is stronger evidence than any combination of weaker
    signals from TypeMatcher, SemanticMatcher, etc.

2.  Collect best score per (uri, strategy) from all non-HistoryMatcher evidence.

3.  Type gate: TypeMatcher scores ObjectProperties at exactly 0.0 for scalar
    columns, and DatatypeProperties at exactly 0.0 for FK columns.  Eliminate
    all URIs with TypeMatcher score == 0.0 before weighted scoring.

3.5 Domain-preference filter: when table_class_uri is known and at least one
    surviving candidate has rdfs:domain matching the table class, eliminate
    candidates with a mismatching or absent domain.  This forces the correct
    table-specific property to win over generic abstract properties (no domain)
    and cross-table properties (wrong domain).

4.  Weighted average with a floor denominator:

        floor = Σ(weight_s  for non-LLM strategies that produced ANY evidence
                              for this column)

        denominator(uri) = max( Σ(weight_s for strategies that scored THIS uri),
                                floor )

        score(uri) = Σ(score_s × weight_s for strategies that scored THIS uri)
                     ─────────────────────────────────────────────────────────
                     denominator(uri)

    The floor prevents TypeMatcher-only URIs from reaching the confirmation
    threshold on type alone.  TypeMatcher running on a VARCHAR column scores
    every compatible string property at ~0.80; without the floor a single
    TypeMatcher vote gives score = 0.80 (well above 0.70), blocking Tier 2
    and Tier 3 from running at all.

    LLMStrategy's weight (2.0) intentionally exceeds the typical floor (~0.80
    for a full Tier 1 run), so a confident LLM-only answer still reaches its
    natural score (e.g. 0.90 LLM alone → denominator = max(2.0, 0.80) = 2.0
    → score = 0.90).

5.  Return the URI with the highest score.
"""
import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

import rdflib

from semantic_mapper.config.settings import Settings
from semantic_mapper.schema.mapping_output import MappingEvidence

logger = logging.getLogger(__name__)


def aggregate(
    evidence_trail: List[MappingEvidence],
    settings: Settings,
    graph: Optional[rdflib.Graph] = None,
    table_class_uri: Optional[str] = None,
) -> Optional[Tuple[str, float, str]]:
    """
    Return (best_uri, combined_score, rationale) or None if evidence_trail is empty.

    Dispatches to the algorithm named in settings.AGGREGATION_STRATEGY.

    Args:
        graph:           Ontology graph, used to check rdfs:domain on short-circuit
                         candidates.  When None, domain checks are skipped (existing
                         behaviour preserved).
        table_class_uri: URI of the primary OWL class for the current table, used
                         alongside *graph* to detect domain mismatches.
    """
    if not evidence_trail:
        return None

    if settings.AGGREGATION_STRATEGY == "max":
        return _aggregate_max(evidence_trail)

    # Default: weighted_sum
    return _aggregate_weighted_sum(evidence_trail, settings, graph, table_class_uri)


# ---------------------------------------------------------------------------
# max — legacy, single highest individual score
# ---------------------------------------------------------------------------

def _aggregate_max(
    evidence_trail: List[MappingEvidence],
) -> Optional[Tuple[str, float, str]]:
    best = max(evidence_trail, key=lambda e: e.score)
    return (
        best.mapped_uri,
        best.score,
        f"[max] Best individual score: {best.strategy} → {best.score:.4f}",
    )


# ---------------------------------------------------------------------------
# Domain-mismatch helper
# ---------------------------------------------------------------------------

def _has_domain_mismatch(
    prop_uri: str,
    table_class_uri: Optional[str],
    graph: Optional[rdflib.Graph],
) -> bool:
    """Return True when *prop_uri* has an rdfs:domain that differs from *table_class_uri*.

    Returns False when:
    - graph or table_class_uri is None (no information to check — safe default)
    - the property has NO rdfs:domain (abstract property — no mismatch)
    - the property's domain matches table_class_uri (correct target class)
    """
    if graph is None or not table_class_uri or not prop_uri:
        return False
    from rdflib import RDFS, URIRef
    prop = URIRef(prop_uri)
    domains = list(graph.objects(prop, RDFS.domain))
    if not domains:
        return False  # Abstract property — no domain constraint, no mismatch
    return URIRef(table_class_uri) not in domains


def _domain_preference_rank(
    prop_uri: str,
    table_class_uri: Optional[str],
    graph: Optional[rdflib.Graph],
) -> int:
    """Return a sort key that prefers domain-scoped properties over abstract ones.

    2 — property domain matches the current table class  (best)
    1 — property has no domain (abstract — neutral fallback)
    0 — property domain is set but doesn't match         (worst; already filtered by mismatch check)
    """
    if graph is None or not table_class_uri or not prop_uri:
        return 1
    from rdflib import RDFS, URIRef
    prop = URIRef(prop_uri)
    domains = list(graph.objects(prop, RDFS.domain))
    if not domains:
        return 1  # abstract
    return 2 if URIRef(table_class_uri) in domains else 0


# ---------------------------------------------------------------------------
# weighted_sum
# ---------------------------------------------------------------------------

def _aggregate_weighted_sum(
    evidence_trail: List[MappingEvidence],
    settings: Settings,
    graph: Optional[rdflib.Graph] = None,
    table_class_uri: Optional[str] = None,
) -> Optional[Tuple[str, float, str]]:
    """
    See module docstring for full algorithm description.

    Settings knobs
    --------------
    NAME_MATCHER_WEIGHT       — weight for NameMatcher (default 0.35)
    TYPE_MATCHER_WEIGHT       — weight for TypeMatcher (default 0.25)
    SEMANTIC_MATCHER_WEIGHT   — weight for SemanticMatcher (default 0.45)
    LLM_MATCHER_WEIGHT        — weight for LLMStrategy (default 2.0)
    ANNOTATION_MATCHER_WEIGHT — weight for AnnotationMatcher (default 0.20)

    LLMStrategy carries a much higher weight because it is the last resort —
    only called when cheaper strategies failed to reach the threshold.  Its
    evidence should dominate the final score when present.
    """
    strategy_weights: Dict[str, float] = {
        "AnnotationMatcher": settings.ANNOTATION_MATCHER_WEIGHT,
        "NameMatcher":       settings.NAME_MATCHER_WEIGHT,
        "TypeMatcher":       settings.TYPE_MATCHER_WEIGHT,
        "SemanticMatcher":   settings.SEMANTIC_MATCHER_WEIGHT,
        "LLMStrategy":       settings.LLM_MATCHER_WEIGHT,
    }
    default_weight = 0.30

    # Step 1: HistoryMatcher short-circuit (HUMAN corrections only).
    # HistoryMatcher is configured to return nothing for AUTO corrections, so
    # any evidence it emits here represents an explicit human override.
    for ev in evidence_trail:
        if ev.strategy == "HistoryMatcher" and ev.mapped_uri:
            logger.debug(
                "Aggregator: human override → <%s> (score=%.2f, rationale=%s)",
                ev.mapped_uri.rsplit("#", 1)[-1],
                ev.score,
                ev.rationale,
            )
            return (
                ev.mapped_uri,
                1.0,
                f"[human-override] {ev.rationale}",
            )

    # Step 1.7: AnnotationMatcher exact-match short-circuit.
    # Both true verbatim matches (score=1.0) and case-folded matches
    # (score=ANNOTATION_NORMALISED_MATCH_SCORE, default 0.95) are authoritative:
    # the ontology author explicitly put the column name (or its camelCase form) in
    # the annotation.  The floor denominator in the weighted-sum would drag either
    # score below the confirm threshold, so we short-circuit before the weighted
    # average is computed.  Fuzzy matches (≤0.75) are intentionally excluded.
    #
    # Domain check: if the winning property's rdfs:domain does not match the current
    # table class URI, the short-circuit is suppressed.  This prevents a property
    # declared for one class (e.g. hasSubjectId, domain=Patient) from claiming an
    # FK column in a different class (e.g. subject_id in Admission).
    annotation_threshold = getattr(settings, "ANNOTATION_EXACT_CONFIRM_THRESHOLD", 0.95)
    if annotation_threshold > 0.0:
        # Walk candidates sorted by score descending; use the first that passes
        # the domain check.  When multiple properties share the same altLabel
        # (e.g. every table has an "hadm_id" FK property), the globally highest
        # scorer often belongs to a different class — skip those and find the one
        # whose rdfs:domain matches this table's class URI.
        ann_candidates = sorted(
            (ev for ev in evidence_trail if ev.strategy == "AnnotationMatcher" and ev.mapped_uri),
            key=lambda e: (
                e.score,
                _domain_preference_rank(e.mapped_uri, table_class_uri, graph),
            ),
            reverse=True,
        )
        for best_ann_ev in ann_candidates:
            if best_ann_ev.score < annotation_threshold:
                break
            ann_domain_mismatch = _has_domain_mismatch(
                best_ann_ev.mapped_uri, table_class_uri, graph
            )
            if ann_domain_mismatch:
                logger.debug(
                    "Aggregator: annotation-exact short-circuit SKIPPED — domain mismatch "
                    "for <%s> (table class URI=%s), trying next candidate",
                    best_ann_ev.mapped_uri.rsplit("#", 1)[-1],
                    table_class_uri.rsplit("#", 1)[-1] if table_class_uri else "None",
                )
                continue
            logger.debug(
                "Aggregator: annotation-exact short-circuit → <%s> "
                "(score=%.2f ≥ threshold=%.2f, rationale=%s)",
                best_ann_ev.mapped_uri.rsplit("#", 1)[-1],
                best_ann_ev.score,
                annotation_threshold,
                best_ann_ev.rationale,
            )
            return (
                best_ann_ev.mapped_uri,
                best_ann_ev.score,
                f"[annotation-exact] {best_ann_ev.rationale}",
            )

    # Step 1.5: NameMatcher exact-match short-circuit.
    # A NameMatcher score ≥ NAME_EXACT_CONFIRM_THRESHOLD means the property's
    # concept name (local name with "has/is/of" stripped) is identical or
    # near-identical to the column name.  That is stronger evidence than any
    # combination of weaker signals from other strategies, so the top-scoring
    # NameMatcher URI wins immediately at CONFIRMED status.
    # Disabled when NAME_EXACT_CONFIRM_THRESHOLD == 0.0.
    #
    # Same domain check as AnnotationMatcher: skip the short-circuit when the
    # winning property's rdfs:domain doesn't match the current table class URI.
    name_exact_threshold = getattr(settings, "NAME_EXACT_CONFIRM_THRESHOLD", 0.97)
    if name_exact_threshold > 0.0:
        best_name_ev = max(
            (ev for ev in evidence_trail if ev.strategy == "NameMatcher" and ev.mapped_uri),
            key=lambda e: (e.score, _domain_preference_rank(e.mapped_uri, table_class_uri, graph)),
            default=None,
        )
        if best_name_ev and best_name_ev.score >= name_exact_threshold:
            name_domain_mismatch = _has_domain_mismatch(
                best_name_ev.mapped_uri, table_class_uri, graph
            )
            if name_domain_mismatch:
                logger.debug(
                    "Aggregator: name-exact short-circuit SKIPPED — domain mismatch "
                    "for <%s> (table class URI=%s)",
                    best_name_ev.mapped_uri.rsplit("#", 1)[-1],
                    table_class_uri.rsplit("#", 1)[-1] if table_class_uri else "None",
                )
            else:
                logger.debug(
                    "Aggregator: name-exact short-circuit → <%s> (score=%.3f ≥ threshold=%.2f)",
                    best_name_ev.mapped_uri.rsplit("#", 1)[-1],
                    best_name_ev.score,
                    name_exact_threshold,
                )
                return (
                    best_name_ev.mapped_uri,
                    best_name_ev.score,
                    f"[name-exact] NameMatcher score={best_name_ev.score:.4f} ≥ "
                    f"threshold={name_exact_threshold}; exact lexical match overrides "
                    f"weighted sum. {best_name_ev.rationale}",
                )

    # Step 2: Best score per (uri, strategy), excluding HistoryMatcher.
    uri_strat_scores: Dict[str, Dict[str, float]] = defaultdict(dict)
    strategies_seen: set = set()
    for ev in evidence_trail:
        if not ev.mapped_uri or ev.strategy == "HistoryMatcher":
            continue
        strategies_seen.add(ev.strategy)
        existing = uri_strat_scores[ev.mapped_uri].get(ev.strategy, -1.0)
        if ev.score > existing:
            uri_strat_scores[ev.mapped_uri][ev.strategy] = ev.score

    if not uri_strat_scores:
        return None

    # Step 3: Type gate — eliminate structurally incompatible URIs.
    # TypeMatcher scores ObjectProperties at 0.0 for scalar columns, and
    # DatatypeProperties at 0.0 for FK columns.  Eliminating TM==0.0 URIs
    # enforces structural type compatibility without any special-casing in
    # the weighted sum.
    if "TypeMatcher" in strategies_seen:
        eliminated = {
            uri for uri, ss in uri_strat_scores.items()
            if ss.get("TypeMatcher", -1.0) == 0.0
        }
        candidates = {
            uri: ss for uri, ss in uri_strat_scores.items()
            if uri not in eliminated
        }
        if eliminated:
            logger.debug(
                "Aggregator: type gate eliminated %d URI(s): %s",
                len(eliminated),
                [u.rsplit("#", 1)[-1] for u in eliminated],
            )
        if not candidates:  # pragma: no cover
            logger.warning(
                "Aggregator: type gate eliminated ALL candidates; falling back "
                "to full candidate set"
            )
            candidates = uri_strat_scores
    else:
        candidates = uri_strat_scores

    # Step 3.5: Domain-preference filter.
    # When the table's OWL class is known and at least one candidate has
    # rdfs:domain matching that class, drop candidates with a mismatching or
    # absent domain.  Prevents abstract properties (no rdfs:domain) and
    # cross-table properties (wrong rdfs:domain) from outscoring the correct
    # table-specific property in the weighted sum.
    if table_class_uri and graph and candidates:
        from rdflib import RDFS as _RDFS, URIRef as _URIRef
        _cls = _URIRef(table_class_uri)
        domain_matched = {
            uri for uri in candidates
            if _cls in list(graph.objects(_URIRef(uri), _RDFS.domain))
        }
        if domain_matched:
            dropped = set(candidates) - domain_matched
            if dropped:
                logger.debug(
                    "Aggregator: domain filter kept %d candidate(s) (domain=%s); "
                    "dropped %d (no-domain or mismatch): %s",
                    len(domain_matched),
                    table_class_uri.rsplit("#", 1)[-1],
                    len(dropped),
                    [u.rsplit("#", 1)[-1] for u in dropped],
                )
            candidates = {
                uri: ss for uri, ss in candidates.items() if uri in domain_matched
            }

    # Step 4: Weighted average with floor denominator.
    #
    # floor = sum of weights of all non-LLM strategies that ran for this column.
    # denominator(uri) = max(sum of weights of strategies that scored THIS uri, floor)
    #
    # The floor prevents TypeMatcher-only URIs from reaching the confirmation
    # threshold on type compatibility alone.  LLMStrategy's weight (2.0) exceeds
    # the typical Tier-1 floor (~0.80), so a confident LLM-only result still
    # reaches its natural score.
    floor_weight = sum(
        strategy_weights.get(s, default_weight)
        for s in strategies_seen
        if s != "LLMStrategy"
    )

    best_uri: Optional[str] = None
    best_score = -1.0
    best_rationale = ""

    for uri, strat_scores in candidates.items():
        uri_weight = max(
            sum(strategy_weights.get(strat, default_weight) for strat in strat_scores),
            floor_weight,
        )
        if uri_weight == 0.0:
            continue
        weighted_sum = sum(
            score * strategy_weights.get(strat, default_weight)
            for strat, score in strat_scores.items()
        )
        avg = weighted_sum / uri_weight

        if avg > best_score:
            best_score = avg
            best_uri = uri
            contrib = {
                strat: f"{score:.3f}×{strategy_weights.get(strat, default_weight)}"
                for strat, score in sorted(strat_scores.items())
            }
            best_rationale = (
                f"[weighted_sum] "
                f"Σ(score×w)={weighted_sum:.4f} / denom={uri_weight:.3f} "
                f"(floor={floor_weight:.3f}) = {avg:.4f} | {contrib}"
            )

    if best_uri is None:
        return None

    return best_uri, min(1.0, best_score), best_rationale
