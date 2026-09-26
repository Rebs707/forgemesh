# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Acceptance tests for three iterative-generator structural postconditions.

Each test class covers one failure mode observed in the live MIMIC-III graph
(May 2026).  Every "bad input" test documents a bug the current generator
produces; the corresponding "good input" test documents what the fixed
generator must produce instead.

G1 — Scalar-as-ObjectProperty
     Transfer.outtime and Transfer.los were declared owl:ObjectProperty with
     rdfs:range Icustay.  Both are scalar (DATETIME, FLOAT).  258 quality
     triples emitted instead of typed literal values.

G2 — Split-pass column coverage
     InputeventsCv columns (charttime, newbottle, amount, …) shared names with
     InputeventsMv and other tables.  The split pass produced MV-scoped
     properties but left CV subjects using foreign predicates
     (hasMicrobiologyeventChartTime, hasOutputeventNewbottle, hasInputeventsMv*).

G3 — Cross-table property reuse for structurally-identical tables
     IcdDiagnose reused hasIcdProcedureShortTitle and hasIcdProcedureLongTitle
     from IcdProcedure because raw_d_icd_diagnoses and raw_d_icd_procedures have
     identical schemas.  IcdDiagnose subjects carried procedure predicates.
"""
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef
from rdflib.namespace import SKOS, XSD

from semantic_mapper.ontology.iterative.models import ColumnContext
from semantic_mapper.ontology.iterative.postconditions import (
    check_no_cross_table_property_reuse,
    check_no_scalar_as_object_property,
    check_split_column_coverage,
)

BASE = "http://mesh.local/ontology/test#"


def uri(local: str) -> URIRef:
    return URIRef(f"{BASE}{local}")


# ── helpers ──────────────────────────────────────────────────────────────────

def _col(table: str, col: str, inferred_type: str, is_fk: bool = False) -> ColumnContext:
    return ColumnContext(
        table_name=table, col_name=col, inferred_type=inferred_type,
        is_pk=False, is_fk=is_fk, is_nullable=True,
        null_rate=0.0, distinct_count=10,
    )


def _dt_prop(g: Graph, local: str, domain: str, alt: str | None = None) -> None:
    p = uri(local)
    g.add((p, RDF.type, OWL.DatatypeProperty))
    g.add((p, RDFS.domain, uri(domain)))
    g.add((p, RDFS.range, XSD.string))
    if alt:
        g.add((p, SKOS.altLabel, Literal(alt, lang="en")))


def _obj_prop(g: Graph, local: str, domain: str, range_: str, alt: str | None = None) -> None:
    p = uri(local)
    g.add((p, RDF.type, OWL.ObjectProperty))
    g.add((p, RDFS.domain, uri(domain)))
    g.add((p, RDFS.range, uri(range_)))
    if alt:
        g.add((p, SKOS.altLabel, Literal(alt, lang="en")))


def _owl_class(g: Graph, local: str) -> None:
    g.add((uri(local), RDF.type, OWL.Class))


# ── G1: Scalar-as-ObjectProperty ─────────────────────────────────────────────

class TestScalarAsObjectProperty:
    """G1 — a DATETIME/FLOAT column must never be declared owl:ObjectProperty."""

    def test_datetime_as_dataproperty_no_violation(self):
        g = Graph()
        _owl_class(g, "Transfer")
        _owl_class(g, "Icustay")
        _dt_prop(g, "hasTransferIntime", "Transfer", alt="raw_transfers.intime")

        cols = [_col("raw_transfers", "intime", "DATETIME")]
        violations = check_no_scalar_as_object_property(g, cols)
        assert violations == []

    def test_datetime_as_objectproperty_detected(self):
        """G1 bug: outtime (DATETIME) declared ObjectProperty with range Icustay."""
        g = Graph()
        _owl_class(g, "Transfer")
        _owl_class(g, "Icustay")
        # Bug: outtime is a timestamp but declared as FK to Icustay
        _obj_prop(g, "hasTransferOuttime", "Transfer", "Icustay",
                  alt="raw_transfers.outtime")

        cols = [_col("raw_transfers", "outtime", "DATETIME")]
        violations = check_no_scalar_as_object_property(g, cols)
        assert len(violations) == 1
        assert "hasTransferOuttime" in violations[0]
        assert "G1" in violations[0]

    def test_float_los_as_objectproperty_detected(self):
        """G1 bug: los (FLOAT) declared ObjectProperty with range Icustay."""
        g = Graph()
        _owl_class(g, "Transfer")
        _owl_class(g, "Icustay")
        _obj_prop(g, "hasTransferLos", "Transfer", "Icustay",
                  alt="raw_transfers.los")

        cols = [_col("raw_transfers", "los", "FLOAT")]
        violations = check_no_scalar_as_object_property(g, cols)
        assert len(violations) == 1
        assert "hasTransferLos" in violations[0]
        assert "G1" in violations[0]

    def test_fk_int_as_objectproperty_no_violation(self):
        """A real FK (INT pointing to another entity) is correctly ObjectProperty."""
        g = Graph()
        _owl_class(g, "Transfer")
        _owl_class(g, "Admission")
        _obj_prop(g, "hasTransferAdmission", "Transfer", "Admission",
                  alt="raw_transfers.hadm_id")

        # hadm_id is INT but it's an FK — the checker should not fire for FK columns
        cols = [_col("raw_transfers", "hadm_id", "INT", is_fk=True)]
        violations = check_no_scalar_as_object_property(g, cols)
        assert violations == []

    def test_multiple_scalar_violations_all_reported(self):
        """Both outtime and los violations are reported in one call."""
        g = Graph()
        _owl_class(g, "Transfer")
        _owl_class(g, "Icustay")
        _obj_prop(g, "hasTransferOuttime", "Transfer", "Icustay",
                  alt="raw_transfers.outtime")
        _obj_prop(g, "hasTransferLos", "Transfer", "Icustay",
                  alt="raw_transfers.los")

        cols = [
            _col("raw_transfers", "outtime", "DATETIME"),
            _col("raw_transfers", "los", "FLOAT"),
        ]
        violations = check_no_scalar_as_object_property(g, cols)
        assert len(violations) == 2


# ── G2: Split-pass column coverage ───────────────────────────────────────────

class TestSplitPassColumnCoverage:
    """G2 — after split, every (table, column) altLabel must be covered by a
    domain-matching property."""

    def test_both_tables_covered_no_violation(self):
        """After a correct split, each table's columns have domain-scoped properties."""
        g = Graph()
        _owl_class(g, "InputeventsMv")
        _owl_class(g, "InputeventsCv")

        # Correct split: each table has its own charttime property
        _dt_prop(g, "hasInputeventsMvCharttime", "InputeventsMv",
                 alt="raw_inputevents_mv.charttime")
        _dt_prop(g, "hasInputeventsCvCharttime", "InputeventsCv",
                 alt="raw_inputevents_cv.charttime")

        table_map = {
            "raw_inputevents_mv": f"{BASE}InputeventsMv",
            "raw_inputevents_cv": f"{BASE}InputeventsCv",
        }
        violations = check_split_column_coverage(g, table_map)
        assert violations == []

    def test_cv_table_left_without_scoped_property_detected(self):
        """G2 bug: charttime split only produced MV property; CV left uncovered."""
        g = Graph()
        _owl_class(g, "InputeventsMv")
        _owl_class(g, "InputeventsCv")

        # Bug: only MV-scoped property created; CV altLabel present but domain missing
        _dt_prop(g, "hasInputeventsMvCharttime", "InputeventsMv",
                 alt="raw_inputevents_mv.charttime")
        # The CV altLabel is on the MV property — bleed scenario
        mv_prop = uri("hasInputeventsMvCharttime")
        g.add((mv_prop, SKOS.altLabel, Literal("raw_inputevents_cv.charttime", lang="en")))

        table_map = {
            "raw_inputevents_mv": f"{BASE}InputeventsMv",
            "raw_inputevents_cv": f"{BASE}InputeventsCv",
        }
        violations = check_split_column_coverage(g, table_map)
        assert len(violations) >= 1
        assert any("raw_inputevents_cv" in v or "InputeventsCv" in v for v in violations)
        assert any("G2" in v for v in violations)

    def test_single_table_schema_no_violation(self):
        """A property with exactly one table's altLabel and matching domain is clean."""
        g = Graph()
        _owl_class(g, "Outputevent")
        _dt_prop(g, "hasOutputeventCharttime", "Outputevent",
                 alt="raw_outputevents.charttime")

        table_map = {"raw_outputevents": f"{BASE}Outputevent"}
        violations = check_split_column_coverage(g, table_map)
        assert violations == []


