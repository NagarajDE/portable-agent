# _TEMPLATE — starter pack

Copy this folder to `usecases/<your_pack>/` and edit it. Full guide:
[`../../docs/concepts/use-case-pack-anatomy.md`](../../docs/concepts/use-case-pack-anatomy.md).

It's a minimal single-SQL agent that runs on the mock provider with no credentials:

    python run_local.py _TEMPLATE
    python run_evals.py _TEMPLATE

It deliberately ships NO `prompts/rubric.md` or `prompts/refine.md`, so it inherits the shared ones —
add them only if this domain needs different scoring or rewrite behavior (override is by file
presence, not config). For a NON-SQL agent, declare `tools:` in `config.yaml` and add
`exclude_shared_skills: [sql_safety]`.

If this is an AI+BI/Analyst-backed pack (runs `SQL_TOOL=cortex`/`genie`), copy
`semantic_layer.example.yaml` to `semantic_layer.yaml` and set your native view / stage YAML (or
Databricks metric view). Its presence marks the pack as Analyst-backed; the engine reads the binding
from that file, never from env. A mock/tools pack leaves it absent.
