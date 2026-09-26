# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Synthea end-to-end materialisation gate.

Re-materialises the full bronze → silver → gold → RDF chain on every run and
asserts the far end via Fuseki HTTP queries.  The gate owns the named graph: it
clears and rebuilds it, so it is not a stale-triplestore query.

Run with:
    SYNTHEA_E2E=1 python -m pytest tests/synthea/test_e2e_gate.py -v

Pre-conditions (gate fails fast if not met):
  1. Fuseki reachable at localhost:3030.
  2. Bronze MySQL reachable at localhost:3307.
  3. Corrections store is warm (run_map must make 0 LLM calls).
     To warm: python main.py --project synthea --phases inspect,map

The Synthea descriptor at the bottom of this file is the dataset-specific fixture.
Assertion logic (A1–A4) lives in tests/e2e/gate.py and knows nothing about Synthea.
Adding a second dataset gate = add a descriptor fixture + four test functions here (or
in a new file that imports from tests.e2e.gate).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import requests
from sqlalchemy import create_engine, text

from main import run_inspect, run_map, run_silver, run_gold, run_rdf
from semantic_mapper.config.settings import Settings
from semantic_mapper.gold.builder import GoldDefinitionBuilder, refine_id_columns
from semantic_mapper.ontology.loader import OntologyLoader
from semantic_mapper.project.resolver import resolve_project, apply_project_to_env
from semantic_mapper.schema.mapping_output import SemanticMappingReport

from tests.e2e.gate import (
    DatasetDescriptor,
    DistinctCheck,
    SpotCheck,
    assert_coherence_violations_flagged,
    assert_coverage_integrity,
    assert_distinct_subjects,
    assert_provenance_integrity,
    assert_quantity_integrity,
    assert_terminology_provenance,
    assert_terminology_queryable,
    assert_value_spot_checks,
    class_totals,
)

# ---------------------------------------------------------------------------
# Gate flag — skip unless explicitly opted in
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not os.environ.get("SYNTHEA_E2E"),
    reason="Set SYNTHEA_E2E=1 to run the Synthea end-to-end gate",
)

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Infrastructure fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def fuseki_session():
    """requests.Session pointed at localhost:3030.  Skips if Fuseki unreachable."""
    s = requests.Session()
    s.headers.update({"Accept": "application/sparql-results+json"})
    try:
        s.get("http://localhost:3030/$/ping", timeout=2).raise_for_status()
    except Exception:
        pytest.skip("Fuseki not reachable at localhost:3030")
    return s


