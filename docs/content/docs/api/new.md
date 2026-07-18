---
title: "ppg3.new()"
weight: 21
---

# `ppg3.new()` — every parameter

<!-- ppg3-example: fragment -->
```python
ppg3.new(
    stores=None,            # list of ppg3.Store — where results are cached
    default_python=None,    # PyEnv for Python jobs without their own python=
    project_dir=".ppg3",    # pointer/cache state: views, work dirs, stat-cache
    parallelism=None,       # resource pool capacities; default {"cores": N}
    frozen=None,            # reproducibility guard; default: auto-detect
    paranoid=False,         # always ship callbacks as checked source
    forkserver=True,        # warm template processes for Python jobs
    jj=False,               # jj (jujutsu) source tracking + provenance
    sandbox="auto",         # "require" | "auto" | "off"
) -> Graph
```

`ppg3.new()` creates a `Graph` and makes it the **current graph**: job
constructors called afterwards register with it implicitly, and a bare
`ppg3.run()` runs it. You normally call it exactly once, at the top of your
pipeline script.

The sections below take each parameter in turn.

## `stores=` — where built artifacts live {#stores}

A `ppg3.Store(name, path, readonly=False)` is a content-addressed directory
of build results. Every job's outputs are content in a store; the
`outputs/` tree you browse is just symlinks into it.

At least one **writable** store is required — `run()` refuses to start
without one. New entries land in the *first writable* store, unless a job
targets a specific one with [`store=`]({{< relref "job-parameters#store" >}}).

A `readonly=True` store is consulted for cache **hits** but never written —
not even GC root registration touches it. That makes it the right shape for
a shared team cache: CI populates it, everyone else lists it read-only and
gets its results for free. This example builds a store in one project, then
hits it read-only from a second project that has its own writable store:

<!-- ppg3-example: new-stores-readonly requires=core -->
```python
import os
import ppg3
from ppg3.tools import PyEnv

def greeting_job():
    ppg3.CommandJob(
        outputs={"greeting": "greeting.txt"},
        argv=["/bin/sh", "-c", "echo hello > {out:greeting}"],
    )

shared = os.path.abspath("shared-store")

# Project 1 (think: CI) populates the shared store.
os.makedirs("ci", exist_ok=True)
os.chdir("ci")
ppg3.new(stores=[ppg3.Store("shared", shared)], default_python=PyEnv.current())
greeting_job()
r1 = ppg3.run()
assert r1.built == ["greeting.txt"]

# Project 2 (think: your laptop) lists it readonly, plus a local
# writable store for anything the shared cache doesn't have.
os.chdir("..")
os.makedirs("laptop", exist_ok=True)
os.chdir("laptop")
ppg3.new(
    stores=[
        ppg3.Store("local", "local-store"),
        ppg3.Store("shared", shared, readonly=True),
    ],
    default_python=PyEnv.current(),
)
greeting_job()
r2 = ppg3.run()
assert r2.hits == ["greeting.txt"]     # served from the readonly store
assert r2.built == []                  # nothing had to run
```

Store **names** are stable labels recorded in metadata; the **path** is
where the `v1/` layout is created on first write. A store can move on disk
without invalidating anything, as long as its name stays the same.

## `default_python=` — the interpreter for Python jobs {#default_python}

`FileJob`, `DataJob`, and `FetchJob` run in a subprocess under a `PyEnv`.
A job may carry its own [`python=`]({{< relref "job-parameters#python" >}});
`default_python=` is what every job without one uses. If a Python job ends
up with no `PyEnv` at all, its constructor raises `DefinitionError`.

<!-- ppg3-example: new-default-python requires=core -->
```python
import ppg3
from ppg3.tools import PyEnv

ppg3.new(
    stores=[ppg3.Store("main", "store")],
    # The interpreter running this script, with `json` imported into the
    # warm template before each job (a cheap way to amortize heavy imports
    # like numpy across many jobs).
    default_python=PyEnv.current(preload=["json"]),
)

def dump(io):
    import json
    with open(io.path("cfg"), "w") as fh:
        json.dump({"threads": 4}, fh)

ppg3.FileJob(outputs={"cfg": "config.json"}, run=dump)
result = ppg3.run()
assert result.built == ["config.json"]
```

`PyEnv.current()` is *weakly hermetic*: its identity is the interpreter's
real path and version — reproducible enough for local work, but not pinned.
For full hermeticity, pin a Nix flake environment (the reference must
contain a 40-hex-char revision; a bare branch name is a definition-time
error):

<!-- ppg3-example: fragment -->
```python
ppg3.new(
    stores=[ppg3.Store("main", "store")],
    default_python=PyEnv.nix(
        "github:you/env/0123456789abcdef0123456789abcdef01234567#python",
        preload=["numpy"],
    ),
)
```

