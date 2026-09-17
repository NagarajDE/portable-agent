"""Supply-chain policy, enforced instead of trusted (see docs/concepts/security-and-telemetry.md §3):

  1. DECLARE WHAT YOU IMPORT -- every third-party module imported by engine/ or the runner scripts,
     INCLUDING the lazy imports inside functions, must be declared in requirements.txt (or, for an opt-in
     adapter, requirements-optional.txt). `requests` was imported by the Cortex REST call AND the
     platform-neutral http tool while being declared only in the SPCS image file, so a local
     `pip install -r requirements.txt` resolved it transitively, unpinned and unaudited.
  2. PIN WHAT YOU DECLARE -- a declared package is pinned with `==` (or carries an explicit
     `# unpinned:` waiver naming why), in EVERY requirements file.
  3. The files AGREE on any package they share (the image can't drift from local).
  4. TRANSITIVES ARE LOCKED -- each intent file has a fully-resolved lock (every line `==`; the image lock
     also hashed), and the lock is NOT STALE: every pin in the intent file appears in its lock at the same
     version. `starlette` (serves the endpoint; fastapi only bounds it `>=0.46`) sat at a version with five
     advisories in a real environment while a fresh resolve looked clean -- the lock + an explicit pin close
     that. `starlette` must stay explicitly pinned in both intent files.

Pure AST + text parsing: no network, no installation, nothing imported."""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROOT_REQS = ROOT / "requirements.txt"
OPTIONAL_REQS = ROOT / "requirements-optional.txt"
DEV_REQS = ROOT / "requirements-dev.txt"
SPCS_REQS = ROOT / "engine" / "platform_snowflake" / "requirements.txt"
ALL_REQS = [ROOT_REQS, OPTIONAL_REQS, DEV_REQS, SPCS_REQS]
ROOT_LOCK = ROOT / "requirements.lock.txt"
SPCS_LOCK = ROOT / "engine" / "platform_snowflake" / "requirements.lock.txt"
LOCKS = {ROOT_REQS: ROOT_LOCK, SPCS_REQS: SPCS_LOCK}          # intent file -> its fully-resolved lock

# Transitives that serve the network edge: pinned EXPLICITLY in the intent files (not only in the lock) so
# `pip install -r requirements.txt` into an EXISTING environment upgrades them instead of leaving whatever
# already satisfied the library's loose lower bound.
SECURITY_RELEVANT_TRANSITIVES = {"starlette"}

# Source we ship or run. tests/ is excluded on purpose: a test may import a dev-only package (pytest).
SOURCE_DIRS = [ROOT / "engine"]
SOURCE_FILES = [ROOT / "run_local.py", ROOT / "run_evals.py", ROOT / "chat_local.py", ROOT / "runner_env.py"]

FIRST_PARTY = {"engine", "shared", "usecases", "tests", "runner_env"}

# import name -> distribution name on PyPI. An import we cannot map is a TEST FAILURE, not a pass: a new
# dependency must be mapped here AND declared, deliberately.
IMPORT_TO_DIST = {
    "langgraph": "langgraph", "yaml": "pyyaml", "pydantic": "pydantic",
    "requests": "requests", "urllib3": "urllib3", "httpx": "httpx",
    "litellm": "litellm", "anthropic": "anthropic", "openai": "openai",
    "snowflake": "snowflake-snowpark-python", "databricks": "databricks-sdk",
    "mcp": "mcp", "dotenv": "python-dotenv", "fastapi": "fastapi", "uvicorn": "uvicorn",
    "mlflow": "mlflow", "opentelemetry": "opentelemetry-sdk",
}


def _dist(name: str) -> str:
    return re.split(r"\[", name, maxsplit=1)[0].strip().lower().replace("_", "-")


def _requirements(path: Path) -> dict[str, str | None]:
    """{distribution: pinned version or None} from a requirements file (comments/blank lines ignored)."""
    out: dict[str, str | None] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        name, sep, version = line.partition("==")
        out[_dist(name)] = version.strip() if sep else None
    return out


