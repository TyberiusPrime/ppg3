"""Output-contract + "why did this (not) run" reporting, end-to-end.

Covers the pipeline-facing half of the missing-declared-outputs work:

- a job that exits 0 without writing a declared output is an ordinary,
  aggregated job failure whose message/table names the store entry path and
  the declared names it missed (never a later views-layer crash);
- jobs that merely never ran because an upstream failed are aggregated into
  one "did not run" section instead of drowning the report, with the root
  cause resolved through cascade chains;
- every run (success or failure) records `.ppg3/last_run.json` — the
  machine-readable per-job story `ppg3 why` answers from.
"""

import json
import os

import pytest

import ppg3
from ppg3.run import PPGRunError
from ppg3.tools import PyEnv

from conftest import requires_core


def _new_graph(tmp_path):
    store_dir = tmp_path / "store"
    store_dir.mkdir(exist_ok=True)
    return ppg3.new(
        stores=[ppg3.Store("main", str(store_dir))],
        default_python=PyEnv.current(),
        project_dir=str(tmp_path / ".ppg3"),
        frozen=False,
        paranoid=True,
    )


@requires_core
def test_missing_declared_output_is_aggregated_job_error(tmp_path):
    g = _new_graph(tmp_path)
    # Declares output name "result" but writes a different file into /ppg/out.
    ppg3.CommandJob(
        outputs={"result": "out.txt"},
        argv=["/bin/sh", "-c", "echo oops > {out}/wrong-name.txt"],
    )
    with pytest.raises(PPGRunError) as exc_info:
        ppg3.run(g, project_id="contract")

    result = exc_info.value.result
    detail = result.failed_details["out.txt"]
    assert detail["missing_outputs"] == ["result"]

    reason = result.failed["out.txt"]
    assert "without writing declared output(s)" in reason
    assert '"result"' in reason
    assert '"wrong-name.txt"' in reason
    # P9.3: the message names the published entry, and the file the job did
    # write is really inspectable there.
    entry_dir = detail["out_dir"]
    assert entry_dir in reason
    assert os.path.isfile(os.path.join(entry_dir, "wrong-name.txt"))

    table = result.format_failures()
    assert "Missing:" in table and "result" in table
    assert "Outputs:" in table

    # No generation was written; the failure did NOT derail into a views
    # error (the old behavior raised a raw canonicalize/io RuntimeError).
    assert result.generation is None

    # Second run: the published entry is a cache hit — same failure story,
    # now explicitly marked as coming from the cached entry.
    g2 = _new_graph(tmp_path)
    ppg3.CommandJob(
        outputs={"result": "out.txt"},
        argv=["/bin/sh", "-c", "echo oops > {out}/wrong-name.txt"],
    )
    with pytest.raises(PPGRunError) as exc_info2:
        ppg3.run(g2, project_id="contract")
    reason2 = exc_info2.value.result.failed["out.txt"]
    assert "cached entry" in reason2
    assert entry_dir in reason2


@requires_core
def test_upstream_casualties_are_aggregated_not_blocks(tmp_path):
    g = _new_graph(tmp_path)
    bad = ppg3.CommandJob(outputs={"bad": "bad.txt"}, argv=["/bin/sh", "-c", "exit 7"])
    mid = ppg3.CommandJob(
        outputs={"mid": "mid.txt"},
        argv=["/bin/sh", "-c", "echo mid > {out}/mid"],
        inputs={"src": bad},
    )
    ppg3.CommandJob(
        outputs={"leaf": "leaf.txt"},
        argv=["/bin/sh", "-c", "echo leaf > {out}/leaf"],
        inputs={"src": mid},
    )
    with pytest.raises(PPGRunError) as exc_info:
        ppg3.run(g, project_id="cascade")

    result = exc_info.value.result
    # Root cause resolved transitively: leaf.txt's upstream is bad.txt,
    # not the intermediate mid.txt.
    assert result.upstream_failed == {"mid.txt": "bad.txt", "leaf.txt": "bad.txt"}
    assert "upstream job bad.txt failed" in result.failed["leaf.txt"]

    table = result.format_failures()
    # Exactly one real failure block; the casualties are one compact section.
    assert "1 job(s) failed" in table
    assert table.count("Job:") == 1
    assert "2 more job(s) did not run because an upstream failed" in table
    assert "because bad.txt failed: leaf.txt, mid.txt" in table


@requires_core
def test_last_run_json_records_the_per_job_story(tmp_path):
    g = _new_graph(tmp_path)
    ppg3.CommandJob(outputs={"ok": "ok.txt"}, argv=["/bin/sh", "-c", "echo ok > {out}/ok"])
    bad = ppg3.CommandJob(outputs={"bad": "bad.txt"}, argv=["/bin/sh", "-c", "exit 3"])
    ppg3.CommandJob(
        outputs={"leaf": "leaf.txt"},
        argv=["/bin/sh", "-c", "echo leaf > {out}/leaf"],
        inputs={"src": bad},
    )
    with pytest.raises(PPGRunError):
        ppg3.run(g, project_id="lastrun")

    path = tmp_path / ".ppg3" / "last_run.json"
    data = json.loads(path.read_text())
    by_label = {j["label"]: j for j in data["jobs"]}

    assert data["generation"] is None
    assert by_label["ok.txt"]["status"] == "built"
    assert by_label["ok.txt"]["entry"], "built jobs record their entry path"
    assert by_label["bad.txt"]["status"] == "failed"
    assert by_label["bad.txt"]["exit_code"] == 3
    assert by_label["leaf.txt"]["status"] == "not_run"
    assert by_label["leaf.txt"]["upstream"] == "bad.txt"
    # P1.5/P9.1: every record carries its definition site, pointing at this
    # test file.
    assert "test_output_contract.py" in by_label["bad.txt"]["defsite"]

    # Success path records too (fix the pipeline, re-run).
    g2 = _new_graph(tmp_path)
    ppg3.CommandJob(outputs={"ok": "ok.txt"}, argv=["/bin/sh", "-c", "echo ok > {out}/ok"])
    good = ppg3.CommandJob(
        outputs={"bad": "bad.txt"}, argv=["/bin/sh", "-c", "echo fixed > {out}/bad"]
    )
    ppg3.CommandJob(
        outputs={"leaf": "leaf.txt"},
        argv=["/bin/sh", "-c", "echo leaf > {out}/leaf"],
        inputs={"src": good},
    )
    result = ppg3.run(g2, project_id="lastrun")
    data2 = json.loads(path.read_text())
    by_label2 = {j["label"]: j for j in data2["jobs"]}
    assert data2["generation"] == result.generation
    assert by_label2["ok.txt"]["status"] == "hit"
    assert by_label2["leaf.txt"]["status"] == "built"
