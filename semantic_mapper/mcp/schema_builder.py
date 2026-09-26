# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 1 — build_agent_schema.

Produces a structured JSON schema dict from GoldTableDefinitions and the
mapping report.  The output is designed for agent-side query construction:
the agent calls get_schema() once per session, then constructs SPARQL or SQL
queries directly without requiring an LLM inside the MCP server.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "1.0.0"

# Distilled from the SPARQL system prompt — one concise rule per entry.
# The agent reads these alongside the class/property definitions.
_CONSTRUCTION_RULES: List[str] = [
    "Only generate SELECT queries — never INSERT, UPDATE, DELETE, CONSTRUCT, DESCRIBE, ASK, or any SPARQL update.",
    "Always wrap triple patterns in GRAPH <named_graph> { ... }. Replace 'named_graph' with the value in schema metadata.",
    "Always add LIMIT unless the user explicitly asks for all results.",
    "Use exact property URIs from the schema — never invent URIs not listed.",
    "Use only the PREFIX declarations in the namespaces block. The data: prefix is for individual subject URIs only — never use it for class or property URIs.",
    "Object property joins use the direction in join_patterns: ?fromURI <objectProperty> ?toURI. The variable binds to a URI, not a literal.",
    "JOIN VARIABLE REUSE: two triple patterns join because they share the same variable name. If ?fooURI is already bound, the join triple MUST reuse that exact name — not a new one. A renamed variable creates a cartesian product and produces wrong results.",
    "MULTI-HOP: after following an object property, the bound variable IS the linked entity (a URI). Access its data properties directly from that variable. Never treat it as a literal code.",
    "VALUE-MATCHING JOINS: when two entities share a property without an object property (e.g. a shared customer reference), join with a shared literal variable: ?aURI <prop> ?sharedVal . ?bURI <prop> ?sharedVal . See value_matching_guidance for available cross-entity value joins.",
    "Use FILTER for comparisons; use OPTIONAL for nullable properties.",
    "Only reference classes and properties listed in the schema. If required data is absent, return null SPARQL and explain.",
    "Never enumerate discriminated subtypes in UNION to match all instances — bind the entity by its data properties. Only add rdf:type when the question explicitly filters to a specific subtype.",
    "Categorical values are stored exactly as in source data. Use LCASE(STR(?var)) = LCASE('value') for case-insensitive filtering, or CONTAINS/REGEX for partial matches.",
    "Never include URI variables (?xxxURI, ?s, ?o) in SELECT output — they hold entity URIs, not human-readable values.",
    "For every entity type in the query, SELECT its display_fields listed in the schema class block.",
    "Aggregate syntax: (COUNT(DISTINCT ?x) AS ?n) — wrapped in parentheses with AS. GROUP BY is mandatory whenever SELECT contains any aggregate; list every non-aggregate SELECT variable in GROUP BY.",
    "Use OPTIONAL for nullable properties such as death dates, middle names, or optional codes — these are absent from some entities.",
    "CATEGORICAL URI PROPERTIES: some properties (kind=categorical_uri in the schema) store ontology subtype URIs, not string literals. For these, FILTER(?var = \"EMERGENCY\") always returns 0 rows. Use FILTER(STRAFTER(STR(?var), '#') = 'LocalName') or FILTER(?var = <full_uri>) instead. The schema's value_map shows the source value → URI local name mapping for each such property.",
    "OBJECT PROPERTY ID FILTERING: for kind=object properties, ?varURI binds to a full entity URI (e.g. <http://mesh.local/data/patient/10124>), not a bare ID. To filter by raw ID, use FILTER(STRAFTER(STR(?varURI), '/{entity}/') = '10124'), or join to the linked entity and use its id_column data property.",
]


def _uri_frag(uri: str) -> str:
    return uri.split("#")[-1] if "#" in uri else uri.split("/")[-1]


def _detect_ont_namespace(definitions: list) -> Optional[str]:
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


def _source_to_xsd(source_data_type: str) -> str:
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


_DISPLAY_PRIORITY: List[str] = [
    "name", "firstname", "lastname", "fullname", "title", "label",
    "description", "date", "birthdate", "startdate",
    "stopdate", "datetime", "class", "type", "status", "gender", "race",
    "ethnicity", "code", "cost", "city",
    "state", "identifier",
]


