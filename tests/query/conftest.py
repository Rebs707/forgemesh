# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from semantic_mapper.config.settings import Settings
from semantic_mapper.schema.mapping_output import (
    ClassCandidate,
    MappingStatus,
    PropertyMapping,
    SemanticMappingReport,
    TableMapping,
    ValueMappingRule,
)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@pytest.fixture
def query_settings():
    return Settings(
        DATABASE_URL="sqlite:///:memory:",
        SILVER_DATABASE_URL="sqlite:///:memory:",
        QUERY_MAX_ROWS=10,
        QUERY_MODEL="gpt-4o-mini",
    )


# ---------------------------------------------------------------------------
# Silver SQLite engine with a populated table
# ---------------------------------------------------------------------------

@pytest.fixture
def silver_engine():
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        conn.execute(
            text(
                "CREATE TABLE silver_raw_party ("
                "  _ontology_class_uri TEXT,"
                "  _bronze_source_ref TEXT,"
                "  party_identifier TEXT,"
                "  date_of_birth TEXT,"
                "  party_type INTEGER"
                ")"
            )
        )
        conn.execute(
            text(
                "INSERT INTO silver_raw_party VALUES "
                "('http://example.org/ontology#Individual', 'raw_party.party_id=P1', "
                "'P1', '1978-03-14', 1)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO silver_raw_party VALUES "
                "('http://example.org/ontology#Organisation', 'raw_party.party_id=P2', "
                "'P2', NULL, 2)"
            )
        )
        conn.commit()
    return engine


# ---------------------------------------------------------------------------
# Mapping report fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def value_mapping_rule_individual():
    return ValueMappingRule(
        source_value=1,
        mapped_uri="http://example.org/ontology#Individual",
        rationale="1 = Individual",
        confidence_score=0.95,
    )


@pytest.fixture
def value_mapping_rule_organisation():
    return ValueMappingRule(
        source_value=2,
        mapped_uri="http://example.org/ontology#Organisation",
        rationale="2 = Organisation",
        confidence_score=0.95,
    )


@pytest.fixture
def confirmed_id_mapping():
    return PropertyMapping(
        source_column="party_id",
        source_data_type="VARCHAR(50)",
        is_nullable=False,
        mapped_property_uri="http://example.org/ontology#partyIdentifier",
        mapped_property_label="Party Identifier",
        status=MappingStatus.CONFIRMED,
        confidence_score=0.99,
        rationale="PK mapped to identifier",
    )


@pytest.fixture
def confirmed_dob_mapping():
    return PropertyMapping(
        source_column="dob",
        source_data_type="VARCHAR(20)",
        is_nullable=True,
        mapped_property_uri="http://example.org/ontology#dateOfBirth",
        mapped_property_label="Date of Birth",
        status=MappingStatus.CONFIRMED,
        confidence_score=0.95,
        rationale="Date column matched to dateOfBirth",
    )


@pytest.fixture
def confirmed_type_mapping(value_mapping_rule_individual, value_mapping_rule_organisation):
    return PropertyMapping(
        source_column="party_type",
        source_data_type="INTEGER",
        is_nullable=False,
        mapped_property_uri="http://example.org/ontology#partyType",
        mapped_property_label="Party Type",
        status=MappingStatus.CONFIRMED,
        confidence_score=0.90,
        rationale="Discriminator column",
        value_mappings=[value_mapping_rule_individual, value_mapping_rule_organisation],
    )


@pytest.fixture
def pending_mapping():
    return PropertyMapping(
        source_column="notes",
        source_data_type="TEXT",
        is_nullable=True,
        status=MappingStatus.PENDING_REVIEW,
        confidence_score=0.5,
        rationale="Pending review",
    )


@pytest.fixture
def simple_report(confirmed_id_mapping, confirmed_dob_mapping, confirmed_type_mapping, pending_mapping):
    return SemanticMappingReport(
        ontology_uri="http://example.org/ontology",
        database_name="bronze_db",
        settings_used={},
        tables=[
            TableMapping(
                source_table="raw_party",
                class_candidate_set=[
                    ClassCandidate(
                        uri="http://example.org/ontology#Individual",
                        label="Individual",
                        rationale="Primary class",
                        confidence_score=0.99,
                    ),
                    ClassCandidate(
                        uri="http://example.org/ontology#LowConfidence",
                        label="LowConfidence",
                        rationale="Below threshold",
                        confidence_score=0.3,
                    ),
                ],
                column_mappings=[
                    confirmed_id_mapping,
                    confirmed_dob_mapping,
                    confirmed_type_mapping,
                    pending_mapping,
                ],
            )
        ],
        total_pending_review=1,
    )
