"""GROUNDEDNESS: a blank retrieval must never silently pass as a good score.

The scary bug this guards: a run whose SQL tool returned NO rows (or no SQL) was scored 15/18 and
"passed", because the rubric only checks the answer against the evidence -- and an honest "no data"
answer is perfectly consistent with no-data evidence. Groundedness is DETERMINISTICALLY knowable at
the tool boundary, so we detect it there (NO_DATA sentinel) rather than trusting the judge to notice.

A blank is NOT proof the data is missing -- it may be a misread question or a wrong/empty query. So
the paths that fire ONCE (single SQL call, deterministic tool sweep) reformulate the question and
re-retrieve up to `max_data_retries` times; only if it stays blank is the run escalated as
`status="no_data"` (judge skipped, never a pass).

The rewrite is itself guarded, because "retrieved rows" is not the same as "answered the question":
a rewrite that REFUSES, or that quietly swaps the subject for one the data does cover, is discarded
so the run escalates rather than earning a passing score for answering a question nobody asked.
All offline: worker/judge/SQL are test doubles; the live Cortex call is not exercised here.
"""
import engine.graph as G
from engine.graph import build_graph, initial_state, _keeps_subject, _keeps_pinned, _content_words
from engine.sql_tool import no_data, is_no_data, data_hint, looks_all_zero, CortexAnalystTool


class _Cap:
    """Worker double: records the prompts it sees, returns scripted replies in order."""
    def __init__(self, *replies):
        self.replies, self.i, self.prompts = list(replies), 0, []

    def complete(self, prompt, **k):
        self.prompts.append(prompt)
        v = self.replies[min(self.i, len(self.replies) - 1)]
        self.i += 1
        return v


class _JudgeSpy:
    """Judge double that COUNTS calls -- so a test can prove the judge never ran on a no-data run."""
    def __init__(self, reply="SCORE: 18/18 - ok"):
        self.reply, self.calls = reply, 0

    def complete(self, prompt, **k):
        self.calls += 1
        return self.reply


class _Sql:
    """SQLTool double: returns each scripted result once, repeating the last (like the worker)."""
    def __init__(self, *results):
        self.results, self.i, self.calls = list(results), 0, []

    def ask(self, q):
        self.calls.append(q)
        r = self.results[min(self.i, len(self.results) - 1)]
        self.i += 1
        return r


_CFG = {"max_score": 18, "pass_score": 18, "max_iters": 0, "eval_retries": 0,
        "max_stall": 0, "default_sql_tool": "mock", "loop": True}


# --- the NO_DATA sentinel round-trips -------------------------------------------------------------
def test_no_data_sentinel_roundtrip():
    assert is_no_data(no_data("why nothing came back"))
    assert data_hint(no_data("why nothing came back")) == "why nothing came back"
    assert is_no_data(no_data()) and data_hint(no_data()) == ""
    assert not is_no_data("col | val\nA | 1")            # real rows are not no-data
    assert data_hint("col | val") == ""
    assert not is_no_data(None) and not is_no_data(123)  # non-str never crashes / never no-data


# --- graph behavior: escalate, recover, immediate-escalate, and the untouched happy path ---------
def test_blank_persists_escalates_without_running_the_judge(monkeypatch):
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG, "max_data_retries": 1})
    w = _Cap("reformulated question", "Honest: I can't answer, no data was returned.")
    judge = _JudgeSpy()
    sql = _Sql(no_data("Analyst: the semantic model defines no relationships"))  # always blank
    final = build_graph("dq_qals", llm=w, eval_llm=judge, sql=sql, verbose=False) \
        .invoke(initial_state("q"))
    assert final["grounded"] is False and final["status"] == "no_data"
    assert final["best_answer"].startswith("Honest:")      # honest answer preserved for the human
    assert final["best_score"] == -1                       # NEVER scored -> can't be read as a pass
    assert judge.calls == 0                                 # the judge did not run on a no-data run
    assert final["data_retries"] == 1
    assert any("no relationships" in p for p in w.prompts)  # the tool hint fed the reformulation
    assert sql.calls[0] == "q" and sql.calls[1] == "reformulated question"  # re-queried the new Q


