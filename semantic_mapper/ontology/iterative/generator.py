# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
IterativeOntologyGenerator — RIGOR-inspired table-by-table OWL generator.

Processes tables in FK topological order.  For each table:
  1. Build deterministic class URI + table context.
  2. Retrieve top-k already-declared classes from the embedding index.
  3. Call Gen-LLM (gpt-4o) → Manchester Syntax.
  4. Parse Manchester Syntax into rdflib triples.
  5. Call Judge-LLM (gpt-4o-mini) → JSON verdict.
  6. If rejected and retries remain, rebuild prompt with revision instructions → goto 3.
  7. Merge validated fragment into accumulating graph.
  8. Update embedding index with new classes.

After all tables:
  - Apply _qualify_obj_props safety pass.
  - Write OWL file with sequential naming (…_rigor_YYMMDD_NNN.owl).

Public API matches FullOntologyGenerator:
  generate(profiles) → Path
  prompt_tokens: int
  completion_tokens: int
"""
from __future__ import annotations

import json
import logging
import os
import re as _re
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Set

import rdflib
from rdflib import OWL, RDF, RDFS, Graph, Literal, Namespace, URIRef
from rdflib.namespace import SKOS, XSD

from semantic_mapper.config.settings import Settings
from semantic_mapper.db.models import TableProfile
from semantic_mapper.ontology.naming import ontology_filename
from semantic_mapper.ontology.iterative.context_builder import (
    build_table_context,
    derive_class_local_name,
    derive_class_uri,
    compute_class_name_overrides,
    set_class_name_overrides,
    DEFAULT_MAX_NULL_RATE_FOR_CLASS,
    DEFAULT_MIN_SUPPORT_FOR_CLASS,
    DEFAULT_MAX_UNIQUENESS_FOR_CLASS,
)
from semantic_mapper.ontology.iterative.embedding_index import AccumulatedClassIndex
from semantic_mapper.ontology.iterative.categorical_individuals import (
    materialise_categorical_individuals,
)
from semantic_mapper.ontology.iterative.gen_prompt import (
    build_gen_prompt,
    rebuild_with_revision,
)
from semantic_mapper.ontology.iterative.graph_merger import (
    check_duplicate_property_types,
    merge,
    remove_legacy_provenance_comments,
    resolve_mixed_ranges_in_graph,
    synthesise_missing_provenance,
    _scan_and_strip_self_ref_subprops,
    _strip_domain_from_abstract_parents,
    _strip_dual_annotated_object_properties,
)
from semantic_mapper.ontology.iterative.judge_prompt import build_judge_prompt
from semantic_mapper.ontology.iterative.manchester_parser import parse_manchester
from semantic_mapper.ontology.iterative.property_namer import (
    canonical_local,
    normalize_datatype_property_uris,
)
from semantic_mapper.ontology.iterative.models import (
    AccumulatedAbstractPropertySummary,
    JudgeVerdict,
    OntologyFragment,
)
from semantic_mapper.ontology.iterative.topology import fk_topological_sort

logger = logging.getLogger(__name__)

_FM_NS = Namespace("http://forgemesh.ai/ontology#")


class IterativeOntologyGenerator:
    """
    Iterative, table-by-table OWL generator.

    Exposes the same generate() interface as FullOntologyGenerator.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(self, profiles: List[TableProfile]) -> Path:
        """Generate a full OWL file iteratively from table profiles.

        Returns path to the written .owl file.
        """
        base_iri = self._derive_base_iri(profiles)
        # Name entity classes after the entity their PK denotes where the table name
        # is misleading (e.g. a per-country medal-tally table `medals` keyed by
        # country_code → class `Country`). Set before any class URI is derived so the
        # whole run — prompts, FK ranges, merges — uses the corrected name.
        _overrides = compute_class_name_overrides(profiles)
        set_class_name_overrides(_overrides)
        if _overrides:
            logger.info("Class-name overrides (PK denotes entity): %s", _overrides)
        max_retries = getattr(self.settings, "ONTOLOGY_GEN_MAX_RETRIES", 2)
        top_k = getattr(self.settings, "ONTOLOGY_GEN_TOP_K_CLASSES", 10)
        default_model = getattr(self.settings, "LLM_MODEL", None) or self.settings.OPENAI_MODEL
        gen_model = getattr(self.settings, "ONTOLOGY_GEN_MODEL", None) or default_model
        judge_model = getattr(self.settings, "ONTOLOGY_JUDGE_MODEL", None) or default_model
        gen_temp = getattr(self.settings, "ONTOLOGY_GEN_TEMPERATURE", 0.1)
        judge_temp = getattr(self.settings, "ONTOLOGY_GEN_JUDGE_TEMPERATURE", 0.0)
        judge_confidence_floor = getattr(self.settings, "ONTOLOGY_JUDGE_CONFIDENCE_FLOOR", 0.6)
        max_rows_for_enum = getattr(self.settings, "ONTOLOGY_MAX_ROWS_FOR_ENUMERATION", 1000)
        max_distinct_for_enum = getattr(self.settings, "ONTOLOGY_MAX_DISTINCT_FOR_ENUMERATION", 20)
        max_distinct_for_open = getattr(self.settings, "ONTOLOGY_MAX_DISTINCT_FOR_OPEN_CLASS", 100)
        min_rows_threshold = getattr(self.settings, "ONTOLOGY_MIN_ROWS_FOR_GENERATION", 0)
        # Proportional class-vs-literal thresholds (null-rate / support / uniqueness):
        # keep a low-distinct text column a class only if its values recur enough.
        cat_max_null = getattr(self.settings, "ONTOLOGY_CATEGORICAL_MAX_NULL_RATE",
                               DEFAULT_MAX_NULL_RATE_FOR_CLASS)
        cat_min_support = getattr(self.settings, "ONTOLOGY_CATEGORICAL_MIN_SUPPORT",
                                  DEFAULT_MIN_SUPPORT_FOR_CLASS)
        cat_max_uniqueness = getattr(self.settings, "ONTOLOGY_CATEGORICAL_MAX_UNIQUENESS",
                                     DEFAULT_MAX_UNIQUENESS_FOR_CLASS)

        # Identify and log skipped tables (empty or below row-count threshold)
        skip_tables: set = {
            p.table_name for p in profiles if p.row_count <= min_rows_threshold
        }
        for table_name in sorted(skip_tables):
            row_count = next(p.row_count for p in profiles if p.table_name == table_name)
            logger.warning(
                "Skipping table '%s' — row_count=%d is at or below "
                "ONTOLOGY_MIN_ROWS_FOR_GENERATION threshold. "
                "No ontology structure will be generated.",
                table_name, row_count,
            )

        # Sort tables: dimension/reference tables first, fact tables last.
        # skip_tables are excluded so downstream FK deps on them don't block the sort.
        ordered = fk_topological_sort(profiles, skip_tables=skip_tables)
        logger.info(
            "IterativeOntologyGenerator: processing %d tables in FK topo order (%d skipped)",
            len(ordered), len(skip_tables),
        )

        accumulating_graph = Graph()
        accumulating_graph.bind("", Namespace(base_iri))
        accumulating_graph.bind("owl", OWL)
        accumulating_graph.bind("rdfs", RDFS)
        accumulating_graph.bind("skos", SKOS)
        accumulating_graph.bind("xsd", XSD)
        accumulating_graph.bind("fm", rdflib.Namespace("http://forgemesh.ai/ontology#"))
        accumulating_graph.add((URIRef(base_iri.rstrip("#/")), RDF.type, OWL.Ontology))

        _fm_owl = Path(self.settings.SEED_OUTPUT_PATH).parent / "fm_annotations.owl"
        if _fm_owl.exists():
            _fm_g = Graph()
            _fm_g.parse(str(_fm_owl), format="xml")
            for _triple in _fm_g:
                accumulating_graph.add(_triple)
            logger.info("Merged fm_annotations.owl (%d triples)", len(_fm_g))

        index = AccumulatedClassIndex(
            model_name=getattr(self.settings, "SEMANTIC_MATCHER_MODEL", "all-MiniLM-L6-v2")
        )

        total = len(ordered)
        for i, profile in enumerate(ordered, start=1):
            table_ctx = build_table_context(
                profile, processing_index=i, total_tables=total,
                max_distinct_for_enumeration=max_distinct_for_enum,
                max_distinct_for_open_class=max_distinct_for_open,
                skipped_tables=skip_tables,
                max_null_rate_for_class=cat_max_null,
                min_support_for_class=cat_min_support,
                max_uniqueness_for_class=cat_max_uniqueness,
            )
            det_uri = derive_class_uri(profile.table_name, base_iri)
            retrieved = index.retrieve(profile.table_name, top_k=top_k)

            logger.info(
                "Table %d/%d: '%s' → class URI <%s>  (retrieved %d context classes)",
                i, total, profile.table_name, det_uri, len(retrieved),
            )

            existing_prop_locals = [
                str(p).split("#")[-1].split("/")[-1]
                for ptype in (OWL.DatatypeProperty, OWL.ObjectProperty)
                for p in accumulating_graph.subjects(RDF.type, ptype)
            ]
            retrieved_abstract = index.retrieve_abstract_properties(
                query=profile.table_name.replace("_", " "), top_k=8
            )
            # Supplement with abstract ObjectProperties retrieved per FK column.
            # Querying by each FK's referenced table name ensures that hasAdmission,
            # hasPatient, hasIcustay etc. are surfaced even when the current table
            # name doesn't semantically match those abstract parents.
            _seen_abstract_uris = {ap.uri for ap in retrieved_abstract}
            for col in table_ctx.columns:
                if col.is_fk and col.fk_ref_table and not col.fk_ref_skipped:
                    fk_abstracts = index.retrieve_abstract_object_properties(
                        query=col.fk_ref_table.replace("_", " "), top_k=3
                    )
                    for ap in fk_abstracts:
                        if ap.uri not in _seen_abstract_uris:
                            retrieved_abstract.append(ap)
                            _seen_abstract_uris.add(ap.uri)

            gen_prompt = build_gen_prompt(
                table_ctx=table_ctx,
                retrieved_classes=retrieved,
                deterministic_uri=det_uri,
                base_iri=base_iri,
                existing_property_locals=existing_prop_locals,
                abstract_properties=retrieved_abstract,
                max_rows_for_enumeration=max_rows_for_enum,
            )

            last_manchester = ""
            fragment_graph: Optional[Graph] = None
            classes_declared: List[str] = []
            props_declared: List[str] = []
            verdict: Optional[JudgeVerdict] = None

            for attempt in range(max_retries + 1):
                # ---- Gen-LLM call ----
                manchester = self._call_gen_llm(gen_prompt, gen_model, gen_temp)
                last_manchester = manchester
                logger.debug(
                    "Table '%s' attempt %d — Gen-LLM returned %d chars",
                    profile.table_name, attempt + 1, len(manchester),
                )

                # ---- Parse ----
                fragment_graph, classes_declared, props_declared = parse_manchester(
                    manchester, base_iri, profile.table_name
                )

                fragment = OntologyFragment(
                    table_name=profile.table_name,
                    manchester_syntax=manchester,
                    classes_declared=classes_declared,
                    properties_declared=props_declared,
                    retry_count=attempt,
                )

                # ---- Dual-type pre-check ----
                dual_type_uris = check_duplicate_property_types(fragment_graph)
                if dual_type_uris:
                    logger.warning(
                        "Table '%s' attempt %d — %d dual-typed property URI(s): %s",
                        profile.table_name, attempt + 1, len(dual_type_uris),
                        [u.split('#')[-1] for u in dual_type_uris],
                    )

                # ---- Judge-LLM call ----
                all_accumulated = index._classes  # pass full list for duplicate detection
                sys_prompt, user_prompt = build_judge_prompt(
                    table_ctx=table_ctx,
                    fragment=fragment,
                    accumulated_classes=all_accumulated,
                    base_iri=base_iri,
                    max_rows_for_enumeration=max_rows_for_enum,
                )
                verdict = self._call_judge_llm(sys_prompt, user_prompt, judge_model, judge_temp)

                logger.debug(
                    "Table '%s' attempt %d — Judge: approved=%s confidence=%.2f "
                    "coverage_issues=%d coherence_issues=%d",
                    profile.table_name, attempt + 1,
                    verdict.approved, verdict.confidence,
                    len(verdict.coverage_issues), len(verdict.coherence_issues),
                )

                approved = verdict.approved or verdict.confidence >= judge_confidence_floor
                if approved or attempt == max_retries:
                    if not approved:
                        logger.warning(
                            "Table '%s': max retries reached — merging partial fragment anyway "
                            "(coverage_issues=%s, coherence_issues=%s)",
                            profile.table_name,
                            verdict.coverage_issues,
                            verdict.coherence_issues,
                        )
                    break

                # ---- Rebuild prompt with revision instructions ----
                gen_prompt = rebuild_with_revision(
                    original_prompt=gen_prompt,
                    revision_instructions=verdict.revision_instructions,
                    previous_output=manchester,
                )

            # ---- Postcondition checks (log violations; generator continues) ----
            if fragment_graph is not None and len(fragment_graph) > 0:
                from semantic_mapper.ontology.iterative.postconditions import (
                    check_no_cross_table_property_reuse,
                    check_no_scalar_as_object_property,
                )
                # G1: scalar columns must not be declared owl:ObjectProperty.
                g1_violations = check_no_scalar_as_object_property(
                    fragment_graph, table_ctx.columns
                )
                for _v in g1_violations:
                    logger.warning("G1 postcondition (table=%s): %s", profile.table_name, _v)

                # G3: new fragment must not reuse properties already scoped to
                # a different class in the accumulating graph.
                g3_violations = check_no_cross_table_property_reuse(
                    fragment_graph, accumulating_graph, det_uri
                )
                for _v in g3_violations:
                    logger.warning("G3 postcondition (table=%s): %s", profile.table_name, _v)

            # ---- Merge into accumulating graph ----
            if fragment_graph is not None and len(fragment_graph) > 0:
                merge_result = merge(
                    accumulating=accumulating_graph,
                    fragment_graph=fragment_graph,
                    table_name=profile.table_name,
                    base_iri=base_iri,
                    table_row_count=profile.row_count,
                    max_rows_for_enumeration=max_rows_for_enum,
                    fk_info=profile.foreign_keys,
                    skipped_tables=skip_tables,
                )
                index.add_all(merge_result.new_classes)
                new_abstract = _extract_new_abstract_properties(
                    accumulating_graph, base_iri, index._indexed_prop_uris
                )
                index.add_all_abstract_properties(new_abstract)
                _add_provenance_annotations(
                    accumulating_graph, merge_result, table_ctx
                )
                violations = synthesise_missing_provenance(
                    accumulating_graph,
                    [c.uri for c in merge_result.new_classes],
                    merge_result.new_props,
                    profile.table_name,
                )
                if violations and fragment is not None:
                    fragment.provenance_violations.extend(violations)
                logger.info(
                    "Table '%s' merged — +%d classes, +%d existing skipped%s",
                    profile.table_name,
                    len(merge_result.new_classes),
                    len(merge_result.skipped_duplicates),
                    f", {len(violations)} provenance violation(s)" if violations else "",
                )
            else:
                logger.warning(
                    "Table '%s': empty fragment graph after parsing — skipped", profile.table_name
                )

        # ---- Safety passes ----
        word_splits = getattr(self.settings, "ONTOLOGY_COL_WORD_SPLITS", {})
        normalize_datatype_property_uris(accumulating_graph, base_iri, word_splits)
        _qualify_obj_props_safety(accumulating_graph, base_iri)
        # Collapse LLM-generated domain-scoped sub-properties that are redundant
        # specialisations of an abstract parent with the same altLabel.
        # Must run BEFORE gap-fill so the cleaned-up state is visible to it.
        _collapse_domain_subproperty_duplicates(accumulating_graph)
        _fill_missing_column_properties(accumulating_graph, ordered, base_iri, word_splits)
        # Strip any dual-annotation fabrications introduced or surfaced by gap-fill:
        # a column appearing as both fm:sourceColumn (DatatypeProperty) and
        # fm:sourceForeignKey (ObjectProperty) in the full accumulated graph.
        # Preference: DatatypeProperty wins (the ObjectProperty is fabricated).
        _n_stripped = _strip_dual_annotated_object_properties(accumulating_graph)
        if _n_stripped:
            logger.warning(
                "Post-gap-fill dual-annotation strip: removed %d fabricated ObjectProperty/ies "
                "that shared a source column with a DatatypeProperty",
                _n_stripped,
            )
        _infer_missing_domains_and_ranges(accumulating_graph, ordered, base_iri)
        # Final authoritative sweep: strip rdfs:domain from any property that ended up
        # as a shared abstract parent (≥2 subproperties). Catches DatatypeProperty
        # abstracts and anything _infer_missing_domains_and_ranges may have added.
        _strip_domain_from_abstract_parents(accumulating_graph)
        # Remove abstract DatatypeProperties that are semantically superseded by
        # ObjectProperty abstracts — e.g. hasSubjectId (DatatypeProperty) is replaced
        # by hasPatient (ObjectProperty) once FK columns are correctly modelled.
        _remove_superseded_fk_datatype_abstracts(accumulating_graph, ordered, base_iri)
        _deduplicate_properties(accumulating_graph)
        # After deduplication, same-altLabel properties from different table fragments
        # have been merged into a single winner that now carries multiple rdfs:domain
        # triples.  Split each such multi-domain DatatypeProperty into one class-scoped
        # subproperty per domain (e.g. hasCharteventItemid, hasLabeventItemid) so the
        # domain-preference filter and annotation-exact short-circuit route correctly.
        _split_multi_domain_dt_properties(
            accumulating_graph, base_iri, word_splits, profiles=ordered
        )
        # Authoritative completeness pass: the first gap-fill (above) ran before the
        # dual-annotation strip, superseded-abstract removal, and dedup — any of which
        # can DELETE a property the LLM created (e.g. a column the LLM mis-modelled as a
        # bare ObjectProperty, removed as a fabricated FK), orphaning that column. The
        # first gap-fill skipped it (it was covered at the time), so re-run here, after
        # all property-removing passes, to GUARANTEE every non-PK/FK column has a
        # DatatypeProperty with fm:sourceColumn provenance. Idempotent — skips columns
        # already covered. Runs BEFORE the terminology binder so coded columns
        # (conditions.code etc.) exist to be re-typed regardless of LLM omissions.
        _fill_missing_column_properties(accumulating_graph, ordered, base_iri, word_splits)
        # G2 postcondition: after split, every (table, column) altLabel must have
        # a property whose rdfs:domain includes that table's class.
        from semantic_mapper.ontology.iterative.postconditions import check_split_column_coverage
        _table_to_class = {p.table_name: derive_class_uri(p.table_name, base_iri) for p in ordered}
        g2_violations = check_split_column_coverage(accumulating_graph, _table_to_class)
        for _v in g2_violations:
            logger.warning("G2 postcondition (split-pass): %s", _v)

        # Re-derive value-mapped vocabularies deterministically from the SOURCE PROFILE
        # BEFORE building owl:oneOf: one class per source-column concept (every `gender`
        # column → one Gender), exactly one individual per distinct non-null value, named
        # canonically. Supersedes the label-overlap consolidation pass — the LLM's
        # inconsistent member names (`M` vs `Male`) can't be reconciled by string keys.
        _n_minted = materialise_categorical_individuals(
            accumulating_graph, ordered, base_iri
        )
        if _n_minted:
            logger.info(
                "Materialised %d deterministic categorical individual(s) across vocab concepts",
                _n_minted,
            )

        # External terminology binding (SNOMED/ICD/LOINC/FHIR/OMOP): attach the
        # just-minted value-mapped individuals to authoritative external concepts
        # via skos:exactMatch, with provenance. No-op unless TERMINOLOGY_BINDING_
        # ENABLED. Runs here so bindings land on the clean individual set, before
        # owl:oneOf enumeration. Detection keys on value overlap only — no dataset
        # knowledge (CONTRIBUTING.md core principle).
        self._bind_terminology(accumulating_graph, ordered, base_iri)

        excluded_class_uris = {
            derive_class_uri(p.table_name, base_iri)
            for p in ordered
            if p.row_count > max_rows_for_enum
        }
        _build_oneof_enumerations(accumulating_graph, excluded_class_uris=excluded_class_uris, base_iri=base_iri)

        # ---- MIXED_RANGE resolution ----
        # Resolve every property carrying both a class range and an XSD range
        # (range_coherence_check's MIXED_RANGE). Runs here, AFTER union domains are
        # set and owl:oneOf vocabularies are built: R1 (source column is a real FK)
        # → ObjectProperty + class range; R2 (scalar: temporal/non-int-numeric type,
        # or range-class not a well-formed vocab) → DatatypeProperty + XSD range;
        # R3 (well-formed owl:oneOf vocab) → left in place and routed to the
        # coherence queue (class-vs-string is a human modelling decision).
        # Refresh abstract-parent union domains first so shared abstracts (e.g.
        # hasPatient) are keyable after the dedup/split passes mutated their children.
        _strip_domain_from_abstract_parents(accumulating_graph)
        mixed = resolve_mixed_ranges_in_graph(
            accumulating_graph, base_iri, ordered,
            max_distinct_for_enumeration=max_distinct_for_enum,
            max_null_rate_for_class=cat_max_null,
            min_support_for_class=cat_min_support,
            max_uniqueness_for_class=cat_max_uniqueness,
        )
        if any((mixed.resolved_fk, mixed.resolved_scalar, mixed.resolved_vocab_class,
                mixed.resolved_vocab_literal, mixed.routed_vocab, mixed.unkeyable)):
            logger.warning("MIXED_RANGE resolution: %s", mixed.summary())
            for plocal, src, cls in mixed.routed_vocab:
                logger.warning(
                    "MIXED_RANGE routed to coherence queue: <%s> (%s) — well-formed "
                    "vocab class <%s>; class-vs-xsd:string needs human review",
                    plocal, src, cls,
                )
            for tup in mixed.unkeyable:
                logger.warning("MIXED_RANGE unkeyable (source not determinable): <%s>", tup[0])

        # Final self-referential subPropertyOf sweep: covers triples introduced
        # by post-loop safety passes that bypass the per-merge guard.
        _scan_and_strip_self_ref_subprops(accumulating_graph)

        # Authoritative entity-FK promotion: an entity FK (column → a table that is a class)
        # left as a DatatypeProperty — or sourced from a descriptive name column instead of
        # the coded key — is re-typed to ObjectProperty(range=entity) and re-sourced to the
        # coded FK column so it resolves to the entity (keyed by that value). Runs last so it
        # is the final word on FK property typing, keyed on the inspector's foreign_keys.
        _promote_entity_fk_properties(accumulating_graph, ordered, base_iri)

        # Remove any legacy "Derived from" provenance rdfs:comment triples
        # (e.g. from OWL files loaded before Phase 0b, or stale safety-pass residue)
        _removed_legacy = remove_legacy_provenance_comments(accumulating_graph)
        if _removed_legacy:
            logger.info(
                "IterativeOntologyGenerator: removed %d legacy 'Derived from' rdfs:comment triples",
                _removed_legacy,
            )

        # Re-annotate any property left without a comment after the legacy strip.
        _annotate_missing_comments(accumulating_graph)

        # ---- Write output ----
        out_path = self._write_owl(accumulating_graph, profiles)
        logger.info(
            "IterativeOntologyGenerator: wrote %d triples to %s",
            len(accumulating_graph), out_path,
        )

        # Persist the MIXED_RANGE outcome as a machine-readable coherence queue
        # (routed/unkeyable items become an actionable list, not just log lines).
        queue_path = _write_coherence_queue(out_path, mixed)
        if queue_path is not None:
            n_review = len(mixed.routed_vocab) + len(mixed.unkeyable)
            logger.info(
                "IterativeOntologyGenerator: wrote coherence queue (%d item(s) need human review) to %s",
                n_review, queue_path,
            )

        # Persist the terminology-binding sidecar (sub-floor candidates + coverage
        # audit) next to the ontology, so no coded value is silently dropped.
        sidecar_path = _write_binding_sidecar(out_path, getattr(self, "_binding_result", None))
        if sidecar_path is not None:
            br = self._binding_result
            logger.info(
                "IterativeOntologyGenerator: wrote terminology-binding sidecar "
                "(%d asserted, %d for review, %d unmapped) to %s",
                br.n_asserted, br.n_sidecar, len(br.unmapped), sidecar_path,
            )

        return out_path

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _derive_base_iri(self, profiles: List[TableProfile]) -> str:
        """Resolve the ontology (schema) namespace.

        When ``ONTOLOGY_NAMESPACE`` is set it wins: a bare token becomes
        ``http://mesh.local/ontology/{token}#``; a full http(s) IRI is used verbatim.
        This is how a family of datasets (e.g. winterolympics 2026 + 2030) declare a
        single, source/year-agnostic schema so their classes, properties, and shared
        value-mapped vocabularies are the SAME URIs and aggregate across years.

        Otherwise it falls back to the bronze DB name, stripping the ``bronze``
        storage-layer token (prefix ``bronze_openflights`` or suffix
        ``olympics2026_bronze`` / ``mimic_bronze``) so the namespace identifies the
        dataset, not the storage layer (no ``_bronze`` in class URIs).
        """
        ns = (getattr(self.settings, "ONTOLOGY_NAMESPACE", "") or "").strip()
        if ns:
            if ns.startswith(("http://", "https://")):
                return ns if ns.endswith(("#", "/")) else ns + "#"
            return f"http://mesh.local/ontology/{ns}#"
        raw_db = self.settings.DATABASE_URL.rstrip("/").rsplit("/", 1)[-1]
        db_short = _re.sub(r"^bronze[-_]|[-_]bronze$", "", raw_db, flags=_re.IGNORECASE) or raw_db
        return f"http://mesh.local/ontology/{db_short}#"

    def _call_gen_llm(self, prompt: str, model: str, temperature: float) -> str:
        from semantic_mapper.core.llm import call_llm
        result = call_llm(
            prompt, self.settings, json_mode=False, temperature=temperature, model=model,
            disable_thinking=True,
        )
        self.prompt_tokens += result.prompt_tokens
        self.completion_tokens += result.completion_tokens
        return result.content

    def _call_judge_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str,
        temperature: float,
    ) -> JudgeVerdict:
        from semantic_mapper.core.llm import call_llm
        try:
            result = call_llm(
                user_prompt, self.settings,
                system_prompt=system_prompt,
                json_mode=True,
                temperature=temperature,
                model=model,
            )
            self.prompt_tokens += result.prompt_tokens
            self.completion_tokens += result.completion_tokens
            raw = json.loads(result.content or "{}")
            return JudgeVerdict(**raw)
        except Exception as exc:
            logger.warning("Judge-LLM call failed: %s — treating as approved at low confidence", exc)
            return JudgeVerdict(approved=True, confidence=0.3, revision_instructions="")

    def _project_db_source(self) -> tuple:
        """Stable (ontology_source, db_source) keys for the binding store — both
        survive re-materialise so confirmed bindings replay. Mirrors the project /
        db-name derivation used by _write_owl and _derive_base_iri."""
        raw_db = self.settings.DATABASE_URL.rstrip("/").rsplit("/", 1)[-1]
        db_short = _re.sub(r"^bronze[-_]", "", raw_db, flags=_re.IGNORECASE) or raw_db
        project = os.environ.get("FORGEMESH_PROJECT") or db_short
        return project, raw_db

    def _bind_terminology(self, graph: Graph, profiles: List[TableProfile], base_iri: str) -> None:
        """Attach external terminology bindings to the graph. Stores the
        BindingResult on self for the sidecar; safe no-op when disabled or on error."""
        self._binding_result = None
        if not getattr(self.settings, "TERMINOLOGY_BINDING_ENABLED", False):
            return
        try:
            from semantic_mapper.ontology.terminology import bind_terminology
            from semantic_mapper.db.binding_store import BindingStore

            ontology_source, db_source = self._project_db_source()
            store = BindingStore(getattr(self.settings, "BINDINGS_DB_PATH", "bindings.db"))
            self._binding_result = bind_terminology(
                graph, profiles, base_iri, self.settings,
                store=store, ontology_source=ontology_source, db_source=db_source,
            )
        except Exception as exc:  # binding must never crash generation
            logger.warning("Terminology binding pass failed: %s — continuing unbound", exc)

    def _write_owl(self, graph: Graph, profiles: List[TableProfile]) -> Path:
        out_dir = Path(self.settings.SEED_OUTPUT_PATH).parent
        out_dir.mkdir(parents=True, exist_ok=True)

        raw_db = self.settings.DATABASE_URL.rstrip("/").rsplit("/", 1)[-1]
        db_short = _re.sub(r"^bronze[-_]", "", raw_db, flags=_re.IGNORECASE) or raw_db
        project = os.environ.get("FORGEMESH_PROJECT") or db_short
        gen_model = (
            getattr(self.settings, "ONTOLOGY_GEN_MODEL", None)
            or getattr(self.settings, "LLM_MODEL", None)
            or self.settings.OPENAI_MODEL
        )
        out_path = ontology_filename(out_dir, project, "rigor", gen_model)

        graph.bind("fm", "http://forgemesh.ai/ontology#")
        graph.serialize(destination=str(out_path), format="xml")
        return out_path


