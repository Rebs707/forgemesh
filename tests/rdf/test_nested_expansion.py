# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for nested/packed cell detection and parsing."""
from __future__ import annotations

from rdflib import URIRef, Literal, RDF, RDFS, OWL

from semantic_mapper.rdf.nested_expansion import (
    parse_nested_cell, detect_nested_columns,
    nested_term_names, explode_nested_cell, nested_ontology_triples,
    reconcile_nested_keys,
)
from semantic_mapper.rdf.uri_factory import UriFactory

ONTO = "http://ex.org/onto#"
ATHLETE = ONTO + "Athlete"


class TestParseNestedCell:
    def test_python_repr_list_of_objects(self):
        v = "[{'discipline': 'Alpine Skiing', 'event': \"Men's Slalom\"}]"
        assert parse_nested_cell(v) == [{"discipline": "Alpine Skiing", "event": "Men's Slalom"}]

    def test_json_list_of_objects(self):
        assert parse_nested_cell('[{"a": 1}, {"a": 2}]') == [{"a": 1}, {"a": 2}]

    def test_multiple_objects(self):
        v = "[{'discipline': 'X', 'event': 'A'}, {'discipline': 'Y', 'event': 'B'}]"
        assert len(parse_nested_cell(v)) == 2

    def test_scalar_is_none(self):
        assert parse_nested_cell("NOR") is None

    def test_list_of_scalars_is_none(self):
        assert parse_nested_cell('["a", "b"]') is None

    def test_none_and_empty(self):
        assert parse_nested_cell(None) is None
        assert parse_nested_cell("") is None
        assert parse_nested_cell("[]") is None

    def test_unparseable_is_none(self):
        assert parse_nested_cell("[not valid") is None


class TestDetectNestedColumns:
    def test_detects_column_and_keys(self):
        rows = [
            {"id": "1", "events": "[{'discipline': 'Alpine Skiing', 'event': \"Men's Slalom\"}]"},
            {"id": "2", "events": "[{'discipline': 'Biathlon', 'event': 'Sprint'}]"},
        ]
        out = detect_nested_columns(rows, ["id", "events"])
        assert out == {"events": ["discipline", "event"]}

    def test_scalar_column_not_detected(self):
        rows = [{"country_code": "NOR"}, {"country_code": "SWE"}]
        assert detect_nested_columns(rows, ["country_code"]) == {}

    def test_audit_columns_skipped(self):
        rows = [{"_bronze_source_ref": "[{'a':1}]"}]
        assert detect_nested_columns(rows, ["_bronze_source_ref"]) == {}

    def test_min_fraction_guards_mixed_column(self):
        rows = [{"c": "[{'a': 1}]"}] + [{"c": "scalar"}] * 5
        assert detect_nested_columns(rows, ["c"], min_fraction=0.5) == {}

    def test_keys_are_union_across_rows(self):
        rows = [
            {"c": "[{'a': 1}]"},
            {"c": "[{'a': 2, 'b': 3}]"},
        ]
        assert detect_nested_columns(rows, ["c"]) == {"c": ["a", "b"]}


class TestNestedTermNames:
    def test_events_discipline_event(self):
        t = nested_term_names("events", ["discipline", "event"])
        assert t["child"] == "Event"
        assert t["parent_prop"] == "hasEvent"
        assert t["key_props"] == {"discipline": "eventDiscipline", "event": "eventName"}


