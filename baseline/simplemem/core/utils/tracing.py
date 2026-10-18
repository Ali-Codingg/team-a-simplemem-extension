import time, contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field

_trace = contextvars.ContextVar("trace", default=None)
_stage = contextvars.ContextVar("stage", default="untagged")

@dataclass
class Trace:
    calls: list = field(default_factory=list)
    stages: dict = field(default_factory=dict)

@contextmanager
def trace_question():
    t = Trace()
    tok = _trace.set(t)
    try:
        yield t
    finally:
        _trace.reset(tok)

@contextmanager
def stage(name):
    tok = _stage.set(name)
    t0 = time.perf_counter()
    try:
        yield
    finally:
        _stage.reset(tok)
        tr = _trace.get()
        if tr:
            tr.stages[name] = tr.stages.get(name, 0.0) + time.perf_counter() - t0

def record_call(prompt_tokens, completion_tokens, seconds):
    tr = _trace.get()
    if tr:
        tr.calls.append(dict(stage=_stage.get(), prompt=prompt_tokens,
                             completion=completion_tokens, seconds=seconds))