# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Seed ontology generator.

Analyses a profiled database schema using an LLM and produces a starter
OWL/RDF ontology file that can be loaded in Protégé for refinement and
used as the mapping target for the pipeline.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from pydantic import BaseModel, Field
from rdflib import Graph, Namespace, URIRef, Literal
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

from semantic_mapper.config.settings import Settings
from semantic_mapper.db.models import TableProfile

logger = logging.getLogger(__name__)

# ── Valid XSD range types ─────────────────────────────────────────────────────
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


# ── LLM response models ───────────────────────────────────────────────────────
class _OntologyClass(BaseModel):
    local_name: str
    label: str
    comment: str
    source_tables: List[str] = Field(default_factory=list)


class _DatatypeProperty(BaseModel):
    local_name: str
    label: str
    comment: str
    domain: str          # local_name of the owning class
    xsd_range: str       # one of _XSD_RANGE_MAP keys
    source_columns: List[str] = Field(default_factory=list)


class _ObjectProperty(BaseModel):
    local_name: str
    label: str
    comment: str
    domain: str
    range: str
    source_fk: str = ""


class _SeedOntologySpec(BaseModel):
    reasoning: str
    base_iri: str
    classes: List[_OntologyClass]
    datatype_properties: List[_DatatypeProperty]
    object_properties: List[_ObjectProperty]


# ── Generator ─────────────────────────────────────────────────────────────────
class SeedGenerator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.prompt_tokens: int = 0
        self.completion_tokens: int = 0

    def generate(self, profiles: List[TableProfile]) -> Path:
        """Generate a seed OWL file from table profiles. Returns path to written file."""
        spec = self._call_llm(profiles)
        path = self._write_owl(spec)
        logger.info("Seed ontology written to %s", path)
        return path

    # ── LLM call ──────────────────────────────────────────────────────────────
    def _call_llm(self, profiles: List[TableProfile]) -> _SeedOntologySpec:
        from semantic_mapper.core.llm import call_llm

        db_name = self.settings.DATABASE_URL.split("/")[-1]
        schema_summary = _build_schema_summary(profiles)
        inferred_rels = _infer_relationships(profiles)
        inferred_section = (
            "\nINFERRED RELATIONSHIPS (no DB-level FK constraint but column name matches a table):\n"
            + "\n".join(f"  {r}" for r in inferred_rels)
            if inferred_rels else ""
        )

        prompt = f"""You are an ontology engineer. Analyse the following relational database schema \
and design a starter OWL ontology that semantically models the domain.

DATABASE: {db_name}

SCHEMA:
{schema_summary}{inferred_section}

TASK:
1. Reason about the domain — identify what real-world entities, events, and relationships the tables represent.
2. Propose owl:Class for each distinct entity type.
3. Propose owl:DatatypeProperty for each column that describes an attribute of an entity.
   Skip primary key columns (they become entity identifiers).
   Skip FK columns (both explicit FK→ and inferred INFERRED_FK columns) — these become object properties.
4. Propose owl:ObjectProperty for EVERY FK relationship — both explicit (FK→) and inferred (INFERRED_FK).
   Every row in "INFERRED RELATIONSHIPS" above MUST produce an owl:ObjectProperty.

RULES:
- local_name must be a valid URI fragment: CamelCase for classes, camelCase prefixed with "has" for properties.
- xsd_range must be exactly one of: string, integer, decimal, float, double, boolean, date, dateTime, anyURI.
- base_iri should be an HTTP IRI derived from the database name, e.g. "http://example.com/{db_name}/".
- reasoning must briefly explain your entity-boundary decisions.

Return valid JSON matching this structure exactly:
{{
  "reasoning": "...",
  "base_iri": "http://...",
  "classes": [{{"local_name": "...", "label": "...", "comment": "...", "source_tables": ["..."]}}],
  "datatype_properties": [{{"local_name": "...", "label": "...", "comment": "...", "domain": "ClassName", "xsd_range": "string", "source_columns": ["table.column"]}}],
  "object_properties": [{{"local_name": "...", "label": "...", "comment": "...", "domain": "ClassName", "range": "ClassName", "source_fk": "table.fk_col -> ref_table.id"}}]
}}"""

        result = call_llm(prompt, self.settings, json_mode=True, temperature=0.2)
        self.prompt_tokens = result.prompt_tokens
        self.completion_tokens = result.completion_tokens
        raw = json.loads(result.content)
        spec = _SeedOntologySpec.model_validate(raw)

        # Sanitise: replace unknown xsd_range values with "string"
        for prop in spec.datatype_properties:
            if prop.xsd_range not in _XSD_RANGE_MAP:
                logger.warning(
                    "Unknown xsd_range '%s' on property '%s' — defaulting to string",
                    prop.xsd_range, prop.local_name,
                )
                prop.xsd_range = "string"

        logger.info(
            "Seed spec: %d classes, %d datatype props, %d object props",
            len(spec.classes), len(spec.datatype_properties), len(spec.object_properties),
        )
        return spec

    # ── OWL writer ────────────────────────────────────────────────────────────
    def _write_owl(self, spec: _SeedOntologySpec) -> Path:
        # Strip any trailing # or / before normalising to a single trailing /
        # (LLMs occasionally return "http://example.com/db#" which would produce
        #  malformed "#/" URIs for all properties.)
        base = spec.base_iri.rstrip("/#") + "/"
        ns = Namespace(base)
        g = Graph()
        g.bind("", ns)
        g.bind("owl", OWL)
        g.bind("rdfs", RDFS)
        g.bind("skos", SKOS)
        g.bind("xsd", XSD)

        # Ontology declaration
        g.add((URIRef(base.rstrip("/")), RDF.type, OWL.Ontology))

        # Classes
        class_uris: Dict[str, URIRef] = {}
        for cls in spec.classes:
            uri = ns[cls.local_name]
            class_uris[cls.local_name] = uri
            g.add((uri, RDF.type, OWL.Class))
            g.add((uri, RDFS.label, Literal(cls.label, lang="en")))
            if cls.comment:
                g.add((uri, RDFS.comment, Literal(cls.comment, lang="en")))

        # Datatype properties
        for prop in spec.datatype_properties:
            uri = ns[prop.local_name]
            g.add((uri, RDF.type, OWL.DatatypeProperty))
            g.add((uri, RDFS.label, Literal(prop.label, lang="en")))
            if prop.comment:
                g.add((uri, RDFS.comment, Literal(prop.comment, lang="en")))
            if prop.domain in class_uris:
                g.add((uri, RDFS.domain, class_uris[prop.domain]))
            g.add((uri, RDFS.range, _XSD_RANGE_MAP.get(prop.xsd_range, XSD.string)))
            # Annotate with source column name so AnnotationMatcher scores 1.0
            # when this generated ontology is used as the mapping target.
            for src in prop.source_columns:
                col = src.split(".")[-1]  # strip "table." prefix
                g.add((uri, SKOS.altLabel, Literal(col, lang="en")))

        # Object properties
        for prop in spec.object_properties:
            uri = ns[prop.local_name]
            g.add((uri, RDF.type, OWL.ObjectProperty))
            g.add((uri, RDFS.label, Literal(prop.label, lang="en")))
            if prop.comment:
                g.add((uri, RDFS.comment, Literal(prop.comment, lang="en")))
            if prop.domain in class_uris:
                g.add((uri, RDFS.domain, class_uris[prop.domain]))
            if prop.range in class_uris:
                g.add((uri, RDFS.range, class_uris[prop.range]))

        # Build output path using naming convention:
        # {dbshort}_gen_{yymmdd}_{ddd}.owl  (ddd = daily sequence 001–999)
        out_dir = Path(self.settings.SEED_OUTPUT_PATH).parent
        out_dir.mkdir(parents=True, exist_ok=True)

        raw_db = self.settings.DATABASE_URL.rstrip("/").rsplit("/", 1)[-1]
        # Strip leading "bronze_" or "bronze-" prefix (case-insensitive)
        import re as _re
        db_short = _re.sub(r"^bronze[-_]", "", raw_db, flags=_re.IGNORECASE) or raw_db

        today = datetime.now().strftime("%y%m%d")
        pattern = _re.compile(
            rf"^{_re.escape(db_short)}_gen_{_re.escape(today)}_(\d{{3}})\.owl$"
        )
        existing_seqs = [
            int(m.group(1))
            for f in out_dir.iterdir()
            if f.is_file() and (m := pattern.match(f.name))
        ]
        seq = max(existing_seqs, default=0) + 1
        out_path = out_dir / f"{db_short}_gen_{today}_{seq:03d}.owl"

        g.serialize(destination=str(out_path), format="xml")
        return out_path


