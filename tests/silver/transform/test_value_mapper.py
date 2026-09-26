# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from semantic_mapper.schema.mapping_output import (
    MappingStatus,
    PropertyMapping,
    ValueMappingRule,
)
from semantic_mapper.silver.models import TransformOutcome
from semantic_mapper.silver.transform.value_mapper import ValueMapper


def _mapping_with_rules(rules):
    return PropertyMapping(
        source_column="party_type",
        source_data_type="INT",
        is_nullable=False,
        status=MappingStatus.CONFIRMED,
        confidence_score=0.90,
        rationale="test",
        value_mappings=rules,
    )


def _rules():
    return [
        ValueMappingRule(
            source_value=1,
            mapped_uri="http://ex.org#Individual",
            rationale="1=Individual",
            confidence_score=0.95,
        ),
        ValueMappingRule(
            source_value=2,
            mapped_uri="http://ex.org#Organisation",
            rationale="2=Organisation",
            confidence_score=0.95,
        ),
    ]


@pytest.fixture
def mapper():
    return ValueMapper()


# ---------------------------------------------------------------------------
# can_transform
# ---------------------------------------------------------------------------

class TestCanTransform:
    def test_true_when_value_mappings_present(self, mapper):
        mapping = _mapping_with_rules(_rules())
        assert mapper.can_transform(mapping) is True

    def test_false_when_no_value_mappings(self, mapper):
        mapping = _mapping_with_rules([])
        assert mapper.can_transform(mapping) is False


# ---------------------------------------------------------------------------
# Exact match
# ---------------------------------------------------------------------------

class TestExactMatch:
    def test_int_value_maps_to_uri(self, mapper):
        mapping = _mapping_with_rules(_rules())
        result = mapper.transform(1, mapping)
        assert result.outcome == TransformOutcome.TRANSFORMED
        assert result.transformed_value == "http://ex.org#Individual"

    def test_int_value_2_maps_to_org(self, mapper):
        mapping = _mapping_with_rules(_rules())
        result = mapper.transform(2, mapping)
        assert result.transformed_value == "http://ex.org#Organisation"


# ---------------------------------------------------------------------------
# String coercion
# ---------------------------------------------------------------------------

class TestStrCoercion:
    def test_string_one_matches_int_rule(self, mapper):
        mapping = _mapping_with_rules(_rules())
        result = mapper.transform("1", mapping)
        assert result.outcome == TransformOutcome.TRANSFORMED
        assert result.transformed_value == "http://ex.org#Individual"

    def test_string_two_matches_int_rule(self, mapper):
        mapping = _mapping_with_rules(_rules())
        result = mapper.transform("2", mapping)
        assert result.transformed_value == "http://ex.org#Organisation"


# ---------------------------------------------------------------------------
# No match → PASSTHROUGH (value not in mapping table, pass through as string)
# ---------------------------------------------------------------------------

class TestNoMatch:
    def test_unknown_value_passes_through(self, mapper):
        mapping = _mapping_with_rules(_rules())
        result = mapper.transform(99, mapping)
        assert result.outcome == TransformOutcome.PASSTHROUGH
        assert result.transformed_value == 99

    def test_rationale_lists_known_values(self, mapper):
        mapping = _mapping_with_rules(_rules())
        result = mapper.transform(99, mapping)
        assert "1" in result.rationale
        assert "2" in result.rationale

    def test_original_value_preserved(self, mapper):
        mapping = _mapping_with_rules(_rules())
        result = mapper.transform(99, mapping)
        assert result.original_value == 99


class TestCaseInsensitiveMatch:
    """Case-insensitive string fallback handles casing variants in source data."""

    def _str_rules(self):
        return [
            ValueMappingRule(
                source_value="Chemistry",
                mapped_uri="http://ex.org#Chemistry",
                rationale="Chemistry category",
                confidence_score=0.95,
            ),
            ValueMappingRule(
                source_value="Blood Gas",
                mapped_uri="http://ex.org#BloodGas",
                rationale="Blood Gas category",
                confidence_score=0.95,
            ),
        ]

    def test_uppercase_value_matches_mixed_case_rule(self, mapper):
        mapping = _mapping_with_rules(self._str_rules())
        result = mapper.transform("CHEMISTRY", mapping)
        assert result.outcome == TransformOutcome.TRANSFORMED
        assert result.transformed_value == "http://ex.org#Chemistry"

    def test_lowercase_value_matches_mixed_case_rule(self, mapper):
        mapping = _mapping_with_rules(self._str_rules())
        result = mapper.transform("chemistry", mapping)
        assert result.outcome == TransformOutcome.TRANSFORMED
        assert result.transformed_value == "http://ex.org#Chemistry"

    def test_spaced_variant_matches(self, mapper):
        mapping = _mapping_with_rules(self._str_rules())
        result = mapper.transform("BLOOD GAS", mapping)
        assert result.outcome == TransformOutcome.TRANSFORMED
        assert result.transformed_value == "http://ex.org#BloodGas"

    def test_rationale_indicates_case_insensitive(self, mapper):
        mapping = _mapping_with_rules(self._str_rules())
        result = mapper.transform("CHEMISTRY", mapping)
        assert "Case-insensitive" in result.rationale

    def test_numeric_rule_not_affected_by_case_path(self, mapper):
        """Integer rules are unaffected — case path only activates for str/str."""
        mapping = _mapping_with_rules(_rules())
        result = mapper.transform("1", mapping)
        assert result.outcome == TransformOutcome.TRANSFORMED
        assert "Case-insensitive" not in result.rationale


class TestNullSentinels:
    """Null-sentinel strings must be treated as NULL, not quarantined."""

    @pytest.mark.parametrize("sentinel", [
        "None", "none", "NONE",
        "NULL", "null",
        "N/A", "n/a",
        "NaN", "nan",
        "", "  ",
        "unknown", "Unknown",
        "-",
    ])
    def test_sentinel_yields_null_preserved(self, mapper, sentinel):
        mapping = _mapping_with_rules(_rules())
        result = mapper.transform(sentinel, mapping)
        assert result.outcome == TransformOutcome.NULL_PRESERVED, (
            f"Expected NULL_PRESERVED for sentinel {sentinel!r}, got {result.outcome}"
        )
        assert result.transformed_value is None

    def test_sentinel_does_not_quarantine(self, mapper):
        mapping = _mapping_with_rules(_rules())
        result = mapper.transform("None", mapping)
        assert result.outcome != TransformOutcome.QUARANTINED

    def test_real_unknown_value_passes_through(self, mapper):
        """Non-sentinel unknown values pass through as plain strings."""
        mapping = _mapping_with_rules(_rules())
        result = mapper.transform("something_unexpected", mapping)
        assert result.outcome == TransformOutcome.PASSTHROUGH
        assert result.transformed_value == "something_unexpected"
