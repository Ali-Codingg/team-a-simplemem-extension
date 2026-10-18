"""Evaluate LayaMem on the existing baseline memories, without rebuilding them.

Run from extension\\laya (Windows CMD):
    python run_layamem_locomo.py --num-samples 1

The baseline LanceDB is copied into a temporary directory and never modified.
Only sample 0 is supported: the existing database was built for one sample.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path


def find_evaluator(root: Path) -> Path:
    for path in [root / 'test_locomo10.py', root / 'test_ref' / 'test_locomo10.py', *sorted(root.rglob('test_locomo10.py'))]:
        if path.is_file():
            return path.resolve()
    raise FileNotFoundError('Could not find test_locomo10.py; supply --evaluator')


def load_evaluator(path: Path):
    spec = importlib.util.spec_from_file_location('layamem_baseline_evaluator', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main():
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description='Evaluate LayaMem using existing baseline memories only')
    parser.add_argument('--baseline-root', type=Path, default=here.parent.parent / 'baseline')
    parser.add_argument('--evaluator', type=Path)
    parser.add_argument('--dataset', type=Path)
    parser.add_argument('--db-path', type=Path, help='Existing baseline LanceDB directory')
    parser.add_argument('--num-samples', type=int, default=1)
    parser.add_argument('--result-file', type=Path, default=here / 'layamem_locomo_results.json')
    parser.add_argument('--llm-judge', action='store_true')
    parser.add_argument('--strategy', choices=['complexity', 'retrieval', 'combined'], default='complexity')
    parser.add_argument('--semantic-top-k', type=int, default=5)
    parser.add_argument('--keyword-top-k', type=int, default=5)
    parser.add_argument('--structured-top-k', type=int, default=5)
    args = parser.parse_args()
    if args.result_file == here / 'layamem_locomo_results.json':
        args.result_file = here / f'layamem_{args.strategy}_results.json'
    if args.num_samples != 1:
        parser.error('Only sample 0 is supported with the existing single-sample database')

    root = args.baseline_root.resolve()
    if not (root / 'main.py').is_file() or not (root / 'simplemem').is_dir():
        parser.error(f'Invalid baseline root: {root}')
    db_source = (args.db_path or root / 'lancedb_data').resolve()
    if not db_source.is_dir() or not any(db_source.iterdir()):
        parser.error(f'Existing LanceDB not found or empty: {db_source}')
    dataset = (args.dataset or root / 'test_ref' / 'locomo10.json').resolve()
    if not dataset.is_file():
        parser.error(f'Dataset not found: {dataset}; specify --dataset')
    evaluator_path = (args.evaluator.resolve() if args.evaluator else find_evaluator(root))
    for directory in (str(root), str(evaluator_path.parent), str(here)):
        if directory not in sys.path:
            sys.path.insert(0, directory)
    os.chdir(root)

    from main import SimpleMemSystem
    from laya_retriever import LayaRetriever
    from simplemem.core.database.vector_store import VectorStore
    evaluator = load_evaluator(evaluator_path)

    print(f'Baseline database (untouched): {db_source}')
    print(f'Dataset: {dataset}')
    print(f'Strategy: {args.strategy}')
    print('WARNING: Confirm the 344 stored memories were built from LoCoMo sample 0.')

    # Work on a temporary physical copy, never on the original database.
    with tempfile.TemporaryDirectory(prefix='layamem_locomo_') as tmp:
        db_copy = Path(tmp) / 'lancedb_data'
        shutil.copytree(db_source, db_copy)
        # Construct system components without clearing the default database.
        system = SimpleMemSystem(clear_db=False)
        # Explicitly replace its store with the isolated copy, independent of
        # config.py or environment variable precedence.
        system.vector_store = VectorStore(db_path=str(db_copy))
        count = len(system.vector_store.get_all_entries())
        if count == 0:
            raise RuntimeError('Copied database has no memories; refusing to evaluate')
        print(f'Existing memories in temporary copy: {count}')

        # Never permit a write through the store exposed to the evaluator.
        def forbidden(*_args, **_kwargs):
            raise RuntimeError('Memory writes are disabled in the LayaMem evaluator')
        system.vector_store.clear = forbidden
        system.vector_store.add_entries = forbidden
        system.vector_store.optimize = forbidden
        system.add_dialogues = forbidden
        system.finalize = forbidden

        system.hybrid_retriever = LayaRetriever(
            llm_client=system.llm_client,
            vector_store=system.vector_store,
        )

        class ExistingMemoryTester(evaluator.LoCoMoTester):
            def test_sample(self, sample, sample_idx, enable_parallel_questions=False):
                print(f'Using existing memories for sample {sample_idx}; no construction')
                return self._test_questions_sequential(sample.qa)

            def run_test(self, *a, **kw):
                # The original run_test calls vector_store.clear() before each
                # sample. Override only this operation during that method.
                original_clear = self.system.vector_store.clear
                self.system.vector_store.clear = lambda: None
                try:
                    return super().run_test(*a, **kw)
                finally:
                    self.system.vector_store.clear = original_clear

        tester = ExistingMemoryTester(system, str(dataset), use_llm_judge=args.llm_judge)
        output = args.result_file.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        tester.run_test(num_samples=1, save_results=True, result_file=str(output), enable_parallel_questions=False)
        print(f'Results saved to {output}')
        print(f'Original database was not modified: {db_source}')


if __name__ == '__main__':
    main()
