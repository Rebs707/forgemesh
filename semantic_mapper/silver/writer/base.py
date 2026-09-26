# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from abc import ABC, abstractmethod
from typing import Sequence

from semantic_mapper.schema.mapping_output import SemanticMappingReport
from semantic_mapper.silver.models import BatchWriteResult, QuarantineRecord, RunStats, SilverRow


class SilverWriter(ABC):
    """
    Abstract writer interface for the silver layer.

    Data Vault extension point
    --------------------------
    A DataVaultSilverWriter receives identical SilverRow objects and routes internally:
      SilverRow.ontology_class_uri  → Hub table
      SilverRow.property_kind == OBJECT → Link table (two Hub hash keys)
      SilverRow.property_kind == DATATYPE → Satellite column
      SilverRow.audit.run_id        → DV load date / batch key
      SilverRow.audit.bronze_source_ref → business key ingredient for hash

    Register via: _REGISTRY["datavault"] = DataVaultSilverWriter in factory.py
    """

    @abstractmethod
    def initialise(self, report: SemanticMappingReport) -> None:
        """Create target schema. Idempotent. Raises SchemaInitialisationError."""
        ...

    @abstractmethod
    def write_batch(self, rows: Sequence[SilverRow]) -> BatchWriteResult:
        """
        Write a batch of silver rows.

        Individual row failures accumulate in BatchWriteResult.rows_failed.
        Raises WriterError on catastrophic failure only.
        """
        ...

    @abstractmethod
    def write_quarantine(self, records: Sequence[QuarantineRecord]) -> BatchWriteResult:
        """
        Write failed rows to quarantine table.

        Must NEVER raise — log at ERROR and return rows_failed count on any exception.
        """
        ...

    @abstractmethod
    def finalise(self) -> RunStats:
        """Commit, close connections, return RunStats with duration_seconds populated."""
        ...
