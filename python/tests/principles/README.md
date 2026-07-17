# The principles suite

This directory is the executable half of `PRINCIPLES.md` (repo root): one
test (at least) per invariant, plus meta-tests that keep the three artifacts
— `PRINCIPLES.md`, `invariants.json`, and the `test_p*.py` files — in sync.

## How it works

- `invariants.json` is the single source of truth for each invariant's
  **status**:
  - `enforced` — the implementation keeps the promise; its test is a normal
    test and a failure is a regression.
  - `violated` — a known principle violation. Its tests assert the **target**
    behavior and are `xfail(strict=True)`: they fail today by design, and the
    moment a fix lands they *pass*, which strict-xfail turns into an error —
    forcing the same change to flip the manifest status to `enforced`. The
    manifest can therefore never claim more brokenness than is real.
  - `untested` — the assertion needs wiring that doesn't exist yet (e.g. a
    verification subcommand); the test body documents the intent and is
    skipped.
- `conftest.py` provides `principle("P1.2")`, which applies the status
  treatment, and target-API construction shims (`new_graph`, `file_job`,
  `command_job`, `fetch_job`) so that *semantic* tests don't churn when the
  P10 rename (`view=` → `outputs=`) lands. Only the shims and the
  API-shape tests (P1.1, P10.x) reference the constructor keyword directly.
- `test_meta.py` enforces: every invariant id has a test, every test id
  exists in the manifest, every id appears in `PRINCIPLES.md`, and statuses
  are from the valid set.

## The intended workflow ("fix into working shape")

1. Pick the lowest-numbered `violated` invariant (P1 → P10 is dependency
   order: identity before conflicts before storage before UX).
2. Make its test(s) pass. Strict xfail will scream.
3. Flip its status to `enforced` in `invariants.json` in the same commit.
4. Repeat. `PRINCIPLES.md` never changes during this loop — if you find
   yourself wanting to change a principle rather than the code, that's a
   design discussion, not a fix.

Run just this suite with:

    uv run pytest python/tests/principles/ -q

Engine-level tests skip without the compiled extension (`uv sync` builds it).
