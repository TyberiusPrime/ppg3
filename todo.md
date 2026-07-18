
Resolved (branch claude/store-ppg3-gc-redesign-7vozjw):

- store / .ppg3 split: kept, and now justified — see PRINCIPLES.md P3
  ("one truth; everything else is a pointer or a cache") and PPG3_DESIGN.md
  §11.2/§11.3. Store = shared truth; .ppg3 = this project's pointers + caches.
  Generations/outputs living in .ppg3 (not the store) is correct by design.
- GC redesign: levels failed-only / minimal / default / aggressive / reset
  (`ppg3 gc --level`, `ppg3 store gc --level`), PPG3_DESIGN.md §11.2.
  gc now cleans staging + stale leases/intents + violations at every level;
  old/orphan build logs disappear (a swept entry's logs go with it; failed-
  build logs are reclaimed once stale). `reset` = "as if only the current
  generation ever ran here" (drops all other generations + pins).
- `ppg3 store nuke --store PATH --yes` (§11.3) — the rm-rf-the-store door.

Resolved (branch claude/ppg3-job-output-validation-lpxdse):

- missing declared outputs = regular aggregated job error with entry paths
  (see the annotated item below).
- `below=` on FileJob/CommandJob/DataJob/FetchJob/UnsandboxedJob: put all of
  a job's outputs below one folder (`outputs={"c": "counts.tsv"},
  below="samples/s1"` publishes samples/s1/counts.tsv). Pure publish-layer
  sugar — identical to writing the joined paths by hand.
- "why did this job not run / why is file xyz missing": failure reports now
  aggregate upstream casualties by root cause; every run writes
  `.ppg3/last_run.json`; `ppg3 why [path]` answers from it (status,
  definition site, root-cause chain, log + entry paths, whether the file is
  in the current output tree).

Open:

- pytest hangs? somewhere in test_watch.py
  but only after jj patch
  in test_e2e_watch_detects_change_ephemeral_generation_and_clean_sigint
  E       AssertionError: timed out after 30.0s waiting for: generation 1 to appear
  so it's technically not a hang :).

  well, it went away...

- do we want a webserver? maybe for the watcher?

- tmp should be local to store?
  [CLARIFIED — build staging already lives in `<store>/v1/staging/` so
   publish's rename() stays same-filesystem; the job's /tmp is a private
   tmpfs by design (§6.1). GC now reclaims crashed staging dirs (§11.2).]

- why do we need .ppg3 and a store?  [RESOLVED — see P3 / §11.2 preamble above]

- what's in todo?

-- when no writeable store is defined, fail early, not at every damn job
  [RESOLVED — `ppg3.run` raises once, before dispatch, when no writable
   store is configured (PRINCIPLES.md P3.1, run.py). Verified in source.]

-- we are corrently loosing the file names on our python tracebacks. Add those back in.
  [RESOLVED — frames carry their real filename again; and the `_shim.py`
   breadcrumb line was malformed (`_shim.py":<lineno>`, no indent) — now a
   clean `  <file>:<lineno>, in <name> (details skipped)`.]

- is producing extra files an error? do they get hashed?
  or are non-view files just not exported into the output?
  Think it should be the later.

- can we add local python files to the PyEnv as importable modules? Dependencies then?

- we currently can't have a FileJob that doesn't have a view. 
  Why?, nothing wrong with internal jobs! And do we need a stable job id?
  can't have conflicts on job_ids, but are they truly necessary?
  and if not, should we mayhaps link them so users can find their jobs?
  because right now, they do not end up in store at al.o
  [RESOLVED — internal jobs are legal (P10.2): omit `outputs=` entirely, or
   declare an output without publishing it via a None destination
   (`outputs={"data": None}`) — written by the job, keyed, consumable by
   children, cached in the store, just absent from the output tree. There
   is no user-facing job id; consumers hold the job object / `job["name"]`.]

- can't rm -rf the store?
  I mean I get it... maybe we add a ppg3 nuke-store
  [RESOLVED — `ppg3 store nuke --store PATH --yes`]

- binary not in nd...

- can we capture the python definition sites for the error output?
  [RESOLVED — every job records its definition site (`Defined:` in failure
   blocks); `.ppg3/last_run.json` persists label/defsite/status per job and
   `ppg3 why <output path>` goes from a file back to the pipeline.py:line
   that declared it.]

- gc is not removing staging. generally, gc needs rework,
  we need gc 'aggressive', gc 'minimal', 'gc default', gc 'failed only'
  [RESOLVED — `--level {failed-only,minimal,default,aggressive,reset}`, §11.2;
   staging/leases/intents/violations cleaned at every level]


