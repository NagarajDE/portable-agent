"""
Engine-integration + regression tests for the generic tool layer.

Proves the preserved contracts still hold (a legacy pack takes the identical single-SQL path;
scoring is unchanged) AND the new behavior works (a pack that declares `tools:` runs them
deterministically and feeds their observations into generate). Also loads the real
usecases/api_assistant pack end-to-end on the mock provider (no creds, no network).
"""
import pytest

from engine import graph as _graph_mod
from engine.graph import (build_graph, initial_state, load_config, load_exemplars,
                          load_tools, _format_exemplar, load_semantic_layer)


# --- minimal scripted doubles (local; mirror test_graph.py) ---------------
class _Seq:
    def __init__(self, *replies):
        self.replies, self.i = list(replies), 0

    def complete(self, prompt, **k):
        v = self.replies[min(self.i, len(self.replies) - 1)]
        self.i += 1
        return v


class _CapturingWorker:
    def __init__(self, *replies):
        self.replies, self.i, self.prompts = list(replies), 0, []

    def complete(self, prompt, **k):
        self.prompts.append(prompt)
        v = self.replies[min(self.i, len(self.replies) - 1)]
        self.i += 1
        return v


class _SpySql:
    def __init__(self):
        self.calls = []

    def ask(self, q):
        self.calls.append(q)
        return "SPY_SQL_DATA"


# --- CONTRACT: a legacy pack (no `tools:`) takes the identical SQL path -----
def test_legacy_pack_uses_injected_sql_path(monkeypatch):
    monkeypatch.setattr(_graph_mod, "load_config", lambda uc: {
        "max_score": 18, "pass_score": 18, "max_iters": 0, "eval_retries": 0,
        "default_sql_tool": "mock"})                    # NO tools key -> legacy path
    spy = _SpySql()
    worker = _CapturingWorker("answer")
    g = build_graph("dq_qals", llm=worker, sql=spy, eval_llm=_Seq("SCORE: 18/18 - ok"), verbose=False)
    g.invoke(initial_state("legacy question"))
    assert spy.calls == ["legacy question"]             # SQL tool was used (unchanged path)
    assert "SPY_SQL_DATA" in worker.prompts[0]          # its data flowed into generate


# --- NEW: a pack that declares `tools:` runs them and skips the SQL path ----
def _tools_cfg(**over):
    cfg = {"max_score": 18, "pass_score": 18, "max_iters": 0, "eval_retries": 0,
           "default_sql_tool": "mock",
           "tools": [
               {"type": "mock", "name": "catalog", "input": {"query": "${task}"},
                "params": {"output": "CATALOG_ROWS"}},
               {"type": "mock", "name": "tickets", "input": {"q": "${task}"},
                "params": {"output": "TICKET_ROWS"}},
           ]}
    cfg.update(over)
    return cfg


def test_tools_pack_feeds_observations_and_skips_sql(monkeypatch):
    monkeypatch.setattr(_graph_mod, "load_config", lambda uc: _tools_cfg())
    spy = _SpySql()
    worker = _CapturingWorker("answer")
    g = build_graph("api_assistant", llm=worker, sql=spy, eval_llm=_Seq("SCORE: 18/18 - ok"),
                    verbose=False)
    g.invoke(initial_state("multi tool question"))
    gen = worker.prompts[0]
    assert "[catalog]" in gen and "CATALOG_ROWS" in gen
    assert "[tickets]" in gen and "TICKET_ROWS" in gen  # BOTH tools' observations present
    assert spy.calls == []                              # SQL path NOT taken when tools declared


def test_tools_pack_substitutes_task(monkeypatch):
    monkeypatch.setattr(_graph_mod, "load_config", lambda uc: {
        "max_score": 18, "pass_score": 18, "max_iters": 0, "eval_retries": 0,
        "tools": [{"type": "mock", "name": "echo", "input": {"query": "${task}"}}]})  # echoes input
    worker = _CapturingWorker("answer")
    g = build_graph("api_assistant", llm=worker, eval_llm=_Seq("SCORE: 18/18 - ok"), verbose=False)
    g.invoke(initial_state("UNIQUE-TOKEN-42"))
    gen = worker.prompts[0]
    # L3: assert the token reached the TOOL OBSERVATION (the echoed input), not just the QUESTION line
    assert "input={'query': 'UNIQUE-TOKEN-42'}" in gen


