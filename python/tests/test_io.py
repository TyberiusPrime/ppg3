"""``JobIO`` accessor ergonomics: outputs/inputs come back as
:class:`pathlib.Path`, `out_path` mirrors `path`, and the object has a
useful `repr`. Core-free — `JobIO` is pure Python."""

from pathlib import Path

import pytest

from ppg3.io import JobIO, JobIOError


def _io():
    return JobIO(
        inputs={"reads": "/store/in/reads", "ref": "/store/in/ref.fa"},
        outputs={"counts": "/out/counts.tsv"},
        tools={"samtools": "/tools/samtools"},
        log_dir="/logs/j0",
    )


def test_input_and_path_return_path_objects():
    io = _io()
    assert isinstance(io.input("reads"), Path)
    assert io.input("reads") == Path("/store/in/reads")
    assert isinstance(io.path("counts"), Path)
    assert io.path("counts") == Path("/out/counts.tsv")
    # Single-output shortcut and the out_path alias agree, both Path.
    assert io.path() == Path("/out/counts.tsv")
    assert io.out_path("counts") == io.path("counts")
    assert isinstance(io.out_path(), Path)


def test_tool_stays_a_str():
    # tools are spliced into command strings, so they stay str.
    tool = _io().tool("samtools")
    assert tool == "/tools/samtools"
    assert isinstance(tool, str)


def test_unknown_names_raise_jobioerror():
    io = _io()
    with pytest.raises(JobIOError):
        io.input("nope")
    with pytest.raises(JobIOError):
        io.path("nope")


def test_repr_lists_declared_names():
    r = repr(_io())
    assert "inputs=['reads', 'ref']" in r
    assert "outputs=['counts']" in r
    assert "tools=['samtools']" in r
