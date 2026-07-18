---
title: "Shared job parameters"
weight: 25
---

# Shared job parameters

Parameters accepted across job constructors (`FileJob`, `CommandJob`,
`DataJob`; `FetchJob` and `UnsandboxedJob` take the subset that makes sense
for them). Each participates in the job's cache identity — changing any of
them re-runs the job.

## `env=` {#env}

Jobs run with a **minimal environment**: an environment variable exists
inside a job only if `env=` declares it. Declared variables are part of the
job's key (changing a value re-runs the job) — the flip side of ppg2-style
"whatever `os.environ` happened to hold" irreproducibility:

<!-- ppg3-example: jp-env requires=core -->
```python
import ppg3
from ppg3.tools import PyEnv

ppg3.new(stores=[ppg3.Store("main", "store")], default_python=PyEnv.current())

ppg3.CommandJob(
    outputs={"o": "greeting.txt"},
    argv=["/bin/sh", "-c", 'echo "$MODE and $UNDECLARED" > {out:o}'],
    env={"MODE": "production"},
)
ppg3.run()
# $MODE was declared and visible; $UNDECLARED simply doesn't exist:
assert open("outputs/greeting.txt").read() == "production and \n"
```

## `resources=` {#resources}

`ppg3.Resources(pool=amount, ...)` declares how many units of each
[configured pool]({{< relref "new#parallelism" >}})
a job holds while running — the concurrency throttle for RAM-hungry or
license-limited work. A job with no `resources=` holds nothing. Requesting
an unconfigured pool, or more than a pool's capacity, fails the run before
any job is dispatched:

<!-- ppg3-example: jp-resources requires=core -->
```python
import ppg3
from ppg3.tools import PyEnv

ppg3.new(
    stores=[ppg3.Store("main", "store")],
    default_python=PyEnv.current(),
    parallelism={"cores": 8, "ram_gb": 16},
)

# Two of these can hold 8 GB each concurrently; a third waits.
for i in range(3):
    ppg3.CommandJob(
        outputs={"o": f"chunk-{i}.txt"},
        argv=["/bin/sh", "-c", f"echo chunk {i} > {{out:o}}"],
        resources=ppg3.Resources(cores=2, ram_gb=8),
    )
result = ppg3.run()
assert len(result.built) == 3
```

## `retain=` {#retain}

How store garbage collection treats the job's entry:

- `ppg3.Retain.Default` — kept while any generation's roots reach it.
- `ppg3.Retain.Evict` — cheap to recompute; GC may drop it eagerly.
- `ppg3.Retain.Pin("label")` — never collected, under a human-readable
  label, e.g. a release artifact.

<!-- ppg3-example: jp-retain requires=core -->
```python
import ppg3
from ppg3.tools import PyEnv

ppg3.new(stores=[ppg3.Store("main", "store")], default_python=PyEnv.current())

ppg3.CommandJob(
    outputs={"o": "scratch.txt"},
    argv=["/bin/sh", "-c", "echo transient > {out:o}"],
    retain=ppg3.Retain.Evict,
)
ppg3.CommandJob(
    outputs={"o": "release.txt"},
    argv=["/bin/sh", "-c", "echo keep-me > {out:o}"],
    retain=ppg3.Retain.Pin("v1.0-release"),
)
result = ppg3.run()
assert len(result.built) == 2

# Anything else is rejected when the job is defined:
try:
    ppg3.CommandJob(
        outputs={"o": "bad.txt"},
        argv=["/bin/sh", "-c", "echo x > {out:o}"],
        retain="keep",
    )
except ppg3.DefinitionError as e:
    assert "Retain" in str(e)
else:
    raise AssertionError("invalid retain= must be rejected")
```

## `store=` {#store}

With several stores configured, new entries land in the first writable one
by default. `store="name"` targets a specific (writable) store instead —
e.g. big scratch data to a fast local disk, curated results to the shared
store:

