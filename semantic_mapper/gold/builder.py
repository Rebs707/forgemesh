# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef

from semantic_mapper.gold.models import GoldColumnSpec, GoldTableDefinition
from semantic_mapper.schema.mapping_output import MappingStatus, SemanticMappingReport

_FM_SOURCE_FK  = URIRef("http://forgemesh.ai/ontology#sourceForeignKey")
_FM_SOURCE_COL = URIRef("http://forgemesh.ai/ontology#sourceColumn")

logger = logging.getLogger(__name__)


def _uri_fragment(uri: str) -> str:
    return uri.split("#")[-1] if "#" in uri else uri.split("/")[-1]


def _safe_col_name(col: str) -> str:
    return col.replace(" ", "_").replace("-", "_").lower()


def _camel_to_snake(name: str) -> str:
    """Convert camelCase/PascalCase URI local names to snake_case column names."""
    s = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower().replace("-", "_").replace(" ", "_")


def _pick_id_column(
    column_specs: list,
    col_table_counts: Dict[str, int],
) -> Optional[str]:
    """Return the gold column name most likely to be this entity's own PK.

    Uses source_column names (which retain ``_id`` suffixes regardless of how
    gold_column_name is formatted) to detect candidates.  Among non-row_id
    ``*_id`` source columns, picks the one that appears in the fewest tables
    (most exclusive = most likely the entity's own key).  Falls back to the
    row_id spec, then None.

    Returns the corresponding gold_column_name so the caller can use it
    directly as a row-dict key.
    """
    # Only consider columns that are exclusive to this entity (appear in exactly
    # one gold table).  Cross-table _id columns are FK references — using them
    # as the subject URI key collapses all rows sharing the same FK value into
    # one RDF individual (e.g. all diagnoses for the same admission become one
    # DiagnosesIcd subject with 129 subjects instead of 1761).
    candidates = [
        s for s in column_specs
        if s.source_column.endswith("_id")
        and s.source_column != "row_id"
        and col_table_counts.get(s.source_column, 0) == 1
    ]
    if candidates:
        return candidates[0].gold_column_name

    row_id_spec = next((s for s in column_specs if s.source_column == "row_id"), None)
    if row_id_spec:
        return row_id_spec.gold_column_name

    return None


def _entity_class_map(definitions: List[GoldTableDefinition]) -> Dict[str, str]:
    """Map entity short name → primary_class_uri.
    e.g. {"patient": "...#Patient", "encounter": "...#Encounter"}
    """
    return {
        d.gold_table_name[len("gold_"):]: d.primary_class_uri
        for d in definitions
        if d.gold_table_name.startswith("gold_")
    }


def _find_object_property(
    ref_class_uri: str,
    current_class_uri: str,
    ontology_graph: Graph,
) -> Optional[str]:
    """Return URI of object property connecting ref_class → current_class (preferred,
    triggers direction flip in TripleBuilder) or current_class → ref_class (fallback).
    Returns None if no match found.
    """
    preferred, fallback = None, None
    for prop in ontology_graph.subjects(RDF.type, OWL.ObjectProperty):
        domain = next(ontology_graph.objects(prop, RDFS.domain), None)
        range_ = next(ontology_graph.objects(prop, RDFS.range), None)
        if domain is None or range_ is None:
            continue
        if str(domain) == ref_class_uri and str(range_) == current_class_uri:
            preferred = str(prop)
            break
        if str(domain) == current_class_uri and str(range_) == ref_class_uri:
            fallback = str(prop)
    return preferred or fallback


def _build_fk_annotation_map(ontology_graph: Graph) -> Dict[Tuple[str, str], str]:
    """Return {(source_table, source_column): property_uri} from fm:sourceForeignKey annotations.

    Annotation value format: "raw_admissions.subject_id -> raw_patients"
    Only entries with a "table.column" left-hand side are included; pure-class
    references (e.g. "admission_type -> AdmissionType") are skipped because those
    columns are already handled via value_map in the TripleBuilder.
    """
    result: Dict[Tuple[str, str], str] = {}
    for prop, _, fk_val in ontology_graph.triples((None, _FM_SOURCE_FK, None)):
        val = str(fk_val)
        if "." not in val or "->" not in val:
            continue
        left = val.split("->", 1)[0].strip()
        if "." not in left:
            continue
        table, col = left.split(".", 1)
        result[(table.strip(), col.strip())] = str(prop)
    return result


