# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from semantic_mapper.core.exceptions import RdfSchemaInitialisationError, RdfWriterError
from semantic_mapper.rdf.writer.fuseki import FusekiWriter

FUSEKI_URL = "http://localhost:3030"
ONTOLOGY_GRAPH = "http://mesh.local/graph/ontology"
DATASET = "forgemesh"
NAMED_GRAPH = "http://mesh.local/graph/current"
UPDATE_URL = f"{FUSEKI_URL}/{DATASET}/update"


def _ok(status_code=200):
    m = MagicMock()
    m.status_code = status_code
    m.raise_for_status = MagicMock()
    return m


@pytest.fixture
def writer():
    return FusekiWriter(
        fuseki_url=FUSEKI_URL,
        ontology_named_graph=ONTOLOGY_GRAPH,
        admin_user="admin",
        admin_password="admin",
    )


class TestFusekiWriterInitialise:
    @patch("semantic_mapper.rdf.writer.fuseki.requests.post")
    def test_posts_to_datasets_endpoint(self, mock_post, writer):
        mock_post.return_value = _ok(201)
        writer.initialise(DATASET, NAMED_GRAPH)

        first_call = mock_post.call_args_list[0]
        assert first_call == call(
            f"{FUSEKI_URL}/$/datasets",
            data={"dbName": DATASET, "dbType": "tdb2"},
            auth=("admin", "admin"),
        )

    @patch("semantic_mapper.rdf.writer.fuseki.requests.post")
    def test_drops_named_graph_via_sparql_update(self, mock_post, writer):
        """Graph clear must use SPARQL Update DROP SILENT, not a GSP DELETE."""
        mock_post.return_value = _ok(200)
        writer.initialise(DATASET, NAMED_GRAPH)

        # Second POST call is the DROP SILENT update
        update_calls = [
            c for c in mock_post.call_args_list
            if c.args and UPDATE_URL in str(c.args[0])
        ]
        assert len(update_calls) == 1
        update_body = update_calls[0].kwargs["data"]["update"]
        assert f"DROP SILENT GRAPH <{NAMED_GRAPH}>" in update_body

    @patch("semantic_mapper.rdf.writer.fuseki.requests.post")
    def test_409_on_create_is_accepted(self, mock_post, writer):
        mock_post.return_value = _ok(409)
        # Should not raise
        writer.initialise(DATASET, NAMED_GRAPH)

    @patch("semantic_mapper.rdf.writer.fuseki.requests.post")
    def test_drop_silent_tolerates_nonexistent_graph(self, mock_post, writer):
        """DROP SILENT is idempotent: 200 whether the graph existed or not."""
        mock_post.return_value = _ok(200)
        writer.initialise(DATASET, NAMED_GRAPH)
        # 200 from the update endpoint — should not raise

    @patch("semantic_mapper.rdf.writer.fuseki.requests.post")
    def test_http_error_raises_schema_init_error(self, mock_post, writer):
        import requests as req
        mock_post.side_effect = req.RequestException("connection refused")

        with pytest.raises(RdfSchemaInitialisationError, match="forgemesh"):
            writer.initialise(DATASET, NAMED_GRAPH)

    @patch("semantic_mapper.rdf.writer.fuseki.requests.post")
    def test_no_delete_call_made(self, mock_post, writer):
        """The GSP DELETE endpoint must NOT be used — SPARQL Update is the write path."""
        mock_post.return_value = _ok(200)
        writer.initialise(DATASET, NAMED_GRAPH)

        urls_called = [str(c.args[0]) for c in mock_post.call_args_list if c.args]
        # "/data" appears as a substring of "/datasets" — check for the GSP path specifically
        assert not any(u.rstrip("/").endswith("/data") for u in urls_called), (
            "initialise() must not call the GSP /data endpoint for graph clearing"
        )


