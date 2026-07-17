# ppg3 — principles

Status: constitution. PPG3_DESIGN.md stays the *mechanism* document; this file
is the list of falsifiable promises the mechanism exists to keep. Where the
two disagree, this file wins and the design doc gets amended. Where the
*implementation* disagrees, that is a bug — tracked, not argued with.

Why this file exists: the first implementation pass kept the machinery of the
design (store, keys, sandbox plumbing) but lost several of its load-bearing
ideas — jobs grew user-facing names and name conflicts, publish paths leaked
into store entries, fetch pins stopped pinning anything, the sandbox became
unreachable, and error output stopped naming the things it talks about. Each
of those is a principle violation, not a paper cut. This document states the
principles as invariants precise enough to test; the test schema that asserts
them lives in `python/tests/principles/` (see `python/tests/principles/README.md`),
with one manifest entry per invariant below.

Conventions:

- Every invariant has an id (`P1.2`). Every id has at least one test; a
  meta-test enforces the mapping in both directions.
- Invariant statuses live in `python/tests/principles/invariants.json`:
  `enforced` (test passes), `violated` (known broken; its test is
  `xfail(strict=True)`, so fixing it *forces* the status flip), `untested`
  (test exists but needs engine wiring it cannot reach yet).
- "Definition time" = while the pipeline script executes job constructors.
  "Engine" = requires `ppg3._core`.

---

## P1 — Identity is content, not names

A job's identity is its input key: recipe, inputs, tools, runtime, env,
declared output *names*. Nothing else. Users never name jobs, because a name
would be a second identity that can drift from the first — it can conflict,
it can go stale, and (as the first pass demonstrated) it shows up nowhere
useful. When ppg3 must refer to a job in front of a human, it uses the things
the human actually wrote: the paths the job publishes and the source location
that defined it (`pipeline.py:42`).

Consequences:

- There is no `name=` keyword on any job constructor. The scheduler may use
  internal run-local handles, but they are not assignable, not user-visible,
  and not persisted.
- Renaming anything — a published path, a variable, a function — never causes
  recomputation *and never causes a conflict*. Identity cannot be renamed
  because it was never a name.
- Store entries are a function of the input key alone. In particular the
  *publish location* (where a generation links an output) must not shape the
  entry's content: jobs write `/ppg/out/<output-name>`, and the mapping
  output-name → tree path lives entirely in the pointer layer (P3). The
  first pass wired `{out:NAME}` to resolve to the *publish path*, so moving
  an output's destination changed the bytes' layout under the same input key
  — an ik→oh collision manufactured out of a rename.

Invariants:

- **P1.1** No public job constructor accepts `name=`.
- **P1.2** Defining two equal jobs (same recipe, inputs, outputs) that
  publish to *different* paths is legal and is not a conflict.
- **P1.3** Changing only a published path between runs causes zero rebuilds
  (pure re-link). (engine)
- **P1.4** Entry content layout is derived from output names, never from
  publish paths: the same job published at two different tree paths yields
  one store entry. (engine)
- **P1.5** Every user-facing reference to a job (reports, errors, CLI) shows
  its published path(s) and/or its definition site; internal handles never
  appear alone.

## P2 — The only conflict is a contested destination

Deduplication is the point of the system; two identical jobs are a cache hit,
not an error. The only thing two jobs can genuinely fight over is a
*destination*: one tree path cannot be published by two different jobs. That
— and nothing else — is a definition-time conflict, and its error message
must show both definition sites, because the user's next action is to open
those two lines.

Invariants:

- **P2.1** Two different jobs publishing the same tree path → definition-time
  error naming both call sites (file:line each).
- **P2.2** Two jobs with identical inputs/recipe but different destinations:
  built once, linked twice. (engine)
- **P2.3** No other definition-time uniqueness error is reachable: the
  "duplicate job id" error class does not exist.

## P3 — One truth; everything else is a pointer or a cache

Every file ppg3 writes is classifiable as exactly one of:

1. **Content** (authoritative, immutable): store entries, the ik→entry memo
   links. Lives in a store. Never rewritten, only GC'd.
2. **Pointer state** (authoritative, mutable, small): which generation is
   current, what a generation maps where, project config, pins/roots. Lives
   in the project dir (`.ppg3/`). Loss = losing *history/selection*, never
   losing built content.
3. **Cache** (non-authoritative): stat-cache, logs, warm templates, anything
   whose deletion costs recomputation or re-hashing but can never change a
   result or lose history.

That is the entire answer to "why is there a store *and* a `.ppg3` folder":
the store is truth shared between projects/machines; `.ppg3` is this
project's pointers into it, plus caches. Anything that doesn't fit exactly
one class is a design error. Direct corollaries:

- No authoritative datum exists in two places. A generation is *either* a
  record from which the link tree is derived *or* the link tree itself — not
  both with silent disagreement possible (the first pass's `meta.json`
  recapitulating the symlinks fails this).
