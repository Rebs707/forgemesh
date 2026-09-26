# Contributing to ForgeMesh

Thanks for helping. Bug reports, fixes, new datasets and docs are all welcome.

## Setup

Follow [GETTING_STARTED.md](GETTING_STARTED.md) to get a working install and the
Synthea quick start running. For development you only need steps 1–2 plus the tests
below. The unit suite needs no running services.

## Tests

```bash
python -m pytest
```

- Unit tests use in-memory SQLite. Never make a unit test depend on a live MySQL,
  Fuseki or LLM. Mock the LLM client.
- Tests that need live services are opt-in and skip themselves otherwise, for example
  `SYNTHEA_E2E=1 python -m pytest tests/synthea/test_e2e_gate.py -v` (end-to-end gate:
  clears the Synthea graph and re-materialises from bronze) and `OPENFLIGHTS_E2E=1`.
- Add a test with every fix. Name tests after the general case, not a dataset.

Run the Synthea E2E gate before opening a PR that touches bronze → silver → gold
transformation, RDF triple emission, the Fuseki writer, or the mapping report schema.
Green unit tests against a stale triple store don't count.

## The core rule: no dataset-specific knowledge

ForgeMesh must work on any relational schema without knowing anything about it in
advance. Every inference mechanism (FK and PK detection, value mapping, ontology
generation, query context) has to generalise.

A heuristic that only works because of one dataset's naming conventions is a bug.
None of these belong in code or config:

- hard-coded table or column names in pipeline code
- `if "mimic" in table_name:` style branches
- per-dataset settings that bypass general inference

When a dataset exposes a gap:

1. Identify the general structural pattern, not the specific instance.
2. Implement a general algorithm that handles every schema with that pattern.
3. Test the general case, using generic names (`raw_events`, `raw_caregivers`) only
   where needed to illustrate the pattern.
4. Remove any workaround once the general fix is in.

For example, FK inference (`semantic_mapper/db/inspector.py`) combines natural-key,
table-name, PK-name, value-overlap and role-prefix strategies. A foreign key none of
them catches calls for a new general strategy, not a hand-written mapping.

## The provenance contract

Every queryable triple must trace back to its gold row. The RDF triple builder emits
`fm:bronzeSourceRef` and `fm:goldRunId` for every row that carries audit columns.
Don't add logic that drops them: answers would lose their lineage, and
`prove_answer` would fail with an empty trace.

## Pull requests

- Keep each PR to one change, and describe what it does and how you tested it.
- Code matches the surrounding style. New Python files start with the licence header:
  ```python
  # Copyright (c) 2026 Ben Tagger
  # SPDX-License-Identifier: Apache-2.0
  ```
- By contributing, you agree your contribution is licensed under the Apache License
  2.0 (see [LICENSE](LICENSE)).

## Reporting security issues

Don't open a public issue. See [SECURITY.md](SECURITY.md).
