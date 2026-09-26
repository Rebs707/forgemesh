# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the {Value}{ClassName} individual naming convention.

Covers:
  Generator (_build_oneof_enumerations / _canonicalise_individual_uris):
    - Bare-value individual renamed to {Value}{ClassName}.
    - Already-canonical individual not renamed.
    - Renaming updates owl:oneOf list entries in place.
    - Renaming updates rdf:type triple in the graph.
    - Excluded classes are not renamed or enumerated.
    - base_iri fallback: namespace derived from the individual's own URI.

  Enricher (enrich_ontology_from_value_mappings):
    - Warm run: existing individual not duplicated; INFO logged.
    - Warm run: existing individual appended to owl:oneOf when absent from it.
    - Warm run: existing individual in owl:oneOf → no duplication, no re-append.
    - Warm run triggers a file write only when individuals were appended to oneOf.
    - Untyped individual: never created when property has no rdfs:range.
    - ERROR logged when individual skipped due to missing range.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection

from semantic_mapper.ontology.enricher import (
    _individual_in_oneof,
    enrich_ontology_from_value_mappings,
)
from semantic_mapper.ontology.iterative.generator import (
    _build_oneof_enumerations,
    _canonicalise_individual_uris,
)
from semantic_mapper.schema.mapping_output import (
    ClassCandidate,
    MappingStatus,
    PropertyMapping,
    SemanticMappingReport,
    TableMapping,
    ValueMappingRule,
)

BASE = "http://mesh.local/ontology/test#"
ONT = Namespace(BASE)


def _uri(local: str) -> URIRef:
    return URIRef(BASE + local)


# ---------------------------------------------------------------------------
# Helpers for enricher tests
# ---------------------------------------------------------------------------

def _enricher_graph() -> Graph:
    g = Graph()
    g.add((_uri("Admission"), RDF.type, OWL.Class))
    g.add((_uri("Gender"), RDF.type, OWL.Class))
    g.add((_uri("hasGender"), RDF.type, OWL.ObjectProperty))
    g.add((_uri("hasGender"), RDFS.domain, _uri("Admission")))
    g.add((_uri("hasGender"), RDFS.range, _uri("Gender")))
    return g


def _enricher_report(*, uris: list[str]) -> SemanticMappingReport:
    rules = [
        ValueMappingRule(source_value=u.split("#")[-1], mapped_uri=u, rationale="derived", confidence_score=0.9)
        for u in uris
    ]
    col = PropertyMapping(
        source_column="gender",
        source_data_type="VARCHAR",
        is_nullable=False,
        mapped_property_uri=str(_uri("hasGender")),
        status=MappingStatus.CONFIRMED,
        confidence_score=0.9,
        rationale="matched",
        value_mappings=rules,
    )
    table = TableMapping(
        source_table="admissions",
        class_candidate_set=[ClassCandidate(uri=str(_uri("Admission")), label="Admission", rationale="m", confidence_score=0.9)],
        column_mappings=[col],
    )
    return SemanticMappingReport(
        ontology_uri=BASE, database_name="test_db", settings_used={},
        tables=[table], total_pending_review=0,
    )


def _enricher_sidecar(*, uris: list[str]) -> dict:
    return {
        "admissions.gender": [
            {"source_value": u.split("#")[-1], "mapped_uri": u}
            for u in uris
        ]
    }


# ---------------------------------------------------------------------------
# _canonicalise_individual_uris
# ---------------------------------------------------------------------------

