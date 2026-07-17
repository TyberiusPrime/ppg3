"""P9 — Every failure names its artifacts. See PRINCIPLES.md.

P9.3 (artifact paths in violation/mismatch reports) is asserted alongside
the scenarios that produce those reports: test_p05_determinism.py and
test_p07_fetch.py. P9.4 (no bare hashes) lives in test_p05_determinism.py.
"""

from __future__ import annotations

import inspect
import os
import re

import pytest

from principles_helpers import command_job, file_job, new_graph, principle, requires_core, run_graph


def _boom(io):
    raise ValueError("KABOOM-P9")


@requires_core
@principle("P9.1")
def test_failure_blocks_include_the_definition_site(tmp_path):
    g = new_graph(tmp_path)
    defsite_line = inspect.currentframe().f_lineno + 1
    file_job({"out": "fails.txt"}, run=_boom)
    with pytest.raises(Exception) as ei:
        run_graph(g)
    msg = str(ei.value)
    assert re.search(rf"test_p09_errors\.py:{defsite_line}\b", msg), (
        "the report must say where the failing job was defined — that line "
        "is where the user starts fixing"
    )


@requires_core
@principle("P9.2")
def test_single_failure_shows_log_path_and_inlines_the_evidence(tmp_path):
    g = new_graph(tmp_path)
    command_job(
        {"out": "fails.txt"},
        argv=["/bin/sh", "-c", "echo UNIQUE-STDERR-MARKER-42 >&2; exit 1"],
    )
    with pytest.raises(Exception) as ei:
        run_graph(g)
    msg = str(ei.value)

    log_paths = [
        tok.rstrip(".,:;)")
        for tok in re.findall(r"/\S+", msg)
        if os.path.exists(tok.rstrip(".,:;)"))
    ]
    assert log_paths, "no existing log path in the failure block"

    # Exactly one job failed: its evidence belongs in the report itself,
    # not behind another round-trip to a log file.
    assert "UNIQUE-STDERR-MARKER-42" in msg, (
        "the job's own words (stderr tail) were not inlined for a "
        "single-failure run"
    )
