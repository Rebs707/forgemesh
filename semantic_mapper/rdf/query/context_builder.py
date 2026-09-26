# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import Dict, List, Optional

from rdflib import OWL, RDF, RDFS, Graph

from semantic_mapper.gold.models import GoldTableDefinition


def _uri_fragment(uri: str) -> str:
    if "#" in uri:
        return uri.split("#")[-1]
    return uri.split("/")[-1]


def _short(uri: str, ont_ns: str) -> str:
    """Return 'ont:LocalName' if uri starts with ont_ns, else '<uri>'."""
    if ont_ns and uri.startswith(ont_ns):
        return f"ont:{uri[len(ont_ns):]}"
    return f"<{uri}>"


def _source_to_xsd_label(source_data_type: str) -> str:
    """Return an xsd: label for a SQL source data type (no value required)."""
    upper = source_data_type.upper()
    if any(upper.startswith(t) for t in ("INT", "BIGINT", "SMALLINT", "TINYINT")):
        return "xsd:integer"
    if any(upper.startswith(t) for t in ("DECIMAL", "NUMERIC")):
        return "xsd:decimal"
    if any(upper.startswith(t) for t in ("FLOAT", "DOUBLE")):
        return "xsd:double"
    if any(upper.startswith(t) for t in ("BOOLEAN", "BIT")):
        return "xsd:boolean"
    if upper.startswith("DATE") and not upper.startswith("DATETIME"):
        return "xsd:date"
    if any(upper.startswith(t) for t in ("DATETIME", "TIMESTAMP")):
        return "xsd:dateTime"
    return "xsd:string"


def _build_prop_range(ontology_graph: Graph) -> Dict[str, str]:
    """Return {property_uri: range_class_uri} for all OWL object properties."""
    result: Dict[str, str] = {}
    for prop in ontology_graph.subjects(RDF.type, OWL.ObjectProperty):
        range_ = next(ontology_graph.objects(prop, RDFS.range), None)
        if range_:
            result[str(prop)] = str(range_)
    return result


def _build_all_object_props(ontology_graph: Graph) -> set:
    """Return URIs of all owl:ObjectProperty declarations in the ontology."""
    return {str(prop) for prop in ontology_graph.subjects(RDF.type, OWL.ObjectProperty)}


# Property local-name fragments that are good candidates for human-readable display.
# Ordered by descending preference so the first match wins when building DISPLAY FIELDS.
_DISPLAY_PRIORITY_FRAGMENTS: List[str] = [
    "name", "firstname", "lastname", "fullname", "title", "label",
    "description",
    "date", "birthdate", "startdate", "stopdate", "datetime",
    "class", "type", "status", "gender", "race", "ethnicity",
    "code",
    "cost", "coverage",
    "city", "state", "address", "zip", "county",
    "identifier", "ssn",
]

_MAX_DISPLAY_FIELDS = 4


