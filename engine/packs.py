"""
ENGINE / packs -- everything about READING a use-case pack from disk and composing it with `shared/`.
Pure file->text/dict functions; no loop logic, no model calls. (Split out of graph.py so the loop
module is the loop and nothing else.)

Composition rules (the pack-author contract; see docs/concepts/use-case-pack-anatomy.md):
  - config:       shared defaults + pack config (pack wins; via `inherits:`), `models:` deep-merged per role
  - prompts:      pack/prompts/<f>  ELSE  shared/prompts/<f>        (override by FILE PRESENCE)
  - skills:       shared/skills/* + pack/skills/*                    (concatenated; pack may exclude shared)
  - instructions: shared/instructions/* + pack/instructions/*        (concatenated; operator-owned)
  - exemplars:    pack/exemplars/*.yaml                              (few-shot; text_to_sql | input_output)
  - semantic_layer: pack/semantic_layer.yaml                         (AI+BI packs only; presence = AI+BI)

Every loader takes optional root overrides (`usecases=`, `shared=`) so a caller -- the composition
root in graph.py, or a test with a tmp tree -- binds the roots explicitly instead of via hidden globals.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
USECASES = REPO_ROOT / "usecases"
SHARED = REPO_ROOT / "shared"


def _roots(usecases, shared):
    return (usecases or USECASES), (shared or SHARED)


# --- config --------------------------------------------------------------------------------------
def _merge_models(acc: dict, incoming) -> dict:
    """Deep-merge the nested `models:` block PER ROLE (worker/evaluator), so a pack overriding one
    role keeps the other role (and other fields) inherited from a base."""
    if not isinstance(incoming, dict):
        return acc
    out = dict(acc)
    for role, prof in incoming.items():
        out[role] = {**out[role], **prof} if isinstance(prof, dict) and isinstance(out.get(role), dict) else prof
    return out


_PACK_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-]*$")


def check_pack_name(use_case: str) -> str:
    """A pack name is a FOLDER name under usecases/ -- never a path. It comes from operator config (USE_CASE
    env / the CLI), but it is joined onto the filesystem and imported as a module, so it is validated
    anyway: identifier characters only (no separators, no `..`)."""
    if not isinstance(use_case, str) or not _PACK_NAME.match(use_case):
        raise ValueError(f"invalid use-case pack name {use_case!r} (letters, digits, _ and - only)")
    return use_case


def load_config(use_case: str, *, usecases: Path | None = None, shared: Path | None = None) -> dict:
    """Merge inherited bases (e.g. shared) then the pack's own config (pack wins). Scalars use a
    shallow last-writer-wins; the nested `models:` block is deep-merged per role. Returns the RAW dict;
    `engine.config.PackConfig` validates it."""
    check_pack_name(use_case)
    uc, sh = _roots(usecases, shared)
    pack = yaml.safe_load((uc / use_case / "config.yaml").read_text(encoding="utf-8")) or {}
    inherits = pack.get("inherits", [])
    if isinstance(inherits, str):
        inherits = [inherits]
    merged: dict = {}
    models_acc: dict = {}
    for base in inherits:
        base_cfg = (sh if base == "shared" else sh.parent / base) / "config.yaml"
        if base_cfg.exists():
            b = yaml.safe_load(base_cfg.read_text(encoding="utf-8")) or {}
            models_acc = _merge_models(models_acc, b.get("models"))
            merged.update(b)
    models_acc = _merge_models(models_acc, pack.get("models"))
    merged.update(pack)
    if models_acc:
        merged["models"] = models_acc
    merged.pop("inherits", None)
    return merged


def load_semantic_layer(use_case: str, *, usecases: Path | None = None) -> dict | None:
    """The pack's NATIVE semantic layer (AI+BI packs only), from usecases/<pack>/semantic_layer.yaml.
    FILE PRESENCE marks the pack as AI+BI-backed: absent -> None; present-but-empty -> {} so the guard
    rail in get_sql_tool reports it. Pack-ONLY (not inherited); read from HERE, never from env."""
    f = (usecases or USECASES) / use_case / "semantic_layer.yaml"
    if not f.exists():
        return None
    return yaml.safe_load(f.read_text(encoding="utf-8")) or {}


# --- manifest ------------------------------------------------------------------------------------
def _golden_questions(use_case: str, *, usecases: Path | None = None) -> list[str]:
    """Questions from evals/golden_set.yaml (accepts `question` or `input`); best-effort -> []."""
    f = (usecases or USECASES) / use_case / "evals" / "golden_set.yaml"
    if not f.exists():
        return []
    try:
        golden = yaml.safe_load(f.read_text(encoding="utf-8")) or []
    except Exception:
        return []
    out = []
    for case in golden:
        if isinstance(case, dict):
            q = case.get("question") or case.get("input")
            if q and str(q).strip():
                out.append(str(q).strip())
    return out


def pack_manifest(use_case: str, *, usecases: Path | None = None, shared: Path | None = None) -> dict:
    """Non-secret, client-facing pack metadata: name, description, sample questions ('try these').
    `sample_questions` from config when set, else the golden-set questions. Pure metadata."""
    cfg = load_config(use_case, usecases=usecases, shared=shared)
    samples = cfg.get("sample_questions") or _golden_questions(use_case, usecases=usecases)
    samples = [str(s).strip() for s in (samples or []) if str(s).strip()][:20]
    return {"use_case": use_case,
            "name": cfg.get("name", use_case),
            "description": str(cfg.get("description", "")).strip(),
            "sample_task": str(cfg.get("sample_task", "")).strip(),
            "sample_questions": samples}


# --- prompts / skills / instructions / exemplars ------------------------------------------------
def _prompt(use_case: str, name: str, *, usecases: Path | None = None, shared: Path | None = None) -> str:
    """Pack's prompt if present, else fall back to shared/ (override semantics)."""
    uc, sh = _roots(usecases, shared)
    for base in (uc / use_case / "prompts", sh / "prompts"):
        f = base / name
        if f.exists():
            return f.read_text(encoding="utf-8")     # explicit UTF-8 (Windows cp1252 would mangle text)
    raise FileNotFoundError(f"{name} not found in pack or shared")


