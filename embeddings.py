import os
import numpy as np
from typing import List, Tuple
import logging

logger = logging.getLogger(__name__)


class _OnnxMiniLM:
    """Torch free ONNX Runtime embedder for MiniLM style models.

    The hosted demo's free tier enforces a 512 MB ceiling. Importing
    sentence_transformers pulls the whole torch stack (~250 MB) into the
    process before any backend decision, which alone pushed cycles past the
    ceiling. This embedder drives the exported ONNX graph directly through
    onnxruntime with the pure Rust tokenizers library, so torch is never
    imported. It reproduces sentence_transformers' all-MiniLM-L6-v2 output
    exactly: attention mask weighted mean pooling over the last hidden
    states, then L2 normalization.
    """

    def __init__(self, model_name: str, max_seq_length: int = 256,
                 intra_op_threads: int = 2):
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from tokenizers import Tokenizer

        tokenizer_path = hf_hub_download(model_name, "tokenizer.json")
        self._tokenizer = Tokenizer.from_file(tokenizer_path)
        # MiniLM pairs truncation at 256 with batch padding to the longest
        # sequence, applied manually below because encode_batch does not pad.
        self._tokenizer.enable_truncation(max_length=max_seq_length)
        self._max_seq_length = max_seq_length

        weights_path = hf_hub_download(model_name, "onnx/model.onnx")
        options = ort.SessionOptions()
        options.intra_op_num_threads = intra_op_threads
        self._session = ort.InferenceSession(
            weights_path, options, providers=["CPUExecutionProvider"])

    def encode(self, texts, convert_to_numpy=True, **kwargs):
        """Embed texts with MiniLM's documented pooling, torch free."""
        del convert_to_numpy, kwargs
        import numpy as np
        encodings = self._tokenizer.encode_batch(list(texts))
        seq_len = max(len(e.ids) for e in encodings) if encodings else 1
        input_ids = np.zeros((len(encodings), seq_len), dtype=np.int64)
        attention = np.zeros((len(encodings), seq_len), dtype=np.int64)
        type_ids = np.zeros((len(encodings), seq_len), dtype=np.int64)
        for row, enc in enumerate(encodings):
            n = len(enc.ids)
            input_ids[row, :n] = enc.ids
            attention[row, :n] = enc.attention_mask
            type_ids[row, :n] = enc.type_ids
        hidden = self._session.run(
            ["last_hidden_state"],
            {"input_ids": input_ids, "attention_mask": attention,
             "token_type_ids": type_ids})[0]
        mask = attention[:, :, None].astype(hidden.dtype)
        summed = (hidden * mask).sum(axis=1)
        counts = np.clip(mask.sum(axis=1), 1e-9, None)
        embeddings = summed / counts
        norms = np.clip(np.linalg.norm(embeddings, axis=1, keepdims=True),
                        1e-12, None)
        return embeddings / norms


