# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for AnnotationMatcher.evaluate_table — the source-table entity match.

A table's primary ENTITY class is the ontology class the generator annotated with
``fm:sourceTable = <table>`` AND that owns the table's columns as ``rdfs:domain``.
Value-mapped vocabulary classes minted from the same table (the RANGE of its object
properties) are excluded because they are the domain of none of the table's columns.

This is the only signal that connects a table to its entity class when the table is
NAMED after a measure but KEYED by a different entity — e.g. a per-group tally table
whose rows are really the group dimension.  Surface-name matching links the table to
the wrong class; the fm:sourceTable provenance resolves it.

All fixtures use generic table/column names — the rule is structural, not dataset-specific.
"""
from __future__ import annotations

import pytest
import rdflib
from rdflib import OWL, RDF, RDFS, Literal, Namespace, URIRef

from semantic_mapper.config.settings import Settings
from semantic_mapper.db.models import ColumnProfile, TableProfile
from semantic_mapper.engine.strategies.annotation_matcher import AnnotationMatcher

_FM = Namespace("http://forgemesh.ai/ontology#")
_BASE = "http://test.org/ontology#"


@pytest.fixture()
def settings():
    return Settings(DATABASE_URL="sqlite:///:memory:", OPENAI_API_KEY="x")


def _cls(g: rdflib.Graph, local: str, source_table: str | None = None) -> URIRef:
    uri = URIRef(_BASE + local)
    g.add((uri, RDF.type, OWL.Class))
    if source_table is not None:
        g.add((uri, _FM.sourceTable, Literal(source_table)))
    return uri


def _data_prop(g: rdflib.Graph, local: str, domain: URIRef, source_column: str) -> URIRef:
    uri = URIRef(_BASE + local)
    g.add((uri, RDF.type, OWL.DatatypeProperty))
    g.add((uri, RDFS.domain, domain))
    g.add((uri, _FM.sourceColumn, Literal(source_column)))
    return uri


def _obj_prop(g: rdflib.Graph, local: str, domain: URIRef, rng: URIRef,
              source_fk: str) -> URIRef:
    uri = URIRef(_BASE + local)
    g.add((uri, RDF.type, OWL.ObjectProperty))
    g.add((uri, RDFS.domain, domain))
    g.add((uri, RDFS.range, rng))
    g.add((uri, _FM.sourceForeignKey, Literal(source_fk)))
    return uri


def _table(name: str) -> TableProfile:
    return TableProfile(table_name=name, row_count=100, columns={}, foreign_keys=[])


class TestMeasureNamedDimension:
    """A table named after a measure but keyed by an entity resolves to the entity."""

    def _graph(self) -> rdflib.Graph:
        g = rdflib.Graph()
        # `raw_tally` is the group dimension: PK group_code, columns are measures.
        # The generator named the entity class `Group` (PK denotes the entity) and
        # stamped fm:sourceTable=raw_tally on it.
        group = _cls(g, "Group", source_table="raw_tally")
        _data_prop(g, "hasWins", group, "raw_tally.wins")
        _data_prop(g, "hasLosses", group, "raw_tally.losses")
        _data_prop(g, "hasGroupName", group, "raw_tally.group_name")
        # A name-collision class sourced from a DIFFERENT table — what surface-name
        # matching would wrongly pick for `raw_tally`.
        _cls(g, "Tally", source_table="raw_events")
        return g

    def test_picks_entity_class_not_name_collision(self, settings):
        am = AnnotationMatcher(self._graph(), settings)
        evs = am.evaluate_table(_table("raw_tally"))
        assert len(evs) == 1
        assert evs[0].mapped_uri == _BASE + "Group"

    def test_emitted_at_authoritative_score(self, settings):
        am = AnnotationMatcher(self._graph(), settings)
        evs = am.evaluate_table(_table("raw_tally"))
        # Score must beat NameMatcher's surface guesses (which cap below 1.0).
        assert evs[0].score == pytest.approx(1.0)

    def test_rationale_names_provenance(self, settings):
        am = AnnotationMatcher(self._graph(), settings)
        evs = am.evaluate_table(_table("raw_tally"))
        assert "fm:sourceTable" in evs[0].rationale


class TestValueMappedVocabExcluded:
    """A value-mapped vocabulary class sourced from the same table is NOT the entity."""

    def _graph(self) -> rdflib.Graph:
        g = rdflib.Graph()
        # `raw_people` → entity Person (owns the columns) + value-mapped Status
        # (the RANGE of an object property, domain of no column).
        person = _cls(g, "Person", source_table="raw_people")
        status = _cls(g, "Status", source_table="raw_people")
        _data_prop(g, "hasFullName", person, "raw_people.full_name")
        _data_prop(g, "hasAge", person, "raw_people.age")
        # Status is a range, never a domain of a raw_people column.
        _obj_prop(g, "hasPersonStatus", person, status, "raw_people.status -> Status")
        return g

    def test_entity_owns_columns_vocab_does_not(self, settings):
        am = AnnotationMatcher(self._graph(), settings)
        evs = am.evaluate_table(_table("raw_people"))
        assert len(evs) == 1
        assert evs[0].mapped_uri == _BASE + "Person"

    def test_vocab_class_never_emitted_as_entity(self, settings):
        am = AnnotationMatcher(self._graph(), settings)
        evs = am.evaluate_table(_table("raw_people"))
        assert all(e.mapped_uri != _BASE + "Status" for e in evs)


class TestFallbackWhenNoProvenance:
    """No fm:sourceTable provenance → empty, leaving NameMatcher behaviour untouched."""

    def test_no_source_table_annotation_returns_empty(self, settings):
        g = rdflib.Graph()
        cls = _cls(g, "Thing", source_table=None)  # no fm:sourceTable
        _data_prop(g, "hasX", cls, "raw_thing.x")
        am = AnnotationMatcher(g, settings)
        assert am.evaluate_table(_table("raw_thing")) == []

    def test_unknown_table_returns_empty(self, settings):
        g = rdflib.Graph()
        e = _cls(g, "Known", source_table="raw_known")
        _data_prop(g, "hasY", e, "raw_known.y")
        am = AnnotationMatcher(g, settings)
        assert am.evaluate_table(_table("raw_other")) == []

    def test_source_table_class_with_no_owned_columns_returns_empty(self, settings):
        # A class stamped fm:sourceTable but owning none of the table's columns
        # (no rdfs:domain backing) is not a usable entity signal.
        g = rdflib.Graph()
        c = _cls(g, "Empty", source_table="raw_empty")
        # property sourced from a different table — does not credit raw_empty
        other = _cls(g, "Other", source_table="raw_other")
        _data_prop(g, "hasZ", other, "raw_other.z")
        am = AnnotationMatcher(g, settings)
        assert am.evaluate_table(_table("raw_empty")) == []


class TestCaseInsensitiveTableMatch:
    """fm:sourceTable / fm:sourceColumn comparison is case-robust."""

    def test_mixed_case_table_name(self, settings):
        g = rdflib.Graph()
        e = _cls(g, "Order", source_table="Raw_Orders")
        _data_prop(g, "hasOrderTotal", e, "Raw_Orders.total")
        am = AnnotationMatcher(g, settings)
        evs = am.evaluate_table(_table("raw_orders"))
        assert len(evs) == 1
        assert evs[0].mapped_uri == _BASE + "Order"