def _waived(path: Path) -> set[str]:
    """Packages explicitly waived from the pin rule with a `# unpinned: <reason>` comment on the line."""
    waived = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        code, _, comment = raw.partition("#")
        if code.strip() and "unpinned:" in comment:
            waived.add(_dist(code.partition("==")[0]))
    return waived


def _source_files() -> list[Path]:
    files = [p for p in SOURCE_FILES if p.exists()]
    for d in SOURCE_DIRS:
        files += [p for p in d.rglob("*.py") if "__pycache__" not in p.parts]
    return files


def _eager_modules(skip_parts: set[str] = frozenset()) -> set[str]:
    """Third-party modules imported at MODULE level (i.e. on `import engine.…`) -- the vendor adapters
    import lazily inside functions, so those are deliberately excluded."""
    eager = set()
    for f in [p for p in (ROOT / "engine").rglob("*.py")
              if "__pycache__" not in p.parts and not (set(p.parts) & skip_parts)]:
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in tree.body:                                  # module level only == eager
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [] if node.level else [node.module or ""]
            else:
                continue
            for n in names:
                top = n.split(".")[0]
                if top and top not in sys.stdlib_module_names and top not in FIRST_PARTY:
                    eager.add(top)
    return eager


def _imported_modules() -> dict[str, set[Path]]:
    """Top-level module name -> the files importing it, for EVERY import in the tree (module level and
    inside functions, which is where the lazy vendor imports live)."""
    found: dict[str, set[Path]] = {}
    for f in _source_files():
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:                      # relative -> first-party
                    continue
                names = [node.module or ""]
            else:
                continue
            for n in names:
                top = n.split(".")[0]
                if top and top not in sys.stdlib_module_names and top not in FIRST_PARTY:
                    found.setdefault(top, set()).add(f.relative_to(ROOT))
    return found


# --- 1. declare what you import ------------------------------------------------------------------------
def test_every_third_party_import_is_mapped_to_a_distribution():
    unknown = {m: sorted(str(p) for p in f) for m, f in _imported_modules().items() if m not in IMPORT_TO_DIST}
    assert not unknown, (f"new third-party import(s) not mapped in IMPORT_TO_DIST: {unknown}. Add the "
                         f"mapping AND declare the package in requirements.txt.")


def test_every_third_party_import_is_declared_in_requirements():
    declared = set(_requirements(ROOT_REQS)) | set(_requirements(OPTIONAL_REQS))
    missing = {m: (IMPORT_TO_DIST[m], sorted(str(p) for p in f))
               for m, f in _imported_modules().items()
               if m in IMPORT_TO_DIST and IMPORT_TO_DIST[m] not in declared}
    assert not missing, (f"imported by engine/ or a runner but NOT declared in requirements.txt or "
                         f"requirements-optional.txt: {missing}. A direct import must never be left to "
                         f"transitive resolution (it ends up unpinned and unaudited).")


def test_default_install_covers_every_eagerly_imported_package():
    """An opt-in adapter may live in requirements-optional.txt ONLY because it is lazy-imported. Anything
    imported at MODULE level must be in the default install, or `import engine.graph` breaks."""
    optional = set(_requirements(OPTIONAL_REQS)) - set(_requirements(ROOT_REQS))
    eager = {IMPORT_TO_DIST.get(m, m) for m in _eager_modules(skip_parts={"platform_databricks"})}
    assert not (eager & optional), (f"eagerly imported but only in requirements-optional.txt: "
                                    f"{sorted(eager & optional)}")


def test_requests_is_declared_in_both_files():
    # the regression this whole module exists for: it is used by the Cortex REST call (Snowflake image)
    # AND by the platform-neutral http tool (any deployment), so BOTH files must carry it, pinned + equal.
    root, spcs = _requirements(ROOT_REQS), _requirements(SPCS_REQS)
    assert root.get("requests") and spcs.get("requests")
    assert root["requests"] == spcs["requests"]