## `project_dir=` — pointer and cache state {#project_dir}

Everything under `project_dir` (default `".ppg3"`) is *derived* state: the
generation views, per-run work directories, the stat-cache, the store list
(`config.json`), and the last-run report. The store holds the real data;
deleting `project_dir` loses history and convenience pointers, never
results. The browsable `outputs/` symlink is created **next to**
`project_dir` (in its parent directory) and points at the current
generation.

<!-- ppg3-example: new-project-dir requires=core -->
```python
import os
import ppg3
from ppg3.tools import PyEnv

ppg3.new(
    stores=[ppg3.Store("main", "store")],
    default_python=PyEnv.current(),
    project_dir="state/my-pipeline",
)
ppg3.CommandJob(
    outputs={"greeting": "greeting.txt"},
    argv=["/bin/sh", "-c", "echo hi > {out:greeting}"],
)
ppg3.run()

assert os.path.isfile("state/my-pipeline/config.json")     # the store list
assert os.path.isdir("state/my-pipeline/views/1")          # generation 1
assert os.path.isfile("state/my-pipeline/last_run.json")   # per-job report
# outputs/ appears next to the project dir, i.e. under state/:
assert open("state/outputs/greeting.txt").read() == "hi\n"
```

## `parallelism=` — resource pool capacities {#parallelism}

`parallelism` is a dict of named **resource pools** and their capacities.
Jobs draw units from pools with
[`resources=ppg3.Resources(...)`]({{< relref "job-parameters#resources" >}});
a job that declares nothing consumes no pool units and is bounded only by
the worker thread count. The default is `{"cores": os.cpu_count()}`.

Two rules are enforced *before any job runs*: requesting a pool that isn't
configured, or more units than a pool's total capacity, is a definition
error — it could never be satisfied, so the run fails fast.

The reserved key `"workers"` is not a pool: it sets the number of scheduler
worker threads (default 4) — how many jobs can be *dispatched* at once,
regardless of pools.

<!-- ppg3-example: new-parallelism requires=core -->
```python
import ppg3
from ppg3.tools import PyEnv

ppg3.new(
    stores=[ppg3.Store("main", "store")],
    default_python=PyEnv.current(),
    parallelism={"cores": 4, "downloads": 2, "workers": 8},
)

# At most two of these run concurrently, whatever the worker count:
for i in range(5):
    ppg3.CommandJob(
        outputs={"f": f"dl/{i}.txt"},
        argv=["/bin/sh", "-c", f"echo download-{i} > {{out:f}}"],
        resources=ppg3.Resources(downloads=1),
    )
# A big job can claim the whole cores pool for itself:
ppg3.CommandJob(
    outputs={"f": "big.txt"},
    argv=["/bin/sh", "-c", "echo big > {out:f}"],
    resources=ppg3.Resources(cores=4),
)

result = ppg3.run()
assert len(result.built) == 6
```

## `frozen=` — the reproducibility guard {#frozen}

`frozen` controls whether interactive-only conveniences are allowed. Today
that means one thing: a `FetchJob` without a pinned `blake3=` hash
(trust-on-first-use, TOFU). The default is context-aware:

- interactive terminal → `frozen=False` (TOFU allowed; after a successful
  run ppg3 patches the real hash back into your source),
- non-interactive or CI (`CI`, `GITHUB_ACTIONS`, … env vars) →
  `frozen=True` (everything must be pinned).

Under `frozen=True` an unpinned fetch is rejected *at definition time*,
before anything runs:

<!-- ppg3-example: new-frozen -->
```python
import ppg3
from ppg3.tools import PyEnv

ppg3.new(
    stores=[ppg3.Store("main", "store")],
    default_python=PyEnv.current(),
    frozen=True,
)

try:
    ppg3.FetchJob(
        outputs="data.csv",
        url="https://example.com/data.csv",
        # no blake3= pin
    )
except ppg3.DefinitionError as e:
    assert "frozen" in str(e)
else:
    raise AssertionError("unpinned FetchJob must be rejected when frozen")
```

