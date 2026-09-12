"""
PROOF that each pack's artifacts (skills, prompts, rubric, exemplars) are ACTUALLY composed into
what the model sees at runtime -- not stale files sitting on disk. This is a regression guard: if
a loader stops reading a folder (or someone hardcodes a prompt), a fingerprint taken FROM the
artifact file will be absent from the captured prompt and the test fails.

Strategy: inject a capturing worker + judge (records the exact prompt strings) and a fake SQL tool,
run the real pack through the loop, then assert that a distinctive line extracted at runtime FROM
each artifact file appears in the correct prompt:
  - shared skills + pack skills  -> the `generate` prompt (and refine)
  - pack exemplars               -> the `generate` prompt
  - the EFFECTIVE rubric         -> the `judge` prompt   (pack's rubric.md if present, else shared)
  - the EFFECTIVE refine template -> the `refine` prompt

Fully offline/deterministic (no provider, no credentials). All test code lives here in tests/.
"""
import re
import socket
import uuid

import pytest
import yaml

from engine import graph as G
from engine.graph import build_graph, initial_state, load_config, SHARED, USECASES

# Packs that ship a full set of artifacts (skills + exemplars + prompts; some override the rubric).
PACKS = ["dq_qals", "kpi_analytics", "anomaly_rca", "parity_hana_snowflake", "inventory_balance"]


class _Capture:
    """An LLMClient double that records every prompt it is asked to complete."""
    def __init__(self, *replies):
        self.replies, self.i, self.prompts = list(replies), 0, []

    def complete(self, prompt, **k):
        self.prompts.append(prompt)
        v = self.replies[min(self.i, len(self.replies) - 1)]
        self.i += 1
        return v


class _FakeSql:
    def ask(self, q):
        return "LOCATION | UNITS\nX | 1\nY | 2"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """T5: make network denial EXPLICIT -- any real socket use in these offline wiring tests fails
    loudly. Models/SQL are injected doubles and tools run in mock mode, so nothing should touch the
    network; this proves it instead of relying on implicit fixtures."""
    def _boom(*a, **k):
        raise AssertionError("unexpected network call in an offline wiring test")
    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "getaddrinfo", _boom)


def _shared_only_line(shared_path, pack_path) -> str:
    """A distinctive shared-rubric line that does NOT appear in the pack rubric -- used to prove an
    override REPLACES (not concatenates) the shared rubric."""
    pack_text = pack_path.read_text(encoding="utf-8")
    for line in shared_path.read_text(encoding="utf-8").splitlines():
        s = line.strip().lstrip("#-*>0123456789. ").strip()
        if "{" in s or "}" in s:
            continue
        if len(re.findall(r"[A-Za-z]{3,}", s)) >= 4 and s not in pack_text:
            return s
    return ""


def _distinctive(path) -> str:
    """A distinctive, brace-free line from a text artifact (>=4 alpha words). Brace lines are skipped
    because they are template placeholders that get substituted before the prompt is sent."""
    best = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip().lstrip("#-*>0123456789. ").strip()
        if "{" in s or "}" in s:
            continue
        if len(re.findall(r"[A-Za-z]{3,}", s)) >= 4 and len(s) > len(best):
            best = s
    return best


def _effective_prompt(use_case, name):
    """Pack's prompt if it exists (override), else the shared one (inherit) -- mirrors engine._prompt."""
    p = USECASES / use_case / "prompts" / name
    return p if p.exists() else SHARED / "prompts" / name


def _run(use_case, worker=None, judge=None):
    cfg = load_config(use_case)
    mx = cfg.get("max_score", 18)
    worker = worker or _Capture("draft A", "draft B", "draft C", "draft D", "draft E")
    judge = judge or _Capture(f"SCORE: 1/{mx} - low")     # low score -> forces at least one refine
    app = build_graph(use_case, llm=worker, eval_llm=judge, sql=_FakeSql(), verbose=False)
    app.invoke(initial_state(cfg.get("sample_task") or "test question"))
    return worker, judge


