# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for four structural-correctness fixes in the iterative ontology generator:

Issue 1 — Existing property locals are injected into the gen prompt so the LLM
           knows which names are already taken and must use class-scoped variants.
Issue 2 — Dual-typed URIs (both ObjectProperty and DatatypeProperty in one fragment)
           are detected by check_duplicate_property_types before merging.
Issue 3 — merge() warns (via domain_range_warnings) when a newly-added property
           is missing rdfs:domain or rdfs:range.
Issue 4 — judge_prompt contains hard-rejection rules for missing domain/range and
           dual-type conflicts; manchester_parser preserves SubPropertyOf declarations.
"""
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef
from rdflib.namespace import SKOS

from semantic_mapper.ontology.iterative.gen_prompt import build_gen_prompt
from semantic_mapper.ontology.iterative.graph_merger import (
    MergeResult,
    check_duplicate_property_types,
    merge,
)
from semantic_mapper.ontology.iterative.judge_prompt import build_judge_prompt
from semantic_mapper.ontology.iterative.manchester_parser import parse_manchester
from semantic_mapper.ontology.iterative.models import (
    AccumulatedClassSummary,
    OntologyFragment,
)

BASE = "http://mesh.local/ontology/test#"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_table_ctx(table_name: str = "raw_events"):
    from semantic_mapper.ontology.iterative.models import ColumnContext, TableContext
    cols = [
        ColumnContext(
            table_name=table_name,
            col_name="event_id", inferred_type="INT",
            is_pk=True, is_fk=False, fk_ref_table=None,
            is_nullable=False, null_rate=0.0, distinct_count=100,
        ),
        ColumnContext(
            table_name=table_name,
            col_name="value", inferred_type="VARCHAR",
            is_pk=False, is_fk=False, fk_ref_table=None,
            is_nullable=True, null_rate=0.1, distinct_count=50,
        ),
    ]
    return TableContext(
        table_name=table_name,
        row_count=100,
        columns=cols,
        processing_index=1,
        total_tables=1,
    )


def _minimal_fragment(table_name: str = "raw_events") -> OntologyFragment:
    return OntologyFragment(
        table_name=table_name,
        manchester_syntax="",
        classes_declared=[],
        properties_declared=[],
        retry_count=0,
    )


# ---------------------------------------------------------------------------
# Issue 1: existing property locals injected into gen prompt
# ---------------------------------------------------------------------------

class TestExistingPropertyLocalsInGenPrompt:
    def test_properties_section_appears_when_locals_provided(self):
        table_ctx = _minimal_table_ctx()
        prompt = build_gen_prompt(
            table_ctx=table_ctx,
            retrieved_classes=[],
            deterministic_uri=f"{BASE}Event",
            base_iri=BASE,
            existing_property_locals=["hasValue", "hasCode"],
        )
        assert "PROPERTIES ALREADY IN THE ONTOLOGY" in prompt

    def test_existing_property_uris_listed(self):
        table_ctx = _minimal_table_ctx()
        prompt = build_gen_prompt(
            table_ctx=table_ctx,
            retrieved_classes=[],
            deterministic_uri=f"{BASE}Event",
            base_iri=BASE,
            existing_property_locals=["hasValue", "hasCode"],
        )
        assert f"{BASE}hasValue" in prompt
        assert f"{BASE}hasCode" in prompt

    def test_no_properties_section_when_empty(self):
        table_ctx = _minimal_table_ctx()
        prompt = build_gen_prompt(
            table_ctx=table_ctx,
            retrieved_classes=[],
            deterministic_uri=f"{BASE}Event",
            base_iri=BASE,
            existing_property_locals=[],
        )
        assert "PROPERTIES ALREADY IN THE ONTOLOGY" not in prompt

    def test_rule_11_class_scoped_instruction_present(self):
        """Rule 11 must tell the LLM to use has{Class}{Concept} when a name collides."""
        table_ctx = _minimal_table_ctx()
        prompt = build_gen_prompt(
            table_ctx=table_ctx,
            retrieved_classes=[],
            deterministic_uri=f"{BASE}Event",
            base_iri=BASE,
            existing_property_locals=["hasValue"],
        )
        assert "class-scoped" in prompt.lower()
        assert "SubPropertyOf" in prompt

    def test_common_names_always_scoped_rule_present(self):
        """Rule 6 must list the common bare names that always require class-scoping."""
        table_ctx = _minimal_table_ctx()
        prompt = build_gen_prompt(
            table_ctx=table_ctx,
            retrieved_classes=[],
            deterministic_uri=f"{BASE}Event",
            base_iri=BASE,
        )
        assert "value" in prompt
        assert "status" in prompt
        assert "class-scoped" in prompt.lower()

    def test_duplicate_locals_deduplicated(self):
        table_ctx = _minimal_table_ctx()
        prompt = build_gen_prompt(
            table_ctx=table_ctx,
            retrieved_classes=[],
            deterministic_uri=f"{BASE}Event",
            base_iri=BASE,
            existing_property_locals=["hasValue", "hasValue", "hasCode"],
        )
        assert prompt.count(f"{BASE}hasValue") == 1


# ---------------------------------------------------------------------------
# Issue 2: dual-typed property detection
# ---------------------------------------------------------------------------

class TestCheckDuplicatePropertyTypes:
    def test_detects_dual_typed_uri(self):
        """A URI typed as both ObjectProperty and DatatypeProperty must be flagged."""
        g = Graph()
        uri = URIRef(f"{BASE}hasType")
        g.add((uri, RDF.type, OWL.ObjectProperty))
        g.add((uri, RDF.type, OWL.DatatypeProperty))
        dual = check_duplicate_property_types(g)
        assert f"{BASE}hasType" in dual

    def test_clean_graph_returns_empty(self):
        g = Graph()
        g.add((URIRef(f"{BASE}hasType"), RDF.type, OWL.ObjectProperty))
        g.add((URIRef(f"{BASE}hasValue"), RDF.type, OWL.DatatypeProperty))
        assert check_duplicate_property_types(g) == []

    def test_detects_multiple_conflicts(self):
        g = Graph()
        for local in ("hasA", "hasB"):
            uri = URIRef(f"{BASE}{local}")
            g.add((uri, RDF.type, OWL.ObjectProperty))
            g.add((uri, RDF.type, OWL.DatatypeProperty))
        dual = check_duplicate_property_types(g)
        assert len(dual) == 2

    def test_class_uri_not_flagged(self):
        """owl:Class declarations must not appear in the dual-type list."""
        g = Graph()
        g.add((URIRef(f"{BASE}Patient"), RDF.type, OWL.Class))
        assert check_duplicate_property_types(g) == []

    def test_empty_graph_returns_empty(self):
        assert check_duplicate_property_types(Graph()) == []


# ---------------------------------------------------------------------------
# Issue 3: merge() domain/range warnings for new properties
# ---------------------------------------------------------------------------

class TestMergeDomainRangeWarnings:
    def test_missing_domain_raises_warning(self):
        acc = Graph()
        frag = Graph()
        prop = URIRef(f"{BASE}hasValue")
        frag.add((prop, RDF.type, OWL.DatatypeProperty))
        frag.add((prop, RDFS.range, URIRef("http://www.w3.org/2001/XMLSchema#string")))
        # No domain added
        result = merge(acc, frag, "raw_events", BASE)
        assert any("domain" in w.lower() for w in result.domain_range_warnings)

    def test_missing_range_raises_warning(self):
        acc = Graph()
        frag = Graph()
        prop = URIRef(f"{BASE}hasValue")
        frag.add((prop, RDF.type, OWL.DatatypeProperty))
        frag.add((prop, RDFS.domain, URIRef(f"{BASE}Event")))
        # No range added
        result = merge(acc, frag, "raw_events", BASE)
        assert any("range" in w.lower() for w in result.domain_range_warnings)

    def test_complete_property_no_warning(self):
        acc = Graph()
        frag = Graph()
        prop = URIRef(f"{BASE}hasValue")
        frag.add((prop, RDF.type, OWL.DatatypeProperty))
        frag.add((prop, RDFS.domain, URIRef(f"{BASE}Event")))
        frag.add((prop, RDFS.range, URIRef("http://www.w3.org/2001/XMLSchema#string")))
        result = merge(acc, frag, "raw_events", BASE)
        assert result.domain_range_warnings == []

    def test_already_existing_property_not_warned(self):
        """domain/range warnings apply only to NEW properties being merged in."""
        acc = Graph()
        prop = URIRef(f"{BASE}hasValue")
        acc.add((prop, RDF.type, OWL.DatatypeProperty))
        frag = Graph()
        frag.add((prop, RDF.type, OWL.DatatypeProperty))
        # No domain/range on duplicate — must not warn (it's not newly added)
        result = merge(acc, frag, "raw_events", BASE)
        assert result.domain_range_warnings == []

    def test_object_property_missing_domain_warned(self):
        acc = Graph()
        frag = Graph()
        prop = URIRef(f"{BASE}hasPatient")
        frag.add((prop, RDF.type, OWL.ObjectProperty))
        frag.add((prop, RDFS.range, URIRef(f"{BASE}Patient")))
        result = merge(acc, frag, "raw_events", BASE)
        assert any("domain" in w.lower() for w in result.domain_range_warnings)


# ---------------------------------------------------------------------------
# Issue 4: judge rules 5-7 and SubPropertyOf parser support
# ---------------------------------------------------------------------------

class TestJudgeRulesDomainRangeDualType:
    def _build_user_prompt(self):
        table_ctx = _minimal_table_ctx()
        fragment = _minimal_fragment()
        _, user_prompt = build_judge_prompt(
            table_ctx=table_ctx,
            fragment=fragment,
            accumulated_classes=[],
            base_iri=BASE,
        )
        return user_prompt

    def test_judge_prompt_contains_domain_rule(self):
        prompt = self._build_user_prompt()
        assert "DOMAIN" in prompt
        assert "rdfs:domain" in prompt

    def test_judge_prompt_contains_range_rule(self):
        prompt = self._build_user_prompt()
        assert "RANGE" in prompt
        assert "rdfs:range" in prompt

    def test_judge_prompt_contains_dual_type_rule(self):
        prompt = self._build_user_prompt()
        assert "DUAL-TYPE" in prompt
        assert "ObjectProperty" in prompt
        assert "DatatypeProperty" in prompt

    def test_judge_domain_rule_says_hard_rejection(self):
        prompt = self._build_user_prompt()
        # Must be labelled as hard rejection, not just advisory
        assert "HARD REJECTION" in prompt or "hard rejection" in prompt.lower()


class TestSubPropertyOfParsing:
    def test_subproperty_parsed_for_object_property(self):
        """SubPropertyOf: in an ObjectProperty block must add rdfs:subPropertyOf triple."""
        manchester = (
            f"ObjectProperty: <{BASE}hasLabeventValue>\n"
            f"    Domain: <{BASE}Labevent>\n"
            f"    Range: <http://www.w3.org/2001/XMLSchema#string>\n"
            f"    SubPropertyOf: <{BASE}hasValue>\n"
        )
        g, _, props = parse_manchester(manchester, BASE, "test")
        prop = URIRef(f"{BASE}hasLabeventValue")
        parent = URIRef(f"{BASE}hasValue")
        assert (prop, RDFS.subPropertyOf, parent) in g

    def test_subproperty_parsed_for_datatype_property(self):
        """SubPropertyOf: in a DatatypeProperty block must add rdfs:subPropertyOf triple."""
        manchester = (
            f"DatatypeProperty: <{BASE}hasCharteventValue>\n"
            f"    Domain: <{BASE}Chartevent>\n"
            f"    Range: <http://www.w3.org/2001/XMLSchema#string>\n"
            f"    SubPropertyOf: <{BASE}hasValue>\n"
        )
        g, _, props = parse_manchester(manchester, BASE, "test")
        prop = URIRef(f"{BASE}hasCharteventValue")
        parent = URIRef(f"{BASE}hasValue")
        assert (prop, RDFS.subPropertyOf, parent) in g

    def test_no_subproperty_when_absent(self):
        manchester = (
            f"DatatypeProperty: <{BASE}hasCode>\n"
            f"    Domain: <{BASE}Event>\n"
            f"    Range: <http://www.w3.org/2001/XMLSchema#string>\n"
        )
        g, _, _ = parse_manchester(manchester, BASE, "test")
        prop = URIRef(f"{BASE}hasCode")
        assert list(g.objects(prop, RDFS.subPropertyOf)) == []

    def test_subproperty_coexists_with_domain_range(self):
        """SubPropertyOf must not interfere with normal domain/range parsing."""
        manchester = (
            f"ObjectProperty: <{BASE}hasLabeventValue>\n"
            f"    Domain: <{BASE}Labevent>\n"
            f"    Range: <{BASE}ValueClass>\n"
            f"    SubPropertyOf: <{BASE}hasValue>\n"
        )
        g, _, _ = parse_manchester(manchester, BASE, "test")
        prop = URIRef(f"{BASE}hasLabeventValue")
        assert list(g.objects(prop, RDFS.domain)) == [URIRef(f"{BASE}Labevent")]
        assert list(g.objects(prop, RDFS.range)) == [URIRef(f"{BASE}ValueClass")]
        assert (prop, RDFS.subPropertyOf, URIRef(f"{BASE}hasValue")) in g