# ---------------------------------------------------------------------------
# Coherence queue
# ---------------------------------------------------------------------------

def _write_coherence_queue(out_path: Path, mixed) -> Optional[Path]:
    """Persist the MIXED_RANGE resolution outcome as a machine-readable sidecar.

    Turns the routed/unkeyable items into an actionable *coherence queue* rather
    than log lines that vanish: writes ``<ontology>.coherence_queue.json`` next to
    the OWL whenever any mixed range was encountered. The ``needs_human_review``
    array is the queue itself — empty means the ontology was fully auto-resolved.

    Returns the path written, or ``None`` when there was nothing to record (a clean
    ontology with zero mixed ranges, e.g. the gpt-4o output).
    """
    if not any((mixed.resolved_fk, mixed.resolved_scalar, mixed.resolved_vocab_class,
                mixed.resolved_vocab_literal, mixed.routed_vocab, mixed.unkeyable)):
        return None

    needs_review = [
        {
            "property": plocal, "source": src, "issue": "vocab_class_vs_literal",
            "vocab_class": cls,
            "note": ("well-formed owl:oneOf vocabulary; keeping it a class vs demoting "
                     "to xsd:string is a human modelling decision (no inspector stat decided it)"),
        }
        for (plocal, src, cls) in mixed.routed_vocab
    ] + [
        {
            "property": tup[0], "issue": "unkeyable",
            "note": "source (table, col) not determinable from the graph; cannot auto-resolve",
        }
        for tup in mixed.unkeyable
    ]

    payload = {
        "ontology_file": out_path.name,
        "mixed_range": {
            "summary": mixed.summary(),
            "auto_resolved": {
                "fk_to_class": [list(x) for x in mixed.resolved_fk],
                "scalar_to_literal": [list(x) for x in mixed.resolved_scalar],
                "vocab_kept_as_class": [list(x) for x in mixed.resolved_vocab_class],
                "vocab_demoted_to_literal": [list(x) for x in mixed.resolved_vocab_literal],
            },
            "needs_human_review": needs_review,
        },
    }
    queue_path = out_path.with_suffix(".coherence_queue.json")
    queue_path.write_text(json.dumps(payload, indent=2))
    return queue_path


