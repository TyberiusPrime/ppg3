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
import urllib.parse
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


def test_failure_records_carry_defsite_missing_and_kept(tmp_path):
    # The real post-fix event shape: no job_kinds — kind/defsite ride
    # inside failed_details (RunResult's graph enrichment), and the CLI's
    # Missing:/Kept: fields must survive into the web record.
    out_dir = tmp_path / "data"
    out_dir.mkdir()
    (out_dir / "out.txt").write_text("hello\n")
    state = WatchState("p.py", 0.5)
    state.handle_event(_started(1))
    state.handle_event(
        {
            "type": "pass_run_failed",
            "ts": 1001.0,
            "report": {"built": [], "hits": [], "failed": {"out.txt": "..."}},
            "failed": {"out.txt": 'cached entry is missing declared output(s) "shu"'},
            "failed_details": {
                "out.txt": {
                    "reason": 'cached entry is missing declared output(s) "shu"',
                    "missing_outputs": ["shu"],
                    "out_dir": str(out_dir),
                    "kind": "file",
                    "defsite": "/home/u/pipeline.py:41",
                    "exit_code": 1,
                }
            },
            "failures_text": "1 job(s) failed: ...",
        }
    )
    (rec,) = _state_dict(state)["runs"][0]["failures"]
    assert rec["defsite"] == "/home/u/pipeline.py:41"
    assert rec["missing"] == ["shu"]
    assert rec["kept"] == [str(out_dir / "out.txt")]
    assert rec["kind"] == "file"
    assert rec["exit_code"] is None  # non-command kind from the detail itself


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


def test_http_source_serves_only_state_referenced_files(served_state, tmp_path):
    state, base = served_state
    secret = tmp_path / "secret.txt"
    secret.write_text("do not serve\n")
    src = tmp_path / "pipe.py"
    src.write_text("line one\nline <two> & three\n")

    def source_url(p):
        return base + "/source?path=" + urllib.parse.quote(str(p), safe="")

    # Not referenced by any state: 404, even though the file exists.
    for url in (source_url(secret), base + "/source"):
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _get(url)
        assert excinfo.value.code == 404

    # A watched path becomes browsable...
    state.handle_event(
        {"type": "waiting", "ts": 1.0, "watched_paths": [str(src)]}
    )
    status, body = _get(source_url(src))
    assert status == 200
    text = body.decode("utf-8")
    assert 'id="L2"' in text  # per-line anchors for #L<n> links
    assert "line &lt;two&gt; &amp; three" in text  # HTML-escaped content
    # ...but the sibling still is not.
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(source_url(secret))
    assert excinfo.value.code == 404


def test_http_source_allows_defsite_and_log_from_failures(served_state, tmp_path):
    state, base = served_state
    defsite_file = tmp_path / "pipeline.py"
    defsite_file.write_text("x = 1\n")
    log = tmp_path / "failure.log"
    log.write_text("traceback...\n")
    state.handle_event(_started(1))
    state.handle_event(
        {
            "type": "pass_run_failed",
            "ts": 2.0,
            "report": {"failed": {"a": "boom"}},
            "failed": {"a": "boom"},
            "failed_details": {
                "a": {
                    "reason": "boom",
                    "defsite": f"{defsite_file}:1",
                    "failure_log": str(log),
                }
            },
            "failures_text": "1 job(s) failed",
        }
    )
    for p in (defsite_file, log):
        status, _body = _get(
            base + "/source?path=" + urllib.parse.quote(str(p), safe="")
        )
        assert status == 200


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


# --------------------------------------------------------------------------
# Phase 2: live per-job state from the scheduler's JSONL event log.
# --------------------------------------------------------------------------


def _runner(etype, **fields):
    return {"type": etype, "ts_ms": fields.pop("ts_ms", 1000), **fields}


