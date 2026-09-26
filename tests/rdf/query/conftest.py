# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from semantic_mapper.config.settings import Settings


@pytest.fixture
def sparql_settings():
    return Settings(
        DATABASE_URL="sqlite:///:memory:",
        SILVER_DATABASE_URL="sqlite:///:memory:",
        GOLD_DATABASE_URL="sqlite:///:memory:",
        FUSEKI_URL="http://localhost:3030",
        FUSEKI_ADMIN_USER="admin",
        FUSEKI_ADMIN_PASSWORD="admin",
        RDF_DATASET_NAME="test-mesh",
        RDF_NAMED_GRAPH="http://mesh.local/graph/current",
        RDF_BASE_URI="http://mesh.local/data/",
        QUERY_MAX_ROWS=10,
        QUERY_MODEL="gpt-4o-mini",
        OPENAI_API_KEY="test-key",
        ONTOLOGY_PATH="",
        LLM_STREAM=False,
    )
