# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import logging
import threading
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Tuple

from sqlalchemy import Engine, text

from semantic_mapper.config.settings import Settings
from semantic_mapper.gold.models import GoldTableDefinition
from semantic_mapper.rdf.models import RdfPipelineStats
from semantic_mapper.rdf.triple_builder import TripleBuilder
from semantic_mapper.rdf.uri_factory import UriFactory
from semantic_mapper.rdf.nested_expansion import (
    detect_nested_columns, nested_term_names, nested_ontology_triples,
    parse_nested_cell, reconcile_nested_keys,
)
from semantic_mapper.rdf.writer.base import RdfWriter

logger = logging.getLogger(__name__)


class RdfPipeline:
    def __init__(
        self,
        definitions: List[GoldTableDefinition],
        gold_engine: Engine,
        writer: RdfWriter,
        settings: Settings,
    ) -> None:
        self._definitions = definitions
        self._gold_engine = gold_engine
        self._writer = writer
        self._settings = settings

    def run(
        self,
        run_id: Optional[str] = None,
        project_name: Optional[str] = None,
    ) -> RdfPipelineStats:
        run_id = run_id or str(uuid.uuid4())
        logger.info("RdfPipeline starting", extra={"run_id": run_id})

        # Load the ontology BEFORE initialise so any nested-column enrichment is
        # persisted to the .owl first and then loaded into Fuseki's ontology graph.
        ontology_graph = None
        if self._settings.ONTOLOGY_PATH:
            try:
                from semantic_mapper.ontology.loader import OntologyLoader
                ontology_graph = OntologyLoader.load(self._settings.ONTOLOGY_PATH)
                logger.info(
                    "RdfPipeline: ontology loaded for direction detection",
                    extra={"path": self._settings.ONTOLOGY_PATH},
                )
            except Exception as exc:
                logger.warning(
                    "RdfPipeline: could not load ontology for direction detection: %s",
                    exc,
                )

        # Detect packed/nested columns from a gold sample, declare the schema they
        # encode into the ontology (persisting to the .owl so Fuseki and Protégé both
        # see the child class/properties), reconcile child-keys against existing
        # value-mapped vocabularies, and collect the per-table explosion config.
        nested_columns, reconcile_by_key = self._detect_and_enrich_nested(ontology_graph)

        self._writer.initialise(
            self._settings.RDF_DATASET_NAME,
            self._settings.RDF_NAMED_GRAPH,
            ontology_path=self._settings.ONTOLOGY_PATH or None,
        )

        # Terminology sources (option b): resolve code → external concept IRI at
        # materialisation, so terminology-coded object properties mint their code
        # individuals + skos:exactMatch here, one per distinct code. Built whenever
        # configured — the trigger is the ontology's coded properties, not the
        # generation-time ENABLED flag (which only governs the binder pass).
        term_sources: dict = {}
        try:
            from semantic_mapper.ontology.terminology.sources import build_sources
            for s in build_sources(getattr(self._settings, "TERMINOLOGY_SOURCES", {}) or {}):
                term_sources[s.system_label] = s
        except Exception as exc:
            logger.warning("RdfPipeline: could not build terminology sources: %s", exc)

        # TripleBuilder shares a per-run code-individual dedup cache; its races are
        # idempotent (identical triples), so it remains safe to share across threads.
        triple_builder = TripleBuilder(
            self._definitions,
            UriFactory(self._settings.RDF_BASE_URI, self._settings.RDF_SOURCE_NAME),
            ontology_graph=ontology_graph,
            nested_columns=nested_columns,
            reconcile_by_key=reconcile_by_key,
            terminology_sources=term_sources,
        )

        triples_total = 0
        tables_processed: List[str] = []
        lock = threading.Lock()

        is_sqlite = self._gold_engine.dialect.name == "sqlite"
        if is_sqlite:
            for definition in self._definitions:
                count = self._process_definition(definition, triple_builder, run_id)
                triples_total += count
                tables_processed.append(definition.gold_table_name)
        else:
            with ThreadPoolExecutor(max_workers=self._settings.PIPELINE_WORKERS) as pool:
                futures = {
                    pool.submit(
                        self._process_definition, definition, triple_builder, run_id
                    ): definition.gold_table_name
                    for definition in self._definitions
                }
                for future in as_completed(futures):
                    tbl = futures[future]
                    try:
                        count = future.result()
                        with lock:
                            triples_total += count
                            tables_processed.append(tbl)
                    except Exception as exc:
                        logger.error(
                            "RDF table processing failed",
                            extra={"table": tbl, "error": str(exc)},
                        )
                        raise

        ont_source = (
            Path(self._settings.ONTOLOGY_PATH).name
            if self._settings.ONTOLOGY_PATH
            else None
        )
        stats = self._writer.finalise(
            run_id, triples_total, tables_processed,
            project_name=project_name,
            ontology_source=ont_source,
        )
        logger.info(
            "RdfPipeline finished",
            extra={"run_id": run_id, "triples_written": triples_total},
        )
        return stats

    # ------------------------------------------------------------------
    # Nested-column detection + ontology enrichment
    # ------------------------------------------------------------------

    def _detect_and_enrich_nested(self, ontology_graph):
        """Sample each gold table, detect columns that pack a list of flat objects,
        declare the schema they encode into the ontology, reconcile child-keys against
        existing value-mapped vocabularies, and return
        ``({gold_table_name: {gold_column: term_names}}, reconcile_by_key)``.

        Dataset-agnostic — packed keys (e.g. discipline/event) are discovered from the
        data, never hardcoded. Unpacking REPURPOSES the packed column's own mapped
        property (an opaque-string DatatypeProperty) into an ObjectProperty pointing at
        the discovered child class. A child-key whose values resolve to an existing
        value-mapped vocabulary is linked to those individuals instead of emitting a
        duplicate literal. The enriched ontology is persisted to ONTOLOGY_PATH.
        """
        from rdflib import RDF, RDFS, OWL, URIRef

        # Reconciliation inputs, all derived (no dataset knowledge):
        #  reconcile_by_key: field name → {value: existing value-mapped individual URI},
        #    scoped per field name so homonyms across columns cannot cross-link.
        reconcile_by_key: dict = {}
        for d in self._definitions:
            for s in d.column_specs:
                vm = getattr(s, "value_map", None) or {}
                field = (s.source_column or "").split(".")[-1]
                if not vm or not field:
                    continue
                idx = reconcile_by_key.setdefault(field, {})
                for val, uri in vm.items():
                    if uri:
                        idx[str(val).strip()] = uri
        #  individual_class: value-mapped individual URI → its class URI.
        #  obj_prop_by_class: class URI → generic has<Class> object property to reuse.
        individual_class: dict = {}
        obj_prop_by_class: dict = {}
        if ontology_graph is not None:
            classes = set(ontology_graph.subjects(RDF.type, OWL.Class))
            for ind, _, cls in ontology_graph.triples((None, RDF.type, None)):
                if cls in classes:
                    individual_class[str(ind)] = str(cls)
            for p in ontology_graph.subjects(RDF.type, OWL.ObjectProperty):
                plocal = str(p).split("#")[-1]
                for _, _, rng in ontology_graph.triples((p, RDFS.range, None)):
                    if plocal == "has" + str(rng).split("#")[-1]:
                        obj_prop_by_class[str(rng)] = plocal

        nested_columns: dict = {}
        # parent property URI → (term_names, onto_ns) accumulated across tables that
        # share it, so the ontology declaration is emitted once.
        shapes: dict = {}
        for definition in self._definitions:
            sample = self._read_gold_batch(definition.gold_table_name, 200, 0)
            if not sample:
                continue
            columns = list(sample[0].keys())
            detected = detect_nested_columns(sample, columns)
            if not detected:
                continue
            onto_ns = (
                definition.primary_class_uri.rsplit("#", 1)[0] + "#"
                if definition.primary_class_uri and "#" in definition.primary_class_uri
                else None
            )
            if not onto_ns:
                continue
            prop_by_col = {
                s.gold_column_name: s.property_uri for s in definition.column_specs
            }
            per_table: dict = {}
            for column, keys in detected.items():
                prop_uri = prop_by_col.get(column)
                if not prop_uri:
                    # No mapped property for the packed column — cannot repurpose it
                    # into an object link; leave it as the opaque gold literal.
                    continue
                # Gather each key's observed values from the sample, then promote keys
                # whose values resolve to an existing value-mapped vocabulary.
                key_samples: dict = {}
                for row in sample:
                    for obj in parse_nested_cell(row.get(column)) or []:
                        for kk, vv in obj.items():
                            key_samples.setdefault(kk, []).append(vv)
                orig_terms = nested_term_names(column, keys)
                terms = reconcile_nested_keys(
                    orig_terms, key_samples, reconcile_by_key,
                    individual_class, obj_prop_by_class,
                )
                # Literal properties demoted by reconciliation (a key promoted to an
                # object link no longer needs its derived literal property declared).
                demoted = [
                    orig_terms["key_props"][k]
                    for k in terms.get("key_links", {})
                    if k in orig_terms["key_props"]
                ]
                per_table[column] = terms
                shapes[prop_uri] = (terms, onto_ns, demoted)
                logger.info(
                    "Nested column %s.%s → %s entities via %s (keys=%s; linked=%s)",
                    definition.gold_table_name, column,
                    terms["child"], prop_uri.rsplit("#", 1)[-1], keys,
                    {k: v["property"] for k, v in terms.get("key_links", {}).items()},
                )
            if per_table:
                nested_columns[definition.gold_table_name] = per_table

        if shapes and ontology_graph is not None:
            before = len(ontology_graph)
            for prop_uri, (terms, onto_ns, demoted) in shapes.items():
                pp = URIRef(prop_uri)
                child = URIRef(onto_ns + terms["child"])
                # Repurpose: an opaque packed column was mapped as a DatatypeProperty
                # holding the list string; turn it into an ObjectProperty → child.
                ontology_graph.remove((pp, RDF.type, OWL.DatatypeProperty))
                for _, _, r in list(ontology_graph.triples((pp, RDFS.range, None))):
                    if r != child:
                        ontology_graph.remove((pp, RDFS.range, r))
                # Drop any orphan declaration of a literal property whose key was
                # reconciled to an object link to an existing vocabulary.
                for dprop in demoted:
                    for t in list(ontology_graph.triples((URIRef(onto_ns + dprop), None, None))):
                        ontology_graph.remove(t)
                # Domain omitted (None): the property is shared across the packed-column
                # tables, so a single plain rdfs:domain would be wrong and several would
                # intersect; the generator's own domain annotation (if any) is left intact.
                for t in nested_ontology_triples(None, terms, onto_ns, parent_prop=pp):
                    ontology_graph.add(t)
            added = len(ontology_graph) - before
            if self._settings.ONTOLOGY_PATH:
                path = self._settings.ONTOLOGY_PATH
                fmt = "xml" if path.lower().endswith((".owl", ".rdf")) else "turtle"
                try:
                    ontology_graph.serialize(destination=path, format=fmt)
                    logger.info(
                        "Ontology enriched with nested schema (%+d triples, %d shape(s)) → %s",
                        added, len(shapes), path,
                    )
                except Exception as exc:
                    logger.warning("Could not persist nested ontology enrichment: %s", exc)
        return nested_columns, reconcile_by_key

    # ------------------------------------------------------------------
    # Per-definition worker (runs in thread pool)
    # ------------------------------------------------------------------

    def _process_definition(
        self,
        definition: GoldTableDefinition,
        triple_builder: TripleBuilder,
        run_id: str,
    ) -> int:
        tbl = definition.gold_table_name
        batch_size = self._settings.RDF_BATCH_SIZE
        offset = 0
        triple_buffer = []
        triples_written = 0
        # BUG-05: track distinct subject URIs to detect gold rows that merge onto
        # the same RDF individual (duplicate key values → collapsed subjects).
        # A merged row is not "skipped" — it still produces triples — but two gold
        # rows with the same subject URI count as one SPARQL individual, causing a
        # gold-row-count ≠ graph-subject-count discrepancy in check C1.
        seen_subjects: set = set()
        rows_total = 0

        while True:
            rows = self._read_gold_batch(tbl, batch_size, offset)
            if not rows:
                break
            for row in rows:
                built = triple_builder.build(dict(row), definition)
                triple_buffer.extend(built)
                rows_total += 1
                if built:
                    # First triple is always a type assertion; its subject is the entity URI.
                    seen_subjects.add(str(built[0][0]))
            if len(triple_buffer) >= batch_size:
                triples_written += self._writer.write_batch(triple_buffer)
                triple_buffer.clear()
            offset += batch_size

        if triple_buffer:
            triples_written += self._writer.write_batch(triple_buffer)

        merged = rows_total - len(seen_subjects)
        if merged > 0:
            logger.warning(
                "Subject merge in %s: %d gold rows → %d distinct subjects "
                "(%d rows collapsed — duplicate key values produce one RDF individual). "
                "Check for duplicate values in the id_column used for subject URIs.",
                tbl, rows_total, len(seen_subjects), merged,
            )

        logger.info(
            "RDF triples built for table",
            extra={"table": tbl, "triples": triples_written, "run_id": run_id},
        )
        return triples_written

    def _read_gold_batch(self, table: str, batch_size: int, offset: int) -> list:
        is_sqlite = self._gold_engine.dialect.name == "sqlite"
        tbl = f'"{table}"' if is_sqlite else f"`{table}`"
        sql = text(f"SELECT * FROM {tbl} LIMIT {batch_size} OFFSET {offset}")
        with self._gold_engine.connect() as conn:
            return conn.execute(sql).mappings().fetchall()
