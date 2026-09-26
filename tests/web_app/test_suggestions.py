# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Example questions are derived from schema structure alone."""
from semantic_mapper.gold.models import GoldColumnSpec, GoldTableDefinition
from semantic_mapper.webapp.suggestions import MAX_SUGGESTIONS, build_suggestions

NS = "http://ex.org/o#"


def _spec(table, col, prop, value_map=None):
    return GoldColumnSpec(silver_table=f"silver_raw_{table}", source_column=col,
                          gold_column_name=prop, source_data_type="VARCHAR(50)",
                          property_uri=NS + prop, value_map=value_map or {})


def _defn(label, cls, specs):
    return GoldTableDefinition(gold_table_name=f"gold_{label}", primary_class_uri=NS + cls,
                               class_label=label, source_silver_tables=[f"silver_raw_{label}s"],
                               column_specs=specs)


def _schema():
    hub = _defn("hub", "Hub", [_spec("hubs", "region", "hasHubRegion", {"N": NS + "NorthRegion"})])
    spoke = _defn("spoke", "Spoke", [
        _spec("spokes", "hub_id", "hasSpokeHub"),
        _spec("spokes", "kind", "hasSpokeKind", {"A": NS + "AKind"}),
    ])
    leaf = _defn("leaf", "Leaf", [_spec("leaves", "spoke_id", "hasLeafSpoke")])
    ranges = {NS + "hasSpokeHub": NS + "Hub", NS + "hasLeafSpoke": NS + "Spoke",
              NS + "hasSpokeKind": NS + "Kind"}   # Kind is a value class, not an entity
    return [leaf, spoke, hub], ranges


def test_counts_the_most_referenced_entity_first():
    defs, ranges = _schema()
    qs = build_suggestions(defs, ranges)
    assert qs[0] in ("How many hubs are there in total?", "How many spokes are there in total?")


def test_relationship_and_categorical_questions_come_from_structure():
    defs, ranges = _schema()
    qs = build_suggestions(defs, ranges)
    assert "Which hubs have the most spokes?" in qs
    assert "Which spokes have the most leaves?" in qs
    assert "How many spokes are there by kind?" in qs
    # A range pointing at a value class is not an entity relationship.
    assert not any("kinds have the most" in q for q in qs)


def test_uses_ontology_labels_when_given():
    defs, ranges = _schema()
    qs = build_suggestions(defs, ranges, {NS + "Hub": "Distribution Hub"})
    assert "Which distribution hubs have the most spokes?" in qs


def test_no_relationships_or_categories_falls_back_to_a_sample():
    lone = _defn("widget", "Widget", [_spec("widgets", "size", "hasWidgetSize")])
    assert build_suggestions([lone], {}) == [
        "How many widgets are there in total?", "Show me a sample of widgets"]


def test_empty_schema_and_cap():
    assert build_suggestions([], {}) == []
    defs, ranges = _schema()
    assert len(build_suggestions(defs, ranges)) <= MAX_SUGGESTIONS
