# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Judge-LLM prompt assembly for the iterative ontology generator.

The Judge evaluates a Gen-LLM fragment for:
  1. COVERAGE — every non-PK column has a property
  2. COHERENCE — correct namespace, qualified names, valid ranges
  3. DUPLICATES — no redeclaration of already-accumulated classes/properties

Returns a JSON verdict (JudgeVerdict).
"""
from typing import List

from semantic_mapper.ontology.iterative.models import (
    AccumulatedClassSummary,
    OntologyFragment,
    TableContext,
)

_JUDGE_SYSTEM_PROMPT = (
    "You are a strict OWL ontology reviewer. "
    "Respond with a single JSON object only — no prose, no markdown fences."
)

_VERDICT_SCHEMA = """{
  "approved": <true|false>,
  "coverage_issues": ["col_name: reason", ...],
  "coherence_issues": ["description", ...],
  "revision_instructions": "<concise instructions for the generator to fix all issues, or empty string if approved>",
  "confidence": <0.0-1.0>
}"""


def build_judge_prompt(
    table_ctx: TableContext,
    fragment: OntologyFragment,
    accumulated_classes: List[AccumulatedClassSummary],
    base_iri: str,
    max_rows_for_enumeration: int = 1000,
) -> tuple[str, str]:
    """
    Build the Judge-LLM system and user prompts.

    Returns:
        (system_prompt, user_prompt)
    """
    # Coverage checklist: all non-PK columns
    coverage_rows = []
    for col in table_ctx.columns:
        if col.is_pk:
            continue
        tags = []
        if col.is_fk and col.fk_ref_table:
            if col.fk_ref_skipped:
                tags.append(f"FK→{col.fk_ref_table} [SKIPPED]")
            else:
                tags.append(f"FK→{col.fk_ref_table}")
        if col.cardinality_hint:
            tags.append(col.cardinality_hint)
        elif col.is_categorical:
            tags.append("CATEGORICAL")
        if col.null_rate > 0.5:
            tags.append("nullable")
        tag_str = "  [" + ", ".join(tags) + "]" if tags else ""
        coverage_rows.append(f"  {col.col_name:<30} {col.inferred_type:<12}{tag_str}")

    accumulated_lines = []
    for cls in accumulated_classes:
        accumulated_lines.append(f"  <{cls.uri}>  rdfs:label \"{cls.label}\"@en")

    entity_class_warning = []
    if table_ctx.row_count > max_rows_for_enumeration:
        entity_class_warning = [
            f"⚠️  ENTITY-CLASS GUARD: This table has {table_ctx.row_count:,} rows "
            f"(threshold: {max_rows_for_enumeration:,}). It is an ENTITY TABLE — "
            "owl:equivalentClass / owl:oneOf enumeration is a HARD REJECTION.",
            "",
        ]

    user_prompt = "\n".join([
        f"TABLE BEING REVIEWED: {table_ctx.table_name}",
        "",
        *entity_class_warning,
        "COLUMNS THAT MUST EACH HAVE A PROPERTY (all non-PK columns):",
        "\n".join(coverage_rows) if coverage_rows else "  (none — all columns are PK)",
        "",
        "GEN-LLM OUTPUT:",
        fragment.manchester_syntax,
        "",
        "EXISTING CLASSES (must NOT be redeclared):",
        "\n".join(accumulated_lines) if accumulated_lines else "  (none — first table)",
        "",
        f"REQUIRED NAMESPACE: {base_iri}",
        "",
        "EVALUATE:",
        "1. COVERAGE: Does every non-PK column listed above have a corresponding OWL property?",
        "   Flag any column that is missing or only partially covered.",
        "   ACCEPT: FK columns marked [SKIPPED] modelled as DatatypeProperty xsd:integer — the referenced table was empty/absent.",
        "2. COHERENCE: Are all URIs in the correct namespace? Are generic property names qualified?",
        "   Do FK ranges reference the correct existing class URIs?",
        "3. DUPLICATES: Does the output redeclare any class listed under EXISTING CLASSES?",
        "4. SKOS: Does every DatatypeProperty include skos:altLabel with the column name?",
        "5. DOMAIN: Does every property have exactly ONE rdfs:domain declaration? [MULTI-DOMAIN check]",
        "   Missing domain or multiple domains is a HARD REJECTION.",
        "   MULTI-DOMAIN: If a property references >1 class via Domain:, it must be split into class-scoped subproperties.",
        "6. RANGE: Does every property have an rdfs:range declaration?",
        "   Missing range is a HARD REJECTION.",
        "7. DUAL-TYPE: Is any URI typed as both ObjectProperty and DatatypeProperty?",
        "   A URI carrying both types is a HARD REJECTION.",
        "8. INDIVIDUALS: Are owl:NamedIndividual or owl:oneOf patterns used ONLY for CATEGORICAL_CLOSED columns?",
        "   Using these patterns for non-categorical columns is a HARD REJECTION.",
        "9. GENERIC NAMES: Are generic column names (value, name, code, type, etc.) class-scoped?",
        "10. LABELS/COMMENTS: Does every Class and Property have both rdfs:label and rdfs:comment?",
        "11. ABSTRACT PARENTS: Do abstract parent properties (SubPropertyOf targets) have NO rdfs:domain?",
        "    An abstract parent with a Domain: declaration is a HARD REJECTION.",
        "    SELF-REF: A SubPropertyOf: declaration where the subject == the object is a HARD REJECTION (self-referential).",
        "12. CARDINALITY: Are cardinality hints honoured?",
        "    CATEGORICAL_CLOSED → MUST use ObjectProperty + Class + owl:NamedIndividual (or owl:oneOf). ACCEPT.",
        "    CATEGORICAL_OPEN → MUST be DatatypeProperty xsd:string. REJECT if modelled as ObjectProperty or Class.",
        "    FLAG → MUST be DatatypeProperty xsd:boolean or xsd:integer. REJECT if modelled as class or individual.",
        "    FREE_TEXT → MUST be DatatypeProperty xsd:string. REJECT if modelled as class or individual.",
        "    IDENTIFIER → MUST be DatatypeProperty xsd:integer or xsd:string. REJECT if modelled as class.",
        "    CATEGORICAL-PROMOTION: A CATEGORICAL column modelled as DatatypeProperty xsd:string is a HARD REJECTION.",
        "    CATEGORICAL columns must produce ObjectProperty + Class + owl:NamedIndividual.",
        "13. PROVENANCE (fm: annotations): Does every Class: block include fm:sourceTable \"<table_name>\"?",
        "    Does every DatatypeProperty: block include fm:sourceColumn, fm:sqlType, and fm:nullRate?",
        "    Does every ObjectProperty: block for a FK column include fm:sourceForeignKey?",
        "    Does every ObjectProperty: block for a CATEGORICAL column include fm:sourceForeignKey",
        "    (format: \"source_table.col_name -> ClassName\", where col_name is the EXACT source column)?",
        "    (Abstract ObjectProperty parents — those with SubPropertyOf pointing TO them and no domain — are exempt",
        "     from P.3/P.3b but should still have rdfs:comment.)",
        "    Missing fm:sourceTable on a Class is a HARD REJECTION.",
        "    Missing fm:sourceColumn, fm:sqlType, or fm:nullRate on a DatatypeProperty is a HARD REJECTION.",
        "    Missing fm:sourceForeignKey on a concrete FK or categorical ObjectProperty is a HARD REJECTION.",
        "14. COMMENT PURITY: Does any rdfs:comment contain \"Derived from\"? That is a HARD REJECTION.",
        "    rdfs:comment must be human-readable description only — provenance belongs in fm: annotations.",
        "15. SINGLE COMMENT: Does any element have more than one rdfs:comment value? That is a HARD REJECTION.",
        "16. MIXED_RANGE: Does any property declare both a class URI range and an XSD datatype range?",
        "    A property is either an ObjectProperty (exactly one class URI as rdfs:range) or a DatatypeProperty",
        "    (exactly one XSD type as rdfs:range) — never a mix of both on the same property URI.",
        "    HARD REJECTION: any property with both rdfs:range <SomeClass> and rdfs:range xsd:anyType.",
        "17. CLASS_REUSE: Does the fragment declare a new owl:Class whose concept duplicates a class already",
        "    listed under EXISTING CLASSES? (e.g. if Currency is listed, reject OrderItemCurrency).",
        "    HARD REJECTION: minting a table-prefixed variant of an existing class. Instruct the generator",
        "    to use the exact existing URI as the ObjectProperty range instead.",
        "",
        "Respond with this exact JSON schema:",
        _VERDICT_SCHEMA,
    ])

    return _JUDGE_SYSTEM_PROMPT, user_prompt
