"""P1 — Identity is content, not names. See PRINCIPLES.md."""

from __future__ import annotations

import inspect
import re

import pytest

import ppg3
from principles_helpers import (
    command_job,
    entry_manifest,
    file_job,
    new_graph,
    principle,
    requires_core,
    run_graph,
    store_entries,
)


def _write_hello(io):
    with open(io.path("out"), "w") as fh:
        fh.write("hello\n")


def _boom(io):
    raise ValueError("KABOOM-P1")


_PUBLIC_CONSTRUCTORS = [
    ppg3.FileJob,
    ppg3.CommandJob,
    ppg3.FetchJob,
    ppg3.GraphJob,
    ppg3.UnsandboxedJob,
]


@principle("P1.1")
def test_no_public_constructor_accepts_name():
    offenders = [
        ctor.__name__
        for ctor in _PUBLIC_CONSTRUCTORS
        if "name" in inspect.signature(ctor).parameters
    ]
    assert offenders == [], (
        f"job constructors expose name= ({offenders}); identity is the input "
        "key — names are a second identity that can drift and conflict"
    )


@principle("P1.2")
def test_equal_jobs_with_different_destinations_coexist(tmp_path):
    new_graph(tmp_path)
    a = file_job({"out": "a/result.txt"}, run=_write_hello)
    b = file_job({"out": "b/result.txt"}, run=_write_hello)
    assert a is not b  # both defined, no conflict of any kind


@requires_core
@principle("P1.3")
def test_renaming_a_destination_causes_zero_rebuilds_and_a_working_link(tmp_path):
    g1 = new_graph(tmp_path)
    command_job({"out": "old/name.txt"}, argv=["/bin/sh", "-c", "echo hello > {out:out}"])
    r1 = run_graph(g1)
    assert r1.failed == {}
    assert len(r1.built) == 1

    # Same job, new destination: identity (inputs/recipe/output names) is
    # unchanged, so this must be a pure re-link.
    g2 = new_graph(tmp_path)
    command_job({"out": "new/name.txt"}, argv=["/bin/sh", "-c", "echo hello > {out:out}"])
    r2 = run_graph(g2)
    assert r2.failed == {}
    assert r2.built == [], "a rename recomputed"
    moved = tmp_path / "outputs" / "new" / "name.txt"
    assert moved.read_text() == "hello\n", (
        "the re-linked output does not resolve — publish paths leaked into "
        "the store entry's content layout (see P1.4)"
    )


@requires_core
@principle("P1.4")
def test_entry_content_is_keyed_by_output_name_not_publish_path(tmp_path):
    g = new_graph(tmp_path)
    command_job(
        {"out": "deeply/nested/publish/path.txt"},
        argv=["/bin/sh", "-c", "echo hello > {out:out}"],
    )
    r = run_graph(g)
    assert r.failed == {}
    entries = store_entries(tmp_path)
    assert len(entries) == 1
    content_keys = set(entry_manifest(entries[0])["content"].keys())
    assert content_keys == {"out"}, (
        f"entry content is keyed by {sorted(content_keys)} — the publish "
        "path shaped the stored bytes' layout; entries must be a function "
        "of the input key alone (output *names*), publish mapping is "
        "pointer state (P3)"
    )


@requires_core
@principle("P1.5")
def test_failure_reports_name_jobs_by_definition_site(tmp_path):
    g = new_graph(tmp_path)
    defsite_line = inspect.currentframe().f_lineno + 1
    file_job({"out": "will_fail.txt"}, run=_boom)
    with pytest.raises(Exception) as ei:
        run_graph(g)
    msg = str(ei.value)
    assert re.search(r"test_p01_identity\.py:\d+", msg), (
        "the failure report never says where the job was defined — the "
        f"definition site (this file, line {defsite_line}) is how a human "
        "finds their job"
    )
