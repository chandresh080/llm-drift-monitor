import os
import numpy as np
from typing import List, Tuple
import logging

logger = logging.getLogger(__name__)

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
        "onnx" runs the same MiniLM weights on ONNX Runtime, which peaks a
        few hundred MB below the torch stack: the hosted free tier enforces
        a 512 MB ceiling and the torch stack loading mid cycle breached it
        (observed RSS 127 -> 505 MB -> OOM kill). The ONNX export is baked
        into the deployment image, so loading never exports. The default
        stays torch so local runs, experiments and tests behave exactly as
        they always have; a failed ONNX load falls back with a warning.
        """
        if self._model is None:
            backend = (os.environ.get("DRIFT_MONITOR_EMBEDDING_BACKEND")
                       or "").strip().lower()
            if backend == "onnx":
                from sentence_transformers import SentenceTransformer
                try:
                    self._model = SentenceTransformer(
                        self.model_name, device=self.device, backend="onnx",
                        model_kwargs={"file_name": "onnx/model.onnx"})
                    logger.info("Embedding model loaded on the ONNX Runtime "
                                "backend")
                    return self._model
                except Exception as exc:  # fall back to torch below
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
