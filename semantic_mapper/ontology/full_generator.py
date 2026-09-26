# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Full ontology generator.

Produces a comprehensive OWL/RDF ontology from a profiled database schema using
two LLM passes:
  Pass 1 — structure:   identify all classes, object properties, and domain/range
  Pass 2 — properties:  for each class, enumerate every data property with semantic
                         names and correct XSD types using column statistics + samples

The result is a richer, more complete ontology than the seed generator — intended as
a working ontology ready for mapping, not just a starter skeleton.
"""
from __future__ import annotations

import json
import logging
import os
import re as _re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field
from rdflib import Graph, Namespace, URIRef, Literal, BNode
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from semantic_mapper.config.settings import Settings
from semantic_mapper.db.models import TableProfile
from semantic_mapper.ontology.naming import ontology_filename

logger = logging.getLogger(__name__)

_XSD_RANGE_MAP: Dict[str, URIRef] = {
    "string":   XSD.string,
    "integer":  XSD.integer,
    "decimal":  XSD.decimal,
    "float":    XSD.float,
    "double":   XSD.double,
    "boolean":  XSD.boolean,
    "date":     XSD.date,
    "dateTime": XSD.dateTime,
    "anyURI":   XSD.anyURI,
}

# Generic fragments that collide across classes when unqualified (issue #42).
# Multi-word temporal stems (starttime, stopdate, …) are included because clinical
# entities (Encounter, Procedure, Medication) all have start/stop timestamps and the
# deduplicator in _write_owl would otherwise keep only the first class's domain.
_GENERIC_PROP_FRAGMENTS: frozenset = frozenset({
    "code", "type", "status", "class", "name", "date", "id",
    "description", "reason", "cost", "amount",
    # Temporal — shared across Encounter, Procedure, Medication, Condition, …
    "start", "stop", "time", "startdate", "stopdate", "starttime", "stoptime",
})


# ── Pass-1 models ─────────────────────────────────────────────────────────────
def _table_stem(table_name: str) -> str:
    """Lower-cased table name without the bronze ``raw_`` prefix or a leading
    single-letter naming-convention prefix (``d_``, ``t_``, ``v_`` …)."""
    stem = _re.sub(r"^raw_", "", table_name, flags=_re.IGNORECASE).lower()
    return _re.sub(r"^[a-z]_(?=.)", "", stem)


def _leaf_reference_tables(profiles: List[TableProfile]) -> set:
    """Tables that other tables point at by FK but that point at nothing themselves:
    lookup, dictionary and reference tables, identified by structure, not by name."""
    names = {t.table_name for t in profiles}
    outgoing = {
        t.table_name: {
            fk.get("ref_table") for fk in (t.foreign_keys or [])
            if fk.get("ref_table") in names and fk.get("ref_table") != t.table_name
        }
        for t in profiles
    }
    referenced = {r for refs in outgoing.values() for r in refs}
    return {n for n in names if n in referenced and not outgoing[n]}


class _StructClass(BaseModel):
    local_name: str
    label: str
    comment: str
    source_tables: List[str] = Field(default_factory=list)


class _StructObjectProp(BaseModel):
    local_name: str
    label: str
    comment: str
    domain: str
    range: str
    source_fk: str = ""
    inverse_local_name: str = ""
    inverse_label: str = ""


class _SubclassRelation(BaseModel):
    child_class: str
    parent_class: str
    discriminator_column: str
    discriminator_value: str


class _StructureSpec(BaseModel):
    reasoning: str
    base_iri: str
    classes: List[_StructClass]
    object_properties: List[_StructObjectProp]
    subclass_relationships: List[_SubclassRelation] = Field(default_factory=list)


# ── Pass-2 models ─────────────────────────────────────────────────────────────
class _DataProp(BaseModel):
    local_name: str
    label: str
    comment: str
    xsd_range: str
    source_column: str


class _ClassProperties(BaseModel):
    class_local_name: str
    datatype_properties: List[_DataProp] = Field(default_factory=list)


# ── Pass-3 models (categorical enumeration classes, issue #45) ────────────────
class _CategoryValue(BaseModel):
    local_name: str    # CamelCase OWL individual name, e.g. "Male"
    label: str         # human-readable label, e.g. "Male"
    raw_value: str     # original DB value, e.g. "M"


class _CategorySpec(BaseModel):
    class_local_name: str            # owning OWL class, e.g. "Patient"
    property_local_name: str         # e.g. "hasBiologicalSex"
    property_label: str
    property_comment: str
    category_class_local_name: str   # e.g. "Sex"
    category_class_label: str
    category_class_comment: str
    source_column: str               # e.g. "patients.gender"
    values: List[_CategoryValue] = Field(default_factory=list)


class _CategorySpecBatch(BaseModel):
    categories: List[_CategorySpec] = Field(default_factory=list)


# ── Generator ─────────────────────────────────────────────────────────────────
class FullOntologyGenerator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0

    def generate(self, profiles: List[TableProfile]) -> Path:
        """Generate a full OWL file from table profiles. Returns path to written file."""
        logger.info("Full generator — pass 1: structure")
        structure = self._pass1_structure(profiles)

        # Correct any subclass relationships where the LLM attached subclasses to
        # a class from a different table than the one containing the discriminator column.
        structure = _fix_subclass_parents(structure)

        # Supplement with any (domain, range) pairs the LLM silently deduplicated
        structure = _supplement_obj_props(structure, profiles)

        logger.info(
            "Full generator — pass 1 complete: %d classes, %d object properties",
            len(structure.classes), len(structure.object_properties),
        )

        # Build FK-skip set so pass 2 doesn't generate DatatypeProperty for FK columns
        fk_skip = _build_fk_skip(profiles)

        # Detect categorical columns — these become owl:Class enumerations (issue #45)
        # and must be excluded from the DatatypeProperty pass
        cat_cols = _detect_categoricals(profiles, fk_skip,
                                        self.settings.CATEGORICAL_CLASS_THRESHOLD)

        logger.info("Full generator — pass 2: per-class data properties")

        # Reference (lookup/dictionary) tables: pointed at by FKs, pointing at nothing.
        _ref_tables = _leaf_reference_tables(profiles)

        # Pre-process: when Pass 1 lumps a reference table into a class alongside
        # another table and the class name doesn't token-match it (e.g. a lookup of
        # lab items placed under LabEvent), detach it so the sweep below can give it
        # its own class. A reference table that matches its class, or is its class's
        # only source, stays put.
        def _dict_tok_matches(tok: str, cls_norm: str) -> bool:
            min_len = max(3, len(tok) - 2)
            for n in range(len(tok), min_len - 1, -1):
                if tok[:n] in cls_norm:
                    return True
            return False

        for cls in structure.classes:
            if not cls.source_tables:
                continue
            kept, detached = [], []
            shared = len(cls.source_tables) > 1
            for tbl in cls.source_tables:
                if tbl not in _ref_tables or not shared:
                    kept.append(tbl)
                    continue
                tokens = [tok for tok in _table_stem(tbl).split("_") if len(tok) >= 3]
                if any(_dict_tok_matches(tok, cls.local_name.lower()) for tok in tokens):
                    kept.append(tbl)
                else:
                    detached.append(tbl)
                    logger.info(
                        "  dict-table pre-process: detached %s from class %s"
                        " (name mismatch — will be re-assigned by dict sweep)",
                        tbl, cls.local_name,
                    )
            cls.source_tables = kept

        # Build a set of all tables already claimed as source_tables by some class,
        # so the dictionary-table sweep below doesn't double-assign.
        _claimed_tables: set = set()
        for cls in structure.classes:
            for t in (cls.source_tables or []):
                _claimed_tables.add(t)

        # Reference-table sweep: unclaimed reference tables whose descriptive columns
        # should become DatatypeProperties on an existing class.
        #
        # Pass 1 often leaves these unassigned because their names don't match the
        # class of the table that references them (e.g. a code dictionary for
        # diagnoses vs a DiagnosisICD class), but they describe the same concept.
        #
        # Matching: split the table stem into underscore tokens, then score each
        # class by how many tokens appear (as substring or de-pluralised form) in the
        # class local name.  Take the best-scoring class if score >= 1.
        #
        # e.g. icd_diagnoses → tokens ['icd','diagnoses']
        #      DiagnosisICD → 'icd' in 'diagnosisicd'=True, 'diagnose' in 'diagnosisicd'=True → score=2
        _dict_extras: Dict[str, List[str]] = {}  # class_local_name → extra table names
        for t in profiles:
            if t.table_name in _claimed_tables:
                continue
            if t.table_name not in _ref_tables:
                continue
            dict_part = _table_stem(t.table_name)
            tokens = [tok for tok in dict_part.split("_") if len(tok) >= 3]
            if not tokens:
                continue

            def _tok_matches(tok: str, cls_norm: str) -> bool:
                """True if a meaningful stem of tok appears in cls_norm.
                Tries the full token first, then progressively shorter prefixes
                (down to max(3, len-2)) to handle plurals/suffixes.
                e.g. 'diagnoses' → tries 'diagnoses','diagnose','diagnos' → 'diagnos' matches.
                """
                min_len = max(3, len(tok) - 2)
                for n in range(len(tok), min_len - 1, -1):
                    if tok[:n] in cls_norm:
                        return True
                return False

            best_cls = None
            best_score = 0
            for cls in structure.classes:
                cls_norm = cls.local_name.lower()
                score = sum(1 for tok in tokens if _tok_matches(tok, cls_norm))
                if score > best_score:
                    best_score = score
                    best_cls = cls

            if best_cls is not None and best_score >= 1:
                _dict_extras.setdefault(best_cls.local_name, []).append(t.table_name)
                _claimed_tables.add(t.table_name)
                logger.info(
                    "  dict-table sweep: %s → class %s (token score=%d)",
                    t.table_name, best_cls.local_name, best_score,
                )
            else:
                # No matching class found — create one from the table name so that
                # every reference table produces a class regardless of whether the
                # Pass 1 LLM happened to emit it.
                parts = [p for p in dict_part.split("_") if p]
                camel = "".join(p.capitalize() for p in parts)
                if camel.endswith("s") and len(camel) > 3:
                    camel = camel[:-1]
                existing_names = {c.local_name.lower() for c in structure.classes}
                if camel.lower() not in existing_names:
                    label = " ".join(p.capitalize() for p in parts).rstrip("s")
                    new_cls = _StructClass(
                        local_name=camel,
                        label=label,
                        comment=f"Reference entity derived from {t.table_name}.",
                        source_tables=[t.table_name],
                    )
                    structure.classes.append(new_cls)
                    _claimed_tables.add(t.table_name)
                    logger.info(
                        "  dict-table sweep (new class): %s → new class %s",
                        t.table_name, camel,
                    )

        # Unclaimed-table sweep: any non-reference table still unclaimed after
        # Pass 1 and the reference-table sweep gets a class created heuristically.
        # This prevents tables like raw_labevents from silently disappearing
        # when the Pass 1 LLM omits them.
        for t in profiles:
            if t.table_name in _claimed_tables:
                continue
            if t.table_name in _ref_tables:
                continue  # unmatched reference table — skip (no fact data)
            stripped = _table_stem(t.table_name)
            # Derive CamelCase class name from table name:
            # raw_labevents → labevents → ['labevents'] → Labevents → Labevent
            parts = [p for p in stripped.split("_") if p]
            camel = "".join(p.capitalize() for p in parts)
            if camel.endswith("s") and len(camel) > 3:
                camel = camel[:-1]
            # Avoid colliding with an existing class name
            existing_names = {c.local_name.lower() for c in structure.classes}
            if camel.lower() in existing_names:
                continue
            label = " ".join(p.capitalize() for p in parts).rstrip("s")
            new_cls = _StructClass(
                local_name=camel,
                label=label,
                comment=f"Entity derived from {t.table_name}.",
                source_tables=[t.table_name],
            )
            structure.classes.append(new_cls)
            _claimed_tables.add(t.table_name)
            logger.info(
                "  unclaimed-table sweep: %s → new class %s",
                t.table_name, camel,
            )

        # Re-run subclass parent correction now that sweep-created classes
        # (e.g. Labitem) exist in structure.classes.  The first call after Pass 1
        # couldn't fix e.g. BloodGasLabItem → LabEvent because Labitem didn't
        # exist yet; now it can reassign them to the correct parent.
        structure = _fix_subclass_parents(structure)

        class_props: Dict[str, List[_DataProp]] = {}
        for cls in structure.classes:
            related_tables = list(cls.source_tables or [
                t.table_name for t in profiles
                if _re.sub(r"^raw_", "", t.table_name, flags=_re.IGNORECASE).lower()
                   in {cls.local_name.lower(), cls.local_name.lower() + "s"}
            ])
            # Append any dictionary tables linked to this class by the sweep above
            related_tables += [
                t for t in _dict_extras.get(cls.local_name, [])
                if t not in related_tables
            ]
            if not related_tables:
                continue
            props = self._pass2_class_properties(cls, related_tables, profiles,
                                                  fk_skip, cat_cols)
            class_props[cls.local_name] = props
            logger.info("  %s: %d data properties", cls.local_name, len(props))

        logger.info("Full generator — pass 3: categorical enumeration classes")
        cat_specs: List[_CategorySpec] = []
        for cls in structure.classes:
            related_tables = list(cls.source_tables or [
                t.table_name for t in profiles
                if _re.sub(r"^raw_", "", t.table_name, flags=_re.IGNORECASE).lower()
                   in {cls.local_name.lower(), cls.local_name.lower() + "s"}
            ])
            related_tables += [
                t for t in _dict_extras.get(cls.local_name, [])
                if t not in related_tables
            ]
            cls_cats = {
                col: vals
                for tbl in related_tables
                for col, vals in cat_cols.get(tbl, {}).items()
            }
            if cls_cats:
                specs = self._pass_categoricals(cls, cls_cats, profiles)
                cat_specs.extend(specs)
                logger.info("  %s: %d categorical properties", cls.local_name, len(specs))

        path = self._write_owl(structure, class_props, cat_specs, profiles)
        logger.info("Full ontology written to %s", path)
        return path

    # ── Pass 1: structure ─────────────────────────────────────────────────────
    def _pass1_structure(self, profiles: List[TableProfile]) -> _StructureSpec:
        db_name = self.settings.DATABASE_URL.rstrip("/").rsplit("/", 1)[-1]
        summary = _build_rich_summary(profiles, detail_level="overview")
        inferred = _infer_relationships(profiles)
        inferred_section = (
            "\nINFERRED RELATIONSHIPS (column name matches a table — treat as FK):\n"
            + "\n".join(f"  {r}" for r in inferred)
            if inferred else ""
        )

        discriminator_section = _build_discriminator_section(profiles)

        prompt = f"""You are an expert ontology engineer. Analyse this relational database and \
