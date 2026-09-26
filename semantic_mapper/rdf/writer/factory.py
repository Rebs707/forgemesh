# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from semantic_mapper.config.settings import Settings
from semantic_mapper.core.exceptions import MappingConfigError
from semantic_mapper.rdf.writer.base import RdfWriter
from semantic_mapper.rdf.writer.fuseki import FusekiWriter
from semantic_mapper.rdf.writer.rdflib_writer import RdflibWriter

_REGISTRY: dict[str, type[RdfWriter]] = {
    "fuseki": FusekiWriter,
    "rdflib": RdflibWriter,
    # "blazegraph": BlazegraphWriter,
}


def get_rdf_writer(settings: Settings) -> RdfWriter:
    """Resolve and instantiate the configured RdfWriter.

    Raises MappingConfigError for unknown RDF_WRITER values.
    """
    writer_key = settings.RDF_WRITER.lower()
    writer_class = _REGISTRY.get(writer_key)
    if writer_class is None:
        known = ", ".join(sorted(_REGISTRY.keys()))
        raise MappingConfigError(
            f"Unknown RDF_WRITER '{settings.RDF_WRITER}'. "
            f"Registered writers: [{known}]"
        )

    if writer_class is FusekiWriter:
        return FusekiWriter(
            fuseki_url=settings.FUSEKI_URL,
            ontology_named_graph=settings.ONTOLOGY_NAMED_GRAPH,
            admin_user=settings.FUSEKI_ADMIN_USER,
            admin_password=settings.FUSEKI_ADMIN_PASSWORD,
        )

    # RdflibWriter and future writers take no constructor args
    return writer_class()
