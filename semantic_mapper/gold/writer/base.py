# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from abc import ABC, abstractmethod
from typing import Dict, List, Sequence

from semantic_mapper.gold.models import GoldPipelineStats, GoldTableDefinition, GoldWriteResult


class GoldWriter(ABC):
    """
    Abstract writer interface for the gold layer.

    Extension point
    ---------------
    Register alternative implementations (Snowflake, BigQuery, Data Vault) via
    the _REGISTRY in factory.py.
    """

    @abstractmethod
    def initialise(self, definitions: List[GoldTableDefinition]) -> None:
        """Create gold tables. Idempotent. Raises GoldSchemaInitialisationError."""
        ...

    @abstractmethod
    def write_batch(self, table_name: str, rows: Sequence[dict]) -> GoldWriteResult:
        """
        Write a batch of gold rows (already-wide dicts).

        Raises GoldWriterError on catastrophic failure only.
        """
        ...

    @abstractmethod
    def finalise(self, run_id: str, rows_per_table: Dict[str, int]) -> GoldPipelineStats:
        """Commit, return GoldPipelineStats with duration populated."""
        ...
