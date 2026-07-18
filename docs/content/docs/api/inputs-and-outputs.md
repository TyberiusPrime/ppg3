---
title: "Inputs, outputs & io"
weight: 24
---

# Inputs, outputs, and the `io` object

## `inputs=` — the four kinds of input

`inputs=` is a dict mapping an **input name** (how the job's own code
refers to it) to one of four things:

| Value | Meaning | Inside the callback |
| --- | --- | --- |
| a job object | depend on *all* of that job's outputs | `io.input(name)` → its file (or directory of files) |
| `job["out_name"]` | depend on *one named output* | `io.input(name)` → that file |
| `ppg3.File(path)` | a leaf file; content-hashed | `io.input(name)` → the file |
| `ppg3.Params(value)` | a leaf value; canonicalized | `io.params[name]` → the value |

Each input contributes to the job's cache identity: a changed parent
output, file content, or parameter value re-runs the job; anything
unchanged is a hit. This example uses all four at once:

<!-- ppg3-example: io-input-kinds requires=core -->
```python
import ppg3
from ppg3.tools import PyEnv

ppg3.new(stores=[ppg3.Store("main", "store")], default_python=PyEnv.current())

with open("raw.txt", "w") as fh:
    fh.write("7 11\n")

def split(io):
    with open(io.input("raw")) as fh:
        a, b = fh.read().split()
    with open(io.path("first"), "w") as fh:
        fh.write(a)
    with open(io.path("second"), "w") as fh:
        fh.write(b)

both = ppg3.FileJob(
    outputs={"first": "first.txt", "second": "second.txt"},
    run=split,
    inputs={"raw": ppg3.File("raw.txt")},        # 1. leaf file
)

def scale(io):
    with open(io.input("value")) as fh:          # just first.txt, not both
        v = int(fh.read())
    with open(io.path("scaled"), "w") as fh:
        fh.write(str(v * io.params["factor"]))

ppg3.FileJob(
    outputs={"scaled": "scaled.txt"},
    run=scale,
    inputs={
        "value": both["first"],                  # 2. named-subset reference
        "factor": ppg3.Params(3),                # 3. leaf parameter
    },
)

def audit(io):
    entry = io.input("everything")               # 4. whole-job dependency:
    names = sorted(p.name for p in entry.iterdir())   # a directory here,
    with open(io.path("audit"), "w") as fh:           # since `both` has two
        fh.write(",".join(names))                     # outputs
ppg3.FileJob(
    outputs={"audit": "audit.txt"},
    run=audit,
    inputs={"everything": both},
)

ppg3.run()
assert open("outputs/scaled.txt").read() == "21"
assert open("outputs/audit.txt").read() == "first,second"
```

Asking for an output a job never declared fails at *definition* time:

<!-- ppg3-example: io-undeclared-output -->
```python
import ppg3
from ppg3.tools import PyEnv

ppg3.new(stores=[ppg3.Store("main", "store")], default_python=PyEnv.current())

job = ppg3.CommandJob(
    outputs={"listing": "listing.txt"},
    argv=["/bin/sh", "-c", "echo x > {out:listing}"],
)
try:
    job["typo"]
except ppg3.DefinitionError as e:
    assert "listing" in str(e)     # the error lists the declared names
else:
    raise AssertionError("undeclared output reference must be rejected")
```

`ppg3.Params` accepts a closed set of types — `None`, `bool`, `int`,
`float`, `str`, `bytes`, `list`/`tuple`, `dict`, `set`/`frozenset`, enums,
and dataclasses of those — canonicalized so that equal values always hash
equally. Anything else (an open file, a lambda, an arbitrary object) is a
definition-time error rather than a silently unstable key.

## `outputs=` — declaring and publishing outputs

`outputs=` maps each **output name** (how the job's code refers to the
file, via `io.path(name)` or `{out:name}`) to a destination path in the
output tree. Three forms matter:

- `outputs={"name": "path/in/view.txt"}` — declared **and published**.
- `outputs={"name": None}` — declared but **internal**: the job writes it,
  children can consume it, but it never appears under `outputs/`.
- `outputs=None` (or omitted) — an internal job with no published outputs.

