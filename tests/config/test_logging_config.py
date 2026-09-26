# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 1 — logging foundation tests."""
from __future__ import annotations

import json
import logging
import sys
from types import SimpleNamespace

import pytest


def _settings(tmp_path, **overrides):
    base = {
        "LOG_LEVEL": "INFO",
        "LOG_DIR": str(tmp_path / "logs"),
        "LOG_FILE_MAX_BYTES": 10_000_000,
        "LOG_FILE_BACKUP_COUNT": 5,
        "LOG_TO_STDOUT": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _clean_root_logger():
    root = logging.getLogger()
    old_handlers = root.handlers[:]
    old_level = root.level
    old_filters = root.filters[:]
    yield
    root.handlers = old_handlers
    root.level = old_level
    root.filters = old_filters


def test_setup_logging_creates_log_dir(tmp_path):
    from semantic_mapper.config.logging_config import setup_logging

    setup_logging(_settings(tmp_path))
    assert (tmp_path / "logs").is_dir()


def test_json_formatter_includes_request_id_when_set():
    from semantic_mapper.config.log_context import ContextFilter, request_id_var
    from semantic_mapper.config.log_formatters import JsonFormatter

    formatter = JsonFormatter()
    ctx_filter = ContextFilter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="", lineno=0,
        msg="hello", args=(), exc_info=None,
    )
    token = request_id_var.set("abc12345")
    try:
        ctx_filter.filter(record)
        output = formatter.format(record)
    finally:
        request_id_var.reset(token)

    data = json.loads(output)
    assert data["request_id"] == "abc12345"
    assert data["message"] == "hello"
    assert data["level"] == "INFO"


def test_human_formatter_renders_multiline_for_traceback():
    from semantic_mapper.config.log_formatters import HumanFormatter

    formatter = HumanFormatter()
    try:
        raise ValueError("test error")
    except ValueError:
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="test", level=logging.ERROR, pathname="", lineno=0,
        msg="something failed", args=(), exc_info=exc_info,
    )
    record.request_id = None
    record.query_id = None
    output = formatter.format(record)

    assert "something failed" in output
    assert "ValueError" in output
    assert "test error" in output
    assert "\n" in output


def test_log_level_env_override(tmp_path, monkeypatch):
    from semantic_mapper.config.logging_config import setup_logging

    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    setup_logging(_settings(tmp_path, LOG_LEVEL="INFO"))
    assert logging.getLogger().level == logging.DEBUG


def test_rotation_kicks_in_at_max_bytes(tmp_path):
    from semantic_mapper.config.logging_config import setup_logging

    setup_logging(_settings(tmp_path, LOG_FILE_MAX_BYTES=500, LOG_FILE_BACKUP_COUNT=3))
    logger = logging.getLogger("test.rotation")
    for _ in range(200):
        logger.info("x" * 50)

    log_files = list((tmp_path / "logs").glob("webapp.log*"))
    assert len(log_files) > 1, "Expected rotation to produce at least one backup file"


def test_existing_logger_calls_still_work(tmp_path):
    from semantic_mapper.config.logging_config import setup_logging

    setup_logging(_settings(tmp_path))
    logging.getLogger("existing.logger").info("test message from existing logger")

    log_file = tmp_path / "logs" / "webapp.log"
    assert log_file.exists()
    content = log_file.read_text()
    assert "test message from existing logger" in content