class TestFusekiWriterWriteBatch:
    @patch("semantic_mapper.rdf.writer.fuseki.requests.post")
    def test_write_batch_posts_sparql_update(self, mock_post, writer):
        mock_post.return_value = _ok(200)
        writer.initialise(DATASET, NAMED_GRAPH)
        mock_post.reset_mock()
        mock_post.return_value = _ok(200)

        from rdflib import RDF, URIRef
        triples = [(URIRef("http://ex.org/p1"), RDF.type, URIRef("http://ex.org/Patient"))]
        count = writer.write_batch(triples)

        assert count == 1
        assert mock_post.called
        call_kwargs = mock_post.call_args
        # Must POST to /update endpoint, not /data
        assert call_kwargs.args[0] == UPDATE_URL
        # Must use form-encoded SPARQL Update body, not N-Triples content-type
        assert "update" in call_kwargs.kwargs["data"]

    @patch("semantic_mapper.rdf.writer.fuseki.requests.post")
    def test_write_batch_embeds_named_graph(self, mock_post, writer):
        """The INSERT DATA must name the target graph explicitly in the query body."""
        mock_post.return_value = _ok(200)
        writer.initialise(DATASET, NAMED_GRAPH)
        mock_post.reset_mock()
        mock_post.return_value = _ok(200)

        from rdflib import RDF, URIRef
        writer.write_batch([(URIRef("http://ex.org/s"), RDF.type, URIRef("http://ex.org/C"))])

        update_str = mock_post.call_args.kwargs["data"]["update"]
        assert f"GRAPH <{NAMED_GRAPH}>" in update_str
        assert "INSERT DATA" in update_str

    @patch("semantic_mapper.rdf.writer.fuseki.requests.post")
    def test_write_batch_before_initialise_raises(self, mock_post, writer):
        with pytest.raises(RdfWriterError, match="initialise"):
            from rdflib import RDF, URIRef
            writer.write_batch(
                [(URIRef("http://ex.org/p1"), RDF.type, URIRef("http://ex.org/C"))]
            )

    @patch("semantic_mapper.rdf.writer.fuseki.requests.post")
    def test_http_error_in_write_raises(self, mock_post, writer):
        import requests as req

        mock_post.return_value = _ok(200)
        writer.initialise(DATASET, NAMED_GRAPH)
        mock_post.reset_mock()
        mock_post.side_effect = req.RequestException("timeout")

        from rdflib import RDF, URIRef
        with pytest.raises(RdfWriterError, match="Fuseki write_batch failed"):
            writer.write_batch(
                [(URIRef("http://ex.org/p1"), RDF.type, URIRef("http://ex.org/C"))]
            )


class TestFusekiWriterFinalise:
    @patch("semantic_mapper.rdf.writer.fuseki.requests.post")
    def test_finalise_returns_stats(self, mock_post, writer):
        mock_post.return_value = _ok(200)
        writer.initialise(DATASET, NAMED_GRAPH)

        from semantic_mapper.rdf.models import RdfPipelineStats
        stats = writer.finalise("run-abc", 42, ["gold_patient"])

        assert isinstance(stats, RdfPipelineStats)
        assert stats.run_id == "run-abc"
        assert stats.triples_written == 42
        assert stats.named_graph == NAMED_GRAPH
        assert stats.duration_seconds is not None

    @patch("semantic_mapper.rdf.writer.fuseki.requests.post")
    def test_finalise_emits_load_stamp(self, mock_post, writer):
        """finalise() must write fm:loadRunId and fm:loadTimestamp to the named graph."""
        mock_post.return_value = _ok(200)
        writer.initialise(DATASET, NAMED_GRAPH)
        mock_post.reset_mock()
        mock_post.return_value = _ok(200)

        writer.finalise("run-stamp", 10, [])

        # The load stamp POST goes to /update
        update_calls = [
            c for c in mock_post.call_args_list
            if c.args and UPDATE_URL in str(c.args[0])
        ]
        assert len(update_calls) >= 1
        stamp_body = update_calls[0].kwargs["data"]["update"]
        assert "loadRunId" in stamp_body
        assert "loadTimestamp" in stamp_body
        assert "run-stamp" in stamp_body
