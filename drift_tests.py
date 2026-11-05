import numpy as np
from scipy import stats
from scipy.stats import ks_2samp
from typing import List, Tuple, Optional

def ks_test(reference_data: List[float], current_data: List[float]) -> Tuple[float, float]:
    """
    Perform Kolmogorov-Smirnov test comparing reference and current distributions.
    
    Args:
        reference_data: List of scalar values from reference window
        current_data: List of scalar values from current window
    
    Returns:
        (statistic, pvalue)
    """
    if len(reference_data) == 0 or len(current_data) == 0:
        # Insufficient data
        return float('nan'), 1.0  # Return non-significant
    
    try:
        statistic, pvalue = ks_2samp(reference_data, current_data)
        return float(statistic), float(pvalue)
    except Exception:
        return float('nan'), 1.0

def two_proportion_z_test(reference_validity: List[int], current_validity: List[int]) -> Tuple[float, float]:
    """
    Perform two-proportion z-test comparing validity proportions.
    
    Args:
        reference_validity: List of 0/1 where 1 = valid response
        current_validity: List of 0/1 where 1 = valid response
    
    Returns:
        (z_statistic, pvalue)
    """
    if len(reference_validity) == 0 or len(current_validity) == 0:
        return float('nan'), 1.0
    
    n1 = len(reference_validity)
    n2 = len(current_validity)
    p1 = np.mean(reference_validity)  # proportion valid in reference
    p2 = np.mean(current_validity)   # proportion valid in current
    
    # Handle edge cases with insufficient counts
    # Need sufficient counts for z-test approximation
    if n1 < 5 or n2 < 5:
        # Too small for reliable z-test; return conservative result
        return float('nan'), 1.0
    
    # Pooled proportion
    p_pooled = (n1 * p1 + n2 * p2) / (n1 + n2)
    
    # Avoid division by zero
    if p_pooled == 0 or p_pooled == 1:
        # All same class
        return 0.0, 1.0
    
    # Standard error
    se = np.sqrt(p_pooled * (1 - p_pooled) * (1/n1 + 1/n2))
    if se == 0:
        return 0.0, 1.0
    
    # Z statistic
    z_stat = (p2 - p1) / se
    # Two-tailed p-value
    pvalue = 2 * (1 - stats.norm.cdf(abs(z_stat)))
    
    return float(z_stat), float(max(0.0, min(1.0, pvalue)))


def proportion_test(reference_validity: List[int], current_validity: List[int]) -> Tuple[float, float, str]:
    """Two-sample test on a binary proportion, choosing the right approximation.

    The z-test is the standard choice, but its normal approximation is poor
    when an expected cell of the 2x2 table falls below five observations.
    With the small windows this monitor runs, that happens often, so the
    test dispatches to Fisher's exact test instead and reports which was used.

    Returns (statistic, pvalue, method) where statistic is the z score for
    the z-test and the odds ratio for Fisher's exact test.
    """
    n1, n2 = len(reference_validity), len(current_validity)
    if n1 == 0 or n2 == 0:
        return float('nan'), 1.0, 'fisher'
    successes = sum(reference_validity) + sum(current_validity)
    pooled = successes / (n1 + n2)
    expected_cells = [pooled * n1, (1 - pooled) * n1, pooled * n2, (1 - pooled) * n2]
    if any(cell < 5 for cell in expected_cells):
        table = [
            [sum(reference_validity), n1 - sum(reference_validity)],
            [sum(current_validity), n2 - sum(current_validity)],
        ]
        odds_ratio, pvalue = stats.fisher_exact(table, alternative='two-sided')
        return float(odds_ratio), float(pvalue), 'fisher'
    z_stat, pvalue = two_proportion_z_test(reference_validity, current_validity)
    return z_stat, pvalue, 'z'

def rbf_kernel(x: np.ndarray, y: np.ndarray, sigma: float = 1.0) -> float:
    """Compute RBF kernel k(x,y) = exp(-||x-y||^2 / (2*sigma^2))"""
    if sigma <= 0:
        sigma = 1.0
    diff = np.asarray(x, dtype=float) - np.asarray(y, dtype=float)
    dist_sq = float(np.sum(diff ** 2))
    return float(np.exp(-dist_sq / (2.0 * sigma ** 2)))

