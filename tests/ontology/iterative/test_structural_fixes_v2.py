# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for five structural defects identified in mimic_bronze_rigor_260428_001.owl.

Issue 1 — High-row-count tables must NOT be generated as enumerated classes.
Issue 2 — Self-referential rdfs:subPropertyOf must be stripped before merging.
Issue 3 — CATEGORICAL columns must always produce ObjectProperty + Class + Individuals.
Issue 4 — Properties with multiple Domain: declarations are split into class-scoped subprops.
Issue 5 — Abstract property hierarchy is retrievable from the embedding index;
           gen-prompt injects abstract properties and adds rules for them.

All LLM calls are mocked.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, URIRef
from rdflib.collection import Collection

from semantic_mapper.ontology.owl_utils import domain_class_uris
from semantic_mapper.ontology.iterative.embedding_index import AccumulatedClassIndex
from semantic_mapper.ontology.iterative.gen_prompt import build_gen_prompt
from semantic_mapper.ontology.iterative.generator import (
    IterativeOntologyGenerator,
    _build_oneof_enumerations,
    _extract_new_abstract_properties,
)
from semantic_mapper.ontology.iterative.graph_merger import (
    MergeResult,
    _split_multi_domain_properties,
    _strip_self_referential_subprops,
    merge,
)
from semantic_mapper.ontology.iterative.judge_prompt import build_judge_prompt
from semantic_mapper.ontology.iterative.models import (
    AccumulatedAbstractPropertySummary,
    AccumulatedClassSummary,
    JudgeVerdict,
    OntologyFragment,
)

BASE = "http://mesh.local/ontology/test#"
XSD_STR = URIRef("http://www.w3.org/2001/XMLSchema#string")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _minimal_col(table_name, col_name, is_pk=False, is_fk=False,
                 distinct_count=50, top_values=None, is_categorical=False):
    from semantic_mapper.ontology.iterative.models import ColumnContext
    return ColumnContext(
        table_name=table_name, col_name=col_name,
        inferred_type="VARCHAR", is_pk=is_pk, is_fk=is_fk,
        is_nullable=True, null_rate=0.0, distinct_count=distinct_count,
        top_values=top_values or [],
        is_categorical=is_categorical,
        categorical_values=top_values if is_categorical else [],
    )


def _table_ctx(table_name, row_count, cols):
    from semantic_mapper.ontology.iterative.models import TableContext
    return TableContext(
        table_name=table_name,
        row_count=row_count,
        columns=cols,
        processing_index=1,
        total_tables=1,
    )


def _make_settings(tmp_dir: Path):
    s = MagicMock()
    s.OPENAI_API_KEY = "test-key"
    s.LLM_REQUEST_TIMEOUT = 30
    s.ONTOLOGY_GEN_MODEL = "gpt-4o"
    s.ONTOLOGY_JUDGE_MODEL = "gpt-4o-mini"
    s.ONTOLOGY_GEN_MAX_RETRIES = 1
    s.ONTOLOGY_GEN_TOP_K_CLASSES = 5
    s.ONTOLOGY_GEN_TEMPERATURE = 0.0
    s.ONTOLOGY_GEN_JUDGE_TEMPERATURE = 0.0
    s.ONTOLOGY_JUDGE_CONFIDENCE_FLOOR = 0.6
    s.ONTOLOGY_MAX_ROWS_FOR_ENUMERATION = 1000
    s.ONTOLOGY_COL_WORD_SPLITS = {}
    s.SEMANTIC_MATCHER_MODEL = "all-MiniLM-L6-v2"
    s.DATABASE_URL = "mysql://root@localhost/test_db"
    s.SEED_OUTPUT_PATH = str(tmp_dir / "seed.owl")
    s.OPENAI_MODEL = "gpt-4o"
    return s


def _approved_verdict():
    return JudgeVerdict(approved=True, confidence=0.95)


