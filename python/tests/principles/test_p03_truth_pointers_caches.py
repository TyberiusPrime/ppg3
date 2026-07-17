"""P3 — One truth; everything else is a pointer or a cache. See PRINCIPLES.md."""

from __future__ import annotations

import shutil

import pytest

import ppg3
from ppg3.run import PPGRunError
from ppg3.tools import PyEnv

from principles_helpers import (
    command_job,
    file_job,
    new_graph,
    principle,
    requires_core,
    run_graph,
)


def _copy_input_f(io):
    with open(io.input("f"), "rb") as i, open(io.path("out"), "wb") as o:
        o.write(i.read())


@requires_core
@principle("P3.1")
def test_no_writable_store_fails_at_run_start_not_per_job(tmp_path):
    g = ppg3.new(
        stores=[],
        default_python=PyEnv.current(),
        project_dir=str(tmp_path / ".ppg3"),
        frozen=False,
        paranoid=True,
    )
    command_job({"out": "a.txt"}, argv=["/bin/sh", "-c", "echo a > {out:out}"])
    command_job({"out": "b.txt"}, argv=["/bin/sh", "-c", "echo b > {out:out}"])
    with pytest.raises(Exception) as ei:
        run_graph(g)
    err = ei.value
    assert "store" in str(err).lower()
    # A missing writable store is a *configuration* error: one message,
    # before any job is dispatched — not the same failure repeated per job.
    if isinstance(err, PPGRunError):
        assert not err.result.failed, (
            f"{len(err.result.failed)} per-job failures for one config error"
        )


@requires_core
@principle("P3.2")
def test_deleting_all_caches_costs_zero_rebuilds(tmp_path):
    data = tmp_path / "data.bin"
    data.write_bytes(b"leaf input\n")

    g1 = new_graph(tmp_path)
    file_job({"out": "copy.bin"}, run=_copy_input_f, inputs={"f": ppg3.File(str(data))})
    r1 = run_graph(g1)
    assert r1.failed == {}

    # Cache class: stat-cache and store logs. Deleting them may cost
    # re-hashing — never a rebuild, never lost history.
    statcache = tmp_path / ".ppg3" / "statcache.sqlite"
    assert statcache.exists()
    statcache.unlink()
    logs = tmp_path / "store" / "v1" / "logs"
    if logs.is_dir():
        shutil.rmtree(logs)

    g2 = new_graph(tmp_path)
    file_job({"out": "copy.bin"}, run=_copy_input_f, inputs={"f": ppg3.File(str(data))})
    r2 = run_graph(g2)
    assert r2.failed == {}
    assert r2.built == [], "cache deletion caused a rebuild — it was not a cache"


@requires_core
@principle("P3.3")
def test_deleting_the_project_dir_loses_pointers_not_content(tmp_path):
    g1 = new_graph(tmp_path)
    command_job({"out": "a.txt"}, argv=["/bin/sh", "-c", "echo a > {out:out}"])
    r1 = run_graph(g1)
    assert r1.failed == {}

    shutil.rmtree(tmp_path / ".ppg3")

    g2 = new_graph(tmp_path)
    command_job({"out": "a.txt"}, argv=["/bin/sh", "-c", "echo a > {out:out}"])
    r2 = run_graph(g2)
    assert r2.failed == {}
    assert r2.built == [], (
        "losing the project dir must lose generation history only, never "
        "built content — the store is the truth"
    )


@requires_core
@principle("P3.4")
def test_generation_pointer_state_has_one_authoritative_form(tmp_path):
    # Intent: a generation is either a record from which the link tree is
    # derived, or the link tree itself — corruption of the derived form is
    # detected/regenerated, never silently believed. Needs a
    # `ppg3 generations verify`-style entry point to assert against;
    # today meta.json and the symlink tree are written independently and
    # nothing cross-checks them.
    pytest.fail("needs a generation-verification entry point to assert against")