# ---------------------------------------------------------------------------
# Terminology-binding sidecar
# ---------------------------------------------------------------------------

def _write_binding_sidecar(out_path: Path, result) -> Optional[Path]:
    """Persist the terminology-binding outcome as ``<ontology>.terminology_bindings.json``.

    The sidecar is the review surface: sub-floor / ambiguous candidates that were
    NOT asserted, plus the per-column coverage audit (every distinct value in a
    detected coded column is either asserted, in review, or listed unmapped — so no
    coded value passes through silently). Returns ``None`` when binding was disabled
    or produced nothing.
    """
    if result is None or not (result.detected_columns or result.asserted or result.sidecar):
        return None
    payload = {
        "ontology_file": out_path.name,
        "summary": {
            "detected_columns": result.n_detected_columns,
            "asserted": result.n_asserted,
            "coded_properties": len(result.coded_properties),
            "needs_review": result.n_sidecar,
            "system_level": len(result.system_bindings),
            "unmapped": len(result.unmapped),
        },
        "detected_columns": [d.model_dump() for d in result.detected_columns],
        "asserted": [b.model_dump() for b in result.asserted],
        "coded_properties": [c.model_dump() for c in result.coded_properties],
        "needs_human_review": [b.model_dump() for b in result.sidecar],
        "system_level": [s.model_dump() for s in result.system_bindings],
        "unmapped": [u.model_dump() for u in result.unmapped],
    }
    sidecar_path = out_path.with_suffix(".terminology_bindings.json")
    sidecar_path.write_text(json.dumps(payload, indent=2))
    return sidecar_path


# ---------------------------------------------------------------------------
# Provenance annotation helpers
# ---------------------------------------------------------------------------

