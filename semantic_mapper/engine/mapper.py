# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
import logging
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import rdflib
from semantic_mapper.config.settings import Settings
from semantic_mapper.db.corrections_store import CorrectionsStore
from semantic_mapper.db.models import TableProfile, ColumnProfile
from semantic_mapper.schema.mapping_output import (
    SemanticMappingReport, TableMapping, PropertyMapping, MappingStatus, ClassCandidate,
    compute_coverage,
    TransformationSpec,
)
from semantic_mapper.engine.strategies.annotation_matcher import AnnotationMatcher
from semantic_mapper.engine.strategies.history_matcher import HistoryMatcher
from semantic_mapper.engine.strategies.name_matcher import NameMatcher
from semantic_mapper.engine.strategies.type_matcher import TypeMatcher
from semantic_mapper.engine.strategies.semantic_matcher import SemanticMatcher
from semantic_mapper.engine.strategies.llm_strategy import LLMStrategy
from semantic_mapper.engine.aggregator import aggregate

logger = logging.getLogger(__name__)


def canonicalise_value_mappings(source_column, value_mappings, registry):
    """Reconcile a column's value_mappings against a cross-table canonical registry so
    one source value resolves to ONE URI everywhere it appears.

    First-declaration-wins, keyed by (normalised source column, normalised value): the
    first table to map a value fixes its URI; every later table reusing that value is
    rewritten to the same URI. This is the categorical analogue of deterministic class
    URIs — it kills the per-table fragmentation (MaleGender vs MedallistGenderMale) and
    the same-code-different-URI split (DisciplineCodeALP vs DisciplineCodeAlpineSkiing).
    Mutates ``registry`` ({field: {norm_value: uri}}); returns reconciled rules.
    """
    field = str(source_column).strip().lower()
    field_map = registry.setdefault(field, {})
    out = []
    for rule in value_mappings:
        nv = str(rule.source_value).strip().lower()
        canon = field_map.get(nv)
        if canon is None:
            field_map[nv] = rule.mapped_uri            # first declaration wins
            out.append(rule)
        elif canon != rule.mapped_uri:
            out.append(rule.model_copy(update={"mapped_uri": canon}))  # reuse canonical
            logger.info(
                "Canonical value registry: %s=%r → %s (reused; was %s)",
                field, rule.source_value, canon.rsplit("#", 1)[-1],
                rule.mapped_uri.rsplit("#", 1)[-1],
            )
        else:
            out.append(rule)
    return out


def maps_to_vocabulary_property(mapped_property_uri, ontology_graph) -> bool:
    """True only when a column's mapped property is the generator's value-mapped VOCABULARY
    link — an ``owl:ObjectProperty`` whose range is a class that declares its members as
    individuals in the ontology (a closed vocabulary like Gender or Discipline).

    Value-mapping must agree with how the generator modelled the column.  A column the
    generator left as a plain ``owl:DatatypeProperty`` (a string label — e.g. a venue's own
    name) or pointed at an ENTITY class (whose instances live only in the data graph, never
    declared as individuals here) must NOT be value-mapped: doing so mints phantom
    individuals the ontology never declares, which no query can resolve.  Dataset-agnostic —
    keyed on the property's type and whether its range is a declared vocabulary.
    """
    from rdflib import OWL, RDF, RDFS, URIRef

    if not mapped_property_uri:
        return False
    prop = URIRef(str(mapped_property_uri))
    if (prop, RDF.type, OWL.ObjectProperty) not in ontology_graph:
        return False
    for rng in ontology_graph.objects(prop, RDFS.range):
        if (rng, RDF.type, OWL.Class) in ontology_graph and \
                any(True for _ in ontology_graph.subjects(RDF.type, rng)):
            return True  # range class has declared individuals → a vocabulary
    return False


