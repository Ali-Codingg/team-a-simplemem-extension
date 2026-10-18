"""Replace a legacy Tantivy FTS index in a SimpleMem LanceDB folder with a native LanceDB FTS index.

Why this exists: newer LanceDB versions removed Tantivy-based full-text search. A database built with
`create_fts_index(..., use_tantivy=True)` contains `<table>.lance/_indices/fts`, and every keyword
search then fails with "Legacy Tantivy FTS index detected ...". SimpleMem's VectorStore catches that
error and returns [] -- so the lexical layer silently contributes nothing until the index is rebuilt.

Only the *index* is rebuilt (derived data). Memory rows are never touched.

Usage:
    # Safe: write a repaired COPY, original untouched
    python fix_fts_index.py --db-path C:\\path\\to\\lancedb_data --output C:\\path\\to\\lancedb_data_fixed

    # Repair the folder itself
    python fix_fts_index.py --db-path C:\\path\\to\\lancedb_data --in-place

run_fixedk_locomo.py already calls rebuild_fts_index() on its temporary copy, so you only need this
script if you want a permanently repaired database.
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import List, Optional

TEXT_COLUMN = "lossless_restatement"


def _find_tables(db_dir: Path, table_name: Optional[str]) -> List[str]:
    if table_name:
        return [table_name]
    return sorted(p.name[: -len(".lance")] for p in db_dir.glob("*.lance") if p.is_dir())


def rebuild_fts_index(db_path, table_name: Optional[str] = None, column: str = TEXT_COLUMN,
                      verbose: bool = True) -> bool:
    """Delete the legacy Tantivy index (if any) and create a native FTS index.

    Returns True if a legacy index was found and replaced, False if none was present.
    """
    import lancedb

    db_dir = Path(db_path)
    if not db_dir.is_dir():
        raise FileNotFoundError(f"LanceDB folder not found: {db_dir}")

    tables = _find_tables(db_dir, table_name)
    if not tables:
        raise RuntimeError(f"No *.lance tables found in {db_dir}")

    replaced_any = False
    for name in tables:
        legacy = db_dir / f"{name}.lance" / "_indices" / "fts"
        had_legacy = legacy.exists()
        if had_legacy:
            shutil.rmtree(legacy)
            replaced_any = True
            if verbose:
                print(f"[fts] removed legacy Tantivy index: {legacy}")
        else:
            if verbose:
                print(f"[fts] no legacy Tantivy index for table '{name}'")
            # Nothing legacy to replace; leave any existing native index alone.
            continue

        db = lancedb.connect(str(db_dir))
        table = db.open_table(name)

        # Closest native equivalent of Tantivy's "en_stem": simple tokenizer + lowercase + English stemmer.
        # Option names differ slightly between LanceDB versions, so fall back to simpler calls if needed.
        attempts = [
            dict(use_tantivy=False, replace=True, base_tokenizer="simple", language="English",
                 stem=True, lower_case=True, remove_stop_words=False),
            dict(use_tantivy=False, replace=True, stem=True, lower_case=True),
            dict(use_tantivy=False, replace=True),
            dict(replace=True),
        ]
        last_error = None
        for kwargs in attempts:
            try:
                table.create_fts_index(column, **kwargs)
                if verbose:
                    print(f"[fts] created native FTS index on '{name}.{column}' with {kwargs}")
                last_error = None
                break
            except Exception as exc:  # try the next, simpler signature
                last_error = exc
        if last_error is not None:
            raise RuntimeError(f"Could not create FTS index on '{name}': {last_error}") from last_error

        # Verify with a real query built from stored text.
        rows = table.to_arrow().slice(0, 1).to_pylist()
        if rows:
            words = [w for w in str(rows[0][column]).split() if w.isalpha()]
            probe = words[0] if words else "test"
            hits = table.search(probe).limit(3).to_list()
            if verbose:
                print(f"[fts] sanity query '{probe}' returned {len(hits)} row(s)")
    return replaced_any


def main():
    parser = argparse.ArgumentParser(description="Replace a legacy Tantivy FTS index with a native one")
    parser.add_argument("--db-path", type=Path, required=True, help="Path to the lancedb_data folder")
    parser.add_argument("--table-name", type=str, default=None,
                        help="Table to fix (default: every *.lance table in the folder)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--output", type=Path, help="Copy the database here and repair the copy")
    group.add_argument("--in-place", action="store_true", help="Repair the folder itself")
    args = parser.parse_args()

    source = args.db_path.resolve()
    if not source.is_dir():
        parser.error(f"Not a folder: {source}")

    if args.output:
        target = args.output.resolve()
        if target.exists():
            parser.error(f"Output already exists: {target}")
        shutil.copytree(source, target)
        print(f"Copied {source} -> {target}")
    else:
        target = source

    replaced = rebuild_fts_index(target, args.table_name)
    print("Done." if replaced else "Nothing to repair.")
    print(f"Database: {target}")


if __name__ == "__main__":
    main()
