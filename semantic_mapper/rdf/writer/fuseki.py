# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

import requests
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import XSD

from semantic_mapper.core.exceptions import RdfSchemaInitialisationError, RdfWriterError
from semantic_mapper.rdf.models import RdfPipelineStats
from semantic_mapper.rdf.writer.base import RdfWriter

logger = logging.getLogger(__name__)

_FM = Namespace("http://forgemesh.ai/ontology#")


class FusekiWriter(RdfWriter):
    """Writes RDF triples to an Apache Jena Fuseki dataset via SPARQL Update.

    All writes go through the ``/update`` endpoint (SPARQL 1.1 Update) so the
    load path is identical to the canary-tested INSERT DATA mechanism.  GSP
    (``/data``) is intentionally avoided for data writes: when the process runs
    inside Docker the webapp environment may inject a stale RDF_NAMED_GRAPH from
    its own env_file, and a GSP POST with the wrong graph URI deposits triples in
    the wrong named graph silently.  SPARQL Update with an explicit GRAPH clause
    is resilient to that: the graph URI is embedded in the query string, not
    derived from a query parameter that can be shadowed by ambient env vars.

    Ontology loading still uses GSP (``/data``) because it targets a separate
    named graph and runs once per initialise() call.
    """

    def __init__(
        self,
        fuseki_url: str,
        ontology_named_graph: str,
        admin_user: str = "admin",
        admin_password: str = "admin",
    ) -> None:
        self._fuseki_url = fuseki_url.rstrip("/")
        self._ontology_named_graph = ontology_named_graph
        self._auth = (admin_user, admin_password)
        self._dataset_name: Optional[str] = None
        self._named_graph: Optional[str] = None
        self._started_at: datetime = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _update(self, sparql: str, timeout: int = 600) -> None:
        """POST a SPARQL Update string to the dataset's /update endpoint.

        ``timeout`` defaults to 600 s (10 min) — large graph DROPs on MIMIC-scale
        datasets can take several minutes; the old default of 30 s caused spurious
        purge failures.
        """
        url = f"{self._fuseki_url}/{self._dataset_name}/update"
        resp = requests.post(
            url,
            data={"update": sparql},
            auth=self._auth,
            timeout=timeout,
        )
        resp.raise_for_status()

    def _triples_to_update(self, triples: List[Tuple[URIRef, URIRef, Any]]) -> str:
        """Serialise a list of triples as a SPARQL Update INSERT DATA statement.

        Uses rdflib's N-Triples serialiser to produce the triple block.
        N-Triples is a strict subset of SPARQL triple syntax, so the output is
        safe to embed directly inside an INSERT DATA { GRAPH <…> { … } } body.
        """
        g = Graph()
        for triple in triples:
            g.add(triple)
        nt = g.serialize(format="nt").strip()
        return (
            f"INSERT DATA {{\n"
            f"  GRAPH <{self._named_graph}> {{\n"
            f"{nt}\n"
            f"  }}\n"
            f"}}"
        )

    # ------------------------------------------------------------------
    # RdfWriter interface
    # ------------------------------------------------------------------

    def initialise(
        self,
        dataset_name: str,
        named_graph: str,
        ontology_path: Optional[str] = None,
    ) -> None:
        self._dataset_name = dataset_name
        self._named_graph = named_graph

        try:
            # 1. Create dataset — idempotent (409 = already exists)
            resp = requests.post(
                f"{self._fuseki_url}/$/datasets",
                data={"dbName": dataset_name, "dbType": "tdb2"},
                auth=self._auth,
            )
            if resp.status_code not in (200, 201, 409):
                resp.raise_for_status()

            # 2. Clear the data named graph via SPARQL Update.
            #    DROP SILENT is idempotent — 200 whether the graph existed or not.
            self._update(f"DROP SILENT GRAPH <{named_graph}>")

            # 3. Load ontology into its own named graph if provided (GSP is fine
            #    here — the target is the ontology graph, not the data graph, so
            #    there is no risk of the ambient env-var collision described above).
            #    DROP the ontology graph first: GSP POST MERGES, so without this a
            #    re-materialise would accumulate stale schema triples whenever the
            #    ontology changes between runs (e.g. a property re-typed by the
            #    nested-column repurposing) — leaving dual-typed, OWL-Full leftovers.
            if ontology_path:
                self._update(f"DROP SILENT GRAPH <{self._ontology_named_graph}>")
                onto_graph = Graph()
                onto_graph.parse(ontology_path)
                nt_data = onto_graph.serialize(format="nt")
                post_resp = requests.post(
                    f"{self._fuseki_url}/{dataset_name}/data",
                    params={"graph": self._ontology_named_graph},
                    data=nt_data.encode("utf-8") if isinstance(nt_data, str) else nt_data,
                    headers={"Content-Type": "application/n-triples"},
                    auth=self._auth,
                )
                post_resp.raise_for_status()

        except requests.RequestException as exc:
            raise RdfSchemaInitialisationError(
                f"Failed to initialise Fuseki dataset '{dataset_name}': {exc}"
            ) from exc

    def write_batch(self, triples: List[Tuple[URIRef, URIRef, Any]]) -> int:
        if not self._dataset_name or not self._named_graph:
            raise RdfWriterError("initialise() must be called before write_batch()")

        try:
            self._update(self._triples_to_update(triples))
            return len(triples)
        except requests.RequestException as exc:
            raise RdfWriterError(f"Fuseki write_batch failed: {exc}") from exc

    def _compact(self) -> None:
        """Trigger TDB2 journal compaction to prevent unbounded WAL growth."""
        try:
            resp = requests.post(
                f"{self._fuseki_url}/$/compact/{self._dataset_name}",
                params={"deleteOld": "true"},
                auth=self._auth,
            )
            if resp.status_code not in (200, 201):
                logger.warning(
                    "Fuseki compaction returned unexpected status %s — journal may grow",
                    resp.status_code,
                )
            else:
                logger.info(
                    "Fuseki TDB2 compaction triggered for dataset '%s'",
                    self._dataset_name,
                )
        except requests.RequestException as exc:
            logger.warning("Fuseki compaction request failed (non-fatal): %s", exc)

    def finalise(
        self,
        run_id: str,
        triples_written: int,
        tables_processed: List[str],
        project_name: Optional[str] = None,
        ontology_source: Optional[str] = None,
    ) -> RdfPipelineStats:
        finished_at = datetime.now(timezone.utc)

        # Emit a graph-level load stamp so any SPARQL client can determine
        # whether a re-materialisation reached the store and when.
        try:
            graph_node = URIRef(self._named_graph)
            stamp_triples: List[Tuple[URIRef, URIRef, Any]] = [
                (graph_node, _FM.loadRunId, Literal(run_id)),
                (
                    graph_node,
                    _FM.loadTimestamp,
                    Literal(finished_at.isoformat(), datatype=XSD.dateTime),
                ),
            ]
            if project_name:
                stamp_triples.append(
                    (graph_node, _FM.projectName, Literal(project_name))
                )
            if ontology_source:
                stamp_triples.append(
                    (graph_node, _FM.ontologySource, Literal(ontology_source))
                )
            self._update(self._triples_to_update(stamp_triples))
        except Exception as exc:  # non-fatal — stats are still returned
            logger.warning("Failed to write load stamp triple: %s", exc)

        self._compact()
        duration = (finished_at - self._started_at).total_seconds()
        return RdfPipelineStats(
            run_id=run_id,
            triples_written=triples_written,
            tables_processed=tables_processed,
            named_graph=self._named_graph or "",
            started_at=self._started_at,
            finished_at=finished_at,
            duration_seconds=duration,
        )