def _build_source_column_map(ontology_graph: Graph) -> Dict[Tuple[str, str], str]:
    """Return {(source_table, source_column): property_uri} from fm:sourceColumn annotations.

    Annotation value format: "raw_diagnoses_icd.seq_num"

    Used as Strategy 3 in _resolve_fk_properties to fix property URIs for DatatypeProperty
    columns whose mapping-report URI is wrong due to multi-domain bleed in the ontology.
    Only properties that name a SINGLE source table.column are included — shared properties
    (e.g. hasInputeventsMvAmount annotated on both tables) would collide and are skipped.
    """
    counts: Dict[Tuple[str, str], int] = {}
    raw: Dict[Tuple[str, str], str] = {}
    for prop, _, sc_val in ontology_graph.triples((None, _FM_SOURCE_COL, None)):
        val = str(sc_val)
        if "." not in val:
            continue
        table, col = val.split(".", 1)
        key = (table.strip(), col.strip())
        counts[key] = counts.get(key, 0) + 1
        raw[key] = str(prop)
    return {k: v for k, v in raw.items() if counts[k] == 1}


def _resolve_property_uri_collisions(
    definitions: List[GoldTableDefinition],
    collisions: List[Dict],
) -> List[GoldTableDefinition]:
    """C2 fix: ensure each non-empty property_uri is owned by at most one column per table.

    This runs *after* ``_resolve_fk_properties`` so that OWL fm:sourceColumn annotations
    (which assign scoped sibling properties to multi-domain columns) can resolve apparent
    collisions before this function sees them.  Only genuine remaining collisions — where
    two gold columns still share the same property URI — are handled here.

    Resolution rule: the column with the highest ``confidence_score`` retains its
    property_uri.  All other columns have their property_uri cleared to ``""`` so that
    TripleBuilder skips triple emission for those columns (the data remains in gold for
    auditability).

    Collision records are appended to ``collisions`` so the caller can patch the mapping
    report on disk to show the losing column as ``PENDING_REVIEW``.
    """
    result = []
    for defn in definitions:
        # Group specs by property_uri (non-empty)
        groups: Dict[str, List[GoldColumnSpec]] = {}
        for spec in defn.column_specs:
            if spec.property_uri:
                groups.setdefault(spec.property_uri, []).append(spec)

        # Identify loser gold_column_names (all but the highest-confidence spec per URI)
        collision_losers: Set[str] = set()
        for prop_uri, specs in groups.items():
            if len(specs) <= 1:
                continue
            sorted_specs = sorted(specs, key=lambda s: s.confidence_score, reverse=True)
            winner = sorted_specs[0]
            for loser in sorted_specs[1:]:
                source_table = (
                    loser.silver_table[len("silver_"):]
                    if loser.silver_table.startswith("silver_")
                    else loser.silver_table
                )
                logger.warning(
                    "C2 property URI collision resolved: table=%s "
                    "winner=%s (confidence=%.2f) keeps <%s>; "
                    "loser=%s (confidence=%.2f) — triple emission suppressed, "
                    "assign a distinct mapped_property_uri → PENDING_REVIEW",
                    defn.gold_table_name,
                    winner.source_column, winner.confidence_score, prop_uri,
                    loser.source_column, loser.confidence_score,
                )
                collision_losers.add(loser.gold_column_name)
                collisions.append({
                    "silver_table": loser.silver_table,
                    "source_column": loser.source_column,
                    "property_uri": prop_uri,
                    "winner_column": winner.source_column,
                    "winner_confidence": winner.confidence_score,
                    "loser_confidence": loser.confidence_score,
                })

        if collision_losers:
            new_specs = [
                spec.model_copy(update={"property_uri": ""})
                if spec.gold_column_name in collision_losers
                else spec
                for spec in defn.column_specs
            ]
            result.append(defn.model_copy(update={"column_specs": new_specs}))
        else:
            result.append(defn)

    return result