class EmbeddingExtractor:
    """Extract sentence embeddings and response features locally."""

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2", device: str = "cpu"):
        self.model_name = model_name
        self.device = device
        self._model = None
        logger.info(f"Initializing embedding model: {model_name} on {device}")
    
    @property
    def model(self):
        """Lazy load the embedding model.

        The backend is selectable through DRIFT_MONITOR_EMBEDDING_BACKEND.
        "onnx" runs the same MiniLM weights through a direct ONNX Runtime +
        tokenizers implementation (_OnnxMiniLM) that never imports torch: the
        hosted free tier enforces a 512 MB ceiling and importing the torch
        stack mid cycle breached it (observed RSS 127 -> 505 MB -> OOM kill,
        and again 354 MB with the sentence_transformers ONNX backend, which
        still imports torch). The model and tokenizer files are baked into
        the deployment image's HF cache, so loading is fully offline. The
        default stays torch so local runs, experiments and tests behave
        exactly as they always have; a failed ONNX load falls back with a
        warning.
        """
        if self._model is None:
            backend = (os.environ.get("DRIFT_MONITOR_EMBEDDING_BACKEND")
                       or "").strip().lower()
            if backend == "onnx":
                try:
                    self._model = _OnnxMiniLM(self.model_name)
                    logger.info("Embedding model loaded on the torch free "
                                "ONNX Runtime backend")
                    return self._model
                except Exception as exc:
                    # The torch fallback costs roughly 500 MB. On a memory
                    # capped host that fallback is not a fallback but an OOM
                    # kill of the whole instance: one that also destroys the
                    # logs and the run record. When the container cannot
                    # afford torch, fail loudly instead - the cycle handler
                    # turns the exception into a failed run carrying the
                    # underlying ONNX failure, which survives to be read.
                    try:
                        from monitor import (_memory_headroom_mb,
                                             _mlflow_headroom_ok)
                        headroom = _memory_headroom_mb()
                    except Exception:
                        headroom = None
                    if headroom is not None and not _mlflow_headroom_ok(headroom):
                        raise RuntimeError(
                            "ONNX embedding backend failed and the container "
                            f"lacks memory headroom for the torch fallback "
                            f"({headroom:.0f} MB free): {exc}") from exc
                    logger.warning("ONNX embedding backend unavailable (%s); "
                                   "falling back to torch", exc)
            import torch
            # Small model: wide thread pools add overhead rather than speed.
            # Respect an explicit OMP_NUM_THREADS from the deployment; cap otherwise.
            omp = os.environ.get("OMP_NUM_THREADS")
            torch.set_num_threads(max(1, int(omp)) if omp else min(2, os.cpu_count() or 1))
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name, device=self.device)
        return self._model

    def release(self) -> None:
        """Drop the loaded model so the process returns to its idle footprint."""
        if self._model is not None:
            self._model = None
            import gc
            gc.collect()
            logger.info("Embedding model released")
    
    def extract_embedding(self, text: str) -> List[float]:
        """Extract sentence embedding for a text."""
        if not text or text.strip() == "":
            # Return zero embedding for empty text
            # Get dimension from a dummy encoding
            emb = self.model.encode(["dummy"], convert_to_numpy=True)
            return list(np.zeros(emb.shape[1]))
        embedding = self.model.encode([text], convert_to_numpy=True)
        return list(embedding[0])

    def extract_embeddings(self, texts: List[str]) -> List[List[float]]:
        """Extract embeddings for several texts in one batched encode call.

        A cycle embeds its whole response batch at once: one pass through the
        model instead of one forward pass per text.
        """
        if not texts:
            return []
        # Empty responses encode as "dummy" then map to a zero vector, so a
        # failed generation cannot masquerade as a real embedding.
        non_empty_texts = [t if (t and t.strip()) else "dummy" for t in texts]
        enc = self.model.encode(non_empty_texts, convert_to_numpy=True)
        results: List[List[float]] = []
        for row, text in zip(enc, texts):
            if not text or not text.strip():
                results.append(list(np.zeros(row.shape[0])))
            else:
                results.append(list(row))
        return results
    
    def extract_response_features(self, response_text: str) -> Tuple[int, bool]:
        """
        Extract scalar response features for KS test.
        Defines a clearly documented scalar feature suitable for KS test.
        
        Primary feature: response length (number of characters).
        This is a simple, interpretable scalar.
        """
        if not response_text:
            response_length = 0
        else:
            response_length = len(response_text)
        
        is_valid = self.is_valid_response(response_text)
        return response_length, is_valid
    
    def is_valid_response(self, response_text: str) -> bool:
        """
        Determine if response is valid.
        A response is considered invalid if it's empty or clearly malformed.
        This is the binary signal used in the two-proportion z-test.
        """
        if not response_text or response_text.strip() == "":
            return False
        # Basic validity checks
        if len(response_text.strip()) < 1:
            return False
        return True
    
    def compute_mean_embedding(self, embeddings: List[List[float]]) -> np.ndarray:
        """Compute mean embedding."""
        if not embeddings:
            return np.array([])
        return np.mean(np.array(embeddings), axis=0)
    
    def pairwise_distances(self, embeddings: List[List[float]]) -> np.ndarray:
        """Compute pairwise cosine similarities converted to distances conceptually.
        Not directly used in MMD but for reference.
        """
        if not embeddings:
            return np.array([])
        emb_array = np.array(embeddings)
        # Compute pairwise Euclidean distances as a simple metric
        from scipy.spatial.distance import pdist
        return pdist(emb_array)