def test_blank_then_recovers_runs_judge_normally(monkeypatch):
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG, "max_data_retries": 2})
    w = _Cap("reformulated question", "Answer grounded in the recovered rows.")
    judge = _JudgeSpy("SCORE: 18/18 - grounded")
    sql = _Sql(no_data("misread the question"), "region | total\nEMEA | 42")  # blank, then real data
    final = build_graph("dq_qals", llm=w, eval_llm=judge, sql=sql, verbose=False) \
        .invoke(initial_state("q"))
    assert final["grounded"] is True and final["status"] == ""
    assert final["best_score"] == 18 and judge.calls == 1  # recovered -> scored the real answer
    assert final["data_retries"] == 1                      # one retry was enough


def test_zero_retries_escalates_immediately(monkeypatch):
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG, "max_data_retries": 0})
    w = _Cap("Honest no-data answer.")                      # ONLY the generate call, no reformulation
    judge = _JudgeSpy()
    sql = _Sql(no_data("gap"))
    final = build_graph("dq_qals", llm=w, eval_llm=judge, sql=sql, verbose=False) \
        .invoke(initial_state("q"))
    assert final["status"] == "no_data" and judge.calls == 0
    assert len(w.prompts) == 1 and final["data_retries"] == 0   # never asked to reformulate
    assert len(sql.calls) == 1                              # queried once, no re-query


def test_grounded_run_is_unchanged(monkeypatch):
    # Regression: a normal run with real data never reformulates, is grounded, and scores as before.
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG})
    w = _Cap("answer")
    judge = _JudgeSpy("SCORE: 18/18 - ok")
    sql = _Sql("material | qty\nX | 10")
    final = build_graph("dq_qals", llm=w, eval_llm=judge, sql=sql, verbose=False) \
        .invoke(initial_state("q"))
    assert final["grounded"] is True and final["status"] == "" and final["best_score"] == 18
    assert final["data_retries"] == 0 and len(w.prompts) == 1   # no reformulation on a grounded run


# --- the sentinel originates at the real tool boundary (CortexAnalystTool) -----------------------
def _stub_auth(monkeypatch):
    import engine.llm_client as L
    monkeypatch.setattr(L, "snowflake_rest_base", lambda: "https://acct.snowflakecomputing.com")
    monkeypatch.setattr(L, "snowflake_bearer_headers", lambda: {"Authorization": "Bearer x"})


def _fake_post(payload):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return payload
    return lambda *a, **k: _Resp()


def test_cortex_no_sql_becomes_no_data_with_hint(monkeypatch):
    import engine.llm_client as L
    import requests
    monkeypatch.setattr(L, "snowpark_session", lambda: object())
    _stub_auth(monkeypatch)
    monkeypatch.setattr(requests, "post", _fake_post(
        {"message": {"content": [{"type": "text",
                                  "text": "The semantic model defines no relationships between the tables."}]}}))
    out = CortexAnalystTool({"view": "DB.S.V"}).ask("supplier totals?")
    assert is_no_data(out) and "no relationships" in data_hint(out)   # hint carried for reformulation


def test_cortex_zero_rows_becomes_no_data(monkeypatch):
    import engine.llm_client as L
    import requests

    class _DF:
        def limit(self, n):
            return self

        def collect(self, **k):
            return []                                       # query ran, matched nothing

    monkeypatch.setattr(L, "snowpark_session", lambda: type("S", (), {"sql": lambda self, q: _DF()})())
    _stub_auth(monkeypatch)
    monkeypatch.setattr(requests, "post", _fake_post(
        {"message": {"content": [{"type": "sql", "statement": "SELECT 1 WHERE 1=0"}]}}))
    assert is_no_data(CortexAnalystTool({"view": "DB.S.V"}).ask("q"))


# --- the reformulation must ask the USER's question, not one the data happens to answer -----------
def test_refused_reformulation_escalates_without_requerying(monkeypatch):
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG, "max_data_retries": 2})
    w = _Cap("NO_REFORMULATION", "Honest: employee data is not in this model.")
    judge = _JudgeSpy()
    sql = _Sql(no_data("this model covers inventory only"))
    final = build_graph("dq_qals", llm=w, eval_llm=judge, sql=sql, verbose=False) \
        .invoke(initial_state("What is the average employee salary by department?"))
    assert final["status"] == "out_of_scope" and final["grounded"] is False   # WRONG QUESTION, not blank
    assert final["best_score"] == -1 and judge.calls == 0
    assert len(sql.calls) == 1               # a refusal is FINAL: never re-queried, budget not burned
    assert final["data_retries"] == 1