- generations are not stored in the store, but in .pppg3
- so are outputs. sheee...
  [RESOLVED — correct by design: P3. Store = shared truth, .ppg3 = this
   project's generations/outputs (pointers into the store).]

- the 'no output on job fail' thing is idiotic. We should build as much as possible.
  [RESOLVED — every job that finishes still publishes; a failure only stops
   the view swap. Finished outputs are browsable under `partial/` (P8.2).]

- should we even have a new generation if output == output, and change_id==change_id?

- need a ppg3 jj-add-ignores
  [RESOLVED — `ppg3 jj-add-ignores` adds `/.ppg3/` and `/outputs` to the
   project's `.gitignore` (jj honours it); idempotent, preserves existing
   entries.]
- how do I do a verify run?
  [PARTIAL — `ppg3 store verify [--sample PCT|--entry OH]` re-hashes entries
   against their manifests; sampling now always includes unsandboxed
   entries first (P6: first-choice re-verification targets) and failures
   name the entry directory, not just the hash. A rebuild-and-compare
   verify (re-run jobs, diff against the stored entry) is still open.]
- how do I get from a store path to the python that generated it?
  [PARTIAL — from an *output-tree* path: `ppg3 why <path>` (definition site,
   status, root cause, entry/log paths). From a raw entries/<oh> path: still
   open.]

- how do I set a commandjobs stdout in the view?

- tool without name is kinda useless

- is a tool spec even sensible? or should that just be another input job??
 I mean, python on FileJobs (should by PythonJobs...) is sensible, 
 since it's such a central role.


- jobs that didn't produce their views completly derail the error output, 
  they don't get listed in the table, and they fail afterwards with a stupid error message
  [RESOLVED — a job that finishes without writing a declared output is now an
   ordinary aggregated job failure (scheduler-side check, fresh builds *and*
   cached hits): it appears in the failure table with `Missing:` + the store
   entry path + what it did write, gets a failure.log, and cascades normally.
   Never reaches the views layer. Also: cascaded "did not run" jobs no longer
   get one block each — one compact section grouped by root cause.]

- the whole name thing is unsound and needs fable level rethinking.


- having actual 'is this thing still on' output would be nice.
At least a thread that updates 'running / todo / failed' 
on the cli every second...

- tools should support tofu! Should we split the hash and the flake-ref

- how do I even enable the sandbox?
  [RESOLVED — `ppg3.new(sandbox="require" | "auto" | "off")` (P6.1/P6.2);
   `auto` is the default and warns once per run when enforcement is
   unavailable; `off` never nags. Needs bwrap + nix on PATH for real
   enforcement (P6.3 stays `untested` in the principles manifest until a
   host with both runs the suite).]


- the error message on job-contract violation (producing two different outputs from the same inputs)
 is atrocious:
 Job:       anton
Exception: out.txt: size 61->62, blake3
           6bdc286abdc168f0154a7d5fdb01945dcb45c1eb34d0dcea3e02b9ce69a43191->6fa7ef543cb499e3d96a8d26213530fdbe149a550b123c6bf5c6dbb3075e720a,
           mode 0444->0444, first differing byte offset 59
a) no log
b) no reference to the two folders involved
c) why is it logging the mode
d) no clear 'this is what happend' message
e) what jobs is it talking about?
  [RESOLVED — `Store::diff_report` now opens with "the same inputs (memo
   link ...) produced two different outputs", names both the previous-entry
   and this-build directories, lists added/removed/changed files, and ends
   with the `ppg3 diff-entries` command to dig further. Verified in source.]



-error messages on fetchjobs:
They have no user actionable stuff.

example: Hash mismatch on a fetchjob.
```
    print(ppg3.run())
          ~~~~~~~~^^
  File "/home/finkernagel/upstream/pypipegraph2/ppg3/python/ppg3/run.py", line 347, in run
    raise PPGRunError(RunResult(report, generation=None))
ppg3.run.PPGRunError: ppg3 run: 1 job(s) failed: 'incoming/test.dat': job failed: job "incoming/test.dat" exited with code 1
--- stderr tail ---
ppg3._shim: fetch hash mismatch for https://raw.githubusercontent.com/TyberiusPrime/fastqrab/refs/heads/main/README.md: expected 8b4e5da92263fa998f65a7692ed9dbc09e4b89a88e59c5b5ca07fb02c1ecadc6, got d08ed3d7214ea58d3091be4e67d70547b48692234bb3d9ef7c93d57b4b8d7514
```

Good: We got the url. Bad: Neither the store/entries we're comparing against,
nor the downloaded version is present.
  [RESOLVED — `_shim.run_fetch` now reports the url, both hashes (labelled
   pinned/old vs freshly-downloaded), and the absolute path where the
   rejected download was kept for diffing. Tested by P7.4/P9.3
   (test_p07_fetch.py). Verified.]



---
ppg3.run()
    On any job failure, the view is left untouched (§11: a generation must
    be a consistent, all-or-nothing snapshot) and :class:`PPGRunError` is
    raised with the full report attached.

