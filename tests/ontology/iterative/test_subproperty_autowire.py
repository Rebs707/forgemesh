# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the abstract ObjectProperty subPropertyOf wiring.

Covers:
  - graph_merger auto-wires ObjectProperty → abstract parent when range matches
  - graph_merger logs WARNING and skips when range matches multiple abstract parents
  - graph_merger corrects XSD range to named-class range when subPropertyOf is declared
  - gen_prompt shows ABSTRACT OBJECTPROPERTIES section and Rule 15 when obj abstracts present
  - embedding_index.retrieve_abstract_object_properties filters to ObjectProperty only
  - generator supplements retrieved_abstract with per-FK retrieval
"""
import re
import logging

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef
from rdflib.namespace import XSD

from semantic_mapper.ontology.iterative.embedding_index import AccumulatedClassIndex
from semantic_mapper.ontology.iterative.gen_prompt import build_gen_prompt
from semantic_mapper.ontology.iterative.graph_merger import (
    MergeResult,
    _autowire_subproperty_by_range,
    _fix_xsd_range_on_subproperties,
    merge,
)
from semantic_mapper.ontology.iterative.models import (
    AccumulatedAbstractPropertySummary,
    AccumulatedClassSummary,
    ColumnContext,
    TableContext,
)

BASE = "http://mesh.local/ontology/test#"
XSD_INT = str(XSD.integer)
XSD_STRING = str(XSD.string)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uri(local: str) -> URIRef:
    return URIRef(f"{BASE}{local}")


def _graph_with_abstract_obj_prop(
    abstract_local: str,
    range_local: str,
) -> Graph:
    """Return an accumulating graph that already contains one abstract ObjectProperty."""
    g = Graph()
    prop = _uri(abstract_local)
    cls = _uri(range_local)
    g.add((cls, RDF.type, OWL.Class))
    g.add((prop, RDF.type, OWL.ObjectProperty))
    g.add((prop, RDFS.range, cls))
    # no rdfs:domain → abstract
    g.add((prop, RDFS.label, Literal(f"has {range_local.lower()}", lang="en")))
    return g


def _fragment_with_fk_prop(
    fk_prop_local: str,
    domain_local: str,
    range_local: str,
    add_subproperty: bool = False,
    range_xsd: bool = False,
) -> Graph:
    """Fragment declaring one FK ObjectProperty and one domain class."""
    g = Graph()
    cls = _uri(domain_local)
    prop = _uri(fk_prop_local)
    range_cls = _uri(range_local) if not range_xsd else XSD.integer
    g.add((cls, RDF.type, OWL.Class))
    g.add((cls, RDFS.label, Literal(domain_local, lang="en")))
    g.add((prop, RDF.type, OWL.ObjectProperty))
    g.add((prop, RDFS.domain, cls))
    g.add((prop, RDFS.range, range_cls))
    g.add((prop, RDFS.label, Literal(f"has {fk_prop_local}", lang="en")))
    if add_subproperty:
        parent = _uri(f"has{range_local}")
        g.add((prop, RDFS.subPropertyOf, parent))
    return g


def _col_ctx(col_name, inferred_type="INTEGER", is_fk=False, fk_ref_table=None):
    return ColumnContext(
        table_name="inputevents_mv",
        col_name=col_name,
        inferred_type=inferred_type,
        is_pk=False,
        is_fk=is_fk,
        fk_ref_table=fk_ref_table,
        null_rate=0.0,
        distinct_count=1000,
    )


def _table_ctx(cols):
    return TableContext(
        table_name="inputevents_mv",
        row_count=1000,
        columns=cols,
        explicit_fks=[],
        processing_index=3,
        total_tables=5,
    )


# ---------------------------------------------------------------------------
# graph_merger: auto-wire by range match
# ---------------------------------------------------------------------------

class TestAutowireByRange:
    def test_auto_wires_when_exactly_one_abstract_parent(self):
        acc = _graph_with_abstract_obj_prop("hasAdmission", "Admission")
        frag = _fragment_with_fk_prop("hasInputeventsMvAdmission", "InputeventsMv", "Admission")

        result = merge(acc, frag, "inputevents_mv", BASE)

        prop = _uri("hasInputeventsMvAdmission")
        abstract = _uri("hasAdmission")
        assert (prop, RDFS.subPropertyOf, abstract) in acc

    def test_auto_wire_logged_as_info(self, caplog):
        acc = _graph_with_abstract_obj_prop("hasAdmission", "Admission")
        frag = _fragment_with_fk_prop("hasInputeventsMvAdmission", "InputeventsMv", "Admission")

        with caplog.at_level(logging.INFO, logger="semantic_mapper.ontology.iterative.graph_merger"):
            merge(acc, frag, "inputevents_mv", BASE)

        info_msgs = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any("Auto-wired" in m and "hasInputeventsMvAdmission" in m for m in info_msgs)
        assert any("hasAdmission" in m for m in info_msgs)

    def test_auto_wire_result_recorded(self):
        acc = _graph_with_abstract_obj_prop("hasAdmission", "Admission")
        frag = _fragment_with_fk_prop("hasInputeventsMvAdmission", "InputeventsMv", "Admission")

        result = merge(acc, frag, "inputevents_mv", BASE)

        assert any("hasInputeventsMvAdmission" in s for s in result.auto_wired)

    def test_no_auto_wire_when_already_has_subproperty(self):
        """If the gen-LLM already wired subPropertyOf correctly, no double-wiring."""
        acc = _graph_with_abstract_obj_prop("hasAdmission", "Admission")
        frag = _fragment_with_fk_prop(
            "hasInputeventsMvAdmission", "InputeventsMv", "Admission",
            add_subproperty=True,
        )

        result = merge(acc, frag, "inputevents_mv", BASE)

        # Should still be correct, no duplicate subPropertyOf
        prop = _uri("hasInputeventsMvAdmission")
        abstract = _uri("hasAdmission")
        sub_parents = list(acc.objects(prop, RDFS.subPropertyOf))
        assert sub_parents.count(abstract) == 1
        assert result.auto_wired == []

    def test_no_auto_wire_when_no_matching_abstract(self):
        """If no abstract ObjectProperty has the same range, nothing is wired."""
        acc = _graph_with_abstract_obj_prop("hasAdmission", "Admission")
        # Fragment points to a different class (Patient), no matching abstract
        frag = _fragment_with_fk_prop("hasInputeventsMvPatient", "InputeventsMv", "Patient")

        result = merge(acc, frag, "inputevents_mv", BASE)

        prop = _uri("hasInputeventsMvPatient")
        assert not list(acc.objects(prop, RDFS.subPropertyOf))
        assert result.auto_wired == []

    def test_warning_and_no_wire_when_multiple_abstracts_match(self, caplog):
        """Two abstract ObjectProperties with the same range → WARNING, no auto-wire."""
        acc = Graph()
        cls = _uri("Admission")
        acc.add((cls, RDF.type, OWL.Class))
        # Two abstract parents, same range
        for local in ("hasAdmission", "hasHospitalAdmission"):
            prop = _uri(local)
            acc.add((prop, RDF.type, OWL.ObjectProperty))
            acc.add((prop, RDFS.range, cls))

        frag = _fragment_with_fk_prop("hasInputeventsMvAdmission", "InputeventsMv", "Admission")

        with caplog.at_level(logging.WARNING, logger="semantic_mapper.ontology.iterative.graph_merger"):
            result = merge(acc, frag, "inputevents_mv", BASE)

        prop = _uri("hasInputeventsMvAdmission")
        assert not list(acc.objects(prop, RDFS.subPropertyOf))
        assert result.auto_wired == []
        warn_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("ambiguous" in m.lower() and "hasInputeventsMvAdmission" in m for m in warn_msgs)


# ---------------------------------------------------------------------------
# graph_merger: wrong-range fix
# ---------------------------------------------------------------------------

class TestFixXsdRange:
    def test_xsd_range_corrected_to_named_class(self):
        """ObjectProperty with XSD range but subPropertyOf abstract parent → range corrected."""
        acc = _graph_with_abstract_obj_prop("hasAdmission", "Admission")
        frag = _fragment_with_fk_prop(
            "hasInputeventsMvAdmission", "InputeventsMv", "Admission",
            range_xsd=True,
            add_subproperty=True,
        )

        merge(acc, frag, "inputevents_mv", BASE)

        prop = _uri("hasInputeventsMvAdmission")
        ranges = list(acc.objects(prop, RDFS.range))
        range_strs = [str(r) for r in ranges]
        assert not any("xmlSchema" in r or "integer" in r for r in range_strs), (
            f"XSD range not corrected: {range_strs}"
        )
        assert any(BASE in r or "Admission" in r for r in range_strs)

    def test_xsd_range_correction_logged_as_warning(self, caplog):
        acc = _graph_with_abstract_obj_prop("hasAdmission", "Admission")
        frag = _fragment_with_fk_prop(
            "hasInputeventsMvAdmission", "InputeventsMv", "Admission",
            range_xsd=True,
            add_subproperty=True,
        )

        with caplog.at_level(logging.WARNING, logger="semantic_mapper.ontology.iterative.graph_merger"):
            merge(acc, frag, "inputevents_mv", BASE)

        warn_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("corrected" in m.lower() and "hasInputeventsMvAdmission" in m for m in warn_msgs)

    def test_correct_class_range_not_changed(self):
        """ObjectProperty already has named-class range → left untouched."""
        acc = _graph_with_abstract_obj_prop("hasAdmission", "Admission")
        frag = _fragment_with_fk_prop(
            "hasInputeventsMvAdmission", "InputeventsMv", "Admission",
            add_subproperty=True,
        )

        merge(acc, frag, "inputevents_mv", BASE)

        prop = _uri("hasInputeventsMvAdmission")
        ranges = list(acc.objects(prop, RDFS.range))
        assert _uri("Admission") in ranges


# ---------------------------------------------------------------------------
# _autowire_subproperty_by_range unit tests
# ---------------------------------------------------------------------------

class TestAutowireUnit:
    def test_wires_single_match(self):
        acc = Graph()
        abstract = _uri("hasAdmission")
        cls = _uri("Admission")
        prop = _uri("hasMyAdmission")
        acc.add((abstract, RDF.type, OWL.ObjectProperty))
        acc.add((abstract, RDFS.range, cls))
        acc.add((prop, RDF.type, OWL.ObjectProperty))
        acc.add((prop, RDFS.domain, _uri("MyClass")))
        acc.add((prop, RDFS.range, cls))
        result = MergeResult()

        _autowire_subproperty_by_range(acc, [str(prop)], result)

        assert (prop, RDFS.subPropertyOf, abstract) in acc
        assert len(result.auto_wired) == 1

    def test_does_not_wire_abstract_prop_itself(self):
        """The abstract prop (no domain) must not be wired to itself."""
        acc = Graph()
        abstract = _uri("hasAdmission")
        cls = _uri("Admission")
        acc.add((abstract, RDF.type, OWL.ObjectProperty))
        acc.add((abstract, RDFS.range, cls))
        result = MergeResult()

        _autowire_subproperty_by_range(acc, [str(abstract)], result)

        assert list(acc.objects(abstract, RDFS.subPropertyOf)) == []


# ---------------------------------------------------------------------------
# gen_prompt: abstract ObjectProperties section and Rule 15
# ---------------------------------------------------------------------------

class TestGenPromptAbstractObjectProperties:
    def _build(self, obj_props=None):
        col = _col_ctx("hadm_id", is_fk=True, fk_ref_table="admissions")
        ctx = _table_ctx([col])
        return build_gen_prompt(
            table_ctx=ctx,
            retrieved_classes=[],
            deterministic_uri=f"{BASE}InputeventsMv",
            base_iri=BASE,
            abstract_properties=obj_props or [],
        )

    def test_obj_abstract_section_shown(self):
        ap = AccumulatedAbstractPropertySummary(
            local_name="hasAdmission",
            label="has admission",
            uri=f"{BASE}hasAdmission",
            range_uri=f"{BASE}Admission",
            prop_type="ObjectProperty",
        )
        prompt = self._build([ap])
        assert "ABSTRACT OBJECTPROPERTIES FOR FK RANGES" in prompt
        assert "hasAdmission" in prompt

    def test_rule_16_present_when_obj_abstracts_in_prompt(self):
        ap = AccumulatedAbstractPropertySummary(
            local_name="hasAdmission",
            label="has admission",
            uri=f"{BASE}hasAdmission",
            range_uri=f"{BASE}Admission",
            prop_type="ObjectProperty",
        )
        prompt = self._build([ap])
        assert "16." in prompt
        assert "FK ABSTRACT PARENT" in prompt
        assert "SubPropertyOf" in prompt

    def test_concrete_examples_in_rule_16(self):
        ap = AccumulatedAbstractPropertySummary(
            local_name="hasAdmission",
            label="has admission",
            uri=f"{BASE}hasAdmission",
            range_uri=f"{BASE}Admission",
            prop_type="ObjectProperty",
        )
        prompt = self._build([ap])
        assert "hasAdmission" in prompt  # the accumulated abstract property is listed
        examples = re.findall(r"property Range: :(\w+)\s+→ SubPropertyOf: :has\1", prompt)
        assert len(examples) >= 3  # rule 16 shows concrete range → parent examples

    def test_dtype_abstract_section_shown_separately(self):
        ap_dtype = AccumulatedAbstractPropertySummary(
            local_name="hasValue",
            label="has value",
            uri=f"{BASE}hasValue",
            range_uri="http://www.w3.org/2001/XMLSchema#string",
            prop_type="DatatypeProperty",
        )
        ap_obj = AccumulatedAbstractPropertySummary(
            local_name="hasAdmission",
            label="has admission",
            uri=f"{BASE}hasAdmission",
            range_uri=f"{BASE}Admission",
            prop_type="ObjectProperty",
        )
        prompt = self._build([ap_dtype, ap_obj])
        assert "ABSTRACT DATATYPEPROPERTIES" in prompt
        assert "ABSTRACT OBJECTPROPERTIES FOR FK RANGES" in prompt

    def test_no_obj_abstract_section_when_none_provided(self):
        ap_dtype = AccumulatedAbstractPropertySummary(
            local_name="hasValue",
            label="has value",
            uri=f"{BASE}hasValue",
            range_uri="http://www.w3.org/2001/XMLSchema#string",
            prop_type="DatatypeProperty",
        )
        prompt = self._build([ap_dtype])
        assert "ABSTRACT OBJECTPROPERTIES FOR FK RANGES" not in prompt


# ---------------------------------------------------------------------------
# embedding_index: retrieve_abstract_object_properties filters by prop_type
# ---------------------------------------------------------------------------

class TestRetrieveAbstractObjectProperties:
    def test_returns_only_object_properties(self):
        idx = AccumulatedClassIndex.__new__(AccumulatedClassIndex)
        idx._model_name = "all-MiniLM-L6-v2"
        idx._model = None
        idx._classes = []
        idx._embeddings = None
        idx._abstract_props = []
        idx._abstract_embeddings = None
        idx._indexed_prop_uris = set()

        ap_dtype = AccumulatedAbstractPropertySummary(
            local_name="hasValue", label="has value",
            uri=f"{BASE}hasValue",
            range_uri="http://www.w3.org/2001/XMLSchema#string",
            prop_type="DatatypeProperty",
        )
        ap_obj = AccumulatedAbstractPropertySummary(
            local_name="hasAdmission", label="has admission",
            uri=f"{BASE}hasAdmission",
            range_uri=f"{BASE}Admission",
            prop_type="ObjectProperty",
        )
        idx.add_all_abstract_properties([ap_dtype, ap_obj])

        results = idx.retrieve_abstract_object_properties("admission", top_k=5)
        assert all(r.prop_type == "ObjectProperty" for r in results)
        assert any(r.local_name == "hasAdmission" for r in results)

    def test_returns_empty_when_no_object_properties(self):
        idx = AccumulatedClassIndex.__new__(AccumulatedClassIndex)
        idx._model_name = "all-MiniLM-L6-v2"
        idx._model = None
        idx._classes = []
        idx._embeddings = None
        idx._abstract_props = []
        idx._abstract_embeddings = None
        idx._indexed_prop_uris = set()

        ap_dtype = AccumulatedAbstractPropertySummary(
            local_name="hasValue", label="has value",
            uri=f"{BASE}hasValue",
            range_uri="http://www.w3.org/2001/XMLSchema#string",
            prop_type="DatatypeProperty",
        )
        idx.add_all_abstract_properties([ap_dtype])

        results = idx.retrieve_abstract_object_properties("admission", top_k=5)
        assert results == []

    def test_returns_empty_when_index_empty(self):
        idx = AccumulatedClassIndex.__new__(AccumulatedClassIndex)
        idx._model_name = "all-MiniLM-L6-v2"
        idx._model = None
        idx._classes = []
        idx._embeddings = None
        idx._abstract_props = []
        idx._abstract_embeddings = None
        idx._indexed_prop_uris = set()

        results = idx.retrieve_abstract_object_properties("admission")
        assert results == []