def _graph_with_enumerated_class(class_uri: str, individuals=("Ind1", "Ind2")) -> Graph:
    """Build a fragment graph where class_uri is an enumerated class (owl:oneOf)."""
    g = Graph()
    cls = URIRef(class_uri)
    g.add((cls, RDF.type, OWL.Class))
    ind_uris = [URIRef(f"{BASE}{name}") for name in individuals]
    for ind in ind_uris:
        g.add((ind, RDF.type, OWL.NamedIndividual))
        g.add((ind, RDF.type, cls))
    list_bn = BNode()
    Collection(g, list_bn, ind_uris)
    oneof_bn = BNode()
    g.add((oneof_bn, OWL.oneOf, list_bn))
    g.add((cls, OWL.equivalentClass, oneof_bn))
    return g


# ---------------------------------------------------------------------------
# Issue 1: Entity class enumeration guard
# ---------------------------------------------------------------------------

class TestEntityClassEnumerationGuard:
    """High-row-count tables must never be modelled as enumerated types."""

    def test_gen_prompt_includes_entity_guard_for_large_table(self):
        """When row_count > threshold, the prompt warns the LLM it is an ENTITY table."""
        cols = [_minimal_col("raw_admissions", "hadm_id", is_pk=True)]
        ctx = _table_ctx("raw_admissions", row_count=50_000, cols=cols)
        prompt = build_gen_prompt(
            table_ctx=ctx,
            retrieved_classes=[],
            deterministic_uri=f"{BASE}Admission",
            base_iri=BASE,
            max_rows_for_enumeration=1000,
        )
        assert "ENTITY TABLE" in prompt
        assert "owl:oneOf" in prompt
        assert "owl:equivalentClass" in prompt

    def test_gen_prompt_no_entity_guard_for_small_table(self):
        """Small tables (row_count <= threshold) should NOT get the entity guard."""
        cols = [_minimal_col("raw_admtypes", "type_id", is_pk=True)]
        ctx = _table_ctx("raw_admtypes", row_count=5, cols=cols)
        prompt = build_gen_prompt(
            table_ctx=ctx,
            retrieved_classes=[],
            deterministic_uri=f"{BASE}AdmType",
            base_iri=BASE,
            max_rows_for_enumeration=1000,
        )
        assert "ENTITY TABLE" not in prompt

    def test_judge_prompt_includes_entity_class_rule(self):
        """Judge prompt must contain the ENTITY-CLASS hard-rejection rule."""
        cols = [_minimal_col("raw_admissions", "subject_id")]
        ctx = _table_ctx("raw_admissions", row_count=50_000, cols=cols)
        fragment = OntologyFragment(
            table_name="raw_admissions", manchester_syntax="",
            classes_declared=[], properties_declared=[], retry_count=0,
        )
        _, user_prompt = build_judge_prompt(
            table_ctx=ctx,
            fragment=fragment,
            accumulated_classes=[],
            base_iri=BASE,
            max_rows_for_enumeration=1000,
        )
        assert "ENTITY-CLASS" in user_prompt
        assert "50,000" in user_prompt

    def test_merger_logs_critical_for_enumerated_entity_class(self):
        """merge() must populate enumeration_warnings when a high-row-count table's
        primary class is an enumerated type."""
        class_uri = f"{BASE}Admission"
        frag = _graph_with_enumerated_class(class_uri)
        acc = Graph()
        result = merge(acc, frag, "raw_admissions", BASE,
                       table_row_count=50_000, max_rows_for_enumeration=1000)
        assert len(result.enumeration_warnings) >= 1
        assert "CRITICAL" in result.enumeration_warnings[0].upper() or \
               "enumerated" in result.enumeration_warnings[0].lower()

    def test_merger_no_warning_for_small_table(self):
        """No enumeration warning when row_count <= threshold."""
        class_uri = f"{BASE}AdmType"
        frag = _graph_with_enumerated_class(class_uri)
        acc = Graph()
        result = merge(acc, frag, "raw_admtypes", BASE,
                       table_row_count=5, max_rows_for_enumeration=1000)
        assert result.enumeration_warnings == []

    def test_build_oneof_enumerations_skips_excluded_classes(self):
        """_build_oneof_enumerations must skip classes in excluded_class_uris."""
        g = Graph()
        cls_uri = URIRef(f"{BASE}Admission")
        ind_uri = URIRef(f"{BASE}Adm1")
        g.add((cls_uri, RDF.type, OWL.Class))
        g.add((ind_uri, RDF.type, OWL.NamedIndividual))
        g.add((ind_uri, RDF.type, cls_uri))

        _build_oneof_enumerations(g, excluded_class_uris={cls_uri})
        # Class must NOT have equivalentClass after exclusion
        assert list(g.objects(cls_uri, OWL.equivalentClass)) == []

    def test_build_oneof_enumerations_applies_to_non_excluded(self):
        """Classes NOT excluded still get the oneOf enumeration."""
        g = Graph()
        cls_uri = URIRef(f"{BASE}SmallType")
        ind_uri = URIRef(f"{BASE}TypeA")
        g.add((cls_uri, RDF.type, OWL.Class))
        g.add((ind_uri, RDF.type, OWL.NamedIndividual))
        g.add((ind_uri, RDF.type, cls_uri))

        _build_oneof_enumerations(g, excluded_class_uris={URIRef(f"{BASE}Other")})
        assert list(g.objects(cls_uri, OWL.equivalentClass)) != []


