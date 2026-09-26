# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""LLM prompts are dataset-agnostic and fully rendered.

The bundled datasets' table names are read from the repo (not hard-coded here) and
must not appear in any prompt's example identifiers: examples tuned to one dataset
steer generation on every other schema.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from semantic_mapper.engine.strategies import llm_strategy
from semantic_mapper.ontology.iterative.gen_prompt import build_gen_prompt
from semantic_mapper.ontology.iterative.judge_prompt import build_judge_prompt
from semantic_mapper.ontology.iterative.models import (
    AccumulatedAbstractPropertySummary, OntologyFragment, TableContext,
)
from semantic_mapper.rdf.query import engine as sparql_engine

ROOT = Path(__file__).resolve().parents[3]


def _bundled_table_stems() -> set[str]:
    names: set[str] = set()
    for schema in ROOT.glob("datasets/*/setup/schema.sql"):
        names |= {n.lower() for n in re.findall(
            r"CREATE TABLE(?: IF NOT EXISTS)?\s+`?(\w+)", schema.read_text(), re.I)}
    names |= set(re.findall(r'"(raw_\w+)"\s*:', (ROOT / "scripts/load_synthea.py").read_text()))
    names |= set(re.findall(r'"(raw_\w+)"\s*:', (ROOT / "scripts/load_openflights.py").read_text()))
    stems = set()
    for n in names:
        parts = re.sub(r"^[a-z]_", "", re.sub(r"^raw_", "", n)).split("_")
        stems.add("".join(p[:-1] if p.endswith("s") and len(p) > 3 else p for p in parts))
    return {s for s in stems if len(s) >= 6}


def _identifiers(text: str) -> set[str]:
    """Code-like tokens: camelCase, snake_case, or ?var / :Class / hasProp forms."""
    out = set()
    for tok in re.findall(r"[?:]?[A-Za-z_][A-Za-z0-9_]*", text):
        core = tok.lstrip("?:")
        if tok[0] in "?:" or "_" in core or core.startswith("has") or re.search(r"[a-z][A-Z]", core):
            out.add(core.lower().replace("_", ""))
    return out


def _table_ctx() -> TableContext:
    return TableContext(table_name="raw_widgets", row_count=5000, columns=[],
                        explicit_fks=[], processing_index=2, total_tables=7)


def _prompts() -> dict[str, str]:
    ctx = _table_ctx()
    abstract = [AccumulatedAbstractPropertySummary(
        local_name="hasGadget", label="gadget", uri="http://x#hasGadget",
        range_uri="http://x#Gadget", prop_type="ObjectProperty")]
    fragment = OntologyFragment(table_name="raw_widgets", manchester_syntax="",
                                classes_declared=[], properties_declared=[])
    return {
        "gen": build_gen_prompt(ctx, [], "http://x#Widget", "http://x#", abstract_properties=abstract),
        "judge": "\n".join(build_judge_prompt(ctx, fragment, [], "http://x#")),
        "value_mapping": llm_strategy._VALUE_MAPPING_SYSTEM_PROMPT,
        "mapping": llm_strategy._SYSTEM_PROMPT,
        "sparql_system": sparql_engine._SYSTEM_PROMPT_TEMPLATE,
    }


def test_bundled_datasets_are_found():
    assert len(_bundled_table_stems()) > 10


@pytest.mark.parametrize("name", list(_prompts()))
def test_prompt_examples_use_no_bundled_dataset_names(name):
    stems = _bundled_table_stems()
    leaks = sorted({f"{s} in {i}" for i in _identifiers(_prompts()[name]) for s in stems if s in i})
    assert not leaks, f"{name} prompt uses bundled-dataset names: {leaks}"


def test_gen_prompt_header_is_rendered():
    header = build_gen_prompt(_table_ctx(), [], "http://x#Widget", "http://x#").splitlines()[1]
    assert header == 'Table 2 of 7: "raw_widgets" (5,000 rows).'
    assert "{" not in header
