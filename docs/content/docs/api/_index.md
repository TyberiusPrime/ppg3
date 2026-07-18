---
title: "API by example"
weight: 20
bookCollapseSection: false
---

# ppg3 API by example

This section is a reference-by-example for the ppg3 Python API. Where the
[tutorial]({{< relref "/docs/tutorial" >}}) walks you through one pipeline
start to finish, these pages take each function and each parameter in turn
and show a small, complete, runnable script for it.

Every example marked with a `ppg3-example` comment in the page source is a
**standalone script**: it is extracted verbatim by the test suite
(`python/tests/test_docs_examples.py`) and executed in a fresh scratch
directory on every test run. If an example on these pages stops working,
CI fails — the examples cannot drift from the implementation.

## Pages

1. [`ppg3.new()` — every parameter]({{< relref "new" >}}): stores,
   `default_python`, `project_dir`, `parallelism`, `frozen`, `paranoid`,
   `forkserver`, `jj`, and `sandbox`.
2. [`ppg3.run()` — every parameter]({{< relref "run" >}}): `graph`,
   `project_id`, `ephemeral` — plus `RunResult`, `PPGRunError`, and
   `session_stop()`.
3. [Job types]({{< relref "jobs" >}}): `CommandJob`, `FileJob`, `DataJob`,
   `FetchJob`, `GraphJob`, and `UnsandboxedJob`.
4. [Inputs, outputs, and the `io` object]({{< relref "inputs-and-outputs" >}}):
   the four input kinds, internal outputs, `below=`, publish conflicts, and
   the callback-side `io` API.
5. [Shared job parameters]({{< relref "job-parameters" >}}): `env`,
   `resources`, `retain`, `store`, `tools`, and `python`.

## Conventions used in the examples

- Examples run in an **empty scratch directory**, so relative paths like
  `"store"` or `".ppg3"` refer to that directory.
- Examples use `PyEnv.current()` — the interpreter running the script — so
  they work anywhere ppg3 is installed, without Nix.
- Shell commands in `CommandJob`s call binaries by absolute path
  (`/bin/sh`, `/bin/cp`): jobs run with a minimal declared environment, so
  the host's `$PATH` is not available inside a job.
