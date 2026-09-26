# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel


class RdfPipelineStats(BaseModel, frozen=True):
    run_id: str
    triples_written: int
    tables_processed: List[str]
    named_graph: str
    started_at: datetime
    finished_at: Optional[datetime] = None
    duration_seconds: Optional[float] = None
