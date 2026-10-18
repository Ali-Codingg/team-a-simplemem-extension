"""Complexity sets a shared budget; individual scores allocate it."""
from .common import Budget, complexity_score, proportional_allocation

def choose_budget(signals, k_min, k_max, weights, available=('semantic', 'lexical', 'symbolic'), **kwargs):
    c = complexity_score(signals, weights)
    # Total candidates: 3..20 by default, not three separate 3..20 budgets.
    total = k_min + round(c * (k_max - k_min))
    allowed = tuple(name for name in ('semantic', 'lexical', 'symbolic') if name in available)
    if not allowed:
        raise ValueError('No available retrieval methods')
    counts = dict.fromkeys(('semantic', 'lexical', 'symbolic'), 0)
    allocation = proportional_allocation([signals.get(name, 0.0) for name in allowed], total)
    counts.update(zip(allowed, allocation))
    return Budget('combined', counts['semantic'], counts['lexical'], counts['symbolic'], round(c, 4))
