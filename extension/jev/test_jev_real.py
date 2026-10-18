"""Step 3A: test Jev retrieval against the EXISTING SimpleMem LanceDB.

Copy this file into extension/jev/ and run from the repository root:
    python extension/jev/test_jev_real.py

Uses one Jev API request, local embeddings and the existing memory_entries table.
Does not call an answer LLM, clear/rebuild the DB, or insert any memories.
"""

from __future__ import annotations

import argparse
import json
import sys
import types
from importlib.machinery import ModuleSpec
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[2]
SIMPLEMEM_DIR = ROOT / "baseline" / "SimpleMem"
DATABASE_DIR = ROOT / "baseline" / "lancedb_data"

if not (SIMPLEMEM_DIR / "core" / "database" / "vector_store.py").is_file():
    raise SystemExit(f"SimpleMem VectorStore not found under {SIMPLEMEM_DIR}")
if not DATABASE_DIR.is_dir():
    raise SystemExit(f"Existing LanceDB not found: {DATABASE_DIR}")

# Source-tree compatibility: this checkout keeps core/ directly under
# baseline/SimpleMem, but its modules import it as simplemem.core.*.
# Expose that directory as the 'simplemem' package for THIS test process only.
# No source files, package installations or repository paths are changed.
package = types.ModuleType("simplemem")
package.__package__ = "simplemem"
package.__path__ = [str(SIMPLEMEM_DIR)]
package.__spec__ = ModuleSpec("simplemem", loader=None, is_package=True)
sys.modules["simplemem"] = package

# The budget strategies are shared with LAYA, not the LAYA decision model.
sys.path.insert(0, str(ROOT / "extension" / "laya"))
sys.path.insert(0, str(ROOT / "extension" / "jev"))

import lancedb  # noqa: E402
from simplemem.core.database.vector_store import VectorStore  # noqa: E402
from simplemem.core.utils.embedding import EmbeddingModel  # noqa: E402
from jev_controller import JevController  # noqa: E402
from jev_retriever import JevRetriever  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--question",
        default="What inspired Caroline's painting of a sunset?",
    )
    parser.add_argument(
        "--strategy",
        choices=["complexity", "retrieval", "combined"],
        default="complexity",
    )
    args = parser.parse_args()

    # Confirm we're querying the database already present on disk.
    db = lancedb.connect(str(DATABASE_DIR))
    tables = db.table_names()
    if "memory_entries" not in tables:
        raise SystemExit(f"memory_entries table not found; available: {tables}")
    table = db.open_table("memory_entries")
    before = table.count_rows()
    print(f"DATABASE: {DATABASE_DIR}")
    print(f"TABLE: memory_entries | ROWS BEFORE: {before}")
    print(f"QUESTION: {args.question}")
    print(f"STRATEGY: {args.strategy}")
    if before == 0:
        raise SystemExit("Database has no memories; aborting before API call")

    # Build the same embedding and VectorStore classes as SimpleMem.
    # Loading the local embedding model may take time on the first run.
    setup_start = perf_counter()
    embedding = EmbeddingModel()
    store = VectorStore(
        db_path=str(DATABASE_DIR),
        embedding_model=embedding,
        table_name="memory_entries",
    )
    setup_ms = (perf_counter() - setup_start) * 1000
    print(f"EMBEDDING MODEL: {embedding.model_name}")
    print(f"EMBEDDING DIMENSION: {embedding.dimension}")
    print(f"SETUP LATENCY: {setup_ms:.2f} ms (excluded from query latency)")

    controller = JevController()
    retriever = JevRetriever(
        llm_client=None,
        vector_store=store,
        controller=controller,
        strategy=args.strategy,
        strict_controller=True,  # Jev API failures must NOT silently fall back.
    )

    start = perf_counter()
    memories = retriever.retrieve(args.question)
    query_ms = (perf_counter() - start) * 1000

    print("\nJEV CONTROLLER DIAGNOSTICS:")
    print(json.dumps(controller.last_diagnostics, indent=2, default=str))
    print("\nRETRIEVAL DIAGNOSTICS:")
    diagnostics = retriever.last_diagnostics
    print(json.dumps(diagnostics, indent=2, default=str))
    print(f"\nRETRIEVAL LATENCY INCLUDING JEV: {query_ms:.2f} ms")
    print(f"RETRIEVED MEMORIES: {len(memories)}")
    print("\nRETRIEVED EVIDENCE:")
    for idx, memory in enumerate(memories, start=1):
        text = getattr(memory, "lossless_restatement", None)
        print(f"{idx}. [{getattr(memory, 'entry_id', 'no-id')}] {text}")

    # Only an indicative check for this default question, not an accuracy metric.
    if args.question == "What inspired Caroline's painting of a sunset?":
        found = any(
            all(word in (getattr(m, "lossless_restatement", "") or "").lower()
                for word in ("caroline", "sunset", "beach"))
            for m in memories
        )
        print(f"\nEXPECTED SUPPORTING MEMORY PRESENT: {found}")

    after = db.open_table("memory_entries").count_rows()
    print(f"DATABASE ROWS AFTER: {after}")
    if after != before:
        raise RuntimeError(f"Database row count changed: {before} -> {after}")
    if not memories:
        raise RuntimeError("No memories retrieved. Inspect retrieval diagnostics above.")
    if diagnostics.get("retrieval_errors"):
        raise RuntimeError("Retrieval errors were reported. See diagnostics above.")
    print("\nStep 3A retrieval test completed. Answer-generation LLM was NOT called.")


if __name__ == "__main__":
    main()
