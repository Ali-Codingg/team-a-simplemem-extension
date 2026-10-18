"""Step 2: real Jev controller + equivalent retrieval, using test-only in-memory facts.

Run at the repository root:
    python extension/jev/test_jev_integration.py

This makes one paid Jev API request but DOES NOT open LanceDB or call an LLM.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "extension" / "laya"))
sys.path.insert(0, str(ROOT / "baseline" / "SimpleMem"))

from jev_controller import JevController  # noqa: E402
from jev_retriever import JevRetriever  # noqa: E402


@dataclass
class ExampleMemory:
    entry_id: str
    text: str
    persons: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    location: str | None = None


class InMemoryVectorStore:
    """Implements the four methods LayaRetriever uses; no database writes."""

    def __init__(self) -> None:
        self.memories = [
            ExampleMemory("demo-1", "Alice worked at Acme before moving to Boston.", ["Alice"], ["Acme"], "Boston"),
            ExampleMemory("demo-2", "Alice left Acme in June 2022.", ["Alice"], ["Acme"]),
            ExampleMemory("demo-3", "Bob moved to Boston in 2023.", ["Bob"], [], "Boston"),
        ]

    def get_all_entries(self):
        return list(self.memories)

    def semantic_search(self, query, top_k=10):
        return self.memories[:top_k]

    def keyword_search(self, keywords, top_k=10):
        return self.memories[:top_k]

    def structured_search(self, top_k=10, **filters):
        candidates = self.memories
        persons = filters.get("persons") or []
        if persons:
            candidates = [m for m in candidates if set(persons) & set(m.persons)]
        entities = filters.get("entities") or []
        if entities:
            candidates = [m for m in candidates if set(entities) & set(m.entities)]
        if filters.get("location"):
            candidates = [m for m in candidates if m.location == filters["location"]]
        return candidates[:top_k]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", choices=["complexity", "retrieval", "combined"], default="complexity")
    parser.add_argument(
        "--question",
        default="Where did Alice work before moving to Boston, and when did she leave that job?",
    )
    args = parser.parse_args()

    controller = JevController()
    retriever = JevRetriever(
        llm_client=None,
        vector_store=InMemoryVectorStore(),
        controller=controller,
        strategy=args.strategy,
        strict_controller=True,  # fail instead of silently falling back to fixed k
    )
    results = retriever.retrieve(args.question)

    print("\nQUESTION:", args.question)
    print("\nJEV DETAILS:")
    print(json.dumps(controller.last_diagnostics, indent=2))
    print("\nJEV RETRIEVER DIAGNOSTICS:")
    print(json.dumps(retriever.last_diagnostics, indent=2))
    print("\nRETRIEVED TEST MEMORIES:")
    for entry in results:
        print(f"  {entry.entry_id}: {entry.text}")
    print("\nStep 2 wiring test complete; this test used synthetic memories, not LanceDB.")


if __name__ == "__main__":
    main()
