"""Shared budget representation and validated helpers."""
from dataclasses import dataclass
from typing import Mapping

@dataclass(frozen=True)
class Budget:
    level: str
    semantic_k: int
    keyword_k: int
    structured_k: int
    complexity: float = 0.0


def complexity_score(signals: Mapping[str, float], weights: Mapping[str, float]) -> float:
    return max(0.0, min(1.0, sum(float(w) * float(signals.get(name, 0.0)) for name, w in weights.items())))


def proportional_allocation(scores, total: int):
    """Largest-remainder integer allocation; exactly sums to total."""
    if total < 0:
        raise ValueError('total must be nonnegative')
    clean = [max(0.0, min(1.0, float(x))) for x in scores]
    if sum(clean) == 0:
        clean = [1.0] * len(clean)
    raw = [total * x / sum(clean) for x in clean]
    result = [int(x) for x in raw]
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - result[i]), i))
    for i in order[:total - sum(result)]:
        result[i] += 1
    return result