def _pick_display_fields(
    column_specs: list,
    entity_map: Dict[str, str],
    gold_table_name: str,
    prop_range: Dict[str, str],
    class_to_entity: Dict[str, str],
    entity_short: str,
    id_column: Optional[str],
) -> List[str]:
    id_prop_uri = next(
        (s.property_uri for s in column_specs if id_column and s.gold_column_name == id_column),
        None,
    )
    data_specs = [
        s for s in column_specs
        if not ((entity_map.get(s.gold_column_name) or entity_map.get(s.source_column)) and
                (entity_map.get(s.gold_column_name) or entity_map.get(s.source_column)) != gold_table_name)
        and not (prop_range and class_to_entity and
                 class_to_entity.get(prop_range.get(s.property_uri, ""), "") not in ("", entity_short))
        and not (id_prop_uri and s.property_uri == id_prop_uri)
    ]

    def _rank(spec) -> int:
        frag = _uri_frag(spec.property_uri).lower().replace("has", "").replace("_", "")
        for i, kw in enumerate(_DISPLAY_PRIORITY):
            if kw in frag:
                return i
        return len(_DISPLAY_PRIORITY)

    return [s.gold_column_name for s in sorted(data_specs, key=_rank)[:4]]


def _build_prop_range(ontology_graph: Any) -> Dict[str, str]:
    try:
        from rdflib import OWL, RDF, RDFS
        result: Dict[str, str] = {}
        for prop in ontology_graph.subjects(RDF.type, OWL.ObjectProperty):
            range_ = next(ontology_graph.objects(prop, RDFS.range), None)
            if range_:
                result[str(prop)] = str(range_)
        return result
    except Exception:
        return {}


def _build_value_matching_guidance(mapping_report: Any, definitions: Any = None) -> List[str]:
    """Build per-column categorical guidance showing the URI local names stored in Fuseki.

    Columns with value_mappings use discriminated subtypes — Fuseki stores URIs, not
    raw string literals.  The guidance shows both forms so the agent knows to filter
    on the URI local name (via STRAFTER) rather than the raw source value.

    Guidance is reconciled against what actually materialised: the RDF layer only emits
    categorical URIs that survive into a gold column's ``value_map``, so any report
    value_mapping whose target is absent from every gold ``value_map`` is dropped, and a
    column with no surviving targets is omitted.  Without this, a mapping dropped during
    gold generation (e.g. Schedules.venue_code → VenueCode* — absent from gold and the
    graph) would tell the agent to filter on entities that do not exist.  Checking gold
    rather than the .owl is deliberate: value-mapped individuals are materialised into the
    data but are not necessarily declared in the ontology file.
    """
    guidance: List[str] = []
    if mapping_report is None:
        return guidance
    # URIs actually materialised by the RDF layer = those present in some gold value_map.
    gold_uris: Optional[set] = None
    if definitions:
        try:
            gold_uris = {
                str(uri)
                for d in definitions
                for spec in d.column_specs
                for uri in (getattr(spec, "value_map", None) or {}).values()
                if uri
            }
        except Exception:
            gold_uris = None
    for tm in mapping_report.tables:
        for cm in tm.column_mappings:
            if not cm.value_mappings:
                continue
            vms = cm.value_mappings
            if gold_uris is not None:
                vms = [vm for vm in vms if str(vm.mapped_uri) in gold_uris]
                if not vms:
                    continue  # nothing materialised for this column — omit it
            entity = tm.source_table.replace("raw_", "").replace("_", " ").title()
            # Show URI local name form — that's what Fuseki actually holds
            uri_frags = [_uri_frag(vm.mapped_uri) for vm in vms[:10]]
            raw_vals = [str(vm.source_value) for vm in vms[:10]]
            guidance.append(
                f"{entity}.{cm.source_column} stores URI subtypes (NOT string literals). "
                f"Source values ({', '.join(raw_vals)}"
                + (" ..." if len(vms) > 10 else "")
                + f") map to URI local names: {', '.join(uri_frags)}"
                + (" ..." if len(vms) > 10 else "")
                + ". Filter: FILTER(STRAFTER(STR(?var), '#') = 'LocalName')"
            )
    return guidance