def _add_provenance_annotations(
    graph: Graph,
    merge_result: "MergeResult",
    table_ctx: "TableContext",
) -> None:
    """
    Stamp fm: provenance annotation triples on newly merged classes and properties.

    Classes receive:   fm:sourceTable "{table_name}"
    DataProperties:    fm:sourceColumn "{table}.{col}", fm:sqlType "{dtype}", fm:nullRate "{rate:.2f}"
    ObjectProperties:  fm:sourceForeignKey "{table}.{col} -> {target_table}"
                       (falls back to "not recoverable" when FK target is unknown)

    Only NEW entities (tracked in merge_result) are annotated. The skos:altLabel
    on a property holds the source column name; properties without an altLabel
    (abstract parents) are left unannotated here and caught by the post-merge sweep.
    """
    FM_SOURCE_TABLE  = URIRef("http://forgemesh.ai/ontology#sourceTable")
    FM_SOURCE_COLUMN = URIRef("http://forgemesh.ai/ontology#sourceColumn")
    FM_SOURCE_FK     = URIRef("http://forgemesh.ai/ontology#sourceForeignKey")
    FM_SQL_TYPE      = URIRef("http://forgemesh.ai/ontology#sqlType")
    FM_NULL_RATE     = URIRef("http://forgemesh.ai/ontology#nullRate")

    table_name = table_ctx.table_name
    col_info_map = {
        col.col_name.lower(): (col.inferred_type, col.null_rate)
        for col in table_ctx.columns
    }
    fk_target_map = {
        col.col_name.lower(): col.fk_ref_table
        for col in table_ctx.columns
        if col.is_fk and col.fk_ref_table and not col.fk_ref_skipped
    }

    # Reverse map: range class local name (lower) → (source_col_name, fk_ref_table)
    # Used as fallback for ObjectProperties that the LLM did not label with skos:altLabel.
    fk_range_to_col: dict = {}
    for col in table_ctx.columns:
        if col.is_fk and col.fk_ref_table and not col.fk_ref_skipped:
            range_local = derive_class_local_name(col.fk_ref_table).lower()
            fk_range_to_col[range_local] = (col.col_name, col.fk_ref_table)

    for cls_summary in merge_result.new_classes:
        cls_ref = URIRef(cls_summary.uri)
        graph.add((cls_ref, FM_SOURCE_TABLE, Literal(table_name)))

    for prop_uri_str in merge_result.new_props:
        prop_ref = URIRef(prop_uri_str)
        is_obj_prop = (prop_ref, RDF.type, OWL.ObjectProperty) in graph

        if is_obj_prop:
            # If the LLM already stamped fm:sourceForeignKey (via Manchester Syntax),
            # trust it and skip — adding another annotation would create duplicates.
            if list(graph.objects(prop_ref, FM_SOURCE_FK)):
                continue

            src_col: Optional[str] = None
            fk_ref: Optional[str] = None
            # Strategy 1: skos:altLabel is a known FK column name
            for alt in graph.objects(prop_ref, SKOS.altLabel):
                key = str(alt).lower()
                if key in fk_target_map:
                    src_col = str(alt)
                    fk_ref = fk_target_map[key]
                    break
            # Strategy 2: rdfs:range class local name maps back to a FK source column
            if not fk_ref:
                for rng in graph.objects(prop_ref, RDFS.range):
                    rng_local = str(rng).split("#")[-1].split("/")[-1].lower()
                    if rng_local in fk_range_to_col:
                        src_col, fk_ref = fk_range_to_col[rng_local]
                        break
            # Collect range class local names once for use in strategies 3 and 4.
            rng_locals = [
                str(rng).split("#")[-1].split("/")[-1]
                for rng in graph.objects(prop_ref, RDFS.range)
            ]
            # Strategy 3: skos:altLabel matches any known column (categorical ObjectProperty —
            # the LLM promoted a low-cardinality column to an enumeration class).
            if not fk_ref:
                for alt in graph.objects(prop_ref, SKOS.altLabel):
                    key = str(alt).lower()
                    if key in col_info_map:
                        src_col = str(alt)
                        fk_ref = rng_locals[0] if rng_locals else "unknown"
                        break
            # Strategy 4: derive column from range class local name via camelCase→snake_case.
            # Handles categorical ObjectProperties where the LLM omitted skos:altLabel.
            if not fk_ref:
                for rng_local in rng_locals:
                    snake = _camel_to_snake(rng_local)
                    if snake in col_info_map:
                        src_col = snake
                        fk_ref = rng_local
                        break
            fk_val = f"{table_name}.{src_col} -> {fk_ref}" if fk_ref and src_col else "not recoverable"
            graph.add((prop_ref, FM_SOURCE_FK, Literal(fk_val)))
        else:
            alt_labels = list(graph.objects(prop_ref, SKOS.altLabel))
            if not alt_labels:
                continue
            col_name = str(alt_labels[0])
            col_key = col_name.lower()
            dtype, null_rate = col_info_map.get(col_key, ("", 0.0))
            graph.add((prop_ref, FM_SOURCE_COLUMN, Literal(f"{table_name}.{col_name}")))
            graph.add((prop_ref, FM_SQL_TYPE, Literal(dtype or "unknown")))
            graph.add((prop_ref, FM_NULL_RATE, Literal(f"{null_rate:.2f}")))


# ---------------------------------------------------------------------------
# Safety pass: qualify object properties post-merge
# ---------------------------------------------------------------------------

def _qualify_obj_props_safety(graph: Graph, base_iri: str) -> None:
    """
    Safety pass: for each owl:ObjectProperty that has ≥2 distinct rdfs:domain values,
    create a single abstract parent (no domain) and make each domain-specific property
    a rdfs:subPropertyOf that abstract parent.

    This mirrors the full_generator's _qualify_obj_props logic at the graph level,
    operating directly on rdflib triples so it works regardless of how the Manchester
    parser captured the properties.
    """
    # Collect forward object properties grouped by range
    by_range: dict = {}
    for prop in graph.subjects(RDF.type, OWL.ObjectProperty):
        domains = list(graph.objects(prop, RDFS.domain))
        ranges = list(graph.objects(prop, RDFS.range))
        if not ranges:
            continue
        range_uri = str(ranges[0])
        by_range.setdefault(range_uri, []).append((prop, domains))

    for range_uri, prop_domain_pairs in by_range.items():
        if len(prop_domain_pairs) <= 1:
            continue  # Only one source class — no qualification needed

        # Only create abstract parent when props have genuinely different domains.
        # If all share the same domain (or all have no domain), they're duplicates —
        # skip; _deduplicate_properties will handle them.
        all_domains = [frozenset(str(d) for d in doms) for _, doms in prop_domain_pairs]
        if len(set(all_domains)) <= 1:
            continue

        range_local = range_uri.split("#")[-1].split("/")[-1]
        abstract_uri = URIRef(f"{base_iri}has{range_local}")

        # Declare abstract parent if not already present
        if (abstract_uri, RDF.type, OWL.ObjectProperty) not in graph:
            graph.add((abstract_uri, RDF.type, OWL.ObjectProperty))
            graph.add((abstract_uri, RDFS.label, Literal(f"has {range_local}", lang="en")))
            graph.add((abstract_uri, RDFS.range, URIRef(range_uri)))
            graph.add((abstract_uri, RDFS.comment,
                        Literal(f"Abstract object property for {range_local} relationships", lang="en")))
            logger.debug(
                "_qualify_obj_props_safety: created abstract parent <%s>", abstract_uri
            )

        # Abstract parents must have no rdfs:domain — strip any that exist.
        for dom in list(graph.objects(abstract_uri, RDFS.domain)):
            graph.remove((abstract_uri, RDFS.domain, dom))
            logger.warning(
                "_qualify_obj_props_safety: stripped incorrect rdfs:domain from "
                "abstract parent <%s>", abstract_uri,
            )

        # Make each domain-specific prop a subPropertyOf the abstract.
        # Guard: skip if prop_uri == abstract_uri (self-referential).
        for prop_uri, _ in prop_domain_pairs:
            if prop_uri == abstract_uri:
                logger.warning(
                    "_qualify_obj_props_safety: skipping self-referential subPropertyOf "
                    "for <%s>", prop_uri,
                )
                continue
            if (prop_uri, RDFS.subPropertyOf, abstract_uri) not in graph:
                graph.add((prop_uri, RDFS.subPropertyOf, abstract_uri))


# ---------------------------------------------------------------------------
# Post-generation pass: collapse redundant domain-scoped sub-property duplicates
# ---------------------------------------------------------------------------


def _collapse_domain_subproperty_duplicates(graph: Graph) -> None:
    """
    Collapse LLM-generated patterns where a domain-scoped DatatypeProperty is a
    redundant specialisation of an abstract parent with the same skos:altLabel.

    Example pattern eliminated:
      hasAirportAirportId  [domain=Airport, subPropertyOf hasAirportId, altLabel="airport_id"]
      hasAirportId         [no domain,                                   altLabel="airport_id"]
    → becomes:
      hasAirportId         [domain=Airport, altLabel="airport_id"]

    Conditions for collapsing P → parent Q:
      1. P is a DatatypeProperty with rdfs:subPropertyOf Q
      2. Q is a DatatypeProperty with no rdfs:domain (abstract)
      3. P has at least one rdfs:domain
      4. P and Q share at least one skos:altLabel (case-insensitive)
    """
    from rdflib.namespace import SKOS as _SKOS

    collapsed = 0
    for prop in list(graph.subjects(RDF.type, OWL.DatatypeProperty)):
        parents = list(graph.objects(prop, RDFS.subPropertyOf))
        for parent in parents:
            if (parent, RDF.type, OWL.DatatypeProperty) not in graph:
                continue
            if list(graph.objects(parent, RDFS.domain)):
                continue  # parent already has a domain — not a pure abstract
            domains = list(graph.objects(prop, RDFS.domain))
            if not domains:
                continue  # sub-property also has no domain — nothing to hoist
            sub_alts = {str(a).lower() for a in graph.objects(prop, _SKOS.altLabel)}
            parent_alts = {str(a).lower() for a in graph.objects(parent, _SKOS.altLabel)}
            if not (sub_alts & parent_alts):
                continue  # no shared altLabel — different concept
            # Hoist domain(s) onto parent
            for domain in domains:
                graph.add((parent, RDFS.domain, domain))
            # Remove the redundant sub-property entirely
            for triple in list(graph.triples((prop, None, None))):
                graph.remove(triple)
            for triple in list(graph.triples((None, None, prop))):
                graph.remove(triple)
            collapsed += 1
            logger.info(
                "_collapse_domain_subproperty_duplicates: collapsed <%s> → <%s> (domain hoisted)",
                prop, parent,
            )
            break  # prop is removed; stop iterating its parents

    if collapsed:
        logger.info(
            "_collapse_domain_subproperty_duplicates: collapsed %d redundant sub-property duplicate(s)",
            collapsed,
        )


# ---------------------------------------------------------------------------
# Post-generation pass: gap-fill missing column properties from schema profile
# ---------------------------------------------------------------------------

