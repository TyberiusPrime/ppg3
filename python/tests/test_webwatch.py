"""Tests for ``python -m ppg3 webwatch`` (``python/ppg3/webwatch.py``) and
the ``run_watch`` observer hook it rides on (``watch.py``'s ``on_event``).

Layout, mirroring ``test_watch.py``:

- Unit tests for :class:`ppg3.webwatch.WatchState`: event -> snapshot
  transitions (started/ok/run-failed/exception/waiting/stopped), failure
  flattening, bounded history — no ``_core``, no sockets.
- Unit tests for ``ppg3.watch._emit`` (observer exceptions must never
  kill the loop) and for ``run_watch`` emitting events end-to-end against
  a script that needs no ``_core`` (definition pass raises).
- CLI argv parsing tests for ``ppg3.__main__._parse_webwatch_argv``.
- HTTP tests against a real ``start_server`` on an ephemeral port
  (``port=0``): ``/``, ``/api/state``, 404, and the ``/api/events`` SSE
  stream (initial snapshot + one live update) — no ``_core`` needed.
- An E2E subprocess test (``requires_core``): a real ``python -m ppg3
  webwatch`` process against the same pipeline script as the watch E2E
  tests, asserting generations appear both on disk and in ``/api/state``,
  then clean SIGINT. Same deadline/kill discipline as ``test_watch.py``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from ppg3.watch import _emit, run_watch
from ppg3.webwatch import WatchState, start_server

from conftest import requires_core

# --------------------------------------------------------------------------
# WatchState: event -> snapshot transitions.
# --------------------------------------------------------------------------


def _state_dict(state: WatchState) -> dict:
    return json.loads(state.snapshot_json())


def _started(n, ts=1000.0, reason="initial run", changed=None):
    return {
        "type": "pass_started",
        "ts": ts,
        "n_run": n,
        "reason": reason,
        "changed_paths": changed,
    }


def test_watchstate_initial_snapshot():
    state = WatchState("/tmp/p.py", 0.25)
    snap = _state_dict(state)
    assert snap["script"] == "/tmp/p.py"
    assert snap["interval"] == 0.25
    assert snap["status"] == "starting"
    assert snap["runs"] == []
    assert snap["n_runs"] == 0
    assert snap["n_failures"] == 0


def test_watchstate_ok_pass_lifecycle():
    state = WatchState("p.py", 0.5)
    state.handle_event(_started(1))
    snap = _state_dict(state)
    assert snap["status"] == "running"
    assert snap["runs"][0]["status"] == "running"

    state.handle_event(
        {
            "type": "pass_ok",
            "ts": 1002.5,
            "report": {"built": ["a", "b"], "hits": ["c"], "failed": {}},
            "generation": 7,
        }
    )
    state.handle_event({"type": "waiting", "ts": 1002.6, "watched_paths": ["p.py", "x"]})

    snap = _state_dict(state)
    assert snap["status"] == "waiting"
    assert snap["watched_paths"] == ["p.py", "x"]
    run = snap["runs"][0]
    assert run["status"] == "ok"
    assert run["counts"] == {"built": 2, "hits": 1, "failed": 0}
    assert run["generation"] == 7
    assert run["duration_ms"] == 2500
    assert snap["n_failures"] == 0


def test_watchstate_run_failed_flattens_failure_records():
    state = WatchState("p.py", 0.5)
    state.handle_event(_started(1))
    state.handle_event(
        {
            "type": "pass_run_failed",
            "ts": 1001.0,
            "report": {"built": [], "hits": [], "failed": {"jobA": "boom"}},
            "failed": {"jobA": "boom", "jobB": "also boom"},
            "failed_details": {
                "jobA": {
                    "reason": "line1\nException: it broke",
                    "failure_log": "/logs/a/failure.log",
                    "exit_code": 3,
                    "runtime_ms": 42,
                    "out_dir": "/out/a",
                }
                # jobB deliberately absent: older-core "no details" path.
            },
            "job_kinds": {"jobA": "command", "jobB": "file"},
            "failures_text": "2 job(s) failed: ...",
        }
    )
    snap = _state_dict(state)
    assert snap["n_failures"] == 1
    run = snap["runs"][0]
    assert run["status"] == "failed"
    assert run["failures_text"] == "2 job(s) failed: ..."
    rec_a, rec_b = run["failures"]  # sorted by job id
    assert rec_a["job"] == "jobA"
    assert rec_a["exception"] == "it broke"
    assert rec_a["exit_code"] == 3  # command job: exit code surfaced
    assert rec_a["log"] == "/logs/a/failure.log"
    assert rec_a["out_dir"] == "/out/a"
    assert rec_a["runtime_ms"] == 42
    assert rec_b["job"] == "jobB"
    assert rec_b["reason"] == "also boom"  # falls back to `failed` value
    assert rec_b["exit_code"] is None  # file job: exit code suppressed
    assert rec_b["log"] is None


def test_watchstate_definition_exception():
    state = WatchState("p.py", 0.5)
    state.handle_event(_started(1))
    state.handle_event(
        {"type": "pass_exception", "ts": 1001.0, "traceback": "Traceback ...\nBoom\n"}
    )
    snap = _state_dict(state)
    assert snap["n_failures"] == 1
    assert snap["runs"][0]["status"] == "error"
    assert "Boom" in snap["runs"][0]["traceback"]


def test_watchstate_history_is_bounded_newest_first():
    state = WatchState("p.py", 0.5, history_limit=3)
    for n in range(1, 6):
        state.handle_event(_started(n, ts=1000.0 + n))
        state.handle_event({"type": "pass_ok", "ts": 1000.5 + n, "report": {}, "generation": n})
    snap = _state_dict(state)
    assert [r["n"] for r in snap["runs"]] == [5, 4, 3]
    assert snap["n_runs"] == 5


def test_watchstate_stopped():
    state = WatchState("p.py", 0.5)
    state.handle_event({"type": "stopped", "ts": 1.0, "n_runs": 2, "n_failures": 1})
    assert _state_dict(state)["status"] == "stopped"


def test_watchstate_finish_without_started_is_ignored():
    # Can't happen from run_watch's loop; guards a phase-2 log tailer
    # replaying a truncated event file.
    state = WatchState("p.py", 0.5)
    state.handle_event({"type": "pass_ok", "ts": 1.0, "report": {}, "generation": 1})
    assert _state_dict(state)["runs"] == []


def test_watchstate_subscriber_gets_initial_snapshot_and_updates():
    state = WatchState("p.py", 0.5)
    q = state.subscribe()
    first = json.loads(q.get_nowait())
    assert first["status"] == "starting"

    state.handle_event(_started(1))
    second = json.loads(q.get_nowait())
    assert second["status"] == "running"

    state.unsubscribe(q)
    state.handle_event({"type": "waiting", "ts": 2.0, "watched_paths": []})
    assert q.empty()  # unsubscribed: no further snapshots


# --------------------------------------------------------------------------
# watch.py observer hook.
# --------------------------------------------------------------------------


def test_emit_swallows_observer_exceptions(capsys):
    def broken_observer(event):
        raise RuntimeError("observer bug")

    _emit(broken_observer, "waiting", watched_paths=[])  # must not raise
    assert "observer bug" in capsys.readouterr().err


def test_emit_none_observer_is_noop():
    _emit(None, "waiting", watched_paths=[])


def test_run_watch_emits_events_for_failing_definition_pass(tmp_path, capsys):
    """Drive one real run_watch iteration (script raises -> pass_exception,
    then waiting), then stop the loop by raising KeyboardInterrupt from the
    observer once 'waiting' has been seen — no _core, no subprocess."""
    script = tmp_path / "broken.py"
    script.write_text("raise ValueError('deliberately broken')\n")
    events = []

    def observer(event):
        events.append(event)
        if event["type"] == "waiting":
            raise KeyboardInterrupt  # propagates: _emit only swallows Exception

    rc = run_watch(str(script), interval=0.01, on_event=observer)
    assert rc == 0
    types = [e["type"] for e in events]
    assert types == ["pass_started", "pass_exception", "waiting", "stopped"]
    started = events[0]
    assert started["n_run"] == 1
    assert started["reason"] == "initial run"
    assert started["changed_paths"] is None
    assert "deliberately broken" in events[1]["traceback"]
    assert str(script) in events[2]["watched_paths"]
    assert events[3] == {
        "type": "stopped",
        "ts": events[3]["ts"],
        "n_runs": 1,
        "n_failures": 1,
    }
    # Every event must be JSON-serializable (phase 2 writes them as JSONL).
    json.dumps(events)
    capsys.readouterr()  # swallow the loop's console output + traceback


# --------------------------------------------------------------------------
# CLI argv parsing (python/ppg3/__main__.py).
# --------------------------------------------------------------------------


def test_parse_webwatch_argv_defaults():
    from ppg3.__main__ import _parse_webwatch_argv

    script, script_args, interval, host, port = _parse_webwatch_argv(["s.py", "a"])
    assert (script, script_args, interval) == ("s.py", ["a"], 0.5)
    assert host == "127.0.0.1"
    assert port == 8787


def test_parse_webwatch_argv_flags_anywhere():
    from ppg3.__main__ import _parse_webwatch_argv

    script, script_args, interval, host, port = _parse_webwatch_argv(
        ["--port", "0", "s.py", "x", "--interval=0.1", "y", "--host", "0.0.0.0"]
    )
    assert script == "s.py"
    assert script_args == ["x", "y"]
    assert interval == 0.1
    assert host == "0.0.0.0"
    assert port == 0


def test_parse_webwatch_argv_invalid_port_raises():
    from ppg3.__main__ import _parse_webwatch_argv

    with pytest.raises(SystemExit):
        _parse_webwatch_argv(["s.py", "--port", "not-a-port"])


def test_parse_watch_argv_still_works_and_leaves_unknown_flags_alone():
    # The shared flag extractor must not have changed watch's behavior:
    # unknown --flags (including --port) belong to the *script*.
    from ppg3.__main__ import _parse_watch_argv

    script, script_args, interval = _parse_watch_argv(
        ["--interval", "0.3", "s.py", "--port", "1234"]
    )
    assert script == "s.py"
    assert script_args == ["--port", "1234"]
    assert interval == 0.3


# --------------------------------------------------------------------------
# HTTP server (no _core): real sockets on an ephemeral port.
# --------------------------------------------------------------------------


@pytest.fixture()
def served_state():
    state = WatchState("/tmp/pipeline.py", 0.5)
    server = start_server(state, "127.0.0.1", 0)
    host, port = server.server_address[:2]
    try:
        yield state, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _get(url: str) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, resp.read()


def test_http_serves_page_state_and_404(served_state):
    state, base = served_state
    status, body = _get(base + "/")
    assert status == 200
    assert b"ppg3 webwatch" in body

    state.handle_event(_started(1))
    status, body = _get(base + "/api/state")
    assert status == 200
    snap = json.loads(body)
    assert snap["script"] == "/tmp/pipeline.py"
    assert snap["status"] == "running"

    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(base + "/nope")
    assert excinfo.value.code == 404


def test_http_sse_stream_initial_snapshot_then_live_update(served_state):
    state, base = served_state
    resp = urllib.request.urlopen(base + "/api/events", timeout=10)
    try:

        def read_frame():
            """Read one SSE frame: skip comment/blank lines, return the
            payload of the first `data:` line following an `event: state`."""
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                line = resp.readline().decode("utf-8").rstrip("\n")
                if line.startswith("data: "):
                    return json.loads(line[len("data: ") :])
            raise AssertionError("no SSE data frame within deadline")

        first = read_frame()
        assert first["status"] == "starting"

        state.handle_event(_started(1))
        second = read_frame()
        assert second["status"] == "running"
        assert second["runs"][0]["n"] == 1
    finally:
        resp.close()


# --------------------------------------------------------------------------
# E2E: a real `python -m ppg3 webwatch` subprocess (same pipeline as the
# watch E2E tests).
# --------------------------------------------------------------------------

_DEADLINE = 30.0
_POLL = 0.05


def _wait_until(predicate, deadline=_DEADLINE, poll=_POLL, description="condition"):
    start = time.monotonic()
    while time.monotonic() - start < deadline:
        if predicate():
            return
        time.sleep(poll)
    raise AssertionError(f"timed out after {deadline}s waiting for: {description}")


def _write_pipeline_script(path):
    path.write_text(
        "import sys\n"
        "import ppg3\n"
        "from ppg3.tools import PyEnv\n"
        "\n"
        "leaf_path, store_dir, project_dir, cat_bin = sys.argv[1:5]\n"
        "g = ppg3.new(\n"
        "    stores=[ppg3.Store('main', store_dir)],\n"
        "    default_python=PyEnv.current(),\n"
        "    project_dir=project_dir,\n"
        "    frozen=False,\n"
        "    paranoid=True,\n"
        ")\n"
        "ppg3.CommandJob(\n"
        "    view={'out': 'out.txt'},\n"
        "    argv=['/bin/sh', '-c', cat_bin + ' ' + leaf_path + ' > {out:out}'],\n"
        "    inputs={'leaf': ppg3.File(leaf_path)},\n"
        ")\n"
        "ppg3.run(g, project_id='webwatch-e2e')\n"
    )


@requires_core
def test_e2e_webwatch_serves_state_over_http_and_clean_sigint(tmp_path):
    cat_bin = shutil.which("cat") or "/bin/cat"
    script = tmp_path / "pipeline.py"
    _write_pipeline_script(script)
    leaf = tmp_path / "leaf.txt"
    leaf.write_text("v1\n")
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    project_dir = tmp_path / ".ppg3"
    views_dir = project_dir / "views"

    # The subprocess must be able to `import ppg3` even when the package is
    # only importable via conftest's sys.path insertion (dev checkout, no
    # `maturin develop`/pip install) — prepend the checkout's python/ dir.
    env = dict(os.environ)
    python_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = python_dir + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ppg3",
            "webwatch",
            str(script),
            "--interval",
            "0.1",
            "--port",
            "0",  # ephemeral port; parsed back out of the serving line
            str(leaf),
            str(store_dir),
            str(project_dir),
            cat_bin,
        ],
        cwd=str(tmp_path),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout_lines: list[str] = []

    def _drain_stdout():
        for line in proc.stdout:
            stdout_lines.append(line)

    reader = threading.Thread(target=_drain_stdout, daemon=True)
    reader.start()

    def _base_url():
        for line in stdout_lines:
            m = re.search(r"serving on (http://127\.0\.0\.1:\d+)/", line)
            if m:
                return m.group(1)
        return None

    def _api_state():
        with urllib.request.urlopen(_base_url() + "/api/state", timeout=5) as resp:
            return json.loads(resp.read())

    try:
        _wait_until(lambda: _base_url() is not None, description="serving line on stdout")
        _wait_until(
            lambda: (views_dir / "1").is_dir(),
            description="generation 1 to appear",
        )
        _wait_until(
            lambda: any(r.get("generation") == 1 for r in _api_state()["runs"]),
            description="generation 1 in /api/state",
        )
        snap = _api_state()
        assert snap["script"] == str(script)
        assert str(leaf) in snap["watched_paths"]
        ok_run = next(r for r in snap["runs"] if r.get("generation") == 1)
        assert ok_run["status"] == "ok"
        assert ok_run["counts"]["failed"] == 0

        # --- modify the leaf input: a second generation must show up over
        # HTTP as well as on disk.
        leaf.write_text("v2\n")
        _wait_until(
            lambda: (views_dir / "2").is_dir(),
            description="generation 2 to appear",
        )
        _wait_until(
            lambda: any(r.get("generation") == 2 for r in _api_state()["runs"]),
            description="generation 2 in /api/state",
        )

        # --- clean SIGINT exit, same contract as plain watch.
        proc.send_signal(signal.SIGINT)
        try:
            returncode = proc.wait(timeout=_DEADLINE)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError("process did not exit within deadline after SIGINT")
        assert returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        reader.join(timeout=5)