def test_live_state_full_run_lifecycle():
    state = WatchState("p.py", 0.5)
    state.handle_runner_events(
        [
            _runner("run_started", run_id="run-1-abc", total=3, ts_ms=1000),
            _runner("job_started", job="a", ts_ms=1010),
            _runner("job_started", job="b", ts_ms=1010),
        ]
    )
    live = _state_dict(state)["live"]
    assert live["run_id"] == "run-1-abc"
    assert live["total"] == 3
    assert live["counts"] == {"done": 0, "failed": 0, "running": 2, "pending": 1}
    assert live["jobs"]["a"]["status"] == "running"

    state.handle_runner_events(
        [
            _runner("job_finished", job="a", outcome="built", total=3, ts_ms=1500),
            _runner("job_finished", job="b", outcome="hit", total=3, ts_ms=1600),
            _runner("job_started", job="c", ts_ms=1600),
            _runner("job_finished", job="c", outcome="built", total=3, ts_ms=1900),
            _runner("run_finished", built=2, hits=1, failed=0, ts_ms=1900),
        ]
    )
    live = _state_dict(state)["live"]
    assert live["finished"] is True
    assert live["counts"] == {"done": 3, "failed": 0, "running": 0, "pending": 0}
    assert live["jobs"]["a"]["runtime_ms"] == 490
    assert live["jobs"]["b"]["outcome"] == "hit"


def test_live_state_failure_carries_detail_immediately():
    state = WatchState("p.py", 0.5)
    state.handle_runner_events(
        [
            _runner("run_started", run_id="r", total=2, ts_ms=1),
            _runner("job_started", job="boom", ts_ms=2),
            _runner(
                "job_failed",
                job="boom",
                reason='job "boom" exited with code 2\n--- stderr tail ---\nkapow',
                detail={
                    "reason": 'job "boom" exited with code 2\n--- stderr tail ---\nkapow',
                    "failure_log": "/logs/boom/failure.log",
                    "exit_code": 2,
                    "runtime_ms": 17,
                    "out_dir": "/staging/x/data",
                    "log_dir": "/logs/boom",
                },
                total=2,
                ts_ms=20,
            ),
            # Cascade failure: bare detail, never started.
            _runner(
                "job_failed",
                job="downstream",
                reason="upstream failed: boom",
                detail={"reason": "upstream failed: boom"},
                total=2,
                ts_ms=21,
            ),
        ]
    )
    live = _state_dict(state)["live"]
    assert live["counts"] == {"done": 0, "failed": 2, "running": 0, "pending": 0}
    boom = live["jobs"]["boom"]
    assert boom["status"] == "failed"
    assert boom["exception"] == "kapow"
    assert boom["log"] == "/logs/boom/failure.log"
    assert boom["out_dir"] == "/staging/x/data"
    assert boom["exit_code"] == 2
    assert boom["runtime_ms"] == 17
    down = live["jobs"]["downstream"]
    assert down["status"] == "failed"
    assert down["reason"] == "upstream failed: boom"


def test_live_failure_joins_job_meta_and_missing_outputs(tmp_path):
    # The event log speaks internal ids; ppg3.run() stashes an id ->
    # label/kind/defsite map before the run starts (last-run-info slot) and
    # WatchState joins it so live failures render like the CLI's block.
    out_dir = tmp_path / "data"
    out_dir.mkdir()
    (out_dir / "out.txt").write_text("x")
    meta = {
        "j123": {"label": "out.txt", "kind": "file", "defsite": "/x/p.py:41"},
    }
    state = WatchState("p.py", 0.5, job_meta_provider=lambda: meta)
    state.handle_runner_events(
        [
            _runner("run_started", run_id="r", total=1, ts_ms=1),
            _runner("job_started", job="j123", ts_ms=2),
            _runner(
                "job_failed",
                job="j123",
                reason='cached entry is missing declared output(s) "shu"',
                detail={
                    "reason": 'cached entry is missing declared output(s) "shu"',
                    "missing_outputs": ["shu"],
                    "out_dir": str(out_dir),
                    "exit_code": 1,
                },
                total=1,
                ts_ms=3,
            ),
        ]
    )
    job = _state_dict(state)["live"]["jobs"]["j123"]
    assert job["label"] == "out.txt"
    assert job["defsite"] == "/x/p.py:41"
    assert job["kind"] == "file"
    assert job["missing"] == ["shu"]
    assert job["kept"] == [str(out_dir / "out.txt")]
    assert job["exit_code"] is None  # known non-command kind: suppressed


