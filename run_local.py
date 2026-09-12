"""
Run any use case locally. Provider/model/SQL tool come from the env / .env.

    python run_local.py                          # default use case (dq_qals), its sample_task
    python run_local.py inventory_balance        # a pack, its sample_task
    python run_local.py inventory_balance "Top 5 materials by on-hand quantity?"   # your own question
    USE_CASE=dq_qals  QUESTION="..."  python run_local.py                          # via env vars

Question precedence: CLI args after the use-case  >  $QUESTION  >  the pack's sample_task.
Flip to a real platform via env / .env, e.g.:  WORKER_PROVIDER=cortex SQL_TOOL=cortex
"""
import sys, os
try:                                   # optional: load a local .env if python-dotenv is installed
    from dotenv import load_dotenv
    load_dotenv(override=True)          # .env is the local source of truth -> it wins over any
                                        # stale SNOWFLAKE_*/WORKER_* already exported in the shell
except ImportError:
    pass
from engine.graph import build_graph, initial_state, load_config
from engine.tracing import traced_invoke
from engine.memory import remember_run
from runner_env import semantic_override      # TEST/DEV-only env->pack override (kept in one place)

def main():
    args = sys.argv[1:]
    use_case = args[0] if args else os.getenv("USE_CASE", "dq_qals")
    cfg = load_config(use_case)
    # question: everything after the use-case on the CLI > $QUESTION > the pack's sample_task
    task = " ".join(args[1:]).strip() or os.getenv("QUESTION") or cfg["sample_task"]
    print(f"\nUSE CASE: {cfg['name']}  [{use_case}]")
    print(f"TASK    : {task}\n" + "-" * 68)
    app = build_graph(use_case, semantic_layer=semantic_override())
    final = traced_invoke(app, initial_state(task), use_case)
    remember_run(final, use_case)                   # episodic capture (no-op unless MEMORY_STORE set)
    print("-" * 68)
    # denominator is max_score (the score's scale), NOT threshold/pass_score (the stop bar)
    print(f"BEST SCORE : {final['best_score']}/{cfg.get('max_score', 18)}")
    print(f"BEST ANSWER: {final['best_answer']}\n")

if __name__ == "__main__":
    main()