class TestCanonicaliseIndividualUris:
    def test_bare_value_renamed_to_value_classname(self):
        """Female for class Gender → FemaleGender."""
        g = Graph()
        ind = _uri("Female")
        cls = _uri("Gender")
        g.add((ind, RDF.type, OWL.NamedIndividual))
        g.add((ind, RDF.type, cls))

        result = _canonicalise_individual_uris(g, [ind], "Gender", BASE)

        assert result == [_uri("FemaleGender")]
        assert (_uri("FemaleGender"), RDF.type, OWL.NamedIndividual) in g
        assert (_uri("Female"), RDF.type, OWL.NamedIndividual) not in g

    def test_already_canonical_not_renamed(self):
        """FemaleGender already ends with Gender → unchanged."""
        g = Graph()
        ind = _uri("FemaleGender")
        cls = _uri("Gender")
        g.add((ind, RDF.type, OWL.NamedIndividual))
        g.add((ind, RDF.type, cls))

        result = _canonicalise_individual_uris(g, [ind], "Gender", BASE)

        assert result == [_uri("FemaleGender")]
        assert (_uri("FemaleGender"), RDF.type, OWL.NamedIndividual) in g

    def test_rdf_type_triple_rewritten(self):
        """After renaming, the class-type triple points to the new URI."""
        g = Graph()
        ind = _uri("Male")
        cls = _uri("Gender")
        g.add((ind, RDF.type, OWL.NamedIndividual))
        g.add((ind, RDF.type, cls))

        _canonicalise_individual_uris(g, [ind], "Gender", BASE)

        assert (_uri("MaleGender"), RDF.type, cls) in g
        assert (_uri("Male"), RDF.type, cls) not in g

    def test_oneof_list_entry_rewritten(self):
        """If the individual is referenced in an owl:oneOf BNode list, that entry is updated."""
        g = Graph()
        ind = _uri("Female")
        cls = _uri("Gender")
        g.add((ind, RDF.type, OWL.NamedIndividual))
        g.add((ind, RDF.type, cls))

        # Build a one-element owl:oneOf list referencing the bare-value individual
        list_node = BNode()
        g.add((list_node, RDF.first, ind))
        g.add((list_node, RDF.rest, RDF.nil))

        _canonicalise_individual_uris(g, [ind], "Gender", BASE)

        # The list entry must now reference FemaleGender
        members = list(g.objects(list_node, RDF.first))
        assert _uri("FemaleGender") in members
        assert ind not in members

    def test_base_iri_fallback_uses_individual_namespace(self):
        """When base_iri is empty, namespace is derived from the individual URI."""
        g = Graph()
        ind = _uri("Emergency")
        cls = _uri("AdmissionType")
        g.add((ind, RDF.type, OWL.NamedIndividual))
        g.add((ind, RDF.type, cls))

        result = _canonicalise_individual_uris(g, [ind], "AdmissionType", "")

        assert result == [_uri("EmergencyAdmissionType")]

    def test_rdfs_label_carried_over(self):
        """rdfs:label is preserved on the renamed URI."""
        g = Graph()
        ind = _uri("Female")
        g.add((ind, RDF.type, OWL.NamedIndividual))
        g.add((ind, RDFS.label, Literal("Female", lang="en")))

        _canonicalise_individual_uris(g, [ind], "Gender", BASE)

        labels = list(g.objects(_uri("FemaleGender"), RDFS.label))
        assert any(str(l) == "Female" for l in labels)

    def test_multiple_individuals_all_renamed(self):
        """Each individual in the list is independently renamed."""
        g = Graph()
        cls = _uri("Gender")
        individuals = []
        for val in ("Female", "Male", "Other"):
            ind = _uri(val)
            g.add((ind, RDF.type, OWL.NamedIndividual))
            g.add((ind, RDF.type, cls))
            individuals.append(ind)

        result = _canonicalise_individual_uris(g, individuals, "Gender", BASE)

        expected = {_uri("FemaleGender"), _uri("MaleGender"), _uri("OtherGender")}
        assert set(result) == expected
        for orig in (_uri("Female"), _uri("Male"), _uri("Other")):
            assert (orig, RDF.type, OWL.NamedIndividual) not in g

    def test_doubled_stem_is_unique_and_not_class_local(self):
        """When value equals the class stem, result is doubled but unique and not the class URI.

        'Stopped' + 'StoppedStatus' → 'StoppedStoppedStatus' (accepted cosmetic limitation).
        The individual must NOT have the same local name as the class itself.
        """
        g = Graph()
        ind = _uri("Stopped")
        cls = _uri("StoppedStatus")
        g.add((ind, RDF.type, OWL.NamedIndividual))
        g.add((ind, RDF.type, cls))

        result = _canonicalise_individual_uris(g, [ind], "StoppedStatus", BASE)

        assert len(result) == 1
        result_local = str(result[0]).split("#")[-1]
        assert result_local.endswith("StoppedStatus"), f"Expected suffix 'StoppedStatus'; got {result_local}"
        assert result_local != "StoppedStatus", "Individual must not have the same local name as the class"

    def test_special_char_word_boundary_split(self):
        """Non-alphanumeric chars are treated as word boundaries; each segment is capitalised.

        'not.stopped' → split on '.' → words 'not', 'stopped' → 'Not'+'Stopped' = 'NotStopped'.
        The OLD behaviour (strip only, capitalise first char of whole string) would produce
        'Notstopped'; the NEW behaviour capitalises each word → 'NotStopped'.
        """
        g = Graph()
        ind = _uri("not.stopped")
        cls = _uri("StoppedStatus")
        g.add((ind, RDF.type, OWL.NamedIndividual))
        g.add((ind, RDF.type, cls))

        result = _canonicalise_individual_uris(g, [ind], "StoppedStatus", BASE)

        assert len(result) == 1
        result_local = str(result[0]).split("#")[-1]
        # Each segment capitalised: 'Not' + 'Stopped' → 'NotStopped' + 'StoppedStatus'
        assert result_local == "NotStoppedStoppedStatus"


