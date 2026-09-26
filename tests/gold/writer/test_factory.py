# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from semantic_mapper.config.settings import Settings
from semantic_mapper.core.exceptions import MappingConfigError
from semantic_mapper.gold.writer.factory import _REGISTRY, get_gold_writer
from semantic_mapper.gold.writer.mysql import MySQLGoldWriter


def _settings(**kwargs):
    defaults = {
        "DATABASE_URL": "sqlite:///:memory:",
        "SILVER_DATABASE_URL": "sqlite:///:memory:",
        "GOLD_DATABASE_URL": "sqlite:///:memory:",
        "GOLD_WRITER": "mysql",
    }
    defaults.update(kwargs)
    return Settings(**defaults)


class TestRegistry:
    def test_mysql_in_registry(self):
        assert "mysql" in _REGISTRY
        assert _REGISTRY["mysql"] is MySQLGoldWriter


class TestGetGoldWriter:
    def test_mysql_returns_mysql_gold_writer(self):
        writer = get_gold_writer(_settings(GOLD_WRITER="mysql"))
        assert isinstance(writer, MySQLGoldWriter)

    def test_unknown_writer_raises_mapping_config_error(self):
        with pytest.raises(MappingConfigError, match="unknown_backend"):
            get_gold_writer(_settings(GOLD_WRITER="unknown_backend"))

    def test_injected_engine_used(self):
        engine = create_engine("sqlite:///:memory:")
        writer = get_gold_writer(_settings(GOLD_WRITER="mysql"), engine=engine)
        assert isinstance(writer, MySQLGoldWriter)
        assert writer._engine is engine

    def test_no_engine_creates_from_settings(self):
        writer = get_gold_writer(_settings(GOLD_WRITER="mysql"))
        assert isinstance(writer, MySQLGoldWriter)
        assert writer._engine is not None

    def test_error_message_lists_known_writers(self):
        with pytest.raises(MappingConfigError) as exc_info:
            get_gold_writer(_settings(GOLD_WRITER="snowflake_fake"))
        assert "mysql" in str(exc_info.value)
