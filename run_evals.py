"""
Run a use case's GOLDEN SET and report pass/fail. This is your portable eval
signal -- it lives in git, not in Cortex, so it survives any migration.

    python run_evals.py                       # dq_qals
    python run_evals.py parity_hana_snowflake

Note: on the MOCK provider the answer is the same regardless of question, so this
demonstrates the HARNESS. Real signal comes when WORKER_PROVIDER is a real model.
"""
import sys, yaml
try:                                   # optional: load a local .env if python-dotenv is installed
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from engine.graph import build_graph, initial_state, USECASES
from engine.tracing import traced_invoke
from runner_env import semantic_override      # TEST/DEV-only env->pack override (kept in one place)

def run(use_case: str):
    golden = yaml.safe_load((USECASES / use_case / "evals" / "golden_set.yaml").read_text(encoding="utf-8"))
    app = build_graph(use_case, semantic_layer=semantic_override(), verbose=False)          # quiet loop for clean report
    passed = 0
    print(f"\nEVAL: {use_case}   ({len(golden)} cases)")
    print("=" * 68)
    for case in golden:
        prompt = case.get("input") or case["question"]   # domain-neutral: accept `input` OR `question`
        answer = traced_invoke(app, initial_state(prompt), use_case)["best_answer"]
        missing = [s for s in case["expect_contains"] if s.lower() not in answer.lower()]
        ok = not missing
        passed += ok
        print(f"[{'PASS' if ok else 'FAIL'}] {prompt}")
        if missing:
            print(f"        missing: {missing}")
    print("=" * 68)
    print(f"SCORE: {passed}/{len(golden)} passed\n")

if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "dq_qals")