# ---------------------------------------------------------------------------
# Synthea descriptor (only Synthea-specific values live here)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def synthea_descriptor(fuseki_session):
    """Apply Synthea project config, run full pipeline warm, return descriptor.

    Pipeline phases:
      inspect  — profiles bronze tables (reads ~200 k rows, no LLM)
      map      — resolves mappings; must be 0 LLM calls (warm store)
      silver   — transforms bronze → silver
      gold     — projects silver → per-class gold tables
      rdf      — clears Synthea named graph, emits triples to Fuseki

    The rdf phase calls FusekiWriter.initialise() which issues DROP SILENT GRAPH
    before loading, so every gate run operates on a freshly materialised dataset.
    """
    # 1. Apply project config
    project = resolve_project("synthea")
    apply_project_to_env(project)
    os.environ["FORGEMESH_PROJECT"] = project.name
    settings = Settings()

    # 2. Verify MySQL reachable
    try:
        bronze_engine = create_engine(settings.DATABASE_URL)
        with bronze_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        pytest.skip(f"Bronze MySQL not reachable: {exc}")

    gold_engine = create_engine(settings.GOLD_DATABASE_URL)

    # 3. Run full pipeline.  map must complete with 0 LLM calls.
    run_inspect(settings)

    run_map(settings)

    # Warmth check: every column mapping must have resolved without LLM (Tier 1+2 only).
    # Value-mappings always re-run via LLM regardless of store state — they are
    # excluded here because they are not column mappings and are deterministic
    # (same low-cardinality values → same output).  prompt_tokens == 0 would be too
    # strict: it fires on value_mapping LLM calls, masking the real warmth signal.
    with open(Path(settings.QUERY_MAPPING_REPORT_PATH)) as _wf:
        _warm_report = SemanticMappingReport.model_validate(json.load(_wf))
    _llm_columns = [
        f"{_tm.source_table}.{_pm.source_column}"
        for _tm in _warm_report.tables
        for _pm in _tm.column_mappings
        if any(e.strategy == "LLMStrategy" for e in (_pm.evidence_trail or []))
    ]
    assert not _llm_columns, (
        "[synthea gate] Corrections store is cold — column mappings used LLM Tier 3.\n"
        f"  Columns resolved via LLM: {_llm_columns}\n"
        "  Warm the store first, then re-run the gate:\n"
        "  python main.py --project synthea --phases inspect,map"
    )

    run_silver(settings)
    run_gold(settings)
    run_rdf(settings)   # drops + rebuilds http://mesh.local/graph/synthea

    # 4. Build definitions exactly as run_rdf does, including refine_id_columns,
    #    so class_label / id_column / source_silver_tables are consistent with
    #    what was actually materialised.
    with open(Path(settings.QUERY_MAPPING_REPORT_PATH)) as f:
        report = SemanticMappingReport.model_validate(json.load(f))

    ontology_graph = None
    if settings.ONTOLOGY_PATH:
        try:
            ontology_graph = OntologyLoader.load(settings.ONTOLOGY_PATH)
        except Exception:
            pass

    builder = GoldDefinitionBuilder()
    definitions = builder.build(report, ontology_graph=ontology_graph)
    # Mirror run_rdf exactly: tables with a detected PK trust their PK-derived id_column,
    # so refine must not override it with a name/code heuristic.  Omitting this argument
    # would make the descriptor's id_column diverge from what was actually materialised.
    _pk_source_tables = {tm.source_table for tm in report.tables if tm.primary_key_columns}
    definitions = refine_id_columns(definitions, gold_engine, _pk_source_tables)

    return DatasetDescriptor(
        name="synthea",
        graph_uri=settings.RDF_NAMED_GRAPH,           # http://mesh.local/graph/synthea
        fuseki_url=settings.FUSEKI_URL,                # http://localhost:3030
        dataset_name=settings.RDF_DATASET_NAME,        # synthea-mesh
        bronze_engine=bronze_engine,
        gold_engine=gold_engine,
        rdf_base_uri=settings.RDF_BASE_URI,            # http://mesh.local/data/
        rdf_source_name=settings.RDF_SOURCE_NAME,      # synthea
        definitions=definitions,
        mapping_report=report,
        ontology_graph=ontology_graph,
        ontology_graph_uri=settings.ONTOLOGY_NAMED_GRAPH,   # …/graph/synthea/ontology
        term_floor=settings.TERM_BIND_CONFIDENCE_FLOOR,
        spot_checks=[
            # Check 1 — patient birthdate through DateNormaliser (ts=True, fmt YYYY-MM-DD).
            # URI: hasBirthdate (lowercase d, confirmed from Synthea ontology).
            # bronzeSourceRef is stable regardless of which column refine_id_columns picks
            # as id_column (raw_patients.id is a detected PK, not materialised in gold, so
            # the subject keys off fm:bronzeSourceRef — which encodes that same PK).
            # No datatype: TEXT dtype → _xsd_type sniffs non-integer/non-decimal → xsd:string.
            SpotCheck(
                bronze_source_ref="raw_patients.id=277b6507-2c1d-86f4-0496-6cc54feec20a",
                property_uri="http://mesh.local/ontology/synthea#hasBirthdate",
                expected_value="2023-10-23",
            ),
            # A3 spot check 2 (base_encounter_cost → "85.55"^^xsd:decimal) REMOVED — KNOWN-PARKED.
            # SOURCE_COLUMN_COLLISION: raw_encounters.base_encounter_cost is annotated on both
            # hasEncounterBaseCost (ObjectProperty, fm:sourceForeignKey) and hasBaseEncounterCost
            # (DatatypeProperty, fm:sourceColumn). Coherence check (A5) now flags this column as
            # PENDING_REVIEW; no triple is emitted for it. The cost value is absent from the graph
            # pending the separate literal-vs-FK decision.
            # Restore this spot check once the ontology collision is resolved.
        ],
        # No patient subject merge: raw_patients.id is a detected per-row primary key, so
        # refine_id_columns trusts it (the PK is not materialised in gold, so the subject
        # keys off fm:bronzeSourceRef — which encodes that same PK → one subject per row).
        # bronze == gold == sparql == 585.  The two rows sharing SSN 999-53-6287 are
        # genuinely different people (distinct names, genders, birthdates, cities); they
        # must NOT merge — keying patients off the coincidentally-shared ssn would silently
        # drop one real patient (585 → 584).  Hence subject_merge_delta is empty.
        subject_merge_delta={},
        # Pin the known SSN collision as two DISTINCT subjects.  A1b fails if these two
        # refs share a subject, which would mean a regression re-keyed patients off the
        # near-unique ssn attribute and merged two distinct people.
        distinct_checks=[
            DistinctCheck(
                gold_table_name="gold_patient",
                bronze_ref_a="raw_patients.id=910dc7b4-d4ab-b739-2ac2-67e682a09dfd",
                bronze_ref_b="raw_patients.id=00a43dcc-170b-a5d3-f7bb-9fb033ab77c1",
                rationale="shared SSN 999-53-6287 but different people (M Arlington b.1963 "
                          "vs F Boston b.1964) — must stay distinct; id PK keys one subject per row",
            ),
        ],
    )