design the class hierarchy and relationship graph for a production OWL ontology.

DATABASE: {db_name}

SCHEMA OVERVIEW:
{summary}{inferred_section}{discriminator_section}

TASK:
1. Identify every distinct real-world entity type → one owl:Class per entity.
2. Identify every FK relationship (both explicit FK→ and INFERRED) → one owl:ObjectProperty per relationship.
   - For each object property, also provide its inverse (e.g. hasPatient ↔ isPatientOf).
3. Do NOT list datatype properties here — those come in a second pass.
4. Infer subclass (rdfs:subClassOf) relationships from DISCRIMINATOR COLUMNS where each distinct value \
represents a subtype of the table's main class. E.g. if party_type has values Person and Organisation, \
emit Person rdfs:subClassOf Party AND Organisation rdfs:subClassOf Party — also add Person and Organisation \
as classes if not already present.

RULES:
- local_name: CamelCase for classes, camelCase prefixed "has" for object properties.
- inverse_local_name: camelCase prefixed "is" / "of" (empty string if not applicable).
- Every INFERRED RELATIONSHIP row MUST produce an object property.
- base_iri: HTTP IRI derived from the database name.
- subclass_relationships: list each inferred child→parent subclass pair with the discriminator column \
  and the specific discriminator_value that represents the child class. Empty list if none.

