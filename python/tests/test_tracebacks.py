"""Failed-job diagnostics: the failure table, the consolidated
``failure.log``, and rich tracebacks (source context + locals).

All jobs here deliberately raise. `requires_core` gates on the real
extension (the whole feature lives across the Rust scheduler + the Python
shim/template). Callbacks are module-level with body-local imports only so
they are valid under source-mode transport (`paranoid=True`), same as
`test_e2e.py`.
"""

import pytest

import ppg3
from ppg3.run import PPGRunError
from ppg3.tools import PyEnv

from conftest import requires_core


def _boom(io):
    # A KeyError with a distinctive local we can assert shows up in the
    # rich traceback's Locals section.
    marker_local = "find-me-in-locals"  # noqa: F841 (asserted in the log)
    data = {"present": 1}
    with open(io.path("out"), "w") as fh:
        fh.write(data["deliberately_missing"])  # KeyError


def _make_failing_graph(tmp_path, forkserver):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    g = ppg3.new(
        stores=[ppg3.Store("main", str(store_dir))],
        default_python=PyEnv.current(),
        project_dir=str(tmp_path / ".ppg3"),
        frozen=False,
        paranoid=True,
        forkserver=forkserver,
    )
    ppg3.FileJob(view={"out": "out.txt"}, run=_boom)
    return g


@requires_core
@pytest.mark.parametrize("forkserver", [True, False])
def test_failed_job_rich_traceback_and_table(tmp_path, forkserver):
    g = _make_failing_graph(tmp_path, forkserver)
    with pytest.raises(PPGRunError) as exc_info:
        ppg3.run(g, project_id="tb")

    result = exc_info.value.result
    assert "out.txt" in result.failed

    # (c) structured detail crosses from Rust.
    detail = result.failed_details["out.txt"]
    assert detail["exit_code"] == 1
    assert detail["failure_log"], "failure_log path must be set"

    # (a) the exception message renders a table row + the log path.
    msg = str(exc_info.value)
    assert "1 job(s) failed" in msg
    assert "out.txt" in msg
    assert "KeyError" in msg
    assert detail["failure_log"] in msg

    # (b) the consolidated failure.log exists and carries the exception type,
    # the *real* source line, and a Locals section.
    from pathlib import Path

    log_text = Path(detail["failure_log"]).read_text()
    assert "KeyError" in log_text
    assert 'data["deliberately_missing"]' in log_text, "real source line missing"
    assert "Locals:" in log_text
    assert "find-me-in-locals" in log_text, "frame locals missing"
    # stdout/stderr sections are both present in the consolidated log.
    assert "=== traceback / stderr ===" in log_text
    assert "=== stdout ===" in log_text


@requires_core
def test_format_failures_lists_all(tmp_path):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    g = ppg3.new(
        stores=[ppg3.Store("main", str(store_dir))],
        default_python=PyEnv.current(),
        project_dir=str(tmp_path / ".ppg3"),
        frozen=False,
        paranoid=True,
    )
    ppg3.FileJob(view={"a": "a.txt"}, run=_boom)
    ppg3.FileJob(view={"b": "b.txt"}, run=_boom)
    with pytest.raises(PPGRunError) as exc_info:
        ppg3.run(g, project_id="tb-multi")

    table = exc_info.value.result.format_failures()
    assert "2 job(s) failed" in table
    assert "a.txt" in table and "b.txt" in table
    assert "EXIT" in table and "ERROR" in table