def test_off_subject_reformulation_is_discarded(monkeypatch):
    # The exact LIVE failure this guards: the rewrite silently swapped the subject for one the view
    # DOES cover, rows came back, the run looked grounded, and the honest answer scored 16/18.
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG, "max_data_retries": 1})
    w = _Cap("What is the average inventory value per plant?",        # drifted off the asked subject
             "Honest: this data has no employee or salary fields.")
    judge = _JudgeSpy()
    sql = _Sql(no_data("no employee fields"), "plant | avg\n3300 | 12.5")   # WOULD have returned rows
    final = build_graph("dq_qals", llm=w, eval_llm=judge, sql=sql, verbose=False) \
        .invoke(initial_state("What is the average employee salary by department?"))
    assert final["status"] == "out_of_scope" and final["grounded"] is False
    assert final["best_score"] == -1 and judge.calls == 0
    assert len(sql.calls) == 1                # the drifted question was NEVER executed


def test_narrowing_reformulation_is_accepted(monkeypatch):
    # The guard must not block a rewrite that PRESERVES the core measure. Here the measure noun
    # 'contract value' survives, so the drop of the 'top 10' rank is accepted and the run recovers.
    # KNOWN RESIDUAL (F2): dropping the 'active' filter is a broadening the measure check does NOT
    # catch (any-overlap on the measure, so 'contract' surviving is enough) -- the prompt-level
    # NO_REFORMULATION refusal is the guard for that, not this deterministic check.
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG, "max_data_retries": 1})
    w = _Cap("What is the total contract value by supplier?", "Grounded answer from the rows.")
    judge = _JudgeSpy("SCORE: 18/18 - grounded")
    sql = _Sql(no_data("cross-table join unsupported"), "supplier | total\nIBM | 52.9")
    final = build_graph("dq_qals", llm=w, eval_llm=judge, sql=sql, verbose=False) \
        .invoke(initial_state("Total active contract value by supplier for the top 10 suppliers?"))
    assert final["grounded"] is True and final["best_score"] == 18 and judge.calls == 1
    assert sql.calls[1] == "What is the total contract value by supplier?"


def test_keeps_subject_helper():
    assert not _keeps_subject("average employee salary by department",
                              "average inventory value per plant")
    assert _keeps_subject("total active contract value by supplier",
                          "total contract value by supplier")
    assert _keeps_subject("how many widgets", "count of widgets in stock")
    assert _keeps_subject("how many widgets are in stock?", "count of widget inventory")  # plural fold
    assert _keeps_subject("what is the total?", "what is the grand total?")   # nothing to check -> open
    # short (<=3-letter) subject nouns are visible (floor is 3, not 4): a measure swap is caught.
    assert not _keeps_subject("total tax", "total fees")
    # F3: the check is on the MEASURE (part before 'by/per/...'), NOT the whole question, so a swap
    # that keeps only the DIMENSION is now caught -- 'department'/'region' surviving is not enough.
    assert not _keeps_subject("total tax by region", "total fees by region")
    assert not _keeps_subject("employee salary by department", "inventory value by department")
    # -y/-ies singular<->plural now folds to a common stem, so a legit re-pluralization is NOT drift
    assert _keeps_subject("average salary by team", "salaries by team")
    assert _keeps_subject("spend by category", "spend by categories")
    # F5: short ALL-CAPS acronyms are visible now (were invisible under the >=3-letter floor)
    assert "it" in _content_words("IT spend") and "hr" in _content_words("HR headcount")
    # a PINNED identifier may not be silently dropped (that would widen the population)
    assert not _keeps_pinned("on-hand balance for plant ZZ999", "on-hand balance by plant")
    assert _keeps_pinned("on-hand balance for plant ZZ999", "current balance for plant zz999")
    assert _keeps_pinned("top 10 suppliers by value", "suppliers by value")   # a rank is not a filter
    # F1: EVERY pinned id must survive (subset), so dropping one of several is caught, not masked
    assert not _keeps_pinned("compare PO123 and PO456", "show PO123 only")
    assert _keeps_pinned("compare PO123 and PO456", "PO123 versus PO456 by month")
    # documented tradeoff (SAFE direction): a re-expressed period code reads as dropped -> escalates
    assert not _keeps_pinned("FY26 spend by vendor", "fiscal 2026 spend by vendor")


