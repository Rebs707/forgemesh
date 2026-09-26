# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
import re
import logging
from typing import List, Optional, Dict, Tuple
from rdflib import RDF, RDFS, OWL, Namespace
from semantic_mapper.engine.strategies.base import BaseMappingStrategy
from semantic_mapper.db.models import TableProfile, ColumnProfile
from semantic_mapper.schema.mapping_output import MappingEvidence

logger = logging.getLogger(__name__)

XSD = Namespace("http://www.w3.org/2001/XMLSchema#")

XSD_DATE     = str(XSD.date)
XSD_DATETIME = str(XSD.dateTime)
XSD_STRING   = str(XSD.string)
NUMERIC_XSD  = frozenset([str(XSD.integer), str(XSD.decimal), str(XSD.float), str(XSD.double)])
DATE_FORMAT_LABELS = frozenset(["YYYY-MM-DD", "DD/MM/YYYY", "YYYY/MM/DD"])

# Maps base SQL type name → XSD type URI
SQL_TO_XSD: Dict[str, str] = {
    "VARCHAR":    XSD_STRING,
    "CHAR":       XSD_STRING,
    "TEXT":       XSD_STRING,
    "TINYTEXT":   XSD_STRING,
    "MEDIUMTEXT": XSD_STRING,
    "LONGTEXT":   XSD_STRING,
    "INT":        str(XSD.integer),
    "INTEGER":    str(XSD.integer),
    "TINYINT":    str(XSD.integer),
    "SMALLINT":   str(XSD.integer),
    "BIGINT":     str(XSD.integer),
    "DECIMAL":    str(XSD.decimal),
    "NUMERIC":    str(XSD.decimal),
    "FLOAT":      str(XSD.float),
    "DOUBLE":     str(XSD.double),
    "DATE":       XSD_DATE,
    "DATETIME":   XSD_DATETIME,
    "TIMESTAMP":  XSD_DATETIME,
    "BOOLEAN":    str(XSD.boolean),
    "BIT":        str(XSD.boolean),
}


