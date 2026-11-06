"""Sequential alert policy for repeated monitoring cycles.

The Benjamini Hochberg correction inside a cycle handles comparisons across
the three tests. It does nothing about the temporal multiple testing problem:
run the monitor daily for a year and even a calibrated per cycle test will
eventually cry wolf. Two complementary mechanisms live here, and it matters
to be precise about what each one is.

1. Alert persistence: an alert only fires once drift has been flagged in
   `min_consecutive_cycles` consecutive stored results, including the current
   one. This is an operational alert filter that removes isolated borderline
   flags; it is simple to reason about and to test, but it is not a formal
   sequential error control procedure and this module makes no such claim
   for it.

2. Alpha spending: a conservative discrete spending schedule, off by default.
   Cycle t of a planning horizon T may test at

       alpha_t = alpha * (2t - 1) / T^2

   which sums over t = 1..T to exactly alpha and spends very little early.
   This is a custom quadratic spending schedule, not the classical
   O'Brien-Fleming boundary. Used with the Holm correction within each cycle,
   the combined procedure holds the family-wise error spent across cycles to
   alpha over the declared horizon, assuming valid p values. One honest
   limitation: the permutation MMD's minimum attainable p value is about
   1/501, so at very early cycles the boundary is below what any stored p
   value could ever clear, and no claim extends beyond the declared planning
   horizon.
"""

from typing import List


def spent_alpha(cycle_index: int, horizon: int, alpha: float) -> float:
    """Per cycle significance level under the quadratic spending schedule.

    `cycle_index` is 1-based (the first monitoring cycle is 1) and `horizon`
    is the planning horizon T. Increments sum to exactly alpha over T cycles
    and are smallest at the start, so an early alert needs strong evidence.
    """
    if horizon < 1 or cycle_index < 1:
        return alpha
    return alpha * (2 * cycle_index - 1) / float(horizon * horizon)


def cumulative_spent(cycle_index: int, horizon: int, alpha: float) -> float:
    """Total significance budget consumed after `cycle_index` cycles."""
    if horizon < 1 or cycle_index < 1:
        return 0.0
    return min(alpha, alpha * (cycle_index / float(horizon)) ** 2)


def consecutive_flagged_history(stored_flags: List[int], min_consecutive: int) -> bool:
    """Whether the current cycle completes a run of consecutive rejections.

    `stored_flags` is ordered oldest first and must already include the
    current cycle as its last element. A requirement of 1 reproduces the old
    single-cycle behavior.
    """
    if min_consecutive <= 1:
        return bool(stored_flags and stored_flags[-1])
    if len(stored_flags) < min_consecutive:
        return False
    return all(stored_flags[-min_consecutive:])


def consecutive_streak(stored_flags: List[int]) -> int:
    """Length of the trailing run of rejections (0 for an empty history)."""
    streak = 0
    for flag in reversed(stored_flags):
        if not flag:
            break
        streak += 1
    return streak
