"""
Run a use case's GOLDEN SET and report pass/fail. This is your portable eval
signal -- it lives in git, not in Cortex, so it survives any migration.

    python run_evals.py                       # dq_qals
    python run_evals.py parity_hana_snowflake

Note: on the MOCK provider the answer is the same regardless of question, so this
demonstrates the HARNESS. Real signal comes when WORKER_PROVIDER is a real model.
"""
import sys, yaml
from engine.graph import build_graph, initial_state, USECASES
from engine.tracing import traced_invoke

def run(use_case: str):
    golden = yaml.safe_load((USECASES / use_case / "evals" / "golden_set.yaml").read_text())
    app = build_graph(use_case, verbose=False)          # quiet loop for clean report
    passed = 0
    print(f"\nEVAL: {use_case}   ({len(golden)} cases)")
    print("=" * 68)
    for case in golden:
        answer = traced_invoke(app, initial_state(case["question"]), use_case)["best_answer"]
        missing = [s for s in case["expect_contains"] if s.lower() not in answer.lower()]
        ok = not missing
        passed += ok
        print(f"[{'PASS' if ok else 'FAIL'}] {case['question']}")
        if missing:
            print(f"        missing: {missing}")
    print("=" * 68)
    print(f"SCORE: {passed}/{len(golden)} passed\n")

if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "dq_qals")
