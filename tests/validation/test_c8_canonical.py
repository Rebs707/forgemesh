# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for C8 CANON-4 — the data must reference the generator's deterministic
{Value}{Class} vocabulary individuals, not an LLM-invented synonym the enricher added.

CANON-4 catches the drift where the generated OWL declares :MGender (label "M") but the
materialised data references :MaleGender (label "Male Gender") because the map free-named
it. Verified structurally: an individual's local name must equal value_slug(label) + class.
"""
from __future__ import annotations

import pytest
from rdflib import Dataset, Graph, Literal, OWL, RDF, RDFS, Namespace, URIRef

from semantic_mapper.schema.mapping_output import (
    ClassCandidate, MappingStatus, PropertyMapping, SemanticMappingReport,
    TableMapping, ValueMappingRule,
)
from semantic_mapper.validation.checks.categorical_canonicalisation import (
    CategoricalCanonicalisationCheck,
)
from semantic_mapper.validation.context import ValidationContext
from semantic_mapper.validation.sparql_backend import RdflibBackend

ONT = Namespace("http://mesh.local/ontology/test#")
BASE = Namespace("http://mesh.local/resource/")
GRAPH_URI = "http://mesh.local/graph/test"


def _ontology() -> Graph:
    g = Graph()
    g.add((ONT.Gender, RDF.type, OWL.Class))
    g.add((ONT.hasGender, RDF.type, OWL.ObjectProperty))
    g.add((ONT.hasGender, RDFS.range, ONT.Gender))
    # Generator's deterministic individual (canonical) + an LLM-synonym (drift).
    for local, label in [("MGender", "M"), ("MaleGender", "Male Gender")]:
        i = ONT[local]
        g.add((i, RDF.type, OWL.NamedIndividual))
        g.add((i, RDF.type, ONT.Gender))
        g.add((i, RDFS.label, Literal(label)))
    return g


def _dataset(individual_local: str) -> Dataset:
    ds = Dataset()
    g = ds.graph(URIRef(GRAPH_URI))
    g.add((BASE["athlete/1"], ONT.hasGender, ONT[individual_local]))
    return ds


def _report() -> SemanticMappingReport:
    pm = PropertyMapping(
        source_column="gender", source_data_type="VARCHAR", is_nullable=False,
        mapped_property_uri=str(ONT.hasGender), status=MappingStatus.CONFIRMED,
        confidence_score=1.0, rationale="t",
        value_mappings=[ValueMappingRule(source_value="M", mapped_uri=str(ONT.MGender),
                                         rationale="t", confidence_score=0.9)],
    )
    return SemanticMappingReport(
        ontology_uri=str(ONT), database_name="test", settings_used={},
        total_pending_review=0,
        tables=[TableMapping(
            source_table="raw_athletes",
            class_candidate_set=[ClassCandidate(uri=str(ONT.Athlete), label="Athlete",
                                                rationale="t", confidence_score=1.0)],
            column_mappings=[pm],
        )],
    )


def _ctx(individual_local: str) -> ValidationContext:
    return ValidationContext(
        project_name="test", graph_uri=GRAPH_URI,
        sparql=RdflibBackend(_dataset(individual_local)),
        gold_engine=None, mapping_report=_report(), ontology=_ontology(),
        baseline_run_id="run_test_001",
    )


def test_canon4_passes_when_data_uses_canonical_individual():
    result = CategoricalCanonicalisationCheck().run(_ctx("MGender"))
    assert result.passed, result.message
    assert result.details["canon4_noncanonical"] == []


def test_canon4_fails_on_llm_synonym_drift():
    result = CategoricalCanonicalisationCheck().run(_ctx("MaleGender"))
    assert not result.passed
    assert "CANON-4" in result.message
    assert any("MaleGender" in v for v in result.details["canon4_noncanonical"])