def compute_median_bandwidth(Z: np.ndarray) -> float:
    """
    Compute RBF kernel bandwidth sigma using the median heuristic on pairwise Euclidean distances.
    """
    Z_arr = np.asarray(Z, dtype=float)
    n = len(Z_arr)
    if n < 2:
        return 1.0
    diff = Z_arr[:, np.newaxis, :] - Z_arr[np.newaxis, :, :]
    dist_sq = np.sum(diff ** 2, axis=-1)
    i_upper = np.triu_indices(n, k=1)
    dists = np.sqrt(np.maximum(dist_sq[i_upper], 0.0))
    # Filter out exact duplicates / zero distances if possible
    positive_dists = dists[dists > 1e-6]
    if len(positive_dists) > 0:
        med = float(np.median(positive_dists))
    else:
        med = float(np.median(dists)) if len(dists) > 0 else 1.0
    return med if med > 1e-6 else 1.0

def _compute_kernel_matrix(Z: np.ndarray, sigma: Optional[float] = None) -> Tuple[np.ndarray, float]:
    """Compute the full RBF kernel matrix and effective bandwidth sigma."""
    diff = Z[:, np.newaxis, :] - Z[np.newaxis, :, :]
    dist_sq = np.sum(diff ** 2, axis=-1)
    if sigma is None or sigma <= 0:
        sigma = compute_median_bandwidth(Z)
    gamma = 1.0 / (2.0 * (sigma ** 2))
    K = np.exp(-gamma * dist_sq)
    return K, sigma

def _unbiased_mmd2_from_kernel(K: np.ndarray, idx_x: np.ndarray, idx_y: np.ndarray) -> float:
    """Calculate unbiased MMD^2 from precomputed kernel submatrices."""
    m = len(idx_x)
    n = len(idx_y)
    if m < 2 or n < 2:
        return 0.0
    K_xx = K[np.ix_(idx_x, idx_x)]
    K_yy = K[np.ix_(idx_y, idx_y)]
    K_xy = K[np.ix_(idx_x, idx_y)]
    term1 = (np.sum(K_xx) - np.trace(K_xx)) / (m * (m - 1))
    term2 = (np.sum(K_yy) - np.trace(K_yy)) / (n * (n - 1))
    term3 = np.sum(K_xy) / (m * n)
    return float(term1 + term2 - 2.0 * term3)

def mmd_squared(X: np.ndarray, Y: np.ndarray, sigma: Optional[float] = None) -> float:
    """
    Compute unbiased MMD^2 between two samples using RBF kernel.
    If sigma is None or <= 0, the median heuristic bandwidth is used.
    
    MMD^2 = E[k(x,x')] + E[k(y,y')] - 2*E[k(x,y)]
    """
    X_arr = np.asarray(X, dtype=float)
    Y_arr = np.asarray(Y, dtype=float)
    m = X_arr.shape[0]
    n = Y_arr.shape[0]
    
    if m < 2 or n < 2:
        return 0.0
    
    Z = np.vstack([X_arr, Y_arr])
    K, _ = _compute_kernel_matrix(Z, sigma=sigma)
    idx_x = np.arange(m)
    idx_y = np.arange(m, m + n)
    mmd_sq = _unbiased_mmd2_from_kernel(K, idx_x, idx_y)
    return max(0.0, mmd_sq)

