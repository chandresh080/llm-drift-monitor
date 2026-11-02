"""Shared synthetic sampling and shift-injection utilities for drift experiments."""

from typing import Optional

import numpy as np

DEFAULT_LENGTH_MEAN = 100.0
DEFAULT_LENGTH_STD = 15.0
DEFAULT_VALID_P = 0.95
EMBEDDING_DIM = 384
EMBEDDING_STD = 1.0


def make_lengths(n: int, mean: float = DEFAULT_LENGTH_MEAN, std: float = DEFAULT_LENGTH_STD, rng=None) -> np.ndarray:
    """Sample response lengths (characters) ~ Normal(mean, std)."""
    rng = rng or np.random.default_rng()
    return np.asarray(rng.normal(mean, std, n), dtype=float).round().clip(min=1)


def make_validity(n: int, p_valid: float = DEFAULT_VALID_P, rng=None) -> np.ndarray:
    """Sample validity flags (1=valid, 0=invalid) ~ Binomial(n, p_valid)."""
    rng = rng or np.random.default_rng()
    return np.asarray(rng.binomial(1, p_valid, n), dtype=float)


def make_embeddings(n: int, mean=None, std: float = EMBEDDING_STD, rng=None) -> np.ndarray:
    """Sample embedding matrices ~ MultivariateNormal(mean, I*std^2)."""
    rng = rng or np.random.default_rng()
    mu = np.zeros(EMBEDDING_DIM) if mean is None else np.atleast_1d(np.asarray(mean, dtype=float))
    cov = np.eye(len(mu)) * float(std) ** 2
    return rng.multivariate_normal(mu, cov, size=n)


def shift_lengths(cur, delta_mean: float, std: float = DEFAULT_LENGTH_STD):
    """Shift response lengths by delta_mean (caller expresses in std units)."""
    return np.asarray(cur, dtype=float) + float(delta_mean)


def shift_validity(cur, new_p_valid: float, rng=None) -> np.ndarray:
    """Replace validity flags with a new invalid fraction."""
    rng = rng or np.random.default_rng()
    return np.asarray(rng.binomial(1, new_p_valid, len(cur)), dtype=float)


def shift_embeddings(cur, delta_mean: float, std: float = EMBEDDING_STD):
    """Shift embedding mean by delta_mean (in std units -> vector norm delta_mean)."""
    arr = np.asarray(cur, dtype=float)
    n, dim = arr.shape
    u = np.random.default_rng().normal(size=dim)
    u = u / np.linalg.norm(u)
    return arr + float(delta_mean) * u
