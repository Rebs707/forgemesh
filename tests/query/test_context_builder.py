# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from semantic_mapper.query.context_builder import (
    _format_value_mappings,
    _property_col_name,
    _uri_fragment,
    build_schema_context,
)
from semantic_mapper.schema.mapping_output import ValueMappingRule


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------

class TestUriFrag:
    def test_hash_fragment(self):
        assert _uri_fragment("http://example.org/ontology#dateOfBirth") == "dateOfBirth"

    def test_slash_segment(self):
        assert _uri_fragment("http://example.org/ontology/dateOfBirth") == "dateOfBirth"

    def test_bare_string(self):
        assert _uri_fragment("dateOfBirth") == "dateOfBirth"


class TestPropertyColName:
    def test_camel_to_snake(self):
        assert _property_col_name("http://example.org/ontology#dateOfBirth", "dob") == "date_of_birth"

    def test_already_snake(self):
        assert _property_col_name("http://example.org/ontology#party_type", "pt") == "party_type"

    def test_property_fallback(self):
        # If local name resolves to "property" use fallback
        assert _property_col_name("http://www.w3.org/2002/07/owl#property", "col") == "col"

    def test_slash_uri(self):
        assert _property_col_name("http://example.org/ontology/partyIdentifier", "pid") == "party_identifier"


class TestFormatValueMappings:
    def test_basic(self):
        rules = [
            ValueMappingRule(source_value=1, mapped_uri="http://ex.org#Individual", rationale="x", confidence_score=0.9),
            ValueMappingRule(source_value=2, mapped_uri="http://ex.org#Organisation", rationale="y", confidence_score=0.9),
        ]
        result = _format_value_mappings(rules)
        assert result == "1→Individual, 2→Organisation"

    def test_null_uri(self):
        rules = [
            ValueMappingRule(source_value="POL", mapped_uri="http://ex.org#Policy", rationale="x", confidence_score=0.9),
            ValueMappingRule(source_value="SUB", mapped_uri="http://ex.org#Subscription", rationale="y", confidence_score=0.9),
        ]
        assert _format_value_mappings(rules) == "POL→Policy, SUB→Subscription"

    def test_empty(self):
        assert _format_value_mappings([]) == ""


# ---------------------------------------------------------------------------
# Integration test for build_schema_context
# ---------------------------------------------------------------------------

class TestBuildSchemaContext:
    def test_silver_table_name_present(self, simple_report):
        ctx = build_schema_context(simple_report)
        assert "silver_raw_party" in ctx

    def test_class_header_present(self, simple_report):
        ctx = build_schema_context(simple_report)
        assert "Individual" in ctx

    def test_low_confidence_class_excluded(self, simple_report):
        ctx = build_schema_context(simple_report)
        assert "LowConfidence" not in ctx

    def test_ontology_class_uri_metadata_column_present(self, simple_report):
        ctx = build_schema_context(simple_report)
        assert "_ontology_class_uri" in ctx

    def test_bronze_source_ref_metadata_column_present(self, simple_report):
        ctx = build_schema_context(simple_report)
        assert "_bronze_source_ref" in ctx

    def test_confirmed_mapping_included(self, simple_report):
        ctx = build_schema_context(simple_report)
        # source_column="party_id" → safe col name "party_id"
        assert "party_id" in ctx

    def test_pending_mapping_excluded(self, simple_report):
        ctx = build_schema_context(simple_report)
        # pending_mapping source_column = "notes"; its property URI is None so it should not appear
        assert "notes" not in ctx

    def test_value_mappings_inlined(self, simple_report):
        ctx = build_schema_context(simple_report)
        # value-mapped column: stored as full URI, hint shows original codes with →
        assert "LIKE '%ClassName'" in ctx
        assert "1→Individual" in ctx
        assert "2→Organisation" in ctx

    def test_source_column_names_used(self, simple_report):
        ctx = build_schema_context(simple_report)
        # source_column="dob" → "dob"
        assert "dob" in ctx
        # source_column="party_id" → "party_id"
        assert "party_id" in ctx
