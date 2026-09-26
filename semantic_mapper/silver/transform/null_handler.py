# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from semantic_mapper.schema.mapping_output import PropertyMapping
from semantic_mapper.silver.models import TransformOutcome, TransformResult
from semantic_mapper.silver.transform.base import BaseTransformer

_TRANSFORMER_NAME = "NullHandler"


class NullHandler(BaseTransformer):
    """Catch-all transformer. Handles NULL semantics and plain passthrough."""

    def can_transform(self, mapping: PropertyMapping) -> bool:
        return True  # always last in chain

    def transform(self, value: object, mapping: PropertyMapping) -> TransformResult:
        if value is None:
            null_semantic = (
                "explicit_null" if mapping.is_nullable else "unexpected_null"
            )
            return TransformResult(
                outcome=TransformOutcome.NULL_PRESERVED,
                transformed_value=None,
                original_value=value,
                transformer_name=_TRANSFORMER_NAME,
                rationale=f"NULL value preserved ({null_semantic}).",
                null_metadata={
                    "is_nullable": mapping.is_nullable,
                    "source_column": mapping.source_column,
                    "null_semantic": null_semantic,
                },
            )

        return TransformResult(
            outcome=TransformOutcome.PASSTHROUGH,
            transformed_value=value,
            original_value=value,
            transformer_name=_TRANSFORMER_NAME,
            rationale="No transformation required; value passed through unchanged.",
        )
