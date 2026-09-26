# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
"""
Accumulated class index for the iterative generator.

Maintains a growing list of OWL classes already committed to the ontology graph.
Supports semantic retrieval (top-k by cosine similarity) so the Gen-LLM prompt
can reference already-declared classes for FK ranges and subclass assertions.

Uses all-MiniLM-L6-v2 (already required by SemanticMatcher) — no new deps.
"""
import logging
from typing import List, Optional

from semantic_mapper.ontology.iterative.models import (
    AccumulatedAbstractPropertySummary,
    AccumulatedClassSummary,
)

logger = logging.getLogger(__name__)

_MODEL_NAME = "all-MiniLM-L6-v2"


class AccumulatedClassIndex:
    """
    Incrementally-built semantic index of OWL classes.

    add() and add_all() register new classes and encode them immediately.
    retrieve() returns the top-k most similar classes to a query string.
    """

    def __init__(self, model_name: str = _MODEL_NAME):
        self._model_name = model_name
        self._model = None            # lazy-loaded
        self._classes: List[AccumulatedClassSummary] = []
        self._embeddings = None       # numpy array, shape (N, D)
        self._abstract_props: List[AccumulatedAbstractPropertySummary] = []
        self._abstract_embeddings = None  # numpy array, shape (M, D)
        self._indexed_prop_uris: set = set()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self._model_name)
            logger.debug("AccumulatedClassIndex: loaded model %s", self._model_name)

    @staticmethod
    def _encode_text(cls: AccumulatedClassSummary) -> str:
        tables = ", ".join(cls.source_tables) if cls.source_tables else "unknown"
        return f"{cls.label} — tables: {tables}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add(self, cls: AccumulatedClassSummary) -> None:
        """Add a single class to the index."""
        import numpy as np
        self._load_model()
        text = self._encode_text(cls)
        embedding = self._model.encode([text], convert_to_numpy=True)  # shape (1, D)
        self._classes.append(cls)
        if self._embeddings is None:
            self._embeddings = embedding
        else:
            self._embeddings = np.vstack([self._embeddings, embedding])

    def add_all(self, classes: List[AccumulatedClassSummary]) -> None:
        """Add multiple classes in one batch (more efficient than repeated add())."""
        if not classes:
            return
        import numpy as np
        self._load_model()
        texts = [self._encode_text(cls) for cls in classes]
        new_embeddings = self._model.encode(texts, convert_to_numpy=True)
        self._classes.extend(classes)
        if self._embeddings is None:
            self._embeddings = new_embeddings
        else:
            self._embeddings = np.vstack([self._embeddings, new_embeddings])

    def retrieve(self, table_name: str, table_label: str = "", top_k: int = 10) -> List[AccumulatedClassSummary]:
        """
        Return the top-k accumulated classes most semantically similar to the
        given table, ordered by descending cosine similarity.

        Returns an empty list if the index is empty.
        """
        if not self._classes:
            return []
        import numpy as np
        self._load_model()
        label = table_label or table_name.replace("_", " ").title()
        query_text = f"{label} derived from {table_name}"
        query_embedding = self._model.encode([query_text], convert_to_numpy=True)  # (1, D)

        # Cosine similarity: dot product of L2-normalised vectors
        norms_stored = np.linalg.norm(self._embeddings, axis=1, keepdims=True)
        norms_stored = np.where(norms_stored == 0, 1e-9, norms_stored)
        normalised_stored = self._embeddings / norms_stored

        norm_q = np.linalg.norm(query_embedding)
        if norm_q == 0:
            norm_q = 1e-9
        normalised_query = query_embedding / norm_q

        similarities = (normalised_stored @ normalised_query.T).flatten()  # (N,)
        k = min(top_k, len(self._classes))
        top_indices = np.argsort(similarities)[::-1][:k]
        return [self._classes[i] for i in top_indices]

    def add_all_abstract_properties(
        self, props: List[AccumulatedAbstractPropertySummary]
    ) -> None:
        """Add abstract (domain-less) properties to the retrieval index."""
        if not props:
            return
        import numpy as np
        self._load_model()
        texts = [f"{p.label} {p.prop_type} range={p.range_uri}" for p in props]
        new_embeddings = self._model.encode(texts, convert_to_numpy=True)
        self._abstract_props.extend(props)
        self._indexed_prop_uris.update(p.uri for p in props)
        if self._abstract_embeddings is None:
            self._abstract_embeddings = new_embeddings
        else:
            self._abstract_embeddings = np.vstack([self._abstract_embeddings, new_embeddings])

    def _retrieve_from_abstract(
        self, query: str, top_k: int, prop_type_filter: Optional[str] = None
    ) -> List[AccumulatedAbstractPropertySummary]:
        candidates = self._abstract_props
        if prop_type_filter:
            candidates = [p for p in candidates if p.prop_type == prop_type_filter]
        if not candidates:
            return []
        import numpy as np
        self._load_model()
        query_embedding = self._model.encode([query], convert_to_numpy=True)

        # Build an embeddings matrix for the filtered subset
        if prop_type_filter:
            indices = [i for i, p in enumerate(self._abstract_props) if p.prop_type == prop_type_filter]
            embeddings = self._abstract_embeddings[indices]
        else:
            embeddings = self._abstract_embeddings

        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1e-9, norms)
        norm_q = np.linalg.norm(query_embedding)
        if norm_q == 0:
            norm_q = 1e-9
        sims = (embeddings / norms @ (query_embedding / norm_q).T).flatten()
        k = min(top_k, len(candidates))
        top_idx = np.argsort(sims)[::-1][:k]
        return [candidates[i] for i in top_idx]

    def retrieve_abstract_properties(
        self, query: str, top_k: int = 8
    ) -> List[AccumulatedAbstractPropertySummary]:
        """Return the top-k abstract properties most similar to *query*."""
        return self._retrieve_from_abstract(query, top_k)

    def retrieve_abstract_object_properties(
        self, query: str, top_k: int = 3
    ) -> List[AccumulatedAbstractPropertySummary]:
        """Return the top-k abstract ObjectProperties most similar to *query*."""
        return self._retrieve_from_abstract(query, top_k, prop_type_filter="ObjectProperty")

    def __len__(self) -> int:
        return len(self._classes)