def align_value_mappings_to_ontology(mapping, value_mappings, ontology_graph):
    """Snap each value-map target onto the ontology's DECLARED individual for that value, so
    the materialised graph references the generator's deterministic vocabulary individuals
    (``MGender`` for ``M``) rather than an LLM-invented synonym (``MaleGender``).

    The generator mints exactly one ``{Value}{Class}`` individual per distinct source value,
    labelled with the raw value.  Here we match a value-mapping rule to the individual of the
    mapped property's range class whose ``rdfs:label`` equals the rule's source value — a
    deterministic, dataset-agnostic reconciliation.  This makes "OWL individuals == value-map
    targets" hold end to end: the data references the declared individuals, the ontology
    enricher finds nothing new to add, and the generated OWL stays clean.

    Rules whose value has no declared individual (e.g. a value the generator dropped as
    null-like) are left unchanged.  No-op when the property has no vocabulary-class range.
    """
    from rdflib import OWL, RDF, RDFS, URIRef

    prop = getattr(mapping, "mapped_property_uri", None)
    if not prop or not value_mappings:
        return value_mappings
    prop_ref = URIRef(str(prop))
    range_classes = [
        r for r in ontology_graph.objects(prop_ref, RDFS.range)
        if (r, RDF.type, OWL.Class) in ontology_graph
    ]
    if not range_classes:
        return value_mappings
    # rdfs:label (normalised) → declared individual URI, scoped to the property's range class.
    label_to_ind: dict = {}
    for cls in range_classes:
        for ind in ontology_graph.subjects(RDF.type, cls):
            for lbl in ontology_graph.objects(ind, RDFS.label):
                label_to_ind.setdefault(str(lbl).strip().lower(), str(ind))
    if not label_to_ind:
        return value_mappings
    out = []
    for rule in value_mappings:
        target = label_to_ind.get(str(rule.source_value).strip().lower())
        if target and target != rule.mapped_uri:
            out.append(rule.model_copy(update={"mapped_uri": target}))
            logger.info(
                "Ontology-aligned value map: %s=%r → %s (was %s)",
                str(mapping.source_column), rule.source_value,
                target.rsplit("#", 1)[-1], str(rule.mapped_uri).rsplit("#", 1)[-1],
            )
        else:
            out.append(rule)
    return out


def looks_like_packed_cell(col_profile) -> bool:
    """True when a column's values are packed/nested structures (JSON arrays or objects, or
    Python-repr lists/dicts), e.g. an ``events`` cell ``[{'discipline': ..., 'event': ...}]``.

    Such a column is unpacked into first-class entities by the RDF layer; it must NOT be
    value-mapped into per-cell categorical individuals — that mints nonsensical members from
    whole packed strings (``IceHockeyMenEvent`` from a list of events) and the gen-LLM does it
    non-deterministically.  Structural, not dataset-specific: keyed on the cell shape.
    """
    vals = [str(v.get("value", "")).strip() if isinstance(v, dict) else str(v).strip()
            for v in (getattr(col_profile, "top_values", None) or [])]
    vals = [v for v in vals if v]
    if not vals:
        return False
    packed = sum(1 for v in vals if (v[:1] in ("[", "{")))
    return packed / len(vals) >= 0.5


def is_entity_reference_column(col_name, foreign_keys) -> bool:
    """True when a column references an entity and so must NOT be discriminated into
    per-value individuals — the discriminate-vs-FK fork resolves to the entity link.

    Two shapes count as an entity reference:

    - the column IS a foreign key (declared or inferred), or
    - it is the descriptive shadow of a coded FK — a ``venue`` name beside a
      ``venue_code`` FK to a Venue table.  The human-readable form of a reference is a
      label on the linked entity, not its own controlled vocabulary.

    ``foreign_keys`` is a ``TableProfile.foreign_keys`` list (dicts keyed by ``column``
    or ``constrained_column``).  Generic and structural — no dataset names.
    """
    fk_cols = {
        (fk.get("column") or fk.get("constrained_column") or "").lower()
        for fk in (foreign_keys or [])
    }
    c = str(col_name).lower()
    return c in fk_cols or f"{c}_code" in fk_cols or f"{c}_id" in fk_cols


