"""Embedding backend selection tests.

The hosted demo runs the embedder on a torch free ONNX Runtime path
(embeddings._OnnxMiniLM) because importing the torch stack breached the free
tier's 512 MB ceiling mid cycle. These tests pin the selection contract
without downloading anything: sentence_transformers and _OnnxMiniLM are
replaced by recorders before the lazy load runs.
"""
import sys
import types

import numpy as np
import pytest

import embeddings
from embeddings import EmbeddingExtractor


class _FakeSentenceTransformer:
    """Records constructor calls for the torch backend path."""

    calls = []

    def __init__(self, model_name, device=None, **kwargs):
        type(self).calls.append(None)
        self.model_name = model_name


class _FakeOnnxMiniLM:
    """Records construction for the ONNX backend path."""

    calls = []
    fail = False

    def __init__(self, model_name, **kwargs):
        type(self).calls.append("onnx")
        if type(self).fail:
            raise RuntimeError("onnx unavailable")
        self.model_name = model_name

    def encode(self, texts, convert_to_numpy=True, **kwargs):
        return np.ones((len(list(texts)), 384))


@pytest.fixture
def fake_backends(monkeypatch):
    _FakeSentenceTransformer.calls = []
    _FakeOnnxMiniLM.calls = []
    _FakeOnnxMiniLM.fail = False
    monkeypatch.setitem(
        sys.modules, "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=_FakeSentenceTransformer))
    # The torch path tunes threads on import; neutralize the real module.
    torch_stub = types.SimpleNamespace(set_num_threads=lambda n: None)
    monkeypatch.setitem(sys.modules, "torch", torch_stub)
    monkeypatch.setattr(embeddings, "_OnnxMiniLM", _FakeOnnxMiniLM)
    return _FakeSentenceTransformer, _FakeOnnxMiniLM


def test_default_backend_is_torch(fake_backends, monkeypatch):
    monkeypatch.delenv("DRIFT_MONITOR_EMBEDDING_BACKEND", raising=False)
    extractor = EmbeddingExtractor()
    assert extractor.model is not None
    torch_calls, onnx_calls = fake_backends
    assert torch_calls.calls == [None] and onnx_calls.calls == []


def test_onnx_backend_selected_when_configured(fake_backends, monkeypatch):
    monkeypatch.setenv("DRIFT_MONITOR_EMBEDDING_BACKEND", "onnx")
    extractor = EmbeddingExtractor()
    assert extractor.model is not None
    torch_calls, onnx_calls = fake_backends
    assert onnx_calls.calls == ["onnx"] and torch_calls.calls == []


def test_onnx_failure_falls_back_to_torch(fake_backends, monkeypatch):
    _, onnx_cls = fake_backends
    onnx_cls.fail = True
    monkeypatch.setenv("DRIFT_MONITOR_EMBEDDING_BACKEND", "onnx")
    extractor = EmbeddingExtractor()
    assert extractor.model is not None
    torch_calls, onnx_calls = fake_backends
    assert onnx_calls.calls == ["onnx"] and torch_calls.calls == [None]


def test_backend_setting_is_case_insensitive(fake_backends, monkeypatch):
    monkeypatch.setenv("DRIFT_MONITOR_EMBEDDING_BACKEND", "  ONNX ")
    extractor = EmbeddingExtractor()
    assert extractor.model is not None
    _, onnx_calls = fake_backends
    assert onnx_calls.calls == ["onnx"]


def test_zero_vector_embedding_shape_is_preserved():
    """Empty text maps to a zero vector of the model's width, not a crash."""
    extractor = EmbeddingExtractor()
    extractor._model = types.SimpleNamespace(
        encode=lambda texts, convert_to_numpy=True: np.ones((len(texts), 384)))
    vector = extractor.extract_embedding("")
    assert len(vector) == 384
    assert all(v == 0.0 for v in vector)