def load_skills(use_case: str, exclude_shared: list | None = None, *,
                usecases: Path | None = None, shared: Path | None = None) -> str:
    """shared skills + pack skills, concatenated. A pack may drop specific SHARED skills (by stem) via
    `exclude_shared_skills:`; pack skills are never excluded."""
    uc, sh = _roots(usecases, shared)
    drop = {str(s).strip().lower() for s in (exclude_shared or [])}
    shared_dir = sh / "skills"
    files = []
    for base in (shared_dir, uc / use_case / "skills"):
        if base.exists():
            for f in sorted(base.glob("*.md")):
                if base == shared_dir and f.stem.lower() in drop:
                    continue
                files.append(f)
    return "\n\n".join(f.read_text(encoding="utf-8").strip() for f in files) if files else "None."


def load_named_skills(use_case: str, names, *,
                      usecases: Path | None = None, shared: Path | None = None) -> str:
    """A NAMED subset of skills (by stem, `.md` optional), pack dir winning over shared on a clash. Used
    for the framing/planning subsets. Unknown names are skipped; empty/all-unknown -> 'None.'."""
    uc, sh = _roots(usecases, shared)
    if isinstance(names, str):
        names = [names]
    wanted = []
    for n in (names or []):
        s = str(n).strip()
        if s:
            wanted.append((s[:-3] if s.lower().endswith(".md") else s).lower())
    if not wanted:
        return "None."
    found = {}
    for base in (uc / use_case / "skills", sh / "skills"):   # pack first -> pack wins
        if base.exists():
            for f in base.glob("*.md"):
                found.setdefault(f.stem.lower(), f)
    files = [found[w] for w in wanted if w in found]
    return "\n\n".join(f.read_text(encoding="utf-8").strip() for f in files) if files else "None."


def load_instructions(use_case: str, exclude: list | None = None, *,
                      usecases: Path | None = None, shared: Path | None = None) -> str:
    """Operator INSTRUCTIONS (behavioral/policy directives), composed like skills. Distinct from skills
    so an operator owns them and the rubric can grade adherence. '' when none (-> no block injected)."""
    uc, sh = _roots(usecases, shared)
    drop = {str(s).strip().lower() for s in (exclude or [])}
    shared_dir = sh / "instructions"
    files = []
    for base in (shared_dir, uc / use_case / "instructions"):
        if base.exists():
            for f in sorted(base.glob("*.md")):
                if base == shared_dir and f.stem.lower() in drop:
                    continue
                files.append(f)
    return "\n\n".join(f.read_text(encoding="utf-8").strip() for f in files)


