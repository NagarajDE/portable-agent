"""
ENGINE / verdict -- the judge's reply, parsed into a VALIDATED object. The score decides which answer
reaches a human, so this is deliberately strict and fail-closed (a caller retries, then scores 0).

Accepts a JSON object {"score": N, "reason": "..."} (AUTHORITATIVE when present) or the house line
form `SCORE: N/<max> - <reason>`. Rounds (not truncates), rejects non-finite scores, rejects a
mismatched denominator, and never lets an embedded 'SCORE: 18/18' inside prose smuggle a pass.
"""
from __future__ import annotations

import json
import math
import re

from pydantic import BaseModel, field_validator


class Verdict(BaseModel):
    """Typed judge output: a bounded score plus the reason that drives `refine`."""
    score: int
    reason: str = ""

    @field_validator("score")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("score must be >= 0")
        return v


def _coerce_score(value) -> int:
    """A score must be a finite number; round to nearest int (don't truncate 17.9->17).
    Rejects bools (True would become 1) and catches OverflowError from huge JSON integers."""
    if isinstance(value, bool):
        raise ValueError("score must be a number, not a boolean")
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"score is not a finite number: {value!r}")
    if not math.isfinite(f):
        raise ValueError("score must be finite")
    return round(f)


def _first_json_object(text: str) -> str | None:
    """Return the FIRST balanced {...} object in text (tracking JSON strings + escapes), or None. A
    balanced scan beats a greedy regex that spans to the LAST '}' and then fails to parse -- which would
    silently re-enable the line-form fallback and let an embedded 'SCORE: 18/18' smuggle a pass."""
    start = text.find("{")
    if start == -1:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:            esc = False
            elif c == "\\":    esc = True
            elif c == '"':     in_str = False
            continue
        if c == '"':           in_str = True
        elif c == "{":         depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def parse_verdict(raw: str, max_score: int) -> Verdict:
    """Turn a judge reply into a validated Verdict, or raise ValueError/ValidationError (caller retries).
    JSON first (authoritative -- a bad JSON score RAISES rather than falling through to line form, else
    an embedded 'SCORE: 18/18' in the reason could smuggle a pass); else the line form, parsed in TWO
    steps so a malformed score token ('18e3') or a slash with no clean denominator ('18/18e3') is
    rejected rather than backtracked past."""
    text = (raw or "").strip()

    obj = None
    blob = _first_json_object(text)
    if blob:
        try:
            obj = json.loads(blob)
        except (ValueError, TypeError):
            obj = None

    if isinstance(obj, dict) and "score" in obj:
        verdict = Verdict(score=_coerce_score(obj["score"]),
                          reason=str(obj.get("reason", "")).strip())
    else:
        m = re.search(r"SCORE:\s*([0-9]+(?:\.[0-9]+)?)(?![\w.])", text, re.IGNORECASE)
        if not m:
            raise ValueError(f"no score found in judge reply: {text[:120]!r}")
        rest = text[m.end():]
        dm = re.match(r"\s*/\s*([0-9]+(?:\.[0-9]+)?)(?![\w.])", rest)
        if re.match(r"\s*/", rest) and dm is None:     # a slash with NO valid denominator -> reject
            raise ValueError(f"malformed denominator after score in: {text[:120]!r}")
        if dm is not None:
            if float(dm.group(1)) != max_score:        # 18.5 / 100 rejected; 18 or 18.0 pass
                raise ValueError(f"denominator {dm.group(1)} != max_score {max_score}")
            rest = rest[dm.end():]
        reason_m = re.match(r"\s*[-–—:]*\s*([^\n]*)", rest)
        reason = (reason_m.group(1).strip() if reason_m else "") or text
        verdict = Verdict(score=_coerce_score(m.group(1)), reason=reason)

    if verdict.score > max_score:
        raise ValueError(f"score {verdict.score} exceeds max {max_score}")
    return verdict
