# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from semantic_mapper.schema.mapping_output import (
    MappingStatus,
    PropertyMapping,
    TransformationSpec,
)
from semantic_mapper.silver.models import TransformOutcome
from semantic_mapper.silver.transform.date_normaliser import DateNormaliser


def _mapping_with_spec(formats=None, strategy="dateutil.parser"):
    return PropertyMapping(
        source_column="dob",
        source_data_type="VARCHAR(20)",
        is_nullable=True,
        status=MappingStatus.CONFIRMED,
        confidence_score=0.95,
        rationale="test",
        transformation_spec=TransformationSpec(
            detected_formats=formats or ["YYYY-MM-DD", "DD/MM/YYYY"],
            target_format="xsd:date",
            parse_strategy=strategy,
            validation_rule="valid date",
        ),
    )


def _mapping_no_spec():
    return PropertyMapping(
        source_column="name",
        source_data_type="VARCHAR(50)",
        is_nullable=False,
        status=MappingStatus.CONFIRMED,
        confidence_score=0.99,
        rationale="no spec",
    )


@pytest.fixture
def normaliser():
    return DateNormaliser()


# ---------------------------------------------------------------------------
# can_transform
# ---------------------------------------------------------------------------

class TestCanTransform:
    def test_returns_true_when_spec_present(self, normaliser, date_mapping):
        assert normaliser.can_transform(date_mapping) is True

    def test_returns_false_when_no_spec(self, normaliser):
        mapping = _mapping_no_spec()
        assert normaliser.can_transform(mapping) is False

    def test_returns_true_for_none_value_but_spec_present(self, normaliser, date_mapping):
        # can_transform is independent of value — it's a mapping-level predicate
        assert normaliser.can_transform(date_mapping) is True


# ---------------------------------------------------------------------------
# ISO input
# ---------------------------------------------------------------------------

class TestISOPassthrough:
    def test_iso_date_returns_same(self, normaliser):
        mapping = _mapping_with_spec()
        result = normaliser.transform("1978-03-14", mapping)
        assert result.outcome == TransformOutcome.TRANSFORMED
        assert result.transformed_value == "1978-03-14"

    def test_iso_datetime_preserves_time(self, normaliser):
        mapping = _mapping_with_spec()
        result = normaliser.transform("1978-03-14T12:00:00", mapping)
        assert result.outcome == TransformOutcome.TRANSFORMED
        assert result.transformed_value == "1978-03-14T12:00:00"

    def test_mysql_datetime_normalised_to_iso(self, normaliser):
        # MySQL returns DATETIME as "YYYY-MM-DD HH:MM:SS"; must be T-separated for xsd:dateTime
        mapping = _mapping_with_spec()
        result = normaliser.transform("2150-08-22 17:33:00", mapping)
        assert result.outcome == TransformOutcome.TRANSFORMED
        assert result.transformed_value == "2150-08-22T17:33:00"


# ---------------------------------------------------------------------------
# UK format → ISO
# ---------------------------------------------------------------------------

class TestUKFormat:
    def test_uk_date_converted_to_iso(self, normaliser):
        mapping = _mapping_with_spec(formats=["DD/MM/YYYY"])
        result = normaliser.transform("14/03/1982", mapping)
        assert result.outcome == TransformOutcome.TRANSFORMED
        assert result.transformed_value == "1982-03-14"

    def test_uk_date_via_dateutil(self, normaliser):
        mapping = _mapping_with_spec(strategy="dateutil.parser")
        result = normaliser.transform("14/03/1982", mapping)
        assert result.outcome == TransformOutcome.TRANSFORMED
        assert result.transformed_value == "1982-03-14"


# ---------------------------------------------------------------------------
# Unparseable → QUARANTINED
# ---------------------------------------------------------------------------

class TestUnparseable:
    def test_garbage_string_quarantined(self, normaliser):
        mapping = _mapping_with_spec(strategy="strptime")
        result = normaliser.transform("not-a-date", mapping)
        assert result.outcome == TransformOutcome.QUARANTINED
        assert result.transformed_value is None
        assert result.error_detail is not None
        assert "not-a-date" in result.error_detail

    def test_original_value_preserved_on_quarantine(self, normaliser):
        mapping = _mapping_with_spec(strategy="strptime")
        result = normaliser.transform("xxxx", mapping)
        assert result.original_value == "xxxx"


# ---------------------------------------------------------------------------
# None value — can_transform returns True (spec is present) but transform
# handles None gracefully by emitting NULL_PRESERVED
# ---------------------------------------------------------------------------

class TestNoneValue:
    def test_none_value_returns_null_preserved(self, normaliser):
        mapping = _mapping_with_spec()
        result = normaliser.transform(None, mapping)
        assert result.outcome == TransformOutcome.NULL_PRESERVED
        assert result.transformed_value is None
