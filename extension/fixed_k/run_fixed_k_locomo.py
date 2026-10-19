"""Evaluate SimpleMem on LoCoMo with a FIXED retrieval depth k, on existing memories.

Same setup as run_layamem_locomo.py (your teammate's Laya runner): the baseline LanceDB is
copied to a temp dir and never modified, memories are not rebuilt, and the evaluator is the
original test_locomo10.py (same question loop, answer generator, category-5 handling, metrics).
The only change is stage 3: intent-aware planning/reflection is replaced by FixedKRetriever.

Examples (run from the folder containing this file):
    python run_fixedk_locomo.py --k 3 5 10 15 20
    python run_fixedk_locomo.py --k 10 --llm-judge
    python run_fixedk_locomo.py --k 5 10 --max-total 0        # no cap on the union (up to 3*k)

One result file per k is written to --out-dir, plus fixedk_sweep_summary.json / .csv.
Cost note: every k re-answers every question (one answer-generation LLM call per question,
plus judge calls with --llm-judge), so cost scales linearly with the number of k values.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import random
import shutil
import sys
import tempfile
from pathlib import Path


def find_evaluator(root: Path) -> Path:
    for path in [root / 'test_locomo10.py', root / 'test_ref' / 'test_locomo10.py',
                 *sorted(root.rglob('test_locomo10.py'))]:
        if path.is_file():
            return path.resolve()
    raise FileNotFoundError('Could not find test_locomo10.py; supply --evaluator')


def load_evaluator(path: Path):
    spec = importlib.util.spec_from_file_location('fixedk_baseline_evaluator', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def summarize(evaluator, tester, results, k, max_total):
    """One flat row per k for the sweep table."""
    agg = evaluator.aggregate_metrics(tester.metrics_list, tester.categories) if tester.metrics_list else {}
    overall = agg.get('overall', {})
    row = {
        'k': k,
        'max_total': max_total,
        'num_questions': len(results),
        'avg_retrieved': (sum(r['num_retrieved'] for r in results) / len(results)) if results else 0.0,
        'avg_retrieval_time': (sum(tester.retrieval_times) / len(tester.retrieval_times)) if tester.retrieval_times else 0.0,
    }
    for name in ('f1', 'bleu1', 'rougeL_f', 'bert_f1', 'sbert_similarity', 'llm_judge_score'):
        if name in overall:
            row[name] = overall[name]['mean']
    for key in sorted(agg):
        if key.startswith('category_') and 'f1' in agg[key]:
            row[f'{key}_f1'] = agg[key]['f1']['mean']
            row[f'{key}_n'] = agg[key]['f1']['count']
    return row


def main():
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description='Fixed-k LoCoMo evaluation on existing baseline memories')
    parser.add_argument('--k', type=int, nargs='+', default=[3, 5, 10, 15, 20],
                        help='Fixed per-layer retrieval depth(s) to evaluate')
    parser.add_argument('--max-total', type=int, default=20,
                        help='Cap on the merged context size (default 20, as in LayaRetriever). 0 = no cap')
    parser.add_argument('--layers', nargs='+', choices=['semantic', 'lexical', 'symbolic'],
                        default=['semantic', 'lexical', 'symbolic'],
                        help='Retrieval layers to use (default: all three, as in LayaRetriever)')
    parser.add_argument('--baseline-root', type=Path, default=here.parent.parent / 'baseline')
    parser.add_argument('--evaluator', type=Path)
    parser.add_argument('--dataset', type=Path)
    parser.add_argument('--db-path', type=Path, help='Existing baseline LanceDB directory (lancedb_data)')
    parser.add_argument('--table-name', type=str, default=None,
                        help='Memory table name, only if it differs from the default')
    parser.add_argument('--num-samples', type=int, default=1)
    parser.add_argument('--out-dir', type=Path, default=here / 'fixedk_results')
    parser.add_argument('--llm-judge', action='store_true')
    parser.add_argument('--parallel-questions', action='store_true')
    parser.add_argument('--test-workers', type=int, default=None)
    parser.add_argument('--seed', type=int, default=0,
                        help='Seed for the category-5 option shuffling, reset before every k')
    args = parser.parse_args()

    if args.num_samples != 1:
        parser.error('Only sample 0 is supported with the existing single-sample database')
    if any(k < 1 for k in args.k):
        parser.error('All k values must be >= 1')
    k_values = list(dict.fromkeys(args.k))  # dedupe, keep order
    max_total = args.max_total if args.max_total > 0 else None

    root = args.baseline_root.resolve()
    if not (root / 'main.py').is_file() or not (root / 'simplemem').is_dir():
        parser.error(f'Invalid baseline root: {root}')
    db_source = (args.db_path or root / 'lancedb_data').resolve()
    if not db_source.is_dir() or not any(db_source.iterdir()):
        parser.error(f'Existing LanceDB not found or empty: {db_source}')
    dataset = (args.dataset or root / 'test_ref' / 'locomo10.json').resolve()
    if not dataset.is_file():
        parser.error(f'Dataset not found: {dataset}; specify --dataset')
    evaluator_path = args.evaluator.resolve() if args.evaluator else find_evaluator(root)
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    for directory in (str(root), str(evaluator_path.parent), str(here)):
        if directory not in sys.path:
            sys.path.insert(0, directory)
    os.chdir(root)

    from main import SimpleMemSystem
    from simplemem.core.database.vector_store import VectorStore
    from fixed_k_retriever import FixedKRetriever, MetadataExtractor
    evaluator = load_evaluator(evaluator_path)

    print(f'Baseline database (untouched): {db_source}')
    print(f'Dataset: {dataset}')
    print(f'k values: {k_values} | layers: {args.layers} | max_total: {max_total}')
    print('WARNING: Confirm the stored memories were built from LoCoMo sample 0.')

    summary_rows = []
    # Work on a temporary physical copy, never on the original database.
    with tempfile.TemporaryDirectory(prefix='fixedk_locomo_') as tmp:
        db_copy = Path(tmp) / 'lancedb_data'
        shutil.copytree(db_source, db_copy)

        # Construct components without clearing anything, then swap in the isolated store.
        system = SimpleMemSystem(clear_db=False)
        store_kwargs = {'db_path': str(db_copy), 'embedding_model': system.embedding_model}
        if args.table_name:
            store_kwargs['table_name'] = args.table_name
        system.vector_store = VectorStore(**store_kwargs)
        count = len(system.vector_store.get_all_entries())
        if count == 0:
            raise RuntimeError('Copied database has no memories; refusing to evaluate')
        print(f'Existing memories in temporary copy: {count}')

        # Never permit a write through the store exposed to the evaluator.
        def forbidden(*_a, **_kw):
            raise RuntimeError('Memory writes are disabled in this evaluator')
        system.vector_store.clear = forbidden
        system.vector_store.add_entries = forbidden
        system.vector_store.optimize = forbidden
        system.add_dialogues = forbidden
        system.finalize = forbidden

        # Build the (read-only) symbolic vocabulary once and share it across all k runs.
        extractor = MetadataExtractor(system.vector_store)

        class ExistingMemoryTester(evaluator.LoCoMoTester):
            def test_sample(self, sample, sample_idx, enable_parallel_questions=False):
                print(f'Using existing memories for sample {sample_idx}; no construction')
                if enable_parallel_questions and len(sample.qa) > 1:
                    return self._test_questions_parallel(sample.qa)
                return self._test_questions_sequential(sample.qa)

            def run_test(self, *a, **kw):
                # The original run_test calls vector_store.clear() before each sample;
                # neutralise just that call for the duration of the method.
                original_clear = self.system.vector_store.clear
                self.system.vector_store.clear = lambda: None
                try:
                    return super().run_test(*a, **kw)
                finally:
                    self.system.vector_store.clear = original_clear

        for k in k_values:
            print('\n' + '#' * 80)
            print(f'# Fixed k = {k}')
            print('#' * 80)
            random.seed(args.seed)  # same category-5 shuffling for every k

            system.hybrid_retriever = FixedKRetriever(
                vector_store=system.vector_store,
                k=k,
                max_total=max_total,
                use_semantic='semantic' in args.layers,
                use_lexical='lexical' in args.layers,
                use_symbolic='symbolic' in args.layers,
                metadata_extractor=extractor,
                llm_client=system.llm_client,
            )

            # Fresh tester per k so timing/metric statistics do not accumulate across runs.
            tester = ExistingMemoryTester(system, str(dataset), use_llm_judge=args.llm_judge,
                                          test_workers=args.test_workers)
            result_file = out_dir / f'fixedk_k{k}_results.json'
            results = tester.run_test(
                num_samples=1,
                save_results=True,
                result_file=str(result_file),
                enable_parallel_questions=args.parallel_questions,
            )
            summary_rows.append(summarize(evaluator, tester, results, k, max_total))

            # Persist the sweep summary after every k so partial sweeps are not lost.
            with open(out_dir / 'fixedk_sweep_summary.json', 'w') as f:
                json.dump(summary_rows, f, indent=2)

    # CSV (union of columns, since per-category columns can differ).
    columns = []
    for row in summary_rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    with open(out_dir / 'fixedk_sweep_summary.csv', 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(summary_rows)

    print('\n' + '=' * 80)
    print(' Fixed-k sweep summary'.center(80))
    print('=' * 80)
    show = ['f1', 'bleu1', 'bert_f1', 'llm_judge_score']
    header = f"{'k':>4} {'avg_ctx':>8} {'ret_time':>9} " + ' '.join(f'{m:>10}' for m in show)
    print(header)
    for row in summary_rows:
        cells = ' '.join(f"{row[m]:>10.4f}" if m in row else f"{'-':>10}" for m in show)
        print(f"{row['k']:>4} {row['avg_retrieved']:>8.1f} {row['avg_retrieval_time']:>9.3f} {cells}")
    print(f'\nResults in: {out_dir}')
    print(f'Original database was not modified: {db_source}')


if __name__ == '__main__':
    main()