def _fill_missing_column_properties(
    graph: Graph,
    profiles: List["TableProfile"],
    base_iri: str,
    word_splits: dict | None = None,
) -> None:
    """
    Guarantee completeness: for every non-PK, non-FK column in every table
    that has no DatatypeProperty with a matching skos:altLabel already in the
    graph, create one directly from the schema profile.

    This catches tables where the LLM output was truncated or missed columns.
    Created properties get: rdf:type, rdfs:domain, rdfs:range (from SQL type),
    rdfs:label, rdfs:comment, and skos:altLabel.
    """
    from rdflib.namespace import SKOS

    # Build index of (alt_label, domain_uri_str) tuples for covered columns.
    # Abstract properties (no domain) are intentionally NOT added — they must not
    # block creation of class-scoped qualified properties for other classes.
    existing_alts: set[tuple] = set()  # (col_name_lower, domain_uri_str)
    for prop in list(graph.subjects(RDF.type, OWL.DatatypeProperty)) + \
                list(graph.subjects(RDF.type, OWL.ObjectProperty)):
        domains = list(graph.objects(prop, RDFS.domain))
        for alt in graph.objects(prop, SKOS.altLabel):
            alt_lo = str(alt).lower()
            for d in domains:
                existing_alts.add((alt_lo, str(d)))

    # Collect FK column names for each table so gap-fill skips them.
    # Keys use both 'constrained_column' (singular, inspector output) and
    # 'column' (alias) to be robust to format variation.
    fk_cols: dict[str, set[str]] = {}
    for profile in profiles:
        fk_cols[profile.table_name] = {
            (fk.get("constrained_column") or fk.get("column") or "").lower()
            for fk in profile.foreign_keys
            if fk.get("constrained_column") or fk.get("column")
        }

    # Build a provenance-based index: (table, col_lower) → True for any column
    # already modelled as an ObjectProperty with fm:sourceForeignKey.
    # This catches naming mismatches where the LLM's ObjectProperty URI doesn't
    # match the canonical/qualified name expected by the URI checks below.
    _FM_SOURCE_FK = URIRef("http://forgemesh.ai/ontology#sourceForeignKey")
    obj_covered: set[tuple] = set()  # (table_lower, col_lower)
    for _prop in graph.subjects(RDF.type, OWL.ObjectProperty):
        for _val in graph.objects(_prop, _FM_SOURCE_FK):
            _s = str(_val)
            if "." in _s and "->" in _s:
                _left = _s.split("->", 1)[0].strip()
                if "." in _left:
                    _tbl, _col = _left.split(".", 1)
                    obj_covered.add((_tbl.strip().lower(), _col.strip().lower()))

    created = 0
    for profile in profiles:
        class_uri = URIRef(derive_class_uri(profile.table_name, base_iri))
        # Ensure class exists
        if (class_uri, RDF.type, OWL.Class) not in graph:
            continue

        table_fk_cols = fk_cols.get(profile.table_name, set())

        for col_name, col_profile in profile.columns.items():
            if col_profile.is_primary_key:
                continue
            if col_name.lower() in table_fk_cols:
                continue  # confirmed FK → ObjectProperty already handled (or will be)
            col_lo = col_name.lower()
            if (profile.table_name.lower(), col_lo) in obj_covered:
                continue  # already an ObjectProperty with fm:sourceForeignKey for this col
            if (col_lo, str(class_uri)) in existing_alts:
                continue  # already covered for this domain

            # canonical_local handles has_ stripping and word expansion
            prop_local = canonical_local(col_name, word_splits)
            prop_uri = URIRef(base_iri + prop_local)

            # If an ObjectProperty already owns this canonical name, the column is
            # modelled as a relationship (categorical / FK) — no DataProperty needed.
            # Ensure the ObjectProperty has domain set (it may have been created without one).
            if (prop_uri, RDF.type, OWL.ObjectProperty) in graph:
                existing_domains = list(graph.objects(prop_uri, RDFS.domain))
                if not existing_domains:
                    # Unclaimed abstract — assign to this class and stop here
                    graph.add((prop_uri, RDFS.domain, class_uri))
                    existing_alts.add((col_name.lower(), str(class_uri)))
                    continue
                elif class_uri in existing_domains:
                    # Already correctly scoped to this class
                    existing_alts.add((col_name.lower(), str(class_uri)))
                    continue
                # else: belongs to a different class — fall through to create qualified version

            # Compute qualified (class-scoped) URI once — used by all checks below.
            class_local = str(class_uri).split("#")[-1].split("/")[-1]
            camel_body = prop_local[3:]  # strip leading 'has'
            qualified_uri = URIRef(f"{base_iri}has{class_local}{camel_body}")

            # Early-exit: the qualified DatatypeProperty already exists (LLM created it
            # with the right name but without rdfs:domain or skos:altLabel).
            if (qualified_uri, RDF.type, OWL.DatatypeProperty) in graph:
                from rdflib.namespace import SKOS
                if not list(graph.objects(qualified_uri, SKOS.altLabel)):
                    graph.add((qualified_uri, SKOS.altLabel, Literal(col_name, lang="en")))
                if not list(graph.objects(qualified_uri, RDFS.domain)):
                    graph.add((qualified_uri, RDFS.domain, class_uri))
                existing_alts.add((col_name.lower(), str(class_uri)))
                continue

            # Also check the qualified name as an ObjectProperty — the LLM may have
            # created e.g. hasAirportDst as an ObjectProperty (categorical enumeration)
            # without a skos:altLabel "dst", so the existing_alts check above missed it.
            if (qualified_uri, RDF.type, OWL.ObjectProperty) in graph:
                if not list(graph.objects(qualified_uri, RDFS.domain)):
                    graph.add((qualified_uri, RDFS.domain, class_uri))
                from rdflib.namespace import SKOS
                if not list(graph.objects(qualified_uri, SKOS.altLabel)):
                    graph.add((qualified_uri, SKOS.altLabel, Literal(col_name, lang="en")))
                existing_alts.add((col_name.lower(), str(class_uri)))
                continue

            # Qualified fallback for DataProperty name conflicts from a *different* class,
            # or when no domain is set (the property is abstract / not yet owned by anyone).
            # Only skip if the property is ALREADY explicitly scoped to this class.
            if (prop_uri, RDF.type, OWL.DatatypeProperty) in graph:
                existing_domains = list(graph.objects(prop_uri, RDFS.domain))
                if class_uri in existing_domains:
                    if not list(graph.objects(prop_uri, SKOS.altLabel)):
                        graph.add((prop_uri, SKOS.altLabel, Literal(col_name, lang="en")))
                    existing_alts.add((col_name.lower(), str(class_uri)))
                    continue
                # No domain or different class — create a class-qualified version.
                prop_uri = qualified_uri

            sql_type = (col_profile.inferred_type or "VARCHAR").upper().split("(")[0].strip()
            xsd_local = _SQL_TO_XSD.get(sql_type.lower(), "string")

            _fm_source_column_uri = URIRef("http://forgemesh.ai/ontology#sourceColumn")
            _fm_sql_type_uri = URIRef("http://forgemesh.ai/ontology#sqlType")
            _fm_null_rate_uri = URIRef("http://forgemesh.ai/ontology#nullRate")
            null_rate = getattr(col_profile, "null_rate", 0.0) or 0.0
            graph.add((prop_uri, RDF.type, OWL.DatatypeProperty))
            graph.add((prop_uri, RDFS.domain, class_uri))
            graph.add((prop_uri, RDFS.range, URIRef(_XSD_BASE + xsd_local)))
            graph.add((prop_uri, RDFS.label, Literal(
                f"has {col_name.replace('_', ' ')}", lang="en")))
            col_readable = col_name.replace("_", " ").strip()
            class_readable = _re.sub(r"(?<!^)(?=[A-Z])", " ", class_uri.split("#")[-1].split("/")[-1]).lower()
            graph.add((prop_uri, RDFS.comment, Literal(
                f"The {col_readable} of the {class_readable}.", lang="en")))
            graph.add((prop_uri, SKOS.altLabel, Literal(col_name, lang="en")))
            graph.add((prop_uri, _fm_source_column_uri,
                        Literal(f"{profile.table_name}.{col_name}")))
            graph.add((prop_uri, _fm_sql_type_uri, Literal(col_profile.inferred_type or "unknown")))
            graph.add((prop_uri, _fm_null_rate_uri, Literal(f"{null_rate:.2f}")))
            existing_alts.add((col_name.lower(), str(class_uri)))
            created += 1
            logger.debug("_fill_missing_column_properties: created <%s> for %s.%s",
                         prop_uri, profile.table_name, col_name)

    if created:
        logger.info("_fill_missing_column_properties: created %d gap-fill properties", created)


def _derive_class_comment(graph: Graph, cls: URIRef) -> str:
    """Structural rdfs:comment for a class the LLM never described (classes minted by
    deterministic passes: value vocabularies, terminology concept classes). Derived
    only from the graph — incoming property provenance and membership — never from
    dataset-specific names."""
    systems: Set[str] = set()
    cols: Set[str] = set()
    for prop in graph.subjects(RDFS.range, cls):
        systems.update(str(v) for v in graph.objects(prop, _FM_NS.terminologySystem))
        for v in graph.objects(prop, _FM_NS.sourceForeignKey):
            src = str(v).split("->", 1)[0].strip()
            if "." in src:
                cols.add(src)
        for v in graph.objects(prop, _FM_NS.sourceColumn):
            if "." in str(v):
                cols.add(str(v))
    ordered_cols = sorted(cols)
    col_text = ", ".join(ordered_cols[:5])
    if len(ordered_cols) > 5:
        col_text += f" and {len(ordered_cols) - 5} more"
    if systems:
        text = f"A concept from the {', '.join(sorted(systems))} code system"
        return text + (f", referenced by coded source column(s) {col_text}." if cols else ".")
    has_members = any(graph.subjects(RDF.type, cls)) or any(
        graph.objects(ec, OWL.oneOf) for ec in graph.objects(cls, OWL.equivalentClass)
    )
    if has_members and cols:
        return f"Controlled vocabulary of the distinct values of source column(s) {col_text}."
    if cols:
        return f"Referenced by source column(s) {col_text}."
    label = next((str(l) for l in graph.objects(cls, RDFS.label)), None) or _re.sub(
        r"(?<!^)(?=[A-Z])", " ", str(cls).split("#")[-1].split("/")[-1])
    return f"{'Vocabulary' if has_members else 'Class'}: {label}."


def _annotate_missing_comments(graph: Graph) -> None:
    """Add a rule-derived rdfs:comment to every class and property that still lacks one.

    Called AFTER remove_legacy_provenance_comments so that LLM-generated comments
    stripped by that pass are re-covered here. Also the final guarantee for classes
    minted by deterministic passes (categorical vocabularies, terminology binding).
    """
    from rdflib.namespace import SKOS
    annotated = 0
    for cls in list(graph.subjects(RDF.type, OWL.Class)):
        if not isinstance(cls, URIRef) or list(graph.objects(cls, RDFS.comment)):
            continue
        graph.add((cls, RDFS.comment, Literal(_derive_class_comment(graph, cls), lang="en")))
        annotated += 1
    for ptype in (OWL.DatatypeProperty, OWL.ObjectProperty):
        for prop in graph.subjects(RDF.type, ptype):
            if list(graph.objects(prop, RDFS.comment)):
                continue
            alt_labels = [str(lbl) for lbl in graph.objects(prop, SKOS.altLabel)]
            col_readable = (alt_labels[0].replace("_", " ") if alt_labels
                            else _re.sub(r"(?<!^)(?=[A-Z])", " ",
                                         str(prop).split("#")[-1].split("/")[-1]).lower().lstrip("has "))
            domains = list(graph.objects(prop, RDFS.domain))
            if domains:
                class_local = str(domains[0]).split("#")[-1].split("/")[-1]
                class_readable = _re.sub(r"(?<!^)(?=[A-Z])", " ", class_local).lower()
                comment = f"The {col_readable} of the {class_readable}."
            else:
                comment = f"The {col_readable}."
            graph.add((prop, RDFS.comment, Literal(comment, lang="en")))
            annotated += 1
    if annotated:
        logger.info("_annotate_missing_comments: annotated %d classes/properties", annotated)