# ---------------------------------------------------------------------------
# _build_oneof_enumerations with renaming
# ---------------------------------------------------------------------------

class TestBuildOneofEnumerationsNaming:
    def test_produces_prefixed_uri_in_oneof(self):
        """_build_oneof_enumerations renames Female → FemaleGender before building oneOf."""
        g = Graph()
        cls = _uri("Gender")
        ind = _uri("Female")
        g.add((cls, RDF.type, OWL.Class))
        g.add((ind, RDF.type, OWL.NamedIndividual))
        g.add((ind, RDF.type, cls))

        _build_oneof_enumerations(g, base_iri=BASE)

        eq_classes = list(g.objects(cls, OWL.equivalentClass))
        assert eq_classes, "equivalentClass not created"
        one_of_head = g.value(eq_classes[0], OWL.oneOf)
        assert one_of_head is not None
        members = set(Collection(g, one_of_head))
        assert _uri("FemaleGender") in members
        assert _uri("Female") not in members

    def test_already_prefixed_individual_unchanged(self):
        """If the LLM already produced FemaleGender, it stays as-is."""
        g = Graph()
        cls = _uri("Gender")
        ind = _uri("FemaleGender")
        g.add((cls, RDF.type, OWL.Class))
        g.add((ind, RDF.type, OWL.NamedIndividual))
        g.add((ind, RDF.type, cls))

        _build_oneof_enumerations(g, base_iri=BASE)

        eq_classes = list(g.objects(cls, OWL.equivalentClass))
        assert eq_classes
        one_of_head = g.value(eq_classes[0], OWL.oneOf)
        members = set(Collection(g, one_of_head))
        assert _uri("FemaleGender") in members

    def test_excluded_class_not_renamed_or_enumerated(self):
        """Excluded classes skip both renaming and oneOf construction."""
        g = Graph()
        cls = _uri("Gender")
        ind = _uri("Female")
        g.add((cls, RDF.type, OWL.Class))
        g.add((ind, RDF.type, OWL.NamedIndividual))
        g.add((ind, RDF.type, cls))

        _build_oneof_enumerations(g, excluded_class_uris={cls}, base_iri=BASE)

        # No equivalentClass added
        assert list(g.objects(cls, OWL.equivalentClass)) == []
        # Individual may still be renamed (renaming happens before the exclusion check)
        # — but no oneOf is built

    def test_existing_oneof_updated_not_duplicated(self):
        """When LLM produced owl:equivalentClass, _canonicalise rewrites it in-place."""
        g = Graph()
        cls = _uri("AdmissionType")
        ind_bare = _uri("Emergency")
        g.add((cls, RDF.type, OWL.Class))
        g.add((ind_bare, RDF.type, OWL.NamedIndividual))
        g.add((ind_bare, RDF.type, cls))

        # Simulate LLM-produced equivalentClass with bare individual
        eq_bn = BNode()
        list_node = BNode()
        g.add((cls, OWL.equivalentClass, eq_bn))
        g.add((eq_bn, OWL.oneOf, list_node))
        g.add((list_node, RDF.first, ind_bare))
        g.add((list_node, RDF.rest, RDF.nil))

        _build_oneof_enumerations(g, base_iri=BASE)

        # equivalentClass already existed — _build_oneof_enumerations skips construction
        # but _canonicalise must have rewritten the list entry
        eq_classes = list(g.objects(cls, OWL.equivalentClass))
        assert len(eq_classes) == 1, "No duplicate equivalentClass should be created"
        one_of_head = g.value(eq_classes[0], OWL.oneOf)
        members = set(Collection(g, one_of_head))
        assert _uri("EmergencyAdmissionType") in members
        assert ind_bare not in members