def test_dropped_identifier_reports_no_data_not_out_of_scope(monkeypatch):
    # "plant ZZ999" IS in scope -- only that code matched nothing. Widening it would answer about a
    # DIFFERENT population, so the run escalates; but the human must be told "no rows for that code",
    # not "wrong question". Seen live: this exact question reported out_of_scope before the split.
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG, "max_data_retries": 1})
    w = _Cap("What is the on-hand balance by plant?", "No records exist for that plant code.")
    judge = _JudgeSpy()
    sql = _Sql(no_data("the query matched nothing"), "plant | qty\n3300 | 5")
    final = build_graph("dq_qals", llm=w, eval_llm=judge, sql=sql, verbose=False) \
        .invoke(initial_state("What is the on-hand inventory balance for plant ZZ999?"))
    assert final["status"] == "no_data" and final["grounded"] is False
    assert judge.calls == 0 and len(sql.calls) == 1     # the widened question was never executed


# --- a blank TOOLS retrieval is the same failure as a blank SQL retrieval -------------------------
_TOOL_CFG = {**_CFG, "tools": [{"type": "mock", "name": "probe", "input": {"query": "${task}"},
                                "params": {"fail": "backend down"}}]}


def test_blank_tools_retrieval_retries_then_escalates(monkeypatch):
    monkeypatch.setattr(G, "load_config", lambda uc: {**_TOOL_CFG, "max_data_retries": 1})
    w = _Cap("inventory balance by plant", "Honest: the tools returned no evidence.")
    judge = _JudgeSpy()
    final = build_graph("dq_qals", llm=w, eval_llm=judge, verbose=False) \
        .invoke(initial_state("inventory balance by location"))
    assert final["status"] == "no_data" and final["grounded"] is False
    assert final["best_score"] == -1 and judge.calls == 0
    assert final["data_retries"] == 1        # the deterministic tools path retries like SQL now


# --- skill-informed query framing (frame_query) --------------------------------------------------
# With frame_query ON the worker gets an EXTRA call FIRST (the rewrite), so replies are ordered
# [framed_question, answer] and sql.calls[0] is what actually hit the tool.
def test_framing_rewrites_question_before_retrieval(monkeypatch):
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG, "frame_query": True})
    w = _Cap("total InvoiceUSD for the IT Software category", "Grounded answer from the rows.")
    judge = _JudgeSpy("SCORE: 18/18 - ok")
    sql = _Sql("category | total\nIT Software | 99")
    final = build_graph("dq_qals", llm=w, eval_llm=judge, sql=sql, verbose=False) \
        .invoke(initial_state("average invoice for the IT Software category"))
    assert sql.calls[0] == "total InvoiceUSD for the IT Software category"  # the FRAMED q hit the tool
    assert len(w.prompts) == 2                              # one framing call + one generate call
    assert final["grounded"] is True and final["best_score"] == 18


def test_framing_discards_drift_uses_raw(monkeypatch):
    # A rewrite that SUBSTITUTES the subject must be discarded (same drift guards as the retry) and the
    # RAW question sent instead -- framing can only add precision, never change what is asked.
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG, "frame_query": True})
    w = _Cap("total employee headcount by department", "Grounded answer.")
    judge = _JudgeSpy("SCORE: 18/18 - ok")
    sql = _Sql("supplier | total\nIBM | 5")
    final = build_graph("dq_qals", llm=w, eval_llm=judge, sql=sql, verbose=False) \
        .invoke(initial_state("total contract value by supplier"))
    assert sql.calls[0] == "total contract value by supplier"   # drift discarded -> raw question used


