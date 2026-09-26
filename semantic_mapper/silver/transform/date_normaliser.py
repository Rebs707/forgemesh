# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import logging
from datetime import datetime

from semantic_mapper.schema.mapping_output import PropertyMapping
from semantic_mapper.silver.models import TransformOutcome, TransformResult
from semantic_mapper.silver.transform.base import BaseTransformer

logger = logging.getLogger(__name__)

_TRANSFORMER_NAME = "DateNormaliser"


class DateNormaliser(BaseTransformer):
    """Parses date values using TransformationSpec and outputs ISO-8601 strings."""

    def can_transform(self, mapping: PropertyMapping) -> bool:
        return mapping.transformation_spec is not None and mapping.transformation_spec is not None

    def transform(self, value: object, mapping: PropertyMapping) -> TransformResult:
        if value is None:
            # Delegate to NullHandler — this path should not be reached if
            # can_transform correctly returns False for None values, but
            # guard defensively.
            return TransformResult(
                outcome=TransformOutcome.NULL_PRESERVED,
                transformed_value=None,
                original_value=value,
                transformer_name=_TRANSFORMER_NAME,
                rationale="None value passed to DateNormaliser; delegating null semantics.",
                null_metadata={
                    "is_nullable": mapping.is_nullable,
                    "source_column": mapping.source_column,
                    "null_semantic": "unexpected_null",
                },
            )

        spec = mapping.transformation_spec  # type: ignore[assignment]
        str_value = str(value).strip()

        # Fast path: already ISO-8601 (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)
        iso_result = self._try_iso(str_value)
        if iso_result is not None:
            return TransformResult(
                outcome=TransformOutcome.TRANSFORMED,
                transformed_value=iso_result,
                original_value=value,
                transformer_name=_TRANSFORMER_NAME,
                rationale="Value already in ISO-8601 format; passthrough with type normalisation.",
            )

        # Strategy: dateutil
        if spec.parse_strategy == "dateutil.parser":
            result = self._try_dateutil(str_value)
            if result is not None:
                return TransformResult(
                    outcome=TransformOutcome.TRANSFORMED,
                    transformed_value=result,
                    original_value=value,
                    transformer_name=_TRANSFORMER_NAME,
                    rationale=f"Parsed with dateutil.parser → {result}",
                )

        # Strategy: strptime loop over detected_formats
        for fmt in spec.detected_formats:
            result = self._try_strptime(str_value, fmt)
            if result is not None:
                return TransformResult(
                    outcome=TransformOutcome.TRANSFORMED,
                    transformed_value=result,
                    original_value=value,
                    transformer_name=_TRANSFORMER_NAME,
                    rationale=f"Parsed with strptime format '{fmt}' → {result}",
                )

        # All parsing attempts failed → quarantine
        tried = spec.detected_formats or ["<none>"]
        return TransformResult(
            outcome=TransformOutcome.QUARANTINED,
            transformed_value=None,
            original_value=value,
            transformer_name=_TRANSFORMER_NAME,
            rationale="Could not parse date value with any configured strategy.",
            error_detail=(
                f"Unparseable date '{str_value}'. "
                f"Tried formats: {tried}, strategy: {spec.parse_strategy}"
            ),
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _try_iso(s: str) -> str | None:
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                if fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                    # Preserve time component; normalise to T-separated ISO 8601
                    return dt.isoformat()
                return dt.date().isoformat()
            except ValueError:
                pass
        return None

    @staticmethod
    def _try_dateutil(s: str) -> str | None:
        try:
            from dateutil import parser as du_parser  # lazy import

            dt = du_parser.parse(s, dayfirst=True)
            return dt.date().isoformat()
        except Exception:
            return None

    @staticmethod
    def _try_strptime(s: str, fmt: str) -> str | None:
        # Map human-readable format tokens to strptime directives
        _FMT_MAP = {
            "YYYY-MM-DD": "%Y-%m-%d",
            "DD/MM/YYYY": "%d/%m/%Y",
            "MM/DD/YYYY": "%m/%d/%Y",
            "YYYY/MM/DD": "%Y/%m/%d",
            "DD-MM-YYYY": "%d-%m-%Y",
        }
        strptime_fmt = _FMT_MAP.get(fmt, fmt)
        try:
            dt = datetime.strptime(s, strptime_fmt)
            return dt.date().isoformat()
        except ValueError:
            return None
