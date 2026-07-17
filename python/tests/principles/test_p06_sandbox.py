"""P6 — The sandbox is a feature you can hold. See PRINCIPLES.md."""

from __future__ import annotations

import inspect

import pytest

import ppg3
from principles_helpers import (
    command_job,
    entry_manifest,
    new_graph,
    principle,
    requires_core,
    run_graph,
    store_entries,
)


@principle("P6.1")
def test_sandbox_mode_is_selectable_at_graph_creation():
    params = inspect.signature(ppg3.new).parameters
    assert "sandbox" in params, (
        "ppg3.new() has no sandbox= parameter — the bwrap executor and the "
        "unshare entry exist in ppg3-core, but nothing wires them: the "
        "sandbox literally cannot be enabled"
    )


@requires_core
@principle("P6.2")
def test_sandbox_require_fails_at_run_start_when_unavailable(tmp_path):
    from ppg3 import _core

    if _core.sandbox_available():
        pytest.skip("host has real enforcement — the unavailable path can't fire")
    g = new_graph(tmp_path, sandbox="require")
    command_job({"out": "a.txt"}, argv=["/bin/sh", "-c", "echo a > {out:out}"])
    with pytest.raises(Exception) as ei:
        run_graph(g)
    msg = str(ei.value).lower()
    # Never a silent downgrade: an error, at run start, with instructions.
    assert "sandbox" in msg and ("bwrap" in msg or "nix" in msg)


@requires_core
@principle("P6.3")
def test_undeclared_input_is_not_readable_inside_the_job(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("s3cret\n")
    g = new_graph(tmp_path)
    command_job(
        {"out": "stolen.txt"},
        # `read` + redirection are shell builtins: this must not depend on
        # PATH scrubbing accidentally hiding the file — only the sandbox's
        # filesystem view may make it unreadable.
        argv=["/bin/sh", "-c", f"read x < {secret}; echo $x > {{out:out}}"],
    )
    with pytest.raises(Exception):
        # Undeclared dependency = file-not-found at build time (§2), not a
        # latent correctness bug.
        run_graph(g)


@requires_core
@principle("P6.4")
def test_manifest_records_the_enforcement_that_actually_applied(tmp_path):
    g = new_graph(tmp_path)
    command_job({"out": "a.txt"}, argv=["/bin/sh", "-c", "echo a > {out:out}"])
    r = run_graph(g)
    assert r.failed == {}
    (entry,) = store_entries(tmp_path)
    built = entry_manifest(entry)["built"]
    # This suite runs without enforcement wired (see P6.1); the manifest
    # must say so — an unsandboxed entry claiming sandboxed=true poisons
    # every consumer's trust in the store.
    assert built["sandboxed"] is False


@requires_core
@principle("P6.5")
def test_unenforced_runs_warn_exactly_once_per_run(tmp_path, capfd):
    g = new_graph(tmp_path)
    command_job({"out": "a.txt"}, argv=["/bin/sh", "-c", "echo a > {out:out}"])
    command_job({"out": "b.txt"}, argv=["/bin/sh", "-c", "echo b > {out:out}"])
    command_job({"out": "c.txt"}, argv=["/bin/sh", "-c", "echo c > {out:out}"])
    r = run_graph(g)
    assert r.failed == {}
    err = capfd.readouterr().err
    warnings = [ln for ln in err.splitlines() if "sandbox" in ln.lower()]
    assert len(warnings) == 1, (
        f"running without enforcement must be loud exactly once per run "
        f"(not per job, not zero): got {warnings!r}"
    )