Everything else — `FileJob`, `CommandJob`, pinned fetches — behaves
identically in both modes. Pipeline scripts that should behave the same on
laptops and CI (like this tutorial's) simply pass `frozen=False` or
`frozen=True` explicitly.

## `paranoid=` — always ship callbacks as checked source {#paranoid}

ppg3 sends a `FileJob`/`DataJob` callback to its sandboxed subprocess by
one of two transports: pickling it (cloudpickle), or shipping its **source
text** after statically checking that the function only references its own
arguments, locals, and body-level imports ("localscope"). `paranoid=True`
forces the source transport for every callback — you find out at
*definition time* if a callback smuggles in module-level state, instead of
getting a job whose cached identity silently ignores that state:

<!-- ppg3-example: new-paranoid -->
```python
import ppg3
from ppg3.tools import PyEnv

ppg3.new(
    stores=[ppg3.Store("main", "store")],
    default_python=PyEnv.current(),
    paranoid=True,
)

LIMIT = 3  # module-level state the callback (illegally) reaches for

def write_limit(io):
    with open(io.path("n"), "w") as fh:
        fh.write(str(LIMIT))   # not an argument, local, or body-level import

try:
    ppg3.FileJob(outputs={"n": "n.txt"}, run=write_limit)
except ppg3.DefinitionError as e:
    assert "LIMIT" in str(e)
else:
    raise AssertionError("localscope must reject the module-global reference")
```

The fix is always the same: pass the value in as a
`ppg3.Params(...)` input (so it participates in the job's cache key) or
import/compute it inside the function body.

## `forkserver=` — warm templates for Python jobs {#forkserver}

With `forkserver=True` (the default), Python jobs are dispatched by forking
a warm, preloaded **template process** per `(PyEnv, preload)` combination
instead of exec-ing a cold `python -I` per job — with `preload=["numpy"]`,
you pay numpy's import once, not once per job. `forkserver=False` opts out
and every job gets a cold interpreter start:

<!-- ppg3-example: new-forkserver requires=core -->
```python
import ppg3
from ppg3.tools import PyEnv

ppg3.new(
    stores=[ppg3.Store("main", "store")],
    default_python=PyEnv.current(),
    forkserver=False,   # cold interpreter start per Python job
)

def write(io):
    with open(io.path("out"), "w") as fh:
        fh.write("cold start")

ppg3.FileJob(outputs={"out": "out.txt"}, run=write)
result = ppg3.run()
assert result.built == ["out.txt"]
```

Job identity is unaffected: the same pipeline hashes identically with the
forkserver on or off. Note that when a real sandbox is enforced
(see `sandbox=` below), ppg3 disables the templates automatically —
template children would run outside the enforcement.

## `jj=` — jj (jujutsu) source tracking {#jj}

With `jj=True`, `run()` refuses to start unless every file that *defines
jobs* (your pipeline script, `ppg3.Source` files, callback source files) is
tracked in the enclosing [jj](https://github.com/jj-vcs/jj) workspace, and
it records the jj commit/change/operation ids into each generation's
metadata — every generation then says exactly which source state produced
it. Without a jj workspace (or the `jj` binary), the run fails up front
with a `JJError`:

<!-- ppg3-example: new-jj requires=core -->
```python
import ppg3
from ppg3.tools import PyEnv

ppg3.new(
    stores=[ppg3.Store("main", "store")],
    default_python=PyEnv.current(),
    jj=True,
)
ppg3.CommandJob(
    outputs={"o": "o.txt"},
    argv=["/bin/sh", "-c", "echo x > {out:o}"],
)

try:
    ppg3.run()
except ppg3.JJError as e:
    # This scratch directory is not a jj workspace, so the run is refused
    # before any job is dispatched. The message names what is missing
    # (no workspace, an untracked source file, or the jj binary itself).
    print("refused:", e)
else:
    # In a real jj workspace with all sources tracked, the run proceeds
    # and the generation's meta.json carries the jj provenance ids.
    pass
```

## `sandbox=` — enforcement policy {#sandbox}

Jobs are *meant* to run isolated (bwrap + Nix closures), so undeclared
inputs are invisible rather than silently keying nothing. Enforcement
depends on what the host offers; `sandbox=` says what to do about that:

- `"require"` — error at run start if real enforcement is unavailable.
  For CI, where "ran unsandboxed" should be a failure, not a warning.
- `"auto"` (default) — use the best available enforcement; warn exactly
  once per run when running unenforced.
- `"off"` — explicitly opt out, silently.

Anything else is rejected at `ppg3.new()` time:

<!-- ppg3-example: new-sandbox -->
```python
import ppg3
from ppg3.tools import PyEnv

try:
    ppg3.new(
        stores=[ppg3.Store("main", "store")],
        default_python=PyEnv.current(),
        sandbox="yes-please",
    )
except ppg3.DefinitionError as e:
    assert "require" in str(e) and "auto" in str(e) and "off" in str(e)
else:
    raise AssertionError("invalid sandbox= value must be rejected")

# The three valid values, for the record:
for value in ("require", "auto", "off"):
    ppg3.new(
        stores=[ppg3.Store("main", "store")],
        default_python=PyEnv.current(),
        sandbox=value,
    )
```

Next: [`ppg3.run()` — every parameter]({{< relref "run" >}}).
