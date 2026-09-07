"""
ENGINE / memory -- a SELF-CONTAINED, OPTIONAL module. Owns the "learning flywheel"
feedstock: every finished run (EPISODIC) and any thumbs/notes on it (FEEDBACK), as
append-only rows YOU own and can export -- unlike vendor console thumbs/logs. This is
the git-owned crown jewels' raw input: curate these rows into `usecases/*/exemplars`
+ `evals` (a human/offline step -- deliberately NOT automated here).

Scope (deliberate -- see PROJECT_CONTEXT §12.9):
  * WE BUILD: episodic + feedback capture behind one swappable MemoryStore, same pattern
    as LLMClient / SQLTool / Tracer. Loop (`graph.py`) stays pure; capture is applied at
    the call-site boundary via `remember_run(...)`, like `traced_invoke`.
  * WE SKIP: semantic / long-term-facts / RAG vector memory -- least portable (embeddings
    + index are model/vendor-specific) and native (Cortex Search / DBX Vector Search) is
    superior. Wrap the native service behind this interface if/when needed.
  * WE PARK: conversation threads/multi-turn (needs compaction), runtime episodic recall
    + auto-consolidation (would pollute the curated exemplars), and a Databricks/Delta
    adapter (Snowflake is primary and its session is already wired).

Switch with one env var (default OFF -- opt-in, safe for the mock-first deploy):
    MEMORY_STORE=none|mock|sqlite|snowflake

Guarantees: episodic capture is FAIL-SAFE (a store error never breaks an answer);
it makes no model/SQL-generation calls, so zero token/cost impact. Once you persist
question/answer text, YOU own its PII/retention -- rows are append-only + run_id-keyed
so deletion-by-request is a simple DELETE.
"""
from __future__ import annotations
import os
import time
import threading
from typing import Protocol, runtime_checkable


@runtime_checkable
class MemoryStore(Protocol):
    def log_interaction(self, run_id: str, use_case: str, question: str,
                        answer: str, score, iterations) -> None: ...
    def record_feedback(self, run_id: str, rating: str, note: str = "") -> None: ...


# --------------------------------------------------------------------------
# NONE -- no-op (MEMORY_STORE=none, the default). Nothing is persisted.
# --------------------------------------------------------------------------
class NullMemory:
    def log_interaction(self, *a, **k) -> None: ...
    def record_feedback(self, *a, **k) -> None: ...


# --------------------------------------------------------------------------
# MOCK -- in-memory lists. Zero deps; for tests and local inspection.
# --------------------------------------------------------------------------
class MockMemory:
    def __init__(self):
        self.interactions: list[dict] = []
        self.feedback: list[dict] = []

    def log_interaction(self, run_id, use_case, question, answer, score, iterations) -> None:
        self.interactions.append({"run_id": run_id, "use_case": use_case,
                                  "question": question, "answer": answer, "score": score,
                                  "iterations": iterations, "ts": time.time()})

    def record_feedback(self, run_id, rating, note="") -> None:
        self.feedback.append({"run_id": run_id, "rating": rating, "note": note,
                              "ts": time.time()})


# --------------------------------------------------------------------------
# SQLITE -- a local file. Durable local-dev persistence, still zero external infra.
# A fresh connection per write keeps it safe under FastAPI's worker threads.
# --------------------------------------------------------------------------
_DDL_SQLITE = (
    """CREATE TABLE IF NOT EXISTS interactions (
         run_id TEXT, use_case TEXT, question TEXT, answer TEXT,
         score INTEGER, iterations INTEGER, ts TEXT DEFAULT (datetime('now')))""",
    """CREATE TABLE IF NOT EXISTS feedback (
         run_id TEXT, rating TEXT, note TEXT, ts TEXT DEFAULT (datetime('now')))""",
)


