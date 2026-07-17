"""P2 — The only conflict is a contested destination. See PRINCIPLES.md."""

from __future__ import annotations

import inspect

import pytest

from principles_helpers import (
    command_job,
    new_graph,
    principle,
    requires_core,
    run_graph,
    store_entries,
)


def _here() -> int:
    return inspect.currentframe().f_back.f_lineno


@principle("P2.1")
def test_contested_destination_error_names_both_call_sites(tmp_path):
    new_graph(tmp_path)
    l1 = _here(); command_job({"out": "same.txt"}, argv=["/bin/sh", "-c", "echo a > {out:out}"])  # noqa: E702
    with pytest.raises(Exception) as ei:
        l2 = _here(); command_job({"out": "same.txt"}, argv=["/bin/sh", "-c", "echo b > {out:out}"])  # noqa: E702
    msg = str(ei.value)
    assert "test_p02_conflicts" in msg and f":{l1}" in msg and f":{l2}" in msg, (
        "two different jobs claimed one destination — the error must point "
        "at both definition sites, because opening those two lines is the "
        f"user's next action (got: {msg!r})"
    )


@principle("P2.1")
def test_contested_destination_cannot_be_smuggled_past_the_check(tmp_path):
    # The first pass's name= kwarg "disambiguated" duplicate ids — which
    # let two *different* jobs publish the same path with no error at all.
    # However jobs differ — here only in a declared env var, the subtlest
    # difference that still changes the input key — one destination + two
    # different jobs must error.
    new_graph(tmp_path)
    command_job({"out": "same.txt"}, argv=["/bin/sh", "-c", "echo a > {out:out}"])
    with pytest.raises(Exception):
        command_job(
            {"out": "same.txt"},
            argv=["/bin/sh", "-c", "echo a > {out:out}"],
            env={"SUBTLE": "difference"},
        )


@requires_core
@principle("P2.2")
def test_identical_jobs_different_destinations_build_once_link_twice(tmp_path):
    g = new_graph(tmp_path)
    command_job({"out": "left.txt"}, argv=["/bin/sh", "-c", "echo same > {out:out}"])
    command_job({"out": "right.txt"}, argv=["/bin/sh", "-c", "echo same > {out:out}"])
    r = run_graph(g)
    assert r.failed == {}
    assert (tmp_path / "outputs" / "left.txt").read_text() == "same\n"
    assert (tmp_path / "outputs" / "right.txt").read_text() == "same\n"
    assert len(store_entries(tmp_path)) == 1, (
        "identical work stored twice — dedup is the point of the system"
    )


@principle("P2.3")
def test_identical_redefinition_is_not_an_error(tmp_path):
    # Stating the exact same job twice (same recipe, same inputs, same
    # destination) contests nothing: it is one job, stated twice. The
    # "duplicate job id" error class must not exist.
    new_graph(tmp_path)
    command_job({"out": "x.txt"}, argv=["/bin/sh", "-c", "echo x > {out:out}"])
    command_job({"out": "x.txt"}, argv=["/bin/sh", "-c", "echo x > {out:out}"])
