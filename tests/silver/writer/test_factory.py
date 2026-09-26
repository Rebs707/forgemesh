# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from semantic_mapper.config.settings import Settings
from semantic_mapper.core.exceptions import MappingConfigError
from semantic_mapper.silver.writer.factory import _REGISTRY, get_writer
from semantic_mapper.silver.writer.mysql import MySQLSilverWriter


def _settings(**kwargs):
    defaults = {
        "DATABASE_URL": "sqlite:///:memory:",
        "SILVER_DATABASE_URL": "sqlite:///:memory:",
        "SILVER_WRITER": "mysql",
    }
    defaults.update(kwargs)
    return Settings(**defaults)


class TestRegistry:
    def test_mysql_in_registry(self):
        assert "mysql" in _REGISTRY
        assert _REGISTRY["mysql"] is MySQLSilverWriter


class TestGetWriter:
    def test_mysql_returns_mysql_writer(self):
        writer = get_writer(_settings(SILVER_WRITER="mysql"))
        assert isinstance(writer, MySQLSilverWriter)

    def test_unknown_writer_raises_mapping_config_error(self):
        with pytest.raises(MappingConfigError, match="unknown_backend"):
            get_writer(_settings(SILVER_WRITER="unknown_backend"))

    def test_injected_engine_used(self):
        """When an engine is explicitly supplied, the writer uses it."""
        engine = create_engine("sqlite:///:memory:")
        writer = get_writer(_settings(SILVER_WRITER="mysql"), engine=engine)
        assert isinstance(writer, MySQLSilverWriter)
        assert writer._engine is engine

    def test_no_engine_creates_from_settings(self):
        """When no engine is supplied, one is created from SILVER_DATABASE_URL."""
        writer = get_writer(_settings(SILVER_WRITER="mysql"))
        assert isinstance(writer, MySQLSilverWriter)
        assert writer._engine is not None

    def test_error_message_lists_known_writers(self):
        with pytest.raises(MappingConfigError) as exc_info:
            get_writer(_settings(SILVER_WRITER="snowflake_fake"))
        assert "mysql" in str(exc_info.value)
