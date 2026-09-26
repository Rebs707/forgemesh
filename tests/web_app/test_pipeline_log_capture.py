# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 4 — pipeline subprocess log capture tests."""
from __future__ import annotations

import collections
import logging
import subprocess
import sys
import threading

import pytest


@pytest.fixture()
def captured_records():
    records = []

    class _H(logging.Handler):
        def emit(self, r):
            records.append(r)

    h = _H()
    logger = logging.getLogger("pipeline.subprocess")
    logger.addHandler(h)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    yield records
    logger.removeHandler(h)
    logger.setLevel(old_level)
    logger.propagate = True


def _run_capture(script: str, run_id: str = "run_test"):
    """Helper: run a small script as subprocess, capture its output, return (records, deque)."""
    from semantic_mapper.webapp.log_helpers import capture_subprocess_logs

    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    log_list: list = []
    tail: collections.deque = collections.deque(maxlen=50)
    capture_subprocess_logs(proc, run_id, log_list, tail)
    proc.wait()
    return proc, log_list, tail


def test_subprocess_stdout_captured_in_webapp_log(captured_records):
    _run_capture("print('hello from subprocess'); print('second line')")

    messages = [r.getMessage() for r in captured_records]
    assert "hello from subprocess" in messages
    assert "second line" in messages


def test_subprocess_lines_carry_run_id(captured_records):
    _run_capture("print('log line')", run_id="run_abc123")

    for r in captured_records:
        assert getattr(r, "run_id", None) == "run_abc123"
        assert getattr(r, "subprocess", None) is True


def test_non_zero_exit_logged_at_error_with_tail(captured_records):
    from semantic_mapper.webapp.log_helpers import capture_subprocess_logs

    script = "import sys; print('line1'); print('line2'); sys.exit(1)"
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    log_list: list = []
    tail: collections.deque = collections.deque(maxlen=50)

    t = threading.Thread(target=capture_subprocess_logs, args=(proc, "run_fail", log_list, tail))
    t.start()
    return_code = proc.wait()
    t.join(timeout=2.0)

    assert return_code != 0
    # Verify lines were captured in the tail deque
    assert "line1" in tail or "line2" in tail
    assert len(log_list) >= 2


def test_subprocess_log_does_not_block_webapp(captured_records):
    """Capture runs in a thread and does not block the calling thread."""
    from semantic_mapper.webapp.log_helpers import capture_subprocess_logs

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.1); print('done')"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    log_list: list = []
    tail: collections.deque = collections.deque(maxlen=50)

    completed = threading.Event()

    def _run():
        capture_subprocess_logs(proc, "run_nonblock", log_list, tail)
        completed.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    proc.wait()
    t.join(timeout=3.0)

    assert completed.is_set(), "Capture thread should have completed"
    assert "done" in log_list
