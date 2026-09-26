# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Post-generation OWL correctness checks for the iterative ontology generator.

Three structural postconditions caught by live-graph analysis of MIMIC-III (May 2026):

  G1 — Scalar-as-ObjectProperty
       A column whose source type is a scalar (DATETIME, FLOAT, INT, …) must never
       be declared owl:ObjectProperty.  The generator misclassified Transfer.outtime
       and Transfer.los as ObjectProperty with rdfs:range Icustay, causing 258
       fm:incompleteReference triples instead of typed literals.

  G2 — Split-pass column coverage
       After _split_multi_domain_dt_properties, every (table, column) pair that
       appears in a skos:altLabel annotation must be reachable via a property whose
       rdfs:domain includes that table's class.  The split pass failed to produce
       InputeventsCv-scoped properties for columns shared with InputeventsMv,
       leaving 11 cross-table predicates on CV subjects.

  G3 — Cross-table property reuse
       When a new table fragment is merged into the accumulating graph, no property
       it introduces may already have a DIFFERENT class in its rdfs:domain.
       IcdDiagnose reused hasIcdProcedureShortTitle / hasIcdProcedureLongTitle from
       IcdProcedure because raw_d_icd_diagnoses and raw_d_icd_procedures are
       structurally identical; both title columns ended up under procedure predicates.

