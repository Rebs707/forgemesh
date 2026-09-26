# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from semantic_mapper.config.settings import Settings
from semantic_mapper.core.exceptions import MappingConfigError
from semantic_mapper.rdf.writer.factory import get_rdf_writer
from semantic_mapper.rdf.writer.fuseki import FusekiWriter
from semantic_mapper.rdf.writer.rdflib_writer import RdflibWriter


@pytest.fixture
def base_settings():
    return Settings(
        DATABASE_URL="sqlite:///:memory:",
        SILVER_DATABASE_URL="sqlite:///:memory:",
        GOLD_DATABASE_URL="sqlite:///:memory:",
        ONTOLOGY_PATH="",
    )


class TestGetRdfWriter:
    def test_rdflib_writer_returned(self, base_settings):
        settings = Settings(
            **{
                **base_settings.model_dump(),
                "RDF_WRITER": "rdflib",
            }
        )
        writer = get_rdf_writer(settings)
        assert isinstance(writer, RdflibWriter)

    def test_fuseki_writer_returned(self, base_settings):
        settings = Settings(
            **{
                **base_settings.model_dump(),
                "RDF_WRITER": "fuseki",
            }
        )
        writer = get_rdf_writer(settings)
        assert isinstance(writer, FusekiWriter)

    def test_unknown_writer_raises(self, base_settings):
        settings = Settings(
            **{
                **base_settings.model_dump(),
                "RDF_WRITER": "unknown_writer",
            }
        )
        with pytest.raises(MappingConfigError, match="unknown_writer"):
            get_rdf_writer(settings)

    def test_writer_key_is_case_insensitive(self, base_settings):
        settings = Settings(
            **{
                **base_settings.model_dump(),
                "RDF_WRITER": "RdflIb",
            }
        )
        writer = get_rdf_writer(settings)
        assert isinstance(writer, RdflibWriter)
