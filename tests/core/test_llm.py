# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for core.llm backend-specific request shaping.

Regression: the iterative ontology generator passes disable_thinking=True to
suppress qwen3's reasoning trace. That adds {"think": false} to the request body,
which is an Ollama/vLLM-only field. The OpenAI cloud API rejects unknown body
fields with HTTP 400 (it does NOT ignore them), so `think` (and options.num_ctx)
must be sent ONLY when a local endpoint is configured via LLM_BASE_URL.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from semantic_mapper.config.settings import Settings
from semantic_mapper.core import llm as llm_mod


def _fake_response():
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = "{}"
    resp.usage.prompt_tokens = 1
    resp.usage.completion_tokens = 1
    return resp


def _run(settings, **call_kwargs) -> dict:
    """Invoke call_llm with a mocked OpenAI client; return the kwargs sent to create()."""
    captured: dict = {}
    client = MagicMock()

    def _create(**kwargs):
        captured.update(kwargs)
        return _fake_response()

    client.chat.completions.create.side_effect = _create
    with patch.object(llm_mod, "make_openai_client", return_value=client):
        llm_mod.call_llm("hello", settings, **call_kwargs)
    return captured


def _settings(**overrides) -> Settings:
    base = dict(OPENAI_API_KEY="test-key", LLM_STREAM=False, QUERY_PROVIDER="openai")
    base.update(overrides)
    return Settings(**base)


class TestThinkBackendGating:
    def test_think_omitted_for_openai(self):
        """No LLM_BASE_URL → OpenAI → `think` must NOT be in the request body."""
        kwargs = _run(_settings(LLM_BASE_URL=None), disable_thinking=True)
        assert "think" not in kwargs.get("extra_body", {})

    def test_think_sent_for_local_backend(self):
        """LLM_BASE_URL set → local Ollama/vLLM → `think: false` IS sent."""
        kwargs = _run(
            _settings(LLM_BASE_URL="http://localhost:11434/v1"), disable_thinking=True
        )
        assert kwargs.get("extra_body", {}).get("think") is False

    def test_no_extra_body_when_not_disabling_thinking(self):
        """Default OpenAI call with no local extensions sends no extra_body at all."""
        kwargs = _run(_settings(LLM_BASE_URL=None), disable_thinking=False)
        assert "extra_body" not in kwargs


class TestNumCtxBackendGating:
    def test_num_ctx_omitted_for_openai(self):
        """num_ctx (options) is an Ollama field → omitted for OpenAI even if set."""
        kwargs = _run(_settings(LLM_BASE_URL=None, LLM_NUM_CTX=32768), disable_thinking=False)
        assert "options" not in kwargs.get("extra_body", {})

    def test_num_ctx_sent_for_local_backend(self):
        kwargs = _run(
            _settings(LLM_BASE_URL="http://localhost:11434/v1", LLM_NUM_CTX=32768),
            disable_thinking=False,
        )
        assert kwargs.get("extra_body", {}).get("options") == {"num_ctx": 32768}
