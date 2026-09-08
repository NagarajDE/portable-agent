"""
THE ▒ SHELL -- the only Databricks-specific file in the whole repo.
Imports the portable loop, picks a use-case pack, exposes it in Databricks' shape.
No domain or business logic here. Leaving Databricks = rewrite ONLY this file.

Deploy (in a Databricks notebook):

    import mlflow
    from databricks import agents

    with mlflow.start_run():
        info = mlflow.pyfunc.log_model(
            name="dq_agent",
            python_model="engine/platform_databricks/agent.py",   # Models-from-Code
            code_paths=["engine", "shared", "usecases"],                     # ship both packages
            pip_requirements=["langgraph", "pyyaml", "pydantic", "openai",
                              "mlflow", "databricks-agents", "databricks-sdk"],
        )
    mlflow.register_model(info.model_uri, "main.dq.dq_agent")      # -> Unity Catalog
    agents.deploy("main.dq.dq_agent", version=1)                   # -> Model Serving endpoint
    # then point a Databricks App at the endpoint -> chat UI, "just like Genie"
"""
import os
os.environ.setdefault("USE_CASE", "dq_qals")
os.environ.setdefault("WORKER_PROVIDER", "databricks")     # Foundation Model API
os.environ.setdefault("SQL_TOOL", "genie")              # Genie as text-to-SQL
os.environ.setdefault("TRACER", "stdout")               # JSON events -> serving logs; set TRACER=mlflow for spans

from mlflow.pyfunc import ResponsesAgent
from mlflow.types.responses import ResponsesAgentRequest, ResponsesAgentResponse
from mlflow.models import set_model

from engine.graph import build_graph, initial_state
from engine.memory import remember_run
from engine.tracing import traced_invoke


def _last_user_text(request: ResponsesAgentRequest) -> str:
    """Extract the last user message robustly: content may be a plain string or a list of
    typed blocks (input_text/text). Assuming input[-1]['content'] is a string is fragile."""
    for item in reversed(request.input):
        msg = item.model_dump() if hasattr(item, "model_dump") else item
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "\n".join(
                b["text"] for b in content
                if isinstance(b, dict) and b.get("type") in ("input_text", "text")
                and isinstance(b.get("text"), str)).strip()
        break
    return ""


class PortableAgent(ResponsesAgent):
    def __init__(self):
        self.use_case = os.environ["USE_CASE"]
        self.app = build_graph(self.use_case)                # portable loop + chosen pack

    def predict(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
        task = _last_user_text(request)
        if not task:
            raise ValueError("A non-empty user text message is required")
        final = traced_invoke(self.app, initial_state(task), self.use_case)
        remember_run(final, self.use_case)               # episodic capture (best-effort, no-op unless MEMORY_STORE set)
        return ResponsesAgentResponse(
            output=[{"role": "assistant", "content": final["best_answer"]}],
            custom_outputs={"run_id": final["run_id"]})


set_model(PortableAgent())