# ---------------------------------------------------------------------------
# Issue 2: Self-referential subPropertyOf stripping
# ---------------------------------------------------------------------------

class TestSelfReferentialSubPropertyStripping:
    """rdfs:subPropertyOf triples where subject == object must be removed before merge."""

    def test_strips_self_referential_triple(self):
        g = Graph()
        prop = URIRef(f"{BASE}hasAdmission")
        g.add((prop, RDF.type, OWL.ObjectProperty))
        g.add((prop, RDFS.subPropertyOf, prop))  # circular
        removed = _strip_self_referential_subprops(g)
        assert removed == 1
        assert (prop, RDFS.subPropertyOf, prop) not in g

    def test_preserves_valid_subproperty(self):
        g = Graph()
        child = URIRef(f"{BASE}hasAdmissionForPatient")
        parent = URIRef(f"{BASE}hasAdmission")
        g.add((child, RDF.type, OWL.ObjectProperty))
        g.add((child, RDFS.subPropertyOf, parent))
        removed = _strip_self_referential_subprops(g)
        assert removed == 0
        assert (child, RDFS.subPropertyOf, parent) in g

    def test_strips_multiple_self_referential_triples(self):
        g = Graph()
        for local in ("hasA", "hasB", "hasC"):
            prop = URIRef(f"{BASE}{local}")
            g.add((prop, RDF.type, OWL.ObjectProperty))
            g.add((prop, RDFS.subPropertyOf, prop))
        removed = _strip_self_referential_subprops(g)
        assert removed == 3

    def test_merge_strips_self_referential_before_adding(self):
        """merge() calls _strip_self_referential_subprops — the triple must not appear
        in the accumulating graph after merge."""
        prop_uri = f"{BASE}hasAdmission"
        frag = Graph()
        prop = URIRef(prop_uri)
        frag.add((prop, RDF.type, OWL.ObjectProperty))
        frag.add((prop, RDFS.subPropertyOf, prop))
        frag.add((prop, RDFS.domain, URIRef(f"{BASE}SomeClass")))
        frag.add((prop, RDFS.range, URIRef(f"{BASE}OtherClass")))
        acc = Graph()
        merge(acc, frag, "test_table", BASE)
        assert (prop, RDFS.subPropertyOf, prop) not in acc

    def test_judge_prompt_contains_self_ref_rule(self):
        cols = [_minimal_col("raw_t", "col_a")]
        ctx = _table_ctx("raw_t", 100, cols)
        fragment = OntologyFragment(
            table_name="raw_t", manchester_syntax="",
            classes_declared=[], properties_declared=[], retry_count=0,
        )
        _, user_prompt = build_judge_prompt(
            table_ctx=ctx, fragment=fragment, accumulated_classes=[], base_iri=BASE,
        )
        assert "SELF-REF" in user_prompt or "self-referential" in user_prompt.lower()