def test_live_failure_without_meta_keeps_exit_code_and_id():
    # No meta (e.g. a GraphJob-expanded job mid-run): render by id, keep
    # the exit code (kind unknown — better shown than wrongly hidden).
    state = WatchState("p.py", 0.5, job_meta_provider=lambda: {})
    state.handle_runner_events(
        [
            _runner("run_started", run_id="r", total=1, ts_ms=1),
            _runner(
                "job_failed",
                job="jXYZ",
                reason="boom",
                detail={"reason": "boom", "exit_code": 2},
                total=1,
                ts_ms=2,
            ),
        ]
    )
    job = _state_dict(state)["live"]["jobs"]["jXYZ"]
    assert job.get("label") is None
    assert job["exit_code"] == 2
    assert job["missing"] == []


def test_live_state_new_run_resets():
    state = WatchState("p.py", 0.5)
    state.handle_runner_events(
        [
            _runner("run_started", run_id="r1", total=1, ts_ms=1),
            _runner("job_started", job="a", ts_ms=2),
            _runner("job_failed", job="a", reason="x", detail={"reason": "x"}, total=1, ts_ms=3),
            _runner("run_finished", built=0, hits=0, failed=1, ts_ms=3),
            _runner("run_started", run_id="r2", total=1, ts_ms=10),
            _runner("job_started", job="a", ts_ms=11),
        ]
    )
    live = _state_dict(state)["live"]
    assert live["run_id"] == "r2"
    assert live["finished"] is False
    assert live["jobs"]["a"]["status"] == "running"
    assert live["counts"]["failed"] == 0


def test_live_state_graph_expansion_grows_total():
    state = WatchState("p.py", 0.5)
    state.handle_runner_events(
        [
            _runner("run_started", run_id="r", total=1, ts_ms=1),
            _runner("job_started", job="expander", ts_ms=2),
            _runner("job_finished", job="expander", outcome="graph_expanded", total=3, ts_ms=3),
        ]
    )
    live = _state_dict(state)["live"]
    assert live["total"] == 3
    assert live["counts"]["pending"] == 2


# --------------------------------------------------------------------------
# EventLogTailer (no _core): real files, injected path provider.
# --------------------------------------------------------------------------


def _wait_for(predicate, deadline=5.0, poll=0.02, description="condition"):
    start = time.monotonic()
    while time.monotonic() - start < deadline:
        if predicate():
            return
        time.sleep(poll)
    raise AssertionError(f"timed out waiting for: {description}")


def test_tailer_follows_appends_and_run_boundary_path_change(tmp_path):
    from ppg3.webwatch import EventLogTailer

    state = WatchState("p.py", 0.5)
    log1 = tmp_path / "run-events-1.jsonl"
    log1.write_text(
        json.dumps({"type": "run_started", "run_id": "r1", "total": 1, "ts_ms": 1}) + "\n"
    )
    current = {"path": str(log1)}
    tailer = EventLogTailer(state, path_provider=lambda: current["path"], poll=0.02)
    tailer.start()
    try:
        _wait_for(
            lambda: _state_dict(state)["live"]["run_id"] == "r1",
            description="tailer picked up r1",
        )

        # Appends are picked up (including a torn write completed later,
        # and a malformed line that must be skipped).
        with open(log1, "a") as fh:
            fh.write(json.dumps({"type": "job_started", "job": "a", "ts_ms": 2}) + "\n")
            fh.write("{this is not json}\n")
            fh.write('{"type": "job_finished", "job": "a", "outc')  # torn
            fh.flush()
        _wait_for(
            lambda: _state_dict(state)["live"]["jobs"].get("a", {}).get("status")
            == "running",
            description="job a running via append",
        )
        with open(log1, "a") as fh:
            fh.write('ome": "built", "total": 1, "ts_ms": 5}\n')  # completes the torn line
        _wait_for(
            lambda: _state_dict(state)["live"]["jobs"]["a"].get("status") == "done",
            description="torn line completed and parsed",
        )

        # New run = new file; the path change alone must reset cleanly.
        log2 = tmp_path / "run-events-2.jsonl"
        log2.write_text(
            json.dumps({"type": "run_started", "run_id": "r2", "total": 2, "ts_ms": 10}) + "\n"
        )
        current["path"] = str(log2)
        _wait_for(
            lambda: _state_dict(state)["live"]["run_id"] == "r2",
            description="tailer switched to r2",
        )
        assert _state_dict(state)["live"]["jobs"] == {}
    finally:
        tailer.stop()
        tailer.join(timeout=5)


