# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for FK column → ObjectProperty enforcement.

Covers two layers of defence:

  Layer 1 — graph_merger._fix_datatype_fk_properties():
    Pre-merge fragment safety check. If the gen-LLM modelled a FK column
    as DatatypeProperty (ignoring Rule 3), this function converts it to
    ObjectProperty with the correct rdfs:range before the fragment is merged.

  Layer 2 — generator._remove_superseded_fk_datatype_abstracts():
    Post-generation cleanup pass. Abstract DatatypeProperties whose
    skos:altLabel matches a FK column name (e.g. hasSubjectId, altLabel='subject_id')
    are removed when an abstract ObjectProperty supersedes them (e.g. hasPatient).
    All class-scoped subproperties are removed too.

Gold/RDF layer compatibility note:
    gold/builder.py._resolve_fk_properties() and rdf/triple_builder.TripleBuilder both
    detect FK relationships by column name (entity_map), not property OWL type, so they
    function correctly before and after this fix.  The existing DatatypeProperty fallback
    paths in those layers are not removed.
"""
from __future__ import annotations

import logging

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, Namespace, URIRef
from rdflib.namespace import SKOS, XSD

from semantic_mapper.db.models import ColumnProfile, TableProfile
from semantic_mapper.ontology.iterative.graph_merger import _fix_datatype_fk_properties
from semantic_mapper.ontology.iterative.generator import _remove_superseded_fk_datatype_abstracts

BASE = "http://mesh.local/ontology/test#"
ONT = Namespace(BASE)


def _u(local: str) -> URIRef:
    return URIRef(BASE + local)


def _make_fk_info(col: str, ref_table: str) -> list[dict]:
    return [{"constrained_column": col, "referred_table": ref_table}]


def _make_fragment_datatype_fk(col_name: str, domain_local: str) -> Graph:
    """Fragment graph: a DatatypeProperty with altLabel=col_name (the wrong model)."""
    g = Graph()
    prop = _u(f"has{domain_local.capitalize()}{col_name.capitalize()}")
    g.add((prop, RDF.type, OWL.DatatypeProperty))
    g.add((prop, RDFS.domain, _u(domain_local)))
    g.add((prop, RDFS.range, XSD.integer))
    g.add((prop, SKOS.altLabel, Literal(col_name, lang="en")))
    return g


def _make_fragment_objectproperty_fk(col_name: str, domain_local: str, range_local: str) -> Graph:
    """Fragment graph: an ObjectProperty FK (already correct)."""
    g = Graph()
    prop = _u(f"has{domain_local.capitalize()}{range_local}")
    g.add((prop, RDF.type, OWL.ObjectProperty))
    g.add((prop, RDFS.domain, _u(domain_local)))
    g.add((prop, RDFS.range, _u(range_local)))
    g.add((prop, SKOS.altLabel, Literal(col_name, lang="en")))
    return g


# ---------------------------------------------------------------------------
# _fix_datatype_fk_properties
# ---------------------------------------------------------------------------

class TestFixDatatypeFkProperties:
    def test_converts_datatype_to_objectproperty(self):
        """Core scenario: DatatypeProperty altLabel='subject_id' → ObjectProperty Range:Patient."""
        g = _make_fragment_datatype_fk("subject_id", "Admission")
        fk_info = _make_fk_info("subject_id", "patients")

        converted = _fix_datatype_fk_properties(g, BASE, fk_info, set())

        assert converted == 1
        prop = _u("hasAdmissionsubject_id")  # whatever local name was used
        # Find the property by its altLabel instead of hard-coding local name
        prop_uri = _find_prop_by_altlabel(g, "subject_id")
        assert prop_uri is not None
        assert (prop_uri, RDF.type, OWL.ObjectProperty) in g
        assert (prop_uri, RDF.type, OWL.DatatypeProperty) not in g

    def test_range_set_to_referenced_class(self):
        """After conversion, rdfs:range must be the FK referenced class URI."""
        g = _make_fragment_datatype_fk("subject_id", "Admission")
        fk_info = _make_fk_info("subject_id", "patients")

        _fix_datatype_fk_properties(g, BASE, fk_info, set())

        prop_uri = _find_prop_by_altlabel(g, "subject_id")
        ranges = list(g.objects(prop_uri, RDFS.range))
        assert _u("Patient") in ranges, f"Expected range Patient; got {ranges}"
        # XSD range must be gone
        xsd_ranges = [r for r in ranges if str(r).startswith("http://www.w3.org/2001/XMLSchema#")]
        assert xsd_ranges == [], f"XSD range still present: {xsd_ranges}"

    def test_logs_error_for_converted_fk(self, caplog):
        """An ERROR is logged when a DatatypeProperty FK is corrected."""
        g = _make_fragment_datatype_fk("subject_id", "Admission")
        fk_info = _make_fk_info("subject_id", "patients")

        with caplog.at_level(logging.ERROR, logger="semantic_mapper.ontology.iterative.graph_merger"):
            _fix_datatype_fk_properties(g, BASE, fk_info, set())

        errors = [r.message for r in caplog.records if r.levelname == "ERROR"]
        assert any("subject_id" in m for m in errors), f"Expected ERROR for subject_id; got {errors}"

    def test_leaves_objectproperty_fk_unchanged(self):
        """If the FK column is already modelled as ObjectProperty, nothing changes."""
        g = _make_fragment_objectproperty_fk("subject_id", "Admission", "Patient")
        fk_info = _make_fk_info("subject_id", "patients")

        converted = _fix_datatype_fk_properties(g, BASE, fk_info, set())

        assert converted == 0
        prop_uri = _find_prop_by_altlabel(g, "subject_id")
        assert (prop_uri, RDF.type, OWL.ObjectProperty) in g
        assert (prop_uri, RDF.type, OWL.DatatypeProperty) not in g

    def test_skips_skipped_table_fks(self):
        """FK to a skipped (empty) table must NOT be converted — it stays DatatypeProperty."""
        g = _make_fragment_datatype_fk("subject_id", "Admission")
        fk_info = _make_fk_info("subject_id", "patients")
        skipped = {"patients"}

        converted = _fix_datatype_fk_properties(g, BASE, fk_info, skipped)

        assert converted == 0
        prop_uri = _find_prop_by_altlabel(g, "subject_id")
        assert (prop_uri, RDF.type, OWL.DatatypeProperty) in g  # unchanged

    def test_no_fk_info_is_noop(self):
        """Empty fk_info → no conversions, no side effects."""
        g = _make_fragment_datatype_fk("subject_id", "Admission")

        converted = _fix_datatype_fk_properties(g, BASE, [], set())

        assert converted == 0

    def test_converts_multiple_fk_columns_in_one_fragment(self):
        """Multiple FK columns in a single fragment are all corrected."""
        g = Graph()
        for col, domain, ref_table in [
            ("subject_id", "Admission", "patients"),
            ("hadm_id", "Inputevent", "admissions"),
        ]:
            class_local = col.replace("_", "").capitalize()
            prop = _u(f"has{domain}{class_local}")
            g.add((prop, RDF.type, OWL.DatatypeProperty))
            g.add((prop, RDFS.domain, _u(domain)))
            g.add((prop, RDFS.range, XSD.integer))
            g.add((prop, SKOS.altLabel, Literal(col, lang="en")))

        fk_info = [
            {"constrained_column": "subject_id", "referred_table": "patients"},
            {"constrained_column": "hadm_id", "referred_table": "admissions"},
        ]

        converted = _fix_datatype_fk_properties(g, BASE, fk_info, set())

        assert converted == 2
        for col in ("subject_id", "hadm_id"):
            prop_uri = _find_prop_by_altlabel(g, col)
            assert (prop_uri, RDF.type, OWL.ObjectProperty) in g
            assert (prop_uri, RDF.type, OWL.DatatypeProperty) not in g

    def test_derives_class_local_from_raw_prefixed_table(self):
        """FK referred_table with 'raw_' prefix derives correct class local name."""
        g = _make_fragment_datatype_fk("subject_id", "Admission")
        fk_info = _make_fk_info("subject_id", "raw_patients")

        _fix_datatype_fk_properties(g, BASE, fk_info, set())

        prop_uri = _find_prop_by_altlabel(g, "subject_id")
        ranges = list(g.objects(prop_uri, RDFS.range))
        assert _u("Patient") in ranges

    def test_constrained_column_key_variant(self):
        """Accepts 'column' as key alias for 'constrained_column'."""
        g = _make_fragment_datatype_fk("hadm_id", "Chartevents")
        fk_info = [{"column": "hadm_id", "ref_table": "admissions"}]

        converted = _fix_datatype_fk_properties(g, BASE, fk_info, set())

        assert converted == 1


# ---------------------------------------------------------------------------
# _remove_superseded_fk_datatype_abstracts
# ---------------------------------------------------------------------------

def _make_table_profile(table_name: str, fk_col: str, fk_ref: str) -> TableProfile:
    """Minimal TableProfile with one FK column."""
    col = ColumnProfile(
        name=fk_col,
        inferred_type="INT",
        is_nullable=False,
        is_primary_key=False,
        null_rate=0.0,
        distinct_count=100,
    )
    return TableProfile(
        table_name=table_name,
        row_count=100,
        columns={fk_col: col},
        foreign_keys=[{"constrained_column": fk_col, "referred_table": fk_ref}],
    )


class TestRemoveSupersededFkDatatypeAbstracts:
    def test_removes_abstract_when_objectproperty_abstract_exists(self):
        """
        hasSubjectId (DatatypeProperty, no domain, altLabel='subject_id')
        + hasPatient (ObjectProperty abstract)
        → hasSubjectId is removed.
        """
        g = Graph()
        # DatatypeProperty abstract to be removed
        g.add((_u("hasSubjectId"), RDF.type, OWL.DatatypeProperty))
        g.add((_u("hasSubjectId"), SKOS.altLabel, Literal("subject_id", lang="en")))
        g.add((_u("hasSubjectId"), RDFS.range, XSD.integer))

        # ObjectProperty abstract — supersedes hasSubjectId
        g.add((_u("hasPatient"), RDF.type, OWL.ObjectProperty))
        g.add((_u("hasPatient"), RDFS.range, _u("Patient")))

        profiles = [_make_table_profile("admissions", "subject_id", "patients")]
        _remove_superseded_fk_datatype_abstracts(g, profiles, BASE)

        assert (_u("hasSubjectId"), RDF.type, OWL.DatatypeProperty) not in g

    def test_removes_subproperties_of_superseded_abstract(self):
        """
        Class-scoped subproperties (e.g. hasAdmissionSubjectId) are also removed
        when their parent abstract is superseded.
        """
        g = Graph()
        g.add((_u("hasSubjectId"), RDF.type, OWL.DatatypeProperty))
        g.add((_u("hasSubjectId"), SKOS.altLabel, Literal("subject_id", lang="en")))
        g.add((_u("hasSubjectId"), RDFS.range, XSD.integer))

        g.add((_u("hasAdmissionSubjectId"), RDF.type, OWL.DatatypeProperty))
        g.add((_u("hasAdmissionSubjectId"), RDFS.domain, _u("Admission")))
        g.add((_u("hasAdmissionSubjectId"), RDFS.range, XSD.integer))
        g.add((_u("hasAdmissionSubjectId"), RDFS.subPropertyOf, _u("hasSubjectId")))

        g.add((_u("hasPatient"), RDF.type, OWL.ObjectProperty))
        g.add((_u("hasPatient"), RDFS.range, _u("Patient")))

        profiles = [_make_table_profile("admissions", "subject_id", "patients")]
        _remove_superseded_fk_datatype_abstracts(g, profiles, BASE)

        assert (_u("hasSubjectId"), None, None) not in list(g.triples((_u("hasSubjectId"), None, None)))
        assert (_u("hasAdmissionSubjectId"), None, None) not in list(
            g.triples((_u("hasAdmissionSubjectId"), None, None))
        )

    def test_retains_abstract_when_no_objectproperty_abstract(self):
        """
        hasSubjectId is NOT removed when no hasPatient ObjectProperty exists yet.
        This prevents premature removal if the ObjProp abstract was never generated.
        """
        g = Graph()
        g.add((_u("hasSubjectId"), RDF.type, OWL.DatatypeProperty))
        g.add((_u("hasSubjectId"), SKOS.altLabel, Literal("subject_id", lang="en")))

        profiles = [_make_table_profile("admissions", "subject_id", "patients")]
        _remove_superseded_fk_datatype_abstracts(g, profiles, BASE)

        assert (_u("hasSubjectId"), RDF.type, OWL.DatatypeProperty) in g

    def test_retains_non_abstract_datatype_property(self):
        """
        A DatatypeProperty WITH rdfs:domain (concrete/class-scoped) is never removed,
        even if it has an altLabel matching a FK column.
        """
        g = Graph()
        g.add((_u("hasAdmissionSubjectId"), RDF.type, OWL.DatatypeProperty))
        g.add((_u("hasAdmissionSubjectId"), RDFS.domain, _u("Admission")))
        g.add((_u("hasAdmissionSubjectId"), SKOS.altLabel, Literal("subject_id", lang="en")))

        g.add((_u("hasPatient"), RDF.type, OWL.ObjectProperty))

        profiles = [_make_table_profile("admissions", "subject_id", "patients")]
        _remove_superseded_fk_datatype_abstracts(g, profiles, BASE)

        # Has domain → not abstract → must be retained
        assert (_u("hasAdmissionSubjectId"), RDF.type, OWL.DatatypeProperty) in g

    def test_retains_unrelated_datatype_properties(self):
        """
        DatatypeProperties not related to any FK column are untouched.
        """
        g = Graph()
        g.add((_u("hasAdmittime"), RDF.type, OWL.DatatypeProperty))
        g.add((_u("hasAdmittime"), SKOS.altLabel, Literal("admittime", lang="en")))
        g.add((_u("hasPatient"), RDF.type, OWL.ObjectProperty))

        profiles = [_make_table_profile("admissions", "subject_id", "patients")]
        _remove_superseded_fk_datatype_abstracts(g, profiles, BASE)

        assert (_u("hasAdmittime"), RDF.type, OWL.DatatypeProperty) in g

    def test_empty_profiles_is_noop(self):
        """No profiles → nothing removed."""
        g = Graph()
        g.add((_u("hasSubjectId"), RDF.type, OWL.DatatypeProperty))
        g.add((_u("hasSubjectId"), SKOS.altLabel, Literal("subject_id", lang="en")))
        g.add((_u("hasPatient"), RDF.type, OWL.ObjectProperty))

        _remove_superseded_fk_datatype_abstracts(g, [], BASE)

        assert (_u("hasSubjectId"), RDF.type, OWL.DatatypeProperty) in g

    def test_removes_hadm_id_abstract_when_hasadmission_exists(self):
        """hasHadmId (altLabel='hadm_id', hadm_id→admissions→hasAdmission) is removed."""
        g = Graph()
        g.add((_u("hasHadmId"), RDF.type, OWL.DatatypeProperty))
        g.add((_u("hasHadmId"), SKOS.altLabel, Literal("hadm_id", lang="en")))

        g.add((_u("hasAdmission"), RDF.type, OWL.ObjectProperty))
        g.add((_u("hasAdmission"), RDFS.range, _u("Admission")))

        profiles = [_make_table_profile("inputevents_mv", "hadm_id", "admissions")]
        _remove_superseded_fk_datatype_abstracts(g, profiles, BASE)

        assert (_u("hasHadmId"), RDF.type, OWL.DatatypeProperty) not in g


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_prop_by_altlabel(g: Graph, alt: str) -> URIRef | None:
    """Find a property URI by its skos:altLabel (case-insensitive)."""
    for prop in list(g.subjects(RDF.type, OWL.DatatypeProperty)) + \
                list(g.subjects(RDF.type, OWL.ObjectProperty)):
        for lbl in g.objects(prop, SKOS.altLabel):
            if str(lbl).lower() == alt.lower():
                return prop  # type: ignore[return-value]
    return None