- Configuration errors about truth are fatal *early*: a run with no writable
  store fails before the first job is dispatched, not at every job.
- Caches are deletable at any time with impunity; the store is deletable only
  through an explicit command (`ppg3 store nuke`), because it is truth.

Invariants:

- **P3.1** Zero writable stores → the run fails at start with a
  configuration error; no job runs, no per-job error spam.
- **P3.2** Delete every cache-class file; rerun → zero rebuilds. (engine)
- **P3.3** Delete the project dir, keep the store; rerun → zero rebuilds
  (generation history is gone, content is not). (engine)
- **P3.4** Generation pointer state has a single authoritative form; the
  derived form (link tree) can be regenerated from it and disagreement is
  detected, not silent. (engine)

## P4 — A declared input is a readable input

Declaring an input buys the job two things at once, inseparably: the input's
content participates in the job's identity, *and* the job can read those
bytes at a stable virtual path. There is no such thing as a hash-only
dependency the job cannot read — that split is how the first pass shipped
jobs that were invalidated by files they couldn't open. (If pure
"rebuild-when-this-changes" triggers are ever wanted, they are a distinct,
explicitly named concept — never the default meaning of `inputs=`.)

Invariants:

- **P4.1** A `ppg3.File` input is byte-identical readable at
  `/ppg/in/<name>` inside the job. (engine)
- **P4.2** A parent-job input exposes the parent's outputs, byte-identical.
  (engine)
- **P4.3** A subset reference `parent["x"]` exposes exactly output `x`, and
  keys on exactly output `x`'s hash (early cutoff per parent-output).
  (engine)
- **P4.4** `Params` inputs are readable via `io.params`, value-identical
  after canonicalization round-trip. (engine)

## P5 — Same key, same bytes; everything that runs is an input

(Unchanged from PPG3_DESIGN.md §2, restated as invariants.) A job is a pure
function from declared inputs to output bytes. Tools, interpreter
environments, declared env vars, and the recipe are all inputs. A determinism
violation is a build failure with a report a human can act on — and per P9
that report names both entries on disk, the differing files, and the jobs
involved.

Invariants:

- **P5.1** Publishing different bytes under an existing input key is a hard
  failure, never a warning. (engine)
- **P5.2** The violation report includes: both entry paths, per-file diff
  summary, the job's published paths + definition site, and a suggested
  `ppg3 diff-entries` invocation. (engine)
- **P5.3** Changing a tool's hash changes the key → rebuild. (engine)
- **P5.4** Changing a declared env var changes the key; undeclared env vars
  are unreadable inside the job. (engine)

## P6 — The sandbox is a feature you can hold

Sandboxing is the enforcement arm of P4/P5 — without it, "declared inputs"
is a comment. So it must be: **on by default where possible, requirable,
declinable, and always visible.** One knob at graph creation:

```python
ppg3.new(sandbox="require" | "auto" | "off")   # default: "auto"
```

- `require`: enforcement unavailable → error at run start, before any job.
- `auto`: best available enforcement; running *without* enforcement prints
  exactly one prominent warning per run (not per job, not zero).
- `off`: explicit, recorded, no nagging.
- Every manifest records the enforcement that actually applied to that entry
  (`sandboxed: true/false`), and `ppg3 verify` treats unsandboxed entries as
  first-choice re-verification targets.

The first pass has a real bwrap executor and a real unshare entry — and no
path from `ppg3.new()` to either (the PyO3 `run` hardwires the
no-enforcement executor). "I don't know how to enable the sandbox" was the
correct reading: it cannot be enabled.

Invariants:

- **P6.1** `ppg3.new(sandbox=...)` exists, and the chosen mode reaches
  executor selection.
- **P6.2** `sandbox="require"` on a host without enforcement fails at run
  start with instructions. (engine)
- **P6.3** Undeclared input → ENOENT inside the job; no network unless
  fixed-output; env contains only the declared + scrubbed baseline. (engine)
- **P6.4** Manifests record actually-applied enforcement. (engine)
- **P6.5** Unenforced runs under `auto` warn exactly once per run.

## P7 — A fetch pin is an identity, not a comment

`FetchJob` is the one impurity door, and it is only sound if the *pin* —
`(url, expected-hash)` — is the job's whole identity. Both halves key it:

- Change the hash → the old entry no longer satisfies the job; refetch,
  verify against the new pin.
- Remove the hash → TOFU (interactive only): refetch, pin, patch source.
  Frozen/CI: definition-time error, as today.
- Change the URL → refetch, even when an entry with matching content exists —
  nine times out of ten the user changed the URL and *forgot* the hash, and
  a silent hit on stale content is exactly the wrong answer. Worst case it
  costs one download.
- A satisfied pin never re-downloads. A failed verification reports url,
  expected hash, actual hash, and the path where the rejected bytes were
  kept for inspection (P9).

