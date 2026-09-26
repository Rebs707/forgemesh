# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Option (b) materialisation — TripleBuilder mints code individuals + skos:exactMatch
lazily, one per distinct code, for a terminology-coded object property."""
from __future__ import annotations

from rdflib import Graph, Literal, OWL, RDF, RDFS, URIRef, XSD
from rdflib.namespace import SKOS

from semantic_mapper.gold.models import GoldColumnSpec, GoldTableDefinition
from semantic_mapper.ontology.iterative.categorical_individuals import _value_slug
from semantic_mapper.ontology.terminology.sources import OfflineCodesetSource
from semantic_mapper.rdf.triple_builder import TripleBuilder
from semantic_mapper.rdf.uri_factory import UriFactory

from .builders import BASE, FM

_PROP = URIRef(BASE + "hasObsCode")
_CLS = URIRef(BASE + "TestsysConcept")
_OBS = BASE + "Observation"
_SYS = URIRef(FM + "terminologySystem")


def _ontology() -> Graph:
    """A coded object property flagged with fm:terminologySystem + range = code class."""
    g = Graph()
    g.add((_PROP, RDF.type, OWL.ObjectProperty))
    g.add((_PROP, RDFS.range, _CLS))
    g.add((_PROP, _SYS, Literal("TestSys")))
    g.add((_PROP, URIRef(FM + "terminologyBindingConfidence"), Literal(0.9, datatype=XSD.decimal)))
    g.add((_CLS, RDF.type, OWL.Class))
    return g


def _builder() -> TripleBuilder:
    source = OfflineCodesetSource("testsys", "TestSys", codes={"A1": "Alpha", "B2": "Beta"},
                                  iri_template="http://example.org/concept/{code}")
    defn = GoldTableDefinition(
        gold_table_name="gold_observation", primary_class_uri=_OBS, class_label="observation",
        source_silver_tables=["silver_raw_obs"],
        column_specs=[GoldColumnSpec(
            silver_table="silver_raw_obs", source_column="code", gold_column_name="hasObsCode",
            source_data_type="VARCHAR", property_uri=str(_PROP),
        )],
        id_column=None,
    )
    builder = TripleBuilder(
        [defn], UriFactory("http://mesh.local/data/", "test"),
        ontology_graph=_ontology(), terminology_sources={"TestSys": source},
    )
    return builder, defn


def _row(code, ref):
    return {"hasObsCode": code, "_bronze_source_ref": f"raw_obs.id={ref}", "_gold_run_id": "r"}


def _code_ind(code: str) -> URIRef:
    return URIRef(BASE + _value_slug(code) + "TestsysConcept")


class TestCodedMaterialisation:
    def test_known_code_links_and_binds(self) -> None:
        builder, defn = _builder()
        triples = builder.build(_row("A1", "1"), defn)
        ind = _code_ind("A1")
        assert any(p == _PROP and o == ind for s, p, o in triples)
        assert (ind, RDF.type, _CLS) in triples
        assert (ind, RDFS.label, Literal("A1")) in triples
        assert (ind, SKOS.exactMatch, URIRef("http://example.org/concept/A1")) in triples
        assert (ind, _SYS, Literal("TestSys")) in triples

    def test_distinct_code_minted_once_across_rows(self) -> None:
        builder, defn = _builder()
        t1 = builder.build(_row("A1", "1"), defn)
        t2 = builder.build(_row("A1", "2"), defn)   # same code, different subject
        ind = _code_ind("A1")
        # Both rows link to the individual…
        assert sum(1 for s, p, o in t1 + t2 if p == _PROP and o == ind) == 2
        # …but its exactMatch description is emitted only once (first sight).
        assert sum(1 for s, p, o in t1 + t2 if s == ind and p == SKOS.exactMatch) == 1

    def test_unrecognised_value_is_unbound_concept(self) -> None:
        builder, defn = _builder()
        triples = builder.build(_row("Z9", "1"), defn)   # not in the source
        ind = _code_ind("Z9")
        # Still linked + typed (data not dropped)…
        assert any(p == _PROP and o == ind for s, p, o in triples)
        assert (ind, RDF.type, _CLS) in triples
        # …but no exactMatch — the binding is simply absent for an unknown code.
        assert not [(s, p, o) for s, p, o in triples if s == ind and p == SKOS.exactMatch]

    def test_concept_individuals_carry_no_bronze_provenance(self) -> None:
        # Concept individuals are reference nodes (no gold row) — only the entity
        # subject carries fm:bronzeSourceRef / fm:goldRunId.
        builder, defn = _builder()
        triples = builder.build(_row("A1", "1"), defn)
        ind = _code_ind("A1")
        assert not [(s, p, o) for s, p, o in triples
                    if s == ind and p in (URIRef(FM + "bronzeSourceRef"), URIRef(FM + "goldRunId"))]