def test_toolless_pack_uses_neither_tools_nor_sql(monkeypatch):
    # M17: explicit `tools: []` is a deliberately toolless agent -> no tool output AND no SQL
    monkeypatch.setattr(_graph_mod, "load_config", lambda uc: {
        "max_score": 18, "pass_score": 18, "max_iters": 0, "eval_retries": 0, "tools": []})
    spy = _SpySql()
    worker = _CapturingWorker("answer")
    g = build_graph("api_assistant", llm=worker, sql=spy, eval_llm=_Seq("SCORE: 18/18 - ok"),
                    verbose=False)
    g.invoke(initial_state("q"))
    assert spy.calls == []                                     # SQL path NOT taken
    assert "No tool observations." in worker.prompts[0]        # and no tool output


def test_tools_run_once_in_generate_not_per_refine(monkeypatch):
    # L4: tools are gathered ONCE (in generate); refine must NOT re-run them
    calls = {"n": 0}
    real = _graph_mod.gather_context

    def counting(task, loaded, run_id="-"):
        calls["n"] += 1
        return real(task, loaded, run_id)

    monkeypatch.setattr(_graph_mod, "gather_context", counting)
    monkeypatch.setattr(_graph_mod, "load_config", lambda uc: _tools_cfg(max_iters=2))
    worker = _Seq("A0", "A1", "A2")
    judge = _Seq("SCORE: 12/18 - low")                        # never passes -> refines to max_iters
    g = build_graph("api_assistant", llm=worker, eval_llm=judge, verbose=False)
    f = g.invoke(initial_state("q"))
    assert f["iterations"] == 2 and calls["n"] == 1           # 2 refines happened, tools gathered once


# --- CONTRACT: scoring/loop mechanics unchanged when tools are present ------
def test_scoring_unchanged_with_tools_present(monkeypatch):
    monkeypatch.setattr(_graph_mod, "load_config", lambda uc: _tools_cfg(max_iters=1))
    worker = _Seq("A0", "A1")
    judge = _Seq("SCORE: 16/18 - ok", "SCORE: 10/18 - worse")
    g = build_graph("api_assistant", llm=worker, eval_llm=judge, verbose=False)
    f = g.invoke(initial_state("q"))
    assert f["best_score"] == 16 and f["best_answer"] == "A0"   # champion preserved, same as legacy


# --- JIRA-style pack: runs to completion on the mock provider, no creds -----
def test_jira_style_pack_runs_with_mocks(monkeypatch):
    # A generic ticket-reader shape: a mock "tickets" tool + the http tool in mock mode.
    monkeypatch.setenv("WORKER_PROVIDER", "mock")             # L2: pin the mock provider explicitly
    monkeypatch.setenv("EVAL_PROVIDER", "mock")
    monkeypatch.setattr(_graph_mod, "load_config", lambda uc: {
        "max_score": 18, "pass_score": 15, "max_iters": 4, "eval_retries": 0,
        "tools": [
            {"type": "mock", "name": "tickets", "input": {"jql": "${task}"},
             "params": {"output": "OPS-1 open; OPS-2 closed"}},
            {"type": "http", "name": "board", "input": {"path": "/board/ops"},
             "params": {"mode": "mock", "mock_responses": {"/board/ops": '{"open": 1}'}}},
        ]})
    g = build_graph("api_assistant", verbose=False)     # real mock worker + judge (no injection)
    f = g.invoke(initial_state("what OPS tickets are open?"))
    assert f["best_score"] >= 15                        # loop converged
    assert f["best_answer"]                             # produced a non-empty answer


# --- real pack files load + run (integration with usecases/api_assistant) ---
def test_api_assistant_pack_config_loads_two_tools():
    cfg = load_config("api_assistant")
    loaded = load_tools("api_assistant", cfg)
    assert [lt.label for lt in loaded] == ["catalog", "health"]
    assert all(lt.tool.spec.read_only for lt in loaded)


