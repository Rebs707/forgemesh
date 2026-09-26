# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Gen-LLM prompt assembly for the iterative ontology generator.

Produces a four-section prompt (task context, accumulated classes, table profile,
rules) that constrains the LLM to output valid OWL Manchester Syntax for exactly
one table at a time.
"""
import re as _re
from typing import List

from semantic_mapper.ontology.iterative.context_builder import derive_class_local_name
from semantic_mapper.ontology.iterative.models import (
    AccumulatedAbstractPropertySummary,
    AccumulatedClassSummary,
    OntologyFragment,
    TableContext,
)


def build_gen_prompt(
    table_ctx: TableContext,
    retrieved_classes: List[AccumulatedClassSummary],
    deterministic_uri: str,
    base_iri: str,
    revision_instructions: str = "",
    existing_property_locals: List[str] = (),
    abstract_properties: List[AccumulatedAbstractPropertySummary] = (),
    max_rows_for_enumeration: int = 1000,
) -> str:
    """
    Build the Gen-LLM prompt for a single table.

    Args:
        table_ctx:              Structured table context (columns, FKs, etc.).
        retrieved_classes:      Top-k already-declared classes from the index.
        deterministic_uri:      The EXACT class URI the LLM MUST use for the primary class.
        base_iri:               Ontology namespace (e.g. "http://example.org/shop#").
        revision_instructions:  If non-empty, appended as a revision request section.

    Returns:
        A string prompt ready to send to the Gen-LLM.
    """
    sections: List[str] = []

    # ---- Section 1: Task context ----
    entity_guard = (
        f"ENTITY TABLE — {table_ctx.row_count:,} rows > {max_rows_for_enumeration:,} threshold: "
        f"this is an ENTITY class, NOT a controlled vocabulary. "
        f"Do NOT generate owl:NamedIndividual blocks, owl:equivalentClass, or owl:oneOf "
        f"for the primary class <{deterministic_uri}>."
        if table_ctx.row_count > max_rows_for_enumeration else ""
    )
    sections.append(
        f"You are an expert ontology engineer building an OWL ontology table by table.\n"
        f"Table {table_ctx.processing_index} of {table_ctx.total_tables}: "
        f'"{table_ctx.table_name}" ({table_ctx.row_count:,} rows).\n'
        f"Namespace: {base_iri}\n"
        + (entity_guard + "\n" if entity_guard else "")
        + "Output ONLY valid OWL Manchester Syntax. No prose, no markdown fences, no explanations."
    )

    # ---- Section 2: Accumulated classes ----
    if retrieved_classes:
        lines = [
            "CLASSES ALREADY IN THE ONTOLOGY — do NOT redeclare; you MAY reference them as ranges/superclasses:"
        ]
        for cls in retrieved_classes:
            tables_str = ", ".join(cls.source_tables) if cls.source_tables else "unknown"
            lines.append(f"  Class: <{cls.uri}>   Annotations: rdfs:label \"{cls.label}\"@en   (source: {tables_str})")
        sections.append("\n".join(lines))
    else:
        sections.append(
            "CLASSES ALREADY IN THE ONTOLOGY: none yet — this is the first table."
        )

    if existing_property_locals:
        prop_lines = [
            "PROPERTIES ALREADY IN THE ONTOLOGY — do NOT reuse these local names without class-scoping:"
        ]
        for local in sorted(set(existing_property_locals)):
            prop_lines.append(f"  {base_iri}{local}")
        sections.append("\n".join(prop_lines))

    dtype_abstracts = [ap for ap in abstract_properties if ap.prop_type != "ObjectProperty"]
    obj_abstracts   = [ap for ap in abstract_properties if ap.prop_type == "ObjectProperty"]

    if dtype_abstracts:
        ap_lines = [
            "ABSTRACT DATATYPEPROPERTIES (no rdfs:domain — shared parent pattern):",
            "  If your column's natural property name matches one below, create a class-scoped",
            "  subproperty of it (SubPropertyOf:) rather than a new standalone property.",
        ]
        for ap in dtype_abstracts:
            ap_lines.append(
                f"  <{ap.uri}>  local={ap.local_name}  range={ap.range_uri or 'unset'}"
            )
        sections.append("\n".join(ap_lines))

    if obj_abstracts:
        op_lines = [
            "ABSTRACT OBJECTPROPERTIES FOR FK RANGES (no rdfs:domain — use as SubPropertyOf parents):",
            "  For every FK ObjectProperty you declare, if its Range: matches one of these,",
            "  you MUST declare SubPropertyOf: <abstract_uri>.",
        ]
        for ap in obj_abstracts:
            range_local = ap.range_uri.split("#")[-1].split("/")[-1] if ap.range_uri else "?"
            op_lines.append(
                f"  <{ap.uri}>  local={ap.local_name}  range=<{ap.range_uri}>  "
                f"(range class: {range_local})"
            )
        sections.append("\n".join(op_lines))

    # ---- Section 3: Table profile ----
    col_lines = ["COLUMNS:"]
    non_pk_cols = []
    for col in table_ctx.columns:
        flags = []
        if col.is_pk:
            flags.append("PK")
        if col.is_fk and col.fk_ref_table:
            if col.fk_ref_skipped:
                flags.append(
                    f"FK→{col.fk_ref_table} [SKIPPED — model as DatatypeProperty xsd:integer]"
                )
            else:
                flags.append(f"FK→{col.fk_ref_table}")
        if col.is_nullable:
            flags.append("nullable")
        if col.null_rate > 0.5:
            flags.append(f"null_rate={col.null_rate:.0%}")
        if col.cardinality_hint == "CATEGORICAL_CLOSED":
            flags.append(f"CATEGORICAL_CLOSED values=[{', '.join(col.categorical_values[:8])}]")
        elif col.cardinality_hint == "CATEGORICAL_OPEN" and col.top_values:
            flags.append(f"CATEGORICAL_OPEN sample=[{', '.join(col.top_values[:5])}]")
        elif col.cardinality_hint in ("FLAG", "FREE_TEXT", "IDENTIFIER"):
            flags.append(col.cardinality_hint)
        elif col.is_categorical:
            flags.append(f"CATEGORICAL values=[{', '.join(col.categorical_values[:8])}]")
        elif col.top_values:
            flags.append(f"top_values=[{', '.join(col.top_values[:5])}]")
        if col.detected_formats:
            flags.append(f"format={col.detected_formats[0]}")
        flag_str = "  " + "  ".join(flags) if flags else ""
        col_lines.append(f"  {col.col_name:<30} {col.inferred_type:<12}{flag_str}")
        if not col.is_pk:
            non_pk_cols.append(col.col_name)
    sections.append("\n".join(col_lines))

    # ---- Section 3.5: FK column → class URI mapping ----
    fk_active = [
        col for col in table_ctx.columns
        if col.is_fk and col.fk_ref_table and not col.fk_ref_skipped
    ]
    if fk_active:
        fk_lines = [
            "FK COLUMNS — each MUST be owl:ObjectProperty. Modelling any of these as "
            "DatatypeProperty is a HARD REJECTION.",
            "  Exact Range: URIs to use (derived deterministically from referred table):",
        ]
        for col in fk_active:
            class_local = derive_class_local_name(col.fk_ref_table)
            fk_lines.append(
                f"    {col.col_name:<30}  →  Range: <{base_iri}{class_local}>"
            )
        sections.append("\n".join(fk_lines))

    # ---- Section 4: Rules ----
    rules = [
        "RULES (MUST follow exactly):",
        f"1. Declare exactly ONE primary class URI: <{deterministic_uri}>",
        "2. Non-PK non-FK column → owl:DatatypeProperty. BOTH Domain: AND Range: are REQUIRED — a property missing either is invalid.",
        "   Choose range from: xsd:string (text), xsd:integer (IDs/counts/integers), xsd:decimal (amounts/rates/weights),",
        "   xsd:double (floating-point values), xsd:dateTime (timestamps with time), xsd:date (date-only), xsd:boolean (flags/bits).",
        "   Every DatatypeProperty MUST have an explicit Range: declaration.",
        "3. FK column → owl:ObjectProperty ONLY. Domain: = primary class REQUIRED.",
        "   Range: = the exact class URI listed in the FK COLUMNS section above.",
        "   HARD REJECTION: modelling a non-SKIPPED FK column as DatatypeProperty (e.g. xsd:integer) is forbidden.",
        "   The integer ID is a relational artefact — the ontology must express the relationship directly as ObjectProperty.",
        "   If the referenced class is not yet declared (not in Section 2), declare a placeholder Class for it.",
        "   EXCEPTION: FK column marked '[SKIPPED — model as DatatypeProperty xsd:integer]' means its referenced table was empty/absent.",
        "   Model that column as DatatypeProperty with Range: xsd:integer — the only case where integer modelling is permitted for a FK.",
        "4. CATEGORICAL_CLOSED column (shown as CATEGORICAL_CLOSED in column profile) → REQUIRED pattern: declare exactly ONE owl:Class for the categorical concept.",
        "   A CATEGORICAL_CLOSED column MUST use ObjectProperty + Class + Individual — NEVER a DatatypeProperty xsd:string for categorical values.",
        "   If a column is marked CATEGORICAL_CLOSED, modelling it as xsd:string DatatypeProperty is a HARD REJECTION.",
        "   NEVER create a second class with an 'Enum' suffix — one class only, never Foo + FooEnum.",
        "   For EACH distinct categorical value, declare an Individual: block with Types: pointing to that class.",
        "   Use {Value}{ClassName} URIs for individuals: PascalCase(value) + class local name.",
        "   PascalCase(value): capitalise the first letter of each space-separated word, then remove spaces.",
        "   Do NOT stem, singularize, or shorten word endings — 'Not Stopped' → 'NotStopped', not 'NotStopd'.",
        "   Strip special characters (/, ', -) by removing them, then capitalise surrounding words normally.",
        "   e.g. :CreditCardPaymentMethod not :CreditCard; :ExpressShippingMethod not :Express.",
        "   Link the primary class via ONE owl:ObjectProperty (domain=primary class, range=the categorical class).",
        "   Example for payment_method column with values Credit Card, Direct Debit:",
        "     Class: :PaymentMethod",
        "     Individual: :CreditCardPaymentMethod",
        "         Types: :PaymentMethod",
        "         Annotations: rdfs:label \"Credit Card\"@en",
        "     Individual: :DirectDebitPaymentMethod",
        "         Types: :PaymentMethod",
        "         Annotations: rdfs:label \"Direct Debit\"@en",
        "     ObjectProperty: :hasPaymentMethod  Domain: :Order  Range: :PaymentMethod",
        "5. Do NOT redeclare any class listed in Section 2. Reference their URIs directly.",
        "5b. CLASS REUSE — if a column's concept matches an already-declared class in Section 2,",
        "    use that exact URI as the ObjectProperty range. Do NOT mint a table-prefixed variant.",
        "    If Currency is listed, do NOT declare OrderItemCurrency — use Currency.",
        "    A new class is only warranted when no listed class represents the concept.",
        "    HARD REJECTION: declaring a new class whose label or concept duplicates one in Section 2.",
        "5c. SINGLE RANGE — each property declares exactly ONE rdfs:range.",
        "    An ObjectProperty must have exactly one class URI as range.",
        "    A DatatypeProperty must have exactly one XSD type as range.",
        "    NEVER declare both a class range and an XSD range on the same property URI.",
        "    NEVER declare more than one range of any kind on a single property.",
        "    HARD REJECTION: any property with both rdfs:range <SomeClass> and rdfs:range xsd:anyType.",
        "6. Generic names MUST always be class-scoped with the table concept.",
        "   e.g. hasOrderItemFlag not hasFlag; hasOrderItemValue not hasValue.",
        "   The following bare names MUST always be class-scoped: label, description, value, name, code, type, date, time, id, note, comment, text, status, flag, count, rate, seq.",
        "7. Every DatatypeProperty MUST include: Annotations: skos:altLabel \"<column_name>\"@en",
        "   (This drives automatic column-to-property matching in the downstream mapper.)",
        "8. Include rdfs:label AND rdfs:comment annotations (in English) on every declared Class and Property.",
        "   rdfs:comment must be a concise one-sentence description of what the class or property represents.",
        "   Example: rdfs:comment \"A customer order recording what was bought and when.\"@en",
        "9. Use prefix: Ontology: <{base_iri}> at the top, plus required prefix declarations:\n"
        "   Prefix: fm: <http://forgemesh.ai/ontology#>\n"
        "   Prefix: skos: <http://www.w3.org/2004/02/skos/core#>\n"
        "   Prefix: rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
        "   Prefix: xsd: <http://www.w3.org/2001/XMLSchema#>".replace("{base_iri}", base_iri),
        f"10. Columns that MUST each have a property (non-PK columns): {', '.join(non_pk_cols)}",
        "11. If a property local-name would collide with any property listed in the PROPERTIES section above,",
        "    you MUST use a class-scoped name: has{PrimaryClassLocal}{Concept}.",
        "    e.g. if hasValue already exists and primary class is OrderItem, write hasOrderItemValue.",
        "    If using a class-scoped subproperty, add: SubPropertyOf: <abstract_parent_uri>.",
        "    The abstract parent (e.g. hasValue) must have NO rdfs:domain and rdfs:range = the shared type.",
        f"12. Each property MUST have EXACTLY ONE Domain: declaration, pointing to <{deterministic_uri}> only.",
        "    Using any other class URI as a Domain is forbidden and will cause a HARD REJECTION.",
        "    NEVER list multiple Domain: lines on a single property — split into class-scoped subproperties instead.",
        "13. GENERIC NAMES rule: if a column has one of these bare names (label, description, value, name, code,",
        "    type, date, time, id, note, comment, text, status, subject, amount, rate, uom), ALWAYS:",
        "    (a) first declare an abstract parent property with NO Domain: and range = appropriate XSD type;",
        "    (b) then declare a class-scoped subproperty (has{PrimaryClass}{Concept}) with SubPropertyOf: pointing to the abstract parent.",
        "    This applies even if an abstract parent does not yet exist in the ontology.",
        "14. CARDINALITY HINT rules — the column profile shows one of these hints; follow the REQUIRED modelling:",
        "    CATEGORICAL_CLOSED → ObjectProperty + owl:Class + owl:NamedIndividual (the listed values ARE the complete set).",
        "       owl:oneOf and Individual: blocks are ONLY permitted for CATEGORICAL_CLOSED columns.",
        "    CATEGORICAL_OPEN → DatatypeProperty xsd:string ONLY. The value set is partially known and open-ended.",
        "       Do NOT create an owl:Class or owl:ObjectProperty for a CATEGORICAL_OPEN column.",
        "       Model it as a plain text literal — NEVER as a class, individual, or FK reference.",
        "       HARD REJECTION: declaring an ObjectProperty or owl:Class for a CATEGORICAL_OPEN column is forbidden.",
        "    FLAG → DatatypeProperty with xsd:boolean or xsd:integer range; NEVER model as a class or individual.",
        "    FREE_TEXT → DatatypeProperty xsd:string; NEVER model as a class or individual.",
        "    IDENTIFIER → DatatypeProperty xsd:integer (or xsd:string if non-numeric); NEVER model as a class.",
        "    HARD RULE: owl:oneOf or owl:NamedIndividual blocks are FORBIDDEN for any column not marked CATEGORICAL_CLOSED.",
        "15. ABSTRACT PARENT domain rule: a property declared as an abstract parent (i.e. other properties",
        "    declare SubPropertyOf: pointing to it) MUST NOT carry a Domain: declaration.",
        "    Only class-scoped subproperties carry Domain:.",
        "    If you declare an abstract parent (e.g. for a generic-name column per rule 13), write it with",
        "    NO Domain: line at all. Its subproperties each carry exactly one Domain: line.",
        "    Violating this rule causes OWL inference to incorrectly infer class membership for all individuals",
        "    with that property, regardless of their actual type — a semantically catastrophic error.",
    ]
    if obj_abstracts:
        rules += [
            "16. FK ABSTRACT PARENT rule: for every ObjectProperty you declare for an FK column,",
            "    check the ABSTRACT OBJECTPROPERTIES section listed above.",
            "    If an abstract ObjectProperty exists whose range matches your new property's intended range,",
            "    you MUST add: SubPropertyOf: <abstract_uri>",
            "    The range of your ObjectProperty MUST be the named class — never an XSD type.",
            "    Concrete examples (adjust URIs to your namespace):",
            "      property Range: :Order       → SubPropertyOf: :hasOrder",
            "      property Range: :Customer    → SubPropertyOf: :hasCustomer",
            "      property Range: :Product     → SubPropertyOf: :hasProduct",
            "    Omitting SubPropertyOf when a matching abstract parent exists is a HARD REJECTION.",
        ]

    # ---- Provenance annotation rules P1–P5 (fm: namespace) ----
    # P1 — class provenance: fm:sourceTable
    rules.append(
        f"P.1. PROVENANCE (Class): Every Class: block MUST include exactly one fm:sourceTable annotation.\n"
        f"     Value: the source table name as a plain string (no quotes needed in the annotation value).\n"
        f"     Example:\n"
        f"       Class: :Agreement\n"
        f"         Annotations:\n"
        f"           rdfs:label \"Agreement\"@en,\n"
        f"           rdfs:comment \"A record of an agreement between parties.\"@en,\n"
        f"           fm:sourceTable \"{table_ctx.table_name}\"\n"
        f"     This is REQUIRED. Missing fm:sourceTable is a HARD REJECTION.\n"
        f"     The fm: prefix is http://forgemesh.ai/ontology# — declare it at the top."
    )

    # P2 — DataProperty provenance: fm:sourceColumn, fm:sqlType, fm:nullRate
    dt_prov_lines = [
        "P.2. PROVENANCE (DatatypeProperty): Every DatatypeProperty: block MUST include:",
        "     fm:sourceColumn \"table_name.column_name\"  (format: table.col)",
        "     fm:sqlType \"SQL_TYPE\"                     (e.g. \"INT\", \"VARCHAR(255)\", \"DECIMAL(10,2)\")",
        "     fm:nullRate \"0.00\"                         (decimal string, 0.00 to 1.00)",
        "     Example:",
        "       DataProperty: :hasPremiumAmt",
        "         Annotations:",
        "           rdfs:comment \"The premium amount for the agreement.\"@en,",
        "           fm:sourceColumn \"raw_agreement.premium_amt\",",
        "           fm:sqlType \"DECIMAL(10,2)\",",
        "           fm:nullRate \"0.00\"",
        f"     Expected fm: annotations for this table's data columns:",
    ]
    for col in table_ctx.columns:
        if col.is_pk:
            continue
        if col.is_fk and col.fk_ref_table and not col.fk_ref_skipped:
            continue
        dt_prov_lines.append(
            f"       {col.col_name:<30} "
            f"fm:sourceColumn \"{table_ctx.table_name}.{col.col_name}\"  "
            f"fm:sqlType \"{col.inferred_type}\"  "
            f"fm:nullRate \"{col.null_rate:.2f}\""
        )
    dt_prov_lines.append("     Missing fm:sourceColumn, fm:sqlType, or fm:nullRate is a HARD REJECTION.")
    rules.append("\n".join(dt_prov_lines))

    # P3 — ObjectProperty provenance: fm:sourceForeignKey
    fk_prov_cols = [
        col for col in table_ctx.columns
        if col.is_fk and col.fk_ref_table and not col.fk_ref_skipped
    ]
    if fk_prov_cols:
        op_prov_lines = [
            "P.3. PROVENANCE (ObjectProperty / FK): Every ObjectProperty: block for a FK column",
            "     MUST include: fm:sourceForeignKey \"source_table.fk_col -> target_table\"",
            "     Example:",
            "       ObjectProperty: :hasAgreementType",
            "         Annotations:",
            "           rdfs:comment \"Links an agreement to its type.\"@en,",
            "           fm:sourceForeignKey \"raw_agreement.agr_type -> raw_agreement_type\"",
            f"     Expected fm: annotations for this table's FK columns:",
        ]
        for col in fk_prov_cols:
            op_prov_lines.append(
                f"       {col.col_name:<30} "
                f"fm:sourceForeignKey \"{table_ctx.table_name}.{col.col_name} -> {col.fk_ref_table}\""
            )
        op_prov_lines.append(
            "     Abstract ObjectProperty parents (SubPropertyOf targets with no domain) are exempt from P.3\n"
            "     but MUST still have rdfs:comment.\n"
            "     Missing fm:sourceForeignKey on a concrete FK ObjectProperty is a HARD REJECTION."
        )
        rules.append("\n".join(op_prov_lines))

    # P3b — ObjectProperty provenance for categorical columns
    cat_prov_cols = [
        col for col in table_ctx.columns
        if col.is_categorical and not col.is_pk and not col.is_fk
    ]
    if cat_prov_cols:
        cat_prov_lines = [
            "P.3b. PROVENANCE (ObjectProperty / categorical): Every ObjectProperty you declare to model",
            "      a CATEGORICAL column MUST include: fm:sourceForeignKey \"source_table.col -> ClassName\"",
            "      where col is the EXACT source column name and ClassName is the OWL class you chose.",
            "      Example:",
            "        ObjectProperty: :hasAgreementType",
            "          Annotations:",
            "            rdfs:comment \"Links an agreement to its type.\"@en,",
            "            fm:sourceForeignKey \"raw_agreement.agr_type -> AgreementType\"",
            "      Expected fm: annotations for this table's categorical columns:",
        ]
        for col in cat_prov_cols:
            cat_prov_lines.append(
                f"        {col.col_name:<30} "
                f"fm:sourceForeignKey \"{table_ctx.table_name}.{col.col_name} -> <YourChosenClassName>\""
            )
        cat_prov_lines.append(
            "      Missing fm:sourceForeignKey on a categorical ObjectProperty is a HARD REJECTION."
        )
        rules.append("\n".join(cat_prov_lines))

    # P4 — rdfs:comment purity
    rules.append(
        "P.4. COMMENT PURITY: rdfs:comment on any element MUST contain ONLY a human-readable description "
        "of what the element means. It MUST NOT contain provenance strings like \"Derived from...\". "
        "Provenance belongs exclusively in fm: annotations."
    )

    # P5 — single rdfs:comment
    rules.append(
        "P.5. SINGLE COMMENT: No element should have more than one rdfs:comment value."
    )

    sections.append("\n".join(rules))

    # ---- Optional: Revision section ----
    if revision_instructions:
        sections.append(
            f"REVISION REQUIRED — address ALL of the following issues before responding:\n{revision_instructions}"
        )

    return "\n\n".join(sections)


def rebuild_with_revision(
    original_prompt: str,
    revision_instructions: str,
    previous_output: str,
) -> str:
    """
    Rebuild a gen prompt incorporating the previous (rejected) output and
    the Judge's revision instructions.
    """
    return (
        original_prompt.rstrip()
        + f"\n\nPREVIOUS OUTPUT (REJECTED):\n{previous_output}\n\n"
        + f"REVISION REQUIRED — fix ALL issues listed below before responding:\n{revision_instructions}"
    )
