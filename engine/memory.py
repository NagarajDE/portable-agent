"""
Optional, capture-only episodic memory.

This module stores finished runs and explicit feedback at the serving boundary;
it does not perform retrieval, alter prompts, generate SQL, call models, maintain
conversation state, or automatically consolidate examples. Curating captured rows
into exemplars and evaluations remains a separate, human-controlled process.

Configuration:
    MEMORY_STORE=none|mock|sqlite|snowflake       (default: none)
    MEMORY_TIMEOUT_SECONDS=5                    (positive, finite seconds)
    MEMORY_MAX_ENTRIES=1000                      (MockMemory, per collection)
    MEMORY_SQLITE_PATH=portable_agent_memory.db
    MEMORY_SNOWFLAKE_DATABASE=<session database>
    MEMORY_SNOWFLAKE_SCHEMA=<session schema>

Call get_memory() during application startup to validate configuration and
initialize the selected backend. Unknown backend names raise ValueError.
remember_run() is the fail-safe serving boundary: it catches both initialization
and persistence failures. Both remember_run(store, state, use_case) and
remember_run(state, use_case) are supported.

Timeouts are enforced using timed locks, SQLite busy/progress timeouts, and
Snowflake statement timeouts. Snowflake's timeout has one-second granularity and
does not replace connector/network timeouts. Arbitrary third-party MemoryStore
implementations must implement their own persistence timeouts.

Captured question, answer, and feedback text is intentionally persisted verbatim.
Logs never contain that text or raw exception messages. You own access control,
PII handling, backups, and retention. MockMemory automatically retains only its
newest MEMORY_MAX_ENTRIES rows in each collection. Durable stores require a
scheduled purge_before(unix_timestamp), plus an independent backup-retention
policy. purge_before() removes rows strictly older than its UTC cutoff.

SQLite enforces interaction run_id uniqueness. Snowflake standard tables declare
UNIQUE constraints but do not enforce them; MERGE prevents sequential duplicate
inserts, not concurrent inserts from independent processes. Deployments requiring
cross-process uniqueness must use enforced-key tables or a single writer.

Schema version 1 is tracked in each durable backend. Existing, unversioned tables
are rejected rather than silently accepting an incompatible schema or discarding
duplicate historical rows. Migrate or export those tables before opting in.

status() reports configuration, the effective backend, a sanitized initialization
error, and successful capture/feedback writes and failed operations. Counters are
process-local. All singleton initialization and per-instance writes are locked.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import logging
import math
from numbers import Real
import os
import re
import threading
import time
from typing import Protocol, runtime_checkable


logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_DISABLED_BACKENDS = frozenset({"none", "off", "false", "0", ""})
_BACKENDS = frozenset({"mock", "sqlite", "snowflake"})
_LOG_INTERVAL_SECONDS = 60.0
_log_lock = threading.Lock()
_log_last: dict[tuple[str, str], float] = {}


def _redact(value: object) -> str:
    """Return a constant marker without inspecting potentially sensitive input."""
    return "[redacted]"


def _log_failure(operation: str, backend: str, error: Exception) -> None:
    """Rate-limit structured logs; neither messages nor tracebacks expose input."""
    try:
        key = (operation, backend)
        now = time.monotonic()
        with _log_lock:
            previous = _log_last.get(key)
            if previous is not None and now - previous < _LOG_INTERVAL_SECONDS:
                return
            _log_last[key] = now
        logger.warning(
            "Memory operation failed",
            extra={
                "memory_operation": operation,
                "memory_backend": backend,
                "memory_error_type": type(error).__name__,
                "memory_error": _redact(error),
            },
        )
    except Exception:
        # A broken logging handler must not break the fail-safe serving boundary.
        return


def _backend() -> str:
    backend = os.getenv("MEMORY_STORE", "none").strip().lower()
    if backend not in _DISABLED_BACKENDS and backend not in _BACKENDS:
        raise ValueError(
            "Unsupported MEMORY_STORE; expected none, mock, sqlite, or snowflake"
        )
    return backend


def _timeout(value: float | None = None) -> float:
    try:
        result = float(
            os.getenv("MEMORY_TIMEOUT_SECONDS", "5") if value is None else value
        )
    except (TypeError, ValueError, OverflowError) as from_error:
        raise ValueError("Memory timeout must be a positive finite number") from from_error
    if not math.isfinite(result) or result <= 0:
        raise ValueError("Memory timeout must be a positive finite number")
    return result


def _max_entries(value: int | None = None) -> int:
    if value is None:
        try:
            value = int(os.getenv("MEMORY_MAX_ENTRIES", "1000"))
        except ValueError as error:
            raise ValueError("MEMORY_MAX_ENTRIES must be a positive integer") from error
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("MEMORY_MAX_ENTRIES must be a positive integer")
    return value


def _validate_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id must be a non-empty string")


def _validate_interaction(run_id: str, score: Real, iterations: int) -> float:
    _validate_run_id(run_id)
    if isinstance(score, bool) or not isinstance(score, Real):
        raise ValueError("score must be a finite numeric value")
    try:
        numeric_score = float(score)
    except (ValueError, OverflowError) as error:
        raise ValueError("score must be a finite numeric value") from error
    if not math.isfinite(numeric_score):
        raise ValueError("score must be a finite numeric value")
    if (
        isinstance(iterations, bool)
        or not isinstance(iterations, int)
        or iterations < 0
    ):
        raise ValueError("iterations must be a non-negative integer")
    return numeric_score


def _cutoff(cutoff_ts: float) -> float:
    if isinstance(cutoff_ts, bool) or not isinstance(cutoff_ts, Real):
        raise ValueError("cutoff_ts must be a finite Unix timestamp")
    try:
        result = float(cutoff_ts)
        if not math.isfinite(result):
            raise ValueError
        datetime.fromtimestamp(result, timezone.utc)
    except (ValueError, OverflowError, OSError) as error:
        raise ValueError("cutoff_ts must be a finite Unix timestamp") from error
    return result


def _utc_timestamp(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )


@runtime_checkable
class MemoryStore(Protocol):
    def log_interaction(
        self,
        run_id: str,
        use_case: str,
        question: str,
        answer: str,
        score: Real,
        iterations: int,
    ) -> None: ...

    def record_feedback(self, run_id: str, rating: str, note: str = "") -> None: ...

    def purge_before(self, cutoff_ts: float) -> None: ...

    def status(self) -> dict: ...


class _MemoryBase:
    backend = "none"

    def __init__(self, timeout: float | None = None):
        self._timeout = _timeout(timeout)
        self._configured_backend = self.backend
        self._write_count = 0
        self._error_count = 0
        self._counter_lock = threading.Lock()
        self._operation_lock = threading.Lock()

    @contextmanager
    def _operation(self, name: str, *, write: bool = False) -> Iterator[float]:
        acquired = False
        deadline = time.monotonic() + self._timeout
        try:
            acquired = self._operation_lock.acquire(timeout=self._timeout)
            if not acquired:
                raise TimeoutError("Memory operation lock timed out")
            yield deadline
            if write:
                with self._counter_lock:
                    self._write_count += 1
        except Exception as error:
            with self._counter_lock:
                self._error_count += 1
            _log_failure(name, self.backend, error)
            raise
        finally:
            if acquired:
                self._operation_lock.release()

    def status(self) -> dict:
        with self._counter_lock:
            return {
                "configured_backend": self._configured_backend,
                "effective_backend": self.backend,
                "initialization_error": None,
                "write_count": self._write_count,
                "error_count": self._error_count,
            }


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Memory operation timed out")
    return remaining


class NullMemory:
    """No persistence, validation, dependency initialization, or write counting."""

    def __init__(self, configured_backend: str = "none"):
        self._configured_backend = configured_backend

    def log_interaction(self, *args, **kwargs) -> None:
        return None

    def record_feedback(self, *args, **kwargs) -> None:
        return None

    def purge_before(self, cutoff_ts: float) -> None:
        return None

    def status(self) -> dict:
        return {
            "configured_backend": self._configured_backend,
            "effective_backend": "none",
            "initialization_error": None,
            "write_count": 0,
            "error_count": 0,
        }


class MockMemory(_MemoryBase):
    backend = "mock"

    def __init__(
        self, max_entries: int | None = None, timeout: float | None = None
    ):
        super().__init__(timeout)
        self.max_entries = _max_entries(max_entries)
        self.interactions: list[dict] = []
        self.feedback: list[dict] = []

    def log_interaction(
        self, run_id, use_case, question, answer, score, iterations
    ) -> None:
        with self._operation("log_interaction", write=True) as deadline:
            score = _validate_interaction(run_id, score, iterations)
            if any(row["run_id"] == run_id for row in self.interactions):
                raise ValueError("An interaction with this run_id already exists")
            _remaining(deadline)
            self.interactions.append(
                {
                    "run_id": run_id,
                    "use_case": use_case,
                    "question": question,
                    "answer": answer,
                    "score": score,
                    "iterations": iterations,
                    "ts": time.time(),
                }
            )
            del self.interactions[:-self.max_entries]

    def record_feedback(self, run_id, rating, note="") -> None:
        with self._operation("record_feedback", write=True) as deadline:
            _validate_run_id(run_id)
            _remaining(deadline)
            self.feedback.append(
                {"run_id": run_id, "rating": rating, "note": note, "ts": time.time()}
            )
            del self.feedback[:-self.max_entries]

    def purge_before(self, cutoff_ts: float) -> None:
        with self._operation("purge_before") as deadline:
            cutoff = _cutoff(cutoff_ts)
            interactions = [r for r in self.interactions if r["ts"] >= cutoff]
            feedback = [r for r in self.feedback if r["ts"] >= cutoff]
            _remaining(deadline)
            self.interactions[:] = interactions
            self.feedback[:] = feedback


_DDL_SQLITE = (
    """CREATE TABLE IF NOT EXISTS interactions (
        run_id TEXT NOT NULL UNIQUE,
        use_case TEXT, question TEXT, answer TEXT,
        score REAL NOT NULL,
        iterations INTEGER NOT NULL CHECK (iterations >= 0),
        ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now'))
    )""",
    """CREATE TABLE IF NOT EXISTS feedback (
        run_id TEXT NOT NULL, rating TEXT, note TEXT,
        ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now'))
    )""",
    "CREATE INDEX IF NOT EXISTS feedback_run_id_idx ON feedback (run_id)",
    """CREATE TABLE IF NOT EXISTS memory_schema_version (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        version INTEGER NOT NULL
    )""",
)


class SqliteMemory(_MemoryBase):
    backend = "sqlite"

    def __init__(self, path: str | None = None, timeout: float | None = None):
        import sqlite3

        super().__init__(timeout)
        self._sqlite3 = sqlite3
        self._path = os.fspath(
            path if path is not None else os.getenv(
                "MEMORY_SQLITE_PATH", "portable_agent_memory.db"
            )
        )
        if (
            not self._path
            or self._path == ":memory:"
            or self._path.lower().startswith("file:")
        ):
            raise ValueError("SQLite memory requires a durable, non-URI file path")

        with self._operation("initialize") as deadline:
            with self._connection(deadline) as connection:
                connection.execute("BEGIN IMMEDIATE")
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                if "memory_schema_version" not in tables and tables.intersection(
                    {"interactions", "feedback"}
                ):
                    raise ValueError(
                        "Unversioned memory tables require an explicit migration"
                    )
                if "memory_schema_version" in tables:
                    versions = connection.execute(
                        "SELECT singleton, version FROM memory_schema_version"
                    ).fetchall()
                    if versions != [(1, _SCHEMA_VERSION)]:
                        raise ValueError("Unsupported SQLite memory schema version")
                for ddl in _DDL_SQLITE:
                    connection.execute(ddl)
                connection.execute(
                    "INSERT OR IGNORE INTO memory_schema_version "
                    "(singleton, version) VALUES (1, ?)",
                    (_SCHEMA_VERSION,),
                )

    @contextmanager
    def _connection(self, deadline: float) -> Iterator:
        with closing(
            self._sqlite3.connect(self._path, timeout=_remaining(deadline))
        ) as connection:
            connection.set_progress_handler(
                lambda: int(time.monotonic() >= deadline), 100
            )
            with connection:
                yield connection
                _remaining(deadline)

    def log_interaction(
        self, run_id, use_case, question, answer, score, iterations
    ) -> None:
        with self._operation("log_interaction", write=True) as deadline:
            score = _validate_interaction(run_id, score, iterations)
            with self._connection(deadline) as connection:
                connection.execute(
                    "INSERT INTO interactions "
                    "(run_id, use_case, question, answer, score, iterations, ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id, use_case, question, answer, score, iterations,
                        _utc_timestamp(time.time()),
                    ),
                )

    def record_feedback(self, run_id, rating, note="") -> None:
        with self._operation("record_feedback", write=True) as deadline:
            _validate_run_id(run_id)
            with self._connection(deadline) as connection:
                connection.execute(
                    "INSERT INTO feedback (run_id, rating, note, ts) "
                    "VALUES (?, ?, ?, ?)",
                    (run_id, rating, note, _utc_timestamp(time.time())),
                )

    def purge_before(self, cutoff_ts: float) -> None:
        with self._operation("purge_before") as deadline:
            cutoff = _utc_timestamp(_cutoff(cutoff_ts))
            with self._connection(deadline) as connection:
                connection.execute("DELETE FROM interactions WHERE ts < ?", (cutoff,))
                connection.execute("DELETE FROM feedback WHERE ts < ?", (cutoff,))


_T_INTERACTIONS = "PORTABLE_AGENT_INTERACTIONS"
_T_FEEDBACK = "PORTABLE_AGENT_FEEDBACK"
_T_VERSION = "PORTABLE_AGENT_MEMORY_SCHEMA_VERSION"


def _identifier(value: str | None) -> str:
    """Quote one identifier; never accept SQL fragments or dotted qualification."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Snowflake memory requires a database and schema")
    value = value.strip()
    if re.fullmatch(r'"(?:[^"]|"")+"', value):
        return value
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", value):
        raise ValueError("Invalid Snowflake database or schema identifier")
    return '"' + value.upper() + '"'