def mmd_test(X: List[List[float]], Y: List[List[float]], 
             n_permutations: int = 1000, 
             random_seed: int = 42,
             sigma: Optional[float] = None) -> Tuple[float, float]:
    """
    Perform MMD test using fast vectorized kernel permutation testing.
    Uses median heuristic bandwidth if sigma is None or <= 0.
    
    Args:
        X: Reference embeddings (list or array of vectors)
        Y: Current embeddings (list or array of vectors)
        n_permutations: Number of permutations
        random_seed: Random seed for reproducibility
        sigma: RBF kernel bandwidth (None for median heuristic)
    
    Returns:
        (mmd_statistic, pvalue)
    """
    if len(X) == 0 or len(Y) == 0:
        return 0.0, 1.0
    
    X_arr = np.asarray(X, dtype=float)
    Y_arr = np.asarray(Y, dtype=float)
    m = X_arr.shape[0]
    n = Y_arr.shape[0]
    
    if m < 2 or n < 2:
        return 0.0, 1.0
    
    Z = np.vstack([X_arr, Y_arr])
    K, _ = _compute_kernel_matrix(Z, sigma=sigma)
    idx_x = np.arange(m)
    idx_y = np.arange(m, m + n)
    
    # Compute observed unbiased MMD^2 (can be slightly negative under null)
    mmd_obs_sq = _unbiased_mmd2_from_kernel(K, idx_x, idx_y)
    mmd_obs_stat = float(np.sqrt(max(0.0, mmd_obs_sq)))
    
    if n_permutations <= 0:
        return mmd_obs_stat, 1.0
        
    rng = np.random.default_rng(random_seed)
    all_indices = np.arange(m + n)
    count_larger = 0
    
    for _ in range(n_permutations):
        perm = rng.permutation(all_indices)
        perm_stat_sq = _unbiased_mmd2_from_kernel(K, perm[:m], perm[m:])
        if perm_stat_sq >= mmd_obs_sq - 1e-12:
            count_larger += 1
            
    pvalue = (count_larger + 1) / (n_permutations + 1)
    return mmd_obs_stat, float(max(0.0, min(1.0, pvalue)))

def benjamini_hochberg_correction(pvalues: List[float], alpha: float = 0.05) -> Tuple[List[float], List[bool]]:
    """
    Apply Benjamini-Hochberg correction for multiple testing.
    
    Args:
        pvalues: List of raw p-values
        alpha: Significance level
    
    Returns:
        (adjusted_pvalues, is_significant)
    """
    if len(pvalues) == 0:
        return [], []
    
    # Remove NaN values - treat as non-significant
    clean_pvalues = []
    for p in pvalues:
        if np.isnan(p) or np.isinf(p):
            clean_pvalues.append(1.0)  # Treat as non-significant
        else:
            clean_pvalues.append(max(0.0, min(1.0, p)))
    
    n = len(clean_pvalues)
    sorted_indices = np.argsort(clean_pvalues)
    sorted_p = np.array(clean_pvalues)[sorted_indices]
    
    # Compute BH adjusted p-values: p_adj = min(p_{(i)} * n / i, 1) with monotonicity
    adjusted_sorted = np.zeros(n)
    for i in range(n - 1, -1, -1):
        rank = i + 1  # 1-based rank in sorted order
        adjusted = sorted_p[i] * n / rank
        if adjusted > 1.0:
            adjusted = 1.0
        if i == n - 1:
            adjusted_sorted[i] = adjusted
        else:
            adjusted_sorted[i] = min(adjusted, adjusted_sorted[i + 1])
    
    # Map back to original order
    adjusted_pvalues = np.zeros(n)
    is_significant = np.zeros(n, dtype=bool)
    
    for i, idx in enumerate(sorted_indices):
        adjusted_pvalues[idx] = adjusted_sorted[i]
        # Significant if adjusted p-value <= alpha
        is_significant[idx] = adjusted_sorted[i] <= alpha
    
    return list(adjusted_pvalues), list(is_significant)


def centroid_distance(X: List[List[float]], Y: List[List[float]]) -> float:
    """Cosine distance between the mean embeddings of two samples.

    The simple baseline the MMD test has to beat: it responds to a mean
    shift in embedding space but is blind to changes in distribution shape,
    spread, or mixture proportions. A large MMD with a small centroid
    distance is the signature of a higher moment change that a mean based
    monitor would miss entirely.
    """
    if len(X) == 0 or len(Y) == 0:
        return 0.0
    A = np.asarray(X, dtype=float)
    B = np.asarray(Y, dtype=float)
    mu_r = A.mean(axis=0)
    mu_c = B.mean(axis=0)
    norm_r = np.linalg.norm(mu_r)
    norm_c = np.linalg.norm(mu_c)
    if norm_r == 0.0 or norm_c == 0.0:
        return 1.0 if norm_r != norm_c else 0.0
    cosine = float(np.dot(mu_r, mu_c) / (norm_r * norm_c))
    return float(1.0 - max(-1.0, min(1.0, cosine)))
