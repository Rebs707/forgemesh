# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""Phase 1 tests — get_schema / build_agent_schema."""
from __future__ import annotations

import pytest

from semantic_mapper.mcp.schema_builder import build_agent_schema, SCHEMA_VERSION
from .conftest import ONT


def _build(definitions, mapping_report):
    return build_agent_schema(
        definitions=definitions,
        mapping_report=mapping_report,
        named_graph="http://mesh.local/graph/current",
        base_uri="http://mesh.local/data/",
        ontology_graph=None,
        ontology_name="test_ontology",
    )


def test_returns_all_classes_from_ontology(definitions, mapping_report):
    schema = _build(definitions, mapping_report)
    uris = {c["uri"] for c in schema["classes"]}
    assert f"{ONT}Patient" in uris
    assert f"{ONT}Admission" in uris
    assert len(schema["classes"]) == 2


def test_each_class_has_gold_table(definitions, mapping_report):
    schema = _build(definitions, mapping_report)
    for cls in schema["classes"]:
        assert cls["gold_table"].startswith("gold_"), cls
        assert cls["label"]


def test_each_data_property_has_source_column(definitions, mapping_report):
    schema = _build(definitions, mapping_report)
    for cls in schema["classes"]:
        for prop in cls["properties"]:
            if prop["kind"] == "data":
                assert "source_column" in prop, f"Missing source_column on {prop}"
                assert "range" in prop, f"Missing range on {prop}"


def test_object_properties_produce_join_patterns(definitions, mapping_report):
    schema = _build(definitions, mapping_report)
    admission = next(c for c in schema["classes"] if c["label"] == "admission")
    assert len(admission["join_patterns"]) >= 1
    jp = admission["join_patterns"][0]
    assert "sparql_pattern" in jp
    assert "via_property" in jp
    assert "to_entity" in jp
    # The pattern must reference the entity variables
    assert "URI" in jp["sparql_pattern"]


def test_construction_rules_is_non_empty(definitions, mapping_report):
    schema = _build(definitions, mapping_report)
    rules = schema["construction_rules"]
    assert isinstance(rules, list)
    assert len(rules) >= 10
    # Named graph is injected into the relevant rule
    assert "http://mesh.local/graph/current" in rules[1]


def test_value_matching_guidance_present_for_low_cardinality(definitions, mapping_report):
    schema = _build(definitions, mapping_report)
    guidance = schema["value_matching_guidance"]
    assert isinstance(guidance, list)
    assert len(guidance) >= 1
    combined = " ".join(guidance)
    # Should mention both source values and URI local names
    assert "EMERGENCY" in combined
    assert "URI" in combined or "STRAFTER" in combined


def test_categorical_uri_properties_classified_correctly(definitions, mapping_report):
    """Columns with value_map must be kind=categorical_uri, not kind=data."""
    # Add a value_map to the admission_type spec
    from semantic_mapper.gold.models import GoldColumnSpec, GoldTableDefinition
    from .conftest import ONT, PATIENT_DEF

    spec_with_map = GoldColumnSpec(
        silver_table="silver_raw_admissions",
        source_column="admission_type",
        gold_column_name="admission_type",
        source_data_type="VARCHAR(50)",
        property_uri=f"{ONT}hasAdmissionType",
        value_map={
            "EMERGENCY": f"{ONT}EmergencyAdmission",
            "ELECTIVE": f"{ONT}ElectiveAdmission",
        },
    )
    defn_with_map = GoldTableDefinition(
        gold_table_name="gold_admission",
        primary_class_uri=f"{ONT}Admission",
        class_label="admission",
        source_silver_tables=["silver_raw_admissions"],
        column_specs=[spec_with_map],
    )
    schema = build_agent_schema(
        definitions=[PATIENT_DEF, defn_with_map],
        mapping_report=mapping_report,
        named_graph="http://mesh.local/graph/current",
        base_uri="http://mesh.local/data/",
    )
    admission = next(c for c in schema["classes"] if c["label"] == "admission")
    cat_prop = next((p for p in admission["properties"] if "AdmissionType" in p["label"]), None)
    assert cat_prop is not None
    assert cat_prop["kind"] == "categorical_uri"
    assert "value_map" in cat_prop
    assert cat_prop["value_map"]["EMERGENCY"] == "EmergencyAdmission"
    assert "STRAFTER" in cat_prop["note"] or "literal" in cat_prop["note"].lower()