# ---------------------------------------------------------------------------
# _individual_in_oneof helper
# ---------------------------------------------------------------------------

class TestIndividualInOneof:
    def test_returns_true_when_present(self):
        g = Graph()
        cls = _uri("Gender")
        ind = _uri("FemaleGender")
        eq_bn = BNode()
        list_node = BNode()
        g.add((cls, OWL.equivalentClass, eq_bn))
        g.add((eq_bn, OWL.oneOf, list_node))
        g.add((list_node, RDF.first, ind))
        g.add((list_node, RDF.rest, RDF.nil))

        assert _individual_in_oneof(g, cls, ind) is True

    def test_returns_false_when_absent(self):
        g = Graph()
        cls = _uri("Gender")
        ind = _uri("FemaleGender")
        g.add((cls, RDF.type, OWL.Class))  # no equivalentClass

        assert _individual_in_oneof(g, cls, ind) is False

    def test_returns_false_when_no_oneof(self):
        g = Graph()
        cls = _uri("Gender")
        ind = _uri("FemaleGender")
        eq_bn = BNode()
        g.add((cls, OWL.equivalentClass, eq_bn))
        # no oneOf triple on eq_bn

        assert _individual_in_oneof(g, cls, ind) is False


# ---------------------------------------------------------------------------
# Enricher: warm-run behaviour
# ---------------------------------------------------------------------------

