# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest
from pydantic import ValidationError

from semantic_mapper.query.models import QueryResult


def _make_result(**kwargs) -> QueryResult:
    defaults = dict(
        question="How many customers?",
        generated_sql="SELECT COUNT(*) FROM silver_raw_party LIMIT 10",
        explanation="Counts all rows.",
        rows=[{"count": 42}],
        row_count=1,
        tables_used=["silver_raw_party"],
    )
    defaults.update(kwargs)
    return QueryResult(**defaults)


class TestQueryResultFrozen:
    def test_frozen_raises_on_mutation(self):
        result = _make_result()
        with pytest.raises((ValidationError, TypeError)):
            result.row_count = 99  # type: ignore[misc]

    def test_error_none_on_success(self):
        result = _make_result()
        assert result.error is None

    def test_error_set_on_failure(self):
        result = _make_result(rows=[], row_count=0, error="Something went wrong")
        assert result.error == "Something went wrong"

    def test_model_dump_serialisable(self):
        result = _make_result()
        dumped = result.model_dump()
        assert isinstance(dumped, dict)
        assert dumped["question"] == "How many customers?"
        assert dumped["rows"] == [{"count": 42}]

    def test_default_token_fields(self):
        result = _make_result()
        assert result.prompt_tokens == 0
        assert result.completion_tokens == 0

    def test_token_fields_set(self):
        result = _make_result(prompt_tokens=150, completion_tokens=80)
        assert result.prompt_tokens == 150
        assert result.completion_tokens == 80