def test_schema_version_and_metadata(definitions, mapping_report):
    schema = _build(definitions, mapping_report)
    assert schema["schema_version"] == SCHEMA_VERSION
    assert schema["named_graph"] == "http://mesh.local/graph/current"
    assert "ont" in schema["namespaces"] or "data" in schema["namespaces"]


def test_display_fields_populated(definitions, mapping_report):
    schema = _build(definitions, mapping_report)
    # At least one class should have non-empty display_fields
    any_fields = any(cls["display_fields"] for cls in schema["classes"])
    assert any_fields


def test_fk_detected_via_source_column_when_gold_column_name_differs(mapping_report):
    """Regression: source_column='patient' / gold_column_name='subject_id'.

    triple_builder uses source_column for entity_map lookup and emits a URI.
    schema_builder must mirror that so it declares kind=object, not kind=data.
    This was the root cause of hasDiagnosesIcdPatient being declared xsd:integer
    while Fuseki stored a URI — causing FILTER(?var = 10124) to return 0 rows.
    """
    from semantic_mapper.gold.models import GoldColumnSpec, GoldTableDefinition
    from .conftest import PATIENT_DEF

    diagnosesicd_def = GoldTableDefinition(
        gold_table_name="gold_diagnosesicd",
        primary_class_uri=f"{ONT}DiagnosisIcd",
        class_label="diagnosesicd",
        source_silver_tables=["silver_raw_diagnosesicd"],
        id_column=None,
        column_specs=[
            GoldColumnSpec(
                silver_table="silver_raw_diagnosesicd",
                source_column="patient",       # matches entity_map key "patient"
                gold_column_name="subject_id", # does NOT match — this was the bug
                source_data_type="INT",
                property_uri=f"{ONT}hasDiagnosesIcdPatient",
            ),
            GoldColumnSpec(
                silver_table="silver_raw_diagnosesicd",
                source_column="icd9_code",
                gold_column_name="icd9_code",
                source_data_type="VARCHAR(10)",
                property_uri=f"{ONT}hasIcd9Code",
            ),
        ],
    )
    schema = build_agent_schema(
        definitions=[PATIENT_DEF, diagnosesicd_def],
        mapping_report=None,
        named_graph="http://mesh.local/graph/current",
        base_uri="http://mesh.local/data/",
    )
    diag = next(c for c in schema["classes"] if c["label"] == "diagnosesicd")

    patient_prop = next(
        (p for p in diag["properties"] if "IcdPatient" in p["label"]), None
    )
    assert patient_prop is not None
    assert patient_prop["kind"] == "object", (
        "hasDiagnosesIcdPatient must be kind=object: source_column 'patient' matches "
        "entity_map, so triple_builder emits a URI, not a literal"
    )

    assert len(diag["join_patterns"]) >= 1
    jp = diag["join_patterns"][0]
    assert jp["to_entity"] == "patient"
    assert "URI" in jp["sparql_pattern"]

    # The FK column must NOT appear in display_fields (it holds a URI, not a human value)
    assert "subject_id" not in diag["display_fields"]


def test_value_matching_guidance_drops_unmaterialised_targets():
    # Guidance must reference only categorical URIs that actually materialised — i.e.
    # those present in a gold value_map. A report mapping dropped before gold (e.g.
    # Schedules.venue_code -> VenueCode*) must NOT appear, or the agent would filter
    # on entities that do not exist. Targets that ARE in a gold value_map are kept.
    from types import SimpleNamespace as NS
    from semantic_mapper.mcp.schema_builder import _build_value_matching_guidance

    report = NS(tables=[NS(
        source_table="raw_schedules",
        column_mappings=[
            NS(source_column="gender", value_mappings=[
                NS(source_value="M", mapped_uri="http://x#MaleGender"),
                NS(source_value="W", mapped_uri="http://x#FemaleGender"),
            ]),
            NS(source_column="venue_code", value_mappings=[  # dropped before gold
                NS(source_value="CORT", mapped_uri="http://x#VenueCodeCortina"),
            ]),
        ],
    )])
    # gold materialised only the gender URIs
    defs = [NS(column_specs=[
        NS(value_map={"M": "http://x#MaleGender", "W": "http://x#FemaleGender"}),
        NS(value_map=None),
    ])]

    out = _build_value_matching_guidance(report, defs)
    text = " ".join(out)
    assert "gender" in text and "MaleGender" in text          # materialised → kept
    assert "venue_code" not in text and "VenueCode" not in text  # phantom → dropped

    # Without definitions (legacy callers) nothing is filtered — both appear.
    assert any("venue_code" in l for l in _build_value_matching_guidance(report, None))
