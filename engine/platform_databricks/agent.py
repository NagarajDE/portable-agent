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
            code_paths=["engine", "shared", "usecases"],
            pip_requirements=["langgraph", "pyyaml", "pydantic", "openai",
                              "mlflow", "databricks-agents", "databricks-sdk"],
        )
    mlflow.register_model(info.model_uri, "main.dq.dq_agent")      # -> Unity Catalog
    agents.deploy("main.dq.dq_agent", version=1)                   # -> Model Serving endpoint
    # then point a Databricks App at the endpoint -> chat UI, "just like Genie"
"""
import os

os.environ.setdefault("USE_CASE", "dq_qals")
os.environ.setdefault("WORKER_PROVIDER", "databricks")  # Foundation Model API
os.environ.setdefault("SQL_TOOL", "genie")  # Genie as text-to-SQL
os.environ.setdefault("TRACER", "stdout")  # Set TRACER=mlflow for spans.
# Memory is opt-in via MEMORY_STORE; remember_run handles initialization and
# persistence failures internally without interrupting serving.

from mlflow.models import set_model
from mlflow.pyfunc import ResponsesAgent
from mlflow.types.responses import ResponsesAgentRequest, ResponsesAgentResponse

from engine.graph import build_graph, initial_state
from engine.memory import remember_run
from engine.tracing import traced_invoke


class PortableAgent(ResponsesAgent):
    def __init__(self):
        self.use_case = os.environ["USE_CASE"]
        self.app = build_graph(self.use_case)

    def predict(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
        task = ""
        for item in reversed(request.input):
            message = item.model_dump() if hasattr(item, "model_dump") else item
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                task = content.strip()
            elif isinstance(content, list):
                task = "\n".join(
                    block["text"]
                    for block in content
                    if isinstance(block, dict)
                    and block.get("type") in ("input_text", "text")
                    and isinstance(block.get("text"), str)
                ).strip()
            break

        if not task:
            raise ValueError("A non-empty user text message is required")

        final = traced_invoke(self.app, initial_state(task), self.use_case)
        remember_run(final, self.use_case)
        return ResponsesAgentResponse(
            output=[{"role": "assistant", "content": final["best_answer"]}],
            custom_outputs={"run_id": final["run_id"]},
        )


set_model(PortableAgent())