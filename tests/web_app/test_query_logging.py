# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 3 — query lifecycle logging tests."""
from __future__ import annotations

import logging
import sys

import pytest


@pytest.fixture()
def qlog_records():
    from semantic_mapper.config.log_context import ContextFilter

    captured = []

    class _H(logging.Handler):
        def emit(self, r):
            captured.append(r)

    h = _H()
    h.addFilter(ContextFilter())
    logger = logging.getLogger("webapp.query")
    logger.addHandler(h)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    yield captured
    logger.removeHandler(h)
    logger.setLevel(old_level)
    logger.propagate = True


def _qlog(qid="testqid"):
    from semantic_mapper.webapp.log_helpers import QueryLifecycleLogger
    return QueryLifecycleLogger(qid, "sparql")


def test_successful_query_logs_received_generated_completed(qlog_records):
    with _qlog() as qlog:
        qlog.received("how many rows")
        qlog.generated("SELECT (COUNT(*) AS ?n) WHERE { }")
        qlog.completed(42, prompt_tokens=150, completion_tokens=30)

    assert len(qlog_records) == 3
    msgs = [r.getMessage() for r in qlog_records]
    assert msgs[0] == "query received"
    assert msgs[1] == "query generated"
    assert msgs[2] == "query completed"
    completed = qlog_records[2]
    assert completed.prompt_tokens == 150
    assert completed.completion_tokens == 30
    assert completed.total_tokens == 180


def test_failed_query_logs_received_generated_failed(qlog_records):
    with _qlog() as qlog:
        qlog.received("what is x")
        qlog.generated("SELECT ?x WHERE { }")
        qlog.failed(ValueError("Fuseki error"), generated_query="SELECT ?x WHERE { }")

    assert len(qlog_records) == 3
    msgs = [r.getMessage() for r in qlog_records]
    assert "received" in msgs[0]
    assert "generated" in msgs[1]
    assert "failed" in msgs[2]
    assert qlog_records[2].levelno == logging.ERROR


def test_failed_before_generation_logs_received_failed_only(qlog_records):
    with _qlog() as qlog:
        qlog.received("what is x")
        qlog.failed(ConnectionError("DB unreachable"))

    assert len(qlog_records) == 2
    msgs = [r.getMessage() for r in qlog_records]
    assert "received" in msgs[0]
    assert "failed" in msgs[1]


def test_query_id_appears_on_all_lifecycle_lines(qlog_records):
    with _qlog("myuniqid") as qlog:
        qlog.received("test question")
        qlog.generated("SELECT ?x WHERE { }")
        qlog.completed(5)

    for r in qlog_records:
        # ContextFilter sets record.query_id from the ContextVar
        assert getattr(r, "query_id", None) == "myuniqid"


def test_traceback_in_failed_log(qlog_records):
    qlog = _qlog()
    qlog.received("test")
    try:
        raise RuntimeError("something broke")
    except RuntimeError as exc:
        qlog.failed(exc)
    qlog.close()

    failed = [r for r in qlog_records if "failed" in r.getMessage()]
    assert failed, "Expected a 'query failed' log record"
    assert failed[0].exc_info is not None
    assert issubclass(failed[0].exc_info[0], RuntimeError)