<!-- ppg3-example: io-internal-outputs requires=core -->
```python
import os
import ppg3
from ppg3.tools import PyEnv

ppg3.new(stores=[ppg3.Store("main", "store")], default_python=PyEnv.current())

def make_intermediate(io):
    with open(io.path("scratch"), "w") as fh:
        fh.write("intermediate")

step1 = ppg3.FileJob(outputs={"scratch": None}, run=make_intermediate)

def finish(io):
    with open(io.input("part")) as fh:
        data = fh.read()
    with open(io.path("final"), "w") as fh:
        fh.write(data.upper())

ppg3.FileJob(
    outputs={"final": "final.txt"},
    run=finish,
    inputs={"part": step1},
)
ppg3.run()
assert open("outputs/final.txt").read() == "INTERMEDIATE"
# The internal output exists in the store, but not in the view:
assert not os.path.exists("outputs/scratch")
```

### `below=` — one folder for all of a job's outputs

`below="folder"` prefixes every published destination — pure convenience
so a job with many outputs doesn't repeat the folder in each path. It
changes only where things are published, never the job's identity:

<!-- ppg3-example: io-below requires=core -->
```python
import ppg3
from ppg3.tools import PyEnv

ppg3.new(stores=[ppg3.Store("main", "store")], default_python=PyEnv.current())

ppg3.CommandJob(
    outputs={"a": "a.txt", "b": "b.txt"},
    argv=["/bin/sh", "-c", "echo A > {out:a}; echo B > {out:b}"],
    below="sample1/qc",
)
result = ppg3.run()
# One job, two published outputs -> one label joining both destinations:
assert result.built == ["sample1/qc/a.txt+sample1/qc/b.txt"]
assert open("outputs/sample1/qc/a.txt").read() == "A\n"
assert open("outputs/sample1/qc/b.txt").read() == "B\n"
```

### One destination, one producer

The only definition-time uniqueness rule in ppg3: two **different** jobs
may not publish to the same destination. (Two *identical* definitions are
the same job — they merge silently; see below.) The conflict error names
both definition sites:

<!-- ppg3-example: io-conflict -->
```python
import ppg3
from ppg3.tools import PyEnv

graph = ppg3.new(stores=[ppg3.Store("main", "store")],
                 default_python=PyEnv.current())

def make(argv_text):
    ppg3.CommandJob(
        outputs={"o": "result.txt"},
        argv=["/bin/sh", "-c", argv_text],
    )

make("echo one > {out:o}")
make("echo one > {out:o}")          # identical definition: merges, no error
assert len(graph.jobs) == 1

try:
    make("echo DIFFERENT > {out:o}")   # different job, same destination
except ppg3.DefinitionError as e:
    assert "result.txt" in str(e)
else:
    raise AssertionError("contested destination must be rejected")
```

## The `io` object — the callback's window

Every Python callback receives one argument. Its full surface:

| Member | Returns | Notes |
| --- | --- | --- |
| `io.input(name)` | `pathlib.Path` | declared input's real path |
| `io.load(name)` | any | unpickle a `DataJob` input (memoized) |
| `io.path(name=None)` | `pathlib.Path` | where to write output `name`; name optional iff exactly one output |
| `io.out_path(name=None)` | `pathlib.Path` | alias of `io.path` |
| `io.params` | `dict` | all `ppg3.Params` inputs by input name |
| `io.tool(name)` | `str` | declared tool's path (for splicing into commands) |
| `io.log_dir` | `str` | directory for auxiliary log files |

Referencing an undeclared name raises `JobIOError` listing what *is*
declared. The single-output shorthand:

<!-- ppg3-example: io-object requires=core -->
```python
import ppg3
from ppg3.tools import PyEnv

ppg3.new(stores=[ppg3.Store("main", "store")], default_python=PyEnv.current())

def emit(io):
    with open(io.path(), "w") as fh:      # no name: exactly one output
        fh.write("only output")

ppg3.FileJob(outputs={"only": "only.txt"}, run=emit)
ppg3.run()
assert open("outputs/only.txt").read() == "only output"
```

Next: [shared job parameters]({{< relref "job-parameters" >}}).
