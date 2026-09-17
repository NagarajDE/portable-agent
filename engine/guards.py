"""
ENGINE / guards -- DETERMINISTIC checks that a model-produced REWRITE of the user's question still asks
the user's question. Used by the reformulate-retry (and the framing step): a model asked to rephrase an
unanswerable question will happily ask a nearby one the data CAN answer -- which returns rows, looks
grounded, and would earn a passing score for a question nobody asked (seen live: "employee salary by
department" -> "inventory value per plant", scored 16/18). These guards are lexical and deliberately
crude; every false "drifted" verdict is SAFE (the run escalates to a human), a false "kept it" is the bug.

Known, accepted limitations (all in the safe direction): a measure swap that keeps its dimension may
read as kept (the prompt-level NO_REFORMULATION refusal is the second net); a re-expressed period code
(FY26 -> "fiscal 2026") reads as a dropped identifier and escalates rather than recovering.
"""
from __future__ import annotations

import re

_REFUSE = "NO_REFORMULATION"          # the reformulate prompt's explicit refusal token

# Words too generic to prove the subject survived. Deliberately small: every word added makes the guard
# STRICTER. The >=3-letter tier exists because the content-word floor is 3 (so 'tax'/'fee' are visible).
_GENERIC = {
    "what", "which", "when", "where", "many", "much", "total", "average", "list", "show", "give",
    "count", "value", "values", "amount", "amounts", "number", "numbers", "percent", "percentage",
    "share", "data", "field", "fields", "record", "records", "rows", "report", "across", "each",
    "have", "does", "were", "been", "with", "from", "that", "this", "there", "their", "your",
    "last", "over", "into", "most", "than", "then", "also", "only", "some", "them", "they", "will",
    "the", "and", "for", "are", "was", "has", "had", "our", "out", "per", "who", "you", "its",
    "all", "any", "one", "two", "top", "how", "why", "did", "not", "but", "may", "can", "via",
}


def _stem(word: str) -> str:
    """Fold a trailing plural to a common stem ('vendors'->'vendor', 'salaries'->'salary'). Rough on
    purpose: both sides are stemmed identically, so the comparison stays consistent."""
    if len(word) >= 6 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) >= 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


_GENERIC_STEMS = {_stem(w) for w in _GENERIC}   # stemmed, because words are stemmed before the test

# The MEASURE is everything before the first grouping marker; the rest is the DIMENSION.
_DIM = re.compile(r"\b(?:broken down by|grouped by|group by|for each|by|per|across)\b", re.IGNORECASE)


def _content_words(text: str) -> set[str]:
    """Distinctive (>=3-letter, non-generic, plural-folded) words -- the crude 'subject'. Short ALL-CAPS
    acronyms (HR, IT, EMEA) are taken from the original casing so an acronym-only subject is visible."""
    t = text or ""
    words = {_stem(w) for w in re.findall(r"[a-z]{3,}", t.lower())}
    words |= {a.lower() for a in re.findall(r"\b[A-Z]{2,5}\b", t)}
    return {w for w in words if w not in _GENERIC_STEMS}


def _measure_words(text: str) -> set[str]:
    """Content words of the MEASURE (the part before the first 'by/per/...' marker)."""
    return _content_words(_DIM.split(text or "", maxsplit=1)[0])


def _pinned_codes(text: str) -> set[str]:
    """Literal identifiers the user PINNED: tokens mixing letters and digits (ZZ999, PO12345). Dropping
    one WIDENS the population. Bare numbers are not pinned ('top 10' is a rank, not a filter)."""
    pat = r"\b(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{3,}\b"
    return {w.upper() for w in re.findall(pat, text or "")}


def _keeps_subject(original: str, rewritten: str) -> bool:
    """True when the MEASURE of the original survives the rewrite (narrowed, not substituted). Checks the
    measure, not the whole question, so keeping only the dimension is caught. Fail-open when the original
    has no measure word at all."""
    measure = _measure_words(original)
    if measure:
        return bool(measure & _content_words(rewritten))
    whole = _content_words(original)
    return not whole or bool(whole & _content_words(rewritten))


def _keeps_pinned(original: str, rewritten: str) -> bool:
    """True when EVERY pinned identifier survives (subset). Separate from the subject check because the
    two failures mean different things to a human (wrong question vs. no rows for that entity)."""
    pinned = _pinned_codes(original)
    return pinned <= _pinned_codes(rewritten)


def rewrite_drift(original: str, rewritten: str) -> str | None:
    """Classify a rewrite: None (kept the question), 'out_of_scope' (subject substituted -> this data
    can't answer it), or 'no_data' (an identifier was dropped -> the subject is fine, that entity has no
    rows). The reason decides what the human is told."""
    if not _keeps_subject(original, rewritten):
        return "out_of_scope"
    if not _keeps_pinned(original, rewritten):
        return "no_data"
    return None
