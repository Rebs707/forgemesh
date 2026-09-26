# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for semantic_mapper/ontology/annotation_sweep.py.

Graph fixtures are built in-memory so no OWL file is required.

Phase 0b: all provenance is now in fm: annotations, not rdfs:comment.
"""
from __future__ import annotations

import rdflib
import pytest
from rdflib import OWL, RDF, RDFS, URIRef, Literal
from rdflib.namespace import SKOS, XSD

from semantic_mapper.ontology.annotation_sweep import (
    AnnotationSweepResult,
    remove_legacy_provenance_comments,
    sweep_provenance_annotations,
)
from semantic_mapper.schema.mapping_output import (
    ClassCandidate,
    MappingStatus,
    PropertyMapping,
    SemanticMappingReport,
    TableMapping,
)

BASE = "http://example.org/ont#"
FM = rdflib.Namespace("http://forgemesh.ai/ontology#")
SKOS_ALT = SKOS.altLabel


def uri(local: str) -> URIRef:
    return URIRef(f"{BASE}{local}")


# ---------------------------------------------------------------------------
# Graph builders
# ---------------------------------------------------------------------------

def _empty_graph() -> rdflib.Graph:
    return rdflib.Graph()


def _graph_with_class(local: str, fm_source_table: str = "") -> rdflib.Graph:
    """Build a graph with a class; optionally pre-annotated with fm:sourceTable."""
    g = _empty_graph()
    g.add((uri(local), RDF.type, OWL.Class))
    if fm_source_table:
        g.add((uri(local), FM.sourceTable, Literal(fm_source_table)))
    return g


def _graph_with_property(
    local: str,
    prop_type=OWL.DatatypeProperty,
    fm_source_column: str = "",
    fm_sql_type: str = "",
    fm_null_rate: str = "",
    fm_source_fk: str = "",
    alt_label: str = "",
    comment: str = "",
) -> rdflib.Graph:
    g = _empty_graph()
    g.add((uri(local), RDF.type, prop_type))
    if alt_label:
        g.add((uri(local), SKOS_ALT, Literal(alt_label, lang="en")))
    if comment:
        g.add((uri(local), RDFS.comment, Literal(comment, lang="en")))
    if fm_source_column:
        g.add((uri(local), FM.sourceColumn, Literal(fm_source_column)))
    if fm_sql_type:
        g.add((uri(local), FM.sqlType, Literal(fm_sql_type)))
    if fm_null_rate:
        g.add((uri(local), FM.nullRate, Literal(fm_null_rate)))
    if fm_source_fk:
        g.add((uri(local), FM.sourceForeignKey, Literal(fm_source_fk)))
    return g


def _full_graph() -> rdflib.Graph:
    """Graph with fm: annotations already on all elements — sweep should not touch it."""
    g = _empty_graph()
    g.add((uri("Admission"), RDF.type, OWL.Class))
    g.add((uri("Admission"), RDFS.comment, Literal("A hospital admission.", lang="en")))
    g.add((uri("Admission"), FM.sourceTable, Literal("admissions")))
    g.add((uri("hasAdmittime"), RDF.type, OWL.DatatypeProperty))
    g.add((uri("hasAdmittime"), RDFS.comment, Literal("Admission timestamp.", lang="en")))
    g.add((uri("hasAdmittime"), FM.sourceColumn, Literal("admissions.admittime")))
    g.add((uri("hasAdmittime"), FM.sqlType, Literal("DATETIME")))
    g.add((uri("hasAdmittime"), FM.nullRate, Literal("0.00")))
    return g


def _minimal_report(
    table: str = "raw_admissions",
    class_uri: str = f"{BASE}Admission",
    prop_uri: str = f"{BASE}hasSubjectId",
    col: str = "subject_id",
    dtype: str = "INT",
) -> SemanticMappingReport:
    return SemanticMappingReport(
        ontology_uri="http://example.org/ont",
        database_name="test_db",
        settings_used={},
        total_pending_review=0,
        tables=[
            TableMapping(
                source_table=table,
                class_candidate_set=[
                    ClassCandidate(
                        uri=class_uri,
                        label="Admission",
                        rationale="test",
                        confidence_score=1.0,
                    )
                ],
                column_mappings=[
                    PropertyMapping(
                        source_column=col,
                        source_data_type=dtype,
                        is_nullable=False,
                        mapped_property_uri=prop_uri,
                        confidence_score=1.0,
                        status=MappingStatus.CONFIRMED,
                        rationale="test",
                    )
                ],
            )
        ],
    )


# ---------------------------------------------------------------------------
# AnnotationSweepResult shape
# ---------------------------------------------------------------------------

class TestAnnotationSweepResultModel:
    def test_all_fields_present(self):
        r = AnnotationSweepResult(
            total_classes=5,
            total_properties=10,
            synthesised_classes=2,
            synthesised_data_properties=2,
            synthesised_object_properties=1,
            unsynthesisable=0,
        )
        assert r.total_classes == 5
        assert r.total_properties == 10
        assert r.synthesised_classes == 2
        assert r.synthesised_data_properties == 2
        assert r.synthesised_object_properties == 1
        assert r.unsynthesisable == 0

    def test_synthesised_properties_combines_data_and_object(self):
        r = AnnotationSweepResult(
            total_classes=0,
            total_properties=5,
            synthesised_classes=0,
            synthesised_data_properties=2,
            synthesised_object_properties=1,
            unsynthesisable=0,
        )
        assert r.synthesised_properties == 3


# ---------------------------------------------------------------------------
# Empty graph
# ---------------------------------------------------------------------------

class TestEmptyGraph:
    def test_empty_graph_returns_zero_counts(self):
        result = sweep_provenance_annotations(_empty_graph())
        assert result.total_classes == 0
        assert result.total_properties == 0
        assert result.synthesised_classes == 0
        assert result.synthesised_properties == 0
        assert result.unsynthesisable == 0


# ---------------------------------------------------------------------------
# Well-annotated graph — zero synthesis
# ---------------------------------------------------------------------------

class TestFullyAnnotatedGraph:
    def test_no_synthesis_when_all_annotated(self):
        g = _full_graph()
        result = sweep_provenance_annotations(g)
        assert result.synthesised_classes == 0
        assert result.synthesised_properties == 0
        assert result.unsynthesisable == 0

    def test_existing_comments_unchanged(self):
        g = _full_graph()
        sweep_provenance_annotations(g)
        comments = list(g.objects(uri("Admission"), RDFS.comment))
        assert len(comments) == 1
        assert str(comments[0]) == "A hospital admission."

    def test_existing_fm_source_table_unchanged(self):
        g = _full_graph()
        sweep_provenance_annotations(g)
        vals = list(g.objects(uri("Admission"), FM.sourceTable))
        assert len(vals) == 1
        assert str(vals[0]) == "admissions"

    def test_total_counts_correct(self):
        g = _full_graph()
        result = sweep_provenance_annotations(g)
        assert result.total_classes == 1
        assert result.total_properties == 1


# ---------------------------------------------------------------------------
# Class synthesis — no report
# ---------------------------------------------------------------------------

class TestClassSynthesisNoReport:
    def test_sweep_adds_fm_to_class_missing_annotation(self):
        g = _graph_with_class("Patient")
        result = sweep_provenance_annotations(g)
        assert result.synthesised_classes == 1
        vals = list(g.objects(uri("Patient"), FM.sourceTable))
        assert len(vals) == 1

    def test_synthesised_fm_source_table_contains_local_name(self):
        g = _graph_with_class("Patient")
        sweep_provenance_annotations(g)
        val = str(list(g.objects(uri("Patient"), FM.sourceTable))[0])
        assert "Patient" in val

    def test_synthesised_fm_annotation_is_plain_literal(self):
        g = _graph_with_class("Patient")
        sweep_provenance_annotations(g)
        lit = list(g.objects(uri("Patient"), FM.sourceTable))[0]
        assert isinstance(lit, Literal)

    def test_already_annotated_class_not_duplicated(self):
        g = _graph_with_class("Patient", fm_source_table="raw_patients")
        sweep_provenance_annotations(g)
        vals = list(g.objects(uri("Patient"), FM.sourceTable))
        assert len(vals) == 1

    def test_synthesised_count_matches_missing(self):
        g = _empty_graph()
        for local in ("ClassA", "ClassB", "ClassC"):
            g.add((uri(local), RDF.type, OWL.Class))
        g.add((uri("ClassA"), FM.sourceTable, Literal("table_a")))
        result = sweep_provenance_annotations(g)
        assert result.total_classes == 3
        assert result.synthesised_classes == 2


# ---------------------------------------------------------------------------
# Class synthesis — with report
# ---------------------------------------------------------------------------

class TestClassSynthesisWithReport:
    def test_synthesises_source_table_from_report(self):
        g = _graph_with_class("Admission")
        report = _minimal_report(
            table="raw_admissions",
            class_uri=f"{BASE}Admission",
        )
        sweep_provenance_annotations(g, report)
        val = str(list(g.objects(uri("Admission"), FM.sourceTable))[0])
        assert val == "raw_admissions"

    def test_sweep_with_mapping_report_recovers_source_table(self):
        g = _graph_with_class("Admission")
        report = _minimal_report(table="raw_admissions", class_uri=f"{BASE}Admission")
        sweep_provenance_annotations(g, report)
        val = str(list(g.objects(uri("Admission"), FM.sourceTable))[0])
        assert val == "raw_admissions"

    def test_class_not_in_report_falls_back_to_local_name(self):
        g = _graph_with_class("UnknownClass")
        report = _minimal_report(class_uri=f"{BASE}Admission")
        sweep_provenance_annotations(g, report)
        val = str(list(g.objects(uri("UnknownClass"), FM.sourceTable))[0])
        assert "UnknownClass" in val


# ---------------------------------------------------------------------------
# DataProperty synthesis — no report
# ---------------------------------------------------------------------------

class TestDataPropertySynthesisNoReport:
    def test_sweep_adds_fm_to_data_property(self):
        g = _graph_with_property("hasAdmittime")
        result = sweep_provenance_annotations(g)
        assert result.synthesised_data_properties == 1
        assert list(g.objects(uri("hasAdmittime"), FM.sourceColumn))
        assert list(g.objects(uri("hasAdmittime"), FM.sqlType))
        assert list(g.objects(uri("hasAdmittime"), FM.nullRate))

    def test_alt_label_used_in_fm_source_column(self):
        g = _graph_with_property("hasAdmittime", alt_label="admittime")
        sweep_provenance_annotations(g)
        col_val = str(list(g.objects(uri("hasAdmittime"), FM.sourceColumn))[0])
        assert "admittime" in col_val

    def test_already_annotated_property_not_touched(self):
        g = _graph_with_property(
            "hasAdmittime",
            fm_source_column="admissions.admittime",
            fm_sql_type="DATETIME",
            fm_null_rate="0.00",
        )
        result = sweep_provenance_annotations(g)
        assert result.synthesised_data_properties == 0

    def test_partial_annotation_filled_in(self):
        """If only fm:sourceColumn exists, sqlType and nullRate are added."""
        g = _empty_graph()
        g.add((uri("hasProp"), RDF.type, OWL.DatatypeProperty))
        g.add((uri("hasProp"), FM.sourceColumn, Literal("t.col")))
        result = sweep_provenance_annotations(g)
        assert result.synthesised_data_properties == 1
        assert list(g.objects(uri("hasProp"), FM.sqlType))
        assert list(g.objects(uri("hasProp"), FM.nullRate))

    def test_synthesised_fm_annotations_are_plain_literals(self):
        g = _graph_with_property("hasAdmittime")
        sweep_provenance_annotations(g)
        for pred in (FM.sourceColumn, FM.sqlType, FM.nullRate):
            lit = list(g.objects(uri("hasAdmittime"), pred))[0]
            assert isinstance(lit, Literal)


# ---------------------------------------------------------------------------
# DataProperty synthesis — with report
# ---------------------------------------------------------------------------

class TestDataPropertySynthesisWithReport:
    def test_synthesises_fm_source_column_from_report(self):
        g = _graph_with_property("hasSubjectId")
        report = _minimal_report(
            table="raw_admissions",
            prop_uri=f"{BASE}hasSubjectId",
            col="subject_id",
            dtype="INT",
        )
        sweep_provenance_annotations(g, report)
        col_val = str(list(g.objects(uri("hasSubjectId"), FM.sourceColumn))[0])
        assert col_val == "raw_admissions.subject_id"

    def test_synthesises_fm_sql_type_from_report(self):
        g = _graph_with_property("hasSubjectId")
        report = _minimal_report(
            prop_uri=f"{BASE}hasSubjectId",
            col="subject_id",
            dtype="INT",
        )
        sweep_provenance_annotations(g, report)
        type_val = str(list(g.objects(uri("hasSubjectId"), FM.sqlType))[0])
        assert type_val == "INT"

    def test_property_not_in_report_falls_back_to_local_name(self):
        g = _graph_with_property("hasUnknownProp")
        report = _minimal_report(prop_uri=f"{BASE}hasSubjectId")
        sweep_provenance_annotations(g, report)
        col_val = str(list(g.objects(uri("hasUnknownProp"), FM.sourceColumn))[0])
        assert "hasUnknownProp" in col_val or "unknown" in col_val


# ---------------------------------------------------------------------------
# ObjectProperty synthesis
# ---------------------------------------------------------------------------

class TestObjectPropertySynthesis:
    def test_sweep_object_property_not_recoverable(self):
        g = _graph_with_property("hasPatient", prop_type=OWL.ObjectProperty)
        result = sweep_provenance_annotations(g)
        assert result.synthesised_object_properties == 1
        fk_val = str(list(g.objects(uri("hasPatient"), FM.sourceForeignKey))[0])
        assert fk_val == "not recoverable"

    def test_object_property_counted_separately(self):
        g = _empty_graph()
        g.add((uri("hasPatient"), RDF.type, OWL.ObjectProperty))
        result = sweep_provenance_annotations(g)
        assert result.synthesised_object_properties == 1
        assert result.synthesised_data_properties == 0

    def test_data_property_counted_separately(self):
        g = _empty_graph()
        g.add((uri("hasAdmittime"), RDF.type, OWL.DatatypeProperty))
        result = sweep_provenance_annotations(g)
        assert result.synthesised_data_properties == 1
        assert result.synthesised_object_properties == 0

    def test_mixed_properties_counted_by_type(self):
        g = _empty_graph()
        g.add((uri("hasAdmittime"), RDF.type, OWL.DatatypeProperty))
        g.add((uri("hasDischtime"), RDF.type, OWL.DatatypeProperty))
        g.add((uri("hasPatient"), RDF.type, OWL.ObjectProperty))
        result = sweep_provenance_annotations(g)
        assert result.synthesised_data_properties == 2
        assert result.synthesised_object_properties == 1
        assert result.synthesised_properties == 3

    def test_already_annotated_obj_prop_not_touched(self):
        g = _graph_with_property(
            "hasPatient",
            prop_type=OWL.ObjectProperty,
            fm_source_fk="admissions.subject_id -> patients",
        )
        result = sweep_provenance_annotations(g)
        assert result.synthesised_object_properties == 0


# ---------------------------------------------------------------------------
# ObjectProperty FK recovery from mapping_report
# ---------------------------------------------------------------------------

def _report_with_obj_prop(
    source_table: str = "admissions",
    target_table: str = "patients",
    source_col: str = "subject_id",
    prop_uri: str = f"{BASE}hasPatient",
    class_uri: str = f"{BASE}Admission",
    target_class_uri: str = f"{BASE}Patient",
) -> SemanticMappingReport:
    """Report with two tables: source and target, object property on source."""
    return SemanticMappingReport(
        ontology_uri="http://example.org/ont",
        database_name="test_db",
        settings_used={},
        total_pending_review=0,
        tables=[
            TableMapping(
                source_table=source_table,
                class_candidate_set=[ClassCandidate(
                    uri=class_uri, label="Admission",
                    rationale="test", confidence_score=1.0,
                )],
                column_mappings=[PropertyMapping(
                    source_column=source_col,
                    source_data_type="INT",
                    is_nullable=False,
                    mapped_property_uri=prop_uri,
                    confidence_score=1.0,
                    status=MappingStatus.CONFIRMED,
                    rationale="test",
                )],
            ),
            TableMapping(
                source_table=target_table,
                class_candidate_set=[ClassCandidate(
                    uri=target_class_uri, label="Patient",
                    rationale="test", confidence_score=1.0,
                )],
                column_mappings=[],
            ),
        ],
    )


class TestObjectPropertyFKRecovery:
    def test_fk_recovered_from_range_and_report(self):
        """When report maps prop to source col, and range URI maps to target table."""
        g = _empty_graph()
        prop = uri("hasPatient")
        g.add((prop, RDF.type, OWL.ObjectProperty))
        g.add((prop, RDFS.range, uri("Patient")))

        report = _report_with_obj_prop(
            source_table="admissions",
            target_table="patients",
            source_col="subject_id",
            prop_uri=f"{BASE}hasPatient",
            target_class_uri=f"{BASE}Patient",
        )
        sweep_provenance_annotations(g, report)
        fk_val = str(list(g.objects(prop, FM.sourceForeignKey))[0])
        assert "admissions" in fk_val
        assert "subject_id" in fk_val
        assert "patients" in fk_val
        assert "->" in fk_val

    def test_fk_partial_when_target_table_unknown(self):
        """Report maps prop to source col but range URI not in report."""
        g = _empty_graph()
        prop = uri("hasPatient")
        g.add((prop, RDF.type, OWL.ObjectProperty))

        report = _report_with_obj_prop(
            source_table="admissions",
            source_col="subject_id",
            prop_uri=f"{BASE}hasPatient",
            target_class_uri=f"{BASE}UnknownClass",
        )
        sweep_provenance_annotations(g, report)
        fk_val = str(list(g.objects(prop, FM.sourceForeignKey))[0])
        assert "admissions" in fk_val
        assert "subject_id" in fk_val
        assert "target unknown" in fk_val

    def test_fk_degraded_fallback_when_not_in_report(self):
        """No report entry → 'not recoverable' fallback."""
        g = _empty_graph()
        prop = uri("hasUnknownFk")
        g.add((prop, RDF.type, OWL.ObjectProperty))
        g.add((prop, RDFS.range, uri("SomeClass")))

        result = sweep_provenance_annotations(g)
        fk_val = str(list(g.objects(prop, FM.sourceForeignKey))[0])
        assert fk_val == "not recoverable"
        assert result.synthesised_object_properties == 1

    def test_warning_logged_for_each_synthesised_obj_prop(self, caplog):
        import logging
        g = _empty_graph()
        g.add((uri("hasSomeFk"), RDF.type, OWL.ObjectProperty))
        with caplog.at_level(logging.WARNING, logger="semantic_mapper.ontology.annotation_sweep"):
            sweep_provenance_annotations(g)
        assert any("hasSomeFk" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# remove_legacy_provenance_comments
# ---------------------------------------------------------------------------

class TestRemoveLegacyProvenanceComments:
    def test_remove_legacy_provenance_comments(self):
        g = _empty_graph()
        cls = uri("Admission")
        g.add((cls, RDF.type, OWL.Class))
        g.add((cls, RDFS.comment, Literal("Derived from bronze table: raw_admissions", lang="en")))
        g.add((cls, RDFS.comment, Literal("A hospital admission record.", lang="en")))

        removed = remove_legacy_provenance_comments(g)
        assert removed == 1
        remaining = [str(o) for o in g.objects(cls, RDFS.comment)]
        assert len(remaining) == 1
        assert "A hospital admission record." in remaining[0]
        assert not any("Derived from" in r for r in remaining)

    def test_clean_graph_unchanged(self):
        g = _full_graph()
        removed = remove_legacy_provenance_comments(g)
        assert removed == 0

    def test_removes_derived_from_column(self):
        g = _empty_graph()
        prop = uri("hasAdmittime")
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        g.add((prop, RDFS.comment, Literal("Derived from column: admissions.admittime (DATETIME, null_rate=0.00)", lang="en")))

        removed = remove_legacy_provenance_comments(g)
        assert removed == 1
        assert not list(g.objects(prop, RDFS.comment))

    def test_removes_derived_from_fk(self):
        g = _empty_graph()
        prop = uri("hasPatient")
        g.add((prop, RDF.type, OWL.ObjectProperty))
        g.add((prop, RDFS.comment, Literal("Derived from foreign key: admissions.subject_id → patients", lang="en")))

        removed = remove_legacy_provenance_comments(g)
        assert removed == 1
        assert not list(g.objects(prop, RDFS.comment))

    def test_non_derived_comments_preserved(self):
        g = _empty_graph()
        cls = uri("Patient")
        g.add((cls, RDF.type, OWL.Class))
        g.add((cls, RDFS.comment, Literal("A patient in the hospital system.", lang="en")))

        removed = remove_legacy_provenance_comments(g)
        assert removed == 0
        assert list(g.objects(cls, RDFS.comment))

    def test_returns_count_of_removed_triples(self):
        g = _empty_graph()
        for i in range(3):
            cls = uri(f"Class{i}")
            g.add((cls, RDF.type, OWL.Class))
            g.add((cls, RDFS.comment, Literal(f"Derived from bronze table: table_{i}", lang="en")))

        removed = remove_legacy_provenance_comments(g)
        assert removed == 3


# ---------------------------------------------------------------------------
# Mixed graph — classes and properties together
# ---------------------------------------------------------------------------

class TestMixedGraph:
    def test_correct_totals_for_mixed_graph(self):
        g = _empty_graph()
        # 2 classes: 1 annotated with fm:sourceTable, 1 missing
        g.add((uri("Admission"), RDF.type, OWL.Class))
        g.add((uri("Admission"), FM.sourceTable, Literal("admissions")))
        g.add((uri("Patient"), RDF.type, OWL.Class))
        # 3 properties: 2 annotated with fm:, 1 missing
        for local in ("hasAdmittime", "hasDischtime"):
            g.add((uri(local), RDF.type, OWL.DatatypeProperty))
            g.add((uri(local), FM.sourceColumn, Literal(f"admissions.{local}")))
            g.add((uri(local), FM.sqlType, Literal("DATETIME")))
            g.add((uri(local), FM.nullRate, Literal("0.00")))
        g.add((uri("hasSubjectId"), RDF.type, OWL.DatatypeProperty))

        result = sweep_provenance_annotations(g)

        assert result.total_classes == 2
        assert result.total_properties == 3
        assert result.synthesised_classes == 1
        assert result.synthesised_properties == 1
        assert result.unsynthesisable == 0

    def test_graph_mutated_in_place(self):
        g = _graph_with_class("Patient")
        assert not list(g.objects(uri("Patient"), FM.sourceTable))
        sweep_provenance_annotations(g)
        assert list(g.objects(uri("Patient"), FM.sourceTable))

    def test_synthesised_elements_logged(self, caplog):
        """Routine synthesis is logged at DEBUG (WARNING is reserved for elements
        whose provenance is not recoverable)."""
        import logging
        g = _graph_with_class("Patient")
        with caplog.at_level(logging.DEBUG, logger="semantic_mapper.ontology.annotation_sweep"):
            sweep_provenance_annotations(g)
        assert any("Patient" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Full coverage after sweep
# ---------------------------------------------------------------------------

class TestFullCoverageAfterSweep:
    def test_full_coverage_after_sweep(self):
        """After sweep, every element in the graph has the required fm: annotations."""
        g = _empty_graph()
        for local in ("ClassA", "ClassB"):
            g.add((uri(local), RDF.type, OWL.Class))
        for local in ("hasPropA", "hasPropB"):
            g.add((uri(local), RDF.type, OWL.DatatypeProperty))
        for local in ("hasRelA",):
            g.add((uri(local), RDF.type, OWL.ObjectProperty))

        sweep_provenance_annotations(g)

        for cls in g.subjects(RDF.type, OWL.Class):
            assert list(g.objects(cls, FM.sourceTable)), f"{cls} missing fm:sourceTable after sweep"
        for prop in g.subjects(RDF.type, OWL.DatatypeProperty):
            assert list(g.objects(prop, FM.sourceColumn)), f"{prop} missing fm:sourceColumn"
            assert list(g.objects(prop, FM.sqlType)), f"{prop} missing fm:sqlType"
            assert list(g.objects(prop, FM.nullRate)), f"{prop} missing fm:nullRate"
        for prop in g.subjects(RDF.type, OWL.ObjectProperty):
            assert list(g.objects(prop, FM.sourceForeignKey)), f"{prop} missing fm:sourceForeignKey"

    def test_legacy_owls_with_mapping_report_recover_fk(self):
        """Legacy OWL + mapping_report: FK provenance recovered for obj properties."""
        g = _empty_graph()
        prop = uri("hasPatient")
        g.add((prop, RDF.type, OWL.ObjectProperty))
        g.add((prop, RDFS.range, uri("Patient")))

        report = _report_with_obj_prop(
            source_table="admissions",
            target_table="patients",
            source_col="subject_id",
            prop_uri=f"{BASE}hasPatient",
            target_class_uri=f"{BASE}Patient",
        )
        result = sweep_provenance_annotations(g, report)
        fk_val = str(list(g.objects(prop, FM.sourceForeignKey))[0])
        assert "admissions" in fk_val
        assert "subject_id" in fk_val
        assert result.synthesised_object_properties == 1

    def test_only_missing_fm_annotations_synthesised(self):
        """Properties with existing fm: annotations are not touched; those without get backfilled."""
        g = _empty_graph()
        dp = uri("hasAdmittime")
        op = uri("hasPatient")
        g.add((dp, RDF.type, OWL.DatatypeProperty))
        g.add((dp, FM.sourceColumn, Literal("admissions.admittime")))
        g.add((dp, FM.sqlType, Literal("DATETIME")))
        g.add((dp, FM.nullRate, Literal("0.00")))
        g.add((op, RDF.type, OWL.ObjectProperty))
        # ObjectProperty has NO fm:sourceForeignKey

        result = sweep_provenance_annotations(g)
        assert result.synthesised_data_properties == 0
        assert result.synthesised_object_properties == 1
        # DataProperty fm annotations unchanged
        assert str(list(g.objects(dp, FM.sourceColumn))[0]) == "admissions.admittime"
        # ObjectProperty gets fallback
        fk_val = str(list(g.objects(op, FM.sourceForeignKey))[0])
        assert fk_val == "not recoverable"
