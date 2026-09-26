# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

from rdflib import Dataset, Graph, URIRef

from semantic_mapper.core.exceptions import RdfSchemaInitialisationError, RdfWriterError
from semantic_mapper.rdf.models import RdfPipelineStats
from semantic_mapper.rdf.writer.base import RdfWriter

logger = logging.getLogger(__name__)


class RdflibWriter(RdfWriter):
    """In-memory rdflib writer. No HTTP — intended for tests and local inspection."""

    def __init__(self) -> None:
        self._store = Dataset()
        self._named_graph_uri: Optional[URIRef] = None
        self._started_at: datetime = datetime.now(timezone.utc)

    @property
    def graph(self) -> Graph:
        """Return the named graph for test assertions."""
        if self._named_graph_uri is None:
            return Graph()
        return self._store.get_context(self._named_graph_uri)

    def initialise(
        self,
        dataset_name: str,
        named_graph: str,
        ontology_path: Optional[str] = None,
    ) -> None:
        self._named_graph_uri = URIRef(named_graph)

        # Clear any existing triples in this named graph
        ctx = self._store.get_context(self._named_graph_uri)
        self._store.remove_context(ctx)

        if ontology_path:
            try:
                onto_graph = Graph()
                onto_graph.parse(ontology_path)
                onto_ctx = self._store.get_context(
                    URIRef(f"{named_graph}/ontology")
                )
                for triple in onto_graph:
                    onto_ctx.add(triple)
            except Exception as exc:
                raise RdfSchemaInitialisationError(
                    f"Failed to load ontology from '{ontology_path}': {exc}"
                ) from exc

    def write_batch(self, triples: List[Tuple[URIRef, URIRef, Any]]) -> int:
        if self._named_graph_uri is None:
            raise RdfWriterError("initialise() must be called before write_batch()")
        try:
            ctx = self._store.get_context(self._named_graph_uri)
            for triple in triples:
                ctx.add(triple)
            return len(triples)
        except Exception as exc:
            raise RdfWriterError(f"write_batch failed: {exc}") from exc

    def finalise(
        self,
        run_id: str,
        triples_written: int,
        tables_processed: List[str],
        project_name: Optional[str] = None,
        ontology_source: Optional[str] = None,
    ) -> RdfPipelineStats:
        finished_at = datetime.now(timezone.utc)
        duration = (finished_at - self._started_at).total_seconds()
        return RdfPipelineStats(
            run_id=run_id,
            triples_written=triples_written,
            tables_processed=tables_processed,
            named_graph=str(self._named_graph_uri or ""),
            started_at=self._started_at,
            finished_at=finished_at,
            duration_seconds=duration,
        )
