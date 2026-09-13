"""OPERATOR INSTRUCTIONS: composed like skills, injected into generate/refine/rubric, gradeable,
and (per-request) gated by allow_runtime_instructions. Existing packs (no instruction files) get no
block. All offline/deterministic."""
import pytest

import engine.graph as G
from engine.graph import build_graph, initial_state, load_instructions


class _Cap:
    """LLMClient double that records the prompts it is asked to complete."""
    def __init__(self, *replies):
        self.replies, self.i, self.prompts = list(replies), 0, []

    def complete(self, prompt, **k):
        self.prompts.append(prompt)
        v = self.replies[min(self.i, len(self.replies) - 1)]
        self.i += 1
        return v


class _Sql:
    def ask(self, q):
        return "D"


_CFG = {"max_score": 18, "pass_score": 18, "max_iters": 1, "eval_retries": 0,
        "max_stall": 0, "default_sql_tool": "mock"}


def test_di_instructions_reach_generate_refine_and_judge(monkeypatch):
    monkeypatch.setattr(G, "load_config", lambda uc: dict(_CFG))
    w, j = _Cap("a", "b"), _Cap("SCORE: 1/18 - low")     # low score -> one refine runs
    g = build_graph("dq_qals", llm=w, eval_llm=j, sql=_Sql(),
                    instructions="ALWAYS_CITE_XYZ", verbose=False)
    g.invoke(initial_state("q"))
    assert "OPERATOR INSTRUCTIONS" in w.prompts[0] and "ALWAYS_CITE_XYZ" in w.prompts[0]  # generate
    assert "ALWAYS_CITE_XYZ" in w.prompts[1]              # refine
    assert "ALWAYS_CITE_XYZ" in j.prompts[0]              # judge/rubric


def test_no_instructions_means_no_block(monkeypatch):
    monkeypatch.setattr(G, "load_config", lambda uc: dict(_CFG))
    w, j = _Cap("a", "b"), _Cap("SCORE: 1/18 - low")
    g = build_graph("dq_qals", llm=w, eval_llm=j, sql=_Sql(), verbose=False)  # dq_qals has no instructions/
    g.invoke(initial_state("q"))
    assert "OPERATOR INSTRUCTIONS" not in w.prompts[0]    # zero behavior change for existing packs


def test_runtime_instructions_are_gated(monkeypatch):
    off = {**_CFG, "max_iters": 0, "allow_runtime_instructions": False}
    monkeypatch.setattr(G, "load_config", lambda uc: dict(off))
    w = _Cap("a")
    build_graph("dq_qals", llm=w, eval_llm=_Cap("SCORE: 18/18 - ok"), sql=_Sql(), verbose=False) \
        .invoke(initial_state("q", instructions="RUNTIME_XYZ"))
    assert "RUNTIME_XYZ" not in w.prompts[0]              # gate off -> ignored

    on = {**_CFG, "max_iters": 0, "allow_runtime_instructions": True}
    monkeypatch.setattr(G, "load_config", lambda uc: dict(on))
    w2 = _Cap("a")
    build_graph("dq_qals", llm=w2, eval_llm=_Cap("SCORE: 18/18 - ok"), sql=_Sql(), verbose=False) \
        .invoke(initial_state("q", instructions="RUNTIME_XYZ"))
    assert "RUNTIME_XYZ" in w2.prompts[0]                 # gate on -> applied


def _all_packs():
    return sorted(p.name for p in G.USECASES.iterdir()
                  if p.is_dir() and (p / "config.yaml").exists() and not p.name.startswith(("_", ".")))


@pytest.mark.parametrize("uc", _all_packs())
def test_all_packs_instruction_block_matches_file_presence(uc):
    # M16: across EVERY existing pack, the OPERATOR INSTRUCTIONS block appears IFF the pack ships
    # instruction files -> proves zero-break (no silent injection) for the packs without them, and
    # that the feature fires for the one that has them (incident_triage).
    cfg = G.load_config(uc)
    worker, judge = _Cap("a", "a2", "a3", "a4", "a5"), _Cap("SCORE: 18/18 - ok")
    build_graph(uc, llm=worker, eval_llm=judge, sql=_Sql(), verbose=False) \
        .invoke(initial_state(cfg.get("sample_task") or "q"))
    prompts = worker.prompts + judge.prompts
    instr_dir = G.USECASES / uc / "instructions"
    has_files = instr_dir.exists() and bool(list(instr_dir.glob("*.md")))
    leaked = any("OPERATOR INSTRUCTIONS" in p for p in prompts)
    assert leaked == has_files, f"{uc}: block present={leaked} but instruction files present={has_files}"


def test_load_instructions_composes_shared_and_pack_and_excludes(tmp_path, monkeypatch):
    shared = tmp_path / "shared" / "instructions"; shared.mkdir(parents=True)
    (shared / "policy.md").write_text("SHARED_POLICY", encoding="utf-8")
    (shared / "sql_rules.md").write_text("SHARED_SQL", encoding="utf-8")
    pack = tmp_path / "uc" / "p" / "instructions"; pack.mkdir(parents=True)
    (pack / "domain.md").write_text("PACK_DOMAIN", encoding="utf-8")
    monkeypatch.setattr(G, "SHARED", tmp_path / "shared")
    monkeypatch.setattr(G, "USECASES", tmp_path / "uc")
    out = load_instructions("p")
    assert "SHARED_POLICY" in out and "SHARED_SQL" in out and "PACK_DOMAIN" in out
    out2 = load_instructions("p", exclude=["sql_rules"])  # drop a shared file by stem
    assert "SHARED_SQL" not in out2 and "SHARED_POLICY" in out2 and "PACK_DOMAIN" in out2
