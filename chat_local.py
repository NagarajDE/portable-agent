"""
Interactive local REPL -- ask a use case questions in a loop without relaunching.

TESTING / DEV CONVENIENCE ONLY. Like run_local.py and run_evals.py, this is a RUNNER script:
it imports the engine's PUBLIC api (build_graph / initial_state / traced_invoke / remember_run)
and never modifies engine/ or any source. Do not put engine logic here.

    py -3 chat_local.py                    # default use case (dq_qals, or $USE_CASE)
    py -3 chat_local.py inventory_balance  # a specific pack

Provider / model / SQL tool come from env / .env (same as run_local.py). The graph is built
ONCE and reused for every question (so a Cortex session is opened once, not per question).
Each question is a FRESH run -- the loop has no memory of earlier questions (multi-turn is
deliberately parked; see PROJECT_CONTEXT.md). Type 'exit', 'quit', a blank line, or press
Ctrl-D / Ctrl-C to leave.
"""
import sys, os
try:                                   # optional: load a local .env if python-dotenv is installed
    from dotenv import load_dotenv
    load_dotenv(override=True)          # .env is the local source of truth (same as run_local.py)
except ImportError:
    pass
from engine.graph import build_graph, initial_state, load_config
from engine.tracing import traced_invoke
from engine.memory import remember_run


def main():
    use_case = sys.argv[1] if len(sys.argv) > 1 else os.getenv("USE_CASE", "dq_qals")
    cfg = load_config(use_case)
    max_score = cfg.get("max_score", 18)
    app = build_graph(use_case, verbose=False)     # built ONCE; reused for every question
    print(f"\nUSE CASE: {cfg['name']}  [{use_case}]")
    print("Ask a question. Blank line, 'exit' or 'quit' (or Ctrl-D) to leave.\n" + "-" * 68)

    while True:
        try:
            q = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q or q.lower() in ("exit", "quit"):
            break
        try:
            final = traced_invoke(app, initial_state(q), use_case)
            remember_run(final, use_case)          # episodic capture (no-op unless MEMORY_STORE set)
            print(f"\nSCORE : {final['best_score']}/{max_score}")
            print(f"ANSWER: {final['best_answer']}\n" + "-" * 68)
        except Exception as e:                     # keep the REPL alive on a bad run (e.g. transient)
            print(f"[error] {type(e).__name__}: {e}\n" + "-" * 68)
    print("bye.")


if __name__ == "__main__":
    main()