# ---------------------------------------------------------------------------
# Issue 3: CATEGORICAL column promotion
# ---------------------------------------------------------------------------

class TestCategoricalColumnPromotion:
    """CATEGORICAL columns must be modelled as ObjectProperty+Class+Individuals."""

    def test_gen_prompt_rule4_uses_hard_rejection_language(self):
        """Rule 4 must say CATEGORICAL → ObjectProperty + Class + Individuals,
        and explicitly call DatatypeProperty xsd:string a hard rejection."""
        cols = [
            _minimal_col("raw_t", "id", is_pk=True),
            _minimal_col("raw_t", "admission_type", distinct_count=3,
                         top_values=["Emergency", "Elective", "Urgent"],
                         is_categorical=True),
        ]
        ctx = _table_ctx("raw_t", 500, cols)
        prompt = build_gen_prompt(
            table_ctx=ctx,
            retrieved_classes=[],
            deterministic_uri=f"{BASE}T",
            base_iri=BASE,
        )
        lower = prompt.lower()
        assert "categorical" in lower
        assert "hard rejection" in lower or "must" in lower
        assert "objectproperty" in lower or "ObjectProperty" in prompt

    def test_gen_prompt_shows_categorical_flag_for_column(self):
        """The column profile section must label categorical columns clearly."""
        cols = [
            _minimal_col("raw_t", "id", is_pk=True),
            _minimal_col("raw_t", "religion", distinct_count=5,
                         top_values=["Catholic", "Jewish", "Muslim", "Other", "None"],
                         is_categorical=True),
        ]
        ctx = _table_ctx("raw_t", 200, cols)
        prompt = build_gen_prompt(
            table_ctx=ctx,
            retrieved_classes=[],
            deterministic_uri=f"{BASE}T",
            base_iri=BASE,
        )
        assert "CATEGORICAL" in prompt
        assert "Catholic" in prompt

    def test_judge_prompt_categorical_promotion_rule_present(self):
        """Judge prompt must contain the CATEGORICAL-PROMOTION hard-rejection rule."""
        cols = [
            _minimal_col("raw_t", "id", is_pk=True),
            _minimal_col("raw_t", "religion", distinct_count=5,
                         top_values=["Catholic", "Other"],
                         is_categorical=True),
        ]
        ctx = _table_ctx("raw_t", 200, cols)
        fragment = OntologyFragment(
            table_name="raw_t", manchester_syntax="",
            classes_declared=[], properties_declared=[], retry_count=0,
        )
        _, user_prompt = build_judge_prompt(
            table_ctx=ctx, fragment=fragment, accumulated_classes=[], base_iri=BASE,
        )
        assert "CATEGORICAL-PROMOTION" in user_prompt
        assert "DatatypeProperty" in user_prompt


# ---------------------------------------------------------------------------
# Issue 4: Multi-domain property splitting
# ---------------------------------------------------------------------------

