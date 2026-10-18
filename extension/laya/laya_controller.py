"""Laya-based retrieval signals for the LayaMem SimpleMem extension.

This module does not call an LLM or perform retrieval. Raw Laya `noul`
outputs are signals, not calibrated probabilities.
"""
from dataclasses import dataclass
from typing import Any, Dict, Optional

from laya import Router


RETRIEVAL_QUESTIONS = {
    "semantic_need": {
        "type": "noul",
        "instructions": (
            "Would semantic similarity search help retrieve supporting memories "
            "for this question?"
        ),
    },
    "lexical_need": {
        "type": "noul",
        "instructions": (
            "Would exact keyword matching or full-text search help retrieve "
            "supporting memories for this question?"
        ),
    },
    "symbolic_need": {
        "type": "noul",
        "instructions": (
            "Would filtering memories by explicit people, entities, locations, "
            "or dates help answer this question?"
        ),
    },
    "multi_hop_need": {
        "type": "noul",
        "instructions": (
            "Does answering this question require combining evidence from "
            "multiple memories rather than relying on a single memory?"
        ),
    },
}


@dataclass(frozen=True)
class RetrievalSignals:
    semantic: float
    lexical: float
    symbolic: float
    multi_hop: float

    def as_dict(self) -> Dict[str, float]:
        return {
            "semantic": self.semantic,
            "lexical": self.lexical,
            "symbolic": self.symbolic,
            "multi_hop": self.multi_hop,
        }


class LayaController:
    """Produce query-level retrieval signals using the installed Laya Router."""

    def __init__(self, router: Optional[Any] = None):
        self.router = router if router is not None else Router()

    def predict(self, query: str) -> RetrievalSignals:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")

        result = self.router.predict({"text": query}, RETRIEVAL_QUESTIONS)
        answers = result["answers"]
        try:
            return RetrievalSignals(
                semantic=float(answers["semantic_need"]["noul"]),
                lexical=float(answers["lexical_need"]["noul"]),
                symbolic=float(answers["symbolic_need"]["noul"]),
                multi_hop=float(answers["multi_hop_need"]["noul"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Unexpected Laya prediction format: {result!r}") from exc