# ---------------------------------------------------------------------------
# Post-generation pass: profile-backed domain + range inference
# ---------------------------------------------------------------------------

_SQL_TO_XSD: dict[str, str] = {
    "integer": "integer", "int": "integer", "smallint": "integer",
    "bigint": "integer", "mediumint": "integer", "tinyint": "integer",
    "decimal": "decimal", "numeric": "decimal",
    "float": "double", "double": "double", "real": "double",
    "boolean": "boolean", "bool": "boolean", "bit": "boolean",
    "date": "date",
    "datetime": "dateTime", "timestamp": "dateTime",
}
_XSD_BASE = "http://www.w3.org/2001/XMLSchema#"
_STRIP_PREFIXES = ("has", "is", "of")


def _camel_to_snake(name: str) -> str:
    """AdmissionType → admission_type."""
    s = _re.sub(r"([a-z])([A-Z])", r"\1_\2", name)
    return s.lower()


def _domain_candidates(prop_local: str) -> list[str]:
    """
    Return column-name candidates to try for domain inference from a property
    local name.  Ordered from most specific to least:
      1. Direct lowercase (admittime)
      2. CamelCase → snake_case (DrugType → drug_type)
      3. Strip *Type suffix, snake_case (GenderType → gender → gender)
      4. Strip *Type suffix only (FluidType → fluid)
    """
    candidates: list[str] = [prop_local.lower()]
    snake = _camel_to_snake(prop_local)
    if snake not in candidates:
        candidates.append(snake)
    if prop_local.endswith("Type") and len(prop_local) > 4:
        base = prop_local[:-4]
        base_snake = _camel_to_snake(base)
        if base_snake not in candidates:
            candidates.append(base_snake)
        if base.lower() not in candidates:
            candidates.append(base.lower())
    return candidates


def _infer_missing_domains_and_ranges(
    graph: Graph,
    profiles: List["TableProfile"],
    base_iri: str,
) -> None:
    """
    Profile-backed pass that repairs two classes of LLM output failure:

    1. Missing rdfs:domain — uses skos:altLabel (= original column name) to
       find which table/class a property came from and adds the domain triple.
       Falls back to stripping has/is/of prefix from the property local name.
       Only assigns domain when the column name is unambiguous (one table).

    2. Missing or wrong rdfs:range on DatatypeProperty — if range is absent or
       resolves outside xsd: (e.g. LLM wrote 'Range: integer' without prefix),
       infers from the column's SQL inferred_type using _SQL_TO_XSD.

    3. Missing rdfs:range on ObjectProperty — strips has/is prefix + For<Class>
       suffix from local name and matches against declared class local names.
    """
    from rdflib.namespace import SKOS

    # Build col_name → [(class_uri, sql_type)] — keep all tables for ambiguity detection
    col_to_entries: dict[str, list[tuple[str, str]]] = {}
    for profile in profiles:
        class_uri = derive_class_uri(profile.table_name, base_iri)
        for col_name, col_profile in profile.columns.items():
            sql_type = (col_profile.inferred_type or "VARCHAR").upper().split("(")[0].strip()
            col_to_entries.setdefault(col_name.lower(), []).append((class_uri, sql_type))

    # Class local-name map for ObjectProperty range inference
    class_local_map: dict[str, URIRef] = {}
    for cls_uri in graph.subjects(RDF.type, OWL.Class):
        local = str(cls_uri).split("#")[-1].split("/")[-1]
        class_local_map[local.lower()] = cls_uri  # type: ignore[assignment]

    dom_added = rng_added = 0

    for ptype in (OWL.DatatypeProperty, OWL.ObjectProperty):
        for prop in list(graph.subjects(RDF.type, ptype)):
            prop_local = str(prop).split("#")[-1].split("/")[-1]

            # ── 1. Domain inference ─────────────────────────────────────────
            if not list(graph.objects(prop, RDFS.domain)):
                # Skip abstract parents — properties with subproperties pointing to
                # them serve multiple classes and must remain domain-free.
                if not list(graph.subjects(RDFS.subPropertyOf, prop)):
                    alt_labels = [str(lbl) for lbl in graph.objects(prop, SKOS.altLabel)]
                    matched_class: Optional[str] = None
                    matched_sql: Optional[str] = None

                    for alt in alt_labels:
                        entries = col_to_entries.get(alt.lower(), [])
                        if len(entries) == 1:
                            matched_class, matched_sql = entries[0]
                            break

                    if not matched_class:
                        # Fallback: strip has/is/of prefix then try multiple name forms
                        candidate = prop_local
                        for pfx in _STRIP_PREFIXES:
                            if candidate.lower().startswith(pfx) and len(candidate) > len(pfx):
                                candidate = candidate[len(pfx):]
                                break
                        for col_candidate in _domain_candidates(candidate):
                            entries = col_to_entries.get(col_candidate, [])
                            if len(entries) == 1:
                                matched_class, matched_sql = entries[0]
                                break

                    if matched_class:
                        graph.add((prop, RDFS.domain, URIRef(matched_class)))
                        dom_added += 1
                        logger.debug("_infer_missing_domains: <%s> → domain <%s>", prop, matched_class)

            # ── 2. DatatypeProperty range ───────────────────────────────────
            if ptype == OWL.DatatypeProperty:
                current_ranges = list(graph.objects(prop, RDFS.range))
                needs_range = not current_ranges or all(
                    not str(r).startswith(_XSD_BASE) for r in current_ranges
                )
                if not needs_range:
                    continue

                for r in current_ranges:
                    graph.remove((prop, RDFS.range, r))

                # Find SQL type via altLabel
                sql_type: Optional[str] = None
                for alt in [str(lbl) for lbl in graph.objects(prop, SKOS.altLabel)]:
                    entries = col_to_entries.get(alt.lower(), [])
                    if entries:
                        sql_type = entries[0][1]
                        break

                xsd_local = _SQL_TO_XSD.get(sql_type.lower() if sql_type else "", "string")
                graph.add((prop, RDFS.range, URIRef(_XSD_BASE + xsd_local)))
                rng_added += 1

            # ── 3. ObjectProperty range via local-name matching ─────────────
            elif ptype == OWL.ObjectProperty:
                if list(graph.objects(prop, RDFS.range)):
                    continue

                candidate = prop_local
                for pfx in _STRIP_PREFIXES:
                    if candidate.lower().startswith(pfx) and len(candidate) > len(pfx):
                        candidate = candidate[len(pfx):]
                        break
                for_match = _re.search(r"For[A-Z]\w+$", candidate)
                if for_match:
                    candidate = candidate[: for_match.start()]
                if not candidate:
                    continue
                matched_cls = class_local_map.get(candidate.lower())
                if matched_cls is not None and matched_cls != prop:
                    graph.add((prop, RDFS.range, matched_cls))
                    rng_added += 1

    logger.info(
        "_infer_missing_domains_and_ranges: added %d domain triples, %d range triples",
        dom_added, rng_added,
    )


# ---------------------------------------------------------------------------
# Post-generation pass: build owl:oneOf enumerations from NamedIndividuals
# ---------------------------------------------------------------------------

def _canonicalise_individual_uris(
    graph: Graph,
    individuals: list,
    class_local: str,
    base_iri: str,
) -> list:
    """
    Rename individuals whose URI local name does not already end with *class_local*
    to the canonical ``{NormalisedValue}{ClassName}`` form (e.g. Female → FemaleGender).

    All graph triples referencing the old URI — including owl:oneOf list entries —
    are rewritten in place.  Returns the list of (possibly renamed) individual URIRefs.
    """
    result = []
    for ind in individuals:
        ind_str = str(ind)
        ind_local = ind_str.split("#")[-1].split("/")[-1]
        if ind_local.endswith(class_local):
            result.append(ind)
            continue
        words = _re.split(r"[^a-zA-Z0-9]+", ind_local)
        words = [w for w in words if w]
        if not words:
            result.append(ind)
            continue
        clean = "".join(w[0].upper() + w[1:] for w in words)
        new_local = f"{clean}{class_local}"
        if base_iri:
            new_uri = URIRef(f"{base_iri}{new_local}")
        elif "#" in ind_str:
            new_uri = URIRef(ind_str.rsplit("#", 1)[0] + "#" + new_local)
        else:
            new_uri = URIRef(ind_str.rsplit("/", 1)[0] + "/" + new_local)
        if new_uri == ind:
            result.append(ind)
            continue
        for s, p, o in list(graph.triples((ind, None, None))):
            graph.remove((s, p, o))
            graph.add((new_uri, p, o))
        for s, p, o in list(graph.triples((None, None, ind))):
            graph.remove((s, p, o))
            graph.add((s, p, new_uri))
        logger.debug("_build_oneof_enumerations: renamed <%s> → <%s>", ind, new_uri)
        result.append(new_uri)
    return result


def _build_oneof_enumerations(
    graph: Graph,
    excluded_class_uris: set = None,
    base_iri: str = "",
) -> None:
    """
    For each owl:Class that has NamedIndividuals declared as its type:
      1. Normalise individual URIs to {Value}{ClassName} convention.
      2. Add an owl:equivalentClass owl:oneOf list if not already present.

    excluded_class_uris: skip these class URIs (used for high-row-count entity tables).
    base_iri: used when deriving canonical individual URIs; falls back to the
              individual's own namespace when empty.
    """
    from rdflib import BNode
    from rdflib.collection import Collection

    class_to_individuals: dict[URIRef, list[URIRef]] = {}
    for ind in graph.subjects(RDF.type, OWL.NamedIndividual):
        for type_uri in graph.objects(ind, RDF.type):
            if type_uri in (OWL.NamedIndividual,):
                continue
            if (type_uri, RDF.type, OWL.Class) in graph:
                class_to_individuals.setdefault(type_uri, []).append(ind)  # type: ignore[index]

    added = 0
    for cls_uri, individuals in class_to_individuals.items():
        if not individuals:
            continue
        # Skip entity classes (high row count tables)
        if excluded_class_uris and cls_uri in excluded_class_uris:
            logger.debug(
                "_build_oneof_enumerations: skipping entity class <%s> — excluded", cls_uri
            )
            continue

        class_local = str(cls_uri).split("#")[-1].split("/")[-1]
        individuals = _canonicalise_individual_uris(graph, individuals, class_local, base_iri)

        # Skip oneOf construction if equivalentClass already set (LLM may have produced it;
        # _canonicalise_individual_uris has already rewritten its list entries in place).
        if list(graph.objects(cls_uri, OWL.equivalentClass)):
            continue
        ind_list = sorted(individuals, key=str)
        list_bn = BNode()
        Collection(graph, list_bn, ind_list)
        oneof_bn = BNode()
        graph.add((oneof_bn, OWL.oneOf, list_bn))
        graph.add((cls_uri, OWL.equivalentClass, oneof_bn))
        added += 1
        logger.debug("_build_oneof_enumerations: <%s> ≡ {%s}", cls_uri,
                     ", ".join(str(i).split("#")[-1] for i in ind_list))

    if added:
        logger.info("_build_oneof_enumerations: added owl:oneOf for %d enumeration classes", added)


