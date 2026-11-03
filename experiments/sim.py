"""Shared synthetic sampling and shift-injection utilities for drift experiments."""

from typing import Optional
import numpy as np

DEFAULT_LENGTH_MEAN = 100.0
DEFAULT_LENGTH_STD = 15.0
DEFAULT_VALID_P = 0.95
EMBEDDING_DIM = 384
EMBEDDING_STD = 1.0

CATEGORIES = ["factual", "reasoning", "code", "refusal", "formatting"]


def make_lengths(
    n: int,
    mean: float = DEFAULT_LENGTH_MEAN,
    std: float = DEFAULT_LENGTH_STD,
    rng: Optional[np.random.Generator] = None
) -> np.ndarray:
    """Sample response lengths (characters) ~ Normal(mean, std)."""
    rng = rng if rng is not None else np.random.default_rng()
    return np.asarray(rng.normal(mean, std, n), dtype=float).round().clip(min=1.0)


def make_validity(
    n: int,
    p_valid: float = DEFAULT_VALID_P,
    rng: Optional[np.random.Generator] = None
) -> np.ndarray:
    """Sample validity flags (1=valid, 0=invalid/refusal) ~ Binomial(n, p_valid)."""
    rng = rng if rng is not None else np.random.default_rng()
    return np.asarray(rng.binomial(1, p_valid, n), dtype=float)


def make_embeddings(
    n: int,
    dim: int = EMBEDDING_DIM,
    mean: Optional[np.ndarray] = None,
    std: float = EMBEDDING_STD,
    rng: Optional[np.random.Generator] = None
) -> np.ndarray:
    """Sample embedding matrices ~ Normal(mean, std^2 * I) of shape (n, dim)."""
    rng = rng if rng is not None else np.random.default_rng()
    if mean is None:
        emb = rng.normal(0.0, float(std), size=(n, dim))
    else:
        mu = np.asarray(mean, dtype=float)
        emb = rng.normal(0.0, float(std), size=(n, dim)) + mu
    # Normalize embeddings to unit sphere (matching sentence transformer normalized embeddings)
    norms = np.linalg.norm(emb, axis=-1, keepdims=True)
    norms = np.maximum(norms, 1e-12)
    return emb / norms


def shift_lengths(
    cur: np.ndarray,
    delta_mean: float,
    std: float = DEFAULT_LENGTH_STD
) -> np.ndarray:
    """Shift response lengths by delta_mean (expressed in characters or std units)."""
    return (np.asarray(cur, dtype=float) + float(delta_mean)).clip(min=1.0)


def shift_validity(
    cur: np.ndarray,
    new_p_valid: float,
    rng: Optional[np.random.Generator] = None
) -> np.ndarray:
    """Sample new validity flags with adjusted valid fraction."""
    rng = rng if rng is not None else np.random.default_rng()
    p = float(np.clip(new_p_valid, 0.0, 1.0))
    return np.asarray(rng.binomial(1, p, len(cur)), dtype=float)


def shift_embeddings(
    cur: np.ndarray,
    delta_mean: float,
    rng: Optional[np.random.Generator] = None,
    shift_vector: Optional[np.ndarray] = None
) -> np.ndarray:
    """
    Shift embeddings along a fixed or reproducible unit direction by delta_mean standard units,
    then re-normalize to unit sphere.
    """
    arr = np.asarray(cur, dtype=float)
    n, dim = arr.shape
    if shift_vector is not None:
        u = np.asarray(shift_vector, dtype=float)
    else:
        rng = rng if rng is not None else np.random.default_rng()
        u = rng.normal(size=dim)
    u_norm = np.linalg.norm(u)
    u = u / (u_norm if u_norm > 1e-12 else 1.0)
    
    shifted = arr + float(delta_mean) * u
    norms = np.linalg.norm(shifted, axis=-1, keepdims=True)
    norms = np.maximum(norms, 1e-12)
    return shifted / norms
