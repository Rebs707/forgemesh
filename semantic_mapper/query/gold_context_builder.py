# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import Dict, List, Optional

from semantic_mapper.gold.models import GoldTableDefinition


def _uri_fragment(uri: str) -> str:
    if "#" in uri:
        return uri.split("#")[-1]
    return uri.split("/")[-1]


def build_gold_schema_context(
    definitions: List[GoldTableDefinition],
    actual_class_uris: Optional[Dict[str, List[str]]] = None,
) -> str:
    """Build a compact natural-language schema string from gold table definitions.

    Mirrors build_schema_context() for silver but driven by GoldTableDefinition
    objects rather than SemanticMappingReport tables.

    Also emits a JOINS section documenting FK relationships inferred from column
    names that match another gold entity (e.g. ``patient`` in gold_procedure →
    gold_patient.id).
    """
    # Build lookup: entity short-name → gold table name
    # e.g. "patient" → "gold_patient", "encounter" → "gold_encounter"
    entity_map: Dict[str, str] = {
        d.gold_table_name[len("gold_"):]: d.gold_table_name
        for d in definitions
        if d.gold_table_name.startswith("gold_")
    }

    sections: List[str] = []
    fk_lines: List[str] = []

    for definition in definitions:
        tbl = definition.gold_table_name
        class_frag = _uri_fragment(definition.primary_class_uri)

        if actual_class_uris and tbl in actual_class_uris:
            class_header = " | ".join(actual_class_uris[tbl])
            class_list = class_header
            uri_col_line = (
                f"  _ontology_class_uri TEXT"
                f"  — entity class column. Classes in this table: {class_list}."
                f" Filter with LIKE '%ClassName' to restrict by class."
            )
        else:
            class_header = class_frag
            uri_col_line = (
                "  _ontology_class_uri TEXT"
                "  — filter with LIKE '%ClassName' to restrict by class"
            )

        lines: List[str] = [
            f"TABLE: {tbl}  (class: {class_header})",
            uri_col_line,
            "  _bronze_source_ref TEXT   — audit: source row reference",
        ]

        for spec in definition.column_specs:
            prop_frag = _uri_fragment(spec.property_uri)
            col = spec.gold_column_name
            # Detect FK: column name matches another gold entity and the referenced
            # table is not the current one.
            referenced_table = entity_map.get(col)
            if referenced_table and referenced_table != tbl:
                lines.append(
                    f"  {col} {spec.source_data_type}"
                    f"  — {prop_frag} (FK → {referenced_table}.id)"
                )
                fk_lines.append(f"  {tbl}.{col} = {referenced_table}.id")
            else:
                lines.append(
                    f"  {col} {spec.source_data_type}"
                    f"  — {prop_frag}"
                )

        sections.append("\n".join(lines))

    if fk_lines:
        joins_section = (
            "JOINS — use these exact column pairs when joining gold tables:\n"
            + "\n".join(fk_lines)
        )
        sections.append(joins_section)

    return "\n\n".join(sections)