<!-- ppg3-example: jp-store requires=core -->
```python
import os
import ppg3
from ppg3.tools import PyEnv

ppg3.new(
    stores=[
        ppg3.Store("fast", "fast-store"),
        ppg3.Store("archive", "archive-store"),
    ],
    default_python=PyEnv.current(),
)

ppg3.CommandJob(   # no store= -> first writable store ("fast")
    outputs={"o": "scratch.txt"},
    argv=["/bin/sh", "-c", "echo scratch > {out:o}"],
)
ppg3.CommandJob(
    outputs={"o": "keeper.txt"},
    argv=["/bin/sh", "-c", "echo keeper > {out:o}"],
    store="archive",
)
ppg3.run()

assert len(os.listdir("fast-store/v1/entries")) == 1
assert len(os.listdir("archive-store/v1/entries")) == 1
assert open("outputs/keeper.txt").read() == "keeper\n"
```

## `tools=` {#tools}

Tools are inputs: the executables a job runs participate in its key, so a
tool upgrade re-runs exactly the jobs that use it. Two spellings:

- `ToolSpec.nix("github:owner/repo/<40-hex-rev>#attr", name=...)` — a
  pinned Nix flake reference. The resolved store path is mounted at
  `/ppg/tools/<name>` inside the sandbox (that's what `{tool:name}` /
  `ppg3.Tool("name")` resolve to), its `bin/` goes on the job's `PATH`,
  and the store path itself is the tool's identity. Unpinned references
  (no 40-char revision) are rejected at definition time.
- `ToolSpec.binary("/path/to/exe", name=...)` — a non-Nix fallback: the
  file's blake3 hash is the identity. Invoke it by its real path; the
  hash keys the job, so replacing the binary re-runs it.

<!-- ppg3-example: jp-tools-binary requires=core -->
```python
import ppg3
from ppg3.tools import PyEnv, ToolSpec

ppg3.new(stores=[ppg3.Store("main", "store")], default_python=PyEnv.current())

ppg3.CommandJob(
    outputs={"o": "made-with-sh.txt"},
    argv=["/bin/sh", "-c", "echo made > {out:o}"],
    # /bin/sh's content hash now keys this job: a different shell binary
    # (an OS upgrade, another machine) re-runs it instead of hitting.
    tools=[ToolSpec.binary("/bin/sh", name="sh")],
)
result = ppg3.run()
assert result.built == ["made-with-sh.txt"]
```

An unpinned Nix reference is refused immediately, no run needed:

<!-- ppg3-example: jp-tools-unpinned -->
```python
import ppg3
from ppg3.tools import ToolSpec

try:
    ToolSpec.nix("github:owner/repo#samtools")     # branch tip: not pinned
except ppg3.DefinitionError as e:
    assert "pin" in str(e).lower()
else:
    raise AssertionError("unpinned flake reference must be rejected")
```

And the pinned form, as used in a job (requires Nix on the host to run):

<!-- ppg3-example: fragment -->
```python
samtools = ToolSpec.nix(
    "github:nixos/nixpkgs/0123456789abcdef0123456789abcdef01234567#samtools",
    name="samtools",
)
ppg3.CommandJob(
    outputs={"bam": "aligned.bam"},
    argv=["/bin/sh", "-c", "{tool:samtools}/bin/samtools view -b {in:sam} > {out:bam}"],
    inputs={"sam": ppg3.File("aligned.sam")},
    tools=[samtools],
)
```

## `python=` {#python}

Per-job override of
[`ppg3.new(default_python=...)`]({{< relref "new#default_python" >}}) —
for the one job that needs a different environment (or a heavy preload)
without changing every other job's identity:

<!-- ppg3-example: jp-python requires=core -->
```python
import ppg3
from ppg3.tools import PyEnv

ppg3.new(stores=[ppg3.Store("main", "store")], default_python=PyEnv.current())

def report_json(io):
    import sys
    with open(io.path("o"), "w") as fh:
        fh.write("json preloaded: %s" % ("json" in sys.modules))

ppg3.FileJob(
    outputs={"o": "probe.txt"},
    run=report_json,
    python=PyEnv.current(preload=["json"]),   # this job only
)
result = ppg3.run()
assert result.built == ["probe.txt"]
```

(The preload lands in the warm template, so whether `sys.modules` already
holds `json` depends on the dispatch path — the point of the example is
the per-job `python=`, which re-keys just this job.)