def _resolve_fk_properties(
    definitions: List[GoldTableDefinition],
    ontology_graph: Graph,
) -> List[GoldTableDefinition]:
    """Return new definitions with FK column property_uris replaced by correct object properties.

    Three strategies are tried in order:
      1. Column name matches an entity short name (e.g. source_column="patient" → gold_patient).
      2. The ontology carries an fm:sourceForeignKey annotation whose table.column side matches
         this spec's silver_table + source_column (e.g. "raw_admissions.subject_id -> raw_patients"
         on hasAdmissionPatient). This handles FK columns whose name differs from the entity name.
      3. The ontology carries an fm:sourceColumn annotation that points a specific table.column to
         a property different from the one in the mapping report. This fixes multi-domain bleed:
         when the full_generator emits a single property with two rdfs:domain entries, the OWL
         patch adds a table-scoped sibling with the correct fm:sourceColumn, and Strategy 3
         routes each table's column to its own property.
    """
    ecmap = _entity_class_map(definitions)
    fk_map = _build_fk_annotation_map(ontology_graph)
    sc_map = _build_source_column_map(ontology_graph)
    result = []
    for defn in definitions:
        new_specs = []
        for spec in defn.column_specs:
            # Strategy 1: source column name is itself an entity short name.
            ref_class = ecmap.get(spec.source_column)
            if ref_class and ref_class != defn.primary_class_uri:
                obj_prop = _find_object_property(ref_class, defn.primary_class_uri, ontology_graph)
                # Don't reassign a DESCRIPTIVE NAME column (e.g. `venue`) to the entity's FK
                # object property when a coded sibling (`venue_code`/`venue_id`) in this table
                # already owns that property via fm:sourceForeignKey. The coded column is the
                # foreign key — and the value the entity's subject URI is minted from — so the
                # name column must keep its own (datatype) property; reassigning it here would
                # collide with the coded FK and bump it to PENDING, so the link never resolves.
                _raw_tbl = (
                    spec.silver_table[len("silver_"):]
                    if spec.silver_table.startswith("silver_") else spec.silver_table
                )
                _sc = spec.source_column.lower()
                if obj_prop and (
                    fk_map.get((_raw_tbl, f"{_sc}_code")) == obj_prop
                    or fk_map.get((_raw_tbl, f"{_sc}_id")) == obj_prop
                ):
                    new_specs.append(spec)
                    continue
                if obj_prop:
                    logger.info(
                        "FK property resolved (name match)",
                        extra={
                            "table": defn.gold_table_name,
                            "column": spec.gold_column_name,
                            "old_uri": spec.property_uri,
                            "new_uri": obj_prop,
                        },
                    )
                    spec = spec.model_copy(update={"property_uri": obj_prop})
                else:
                    # Strategy 1 found the entity class but _find_object_property
                    # returned None — likely a namespace mismatch between the entity
                    # class map (built from OrgHealth.owl via value_map URIs) and the
                    # rigor OWL (which uses a different namespace prefix for the same
                    # concepts).  Fall through to Strategies 2 and 3 so that
                    # fm:sourceForeignKey / fm:sourceColumn annotations can still
                    # route the column to the correct OWL property.
                    raw_tbl = (
                        spec.silver_table[len("silver_"):]
                        if spec.silver_table.startswith("silver_")
                        else spec.silver_table
                    )
                    fk_prop = fk_map.get((raw_tbl, spec.source_column))
                    if fk_prop and fk_prop != spec.property_uri:
                        logger.info(
                            "FK property resolved (Strategy 1 fallthrough → fm:sourceForeignKey)",
                            extra={
                                "table": defn.gold_table_name,
                                "column": spec.gold_column_name,
                                "old_uri": spec.property_uri,
                                "new_uri": fk_prop,
                            },
                        )
                        spec = spec.model_copy(update={"property_uri": fk_prop})
                    else:
                        sc_prop = sc_map.get((raw_tbl, spec.source_column))
                        if sc_prop and sc_prop != spec.property_uri:
                            logger.info(
                                "Property URI corrected (Strategy 1 fallthrough → fm:sourceColumn override)",
                                extra={
                                    "table": defn.gold_table_name,
                                    "column": spec.gold_column_name,
                                    "old_uri": spec.property_uri,
                                    "new_uri": sc_prop,
                                },
                            )
                            spec = spec.model_copy(update={"property_uri": sc_prop})
            elif not ref_class:
                raw_tbl = (
                    spec.silver_table[len("silver_"):]
                    if spec.silver_table.startswith("silver_")
                    else spec.silver_table
                )
                # Strategy 2: fm:sourceForeignKey annotation lookup.
                fk_prop = fk_map.get((raw_tbl, spec.source_column))
                if fk_prop and fk_prop != spec.property_uri:
                    logger.info(
                        "FK property resolved (fm:sourceForeignKey)",
                        extra={
                            "table": defn.gold_table_name,
                            "column": spec.gold_column_name,
                            "old_uri": spec.property_uri,
                            "new_uri": fk_prop,
                        },
                    )
                    spec = spec.model_copy(update={"property_uri": fk_prop})
                else:
                    # Strategy 3: fm:sourceColumn exact-match override.
                    # Fixes property URI when the mapping report recorded a shared
                    # (multi-domain) property but the OWL has a scoped sibling.
                    sc_prop = sc_map.get((raw_tbl, spec.source_column))
                    if sc_prop and sc_prop != spec.property_uri:
                        logger.info(
                            "Property URI corrected (fm:sourceColumn override)",
                            extra={
                                "table": defn.gold_table_name,
                                "column": spec.gold_column_name,
                                "old_uri": spec.property_uri,
                                "new_uri": sc_prop,
                            },
                        )
                        spec = spec.model_copy(update={"property_uri": sc_prop})
            new_specs.append(spec)
        result.append(defn.model_copy(update={"column_specs": new_specs}))
    return result


