# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Tests for semantic_mapper.ontology.seed_generator."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rdflib import Graph
from rdflib.namespace import OWL, RDF, RDFS, XSD

from semantic_mapper.config.settings import Settings
from semantic_mapper.db.models import ColumnProfile, TableProfile
from semantic_mapper.ontology.seed_generator import SeedGenerator, _build_schema_summary


# ── Fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture()
def settings(tmp_path):
    return Settings(
        DATABASE_URL="mysql+pymysql://user:pass@localhost/test_db",
        ONTOLOGY_PATH="",
        OPENAI_API_KEY="test-key",
        LLM_STREAM=False,  # mocks return a single (non-streamed) response
        SEED_OUTPUT_PATH=str(tmp_path / "seed_ontology.owl"),
    )


@pytest.fixture()
def simple_profiles():
    return [
        TableProfile(
            table_name="patients",
            row_count=100,
            columns={
                "id": ColumnProfile(
                    name="id", inferred_type="VARCHAR(36)", is_nullable=False,
                    is_primary_key=True, null_rate=0.0, distinct_count=100,
                ),
                "birthdate": ColumnProfile(
                    name="birthdate", inferred_type="DATE", is_nullable=False,
                    is_primary_key=False, null_rate=0.0, distinct_count=80,
                ),
                "gender": ColumnProfile(
                    name="gender", inferred_type="VARCHAR(10)", is_nullable=True,
                    is_primary_key=False, null_rate=0.01, distinct_count=2,
                    top_values=[{"value": "M", "count": 55}, {"value": "F", "count": 44}],
                ),
            },
            foreign_keys=[],
        ),
        TableProfile(
            table_name="encounters",
            row_count=500,
            columns={
                "id": ColumnProfile(
                    name="id", inferred_type="VARCHAR(36)", is_nullable=False,
                    is_primary_key=True, null_rate=0.0, distinct_count=500,
                ),
                "patient": ColumnProfile(
                    name="patient", inferred_type="VARCHAR(36)", is_nullable=False,
                    is_primary_key=False, null_rate=0.0, distinct_count=100,
                ),
                "start": ColumnProfile(
                    name="start", inferred_type="DATETIME", is_nullable=False,
                    is_primary_key=False, null_rate=0.0, distinct_count=500,
                ),
            },
            foreign_keys=[{"column": "patient", "ref_table": "patients"}],
        ),
    ]


@pytest.fixture()
def llm_response():
    return {
        "reasoning": "The schema has two entities: Patient and Encounter.",
        "base_iri": "http://example.com/test_db/",
        "classes": [
            {"local_name": "Patient", "label": "Patient",
             "comment": "A person receiving care", "source_tables": ["patients"]},
            {"local_name": "Encounter", "label": "Encounter",
             "comment": "A healthcare visit", "source_tables": ["encounters"]},
        ],
        "datatype_properties": [
            {"local_name": "hasDateOfBirth", "label": "has date of birth",
             "comment": "Date of birth", "domain": "Patient",
             "xsd_range": "date", "source_columns": ["patients.birthdate"]},
            {"local_name": "hasGender", "label": "has gender",
             "comment": "Gender", "domain": "Patient",
             "xsd_range": "string", "source_columns": ["patients.gender"]},
            {"local_name": "hasStartDateTime", "label": "has start date time",
             "comment": "Start of encounter", "domain": "Encounter",
             "xsd_range": "dateTime", "source_columns": ["encounters.start"]},
        ],
        "object_properties": [
            {"local_name": "hasPatient", "label": "has patient",
             "comment": "Links encounter to patient", "domain": "Encounter",
             "range": "Patient", "source_fk": "encounters.patient -> patients.id"},
        ],
    }


def _mock_openai(llm_response: dict):
    mock_choice = MagicMock()
    mock_choice.message.content = json.dumps(llm_response)
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage.prompt_tokens = 200
    mock_response.usage.completion_tokens = 150
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_response
    return mock_client


# ── _build_schema_summary ─────────────────────────────────────────────────────
def test_schema_summary_includes_table_names(simple_profiles):
    summary = _build_schema_summary(simple_profiles)
    assert "TABLE: patients" in summary
    assert "TABLE: encounters" in summary


def test_schema_summary_marks_pk(simple_profiles):
    summary = _build_schema_summary(simple_profiles)
    assert "PK" in summary


def test_schema_summary_marks_fk(simple_profiles):
    summary = _build_schema_summary(simple_profiles)
    assert "FK→patients" in summary


def test_schema_summary_includes_top_values(simple_profiles):
    summary = _build_schema_summary(simple_profiles)
    assert "values=[M, F]" in summary


