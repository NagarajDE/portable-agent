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

USE_CASE = os.environ.get("USE_CASE", "dq_qals")
os.environ.setdefault("LLM_PROVIDER", "cortex")   # Cortex COMPLETE
os.environ.setdefault("SQL_TOOL", "cortex")       # Cortex Analyst

app = FastAPI(title="Portable Agent (Snowflake/SPCS)")
_graph = build_graph(USE_CASE, verbose=False)      # built once at startup


class AskRequest(BaseModel):
    question: str


class AskResponse(BaseModel):
    answer: str
    score: int


@app.get("/healthz")
def healthz():
    return {"status": "ok", "use_case": USE_CASE}


@app.post("/invoke", response_model=AskResponse)
def invoke(req: AskRequest) -> AskResponse:
    final = _graph.invoke(initial_state(req.question))
    return AskResponse(answer=final["best_answer"], score=final["best_score"])