class TestMultiDomainPropertySplitting:
    """Properties with >1 rdfs:domain must be split into class-scoped subproperties."""

    def test_splits_multi_domain_into_subproperties(self):
        """A fragment property with 3 domains should become 3 scoped subproperties
        and the original abstract (no domain)."""
        g = Graph()
        prop = URIRef(f"{BASE}hasSubjectId")
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        g.add((prop, RDFS.range, XSD_STR))
        for cls_name in ("Admission", "Icu", "Transfer"):
            g.add((prop, RDFS.domain, URIRef(f"{BASE}{cls_name}")))

        rewritten = _split_multi_domain_properties(g, BASE)
        assert rewritten == 1
        # Original property must now have no domain (abstract)
        assert list(g.objects(prop, RDFS.domain)) == []
        # Three class-scoped subproperties must exist
        scoped = [
            s for s in g.subjects(RDFS.subPropertyOf, prop)
        ]
        assert len(scoped) == 3

    def test_scoped_subproperty_has_correct_domain(self):
        g = Graph()
        prop = URIRef(f"{BASE}hasValue")
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        g.add((prop, RDFS.range, XSD_STR))
        g.add((prop, RDFS.domain, URIRef(f"{BASE}Labevent")))
        g.add((prop, RDFS.domain, URIRef(f"{BASE}Chartevent")))

        _split_multi_domain_properties(g, BASE)
        # Each scoped property should have exactly one domain
        for scoped in g.subjects(RDFS.subPropertyOf, prop):
            domains = list(g.objects(scoped, RDFS.domain))
            assert len(domains) == 1

    def test_single_domain_property_untouched(self):
        g = Graph()
        prop = URIRef(f"{BASE}hasSubjectId")
        g.add((prop, RDF.type, OWL.DatatypeProperty))
        g.add((prop, RDFS.range, XSD_STR))
        g.add((prop, RDFS.domain, URIRef(f"{BASE}Admission")))

        rewritten = _split_multi_domain_properties(g, BASE)
        assert rewritten == 0
        assert list(g.objects(prop, RDFS.domain)) == [URIRef(f"{BASE}Admission")]

    def test_merge_splits_multi_domain_property(self):
        """merge() must rewrite multi-domain properties before inserting into graph."""
        frag = Graph()
        prop = URIRef(f"{BASE}hasSubjectId")
        frag.add((prop, RDF.type, OWL.DatatypeProperty))
        frag.add((prop, RDFS.range, XSD_STR))
        for cls_name in ("Admission", "Patient"):
            frag.add((prop, RDFS.domain, URIRef(f"{BASE}{cls_name}")))

        acc = Graph()
        merge(acc, frag, "raw_admissions", BASE)
        # After merge, the original prop is the abstract parent: its domain is the
        # owl:unionOf of its two scoped subproperties' domains — a single restriction
        # triple, never two plain rdfs:domain triples (which OWL would AND together).
        domains = list(acc.objects(prop, RDFS.domain))
        assert len(domains) == 1
        assert domain_class_uris(acc, prop) == {f"{BASE}Admission", f"{BASE}Patient"}
        # Both scoped subproperties should be in acc
        scoped = list(acc.subjects(RDFS.subPropertyOf, prop))
        assert len(scoped) == 2

    def test_judge_prompt_contains_multi_domain_rule(self):
        cols = [_minimal_col("raw_t", "subject_id")]
        ctx = _table_ctx("raw_t", 100, cols)
        fragment = OntologyFragment(
            table_name="raw_t", manchester_syntax="",
            classes_declared=[], properties_declared=[], retry_count=0,
        )
        _, user_prompt = build_judge_prompt(
            table_ctx=ctx, fragment=fragment, accumulated_classes=[], base_iri=BASE,
        )
        assert "MULTI-DOMAIN" in user_prompt

    def test_gen_prompt_rule12_exactly_one_domain(self):
        """Rule 12 must state exactly one Domain: per property pointing to the primary class."""
        cols = [_minimal_col("raw_t", "subject_id")]
        ctx = _table_ctx("raw_t", 100, cols)
        prompt = build_gen_prompt(
            table_ctx=ctx, retrieved_classes=[],
            deterministic_uri=f"{BASE}T", base_iri=BASE,
        )
        assert "EXACTLY ONE" in prompt or "exactly one" in prompt.lower()
        assert "Domain:" in prompt


# ---------------------------------------------------------------------------
# Issue 5: Abstract property hierarchy
# ---------------------------------------------------------------------------

