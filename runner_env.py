"""
Test/dev-time ENV bridge for the RUNNER scripts (run_local.py, run_evals.py, chat_local.py).

NOT engine source. engine/ NEVER reads env for functional config -- the semantic layer lives in each
pack's semantic_layer.yaml and is read from the pack. The ONLY sanctioned env-vs-pack override -- used
during local / unit testing to point a run at a DIFFERENT semantic layer than the pack declares --
lives HERE, in exactly one place, so it is never sprinkled into source.
"""
import os


def semantic_override():
    """A {"snowflake": {...}} override built from env (CORTEX_SEMANTIC_VIEW / CORTEX_SEMANTIC_MODEL),
    or None to fall back to the pack's semantic_layer.yaml. view wins if both are set. TEST/DEV ONLY;
    engine/ ignores env for the semantic layer -- this override reaches it via build_graph(semantic_layer=)."""
    view = (os.getenv("CORTEX_SEMANTIC_VIEW") or "").strip()
    model = (os.getenv("CORTEX_SEMANTIC_MODEL") or "").strip()
    if view:
        return {"snowflake": {"view": view}}
    if model:
        return {"snowflake": {"model_file": model}}
    return None
