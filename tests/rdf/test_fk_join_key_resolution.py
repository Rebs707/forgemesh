# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""P0 regression: FK-target object URIs must resolve to real entity subjects.

The bug: an entity's subject URI was keyed on its detected id_column (a surrogate PK
such as ``row_id``), but FK columns in other tables carry the *join key*
(``hadm_id``). The two hash to different URIs, so every object link dangles.

Fix: when an entity is the TARGET of FK object properties whose source column
name-matches one of its own columns, and that column is unique, key the subject on
that join column — even over a surrogate PK. Verified here on a synthetic gold
fixture with generic names (org / visit); no dataset-specific strings in the logic
under test.
"""
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, Graph, URIRef
from sqlalchemy import create_engine, text

from semantic_mapper.gold.models import GoldColumnSpec, GoldTableDefinition
from semantic_mapper.gold.builder import derive_fk_join_candidates, refine_id_columns
from semantic_mapper.rdf.triple_builder import TripleBuilder
from semantic_mapper.rdf.uri_factory import UriFactory, hash_identifier

BASE = "http://mesh.local/data/"
ORG_CLASS = "http://ex.org/ontology#Org"
VISIT_CLASS = "http://ex.org/ontology#Visit"
HAS_VISIT_ORG = "http://ex.org/ontology#hasVisitOrg"


def _spec(col, prop=""):
    return GoldColumnSpec(
        silver_table="silver_x",
        source_column=col,
        gold_column_name=col,
        source_data_type="INT",
        property_uri=prop,
    )


def _org_def(id_column="row_id"):
    # Parent entity: surrogate PK row_id + business join key org_id.
    return GoldTableDefinition(
        gold_table_name="gold_org",
        primary_class_uri=ORG_CLASS,
        class_label="org",
        source_silver_tables=["silver_raw_org"],
        column_specs=[_spec("row_id"), _spec("org_id")],
        id_column=id_column,   # pre-fix: the surrogate PK — triggers the dangling bug
    )


def _visit_def():
    # Child: references Org via org_id (an OWL ObjectProperty with range=Org).
    return GoldTableDefinition(
        gold_table_name="gold_visit",
        primary_class_uri=VISIT_CLASS,
        class_label="visit",
        source_silver_tables=["silver_raw_visit"],
        column_specs=[_spec("row_id"), _spec("org_id", prop=HAS_VISIT_ORG)],
        id_column="row_id",
    )


def _ontology():
    g = Graph()
    p = URIRef(HAS_VISIT_ORG)
    g.add((p, RDF.type, OWL.ObjectProperty))
    g.add((p, RDFS.range, URIRef(ORG_CLASS)))
    return g


@pytest.fixture
def gold_engine():
    """In-memory gold DB: org (surrogate row_id + unique org_id) and visit (FK org_id)."""
    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE gold_org (row_id INT, org_id INT)"))
        conn.execute(text("INSERT INTO gold_org VALUES (1,100),(2,200),(3,300)"))
        conn.execute(text("CREATE TABLE gold_visit (row_id INT, org_id INT)"))
        conn.execute(text("INSERT INTO gold_visit VALUES (1,100),(2,200)"))
    return eng


class TestJoinKeyDerivation:
    def test_name_matched_fk_target_column_detected(self):
        cands = derive_fk_join_candidates([_org_def(), _visit_def()], _ontology())
        assert cands == {"gold_org": {"org_id"}}

    def test_no_ontology_returns_empty(self):
        assert derive_fk_join_candidates([_org_def(), _visit_def()], None) == {}

    def test_fk_column_not_present_in_parent_is_not_a_candidate(self):
        # If the parent has no column of the FK's name (e.g. Synthea encounter.patient
        # → patients.id, names differ), there is no name-match → no override, and the
        # PK path (which already resolves for such datasets) is left untouched.
        visit = _visit_def().model_copy(
            update={"column_specs": [_spec("row_id"), _spec("org_ref", prop=HAS_VISIT_ORG)]}
        )
        assert derive_fk_join_candidates([_org_def(), visit], _ontology()) == {}


class TestRefinePicksJoinKey:
    def test_join_key_overrides_surrogate_pk(self, gold_engine):
        cands = derive_fk_join_candidates([_org_def(), _visit_def()], _ontology())
        # has_pk=True for the parent — the fix must override PK trust, not defer to it.
        refined = refine_id_columns(
            [_org_def(), _visit_def()], gold_engine,
            pk_source_tables={"raw_org", "raw_visit"}, fk_target_cols=cands,
        )
        org = next(d for d in refined if d.gold_table_name == "gold_org")
        assert org.id_column == "org_id"   # NOT the surrogate "row_id"

    def test_nonunique_name_match_is_rejected(self):
        # A coincidental name match on a NON-unique column must not become the key.
        eng = create_engine("sqlite:///:memory:")
        with eng.begin() as conn:
            conn.execute(text("CREATE TABLE gold_org (row_id INT, org_id INT)"))
            conn.execute(text("INSERT INTO gold_org VALUES (1,100),(2,100),(3,200)"))  # org_id NOT unique
            conn.execute(text("CREATE TABLE gold_visit (row_id INT, org_id INT)"))
            conn.execute(text("INSERT INTO gold_visit VALUES (1,100)"))
        cands = derive_fk_join_candidates([_org_def(), _visit_def()], _ontology())
        refined = refine_id_columns(
            [_org_def(), _visit_def()], eng,
            pk_source_tables={"raw_org", "raw_visit"}, fk_target_cols=cands,
        )
        org = next(d for d in refined if d.gold_table_name == "gold_org")
        assert org.id_column != "org_id"   # uniqueness guard rejected it


class TestLinkResolves:
    """The two mechanical checks from the bug's definition of done."""

    def _build_all(self, gold_engine):
        cands = derive_fk_join_candidates([_org_def(), _visit_def()], _ontology())
        refined = refine_id_columns(
            [_org_def(), _visit_def()], gold_engine,
            pk_source_tables={"raw_org", "raw_visit"}, fk_target_cols=cands,
        )
        builder = TripleBuilder(
            definitions=refined, uri_factory=UriFactory(BASE), ontology_graph=_ontology()
        )
        org = next(d for d in refined if d.gold_table_name == "gold_org")
        visit = next(d for d in refined if d.gold_table_name == "gold_visit")
        org_triples = builder.build(
            {"_ontology_class_uri": ORG_CLASS, "_bronze_source_ref": "raw_org.row_id=1",
             "row_id": 1, "org_id": 100}, org)
        visit_triples = builder.build(
            {"_ontology_class_uri": VISIT_CLASS, "_bronze_source_ref": "raw_visit.row_id=1",
             "row_id": 1, "org_id": 100}, visit)
        return org_triples, visit_triples

    def test_fk_target_uri_equals_target_subject_uri(self, gold_engine):
        org_triples, visit_triples = self._build_all(gold_engine)
        org_subject = next(s for s, p, o in org_triples if p == RDF.type)
        fk_target = next(o for s, p, o in visit_triples if str(p) == HAS_VISIT_ORG)
        assert str(fk_target) == str(org_subject)                       # string-equal
        assert str(fk_target) == f"{BASE}org/{hash_identifier('100')}"  # keyed on org_id, not row_id

    def test_link_resolves_to_a_typed_subject(self, gold_engine):
        org_triples, visit_triples = self._build_all(gold_engine)
        g = Graph()
        for t in list(org_triples) + list(visit_triples):
            g.add(t)
        fk_target = next(o for s, p, o in visit_triples if str(p) == HAS_VISIT_ORG)
        assert (fk_target, RDF.type, URIRef(ORG_CLASS)) in g   # target is a real, typed subject
