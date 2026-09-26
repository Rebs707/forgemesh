# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for domain-mismatch detection in the aggregator short-circuits and
cross-type resolution in _resolve_to_specific_property.

Scenarios covered:
- subject_id in raw_admissions  → hasSubjectId (domain=Patient) does NOT short-circuit
                                   → cross-type resolution returns hasAdmissionSubject
- subject_id in raw_patients    → hasSubjectId (domain=Patient) short-circuits correctly
- hadm_id   in raw_chartevents  → hasCharteventHadmid wins, not hasHadmId (domain=Admission)
- Abstract property with no domain → existing behaviour unchanged
- _has_domain_mismatch helper   → unit tests
"""
from __future__ import annotations

import pytest
import rdflib
from rdflib import OWL, RDF, RDFS, XSD, URIRef
from rdflib.namespace import SKOS

from semantic_mapper.config.settings import Settings
from semantic_mapper.engine.aggregator import aggregate, _has_domain_mismatch
from semantic_mapper.schema.mapping_output import MappingEvidence


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

BASE = "http://example.org/mimic#"


def uri(local: str) -> URIRef:
    return URIRef(f"{BASE}{local}")


@pytest.fixture()
def settings():
    return Settings(DATABASE_URL="sqlite:///:memory:", OPENAI_API_KEY="x")


@pytest.fixture()
def graph():
    """Minimal OWL graph modelling the MIMIC admissions / patients / chartevents scenario.

    Classes:   Patient, Admission, Chartevent
    Properties:
      hasSubjectId         DatatypeProperty  domain=Patient    altLabel=subject_id
      hasAdmissionSubject  ObjectProperty    domain=Admission  range=Patient
      hasHadmId            DatatypeProperty  domain=Admission  altLabel=hadm_id
      hasCharteventHadmid  ObjectProperty    domain=Chartevent range=Admission
      hasValue             DatatypeProperty  (no domain — abstract)
      hasPatientValue      DatatypeProperty  domain=Patient    SubPropertyOf hasValue
    """
    g = rdflib.Graph()

    # Classes
    for cls in ("Patient", "Admission", "Chartevent"):
        g.add((uri(cls), RDF.type, OWL.Class))

    # hasSubjectId — DatatypeProperty, domain=Patient (PK-derived property)
    g.add((uri("hasSubjectId"), RDF.type, OWL.DatatypeProperty))
    g.add((uri("hasSubjectId"), RDFS.domain, uri("Patient")))
    g.add((uri("hasSubjectId"), RDFS.range, XSD.integer))
    g.add((uri("hasSubjectId"), SKOS.altLabel, rdflib.Literal("subject_id", lang="en")))

    # hasAdmissionSubject — ObjectProperty, domain=Admission, range=Patient (FK link)
    g.add((uri("hasAdmissionSubject"), RDF.type, OWL.ObjectProperty))
    g.add((uri("hasAdmissionSubject"), RDFS.domain, uri("Admission")))
    g.add((uri("hasAdmissionSubject"), RDFS.range, uri("Patient")))
    g.add((uri("hasAdmissionSubject"), SKOS.altLabel, rdflib.Literal("subject_id", lang="en")))

    # hasHadmId — DatatypeProperty, domain=Admission (PK-derived)
    g.add((uri("hasHadmId"), RDF.type, OWL.DatatypeProperty))
    g.add((uri("hasHadmId"), RDFS.domain, uri("Admission")))
    g.add((uri("hasHadmId"), RDFS.range, XSD.integer))
    g.add((uri("hasHadmId"), SKOS.altLabel, rdflib.Literal("hadm_id", lang="en")))

    # hasCharteventHadmid — ObjectProperty, domain=Chartevent, range=Admission (FK link)
    g.add((uri("hasCharteventHadmid"), RDF.type, OWL.ObjectProperty))
    g.add((uri("hasCharteventHadmid"), RDFS.domain, uri("Chartevent")))
    g.add((uri("hasCharteventHadmid"), RDFS.range, uri("Admission")))
    g.add((uri("hasCharteventHadmid"), SKOS.altLabel, rdflib.Literal("hadm_id", lang="en")))

    # hasValue — abstract DatatypeProperty (no domain)
    g.add((uri("hasValue"), RDF.type, OWL.DatatypeProperty))
    g.add((uri("hasValue"), RDFS.range, XSD.string))

    # hasPatientValue — class-scoped subproperty of hasValue
    g.add((uri("hasPatientValue"), RDF.type, OWL.DatatypeProperty))
    g.add((uri("hasPatientValue"), RDFS.domain, uri("Patient")))
    g.add((uri("hasPatientValue"), RDFS.range, XSD.string))
    g.add((uri("hasPatientValue"), RDFS.subPropertyOf, uri("hasValue")))

    return g


def _ev(strategy: str, score: float, prop: str, rationale: str = "test") -> MappingEvidence:
    return MappingEvidence(
        strategy=strategy, score=score,
        mapped_uri=f"{BASE}{prop}", rationale=rationale,
    )


# ---------------------------------------------------------------------------
# _has_domain_mismatch unit tests
# ---------------------------------------------------------------------------

class TestHasDomainMismatch:
    def test_no_graph_returns_false(self, graph):
        assert _has_domain_mismatch(f"{BASE}hasSubjectId", f"{BASE}Patient", None) is False

    def test_no_table_class_returns_false(self, graph):
        assert _has_domain_mismatch(f"{BASE}hasSubjectId", None, graph) is False

    def test_abstract_no_domain_returns_false(self, graph):
        """Abstract property (no rdfs:domain) is never a mismatch."""
        assert _has_domain_mismatch(f"{BASE}hasValue", f"{BASE}Admission", graph) is False

    def test_domain_matches_returns_false(self, graph):
        """Property domain matches table class → no mismatch."""
        assert _has_domain_mismatch(f"{BASE}hasSubjectId", f"{BASE}Patient", graph) is False

    def test_domain_mismatch_returns_true(self, graph):
        """hasSubjectId (domain=Patient) vs Admission → mismatch."""
        assert _has_domain_mismatch(f"{BASE}hasSubjectId", f"{BASE}Admission", graph) is True

    def test_domain_mismatch_chartevent(self, graph):
        assert _has_domain_mismatch(f"{BASE}hasHadmId", f"{BASE}Chartevent", graph) is True


# ---------------------------------------------------------------------------
# Aggregator short-circuit domain-mismatch tests
# ---------------------------------------------------------------------------

class TestAnnotationShortCircuitDomainCheck:
    def test_domain_mismatch_skips_short_circuit(self, settings, graph):
        """AnnotationMatcher 1.0 on hasSubjectId (domain=Patient) in Admissions context
        must NOT short-circuit — fall through to weighted sum."""
        trail = [
            _ev("AnnotationMatcher", 1.0, "hasSubjectId", "verbatim altLabel match: subject_id"),
            _ev("AnnotationMatcher", 0.75, "hasAdmissionSubject", "altLabel match: subject_id"),
            _ev("NameMatcher",       0.50, "hasAdmissionSubject"),
        ]
        uri_result, score, rationale = aggregate(
            trail, settings,
            graph=graph,
            table_class_uri=f"{BASE}Admission",
        )
        assert "[annotation-exact]" not in rationale
        # hasAdmissionSubject wins in the weighted sum (TypeMatcher not in trail so no type gate)
        assert uri_result == f"{BASE}hasAdmissionSubject"

    def test_domain_match_fires_short_circuit(self, settings, graph):
        """AnnotationMatcher 1.0 on hasSubjectId (domain=Patient) in Patient context
        MUST short-circuit — correct target class."""
        trail = [
            _ev("AnnotationMatcher", 1.0, "hasSubjectId", "verbatim altLabel match: subject_id"),
            _ev("NameMatcher",       0.50, "hasAdmissionSubject"),
        ]
        uri_result, score, rationale = aggregate(
            trail, settings,
            graph=graph,
            table_class_uri=f"{BASE}Patient",
        )
        assert uri_result == f"{BASE}hasSubjectId"
        assert score == pytest.approx(1.0)
        assert "[annotation-exact]" in rationale

    def test_no_graph_preserves_existing_behaviour(self, settings):
        """When graph=None the short-circuit fires unconditionally (backwards compat)."""
        trail = [
            _ev("AnnotationMatcher", 1.0, "hasSubjectId", "verbatim hit"),
            _ev("NameMatcher",       0.50, "hasAdmissionSubject"),
        ]
        uri_result, score, rationale = aggregate(trail, settings, graph=None)
        assert uri_result == f"{BASE}hasSubjectId"
        assert "[annotation-exact]" in rationale

    def test_abstract_property_short_circuits_normally(self, settings, graph):
        """Abstract property (no domain) must NOT suppress the short-circuit."""
        trail = [
            _ev("AnnotationMatcher", 1.0, "hasValue", "verbatim altLabel hit"),
            _ev("NameMatcher",       0.40, "hasPatientValue"),
        ]
        uri_result, score, rationale = aggregate(
            trail, settings,
            graph=graph,
            table_class_uri=f"{BASE}Patient",
        )
        assert uri_result == f"{BASE}hasValue"
        assert "[annotation-exact]" in rationale


class TestNameMatcherShortCircuitDomainCheck:
    def test_domain_mismatch_skips_name_short_circuit_and_domain_filter_wins(self, settings, graph):
        """NameMatcher 1.0 on hasHadmId (domain=Admission) in Chartevent context must NOT
        short-circuit.  The domain filter then eliminates hasHadmId (wrong domain) and keeps
        hasCharteventHadmid (domain=Chartevent), which wins despite a lower NameMatcher score."""
        trail = [
            _ev("NameMatcher", 1.0, "hasHadmId", "exact concept match: hadmid"),
            _ev("NameMatcher", 0.60, "hasCharteventHadmid", "partial concept match"),
        ]
        uri_result, score, rationale = aggregate(
            trail, settings,
            graph=graph,
            table_class_uri=f"{BASE}Chartevent",
        )
        assert "[name-exact]" not in rationale
        # Domain filter drops hasHadmId (domain=Admission ≠ Chartevent); hasCharteventHadmid wins.
        assert uri_result == f"{BASE}hasCharteventHadmid"

    def test_domain_match_fires_name_short_circuit(self, settings, graph):
        """NameMatcher 1.0 on hasHadmId in Admission context must short-circuit."""
        trail = [
            _ev("NameMatcher", 1.0, "hasHadmId", "exact concept match: hadmid"),
        ]
        uri_result, score, rationale = aggregate(
            trail, settings,
            graph=graph,
            table_class_uri=f"{BASE}Admission",
        )
        assert uri_result == f"{BASE}hasHadmId"
        assert "[name-exact]" in rationale


# ---------------------------------------------------------------------------
# _resolve_to_specific_property cross-type resolution tests
# ---------------------------------------------------------------------------

class _FakeMapper:
    """Minimal stand-in for MapperOrchestrator that only exposes the graph."""

    def __init__(self, g: rdflib.Graph) -> None:
        self.graph = g

    def resolve(self, uri_str: str, table_class_uri: str, col_name: str = "") -> str:
        from semantic_mapper.engine.mapper import MapperOrchestrator
        return MapperOrchestrator._resolve_to_specific_property(self, uri_str, table_class_uri, col_name)


class TestResolveToSpecificPropertyDomainMismatch:
    def test_subject_id_in_admissions_resolves_to_hasAdmissionSubject(self, graph):
        """hasSubjectId (domain=Patient) + table=Admission + col=subject_id
        → cross-type resolution returns hasAdmissionSubject."""
        mapper = _FakeMapper(graph)
        result = mapper.resolve(f"{BASE}hasSubjectId", f"{BASE}Admission", "subject_id")
        assert result == f"{BASE}hasAdmissionSubject"

    def test_subject_id_in_patients_returns_unchanged(self, graph):
        """hasSubjectId (domain=Patient) + table=Patient → already correct, returned as-is."""
        mapper = _FakeMapper(graph)
        result = mapper.resolve(f"{BASE}hasSubjectId", f"{BASE}Patient", "subject_id")
        assert result == f"{BASE}hasSubjectId"

    def test_hadm_id_in_chartevents_resolves_to_hasCharteventHadmid(self, graph):
        """hasHadmId (domain=Admission) + table=Chartevent + col=hadm_id
        → cross-type resolution returns hasCharteventHadmid."""
        mapper = _FakeMapper(graph)
        result = mapper.resolve(f"{BASE}hasHadmId", f"{BASE}Chartevent", "hadm_id")
        assert result == f"{BASE}hasCharteventHadmid"

    def test_abstract_property_resolves_to_subproperty(self, graph):
        """hasValue (no domain) + table=Patient → Phase 1 returns hasPatientValue."""
        mapper = _FakeMapper(graph)
        result = mapper.resolve(f"{BASE}hasValue", f"{BASE}Patient", "value")
        assert result == f"{BASE}hasPatientValue"

    def test_empty_uri_returns_empty(self, graph):
        mapper = _FakeMapper(graph)
        assert mapper.resolve("", f"{BASE}Patient", "value") == ""

    def test_no_table_class_returns_uri_unchanged(self, graph):
        mapper = _FakeMapper(graph)
        assert mapper.resolve(f"{BASE}hasSubjectId", "", "subject_id") == f"{BASE}hasSubjectId"

    def test_domain_mismatch_no_candidates_returns_original(self, graph):
        """hasSubjectId domain=Patient, table=Chartevent: hasAdmissionSubject has range=Patient
        but domain=Admission (not Chartevent). hasCharteventHadmid range=Admission (not Patient).
        No candidate with range=Patient AND domain=Chartevent → return original."""
        mapper = _FakeMapper(graph)
        # No property has domain=Chartevent AND range=Patient AND concept overlaps "subject"
        result = mapper.resolve(f"{BASE}hasSubjectId", f"{BASE}Chartevent", "subject_id")
        # Falls back to original since no exact-range match and multiple any_match candidates
        # (hasCharteventHadmid has domain=Chartevent but concept doesn't overlap subject_id well)
        assert result == f"{BASE}hasSubjectId"
