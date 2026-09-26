# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
import re
import logging
from typing import List, Optional, Dict, Tuple

import numpy as np
from rdflib import RDF, RDFS, OWL
from sentence_transformers import SentenceTransformer
from sentence_transformers.util import cos_sim

from semantic_mapper.engine.strategies.base import BaseMappingStrategy
from semantic_mapper.db.models import TableProfile, ColumnProfile
from semantic_mapper.schema.mapping_output import MappingEvidence

logger = logging.getLogger(__name__)

MODEL_NAME = "all-MiniLM-L6-v2"


def _expand_camel(text: str) -> str:
    """Expand a camelCase local name to spaced lowercase words.

    Examples:
        hasDateOfBirth  → date of birth
        hasRiskCategory → risk category
        isQuoted        → quoted
    """
    text = re.sub(r"^(has|is)", "", text)
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    return text.lower().strip()


def _prepare_column_text(name: str) -> str:
    """Normalise a source column name to natural-language words.

    Examples:
        email_addr  → email addr
        full_nm     → full nm
        party_id    → party id
        raw_dob     → dob
    """
    cleaned = re.sub(r"^(raw_)", "", name)
    return cleaned.replace("_", " ").strip()


class SemanticMatcher(BaseMappingStrategy):
    """
    Scores each (column, ontology property) pair using cosine similarity
    between sentence embeddings of the column name and the property label.

    Property text used (in priority order):
      1. rdfs:label if declared (e.g. "email address", "date of birth")
      2. Expanded camelCase local name with has/is prefix stripped
         (e.g. hasRiskCategory → "risk category")

    Column text: underscores replaced with spaces, raw_ prefix stripped.

    Model: all-MiniLM-L6-v2 (local, ~90 MB, no API cost).
    Property embeddings are pre-computed once at init and reused.
    """

    def __init__(self, graph, settings):
        super().__init__(graph, settings)
        logger.info(f"SemanticMatcher: loading model '{MODEL_NAME}' ...")
        self._model = SentenceTransformer(MODEL_NAME)
        logger.info("SemanticMatcher: model ready")

        # Parallel structures for batch similarity
        self._prop_uris:   List[str]        = []
        self._prop_texts:  List[str]        = []
        self._prop_matrix: Optional[np.ndarray] = None  # shape (n_props, dim)

        self._build_prop_cache()

    def _build_prop_cache(self) -> None:
        """Pre-compute embeddings for every DatatypeProperty and ObjectProperty."""
        props = (
            list(self.graph.subjects(RDF.type, OWL.DatatypeProperty))
            + list(self.graph.subjects(RDF.type, OWL.ObjectProperty))
        )

        for prop in props:
            label = self.graph.value(prop, RDFS.label)
            frag = str(prop).split("#")[-1].split("/")[-1]
            text = str(label) if label else _expand_camel(frag)
            self._prop_uris.append(str(prop))
            self._prop_texts.append(text)

        if not self._prop_uris:
            logger.warning("SemanticMatcher: no properties found in graph")
            return

        logger.info(
            f"SemanticMatcher: encoding {len(self._prop_uris)} property embeddings ..."
        )
        self._prop_matrix = self._model.encode(
            self._prop_texts, convert_to_numpy=True, show_progress_bar=False
        )
        logger.info("SemanticMatcher: property embeddings cached")

    def evaluate_column(
        self, profile: ColumnProfile, table_profile: TableProfile
    ) -> List[MappingEvidence]:
        if self._prop_matrix is None:
            return []

        col_text = _prepare_column_text(profile.name)
        col_emb = self._model.encode(
            col_text, convert_to_numpy=True, show_progress_bar=False
        )

        # Batch cosine similarity: shape (1, n_props) → flat list
        scores = cos_sim(col_emb, self._prop_matrix)[0].tolist()

        evidence = []
        for uri, prop_text, raw_score in zip(
            self._prop_uris, self._prop_texts, scores
        ):
            score = round(float(max(0.0, min(1.0, raw_score))), 4)
            evidence.append(MappingEvidence(
                strategy="SemanticMatcher",
                score=score,
                rationale=(
                    f'Cosine similarity: "{col_text}" ↔ "{prop_text}" = {score:.4f}'
                ),
                mapped_uri=uri,
            ))

        return evidence

    def evaluate_table(self, profile: TableProfile) -> List[MappingEvidence]:
        # TODO: implement table-level class matching against ontology class labels
        return []
