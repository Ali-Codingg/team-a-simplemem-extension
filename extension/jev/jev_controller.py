"""Jev adapter for extension/laya/laya_retriever.py.

Accepts a question and returns four scores compatible with LayaRetriever.
No retrieval or answer-generation LLM happens here.
"""

from __future__ import annotations

import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

from dotenv import load_dotenv
from typesafe_sdk import Score, TypeSafeClient

# Loads extension/jev/.env without overriding a process-level secret.
load_dotenv(Path(__file__).with_name(".env"))


@dataclass(frozen=True)
class RetrievalSignals:
    """Same attributes and as_dict() interface as LAYA's RetrievalSignals."""

    semantic: float
    lexical: float
    symbolic: float
    multi_hop: float

    def as_dict(self) -> dict[str, float]:
        return {
            "semantic": self.semantic,
            "lexical": self.lexical,
            "symbolic": self.symbolic,
            "multi_hop": self.multi_hop,
        }


# Match LAYA's four question dimensions. Jev uses explicit ordinal rubrics.
RUBRICS: dict[str, tuple[str, list[str]]] = {
    "semantic": (
        "Would semantic similarity search help retrieve supporting memories for this question?",
        [
            "Semantic similarity search is unlikely to help.",
            "Semantic similarity search may help.",
            "Semantic similarity search is very useful.",
        ],
    ),
    "lexical": (
        "Would exact keyword matching or full-text search help retrieve supporting memories for this question?",
        [
            "Keyword or full-text retrieval is unlikely to help.",
            "Keyword or full-text retrieval may help.",
            "Keyword or full-text retrieval is very useful.",
        ],
    ),
    "symbolic": (
        "Would filtering memories by explicit people, entities, locations, or dates help answer this question?",
        [
            "No clear person, entity, place, or date filter would help.",
            "A person, entity, place, or date filter may help.",
            "An explicit person, entity, place, or date strongly supports filtering.",
        ],
    ),
    "multi_hop": (
        "Does answering this question require combining evidence from multiple memories rather than relying on a single memory?",
        [
            "One memory will likely be enough.",
            "Two related memories will likely need to be combined.",
            "Several connected memories or reasoning steps will likely be required.",
        ],
    ),
}

JEV_QUESTIONS = {
    f"{name}_need": Score(instructions=instructions, criteria=criteria)
    for name, (instructions, criteria) in RUBRICS.items()
}


class JevController:
    """Drop-in controller for LayaRetriever(controller=JevController()).

    Usage and confidence are diagnostic; only the four normalized scores are
    passed to the existing LAYA budget strategies.
    """

    def __init__(self, model: str = "jev-1.13.0") -> None:
        self.model = model
        self._local = threading.local()

    @property
    def last_diagnostics(self) -> dict[str, Any]:
        """Diagnostics for the most recent predict() in the calling thread."""
        return dict(getattr(self._local, "diagnostics", {}))

    def predict(self, query: str) -> RetrievalSignals:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        if not os.getenv("TYPESAFE_API_KEY"):
            raise RuntimeError("TYPESAFE_API_KEY is missing from extension/jev/.env or the environment")

        self._local.diagnostics = {}
        start = perf_counter()
        with TypeSafeClient() as client:
            response = client.system_one(
                state={"question": query},
                questions=JEV_QUESTIONS,
                model=self.model,
            )
        latency_ms = (perf_counter() - start) * 1000

        values: dict[str, float] = {}
        per_signal: dict[str, dict[str, Any]] = {}
        for name, (_, rubric) in RUBRICS.items():
            key = f"{name}_need"
            answer = response.scores[key]
            raw = float(answer.score)
            max_score = len(rubric) - 1
            if not math.isfinite(raw) or not 0 <= raw <= max_score:
                raise ValueError(f"Unexpected Jev {key} score: {raw!r}")
            normalized = raw / max_score
            values[name] = normalized
            per_signal[name] = {
                "raw": raw,
                "normalized": normalized,
                "confidence": float(answer.confidence),
                "probabilities": dict(answer.probabilities),
            }

        usage = response.usage
        self._local.diagnostics = {
            "model": str(response.model),
            "jev_calls": 1,
            "input_tokens": int(usage.input_tokens),
            "output_tokens": int(usage.output_tokens),
            "latency_ms": round(latency_ms, 2),
            "scores": per_signal,
        }
        return RetrievalSignals(**values)