def _instr_block(*parts: str) -> str:
    """The OPERATOR INSTRUCTIONS block from one or more sources; '' when all empty (no block)."""
    body = "\n".join(p.strip() for p in parts if p and p.strip())
    if not body:
        return ""
    return ("OPERATOR INSTRUCTIONS (authoritative — the answer MUST comply; "
            "penalize violations):\n" + body)


def _format_exemplar(p: dict) -> str:
    """`kind: text_to_sql` -> Q:/SQL:, `kind: input_output` -> Input:/Output:; no `kind` -> the
    presence of `sql` selects text_to_sql (backward compatible)."""
    kind = str(p.get("kind", "")).strip().lower()
    if kind == "text_to_sql" or (not kind and "sql" in p):
        return f"Q: {p['question']}\nSQL: {p['sql']}"
    prompt = p.get("input", p.get("question", ""))
    output = p.get("output", p.get("answer", ""))
    return f"Input: {prompt}\nOutput: {output}"


def load_exemplars(use_case: str, *, usecases: Path | None = None) -> str:
    """Pack few-shot exemplars from exemplars/*.yaml; 'None.' when there are none."""
    d = (usecases or USECASES) / use_case / "exemplars"
    pairs = []
    if d.exists():
        for f in sorted(d.glob("*.yaml")):
            pairs += yaml.safe_load(f.read_text(encoding="utf-8")) or []
    if not pairs:
        return "None."
    return "\n\n".join(_format_exemplar(p) for p in pairs)


# --- business glossary -----------------------------------------------------------------------------
def load_glossary(use_case: str, *, usecases: Path | None = None, shared: Path | None = None) -> list[dict]:
    """The BUSINESS GLOSSARY: `shared/glossary.yaml` + `usecases/<pack>/glossary.yaml`, each a list of
    `{term, aliases?, meaning, maps_to?}`, MERGED BY TERM with the pack winning on an overlap (the same
    'pack overrides shared' rule as config/skills). Absent files -> []. A term entry tells the agent what a
    business word MEANS and HOW IT MAPS to the data -- consulted by framing (query side) and by the answer."""
    uc, sh = _roots(usecases, shared)
    merged: dict[str, dict] = {}
    for f in (sh / "glossary.yaml", uc / use_case / "glossary.yaml"):    # shared first -> pack overrides
        if not f.exists():
            continue
        entries = yaml.safe_load(f.read_text(encoding="utf-8")) or []
        if not isinstance(entries, list):
            raise ValueError(f"{f}: glossary must be a list of {{term, meaning, maps_to}} entries")
        for e in entries:
            if not isinstance(e, dict) or not str(e.get("term", "")).strip():
                raise ValueError(f"{f}: every glossary entry needs a `term`")
            merged[str(e["term"]).strip().lower()] = e
    return list(merged.values())


def render_glossary(entries: list[dict]) -> str:
    """Prompt block for the glossary ('' when empty): term (aka aliases): meaning -> maps to."""
    lines = []
    for e in entries or []:
        term = str(e.get("term", "")).strip()
        aliases = e.get("aliases") or []
        aka = f" (aka {', '.join(str(a) for a in aliases)})" if aliases else ""
        meaning = str(e.get("meaning", "")).strip()
        maps_to = str(e.get("maps_to", "")).strip()
        line = f"- **{term}**{aka}: {meaning}" + (f" -> maps to: {maps_to}" if maps_to else "")
        lines.append(line)
    if not lines:
        return ""
    return ("BUSINESS GLOSSARY (what a business term means and how it maps to the data; a pack entry "
            "overrides the shared one):\n" + "\n".join(lines))


# --- templating ----------------------------------------------------------------------------------
def fill(template: str, **kw) -> str:
    """Single-pass placeholder fill: each {key} replaced in ONE pass, so a substituted value containing
    "{other}" is never re-interpreted; literal braces / unknown {names} are left untouched."""
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
                  lambda m: str(kw[m.group(1)]) if m.group(1) in kw else m.group(0),
                  template)


def _fill(template: str, instructions: str = "", **kw) -> str:
    """fill() plus operator-instruction placement: `{instructions}` in the template controls WHERE the
    block goes; without it the block is AUTO-PREPENDED only when non-empty (packs with no instructions
    render byte-for-byte as before)."""
    out = fill(template, instructions=instructions, **kw)
    if instructions and "{instructions}" not in template:
        out = instructions + "\n\n" + out
    return out