class SnowflakeMemory(_MemoryBase):
    backend = "snowflake"

    def __init__(
        self,
        database: str | None = None,
        schema: str | None = None,
        timeout: float | None = None,
    ):
        super().__init__(timeout)
        from engine.llm_client import snowpark_session

        self._s = snowpark_session()
        database = (
            database
            or os.getenv("MEMORY_SNOWFLAKE_DATABASE")
            or self._s.get_current_database()
        )
        schema = (
            schema
            or os.getenv("MEMORY_SNOWFLAKE_SCHEMA")
            or self._s.get_current_schema()
        )
        self._database = _identifier(database)
        self._schema = _identifier(schema)
        prefix = f"{self._database}.{self._schema}"
        self._interactions = f"{prefix}.{_T_INTERACTIONS}"
        self._feedback = f"{prefix}.{_T_FEEDBACK}"
        self._version = f"{prefix}.{_T_VERSION}"

        with self._operation("initialize") as deadline:
            schema_name = self._schema[1:-1].replace('""', '"')
            rows = self._execute(
                f"SELECT TABLE_NAME FROM {self._database}.INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = ? AND TABLE_NAME IN (?, ?, ?)",
                [schema_name, _T_INTERACTIONS, _T_FEEDBACK, _T_VERSION],
                deadline,
            )
            tables = {row[0] for row in rows}
            if _T_VERSION not in tables and tables.intersection(
                {_T_INTERACTIONS, _T_FEEDBACK}
            ):
                raise ValueError(
                    "Unversioned memory tables require an explicit migration"
                )
            if _T_VERSION in tables:
                versions = self._execute(
                    f"SELECT SINGLETON, VERSION FROM {self._version}", [], deadline
                )
                if [tuple(row) for row in versions] != [(1, _SCHEMA_VERSION)]:
                    raise ValueError("Unsupported Snowflake memory schema version")

            statements = (
                f"""CREATE TABLE IF NOT EXISTS {self._interactions} (
                    RUN_ID STRING NOT NULL UNIQUE,
                    USE_CASE STRING, QUESTION STRING, ANSWER STRING,
                    SCORE NUMBER(10,4) NOT NULL, ITERATIONS NUMBER NOT NULL,
                    TS TIMESTAMP_NTZ NOT NULL
                        DEFAULT (SYSDATE())
                )""",
                f"""CREATE TABLE IF NOT EXISTS {self._feedback} (
                    RUN_ID STRING NOT NULL, RATING STRING, NOTE STRING,
                    TS TIMESTAMP_NTZ NOT NULL
                        DEFAULT (SYSDATE())
                ) CLUSTER BY (RUN_ID)""",
                f"""CREATE TABLE IF NOT EXISTS {self._version} (
                    SINGLETON NUMBER NOT NULL UNIQUE, VERSION NUMBER NOT NULL
                )""",
            )
            # Standard Snowflake tables do not support secondary indexes.
            # Clustering provides the backend-native feedback lookup layout.
            for statement in statements:
                self._execute(statement, [], deadline)
            self._execute(
                f"MERGE INTO {self._version} t "
                "USING (SELECT 1 AS SINGLETON, ? AS VERSION) s "
                "ON t.SINGLETON = s.SINGLETON "
                "WHEN NOT MATCHED THEN INSERT (SINGLETON, VERSION) "
                "VALUES (s.SINGLETON, s.VERSION)",
                [_SCHEMA_VERSION],
                deadline,
            )

    def _execute(self, sql: str, params: list, deadline: float):
        seconds = max(1, math.ceil(_remaining(deadline)))
        return self._s.sql(sql, params=params).collect(
            statement_params={
                "STATEMENT_TIMEOUT_IN_SECONDS": str(seconds),
                "STATEMENT_QUEUED_TIMEOUT_IN_SECONDS": str(seconds),
            }
        )

    def log_interaction(
        self, run_id, use_case, question, answer, score, iterations
    ) -> None:
        with self._operation("log_interaction", write=True) as deadline:
            score = _validate_interaction(run_id, score, iterations)
            self._execute(
                f"MERGE INTO {self._interactions} t "
                "USING (SELECT ? AS RUN_ID, ? AS USE_CASE, ? AS QUESTION, "
                "? AS ANSWER, ? AS SCORE, ? AS ITERATIONS) s "
                "ON t.RUN_ID = s.RUN_ID "
                "WHEN NOT MATCHED THEN INSERT "
                "(RUN_ID, USE_CASE, QUESTION, ANSWER, SCORE, ITERATIONS) "
                "VALUES (s.RUN_ID, s.USE_CASE, s.QUESTION, s.ANSWER, "
                "s.SCORE, s.ITERATIONS)",
                [run_id, use_case, question, answer, score, iterations],
                deadline,
            )

    def record_feedback(self, run_id, rating, note="") -> None:
        with self._operation("record_feedback", write=True) as deadline:
            _validate_run_id(run_id)
            self._execute(
                f"INSERT INTO {self._feedback} (RUN_ID, RATING, NOTE) "
                "VALUES (?, ?, ?)",
                [run_id, rating, note],
                deadline,
            )

    def purge_before(self, cutoff_ts: float) -> None:
        with self._operation("purge_before") as deadline:
            cutoff = _utc_timestamp(_cutoff(cutoff_ts))
            # Separate statements make retries safe if only one delete succeeds.
            for table in (self._interactions, self._feedback):
                self._execute(
                    f"DELETE FROM {table} WHERE TS < TO_TIMESTAMP_NTZ(?)",
                    [cutoff],
                    deadline,
                )


