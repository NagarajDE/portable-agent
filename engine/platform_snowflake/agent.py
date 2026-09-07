"""
THE ▒ SHELL for Snowflake — the analog to engine/platform_databricks/agent.py.
Imports the portable loop, exposes it as an HTTP service. No business logic
here. If you leave Snowflake, you rewrite ONLY this file (+ Dockerfile + spec).

Deploy (from a terminal with Snowflake CLI configured):

    snow spcs image-repository create dq_agent_repo
    docker build -t <repo_url>/dq_agent:latest -f engine/platform_snowflake/Dockerfile .
    docker push <repo_url>/dq_agent:latest
    snow spcs compute-pool create dq_pool --family CPU_X64_XS --min-nodes 1 --max-nodes 1 \
        --auto-suspend-secs 300 --auto-resume
    snow spcs service create dq_agent --compute-pool dq_pool \
        --spec-path engine/platform_snowflake/spec.yaml --min-instances 1 --max-instances 1
    # then: SHOW ENDPOINTS IN SERVICE dq_agent;  -> the URL people/tools hit
"""
import os
from fastapi import FastAPI
from pydantic import BaseModel, Field, field_validator

from engine.graph import build_graph, initial_state
from engine.tracing import traced_invoke
from engine.memory import get_memory, remember_run, memory_enabled, NullMemory

USE_CASE = os.environ.get("USE_CASE", "dq_qals")
os.environ.setdefault("WORKER_PROVIDER", "cortex")   # Cortex COMPLETE
os.environ.setdefault("SQL_TOOL", "cortex")       # Cortex Analyst
os.environ.setdefault("TRACER", "stdout")         # JSON events -> SPCS stdout -> event table
# MEMORY_STORE defaults to "none" (opt-in). Set MEMORY_STORE=snowflake to persist the
# episodic + feedback flywheel to tables the service's role owns.

app = FastAPI(title="Portable Agent (Snowflake/SPCS)")
_graph = build_graph(USE_CASE, verbose=False)      # built once at startup
try:                                               # memory is auxiliary: never let a
    _memory = get_memory()                         # misconfigured store take down serving
except Exception:
    _memory = NullMemory()


class AskRequest(BaseModel):
    question: str = Field(strict=True, min_length=1, max_length=10_000)

    @field_validator("question")
    @classmethod
    def validate_question(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("question must contain non-whitespace characters")
        return value


class AskResponse(BaseModel):
    answer: str
    score: int
    run_id: str          # correlate this answer to its event-table / query-history / memory rows


class FeedbackRequest(BaseModel):
    run_id: str
    rating: str                    # e.g. "up" / "down", or "1".."5" — your convention
    note: str | None = None


@app.get("/healthz")
def healthz():
    return {"status": "ok", "use_case": USE_CASE,
            "tracer": os.getenv("TRACER", "stdout"),
            "memory": os.getenv("MEMORY_STORE", "none")}


@app.post("/invoke", response_model=AskResponse)
def invoke(req: AskRequest) -> AskResponse:
    final = traced_invoke(_graph, initial_state(req.question), USE_CASE)
    remember_run(_memory, final, USE_CASE)          # episodic capture (best-effort)
    return AskResponse(answer=final["best_answer"], score=final["best_score"],
                       run_id=final["run_id"])


@app.post("/feedback")
def feedback(req: FeedbackRequest):
    if not memory_enabled():
        return {"status": "memory_disabled", "run_id": req.run_id}
    _memory.record_feedback(req.run_id, req.rating, req.note or "")
    return {"status": "recorded", "run_id": req.run_id}
