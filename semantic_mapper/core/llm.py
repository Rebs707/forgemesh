# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Shared LLM dispatch — routes to OpenAI or Claude Code based on QUERY_PROVIDER."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Optional

def repair_json(text: str) -> str:
    """Escape literal newlines/tabs within JSON string values.

    LLMs often emit pretty-printed SPARQL or SQL inside JSON strings with
    real newlines, which is invalid JSON.  Walking char-by-char lets us
    repair only inside strings without touching structural whitespace.
    """
    out: list[str] = []
    in_str = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            out.append(ch)
            out.append(text[i + 1])
            i += 2
            continue
        if ch == '"':
            in_str = not in_str
        if in_str:
            if ch == "\n":
                out.append("\\n"); i += 1; continue
            if ch == "\r":
                out.append("\\r"); i += 1; continue
            if ch == "\t":
                out.append("\\t"); i += 1; continue
        out.append(ch)
        i += 1
    return "".join(out)


def _claude_exe() -> str:
    """Return the full path to the claude CLI, checking PATH then common install locations."""
    found = shutil.which("claude")
    if found:
        return found
    for candidate in [
        os.path.expanduser("~/.local/bin/claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
    ]:
        if os.path.isfile(candidate):
            return candidate
    return "claude"  # last resort — will raise FileNotFoundError with a clear message


@dataclass
class LLMResult:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


def make_openai_client(settings: Any):
    """Construct an OpenAI-SDK client, optionally pointed at a local endpoint.

    The SDK is the wire protocol, not a vendor commitment — it speaks to any
    OpenAI-compatible server. Choosing a backend is config-only:
      - settings.LLM_BASE_URL unset → cloud OpenAI (api.openai.com)
      - settings.LLM_BASE_URL set   → that OpenAI-compatible endpoint
        (Ollama at http://localhost:11434/v1, vLLM, LM Studio, llama.cpp …)

    Routing it at a local server makes every LLM call offline / air-gapped.
    Local servers ignore the API key, but the SDK requires a non-empty string,
    so a placeholder is supplied when a base_url is set and no key is given.

    Reads LLM_BASE_URL / LLM_API_KEY, falling back to the legacy OPENAI_* names.
    """
    import openai  # lazy import
    base_url = (
        getattr(settings, "LLM_BASE_URL", None)
        or getattr(settings, "OPENAI_BASE_URL", None)
        or None
    )
    api_key = (
        getattr(settings, "LLM_API_KEY", None)
        or getattr(settings, "OPENAI_API_KEY", None)
        or ("local" if base_url else None)
    )
    if base_url:
        return openai.OpenAI(api_key=api_key, base_url=base_url)
    return openai.OpenAI(api_key=api_key)


def call_llm(
    user_prompt: str,
    settings: Any,
    *,
    system_prompt: Optional[str] = None,
    json_mode: bool = True,
    temperature: float = 0.0,
    model: Optional[str] = None,
    disable_thinking: bool = False,
) -> LLMResult:
    """Dispatch to the configured LLM provider and return the raw text content.

    `model` overrides the default chat model (settings.LLM_MODEL) for this call
    only — used e.g. by the ontology generator to run a large model for the
    generation pass and a cheaper one for the judge pass. When None, the
    configured default is used.

    `disable_thinking` adds {"think": false} to the request body via extra_body.
    Required for qwen3 on Ollama/vLLM to prevent the reasoning trace from
    contaminating the Manchester Syntax output. This is an Ollama/vLLM-only body
    field, so it is sent ONLY when a local endpoint is configured (LLM_BASE_URL
    set). The OpenAI cloud API rejects unknown body fields with HTTP 400 — it does
    NOT silently ignore them — so against OpenAI the field is omitted.

    Raises RuntimeError on failure — callers are responsible for catching.
    """
    provider = (getattr(settings, "QUERY_PROVIDER", "openai") or "openai").lower()
    if provider == "claude_code":
        return _via_claude_code(user_prompt, settings, system_prompt=system_prompt)
    return _via_openai(
        user_prompt, settings,
        system_prompt=system_prompt,
        json_mode=json_mode,
        temperature=temperature,
        model=model,
        disable_thinking=disable_thinking,
    )


def _via_claude_code(
    user_prompt: str,
    settings: Any,
    *,
    system_prompt: Optional[str] = None,
) -> LLMResult:
    model = getattr(settings, "QUERY_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    timeout = getattr(settings, "LLM_REQUEST_TIMEOUT", 120)
    cmd = [_claude_exe(), "-p", "--model", model, "--output-format", "json"]
    if system_prompt:
        cmd += ["--system-prompt", system_prompt]
    cmd.append(user_prompt)

    # Ensure ~/.local/bin is in PATH so the claude CLI is found even when the
    # web server process inherits a restricted shell environment.
    env = os.environ.copy()
    local_bin = os.path.expanduser("~/.local/bin")
    if local_bin not in env.get("PATH", ""):
        env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude CLI exited {proc.returncode}: {proc.stderr.strip()[:400]}"
        )
    try:
        envelope = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError(
            f"claude CLI unparseable output: {proc.stdout[:200]!r}"
        ) from exc

    if envelope.get("is_error"):
        raise RuntimeError(f"claude CLI error: {envelope.get('result', '')[:400]}")

    content = envelope.get("result", "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-z]*\n?", "", content).rstrip("` \n")
    return LLMResult(content=repair_json(content))


def _via_openai(
    user_prompt: str,
    settings: Any,
    *,
    system_prompt: Optional[str] = None,
    json_mode: bool = True,
    temperature: float = 0.0,
    model: Optional[str] = None,
    disable_thinking: bool = False,
) -> LLMResult:
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    kwargs: dict = {
        "model": model or getattr(settings, "LLM_MODEL", None) or settings.OPENAI_MODEL,
        "messages": messages,
        "temperature": temperature,
        "timeout": getattr(settings, "LLM_REQUEST_TIMEOUT", 60),
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    # Ollama/vLLM-only request-body extensions (options.num_ctx for the context
    # window; think:false to suppress qwen3's reasoning trace). The OpenAI cloud
    # API rejects unknown body fields with HTTP 400 — it does NOT silently ignore
    # them — so these are attached ONLY when a local/custom endpoint is configured
    # via LLM_BASE_URL. Against OpenAI (LLM_BASE_URL unset) they are omitted.
    extra_body: dict = {}
    is_local_backend = bool(getattr(settings, "LLM_BASE_URL", None))
    num_ctx = getattr(settings, "LLM_NUM_CTX", None)
    if is_local_backend and num_ctx:
        extra_body["options"] = {"num_ctx": int(num_ctx)}
    if is_local_backend and disable_thinking:
        extra_body["think"] = False
    if extra_body:
        kwargs["extra_body"] = extra_body

    client = make_openai_client(settings)

    # Stream by default and reassemble client-side. A long single-shot generation
    # (a large model emitting a big ontology fragment) can exceed the ~100-120s
    # read timeout of a fronting proxy (RunPod's Cloudflare layer → HTTP 524) even
    # when the SDK timeout is higher. Streaming keeps bytes flowing so the proxy
    # never sees a stall; usage arrives on the final chunk via stream_options.
    if getattr(settings, "LLM_STREAM", True):
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}
        parts: list[str] = []
        pt = ct = 0
        for chunk in client.chat.completions.create(**kwargs):
            if chunk.choices:
                delta = chunk.choices[0].delta
                if delta and delta.content:
                    parts.append(delta.content)
            usage = getattr(chunk, "usage", None)
            if usage:
                pt = usage.prompt_tokens or 0
                ct = usage.completion_tokens or 0
        return LLMResult(content="".join(parts), prompt_tokens=pt, completion_tokens=ct)

    response = client.chat.completions.create(**kwargs)
    pt = response.usage.prompt_tokens if response.usage else 0
    ct = response.usage.completion_tokens if response.usage else 0
    return LLMResult(
        content=response.choices[0].message.content or "",
        prompt_tokens=pt,
        completion_tokens=ct,
    )
