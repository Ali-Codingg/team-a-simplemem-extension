"""Preserve the original same-K-per-layer complexity policy."""
from .common import Budget, complexity_score

def choose_budget(signals, k_min, k_max, weights, **kwargs):
    c = complexity_score(signals, weights)
    k = k_min + round(c * (k_max - k_min))
    return Budget('adaptive', k, k, k, round(c, 4))
