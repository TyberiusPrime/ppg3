---
title: "ppg3.run()"
weight: 22
---

# `ppg3.run()` — every parameter

<!-- ppg3-example: fragment -->
```python
ppg3.run(
    graph=None,             # the Graph to run; default: the current graph
    project_id="default",   # namespace for GC roots and run metadata
    ephemeral=False,        # mark the resulting generation as transient
) -> RunResult              # raises PPGRunError if any job failed
```

`ppg3.run()` lowers the graph to job definitions, hands them to the Rust
scheduler, and — on success — writes a new **generation**: an atomic,
numbered snapshot of every published output, with `outputs/` repointed to
it. On any failure the view is left untouched and `PPGRunError` is raised.

## `graph=` — which graph to run

By default `run()` runs the **current graph** — the one the last
`ppg3.new()` call created. You can also hold on to graph objects and run
one explicitly, which is what you want when a single process builds more
than one graph (tests, notebooks, orchestration scripts):

<!-- ppg3-example: run-graph requires=core -->
```python
import ppg3
from ppg3.tools import PyEnv

def make_graph(message):
    g = ppg3.new(
        stores=[ppg3.Store("main", "store")],
        default_python=PyEnv.current(),
    )
    ppg3.CommandJob(
        outputs={"m": f"{message}.txt"},
        argv=["/bin/sh", "-c", f"echo {message} > {{out:m}}"],
    )
    return g

first = make_graph("alpha")
second = make_graph("beta")     # `second` is now the current graph

# Run an explicit, non-current graph:
r1 = ppg3.run(first)
assert r1.built == ["alpha.txt"]

# A bare run() takes the current graph:
r2 = ppg3.run()
assert r2.built == ["beta.txt"]
```

Note both graphs share one store: `alpha.txt`'s entry is content-addressed,
so if a third graph defined the identical job it would be a cache hit.

## `project_id=` — namespacing shared state

Stores are designed to be shared — between projects, machines, and people.
`project_id` is how a run marks store entries as *in use by this project*:
every generation registers **GC roots** under
`v1/roots/<project_id>/<generation>/` in each writable store it links from,
and store garbage collection keeps everything reachable from any project's
roots. It is also recorded in each generation's `meta.json` and in
`<project_dir>/last_run.json`.

Two pipelines sharing a store should use two `project_id`s — then neither
project's GC housekeeping can pull entries out from under the other:

<!-- ppg3-example: run-project-id requires=core -->
```python
import json
import os
import ppg3
from ppg3.tools import PyEnv

ppg3.new(stores=[ppg3.Store("main", "store")], default_python=PyEnv.current())
ppg3.CommandJob(
    outputs={"o": "report.txt"},
    argv=["/bin/sh", "-c", "echo report > {out:o}"],
)
result = ppg3.run(project_id="quarterly-report")
assert result.generation == 1

# The store now holds a root for this project's generation 1:
assert os.path.isdir("store/v1/roots/quarterly-report/1")
# ...and the run metadata names the project:
last_run = json.load(open(".ppg3/last_run.json"))
assert last_run["project_id"] == "quarterly-report"
```

## `ephemeral=` — transient generations