class TestExplodeNestedCell:
    def _uf(self):
        return UriFactory("http://mesh.local/data/", "olympics2026")

    def test_explodes_into_shared_event_entity(self):
        uf = self._uf()
        terms = nested_term_names("events", ["discipline", "event"])
        subj = uf.individual("athlete", "139896")
        val = "[{'discipline': 'Alpine Skiing', 'event': \"Men's Slalom\"}]"
        triples = explode_nested_cell(subj, val, terms, uf, ONTO)
        # parent -> hasEvent -> event ; event a Event ; eventName / eventDiscipline literals
        hasEvent = URIRef(ONTO + "hasEvent")
        edges = [(s, p, o) for s, p, o in triples if p == hasEvent]
        assert len(edges) == 1
        ev = edges[0][2]
        assert (ev, RDF.type, URIRef(ONTO + "Event")) in triples
        assert (ev, URIRef(ONTO + "eventName"), Literal("Men's Slalom")) in triples
        assert (ev, URIRef(ONTO + "eventDiscipline"), Literal("Alpine Skiing")) in triples

    def test_same_object_is_one_shared_entity(self):
        uf = self._uf()
        terms = nested_term_names("events", ["discipline", "event"])
        val = "[{'discipline': 'Alpine Skiing', 'event': \"Men's Slalom\"}]"
        a = explode_nested_cell(uf.individual("athlete", "1"), val, terms, uf, ONTO)
        b = explode_nested_cell(uf.individual("athlete", "2"), val, terms, uf, ONTO)
        ev_a = next(o for s, p, o in a if p == URIRef(ONTO + "hasEvent"))
        ev_b = next(o for s, p, o in b if p == URIRef(ONTO + "hasEvent"))
        assert ev_a == ev_b  # both athletes point at the SAME Event entity

    def test_scalar_value_no_triples(self):
        uf = self._uf()
        terms = nested_term_names("events", ["discipline", "event"])
        assert explode_nested_cell(uf.individual("athlete", "1"), "NOR", terms, uf, ONTO) == []

    def test_edge_uses_explicit_parent_prop(self):
        # Unpacking repurposes the packed column's OWN property as the edge — the
        # derived has<Child> name is ignored when an explicit property is supplied.
        uf = self._uf()
        terms = nested_term_names("events", ["discipline", "event"])
        val = "[{'discipline': 'Alpine Skiing', 'event': \"Men's Slalom\"}]"
        col_prop = ONTO + "hasEvents"
        triples = explode_nested_cell(
            uf.individual("athlete", "1"), val, terms, uf, ONTO, parent_prop=col_prop,
        )
        preds = {p for _, p, _ in triples}
        assert URIRef(col_prop) in preds            # the column's own property is the edge
        assert URIRef(ONTO + "hasEvent") not in preds  # the derived name is not used


CAT = ONTO + "Category"
CAT_FOO = ONTO + "CategoryFoo"
CAT_BAR = ONTO + "CategoryBar"
OTHER = ONTO + "OtherFoo"


class TestReconcileNestedKeys:
    """A child-key whose values resolve to an existing value-mapped vocabulary is
    promoted from a literal to an object link — generically, per field name."""

    def _terms(self):
        # column 'items', keys ['category','note'] → child 'Item',
        # key_props {category: itemCategory, note: itemNote}
        return nested_term_names("items", ["category", "note"])

    def _index(self):
        return {"category": {"Foo": CAT_FOO, "Bar": CAT_BAR}}

    def _ind_class(self):
        return {CAT_FOO: CAT, CAT_BAR: CAT}

    def test_promotes_resolving_key_reusing_has_class_prop(self):
        out = reconcile_nested_keys(
            self._terms(),
            {"category": ["Foo", "Bar", "Foo"], "note": ["x", "y"]},
            self._index(), self._ind_class(), {CAT: "hasCategory"},
        )
        assert out["key_links"]["category"] == {
            "property": "hasCategory", "class_uri": CAT, "reused": True,
        }
        assert "category" not in out["key_props"]      # no longer a literal
        assert out["key_props"]["note"] == "itemNote"  # unresolved key stays literal

    def test_repurposes_derived_prop_when_no_generic_prop(self):
        out = reconcile_nested_keys(
            self._terms(), {"category": ["Foo", "Bar"]},
            self._index(), self._ind_class(), {},  # no has<Class> to reuse
        )
        assert out["key_links"]["category"]["property"] == "itemCategory"
        assert out["key_links"]["category"]["reused"] is False

    def test_below_threshold_stays_literal(self):
        out = reconcile_nested_keys(
            self._terms(), {"category": ["Foo", "Unmapped", "Nope", "Nope"]},
            self._index(), self._ind_class(), {CAT: "hasCategory"},
        )
        assert "category" not in out.get("key_links", {})
        assert out["key_props"]["category"] == "itemCategory"

    def test_ambiguous_target_class_stays_literal(self):
        out = reconcile_nested_keys(
            self._terms(), {"category": ["Foo", "Other"]},
            {"category": {"Foo": CAT_FOO, "Other": OTHER}},
            {CAT_FOO: CAT, OTHER: ONTO + "OtherClass"}, {CAT: "hasCategory"},
        )
        assert "category" not in out.get("key_links", {})  # two classes → no promotion

    def test_homonym_scoped_by_field_name(self):
        # The vocab is indexed under a DIFFERENT field name, so a same-valued key on
        # another field must NOT cross-link.
        out = reconcile_nested_keys(
            self._terms(), {"category": ["Foo", "Bar"]},
            {"gender": {"Foo": CAT_FOO, "Bar": CAT_BAR}},  # not under 'category'
            self._ind_class(), {CAT: "hasCategory"},
        )
        assert "category" not in out.get("key_links", {})