def test_api_assistant_pack_runs_end_to_end(monkeypatch):
    monkeypatch.setenv("WORKER_PROVIDER", "mock")             # L2: pin the mock provider explicitly
    monkeypatch.setenv("EVAL_PROVIDER", "mock")
    g = build_graph("api_assistant", verbose=False)     # mock provider by default (no creds)
    f = g.invoke(initial_state("Is the orders service healthy, and who owns it?"))
    assert f["best_score"] >= 15
    # converges to the pack's canned refined answer
    assert "orders-api" in f["best_answer"] and "team-fulfillment" in f["best_answer"]


# --- CONTRACT: both exemplar formats load (legacy Q->SQL + domain-neutral) --
def test_legacy_exemplars_render_q_sql():
    text = load_exemplars("dq_qals")
    assert text.startswith("Q:") and "SQL:" in text


def test_domain_neutral_exemplars_render_input_output():
    text = load_exemplars("api_assistant")
    assert "Input:" in text and "Output:" in text and "SQL:" not in text


# --- #3: explicit `kind:` is a real discriminator, not filename/key guessing ----------------
def test_exemplar_kind_text_to_sql_renders_q_sql():
    out = _format_exemplar({"kind": "text_to_sql", "question": "Q1", "sql": "SELECT 1"})
    assert out == "Q: Q1\nSQL: SELECT 1"


def test_exemplar_kind_input_output_renders_input_output():
    out = _format_exemplar({"kind": "input_output", "input": "hi", "output": "bye"})
    assert out == "Input: hi\nOutput: bye"


def test_exemplar_kind_overrides_key_sniffing():
    # a `sql` key is PRESENT, but kind: input_output must win (explicit discriminator, not a guess)
    out = _format_exemplar({"kind": "input_output", "sql": "SELECT 1",
                            "input": "hi", "output": "bye"})
    assert out == "Input: hi\nOutput: bye"


def test_exemplar_without_kind_falls_back_to_key_sniffing():
    assert _format_exemplar({"question": "Q1", "sql": "SELECT 1"}) == "Q: Q1\nSQL: SELECT 1"
    assert _format_exemplar({"input": "hi", "output": "bye"}) == "Input: hi\nOutput: bye"


# --- #7: a pack can opt OUT of a shared skill (non-SQL packs drop sql_safety) ----------------
def test_exclude_shared_skills_drops_named_shared_skill():
    from engine.graph import load_skills
    kept = load_skills("dq_qals")                       # no exclusion -> sql_safety present
    dropped = load_skills("dq_qals", ["sql_safety"])    # excluded -> sql_safety gone
    assert "SELECT queries only" in kept
    assert "SELECT queries only" not in dropped
    assert "most important number" in dropped           # a DIFFERENT shared skill (reporting) survives


# --- semantic layer: pack-only loader; file PRESENCE marks an AI+BI pack (never reads env) ---
def test_load_semantic_layer_absent_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(_graph_mod, "USECASES", tmp_path)
    (tmp_path / "nolayer").mkdir()                       # a pack dir with NO semantic_layer.yaml
    assert load_semantic_layer("nolayer") is None        # engine never looks -> non-AI+BI pack


def test_load_semantic_layer_present_parses_block(tmp_path, monkeypatch):
    monkeypatch.setattr(_graph_mod, "USECASES", tmp_path)
    pack = tmp_path / "aibi"
    pack.mkdir()
    (pack / "semantic_layer.yaml").write_text("snowflake:\n  view: DB.S.V\n", encoding="utf-8")
    assert load_semantic_layer("aibi") == {"snowflake": {"view": "DB.S.V"}}


def test_load_semantic_layer_empty_file_returns_dict(tmp_path, monkeypatch):
    monkeypatch.setattr(_graph_mod, "USECASES", tmp_path)
    pack = tmp_path / "empty"
    pack.mkdir()
    (pack / "semantic_layer.yaml").write_text("", encoding="utf-8")     # present but empty
    assert load_semantic_layer("empty") == {}            # {} -> get_sql_tool guard-rails it clearly
