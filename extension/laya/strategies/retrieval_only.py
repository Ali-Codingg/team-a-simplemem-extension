"""Independent per-method K based on each Laya signal."""
from .common import Budget, complexity_score

def choose_budget(signals, k_min, k_max, weights, **kwargs):
    def k(name):
        score = max(0.0, min(1.0, float(signals.get(name, 0.0))))
        return k_min + round(score * (k_max - k_min))
    return Budget('retrieval', k('semantic'), k('lexical'), k('symbolic'), round(complexity_score(signals, weights), 4))
