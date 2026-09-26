# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import logging
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from sqlalchemy import Engine, text

from semantic_mapper.config.settings import Settings
from semantic_mapper.schema.mapping_output import MappingStatus, SemanticMappingReport
from semantic_mapper.silver.models import (
    PropertyKind,
    QuarantineRecord,
    RowAudit,
    RunStats,
    SilverRow,
    TransformOutcome,
)
from semantic_mapper.silver.transform.date_normaliser import DateNormaliser
from semantic_mapper.silver.transform.null_handler import NullHandler
from semantic_mapper.silver.writer.base import SilverWriter
from semantic_mapper.silver.writer.mysql import MySQLSilverWriter

logger = logging.getLogger(__name__)


class SilverPipeline:
    def __init__(
        self,
        report: SemanticMappingReport,
        bronze_engine: Engine,
        writer: SilverWriter,
        settings: Settings,
        ontology_graph=None,
    ) -> None:
        self._report = report       # frozen; never mutated
        self._bronze_engine = bronze_engine
        self._writer = writer
        self._settings = settings
        # Precompute, single-threaded, the data needed to decide whether a
        # value-mapped column is a genuine subtype discriminator. Plain Python sets
        # / dicts so the per-table worker threads only do lookups (thread-safe).
        self._owl_classes, self._superclasses = self._index_ontology(ontology_graph)

    @staticmethod
    def _index_ontology(graph):
        """Return (set of owl:Class URIs, {class URI: transitive superclass URIs}).

        Used to tell a real subtype discriminator (targets are subclasses of the
        entity) from a plain categorical property whose value-mappings point at
        vocabulary individuals (e.g. gender → MaleGender). A flat ontology with no
        rdfs:subClassOf yields an empty closure, so no column is ever a discriminator.
        """
        if graph is None:
            return set(), {}
        from rdflib import RDF, RDFS, OWL, URIRef
        classes = {str(s) for s in graph.subjects(RDF.type, OWL.Class) if isinstance(s, URIRef)}
        direct: dict = {}
        for s, o in graph.subject_objects(RDFS.subClassOf):
            if isinstance(s, URIRef) and isinstance(o, URIRef):
                direct.setdefault(str(s), set()).add(str(o))

        def _supers(u, seen):
            out = set()
            for p in direct.get(u, ()):
                if p not in seen:
                    seen.add(p)
                    out.add(p)
                    out |= _supers(p, seen)
            return out

        closure = {u: _supers(u, set()) for u in direct}
        return classes, closure

    def _is_subtype_discriminator(self, mapping, primary_class_uri: str) -> bool:
        """A value-mapped column is a discriminated-subtype discriminator ONLY when
        every target is an owl:Class that is rdfs:subClassOf the table's entity class.
        Targets that are vocabulary individuals (gender → MaleGender) are not subtypes:
        the column stays a plain property and the row keeps its table's class.
        """
        uris = {vm.mapped_uri for vm in mapping.value_mappings}
        return bool(uris) and all(
            u in self._owl_classes and primary_class_uri in self._superclasses.get(u, set())
            for u in uris
        )

    def run(self, run_id: Optional[str] = None) -> RunStats:
        run_id = run_id or str(uuid.uuid4())
        logger.info("SilverPipeline starting", extra={"run_id": run_id})

        # 1. Initialise writer — propagate SchemaInitialisationError immediately
        self._writer.initialise(self._report)

        # 2. Build transformer chain (stateless; shared safely across threads)
        transformer_chain = [DateNormaliser(), NullHandler()]

        # Set run context on MySQLSilverWriter if applicable
        if isinstance(self._writer, MySQLSilverWriter):
            self._writer._set_run_context(run_id, "")

        # 3. Process tables — parallel for MySQL, serial for SQLite
        # (SQLite in-memory creates a fresh DB per connection, so threads can't share data)
        is_sqlite = self._bronze_engine.dialect.name == "sqlite"
        if is_sqlite:
            for tm in self._report.tables:
                self._process_table(tm, run_id, transformer_chain)
        else:
            with ThreadPoolExecutor(max_workers=self._settings.PIPELINE_WORKERS) as pool:
                futures = {
                    pool.submit(
                        self._process_table, tm, run_id, transformer_chain
                    ): tm.source_table
                    for tm in self._report.tables
                }
                for future in as_completed(futures):
                    table = futures[future]
                    try:
                        future.result()
                    except Exception as exc:
                        logger.error(
                            "Table processing failed",
                            extra={"table": table, "error": str(exc)},
                        )
                        raise

        # 4. Finalise and return
        stats = self._writer.finalise()
        logger.info("SilverPipeline finished", extra={"run_id": run_id})
        return stats

    # ------------------------------------------------------------------
    # Per-table worker (runs in thread pool)
    # ------------------------------------------------------------------

    def _process_table(
        self,
        table_mapping,
        run_id: str,
        transformer_chain: list,
    ) -> None:
        source_table = table_mapping.source_table
        pk_cols = list(getattr(table_mapping, "primary_key_columns", []) or [])
        confirmed_mappings = [
            m for m in table_mapping.column_mappings
            if m.status == MappingStatus.CONFIRMED
        ]

        primary_class = (
            max(table_mapping.class_candidate_set, key=lambda c: c.confidence_score).uri
            if table_mapping.class_candidate_set
            else f"urn:unknown:{source_table}"
        )

        discriminator_mapping = next(
            (
                m for m in confirmed_mappings
                if m.value_mappings
                and len({vm.mapped_uri for vm in m.value_mappings}) > 1
                and self._is_subtype_discriminator(m, primary_class)
            ),
            None,
        )

        offset = 0
        batch_size = self._settings.SILVER_BATCH_SIZE
        table_row_count = 0
        silver_buffer: List[SilverRow] = []
        quarantine_records: List[QuarantineRecord] = []

        while True:
            with self._bronze_engine.connect().execution_options(
                isolation_level="READ UNCOMMITTED"
            ) as conn:
                rows = conn.execute(
                    text(
                        f"SELECT * FROM `{source_table}` "
                        f"LIMIT {batch_size} OFFSET {offset}"
                    )
                ).mappings().fetchall()

            if not rows:
                break

            table_row_count += len(rows)

            for row_index, bronze_row in enumerate(rows):
                absolute_index = offset + row_index
                bronze_source_ref = self._build_bronze_source_ref(
                    source_table, bronze_row, absolute_index, pk_cols
                )

                expansion_targets = self._resolve_expansion_targets(
                    bronze_row, discriminator_mapping, primary_class
                )

                for expansion_index, (class_uri, discriminator_value) in enumerate(
                    expansion_targets
                ):
                    for mapping in confirmed_mappings:
                        col = mapping.source_column
                        raw_value = bronze_row.get(col)

                        prop_kind = (
                            PropertyKind.OBJECT
                            if mapping.value_mappings
                            else PropertyKind.DATATYPE
                        )

                        prop_local = _resolve_property_local_name(
                            mapping.mapped_property_uri or "",
                            fallback=col,
                        )

                        transformer = next(
                            (t for t in transformer_chain if t.can_transform(mapping)),
                            NullHandler(),
                        )

                        result = transformer.transform(raw_value, mapping)

                        if result.outcome == TransformOutcome.QUARANTINED:
                            if isinstance(self._writer, MySQLSilverWriter):
                                self._writer._record_transformation_error()
                            audit = RowAudit(
                                bronze_source_ref=bronze_source_ref,
                                run_id=run_id,
                                transformation_timestamp=datetime.now(timezone.utc),
                                mapping_confidence_score=mapping.confidence_score,
                                transformation_strategy=result.transformer_name,
                            )
                            quarantine_records.append(
                                QuarantineRecord(
                                    source_table=source_table,
                                    source_column=col,
                                    original_value=result.original_value,
                                    property_uri=mapping.mapped_property_uri,
                                    transformer_name=result.transformer_name,
                                    error_detail=result.error_detail or "",
                                    audit=audit,
                                )
                            )
                            continue

                        if result.outcome == TransformOutcome.NULL_PRESERVED:
                            if isinstance(self._writer, MySQLSilverWriter):
                                self._writer._record_null_preserved()

                        audit = RowAudit(
                            bronze_source_ref=bronze_source_ref,
                            run_id=run_id,
                            transformation_timestamp=datetime.now(timezone.utc),
                            mapping_confidence_score=mapping.confidence_score,
                            transformation_strategy=result.transformer_name,
                        )
                        silver_buffer.append(
                            SilverRow(
                                source_table=source_table,
                                source_column=col,
                                ontology_class_uri=class_uri,
                                property_uri=mapping.mapped_property_uri or "",
                                property_local_name=prop_local,
                                property_kind=prop_kind,
                                value=result.transformed_value,
                                transform_result=result,
                                expansion_index=expansion_index,
                                discriminator_value=discriminator_value,
                                audit=audit,
                            )
                        )

                # Flush buffer when it reaches batch_size
                if len(silver_buffer) >= batch_size:
                    self._writer.write_batch(silver_buffer)
                    silver_buffer.clear()

            offset += batch_size

        # Flush remaining rows for this table
        if silver_buffer:
            self._writer.write_batch(silver_buffer)

        # Write this table's quarantine records
        if quarantine_records:
            self._writer.write_quarantine(quarantine_records)

        if isinstance(self._writer, MySQLSilverWriter):
            self._writer._record_bronze_rows(table_row_count)

        logger.info(
            "Table processed",
            extra={"table": source_table, "rows": table_row_count, "run_id": run_id},
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_bronze_source_ref(
        table: str, row: dict, fallback_index: int, pk_cols=None
    ) -> str:
        # Canonical row key = the inspector-detected PRIMARY KEY (declared > inferred),
        # name-agnostic and composite-aware. This is the unique, stable join key back to
        # bronze and the basis for the RDF subject URI. A column literally named `id` is
        # NOT assumed to be the key — e.g. a non-unique business code like
        # schedules.id would collapse rows; the declared PK (row_id) is used instead.
        if pk_cols:
            present = [c for c in pk_cols if c in row and row[c] is not None]
            if present and len(present) == len(pk_cols):
                key = "+".join(present)
                val = "|".join(str(row[c]) for c in present)
                return f"{table}.{key}={val}"
        # Fallback only when no PK was detected: the legacy name heuristic, then a
        # positional index (_idx=N), resolved by the lineage walker via LIMIT/OFFSET.
        # Do NOT use arbitrary *_id columns — those are FK columns and are non-unique.
        for pk_candidate in (f"{table}_id", "id", "row_id", "rowid"):
            if pk_candidate in row and row[pk_candidate] is not None:
                return f"{table}.{pk_candidate}={row[pk_candidate]}"
        return f"{table}._idx={fallback_index}"

    @staticmethod
    def _resolve_expansion_targets(bronze_row, discriminator_mapping, primary_class_uri):
        """Return list of (class_uri, discriminator_value) for multi-class expansion."""
        if discriminator_mapping is None or not discriminator_mapping.value_mappings:
            return [(primary_class_uri, None)]

        disc_col = discriminator_mapping.source_column
        disc_raw = bronze_row.get(disc_col)
        if disc_raw is None:
            return [(primary_class_uri, None)]

        targets = []
        for rule in discriminator_mapping.value_mappings:
            sv = rule.source_value
            matched = (
                disc_raw == sv
                or str(disc_raw) == str(sv)
            )
            if matched:
                targets.append((rule.mapped_uri, disc_raw))

        return targets or [(primary_class_uri, disc_raw)]


def _resolve_property_local_name(uri: str, fallback: str) -> str:
    local = uri.split("#")[-1] if "#" in uri else uri.split("/")[-1]
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", local).lower()
    return snake if snake and snake != "property" else fallback
