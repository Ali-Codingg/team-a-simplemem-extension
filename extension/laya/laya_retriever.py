"""LLM-free ADAPTIVE retrieval for the LayaMem extension of SimpleMem.

Laya's four signals are combined into ONE complexity score C(q) that picks a single k for all layers,
and the symbolic layer runs on *verified* metadata found in the query.
No LLM call is made during retrieval.

Laya `noul` scores are NOT calibrated probabilities. The thresholds below are
starting values: check the logged score distribution and tune them (see
`laya_signal_report` in the notes) before trusting any result.
"""
from __future__ import annotations

import calendar
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

from laya_controller import LayaController, RetrievalSignals
from simplemem.core.utils.tracing import stage

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


from strategies.common import Budget, complexity_score
from strategies.complexity_only import choose_budget as choose_adaptive_k
from strategies.retrieval_only import choose_budget as choose_retrieval_budget
from strategies.combined import choose_budget as choose_combined_budget

K_MIN, K_MAX = 3, 20
MAX_TOTAL = 20
DEFAULT_WEIGHTS: Dict[str, float] = {"semantic": 0.2, "lexical": 0.2, "symbolic": 0.2, "multi_hop": 0.4}


class MetadataExtractor:
    """Deterministic, no-LLM extractor of symbolic filters that are VERIFIED against the store.

    - persons / entities / location: only strings that exist in stored memories AND appear in the query.
    - timestamp_range: explicit "Month YYYY" or "YYYY" in the query.
    Anything not verified is dropped, so a query word is never mistaken for stored metadata.
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
        persons = [orig for low, orig in vocab["persons"].items() if len(low) > 1 and self._in_query(low, q)]
        entities = [orig for low, orig in vocab["entities"].items() if len(low) > 2 and self._in_query(low, q)]
        locations = [orig for low, orig in vocab["location"].items() if len(low) > 2 and self._in_query(low, q)]
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
            out["timestamp_range"] = (f"{year:04d}-{month:02d}-01T00:00:00", f"{year:04d}-{month:02d}-{last:02d}T23:59:59")
        else:
            y = _YEAR_RE.search(query)
            if y:
                year = int(y.group(1))
                out["timestamp_range"] = (f"{year:04d}-01-01T00:00:00", f"{year:04d}-12-31T23:59:59")
        return out


_ALLOWED_FILTERS = frozenset({"persons", "timestamp_range", "location", "entities"})


class LayaRetriever:
    """Drop-in SimpleMem retriever: Laya picks the budget, three layers retrieve, no LLM call."""

    def __init__(
        self,
        llm_client: Any,
        vector_store: Any,
        controller: Optional[LayaController] = None,
        k_min: int = K_MIN,
        k_max: int = K_MAX,
        max_total: int = MAX_TOTAL,
        weights: Mapping[str, float] = DEFAULT_WEIGHTS,
        default_k: int = 10,
        metadata_extractor: Optional[Callable[[str], Optional[Mapping[str, Any]]]] = None,
        use_symbolic: bool = True,
        symbolic_min_signal: float = 0.0,
        strict_controller: bool = False,
        strategy: str = "complexity",
    ) -> None:
        if vector_store is None:
            raise ValueError("vector_store is required")
        if not (1 <= k_min <= k_max):
            raise ValueError("need 1 <= k_min <= k_max")
        if max_total < 1:
            raise ValueError("max_total must be >= 1")
        self.llm_client = llm_client  # unused: retrieval makes no LLM call
        self.vector_store = vector_store
        self.controller = controller if controller is not None else LayaController()
        self.k_min, self.k_max, self.max_total = k_min, k_max, max_total
        self.default_k = max(k_min, min(k_max, default_k))
        self.weights = dict(weights)
        self.use_symbolic = use_symbolic
        self.symbolic_min_signal = symbolic_min_signal
        self.metadata_extractor = metadata_extractor if metadata_extractor is not None else MetadataExtractor(vector_store)
        self.strict_controller = strict_controller
        if strategy not in ('complexity', 'retrieval', 'combined'):
            raise ValueError(f'Unknown strategy: {strategy}')
        self.strategy = strategy
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
        """enable_reflection is accepted for compatibility; there is no reflection."""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        diag: Dict[str, Any] = {"signals": None, "controller_error": None, "budget": None,
                                "filters": None, "retrieval_errors": {}, "retrieval_counts": {}}
        self._local.diagnostics = diag

        # 1) Predict once; extract verified filters before allocating shared budget.
        with stage("plan"):
            signals: Optional[RetrievalSignals] = None
            try:
                signals = self.controller.predict(query)
                diag["signals"] = signals.as_dict()
            except Exception as exc:
                if self.strict_controller:
                    raise
                diag["controller_error"] = f"{type(exc).__name__}: {exc}"

            filters = {}
            symbolic_allowed = self.use_symbolic and (signals is None or signals.symbolic >= self.symbolic_min_signal)
            if symbolic_allowed:
                try:
                    filters = dict(self.metadata_extractor(query) or {})
                    bad = set(filters) - _ALLOWED_FILTERS
                    if bad:
                        raise ValueError(f"Unsupported structured filters: {sorted(bad)}")
                    filters = {key: val for key, val in filters.items() if val not in (None, [], "")}
                    diag["filters"] = {key: (list(val) if isinstance(val, tuple) else val) for key, val in filters.items()}
                except Exception as exc:
                    filters = {}
                    diag["retrieval_errors"]["symbolic_extract"] = f"{type(exc).__name__}: {exc}"

            if signals is None:
                # Explicit fallback, preserving the old same-K behavior.
                k = self.default_k
                budget = Budget("default", k, k, k if filters else 0)
            else:
                scores = signals.as_dict()
                if self.strategy == "complexity":
                    budget = choose_adaptive_k(scores, self.k_min, self.k_max, self.weights)
                elif self.strategy == "retrieval":
                    budget = choose_retrieval_budget(scores, self.k_min, self.k_max, self.weights)
                else:
                    available = ('semantic', 'lexical', 'symbolic') if filters else ('semantic', 'lexical')
                    budget = choose_combined_budget(scores, self.k_min, self.k_max, self.weights, available=available)
            diag["strategy"] = self.strategy
            diag["budget"] = {"level": budget.level, "semantic_k": budget.semantic_k,
                              "keyword_k": budget.keyword_k, "structured_k": budget.structured_k,
                              "complexity": budget.complexity}

        # 2) semantic + lexical + symbolic, union
        layer_results: List[List[Any]] = []
        ok = 0
        with stage("search"):
            searches = []
            if budget.semantic_k > 0:
                searches.append(("semantic", lambda: self.vector_store.semantic_search(query, top_k=budget.semantic_k)))
            if budget.keyword_k > 0:
                searches.append(("lexical", lambda: self.vector_store.keyword_search(self._keywords(query), top_k=budget.keyword_k)))
            if filters and budget.structured_k > 0:
                searches.append(("symbolic", lambda: self.vector_store.structured_search(
                    **filters, top_k=budget.structured_k)))
            for name, fn in searches:
                try:
                    entries = fn()
                    if entries is None:
                        raise RuntimeError(f"{name} search returned None")
                    entries = list(entries)
                    diag["retrieval_counts"][name] = len(entries)
                    layer_results.append(entries)
                    ok += 1
                except Exception as exc:
                    diag["retrieval_errors"][name] = f"{type(exc).__name__}: {exc}"
        if ok == 0:
            raise RuntimeError(f"All retrieval methods failed: {diag['retrieval_errors']}")

        # C_q = R_sem U R_lex U R_sym. Interleave by rank (sem, lex, sym, sem, lex, ...) so that the
        # cap at max_total keeps a fair share of every layer instead of only the first layer.
        unique, seen = [], set()
        for rank in range(max(len(r) for r in layer_results)):
            for layer in layer_results:
                if rank < len(layer):
                    e = layer[rank]
                    eid = getattr(e, "entry_id", None)
                    if eid is None:
                        raise TypeError("VectorStore returned an entry without entry_id")
                    if eid not in seen:
                        seen.add(eid); unique.append(e)
        diag["num_candidates"] = sum(diag["retrieval_counts"].values())
        diag["num_before_cap"] = len(unique)
        unique = unique[: self.max_total]
        diag["num_unique"] = len(unique)
        return unique