# --- skills are concatenated into generate (and refine) --------------------
@pytest.mark.parametrize("use_case", PACKS)
def test_shared_and_pack_skills_reach_generate(use_case):
    worker, _ = _run(use_case)
    gen = worker.prompts[0]
    skill_files = sorted((SHARED / "skills").glob("*.md")) + sorted((USECASES / use_case / "skills").glob("*.md"))
    assert skill_files, f"{use_case}: no skill files found"
    for f in skill_files:
        fp = _distinctive(f)
        assert fp and fp in gen, f"{use_case}: skill '{f.name}' NOT in generate prompt (fp={fp!r})"


@pytest.mark.parametrize("use_case", PACKS)
def test_skills_reach_refine(use_case):
    worker, _ = _run(use_case)
    assert len(worker.prompts) >= 2, f"{use_case}: expected a refine to run"
    refine_prompt = worker.prompts[1]
    skill_files = sorted((SHARED / "skills").glob("*.md")) + sorted((USECASES / use_case / "skills").glob("*.md"))
    assert skill_files, f"{use_case}: no skill files found"
    for f in skill_files:                               # ALL shared+pack skills reach refine (T1)
        fp = _distinctive(f)
        assert fp, f"{use_case}: skill '{f.name}' has no distinctive fingerprint"
        assert fp in refine_prompt, f"{use_case}: skill '{f.name}' NOT in refine prompt (fp={fp!r})"


# --- exemplars are injected into generate ----------------------------------
@pytest.mark.parametrize("use_case", PACKS)
def test_exemplars_reach_generate(use_case):
    worker, _ = _run(use_case)
    gen = worker.prompts[0]
    items = []
    for f in sorted((USECASES / use_case / "exemplars").glob("*.yaml")):
        items += yaml.safe_load(f.read_text(encoding="utf-8")) or []
    assert items, f"{use_case}: no exemplars found"
    for it in items:                                    # ALL exemplars, not just the first (T2)
        q = it.get("question") or it.get("input")
        assert q and q in gen, f"{use_case}: exemplar '{q}' NOT in generate prompt"


# --- the EFFECTIVE rubric reaches the judge (override vs inherit) -----------
@pytest.mark.parametrize("use_case", PACKS)
def test_effective_rubric_reaches_judge(use_case):
    _, judge = _run(use_case)
    rub = _effective_prompt(use_case, "rubric.md")
    fp = _distinctive(rub)
    assert fp and fp in judge.prompts[0], f"{use_case}: rubric '{rub}' NOT in judge prompt"


@pytest.mark.parametrize("use_case", PACKS)
def test_effective_refine_template_used(use_case):
    worker, _ = _run(use_case)
    ref = _effective_prompt(use_case, "refine.md")
    fp = _distinctive(ref)
    assert fp and fp in worker.prompts[1], f"{use_case}: refine template '{ref}' NOT used"


# --- override vs inherit, made explicit ------------------------------------
@pytest.mark.parametrize("use_case", PACKS)
def test_effective_generate_template_used(use_case):
    worker, _ = _run(use_case)
    tpl = _effective_prompt(use_case, "generate.md")
    fp = _distinctive(tpl)
    assert fp and fp in worker.prompts[0], f"{use_case}: generate template '{tpl}' NOT used (fp={fp!r})"


