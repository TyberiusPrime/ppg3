"""Plumbing for the principles suite (see README.md in this directory and
PRINCIPLES.md at the repo root).

Deliberately NOT a conftest.py: a second conftest module in the tree
shadows ``python/tests/conftest.py`` for every sibling test file that does
``from conftest import ...`` (pytest prepend-mode imports both as
``conftest``). The ``principle`` marker is registered by the parent
conftest instead.

`principle("P1.1")` binds a test to an invariant in ``invariants.json`` and
derives its pytest treatment from the manifest status:

- ``enforced``  → plain test; a failure is a regression.
- ``violated``  → ``xfail(strict=True)``: the test asserts the *target*
  behavior and is expected to fail against today's implementation. The
  moment a fix lands, strict xfail turns the pass into an error, forcing
  the manifest status to be flipped to ``enforced`` in the same change.
- ``untested``  → skipped; the test body documents the intended assertion
  but needs wiring that does not exist yet.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PYTHON_DIR = HERE.parent.parent  # <repo>/python
REPO_ROOT = PYTHON_DIR.parent
if str(PYTHON_DIR) not in sys.path:
    sys.path.insert(0, str(PYTHON_DIR))

MANIFEST_PATH = HERE / "invariants.json"
MANIFEST = json.loads(MANIFEST_PATH.read_text())
INVARIANTS = {
    inv["id"]: inv
    for principle_entry in MANIFEST["principles"]
    for inv in principle_entry["invariants"]
}
VALID_STATUSES = {"enforced", "violated", "untested"}


def _has_core() -> bool:
    try:
        from ppg3 import _core  # noqa: F401

        return True
    except ImportError:
        return False


HAVE_CORE = _has_core()
requires_core = pytest.mark.skipif(
    not HAVE_CORE, reason="ppg3._core extension not built"
)


def principle(*ids: str):
    """Decorator: mark a test as asserting the given invariant id(s)."""
    for iid in ids:
        if iid not in INVARIANTS:
            raise KeyError(
                f"unknown invariant id {iid!r} — add it to invariants.json "
                "and PRINCIPLES.md first"
            )
    statuses = {INVARIANTS[iid]["status"] for iid in ids}

    def deco(fn):
        fn = pytest.mark.principle(ids)(fn)
        label = "/".join(ids)
        if "untested" in statuses:
            return pytest.mark.skip(
                reason=f"{label}: untested — needs wiring, see invariants.json"
            )(fn)
        if "violated" in statuses:
            return pytest.mark.xfail(
                strict=True,
                reason=f"{label}: known principle violation — see PRINCIPLES.md",
            )(fn)
        return fn

    return deco


# --------------------------------------------------------------------------
# Target-API construction helpers.
#
# Semantic invariants (does the engine rebuild? are the bytes right?) must
# not churn when the P10 rename (view= → outputs=) lands, so every test
# that doesn't specifically assert API *shape* builds jobs through these
# shims. When the rename happens, update the shims and the P1.1/P10.x
# shape tests — nothing else.
# --------------------------------------------------------------------------


def new_graph(tmp_path, **kwargs):
    """A fresh Graph against a store + project dir under tmp_path."""
    import ppg3
    from ppg3.tools import PyEnv

    store_dir = tmp_path / "store"
    store_dir.mkdir(exist_ok=True)
    defaults = dict(
        stores=[ppg3.Store("main", str(store_dir))],
        default_python=PyEnv.current(),
        project_dir=str(tmp_path / ".ppg3"),
        frozen=False,
        # Force source-mode transport: cloudpickle pickles module-level test
        # callbacks by reference, which the `python -I` shim cannot import
        # (same reasoning as test_e2e.py's _make_graph).
        paranoid=True,
    )
    defaults.update(kwargs)
    return ppg3.new(**defaults)


def file_job(outputs, run, **kwargs):
    import ppg3

    return ppg3.FileJob(view=outputs, run=run, **kwargs)


def command_job(outputs, argv, **kwargs):
    import ppg3

    return ppg3.CommandJob(view=outputs, argv=argv, **kwargs)


def fetch_job(output, url, **kwargs):
    import ppg3

    return ppg3.FetchJob(view=output, url=url, **kwargs)


def run_graph(graph, project_id="principles"):
    import ppg3

    return ppg3.run(graph, project_id=project_id)


def store_entries(tmp_path):
    """All published entry dirs in the tmp_path store."""
    entries = tmp_path / "store" / "v1" / "entries"
    if not entries.is_dir():
        return []
    return sorted(p for p in entries.iterdir() if p.is_dir())


def entry_manifest(entry_dir):
    return json.loads((Path(entry_dir) / "manifest.json").read_text())
