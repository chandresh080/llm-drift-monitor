"""Embedding backend selection tests.

The hosted demo runs the embedder on ONNX Runtime because the torch stack
breached the free tier's 512 MB ceiling mid cycle. These tests pin the
selection contract without downloading any model: sentence_transformers is
replaced by a recorder before the lazy load runs.
"""
import sys
import types

import pytest

from embeddings import EmbeddingExtractor


class _FakeSentenceTransformer:
    """Records constructor calls; optionally fails the ONNX backend."""

    calls = []
    fail_onnx = False

    def __init__(self, model_name, device=None, backend=None, **kwargs):
        type(self).calls.append(backend)
        if backend == "onnx" and type(self).fail_onnx:
            raise RuntimeError("onnx unavailable")
        self.model_name = model_name


@pytest.fixture
def fake_sentence_transformers(monkeypatch):
    _FakeSentenceTransformer.calls = []
    _FakeSentenceTransformer.fail_onnx = False
    monkeypatch.setitem(
        sys.modules, "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=_FakeSentenceTransformer))
    # The torch fallback imports torch for thread tuning; neutralize the
    # module lookup so the test never loads the real stack.
    torch_stub = types.SimpleNamespace(set_num_threads=lambda n: None)
    monkeypatch.setitem(sys.modules, "torch", torch_stub)
    return _FakeSentenceTransformer


def test_default_backend_is_torch(fake_sentence_transformers, monkeypatch):
    monkeypatch.delenv("DRIFT_MONITOR_EMBEDDING_BACKEND", raising=False)
    extractor = EmbeddingExtractor()
    assert extractor.model is not None
    assert fake_sentence_transformers.calls == [None]


def test_onnx_backend_selected_when_configured(
        fake_sentence_transformers, monkeypatch):
    monkeypatch.setenv("DRIFT_MONITOR_EMBEDDING_BACKEND", "onnx")
    extractor = EmbeddingExtractor()
    assert extractor.model is not None
    assert fake_sentence_transformers.calls == ["onnx"]


def test_onnx_failure_falls_back_to_torch(
        fake_sentence_transformers, monkeypatch):
    fake_sentence_transformers.fail_onnx = True
    monkeypatch.setenv("DRIFT_MONITOR_EMBEDDING_BACKEND", "onnx")
    extractor = EmbeddingExtractor()
    assert extractor.model is not None
    assert fake_sentence_transformers.calls == ["onnx", None]


def test_backend_setting_is_case_insensitive(
        fake_sentence_transformers, monkeypatch):
    monkeypatch.setenv("DRIFT_MONITOR_EMBEDDING_BACKEND", "  ONNX ")
    extractor = EmbeddingExtractor()
    assert extractor.model is not None
    assert fake_sentence_transformers.calls == ["onnx"]


def test_zero_vector_embedding_shape_is_preserved():
    """Empty text maps to a zero vector of the model's width, not a crash."""
    extractor = EmbeddingExtractor()
    extractor._model = types.SimpleNamespace(
        encode=lambda texts, convert_to_numpy=True: __import__("numpy").ones(
            (len(texts), 384)))
    vector = extractor.extract_embedding("")
    assert len(vector) == 384
    assert all(v == 0.0 for v in vector)