class GoldDefinitionBuilder:
    """Derives GoldTableDefinition objects from a SemanticMappingReport.

    One GoldTableDefinition is emitted per unique primary ontology class URI.
    Tables that share a class URI are merged into a single gold table.

    After each ``build()`` call, ``self.collisions`` holds records of any
    C2 property-URI collisions that were resolved (loser column → empty
    property_uri).  The caller (main.py) uses this to patch the mapping
    report on disk, marking losers as PENDING_REVIEW.
    """

    def __init__(self) -> None:
        # Populated by build(); caller checks after build() returns.
        self.collisions: List[Dict] = []

    def build(
        self,
        report: SemanticMappingReport,
        ontology_graph: Optional[Graph] = None,
        gold_mode: int = 1,
        value_map_overrides: Optional[Dict[str, list]] = None,
    ) -> List[GoldTableDefinition]:
        self.collisions.clear()

        # Group TableMappings by primary class URI
        groups: Dict[str, list] = {}
        for tm in report.tables:
            if not tm.class_candidate_set:
                logger.warning(
                    "Skipping table with no class candidates",
                    extra={"table": tm.source_table},
                )
                continue
            uri = max(tm.class_candidate_set, key=lambda c: c.confidence_score).uri
            groups.setdefault(uri, []).append(tm)

        definitions: List[GoldTableDefinition] = []
        for class_uri, table_mappings in groups.items():
            class_label = _uri_fragment(class_uri).lower()
            gold_table_name = f"gold_{class_label}"
            source_silver_tables = [f"silver_{tm.source_table}" for tm in table_mappings]

            # Collect GoldColumnSpec from CONFIRMED mappings across all tables.
            # Deduplication rules:
            #   1. Same source column seen before → skip (true duplicate across merged tables).
            #   2. gold_column_name collision (rare edge case) → skip.
            #   3. Same property URI claimed by two or more source columns →
            #      warn and keep both columns in gold (data integrity).
            #      _resolve_property_uri_collisions() (post-OWL pass) clears the
            #      lower-confidence loser's property_uri so TripleBuilder skips
            #      triple emission for it.  This enforces C2 ratio = 1.0.
            seen_gold_names: Set[str] = set()
            seen_source_cols: Set[str] = set()
            seen_property_uris: Set[str] = set()
            column_specs: List[GoldColumnSpec] = []

            for tm in table_mappings:
                silver_table = f"silver_{tm.source_table}"
                # Sort CONFIRMED mappings by confidence DESC so that when two columns
                # share a property_uri, the higher-confidence one is processed first
                # (wins in seen_property_uris) — consistent with the tiebreaking in
                # _resolve_property_uri_collisions() which runs after OWL overrides.
                confirmed = sorted(
                    [m for m in tm.column_mappings if m.status == MappingStatus.CONFIRMED],
                    key=lambda m: m.confidence_score or 0.0,
                    reverse=True,
                )
                for mapping in confirmed:
                    if mapping.source_column in seen_source_cols:
                        continue  # same source column already included (merged table duplicate)

                    gold_col = _safe_col_name(mapping.source_column)
                    if gold_col in seen_gold_names:
                        continue  # safe_col name collision (rare edge case)

                    # Rule 3: property URI collision detected at mapping-report level.
                    # Both columns are written to gold (data integrity preserved).
                    # _resolve_property_uri_collisions() runs post-OWL and clears the
                    # lower-confidence loser's property_uri so TripleBuilder skips it.
                    # OWL fm:sourceColumn overrides (Strategy 3) can still fix apparent
                    # collisions before the post-OWL resolver sees them.
                    prop_uri = mapping.mapped_property_uri or ""
                    if prop_uri and prop_uri in seen_property_uris:
                        logger.warning(
                            "C2 pre-OWL collision (may resolve via fm:sourceColumn): "
                            "table=%s column=%s shares URI <%s> — "
                            "post-OWL resolver will suppress the lower-confidence loser.",
                            tm.source_table, mapping.source_column, prop_uri,
                        )
                    # (no skip — every confirmed mapping reaches gold)

                    seen_gold_names.add(gold_col)
                    seen_source_cols.add(mapping.source_column)
                    if prop_uri:
                        seen_property_uris.add(prop_uri)
                    override_key = f"{tm.source_table}.{mapping.source_column}"
                    if value_map_overrides and override_key in value_map_overrides:
                        value_map = {
                            str(r["source_value"]): r["mapped_uri"]
                            for r in value_map_overrides[override_key]
                            if r.get("source_value") is not None and r.get("mapped_uri")
                        }
                    else:
                        value_map = {
                            str(vm.source_value): vm.mapped_uri
                            for vm in (mapping.value_mappings or [])
                            if vm.source_value is not None and vm.mapped_uri
                        }
                    column_specs.append(
                        GoldColumnSpec(
                            silver_table=silver_table,
                            source_column=mapping.source_column,
                            gold_column_name=gold_col,
                            source_data_type=mapping.source_data_type,
                            property_uri=mapping.mapped_property_uri or "",
                            value_map=value_map,
                            confidence_score=mapping.confidence_score or 0.0,
                        )
                    )

            # Collect discriminated class URIs: value_mappings columns where
            # distinct mapped_uris > 1 (these become the actual rdf:type values
            # in the triplestore, distinct from primary_class_uri).
            discriminated: set = set()
            for tm in table_mappings:
                for m in tm.column_mappings:
                    if m.status != MappingStatus.CONFIRMED:
                        continue
                    if not m.value_mappings:
                        continue
                    mapped_uris = {vm.mapped_uri for vm in m.value_mappings}
                    if len(mapped_uris) > 1:
                        discriminated.update(mapped_uris)

            definitions.append(
                GoldTableDefinition(
                    gold_table_name=gold_table_name,
                    primary_class_uri=class_uri,
                    class_label=class_label,
                    source_silver_tables=source_silver_tables,
                    column_specs=column_specs,
                    discriminated_class_uris=sorted(discriminated),
                )
            )

        # id_column assignment. Prefer the inspector-detected PRIMARY KEY (carried in
        # the mapping report) when its column(s) are present in gold — name-agnostic
        # and not fooled by a non-unique column literally named `id` or by a unique
        # `name` column. Only fall back to the *_id name heuristic when no PK column
        # reached gold (e.g. a surrogate row_id that maps to no property — then
        # id_column stays None and the RDF subject keys off the PK-based source ref).
        pk_by_table: Dict[str, list] = {
            tm.source_table: list(getattr(tm, "primary_key_columns", []) or [])
            for tm in report.tables
        }

        def _pk_id_column(defn) -> Optional[str]:
            src_tables = [
                st[len("silver_"):] for st in defn.source_silver_tables
                if st.startswith("silver_")
            ]
            pk_src: list = []
            for st in src_tables:
                for c in pk_by_table.get(st, []):
                    if c not in pk_src:
                        pk_src.append(c)
            if not pk_src:
                return None
            # Use the gold column name where the PK is materialised, else the PK source
            # column name — a marker the FK guard matches (target_id_col == fk column),
            # while the RDF subject falls back to the PK value carried in
            # bronze_source_ref when that column was not projected into gold.
            gold_by_src = {s.source_column: s.gold_column_name for s in defn.column_specs}
            cols = [gold_by_src.get(c, c) for c in pk_src]
            return "|".join(cols) if len(cols) > 1 else cols[0]

        col_table_counts: Dict[str, int] = {}
        for defn in definitions:
            for spec in defn.column_specs:
                if spec.source_column.endswith("_id") and spec.source_column != "row_id":
                    col_table_counts[spec.source_column] = (
                        col_table_counts.get(spec.source_column, 0) + 1
                    )
        definitions = [
            defn.model_copy(
                update={
                    "id_column": _pk_id_column(defn)
                    or _pick_id_column(defn.column_specs, col_table_counts)
                }
            )
            for defn in definitions
        ]

        if ontology_graph is not None and definitions:
            definitions = _resolve_fk_properties(definitions, ontology_graph)
        # C2 fix: after OWL-annotation overrides have assigned final property URIs,
        # scan for any remaining duplicate predicates per gold table and clear the
        # lower-confidence loser's property_uri.  Records are stored in self.collisions
        # so the caller can patch the mapping report to PENDING_REVIEW.
        definitions = _resolve_property_uri_collisions(definitions, self.collisions)
        return definitions


