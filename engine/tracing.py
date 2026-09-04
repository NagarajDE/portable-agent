"""
ENGINE / observability -- a SELF-CONTAINED, OPTIONAL module.  Nothing in the loop
(`graph.py`) contains tracing logic; the loop stays pure and this file wraps it from
the outside.  To change or extend observability, you edit ONLY this file.

Design goals (all enforced here, not in the loop):
  * SEPARATE     -- all tracing code lives here; `graph.py` calls `instrument(...)` once
                    per node and `traced_invoke(...)` once per run. Nothing else.
  * CONFIGURABLE -- one env var. TRACER=none (or off) fully disables it; when disabled,
                    `instrument` returns the bare node function, so there is LITERALLY
                    ZERO overhead (no wrapper, no timing, no branch per call).
  * NON-INVASIVE -- it NEVER changes prompts, answers, scores, or control flow, so it
                    cannot affect ACCURACY. It makes NO model/SQL calls, so it adds NO
                    TOKENS / COST. LATENCY impact is a small span/log write per event
                    (exporters batch async) -- sub-ms vs. the LLM calls that dominate.
  * FAIL-SAFE    -- any error inside a tracer is swallowed; a broken/missing sink can
                    never take down an answer. (Business logic runs first, emission is
                    best-effort.)

Sinks are swappable, same pattern as LLMClient / SQLTool:
    TRACER=stdout|otel|mlflow|eventtable|none
  - stdout     : one JSON line per event. Zero deps. Both platforms collect stdout.
  - otel       : OpenTelemetry spans (run = trace, node = child span), exported via OTLP
                 to any backend (set OTEL_EXPORTER_OTLP_ENDPOINT); falls back to a console
                 exporter for local visibility. Vendor-neutral -- the recommended default
                 once you have a tracing backend.
  - mlflow     : MLflow Tracing (Databricks-native). Stubbed.
  - eventtable : Snowflake SPCS event-table rows. Stubbed.

Do NOT rebuild token/credit/cost telemetry here -- that lives in Cortex usage views /
Databricks system tables. Correlate to it with the `run_id` every event carries.
"""
from __future__ import annotations
import os
import sys
import json
import time
import logging
import threading
from typing import Callable, Protocol, runtime_checkable


# ==========================================================================
# SINKS (adapters) -- the only place a backend name is mentioned.
# ==========================================================================
@runtime_checkable
class Tracer(Protocol):
    def event(self, name: str, **fields) -> None: ...   # record one structured event
    def flush(self) -> None: ...                         # push anything buffered
    def close(self) -> None: ...                         # finalize a run (end root span etc.)


class StdoutTracer:
    """Portable default: one JSON line per event to stdout. Zero deps."""
    def __init__(self, run_id: str, use_case: str):
        self.base = {"run_id": run_id, "use_case": use_case}
        self._log = logging.getLogger("portable_agent")
        if not self._log.handlers:                       # configure the logger once
            h = logging.StreamHandler(sys.stdout)
            h.setFormatter(logging.Formatter("%(message)s"))   # message is already JSON
            self._log.addHandler(h)
            self._log.setLevel(logging.INFO)
            self._log.propagate = False

    def event(self, name: str, **fields) -> None:
        rec = {"ts": round(time.time(), 3), "event": name, **self.base, **fields}
        self._log.info(json.dumps(rec, default=str))

    def flush(self) -> None:
        for h in self._log.handlers:
            h.flush()

    def close(self) -> None:
        self.flush()


class NullTracer:
    """No-op sink (TRACER=none). Usually unused because `instrument` short-circuits."""
    def event(self, name: str, **fields) -> None: ...
    def flush(self) -> None: ...
    def close(self) -> None: ...


class OTelTracer:
    """OpenTelemetry: run = one trace (root span), each node = a child span with its
    measured duration. Exported via OTLP if OTEL_EXPORTER_OTLP_ENDPOINT is set, else to a
    console exporter so it's visible locally. SDK imported lazily."""
    _configured = False
    _lock = threading.Lock()

    def __init__(self, run_id: str, use_case: str):
        from opentelemetry import trace                  # lazy: only when TRACER=otel
        with OTelTracer._lock:
            if not OTelTracer._configured:
                _configure_otel(use_case)
                OTelTracer._configured = True
        self._trace = trace
        self._tracer = trace.get_tracer("portable_agent")
        self._run_id, self._use_case = run_id, use_case
        self._root = None
        self._root_ctx = None

    def _attrs(self, fields: dict) -> dict:
        a = {"run_id": self._run_id, "use_case": self._use_case}
        for k, v in fields.items():
            if v is not None:
                a[k] = v if isinstance(v, (str, int, float, bool)) else str(v)
        return a

    def event(self, name: str, **fields) -> None:
        if name == "run_start":
            self._root = self._tracer.start_span("run")
            for k, v in self._attrs(fields).items():
                self._root.set_attribute(k, v)
            self._root_ctx = self._trace.set_span_in_context(self._root)
            return
        if name == "run_end":
            if self._root is not None:
                for k, v in self._attrs(fields).items():
                    self._root.set_attribute(k, v)
                self._root.end()
                self._root = None
            return
        # a node event -> child span whose start/end reflect the measured ms
        ms = fields.get("ms", 0) or 0
        end = time.time_ns()
        span = self._tracer.start_span(name, context=self._root_ctx,
                                       start_time=end - int(ms * 1e6))
        for k, v in self._attrs(fields).items():
            span.set_attribute(k, v)
        span.end(end_time=end)

    def flush(self) -> None:
        try:
            self._trace.get_tracer_provider().force_flush()
        except Exception:
            pass

    def close(self) -> None:
        if self._root is not None:
            self._root.end()
            self._root = None
        self.flush()