Return JSON:
{{
  "reasoning": "...",
  "base_iri": "http://...",
  "classes": [{{"local_name":"...", "label":"...", "comment":"...", "source_tables":["..."]}}],
  "object_properties": [{{
    "local_name":"...", "label":"...", "comment":"...",
    "domain":"ClassName", "range":"ClassName",
    "source_fk":"table.col -> ref_table.id",
    "inverse_local_name":"...", "inverse_label":"..."
  }}],
  "subclass_relationships": [{{
    "child_class":"ChildClassName", "parent_class":"ParentClassName",
    "discriminator_column":"table.column_name", "discriminator_value":"value"
  }}]
}}"""

        raw = self._llm_json(prompt)
        spec = _StructureSpec.model_validate(raw)
        return spec

    # ── Pass 2: data properties per class ─────────────────────────────────────
    def _pass2_class_properties(
        self,
        cls: _StructClass,
        table_names: List[str],
        profiles: List[TableProfile],
        fk_skip: Dict[str, set],
        cat_cols: Dict[str, Dict[str, list]],
    ) -> List[_DataProp]:
        tables = [t for t in profiles if t.table_name in table_names]
        if not tables:
            return []

        col_descriptions: List[str] = []
        for tbl in tables:
            for col_name, col in tbl.columns.items():
                if col.is_primary_key:
                    continue
                if col_name in fk_skip.get(tbl.table_name, set()):
                    continue
                # Categorical columns are handled in pass 3 as enumeration classes
                if col_name in cat_cols.get(tbl.table_name, {}):
                    continue
                type_str = col.inferred_type.split("(")[0].upper()
                null_info = f"null_rate={col.null_rate:.0%}" if col.null_rate > 0 else "not nullable"
                card = f"distinct={col.distinct_count}"
                samples = ""
                if col.top_values:
                    vals = ", ".join(str(v["value"]) for v in col.top_values[:5])
                    samples = f" | top_values=[{vals}]"
                elif col.sample_data:
                    vals = ", ".join(str(v) for v in col.sample_data[:4])
                    samples = f" | sample=[{vals}]"
                col_descriptions.append(
                    f"  {tbl.table_name}.{col_name}  {type_str}  {null_info}  {card}{samples}"
                )

        if not col_descriptions:
            return []

        cols_text = "\n".join(col_descriptions)
        prompt = f"""You are an expert ontology engineer. For the OWL class "{cls.local_name}" \
(label: "{cls.label}", {cls.comment}), generate a complete owl:DatatypeProperty for EVERY column \
listed below. Do not skip any column.

COLUMNS (table.column  TYPE  nullability  cardinality  sample_values):
{cols_text}

RULES:
- local_name: camelCase starting with "has" followed by a semantic name (NOT just the raw column name \
  when a better semantic name exists — e.g. "birthdate" → "hasDateOfBirth").
- label: human-readable short phrase.
- comment: one sentence describing what this property captures semantically.
- xsd_range: one of: string, integer, decimal, float, double, boolean, date, dateTime, anyURI.
  Use the column type AND sample values to pick the best type.
- source_column: exact "table_name.column_name".
- Include EVERY column listed. Do not omit any.

