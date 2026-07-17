
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

-- we are corrently loosing the file names on our python tracebacks. Add those back in.

- is producing extra files an error? do they get hashed?
  or are non-view files just not exported into the output?
  Think it should be the later.

- can we add local python files to the PyEnv as importable modules? Dependencies then?

- we currently can't have a FileJob that doesn't have a view. 
  Why?, nothing wrong with internal jobs! And do we need a stable job id?
  can't have conflicts on job_ids, but are they truly necessary?
  and if not, should we mayhaps link them so users can find their jobs?
  because right now, they do not end up in store at al.o

- can't rm -rf the store?
  I mean I get it... maybe we add a ppg3 nuke-store
  [RESOLVED — `ppg3 store nuke --store PATH --yes`]

- binary not in nd...

- can we capture the python definition sites for the error output?

- gc is not removing staging. generally, gc needs rework,
  we need gc 'aggressive', gc 'minimal', 'gc default', gc 'failed only'
  [RESOLVED — `--level {failed-only,minimal,default,aggressive,reset}`, §11.2;
   staging/leases/intents/violations cleaned at every level]


- generations are not stored in the store, but in .pppg3
- so are outputs. sheee...
  [RESOLVED — correct by design: P3. Store = shared truth, .ppg3 = this
   project's generations/outputs (pointers into the store).]

- the 'no output on job fail' thing is idiotic. We should build as much as possible.

- should we even have a new generation if output == output, and change_id==change_id?

- need a ppg3 jj-add-ignores
- how do I do a verify run?
- how do I get from a store path to the python that generated it?

- how do I set a commandjobs stdout in the view?

- tool without name is kinda useless

- is a tool spec even sensible? or should that just be another input job??
 I mean, python on FileJobs (should by PythonJobs...) is sensible, 
 since it's such a central role.


- jobs that didn't produce their views completly derail the error output, 
  they don't get listed in the table, and they fail afterwards with a stupid error message

- the whole name thing is unsound and needs fable level rethinking.


- having actual 'is this thing still on' output would be nice.
At least a thread that updates 'running / todo / failed' 
on the cli every second...

- tools should support tofu! Should we split the hash and the flake-ref

- how do I even enable the sandbox?


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



---
ppg3.run()
    On any job failure, the view is left untouched (§11: a generation must
    be a consistent, all-or-nothing snapshot) and :class:`PPGRunError` is
    raised with the full report attached.

That's awful. The user wants to inspect job outputs as they're done,
not after a multi day run...


And the report sucks.

---

Jobs creating the same target views are in conflict once you add in a name,
and the error is bad, and I don't even think that 'name=' should be a thing.


-- 
rollback ux is shit. 
It needs at least to print how to get back.

It needs to be able to produce the link tree in a different folder.

-- 
where is the 'materialize' command that turns an output folder into 
a non-symlinked copy?

-- 
ux generations:
'created_at_ms' - user facing timestamps? seriously?

-- 
what does 
generations keep even do?

--
explain should list the diff-entries command.
diff enries should offer to actually diff the damn files...

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


-- 
how do I even change the output folder?

--
failing command jobs are only detected because of missing output?



-- 
can I fod an input file, so it will fail loudly if it ever changes?
For documentation purposes?
--


JobIO objects need a str. 
And generally a rework, the whole thing seems whack, 
or at least I had to look at the damn source.
path should be out_path - and return a Path(!).
Same for input...  what happens if an input has more than one file?



--
Print's are getting lost before exceptions?
needs a flush?


-- when there's only one job failing, show it's error log straight away

-- fetch isn't sound.
Removing the hash doesn't trigger a refetch.
hell, even changing the hash doesn't trigger anything!

(and changing the url should trigger a refetch, even if we have 
a matching hash. most of the time it's a case of 'the user forgot to change the hash',
worst case it's a redownload, not a 'and we updated all the urls').


-- we need a ppg3 blake3sum command
  [RESOLVED — `ppg3 blake3sum <paths...>` (or stdin via `-`/no args),
   prints `<hash>  <path>` like sha256sum, `--json` for machine output.]



