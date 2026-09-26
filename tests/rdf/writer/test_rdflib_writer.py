# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest
from rdflib import RDF, Literal, URIRef
from rdflib.namespace import XSD

from semantic_mapper.core.exceptions import RdfWriterError
from semantic_mapper.rdf.models import RdfPipelineStats
from semantic_mapper.rdf.writer.rdflib_writer import RdflibWriter

NAMED_GRAPH = "http://mesh.local/graph/test"
S = URIRef("http://ex.org/subject/1")
P = URIRef("http://ex.org/prop/name")
O = Literal("Alice", datatype=XSD.string)


class TestRdflibWriterInitialise:
    def test_initialise_sets_named_graph(self):
        writer = RdflibWriter()
        writer.initialise("test-ds", NAMED_GRAPH)
        assert writer.graph is not None

    def test_initialise_clears_previous_triples(self):
        writer = RdflibWriter()
        writer.initialise("test-ds", NAMED_GRAPH)
        writer.write_batch([(S, P, O)])
        assert len(list(writer.graph)) == 1

        # Re-initialise should clear the graph
        writer.initialise("test-ds", NAMED_GRAPH)
        assert len(list(writer.graph)) == 0

    def test_initialise_with_empty_ontology_path_skips_load(self):
        writer = RdflibWriter()
        # Should not raise even though ontology_path is empty string
        writer.initialise("test-ds", NAMED_GRAPH, ontology_path="")

    def test_initialise_with_bad_ontology_path_raises(self):
        writer = RdflibWriter()
        from semantic_mapper.core.exceptions import RdfSchemaInitialisationError
        with pytest.raises(RdfSchemaInitialisationError):
            writer.initialise("test-ds", NAMED_GRAPH, ontology_path="/nonexistent/file.owl")


class TestRdflibWriterWriteBatch:
    def test_write_batch_returns_count(self):
        writer = RdflibWriter()
        writer.initialise("test-ds", NAMED_GRAPH)
        count = writer.write_batch([(S, P, O)])
        assert count == 1

    def test_triples_accessible_via_graph(self):
        writer = RdflibWriter()
        writer.initialise("test-ds", NAMED_GRAPH)
        writer.write_batch([(S, P, O)])
        assert (S, P, O) in writer.graph

    def test_multiple_batches_accumulate(self):
        writer = RdflibWriter()
        writer.initialise("test-ds", NAMED_GRAPH)
        s2 = URIRef("http://ex.org/subject/2")
        writer.write_batch([(S, P, O)])
        writer.write_batch([(s2, P, Literal("Bob", datatype=XSD.string))])
        assert len(list(writer.graph)) == 2

    def test_write_batch_before_initialise_raises(self):
        writer = RdflibWriter()
        with pytest.raises(RdfWriterError):
            writer.write_batch([(S, P, O)])

    def test_empty_batch_returns_zero(self):
        writer = RdflibWriter()
        writer.initialise("test-ds", NAMED_GRAPH)
        count = writer.write_batch([])
        assert count == 0


class TestRdflibWriterFinalise:
    def test_finalise_returns_stats(self):
        writer = RdflibWriter()
        writer.initialise("test-ds", NAMED_GRAPH)
        writer.write_batch([(S, P, O)])
        stats = writer.finalise("run-xyz", 1, ["gold_patient"])

        assert isinstance(stats, RdfPipelineStats)
        assert stats.run_id == "run-xyz"
        assert stats.triples_written == 1
        assert stats.tables_processed == ["gold_patient"]
        assert stats.named_graph == NAMED_GRAPH
        assert stats.duration_seconds is not None
        assert stats.finished_at is not None
