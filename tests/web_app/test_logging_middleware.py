# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 2 — request middleware tests."""
from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient


def _make_app() -> FastAPI:
    from semantic_mapper.webapp.middleware import request_logging_middleware

    app = FastAPI()
    app.middleware("http")(request_logging_middleware)

    @app.get("/test")
    async def ok():
        return {"ok": True}

    @app.get("/status/{code}")
    async def with_code(code: int):
        return Response(status_code=code)

    return app


@pytest.fixture()
def records():
    from semantic_mapper.config.log_context import ContextFilter

    captured = []

    class _H(logging.Handler):
        def emit(self, r):
            captured.append(r)

    h = _H()
    h.addFilter(ContextFilter())
    root = logging.getLogger()
    root.addHandler(h)
    old_level = root.level
    root.setLevel(logging.DEBUG)
    yield captured
    root.removeHandler(h)
    root.setLevel(old_level)


def test_get_request_logs_received_and_completed(records):
    client = TestClient(_make_app())
    client.get("/test")

    req_records = [r for r in records if r.name == "webapp.request"]
    assert len(req_records) >= 2
    messages = [r.getMessage() for r in req_records]
    assert any("received" in m for m in messages)


def test_request_id_propagates_to_inner_log_calls(records):
    client = TestClient(_make_app())
    client.get("/test")

    req_records = [r for r in records if r.name == "webapp.request"]
    ids = {getattr(r, "request_id", None) for r in req_records} - {None}
    assert len(ids) == 1, f"Expected one unique request_id, got: {ids}"
    assert len(next(iter(ids))) == 8


def test_4xx_logs_at_warning(records):
    client = TestClient(_make_app(), raise_server_exceptions=False)
    client.get("/status/404")

    response_records = [
        r for r in records
        if r.name == "webapp.request" and "404" in r.getMessage()
    ]
    assert any(r.levelno == logging.WARNING for r in response_records)


def test_5xx_logs_at_error(records):
    client = TestClient(_make_app(), raise_server_exceptions=False)
    client.get("/status/500")

    response_records = [
        r for r in records
        if r.name == "webapp.request" and "500" in r.getMessage()
    ]
    assert any(r.levelno == logging.ERROR for r in response_records)


def test_duration_ms_in_extra(records):
    client = TestClient(_make_app())
    client.get("/test")

    timed = [r for r in records if r.name == "webapp.request" and hasattr(r, "duration_ms")]
    assert timed, "Expected at least one record with duration_ms"
    assert all(r.duration_ms >= 0 for r in timed)