# ── Schema summary builder (pure function, testable) ─────────────────────────
def _build_schema_summary(profiles: List[TableProfile]) -> str:
    import re as _re
    # Build a set of normalised table names for inferred-FK detection
    table_name_set: set = set()
    for t in profiles:
        raw = _re.sub(r"^raw_", "", t.table_name, flags=_re.IGNORECASE).lower()
        table_name_set.add(raw)
        table_name_set.add(raw.rstrip("s"))  # naive deplural

    lines = []
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
                # Infer FK from column name matching a table name
                cn_lo = col_name.lower()
                # strip _id suffix for matching
                cn_base = _re.sub(r"_id$", "", cn_lo)
                if cn_lo.endswith("_id") and cn_base in table_name_set:
                    # find the actual table name
                    ref = next(
                        (t.table_name for t in profiles
                         if _re.sub(r"^raw_", "", t.table_name, flags=_re.IGNORECASE).lower().rstrip("s") == cn_base
                         or _re.sub(r"^raw_", "", t.table_name, flags=_re.IGNORECASE).lower() == cn_base),
                        cn_base,
                    )
                    flags.append(f"INFERRED_FK→{ref}")
                elif cn_lo in table_name_set or cn_lo.rstrip("s") in table_name_set:
                    ref = next(
                        (t.table_name for t in profiles
                         if _re.sub(r"^raw_", "", t.table_name, flags=_re.IGNORECASE).lower().rstrip("s") == cn_lo.rstrip("s")
                         or _re.sub(r"^raw_", "", t.table_name, flags=_re.IGNORECASE).lower() == cn_lo),
                        cn_lo,
                    )
                    flags.append(f"INFERRED_FK→{ref}")
            if col.is_nullable:
                flags.append("nullable")
            flag_str = f"  [{', '.join(flags)}]" if flags else ""
            type_str = col.inferred_type.split("(")[0].upper()
            line = f"  {col_name}  {type_str}{flag_str}"
            if col.top_values:
                vals = ", ".join(str(v["value"]) for v in col.top_values[:5])
                line += f"  values=[{vals}]"
            lines.append(line)
    return "\n".join(lines)


def _infer_relationships(profiles: List[TableProfile]) -> List[str]:
    """Return human-readable relationship strings for the prompt's INFERRED RELATIONSHIPS section."""
    import re as _re
    table_name_set: set = set()
    table_map: dict = {}  # normalised name → actual table name
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
                    rels.append(f"{table.table_name}.{col_name} → {matched_ref} (column '{col_name}' names a table)")
    return rels
