"""``ppg3.run()`` — lowers a ``Graph`` to ``JobDef`` JSON, calls
``ppg3._core.run``, then assembles a ``ViewSpec`` and calls
``ppg3._core.write_generation`` (CONTRACT.md "PyO3 boundary" / "Python
package").

This module is the one place in the package that touches the compiled
extension (via :mod:`ppg3._bridge`).
"""

from __future__ import annotations

import contextlib
import json
import re
import os
import sys
from typing import Any, Dict, List, Optional

from ._bridge import get_core
from .io import JobIO
from .jobs import File, Graph, GraphJob, Params, UnsandboxedJob


# error-stack Debug-render decorations + backtrace noise we skip when
# extracting a one-line summary from a reason blob.
_DECOR_PREFIXES = ("╰╴", "├╴", "│", "╰─▶", "├─▶", "===", "---", "━", "backtrace")


def _is_noise(line: str) -> bool:
    if line.startswith(_DECOR_PREFIXES):
        return True
    # error-stack location frames ("at crates/...:8:9") and numbered
    # backtrace frames ("  12: some::symbol").
    if line.startswith("at ") and (".rs:" in line or "/rustc/" in line):
        return True
    head = line.split(":", 1)[0]
    if head.isdigit():
        return True
    return False


def _last_meaningful_line(text: str) -> str:
    """A one-line error summary from a reason/traceback blob: the final
    exception line if we can find one (our rich formatter's ``Exception: …``
    or a trailing ``Type: value``), else the last substantive line — skipping
    error-stack tree/backtrace decoration and section headers."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    lines = [ln for ln in lines if not _is_noise(ln)]
    if not lines:
        return ""
    for ln in reversed(lines):
        if ln.startswith("Exception: "):
            return ln[len("Exception: "):].strip()
    return lines[-1]


def _fmt_duration(ms: Optional[int]) -> str:
    """Human-friendly wall-clock duration from milliseconds."""
    if ms is None:
        return "?"
    ms = max(0, int(ms))
    if ms < 1000:
        return f"{ms}ms"
    s = ms / 1000.0
    if s < 60:
        return f"{s:.0f}s" if s >= 10 else f"{s:.1f}s"
    m, rem = divmod(int(round(s)), 60)
    return f"{m}m{rem}s"


def _job_meta_from_graph(graph) -> Dict[str, Dict[str, str]]:
    """id -> {"label", "kind", "defsite"} for report translation: internal
    ids never reach the user (PRINCIPLES.md P1.5) — every job is shown by
    its published destinations (or definition site for internal jobs)."""
    meta: Dict[str, Dict[str, str]] = {}
    seen_labels: Dict[str, int] = {}
    for jid, job in graph.jobs.items():
        label = getattr(job, "label", jid)
        # Labels can collide (e.g. two internal jobs defined on one line in
        # a loop); disambiguate so translated dicts never silently merge.
        n = seen_labels.get(label, 0)
        seen_labels[label] = n + 1
        if n:
            label = f"{label}#{n + 1}"
        meta[jid] = {
            "label": label,
            "kind": getattr(job, "kind", ""),
            "defsite": getattr(job, "defsite", ""),
        }
    return meta


# The scheduler's cascade marker (core/src/scheduler.rs `fail_job`): a job
# that never ran because a parent failed gets exactly this reason shape,
# with the *internal id* of the immediate failed parent.
_UPSTREAM_PREFIX = "upstream failed: "


def _cascade_parent_id(reason: str) -> Optional[str]:
    if reason.startswith(_UPSTREAM_PREFIX):
        return reason[len(_UPSTREAM_PREFIX):].strip()
    return None


class RunResult:
    """The outcome of a :func:`run` call. ``built``/``hits``/``failed`` are
    keyed by job *label* (published destinations, or `<kind @ file:line>`
    for internal jobs) — never by internal ids (P1.5). ``.raw`` is the full
    decoded ``RunReport`` JSON, untranslated."""

    def __init__(
        self,
        report: Dict[str, Any],
        generation: Optional[int] = None,
        job_meta: Optional[Dict[str, Dict[str, str]]] = None,
        partial_dir: Optional[str] = None,
    ):
        self.job_meta: Dict[str, Dict[str, str]] = dict(job_meta or {})

        def _label(jid: str) -> str:
            return self.job_meta.get(jid, {}).get("label", jid)

        raw_failed: Dict[str, str] = dict(report.get("failed", {}))

        def _root_id(jid: str) -> str:
            """Follow `upstream failed: <id>` chains to the job that
            actually failed (the root cause). Cycle/missing-id safe."""
            seen = {jid}
            cur = jid
            while True:
                parent = _cascade_parent_id(raw_failed.get(cur, ""))
                if parent is None:
                    return cur
                if parent in seen or parent not in raw_failed:
                    return parent
                seen.add(parent)
                cur = parent

        # label -> root-cause label, for every job that did not run because
        # an upstream (transitively) failed. Basis for the aggregated
        # "did not run" section of format_failures and for `ppg3 why`.
        self.upstream_failed: Dict[str, str] = {}
        translated_failed: Dict[str, str] = {}
        for jid, reason in raw_failed.items():
            if _cascade_parent_id(reason) is not None:
                root = _root_id(jid)
                self.upstream_failed[_label(jid)] = _label(root)
                reason = f"did not run — upstream job {_label(root)} failed"
            translated_failed[_label(jid)] = reason
        self.built: List[str] = sorted(_label(j) for j in report.get("built", []))
        self.hits: List[str] = sorted(_label(j) for j in report.get("hits", []))
        self.failed: Dict[str, str] = translated_failed
        # label -> {"reason", "log_dir", "failure_log", "exit_code",
        # "out_dir", "runtime_ms", "missing_outputs", "kind", "defsite",
        # "upstream"} (see the Rust `FailedJob`, enriched with graph
        # metadata and the resolved cascade root).
        self.failed_details: Dict[str, Dict[str, Any]] = {}
        for jid, detail in dict(report.get("failed_details", {})).items():
            enriched = dict(detail)
            enriched["kind"] = self.job_meta.get(jid, {}).get("kind", "")
            enriched["defsite"] = self.job_meta.get(jid, {}).get("defsite", "")
            label = _label(jid)
            if label in self.upstream_failed:
                enriched["upstream"] = self.upstream_failed[label]
                enriched["reason"] = translated_failed[label]
            self.failed_details[label] = enriched
        self.job_entries: Dict[str, Any] = {
            _label(j): v for j, v in dict(report.get("job_entries", {})).items()
        }
        self.generation = generation
        # P8.2: where the partial tree of finished outputs lives after a
        # failed run (None on success or when nothing finished).
        self.partial_dir = partial_dir
        self.raw = report

    def __repr__(self) -> str:
        return (
            f"RunResult(built={len(self.built)}, hits={len(self.hits)}, "
            f"failed={len(self.failed)}, generation={self.generation!r})"
        )

    # Label column width (widest label + colon = "Exception:"); every field
    # value starts one space past it, so labels and values line up.
    _LABEL_W = len("Exception:")
    _WRAP = 80

    def format_failures(self) -> str:
        """Human-readable, per-job failure report (PRINCIPLES.md P9: every
        failure names its artifacts). One block per *actually failed* job:
        ``Job`` / ``Defined`` / ``Runtime`` / ``Exit`` (only for
        CommandJobs) / ``Exception`` (wrapped) / ``Missing`` / ``Log`` /
        ``Outputs`` / ``Kept``. Jobs that merely never ran because an
        upstream failed are aggregated into one compact "did not run"
        section, grouped by root cause — they are casualties, not stories
        of their own. Plain text, no third-party deps — what
        :class:`PPGRunError` renders."""
        names = sorted(n for n in self.failed if n not in self.upstream_failed)
        if not names and not self.upstream_failed:
            return "no failed jobs"
        # When exactly one job actually failed, the log *is* the story:
        # inline its full consolidated log so the user reads the whole
        # traceback right here instead of opening the file the `Log:` field
        # points at. In that case the per-job block drops its truncated
        # stderr tail, since the full log (which includes stderr) follows
        # in its entirety.
        full = self._read_failure_log(names[0]) if len(names) == 1 else ""
        blocks = [self._format_one_failure(name, inline_tail=not full) for name in names]
        text = f"{len(names)} job(s) failed:\n\n" + "\n\n".join(blocks)
        if full:
            text += "\n\n--- full log ---\n" + full
        text += self._format_upstream_casualties()
        if self.partial_dir:
            text += (
                f"\n\nPartial results: {self.partial_dir}\n"
                "(every job that finished before the failure, browsable; "
                "the current generation is untouched)"
            )
        return text

    def _format_upstream_casualties(self) -> str:
        """The aggregated "did not run" section: cascaded jobs grouped by
        the root failure that took them down. ``""`` when there are none."""
        if not self.upstream_failed:
            return ""
        by_root: Dict[str, List[str]] = {}
        for label, root in self.upstream_failed.items():
            by_root.setdefault(root, []).append(label)
        lines = [
            "",
            "",
            f"{len(self.upstream_failed)} more job(s) did not run because an "
            "upstream failed:",
        ]
        for root in sorted(by_root):
            victims = sorted(by_root[root])
            shown = ", ".join(victims[:10])
            if len(victims) > 10:
                shown += f" (+{len(victims) - 10} more)"
            lines.append(f"  because {root} failed: {shown}")
        return "\n".join(lines)

    def _format_one_failure(self, name: str, inline_tail: bool = True) -> str:
        detail = self.failed_details.get(name, {})
        lines: List[str] = []

        def field(label: str, value: str) -> None:
            lines.append(f"{(label + ':').ljust(self._LABEL_W)} {value}")

        field("Job", name)

        # P9.1/P1.5: where the failing job was defined — the line the user
        # opens first.
        defsite = detail.get("defsite")
        if defsite:
            field("Defined", defsite)

        runtime_ms = detail.get("runtime_ms")
        if runtime_ms is not None:
            field("Runtime", _fmt_duration(runtime_ms))

        # Exit code only for CommandJobs — a FileJob's is always 1 and its
        # traceback (Exception, below) is the real story.
        exit_code = detail.get("exit_code")
        if exit_code is not None and detail.get("kind") == "command":
            field("Exit", str(exit_code))

        reason = detail.get("reason") or self.failed.get(name, "")
        exc = _last_meaningful_line(reason)
        if exc:
            # The scheduler's reason opens with the internal job handle
            # (`job "j…" exited …`); the block header already names the job
            # in the user's terms (P1.5) — strip the handle, and drop the
            # line entirely when it would only repeat the Exit field.
            m = re.match(r'^job "[^"]+" (.+)$', exc, re.DOTALL)
            if m:
                exc = m.group(1)
            duplicates_exit = (
                exit_code is not None
                and detail.get("kind") == "command"
                and exc == f"exited with code {exit_code}"
            )
            if not duplicates_exit:
                self._wrapped_field(lines, "Exception", exc)

        # Output-contract failures: name the declared outputs the job never
        # wrote (the entry path with what it *did* write follows as
        # Outputs/Kept below).
        missing = detail.get("missing_outputs") or []
        if missing:
            self._wrapped_field(lines, "Missing", ", ".join(missing))

        # P9.2/P9.3: the job's own words are the evidence — inline the
        # stderr tail (which for e.g. a fetch mismatch carries the url,
        # both hashes, and the retained-download path) instead of reducing
        # it to one line.
        if inline_tail and "--- stderr tail ---" in reason:
            tail = reason.split("--- stderr tail ---", 1)[1].strip().splitlines()
            if len(tail) > 1:  # a single line is already the Exception above
                lines.append("Stderr:")
                for tl in tail[:15]:
                    lines.append(" " * (self._LABEL_W + 1) + tl.rstrip())

        log = detail.get("failure_log") or detail.get("log_dir")
        if log:
            field("Log", log)

        out_dir = detail.get("out_dir")
        if out_dir:
            field("Outputs", out_dir)
            # P9.3/P7.4: whatever the failed job left behind (e.g. a
            # rejected download kept for inspection) — list it, don't make
            # the user go digging.
            for kept in self._kept_files(out_dir):
                field("Kept", kept)

        return "\n".join(lines)

    def _read_failure_log(self, name: str) -> str:
        """Full text of a failed job's consolidated log, or ``""`` if there
        is no readable log. Best-effort: a missing/unreadable log must never
        turn a job failure into a *reporting* failure."""
        import os

        detail = self.failed_details.get(name, {})
        log = detail.get("failure_log")
        if not log or not os.path.isfile(log):
            return ""
        try:
            with open(log, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read().rstrip("\n")
        except OSError:
            return ""

    @staticmethod
    def _kept_files(out_dir: str, limit: int = 5) -> List[str]:
        import os

        found: List[str] = []
        for root, _dirs, files in os.walk(out_dir):
            for fname in sorted(files):
                found.append(os.path.join(root, fname))
                if len(found) >= limit:
                    return found
        return found

    def _wrapped_field(self, lines: List[str], label: str, text: str) -> None:
        """Append a label/value field whose value is word-wrapped to
        ``_WRAP`` columns, with continuation lines indented to line up under
        the value (never breaking long words like paths/identifiers)."""
        import textwrap

        value_col = self._LABEL_W + 1
        avail = max(20, self._WRAP - value_col)
        pieces = textwrap.wrap(
            text, width=avail, break_long_words=False, break_on_hyphens=False
        ) or [""]
        lines.append(f"{(label + ':').ljust(self._LABEL_W)} {pieces[0]}")
        for cont in pieces[1:]:
            lines.append(" " * value_col + cont)


class PPGRunError(RuntimeError):
    """Raised by :func:`run` when ``report.failed`` is non-empty. The view
    is deliberately *not* updated on partial failure (§11: a generation is a
    consistent, all-or-nothing snapshot) — ``result.generation`` is
    ``None``; ``result.failed``/``result.raw`` carry the full report so
    callers can inspect what broke."""

    def __init__(self, result: RunResult):
        super().__init__("ppg3 run failed:\n" + result.format_failures())
        self.result = result


class RunCallbacks:
    """The two coarse Rust->Python callbacks of CONTRACT.md's PyO3 boundary
    (§8.1 rule 1): ``expand_graph_job`` / ``run_in_process``. Passed as the
    ``py_callbacks`` object to ``ppg3._core.run``.
    """

    def __init__(self, graph: Graph):
        self.graph = graph
        # Memoizes `run_in_process` by (job_id, ik) within this run — a
        # `HostCallbacks::run_in_process` call carries the same key document
        # a store-backed job would hash; since InProcess jobs never consult
        # the StoreSet themselves (core/src/scheduler.rs's `dispatch_job`
        # only does `derive_key` for them, no `storeset.lookup`), any
        # caching is this callback's own responsibility. Mostly relevant if
        # a `GraphJob` expansion could cause the same `UnsandboxedJob` id to
        # be dispatched more than once in pathological graphs; cheap safety
        # net either way.
        self._inprocess_memo: Dict[str, None] = {}

    def expand_graph_job(self, job_id: str) -> str:
        """Run a ``GraphJob``'s callback in-process; it may call job
        constructors, which register into this same graph. Returns the
        JobDef JSON list of newly-added jobs."""
        job = self.graph.jobs.get(job_id)
        if job is None or not isinstance(job, GraphJob):
            raise RuntimeError(f"expand_graph_job: no GraphJob with id {job_id!r}")
        before = set(self.graph.jobs.keys())
        job.fn()
        new_ids = set(self.graph.jobs.keys()) - before
        new_defs = [self.graph.jobs[i].job_def(self.graph) for i in sorted(new_ids)]
        return json.dumps(new_defs)

    def run_in_process(self, job_id: str, key_doc_json: str) -> None:
        """Run an ``UnsandboxedJob``'s callback in-process (loader layer,
        §6.3 tier 3).

        Known limitation (see STATUS.md "run_in_process real-path gap"):
        ``HostCallbacks::run_in_process(job_id, key_doc)`` only carries the
        *declared* input hashes (the key document), never resolved real
        filesystem paths — that mapping only exists inside the Rust
        scheduler's dispatch-time state (`CompletedInfo`), which is not
        exposed across the PyO3 boundary (`ExecTemplate::InProcess` jobs get
        no `PreparedJob`/mounts at all — see `dispatch_job` in
        scheduler.rs). This implementation therefore only supports
        ``UnsandboxedJob``s whose declared inputs are `File`/`Params` (leaf)
        refs: a `File` input's real on-disk path is already known host-side
        (it was never mounted for *any* job kind, sandboxed or not — see
        `resolve_placeholder`'s `Leaf` case in scheduler.rs erroring on
        `{in:NAME}`), and a `Params` input's value is already known from
        `job.inputs` itself. A `Job`/`JobSubset`-typed input raises
        ``NotImplementedError`` naming the gap.
        """
        job = self.graph.jobs.get(job_id)
        if job is None or not isinstance(job, UnsandboxedJob):
            raise RuntimeError(f"run_in_process: no UnsandboxedJob with id {job_id!r}")
        if job_id in self._inprocess_memo:
            return
        json.loads(key_doc_json)  # validate shape
        # §6.7 session-lifetime memo, keyed by ik: an InProcess job whose
        # key document is unchanged since an earlier run in this session
        # does not run again. Cleared by session_stop().
        core = get_core()
        ik = core.input_key(core.canonicalize(key_doc_json).encode("utf-8"))
        if ik in _loader_memo:
            self._inprocess_memo[job_id] = None
            return

        inputs: Dict[str, str] = {}
        params: Dict[str, Any] = {}
        for name, value in job.inputs.items():
            if isinstance(value, File):
                inputs[name] = value.path
            elif isinstance(value, Params):
                params[name] = value.canonical()
            else:
                raise NotImplementedError(
                    f"UnsandboxedJob {job_id!r}: run_in_process cannot resolve a real "
                    f"path for {type(value).__name__}-typed input {name!r} (only "
                    "File/Params leaf inputs are supported in this v1 — see "
                    "STATUS.md 'run_in_process real-path gap')."
                )

        job_io = JobIO(inputs=inputs, outputs={}, tools={}, log_dir="", params=params)
        job.run(job_io)
        self._inprocess_memo[job_id] = None
        _loader_memo[ik] = None


def _write_partial_tree(graph, report, core, handle, partial_dir: str) -> bool:
    """PRINCIPLES.md P8.2: after a failed run, link every *finished* job's
    published outputs into ``<project_dir>/partial/`` so a user two days
    into a multi-day run can inspect results without store archaeology. The
    tree is rebuilt from scratch on every failed run and removed on the
    next success; `current` (P8.3) is never touched. Returns whether any
    output was linked."""
    import shutil

    shutil.rmtree(partial_dir, ignore_errors=True)
    failed = set(report.get("failed", {}))
    entries = report.get("job_entries", {})
    made = False
    for jid, job in graph.jobs.items():
        if jid in failed or not getattr(job, "publish", None):
            continue
        entry = entries.get(jid)
        if entry is None:
            continue
        ik, _oh = entry
        looked = core.lookup(handle, ik)
        if looked is None:
            continue
        info = json.loads(looked)
        store = graph.stores[info["store_index"]]
        data_dir = os.path.join(
            os.path.abspath(store.path), "v1", "entries", info["output_hash"], "data"
        )
        for output_name, dest in job.publish.items():
            src = os.path.join(data_dir, output_name)
            if not os.path.exists(src):
                continue
            target = os.path.join(partial_dir, dest)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if os.path.lexists(target):
                os.remove(target)
            os.symlink(src, target)
            made = True
    if not made:
        shutil.rmtree(partial_dir, ignore_errors=True)
    return made


def _entry_data_dirs(graph, report, core, handle) -> Dict[str, str]:
    """id -> absolute ``entries/<oh>/data`` path, for every job the run
    report recorded a published entry for (hit or built — and failed jobs
    whose entry was still published, P8)."""
    dirs: Dict[str, str] = {}
    for jid, entry in dict(report.get("job_entries", {})).items():
        ik, _oh = entry
        looked = core.lookup(handle, ik)
        if looked is None:
            continue
        info = json.loads(looked)
        store = graph.stores[info["store_index"]]
        dirs[jid] = os.path.join(
            os.path.abspath(store.path), "v1", "entries", info["output_hash"], "data"
        )
    return dirs


def _write_last_run(
    graph, result: "RunResult", report: Dict[str, Any], core, handle, project_id: str
) -> None:
    """``<project_dir>/last_run.json`` — the per-job story of the most
    recent run, in user terms (labels, definition sites, publish paths;
    P1.5). This is what lets `ppg3 why <path>` answer "why did this job
    not run?" / "why is this file missing from the output tree?" after the
    Python process is gone. Pointer/cache state per P3 (derived, freely
    regenerated by the next run); best-effort — a reporting hiccup must
    never fail the run itself."""
    import time

    built_ids = set(report.get("built", []))
    hit_ids = set(report.get("hits", []))
    raw_failed = dict(report.get("failed", {}))
    entry_dirs = _entry_data_dirs(graph, report, core, handle)

    jobs: List[Dict[str, Any]] = []
    for jid, job in graph.jobs.items():
        meta = result.job_meta.get(jid, {})
        label = meta.get("label", jid)
        rec: Dict[str, Any] = {
            "label": label,
            "kind": meta.get("kind", ""),
            "defsite": meta.get("defsite", ""),
            "outputs": dict(getattr(job, "publish", {}) or {}),
        }
        if jid in built_ids:
            rec["status"] = "built"
        elif jid in hit_ids:
            rec["status"] = "hit"
        elif jid in raw_failed:
            detail = result.failed_details.get(label, {})
            upstream = detail.get("upstream")
            if upstream is not None:
                rec["status"] = "not_run"
                rec["upstream"] = upstream
            else:
                rec["status"] = "failed"
                for k in ("failure_log", "log_dir", "exit_code", "missing_outputs"):
                    if detail.get(k):
                        rec[k] = detail[k]
                if detail.get("out_dir"):
                    rec["out_dir"] = detail["out_dir"]
            rec["reason"] = result.failed.get(label, raw_failed[jid])
        else:
            # Never dispatched (abort mid-run) — absent from every list.
            rec["status"] = "not_reached"
        if jid in entry_dirs:
            rec["entry"] = entry_dirs[jid]
        jobs.append(rec)

    payload = {
        "schema": 1,
        "created_at_ms": int(time.time() * 1000),
        "project_id": project_id,
        "generation": result.generation,
        "partial_dir": result.partial_dir,
        "jobs": jobs,
    }
    try:
        os.makedirs(graph.project_dir, exist_ok=True)
        path = os.path.join(graph.project_dir, "last_run.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        pass


def _write_project_config(graph: Graph) -> None:
    """``<project_dir>/config.json`` (CONTRACT.md "Additive clarification:
    `--keep-generations` and `.ppg3/config.json`"): the standalone CLI has
    no ``ppg3.new(stores=...)`` call to hand it a ready ``StoreSet``, so
    this file — written here, the one place a project's store list is
    known on the Python side — lets it reconstruct one from disk."""
    os.makedirs(graph.project_dir, exist_ok=True)
    config_path = os.path.join(graph.project_dir, "config.json")
    payload = {"stores": [s.to_json() for s in graph.stores]}
    with open(config_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")


# --------------------------------------------------------------------------
# §6.7 watch mode support
# --------------------------------------------------------------------------

# Module-level flag consulted by `run()` to decide whether the generation it
# writes is ephemeral, *in addition to* an explicit `ephemeral=True` kwarg
# (CONTRACT.md/PPG3_DESIGN.md §6.7 "watch-mode generations are flagged
# ephemeral"). A plain bool (not a contextvar) is enough: `python -m ppg3
# watch`'s loop (python/ppg3/watch.py) drives `runpy.run_path()` on a single
# thread, synchronously, so there is never a concurrent non-watch `run()`
# call while this is set.
_watch_active = False


@contextlib.contextmanager
def watch_mode():
    """Mark every ``run()`` call made while the pipeline script's definition
    pass executes underneath this context as producing an ephemeral view
    generation (§6.7), regardless of the ``ephemeral=`` argument the script
    itself passes (or omits) — the script is written for normal,
    non-watch use and should not need to know it is being watched. Used by
    ``python -m ppg3 watch`` (``python/ppg3/watch.py``); not part of the
    normal user-facing API."""
    global _watch_active
    previous = _watch_active
    _watch_active = True
    try:
        yield
    finally:
        _watch_active = previous


# The "last run info" slot (CONTRACT.md "Python package" watch addendum):
# `run()` is called from *inside* the pipeline script executed by
# `runpy.run_path()`, so once that call returns the script's own local
# `graph` variable is gone along with the temporary module namespace
# `runpy` built it in. `run()` stashes what the watcher needs here on every
# call (even one that ends up raising `PPGRunError` — see below), so
# `python -m ppg3 watch` can read it back via `get_last_run_info()`
# immediately after `runpy.run_path()` returns (or raises). ``report`` is
# the raw decoded RunReport dict (``built``/``hits``/``failed``/
# ``job_entries``), stashed unconditionally (success or failure) so the
# watcher can print counts either way.
# ---------------------------------------------------------- session (§6.7)
#
# One module-level coordinator session per process: warm forkserver
# templates (and the loader-layer memos below) survive across ppg3.run()
# calls — watch-mode iterations and repl use get this for free, since both
# just call run() again in the same process. The session's TemplateManager
# lives on the Rust side (see py/src/lib.rs `Session`); templates are keyed
# by (interpreter, preload, python_env hash), so a changed PyEnv resolution
# simply spawns a new template and the old one idles until session end.
_session: Optional[Any] = None
_session_work_dir: Optional[str] = None

# §6.7: "loader-layer memos persist across runs in the session, keyed by
# ik". Consulted by RunCallbacks.run_in_process; cleared by session_stop().
_loader_memo: Dict[str, None] = {}


def _get_session(core) -> Any:
    global _session, _session_work_dir
    if _session is None:
        import tempfile

        _session_work_dir = tempfile.mkdtemp(prefix="ppg3-session-")
        _session = core.open_session(
            _session_work_dir, [sys.executable, "-I", "-m", "ppg3._template"]
        )
    return _session


def session_stop() -> None:
    """End the coordinator session (§6.7 `ppg3 session stop`): kill all warm
    forkserver templates and clear the loader-layer memos. Safe to call any
    number of times; the next :func:`run` lazily starts a fresh session."""
    global _session
    if _session is not None:
        core = get_core()
        core.session_shutdown(_session)
        _session = None
    _loader_memo.clear()


_last_run_info: Dict[str, Any] = {
    "graph": None,
    "watched_paths": [],
    "generation": None,
    "report": None,
}


def get_last_run_info() -> Dict[str, Any]:
    """Snapshot of the most recent :func:`run` call's graph / watched-path
    set / generation number. Never cleared: a pipeline pass whose
    definition raises *before* ever calling :func:`run` (or a script that
    never calls it at all) leaves this holding whatever the previous
    successful call recorded, which is exactly what watch mode's "keep
    watching the last-known watch set" failure policy (§6.7) wants."""
    return dict(_last_run_info)


def run(
    graph: Optional[Graph] = None,
    project_id: str = "default",
    ephemeral: bool = False,
) -> RunResult:
    """Run `graph` (or the current graph) to completion and update the view.

    Calls, in order: writes ``.ppg3/config.json``, ``_core.open_stores``
    (from ``graph.stores``), ``_core.run`` (the lowered ``JobDef`` list +
    parallelism config + :class:`RunCallbacks`). On success (no failed
    jobs), assembles a ``ViewSpec`` from the run report's ``job_entries``
    (``id -> (ik, oh)``, cross-referenced against ``_core.lookup`` for the
    *store index* each job's output actually landed in — the report itself
    doesn't carry that, see ``py/src/lib.rs``'s ``lookup`` doc comment) and
    every job's ``view`` map, then calls ``_core.write_generation``.

    On any job failure, the view is left untouched (§11: a generation must
    be a consistent, all-or-nothing snapshot) and :class:`PPGRunError` is
    raised with the full report attached.

    Raises :class:`ppg3._bridge.CoreNotAvailable` if the extension isn't
    built — every other part of this package works without it.
    """
    from .jobs import _current_graph as default_graph

    graph = graph or default_graph
    if graph is None:
        raise RuntimeError("ppg3.run(): no graph — call ppg3.new(...) first")

    # Single-script ownership of the project dir (see runscript.py): refuse
    # to run when a *different* script last used this project_dir — before
    # any other side effect, so a refused run leaves no trace.
    from .runscript import check_and_record

    check_and_record(graph.project_dir)

    core = get_core()

    # jj support (ppg3.new(jj=True), see jj.py): enforce that every
    # job-source file known so far is tracked (hard error, before any job
    # is dispatched) and snapshot the jj state — the snapshot happens
    # *before* the run so the recorded commit/op ids match the sources the
    # jobs were actually lowered from.
    vcs_json: Optional[str] = None
    jj_root: Optional[str] = None
    if graph.jj:
        from . import jj as _jj

        jj_root = _jj.find_workspace_root(os.getcwd())
        if jj_root is None:
            raise _jj.JJError(
                "ppg3.new(jj=True): no jj workspace found from "
                f"{os.getcwd()!r} (create one with `jj git init --colocate`, "
                "or drop jj=True)"
            )
        _jj.assert_sources_tracked(jj_root, graph.source_paths())
        vcs_json = json.dumps(_jj.capture_state(jj_root))

    # PRINCIPLES.md P3.1: a missing writable store is a *configuration*
    # error — one message, before any job is dispatched, never the same
    # failure repeated per job.
    if not any(not s.readonly for s in graph.stores):
        raise RuntimeError(
            "no writable store configured — ppg3.new(stores=[...]) needs at "
            "least one writable ppg3.Store(...); every job's outputs are "
            "content in a store (PRINCIPLES.md P3)"
        )

    _write_project_config(graph)
    work_dir = os.path.join(graph.project_dir, "work")
    os.makedirs(work_dir, exist_ok=True)

    jobs_json = json.dumps(graph.job_defs())
    parallelism_json = json.dumps(graph.parallelism)
    # Bare-array shape per CONTRACT.md's PyO3 boundary paragraph
    # (`open_stores(config [{"name","path","readonly"}...])`) — distinct
    # from `.ppg3/config.json`'s `{"stores": [...]}` wrapper object above.
    stores_config_json = json.dumps([s.to_json() for s in graph.stores])

    handle = core.open_stores(stores_config_json)
    callbacks = RunCallbacks(graph)
    # §6.4 / CONTRACT.md "PyO3 boundary" addendum: `template_argv` is the
    # template start command handed to `ForkserverExecutor::new` on the
    # Rust side. `run()`'s own interpreter (`sys.executable`) is used as
    # the *default* interpreter — `ForkserverExecutor` substitutes each
    # job's own resolved `PyEnv` interpreter (`job.argv[0]`) for this at
    # spawn time (see `core/src/forkserver.rs`'s `ForkserverExecutor::new`
    # doc comment), so this only matters as "some real interpreter" to
    # satisfy the constructor — it is not actually what ends up running a
    # template for a job under a *different* `PyEnv`.
    # `graph.forkserver=False` disables this entirely: an empty list here
    # makes the Rust side skip template dispatch unconditionally for every
    # job, falling back to the pre-forkserver behavior.
    # PRINCIPLES.md P6: when real enforcement is available and wanted,
    # disable the forkserver templates — template children run unenforced,
    # so every job must take the (sandboxed) fallback executor instead.
    use_forkserver = graph.forkserver
    if graph.sandbox != "off" and core.sandbox_available():
        use_forkserver = False
    template_argv = (
        [sys.executable, "-I", "-m", "ppg3._template"] if use_forkserver else []
    )
    # §6.7: with the forkserver on, dispatch through the module-level
    # session so templates stay warm across run() calls in this process
    # (watch iterations, repl). run(session=...) makes the Rust side use
    # the session's TemplateManager instead of a run-scoped one.
    session = _get_session(core) if use_forkserver else None
    report_json = core.run(
        handle,
        jobs_json,
        parallelism_json,
        callbacks,
        work_dir,
        template_argv,
        session,
        graph.sandbox,
    )
    report = json.loads(report_json)

    # §6.7 watch mode: snapshot graph + watched-path set *before* the
    # failure check below, so a failed run still updates the "last run
    # info" slot a watcher reads (watched_paths() reflects everything
    # recorded during job_defs()/GraphJob expansion up to this point, even
    # if the run itself then fails).
    _last_run_info["graph"] = graph
    _last_run_info["watched_paths"] = graph.watched_paths()
    _last_run_info["report"] = report

    job_meta = _job_meta_from_graph(graph)
    partial_dir = os.path.join(graph.project_dir, "partial")

    if report.get("failed"):
        # P8.2: finished work is never withheld — link every completed
        # job's outputs into a browsable partial tree (marked by its name
        # and location; never `current`, see P8.3) and say where it is.
        made = _write_partial_tree(graph, report, core, handle, partial_dir)
        result = RunResult(
            report,
            generation=None,
            job_meta=job_meta,
            partial_dir=partial_dir if made else None,
        )
        _write_last_run(graph, result, report, core, handle, project_id)
        raise PPGRunError(result)
    # A stale partial tree from an earlier failed run would misrepresent
    # this (successful) state — drop it.
    import shutil as _shutil

    _shutil.rmtree(partial_dir, ignore_errors=True)

    # §7.6 TOFU: after a successful run, patch (or table-print) the real
    # hash for every FetchJob defined with blake3=None. Purely a
    # post-run/reporting side effect — the run itself is already done and
    # its store entry already published; patching only changes what the
    # *next* definition pass reads (see tofu.py's module docstring). Import
    # kept local to avoid `tofu` (which imports `libcst` lazily anyway)
    # being on the hot import path for every `import ppg3`.
    from . import tofu

    tofu.run_tofu_pass(graph, report, core, handle)

    job_entries = report.get("job_entries", {})
    view_entries = []
    for job_id, job in graph.jobs.items():
        if not job.publish:
            continue
        entry = job_entries.get(job_id)
        if entry is None:
            continue
        ik, oh = entry
        looked = core.lookup(handle, ik)
        if looked is None:
            raise RuntimeError(
                f"internal error: job {job.label!r} reported input key {ik!r} "
                "in its run report but a post-run lookup() found no manifest "
                "for it"
            )
        info = json.loads(looked)
        store_index = info["store_index"]
        for output_name, view_path in job.publish.items():
            # Entry layout is keyed by output *name* (PRINCIPLES.md P1.4 —
            # the job wrote `/ppg/out/<output_name>`); the publish path is
            # pure pointer state, resolved right here and nowhere deeper.
            view_entries.append(
                {
                    "view_rel_path": view_path,
                    "oh": oh,
                    "path_within_entry": output_name,
                    "store_index": store_index,
                }
            )
    view_spec_json = json.dumps({"entries": view_entries})

    # jj support: a GraphJob expansion may have defined jobs (and thus new
    # job-source files: call sites, Source refs) mid-run that the pre-run
    # check could not have seen — re-check the final source set before the
    # generation is written, so an untracked dynamically-introduced source
    # still hard-errors rather than silently producing a generation whose
    # recorded jj state doesn't cover its own sources.
    if graph.jj and jj_root is not None:
        from . import jj as _jj

        _jj.assert_sources_tracked(jj_root, graph.source_paths())

    # §6.7: an explicit `ephemeral=True` kwarg *or* an active watch-mode
    # context both mark the generation ephemeral; either alone is enough.
    effective_ephemeral = ephemeral or _watch_active
    generation = core.write_generation(
        handle,
        graph.project_dir,
        project_id,
        view_spec_json,
        effective_ephemeral,
        vcs_json,
    )
    _last_run_info["generation"] = generation
    result = RunResult(report, generation=generation, job_meta=job_meta)
    _write_last_run(graph, result, report, core, handle, project_id)
    return result
