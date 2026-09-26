# ForgeMesh: instructions for Claude Code

Setup and running: [GETTING_STARTED.md](GETTING_STARTED.md). Full contributor rules:
[CONTRIBUTING.md](CONTRIBUTING.md). The rules below are binding.

## No dataset-specific knowledge

ForgeMesh must work on any relational schema without knowing it in advance. Never add
hard-coded table or column names to pipeline code, `if "<dataset>" in name:` branches,
or per-dataset settings that bypass inference. When a dataset exposes a gap, find the
general structural pattern and fix the inference algorithm for every schema that has
it, with tests written against the general case (generic names like `raw_events`).

## Provenance contract

Every queryable triple must trace back to its gold row. Never drop or skip the
`fm:bronzeSourceRef` and `fm:goldRunId` triples the RDF triple builder emits.
Without them, `prove_answer` fails with an empty lineage trace.

## Tests

- `python -m pytest`. Unit tests use in-memory SQLite; never make one depend on live
  MySQL, Fuseki or an LLM (mock the client).
- A change to bronze → silver → gold transformation, RDF triple emission, the Fuseki
  writer or the mapping report schema isn't done until the end-to-end gate passes on a
  freshly re-materialised graph:
  `SYNTHEA_E2E=1 python -m pytest tests/synthea/test_e2e_gate.py -v`
- If the gate fails because the pipeline is wrong, fix the pipeline, never the gate.

## Conventions

- Dataset settings live in `projects/<name>.yaml`; run with `--project <name>`.
- Secrets go in `.env` (gitignored). Never commit keys.
- New source files start with:
  `# Copyright (c) 2026 Ben Tagger` / `# SPDX-License-Identifier: Apache-2.0`
- Machine-specific instructions (e.g. which Python interpreter to use) belong in a
  gitignored `CLAUDE.local.md`.
