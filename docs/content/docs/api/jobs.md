---
title: "Job types"
weight: 23
---

# Job types

Six constructors define work. All of them register with the current graph
(so `ppg3.new()` must have been called first), and none of them takes a
name — a job's identity *is* its definition (recipe, inputs, outputs,
environment), and two identical definitions merge into one job.

| Constructor | Runs | Typical use |
| --- | --- | --- |
| `CommandJob` | an argv in a sandbox | shelling out to tools |
| `FileJob` | a Python callback in a subprocess | most Python work |
| `DataJob` | a Python callback; return value is pickled | passing values, not files |
| `FetchJob` | a pinned download | bringing external data in |
| `GraphJob` | a callback that *defines more jobs* | data-dependent graphs |
| `UnsandboxedJob` | a callback forked in-process | the escape hatch |

## `CommandJob` — run a command line

`argv` is the command as a list. Paths are never hard-coded: inputs,
outputs, and tools appear as **placeholders** that the scheduler resolves
at run time — `{in:NAME}`, `{out:NAME}` (or `{out}` for a single-output
job), `{tool:NAME}` inside strings, or the equivalent typed items
`ppg3.In("name")`, `ppg3.Out("name")`, `ppg3.Tool("name")` as list
elements. Anything else in `argv` must be a plain string, or the
constructor raises `DefinitionError`.

<!-- ppg3-example: jobs-commandjob requires=core -->
```python
import ppg3
from ppg3.tools import PyEnv

ppg3.new(stores=[ppg3.Store("main", "store")], default_python=PyEnv.current())

with open("names.txt", "w") as fh:
    fh.write("ada\ngrace\n")

# String placeholders, embedded in a shell command:
ppg3.CommandJob(
    outputs={"count": "count.txt"},
    argv=["/bin/sh", "-c", "wc -l < {in:names} > {out:count}"],
    inputs={"names": ppg3.File("names.txt")},
    env={"PATH": "/usr/bin:/bin"},   # `wc` must be findable: declare PATH
)

# The same wiring with typed placeholders as argv items:
ppg3.CommandJob(
    outputs={"copy": "names-copy.txt"},
    argv=["/bin/cp", ppg3.In("names"), ppg3.Out("copy")],
    inputs={"names": ppg3.File("names.txt")},
)

result = ppg3.run()
assert sorted(result.built) == ["count.txt", "names-copy.txt"]
assert open("outputs/count.txt").read().strip() == "2"
assert open("outputs/names-copy.txt").read() == "ada\ngrace\n"
```

Jobs run with a **minimal environment**: only what `env=` declares exists,
and only declared inputs are meant to be visible. That is why the first
job declares `PATH` (for `wc`) and why these examples call `/bin/sh` and
`/bin/cp` by absolute path.

## `FileJob` — run a Python callback

`run=` is a function of one argument, conventionally `io`, which is the
callback's only channel to the world: `io.input(name)` resolves declared
inputs, `io.path(name)` gives the path each declared output must be
written to, `io.params[name]` carries `ppg3.Params` values. The callback
must be **self-contained** — arguments, locals, and body-level imports
only (see [`paranoid=`]({{< relref "new#paranoid" >}})).

<!-- ppg3-example: jobs-filejob requires=core -->
```python
import ppg3
from ppg3.tools import PyEnv

ppg3.new(stores=[ppg3.Store("main", "store")], default_python=PyEnv.current())

with open("numbers.txt", "w") as fh:
    fh.write("3 1 4 1 5 9\n")

def summarize(io):
    with open(io.input("numbers")) as fh:
        values = [int(x) for x in fh.read().split()]
    scaled = [v * io.params["factor"] for v in values]
    with open(io.path("summary"), "w") as fh:
        fh.write(f"n={len(scaled)} max={max(scaled)}\n")

ppg3.FileJob(
    outputs={"summary": "summary.txt"},
    run=summarize,
    inputs={
        "numbers": ppg3.File("numbers.txt"),
        "factor": ppg3.Params(10),
    },
)
ppg3.run()
assert open("outputs/summary.txt").read() == "n=6 max=90\n"
```

Instead of a live function, `run=` also accepts an *opaque file reference*
`ppg3.Source("path/to/file.py::function_name")` — the coordinator then
never imports the file; the job's identity is the file's content. Give it
an **absolute path**: the file is read again inside the job process, which
runs in its own working directory:

<!-- ppg3-example: jobs-filejob-source requires=core -->
```python
import os
import ppg3
from ppg3.tools import PyEnv

ppg3.new(stores=[ppg3.Store("main", "store")], default_python=PyEnv.current())

with open("steps.py", "w") as fh:
    fh.write(
        "def emit(io):\n"
        "    with open(io.path('out'), 'w') as fh:\n"
        "        fh.write('from a Source file')\n"
    )

ppg3.FileJob(
    outputs={"out": "from-source.txt"},
    run=ppg3.Source(os.path.abspath("steps.py") + "::emit"),
)
ppg3.run()
assert open("outputs/from-source.txt").read() == "from a Source file"
```

## `DataJob` — produce a Python value

A `DataJob` is a `FileJob` whose callback **returns a value**; ppg3 pickles
it, and downstream jobs read it back with `io.load(name)`. Because the
artifact is a single pickle, `outputs=` is just one destination path — or
omitted entirely for the common case of an intermediate value other jobs
consume but nobody publishes:

<!-- ppg3-example: jobs-datajob requires=core -->
```python
import ppg3
from ppg3.tools import PyEnv

ppg3.new(stores=[ppg3.Store("main", "store")], default_python=PyEnv.current())

def compute_stats(io):
    values = [3, 1, 4, 1, 5]
    return {"n": len(values), "mean": sum(values) / len(values)}

stats = ppg3.DataJob(run=compute_stats)   # internal: not published

def render_report(io):
    s = io.load("stats")                  # unpickled dict
    with open(io.path("report"), "w") as fh:
        fh.write(f"{s['n']} values, mean {s['mean']:.1f}\n")

ppg3.FileJob(
    outputs={"report": "report.txt"},
    run=render_report,
    inputs={"stats": stats},
)
ppg3.run()
assert open("outputs/report.txt").read() == "5 values, mean 2.8\n"
```

## `FetchJob` — pinned downloads

A fetch's identity is `(url, blake3 pin)` — **the pin is the identity**:
change the pin and it refetches; change the URL and it refetches (a moved
file must still prove it has the pinned content); leave both and the fetch
is a permanent cache hit. On a hash mismatch the job fails and keeps the
rejected download next to its intended path for inspection.

This example fetches over `file://` so it runs hermetically; in real
pipelines the URL is `https://…`:

<!-- ppg3-example: jobs-fetchjob requires=core -->
```python
import os
import ppg3
from ppg3.tools import PyEnv

ppg3.new(stores=[ppg3.Store("main", "store")], default_python=PyEnv.current())

# Stand-in for some external server:
with open("upstream.txt", "w") as fh:
    fh.write("hello ppg3\n")

ppg3.FetchJob(
    outputs="data/fetched.txt",
    url="file://" + os.path.abspath("upstream.txt"),
    # blake3 of the expected content — the fetch fails on anything else:
    blake3="f8055490a762f562c8fb8e60fbd311505f9532cc1f8480483d1f85f4d34cf3ab",
)
result = ppg3.run()
assert result.built == ["data/fetched.txt"]
assert open("outputs/data/fetched.txt").read() == "hello ppg3\n"
```

Where does the pin come from the first time? **Trust-on-first-use**: in an
interactive session (see [`frozen=`]({{< relref "new#frozen" >}})),
you may write `blake3=None`; after a successful run ppg3 prints the real
hash and patches it into your source file, so the *next* run is pinned —
and is a cache hit, not a refetch. Frozen mode (CI) rejects unpinned
fetches at definition time.

## `GraphJob` — jobs that define jobs

When the job list itself depends on data — one job per discovered sample,
say — wrap the discovery in a `GraphJob`. Its callback runs during
`ppg3.run()`, in-process, and any jobs it constructs join the same run:

<!-- ppg3-example: jobs-graphjob requires=core -->
```python
import ppg3
from ppg3.tools import PyEnv

ppg3.new(stores=[ppg3.Store("main", "store")], default_python=PyEnv.current())

for sample in ("liver", "kidney"):
    with open(f"{sample}.sample", "w") as fh:
        fh.write(f"{sample} data\n")

def one_job_per_sample():
    import glob
    for path in sorted(glob.glob("*.sample")):
        name = path.removesuffix(".sample")
        ppg3.CommandJob(
            outputs={"o": f"processed/{name}.txt"},
            argv=[
                "/bin/sh", "-c",
                'read line < {in:src}; echo "processed: $line" > {out:o}',
            ],
            inputs={"src": ppg3.File(path)},
        )

ppg3.GraphJob(one_job_per_sample)
result = ppg3.run()
assert "processed/liver.txt" in result.built
assert "processed/kidney.txt" in result.built
assert open("outputs/processed/liver.txt").read() == "processed: liver data\n"
```

## `UnsandboxedJob` — the explicit escape hatch

An `UnsandboxedJob` callback is forked from the coordinator process itself:
it sees your script's in-memory state (copy-on-write) and the real
filesystem, and its outputs are *not* store-managed. That trades away
everything the sandbox buys, so defining one emits a `UserWarning`, and its
declared inputs are limited to `ppg3.File`/`ppg3.Params` leaves. Prefer
`DataJob` unless you genuinely need shared in-process state:

<!-- ppg3-example: jobs-unsandboxedjob requires=core -->
```python
import ppg3
from ppg3.tools import PyEnv

ppg3.new(stores=[ppg3.Store("main", "store")], default_python=PyEnv.current())

with open("words.txt", "w") as fh:
    fh.write("one two three")

def count_words(io):
    with open(io.input("words")) as fh:
        n = len(fh.read().split()) * io.params["factor"]
    with open("word-count.txt", "w") as fh:   # a real, un-managed path
        fh.write(str(n))

ppg3.UnsandboxedJob(          # emits UserWarning: runs unsandboxed
    run=count_words,
    inputs={"words": ppg3.File("words.txt"), "factor": ppg3.Params(2)},
)
ppg3.run()
assert open("word-count.txt").read() == "6"
```

Next: [inputs, outputs, and the `io` object]({{< relref "inputs-and-outputs" >}}).
