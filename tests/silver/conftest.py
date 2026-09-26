# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text

from semantic_mapper.config.settings import Settings
from semantic_mapper.schema.mapping_output import (
    ClassCandidate,
    MappingStatus,
    PropertyMapping,
    SemanticMappingReport,
    TableMapping,
    TransformationSpec,
    ValueMappingRule,
)
from semantic_mapper.silver.models import RowAudit


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@pytest.fixture
def silver_settings():
    return Settings(
        DATABASE_URL="sqlite:///:memory:",
        SILVER_DATABASE_URL="sqlite:///:memory:",
        SILVER_WRITER="mysql",
        SILVER_BATCH_SIZE=10,
    )


# ---------------------------------------------------------------------------
# SQLite engines
# ---------------------------------------------------------------------------

@pytest.fixture
def silver_engine(silver_settings):
    """In-memory SQLite engine for the silver target."""
    return create_engine("sqlite:///:memory:")


@pytest.fixture
def bronze_engine():
    """In-memory SQLite engine with a raw_party bronze table."""
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        conn.execute(
            text(
                "CREATE TABLE raw_party ("
                "  party_id VARCHAR(50),"
                "  dob VARCHAR(20),"
                "  party_type INT"
                ")"
            )
        )
        conn.execute(text("INSERT INTO raw_party VALUES ('P1', '1978-03-14', 1)"))
        conn.execute(text("INSERT INTO raw_party VALUES ('P2', '14/03/1982', 1)"))
        conn.execute(text("INSERT INTO raw_party VALUES ('P3', NULL, 2)"))
        conn.commit()
    return engine


# ---------------------------------------------------------------------------
# Mapping models
# ---------------------------------------------------------------------------

@pytest.fixture
def date_mapping():
    """PropertyMapping for a date column with transformation_spec."""
    return PropertyMapping(
        source_column="dob",
        source_data_type="VARCHAR(20)",
        is_nullable=True,
        mapped_property_uri="http://example.org/ontology#dateOfBirth",
        mapped_property_label="Date of Birth",
        status=MappingStatus.CONFIRMED,
        confidence_score=0.95,
        rationale="Date column matched to dateOfBirth property",
        transformation_spec=TransformationSpec(
            detected_formats=["YYYY-MM-DD", "DD/MM/YYYY"],
            target_format="xsd:date",
            parse_strategy="dateutil.parser",
            validation_rule="must be valid date",
        ),
    )


@pytest.fixture
def value_mapping():
    """PropertyMapping for a discriminator column with value_mappings."""
    return PropertyMapping(
        source_column="party_type",
        source_data_type="INT",
        is_nullable=False,
        mapped_property_uri="http://example.org/ontology#partyType",
        mapped_property_label="Party Type",
        status=MappingStatus.CONFIRMED,
        confidence_score=0.90,
        rationale="party_type maps to partyType discriminator",
        value_mappings=[
            ValueMappingRule(
                source_value=1,
                mapped_uri="http://example.org/ontology#Individual",
                rationale="1 = Individual party",
                confidence_score=0.95,
            ),
            ValueMappingRule(
                source_value=2,
                mapped_uri="http://example.org/ontology#Organisation",
                rationale="2 = Organisation party",
                confidence_score=0.95,
            ),
        ],
    )


@pytest.fixture
def plain_mapping():
    """Simple passthrough PropertyMapping."""
    return PropertyMapping(
        source_column="party_id",
        source_data_type="VARCHAR(50)",
        is_nullable=False,
        mapped_property_uri="http://example.org/ontology#partyIdentifier",
        status=MappingStatus.CONFIRMED,
        confidence_score=0.99,
        rationale="PK column mapped to identifier",
    )


@pytest.fixture
def pending_mapping():
    """PropertyMapping in PENDING status — should be skipped by pipeline."""
    return PropertyMapping(
        source_column="party_id",
        source_data_type="VARCHAR(50)",
        is_nullable=False,
        status=MappingStatus.PENDING_REVIEW,
        confidence_score=0.5,
        rationale="Pending review",
    )


@pytest.fixture
def simple_report(plain_mapping, date_mapping):
    """Minimal SemanticMappingReport with two CONFIRMED mappings."""
    return SemanticMappingReport(
        ontology_uri="http://example.org/ontology",
        database_name="bronze_db",
        settings_used={},
        tables=[
            TableMapping(
                source_table="raw_party",
                class_candidate_set=[
                    ClassCandidate(
                        uri="http://example.org/ontology#Party",
                        label="Party",
                        rationale="Primary class",
                        confidence_score=0.99,
                    )
                ],
                column_mappings=[plain_mapping, date_mapping],
            )
        ],
        total_pending_review=0,
    )


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_audit():
    return RowAudit(
        bronze_source_ref="raw_party.party_id=P1",
        run_id="test-run-001",
        transformation_timestamp=datetime.now(timezone.utc),
        mapping_confidence_score=0.95,
        transformation_strategy="DateNormaliser",
    )