def test_framing_empty_reply_falls_back_to_raw(monkeypatch):
    # An empty framing reply is fail-safe: use the raw question (never escalate just because framing
    # produced nothing).
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG, "frame_query": True})
    w = _Cap("", "Grounded answer.")
    judge = _JudgeSpy("SCORE: 18/18 - ok")
    sql = _Sql("region | total\nEMEA | 42")
    final = build_graph("dq_qals", llm=w, eval_llm=judge, sql=sql, verbose=False) \
        .invoke(initial_state("average invoice for the IT Software category"))
    assert sql.calls[0] == "average invoice for the IT Software category"   # raw used, not the empty q


def test_framing_off_sends_raw_question(monkeypatch):
    # frame_query defaults OFF -> no framing call, raw question sent -> byte-identical to legacy behavior.
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG})
    w = _Cap("Grounded answer from the rows.")               # ONLY the generate call, no framing call
    judge = _JudgeSpy("SCORE: 18/18 - ok")
    sql = _Sql("region | total\nEMEA | 42")
    final = build_graph("dq_qals", llm=w, eval_llm=judge, sql=sql, verbose=False) \
        .invoke(initial_state("average invoice for the IT Software category"))
    assert sql.calls[0] == "average invoice for the IT Software category"   # raw, unframed
    assert len(w.prompts) == 1                               # no extra framing call happened


# --- frame_skills: framing sees a focused SUBSET; the answer step still sees ALL skills --------------
def test_load_named_skills_basics():
    full = G.load_skills("dq_qals")
    one = G.load_named_skills("dq_qals", ["duplicate_detection"])
    assert one != "None." and one != full and len(one) < len(full)   # a real, strict subset
    assert G.load_named_skills("dq_qals", ["duplicate_detection.md"]) == one   # .md optional
    assert G.load_named_skills("dq_qals", []) == "None."             # empty -> nothing
    assert G.load_named_skills("dq_qals", ["nope_missing"]) == "None."  # unknown -> nothing


def test_frame_skills_subsets_framing_prompt_only(monkeypatch):
    # framing prompt carries ONLY the listed skill; the generate prompt carries the FULL skill set.
    monkeypatch.setattr(G, "load_config",
                        lambda uc: {**_CFG, "frame_query": True, "frame_skills": ["duplicate_detection"]})
    sub = G.load_named_skills("dq_qals", ["duplicate_detection"])
    full = G.load_skills("dq_qals")
    w = _Cap("framed question", "Grounded answer.")          # [0]=framing call, [1]=generate call
    sql = _Sql("status | n\nOPEN | 3")
    build_graph("dq_qals", llm=w, eval_llm=_JudgeSpy("SCORE: 18/18 - ok"), sql=sql, verbose=False) \
        .invoke(initial_state("how many duplicate lots by status"))
    assert sub in w.prompts[0] and full not in w.prompts[0]  # framing got the subset, NOT the full set
    assert full in w.prompts[1]                              # generate still got the full skill set


def test_framing_runs_before_the_agentic_loop(monkeypatch):
    # frame_query now applies to the AGENTIC path too: the question is framed ONCE before the ReAct loop
    # (was previously skipped for tool_mode: agentic).
    cfg = {**_CFG, "frame_query": True, "tool_mode": "agentic", "max_tool_steps": 1,
           "tools": [{"type": "mock", "name": "probe",
                      "params": {"description": "probe", "output": "SVC | STATUS\norders | ok"}}]}
    monkeypatch.setattr(G, "load_config", lambda uc: cfg)
    w = _Cap("framed question", "final answer")              # [0] MUST be the framing call
    build_graph("incident_triage", llm=w, eval_llm=_JudgeSpy("SCORE: 18/18 - ok"), verbose=False) \
        .invoke(initial_state("why is orders-api failing"))
    assert "remove column ambiguity" in w.prompts[0]         # framing ran FIRST, before the agentic loop


