"""P8 — Finished work is never withheld. See PRINCIPLES.md."""

from __future__ import annotations

import os

import pytest

from principles_helpers import command_job, new_graph, principle, requires_core, run_graph


def _mixed_graph(tmp_path, fixed=False):
    g = new_graph(tmp_path)
    command_job({"out": "good.txt"}, argv=["/bin/sh", "-c", "echo ok > {out:out}"])
    if fixed:
        command_job({"out": "bad.txt"}, argv=["/bin/sh", "-c", "echo fixed > {out:out}"])
    else:
        command_job({"out": "bad.txt"}, argv=["/bin/sh", "-c", "exit 3"])
    return g


@requires_core
@principle("P8.1")
def test_succeeded_jobs_survive_a_failed_run_as_hits(tmp_path):
    with pytest.raises(Exception):
        run_graph(_mixed_graph(tmp_path, fixed=False))

    r2 = run_graph(_mixed_graph(tmp_path, fixed=True))
    assert r2.failed == {}
    assert "good.txt" in " ".join(r2.hits), (
        "work that succeeded before the failure was recomputed — the store "
        "must keep it"
    )


@requires_core
@principle("P8.2")
def test_failed_run_leaves_an_inspectable_partial_tree(tmp_path):
    with pytest.raises(Exception):
        run_graph(_mixed_graph(tmp_path, fixed=False))

    # good.txt finished before the run failed. Its bytes must be reachable
    # through some browsable tree under the project (a partial generation,
    # marked, never `current`) — not only via store archaeology. A user two
    # days into a multi-day run inspects results as they finish.
    found = []
    for root, _dirs, files in os.walk(tmp_path / ".ppg3"):
        for name in files:
            p = os.path.join(root, name)
            try:
                if open(p, "rb").read() == b"ok\n":
                    found.append(p)
            except OSError:
                pass
    assert found, (
        "no browsable path to the finished job's output exists after the "
        "failed run — 899 finished jobs hidden because the 900th failed"
    )


@requires_core
@principle("P8.3")
def test_current_never_points_at_a_partial_snapshot(tmp_path):
    g1 = new_graph(tmp_path)
    command_job({"out": "a.txt"}, argv=["/bin/sh", "-c", "echo v1 > {out:out}"])
    r1 = run_graph(g1)
    assert r1.failed == {}
    outputs = tmp_path / "outputs"
    assert (outputs / "a.txt").read_text() == "v1\n"

    # Second run changes a's content *and* fails elsewhere: the visible
    # tree must still be the complete v1 snapshot, not a torn mix.
    g2 = new_graph(tmp_path)
    command_job({"out": "a.txt"}, argv=["/bin/sh", "-c", "echo v2 > {out:out}"])
    command_job({"out": "bad.txt"}, argv=["/bin/sh", "-c", "exit 3"])
    with pytest.raises(Exception):
        run_graph(g2)
    assert (outputs / "a.txt").read_text() == "v1\n"
    assert not (outputs / "bad.txt").exists()