_STORES = {
    "mock": MockMemory,
    "sqlite": SqliteMemory,
    "snowflake": SnowflakeMemory,
}
_instance: MemoryStore | None = None
_lock = threading.RLock()
_initialization_error: str | None = None
_initialization_error_count = 0


def memory_enabled() -> bool:
    """Validate the configured backend and report whether capture is requested."""
    return _backend() not in _DISABLED_BACKENDS


def get_memory() -> MemoryStore:
    """Return the locked singleton; configuration and initialization errors raise.

    Backend selection is fixed after successful initialization. status() still
    reports subsequent configuration changes without silently replacing a store.
    """
    global _instance, _initialization_error, _initialization_error_count

    with _lock:
        backend = "invalid"
        try:
            backend = _backend()
            if _instance is None:
                if backend in _DISABLED_BACKENDS:
                    candidate = NullMemory(configured_backend=backend)
                else:
                    candidate = _STORES[backend]()
                    candidate._configured_backend = backend
                _instance = candidate
            _initialization_error = None
            return _instance
        except Exception as error:
            _initialization_error = (
                f"{type(error).__name__}: memory initialization failed; "
                "details redacted"
            )
            _initialization_error_count += 1
            _log_failure("initialize", backend, error)
            raise


def status() -> dict:
    """Inspect current process state without initializing a backend or doing I/O."""
    with _lock:
        try:
            configured = _backend()
        except ValueError:
            configured = "invalid"

        if _instance is None:
            result = NullMemory(configured).status()
        else:
            result = _instance.status()
            result["configured_backend"] = configured
        result["initialization_error"] = _initialization_error
        result["error_count"] += _initialization_error_count
        return result


def remember_run(
    store: MemoryStore | dict | None = None,
    state: dict | str | None = None,
    use_case: str = "",
) -> None:
    """Best-effort capture, including lazy store initialization.

    Accepted forms:
        remember_run(store, state, use_case)
        remember_run(state, use_case)
        remember_run(state=state, use_case=use_case)

    Failure details are logged in sanitized, rate-limited form. No failure from
    validation, configuration, initialization, or writing escapes this boundary.
    """
    backend = "unknown"
    try:
        if isinstance(store, dict):
            if isinstance(state, str):
                if use_case:
                    raise TypeError("use_case was supplied more than once")
                use_case = state
            elif state is not None:
                raise TypeError("Invalid remember_run arguments")
            state = store
            store = None

        if not isinstance(state, dict):
            raise TypeError("state must be a dictionary")

        if store is None:
            store = get_memory()

        if isinstance(store, _MemoryBase):
            backend = store.backend
        elif isinstance(store, NullMemory):
            backend = "none"

        store.log_interaction(
            state.get("run_id", ""),
            use_case,
            state.get("task", ""),
            state.get("best_answer", ""),
            state.get("best_score"),
            state.get("iterations"),
        )
    except Exception as error:
        _log_failure("remember_run", backend, error)