# ── G3: Cross-table property reuse ───────────────────────────────────────────

class TestCrossTablePropertyReuse:
    """G3 — a new table fragment must not reuse properties already scoped to
    a different class in the accumulating graph."""

    def test_fresh_properties_no_violation(self):
        """New table introduces brand-new property URIs — no conflict."""
        accumulating = Graph()
        _owl_class(accumulating, "IcdProcedure")
        _dt_prop(accumulating, "hasIcdProcedureShortTitle", "IcdProcedure",
                 alt="raw_d_icd_procedures.short_title")

        new_fragment = Graph()
        _owl_class(new_fragment, "IcdDiagnose")
        # Correct: uses its own scoped property
        _dt_prop(new_fragment, "hasIcdDiagnoseShortTitle", "IcdDiagnose",
                 alt="raw_d_icd_diagnoses.short_title")

        violations = check_no_cross_table_property_reuse(
            new_fragment, accumulating, f"{BASE}IcdDiagnose"
        )
        assert violations == []

    def test_reusing_procedure_property_for_diagnose_detected(self):
        """G3 bug: IcdDiagnose fragment reuses hasIcdProcedureShortTitle."""
        accumulating = Graph()
        _owl_class(accumulating, "IcdProcedure")
        _dt_prop(accumulating, "hasIcdProcedureShortTitle", "IcdProcedure",
                 alt="raw_d_icd_procedures.short_title")

        new_fragment = Graph()
        _owl_class(new_fragment, "IcdDiagnose")
        # Bug: reuses the procedure property for the diagnosis class
        _dt_prop(new_fragment, "hasIcdProcedureShortTitle", "IcdDiagnose",
                 alt="raw_d_icd_diagnoses.short_title")

        violations = check_no_cross_table_property_reuse(
            new_fragment, accumulating, f"{BASE}IcdDiagnose"
        )
        assert len(violations) == 1
        assert "hasIcdProcedureShortTitle" in violations[0]
        assert "G3" in violations[0]

    def test_both_titles_reused_both_detected(self):
        """Both short_title and long_title reuse violations are reported."""
        accumulating = Graph()
        _owl_class(accumulating, "IcdProcedure")
        _dt_prop(accumulating, "hasIcdProcedureShortTitle", "IcdProcedure")
        _dt_prop(accumulating, "hasIcdProcedureLongTitle", "IcdProcedure")

        new_fragment = Graph()
        _owl_class(new_fragment, "IcdDiagnose")
        _dt_prop(new_fragment, "hasIcdProcedureShortTitle", "IcdDiagnose")
        _dt_prop(new_fragment, "hasIcdProcedureLongTitle", "IcdDiagnose")

        violations = check_no_cross_table_property_reuse(
            new_fragment, accumulating, f"{BASE}IcdDiagnose"
        )
        assert len(violations) == 2
        local_names = " ".join(violations)
        assert "hasIcdProcedureShortTitle" in local_names
        assert "hasIcdProcedureLongTitle" in local_names

    def test_extending_own_class_no_violation(self):
        """A second fragment for the same class adding more properties is not a conflict."""
        accumulating = Graph()
        _owl_class(accumulating, "Admission")
        _dt_prop(accumulating, "hasAdmitTime", "Admission")

        new_fragment = Graph()
        # Same class, different property — legitimate extension
        _dt_prop(new_fragment, "hasDischargeTime", "Admission")

        violations = check_no_cross_table_property_reuse(
            new_fragment, accumulating, f"{BASE}Admission"
        )
        assert violations == []