# ---------------------------------------------------------------------------
# Post-generation pass: deduplicate DatatypeProperties by skos:altLabel
# ---------------------------------------------------------------------------

def _deduplicate_properties(graph: Graph) -> None:
    """
    Remove duplicate DatatypeProperties that represent the same source column.

    Groups properties by skos:altLabel (= original column name).  When a group
    has more than one member, elects a single winner and merges all domain/range
    triples into it, then removes every loser and all their triples.

    Winner selection (highest score wins):
      +3  URI local-name starts with "has" (camelCase convention)
      +2  URI local-name is longer (more qualified, e.g. hasCharteventCharttime > hasCharttime)
      +1  URI local-name contains no underscore (avoids raw snake_case names)
    Tie-break: alphabetical ascending (stable across runs).
    """
    from collections import defaultdict
    from rdflib.namespace import SKOS

    # Group by altLabel
    alt_to_props: dict[str, list[URIRef]] = defaultdict(list)
    for prop in graph.subjects(RDF.type, OWL.DatatypeProperty):
        for alt in graph.objects(prop, SKOS.altLabel):
            alt_to_props[str(alt)].append(prop)  # type: ignore[arg-type]

    removed = 0
    for alt, props in alt_to_props.items():
        if len(props) <= 1:
            continue

        def _score(p: URIRef) -> tuple:
            local = str(p).split("#")[-1].split("/")[-1]
            has_prefix = 3 if local.startswith("has") else 0
            length_score = len(local)
            no_snake = 1 if "_" not in local else 0
            return (has_prefix + no_snake, length_score, local)

        sorted_props = sorted(props, key=_score, reverse=True)
        winner = sorted_props[0]
        losers = sorted_props[1:]

        # Merge domain triples from losers into winner
        winner_domains = set(graph.objects(winner, RDFS.domain))
        for loser in losers:
            for domain in graph.objects(loser, RDFS.domain):
                if domain not in winner_domains:
                    graph.add((winner, RDFS.domain, domain))
                    winner_domains.add(domain)
            # Merge range if winner has none
            if not list(graph.objects(winner, RDFS.range)):
                for rng in graph.objects(loser, RDFS.range):
                    graph.add((winner, RDFS.range, rng))
            # Remove all triples for loser
            for triple in list(graph.triples((loser, None, None))):
                graph.remove(triple)
            for triple in list(graph.triples((None, None, loser))):
                graph.remove(triple)
            removed += 1
            logger.debug(
                "_deduplicate_properties: removed <%s> (dupe of <%s> for column '%s')",
                str(loser).split("#")[-1], str(winner).split("#")[-1], alt,
            )

    if removed:
        logger.info("_deduplicate_properties: removed %d duplicate DatatypeProperties", removed)


# ---------------------------------------------------------------------------
# Post-accumulation pass: split multi-domain DatatypeProperties into
# class-scoped subproperties with clean table-specific names.
# ---------------------------------------------------------------------------

def _split_multi_domain_dt_properties(
    graph: Graph,
    base_iri: str,
    word_splits: Optional[dict] = None,
    profiles: Optional[List["TableProfile"]] = None,
) -> int:
    """
    Find every DatatypeProperty with >1 rdfs:domain and split it into one
    class-scoped subproperty per domain, each named has{DomainClass}{Concept},
    all hanging off a truly abstract parent named has{Concept}.

    The abstract parent is derived from skos:altLabel (the source column name),
    NOT from the original property's local name.  This matters because
    _deduplicate_properties elects a table-specific winner (e.g.
    hasInputeventsMvItemId) whose name is wrong for a generic parent.
    Using has{Concept} (e.g. hasItemid) gives a stable, table-neutral root
    regardless of which table's property won deduplication.

    Capitalization tolerance: the computed scoped local name is compared
    case-insensitively against the original property's local name.  If they
    match (e.g. hasInputeventsMvItemId vs hasInputeventsMvItemid) the original
    is reused as the class-scoped subproperty for that domain rather than
    creating a second near-duplicate.

    Provenance restoration: ``_deduplicate_properties`` collapses every column
    that shares a name across tables (e.g. ``code`` in conditions/encounters/
    procedures/medications) into a single multi-domain winner and *discards the
    losers' ``fm:sourceColumn``*. The reclaimed winner keeps its own provenance,
    but the per-class children created here would otherwise be bare stubs with no
    ``fm:sourceColumn`` — invisible to any downstream pass that keys on source
    provenance (notably external terminology binding). When ``profiles`` is
    supplied, each concrete class-scoped child is re-annotated with the
    ``fm:sourceColumn``/``fm:sqlType``/``fm:nullRate`` for its (domain class →
    source table, column) pair, recovered from the schema profile. This is fully
    general: the table is the child's domain class, the column is its
    ``skos:altLabel`` — no dataset-specific names.

    Must run AFTER _deduplicate_properties.
    """
    from rdflib.namespace import SKOS
    from rdflib import BNode

    if word_splits is None:
        word_splits = {}

    def _to_concept(col_name: str) -> str:
        expanded = word_splits.get(col_name, col_name)
        return "".join(part.capitalize() for part in expanded.split("_") if part)

    # Provenance lookup keyed on (class_uri, column_name_lower) → (source_column,
    # sql_type, null_rate).  Lets each class-scoped child below recover the
    # fm:sourceColumn that dedup discarded for the losing tables.
    _FM = "http://forgemesh.ai/ontology#"
    _SRC = URIRef(_FM + "sourceColumn")
    _SQLT = URIRef(_FM + "sqlType")
    _NULLR = URIRef(_FM + "nullRate")
    prov: dict[tuple, tuple] = {}
    for prof in (profiles or []):
        cls_uri = derive_class_uri(prof.table_name, base_iri)
        for col_name, col_profile in prof.columns.items():
            null_rate = getattr(col_profile, "null_rate", 0.0) or 0.0
            prov[(cls_uri, col_name.lower())] = (
                f"{prof.table_name}.{col_name}",
                col_profile.inferred_type or "unknown",
                null_rate,
            )

    def _restore_provenance(target: URIRef, dom_uri, col_label: Optional[str]) -> None:
        """Backfill fm:sourceColumn/sqlType/nullRate onto a class-scoped child that
        lacks provenance, when the (domain class, column) pair is known from the
        profiles.  No-op for the reclaimed dedup winner (already provenanced) and
        when profiles are unavailable (e.g. unit tests)."""
        if col_label is None:
            return
        if list(graph.objects(target, _SRC)):
            return  # already provenanced — do not clobber the reclaimed winner
        info = prov.get((str(dom_uri), col_label.lower()))
        if not info:
            return
        source_column, sql_type, null_rate = info
        graph.add((target, _SRC, Literal(source_column)))
        graph.add((target, _SQLT, Literal(sql_type)))
        graph.add((target, _NULLR, Literal(f"{null_rate:.2f}")))

    rewritten = 0
    for prop in list(graph.subjects(RDF.type, OWL.DatatypeProperty)):
        domains = list(graph.objects(prop, RDFS.domain))
        if len(domains) <= 1:
            continue

        alts = list(graph.objects(prop, SKOS.altLabel))
        concept = _to_concept(str(alts[0])) if alts else str(prop).split("#")[-1][3:]
        # The original source column name (= altLabel) used to recover provenance
        # for each class-scoped child below. None when the property has no altLabel.
        col_label = str(alts[0]) if alts else None
        prop_local = str(prop).split("#")[-1]

        # Abstract parent: has{Concept} — named after the column concept alone,
        # no table prefix, so it is stable regardless of dedup winner.
        abstract_local = f"has{concept}"
        abstract_uri = URIRef(f"{base_iri}{abstract_local}")

        ranges = list(graph.objects(prop, RDFS.range))
        range_uri = ranges[0] if ranges else None

        # Ensure the abstract parent exists.
        if (abstract_uri, RDF.type, OWL.DatatypeProperty) not in graph:
            graph.add((abstract_uri, RDF.type, OWL.DatatypeProperty))
            if range_uri:
                graph.add((abstract_uri, RDFS.range, range_uri))
            for alt in alts:
                graph.add((abstract_uri, SKOS.altLabel, alt))

        # Strip all domain triples from the original — it becomes abstract or
        # is reclaimed as the scoped subproperty for its natural domain below.
        for dom in domains:
            graph.remove((prop, RDFS.domain, dom))

        created: list[str] = []
        for dom in domains:
            # A blank-node domain is a union/restriction class, not a concrete named
            # class — it cannot name a class-scoped subproperty without leaking the
            # bnode id into the name (e.g. hasN4762…Country). The concrete URIRef
            # domains below each get their proper scoped property; the union domain
            # is intentionally not carried onto a named subproperty.
            if isinstance(dom, BNode):
                continue
            cls_local = str(dom).split("#")[-1]
            scoped_local = f"has{cls_local}{concept}"
            scoped_uri = URIRef(f"{base_iri}{scoped_local}")

            if scoped_uri == abstract_uri:
                # Edge case: scoped name == abstract name.  Re-attach domain to
                # the abstract rather than making it self-referential.
                graph.add((abstract_uri, RDFS.domain, dom))

            elif (
                scoped_uri == prop
                or scoped_local.lower() == prop_local.lower()
            ):
                # The original property IS the correct scoped name for this domain
                # (exact URI match, or same name modulo capitalisation convention —
                # e.g. hasInputeventsMvItemId ↔ hasInputeventsMvItemid).
                # Re-attach its domain and wire it under the new abstract parent.
                graph.add((prop, RDFS.domain, dom))
                if (prop, RDFS.subPropertyOf, abstract_uri) not in graph:
                    graph.add((prop, RDFS.subPropertyOf, abstract_uri))
                _restore_provenance(prop, dom, col_label)

            elif (scoped_uri, RDF.type, OWL.DatatypeProperty) in graph:
                # Already exists — fix its parent pointer to the new abstract,
                # removing any stale pointer to the old multi-domain property.
                if (scoped_uri, RDFS.subPropertyOf, prop) in graph:
                    graph.remove((scoped_uri, RDFS.subPropertyOf, prop))
                if (scoped_uri, RDFS.subPropertyOf, abstract_uri) not in graph:
                    graph.add((scoped_uri, RDFS.subPropertyOf, abstract_uri))
                _restore_provenance(scoped_uri, dom, col_label)

            else:
                graph.add((scoped_uri, RDF.type, OWL.DatatypeProperty))
                graph.add((scoped_uri, RDFS.domain, dom))
                if range_uri:
                    graph.add((scoped_uri, RDFS.range, range_uri))
                graph.add((scoped_uri, RDFS.subPropertyOf, abstract_uri))
                for alt in alts:
                    graph.add((scoped_uri, SKOS.altLabel, alt))
                _restore_provenance(scoped_uri, dom, col_label)
                created.append(scoped_local)

        # If the original property was not reclaimed as a scoped subproperty
        # (its name didn't match any domain class), it is now domain-less.
        # Wire it under the abstract so it stays coherent in the hierarchy.
        if not list(graph.objects(prop, RDFS.domain)) and prop != abstract_uri:
            if (prop, RDFS.subPropertyOf, abstract_uri) not in graph:
                graph.add((prop, RDFS.subPropertyOf, abstract_uri))

        rewritten += 1
        logger.debug(
            "_split_multi_domain_dt_properties: split <%s> (%d domains) "
            "→ abstract <%s>%s",
            prop_local, len(domains), abstract_local,
            f"; created: {created}" if created else "",
        )

    if rewritten:
        logger.info(
            "_split_multi_domain_dt_properties: %d multi-domain DatatypeProperty/ies "
            "split into class-scoped subproperties under has{Concept} abstracts",
            rewritten,
        )
    return rewritten


