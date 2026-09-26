# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for _split_multi_domain_dt_properties.

Scenarios:
1. Standard two-domain split → abstract has{Concept}, two class-scoped subprops
2. LLM capitalisation variant (hasInputeventsMvItemId vs expected hasInputeventsMvItemid)
   → original reused as scoped subprop, not duplicated
3. Stale subPropertyOf pointer to old multi-domain prop → replaced with abstract pointer
4. Single-domain property → untouched
5. Orphaned original (local name matches no domain class) → wired under abstract
6. word_splits expansion (e.g. charttime → chart_time → ChartTime)
7. Abstract parent already exists in graph → not duplicated
8. Three-domain split → three class-scoped subprops, all wired to abstract
"""
from __future__ import annotations

import pytest
import rdflib
from rdflib import OWL, RDF, RDFS, Literal, URIRef
from rdflib.namespace import SKOS, XSD

from semantic_mapper.ontology.iterative.generator import _split_multi_domain_dt_properties

BASE = "http://example.org/test#"


def uri(local: str) -> URIRef:
    return URIRef(f"{BASE}{local}")


def _make_graph() -> rdflib.Graph:
    return rdflib.Graph()


def _add_dt_prop(g: rdflib.Graph, local: str, domains: list[str], range_=XSD.string, alt: str | None = None) -> None:
    p = uri(local)
    g.add((p, RDF.type, OWL.DatatypeProperty))
    for d in domains:
        g.add((p, RDFS.domain, uri(d)))
    g.add((p, RDFS.range, range_))
    if alt:
        g.add((p, SKOS.altLabel, Literal(alt, lang="en")))


def _add_class(g: rdflib.Graph, local: str) -> None:
    g.add((uri(local), RDF.type, OWL.Class))


# ---------------------------------------------------------------------------
# 1. Standard two-domain split
# ---------------------------------------------------------------------------

class TestStandardTwoDomainSplit:
    def test_abstract_parent_created_with_no_table_prefix(self):
        """Abstract parent must be hasValue, NOT hasTableAValue."""
        g = _make_graph()
        _add_class(g, "TableA")
        _add_class(g, "TableB")
        # Property that dedup elected from TableA but carries both domains
        _add_dt_prop(g, "hasTableAValue", ["TableA", "TableB"], XSD.string, alt="value")

        _split_multi_domain_dt_properties(g, BASE)

        # Abstract parent named after concept only
        assert (uri("hasValue"), RDF.type, OWL.DatatypeProperty) in g
        # Abstract has no domain
        assert list(g.objects(uri("hasValue"), RDFS.domain)) == []

    def test_class_scoped_subprops_created(self):
        g = _make_graph()
        _add_class(g, "TableA")
        _add_class(g, "TableB")
        _add_dt_prop(g, "hasTableAValue", ["TableA", "TableB"], XSD.string, alt="value")

        _split_multi_domain_dt_properties(g, BASE)

        # Both class-scoped props exist
        assert (uri("hasTableAValue"), RDF.type, OWL.DatatypeProperty) in g
        assert (uri("hasTableBValue"), RDF.type, OWL.DatatypeProperty) in g

    def test_class_scoped_subprops_have_correct_domain(self):
        g = _make_graph()
        _add_class(g, "TableA")
        _add_class(g, "TableB")
        _add_dt_prop(g, "hasTableAValue", ["TableA", "TableB"], XSD.string, alt="value")

        _split_multi_domain_dt_properties(g, BASE)

        assert uri("TableA") in list(g.objects(uri("hasTableAValue"), RDFS.domain))
        assert uri("TableB") in list(g.objects(uri("hasTableBValue"), RDFS.domain))

    def test_class_scoped_subprops_point_to_abstract(self):
        g = _make_graph()
        _add_class(g, "TableA")
        _add_class(g, "TableB")
        _add_dt_prop(g, "hasTableAValue", ["TableA", "TableB"], XSD.string, alt="value")

        _split_multi_domain_dt_properties(g, BASE)

        assert (uri("hasTableAValue"), RDFS.subPropertyOf, uri("hasValue")) in g
        assert (uri("hasTableBValue"), RDFS.subPropertyOf, uri("hasValue")) in g

    def test_range_propagated_to_scoped_props(self):
        g = _make_graph()
        _add_class(g, "TableA")
        _add_class(g, "TableB")
        _add_dt_prop(g, "hasTableAValue", ["TableA", "TableB"], XSD.integer, alt="value")

        _split_multi_domain_dt_properties(g, BASE)

        assert (uri("hasTableBValue"), RDFS.range, XSD.integer) in g

    def test_returns_count(self):
        g = _make_graph()
        _add_class(g, "TableA")
        _add_class(g, "TableB")
        _add_dt_prop(g, "hasTableAValue", ["TableA", "TableB"], XSD.string, alt="value")

        result = _split_multi_domain_dt_properties(g, BASE)

        assert result == 1


# ---------------------------------------------------------------------------
# 2. LLM capitalisation variant — original reused, no duplicate created
# ---------------------------------------------------------------------------

class TestCapitalisationVariant:
    """
    LLM generates hasInputeventsMvItemId (capital I in Id).
    _to_concept("itemid") = "Itemid" (lowercase d).
    Computed scoped local for domain InputeventsMv = "hasInputeventsMvItemid".
    Original local = "hasInputeventsMvItemId".
    These differ in case only → original must be reclaimed, not duplicated.
    """

    def test_original_reclaimed_not_duplicated(self):
        g = _make_graph()
        _add_class(g, "InputeventsMv")
        _add_class(g, "Chartevent")
        # LLM-style capitalisation: capital I in Id
        _add_dt_prop(g, "hasInputeventsMvItemId", ["InputeventsMv", "Chartevent"], XSD.integer, alt="itemid")

        _split_multi_domain_dt_properties(g, BASE)

        # hasInputeventsMvItemid (lowercase d) must NOT exist as a separate prop
        assert (uri("hasInputeventsMvItemid"), RDF.type, OWL.DatatypeProperty) not in g

    def test_original_gets_domain_and_subpropertyof(self):
        g = _make_graph()
        _add_class(g, "InputeventsMv")
        _add_class(g, "Chartevent")
        _add_dt_prop(g, "hasInputeventsMvItemId", ["InputeventsMv", "Chartevent"], XSD.integer, alt="itemid")

        _split_multi_domain_dt_properties(g, BASE)

        # Original gets its own domain back
        assert uri("InputeventsMv") in list(g.objects(uri("hasInputeventsMvItemId"), RDFS.domain))
        # And is wired under abstract
        assert (uri("hasInputeventsMvItemId"), RDFS.subPropertyOf, uri("hasItemid")) in g

    def test_other_domain_gets_new_scoped_prop(self):
        g = _make_graph()
        _add_class(g, "InputeventsMv")
        _add_class(g, "Chartevent")
        _add_dt_prop(g, "hasInputeventsMvItemId", ["InputeventsMv", "Chartevent"], XSD.integer, alt="itemid")

        _split_multi_domain_dt_properties(g, BASE)

        # Chartevent gets a new scoped prop
        assert (uri("hasCharteventItemid"), RDF.type, OWL.DatatypeProperty) in g
        assert uri("Chartevent") in list(g.objects(uri("hasCharteventItemid"), RDFS.domain))
        assert (uri("hasCharteventItemid"), RDFS.subPropertyOf, uri("hasItemid")) in g

    def test_no_self_referential_subproperty(self):
        g = _make_graph()
        _add_class(g, "InputeventsMv")
        _add_class(g, "Chartevent")
        _add_dt_prop(g, "hasInputeventsMvItemId", ["InputeventsMv", "Chartevent"], XSD.integer, alt="itemid")

        _split_multi_domain_dt_properties(g, BASE)

        # Original must NOT point to itself
        assert (uri("hasInputeventsMvItemId"), RDFS.subPropertyOf, uri("hasInputeventsMvItemId")) not in g


# ---------------------------------------------------------------------------
# 3. Stale subPropertyOf pointer to old multi-domain prop → replaced
# ---------------------------------------------------------------------------

class TestStaleSubPropertyPointerReplaced:
    """
    Simulates state after the first (buggy) generator run:
    hasCharteventCgid subPropertyOf hasInputeventsMvCgid (wrong — table-scoped parent)
    After split, hasCharteventCgid must point to hasCgid (the correct abstract).
    """

    def test_stale_pointer_removed(self):
        g = _make_graph()
        _add_class(g, "InputeventsMv")
        _add_class(g, "Chartevent")
        _add_class(g, "Outputevent")

        # Buggy state: hasInputeventsMvCgid has multiple domains (the multi-domain winner)
        _add_dt_prop(g, "hasInputeventsMvCgid", ["InputeventsMv", "Chartevent", "Outputevent"], XSD.integer, alt="cgid")

        # hasCharteventCgid already exists but points to old winner (stale pointer)
        g.add((uri("hasCharteventCgid"), RDF.type, OWL.DatatypeProperty))
        g.add((uri("hasCharteventCgid"), RDFS.domain, uri("Chartevent")))
        g.add((uri("hasCharteventCgid"), RDFS.subPropertyOf, uri("hasInputeventsMvCgid")))

        _split_multi_domain_dt_properties(g, BASE)

        # Stale pointer must be gone
        assert (uri("hasCharteventCgid"), RDFS.subPropertyOf, uri("hasInputeventsMvCgid")) not in g

    def test_stale_prop_rewired_to_abstract(self):
        g = _make_graph()
        _add_class(g, "InputeventsMv")
        _add_class(g, "Chartevent")
        _add_class(g, "Outputevent")
        _add_dt_prop(g, "hasInputeventsMvCgid", ["InputeventsMv", "Chartevent", "Outputevent"], XSD.integer, alt="cgid")
        g.add((uri("hasCharteventCgid"), RDF.type, OWL.DatatypeProperty))
        g.add((uri("hasCharteventCgid"), RDFS.domain, uri("Chartevent")))
        g.add((uri("hasCharteventCgid"), RDFS.subPropertyOf, uri("hasInputeventsMvCgid")))

        _split_multi_domain_dt_properties(g, BASE)

        assert (uri("hasCharteventCgid"), RDFS.subPropertyOf, uri("hasCgid")) in g

    def test_original_also_wired_under_abstract(self):
        g = _make_graph()
        _add_class(g, "InputeventsMv")
        _add_class(g, "Chartevent")
        _add_class(g, "Outputevent")
        _add_dt_prop(g, "hasInputeventsMvCgid", ["InputeventsMv", "Chartevent", "Outputevent"], XSD.integer, alt="cgid")
        g.add((uri("hasCharteventCgid"), RDF.type, OWL.DatatypeProperty))
        g.add((uri("hasCharteventCgid"), RDFS.domain, uri("Chartevent")))
        g.add((uri("hasCharteventCgid"), RDFS.subPropertyOf, uri("hasInputeventsMvCgid")))

        _split_multi_domain_dt_properties(g, BASE)

        # Original gets domain=InputeventsMv back and wired under abstract
        assert uri("InputeventsMv") in list(g.objects(uri("hasInputeventsMvCgid"), RDFS.domain))
        assert (uri("hasInputeventsMvCgid"), RDFS.subPropertyOf, uri("hasCgid")) in g


# ---------------------------------------------------------------------------
# 4. Single-domain property → untouched
# ---------------------------------------------------------------------------

class TestSingleDomainUntouched:
    def test_single_domain_property_not_split(self):
        g = _make_graph()
        _add_class(g, "Patient")
        _add_dt_prop(g, "hasSubjectId", ["Patient"], XSD.integer, alt="subject_id")

        result = _split_multi_domain_dt_properties(g, BASE)

        assert result == 0
        # Domain still attached
        assert uri("Patient") in list(g.objects(uri("hasSubjectId"), RDFS.domain))
        # No abstract created
        assert (uri("hasSubjectId_abstract"), RDF.type, OWL.DatatypeProperty) not in g

    def test_no_domain_property_not_split(self):
        g = _make_graph()
        g.add((uri("hasAbstract"), RDF.type, OWL.DatatypeProperty))
        g.add((uri("hasAbstract"), RDFS.range, XSD.string))

        result = _split_multi_domain_dt_properties(g, BASE)

        assert result == 0


# ---------------------------------------------------------------------------
# 5. Orphaned original (name matches no domain class) → wired under abstract
# ---------------------------------------------------------------------------

class TestOrphanedOriginalWiredUnderAbstract:
    """
    After dedup, the winner is hasWinnerValue (domain=Winner + Loser).
    Loser class → hasLoserValue (new scoped prop, domain=Loser).
    Winner class → hasWinnerValue (re-attached as scoped prop, domain=Winner).
    No orphan in this standard case — but if we give it a weird name that
    doesn't match any domain class, it stays domain-less and must be wired under abstract.
    """

    def test_oddly_named_winner_wired_under_abstract(self):
        g = _make_graph()
        _add_class(g, "Alpha")
        _add_class(g, "Beta")
        # Name does not start with has{DomainClass}... for either Alpha or Beta
        _add_dt_prop(g, "hasGenericRating", ["Alpha", "Beta"], XSD.integer, alt="rating")

        _split_multi_domain_dt_properties(g, BASE)

        # abstract = hasRating
        assert (uri("hasRating"), RDF.type, OWL.DatatypeProperty) in g
        # hasAlphaRating and hasBetaRating created
        assert (uri("hasAlphaRating"), RDF.type, OWL.DatatypeProperty) in g
        assert (uri("hasBetaRating"), RDF.type, OWL.DatatypeProperty) in g
        # Original orphaned (no domain) → wired under abstract
        assert (uri("hasGenericRating"), RDFS.subPropertyOf, uri("hasRating")) in g
        assert list(g.objects(uri("hasGenericRating"), RDFS.domain)) == []


# ---------------------------------------------------------------------------
# 6. word_splits expansion
# ---------------------------------------------------------------------------

class TestWordSplitsExpansion:
    def test_charttime_expanded_to_chart_time(self):
        """charttime → chart_time (via word_splits) → ChartTime concept."""
        g = _make_graph()
        _add_class(g, "Chartevent")
        _add_class(g, "Labevent")
        _add_dt_prop(g, "hasCharteventCharttime", ["Chartevent", "Labevent"], XSD.string, alt="charttime")

        word_splits = {"charttime": "chart_time"}
        _split_multi_domain_dt_properties(g, BASE, word_splits=word_splits)

        # Abstract should be hasChartTime (CamelCase from chart_time)
        assert (uri("hasChartTime"), RDF.type, OWL.DatatypeProperty) in g

    def test_scoped_props_use_expanded_concept(self):
        g = _make_graph()
        _add_class(g, "Chartevent")
        _add_class(g, "Labevent")
        _add_dt_prop(g, "hasCharteventCharttime", ["Chartevent", "Labevent"], XSD.string, alt="charttime")

        word_splits = {"charttime": "chart_time"}
        _split_multi_domain_dt_properties(g, BASE, word_splits=word_splits)

        # Labevent scoped prop: hasLabeventChartTime
        assert (uri("hasLabeventChartTime"), RDF.type, OWL.DatatypeProperty) in g
        assert (uri("hasLabeventChartTime"), RDFS.subPropertyOf, uri("hasChartTime")) in g


# ---------------------------------------------------------------------------
# 7. Abstract parent already exists → not duplicated
# ---------------------------------------------------------------------------

class TestAbstractAlreadyExists:
    def test_existing_abstract_not_overwritten(self):
        g = _make_graph()
        _add_class(g, "TableA")
        _add_class(g, "TableB")
        _add_dt_prop(g, "hasTableAScore", ["TableA", "TableB"], XSD.integer, alt="score")

        # Pre-existing abstract
        g.add((uri("hasScore"), RDF.type, OWL.DatatypeProperty))
        g.add((uri("hasScore"), RDFS.range, XSD.integer))
        g.add((uri("hasScore"), SKOS.altLabel, Literal("score_original", lang="en")))

        _split_multi_domain_dt_properties(g, BASE)

        # altLabel of pre-existing abstract not overwritten
        alts = list(g.objects(uri("hasScore"), SKOS.altLabel))
        assert Literal("score_original", lang="en") in alts

    def test_scoped_props_still_wired_to_existing_abstract(self):
        g = _make_graph()
        _add_class(g, "TableA")
        _add_class(g, "TableB")
        _add_dt_prop(g, "hasTableAScore", ["TableA", "TableB"], XSD.integer, alt="score")
        g.add((uri("hasScore"), RDF.type, OWL.DatatypeProperty))
        g.add((uri("hasScore"), RDFS.range, XSD.integer))

        _split_multi_domain_dt_properties(g, BASE)

        assert (uri("hasTableBScore"), RDFS.subPropertyOf, uri("hasScore")) in g


# ---------------------------------------------------------------------------
# 8. Three-domain split
# ---------------------------------------------------------------------------

class TestThreeDomainSplit:
    def test_all_three_scoped_props_created(self):
        g = _make_graph()
        _add_class(g, "Chartevent")
        _add_class(g, "Labevent")
        _add_class(g, "Outputevent")
        _add_dt_prop(
            g, "hasCharteventValuenum",
            ["Chartevent", "Labevent", "Outputevent"],
            XSD.double, alt="valuenum"
        )

        _split_multi_domain_dt_properties(g, BASE)

        abstract = uri("hasValuenum")
        assert (abstract, RDF.type, OWL.DatatypeProperty) in g

        for cls in ("Chartevent", "Labevent", "Outputevent"):
            scoped = uri(f"has{cls}Valuenum")
            assert (scoped, RDF.type, OWL.DatatypeProperty) in g, f"missing {scoped}"
            assert (scoped, RDFS.subPropertyOf, abstract) in g
            assert uri(cls) in list(g.objects(scoped, RDFS.domain))

    def test_original_reclaimed_for_its_domain(self):
        g = _make_graph()
        _add_class(g, "Chartevent")
        _add_class(g, "Labevent")
        _add_class(g, "Outputevent")
        _add_dt_prop(
            g, "hasCharteventValuenum",
            ["Chartevent", "Labevent", "Outputevent"],
            XSD.double, alt="valuenum"
        )

        _split_multi_domain_dt_properties(g, BASE)

        # Original (hasCharteventValuenum) reclaimed as scoped prop for Chartevent
        assert uri("Chartevent") in list(g.objects(uri("hasCharteventValuenum"), RDFS.domain))
        assert (uri("hasCharteventValuenum"), RDFS.subPropertyOf, uri("hasValuenum")) in g

    def test_returns_count_of_one(self):
        g = _make_graph()
        _add_class(g, "Chartevent")
        _add_class(g, "Labevent")
        _add_class(g, "Outputevent")
        _add_dt_prop(
            g, "hasCharteventValuenum",
            ["Chartevent", "Labevent", "Outputevent"],
            XSD.double, alt="valuenum"
        )

        result = _split_multi_domain_dt_properties(g, BASE)
        assert result == 1

    def test_multiple_multi_domain_props_all_split(self):
        g = _make_graph()
        _add_class(g, "Chartevent")
        _add_class(g, "Labevent")
        _add_class(g, "Outputevent")
        _add_dt_prop(g, "hasCharteventValue", ["Chartevent", "Labevent"], XSD.string, alt="value")
        _add_dt_prop(g, "hasCharteventValuenum", ["Chartevent", "Labevent", "Outputevent"], XSD.double, alt="valuenum")

        result = _split_multi_domain_dt_properties(g, BASE)

        assert result == 2
        assert (uri("hasValue"), RDF.type, OWL.DatatypeProperty) in g
        assert (uri("hasValuenum"), RDF.type, OWL.DatatypeProperty) in g


# ---------------------------------------------------------------------------
# 9. Provenance restoration for the per-class children created by the split
# ---------------------------------------------------------------------------

from semantic_mapper.db.models import ColumnProfile, TableProfile
from semantic_mapper.ontology.iterative.context_builder import derive_class_uri

_FM_SOURCE_COLUMN = URIRef("http://forgemesh.ai/ontology#sourceColumn")
_FM_SQL_TYPE = URIRef("http://forgemesh.ai/ontology#sqlType")
_FM_NULL_RATE = URIRef("http://forgemesh.ai/ontology#nullRate")


def _col(name: str, *, inferred_type: str = "VARCHAR", null_rate: float = 0.0,
         distinct: int = 1, pk: bool = False) -> ColumnProfile:
    return ColumnProfile(
        name=name, inferred_type=inferred_type, is_nullable=True,
        is_primary_key=pk, null_rate=null_rate, distinct_count=distinct,
    )


def _profile(table: str, cols: list[ColumnProfile], rows: int = 1000) -> TableProfile:
    return TableProfile(
        table_name=table, row_count=rows, columns={c.name: c for c in cols},
    )


class TestProvenanceRestoredForSplitChildren:
    """A coded column that appears under the same name in several tables is merged
    by ``_deduplicate_properties`` into one multi-domain winner; the losing
    tables' ``fm:sourceColumn`` is discarded in that merge. The split must then
    re-attach the correct ``fm:sourceColumn`` to *every* class-scoped child — not
    only the reclaimed winner — so a high-cardinality coded column always yields a
    DatatypeProperty carrying its source-column provenance.

    Generic names only — the contract holds for any schema where a column name is
    shared across tables.
    """

    # Three tables that each carry a high-cardinality `code` column under the same
    # name. Distinct counts are high so the column is a scalar code, not a vocab.
    TABLES = ["raw_events", "raw_records", "raw_items"]

    def _setup(self):
        profiles = [
            _profile(t, [_col("id", inferred_type="INTEGER", pk=True),
                         _col("code", inferred_type="TEXT", null_rate=0.0, distinct=250)])
            for t in self.TABLES
        ]
        classes = [derive_class_uri(t, BASE) for t in self.TABLES]
        g = _make_graph()
        for c in classes:
            g.add((URIRef(c), RDF.type, OWL.Class))
        # Dedup winner: a single DatatypeProperty carrying ALL domains but
        # provenance for only the FIRST table (the others were discarded).
        winner_local = "has" + classes[0].split("#")[-1] + "Code"
        winner = uri(winner_local)
        g.add((winner, RDF.type, OWL.DatatypeProperty))
        for c in classes:
            g.add((winner, RDFS.domain, URIRef(c)))
        g.add((winner, RDFS.range, XSD.string))
        g.add((winner, SKOS.altLabel, Literal("code", lang="en")))
        g.add((winner, _FM_SOURCE_COLUMN, Literal(f"{self.TABLES[0]}.code")))
        return g, profiles, classes

    def test_every_class_scoped_child_has_source_column(self):
        g, profiles, classes = self._setup()

        _split_multi_domain_dt_properties(g, BASE, profiles=profiles)

        for table, cls in zip(self.TABLES, classes):
            child = uri("has" + cls.split("#")[-1] + "Code")
            srcs = sorted(str(s) for s in g.objects(child, _FM_SOURCE_COLUMN))
            assert srcs == [f"{table}.code"], (
                f"{child} should carry fm:sourceColumn '{table}.code', got {srcs}"
            )

    def test_child_provenance_carries_sql_type_and_null_rate(self):
        g, profiles, classes = self._setup()

        _split_multi_domain_dt_properties(g, BASE, profiles=profiles)

        # A freshly-created child (second table — not the reclaimed winner).
        child = uri("has" + classes[1].split("#")[-1] + "Code")
        assert (child, _FM_SQL_TYPE, Literal("TEXT")) in g
        assert (child, _FM_NULL_RATE, Literal("0.00")) in g

    def test_abstract_parent_stays_provenance_free(self):
        g, profiles, _ = self._setup()

        _split_multi_domain_dt_properties(g, BASE, profiles=profiles)

        # The abstract parent spans many source columns — it must carry none.
        assert list(g.objects(uri("hasCode"), _FM_SOURCE_COLUMN)) == []

    def test_reclaimed_winner_provenance_not_clobbered(self):
        g, profiles, classes = self._setup()

        _split_multi_domain_dt_properties(g, BASE, profiles=profiles)

        # The winner is reclaimed as the first table's child and keeps its own src.
        winner = uri("has" + classes[0].split("#")[-1] + "Code")
        srcs = sorted(str(s) for s in g.objects(winner, _FM_SOURCE_COLUMN))
        assert srcs == [f"{self.TABLES[0]}.code"]

    def test_without_profiles_no_provenance_added(self):
        # Backward-compatible: profiles omitted → children created without src,
        # exactly as before (no crash, no fabricated provenance).
        g, _, classes = self._setup()

        _split_multi_domain_dt_properties(g, BASE)

        child = uri("has" + classes[1].split("#")[-1] + "Code")
        assert (child, RDF.type, OWL.DatatypeProperty) in g
        assert list(g.objects(child, _FM_SOURCE_COLUMN)) == []