def test_rubric_override_and_inherit_are_distinct():
    # dq_qals OVERRIDES the rubric -> the judge sees the pack's rubric text
    _, judge = _run("dq_qals")
    assert _distinctive(USECASES / "dq_qals" / "prompts" / "rubric.md") in judge.prompts[0]
    # PRECEDENCE (not just presence): an override REPLACES shared, so a shared-ONLY line is absent (T4)
    shared_only = _shared_only_line(SHARED / "prompts" / "rubric.md",
                                    USECASES / "dq_qals" / "prompts" / "rubric.md")
    assert shared_only, "expected a shared-only rubric line to test precedence"
    assert shared_only not in judge.prompts[0], "override must REPLACE the shared rubric, not concatenate it"
    # api_assistant has NO rubric.md -> it INHERITS shared; the judge sees the SHARED rubric text
    assert not (USECASES / "api_assistant" / "prompts" / "rubric.md").exists()
    _, ijudge = _run("api_assistant",
                     worker=_Capture("d"), judge=_Capture("SCORE: 18/18 - ok"))
    assert _distinctive(SHARED / "prompts" / "rubric.md") in ijudge.prompts[0]


# --- #7: a pack can opt OUT of a shared skill (config-driven, end to end) ---
def test_pack_can_exclude_a_shared_skill():
    # api_assistant (non-SQL) sets exclude_shared_skills: [sql_safety]; reporting (universal) stays.
    worker, _ = _run("api_assistant", worker=_Capture("a", "b"), judge=_Capture("SCORE: 1/18 - low"))
    gen = worker.prompts[0]
    assert _distinctive(SHARED / "skills" / "reporting.md") in gen        # universal skill kept
    assert _distinctive(SHARED / "skills" / "sql_safety.md") not in gen   # SQL skill dropped for a non-SQL pack


# --- DECISIVE: loaders read the folders LIVE, nothing is hardcoded ----------
def test_loaders_read_folders_live_not_hardcoded(tmp_path, monkeypatch):
    sk = "SKILL_" + uuid.uuid4().hex
    gen_s = "GEN_" + uuid.uuid4().hex
    rub_s = "RUB_" + uuid.uuid4().hex
    ref_s = "REF_" + uuid.uuid4().hex
    ex_q = "EXQ_" + uuid.uuid4().hex
    pk = tmp_path / "synthetic"
    (pk / "prompts").mkdir(parents=True)
    (pk / "skills").mkdir()
    (pk / "exemplars").mkdir()
    (pk / "config.yaml").write_text(
        "name: synthetic\nsample_task: hello\nmax_score: 18\npass_score: 18\n"
        "max_iters: 1\neval_retries: 0\nmax_stall: 0\n")
    (pk / "skills" / "s.md").write_text(f"unique skill sentinel {sk} lives in this pack folder")
    (pk / "prompts" / "generate.md").write_text(
        f"PERSONA {gen_s}.\nSKILLS:\n{{skills}}\nEXAMPLES:\n{{exemplars}}\n"
        f"Q: {{task}}\nDATA: {{data}}\nrev {{revision}}")
    (pk / "prompts" / "rubric.md").write_text(
        f"RUBRIC {rub_s} out of {{max_score}} for {{task}} given {{answer}} vs {{data}}. "
        f"Reply SCORE: N/{{max_score}} - reason")
    (pk / "prompts" / "refine.md").write_text(
        f"REFINE {ref_s}: improve {{answer}} using {{feedback}} and {{data}} rev {{revision}}")
    (pk / "exemplars" / "e.yaml").write_text(f"- input: {ex_q}\n  output: something")
    monkeypatch.setattr(G, "USECASES", tmp_path)          # point the loaders at the synthetic pack

    worker = _Capture("a", "b")
    judge = _Capture("SCORE: 1/18 - low")                 # forces the single refine
    build_graph("synthetic", llm=worker, eval_llm=judge, sql=_FakeSql(), verbose=False) \
        .invoke(initial_state("hello"))
    gen = worker.prompts[0]
    assert sk in gen, "skill folder not read live"
    assert gen_s in gen, "pack generate.md not read live"
    assert ex_q in gen, "exemplars folder not read live"
    assert rub_s in judge.prompts[0], "pack rubric.md not read live"
    assert ref_s in worker.prompts[1], "pack refine.md not read live"