def build_agent_schema(
    definitions: list,
    mapping_report: Any,
    named_graph: str,
    base_uri: str,
    ontology_graph: Any = None,
    ontology_name: str = "",
) -> Dict[str, Any]:
    """Return the structured JSON schema for agent-side query construction.

    Parameters
    ----------
    definitions:
        List of GoldTableDefinition — one per ontology class.
    mapping_report:
        SemanticMappingReport — used to derive value_matching_guidance.
    named_graph:
        The Fuseki named graph URI (injected into construction_rules).
    base_uri:
        The RDF base URI for individual subjects (data: prefix).
    ontology_graph:
        Optional rdflib.Graph — used to derive object property join directions.
    ontology_name:
        Descriptive label for the ontology, stored in schema_version metadata.
    """
    ont_ns = _detect_ont_namespace(definitions)
    prop_range: Dict[str, str] = _build_prop_range(ontology_graph) if ontology_graph else {}

    entity_map: Dict[str, str] = {
        d.gold_table_name[len("gold_"):]: d.gold_table_name
        for d in definitions
        if d.gold_table_name.startswith("gold_")
    }
    class_to_entity: Dict[str, str] = {
        d.primary_class_uri: d.gold_table_name[len("gold_"):]
        for d in definitions
        if d.gold_table_name.startswith("gold_") and d.primary_class_uri
    }

    classes = []
    for defn in definitions:
        entity_short = (
            defn.gold_table_name[len("gold_"):]
            if defn.gold_table_name.startswith("gold_")
            else defn.gold_table_name
        )
        id_prop_uri = next(
            (s.property_uri for s in defn.column_specs
             if defn.id_column and s.gold_column_name == defn.id_column),
            None,
        )

        properties = []
        join_patterns = []

        for spec in defn.column_specs:
            if id_prop_uri and spec.property_uri == id_prop_uri:
                continue

            col = spec.gold_column_name
            # Mirror triple_builder: check source_column first, then gold_column_name.
            # triple_builder uses source_column for entity_map lookup, so a property whose
            # source column is named "patient" but gold column is "subject_id" is correctly
            # materialised as a URI — but was silently declared kind=data in the schema.
            referenced_table = entity_map.get(col) or entity_map.get(spec.source_column)
            range_class = prop_range.get(spec.property_uri)
            target_via_range = class_to_entity.get(range_class, "") if range_class else ""

            is_fk_by_col = referenced_table and referenced_table != defn.gold_table_name
            is_fk_by_range = target_via_range and target_via_range != entity_short

            if spec.value_map:
                # Discriminated categorical — TripleBuilder emits an object property
                # pointing to a subtype URI, NOT a literal.  FILTER(?var = "EMERGENCY")
                # will always return 0 rows; use FILTER(?var = <SubtypeURI>) or
                # FILTER(STRAFTER(STR(?var), "#") = "LocalName") instead.
                value_map_frags = {src: _uri_frag(uri) for src, uri in spec.value_map.items()}
                properties.append({
                    "uri": spec.property_uri,
                    "label": _uri_frag(spec.property_uri),
                    "kind": "categorical_uri",
                    "source_column": spec.source_column,
                    "note": (
                        "Stores a subtype URI, not a string literal. "
                        "Filter with FILTER(STRAFTER(STR(?var), '#') = 'LocalName') "
                        "or FILTER(?var = <full_uri>). "
                        "Do NOT use FILTER(?var = \"raw_string\")."
                    ),
                    "value_map": value_map_frags,
                })
            elif is_fk_by_col or is_fk_by_range:
                ref_short = (
                    referenced_table[len("gold_"):] if is_fk_by_col and referenced_table.startswith("gold_")
                    else (referenced_table if is_fk_by_col else target_via_range)
                )
                ref_def = next((d for d in definitions if d.gold_table_name == f"gold_{ref_short}"), None)
                ref_class_uri = ref_def.primary_class_uri if ref_def else ""

                if range_class and range_class == defn.primary_class_uri:
                    sparql_pattern = f"?{ref_short}URI <{spec.property_uri}> ?{entity_short}URI ."
                else:
                    sparql_pattern = f"?{entity_short}URI <{spec.property_uri}> ?{ref_short}URI ."

                properties.append({
                    "uri": spec.property_uri,
                    "label": _uri_frag(spec.property_uri),
                    "kind": "object",
                    "range_class": ref_class_uri,
                })
                join_patterns.append({
                    "to_class": ref_class_uri,
                    "to_entity": ref_short,
                    "via_property": spec.property_uri,
                    "sparql_pattern": sparql_pattern,
                })
            else:
                properties.append({
                    "uri": spec.property_uri,
                    "label": _uri_frag(spec.property_uri),
                    "kind": "data",
                    "range": _source_to_xsd(spec.source_data_type),
                    "source_column": spec.source_column,
                })

        display_fields = _pick_display_fields(
            defn.column_specs, entity_map, defn.gold_table_name,
            prop_range, class_to_entity, entity_short, defn.id_column,
        )

        classes.append({
            "uri": defn.primary_class_uri,
            "label": entity_short,
            "gold_table": defn.gold_table_name,
            "id_column": defn.id_column,
            "display_fields": display_fields,
            "discriminated_subtypes": defn.discriminated_class_uris,
            "properties": properties,
            "join_patterns": join_patterns,
        })

    namespaces: Dict[str, str] = {"data": base_uri}
    if ont_ns:
        namespaces["ont"] = ont_ns

    rules = list(_CONSTRUCTION_RULES)
    rules[1] = f"Always wrap triple patterns in GRAPH <{named_graph}> {{ ... }}."

    return {
        "schema_version": SCHEMA_VERSION,
        "ontology_name": ontology_name,
        "named_graph": named_graph,
        "namespaces": namespaces,
        "classes": classes,
        "value_matching_guidance": _build_value_matching_guidance(mapping_report, definitions),
        "construction_rules": rules,
    }