class MapperOrchestrator:
    """
    Three-tier mapping orchestrator with early exit.

    Tier 1 — cheap, local (HistoryMatcher, AnnotationMatcher, NameMatcher, TypeMatcher):
        Runs on every column.  Fast lookups — no embedding model, no API calls.
        Columns that clear AUTO_CONFIRM_THRESHOLD are CONFIRMED and skip Tier 2 + 3.

    Tier 2 — moderate, local (SemanticMatcher):
        Runs only on columns still PENDING after Tier 1.  Uses a local embedding
        model (sentence-transformers) — compute-heavy but no API cost.
        Columns that clear the threshold skip Tier 3.

    Tier 3 — expensive, remote (LLMStrategy):
        Runs only on columns still PENDING after Tier 2.  Batched per table to
        minimise API calls.  LLMStrategy carries a much higher weight in the
        aggregator because it is called only as a last resort and its evidence
        should dominate the final score.

    Telemetry (available after run()):
        llm_api_calls         — total OpenAI API calls made
        llm_prompt_tokens     — total prompt tokens consumed
        llm_completion_tokens — total completion tokens consumed
        llm_hallucinated_uris — URIs returned by LLM but not in the ontology
    """

    def __init__(
        self,
        profiles: List[TableProfile],
        settings: Settings,
        graph: rdflib.Graph,
        ontology_source: str = "",
        db_source: str = "",
    ) -> None:
        self.profiles = profiles
        self.settings = settings
        self.graph = graph
        self.ontology_source = ontology_source or settings.ONTOLOGY_PATH
        self.db_source = db_source

        # Corrections store — load prior confirmed patterns for HistoryMatcher.
        self.corrections_store = CorrectionsStore(
            Path(settings.CORRECTIONS_DB_PATH)
        )
        pattern_lookup = self.corrections_store.load_patterns(
            self.ontology_source, self.db_source
        )
        self.corrections_loaded: int = len(pattern_lookup)

        self.tier1_strategies = [
            HistoryMatcher(graph, settings, pattern_lookup),
            AnnotationMatcher(graph, settings),
            NameMatcher(graph, settings),
            TypeMatcher(graph, settings),
        ]
        self.tier2_strategies = [
            SemanticMatcher(graph, settings),
        ]

        self.llm_strategy: Optional[LLMStrategy] = None
        if settings.LLM_ENABLED and settings.OPENAI_API_KEY:
            logger.info("LLMStrategy: Tier 3 enabled")
            self.llm_strategy = LLMStrategy(graph, settings)
        else:
            logger.info(
                "LLMStrategy: Tier 3 disabled "
                "(LLM_ENABLED=False or OPENAI_API_KEY not set)"
            )

        # Telemetry — populated after run()
        self.llm_api_calls: int = 0
        self.llm_prompt_tokens: int = 0
        self.llm_completion_tokens: int = 0
        self.llm_hallucinated_uris: List[str] = []
        self.corrections_written: int = 0

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_to_specific_property(
        self, uri: str, table_class_uri: str, col_name: str = ""
    ) -> str:
        """Resolve *uri* to the most domain-appropriate class-scoped property.

        Three resolution modes, applied in order:

        **Domain match** — property already has domain=table_class_uri → return as-is.

        **Domain mismatch** — property has a domain that differs from table_class_uri
        (e.g. hasSubjectId has domain=Patient but we're mapping subject_id in Admissions).
        Searches for a class-scoped alternative with domain=table_class_uri:
        1. Prefer ObjectProperties whose range equals the mismatched domain class and
           whose concept overlaps the column name (exact cross-type resolution).
        2. Fall back to any property with domain=table_class_uri and concept overlap.

        **No domain (abstract)** — property has no rdfs:domain; searches subproperties
        and domain-matched properties via Phase 1 (subPropertyOf walk) and Phase 2
        (compound-name broad containment fallback).
        """
        if not uri or not table_class_uri:
            return uri
        import re as _re
        from rdflib import OWL as _OWL, RDF as _RDF, RDFS, URIRef
        prop = URIRef(uri)
        cls_ref = URIRef(table_class_uri)
        col_norm = _re.sub(r"[_\s]", "", col_name.lower())

        # Extended suffix stripping — longest first to prevent partial stripping.
        _STRIP_SUFFIXES = ("description", "status", "name", "code", "type", "uom", "id")
        col_base = col_norm
        for sfx in _STRIP_SUFFIXES:
            if col_norm.endswith(sfx) and len(col_norm) > len(sfx) + 2:
                col_base = col_norm[: -len(sfx)]
                break

        def _concept(sub_uri: str) -> str:
            local = sub_uri.split("#")[-1].split("/")[-1].lower()
            return _re.sub(r"^(?:has|is|of)(?=[a-z])", "", local)

        domains = list(self.graph.objects(prop, RDFS.domain))

        # --- Domain match: already specific to the right class ---
        if domains and cls_ref in domains:
            return uri

        # --- Domain mismatch: property belongs to a different class ---
        if domains:
            wrong_domain = domains[0]  # The class this property is scoped to

            def _cross_overlap(concept: str) -> bool:
                return (
                    not col_norm
                    or col_norm in concept
                    or concept in col_norm
                    or (col_base != col_norm and col_base in concept)
                )

            # Priority 1: ObjectProperty with domain=table_class_uri and range=wrong_domain.
            # This handles FK cases: hasSubjectId (domain=Patient, DatatypeProperty) →
            # hasAdmissionSubject (domain=Admission, range=Patient, ObjectProperty).
            exact_range = [
                str(p) for p in self.graph.subjects(RDFS.domain, cls_ref)
                if isinstance(p, URIRef)
                and str(p) != uri
                and wrong_domain in list(self.graph.objects(p, RDFS.range))
                and _cross_overlap(_concept(str(p)))
            ]
            if len(exact_range) == 1:
                return exact_range[0]

            # Priority 2: any property with domain=table_class_uri and concept overlap.
            any_match = [
                str(p) for p in self.graph.subjects(RDFS.domain, cls_ref)
                if isinstance(p, URIRef)
                and str(p) != uri
                and _cross_overlap(_concept(str(p)))
            ]
            if len(any_match) == 1:
                return any_match[0]

            return uri

        # --- No domain: abstract property ---

        def _name_overlap(concept: str) -> bool:
            return (
                not col_norm
                or concept in col_norm
                or col_norm in concept
                or (col_base != col_norm and col_base in concept)
            )

        # Phase 1: subproperty walk.
        candidates = [
            str(sub)
            for sub in self.graph.subjects(RDFS.subPropertyOf, prop)
            if cls_ref in list(self.graph.objects(sub, RDFS.domain))
            and _name_overlap(_concept(str(sub)))
        ]
        if len(candidates) == 1:
            return candidates[0]

        # Phase 2: broad containment fallback for compound column names.
        # Only runs when a suffix was stripped AND the column name's suffix
        # aligns with the abstract property's concept (semantic sanity check).
        if col_base and col_base != col_norm:
            abstract_concept = _concept(uri)
            if abstract_concept and (
                col_norm.endswith(abstract_concept)
                or col_norm.startswith(abstract_concept)
            ):
                abstract_is_datatype = (prop, _RDF.type, _OWL.DatatypeProperty) in self.graph
                prop_type = _OWL.DatatypeProperty if abstract_is_datatype else _OWL.ObjectProperty
                broad = [
                    str(p)
                    for p in self.graph.subjects(RDFS.domain, cls_ref)
                    if isinstance(p, URIRef)
                    and p != prop
                    and (p, _RDF.type, prop_type) in self.graph
                    and col_base in _concept(str(p))
                ]
                if len(broad) == 1:
                    return broad[0]

        return uri

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self) -> SemanticMappingReport:
        table_mappings = []
        # Cross-table canonical registry for categorical values (first-declaration-wins),
        # so a value maps to ONE URI everywhere — {field: {norm_value: uri}}.
        canonical_registry: dict = {}

        for table in self.profiles:
            # ---- Table-level class mapping (used to resolve abstract props) ----
            table_evidence = []
            for strat in self.tier1_strategies:
                table_evidence.extend(strat.evaluate_table(table))
            for strat in self.tier2_strategies:
                table_evidence.extend(strat.evaluate_table(table))
            table_class_uri: str = ""
            if table_evidence:
                best_ev = max(table_evidence, key=lambda e: e.score)
                if best_ev.score > 0.5:
                    table_class_uri = best_ev.mapped_uri

            # ---- Tier 1: cheap local strategies (no embedding, no API) ----
            col_trails: Dict[str, list] = {}
            non_pk_cols = [
                (col_name, col_profile)
                for col_name, col_profile in table.columns.items()
                if not col_profile.is_primary_key
            ]

            for col_name, col_profile in non_pk_cols:
                trail = []
                for strat in self.tier1_strategies:
                    trail.extend(strat.evaluate_column(col_profile, table))
                col_trails[col_name] = trail

            # ---- Tier 2: SemanticMatcher — only for Tier 1 PENDING columns ----
            pending_after_t1 = {
                col_name
                for col_name, _ in non_pk_cols
                if (aggregate(col_trails[col_name], self.settings, self.graph, table_class_uri) or (None, 0.0))[1]
                < self.settings.AUTO_CONFIRM_THRESHOLD
            }
            if pending_after_t1:
                logger.debug(
                    "Tier 2: running SemanticMatcher on %d/%d column(s) for table '%s'",
                    len(pending_after_t1), len(non_pk_cols), table.table_name,
                )
            for col_name, col_profile in non_pk_cols:
                if col_name not in pending_after_t1:
                    continue
                for strat in self.tier2_strategies:
                    col_trails[col_name].extend(
                        strat.evaluate_column(col_profile, table)
                    )

            # ---- Tier 3: LLM — only for Tier 1+2 PENDING columns ----
            if self.llm_strategy is not None:
                pending_cols = []
                for col_name, col_profile in non_pk_cols:
                    result = aggregate(col_trails[col_name], self.settings, self.graph, table_class_uri)
                    score = result[1] if result else 0.0
                    if score < self.settings.AUTO_CONFIRM_THRESHOLD:
                        best_uri = result[0] if result else None
                        pending_cols.append(
                            (col_name, col_profile, best_uri, score,
                             col_trails[col_name])
                        )

                if pending_cols:
                    logger.debug(
                        "Tier 3: LLM called on %d/%d column(s) for table '%s'",
                        len(pending_cols), len(non_pk_cols), table.table_name,
                    )
                    llm_evidence = self.llm_strategy.evaluate_pending_batch(
                        pending_cols, table
                    )
                    for col_name, new_ev in llm_evidence.items():
                        col_trails[col_name].extend(new_ev)

            # ---- Final aggregate + build PropertyMappings ----
            col_mappings = []
            for col_name, col_profile in table.columns.items():
                if col_profile.is_primary_key:
                    continue  # Surrogate PKs skipped — no ontology property for row identity
                trail = col_trails[col_name]
                result = aggregate(trail, self.settings, self.graph, table_class_uri)
                best_uri, combined_score, rationale = (
                    result if result else (None, 0.0, "No match found")
                )
                if best_uri:
                    resolved = self._resolve_to_specific_property(best_uri, table_class_uri, col_name)
                    if resolved != best_uri:
                        abstract_local = best_uri.split('#')[-1] if '#' in best_uri else best_uri.split('/')[-1]
                        resolved_local = resolved.split('#')[-1] if '#' in resolved else resolved.split('/')[-1]
                        class_local = table_class_uri.split('#')[-1] if '#' in table_class_uri else table_class_uri.split('/')[-1]
                        logger.debug(
                            "Abstract prop resolved: %s → %s (table class: %s)",
                            best_uri, resolved, table_class_uri,
                        )
                        rationale += (
                            f"\nAbstract property {abstract_local} has no rdfs:domain — "
                            f"substituted with domain-specific subproperty {resolved_local}, "
                            f"whose rdfs:domain matches the table class {class_local}."
                        )
                        best_uri = resolved
                transformation_spec = None
                if col_profile.detected_formats:
                    transformation_spec = TransformationSpec(
                        detected_formats=col_profile.detected_formats,
                        target_format="xsd:date",
                        parse_strategy="dateutil.parser",
                        edge_cases=["NULL values handled by NullHandler"],
                        validation_rule="must be parseable as a date",
                    )

                col_mappings.append(PropertyMapping(
                    source_column=col_name,
                    source_data_type=col_profile.inferred_type,
                    is_nullable=col_profile.is_nullable,
                    mapped_property_uri=best_uri,
                    confidence_score=combined_score,
                    status=(
                        MappingStatus.CONFIRMED
                        if combined_score >= self.settings.AUTO_CONFIRM_THRESHOLD
                        else MappingStatus.PENDING_REVIEW
                    ),
                    rationale=rationale,
                    transformation_spec=transformation_spec,
                    evidence_trail=trail,
                ))

            # ---- Value mappings for low-cardinality columns (LLM) ----
            if self.llm_strategy is not None:
                # An entity-reference column must NOT be discriminated into per-value
                # individuals — the discriminate-vs-FK fork resolves to the entity link
                # (see is_entity_reference_column).  A packed/nested cell must NOT be
                # value-mapped either — the RDF layer unpacks it into entities
                # (see looks_like_packed_cell).
                low_card_cols = [
                    (m.source_column, table.columns[m.source_column])
                    for m in col_mappings
                    if (
                        m.status == MappingStatus.CONFIRMED
                        and not table.columns[m.source_column].is_primary_key
                        and table.columns[m.source_column].top_values
                        and not is_entity_reference_column(
                            m.source_column, table.foreign_keys
                        )
                        and not looks_like_packed_cell(table.columns[m.source_column])
                        # Only value-map columns the generator modelled as a vocabulary
                        # (ObjectProperty → a class with declared individuals).  A column
                        # left as a string literal (e.g. a venue's own name) must not be
                        # discriminated into phantom individuals the ontology never declares.
                        and maps_to_vocabulary_property(
                            m.mapped_property_uri, self.graph
                        )
                    )
                ]
                if low_card_cols:
                    # Check corrections store for cached value_mappings first;
                    # only call the LLM for columns that have no cached entry.
                    cached_rules: dict = {}
                    uncached_cols = []
                    for col_name, col_profile in low_card_cols:
                        stored = self.corrections_store.load_value_mappings(
                            table.table_name, col_name,
                            self.ontology_source, self.db_source,
                        )
                        if stored is not None:
                            cached_rules[col_name] = stored
                            logger.debug(
                                "ValueMappings: cache hit for %s.%s (%d rules)",
                                table.table_name, col_name, len(stored),
                            )
                        else:
                            uncached_cols.append((col_name, col_profile))

                    llm_rules: dict = {}
                    if uncached_cols:
                        llm_rules = self.llm_strategy.generate_value_mappings(
                            uncached_cols, table
                        )
                        # Persist newly generated rules so future runs skip the LLM
                        for col_name, rules in llm_rules.items():
                            self.corrections_store.write_value_mappings(
                                table.table_name, col_name, rules,
                                self.ontology_source, self.db_source,
                            )

                    all_rules = {**cached_rules, **llm_rules}
                    for mapping in col_mappings:
                        rules = all_rules.get(mapping.source_column)
                        if rules:
                            # Snap each target onto the generator's deterministic vocabulary
                            # individual for that value (M → MGender) BEFORE canonicalising,
                            # so the cross-table registry and the materialised graph both use
                            # the OWL-declared individuals rather than an LLM-invented synonym.
                            aligned = align_value_mappings_to_ontology(
                                mapping, rules, self.graph
                            )
                            mapping.value_mappings = canonicalise_value_mappings(
                                mapping.source_column, aligned, canonical_registry
                            )

            table_mappings.append(TableMapping(
                source_table=table.table_name,
                class_candidate_set=[
                    ClassCandidate(
                        uri=e.mapped_uri,
                        label="Label",
                        rationale=e.rationale,
                        confidence_score=e.score,
                    )
                    for e in table_evidence
                    if e.score > 0.5
                ],
                column_mappings=col_mappings,
                # Carry the inspector-detected PK forward as the canonical row key.
                primary_key_columns=[
                    name for name, col in table.columns.items() if col.is_primary_key
                ],
            ))

        # ---- Capture LLM telemetry ----
        if self.llm_strategy:
            self.llm_api_calls = self.llm_strategy.api_calls
            self.llm_prompt_tokens = self.llm_strategy.prompt_tokens
            self.llm_completion_tokens = self.llm_strategy.completion_tokens
            self.llm_hallucinated_uris = self.llm_strategy.hallucinated_uris

        report = SemanticMappingReport(
            ontology_uri=self.settings.ONTOLOGY_PATH,
            database_name="MySQL_Bronze",
            settings_used=self.settings.model_dump(),
            tables=table_mappings,
            total_pending_review=sum(
                1
                for t in table_mappings
                for c in t.column_mappings
                if c.status == MappingStatus.PENDING_REVIEW
            ),
            coverage=compute_coverage(table_mappings),
        )

        # Persist confirmed mappings so the next run can skip OWL scanning
        # and LLM calls for already-confirmed columns.
        self.corrections_written = self.corrections_store.write_confirmed(
            report, self.ontology_source, self.db_source
        )

        return report