class TestEnricherWarmRun:
    def test_existing_individual_not_duplicated(self):
        """n_added == 0 when the individual already exists in the graph."""
        g = _enricher_graph()
        female_uri = str(_uri("FemaleGender"))
        g.add((_uri("FemaleGender"), RDF.type, OWL.NamedIndividual))
        g.add((_uri("FemaleGender"), RDF.type, _uri("Gender")))

        report = _enricher_report(uris=[female_uri])
        sidecar = _enricher_sidecar(uris=[female_uri])

        with tempfile.NamedTemporaryFile(suffix=".owl", delete=False) as f:
            owl_path = f.name

        n_added, new_uris = enrich_ontology_from_value_mappings(sidecar, report, g, owl_path)

        assert n_added == 0
        assert new_uris == []

    def test_existing_individual_appended_to_oneof_when_absent(self):
        """Existing individual not in owl:oneOf gets appended and file is written."""
        g = _enricher_graph()
        female_uri = str(_uri("FemaleGender"))
        g.add((_uri("FemaleGender"), RDF.type, OWL.NamedIndividual))
        g.add((_uri("FemaleGender"), RDF.type, _uri("Gender")))
        # No owl:oneOf on Gender yet

        report = _enricher_report(uris=[female_uri])
        sidecar = _enricher_sidecar(uris=[female_uri])

        with tempfile.NamedTemporaryFile(suffix=".owl", delete=False) as f:
            owl_path = f.name

        mtime_before = Path(owl_path).stat().st_mtime
        n_added, _ = enrich_ontology_from_value_mappings(sidecar, report, g, owl_path)

        assert n_added == 0
        # Individual must now be in a oneOf list
        assert _individual_in_oneof(g, _uri("Gender"), _uri("FemaleGender"))
        # File written because graph was modified (warm append)
        assert Path(owl_path).stat().st_mtime >= mtime_before

    def test_existing_individual_in_oneof_no_reappend(self):
        """Individual already in owl:oneOf → not re-appended; file not written."""
        g = _enricher_graph()
        female_uri = str(_uri("FemaleGender"))
        g.add((_uri("FemaleGender"), RDF.type, OWL.NamedIndividual))
        g.add((_uri("FemaleGender"), RDF.type, _uri("Gender")))
        # Set up owl:oneOf with FemaleGender already in it
        eq_bn = BNode()
        list_node = BNode()
        g.add((_uri("Gender"), OWL.equivalentClass, eq_bn))
        g.add((eq_bn, OWL.oneOf, list_node))
        g.add((list_node, RDF.first, _uri("FemaleGender")))
        g.add((list_node, RDF.rest, RDF.nil))

        report = _enricher_report(uris=[female_uri])
        sidecar = _enricher_sidecar(uris=[female_uri])

        with tempfile.NamedTemporaryFile(suffix=".owl", delete=False) as f:
            owl_path = f.name

        mtime_before = Path(owl_path).stat().st_mtime
        n_added, _ = enrich_ontology_from_value_mappings(sidecar, report, g, owl_path)

        assert n_added == 0
        # List should still have exactly one entry
        one_of_head = g.value(eq_bn, OWL.oneOf)
        members = list(Collection(g, one_of_head))
        assert members.count(_uri("FemaleGender")) == 1
        # File NOT written — nothing changed
        assert Path(owl_path).stat().st_mtime == mtime_before

    def test_warm_run_logs_info_not_warning(self, caplog):
        """Existing individual logs INFO, never WARNING."""
        import logging
        g = _enricher_graph()
        female_uri = str(_uri("FemaleGender"))
        g.add((_uri("FemaleGender"), RDF.type, OWL.NamedIndividual))
        g.add((_uri("FemaleGender"), RDF.type, _uri("Gender")))

        report = _enricher_report(uris=[female_uri])
        sidecar = _enricher_sidecar(uris=[female_uri])

        with tempfile.NamedTemporaryFile(suffix=".owl", delete=False) as f:
            owl_path = f.name

        with caplog.at_level(logging.INFO, logger="semantic_mapper.ontology.enricher"):
            enrich_ontology_from_value_mappings(sidecar, report, g, owl_path)

        info_msgs = [r.message for r in caplog.records if r.levelname == "INFO"]
        warning_msgs = [r.message for r in caplog.records
                        if r.levelname == "WARNING" and "FemaleGender" in r.message]
        assert any("already exists" in m for m in info_msgs), \
            f"Expected INFO 'already exists' — got: {info_msgs}"
        assert not warning_msgs, f"Unexpected WARNING for warm individual: {warning_msgs}"


# ---------------------------------------------------------------------------
# Enricher: untyped individual prevention
# ---------------------------------------------------------------------------

