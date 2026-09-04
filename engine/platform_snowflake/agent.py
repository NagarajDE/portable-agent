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
from pydantic import BaseModel

from engine.graph import build_graph, initial_state
from engine.tracing import traced_invoke

USE_CASE = os.environ.get("USE_CASE", "dq_qals")
os.environ.setdefault("WORKER_PROVIDER", "cortex")   # Cortex COMPLETE
os.environ.setdefault("SQL_TOOL", "cortex")       # Cortex Analyst
os.environ.setdefault("TRACER", "stdout")         # JSON events -> SPCS stdout -> event table

app = FastAPI(title="Portable Agent (Snowflake/SPCS)")
_graph = build_graph(USE_CASE, verbose=False)      # built once at startup


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    answer: str
    score: int
    run_id: str          # correlate this answer to its event-table / query-history rows


@app.get("/healthz")
def healthz():
    return {"status": "ok", "use_case": USE_CASE, "tracer": os.getenv("TRACER", "stdout")}


@app.post("/invoke", response_model=AskResponse)
def invoke(req: AskRequest) -> AskResponse:
    final = traced_invoke(_graph, initial_state(req.question), USE_CASE)
    return AskResponse(answer=final["best_answer"], score=final["best_score"],
                       run_id=final["run_id"])
