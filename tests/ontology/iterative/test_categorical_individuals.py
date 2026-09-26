# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for materialise_categorical_individuals — deterministic gen-time minting of
value-mapped vocabulary individuals.

The pass re-derives every categorical vocabulary from the SOURCE PROFILE: one class per
source-column concept (all `gender` columns → one Gender), exactly one individual per
distinct non-null value, named canonically `{Value}{Class}`.  It supersedes the
label-overlap consolidation pass and kills the LLM's per-table fragmentation
(Gender/TeamGender/MedallistGender), inconsistent member names (MGender vs GenderMale),
hallucinated members (NonBinaryGender) and placeholder passthrough ("-").

Generic — no dataset names in the logic under test; keyed on column names + profiled
values.
"""
from __future__ import annotations

import pytest
from rdflib import Graph, Literal, OWL, RDF, RDFS, URIRef
from rdflib.collection import Collection

from semantic_mapper.db.models import ColumnProfile, TableProfile
from semantic_mapper.ontology.iterative.context_builder import (
    derive_class_uri,
    set_class_name_overrides,
)
from semantic_mapper.ontology.iterative.categorical_individuals import (
    _dimension_stem,
    _pascal,
    _value_slug,
    _values_corroborate,
    materialise_categorical_individuals,
)
from semantic_mapper.ontology.iterative.generator import _build_oneof_enumerations

BASE = "http://mesh.local/ontology/test#"
FM = "http://forgemesh.ai/ontology#"
_FK = URIRef(FM + "sourceForeignKey")


def L(u) -> str:
    return str(u).split("#")[-1].split("/")[-1]


@pytest.fixture(autouse=True)
def _reset_overrides():
    """The class-name override map is a module global set per generation run; reset it so
    one test's overrides never leak into another's entity-class detection."""
    set_class_name_overrides({})
    yield
    set_class_name_overrides({})


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _col(name, top=None, pk=False):
    tv = [{"value": v, "count": c} for v, c in (top or [])]
    return ColumnProfile(
        name=name, inferred_type="VARCHAR(8)", is_nullable=True, is_primary_key=pk,
        null_rate=0.0, distinct_count=len(tv), top_values=tv,
    )


def _entity_class(g, table):
    uri = URIRef(derive_class_uri(table, BASE))
    g.add((uri, RDF.type, OWL.Class))
    return uri


def _vocab_class(g, local, individuals):
    """individuals: list of (local, label) minted as members of the vocab class."""
    cls = URIRef(BASE + local)
    g.add((cls, RDF.type, OWL.Class))
    for ind_local, label in individuals:
        ind = URIRef(BASE + ind_local)
        g.add((ind, RDF.type, OWL.NamedIndividual))
        g.add((ind, RDF.type, cls))
        g.add((ind, RDFS.label, Literal(label)))
    return cls


def _obj_prop(g, local, domain_table, range_cls, table, col):
    p = URIRef(BASE + local)
    g.add((p, RDF.type, OWL.ObjectProperty))
    g.add((p, RDFS.domain, URIRef(derive_class_uri(domain_table, BASE))))
    g.add((p, RDFS.range, range_cls))
    g.add((p, _FK, Literal(f"{table}.{col} -> {L(range_cls)}")))
    return p


def _gender_fragmentation():
    """Three tables each carrying a `gender` column, modelled by the LLM as three parallel
    classes with inconsistently-named members, a hallucinated member, and a "-" passthrough.
    """
    g = Graph()
    _entity_class(g, "raw_athletes")
    _entity_class(g, "raw_teams")
    _entity_class(g, "raw_medallists")

    gender = _vocab_class(g, "Gender", [
        ("MGender", "M"), ("GenderMale", "Gender Male"), ("FGender", "F"),
        ("NonBinaryGender", "Non Binary Gender"),  # hallucinated — no source value
    ])
    team_gender = _vocab_class(g, "TeamGender", [
        ("NoGenderTeamGender", "-"), ("MaleTeamGender", "M"), ("WTeamGender", "W"),
    ])
    med_gender = _vocab_class(g, "MedallistGender", [
        ("MedallistGenderMale", "M"),
    ])

    _obj_prop(g, "hasAthleteGender", "raw_athletes", gender, "raw_athletes", "gender")
    _obj_prop(g, "hasTeamGender", "raw_teams", team_gender, "raw_teams", "gender")
    _obj_prop(g, "hasMedallistGender", "raw_medallists", med_gender, "raw_medallists", "gender")

    profiles = [
        TableProfile(table_name="raw_athletes", row_count=100,
                     columns={"gender": _col("gender", [("M", 60), ("F", 40)])}),
        TableProfile(table_name="raw_teams", row_count=50,
                     columns={"gender": _col("gender", [("-", 20), ("M", 15), ("W", 10), ("X", 5)])}),
        TableProfile(table_name="raw_medallists", row_count=30,
                     columns={"gender": _col("gender", [("M", 18), ("F", 12)])}),
    ]
    return g, profiles


# ---------------------------------------------------------------------------
# Naming helpers
# ---------------------------------------------------------------------------

class TestNaming:
    def test_pascal_concept(self):
        assert _pascal("gender") == "Gender"
        assert _pascal("discipline_code") == "DisciplineCode"
        assert _pascal("event_type") == "EventType"
        assert _pascal("eventType") == "EventType"  # internal capital preserved

    def test_value_slug(self):
        assert _value_slug("M") == "M"
        assert _value_slug("Alpine Skiing") == "AlpineSkiing"
        assert _value_slug("Head Coach") == "HeadCoach"
        assert _value_slug("CAN") == "CAN"
        assert _value_slug("Cross-Country Skiing") == "CrossCountrySkiing"

    def test_value_slug_digit_guard(self):
        assert _value_slug("2026")[0].isalpha()  # never starts with a digit


# ---------------------------------------------------------------------------
# Core behaviour
# ---------------------------------------------------------------------------

class TestMaterialise:
    def test_one_class_per_concept(self):
        g, profiles = _gender_fragmentation()
        materialise_categorical_individuals(g, profiles, BASE)
        vocab_classes = {
            L(c) for c in g.subjects(RDF.type, OWL.Class)
            if any(True for _ in g.subjects(RDF.type, c))  # has members
        }
        assert "Gender" in vocab_classes
        assert "TeamGender" not in vocab_classes
        assert "MedallistGender" not in vocab_classes
        # the parallel classes are removed from the graph entirely
        assert (URIRef(BASE + "TeamGender"), RDF.type, OWL.Class) not in g
        assert (URIRef(BASE + "MedallistGender"), RDF.type, OWL.Class) not in g

    def test_one_individual_per_distinct_value(self):
        g, profiles = _gender_fragmentation()
        materialise_categorical_individuals(g, profiles, BASE)
        gender = URIRef(BASE + "Gender")
        members = sorted(L(i) for i in g.subjects(RDF.type, gender))
        # union of distinct non-null values across the three columns: M, F, W, X
        assert members == ["FGender", "MGender", "WGender", "XGender"]

    def test_no_duplicate_or_hallucinated_members(self):
        g, profiles = _gender_fragmentation()
        materialise_categorical_individuals(g, profiles, BASE)
        all_inds = {L(i) for i in g.subjects(RDF.type, OWL.NamedIndividual)}
        assert "GenderMale" not in all_inds        # duplicate of M
        assert "NonBinaryGender" not in all_inds    # hallucinated, no source value
        assert "MedallistGenderMale" not in all_inds
        assert "NoGenderTeamGender" not in all_inds  # "-" passthrough

    def test_no_semantic_merge_f_and_w_distinct(self):
        g, profiles = _gender_fragmentation()
        materialise_categorical_individuals(g, profiles, BASE)
        members = {L(i) for i in g.subjects(RDF.type, URIRef(BASE + "Gender"))}
        assert "FGender" in members and "WGender" in members  # F and W NOT merged

    def test_null_like_dropped(self):
        g, profiles = _gender_fragmentation()
        materialise_categorical_individuals(g, profiles, BASE)
        labels = {str(o) for o in g.objects(None, RDFS.label)}
        assert "-" not in labels  # the "-" placeholder yields no individual

    def test_label_is_raw_value(self):
        g, profiles = _gender_fragmentation()
        materialise_categorical_individuals(g, profiles, BASE)
        m = URIRef(BASE + "MGender")
        assert str(next(g.objects(m, RDFS.label))) == "M"

    def test_value_in_multiple_columns_single_individual(self):
        g, profiles = _gender_fragmentation()
        materialise_categorical_individuals(g, profiles, BASE)
        # "M" appears in all three columns → exactly one MGender individual
        assert len(list(g.subjects(RDF.type, OWL.NamedIndividual))) == 4
        assert sum(1 for i in g.subjects(RDF.type, OWL.NamedIndividual)
                   if L(i) == "MGender") == 1

    def test_object_property_ranges_repointed(self):
        g, profiles = _gender_fragmentation()
        materialise_categorical_individuals(g, profiles, BASE)
        gender = URIRef(BASE + "Gender")
        for p in ("hasAthleteGender", "hasTeamGender", "hasMedallistGender"):
            ranges = list(g.objects(URIRef(BASE + p), RDFS.range))
            assert ranges == [gender], f"{p} range={ranges}"

    def test_deterministic_across_fresh_runs(self):
        g1, p1 = _gender_fragmentation()
        g2, p2 = _gender_fragmentation()
        materialise_categorical_individuals(g1, p1, BASE)
        materialise_categorical_individuals(g2, p2, BASE)
        assert set(g1) == set(g2)

    def test_idempotent(self):
        g, profiles = _gender_fragmentation()
        n1 = materialise_categorical_individuals(g, profiles, BASE)
        snapshot = set(g)
        n2 = materialise_categorical_individuals(g, profiles, BASE)
        assert n1 == n2
        assert set(g) == snapshot

    def test_purges_stale_oneof(self):
        g, profiles = _gender_fragmentation()
        # Give the LLM Gender an owl:equivalentClass owl:oneOf over its noisy members.
        from rdflib import BNode
        members = [URIRef(BASE + "MGender"), URIRef(BASE + "GenderMale")]
        lst = BNode(); Collection(g, lst, members)
        restr = BNode(); g.add((restr, OWL.oneOf, lst))
        g.add((URIRef(BASE + "Gender"), OWL.equivalentClass, restr))

        materialise_categorical_individuals(g, profiles, BASE)
        # stale enumeration gone; GenderMale list node gone
        assert (URIRef(BASE + "GenderMale"), None, None) not in g
        # rebuild from clean individuals yields the correct enumeration
        _build_oneof_enumerations(g, excluded_class_uris=set(), base_iri=BASE)
        eqs = list(g.objects(URIRef(BASE + "Gender"), OWL.equivalentClass))
        assert len(eqs) == 1
        for oneof in g.objects(eqs[0], OWL.oneOf):
            got = sorted(L(m) for m in Collection(g, oneof))
            assert got == ["FGender", "MGender", "WGender", "XGender"]

    def test_skips_packed_values(self):
        """A JSON/list-shaped cell is never minted as a single categorical value."""
        g = Graph()
        _entity_class(g, "raw_venues")
        vocab = _vocab_class(g, "Disciplines", [("PackedDisciplines", '["A","B"]')])
        _obj_prop(g, "hasVenueDisciplines", "raw_venues", vocab, "raw_venues", "disciplines")
        profiles = [TableProfile(
            table_name="raw_venues", row_count=12,
            columns={"disciplines": _col("disciplines",
                     [('["A","B"]', 1), ("alpine", 2)])},
        )]
        materialise_categorical_individuals(g, profiles, BASE)
        members = sorted(L(i) for i in g.subjects(RDF.type, URIRef(BASE + "Disciplines")))
        assert members == ["AlpineDisciplines"]  # packed cell skipped, scalar kept


class TestCanonicalClassComment:
    """Every canonical vocabulary class carries an rdfs:comment — including one freshly
    minted from the column name, whose LLM-described parallels are dropped."""

    def _status_graph(self, parallel_comment):
        g = Graph()
        _entity_class(g, "raw_events")
        parallel = _vocab_class(g, "EventStatusKind", [("OpenEventStatusKind", "open")])
        if parallel_comment:
            g.add((parallel, RDFS.comment, Literal(parallel_comment, lang="en")))
        _obj_prop(g, "hasEventStatus", "raw_events", parallel, "raw_events", "status")
        profiles = [TableProfile(
            table_name="raw_events", row_count=10,
            columns={"status": _col("status", [("open", 6), ("closed", 4)])},
        )]
        materialise_categorical_individuals(g, profiles, BASE)
        return g, URIRef(BASE + "Status")

    def test_inherits_comment_from_superseded_parallel(self):
        g, canon = self._status_graph("The lifecycle status of an event.")
        assert (URIRef(BASE + "EventStatusKind"), RDF.type, OWL.Class) not in g
        assert [str(c) for c in g.objects(canon, RDFS.comment)] == [
            "The lifecycle status of an event."
        ]

    def test_synthesises_comment_when_no_parallel_described(self):
        g, canon = self._status_graph(None)
        comments = [str(c) for c in g.objects(canon, RDFS.comment)]
        assert len(comments) == 1
        assert "raw_events.status" in comments[0]

    def test_every_class_commented_after_fragmentation_fold(self):
        g, profiles = _gender_fragmentation()
        materialise_categorical_individuals(g, profiles, BASE)
        vocab = [c for c in g.subjects(RDF.type, OWL.Class) if any(g.subjects(RDF.type, c))]
        assert vocab and all(list(g.objects(c, RDFS.comment)) for c in vocab)


class TestEntityFkUntouched:
    def test_entity_class_range_not_treated_as_vocab(self):
        """A property whose range is a table-entity class (venue_code → Venue) is an entity
        FK, not a value-mapped vocabulary — the pass must leave it alone."""
        g = Graph()
        _entity_class(g, "raw_schedules")
        venue = _entity_class(g, "raw_venues")  # entity class, no members
        p = _obj_prop(g, "hasScheduleVenue", "raw_schedules", venue, "raw_schedules", "venue_code")
        profiles = [
            TableProfile(table_name="raw_schedules", row_count=100,
                         columns={"venue_code": _col("venue_code", [("CCU", 5), ("BIA", 3)])}),
            TableProfile(table_name="raw_venues", row_count=15,
                         columns={"venue_code": _col("venue_code", pk=True)}),
        ]
        n = materialise_categorical_individuals(g, profiles, BASE)
        assert n == 0
        assert list(g.objects(p, RDFS.range)) == [venue]  # unchanged
        assert not list(g.subjects(RDF.type, venue))       # no individuals minted
        assert (URIRef(BASE + "VenueCode"), RDF.type, OWL.Class) not in g

    def test_concept_colliding_with_entity_class_skipped(self):
        """If a concept's canonical class name would collide with an existing entity class,
        the LLM vocabulary is left untouched rather than fused into the entity."""
        g = Graph()
        # entity table whose class IS "Gender"
        gender_entity = _entity_class(g, "raw_genders")
        assert L(gender_entity) == "Gender"
        _entity_class(g, "raw_athletes")
        vocab = _vocab_class(g, "AthleteGender", [("MAthleteGender", "M")])
        _obj_prop(g, "hasAthleteGender", "raw_athletes", vocab, "raw_athletes", "gender")
        profiles = [
            TableProfile(table_name="raw_genders", row_count=2,
                         columns={"gender_id": _col("gender_id", pk=True)}),
            TableProfile(table_name="raw_athletes", row_count=100,
                         columns={"gender": _col("gender", [("M", 60), ("F", 40)])}),
        ]
        n = materialise_categorical_individuals(g, profiles, BASE)
        assert n == 0  # collision → left untouched
        assert (URIRef(BASE + "AthleteGender"), RDF.type, OWL.Class) in g


# ---------------------------------------------------------------------------
# Conformed-dimension folding (role-variant columns → one vocabulary class)
# ---------------------------------------------------------------------------

class TestDimensionStem:
    """Role-qualifier stripping is purely structural — generic English role words,
    never dataset terms — and under-stemming is the safe failure."""

    def test_strips_delimited_role_token(self):
        assert _dimension_stem("curr_careunit") == "careunit"
        assert _dimension_stem("first_careunit") == "careunit"
        assert _dimension_stem("prev_service") == "service"
        assert _dimension_stem("home_team") == "team"
        assert _dimension_stem("dest_airport") == "airport"

    def test_strips_smashed_role_prefix(self):
        assert _dimension_stem("originalamountuom") == "amountuom"
        assert _dimension_stem("totalamountuom") == "amountuom"
        assert _dimension_stem("amountuom") == "amountuom"

    def test_longest_match_first(self):
        # "total" must be peeled before "to" leaves a garbage "talamountuom" stem.
        assert _dimension_stem("totalamountuom") == "amountuom"

    def test_non_role_name_unchanged(self):
        assert _dimension_stem("gender") == "gender"
        assert _dimension_stem("discipline_code") == "disciplinecode"
        assert _dimension_stem("valueuom") == "valueuom"  # "value" is not a role word


class TestValuesCorroborate:
    def test_identical(self):
        a = {"x": "x", "y": "y"}
        assert _values_corroborate(a, a)

    def test_subset_joins(self):
        # a sparsely-profiled member {ml} still joins its larger family
        assert _values_corroborate({"ml": "ml"}, {"ml": "ml", "mg": "mg", "u": "U"})

    def test_disjoint_rejected(self):
        assert not _values_corroborate({"a": "a"}, {"b": "b"})

    def test_below_jaccard_threshold_rejected(self):
        # 1 shared of union 4 = 0.25 < 0.3, and not a subset
        assert not _values_corroborate({"a": "a", "b": "b"}, {"a": "a", "c": "c", "d": "d"})

    def test_empty_rejected(self):
        assert not _values_corroborate({}, {"a": "a"})


def _zone_conformed():
    """Two role-variant columns of ONE dimension (curr_zone / prev_zone) modelled by
    the LLM as two parallel classes, each with a duplicate copy of the zone values.
    Generic names — no dataset knowledge in the logic under test."""
    g = Graph()
    _entity_class(g, "raw_moves")
    curr = _vocab_class(g, "CurrZone", [("ACurrZone", "A"), ("BCurrZone", "B")])
    prev = _vocab_class(g, "PrevZone", [("APrevZone", "A"), ("BPrevZone", "B"), ("CPrevZone", "C")])
    _obj_prop(g, "hasCurrZone", "raw_moves", curr, "raw_moves", "curr_zone")
    _obj_prop(g, "hasPrevZone", "raw_moves", prev, "raw_moves", "prev_zone")
    profiles = [
        TableProfile(table_name="raw_moves", row_count=100, columns={
            "curr_zone": _col("curr_zone", [("A", 50), ("B", 50)]),
            "prev_zone": _col("prev_zone", [("A", 30), ("B", 30), ("C", 40)]),
        }),
    ]
    return g, profiles


class TestConformedDimensionFolding:
    def test_role_variants_fold_to_one_class(self):
        g, profiles = _zone_conformed()
        materialise_categorical_individuals(g, profiles, BASE)
        classes = {L(c) for c in g.subjects(RDF.type, OWL.Class)}
        assert "Zone" in classes
        assert "CurrZone" not in classes
        assert "PrevZone" not in classes
        assert (URIRef(BASE + "CurrZone"), RDF.type, OWL.Class) not in g
        assert (URIRef(BASE + "PrevZone"), RDF.type, OWL.Class) not in g

    def test_role_properties_survive_and_repoint(self):
        g, profiles = _zone_conformed()
        materialise_categorical_individuals(g, profiles, BASE)
        zone = URIRef(BASE + "Zone")
        # both role distinctions are preserved as properties, ranging to the shared class
        assert (URIRef(BASE + "hasCurrZone"), RDFS.range, zone) in g
        assert (URIRef(BASE + "hasPrevZone"), RDFS.range, zone) in g

    def test_values_deduped_across_roles(self):
        g, profiles = _zone_conformed()
        materialise_categorical_individuals(g, profiles, BASE)
        zone = URIRef(BASE + "Zone")
        members = sorted(L(i) for i in g.subjects(RDF.type, zone))
        # A appears ONCE, not once per role (no ACurrZone / APrevZone duplication)
        assert members == ["AZone", "BZone", "CZone"]

    def test_coincidental_tiny_overlap_not_merged(self):
        """total*uom {ml} and value*uom {ml} share an identical tiny value set but
        DIFFERENT stems — the stem gate keeps them apart (the value-only trap)."""
        g = Graph()
        _entity_class(g, "raw_flows")
        tot = _vocab_class(g, "Totalamountuom", [("MlTotalamountuom", "ml")])
        val = _vocab_class(g, "Valueuom", [("MlValueuom", "ml")])
        _obj_prop(g, "hasTotalAmountUom", "raw_flows", tot, "raw_flows", "totalamountuom")
        _obj_prop(g, "hasValueUom", "raw_flows", val, "raw_flows", "valueuom")
        profiles = [
            TableProfile(table_name="raw_flows", row_count=10, columns={
                "totalamountuom": _col("totalamountuom", [("ml", 10)]),
                "valueuom": _col("valueuom", [("ml", 10)]),
            }),
        ]
        materialise_categorical_individuals(g, profiles, BASE)
        classes = {L(c) for c in g.subjects(RDF.type, OWL.Class)}
        # distinct stems (amountuom vs valueuom) → two classes, NOT one
        assert "Valueuom" in classes
        assert "Amountuom" in classes or "Totalamountuom" in classes

    def test_same_stem_disjoint_values_not_merged(self):
        """src_code and dest_code share stem 'code' but have disjoint value sets —
        the value gate blocks the merge (different code systems)."""
        g = Graph()
        _entity_class(g, "raw_links")
        sc = _vocab_class(g, "SrcCode", [("XSrcCode", "X"), ("YSrcCode", "Y")])
        dc = _vocab_class(g, "DestCode", [("PDestCode", "P"), ("QDestCode", "Q")])
        _obj_prop(g, "hasSrcCode", "raw_links", sc, "raw_links", "src_code")
        _obj_prop(g, "hasDestCode", "raw_links", dc, "raw_links", "dest_code")
        profiles = [
            TableProfile(table_name="raw_links", row_count=10, columns={
                "src_code": _col("src_code", [("X", 5), ("Y", 5)]),
                "dest_code": _col("dest_code", [("P", 5), ("Q", 5)]),
            }),
        ]
        materialise_categorical_individuals(g, profiles, BASE)
        classes = {L(c) for c in g.subjects(RDF.type, OWL.Class)}
        assert "SrcCode" in classes
        assert "DestCode" in classes


class TestEntityNameCollision:
    """A value vocabulary whose folded stem name collides with a table-entity class
    must be re-derived into a non-colliding class (keeping completeness), not bailed."""

    def _service_like(self):
        # raw_zones → entity class 'Zone'. curr_zone / prev_zone are value columns
        # whose stem 'zone' collides with the Zone entity; the LLM ranged both role
        # properties at a single non-colliding vocab class 'ZoneType'.
        g = Graph()
        _entity_class(g, "raw_zones")           # entity class: Zone
        _entity_class(g, "raw_moves")
        zonetype = _vocab_class(g, "ZoneType", [
            ("AZoneType", "A"), ("BZoneType", "B"),  # LLM captured only 2 of 3
        ])
        _obj_prop(g, "hasCurrZone", "raw_zones", zonetype, "raw_zones", "curr_zone")
        _obj_prop(g, "hasPrevZone", "raw_zones", zonetype, "raw_zones", "prev_zone")
        profiles = [
            TableProfile(table_name="raw_zones", row_count=100, columns={
                "curr_zone": _col("curr_zone", [("A", 40), ("B", 40), ("C", 20)]),
                "prev_zone": _col("prev_zone", [("A", 30), ("B", 30), ("C", 40)]),
            }),
            TableProfile(table_name="raw_moves", row_count=10, columns={}),
        ]
        return g, profiles

    def test_collision_rederives_not_bailed(self):
        g, profiles = self._service_like()
        materialise_categorical_individuals(g, profiles, BASE)
        # the vocab is re-derived into ZoneType (non-colliding), fully populated
        zonetype = URIRef(BASE + "ZoneType")
        members = sorted(L(i) for i in g.subjects(RDF.type, zonetype))
        assert members == ["AZoneType", "BZoneType", "CZoneType"]  # C recovered, not 2
        # the Zone ENTITY class is untouched (no vocab individuals minted onto it)
        zone = URIRef(BASE + "Zone")
        assert (zone, RDF.type, OWL.Class) in g
        assert not list(g.subjects(RDF.type, zone))
        # both role properties still range to the vocab class
        assert (URIRef(BASE + "hasCurrZone"), RDFS.range, zonetype) in g
        assert (URIRef(BASE + "hasPrevZone"), RDFS.range, zonetype) in g

    def test_collision_suffix_fallback_when_no_bound_class(self):
        # If no non-entity vocab class exists to reuse, fall back to a {Stem}Type name.
        from semantic_mapper.ontology.iterative.categorical_individuals import _disambiguate_vocab_canon
        entity = {URIRef(BASE + "Service")}
        got = _disambiguate_vocab_canon("service", set(), entity, BASE)
        assert got == URIRef(BASE + "ServiceType")
        # and it reuses a bound non-entity class when one is present
        got2 = _disambiguate_vocab_canon("service", {URIRef(BASE + "ServiceKind")}, entity, BASE)
        assert got2 == URIRef(BASE + "ServiceKind")
