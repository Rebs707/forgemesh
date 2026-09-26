# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""A value-mapped column is a discriminated-subtype discriminator ONLY when its
targets are owl:Class nodes that are rdfs:subClassOf the table's entity class.
Targets that are vocabulary individuals (gender → MaleGender) are a plain property,
so the row keeps its table's class — and a flat ontology never discriminates.
"""
from __future__ import annotations

from rdflib import Graph, URIRef, RDF, RDFS, OWL

from semantic_mapper.silver.pipeline import SilverPipeline
from semantic_mapper.schema.mapping_output import (
    SemanticMappingReport, PropertyMapping, MappingStatus, ValueMappingRule,
)

EX = "http://ex.org#"


def _pipeline(graph):
    rep = SemanticMappingReport(ontology_uri="x", database_name="x", settings_used={},
                                tables=[], total_pending_review=0)
    # engines/writer/settings are unused by _is_subtype_discriminator
    return SilverPipeline(report=rep, bronze_engine=None, writer=None,
                          settings=None, ontology_graph=graph)


def _mapping(uris):
    return PropertyMapping(
        source_column="c", source_data_type="INT", is_nullable=False,
        status=MappingStatus.CONFIRMED, confidence_score=0.9, rationale="t",
        mapped_property_uri=EX + "prop",
        value_mappings=[
            ValueMappingRule(source_value=i, mapped_uri=u, rationale="t", confidence_score=0.9)
            for i, u in enumerate(uris)
        ],
    )


def _subclass_onto():
    g = Graph()
    for c in ("Party", "Individual", "Organisation"):
        g.add((URIRef(EX + c), RDF.type, OWL.Class))
    g.add((URIRef(EX + "Individual"), RDFS.subClassOf, URIRef(EX + "Party")))
    g.add((URIRef(EX + "Organisation"), RDFS.subClassOf, URIRef(EX + "Party")))
    return g


def _individual_onto():
    g = Graph()
    g.add((URIRef(EX + "Athlete"), RDF.type, OWL.Class))
    g.add((URIRef(EX + "Gender"), RDF.type, OWL.Class))
    for ind in ("MaleGender", "FemaleGender"):
        g.add((URIRef(EX + ind), RDF.type, OWL.NamedIndividual))
        g.add((URIRef(EX + ind), RDF.type, URIRef(EX + "Gender")))
    return g


class TestSubtypeDiscriminatorGate:
    def test_subclass_targets_are_a_discriminator(self):
        p = _pipeline(_subclass_onto())
        assert p._is_subtype_discriminator(_mapping([EX + "Individual", EX + "Organisation"]),
                                           EX + "Party") is True

    def test_individual_targets_are_not_a_discriminator(self):
        # gender → MaleGender/FemaleGender are individuals, not subclasses → property
        p = _pipeline(_individual_onto())
        assert p._is_subtype_discriminator(_mapping([EX + "MaleGender", EX + "FemaleGender"]),
                                           EX + "Athlete") is False

    def test_flat_ontology_never_discriminates(self):
        g = Graph()
        g.add((URIRef(EX + "Athlete"), RDF.type, OWL.Class))
        p = _pipeline(g)
        assert p._is_subtype_discriminator(_mapping([EX + "X", EX + "Y"]), EX + "Athlete") is False

    def test_subclasses_of_a_different_class_are_not_discriminator(self):
        # real subclasses, but of Party — not of the table's entity (Athlete)
        p = _pipeline(_subclass_onto())
        assert p._is_subtype_discriminator(_mapping([EX + "Individual", EX + "Organisation"]),
                                           EX + "Athlete") is False
