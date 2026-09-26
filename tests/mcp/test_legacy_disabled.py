# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 6 tests — MCP_LEGACY_QUERY_TOOL setting."""
from __future__ import annotations

import pytest

from semantic_mapper.config.settings import Settings


def test_mcp_legacy_query_tool_default_is_true():
    s = Settings(
        DATABASE_URL="sqlite:///:memory:",
        SILVER_DATABASE_URL="sqlite:///:memory:",
        GOLD_DATABASE_URL="sqlite:///:memory:",
    )
    assert s.MCP_LEGACY_QUERY_TOOL is True


def test_mcp_legacy_query_tool_can_be_disabled():
    s = Settings(
        DATABASE_URL="sqlite:///:memory:",
        SILVER_DATABASE_URL="sqlite:///:memory:",
        GOLD_DATABASE_URL="sqlite:///:memory:",
        MCP_LEGACY_QUERY_TOOL=False,
    )
    assert s.MCP_LEGACY_QUERY_TOOL is False


def test_mcp_legacy_false_does_not_require_llm_api_key():
    """When legacy is disabled, the LLM API key is Optional — Settings constructs without it.

    The key is the canonical LLM_API_KEY field (legacy OPENAI_API_KEY remains a
    read/write alias). Both must be absent-tolerant so the air-gapped MCP server
    can start with no key.
    """
    # Constructs cleanly with no key supplied at all (_env_file=None so a local
    # .env with a real key doesn't bleed into the test).
    s = Settings(
        _env_file=None,
        DATABASE_URL="sqlite:///:memory:",
        SILVER_DATABASE_URL="sqlite:///:memory:",
        GOLD_DATABASE_URL="sqlite:///:memory:",
        MCP_LEGACY_QUERY_TOOL=False,
    )
    assert s.LLM_API_KEY is None
    assert s.OPENAI_API_KEY is None  # legacy read alias resolves

    # The canonical field is declared Optional[str].
    field = Settings.model_fields["LLM_API_KEY"]
    args = getattr(field.annotation, "__args__", ())
    assert type(None) in args, "LLM_API_KEY must be Optional[str]"


def test_new_tools_do_not_require_legacy_setting():
    """execute_sparql and execute_sql_* tools exist regardless of legacy setting."""
    from semantic_mapper.mcp.tools.execute_sparql import run_execute_sparql
    from semantic_mapper.mcp.tools.execute_sql import run_execute_sql
    assert callable(run_execute_sparql)
    assert callable(run_execute_sql)


def test_schema_builder_does_not_require_openai():
    """build_agent_schema makes no OpenAI calls."""
    from semantic_mapper.mcp.schema_builder import build_agent_schema
    from tests.mcp.conftest import PATIENT_DEF, ADMISSION_DEF
    # Call with no mapping_report — should not raise
    schema = build_agent_schema(
        definitions=[PATIENT_DEF, ADMISSION_DEF],
        mapping_report=None,
        named_graph="http://mesh.local/graph/current",
        base_uri="http://mesh.local/data/",
    )
    assert schema["classes"]
    assert schema["construction_rules"]
