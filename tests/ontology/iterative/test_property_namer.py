# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the PropertyNamer module.

canonical_local() is a pure function — tests are exhaustive and fast.
normalize_datatype_property_uris() tests use small in-memory rdflib graphs.
"""
import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef
from rdflib.namespace import SKOS

from semantic_mapper.ontology.iterative.property_namer import (
    _is_canonical,
    canonical_local,
    normalize_datatype_property_uris,
)

BASE = "http://mesh.local/ontology/mimic#"

MIMIC_SPLITS = {
    "admittime": "admit_time",
    "dischtime": "discharge_time",
    "deathtime": "death_time",
    "edregtime": "ed_reg_time",
    "edouttime": "ed_out_time",
    "charttime": "chart_time",
    "storetime": "store_time",
    "starttime": "start_time",
    "endtime": "end_time",
    "intime": "in_time",
    "outtime": "out_time",
}


# ---------------------------------------------------------------------------
# canonical_local — no word_splits
# ---------------------------------------------------------------------------

class TestCanonicalLocalNoSplits:
    def test_plain_column_name(self):
        assert canonical_local("insurance") == "hasInsurance"

    def test_snake_case(self):
        assert canonical_local("admission_location") == "hasAdmissionLocation"

    def test_snake_case_three_parts(self):
        assert canonical_local("hospital_expire_flag") == "hasHospitalExpireFlag"

    def test_already_has_prefix_camel(self):
        # Has prefix stripped, then re-added — idempotent for well-formed names
        assert canonical_local("hasInsurance") == "hasInsurance"

    def test_has_underscore_prefix_stripped(self):
        assert canonical_local("has_chartevents_data") == "hasCharteventsData"

    def test_has_prefix_lowercase(self):
        assert canonical_local("hasadmission_location") == "hasAdmissionLocation"

    def test_single_word_no_prefix(self):
        assert canonical_local("diagnosis") == "hasDiagnosis"

    def test_single_word_with_has(self):
        assert canonical_local("hasPatient") == "hasPatient"

    def test_column_with_underscore_id(self):
        assert canonical_local("hadm_id") == "hasHadmId"

    def test_column_subject_id(self):
        assert canonical_local("subject_id") == "hasSubjectId"

    def test_ethnicity(self):
        assert canonical_local("ethnicity") == "hasEthnicity"

    def test_language(self):
        assert canonical_local("language") == "hasLanguage"

    def test_discharge_location(self):
        assert canonical_local("discharge_location") == "hasDischargeLocation"

    def test_already_canonical_not_doubled(self):
        result = canonical_local("hasAdmissionLocation")
        assert result == "hasAdmissionLocation"
        assert not result.startswith("hasHas")

    def test_returns_string(self):
        assert isinstance(canonical_local("foo"), str)

    def test_empty_string_does_not_raise(self):
        result = canonical_local("")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# canonical_local — with MIMIC word_splits
# ---------------------------------------------------------------------------

class TestCanonicalLocalWithSplits:
    def test_admittime(self):
        assert canonical_local("admittime", MIMIC_SPLITS) == "hasAdmitTime"

    def test_dischtime(self):
        assert canonical_local("dischtime", MIMIC_SPLITS) == "hasDischargeTime"

    def test_deathtime(self):
        assert canonical_local("deathtime", MIMIC_SPLITS) == "hasDeathTime"

    def test_edregtime(self):
        assert canonical_local("edregtime", MIMIC_SPLITS) == "hasEdRegTime"

    def test_edouttime(self):
        assert canonical_local("edouttime", MIMIC_SPLITS) == "hasEdOutTime"

    def test_charttime(self):
        assert canonical_local("charttime", MIMIC_SPLITS) == "hasChartTime"

    def test_storetime(self):
        assert canonical_local("storetime", MIMIC_SPLITS) == "hasStoreTime"

    def test_starttime(self):
        assert canonical_local("starttime", MIMIC_SPLITS) == "hasStartTime"

    def test_endtime(self):
        assert canonical_local("endtime", MIMIC_SPLITS) == "hasEndTime"

    def test_intime(self):
        assert canonical_local("intime", MIMIC_SPLITS) == "hasInTime"

    def test_outtime(self):
        assert canonical_local("outtime", MIMIC_SPLITS) == "hasOutTime"

    def test_has_prefixed_admittime(self):
        # LLM generated hasAdmittime — should still expand
        assert canonical_local("hasAdmittime", MIMIC_SPLITS) == "hasAdmitTime"

    def test_has_prefixed_dischtime(self):
        assert canonical_local("hasDischtime", MIMIC_SPLITS) == "hasDischargeTime"

    def test_non_split_word_unchanged(self):
        # 'insurance' has no split entry — should pass through normally
        assert canonical_local("insurance", MIMIC_SPLITS) == "hasInsurance"

    def test_snake_case_unchanged_with_splits(self):
        assert canonical_local("admission_location", MIMIC_SPLITS) == "hasAdmissionLocation"


# ---------------------------------------------------------------------------
# canonical_local — has_ prefix stripping
# ---------------------------------------------------------------------------

class TestHasPrefixStripping:
    def test_has_underscore_stripped(self):
        result = canonical_local("has_chartevents_data")
        assert not result.startswith("hasHas")
        assert result == "hasCharteventsData"

    def test_has_underscore_stripped_with_splits(self):
        ws = {"charttime": "chart_time"}
        # has_charttime → charttime → chart_time → ChartTime
        result = canonical_local("has_charttime", ws)
        assert result == "hasChartTime"

    def test_no_double_has(self):
        for source in ["has_foo", "hasFoo", "has_foo_bar"]:
            result = canonical_local(source)
            assert result.count("has") == 1 or result.startswith("hasHas") is False, \
                f"Double-has in result for source={source!r}: {result}"


# ---------------------------------------------------------------------------
# _is_canonical helper
# ---------------------------------------------------------------------------

class TestIsCanonical:
    def test_canonical(self):
        assert _is_canonical("hasAdmissionLocation") is True

    def test_canonical_single(self):
        assert _is_canonical("hasInsurance") is True

    def test_no_has_prefix(self):
        assert _is_canonical("admission_location") is False

    def test_snake_case(self):
        assert _is_canonical("has_admission_location") is False

    def test_lowercase_after_has(self):
        assert _is_canonical("hasadmittime") is False

    def test_has_only(self):
        assert _is_canonical("has") is False

    def test_digit_after_has(self):
        assert _is_canonical("has3foo") is False


# ---------------------------------------------------------------------------
# normalize_datatype_property_uris — graph mutation tests
# ---------------------------------------------------------------------------

def _make_data_prop(g: Graph, local: str, alt: str | None = None) -> URIRef:
    uri = URIRef(BASE + local)
    g.add((uri, RDF.type, OWL.DatatypeProperty))
    g.add((uri, RDFS.range, URIRef("http://www.w3.org/2001/XMLSchema#string")))
    if alt:
        g.add((uri, SKOS.altLabel, Literal(alt, lang="en")))
    return uri


class TestNormalizeDataTypePropertyUris:
    def test_snake_case_renamed(self):
        g = Graph()
        _make_data_prop(g, "admission_location", alt="admission_location")
        normalize_datatype_property_uris(g, BASE)
        assert (URIRef(f"{BASE}hasAdmissionLocation"), RDF.type, OWL.DatatypeProperty) in g
        assert (URIRef(f"{BASE}admission_location"), RDF.type, OWL.DatatypeProperty) not in g

    def test_bare_column_name_renamed(self):
        g = Graph()
        _make_data_prop(g, "admittime", alt="admittime")
        normalize_datatype_property_uris(g, BASE, MIMIC_SPLITS)
        assert (URIRef(f"{BASE}hasAdmitTime"), RDF.type, OWL.DatatypeProperty) in g

    def test_already_canonical_not_touched(self):
        g = Graph()
        prop = _make_data_prop(g, "hasAdmissionLocation")
        normalize_datatype_property_uris(g, BASE)
        assert (URIRef(f"{BASE}hasAdmissionLocation"), RDF.type, OWL.DatatypeProperty) in g

    def test_altlabel_preferred_over_local_name(self):
        g = Graph()
        # URI says hasFoo but altLabel says admission_location
        _make_data_prop(g, "hasFoo", alt="admission_location")
        normalize_datatype_property_uris(g, BASE)
        assert (URIRef(f"{BASE}hasAdmissionLocation"), RDF.type, OWL.DatatypeProperty) in g
        assert (URIRef(f"{BASE}hasFoo"), RDF.type, OWL.DatatypeProperty) not in g

    def test_domain_triple_rewritten(self):
        g = Graph()
        cls = URIRef(f"{BASE}Admission")
        g.add((cls, RDF.type, OWL.Class))
        prop = _make_data_prop(g, "admittime", alt="admittime")
        g.add((prop, RDFS.domain, cls))
        normalize_datatype_property_uris(g, BASE, MIMIC_SPLITS)
        new_prop = URIRef(f"{BASE}hasAdmitTime")
        assert (new_prop, RDFS.domain, cls) in g

    def test_range_triple_rewritten(self):
        g = Graph()
        prop = _make_data_prop(g, "admittime", alt="admittime")
        xsd_dt = URIRef("http://www.w3.org/2001/XMLSchema#dateTime")
        g.add((prop, RDFS.range, xsd_dt))
        normalize_datatype_property_uris(g, BASE, MIMIC_SPLITS)
        new_prop = URIRef(f"{BASE}hasAdmitTime")
        assert (new_prop, RDFS.range, xsd_dt) in g

    def test_altlabel_preserved_after_rename(self):
        g = Graph()
        _make_data_prop(g, "admittime", alt="admittime")
        normalize_datatype_property_uris(g, BASE, MIMIC_SPLITS)
        new_prop = URIRef(f"{BASE}hasAdmitTime")
        alts = list(g.objects(new_prop, SKOS.altLabel))
        assert any(str(a) == "admittime" for a in alts)

    def test_object_property_not_touched(self):
        g = Graph()
        obj_prop = URIRef(f"{BASE}hasPatient")
        g.add((obj_prop, RDF.type, OWL.ObjectProperty))
        normalize_datatype_property_uris(g, BASE)
        assert (obj_prop, RDF.type, OWL.ObjectProperty) in g

    def test_shadowed_by_object_property_removed(self):
        g = Graph()
        _make_data_prop(g, "insurance", alt="insurance")
        obj_prop = URIRef(f"{BASE}hasInsurance")
        g.add((obj_prop, RDF.type, OWL.ObjectProperty))
        normalize_datatype_property_uris(g, BASE)
        assert (URIRef(f"{BASE}insurance"), RDF.type, OWL.DatatypeProperty) not in g
        assert (obj_prop, RDF.type, OWL.ObjectProperty) in g

    def test_shadowed_transfers_domain_to_object_property(self):
        # DataProp had domain=Admission — when removed, domain must be copied to ObjProp
        g = Graph()
        admission = URIRef(f"{BASE}Admission")
        g.add((admission, RDF.type, OWL.Class))
        data_prop = _make_data_prop(g, "insurance", alt="insurance")
        g.add((data_prop, RDFS.domain, admission))
        obj_prop = URIRef(f"{BASE}hasInsurance")
        g.add((obj_prop, RDF.type, OWL.ObjectProperty))
        normalize_datatype_property_uris(g, BASE)
        # DataProp gone, but ObjProp now has the domain
        assert (URIRef(f"{BASE}insurance"), RDF.type, OWL.DatatypeProperty) not in g
        assert (obj_prop, RDFS.domain, admission) in g

    def test_shadowed_does_not_overwrite_existing_obj_prop_domain(self):
        # ObjProp already has domain=Patient — DataProp's domain=Admission must NOT overwrite it
        g = Graph()
        admission = URIRef(f"{BASE}Admission")
        patient = URIRef(f"{BASE}Patient")
        g.add((admission, RDF.type, OWL.Class))
        g.add((patient, RDF.type, OWL.Class))
        data_prop = _make_data_prop(g, "insurance", alt="insurance")
        g.add((data_prop, RDFS.domain, admission))
        obj_prop = URIRef(f"{BASE}hasInsurance")
        g.add((obj_prop, RDF.type, OWL.ObjectProperty))
        g.add((obj_prop, RDFS.domain, patient))  # already set
        normalize_datatype_property_uris(g, BASE)
        # Domain must remain Patient, not Admission
        domains = list(g.objects(obj_prop, RDFS.domain))
        assert patient in domains
        assert admission not in domains

    def test_shadowed_no_altlabel_not_removed(self):
        # DataProp with no altLabel and target taken by ObjectProp → conservative: leave it
        g = Graph()
        _make_data_prop(g, "hasInsurance")  # already canonical, no altLabel
        obj_prop = URIRef(f"{BASE}hasInsurance")
        # Remove the DataProp type and add as ObjProp to simulate collision
        g.remove((URIRef(f"{BASE}hasInsurance"), RDF.type, OWL.DatatypeProperty))
        g.add((URIRef(f"{BASE}hasInsurance"), RDF.type, OWL.ObjectProperty))
        # Now add a snake_case DataProp without altLabel pointing at same canonical
        _make_data_prop(g, "has_insurance")  # no altLabel — should not be removed
        normalize_datatype_property_uris(g, BASE)
        assert (URIRef(f"{BASE}has_insurance"), RDF.type, OWL.DatatypeProperty) in g

    def test_target_datatype_occupied_skips_rename(self):
        g = Graph()
        # Two DataProps both wanting hasInsurance — second one skipped
        _make_data_prop(g, "insurance", alt="insurance")
        _make_data_prop(g, "hasInsurance")  # already at target URI (no altLabel, canonical)
        count = normalize_datatype_property_uris(g, BASE)
        # insurance DataProp renamed; hasInsurance not touched
        assert (URIRef(f"{BASE}hasInsurance"), RDF.type, OWL.DatatypeProperty) in g

    def test_returns_rename_count(self):
        g = Graph()
        _make_data_prop(g, "ethnicity", alt="ethnicity")
        _make_data_prop(g, "language", alt="language")
        count = normalize_datatype_property_uris(g, BASE)
        assert count == 2

    def test_no_has_prefix_uri_normalized(self):
        # LLM forgot altLabel but used bare column name as URI
        g = Graph()
        _make_data_prop(g, "hospital_expire_flag")  # no altLabel
        normalize_datatype_property_uris(g, BASE)
        assert (URIRef(f"{BASE}hasHospitalExpireFlag"), RDF.type, OWL.DatatypeProperty) in g

    def test_already_has_prefixed_no_altlabel_not_touched(self):
        # hasHadmId with no altLabel is already canonical — leave it
        g = Graph()
        _make_data_prop(g, "hasHadmId")  # no altLabel, already canonical
        normalize_datatype_property_uris(g, BASE)
        assert (URIRef(f"{BASE}hasHadmId"), RDF.type, OWL.DatatypeProperty) in g

    def test_multiple_props_all_normalized(self):
        g = Graph()
        for col in ["admission_location", "ethnicity", "language", "diagnosis"]:
            _make_data_prop(g, col, alt=col)
        normalize_datatype_property_uris(g, BASE)
        for expected in ["hasAdmissionLocation", "hasEthnicity", "hasLanguage", "hasDiagnosis"]:
            assert (URIRef(f"{BASE}{expected}"), RDF.type, OWL.DatatypeProperty) in g

    def test_dischtime_with_splits(self):
        g = Graph()
        _make_data_prop(g, "dischtime", alt="dischtime")
        normalize_datatype_property_uris(g, BASE, MIMIC_SPLITS)
        assert (URIRef(f"{BASE}hasDischargeTime"), RDF.type, OWL.DatatypeProperty) in g
        assert (URIRef(f"{BASE}dischtime"), RDF.type, OWL.DatatypeProperty) not in g
