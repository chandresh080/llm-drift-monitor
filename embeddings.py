import numpy as np
from sentence_transformers import SentenceTransformer
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
        """Lazy load the embedding model."""
        if self._model is None:
            self._model = SentenceTransformer(self.model_name, device=self.device)
        return self._model
    
    def extract_embedding(self, text: str) -> List[float]:
        """Extract sentence embedding for a text."""
        if not text or text.strip() == "":
            # Return zero embedding for empty text
            # Get dimension from a dummy encoding
            emb = self.model.encode(["dummy"], convert_to_numpy=True)
            return list(np.zeros(emb.shape[1]))
        embedding = self.model.encode([text], convert_to_numpy=True)
        return list(embedding[0])
    
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