# --- recover_value_dims: on an EMPTY result, look up real values and re-ask with the exact spelling -------
class _SqlDisc(_Sql):
    """SQL double that ALSO exposes the OPTIONAL value-lookup capability (distinct_values), recording how it
    was called. A plain _Sql lacks the method -- that is the mock/off-Cortex case (the lookup no-ops)."""
    def __init__(self, *results, values=None, boom=False):
        super().__init__(*results)
        self._values, self._boom, self.discover_calls = (values or {}), boom, []

    def distinct_values(self, dim_paths, limit=50):
        self.discover_calls.append((list(dim_paths), limit))
        if self._boom:
            raise RuntimeError("value lookup blew up")
        return self._values


def test_recovery_looks_up_values_on_empty_and_feeds_reformulate(monkeypatch):
    # First query comes back EMPTY -> the engine looks up the real values and hands them to the REFORMULATE
    # step (worker call [0], framing is off), which re-asks with the exact spelling; the retry then recovers.
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG, "max_data_retries": 1,
                        "recover_value_dims": ["BLV.BUSINESS_STATUS"]})
    w = _Cap("contracts where BUSINESS_STATUS = 'Executed'", "Grounded answer from the rows.")
    sql = _SqlDisc(no_data("no rows for status = 'active'"), "status | n\nExecuted | 3",
                   values={"BLV.BUSINESS_STATUS": ["Executed", "Approved", "Expired"]})
    final = build_graph("dq_qals", llm=w, eval_llm=_JudgeSpy("SCORE: 18/18 - ok"), sql=sql, verbose=False) \
        .invoke(initial_state("show me active contracts"))
    assert "Executed, Approved, Expired" in w.prompts[0]      # real values reached the REFORMULATE prompt
    assert "Executed, Approved, Expired" not in w.prompts[1]  # NOT the answer prompt
    assert sql.discover_calls == [(["BLV.BUSINESS_STATUS"], 50)]   # looked up once, default cap
    assert final["grounded"] is True and final["best_score"] == 18


def test_recovery_not_triggered_when_first_query_returns_rows(monkeypatch):
    # The efficiency win: a question that returns rows on the first try pays NOTHING -- no value lookup.
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG, "recover_value_dims": ["T.STATUS"]})
    w = _Cap("Grounded answer.")                              # only the generate call
    sql = _SqlDisc("status | n\nOPEN | 3", values={"T.STATUS": ["OPEN", "CLOSED"]})
    final = build_graph("dq_qals", llm=w, eval_llm=_JudgeSpy("SCORE: 18/18 - ok"), sql=sql, verbose=False) \
        .invoke(initial_state("how many open lots"))
    assert sql.discover_calls == []                           # first query had rows -> no lookup, no waste
    assert final["grounded"] is True


def test_recovery_noop_when_tool_lacks_capability(monkeypatch):
    # Tool has no distinct_values (MOCK / off-Cortex): the empty-result retry degrades to a plain reword,
    # no crash, and still recovers.
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG, "max_data_retries": 1,
                        "recover_value_dims": ["T.STATUS"]})
    w = _Cap("count of open lots", "Grounded answer.")        # reword keeps the subject
    sql = _Sql(no_data("nothing"), "status | n\nOPEN | 3")   # plain double: no distinct_values method
    final = build_graph("dq_qals", llm=w, eval_llm=_JudgeSpy("SCORE: 18/18 - ok"), sql=sql, verbose=False) \
        .invoke(initial_state("how many open lots"))
    assert final["grounded"] is True                         # recovered via plain reword, no crash


def test_recovery_error_falls_back_to_plain_reword(monkeypatch):
    # A value lookup that RAISES is swallowed (best-effort): the retry rewords without live values and the
    # run still recovers -- recovery can never create an escalation or a 500.
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG, "max_data_retries": 1,
                        "recover_value_dims": ["T.STATUS"]})
    w = _Cap("count of open lots", "Grounded answer.")
    sql = _SqlDisc(no_data("nothing"), "status | n\nOPEN | 3", boom=True)   # lookup raises
    final = build_graph("dq_qals", llm=w, eval_llm=_JudgeSpy("SCORE: 18/18 - ok"), sql=sql, verbose=False) \
        .invoke(initial_state("how many open lots"))
    assert len(sql.discover_calls) == 1                      # it was attempted...
    assert final["grounded"] is True                         # ...error swallowed, reword recovered


