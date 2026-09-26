# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from semantic_mapper.silver.writer.base import SilverWriter


class TestSilverWriterABC:
    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            SilverWriter()  # type: ignore[abstract]

    def test_partial_implementation_raises(self):
        class PartialWriter(SilverWriter):
            def initialise(self, report):
                pass

            def write_batch(self, rows):
                pass

            # Missing write_quarantine and finalise

        with pytest.raises(TypeError):
            PartialWriter()

    def test_full_implementation_instantiates(self):
        from datetime import datetime, timezone

        from semantic_mapper.silver.models import BatchWriteResult, RunStats

        class ConcreteWriter(SilverWriter):
            def initialise(self, report):
                pass

            def write_batch(self, rows):
                return BatchWriteResult(
                    rows_written=len(rows),
                    target_table="t",
                    run_id="r",
                    written_at=datetime.now(timezone.utc),
                )

            def write_quarantine(self, records):
                return BatchWriteResult(
                    rows_written=0,
                    target_table="q",
                    run_id="r",
                    written_at=datetime.now(timezone.utc),
                )

            def finalise(self):
                return RunStats(
                    run_id="r",
                    source_table="t",
                    bronze_rows_read=0,
                    batch_results=[],
                    started_at=datetime.now(timezone.utc),
                )

        writer = ConcreteWriter()
        assert writer is not None