Return JSON:
{{
  "class_local_name": "{cls.local_name}",
  "datatype_properties": [{{
    "local_name":"...", "label":"...", "comment":"...", "xsd_range":"...", "source_column":"table.col"
  }}]
}}"""

        raw = self._llm_json(prompt)
        result = _ClassProperties.model_validate(raw)

        # Sanitise xsd_range
        for prop in result.datatype_properties:
            if prop.xsd_range not in _XSD_RANGE_MAP:
                logger.warning(
                    "Unknown xsd_range '%s' on %s.%s — defaulting to string",
                    prop.xsd_range, cls.local_name, prop.local_name,
                )
                prop.xsd_range = "string"
        return result.datatype_properties

    # ── Pass 3: categorical enumeration classes (issue #45) ───────────────────
    def _pass_categoricals(
        self,
        cls: _StructClass,
        cat_cols: Dict[str, list],   # col_name → top_values list
        profiles: List[TableProfile],
    ) -> List[_CategorySpec]:
        """For each categorical column belonging to cls, ask the LLM to produce
        a semantic OWL enumeration class name and clean individual names."""
        col_lines = []
        for col_name, top_values in cat_cols.items():
            vals = ", ".join(f'"{v["value"]}"' for v in top_values[:15])
            col_lines.append(f"  {col_name}  values=[{vals}]")
        if not col_lines:
            return []

        cols_text = "\n".join(col_lines)
        prompt = f"""You are an expert ontology engineer. The OWL class "{cls.local_name}" \
has the following low-cardinality columns that should be modelled as OWL enumeration \
classes (owl:Class + owl:NamedIndividual) rather than plain string literals.

COLUMNS AND THEIR DISTINCT VALUES:
{cols_text}

For EACH column produce:
- property_local_name: camelCase "has" ObjectProperty name derived from the column name \
(e.g. column "payment_method" → "hasPaymentMethod"; column "shipping_tier" → "hasShippingTier")
- property_label: short human-readable label
- property_comment: one sentence
- category_class_local_name: CamelCase OWL class for the enumeration derived from the \
column name (e.g. column "payment_method" → "PaymentMethod")
- category_class_label: human-readable label
- category_class_comment: one sentence describing the enumeration
- values: for each distinct raw value, provide local_name (CamelCase clean name \
faithful to the raw value, e.g. raw "EXPRESS" → "Express"), label (human label), \
raw_value (the original DB value exactly as shown)