class TestExplodeReconciledKey:
    def _uf(self):
        return UriFactory("http://mesh.local/data/", "olympics2026")

    def test_emits_object_link_not_literal(self):
        uf = self._uf()
        terms = reconcile_nested_keys(
            nested_term_names("items", ["category", "note"]),
            {"category": ["Foo"], "note": ["hi"]},
            {"category": {"Foo": CAT_FOO}}, {CAT_FOO: CAT}, {CAT: "hasCategory"},
        )
        triples = explode_nested_cell(
            uf.individual("athlete", "1"),
            "[{'category': 'Foo', 'note': 'hi'}]",
            terms, uf, ONTO,
            reconcile_by_key={"category": {"Foo": CAT_FOO}},
        )
        child = next(o for s, p, o in triples if p == URIRef(ONTO + "hasItem"))
        # category → object link to the EXISTING individual, not a literal
        assert (child, URIRef(ONTO + "hasCategory"), URIRef(CAT_FOO)) in triples
        assert not any(p == URIRef(ONTO + "itemCategory") for _, p, _ in triples)
        # an unreconciled key is still a literal
        assert (child, URIRef(ONTO + "itemNote"), Literal("hi")) in triples

    def test_unresolved_value_under_object_key_emits_nothing(self):
        uf = self._uf()
        terms = reconcile_nested_keys(
            nested_term_names("items", ["category"]),
            {"category": ["Foo"]},
            {"category": {"Foo": CAT_FOO}}, {CAT_FOO: CAT}, {CAT: "hasCategory"},
        )
        triples = explode_nested_cell(
            uf.individual("athlete", "1"),
            "[{'category': 'Unmapped'}]",  # not in the index
            terms, uf, ONTO, reconcile_by_key={"category": {"Foo": CAT_FOO}},
        )
        # no hasCategory edge and no literal — the child is simply incomplete on category
        assert not any(p == URIRef(ONTO + "hasCategory") for _, p, _ in triples)
        assert not any(p == URIRef(ONTO + "itemCategory") for _, p, _ in triples)


class TestNestedOntologyTriples:
    def test_declares_class_and_properties(self):
        terms = nested_term_names("events", ["discipline", "event"])
        triples = nested_ontology_triples(ATHLETE, terms, ONTO)
        Event = URIRef(ONTO + "Event")
        hasEvent = URIRef(ONTO + "hasEvent")
        assert (Event, RDF.type, OWL.Class) in triples
        assert (hasEvent, RDF.type, OWL.ObjectProperty) in triples
        assert (hasEvent, RDFS.domain, URIRef(ATHLETE)) in triples
        assert (hasEvent, RDFS.range, Event) in triples
        assert (URIRef(ONTO + "eventName"), RDF.type, OWL.DatatypeProperty) in triples

    def test_repurposes_explicit_prop_and_omits_domain_when_none(self):
        # Shared across several subject classes → domain omitted; the supplied column
        # property is declared as the ObjectProperty (not the derived has<Child> name).
        terms = nested_term_names("events", ["discipline", "event"])
        pp = ONTO + "hasEvents"
        triples = nested_ontology_triples(None, terms, ONTO, parent_prop=pp)
        Event = URIRef(ONTO + "Event")
        assert (URIRef(pp), RDF.type, OWL.ObjectProperty) in triples
        assert (URIRef(pp), RDFS.range, Event) in triples
        # the SHARED parent property gets no rdfs:domain (key props still have domain=Event)
        assert not any(s == URIRef(pp) and p == RDFS.domain for s, p, _ in triples)

    def test_reused_link_prop_not_redeclared_repurposed_one_is(self):
        terms = nested_term_names("items", ["category", "extra"])
        # category reuses an existing hasCategory; extra repurposes its derived prop
        terms["key_links"] = {
            "category": {"property": "hasCategory", "class_uri": CAT, "reused": True},
            "extra": {"property": "itemExtra", "class_uri": ONTO + "Extra", "reused": False},
        }
        terms["key_props"] = {}  # both promoted off literals
        triples = nested_ontology_triples(None, terms, ONTO, parent_prop=ONTO + "hasItems")
        # reused property is NOT re-declared (its existing definition stands)
        assert not any(s == URIRef(ONTO + "hasCategory") for s, _, _ in triples)
        # repurposed property IS declared as an ObjectProperty onto its vocab class
        Item = URIRef(ONTO + "Item")
        assert (URIRef(ONTO + "itemExtra"), RDF.type, OWL.ObjectProperty) in triples
        assert (URIRef(ONTO + "itemExtra"), RDFS.domain, Item) in triples
        assert (URIRef(ONTO + "itemExtra"), RDFS.range, URIRef(ONTO + "Extra")) in triples