def _pick_display_fields(
    column_specs: list,
    entity_map: Dict[str, str],
    gold_table_name: str,
    prop_range: Optional[Dict[str, str]] = None,
    class_to_entity: Optional[Dict[str, str]] = None,
    id_column: Optional[str] = None,
) -> List[tuple]:  # [(var_name, prop_uri)]
    """Return up to _MAX_DISPLAY_FIELDS (varName, propURI) pairs for display hints.

    Prefers properties whose local fragment matches a human-readable keyword.
    Falls back to the first data properties if nothing matches.
    Excludes object properties (FK links to other entities) — they return URIs,
    not human-readable values, and listing them as display fields confuses the LLM
    into treating them as value-join variables.
    Excludes the id_column property — its value is encoded in the subject URI and
    showing it as a separate data property misleads the LLM into value-join misuse.
    """
    entity_short = (
        gold_table_name[len("gold_"):] if gold_table_name.startswith("gold_") else gold_table_name
    )
    data_specs = [
        s for s in column_specs
        # Exclude path 1 FKs: column name matches another entity short name
        if not (entity_map.get(s.gold_column_name) and
                entity_map.get(s.gold_column_name) != gold_table_name)
        # Exclude path 2 FKs: property's rdfs:range is another entity's class
        and not (
            prop_range and class_to_entity and
            class_to_entity.get(prop_range.get(s.property_uri, ""), "") not in ("", entity_short)
        )
        # Exclude the id_column property — its value is already in the subject URI
        and not (id_column and s.gold_column_name == id_column)
    ]

    def _rank(spec) -> int:
        frag = _uri_fragment(spec.property_uri).lower().replace("has", "").replace("_", "")
        for i, kw in enumerate(_DISPLAY_PRIORITY_FRAGMENTS):
            if kw in frag:
                return i
        return len(_DISPLAY_PRIORITY_FRAGMENTS)

    ranked = sorted(data_specs, key=_rank)
    result = []
    for spec in ranked[:_MAX_DISPLAY_FIELDS]:
        frag = _uri_fragment(spec.property_uri)
        # camelCase var name: strip "has" prefix, lowercase first char
        var = frag[3:] if frag.startswith("has") or frag.startswith("Has") else frag
        var = var[0].lower() + var[1:] if var else frag
        result.append((var, spec.property_uri))
    return result


def _detect_ontology_namespace(definitions: List[GoldTableDefinition]) -> Optional[str]:
    """Return the common namespace prefix of property URIs across all definitions.

    Handles both '#'-based (http://example.org/ont#) and '/'-based
    (http://example.org/ont/) ontology namespaces.
    """
    for defn in definitions:
        for spec in defn.column_specs:
            uri = spec.property_uri
            if not uri:
                continue
            if "#" in uri:
                return uri.rsplit("#", 1)[0] + "#"
            parts = uri.rsplit("/", 1)
            if len(parts) == 2 and parts[0].startswith("http"):
                return parts[0] + "/"
    return None


def _build_value_join_hints(definitions: List[GoldTableDefinition]) -> List[str]:
    """Detect properties shared across 2+ entity types and emit explicit join patterns.

    These are value-matching joins (shared SPARQL variable between two triple patterns),
    the primary join mechanism when FK columns are not modelled as OWL object properties.
    """
    from collections import defaultdict

    # prop_uri → ordered-dedup list of entity short names
    prop_to_entities: Dict[str, List[str]] = defaultdict(list)
    for defn in definitions:
        entity_short = (
            defn.gold_table_name[len("gold_"):]
            if defn.gold_table_name.startswith("gold_")
            else defn.gold_table_name
        )
        for spec in defn.column_specs:
            if entity_short not in prop_to_entities[spec.property_uri]:
                prop_to_entities[spec.property_uri].append(entity_short)

    lines: List[str] = []
    for prop_uri, entities in sorted(prop_to_entities.items()):
        if len(entities) < 2:
            continue
        frag = _uri_fragment(prop_uri).lower()
        # Only emit for properties that are plausible domain join keys.
        # Exclude generic row-level IDs (itemid, rowid) which appear on every table
        # but don't represent meaningful cross-entity relationships.
        if not any(kw in frag for kw in ("id", "code", "ref", "num", "key")):
            continue
        excluded = {"itemid", "rowid", "item_id", "row_id"}
        if any(frag.endswith(e) or frag == f"has{e}" for e in excluded):
            continue
        shared_var = frag[3:] if frag.startswith("has") else frag  # strip "has"
        lines.append(f"  <{prop_uri}>  shared by: {', '.join(entities)}")
        lines.append(f"    Value-join pattern (share ?{shared_var} across patterns):")
        for entity in entities:
            lines.append(f"      ?{entity}URI <{prop_uri}> ?{shared_var} .")
        lines.append("")

    return lines