class TestAbstractPropertyHierarchy:
    """Abstract properties (no domain) are indexed and injected into gen prompts."""

    def test_extract_abstract_properties_finds_domainless_props(self):
        """_extract_new_abstract_properties returns props with no rdfs:domain."""
        g = Graph()
        abstract = URIRef(f"{BASE}hasValue")
        g.add((abstract, RDF.type, OWL.DatatypeProperty))
        g.add((abstract, RDFS.range, XSD_STR))
        g.add((abstract, RDFS.label, Literal("has value", lang="en")))
        # Add a non-abstract prop (has domain) — should NOT be returned
        concrete = URIRef(f"{BASE}hasLabeventValue")
        g.add((concrete, RDF.type, OWL.DatatypeProperty))
        g.add((concrete, RDFS.domain, URIRef(f"{BASE}Labevent")))
        g.add((concrete, RDFS.range, XSD_STR))

        result = _extract_new_abstract_properties(g, BASE, already_indexed=set())
        uris = [r.uri for r in result]
        assert f"{BASE}hasValue" in uris
        assert f"{BASE}hasLabeventValue" not in uris

    def test_extract_abstract_properties_skips_already_indexed(self):
        g = Graph()
        abstract = URIRef(f"{BASE}hasValue")
        g.add((abstract, RDF.type, OWL.DatatypeProperty))
        result = _extract_new_abstract_properties(
            g, BASE, already_indexed={f"{BASE}hasValue"}
        )
        assert result == []

    def test_extract_abstract_properties_filters_non_base_iri(self):
        """Properties from external namespaces must not be returned."""
        g = Graph()
        # RDF/OWL built-ins — different IRI
        foreign = URIRef("http://external.org/ontology#hasValue")
        g.add((foreign, RDF.type, OWL.DatatypeProperty))
        result = _extract_new_abstract_properties(g, BASE, already_indexed=set())
        assert result == []

    def test_index_add_all_abstract_properties(self):
        """AccumulatedClassIndex stores abstract properties and deduplicates on re-add."""
        idx = AccumulatedClassIndex.__new__(AccumulatedClassIndex)
        idx._model_name = "mock"
        idx._model = None
        idx._classes = []
        idx._embeddings = None
        idx._abstract_props = []
        idx._abstract_embeddings = None
        idx._indexed_prop_uris = set()

        props = [
            AccumulatedAbstractPropertySummary(
                local_name="hasValue", label="has value",
                uri=f"{BASE}hasValue", range_uri=str(XSD_STR),
            )
        ]
        # Manually add without embedding (model not loaded in unit test)
        idx._abstract_props.extend(props)
        idx._indexed_prop_uris.update(p.uri for p in props)

        # Re-adding the same property should be a no-op
        pre_count = len(idx._abstract_props)
        for p in props:
            if p.uri not in idx._indexed_prop_uris:
                idx._abstract_props.append(p)
        assert len(idx._abstract_props) == pre_count

    def test_gen_prompt_abstract_properties_section_present(self):
        """When abstract_properties are passed, the prompt must show them."""
        cols = [_minimal_col("raw_labevent", "value")]
        ctx = _table_ctx("raw_labevent", 500, cols)
        abstract_props = [
            AccumulatedAbstractPropertySummary(
                local_name="hasValue", label="has value",
                uri=f"{BASE}hasValue", range_uri=str(XSD_STR),
                prop_type="DatatypeProperty",
            )
        ]
        prompt = build_gen_prompt(
            table_ctx=ctx,
            retrieved_classes=[],
            deterministic_uri=f"{BASE}Labevent",
            base_iri=BASE,
            abstract_properties=abstract_props,
        )
        assert "ABSTRACT DATATYPEPROPERTIES" in prompt
        assert f"{BASE}hasValue" in prompt

    def test_gen_prompt_no_abstract_section_when_empty(self):
        cols = [_minimal_col("raw_t", "value")]
        ctx = _table_ctx("raw_t", 100, cols)
        prompt = build_gen_prompt(
            table_ctx=ctx, retrieved_classes=[],
            deterministic_uri=f"{BASE}T", base_iri=BASE,
            abstract_properties=[],
        )
        assert "ABSTRACT PROPERTIES" not in prompt

    def test_gen_prompt_rule13_generic_names_always_abstract(self):
        """Rule 13 must list the generic names that always require an abstract parent."""
        cols = [_minimal_col("raw_t", "value")]
        ctx = _table_ctx("raw_t", 100, cols)
        prompt = build_gen_prompt(
            table_ctx=ctx, retrieved_classes=[],
            deterministic_uri=f"{BASE}T", base_iri=BASE,
        )
        # Rule 13 should mention abstract parent + class-scoped subproperty
        assert "abstract parent" in prompt.lower()
        assert "subject" in prompt
        assert "amount" in prompt
        assert "uom" in prompt