The first pass keyed fetches on the URL alone (`blake3=` went only into
post-hoc output verification), so editing or deleting the hash changed
nothing — the memo table still hit. That inverts the trust model: the pin
must *select* the content, not merely complain about it afterwards.

Invariants:

- **P7.1** The declared hash participates in the input key: two `FetchJob`s
  differing only in `blake3=` have different keys.
- **P7.2** Removing `blake3=` (interactive) → refetch + TOFU re-pin, never a
  silent hit on the previously pinned entry. (engine)
- **P7.3** Changing the URL → refetch even if content matching the pin is
  already in the store. (engine)
- **P7.4** Verification failure reports url, expected hash, got hash, and
  the retained path of the rejected download. (engine)

## P8 — Finished work is never withheld

A multi-day run whose 900th job fails must not hide the 899 finished
results. The store already makes this cheap: entries publish per-job. The
principle is about *reachability*:

- Every job's outputs are reachable by the user the moment the job finishes,
  through a stable, reported path — during the run, after a failed run,
  always.
- The `current` generation pointer stays transactional: it only ever points
  at a complete, consistent snapshot (that part of the first pass is right
  and stays).
- A failed run therefore leaves: all successful entries in the store
  (rerunning is pure hits), a *partial* generation — clearly marked, never
  `current` — linking everything that did finish, and a report that says
  where it is.

Invariants:

- **P8.1** After a failed run, rerunning with the failure fixed rebuilds
  only the failed job and below — everything that succeeded is a hit.
  (engine)
- **P8.2** A failed run leaves an inspectable partial generation (marked,
  not `current`) and reports its path. (engine)
- **P8.3** `current` never points at a partial snapshot. (engine)

## P9 — Every failure names its artifacts

An error message is a set of directions. Every failure ppg3 reports must
carry, where they exist:

1. *which job*, in the user's terms: published path(s) and definition site
   (`pipeline.py:42`) — per P1.5;
2. *the evidence*: log path (and when exactly one job failed, the log tail
   inline);
3. *the objects involved*: paths to the store entries / staging / rejected
   downloads the message is about — never just their hashes;
4. *the next step*: the command that continues the investigation
   (`ppg3 diff-entries …`, `ppg3 explain …`), when one exists.

No internal identifier (input key, output hash) appears in user-facing text
without an accompanying filesystem path or user-recognizable name.
Definition-time errors point at the offending source line(s).

Invariants:

- **P9.1** Job-failure blocks include the definition site.
- **P9.2** Job-failure blocks include a log path; single-failure runs inline
  the log tail. (engine)
- **P9.3** Determinism-violation and fetch-mismatch reports include the
  on-disk paths of every artifact they mention (both entries; the rejected
  download). (engine)
- **P9.4** No user-facing failure text contains a bare hash with no
  accompanying path.

## P10 — One word per concept

Terminology is API. Every concept gets exactly one name, chosen from the
user's vocabulary, and internal jargon never leaks. The ratified table:

| concept                                   | name              | replaces        |
|-------------------------------------------|-------------------|-----------------|
| per-job map: output name → tree path      | `outputs=`        | `view=`         |
| the linked tree users browse              | output tree       | "view"          |
| a numbered snapshot of the output tree    | generation        | (kept)          |
| immutable content-addressed truth         | store             | (kept)          |
| input key / output hash in user-facing UI | spelled out, with paths (P9.4) | `ik`/`oh` |

And two API consequences:

- `outputs=` is *optional*: a job without published outputs is an internal
  job — perfectly legal (its consumers reference it directly), it simply
  doesn't appear in the output tree.
- There is exactly one publishing vocabulary; no second mechanism, no
  synonyms.

Invariants:

- **P10.1** Job constructors take `outputs=`; `view=` does not exist.
- **P10.2** A `FileJob`/`CommandJob` without `outputs=` is legal and its
  consumers can depend on it. (engine for the consumption half)

---

## The complaint → principle map

The concrete failures that prompted this document, and which invariant now
owns each:

| complaint | invariant(s) |
|---|---|
| "why do I need to name jobs" | P1.1 |
| "why do jobs conflict" | P2.1–P2.3 |
| "why do the names show up nowhere" | P1.5, P9.1 |
| "why is there a store and a .ppg3 folder" | P3 (preamble), P3.1–P3.4 |
| "why do jobs have to have 'view' definitions (stupid name)" | P10.1, P10.2 |
| "why don't fetchjobs ever invalidate" | P7.1–P7.3 |
| "I had to patch in that jobs get the bytes of their byte inputs" | P4.1–P4.4 |
| "I still don't know how to enable the damn sandbox" | P6.1–P6.5 |
| "no output on job fail is idiotic" | P8.1–P8.3 |
| "the error message … is atrocious" | P5.2, P7.4, P9.1–P9.4 |

Everything in `todo.md` that is *not* covered here is a genuine paper cut:
fix it whenever, no principle at stake. Everything that *is* covered here
gets fixed by making its invariant's test pass — in manifest order, P1 → P10.
