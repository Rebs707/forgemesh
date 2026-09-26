# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for OntologyCoherenceChecker — issue #52."""
from __future__ import annotations

import rdflib
from rdflib.namespace import OWL, RDF, RDFS, XSD

from semantic_mapper.ontology.coherence import CoherenceReport, OntologyCoherenceChecker


ONT = rdflib.Namespace("http://example.org/ont#")


def _graph() -> rdflib.Graph:
    """Return an empty graph with the test namespace bound."""
    g = rdflib.Graph()
    g.bind("ont", ONT)
    return g


def _add_class(g: rdflib.Graph, name: str) -> rdflib.URIRef:
    uri = ONT[name]
    g.add((uri, RDF.type, OWL.Class))
    return uri


def _add_datatype_prop(
    g: rdflib.Graph,
    name: str,
    domain=None,
    range_=None,
) -> rdflib.URIRef:
    uri = ONT[name]
    g.add((uri, RDF.type, OWL.DatatypeProperty))
    if domain:
        g.add((uri, RDFS.domain, domain))
    if range_:
        g.add((uri, RDFS.range, range_))
    return uri


def _add_object_prop(
    g: rdflib.Graph,
    name: str,
    domain=None,
    range_=None,
    inverse_of=None,
) -> rdflib.URIRef:
    uri = ONT[name]
    g.add((uri, RDF.type, OWL.ObjectProperty))
    if domain:
        g.add((uri, RDFS.domain, domain))
    if range_:
        g.add((uri, RDFS.range, range_))
    if inverse_of:
        g.add((uri, OWL.inverseOf, inverse_of))
    return uri


def _check(g: rdflib.Graph) -> CoherenceReport:
    return OntologyCoherenceChecker(g).check()


def _issues_by_category(report: CoherenceReport, category: str):
    return [i for i in report.issues if i.category == category]


# ── MISSING_DOMAIN ────────────────────────────────────────────────────────────

class TestMissingDomain:
    def test_property_without_domain_flagged(self):
        g = _graph()
        _add_datatype_prop(g, "hasName")
        report = _check(g)
        issues = _issues_by_category(report, "MISSING_DOMAIN")
        assert len(issues) == 1
        assert "ont#hasName" in issues[0].subject_uri

    def test_property_with_domain_clean(self):
        g = _graph()
        cls = _add_class(g, "Patient")
        _add_datatype_prop(g, "hasName", domain=cls)
        report = _check(g)
        assert not _issues_by_category(report, "MISSING_DOMAIN")

    def test_object_prop_without_domain_flagged(self):
        g = _graph()
        _add_object_prop(g, "hasEncounter")
        report = _check(g)
        assert any(i.category == "MISSING_DOMAIN" for i in report.issues)


# ── MISSING_RANGE ─────────────────────────────────────────────────────────────

class TestMissingRange:
    def test_property_without_range_flagged(self):
        g = _graph()
        cls = _add_class(g, "Patient")
        _add_datatype_prop(g, "hasDOB", domain=cls)
        report = _check(g)
        assert _issues_by_category(report, "MISSING_RANGE")

    def test_property_with_range_clean(self):
        g = _graph()
        cls = _add_class(g, "Patient")
        _add_datatype_prop(g, "hasDOB", domain=cls, range_=XSD.date)
        report = _check(g)
        assert not _issues_by_category(report, "MISSING_RANGE")


# ── NAMESPACE_COLLISION ───────────────────────────────────────────────────────

class TestNamespaceCollision:
    def test_same_local_name_different_ns_flagged(self):
        g = _graph()
        ns2 = rdflib.Namespace("http://other.org/ont#")
        cls1 = ONT["Patient"]
        cls2 = ns2["Patient"]
        g.add((cls1, RDF.type, OWL.Class))
        g.add((cls2, RDF.type, OWL.Class))
        report = _check(g)
        issues = _issues_by_category(report, "NAMESPACE_COLLISION")
        assert issues, "Expected NAMESPACE_COLLISION for duplicate local name 'Patient'"

    def test_unique_names_clean(self):
        g = _graph()
        _add_class(g, "Patient")
        _add_class(g, "Encounter")
        report = _check(g)
        assert not _issues_by_category(report, "NAMESPACE_COLLISION")


