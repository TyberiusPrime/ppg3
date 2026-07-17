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


def _print_then_boom(io):
    # A print immediately before the raise: on a non-tty stdout is
    # block-buffered, so this only survives into the log if the shim flushes
    # stdout on the failure path.
    print("PROGRESS-MARKER-42")
    raise RuntimeError("kaboom after printing")


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
    ppg3.FileJob(outputs={"out": "out.txt"}, run=_boom)
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
@pytest.mark.parametrize("forkserver", [True, False])
def test_print_before_exception_survives_into_log(tmp_path, forkserver):
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
    ppg3.FileJob(outputs={"out": "out.txt"}, run=_print_then_boom)
    with pytest.raises(PPGRunError) as exc_info:
        ppg3.run(g, project_id="tb-flush")

    from pathlib import Path

    detail = exc_info.value.result.failed_details["out.txt"]
    log_text = Path(detail["failure_log"]).read_text()
    assert "PROGRESS-MARKER-42" in log_text, "buffered stdout lost before exception"
    assert "kaboom after printing" in log_text


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
    ppg3.FileJob(outputs={"a": "a.txt"}, run=_boom)
    ppg3.FileJob(outputs={"b": "b.txt"}, run=_boom)
    with pytest.raises(PPGRunError) as exc_info:
        ppg3.run(g, project_id="tb-multi")

    table = exc_info.value.result.format_failures()
    assert "2 job(s) failed" in table
    # one block per job, keyed by a "Job:" line.
    assert table.count("Job:") == 2
    assert "Job:       a.txt" in table and "Job:       b.txt" in table
    # per-job fields present.
    assert "Exception:" in table and "Log:" in table and "Outputs:" in table


def test_shim_frames_get_a_clean_breadcrumb_line():
    """ppg3's worker-shim frames are collapsed to a single, well-formed
    breadcrumb: real filename kept, no stray quote, indented like every
    other frame, and their source/locals suppressed (regression guard for
    the malformed ``_shim.py":<lineno>`` line)."""
    from ppg3._traceback import Frame, Stack, Trace

    # Build a Trace from a throwaway exception, then swap in a hand-made
    # stack so we can assert on the _shim frame's rendering deterministically.
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        trace = Trace(*sys.exc_info())

    shim_file = "/some/where/ppg3/_shim.py"
    user_file = "/proj/analysis.py"
    trace.stacks = [
        Stack(
            exc_type="ValueError",
            exc_value="boom",
            frames=[
                Frame(
                    filename=shim_file,
                    lineno=42,
                    name="run_in_process",
                    locals={"secret": "should-not-render"},
                    source="line1\nline2\nline3\n",
                ),
                Frame(
                    filename=user_file,
                    lineno=2,
                    name="_boom",
                    locals={},
                    source="def _boom(io):\n    raise ValueError('boom')\n",
                ),
            ],
        )
    ]

    out = trace.format()

    assert f"  {shim_file}:42, in run_in_process (details skipped)" in out
    assert '_shim.py":' not in out, "stray quote in shim breadcrumb"
    # Shim frame's source/locals are suppressed...
    assert "should-not-render" not in out
    # ...but the real user frame still renders its source.
    assert "raise ValueError('boom')" in out


def test_single_failure_inlines_full_log_and_drops_truncated_tail(tmp_path):
    """With exactly one failed job, format_failures inlines the whole
    consolidated log (so the user needn't open the file), and it suppresses
    the truncated stderr-tail block that would otherwise duplicate it."""
    from ppg3.run import RunResult

    log = tmp_path / "failure.log"
    log.write_text(
        "=== traceback / stderr ===\n"
        "Traceback (most recent call last):\n"
        "  /proj/analysis.py:2, in _boom\n"
        "KeyError: 'deliberately_missing'\n"
        "=== stdout ===\n"
        "progress: 100%\n"
    )
    report = {
        "failed": {"j0": "KeyError: 'deliberately_missing'\n--- stderr tail ---\nline\ntail-two\n"},
        "failed_details": {
            "j0": {"reason": "KeyError\n--- stderr tail ---\nline\ntail-two",
                   "failure_log": str(log)},
        },
    }
    result = RunResult(report, job_meta={"j0": {"label": "out.txt"}})
    text = result.format_failures()

    assert "1 job(s) failed" in text
    assert "--- full log ---" in text
    assert "progress: 100%" in text, "full log body (stdout section) missing"
    assert "=== traceback / stderr ===" in text
    # The 15-line truncated tail block is suppressed in favour of the full log.
    assert "Stderr:" not in text


def test_multiple_failures_keep_tail_and_no_full_log(tmp_path):
    """Two+ failures keep the per-job truncated tail and do NOT dump any
    single job's full log (that only makes sense for a lone failure)."""
    from ppg3.run import RunResult

    report = {
        "failed": {
            "a": "Boom\n--- stderr tail ---\nfirst\nsecond",
            "b": "Bang\n--- stderr tail ---\nthird\nfourth",
        },
        "failed_details": {
            "a": {"reason": "Boom\n--- stderr tail ---\nfirst\nsecond"},
            "b": {"reason": "Bang\n--- stderr tail ---\nthird\nfourth"},
        },
    }
    result = RunResult(report, job_meta={"a": {"label": "a.txt"}, "b": {"label": "b.txt"}})
    text = result.format_failures()

    assert "2 job(s) failed" in text
    assert "--- full log ---" not in text
    assert "Stderr:" in text