def test_tailer_tolerates_missing_file_until_it_appears(tmp_path):
    from ppg3.webwatch import EventLogTailer

    state = WatchState("p.py", 0.5)
    log = tmp_path / "not-yet.jsonl"
    tailer = EventLogTailer(state, path_provider=lambda: str(log), poll=0.02)
    tailer.start()
    try:
        time.sleep(0.1)  # a few polls against the missing file: no crash
        assert tailer.is_alive()
        log.write_text(
            json.dumps({"type": "run_started", "run_id": "late", "total": 0, "ts_ms": 1}) + "\n"
        )
        _wait_for(
            lambda: _state_dict(state)["live"]["run_id"] == "late",
            description="tailer picked up the late-appearing file",
        )
    finally:
        tailer.stop()
        tailer.join(timeout=5)


def _write_live_pipeline_script(path):
    """Two independent jobs: one fails immediately (nonzero exit), one
    sleeps then writes into a *nested* view path without mkdir'ing it —
    covering both the live failure-drill-down contract and the
    scheduler-side view-path parent-dir creation, through the real
    executor."""
    sleep_bin = shutil.which("sleep") or "/bin/sleep"
    cat_bin = shutil.which("cat") or "/bin/cat"
    path.write_text(
        "import sys\n"
        "import ppg3\n"
        "from ppg3.tools import PyEnv\n"
        "\n"
        "leaf_path, store_dir, project_dir, sleep_s = sys.argv[1:5]\n"
        "g = ppg3.new(\n"
        "    stores=[ppg3.Store('main', store_dir)],\n"
        "    default_python=PyEnv.current(),\n"
        "    project_dir=project_dir,\n"
        "    frozen=False,\n"
        "    paranoid=True,\n"
        ")\n"
        "ppg3.CommandJob(\n"
        "    view={'out': 'results/failed.txt'},\n"
        "    argv=['/bin/sh', '-c', 'echo doomed >&2; exit 3'],\n"
        "    inputs={'leaf': ppg3.File(leaf_path)},\n"
        ")\n"
        "ppg3.CommandJob(\n"
        "    view={'out': 'results/slow.txt'},\n"
        f"    argv=['/bin/sh', '-c', '{sleep_bin} ' + sleep_s + ' && {cat_bin} ' + leaf_path + ' > {{out:out}}'],\n"
        "    inputs={'leaf': ppg3.File(leaf_path)},\n"
        ")\n"
        "ppg3.run(g, project_id='webwatch-live-e2e')\n"
    )


@requires_core
def test_e2e_webwatch_live_progress_and_immediate_failure(tmp_path):
    script = tmp_path / "pipeline.py"
    _write_live_pipeline_script(script)
    leaf = tmp_path / "leaf.txt"
    leaf.write_text("v1\n")
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    project_dir = tmp_path / ".ppg3"

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
            "0",
            str(leaf),
            str(store_dir),
            str(project_dir),
            "4",  # slow job sleeps 4s: the window in which the fast failure must already be visible
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

    def _failed_visible_mid_run():
        live = _api_state()["live"]
        job = live["jobs"].get("results/failed.txt")
        return (
            job is not None
            and job.get("status") == "failed"
            and not live.get("finished")
        )

    try:
        _wait_until(lambda: _base_url() is not None, description="serving line on stdout")
        # THE phase-2 contract: the failed job (detail included) is visible
        # over HTTP while the run is still executing the slow sibling.
        _wait_until(_failed_visible_mid_run, description="failure visible mid-run")
        live = _api_state()["live"]
        failed_job = live["jobs"]["results/failed.txt"]
        assert failed_job["exit_code"] == 3
        assert failed_job["log"], "failure log path must be available immediately"
        assert "doomed" in (failed_job["reason"] or "")
        assert live["total"] == 2

        # The slow sibling finishes (its nested `results/` view path is
        # auto-created by the scheduler now — no job-side mkdir), then the
        # run ends failed and the pass lands in the run history.
        _wait_until(
            lambda: _api_state()["live"].get("finished") is True,
            description="run_finished in live state",
        )
        live = _api_state()["live"]
        assert live["jobs"]["results/slow.txt"]["status"] == "done"
        assert live["counts"] == {"done": 1, "failed": 1, "running": 0, "pending": 0}
        _wait_until(
            lambda: any(r["status"] == "failed" for r in _api_state()["runs"]),
            description="failed pass in run history",
        )

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