def derive_fk_join_candidates(
    definitions: List[GoldTableDefinition],
    ontology_graph: Optional[Graph],
) -> Dict[str, Set[str]]:
    """For each entity that is the TARGET of an FK object property, return the set of
    its own source columns that incoming FKs reference by name.

    A child table's FK column (``chartevents.hadm_id``) points — via an OWL
    ObjectProperty whose ``rdfs:range`` is the parent class — at a parent entity
    (``Admission``). When the parent ALSO carries a column of that name
    (``admissions.hadm_id``), that column is the **join key** the parent's subject
    URI must be built on. Otherwise the parent subject (keyed on a surrogate PK such
    as ``row_id``) and the FK-target URI (hashed from the join value ``hadm_id``)
    resolve to different URIs and every incoming link dangles.

    Dataset-agnostic: derived purely from the ontology's FK object properties and
    column names, never from specific table/column strings. Returns
    ``{parent_gold_table_name: {parent_source_column, ...}}``; the caller applies a
    uniqueness check before adopting one as the id_column (so a non-unique
    coincidental name match — e.g. a fact table's own FK column — is rejected).
    """
    if ontology_graph is None:
        return {}
    prop_range: Dict[str, str] = {}
    for prop in ontology_graph.subjects(RDF.type, OWL.ObjectProperty):
        rng = next(ontology_graph.objects(prop, RDFS.range), None)
        if rng is not None:
            prop_range[str(prop)] = str(rng)
    class_to_def = {d.primary_class_uri: d for d in definitions if d.primary_class_uri}
    parent_src_cols = {
        d.gold_table_name: {s.source_column for s in d.column_specs} for d in definitions
    }
    fk_ref: Dict[str, Set[str]] = {}
    for child in definitions:
        for spec in child.column_specs:
            if not spec.property_uri:
                continue
            parent = class_to_def.get(prop_range.get(spec.property_uri, ""))
            if parent is None or parent.gold_table_name == child.gold_table_name:
                continue
            # Name match: the child's FK source column is also a column of the parent
            # (chartevents.hadm_id references admissions.hadm_id).
            if spec.source_column in parent_src_cols.get(parent.gold_table_name, set()):
                fk_ref.setdefault(parent.gold_table_name, set()).add(spec.source_column)
    return fk_ref