# --- 2. pin what you declare --------------------------------------------------------------------------
@pytest.mark.parametrize("path", ALL_REQS, ids=["root", "optional", "dev", "spcs"])
def test_declared_packages_are_pinned_or_explicitly_waived(path):
    unpinned = sorted(d for d, v in _requirements(path).items() if v is None and d not in _waived(path))
    assert not unpinned, (f"{path.name}: unpinned requirement(s) {unpinned}. Pin with `==` (or add a "
                          f"`# unpinned: <reason>` waiver on the line).")


# --- 3. the files cannot drift from each other ----------------------------------------------------------
@pytest.mark.parametrize("other", [OPTIONAL_REQS, DEV_REQS, SPCS_REQS], ids=["optional", "dev", "spcs"])
def test_shared_packages_are_pinned_to_the_same_version(other):
    root, o = _requirements(ROOT_REQS), _requirements(other)
    mismatched = {d: (root[d], o[d]) for d in set(root) & set(o) if root[d] and o[d] and root[d] != o[d]}
    assert not mismatched, f"requirements.txt vs {other.name} disagree: {mismatched}"


# --- 4. transitives are locked, and the lock is not stale -------------------------------------------------
def _lock(path: Path) -> dict[str, str]:
    """{distribution: version} from a compiled lock (`name==ver \\` lines; `--hash` continuation lines
    and `# via` comments ignored)."""
    out = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip().rstrip("\\").strip()
        if not line or line.startswith("--"):
            continue
        name, sep, rest = line.partition("==")
        if sep:
            out[_dist(name)] = rest.split(";")[0].split()[0].strip()   # drop markers / trailing options
    return out


@pytest.mark.parametrize("intent", list(LOCKS), ids=["root", "spcs"])
def test_lock_exists_and_is_fully_pinned(intent):
    lock = LOCKS[intent]
    assert lock.exists(), f"{lock.relative_to(ROOT)} is missing -- regenerate it (command in {intent.name})"
    body = [ln.split("#", 1)[0].strip() for ln in lock.read_text(encoding="utf-8").splitlines()]
    loose = [ln for ln in body if ln and not ln.startswith("--") and "==" not in ln]
    assert not loose, f"{lock.name}: unpinned line(s) {loose[:5]}"
    assert len(_lock(lock)) > len(_requirements(intent)), "a lock must carry the transitives, not just the intent"


def test_image_lock_is_hashed():
    text = SPCS_LOCK.read_text(encoding="utf-8")
    pinned = [ln for ln in text.splitlines() if "==" in ln.split("#", 1)[0]]
    assert pinned and text.count("--hash=sha256:") >= len(pinned), \
        "the image lock must carry a hash for every pinned package (pip --require-hashes)"


@pytest.mark.parametrize("intent", list(LOCKS), ids=["root", "spcs"])
def test_lock_is_not_stale_relative_to_its_intent_file(intent):
    """A pin bumped in the intent file without regenerating the lock would ship the OLD version."""
    want, have = _requirements(intent), _lock(LOCKS[intent])
    stale = {d: (v, have.get(d)) for d, v in want.items() if v and have.get(d) != v}
    assert not stale, (f"{LOCKS[intent].name} is stale vs {intent.name}: {stale}. Regenerate it "
                       f"(the command is in the lock's header / the intent file).")


@pytest.mark.parametrize("intent", [ROOT_REQS, SPCS_REQS], ids=["root", "spcs"])
def test_security_relevant_transitives_are_pinned_explicitly(intent):
    declared = _requirements(intent)
    missing = sorted(t for t in SECURITY_RELEVANT_TRANSITIVES if not declared.get(t))
    assert not missing, (f"{intent.name}: {missing} must be pinned explicitly (`==`), not left to the "
                         f"library's loose lower bound -- see SECURITY_RELEVANT_TRANSITIVES")


def test_the_spcs_image_carries_everything_the_engine_imports_eagerly():
    """The image ships all of engine/, but only the EAGER imports must resolve at startup (the vendor
    adapters import lazily, so an unused provider need not be installed)."""
    eager = {IMPORT_TO_DIST.get(m, m) for m in _eager_modules(skip_parts={"platform_databricks"})}
    missing = sorted(eager - set(_requirements(SPCS_REQS)))
    assert not missing, f"the SPCS image would fail at import: {missing} not in its requirements"
