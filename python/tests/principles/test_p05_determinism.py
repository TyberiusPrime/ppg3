"""P5 — Same key, same bytes; everything that runs is an input.

The violation scenario is the same-miss-in-two-builders race of §11.1,
inside one run: two jobs with identical input keys (same recipe, same
inputs, same declared outputs — only their destinations differ) whose
output embeds the shell's pid. Both miss, both build, and the second
publish presents different bytes for an existing key. Publish-time
enforcement must make that a hard error.

NOTE: if identical-key jobs ever get coalesced before dispatch (the P2.2
target allows that), this scenario stops racing and these tests will fail
loudly — port them to a genuine two-coordinator race (two subprocesses
against one store) at that point.
"""

from __future__ import annotations

import os
import re

import pytest

from ppg3.run import PPGRunError

from principles_helpers import command_job, new_graph, principle, requires_core, run_graph


def _violation_message(tmp_path) -> str:
    """Provoke a determinism violation; return all user-facing failure text
    (the exception rendering plus the per-job reason blobs)."""
    g = new_graph(tmp_path, parallelism={"cores": 4})
    # A builtin-only busy loop keeps both builds in flight long enough that
    # neither's publish can turn the other into a cache hit (the job env has
    # no PATH to a sleep binary — deliberately, see P5.4).
    delay = "i=0; while [ $i -lt 200000 ]; do i=$((i+1)); done; "
    command_job({"out": "left.txt"}, argv=["/bin/sh", "-c", delay + "echo $$ > {out:out}"])
    command_job({"out": "right.txt"}, argv=["/bin/sh", "-c", delay + "echo $$ > {out:out}"])
    with pytest.raises(PPGRunError) as ei:
        run_graph(g)
    err = ei.value
    parts = [str(err)]
    parts += [d.get("reason") or "" for d in err.result.failed_details.values()]
    text = "\n".join(parts)
    assert "determinism" in text.lower()
    return text


@requires_core
@principle("P5.1")
def test_different_bytes_under_an_existing_key_is_a_hard_failure(tmp_path):
    _violation_message(tmp_path)  # raises inside, or this test fails


@requires_core
@principle("P5.2", "P9.3")
def test_violation_report_names_the_artifacts(tmp_path):
    msg = _violation_message(tmp_path)
    # The report is a set of directions (P9): where are the two conflicting
    # entries on disk, and what command continues the investigation.
    on_disk_paths = [
        tok for tok in re.findall(r"/\S+", msg) if os.path.exists(tok.rstrip(".,:;)"))
    ]
    assert on_disk_paths, (
        f"the violation report references no existing on-disk artifact — "
        f"nothing to inspect (got: {msg!r})"
    )
    assert "diff-entries" in msg, (
        "the report must hand the user the next command (ppg3 diff-entries …)"
    )


@requires_core
@principle("P9.4")
def test_no_bare_hash_without_an_accompanying_path(tmp_path):
    msg = _violation_message(tmp_path)
    # Rejoin word-wrap continuations (indented to the value column): the
    # invariant governs logical statements, not the renderer's line width.
    logical = re.sub(r"\n {8,}", " ", msg)
    hex64 = re.compile(r"\b[0-9a-f]{64}\b")
    bare = [
        line
        for line in logical.splitlines()
        if hex64.search(line) and "/" not in line and "://" not in line
    ]
    assert bare == [], (
        f"hashes with no path/name next to them are archaeology, not a "
        f"report: {bare!r}"
    )


def _tool_script(tmp_path, body: str):
    tool = tmp_path / "mytool.sh"
    tool.write_text(f"#!/bin/sh\n{body}\n")
    tool.chmod(0o755)
    return tool


@requires_core
@principle("P5.3")
def test_tool_hash_change_is_a_key_change(tmp_path):
    import ppg3

    tool = _tool_script(tmp_path, "echo v1")

    def build():
        g = new_graph(tmp_path)
        command_job(
            {"out": "out.txt"},
            argv=["/bin/sh", "-c", "echo fixed > {out:out}"],
            tools=[ppg3.ToolSpec.binary(str(tool), name="mytool")],
        )
        return run_graph(g)

    r1 = build()
    assert r1.failed == {}
    assert len(r1.built) == 1

    r2 = build()
    assert r2.built == []  # unchanged tool: hit

    _tool_script(tmp_path, "echo v2")
    r3 = build()
    assert len(r3.built) == 1, "a changed tool did not re-key the job"


@requires_core
@principle("P5.4")
def test_declared_env_keys_the_job_and_undeclared_env_is_invisible(tmp_path, monkeypatch):
    monkeypatch.setenv("PPG3_PRINCIPLES_LEAK", "leaked")

    def build(foo):
        g = new_graph(tmp_path)
        command_job(
            {"out": "env.txt"},
            argv=[
                "/bin/sh",
                "-c",
                'printf "%s|%s" "$FOO" "$PPG3_PRINCIPLES_LEAK" > {out:out}',
            ],
            env={"FOO": foo},
        )
        return run_graph(g)

    r1 = build("f1")
    assert r1.failed == {}
    assert (tmp_path / "outputs" / "env.txt").read_text() == "f1|", (
        "an undeclared host env var was visible inside the job"
    )

    r2 = build("f2")
    assert len(r2.built) == 1, "a declared env change did not re-key the job"
    assert (tmp_path / "outputs" / "env.txt").read_text() == "f2|"