# ── ORPHAN_CLASS ──────────────────────────────────────────────────────────────

class TestOrphanClass:
    def test_class_with_no_domain_property_flagged(self):
        g = _graph()
        _add_class(g, "Encounter")
        report = _check(g)
        issues = _issues_by_category(report, "ORPHAN_CLASS")
        assert any("Encounter" in i.subject_uri for i in issues)

    def test_class_as_domain_not_orphan(self):
        g = _graph()
        cls = _add_class(g, "Patient")
        _add_datatype_prop(g, "hasName", domain=cls, range_=XSD.string)
        report = _check(g)
        orphan_issues = _issues_by_category(report, "ORPHAN_CLASS")
        assert not any("Patient" in i.subject_uri for i in orphan_issues)


# ── INVERSE_ASYMMETRY ─────────────────────────────────────────────────────────

class TestInverseAsymmetry:
    def test_asymmetric_inverse_flagged(self):
        g = _graph()
        p = _add_object_prop(g, "hasEncounter")
        q = _add_object_prop(g, "isEncounterOf")
        g.add((p, OWL.inverseOf, q))
        # q has no inverseOf p
        report = _check(g)
        issues = _issues_by_category(report, "INVERSE_ASYMMETRY")
        assert issues

    def test_symmetric_inverse_clean(self):
        g = _graph()
        p = _add_object_prop(g, "hasEncounter")
        q = _add_object_prop(g, "isEncounterOf")
        g.add((p, OWL.inverseOf, q))
        g.add((q, OWL.inverseOf, p))
        report = _check(g)
        assert not _issues_by_category(report, "INVERSE_ASYMMETRY")


# ── CoherenceReport helpers ───────────────────────────────────────────────────

class TestCoherenceReport:
    def test_empty_graph_coherent(self):
        g = _graph()
        report = _check(g)
        assert report.is_coherent is True
        assert report.error_count == 0

    def test_counts_classes_and_props(self):
        g = _graph()
        _add_class(g, "Patient")
        cls = _add_class(g, "Encounter")
        _add_datatype_prop(g, "hasId", domain=cls, range_=XSD.string)
        report = _check(g)
        assert report.classes_checked == 2
        assert report.properties_checked == 1

    def test_to_dict_structure(self):
        g = _graph()
        _add_class(g, "Patient")
        report = _check(g)
        d = report.to_dict()
        assert "is_coherent" in d
        assert "issues" in d
        assert isinstance(d["issues"], list)


# ── SOURCE_COLUMN_COLLISION ───────────────────────────────────────────────────

FM_NS = rdflib.Namespace("http://forgemesh.ai/ontology#")


def _add_fm_source_column(g: rdflib.Graph, prop_uri: rdflib.URIRef, table_col: str) -> None:
    """Annotate prop_uri with fm:sourceColumn = 'table.col'."""
    g.add((prop_uri, FM_NS.sourceColumn, rdflib.Literal(table_col)))


def _add_fm_source_fk(
    g: rdflib.Graph, prop_uri: rdflib.URIRef, table_col: str, target: str = "raw_other"
) -> None:
    """Annotate prop_uri with fm:sourceForeignKey = 'table.col -> target'."""
    g.add((prop_uri, FM_NS.sourceForeignKey, rdflib.Literal(f"{table_col} -> {target}")))


