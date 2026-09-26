# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
TypeMatcher FK scoring regression tests.

FK columns reference entity instances, so ObjectProperty is the correct
structural type.  TypeMatcher must:
  - Score DatatypeProperties 0.0 when is_fk=True (eliminated by aggregator type gate)
  - Reward ObjectProperties to 0.80 when is_fk=True

is_fk is True when:
  (a) the column name appears in table_profile.foreign_keys (confirmed), OR
  (b) the column name ends with "_id" (heuristic)

Non-FK columns retain the original score matrix.
"""
from __future__ import annotations

import pytest
import rdflib
from rdflib import OWL, RDF, RDFS, Namespace

from semantic_mapper.config.settings import Settings
from semantic_mapper.db.models import ColumnProfile, TableProfile
from semantic_mapper.engine.strategies.type_matcher import TypeMatcher

XSD = Namespace("http://www.w3.org/2001/XMLSchema#")
NS = "http://test.org/ontology#"


@pytest.fixture()
def settings():
    return Settings(DATABASE_URL="sqlite:///:memory:", OPENAI_API_KEY="x")


def _make_graph(*, datatype_uri: str, object_uri: str, dt_range=None) -> rdflib.Graph:
    g = rdflib.Graph()
    dp = rdflib.URIRef(datatype_uri)
    g.add((dp, RDF.type, OWL.DatatypeProperty))
    if dt_range is not None:
        g.add((dp, RDFS.range, rdflib.URIRef(dt_range)))
    op = rdflib.URIRef(object_uri)
    g.add((op, RDF.type, OWL.ObjectProperty))
    return g


def _col(name: str, sql_type: str = "INT") -> ColumnProfile:
    return ColumnProfile(
        name=name, inferred_type=sql_type,
        is_nullable=False, is_primary_key=False,
        null_rate=0.0, distinct_count=100, top_values=[],
    )


def _table(*, fk_col: str | None = None) -> TableProfile:
    fks = [{"column": fk_col, "ref_table": "patients"}] if fk_col else []
    return TableProfile(table_name="raw_admissions", row_count=1000, columns={}, foreign_keys=fks)


def _scores(matcher: TypeMatcher, col: ColumnProfile, table: TableProfile) -> dict:
    return {ev.mapped_uri: ev.score for ev in matcher.evaluate_column(col, table)}


# ---------------------------------------------------------------------------
# Confirmed FK (column appears in table_profile.foreign_keys)
# ---------------------------------------------------------------------------

class TestConfirmedFk:
    def test_datatype_property_gets_zero(self, settings):
        g = _make_graph(
            datatype_uri=f"{NS}hasSubjectId",
            object_uri=f"{NS}hasSubject",
            dt_range=str(XSD.integer),
        )
        m = TypeMatcher(g, settings)
        scores = _scores(m, _col("subject_id"), _table(fk_col="subject_id"))
        assert scores[f"{NS}hasSubjectId"] == pytest.approx(0.0)

    def test_object_property_gets_boost(self, settings):
        g = _make_graph(
            datatype_uri=f"{NS}hasSubjectId",
            object_uri=f"{NS}hasSubject",
            dt_range=str(XSD.integer),
        )
        m = TypeMatcher(g, settings)
        scores = _scores(m, _col("subject_id"), _table(fk_col="subject_id"))
        assert scores[f"{NS}hasSubject"] == pytest.approx(0.80)

    def test_object_property_beats_datatype_property(self, settings):
        g = _make_graph(
            datatype_uri=f"{NS}hasSubjectId",
            object_uri=f"{NS}hasSubject",
            dt_range=str(XSD.integer),
        )
        m = TypeMatcher(g, settings)
        scores = _scores(m, _col("subject_id"), _table(fk_col="subject_id"))
        assert scores[f"{NS}hasSubject"] > scores[f"{NS}hasSubjectId"]

    def test_rationale_mentions_fk_exclusion(self, settings):
        g = _make_graph(
            datatype_uri=f"{NS}hasSubjectId",
            object_uri=f"{NS}hasSubject",
            dt_range=str(XSD.integer),
        )
        m = TypeMatcher(g, settings)
        evs = m.evaluate_column(_col("subject_id"), _table(fk_col="subject_id"))
        dp_ev = next(e for e in evs if e.mapped_uri == f"{NS}hasSubjectId")
        assert "FK" in dp_ev.rationale or "entity" in dp_ev.rationale.lower()

    def test_rationale_mentions_fk_boost(self, settings):
        g = _make_graph(
            datatype_uri=f"{NS}hasSubjectId",
            object_uri=f"{NS}hasSubject",
            dt_range=str(XSD.integer),
        )
        m = TypeMatcher(g, settings)
        evs = m.evaluate_column(_col("subject_id"), _table(fk_col="subject_id"))
        op_ev = next(e for e in evs if e.mapped_uri == f"{NS}hasSubject")
        assert "FK" in op_ev.rationale or "entity" in op_ev.rationale.lower()


# ---------------------------------------------------------------------------
# Heuristic FK (column name ends with "_id", not in foreign_keys list)
# ---------------------------------------------------------------------------

class TestHeuristicFk:
    def test_id_suffix_column_zeros_datatype_property(self, settings):
        g = _make_graph(
            datatype_uri=f"{NS}hasAdmitId",
            object_uri=f"{NS}hasAdmission",
            dt_range=str(XSD.integer),
        )
        m = TypeMatcher(g, settings)
        scores = _scores(m, _col("admit_id"), _table())  # no confirmed FK
        assert scores[f"{NS}hasAdmitId"] == pytest.approx(0.0)

    def test_id_suffix_column_boosts_object_property(self, settings):
        g = _make_graph(
            datatype_uri=f"{NS}hasAdmitId",
            object_uri=f"{NS}hasAdmission",
            dt_range=str(XSD.integer),
        )
        m = TypeMatcher(g, settings)
        scores = _scores(m, _col("admit_id"), _table())
        assert scores[f"{NS}hasAdmission"] == pytest.approx(0.80)


# ---------------------------------------------------------------------------
# Non-FK column — original score matrix unchanged
# ---------------------------------------------------------------------------

class TestNonFkColumn:
    def test_non_fk_datatype_property_gets_full_type_match(self, settings):
        g = _make_graph(
            datatype_uri=f"{NS}hasDob",
            object_uri=f"{NS}hasPatient",
            dt_range=str(XSD.integer),
        )
        m = TypeMatcher(g, settings)
        # "dob" does not end with "_id", not in FK list
        scores = _scores(m, _col("dob", "INT"), _table())
        assert scores[f"{NS}hasDob"] == pytest.approx(0.80)  # exact XSD match

    def test_non_fk_object_property_gets_zero(self, settings):
        g = _make_graph(
            datatype_uri=f"{NS}hasDob",
            object_uri=f"{NS}hasPatient",
            dt_range=str(XSD.integer),
        )
        m = TypeMatcher(g, settings)
        scores = _scores(m, _col("dob", "INT"), _table())
        assert scores[f"{NS}hasPatient"] == pytest.approx(0.0)

    def test_non_fk_datatype_beats_object(self, settings):
        g = _make_graph(
            datatype_uri=f"{NS}hasDob",
            object_uri=f"{NS}hasPatient",
            dt_range=str(XSD.integer),
        )
        m = TypeMatcher(g, settings)
        scores = _scores(m, _col("dob", "INT"), _table())
        assert scores[f"{NS}hasDob"] > scores[f"{NS}hasPatient"]
