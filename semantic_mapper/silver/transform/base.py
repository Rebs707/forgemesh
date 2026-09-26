# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from abc import ABC, abstractmethod

from semantic_mapper.schema.mapping_output import PropertyMapping
from semantic_mapper.silver.models import TransformResult


class BaseTransformer(ABC):
    @abstractmethod
    def can_transform(self, mapping: PropertyMapping) -> bool:
        """Pure predicate, no side effects."""
        ...

    @abstractmethod
    def transform(self, value: object, mapping: PropertyMapping) -> TransformResult:
        """NEVER raises. Failures → TransformResult(outcome=QUARANTINED)."""
        ...
