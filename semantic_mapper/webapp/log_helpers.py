# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
import collections
import logging
import time
from typing import Optional

from semantic_mapper.config.log_context import query_id_var


class QueryLifecycleLogger:
    def __init__(self, query_id: str, mode: str):
        self.query_id = query_id
        self.mode = mode
        self.start = time.monotonic()
        self._token = query_id_var.set(query_id)
        self.logger = logging.getLogger("webapp.query")

    def received(self, question: str) -> None:
        self.logger.info(
            "query received",
            extra={"event": "received", "mode": self.mode, "question": question},
        )

    def generated(self, generated_query: str) -> None:
        self.logger.info(
            "query generated",
            extra={"event": "generated", "mode": self.mode, "generated_query": generated_query},
        )

    def completed(self, row_count: int, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        duration_ms = int((time.monotonic() - self.start) * 1000)
        self.logger.info(
            "query completed",
            extra={
                "event": "completed",
                "mode": self.mode,
                "row_count": row_count,
                "duration_ms": duration_ms,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        )

    def failed(self, error: Exception, generated_query: Optional[str] = None) -> None:
        duration_ms = int((time.monotonic() - self.start) * 1000)
        self.logger.error(
            "query failed",
            exc_info=error,
            extra={
                "event": "failed",
                "mode": self.mode,
                "generated_query": generated_query,
                "duration_ms": duration_ms,
                "error_type": type(error).__name__,
            },
        )

    def close(self) -> None:
        query_id_var.reset(self._token)

    def __enter__(self) -> "QueryLifecycleLogger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def capture_subprocess_logs(
    proc,
    run_id: str,
    log_list: list,
    tail_deque: collections.deque,
    lock=None,
) -> None:
    """Read subprocess stdout line by line and re-log through webapp logger.

    Designed to run in a daemon thread. Also appends to log_list (under lock
    if provided) so the existing pipeline log streaming is preserved.
    """
    cap_logger = logging.getLogger("pipeline.subprocess")
    for line in proc.stdout:
        line = line.rstrip("\n")
        if not line:
            continue
        if lock is not None:
            with lock:
                log_list.append(line)
        else:
            log_list.append(line)
        tail_deque.append(line)
        cap_logger.info(line, extra={"run_id": run_id, "subprocess": True})