class TestSourceColumnCollision:
    def test_fk_and_dt_same_column_detected(self):
        g = _graph()
        dt_prop = _add_datatype_prop(g, "hasStatusCode")
        obj_prop = _add_object_prop(g, "hasStatusRef")
        _add_fm_source_column(g, dt_prop, "raw_events.status_code")
        _add_fm_source_fk(g, obj_prop, "raw_events.status_code", "raw_statuses")
        issues = _issues_by_category(_check(g), "SOURCE_COLUMN_COLLISION")
        assert len(issues) == 1

    def test_subject_uri_is_table_dot_column(self):
        g = _graph()
        dt_prop = _add_datatype_prop(g, "hasStatusCode")
        obj_prop = _add_object_prop(g, "hasStatusRef")
        _add_fm_source_column(g, dt_prop, "raw_events.status_code")
        _add_fm_source_fk(g, obj_prop, "raw_events.status_code", "raw_statuses")
        issues = _issues_by_category(_check(g), "SOURCE_COLUMN_COLLISION")
        assert issues[0].subject_uri == "raw_events.status_code"

    def test_collision_is_error_not_warning(self):
        g = _graph()
        dt_prop = _add_datatype_prop(g, "hasStatusCode")
        obj_prop = _add_object_prop(g, "hasStatusRef")
        _add_fm_source_column(g, dt_prop, "raw_events.status_code")
        _add_fm_source_fk(g, obj_prop, "raw_events.status_code", "raw_statuses")
        issues = _issues_by_category(_check(g), "SOURCE_COLUMN_COLLISION")
        assert issues[0].severity == "error"

    def test_fk_only_no_collision(self):
        g = _graph()
        obj_prop = _add_object_prop(g, "hasStatusRef")
        _add_fm_source_fk(g, obj_prop, "raw_events.status_code", "raw_statuses")
        assert not _issues_by_category(_check(g), "SOURCE_COLUMN_COLLISION")

    def test_dt_only_no_collision(self):
        g = _graph()
        dt_prop = _add_datatype_prop(g, "hasStatusCode")
        _add_fm_source_column(g, dt_prop, "raw_events.status_code")
        assert not _issues_by_category(_check(g), "SOURCE_COLUMN_COLLISION")

    def test_different_columns_no_collision(self):
        g = _graph()
        dt_prop = _add_datatype_prop(g, "hasAmount")
        obj_prop = _add_object_prop(g, "hasCategory")
        _add_fm_source_column(g, dt_prop, "raw_orders.amount")
        _add_fm_source_fk(g, obj_prop, "raw_orders.category_id", "raw_categories")
        assert not _issues_by_category(_check(g), "SOURCE_COLUMN_COLLISION")

    def test_two_collisions_detected(self):
        g = _graph()
        dt1 = _add_datatype_prop(g, "hasStatusCode")
        obj1 = _add_object_prop(g, "hasStatusRef")
        dt2 = _add_datatype_prop(g, "hasTypeCode")
        obj2 = _add_object_prop(g, "hasTypeRef")
        _add_fm_source_column(g, dt1, "raw_events.status_code")
        _add_fm_source_fk(g, obj1, "raw_events.status_code", "raw_statuses")
        _add_fm_source_column(g, dt2, "raw_events.type_code")
        _add_fm_source_fk(g, obj2, "raw_events.type_code", "raw_types")
        issues = _issues_by_category(_check(g), "SOURCE_COLUMN_COLLISION")
        assert len(issues) == 2

    def test_is_not_coherent_when_collision(self):
        g = _graph()
        dt_prop = _add_datatype_prop(g, "hasStatusCode")
        obj_prop = _add_object_prop(g, "hasStatusRef")
        _add_fm_source_column(g, dt_prop, "raw_events.status_code")
        _add_fm_source_fk(g, obj_prop, "raw_events.status_code", "raw_statuses")
        report = _check(g)
        assert not report.is_coherent
        assert report.error_count == 1


# ---------------------------------------------------------------------------
# Range coherence: MIXED_RANGE, WRONG_RANGE_TYPE, MULTI_RANGE, DANGLING_RANGE
# ---------------------------------------------------------------------------

