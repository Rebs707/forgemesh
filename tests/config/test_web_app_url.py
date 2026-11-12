# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""The web app's self URL follows the port it serves on unless set explicitly."""
from __future__ import annotations

import pytest

from semantic_mapper.config.settings import Settings, apply_web_app_url


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("FORGEMESH_WEB_APP_URL", raising=False)


@pytest.mark.parametrize("host,port,expected", [
    ("127.0.0.1", 5174, "http://127.0.0.1:5174"),
    ("0.0.0.0", 8080, "http://127.0.0.1:8080"),
    ("localhost", 5173, "http://localhost:5173"),
    ("::1", 9000, "http://[::1]:9000"),
])
def test_derived_from_host_and_port(host, port, expected):
    s = Settings(_env_file=None)
    apply_web_app_url(s, host, port)
    assert s.FORGEMESH_WEB_APP_URL == expected


def test_explicit_value_wins(monkeypatch):
    monkeypatch.setenv("FORGEMESH_WEB_APP_URL", "http://webapp:5173")
    s = Settings(_env_file=None)
    apply_web_app_url(s, "127.0.0.1", 5174)
    assert s.FORGEMESH_WEB_APP_URL == "http://webapp:5173"
