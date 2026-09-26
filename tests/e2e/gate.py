# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Parameterised end-to-end gate assertions.

Each assertion function takes a DatasetDescriptor and a requests.Session.
No dataset-specific values (graph URI, table name, ontology URI, PK) appear
inside assertion bodies. Synthea is the first concrete DatasetDescriptor
instance; a second dataset (feature-12, MIMIC, …) adds a descriptor and any
feature-specific assertions without touching this file.
"""
from __future__ import annotations

import dataclasses
from typing import Dict, List, Optional

import requests
from rdflib import Graph
from sqlalchemy import Engine, text

from semantic_mapper.gold.models import GoldTableDefinition
from semantic_mapper.schema.mapping_output import MappingStatus, SemanticMappingReport

# Prefix block used by all queries.  No FROM clause — callers embed explicit
# GRAPH <uri> { … } blocks so the query is resilient to default-graph state.
PREFIXES = """\
PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX owl:  <http://www.w3.org/2002/07/owl#>
PREFIX xsd:  <http://www.w3.org/2001/XMLSchema#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
PREFIX fm:   <http://forgemesh.ai/ontology#>
"""


@dataclasses.dataclass
class SpotCheck:
    """A single value assertion: a specific subject+property must carry expected_value.

    Subjects are located via fm:bronzeSourceRef rather than by constructing the subject
    URI from a PK value.  This is robust to changes in id_column (e.g. a detected PK that
    is not materialised in gold, so the subject keys off fm:bronzeSourceRef), which would
    silently invalidate URI-based lookups.  fm:bronzeSourceRef is stable across
    re-materialisations because it encodes the source table PK, not the derived RDF
    subject URI.

    When expected_datatype is set the assertion uses a SPARQL typed-literal pattern
    (``"value"^^<datatype>``) so that a wrong XSD type (e.g. xsd:string instead of
    xsd:decimal) is a hard failure rather than a silent pass.
    """
    bronze_source_ref: str         # e.g. "raw_patients.id=277b6507-…" — stable entity id
    property_uri: str              # full property URI
    expected_value: str            # expected lexical value
    expected_datatype: Optional[str] = None  # full XSD URI, e.g. "…XMLSchema#decimal"


@dataclasses.dataclass
class MergeCheck:
    """Assert that two specific bronze refs map to the same RDF subject.

    Used to verify a known subject-merge case at the individual-pair level, beyond the
    per-class count delta in subject_merge_delta.  Both refs must resolve to the same
    ?s via fm:bronzeSourceRef, confirming the merge is the expected specific collision
    rather than an arbitrary one that happens to keep the count correct.

    bronze_ref_a / bronze_ref_b: stable bronze source refs, e.g. "raw_patients.id=<uuid>".
    rationale: human-readable description of why these two rows are expected to merge.
    """
    gold_table_name: str
    bronze_ref_a: str
    bronze_ref_b: str
    rationale: str = ""


@dataclasses.dataclass
class DistinctCheck:
    """Assert that two specific bronze refs map to DIFFERENT RDF subjects.

    The inverse of MergeCheck.  Used to pin a known same-attribute collision that must
    NOT collapse — e.g. two patient rows that coincidentally share an SSN but are
    genuinely different people (distinct names, genders, birthdates).  A table with a
    real per-row primary key keys its subject off that PK (directly or via
    fm:bronzeSourceRef), so coincidental attribute collisions must never merge it.

    Guards against a regression where the subject key reverts to a near-unique
    attribute and silently drops a distinct entity from the graph.

    bronze_ref_a / bronze_ref_b: stable bronze source refs, e.g. "raw_patients.id=<uuid>".
    rationale: human-readable description of why these two rows must stay distinct.
    """
    gold_table_name: str
    bronze_ref_a: str
    bronze_ref_b: str
    rationale: str = ""


@dataclasses.dataclass
class DatasetDescriptor:
    """All dataset-specific state the assertions need.  No Synthea constants inside."""
    name: str                              # for failure messages, e.g. "synthea"
    graph_uri: str
    fuseki_url: str
    dataset_name: str
    bronze_engine: Engine
    gold_engine: Engine
    rdf_base_uri: str                      # e.g. "http://mesh.local/data/"
    rdf_source_name: str                   # e.g. "synthea"
    definitions: List[GoldTableDefinition]
    mapping_report: SemanticMappingReport
    ontology_graph: Optional[Graph] = None
    ontology_graph_uri: Optional[str] = None
    """Named graph URI the ontology (with its terminology bindings) was loaded into,
    e.g. http://mesh.local/graph/synthea/ontology.  Needed by the A6 terminology
    assertions, which join external-concept bindings (ontology graph) against the
    materialised data (data graph).  None disables A6 (no-op)."""
    term_floor: float = 0.5
    """Confidence floor asserted by A6/TERM-3 — no skos:exactMatch below it."""
    spot_checks: List[SpotCheck] = dataclasses.field(default_factory=list)
    merge_checks: List[MergeCheck] = dataclasses.field(default_factory=list)
    """Known subject-merge pairs: each MergeCheck names two bronze refs expected to share
    one RDF subject.  assert_merge_subjects() verifies they resolve to the same ?s.
    """
    distinct_checks: List[DistinctCheck] = dataclasses.field(default_factory=list)
    """Known same-attribute collisions that must NOT merge: each DistinctCheck names two
    bronze refs that must resolve to DIFFERENT RDF subjects.  assert_distinct_subjects()
    verifies they do not share a ?s — guarding against a regression that keys subjects off
    a coincidentally-shared attribute and silently drops a distinct entity.
    """
    subject_merge_delta: Dict[str, int] = dataclasses.field(default_factory=dict)
    """Known subject-merge deltas: {gold_table_name: expected_merge_count}.

    When refine_id_columns keys a table off a near-unique column (a keyless dimension
    table with no detected PK, e.g. a Country keyed by name), duplicate values produce
    merged subjects: N gold rows → N-1 distinct SPARQL subjects.  Encode the confirmed
    delta here so A1 can account for it: sparql_count must equal gold_count - delta.

    A table WITH a detected per-row primary key keys off that PK (one subject per row),
    so it never appears here regardless of coincidental attribute collisions.

    Only encode a delta after verifying it by hand.  Leave the dict empty if no merges
    are expected — a non-zero merge that is NOT listed here will fail A1.
    """


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _endpoint(descriptor: DatasetDescriptor) -> str:
    return f"{descriptor.fuseki_url.rstrip('/')}/{descriptor.dataset_name}/sparql"


def _sparql(session: requests.Session, endpoint: str, query: str) -> list[dict]:
    resp = session.post(endpoint, data={"query": PREFIXES + query}, timeout=120)
    resp.raise_for_status()
    return [
        {k: v["value"] for k, v in row.items()}
        for row in resp.json()["results"]["bindings"]
    ]


def class_totals(
    descriptor: DatasetDescriptor,
    session: requests.Session,
) -> Dict[str, int]:
    """Return {gold_table_name: SPARQL-subject-count} for every class in the descriptor."""
    ep = _endpoint(descriptor)
    result: Dict[str, int] = {}
    for defn in descriptor.definitions:
        rows = _sparql(session, ep, f"""
            SELECT (COUNT(DISTINCT ?s) AS ?n)
            WHERE {{
                GRAPH <{descriptor.graph_uri}> {{
                    ?s a <{defn.primary_class_uri}> .
                }}
            }}
        """)
        result[defn.gold_table_name] = int(rows[0]["n"])
    return result


def _is_terminal_property_mapping(pm, descriptor: DatasetDescriptor) -> bool:
    """True when every value_mapping mapped_uri in a PropertyMapping resolves to an OWL class URI.

    Used by the mapping_report-based A2 to detect columns whose value_mappings point at
    class URIs rather than named individuals.  Such properties emit fm:incompleteReference
    in the graph instead of data triples.
    """
    if not pm.value_mappings:
        return False
    if descriptor.ontology_graph is None:
        return False
    from rdflib import OWL, RDF, URIRef as _URIRef
    class_uris: set[str] = {
        str(s)
        for s in descriptor.ontology_graph.subjects(RDF.type, OWL.Class)
        if isinstance(s, _URIRef)
    }
    class_frags: set[str] = {uri.split("#")[-1] for uri in class_uris if "#" in uri}
    expanded: set[str] = set(class_uris)
    for vm in pm.value_mappings:
        if vm.mapped_uri and "#" in vm.mapped_uri and vm.mapped_uri.split("#")[-1] in class_frags:
            expanded.add(vm.mapped_uri)
    return all(vm.mapped_uri in expanded for vm in pm.value_mappings if vm.mapped_uri)


def _is_terminal_object_property_spec(spec, descriptor: DatasetDescriptor) -> bool:
    """True when every value_map value in a GoldColumnSpec resolves to an OWL class URI.

    GoldColumnSpec.value_map is a plain {source_value: uri_str} dict (post-sidecar
    override).  A property whose every mapped URI is a known OWL class rather than a
    NamedIndividual will cause TripleBuilder to emit fm:incompleteReference instead of
    a data triple.

    Cross-namespace: a value_map URI whose local fragment matches a known class fragment
    is treated as a class URI even when its namespace differs (mirrors TripleBuilder.__init__).
    """
    if not spec.value_map:
        return False
    if descriptor.ontology_graph is None:
        return False
    from rdflib import OWL, RDF, URIRef as _URIRef
    class_uris: set[str] = {
        str(s)
        for s in descriptor.ontology_graph.subjects(RDF.type, OWL.Class)
        if isinstance(s, _URIRef)
    }
    class_frags: set[str] = {uri.split("#")[-1] for uri in class_uris if "#" in uri}
    expanded: set[str] = set(class_uris)
    for uri in spec.value_map.values():
        if uri and "#" in uri and uri.split("#")[-1] in class_frags:
            expanded.add(uri)
    return all(uri in expanded for uri in spec.value_map.values() if uri)


# ---------------------------------------------------------------------------
# A1 — Quantity integrity
# ---------------------------------------------------------------------------

def assert_quantity_integrity(
    descriptor: DatasetDescriptor,
    session: requests.Session,
) -> None:
    """A1: per-class bronze_count == gold_count == sparql_subject_count.

    Hard fail on any mismatch.  On failure, all three numbers are shown side
    by side.  No self-excusing divergences: if a subject merge is genuinely
    expected for a specific class, encode the delta as a hardcoded constant in
    the descriptor and subtract it from gold_count before comparing.
    """
    ep = _endpoint(descriptor)
    failures: list[str] = []

    for defn in descriptor.definitions:
        bronze_count = 0
        for silver_table in defn.source_silver_tables:
            bronze_table = silver_table[len("silver_"):]
            with descriptor.bronze_engine.connect() as conn:
                bronze_count += conn.execute(
                    text(f"SELECT COUNT(*) FROM `{bronze_table}`")
                ).scalar()

        with descriptor.gold_engine.connect() as conn:
            gold_count = conn.execute(
                text(f"SELECT COUNT(*) FROM `{defn.gold_table_name}`")
            ).scalar()

        rows = _sparql(session, ep, f"""
            SELECT (COUNT(DISTINCT ?s) AS ?n)
            WHERE {{
                GRAPH <{descriptor.graph_uri}> {{
                    ?s a <{defn.primary_class_uri}> .
                }}
            }}
        """)
        sparql_count = int(rows[0]["n"])

        delta = descriptor.subject_merge_delta.get(defn.gold_table_name, 0)
        expected_sparql = gold_count - delta
        if bronze_count != gold_count or sparql_count != expected_sparql:
            note = f"  (delta={delta} documented merge)" if delta else ""
            failures.append(
                f"  {defn.class_label or defn.gold_table_name}:\n"
                f"    bronze={bronze_count:,}  gold={gold_count:,}  sparql={sparql_count:,}"
                f"  expected_sparql={expected_sparql:,}{note}"
            )

    assert not failures, (
        f"[{descriptor.name}] A1: quantity mismatch in {len(failures)} class(es):\n"
        + "\n".join(failures)
    )


# ---------------------------------------------------------------------------
# A2 — Coverage integrity
# ---------------------------------------------------------------------------

def assert_coverage_integrity(
    descriptor: DatasetDescriptor,
    session: requests.Session,
    totals: Dict[str, int],
) -> None:
    """A2: every CONFIRMED mapping in the mapping_report produces at least one triple.

    Iterates descriptor.mapping_report.tables (not GoldDefinitionBuilder column_specs).
    This surfaces genuine disagreements between the mapping-report property URI and
    what is actually in the graph — e.g. GoldDefinitionBuilder overrides the report
    URI for marital/fips/encounter columns, so the graph has a different predicate than
    the report records.  Those 0-density rows are real signal, not noise.

    Density (present/total) is printed for every CONFIRMED mapping regardless of
    pass/fail so near-zero collapses are visible even when the test passes.

    For terminal object properties (all value_mappings point at OWL class URIs rather
    than named individuals), the primary check is FILTER(isIRI(?o)) because such
    columns emit fm:incompleteReference in the graph instead of data triples.
    fm:incompleteReference count is printed as additional diagnostic context.

    Mappings with status != CONFIRMED or no mapped_property_uri are skipped.
    """
    ep = _endpoint(descriptor)
    failures: list[str] = []
    density_lines: list[str] = []

    # Build silver_table → GoldTableDefinition lookup for class_uri and total.
    silver_to_defn: Dict[str, GoldTableDefinition] = {}
    for defn in descriptor.definitions:
        for st in defn.source_silver_tables:
            silver_to_defn[st] = defn

    for table_mapping in descriptor.mapping_report.tables:
        silver_table = f"silver_{table_mapping.source_table}"
        defn = silver_to_defn.get(silver_table)
        if defn is None:
            continue  # table not among materialised definitions

        class_uri = defn.primary_class_uri
        total = totals.get(defn.gold_table_name, 0)

        for pm in table_mapping.column_mappings:
            if pm.status != MappingStatus.CONFIRMED:
                continue
            if not pm.mapped_property_uri:
                continue

            property_uri = pm.mapped_property_uri
            source_label = f"{table_mapping.source_table}.{pm.source_column}"
            prop_short = property_uri.split("#")[-1]

            terminal = _is_terminal_property_mapping(pm, descriptor)

            if terminal:
                rows = _sparql(session, ep, f"""
                    SELECT (COUNT(?s) AS ?present)
                    WHERE {{
                        GRAPH <{descriptor.graph_uri}> {{
                            ?s a <{class_uri}> .
                            ?s <{property_uri}> ?o .
                            FILTER(isIRI(?o))
                        }}
                    }}
                """)
                present = int(rows[0]["present"])
                inc_rows = _sparql(session, ep, f"""
                    SELECT (COUNT(?s) AS ?n)
                    WHERE {{
                        GRAPH <{descriptor.graph_uri}> {{
                            ?s a <{class_uri}> .
                            ?s fm:incompleteReference "{property_uri}" .
                        }}
                    }}
                """)
                n_inc = int(inc_rows[0]["n"])
                pct = f"{present / total:.1%}" if total else "n/a"
                density_lines.append(
                    f"  {source_label:45} {prop_short:35} {present:>7}/{total}"
                    f"  ({pct})  [terminal; incompleteRef={n_inc}]"
                )
            else:
                rows = _sparql(session, ep, f"""
                    SELECT (COUNT(?s) AS ?present)
                    WHERE {{
                        GRAPH <{descriptor.graph_uri}> {{
                            ?s a <{class_uri}> .
                            ?s <{property_uri}> ?o .
                        }}
                    }}
                """)
                present = int(rows[0]["present"])
                pct = f"{present / total:.1%}" if total else "n/a"
                density_lines.append(
                    f"  {source_label:45} {prop_short:35} {present:>7}/{total}  ({pct})"
                )

            if present == 0:
                failures.append(
                    f"  {source_label} → {property_uri}  [0/{total}]"
                    + (" [terminal: value_map resolves to class URI, not individual]"
                       if terminal else "")
                )

    print(f"\n[{descriptor.name}] A2 coverage density:\n" + "\n".join(density_lines))

    assert not failures, (
        f"[{descriptor.name}] A2: {len(failures)} confirmed mapping(s) produce zero triples:\n"
        + "\n".join(failures)
    )


# ---------------------------------------------------------------------------
# A3 — Value spot-checks
# ---------------------------------------------------------------------------

def assert_value_spot_checks(
    descriptor: DatasetDescriptor,
    session: requests.Session,
) -> None:
    """A3: specific known values survive transforms intact from bronze through to RDF.

    No-op when descriptor.spot_checks is empty.  Subjects are located via
    fm:bronzeSourceRef rather than by constructing a subject URI — bronzeSourceRef is
    stable across re-materialisations regardless of which column refine_id_columns
    selects as id_column (e.g. PK-exclusion policy making the fallback non-obvious).

    When expected_datatype is set, a SPARQL typed-literal pattern is used so that the
    wrong XSD type (xsd:string instead of xsd:decimal) is a hard failure.
    """
    if not descriptor.spot_checks:
        return

    ep = _endpoint(descriptor)
    failures: list[str] = []
    _XSD = "http://www.w3.org/2001/XMLSchema#"

    for check in descriptor.spot_checks:
        ref = check.bronze_source_ref.replace('"', '\\"')  # escape for SPARQL literal

        if check.expected_datatype:
            dt = check.expected_datatype
            if dt.startswith(_XSD):
                literal = f'"{check.expected_value}"^^xsd:{dt[len(_XSD):]}'
            else:
                literal = f'"{check.expected_value}"^^<{dt}>'

            rows = _sparql(session, ep, f"""
                SELECT (COUNT(*) AS ?n)
                WHERE {{
                    GRAPH <{descriptor.graph_uri}> {{
                        ?s fm:bronzeSourceRef "{ref}" .
                        ?s <{check.property_uri}> {literal} .
                    }}
                }}
            """)
            matched = int(rows[0]["n"])
            if matched == 0:
                diag = _sparql(session, ep, f"""
                    SELECT ?val (datatype(?val) AS ?dt)
                    WHERE {{
                        GRAPH <{descriptor.graph_uri}> {{
                            ?s fm:bronzeSourceRef "{ref}" .
                            ?s <{check.property_uri}> ?val .
                        }}
                    }}
                    LIMIT 3
                """)
                actual = [(r.get("val"), r.get("dt")) for r in diag]
                failures.append(
                    f"  bronzeRef={check.bronze_source_ref}\n"
                    f"    property  : {check.property_uri}\n"
                    f"    expected  : {literal}\n"
                    f"    got       : {actual!r}"
                )
        else:
            rows = _sparql(session, ep, f"""
                SELECT ?val
                WHERE {{
                    GRAPH <{descriptor.graph_uri}> {{
                        ?s fm:bronzeSourceRef "{ref}" .
                        ?s <{check.property_uri}> ?val .
                    }}
                }}
            """)
            got = [r["val"] for r in rows]
            if check.expected_value not in got:
                failures.append(
                    f"  bronzeRef={check.bronze_source_ref}\n"
                    f"    property : {check.property_uri}\n"
                    f"    expected : {check.expected_value!r}\n"
                    f"    got      : {got!r}"
                )

    assert not failures, (
        f"[{descriptor.name}] A3: {len(failures)} spot-check(s) failed:\n"
        + "\n".join(failures)
    )


# ---------------------------------------------------------------------------
# A1b — Subject merge pair verification
# ---------------------------------------------------------------------------

def assert_merge_subjects(
    descriptor: DatasetDescriptor,
    session: requests.Session,
) -> None:
    """A1b: verify that each MergeCheck pair resolves to exactly one shared subject.

    For each (bronze_ref_a, bronze_ref_b) pair, both refs must map to the same ?s
    via fm:bronzeSourceRef.  This confirms the subject-merge is the specific known
    collision rather than an unrelated count shortfall that happens to match the delta.

    No-op when descriptor.merge_checks is empty.
    """
    if not descriptor.merge_checks:
        return

    ep = _endpoint(descriptor)
    failures: list[str] = []

    for check in descriptor.merge_checks:
        ref_a = check.bronze_ref_a.replace('"', '\\"')
        ref_b = check.bronze_ref_b.replace('"', '\\"')

        rows = _sparql(session, ep, f"""
            SELECT ?s
            WHERE {{
                GRAPH <{descriptor.graph_uri}> {{
                    ?s fm:bronzeSourceRef "{ref_a}" .
                }}
            }}
        """)
        subjects_a = {r["s"] for r in rows}

        rows = _sparql(session, ep, f"""
            SELECT ?s
            WHERE {{
                GRAPH <{descriptor.graph_uri}> {{
                    ?s fm:bronzeSourceRef "{ref_b}" .
                }}
            }}
        """)
        subjects_b = {r["s"] for r in rows}

        shared = subjects_a & subjects_b
        if not shared:
            failures.append(
                f"  [{check.gold_table_name}] Expected merge not found:\n"
                f"    ref_a={check.bronze_ref_a!r} → subjects={subjects_a!r}\n"
                f"    ref_b={check.bronze_ref_b!r} → subjects={subjects_b!r}\n"
                f"    rationale: {check.rationale}"
            )

    assert not failures, (
        f"[{descriptor.name}] A1b: {len(failures)} expected merge pair(s) did not share a subject:\n"
        + "\n".join(failures)
    )


def assert_distinct_subjects(
    descriptor: DatasetDescriptor,
    session: requests.Session,
) -> None:
    """A1b (inverse): verify that each DistinctCheck pair resolves to DIFFERENT subjects.

    For each (bronze_ref_a, bronze_ref_b) pair, the refs must NOT share any ?s via
    fm:bronzeSourceRef.  This pins a known same-attribute collision (e.g. two patient
    rows sharing one SSN) as two distinct entities — catching a regression that keys the
    subject off the shared attribute and silently merges (and drops) a real entity.

    Each ref is also required to resolve to at least one subject, so a typo'd ref that
    matches nothing cannot pass vacuously.

    No-op when descriptor.distinct_checks is empty.
    """
    if not descriptor.distinct_checks:
        return

    ep = _endpoint(descriptor)
    failures: list[str] = []

    for check in descriptor.distinct_checks:
        ref_a = check.bronze_ref_a.replace('"', '\\"')
        ref_b = check.bronze_ref_b.replace('"', '\\"')

        rows = _sparql(session, ep, f"""
            SELECT ?s
            WHERE {{
                GRAPH <{descriptor.graph_uri}> {{
                    ?s fm:bronzeSourceRef "{ref_a}" .
                }}
            }}
        """)
        subjects_a = {r["s"] for r in rows}

        rows = _sparql(session, ep, f"""
            SELECT ?s
            WHERE {{
                GRAPH <{descriptor.graph_uri}> {{
                    ?s fm:bronzeSourceRef "{ref_b}" .
                }}
            }}
        """)
        subjects_b = {r["s"] for r in rows}

        if not subjects_a or not subjects_b:
            failures.append(
                f"  [{check.gold_table_name}] DistinctCheck ref matched no subject:\n"
                f"    ref_a={check.bronze_ref_a!r} → subjects={subjects_a!r}\n"
                f"    ref_b={check.bronze_ref_b!r} → subjects={subjects_b!r}\n"
                f"    rationale: {check.rationale}"
            )
            continue

        shared = subjects_a & subjects_b
        if shared:
            failures.append(
                f"  [{check.gold_table_name}] Rows expected to stay distinct merged onto one subject:\n"
                f"    ref_a={check.bronze_ref_a!r}\n"
                f"    ref_b={check.bronze_ref_b!r}\n"
                f"    shared={shared!r}\n"
                f"    rationale: {check.rationale}"
            )

    assert not failures, (
        f"[{descriptor.name}] A1b: {len(failures)} distinct pair(s) failed:\n"
        + "\n".join(failures)
    )


# ---------------------------------------------------------------------------
# A4 — Provenance integrity
# ---------------------------------------------------------------------------

def assert_provenance_integrity(
    descriptor: DatasetDescriptor,
    session: requests.Session,
) -> None:
    """A4: every materialised ENTITY carries both fm:bronzeSourceRef and fm:goldRunId.

    A materialiser that drops either predicate makes prove_answer return
    proof_failed silently — this assertion catches that before it reaches prod.
    On failure the first 5 offending subjects are listed per missing predicate.

    Scoped to entity subjects — instances of a gold-table class (descriptor.definitions
    primary_class_uri).  Reference/concept individuals (e.g. terminology code concepts
    materialised in the data graph, one per distinct SNOMED/RxNorm code) deliberately
    have NO bronze row — they are not gold-row entities — so requiring lineage of them
    would be wrong.  Restricting to entity classes is precise, not a weakening: every
    gold-row subject is still checked.
    """
    entity_classes = sorted({
        d.primary_class_uri for d in descriptor.definitions if d.primary_class_uri
    })
    if not entity_classes:
        return
    values = " ".join(f"<{c}>" for c in entity_classes)
    ep = _endpoint(descriptor)
    failures: list[str] = []

    for predicate in ("fm:bronzeSourceRef", "fm:goldRunId"):
        rows = _sparql(session, ep, f"""
            SELECT (COUNT(DISTINCT ?s) AS ?missing)
            WHERE {{
                GRAPH <{descriptor.graph_uri}> {{
                    VALUES ?type {{ {values} }}
                    ?s a ?type .
                    FILTER NOT EXISTS {{ ?s {predicate} ?v }}
                }}
            }}
        """)
        missing = int(rows[0]["missing"])
        if missing > 0:
            sample_rows = _sparql(session, ep, f"""
                SELECT DISTINCT ?s
                WHERE {{
                    GRAPH <{descriptor.graph_uri}> {{
                        VALUES ?type {{ {values} }}
                        ?s a ?type .
                        FILTER NOT EXISTS {{ ?s {predicate} ?v }}
                    }}
                }}
                LIMIT 5
            """)
            sample = [r["s"] for r in sample_rows]
            failures.append(
                f"  {missing:,} entity subject(s) missing {predicate}.\n"
                f"  First 5: {sample}"
            )

    assert not failures, (
        f"[{descriptor.name}] A4: provenance gaps found:\n" + "\n".join(failures)
    )


# ---------------------------------------------------------------------------
# A5 — Ontology coherence: SOURCE_COLUMN_COLLISION violations flagged
# ---------------------------------------------------------------------------

def assert_coherence_violations_flagged(
    descriptor: DatasetDescriptor,
    session: requests.Session,
) -> None:
    """A5: SOURCE_COLUMN_COLLISION violations are detected and flagged PENDING_REVIEW.

    Two-part check:
      (a) OntologyCoherenceChecker detects at least one SOURCE_COLUMN_COLLISION error
          in the ontology graph.
      (b) Every detected collision's source column is no longer CONFIRMED in the
          mapping report — the pipeline acted on the detection.

    This assertion does NOT check what value or property the column resolves to.
    A column parked as PENDING_REVIEW is correctly absent from A2 and A3; that
    absence is a tracked state (parked pending literal-vs-FK decision), not a
    data-correctness claim.

    No-op when descriptor.ontology_graph is None.
    """
    if descriptor.ontology_graph is None:
        return

    from semantic_mapper.ontology.coherence import OntologyCoherenceChecker
    from semantic_mapper.schema.mapping_output import MappingStatus

    coherence = OntologyCoherenceChecker(descriptor.ontology_graph).check()
    collisions = [i for i in coherence.issues if i.category == "SOURCE_COLUMN_COLLISION"]

    failures: list[str] = []
    for issue in collisions:
        table, col = issue.subject_uri.split(".", 1)
        tm = next(
            (t for t in descriptor.mapping_report.tables if t.source_table == table),
            None,
        )
        if tm is None:
            pm = None
        else:
            pm = next(
                (p for p in tm.column_mappings if p.source_column == col),
                None,
            )
        if pm is None or pm.status == MappingStatus.CONFIRMED:
            failures.append(
                f"  {issue.subject_uri}: detected as SOURCE_COLUMN_COLLISION "
                f"but mapping status is {pm.status if pm else 'NOT FOUND'} "
                "(expected PENDING_REVIEW)"
            )

    assert collisions, (
        f"[{descriptor.name}] A5: no SOURCE_COLUMN_COLLISION violations detected — "
        "expected at least one for this dataset"
    )
    assert not failures, (
        f"[{descriptor.name}] A5: {len(failures)} violation(s) detected but not flagged:\n"
        + "\n".join(failures)
    )


# ---------------------------------------------------------------------------
# A6 — External terminology binding (TERM-2 / TERM-3 / TERM-6)
# ---------------------------------------------------------------------------

def assert_terminology_provenance(
    descriptor: DatasetDescriptor,
    session: requests.Session,
) -> None:
    """A6/TERM-2 + TERM-3: every external concept binding carries provenance and
    honours the confidence floor — over the materialised ontology graph in Fuseki.

    TERM-2: every individual with a skos:exactMatch to an external concept carries
            at least one fm:terminologySystem and one fm:terminologyBindingConfidence.
            No exactMatch exists without them.
    TERM-3: no asserted skos:exactMatch has a binding confidence below term_floor.

    No-op when the descriptor names no ontology graph.  Fails if the ontology graph
    carries zero bindings — a silent loss of the whole feature would otherwise pass.
    """
    if not descriptor.ontology_graph_uri:
        return
    ep = _endpoint(descriptor)

    # Bindings live in either graph: low-cardinality vocab (gender) is bound in the
    # ontology graph at generation; high-cardinality code concepts (SNOMED/RxNorm)
    # are minted into the data graph at materialisation. Check both via GRAPH ?g.
    n_bound = int(_sparql(session, ep, """
        SELECT (COUNT(DISTINCT ?s) AS ?n)
        WHERE { GRAPH ?g { ?s skos:exactMatch ?c . } }
    """)[0]["n"])
    assert n_bound > 0, (
        f"[{descriptor.name}] A6/TERM-2: no skos:exactMatch bindings in any graph — "
        "the terminology binding pass produced nothing."
    )

    failures: list[str] = []
    for predicate in ("fm:terminologySystem", "fm:terminologyBindingConfidence"):
        missing = int(_sparql(session, ep, f"""
            SELECT (COUNT(DISTINCT ?s) AS ?n)
            WHERE {{ GRAPH ?g {{
                ?s skos:exactMatch ?c .
                FILTER NOT EXISTS {{ ?s {predicate} ?v }}
            }} }}
        """)[0]["n"])
        if missing:
            failures.append(f"  TERM-2: {missing} binding(s) missing {predicate}")

    sub_floor = int(_sparql(session, ep, f"""
        SELECT (COUNT(DISTINCT ?s) AS ?n)
        WHERE {{ GRAPH ?g {{
            ?s skos:exactMatch ?c ; fm:terminologyBindingConfidence ?conf .
            FILTER(xsd:decimal(?conf) < {descriptor.term_floor})
        }} }}
    """)[0]["n"])
    if sub_floor:
        failures.append(
            f"  TERM-3: {sub_floor} binding(s) below confidence floor {descriptor.term_floor}"
        )

    assert not failures, (
        f"[{descriptor.name}] A6: terminology-binding provenance/floor violations "
        f"({n_bound} bound individual(s)):\n" + "\n".join(failures)
    )


def assert_terminology_queryable(
    descriptor: DatasetDescriptor,
    session: requests.Session,
) -> None:
    """A6/TERM-6: an external concept binding is queryable end-to-end.

    A query that filters by an external concept IRI returns matching data rows *via*
    the skos:exactMatch link — proving the binding lets a query reach the standard
    term, rather than being decorative.  Joins the ontology graph (individual →
    exactMatch → external concept) against the data graph (data subject → property →
    that individual).  Generic: passes if ANY bound individual is referenced by data,
    and reports which external systems were reachable.

    No-op when the descriptor names no ontology graph.
    """
    if not descriptor.ontology_graph_uri:
        return
    ep = _endpoint(descriptor)

    # The bound individual may live in either graph (ontology for vocab, data for
    # code concepts); the data subject → individual link is always in the data graph.
    rows = _sparql(session, ep, f"""
        SELECT ?sys (COUNT(DISTINCT ?s) AS ?rows) (COUNT(DISTINCT ?concept) AS ?concepts)
        WHERE {{
            GRAPH ?g {{
                ?ind skos:exactMatch ?concept ;
                     fm:terminologySystem ?sys .
            }}
            GRAPH <{descriptor.graph_uri}> {{ ?s ?p ?ind . }}
        }}
        GROUP BY ?sys
    """)
    reachable = {r["sys"]: (int(r["rows"]), int(r["concepts"])) for r in rows}
    total_rows = sum(v[0] for v in reachable.values())

    summary = "\n".join(
        f"    {sys}: {rws} data row(s) via {cs} concept(s)"
        for sys, (rws, cs) in sorted(reachable.items())
    ) or "    (none reachable)"
    assert total_rows > 0, (
        f"[{descriptor.name}] A6/TERM-6: no external concept binding is reachable from "
        f"the data graph via skos:exactMatch — the binding is decorative, not queryable.\n"
        f"  ontology graph: <{og}>\n  reachable systems:\n{summary}"
    )
    print(f"\n[{descriptor.name}] A6/TERM-6 reachable external concepts:\n{summary}")