Each function returns a (possibly empty) list of human-readable violation strings.
An empty list means the OWL passes that check.  Callers should log violations and,
in CI, treat a non-empty list as a hard failure.
"""
from __future__ import annotations

from typing import List

from rdflib import OWL, RDF, RDFS, Graph, URIRef
from rdflib.namespace import SKOS

# Source-type tokens that indicate a scalar value — these columns must never be
# declared owl:ObjectProperty regardless of what the LLM outputs.
_SCALAR_TYPES = frozenset({
    "datetime", "date", "time", "timestamp",
    "int", "integer", "bigint", "smallint", "tinyint",
    "float", "double", "decimal", "numeric", "real",
    "boolean", "bit",
    # MySQL-style aliases
    "int unsigned", "bigint unsigned", "tinyint(1)", "tinyint unsigned",
})


def _is_scalar(inferred_type: str) -> bool:
    t = inferred_type.lower().strip()
    if t in _SCALAR_TYPES:
        return True
    # Catch parameterised variants: decimal(10,2), varchar is NOT scalar
    base = t.split("(")[0].strip()
    return base in _SCALAR_TYPES


def check_no_scalar_as_object_property(
    graph: Graph,
    column_contexts: list,  # List[ColumnContext]
) -> List[str]:
    """G1: no scalar-type column may be declared owl:ObjectProperty.

    Matches each ColumnContext against OWL properties via skos:altLabel
    (format: "<table_name>.<col_name>" or bare "<col_name>").  Reports a
    violation for every property that is both owl:ObjectProperty and mapped
    to a scalar-type column.

    Args:
        graph: the generated or accumulated OWL graph.
        column_contexts: List[ColumnContext] for the table(s) in scope.

    Returns:
        List of violation strings (empty = no violations).
    """
    # Build lookup: altLabel token → set of properties that carry it
    alt_to_props: dict[str, set] = {}
    for prop in graph.subjects(RDF.type, OWL.ObjectProperty):
        for label in graph.objects(prop, SKOS.altLabel):
            token = str(label).lower()
            alt_to_props.setdefault(token, set()).add(prop)
        # Also index by rdfs:label
        for label in graph.objects(prop, RDFS.label):
            token = str(label).lower()
            alt_to_props.setdefault(token, set()).add(prop)

    violations = []
    for col in column_contexts:
        if not _is_scalar(col.inferred_type):
            continue
        if col.is_fk:
            # A FK column carries an entity reference — ObjectProperty is correct
            # even when the storage type is INT/BIGINT (surrogate key pattern).
            continue
        # Try full "table.col" key first, then bare col name
        for key in (f"{col.table_name}.{col.col_name}", col.col_name):
            for prop in alt_to_props.get(key.lower(), set()):
                local = str(prop).split("#")[-1]
                violations.append(
                    f"G1: {local} is owl:ObjectProperty but column "
                    f"'{col.table_name}.{col.col_name}' has scalar type "
                    f"'{col.inferred_type}' — must be DatatypeProperty"
                )
    return violations


def check_split_column_coverage(
    graph: Graph,
    table_to_class_uri: dict[str, str],
) -> List[str]:
    """G2: after split, every (table, column) altLabel has a domain-matched property.

    For each property P in the graph that carries a skos:altLabel of the form
    "raw_<table>.<col>" (or "<table>.<col>"), verifies that there exists at
    least one property whose rdfs:domain matches the corresponding class URI
    and whose altLabel references that same column.

    Args:
        graph: the OWL graph after _split_multi_domain_dt_properties.
        table_to_class_uri: maps source table name → ontology class URI,
            e.g. {"raw_inputevents_cv": "http://...#InputeventsCv"}.

    Returns:
        List of violation strings (empty = no violations).
    """
    # Build: (class_uri, col_token) → True if at least one property covers it
    covered: dict[tuple, bool] = {}

    for prop in graph.subjects(RDF.type, OWL.DatatypeProperty):
        domains = set(str(d) for d in graph.objects(prop, RDFS.domain))
        for label in graph.objects(prop, SKOS.altLabel):
            token = str(label).lower()
            # Try to match "table.col" pattern
            if "." in token:
                tbl, col = token.split(".", 1)
                for full_table, class_uri in table_to_class_uri.items():
                    if full_table.lower().endswith(tbl) or tbl in full_table.lower():
                        if class_uri in domains:
                            covered[(class_uri, col)] = True
            else:
                for class_uri in domains:
                    covered[(class_uri, token)] = True

    # Now check: for every (table, col) altLabel, the corresponding class is covered
    violations = []
    for prop in graph.subjects(RDF.type, OWL.DatatypeProperty):
        domains_for_prop = set(str(d) for d in graph.objects(prop, RDFS.domain))
        for label in graph.objects(prop, SKOS.altLabel):
            token = str(label).lower()
            if "." not in token:
                continue
            tbl, col = token.split(".", 1)
            for full_table, class_uri in table_to_class_uri.items():
                if not (full_table.lower().endswith(tbl) or tbl in full_table.lower()):
                    continue
                if class_uri not in domains_for_prop and not covered.get((class_uri, col)):
                    local = str(prop).split("#")[-1]
                    violations.append(
                        f"G2: column '{full_table}.{col}' has altLabel on {local} "
                        f"but no property with rdfs:domain={class_uri.split('#')[-1]} "
                        f"covers it — split-pass left this table without scoped property"
                    )
    return violations


def check_no_cross_table_property_reuse(
    new_fragment: Graph,
    accumulating: Graph,
    new_class_uri: str,
) -> List[str]:
    """G3: no property in new_fragment may already belong to a different class's domain.

    When a new table's OWL fragment is about to be merged into the accumulating
    graph, every property it introduces should be either:
      - brand new (not yet in accumulating), or
      - already scoped to new_class_uri's domain (legitimate extension).

    A property that already has a DIFFERENT class in its rdfs:domain in the
    accumulating graph must not be reused for new_class_uri — the generator must
    emit a fresh class-scoped property instead.

    Args:
        new_fragment: OWL fragment the generator produced for the new table.
        accumulating: the OWL graph accumulated so far (does not include new_fragment).
        new_class_uri: the ontology class URI for the new table being merged.

    Returns:
        List of violation strings (empty = no violations).
    """
    new_class = URIRef(new_class_uri)
    violations = []

    for prop in new_fragment.subjects(RDF.type, None):
        # Only check OWL property declarations
        types = set(new_fragment.objects(prop, RDF.type))
        if OWL.ObjectProperty not in types and OWL.DatatypeProperty not in types:
            continue

        # Check if this property already exists in the accumulating graph
        existing_domains = set(accumulating.objects(URIRef(prop), RDFS.domain))
        if not existing_domains:
            continue  # brand new — fine

        # If it exists and has domains, those domains must include new_class or be empty
        for existing_domain in existing_domains:
            if existing_domain != new_class:
                local = str(prop).split("#")[-1]
                violations.append(
                    f"G3: property {local} already has rdfs:domain "
                    f"{str(existing_domain).split('#')[-1]} in accumulating graph "
                    f"but new fragment re-declares it for "
                    f"{new_class_uri.split('#')[-1]} — "
                    f"generate a class-scoped sibling instead"
                )
                break  # one violation per property is enough

    return violations