def build_sparql_schema_context(
    definitions: List[GoldTableDefinition],
    named_graph: str,
    base_uri: str,
    ontology_graph: Optional[Graph] = None,
) -> str:
    """Build a SPARQL-oriented schema context string for the LLM system prompt.

    Each gold table definition becomes a CLASS block showing:
    - The OWL class URI and entity short-name
    - DISPLAY FIELDS: suggested human-readable properties to SELECT
    - Each property URI with its XSD datatype or object-property join pattern

    When ontology_graph is supplied, object-property join patterns reflect the
    correct triple direction based on rdfs:range declarations, matching how
    TripleBuilder emits triples.  This generalises to any domain.
    """
    # Build entity_map: short name → gold_table_name
    entity_map: Dict[str, str] = {
        d.gold_table_name[len("gold_"):]: d.gold_table_name
        for d in definitions
        if d.gold_table_name.startswith("gold_")
    }

    # property URI → range class URI (from ontology)
    prop_range: Dict[str, str] = (
        _build_prop_range(ontology_graph) if ontology_graph is not None else {}
    )
    # All OWL ObjectProperty URIs — used to detect terminal object properties
    # (those whose range class has no corresponding gold table).
    all_object_props: set = (
        _build_all_object_props(ontology_graph) if ontology_graph is not None else set()
    )

    ont_ns = _detect_ontology_namespace(definitions)

    lines: List[str] = [
        f"NAMED GRAPH: <{named_graph}>",
        "",
        "PREFIXES (use these exactly — do NOT invent others):",
        f"  PREFIX ont: <{ont_ns}>  -- for all class and property URIs" if ont_ns
        else "  (no ontology namespace detected — use full URIs in angle brackets)",
        f"  PREFIX data: <{base_uri}>  -- for individual subject URIs only (rarely needed in SELECT)",
        "",
    ]

    # Reverse: primary_class_uri → entity short name (for range-based FK detection)
    class_to_entity: Dict[str, str] = {
        d.primary_class_uri: d.gold_table_name[len("gold_"):]
        for d in definitions
        if d.gold_table_name.startswith("gold_") and d.primary_class_uri
    }

    for definition in definitions:
        entity_short = (
            definition.gold_table_name[len("gold_"):]
            if definition.gold_table_name.startswith("gold_")
            else definition.gold_table_name
        )
        class_uri = definition.primary_class_uri
        disc = getattr(definition, "discriminated_class_uris", [])

        class_short = _short(class_uri, ont_ns)
        lines.append(f"CLASS {class_short}  (entity: {entity_short})")

        if disc:
            sample = disc[:3]
            lines.append(
                f"  NOTE: {class_short} has {len(disc)} discriminated subtypes (not stored directly)."
            )
            lines.append(
                f"  Subtypes (sample of {len(sample)}/{len(disc)}): "
                + ", ".join(_short(d, ont_ns) for d in sample)
                + (" …" if len(disc) > len(sample) else "")
            )
            lines.append(
                f"  IMPORTANT: To query ALL {entity_short} instances without listing every subtype,"
            )
            lines.append(
                f"  use the data properties directly — ?{entity_short}URI will be bound via its properties."
            )
            lines.append(
                f"  Only add a ?{entity_short}URI a <type> triple when you need to FILTER to a specific subtype."
            )
            lines.append(
                f"  Example (query all {entity_short} with a property, no type restriction needed):"
            )
            union_parts = " UNION ".join(
                f"{{ ?{entity_short}URI a {_short(d, ont_ns)} }}" for d in sample[:2]
            )
            lines.append(
                f"    {union_parts}  -- only when restricting to those two subtypes"
            )
        else:
            lines.append(
                f"  Use ?{entity_short}URI a {class_short} to select instances."
            )

        # ── Display fields hint ─────────────────────────────────────────
        display_fields = _pick_display_fields(
            definition.column_specs, entity_map, definition.gold_table_name,
            prop_range=prop_range, class_to_entity=class_to_entity,
            id_column=definition.id_column,
        )
        if display_fields:
            lines.append(f"  DISPLAY FIELDS (always SELECT these for {entity_short}):")
            for var, prop in display_fields:
                prop_short = _short(prop, ont_ns)
                lines.append(
                    f"    ?{entity_short}_{var}  ← ?{entity_short}URI {prop_short} ?{entity_short}_{var}"
                )

        lines.append("  Properties:")

        # Property URI for the id_column — its value is encoded in the subject URI;
        # showing it as a data property is misleading so skip it.
        id_col_prop_uri: Optional[str] = next(
            (s.property_uri for s in definition.column_specs
             if definition.id_column and s.gold_column_name == definition.id_column),
            None,
        )

        for spec in definition.column_specs:
            if id_col_prop_uri and spec.property_uri == id_col_prop_uri:
                continue  # skip — value is encoded in the subject URI

            col = spec.gold_column_name
            prop_uri = spec.property_uri
            prop_short = _short(prop_uri, ont_ns)
            referenced_table = entity_map.get(col)

            if referenced_table and referenced_table != definition.gold_table_name:
                # Object property path 1 — column name matches entity short name
                ref_short = (
                    referenced_table[len("gold_"):]
                    if referenced_table.startswith("gold_")
                    else referenced_table
                )
                ref_def = next(
                    (d for d in definitions if d.gold_table_name == referenced_table),
                    None,
                )
                ref_class_uri = (
                    ref_def.primary_class_uri
                    if ref_def
                    else f"(unknown class for {ref_short})"
                )
                ref_class_short = _short(ref_class_uri, ont_ns)

                lines.append(f"    {prop_short}  object property → {ref_short}")

                range_class = prop_range.get(prop_uri)
                if range_class and range_class == class_uri:
                    lines.append(
                        f"      (join: ?{ref_short}URI {prop_short} ?{entity_short}URI ."
                        f" ?{ref_short}URI a {ref_class_short})"
                    )
                else:
                    lines.append(
                        f"      (join: ?{entity_short}URI {prop_short} ?{ref_short}URI ."
                        f" ?{ref_short}URI a {ref_class_short})"
                    )
            else:
                # Object property path 2 — column name doesn't match entity short
                # name but the property's rdfs:range identifies the target entity.
                range_class = prop_range.get(prop_uri)
                target_entity = class_to_entity.get(range_class) if range_class else None
                if target_entity and target_entity != entity_short:
                    ref_def = next(
                        (d for d in definitions if d.gold_table_name == f"gold_{target_entity}"),
                        None,
                    )
                    ref_class_uri = ref_def.primary_class_uri if ref_def else range_class
                    ref_class_short = _short(ref_class_uri, ont_ns)
                    lines.append(f"    {prop_short}  object property → {target_entity}")
                    if range_class == class_uri:
                        lines.append(
                            f"      (join: ?{target_entity}URI {prop_short} ?{entity_short}URI ."
                            f" ?{target_entity}URI a {ref_class_short})"
                        )
                    else:
                        lines.append(
                            f"      (join: ?{entity_short}URI {prop_short} ?{target_entity}URI ."
                            f" ?{target_entity}URI a {ref_class_short})"
                        )
                else:
                    if prop_uri in all_object_props:
                        range_frag = _uri_fragment(range_class) if range_class else "NamedIndividual"
                        lines.append(
                            f"    {prop_short}  object property → {range_frag}"
                            f" (stores URI individual — filter with LIKE '%Fragment',"
                            f" never compare as string literal)"
                        )
                    else:
                        xsd_label = _source_to_xsd_label(spec.source_data_type)
                        lines.append(f"    {prop_short}  {xsd_label}")

        lines.append("")

    # Cross-entity value-join hints
    join_hints = _build_value_join_hints(definitions)
    if join_hints:
        lines.insert(
            lines.index("") + 1 if "" in lines else len(lines),
            "",
        )
        lines.append("VALUE-MATCHING JOINS (use shared variables to join entities without object properties):")
        lines.extend(join_hints)

    return "\n".join(lines).rstrip()
