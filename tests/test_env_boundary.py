"""ENFORCE the .env-file boundary as an executable guarantee.

The `.env` file is a DUMMY local-testing file. It must be read ONLY by the runner/test scripts (via
`load_dotenv`), and NEVER by `engine/` (real/source code). Note the distinction this test rests on:

  * the `.env` FILE  -> loaded by `load_dotenv()`  -> allowed only in runner scripts.
  * ENVIRONMENT VARS -> `os.environ`               -> engine MAY read these; in production they are
                                                      injected by the platform (SPCS `spec.yaml`,
                                                      Databricks Serving), not by any `.env` file.

So this test does NOT forbid engine from reading `os.environ` (that is the documented core principle --
"swap platform via one env var each"). It forbids engine from reading the `.env` FILE, i.e. importing
`dotenv` / calling `load_dotenv`. AST-based so comments/docstrings that merely MENTION `.env` (like the
ones above) don't trip it.
"""
import ast

import engine.graph as G

ENGINE = G.REPO_ROOT / "engine"
RUNNERS = ("run_local.py", "run_evals.py", "chat_local.py")


def _reads_dotenv_file(py_path):
    """True iff this module imports the `dotenv` package or calls `load_dotenv` (the mechanisms that
    actually open a `.env` file). AST-only: string/comment mentions of `.env` are ignored."""
    tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] == "dotenv" for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "dotenv":
                return True
        elif isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
            if name == "load_dotenv":
                return True
    return False


def test_engine_never_reads_the_dotenv_file():
    offenders = [p.relative_to(G.REPO_ROOT).as_posix()
                 for p in ENGINE.rglob("*.py") if _reads_dotenv_file(p)]
    assert not offenders, (
        "engine/ must NEVER read the .env file (load_dotenv/import dotenv). It reads os.environ, which "
        f"the platform injects in prod. Offending files: {offenders}")


def test_runner_scripts_do_read_the_dotenv_file():
    # Positive control: if the runners STOPPED using load_dotenv, the boundary test above would pass
    # vacuously (dotenv unused everywhere). Assert the .env file is still loaded where it SHOULD be, so
    # the guarantee stays meaningful.
    for name in RUNNERS:
        p = G.REPO_ROOT / name
        assert p.exists(), f"missing runner {name}"
        assert _reads_dotenv_file(p), f"{name} should load the dummy .env via load_dotenv (local test)"