# ---------------------------------------------------------------------------
# Post-generation pass: remove DatatypeProperty abstracts superseded by FK ObjectProperties
# ---------------------------------------------------------------------------

def _promote_entity_fk_properties(
    graph: Graph,
    profiles: List["TableProfile"],
    base_iri: str,
) -> int:
    """Deterministic safety net: an *entity* foreign key — a column referencing a table
    that is itself a class — modelled as a DatatypeProperty (or as an ObjectProperty with
    the wrong range) is promoted to an ObjectProperty whose ``rdfs:range`` is the referenced
    entity class and whose ``fm:sourceColumn`` is the FK column.

    Keyed on the inspector's ``foreign_keys`` (authoritative) and matched by the
    ``fm:sourceForeignKey`` annotation the generator already emits.  Fixes the coded+name
    entanglement: when a table carries both a descriptive name column (``venue``) and a
    coded FK to that entity (``venue_code`` → a Venue table), the gen-LLM tends to collapse
    them into one DatatypeProperty sourced from the *name*.  This pass re-types it to an
    ObjectProperty(range=entity) and strips ``fm:sourceColumn`` so the FK column is bound to
    this link via ``fm:sourceForeignKey`` only — leaving ``fm:sourceColumn`` on an FK property
    makes the column read as both a scalar and a FK, which the coherence check flags as a
    collision and forces to PENDING_REVIEW, so the link never materialises.

    Only acts when the referenced table corresponds to a declared ``owl:Class``; value-mapped
    categorical FKs (``gender`` → Gender) are untouched.  ``fixed`` counts only re-typed
    properties; the source/altLabel normalisation runs on every matched FK property (idempotent).
    """
    from rdflib.namespace import SKOS as _SKOS

    _FM = "http://forgemesh.ai/ontology#"
    _SRC = URIRef(_FM + "sourceColumn")
    _FK = URIRef(_FM + "sourceForeignKey")
    _XSD = "http://www.w3.org/2001/XMLSchema#"

    def _matches(ann: str, table: str, col: str, ref: str) -> bool:
        if "->" not in ann:
            return False
        left, right = ann.split("->", 1)
        return left.strip() == f"{table}.{col}" and right.strip() == ref

    fixed = 0
    for profile in profiles:
        c_t = URIRef(derive_class_uri(profile.table_name, base_iri))
        for fk in profile.foreign_keys:
            col = fk.get("constrained_column") or fk.get("column")
            ref = fk.get("referred_table") or fk.get("ref_table")
            if not col or not ref:
                continue
            c_ref = URIRef(derive_class_uri(ref, base_iri))
            if (c_ref, RDF.type, OWL.Class) not in graph:
                continue  # ref is not an entity class (value-mapped categorical) — leave it

            props = [
                p for p in graph.subjects(_FK, None)
                for ann in graph.objects(p, _FK)
                if _matches(str(ann), profile.table_name, col, ref)
            ]
            for p in set(props):
                is_obj = (p, RDF.type, OWL.ObjectProperty) in graph
                ranges = list(graph.objects(p, RDFS.range))
                # Leave an ObjectProperty that already points at a DIFFERENT declared entity
                # class — that is a deliberate modelling choice (e.g. country_code modelled as
                # → Country rather than the raw_medals-derived class), not the scalar/wrong-type
                # bug this pass repairs.  Only act when the range is this FK's class or a scalar.
                if is_obj and any(
                    URIRef(str(r)) != c_ref and (URIRef(str(r)), RDF.type, OWL.Class) in graph
                    for r in ranges
                ):
                    continue
                already_ok = is_obj and ranges == [c_ref]
                if not already_ok:
                    graph.remove((p, RDF.type, OWL.DatatypeProperty))
                    graph.add((p, RDF.type, OWL.ObjectProperty))
                    for r in ranges:
                        graph.remove((p, RDFS.range, r))
                    graph.add((p, RDFS.range, c_ref))
                    # An ObjectProperty must not be subPropertyOf a DatatypeProperty parent.
                    for parent in list(graph.objects(p, RDFS.subPropertyOf)):
                        if (parent, RDF.type, OWL.DatatypeProperty) in graph:
                            graph.remove((p, RDFS.subPropertyOf, parent))
                    fixed += 1
                # Domain must be the referencing class.
                if c_t not in list(graph.objects(p, RDFS.domain)):
                    for d in list(graph.objects(p, RDFS.domain)):
                        graph.remove((p, RDFS.domain, d))
                    graph.add((p, RDFS.domain, c_t))
                # An FK ObjectProperty is identified by fm:sourceForeignKey alone.  Strip any
                # fm:sourceColumn: it would make the FK column read as a scalar source too,
                # which the coherence check flags as a FK/literal collision and forces the
                # mapping to PENDING_REVIEW (so the link never materialises).  The AnnotationMatcher
                # binds the coded column from fm:sourceForeignKey, so no source column is needed.
                for s in list(graph.objects(p, _SRC)):
                    graph.remove((p, _SRC, s))
                for al in list(graph.objects(p, _SKOS.altLabel)):
                    graph.remove((p, _SKOS.altLabel, al))
                graph.add((p, _SKOS.altLabel, Literal(col, lang="en")))

    if fixed:
        logger.warning(
            "_promote_entity_fk_properties: promoted %d entity FK property(ies) to "
            "ObjectProperty re-sourced to the coded key column", fixed,
        )
    return fixed


def _remove_superseded_fk_datatype_abstracts(
    graph: Graph,
    profiles: List["TableProfile"],
    base_iri: str,
) -> None:
    """
    Remove abstract DatatypeProperties whose skos:altLabel matches a known FK column name
    AND whose concept is superseded by an abstract ObjectProperty for the FK's referenced class.

    Rationale: once FK columns are correctly modelled as ObjectProperty, abstract
    DatatypeProperties like hasSubjectId (altLabel='subject_id', subject_id→patients→hasPatient)
    are modelling artefacts from an earlier incorrect pass. Their role is replaced by the
    abstract ObjectProperty hierarchy (hasPatient, hasAdmission, hasIcustay). Removing them
    eliminates confusion in the mapper and prevents incorrect DatatypeProperty matches.

    Also removes all class-scoped subproperties of the superseded abstract (e.g.
    hasAdmissionSubjectId) since they are equally wrong.

    Only removes an abstract when the corresponding ObjectProperty abstract already exists —
    safe to run multiple times.
    """
    from rdflib.namespace import SKOS as _SKOS
    # Build: lower(fk_col_name) → ref_class_local  (first occurrence wins)
    fk_col_to_ref_class: dict[str, str] = {}
    for profile in profiles:
        for fk in profile.foreign_keys:
            col = fk.get("constrained_column") or fk.get("column")
            ref_table = fk.get("referred_table") or fk.get("ref_table")
            if col and ref_table and col.lower() not in fk_col_to_ref_class:
                fk_col_to_ref_class[col.lower()] = derive_class_local_name(ref_table)

    if not fk_col_to_ref_class:
        return

    removed_abstracts = removed_subprops = 0

    for prop in list(graph.subjects(RDF.type, OWL.DatatypeProperty)):
        # Must be abstract (no domain)
        if list(graph.objects(prop, RDFS.domain)):
            continue

        alt_labels = [str(lbl).lower() for lbl in graph.objects(prop, _SKOS.altLabel)]
        matched_col = next((al for al in alt_labels if al in fk_col_to_ref_class), None)
        if matched_col is None:
            continue

        ref_class_local = fk_col_to_ref_class[matched_col]
        expected_obj_abstract = URIRef(f"{base_iri}has{ref_class_local}")
        if (expected_obj_abstract, RDF.type, OWL.ObjectProperty) not in graph:
            continue  # ObjectProperty abstract not yet present — leave it

        # Remove all class-scoped subproperties of this abstract
        for sub in list(graph.subjects(RDFS.subPropertyOf, prop)):
            for triple in list(graph.triples((sub, None, None))):
                graph.remove(triple)
            for triple in list(graph.triples((None, None, sub))):
                graph.remove(triple)
            removed_subprops += 1
            logger.info(
                "_remove_superseded_fk_datatype_abstracts: removed subproperty <%s> "
                "(parent <%s> superseded by <%s>)",
                sub, prop, expected_obj_abstract,
            )

        # Remove the abstract itself
        for triple in list(graph.triples((prop, None, None))):
            graph.remove(triple)
        for triple in list(graph.triples((None, None, prop))):
            graph.remove(triple)
        removed_abstracts += 1
        logger.info(
            "_remove_superseded_fk_datatype_abstracts: removed <%s> "
            "(altLabel='%s' FK, superseded by ObjectProperty abstract <%s>)",
            prop, matched_col, expected_obj_abstract,
        )

    if removed_abstracts:
        logger.info(
            "_remove_superseded_fk_datatype_abstracts: removed %d abstract DatatypeProperty(ies) "
            "and %d subproperty(ies)",
            removed_abstracts, removed_subprops,
        )


# ---------------------------------------------------------------------------
# Helper: extract new abstract properties from the accumulating graph
# ---------------------------------------------------------------------------

def _extract_new_abstract_properties(
    graph: Graph,
    base_iri: str,
    already_indexed: set,
) -> "List[AccumulatedAbstractPropertySummary]":
    """
    Find all DatatypeProperty/ObjectProperty in *graph* that have no rdfs:domain,
    belong to *base_iri*, and are not already in *already_indexed*.
    Returns them as AccumulatedAbstractPropertySummary objects.
    """
    result: List[AccumulatedAbstractPropertySummary] = []
    for ptype, type_label in (
        (OWL.DatatypeProperty, "DatatypeProperty"),
        (OWL.ObjectProperty, "ObjectProperty"),
    ):
        for prop in graph.subjects(RDF.type, ptype):
            uri_str = str(prop)
            if uri_str in already_indexed:
                continue
            if not uri_str.startswith(base_iri):
                continue
            if list(graph.objects(prop, RDFS.domain)):
                continue  # not abstract
            local = uri_str.split("#")[-1].split("/")[-1]
            labels = list(graph.objects(prop, RDFS.label))
            label = str(labels[0]) if labels else local
            ranges = list(graph.objects(prop, RDFS.range))
            range_uri = str(ranges[0]) if ranges else ""
            result.append(AccumulatedAbstractPropertySummary(
                local_name=local,
                label=label,
                uri=uri_str,
                range_uri=range_uri,
                prop_type=type_label,
            ))
    return result