Generations are kept until you drop them — except **ephemeral** ones,
which are marked as transient and cleaned up more aggressively by
generation housekeeping ("keep the last N ephemeral, keep all explicit
generations"). The marker is a `.ephemeral` file in the generation
directory. Watch mode (`python -m ppg3 watch`) marks its generations
ephemeral automatically; pass `ephemeral=True` yourself for any run whose
snapshot you don't need to keep, e.g. a quick parameter probe:

<!-- ppg3-example: run-ephemeral requires=core -->
```python
import os
import ppg3
from ppg3.tools import PyEnv

ppg3.new(stores=[ppg3.Store("main", "store")], default_python=PyEnv.current())
ppg3.CommandJob(
    outputs={"o": "probe.txt"},
    argv=["/bin/sh", "-c", "echo probe > {out:o}"],
)
result = ppg3.run(ephemeral=True)

assert result.generation == 1
assert os.path.isfile(".ppg3/views/1/.ephemeral")   # marked transient
# The view works exactly like a normal generation until it is collected:
assert open("outputs/probe.txt").read() == "probe\n"
```

## One project dir, one run script {#run-script}

A project dir has **one** generation sequence, one `views/current`, one
`outputs/` symlink. Two different scripts sharing it would take turns
replacing each other's `outputs/` — almost always an accident. `run()`
therefore records the running script's path in
`<project_dir>/run_script` (its own file, nothing else in it) on the
first run, and *refuses to run* when a different script shows up:

<!-- ppg3-example: run-script-guard requires=core -->
```python
import os
import subprocess
import sys

PIPELINE = """
import ppg3
from ppg3.tools import PyEnv
ppg3.new(stores=[ppg3.Store("main", "store")], default_python=PyEnv.current())
ppg3.CommandJob(outputs={"o": "%s"}, argv=["/bin/sh", "-c", "echo hi > {out:o}"])
ppg3.run()
"""

open("a.py", "w").write(PIPELINE % "a.txt")
open("b.py", "w").write(PIPELINE % "b.txt")

assert subprocess.run([sys.executable, "a.py"]).returncode == 0
assert open(".ppg3/run_script").read().strip() == os.path.realpath("a.py")

# A different script against the same project dir is refused, before any
# side effect — no generation is written, outputs/ still belongs to a.py:
proc = subprocess.run([sys.executable, "b.py"], capture_output=True, text=True)
assert proc.returncode != 0
assert "run_script" in proc.stderr
assert not os.path.exists("outputs/b.txt")

# The record is the whole decision: delete it to hand the dir over.
os.remove(".ppg3/run_script")
assert subprocess.run([sys.executable, "b.py"]).returncode == 0
assert open("outputs/b.txt").read() == "hi\n"
```

Renames are recognized, not punished: when the recorded script no longer
exists *and* no other `*.py` file next to the project dir looks like a
ppg3 run script, the record updates silently — you renamed your one
pipeline script, nothing is contested:

<!-- ppg3-example: run-script-rename requires=core -->
```python
import os
import subprocess
import sys

os.makedirs("proj")
open("proj/pipeline.py", "w").write("""
import ppg3
from ppg3.tools import PyEnv
ppg3.new(stores=[ppg3.Store("main", "store")], default_python=PyEnv.current())
ppg3.CommandJob(outputs={"o": "x.txt"}, argv=["/bin/sh", "-c", "echo x > {out:o}"])
ppg3.run()
""")
assert subprocess.run([sys.executable, "pipeline.py"], cwd="proj").returncode == 0

os.rename("proj/pipeline.py", "proj/renamed.py")
assert subprocess.run([sys.executable, "renamed.py"], cwd="proj").returncode == 0
record = open("proj/.ppg3/run_script").read().strip()
assert record == os.path.realpath("proj/renamed.py")
```

If both scripts are meant to exist, give each its own project dir
(`ppg3.new(project_dir=...)`) — separate generation sequences, separate
`outputs/`, separate retention. Interactive use (a REPL, a notebook,
`python -c`) has no script identity; the guard skips it and leaves any
record untouched.

## The return value: `RunResult`

`run()` returns a `RunResult`. Its collections are keyed by job **label** —
a job's published destinations (like `"summary.txt"`; a job publishing
several outputs appears once, its destinations joined with `+`), or
`<kind @ file:line>` for internal jobs — never by internal ids:

- `result.built` — labels (re)built this run,
- `result.hits` — labels served from a store,
- `result.failed` — `{label: reason}`, empty on a successful run,
- `result.generation` — the generation number written (`None` on failure),
- `result.raw` — the full untranslated run report, if you need it.

<!-- ppg3-example: run-result requires=core -->
```python
import ppg3
from ppg3.tools import PyEnv

def make_graph():
    g = ppg3.new(
        stores=[ppg3.Store("main", "store")],
        default_python=PyEnv.current(),
    )
    ppg3.CommandJob(
        outputs={"o": "greeting.txt"},
        argv=["/bin/sh", "-c", "echo hello > {out:o}"],
    )
    return g

make_graph()
first = ppg3.run()
assert first.built == ["greeting.txt"]
assert first.hits == []
assert first.failed == {}
assert first.generation == 1

# An identical second run does no work: all hits, a new generation.
make_graph()
second = ppg3.run()
assert second.built == []
assert second.hits == ["greeting.txt"]
assert second.generation == 2
```

