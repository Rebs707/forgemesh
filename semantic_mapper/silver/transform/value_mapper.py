# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import logging

from semantic_mapper.schema.mapping_output import PropertyMapping
from semantic_mapper.silver.models import TransformOutcome, TransformResult
from semantic_mapper.silver.transform.base import BaseTransformer

logger = logging.getLogger(__name__)

_TRANSFORMER_NAME = "ValueMapper"

# String values that represent NULL semantics in source systems (e.g. MIMIC-III).
# Matched case-insensitively after stripping whitespace.
_NULL_SENTINELS: frozenset[str] = frozenset({
    "none", "null", "n/a", "na", "nan", "", "unknown", "-",
})


class ValueMapper(BaseTransformer):
    """Resolves ValueMappingRule discriminator lists to mapped URIs."""

    def can_transform(self, mapping: PropertyMapping) -> bool:
        return bool(mapping.value_mappings)

    def transform(self, value: object, mapping: PropertyMapping) -> TransformResult:
        # Treat Python None and null-sentinel strings as missing data rather than quarantining.
        if value is None or (isinstance(value, str) and value.strip().lower() in _NULL_SENTINELS):
            return TransformResult(
                outcome=TransformOutcome.NULL_PRESERVED,
                transformed_value=None,
                original_value=value,
                transformer_name=_TRANSFORMER_NAME,
                rationale=f"Null value {value!r} treated as NULL (no value to map).",
                null_metadata={
                    "is_nullable": mapping.is_nullable,
                    "source_column": mapping.source_column,
                    "null_semantic": "null_sentinel",
                },
            )

        known: list[str] = []

        for rule in mapping.value_mappings:
            sv = rule.source_value
            known.append(repr(sv))

            # Equality with int/str coercion
            if value == sv:
                return TransformResult(
                    outcome=TransformOutcome.TRANSFORMED,
                    transformed_value=rule.mapped_uri,
                    original_value=value,
                    transformer_name=_TRANSFORMER_NAME,
                    rationale=(
                        f"Exact match on source_value {sv!r} → {rule.mapped_uri} "
                        f"(confidence={rule.confidence_score:.2f}; {rule.rationale})"
                    ),
                )

            # Coercion: try str vs int match
            try:
                if str(value) == str(sv):
                    return TransformResult(
                        outcome=TransformOutcome.TRANSFORMED,
                        transformed_value=rule.mapped_uri,
                        original_value=value,
                        transformer_name=_TRANSFORMER_NAME,
                        rationale=(
                            f"String-coercion match on source_value {sv!r} → {rule.mapped_uri} "
                            f"(confidence={rule.confidence_score:.2f}; {rule.rationale})"
                        ),
                    )
            except Exception:
                pass

            try:
                if isinstance(sv, (int, float)) and int(str(value)) == int(sv):
                    return TransformResult(
                        outcome=TransformOutcome.TRANSFORMED,
                        transformed_value=rule.mapped_uri,
                        original_value=value,
                        transformer_name=_TRANSFORMER_NAME,
                        rationale=(
                            f"Int-coercion match on source_value {sv!r} → {rule.mapped_uri} "
                            f"(confidence={rule.confidence_score:.2f}; {rule.rationale})"
                        ),
                    )
            except Exception:
                pass

            # Case-insensitive string match: handles casing variants produced by
            # different source systems or LLM-generated rules built from a single
            # sample casing (e.g. "Chemistry" rule matching "CHEMISTRY" in data).
            if isinstance(value, str) and isinstance(sv, str) and value.strip().lower() == sv.strip().lower():
                return TransformResult(
                    outcome=TransformOutcome.TRANSFORMED,
                    transformed_value=rule.mapped_uri,
                    original_value=value,
                    transformer_name=_TRANSFORMER_NAME,
                    rationale=(
                        f"Case-insensitive match on source_value {sv!r} → {rule.mapped_uri} "
                        f"(confidence={rule.confidence_score:.2f}; {rule.rationale})"
                    ),
                )

        return TransformResult(
            outcome=TransformOutcome.PASSTHROUGH,
            transformed_value=value,
            original_value=value,
            transformer_name=_TRANSFORMER_NAME,
            rationale=(
                f"Value {value!r} not found in value_mappings; passed through as plain string. "
                f"Known mapped values: [{', '.join(known)}]"
            ),
        )
