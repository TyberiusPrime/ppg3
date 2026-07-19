
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

closed:

- should we even have a new generation if output == output, and change_id==change_id?

- how do I set a commandjobs stdout in the view?

- tool without name is kinda useless

- is a tool spec even sensible? or should that just be another input job??
 I mean, python on FileJobs (should by PythonJobs...) is sensible, 
 since it's such a central role.


- having actual 'is this thing still on' output would be nice.
At least a thread that updates 'running / todo / failed' 
on the cli every second...

- tools should support tofu! Should we split the hash and the flake-ref


-error messages on fetchjobs:
They still lack the path to the originally stored value (or the message that
we ain't got such a value!).
Job:       test.txt
Defined:   /home/finkernagel/upstream/ppg3-standalone/test/pipeline.py:65
Runtime:   246ms
Exception: diff them against your pinned copy to see what upstream changed.
Stderr:
           ppg3._shim: fetch hash mismatch for https://coonabibba.de/files/test.txt:
             expected blake3 023311e40a171fde5a76ffef70fa29768d0068867cb0672d17a24b7071070913 (the pinned/old content)
             got      blake3 02b311e40a171fde5a76feef7afa29768d0068867cb0672d17a24b7071070913 (the freshly downloaded content)
             the downloaded bytes were kept at: /tmp/nix-shell.pVWY9X/ppg3-session-62tcexvj/05e57b59d1c14e348c5de9e474c087054f480dc666eeab0c4f7f964ccce0044a-18c3a12d3bbb30f8-0/ppg/out/file.rejected
             diff them against your pinned copy to see what upstream changed.
Log:       store/v1/logs/05e57b59d1c14e348c5de9e474c087054f480dc666eeab0c4f7f964ccce0044a/1784447093016-host/failure.log
Outputs:   store/v1/staging/host-1665340-18c3a12d3bb924610/data
Kept:      store/v1/staging/host-1665340-18c3a12d3bb924610/data/file.rejected

And they shouldn't rename the file.


--
wtf is meta.json, what's the use for the user?
And isn't it's entries essentially just a recapulation of the symlinks???


-- 
how do I even change the output folder?
  [the neat part -you don't]

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



-- The 'blake3 write back into python' only happens
after all jobs run successful - we should be doing this
after every fetch job.


-- watcher, code, syntax highlighting?