class MLflowTracer:
    """Databricks: MLflow Tracing spans. One run == one trace; each event == a span."""
    def __init__(self, run_id: str, use_case: str):
        import mlflow                                     # lazy: only when TRACER=mlflow
        self._mlflow = mlflow
        self.base = {"run_id": run_id, "use_case": use_case}
        # TODO: start a trace (mlflow.start_span / @mlflow.trace) and keep the handle.

    def event(self, name: str, **fields) -> None:
        raise NotImplementedError("wire MLflow Tracing span here")

    def flush(self) -> None: ...
    def close(self) -> None: ...


class EventTableTracer:
    """Snowflake: write rows to a table (or the event-table logging API). Note: in SPCS,
    StdoutTracer already lands in the event table if the service has a log level set."""
    def __init__(self, run_id: str, use_case: str):
        self.base = {"run_id": run_id, "use_case": use_case}
        # TODO: get a Snowpark session (engine.llm_client.sf_cfg) and INSERT rows.

    def event(self, name: str, **fields) -> None:
        raise NotImplementedError("wire Snowflake event-table insert here")

    def flush(self) -> None: ...
    def close(self) -> None: ...


def _configure_otel(use_case: str) -> None:
    """Set up a process-wide OTel TracerProvider ONCE. Reuses one the host already set
    (e.g. Databricks) instead of clobbering it."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    from opentelemetry.sdk.resources import Resource

    if isinstance(trace.get_tracer_provider(), TracerProvider):
        return                                           # a real provider already exists
    resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", "portable-agent"),
                                "portable_agent.use_case": use_case})
    provider = TracerProvider(resource=resource)
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        exporter = OTLPSpanExporter()                    # reads endpoint/headers from env
    else:
        exporter = ConsoleSpanExporter()                 # local visibility, no backend needed
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)


_SINKS = {"stdout": StdoutTracer, "otel": OTelTracer,
          "mlflow": MLflowTracer, "eventtable": EventTableTracer}
_active: dict[str, Tracer] = {}                          # one tracer per in-flight run_id
_active_lock = threading.Lock()


def _backend() -> str:
    return os.getenv("TRACER", "stdout").strip().lower()


def tracing_enabled() -> bool:
    """False when TRACER is none/off. Read at graph-build time by `instrument`."""
    return _backend() not in ("none", "off", "false", "0")


def get_tracer(run_id: str, use_case: str) -> Tracer:
    """One tracer per run_id, memoized so a run's nodes + run_start/run_end share it
    (needed for OTel/MLflow span trees). Freed by `end_tracer` at run end."""
    if not tracing_enabled():
        return NullTracer()
    with _active_lock:
        t = _active.get(run_id)
        if t is None:
            b = _backend()
            cls = _SINKS.get(b if b not in ("", "auto") else "stdout", StdoutTracer)
            t = cls(run_id, use_case)
            _active[run_id] = t
        return t


def end_tracer(run_id: str) -> None:
    with _active_lock:
        t = _active.pop(run_id, None)
    if t is not None:
        t.close()


# ==========================================================================
# INSTRUMENTATION -- how the loop is observed WITHOUT touching the loop.
# graph.py imports only `instrument`; call sites import `traced_invoke`.
# ==========================================================================
def _fields(name: str, out: dict) -> dict:
    """Pull a small, uniform snapshot from a node's OUTPUT state. Knowing the state
    shape lives HERE, not in the nodes, so the loop stays observability-free."""
    f = {"iter": out.get("iterations"),
         "score": out.get("score"),
         "best_score": out.get("best_score")}
    if name in ("generate", "refine"):
        f["answer_preview"] = (out.get("answer") or "")[:120]
    if name == "evaluate":
        f["verdict"] = out.get("feedback")           # "SCORE: n/thr - reason"
    return f


def instrument(name: str, fn: Callable[[dict], dict], use_case: str) -> Callable[[dict], dict]:
    """Wrap one loop node so it emits a timed event after it runs. When tracing is
    disabled this returns `fn` UNCHANGED -> zero overhead. The node runs first and its
    result is returned no matter what; emission is best-effort and never raises."""
    if not tracing_enabled():
        return fn

    def wrapped(state: dict) -> dict:
        t0 = time.perf_counter()
        out = fn(state)                              # business logic; errors propagate
        try:                                         # emission is best-effort only
            ms = round((time.perf_counter() - t0) * 1000)
            get_tracer(state.get("run_id", "-"), use_case).event(name, ms=ms, **_fields(name, out))
        except Exception:                            # observability must never break a run
            pass
        return out

    return wrapped


def traced_invoke(graph, state: dict, use_case: str) -> dict:
    """Run the graph, bracketing it with run_start / run_end and freeing the run's tracer.
    Use this instead of graph.invoke(...) at call sites. No-op bracket when disabled; the
    graph always runs, and tracing errors are swallowed."""
    if not tracing_enabled():
        return graph.invoke(state)
    run_id = state.get("run_id", "-")
    try:
        from engine.llm_client import model_summary
        get_tracer(run_id, use_case).event("run_start", models=model_summary())
    except Exception:
        pass
    try:
        out = graph.invoke(state)                    # the actual run (never gated by tracing)
    except Exception:
        try:
            end_tracer(run_id)
        except Exception:
            pass
        raise
    try:
        get_tracer(run_id, use_case).event("run_end", best_score=out.get("best_score"),
                                           iterations=out.get("iterations"))
    except Exception:
        pass
    try:
        end_tracer(run_id)
    except Exception:
        pass
    return out
