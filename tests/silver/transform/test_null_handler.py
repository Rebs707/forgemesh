# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from semantic_mapper.schema.mapping_output import MappingStatus, PropertyMapping
from semantic_mapper.silver.models import TransformOutcome
from semantic_mapper.silver.transform.null_handler import NullHandler


def _mapping(nullable: bool = True):
    return PropertyMapping(
        source_column="some_col",
        source_data_type="VARCHAR(50)",
        is_nullable=nullable,
        status=MappingStatus.CONFIRMED,
        confidence_score=0.90,
        rationale="test",
    )


@pytest.fixture
def handler():
    return NullHandler()


# ---------------------------------------------------------------------------
# can_transform — always True
# ---------------------------------------------------------------------------

class TestCanTransform:
    def test_always_true_for_nullable(self, handler):
        assert handler.can_transform(_mapping(nullable=True)) is True

    def test_always_true_for_non_nullable(self, handler):
        assert handler.can_transform(_mapping(nullable=False)) is True

    def test_always_true_for_any_mapping(self, handler, date_mapping, plain_mapping):
        assert handler.can_transform(date_mapping) is True
        assert handler.can_transform(plain_mapping) is True


# ---------------------------------------------------------------------------
# None value → NULL_PRESERVED + null_metadata
# ---------------------------------------------------------------------------

class TestNoneValue:
    def test_none_returns_null_preserved(self, handler):
        result = handler.transform(None, _mapping())
        assert result.outcome == TransformOutcome.NULL_PRESERVED

    def test_transformed_value_is_none(self, handler):
        result = handler.transform(None, _mapping())
        assert result.transformed_value is None

    def test_null_metadata_present(self, handler):
        result = handler.transform(None, _mapping())
        assert result.null_metadata is not None

    def test_null_metadata_keys(self, handler):
        result = handler.transform(None, _mapping())
        assert "is_nullable" in result.null_metadata
        assert "source_column" in result.null_metadata
        assert "null_semantic" in result.null_metadata

    def test_explicit_null_semantic_when_nullable(self, handler):
        result = handler.transform(None, _mapping(nullable=True))
        assert result.null_metadata["null_semantic"] == "explicit_null"

    def test_unexpected_null_semantic_when_not_nullable(self, handler):
        result = handler.transform(None, _mapping(nullable=False))
        assert result.null_metadata["null_semantic"] == "unexpected_null"


# ---------------------------------------------------------------------------
# Non-None value → PASSTHROUGH
# ---------------------------------------------------------------------------

class TestNonNoneValue:
    def test_string_passthrough(self, handler):
        result = handler.transform("hello", _mapping())
        assert result.outcome == TransformOutcome.PASSTHROUGH
        assert result.transformed_value == "hello"

    def test_int_passthrough(self, handler):
        result = handler.transform(42, _mapping())
        assert result.outcome == TransformOutcome.PASSTHROUGH
        assert result.transformed_value == 42

    def test_value_unchanged(self, handler):
        obj = {"key": "value"}
        result = handler.transform(obj, _mapping())
        assert result.transformed_value is obj

    def test_no_null_metadata_on_passthrough(self, handler):
        result = handler.transform("x", _mapping())
        assert result.null_metadata is None
