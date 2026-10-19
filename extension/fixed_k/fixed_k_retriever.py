"""Fixed-depth retriever for SimpleMem (LLM-free, no planning, no reflection).

Drop-in replacement for the `hybrid_retriever` of SimpleMemSystem. It exposes the same
`retrieve(query, enable_reflection=None)` interface used by test_locomo10.py, but replaces
stage 3 (intent-aware retrieval planning) with a constant retrieval depth `k`.

It deliberately mirrors the structure of laya_retriever.LayaRetriever so that the ONLY thing
that differs between the two experiments is how the budget is chosen:

  * Laya:   k = f(complexity score of the query), identical k for all layers
  * Here:   k = constant, identical for all layers

Layers (each returns up to k entries):
  semantic  -> vector_store.semantic_search(query, top_k=k)
  lexical   -> vector_store.keyword_search(keywords, top_k=k)
  symbolic  -> vector_store.structured_search(**verified_filters, top_k=k)
               (only when verified metadata is found in the query, as in LayaRetriever)

The union is built by rank-interleaving the layers (sem, lex, sym, sem, ...), deduplicating by
entry_id, and truncating to `max_total`. max_total=20 matches the LayaRetriever default; pass
max_total=None for no cap (up to 3*k entries).

No dependency on the `laya` package.
"""
from __future__ import annotations

import calendar
import re
import threading
from typing import Any, Callable, Dict, List, Mapping, Optional

_TOKEN_RE = re.compile(r"[\w]+(?:[-'][\w]+)*", re.UNICODE)
_STOPWORDS = frozenset(
    "a an the and or but of in on at to for from by with about as is are was were be been "
    "do does did done what when where who whom whose which why how that this these those "
    "it its he she they them his her their you your i me my we our not no yes if then than "
    "has have had will would can could should may might".split()
)
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTH_YEAR_RE = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{4})\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
_ALLOWED_FILTERS = frozenset({"persons", "timestamp_range", "location", "entities"})


class MetadataExtractor:
    """Deterministic, no-LLM extractor of symbolic filters VERIFIED against the store.

    Identical in behavior to the extractor used by LayaRetriever, so the symbolic layer is
    triggered on exactly the same queries in both experiments.
    """

    def __init__(self, vector_store: Any):
        self.vector_store = vector_store
        self._lock = threading.Lock()
        self._vocab: Optional[Dict[str, Dict[str, str]]] = None

    def refresh(self) -> None:
        with self._lock:
            self._vocab = None

    def _build(self) -> Dict[str, Dict[str, str]]:
        with self._lock:
            if self._vocab is None:
                vocab: Dict[str, Dict[str, str]] = {"persons": {}, "entities": {}, "location": {}}
                for e in self.vector_store.get_all_entries():
                    for p in (getattr(e, "persons", None) or []):
                        vocab["persons"][str(p).lower()] = str(p)
                    for x in (getattr(e, "entities", None) or []):
                        vocab["entities"][str(x).lower()] = str(x)
                    loc = getattr(e, "location", None)
                    if loc:
                        vocab["location"][str(loc).lower()] = str(loc)
                self._vocab = vocab
            return self._vocab

    @staticmethod
    def _in_query(term_lower: str, query_lower: str) -> bool:
        return re.search(r"(?<!\w)" + re.escape(term_lower) + r"(?!\w)", query_lower) is not None

    def __call__(self, query: str) -> Dict[str, Any]:
        vocab = self._build()
        q = query.lower()
        out: Dict[str, Any] = {}
        persons = [o for low, o in vocab["persons"].items() if len(low) > 1 and self._in_query(low, q)]
        entities = [o for low, o in vocab["entities"].items() if len(low) > 2 and self._in_query(low, q)]
        locations = [o for low, o in vocab["location"].items() if len(low) > 2 and self._in_query(low, q)]
        if persons:
            out["persons"] = persons
        if entities:
            out["entities"] = entities
        if locations:
            out["location"] = locations[0]

        m = _MONTH_YEAR_RE.search(query)
        if m:
            month, year = _MONTHS[m.group(1).lower()], int(m.group(2))
            last = calendar.monthrange(year, month)[1]
            out["timestamp_range"] = (f"{year:04d}-{month:02d}-01T00:00:00",
                                      f"{year:04d}-{month:02d}-{last:02d}T23:59:59")
        else:
            y = _YEAR_RE.search(query)
            if y:
                year = int(y.group(1))
                out["timestamp_range"] = (f"{year:04d}-01-01T00:00:00", f"{year:04d}-12-31T23:59:59")
        return out