## Failure: `PPGRunError`, partial results, and the untouched view

If any job fails, `run()` raises `PPGRunError`. Three guarantees hold:

1. **The view is untouched.** A generation is all-or-nothing; `outputs/`
   still points at the last good run, never at a half-updated tree.
2. **Finished work is not withheld.** Every job that *did* complete has its
   outputs linked into a browsable partial tree at
   `<project_dir>/partial/` (`result.partial_dir`; removed again on the
   next successful run).
3. **Every failure names its artifacts.** `result.failed_details[label]`
   carries the failure log path, exit code, runtime, and whatever the job
   left behind; `result.format_failures()` renders the human-readable
   report (it is also the exception's message).

Jobs that never ran because a parent failed are not failures of their own:
they appear in `result.failed` with a "did not run — upstream job … failed"
reason, and `result.upstream_failed` maps each one to the root cause.

<!-- ppg3-example: run-failure requires=core -->
```python
import ppg3
from ppg3.run import PPGRunError
from ppg3.tools import PyEnv

def make_graph(break_it):
    g = ppg3.new(
        stores=[ppg3.Store("main", "store")],
        default_python=PyEnv.current(),
    )
    good = ppg3.CommandJob(
        outputs={"o": "good.txt"},
        argv=["/bin/sh", "-c", "echo good > {out:o}"],
    )
    script = "exit 3" if break_it else "echo fine > {out:o}"
    flaky = ppg3.CommandJob(
        outputs={"o": "flaky.txt"},
        argv=["/bin/sh", "-c", script],
    )
    # Depends on flaky -> becomes an upstream casualty when flaky fails.
    ppg3.CommandJob(
        outputs={"o": "downstream.txt"},
        argv=["/bin/sh", "-c", "read _ < {in:f}; echo done > {out:o}"],
        inputs={"f": flaky},
    )
    return g

# A first, healthy run establishes generation 1.
make_graph(break_it=False)
ok = ppg3.run()
assert ok.generation == 1

# Now break the middle job.
make_graph(break_it=True)
try:
    ppg3.run()
except PPGRunError as e:
    result = e.result
    assert result.generation is None                      # no new generation
    assert "exited with code 3" in result.failed["flaky.txt"]
    assert result.upstream_failed == {"downstream.txt": "flaky.txt"}
    detail = result.failed_details["flaky.txt"]
    assert detail["exit_code"] == 3
    # Finished work is browsable in the partial tree...
    assert open(result.partial_dir + "/good.txt").read() == "good\n"
    # ...and the real view still shows the last good generation:
    assert open("outputs/flaky.txt").read() == "fine\n"
else:
    raise AssertionError("run with a failing job must raise PPGRunError")
```

## `session_stop()` — ending the warm session

Within one process, consecutive `run()` calls share a **session**: warm
forkserver templates and in-process memo state survive across runs (that is
what makes watch-mode iterations cheap). `ppg3.session_stop()` tears that
down — kills the templates, clears the memos; the next `run()` lazily
starts a fresh session. Call it if you need to reclaim the resources or
force templates to pick up an environment change mid-process. It is safe to
call any number of times:

<!-- ppg3-example: run-session-stop requires=core -->
```python
import ppg3
from ppg3.tools import PyEnv

ppg3.new(stores=[ppg3.Store("main", "store")], default_python=PyEnv.current())

def write(io):
    with open(io.path("out"), "w") as fh:
        fh.write("done")

ppg3.FileJob(outputs={"out": "out.txt"}, run=write)
result = ppg3.run()
assert result.built == ["out.txt"]

ppg3.session_stop()      # templates gone, memos cleared
ppg3.session_stop()      # idempotent
```

Next: [job types]({{< relref "jobs" >}}).
