# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from unittest.mock import MagicMock, patch
import numpy as np
import pytest

from semantic_mapper.ontology.iterative.embedding_index import AccumulatedClassIndex
from semantic_mapper.ontology.iterative.models import AccumulatedClassSummary


def _cls(local_name: str, label: str, uri: str, tables: list = None) -> AccumulatedClassSummary:
    return AccumulatedClassSummary(
        local_name=local_name, label=label, uri=uri,
        source_tables=tables or [local_name.lower()],
    )


def _mock_model(dim: int = 8):
    """Return a fake SentenceTransformer that produces deterministic embeddings."""
    model = MagicMock()

    def encode(texts, convert_to_numpy=True):
        # Each text gets a deterministic vector based on its hash
        arr = np.zeros((len(texts), dim), dtype=np.float32)
        for i, text in enumerate(texts):
            seed = abs(hash(text)) % 1000
            rng = np.random.default_rng(seed)
            arr[i] = rng.random(dim).astype(np.float32)
        return arr

    model.encode.side_effect = encode
    return model


class TestAccumulatedClassIndex:
    def _index_with_mock(self) -> AccumulatedClassIndex:
        idx = AccumulatedClassIndex()
        idx._model = _mock_model()
        return idx

    def test_empty_retrieve_returns_empty(self):
        idx = self._index_with_mock()
        assert idx.retrieve("raw_labevents") == []

    def test_add_and_retrieve(self):
        idx = self._index_with_mock()
        cls = _cls("Patient", "Patient", "http://x#Patient", ["raw_patients"])
        idx.add(cls)
        assert len(idx) == 1
        result = idx.retrieve("raw_patients", top_k=1)
        assert len(result) == 1
        assert result[0].local_name == "Patient"

    def test_add_all(self):
        idx = self._index_with_mock()
        classes = [
            _cls("Patient", "Patient", "http://x#Patient"),
            _cls("Admission", "Admission", "http://x#Admission"),
            _cls("Labevent", "Labevent", "http://x#Labevent"),
        ]
        idx.add_all(classes)
        assert len(idx) == 3

    def test_top_k_respected(self):
        idx = self._index_with_mock()
        for i in range(10):
            idx.add(_cls(f"Class{i}", f"Class {i}", f"http://x#Class{i}"))
        result = idx.retrieve("some_table", top_k=3)
        assert len(result) == 3

    def test_top_k_capped_at_index_size(self):
        idx = self._index_with_mock()
        idx.add(_cls("A", "A", "http://x#A"))
        result = idx.retrieve("t", top_k=50)
        assert len(result) == 1

    def test_model_lazy_loaded(self):
        """Model should not be loaded until first add/retrieve call."""
        idx = AccumulatedClassIndex()
        assert idx._model is None

    @patch("sentence_transformers.SentenceTransformer")
    def test_model_loaded_on_add(self, mock_st):
        mock_st.return_value = _mock_model()
        idx = AccumulatedClassIndex()
        idx.add(_cls("A", "A", "http://x#A"))
        mock_st.assert_called_once()