class FixedKRetriever:
    """Semantic + lexical + symbolic retrieval with a constant per-layer depth k."""

    def __init__(
        self,
        vector_store: Any,
        k: int = 10,
        max_total: Optional[int] = 20,
        use_semantic: bool = True,
        use_lexical: bool = True,
        use_symbolic: bool = True,
        metadata_extractor: Optional[Callable[[str], Optional[Mapping[str, Any]]]] = None,
        llm_client: Any = None,
    ) -> None:
        if vector_store is None:
            raise ValueError("vector_store is required")
        if k < 1:
            raise ValueError("k must be >= 1")
        if max_total is not None and max_total < 1:
            raise ValueError("max_total must be >= 1 or None")
        if not (use_semantic or use_lexical or use_symbolic):
            raise ValueError("at least one retrieval layer must be enabled")
        self.llm_client = llm_client  # unused: retrieval makes no LLM call
        self.vector_store = vector_store
        self.k = k
        self.max_total = max_total
        self.use_semantic = use_semantic
        self.use_lexical = use_lexical
        self.use_symbolic = use_symbolic
        self.metadata_extractor = (
            metadata_extractor if metadata_extractor is not None else MetadataExtractor(vector_store)
        )
        self._local = threading.local()

    @property
    def last_diagnostics(self) -> Dict[str, Any]:
        return dict(getattr(self._local, "diagnostics", {}))

    @staticmethod
    def _keywords(query: str) -> List[str]:
        toks = _TOKEN_RE.findall(query)
        kept = [t for t in toks if t.lower() not in _STOPWORDS]
        return kept or toks

    def retrieve(self, query: str, enable_reflection: Optional[bool] = None) -> List[Any]:
        """`enable_reflection` is accepted for interface compatibility; there is no reflection."""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        diag: Dict[str, Any] = {"k": self.k, "filters": None, "retrieval_errors": {}, "retrieval_counts": {}}
        self._local.diagnostics = diag
        k = self.k

        filters: Dict[str, Any] = {}
        if self.use_symbolic:
            try:
                filters = dict(self.metadata_extractor(query) or {})
                bad = set(filters) - _ALLOWED_FILTERS
                if bad:
                    raise ValueError(f"Unsupported structured filters: {sorted(bad)}")
                filters = {key: val for key, val in filters.items() if val not in (None, [], "")}
                diag["filters"] = {key: (list(val) if isinstance(val, tuple) else val)
                                   for key, val in filters.items()}
            except Exception as exc:
                filters = {}
                diag["retrieval_errors"]["symbolic_extract"] = f"{type(exc).__name__}: {exc}"

        searches = []
        if self.use_semantic:
            searches.append(("semantic", lambda: self.vector_store.semantic_search(query, top_k=k)))
        if self.use_lexical:
            searches.append(("lexical", lambda: self.vector_store.keyword_search(self._keywords(query), top_k=k)))
        if filters:
            searches.append(("symbolic", lambda: self.vector_store.structured_search(**filters, top_k=k)))

        layer_results: List[List[Any]] = []
        for name, fn in searches:
            try:
                entries = fn()
                if entries is None:
                    raise RuntimeError(f"{name} search returned None")
                entries = list(entries)
                diag["retrieval_counts"][name] = len(entries)
                layer_results.append(entries)
            except Exception as exc:
                diag["retrieval_errors"][name] = f"{type(exc).__name__}: {exc}"
        if not layer_results:
            raise RuntimeError(f"All retrieval methods failed: {diag['retrieval_errors']}")

        # Union by rank-interleaving so a cap keeps a fair share of every layer.
        unique, seen = [], set()
        for rank in range(max(len(r) for r in layer_results)):
            for layer in layer_results:
                if rank < len(layer):
                    e = layer[rank]
                    eid = getattr(e, "entry_id", None)
                    if eid is None:
                        raise TypeError("VectorStore returned an entry without entry_id")
                    if eid not in seen:
                        seen.add(eid)
                        unique.append(e)
        diag["num_candidates"] = sum(diag["retrieval_counts"].values())
        diag["num_before_cap"] = len(unique)
        if self.max_total is not None:
            unique = unique[: self.max_total]
        diag["num_unique"] = len(unique)
        return unique
