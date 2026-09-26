# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from semantic_mapper.gold.models import GoldColumnSpec, GoldTableDefinition
from semantic_mapper.query.gold_context_builder import build_gold_schema_context


def _make_definition(table_name="gold_party", class_uri="http://ex.org#Party", cols=None):
    if cols is None:
        cols = [
            GoldColumnSpec(
                silver_table="silver_raw_party",
                source_column="party_id",
                gold_column_name="party_id",
                source_data_type="VARCHAR(50)",
                property_uri="http://ex.org#hasCustomerID",
            ),
        ]
    return GoldTableDefinition(
        gold_table_name=table_name,
        primary_class_uri=class_uri,
        class_label=table_name.replace("gold_", ""),
        source_silver_tables=["silver_raw_party"],
        column_specs=cols,
    )


class TestBuildGoldSchemaContext:
    def test_table_name_present(self):
        ctx = build_gold_schema_context([_make_definition()])
        assert "gold_party" in ctx

    def test_class_header_uses_uri_fragment(self):
        ctx = build_gold_schema_context([_make_definition(class_uri="http://ex.org#Party")])
        assert "class: Party" in ctx

    def test_ontology_class_uri_column_present(self):
        ctx = build_gold_schema_context([_make_definition()])
        assert "_ontology_class_uri" in ctx

    def test_bronze_source_ref_column_present(self):
        ctx = build_gold_schema_context([_make_definition()])
        assert "_bronze_source_ref" in ctx

    def test_data_column_present(self):
        ctx = build_gold_schema_context([_make_definition()])
        assert "party_id" in ctx

    def test_property_fragment_shown(self):
        ctx = build_gold_schema_context([_make_definition()])
        assert "hasCustomerID" in ctx

    def test_multiple_definitions_produce_multiple_sections(self):
        defs = [
            _make_definition("gold_party", "http://ex.org#Party"),
            _make_definition("gold_agreement", "http://ex.org#Agreement"),
        ]
        ctx = build_gold_schema_context(defs)
        assert "gold_party" in ctx
        assert "gold_agreement" in ctx

    def test_actual_class_uris_override_header(self):
        ctx = build_gold_schema_context(
            [_make_definition()],
            actual_class_uris={"gold_party": ["IndividualCustomer", "Organisation"]},
        )
        assert "IndividualCustomer" in ctx
        assert "Organisation" in ctx

    def test_empty_definitions_returns_empty_string(self):
        ctx = build_gold_schema_context([])
        assert ctx == ""
