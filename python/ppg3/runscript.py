"""Run-script ownership guard for a project directory.

A project directory holds ONE generation sequence, one ``views/current``,
and one ``outputs/`` symlink. Two different run scripts sharing it don't
corrupt anything, but each run silently replaces the other script's
``outputs/`` — almost always an accident (a copy-pasted script, a second
pipeline started in the same folder). This module makes that accident loud:

- :func:`check_and_record` records the running script's path in
  ``<project_dir>/run_script`` (its own file, nothing else in it) on the
  first run, and refuses later runs made by a *different* script, telling
  the user to delete the file if the takeover is intentional.
- Renames/moves are recognized, not punished: when the recorded script no
  longer exists on disk *and* no other ``*.py`` file next to the project
  dir looks like a ppg3 run script, the record is updated silently — the
  user renamed their one pipeline script, nothing is contested.

The identity used is ``sys.argv[0]`` (resolved via ``os.path.realpath``):
that is the pipeline script for ``python script.py`` and — because
``watch.py``'s ``_run_definition_pass`` swaps ``sys.argv`` — for
``python -m ppg3 watch script.py`` too. Interactive sessions (REPL,
``python -c``) have no script identity; the guard skips them entirely.

Like everything under ``project_dir``, the record is pointer state
(PRINCIPLES.md P3): deleting it loses only the decision, never results.
"""

from __future__ import annotations

import os
import re
import sys
from typing import List, Optional

#: File name inside ``project_dir`` holding the recorded script path.
RUN_SCRIPT_FILE = "run_script"

# Heuristic for "this .py file is a ppg3 run script": it mentions ppg3
# (an `import ppg3`, `from ppg3 import ...`) and calls `ppg3.run(` or a
# bare `run(` (the `from ppg3 import run` spelling) — a method call on
# anything else (`subprocess.run(`, `proc.run(`) does not count. An
# aliased `import ppg3 as pp; pp.run()` slips through; over-matching the
# other way is safe — it only turns a silent record update into the
# explicit error, never the reverse.
_RUN_CALL_RE = re.compile(r"ppg3\s*\.\s*run\s*\(|(?<![\w.])run\s*\(")


class RunScriptChangedError(RuntimeError):
    """Raised by :func:`check_and_record` when a project directory that was
    last used by one run script is now being run by a different one."""


def current_script() -> Optional[str]:
    """The running script's real path, or ``None`` when there is no script
    to speak of (interactive REPL, ``python -c``, embedded interpreter)."""
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0 in ("", "-", "-c"):
        return None
    return os.path.realpath(argv0)


def _calls_ppg3_run(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return False
    return "ppg3" in text and _RUN_CALL_RE.search(text) is not None


def _other_run_scripts(folder: str, exclude: str) -> List[str]:
    """``*.py`` files directly in ``folder`` (not recursive) that look like
    ppg3 run scripts, excluding ``exclude`` (the script currently running)."""
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    out = []
    for name in names:
        if not name.endswith(".py"):
            continue
        path = os.path.join(folder, name)
        if os.path.realpath(path) == exclude or not os.path.isfile(path):
            continue
        if _calls_ppg3_run(path):
            out.append(path)
    return out


def check_and_record(project_dir: str, script: Optional[str] = None) -> None:
    """Enforce single-script ownership of ``project_dir`` (see module doc).

    Called by :func:`ppg3.run` before any other side effect. ``script``
    overrides the auto-detected identity (tests). No script identity at all
    (interactive use) skips the check and leaves any record untouched.

    Raises :class:`RunScriptChangedError` when a different script than the
    recorded one is running, unless the recorded script is gone from disk
    and no other candidate run script sits next to the project dir — the
    rename/move case, where the record is silently updated instead.
    """
    if script is None:
        script = current_script()
    if script is None:
        return

    record_path = os.path.join(project_dir, RUN_SCRIPT_FILE)
    try:
        with open(record_path, "r", encoding="utf-8") as fh:
            recorded = fh.read().strip()
    except OSError:
        recorded = ""

    if recorded == script:
        return

    if recorded:
        folder = os.path.dirname(os.path.abspath(project_dir))
        contenders = _other_run_scripts(folder, exclude=script)
        if os.path.exists(recorded) or contenders:
            hint = (
                "  (other run script(s) in the same folder: "
                + ", ".join(os.path.basename(p) for p in contenders)
                + ")\n"
                if contenders
                else ""
            )
            raise RunScriptChangedError(
                f"this project directory ({project_dir}) was last run by a "
                "different script:\n"
                f"  recorded: {recorded}\n"
                f"  running:  {script}\n"
                f"{hint}"
                "Two scripts sharing one project dir take turns overwriting "
                "each other's outputs/ (one generation sequence, one "
                "`current`). Either:\n"
                f"  - give this script its own project dir: "
                "ppg3.new(project_dir=...)\n"
                f"  - or, if the switch is intentional, delete {record_path} "
                "and re-run"
            )
        # The recorded script is gone and nothing else around claims this
        # project dir: a rename/move of the one pipeline script. Adopt it.

    os.makedirs(project_dir, exist_ok=True)
    tmp = record_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(script + "\n")
    os.replace(tmp, record_path)
