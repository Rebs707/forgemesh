# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
import pytest
from sqlalchemy import create_engine, text
from semantic_mapper.config.settings import Settings

@pytest.fixture
def test_settings():
    return Settings(DATABASE_URL="sqlite:///:memory:")

@pytest.fixture
def messy_db_engine(test_settings):
    engine = create_engine(test_settings.DATABASE_URL)
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE raw_party (party_id VARCHAR(50), dob VARCHAR(20), party_type INT)"))
        conn.execute(text("INSERT INTO raw_party VALUES ('P1', '1978-03-14', 1)")) # ISO
        conn.execute(text("INSERT INTO raw_party VALUES ('P2', '14/03/1982', 1)")) # UK
        conn.execute(text("INSERT INTO raw_party VALUES ('P3', NULL, 2)"))          # Null
        conn.commit()
    return engine
