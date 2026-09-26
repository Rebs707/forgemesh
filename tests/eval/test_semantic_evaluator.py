# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Semantic scoring: a mapping is correct when the ontology gives the column the
meaning the ground truth states — regardless of how the property is named."""
from __future__ import annotations

import rdflib
from rdflib import OWL, RDF, RDFS, XSD, Namespace

from semantic_mapper.eval.evaluator import MappingEvaluator
from semantic_mapper.eval.models import GroundTruth, GroundTruthEntry
from semantic_mapper.schema.mapping_output import (
    ClassCandidate, MappingStatus, PropertyMapping, SemanticMappingReport, TableMapping,
)

O = Namespace("http://ex.org/shop#")


def _ontology() -> rdflib.Graph:
    g = rdflib.Graph()
    for cls in ("Order", "Customer", "Product", "Channel", "Party"):
        g.add((O[cls], RDF.type, OWL.Class))
    g.add((O.Customer, RDFS.subClassOf, O.Party))

    def prop(name, kind, domain, rng):
        g.add((O[name], RDF.type, kind))
        g.add((O[name], RDFS.domain, O[domain]))
        g.add((O[name], RDFS.range, rng))

    # well-modelled
    prop("hasOrderBuyer", OWL.ObjectProperty, "Order", O.Customer)      # FK, any name
    prop("hasOrderPlacedAt", OWL.DatatypeProperty, "Order", XSD.dateTime)
    prop("hasOrderChannel", OWL.ObjectProperty, "Order", O.Channel)     # categorical → value class
    prop("hasOrderNote", OWL.DatatypeProperty, "Order", XSD.string)
    prop("hasPartyName", OWL.DatatypeProperty, "Party", XSD.string)     # inherited domain
    # mistakes
    prop("hasOrderProductId", OWL.DatatypeProperty, "Order", XSD.integer)  # FK left as a number
    prop("hasOrderWrongLink", OWL.ObjectProperty, "Order", O.Product)      # links the wrong entity
    prop("hasCustomerTotal", OWL.DatatypeProperty, "Customer", XSD.decimal)  # wrong class
    prop("hasOrderShippedAt", OWL.DatatypeProperty, "Order", XSD.string)   # date as text
    prop("hasOrderCustomerLink", OWL.ObjectProperty, "Order", O.Customer)  # entity, not value class
    return g


def _col(col, uri, status=MappingStatus.CONFIRMED):
    return PropertyMapping(source_column=col, source_data_type="VARCHAR", is_nullable=True,
                           status=status, confidence_score=0.9, mapped_property_uri=uri,
                           rationale="test")


def _table(name, cls, cols):
    return TableMapping(source_table=name, column_mappings=cols, class_candidate_set=[
        ClassCandidate(uri=O[cls], label=cls, rationale="t", confidence_score=1.0)])


def _score(order_cols, customer_cols, entries):
    report = SemanticMappingReport(
        ontology_uri="", database_name="shop", settings_used={}, total_pending_review=0,
        tables=[_table("raw_orders", "Order", order_cols),
                _table("raw_customers", "Customer", customer_cols),
                _table("raw_products", "Product", [])])
    gt = GroundTruth(dataset="shop", mappings=[GroundTruthEntry(table=t, column=c, **exp)
                                              for t, c, exp in entries])
    return MappingEvaluator().evaluate(report, gt, ontology_graph=_ontology())


def _result(r, column):
    return next(c for c in r.per_column if c.column == column)


def test_correct_meanings_score_regardless_of_names():
    r = _score(
        [_col("customer_id", O.hasOrderBuyer), _col("placed_at", O.hasOrderPlacedAt),
         _col("channel", O.hasOrderChannel), _col("note", O.hasOrderNote)],
        [_col("name", O.hasPartyName)],
        [("raw_orders", "customer_id", {"refers_to": "raw_customers"}),
         ("raw_orders", "placed_at", {"value_type": "temporal"}),
         ("raw_orders", "channel", {"value_type": "categorical"}),
         ("raw_orders", "note", {"value_type": "text"}),
         ("raw_customers", "name", {"value_type": "text"})],   # domain via superclass
    )
    assert (r.true_positives, r.false_positives) == (5, 0)


def test_reference_left_as_a_literal_is_wrong():
    r = _score([_col("product_id", O.hasOrderProductId)], [],
               [("raw_orders", "product_id", {"refers_to": "raw_products"})])
    assert not _result(r, "product_id").correct
    assert "literal" in _result(r, "product_id").reason


def test_reference_to_the_wrong_entity_is_wrong():
    r = _score([_col("customer_id", O.hasOrderWrongLink)], [],
               [("raw_orders", "customer_id", {"refers_to": "raw_customers"})])
    assert "not Customer" in _result(r, "customer_id").reason


def test_property_on_the_wrong_class_is_wrong():
    r = _score([_col("total", O.hasCustomerTotal)], [],
               [("raw_orders", "total", {"value_type": "numeric"})])
    assert "domain" in _result(r, "total").reason


def test_wrong_value_type_is_wrong():
    r = _score([_col("shipped_at", O.hasOrderShippedAt)], [],
               [("raw_orders", "shipped_at", {"value_type": "temporal"})])
    assert not _result(r, "shipped_at").correct


def test_categorical_link_to_an_entity_is_not_a_value_class():
    r = _score([_col("vip", O.hasOrderCustomerLink)], [],
               [("raw_orders", "vip", {"value_type": "categorical"})])
    assert not _result(r, "vip").correct


def test_unconfirmed_mapping_is_a_miss_not_a_false_positive():
    r = _score([_col("placed_at", O.hasOrderPlacedAt, MappingStatus.PENDING_REVIEW)], [],
               [("raw_orders", "placed_at", {"value_type": "temporal"})])
    assert (r.true_positives, r.false_positives, r.false_negatives) == (0, 0, 1)


def test_any_of_several_value_types():
    r = _score([_col("note", O.hasOrderNote)], [],
               [("raw_orders", "note", {"value_type": ["numeric", "text"]})])
    assert r.true_positives == 1