@pytest.fixture(scope="session")
def synthea_class_totals(synthea_descriptor, fuseki_session):
    """SPARQL-derived subject counts per class; shared by A1 and A2."""
    return class_totals(synthea_descriptor, fuseki_session)


# ---------------------------------------------------------------------------
# Gate tests
# ---------------------------------------------------------------------------

def test_a1_quantity_integrity(synthea_descriptor, fuseki_session, synthea_class_totals):
    """A1: bronze == gold == SPARQL subject count per class."""
    assert_quantity_integrity(synthea_descriptor, fuseki_session)


def test_a1b_distinct_subjects(synthea_descriptor, fuseki_session):
    """A1b: the two raw_patients rows sharing SSN 999-53-6287 stay DISTINCT subjects.

    raw_patients.id is a detected per-row primary key, so each patient row mints its own
    subject (via fm:bronzeSourceRef, which encodes that PK).  The two rows that share the
    SSN are genuinely different people, so they must resolve to different subjects.  If
    they share a subject, a regression has re-keyed patients off the coincidental ssn
    attribute and silently merged (dropping) a distinct patient.
    """
    assert_distinct_subjects(synthea_descriptor, fuseki_session)


def test_a2_coverage_integrity(synthea_descriptor, fuseki_session, synthea_class_totals):
    """A2: every CONFIRMED mapping produces at least one triple for its class."""
    assert_coverage_integrity(synthea_descriptor, fuseki_session, synthea_class_totals)


def test_a3_value_spot_checks(synthea_descriptor, fuseki_session):
    """A3: specific values survive transforms intact end-to-end.

    Check 1 — patient birthdate (DateNormaliser path, lexical check):
      entity   : patient/277b6507-2c1d-86f4-0496-6cc54feec20a
      property : hasBirthDate
      expected : "2023-10-23"

    Check 2 (base_encounter_cost) REMOVED — KNOWN-PARKED. See A3 descriptor comment.

    No second data check: conditions/medications/procedures key off _gold_id (auto-increment)
    so subject URIs are not stable across runs — spot-checks on those tables would be
    non-deterministic.  They are covered by A1 (counts) and A2 (density).
    """
    assert_value_spot_checks(synthea_descriptor, fuseki_session)


def test_a4_provenance_integrity(synthea_descriptor, fuseki_session):
    """A4: every subject carries fm:bronzeSourceRef and fm:goldRunId."""
    assert_provenance_integrity(synthea_descriptor, fuseki_session)


def test_a5_coherence_violations_flagged(synthea_descriptor, fuseki_session):
    """A5: FK+DT source-column collisions are detected and flagged PENDING_REVIEW.

    Two-part check:
      (a) OntologyCoherenceChecker finds at least one SOURCE_COLUMN_COLLISION error.
      (b) Every detected collision column is no longer CONFIRMED in the mapping report.

    Concrete Synthea instance: raw_encounters.base_encounter_cost appears in the
    detected collision set.  The column's cost data is absent from the graph — this is
    a tracked-parked state pending the literal-vs-FK decision, not a data-correctness
    claim.

    # base_encounter_cost in A2: naturally absent (PENDING_REVIEW, not CONFIRMED).
    # The 3 remaining 0-density columns (encounterclass, marital, fips) are real signal
    # from the mapping layer — out of scope for this feature; A2 correctly stays RED.
    """
    assert_coherence_violations_flagged(synthea_descriptor, fuseki_session)

    from semantic_mapper.ontology.coherence import OntologyCoherenceChecker
    coherence = OntologyCoherenceChecker(synthea_descriptor.ontology_graph).check()
    collisions = [i for i in coherence.issues if i.category == "SOURCE_COLUMN_COLLISION"]
    assert any(
        "base_encounter_cost" in i.subject_uri for i in collisions
    ), "raw_encounters.base_encounter_cost must appear in the SOURCE_COLUMN_COLLISION set"


def test_a6_terminology_provenance(synthea_descriptor, fuseki_session):
    """A6/TERM-2 + TERM-3: every external concept binding in the materialised ontology
    graph carries fm:terminologySystem + fm:terminologyBindingConfidence, and none is
    below the confidence floor.  Synthea binds marital→HL7 MaritalStatus and
    gender→HL7 Administrative Sex; code columns align to SNOMED CT / RxNorm.
    """
    assert_terminology_provenance(synthea_descriptor, fuseki_session)


def test_a6_terminology_queryable(synthea_descriptor, fuseki_session):
    """A6/TERM-6: filtering by an external concept IRI returns data rows via
    skos:exactMatch.  Synthea's gender individuals are bound to HL7 Administrative Sex
    and referenced by every patient, so a query reaching the standard term resolves to
    real subjects — the binding is queryable, not decorative.
    """
    assert_terminology_queryable(synthea_descriptor, fuseki_session)