class TestRangeCoherence:
    def test_mixed_range_class_and_xsd(self):
        """ObjectProperty with a declared class range AND an XSD range → MIXED_RANGE."""
        g = _graph()
        cls = _add_class(g, "Patient")
        prop = _add_object_prop(g, "hasPatient", range_=cls)
        g.add((prop, RDFS.range, XSD.integer))  # wrong second range
        issues = _issues_by_category(_check(g), "MIXED_RANGE")
        assert len(issues) == 1
        dangling = _issues_by_category(_check(g), "DANGLING_RANGE")
        assert len(dangling) == 0

    def test_mixed_range_also_fires_dangling(self):
        """MIXED_RANGE and DANGLING_RANGE are additive: both fire when conditions overlap."""
        g = _graph()
        cls = _add_class(g, "Patient")
        prop = _add_object_prop(g, "hasPatient", range_=cls)
        g.add((prop, RDFS.range, XSD.integer))
        g.add((prop, RDFS.range, ONT["UndeclaredClass"]))
        report = _check(g)
        mixed = _issues_by_category(report, "MIXED_RANGE")
        dangling = _issues_by_category(report, "DANGLING_RANGE")
        assert len(mixed) == 1
        assert len(dangling) == 1

    def test_wrong_range_type_xsd_only(self):
        """ObjectProperty with only XSD range(s) and no class range → WRONG_RANGE_TYPE."""
        g = _graph()
        prop = _add_object_prop(g, "hasQuantity", range_=XSD.integer)
        report = _check(g)
        wrong = _issues_by_category(report, "WRONG_RANGE_TYPE")
        assert len(wrong) == 1
        assert wrong[0].subject_uri == str(ONT["hasQuantity"])
        mixed = _issues_by_category(report, "MIXED_RANGE")
        assert len(mixed) == 0
        dangling = _issues_by_category(report, "DANGLING_RANGE")
        assert len(dangling) == 0

    def test_multi_range_two_declared_class_ranges(self):
        """ObjectProperty with two declared class ranges, no XSD → MULTI_RANGE warning."""
        g = _graph()
        cls1 = _add_class(g, "Patient")
        cls2 = _add_class(g, "Admission")
        prop = _add_object_prop(g, "hasLink", range_=cls1)
        g.add((prop, RDFS.range, cls2))
        issues = _issues_by_category(_check(g), "MULTI_RANGE")
        assert len(issues) == 1
        assert issues[0].severity == "warning"

    def test_dangling_range_unknown_class(self):
        """ObjectProperty whose range URI is not a declared owl:Class → DANGLING_RANGE."""
        g = _graph()
        prop = _add_object_prop(g, "hasTarget", range_=ONT["UndeclaredClass"])
        issues = _issues_by_category(_check(g), "DANGLING_RANGE")
        assert len(issues) == 1
        assert issues[0].severity == "error"
        mixed = _issues_by_category(_check(g), "MIXED_RANGE")
        assert len(mixed) == 0

    def test_owl_thing_range_exempt(self):
        """owl:Thing as range is always valid — no range coherence issues."""
        g = _graph()
        _add_object_prop(g, "hasAnything", range_=OWL.Thing)
        for cat in ("MIXED_RANGE", "WRONG_RANGE_TYPE", "MULTI_RANGE", "DANGLING_RANGE"):
            assert len(_issues_by_category(_check(g), cat)) == 0

    def test_correct_single_class_range_no_issues(self):
        """ObjectProperty with exactly one declared class range → no range errors."""
        g = _graph()
        cls = _add_class(g, "Admission")
        _add_object_prop(g, "hasAdmission", range_=cls)
        for cat in ("MIXED_RANGE", "WRONG_RANGE_TYPE", "MULTI_RANGE", "DANGLING_RANGE"):
            assert len(_issues_by_category(_check(g), cat)) == 0