Return JSON:
{{
  "categories": [{{
    "class_local_name": "{cls.local_name}",
    "property_local_name": "...",
    "property_label": "...",
    "property_comment": "...",
    "category_class_local_name": "...",
    "category_class_label": "...",
    "category_class_comment": "...",
    "source_column": "table.column",
    "values": [{{"local_name":"...", "label":"...", "raw_value":"..."}}]
  }}]
}}"""

        raw = self._llm_json(prompt)
        batch = _CategorySpecBatch.model_validate(raw)
        return batch.categories

    # ── OWL writer ────────────────────────────────────────────────────────────
    def _write_owl(
        self,
        structure: _StructureSpec,
        class_props: Dict[str, List[_DataProp]],
        cat_specs: List[_CategorySpec],
        profiles: List[TableProfile],
    ) -> Path:
        # Strip any trailing # or / before normalising to a single trailing /
        base = structure.base_iri.rstrip("/#") + "/"
        ns = Namespace(base)
        g = Graph()
        g.bind("", ns)
        g.bind("owl", OWL)
        g.bind("rdfs", RDFS)
        g.bind("skos", SKOS)
        g.bind("xsd", XSD)

        g.add((URIRef(base.rstrip("/")), RDF.type, OWL.Ontology))

        # Classes
        class_uris: Dict[str, URIRef] = {}
        for cls in structure.classes:
            uri = ns[cls.local_name]
            class_uris[cls.local_name] = uri
            g.add((uri, RDF.type, OWL.Class))
            g.add((uri, RDFS.label, Literal(cls.label, lang="en")))
            if cls.comment:
                g.add((uri, RDFS.comment, Literal(cls.comment, lang="en")))

        # Issue #45: categorical enumeration classes — owl:Class + owl:NamedIndividual
        for spec in cat_specs:
            cat_uri = ns[spec.category_class_local_name]
            g.add((cat_uri, RDF.type, OWL.Class))
            g.add((cat_uri, RDFS.label, Literal(spec.category_class_label, lang="en")))
            if spec.category_class_comment:
                g.add((cat_uri, RDFS.comment, Literal(spec.category_class_comment, lang="en")))

            # owl:oneOf list of individuals
            ind_uris = []
            for val in spec.values:
                ind_uri = ns[val.local_name]
                g.add((ind_uri, RDF.type, OWL.NamedIndividual))
                g.add((ind_uri, RDF.type, cat_uri))
                g.add((ind_uri, RDFS.label, Literal(val.label, lang="en")))
                # Preserve the raw DB value as rdfs:comment so the mapping is traceable
                g.add((ind_uri, RDFS.comment, Literal(
                    f"Raw database value: {val.raw_value!r}", lang="en"
                )))
                ind_uris.append(ind_uri)
            if ind_uris:
                bn = BNode()
                Collection(g, bn, ind_uris)
                g.add((cat_uri, OWL.oneOf, bn))

            # ObjectProperty linking the owning class to the enumeration
            prop_uri = ns[spec.property_local_name]
            g.add((prop_uri, RDF.type, OWL.ObjectProperty))
            g.add((prop_uri, RDFS.label, Literal(spec.property_label, lang="en")))
            if spec.property_comment:
                g.add((prop_uri, RDFS.comment, Literal(spec.property_comment, lang="en")))
            if spec.class_local_name in class_uris:
                g.add((prop_uri, RDFS.domain, class_uris[spec.class_local_name]))
            g.add((prop_uri, RDFS.range, cat_uri))

        # Issue #44: qualify object properties when multiple domains share the same range
        qualified_obj_props, abstract_fwd_parents, abstract_inv_parents = (
            _qualify_obj_props(structure.object_properties)
        )

        # Build lookup sets for subPropertyOf link decisions
        abstract_fwd_names: set = {p.local_name for p in abstract_fwd_parents}
        abstract_inv_names: set = {p.local_name for p in abstract_inv_parents}

        # Abstract forward parents (no domain, range constrained)
        for parent in abstract_fwd_parents:
            p_uri = ns[parent.local_name]
            g.add((p_uri, RDF.type, OWL.ObjectProperty))
            g.add((p_uri, RDFS.label, Literal(parent.label, lang="en")))
            if parent.comment:
                g.add((p_uri, RDFS.comment, Literal(parent.comment, lang="en")))
            if parent.range in class_uris:
                g.add((p_uri, RDFS.range, class_uris[parent.range]))
            # owl:inverseOf the abstract inverse parent
            if parent.inverse_local_name:
                g.add((p_uri, OWL.inverseOf, ns[parent.inverse_local_name]))

        # Abstract inverse parents (domain constrained, no range)
        for parent in abstract_inv_parents:
            p_uri = ns[parent.local_name]
            g.add((p_uri, RDF.type, OWL.ObjectProperty))
            g.add((p_uri, RDFS.label, Literal(parent.label, lang="en")))
            if parent.comment:
                g.add((p_uri, RDFS.comment, Literal(parent.comment, lang="en")))
            if parent.domain in class_uris:
                g.add((p_uri, RDFS.domain, class_uris[parent.domain]))
            # owl:inverseOf the abstract forward parent
            if parent.inverse_local_name:
                g.add((p_uri, OWL.inverseOf, ns[parent.inverse_local_name]))

        # Object properties + inverses
        for prop in qualified_obj_props:
            uri = ns[prop.local_name]
            g.add((uri, RDF.type, OWL.ObjectProperty))
            g.add((uri, RDFS.label, Literal(prop.label, lang="en")))
            if prop.comment:
                g.add((uri, RDFS.comment, Literal(prop.comment, lang="en")))
            if prop.domain in class_uris:
                g.add((uri, RDFS.domain, class_uris[prop.domain]))
            if prop.range in class_uris:
                g.add((uri, RDFS.range, class_uris[prop.range]))
            # rdfs:subPropertyOf to abstract forward parent when this prop was qualified
            fwd_abstract = f"has{prop.range}"
            if fwd_abstract in abstract_fwd_names and prop.local_name != fwd_abstract:
                g.add((uri, RDFS.subPropertyOf, ns[fwd_abstract]))
            # Inverse
            if prop.inverse_local_name:
                inv_uri = ns[prop.inverse_local_name]
                g.add((inv_uri, RDF.type, OWL.ObjectProperty))
                g.add((inv_uri, RDFS.label, Literal(prop.inverse_label or prop.inverse_local_name, lang="en")))
                g.add((inv_uri, OWL.inverseOf, uri))
                g.add((uri, OWL.inverseOf, inv_uri))
                # Swap domain/range for inverse
                if prop.range in class_uris:
                    g.add((inv_uri, RDFS.domain, class_uris[prop.range]))
                if prop.domain in class_uris:
                    g.add((inv_uri, RDFS.range, class_uris[prop.domain]))
                # rdfs:subPropertyOf to abstract inverse parent when inverse was qualified
                inv_abstract = f"is{prop.range}Of"
                if inv_abstract in abstract_inv_names and prop.inverse_local_name != inv_abstract:
                    g.add((inv_uri, RDFS.subPropertyOf, ns[inv_abstract]))

        # Issue #42: qualify generic single-domain property local names with their class name
        class_props, abstract_parents = _qualify_generic_props(class_props)

        # Build reverse map: qualified local name → abstract parent local name
        # (needed to emit rdfs:subPropertyOf when writing each class-specific property)
        qualified_to_abstract: Dict[str, str] = {}
        for cls_name, props in class_props.items():
            for prop in props:
                fragment = _re.sub(r"^has", "", prop.local_name, count=1, flags=_re.IGNORECASE)
                # Reconstruct the original unqualified name: strip the class name from fragment
                # by checking whether stripping the class prefix leaves a known abstract parent
                for orig_name in abstract_parents:
                    orig_fragment = _re.sub(r"^has", "", orig_name, count=1, flags=_re.IGNORECASE)
                    expected_qualified = (
                        f"has{cls_name}{orig_fragment[0].upper()}{orig_fragment[1:]}"
                        if orig_fragment else f"has{cls_name}"
                    )
                    if prop.local_name == expected_qualified:
                        qualified_to_abstract[prop.local_name] = orig_name
                        break

        # Abstract parent datatype properties (no domain — rdfs:subPropertyOf targets)
        for orig_name, parent_prop in abstract_parents.items():
            parent_uri = ns[orig_name]
            g.add((parent_uri, RDF.type, OWL.DatatypeProperty))
            g.add((parent_uri, RDFS.label, Literal(parent_prop.label, lang="en")))
            if parent_prop.comment:
                g.add((parent_uri, RDFS.comment, Literal(parent_prop.comment, lang="en")))
            g.add((parent_uri, RDFS.range, _XSD_RANGE_MAP.get(parent_prop.xsd_range, XSD.string)))
            # Intentionally no rdfs:domain — abstract parent is unconstrained

        # Datatype properties
        seen_local: set = set()
        for cls_name, props in class_props.items():
            for prop in props:
                # Deduplicate across classes
                if prop.local_name in seen_local:
                    continue
                seen_local.add(prop.local_name)
                uri = ns[prop.local_name]
                g.add((uri, RDF.type, OWL.DatatypeProperty))
                g.add((uri, RDFS.label, Literal(prop.label, lang="en")))
                if prop.comment:
                    g.add((uri, RDFS.comment, Literal(prop.comment, lang="en")))
                if cls_name in class_uris:
                    g.add((uri, RDFS.domain, class_uris[cls_name]))
                g.add((uri, RDFS.range, _XSD_RANGE_MAP.get(prop.xsd_range, XSD.string)))
                # Annotate with source column name so AnnotationMatcher scores 1.0
                # when this generated ontology is used as the mapping target.
                if prop.source_column:
                    col = prop.source_column.split(".")[-1]  # strip "table." prefix
                    g.add((uri, SKOS.altLabel, Literal(col, lang="en")))
                # rdfs:subPropertyOf link to abstract parent if this is a qualified generic
                if prop.local_name in qualified_to_abstract:
                    parent_uri = ns[qualified_to_abstract[prop.local_name]]
                    g.add((uri, RDFS.subPropertyOf, parent_uri))

        # Issue #43: write rdfs:subClassOf from inferred discriminator relationships
        for rel in structure.subclass_relationships:
            parent_uri = class_uris.get(rel.parent_class)
            if parent_uri is None:
                parent_uri = ns[rel.parent_class]
                g.add((parent_uri, RDF.type, OWL.Class))
                g.add((parent_uri, RDFS.label, Literal(rel.parent_class, lang="en")))
                class_uris[rel.parent_class] = parent_uri
            child_uri = class_uris.get(rel.child_class)
            if child_uri is None:
                child_uri = ns[rel.child_class]
                g.add((child_uri, RDF.type, OWL.Class))
                g.add((child_uri, RDFS.label, Literal(rel.child_class, lang="en")))
                class_uris[rel.child_class] = child_uri
            g.add((child_uri, RDFS.subClassOf, parent_uri))

        # Output path: <project>_full_<model-short>_<DDMONYY>.owl
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
        out_path = ontology_filename(out_dir, project, "full", gen_model)

        g.serialize(destination=str(out_path), format="xml")
        return out_path

    # ── LLM helper ────────────────────────────────────────────────────────────
    def _llm_json(self, prompt: str) -> dict:
        from semantic_mapper.core.llm import call_llm
        result = call_llm(prompt, self.settings, json_mode=True, temperature=0.1)
        self.prompt_tokens += result.prompt_tokens
        self.completion_tokens += result.completion_tokens
        return json.loads(result.content)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_fk_skip(profiles: List[TableProfile]) -> Dict[str, set]:
    """Build a mapping of table_name → set of FK column names to skip in pass 2."""
    table_name_set: set = set()
    for t in profiles:
        raw = _re.sub(r"^raw_", "", t.table_name, flags=_re.IGNORECASE).lower()
        table_name_set.add(raw)
        table_name_set.add(raw.rstrip("s"))

    skip: Dict[str, set] = {}
    for table in profiles:
        explicit_fks = {fk["column"] for fk in table.foreign_keys}
        inferred = set()
        for col_name in table.columns:
            if col_name in explicit_fks:
                continue
            cn_lo = col_name.lower()
            cn_base = _re.sub(r"_id$", "", cn_lo)
            if (cn_lo.endswith("_id") and cn_base in table_name_set) or \
               cn_lo in table_name_set or cn_lo.rstrip("s") in table_name_set:
                inferred.add(col_name)
        skip[table.table_name] = explicit_fks | inferred
    return skip


def _build_rich_summary(profiles: List[TableProfile], detail_level: str = "overview") -> str:
    """Build a schema summary richer than the seed version — includes row counts and FK markers."""
    table_name_set: set = set()
    for t in profiles:
        raw = _re.sub(r"^raw_", "", t.table_name, flags=_re.IGNORECASE).lower()
        table_name_set.add(raw)
        table_name_set.add(raw.rstrip("s"))

    lines: list = []
    for table in profiles:
        fk_map = {fk["column"]: fk["ref_table"] for fk in table.foreign_keys}
        lines.append(f"\nTABLE: {table.table_name}  ({table.row_count:,} rows)")
        for col_name, col in table.columns.items():
            flags = []
            if col.is_primary_key:
                flags.append("PK")
            if col_name in fk_map:
                flags.append(f"FK→{fk_map[col_name]}")
            elif not col.is_primary_key:
                cn_lo = col_name.lower()
                cn_base = _re.sub(r"_id$", "", cn_lo)
                if (cn_lo.endswith("_id") and cn_base in table_name_set) or \
                   cn_lo in table_name_set or cn_lo.rstrip("s") in table_name_set:
                    lines_ref = next(
                        (t.table_name for t in profiles
                         if _re.sub(r"^raw_", "", t.table_name, flags=_re.IGNORECASE).lower().rstrip("s")
                         in {cn_lo.rstrip("s"), cn_base}),
                        col_name,
                    )
                    flags.append(f"INFERRED_FK→{lines_ref}")
            if col.is_nullable:
                flags.append("nullable")
            flag_str = f"  [{', '.join(flags)}]" if flags else ""
            type_str = col.inferred_type.split("(")[0].upper()
            line = f"  {col_name}  {type_str}{flag_str}"
            if col.top_values:
                vals = ", ".join(str(v["value"]) for v in col.top_values[:3])
                line += f"  values=[{vals}]"
            lines.append(line)
    return "\n".join(lines)


def _detect_categoricals(
    profiles: List[TableProfile],
    fk_skip: Dict[str, set],
    threshold: int,
) -> Dict[str, Dict[str, list]]:
    """Return {table_name: {col_name: top_values}} for columns that should be
    modelled as owl:Class enumerations rather than xsd:string DatatypeProperties.

    Eligibility criteria (all must hold):
    - Not a primary key
    - Not a FK column (already handled as ObjectProperty)
    - distinct_count ≤ threshold
    - top_values is non-empty (profiler populated it)

    Nullable columns are included — null simply means the property is not asserted
    for that individual, which is compatible with an owl:oneOf enumeration class.
    """
    result: Dict[str, Dict[str, list]] = {}
    for table in profiles:
        skip = fk_skip.get(table.table_name, set())
        for col_name, col in table.columns.items():
            if col.is_primary_key:
                continue
            if col_name in skip:
                continue
            if not col.top_values:
                continue
            if col.distinct_count is None or col.distinct_count > threshold:
                continue
            result.setdefault(table.table_name, {})[col_name] = col.top_values
    return result


def _fix_subclass_parents(structure: _StructureSpec) -> _StructureSpec:
    """Correct subclass_relationships where the LLM attached a child class to the
    wrong parent.

    Rule: for each subclass_relationship, the discriminator_column is
    "table_name.column_name".  The correct parent_class is whichever class in
    structure.classes has that table_name in its source_tables list.  If the LLM
    picked a different parent, replace it and log a warning.

    If no class claims the discriminator table, the relationship is left unchanged
    (can't do better without guessing).
    """
    # Build table_name → class local_name index from LLM source_tables declarations
    table_to_class: Dict[str, str] = {}
    for cls in structure.classes:
        for tbl in cls.source_tables:
            table_to_class[tbl.lower()] = cls.local_name

    fixed: List[_SubclassRelation] = []
    for rel in structure.subclass_relationships:
        discriminator_table = rel.discriminator_column.split(".")[0].lower()
        correct_parent = table_to_class.get(discriminator_table)
        if correct_parent and correct_parent != rel.parent_class:
            logger.warning(
                "_fix_subclass_parents: %s subClassOf %s → corrected to %s "
                "(discriminator from %s, not %s's table)",
                rel.child_class, rel.parent_class, correct_parent,
                discriminator_table, rel.parent_class,
            )
            fixed.append(rel.model_copy(update={"parent_class": correct_parent}))
        else:
            fixed.append(rel)

    return structure.model_copy(update={"subclass_relationships": fixed})


def _supplement_obj_props(
    structure: _StructureSpec,
    profiles: List[TableProfile],
) -> _StructureSpec:
    """Fill in object properties that the LLM silently deduplicated.

    When multiple tables share the same FK target (e.g. conditions, medications,
    and procedures all have patient_id → patients) the LLM often emits only one
    hasPatient entry instead of one per source class.  This function reads the
    FK data directly from the profiles and adds any missing (domain, range) pairs
    so that _qualify_obj_props can then build the full hierarchy.
    """
    # Build table → class mapping from the LLM's source_tables declarations
    table_to_class: Dict[str, str] = {}
    for cls in structure.classes:
        for tbl in cls.source_tables:
            table_to_class[tbl.lower()] = cls.local_name

    # Fuzzy fallback: class name ≈ table name (strip raw_ prefix, strip plural s)
    cls_by_name: Dict[str, str] = {}
    cls_by_singular: Dict[str, str] = {}
    for cls in structure.classes:
        lo = cls.local_name.lower()
        cls_by_name[lo] = cls.local_name
        cls_by_singular[lo.rstrip("s")] = cls.local_name

    def _resolve(table_name: str) -> Optional[str]:
        key = _re.sub(r"^raw_", "", table_name, flags=_re.IGNORECASE).lower()
        return (
            table_to_class.get(table_name.lower())
            or table_to_class.get(key)
            or cls_by_name.get(key)
            or cls_by_singular.get(key.rstrip("s"))
        )

    # Build the table-name lookup used by _infer_relationships
    table_name_set: set = set()
    table_map: Dict[str, str] = {}
    for t in profiles:
        raw = _re.sub(r"^raw_", "", t.table_name, flags=_re.IGNORECASE).lower()
        table_name_set.add(raw)
        table_name_set.add(raw.rstrip("s"))
        table_map[raw] = t.table_name
        table_map[raw.rstrip("s")] = t.table_name

    # Mutable copy — we may drop LLM-generated properties where the FK runs the other way.
    # When the LLM generates hasCondition (Patient→Condition) but the actual FK is
    # conditions.patient_id → patients (Condition→Patient), we remove hasCondition and
    # replace it with hasPatientForCondition + isPatientOfCondition so the systematic
    # is{Shared}Of* hierarchy is complete and consistent.
    result_props: List[_StructObjectProp] = list(structure.object_properties)
    existing_pairs: set = {(p.domain, p.range) for p in result_props}
    new_props: List[_StructObjectProp] = []
    changed = False

    def _add_fk_prop(src: str, tgt: str, source_fk: str) -> None:
        nonlocal changed
        if (src, tgt) in existing_pairs:
            return  # already present in FK direction — nothing to do
        if (tgt, src) in existing_pairs:
            # LLM captured the reverse direction — remove it so the FK direction wins
            nonlocal result_props
            result_props = [p for p in result_props
                            if not (p.domain == tgt and p.range == src)]
            existing_pairs.discard((tgt, src))
            changed = True
            logger.info(
                "Replacing LLM property (%s→%s) with FK-direction (%s→%s) "
                "for systematic is*Of naming",
                tgt, src, src, tgt,
            )
        new_props.append(_StructObjectProp(
            local_name=f"has{tgt}",
            label=f"Has {tgt}",
            comment=f"Links {src} to its associated {tgt}.",
            domain=src, range=tgt,
            source_fk=source_fk,
            inverse_local_name=f"is{tgt}Of",
            inverse_label=f"Is {tgt} of",
        ))
        existing_pairs.add((src, tgt))

    for table in profiles:
        src_cls = _resolve(table.table_name)
        if not src_cls:
            continue

        # Explicit FKs
        for fk in table.foreign_keys:
            ref_table = fk.get("ref_table", "")
            tgt_cls = _resolve(ref_table)
            if not tgt_cls:
                continue
            _add_fk_prop(src_cls, tgt_cls,
                         f"{table.table_name}.{fk['column']} -> {ref_table}")

        # Inferred FKs (same logic as _infer_relationships)
        explicit_fk_cols = {fk["column"] for fk in table.foreign_keys}
        for col_name in table.columns:
            if col_name in explicit_fk_cols:
                continue
            cn_lo = col_name.lower()
            cn_base = _re.sub(r"_id$", "", cn_lo)
            ref_table = None
            if cn_lo.endswith("_id") and cn_base in table_name_set:
                ref_table = table_map.get(cn_base)
            elif cn_lo in table_name_set or cn_lo.rstrip("s") in table_name_set:
                ref_table = table_map.get(cn_lo) or table_map.get(cn_lo.rstrip("s"))
            if not ref_table:
                continue
            tgt_cls = _resolve(ref_table)
            if not tgt_cls:
                continue
            _add_fk_prop(src_cls, tgt_cls,
                         f"{table.table_name}.{col_name} -> {ref_table}")

    if not new_props and not changed:
        return structure

    logger.info(
        "Supplemented %d missing object properties from FK data", len(new_props)
    )
    return structure.model_copy(update={
        "object_properties": result_props + new_props,
    })


def _qualify_obj_props(
    props: List[_StructObjectProp],
) -> tuple[List[_StructObjectProp], List[_StructObjectProp], List[_StructObjectProp]]:
    """Issue #44: qualify object properties when ≥2 source classes share the same range.

    If only one class points at a given target (e.g. only Encounter→Medication) the
    property keeps its simple name (``hasMedication``).  When two or more classes point
    at the same target (e.g. Encounter→Medication AND Patient→Medication) each is
    renamed ``has{Range}For{Domain}`` (e.g. ``hasMedicationForEncounter``) and an
    abstract forward parent ``has{Range}`` (no domain) plus an abstract inverse parent
    ``is{Range}Of`` (domain={Range}) are created so that SPARQL/RDFS queries on either
    abstract property match all subproperties via reasoning.

    Inverses are renamed ``is{Range}Of{Domain}`` and declared
    ``rdfs:subPropertyOf is{Range}Of``.

    Returns ``(qualified_props, abstract_fwd_parents, abstract_inv_parents)``.
    """
    # Group forward properties by range class
    by_range: Dict[str, List[_StructObjectProp]] = {}
    for prop in props:
        by_range.setdefault(prop.range, []).append(prop)

    qualified: List[_StructObjectProp] = []
    abstract_fwd: List[_StructObjectProp] = []
    abstract_inv: List[_StructObjectProp] = []

    for range_cls, group in by_range.items():
        if len(group) == 1:
            # Single source — simple name is unambiguous, keep as-is
            qualified.append(group[0])
            continue

        # Multiple sources → qualify each and create abstract forward + inverse parents
        rep = group[0]
        fwd_name = f"has{range_cls}"
        inv_name = f"is{range_cls}Of"
        readable_range = _re.sub(r"([A-Z])", r" \1", range_cls).strip().lower()

        abstract_fwd.append(_StructObjectProp(
            local_name=fwd_name,
            label=range_cls,
            comment=(
                f"Abstract parent property for all class-specific {readable_range} "
                f"object properties (e.g. {fwd_name}For{rep.domain}). "
                f"SPARQL queries on {fwd_name} match all rdfs:subPropertyOf subproperties "
                f"via RDFS reasoning. Inverse: {inv_name}."
            ),
            domain="", range=range_cls,
            source_fk="",
            inverse_local_name=inv_name, inverse_label=f"Is {readable_range} of",
        ))
        abstract_inv.append(_StructObjectProp(
            local_name=inv_name,
            label=f"Is {readable_range} of",
            comment=(
                f"Abstract inverse parent property for all class-specific {inv_name} "
                f"subproperties (e.g. {inv_name}{rep.domain}). "
                f"Inverse of {fwd_name}."
            ),
            domain=range_cls, range="",
            source_fk="",
            inverse_local_name=fwd_name, inverse_label=range_cls,
        ))

        for prop in group:
            qualified_fwd = f"has{range_cls}For{prop.domain}"
            qualified_inv = f"is{range_cls}Of{prop.domain}" if prop.inverse_local_name else ""
            inv_label = f"Is {readable_range} of {prop.domain}" if qualified_inv else ""
            qualified.append(prop.model_copy(update={
                "local_name": qualified_fwd,
                "inverse_local_name": qualified_inv,
                "inverse_label": inv_label,
            }))

    return qualified, abstract_fwd, abstract_inv


def _qualify_generic_props(
    class_props: Dict[str, List[_DataProp]],
) -> tuple[Dict[str, List[_DataProp]], Dict[str, _DataProp]]:
    """Issue #42: prefix generic property local names with their class name.

    Returns ``(qualified_class_props, abstract_parents)`` where
    ``abstract_parents`` maps the original (unqualified) local name to a
    representative _DataProp that can be written as a domain-free abstract
    parent property — enabling ``rdfs:subPropertyOf`` inheritance so that
    SPARQL/reasoner queries on e.g. ``hasStartTime`` also return triples
    asserted via ``hasEncounterStartTime`` or ``hasProcedureStartTime``.
    """
    result: Dict[str, List[_DataProp]] = {}
    # original_name → representative _DataProp for the abstract parent
    abstract_parents: Dict[str, _DataProp] = {}

    for cls_name, props in class_props.items():
        new_props: List[_DataProp] = []
        for prop in props:
            fragment = _re.sub(r"^has", "", prop.local_name, count=1, flags=_re.IGNORECASE)
            if fragment.lower() in _GENERIC_PROP_FRAGMENTS:
                original_name = prop.local_name
                qualified = (
                    f"has{cls_name}{fragment[0].upper()}{fragment[1:]}"
                    if fragment else f"has{cls_name}"
                )
                prop = prop.model_copy(update={"local_name": qualified})
                # Record the abstract parent once with a generic, class-neutral comment
                if original_name not in abstract_parents:
                    # Human-readable fragment: "StartTime" → "start time"
                    readable = _re.sub(r"([A-Z])", r" \1", fragment).strip().lower()
                    generic_comment = (
                        f"Abstract parent property for all class-specific {readable} "
                        f"datatype properties. Class-specific subproperties (e.g. "
                        f"{qualified}) are declared rdfs:subPropertyOf this property; "
                        f"SPARQL queries on {original_name} will match all subproperties "
                        f"via RDFS reasoning."
                    )
                    abstract_parents[original_name] = prop.model_copy(
                        update={"local_name": original_name, "comment": generic_comment}
                    )
            new_props.append(prop)
        result[cls_name] = new_props
    return result, abstract_parents


def _build_discriminator_section(profiles: List[TableProfile]) -> str:
    """Issue #43: build a prompt section listing discriminator columns for subclass inference.

    Columns are grouped under a TABLE header so the LLM cannot lose track of
    which table a discriminator belongs to.  The subclass parent MUST be the
    entity class whose source_tables includes that table — never a class from
    a different table.
    """
    table_blocks: List[str] = []
    for table in profiles:
        col_lines: List[str] = []
        for col_name, col in table.columns.items():
            if col.is_primary_key or col.is_nullable:
                continue
            if not col.top_values:
                continue
            # Discriminator heuristic: low-cardinality (≤ 10 distinct), all string values
            if col.distinct_count is None or col.distinct_count > 10:
                continue
            values = [str(v["value"]) for v in col.top_values]
            if all(isinstance(v["value"], str) for v in col.top_values):
                col_lines.append(
                    f"    {col_name}  distinct={col.distinct_count}"
                    f"  values=[{', '.join(values)}]"
                )
        if col_lines:
            table_blocks.append(
                f"  TABLE: {table.table_name}\n" + "\n".join(col_lines)
            )
    if not table_blocks:
        return ""
    return (
        "\n\nDISCRIMINATOR COLUMNS (subclasses MUST be attached to the entity class"
        " whose source_tables contains the TABLE listed — never to a class from a"
        " different table):\n"
        + "\n".join(table_blocks)
    )


def _infer_relationships(profiles: List[TableProfile]) -> List[str]:
    """Return human-readable inferred-FK relationship strings for the prompt."""
    table_name_set: set = set()
    table_map: dict = {}
    for t in profiles:
        raw = _re.sub(r"^raw_", "", t.table_name, flags=_re.IGNORECASE).lower()
        table_name_set.add(raw)
        table_name_set.add(raw.rstrip("s"))
        table_map[raw] = t.table_name
        table_map[raw.rstrip("s")] = t.table_name

    rels: list = []
    seen: set = set()
    for table in profiles:
        explicit_fk_cols = {fk["column"] for fk in table.foreign_keys}
        for col_name, col in table.columns.items():
            if col.is_primary_key or col_name in explicit_fk_cols:
                continue
            cn_lo = col_name.lower()
            cn_base = _re.sub(r"_id$", "", cn_lo)
            matched_ref = None
            if cn_lo.endswith("_id") and cn_base in table_name_set:
                matched_ref = table_map.get(cn_base, cn_base)
            elif cn_lo in table_name_set or cn_lo.rstrip("s") in table_name_set:
                matched_ref = table_map.get(cn_lo) or table_map.get(cn_lo.rstrip("s"), cn_lo)
            if matched_ref:
                key = (table.table_name, col_name, matched_ref)
                if key not in seen:
                    seen.add(key)
                    rels.append(
                        f"{table.table_name}.{col_name} → {matched_ref}"
                    )
    return rels