class SqliteMemory:
    def __init__(self, path: str | None = None):
        import sqlite3
        self._sqlite3 = sqlite3
        self._path = path or os.getenv("MEMORY_SQLITE_PATH", "portable_agent_memory.db")
        with self._connect() as c:
            for ddl in _DDL_SQLITE:
                c.execute(ddl)

    def _connect(self):
        return self._sqlite3.connect(self._path)

    def log_interaction(self, run_id, use_case, question, answer, score, iterations) -> None:
        with self._connect() as c:
            c.execute("INSERT INTO interactions "
                      "(run_id, use_case, question, answer, score, iterations) "
                      "VALUES (?,?,?,?,?,?)",
                      (run_id, use_case, question, answer, score, iterations))

    def record_feedback(self, run_id, rating, note="") -> None:
        with self._connect() as c:
            c.execute("INSERT INTO feedback (run_id, rating, note) VALUES (?,?,?)",
                      (run_id, rating, note))


# --------------------------------------------------------------------------
# SNOWFLAKE -- append-only tables via the Snowpark session (SPCS OAuth token or local
# creds, shared with the Cortex adapters). Tables live in the session's current
# database/schema. Needs CREATE TABLE + INSERT privileges for the service's role.
# --------------------------------------------------------------------------
_T_INTERACTIONS = "PORTABLE_AGENT_INTERACTIONS"
_T_FEEDBACK = "PORTABLE_AGENT_FEEDBACK"
_DDL_SNOWFLAKE = (
    f"""CREATE TABLE IF NOT EXISTS {_T_INTERACTIONS} (
          RUN_ID STRING, USE_CASE STRING, QUESTION STRING, ANSWER STRING,
          SCORE NUMBER, ITERATIONS NUMBER, TS TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP())""",
    f"""CREATE TABLE IF NOT EXISTS {_T_FEEDBACK} (
          RUN_ID STRING, RATING STRING, NOTE STRING, TS TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP())""",
)


class SnowflakeMemory:
    def __init__(self):
        from engine.llm_client import snowpark_session      # lazy: only when selected
        self._s = snowpark_session()
        for ddl in _DDL_SNOWFLAKE:
            self._s.sql(ddl).collect()

    def log_interaction(self, run_id, use_case, question, answer, score, iterations) -> None:
        self._s.sql(f"INSERT INTO {_T_INTERACTIONS} "
                    "(RUN_ID, USE_CASE, QUESTION, ANSWER, SCORE, ITERATIONS) "
                    "VALUES (?,?,?,?,?,?)",
                    params=[run_id, use_case, question, answer, score, iterations]).collect()

    def record_feedback(self, run_id, rating, note="") -> None:
        self._s.sql(f"INSERT INTO {_T_FEEDBACK} (RUN_ID, RATING, NOTE) VALUES (?,?,?)",
                    params=[run_id, rating, note]).collect()


# --------------------------------------------------------------------------
# SELECTION -- the only place a store name is mentioned. One memoized instance
# (a Snowflake session / sqlite file is not free to rebuild per request).
# --------------------------------------------------------------------------
_STORES = {"mock": MockMemory, "sqlite": SqliteMemory, "snowflake": SnowflakeMemory}
_instance: MemoryStore | None = None
_lock = threading.Lock()


def _backend() -> str:
    return os.getenv("MEMORY_STORE", "none").strip().lower()


def memory_enabled() -> bool:
    return _backend() not in ("none", "off", "false", "0", "")


def get_memory() -> MemoryStore:
    """The active store (memoized). Raises if a selected backend can't init -- callers
    that must stay up (the serving shell) should catch and fall back to NullMemory."""
    global _instance
    with _lock:
        if _instance is None:
            b = _backend()
            _instance = _STORES[b]() if b in _STORES else NullMemory()
        return _instance


def remember_run(store: MemoryStore, state: dict, use_case: str) -> None:
    """Persist one finished run as an EPISODIC row. Best-effort: a store failure is
    swallowed so it can never break the answer that already succeeded."""
    try:
        store.log_interaction(state.get("run_id", ""), use_case, state.get("task", ""),
                              state.get("best_answer", ""), state.get("best_score"),
                              state.get("iterations"))
    except Exception:
        pass
