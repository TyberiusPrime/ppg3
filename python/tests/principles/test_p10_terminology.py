"""P10 — One word per concept. See PRINCIPLES.md.

These are the only tests (besides P1.1) that assert constructor *shape*;
everything else goes through conftest's target-API shims precisely so the
rename asserted here only touches the shims when it lands.
"""

from __future__ import annotations

import inspect

import ppg3
from principles_helpers import (
    file_job,
    new_graph,
    principle,
    requires_core,
    run_graph,
    store_entries,
)


def _noop(io):
    pass


def _write_internal(io):
    with open(io.path("data"), "w") as fh:
        fh.write("internal payload\n")


def _copy_dep(io):
    with open(io.input("dep"), "rb") as i, open(io.path("out"), "wb") as o:
        o.write(i.read())


@principle("P10.1")
def test_publishing_is_spelled_outputs_not_view():
    for ctor in (ppg3.FileJob, ppg3.CommandJob, ppg3.FetchJob):
        params = inspect.signature(ctor).parameters
        assert "outputs" in params and "view" not in params, (
            f"{ctor.__name__}: the output-name → tree-path mapping is "
            "publishing, and it is spelled outputs=; 'view' names nothing "
            "a user ever asked for"
        )


@principle("P10.2")
def test_a_job_without_published_outputs_is_legal(tmp_path):
    new_graph(tmp_path)
    # An internal job: consumers reference it directly; it simply does not
    # appear in the output tree. Nothing about "being a job" requires being
    # published.
    ppg3.FileJob(run=_noop)


@requires_core
@principle("P10.2")
def test_internal_outputs_are_consumable_and_unpublished(tmp_path):
    # The consumption half: a None destination declares the output *name*
    # (identity + entry layout, P1.4) without publishing it. The child reads
    # the bytes; the output tree never shows the internal file.
    g = new_graph(tmp_path)
    parent = file_job({"data": None}, run=_write_internal)
    file_job({"out": "final.txt"}, run=_copy_dep, inputs={"dep": parent})
    r = run_graph(g)
    assert r.failed == {}
    assert (tmp_path / "outputs" / "final.txt").read_text() == "internal payload\n"
    assert not (tmp_path / "outputs" / "data").exists(), (
        "an internal (unpublished) output leaked into the output tree"
    )
    # Both jobs are real store entries — internal means unpublished, not
    # uncached.
    assert len(store_entries(tmp_path)) == 2