# ── SeedGenerator.generate ────────────────────────────────────────────────────
def test_generate_writes_owl_file(settings, simple_profiles, llm_response, tmp_path):
    with patch("openai.OpenAI", return_value=_mock_openai(llm_response)):
        gen = SeedGenerator(settings)
        out = gen.generate(simple_profiles)

    assert out.exists()
    assert out.suffix == ".owl"


def test_generate_owl_contains_classes(settings, simple_profiles, llm_response):
    with patch("openai.OpenAI", return_value=_mock_openai(llm_response)):
        gen = SeedGenerator(settings)
        out = gen.generate(simple_profiles)

    g = Graph()
    g.parse(str(out), format="xml")
    classes = list(g.subjects(RDF.type, OWL.Class))
    local_names = {str(c).split("/")[-1] for c in classes}
    assert "Patient" in local_names
    assert "Encounter" in local_names


def test_generate_owl_contains_datatype_properties(settings, simple_profiles, llm_response):
    with patch("openai.OpenAI", return_value=_mock_openai(llm_response)):
        gen = SeedGenerator(settings)
        out = gen.generate(simple_profiles)

    g = Graph()
    g.parse(str(out), format="xml")
    props = list(g.subjects(RDF.type, OWL.DatatypeProperty))
    local_names = {str(p).split("/")[-1] for p in props}
    assert "hasDateOfBirth" in local_names
    assert "hasGender" in local_names


def test_generate_owl_contains_object_properties(settings, simple_profiles, llm_response):
    with patch("openai.OpenAI", return_value=_mock_openai(llm_response)):
        gen = SeedGenerator(settings)
        out = gen.generate(simple_profiles)

    g = Graph()
    g.parse(str(out), format="xml")
    props = list(g.subjects(RDF.type, OWL.ObjectProperty))
    local_names = {str(p).split("/")[-1] for p in props}
    assert "hasPatient" in local_names


def test_generate_owl_domain_range_set(settings, simple_profiles, llm_response):
    with patch("openai.OpenAI", return_value=_mock_openai(llm_response)):
        gen = SeedGenerator(settings)
        out = gen.generate(simple_profiles)

    g = Graph()
    g.parse(str(out), format="xml")
    ns = "http://example.com/test_db/"
    from rdflib import URIRef
    dob_uri = URIRef(ns + "hasDateOfBirth")
    ranges = list(g.objects(dob_uri, RDFS.range))
    assert XSD.date in ranges


def test_generate_uses_new_naming_convention(settings, simple_profiles, llm_response, tmp_path):
    # Generate twice; each call should produce a distinct sequenced filename
    with patch("openai.OpenAI", return_value=_mock_openai(llm_response)):
        gen = SeedGenerator(settings)
        out1 = gen.generate(simple_profiles)
        out2 = gen.generate(simple_profiles)

    assert out1 != out2
    # Both follow {dbshort}_gen_{yymmdd}_{ddd}.owl convention
    import re
    pattern = re.compile(r"^[a-zA-Z0-9_]+_gen_\d{6}_\d{3}\.owl$")
    assert pattern.match(out1.name), f"Unexpected name: {out1.name}"
    assert pattern.match(out2.name), f"Unexpected name: {out2.name}"


def test_generate_does_not_overwrite_existing(settings, simple_profiles, llm_response):
    # Pre-existing files in the output dir are never clobbered; sequence increments
    out_dir = Path(settings.SEED_OUTPUT_PATH).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    today = datetime.now().strftime("%y%m%d")
    # Place a sequence-001 file to force the generator to use 002
    existing = out_dir / f"test_db_gen_{today}_001.owl"
    existing.write_text("<dummy/>")

    with patch("openai.OpenAI", return_value=_mock_openai(llm_response)):
        gen = SeedGenerator(settings)
        out = gen.generate(simple_profiles)

    assert existing.read_text() == "<dummy/>"   # untouched
    assert out != existing
    assert f"_gen_{today}_002.owl" in out.name


def test_generate_records_token_counts(settings, simple_profiles, llm_response):
    with patch("openai.OpenAI", return_value=_mock_openai(llm_response)):
        gen = SeedGenerator(settings)
        gen.generate(simple_profiles)

    assert gen.prompt_tokens == 200
    assert gen.completion_tokens == 150


def test_unknown_xsd_range_defaults_to_string(settings, simple_profiles, llm_response):
    llm_response["datatype_properties"][0]["xsd_range"] = "nonsense_type"
    with patch("openai.OpenAI", return_value=_mock_openai(llm_response)):
        gen = SeedGenerator(settings)
        out = gen.generate(simple_profiles)

    g = Graph()
    g.parse(str(out), format="xml")
    ns = "http://example.com/test_db/"
    from rdflib import URIRef
    dob_uri = URIRef(ns + "hasDateOfBirth")
    ranges = list(g.objects(dob_uri, RDFS.range))
    assert XSD.string in ranges