def refine_id_columns(
    definitions: List[GoldTableDefinition],
    gold_engine: Any,
    pk_source_tables: Optional[set] = None,
    fk_target_cols: Optional[Dict[str, Set[str]]] = None,
) -> List[GoldTableDefinition]:
    """Post-hoc: verify and assign id_column for all gold table definitions.

    Tables with an inspector-detected PRIMARY KEY (listed in pk_source_tables) TRUST
    the PK-derived id_column set in build(): a declared PK is unique by definition, and
    it may not be a materialised gold column (e.g. an identifier `code` that maps to no
    property) — so it must not be reset or overridden by a name/code heuristic. The
    heuristic passes below run ONLY for tables with no detected PK.

    Three-pass logic per definition:

    Pass 1 — verify existing id_column.
        _pick_id_column selects the most exclusive *_id column but cannot
        check actual uniqueness without a DB query.  For tables like gold_route,
        source_airport_id is exclusive to one table yet is not a PK (many routes
        share the same source airport).  If the column is not unique, reset to None
        so the table falls through to the next pass instead of collapsing N rows
        onto one RDF subject.

    Pass 2 — *_id candidate search (existing logic).
        For definitions with id_column=None, try *_id columns sorted by cross-table
        frequency (least-shared first).  First unique column wins.

    Pass 3 — name/code column fallback.
        For entities with no *_id primary key at all (e.g. a Country table keyed
        by name), pick the first unique non-audit column, preferring columns whose
        source name is "name", ends with "_name", is "code", or ends with "_code".
        This ensures the entity subject URI (country/Portugal) matches the FK value
        emitted by TripleBuilder from referencing tables (airport.country = Portugal).
    """
    from sqlalchemy import text as _sa_text

    col_table_counts: Dict[str, int] = {}
    for defn in definitions:
        for spec in defn.column_specs:
            if spec.source_column.endswith("_id") and spec.source_column != "row_id":
                col_table_counts[spec.source_column] = col_table_counts.get(spec.source_column, 0) + 1

    def _is_unique(conn, table: str, gold_col: str, threshold: float = 1.0) -> bool:
        """True if gold_col has no NULLs and distinct ratio >= threshold."""
        try:
            row = conn.execute(_sa_text(
                f"SELECT COUNT(*) AS total,"
                f" COUNT(`{gold_col}`) AS non_null,"
                f" COUNT(DISTINCT `{gold_col}`) AS uniq"
                f" FROM `{table}`"
            )).mappings().fetchone()
            if not row or row["total"] == 0:
                return False
            return (
                row["non_null"] == row["total"]           # no NULLs
                and row["uniq"] / row["total"] >= threshold
            )
        except Exception:
            return False

    def _name_sort_key(spec) -> int:
        sc = spec.source_column
        if sc == "name":
            return 0
        if sc.endswith("_name"):
            return 1
        if sc in ("code", "iso_code") or sc.endswith("_code"):
            return 2
        return 3

    refined: List[GoldTableDefinition] = []
    try:
        with gold_engine.connect() as conn:
            _pk_tables = pk_source_tables or set()
            for defn in definitions:
                chosen = defn.id_column

                # Trust the PK-derived id_column for tables with a detected PK — do not
                # verify/reset (the PK may not be a gold column) and do not run the
                # heuristic passes (a unique `name` must not be chosen over the real key).
                _src_tables = {
                    st[len("silver_"):] for st in defn.source_silver_tables
                    if st.startswith("silver_")
                }
                has_pk = bool(_src_tables & _pk_tables)

                # Pass 0: FK join-key override. If this entity is the TARGET of FK
                # object properties whose source column name-matches one of its own
                # columns, and that column is unique in gold, key the subject on it —
                # even over a surrogate PK. FKs reference the join key
                # (admissions.hadm_id), not the surrogate row_id, so a subject built
                # on the PK leaves every incoming link dangling. The uniqueness check
                # rejects a coincidental name match on a non-unique column (a fact
                # table's own FK column), which must not become the subject key.
                _join_candidates = (fk_target_cols or {}).get(defn.gold_table_name, set())
                _join_specs = sorted(
                    [s for s in defn.column_specs if s.source_column in _join_candidates],
                    key=lambda s: (0 if s.source_column.endswith("_id") else 1, s.source_column),
                )
                _picked_join_key = False
                for spec in _join_specs:
                    if _is_unique(conn, defn.gold_table_name, spec.gold_column_name):
                        chosen = spec.gold_column_name
                        _picked_join_key = True
                        logger.info(
                            "refine_id_columns: %s id_column=%s (FK join key — overrides PK/heuristic)",
                            defn.gold_table_name, chosen,
                        )
                        break

                # Pass 1: verify existing id_column is actually unique
                if not _picked_join_key and not has_pk and chosen is not None:
                    if not _is_unique(conn, defn.gold_table_name, chosen):
                        logger.debug(
                            "refine_id_columns: %s id_column=%s not unique — resetting",
                            defn.gold_table_name, chosen,
                        )
                        chosen = None

                # Pass 2: try *_id candidates sorted by cross-table frequency
                if chosen is None and not has_pk:
                    candidates = sorted(
                        [s for s in defn.column_specs
                         if s.source_column.endswith("_id") and s.source_column != "row_id"],
                        key=lambda s: col_table_counts.get(s.source_column, 0),
                    )
                    for spec in candidates:
                        if _is_unique(conn, defn.gold_table_name, spec.gold_column_name):
                            chosen = spec.gold_column_name
                            break

                # Pass 3: name/code column fallback for keyless dimension tables.
                # Uses a near-unique threshold (0.95) — duplicate name rows
                # (e.g. India×2) will merge onto one RDF subject, which is
                # semantically correct and consistent with FK join values.
                if chosen is None and not has_pk:
                    name_candidates = sorted(
                        [s for s in defn.column_specs
                         if not s.source_column.endswith("_id")
                         and not s.gold_column_name.startswith("_")],
                        key=_name_sort_key,
                    )
                    for spec in name_candidates:
                        if _is_unique(conn, defn.gold_table_name, spec.gold_column_name, threshold=0.95):
                            if not _is_unique(conn, defn.gold_table_name, spec.gold_column_name):
                                logger.warning(
                                    "refine_id_columns: %s using near-unique column '%s' as id "
                                    "(duplicate source rows will merge onto one RDF subject)",
                                    defn.gold_table_name, spec.gold_column_name,
                                )
                            chosen = spec.gold_column_name
                            break

                # Pass 4: composite key for tables with no single-column PK.
                # Try combinations of *_id columns (pairs, then triples) and
                # pick the smallest combination whose CONCAT_WS is unique.
                # Stored as pipe-separated gold column names in id_column so
                # TripleBuilder can detect and build composite subject URIs.
                # candidates was built in Pass 2 when chosen was None; rebuild
                # here in case Pass 2 was skipped (chosen was set then reset).
                _id_candidates = sorted(
                    [s for s in defn.column_specs
                     if s.source_column.endswith("_id") and s.source_column != "row_id"],
                    key=lambda s: col_table_counts.get(s.source_column, 0),
                )
                if chosen is None and not has_pk and len(_id_candidates) >= 2:
                    from itertools import combinations as _combos
                    for size in (2, 3):
                        if chosen is not None:
                            break
                        for combo in _combos(_id_candidates[:6], size):
                            cols_str = ", ".join(f"`{s.gold_column_name}`" for s in combo)
                            not_null = " AND ".join(
                                f"`{s.gold_column_name}` IS NOT NULL" for s in combo
                            )
                            try:
                                row_c = conn.execute(_sa_text(
                                    f"SELECT COUNT(*) AS total,"
                                    f" COUNT(DISTINCT CONCAT_WS('|', {cols_str})) AS uniq"
                                    f" FROM `{defn.gold_table_name}`"
                                    f" WHERE {not_null}"
                                )).mappings().fetchone()
                                if (row_c and row_c["total"] > 0
                                        and row_c["uniq"] == row_c["total"]):
                                    chosen = "|".join(s.gold_column_name for s in combo)
                                    logger.info(
                                        "refine_id_columns: %s composite id_column=%s",
                                        defn.gold_table_name, chosen,
                                    )
                                    break
                            except Exception:
                                continue

                if chosen != defn.id_column:
                    logger.info(
                        "refine_id_columns: %s id_column=%s",
                        defn.gold_table_name, chosen,
                    )
                refined.append(defn.model_copy(update={"id_column": chosen}))
    except Exception as exc:
        logger.warning("refine_id_columns: DB check failed (%s) — id_column unchanged", exc)
        return definitions
    return refined
