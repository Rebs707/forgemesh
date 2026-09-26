# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Preflight check for a fully-local / air-gapped ForgeMesh LLM backend.

Verifies, without touching the cloud:
  1. The resolved LLM backend config (base_url, models, provider).
  2. That the local OpenAI-compatible endpoint (Ollama) answers a chat call.
  3. That the local sentence-transformers embedding model is cached on disk.

Run it twice to prove the air gap:
  * Online  — confirm it passes and check what it reports.
  * Offline — disable the network adapter (or run behind a black-hole proxy)
              and run again. If it still passes, no call left the machine.

Usage:
    python scripts/check_local_llm.py [path-to-env-file]
    python scripts/check_local_llm.py .env.local
"""
from __future__ import annotations

import os
import sys

# Allow running as `python scripts/check_local_llm.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg: str) -> None:
    print(f"  [ OK ] {msg}")


def _warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


def _fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def main() -> int:
    from semantic_mapper.config.settings import Settings
    from semantic_mapper.core.llm import make_openai_client

    # Optional: path to an env file (e.g. .env.local). Defaults to .env.
    env_file = sys.argv[1] if len(sys.argv) > 1 else None
    settings = Settings(_env_file=env_file) if env_file else Settings()
    if env_file:
        print(f"(loaded config from {env_file})")

    base_url = getattr(settings, "LLM_BASE_URL", None) or getattr(settings, "OPENAI_BASE_URL", None)
    provider = (getattr(settings, "QUERY_PROVIDER", "openai") or "openai").lower()
    anthropic_key = getattr(settings, "ANTHROPIC_API_KEY", None)
    llm_model = getattr(settings, "LLM_MODEL", None) or settings.OPENAI_MODEL
    query_model = settings.QUERY_MODEL
    gen_model = getattr(settings, "ONTOLOGY_GEN_MODEL", None) or llm_model
    judge_model = getattr(settings, "ONTOLOGY_JUDGE_MODEL", None) or llm_model

    print("\n1. Backend configuration")
    print(f"     LLM_BASE_URL : {base_url or '(unset -> cloud api.openai.com)'}")
    print(f"     LLM_MODEL    : {llm_model}   (ontology gen / mapping / narrator)")
    print(f"     QUERY_MODEL  : {query_model}   (NL->SQL / NL->SPARQL / MCP)")
    print(f"     ontology gen : {gen_model}  |  judge: {judge_model}")
    print(f"     QUERY_PROVIDER : {provider}")

    failures = 0

    if not base_url:
        _fail("LLM_BASE_URL is unset -- calls would go to cloud OpenAI, NOT air-gapped.")
        failures += 1
    elif "localhost" in base_url or "127.0.0.1" in base_url:
        _ok(f"base_url points at a local endpoint ({base_url}).")
    else:
        _warn(f"base_url is set but not obviously local: {base_url}")

    if provider == "claude_code":
        _fail("QUERY_PROVIDER=claude_code shells out to the cloud `claude` CLI. Use 'openai'.")
        failures += 1
    else:
        _ok(f"QUERY_PROVIDER='{provider}' (no cloud Claude CLI).")

    if anthropic_key:
        _warn("ANTHROPIC_API_KEY is set -- harmless while provider=openai, but clear it to be safe.")
    else:
        _ok("ANTHROPIC_API_KEY is empty.")

    print("\n2. Local chat completion (each configured model)")
    # Test every distinct model so a missing pull is caught for the right role.
    client = make_openai_client(settings)
    roles = {}
    for role, mdl in [("LLM_MODEL", llm_model), ("QUERY_MODEL", query_model),
                      ("ONTOLOGY_GEN_MODEL", gen_model), ("ONTOLOGY_JUDGE_MODEL", judge_model)]:
        roles.setdefault(mdl, []).append(role)
    for mdl, role_names in roles.items():
        try:
            resp = client.chat.completions.create(
                model=mdl,
                messages=[{"role": "user", "content": "Reply with the single word: pong"}],
                temperature=0,
                timeout=getattr(settings, "LLM_REQUEST_TIMEOUT", 60),
            )
            reply = (resp.choices[0].message.content or "").strip()
            _ok(f"{mdl} responded: {reply!r}  [{', '.join(role_names)}]")
        except Exception as exc:  # noqa: BLE001 -- preflight wants the raw reason
            _fail(f"{mdl} chat call failed: {exc}  [{', '.join(role_names)}]")
            _warn(f"Pull it:  ollama pull {mdl}")
            failures += 1

    print("\n3. Local embedding model (sentence-transformers)")
    try:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer("all-MiniLM-L6-v2")
        dim = model.get_sentence_embedding_dimension()
        _ok(f"all-MiniLM-L6-v2 loaded locally (dim={dim}).")
    except Exception as exc:  # noqa: BLE001
        _fail(f"could not load all-MiniLM-L6-v2: {exc}")
        _warn("Pre-cache it ONCE while online (it downloads from HuggingFace):")
        _warn('  python -c "from sentence_transformers import SentenceTransformer; '
              "SentenceTransformer('all-MiniLM-L6-v2')\"")
        _warn("Then set HF_HUB_OFFLINE=1 to forbid any future download attempt.")
        failures += 1

    print()
    if failures:
        print(f"RESULT: {failures} check(s) failed -- not yet fully local.\n")
        return 1
    print("RESULT: all checks passed -- ForgeMesh is running fully local.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