class TypeMatcher(BaseMappingStrategy):
    """
    Scores each (column, property) pair by comparing structural type signals.

    For FK columns (confirmed via table_profile.foreign_keys, or heuristic
    _id suffix) the scoring is *inverted* relative to non-FK columns:
    FK values reference entity instances, so ObjectProperty is the correct
    structural choice and DatatypeProperty is a type mismatch.

    Inferred XSD type rules (non-FK columns, evaluated in priority order):
      1. VARCHAR/string column with profiler-detected date formats → xsd:date
         (data-proven: the inspector empirically confirmed date values exist)
      2. All other columns: SQL base type mapped via SQL_TO_XSD

    Score matrix — non-FK DatatypeProperties:
      data-proven date + xsd:date range       → 0.92  (empirical + ontology)
      native SQL DATE/DATETIME + xsd:date      → 0.85  (structural only)
      exact XSD match (non-date types)         → 0.80
      compatible numeric types                 → 0.70
      xsd:string ↔ xsd:string                  → 0.60  (weak: string is ubiquitous)
      type mismatch                            → 0.05
      no rdfs:range declared on property       → 0.15  (neutral, no evidence)

    Score matrix — ObjectProperties:
      ObjectProperty + FK column               → 0.80  (correct entity-link type)
      ObjectProperty + categorical column      → 0.10  (compatible via NamedIndividual)
      ObjectProperty (non-categorical, non-FK) → 0.00  (categorically incompatible)

    Score matrix — FK DatatypeProperties:
      DatatypeProperty + FK column             → 0.0   (categorically incompatible; eliminated by type gate)
    """

    def __init__(self, graph, settings):
        super().__init__(graph, settings)
        self._range_cache: Dict[str, Optional[str]] = {}
        self._object_props: set = set()
        self._build_range_cache()

    def _build_range_cache(self) -> None:
        """Pre-compute rdfs:range for every DatatypeProperty and collect ObjectProperty URIs."""
        for prop in self.graph.subjects(RDF.type, OWL.DatatypeProperty):
            ranges = list(self.graph.objects(prop, RDFS.range))
            self._range_cache[str(prop)] = str(ranges[0]) if ranges else None
        for prop in self.graph.subjects(RDF.type, OWL.ObjectProperty):
            self._object_props.add(str(prop))
        logger.debug(
            f"TypeMatcher: cached ranges for {len(self._range_cache)} DatatypeProperties, "
            f"{len(self._object_props)} ObjectProperties disqualified"
        )

    def _infer_xsd(self, profile: ColumnProfile) -> Tuple[str, bool]:
        """
        Returns (inferred_xsd_uri, is_data_proven_date).

        is_data_proven_date=True means the profiler detected date format patterns
        inside a VARCHAR/string column — a stronger signal than a native SQL DATE
        type, because it means we have empirical evidence from the data itself.
        """
        base_sql = re.sub(r"\(.*\)", "", profile.inferred_type).strip().upper()
        native_xsd = SQL_TO_XSD.get(base_sql, XSD_STRING)

        # Upgrade string types to xsd:date if profiler detected date formats in the data
        if native_xsd == XSD_STRING and profile.detected_formats:
            if DATE_FORMAT_LABELS.intersection(set(profile.detected_formats)):
                return XSD_DATE, True

        return native_xsd, False

    def _score(
        self,
        inferred_xsd: str,
        is_data_proven: bool,
        declared_range: Optional[str],
    ) -> Tuple[float, str]:
        if declared_range is None:
            return 0.15, "No rdfs:range declared — type evidence neutral"

        inferred_label = inferred_xsd.split("#")[-1]
        declared_label = declared_range.split("#")[-1]

        # Date handling — split by evidence strength
        if inferred_xsd == XSD_DATE and declared_range == XSD_DATE:
            if is_data_proven:
                return 0.92, (
                    "Data-proven date: profiler detected date formats in VARCHAR data; "
                    "property range is xsd:date"
                )
            return 0.85, "Native SQL DATE type maps to xsd:date range"

        # Exact XSD match (non-date)
        if inferred_xsd == declared_range:
            return 0.80, f"Exact XSD type match: {inferred_label}"

        # Compatible numeric family
        if inferred_xsd in NUMERIC_XSD and declared_range in NUMERIC_XSD:
            return 0.70, (
                f"Compatible numeric types: "
                f"inferred={inferred_label} declared={declared_label}"
            )

        # Both string — very weak signal
        if inferred_xsd == XSD_STRING and declared_range == XSD_STRING:
            return 0.60, "Both xsd:string — weak signal (string is ubiquitous)"

        # Mismatch
        return 0.05, (
            f"Type mismatch: inferred={inferred_label} declared={declared_label}"
        )

    def evaluate_column(
        self, profile: ColumnProfile, table_profile: TableProfile
    ) -> List[MappingEvidence]:
        inferred_xsd, is_data_proven = self._infer_xsd(profile)
        # FK columns (name ends with _id or listed in table foreign_keys) reference
        # another entity — ObjectProperties are semantically appropriate.
        is_fk = profile.name.lower().endswith("_id") or any(
            fk.get("column") == profile.name for fk in table_profile.foreign_keys
        )
        # Categorical columns (low-cardinality with top_values) can map to ObjectProperties
        # via NamedIndividual value-mapping.
        is_categorical = bool(profile.top_values)
        evidence = []

        for prop_uri, declared_range in self._range_cache.items():
            if is_fk:
                # FK columns reference entity instances — DatatypeProperty is categorically
                # wrong.  Score 0.0 so the aggregator's type gate eliminates it entirely.
                score = 0.0
                rationale = "DatatypeProperty — FK column must map to ObjectProperty (entity link)"
            else:
                score, rationale = self._score(inferred_xsd, is_data_proven, declared_range)
            evidence.append(MappingEvidence(
                strategy="TypeMatcher",
                score=score,
                rationale=(
                    f"[inferred={inferred_xsd.split('#')[-1]}, "
                    f"data_proven_date={is_data_proven}] {rationale}"
                ),
                mapped_uri=prop_uri,
            ))

        for prop_uri in self._object_props:
            if is_fk:
                obj_score = 0.80
                obj_rationale = "ObjectProperty — correct type for FK column (entity instance link)"
            elif is_categorical:
                obj_score = 0.10
                obj_rationale = (
                    "ObjectProperty — compatible via NamedIndividual value-mapping "
                    f"(column has {len(profile.top_values)} distinct value(s))"
                )
            else:
                obj_score = 0.0
                obj_rationale = "ObjectProperty — categorically incompatible with scalar column data"
            evidence.append(MappingEvidence(
                strategy="TypeMatcher",
                score=obj_score,
                rationale=(
                    f"[inferred={inferred_xsd.split('#')[-1]}, "
                    f"data_proven_date={is_data_proven}] {obj_rationale}"
                ),
                mapped_uri=prop_uri,
            ))

        return evidence

    def evaluate_table(self, profile: TableProfile) -> List[MappingEvidence]:
        return []  # Tables have no XSD type
