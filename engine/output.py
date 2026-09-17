"""
ENGINE / output -- an OPT-IN declarable structured-output contract for a pack (the GAP-4 seam).

A pack may set `output_schema:` in config.yaml (a small JSON-Schema subset: `type: object`,
`required: [...]`, `properties: {name: {type: ...}}`). When declared, the loop parses the worker's
answer as JSON and validates it; a violation is fed back to the judge/refine loop as a hard failure so
the next revision fixes the SHAPE, not just the prose. Undeclared -> pass-through (zero behavior change).
Kept dependency-free on purpose (no jsonschema import) -- the subset covers report/record outputs.
"""
from __future__ import annotations

import json

_TYPES = {"string": str, "number": (int, float), "integer": int, "boolean": bool,
          "array": list, "object": dict}


def _first_json(text: str):
    start = text.find("{")
    if start == -1:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except (ValueError, TypeError):
                    return None
    return None


def validate_output(text: str, schema: dict | None) -> tuple[bool, dict | None, str]:
    """(ok, parsed_object, error). With no schema -> (True, None, ''). Checks: a JSON object is present;
    every `required` key exists; each declared `properties[k].type` matches (bool is not a number)."""
    if not schema:
        return True, None, ""
    obj = _first_json(text or "")
    if not isinstance(obj, dict):
        return False, None, "answer must be a JSON object matching the declared output_schema"
    missing = [k for k in (schema.get("required") or []) if k not in obj]
    if missing:
        return False, obj, f"missing required field(s): {', '.join(missing)}"
    for k, spec in (schema.get("properties") or {}).items():
        if k in obj and isinstance(spec, dict) and spec.get("type") in _TYPES:
            want = _TYPES[spec["type"]]
            v = obj[k]
            if isinstance(v, bool) and spec["type"] in ("number", "integer"):
                return False, obj, f"field {k!r} must be {spec['type']}, got boolean"
            if not isinstance(v, want):
                return False, obj, f"field {k!r} must be {spec['type']}"
    return True, obj, ""
