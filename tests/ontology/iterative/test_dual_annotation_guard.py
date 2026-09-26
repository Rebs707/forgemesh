# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Tests for the fabricated-class / dual-annotation guard.

Covers two layers:

  Layer 1 — graph_merger._strip_dual_annotated_object_properties():
    Pre-merge fragment safety net. When the gen-LLM emits both a
    DatatypeProperty (fm:sourceColumn "table.col") and an ObjectProperty
    (fm:sourceForeignKey "table.col → FabricatedClass") for the same source
    column, the ObjectProperty side is stripped. The fabricated range class is
    also stripped if nothing else in the fragment references it.

  Layer 2 — gen_prompt Rule 14 (CATEGORICAL_OPEN → DatatypeProperty):
    The prompt no longer instructs the LLM to create ObjectProperty + open
    owl:Class for CATEGORICAL_OPEN columns. The rule text must not contain
    any instruction to use ObjectProperty for that hint.

All tests use generic table/column names — no dataset-specific names.
"""
from __future__ import annotations

import rdflib
import pytest
from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef
from rdflib.namespace import XSD

from semantic_mapper.ontology.iterative.graph_merger import (
    _strip_dual_annotated_object_properties,
    FM_SOURCE_COLUMN,
    FM_SOURCE_FK,
)

BASE = "http://mesh.local/ontology/test#"


def _u(local: str) -> URIRef:
    return URIRef(BASE + local)


# ---------------------------------------------------------------------------
# Fragment-builder helpers
# ---------------------------------------------------------------------------

def _make_datatype_prop(fragment: Graph, col: str, table: str, prop_local: str) -> URIRef:
    """Add a DatatypeProperty with fm:sourceColumn to the fragment."""
    prop = _u(prop_local)
    fragment.add((prop, RDF.type, OWL.DatatypeProperty))
    fragment.add((prop, RDFS.domain, _u("Event")))
    fragment.add((prop, RDFS.range, XSD.string))
    fragment.add((prop, FM_SOURCE_COLUMN, Literal(f"{table}.{col}")))
    return prop


def _make_object_prop_with_fabricated_class(
    fragment: Graph, col: str, table: str, prop_local: str, class_local: str
) -> tuple[URIRef, URIRef]:
    """Add an ObjectProperty + fabricated class with fm:sourceForeignKey to the fragment."""
    cls = _u(class_local)
    fragment.add((cls, RDF.type, OWL.Class))
    fragment.add((cls, RDFS.label, Literal(class_local, lang="en")))

    prop = _u(prop_local)
    fragment.add((prop, RDF.type, OWL.ObjectProperty))
    fragment.add((prop, RDFS.domain, _u("Event")))
    fragment.add((prop, RDFS.range, cls))
    fragment.add((prop, FM_SOURCE_FK, Literal(f"{table}.{col} -> {class_local}")))
    return prop, cls


# ---------------------------------------------------------------------------
# _strip_dual_annotated_object_properties tests
# ---------------------------------------------------------------------------

class TestStripDualAnnotatedObjectProperties:
    def test_strips_object_property_when_same_column_has_datatype(self):
        """Core scenario: both DatatypeProperty and ObjectProperty annotate the same column."""
        g = Graph()
        _make_datatype_prop(g, "status", "raw_events", "hasEventStatus")
        obj_prop, _ = _make_object_prop_with_fabricated_class(
            g, "status", "raw_events", "hasEventStatusDescription", "StatusDescription"
        )

        removed = _strip_dual_annotated_object_properties(g)

        assert removed == 1
        assert (obj_prop, RDF.type, OWL.ObjectProperty) not in g

    def test_strips_fabricated_class_when_unreferenced(self):
        """The fabricated range class should be removed when the ObjectProperty is stripped."""
        g = Graph()
        _make_datatype_prop(g, "category", "raw_orders", "hasOrderCategory")
        _, fabricated_cls = _make_object_prop_with_fabricated_class(
            g, "category", "raw_orders", "hasOrderCategoryRef", "OrderCategory"
        )

        _strip_dual_annotated_object_properties(g)

        assert (fabricated_cls, RDF.type, OWL.Class) not in g

    def test_retains_fabricated_class_when_still_referenced(self):
        """If another ObjectProperty in the fragment also uses the range class, retain it."""
        g = Graph()
        cls = _u("OrderCategory")
        g.add((cls, RDF.type, OWL.Class))

        # First ObjectProperty — the dual-annotated one (will be stripped)
        _make_datatype_prop(g, "category", "raw_orders", "hasOrderCategory")
        obj_prop, _ = _make_object_prop_with_fabricated_class(
            g, "category", "raw_orders", "hasOrderCategoryRef", "OrderCategory"
        )

        # Second ObjectProperty pointing at the same class — legitimate FK from inspector
        other_prop = _u("hasRelatedOrderCategory")
        g.add((other_prop, RDF.type, OWL.ObjectProperty))
        g.add((other_prop, RDFS.domain, _u("Item")))
        g.add((other_prop, RDFS.range, cls))

        _strip_dual_annotated_object_properties(g)

        # Class must survive because other_prop still references it
        assert (cls, RDF.type, OWL.Class) in g

    def test_leaves_object_property_intact_when_no_dual_annotation(self):
        """ObjectProperty with no matching DatatypeProperty must not be touched."""
        g = Graph()
        # Only an ObjectProperty — no DatatypeProperty for the same column
        obj_prop, cls = _make_object_prop_with_fabricated_class(
            g, "patient_id", "raw_events", "hasEventPatient", "Patient"
        )

        removed = _strip_dual_annotated_object_properties(g)

        assert removed == 0
        assert (obj_prop, RDF.type, OWL.ObjectProperty) in g
        assert (cls, RDF.type, OWL.Class) in g

    def test_leaves_datatype_property_intact_after_stripping(self):
        """The DatatypeProperty side must survive the strip operation."""
        g = Graph()
        dt_prop = _make_datatype_prop(g, "status", "raw_events", "hasEventStatus")
        _make_object_prop_with_fabricated_class(
            g, "status", "raw_events", "hasEventStatusRef", "StatusDescription"
        )

        _strip_dual_annotated_object_properties(g)

        assert (dt_prop, RDF.type, OWL.DatatypeProperty) in g
        assert (dt_prop, FM_SOURCE_COLUMN, Literal("raw_events.status")) in g

    def test_strips_multiple_dual_annotated_properties(self):
        """Multiple columns each with a dual annotation are all stripped."""
        g = Graph()
        _make_datatype_prop(g, "uom", "raw_measurements", "hasMeasurementUom")
        obj1, cls1 = _make_object_prop_with_fabricated_class(
            g, "uom", "raw_measurements", "hasMeasurementUomRef", "AmountUom"
        )

        _make_datatype_prop(g, "cancelled_by", "raw_orders", "hasOrderCancelledBy")
        obj2, cls2 = _make_object_prop_with_fabricated_class(
            g, "cancelled_by", "raw_orders", "hasOrderCancelledByRef", "CancelledBy"
        )

        removed = _strip_dual_annotated_object_properties(g)

        assert removed == 2
        assert (obj1, RDF.type, OWL.ObjectProperty) not in g
        assert (obj2, RDF.type, OWL.ObjectProperty) not in g
        assert (cls1, RDF.type, OWL.Class) not in g
        assert (cls2, RDF.type, OWL.Class) not in g

    def test_noop_when_fragment_has_no_datatype_properties(self):
        """Fragment with only ObjectProperties → guard is a no-op."""
        g = Graph()
        obj_prop, _ = _make_object_prop_with_fabricated_class(
            g, "patient_id", "raw_events", "hasEventPatient", "Patient"
        )

        removed = _strip_dual_annotated_object_properties(g)

        assert removed == 0

    def test_noop_on_empty_fragment(self):
        """Empty fragment → no crash, returns 0."""
        g = Graph()
        removed = _strip_dual_annotated_object_properties(g)
        assert removed == 0

    def test_logs_warning_on_strip(self, caplog):
        """A WARNING is logged for each stripped ObjectProperty."""
        import logging
        g = Graph()
        _make_datatype_prop(g, "status", "raw_events", "hasEventStatus")
        _make_object_prop_with_fabricated_class(
            g, "status", "raw_events", "hasEventStatusRef", "StatusDescription"
        )

        with caplog.at_level(
            logging.WARNING, logger="semantic_mapper.ontology.iterative.graph_merger"
        ):
            _strip_dual_annotated_object_properties(g)

        warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
        assert any("status" in m for m in warnings), f"Expected warning mentioning 'status'; got {warnings}"

    def test_different_tables_same_col_name_not_stripped(self):
        """DatatypeProperty on raw_a.status and ObjectProperty on raw_b.status are distinct — no strip."""
        g = Graph()
        # DatatypeProperty for raw_table_a.status
        _make_datatype_prop(g, "status", "raw_table_a", "hasTableAStatus")
        # ObjectProperty for raw_table_b.status — different table, no conflict
        obj_prop = _u("hasTableBStatusRef")
        cls = _u("StatusB")
        g.add((cls, RDF.type, OWL.Class))
        g.add((obj_prop, RDF.type, OWL.ObjectProperty))
        g.add((obj_prop, RDFS.domain, _u("TableB")))
        g.add((obj_prop, RDFS.range, cls))
        g.add((obj_prop, FM_SOURCE_FK, Literal("raw_table_b.status -> StatusB")))

        removed = _strip_dual_annotated_object_properties(g)

        assert removed == 0
        assert (obj_prop, RDF.type, OWL.ObjectProperty) in g


# ---------------------------------------------------------------------------
# gen_prompt Rule 14 — CATEGORICAL_OPEN must map to DatatypeProperty
# ---------------------------------------------------------------------------

class TestGenPromptCategoricalOpenRule:
    """Verify Rule 14 in the built prompt forbids ObjectProperty for CATEGORICAL_OPEN."""

    def _build_minimal_prompt(self, hint: str) -> str:
        from semantic_mapper.ontology.iterative.models import ColumnContext, TableContext
        from semantic_mapper.ontology.iterative.gen_prompt import build_gen_prompt

        col = ColumnContext(
            table_name="raw_events",
            col_name="category",
            inferred_type="VARCHAR",
            is_pk=False,
            is_fk=False,
            is_nullable=True,
            null_rate=0.0,
            distinct_count=50,
            top_values=["A", "B", "C"],
            cardinality_hint=hint,
        )
        ctx = TableContext(
            table_name="raw_events",
            row_count=500,
            columns=[col],
            processing_index=1,
            total_tables=1,
        )
        return build_gen_prompt(
            table_ctx=ctx,
            retrieved_classes=[],
            deterministic_uri=f"{BASE}Event",
            base_iri=BASE,
        )

    def _rule14_segment(self, prompt: str) -> str:
        """Extract the Rule 14 cardinality hint section from the prompt."""
        # Rule 14 is always in the RULES section; find the line starting with "14."
        idx = prompt.find("14. CARDINALITY HINT")
        assert idx != -1, "Rule 14 not found in prompt"
        return prompt[idx:idx + 600]

    def test_categorical_open_rule_contains_datatypeproperty(self):
        """Rule 14 for CATEGORICAL_OPEN must instruct DatatypeProperty."""
        prompt = self._build_minimal_prompt("CATEGORICAL_OPEN")
        segment = self._rule14_segment(prompt)
        assert "CATEGORICAL_OPEN" in segment
        # Find the CATEGORICAL_OPEN line within Rule 14
        oc_idx = segment.index("CATEGORICAL_OPEN")
        subsegment = segment[oc_idx:oc_idx + 300]
        assert "DatatypeProperty" in subsegment, (
            f"Rule 14 CATEGORICAL_OPEN segment should instruct DatatypeProperty; got:\n{subsegment}"
        )

    def test_categorical_open_rule_forbids_objectproperty_class_creation(self):
        """Rule 14 for CATEGORICAL_OPEN must not instruct ObjectProperty + open owl:Class."""
        prompt = self._build_minimal_prompt("CATEGORICAL_OPEN")
        segment = self._rule14_segment(prompt)
        oc_idx = segment.index("CATEGORICAL_OPEN")
        subsegment = segment[oc_idx:oc_idx + 300]
        assert "open owl:Class" not in subsegment, (
            f"Rule 14 must not instruct 'open owl:Class' for CATEGORICAL_OPEN; got:\n{subsegment}"
        )
        assert "ObjectProperty + open" not in subsegment, (
            f"Rule 14 must not instruct 'ObjectProperty + open' for CATEGORICAL_OPEN; got:\n{subsegment}"
        )

    def test_categorical_closed_rule_still_uses_objectproperty(self):
        """CATEGORICAL_CLOSED must still instruct ObjectProperty + Class + Individual."""
        prompt = self._build_minimal_prompt("CATEGORICAL_CLOSED")
        segment = self._rule14_segment(prompt)
        assert "CATEGORICAL_CLOSED" in segment
        cc_idx = segment.index("CATEGORICAL_CLOSED")
        subsegment = segment[cc_idx:cc_idx + 400]
        assert "ObjectProperty" in subsegment, (
            f"CATEGORICAL_CLOSED rule must still use ObjectProperty; got:\n{subsegment}"
        )
        assert "NamedIndividual" in subsegment or "Individual" in subsegment, (
            f"CATEGORICAL_CLOSED rule must mention Individual; got:\n{subsegment}"
        )