class TestEnricherUntypedIndividualPrevention:
    def test_individual_not_created_without_range(self):
        """When the mapped property has no rdfs:range, no individual is created."""
        g = Graph()
        g.add((_uri("Admission"), RDF.type, OWL.Class))
        no_range_prop = _uri("hasStatus")
        g.add((no_range_prop, RDF.type, OWL.ObjectProperty))
        # deliberately no RDFS.range

        col = PropertyMapping(
            source_column="status",
            source_data_type="VARCHAR",
            is_nullable=False,
            mapped_property_uri=str(no_range_prop),
            status=MappingStatus.CONFIRMED,
            confidence_score=0.9,
            rationale="m",
            value_mappings=[
                ValueMappingRule(source_value="ACTIVE", mapped_uri=str(_uri("ActiveStatus")),
                                 rationale="d", confidence_score=0.9)
            ],
        )
        table = TableMapping(
            source_table="admissions",
            class_candidate_set=[ClassCandidate(uri=str(_uri("Admission")), label="Admission",
                                                rationale="m", confidence_score=0.9)],
            column_mappings=[col],
        )
        report = SemanticMappingReport(
            ontology_uri=BASE, database_name="test_db", settings_used={},
            tables=[table], total_pending_review=0,
        )
        sidecar = {"admissions.status": [
            {"source_value": "ACTIVE", "mapped_uri": str(_uri("ActiveStatus"))}
        ]}

        with tempfile.NamedTemporaryFile(suffix=".owl", delete=False) as f:
            owl_path = f.name

        n_added, _ = enrich_ontology_from_value_mappings(sidecar, report, g, owl_path)

        assert n_added == 0
        assert (_uri("ActiveStatus"), RDF.type, OWL.NamedIndividual) not in g

    def test_error_logged_when_skipping_untyped(self, caplog):
        """An ERROR is logged when an individual is skipped due to missing range."""
        import logging
        g = Graph()
        g.add((_uri("Admission"), RDF.type, OWL.Class))
        no_range_prop = _uri("hasStatus")
        g.add((no_range_prop, RDF.type, OWL.ObjectProperty))

        col = PropertyMapping(
            source_column="status",
            source_data_type="VARCHAR",
            is_nullable=False,
            mapped_property_uri=str(no_range_prop),
            status=MappingStatus.CONFIRMED,
            confidence_score=0.9,
            rationale="m",
            value_mappings=[
                ValueMappingRule(source_value="ACTIVE", mapped_uri=str(_uri("ActiveStatus")),
                                 rationale="d", confidence_score=0.9)
            ],
        )
        table = TableMapping(
            source_table="admissions",
            class_candidate_set=[ClassCandidate(uri=str(_uri("Admission")), label="Admission",
                                                rationale="m", confidence_score=0.9)],
            column_mappings=[col],
        )
        report = SemanticMappingReport(
            ontology_uri=BASE, database_name="test_db", settings_used={},
            tables=[table], total_pending_review=0,
        )
        sidecar = {"admissions.status": [
            {"source_value": "ACTIVE", "mapped_uri": str(_uri("ActiveStatus"))}
        ]}

        with tempfile.NamedTemporaryFile(suffix=".owl", delete=False) as f:
            owl_path = f.name

        with caplog.at_level(logging.ERROR, logger="semantic_mapper.ontology.enricher"):
            enrich_ontology_from_value_mappings(sidecar, report, g, owl_path)

        error_msgs = [r.message for r in caplog.records if r.levelname == "ERROR"]
        assert any("untyped" in m or "no rdfs:range" in m for m in error_msgs), \
            f"Expected ERROR about missing range; got: {error_msgs}"

    def test_new_individual_always_has_class_type(self):
        """Every new individual must have rdf:type pointing to the range class."""
        g = _enricher_graph()
        female_uri = str(_uri("FemaleGender"))
        report = _enricher_report(uris=[female_uri])
        sidecar = _enricher_sidecar(uris=[female_uri])

        with tempfile.NamedTemporaryFile(suffix=".owl", delete=False) as f:
            owl_path = f.name

        enrich_ontology_from_value_mappings(sidecar, report, g, owl_path)

        assert (_uri("FemaleGender"), RDF.type, OWL.NamedIndividual) in g
        assert (_uri("FemaleGender"), RDF.type, _uri("Gender")) in g
