"""Shared test doubles (one home, not four copies). Worker/judge/SQL doubles + a scripted planner.
All deterministic, creds-free."""
from __future__ import annotations


class Cap:
    """Worker double: records the prompts it sees, returns scripted replies in order (repeats last)."""
    def __init__(self, *replies):
        self.replies, self.i, self.prompts = list(replies) or ["ANSWER"], 0, []

    def complete(self, prompt, **k):
        self.prompts.append(prompt)
        v = self.replies[min(self.i, len(self.replies) - 1)]
        self.i += 1
        return v


class Judge:
    """Judge double: records prompts AND counts calls (so a test can prove the judge never ran)."""
    def __init__(self, reply="SCORE: 18/18 - ok"):
        self.reply, self.calls, self.prompts = reply, 0, []

    def complete(self, prompt, **k):
        self.calls += 1
        self.prompts.append(prompt)
        return self.reply


class Sql:
    """SQLTool double: returns each scripted result once, repeating the last; records questions."""
    def __init__(self, *results):
        self.results, self.i, self.calls = list(results) or ["col | val\nA | 1"], 0, []

    def ask(self, q):
        self.calls.append(q)
        r = self.results[min(self.i, len(self.results) - 1)]
        self.i += 1
        if isinstance(r, Exception):
            raise r
        return r


class ScriptedLLM:
    """Emits scripted PLAN JSON for planner/replan prompts, scripted tool-calls for agentic prompts, and
    scripted text otherwise. Detects the kind by a stable marker, so it's robust to call order."""
    def __init__(self, *, plans=(), answers=("ANSWER",), acts=()):
        self.plans, self.answers, self.acts = list(plans), list(answers), list(acts)
        self.prompts, self._p, self._a, self._c = [], 0, 0, 0

    @staticmethod
    def _pop(q, i):
        return (q[min(i, len(q) - 1)], i + 1) if q else (None, i)

    def complete(self, prompt, **k):
        self.prompts.append(prompt)
        if "PLANNER" in prompt or "REVISING" in prompt:
            v, self._p = self._pop(self.plans, self._p)
            return v if v is not None else "{}"
        if "GATHERING EVIDENCE" in prompt:
            v, self._c = self._pop(self.acts, self._c)
            return v if v is not None else '{"final": true}'
        v, self._a = self._pop(self.answers, self._a)
        return v if v is not None else "ANSWER"
