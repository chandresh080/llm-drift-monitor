import numpy as np
from scipy import stats
from scipy.stats import ks_2samp
import random
from typing import List, Tuple, Dict, Any, Optional

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

def rbf_kernel(x: np.ndarray, y: np.ndarray, sigma: float = 1.0) -> float:
    """Compute RBF kernel k(x,y) = exp(-||x-y||^2 / (2*sigma^2))"""
    if sigma <= 0:
        sigma = 1.0
    diff = x - y
    dist_sq = np.sum(diff ** 2)
    return np.exp(-dist_sq / (2 * sigma ** 2))

def mmd_squared(X: np.ndarray, Y: np.ndarray, sigma: float = 1.0) -> float:
    """
    Compute unbiased MMD^2 between two samples using RBF kernel.
    
    MMD^2 = E[k(x,x')] + E[k(y,y')] - 2*E[k(x,y)]
    """
    m = X.shape[0]
    n = Y.shape[0]
    
    if m < 2 or n < 2:
        return 0.0  # Need at least 2 samples per group for unbiased estimate
    
    # Term 1: k(x_i, x_j) for i != j
    term1 = 0.0
    for i in range(m):
        for j in range(m):
            if i != j:
                term1 += rbf_kernel(X[i], X[j], sigma=sigma)
    term1 /= (m * (m - 1))
    
    # Term 2: k(y_i, y_j) for i != j
    term2 = 0.0
    for i in range(n):
        for j in range(n):
            if i != j:
                term2 += rbf_kernel(Y[i], Y[j], sigma=sigma)
    term2 /= (n * (n - 1))
    
    # Term 3: k(x_i, y_j)
    term3 = 0.0
    for i in range(m):
        for j in range(n):
            term3 += rbf_kernel(X[i], Y[j], sigma=sigma)
    term3 /= (m * n)
    
    mmd_sq = term1 + term2 - 2 * term3
    return max(0.0, mmd_sq)  # Ensure non-negative

def mmd_test(X: List[List[float]], Y: List[List[float]], 
             n_permutations: int = 1000, 
             random_seed: int = 42,
             sigma: float = 1.0) -> Tuple[float, float]:
    """
    Perform MMD test using permutation-based p-values.
    
    Args:
        X: Reference embeddings (list of vectors)
        Y: Current embeddings (list of vectors)
        n_permutations: Number of permutations
        random_seed: Random seed for reproducibility
    
    Returns:
        (mmd_statistic, pvalue)
    """
    if len(X) == 0 or len(Y) == 0:
        return 0.0, 1.0
    
    # Convert to numpy arrays
    X_arr = np.array(X)
    Y_arr = np.array(Y)
    
    m = X_arr.shape[0]
    n = Y_arr.shape[0]
    
    # Compute observed MMD^2
    mmd_obs_sq = mmd_squared(X_arr, Y_arr, sigma=sigma)
    mmd_obs = np.sqrt(mmd_obs_sq) if mmd_obs_sq >= 0 else 0.0
    
    # Permutation test
    rng = random.Random(random_seed)
    combined = np.vstack([X_arr, Y_arr])
    count_larger = 0
    
    for _ in range(n_permutations):
        # Permute indices
        indices = list(range(m + n))
        rng.shuffle(indices)
        X_perm = combined[indices[:m]]
        Y_perm = combined[indices[m:m + n]]
        mmd_perm_sq = mmd_squared(X_perm, Y_perm, sigma=sigma)
        if mmd_perm_sq >= mmd_obs_sq - 1e-10:  # Allow for numerical precision
            count_larger += 1
    
    # Use plus-one correction for Monte Carlo p-value
    pvalue = (count_larger + 1) / (n_permutations + 1) if n_permutations > 0 else 1.0
    return float(mmd_obs), float(max(0.0, min(1.0, pvalue)))

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
