"""
Run any use case locally on the MOCK provider -- no credentials.

    python run_local.py                     # default use case (dq_qals)
    python run_local.py parity_hana_snowflake
    USE_CASE=dq_qals python run_local.py

Flip to a real platform by exporting env first, e.g.:
    WORKER_PROVIDER=databricks SQL_TOOL=genie python run_local.py
"""
import sys, os
from engine.graph import build_graph, initial_state, load_config
from engine.tracing import traced_invoke

def main():
    use_case = (sys.argv[1] if len(sys.argv) > 1
                else os.getenv("USE_CASE", "dq_qals"))
    cfg = load_config(use_case)
    task = cfg["sample_task"]
    print(f"\nUSE CASE: {cfg['name']}  [{use_case}]")
    print(f"TASK    : {task}\n" + "-" * 68)
    app = build_graph(use_case)
    final = traced_invoke(app, initial_state(task), use_case)
    print("-" * 68)
    print(f"BEST SCORE : {final['best_score']}/{cfg.get('threshold', 18)}")
    print(f"BEST ANSWER: {final['best_answer']}\n")

if __name__ == "__main__":
    main()
