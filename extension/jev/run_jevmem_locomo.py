"""Evaluate JevMem on LoCoMo sample 0 using EXISTING SimpleMem memories.

Run from the repository root (PowerShell):
    python extension/jev/run_jevmem_locomo.py --preflight-only
    python extension/jev/run_jevmem_locomo.py --max-questions 2
    python extension/jev/run_jevmem_locomo.py

This adapts extension/laya/run_layamem_locomo.py. It reuses the SAME
LoCoMoTester, answer-generation pipeline, retrieval budget strategies,
and stored memories. Only the retrieval controller/retriever is swapped.

The 336-memory baseline DB is never changed: evaluation uses a temporary
physical copy, automatically removed at the end. Jev and the answer LLM
are paid services; --preflight-only makes NO model API calls.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import statistics
import sys
import tempfile
import types
from importlib.machinery import ModuleSpec
from pathlib import Path
from time import perf_counter
from typing import Any

from dotenv import load_dotenv


def find_evaluator(app_root: Path, project: Path) -> Path:
    candidates = [
        app_root / "test_locomo10.py",
        app_root / "test_ref" / "test_locomo10.py",
        project / "baseline" / "test_ref" / "test_locomo10.py",
        *sorted(app_root.rglob("test_locomo10.py")),
    ]
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise FileNotFoundError("Cannot find test_locomo10.py; pass --evaluator PATH")


def load_evaluator(path: Path):
    spec = importlib.util.spec_from_file_location("jevmem_baseline_evaluator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load evaluator: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_sample_zero(dataset: Path) -> tuple[dict, int]:
    with dataset.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("data", data.get("samples"))
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        raise ValueError("Dataset must contain a list of LoCoMo samples")
    sample = data[0]
    if sample.get("sample_id") != "conv-26":
        raise ValueError(
            f"Sample 0 ID is {sample.get('sample_id')!r}; expected 'conv-26'. "
            "Do not evaluate questions against the wrong database."
        )
    qa = sample.get("qa")
    if not isinstance(qa, list) or not qa:
        raise ValueError("LoCoMo sample 0 has no QA list")
    return sample, len(qa)


def install_source_tree_alias(package_root: Path) -> None:
    """Supply a process-local simplemem package alias only if it cannot be imported."""
    if (package_root / "core").is_dir() and importlib.util.find_spec("simplemem") is None:
        if "simplemem" not in sys.modules:
            package = types.ModuleType("simplemem")
            package.__package__ = "simplemem"
            package.__path__ = [str(package_root)]
            package.__spec__ = ModuleSpec("simplemem", loader=None, is_package=True)
            sys.modules["simplemem"] = package


def write_diagnostics(path: Path, records: list[dict[str, Any]], strategy: str) -> None:
    latencies = [r["retrieval_ms"] for r in records if "retrieval_ms" in r]
    calls = sum(int(r.get("jev", {}).get("jev_calls", 0)) for r in records)
    input_tokens = sum(int(r.get("jev", {}).get("input_tokens", 0)) for r in records)
    output_tokens = sum(int(r.get("jev", {}).get("output_tokens", 0)) for r in records)
    payload = {
        "sample_id": "conv-26",
        "strategy": strategy,
        "questions_observed": len(records),
        "retrieval_latency_mean_ms": round(statistics.mean(latencies), 2) if latencies else None,
        "jev_logical_calls": calls,
        "jev_input_tokens": input_tokens,
        "jev_output_tokens": output_tokens,
        "notes": "Jev SDK usage only; LLM answer/judge tokens are not included.",
        "queries": records,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
    print(f"Jev retrieval diagnostics: {path}")


def main() -> None:
    here = Path(__file__).resolve().parent  # extension/jev
    project = here.parents[1]               # repository root
    # Import both Jev and answering-LLM credentials before SimpleMem config is loaded.
    load_dotenv(here / ".env", override=False)

    parser = argparse.ArgumentParser(description="Evaluate JevMem on LoCoMo sample conv-26")
    parser.add_argument("--baseline-root", type=Path, default=project / "baseline",
                        help="Folder containing main.py (normally baseline/)")
    parser.add_argument("--db-path", type=Path, default=project / "baseline" / "lancedb_data")
    parser.add_argument("--dataset", type=Path, default=project / "data" / "locomo" / "locomo10.json")
    parser.add_argument("--evaluator", type=Path)
    parser.add_argument("--num-samples", type=int, default=1)
    parser.add_argument("--max-questions", type=int, default=None,
                        help="Pilot with this many questions; omit to run all 199")
    parser.add_argument("--strategy", choices=("complexity", "retrieval", "combined"),
                        default="complexity")
    parser.add_argument("--llm-judge", action="store_true", help="Optional; adds LLM-judge API cost")
    parser.add_argument("--result-file", type=Path)
    parser.add_argument("--preflight-only", action="store_true",
                        help="Check dataset/database only; no Jev, LLM, embeddings or copying")
    args = parser.parse_args()

    if args.num_samples != 1:
        parser.error("Only LoCoMo sample 0 (conv-26) is supported by these existing memories")
    if args.max_questions is not None and args.max_questions < 1:
        parser.error("--max-questions must be positive")

    # --result-file is resolved against the repository root, independent of the
    # later os.chdir(app_root) used for compatibility with the baseline evaluator.
    if args.result_file is None:
        result_file = here / f"jevmem_{args.strategy}_results.json"
    elif args.result_file.is_absolute():
        result_file = args.result_file
    else:
        result_file = project / args.result_file
    result_file = result_file.resolve()

    answer_key = (os.getenv("ANSWER_LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
    answer_model = (os.getenv("ANSWER_LLM_MODEL") or os.getenv("LLM_MODEL") or "").strip() or None
    answer_base_url = (os.getenv("ANSWER_LLM_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "").strip() or None

    baseline_root = args.baseline_root.resolve()
    db_source = args.db_path.resolve()
    dataset = args.dataset.resolve()

    # This checkout has baseline/main.py and baseline/SimpleMem/core/.
    # Also accept checkouts with main.py directly under baseline/SimpleMem/.
    app_candidates = [baseline_root, baseline_root / "SimpleMem"]
    app_root = next((p for p in app_candidates if (p / "main.py").is_file()), None)
    package_root = next(
        (p for p in app_candidates if (p / "core" / "database" / "vector_store.py").is_file()),
        None,
    )
    if app_root is None:
        parser.error(f"SimpleMem main.py not found under {baseline_root} or its SimpleMem subfolder")
    if package_root is None:
        parser.error(f"SimpleMem core/database/vector_store.py not found under {baseline_root}")
    if not db_source.is_dir():
        parser.error(f"Existing LanceDB not found: {db_source}")
    if not dataset.is_file():
        parser.error(f"LoCoMo dataset not found: {dataset}")

    sample, num_questions = load_sample_zero(dataset)
    if num_questions != 199:
        print(f"WARNING: Expected 199 questions for conv-26, found {num_questions}.")
    pilot_count = min(args.max_questions or num_questions, num_questions)

    import lancedb
    db = lancedb.connect(str(db_source))
    if "memory_entries" not in db.table_names():
        raise RuntimeError("Source database lacks the memory_entries table")
    tbl = db.open_table("memory_entries")
    count_before = tbl.count_rows()
    indexes = tbl.list_indices()
    print(f"Application root: {app_root}")
    print(f"SimpleMem package: {package_root}")
    print(f"Source database (untouched): {db_source}")
    print(f"Memories: {count_before} | Indexes: {indexes}")
    print(f"Dataset: {dataset}")
    print(f"Sample: {sample['sample_id']} | Total questions: {num_questions}")
    print(f"Questions for this run: {pilot_count} | Strategy: {args.strategy}")
    if count_before != 336:
        raise RuntimeError(f"Expected 336 memories for conv-26; found {count_before}. Stopping.")
    if not any("FTS" in str(idx) and "lossless_restatement" in str(idx) for idx in indexes):
        raise RuntimeError("Missing BM25 index on lossless_restatement; stopping.")
    if args.preflight_only:
        print(f"Answer LLM key configured in environment: {bool(answer_key)} (value not shown)")
        print(f"Answer LLM model override: {answer_model or 'baseline default'}")
        print(f"Answer LLM endpoint override: {answer_base_url or 'baseline default'}")
        print("Preflight passed. No API requests or database modifications made.")
        return

    if not answer_key or answer_key.lower() in {"none", "your_api_key", "your_actual_api_key_here"}:
        parser.error(
            "Missing answer-generation LLM key. Put ANSWER_LLM_API_KEY=... (or OPENAI_API_KEY=...) "
            "in extension/jev/.env. TYPESAFE_API_KEY is only for Jev and cannot answer questions."
        )

    eval_path = (args.evaluator.resolve() if args.evaluator else find_evaluator(app_root, project))
    for directory in (str(app_root), str(package_root.parent), str(package_root), str(eval_path.parent),
                      str(project / "extension" / "laya"), str(here)):
        if directory not in sys.path:
            sys.path.insert(0, directory)
    install_source_tree_alias(package_root)
    os.chdir(app_root)  # preserve the teammate runner's working-directory behavior

    try:
        from main import SimpleMemSystem
    except ImportError as exc:
        raise ImportError(
            "Could not import SimpleMemSystem from baseline/main.py. "
            "Please share main.py's first 40 lines if this checkout uses different imports."
        ) from exc
    from jev_controller import JevController
    from jev_retriever import JevRetriever
    evaluator = load_evaluator(eval_path)
    if not hasattr(evaluator, "LoCoMoTester"):
        raise RuntimeError(f"Evaluator has no LoCoMoTester class: {eval_path}")

    diag_file = result_file.with_name(result_file.stem + "_retrieval_diagnostics.json")
    records: list[dict[str, Any]] = []

    # Temporary copy means the actual source database can NEVER be modified
    # even if the existing evaluator performs a maintenance write.
    with tempfile.TemporaryDirectory(prefix="jevmem_locomo_") as temp:
        db_copy = Path(temp) / "lancedb_data"
        shutil.copytree(db_source, db_copy)
        copy_table = lancedb.connect(str(db_copy)).open_table("memory_entries")
        if copy_table.count_rows() != count_before:
            raise RuntimeError("Copied LanceDB row count differs from source")

        # Passing db_path up front avoids initializing the default database
        # and avoids constructing a second embedding/vector-store instance.
        system = SimpleMemSystem(
            api_key=answer_key,
            model=answer_model,
            base_url=answer_base_url,
            db_path=str(db_copy),
            table_name="memory_entries",
            clear_db=False,
        )
        print(f"Answer LLM model: {system.llm_client.model}")
        print(f"Answer LLM endpoint: {system.llm_client.base_url or 'default OpenAI'}")
        if len(system.vector_store.get_all_entries()) != count_before:
            raise RuntimeError("SimpleMem loaded an unexpected number of memories")

        def forbidden(*_args: Any, **_kwargs: Any):
            raise RuntimeError("Memory writes are disabled for this evaluation")

        system.vector_store.clear = forbidden
        system.vector_store.add_entries = forbidden
        system.vector_store.optimize = forbidden
        system.add_dialogues = forbidden
        system.finalize = forbidden

        class RecordingJevRetriever(JevRetriever):
            def retrieve(self, query: str, enable_reflection=None):
                started = perf_counter()
                entry: dict[str, Any] = {"question": query}
                try:
                    result = super().retrieve(query, enable_reflection=enable_reflection)
                    entry["retrieved_count"] = len(result)
                    return result
                except Exception as error:
                    entry["error"] = f"{type(error).__name__}: {error}"
                    raise
                finally:
                    entry["retrieval_ms"] = round((perf_counter() - started) * 1000, 2)
                    entry["retriever"] = self.last_diagnostics
                    entry["jev"] = self.controller.last_diagnostics
                    records.append(entry)

        system.hybrid_retriever = RecordingJevRetriever(
            llm_client=system.llm_client,
            vector_store=system.vector_store,
            controller=JevController(),
            strategy=args.strategy,   # IMPORTANT: activate the CLI-selected strategy
            strict_controller=True,   # Never hide API failures using a fallback
        )

        class ExistingMemoryTester(evaluator.LoCoMoTester):
            def test_sample(self, sample, sample_idx, enable_parallel_questions=False):
                if sample_idx != 0:
                    raise RuntimeError("Only sample 0 was indexed in this DB")
                questions = sample.qa[:pilot_count]
                print(f"Evaluating {len(questions)} existing-memory questions from sample {sample_idx}")
                return self._test_questions_sequential(questions)

            def run_test(self, *a, **kw):
                # Original runner uses clear before the sample: skip that step
                # since this evaluation must reuse the existing 336 memories.
                original_clear = self.system.vector_store.clear
                self.system.vector_store.clear = lambda: None
                try:
                    return super().run_test(*a, **kw)
                finally:
                    self.system.vector_store.clear = original_clear

        tester = ExistingMemoryTester(system, str(dataset), use_llm_judge=args.llm_judge)
        result_file.parent.mkdir(parents=True, exist_ok=True)
        print(f"Answer-generation LLM: same SimpleMem configuration as the baseline")
        print("Using temporary database copy; original 336 memories are protected.")
        try:
            tester.run_test(
                num_samples=1,
                save_results=True,
                result_file=str(result_file),
                enable_parallel_questions=False,
            )
        finally:
            write_diagnostics(diag_file, records, args.strategy)

    count_after = lancedb.connect(str(db_source)).open_table("memory_entries").count_rows()
    if count_after != count_before:
        raise RuntimeError(f"Source memory count changed: {count_before} -> {count_after}")
    print(f"Evaluator results: {result_file}")
    print(f"Original database unchanged: {count_before} memories")


if __name__ == "__main__":
    main()
