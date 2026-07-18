"""Tests for the run-script ownership guard (``ppg3/runscript.py``): a
project directory records the script that runs it in its own
``run_script`` file; a *different* script is refused (delete the file to
override), except for the recognized rename/move case (recorded script
gone, no other candidate run script in the folder), which updates the
record silently."""

import os
import subprocess
import sys

import pytest

import ppg3
from conftest import PYTHON_DIR, requires_core
from ppg3.runscript import (
    RUN_SCRIPT_FILE,
    RunScriptChangedError,
    check_and_record,
    current_script,
)


def _record(project_dir):
    path = os.path.join(str(project_dir), RUN_SCRIPT_FILE)
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        return fh.read().strip()


def _script(tmp_path, name, body="import ppg3\nppg3.run()\n"):
    path = tmp_path / name
    path.write_text(body)
    return os.path.realpath(str(path))


def test_first_run_records_script(tmp_path):
    project_dir = str(tmp_path / ".ppg3")
    a = _script(tmp_path, "a.py")
    check_and_record(project_dir, script=a)
    assert _record(project_dir) == a


def test_same_script_passes(tmp_path):
    project_dir = str(tmp_path / ".ppg3")
    a = _script(tmp_path, "a.py")
    check_and_record(project_dir, script=a)
    check_and_record(project_dir, script=a)
    assert _record(project_dir) == a


def test_changed_script_refused_while_old_exists(tmp_path):
    project_dir = str(tmp_path / ".ppg3")
    a = _script(tmp_path, "a.py")
    b = _script(tmp_path, "b.py")
    check_and_record(project_dir, script=a)
    with pytest.raises(RunScriptChangedError) as exc:
        check_and_record(project_dir, script=b)
    msg = str(exc.value)
    assert "a.py" in msg and "b.py" in msg
    # The override instruction names the exact file to delete.
    assert os.path.join(project_dir, RUN_SCRIPT_FILE) in msg
    # The record is left untouched by a refused run.
    assert _record(project_dir) == a


def test_deleting_record_overrides_the_decision(tmp_path):
    project_dir = str(tmp_path / ".ppg3")
    a = _script(tmp_path, "a.py")
    b = _script(tmp_path, "b.py")
    check_and_record(project_dir, script=a)
    os.remove(os.path.join(project_dir, RUN_SCRIPT_FILE))
    check_and_record(project_dir, script=b)
    assert _record(project_dir) == b


def test_rename_adopted_silently(tmp_path):
    """Recorded script gone + no other run script in the folder = a rename;
    the record updates without an error."""
    project_dir = str(tmp_path / ".ppg3")
    a = _script(tmp_path, "a.py")
    check_and_record(project_dir, script=a)
    os.remove(a)
    b = _script(tmp_path, "b.py")
    check_and_record(project_dir, script=b)
    assert _record(project_dir) == b


def test_rename_contested_by_other_run_script_refused(tmp_path):
    """Recorded script gone, but ANOTHER .py calling ppg3.run() sits in the
    folder: ambiguous — refuse rather than silently adopt."""
    project_dir = str(tmp_path / ".ppg3")
    a = _script(tmp_path, "a.py")
    _script(tmp_path, "other.py")
    check_and_record(project_dir, script=a)
    os.remove(a)
    b = _script(tmp_path, "b.py")
    with pytest.raises(RunScriptChangedError) as exc:
        check_and_record(project_dir, script=b)
    assert "other.py" in str(exc.value)


def test_non_run_python_files_do_not_contest(tmp_path):
    """A helper .py that never calls run() (no ppg3 mention / no run call)
    does not block the rename adoption."""
    project_dir = str(tmp_path / ".ppg3")
    a = _script(tmp_path, "a.py")
    _script(tmp_path, "helpers.py", body="def helper():\n    return 1\n")
    _script(tmp_path, "uses_ppg3.py", body="import ppg3  # definition-only\n")
    # Mentions ppg3 and calls *a* run() — but a method on something else:
    _script(
        tmp_path,
        "driver.py",
        body="import subprocess  # drives a ppg3 pipeline\nsubprocess.run(['x'])\n",
    )
    check_and_record(project_dir, script=a)
    os.remove(a)
    b = _script(tmp_path, "b.py")
    check_and_record(project_dir, script=b)
    assert _record(project_dir) == b


def test_moved_project_adopted_silently(tmp_path):
    """Moving the whole folder (script + project dir) changes the recorded
    absolute path; the old path is gone and nothing contests — adopt."""
    old = tmp_path / "old"
    old.mkdir()
    a = _script(old, "pipeline.py")
    project_dir_old = str(old / ".ppg3")
    check_and_record(project_dir_old, script=a)
    new = tmp_path / "new"
    os.rename(str(old), str(new))
    project_dir_new = str(new / ".ppg3")
    moved = os.path.realpath(str(new / "pipeline.py"))
    check_and_record(project_dir_new, script=moved)
    assert _record(project_dir_new) == moved


def test_interactive_use_skips_guard(tmp_path, monkeypatch):
    """No script identity (REPL / python -c): no check, no record written,
    and an existing record from script use is left untouched."""
    project_dir = str(tmp_path / ".ppg3")
    for argv in ([""], ["-c"], []):
        monkeypatch.setattr(sys, "argv", argv)
        assert current_script() is None
        check_and_record(project_dir)
        assert _record(project_dir) is None
    a = _script(tmp_path, "a.py")
    check_and_record(project_dir, script=a)
    monkeypatch.setattr(sys, "argv", [""])
    check_and_record(project_dir)  # interactive after script use: no error
    assert _record(project_dir) == a


PIPELINE = """\
import ppg3
from ppg3.tools import PyEnv

ppg3.new(stores=[ppg3.Store("main", "store")], default_python=PyEnv.current())
ppg3.CommandJob(
    outputs={{"o": "{out}"}},
    argv=["/bin/sh", "-c", "echo hi > {{out:o}}"],
)
ppg3.run()
"""


def _run_script(path, cwd):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PYTHON_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, str(path)],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


@requires_core
def test_e2e_second_script_refused_then_override(tmp_path):
    """The real thing: script A owns .ppg3; script B is refused with the
    delete-the-file hint; deleting the record lets B take over."""
    a = tmp_path / "a.py"
    a.write_text(PIPELINE.format(out="a.txt"))
    b = tmp_path / "b.py"
    b.write_text(PIPELINE.format(out="b.txt"))

    proc = _run_script(a, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "outputs" / "a.txt").exists()

    proc = _run_script(b, tmp_path)
    assert proc.returncode != 0
    assert "a.py" in proc.stderr and "b.py" in proc.stderr
    assert "run_script" in proc.stderr
    # Refused before any side effect: no generation was written for b.
    assert (tmp_path / "outputs" / "a.txt").exists()
    assert not (tmp_path / "outputs" / "b.txt").exists()

    os.remove(tmp_path / ".ppg3" / RUN_SCRIPT_FILE)
    proc = _run_script(b, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "outputs" / "b.txt").exists()


@requires_core
def test_e2e_renamed_script_runs_silently(tmp_path):
    a = tmp_path / "pipeline.py"
    a.write_text(PIPELINE.format(out="x.txt"))
    proc = _run_script(a, tmp_path)
    assert proc.returncode == 0, proc.stderr
    renamed = tmp_path / "renamed.py"
    os.rename(str(a), str(renamed))
    proc = _run_script(renamed, tmp_path)
    assert proc.returncode == 0, proc.stderr
    record = (tmp_path / ".ppg3" / RUN_SCRIPT_FILE).read_text().strip()
    assert record == os.path.realpath(str(renamed))