def test_recover_value_dims_must_be_a_list(monkeypatch):
    # Misconfiguration is caught at build time: a non-list recover_value_dims is a hard error.
    import pytest
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG, "recover_value_dims": "T.STATUS"})
    with pytest.raises(ValueError):
        build_graph("dq_qals", llm=_Cap("x", "y"), eval_llm=_JudgeSpy(), sql=_Sql("a | b\n1 | 2"), verbose=False)


def test_toolless_pack_is_not_treated_as_blank(monkeypatch):
    # `tools: []` is a DELIBERATELY toolless agent (answers from skills alone): it has no retrieval,
    # so there is no blank retrieval to escalate.
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG, "tools": []})
    w = _Cap("answer from skills alone")
    judge = _JudgeSpy("SCORE: 18/18 - ok")
    final = build_graph("dq_qals", llm=w, eval_llm=judge, verbose=False).invoke(initial_state("q"))
    assert final["grounded"] is True and final["status"] == "" and final["best_score"] == 18


# --- OPT-IN: one all-zero row is structurally a row but carries no information --------------------
def test_looks_all_zero_is_narrow():
    assert looks_all_zero("ACTIVE_CONTRACT_CNT\n0")
    assert looks_all_zero("CNT | TOTAL\n0 | NULL")
    assert not looks_all_zero("CNT\n5")
    assert not looks_all_zero("SUPPLIER | TOTAL\nIBM | 0")        # a real label -> informative
    assert not looks_all_zero("CNT\n0\n0")                        # more than one data row
    assert not looks_all_zero(no_data("x")) and not looks_all_zero(None)


def test_zero_row_escalates_only_when_pack_opts_in(monkeypatch):
    for flag, expect_status in ((False, ""), (True, "no_data")):
        monkeypatch.setattr(G, "load_config",
                            lambda uc, f=flag: {**_CFG, "max_data_retries": 0, "zero_is_no_data": f})
        final = build_graph("dq_qals", llm=_Cap("Zero active contracts were found."),
                            eval_llm=_JudgeSpy("SCORE: 18/18 - ok"),
                            sql=_Sql("ACTIVE_CONTRACT_CNT\n0"), verbose=False) \
            .invoke(initial_state("how many active contracts?"))
        assert final["status"] == expect_status


def test_all_null_aggregate_row_is_caught_when_opted_in(monkeypatch):
    # The live shape this exists for: asking about a plant that isn't in the data returned ONE row of
    # NULLs, which is structurally "a row" -> it scored 16/18 with status=ok before the pack opted in.
    monkeypatch.setattr(G, "load_config",
                        lambda uc: {**_CFG, "max_data_retries": 0, "zero_is_no_data": True})
    judge = _JudgeSpy()
    final = build_graph("dq_qals", llm=_Cap("No inventory records exist for that plant."),
                        eval_llm=judge, verbose=False,
                        sql=_Sql("TOTAL_STOCK_QUANTITY | TOTAL_INVENTORY_VALUE\nNone | None")) \
        .invoke(initial_state("on-hand inventory balance for plant ZZ999"))
    assert final["status"] == "no_data" and final["grounded"] is False
    assert final["best_score"] == -1 and judge.calls == 0


def test_the_two_escalation_reasons_are_distinguishable(monkeypatch):
    # A human must be able to tell "the query ran and matched nothing" (check filters / freshness) from
    # "this data cannot answer that question" (ask a different agent). Both are unscored.
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG, "max_data_retries": 1})
    blank = build_graph("dq_qals", llm=_Cap("how many widgets are in stock right now?",
                                            "No rows came back."),
                        eval_llm=_JudgeSpy(), sql=_Sql(no_data("matched nothing")), verbose=False) \
        .invoke(initial_state("how many widgets are in stock?"))
    wrong = build_graph("dq_qals", llm=_Cap("NO_REFORMULATION", "Employee data isn't here."),
                        eval_llm=_JudgeSpy(), sql=_Sql(no_data("covers inventory only")), verbose=False) \
        .invoke(initial_state("average employee salary by department?"))
    assert blank["status"] == "no_data" and wrong["status"] == "out_of_scope"
    assert blank["best_score"] == -1 and wrong["best_score"] == -1      # neither is ever scored
