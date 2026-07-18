"""P4 — A declared input is a readable input. See PRINCIPLES.md."""

from __future__ import annotations

import ppg3
from principles_helpers import command_job, file_job, new_graph, principle, requires_core, run_graph

PAYLOAD = b"\x00\x01\xffbinary bytes\n\x00tail"


def _copy_input_f(io):
    with open(io.input("f"), "rb") as i, open(io.path("out"), "wb") as o:
        o.write(i.read())


def _copy_input_dep(io):
    with open(io.input("dep"), "rb") as i, open(io.path("out"), "wb") as o:
        o.write(i.read())


def _two_outputs(io):
    with open(io.path("stable"), "w") as fh:
        fh.write("constant\n")
    with open(io.path("vary"), "w") as fh:
        fh.write(str(io.params["n"]))


def _consume_stable(io):
    # Read the subset input's bytes: P4's preamble makes readability half of
    # what declaring an input means — a subset ref that keys correctly but
    # cannot be opened is exactly the hash-only split P4 forbids.
    with open(io.input("s"), "rb") as fh:
        payload = fh.read()
    with open(io.path("out"), "wb") as fh:
        fh.write(b"consumed: " + payload)


def _write_params(io):
    with open(io.path("out"), "w") as fh:
        fh.write(f"{io.params['p']['n']}-{io.params['p']['s']}")


@requires_core
@principle("P4.1")
def test_file_input_bytes_are_readable_and_identical(tmp_path):
    data = tmp_path / "data.bin"
    data.write_bytes(PAYLOAD)
    g = new_graph(tmp_path)
    file_job({"out": "copy.bin"}, run=_copy_input_f, inputs={"f": ppg3.File(str(data))})
    r = run_graph(g)
    assert r.failed == {}
    assert (tmp_path / "outputs" / "copy.bin").read_bytes() == PAYLOAD


@requires_core
@principle("P4.2")
def test_parent_job_input_bytes_are_readable_and_identical(tmp_path):
    g = new_graph(tmp_path)
    parent = command_job(
        {"out": "parent.txt"}, argv=["/bin/sh", "-c", "printf 'payload-A\\n' > {out:out}"]
    )
    file_job({"out": "copy.txt"}, run=_copy_input_dep, inputs={"dep": parent})
    r = run_graph(g)
    assert r.failed == {}
    assert (tmp_path / "outputs" / "copy.txt").read_text() == "payload-A\n"


@requires_core
@principle("P4.3")
def test_subset_ref_keys_on_exactly_the_named_output(tmp_path):
    # Early cutoff per parent-output: a consumer of parent["stable"] must
    # not rebuild when only the parent's *other* output changed.
    def build(n):
        g = new_graph(tmp_path)
        parent = file_job(
            # publish paths == output names, so this test stays orthogonal
            # to the name/path conflation P1.4 covers.
            {"stable": "stable", "vary": "vary"},
            run=_two_outputs,
            inputs={"n": ppg3.Params(n)},
        )
        file_job({"out": "consumed"}, run=_consume_stable, inputs={"s": parent["stable"]})
        return run_graph(g)

    r1 = build(1)
    assert r1.failed == {}
    assert len(r1.built) == 2
    assert (tmp_path / "outputs" / "consumed").read_bytes() == b"consumed: constant\n"

    r2 = build(2)
    assert r2.failed == {}
    assert "stable+vary" in r2.built, "the params change must rerun the parent"
    assert "consumed" in r2.hits, (
        "the consumer depends on parent['stable'] only — a change confined "
        "to the sibling output must not rebuild it"
    )


@requires_core
@principle("P4.4")
def test_params_are_readable_value_identical(tmp_path):
    g = new_graph(tmp_path)
    file_job(
        {"out": "params.txt"},
        run=_write_params,
        inputs={"p": ppg3.Params({"n": 3, "s": "x"})},
    )
    r = run_graph(g)
    assert r.failed == {}
    assert (tmp_path / "outputs" / "params.txt").read_text() == "3-x"
