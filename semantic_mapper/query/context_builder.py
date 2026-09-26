# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import re
from typing import Dict, List, Optional

from semantic_mapper.schema.mapping_output import (
    MappingStatus,
    SemanticMappingReport,
    ValueMappingRule,
)


# ---------------------------------------------------------------------------
# Module-level helpers (tested directly)
# ---------------------------------------------------------------------------

def _uri_fragment(uri: str) -> str:
    """Return the local name after '#' or the last path segment."""
    if "#" in uri:
        return uri.split("#")[-1]
    return uri.split("/")[-1]


def _property_col_name(uri: str, fallback: str) -> str:
    """Replicate pipeline._resolve_property_local_name snake_case logic."""
    local = uri.split("#")[-1] if "#" in uri else uri.split("/")[-1]
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", local).lower()
    return snake if snake and snake != "property" else fallback


def _source_col_name(source_column: str) -> str:
    """Replicate MySQLSilverWriter._safe_col_name — the actual column name in silver tables."""
    return source_column.replace(" ", "_").replace("-", "_").lower()


def _format_value_mappings(value_mappings: List[ValueMappingRule]) -> str:
    """Return a compact inline string showing original→class mappings, e.g. 'POL→Policy, SUB→Subscription'."""
    parts = []
    for rule in value_mappings:
        label = _uri_fragment(rule.mapped_uri) if rule.mapped_uri else "null"
        parts.append(f"{rule.source_value}→{label}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_schema_context(
    report: SemanticMappingReport,
    actual_class_uris: Optional[Dict[str, List[str]]] = None,
) -> str:
    """Build a compact natural-language schema string from the mapping report.

    Only CONFIRMED mappings are included.  The result is cached by callers.

    ``actual_class_uris`` maps silver table name → sorted list of class
    fragments actually present in that table's ``_ontology_class_uri`` column.
    When supplied the TABLE header and the metadata column description use the
    real class names rather than the ontology candidates.
    """
    sections: List[str] = []

    for table in report.tables:
        silver_table = f"silver_{table.source_table}"

        # Class header — prefer actual DB classes when available
        if actual_class_uris and silver_table in actual_class_uris:
            class_frags = actual_class_uris[silver_table]
        else:
            class_frags = [
                _uri_fragment(c.uri)
                for c in table.class_candidate_set
                if c.confidence_score > 0.5
            ]
        class_header = " | ".join(class_frags) if class_frags else "(unknown)"

        # _ontology_class_uri metadata line — include class list when known
        if actual_class_uris and silver_table in actual_class_uris:
            class_list = " | ".join(actual_class_uris[silver_table])
            uri_col_line = (
                f"  _ontology_class_uri TEXT"
                f"  — entity class column. Classes in this table: {class_list}."
                f" Filter with LIKE '%ClassName' to restrict by class."
            )
        else:
            uri_col_line = (
                "  _ontology_class_uri TEXT"
                "  — filter with LIKE '%ClassName' to restrict by class"
            )

        lines: List[str] = [
            f"TABLE: {silver_table}  (class: {class_header})",
            uri_col_line,
            f"  _bronze_source_ref TEXT   — audit: source row reference",
        ]

        for col in table.column_mappings:
            if col.status != MappingStatus.CONFIRMED:
                continue
            if not col.mapped_property_uri:
                continue

            col_name = _source_col_name(col.source_column)
            prop_frag = _uri_fragment(col.mapped_property_uri)
            label = col.mapped_property_label or prop_frag

            if col.value_mappings:
                # ValueMapper has transformed coded values into full ontology URIs.
                # Filter this column with LIKE '%ClassName', same as _ontology_class_uri.
                mapping_hint = _format_value_mappings(col.value_mappings)
                lines.append(
                    f"  {col_name} TEXT"
                    f"  — {prop_frag}: contains full URI after transformation"
                    f" (filter with LIKE '%ClassName'). Original codes: {mapping_hint}"
                )
            else:
                # Suppress label when it's identical to the property fragment
                label_part = f": {label}" if label != prop_frag else ""
                lines.append(
                    f"  {col_name} {col.source_data_type}"
                    f"  — {prop_frag}{label_part}"
                )

        sections.append("\n".join(lines))

    return "\n\n".join(sections)
