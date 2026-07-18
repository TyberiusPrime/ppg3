"""Web frontend for the §6.7 watcher: ``python -m ppg3 webwatch``.

Runs *exactly* the same loop as ``python -m ppg3 watch`` — it calls
:func:`ppg3.watch.run_watch` unchanged, passing an ``on_event`` observer —
and additionally serves a read-only status page over HTTP: current loop
state (running a pass / waiting for changes), per-pass history with
built/hit/failed counts and generation numbers, error drill-down (per-job
failure details from the RunReport: exception summary, log paths, output
dirs, full reason blobs; definition-pass tracebacks), live-updated via
Server-Sent Events. The basic watcher stays available and untouched.

Shaped by the same CONTRACT.md "no third-party dependencies" rule that
made ``watch.py`` poll instead of inotify: stdlib only —
``http.server.ThreadingHTTPServer``, SSE (no websockets in the stdlib,
and SSE auto-reconnects for free), and one embedded vanilla-JS/CSS HTML
page (no build step, ships inside the wheel). The server binds
``127.0.0.1`` by default and is strictly read-only:

- ``GET /``            the status page
- ``GET /api/state``   full state snapshot as JSON
- ``GET /api/events``  SSE stream; every message is a full snapshot

Full-snapshot SSE messages (rather than deltas) are deliberate: the state
is small (run history is bounded at ``_HISTORY_LIMIT``), reconnects are
trivially correct, and a stalled client can simply have intermediate
snapshots dropped (`_broadcast_locked`) with no resync protocol. Phase 2
(browsing previous generations of an output path, live per-job progress)
is planned to work by tailing a runner-written event log in ``.ppg3/`` —
the :data:`ppg3.watch.WatchEvent` dicts consumed here are already
JSON-serializable with that in mind.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, IO, List, Optional, Sequence

from .run import _last_meaningful_line
from .watch import WatchEvent, _print, _timestamp, run_watch

# Bounded per-pass history kept in memory (and shipped in every snapshot).
# Old entries simply fall off; phase 2's on-disk event log is the durable
# record.
_HISTORY_LIMIT = 50

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


def _counts(report: Optional[Dict[str, Any]]) -> Optional[Dict[str, int]]:
    if not report:
        return None
    return {
        "built": len(report.get("built", [])),
        "hits": len(report.get("hits", [])),
        "failed": len(report.get("failed", {})),
    }


def _failure_records(event: WatchEvent) -> List[Dict[str, Any]]:
    """Flatten a ``pass_run_failed`` event's RunResult attributes into one
    self-contained record per failed job — the same fields
    ``RunResult._format_one_failure`` renders (job/runtime/exit/exception/
    log/outputs), plus the full ``reason`` blob for the drill-down view."""
    failed: Dict[str, str] = event.get("failed") or {}
    details: Dict[str, Dict[str, Any]] = event.get("failed_details") or {}
    kinds: Dict[str, str] = event.get("job_kinds") or {}
    records = []
    for job_id in sorted(failed):
        detail = details.get(job_id) or {}
        reason = detail.get("reason") or failed.get(job_id, "")
        exit_code = detail.get("exit_code")
        records.append(
            {
                "job": job_id,
                "kind": kinds.get(job_id),
                "exception": _last_meaningful_line(reason),
                "reason": reason,
                "runtime_ms": detail.get("runtime_ms"),
                # Same CommandJob-only rule as RunResult._format_one_failure:
                # a FileJob's exit code is always 1 and meaningless.
                "exit_code": exit_code if kinds.get(job_id) == "command" else None,
                "log": detail.get("failure_log") or detail.get("log_dir"),
                "out_dir": detail.get("out_dir"),
            }
        )
    return records


class WatchState:
    """Thread-safe recorder of :func:`ppg3.watch.run_watch` events plus
    SSE subscriber fan-out.

    ``handle_event`` is called from the watch loop's (main) thread;
    ``snapshot_json``/``subscribe``/``unsubscribe`` from HTTP handler
    threads — everything mutable sits behind one lock. The state dict is
    only ever exposed as a JSON string, so handler threads can never
    observe (or mutate) a half-updated structure.
    """

    def __init__(self, script: str, interval: float, history_limit: int = _HISTORY_LIMIT):
        self._lock = threading.Lock()
        self._subscribers: List["queue.Queue[str]"] = []
        self._history_limit = history_limit
        self._state: Dict[str, Any] = {
            "script": script,
            "interval": interval,
            "started_at": time.time(),
            # starting -> (running <-> waiting)* -> stopped
            "status": "starting",
            "watched_paths": [],
            "n_runs": 0,
            "n_failures": 0,
            "runs": [],  # newest first, bounded at history_limit
        }

    # ------------------------------------------------------------- events

    def handle_event(self, event: WatchEvent) -> None:
        etype = event.get("type")
        with self._lock:
            if etype == "pass_started":
                run = {
                    "n": event.get("n_run"),
                    "reason": event.get("reason"),
                    "changed_paths": event.get("changed_paths"),
                    "started_at": event.get("ts"),
                    "finished_at": None,
                    "duration_ms": None,
                    "status": "running",
                    "counts": None,
                    "generation": None,
                    "failures": [],
                    "failures_text": None,
                    "traceback": None,
                }
                self._state["runs"].insert(0, run)
                del self._state["runs"][self._history_limit :]
                self._state["n_runs"] = event.get("n_run")
                self._state["status"] = "running"
            elif etype == "pass_ok":
                self._finish_run(
                    event,
                    status="ok",
                    counts=_counts(event.get("report")),
                    generation=event.get("generation"),
                )
            elif etype == "pass_run_failed":
                self._state["n_failures"] += 1
                self._finish_run(
                    event,
                    status="failed",
                    counts=_counts(event.get("report")),
                    failures=_failure_records(event),
                    failures_text=event.get("failures_text"),
                )
            elif etype == "pass_exception":
                self._state["n_failures"] += 1
                self._finish_run(
                    event, status="error", traceback=event.get("traceback")
                )
            elif etype == "waiting":
                self._state["status"] = "waiting"
                self._state["watched_paths"] = event.get("watched_paths") or []
            elif etype == "stopped":
                self._state["status"] = "stopped"
            # Unknown event types are recorded nowhere but still broadcast
            # a fresh snapshot — harmless either way.
            self._broadcast_locked()

    def _finish_run(self, event: WatchEvent, status: str, **fields: Any) -> None:
        runs = self._state["runs"]
        if not runs or runs[0]["status"] != "running":
            # Defensive: a finish event without a matching pass_started
            # (can't happen from run_watch's loop, but a phase-2 log tailer
            # replaying a truncated file could produce it).
            return
        run = runs[0]
        run["status"] = status
        run["finished_at"] = event.get("ts")
        if run["started_at"] is not None and run["finished_at"] is not None:
            run["duration_ms"] = int((run["finished_at"] - run["started_at"]) * 1000)
        run.update(fields)

    # ---------------------------------------------------------- snapshots

    def snapshot_json(self) -> str:
        with self._lock:
            return json.dumps(self._state)

    def _broadcast_locked(self) -> None:
        payload = json.dumps(self._state)
        for q in self._subscribers:
            try:
                q.put_nowait(payload)
            except queue.Full:
                # Slow/stalled client: drop its oldest snapshot to make
                # room — every message is a full snapshot, so skipping
                # intermediates loses nothing once the client catches up.
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    pass

    # -------------------------------------------------------- subscribers

    def subscribe(self) -> "queue.Queue[str]":
        """Register an SSE client; its queue arrives pre-seeded with the
        current snapshot so the client renders immediately on connect."""
        q: "queue.Queue[str]" = queue.Queue(maxsize=16)
        with self._lock:
            q.put_nowait(json.dumps(self._state))
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[str]") -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass


class _WebWatchServer(ThreadingHTTPServer):
    daemon_threads = True  # SSE handler threads must not block interpreter exit
    watch_state: WatchState


class _Handler(BaseHTTPRequestHandler):
    server_version = "ppg3-webwatch"
    protocol_version = "HTTP/1.1"

    server: _WebWatchServer  # narrowed type for attribute access below

    def log_message(self, format: str, *args: Any) -> None:
        # Per-request access logging is noise next to the watch loop's own
        # console output.
        pass

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        path = self.path.split("?", 1)[0]
        if path == "/":
            body = _PAGE_HTML.encode("utf-8")
            self._respond(200, "text/html; charset=utf-8", body)
        elif path == "/api/state":
            body = self.server.watch_state.snapshot_json().encode("utf-8")
            self._respond(200, "application/json", body)
        elif path == "/api/events":
            self._serve_events()
        else:
            self._respond(404, "text/plain; charset=utf-8", b"not found\n")

    def _respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_events(self) -> None:
        """One SSE connection: initial snapshot immediately (the queue is
        pre-seeded by ``subscribe``), then one ``event: state`` message per
        watch event, with comment keepalives while idle so half-dead
        connections get noticed. Runs until the client disconnects (write
        fails) — the handler thread is daemonic, so a client that never
        disconnects still cannot block process exit."""
        state = self.server.watch_state
        q = state.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            # SSE is an unbounded stream: no Content-Length; close delimits.
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                try:
                    payload = q.get(timeout=15.0)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                # json.dumps output is a single line (newlines in strings
                # are escaped), so one data: field carries the whole
                # snapshot.
                self.wfile.write(f"event: state\ndata: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            state.unsubscribe(q)


def start_server(state: WatchState, host: str, port: int) -> _WebWatchServer:
    """Bind and start serving on a daemon thread; returns the server (whose
    ``server_address`` carries the actual port — useful with ``port=0``)."""
    server = _WebWatchServer((host, port), _Handler)
    server.watch_state = state
    thread = threading.Thread(
        target=server.serve_forever,
        name="ppg3-webwatch-http",
        daemon=True,
    )
    thread.start()
    return server


def run_webwatch(
    script: str,
    script_args: Sequence[str] = (),
    interval: float = 0.5,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    stream: Optional[IO[str]] = None,
) -> int:
    """``python -m ppg3 webwatch <script> [args...]``: `run_watch` with the
    HTTP status frontend attached. Same loop, same console output, same
    clean-SIGINT exit (returns 0); the server dies with the loop."""
    import os

    stream = stream or sys.stdout
    state = WatchState(os.path.abspath(script), interval)
    server = start_server(state, host, port)
    shown_host, shown_port = server.server_address[:2]
    _print(
        stream,
        f"[{_timestamp()}] webwatch: serving on http://{shown_host}:{shown_port}/",
    )
    try:
        return run_watch(
            script,
            script_args,
            interval=interval,
            stream=stream,
            on_event=state.handle_event,
        )
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# The page. One self-contained document: no external assets (works with any
# network policy, no build step), vanilla JS, EventSource with its built-in
# auto-reconnect, full re-render per snapshot (expanded rows are re-applied
# from a JS-side set keyed by run number).
# ---------------------------------------------------------------------------

_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ppg3 webwatch</title>
<style>
  :root {
    --bg: #ffffff; --fg: #1a1d21; --muted: #667085; --line: #e4e7ec;
    --card: #f8fafc; --accent: #2563eb;
    --ok: #16a34a; --ok-bg: #dcfce7;
    --failed: #dc2626; --failed-bg: #fee2e2;
    --error: #b45309; --error-bg: #fef3c7;
    --running: #2563eb; --running-bg: #dbeafe;
    --waiting: #475569; --waiting-bg: #e2e8f0;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #101418; --fg: #e6e9ec; --muted: #8b98a5; --line: #2a3138;
      --card: #171c22; --accent: #60a5fa;
      --ok: #4ade80; --ok-bg: #14331f;
      --failed: #f87171; --failed-bg: #3a1518;
      --error: #fbbf24; --error-bg: #362a10;
      --running: #60a5fa; --running-bg: #16283f;
      --waiting: #9fb0bf; --waiting-bg: #232c34;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 14px/1.5 ui-sans-serif, system-ui, sans-serif;
  }
  code, pre, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .wrap { max-width: 980px; margin: 0 auto; padding: 1.2rem 1rem 4rem; }
  header { display: flex; flex-wrap: wrap; align-items: baseline; gap: .6rem 1rem; }
  header h1 { font-size: 1.15rem; margin: 0; }
  header .script { color: var(--muted); font-size: .85rem; word-break: break-all; }
  .toolbar { display: flex; flex-wrap: wrap; gap: .5rem 1.2rem; align-items: center;
             margin: .8rem 0 1.2rem; color: var(--muted); font-size: .85rem; }
  .badge { display: inline-block; padding: .1rem .55rem; border-radius: 999px;
           font-size: .78rem; font-weight: 600; letter-spacing: .02em; }
  .badge.ok       { color: var(--ok);      background: var(--ok-bg); }
  .badge.failed   { color: var(--failed);  background: var(--failed-bg); }
  .badge.error    { color: var(--error);   background: var(--error-bg); }
  .badge.running  { color: var(--running); background: var(--running-bg); }
  .badge.waiting, .badge.starting, .badge.stopped
                  { color: var(--waiting); background: var(--waiting-bg); }
  .badge.running::before { content: "● "; animation: pulse 1.2s ease-in-out infinite; }
  @keyframes pulse { 50% { opacity: .3; } }
  #conn { margin-left: auto; }
  #conn.down { color: var(--failed); }
  .runs { display: flex; flex-direction: column; gap: .5rem; }
  .run { border: 1px solid var(--line); border-radius: 8px; background: var(--card); }
  .run > summary {
    display: flex; flex-wrap: wrap; gap: .4rem .9rem; align-items: baseline;
    padding: .55rem .8rem; cursor: pointer; list-style: none;
  }
  .run > summary::-webkit-details-marker { display: none; }
  .run .n { color: var(--muted); font-size: .8rem; min-width: 2.2rem; }
  .run .when, .run .dur { color: var(--muted); font-size: .8rem; }
  .run .counts { font-size: .85rem; }
  .run .counts .f { color: var(--failed); font-weight: 600; }
  .run .gen { font-size: .8rem; color: var(--accent); }
  .run .reason { flex-basis: 100%; color: var(--muted); font-size: .8rem;
                 word-break: break-all; }
  .detail { border-top: 1px solid var(--line); padding: .7rem .8rem; }
  .detail h3 { margin: .6rem 0 .3rem; font-size: .82rem; text-transform: uppercase;
               letter-spacing: .05em; color: var(--muted); }
  .failure { border: 1px solid var(--line); border-radius: 6px;
             padding: .5rem .7rem; margin: .4rem 0; background: var(--bg); }
  .failure .job { font-weight: 600; word-break: break-all; }
  .failure .meta { color: var(--muted); font-size: .8rem; margin: .15rem 0; }
  .failure .exc { margin: .25rem 0; word-break: break-word; }
  .failure .path { font-size: .8rem; word-break: break-all; }
  pre { background: var(--bg); border: 1px solid var(--line); border-radius: 6px;
        padding: .6rem; overflow-x: auto; font-size: .78rem; max-height: 24rem; }
  details.sub > summary { cursor: pointer; color: var(--muted); font-size: .8rem; }
  ul.paths { margin: .2rem 0; padding-left: 1.2rem; font-size: .8rem; }
  ul.paths li { word-break: break-all; }
  .empty { color: var(--muted); font-style: italic; padding: 1.5rem 0; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>ppg3 webwatch</h1>
    <span id="status" class="badge starting">starting</span>
    <span class="script mono" id="script"></span>
  </header>
  <div class="toolbar">
    <span id="totals"></span>
    <details class="sub" id="watchedbox">
      <summary><span id="watchedcount">0</span> watched paths</summary>
      <ul class="paths mono" id="watched"></ul>
    </details>
    <span id="conn">connecting&hellip;</span>
  </div>
  <div class="runs" id="runs"><div class="empty">no runs yet</div></div>
</div>
<script>
"use strict";
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const expanded = new Set();   // run numbers whose <details> are open
let watchedOpen = false;

function fmtTime(ts) {
  return ts ? new Date(ts * 1000).toLocaleTimeString() : "";
}
function fmtDur(ms) {
  if (ms == null) return "";
  if (ms < 1000) return ms + "ms";
  const s = ms / 1000;
  if (s < 60) return (s >= 10 ? s.toFixed(0) : s.toFixed(1)) + "s";
  return Math.floor(s / 60) + "m" + Math.round(s % 60) + "s";
}

function failureHtml(f) {
  const meta = [];
  if (f.runtime_ms != null) meta.push("runtime " + fmtDur(f.runtime_ms));
  if (f.exit_code != null) meta.push("exit " + esc(f.exit_code));
  if (f.kind) meta.push(esc(f.kind) + " job");
  return `<div class="failure">
    <div class="job mono">${esc(f.job)}</div>
    ${meta.length ? `<div class="meta">${meta.join(" &middot; ")}</div>` : ""}
    ${f.exception ? `<div class="exc mono">${esc(f.exception)}</div>` : ""}
    ${f.log ? `<div class="path">log: <span class="mono">${esc(f.log)}</span></div>` : ""}
    ${f.out_dir ? `<div class="path">outputs: <span class="mono">${esc(f.out_dir)}</span></div>` : ""}
    ${f.reason && f.reason.trim() !== (f.exception || "").trim()
      ? `<details class="sub"><summary>full reason</summary><pre>${esc(f.reason)}</pre></details>`
      : ""}
  </div>`;
}

function runHtml(r) {
  const c = r.counts;
  const countsHtml = c
    ? `<span class="counts">built ${c.built} &middot; hits ${c.hits} &middot; ` +
      `<span class="${c.failed ? "f" : ""}">failed ${c.failed}</span></span>`
    : "";
  const genHtml = r.generation != null
    ? `<span class="gen mono">gen ${esc(r.generation)}</span>` : "";
  const open = expanded.has(r.n) ? " open" : "";
  const changed = (r.changed_paths || []).map(p => `<li>${esc(p)}</li>`).join("");
  return `<details class="run" data-n="${r.n}"${open}>
    <summary>
      <span class="n mono">#${r.n}</span>
      <span class="badge ${esc(r.status)}">${esc(r.status)}</span>
      <span class="when">${fmtTime(r.started_at)}</span>
      <span class="dur">${fmtDur(r.duration_ms)}</span>
      ${countsHtml} ${genHtml}
      <span class="reason mono">${esc(r.reason)}</span>
    </summary>
    <div class="detail">
      ${changed ? `<h3>changed paths</h3><ul class="paths mono">${changed}</ul>` : ""}
      ${r.failures && r.failures.length
        ? `<h3>${r.failures.length} failed job(s) — view left unchanged</h3>` +
          r.failures.map(failureHtml).join("")
        : ""}
      ${r.traceback
        ? `<h3>definition pass traceback</h3><pre>${esc(r.traceback)}</pre>` : ""}
      ${r.status === "ok"
        ? `<h3>result</h3><div>generation ${esc(r.generation)} written</div>` : ""}
      ${r.status === "running" ? `<div class="empty">still running&hellip;</div>` : ""}
    </div>
  </details>`;
}

function render(state) {
  const st = $("status");
  st.textContent = state.status;
  st.className = "badge " + state.status;
  $("script").textContent = state.script;
  document.title = `ppg3 webwatch — ${state.status}`;
  $("totals").textContent =
    `${state.n_runs} run(s), ${state.n_failures} failure(s), ` +
    `interval ${state.interval}s`;
  $("watchedcount").textContent = state.watched_paths.length;
  $("watched").innerHTML =
    state.watched_paths.map(p => `<li>${esc(p)}</li>`).join("");
  $("watchedbox").open = watchedOpen;
  $("runs").innerHTML = state.runs.length
    ? state.runs.map(runHtml).join("")
    : '<div class="empty">no runs yet</div>';
}

document.addEventListener("toggle", ev => {
  const el = ev.target;
  if (el.id === "watchedbox") { watchedOpen = el.open; return; }
  if (el.classList && el.classList.contains("run")) {
    const n = Number(el.dataset.n);
    if (el.open) expanded.add(n); else expanded.delete(n);
  }
}, true);

const es = new EventSource("/api/events");
es.addEventListener("state", ev => {
  $("conn").textContent = "live";
  $("conn").className = "";
  render(JSON.parse(ev.data));
});
es.onerror = () => {
  $("conn").textContent = "disconnected — retrying\\u2026";
  $("conn").className = "down";
};
</script>
</body>
</html>
"""