That's awful. The user wants to inspect job outputs as they're done,
not after a multi day run...
  [RESOLVED — the view stays all-or-nothing (P8.3), but every *finished*
   job's outputs are now linked into `<project_dir>/partial/` on a failed
   run (P8.2, run.py `_write_partial_tree`), and `format_failures` points at
   it. Inspect partial results without store archaeology. Verified.]


And the report sucks.

---

Jobs creating the same target views are in conflict once you add in a name,
and the error is bad, and I don't even think that 'name=' should be a thing.


-- 
rollback ux is shit. 
It needs at least to print how to get back.
  [RESOLVED — rollback now prints the generation it came from and the exact
   `ppg3 rollback <prev>` to return; JSON gains `rolled_back_from`.]

It needs to be able to produce the link tree in a different folder.
  [RESOLVED — `ppg3 materialize <dest> --generation N` (see above) writes any
   generation's tree into a folder of your choosing.]

-- 
where is the 'materialize' command that turns an output folder into 
a non-symlinked copy?
  [RESOLVED — `ppg3 materialize <dest> [--generation N]` copies a
   generation's view tree into a fresh directory of real, writable files
   (0644), dereferencing the store symlinks; refuses to clobber an existing
   dest. Core: `views::materialize`.]

-- 
ux generations:
'created_at_ms' - user facing timestamps? seriously?
  [RESOLVED — `generations list` human output now shows a `CREATED (UTC)`
   column formatted `YYYY-MM-DD HH:MM:SSZ` (dependency-free); `--json` keeps
   the raw `created_at` ms for machines.]

-- 
what does 
generations keep even do?

--
explain should list the diff-entries command.
  [RESOLVED — `explain`'s human output now prints the ready-to-run
   `ppg3 diff-entries <oh_a> <oh_b>` for the two entries it compared.]
diff enries should offer to actually diff the damn files...
  [RESOLVED — `ppg3 diff-entries A B --diff` now prints the line-level
   content diff of each changed file (self-contained LCS) plus the bodies of
   added/removed files; binary/oversized files are noted, not dumped.]

-- 
gc does nothing. even after removing all the generations
  [RESOLVED — that was `default` keeping unrooted entries as cache. Use
   `ppg3 gc --level aggressive` (reclaim every unrooted entry) or `reset`.]

--
wtf is meta.json, what's the use for the user?
And isn't it's entries essentially just a recapulation of the symlinks???

--
ppg3 binary
auto find the store if there is only one.
  [RESOLVED — `--store` is now optional on `store gc/nuke/verify` and
   `diff-entries`; when omitted, ppg3 walks up for the project and uses its
   sole configured store, erroring (asking for --store) on zero or many.]


-- 
how do I even change the output folder?

--
failing command jobs are only detected because of missing output?
  [VERIFIED NOT SO — a nonzero exit code fails the job even when every
   declared output was written (scheduler.rs checks exit_code before
   publish; the staging dir is kept and reported as Outputs:/Kept:).
   Missing outputs are a *second*, independent failure class.]



-- 
can I fod an input file, so it will fail loudly if it ever changes?
For documentation purposes?
--


JobIO objects need a str. 
And generally a rework, the whole thing seems whack, 
or at least I had to look at the damn source.
path should be out_path - and return a Path(!).
Same for input...  what happens if an input has more than one file?
  [PARTIALLY RESOLVED — `JobIO` now has a `__repr__` listing declared
   inputs/outputs/tools/params; `io.input()` and `io.path()`/`io.out_path()`
   return `pathlib.Path`; `tool()` stays `str` (spliced into commands). The
   multi-file-input data model (an input name -> several files) is still the
   flagged bigger rework.]



--
Print's are getting lost before exceptions?
needs a flush?
  [RESOLVED — yes: forkserver children exit via os._exit(), which skips
   Python's buffer flush, so block-buffered stdout before a raise was lost.
   `_shim.main` now flushes stdout on the failure path and stdout+stderr in
   a finally, before returning to the os._exit() caller.]


-- when there's only one job failing, show it's error log straight away
  [RESOLVED — with exactly one failed job, `format_failures` inlines the
   whole consolidated log under `--- full log ---` (and drops the truncated
   stderr-tail block that would otherwise duplicate it).]

-- fetch isn't sound.
Removing the hash doesn't trigger a refetch.
hell, even changing the hash doesn't trigger anything!

(and changing the url should trigger a refetch, even if we have 
a matching hash. most of the time it's a case of 'the user forgot to change the hash',
worst case it's a redownload, not a 'and we updated all the urls').
  [RESOLVED — a fetch's identity is (url, pin): the pin rides in the recipe
   hash and the url is a leaf input, so changing either forces a refetch;
   removing the pin gives a one-shot key that never memo-hits. Covered by
   P7.1/P7.2/P7.3 in test_p07_fetch.py. Verified.]


-- we need a ppg3 blake3sum command
  [RESOLVED — `ppg3 blake3sum <paths...>` (or stdin via `-`/no args),
   prints `<hash>  <path>` like sha256sum, `--json` for machine output.]



